"""Bounded lexical retrieval over the route-owned SQLite FTS indexes.

Lexical scores remain in one ranking per source.  They are intentionally not
normalized or merged here because SQLite BM25 values from different corpora do
not share a calibrated scale; callers can combine ranks with RRF instead.
"""

from __future__ import annotations

import math
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from .semantic_models import EmbeddingModality, ResolvedSearchHit, SearchHit
from .sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from .sqlite_paths import readonly_sqlite_uri

# region [01] Public contracts and limits

MAX_LEXICAL_RESULTS = 1_000
MAX_QUERY_CHARS = 4_096
MAX_QUERY_TERMS = 64
MAX_QUERY_TERM_CHARS = 128
MAX_SNIPPET_CHARS = 1_024
_CANCELLATION_BATCH_ROWS = 128

LEXICAL_MODEL_SIGNATURE = "sqlite-fts5-unicode61-rd2-v2"
LEXICAL_QUERY_POLICY_SIGNATURE = "sqlite-fts5-natural-strict-soft-v2"
_SOURCE_ORDER = ("pdf", "docx", "office", "audio", "archive", "text")


class LexicalAvailability(StrEnum):
    """Whether a route-owned source can participate in lexical retrieval."""

    AVAILABLE = "available"
    DATABASE_MISSING = "database_missing"
    NOT_CONFIGURED = "not_configured"
    READ_FAILED = "read_failed"


@dataclass(frozen=True, slots=True)
class LexicalStatePaths:
    """Optional locations of route-owned FTS databases."""

    pdf: Path | None = None
    docx: Path | None = None
    office: Path | None = None
    audio: Path | None = None
    archive: Path | None = None
    text: Path | None = None

    def ordered(self) -> tuple[tuple[str, Path | None], ...]:
        base = (
            ("pdf", self.pdf),
            ("docx", self.docx),
            ("office", self.office),
            ("audio", self.audio),
        )
        optional: list[tuple[str, Path | None]] = []
        if self.archive is not None:
            optional.append(("archive", self.archive))
        if self.text is not None:
            optional.append(("text", self.text))
        return (*base, *optional)


@dataclass(frozen=True, slots=True)
class LexicalRanking:
    """One independent source ranking plus explicit availability metadata."""

    source_kind: str
    state_path: Path | None
    availability: LexicalAvailability
    normalized_query: str
    hits: tuple[ResolvedSearchHit, ...]
    unavailable_reason: str | None = None
    elapsed_ns: int = field(default=0, compare=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.elapsed_ns, bool)
            or not isinstance(self.elapsed_ns, int)
            or self.elapsed_ns < 0
        ):
            raise ValueError("lexical ranking elapsed_ns cannot be negative")

    @property
    def ranking_name(self) -> str:
        return f"fts_{self.source_kind}"

    @property
    def search_hits(self) -> tuple[SearchHit, ...]:
        """Expose the ranking directly to ``reciprocal_rank_fusion``."""

        return tuple(resolved.hit for resolved in self.hits)


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    source_kind: str
    fts_table: str
    sql: str
    section_kind: str


# endregion [01]


# region [02] Safe natural-query compilation

_NATURAL_TERM = re.compile(r"[^\W_]+", flags=re.UNICODE)

# These are grammar words, not domain concepts.  They are removed only after
# the strict all-term query returns no rows, so existing exact matches and FTS
# operator escaping retain their historical behavior.
_NATURAL_STOPWORDS = frozenset(
    {
        # Spanish
        "a",
        "al",
        "como",
        "con",
        "de",
        "del",
        "el",
        "en",
        "la",
        "las",
        "lo",
        "los",
        "o",
        "para",
        "por",
        "que",
        "sobre",
        "un",
        "una",
        "y",
        # English
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        # German
        "auf",
        "das",
        "dem",
        "den",
        "der",
        "des",
        "die",
        "ein",
        "eine",
        "einem",
        "einen",
        "einer",
        "eines",
        "für",
        "im",
        "mit",
        "oder",
        "und",
        "von",
        "zu",
    }
)
_MAX_SOFT_FALLBACK_TERMS = 5


@dataclass(frozen=True, slots=True)
class _NaturalFTSQueryPlan:
    strict_query: str
    fallbacks: tuple[tuple[str, str], ...]


def _natural_query_terms(query: str) -> tuple[str, ...]:
    """Validate one natural query and return stable case-insensitive terms."""

    value = query.strip()
    if not value:
        raise ValueError("lexical search query must be non-empty")
    if len(value) > MAX_QUERY_CHARS:
        raise ValueError(f"lexical search query cannot exceed {MAX_QUERY_CHARS} characters")
    terms = _NATURAL_TERM.findall(value)
    if not terms:
        raise ValueError("lexical search query must contain letters or numbers")
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError(f"lexical search query cannot exceed {MAX_QUERY_TERMS} terms")
    if any(len(term) > MAX_QUERY_TERM_CHARS for term in terms):
        raise ValueError(f"lexical search terms cannot exceed {MAX_QUERY_TERM_CHARS} characters")

    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_terms.append(term)
    return tuple(unique_terms)


def _quoted_fts_term(term: str) -> str:
    return f'"{term}"'


def _all_terms_query(terms: tuple[str, ...]) -> str:
    return " AND ".join(_quoted_fts_term(term) for term in terms)


def _soft_content_query(terms: tuple[str, ...]) -> str | None:
    """Require any two content terms for a bounded, deterministic fallback."""

    if not 3 <= len(terms) <= _MAX_SOFT_FALLBACK_TERMS:
        return None
    pairs = (
        f"({_quoted_fts_term(left)} AND {_quoted_fts_term(right)})"
        for left, right in combinations(terms, 2)
    )
    return " OR ".join(pairs)


def _compile_natural_fts_query_plan(query: str) -> _NaturalFTSQueryPlan:
    terms = _natural_query_terms(query)
    strict = _all_terms_query(terms)
    content_terms = tuple(
        term for term in terms if term.casefold() not in _NATURAL_STOPWORDS
    )
    fallbacks: list[tuple[str, str]] = []
    if content_terms and content_terms != terms:
        fallbacks.append(("content_terms_all", _all_terms_query(content_terms)))
    soft = _soft_content_query(content_terms)
    if soft is not None and soft not in {strict, *(query for _, query in fallbacks)}:
        fallbacks.append(("content_terms_any_two", soft))
    return _NaturalFTSQueryPlan(strict, tuple(fallbacks))


def compile_natural_fts_query(query: str) -> str:
    """Convert punctuation-rich natural text into a quoted FTS5 AND query.

    Only Unicode letter and number runs become terms.  Quoting every term keeps
    words such as ``OR`` or ``NEAR`` literal and prevents user punctuation from
    entering the FTS5 query grammar.
    """

    return _compile_natural_fts_query_plan(query).strict_query


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LEXICAL_RESULTS:
        raise ValueError(f"lexical search limit must be between 1 and {MAX_LEXICAL_RESULTS}")


# endregion [02]


# region [03] Source-specific read-only queries

_SPECS = {
    "pdf": _SourceSpec(
        source_kind="pdf",
        fts_table="page_fts",
        section_kind="page",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,f.page_number,
        snippet(page_fts,3,'[',']',' ... ',24) AS snippet,
        bm25(page_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.is_partial AS source_is_partial
        FROM page_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE page_fts MATCH ? AND d.status IN ('done','partial')
        ORDER BY raw_bm25,f.path COLLATE NOCASE,f.page_number LIMIT ?""",
    ),
    "docx": _SourceSpec(
        source_kind="docx",
        fts_table="document_fts",
        section_kind="document",
        sql="""WITH ranked AS MATERIALIZED (
        SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ?1 AND d.status IN ('complete','partial')
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?2
        )
        SELECT ranked.fts_rowid,ranked.file_key,ranked.path,
        snippet(document_fts,4,'[',']',' ... ',24) AS snippet,
        ranked.raw_bm25,ranked.source_size,ranked.source_mtime_ns,
        ranked.source_birthtime_ns,ranked.source_processing_signature,
        ranked.source_last_seen_run_id,ranked.source_status
        FROM ranked JOIN document_fts
        ON document_fts.rowid=ranked.fts_rowid
        WHERE document_fts MATCH ?1
        ORDER BY ranked.raw_bm25,ranked.path COLLATE NOCASE""",
    ),
    "office": _SourceSpec(
        source_kind="office",
        fts_table="document_fts",
        section_kind="document",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,f.format,
        snippet(document_fts,5,'[',']',' ... ',24) AS snippet,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
    ),
    "audio": _SourceSpec(
        source_kind="audio",
        fts_table="transcript_fts",
        section_kind="transcript",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        snippet(transcript_fts,3,'[',']',' ... ',24) AS snippet,
        bm25(transcript_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM transcript_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE transcript_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
    ),
    "archive": _SourceSpec(
        source_kind="archive",
        fts_table="document_fts",
        section_kind="archive_member",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        CASE WHEN d.text_chars>0
        THEN snippet(document_fts,6,'[',']',' ... ',24)
        ELSE d.member_chain END AS snippet,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.container_path,d.member_chain,d.member_path,d.archive_depth,d.content_kind
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ?
        AND d.status IN ('indexed','metadata_only','archive')
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
    ),
    "text": _SourceSpec(
        source_kind="text",
        fts_table="document_fts",
        section_kind="document",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        snippet(document_fts,5,'[',']',' ... ',24) AS snippet,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
    ),
}


def _bounded_snippet(value: object) -> str | None:
    if value is None:
        return None
    snippet = str(value)
    if len(snippet) <= MAX_SNIPPET_CHARS:
        return snippet
    return snippet[: MAX_SNIPPET_CHARS - 1] + "…"


def _resolved_hit(
    spec: _SourceSpec,
    state_path: Path,
    query_plan: _NaturalFTSQueryPlan,
    applied_query: str,
    query_strategy: str,
    row: sqlite3.Row,
    rank_position: int,
) -> ResolvedSearchHit:
    file_key = str(row["file_key"])
    item_source_kind = (
        str(row["format"]).strip().casefold() if spec.source_kind == "office" else spec.source_kind
    )
    if not item_source_kind:
        raise sqlite3.DataError("FTS5 result has a blank source kind")
    raw_bm25 = float(row["raw_bm25"])
    if not math.isfinite(raw_bm25):
        raise sqlite3.DataError("FTS5 returned a non-finite BM25 score")

    if spec.source_kind == "pdf":
        section_id = str(int(row["page_number"]))
        entity_id = f"lexical:pdf:{file_key}:page:{section_id}"
    elif spec.source_kind == "archive":
        section_id = file_key
        entity_id = f"lexical:archive:{file_key}:member"
    else:
        section_id = "fulltext"
        entity_id = f"lexical:{item_source_kind}:{file_key}:fulltext"

    provenance: dict[str, object] = {
        "backend": "sqlite_fts5",
        "fts_table": spec.fts_table,
        "normalized_query": query_plan.strict_query,
        "applied_query": applied_query,
        "query_policy_signature": LEXICAL_QUERY_POLICY_SIGNATURE,
        "query_strategy": query_strategy,
        "query_fallback_used": query_strategy != "strict_all_terms",
        "rank_position": rank_position,
        "raw_bm25": raw_bm25,
        "score_transform": "negative_raw_bm25",
        "ranking_source_kind": spec.source_kind,
        "source_kind": item_source_kind,
        "state_path": str(state_path.resolve(strict=False)),
    }
    source_revision: dict[str, object] = {
        "size": int(row["source_size"]),
        "mtime_ns": int(row["source_mtime_ns"]),
        "birthtime_ns": int(row["source_birthtime_ns"]),
        "processing_signature": str(row["source_processing_signature"]),
        "last_seen_run_id": int(row["source_last_seen_run_id"]),
    }
    if spec.source_kind == "pdf":
        source_revision["is_partial"] = bool(row["source_is_partial"])
    section_provenance: dict[str, object] = {}
    if spec.source_kind == "archive":
        section_provenance = {
            "inside_zip": True,
            "container_path": str(row["container_path"]),
            "member_chain": str(row["member_chain"]),
            "member_path": str(row["member_path"]),
            "archive_depth": int(row["archive_depth"]),
            "content_kind": str(row["content_kind"]),
        }
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=int(row["fts_rowid"]),
            entity_id=entity_id,
            item_id=f"item:{item_source_kind}:{file_key}",
            indexed_model_signature=LEXICAL_MODEL_SIGNATURE,
            vector_space=f"lexical:fts5:{spec.source_kind}:v1",
            modality=EmbeddingModality.TEXT,
            score=-raw_bm25,
            generation_id=0,
            provenance=provenance,
        ),
        path=str(row["path"]),
        source_kind=item_source_kind,
        source_identity=file_key,
        source_status=str(row["source_status"]),
        source_revision=source_revision,
        section_provenance=section_provenance,
        section_kind=spec.section_kind,
        section_id=section_id,
        start_char=None,
        end_char=None,
        snippet=_bounded_snippet(row["snippet"]),
    )


def _unavailable_ranking(
    source_kind: str,
    state_path: Path | None,
    normalized_query: str,
    availability: LexicalAvailability,
    reason: str,
) -> LexicalRanking:
    return LexicalRanking(
        source_kind=source_kind,
        state_path=state_path,
        availability=availability,
        normalized_query=normalized_query,
        hits=(),
        unavailable_reason=reason,
    )


def _search_compiled_source(
    source_kind: str,
    state_path: Path | None,
    query_plan: _NaturalFTSQueryPlan,
    limit: int,
    cancellation: SQLiteCancellationBridge,
) -> LexicalRanking:
    cancellation.checkpoint()
    try:
        spec = _SPECS[source_kind]
    except KeyError as exc:
        supported = ", ".join(_SOURCE_ORDER)
        raise ValueError(f"unsupported lexical source {source_kind!r}; use {supported}") from exc

    if state_path is None:
        return _unavailable_ranking(
            source_kind,
            None,
            query_plan.strict_query,
            LexicalAvailability.NOT_CONFIGURED,
            "state_database_not_configured",
        )
    path = Path(state_path)
    try:
        state = path.stat()
    except FileNotFoundError:
        return _unavailable_ranking(
            source_kind,
            path,
            query_plan.strict_query,
            LexicalAvailability.DATABASE_MISSING,
            "state_database_missing",
        )
    if not stat.S_ISREG(state.st_mode):
        raise ValueError(f"lexical state path is not a regular file: {path}")

    connection = sqlite3.connect(
        readonly_sqlite_uri(path),
        uri=True,
        timeout=60,
    )
    try:
        connection.row_factory = sqlite3.Row
        with sqlite_cancellation_scope(connection, cancellation):
            connection.execute("PRAGMA busy_timeout=60000")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("lexical source reader could not enable foreign keys")
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("lexical source reader is not query-only")
            applied_query = query_plan.strict_query
            query_strategy = "strict_all_terms"
            rows = connection.execute(spec.sql, (applied_query, limit)).fetchall()
            for fallback_strategy, fallback_query in query_plan.fallbacks:
                if rows:
                    break
                cancellation.checkpoint()
                rows = connection.execute(spec.sql, (fallback_query, limit)).fetchall()
                if rows:
                    applied_query = fallback_query
                    query_strategy = fallback_strategy
    finally:
        connection.close()
    hits: list[ResolvedSearchHit] = []
    for rank_position, row in enumerate(rows, start=1):
        if rank_position % _CANCELLATION_BATCH_ROWS == 0:
            cancellation.checkpoint()
        hits.append(
            _resolved_hit(
                spec,
                path,
                query_plan,
                applied_query,
                query_strategy,
                row,
                rank_position,
            )
        )
    cancellation.checkpoint()
    return LexicalRanking(
        source_kind=source_kind,
        state_path=path,
        availability=LexicalAvailability.AVAILABLE,
        normalized_query=query_plan.strict_query,
        hits=tuple(hits),
    )


# endregion [03]


# region [04] Public retrieval API


def _duration_ns(clock_ns: Callable[[], int], started_ns: int) -> int:
    finished_ns = clock_ns()
    if (
        isinstance(finished_ns, bool)
        or not isinstance(finished_ns, int)
        or finished_ns < started_ns
    ):
        raise RuntimeError("lexical monotonic clock moved backwards or was invalid")
    return finished_ns - started_ns


def search_lexical_source(
    source_kind: str,
    state_path: Path | None,
    query: str,
    *,
    limit: int = 20,
    cancellation_check: CancellationCheck | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> LexicalRanking:
    """Search one FTS source without creating or modifying its database."""

    _validate_limit(limit)
    query_plan = _compile_natural_fts_query_plan(query)
    cancellation = SQLiteCancellationBridge(cancellation_check)
    clock = clock_ns or time.perf_counter_ns
    started_ns = clock()
    ranking = _search_compiled_source(
        source_kind,
        state_path,
        query_plan,
        limit,
        cancellation,
    )
    return replace(ranking, elapsed_ns=_duration_ns(clock, started_ns))


def search_lexical_sources(
    paths: LexicalStatePaths,
    query: str,
    *,
    limit: int = 20,
    cancellation_check: CancellationCheck | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> tuple[LexicalRanking, ...]:
    """Return independent, availability-aware rankings in stable order."""

    _validate_limit(limit)
    query_plan = _compile_natural_fts_query_plan(query)
    cancellation = SQLiteCancellationBridge(cancellation_check)
    clock = clock_ns or time.perf_counter_ns
    rankings: list[LexicalRanking] = []
    for source_kind, state_path in paths.ordered():
        started_ns = clock()
        try:
            ranking = _search_compiled_source(
                source_kind,
                state_path,
                query_plan,
                limit,
                cancellation,
            )
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            if cancellation.captured_exception is exc:
                raise
            cancellation.reraise_if_captured(exc)
            ranking = _unavailable_ranking(
                source_kind,
                state_path,
                query_plan.strict_query,
                LexicalAvailability.READ_FAILED,
                f"state_database_read_failed:{type(exc).__name__}",
            )
        rankings.append(replace(ranking, elapsed_ns=_duration_ns(clock, started_ns)))
    return tuple(rankings)


# endregion [04]
