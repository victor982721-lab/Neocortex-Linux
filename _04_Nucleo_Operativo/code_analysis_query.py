"""Bounded multidimensional queries over explicit published Code surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_questions_payload,
    analysis_identity,
    analysis_question_spec_fingerprint,
    parse_analysis_questions_payload,
)
from .code_analyzer_effectiveness import (
    analyzer_effectiveness_questions,
    parse_code_analyzer_effectiveness_payload,
)
from .code_assurance_analysis import assurance_questions, parse_code_assurance_payload
from .code_architecture_questions import (
    architecture_questions,
    parse_code_architecture_question_payload,
)
from .code_capability_reachability_analysis import (
    capability_reachability_questions,
    parse_capability_reachability_payload,
)
from .code_change_evolution_analysis import (
    expected_code_change_evolution_questions,
    parse_code_change_evolution_payload,
)
from .code_class_surface_analysis import (
    expected_class_surface_questions,
    parse_code_class_surface_payload,
)
from .code_interface_surface_analysis import (
    interface_surface_questions,
    parse_code_interface_surface_payload,
)
from .code_state_projection_analysis import (
    parse_code_state_projection_payload,
    state_projection_questions,
)
from .code_state_topology_analysis import (
    parse_code_state_topology_payload,
    state_topology_questions,
)
from .code_review_epistemics import STRUCTURAL_HOTSPOT_QUESTION
from .code_security_dependency_questions import security_dependency_questions
from .code_schema import CODE_SCHEMA_VERSION
from .code_supply_chain_analysis import parse_code_supply_chain_payload
from .semantic_models import canonical_json

CODE_ANALYSIS_QUERY_SCHEMA = "neocortex.code-analysis-query/v1"

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
_CODE_ANALYSIS_EPISTEMICS_V1 = "neocortex.code-analysis-epistemics/v1"
_UNUSED_V11_STEP_REQUIREMENTS = (
    "verify_import_reexport_callback_registry_protocol_and_entry_point_usage",
    "run_targeted_tests_and_public_import_smoke_without_mutating_code",
    "record_explicit_human_confirmation_or_reclassify_with_new_evidence",
    "require_comparable_unused_analysis_replay_before_any_separate_change",
)


def _normalize_filter(values: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} filters must be a tuple")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} filters must contain strings")
        candidate = value.strip().casefold()
        if not candidate:
            raise ValueError(f"{name} filters must be non-empty")
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
        for name in _DIMENSIONS:
            object.__setattr__(
                self,
                name,
                _normalize_filter(getattr(self, name), name=name),
            )


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _string_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidate = value.strip()
        return (candidate,) if candidate else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
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
    return {
        "id": _bounded_text(f"{source_path}:{record_id}", limit=1024),
        "record_type": record_type,
        "source_path": source_path,
        "dimensions": dimensions,
        "facts": dict(facts or {}),
    }


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
    for index, module in enumerate(_mapping_items(architecture.get("modules"))):
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


def _extract_review(payload: Mapping[str, object]) -> list[dict[str, object]]:
    records = _extract_status(payload)
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
    epistemics = _mapping(payload.get("epistemics"))
    if epistemics is not None:
        for index, evaluation in enumerate(_mapping_items(epistemics.get("evaluations"))):
            evaluation_id = _first_text(evaluation, "evaluation_id") or str(index)
            subject = _mapping(evaluation.get("subject")) or {}
            question_id = _first_text(evaluation, "question_id") or "question"
            subject_kind = _first_text(subject, "subject_kind") or "subject"
            location = _mapping(subject.get("location")) or {}
            evidence = _mapping_items(evaluation.get("evidence"))
            modules = _module_values(location)
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
                    ),
                    facts={
                        **_facts(
                            evaluation,
                            "question_id",
                            "question_version",
                            "question_spec_fingerprint",
                            "rank",
                            "decision_reason",
                            "authority",
                            "mutation_authority",
                        ),
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
        if review_schema == _CODE_REVIEW_V15:
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
    records.sort(key=lambda item: (str(item["record_type"]), str(item["id"])))
    matched = [record for record in records if _record_matches(record, query)]
    returned = matched[: query.limit]
    status = _source_status(payload, query.surface)
    filters = {name: list(getattr(query, name)) for name in _DIMENSIONS}
    return {
        "kind": "code-analysis-query",
        "schema": CODE_ANALYSIS_QUERY_SCHEMA,
        "surface": query.surface,
        "status": status,
        "source": {
            "kind": expected_kind,
            "schema": _source_schema(payload, query.surface),
            "digest": _source_digest(payload),
        },
        "filters": filters,
        "counts": {
            "available": len(records),
            "matched": len(matched),
            "returned": len(returned),
            "truncated": len(returned) < len(matched),
        },
        "matches": returned,
        "limitations": _source_limitations(payload, status),
        "authority": "advisory",
        "mutation_authority": False,
        "aggregate_score": None,
        "defect_probability": None,
    }


__all__ = ["CODE_ANALYSIS_QUERY_SCHEMA", "CodeAnalysisQuery", "query_code_analysis"]
