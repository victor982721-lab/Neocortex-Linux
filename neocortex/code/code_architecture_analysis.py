"""Read-only architecture and cognitive-complexity view over Code schema v4.

The provider tables are generic persistence.  This module is their first public
consumer: it turns the three Hito 2 providers into one bounded, deterministic,
and explicitly abstaining architecture view.  Hito 6 derives bounded graph
reach, path-namespace crossings, and named centrality from that same published
graph.  A Python package prefix is never presented as repository or logical
ownership.
"""

from __future__ import annotations
import math
import os
import sqlite3
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, cast

from .code_schema import CODE_SCHEMA_VERSION, readonly_code_database, validate_code_schema
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_evidence,
)
from neocortex.workflow.self_analysis.self_analysis_status import require_sqlite_sidecars_absent
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

CODE_ARCHITECTURE_SCHEMA = "neocortex.code-architecture-analysis/v3"
CODE_ARCHITECTURE_REQUIRED_PROVIDERS = (
    "complexipy-cognitive",
    "grimp-architecture",
    "ruff-analyze-imports",
)
CODE_ARCHITECTURE_METRIC_LIMIT = 100_000
CODE_ARCHITECTURE_RELATION_LIMIT = 250_000
CODE_ARCHITECTURE_SYMBOL_LIMIT = 50_000
CODE_ARCHITECTURE_IMPORT_CHAIN_LIMIT = 20
CODE_ARCHITECTURE_IMPORT_CHAIN_DEPTH = 3
CODE_ARCHITECTURE_REACHABILITY_LIMIT = 2_000
CODE_ARCHITECTURE_CENTRALITY_FORMULA = (
    "(distinct_direct_importers_excluding_self + "
    "distinct_direct_dependencies_excluding_self) / (2 * (module_count - 1))"
)

ArchitectureStatus = Literal["ready", "abstained"]
ArchitectureComparison = Literal["both", "ruff_only", "grimp_only"]
ArchitectureGate = Literal["observed", "abstained"]
ArchitectureContractStatus = Literal["passed", "failed", "abstained"]
ArchitectureGateStatus = Literal["baseline", "passed", "failed", "not_evaluated"]


@dataclass(frozen=True, slots=True)
class ArchitectureProviderStatus:
    provider_id: str
    status: ArchitectureStatus
    reason: str | None
    tool_name: str | None
    tool_version: str | None
    provider_schema: str | None
    comparability_signature: str | None
    provider_gate: str | None
    execution: str | None
    tool_run_id: int | None
    source_tool_run_id: int | None
    metrics: int
    relations: int


@dataclass(frozen=True, slots=True)
class ArchitectureGateEvaluation:
    gate: Literal[
        "import_graph_consensus",
        "architecture_contracts",
        "module_complexity_displacement",
    ]
    status: ArchitectureGateStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class ArchitectureImportEdge:
    source_module: str
    target_module: str
    comparison: ArchitectureComparison
    ruff_observed: bool
    grimp_observed: bool
    confirmed: bool
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ArchitectureCycle:
    cycle_id: str
    modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchitectureContract:
    contract_id: str
    status: ArchitectureContractStatus
    evaluated: bool
    violations: int
    importer_modules: tuple[str, ...]
    imported_modules: tuple[str, ...]
    import_chains: tuple[tuple[str, ...], ...]
    contract_schema: str | None


@dataclass(frozen=True, slots=True)
class ArchitectureSymbolComplexity:
    symbol_id: str
    module_id: str
    cognitive_complexity: float


@dataclass(frozen=True, slots=True)
class ArchitectureModule:
    module_id: str
    fan_in: int
    fan_out: int
    cognitive_complexity_total: float | None
    cognitive_complexity_max: float | None
    cognitive_symbol_count: int
    cycle_ids: tuple[str, ...]
    contract_ids: tuple[str, ...]
    grimp_fan_in: int | None
    grimp_fan_out: int | None
    grimp_scc_size: int | None
    grimp_cycle_membership: bool | None
    path_namespace_id: str | None = None
    dependency_reach: int = 0
    dependency_reach_truncated: bool = False
    dependency_path_namespace_ids: tuple[str, ...] = ()
    blast_radius: int = 0
    blast_radius_truncated: bool = False
    consumer_path_namespace_ids: tuple[str, ...] = ()
    directed_degree_centrality: float = 0.0
    cross_path_namespace_fan_in: int = 0
    cross_path_namespace_fan_out: int = 0


@dataclass(frozen=True, slots=True)
class ArchitectureSummary:
    modules: int
    import_edges: int
    consensus_edges: int
    graph_disagreements: int
    cyclic_sccs: int
    grimp_reported_internal_modules: int | None
    grimp_reported_import_edges: int | None
    grimp_reported_cyclic_sccs: int | None
    grimp_counts_consistent: bool | None


@dataclass(frozen=True, slots=True)
class CodeArchitectureAnalysis:
    database: str
    analysis_run_id: int | None
    status: ArchitectureStatus
    reason: str | None
    gate: ArchitectureGate
    gates: tuple[ArchitectureGateEvaluation, ...]
    providers: tuple[ArchitectureProviderStatus, ...]
    summary: ArchitectureSummary | None
    modules: tuple[ArchitectureModule, ...]
    symbols: tuple[ArchitectureSymbolComplexity, ...]
    imports: tuple[ArchitectureImportEdge, ...]
    cycles: tuple[ArchitectureCycle, ...]
    contracts: tuple[ArchitectureContract, ...]
    limitations: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": "code-architecture-analysis",
            "schema": CODE_ARCHITECTURE_SCHEMA,
            **asdict(self),
        }

    def digest_payload(self) -> dict[str, object]:
        """Return replay-stable evidence, excluding local/run identities."""

        providers = []
        for provider in self.providers:
            item = asdict(provider)
            item.pop("tool_run_id")
            item.pop("source_tool_run_id")
            item.pop("execution")
            item.pop("provider_gate")
            providers.append(item)
        return {
            "schema": CODE_ARCHITECTURE_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "gate": self.gate,
            "gates": [asdict(item) for item in self.gates],
            "providers": providers,
            "summary": None if self.summary is None else asdict(self.summary),
            "modules": [asdict(item) for item in self.modules],
            "symbols": [asdict(item) for item in self.symbols],
            "imports": [asdict(item) for item in self.imports],
            "cycles": [asdict(item) for item in self.cycles],
            "contracts": [asdict(item) for item in self.contracts],
            "limitations": list(self.limitations),
        }


class _MetricEvidence(Protocol):
    subject_kind: str
    subject_key: str
    metric_name: str
    value: float
    metadata: Mapping[str, object]


class _RelationEvidence(Protocol):
    relation_kind: str
    source_kind: str
    source_key: str
    target_kind: str
    target_key: str
    confidence: float | None


class _FindingEvidence(Protocol):
    category: str
    code: str
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
    tool_name: str
    tool_version: str | None
    provider_schema: str
    comparability_signature: str | None
    gate: str
    execution: str | None
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class _Metric:
    provider_id: str
    subject_kind: str
    subject_id: str
    name: str
    value: float
    confirmed: bool
    provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Relation:
    provider_id: str
    relation_kind: str
    source_kind: str
    source_id: str
    target_kind: str
    target_id: str
    confirmed: bool
    confidence: float | None


@dataclass(frozen=True, slots=True)
class _ModuleGraphMetrics:
    path_namespace_id: str
    dependency_reach: int
    dependency_reach_truncated: bool
    dependency_path_namespace_ids: tuple[str, ...]
    blast_radius: int
    blast_radius_truncated: bool
    consumer_path_namespace_ids: tuple[str, ...]
    directed_degree_centrality: float
    cross_path_namespace_fan_in: int
    cross_path_namespace_fan_out: int


def _root_hint(connection: sqlite3.Connection) -> Path:
    roots = [
        str(row[0])
        for row in connection.execute(
            "SELECT probable_root FROM projects WHERE status='current' "
            "AND probable_root IS NOT NULL ORDER BY probable_root COLLATE NOCASE"
        )
    ]
    if not roots:
        roots = [
            str(Path(str(row[0])).parent)
            for row in connection.execute(
                "SELECT current_path FROM files WHERE status='current' "
                "ORDER BY current_path COLLATE NOCASE"
            )
        ]
    if not roots:
        raise ValueError("code publication has no architecture root")
    try:
        root = Path(os.path.commonpath(roots))
    except ValueError as exc:
        raise ValueError("code publication spans incompatible architecture roots") from exc
    if not root.is_absolute():
        raise ValueError("code architecture root is not absolute")
    return root


def module_id_from_path(path: str, root: str | Path) -> str | None:
    """Map one Python path to the dotted module identity used by providers."""

    path_value = os.fspath(path)
    root_value = os.fspath(root)
    windows_syntax = (
        "\\" in path_value
        or "\\" in root_value
        or PureWindowsPath(path_value).drive != ""
        or PureWindowsPath(root_value).drive != ""
    )
    path_type = PureWindowsPath if windows_syntax else PurePosixPath
    selected = path_type(path_value)
    base = path_type(root_value)
    try:
        relative = selected.relative_to(base) if selected.is_absolute() else selected
    except ValueError:
        return None
    if relative.suffix.casefold() not in {".py", ".pyi"}:
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _provider_reason(
    provider: _ProviderEvidence | None,
    suite_status: _ProviderSuiteStatus | None,
) -> str | None:
    if provider is None or suite_status is None:
        return "provider_missing"
    if provider.status != "ready":
        return provider.reason or "provider_evidence_abstained"
    if suite_status.status != "ready":
        return suite_status.reason or f"provider_{suite_status.status}"
    if provider.effective_tool_run_id is None:
        return "effective_tool_run_missing"
    if len(provider.metrics) > CODE_ARCHITECTURE_METRIC_LIMIT:
        return "provider_metric_bound_exceeded"
    if len(provider.relations) > CODE_ARCHITECTURE_RELATION_LIMIT:
        return "provider_relation_bound_exceeded"
    return None


def _architecture_provider_status(
    provider_id: str,
    provider: _ProviderEvidence | None,
    suite_status: _ProviderSuiteStatus | None,
    reason: str | None,
) -> ArchitectureProviderStatus:
    return ArchitectureProviderStatus(
        provider_id,
        "ready" if reason is None else "abstained",
        reason,
        None if suite_status is None else suite_status.tool_name,
        None if suite_status is None else suite_status.tool_version,
        None if suite_status is None else suite_status.provider_schema,
        None if suite_status is None else suite_status.comparability_signature,
        None if suite_status is None else suite_status.gate,
        None if suite_status is None else suite_status.execution,
        None if provider is None else provider.tool_run_id,
        None if provider is None else provider.effective_tool_run_id,
        0 if provider is None else len(provider.metrics),
        0 if provider is None else len(provider.relations),
    )


def _provider_evidence(
    connection: sqlite3.Connection,
    analysis_run_id: int,
) -> tuple[dict[str, _ProviderEvidence], tuple[ArchitectureProviderStatus, ...]]:
    evidence = cast(
        dict[str, _ProviderEvidence],
        read_external_provider_evidence(
            connection,
            analysis_run_id,
            provider_ids=CODE_ARCHITECTURE_REQUIRED_PROVIDERS,
        ),
    )
    suite = read_external_evidence_suite(
        connection,
        analysis_run_id,
        enforce_current_runtime=False,
        provider_ids=CODE_ARCHITECTURE_REQUIRED_PROVIDERS,
    )
    suite_statuses = cast(
        dict[str, _ProviderSuiteStatus],
        {item.provider_id: item for item in suite.providers},
    )
    selected: dict[str, _ProviderEvidence] = {}
    statuses: list[ArchitectureProviderStatus] = []
    for provider_id in CODE_ARCHITECTURE_REQUIRED_PROVIDERS:
        provider = evidence.get(provider_id)
        suite_status = suite_statuses.get(provider_id)
        reason = _provider_reason(provider, suite_status)
        if provider is not None and reason is None:
            selected[provider_id] = provider
        statuses.append(_architecture_provider_status(provider_id, provider, suite_status, reason))
    return selected, tuple(statuses)


def _read_metrics(evidence: dict[str, _ProviderEvidence]) -> tuple[_Metric, ...]:
    metrics: list[_Metric] = []
    for provider_id in sorted(evidence):
        for metric in evidence[provider_id].metrics:
            value = float(metric.value)
            if not math.isfinite(value):
                raise ValueError("architecture metric is not finite")
            metrics.append(
                _Metric(
                    provider_id,
                    str(metric.subject_kind),
                    metric.subject_key,
                    metric.metric_name,
                    value,
                    True,
                    dict(metric.metadata),
                )
            )
    metrics.sort(key=lambda item: (item.provider_id, item.subject_kind, item.subject_id, item.name))
    return tuple(metrics)


def _read_relations(evidence: dict[str, _ProviderEvidence]) -> tuple[_Relation, ...]:
    relations = [
        _Relation(
            provider_id,
            relation.relation_kind,
            str(relation.source_kind),
            relation.source_key,
            str(relation.target_kind),
            relation.target_key,
            True,
            relation.confidence,
        )
        for provider_id in sorted(evidence)
        for relation in evidence[provider_id].relations
    ]
    relations.sort(
        key=lambda item: (
            item.provider_id,
            item.relation_kind,
            item.source_kind,
            item.source_id,
            item.target_kind,
            item.target_id,
        )
    )
    return tuple(relations)


def _import_edge(
    source: str,
    target: str,
    providers: dict[str, _Relation],
) -> ArchitectureImportEdge:
    ruff = providers.get("ruff-analyze-imports")
    grimp = providers.get("grimp-architecture")
    comparison: ArchitectureComparison = (
        "both" if ruff is not None and grimp is not None else "ruff_only"
    )
    if ruff is None:
        comparison = "grimp_only"
    evidence = tuple(item for item in (ruff, grimp) if item is not None)
    confidences = tuple(item.confidence for item in evidence if item.confidence is not None)
    return ArchitectureImportEdge(
        source,
        target,
        comparison,
        ruff is not None,
        grimp is not None,
        bool(evidence) and all(item.confirmed for item in evidence),
        min(confidences) if confidences else None,
    )


def _is_module_import(relation: _Relation) -> bool:
    return bool(
        relation.provider_id in {"ruff-analyze-imports", "grimp-architecture"}
        and relation.relation_kind == "module_import"
        and relation.source_kind == "module"
        and relation.target_kind == "module"
    )


def _import_edges(relations: tuple[_Relation, ...]) -> tuple[ArchitectureImportEdge, ...]:
    observations: dict[tuple[str, str], dict[str, _Relation]] = {}
    for relation in filter(_is_module_import, relations):
        observations.setdefault((relation.source_id, relation.target_id), {})[
            relation.provider_id
        ] = relation
    return tuple(
        _import_edge(source, target, providers)
        for (source, target), providers in sorted(observations.items())
    )


def _strongly_connected_components(
    modules: set[str],
    edges: tuple[ArchitectureImportEdge, ...],
) -> tuple[ArchitectureCycle, ...]:
    adjacency: dict[str, list[str]] = {module: [] for module in modules}
    reverse: dict[str, list[str]] = {module: [] for module in modules}
    self_edges: set[str] = set()
    for edge in edges:
        adjacency.setdefault(edge.source_module, []).append(edge.target_module)
        reverse.setdefault(edge.target_module, []).append(edge.source_module)
        if edge.source_module == edge.target_module:
            self_edges.add(edge.source_module)
    for graph in (adjacency, reverse):
        for values in graph.values():
            values.sort()
    components = _cyclic_components(
        _finish_order(modules, adjacency),
        reverse,
        self_edges,
    )
    return tuple(
        ArchitectureCycle(
            "import-scc-v1:xxh3_128:"
            + fingerprint_text(canonical_json({"modules": component})).xxh3_128,
            component,
        )
        for component in components
    )


def _finish_order(modules: set[str], adjacency: dict[str, list[str]]) -> list[str]:
    visited: set[str] = set()
    order: list[str] = []
    for start in sorted(modules):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node, index = stack[-1]
            targets = adjacency.get(node, [])
            if index < len(targets):
                target = targets[index]
                stack[-1] = (node, index + 1)
                if target not in visited:
                    visited.add(target)
                    stack.append((target, 0))
                continue
            order.append(node)
            stack.pop()
    return order


def _cyclic_components(
    order: list[str],
    reverse: dict[str, list[str]],
    self_edges: set[str],
) -> list[tuple[str, ...]]:
    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[str] = []
        component_stack = [start]
        while component_stack:
            node = component_stack.pop()
            component.append(node)
            for target in reversed(reverse.get(node, [])):
                if target not in assigned:
                    assigned.add(target)
                    component_stack.append(target)
        frozen = tuple(sorted(component))
        if len(frozen) > 1 or frozen[0] in self_edges:
            components.append(frozen)
    components.sort()
    return components


def _symbol_complexity(metric: _Metric) -> ArchitectureSymbolComplexity | None:
    if metric.subject_kind != "symbol" or metric.name != "cognitive_complexity":
        return None
    module = metric.provenance.get("module_id") or metric.provenance.get("module")
    module_id = str(module) if isinstance(module, str) and module else ""
    if not module_id and metric.subject_id.count(":") >= 3:
        module_id = metric.subject_id.rsplit(":", 3)[0]
    if not module_id and "::" in metric.subject_id:
        module_id = metric.subject_id.split("::", 1)[0]
    if not module_id:
        return None
    return ArchitectureSymbolComplexity(metric.subject_id, module_id, metric.value)


def _complexity(
    metrics: tuple[_Metric, ...],
) -> tuple[
    dict[str, tuple[float | None, float | None]],
    tuple[ArchitectureSymbolComplexity, ...],
]:
    totals: dict[str, float] = {}
    maxima: dict[str, float] = {}
    symbols: list[ArchitectureSymbolComplexity] = []
    for metric in metrics:
        if metric.provider_id != "complexipy-cognitive" or not metric.confirmed:
            continue
        if metric.subject_kind == "module" and metric.name == "module_cognitive_complexity_total":
            totals[metric.subject_id] = metric.value
        if metric.subject_kind == "module" and metric.name == "module_cognitive_complexity_max":
            maxima[metric.subject_id] = metric.value
        symbol = _symbol_complexity(metric)
        if symbol is not None:
            symbols.append(symbol)
    if len(symbols) > CODE_ARCHITECTURE_SYMBOL_LIMIT:
        raise ValueError("architecture symbol complexity exceeded its bound")
    symbols.sort(key=lambda item: (item.module_id, item.symbol_id))
    modules = {
        module: (totals.get(module), maxima.get(module))
        for module in sorted(set(totals) | set(maxima))
    }
    return modules, tuple(symbols)


def _grimp_metrics(
    metrics: tuple[_Metric, ...],
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    module_metrics: dict[str, dict[str, int]] = {}
    run_metrics: dict[str, int] = {}
    supported_module = {
        "module_fan_in",
        "module_fan_out",
        "module_scc_size",
        "module_cycle_membership",
    }
    supported_run = {
        "internal_module_count",
        "internal_import_edge_count",
        "cyclic_scc_count",
    }
    for metric in metrics:
        if metric.provider_id != "grimp-architecture" or not metric.confirmed:
            continue
        if metric.value < 0 or not metric.value.is_integer():
            raise ValueError("Grimp architecture count metric is not a non-negative integer")
        value = int(metric.value)
        if metric.subject_kind == "module" and metric.name in supported_module:
            module_metrics.setdefault(metric.subject_id, {})[metric.name] = value
        elif metric.subject_kind == "run" and metric.name in supported_run:
            prior = run_metrics.setdefault(metric.name, value)
            if prior != value:
                raise ValueError("Grimp run metric has conflicting observations")
    return module_metrics, run_metrics


def _contract_metric_state(
    metrics: tuple[_Metric, ...],
) -> tuple[set[str], dict[str, int]]:
    evaluated: set[str] = set()
    declared_violations: dict[str, int] = {}
    for metric in metrics:
        if metric.provider_id != "grimp-architecture" or metric.subject_kind != "contract":
            continue
        if metric.name == "architecture_contract_evaluated" and metric.confirmed:
            evaluated.add(metric.subject_id)
        elif metric.name == "architecture_contract_violations" and metric.confirmed:
            declared_violations[metric.subject_id] = int(metric.value)
    return evaluated, declared_violations


def _contract_record(
    contract_id: str,
    evaluated: set[str],
    declared_violations: dict[str, int],
    records: list[dict[str, object]],
) -> ArchitectureContract:
    def string_values(name: str) -> tuple[str, ...]:
        return tuple(
            sorted({str(item[name]) for item in records if isinstance(item.get(name), str)})
        )

    chains = {
        tuple(raw_chain[: CODE_ARCHITECTURE_IMPORT_CHAIN_DEPTH + 1])
        for item in records
        if isinstance((raw_chain := item.get("import_chain")), list)
        and all(isinstance(value, str) for value in raw_chain)
    }
    schemas = sorted(
        {
            str(item["contract_schema"])
            for item in records
            if isinstance(item.get("contract_schema"), str) and item["contract_schema"]
        }
    )
    count = max(declared_violations.get(contract_id, 0), len(records))
    was_evaluated = contract_id in evaluated
    status: ArchitectureContractStatus = "abstained"
    if was_evaluated:
        status = "failed" if count else "passed"
    return ArchitectureContract(
        contract_id,
        status,
        was_evaluated,
        count,
        string_values("importer_module"),
        string_values("imported_module"),
        tuple(sorted(chains)[:CODE_ARCHITECTURE_IMPORT_CHAIN_LIMIT]),
        schemas[0] if schemas else None,
    )


def _contracts(
    evidence: dict[str, _ProviderEvidence],
    metrics: tuple[_Metric, ...],
) -> tuple[ArchitectureContract, ...]:
    provider = evidence.get("grimp-architecture")
    if provider is None:
        return ()
    evaluated, declared_violations = _contract_metric_state(metrics)
    findings = tuple(item for item in provider.findings if item.category == "architecture")
    if len(findings) > CODE_ARCHITECTURE_METRIC_LIMIT:
        raise ValueError("architecture contract findings exceeded their bound")
    violations: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        violations.setdefault(finding.code, []).append(dict(finding.metadata))
    contract_ids = sorted(evaluated | set(declared_violations) | set(violations))
    return tuple(
        _contract_record(
            contract_id,
            evaluated,
            declared_violations,
            violations.get(contract_id, []),
        )
        for contract_id in contract_ids
    )


def _module_indexes(
    edges: tuple[ArchitectureImportEdge, ...],
    cycles: tuple[ArchitectureCycle, ...],
    contracts: tuple[ArchitectureContract, ...],
    complexities: dict[str, tuple[float | None, float | None]],
    symbols: tuple[ArchitectureSymbolComplexity, ...],
    grimp_metrics: dict[str, dict[str, int]],
) -> tuple[
    set[str],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, list[str]],
    dict[str, set[str]],
    dict[str, int],
]:
    module_ids = set(complexities)
    module_ids.update(item.module_id for item in symbols)
    module_ids.update(grimp_metrics)
    for edge in edges:
        module_ids.update((edge.source_module, edge.target_module))
    fan_in: dict[str, set[str]] = {module: set() for module in module_ids}
    fan_out: dict[str, set[str]] = {module: set() for module in module_ids}
    for edge in edges:
        fan_out.setdefault(edge.source_module, set()).add(edge.target_module)
        fan_in.setdefault(edge.target_module, set()).add(edge.source_module)
    cycles_by_module: dict[str, list[str]] = {module: [] for module in module_ids}
    for cycle in cycles:
        for module in cycle.modules:
            cycles_by_module.setdefault(module, []).append(cycle.cycle_id)
    contracts_by_module: dict[str, set[str]] = {module: set() for module in module_ids}
    for contract in contracts:
        for module in (*contract.importer_modules, *contract.imported_modules):
            module_ids.add(module)
            contracts_by_module.setdefault(module, set()).add(contract.contract_id)
    symbol_counts: dict[str, int] = {}
    for symbol in symbols:
        symbol_counts[symbol.module_id] = symbol_counts.get(symbol.module_id, 0) + 1
    return module_ids, fan_in, fan_out, cycles_by_module, contracts_by_module, symbol_counts


def _module_path_namespace(module_id: str) -> str:
    """Return only the first dotted path namespace, never an ownership claim."""

    first, _, _ = module_id.partition(".")
    return first or module_id


def _bounded_reach(
    module_id: str,
    adjacency: Mapping[str, set[str]],
    *,
    limit: int,
) -> tuple[frozenset[str], bool]:
    """Return a deterministic transitive closure and whether it exceeded ``limit``."""

    if limit < 1:
        raise ValueError("architecture reachability limit must be positive")
    seen = {module_id}
    reached: set[str] = set()
    queue = deque([module_id])
    while queue:
        source = queue.popleft()
        for target in sorted(adjacency.get(source, set())):
            if target in seen:
                continue
            if len(reached) >= limit:
                return frozenset(reached), True
            seen.add(target)
            reached.add(target)
            queue.append(target)
    return frozenset(reached), False


def _module_graph_metrics(
    module_ids: set[str],
    fan_in: Mapping[str, set[str]],
    fan_out: Mapping[str, set[str]],
    *,
    reachability_limit: int,
) -> dict[str, _ModuleGraphMetrics]:
    module_count = len(module_ids)
    denominator = 2 * (module_count - 1)
    result: dict[str, _ModuleGraphMetrics] = {}
    for module_id in sorted(module_ids):
        path_namespace_id = _module_path_namespace(module_id)
        direct_consumers = fan_in.get(module_id, set()) - {module_id}
        direct_dependencies = fan_out.get(module_id, set()) - {module_id}
        dependencies, dependencies_truncated = _bounded_reach(
            module_id,
            fan_out,
            limit=reachability_limit,
        )
        consumers, consumers_truncated = _bounded_reach(
            module_id,
            fan_in,
            limit=reachability_limit,
        )
        centrality = (
            0.0
            if denominator == 0
            else round((len(direct_consumers) + len(direct_dependencies)) / denominator, 12)
        )
        result[module_id] = _ModuleGraphMetrics(
            path_namespace_id,
            len(dependencies),
            dependencies_truncated,
            tuple(
                sorted(
                    {
                        _module_path_namespace(dependency)
                        for dependency in dependencies
                        if _module_path_namespace(dependency) != path_namespace_id
                    }
                )
            ),
            len(consumers),
            consumers_truncated,
            tuple(
                sorted(
                    {
                        _module_path_namespace(consumer)
                        for consumer in consumers
                        if _module_path_namespace(consumer) != path_namespace_id
                    }
                )
            ),
            centrality,
            sum(
                _module_path_namespace(consumer) != path_namespace_id
                for consumer in direct_consumers
            ),
            sum(
                _module_path_namespace(dependency) != path_namespace_id
                for dependency in direct_dependencies
            ),
        )
    return result


def _modules(
    edges: tuple[ArchitectureImportEdge, ...],
    cycles: tuple[ArchitectureCycle, ...],
    contracts: tuple[ArchitectureContract, ...],
    complexities: dict[str, tuple[float | None, float | None]],
    symbols: tuple[ArchitectureSymbolComplexity, ...],
    grimp_metrics: dict[str, dict[str, int]],
    *,
    reachability_limit: int = CODE_ARCHITECTURE_REACHABILITY_LIMIT,
) -> tuple[ArchitectureModule, ...]:
    (
        module_ids,
        fan_in,
        fan_out,
        cycles_by_module,
        contracts_by_module,
        symbol_counts,
    ) = _module_indexes(edges, cycles, contracts, complexities, symbols, grimp_metrics)
    graph_metrics = _module_graph_metrics(
        module_ids,
        fan_in,
        fan_out,
        reachability_limit=reachability_limit,
    )
    return tuple(
        ArchitectureModule(
            module,
            len(fan_in.get(module, set())),
            len(fan_out.get(module, set())),
            complexities.get(module, (None, None))[0],
            complexities.get(module, (None, None))[1],
            symbol_counts.get(module, 0),
            tuple(sorted(cycles_by_module.get(module, []))),
            tuple(sorted(contracts_by_module.get(module, set()))),
            grimp_metrics.get(module, {}).get("module_fan_in"),
            grimp_metrics.get(module, {}).get("module_fan_out"),
            grimp_metrics.get(module, {}).get("module_scc_size"),
            (
                None
                if "module_cycle_membership" not in grimp_metrics.get(module, {})
                else bool(grimp_metrics[module]["module_cycle_membership"])
            ),
            graph_metrics[module].path_namespace_id,
            graph_metrics[module].dependency_reach,
            graph_metrics[module].dependency_reach_truncated,
            graph_metrics[module].dependency_path_namespace_ids,
            graph_metrics[module].blast_radius,
            graph_metrics[module].blast_radius_truncated,
            graph_metrics[module].consumer_path_namespace_ids,
            graph_metrics[module].directed_degree_centrality,
            graph_metrics[module].cross_path_namespace_fan_in,
            graph_metrics[module].cross_path_namespace_fan_out,
        )
        for module in sorted(module_ids)
    )


def _summary(
    modules: tuple[ArchitectureModule, ...],
    edges: tuple[ArchitectureImportEdge, ...],
    cycles: tuple[ArchitectureCycle, ...],
    run_metrics: dict[str, int],
) -> ArchitectureSummary:
    reported_modules = run_metrics.get("internal_module_count")
    reported_edges = run_metrics.get("internal_import_edge_count")
    reported_cycles = run_metrics.get("cyclic_scc_count")
    observed_values = (reported_modules, reported_edges, reported_cycles)
    grimp_edges = sum(item.grimp_observed for item in edges)
    grimp_cycle_modules = {
        item.module_id for item in modules if item.grimp_cycle_membership is True
    }
    derived_grimp_cycles = sum(1 for cycle in cycles if set(cycle.modules) <= grimp_cycle_modules)
    consistent = None
    if all(value is not None for value in observed_values):
        consistent = bool(
            reported_modules == len(modules)
            and reported_edges == grimp_edges
            and reported_cycles == derived_grimp_cycles
        )
    return ArchitectureSummary(
        len(modules),
        len(edges),
        sum(item.comparison == "both" for item in edges),
        sum(item.comparison != "both" for item in edges),
        len(cycles),
        reported_modules,
        reported_edges,
        reported_cycles,
        consistent,
    )


def _not_evaluated_gates(reason: str) -> tuple[ArchitectureGateEvaluation, ...]:
    return (
        ArchitectureGateEvaluation("import_graph_consensus", "not_evaluated", reason),
        ArchitectureGateEvaluation("architecture_contracts", "not_evaluated", reason),
        ArchitectureGateEvaluation(
            "module_complexity_displacement",
            "not_evaluated",
            "requires_comparable_publication_diff",
        ),
    )


def _contract_gate(
    contracts: tuple[ArchitectureContract, ...],
    grimp_provider_gate: str | None,
) -> ArchitectureGateEvaluation:
    if not contracts:
        return ArchitectureGateEvaluation(
            "architecture_contracts",
            "not_evaluated",
            "no_versioned_architecture_contracts_recorded",
        )
    if any(item.status == "failed" for item in contracts):
        return ArchitectureGateEvaluation(
            "architecture_contracts",
            "failed",
            "architecture_contract_violation_observed",
        )
    if any(item.status == "abstained" for item in contracts):
        return ArchitectureGateEvaluation(
            "architecture_contracts",
            "not_evaluated",
            "architecture_contract_not_evaluated",
        )
    return ArchitectureGateEvaluation(
        "architecture_contracts",
        "baseline" if grimp_provider_gate == "baseline" else "passed",
        None,
    )


def _architecture_gates(
    provider_statuses: tuple[ArchitectureProviderStatus, ...],
    contracts: tuple[ArchitectureContract, ...],
    summary: ArchitectureSummary,
) -> tuple[str | None, tuple[ArchitectureGateEvaluation, ...]]:
    missing = tuple(item for item in provider_statuses if item.status != "ready")
    if missing:
        reason = "required_provider_not_ready:" + ",".join(
            f"{item.provider_id}:{item.reason or item.status}" for item in missing
        )
        return reason, _not_evaluated_gates(reason)
    grimp = next(item for item in provider_statuses if item.provider_id == "grimp-architecture")
    graph_failed = summary.graph_disagreements > 0 or summary.grimp_counts_consistent is False
    graph_reason = None
    if summary.graph_disagreements:
        graph_reason = f"ruff_grimp_edge_disagreements:{summary.graph_disagreements}"
    elif summary.grimp_counts_consistent is False:
        graph_reason = "grimp_reported_graph_counts_disagree_with_projection"
    return None, (
        ArchitectureGateEvaluation(
            "import_graph_consensus",
            "failed" if graph_failed else "passed",
            graph_reason,
        ),
        _contract_gate(contracts, grimp.provider_gate),
        ArchitectureGateEvaluation(
            "module_complexity_displacement",
            "not_evaluated",
            "requires_comparable_publication_diff",
        ),
    )


def read_code_architecture_analysis(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    database: str = "",
    reachability_limit: int = CODE_ARCHITECTURE_REACHABILITY_LIMIT,
) -> CodeArchitectureAnalysis:
    """Read providers and derive bounded graph analytics from a read-only connection."""

    try:
        evidence, provider_statuses = _provider_evidence(connection, analysis_run_id)
        metrics = _read_metrics(evidence)
        relations = _read_relations(evidence)
        edges = _import_edges(relations)
        complexities, symbols = _complexity(metrics)
        grimp_module_metrics, grimp_run_metrics = _grimp_metrics(metrics)
        contracts = _contracts(evidence, metrics)
        module_ids = set(complexities)
        for edge in edges:
            module_ids.update((edge.source_module, edge.target_module))
        cycles = _strongly_connected_components(module_ids, edges)
        modules = _modules(
            edges,
            cycles,
            contracts,
            complexities,
            symbols,
            grimp_module_metrics,
            reachability_limit=reachability_limit,
        )
        summary = _summary(modules, edges, cycles, grimp_run_metrics)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return CodeArchitectureAnalysis(
            database,
            analysis_run_id,
            "abstained",
            f"architecture_evidence_incompatible:{type(exc).__name__}:{exc}",
            "abstained",
            _not_evaluated_gates("architecture_evidence_incompatible"),
            (),
            None,
            (),
            (),
            (),
            (),
            (),
            ("architecture_provider_evidence_was_not_interpreted",),
        )
    reason, gates = _architecture_gates(provider_statuses, contracts, summary)
    limitations = (
        "import_graph_is_static_and_does_not_observe_dynamic_imports",
        "ruff_grimp_disagreement_is_preserved_not_forced_to_consensus",
        "cognitive_complexity_is_a_tool_metric_not_defect_probability",
        "path_namespace_id_is_only_the_first_dotted_module_component",
        "logical_ownership_is_observed_only_by_the_separate_explicit_partial_registry",
        "repository_and_state_ownership_are_not_observed_by_the_import_graph",
        "directed_degree_centrality_formula:" + CODE_ARCHITECTURE_CENTRALITY_FORMULA,
        f"transitive_reach_is_exact_until_module_limit:{reachability_limit}",
        "truncated_transitive_reach_counts_are_lower_bounds",
        "architecture_evidence_is_advisory_and_has_no_mutation_authority",
    )
    return CodeArchitectureAnalysis(
        database,
        analysis_run_id,
        "abstained" if reason is not None else "ready",
        reason,
        "abstained" if reason is not None else "observed",
        gates,
        provider_statuses,
        summary,
        modules,
        symbols,
        edges,
        cycles,
        contracts,
        limitations,
    )


def analyze_code_architecture(state_directory: Path) -> CodeArchitectureAnalysis:
    """Return the latest immutable architecture view without writing Code state."""

    state_directory = Path(state_directory)
    database = state_directory / "code.sqlite3"
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        return CodeArchitectureAnalysis(
            str(database),
            None,
            "abstained",
            "code_state_missing",
            "abstained",
            _not_evaluated_gates("code_state_missing"),
            (),
            None,
            (),
            (),
            (),
            (),
            (),
            (),
        )
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != CODE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"code state schema {schema_version} is unsupported for architecture"
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
                _root_hint(connection)
                return read_code_architecture_analysis(
                    connection,
                    int(latest["analysis_run_id"]),
                    database=str(database),
                )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        reason = f"code_state_unavailable:{type(exc).__name__}:{exc}"
    return CodeArchitectureAnalysis(
        str(database),
        None,
        "abstained",
        reason,
        "abstained",
        _not_evaluated_gates(reason),
        (),
        None,
        (),
        (),
        (),
        (),
        (),
        (),
    )


def bounded_import_chains(
    analysis: CodeArchitectureAnalysis,
    module_id: str,
    *,
    limit: int = CODE_ARCHITECTURE_IMPORT_CHAIN_LIMIT,
    depth: int = CODE_ARCHITECTURE_IMPORT_CHAIN_DEPTH,
) -> tuple[tuple[str, ...], ...]:
    """Return deterministic inbound import chains ending at ``module_id``."""

    if analysis.status != "ready" or limit < 1 or depth < 1:
        return ()
    reverse: dict[str, list[str]] = {}
    for edge in analysis.imports:
        reverse.setdefault(edge.target_module, []).append(edge.source_module)
    for values in reverse.values():
        values.sort()
    found: set[tuple[str, ...]] = set()
    queue: deque[tuple[str, ...]] = deque([(module_id,)])
    while queue and len(found) < limit:
        reverse_path = queue.popleft()
        if len(reverse_path) > depth:
            continue
        tail = reverse_path[-1]
        for source in reverse.get(tail, []):
            if source in reverse_path:
                continue
            candidate = (*reverse_path, source)
            found.add(tuple(reversed(candidate)))
            if len(candidate) <= depth:
                queue.append(candidate)
            if len(found) >= limit:
                break
    return tuple(sorted(found, key=lambda item: (len(item), item)))


__all__ = [
    "CODE_ARCHITECTURE_CENTRALITY_FORMULA",
    "CODE_ARCHITECTURE_IMPORT_CHAIN_DEPTH",
    "CODE_ARCHITECTURE_IMPORT_CHAIN_LIMIT",
    "CODE_ARCHITECTURE_REACHABILITY_LIMIT",
    "CODE_ARCHITECTURE_REQUIRED_PROVIDERS",
    "CODE_ARCHITECTURE_SCHEMA",
    "ArchitectureContract",
    "ArchitectureCycle",
    "ArchitectureGateEvaluation",
    "ArchitectureImportEdge",
    "ArchitectureModule",
    "ArchitectureProviderStatus",
    "ArchitectureSummary",
    "ArchitectureSymbolComplexity",
    "CodeArchitectureAnalysis",
    "analyze_code_architecture",
    "bounded_import_chains",
    "module_id_from_path",
    "read_code_architecture_analysis",
]
