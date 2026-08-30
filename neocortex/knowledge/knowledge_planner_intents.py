"""Query normalization and deterministic intent inference for knowledge plans."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_planner_intents.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .knowledge_planner_exact import (
    RELATIONAL_WORDS,
    STRUCTURAL_WORDS,
    SYMBOL_PATTERN,
    TEMPORAL_WORDS,
    TEMPORAL_YEAR_WORDS,
    has_temporal_year,
    token_words,
)
from .knowledge_planner_steps import validated_date
# endregion [01]

# region [02] Implementación


_ExactTerms = tuple[tuple[str, ...], bool, bool, bool, str, str]


class QueryLike(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def include_history(self) -> bool: ...

    @property
    def source_kinds(self) -> tuple[str, ...]: ...

    @property
    def formats(self) -> tuple[str, ...]: ...

    @property
    def project(self) -> str | None: ...

    @property
    def date_from(self) -> str | None: ...

    @property
    def date_to(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class QueryLimits:
    query_chars: int
    results: int
    vectors: int
    filters: int
    filter_value_chars: int
    filter_total_chars: int
    project_chars: int


@dataclass(frozen=True, slots=True)
class NormalizedKnowledgeQuery:
    text: str
    source_kinds: tuple[str, ...]
    formats: tuple[str, ...]
    project: str | None
    date_from: str | None
    date_to: str | None


def _normalize_text(value: object, limits: QueryLimits) -> str:
    if not isinstance(value, str):
        raise ValueError("Knowledge query text must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Knowledge query cannot be blank")
    if len(normalized) > limits.query_chars:
        raise ValueError(
            f"Knowledge query cannot exceed {limits.query_chars} characters"
        )
    return normalized


def _validate_query_flags(
    retrieval_mode: object,
    include_history: object,
    retrieval_mode_type: type[object],
) -> None:
    if not isinstance(retrieval_mode, retrieval_mode_type):
        raise ValueError("retrieval_mode must be a RetrievalMode instance")
    if not isinstance(include_history, bool):
        raise ValueError("include_history must be a bool")


def _bounded_integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
    message: str,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(message)


def _validate_query_numbers(
    *,
    limit: object,
    max_per_resource: object,
    min_section_distance: object,
    max_vectors: object,
    limits: QueryLimits,
) -> None:
    _bounded_integer(
        limit,
        minimum=1,
        maximum=limits.results,
        message="Knowledge limit must be between 1 and 1000",
    )
    _bounded_integer(
        max_per_resource,
        minimum=1,
        maximum=100,
        message="max_per_resource must be between 1 and 100",
    )
    _bounded_integer(
        min_section_distance,
        minimum=0,
        maximum=1_000_000,
        message="min_section_distance must be between 0 and 1000000",
    )
    _bounded_integer(
        max_vectors,
        minimum=1,
        maximum=limits.vectors,
        message="max_vectors must be between 1 and 10000000",
    )


def _normalized_filter_value(value: object, limits: QueryLimits) -> str:
    if not isinstance(value, str):
        raise ValueError("query filters must contain only strings")
    normalized = value.strip()
    if not normalized:
        raise ValueError("query filters cannot contain blank values")
    if len(normalized) > limits.filter_value_chars:
        raise ValueError(
            f"query filter values cannot exceed {limits.filter_value_chars} characters"
        )
    return normalized


def _deduplicate_filter(
    values: object,
    *,
    casefold: bool,
    limits: QueryLimits,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError("query filters must be tuples of strings")
    if len(values) > limits.filters:
        raise ValueError(f"at most {limits.filters} values are allowed per filter")
    result: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for value in values:
        normalized = _normalized_filter_value(value, limits)
        total_chars += len(normalized)
        if total_chars > limits.filter_total_chars:
            raise ValueError(
                "query filter values cannot exceed "
                f"{limits.filter_total_chars} total characters"
            )
        key = normalized.casefold() if casefold else normalized
        if key in seen:
            continue
        seen.add(key)
        result.append(key if casefold else normalized)
    return tuple(result)


def _normalize_project(value: object, limits: QueryLimits) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("project must be a string when present")
    normalized = value.strip()
    if not normalized:
        raise ValueError("project cannot be blank when present")
    if len(normalized) > limits.project_chars:
        raise ValueError(f"project cannot exceed {limits.project_chars} characters")
    return normalized


def _normalized_dates(
    date_from: object,
    date_to: object,
) -> tuple[str | None, str | None]:
    normalized_from = validated_date("date_from", date_from)
    normalized_to = validated_date("date_to", date_to)
    if (
        normalized_from is not None
        and normalized_to is not None
        and normalized_from > normalized_to
    ):
        raise ValueError("date_from cannot be after date_to")
    return normalized_from, normalized_to


def normalize_knowledge_query(
    *,
    text: object,
    retrieval_mode: object,
    include_history: object,
    source_kinds: object,
    formats: object,
    project: object,
    date_from: object,
    date_to: object,
    limit: object,
    max_per_resource: object,
    min_section_distance: object,
    max_vectors: object,
    retrieval_mode_type: type[object],
    limits: QueryLimits,
) -> NormalizedKnowledgeQuery:
    normalized_text = _normalize_text(text, limits)
    _validate_query_flags(retrieval_mode, include_history, retrieval_mode_type)
    _validate_query_numbers(
        limit=limit,
        max_per_resource=max_per_resource,
        min_section_distance=min_section_distance,
        max_vectors=max_vectors,
        limits=limits,
    )
    normalized_sources = _deduplicate_filter(
        source_kinds,
        casefold=True,
        limits=limits,
    )
    normalized_formats = _deduplicate_filter(
        formats,
        casefold=True,
        limits=limits,
    )
    normalized_project = _normalize_project(project, limits)
    normalized_from, normalized_to = _normalized_dates(date_from, date_to)
    return NormalizedKnowledgeQuery(
        text=normalized_text,
        source_kinds=normalized_sources,
        formats=normalized_formats,
        project=normalized_project,
        date_from=normalized_from,
        date_to=normalized_to,
    )


def _has_explicit_code_context(
    query: QueryLike,
    words: frozenset[str],
    code_formats: frozenset[str],
) -> bool:
    format_keys = tuple(value.removeprefix(".") for value in query.formats)
    return (
        "code" in query.source_kinds
        or any(value in code_formats for value in format_keys)
        or bool(words.intersection(STRUCTURAL_WORDS))
    )


def _has_qualified_code_name(
    terms: tuple[str, ...],
    *,
    explicit_code_context: bool,
) -> bool:
    return explicit_code_context and any(
        "/" not in term
        and "\\" not in term
        and SYMBOL_PATTERN.fullmatch(term) is not None
        for term in terms
    )


def _has_temporal_intent(
    query: QueryLike,
    words: frozenset[str],
    temporal_text: str,
) -> bool:
    return (
        query.include_history
        or query.date_from is not None
        or query.date_to is not None
        or bool(words.intersection(TEMPORAL_WORDS))
        or (
            bool(words.intersection(TEMPORAL_YEAR_WORDS))
            and has_temporal_year(temporal_text)
        )
    )


def _ordered_intents(
    *,
    path_present: bool,
    name_present: bool,
    terms_present: bool,
    symbol_intent: bool,
    structural: bool,
    explicit_filters: bool,
    relational: bool,
    temporal: bool,
) -> tuple[str, ...]:
    intents: list[str] = []
    for present, name in (
        (path_present, "path"),
        (name_present, "name"),
        (terms_present, "identifier"),
        (symbol_intent, "symbol"),
    ):
        if present:
            intents.append(name)
    intents.extend(("lexical", "semantic"))
    for present, name in (
        (structural, "structural"),
        (explicit_filters, "filtered"),
        (relational, "relational"),
        (temporal, "temporal"),
    ):
        if present:
            intents.append(name)
    return tuple(intents)


def infer_query_plan_signals(
    query: QueryLike,
    *,
    exact_terms: Callable[[str], _ExactTerms],
    code_formats: frozenset[str],
    max_exact_terms: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    (
        terms,
        path_present,
        name_present,
        symbol_present,
        non_path_text,
        temporal_text,
    ) = exact_terms(query.text)
    if len(terms) > max_exact_terms:
        raise ValueError(
            f"Knowledge query cannot contain more than {max_exact_terms} exact terms"
        )
    words = token_words(non_path_text)
    explicit_code_context = _has_explicit_code_context(query, words, code_formats)
    symbol_intent = symbol_present or _has_qualified_code_name(
        terms,
        explicit_code_context=explicit_code_context,
    )
    structural = symbol_intent or explicit_code_context
    relational = bool(words.intersection(RELATIONAL_WORDS))
    temporal = _has_temporal_intent(query, words, temporal_text)
    explicit_filters = bool(
        query.source_kinds
        or query.formats
        or query.project is not None
        or query.date_from is not None
        or query.date_to is not None
    )
    intents = _ordered_intents(
        path_present=path_present,
        name_present=name_present,
        terms_present=bool(terms),
        symbol_intent=symbol_intent,
        structural=structural,
        explicit_filters=explicit_filters,
        relational=relational,
        temporal=temporal,
    )
    return terms, intents


__all__ = (
    "NormalizedKnowledgeQuery",
    "QueryLike",
    "QueryLimits",
    "infer_query_plan_signals",
    "normalize_knowledge_query",
)
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.knowledge_planner_intents")
