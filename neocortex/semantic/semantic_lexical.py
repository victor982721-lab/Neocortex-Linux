"""Bounded lexical retrieval over the route-owned SQLite FTS indexes.

Lexical scores remain in one ranking per source.  They are intentionally not
normalized or merged here because SQLite BM25 values from different corpora do
not share a calibrated scale; callers can combine ranks with RRF instead.
"""

from __future__ import annotations
import math
import re
import sqlite3
import unicodedata
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import combinations
from pathlib import Path

from .semantic_models import EmbeddingModality, ResolvedSearchHit, SearchHit
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)

# region [01] Public contracts and limits

MAX_LEXICAL_RESULTS = 1_000
MAX_QUERY_CHARS = 4_096
MAX_QUERY_TERMS = 64
MAX_QUERY_TERM_CHARS = 128
MAX_SNIPPET_CHARS = 1_024
MAX_CJK_SUBSTRING_SCAN_ROWS = 50_000
MAX_CJK_SUBSTRING_TERMS = 8
_CANCELLATION_BATCH_ROWS = 128

LEXICAL_MODEL_SIGNATURE = "sqlite-fts5-unicode61-rd2-cjk-substring-v3"
LEXICAL_QUERY_POLICY_SIGNATURE = "sqlite-fts5-natural-strict-soft-cjk-concepts-v6"
_SOURCE_ORDER = ("pdf", "docx", "office", "audio", "video", "archive", "text")


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
    video: Path | None = None
    archive: Path | None = None
    text: Path | None = None

    def ordered(self) -> tuple[tuple[str, Path | None], ...]:
        base = (
            ("pdf", self.pdf),
            ("docx", self.docx),
            ("office", self.office),
            ("audio", self.audio),
            ("video", self.video),
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
    cjk_sql: str
    cjk_content_expression: str
    section_kind: str


# endregion [01]


# region [02] Safe natural-query compilation

_NATURAL_TERM = re.compile(r"[^\W_]+", flags=re.UNICODE)
_HAN_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_NON_HAN_NATURAL_TERM = re.compile(
    r"[0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ž]+",
    flags=re.UNICODE,
)

# FTS5's unicode61 tokenizer treats an uninterrupted Han run as one token.
# Consequently, a safe MATCH query cannot find a shorter technical term inside
# a longer Chinese token.  These phrases are removed only for the bounded,
# read-only substring fallback below; they never alter the primary FTS query.
# Keep the list deliberately small and grammar-only so domain concepts are not
# silently weakened.
_CJK_QUERY_SCAFFOLDING = (
    "请告诉我",
    "請告訴我",
    "请查找",
    "請查找",
    "请显示",
    "請顯示",
    "有什么",
    "有什麼",
    "有哪些",
    "哪一些",
    "相关的",
    "相關的",
    "关于",
    "關於",
    "是否有",
    "是否存在",
    "在哪里",
    "在哪裡",
    "哪里",
    "哪裡",
    "证据",
    "證據",
    "信息",
    "資訊",
    "资料",
    "資料",
    "文档",
    "文件",
)
_CJK_GRAMMAR_PARTICLES = frozenset({"的", "了", "吗", "嗎", "呢"})

# These are grammar words, not domain concepts.  Ordinary queries remove them
# only after the strict all-term query returns no rows.  Explicit question
# requests remove them up front so generic prompt language cannot outrank the
# requested subject.  FTS operator escaping retains its historical behavior.
_NATURAL_STOPWORDS = frozenset(
    {
        # Spanish
        "a",
        "al",
        "como",
        "con",
        "cual",
        "cuales",
        "cuál",
        "cuáles",
        "de",
        "del",
        "donde",
        "dónde",
        "el",
        "en",
        "encuentra",
        "encontrar",
        "evidencia",
        "existe",
        "existen",
        "la",
        "las",
        "hay",
        "informacion",
        "información",
        "lo",
        "los",
        "muestra",
        "mostrar",
        "o",
        "para",
        "por",
        "que",
        "qué",
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
        "about",
        "by",
        "evidence",
        "find",
        "for",
        "from",
        "in",
        "information",
        "is",
        "of",
        "on",
        "or",
        "show",
        "the",
        "there",
        "to",
        "what",
        "where",
        "which",
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
        "es",
        "evidenz",
        "für",
        "finde",
        "finden",
        "gibt",
        "im",
        "mit",
        "nachweis",
        "oder",
        "uber",
        "und",
        "über",
        "von",
        "was",
        "welche",
        "welcher",
        "welches",
        "wo",
        "zeige",
        "zeigen",
        "zu",
    }
)
_MAX_SOFT_FALLBACK_TERMS = 5

# Dropping one of these tokens can reverse the requested condition.  A bag of
# words cannot safely recover its scope, so only the all-content-term fallback
# is allowed for such queries; semantic retrieval remains an independent path.
_PROTECTED_NEGATIONS = frozenset(
    {
        "no", "sin", "nunca", "ningun", "ninguno", "ninguna", "ningunos", "ningunas",
        "not", "without", "never", "neither", "nor",
        "nicht", "ohne", "kein", "keine", "keinen", "keinem", "keiner", "keines",
    }
)
QUERY_SUPPORT_POLICY = "retrieval-query-support-v1"


def _fold_retrieval_term(term: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", term.casefold())
        if not unicodedata.combining(character)
    )


def _query_support_terms(query: str) -> tuple[str, ...]:
    # Semantic accepts longer queries than the FTS adapter.  Diagnostics are
    # bounded independently and must not narrow that existing query contract.
    terms = dict.fromkeys(
        _fold_retrieval_term(term) for term in _NATURAL_TERM.findall(query[:MAX_QUERY_CHARS])
        if term.casefold() not in _NATURAL_STOPWORDS
    )
    return tuple(terms)[:MAX_QUERY_TERMS]


def _query_term_matches(
    text: str, terms: tuple[str, ...],
) -> tuple[tuple[int, int, int, str], ...]:
    wanted = set(terms)
    return tuple(
        (index, match.start(), match.end(), folded)
        for index, match in enumerate(_NATURAL_TERM.finditer(text))
        if (folded := _fold_retrieval_term(match.group())) in wanted
    )


def _minimum_term_span(matches: tuple[tuple[int, int, int, str], ...]) -> int | None:
    if not matches:
        return None
    target_count = len({match[3] for match in matches})
    counts: dict[str, int] = {}
    left = 0
    best: int | None = None
    for right, match in enumerate(matches):
        counts[match[3]] = counts.get(match[3], 0) + 1
        while len(counts) == target_count:
            span = matches[right][0] - matches[left][0] + 1
            best = span if best is None else min(best, span)
            term = matches[left][3]
            counts[term] -= 1
            if counts[term] == 0:
                del counts[term]
            left += 1
    return best


def query_term_support(query: str, text: str, *, basis: str) -> dict[str, object]:
    """Explain literal coverage, never infer entailment or relevance probability."""
    from .semantic_query_evidence import query_role_counterevidence, requested_evidence_checks

    terms = _query_support_terms(query)
    matches = _query_term_matches(text, terms)
    observed = {match[3] for match in matches}
    missing = [term for term in terms if term not in observed]
    text_terms = [_fold_retrieval_term(match.group()) for match in _NATURAL_TERM.finditer(text)]
    phrase_terms = tuple(
        _fold_retrieval_term(term) for term in _NATURAL_TERM.findall(query[:MAX_QUERY_CHARS])
    )[:MAX_QUERY_TERMS]
    phrase_match = bool(phrase_terms) and any(
        tuple(text_terms[index:index + len(phrase_terms)]) == phrase_terms
        for index in range(max(0, len(text_terms) - len(phrase_terms) + 1))
    )
    negations = [term for term in terms if term in _PROTECTED_NEGATIONS]
    return {
        "policy_signature": QUERY_SUPPORT_POLICY,
        "basis": basis,
        "interpretation": "literal_overlap_not_entailment",
        "support": (
            "no_content_terms" if not terms else "no_terms" if not observed
            else "partial_terms" if missing else "full_terms"
        ),
        "matched_terms": [term for term in terms if term in observed],
        "missing_terms": missing,
        "negation_terms": negations,
        "missing_negation_terms": [term for term in negations if term not in observed],
        "term_coverage": len(observed) / len(terms) if terms else 0.0,
        "phrase_match": phrase_match,
        "minimum_span_terms": _minimum_term_span(matches),
        "role_counterevidence": query_role_counterevidence(query, text),
        "requested_witness_checks": requested_evidence_checks(query, text),
    }


def query_centered_snippet(
    text: str, query: str | None, *, max_chars: int,
) -> tuple[str | None, dict[str, object]]:
    """Select a verbatim window of the scored chunk, with explicit local offsets."""
    if max_chars < 0:
        raise ValueError("snippet max_chars cannot be negative")
    start = 0
    matches = _query_term_matches(text, _query_support_terms(query or ""))
    if max_chars and len(text) > max_chars and matches:
        # Prefer the window containing the most distinct query terms; then the
        # shortest covering span and earliest occurrence.  Only the displayed
        # witness changes, never the vector score or document ranking.
        best: tuple[int, int, int] | None = None
        best_bounds = (matches[0][1], matches[0][2])
        counts: dict[str, int] = {}
        left = 0
        for right, match in enumerate(matches):
            counts[match[3]] = counts.get(match[3], 0) + 1
            while left <= right and (
                match[2] - matches[left][1] > max_chars or counts[matches[left][3]] > 1
            ):
                term = matches[left][3]
                counts[term] -= 1
                if counts[term] == 0:
                    del counts[term]
                left += 1
            if left <= right:
                key = (-len(counts), match[2] - matches[left][1], matches[left][1])
                if best is None or key < best:
                    best, best_bounds = key, (matches[left][1], match[2])
        span = best_bounds[1] - best_bounds[0]
        start = max(0, best_bounds[0] - max(0, max_chars - span) // 2)
        start = min(start, max(0, len(text) - max_chars))
    end = min(len(text), start + max_chars)
    return (text[start:end] if max_chars else None), {
        "policy_signature": "query-centered-scored-chunk-v1",
        "basis": "normalized_scored_chunk",
        "start_in_chunk": start,
        "end_in_chunk": end,
        "chunk_chars": len(text),
        "truncated": start > 0 or end < len(text),
        "query_terms_found": bool(matches),
    }


@dataclass(frozen=True, slots=True)
class _NaturalFTSQueryPlan:
    original_query: str
    normalized_query: str
    primary_query: str
    primary_strategy: str
    fallbacks: tuple[tuple[str, str], ...]
    cjk_substring_terms: tuple[str, ...]
    cjk_query_rewritten: bool


_QUESTION_OPENERS = frozenset(
    {
        # Spanish
        "cual",
        "cuales",
        "cuál",
        "cuáles",
        "donde",
        "dónde",
        "encuentra",
        "muestra",
        "que",
        "qué",
        # English
        "find",
        "show",
        "what",
        "where",
        "which",
        # German
        "finde",
        "was",
        "welche",
        "welcher",
        "welches",
        "wo",
        "zeige",
    }
)


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

    if not 3 <= len(terms) <= _MAX_SOFT_FALLBACK_TERMS or any(
        _fold_retrieval_term(term) in _PROTECTED_NEGATIONS for term in terms
    ):
        return None
    pairs = (
        f"({_quoted_fts_term(left)} AND {_quoted_fts_term(right)})"
        for left, right in combinations(terms, 2)
    )
    return " OR ".join(pairs)


def _condition_concept_query(query: str) -> str | None:
    """Bounded bilingual recall for an explicit cooling/pressure-absence query.

    Both concepts remain mandatory.  This is a retrieval expansion, not proof
    of arrival, a pressure-loss cause, or equivalence of physical equipment.
    Unlike the any-two fallback it cannot drop the requested negative condition.
    """
    from .semantic_query_variants import cooling_pressure_concepts

    if not cooling_pressure_concepts(query):
        return None
    subjects = ("radiador", "radiadores", "enfriador", "enfriadores", "radiator", "radiators", "cooler", "coolers")
    conditions = ("sin presión", "ausencia de presión", "despresurizado", "despresurizados", "despresurizada", "despresurizadas", "without pressure", "unpressurized", "depressurized", "unpressurised", "depressurised")
    return "(" + " OR ".join(_quoted_fts_term(term) for term in subjects) + ") AND (" + " OR ".join(_quoted_fts_term(term) for term in conditions) + ")"


def _cjk_substring_terms(query: str) -> tuple[tuple[str, ...], bool]:
    """Return conservative exact substrings for a Han-aware fallback.

    The fallback requires every returned term.  A one-character Han query is
    intentionally rejected because scanning for it would be both broad and
    noisy.  Latin/digit terms in a mixed query remain mandatory rather than
    being discarded merely because Han text is present.
    """

    han_runs = _HAN_RUN.findall(query)
    if not han_runs:
        return (), False

    rewritten = False
    candidates: list[str] = []
    for run in han_runs:
        cleaned = run
        for phrase in _CJK_QUERY_SCAFFOLDING:
            if phrase in cleaned:
                cleaned = cleaned.replace(phrase, "")
                rewritten = True
        without_particles = "".join(
            character for character in cleaned if character not in _CJK_GRAMMAR_PARTICLES
        )
        if without_particles != cleaned:
            rewritten = True
        if len(without_particles) >= 2:
            candidates.append(without_particles)

    for term in _NON_HAN_NATURAL_TERM.findall(query):
        if term.casefold() not in _NATURAL_STOPWORDS:
            candidates.append(term)

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    if not unique or len(unique) > MAX_CJK_SUBSTRING_TERMS:
        return (), rewritten
    return tuple(unique), rewritten


def _compile_natural_fts_query_plan(query: str) -> _NaturalFTSQueryPlan:
    terms = _natural_query_terms(query)
    normalized = _all_terms_query(terms)
    content_terms = tuple(term for term in terms if term.casefold() not in _NATURAL_STOPWORDS)
    is_question_request = terms[0].casefold() in _QUESTION_OPENERS
    if is_question_request and content_terms:
        primary_query = _all_terms_query(content_terms)
        primary_strategy = "question_content_terms_all"
    else:
        primary_query = normalized
        primary_strategy = "strict_all_terms"

    fallbacks: list[tuple[str, str]] = []
    if primary_strategy == "strict_all_terms" and content_terms and content_terms != terms:
        fallbacks.append(("content_terms_all", _all_terms_query(content_terms)))
    concept_query = _condition_concept_query(query)
    if concept_query is not None:
        fallbacks.append(("cooling_pressure_absence_concepts", concept_query))
    cjk_terms, cjk_rewritten = _cjk_substring_terms(query)
    # Never let the Latin any-two recovery path discard the Han subject of a
    # mixed query.  If exact FTS matching fails, the CJK fallback below keeps
    # every Han, Latin and numeric content term mandatory.
    if not cjk_terms:
        soft = _soft_content_query(content_terms)
        if soft is not None and soft not in {
            primary_query,
            *(fallback_query for _, fallback_query in fallbacks),
        }:
            fallbacks.append(("content_terms_any_two", soft))
    return _NaturalFTSQueryPlan(
        original_query=query,
        normalized_query=normalized,
        primary_query=primary_query,
        primary_strategy=primary_strategy,
        fallbacks=tuple(fallbacks),
        cjk_substring_terms=cjk_terms,
        cjk_query_rewritten=cjk_rewritten,
    )


def compile_natural_fts_query(query: str) -> str:
    """Convert punctuation-rich natural text into a quoted FTS5 AND query.

    Only Unicode letter and number runs become terms.  Quoting every term keeps
    words such as ``OR`` or ``NEAR`` literal and prevents user punctuation from
    entering the FTS5 query grammar.
    """

    return _compile_natural_fts_query_plan(query).normalized_query


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
        cjk_content_expression="f.text",
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
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,f.page_number,
        substr(f.text,max(1,instr(f.text,?)-80),240) AS snippet,
        CAST(length(f.text) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.is_partial AS source_is_partial
        FROM page_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status IN ('done','partial') AND {conditions}
        ORDER BY length(f.text),f.path COLLATE NOCASE,f.page_number LIMIT ?""",
    ),
    "docx": _SourceSpec(
        source_kind="docx",
        fts_table="document_fts",
        section_kind="document",
        cjk_content_expression="f.body",
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
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        substr(f.body,max(1,instr(f.body,?)-80),240) AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status IN ('complete','partial') AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE LIMIT ?""",
    ),
    "office": _SourceSpec(
        source_kind="office",
        fts_table="document_fts",
        section_kind="document",
        cjk_content_expression="f.body",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,f.format,
        snippet(document_fts,5,'[',']',' ... ',24) AS snippet,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,f.format,
        substr(f.body,max(1,instr(f.body,?)-80),240) AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status='complete' AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE LIMIT ?""",
    ),
    "audio": _SourceSpec(
        source_kind="audio",
        fts_table="transcript_fts",
        section_kind="transcript",
        cjk_content_expression="f.body",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        snippet(transcript_fts,3,'[',']',' ... ',24) AS snippet,
        bm25(transcript_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM transcript_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE transcript_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        substr(f.body,max(1,instr(f.body,?)-80),240) AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM transcript_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status='complete' AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE LIMIT ?""",
    ),
    "video": _SourceSpec(
        source_kind="video",
        fts_table="frame_fts",
        section_kind="video_frame_ocr",
        cjk_content_expression="f.body",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        fr.frame_index,fr.timestamp_ms,fr.content_xxh3_128,
        fr.ocr_mean_confidence,fr.ocr_provenance,
        snippet(frame_fts,4,'[',']',' ... ',24) AS snippet,
        bm25(frame_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM frame_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        JOIN frames AS fr ON fr.file_key=f.file_key
        AND fr.timestamp_ms=CAST(f.timestamp_ms AS INTEGER)
        WHERE frame_fts MATCH ? AND d.status IN ('complete','partial')
        AND fr.ocr_available=1
        ORDER BY raw_bm25,f.path COLLATE NOCASE,fr.timestamp_ms,fr.frame_index LIMIT ?""",
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        fr.frame_index,fr.timestamp_ms,fr.content_xxh3_128,
        fr.ocr_mean_confidence,fr.ocr_provenance,
        substr(f.body,max(1,instr(f.body,?)-80),240) AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status
        FROM frame_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        JOIN frames AS fr ON fr.file_key=f.file_key
        AND fr.timestamp_ms=CAST(f.timestamp_ms AS INTEGER)
        WHERE d.status IN ('complete','partial') AND fr.ocr_available=1
        AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE,
        fr.timestamp_ms,fr.frame_index LIMIT ?""",
    ),
    "archive": _SourceSpec(
        source_kind="archive",
        fts_table="document_fts",
        section_kind="archive_member",
        cjk_content_expression="f.body",
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
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        CASE WHEN d.text_chars>0
        THEN substr(f.body,max(1,instr(f.body,?)-80),240)
        ELSE d.member_chain END AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.container_path,d.member_chain,d.member_path,d.archive_depth,d.content_kind
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status IN ('indexed','metadata_only','archive') AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE LIMIT ?""",
    ),
    "text": _SourceSpec(
        source_kind="text",
        fts_table="document_fts",
        section_kind="document",
        cjk_content_expression="f.body",
        sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        snippet(document_fts,5,'[',']',' ... ',24) AS snippet,
        bm25(document_fts) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.revision_id AS source_revision_id
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE document_fts MATCH ? AND d.status='complete'
        ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?""",
        cjk_sql="""SELECT f.rowid AS fts_rowid,f.file_key,f.path,
        substr(f.body,max(1,instr(f.body,?)-80),240) AS snippet,
        CAST(length(f.body) AS REAL) AS raw_bm25,d.size AS source_size,
        d.mtime_ns AS source_mtime_ns,d.birthtime_ns AS source_birthtime_ns,
        d.processing_signature AS source_processing_signature,
        d.last_seen_run_id AS source_last_seen_run_id,d.status AS source_status,
        d.revision_id AS source_revision_id
        FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
        WHERE d.status='complete' AND {conditions}
        ORDER BY length(f.body),f.path COLLATE NOCASE LIMIT ?""",
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
    *,
    retrieval_backend: str = "sqlite_fts5",
    cjk_scanned_rows: int | None = None,
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
    elif spec.source_kind == "video":
        section_id = str(int(row["frame_index"]))
        entity_id = f"lexical:video:{file_key}:frame:{section_id}"
    elif spec.source_kind == "archive":
        section_id = file_key
        entity_id = f"lexical:archive:{file_key}:member"
    else:
        section_id = "fulltext"
        entity_id = f"lexical:{item_source_kind}:{file_key}:fulltext"

    is_cjk_substring = retrieval_backend == "sqlite_bounded_cjk_substring"
    snippet = _bounded_snippet(row["snippet"])
    support = query_term_support(query_plan.original_query, snippet or "", basis="fts_snippet")
    support.update(
        {
            "query_strategy": query_strategy,
            "query_fallback_used": is_cjk_substring or query_strategy not in {
                "strict_all_terms", "question_content_terms_all",
            },
        }
    )
    if query_strategy == "cooling_pressure_absence_concepts":
        support["query_expansion"] = {
            "policy_signature": "cooling-pressure-absence-aliases-v1",
            "required_concepts": ["cooling_component", "pressure_absence"],
            "interpretation": "retrieval_aliases_not_arrival_or_causal_evidence",
            "applied_query": applied_query,
        }
    provenance: dict[str, object] = {
        "backend": retrieval_backend,
        "fts_table": spec.fts_table,
        "normalized_query": query_plan.normalized_query,
        "applied_query": applied_query,
        "query_policy_signature": LEXICAL_QUERY_POLICY_SIGNATURE,
        "query_strategy": query_strategy,
        "query_fallback_used": is_cjk_substring
        or query_strategy
        not in {
            "strict_all_terms",
            "question_content_terms_all",
        },
        "query_rewrite_used": query_strategy == "question_content_terms_all"
        or (is_cjk_substring and query_plan.cjk_query_rewritten),
        "rank_position": rank_position,
        "ranking_source_kind": spec.source_kind,
        "source_kind": item_source_kind,
        "state_path": str(state_path.resolve(strict=False)),
        "query_support": support,
    }
    if is_cjk_substring:
        provenance.update(
            {
                "candidate_text_chars": int(raw_bm25),
                "score_transform": "negative_candidate_text_chars",
                "substring_terms": query_plan.cjk_substring_terms,
                "scanned_rows": cjk_scanned_rows,
                "scan_row_limit": MAX_CJK_SUBSTRING_SCAN_ROWS,
            }
        )
    else:
        provenance.update(
            {
                "raw_bm25": raw_bm25,
                "score_transform": "negative_raw_bm25",
            }
        )
    source_revision: dict[str, object] = {
        "size": int(row["source_size"]),
        "mtime_ns": int(row["source_mtime_ns"]),
        "birthtime_ns": int(row["source_birthtime_ns"]),
        "processing_signature": str(row["source_processing_signature"]),
        "last_seen_run_id": int(row["source_last_seen_run_id"]),
    }
    if "source_revision_id" in row.keys() and row["source_revision_id"] is not None:
        revision_id = str(row["source_revision_id"])
        if not revision_id.strip():
            raise sqlite3.DataError("source revision identity cannot be blank")
        source_revision["revision_id"] = revision_id
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
    elif spec.source_kind == "video":
        timestamp_ms = int(row["timestamp_ms"])
        section_provenance = {
            "adapter": "video-frame-ocr-v1",
            "start_ms": timestamp_ms,
            "end_ms": timestamp_ms + 1,
            "timestamp": _format_timestamp(timestamp_ms),
            "frame_index": int(row["frame_index"]),
            "content_xxh3_128": str(row["content_xxh3_128"]),
        }
        if row["ocr_mean_confidence"] is not None:
            section_provenance["ocr_mean_confidence"] = float(row["ocr_mean_confidence"])
        if row["ocr_provenance"] is not None:
            section_provenance["ocr_provenance"] = str(row["ocr_provenance"])
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=int(row["fts_rowid"]),
            entity_id=entity_id,
            item_id=f"item:{item_source_kind}:{file_key}",
            indexed_model_signature=LEXICAL_MODEL_SIGNATURE,
            vector_space=(
                f"lexical:substring-cjk:{spec.source_kind}:v1"
                if is_cjk_substring
                else f"lexical:fts5:{spec.source_kind}:v1"
            ),
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
        snippet=snippet,
    )


def _format_timestamp(timestamp_ms: int) -> str:
    hours, remainder = divmod(max(0, timestamp_ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


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


def _search_cjk_substrings(
    connection: sqlite3.Connection,
    spec: _SourceSpec,
    terms: tuple[str, ...],
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    """Run one exact, bounded Han substring scan over FTS-owned text."""

    scanned_rows = int(connection.execute(f"SELECT COUNT(*) FROM {spec.fts_table}").fetchone()[0])
    if scanned_rows > MAX_CJK_SUBSTRING_SCAN_ROWS:
        return [], scanned_rows
    content = spec.cjk_content_expression
    conditions = " AND ".join(f"instr(lower({content}),lower(?))>0" for _term in terms)
    sql = spec.cjk_sql.format(conditions=conditions)
    parameters: tuple[object, ...] = (terms[0], *terms, limit)
    return connection.execute(sql, parameters).fetchall(), scanned_rows


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
            query_plan.normalized_query,
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
            query_plan.normalized_query,
            LexicalAvailability.DATABASE_MISSING,
            "state_database_missing",
        )
    if not stat.S_ISREG(state.st_mode):
        raise ValueError(f"lexical state path is not a regular file: {path}")

    retrieval_backend = "sqlite_fts5"
    cjk_scanned_rows: int | None = None
    from neocortex.persistence.sqlite_immutable import (
        preferred_sqlite_read_mode,
    )
    from .semantic_schema import semantic_read_context

    rows: list[sqlite3.Row]
    with (
        semantic_read_context() as read_context,
        read_context.acquire(path, mode=preferred_sqlite_read_mode(path).value) as connection,
    ):
        with sqlite_cancellation_scope(connection, cancellation):
            connection.execute("PRAGMA busy_timeout=60000")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("lexical source reader could not enable foreign keys")
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("lexical source reader is not query-only")
            applied_query = query_plan.primary_query
            query_strategy = query_plan.primary_strategy
            rows = connection.execute(spec.sql, (applied_query, limit)).fetchall()
            for fallback_strategy, fallback_query in query_plan.fallbacks:
                if rows:
                    break
                cancellation.checkpoint()
                rows = connection.execute(spec.sql, (fallback_query, limit)).fetchall()
                if rows:
                    applied_query = fallback_query
                    query_strategy = fallback_strategy
            if not rows and query_plan.cjk_substring_terms:
                cancellation.checkpoint()
                rows, cjk_scanned_rows = _search_cjk_substrings(
                    connection,
                    spec,
                    query_plan.cjk_substring_terms,
                    limit,
                )
                if cjk_scanned_rows > MAX_CJK_SUBSTRING_SCAN_ROWS:
                    return _unavailable_ranking(
                        source_kind,
                        path,
                        query_plan.normalized_query,
                        LexicalAvailability.READ_FAILED,
                        "cjk_substring_scan_limit_exceeded",
                    )
                if rows:
                    applied_query = _all_terms_query(query_plan.cjk_substring_terms)
                    query_strategy = "cjk_substring_all_terms"
                    retrieval_backend = "sqlite_bounded_cjk_substring"
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
                retrieval_backend=retrieval_backend,
                cjk_scanned_rows=cjk_scanned_rows,
            )
        )
    cancellation.checkpoint()
    return LexicalRanking(
        source_kind=source_kind,
        state_path=path,
        availability=LexicalAvailability.AVAILABLE,
        normalized_query=query_plan.normalized_query,
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
                query_plan.normalized_query,
                LexicalAvailability.READ_FAILED,
                f"state_database_read_failed:{type(exc).__name__}",
            )
        rankings.append(replace(ranking, elapsed_ns=_duration_ns(clock, started_ns)))
    return tuple(rankings)


# endregion [04]
