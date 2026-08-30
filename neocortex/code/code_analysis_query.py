"""Bounded multidimensional queries over explicit published Code surfaces."""

from __future__ import annotations
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_questions_payload,
    analysis_identity,
    analysis_question_spec_fingerprint,
    parse_analysis_questions_payload,
)
from .code_analyzer_effectiveness import (
    CodeAnalyzerEffectivenessAnalysis,
    analyzer_effectiveness_questions,
    parse_code_analyzer_effectiveness_payload,
)
from .code_analyzer_calibration import (
    CodeAnalyzerCalibrationAnalysis,
    analyzer_calibration_questions,
    parse_code_analyzer_calibration_payload,
)
from .code_assurance_analysis import (
    CodeAssuranceAnalysis,
    assurance_questions,
    parse_code_assurance_payload,
)
from .code_invariant_assurance_analysis import (
    CodeInvariantAssuranceAnalysis,
    invariant_assurance_questions,
    parse_code_invariant_assurance_payload,
)
from .code_architecture_questions import (
    CodeArchitectureAnalysis,
    architecture_questions,
    parse_code_architecture_question_payload,
)
from .code_capability_reachability_analysis import (
    CodeCapabilityReachabilityAnalysis,
    capability_reachability_questions,
    parse_capability_reachability_payload,
)
from .code_change_evolution_analysis import (
    CodeChangeEvolutionAnalysis,
    expected_code_change_evolution_questions,
    parse_code_change_evolution_payload,
)
from .code_class_surface_analysis import (
    expected_class_surface_questions,
    parse_code_class_surface_payload,
)
from .code_interface_surface_analysis import (
    CodeInterfaceSurfaceAnalysis,
    interface_surface_questions,
    parse_code_interface_surface_payload,
)
from .code_knowledge_asset_health_analysis import knowledge_asset_health_questions
from .code_knowledge_pdf_asset_health_analysis import knowledge_pdf_asset_health_questions
from .code_experiment_planner import (
    CodeExperimentPlan,
    experiment_template,
    parse_code_experiment_plan_payload,
    plan_code_experiments,
)
from .code_experiment_store import (
    CODE_EXPERIMENT_STORE_MAX_RESOLVED,
    ResolvedCodeExperimentReceipt,
    apply_code_experiment_receipts,
    parse_resolved_code_experiment_receipt_payload,
)
from .code_route_capability_analysis import (
    CodeRouteCapabilityAnalysis,
    parse_code_route_capability_payload,
    route_capability_questions,
)
from .code_retention_analysis import (
    CodeRetentionAnalysis,
    parse_code_retention_analysis_payload,
    retention_questions,
)
from .code_review_task_analysis import framework_review_task_questions
from .code_state_projection_analysis import (
    CodeStateProjectionAnalysis,
    parse_code_state_projection_payload,
    state_projection_questions,
)
from .code_state_topology_analysis import (
    CodeStateTopologyAnalysis,
    parse_code_state_topology_payload,
    state_topology_questions,
)
from .code_state_interaction_analysis import (
    CodeStateInteractionAnalysis,
    parse_code_state_interaction_payload,
    state_interaction_questions,
)
from .code_technical_verification import (
    CodeTechnicalVerification,
    build_code_technical_verification,
    parse_code_technical_verification_payload,
)
from .code_review_epistemics import STRUCTURAL_HOTSPOT_QUESTION
from .code_security_dependency_questions import security_dependency_questions
from .code_schema import CODE_SCHEMA_VERSION
from .code_supply_chain_analysis import (
    CodeSupplyChainAnalysis,
    parse_code_supply_chain_payload,
)
from neocortex.semantic.semantic_models import canonical_json

CODE_ANALYSIS_QUERY_SCHEMA = "neocortex.code-analysis-query/v1"
CODE_ANALYSIS_QUERY_MAX_FILTERS_PER_DIMENSION = 32
CODE_ANALYSIS_QUERY_MAX_FILTERS_TOTAL = 64
CODE_ANALYSIS_QUERY_MAX_FILTER_VALUE_BYTES = 512
CODE_ANALYSIS_QUERY_MAX_FILTER_BYTES_TOTAL = 8 * 1024
CODE_ANALYSIS_QUERY_MAX_SOURCE_SEQUENCE_ITEMS = 20_000
CODE_ANALYSIS_QUERY_MAX_SOURCE_RECORDS = 20_000
CODE_ANALYSIS_QUERY_MAX_RECORD_BYTES = 64 * 1024
CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
CODE_ANALYSIS_QUERY_MAX_ACTIONS = 16
CODE_ANALYSIS_QUERY_MAX_TEMPLATE_LIMITATIONS = 16
CODE_ANALYSIS_QUERY_MAX_RECEIPT_DETAILS = 16

_SURFACE_KINDS = {
    "status": "code-status",
    "review": "code-review",
    "diff": "code-publication-diff",
}
_DIMENSIONS = (
    "providers",
    "categories",
    "modules",
    "statuses",
    "deltas",
    "work_packages",
)
_ENGINEERING_DIMENSIONS = ("complexity", "coverage", "mutation", "history", "graph")
_SCALAR_TYPES = (str, int, float, bool)
_CODE_REVIEW_V10 = "neocortex.code-review/v10"
_CODE_REVIEW_V11 = "neocortex.code-review/v11"
_CODE_REVIEW_V12 = "neocortex.code-review/v12"
_CODE_REVIEW_V13 = "neocortex.code-review/v13"
_CODE_REVIEW_V14 = "neocortex.code-review/v14"
_CODE_REVIEW_V15 = "neocortex.code-review/v15"
_CODE_REVIEW_V16 = "neocortex.code-review/v16"
_CODE_REVIEW_V17 = "neocortex.code-review/v17"
_CODE_REVIEW_V18 = "neocortex.code-review/v18"
_CODE_REVIEW_V19 = "neocortex.code-review/v19"
_CODE_REVIEW_V20 = "neocortex.code-review/v20"
_CODE_REVIEW_V21 = "neocortex.code-review/v21"
_CODE_REVIEW_V22 = "neocortex.code-review/v22"
_CODE_ANALYSIS_EPISTEMICS_V1 = "neocortex.code-analysis-epistemics/v1"
_UNUSED_V11_STEP_REQUIREMENTS = (
    "verify_import_reexport_callback_registry_protocol_and_entry_point_usage",
    "run_targeted_tests_and_public_import_smoke_without_mutating_code",
    "record_explicit_human_confirmation_or_reclassify_with_new_evidence",
    "require_comparable_unused_analysis_replay_before_any_separate_change",
)


def _public_json_bytes(value: object) -> int:
    """Measure the exact one-line JSON representation used by the public CLI."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("query projection is not canonical JSON") from exc
    return len(encoded) + 1  # The CLI terminates the public report with one newline.


def _normalize_filter(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} filters must be a tuple")
    if len(values) > CODE_ANALYSIS_QUERY_MAX_FILTERS_PER_DIMENSION:
        raise ValueError(
            f"{name} filters exceed {CODE_ANALYSIS_QUERY_MAX_FILTERS_PER_DIMENSION} values"
        )
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} filters must contain strings")
        if len(value.encode("utf-8")) > CODE_ANALYSIS_QUERY_MAX_FILTER_VALUE_BYTES:
            raise ValueError(
                f"{name} filter exceeds {CODE_ANALYSIS_QUERY_MAX_FILTER_VALUE_BYTES} UTF-8 bytes"
            )
        candidate = value.strip().casefold()
        if not candidate:
            raise ValueError(f"{name} filters must be non-empty")
        if len(candidate.encode("utf-8")) > CODE_ANALYSIS_QUERY_MAX_FILTER_VALUE_BYTES:
            raise ValueError(
                f"normalized {name} filter exceeds "
                f"{CODE_ANALYSIS_QUERY_MAX_FILTER_VALUE_BYTES} UTF-8 bytes"
            )
        normalized.add(candidate)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class CodeAnalysisQuery:
    """One normalized, bounded query over a public Code surface."""

    surface: Literal["status", "review", "diff"]
    providers: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    deltas: tuple[str, ...] = ()
    work_packages: tuple[str, ...] = ()
    limit: int = 50

    def __post_init__(self) -> None:
        surface = self.surface.strip().casefold() if isinstance(self.surface, str) else ""
        if surface not in _SURFACE_KINDS:
            raise ValueError("surface must be status, review or diff")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= self.limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        object.__setattr__(self, "surface", surface)
        normalized_filters: dict[str, tuple[str, ...]] = {}
        filter_count = 0
        filter_bytes = 0
        for name in _DIMENSIONS:
            raw_values = getattr(self, name)
            normalized_filters[name] = _normalize_filter(raw_values, name=name)
            filter_count += len(raw_values)
            filter_bytes += sum(len(value.encode("utf-8")) for value in raw_values)
        if filter_count > CODE_ANALYSIS_QUERY_MAX_FILTERS_TOTAL:
            raise ValueError(
                f"query filters exceed {CODE_ANALYSIS_QUERY_MAX_FILTERS_TOTAL} total values"
            )
        normalized_filter_bytes = sum(
            len(value.encode("utf-8")) for values in normalized_filters.values() for value in values
        )
        if max(filter_bytes, normalized_filter_bytes) > CODE_ANALYSIS_QUERY_MAX_FILTER_BYTES_TOTAL:
            raise ValueError(
                f"query filters exceed {CODE_ANALYSIS_QUERY_MAX_FILTER_BYTES_TOTAL} total UTF-8 bytes"
            )
        for name, values in normalized_filters.items():
            object.__setattr__(self, name, values)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    if len(value) > CODE_ANALYSIS_QUERY_MAX_SOURCE_SEQUENCE_ITEMS:
        raise ValueError("query source sequence exceeds its item bound")
    return tuple(item for item in value if isinstance(item, Mapping))


def _string_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidate = value.strip()
        return (candidate,) if candidate else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    if len(value) > CODE_ANALYSIS_QUERY_MAX_SOURCE_SEQUENCE_ITEMS:
        raise ValueError("query source text sequence exceeds its item bound")
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _texts(item: Mapping[str, object], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        values.extend(_string_items(item.get(key)))
    return tuple(values)


def _first_text(item: Mapping[str, object], *keys: str) -> str | None:
    values = _texts(item, *keys)
    return values[0] if values else None


def _dimension_values(values: Iterable[object]) -> list[str]:
    by_key: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate:
            continue
        by_key.setdefault(candidate.casefold(), candidate)
    return [by_key[key] for key in sorted(by_key)]


def _bounded_text(value: str, *, limit: int = 512) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fact_value(value: object) -> object | None:
    if value is None or isinstance(value, _SCALAR_TYPES):
        return _bounded_text(value) if isinstance(value, str) else value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        scalars = [
            _bounded_text(item) for item in value[:8] if isinstance(item, str) and item.strip()
        ]
        return scalars or None
    return None


def _facts(item: Mapping[str, object], *keys: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in keys:
        if key not in item:
            continue
        value = _fact_value(item[key])
        if value is not None:
            result[key] = value
    return result


def _path_module(path: str | None) -> str | None:
    if path is None:
        return None
    normalized = path.replace("\\", "/").strip()
    marker = "/Repository/"
    if marker.casefold() in normalized.casefold():
        index = normalized.casefold().index(marker.casefold())
        normalized = normalized[index + len(marker) :]
    normalized = normalized.removesuffix(".py").strip("/")
    if not normalized:
        return None
    return normalized.replace("/", ".")


def _module_values(item: Mapping[str, object]) -> tuple[str, ...]:
    explicit = _texts(
        item,
        "module_id",
        "module",
        "primary_module",
        "source_module",
        "target_module",
    )
    path = _first_text(item, "relative_path", "path")
    path_module = _path_module(path)
    return explicit + ((path_module,) if path_module else ())


def _record(
    *,
    record_type: str,
    record_id: str,
    source_path: str,
    providers: Iterable[object] = (),
    categories: Iterable[object] = (),
    modules: Iterable[object] = (),
    statuses: Iterable[object] = (),
    deltas: Iterable[object] = (),
    work_packages: Iterable[object] = (),
    facts: Mapping[str, object] | None = None,
) -> dict[str, object]:
    dimensions = {
        "providers": _dimension_values(providers),
        "categories": _dimension_values(categories),
        "modules": _dimension_values(modules),
        "statuses": _dimension_values(statuses),
        "deltas": _dimension_values(deltas),
        "work_packages": _dimension_values(work_packages),
    }
    record: dict[str, object] = {
        "id": _bounded_text(f"{source_path}:{record_id}", limit=1024),
        "record_type": record_type,
        "source_path": source_path,
        "dimensions": dimensions,
        "facts": dict(facts or {}),
    }
    if _public_json_bytes(record) > CODE_ANALYSIS_QUERY_MAX_RECORD_BYTES:
        raise ValueError("query record exceeds its public JSON byte bound")
    return record


def _provider_values(item: Mapping[str, object]) -> tuple[str, ...]:
    values = list(_texts(item, "provider_id", "provider", "analyzer_id", "source"))
    values.extend(_texts(item, "provider_ids", "provenance"))
    for diagnostic in _mapping_items(item.get("diagnostics")):
        values.extend(_texts(diagnostic, "source", "tool_name", "provider_id"))
    return tuple(values)


def _delta_words(item: Mapping[str, object], *keys: str) -> tuple[str, ...]:
    words = list(_texts(item, "change", "delta", "verdict"))
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            words.extend((key, "increased"))
        elif value < 0:
            words.extend((key, "decreased"))
        else:
            words.append("unchanged")
    return tuple(words)


def _append_provider_suite(
    records: list[dict[str, object]],
    suite: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if suite is None:
        return
    for index, provider in enumerate(_mapping_items(suite.get("providers"))):
        provider_id = _first_text(provider, "provider_id", "provider") or str(index)
        records.append(
            _record(
                record_type="provider",
                record_id=provider_id,
                source_path=f"{source_path}.providers[{index}]",
                providers=(provider_id,),
                categories=("external_provider", _first_text(provider, "profile") or ""),
                statuses=_texts(provider, "status", "gate", "execution"),
                facts=_facts(
                    provider,
                    "provider_schema",
                    "tool_name",
                    "tool_version",
                    "findings",
                    "metrics",
                    "relations",
                    "covered_files",
                    "eligible_files",
                    "reason",
                ),
            )
        )


def _append_architecture(
    records: list[dict[str, object]],
    architecture: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if architecture is None:
        return
    modules = _mapping_items(architecture.get("modules"))
    if not modules and _mapping(architecture.get("counts")) is not None:
        records.append(
            _record(
                record_type="architecture_summary",
                record_id=str(architecture.get("analysis_run_id") or "current"),
                source_path=f"{source_path}.summary",
                categories=("architecture", "summary"),
                statuses=_texts(architecture, "status", "gate"),
                facts={
                    **_facts(architecture, "reason", "analysis_run_id"),
                    **(dict(_mapping(architecture.get("counts")) or {})),
                },
            )
        )
    for index, module in enumerate(modules):
        module_id = _first_text(module, "module_id", "module") or str(index)
        records.append(
            _record(
                record_type="architecture_module",
                record_id=module_id,
                source_path=f"{source_path}.modules[{index}]",
                categories=("architecture", "module"),
                modules=_module_values(module),
                statuses=_texts(module, "status"),
                facts=_facts(
                    module,
                    "path_namespace_id",
                    "owner_id",
                    "fan_in",
                    "fan_out",
                    "blast_radius",
                    "dependency_reach",
                    "cross_path_namespace_fan_in",
                    "cross_path_namespace_fan_out",
                    "cross_owner_fan_in",
                    "cross_owner_fan_out",
                    "directed_degree_centrality",
                    "cognitive_complexity_max",
                    "cognitive_complexity_total",
                ),
            )
        )


def _append_engineering(
    records: list[dict[str, object]],
    engineering: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if engineering is None:
        return
    for index, module in enumerate(_mapping_items(engineering.get("modules"))):
        module_id = _first_text(module, "module_id") or str(index)
        providers: list[str] = []
        categories: list[str] = ["engineering"]
        statuses: list[str] = []
        facts = _facts(module, "path_namespace_id", "owner_id")
        for dimension_name in _ENGINEERING_DIMENSIONS:
            dimension = _mapping(module.get(dimension_name))
            if dimension is None:
                continue
            categories.append(dimension_name)
            providers.extend(_texts(dimension, "provenance"))
            statuses.extend(_texts(dimension, "status"))
            facts[f"{dimension_name}_status"] = _first_text(dimension, "status")
            facts[f"{dimension_name}_metric_count"] = len(_mapping_items(dimension.get("metrics")))
        records.append(
            _record(
                record_type="engineering_module",
                record_id=module_id,
                source_path=f"{source_path}.modules[{index}]",
                providers=providers,
                categories=categories,
                modules=_module_values(module),
                statuses=statuses,
                facts=facts,
            )
        )


def _append_supply_chain(
    records: list[dict[str, object]],
    supply_chain: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if supply_chain is None:
        return
    for index, observation in enumerate(_mapping_items(supply_chain.get("observations"))):
        observation_id = _first_text(observation, "observation_id", "id") or str(index)
        category = _first_text(observation, "category") or "supply_chain"
        records.append(
            _record(
                record_type="supply_chain_observation",
                record_id=observation_id,
                source_path=f"{source_path}.observations[{index}]",
                providers=_provider_values(observation),
                categories=("supply_chain", category, _first_text(observation, "code") or ""),
                modules=_module_values(observation),
                statuses=_texts(observation, "status", "severity", "freshness"),
                facts=_facts(
                    observation,
                    "code",
                    "evidence_kind",
                    "message",
                    "path",
                    "start_line",
                    "end_line",
                    "gate_authority",
                    "observed_date",
                ),
            )
        )


def _append_unused(
    records: list[dict[str, object]],
    unused: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if unused is None:
        return
    for index, candidate in enumerate(_mapping_items(unused.get("candidates"))):
        candidate_id = _first_text(candidate, "candidate_id", "id") or str(index)
        state = _first_text(candidate, "state") or "unknown"
        records.append(
            _record(
                record_type="unused_candidate",
                record_id=candidate_id,
                source_path=f"{source_path}.candidates[{index}]",
                providers=_provider_values(candidate),
                categories=("unused_analysis", state, _first_text(candidate, "kind") or ""),
                modules=_module_values(candidate),
                statuses=(state,),
                facts=_facts(
                    candidate,
                    "name",
                    "symbol",
                    "relative_path",
                    "start_line",
                    "end_line",
                    "evidence_total",
                ),
            )
        )


def _append_coverage(
    records: list[dict[str, object]],
    coverage: Mapping[str, object] | None,
    *,
    source_path: str,
) -> None:
    if coverage is None:
        return
    provider = _first_text(coverage, "provider_id") or "pytest-coverage-trusted-deep"
    records.append(
        _record(
            record_type="test_coverage",
            record_id=provider,
            source_path=source_path,
            providers=(provider,),
            categories=("test_coverage", "coverage"),
            statuses=_texts(coverage, "status", "suite_selection"),
            facts=_facts(
                coverage,
                "measurement_complete",
                "suite_selection",
                "measurement_scope_signature",
                "suite_signature",
                "reason",
            ),
        )
    )


def _extract_status(payload: Mapping[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    _append_provider_suite(
        records,
        _mapping(payload.get("external_evidence_suite")),
        source_path="external_evidence_suite",
    )
    _append_architecture(
        records,
        _mapping(payload.get("architecture")),
        source_path="architecture",
    )
    _append_engineering(
        records,
        _mapping(payload.get("engineering_analytics")),
        source_path="engineering_analytics",
    )
    _append_supply_chain(
        records,
        _mapping(payload.get("supply_chain")),
        source_path="supply_chain",
    )
    _append_unused(
        records,
        _mapping(payload.get("unused_analysis")),
        source_path="unused_analysis",
    )
    _append_coverage(
        records,
        _mapping(payload.get("test_coverage")),
        source_path="test_coverage",
    )
    return records


def _work_package_providers(item: Mapping[str, object]) -> tuple[str, ...]:
    providers: list[str] = []
    for gate in _mapping_items(item.get("supply_chain_gates")):
        providers.extend(_provider_values(gate))
    for observation in _mapping_items(item.get("supply_chain_observations")):
        providers.extend(_provider_values(observation))
    engineering = _mapping(item.get("engineering_profile"))
    if engineering is not None:
        for dimension_name in _ENGINEERING_DIMENSIONS:
            dimension = _mapping(engineering.get(dimension_name))
            if dimension is not None:
                providers.extend(_texts(dimension, "provenance"))
    return tuple(providers)


def _question_specs_by_identity(
    epistemics: Mapping[str, object] | None,
) -> dict[tuple[str, str], Mapping[str, object]]:
    if epistemics is None:
        return {}
    result: dict[tuple[str, str], Mapping[str, object]] = {}
    for spec in _mapping_items(epistemics.get("specs")):
        question_id = _first_text(spec, "question_id")
        version = _first_text(spec, "version")
        if question_id is None or version is None:
            raise ValueError("analysis question spec identity is incomplete")
        identity = (question_id, version)
        if identity in result:
            raise ValueError("analysis question spec identity is duplicated")
        result[identity] = spec
    return result


def _next_action_projection(
    spec: Mapping[str, object] | None,
) -> tuple[dict[str, str], ...]:
    if spec is None:
        return ()
    raw_actions = _mapping_items(spec.get("next_actions"))
    if len(raw_actions) > CODE_ANALYSIS_QUERY_MAX_ACTIONS:
        raise ValueError("analysis question next actions exceed their query bound")
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_actions:
        action_id = _first_text(item, "action_id")
        kind = _first_text(item, "kind")
        description = _first_text(item, "description")
        if action_id is None or kind is None or description is None:
            raise ValueError("analysis question next action is incomplete")
        if action_id in seen:
            raise ValueError("analysis question next action is duplicated")
        seen.add(action_id)
        actions.append(
            {
                "action_id": _bounded_text(action_id, limit=256),
                "kind": _bounded_text(kind, limit=64),
                "description": _bounded_text(description),
            }
        )
    return tuple(actions)


def _template_limitations(proposal: Mapping[str, object] | None) -> tuple[str, ...]:
    if proposal is None:
        return ()
    template_id = _first_text(proposal, "template_id")
    template_version = _first_text(proposal, "template_version")
    if template_id is None:
        if template_version is not None:
            raise ValueError("experiment proposal template identity is incomplete")
        return ()
    template = experiment_template(template_id)
    if template.version != template_version:
        raise ValueError("experiment proposal template version is not query-compatible")
    if len(template.limitations) > CODE_ANALYSIS_QUERY_MAX_TEMPLATE_LIMITATIONS:
        raise ValueError("experiment template limitations exceed their query bound")
    return tuple(_bounded_text(item) for item in template.limitations)


def _manual_reason(proposal: Mapping[str, object] | None) -> str | None:
    if proposal is None:
        return None
    planning_status = _first_text(proposal, "planning_status")
    runner_kind = _first_text(proposal, "runner_kind")
    if planning_status == "registry_gap":
        return "no_registered_experiment_template_for_any_next_action"
    if planning_status == "planned" and runner_kind in {None, "none"}:
        return "registered_template_has_no_allowlisted_runner"
    return None


def _plan_count(plan: Mapping[str, object], key: str) -> int:
    value = plan.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"experiment plan {key} is invalid")
    return value


def _experiment_plan_axes(plan: Mapping[str, object]) -> tuple[str, str, int]:
    status = _first_text(plan, "status")
    if status not in {"ready", "partial", "not_required", "abstained"}:
        raise ValueError("experiment plan status is invalid")
    required = _plan_count(plan, "experiment_required_count")
    planned = _plan_count(plan, "planned_count")
    executable = _plan_count(plan, "executable_count")
    gaps = _plan_count(plan, "registry_gap_count")
    if executable > planned or planned + gaps != required:
        raise ValueError("experiment plan counts cannot derive query readiness")
    expected_status = (
        "abstained"
        if status == "abstained"
        else "not_required"
        if required == 0
        else "partial"
        if gaps
        else "ready"
    )
    if status != expected_status or (status == "abstained" and required != 0):
        raise ValueError("experiment plan status cannot derive query readiness")
    manual = planned - executable
    if status == "abstained":
        return "abstained", "abstained", manual
    if required == 0:
        return "not_required", "not_required", manual
    planning_coverage = "partial" if gaps else "complete"
    execution_readiness = (
        "all_executable"
        if executable == required
        else "partially_executable"
        if executable
        else "manual_with_registry_gaps"
        if manual and gaps
        else "manual_only"
        if manual
        else "registry_gap_only"
    )
    return planning_coverage, execution_readiness, manual


def _append_experiment_plan_summary(
    records: list[dict[str, object]],
    plan: Mapping[str, object] | None,
) -> None:
    if plan is None:
        return
    planning_coverage, execution_readiness, manual_count = _experiment_plan_axes(plan)
    plan_status = _first_text(plan, "status") or "abstained"
    limitations = _string_items(plan.get("limitations"))
    if len(limitations) > CODE_ANALYSIS_QUERY_MAX_TEMPLATE_LIMITATIONS:
        raise ValueError("experiment plan limitations exceed their query bound")
    records.append(
        _record(
            record_type="experiment_plan_summary",
            record_id=_first_text(plan, "plan_id") or "current",
            source_path="experiment_plan",
            categories=("experiment_plan", "summary"),
            statuses=(
                f"experiment:{plan_status}",
                f"planning:{planning_coverage}",
                f"execution:{execution_readiness}",
            ),
            facts={
                **_facts(
                    plan,
                    "plan_id",
                    "status",
                    "reason",
                    "policy_id",
                    "registry_fingerprint",
                    "source_evaluation_count",
                    "experiment_required_count",
                    "planned_count",
                    "executable_count",
                    "registry_gap_count",
                    "authority",
                    "mutation_authority",
                ),
                "planning_coverage": planning_coverage,
                "execution_readiness": execution_readiness,
                "manual_count": manual_count,
                "limitations": [_bounded_text(item) for item in limitations],
            },
        )
    )


def _question_fact_projection(
    evaluation: Mapping[str, object],
    proposal: Mapping[str, object] | None,
    spec: Mapping[str, object] | None,
) -> dict[str, object]:
    """Expose the bounded evidence gap and its selected next experiment."""

    facts = {
        **_facts(
            evaluation,
            "evaluation_id",
            "question_id",
            "question_version",
            "question_spec_fingerprint",
            "rank",
            "observation_status",
            "inference_status",
            "question_readiness",
            "decision_readiness",
            "decision",
            "decision_reason",
            "counterevidence_status",
            "authority",
            "mutation_authority",
        ),
    }
    requirements = _mapping_items(evaluation.get("requirements"))
    requirement_states: list[str] = []
    requirement_reasons: list[str] = []
    missing: list[str] = []
    satisfied: list[str] = []
    contradicted: list[str] = []
    for requirement in requirements[:16]:
        requirement_id = _first_text(requirement, "requirement_id")
        status = _first_text(requirement, "status")
        reason = _first_text(requirement, "reason")
        if requirement_id is None or status is None:
            continue
        requirement_states.append(_bounded_text(f"{requirement_id}:{status}"))
        if reason is not None:
            requirement_reasons.append(_bounded_text(f"{requirement_id}:{reason}"))
        if status == "satisfied":
            satisfied.append(requirement_id)
        elif status == "contradicted":
            contradicted.append(requirement_id)
        else:
            missing.append(requirement_id)
    declared_actions = _next_action_projection(spec)
    action_by_id = {item["action_id"]: item for item in declared_actions}
    evaluation_action_ids = _string_items(evaluation.get("next_action_ids"))
    if any(action_id not in action_by_id for action_id in evaluation_action_ids):
        raise ValueError("analysis question action projection is inconsistent")
    actions = tuple(action_by_id[action_id] for action_id in evaluation_action_ids)
    facts.update(
        {
            "requirements": requirement_states,
            "requirement_reasons": requirement_reasons,
            "satisfied_requirement_ids": satisfied,
            "missing_requirement_ids": missing,
            "contradicted_requirement_ids": contradicted,
            "next_action_ids": [
                _bounded_text(item)
                for item in evaluation_action_ids[:CODE_ANALYSIS_QUERY_MAX_ACTIONS]
            ],
            "next_actions": list(actions),
            "hypotheses": [
                _bounded_text(item) for item in _string_items(evaluation.get("hypotheses"))[:8]
            ],
            "limitations": [
                _bounded_text(item) for item in _string_items(evaluation.get("limitations"))[:8]
            ],
            "evidence_count": len(_mapping_items(evaluation.get("evidence"))),
        }
    )
    if proposal is not None:
        runner_kind = _first_text(proposal, "runner_kind")
        facts.update(
            _facts(
                proposal,
                "proposal_id",
                "planning_status",
                "selected_action_id",
                "template_id",
                "template_version",
                "cost_tier",
                "estimated_attention_minutes",
                "timeout_seconds",
                "max_items",
                "isolation",
                "runner_kind",
                "reason",
            )
        )
        facts["proposal_executable"] = runner_kind not in {None, "none"}
        facts["scenario_ids"] = [
            _bounded_text(item) for item in _string_items(proposal.get("scenario_ids"))[:16]
        ]
        facts["acceptance_gates"] = [
            _bounded_text(item) for item in _string_items(proposal.get("acceptance_gates"))[:16]
        ]
        selected_action_id = _first_text(proposal, "selected_action_id")
        facts["selected_action"] = (
            None if selected_action_id is None else action_by_id.get(selected_action_id)
        )
        facts["template_limitations"] = list(_template_limitations(proposal))
        facts["manual_reason"] = _manual_reason(proposal)
    else:
        facts["proposal_executable"] = False
        facts["selected_action"] = None
        facts["template_limitations"] = []
        facts["manual_reason"] = None
    return facts


def _experiment_plan(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    if payload.get("schema") not in {
        _CODE_REVIEW_V16,
        _CODE_REVIEW_V17,
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }:
        return None
    return _mapping(payload.get("experiment_plan"))


def _experiment_proposals(
    plan: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], ...]:
    return () if plan is None else _mapping_items(plan.get("proposals"))


def _experiment_receipt_payloads(
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Return only the mandatory, bounded v17+ receipt envelope sequence."""

    if payload.get("schema") not in {
        _CODE_REVIEW_V17,
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }:
        return ()
    raw = payload.get("experiment_receipts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("code-review experiment receipts must be a sequence")
    if len(raw) > CODE_EXPERIMENT_STORE_MAX_RESOLVED:
        raise ValueError("code-review experiment receipts exceed their public bound")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ValueError("code-review experiment receipts contain a malformed envelope")
    return tuple(cast(Mapping[str, object], item) for item in raw)


def _append_experiment_receipts(
    records: list[dict[str, object]],
    payload: Mapping[str, object],
) -> None:
    for index, envelope in enumerate(_experiment_receipt_payloads(payload)):
        receipt = _mapping(envelope.get("receipt"))
        if receipt is None:
            raise ValueError("code-review experiment receipt lacks its public payload")
        question_id = _first_text(envelope, "question_id") or "question"
        receipt_id = _first_text(receipt, "receipt_id") or str(index)
        gate_outcomes = _mapping_items(receipt.get("gate_outcomes"))
        selected_scenarios = _string_items(receipt.get("selected_scenarios"))
        limitations = _string_items(receipt.get("limitations"))
        detail_limit = CODE_ANALYSIS_QUERY_MAX_RECEIPT_DETAILS
        gate_states = [
            _bounded_text(
                f"{_first_text(item, 'gate_id') or 'gate'}:"
                f"{_first_text(item, 'status') or 'unknown'}"
            )
            for item in gate_outcomes[:detail_limit]
        ]
        gate_reasons = [
            _bounded_text(
                f"{_first_text(item, 'gate_id') or 'gate'}:"
                f"{_first_text(item, 'reason') or 'unknown'}"
            )
            for item in gate_outcomes[:detail_limit]
        ]
        database_state = (
            "database:unchanged"
            if receipt.get("code_database_unchanged") is True
            else "database:changed"
        )
        records.append(
            _record(
                record_type="experiment_receipt",
                record_id=receipt_id,
                source_path=f"experiment_receipts[{index}]",
                providers=_texts(receipt, "provider_id"),
                categories=(
                    "experiment_receipt",
                    "experiment_plan",
                    f"experiment-question:{question_id}",
                    _first_text(receipt, "template_id") or "template",
                ),
                statuses=(
                    f"receipt:{_first_text(receipt, 'status') or 'unknown'}",
                    f"provider:{_first_text(receipt, 'provider_status') or 'unknown'}",
                    database_state,
                ),
                facts={
                    **_facts(
                        envelope,
                        "analysis_run_id",
                        "source_evaluation_id",
                        "question_id",
                        "subject_key",
                        "review_digest",
                        "recorded_ns",
                        "payload_xxh3_128",
                        "payload_xxh3_64_guard",
                        "payload_bytes",
                    ),
                    **_facts(
                        receipt,
                        "receipt_id",
                        "status",
                        "reason",
                        "policy_id",
                        "proposal_id",
                        "template_id",
                        "template_version",
                        "runner_kind",
                        "source_version",
                        "source_manifest_digest",
                        "code_database_digest_before",
                        "code_database_digest_after",
                        "code_database_unchanged",
                        "configuration_signature",
                        "scenario_registry_fingerprint",
                        "provider_id",
                        "provider_schema",
                        "provider_status",
                        "provider_execution",
                        "provider_input_signature",
                        "provider_result_digest",
                        "passed",
                        "failed",
                        "skipped",
                        "duration_ms",
                        "process_invocations",
                        "stdout_bytes",
                        "stderr_bytes",
                        "authority",
                        "mutation_authority",
                    ),
                    "selected_scenarios": [
                        _bounded_text(item) for item in selected_scenarios[:detail_limit]
                    ],
                    "selected_scenarios_count": len(selected_scenarios),
                    "selected_scenarios_truncated": len(selected_scenarios) > detail_limit,
                    "gate_states": gate_states,
                    "gate_reasons": gate_reasons,
                    "gate_outcomes_count": len(gate_outcomes),
                    "gate_outcomes_truncated": len(gate_outcomes) > detail_limit,
                    "limitations": [_bounded_text(item) for item in limitations[:detail_limit]],
                    "limitations_count": len(limitations),
                    "limitations_truncated": len(limitations) > detail_limit,
                },
            )
        )


def _append_technical_verification(
    records: list[dict[str, object]],
    payload: Mapping[str, object],
) -> None:
    if payload.get("schema") not in {
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }:
        return
    raw = _mapping(payload.get("technical_verification"))
    if raw is None:
        if payload.get("status") == "ready":
            raise ValueError("ready code-review/v18+ lacks technical verification")
        return
    verification = parse_code_technical_verification_payload(raw)
    records.append(
        _record(
            record_type="technical_verification_summary",
            record_id=verification.verification_id,
            source_path="technical_verification",
            categories=("technical_verification",),
            statuses=(f"technical:{verification.status}",),
            facts={
                "status": verification.status,
                "reason": verification.reason,
                "policy_id": verification.policy_id,
                "policy_fingerprint": verification.policy_fingerprint,
                "evidence_complete_evaluations": verification.evidence_complete_evaluations,
                "reviewed_count": verification.reviewed_count,
                "no_change_required_count": verification.no_change_required_count,
                "unresolved_count": verification.unresolved_count,
                "limitations": list(verification.limitations),
                "authority": verification.authority,
                "mutation_authority": verification.mutation_authority,
            },
        )
    )
    for index, item in enumerate(verification.reviews):
        records.append(
            _record(
                record_type="technical_disposition",
                record_id=item.review_id,
                source_path=f"technical_verification.reviews[{index}]",
                categories=(
                    "technical_verification",
                    "technical_disposition",
                    item.question_id,
                ),
                statuses=(f"technical:{item.disposition}",),
                facts={
                    **asdict(item),
                    "verified_requirement_count": len(item.verified_requirement_ids),
                    "evidence_count": len(item.evidence_ids),
                    "receipt_count": len(item.receipt_ids),
                },
            )
        )
    for index, gap in enumerate(verification.gaps):
        records.append(
            _record(
                record_type="technical_verification_gap",
                record_id=gap.evaluation_id,
                source_path=f"technical_verification.gaps[{index}]",
                categories=(
                    "technical_verification",
                    "technical_verification_gap",
                    gap.question_id,
                ),
                statuses=("technical:unresolved",),
                facts=asdict(gap),
            )
        )


def _extract_review(payload: Mapping[str, object]) -> list[dict[str, object]]:
    records = _extract_status(payload)
    plan = _experiment_plan(payload)
    _append_experiment_plan_summary(records, plan)
    _append_experiment_receipts(records, payload)
    _append_technical_verification(records, payload)
    proposals = _experiment_proposals(plan)
    proposal_by_evaluation = {
        evaluation_id: proposal
        for proposal in proposals
        if (evaluation_id := _first_text(proposal, "evaluation_id")) is not None
    }
    epistemics = _mapping(payload.get("epistemics"))
    specs_by_identity = _question_specs_by_identity(epistemics)
    evaluations = () if epistemics is None else _mapping_items(epistemics.get("evaluations"))
    evaluation_by_id = {
        evaluation_id: evaluation
        for evaluation in evaluations
        if (evaluation_id := _first_text(evaluation, "evaluation_id")) is not None
    }
    for index, finding in enumerate(_mapping_items(payload.get("findings"))):
        finding_id = _first_text(finding, "finding_id", "hotspot_id") or str(index)
        category = _first_text(finding, "category") or "finding"
        diagnostic_codes = [
            code
            for diagnostic in _mapping_items(finding.get("diagnostics"))
            for code in _texts(diagnostic, "code")
        ]
        epistemic = _mapping(finding.get("epistemic_state")) or {}
        epistemic_statuses = tuple(
            f"{prefix}:{value}"
            for prefix, field_name in (
                ("observation", "observation_status"),
                ("inference", "inference_status"),
                ("question", "question_readiness"),
                ("decision", "decision_readiness"),
            )
            for value in _texts(epistemic, field_name)
        )
        records.append(
            _record(
                record_type="review_finding",
                record_id=finding_id,
                source_path=f"findings[{index}]",
                providers=_provider_values(finding),
                categories=("finding", category, *diagnostic_codes),
                modules=_module_values(finding),
                statuses=(
                    *_texts(finding, "actionability", "observation_confidence", "change_risk"),
                    *epistemic_statuses,
                ),
                facts=_facts(
                    finding,
                    "symbol",
                    "path",
                    "rank",
                    "complexity",
                    "function_lines",
                    "actionability",
                    "observation_confidence",
                    "recommended_change",
                    "change_risk",
                ),
            )
        )
    if epistemics is not None:
        for index, evaluation in enumerate(evaluations):
            evaluation_id = _first_text(evaluation, "evaluation_id") or str(index)
            subject = _mapping(evaluation.get("subject")) or {}
            question_id = _first_text(evaluation, "question_id") or "question"
            question_version = _first_text(evaluation, "question_version")
            spec = (
                None
                if question_version is None
                else specs_by_identity.get((question_id, question_version))
            )
            subject_kind = _first_text(subject, "subject_kind") or "subject"
            location = _mapping(subject.get("location")) or {}
            evidence = _mapping_items(evaluation.get("evidence"))
            modules = _module_values(location)
            proposal = proposal_by_evaluation.get(evaluation_id)
            runner_kind = None if proposal is None else _first_text(proposal, "runner_kind")
            proposal_statuses = (
                ()
                if proposal is None
                else (
                    f"experiment:{_first_text(proposal, 'planning_status') or 'unknown'}",
                    (
                        "execution:executable"
                        if runner_kind not in {None, "none"}
                        else "execution:manual"
                    ),
                )
            )
            records.append(
                _record(
                    record_type="analysis_question",
                    record_id=evaluation_id,
                    source_path=f"epistemics.evaluations[{index}]",
                    providers=tuple(
                        provider
                        for item in evidence
                        for provider in _texts(item, "producer_id", "resolver_id")
                    ),
                    categories=("analysis_question", question_id, subject_kind),
                    modules=modules,
                    statuses=tuple(
                        f"{prefix}:{value}"
                        for prefix, field_name in (
                            ("observation", "observation_status"),
                            ("inference", "inference_status"),
                            ("question", "question_readiness"),
                            ("decision", "decision_readiness"),
                            ("counterevidence", "counterevidence_status"),
                        )
                        for value in _texts(evaluation, field_name)
                    )
                    + proposal_statuses,
                    facts={
                        **_question_fact_projection(evaluation, proposal, spec),
                        **_facts(
                            subject,
                            "subject_kind",
                            "subject_key",
                            "display_name",
                            "source_owner_id",
                            "snapshot_id",
                            "snapshot_freshness",
                            "revision_id",
                        ),
                    },
                )
            )
    for index, proposal in enumerate(proposals):
        proposal_id = _first_text(proposal, "proposal_id") or str(index)
        question_id = _first_text(proposal, "question_id") or "question"
        evaluation_id = _first_text(proposal, "evaluation_id")
        proposal_evaluation = None if evaluation_id is None else evaluation_by_id.get(evaluation_id)
        question_version = (
            None
            if proposal_evaluation is None
            else _first_text(proposal_evaluation, "question_version")
        )
        spec = (
            None
            if question_version is None
            else specs_by_identity.get((question_id, question_version))
        )
        actions = _next_action_projection(spec)
        action_by_id = {item["action_id"]: item for item in actions}
        selected_action_id = _first_text(proposal, "selected_action_id")
        selected_action = (
            None if selected_action_id is None else action_by_id.get(selected_action_id)
        )
        if selected_action_id is not None and selected_action is None:
            raise ValueError("experiment proposal selected action is not declared")
        alternative_action_ids = _string_items(proposal.get("alternative_action_ids"))
        alternative_actions = [
            action_by_id[action_id]
            for action_id in alternative_action_ids
            if action_id in action_by_id
        ]
        if actions and len(alternative_actions) != len(alternative_action_ids):
            raise ValueError("experiment proposal alternative action is not declared")
        planning_status = _first_text(proposal, "planning_status") or "unknown"
        runner_kind = _first_text(proposal, "runner_kind")
        executable = runner_kind not in {None, "none"}
        records.append(
            _record(
                record_type="experiment_proposal",
                record_id=proposal_id,
                source_path=f"experiment_plan.proposals[{index}]",
                categories=(
                    "experiment_plan",
                    f"experiment-question:{question_id}",
                    _first_text(proposal, "template_id") or "registry_gap",
                    _first_text(proposal, "selected_action_id") or "unregistered_action",
                ),
                statuses=(
                    f"experiment:{planning_status}",
                    "execution:executable" if executable else "execution:manual",
                ),
                facts={
                    **_facts(
                        proposal,
                        "proposal_id",
                        "evaluation_id",
                        "question_id",
                        "subject_key",
                        "selected_action_id",
                        "template_id",
                        "template_version",
                        "cost_tier",
                        "estimated_attention_minutes",
                        "timeout_seconds",
                        "max_items",
                        "isolation",
                        "runner_kind",
                        "planning_status",
                        "reason",
                        "authority",
                        "mutation_authority",
                    ),
                    "scenario_ids": [
                        _bounded_text(item)
                        for item in _string_items(proposal.get("scenario_ids"))[:16]
                    ],
                    "acceptance_gates": [
                        _bounded_text(item)
                        for item in _string_items(proposal.get("acceptance_gates"))[:16]
                    ],
                    "missing_requirement_ids": [
                        _bounded_text(item)
                        for item in _string_items(proposal.get("missing_requirement_ids"))[:16]
                    ],
                    "alternative_action_ids": [
                        _bounded_text(item)
                        for item in alternative_action_ids[:CODE_ANALYSIS_QUERY_MAX_ACTIONS]
                    ],
                    "selected_action": selected_action,
                    "alternative_actions": alternative_actions,
                    "template_limitations": list(_template_limitations(proposal)),
                    "manual_reason": _manual_reason(proposal),
                    "executable": executable,
                },
            )
        )
    parent_status = _first_text(payload, "work_package_status") or "unknown"
    for index, package in enumerate(_mapping_items(payload.get("work_packages"))):
        package_id = _first_text(package, "package_id") or str(index)
        package_kind = _first_text(package, "package_kind") or "work_package"
        package_names = (
            package_id,
            package_kind,
            *_texts(package, "title", "objective", "primary_symbol"),
        )
        categories = ["work_package", package_kind]
        engineering = _mapping(package.get("engineering_profile"))
        if engineering is not None:
            categories.extend(
                name
                for name in _ENGINEERING_DIMENSIONS
                if _mapping(engineering.get(name)) is not None
            )
        statuses = [parent_status]
        for gate in _mapping_items(package.get("engineering_gates")):
            statuses.extend(_texts(gate, "status"))
        for gate in _mapping_items(package.get("supply_chain_gates")):
            statuses.extend(_texts(gate, "status"))
        records.append(
            _record(
                record_type="work_package",
                record_id=package_id,
                source_path=f"work_packages[{index}]",
                providers=_work_package_providers(package),
                categories=categories,
                modules=_module_values(package),
                statuses=statuses,
                work_packages=package_names,
                facts=_facts(
                    package,
                    "package_kind",
                    "package_rank",
                    "title",
                    "objective",
                    "primary_module",
                    "primary_symbol",
                    "change_risk",
                    "confidence",
                    "requires_human_confirmation",
                    "members_truncated",
                ),
            )
        )
    return records


def _append_diff_examples(
    records: list[dict[str, object]],
    value: object,
    *,
    source_path: str,
    record_type: str,
    category: str,
    delta: str,
) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return
    for index, raw in enumerate(value):
        modules: tuple[str, ...]
        if isinstance(raw, str):
            item: Mapping[str, object] = {"value": raw}
            item_id = raw
            parsed_module = _path_module(raw.split("::", 1)[0])
            modules = (parsed_module,) if parsed_module is not None else ()
        elif isinstance(raw, Mapping):
            item = raw
            item_id = _first_text(
                item,
                "finding_id",
                "candidate_id",
                "observation_id",
                "id",
                "symbol",
                "path",
                "name",
            ) or str(index)
            modules = _module_values(item)
        else:
            continue
        records.append(
            _record(
                record_type=record_type,
                record_id=item_id,
                source_path=f"{source_path}[{index}]",
                providers=_provider_values(item),
                categories=(category, record_type),
                modules=modules,
                statuses=_texts(item, "status", "baseline_state", "current_state"),
                deltas=(delta, *_texts(item, "change")),
                facts=_facts(
                    item,
                    "value",
                    "symbol",
                    "path",
                    "name",
                    "line",
                    "baseline_state",
                    "current_state",
                    "baseline_target",
                    "current_target",
                ),
            )
        )


def _append_provider_relocations(
    records: list[dict[str, object]],
    provider: Mapping[str, object],
    *,
    provider_index: int,
    provider_id: str,
) -> None:
    for relocation_index, relocation in enumerate(
        _mapping_items(provider.get("relocation_examples"))
    ):
        relocation_id = _first_text(
            relocation,
            "current_finding_id",
            "baseline_finding_id",
        ) or str(relocation_index)
        records.append(
            _record(
                record_type="provider_finding_relocation",
                record_id=relocation_id,
                source_path=(
                    f"providers[{provider_index}].relocation_examples[{relocation_index}]"
                ),
                providers=(provider_id,),
                categories=(
                    "finding_relocation",
                    _first_text(relocation, "category") or "finding",
                ),
                modules=_module_values(relocation),
                statuses=("ready",),
                deltas=("relocated",),
                facts=_facts(
                    relocation,
                    "baseline_finding_id",
                    "current_finding_id",
                    "path",
                    "category",
                    "code",
                    "severity",
                    "message",
                    "baseline_start_line",
                    "baseline_start_column",
                    "baseline_end_line",
                    "baseline_end_column",
                    "current_start_line",
                    "current_start_column",
                    "current_end_line",
                    "current_end_column",
                ),
            )
        )


def _append_provider_diffs(
    records: list[dict[str, object]],
    value: object,
) -> None:
    for index, provider in enumerate(_mapping_items(value)):
        provider_id = _first_text(provider, "provider_id") or str(index)
        baseline = _mapping(provider.get("baseline"))
        current = _mapping(provider.get("current"))
        statuses = list(_texts(provider, "status", "gate"))
        if baseline is not None:
            statuses.extend(_texts(baseline, "status", "gate"))
        if current is not None:
            statuses.extend(_texts(current, "status", "gate"))
        records.append(
            _record(
                record_type="provider_delta",
                record_id=provider_id,
                source_path=f"providers[{index}]",
                providers=(provider_id,),
                categories=("provider_delta", "external_provider"),
                statuses=statuses,
                deltas=_delta_words(provider, "added", "resolved", "relocated"),
                facts=_facts(
                    provider,
                    "common",
                    "added",
                    "resolved",
                    "relocated",
                    "gate",
                    "reason",
                ),
            )
        )
        _append_provider_relocations(
            records,
            provider,
            provider_index=index,
            provider_id=provider_id,
        )


def _append_architecture_diffs(
    records: list[dict[str, object]],
    value: object,
) -> None:
    architecture = _mapping(value)
    if architecture is None:
        return
    for index, module in enumerate(_mapping_items(architecture.get("modules"))):
        module_id = _first_text(module, "module_id", "module") or str(index)
        records.append(
            _record(
                record_type="architecture_module_delta",
                record_id=module_id,
                source_path=f"architecture.modules[{index}]",
                categories=("architecture", "module"),
                modules=_module_values(module),
                statuses=_texts(module, "status"),
                deltas=_delta_words(
                    module,
                    "complexity_delta",
                    "fan_in_delta",
                    "fan_out_delta",
                    "blast_radius_delta",
                ),
                facts=_facts(
                    module,
                    "complexity_delta",
                    "fan_in_delta",
                    "fan_out_delta",
                    "blast_radius_delta",
                    "reason",
                ),
            )
        )


def _append_hotspot_diffs(
    records: list[dict[str, object]],
    value: object,
) -> None:
    hotspots = _mapping(value)
    if hotspots is None:
        return
    for key, delta in (
        ("added_examples", "added"),
        ("removed_examples", "removed"),
        ("changed_examples", "changed"),
    ):
        _append_diff_examples(
            records,
            hotspots.get(key),
            source_path=f"hotspots.{key}",
            record_type="hotspot_delta",
            category="hotspot",
            delta=delta,
        )


def _append_supply_chain_diffs(
    records: list[dict[str, object]],
    value: object,
) -> None:
    supply = _mapping(value)
    if supply is None:
        return
    for index, category in enumerate(_mapping_items(supply.get("categories"))):
        category_id = _first_text(category, "category") or str(index)
        records.append(
            _record(
                record_type="supply_chain_category_delta",
                record_id=category_id,
                source_path=f"supply_chain.categories[{index}]",
                categories=("supply_chain", category_id),
                statuses=_texts(supply, "status", "current_status"),
                deltas=_delta_words(category, "delta"),
                facts=_facts(category, "baseline", "current", "delta"),
            )
        )
    for index, provider in enumerate(_mapping_items(supply.get("providers"))):
        provider_id = _first_text(provider, "provider_id") or str(index)
        records.append(
            _record(
                record_type="supply_chain_provider_delta",
                record_id=provider_id,
                source_path=f"supply_chain.providers[{index}]",
                providers=(provider_id,),
                categories=("supply_chain", "provider_delta"),
                statuses=_texts(provider, "baseline_status", "current_status"),
                deltas=_delta_words(
                    provider,
                    "findings_delta",
                    "metrics_delta",
                    "relations_delta",
                ),
                facts=_facts(
                    provider,
                    "baseline_status",
                    "current_status",
                    "findings_delta",
                    "metrics_delta",
                    "relations_delta",
                ),
            )
        )


def _append_coverage_diff(
    records: list[dict[str, object]],
    value: object,
) -> None:
    coverage = _mapping(value)
    if coverage is None:
        return
    records.append(
        _record(
            record_type="coverage_delta",
            record_id="pytest-coverage-trusted-deep",
            source_path="test_coverage",
            providers=("pytest-coverage-trusted-deep",),
            categories=("test_coverage", "coverage"),
            statuses=_texts(coverage, "status"),
            deltas=_delta_words(
                coverage,
                "line_coverage_percent_delta",
                "branch_coverage_percent_delta",
                "covered_lines_delta",
                "covered_branch_exits_delta",
            ),
            facts=_facts(
                coverage,
                "line_coverage_percent_delta",
                "branch_coverage_percent_delta",
                "covered_lines_delta",
                "missing_lines_delta",
                "covered_branch_exits_delta",
                "missing_branch_exits_delta",
                "reason",
            ),
        )
    )


def _append_engineering_diffs(
    records: list[dict[str, object]],
    value: object,
) -> None:
    engineering = _mapping(value)
    if engineering is None:
        return
    for index, dimension in enumerate(_mapping_items(engineering.get("dimensions"))):
        dimension_id = _first_text(dimension, "dimension") or str(index)
        records.append(
            _record(
                record_type="engineering_dimension_delta",
                record_id=dimension_id,
                source_path=f"engineering_analytics.dimensions[{index}]",
                categories=("engineering", dimension_id),
                statuses=_texts(dimension, "baseline_status", "current_status"),
                deltas=_delta_words(dimension),
                facts=_facts(
                    dimension,
                    "baseline_status",
                    "current_status",
                    "baseline_reason",
                    "current_reason",
                ),
            )
        )


def _extract_diff(payload: Mapping[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    _append_provider_diffs(records, payload.get("providers"))
    _append_architecture_diffs(records, payload.get("architecture"))
    _append_hotspot_diffs(records, payload.get("hotspots"))
    _append_supply_chain_diffs(records, payload.get("supply_chain"))
    _append_coverage_diff(records, payload.get("test_coverage"))
    _append_engineering_diffs(records, payload.get("engineering_analytics"))
    return records


def _source_schema(payload: Mapping[str, object], surface: str) -> str:
    schema = payload.get("schema")
    if isinstance(schema, str) and schema.strip():
        return schema.strip()
    version = payload.get("schema_version")
    if surface == "status" and isinstance(version, int):
        return f"neocortex.code-status/schema-v{version}"
    return f"neocortex.code-{surface}/unknown"


def _source_digest(payload: Mapping[str, object]) -> str | None:
    digest = _mapping(payload.get("digest"))
    if digest is not None:
        value = _first_text(digest, "xxh3_128", "xxh3_64_guard")
        if value:
            return value
    latest = _mapping(payload.get("latest_run"))
    if latest is not None:
        return _first_text(latest, "processing_signature", "analysis_run_id")
    return None


def _source_status(payload: Mapping[str, object], surface: str) -> str:
    if surface == "status":
        if payload.get("exists") is False:
            return "abstained"
        latest = _mapping(payload.get("latest_run"))
        if latest is not None:
            status = _first_text(latest, "status")
            if status and status.casefold() not in {"completed", "ready"}:
                return "abstained"
        self_analysis = _mapping(payload.get("self_analysis"))
        freshness = None if self_analysis is None else _mapping(self_analysis.get("freshness"))
        if (
            self_analysis is None
            or self_analysis.get("manifest_status") != "valid"
            or freshness is None
            or freshness.get("current") is not True
        ):
            return "abstained"
        suite = _mapping(payload.get("external_evidence_suite"))
        if suite is None or _first_text(suite, "status") not in {"ready", "completed"}:
            return "abstained"
        return "ready"
    status = _first_text(payload, "status")
    return "ready" if status and status.casefold() == "ready" else "abstained"


def _record_matches(record: Mapping[str, object], query: CodeAnalysisQuery) -> bool:
    dimensions = _mapping(record.get("dimensions"))
    if dimensions is None:
        return False
    for name in _DIMENSIONS:
        requested = getattr(query, name)
        if not requested:
            continue
        available = tuple(value.casefold() for value in _string_items(dimensions.get(name)))
        if name == "modules":
            if not any(
                value == expected or value.startswith(expected + ".")
                for expected in requested
                for value in available
            ):
                return False
        elif not set(requested).intersection(available):
            return False
    return True


def _source_limitations(payload: Mapping[str, object], status: str) -> list[str]:
    limitations = list(_string_items(payload.get("limitations"))[:20])
    if status != "ready":
        reason = _first_text(payload, "reason") or "source_publication_not_ready"
        limitations.append(reason)
        if payload.get("kind") == "code-status":
            self_analysis = _mapping(payload.get("self_analysis"))
            if self_analysis is None:
                limitations.append("self_analysis_evidence_missing")
            else:
                manifest = _first_text(self_analysis, "manifest_status") or "missing"
                if manifest != "valid":
                    limitations.append(f"self_analysis_manifest_{manifest}")
                freshness = _mapping(self_analysis.get("freshness"))
                if freshness is None or freshness.get("current") is not True:
                    limitations.append("self_analysis_freshness_not_current")
            suite = _mapping(payload.get("external_evidence_suite"))
            suite_status = (
                "missing" if suite is None else (_first_text(suite, "status") or "missing")
            )
            if suite_status not in {"ready", "completed"}:
                limitations.append(f"external_evidence_suite_status_{suite_status}")
    limitations.append("explicit_public_projection_only")
    if payload.get("schema") in {_CODE_REVIEW_V12, _CODE_REVIEW_V13, _CODE_REVIEW_V14}:
        limitations.append("query_adapter_does_not_reopen_source_records")
    return _dimension_values(limitations)


def _validate_review_v11_payload(payload: Mapping[str, object]) -> None:
    """Reject mappings that claim v11 while violating its fail-closed wire contract."""

    status = payload.get("status")
    if status not in {"ready", "abstained"}:
        raise ValueError("code-review/v11 status is invalid")
    findings = _mapping_items(payload.get("findings"))
    recommendations = _mapping_items(payload.get("recommendations"))
    work_packages = _mapping_items(payload.get("work_packages"))
    recommendation_status = payload.get("recommendation_status")
    if recommendation_status not in {"abstained", "not_evaluated"}:
        raise ValueError("code-review/v11 recommendation status is invalid")
    if recommendations:
        raise ValueError("code-review/v11 cannot contain semantic change recommendations")
    if not _first_text(payload, "recommendation_reason"):
        raise ValueError("code-review/v11 recommendation abstention requires a reason")
    work_package_status = payload.get("work_package_status", "abstained")
    if work_package_status not in {"ready", "abstained", "not_evaluated"}:
        raise ValueError("code-review/v11 work-package status is invalid")
    if (work_package_status == "ready") != bool(work_packages):
        raise ValueError("code-review/v11 work-package readiness is inconsistent")
    work_package_reason = _first_text(payload, "work_package_reason")
    if (work_package_status == "ready") == (work_package_reason is not None):
        raise ValueError("code-review/v11 work-package reason is inconsistent")
    if status == "abstained":
        asserted_envelopes = (
            "snapshot",
            "coverage",
            "digest",
            "external_evidence",
            "external_evidence_suite",
            "architecture",
            "test_coverage",
            "unused_analysis",
            "supply_chain",
            "engineering_analytics",
        )
        if (
            not _first_text(payload, "reason")
            or findings
            or work_packages
            or any(payload.get(name) is not None for name in asserted_envelopes)
        ):
            raise ValueError("abstained code-review/v11 payload contains asserted evidence")
        return
    if _first_text(payload, "reason") is not None:
        raise ValueError("ready code-review/v11 payload carries an abstention reason")
    for required in ("snapshot", "coverage", "digest"):
        if _mapping(payload.get(required)) is None:
            raise ValueError(f"ready code-review/v11 payload lacks {required}")
    for finding in findings:
        epistemic = _mapping(finding.get("epistemic_state"))
        if (
            finding.get("recommended_change") is not False
            or finding.get("construction") != "unknown"
            or finding.get("change_risk") != "unknown"
            or finding.get("actionability") != "characterize_first"
            or epistemic is None
            or epistemic.get("observation_status") != "confirmed"
            or epistemic.get("inference_status") != "abstained"
            or epistemic.get("question_readiness") != "ready"
            or epistemic.get("decision_readiness") != "experiment_required"
            or epistemic.get("decision") is not None
            or epistemic.get("authority") != "advisory"
            or epistemic.get("mutation_authority") is not False
            or not _mapping_items(finding.get("diagnostics"))
        ):
            raise ValueError("code-review/v11 finding violates its observation-only contract")
    for package in work_packages:
        steps = _mapping_items(package.get("steps"))
        requirements = tuple(_first_text(step, "requirement") for step in steps)
        candidates = _mapping_items(package.get("unused_candidates"))
        candidate = candidates[0] if len(candidates) == 1 else None
        candidate_id = None if candidate is None else _first_text(candidate, "candidate_id")
        target = None if candidate is None else _first_text(candidate, "symbol", "name")
        if (
            package.get("package_kind") != "unused_characterization"
            or package.get("objective")
            != "characterize_high_consensus_unused_candidate_without_mutation"
            or package.get("change_risk") != "unknown"
            or _mapping_items(package.get("members"))
            or package.get("requires_human_confirmation") is not True
            or package.get("mutation_authority") is not False
            or package.get("confidence") != "unused_high_consensus_advisory"
            or candidate is None
            or candidate.get("state") != "probable_unused_high_consensus"
            or candidate.get("authority") != "advisory"
            or candidate.get("mutation_authority") is not False
            or not candidate_id
            or not target
            or _first_text(package, "primary_finding_id") != candidate_id
            or _first_text(package, "primary_hotspot_id") != candidate_id
            or _first_text(package, "primary_symbol") != target
            or _first_text(package, "title") != f"{target} unused-code characterization"
            or tuple(_first_text(step, "phase") for step in steps) != ("characterize",) * 4
            or requirements != _UNUSED_V11_STEP_REQUIREMENTS
        ):
            raise ValueError("code-review/v11 work package violates characterization-only policy")


def _review_v12_revision_id(finding: Mapping[str, object]) -> str | None:
    raw = _first_text(finding, "file_xxh3_128")
    guard = _first_text(finding, "file_xxh3_64_guard")
    if raw is None and guard is None:
        return None
    if raw is None or guard is None:
        raise ValueError("code-review/v12 finding revision identity is incomplete")
    return f"xxh3_128:{raw}:xxh3_64_guard:{guard}"


def _review_v12_expected_evaluation(
    finding: Mapping[str, object],
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    finding_id = _first_text(finding, "finding_id")
    hotspot_id = _first_text(finding, "hotspot_id")
    symbol = _first_text(finding, "symbol")
    path = _first_text(finding, "path")
    snapshot_id = _first_text(snapshot, "processing_signature")
    freshness = _first_text(snapshot, "freshness")
    rank = finding.get("rank")
    location_values = tuple(
        finding.get(name) for name in ("start_line", "end_line", "start_column", "end_column")
    )
    if (
        not finding_id
        or not hotspot_id
        or not symbol
        or not path
        or not snapshot_id
        or freshness not in {"current", "publication_only", "unknown"}
        or isinstance(rank, bool)
        or not isinstance(rank, int)
        or rank < 1
        or any(isinstance(value, bool) or not isinstance(value, int) for value in location_values)
    ):
        raise ValueError("code-review/v12 finding cannot identify its question subject")
    revision_id = _review_v12_revision_id(finding)
    evidence: list[dict[str, object]] = []
    evidence_ids: list[str] = []
    for diagnostic in _mapping_items(finding.get("diagnostics")):
        diagnostic_id = diagnostic.get("diagnostic_id")
        code = _first_text(diagnostic, "code")
        source = _first_text(diagnostic, "source")
        tool_name = _first_text(diagnostic, "tool_name")
        tool_version = _first_text(diagnostic, "tool_version")
        value = diagnostic.get("value")
        threshold = diagnostic.get("threshold")
        confirmed = diagnostic.get("confirmed")
        confidence = diagnostic.get("confidence")
        if (
            isinstance(diagnostic_id, bool)
            or not isinstance(diagnostic_id, int)
            or diagnostic_id < 1
            or code not in {"high_complexity", "long_function"}
            or not source
            or not tool_name
            or not tool_version
            or isinstance(value, bool)
            or not isinstance(value, int)
            or isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or confirmed is not True
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
        ):
            raise ValueError("code-review/v12 diagnostic evidence is malformed")
        source_projection = {
            "snapshot_id": snapshot_id,
            "revision_id": revision_id,
            "path": path,
            "start_line": location_values[0],
            "end_line": location_values[1],
            "start_column": location_values[2],
            "end_column": location_values[3],
            "code": code,
            "value": value,
            "threshold": threshold,
            "source": source,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "confirmed": confirmed,
            "reported_confidence": confidence,
        }
        projection_digest = analysis_identity(
            "code-source-projection-v1",
            source_projection,
        )
        evidence_id = analysis_identity(
            "code-diagnostic-evidence-v1",
            {
                "subject_key": hotspot_id,
                "source_projection_digest": projection_digest,
                "code": code,
            },
        )
        evidence_ids.append(evidence_id)
        evidence.append(
            {
                "evidence_id": evidence_id,
                "subject_key": hotspot_id,
                "role": "supporting",
                "evidence_kind": "internal_diagnostic",
                "source_owner_id": "code",
                "producer_id": tool_name,
                "producer_version": tool_version,
                "source_schema": f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
                "source_record_kind": "diagnostic",
                "source_record_id": str(diagnostic_id),
                "source_projection_digest": projection_digest,
                "snapshot_id": snapshot_id,
                "revision_id": revision_id,
                "facts": (
                    {"name": "code", "value": code, "unit": None},
                    {"name": "value", "value": value, "unit": None},
                    {"name": "threshold", "value": threshold, "unit": None},
                    {"name": "confirmed", "value": True, "unit": None},
                    {
                        "name": "reported_confidence",
                        "value": confidence,
                        "unit": "ratio",
                    },
                ),
                "completeness": "complete",
                "bounded": False,
                "truncated": False,
                "resolver_id": "code.sqlite-diagnostic-resolver",
                "resolver_version": "v1",
                "resolution_status": "resolved",
                "limitations": ("diagnostic_confirms_threshold_only",),
                "provider_run_id": None,
                "authority": "advisory",
                "mutation_authority": False,
            }
        )
    if not evidence:
        raise ValueError("code-review/v12 finding lacks diagnostic evidence")
    spec_fingerprint = analysis_question_spec_fingerprint(STRUCTURAL_HOTSPOT_QUESTION)
    return {
        "evaluation_id": analysis_identity(
            "code-question-evaluation-v1",
            {
                "finding_id": finding_id,
                "snapshot": snapshot_id,
                "question_spec": spec_fingerprint,
                "evidence_ids": tuple(evidence_ids),
            },
        ),
        "question_id": STRUCTURAL_HOTSPOT_QUESTION.question_id,
        "question_version": STRUCTURAL_HOTSPOT_QUESTION.version,
        "question_spec_fingerprint": spec_fingerprint,
        "rank": rank,
        "subject": {
            "subject_kind": "symbol",
            "subject_key": hotspot_id,
            "display_name": symbol,
            "source_owner_id": "code",
            "snapshot_id": snapshot_id,
            "snapshot_freshness": freshness,
            "revision_id": revision_id,
            "location": {
                "path": path,
                "start_line": location_values[0],
                "end_line": location_values[1],
                "start_column": location_values[2],
                "end_column": location_values[3],
            },
        },
        "evidence": tuple(evidence),
        "requirements": (
            {
                "requirement_id": "confirmed_structural_hotspot",
                "status": "satisfied",
                "evidence_ids": tuple(evidence_ids),
                "reason": "linked_confirmed_threshold_diagnostics",
            },
            {
                "requirement_id": "behavior_or_contract_problem_observed",
                "status": "missing",
                "evidence_ids": (),
                "reason": "no_behavior_or_contract_problem_evidence_linked",
            },
            {
                "requirement_id": "counterevidence_evaluated",
                "status": "not_evaluated",
                "evidence_ids": (),
                "reason": "counterevidence_not_evaluated",
            },
            {
                "requirement_id": "discriminating_experiment_result",
                "status": "missing",
                "evidence_ids": (),
                "reason": "no_discriminating_experiment_result_linked",
            },
        ),
        "observation_status": "confirmed",
        "inference_status": "abstained",
        "inferences": (),
        "hypotheses": STRUCTURAL_HOTSPOT_QUESTION.hypotheses,
        "question_readiness": "ready",
        "decision_readiness": "experiment_required",
        "decision": None,
        "decision_reason": "decision_evidence_incomplete",
        "counterevidence_status": "not_evaluated",
        "next_action_ids": tuple(
            item.action_id for item in STRUCTURAL_HOTSPOT_QUESTION.next_actions
        ),
        "limitations": (
            "structural_threshold_does_not_prove_maintenance_harm",
            "source_record_projection_is_resolved_but_semantics_are_not",
            "human_decision_not_owned_by_code_analysis",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }


def _validate_review_v12_payload(payload: Mapping[str, object]) -> None:
    """Validate v12's linked, observation-only epistemic wire projection."""

    _validate_review_v11_payload(payload)
    if payload.get("status") == "abstained":
        epistemics = _mapping(payload.get("epistemics"))
        if epistemics is None or epistemics.get("schema") != _CODE_ANALYSIS_EPISTEMICS_V1:
            raise ValueError("abstained code-review/v12 payload lacks its empty epistemic envelope")
        if _mapping_items(epistemics.get("specs")) or _mapping_items(epistemics.get("evaluations")):
            raise ValueError("abstained code-review/v12 payload asserts epistemic evidence")
        return
    findings = _mapping_items(payload.get("findings"))
    epistemics = _mapping(payload.get("epistemics"))
    if epistemics is None or epistemics.get("schema") != _CODE_ANALYSIS_EPISTEMICS_V1:
        raise ValueError("ready code-review/v12 payload lacks its epistemic contract")
    specs = _mapping_items(epistemics.get("specs"))
    evaluations = _mapping_items(epistemics.get("evaluations"))
    expected_spec = {
        **asdict(STRUCTURAL_HOTSPOT_QUESTION),
        "spec_fingerprint": analysis_question_spec_fingerprint(STRUCTURAL_HOTSPOT_QUESTION),
    }
    if bool(findings) != bool(specs) or len(specs) > 1 or len(evaluations) != len(findings):
        raise ValueError("code-review/v12 epistemic coverage is incomplete")
    try:
        if specs and canonical_json(dict(specs[0])) != canonical_json(expected_spec):
            raise ValueError("code-review/v12 question spec is not canonical")
        snapshot = _mapping(payload.get("snapshot"))
        if snapshot is None:
            raise ValueError("code-review/v12 snapshot is missing")
        expected_evaluations = tuple(
            _review_v12_expected_evaluation(finding, snapshot) for finding in findings
        )
        if canonical_json(list(evaluations)) != canonical_json(expected_evaluations):
            raise ValueError("code-review/v12 epistemic projection is not source-linked")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("code-review/v12"):
            raise
        raise ValueError("code-review/v12 epistemic projection is malformed") from exc


def _validate_review_v13_payload(payload: Mapping[str, object]) -> None:
    """Validate v13 class surfaces and their complete question projection."""

    _validate_review_v11_payload(payload)
    epistemics = _mapping(payload.get("epistemics"))
    if epistemics is None or epistemics.get("schema") != _CODE_ANALYSIS_EPISTEMICS_V1:
        raise ValueError("code-review/v13 payload lacks its epistemic contract")
    if payload.get("status") == "abstained":
        if payload.get("structural_analysis") is not None:
            raise ValueError("abstained code-review/v13 payload asserts structural evidence")
        if _mapping_items(epistemics.get("specs")) or _mapping_items(epistemics.get("evaluations")):
            raise ValueError("abstained code-review/v13 payload asserts epistemic evidence")
        return
    findings = _mapping_items(payload.get("findings"))
    structural_payload = _mapping(payload.get("structural_analysis"))
    snapshot = _mapping(payload.get("snapshot"))
    if structural_payload is None or snapshot is None:
        raise ValueError("ready code-review/v13 payload lacks resolved structural evidence")
    try:
        structural = parse_code_class_surface_payload(structural_payload)
        snapshot_id = _first_text(snapshot, "processing_signature")
        snapshot_freshness = _first_text(snapshot, "freshness")
        if (
            not snapshot_id
            or not snapshot_freshness
            or structural.snapshot_id != snapshot_id
            or structural.snapshot_freshness != snapshot_freshness
        ):
            raise ValueError("code-review/v13 structural snapshot is inconsistent")
        hotspot_evaluations = tuple(
            _review_v12_expected_evaluation(finding, snapshot) for finding in findings
        )
        hotspot_specs = (
            (
                {
                    **asdict(STRUCTURAL_HOTSPOT_QUESTION),
                    "spec_fingerprint": analysis_question_spec_fingerprint(
                        STRUCTURAL_HOTSPOT_QUESTION
                    ),
                },
            )
            if hotspot_evaluations
            else ()
        )
        class_specs, class_evaluations = expected_class_surface_questions(
            structural,
            rank_offset=len(hotspot_evaluations),
        )
        expected = {
            "schema": _CODE_ANALYSIS_EPISTEMICS_V1,
            "specs": [
                *hotspot_specs,
                *(
                    {
                        **asdict(spec),
                        "spec_fingerprint": analysis_question_spec_fingerprint(spec),
                    }
                    for spec in class_specs
                ),
            ],
            "evaluations": [
                *hotspot_evaluations,
                *(asdict(evaluation) for evaluation in class_evaluations),
            ],
        }
        if canonical_json(dict(epistemics)) != canonical_json(expected):
            raise ValueError("code-review/v13 epistemic projection is not source-linked")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("code-review/v13"):
            raise
        raise ValueError("code-review/v13 structural projection is malformed") from exc


def _validate_review_v14_payload(payload: Mapping[str, object]) -> None:
    """Validate v14's structural wire plus bounded state-projection receipt."""

    projected = dict(payload)
    projected["schema"] = _CODE_REVIEW_V13
    projected.pop("state_projection", None)
    _validate_review_v13_payload(projected)
    state_projection = _mapping(payload.get("state_projection"))
    if payload.get("status") == "abstained":
        if state_projection is not None:
            raise ValueError("abstained code-review/v14 payload asserts a state projection")
        return
    if state_projection is None:
        raise ValueError("ready code-review/v14 payload lacks a state projection")
    try:
        parse_code_state_projection_payload(state_projection)
    except (TypeError, ValueError) as exc:
        raise ValueError("code-review/v14 state projection is malformed") from exc


def _validate_review_v15_payload(payload: Mapping[str, object]) -> None:
    """Validate every integrated v15 projection and its canonical question suffix."""

    projected = dict(payload)
    projected["schema"] = _CODE_REVIEW_V14
    for key in (
        "state_topology",
        "change_evolution",
        "assurance",
        "capability_reachability",
        "analyzer_effectiveness",
        "interface_surface",
    ):
        projected.pop(key, None)
    epistemics = _mapping(payload.get("epistemics"))
    if epistemics is None:
        raise ValueError("code-review/v15 payload lacks its epistemic contract")
    if payload.get("status") == "abstained":
        for key in (
            "state_topology",
            "change_evolution",
            "assurance",
            "capability_reachability",
            "analyzer_effectiveness",
            "interface_surface",
        ):
            if payload.get(key) is not None:
                raise ValueError("abstained code-review/v15 payload asserts integrated evidence")
        _validate_review_v14_payload(projected)
        return
    try:
        specs, evaluations = parse_analysis_questions_payload(epistemics)
        findings = _mapping_items(payload.get("findings"))
        structural_payload = _mapping(payload.get("structural_analysis"))
        snapshot = _mapping(payload.get("snapshot"))
        if structural_payload is None or snapshot is None:
            raise ValueError("ready code-review/v15 payload lacks structural snapshot evidence")
        structural = parse_code_class_surface_payload(structural_payload)
        class_specs, class_evaluations = expected_class_surface_questions(
            structural,
            rank_offset=len(findings),
        )
        base_spec_count = (1 if findings else 0) + len(class_specs)
        base_evaluation_count = len(findings) + len(class_evaluations)
        projected["epistemics"] = analysis_questions_payload(
            specs[:base_spec_count],
            evaluations[:base_evaluation_count],
        )
        _validate_review_v14_payload(projected)

        state_topology_payload = _mapping(payload.get("state_topology"))
        state_projection_payload = _mapping(payload.get("state_projection"))
        architecture_payload = _mapping(payload.get("architecture"))
        change_evolution_payload = _mapping(payload.get("change_evolution"))
        assurance_payload = _mapping(payload.get("assurance"))
        capability_payload = _mapping(payload.get("capability_reachability"))
        analyzer_effectiveness_payload = _mapping(payload.get("analyzer_effectiveness"))
        supply_chain_payload = _mapping(payload.get("supply_chain"))
        interface_surface_payload = _mapping(payload.get("interface_surface"))
        if any(
            item is None
            for item in (
                state_topology_payload,
                state_projection_payload,
                architecture_payload,
                change_evolution_payload,
                assurance_payload,
                capability_payload,
                analyzer_effectiveness_payload,
                supply_chain_payload,
                interface_surface_payload,
            )
        ):
            raise ValueError("ready code-review/v15 payload lacks integrated evidence")
        assert state_topology_payload is not None
        assert state_projection_payload is not None
        assert architecture_payload is not None
        assert change_evolution_payload is not None
        assert assurance_payload is not None
        assert capability_payload is not None
        assert analyzer_effectiveness_payload is not None
        assert supply_chain_payload is not None
        assert interface_surface_payload is not None
        state_topology = parse_code_state_topology_payload(state_topology_payload)
        state_projection = parse_code_state_projection_payload(state_projection_payload)
        architecture = parse_code_architecture_question_payload(architecture_payload)
        change_evolution = parse_code_change_evolution_payload(change_evolution_payload)
        assurance = parse_code_assurance_payload(assurance_payload)
        capability = parse_capability_reachability_payload(capability_payload)
        analyzer_effectiveness = parse_code_analyzer_effectiveness_payload(
            analyzer_effectiveness_payload
        )
        supply_chain = parse_code_supply_chain_payload(supply_chain_payload)
        interface_surface = parse_code_interface_surface_payload(interface_surface_payload)
        if (
            state_topology.source_version != _CODE_REVIEW_V15
            or capability.source_version != _CODE_REVIEW_V15
        ):
            raise ValueError("code-review/v15 integrated source version is inconsistent")
        snapshot_id = _first_text(snapshot, "processing_signature")
        snapshot_freshness = _first_text(snapshot, "freshness")
        if snapshot_id is None or snapshot_freshness not in {
            "current",
            "publication_only",
            "unknown",
        }:
            raise ValueError("code-review/v15 snapshot identity is inconsistent")
        resolved_freshness = cast(
            Literal["current", "publication_only", "unknown"], snapshot_freshness
        )
        if (
            assurance.snapshot_id != snapshot_id
            or assurance.snapshot_freshness != snapshot_freshness
        ):
            raise ValueError("code-review/v15 assurance snapshot is inconsistent")
        if change_evolution.change_surface.status == "ready" and (
            change_evolution.change_surface.current_analysis_run_id
            != snapshot.get("analysis_run_id")
            or change_evolution.change_surface.processing_signature != snapshot_id
        ):
            raise ValueError("code-review/v15 change transition snapshot is inconsistent")
        if supply_chain.analysis_run_id != snapshot.get("analysis_run_id"):
            raise ValueError("code-review/v15 supply-chain snapshot is inconsistent")
        if interface_surface.status == "ready" and (
            interface_surface.analysis_run_id != snapshot.get("analysis_run_id")
            or interface_surface.processing_signature != snapshot_id
        ):
            raise ValueError("code-review/v15 interface surface snapshot is inconsistent")
        if analyzer_effectiveness.status == "ready" and (
            analyzer_effectiveness.analysis_run_id != snapshot.get("analysis_run_id")
            or analyzer_effectiveness.framework_run_id != snapshot.get("framework_run_id")
            or analyzer_effectiveness.processing_signature != snapshot_id
            or analyzer_effectiveness.snapshot_freshness != snapshot_freshness
            or analyzer_effectiveness.source_version != _CODE_REVIEW_V15
        ):
            raise ValueError("code-review/v15 analyzer effectiveness snapshot is inconsistent")

        expected_extra_specs: list[AnalysisQuestionSpec] = []
        expected_extra_evaluations: list[AnalysisQuestionEvaluation] = []
        offset = base_evaluation_count
        architecture_specs, architecture_evaluations = architecture_questions(
            architecture,
            snapshot_id=snapshot_id,
            snapshot_freshness=resolved_freshness,
            rank_offset=offset,
        )
        expected_extra_specs.extend(architecture_specs)
        expected_extra_evaluations.extend(architecture_evaluations)
        offset += len(architecture_evaluations)
        interface_specs, interface_evaluations = interface_surface_questions(
            interface_surface,
            snapshot_freshness=resolved_freshness,
            rank_offset=offset,
        )
        expected_extra_specs.extend(interface_specs)
        expected_extra_evaluations.extend(interface_evaluations)
        offset += len(interface_evaluations)
        projection_specs, projection_evaluations = state_projection_questions(
            state_projection,
            rank=offset + 1,
        )
        expected_extra_specs.extend(projection_specs)
        expected_extra_evaluations.extend(projection_evaluations)
        offset += len(projection_evaluations)
        topology_specs, topology_evaluations = state_topology_questions(
            state_topology,
            rank=offset + 1,
        )
        expected_extra_specs.extend(topology_specs)
        expected_extra_evaluations.extend(topology_evaluations)
        offset += len(topology_evaluations)
        evolution_specs, evolution_evaluations = expected_code_change_evolution_questions(
            change_evolution,
            rank_offset=offset,
        )
        expected_extra_specs.extend(evolution_specs)
        expected_extra_evaluations.extend(evolution_evaluations)
        offset += len(evolution_evaluations)
        assurance_specs, assurance_evaluations = assurance_questions(
            assurance,
            rank_offset=offset,
        )
        expected_extra_specs.extend(assurance_specs)
        expected_extra_evaluations.extend(assurance_evaluations)
        offset += len(assurance_evaluations)
        security_specs, security_evaluations = security_dependency_questions(
            supply_chain,
            snapshot_id=snapshot_id,
            snapshot_freshness=resolved_freshness,
            rank_offset=offset,
        )
        expected_extra_specs.extend(security_specs)
        expected_extra_evaluations.extend(security_evaluations)
        offset += len(security_evaluations)
        capability_specs, capability_evaluations = capability_reachability_questions(
            capability,
            rank_offset=offset,
        )
        expected_extra_specs.extend(capability_specs)
        expected_extra_evaluations.extend(capability_evaluations)
        offset += len(capability_evaluations)
        effectiveness_specs, effectiveness_evaluations = analyzer_effectiveness_questions(
            analyzer_effectiveness,
            rank_offset=offset,
        )
        expected_extra_specs.extend(effectiveness_specs)
        expected_extra_evaluations.extend(effectiveness_evaluations)
        if specs[base_spec_count:] != tuple(expected_extra_specs) or evaluations[
            base_evaluation_count:
        ] != tuple(expected_extra_evaluations):
            raise ValueError("code-review/v15 integrated question projection is not canonical")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("code-review/v15"):
            raise
        raise ValueError("code-review/v15 integrated projection is malformed") from exc


@dataclass(frozen=True, slots=True)
class _ReviewV16Contract:
    schema: str
    label: str
    has_receipts: bool
    has_technical_verification: bool
    has_retention: bool
    has_review_task_protocol: bool
    has_knowledge_asset_health: bool
    has_knowledge_pdf_asset_health: bool
    added_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReviewV16BaseProjection:
    specs: tuple[AnalysisQuestionSpec, ...]
    evaluations: tuple[AnalysisQuestionEvaluation, ...]
    base_spec_count: int
    base_evaluation_count: int
    snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _ReviewV16IntegratedEvidence:
    state_topology: CodeStateTopologyAnalysis
    retention_analysis: CodeRetentionAnalysis | None
    state_projection: CodeStateProjectionAnalysis
    state_interactions: CodeStateInteractionAnalysis
    architecture: CodeArchitectureAnalysis
    change_evolution: CodeChangeEvolutionAnalysis
    assurance: CodeAssuranceAnalysis
    invariant_assurance: CodeInvariantAssuranceAnalysis
    capability: CodeCapabilityReachabilityAnalysis
    route_capabilities: CodeRouteCapabilityAnalysis
    analyzer_effectiveness: CodeAnalyzerEffectivenessAnalysis
    analyzer_calibration: CodeAnalyzerCalibrationAnalysis
    experiment_plan: CodeExperimentPlan
    receipts: tuple[ResolvedCodeExperimentReceipt, ...]
    technical_verification: CodeTechnicalVerification | None
    supply_chain: CodeSupplyChainAnalysis
    interface_surface: CodeInterfaceSurfaceAnalysis


def _review_v16_contract(payload: Mapping[str, object]) -> _ReviewV16Contract:
    review_schema = payload.get("schema")
    if review_schema not in {
        _CODE_REVIEW_V16,
        _CODE_REVIEW_V17,
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }:
        raise ValueError("code-review/v16-v22 validator received an unsupported schema")
    schema = str(review_schema)
    has_receipts = schema in {
        _CODE_REVIEW_V17,
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }
    has_technical_verification = schema in {
        _CODE_REVIEW_V18,
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }
    has_retention = schema in {
        _CODE_REVIEW_V19,
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }
    has_review_task_protocol = schema in {
        _CODE_REVIEW_V20,
        _CODE_REVIEW_V21,
        _CODE_REVIEW_V22,
    }
    has_knowledge_asset_health = schema in {_CODE_REVIEW_V21, _CODE_REVIEW_V22}
    has_knowledge_pdf_asset_health = schema == _CODE_REVIEW_V22
    added_keys = (
        "state_interactions",
        "invariant_assurance",
        "route_capabilities",
        "analyzer_calibration",
        "experiment_plan",
    ) + (("retention_analysis",) if has_retention else ())
    return _ReviewV16Contract(
        schema=schema,
        label=schema.replace("neocortex.", ""),
        has_receipts=has_receipts,
        has_technical_verification=has_technical_verification,
        has_retention=has_retention,
        has_review_task_protocol=has_review_task_protocol,
        has_knowledge_asset_health=has_knowledge_asset_health,
        has_knowledge_pdf_asset_health=has_knowledge_pdf_asset_health,
        added_keys=added_keys,
    )


def _validate_review_v16_schema_surface(
    payload: Mapping[str, object],
    contract: _ReviewV16Contract,
) -> None:
    receipts = payload.get("experiment_receipts")
    if not contract.has_receipts and receipts is not None and receipts != [] and receipts != ():
        raise ValueError(f"{contract.label} does not define experiment receipts")
    if (
        not contract.has_technical_verification
        and payload.get("technical_verification") is not None
    ):
        raise ValueError(f"{contract.label} does not define technical verification")
    if not contract.has_retention and payload.get("retention_analysis") is not None:
        raise ValueError(f"{contract.label} does not define retention analysis")


def _validate_abstained_review_v16_payload(
    payload: Mapping[str, object],
    contract: _ReviewV16Contract,
) -> None:
    if any(payload.get(key) is not None for key in contract.added_keys):
        raise ValueError(f"abstained {contract.label} payload asserts integrated evidence")
    if contract.has_receipts and payload.get("experiment_receipts") != []:
        raise ValueError(f"abstained {contract.label} payload asserts experiment receipts")
    if contract.has_technical_verification and payload.get("technical_verification") is not None:
        raise ValueError(f"abstained {contract.label} payload asserts technical verification")
    projected = dict(payload)
    projected["schema"] = _CODE_REVIEW_V15
    for key in contract.added_keys:
        projected.pop(key, None)
    projected.pop("experiment_receipts", None)
    projected.pop("technical_verification", None)
    _validate_review_v15_payload(projected)


def _review_v16_base_projection(
    payload: Mapping[str, object],
    epistemics: Mapping[str, object],
    contract: _ReviewV16Contract,
) -> _ReviewV16BaseProjection:
    specs, evaluations = parse_analysis_questions_payload(epistemics)
    findings = _mapping_items(payload.get("findings"))
    structural_payload = _mapping(payload.get("structural_analysis"))
    snapshot = _mapping(payload.get("snapshot"))
    if structural_payload is None or snapshot is None:
        raise ValueError(f"ready {contract.label} payload lacks structural snapshot evidence")
    structural = parse_code_class_surface_payload(structural_payload)
    class_specs, class_evaluations = expected_class_surface_questions(
        structural,
        rank_offset=len(findings),
    )
    base_spec_count = (1 if findings else 0) + len(class_specs)
    base_evaluation_count = len(findings) + len(class_evaluations)
    projected = dict(payload)
    projected["schema"] = _CODE_REVIEW_V14
    for key in (
        "state_topology",
        "retention_analysis",
        "change_evolution",
        "assurance",
        "capability_reachability",
        "analyzer_effectiveness",
        "interface_surface",
        *contract.added_keys,
        "experiment_receipts",
        "technical_verification",
    ):
        projected.pop(key, None)
    projected["epistemics"] = analysis_questions_payload(
        specs[:base_spec_count],
        evaluations[:base_evaluation_count],
    )
    _validate_review_v14_payload(projected)
    return _ReviewV16BaseProjection(
        specs=specs,
        evaluations=evaluations,
        base_spec_count=base_spec_count,
        base_evaluation_count=base_evaluation_count,
        snapshot=snapshot,
    )


def _parse_review_v16_integrated_evidence(
    payload: Mapping[str, object],
    contract: _ReviewV16Contract,
) -> _ReviewV16IntegratedEvidence:
    nested_keys = (
        "state_topology",
        "state_projection",
        "state_interactions",
        "architecture",
        "change_evolution",
        "assurance",
        "invariant_assurance",
        "capability_reachability",
        "route_capabilities",
        "analyzer_effectiveness",
        "analyzer_calibration",
        "experiment_plan",
        "supply_chain",
        "interface_surface",
    ) + (("retention_analysis",) if contract.has_retention else ())
    nested = {key: _mapping(payload.get(key)) for key in nested_keys}
    if any(value is None for value in nested.values()):
        raise ValueError(f"ready {contract.label} payload lacks integrated evidence")
    return _ReviewV16IntegratedEvidence(
        state_topology=parse_code_state_topology_payload(cast(Any, nested["state_topology"])),
        retention_analysis=(
            parse_code_retention_analysis_payload(cast(Any, nested["retention_analysis"]))
            if contract.has_retention
            else None
        ),
        state_projection=parse_code_state_projection_payload(cast(Any, nested["state_projection"])),
        state_interactions=parse_code_state_interaction_payload(
            cast(Any, nested["state_interactions"])
        ),
        architecture=parse_code_architecture_question_payload(cast(Any, nested["architecture"])),
        change_evolution=parse_code_change_evolution_payload(cast(Any, nested["change_evolution"])),
        assurance=parse_code_assurance_payload(cast(Any, nested["assurance"])),
        invariant_assurance=parse_code_invariant_assurance_payload(
            cast(Any, nested["invariant_assurance"])
        ),
        capability=parse_capability_reachability_payload(
            cast(Any, nested["capability_reachability"])
        ),
        route_capabilities=parse_code_route_capability_payload(
            cast(Any, nested["route_capabilities"])
        ),
        analyzer_effectiveness=parse_code_analyzer_effectiveness_payload(
            cast(Any, nested["analyzer_effectiveness"])
        ),
        analyzer_calibration=parse_code_analyzer_calibration_payload(
            cast(Any, nested["analyzer_calibration"])
        ),
        experiment_plan=parse_code_experiment_plan_payload(cast(Any, nested["experiment_plan"])),
        receipts=(
            tuple(
                parse_resolved_code_experiment_receipt_payload(item)
                for item in _experiment_receipt_payloads(payload)
            )
            if contract.has_receipts
            else ()
        ),
        technical_verification=(
            parse_code_technical_verification_payload(
                cast(Any, _mapping(payload.get("technical_verification")))
            )
            if contract.has_technical_verification
            else None
        ),
        supply_chain=parse_code_supply_chain_payload(cast(Any, nested["supply_chain"])),
        interface_surface=parse_code_interface_surface_payload(
            cast(Any, nested["interface_surface"])
        ),
    )


def _validate_review_v16_source_versions(
    evidence: _ReviewV16IntegratedEvidence,
    contract: _ReviewV16Contract,
) -> None:
    if (
        evidence.state_topology.source_version != contract.schema
        or (
            evidence.retention_analysis is not None
            and evidence.retention_analysis.source_version != contract.schema
        )
        or evidence.capability.source_version != contract.schema
        or evidence.route_capabilities.source_version != contract.schema
        or evidence.analyzer_calibration.source_version != contract.schema
    ):
        raise ValueError(f"{contract.label} integrated source version is inconsistent")


def _review_v16_snapshot_identity(
    snapshot: Mapping[str, object],
) -> tuple[str, Literal["current", "publication_only", "unknown"], int]:
    snapshot_id = _first_text(snapshot, "processing_signature")
    snapshot_freshness = _first_text(snapshot, "freshness")
    snapshot_run_id = snapshot.get("analysis_run_id")
    if (
        snapshot_id is None
        or snapshot_freshness not in {"current", "publication_only", "unknown"}
        or (
            isinstance(snapshot_run_id, bool)
            or not isinstance(snapshot_run_id, int)
            or snapshot_run_id < 1
        )
    ):
        raise ValueError("code-review/v16 snapshot identity is inconsistent")
    return (
        snapshot_id,
        cast(Literal["current", "publication_only", "unknown"], snapshot_freshness),
        snapshot_run_id,
    )


def _validate_review_v16_receipt_links(
    evidence: _ReviewV16IntegratedEvidence,
    contract: _ReviewV16Contract,
    snapshot_id: str,
    snapshot_run_id: int,
) -> None:
    if any(
        item.analysis_run_id > snapshot_run_id or item.receipt.source_version != snapshot_id
        for item in evidence.receipts
    ):
        raise ValueError(f"{contract.label} experiment receipt snapshot is inconsistent")


def _validate_review_v16_assurance_links(
    evidence: _ReviewV16IntegratedEvidence,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> None:
    if (
        evidence.assurance.snapshot_id != snapshot_id
        or evidence.assurance.snapshot_freshness != snapshot_freshness
        or evidence.invariant_assurance.snapshot_id != snapshot_id
        or evidence.invariant_assurance.snapshot_freshness != snapshot_freshness
    ):
        raise ValueError("code-review/v16 assurance snapshot is inconsistent")


def _validate_review_v16_runtime_links(
    evidence: _ReviewV16IntegratedEvidence,
    snapshot_id: str,
    snapshot_run_id: int,
) -> None:
    if evidence.state_interactions.status != "abstained" and (
        evidence.state_interactions.analysis_run_id != snapshot_run_id
        or evidence.state_interactions.source_processing_signature != snapshot_id
    ):
        raise ValueError("code-review/v16 state interaction snapshot is inconsistent")
    if evidence.change_evolution.change_surface.status == "ready" and (
        evidence.change_evolution.change_surface.current_analysis_run_id != snapshot_run_id
        or evidence.change_evolution.change_surface.processing_signature != snapshot_id
    ):
        raise ValueError("code-review/v16 change transition snapshot is inconsistent")
    if evidence.supply_chain.analysis_run_id != snapshot_run_id:
        raise ValueError("code-review/v16 supply-chain snapshot is inconsistent")
    if evidence.interface_surface.status == "ready" and (
        evidence.interface_surface.analysis_run_id != snapshot_run_id
        or evidence.interface_surface.processing_signature != snapshot_id
    ):
        raise ValueError("code-review/v16 interface surface snapshot is inconsistent")


def _validate_review_v16_effectiveness_link(
    evidence: _ReviewV16IntegratedEvidence,
    contract: _ReviewV16Contract,
    snapshot: Mapping[str, object],
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    snapshot_run_id: int,
) -> None:
    if evidence.analyzer_effectiveness.status == "ready" and (
        evidence.analyzer_effectiveness.analysis_run_id != snapshot_run_id
        or evidence.analyzer_effectiveness.framework_run_id != snapshot.get("framework_run_id")
        or evidence.analyzer_effectiveness.processing_signature != snapshot_id
        or evidence.analyzer_effectiveness.snapshot_freshness != snapshot_freshness
        or evidence.analyzer_effectiveness.source_version != contract.schema
    ):
        raise ValueError("code-review/v16 analyzer effectiveness snapshot is inconsistent")


def _canonical_review_v16_questions(
    base: _ReviewV16BaseProjection,
    evidence: _ReviewV16IntegratedEvidence,
    contract: _ReviewV16Contract,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    expected_specs: list[AnalysisQuestionSpec] = []
    expected_evaluations: list[AnalysisQuestionEvaluation] = []
    offset = base.base_evaluation_count

    def append_questions(
        resolved: tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]],
    ) -> None:
        nonlocal offset
        new_specs, new_evaluations = resolved
        expected_specs.extend(new_specs)
        expected_evaluations.extend(new_evaluations)
        offset += len(new_evaluations)

    append_questions(
        architecture_questions(
            evidence.architecture,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            rank_offset=offset,
        )
    )
    append_questions(
        interface_surface_questions(
            evidence.interface_surface,
            snapshot_freshness=snapshot_freshness,
            rank_offset=offset,
        )
    )
    append_questions(state_projection_questions(evidence.state_projection, rank=offset + 1))
    append_questions(state_topology_questions(evidence.state_topology, rank=offset + 1))
    if evidence.retention_analysis is not None:
        append_questions(retention_questions(evidence.retention_analysis, rank=offset + 1))
    append_questions(
        state_interaction_questions(
            evidence.state_interactions,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            rank_offset=offset,
        )
    )
    append_questions(
        expected_code_change_evolution_questions(
            evidence.change_evolution,
            rank_offset=offset,
        )
    )
    append_questions(assurance_questions(evidence.assurance, rank_offset=offset))
    append_questions(
        invariant_assurance_questions(evidence.invariant_assurance, rank_offset=offset)
    )
    if contract.has_review_task_protocol:
        append_questions(
            framework_review_task_questions(
                snapshot_id=snapshot_id,
                snapshot_freshness=snapshot_freshness,
                rank=offset + 1,
            )
        )
    if contract.has_knowledge_asset_health:
        append_questions(
            knowledge_asset_health_questions(
                snapshot_id=snapshot_id,
                snapshot_freshness=snapshot_freshness,
                rank=offset + 1,
            )
        )
    if contract.has_knowledge_pdf_asset_health:
        append_questions(
            knowledge_pdf_asset_health_questions(
                snapshot_id=snapshot_id,
                snapshot_freshness=snapshot_freshness,
                rank=offset + 1,
            )
        )
    append_questions(
        security_dependency_questions(
            evidence.supply_chain,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            rank_offset=offset,
        )
    )
    append_questions(capability_reachability_questions(evidence.capability, rank_offset=offset))
    append_questions(route_capability_questions(evidence.route_capabilities, rank_offset=offset))
    append_questions(
        analyzer_effectiveness_questions(evidence.analyzer_effectiveness, rank_offset=offset)
    )
    append_questions(
        analyzer_calibration_questions(evidence.analyzer_calibration, rank_offset=offset)
    )
    return (
        base.specs[: base.base_spec_count] + tuple(expected_specs),
        base.evaluations[: base.base_evaluation_count] + tuple(expected_evaluations),
    )


def _validate_review_v16_experiment_projection(
    base: _ReviewV16BaseProjection,
    evidence: _ReviewV16IntegratedEvidence,
    contract: _ReviewV16Contract,
    canonical_specs: tuple[AnalysisQuestionSpec, ...],
    canonical_base_evaluations: tuple[AnalysisQuestionEvaluation, ...],
) -> None:
    if base.specs != canonical_specs:
        raise ValueError(f"{contract.label} question registry is not canonical")
    base_plan = plan_code_experiments(canonical_specs, canonical_base_evaluations)
    canonical_evaluations = apply_code_experiment_receipts(
        canonical_specs,
        canonical_base_evaluations,
        base_plan,
        evidence.receipts,
    )
    if base.evaluations != canonical_evaluations:
        raise ValueError(f"{contract.label} integrated question projection is not canonical")
    if evidence.experiment_plan != plan_code_experiments(base.specs, canonical_evaluations):
        raise ValueError(f"{contract.label} experiment plan is not canonical")
    if contract.has_technical_verification and evidence.technical_verification != (
        build_code_technical_verification(base.specs, canonical_evaluations, evidence.receipts)
    ):
        raise ValueError(f"{contract.label} technical verification is not canonical")


def _validate_review_v16_payload(payload: Mapping[str, object]) -> None:
    """Validate v16-v22 verticals and their durable evidence projections."""

    contract = _review_v16_contract(payload)
    _validate_review_v16_schema_surface(payload, contract)
    epistemics = _mapping(payload.get("epistemics"))
    if epistemics is None:
        raise ValueError(f"{contract.label} payload lacks its epistemic contract")
    if payload.get("status") == "abstained":
        _validate_abstained_review_v16_payload(payload, contract)
        return
    try:
        base = _review_v16_base_projection(payload, epistemics, contract)
        evidence = _parse_review_v16_integrated_evidence(payload, contract)
        _validate_review_v16_source_versions(evidence, contract)
        snapshot_id, snapshot_freshness, snapshot_run_id = _review_v16_snapshot_identity(
            base.snapshot
        )
        _validate_review_v16_receipt_links(
            evidence,
            contract,
            snapshot_id,
            snapshot_run_id,
        )
        _validate_review_v16_assurance_links(evidence, snapshot_id, snapshot_freshness)
        _validate_review_v16_runtime_links(evidence, snapshot_id, snapshot_run_id)
        _validate_review_v16_effectiveness_link(
            evidence,
            contract,
            base.snapshot,
            snapshot_id,
            snapshot_freshness,
            snapshot_run_id,
        )
        canonical_specs, canonical_base_evaluations = _canonical_review_v16_questions(
            base,
            evidence,
            contract,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
        )
        _validate_review_v16_experiment_projection(
            base,
            evidence,
            contract,
            canonical_specs,
            canonical_base_evaluations,
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(contract.label):
            raise
        raise ValueError(f"{contract.label} integrated projection is malformed") from exc


def _query_result_payload(
    *,
    query: CodeAnalysisQuery,
    expected_kind: str,
    source_payload: Mapping[str, object],
    status: str,
    filters: Mapping[str, list[str]],
    available: int,
    matched: int,
    returned: list[dict[str, object]],
    limitations: list[str],
    byte_truncated: bool,
) -> dict[str, object]:
    projected_limitations = (
        _dimension_values((*limitations, "query_output_byte_bound_applied"))
        if byte_truncated
        else limitations
    )
    return {
        "kind": "code-analysis-query",
        "schema": CODE_ANALYSIS_QUERY_SCHEMA,
        "surface": query.surface,
        "status": status,
        "source": {
            "kind": expected_kind,
            "schema": _source_schema(source_payload, query.surface),
            "digest": _source_digest(source_payload),
        },
        "filters": dict(filters),
        "counts": {
            "available": available,
            "matched": matched,
            "returned": len(returned),
            "truncated": len(returned) < matched,
        },
        "matches": returned,
        "limitations": projected_limitations,
        "output_bound": {
            "max_public_json_bytes": CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES,
            "byte_truncated": byte_truncated,
        },
        "authority": "advisory",
        "mutation_authority": False,
        "aggregate_score": None,
        "defect_probability": None,
    }


def query_code_analysis(
    payload: Mapping[str, object],
    query: CodeAnalysisQuery,
) -> dict[str, object]:
    """Project and filter one already-materialized public Code payload."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    if not isinstance(query, CodeAnalysisQuery):
        raise TypeError("query must be CodeAnalysisQuery")
    expected_kind = _SURFACE_KINDS[query.surface]
    actual_kind = payload.get("kind")
    if actual_kind != expected_kind:
        raise ValueError(
            f"{query.surface} query requires kind {expected_kind!r}, got {actual_kind!r}"
        )
    if query.surface == "review":
        review_schema = payload.get("schema")
        if review_schema in {
            _CODE_REVIEW_V16,
            _CODE_REVIEW_V17,
            _CODE_REVIEW_V18,
            _CODE_REVIEW_V19,
            _CODE_REVIEW_V20,
            _CODE_REVIEW_V21,
            _CODE_REVIEW_V22,
        }:
            _validate_review_v16_payload(payload)
        elif review_schema == _CODE_REVIEW_V15:
            _validate_review_v15_payload(payload)
        elif review_schema == _CODE_REVIEW_V14:
            _validate_review_v14_payload(payload)
        elif review_schema == _CODE_REVIEW_V13:
            _validate_review_v13_payload(payload)
        elif review_schema == _CODE_REVIEW_V12:
            _validate_review_v12_payload(payload)
        elif review_schema == _CODE_REVIEW_V11:
            _validate_review_v11_payload(payload)
        elif review_schema != _CODE_REVIEW_V10:
            raise ValueError(f"unsupported code-review schema: {review_schema!r}")
    extractors = {
        "status": _extract_status,
        "review": _extract_review,
        "diff": _extract_diff,
    }
    records = extractors[query.surface](payload)
    if len(records) > CODE_ANALYSIS_QUERY_MAX_SOURCE_RECORDS:
        raise ValueError("query source projection exceeds its record bound")
    records.sort(key=lambda item: (str(item["record_type"]), str(item["id"])))
    matched = [record for record in records if _record_matches(record, query)]
    returned = matched[: query.limit]
    status = _source_status(payload, query.surface)
    filters = {name: list(getattr(query, name)) for name in _DIMENSIONS}
    limitations = _source_limitations(payload, status)
    result = _query_result_payload(
        query=query,
        expected_kind=expected_kind,
        source_payload=payload,
        status=status,
        filters=filters,
        available=len(records),
        matched=len(matched),
        returned=returned,
        limitations=limitations,
        byte_truncated=False,
    )
    if _public_json_bytes(result) <= CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES:
        return result

    lower = 0
    upper = len(returned)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        candidate = _query_result_payload(
            query=query,
            expected_kind=expected_kind,
            source_payload=payload,
            status=status,
            filters=filters,
            available=len(records),
            matched=len(matched),
            returned=returned[:midpoint],
            limitations=limitations,
            byte_truncated=True,
        )
        if _public_json_bytes(candidate) <= CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES:
            lower = midpoint
        else:
            upper = midpoint - 1
    bounded = _query_result_payload(
        query=query,
        expected_kind=expected_kind,
        source_payload=payload,
        status=status,
        filters=filters,
        available=len(records),
        matched=len(matched),
        returned=returned[:lower],
        limitations=limitations,
        byte_truncated=True,
    )
    if _public_json_bytes(bounded) > CODE_ANALYSIS_QUERY_MAX_OUTPUT_BYTES:
        raise ValueError("query output cannot satisfy its public JSON byte bound")
    return bounded


__all__ = ["CODE_ANALYSIS_QUERY_SCHEMA", "CodeAnalysisQuery", "query_code_analysis"]
