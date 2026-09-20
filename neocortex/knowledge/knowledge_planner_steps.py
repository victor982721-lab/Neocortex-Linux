"""Retrieval-step validation and canonical routing for knowledge plans."""
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_planner_steps.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol, TypeVar

# endregion [01]

# region [02] Implementación


_IMAGE_FORMATS = frozenset(
    {"avif", "bmp", "gif", "heic", "heif", "jpeg", "jpg", "png", "tif", "tiff", "webp"}
)
_NON_LEXICAL_SOURCE_KINDS = frozenset({"image", "image_ocr"})
_CATALOG_SOURCE_KINDS = frozenset({"audio", "docx", "office", "pdf", "pptx", "xlsx"})
_CATALOG_FORMATS = frozenset(
    {
        "aac",
        "doc",
        "docx",
        "flac",
        "m4a",
        "mp3",
        "odp",
        "ods",
        "odt",
        "ogg",
        "opus",
        "pdf",
        "ppt",
        "pptx",
        "wav",
        "wma",
        "xls",
        "xlsx",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalStepSpec:
    channel: str
    ranking_name: str
    reason: str
    candidate_limit: int
    required: bool


def _validate_step_text(
    name: str,
    value: object,
    *,
    max_query_chars: int,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"retrieval step {name} must be a string")
    if not value.strip():
        raise ValueError(f"retrieval step {name} cannot be blank")
    if value != value.strip():
        raise ValueError(f"retrieval step {name} cannot have surrounding whitespace")
    if len(value) > max_query_chars:
        raise ValueError(
            f"retrieval step {name} cannot exceed {max_query_chars} characters"
        )


def validate_retrieval_step(
    *,
    channel: object,
    ranking_name: object,
    reason: object,
    candidate_limit: object,
    required: object,
    max_query_chars: int,
    max_results: int,
) -> None:
    for name, value in (
        ("channel", channel),
        ("ranking_name", ranking_name),
        ("reason", reason),
    ):
        _validate_step_text(name, value, max_query_chars=max_query_chars)
    if (
        isinstance(candidate_limit, bool)
        or not isinstance(candidate_limit, int)
        or not 1 <= candidate_limit <= max_results
    ):
        raise ValueError(
            f"retrieval candidate limit must be between 1 and {max_results}"
        )
    if not isinstance(required, bool):
        raise ValueError("retrieval step required must be a bool")


def semantic_ranking_names(
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
) -> tuple[str, ...]:
    format_keys = tuple(value.removeprefix(".") for value in formats)
    if source_kinds:
        # An image source can carry OCR text in the textual vector space.
        # Source filters are not an instruction to discard that content channel.
        include_text = True
        include_image = "image" in source_kinds
    elif formats:
        include_text = True
        include_image = any(value in _IMAGE_FORMATS for value in format_keys)
    else:
        include_text = True
        include_image = True
    rankings: list[str] = []
    if include_text:
        rankings.append("semantic_text")
    if include_image:
        rankings.append("semantic_image")
    return tuple(rankings)


def image_only_source_filters(source_kinds: tuple[str, ...], formats: tuple[str, ...]) -> bool:
    """Recognize an explicit source filter independently of selected channels."""
    if source_kinds:
        return all(source_kind == "image" for source_kind in source_kinds)
    return bool(formats) and all(value.removeprefix(".") in _IMAGE_FORMATS for value in formats)


def _has_explicit_catalog_filters(
    source_kinds: tuple[str, ...],
    format_keys: tuple[str, ...],
    *,
    project: str | None,
    date_from: str | None,
    date_to: str | None,
) -> bool:
    return bool(
        any(value in _CATALOG_SOURCE_KINDS for value in source_kinds)
        or any(value in _CATALOG_FORMATS for value in format_keys)
        or project is not None
        or date_from is not None
        or date_to is not None
    )


def _is_lexical_required(
    source_kinds: tuple[str, ...],
    format_keys: tuple[str, ...],
) -> bool:
    retrieval_scope_present = bool(source_kinds or format_keys)
    return (
        not retrieval_scope_present
        or any(
            source_kind not in _NON_LEXICAL_SOURCE_KINDS for source_kind in source_kinds
        )
        or any(
            value not in _IMAGE_FORMATS
            for value in format_keys
        )
    )


def _semantic_specs(
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    *,
    candidate_limit: int,
    semantic_rankings: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> tuple[RetrievalStepSpec, ...]:
    specs: list[RetrievalStepSpec] = []
    for ranking_name in semantic_rankings(source_kinds, formats):
        reason = (
            "semantic text retrieval covers compatible text and OCR evidence"
            if ranking_name == "semantic_text"
            else "semantic image retrieval covers compatible visual evidence"
        )
        specs.append(
            RetrievalStepSpec(
                "semantic",
                ranking_name,
                reason,
                candidate_limit,
                True,
            )
        )
    return tuple(specs)


def canonical_retrieval_step_specs(
    *,
    exact_terms: tuple[str, ...],
    intents: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    max_results: int,
    semantic_rankings: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> tuple[RetrievalStepSpec, ...]:
    format_keys = tuple(value.removeprefix(".") for value in formats)
    explicit_catalog_filters = _has_explicit_catalog_filters(
        source_kinds,
        format_keys,
        project=project,
        date_from=date_from,
        date_to=date_to,
    )
    catalog = explicit_catalog_filters or (
        bool(exact_terms) and not bool(source_kinds or formats)
    )
    candidate_limit = min(max_results, max(limit * 3, limit))
    specs: list[RetrievalStepSpec] = []
    if exact_terms:
        specs.append(
            RetrievalStepSpec(
                "exact",
                "exact_identifiers",
                "query contains exact path, identifier, hash or serial syntax",
                candidate_limit,
                True,
            )
        )
    specs.append(
        RetrievalStepSpec(
            "lexical",
            "owner_fts",
            "exact lexical evidence is available from owner FTS indexes",
            candidate_limit,
            _is_lexical_required(source_kinds, format_keys),
        )
    )
    specs.extend(
        _semantic_specs(
            source_kinds,
            formats,
            candidate_limit=candidate_limit,
            semantic_rankings=semantic_rankings,
        )
    )
    if catalog:
        specs.append(
            RetrievalStepSpec(
                "catalog",
                "catalog_metadata",
                "exact identifiers or explicit filters require owner metadata",
                candidate_limit,
                explicit_catalog_filters,
            )
        )
    if "relational" in intents:
        specs.append(
            RetrievalStepSpec(
                "relational",
                "verified_relations",
                "query asks for a relation or dependency",
                candidate_limit,
                True,
            )
        )
    if "temporal" in intents:
        specs.append(
            RetrievalStepSpec(
                "temporal",
                "published_history",
                "query requests history, vigency or a temporal boundary",
                candidate_limit,
                True,
            )
        )
    return tuple(specs)


def canonical_retrieval_step_specs_v3(
    *,
    retrieval_mode: RetrievalModeLike,
    exact_terms: tuple[str, ...],
    intents: tuple[str, ...],
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    project: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    max_results: int,
    semantic_rankings: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> tuple[RetrievalStepSpec, ...]:
    """Return v3 topology with an explicit optional title discovery channel."""

    base = canonical_retrieval_step_specs(
        exact_terms=exact_terms,
        intents=intents,
        source_kinds=source_kinds,
        formats=formats,
        project=project,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        max_results=max_results,
        semantic_rankings=semantic_rankings,
    )
    if retrieval_mode.value != "discovery":
        return base
    candidate_limit = min(max_results, max(limit * 3, limit))
    expanded: list[RetrievalStepSpec] = []
    for spec in base:
        expanded.append(spec)
        if spec.channel == "semantic" and spec.ranking_name == "semantic_text":
            expanded.append(
                RetrievalStepSpec(
                    "semantic_discovery",
                    "semantic_title",
                    "durable basename metadata may boost already-grounded resources",
                    candidate_limit,
                    False,
                )
            )
    return tuple(expanded)


KNOWLEDGE_PLAN_V2_PREFIX = "knowledge-plan-v2:"
_KNOWLEDGE_PLAN_V2_PATTERN = re.compile(
    rf"{re.escape(KNOWLEDGE_PLAN_V2_PREFIX)}[0-9a-f]{{32}}"
)
KNOWLEDGE_PLAN_V3_PREFIX = "knowledge-plan-v3:"
_KNOWLEDGE_PLAN_V3_PATTERN = re.compile(
    rf"{re.escape(KNOWLEDGE_PLAN_V3_PREFIX)}[0-9a-f]{{32}}"
)
_ALLOWED_RETRIEVAL_STEPS = frozenset(
    {
        ("exact", "exact_identifiers"),
        ("lexical", "owner_fts"),
        ("semantic", "semantic_text"),
        ("semantic", "semantic_image"),
        ("catalog", "catalog_metadata"),
        ("relational", "verified_relations"),
        ("temporal", "published_history"),
    }
)
_ALLOWED_RETRIEVAL_STEPS_V3 = _ALLOWED_RETRIEVAL_STEPS | {
    ("semantic_discovery", "semantic_title")
}


class RetrievalModeLike(Protocol):
    @property
    def value(self) -> str: ...


class RetrievalStepLike(Protocol):
    @property
    def channel(self) -> str: ...

    @property
    def ranking_name(self) -> str: ...

    @property
    def required(self) -> bool: ...

    def to_dict(self) -> dict[str, object]: ...


class KnowledgePlanLike(Protocol):
    @property
    def plan_id(self) -> str: ...

    @property
    def normalized_query(self) -> str: ...

    @property
    def retrieval_mode(self) -> RetrievalModeLike: ...

    @property
    def intents(self) -> tuple[str, ...]: ...

    @property
    def exact_terms(self) -> tuple[str, ...]: ...

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

    @property
    def include_history(self) -> bool: ...

    @property
    def limit(self) -> int: ...

    @property
    def max_per_resource(self) -> int: ...

    @property
    def min_section_distance(self) -> int: ...

    @property
    def max_vectors(self) -> int: ...

    @property
    def steps(self) -> tuple[RetrievalStepLike, ...]: ...

    @property
    def notices(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class PlanLimits:
    query_chars: int
    results: int
    vectors: int
    filters: int
    filter_total_chars: int
    project_chars: int
    exact_terms: int
    plan_steps: int


def _validate_plan_identity_strings(
    plan: KnowledgePlanLike,
    limits: PlanLimits,
) -> None:
    for name, value in (
        ("plan_id", plan.plan_id),
        ("normalized_query", plan.normalized_query),
    ):
        if not isinstance(value, str):
            raise ValueError(f"Knowledge plan {name} must be a string")
        if not value.strip():
            raise ValueError(f"Knowledge plan {name} cannot be blank")
        if value != value.strip():
            raise ValueError(
                f"Knowledge plan {name} cannot have surrounding whitespace"
            )
        if len(value) > limits.query_chars:
            raise ValueError(
                f"Knowledge plan {name} cannot exceed {limits.query_chars} characters"
            )


def _validate_plan_string_sequence(
    name: str,
    values: object,
    *,
    maximum: int,
    limits: PlanLimits,
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"Knowledge plan {name} must be a tuple of strings")
    if len(values) > maximum:
        raise ValueError(
            f"Knowledge plan {name} cannot contain more than {maximum} values"
        )
    total_chars = 0
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"Knowledge plan {name} must contain only strings")
        if not value.strip():
            raise ValueError(f"Knowledge plan {name} cannot contain blank values")
        if value != value.strip():
            raise ValueError(
                f"Knowledge plan {name} values cannot have surrounding whitespace"
            )
        if len(value) > limits.query_chars:
            raise ValueError(
                f"Knowledge plan {name} values cannot exceed "
                f"{limits.query_chars} characters"
            )
        total_chars += len(value)
        if total_chars > limits.filter_total_chars:
            raise ValueError(
                f"Knowledge plan {name} cannot exceed "
                f"{limits.filter_total_chars} total characters"
            )
        if value in seen:
            raise ValueError(f"Knowledge plan {name} cannot contain duplicates")
        seen.add(value)


def _validate_plan_string_sequences(
    plan: KnowledgePlanLike,
    limits: PlanLimits,
) -> None:
    for name, values, maximum in (
        ("intents", plan.intents, limits.filters),
        ("exact_terms", plan.exact_terms, limits.exact_terms),
        ("source_kinds", plan.source_kinds, limits.filters),
        ("formats", plan.formats, limits.filters),
        ("notices", plan.notices, limits.filters),
    ):
        _validate_plan_string_sequence(
            name,
            values,
            maximum=maximum,
            limits=limits,
        )


def _validate_canonical_filters(plan: KnowledgePlanLike) -> None:
    if plan.source_kinds != tuple(value.casefold() for value in plan.source_kinds):
        raise ValueError(
            "Knowledge plan source_kinds must be canonical lowercase values"
        )
    if plan.formats != tuple(value.casefold() for value in plan.formats):
        raise ValueError("Knowledge plan formats must be canonical lowercase values")


def _validate_plan_project(plan: KnowledgePlanLike, limits: PlanLimits) -> None:
    if plan.project is None:
        return
    if not isinstance(plan.project, str):
        raise ValueError("Knowledge plan project must be a string when present")
    if not plan.project.strip():
        raise ValueError("Knowledge plan project cannot be blank when present")
    if plan.project != plan.project.strip():
        raise ValueError("Knowledge plan project cannot have surrounding whitespace")
    if len(plan.project) > limits.project_chars:
        raise ValueError(
            f"Knowledge plan project cannot exceed {limits.project_chars} characters"
        )


def validated_date(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO calendar date string")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO calendar date") from exc


def _validate_plan_dates(plan: KnowledgePlanLike) -> None:
    normalized_from = validated_date("Knowledge plan date_from", plan.date_from)
    normalized_to = validated_date("Knowledge plan date_to", plan.date_to)
    if normalized_from != plan.date_from or normalized_to != plan.date_to:
        raise ValueError("Knowledge plan dates must use canonical ISO format")
    if (
        normalized_from is not None
        and normalized_to is not None
        and normalized_from > normalized_to
    ):
        raise ValueError("Knowledge plan date_from cannot be after date_to")


def _validate_plan_numbers(plan: KnowledgePlanLike, limits: PlanLimits) -> None:
    for name, numeric_value, minimum, maximum in (
        ("limit", plan.limit, 1, limits.results),
        ("max_per_resource", plan.max_per_resource, 1, 100),
        ("min_section_distance", plan.min_section_distance, 0, 1_000_000),
        ("max_vectors", plan.max_vectors, 1, limits.vectors),
    ):
        if (
            isinstance(numeric_value, bool)
            or not isinstance(numeric_value, int)
            or not minimum <= numeric_value <= maximum
        ):
            raise ValueError(
                f"Knowledge plan {name} must be between {minimum} and {maximum}"
            )


def _validated_step_keys(
    plan: KnowledgePlanLike,
    *,
    retrieval_step_type: type[object],
    limits: PlanLimits,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(plan.steps, tuple):
        raise ValueError("Knowledge plan steps must be a tuple")
    if len(plan.steps) > limits.plan_steps:
        raise ValueError(
            f"Knowledge plan cannot contain more than {limits.plan_steps} steps"
        )
    if not all(isinstance(step, retrieval_step_type) for step in plan.steps):
        raise ValueError("Knowledge plan steps must contain only RetrievalStep values")
    return tuple((step.channel, step.ranking_name) for step in plan.steps)


def validate_knowledge_plan_base(
    plan: KnowledgePlanLike,
    *,
    retrieval_mode_type: type[object],
    retrieval_step_type: type[object],
    limits: PlanLimits,
) -> tuple[tuple[str, str], ...]:
    _validate_plan_identity_strings(plan, limits)
    if not isinstance(plan.retrieval_mode, retrieval_mode_type):
        raise ValueError(
            "Knowledge plan retrieval_mode must be a RetrievalMode instance"
        )
    _validate_plan_string_sequences(plan, limits)
    _validate_canonical_filters(plan)
    _validate_plan_project(plan, limits)
    _validate_plan_dates(plan)
    if not isinstance(plan.include_history, bool):
        raise ValueError("Knowledge plan include_history must be a bool")
    _validate_plan_numbers(plan, limits)
    return _validated_step_keys(
        plan,
        retrieval_step_type=retrieval_step_type,
        limits=limits,
    )


def knowledge_plan_identity_payload(
    *,
    schema_version: int,
    normalized_query: str,
    retrieval_mode: RetrievalModeLike,
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
    steps: tuple[RetrievalStepLike, ...],
    notices: tuple[str, ...],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "query": normalized_query,
        "retrieval_mode": retrieval_mode.value,
        "intents": list(intents),
        "exact_terms": list(exact_terms),
        "source_kinds": list(source_kinds),
        "formats": list(formats),
        "project": project,
        "date_from": date_from,
        "date_to": date_to,
        "include_history": include_history,
        "limit": limit,
        "max_per_resource": max_per_resource,
        "min_section_distance": min_section_distance,
        "max_vectors": max_vectors,
        "steps": [step.to_dict() for step in steps],
    }
    if notices:
        payload["notices"] = list(notices)
    return payload


def _validate_v2_identifier(plan: KnowledgePlanLike) -> None:
    if _KNOWLEDGE_PLAN_V2_PATTERN.fullmatch(plan.plan_id) is None:
        raise ValueError(
            "Knowledge plan v2 plan_id must contain a lowercase XXH3-128 digest"
        )


def _validate_supported_steps(step_keys: tuple[tuple[str, str], ...]) -> None:
    if any(step_key not in _ALLOWED_RETRIEVAL_STEPS for step_key in step_keys):
        raise ValueError("Knowledge plan v2 contains an unsupported retrieval step")


def _validate_lexical_steps(plan: KnowledgePlanLike) -> None:
    lexical_steps = tuple(step for step in plan.steps if step.channel == "lexical")
    if len(lexical_steps) != 1:
        raise ValueError(
            "Knowledge plan v2 must contain exactly one lexical retrieval step"
        )


def _semantic_names(plan: KnowledgePlanLike) -> tuple[str, ...]:
    return tuple(step.ranking_name for step in plan.steps if step.channel == "semantic")


def _validate_semantic_uniqueness(semantic_names: tuple[str, ...]) -> None:
    if len(semantic_names) != len(set(semantic_names)):
        raise ValueError(
            "Knowledge plan v2 semantic rankings cannot contain duplicates"
        )


def _validate_semantic_scope(
    plan: KnowledgePlanLike,
    semantic_names: tuple[str, ...],
    semantic_ranking_names: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> None:
    if semantic_names != semantic_ranking_names(plan.source_kinds, plan.formats):
        raise ValueError(
            "Knowledge plan v2 semantic rankings do not match its retrieval scope"
        )


def _validate_ranking_uniqueness(step_keys: tuple[tuple[str, str], ...]) -> None:
    if len(step_keys) != len(set(step_keys)):
        raise ValueError(
            "Knowledge plan v2 retrieval rankings cannot contain duplicates"
        )


def _validate_v2_shape(
    plan: KnowledgePlanLike,
    semantic_ranking_names: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> None:
    _validate_v2_identifier(plan)
    step_keys = tuple((step.channel, step.ranking_name) for step in plan.steps)
    _validate_supported_steps(step_keys)
    _validate_lexical_steps(plan)
    semantic_names = _semantic_names(plan)
    _validate_semantic_uniqueness(semantic_names)
    _validate_semantic_scope(plan, semantic_names, semantic_ranking_names)
    _validate_ranking_uniqueness(step_keys)


_QueryT = TypeVar("_QueryT")
_StepT = TypeVar("_StepT")


def validate_knowledge_plan_v2(
    plan: KnowledgePlanLike,
    *,
    query_factory: Callable[..., _QueryT],
    query_plan_signals: Callable[
        [_QueryT],
        tuple[tuple[str, ...], tuple[str, ...]],
    ],
    semantic_ranking_names: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
    plan_identifier: Callable[..., str],
    canonical_retrieval_steps: Callable[..., tuple[_StepT, ...]],
) -> None:
    _validate_v2_shape(plan, semantic_ranking_names)
    query = query_factory(
        text=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode,
        include_history=plan.include_history,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
    )
    expected_terms, expected_intents = query_plan_signals(query)
    if plan.exact_terms != expected_terms or plan.intents != expected_intents:
        raise ValueError(
            "Knowledge plan v2 query signals do not match its normalized query"
        )
    expected_identifier = plan_identifier(
        normalized_query=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode,
        intents=plan.intents,
        exact_terms=plan.exact_terms,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        include_history=plan.include_history,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
        steps=plan.steps,
        notices=plan.notices,
    )
    if plan.plan_id != expected_identifier:
        raise ValueError(
            "Knowledge plan v2 plan_id does not match its canonical payload"
        )
    expected_steps = canonical_retrieval_steps(
        exact_terms=expected_terms,
        intents=expected_intents,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        limit=plan.limit,
    )
    if plan.steps != expected_steps:
        raise ValueError(
            "Knowledge plan v2 steps do not match canonical executable topology"
        )


def _validate_v3_shape(
    plan: KnowledgePlanLike,
    semantic_ranking_names: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
) -> None:
    if _KNOWLEDGE_PLAN_V3_PATTERN.fullmatch(plan.plan_id) is None:
        raise ValueError(
            "Knowledge plan v3 plan_id must contain a lowercase XXH3-128 digest"
        )
    step_keys = tuple((step.channel, step.ranking_name) for step in plan.steps)
    if any(step_key not in _ALLOWED_RETRIEVAL_STEPS_V3 for step_key in step_keys):
        raise ValueError("Knowledge plan v3 contains an unsupported retrieval step")
    lexical_steps = tuple(step for step in plan.steps if step.channel == "lexical")
    if len(lexical_steps) != 1:
        raise ValueError(
            "Knowledge plan v3 must contain exactly one lexical retrieval step"
        )
    semantic_names = _semantic_names(plan)
    if len(semantic_names) != len(set(semantic_names)):
        raise ValueError(
            "Knowledge plan v3 semantic rankings cannot contain duplicates"
        )
    if semantic_names != semantic_ranking_names(plan.source_kinds, plan.formats):
        raise ValueError(
            "Knowledge plan v3 semantic rankings do not match its retrieval scope"
        )
    discovery_steps = tuple(
        step for step in plan.steps if step.channel == "semantic_discovery"
    )
    expected_title = (
        plan.retrieval_mode.value == "discovery" and "semantic_text" in semantic_names
    )
    if expected_title:
        if len(discovery_steps) != 1:
            raise ValueError(
                "Knowledge plan v3 discovery must contain one semantic title step"
            )
        title_step = discovery_steps[0]
        if title_step.ranking_name != "semantic_title" or title_step.required:
            raise ValueError("Knowledge plan v3 semantic title step must be optional")
    elif discovery_steps:
        raise ValueError(
            "Knowledge plan v3 semantic title step is outside retrieval scope"
        )
    if len(step_keys) != len(set(step_keys)):
        raise ValueError(
            "Knowledge plan v3 retrieval rankings cannot contain duplicates"
        )


def validate_knowledge_plan_v3(
    plan: KnowledgePlanLike,
    *,
    query_factory: Callable[..., _QueryT],
    query_plan_signals: Callable[
        [_QueryT],
        tuple[tuple[str, ...], tuple[str, ...]],
    ],
    semantic_ranking_names: Callable[
        [tuple[str, ...], tuple[str, ...]],
        tuple[str, ...],
    ],
    plan_identifier: Callable[..., str],
    canonical_retrieval_steps: Callable[..., tuple[_StepT, ...]],
) -> None:
    _validate_v3_shape(plan, semantic_ranking_names)
    query = query_factory(
        text=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode,
        include_history=plan.include_history,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
    )
    expected_terms, expected_intents = query_plan_signals(query)
    if plan.exact_terms != expected_terms or plan.intents != expected_intents:
        raise ValueError(
            "Knowledge plan v3 query signals do not match its normalized query"
        )
    expected_identifier = plan_identifier(
        normalized_query=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode,
        intents=plan.intents,
        exact_terms=plan.exact_terms,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        include_history=plan.include_history,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
        steps=plan.steps,
        notices=plan.notices,
    )
    if plan.plan_id != expected_identifier:
        raise ValueError(
            "Knowledge plan v3 plan_id does not match its canonical payload"
        )
    expected_steps = canonical_retrieval_steps(
        retrieval_mode=plan.retrieval_mode,
        exact_terms=expected_terms,
        intents=expected_intents,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        limit=plan.limit,
    )
    if plan.steps != expected_steps:
        raise ValueError(
            "Knowledge plan v3 steps do not match canonical executable topology"
        )


__all__ = (  # noqa: RUF022
    "KNOWLEDGE_PLAN_V2_PREFIX",
    "KNOWLEDGE_PLAN_V3_PREFIX",
    "KnowledgePlanLike",
    "PlanLimits",
    "RetrievalStepLike",
    "RetrievalStepSpec",
    "canonical_retrieval_step_specs",
    "canonical_retrieval_step_specs_v3",
    "knowledge_plan_identity_payload",
    "semantic_ranking_names",
    "validate_knowledge_plan_base",
    "validate_knowledge_plan_v2",
    "validate_knowledge_plan_v3",
    "validated_date",
    "validate_retrieval_step",
)
# endregion [02]
