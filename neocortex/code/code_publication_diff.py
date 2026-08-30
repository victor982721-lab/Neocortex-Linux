"""Deterministic read-only comparison of two completed Code publications."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .code_architecture_analysis import (
    ArchitectureModule,
    CodeArchitectureAnalysis,
    read_code_architecture_analysis,
)
from .code_coverage_analysis import (
    CODE_COVERAGE_SCHEMA,
    CodeCoverageAnalysis,
    CoverageComparison,
    compare_code_coverage,
    read_code_coverage_analysis,
)
from .code_engineering_analytics import (
    CodeEngineeringAnalytics,
    read_code_engineering_analysis,
)
from .code_external_evidence import (
    RUFF_CONFIGURATION_SIGNATURE,
    ExternalEvidenceStatus,
    external_status_digest_payload,
    read_external_evidence,
)
from .code_unused_analysis import CodeUnusedAnalysis, read_code_unused_analysis
from .code_supply_chain_analysis import (
    CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS,
    CodeSupplyChainAnalysis,
    SupplyChainObservation,
    read_code_supply_chain_analysis,
)
from .code_schema import (
    CODE_SCHEMA_VERSION,
    readonly_code_database,
    validate_code_schema,
)
from .external_evidence_models import (
    ExternalEvidenceSuiteStatus,
    ExternalProviderFinding,
    ExternalProviderStatus,
    normalize_external_finding_message,
)
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_findings,
    read_external_provider_finding_ids,
)
from neocortex.workflow.self_analysis.self_analysis_status import require_sqlite_sidecars_absent
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

CODE_PUBLICATION_DIFF_SCHEMA = "neocortex.code-publication-diff/v10"
# v10 deliberately removes the path-prefix-as-owner projection.  Older wires
# are therefore neither structurally nor semantically compatible.
CODE_PUBLICATION_DIFF_COMPATIBLE_SCHEMAS: tuple[str, ...] = ()
CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT = 20
_LEGACY_RUFF_COMPARABILITY_REASON = "legacy_ruff_contract_compatibility_projection"
_RELOCATION_AWARE_PROVIDER_IDS = frozenset({"mypy-trusted-project", "pyright-trusted-project"})
CODE_PUBLICATION_DIFF_MAX_CALLS = 250_000
CODE_PUBLICATION_DIFF_MAX_HOTSPOTS = 20_000
CODE_PUBLICATION_DIFF_MAX_MODULES = 50_000
_UNUSED_REQUIRED_PRECISION_GATES = frozenset(
    {
        "calibration_probable_unused_precision",
        "holdout_probable_unused_precision",
    }
)

DiffStatus = Literal["ready", "abstained"]


@dataclass(frozen=True, slots=True)
class CodePublicationSnapshot:
    """Bounded facts read from one completed Code owner."""

    state_directory: str
    database: str
    root: str
    processing_signature: str
    python_files: int
    complete_python_files: int
    call_edges: int
    resolved_call_edges: int
    hotspots: int
    high_complexity: int
    long_function: int
    probable_dead: int


@dataclass(frozen=True, slots=True)
class CodeCallResolutionChange:
    """One bounded example from a common unchanged call site."""

    change: Literal["newly_resolved", "corrected", "lost"]
    path: str
    source_symbol: str | None
    name: str
    target_hint: str | None
    line: int
    baseline_target: str | None
    current_target: str | None


@dataclass(frozen=True, slots=True)
class CodeCallResolutionDelta:
    common_call_sites: int
    baseline_only_call_sites: int
    current_only_call_sites: int
    unchanged_resolved: int
    still_unresolved: int
    newly_resolved: int
    corrected: int
    lost: int
    examples: tuple[CodeCallResolutionChange, ...]


@dataclass(frozen=True, slots=True)
class CodeHotspotDelta:
    common: int
    added: int
    removed: int
    changed_evidence: int
    added_examples: tuple[str, ...]
    removed_examples: tuple[str, ...]
    changed_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeExternalEvidenceDelta:
    """Comparable Ruff evidence and its non-mutating acceptance gate."""

    status: Literal["ready", "not_evaluated"]
    reason: str | None
    baseline: ExternalEvidenceStatus
    current: ExternalEvidenceStatus
    common: int
    added: int | None
    resolved: int | None
    added_examples: tuple[str, ...]
    resolved_examples: tuple[str, ...]
    gate: Literal["passed", "failed", "not_evaluated"]


@dataclass(frozen=True, slots=True)
class CodeProviderFindingRelocation:
    """One typed finding whose semantic evidence stayed exact but moved."""

    baseline_finding_id: str
    current_finding_id: str
    path: str
    category: str
    code: str
    severity: str
    message: str
    baseline_start_line: int
    baseline_start_column: int
    baseline_end_line: int
    baseline_end_column: int
    current_start_line: int
    current_start_column: int
    current_end_line: int
    current_end_column: int


@dataclass(frozen=True, slots=True)
class CodeProviderEvidenceDelta:
    provider_id: str
    status: Literal["ready", "not_evaluated"]
    reason: str | None
    baseline: ExternalProviderStatus | None
    current: ExternalProviderStatus | None
    common: int
    added: int | None
    resolved: int | None
    gate: Literal["passed", "failed", "not_evaluated"]
    relocated: int | None = None
    relocation_examples: tuple[CodeProviderFindingRelocation, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeModuleArchitectureDelta:
    module_id: str
    baseline_cognitive_complexity: float | None
    current_cognitive_complexity: float | None
    cognitive_complexity_delta: float | None
    fan_in_delta: int
    fan_out_delta: int
    baseline_cycle_ids: tuple[str, ...]
    current_cycle_ids: tuple[str, ...]
    baseline_contract_ids: tuple[str, ...]
    current_contract_ids: tuple[str, ...]
    baseline_dependency_reach: int | None = None
    current_dependency_reach: int | None = None
    dependency_reach_delta: int | None = None
    baseline_dependency_reach_truncated: bool = False
    current_dependency_reach_truncated: bool = False
    dependency_reach_status: Literal["comparable", "not_comparable"] = "not_comparable"
    dependency_reach_reason: str | None = "dependency_reach_not_compared"
    baseline_blast_radius: int | None = None
    current_blast_radius: int | None = None
    blast_radius_delta: int | None = None
    baseline_blast_radius_truncated: bool = False
    current_blast_radius_truncated: bool = False
    blast_radius_status: Literal["comparable", "not_comparable"] = "not_comparable"
    blast_radius_reason: str | None = "blast_radius_not_compared"
    baseline_directed_degree_centrality: float | None = None
    current_directed_degree_centrality: float | None = None
    directed_degree_centrality_delta: float | None = None
    baseline_cross_path_namespace_fan_in: int | None = None
    current_cross_path_namespace_fan_in: int | None = None
    cross_path_namespace_fan_in_delta: int | None = None
    baseline_cross_path_namespace_fan_out: int | None = None
    current_cross_path_namespace_fan_out: int | None = None
    cross_path_namespace_fan_out_delta: int | None = None
    graph_metrics_status: Literal["comparable", "not_comparable"] = "not_comparable"
    graph_metrics_reason: str | None = "graph_metrics_not_compared"


@dataclass(frozen=True, slots=True)
class CodeComplexityDisplacement:
    target_module: str
    target_decrease: float
    recipient_modules: tuple[str, ...]
    recipient_increase: float
    import_relationships: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeArchitectureDelta:
    status: Literal["ready", "not_evaluated"]
    reason: str | None
    modules: tuple[CodeModuleArchitectureDelta, ...]
    added_failed_contracts: tuple[str, ...]
    resolved_failed_contracts: tuple[str, ...]
    added_cycles: tuple[tuple[str, ...], ...]
    resolved_cycles: tuple[tuple[str, ...], ...]
    displaced_complexity: tuple[CodeComplexityDisplacement, ...]
    architecture_contracts_not_degraded: Literal["passed", "failed", "not_evaluated"]
    no_new_import_cycles: Literal["passed", "failed", "not_evaluated"]
    module_complexity_not_displaced: Literal["passed", "failed", "not_evaluated"]


@dataclass(frozen=True, slots=True)
class CodeEngineeringDimensionState:
    """Availability of one dimension in each publication; never a combined score."""

    dimension: Literal["complexity", "coverage", "mutation", "history", "graph"]
    baseline_status: str
    baseline_reason: str | None
    current_status: str
    current_reason: str | None


@dataclass(frozen=True, slots=True)
class CodeEngineeringGateDelta:
    gate: Literal["mutation_measurement_complete", "mutation_score_not_degraded"]
    status: Literal["passed", "failed", "not_evaluated"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class CodeEngineeringAnalyticsDelta:
    """Comparable mutation evidence alongside independent dimension states."""

    status: Literal["comparable", "not_comparable"]
    reason: str | None
    baseline_status: str
    baseline_reason: str | None
    current_status: str
    current_reason: str | None
    dimensions: tuple[CodeEngineeringDimensionState, ...]
    baseline_mutation_scope_signature: str | None
    current_mutation_scope_signature: str | None
    baseline_mutation_score: float | None
    current_mutation_score: float | None
    mutation_score_delta: float | None
    gates: tuple[CodeEngineeringGateDelta, ...]
    limitations: tuple[str, ...] = (
        "engineering_dimensions_are_not_aggregated",
        "mutation_score_is_not_defect_probability",
    )
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False
    aggregate_score: None = None
    defect_probability: None = None


@dataclass(frozen=True, slots=True)
class CodeUnusedCandidateExample:
    candidate_id: str
    relative_path: str
    symbol: str | None
    state: str


@dataclass(frozen=True, slots=True)
class CodeUnusedStateChange:
    candidate_id: str
    relative_path: str
    symbol: str | None
    baseline_state: str
    current_state: str


@dataclass(frozen=True, slots=True)
class CodeUnusedAnalysisDelta:
    """Comparable identity/state changes; never a defect or deletion score."""

    status: Literal["ready", "not_evaluated"]
    reason: str | None
    baseline_provider_signature: str | None
    current_provider_signature: str | None
    baseline_calibration_signature: str | None
    current_calibration_signature: str | None
    common: int
    added: int | None
    removed: int | None
    state_changes: int | None
    high_consensus_added: int | None
    high_consensus_resolved: int | None
    added_examples: tuple[CodeUnusedCandidateExample, ...]
    removed_examples: tuple[CodeUnusedCandidateExample, ...]
    state_change_examples: tuple[CodeUnusedStateChange, ...]
    gate: Literal["passed", "failed", "not_evaluated"]
    gate_reason: str | None


@dataclass(frozen=True, slots=True)
class CodeSupplyChainCategoryDelta:
    category: str
    baseline: int
    current: int
    delta: int


@dataclass(frozen=True, slots=True)
class CodeSupplyChainProviderDelta:
    provider_id: str
    baseline_status: str
    current_status: str
    baseline_freshness: str
    current_freshness: str
    findings_delta: int
    metrics_delta: int
    relations_delta: int


@dataclass(frozen=True, slots=True)
class CodeSupplyChainGateDelta:
    gate: str
    provider_id: str
    baseline_status: str
    current_status: str
    baseline_reason: str
    current_reason: str
    evidence_count_delta: int


@dataclass(frozen=True, slots=True)
class CodeSupplyChainObservationChange:
    change: Literal["added", "resolved", "changed"]
    observation_id: str
    provider_id: str
    category: str
    evidence_kind: str
    code: str
    path: str | None
    subject_key: str | None
    target_key: str | None
    baseline_value: float | None
    current_value: float | None


@dataclass(frozen=True, slots=True)
class CodeSupplyChainDelta:
    status: Literal["ready", "not_evaluated"]
    reason: str | None
    baseline_status: str
    current_status: str
    categories: tuple[CodeSupplyChainCategoryDelta, ...]
    providers: tuple[CodeSupplyChainProviderDelta, ...]
    gates: tuple[CodeSupplyChainGateDelta, ...]
    common_visible: int
    added_visible: int | None
    resolved_visible: int | None
    changed_visible: int | None
    baseline_observations_truncated: bool
    current_observations_truncated: bool
    examples: tuple[CodeSupplyChainObservationChange, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False


@dataclass(frozen=True, slots=True)
class CodePublicationDiffDigest:
    xxh3_128: str
    xxh3_64_guard: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class CodePublicationDiffResult:
    baseline_database: str
    current_database: str
    status: DiffStatus
    reason: str | None
    baseline: CodePublicationSnapshot | None
    current: CodePublicationSnapshot | None
    calls: CodeCallResolutionDelta | None
    hotspots: CodeHotspotDelta | None
    probable_dead_delta: int | None
    external_evidence: CodeExternalEvidenceDelta | None
    analysis_profile: str | None
    providers: tuple[CodeProviderEvidenceDelta, ...]
    architecture: CodeArchitectureDelta | None
    test_coverage: CoverageComparison | None
    verdict: (
        Literal[
            "improved",
            "regressed",
            "mixed",
            "equivalent_under_observed_metrics",
            "incomparable",
        ]
        | None
    )
    limitations: tuple[str, ...]
    digest: CodePublicationDiffDigest | None
    unused_analysis: CodeUnusedAnalysisDelta | None = None
    supply_chain: CodeSupplyChainDelta | None = None
    engineering_analytics: CodeEngineeringAnalyticsDelta | None = None

    def as_payload(self) -> dict[str, object]:
        payload = asdict(self)
        if self.test_coverage is not None:
            payload["test_coverage"] = {
                "schema": CODE_COVERAGE_SCHEMA,
                **asdict(self.test_coverage),
            }
        return {
            "kind": "code-publication-diff",
            "schema": CODE_PUBLICATION_DIFF_SCHEMA,
            "compatible_schemas": list(CODE_PUBLICATION_DIFF_COMPATIBLE_SCHEMAS),
            **payload,
        }


@dataclass(frozen=True, slots=True)
class _CallSite:
    path: str
    source_symbol: str | None
    name: str
    target_hint: str | None
    line: int
    target: str | None


@dataclass(frozen=True, slots=True)
class _Publication:
    snapshot: CodePublicationSnapshot
    calls: dict[tuple[object, ...], _CallSite]
    hotspots: dict[tuple[str, str], tuple[str, ...]]
    external_evidence: ExternalEvidenceStatus
    external_diagnostic_ids: frozenset[str]
    external_evidence_suite: ExternalEvidenceSuiteStatus
    provider_finding_ids: dict[str, frozenset[str]]
    provider_findings: dict[str, tuple[ExternalProviderFinding, ...]]
    architecture: CodeArchitectureAnalysis
    test_coverage: CodeCoverageAnalysis
    unused_analysis: CodeUnusedAnalysis
    supply_chain: CodeSupplyChainAnalysis
    engineering_analytics: CodeEngineeringAnalytics


def _root_hint(connection: sqlite3.Connection) -> Path:
    project_roots = [
        str(row[0])
        for row in connection.execute(
            "SELECT probable_root FROM projects WHERE status='current' "
            "ORDER BY probable_root COLLATE NOCASE"
        )
        if row[0]
    ]
    file_parents = [
        str(Path(str(row[0])).parent)
        for row in connection.execute(
            "SELECT current_path FROM files WHERE status='current' "
            "ORDER BY current_path COLLATE NOCASE"
        )
    ]
    candidates = project_roots or file_parents
    if not candidates:
        raise ValueError("code publication has no current project or file root")
    try:
        root = Path(os.path.commonpath(candidates))
    except ValueError as exc:
        raise ValueError("code publication spans incompatible filesystem roots") from exc
    if not root.is_absolute():
        raise ValueError("code publication root is not absolute")
    return root


def _relative_path(path: object, root: Path) -> str:
    absolute = Path(str(path))
    if not absolute.is_absolute():
        raise ValueError(f"code publication path is not absolute: {absolute}")
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"code publication path escapes root: {absolute}") from exc
    value = relative.as_posix()
    if not value or value == ".":
        raise ValueError("code publication path resolves to its root")
    return value


def _target_identity(row: sqlite3.Row, root: Path) -> str | None:
    if row["target_symbol_id"] is None:
        return None
    if row["target_path"] is None or row["target_qualified_name"] is None:
        raise ValueError("resolved call target lacks current file or symbol evidence")
    path = _relative_path(row["target_path"], root)
    return f"{path}::{row['target_qualified_name']}::{row['target_kind']}"


def _read_calls(
    connection: sqlite3.Connection,
    root: Path,
    total_calls: int,
) -> dict[tuple[object, ...], _CallSite]:
    if total_calls > CODE_PUBLICATION_DIFF_MAX_CALLS:
        raise ValueError(
            f"code publication has {total_calls} calls; maximum is "
            f"{CODE_PUBLICATION_DIFF_MAX_CALLS}"
        )
    rows = connection.execute(
        """SELECT f.current_path,
        source.qualified_name AS source_qualified_name,
        r.target_symbol_id,r.name,r.target_hint,
        r.start_line,r.start_column,r.end_line,r.end_column,
        r.start_byte,r.end_byte,
        target.qualified_name AS target_qualified_name,
        target.kind AS target_kind,target_file.current_path AS target_path
        FROM code_references r
        JOIN file_versions v ON v.version_id=r.version_id
        JOIN files f ON f.current_version_id=v.version_id AND f.status='current'
        LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
        LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id
        LEFT JOIN file_versions target_version
          ON target_version.version_id=target.version_id
        LEFT JOIN files target_file
          ON target_file.file_id=target_version.file_id
         AND target_file.current_version_id=target_version.version_id
         AND target_file.status='current'
        WHERE v.invalidated_ns IS NULL AND v.language='python'
          AND r.kind='call' AND r.confirmed=1
        ORDER BY f.current_path COLLATE NOCASE,r.start_byte,r.end_byte,
                 r.name,r.reference_id"""
    ).fetchall()
    if len(rows) != total_calls:
        raise ValueError("code call count changed during immutable publication read")
    calls: dict[tuple[object, ...], _CallSite] = {}
    for row in rows:
        relative = _relative_path(row["current_path"], root)
        source_symbol = (
            None if row["source_qualified_name"] is None else str(row["source_qualified_name"])
        )
        target_hint = None if row["target_hint"] is None else str(row["target_hint"])
        key = (
            relative.casefold(),
            int(row["start_byte"]),
            int(row["end_byte"]),
            str(row["name"]),
        )
        if key in calls:
            raise ValueError(f"duplicate stable call identity in publication: {relative}")
        calls[key] = _CallSite(
            relative,
            source_symbol,
            str(row["name"]),
            target_hint,
            int(row["start_line"]),
            _target_identity(row, root),
        )
    return calls


def _diagnostic_signal(row: sqlite3.Row) -> str:
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        raise ValueError("hotspot diagnostic metadata is not an object")
    value = metadata.get("value")
    threshold = metadata.get("threshold")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("hotspot diagnostic value is not an integer")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError("hotspot diagnostic threshold is not positive")
    return f"{row['code']}:{value}/{threshold}"


def _read_hotspots(
    connection: sqlite3.Connection,
    root: Path,
    total_diagnostics: int,
) -> tuple[dict[tuple[str, str], tuple[str, ...]], int, int]:
    if total_diagnostics > CODE_PUBLICATION_DIFF_MAX_HOTSPOTS * 2:
        raise ValueError(
            f"code publication has {total_diagnostics} hotspot diagnostics; maximum is "
            f"{CODE_PUBLICATION_DIFF_MAX_HOTSPOTS * 2}"
        )
    rows = connection.execute(
        """SELECT f.current_path,s.qualified_name,d.code,d.metadata_json
        FROM diagnostics d
        JOIN file_versions v ON v.version_id=d.version_id
        JOIN files f ON f.current_version_id=v.version_id AND f.status='current'
        JOIN symbols s ON s.version_id=d.version_id
         AND s.start_byte=d.start_byte AND s.end_byte=d.end_byte
        WHERE v.invalidated_ns IS NULL AND v.language='python'
          AND d.confirmed=1 AND d.code IN ('high_complexity','long_function')
        ORDER BY f.current_path COLLATE NOCASE,s.qualified_name,d.code"""
    ).fetchall()
    if len(rows) != total_diagnostics:
        raise ValueError("hotspot count changed during immutable publication read")
    grouped: dict[tuple[str, str], set[str]] = {}
    high_complexity = 0
    long_function = 0
    for row in rows:
        relative = _relative_path(row["current_path"], root)
        key = (relative, str(row["qualified_name"]))
        signal = _diagnostic_signal(row)
        if signal in grouped.setdefault(key, set()):
            raise ValueError(f"duplicate hotspot signal in publication: {relative}")
        grouped[key].add(signal)
        high_complexity += row["code"] == "high_complexity"
        long_function += row["code"] == "long_function"
    if len(grouped) > CODE_PUBLICATION_DIFF_MAX_HOTSPOTS:
        raise ValueError(
            f"code publication has {len(grouped)} hotspots; maximum is "
            f"{CODE_PUBLICATION_DIFF_MAX_HOTSPOTS}"
        )
    return (
        {key: tuple(sorted(signals)) for key, signals in grouped.items()},
        high_complexity,
        long_function,
    )


def _read_publication(state_directory: Path) -> _Publication:
    state_directory = Path(state_directory)
    database = state_directory / "code.sqlite3"
    require_sqlite_sidecars_absent(database)
    if not database.is_file():
        raise FileNotFoundError(database)
    with readonly_code_database(database) as connection:
        validate_code_schema(connection)
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != CODE_SCHEMA_VERSION:
            raise RuntimeError(f"code state schema {schema_version} is unsupported for diff")
        latest = connection.execute(
            """SELECT analysis_run_id,processing_signature,status FROM analysis_runs
            ORDER BY analysis_run_id DESC LIMIT 1"""
        ).fetchone()
        if latest is None:
            raise ValueError("code publication has no analysis run")
        if str(latest["status"]) != "completed":
            raise ValueError(f"latest code run is not completed: {latest['status']}")
        root = _root_hint(connection)
        python_files, complete_python_files = connection.execute(
            """SELECT COUNT(*),SUM(CASE WHEN v.analysis_status='complete' THEN 1
            ELSE 0 END) FROM file_versions v
            JOIN files f ON f.current_version_id=v.version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
              AND v.language='python' AND v.generated=0 AND v.vendored=0"""
        ).fetchone()
        call_edges, resolved_call_edges = connection.execute(
            """SELECT COUNT(*),COUNT(r.target_symbol_id)
            FROM code_references r
            JOIN file_versions v ON v.version_id=r.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
              AND v.language='python' AND r.kind='call' AND r.confirmed=1"""
        ).fetchone()
        total_hotspot_diagnostics = int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d
                JOIN file_versions v ON v.version_id=d.version_id
                JOIN files f ON f.current_version_id=v.version_id
                WHERE f.status='current' AND v.invalidated_ns IS NULL
                  AND v.language='python' AND d.confirmed=1
                  AND d.code IN ('high_complexity','long_function')"""
            ).fetchone()[0]
        )
        probable_dead = int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d
                JOIN file_versions v ON v.version_id=d.version_id
                JOIN files f ON f.current_version_id=v.version_id
                WHERE f.status='current' AND v.invalidated_ns IS NULL
                  AND v.language='python' AND d.code='probable_dead_symbol'"""
            ).fetchone()[0]
        )
        calls = _read_calls(connection, root, int(call_edges))
        hotspots, high_complexity, long_function = _read_hotspots(
            connection,
            root,
            total_hotspot_diagnostics,
        )
        external_evidence, external_ids, _external_row = read_external_evidence(
            connection,
            int(latest["analysis_run_id"]),
            enforce_current_runtime=False,
        )
        external_suite = read_external_evidence_suite(
            connection,
            int(latest["analysis_run_id"]),
            enforce_current_runtime=False,
        )
        provider_ids = read_external_provider_finding_ids(
            connection,
            int(latest["analysis_run_id"]),
        )
        provider_findings = read_external_provider_findings(
            connection,
            int(latest["analysis_run_id"]),
            provider_ids=_RELOCATION_AWARE_PROVIDER_IDS,
        )
        architecture = read_code_architecture_analysis(
            connection,
            int(latest["analysis_run_id"]),
            database=str(database),
        )
        test_coverage = read_code_coverage_analysis(
            connection,
            int(latest["analysis_run_id"]),
            database=str(database),
        )
        unused_analysis = read_code_unused_analysis(
            connection,
            int(latest["analysis_run_id"]),
            database=str(database),
        )
        supply_chain = read_code_supply_chain_analysis(
            connection,
            int(latest["analysis_run_id"]),
            database=str(database),
        )
        engineering_analytics = read_code_engineering_analysis(
            connection,
            int(latest["analysis_run_id"]),
            database=str(database),
        )
    snapshot = CodePublicationSnapshot(
        state_directory=str(state_directory),
        database=str(database),
        root=str(root),
        processing_signature=str(latest["processing_signature"]),
        python_files=int(python_files),
        complete_python_files=int(complete_python_files or 0),
        call_edges=int(call_edges),
        resolved_call_edges=int(resolved_call_edges),
        hotspots=len(hotspots),
        high_complexity=high_complexity,
        long_function=long_function,
        probable_dead=probable_dead,
    )
    return _Publication(
        snapshot,
        calls,
        hotspots,
        external_evidence,
        external_ids,
        external_suite,
        provider_ids,
        provider_findings,
        architecture,
        test_coverage,
        unused_analysis,
        supply_chain,
        engineering_analytics,
    )


def _call_delta(
    baseline: _Publication,
    current: _Publication,
) -> CodeCallResolutionDelta:
    baseline_keys = set(baseline.calls)
    current_keys = set(current.calls)
    common_keys = sorted(baseline_keys & current_keys)
    unchanged_resolved = 0
    still_unresolved = 0
    newly_resolved = 0
    corrected = 0
    lost = 0
    examples: list[CodeCallResolutionChange] = []
    for key in common_keys:
        before = baseline.calls[key]
        after = current.calls[key]
        change: Literal["newly_resolved", "corrected", "lost"] | None = None
        if before.target is None and after.target is None:
            still_unresolved += 1
        elif before.target is None:
            newly_resolved += 1
            change = "newly_resolved"
        elif after.target is None:
            lost += 1
            change = "lost"
        elif before.target == after.target:
            unchanged_resolved += 1
        else:
            corrected += 1
            change = "corrected"
        if change is not None and len(examples) < CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT:
            examples.append(
                CodeCallResolutionChange(
                    change,
                    after.path,
                    after.source_symbol,
                    after.name,
                    after.target_hint,
                    after.line,
                    before.target,
                    after.target,
                )
            )
    return CodeCallResolutionDelta(
        common_call_sites=len(common_keys),
        baseline_only_call_sites=len(baseline_keys - current_keys),
        current_only_call_sites=len(current_keys - baseline_keys),
        unchanged_resolved=unchanged_resolved,
        still_unresolved=still_unresolved,
        newly_resolved=newly_resolved,
        corrected=corrected,
        lost=lost,
        examples=tuple(examples),
    )


def _hotspot_label(key: tuple[str, str]) -> str:
    return f"{key[0]}::{key[1]}"


def _hotspot_delta(baseline: _Publication, current: _Publication) -> CodeHotspotDelta:
    baseline_keys = set(baseline.hotspots)
    current_keys = set(current.hotspots)
    common_keys = baseline_keys & current_keys
    added = sorted(current_keys - baseline_keys)
    removed = sorted(baseline_keys - current_keys)
    changed = sorted(key for key in common_keys if baseline.hotspots[key] != current.hotspots[key])
    limit = CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT
    return CodeHotspotDelta(
        common=len(common_keys),
        added=len(added),
        removed=len(removed),
        changed_evidence=len(changed),
        added_examples=tuple(_hotspot_label(key) for key in added[:limit]),
        removed_examples=tuple(_hotspot_label(key) for key in removed[:limit]),
        changed_examples=tuple(_hotspot_label(key) for key in changed[:limit]),
    )


def _external_delta(
    baseline: _Publication,
    current: _Publication,
) -> CodeExternalEvidenceDelta:
    before = baseline.external_evidence
    after = current.external_evidence
    comparable = (
        before.status == "ready"
        and after.status == "ready"
        and before.tool_version == after.tool_version
        and before.configuration_signature == after.configuration_signature
    )
    if not comparable:
        reasons = []
        if before.status != "ready":
            reasons.append(f"baseline_{before.status}:{before.reason}")
        if after.status != "ready":
            reasons.append(f"current_{after.status}:{after.reason}")
        if (
            before.status == "ready"
            and after.status == "ready"
            and before.tool_version != after.tool_version
        ):
            reasons.append("tool_version_changed")
        if (
            before.status == "ready"
            and after.status == "ready"
            and before.configuration_signature != after.configuration_signature
        ):
            reasons.append("configuration_changed")
        return CodeExternalEvidenceDelta(
            "not_evaluated",
            ";".join(reasons) or "external_evidence_not_comparable",
            before,
            after,
            0,
            None,
            None,
            (),
            (),
            "not_evaluated",
        )
    common = baseline.external_diagnostic_ids & current.external_diagnostic_ids
    added = sorted(current.external_diagnostic_ids - baseline.external_diagnostic_ids)
    resolved = sorted(baseline.external_diagnostic_ids - current.external_diagnostic_ids)
    return CodeExternalEvidenceDelta(
        "ready",
        None,
        before,
        after,
        len(common),
        len(added),
        len(resolved),
        tuple(added[:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT]),
        tuple(resolved[:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT]),
        "passed" if not added else "failed",
    )


def _legacy_ruff_contract_compatible(
    provider_id: str,
    left: ExternalProviderStatus | None,
    right: ExternalProviderStatus | None,
) -> bool:
    return (
        left is not None
        and right is not None
        and provider_id == "ruff-protected-basic"
        and left.profile == right.profile == "protected"
        and left.provider_schema == right.provider_schema == "neocortex.ruff-protected-basic/v1"
        and left.tool_name == right.tool_name == "ruff"
        and left.tool_version == right.tool_version
        and (
            (
                left.comparability_signature == RUFF_CONFIGURATION_SIGNATURE
                and right.comparability_signature is not None
                and right.comparability_signature.startswith("ruff-protected-basic-comparable-v1:")
            )
            or (
                right.comparability_signature == RUFF_CONFIGURATION_SIGNATURE
                and left.comparability_signature is not None
                and left.comparability_signature.startswith("ruff-protected-basic-comparable-v1:")
            )
        )
    )


def _finding_location(finding: ExternalProviderFinding) -> tuple[int, int, int, int]:
    return (
        finding.start_line,
        finding.start_column,
        finding.end_line,
        finding.end_column,
    )


def _finding_semantic_key(provider_id: str, finding: ExternalProviderFinding) -> str:
    return canonical_json(
        {
            "path": finding.relative_path,
            "category": finding.category,
            "code": finding.code,
            "severity": finding.severity,
            "message": normalize_external_finding_message(provider_id, finding.message),
            "observation_confirmed": finding.observation_confirmed,
            "tool_confidence": finding.tool_confidence,
            "calibrated_confidence": finding.calibrated_confidence,
            "gate_authority": finding.gate_authority,
            "url": finding.url,
            "fix_available": finding.fix_available,
            "metadata": dict(finding.metadata),
            "mutation_authority": finding.mutation_authority,
        }
    )


def _typed_finding_relocations(
    provider_id: str,
    baseline_findings: tuple[ExternalProviderFinding, ...],
    current_findings: tuple[ExternalProviderFinding, ...],
    baseline_ids: frozenset[str],
    current_ids: frozenset[str],
) -> tuple[
    tuple[CodeProviderFindingRelocation, ...],
    frozenset[str],
    frozenset[str],
]:
    if {item.portable_finding_id for item in baseline_findings} != baseline_ids or {
        item.portable_finding_id for item in current_findings
    } != current_ids:
        return (), frozenset(), frozenset()
    baseline_groups: dict[str, list[ExternalProviderFinding]] = {}
    current_groups: dict[str, list[ExternalProviderFinding]] = {}
    for finding in baseline_findings:
        if finding.portable_finding_id not in current_ids:
            baseline_groups.setdefault(_finding_semantic_key(provider_id, finding), []).append(
                finding
            )
    for finding in current_findings:
        if finding.portable_finding_id not in baseline_ids:
            current_groups.setdefault(_finding_semantic_key(provider_id, finding), []).append(
                finding
            )
    relocations: list[CodeProviderFindingRelocation] = []
    relocated_baseline_ids: set[str] = set()
    relocated_current_ids: set[str] = set()
    for key in sorted(set(baseline_groups) & set(current_groups)):
        before_group = sorted(
            baseline_groups[key],
            key=lambda item: (*_finding_location(item), item.portable_finding_id),
        )
        after_group = sorted(
            current_groups[key],
            key=lambda item: (*_finding_location(item), item.portable_finding_id),
        )
        for before, after in zip(before_group, after_group, strict=False):
            if _finding_location(before) == _finding_location(after):
                continue
            relocated_baseline_ids.add(before.portable_finding_id)
            relocated_current_ids.add(after.portable_finding_id)
            if len(relocations) < CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT:
                relocations.append(
                    CodeProviderFindingRelocation(
                        before.portable_finding_id,
                        after.portable_finding_id,
                        before.relative_path,
                        before.category,
                        before.code,
                        before.severity,
                        normalize_external_finding_message(provider_id, before.message),
                        before.start_line,
                        before.start_column,
                        before.end_line,
                        before.end_column,
                        after.start_line,
                        after.start_column,
                        after.end_line,
                        after.end_column,
                    )
                )
    return (
        tuple(relocations),
        frozenset(relocated_baseline_ids),
        frozenset(relocated_current_ids),
    )


def _provider_deltas(
    baseline: _Publication,
    current: _Publication,
) -> tuple[CodeProviderEvidenceDelta, ...]:
    before = {item.provider_id: item for item in baseline.external_evidence_suite.providers}
    after = {item.provider_id: item for item in current.external_evidence_suite.providers}
    deltas: list[CodeProviderEvidenceDelta] = []
    for provider_id in sorted(set(before) | set(after)):
        left = before.get(provider_id)
        right = after.get(provider_id)
        legacy_ruff_compatible = _legacy_ruff_contract_compatible(provider_id, left, right)
        comparable = (
            left is not None
            and right is not None
            and left.status == "ready"
            and right.status == "ready"
            and left.profile == right.profile
            and left.provider_schema == right.provider_schema
            and left.tool_version == right.tool_version
            and (
                left.comparability_signature == right.comparability_signature
                or legacy_ruff_compatible
            )
        )
        if not comparable:
            reasons: list[str] = []
            if left is None:
                reasons.append("baseline_provider_missing")
            elif left.status != "ready":
                reasons.append(f"baseline_{left.status}:{left.reason}")
            if right is None:
                reasons.append("current_provider_missing")
            elif right.status != "ready":
                reasons.append(f"current_{right.status}:{right.reason}")
            if left is not None and right is not None:
                if left.profile != right.profile:
                    reasons.append("profile_changed")
                if left.provider_schema != right.provider_schema:
                    reasons.append("provider_schema_changed")
                if left.tool_version != right.tool_version:
                    reasons.append("tool_version_changed")
                if (
                    left.comparability_signature != right.comparability_signature
                    and not legacy_ruff_compatible
                ):
                    reasons.append("comparability_signature_changed")
            deltas.append(
                CodeProviderEvidenceDelta(
                    provider_id,
                    "not_evaluated",
                    ";".join(reasons) or "provider_not_comparable",
                    left,
                    right,
                    0,
                    None,
                    None,
                    "not_evaluated",
                )
            )
            continue
        assert left is not None and right is not None
        baseline_ids = baseline.provider_finding_ids.get(provider_id)
        if baseline_ids is None:
            baseline_ids = (
                baseline.external_diagnostic_ids
                if left.comparability_signature == RUFF_CONFIGURATION_SIGNATURE
                else frozenset()
            )
        current_ids = current.provider_finding_ids.get(provider_id)
        if current_ids is None:
            current_ids = (
                current.external_diagnostic_ids
                if right.comparability_signature == RUFF_CONFIGURATION_SIGNATURE
                else frozenset()
            )
        relocation_examples: tuple[CodeProviderFindingRelocation, ...] = ()
        relocated_baseline_ids: frozenset[str] = frozenset()
        relocated_current_ids: frozenset[str] = frozenset()
        relocated: int | None = None
        if provider_id in _RELOCATION_AWARE_PROVIDER_IDS:
            relocation_examples, relocated_baseline_ids, relocated_current_ids = (
                _typed_finding_relocations(
                    provider_id,
                    baseline.provider_findings.get(provider_id, ()),
                    current.provider_findings.get(provider_id, ()),
                    baseline_ids,
                    current_ids,
                )
            )
            relocated = len(relocated_baseline_ids)
        added = current_ids - baseline_ids - relocated_current_ids
        resolved = baseline_ids - current_ids - relocated_baseline_ids
        deltas.append(
            CodeProviderEvidenceDelta(
                provider_id,
                "ready",
                None,
                left,
                right,
                len(baseline_ids & current_ids),
                len(added),
                len(resolved),
                "passed" if not added else "failed",
                relocated,
                relocation_examples,
            )
        )
    return tuple(deltas)


def _provider_verdict(
    deltas: tuple[CodeProviderEvidenceDelta, ...],
) -> Literal[
    "improved",
    "regressed",
    "mixed",
    "equivalent_under_observed_metrics",
    "incomparable",
]:
    comparable = tuple(item for item in deltas if item.status == "ready")
    if not comparable:
        return "incomparable"
    added = sum(item.added or 0 for item in comparable)
    resolved = sum(item.resolved or 0 for item in comparable)
    if added and resolved:
        return "mixed"
    if added:
        return "regressed"
    if resolved:
        return "improved"
    return "equivalent_under_observed_metrics"


def _architecture_not_evaluated(reason: str) -> CodeArchitectureDelta:
    return CodeArchitectureDelta(
        "not_evaluated",
        reason,
        (),
        (),
        (),
        (),
        (),
        (),
        "not_evaluated",
        "not_evaluated",
        "not_evaluated",
    )


def _architecture_comparability_reason(
    baseline: CodeArchitectureAnalysis,
    current: CodeArchitectureAnalysis,
) -> str | None:
    reasons: list[str] = []
    if baseline.status != "ready":
        reasons.append(f"baseline_{baseline.status}:{baseline.reason}")
    if current.status != "ready":
        reasons.append(f"current_{current.status}:{current.reason}")
    before = {item.provider_id: item for item in baseline.providers}
    after = {item.provider_id: item for item in current.providers}
    for provider_id in sorted(set(before) | set(after)):
        left = before.get(provider_id)
        right = after.get(provider_id)
        if left is None:
            reasons.append(f"baseline_provider_missing:{provider_id}")
            continue
        if right is None:
            reasons.append(f"current_provider_missing:{provider_id}")
            continue
        if left.status != "ready" or right.status != "ready":
            reasons.append(f"provider_not_ready:{provider_id}")
            continue
        if left.provider_schema != right.provider_schema:
            reasons.append(f"provider_schema_changed:{provider_id}")
        if left.tool_version != right.tool_version:
            reasons.append(f"tool_version_changed:{provider_id}")
        if left.comparability_signature != right.comparability_signature:
            reasons.append(f"comparability_signature_changed:{provider_id}")
    return ";".join(dict.fromkeys(reasons)) or None


def _module_map(
    analysis: CodeArchitectureAnalysis,
) -> dict[str, ArchitectureModule]:
    if len(analysis.modules) > CODE_PUBLICATION_DIFF_MAX_MODULES:
        raise ValueError(
            f"architecture has {len(analysis.modules)} modules; maximum is "
            f"{CODE_PUBLICATION_DIFF_MAX_MODULES}"
        )
    return {item.module_id: item for item in analysis.modules}


def _complexity_displacement(
    baseline: CodeArchitectureAnalysis,
    current: CodeArchitectureAnalysis,
    modules: tuple[CodeModuleArchitectureDelta, ...],
) -> tuple[CodeComplexityDisplacement, ...]:
    decreases = {
        item.module_id: -(item.cognitive_complexity_delta or 0.0)
        for item in modules
        if item.cognitive_complexity_delta is not None and item.cognitive_complexity_delta < 0.0
    }
    increases = {
        item.module_id: item.cognitive_complexity_delta or 0.0
        for item in modules
        if item.cognitive_complexity_delta is not None and item.cognitive_complexity_delta > 0.0
    }
    adjacent, relationships = _architecture_adjacency(baseline, current)
    displaced: list[CodeComplexityDisplacement] = []
    for target in sorted(decreases):
        recipients = tuple(
            module for module in sorted(adjacent.get(target, set())) if module in increases
        )
        if not recipients:
            continue
        evidence = tuple(
            sorted(
                item
                for recipient in recipients
                for item in relationships.get(_module_pair(target, recipient), set())
            )
        )
        displaced.append(
            CodeComplexityDisplacement(
                target,
                decreases[target],
                recipients,
                sum(increases[module] for module in recipients),
                evidence,
            )
        )
    return tuple(displaced)


def _module_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _architecture_adjacency(
    baseline: CodeArchitectureAnalysis,
    current: CodeArchitectureAnalysis,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    adjacent: dict[str, set[str]] = {}
    relationships: dict[tuple[str, str], set[str]] = {}
    for publication, analysis in (("baseline", baseline), ("current", current)):
        for edge in analysis.imports:
            adjacent.setdefault(edge.source_module, set()).add(edge.target_module)
            adjacent.setdefault(edge.target_module, set()).add(edge.source_module)
            relationships.setdefault(
                _module_pair(edge.source_module, edge.target_module), set()
            ).add(f"{publication}:{edge.source_module}->{edge.target_module}:{edge.comparison}")
    return adjacent, relationships


def _architecture_module_deltas(
    before: dict[str, ArchitectureModule],
    after: dict[str, ArchitectureModule],
) -> tuple[CodeModuleArchitectureDelta, ...]:
    modules: list[CodeModuleArchitectureDelta] = []
    for module_id in sorted(set(before) | set(after)):
        left = before.get(module_id)
        right = after.get(module_id)
        baseline_complexity = None if left is None else left.cognitive_complexity_total
        current_complexity = None if right is None else right.cognitive_complexity_total
        complexity_delta = (
            None
            if baseline_complexity is None or current_complexity is None
            else current_complexity - baseline_complexity
        )
        dependency_reason: str | None = None
        blast_reason: str | None = None
        if left is None or right is None:
            dependency_reason = blast_reason = "module_not_present_in_both_publications"
        else:
            if left.dependency_reach_truncated or right.dependency_reach_truncated:
                dependency_reason = "dependency_reach_truncated_lower_bound"
            if left.blast_radius_truncated or right.blast_radius_truncated:
                blast_reason = "blast_radius_truncated_lower_bound"
        dependency_comparable = dependency_reason is None
        blast_comparable = blast_reason is None
        graph_reason = (
            ";".join(reason for reason in (dependency_reason, blast_reason) if reason is not None)
            or None
        )
        graph_comparable = dependency_comparable and blast_comparable
        modules.append(
            CodeModuleArchitectureDelta(
                module_id=module_id,
                baseline_cognitive_complexity=baseline_complexity,
                current_cognitive_complexity=current_complexity,
                cognitive_complexity_delta=complexity_delta,
                fan_in_delta=(0 if right is None else right.fan_in)
                - (0 if left is None else left.fan_in),
                fan_out_delta=(0 if right is None else right.fan_out)
                - (0 if left is None else left.fan_out),
                baseline_cycle_ids=() if left is None else left.cycle_ids,
                current_cycle_ids=() if right is None else right.cycle_ids,
                baseline_contract_ids=() if left is None else left.contract_ids,
                current_contract_ids=() if right is None else right.contract_ids,
                baseline_dependency_reach=None if left is None else left.dependency_reach,
                current_dependency_reach=None if right is None else right.dependency_reach,
                dependency_reach_delta=(
                    right.dependency_reach - left.dependency_reach
                    if dependency_comparable and left is not None and right is not None
                    else None
                ),
                baseline_dependency_reach_truncated=(
                    False if left is None else left.dependency_reach_truncated
                ),
                current_dependency_reach_truncated=(
                    False if right is None else right.dependency_reach_truncated
                ),
                dependency_reach_status=(
                    "comparable" if dependency_comparable else "not_comparable"
                ),
                dependency_reach_reason=dependency_reason,
                baseline_blast_radius=None if left is None else left.blast_radius,
                current_blast_radius=None if right is None else right.blast_radius,
                blast_radius_delta=(
                    right.blast_radius - left.blast_radius
                    if blast_comparable and left is not None and right is not None
                    else None
                ),
                baseline_blast_radius_truncated=(
                    False if left is None else left.blast_radius_truncated
                ),
                current_blast_radius_truncated=(
                    False if right is None else right.blast_radius_truncated
                ),
                blast_radius_status=("comparable" if blast_comparable else "not_comparable"),
                blast_radius_reason=blast_reason,
                baseline_directed_degree_centrality=(
                    None if left is None else left.directed_degree_centrality
                ),
                current_directed_degree_centrality=(
                    None if right is None else right.directed_degree_centrality
                ),
                directed_degree_centrality_delta=(
                    right.directed_degree_centrality - left.directed_degree_centrality
                    if left is not None and right is not None
                    else None
                ),
                baseline_cross_path_namespace_fan_in=(
                    None if left is None else left.cross_path_namespace_fan_in
                ),
                current_cross_path_namespace_fan_in=(
                    None if right is None else right.cross_path_namespace_fan_in
                ),
                cross_path_namespace_fan_in_delta=(
                    right.cross_path_namespace_fan_in - left.cross_path_namespace_fan_in
                    if left is not None and right is not None
                    else None
                ),
                baseline_cross_path_namespace_fan_out=(
                    None if left is None else left.cross_path_namespace_fan_out
                ),
                current_cross_path_namespace_fan_out=(
                    None if right is None else right.cross_path_namespace_fan_out
                ),
                cross_path_namespace_fan_out_delta=(
                    right.cross_path_namespace_fan_out - left.cross_path_namespace_fan_out
                    if left is not None and right is not None
                    else None
                ),
                graph_metrics_status=("comparable" if graph_comparable else "not_comparable"),
                graph_metrics_reason=graph_reason,
            )
        )
    return tuple(modules)


def _architecture_delta(
    baseline: CodeArchitectureAnalysis,
    current: CodeArchitectureAnalysis,
) -> CodeArchitectureDelta:
    reason = _architecture_comparability_reason(baseline, current)
    if reason is not None:
        return _architecture_not_evaluated(reason)
    try:
        before = _module_map(baseline)
        after = _module_map(current)
    except ValueError as exc:
        return _architecture_not_evaluated(str(exc))
    frozen_modules = _architecture_module_deltas(before, after)
    baseline_failed = {item.contract_id for item in baseline.contracts if item.status == "failed"}
    current_failed = {item.contract_id for item in current.contracts if item.status == "failed"}
    baseline_cycles = {item.modules for item in baseline.cycles}
    current_cycles = {item.modules for item in current.cycles}
    added_contracts = tuple(sorted(current_failed - baseline_failed))
    resolved_contracts = tuple(sorted(baseline_failed - current_failed))
    added_cycles = tuple(sorted(current_cycles - baseline_cycles))
    resolved_cycles = tuple(sorted(baseline_cycles - current_cycles))
    displaced = _complexity_displacement(baseline, current, frozen_modules)
    return CodeArchitectureDelta(
        "ready",
        None,
        frozen_modules,
        added_contracts,
        resolved_contracts,
        added_cycles,
        resolved_cycles,
        displaced,
        "passed" if not added_contracts else "failed",
        "passed" if not added_cycles else "failed",
        "passed" if not displaced else "failed",
    )


def _engineering_dimension_state(
    analysis: CodeEngineeringAnalytics,
    dimension: Literal["complexity", "coverage", "mutation", "history", "graph"],
) -> tuple[str, str | None]:
    values = tuple(getattr(module, dimension) for module in analysis.modules)
    if not values:
        return "abstained", analysis.reason or "engineering_modules_not_recorded"
    statuses = {item.status for item in values}
    if len(statuses) != 1:
        return "partial", "module_dimension_states_mixed"
    status = next(iter(statuses))
    reasons = tuple(sorted({item.reason for item in values if item.reason is not None}))
    reason = None if status == "ready" else (";".join(reasons) or f"{dimension}_not_ready")
    return status, reason


def _engineering_source_gate(
    analysis: CodeEngineeringAnalytics,
    gate: str,
) -> tuple[str, str | None]:
    evidence = next((item for item in analysis.gates if item.gate == gate), None)
    if evidence is None:
        return "not_evaluated", "engineering_gate_not_recorded"
    return evidence.status, evidence.reason


def _engineering_not_comparable_reason(
    baseline: CodeEngineeringAnalytics,
    current: CodeEngineeringAnalytics,
    measurement_gate: CodeEngineeringGateDelta,
) -> str | None:
    if baseline.status != "ready" or current.status != "ready":
        return (
            f"engineering_analytics_not_ready:baseline={baseline.status}:current={current.status}"
        )
    if measurement_gate.status != "passed":
        return measurement_gate.reason or "mutation_measurement_incomplete"
    if baseline.mutation_scope_signature is None or current.mutation_scope_signature is None:
        return "mutation_measurement_scope_not_recorded"
    if baseline.mutation_scope_signature != current.mutation_scope_signature:
        return "mutation_measurement_scope_changed"
    if baseline.mutation_score is None or current.mutation_score is None:
        return "mutation_score_not_recorded"
    return None


def _engineering_delta(
    baseline: CodeEngineeringAnalytics,
    current: CodeEngineeringAnalytics,
) -> CodeEngineeringAnalyticsDelta:
    dimensions = []
    dimension_names: tuple[
        Literal["complexity", "coverage", "mutation", "history", "graph"], ...
    ] = ("complexity", "coverage", "mutation", "history", "graph")
    for dimension in dimension_names:
        baseline_status, baseline_reason = _engineering_dimension_state(baseline, dimension)
        current_status, current_reason = _engineering_dimension_state(current, dimension)
        dimensions.append(
            CodeEngineeringDimensionState(
                dimension=dimension,
                baseline_status=baseline_status,
                baseline_reason=baseline_reason,
                current_status=current_status,
                current_reason=current_reason,
            )
        )
    before_gate, before_reason = _engineering_source_gate(baseline, "mutation_measurement_complete")
    after_gate, after_reason = _engineering_source_gate(current, "mutation_measurement_complete")
    if "failed" in {before_gate, after_gate}:
        measurement_gate = CodeEngineeringGateDelta(
            "mutation_measurement_complete",
            "failed",
            before_reason or after_reason or "mutation_measurement_incomplete",
        )
    elif before_gate == after_gate == "passed":
        measurement_gate = CodeEngineeringGateDelta("mutation_measurement_complete", "passed", None)
    else:
        measurement_gate = CodeEngineeringGateDelta(
            "mutation_measurement_complete",
            "not_evaluated",
            before_reason or after_reason or "mutation_measurement_not_comparable",
        )
    reason = _engineering_not_comparable_reason(baseline, current, measurement_gate)
    if reason is not None:
        score_gate = CodeEngineeringGateDelta(
            "mutation_score_not_degraded", "not_evaluated", reason
        )
        return CodeEngineeringAnalyticsDelta(
            status="not_comparable",
            reason=reason,
            baseline_status=baseline.status,
            baseline_reason=baseline.reason,
            current_status=current.status,
            current_reason=current.reason,
            dimensions=tuple(dimensions),
            baseline_mutation_scope_signature=baseline.mutation_scope_signature,
            current_mutation_scope_signature=current.mutation_scope_signature,
            baseline_mutation_score=None,
            current_mutation_score=None,
            mutation_score_delta=None,
            gates=(measurement_gate, score_gate),
        )
    assert baseline.mutation_score is not None
    assert current.mutation_score is not None
    score_delta = current.mutation_score - baseline.mutation_score
    score_gate = CodeEngineeringGateDelta(
        "mutation_score_not_degraded",
        "passed" if score_delta >= 0.0 else "failed",
        None if score_delta >= 0.0 else "mutation_score_decreased",
    )
    return CodeEngineeringAnalyticsDelta(
        status="comparable",
        reason=None,
        baseline_status=baseline.status,
        baseline_reason=baseline.reason,
        current_status=current.status,
        current_reason=current.reason,
        dimensions=tuple(dimensions),
        baseline_mutation_scope_signature=baseline.mutation_scope_signature,
        current_mutation_scope_signature=current.mutation_scope_signature,
        baseline_mutation_score=baseline.mutation_score,
        current_mutation_score=current.mutation_score,
        mutation_score_delta=score_delta,
        gates=(measurement_gate, score_gate),
    )


def _unused_not_evaluated(
    baseline: CodeUnusedAnalysis,
    current: CodeUnusedAnalysis,
    reason: str,
) -> CodeUnusedAnalysisDelta:
    return CodeUnusedAnalysisDelta(
        status="not_evaluated",
        reason=reason,
        baseline_provider_signature=baseline.provider_signature,
        current_provider_signature=current.provider_signature,
        baseline_calibration_signature=baseline.calibration_signature,
        current_calibration_signature=current.calibration_signature,
        common=0,
        added=None,
        removed=None,
        state_changes=None,
        high_consensus_added=None,
        high_consensus_resolved=None,
        added_examples=(),
        removed_examples=(),
        state_change_examples=(),
        gate="not_evaluated",
        gate_reason=reason,
    )


def _unused_comparability_reason(
    baseline: CodeUnusedAnalysis,
    current: CodeUnusedAnalysis,
) -> str | None:
    if baseline.status != "ready":
        return "baseline_unused_analysis_not_ready:" + (baseline.reason or baseline.status)
    if current.status != "ready":
        return "current_unused_analysis_not_ready:" + (current.reason or current.status)
    signatures = (
        ("provider_signature", baseline.provider_signature, current.provider_signature),
        ("calibration_signature", baseline.calibration_signature, current.calibration_signature),
        ("policy_signature", baseline.policy_signature, current.policy_signature),
    )
    for name, baseline_value, current_value in signatures:
        if not baseline_value or not current_value:
            return f"unused_{name}_missing"
        if baseline_value != current_value:
            return f"unused_{name}_mismatch"
    return None


def _unused_acceptance_gate(
    baseline: CodeUnusedAnalysis,
    current: CodeUnusedAnalysis,
    *,
    high_consensus_added: int,
) -> tuple[Literal["passed", "failed", "not_evaluated"], str | None]:
    observed = {
        f"{label}:{gate.gate}": gate.status
        for label, analysis in (("baseline", baseline), ("current", current))
        for gate in analysis.gates
        if gate.gate in _UNUSED_REQUIRED_PRECISION_GATES
    }
    required = tuple(
        f"{label}:{gate}"
        for label in ("baseline", "current")
        for gate in sorted(_UNUSED_REQUIRED_PRECISION_GATES)
    )
    failed = tuple(item for item in required if observed.get(item) == "failed")
    if failed:
        return "failed", "unused_precision_gate_failed:" + ",".join(failed)
    unavailable = tuple(item for item in required if observed.get(item) != "passed")
    if unavailable:
        return "not_evaluated", "unused_precision_gate_not_evaluated:" + ",".join(unavailable)
    if high_consensus_added:
        return "failed", "new_probable_unused_high_consensus_candidates"
    return "passed", None


def _unused_delta(
    baseline: CodeUnusedAnalysis,
    current: CodeUnusedAnalysis,
) -> CodeUnusedAnalysisDelta:
    reason = _unused_comparability_reason(baseline, current)
    if reason is not None:
        return _unused_not_evaluated(baseline, current, reason)
    baseline_by_id = {item.candidate_id: item for item in baseline.candidates}
    current_by_id = {item.candidate_id: item for item in current.candidates}
    baseline_ids = set(baseline_by_id)
    current_ids = set(current_by_id)
    common_ids = sorted(baseline_ids & current_ids)
    added_ids = sorted(current_ids - baseline_ids)
    removed_ids = sorted(baseline_ids - current_ids)
    changes = tuple(
        CodeUnusedStateChange(
            candidate_id=candidate_id,
            relative_path=current_by_id[candidate_id].relative_path,
            symbol=current_by_id[candidate_id].symbol,
            baseline_state=baseline_by_id[candidate_id].state,
            current_state=current_by_id[candidate_id].state,
        )
        for candidate_id in common_ids
        if baseline_by_id[candidate_id].state != current_by_id[candidate_id].state
    )
    high_state = "probable_unused_high_consensus"
    high_added = sum(
        current_by_id[candidate_id].state == high_state
        and (candidate_id not in baseline_by_id or baseline_by_id[candidate_id].state != high_state)
        for candidate_id in current_ids
    )
    high_resolved = sum(
        baseline_by_id[candidate_id].state == high_state
        and (candidate_id not in current_by_id or current_by_id[candidate_id].state != high_state)
        for candidate_id in baseline_ids
    )
    added_examples = tuple(
        CodeUnusedCandidateExample(
            candidate_id=item.candidate_id,
            relative_path=item.relative_path,
            symbol=item.symbol,
            state=item.state,
        )
        for item in (current_by_id[candidate_id] for candidate_id in added_ids)
    )[:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT]
    removed_examples = tuple(
        CodeUnusedCandidateExample(
            candidate_id=item.candidate_id,
            relative_path=item.relative_path,
            symbol=item.symbol,
            state=item.state,
        )
        for item in (baseline_by_id[candidate_id] for candidate_id in removed_ids)
    )[:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT]
    gate, gate_reason = _unused_acceptance_gate(
        baseline,
        current,
        high_consensus_added=high_added,
    )
    return CodeUnusedAnalysisDelta(
        status="ready",
        reason=None,
        baseline_provider_signature=baseline.provider_signature,
        current_provider_signature=current.provider_signature,
        baseline_calibration_signature=baseline.calibration_signature,
        current_calibration_signature=current.calibration_signature,
        common=len(common_ids),
        added=len(added_ids),
        removed=len(removed_ids),
        state_changes=len(changes),
        high_consensus_added=high_added,
        high_consensus_resolved=high_resolved,
        added_examples=added_examples,
        removed_examples=removed_examples,
        state_change_examples=changes[:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT],
        gate=gate,
        gate_reason=gate_reason,
    )


_SUPPLY_CHAIN_CATEGORIES = (
    "project_invariant",
    "dependency_hygiene",
    "known_vulnerability",
    "package_integrity",
    "license_inventory",
)


def _supply_chain_change(
    change: Literal["added", "resolved", "changed"],
    observation_id: str,
    baseline: SupplyChainObservation | None,
    current: SupplyChainObservation | None,
) -> CodeSupplyChainObservationChange:
    selected = current or baseline
    if selected is None:
        raise AssertionError("supply-chain observation change requires evidence")
    return CodeSupplyChainObservationChange(
        change=change,
        observation_id=observation_id,
        provider_id=selected.provider_id,
        category=selected.category,
        evidence_kind=selected.evidence_kind,
        code=selected.code,
        path=selected.path,
        subject_key=selected.subject_key,
        target_key=selected.target_key,
        baseline_value=None if baseline is None else baseline.value,
        current_value=None if current is None else current.value,
    )


def _supply_chain_delta(
    baseline: CodeSupplyChainAnalysis,
    current: CodeSupplyChainAnalysis,
) -> CodeSupplyChainDelta:
    category_deltas = tuple(
        CodeSupplyChainCategoryDelta(
            category,
            int(getattr(baseline.counts, category)),
            int(getattr(current.counts, category)),
            int(getattr(current.counts, category)) - int(getattr(baseline.counts, category)),
        )
        for category in _SUPPLY_CHAIN_CATEGORIES
    )
    baseline_providers = {item.provider_id: item for item in baseline.providers}
    current_providers = {item.provider_id: item for item in current.providers}
    provider_deltas = []
    for provider_id in CODE_SUPPLY_CHAIN_REQUIRED_PROVIDERS:
        before = baseline_providers.get(provider_id)
        after = current_providers.get(provider_id)
        provider_deltas.append(
            CodeSupplyChainProviderDelta(
                provider_id=provider_id,
                baseline_status="not_recorded" if before is None else before.status,
                current_status="not_recorded" if after is None else after.status,
                baseline_freshness="unknown" if before is None else before.freshness,
                current_freshness="unknown" if after is None else after.freshness,
                findings_delta=(0 if after is None else after.findings)
                - (0 if before is None else before.findings),
                metrics_delta=(0 if after is None else after.metrics)
                - (0 if before is None else before.metrics),
                relations_delta=(0 if after is None else after.relations)
                - (0 if before is None else before.relations),
            )
        )
    baseline_gates = {item.gate: item for item in baseline.gates}
    current_gates = {item.gate: item for item in current.gates}
    gate_deltas = []
    for gate in sorted(set(baseline_gates) | set(current_gates)):
        gate_before = baseline_gates.get(gate)
        gate_after = current_gates.get(gate)
        selected = gate_after or gate_before
        if selected is None:
            continue
        gate_deltas.append(
            CodeSupplyChainGateDelta(
                gate=gate,
                provider_id=selected.provider_id,
                baseline_status=("not_evaluated" if gate_before is None else gate_before.status),
                current_status="not_evaluated" if gate_after is None else gate_after.status,
                baseline_reason=(
                    "gate_not_recorded" if gate_before is None else gate_before.reason
                ),
                current_reason="gate_not_recorded" if gate_after is None else gate_after.reason,
                evidence_count_delta=(0 if gate_after is None else gate_after.evidence_count)
                - (0 if gate_before is None else gate_before.evidence_count),
            )
        )
    before_observations = {item.observation_id: item for item in baseline.observations}
    after_observations = {item.observation_id: item for item in current.observations}
    common_ids = set(before_observations) & set(after_observations)
    changed_ids = sorted(
        observation_id
        for observation_id in common_ids
        if asdict(before_observations[observation_id]) != asdict(after_observations[observation_id])
    )
    added_ids = sorted(set(after_observations) - set(before_observations))
    resolved_ids = sorted(set(before_observations) - set(after_observations))
    examples = tuple(
        [
            *(
                _supply_chain_change(
                    "changed",
                    observation_id,
                    before_observations[observation_id],
                    after_observations[observation_id],
                )
                for observation_id in changed_ids
            ),
            *(
                _supply_chain_change(
                    "added", observation_id, None, after_observations[observation_id]
                )
                for observation_id in added_ids
            ),
            *(
                _supply_chain_change(
                    "resolved", observation_id, before_observations[observation_id], None
                )
                for observation_id in resolved_ids
            ),
        ][:CODE_PUBLICATION_DIFF_EXAMPLE_LIMIT]
    )
    status: Literal["ready", "not_evaluated"] = (
        "ready" if baseline.status == "ready" or current.status == "ready" else "not_evaluated"
    )
    reason = None
    if baseline.status != current.status or status == "not_evaluated":
        reason = f"supply_chain_availability:baseline={baseline.status}:current={current.status}"
    return CodeSupplyChainDelta(
        status=status,
        reason=reason,
        baseline_status=baseline.status,
        current_status=current.status,
        categories=category_deltas,
        providers=tuple(provider_deltas),
        gates=tuple(gate_deltas),
        common_visible=len(common_ids) - len(changed_ids),
        added_visible=len(added_ids),
        resolved_visible=len(resolved_ids),
        changed_visible=len(changed_ids),
        baseline_observations_truncated=baseline.counts.observations_truncated,
        current_observations_truncated=current.counts.observations_truncated,
        examples=examples,
    )


def _digest_payload(
    baseline: CodePublicationSnapshot,
    current: CodePublicationSnapshot,
    calls: CodeCallResolutionDelta,
    hotspots: CodeHotspotDelta,
    probable_dead_delta: int,
    external_evidence: CodeExternalEvidenceDelta,
    providers: tuple[CodeProviderEvidenceDelta, ...],
    architecture: CodeArchitectureDelta,
    test_coverage: CoverageComparison,
    unused_analysis: CodeUnusedAnalysisDelta,
    supply_chain: CodeSupplyChainDelta,
    engineering_analytics: CodeEngineeringAnalyticsDelta,
    verdict: str,
    limitations: tuple[str, ...],
) -> CodePublicationDiffDigest:
    def snapshot(value: CodePublicationSnapshot) -> dict[str, object]:
        payload = asdict(value)
        for key in ("state_directory", "database", "root"):
            payload.pop(key)
        return payload

    serialized = canonical_json(
        {
            "schema": CODE_PUBLICATION_DIFF_SCHEMA,
            "baseline": snapshot(baseline),
            "current": snapshot(current),
            "calls": asdict(calls),
            "hotspots": asdict(hotspots),
            "probable_dead_delta": probable_dead_delta,
            "external_evidence": {
                "status": external_evidence.status,
                "reason": external_evidence.reason,
                "baseline": external_status_digest_payload(external_evidence.baseline),
                "current": external_status_digest_payload(external_evidence.current),
                "common": external_evidence.common,
                "added": external_evidence.added,
                "resolved": external_evidence.resolved,
                "added_examples": list(external_evidence.added_examples),
                "resolved_examples": list(external_evidence.resolved_examples),
                "gate": external_evidence.gate,
            },
            "providers": [asdict(item) for item in providers],
            "architecture": asdict(architecture),
            "test_coverage": asdict(test_coverage),
            "unused_analysis": asdict(unused_analysis),
            "supply_chain": asdict(supply_chain),
            "engineering_analytics": asdict(engineering_analytics),
            "verdict": verdict,
            "limitations": list(limitations),
        }
    )
    fingerprint = fingerprint_text(serialized)
    return CodePublicationDiffDigest(
        fingerprint.xxh3_128,
        fingerprint.xxh3_64_guard,
        fingerprint.byte_count,
    )


def _abstained(
    baseline_state: Path,
    current_state: Path,
    reason: str,
) -> CodePublicationDiffResult:
    return CodePublicationDiffResult(
        baseline_database=str(Path(baseline_state) / "code.sqlite3"),
        current_database=str(Path(current_state) / "code.sqlite3"),
        status="abstained",
        reason=reason,
        baseline=None,
        current=None,
        calls=None,
        hotspots=None,
        probable_dead_delta=None,
        external_evidence=None,
        analysis_profile=None,
        providers=(),
        architecture=None,
        test_coverage=None,
        verdict=None,
        limitations=(),
        digest=None,
        unused_analysis=None,
        supply_chain=None,
        engineering_analytics=None,
    )


def compare_code_publications(
    baseline_state: Path,
    current_state: Path,
) -> CodePublicationDiffResult:
    """Compare two immutable completed publications without writing either owner."""

    baseline_state = Path(baseline_state)
    current_state = Path(current_state)
    try:
        baseline = _read_publication(baseline_state)
    except (FileNotFoundError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _abstained(
            baseline_state,
            current_state,
            f"baseline_unavailable:{type(exc).__name__}:{exc}",
        )
    try:
        current = _read_publication(current_state)
    except (FileNotFoundError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _abstained(
            baseline_state,
            current_state,
            f"current_unavailable:{type(exc).__name__}:{exc}",
        )
    calls = _call_delta(baseline, current)
    hotspots = _hotspot_delta(baseline, current)
    probable_dead_delta = current.snapshot.probable_dead - baseline.snapshot.probable_dead
    external_evidence = _external_delta(baseline, current)
    providers = _provider_deltas(baseline, current)
    architecture = _architecture_delta(baseline.architecture, current.architecture)
    test_coverage = compare_code_coverage(
        baseline.test_coverage,
        current.test_coverage,
    )
    unused_analysis = _unused_delta(
        baseline.unused_analysis,
        current.unused_analysis,
    )
    supply_chain = _supply_chain_delta(
        baseline.supply_chain,
        current.supply_chain,
    )
    engineering_analytics = _engineering_delta(
        baseline.engineering_analytics,
        current.engineering_analytics,
    )
    verdict = _provider_verdict(providers)
    limitations = [
        "common_calls_require_matching_source_path_byte_range_and_name",
        "dynamic_dispatch_is_not_observed",
        "probable_dead_is_count_only_uncalibrated_evidence",
        "diff_is_observational_and_never_authorizes_code_or_corpus_mutation",
        "provider_verdict_excludes_architecture_test_coverage_unused_supply_chain_and_engineering_gates",
        "typed_relocation_requires_identical_path_category_code_severity_message_and_metadata",
        "unused_analysis_is_advisory_and_never_authorizes_deletion_or_mutation",
        "supply_chain_delta_is_multidimensional_and_has_no_aggregate_score",
        "supply_chain_evidence_is_advisory_and_never_authorizes_mutation",
        "engineering_dimensions_are_not_aggregated",
        "engineering_dimensions_and_mutation_score_are_not_defect_probability",
    ]
    if any(item.status != "ready" for item in providers):
        limitations.append("provider_verdict_uses_only_comparable_providers")
    if architecture.status != "ready":
        limitations.append("architecture_delta_not_comparable")
    if test_coverage.status != "comparable":
        limitations.append("test_coverage_delta_not_comparable")
    if unused_analysis.status != "ready":
        limitations.append("unused_analysis_delta_not_comparable")
    if supply_chain.status != "ready":
        limitations.append("supply_chain_delta_not_comparable")
    if engineering_analytics.status != "comparable":
        limitations.append("engineering_analytics_delta_not_comparable")
    if supply_chain.baseline_observations_truncated or supply_chain.current_observations_truncated:
        limitations.append("supply_chain_observation_deltas_are_visible_examples_only")
    if any(
        _legacy_ruff_contract_compatible(item.provider_id, item.baseline, item.current)
        for item in providers
    ):
        limitations.append(_LEGACY_RUFF_COMPARABILITY_REASON)
    frozen_limitations = tuple(limitations)
    digest = _digest_payload(
        baseline.snapshot,
        current.snapshot,
        calls,
        hotspots,
        probable_dead_delta,
        external_evidence,
        providers,
        architecture,
        test_coverage,
        unused_analysis,
        supply_chain,
        engineering_analytics,
        verdict,
        frozen_limitations,
    )
    return CodePublicationDiffResult(
        baseline_database=baseline.snapshot.database,
        current_database=current.snapshot.database,
        status="ready",
        reason=None,
        baseline=baseline.snapshot,
        current=current.snapshot,
        calls=calls,
        hotspots=hotspots,
        probable_dead_delta=probable_dead_delta,
        external_evidence=external_evidence,
        analysis_profile=current.external_evidence_suite.profile,
        providers=providers,
        architecture=architecture,
        test_coverage=test_coverage,
        verdict=verdict,
        limitations=frozen_limitations,
        digest=digest,
        unused_analysis=unused_analysis,
        supply_chain=supply_chain,
        engineering_analytics=engineering_analytics,
    )


__all__ = [
    "CODE_PUBLICATION_DIFF_COMPATIBLE_SCHEMAS",
    "CODE_PUBLICATION_DIFF_SCHEMA",
    "CodeArchitectureDelta",
    "CodeCallResolutionChange",
    "CodeCallResolutionDelta",
    "CodeComplexityDisplacement",
    "CodeEngineeringAnalyticsDelta",
    "CodeEngineeringDimensionState",
    "CodeEngineeringGateDelta",
    "CodeExternalEvidenceDelta",
    "CodeHotspotDelta",
    "CodeModuleArchitectureDelta",
    "CodeProviderEvidenceDelta",
    "CodeProviderFindingRelocation",
    "CodePublicationDiffDigest",
    "CodePublicationDiffResult",
    "CodePublicationSnapshot",
    "CodeSupplyChainCategoryDelta",
    "CodeSupplyChainDelta",
    "CodeSupplyChainGateDelta",
    "CodeSupplyChainObservationChange",
    "CodeSupplyChainProviderDelta",
    "CodeUnusedAnalysisDelta",
    "CodeUnusedCandidateExample",
    "CodeUnusedStateChange",
    "compare_code_publications",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_publication_diff")
