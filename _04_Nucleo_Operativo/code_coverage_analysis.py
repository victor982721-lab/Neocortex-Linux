"""Bounded public coverage view over normalized trusted-deep evidence.

This module does not execute tests or read source files.  It interprets one
already-published ``pytest-coverage-trusted-deep`` provider result, preserves
the selected suite independently from measurement completeness, and fails
closed when persisted evidence is malformed.  Coverage dimensions remain
separate; no aggregate quality or defect score is produced.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from typing import Literal, Protocol, cast

from .external_evidence_models import ExternalProviderEvidence
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_evidence,
)

CODE_COVERAGE_SCHEMA = "neocortex.code-coverage-analysis/v2"
CODE_COVERAGE_PROVIDER_ID = "pytest-coverage-trusted-deep"

CODE_COVERAGE_METRIC_LIMIT = 250_000
CODE_COVERAGE_RELATION_LIMIT = 250_000
CODE_COVERAGE_FINDING_LIMIT = 25_000
CODE_COVERAGE_MODULE_LIMIT = 25_000
CODE_COVERAGE_SYMBOL_LIMIT = 100_000
CODE_COVERAGE_DETAIL_LIMIT = 25_000
CODE_COVERAGE_TESTS_PER_SYMBOL_LIMIT = 10_000
CODE_COVERAGE_STRING_LIMIT = 8_192
CODE_COVERAGE_TOOL_VERSION_LIMIT = 32
CODE_COVERAGE_COUNT_LIMIT = 1_000_000_000

CoverageStatus = Literal["ready", "abstained"]
CoverageSuiteSelection = Literal["full", "selected"]
CoverageGateStatus = Literal["passed", "failed", "not_evaluated"]
CoverageGateName = Literal[
    "tests_passed",
    "coverage_available",
    "work_package_target_executed_by_passing_suite",
    "line_coverage_not_degraded",
    "branch_coverage_not_degraded",
]


@dataclass(frozen=True, slots=True)
class CoverageGateEvaluation:
    gate: CoverageGateName
    status: CoverageGateStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class CoverageToolVersion:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class CoverageTestOutcomes:
    collected: int
    selected: int
    passed: int
    failed: int
    skipped: int


@dataclass(frozen=True, slots=True)
class CoverageTotals:
    executable_lines: int
    covered_lines: int
    missing_lines: int
    branch_exits: int
    covered_branch_exits: int
    missing_branch_exits: int
    line_coverage_percent: float | None
    branch_coverage_percent: float | None


@dataclass(frozen=True, slots=True)
class CoverageScopeSummary:
    subject_kind: Literal["module", "symbol"]
    subject_key: str
    module_key: str | None
    symbol_key: str | None
    qualified_name: str | None
    start_line: int | None
    end_line: int | None
    relative_path: str | None
    totals: CoverageTotals
    missing_line_ranges: tuple[tuple[int, int], ...]
    missing_branch_arcs: tuple[tuple[int, int], ...]
    missing_line_ranges_truncated: bool
    missing_branch_arcs_truncated: bool
    executing_tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestToSymbolRelation:
    relation_id: str
    test_key: str
    production_symbol: str
    test_nodeids: tuple[str, ...]
    lines: tuple[int, ...]
    contexts: tuple[str, ...]
    relative_path: str | None
    module_key: str | None
    symbol_key: str | None


@dataclass(frozen=True, slots=True)
class CodeCoverageAnalysis:
    database: str
    analysis_run_id: int | None
    provider_id: str
    tool_run_id: int | None
    effective_tool_run_id: int | None
    status: CoverageStatus
    reason: str | None
    suite_selection: CoverageSuiteSelection | None
    measurement_complete: bool | None
    content_executed: bool | None
    tool_versions: tuple[CoverageToolVersion, ...]
    suite_signature: str | None
    configuration_signature: str | None
    measurement_scope_signature: str | None
    outcomes: CoverageTestOutcomes | None
    totals: CoverageTotals | None
    modules: tuple[CoverageScopeSummary, ...]
    symbols: tuple[CoverageScopeSummary, ...]
    test_relations: tuple[TestToSymbolRelation, ...]
    failed_test_nodeids: tuple[str, ...]
    gates: tuple[CoverageGateEvaluation, ...]
    limitations: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": "code-coverage-analysis",
            "schema": CODE_COVERAGE_SCHEMA,
            **asdict(self),
        }

    def digest_payload(self) -> dict[str, object]:
        """Return replay-stable evidence without database-local identities."""

        payload = self.as_payload()
        payload.pop("database")
        payload.pop("analysis_run_id")
        payload.pop("tool_run_id")
        payload.pop("effective_tool_run_id")
        return payload


@dataclass(frozen=True, slots=True)
class CoverageComparison:
    status: Literal["comparable", "not_comparable"]
    reason: str | None
    baseline_suite_signature: str | None
    current_suite_signature: str | None
    executable_lines_delta: int | None
    covered_lines_delta: int | None
    missing_lines_delta: int | None
    branch_exits_delta: int | None
    covered_branch_exits_delta: int | None
    missing_branch_exits_delta: int | None
    line_coverage_percent_delta: float | None
    branch_coverage_percent_delta: float | None
    gates: tuple[CoverageGateEvaluation, ...]


@dataclass(frozen=True, slots=True)
class WorkPackageCoverageProjection:
    primary_symbol: str
    status: Literal["executed", "not_executed", "not_evaluated"]
    executing_tests: tuple[str, ...]
    relation_ids: tuple[str, ...]
    gate: CoverageGateEvaluation


class _MetricEvidence(Protocol):
    subject_kind: str
    subject_key: str
    category: str
    metric_name: str
    value: float
    unit: str
    metadata: Mapping[str, object]


class _RelationEvidence(Protocol):
    portable_relation_id: str
    relation_kind: str
    source_kind: str
    source_key: str
    target_kind: str
    target_key: str
    metadata: Mapping[str, object]


class _FindingEvidence(Protocol):
    category: str
    metadata: Mapping[str, object]


class _ProviderEvidence(Protocol):
    provider_id: str
    tool_run_id: int
    effective_tool_run_id: int | None
    status: str
    reason: str | None

    @property
    def findings(self) -> Sequence[_FindingEvidence]: ...

    @property
    def metrics(self) -> Sequence[_MetricEvidence]: ...

    @property
    def relations(self) -> Sequence[_RelationEvidence]: ...


@dataclass(frozen=True, slots=True)
class _RunContext:
    suite_selection: CoverageSuiteSelection
    measurement_complete: bool
    content_executed: bool
    tool_versions: tuple[CoverageToolVersion, ...]
    suite_signature: str
    configuration_signature: str
    measurement_scope_signature: str
    limitations: tuple[str, ...]


_COUNT_ALIASES = {
    "line_total": "executable_lines",
    "executable_lines": "executable_lines",
    "line_covered": "covered_lines",
    "covered_lines": "covered_lines",
    "line_missing": "missing_lines",
    "missing_lines": "missing_lines",
    "branch_total": "branch_exits",
    "branches": "branch_exits",
    "branch_exits": "branch_exits",
    "branch_covered": "covered_branch_exits",
    "covered_branches": "covered_branch_exits",
    "covered_branch_exits": "covered_branch_exits",
    "branch_missing": "missing_branch_exits",
    "missing_branches": "missing_branch_exits",
    "missing_branch_exits": "missing_branch_exits",
    "tests_collected": "tests_collected",
    "tests_selected": "tests_selected",
    "tests_passed": "tests_passed",
    "tests_failed": "tests_failed",
    "tests_skipped": "tests_skipped",
    "shards_total": "shards_total",
    "shards_reused": "shards_reused",
}
_RATIO_ALIASES = {
    "line_rate": "line_coverage_percent",
    "branch_rate": "branch_coverage_percent",
}
_PERCENT_ALIASES = {
    "line_coverage_percent": "line_coverage_percent",
    "branch_coverage_percent": "branch_coverage_percent",
}
_COVERAGE_COUNTS = (
    "executable_lines",
    "covered_lines",
    "missing_lines",
    "branch_exits",
    "covered_branch_exits",
    "missing_branch_exits",
)
_RUN_COUNTS = (
    "tests_collected",
    "tests_selected",
    "tests_passed",
    "tests_failed",
    "tests_skipped",
    "shards_total",
    "shards_reused",
)


class _CoverageEvidenceError(ValueError):
    pass


def _bounded_string(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _CoverageEvidenceError(f"{name}_not_string")
    if (not value and not allow_empty) or len(value) > CODE_COVERAGE_STRING_LIMIT:
        raise _CoverageEvidenceError(f"{name}_invalid_length")
    return value


def _bounded_string_sequence(
    value: object,
    name: str,
    *,
    limit: int = CODE_COVERAGE_DETAIL_LIMIT,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _CoverageEvidenceError(f"{name}_not_sequence")
    if len(value) > limit:
        raise _CoverageEvidenceError(f"{name}_bound_exceeded")
    result = tuple(_bounded_string(item, name) for item in value)
    if len(set(result)) != len(result):
        raise _CoverageEvidenceError(f"{name}_contains_duplicates")
    return tuple(sorted(result))


def _optional_string(metadata: Mapping[str, object], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    return _bounded_string(value, name)


def _required_bool(metadata: Mapping[str, object], name: str) -> bool:
    value = metadata.get(name)
    if not isinstance(value, bool):
        raise _CoverageEvidenceError(f"{name}_not_boolean")
    return value


def _optional_bool(metadata: Mapping[str, object], name: str) -> bool:
    value = metadata.get(name, False)
    if not isinstance(value, bool):
        raise _CoverageEvidenceError(f"{name}_not_boolean")
    return value


def _required_positive_int(metadata: Mapping[str, object], name: str) -> int:
    value = metadata.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _CoverageEvidenceError(f"{name}_not_positive_integer")
    return value


def _tool_versions(value: object) -> tuple[CoverageToolVersion, ...]:
    if not isinstance(value, Mapping):
        raise _CoverageEvidenceError("tool_versions_not_mapping")
    if not value or len(value) > CODE_COVERAGE_TOOL_VERSION_LIMIT:
        raise _CoverageEvidenceError("tool_versions_invalid_size")
    versions = tuple(
        CoverageToolVersion(
            _bounded_string(name, "tool_name"),
            _bounded_string(version, "tool_version"),
        )
        for name, version in sorted(value.items(), key=lambda item: str(item[0]))
    )
    if len({item.name for item in versions}) != len(versions):
        raise _CoverageEvidenceError("tool_versions_duplicate_name")
    return versions


def _limitations(metadata: Mapping[str, object]) -> tuple[str, ...]:
    raw = metadata.get("limitations", ())
    if raw == ():
        return ()
    return _bounded_string_sequence(raw, "limitations", limit=100)


def _run_context(metadata: Mapping[str, object]) -> _RunContext:
    selection = metadata.get("suite_selection")
    if selection not in {"full", "selected"}:
        raise _CoverageEvidenceError("suite_selection_invalid")
    return _RunContext(
        cast(CoverageSuiteSelection, selection),
        _required_bool(metadata, "measurement_complete"),
        _required_bool(metadata, "content_executed"),
        _tool_versions(metadata.get("tool_versions")),
        _bounded_string(metadata.get("suite_signature"), "suite_signature"),
        _bounded_string(
            metadata.get("configuration_signature"),
            "configuration_signature",
        ),
        _bounded_string(
            metadata.get("measurement_scope_signature"),
            "measurement_scope_signature",
        ),
        _limitations(metadata),
    )


def _metric_value(metric: _MetricEvidence) -> tuple[str, float] | None:
    name = _bounded_string(metric.metric_name, "metric_name")
    if name not in _COUNT_ALIASES and name not in _RATIO_ALIASES and name not in _PERCENT_ALIASES:
        return None
    if metric.category != "coverage":
        raise _CoverageEvidenceError("coverage_metric_category_invalid")
    if isinstance(metric.value, bool):
        raise _CoverageEvidenceError("coverage_metric_value_boolean")
    try:
        value = float(metric.value)
    except (TypeError, ValueError) as exc:
        raise _CoverageEvidenceError("coverage_metric_value_invalid") from exc
    if not math.isfinite(value) or value < 0:
        raise _CoverageEvidenceError("coverage_metric_value_invalid")
    unit = _bounded_string(metric.unit, "metric_unit")
    if name in _COUNT_ALIASES:
        if unit != "count" or not value.is_integer():
            raise _CoverageEvidenceError("coverage_count_metric_invalid")
        return _COUNT_ALIASES[name], value
    if name in _RATIO_ALIASES:
        if unit != "ratio" or value > 1:
            raise _CoverageEvidenceError("coverage_ratio_metric_invalid")
        return _RATIO_ALIASES[name], value * 100.0
    if unit != "percent" or value > 100:
        raise _CoverageEvidenceError("coverage_percent_metric_invalid")
    return _PERCENT_ALIASES[name], value


def _metric_groups(
    metrics: Sequence[_MetricEvidence],
) -> dict[tuple[str, str], tuple[_MetricEvidence, ...]]:
    if len(metrics) > CODE_COVERAGE_METRIC_LIMIT:
        raise _CoverageEvidenceError("provider_metric_bound_exceeded")
    groups: dict[tuple[str, str], list[_MetricEvidence]] = {}
    for metric in metrics:
        kind = _bounded_string(metric.subject_kind, "metric_subject_kind")
        if kind not in {"run", "file", "module", "symbol"}:
            if _metric_value(metric) is not None:
                raise _CoverageEvidenceError("coverage_metric_subject_kind_invalid")
            continue
        key = _bounded_string(metric.subject_key, "metric_subject_key")
        groups.setdefault((kind, key), []).append(metric)
    return {key: tuple(value) for key, value in groups.items()}


def _logical_metrics(metrics: Sequence[_MetricEvidence]) -> dict[str, float]:
    values: dict[str, float] = {}
    raw_names: set[str] = set()
    for metric in metrics:
        raw_name = _bounded_string(metric.metric_name, "metric_name")
        if raw_name in raw_names:
            raise _CoverageEvidenceError("duplicate_coverage_metric")
        raw_names.add(raw_name)
        normalized = _metric_value(metric)
        if normalized is None:
            continue
        name, value = normalized
        prior = values.setdefault(name, value)
        if not math.isclose(prior, value, rel_tol=0, abs_tol=1e-6):
            raise _CoverageEvidenceError(f"conflicting_metric_alias:{name}")
    return values


def _required_count(values: Mapping[str, float], name: str) -> int:
    value = values.get(name)
    if value is None or value < 0 or value > CODE_COVERAGE_COUNT_LIMIT or not value.is_integer():
        raise _CoverageEvidenceError(f"required_count_missing_or_invalid:{name}")
    return int(value)


def _coverage_totals(values: Mapping[str, float]) -> CoverageTotals:
    counts = {name: _required_count(values, name) for name in _COVERAGE_COUNTS}
    if counts["covered_lines"] + counts["missing_lines"] != counts["executable_lines"]:
        raise _CoverageEvidenceError("line_coverage_counts_inconsistent")
    if counts["covered_branch_exits"] + counts["missing_branch_exits"] != counts["branch_exits"]:
        raise _CoverageEvidenceError("branch_coverage_counts_inconsistent")
    line_percent = _validated_percent(
        values.get("line_coverage_percent"),
        counts["covered_lines"],
        counts["executable_lines"],
        "line_coverage_percent",
    )
    branch_percent = _validated_percent(
        values.get("branch_coverage_percent"),
        counts["covered_branch_exits"],
        counts["branch_exits"],
        "branch_coverage_percent",
    )
    return CoverageTotals(
        counts["executable_lines"],
        counts["covered_lines"],
        counts["missing_lines"],
        counts["branch_exits"],
        counts["covered_branch_exits"],
        counts["missing_branch_exits"],
        line_percent,
        branch_percent,
    )


def _validated_percent(
    observed: float | None,
    covered: int,
    total: int,
    name: str,
) -> float | None:
    if total == 0:
        return None
    expected = covered * 100.0 / total
    if observed is not None and not math.isclose(observed, expected, rel_tol=0, abs_tol=1e-4):
        raise _CoverageEvidenceError(f"{name}_inconsistent")
    return expected


def _context_for_group(metrics: Sequence[_MetricEvidence]) -> _RunContext:
    contexts: list[_RunContext] = []
    for metric in metrics:
        if not isinstance(metric.metadata, Mapping):
            raise _CoverageEvidenceError("metric_metadata_not_mapping")
        contexts.append(_run_context(metric.metadata))
    if not contexts:
        raise _CoverageEvidenceError("coverage_metric_group_empty")
    if any(item != contexts[0] for item in contexts[1:]):
        raise _CoverageEvidenceError("coverage_metric_context_conflict")
    return contexts[0]


def _integer_pairs(
    value: object,
    name: str,
    *,
    positive: bool,
) -> tuple[tuple[int, int], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _CoverageEvidenceError(f"{name}_not_sequence")
    if len(value) > CODE_COVERAGE_DETAIL_LIMIT:
        raise _CoverageEvidenceError(f"{name}_bound_exceeded")
    pairs: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
            raise _CoverageEvidenceError(f"{name}_item_invalid")
        start, end = item
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise _CoverageEvidenceError(f"{name}_item_invalid")
        if abs(start) > CODE_COVERAGE_COUNT_LIMIT or abs(end) > CODE_COVERAGE_COUNT_LIMIT:
            raise _CoverageEvidenceError(f"{name}_item_invalid")
        if positive and (start < 1 or end < start):
            raise _CoverageEvidenceError(f"{name}_item_invalid")
        pairs.append((start, end))
    if len(set(pairs)) != len(pairs):
        raise _CoverageEvidenceError(f"{name}_contains_duplicates")
    result = tuple(sorted(pairs))
    if positive:
        for prior, current in pairwise(result):
            if current[0] <= prior[1]:
                raise _CoverageEvidenceError(f"{name}_overlaps")
    return result


def _scope_metadata(
    metrics: Sequence[_MetricEvidence],
    kind: Literal["module", "symbol"],
    key: str,
) -> tuple[
    str | None,
    str | None,
    str | None,
    int | None,
    int | None,
    str | None,
    tuple[tuple[int, int], ...],
    tuple[tuple[int, int], ...],
    bool,
    bool,
]:
    parsed = []
    for metric in metrics:
        metadata = metric.metadata
        if not isinstance(metadata, Mapping):
            raise _CoverageEvidenceError("metric_metadata_not_mapping")
        parsed.append(
            (
                _optional_string(metadata, "module_key"),
                _optional_string(metadata, "symbol_key"),
                _optional_string(metadata, "qualified_name"),
                (_required_positive_int(metadata, "start_line") if kind == "symbol" else None),
                (_required_positive_int(metadata, "end_line") if kind == "symbol" else None),
                _optional_string(metadata, "relative_path"),
                _integer_pairs(
                    metadata.get("missing_line_ranges", ()),
                    "missing_line_ranges",
                    positive=True,
                ),
                _integer_pairs(
                    metadata.get("missing_branch_arcs", ()),
                    "missing_branch_arcs",
                    positive=False,
                ),
                _optional_bool(metadata, "missing_line_ranges_truncated"),
                _optional_bool(metadata, "missing_branch_arcs_truncated"),
            )
        )
    if not parsed or any(item != parsed[0] for item in parsed[1:]):
        raise _CoverageEvidenceError("coverage_scope_metadata_conflict")
    (
        module_key,
        symbol_key,
        qualified_name,
        start_line,
        end_line,
        relative_path,
        line_ranges,
        branch_arcs,
        line_cut,
        branch_cut,
    ) = parsed[0]
    if kind == "module" and module_key != key:
        raise _CoverageEvidenceError("coverage_module_identity_mismatch")
    if kind == "symbol" and symbol_key != key:
        raise _CoverageEvidenceError("coverage_symbol_identity_mismatch")
    if module_key is None or relative_path is None:
        raise _CoverageEvidenceError("coverage_scope_identity_incomplete")
    if kind == "symbol":
        if qualified_name is None or start_line is None or end_line is None:
            raise _CoverageEvidenceError("coverage_symbol_identity_incomplete")
        if end_line < start_line:
            raise _CoverageEvidenceError("coverage_symbol_line_range_invalid")
        expected_key = f"{module_key}:{qualified_name}:{start_line}:{end_line}"
        if key != expected_key:
            raise _CoverageEvidenceError("coverage_symbol_key_components_disagree")
    return (
        module_key,
        symbol_key,
        qualified_name,
        start_line,
        end_line,
        relative_path,
        line_ranges,
        branch_arcs,
        line_cut,
        branch_cut,
    )


def _scope_summary(
    kind: Literal["module", "symbol"],
    key: str,
    metrics: Sequence[_MetricEvidence],
) -> CoverageScopeSummary:
    values = _logical_metrics(metrics)
    totals = _coverage_totals(values)
    metadata = _scope_metadata(metrics, kind, key)
    return CoverageScopeSummary(
        kind,
        key,
        metadata[0],
        metadata[1],
        metadata[2],
        metadata[3],
        metadata[4],
        metadata[5],
        totals,
        metadata[6],
        metadata[7],
        metadata[8],
        metadata[9],
        (),
    )


def _run_data(
    groups: Mapping[tuple[str, str], tuple[_MetricEvidence, ...]],
) -> tuple[_RunContext, CoverageTestOutcomes, CoverageTotals]:
    run_groups = [(key, value) for (kind, key), value in groups.items() if kind == "run"]
    if len(run_groups) != 1:
        raise _CoverageEvidenceError("coverage_run_metric_scope_invalid")
    _, metrics = run_groups[0]
    context = _context_for_group(metrics)
    values = _logical_metrics(metrics)
    counts = {name: _required_count(values, name) for name in _RUN_COUNTS}
    if counts["tests_selected"] > counts["tests_collected"]:
        raise _CoverageEvidenceError("selected_tests_exceed_collected_tests")
    accounted = counts["tests_passed"] + counts["tests_failed"] + counts["tests_skipped"]
    if accounted != counts["tests_selected"]:
        raise _CoverageEvidenceError("test_outcomes_do_not_match_selected_tests")
    if counts["shards_reused"] > counts["shards_total"]:
        raise _CoverageEvidenceError("reused_shards_exceed_total_shards")
    outcomes = CoverageTestOutcomes(
        counts["tests_collected"],
        counts["tests_selected"],
        counts["tests_passed"],
        counts["tests_failed"],
        counts["tests_skipped"],
    )
    return context, outcomes, _coverage_totals(values)


def _scope_summaries(
    groups: Mapping[tuple[str, str], tuple[_MetricEvidence, ...]],
    context: _RunContext,
) -> tuple[tuple[CoverageScopeSummary, ...], tuple[CoverageScopeSummary, ...]]:
    modules: list[CoverageScopeSummary] = []
    symbols: list[CoverageScopeSummary] = []
    for (kind, key), metrics in sorted(groups.items()):
        if kind not in {"module", "symbol"}:
            continue
        if _context_for_group(metrics) != context:
            raise _CoverageEvidenceError("coverage_scope_context_mismatch")
        summary = _scope_summary(kind, key, metrics)  # type: ignore[arg-type]
        (modules if kind == "module" else symbols).append(summary)
    if len(modules) > CODE_COVERAGE_MODULE_LIMIT:
        raise _CoverageEvidenceError("coverage_module_bound_exceeded")
    if len(symbols) > CODE_COVERAGE_SYMBOL_LIMIT:
        raise _CoverageEvidenceError("coverage_symbol_bound_exceeded")
    return tuple(modules), tuple(symbols)


def _validate_file_scopes(
    groups: Mapping[tuple[str, str], tuple[_MetricEvidence, ...]],
    context: _RunContext,
) -> None:
    for (kind, key), metrics in groups.items():
        if kind != "file":
            continue
        if _context_for_group(metrics) != context:
            raise _CoverageEvidenceError("coverage_file_context_mismatch")
        _coverage_totals(_logical_metrics(metrics))
        parsed = []
        for metric in metrics:
            metadata = metric.metadata
            if not isinstance(metadata, Mapping):
                raise _CoverageEvidenceError("metric_metadata_not_mapping")
            parsed.append(
                (
                    _optional_string(metadata, "relative_path"),
                    _integer_pairs(
                        metadata.get("missing_line_ranges", ()),
                        "missing_line_ranges",
                        positive=True,
                    ),
                    _integer_pairs(
                        metadata.get("missing_branch_arcs", ()),
                        "missing_branch_arcs",
                        positive=False,
                    ),
                    _optional_bool(metadata, "missing_line_ranges_truncated"),
                    _optional_bool(metadata, "missing_branch_arcs_truncated"),
                )
            )
        if not parsed or any(item != parsed[0] for item in parsed[1:]):
            raise _CoverageEvidenceError("coverage_file_metadata_conflict")
        if parsed[0][0] != key:
            raise _CoverageEvidenceError("coverage_file_identity_mismatch")


def _relation_metadata(
    relation: _RelationEvidence,
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[str, ...], str | None, str | None, str | None]:
    metadata = relation.metadata
    if not isinstance(metadata, Mapping):
        raise _CoverageEvidenceError("coverage_relation_metadata_not_mapping")
    nodeids = _bounded_string_sequence(
        metadata.get("test_nodeids"),
        "test_nodeids",
        limit=CODE_COVERAGE_TESTS_PER_SYMBOL_LIMIT,
    )
    raw_lines = metadata.get("lines")
    if isinstance(raw_lines, (str, bytes)) or not isinstance(raw_lines, Sequence):
        raise _CoverageEvidenceError("coverage_relation_lines_not_sequence")
    if len(raw_lines) > CODE_COVERAGE_DETAIL_LIMIT:
        raise _CoverageEvidenceError("coverage_relation_lines_bound_exceeded")
    lines: list[int] = []
    for line in raw_lines:
        if (
            isinstance(line, bool)
            or not isinstance(line, int)
            or line < 1
            or line > CODE_COVERAGE_COUNT_LIMIT
        ):
            raise _CoverageEvidenceError("coverage_relation_line_invalid")
        lines.append(line)
    if len(set(lines)) != len(lines):
        raise _CoverageEvidenceError("coverage_relation_lines_duplicate")
    contexts = _bounded_string_sequence(
        metadata.get("contexts"),
        "coverage_contexts",
        limit=CODE_COVERAGE_DETAIL_LIMIT,
    )
    return (
        nodeids,
        tuple(sorted(lines)),
        contexts,
        _optional_string(metadata, "relative_path"),
        _optional_string(metadata, "module_key"),
        _optional_string(metadata, "symbol_key"),
    )


def _test_relations(
    relations: Sequence[_RelationEvidence],
    symbols: tuple[CoverageScopeSummary, ...],
) -> tuple[TestToSymbolRelation, ...]:
    if len(relations) > CODE_COVERAGE_RELATION_LIMIT:
        raise _CoverageEvidenceError("provider_relation_bound_exceeded")
    symbol_ids = {item.subject_key for item in symbols}
    result: list[TestToSymbolRelation] = []
    relation_ids: set[str] = set()
    endpoints: set[tuple[str, str]] = set()
    for relation in relations:
        if relation.relation_kind != "test_covers_symbol":
            continue
        if relation.source_kind != "symbol" or relation.target_kind != "symbol":
            raise _CoverageEvidenceError("test_coverage_relation_endpoint_kind_invalid")
        relation_id = _bounded_string(relation.portable_relation_id, "coverage_relation_id")
        test_key = _bounded_string(relation.source_key, "coverage_test_key")
        target = _bounded_string(relation.target_key, "coverage_target_symbol")
        if not test_key.startswith("pytest-nodeid:") or len(test_key) == len("pytest-nodeid:"):
            raise _CoverageEvidenceError("coverage_test_key_invalid")
        if target not in symbol_ids:
            raise _CoverageEvidenceError("coverage_relation_target_not_measured")
        metadata = _relation_metadata(relation)
        source_nodeid = test_key.removeprefix("pytest-nodeid:")
        if metadata[0] != (source_nodeid,):
            raise _CoverageEvidenceError("coverage_relation_test_identity_mismatch")
        if metadata[5] != target or metadata[4] is None or metadata[3] is None:
            raise _CoverageEvidenceError("coverage_relation_target_metadata_mismatch")
        endpoint = (test_key, target)
        if relation_id in relation_ids or endpoint in endpoints:
            raise _CoverageEvidenceError("duplicate_test_coverage_relation")
        relation_ids.add(relation_id)
        endpoints.add(endpoint)
        result.append(
            TestToSymbolRelation(
                relation_id,
                test_key,
                target,
                metadata[0],
                metadata[1],
                metadata[2],
                metadata[3],
                metadata[4],
                metadata[5],
            )
        )
    result.sort(key=lambda item: (item.production_symbol, item.test_key, item.relation_id))
    return tuple(result)


def _executing_tests(
    symbols: tuple[CoverageScopeSummary, ...],
    relations: tuple[TestToSymbolRelation, ...],
) -> tuple[CoverageScopeSummary, ...]:
    by_symbol: dict[str, set[str]] = {item.subject_key: set() for item in symbols}
    for relation in relations:
        by_symbol[relation.production_symbol].update(relation.test_nodeids)
    if any(len(values) > CODE_COVERAGE_TESTS_PER_SYMBOL_LIMIT for values in by_symbol.values()):
        raise _CoverageEvidenceError("executing_tests_bound_exceeded")
    return tuple(
        replace(item, executing_tests=tuple(sorted(by_symbol[item.subject_key])))
        for item in symbols
    )


def _failed_test_nodeids(findings: Sequence[_FindingEvidence]) -> tuple[str, ...]:
    if len(findings) > CODE_COVERAGE_FINDING_LIMIT:
        raise _CoverageEvidenceError("provider_finding_bound_exceeded")
    nodeids: set[str] = set()
    for finding in findings:
        if finding.category != "test_failure":
            continue
        if not isinstance(finding.metadata, Mapping):
            raise _CoverageEvidenceError("coverage_finding_metadata_not_mapping")
        raw = finding.metadata.get("nodeid")
        if raw is not None:
            nodeids.add(_bounded_string(raw, "failed_test_nodeid"))
    if len(nodeids) > CODE_COVERAGE_FINDING_LIMIT:
        raise _CoverageEvidenceError("failed_test_nodeid_bound_exceeded")
    return tuple(sorted(nodeids))


def _analysis_gates(
    context: _RunContext,
    outcomes: CoverageTestOutcomes,
    totals: CoverageTotals,
) -> tuple[CoverageGateEvaluation, ...]:
    if not context.measurement_complete:
        reason = "coverage_measurement_incomplete"
        return (
            CoverageGateEvaluation("tests_passed", "not_evaluated", reason),
            CoverageGateEvaluation("coverage_available", "not_evaluated", reason),
        )
    if not context.content_executed:
        reason = "trusted_deep_content_not_executed"
        return (
            CoverageGateEvaluation("tests_passed", "not_evaluated", reason),
            CoverageGateEvaluation("coverage_available", "not_evaluated", reason),
        )
    if outcomes.selected == 0:
        tests_gate = CoverageGateEvaluation("tests_passed", "failed", "no_tests_selected")
    elif outcomes.failed:
        tests_gate = CoverageGateEvaluation(
            "tests_passed",
            "failed",
            f"selected_tests_failed:{outcomes.failed}",
        )
    elif outcomes.passed == 0:
        tests_gate = CoverageGateEvaluation("tests_passed", "failed", "no_test_passed")
    else:
        tests_gate = CoverageGateEvaluation("tests_passed", "passed", None)
    coverage_gate = CoverageGateEvaluation(
        "coverage_available",
        "passed" if totals.executable_lines > 0 else "not_evaluated",
        None if totals.executable_lines > 0 else "no_executable_lines_measured",
    )
    return tests_gate, coverage_gate


def _not_evaluated_analysis_gates(reason: str) -> tuple[CoverageGateEvaluation, ...]:
    return (
        CoverageGateEvaluation("tests_passed", "not_evaluated", reason),
        CoverageGateEvaluation("coverage_available", "not_evaluated", reason),
    )


def _abstained(
    reason: str,
    *,
    database: str,
    analysis_run_id: int | None,
    evidence: ExternalProviderEvidence | _ProviderEvidence | None,
) -> CodeCoverageAnalysis:
    return CodeCoverageAnalysis(
        database,
        analysis_run_id,
        CODE_COVERAGE_PROVIDER_ID,
        None if evidence is None else getattr(evidence, "tool_run_id", None),
        None if evidence is None else getattr(evidence, "effective_tool_run_id", None),
        "abstained",
        reason,
        None,
        None,
        None,
        (),
        None,
        None,
        None,
        None,
        None,
        (),
        (),
        (),
        (),
        _not_evaluated_analysis_gates(reason),
        ("coverage_evidence_was_not_interpreted",),
    )


def analyze_code_coverage(
    evidence: ExternalProviderEvidence | _ProviderEvidence | None,
    *,
    database: str = "",
    analysis_run_id: int | None = None,
) -> CodeCoverageAnalysis:
    """Interpret one normalized trusted-deep provider result without I/O."""

    if evidence is None:
        return _abstained(
            "coverage_provider_missing",
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=None,
        )
    if evidence.provider_id != CODE_COVERAGE_PROVIDER_ID:
        return _abstained(
            "coverage_provider_identity_mismatch",
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=evidence,
        )
    if evidence.status != "ready" or evidence.effective_tool_run_id is None:
        reason = evidence.reason or "coverage_provider_not_ready"
        return _abstained(
            reason,
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=evidence,
        )
    try:
        groups = _metric_groups(cast(Sequence[_MetricEvidence], evidence.metrics))
        context, outcomes, totals = _run_data(groups)
        _validate_file_scopes(groups, context)
        modules, symbols = _scope_summaries(groups, context)
        relations = _test_relations(
            cast(Sequence[_RelationEvidence], evidence.relations),
            symbols,
        )
        symbols = _executing_tests(symbols, relations)
        failed_test_nodeids = _failed_test_nodeids(
            cast(Sequence[_FindingEvidence], evidence.findings)
        )
    except (AttributeError, KeyError, TypeError, _CoverageEvidenceError, ValueError) as exc:
        return _abstained(
            f"coverage_evidence_incompatible:{type(exc).__name__}:{exc}",
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=evidence,
        )
    if not context.content_executed:
        return _abstained(
            "trusted_deep_content_not_executed",
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=evidence,
        )
    limitations = set(context.limitations)
    limitations.update(
        {
            "coverage_observes_only_the_declared_test_suite_and_measurement_scope",
            "coverage_does_not_prove_behavioral_correctness",
            "coverage_evidence_is_advisory_and_has_no_mutation_authority",
        }
    )
    if context.suite_selection == "selected":
        limitations.add("selected_suite_is_not_claimed_as_full_project_coverage")
    if not context.measurement_complete:
        limitations.add("coverage_measurement_is_incomplete_and_not_comparable")
    return CodeCoverageAnalysis(
        database,
        analysis_run_id,
        CODE_COVERAGE_PROVIDER_ID,
        evidence.tool_run_id,
        evidence.effective_tool_run_id,
        "ready",
        None,
        context.suite_selection,
        context.measurement_complete,
        context.content_executed,
        context.tool_versions,
        context.suite_signature,
        context.configuration_signature,
        context.measurement_scope_signature,
        outcomes,
        totals,
        modules,
        symbols,
        relations,
        failed_test_nodeids,
        _analysis_gates(context, outcomes, totals),
        tuple(sorted(limitations)),
    )


def read_code_coverage_analysis(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    database: str = "",
) -> CodeCoverageAnalysis:
    """Read one already-published normalized provider result without mutation."""

    try:
        suite = read_external_evidence_suite(
            connection,
            analysis_run_id,
            enforce_current_runtime=True,
            provider_ids=(CODE_COVERAGE_PROVIDER_ID,),
        )
        suite_status = next(
            (item for item in suite.providers if item.provider_id == CODE_COVERAGE_PROVIDER_ID),
            None,
        )
        if suite_status is None:
            return _abstained(
                "coverage_provider_missing",
                database=database,
                analysis_run_id=analysis_run_id,
                evidence=None,
            )
        if suite_status.status != "ready":
            return _abstained(
                suite_status.reason or f"coverage_provider_{suite_status.status}",
                database=database,
                analysis_run_id=analysis_run_id,
                evidence=None,
            )
        evidence = read_external_provider_evidence(
            connection,
            analysis_run_id,
            provider_ids=(CODE_COVERAGE_PROVIDER_ID,),
        ).get(CODE_COVERAGE_PROVIDER_ID)
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
        return _abstained(
            f"coverage_evidence_unavailable:{type(exc).__name__}:{exc}",
            database=database,
            analysis_run_id=analysis_run_id,
            evidence=None,
        )
    return analyze_code_coverage(
        evidence,
        database=database,
        analysis_run_id=analysis_run_id,
    )


def _comparison_gates(reason: str) -> tuple[CoverageGateEvaluation, ...]:
    return (
        CoverageGateEvaluation("line_coverage_not_degraded", "not_evaluated", reason),
        CoverageGateEvaluation("branch_coverage_not_degraded", "not_evaluated", reason),
    )


def _not_comparable(
    baseline: CodeCoverageAnalysis,
    current: CodeCoverageAnalysis,
    reason: str,
) -> CoverageComparison:
    return CoverageComparison(
        "not_comparable",
        reason,
        baseline.suite_signature,
        current.suite_signature,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        _comparison_gates(reason),
    )


def compare_code_coverage(
    baseline: CodeCoverageAnalysis,
    current: CodeCoverageAnalysis,
) -> CoverageComparison:
    """Compare exact complete measurement scopes; otherwise abstain."""

    if baseline.status != "ready" or current.status != "ready":
        return _not_comparable(baseline, current, "coverage_provider_not_ready")
    if baseline.measurement_complete is not True or current.measurement_complete is not True:
        return _not_comparable(baseline, current, "coverage_measurement_incomplete")
    if baseline.content_executed is not True or current.content_executed is not True:
        return _not_comparable(baseline, current, "trusted_deep_content_not_executed")
    if baseline.tool_versions != current.tool_versions:
        return _not_comparable(baseline, current, "coverage_tool_versions_changed")
    signatures = (
        (baseline.suite_selection, current.suite_selection, "suite_selection_changed"),
        (baseline.suite_signature, current.suite_signature, "suite_signature_changed"),
        (
            baseline.configuration_signature,
            current.configuration_signature,
            "coverage_configuration_changed",
        ),
        (
            baseline.measurement_scope_signature,
            current.measurement_scope_signature,
            "measurement_scope_changed",
        ),
    )
    for before, after, reason in signatures:
        if before is None or after is None or before != after:
            return _not_comparable(baseline, current, reason)
    if baseline.totals is None or current.totals is None:
        return _not_comparable(baseline, current, "coverage_totals_missing")
    if (
        baseline.totals.line_coverage_percent is None
        or current.totals.line_coverage_percent is None
    ):
        return _not_comparable(baseline, current, "line_coverage_unavailable")
    line_delta = current.totals.line_coverage_percent - baseline.totals.line_coverage_percent
    line_gate = CoverageGateEvaluation(
        "line_coverage_not_degraded",
        "passed" if line_delta >= -1e-9 else "failed",
        None if line_delta >= -1e-9 else "line_coverage_decreased",
    )
    branch_delta: float | None = None
    if baseline.totals.branch_exits == 0:
        branch_gate = CoverageGateEvaluation(
            "branch_coverage_not_degraded",
            "not_evaluated",
            "no_branch_exits_in_measurement_scope",
        )
    else:
        before_branch = baseline.totals.branch_coverage_percent
        after_branch = current.totals.branch_coverage_percent
        if before_branch is None or after_branch is None:
            return _not_comparable(baseline, current, "branch_coverage_unavailable")
        branch_delta = after_branch - before_branch
        branch_gate = CoverageGateEvaluation(
            "branch_coverage_not_degraded",
            "passed" if branch_delta >= -1e-9 else "failed",
            None if branch_delta >= -1e-9 else "branch_coverage_decreased",
        )
    return CoverageComparison(
        "comparable",
        None,
        baseline.suite_signature,
        current.suite_signature,
        current.totals.executable_lines - baseline.totals.executable_lines,
        current.totals.covered_lines - baseline.totals.covered_lines,
        current.totals.missing_lines - baseline.totals.missing_lines,
        current.totals.branch_exits - baseline.totals.branch_exits,
        current.totals.covered_branch_exits - baseline.totals.covered_branch_exits,
        current.totals.missing_branch_exits - baseline.totals.missing_branch_exits,
        line_delta,
        branch_delta,
        (line_gate, branch_gate),
    )


def project_work_package_coverage(
    analysis: CodeCoverageAnalysis,
    primary_symbol: str,
) -> WorkPackageCoverageProjection:
    """Project one exact work-package target to tests and a typed gate."""

    try:
        target = _bounded_string(primary_symbol, "work_package_primary_symbol")
    except _CoverageEvidenceError as exc:
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite",
            "not_evaluated",
            str(exc),
        )
        return WorkPackageCoverageProjection(primary_symbol, "not_evaluated", (), (), gate)
    if analysis.status != "ready":
        reason = analysis.reason or "coverage_provider_not_ready"
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite", "not_evaluated", reason
        )
        return WorkPackageCoverageProjection(target, "not_evaluated", (), (), gate)
    if analysis.measurement_complete is not True:
        reason = "coverage_measurement_incomplete"
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite", "not_evaluated", reason
        )
        return WorkPackageCoverageProjection(target, "not_evaluated", (), (), gate)
    measured, resolution_reason = _resolve_work_package_coverage_scope(analysis, target)
    if measured is None:
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite",
            "not_evaluated",
            resolution_reason,
        )
        return WorkPackageCoverageProjection(target, "not_evaluated", (), (), gate)
    resolved_target = measured.subject_key
    relations = tuple(
        item for item in analysis.test_relations if item.production_symbol == resolved_target
    )
    tests = tuple(sorted({nodeid for item in relations for nodeid in item.test_nodeids}))
    relation_ids = tuple(sorted(item.relation_id for item in relations))
    tests_gate = next(item for item in analysis.gates if item.gate == "tests_passed")
    if tests_gate.status != "passed":
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite",
            "failed" if tests_gate.status == "failed" else "not_evaluated",
            "executing_suite_did_not_pass",
        )
        if gate.status == "failed":
            return WorkPackageCoverageProjection(
                resolved_target,
                "not_executed",
                tests,
                relation_ids,
                gate,
            )
        return WorkPackageCoverageProjection(
            resolved_target,
            "not_evaluated",
            tests,
            relation_ids,
            gate,
        )
    if not tests:
        gate = CoverageGateEvaluation(
            "work_package_target_executed_by_passing_suite",
            "failed",
            "no_executing_test_context_observed",
        )
        return WorkPackageCoverageProjection(resolved_target, "not_executed", (), (), gate)
    gate = CoverageGateEvaluation("work_package_target_executed_by_passing_suite", "passed", None)
    return WorkPackageCoverageProjection(resolved_target, "executed", tests, relation_ids, gate)


def _resolve_work_package_coverage_scope(
    analysis: CodeCoverageAnalysis,
    primary_symbol: str,
) -> tuple[CoverageScopeSummary | None, str]:
    exact = tuple(
        item for item in analysis.symbols if primary_symbol in {item.subject_key, item.symbol_key}
    )
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, "work_package_target_ambiguous"
    aliases = tuple(
        item
        for item in analysis.symbols
        if item.module_key is not None
        and item.qualified_name is not None
        and _coverage_scope_matches_review_symbol(item, primary_symbol)
    )
    if len(aliases) == 1:
        return aliases[0], ""
    if len(aliases) > 1:
        return None, "work_package_target_ambiguous"
    return None, "work_package_target_not_measured"


def _coverage_scope_matches_review_symbol(
    scope: CoverageScopeSummary,
    primary_symbol: str,
) -> bool:
    module_key = scope.module_key
    qualified_name = scope.qualified_name
    if module_key is None or qualified_name is None:
        return False
    module_prefix = f"{module_key}."
    canonical_name = (
        qualified_name
        if qualified_name.startswith(module_prefix)
        else f"{module_prefix}{qualified_name}"
    )
    if canonical_name == primary_symbol:
        return True
    return "." in primary_symbol and canonical_name.endswith(f".{primary_symbol}")


def project_work_package_coverage_scope(
    analysis: CodeCoverageAnalysis,
    primary_symbol: str,
) -> CoverageScopeSummary | None:
    """Resolve a stable coverage scope from an exact or review symbol identity."""

    try:
        target = _bounded_string(primary_symbol, "work_package_primary_symbol")
    except _CoverageEvidenceError:
        return None
    if analysis.status != "ready":
        return None
    scope, _ = _resolve_work_package_coverage_scope(analysis, target)
    return scope


__all__ = [
    "CODE_COVERAGE_PROVIDER_ID",
    "CODE_COVERAGE_SCHEMA",
    "CodeCoverageAnalysis",
    "CoverageComparison",
    "CoverageGateEvaluation",
    "CoverageScopeSummary",
    "CoverageTestOutcomes",
    "CoverageToolVersion",
    "CoverageTotals",
    "TestToSymbolRelation",
    "WorkPackageCoverageProjection",
    "analyze_code_coverage",
    "compare_code_coverage",
    "project_work_package_coverage",
    "project_work_package_coverage_scope",
    "read_code_coverage_analysis",
]
