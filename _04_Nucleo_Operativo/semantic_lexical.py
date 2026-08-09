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

LEXICAL_MODEL_SIGNATURE = "sqlite-fts5-unicode61-rd2-v1"
_SOURCE_ORDER = ("pdf", "docx", "office", "audio", "archive")


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

    def ordered(self) -> tuple[tuple[str, Path | None], ...]:
        base = (
            ("pdf", self.pdf),
            ("docx", self.docx),
            ("office", self.office),
            ("audio", self.audio),
        )
        if self.archive is None:
            return base
        return (*base, ("archive", self.archive))


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


def compile_natural_fts_query(query: str) -> str:
    """Convert punctuation-rich natural text into a quoted FTS5 AND query.

    Only Unicode letter and number runs become terms.  Quoting every term keeps
    words such as ``OR`` or ``NEAR`` literal and prevents user punctuation from
    entering the FTS5 query grammar.
    """

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
    return " AND ".join(f'"{term}"' for term in unique_terms)


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
    normalized_query: str,
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
        "normalized_query": normalized_query,
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
    normalized_query: str,
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
            normalized_query,
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
            normalized_query,
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
            rows = connection.execute(spec.sql, (normalized_query, limit)).fetchall()
    finally:
        connection.close()
    hits: list[ResolvedSearchHit] = []
    for rank_position, row in enumerate(rows, start=1):
        if rank_position % _CANCELLATION_BATCH_ROWS == 0:
            cancellation.checkpoint()
        hits.append(_resolved_hit(spec, path, normalized_query, row, rank_position))
    cancellation.checkpoint()
    return LexicalRanking(
        source_kind=source_kind,
        state_path=path,
        availability=LexicalAvailability.AVAILABLE,
        normalized_query=normalized_query,
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
    normalized_query = compile_natural_fts_query(query)
    cancellation = SQLiteCancellationBridge(cancellation_check)
    clock = clock_ns or time.perf_counter_ns
    started_ns = clock()
    ranking = _search_compiled_source(
        source_kind,
        state_path,
        normalized_query,
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
    normalized_query = compile_natural_fts_query(query)
    cancellation = SQLiteCancellationBridge(cancellation_check)
    clock = clock_ns or time.perf_counter_ns
    rankings: list[LexicalRanking] = []
    for source_kind, state_path in paths.ordered():
        started_ns = clock()
        try:
            ranking = _search_compiled_source(
                source_kind,
                state_path,
                normalized_query,
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
                normalized_query,
                LexicalAvailability.READ_FAILED,
                f"state_database_read_failed:{type(exc).__name__}",
            )
        rankings.append(replace(ranking, elapsed_ns=_duration_ns(clock, started_ns)))
    return tuple(rankings)


# endregion [04]
