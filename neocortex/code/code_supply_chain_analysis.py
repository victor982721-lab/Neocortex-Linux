"""Read-only supply-chain view over normalized external provider evidence.

The external provider store is intentionally generic.  This module gives the
four Hito 5 providers one bounded, deterministic public interpretation without
turning a tool report into mutation authority or a synthetic aggregate score.
"""

from __future__ import annotations
import datetime as dt
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, Protocol, cast

from .code_schema import CODE_SCHEMA_VERSION, readonly_code_database, validate_code_schema
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_evidence,
)
from neocortex.workflow.self_analysis.self_analysis_status import require_sqlite_sidecars_absent
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

CODE_SUPPLY_CHAIN_SCHEMA = "neocortex.code-supply-chain-analysis/v1"
CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS = (
    "semgrep-neocortex-invariants",
    "deptry-project-dependencies",
    "pip-audit-known-vulnerabilities",
    "installed-package-inventory",
)
CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT = 200
CODE_SUPPLY_CHAIN_FINDING_BOUND = 100_000
CODE_SUPPLY_CHAIN_METRIC_BOUND = 100_000
CODE_SUPPLY_CHAIN_RELATION_BOUND = 250_000
CODE_SUPPLY_CHAIN_MESSAGE_BYTES = 2_048
CODE_SUPPLY_CHAIN_PAYLOAD_BYTES = 512 * 1024

SupplyChainStatus = Literal["ready", "abstained"]
SupplyChainProviderState = Literal["ready", "abstained", "not_recorded"]
SupplyChainGateStatus = Literal["passed", "failed", "abstained", "not_evaluated"]
SupplyChainFreshness = Literal["current", "stale", "unknown", "not_applicable"]
SupplyChainEvidenceKind = Literal["finding", "metric", "relation"]
SupplyChainCategory = Literal[
    "project_invariant",
    "dependency_hygiene",
    "known_vulnerability",
    "package_integrity",
    "license_inventory",
]
SupplyChainGate = Literal[
    "semgrep_invariants",
    "dependency_declaration_integrity",
    "vulnerability_snapshot_current",
    "no_known_vulnerabilities",
    "installed_package_integrity",
    "license_inventory_available",
]

_PROVIDER_CATEGORIES: dict[str, frozenset[SupplyChainCategory]] = {
    "semgrep-neocortex-invariants": frozenset({"project_invariant"}),
    "deptry-project-dependencies": frozenset({"dependency_hygiene"}),
    "pip-audit-known-vulnerabilities": frozenset({"known_vulnerability"}),
    "installed-package-inventory": frozenset({"package_integrity", "license_inventory"}),
}


@dataclass(frozen=True, slots=True)
class SupplyChainProviderStatus:
    provider_id: str
    status: SupplyChainProviderState
    reason: str | None
    profile: str | None
    tool_name: str | None
    tool_version: str | None
    provider_schema: str | None
    comparability_signature: str | None
    execution: str | None
    tool_run_id: int | None
    source_tool_run_id: int | None
    findings: int
    metrics: int
    relations: int
    source: str | None
    observed_date: str | None
    freshness: SupplyChainFreshness
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False


@dataclass(frozen=True, slots=True)
class SupplyChainObservation:
    observation_id: str
    provider_id: str
    evidence_kind: SupplyChainEvidenceKind
    category: SupplyChainCategory
    code: str
    severity: str
    message: str
    path: str | None
    start_line: int | None
    end_line: int | None
    subject_kind: str | None
    subject_key: str | None
    target_kind: str | None
    target_key: str | None
    value: float | None
    unit: str | None
    gate_authority: str
    source: str | None
    observed_date: str | None
    freshness: SupplyChainFreshness
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False


@dataclass(frozen=True, slots=True)
class SupplyChainCounts:
    findings: int
    metrics: int
    relations: int
    project_invariant: int
    dependency_hygiene: int
    known_vulnerability: int
    package_integrity: int
    license_inventory: int
    duplicate_ids: int
    observations: int
    observations_truncated: bool


@dataclass(frozen=True, slots=True)
class SupplyChainGateEvaluation:
    gate: SupplyChainGate
    provider_id: str
    status: SupplyChainGateStatus
    reason: str
    evidence_count: int


@dataclass(frozen=True, slots=True)
class SupplyChainDigest:
    xxh3_128: str
    xxh3_64_guard: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class CodeSupplyChainAnalysis:
    database: str
    analysis_run_id: int | None
    status: SupplyChainStatus
    reason: str | None
    providers: tuple[SupplyChainProviderStatus, ...]
    observations: tuple[SupplyChainObservation, ...]
    counts: SupplyChainCounts
    gates: tuple[SupplyChainGateEvaluation, ...]
    limitations: tuple[str, ...]
    digest: SupplyChainDigest
    authority: Literal["advisory"] = "advisory"
    mutation_authority: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": "code-supply-chain-analysis",
            "schema": CODE_SUPPLY_CHAIN_SCHEMA,
            **asdict(self),
        }


class _FindingEvidence(Protocol):
    portable_finding_id: str
    relative_path: str
    category: str
    code: str
    severity: str
    message: str
    gate_authority: str
    start_line: int
    end_line: int
    metadata: Mapping[str, object]


class _MetricEvidence(Protocol):
    portable_metric_id: str
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


class _ProviderEvidence(Protocol):
    provider_id: str
    tool_run_id: int
    effective_tool_run_id: int | None
    status: str
    reason: str | None
    findings: tuple[_FindingEvidence, ...]
    metrics: tuple[_MetricEvidence, ...]
    relations: tuple[_RelationEvidence, ...]


class _ProviderSuiteStatus(Protocol):
    provider_id: str
    provider_schema: str
    profile: str
    tool_name: str
    tool_version: str | None
    status: str
    reason: str | None
    execution: str | None
    comparability_signature: str | None
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ProviderView:
    evidence: _ProviderEvidence | None
    status: SupplyChainProviderStatus


def _bounded_text(value: object, *, maximum: int = CODE_SUPPLY_CHAIN_MESSAGE_BYTES) -> str:
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    suffix = "..."
    available = maximum - len(suffix)
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


def _metadata_strings(
    evidence: _ProviderEvidence,
    names: frozenset[str],
) -> tuple[str, ...]:
    values: set[str] = set()
    records: Sequence[object] = (
        *evidence.findings,
        *evidence.metrics,
        *evidence.relations,
    )
    for record in records:
        metadata = getattr(record, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        for name in names:
            value = metadata.get(name)
            if isinstance(value, str) and value.strip():
                values.add(_bounded_text(value.strip(), maximum=512))
    return tuple(sorted(values, key=lambda item: (item.casefold(), item)))


def _metric_value(
    evidence: _ProviderEvidence,
    name: str,
    *,
    subject_kind: str | None = None,
    subject_key: str | None = None,
) -> float | None:
    values = sorted(
        float(metric.value)
        for metric in evidence.metrics
        if metric.metric_name == name
        and (subject_kind is None or metric.subject_kind == subject_kind)
        and (subject_key is None or metric.subject_key == subject_key)
    )
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _unix_date(value: float | None) -> str | None:
    if value is None or value < 0:
        return None
    try:
        observed = dt.datetime.fromtimestamp(value, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return observed.date().isoformat()


def _unix_datetime(value: float | None) -> dt.datetime | None:
    if value is None or value < 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_datetime(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.UTC)


def _fresh_until(
    evidence: _ProviderEvidence,
    *,
    metric_name: str,
) -> tuple[dt.datetime | None, str | None]:
    metric_raw = _metric_value(
        evidence,
        metric_name,
        subject_kind="project",
        subject_key="project:installed-environment",
    )
    metric_deadline = _unix_datetime(metric_raw)
    metadata_values = _metadata_strings(evidence, frozenset({"fresh_until_utc"}))
    if metric_raw is not None and metric_deadline is None:
        return None, "freshness_deadline_invalid"
    if len(metadata_values) > 1:
        return None, "freshness_deadline_conflicting"
    metadata_deadline = None if not metadata_values else _iso_datetime(metadata_values[0])
    if metadata_values and metadata_deadline is None:
        return None, "freshness_deadline_invalid"
    if metric_deadline is not None and metadata_deadline is not None:
        if abs((metric_deadline - metadata_deadline).total_seconds()) > 1:
            return None, "freshness_deadline_inconsistent"
    deadline = metadata_deadline or metric_deadline
    if deadline is None:
        return None, "freshness_deadline_not_recorded"
    return deadline, None


def _normalized_now(now_utc: dt.datetime | None) -> dt.datetime:
    if now_utc is None:
        return dt.datetime.now(tz=dt.UTC)
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return now_utc.astimezone(dt.UTC)


def _provider_provenance(
    provider_id: str,
    evidence: _ProviderEvidence,
    suite_status: _ProviderSuiteStatus,
    now_utc: dt.datetime,
) -> tuple[str | None, str | None, SupplyChainFreshness, tuple[str, ...]]:
    sources = _metadata_strings(
        evidence,
        frozenset(
            {
                "source",
                "advisory_source",
                "database_source",
                "inventory_source",
                "source_name",
                "snapshot_source",
            }
        ),
    )
    source = ";".join(sources) if sources else suite_status.tool_name
    observed_date: str | None = None
    freshness: SupplyChainFreshness = "not_applicable"
    limitations: list[str] = []
    if provider_id == "pip-audit-known-vulnerabilities":
        observed_date = _unix_date(
            _metric_value(
                evidence,
                "audit_observed_at_unix_seconds",
                subject_kind="project",
                subject_key="project:installed-environment",
            )
        )
        deadline, deadline_limitation = _fresh_until(
            evidence,
            metric_name="audit_fresh_until_unix_seconds",
        )
        if deadline is None:
            freshness = "unknown"
            limitations.append(deadline_limitation or "freshness_deadline_not_recorded")
        else:
            freshness = "current" if now_utc <= deadline else "stale"
    elif provider_id == "installed-package-inventory":
        observed_date = _unix_date(
            _metric_value(
                evidence,
                "inventory_observed_at_unix_seconds",
                subject_kind="project",
                subject_key="project:installed-environment",
            )
        )
        freshness = "unknown"
        limitations.append("installed_inventory_freshness_deadline_not_recorded")
    return source, observed_date, freshness, tuple(limitations)


def _provider_reason(
    provider_id: str,
    evidence: _ProviderEvidence | None,
    suite_status: _ProviderSuiteStatus | None,
) -> str | None:
    if evidence is None or suite_status is None:
        return "provider_not_recorded"
    if evidence.status != "ready":
        return evidence.reason or "provider_evidence_abstained"
    if suite_status.status != "ready":
        return suite_status.reason or f"provider_{suite_status.status}"
    if evidence.effective_tool_run_id is None:
        return "effective_tool_run_missing"
    if len(evidence.findings) > CODE_SUPPLY_CHAIN_FINDING_BOUND:
        return "provider_finding_bound_exceeded"
    if len(evidence.metrics) > CODE_SUPPLY_CHAIN_METRIC_BOUND:
        return "provider_metric_bound_exceeded"
    if len(evidence.relations) > CODE_SUPPLY_CHAIN_RELATION_BOUND:
        return "provider_relation_bound_exceeded"
    allowed = _PROVIDER_CATEGORIES[provider_id]
    if any(finding.category not in allowed for finding in evidence.findings):
        return "provider_finding_category_incompatible"
    return None


def _provider_views(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    now_utc: dt.datetime,
) -> tuple[_ProviderView, ...]:
    evidence = cast(
        dict[str, _ProviderEvidence],
        read_external_provider_evidence(
            connection,
            analysis_run_id,
            provider_ids=CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS,
        ),
    )
    suite = read_external_evidence_suite(
        connection,
        analysis_run_id,
        enforce_current_runtime=False,
        provider_ids=CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS,
    )
    suite_statuses = cast(
        dict[str, _ProviderSuiteStatus],
        {item.provider_id: item for item in suite.providers},
    )
    views: list[_ProviderView] = []
    for provider_id in CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS:
        provider = evidence.get(provider_id)
        suite_status = suite_statuses.get(provider_id)
        reason = _provider_reason(provider_id, provider, suite_status)
        source: str | None = None
        observed_date: str | None = None
        freshness: SupplyChainFreshness = "unknown"
        provenance_limitations: tuple[str, ...] = ()
        if provider is not None and suite_status is not None and reason is None:
            source, observed_date, freshness, provenance_limitations = _provider_provenance(
                provider_id, provider, suite_status, now_utc
            )
        state: SupplyChainProviderState
        if provider is None or suite_status is None:
            state = "not_recorded"
        elif reason is None:
            state = "ready"
        else:
            state = "abstained"
        limitations = tuple(
            dict.fromkeys(
                (
                    *(() if suite_status is None else suite_status.limitations),
                    *provenance_limitations,
                )
            )
        )
        views.append(
            _ProviderView(
                provider if reason is None else None,
                SupplyChainProviderStatus(
                    provider_id,
                    state,
                    reason,
                    None if suite_status is None else suite_status.profile,
                    None if suite_status is None else suite_status.tool_name,
                    None if suite_status is None else suite_status.tool_version,
                    None if suite_status is None else suite_status.provider_schema,
                    None if suite_status is None else suite_status.comparability_signature,
                    None if suite_status is None else suite_status.execution,
                    None if provider is None else provider.tool_run_id,
                    None if provider is None else provider.effective_tool_run_id,
                    0 if provider is None else len(provider.findings),
                    0 if provider is None else len(provider.metrics),
                    0 if provider is None else len(provider.relations),
                    source,
                    observed_date,
                    freshness,
                    limitations,
                ),
            )
        )
    return tuple(views)


def _metric_category(provider_id: str, metric: _MetricEvidence) -> SupplyChainCategory:
    if provider_id == "installed-package-inventory":
        if metric.category == "license_inventory" or "license" in metric.metric_name:
            return "license_inventory"
        return "package_integrity"
    return next(iter(_PROVIDER_CATEGORIES[provider_id]))


def _relation_category(
    provider_id: str,
    relation: _RelationEvidence,
) -> SupplyChainCategory:
    if provider_id == "installed-package-inventory":
        metadata_category = relation.metadata.get("category")
        if metadata_category == "license_inventory" or "license" in relation.relation_kind:
            return "license_inventory"
        return "package_integrity"
    return next(iter(_PROVIDER_CATEGORIES[provider_id]))


def _finding_observation(
    provider: SupplyChainProviderStatus,
    finding: _FindingEvidence,
) -> SupplyChainObservation:
    return SupplyChainObservation(
        f"{provider.provider_id}:finding:{finding.portable_finding_id}",
        provider.provider_id,
        "finding",
        cast(SupplyChainCategory, finding.category),
        _bounded_text(finding.code, maximum=512),
        _bounded_text(finding.severity, maximum=64),
        _bounded_text(finding.message),
        _bounded_text(finding.relative_path, maximum=2_048),
        finding.start_line,
        finding.end_line,
        None,
        None,
        None,
        None,
        None,
        None,
        _bounded_text(finding.gate_authority, maximum=256),
        provider.source,
        provider.observed_date,
        provider.freshness,
    )


def _metric_observation(
    provider: SupplyChainProviderStatus,
    metric: _MetricEvidence,
) -> SupplyChainObservation:
    category = _metric_category(provider.provider_id, metric)
    return SupplyChainObservation(
        f"{provider.provider_id}:metric:{metric.portable_metric_id}",
        provider.provider_id,
        "metric",
        category,
        _bounded_text(metric.metric_name, maximum=512),
        "info",
        _bounded_text(f"{metric.metric_name}={metric.value:g} {metric.unit}"),
        None,
        None,
        None,
        _bounded_text(metric.subject_kind, maximum=64),
        _bounded_text(metric.subject_key, maximum=2_048),
        None,
        None,
        float(metric.value),
        _bounded_text(metric.unit, maximum=64),
        "advisory",
        provider.source,
        provider.observed_date,
        provider.freshness,
    )


def _relation_observation(
    provider: SupplyChainProviderStatus,
    relation: _RelationEvidence,
) -> SupplyChainObservation:
    category = _relation_category(provider.provider_id, relation)
    return SupplyChainObservation(
        f"{provider.provider_id}:relation:{relation.portable_relation_id}",
        provider.provider_id,
        "relation",
        category,
        _bounded_text(relation.relation_kind, maximum=512),
        "info",
        _bounded_text(
            f"{relation.source_kind}:{relation.source_key} -> "
            f"{relation.target_kind}:{relation.target_key}"
        ),
        None,
        None,
        None,
        _bounded_text(relation.source_kind, maximum=64),
        _bounded_text(relation.source_key, maximum=2_048),
        _bounded_text(relation.target_kind, maximum=64),
        _bounded_text(relation.target_key, maximum=2_048),
        None,
        None,
        "advisory",
        provider.source,
        provider.observed_date,
        provider.freshness,
    )


def _observations(
    views: tuple[_ProviderView, ...],
) -> tuple[tuple[SupplyChainObservation, ...], int, tuple[SupplyChainObservation, ...]]:
    unique: dict[str, SupplyChainObservation] = {}
    duplicates = 0
    for view in views:
        if view.evidence is None:
            continue
        provider = view.status
        candidates = (
            *(_finding_observation(provider, item) for item in view.evidence.findings),
            *(_metric_observation(provider, item) for item in view.evidence.metrics),
            *(_relation_observation(provider, item) for item in view.evidence.relations),
        )
        for observation in candidates:
            if observation.observation_id in unique:
                duplicates += 1
                continue
            unique[observation.observation_id] = observation

    def priority(item: SupplyChainObservation) -> int:
        if item.evidence_kind == "finding":
            return 0
        if item.category == "known_vulnerability" and (
            (item.evidence_kind == "relation" and item.code == "package_has_known_vulnerability")
            or (
                item.evidence_kind == "metric"
                and item.code.startswith("known_vulnerability:")
                and item.value is not None
                and item.value > 0
            )
        ):
            return 1
        return 2 if item.evidence_kind == "metric" else 3

    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                priority(item),
                {"error": 0, "warning": 1, "info": 2}.get(item.severity, 3),
                item.provider_id,
                item.category,
                item.observation_id,
            ),
        )
    )
    return ordered, duplicates, ordered[:CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT]


def _counts(
    observations: tuple[SupplyChainObservation, ...],
    duplicates: int,
    visible: tuple[SupplyChainObservation, ...],
) -> SupplyChainCounts:
    def category(name: SupplyChainCategory) -> int:
        return sum(item.category == name for item in observations)

    return SupplyChainCounts(
        sum(item.evidence_kind == "finding" for item in observations),
        sum(item.evidence_kind == "metric" for item in observations),
        sum(item.evidence_kind == "relation" for item in observations),
        category("project_invariant"),
        category("dependency_hygiene"),
        category("known_vulnerability"),
        category("package_integrity"),
        category("license_inventory"),
        duplicates,
        len(visible),
        len(observations) > len(visible),
    )


def _provider_unready_gate(
    gate: SupplyChainGate,
    provider: SupplyChainProviderStatus,
) -> SupplyChainGateEvaluation | None:
    if provider.status == "ready":
        return None
    return SupplyChainGateEvaluation(
        gate,
        provider.provider_id,
        "not_evaluated" if provider.status == "not_recorded" else "abstained",
        provider.reason or f"provider_{provider.status}",
        0,
    )


def _finding_count(
    observations: Sequence[SupplyChainObservation],
    provider_id: str,
    category: SupplyChainCategory,
    *,
    gate_authority: str | None = None,
) -> int:
    return sum(
        item.provider_id == provider_id
        and item.evidence_kind == "finding"
        and item.category == category
        and (gate_authority is None or item.gate_authority == gate_authority)
        for item in observations
    )


def _metric_by_name(
    observations: Sequence[SupplyChainObservation],
    provider_id: str,
    name: str,
    *,
    subject_kind: str | None = None,
    subject_key: str | None = None,
) -> tuple[float, ...]:
    return tuple(
        item.value
        for item in observations
        if item.provider_id == provider_id
        and item.evidence_kind == "metric"
        and item.code == name
        and item.value is not None
        and (subject_kind is None or item.subject_kind == subject_kind)
        and (subject_key is None or item.subject_key == subject_key)
    )


def _exact_count_metric(
    observations: Sequence[SupplyChainObservation],
    provider_id: str,
    name: str,
    *,
    subject_key: str,
) -> tuple[int | None, str | None]:
    values = _metric_by_name(
        observations,
        provider_id,
        name,
        subject_kind="project",
        subject_key=subject_key,
    )
    if not values:
        return None, f"{name}_not_recorded"
    if len(values) != 1:
        return None, f"{name}_not_unique"
    value = values[0]
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        return None, f"{name}_invalid"
    return int(value), None


def _zero_gate(
    gate: SupplyChainGate,
    provider: SupplyChainProviderStatus,
    count: int,
    *,
    failure_reason: str,
) -> SupplyChainGateEvaluation:
    unready = _provider_unready_gate(gate, provider)
    if unready is not None:
        return unready
    return SupplyChainGateEvaluation(
        gate,
        provider.provider_id,
        "passed" if count == 0 else "failed",
        "no_findings_observed" if count == 0 else failure_reason,
        count,
    )


def _gates(
    providers: tuple[SupplyChainProviderStatus, ...],
    observations: tuple[SupplyChainObservation, ...],
) -> tuple[SupplyChainGateEvaluation, ...]:
    by_id = {item.provider_id: item for item in providers}
    semgrep = by_id["semgrep-neocortex-invariants"]
    deptry = by_id["deptry-project-dependencies"]
    audit = by_id["pip-audit-known-vulnerabilities"]
    inventory = by_id["installed-package-inventory"]
    invariant_count = _finding_count(observations, semgrep.provider_id, "project_invariant")
    gates: list[SupplyChainGateEvaluation] = [
        _zero_gate(
            "semgrep_invariants",
            semgrep,
            invariant_count,
            failure_reason="project_invariant_findings_observed",
        ),
    ]
    deptry_unready = _provider_unready_gate("dependency_declaration_integrity", deptry)
    if deptry_unready is not None:
        gates.append(deptry_unready)
    else:
        dependency_gate_count, dependency_error = _exact_count_metric(
            observations,
            deptry.provider_id,
            "dependency_gate_issue_count",
            subject_key="project:neocortex-framework",
        )
        if dependency_error is not None or dependency_gate_count is None:
            gates.append(
                SupplyChainGateEvaluation(
                    "dependency_declaration_integrity",
                    deptry.provider_id,
                    "abstained",
                    dependency_error or "dependency_gate_issue_count_invalid",
                    0,
                )
            )
        else:
            gates.append(
                SupplyChainGateEvaluation(
                    "dependency_declaration_integrity",
                    deptry.provider_id,
                    "failed" if dependency_gate_count else "passed",
                    "dependency_declaration_issues_observed"
                    if dependency_gate_count
                    else "no_dependency_declaration_issues_observed",
                    dependency_gate_count,
                )
            )
    unready = _provider_unready_gate("vulnerability_snapshot_current", audit)
    if unready is not None:
        gates.append(unready)
    else:
        snapshot_status: SupplyChainGateStatus
        if audit.freshness == "current":
            snapshot_status = "passed"
        elif audit.freshness == "stale":
            snapshot_status = "failed"
        else:
            snapshot_status = "abstained"
        gates.append(
            SupplyChainGateEvaluation(
                "vulnerability_snapshot_current",
                audit.provider_id,
                snapshot_status,
                "snapshot_current"
                if audit.freshness == "current"
                else f"snapshot_{audit.freshness}",
                0,
            )
        )
    unready = _provider_unready_gate("no_known_vulnerabilities", audit)
    if unready is not None:
        gates.append(unready)
    else:
        vulnerability_count, vulnerability_error = _exact_count_metric(
            observations,
            audit.provider_id,
            "known_vulnerability_count",
            subject_key="project:installed-environment",
        )
        if vulnerability_error is not None or vulnerability_count is None:
            gates.append(
                SupplyChainGateEvaluation(
                    "no_known_vulnerabilities",
                    audit.provider_id,
                    "abstained",
                    vulnerability_error or "known_vulnerability_count_invalid",
                    0,
                )
            )
        elif vulnerability_count:
            gates.append(
                SupplyChainGateEvaluation(
                    "no_known_vulnerabilities",
                    audit.provider_id,
                    "failed",
                    "known_vulnerabilities_observed",
                    vulnerability_count,
                )
            )
        elif audit.freshness == "current":
            gates.append(
                SupplyChainGateEvaluation(
                    "no_known_vulnerabilities",
                    audit.provider_id,
                    "passed",
                    "no_known_vulnerabilities_in_current_snapshot",
                    0,
                )
            )
        else:
            gates.append(
                SupplyChainGateEvaluation(
                    "no_known_vulnerabilities",
                    audit.provider_id,
                    "abstained",
                    "vulnerability_snapshot_not_current",
                    0,
                )
            )

    integrity_unready = _provider_unready_gate("installed_package_integrity", inventory)
    if integrity_unready is not None:
        gates.append(integrity_unready)
    else:
        integrity_specs = (
            ("wheel_record_integrity_current", "package:neocortex-framework", 1),
            ("record_missing_file_count", "package:neocortex-framework", 0),
            ("record_hash_mismatch_count", "package:neocortex-framework", 0),
            ("record_size_mismatch_count", "package:neocortex-framework", 0),
            ("record_unverifiable_entry_count", "package:neocortex-framework", 0),
            ("record_unsafe_entry_count", "package:neocortex-framework", 0),
            ("record_malformed_entry_count", "package:neocortex-framework", 0),
            (
                "pyproject_required_missing_dependency_count",
                "project:installed-environment",
                0,
            ),
            (
                "pyproject_required_version_mismatch_count",
                "project:installed-environment",
                0,
            ),
        )
        integrity_values: list[tuple[str, int, int]] = []
        integrity_errors: list[str] = []
        for metric_name, subject_key, expected in integrity_specs:
            value, error = _exact_count_metric(
                observations,
                inventory.provider_id,
                metric_name,
                subject_key=subject_key,
            )
            if error is not None or value is None:
                integrity_errors.append(error or f"{metric_name}_invalid")
            else:
                integrity_values.append((metric_name, value, expected))
        if integrity_errors:
            gates.append(
                SupplyChainGateEvaluation(
                    "installed_package_integrity",
                    inventory.provider_id,
                    "abstained",
                    "package_integrity_metrics_incomplete:" + ",".join(integrity_errors),
                    len(integrity_values),
                )
            )
        else:
            failures = tuple(
                (name, value) for name, value, expected in integrity_values if value != expected
            )
            gates.append(
                SupplyChainGateEvaluation(
                    "installed_package_integrity",
                    inventory.provider_id,
                    "failed" if failures else "passed",
                    "package_integrity_failures_observed:"
                    + ",".join(f"{name}={value}" for name, value in failures)
                    if failures
                    else "installed_package_integrity_confirmed",
                    len(integrity_values),
                )
            )

    license_unready = _provider_unready_gate("license_inventory_available", inventory)
    if license_unready is not None:
        gates.append(license_unready)
    else:
        license_values: dict[str, int] = {}
        license_errors: list[str] = []
        for metric_name in (
            "packages_with_license_metadata",
            "packages_with_ambiguous_license_metadata",
            "packages_without_license_metadata",
        ):
            value, error = _exact_count_metric(
                observations,
                inventory.provider_id,
                metric_name,
                subject_key="project:installed-environment",
            )
            if error is not None or value is None:
                license_errors.append(error or f"{metric_name}_invalid")
            else:
                license_values[metric_name] = value
        if license_errors:
            gates.append(
                SupplyChainGateEvaluation(
                    "license_inventory_available",
                    inventory.provider_id,
                    "abstained",
                    "license_inventory_metrics_incomplete:" + ",".join(license_errors),
                    len(license_values),
                )
            )
        else:
            gaps = (
                license_values["packages_with_ambiguous_license_metadata"]
                + license_values["packages_without_license_metadata"]
            )
            gates.append(
                SupplyChainGateEvaluation(
                    "license_inventory_available",
                    inventory.provider_id,
                    "passed",
                    "license_inventory_recorded_with_metadata_gaps"
                    if gaps
                    else "license_inventory_recorded",
                    sum(license_values.values()),
                )
            )
    return tuple(gates)


def _portable_provider_payload(
    provider: SupplyChainProviderStatus,
) -> dict[str, object]:
    payload = asdict(provider)
    payload.pop("tool_run_id")
    payload.pop("source_tool_run_id")
    payload.pop("execution")
    return payload


def _digest(
    status: SupplyChainStatus,
    reason: str | None,
    providers: tuple[SupplyChainProviderStatus, ...],
    observations: tuple[SupplyChainObservation, ...],
    counts: SupplyChainCounts,
    gates: tuple[SupplyChainGateEvaluation, ...],
    limitations: tuple[str, ...],
) -> SupplyChainDigest:
    payload = canonical_json(
        {
            "schema": CODE_SUPPLY_CHAIN_SCHEMA,
            "status": status,
            "reason": reason,
            "providers": [_portable_provider_payload(item) for item in providers],
            "observations": [asdict(item) for item in observations],
            "counts": asdict(counts),
            "gates": [asdict(item) for item in gates],
            "limitations": list(limitations),
            "authority": "advisory",
            "mutation_authority": False,
        }
    )
    fingerprint = fingerprint_text(payload)
    return SupplyChainDigest(
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
        fingerprint.byte_count,
    )


def _empty_counts() -> SupplyChainCounts:
    return SupplyChainCounts(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, False)


def _not_evaluated_gates(reason: str) -> tuple[SupplyChainGateEvaluation, ...]:
    definitions: tuple[tuple[SupplyChainGate, str], ...] = (
        ("semgrep_invariants", "semgrep-neocortex-invariants"),
        ("dependency_declaration_integrity", "deptry-project-dependencies"),
        ("vulnerability_snapshot_current", "pip-audit-known-vulnerabilities"),
        ("no_known_vulnerabilities", "pip-audit-known-vulnerabilities"),
        ("installed_package_integrity", "installed-package-inventory"),
        ("license_inventory_available", "installed-package-inventory"),
    )
    return tuple(
        SupplyChainGateEvaluation(gate, provider, "not_evaluated", reason, 0)
        for gate, provider in definitions
    )


def _abstained(
    database: str,
    analysis_run_id: int | None,
    reason: str,
) -> CodeSupplyChainAnalysis:
    counts = _empty_counts()
    gates = _not_evaluated_gates(reason)
    limitations = ("supply_chain_provider_evidence_was_not_interpreted",)
    digest = _digest("abstained", reason, (), (), counts, gates, limitations)
    return CodeSupplyChainAnalysis(
        database,
        analysis_run_id,
        "abstained",
        reason,
        (),
        (),
        counts,
        gates,
        limitations,
        digest,
    )


def read_code_supply_chain_analysis(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    database: str = "",
    now_utc: dt.datetime | None = None,
) -> CodeSupplyChainAnalysis:
    """Interpret Hito 5 provider evidence from an already validated reader."""

    try:
        observed_now = _normalized_now(now_utc)
        views = _provider_views(connection, analysis_run_id, observed_now)
        all_observations, duplicates, visible = _observations(views)
        counts = _counts(all_observations, duplicates, visible)
        providers = tuple(view.status for view in views)
        gates = _gates(providers, all_observations)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return _abstained(
            database,
            analysis_run_id,
            f"supply_chain_evidence_incompatible:{type(exc).__name__}:{exc}",
        )
    unavailable = tuple(item for item in providers if item.status != "ready")
    reason = None
    if unavailable:
        reason = "required_provider_not_ready:" + ",".join(
            f"{item.provider_id}:{item.reason or item.status}" for item in unavailable
        )
    limitations = [
        "supply_chain_evidence_is_advisory_and_has_no_mutation_authority",
        "provider_absence_never_passes_a_gate",
        "known_vulnerability_absence_is_bounded_to_the_recorded_snapshot",
        "vulnerability_snapshot_freshness_is_recomputed_from_its_utc_deadline",
        "installed_inventory_has_no_query_time_freshness_deadline",
        "license_inventory_does_not_assess_legal_compatibility",
    ]
    if any(
        gate.gate == "license_inventory_available"
        and gate.reason == "license_inventory_recorded_with_metadata_gaps"
        for gate in gates
    ):
        limitations.append("license_metadata_incomplete_or_ambiguous")
    if counts.observations_truncated:
        limitations.append("supply_chain_observations_truncated")
    if duplicates:
        limitations.append("duplicate_evidence_ids_deduplicated")
    digest = _digest(
        "abstained" if reason is not None else "ready",
        reason,
        providers,
        visible,
        counts,
        gates,
        tuple(limitations),
    )
    result = CodeSupplyChainAnalysis(
        database,
        analysis_run_id,
        "abstained" if reason is not None else "ready",
        reason,
        providers,
        visible,
        counts,
        gates,
        tuple(limitations),
        digest,
    )
    payload_bytes = len(
        json.dumps(
            result.as_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if payload_bytes > CODE_SUPPLY_CHAIN_PAYLOAD_BYTES:
        return _abstained(database, analysis_run_id, "supply_chain_payload_bound_exceeded")
    return result


def analyze_code_supply_chain(
    state_directory: Path,
    *,
    now_utc: dt.datetime | None = None,
) -> CodeSupplyChainAnalysis:
    """Return the latest supply-chain view without creating or mutating state."""

    state_directory = Path(state_directory)
    database = state_directory / "code.sqlite3"
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        return _abstained(str(database), None, "code_state_missing")
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != CODE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"code state schema {schema_version} is unsupported for supply chain"
                )
            latest = connection.execute(
                "SELECT analysis_run_id,status FROM analysis_runs "
                "ORDER BY analysis_run_id DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                reason = "code_run_missing"
            elif str(latest["status"]) != "completed":
                reason = f"code_run_not_completed:{latest['status']}"
            else:
                return read_code_supply_chain_analysis(
                    connection,
                    int(latest["analysis_run_id"]),
                    database=str(database),
                    now_utc=now_utc,
                )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        reason = f"code_state_unavailable:{type(exc).__name__}:{exc}"
    return _abstained(str(database), None, reason)


def _wire_items(label: str, value: object) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    return tuple(value)


def _wire_mapping(
    label: str,
    value: object,
    expected_fields: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError(f"{label} fields are invalid")
    return value


def _wire_texts(label: str, value: object) -> tuple[str, ...]:
    items = _wire_items(label, value)
    if any(not isinstance(item, str) or not item or item.strip() != item for item in items):
        raise ValueError(f"{label} contains invalid text")
    return cast(tuple[str, ...], items)


def _wire_nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _parse_wire_providers(value: object) -> tuple[SupplyChainProviderStatus, ...]:
    provider_fields = frozenset(field.name for field in fields(SupplyChainProviderStatus))
    providers: list[SupplyChainProviderStatus] = []
    for raw in _wire_items("supply-chain providers", value):
        item = _wire_mapping("supply-chain provider", raw, provider_fields)
        values = dict(item)
        values["limitations"] = _wire_texts(
            "supply-chain provider limitations", values.get("limitations")
        )
        provider = SupplyChainProviderStatus(**values)  # type: ignore[arg-type]
        if (
            provider.status not in {"ready", "abstained", "not_recorded"}
            or provider.freshness not in {"current", "stale", "unknown", "not_applicable"}
            or provider.authority != "advisory"
            or provider.mutation_authority
        ):
            raise ValueError("supply-chain provider authority or status is invalid")
        for count in (provider.findings, provider.metrics, provider.relations):
            _wire_nonnegative("supply-chain provider evidence count", count)
        if provider.status == "ready" and provider.reason is not None:
            raise ValueError("ready supply-chain provider cannot carry an abstention reason")
        if provider.status != "ready" and not provider.reason:
            raise ValueError("unready supply-chain provider requires a reason")
        providers.append(provider)
    provider_tuple = tuple(providers)
    provider_ids = tuple(item.provider_id for item in provider_tuple)
    if provider_ids and provider_ids != CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS:
        raise ValueError("supply-chain provider registry is not canonical")
    return provider_tuple


def _parse_wire_observations(value: object) -> tuple[SupplyChainObservation, ...]:
    observation_fields = frozenset(field.name for field in fields(SupplyChainObservation))
    observations: list[SupplyChainObservation] = []
    for raw in _wire_items("supply-chain observations", value):
        item = _wire_mapping("supply-chain observation", raw, observation_fields)
        observation = SupplyChainObservation(**dict(item))  # type: ignore[arg-type]
        if (
            observation.provider_id not in CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS
            or observation.evidence_kind not in {"finding", "metric", "relation"}
            or observation.category not in set().union(*_PROVIDER_CATEGORIES.values())
            or observation.authority != "advisory"
            or observation.mutation_authority
        ):
            raise ValueError("supply-chain observation semantics are invalid")
        observations.append(observation)
    observation_tuple = tuple(observations)
    if len({item.observation_id for item in observation_tuple}) != len(observation_tuple):
        raise ValueError("supply-chain observation identities repeat")
    return observation_tuple


def _parse_wire_counts(
    value: object,
    observations: tuple[SupplyChainObservation, ...],
) -> SupplyChainCounts:
    count_fields = frozenset(field.name for field in fields(SupplyChainCounts))
    raw_counts = _wire_mapping("supply-chain counts", value, count_fields)
    counts = SupplyChainCounts(**dict(raw_counts))  # type: ignore[arg-type]
    for field in fields(SupplyChainCounts):
        count = getattr(counts, field.name)
        if field.name == "observations_truncated":
            if not isinstance(count, bool):
                raise ValueError("supply-chain truncation flag is invalid")
        else:
            _wire_nonnegative(f"supply-chain {field.name}", count)
    if counts.observations != len(observations):
        raise ValueError("supply-chain bounded observation count is inconsistent")
    if counts.observations_truncated != (
        counts.findings + counts.metrics + counts.relations > counts.observations
    ):
        raise ValueError("supply-chain truncation is not derived from exact counts")
    return counts


def _parse_wire_gates(value: object) -> tuple[SupplyChainGateEvaluation, ...]:
    gate_fields = frozenset(field.name for field in fields(SupplyChainGateEvaluation))
    gates: list[SupplyChainGateEvaluation] = []
    for raw in _wire_items("supply-chain gates", value):
        item = _wire_mapping("supply-chain gate", raw, gate_fields)
        gate = SupplyChainGateEvaluation(**dict(item))  # type: ignore[arg-type]
        if gate.status not in {"passed", "failed", "abstained", "not_evaluated"}:
            raise ValueError("supply-chain gate status is invalid")
        _wire_nonnegative("supply-chain gate evidence count", gate.evidence_count)
        gates.append(gate)
    gate_tuple = tuple(gates)
    expected_gate_owners = tuple(
        (item.gate, item.provider_id) for item in _not_evaluated_gates("wire_validation")
    )
    if tuple((item.gate, item.provider_id) for item in gate_tuple) != expected_gate_owners:
        raise ValueError("supply-chain gate registry is not canonical")
    return gate_tuple


def _parse_wire_digest(value: object) -> SupplyChainDigest:
    digest_fields = frozenset(field.name for field in fields(SupplyChainDigest))
    raw_digest = _wire_mapping("supply-chain digest", value, digest_fields)
    digest = SupplyChainDigest(**dict(raw_digest))  # type: ignore[arg-type]
    if (
        not isinstance(digest.xxh3_128, str)
        or not digest.xxh3_128
        or not isinstance(digest.xxh3_64_guard, str)
        or not digest.xxh3_64_guard
    ):
        raise ValueError("supply-chain digest identity is invalid")
    _wire_nonnegative("supply-chain digest byte count", digest.byte_count)
    return digest


def _parse_wire_analysis_identity(
    payload: Mapping[str, object],
) -> tuple[SupplyChainStatus, str | None, int | None, str]:
    status = payload.get("status")
    reason = payload.get("reason")
    analysis_run_id = payload.get("analysis_run_id")
    if status not in {"ready", "abstained"}:
        raise ValueError("supply-chain analysis status is invalid")
    if status == "ready" and reason is not None:
        raise ValueError("ready supply-chain analysis cannot carry a reason")
    if status == "abstained" and (not isinstance(reason, str) or not reason):
        raise ValueError("abstained supply-chain analysis requires a reason")
    if analysis_run_id is not None and (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("supply-chain analysis run identity is invalid")
    if payload.get("authority") != "advisory" or payload.get("mutation_authority") is not False:
        raise ValueError("supply-chain analysis authority is invalid")
    database = payload.get("database")
    if not isinstance(database, str):
        raise ValueError("supply-chain database identity is invalid")
    return (
        cast(SupplyChainStatus, status),
        cast(str | None, reason),
        analysis_run_id,
        database,
    )


def parse_code_supply_chain_payload(payload: Mapping[str, object]) -> CodeSupplyChainAnalysis:
    """Strictly reconstruct and re-digest the public supply-chain projection."""

    expected = {field.name for field in fields(CodeSupplyChainAnalysis)} | {"kind", "schema"}
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected
        or payload.get("kind") != "code-supply-chain-analysis"
        or payload.get("schema") != CODE_SUPPLY_CHAIN_SCHEMA
    ):
        raise ValueError("supply-chain payload envelope is invalid")

    provider_tuple = _parse_wire_providers(payload.get("providers"))
    observation_tuple = _parse_wire_observations(payload.get("observations"))
    counts = _parse_wire_counts(payload.get("counts"), observation_tuple)
    gate_tuple = _parse_wire_gates(payload.get("gates"))
    limitations = _wire_texts("supply-chain limitations", payload.get("limitations"))
    digest = _parse_wire_digest(payload.get("digest"))
    status, reason, analysis_run_id, database = _parse_wire_analysis_identity(payload)

    expected_digest = _digest(
        status,
        reason,
        provider_tuple,
        observation_tuple,
        counts,
        gate_tuple,
        limitations,
    )
    if digest != expected_digest:
        raise ValueError("supply-chain payload digest is invalid")
    return CodeSupplyChainAnalysis(
        database=database,
        analysis_run_id=analysis_run_id,
        status=status,
        reason=reason,
        providers=provider_tuple,
        observations=observation_tuple,
        counts=counts,
        gates=gate_tuple,
        limitations=limitations,
        digest=digest,
    )


__all__ = [
    "CODE_SUPPLY_CHAIN_OBSERVATION_LIMIT",
    "CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS",
    "CODE_SUPPLY_CHAIN_SCHEMA",
    "CodeSupplyChainAnalysis",
    "SupplyChainCounts",
    "SupplyChainDigest",
    "SupplyChainGateEvaluation",
    "SupplyChainObservation",
    "SupplyChainProviderStatus",
    "analyze_code_supply_chain",
    "parse_code_supply_chain_payload",
    "read_code_supply_chain_analysis",
]
