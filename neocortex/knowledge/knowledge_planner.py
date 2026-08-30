"""Deterministic, observable query planning for Knowledge retrieval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .knowledge_contracts import KNOWLEDGE_CONTRACT_SCHEMA_VERSION
from .knowledge_planner_exact import extract_exact_terms
from .knowledge_planner_intents import (
    QueryLimits,
    infer_query_plan_signals,
    normalize_knowledge_query,
)
from .knowledge_planner_steps import (
    CODE_FORMATS as _CODE_FORMATS,
    KNOWLEDGE_PLAN_V2_PREFIX as _KNOWLEDGE_PLAN_V2_PREFIX,
    KNOWLEDGE_PLAN_V3_PREFIX as _KNOWLEDGE_PLAN_V3_PREFIX,
    PlanLimits,
    canonical_retrieval_step_specs,
    canonical_retrieval_step_specs_v3,
    knowledge_plan_identity_payload,
    semantic_ranking_names,
    validate_knowledge_plan_base,
    validate_knowledge_plan_v2,
    validate_knowledge_plan_v3,
    validate_retrieval_step,
)
from _04_Nucleo_Operativo.semantic_models import canonical_json, fingerprint_text


# region [01] Public query and plan contracts


MAX_KNOWLEDGE_QUERY_CHARS = 4_096
MAX_KNOWLEDGE_RESULTS = 1_000
MAX_KNOWLEDGE_VECTORS = 10_000_000
MAX_KNOWLEDGE_FILTERS = 64
MAX_KNOWLEDGE_FILTER_VALUE_CHARS = 256
MAX_KNOWLEDGE_FILTER_TOTAL_CHARS = 4_096
MAX_KNOWLEDGE_PROJECT_CHARS = 1_024
MAX_KNOWLEDGE_EXACT_TERMS = 64
MAX_KNOWLEDGE_PLAN_STEPS = 32

_QUERY_LIMITS = QueryLimits(
    query_chars=MAX_KNOWLEDGE_QUERY_CHARS,
    results=MAX_KNOWLEDGE_RESULTS,
    vectors=MAX_KNOWLEDGE_VECTORS,
    filters=MAX_KNOWLEDGE_FILTERS,
    filter_value_chars=MAX_KNOWLEDGE_FILTER_VALUE_CHARS,
    filter_total_chars=MAX_KNOWLEDGE_FILTER_TOTAL_CHARS,
    project_chars=MAX_KNOWLEDGE_PROJECT_CHARS,
)

_PLAN_LIMITS = PlanLimits(
    query_chars=MAX_KNOWLEDGE_QUERY_CHARS,
    results=MAX_KNOWLEDGE_RESULTS,
    vectors=MAX_KNOWLEDGE_VECTORS,
    filters=MAX_KNOWLEDGE_FILTERS,
    filter_total_chars=MAX_KNOWLEDGE_FILTER_TOTAL_CHARS,
    project_chars=MAX_KNOWLEDGE_PROJECT_CHARS,
    exact_terms=MAX_KNOWLEDGE_EXACT_TERMS,
    plan_steps=MAX_KNOWLEDGE_PLAN_STEPS,
)


class RetrievalMode(StrEnum):
    DISCOVERY = "discovery"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    text: str
    retrieval_mode: RetrievalMode = RetrievalMode.EVIDENCE
    include_history: bool = False
    source_kinds: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    project: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 20
    max_per_resource: int = 3
    min_section_distance: int = 128
    max_vectors: int = 500_000

    def __post_init__(self) -> None:
        normalized = normalize_knowledge_query(
            text=self.text,
            retrieval_mode=self.retrieval_mode,
            include_history=self.include_history,
            source_kinds=self.source_kinds,
            formats=self.formats,
            project=self.project,
            date_from=self.date_from,
            date_to=self.date_to,
            limit=self.limit,
            max_per_resource=self.max_per_resource,
            min_section_distance=self.min_section_distance,
            max_vectors=self.max_vectors,
            retrieval_mode_type=RetrievalMode,
            limits=_QUERY_LIMITS,
        )
        object.__setattr__(self, "text", normalized.text)
        object.__setattr__(self, "source_kinds", normalized.source_kinds)
        object.__setattr__(self, "formats", normalized.formats)
        object.__setattr__(self, "project", normalized.project)
        object.__setattr__(self, "date_from", normalized.date_from)
        object.__setattr__(self, "date_to", normalized.date_to)


@dataclass(frozen=True, slots=True)
class RetrievalStep:
    channel: str
    ranking_name: str
    reason: str
    candidate_limit: int
    required: bool = False

    def __post_init__(self) -> None:
        validate_retrieval_step(
            channel=self.channel,
            ranking_name=self.ranking_name,
            reason=self.reason,
            candidate_limit=self.candidate_limit,
            required=self.required,
            max_query_chars=MAX_KNOWLEDGE_QUERY_CHARS,
            max_results=MAX_KNOWLEDGE_RESULTS,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "ranking_name": self.ranking_name,
            "reason": self.reason,
            "candidate_limit": self.candidate_limit,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class KnowledgePlan:
    plan_id: str
    normalized_query: str
    retrieval_mode: RetrievalMode
    intents: tuple[str, ...]
    exact_terms: tuple[str, ...]
    source_kinds: tuple[str, ...]
    formats: tuple[str, ...]
    project: str | None
    date_from: str | None
    date_to: str | None
    include_history: bool
    limit: int
    max_per_resource: int
    min_section_distance: int
    max_vectors: int
    steps: tuple[RetrievalStep, ...]
    notices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        step_keys = validate_knowledge_plan_base(
            self,
            retrieval_mode_type=RetrievalMode,
            retrieval_step_type=RetrievalStep,
            limits=_PLAN_LIMITS,
        )
        if self.plan_id.startswith(_KNOWLEDGE_PLAN_V2_PREFIX):
            _validate_knowledge_plan_v2(self)
        elif self.plan_id.startswith(_KNOWLEDGE_PLAN_V3_PREFIX):
            _validate_knowledge_plan_v3(self)
        elif len(step_keys) != len(set(step_keys)):
            raise ValueError("Knowledge plan steps cannot contain duplicate rankings")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
            "kind": "knowledge_query_plan",
            "plan_id": self.plan_id,
            "normalized_query": self.normalized_query,
            "retrieval_mode": self.retrieval_mode.value,
            "intents": list(self.intents),
            "exact_terms": list(self.exact_terms),
            "source_kinds": list(self.source_kinds),
            "formats": list(self.formats),
            "include_history": self.include_history,
            "limit": self.limit,
            "max_per_resource": self.max_per_resource,
            "min_section_distance": self.min_section_distance,
            "max_vectors": self.max_vectors,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.project is not None:
            payload["project"] = self.project
        if self.date_from is not None:
            payload["date_from"] = self.date_from
        if self.date_to is not None:
            payload["date_to"] = self.date_to
        if self.notices:
            payload["notices"] = list(self.notices)
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


# endregion [01]


# region [02] Deterministic signal recognition


def _exact_terms(
    text: str,
) -> tuple[tuple[str, ...], bool, bool, bool, str, str]:
    return extract_exact_terms(text)


# endregion [02]


# region [03] Planner


def _query_plan_signals(
    query: KnowledgeQuery,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return infer_query_plan_signals(
        query,
        exact_terms=_exact_terms,
        code_formats=_CODE_FORMATS,
        max_exact_terms=MAX_KNOWLEDGE_EXACT_TERMS,
    )


def _semantic_ranking_names(
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
) -> tuple[str, ...]:
    return semantic_ranking_names(source_kinds, formats)


def _canonical_retrieval_steps(
    *,
    exact_terms: tuple[str, ...],
    intents: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[RetrievalStep, ...]:
    specs = canonical_retrieval_step_specs(
        exact_terms=exact_terms,
        intents=intents,
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        max_results=MAX_KNOWLEDGE_RESULTS,
        semantic_rankings=_semantic_ranking_names,
    )
    return tuple(
        RetrievalStep(
            spec.channel,
            spec.ranking_name,
            spec.reason,
            spec.candidate_limit,
            spec.required,
        )
        for spec in specs
    )


def _canonical_retrieval_steps_v3(
    *,
    retrieval_mode: RetrievalMode,
    exact_terms: tuple[str, ...],
    intents: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[RetrievalStep, ...]:
    specs = canonical_retrieval_step_specs_v3(
        retrieval_mode=retrieval_mode,
        exact_terms=exact_terms,
        intents=intents,
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        max_results=MAX_KNOWLEDGE_RESULTS,
        semantic_rankings=_semantic_ranking_names,
    )
    return tuple(
        RetrievalStep(
            spec.channel,
            spec.ranking_name,
            spec.reason,
            spec.candidate_limit,
            spec.required,
        )
        for spec in specs
    )


def _knowledge_plan_identity_payload(
    *,
    normalized_query: str,
    retrieval_mode: RetrievalMode,
    intents: tuple[str, ...],
    exact_terms: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    include_history: bool,
    limit: int,
    max_per_resource: int,
    min_section_distance: int,
    max_vectors: int,
    steps: tuple[RetrievalStep, ...],
    notices: tuple[str, ...],
) -> dict[str, object]:
    return knowledge_plan_identity_payload(
        schema_version=KNOWLEDGE_CONTRACT_SCHEMA_VERSION,
        normalized_query=normalized_query,
        retrieval_mode=retrieval_mode,
        intents=intents,
        exact_terms=exact_terms,
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
        include_history=include_history,
        limit=limit,
        max_per_resource=max_per_resource,
        min_section_distance=min_section_distance,
        max_vectors=max_vectors,
        steps=steps,
        notices=notices,
    )


def _knowledge_plan_identifier(
    *,
    normalized_query: str,
    retrieval_mode: RetrievalMode,
    intents: tuple[str, ...],
    exact_terms: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    include_history: bool,
    limit: int,
    max_per_resource: int,
    min_section_distance: int,
    max_vectors: int,
    steps: tuple[RetrievalStep, ...],
    notices: tuple[str, ...],
) -> str:
    payload = _knowledge_plan_identity_payload(
        normalized_query=normalized_query,
        retrieval_mode=retrieval_mode,
        intents=intents,
        exact_terms=exact_terms,
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
        include_history=include_history,
        limit=limit,
        max_per_resource=max_per_resource,
        min_section_distance=min_section_distance,
        max_vectors=max_vectors,
        steps=steps,
        notices=notices,
    )
    fingerprint = fingerprint_text(canonical_json(payload))
    return f"{_KNOWLEDGE_PLAN_V2_PREFIX}{fingerprint.xxh3_128}"


def _knowledge_plan_identifier_v3(
    **values: object,
) -> str:
    payload = _knowledge_plan_identity_payload(**values)  # type: ignore[arg-type]
    fingerprint = fingerprint_text(canonical_json(payload))
    return f"{_KNOWLEDGE_PLAN_V3_PREFIX}{fingerprint.xxh3_128}"


def _validate_knowledge_plan_v2(plan: KnowledgePlan) -> None:
    return validate_knowledge_plan_v2(
        plan,
        query_factory=KnowledgeQuery,
        query_plan_signals=_query_plan_signals,
        semantic_ranking_names=_semantic_ranking_names,
        plan_identifier=_knowledge_plan_identifier,
        canonical_retrieval_steps=_canonical_retrieval_steps,
    )


def _validate_knowledge_plan_v3(plan: KnowledgePlan) -> None:
    return validate_knowledge_plan_v3(
        plan,
        query_factory=KnowledgeQuery,
        query_plan_signals=_query_plan_signals,
        semantic_ranking_names=_semantic_ranking_names,
        plan_identifier=_knowledge_plan_identifier_v3,
        canonical_retrieval_steps=_canonical_retrieval_steps_v3,
    )


def plan_knowledge_query(query: KnowledgeQuery) -> KnowledgePlan:
    """Compile a fixed retrieval plan from explicit syntax and bounded rules."""

    terms, intents = _query_plan_signals(query)
    plan_identifier: Callable[..., str]
    if query.retrieval_mode is RetrievalMode.DISCOVERY:
        steps = _canonical_retrieval_steps_v3(
            retrieval_mode=query.retrieval_mode,
            exact_terms=terms,
            intents=intents,
            source_kinds=query.source_kinds,
            formats=query.formats,
            project=query.project,
            date_from=query.date_from,
            date_to=query.date_to,
            limit=query.limit,
        )
        plan_identifier = _knowledge_plan_identifier_v3
    else:
        steps = _canonical_retrieval_steps(
            exact_terms=terms,
            intents=intents,
            source_kinds=query.source_kinds,
            formats=query.formats,
            project=query.project,
            date_from=query.date_from,
            date_to=query.date_to,
            limit=query.limit,
        )
        plan_identifier = _knowledge_plan_identifier
    plan_id = plan_identifier(
        normalized_query=query.text,
        retrieval_mode=query.retrieval_mode,
        intents=intents,
        exact_terms=terms,
        source_kinds=query.source_kinds,
        formats=query.formats,
        project=query.project,
        date_from=query.date_from,
        date_to=query.date_to,
        include_history=query.include_history,
        limit=query.limit,
        max_per_resource=query.max_per_resource,
        min_section_distance=query.min_section_distance,
        max_vectors=query.max_vectors,
        steps=steps,
        notices=(),
    )
    return KnowledgePlan(
        plan_id=plan_id,
        normalized_query=query.text,
        retrieval_mode=query.retrieval_mode,
        intents=intents,
        exact_terms=terms,
        source_kinds=query.source_kinds,
        formats=query.formats,
        project=query.project,
        date_from=query.date_from,
        date_to=query.date_to,
        include_history=query.include_history,
        limit=query.limit,
        max_per_resource=query.max_per_resource,
        min_section_distance=query.min_section_distance,
        max_vectors=query.max_vectors,
        steps=steps,
    )


# endregion [03]


__all__ = (
    "MAX_KNOWLEDGE_EXACT_TERMS",
    "MAX_KNOWLEDGE_QUERY_CHARS",
    "MAX_KNOWLEDGE_RESULTS",
    "MAX_KNOWLEDGE_VECTORS",
    "KnowledgePlan",
    "KnowledgeQuery",
    "RetrievalMode",
    "RetrievalStep",
    "plan_knowledge_query",
)
