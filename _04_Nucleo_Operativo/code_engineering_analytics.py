"""Explainable engineering dimensions over already-published Code evidence.

This projection intentionally places complexity, coverage, mutation, local Git
history and graph observations next to each other without collapsing them into
a risk score or a defect probability.  It is read-only and bounded; producers
remain the owners of their normalized evidence.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from .code_architecture_analysis import (
    ArchitectureModule,
    CodeArchitectureAnalysis,
    module_id_from_path,
    read_code_architecture_analysis,
)
from .code_coverage_analysis import CodeCoverageAnalysis, read_code_coverage_analysis
from .external_evidence_models import ExternalProviderEvidence, ExternalProviderMetric
from .external_evidence_store import read_external_provider_evidence
from .semantic_models import canonical_json, fingerprint_text

CODE_ENGINEERING_ANALYTICS_SCHEMA = "neocortex.code-engineering-analytics/v2"
GIT_HISTORY_PROVIDER_ID = "git-history-local"
MUTATION_PROVIDER_ID = "cosmic-ray-focal-mutation"
CODE_ENGINEERING_MODULE_LIMIT = 5_000
CODE_ENGINEERING_PROVIDER_METRIC_LIMIT = 250_000
CODE_ENGINEERING_PROVIDER_RELATION_LIMIT = 250_000

EngineeringStatus = Literal["ready", "partial", "abstained"]
DimensionStatus = Literal["ready", "abstained", "not_recorded"]
GateStatus = Literal["passed", "failed", "not_evaluated"]

_HISTORY_METRICS = frozenset(
    {
        "history_observed",
        "observed_commit_count",
        "observed_touch_count",
        "observed_additions",
        "observed_deletions",
        "observed_churn_lines",
        "binary_or_unmeasured_touch_count",
        "observed_change_frequency_per_100_commits",
        "observed_age_seconds",
        "observed_recency_seconds",
    }
)
_MUTATION_METRICS = frozenset(
    {
        "mutants_generated",
        "mutants_selected",
        "mutants_completed",
        "mutants_killed",
        "mutants_survived",
        "mutants_timed_out",
        "mutants_incompetent",
        "mutants_reused",
        "mutation_score",
        "duration_milliseconds",
        "baseline_duration_milliseconds",
        "baseline_passed",
        "measurement_complete",
    }
)


@dataclass(frozen=True, slots=True)
class EngineeringMetric:
    name: str
    value: float
    unit: str
    source: str
    subject_kind: str
    subject_key: str


@dataclass(frozen=True, slots=True)
class EngineeringDimension:
    dimension: Literal["complexity", "coverage", "mutation", "history", "graph"]
    status: DimensionStatus
    reason: str | None
    metrics: tuple[EngineeringMetric, ...]
    provenance: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EngineeringGate:
    gate: Literal[
        "mutation_test_baseline",
        "mutation_measurement_complete",
        "mutation_score_recorded",
    ]
    status: GateStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class ModuleEngineeringProfile:
    module_id: str
    path_namespace_id: str | None
    complexity: EngineeringDimension
    coverage: EngineeringDimension
    mutation: EngineeringDimension
    history: EngineeringDimension
    graph: EngineeringDimension


@dataclass(frozen=True, slots=True)
class EngineeringProviderSummary:
    provider_id: str
    status: DimensionStatus
    reason: str | None
    tool_run_id: int | None
    effective_tool_run_id: int | None
    metrics: int
    relations: int


@dataclass(frozen=True, slots=True)
class CodeEngineeringAnalytics:
    database: str
    analysis_run_id: int | None
    status: EngineeringStatus
    reason: str | None
    providers: tuple[EngineeringProviderSummary, ...]
    modules: tuple[ModuleEngineeringProfile, ...]
    gates: tuple[EngineeringGate, ...]
    mutation_scope_signature: str | None
    mutation_score: float | None
    limitations: tuple[str, ...]
    digest: str
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False
    aggregate_score: None = None
    defect_probability: None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": "code-engineering-analytics",
            "schema": CODE_ENGINEERING_ANALYTICS_SCHEMA,
            **asdict(self),
        }


def _metric(
    name: str,
    value: float | int | bool,
    unit: str,
    source: str,
    subject_kind: str,
    subject_key: str,
) -> EngineeringMetric:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("engineering metric must be finite")
    return EngineeringMetric(name, numeric, unit, source, subject_kind, subject_key)


def _dimension(
    name: Literal["complexity", "coverage", "mutation", "history", "graph"],
    metrics: Sequence[EngineeringMetric],
    *,
    reason: str | None = None,
    not_recorded: bool = False,
    provenance: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> EngineeringDimension:
    ordered = tuple(sorted(metrics, key=lambda item: (item.name, item.source, item.subject_key)))
    status: DimensionStatus
    if ordered:
        status = "ready"
    elif not_recorded:
        status = "not_recorded"
    else:
        status = "abstained"
    return EngineeringDimension(
        name,
        status,
        None if status == "ready" else reason,
        ordered,
        tuple(sorted(set(provenance))),
        tuple(sorted(set(limitations))),
    )


def _provider_summary(
    provider_id: str,
    evidence: ExternalProviderEvidence | None,
) -> EngineeringProviderSummary:
    if evidence is None:
        return EngineeringProviderSummary(
            provider_id, "not_recorded", "provider_missing", None, None, 0, 0
        )
    if evidence.status != "ready":
        return EngineeringProviderSummary(
            provider_id,
            "abstained",
            evidence.reason or "provider_evidence_abstained",
            evidence.tool_run_id,
            evidence.effective_tool_run_id,
            0,
            0,
        )
    return EngineeringProviderSummary(
        provider_id,
        "ready",
        None,
        evidence.tool_run_id,
        evidence.effective_tool_run_id,
        len(evidence.metrics),
        len(evidence.relations),
    )


def _validated_provider(
    provider_id: str,
    providers: Mapping[str, ExternalProviderEvidence],
) -> ExternalProviderEvidence | None:
    evidence = providers.get(provider_id)
    if evidence is None or evidence.status != "ready":
        return None
    if len(evidence.metrics) > CODE_ENGINEERING_PROVIDER_METRIC_LIMIT:
        raise ValueError(f"{provider_id} metric bound exceeded")
    if len(evidence.relations) > CODE_ENGINEERING_PROVIDER_RELATION_LIMIT:
        raise ValueError(f"{provider_id} relation bound exceeded")
    return evidence


def _module_for_metric(metric: ExternalProviderMetric) -> str | None:
    if metric.subject_kind == "module":
        return metric.subject_key
    path = metric.metadata.get("target_relative_path")
    if not isinstance(path, str) and metric.subject_kind == "file":
        path = metric.subject_key
    return None if not isinstance(path, str) else module_id_from_path(path, ".")


def _external_metrics_by_module(
    evidence: ExternalProviderEvidence | None,
    allowed_names: frozenset[str],
    source: str,
) -> tuple[dict[str, list[EngineeringMetric]], dict[str, object]]:
    grouped: dict[str, list[EngineeringMetric]] = defaultdict(list)
    metadata: dict[str, object] = {}
    if evidence is None:
        return grouped, metadata
    for item in evidence.metrics:
        if item.metric_name not in allowed_names:
            continue
        module = _module_for_metric(item)
        if module is None:
            continue
        grouped[module].append(
            _metric(
                item.metric_name,
                item.value,
                item.unit,
                source,
                item.subject_kind,
                item.subject_key,
            )
        )
        if source == MUTATION_PROVIDER_ID:
            for key in (
                "measurement_scope_signature",
                "selection_truncated",
                "measurement_complete",
            ):
                if key in item.metadata:
                    metadata[key] = item.metadata[key]
    return grouped, metadata


def _history_cochanges(
    evidence: ExternalProviderEvidence | None,
) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    if evidence is None:
        return counts
    for relation in evidence.relations:
        if relation.relation_kind != "file_cochange":
            continue
        left = module_id_from_path(relation.source_key, ".")
        right = module_id_from_path(relation.target_key, ".")
        if left is not None:
            counts[left] += 1
        if right is not None and right != left:
            counts[right] += 1
    return counts


def _complexity_dimension(module: ArchitectureModule) -> EngineeringDimension:
    values: list[EngineeringMetric] = []
    if module.cognitive_complexity_total is not None:
        values.append(
            _metric(
                "cognitive_complexity_total",
                module.cognitive_complexity_total,
                "count",
                "complexipy-cognitive",
                "module",
                module.module_id,
            )
        )
    if module.cognitive_complexity_max is not None:
        values.append(
            _metric(
                "cognitive_complexity_max",
                module.cognitive_complexity_max,
                "count",
                "complexipy-cognitive",
                "module",
                module.module_id,
            )
        )
    return _dimension(
        "complexity",
        values,
        reason="cognitive_complexity_not_recorded",
        provenance=("complexipy-cognitive",),
        limitations=("complexity_is_not_defect_probability",),
    )


def _coverage_dimensions(coverage: CodeCoverageAnalysis | None) -> dict[str, EngineeringDimension]:
    result: dict[str, EngineeringDimension] = {}
    if coverage is None or coverage.status != "ready":
        return result
    for scope in coverage.modules:
        metrics: list[EngineeringMetric] = []
        totals = scope.totals
        for name, value, unit in (
            ("executable_lines", totals.executable_lines, "lines"),
            ("covered_lines", totals.covered_lines, "lines"),
            ("missing_lines", totals.missing_lines, "lines"),
            ("branch_exits", totals.branch_exits, "branches"),
            ("covered_branch_exits", totals.covered_branch_exits, "branches"),
            ("missing_branch_exits", totals.missing_branch_exits, "branches"),
        ):
            metrics.append(
                _metric(name, value, unit, coverage.provider_id, "module", scope.subject_key)
            )
        for name, percentage in (
            ("line_coverage_percent", totals.line_coverage_percent),
            ("branch_coverage_percent", totals.branch_coverage_percent),
        ):
            if percentage is not None:
                metrics.append(
                    _metric(
                        name,
                        percentage,
                        "percent",
                        coverage.provider_id,
                        "module",
                        scope.subject_key,
                    )
                )
        result[scope.subject_key] = _dimension(
            "coverage",
            metrics,
            provenance=(coverage.provider_id,),
            limitations=coverage.limitations,
        )
    return result


def _graph_dimension(module: ArchitectureModule) -> EngineeringDimension:
    values = (
        _metric(
            "fan_in", module.fan_in, "count", "internal-import-graph", "module", module.module_id
        ),
        _metric(
            "fan_out", module.fan_out, "count", "internal-import-graph", "module", module.module_id
        ),
        _metric(
            "dependency_reach",
            module.dependency_reach,
            "count",
            "internal-import-graph",
            "module",
            module.module_id,
        ),
        _metric(
            "blast_radius",
            module.blast_radius,
            "count",
            "internal-import-graph",
            "module",
            module.module_id,
        ),
        _metric(
            "directed_degree_centrality",
            module.directed_degree_centrality,
            "ratio",
            "internal-import-graph",
            "module",
            module.module_id,
        ),
        _metric(
            "cross_path_namespace_fan_in",
            module.cross_path_namespace_fan_in,
            "count",
            "internal-import-graph",
            "module",
            module.module_id,
        ),
        _metric(
            "cross_path_namespace_fan_out",
            module.cross_path_namespace_fan_out,
            "count",
            "internal-import-graph",
            "module",
            module.module_id,
        ),
    )
    limitations = ["graph_metrics_are_structural_not_defect_probability"]
    if module.dependency_reach_truncated or module.blast_radius_truncated:
        limitations.append("transitive_graph_counts_are_truncated_lower_bounds")
    return _dimension(
        "graph", values, provenance=("internal-import-graph",), limitations=limitations
    )


def _mutation_gates(
    evidence: ExternalProviderEvidence | None,
    mutation_metrics: Mapping[str, Sequence[EngineeringMetric]],
) -> tuple[EngineeringGate, ...]:
    flattened = [item for values in mutation_metrics.values() for item in values]
    names = {item.name: item.value for item in flattened}
    if evidence is None:
        return (
            EngineeringGate(
                "mutation_test_baseline", "not_evaluated", "mutation_provider_not_ready"
            ),
            EngineeringGate(
                "mutation_measurement_complete",
                "not_evaluated",
                "mutation_provider_not_ready",
            ),
            EngineeringGate(
                "mutation_score_recorded", "not_evaluated", "mutation_provider_not_ready"
            ),
        )
    baseline = names.get("baseline_passed")
    complete = names.get("measurement_complete")
    score = names.get("mutation_score")
    return (
        EngineeringGate(
            "mutation_test_baseline",
            "passed" if baseline == 1.0 else "failed",
            None if baseline == 1.0 else "mutation_test_baseline_not_passed",
        ),
        EngineeringGate(
            "mutation_measurement_complete",
            "passed" if complete == 1.0 else "failed",
            None if complete == 1.0 else "mutation_measurement_incomplete",
        ),
        EngineeringGate(
            "mutation_score_recorded",
            "passed" if score is not None and 0.0 <= score <= 1.0 else "failed",
            None
            if score is not None and 0.0 <= score <= 1.0
            else "mutation_score_missing_or_invalid",
        ),
    )


@dataclass(frozen=True, slots=True)
class _EngineeringEvidence:
    history: ExternalProviderEvidence | None
    mutation: ExternalProviderEvidence | None
    history_metrics: Mapping[str, Sequence[EngineeringMetric]]
    mutation_metrics: Mapping[str, Sequence[EngineeringMetric]]
    mutation_metadata: Mapping[str, object]
    cochanges: Mapping[str, int]
    coverage_by_module: Mapping[str, EngineeringDimension]


def _engineering_evidence(
    coverage: CodeCoverageAnalysis | None,
    providers: Mapping[str, ExternalProviderEvidence],
) -> _EngineeringEvidence:
    history = _validated_provider(GIT_HISTORY_PROVIDER_ID, providers)
    mutation = _validated_provider(MUTATION_PROVIDER_ID, providers)
    history_metrics, _history_metadata = _external_metrics_by_module(
        history,
        _HISTORY_METRICS,
        GIT_HISTORY_PROVIDER_ID,
    )
    mutation_metrics, mutation_metadata = _external_metrics_by_module(
        mutation,
        _MUTATION_METRICS,
        MUTATION_PROVIDER_ID,
    )
    return _EngineeringEvidence(
        history,
        mutation,
        history_metrics,
        mutation_metrics,
        mutation_metadata,
        _history_cochanges(history),
        _coverage_dimensions(coverage),
    )


def _module_engineering_profile(
    module: ArchitectureModule,
    coverage: CodeCoverageAnalysis | None,
    evidence: _EngineeringEvidence,
) -> ModuleEngineeringProfile:
    history_values = list(evidence.history_metrics.get(module.module_id, ()))
    if module.module_id in evidence.cochanges:
        history_values.append(
            _metric(
                "observed_cochange_relation_count",
                evidence.cochanges[module.module_id],
                "count",
                GIT_HISTORY_PROVIDER_ID,
                "module",
                module.module_id,
            )
        )
    return ModuleEngineeringProfile(
        module.module_id,
        module.path_namespace_id,
        _complexity_dimension(module),
        evidence.coverage_by_module.get(
            module.module_id,
            _dimension(
                "coverage",
                (),
                reason=coverage.reason if coverage is not None else "coverage_not_recorded",
                not_recorded=coverage is None,
                provenance=("pytest-coverage-trusted-deep",),
            ),
        ),
        _dimension(
            "mutation",
            evidence.mutation_metrics.get(module.module_id, ()),
            reason=(
                evidence.mutation.reason
                if evidence.mutation is not None
                else "mutation_not_recorded"
            ),
            not_recorded=evidence.mutation is None,
            provenance=(MUTATION_PROVIDER_ID,),
            limitations=("mutation_score_is_not_defect_probability",),
        ),
        _dimension(
            "history",
            history_values,
            reason=(
                evidence.history.reason if evidence.history is not None else "history_not_recorded"
            ),
            not_recorded=evidence.history is None,
            provenance=(GIT_HISTORY_PROVIDER_ID,),
            limitations=("history_and_cochange_are_not_defect_probability",),
        ),
        _graph_dimension(module),
    )


def _engineering_profiles(
    architecture: CodeArchitectureAnalysis | None,
    coverage: CodeCoverageAnalysis | None,
    evidence: _EngineeringEvidence,
) -> tuple[ModuleEngineeringProfile, ...]:
    modules = () if architecture is None or architecture.status != "ready" else architecture.modules
    if len(modules) > CODE_ENGINEERING_MODULE_LIMIT:
        raise ValueError("engineering module bound exceeded")
    return tuple(_module_engineering_profile(module, coverage, evidence) for module in modules)


def _engineering_status(
    architecture: CodeArchitectureAnalysis | None,
    coverage: CodeCoverageAnalysis | None,
    summaries: Sequence[EngineeringProviderSummary],
) -> tuple[EngineeringStatus, str | None]:
    if architecture is None or architecture.status != "ready":
        return "abstained", "architecture_not_ready"
    if (
        sum(item.status == "ready" for item in summaries) == len(summaries)
        and coverage is not None
        and coverage.status == "ready"
    ):
        return "ready", None
    return "partial", "one_or_more_engineering_dimensions_not_ready"


def _engineering_mutation_score(
    mutation_metrics: Mapping[str, Sequence[EngineeringMetric]],
) -> float | None:
    return next(
        (
            item.value
            for values in mutation_metrics.values()
            for item in values
            if item.name == "mutation_score"
        ),
        None,
    )


def _engineering_digest(
    *,
    status: EngineeringStatus,
    reason: str | None,
    summaries: Sequence[EngineeringProviderSummary],
    profiles: Sequence[ModuleEngineeringProfile],
    gates: Sequence[EngineeringGate],
    mutation_scope_signature: str | None,
    mutation_score: float | None,
    limitations: Sequence[str],
) -> str:
    payload = {
        "status": status,
        "reason": reason,
        "providers": [asdict(item) for item in summaries],
        "modules": [asdict(item) for item in profiles],
        "gates": [asdict(item) for item in gates],
        "mutation_scope_signature": mutation_scope_signature,
        "mutation_score": mutation_score,
        "limitations": limitations,
    }
    return "code-engineering-v2:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


def analyze_code_engineering(
    architecture: CodeArchitectureAnalysis | None,
    coverage: CodeCoverageAnalysis | None,
    providers: Mapping[str, ExternalProviderEvidence],
    *,
    database: str = "",
    analysis_run_id: int | None = None,
) -> CodeEngineeringAnalytics:
    """Correlate dimensions by stable module key without an aggregate score."""

    evidence = _engineering_evidence(coverage, providers)
    profiles = _engineering_profiles(architecture, coverage, evidence)
    summaries = tuple(
        _provider_summary(provider_id, providers.get(provider_id))
        for provider_id in (GIT_HISTORY_PROVIDER_ID, MUTATION_PROVIDER_ID)
    )
    gates = _mutation_gates(evidence.mutation, evidence.mutation_metrics)
    status, reason = _engineering_status(architecture, coverage, summaries)
    limitations = (
        "dimensions_are_correlated_by_module_identity_not_statistical_causation",
        "no_aggregate_score_is_computed",
        "no_dimension_is_a_defect_probability",
        "mutation_findings_are_advisory_and_have_zero_mutation_authority",
    )
    scope = evidence.mutation_metadata.get("measurement_scope_signature")
    mutation_scope_signature = scope if isinstance(scope, str) else None
    mutation_score = _engineering_mutation_score(evidence.mutation_metrics)
    digest = _engineering_digest(
        status=status,
        reason=reason,
        summaries=summaries,
        profiles=profiles,
        gates=gates,
        mutation_scope_signature=mutation_scope_signature,
        mutation_score=mutation_score,
        limitations=limitations,
    )
    return CodeEngineeringAnalytics(
        database,
        analysis_run_id,
        status,
        reason,
        summaries,
        profiles,
        gates,
        mutation_scope_signature,
        mutation_score,
        limitations,
        digest,
    )


def read_code_engineering_analysis(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    database: str = "",
    architecture: CodeArchitectureAnalysis | None = None,
    coverage: CodeCoverageAnalysis | None = None,
) -> CodeEngineeringAnalytics:
    """Read one already-published run without executing tools or changing state.

    Status/review consumers may pass architecture and coverage projections they
    already read from the same SQLite snapshot.  Omitting either projection
    preserves the standalone reader contract.
    """

    if architecture is None:
        architecture = read_code_architecture_analysis(
            connection,
            analysis_run_id,
            database=database,
        )
    if coverage is None:
        coverage = read_code_coverage_analysis(
            connection,
            analysis_run_id,
            database=database,
        )
    providers = read_external_provider_evidence(
        connection,
        analysis_run_id,
        provider_ids=(GIT_HISTORY_PROVIDER_ID, MUTATION_PROVIDER_ID),
    )
    return analyze_code_engineering(
        architecture,
        coverage,
        providers,
        database=database,
        analysis_run_id=analysis_run_id,
    )


def engineering_profile_for_module(
    analysis: CodeEngineeringAnalytics,
    module_id: str,
) -> ModuleEngineeringProfile | None:
    """Return one exact module profile for status/review/work-package consumers."""

    return next((item for item in analysis.modules if item.module_id == module_id), None)


__all__ = [
    "CODE_ENGINEERING_ANALYTICS_SCHEMA",
    "GIT_HISTORY_PROVIDER_ID",
    "MUTATION_PROVIDER_ID",
    "CodeEngineeringAnalytics",
    "EngineeringDimension",
    "EngineeringGate",
    "EngineeringMetric",
    "EngineeringProviderSummary",
    "ModuleEngineeringProfile",
    "analyze_code_engineering",
    "engineering_profile_for_module",
    "read_code_engineering_analysis",
]
