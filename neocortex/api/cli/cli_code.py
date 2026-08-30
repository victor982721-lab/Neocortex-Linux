"""Bounded direct CLI operations for structured code intelligence."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from neocortex.code.code_coverage_analysis import CODE_COVERAGE_SCHEMA

if TYPE_CHECKING:
    from neocortex.code.code_architecture_analysis import CodeArchitectureAnalysis
    from neocortex.code.code_coverage_analysis import CodeCoverageAnalysis, CoverageComparison
    from neocortex.code.code_engineering_analytics import CodeEngineeringAnalytics
    from neocortex.code.code_publication_diff import (
        CodeArchitectureDelta,
        CodeCallResolutionDelta,
        CodeEngineeringAnalyticsDelta,
        CodeExternalEvidenceDelta,
        CodeHotspotDelta,
        CodeModuleArchitectureDelta,
        CodePublicationDiffDigest,
        CodePublicationDiffResult,
        CodePublicationSnapshot,
        CodeSupplyChainDelta,
        CodeUnusedAnalysisDelta,
    )
    from neocortex.code.code_review_models import (
        CodeReviewCoverage,
        CodeReviewDigest,
        CodeReviewResult,
        CodeReviewSnapshot,
        CodeReviewWorkPackage,
    )


_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT = 20
_CODE_CLI_COVERAGE_EXAMPLE_LIMIT = 20
_CODE_CLI_UNUSED_EXAMPLE_LIMIT = 20
_CODE_CLI_ENGINEERING_MODULE_LIMIT = 20
_CODE_ARCHITECTURE_PROVIDER_IDS = (
    "complexipy-cognitive",
    "grimp-architecture",
    "ruff-analyze-imports",
)
_CODE_ARCHITECTURE_ACCEPTANCE_GATES = frozenset(
    {
        "architecture_contracts_not_degraded",
        "module_complexity_not_displaced",
        "no_new_import_cycles",
    }
)


def _state_path(args: argparse.Namespace) -> Path:
    return args.state_directory / "code.sqlite3"


def _console_text(value: str, stream: object) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        return value
    return value


def _print_console_line(value: str, *, file: TextIO | None = None) -> None:
    stream = sys.stdout if file is None else file
    print(_console_text(value, stream), file=stream)


def _emit(value: object, *, json_output: bool) -> None:
    if json_output:
        _print_console_line(json.dumps(value, ensure_ascii=True, sort_keys=True))
    else:
        _print_console_line(str(value))


def _error(operation: str, exc: BaseException) -> int:
    _print_console_line(
        f"ERROR {operation} {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return 2


# region [01] Status and diagnostics


@dataclass(frozen=True, slots=True)
class _CodeStatusSnapshot:
    schema_version: int
    counts: dict[str, int]
    latest_run: sqlite3.Row | None
    external_evidence: dict[str, object]
    external_evidence_suite: dict[str, object]
    architecture: dict[str, object]
    test_coverage: dict[str, object]
    unused_analysis: dict[str, object] = field(default_factory=dict)
    supply_chain: dict[str, object] = field(default_factory=dict)
    engineering_analytics: dict[str, object] = field(default_factory=dict)


def _architecture_abstained_payload(
    database: str,
    reason: str,
    *,
    analysis_run_id: int | None = None,
) -> dict[str, object]:
    return {
        "schema": "neocortex.code-architecture-analysis/v3",
        "status": "abstained",
        "reason": reason,
        "gate": "abstained",
        "analysis_run_id": analysis_run_id,
        "providers": [
            {
                "provider_id": provider_id,
                "status": "abstained",
                "reason": reason,
                "tool_name": None,
                "tool_version": None,
                "execution": None,
                "provider_gate": None,
                "metrics": 0,
                "relations": 0,
            }
            for provider_id in _CODE_ARCHITECTURE_PROVIDER_IDS
        ],
        "summary": None,
        "counts": {
            "modules": 0,
            "symbols": 0,
            "imports": 0,
            "cycles": 0,
            "contracts": 0,
            "failed_contracts": 0,
        },
        "gates": [
            {"gate": gate, "status": "not_evaluated", "reason": reason}
            for gate in (
                "import_graph_consensus",
                "architecture_contracts",
                "module_complexity_displacement",
            )
        ],
        "database": database,
    }


def _architecture_status_payload(
    analysis: CodeArchitectureAnalysis,
) -> dict[str, object]:
    failed_contracts = sum(item.status == "failed" for item in analysis.contracts)
    return {
        "schema": "neocortex.code-architecture-analysis/v3",
        "status": analysis.status,
        "reason": analysis.reason,
        "gate": analysis.gate,
        "analysis_run_id": analysis.analysis_run_id,
        "providers": [
            {
                "provider_id": item.provider_id,
                "status": item.status,
                "reason": item.reason,
                "tool_name": item.tool_name,
                "tool_version": item.tool_version,
                "execution": item.execution,
                "provider_gate": item.provider_gate,
                "metrics": item.metrics,
                "relations": item.relations,
            }
            for item in analysis.providers
        ],
        "summary": None if analysis.summary is None else asdict(analysis.summary),
        "counts": {
            "modules": len(analysis.modules),
            "symbols": len(analysis.symbols),
            "imports": len(analysis.imports),
            "cycles": len(analysis.cycles),
            "contracts": len(analysis.contracts),
            "failed_contracts": failed_contracts,
        },
        "gates": [asdict(item) for item in analysis.gates],
        "database": analysis.database,
    }


def _coverage_status_payload(
    analysis: CodeCoverageAnalysis,
) -> dict[str, object]:
    """Project bounded status facts without dumping every measured symbol/test edge."""

    return {
        "kind": "code-coverage-analysis",
        "schema": CODE_COVERAGE_SCHEMA,
        "database": analysis.database,
        "analysis_run_id": analysis.analysis_run_id,
        "provider_id": analysis.provider_id,
        "tool_run_id": analysis.tool_run_id,
        "effective_tool_run_id": analysis.effective_tool_run_id,
        "status": analysis.status,
        "reason": analysis.reason,
        "suite_selection": analysis.suite_selection,
        "measurement_complete": analysis.measurement_complete,
        "content_executed": analysis.content_executed,
        "tool_versions": [asdict(item) for item in analysis.tool_versions],
        "suite_signature": analysis.suite_signature,
        "configuration_signature": analysis.configuration_signature,
        "measurement_scope_signature": analysis.measurement_scope_signature,
        "outcomes": None if analysis.outcomes is None else asdict(analysis.outcomes),
        "totals": None if analysis.totals is None else asdict(analysis.totals),
        "counts": {
            "modules": len(analysis.modules),
            "symbols": len(analysis.symbols),
            "test_relations": len(analysis.test_relations),
            "failed_tests": len(analysis.failed_test_nodeids),
        },
        "failed_test_examples": list(
            analysis.failed_test_nodeids[:_CODE_CLI_COVERAGE_EXAMPLE_LIMIT]
        ),
        "failed_test_examples_truncated": (
            len(analysis.failed_test_nodeids) > _CODE_CLI_COVERAGE_EXAMPLE_LIMIT
        ),
        "gates": [asdict(item) for item in analysis.gates],
        "limitations": list(analysis.limitations),
    }


def _coverage_abstained_payload(
    database: str,
    reason: str,
    *,
    analysis_run_id: int | None = None,
) -> dict[str, object]:
    return {
        "kind": "code-coverage-analysis",
        "schema": CODE_COVERAGE_SCHEMA,
        "database": database,
        "analysis_run_id": analysis_run_id,
        "provider_id": "pytest-coverage-trusted-deep",
        "tool_run_id": None,
        "effective_tool_run_id": None,
        "status": "abstained",
        "reason": reason,
        "suite_selection": None,
        "measurement_complete": None,
        "content_executed": None,
        "tool_versions": [],
        "suite_signature": None,
        "configuration_signature": None,
        "measurement_scope_signature": None,
        "outcomes": None,
        "totals": None,
        "counts": {"modules": 0, "symbols": 0, "test_relations": 0, "failed_tests": 0},
        "failed_test_examples": [],
        "failed_test_examples_truncated": False,
        "gates": [
            {"gate": gate, "status": "not_evaluated", "reason": reason}
            for gate in ("tests_passed", "coverage_available")
        ],
        "limitations": ["trusted_deep_evidence_not_ready"],
    }


def _engineering_abstained_payload(
    database: str,
    reason: str,
    *,
    analysis_run_id: int | None = None,
) -> dict[str, object]:
    return {
        "kind": "code-engineering-analytics",
        "schema": "neocortex.code-engineering-analytics/v2",
        "database": database,
        "analysis_run_id": analysis_run_id,
        "status": "abstained",
        "reason": reason,
        "providers": [],
        "modules": [],
        "counts": {"modules": 0, "module_examples": 0},
        "modules_truncated": False,
        "gates": [
            {"gate": gate, "status": "not_evaluated", "reason": reason}
            for gate in (
                "mutation_test_baseline",
                "mutation_measurement_complete",
                "mutation_score_recorded",
            )
        ],
        "mutation_scope_signature": None,
        "mutation_score": None,
        "limitations": [
            "engineering_dimensions_are_not_aggregated",
            "no_dimension_is_a_defect_probability",
        ],
        "digest": None,
        "authority": "advisory",
        "mutation_authority": False,
        "aggregate_score": None,
        "defect_probability": None,
    }


def _engineering_status_payload(
    analysis: CodeEngineeringAnalytics,
) -> dict[str, object]:
    modules = analysis.modules[:_CODE_CLI_ENGINEERING_MODULE_LIMIT]
    return {
        "kind": "code-engineering-analytics",
        "schema": "neocortex.code-engineering-analytics/v2",
        "database": analysis.database,
        "analysis_run_id": analysis.analysis_run_id,
        "status": analysis.status,
        "reason": analysis.reason,
        "providers": [asdict(item) for item in analysis.providers],
        "modules": [asdict(item) for item in modules],
        "counts": {"modules": len(analysis.modules), "module_examples": len(modules)},
        "modules_truncated": len(analysis.modules) > len(modules),
        "gates": [asdict(item) for item in analysis.gates],
        "mutation_scope_signature": analysis.mutation_scope_signature,
        "mutation_score": analysis.mutation_score,
        "limitations": list(analysis.limitations),
        "digest": analysis.digest,
        "authority": analysis.authority,
        "mutation_authority": analysis.mutation_authority,
        "aggregate_score": None,
        "defect_probability": None,
    }


def _unused_abstained_payload(
    database: str,
    reason: str,
    *,
    analysis_run_id: int | None = None,
) -> dict[str, object]:
    return {
        "kind": "code-unused-analysis",
        "schema": "neocortex.code-unused-analysis/v1",
        "database": database,
        "analysis_run_id": analysis_run_id,
        "status": "abstained",
        "reason": reason,
        "counts": {
            "total": 0,
            "explained_usage": 0,
            "dynamic_usage_possible": 0,
            "insufficient_evidence": 0,
            "probable_unused_high_consensus": 0,
        },
        "providers": [],
        "candidates": [],
        "calibration": None,
        "holdout": None,
        "gates": [],
        "limitations": ["unused_evidence_not_ready"],
        "authority": "advisory",
        "mutation_authority": False,
    }


def _supply_chain_abstained_payload(
    database: str,
    reason: str,
    *,
    analysis_run_id: int | None = None,
) -> dict[str, object]:
    gates = (
        ("semgrep_invariants", "semgrep-neocortex-invariants"),
        ("dependency_declaration_integrity", "deptry-project-dependencies"),
        ("vulnerability_snapshot_current", "pip-audit-known-vulnerabilities"),
        ("no_known_vulnerabilities", "pip-audit-known-vulnerabilities"),
        ("installed_package_integrity", "installed-package-inventory"),
        ("license_inventory_available", "installed-package-inventory"),
    )
    return {
        "kind": "code-supply-chain-analysis",
        "schema": "neocortex.code-supply-chain-analysis/v1",
        "database": database,
        "analysis_run_id": analysis_run_id,
        "status": "abstained",
        "reason": reason,
        "providers": [],
        "observations": [],
        "counts": {
            "findings": 0,
            "metrics": 0,
            "relations": 0,
            "project_invariant": 0,
            "dependency_hygiene": 0,
            "known_vulnerability": 0,
            "package_integrity": 0,
            "license_inventory": 0,
            "duplicate_ids": 0,
            "observations": 0,
            "observations_truncated": False,
        },
        "gates": [
            {
                "gate": gate,
                "provider_id": provider,
                "status": "not_evaluated",
                "reason": reason,
                "evidence_count": 0,
            }
            for gate, provider in gates
        ],
        "limitations": ["supply_chain_provider_evidence_was_not_interpreted"],
        "digest": None,
        "authority": "advisory",
        "mutation_authority": False,
    }


def _code_status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    active_embedding_links = int(
        connection.execute("SELECT COUNT(*) FROM embedding_links WHERE active=1").fetchone()[0]
    )
    current_embedding_links = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_links e
            JOIN code_chunks c ON c.chunk_id=e.chunk_id
            JOIN file_versions v ON v.version_id=c.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE e.active=1 AND f.status='current'
            AND v.invalidated_ns IS NULL"""
        ).fetchone()[0]
    )
    return {
        "current_files": int(
            connection.execute("SELECT COUNT(*) FROM files WHERE status='current'").fetchone()[0]
        ),
        "versions": int(connection.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]),
        "current_symbols": int(
            connection.execute(
                """SELECT COUNT(*) FROM symbols s JOIN file_versions v
                ON v.version_id=s.version_id WHERE v.invalidated_ns IS NULL"""
            ).fetchone()[0]
        ),
        "current_references": int(
            connection.execute(
                """SELECT COUNT(*) FROM code_references r JOIN file_versions v
                ON v.version_id=r.version_id WHERE v.invalidated_ns IS NULL"""
            ).fetchone()[0]
        ),
        "current_diagnostics": int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d JOIN file_versions v
                ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL"""
            ).fetchone()[0]
        ),
        "current_external_diagnostics": int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d JOIN file_versions v
                ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL
                AND d.source='external:ruff'"""
            ).fetchone()[0]
        ),
        "current_provider_diagnostics": int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d JOIN file_versions v
                ON v.version_id=d.version_id WHERE v.invalidated_ns IS NULL
                AND d.source LIKE 'external:%' AND d.source<>'external:ruff'"""
            ).fetchone()[0]
        ),
        "projects": int(
            connection.execute(
                "SELECT COUNT(*) FROM projects WHERE status<>'historical'"
            ).fetchone()[0]
        ),
        "active_embedding_links": active_embedding_links,
        "current_embedding_links": current_embedding_links,
        "stale_embedding_links": active_embedding_links - current_embedding_links,
    }


def _latest_code_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT analysis_run_id,framework_run_id,scan_id,
        CASE WHEN length(CAST(processing_signature AS BLOB))
            BETWEEN 1 AND 4096 THEN processing_signature END
            AS processing_signature,
        CASE WHEN length(CAST(status AS BLOB)) BETWEEN 1 AND 32
            THEN status END AS status,
        started_ns,completed_ns,candidates,
        processed,cache_hits,errors
        FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"""
    ).fetchone()


def _read_code_status_snapshot(path: Path) -> _CodeStatusSnapshot:
    from neocortex.code.code_architecture_analysis import read_code_architecture_analysis
    from neocortex.code.code_coverage_analysis import read_code_coverage_analysis
    from neocortex.code.code_engineering_analytics import read_code_engineering_analysis
    from neocortex.code.code_external_evidence import read_external_evidence
    from neocortex.code.code_review_models import bounded_code_unused_payload
    from neocortex.code.code_unused_analysis import read_code_unused_analysis
    from neocortex.code.code_schema import CODE_SCHEMA_VERSION, validate_code_schema
    from neocortex.code.code_supply_chain_analysis import read_code_supply_chain_analysis
    from neocortex.code.external_evidence_store import read_external_evidence_suite
    from neocortex.workflow.self_analysis.self_analysis_status import quiescent_sqlite_database

    with quiescent_sqlite_database(path) as connection:
        validate_code_schema(connection)
        counts = _code_status_counts(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != CODE_SCHEMA_VERSION:
            raise RuntimeError(f"code state schema {version} is unsupported for status")
        latest = _latest_code_run(connection)
        external_evidence = (
            read_external_evidence(
                connection,
                int(latest["analysis_run_id"]),
                enforce_current_runtime=True,
            )[0].as_payload()
            if latest is not None
            else read_external_evidence(
                connection,
                -1,
                enforce_current_runtime=True,
            )[0].as_payload()
        )
        suite = read_external_evidence_suite(
            connection,
            -1 if latest is None else int(latest["analysis_run_id"]),
            enforce_current_runtime=True,
        ).as_payload()
        architecture_analysis: CodeArchitectureAnalysis | None = None
        if latest is None:
            architecture = _architecture_abstained_payload(
                str(path),
                "code_run_missing",
            )
        elif latest["status"] != "completed":
            architecture = _architecture_abstained_payload(
                str(path),
                f"code_run_not_completed:{latest['status']}",
                analysis_run_id=int(latest["analysis_run_id"]),
            )
        else:
            architecture_analysis = read_code_architecture_analysis(
                connection,
                int(latest["analysis_run_id"]),
                database=str(path),
            )
            architecture = _architecture_status_payload(architecture_analysis)
        coverage_analysis: CodeCoverageAnalysis | None = None
        if latest is None:
            test_coverage = _coverage_abstained_payload(
                str(path),
                "code_run_missing",
            )
        elif latest["status"] != "completed":
            test_coverage = _coverage_abstained_payload(
                str(path),
                f"code_run_not_completed:{latest['status']}",
                analysis_run_id=int(latest["analysis_run_id"]),
            )
        else:
            coverage_analysis = read_code_coverage_analysis(
                connection,
                int(latest["analysis_run_id"]),
                database=str(path),
            )
            test_coverage = _coverage_status_payload(coverage_analysis)
        unused_analysis = bounded_code_unused_payload(
            read_code_unused_analysis(
                connection,
                -1 if latest is None else int(latest["analysis_run_id"]),
                database=str(path),
            )
        )
        if latest is None:
            supply_chain = _supply_chain_abstained_payload(str(path), "code_run_missing")
        elif latest["status"] != "completed":
            supply_chain = _supply_chain_abstained_payload(
                str(path),
                f"code_run_not_completed:{latest['status']}",
                analysis_run_id=int(latest["analysis_run_id"]),
            )
        else:
            supply_chain = read_code_supply_chain_analysis(
                connection,
                int(latest["analysis_run_id"]),
                database=str(path),
            ).as_payload()
        if latest is None:
            engineering_analytics = _engineering_abstained_payload(str(path), "code_run_missing")
        elif latest["status"] != "completed":
            engineering_analytics = _engineering_abstained_payload(
                str(path),
                f"code_run_not_completed:{latest['status']}",
                analysis_run_id=int(latest["analysis_run_id"]),
            )
        else:
            engineering_analytics = _engineering_status_payload(
                read_code_engineering_analysis(
                    connection,
                    int(latest["analysis_run_id"]),
                    database=str(path),
                    architecture=architecture_analysis,
                    coverage=coverage_analysis,
                )
            )
    return _CodeStatusSnapshot(
        version,
        counts,
        latest,
        external_evidence,
        suite,
        architecture,
        test_coverage,
        unused_analysis,
        supply_chain,
        engineering_analytics,
    )


def _read_self_analysis_payload(
    args: argparse.Namespace,
    latest: sqlite3.Row | None,
    *,
    enabled: bool | None = None,
) -> dict[str, object] | None:
    if enabled is None:
        enabled = args.code_json
    if not enabled or latest is None:
        return None
    from neocortex.workflow.self_analysis.self_analysis_status import (
        CodeRunStatusEvidence,
        read_self_analysis_status,
    )

    processing_signature = latest["processing_signature"]
    run_status = latest["status"]
    if not isinstance(processing_signature, str) or not isinstance(run_status, str):
        raise ValueError("latest code run has unbounded status evidence")
    status = read_self_analysis_status(
        args.state_directory,
        CodeRunStatusEvidence(
            analysis_run_id=int(latest["analysis_run_id"]),
            framework_run_id=int(latest["framework_run_id"]),
            scan_id=int(latest["scan_id"]),
            processing_signature=processing_signature,
            status=run_status,
        ),
    )
    return None if status is None else status.as_payload()


def _missing_code_status_payload(path: Path, analyzers: object) -> dict[str, object]:
    architecture = _architecture_abstained_payload(
        str(path),
        "code_state_missing",
    )
    return {
        "kind": "code-status",
        "schema": "neocortex.code-status/v1",
        "database": str(path),
        "exists": False,
        "analyzers": analyzers,
        "self_analysis": None,
        "external_evidence": {
            "status": "not_recorded",
            "reason": "code_state_missing",
            "provider": "ruff",
        },
        "external_evidence_suite": {
            "schema": "neocortex.external-evidence-suite/v1",
            "profile": "protected",
            "status": "not_recorded",
            "providers": [],
            "type_consensus": {"status": "not_comparable"},
            "gates": [],
        },
        "architecture": architecture,
        "test_coverage": _coverage_abstained_payload(str(path), "code_state_missing"),
        "unused_analysis": _unused_abstained_payload(str(path), "code_state_missing"),
        "supply_chain": _supply_chain_abstained_payload(str(path), "code_state_missing"),
        "engineering_analytics": _engineering_abstained_payload(str(path), "code_state_missing"),
    }


def _emit_missing_code_status(
    path: Path,
    analyzers: object,
    *,
    json_output: bool,
) -> None:
    payload = _missing_code_status_payload(path, analyzers)
    architecture = payload["architecture"]
    if not isinstance(architecture, dict):
        raise AssertionError("missing Code status architecture must be a mapping")
    if json_output:
        _emit(payload, json_output=True)
        return
    _emit(f"CODE_STATUS database={path} exists=false", json_output=False)
    _emit_code_status_architecture(architecture)
    _emit_code_coverage(
        "CODE_COVERAGE",
        _coverage_abstained_payload(str(path), "code_state_missing"),
    )
    _emit_code_unused(
        "CODE_UNUSED",
        _unused_abstained_payload(str(path), "code_state_missing"),
    )
    _emit_code_supply_chain(
        "CODE_SUPPLY_CHAIN",
        _supply_chain_abstained_payload(str(path), "code_state_missing"),
    )
    _emit_code_engineering(_engineering_abstained_payload(str(path), "code_state_missing"))


def _emit_code_supply_chain(prefix: str, payload: dict[str, object]) -> None:
    counts = payload.get("counts")
    bounded_counts = counts if isinstance(counts, dict) else {}
    _print_console_line(
        f"{prefix} status={payload.get('status')} "
        f"observations={bounded_counts.get('observations', 0)} "
        f"findings={bounded_counts.get('findings', 0)} "
        f"metrics={bounded_counts.get('metrics', 0)} "
        f"relations={bounded_counts.get('relations', 0)} "
        f"truncated={int(bool(bounded_counts.get('observations_truncated')))} "
        f"reason={json.dumps(payload.get('reason'), ensure_ascii=True)}"
    )
    providers = payload.get("providers")
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            _print_console_line(
                f"{prefix}_PROVIDER id={provider.get('provider_id')} "
                f"status={provider.get('status')} freshness={provider.get('freshness')} "
                f"observed_date={provider.get('observed_date')} "
                f"findings={provider.get('findings', 0)} "
                f"metrics={provider.get('metrics', 0)} "
                f"relations={provider.get('relations', 0)}"
            )
    gates = payload.get("gates")
    if isinstance(gates, list):
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            _print_console_line(
                f"{prefix}_GATE id={gate.get('gate')} status={gate.get('status')} "
                f"provider={gate.get('provider_id')} evidence={gate.get('evidence_count', 0)} "
                f"reason={json.dumps(gate.get('reason'), ensure_ascii=True)}"
            )


def _emit_code_engineering(payload: dict[str, object]) -> None:
    counts = payload.get("counts")
    bounded_counts = counts if isinstance(counts, dict) else {}
    providers = payload.get("providers")
    provider_states = (
        {
            str(item.get("provider_id")): item.get("status")
            for item in providers
            if isinstance(item, dict)
        }
        if isinstance(providers, list)
        else {}
    )
    _print_console_line(
        f"CODE_ENGINEERING status={payload.get('status')} "
        f"modules={bounded_counts.get('modules', 0)} "
        f"history={provider_states.get('git-history-local', 'not_recorded')} "
        f"mutation={provider_states.get('cosmic-ray-focal-mutation', 'not_recorded')} "
        f"mutation_score={payload.get('mutation_score')} "
        f"reason={json.dumps(payload.get('reason'), ensure_ascii=True)}"
    )


def _emit_code_status_architecture(architecture: dict[str, object]) -> None:
    counts = architecture.get("counts")
    bounded_counts = counts if isinstance(counts, dict) else {}
    _print_console_line(
        f"CODE_ARCHITECTURE status={architecture.get('status')} "
        f"gate={architecture.get('gate')} "
        f"modules={bounded_counts.get('modules', 0)} "
        f"imports={bounded_counts.get('imports', 0)} "
        f"cycles={bounded_counts.get('cycles', 0)} "
        f"contracts={bounded_counts.get('contracts', 0)} "
        f"failed_contracts={bounded_counts.get('failed_contracts', 0)} "
        f"reason={json.dumps(architecture.get('reason'), ensure_ascii=True)}"
    )
    architecture_summary = architecture.get("summary")
    if isinstance(architecture_summary, dict):
        _print_console_line(
            "CODE_ARCHITECTURE_SUMMARY "
            f"modules={architecture_summary.get('modules', 0)} "
            f"import_edges={architecture_summary.get('import_edges', 0)} "
            f"consensus_edges={architecture_summary.get('consensus_edges', 0)} "
            f"graph_disagreements={architecture_summary.get('graph_disagreements', 0)} "
            f"cyclic_sccs={architecture_summary.get('cyclic_sccs', 0)}"
        )
    else:
        _print_console_line("CODE_ARCHITECTURE_SUMMARY status=not_evaluated")
    architecture_providers = architecture.get("providers")
    if isinstance(architecture_providers, list):
        for provider in architecture_providers:
            if not isinstance(provider, dict):
                continue
            _print_console_line(
                f"CODE_ARCHITECTURE_PROVIDER id={provider.get('provider_id')} "
                f"status={provider.get('status')} execution={provider.get('execution')} "
                f"metrics={provider.get('metrics', 0)} "
                f"relations={provider.get('relations', 0)} "
                f"gate={provider.get('provider_gate')}"
            )
    architecture_gates = architecture.get("gates")
    if isinstance(architecture_gates, list):
        for gate in architecture_gates:
            if not isinstance(gate, dict):
                continue
            _print_console_line(
                f"CODE_ARCHITECTURE_GATE id={gate.get('gate')} "
                f"status={gate.get('status')} "
                f"reason={json.dumps(gate.get('reason'), ensure_ascii=True)}"
            )


def _emit_code_coverage(prefix: str, coverage: dict[str, object]) -> None:
    """Render bounded trusted-deep evidence without assuming it is available."""

    outcomes_value = coverage.get("outcomes")
    totals_value = coverage.get("totals")
    outcomes = outcomes_value if isinstance(outcomes_value, dict) else {}
    totals = totals_value if isinstance(totals_value, dict) else {}
    _print_console_line(
        f"{prefix} status={coverage.get('status')} "
        f"suite={coverage.get('suite_selection')} "
        f"measurement_complete={int(bool(coverage.get('measurement_complete')))} "
        f"content_executed={int(bool(coverage.get('content_executed')))} "
        f"tests={outcomes.get('passed', 0)}/{outcomes.get('selected', 0)} "
        f"collected={outcomes.get('collected', 0)} "
        f"failed={outcomes.get('failed', 0)} skipped={outcomes.get('skipped', 0)} "
        f"lines={totals.get('covered_lines', 0)}/{totals.get('executable_lines', 0)} "
        f"branches={totals.get('covered_branch_exits', 0)}/"
        f"{totals.get('branch_exits', 0)} "
        f"reason={json.dumps(coverage.get('reason'), ensure_ascii=True)}"
    )
    gates_value = coverage.get("gates")
    if not isinstance(gates_value, (list, tuple)):
        return
    for gate in gates_value[:_CODE_CLI_COVERAGE_EXAMPLE_LIMIT]:
        if not isinstance(gate, dict):
            continue
        _print_console_line(
            f"{prefix}_GATE id={gate.get('gate')} status={gate.get('status')} "
            f"reason={json.dumps(gate.get('reason'), ensure_ascii=True)}"
        )
    failed_tests = coverage.get("failed_test_nodeids")
    if failed_tests is None:
        failed_tests = coverage.get("failed_test_examples")
    if isinstance(failed_tests, (list, tuple)):
        for nodeid in failed_tests[:_CODE_CLI_COVERAGE_EXAMPLE_LIMIT]:
            _print_console_line(
                f"{prefix}_FAILED_TEST nodeid={json.dumps(nodeid, ensure_ascii=True)}"
            )


def _emit_code_unused(prefix: str, analysis: dict[str, object]) -> None:
    """Render bounded advisory unused-code evidence across all four states."""

    counts_value = analysis.get("counts")
    counts = counts_value if isinstance(counts_value, dict) else {}
    state_total = sum(
        int(counts.get(state, 0))
        for state in (
            "explained_usage",
            "dynamic_usage_possible",
            "insufficient_evidence",
            "probable_unused_high_consensus",
        )
    )
    _print_console_line(
        f"{prefix} status={analysis.get('status')} "
        f"total={counts.get('total', state_total)} "
        f"explained_usage={counts.get('explained_usage', 0)} "
        f"dynamic_usage_possible={counts.get('dynamic_usage_possible', 0)} "
        f"insufficient_evidence={counts.get('insufficient_evidence', 0)} "
        f"probable_unused_high_consensus="
        f"{counts.get('probable_unused_high_consensus', 0)} "
        f"authority={analysis.get('authority')} "
        f"mutation_authority={int(bool(analysis.get('mutation_authority')))} "
        f"reason={json.dumps(analysis.get('reason'), ensure_ascii=True)}"
    )
    providers = analysis.get("providers")
    if isinstance(providers, (list, tuple)):
        for provider in providers[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
            if not isinstance(provider, dict):
                continue
            _print_console_line(
                f"{prefix}_PROVIDER id={provider.get('provider_id')} "
                f"status={provider.get('status')} "
                f"comparability={provider.get('comparability')} "
                f"findings={provider.get('findings', 0)} "
                f"eligible={provider.get('eligible_candidates', 0)} "
                f"covered={provider.get('covered_candidates', 0)} "
                f"reason={json.dumps(provider.get('reason'), ensure_ascii=True)}"
            )
    for split in ("calibration", "holdout"):
        report = analysis.get(split)
        if not isinstance(report, dict):
            _print_console_line(f"{prefix}_{split.upper()} status=not_evaluated")
            continue
        _print_console_line(
            f"{prefix}_{split.upper()} signature={report.get('signature')} "
            f"samples={report.get('total', 0)} "
            f"precision={report.get('precision')} recall={report.get('recall')} "
            f"abstention={report.get('abstention_rate', report.get('abstention'))} "
            f"unsupported={report.get('unsupported', 0)}"
        )
    candidates = analysis.get("candidates")
    if isinstance(candidates, (list, tuple)):
        for candidate in candidates[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
            if not isinstance(candidate, dict):
                continue
            reasons = candidate.get("reasons")
            bounded_reasons = (
                reasons[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]
                if isinstance(reasons, (list, tuple))
                else []
            )
            _print_console_line(
                f"{prefix}_CANDIDATE id={candidate.get('candidate_id')} "
                f"state={candidate.get('state')} "
                f"path={json.dumps(candidate.get('relative_path'), ensure_ascii=True)} "
                f"symbol={json.dumps(candidate.get('symbol'), ensure_ascii=True)} "
                f"line={candidate.get('start_line')} "
                f"providers={json.dumps(candidate.get('provider_ids'), ensure_ascii=True)} "
                f"reasons={json.dumps(bounded_reasons, ensure_ascii=True)}"
            )
        if len(candidates) > _CODE_CLI_UNUSED_EXAMPLE_LIMIT:
            _print_console_line(
                f"{prefix}_CANDIDATES shown={_CODE_CLI_UNUSED_EXAMPLE_LIMIT} "
                f"omitted={len(candidates) - _CODE_CLI_UNUSED_EXAMPLE_LIMIT}"
            )
    limitations = analysis.get("limitations")
    if isinstance(limitations, (list, tuple)):
        for limitation in limitations[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
            _print_console_line(f"{prefix}_LIMITATION {limitation}")
    gates = analysis.get("gates")
    if isinstance(gates, (list, tuple)):
        for gate in gates[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
            if not isinstance(gate, dict):
                continue
            _print_console_line(
                f"{prefix}_GATE id={gate.get('gate')} status={gate.get('status')} "
                f"reason={json.dumps(gate.get('reason'), ensure_ascii=True)}"
            )


def _emit_code_status(
    path: Path,
    analyzers: object,
    snapshot: _CodeStatusSnapshot,
    self_analysis: dict[str, object] | None,
    *,
    json_output: bool,
) -> None:
    payload = _code_status_payload(path, analyzers, snapshot, self_analysis)
    latest = snapshot.latest_run
    if json_output:
        _emit(payload, json_output=True)
        return
    _print_console_line(
        f"CODE_STATUS database={path} schema={snapshot.schema_version} "
        + " ".join(f"{name}={value}" for name, value in snapshot.counts.items())
    )
    suite = snapshot.external_evidence_suite
    _print_console_line(
        f"CODE_PROVIDER_SUITE profile={suite.get('profile')} status={suite.get('status')}"
    )
    providers = suite.get("providers")
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            _print_console_line(
                f"CODE_PROVIDER id={provider.get('provider_id')} "
                f"status={provider.get('status')} execution={provider.get('execution')} "
                f"findings={provider.get('findings', 0)} "
                f"metrics={provider.get('metrics', 0)} "
                f"relations={provider.get('relations', 0)} "
                f"content_executed={int(bool(provider.get('content_executed')))} "
                f"gate={provider.get('gate')}"
            )
    if latest is not None:
        _print_console_line(
            f"CODE_RUN id={latest['analysis_run_id']} "
            f"framework_run={latest['framework_run_id']} status={latest['status']} "
            f"candidates={latest['candidates']} processed={latest['processed']} "
            f"cache_hits={latest['cache_hits']} errors={latest['errors']}"
        )
    external = snapshot.external_evidence
    _print_console_line(
        f"CODE_EXTERNAL provider=ruff status={external['status']} "
        f"execution={external.get('execution')} diagnostics="
        f"{external.get('diagnostics', 0)} gate={external.get('gate')}"
    )
    _emit_code_status_architecture(snapshot.architecture)
    _emit_code_coverage("CODE_COVERAGE", snapshot.test_coverage)
    _emit_code_unused("CODE_UNUSED", snapshot.unused_analysis)
    _emit_code_supply_chain("CODE_SUPPLY_CHAIN", snapshot.supply_chain)
    _emit_code_engineering(snapshot.engineering_analytics)


def _code_status_payload(
    path: Path,
    analyzers: object,
    snapshot: _CodeStatusSnapshot,
    self_analysis: dict[str, object] | None,
) -> dict[str, object]:
    latest = snapshot.latest_run
    return {
        "kind": "code-status",
        "schema": "neocortex.code-status/v1",
        "database": str(path),
        "exists": True,
        "schema_version": snapshot.schema_version,
        "counts": snapshot.counts,
        "latest_run": None if latest is None else dict(latest),
        "analyzers": analyzers,
        "self_analysis": self_analysis,
        "external_evidence": snapshot.external_evidence,
        "analysis_profile": snapshot.external_evidence_suite.get("profile"),
        "external_evidence_suite": snapshot.external_evidence_suite,
        "architecture": snapshot.architecture,
        "test_coverage": snapshot.test_coverage,
        "unused_analysis": snapshot.unused_analysis,
        "supply_chain": snapshot.supply_chain,
        "engineering_analytics": snapshot.engineering_analytics,
    }


def _emit_code_review_architecture(analysis: CodeArchitectureAnalysis) -> None:
    failed_contracts = tuple(item for item in analysis.contracts if item.status == "failed")
    _print_console_line(
        f"CODE_REVIEW_ARCHITECTURE status={analysis.status} gate={analysis.gate} "
        f"failed_contracts={len(failed_contracts)} "
        f"reason={json.dumps(analysis.reason, ensure_ascii=True)}"
    )
    if analysis.summary is None:
        _print_console_line("CODE_REVIEW_ARCHITECTURE_SUMMARY status=not_evaluated")
    else:
        summary = analysis.summary
        _print_console_line(
            f"CODE_REVIEW_ARCHITECTURE_SUMMARY modules={summary.modules} "
            f"import_edges={summary.import_edges} consensus_edges={summary.consensus_edges} "
            f"graph_disagreements={summary.graph_disagreements} "
            f"cyclic_sccs={summary.cyclic_sccs}"
        )
    for gate in analysis.gates:
        _print_console_line(
            f"CODE_REVIEW_ARCHITECTURE_GATE id={gate.gate} status={gate.status} "
            f"reason={json.dumps(gate.reason, ensure_ascii=True)}"
        )
    for contract in failed_contracts[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_REVIEW_ARCHITECTURE_CONTRACT status=failed "
            f"id={json.dumps(contract.contract_id, ensure_ascii=True)} "
            f"violations={contract.violations} "
            f"importers={json.dumps(contract.importer_modules, ensure_ascii=True)} "
            f"imported={json.dumps(contract.imported_modules, ensure_ascii=True)}"
        )
    if len(failed_contracts) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT:
        _print_console_line(
            "CODE_REVIEW_ARCHITECTURE_CONTRACTS "
            f"shown={_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT} "
            f"omitted={len(failed_contracts) - _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT}"
        )


def _module_architecture_changed(module: CodeModuleArchitectureDelta) -> bool:
    return bool(
        module.cognitive_complexity_delta
        or module.fan_in_delta
        or module.fan_out_delta
        or getattr(module, "dependency_reach_delta", None)
        or getattr(module, "blast_radius_delta", None)
        or getattr(module, "directed_degree_centrality_delta", None)
        or getattr(module, "cross_path_namespace_fan_in_delta", None)
        or getattr(module, "cross_path_namespace_fan_out_delta", None)
        or getattr(module, "graph_metrics_status", "comparable") == "not_comparable"
        or module.baseline_cycle_ids != module.current_cycle_ids
        or module.baseline_contract_ids != module.current_contract_ids
    )


def _emit_code_publication_architecture(architecture: CodeArchitectureDelta) -> None:
    modules = architecture.modules
    changed_modules = tuple(item for item in modules if _module_architecture_changed(item))
    added_contracts = architecture.added_failed_contracts
    resolved_contracts = architecture.resolved_failed_contracts
    added_cycles = architecture.added_cycles
    resolved_cycles = architecture.resolved_cycles
    displacements = architecture.displaced_complexity
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_ARCHITECTURE status={architecture.status} "
        f"module_deltas={len(modules)} changed_modules={len(changed_modules)} "
        f"added_failed_contracts={len(added_contracts)} "
        f"resolved_failed_contracts={len(resolved_contracts)} "
        f"added_cycles={len(added_cycles)} resolved_cycles={len(resolved_cycles)} "
        f"displacements={len(displacements)} "
        f"contracts_gate={architecture.architecture_contracts_not_degraded} "
        f"cycles_gate={architecture.no_new_import_cycles} "
        f"displacement_gate={architecture.module_complexity_not_displaced} "
        f"reason={json.dumps(architecture.reason, ensure_ascii=True)}"
    )
    _print_console_line(
        "CODE_PUBLICATION_DIFF_ARCHITECTURE_CONTRACTS "
        f"added={json.dumps(added_contracts[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT], ensure_ascii=True)} "
        f"resolved={json.dumps(resolved_contracts[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT], ensure_ascii=True)} "
        f"added_truncated={int(len(added_contracts) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)} "
        f"resolved_truncated={int(len(resolved_contracts) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)}"
    )
    _print_console_line(
        "CODE_PUBLICATION_DIFF_ARCHITECTURE_CYCLES "
        f"added={json.dumps(added_cycles[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT], ensure_ascii=True)} "
        f"resolved={json.dumps(resolved_cycles[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT], ensure_ascii=True)} "
        f"added_truncated={int(len(added_cycles) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)} "
        f"resolved_truncated={int(len(resolved_cycles) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)}"
    )
    for module in changed_modules[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_ARCHITECTURE_MODULE "
            f"module={json.dumps(module.module_id, ensure_ascii=True)} "
            f"cognitive_delta={module.cognitive_complexity_delta} "
            f"fan_in_delta={module.fan_in_delta:+d} fan_out_delta={module.fan_out_delta:+d} "
            f"dependency_reach_delta={getattr(module, 'dependency_reach_delta', None)} "
            f"blast_radius_delta={getattr(module, 'blast_radius_delta', None)} "
            f"centrality_delta={getattr(module, 'directed_degree_centrality_delta', None)} "
            "cross_path_namespace_fan_in_delta="
            f"{getattr(module, 'cross_path_namespace_fan_in_delta', None)} "
            "cross_path_namespace_fan_out_delta="
            f"{getattr(module, 'cross_path_namespace_fan_out_delta', None)} "
            f"graph_status={getattr(module, 'graph_metrics_status', 'not_comparable')} "
            f"baseline_cycles={json.dumps(module.baseline_cycle_ids, ensure_ascii=True)} "
            f"current_cycles={json.dumps(module.current_cycle_ids, ensure_ascii=True)} "
            f"baseline_contracts={json.dumps(module.baseline_contract_ids, ensure_ascii=True)} "
            f"current_contracts={json.dumps(module.current_contract_ids, ensure_ascii=True)}"
        )
    for displacement in displacements[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_ARCHITECTURE_DISPLACEMENT "
            f"target={json.dumps(displacement.target_module, ensure_ascii=True)} "
            f"target_decrease={displacement.target_decrease} "
            f"recipients={json.dumps(displacement.recipient_modules, ensure_ascii=True)} "
            f"recipient_increase={displacement.recipient_increase} "
            f"imports={json.dumps(displacement.import_relationships, ensure_ascii=True)}"
        )
    if (
        len(changed_modules) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT
        or len(displacements) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT
    ):
        _print_console_line(
            "CODE_PUBLICATION_DIFF_ARCHITECTURE_EXAMPLES "
            f"module_examples_omitted="
            f"{max(0, len(changed_modules) - _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)} "
            f"displacement_examples_omitted="
            f"{max(0, len(displacements) - _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)}"
        )


def run_code_status(args: argparse.Namespace) -> int:
    """Show bounded read-only state and lazy analyzer registration status."""

    from neocortex.code.code_analyzers import builtin_analyzer_registry
    from neocortex.workflow.self_analysis.self_analysis_status import require_sqlite_sidecars_absent

    path = _state_path(args)
    analyzers = builtin_analyzer_registry().status()
    try:
        require_sqlite_sidecars_absent(path)
        if not path.is_file():
            _emit_missing_code_status(path, analyzers, json_output=args.code_json)
            return 0
        snapshot = _read_code_status_snapshot(path)
        self_analysis = _read_self_analysis_payload(args, snapshot.latest_run)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-status", exc)
    _emit_code_status(
        path,
        analyzers,
        snapshot,
        self_analysis,
        json_output=args.code_json,
    )
    return 0


def _emit_code_question_human(payload: dict[str, object], *, limit: int) -> None:
    evaluations_value = payload.get("evaluations")
    evaluations = evaluations_value if isinstance(evaluations_value, list) else []
    _print_console_line(
        f"CODE_QUESTION id={json.dumps(payload.get('question_id'), ensure_ascii=True)} "
        f"version={payload.get('question_version')} status={payload.get('status')} "
        f"reader={payload.get('reader_id')} source={payload.get('source_surface')} "
        f"matched={payload.get('total_matches', 0)} returned={len(evaluations[:limit])} "
        f"truncated={int(bool(payload.get('truncated')))} "
        f"reason={json.dumps(payload.get('reason'), ensure_ascii=True)}"
    )
    fallback = payload.get("fallback")
    if isinstance(fallback, dict):
        _print_console_line(
            "CODE_QUESTION_FALLBACK "
            + json.dumps(fallback, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    for evaluation in evaluations[:limit]:
        _print_console_line(
            "CODE_QUESTION_EVALUATION "
            + json.dumps(evaluation, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    limitations = payload.get("limitations")
    if isinstance(limitations, list):
        for limitation in limitations[:limit]:
            _print_console_line(f"CODE_QUESTION_LIMITATION {limitation}")


def run_code_question(args: argparse.Namespace) -> int:
    """Resolve one registered question through its bounded focal reader only."""

    try:
        from neocortex.code.code_question_resolver import resolve_code_question

        result = resolve_code_question(
            args.state_directory,
            args.code_question,
            limit=args.code_question_limit,
        )
        payload = result.as_payload()
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        return _error("code-question", exc)
    if args.code_json:
        _emit(payload, json_output=True)
    else:
        _emit_code_question_human(payload, limit=args.code_question_limit)
    return 0 if result.status == "ready" else 2


def _emit_code_storage_human(payload: dict[str, object], *, limit: int) -> None:
    tables_value = payload.get("tables")
    providers_value = payload.get("providers")
    runs_value = payload.get("runs")
    tables = tables_value if isinstance(tables_value, list) else []
    providers = providers_value if isinstance(providers_value, list) else []
    runs = runs_value if isinstance(runs_value, list) else []
    _print_console_line(
        f"CODE_STORAGE status={payload.get('status')} database="
        f"{json.dumps(payload.get('database'), ensure_ascii=True)} "
        f"bytes={payload.get('database_file_bytes', 0)} "
        f"pages={payload.get('page_count', 0)} free_pages={payload.get('freelist_pages', 0)} "
        f"tables={len(tables)} providers={len(providers)} runs={len(runs)} "
        f"runs_truncated={int(bool(payload.get('runs_truncated')))} "
        f"reason={json.dumps(payload.get('reason'), ensure_ascii=True)}"
    )
    for table in tables[:limit]:
        _print_console_line(
            "CODE_STORAGE_TABLE "
            + json.dumps(table, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    for provider in providers[:limit]:
        _print_console_line(
            "CODE_STORAGE_PROVIDER "
            + json.dumps(provider, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    for run in runs[:limit]:
        _print_console_line(
            "CODE_STORAGE_RUN "
            + json.dumps(run, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    for label in ("growth", "retention"):
        detail = payload.get(label)
        if isinstance(detail, dict):
            _print_console_line(
                f"CODE_STORAGE_{label.upper()} "
                + json.dumps(detail, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            )
    limitations = payload.get("limitations")
    if isinstance(limitations, list):
        for limitation in limitations[:limit]:
            _print_console_line(f"CODE_STORAGE_LIMITATION {limitation}")


def run_code_storage(args: argparse.Namespace) -> int:
    """Inspect immutable bounded storage evidence without owner mutation."""

    run_limit = args.code_storage_run_limit
    retain_runs = args.code_storage_retain_runs
    if retain_runs is None:
        retain_runs = min(5, run_limit)
    try:
        from neocortex.code.code_storage_analysis import analyze_code_storage

        result = analyze_code_storage(
            _state_path(args),
            run_limit=run_limit,
            row_scan_limit=args.code_storage_row_scan_limit,
            retain_latest_completed_runs=retain_runs,
        )
        payload = result.as_payload()
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        return _error("code-storage", exc)
    if args.code_json:
        _emit(payload, json_output=True)
    else:
        _emit_code_storage_human(payload, limit=run_limit)
    return 0 if result.status == "ready" else 2


def _read_code_query_source(args: argparse.Namespace) -> dict[str, object]:
    surface = args.code_query
    if surface == "status":
        from neocortex.code.code_analyzers import builtin_analyzer_registry
        from neocortex.workflow.self_analysis.self_analysis_status import require_sqlite_sidecars_absent

        path = _state_path(args)
        analyzers = builtin_analyzer_registry().status()
        require_sqlite_sidecars_absent(path)
        if not path.is_file():
            return _missing_code_status_payload(path, analyzers)
        snapshot = _read_code_status_snapshot(path)
        self_analysis = _read_self_analysis_payload(
            args,
            snapshot.latest_run,
            enabled=True,
        )
        return _code_status_payload(path, analyzers, snapshot, self_analysis)
    if surface == "review":
        from neocortex.code.code_review import review_code_state

        return review_code_state(
            args.state_directory,
            limit=args.code_query_limit,
        ).as_payload()
    if surface == "diff":
        from neocortex.code.code_publication_diff import compare_code_publications

        baseline = getattr(args, "code_query_baseline", None)
        if baseline is None:
            raise ValueError("--code-query-baseline is required for --code-query diff")
        return compare_code_publications(
            Path(baseline),
            args.state_directory,
        ).as_payload()
    raise ValueError(f"unsupported Code query surface: {surface!r}")


def _emit_code_query_human(payload: dict[str, object], *, limit: int) -> None:
    raw_matches = payload.get("matches")
    matches = raw_matches if isinstance(raw_matches, list) else []
    displayed_matches = matches[:limit]
    raw_counts = payload.get("counts")
    counts = raw_counts if isinstance(raw_counts, dict) else {}
    _print_console_line(
        f"CODE_QUERY surface={payload.get('surface')} status={payload.get('status')} "
        f"matched={counts.get('matched', len(matches))} returned={len(displayed_matches)} "
        f"limit={limit}"
    )
    filters = payload.get("filters")
    if isinstance(filters, dict):
        _print_console_line(
            "CODE_QUERY_FILTERS "
            + json.dumps(filters, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    for match in displayed_matches:
        _print_console_line(
            "CODE_QUERY_MATCH "
            + json.dumps(match, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    limitations = payload.get("limitations")
    if isinstance(limitations, list):
        for limitation in limitations[:limit]:
            _print_console_line(f"CODE_QUERY_LIMITATION {limitation}")


def run_code_query(args: argparse.Namespace) -> int:
    """Filter one existing published Code surface without mutating its owner."""

    try:
        from neocortex.code.code_analysis_query import CodeAnalysisQuery, query_code_analysis

        source = _read_code_query_source(args)
        query = CodeAnalysisQuery(
            surface=args.code_query,
            providers=tuple(args.code_query_provider or ()),
            categories=tuple(args.code_query_category or ()),
            modules=tuple(args.code_query_module or ()),
            statuses=tuple(args.code_query_status or ()),
            deltas=tuple(args.code_query_delta or ()),
            work_packages=tuple(args.code_query_work_package or ()),
            limit=args.code_query_limit,
        )
        payload = query_code_analysis(source, query)
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        return _error("code-query", exc)
    if args.code_json:
        _emit(payload, json_output=True)
    else:
        _emit_code_query_human(payload, limit=args.code_query_limit)
    return 0 if payload.get("status") == "ready" else 2


def _emit_code_review_unused_result(result: CodeReviewResult) -> None:
    from neocortex.code.code_review_models import bounded_code_unused_payload

    unused_analysis = getattr(result, "unused_analysis", None)
    if unused_analysis is None:
        _emit_code_unused(
            "CODE_REVIEW_UNUSED",
            _unused_abstained_payload(result.database, "unused_result_missing"),
        )
        return
    _emit_code_unused(
        "CODE_REVIEW_UNUSED",
        bounded_code_unused_payload(unused_analysis),
    )


def _emit_code_review_supply_chain_result(result: CodeReviewResult) -> None:
    analysis = getattr(result, "supply_chain", None)
    payload = (
        _supply_chain_abstained_payload(result.database, "supply_chain_result_missing")
        if analysis is None
        else analysis.as_payload()
    )
    _emit_code_supply_chain("CODE_REVIEW_SUPPLY_CHAIN", payload)


def _emit_code_review_ranked_evidence(result: CodeReviewResult) -> None:
    state_projection = getattr(result, "state_projection", None)
    if state_projection is not None:
        _print_console_line(
            "CODE_STATE_PROJECTION "
            f"status={state_projection.status} "
            f"reason={json.dumps(state_projection.reason, ensure_ascii=True)} "
            f"policy={state_projection.policy_id} "
            f"observation={state_projection.observation} "
            f"inference={state_projection.inference_status} "
            f"decision={state_projection.decision_readiness} "
            f"heads={len(state_projection.heads)} "
            f"eligible_text={state_projection.eligible_text_rows} "
            f"excluded_empty={state_projection.excluded_empty_text_rows} "
            f"authority={state_projection.authority} "
            f"mutation_authority={int(state_projection.mutation_authority)}"
        )
    state_topology = getattr(result, "state_topology", None)
    if state_topology is not None:
        closure = state_topology.closure
        _print_console_line(
            "CODE_STATE_TOPOLOGY "
            f"status={state_topology.status} "
            f"reason={json.dumps(state_topology.reason, ensure_ascii=True)} "
            f"workflow={state_topology.workflow_id} "
            f"boundary={state_topology.boundary_id} "
            f"observation={state_topology.observation} "
            f"decision={state_topology.decision_readiness} "
            f"terminal_attempts={0 if closure is None else closure.terminal_attempts} "
            f"receipts={0 if closure is None else closure.receipts} "
            f"outbox_events={0 if closure is None else closure.outbox_events} "
            f"relationally_closed={int(bool(closure and closure.relationally_closed))} "
            f"authority={state_topology.authority} "
            f"mutation_authority={int(state_topology.mutation_authority)}"
        )
    retention = getattr(result, "retention_analysis", None)
    if retention is not None:
        ready_stores = sum(item.status == "ready" for item in retention.stores)
        truncated_stores = sum(item.truncated for item in retention.stores)
        _print_console_line(
            "CODE_RETENTION_ANALYSIS "
            f"status={retention.status} "
            f"reason={json.dumps(retention.reason, ensure_ascii=True)} "
            f"policy={retention.policy_id} "
            f"observation={retention.observation} "
            f"stores={len(retention.stores)} "
            f"ready_stores={ready_stores} "
            f"missing_holds={len(retention.missing_hold_ids)} "
            f"truncated_stores={truncated_stores} "
            f"authority={retention.authority} "
            f"mutation_authority={int(retention.mutation_authority)}"
        )
    state_interactions = getattr(result, "state_interactions", None)
    if state_interactions is not None:
        _print_console_line(
            "CODE_STATE_INTERACTIONS "
            f"status={state_interactions.status} "
            f"reason={json.dumps(state_interactions.reason, ensure_ascii=True)} "
            f"files={state_interactions.source_files} "
            f"literal_sql={state_interactions.literal_sql_sites} "
            f"parsed_sql={state_interactions.parsed_sql_sites} "
            f"dynamic_sql={state_interactions.dynamic_sql_sites} "
            f"parse_errors={state_interactions.parse_error_sites} "
            f"interactions={state_interactions.interactions_count} "
            f"transactions={state_interactions.transaction_events_count} "
            f"workflow_boundaries={len(state_interactions.workflow_boundaries)} "
            f"parser={state_interactions.sql_parser} "
            f"authority={state_interactions.authority} "
            f"mutation_authority={int(state_interactions.mutation_authority)}"
        )
    change_evolution = getattr(result, "change_evolution", None)
    if change_evolution is not None:
        surface = change_evolution.change_surface
        _print_console_line(
            "CODE_CHANGE_EVOLUTION "
            f"status={change_evolution.status} "
            f"reason={json.dumps(change_evolution.reason, ensure_ascii=True)} "
            f"surface={surface.status} history={change_evolution.history.status} "
            f"schema={change_evolution.code_schema.status} "
            f"observations={surface.total_observations} "
            f"truncated={int(surface.truncated)} "
            f"authority={change_evolution.authority} "
            f"mutation_authority={int(change_evolution.mutation_authority)}"
        )
    assurance = getattr(result, "assurance", None)
    if assurance is not None:
        _print_console_line(
            "CODE_ASSURANCE "
            f"status={assurance.status} "
            f"reason={json.dumps(assurance.reason, ensure_ascii=True)} "
            f"eligible={assurance.eligible_symbols} "
            f"returned={assurance.returned_symbols} "
            f"truncated={int(assurance.selection_truncated)} "
            f"calibration={assurance.calibration.status} "
            f"behavioral_claims={assurance.calibration.behavioral_assurance_claims} "
            f"authority={assurance.authority} "
            f"mutation_authority={int(assurance.mutation_authority)}"
        )
    invariant_assurance = getattr(result, "invariant_assurance", None)
    if invariant_assurance is not None:
        _print_console_line(
            "CODE_INVARIANT_ASSURANCE "
            f"status={invariant_assurance.status} "
            f"reason={json.dumps(invariant_assurance.reason, ensure_ascii=True)} "
            f"provider={invariant_assurance.provider_status} "
            f"invariants={invariant_assurance.declared_invariants} "
            f"scenarios={invariant_assurance.declared_scenarios} "
            f"resolved={invariant_assurance.resolved_scenarios} "
            f"passed={invariant_assurance.passed_scenarios} "
            f"counterevidence={invariant_assurance.counterevidence_scenarios} "
            f"authority={invariant_assurance.authority} "
            f"mutation_authority={int(invariant_assurance.mutation_authority)}"
        )
    capability = getattr(result, "capability_reachability", None)
    if capability is not None:
        published = sum(item.manifest_matching_current_heads for item in capability.observations)
        _print_console_line(
            "CODE_CAPABILITY_REACHABILITY "
            f"status={capability.status} "
            f"reason={json.dumps(capability.reason, ensure_ascii=True)} "
            f"manifests={capability.manifest_count} "
            f"attempts={capability.total_attempts} "
            f"unattributed={capability.unattributed_attempts} "
            f"manifest_matching_current_heads={published} "
            f"authority={capability.authority} "
            f"mutation_authority={int(capability.mutation_authority)}"
        )
    routes = getattr(result, "route_capabilities", None)
    if routes is not None:
        _print_console_line(
            "CODE_ROUTE_CAPABILITIES "
            f"status={routes.status} "
            f"reason={json.dumps(routes.reason, ensure_ascii=True)} "
            f"routes={len(routes.observations)} "
            "causal="
            f"{sum(item.causal_durable_output_observed for item in routes.observations)} "
            "owner_state_unattributed="
            f"{sum(item.evidence_level == 'owner_state_observed_unattributed' for item in routes.observations)} "
            f"authority={routes.authority} "
            f"mutation_authority={int(routes.mutation_authority)}"
        )
    analyzer = getattr(result, "analyzer_effectiveness", None)
    if analyzer is not None:
        _print_console_line(
            "CODE_ANALYZER_EFFECTIVENESS "
            f"status={analyzer.status} "
            f"reason={json.dumps(analyzer.reason, ensure_ascii=True)} "
            f"inventory={analyzer.inventory_observation} "
            f"recorded={analyzer.scoped_recorded_files} "
            f"git_visible={analyzer.git_visible_files} "
            f"content_equal={analyzer.exact_content_files} "
            f"content_changed={analyzer.content_changed_files} "
            f"missing={analyzer.missing_recorded_files} "
            f"unindexed={analyzer.unindexed_git_visible_files} "
            f"duration_ms={analyzer.duration_ms} "
            f"providers_ready={analyzer.providers_ready}/{analyzer.providers_observed} "
            f"calibration={analyzer.calibration_status} "
            f"independent_labels={analyzer.independent_outcome_labels} "
            f"authority={analyzer.authority} "
            f"mutation_authority={int(analyzer.mutation_authority)}"
        )
    calibration = getattr(result, "analyzer_calibration", None)
    if calibration is not None:
        _print_console_line(
            "CODE_ANALYZER_CALIBRATION "
            f"status={calibration.status} "
            f"reason={json.dumps(calibration.reason, ensure_ascii=True)} "
            f"labels={calibration.labels_total} "
            f"independent={calibration.independent_labels} "
            f"provisional={calibration.provisional_labels} "
            f"holdout={calibration.holdout_labels} "
            f"antigoodhart_passed={calibration.anti_goodhart_passed} "
            f"antigoodhart_failed={calibration.anti_goodhart_failed} "
            f"antigoodhart_missing={calibration.anti_goodhart_not_observed} "
            f"antigoodhart_nodeids={sum(len(item.test_nodeids) for item in calibration.anti_goodhart_controls)} "
            f"authority={calibration.authority} "
            f"mutation_authority={int(calibration.mutation_authority)}"
        )
    plan = getattr(result, "experiment_plan", None)
    if plan is not None:
        execution_readiness = (
            "not_required"
            if plan.experiment_required_count == 0
            else "executable"
            if plan.executable_count > 0
            else "registry_gap"
            if plan.registry_gap_count > 0
            else "manual"
        )
        _print_console_line(
            "CODE_EXPERIMENT_PLAN "
            f"status={plan.status} "
            f"execution={execution_readiness} "
            f"reason={json.dumps(plan.reason, ensure_ascii=True)} "
            f"evaluations={plan.source_evaluation_count} "
            f"required={plan.experiment_required_count} "
            f"planned={plan.planned_count} "
            f"executable={plan.executable_count} "
            f"registry_gaps={plan.registry_gap_count} "
            f"authority={plan.authority} "
            f"mutation_authority={int(plan.mutation_authority)}"
        )
        receipts = getattr(result, "experiment_receipts", ())
        if receipts:
            _print_console_line(
                "CODE_EXPERIMENT_EVIDENCE "
                f"receipts={len(receipts)} passed={sum(item.receipt.status == 'passed' for item in receipts)} "
                "human_actor_impersonated=0 mutation_authority=0"
            )
        technical = getattr(result, "technical_verification", None)
        if technical is not None:
            _print_console_line(
                "CODE_TECHNICAL_VERIFICATION "
                f"status={technical.status} "
                f"eligible={technical.evidence_complete_evaluations} "
                f"reviewed={technical.reviewed_count} "
                f"no_change_required={technical.no_change_required_count} "
                f"unresolved={technical.unresolved_count} "
                f"authority={technical.authority} "
                f"mutation_authority={int(technical.mutation_authority)}"
            )
            for item in technical.reviews:
                _print_console_line(
                    "CODE_TECHNICAL_DISPOSITION "
                    f"question_id={item.question_id} "
                    f"subject={json.dumps(item.subject_key, ensure_ascii=True)} "
                    f"disposition={item.disposition} "
                    f"reason={item.reason_code} "
                    f"receipts={len(item.receipt_ids)} "
                    f"mutation_authority={int(item.mutation_authority)}"
                )
        executable = tuple(
            proposal
            for proposal in plan.proposals
            if proposal.planning_status == "planned" and proposal.runner_kind != "none"
        )
        for proposal in executable[:20]:
            _print_console_line(
                "CODE_EXPERIMENT_PROPOSAL "
                f"proposal_id={proposal.proposal_id} "
                f"question_id={proposal.question_id} "
                f"subject={json.dumps(proposal.subject_key, ensure_ascii=True)} "
                f"template={proposal.template_id} cost={proposal.cost_tier} "
                f"timeout_seconds={proposal.timeout_seconds} "
                f"scenarios={len(proposal.scenario_ids)} "
                f"authority={proposal.authority} "
                f"mutation_authority={int(proposal.mutation_authority)}"
            )
        if len(executable) > 20:
            _print_console_line(
                f"CODE_EXPERIMENT_PROPOSALS_TRUNCATED total={len(executable)} returned=20"
            )
    interface = getattr(result, "interface_surface", None)
    if interface is not None:
        _print_console_line(
            "CODE_INTERFACE_SURFACE "
            f"status={interface.status} "
            f"reason={json.dumps(interface.reason, ensure_ascii=True)} "
            f"modules={interface.selected_modules}/{interface.total_modules} "
            f"module_examples={interface.returned_modules} "
            f"module_truncated={int(interface.module_selection_truncated)} "
            f"config_exact={interface.exact_configuration_artifacts}/"
            f"{interface.configuration_artifacts} "
            f"config_unsupported={interface.unsupported_configuration_artifacts} "
            f"config_incomplete={interface.incomplete_configuration_artifacts} "
            f"cli_exact={interface.exact_cli_files}/{interface.cli_candidate_files} "
            f"argparse_calls={interface.ast_argparse_call_sites}/"
            f"{interface.recorded_argparse_call_sites} "
            f"authority={interface.authority} "
            f"mutation_authority={int(interface.mutation_authority)}"
        )
    structural = getattr(result, "structural_analysis", None)
    if structural is not None:
        _print_console_line(
            "CODE_CLASS_SURFACE "
            f"status={structural.status} policy={structural.policy_id} "
            f"eligible={structural.eligible_classes} "
            f"selected={structural.selected_classes} "
            f"returned={structural.returned_classes} "
            f"truncated={int(structural.selection_truncated)} "
            f"span_threshold={structural.span_lines_threshold} "
            f"method_threshold={structural.direct_methods_threshold} "
            f"authority={structural.authority} "
            f"mutation_authority={int(structural.mutation_authority)}"
        )
    for recommendation in result.recommendations:
        _print_console_line(
            "CODE_REVIEW_RECOMMENDATION status=ready "
            f"recommendation_rank={recommendation.recommendation_rank} "
            f"hotspot_rank={recommendation.hotspot_rank} "
            f"construction={recommendation.construction} "
            f"risk={recommendation.change_risk} "
            f"production_callers={recommendation.production_callers} "
            f"test_callers={recommendation.test_callers} "
            f"path={json.dumps(recommendation.path, ensure_ascii=True)} "
            f"symbol={json.dumps(recommendation.symbol, ensure_ascii=True)}"
        )
    for finding in result.findings:
        epistemic = finding.epistemic_state
        _print_console_line(
            f"CODE_REVIEW_FINDING rank={finding.rank} "
            f"score_bp={finding.score_basis_points} category={finding.category} "
            f"construction={finding.construction} "
            f"actionability={finding.actionability} risk={finding.change_risk} "
            f"observation={epistemic.observation_status} "
            f"question={epistemic.question_readiness} "
            f"decision={epistemic.decision_readiness} "
            f"authority={epistemic.authority} "
            f"mutation_authority={int(epistemic.mutation_authority)} "
            f"complexity={finding.complexity} lines={finding.function_lines} "
            "path_convention_production_callers="
            f"{finding.impact.path_convention_production_callers} "
            "path_convention_test_or_fixture_callers="
            f"{finding.impact.path_convention_test_callers + finding.impact.path_convention_fixture_callers} "
            f"path={json.dumps(finding.path, ensure_ascii=True)} "
            f"symbol={json.dumps(finding.symbol, ensure_ascii=True)} "
            f"line={finding.start_line}"
        )
        _print_console_line(
            f"CODE_REVIEW_QUESTION rank={finding.rank} "
            f"finding_id={finding.finding_id} "
            f"question_id={epistemic.question_id} "
            f"question_version={epistemic.question_version} "
            f"inference={epistemic.inference_status} "
            f"decision_readiness={epistemic.decision_readiness} "
            f"decision={json.dumps(epistemic.decision, ensure_ascii=True)} "
            f"missing_evidence={json.dumps(epistemic.missing_evidence, ensure_ascii=True)} "
            f"counterevidence={epistemic.counterevidence_status} "
            f"next_actions={json.dumps(epistemic.next_actions, ensure_ascii=True)} "
            f"authority={epistemic.authority} "
            f"mutation_authority={int(epistemic.mutation_authority)}"
        )
    for evaluation in getattr(result, "question_evaluations", ()):
        source_records = tuple(
            f"{item.source_record_kind}:{item.source_record_id}" for item in evaluation.evidence
        )
        _print_console_line(
            f"CODE_ANALYSIS_QUESTION rank={evaluation.rank} "
            f"evaluation_id={evaluation.evaluation_id} "
            f"question_id={evaluation.question_id} "
            f"question_version={evaluation.question_version} "
            f"spec_fingerprint={evaluation.question_spec_fingerprint} "
            f"subject_kind={evaluation.subject.subject_kind} "
            f"subject_key={evaluation.subject.subject_key} "
            f"freshness={evaluation.subject.snapshot_freshness} "
            f"observation={evaluation.observation_status} "
            f"inference={evaluation.inference_status} "
            f"question={evaluation.question_readiness} "
            f"decision={evaluation.decision_readiness} "
            f"source_records={json.dumps(source_records, ensure_ascii=True)} "
            f"authority={evaluation.authority} "
            f"mutation_authority={int(evaluation.mutation_authority)}"
        )
    for limitation in result.limitations:
        _print_console_line(f"CODE_REVIEW_LIMITATION {limitation}")


def _emit_code_review_json(result: CodeReviewResult) -> int:
    _emit(result.as_payload(), json_output=True)
    return 0 if result.status == "ready" else 2


def _emit_code_review_abstention(result: CodeReviewResult) -> int:
    _print_console_line(
        f"CODE_REVIEW status=abstained reason={result.reason} "
        f"database={json.dumps(result.database, ensure_ascii=True)}"
    )
    return 2


def _complete_code_review_evidence(
    result: CodeReviewResult,
) -> tuple[CodeReviewSnapshot, CodeReviewCoverage, CodeReviewDigest] | None:
    snapshot = result.snapshot
    coverage = result.coverage
    digest = result.digest
    if snapshot is None or coverage is None or digest is None:
        return None
    return snapshot, coverage, digest


def _emit_code_review_header(
    result: CodeReviewResult,
    snapshot: CodeReviewSnapshot,
    coverage: CodeReviewCoverage,
    digest: CodeReviewDigest,
) -> None:
    _print_console_line(
        f"CODE_REVIEW status=ready freshness={snapshot.freshness} "
        f"current={int(snapshot.current)} findings={len(result.findings)} "
        f"recommendations={len(result.recommendations)} "
        f"work_packages={len(result.work_packages)} ranking={result.ranking} "
        f"actionability={result.actionability_version} planner={result.planning_version} "
        f"digest={digest.xxh3_128}"
    )
    _print_console_line(
        f"CODE_REVIEW_COVERAGE python_files={coverage.current_python_files} "
        f"complete={coverage.complete_python_files} "
        f"hotspots={coverage.candidate_hotspots} "
        f"probable_dead_suppressed={coverage.probable_dead_suppressed} "
        f"resolved_calls={coverage.resolved_call_edges}/{coverage.call_edges}"
    )


def _emit_code_review_external_evidence(result: CodeReviewResult) -> None:
    if result.external_evidence is not None:
        external = result.external_evidence
        _print_console_line(
            f"CODE_REVIEW_EXTERNAL provider={external.provider} "
            f"status={external.status} execution={external.execution} "
            f"diagnostics={external.diagnostics} added={external.added} "
            f"resolved={external.resolved} gate={external.gate}"
        )
    if result.external_evidence_suite is None:
        return
    _print_console_line(
        f"CODE_REVIEW_PROVIDER_SUITE profile={result.external_evidence_suite.profile} "
        f"status={result.external_evidence_suite.status}"
    )
    for provider in result.external_evidence_suite.providers:
        _print_console_line(
            f"CODE_REVIEW_PROVIDER id={provider.provider_id} "
            f"status={provider.status} findings={provider.findings} "
            f"metrics={provider.metrics} relations={provider.relations} "
            f"content_executed={int(provider.content_executed)} "
            f"gate={provider.gate}"
        )


def _emit_code_review_architecture_result(result: CodeReviewResult) -> None:
    if result.architecture is None:
        _print_console_line(
            'CODE_REVIEW_ARCHITECTURE status=not_evaluated reason="architecture_result_missing"'
        )
        return
    _emit_code_review_architecture(result.architecture)


def _emit_code_review_test_coverage_result(result: CodeReviewResult) -> None:
    from neocortex.code.code_review_models import bounded_code_coverage_payload

    if result.test_coverage is None:
        payload: dict[str, object] = {
            "status": "abstained",
            "reason": "coverage_result_missing",
            "suite_selection": None,
            "measurement_complete": False,
            "content_executed": False,
            "outcomes": None,
            "totals": None,
            "gates": [],
        }
    else:
        payload = bounded_code_coverage_payload(result.test_coverage)
    _emit_code_coverage("CODE_REVIEW_TEST_COVERAGE", payload)


def _emit_code_review_abstention_statuses(result: CodeReviewResult) -> None:
    if result.recommendation_status == "abstained":
        _print_console_line(
            f"CODE_REVIEW_RECOMMENDATION status=abstained reason={result.recommendation_reason}"
        )
    if result.work_package_status == "abstained":
        _print_console_line(
            f"CODE_REVIEW_WORK_PACKAGE status=abstained reason={result.work_package_reason}"
        )


def _emit_code_review_question_summary(result: CodeReviewResult) -> None:
    evaluations = result.question_evaluations
    if not evaluations:
        _print_console_line(
            "CODE_ANALYSIS_QUESTIONS status=not_evaluated total=0 "
            "confirmed=0 abstained=0 human_review_required=0 "
            "experiment_required=0 decision_abstained=0 decisions=0 "
            "mutation_authority=0"
        )
        return
    _print_console_line(
        "CODE_ANALYSIS_QUESTIONS status=ready "
        f"total={len(evaluations)} "
        f"confirmed={sum(item.observation_status == 'confirmed' for item in evaluations)} "
        f"abstained={sum(item.observation_status == 'abstained' for item in evaluations)} "
        "human_review_required="
        f"{sum(item.decision_readiness == 'human_review_required' for item in evaluations)} "
        "experiment_required="
        f"{sum(item.decision_readiness == 'experiment_required' for item in evaluations)} "
        "decision_abstained="
        f"{sum(item.decision_readiness == 'abstained' for item in evaluations)} "
        f"decisions={sum(item.decision is not None for item in evaluations)} "
        f"mutation_authority={int(any(item.mutation_authority for item in evaluations))}"
    )


def _emit_code_review_work_package_supply_chain(
    package: CodeReviewWorkPackage,
) -> None:
    gates = getattr(package, "supply_chain_gates", ())
    observations = getattr(package, "supply_chain_observations", ())
    relations = getattr(package, "supply_chain_relations", ())
    _print_console_line(
        "CODE_REVIEW_WORK_PACKAGE_SUPPLY_CHAIN "
        f"status={'ready' if gates else 'not_evaluated'} "
        f"package_rank={package.package_rank} package_id={package.package_id} "
        f"observations={len(observations)} "
        f"relations={len(relations)} "
        f"gates={json.dumps([gate.gate for gate in gates], ensure_ascii=True)} "
        "mutation_authority=0"
    )


def _emit_code_review_work_package_summary(package: CodeReviewWorkPackage) -> None:
    _print_console_line(
        "CODE_REVIEW_WORK_PACKAGE status=ready "
        f"package_rank={package.package_rank} risk={package.change_risk} "
        f"kind={getattr(package, 'package_kind', 'hotspot_maintenance')} "
        f"members={len(package.members)} "
        f"members_truncated={int(package.members_truncated)} "
        f"confidence={package.confidence} "
        f"primary={json.dumps(package.primary_symbol, ensure_ascii=True)} "
        f"human_confirmation="
        f"{int(bool(getattr(package, 'requires_human_confirmation', False)))} "
        f"mutation_authority="
        f"{int(bool(getattr(package, 'mutation_authority', False)))} "
        f"package_id={package.package_id}"
    )


def _emit_code_review_work_package_unused(package: CodeReviewWorkPackage) -> None:
    unused_candidates = getattr(package, "unused_candidates", ())
    for candidate in unused_candidates[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_REVIEW_WORK_PACKAGE_UNUSED "
            f"package_rank={package.package_rank} package_id={package.package_id} "
            f"candidate_id={candidate.candidate_id} state={candidate.state} "
            f"path={json.dumps(candidate.relative_path, ensure_ascii=True)} "
            f"symbol={json.dumps(candidate.symbol, ensure_ascii=True)} "
            f"reasons={json.dumps(candidate.reasons, ensure_ascii=True)}"
        )


def _emit_code_review_work_package_architecture(
    package: CodeReviewWorkPackage,
) -> None:
    import_chains = package.import_chains[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT]
    affected_contracts = package.affected_architecture_contracts[
        :_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT
    ]
    architecture_gates = tuple(
        gate for gate in package.acceptance_gates if gate in _CODE_ARCHITECTURE_ACCEPTANCE_GATES
    )
    _print_console_line(
        "CODE_REVIEW_WORK_PACKAGE_ARCHITECTURE status=ready "
        f"package_rank={package.package_rank} package_id={package.package_id} "
        f"primary_module={json.dumps(package.primary_module, ensure_ascii=True)} "
        f"import_chains={json.dumps(import_chains, ensure_ascii=True)} "
        f"import_chains_truncated="
        f"{int(len(package.import_chains) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)} "
        f"affected_architecture_contracts="
        f"{json.dumps(affected_contracts, ensure_ascii=True)} "
        f"affected_contracts_truncated="
        f"{int(len(package.affected_architecture_contracts) > _CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT)} "
        f"architecture_acceptance_gates={json.dumps(architecture_gates, ensure_ascii=True)}"
    )


def _bounded_code_review_sequence(value: object) -> list[object] | tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return value[:_CODE_CLI_COVERAGE_EXAMPLE_LIMIT]
    return []


def _emit_code_review_work_package_coverage(
    package: CodeReviewWorkPackage,
) -> None:
    projection = package.test_coverage
    if projection is None:
        _print_console_line(
            "CODE_REVIEW_WORK_PACKAGE_COVERAGE status=not_evaluated "
            f"package_rank={package.package_rank} package_id={package.package_id} "
            'reason="coverage_projection_missing"'
        )
        return
    payload = asdict(projection)
    coverage_scope = (
        {} if package.test_coverage_scope is None else asdict(package.test_coverage_scope)
    )
    coverage_gate = payload.get("gate")
    bounded_gate = coverage_gate if isinstance(coverage_gate, dict) else {}
    _print_console_line(
        "CODE_REVIEW_WORK_PACKAGE_COVERAGE "
        f"status={payload.get('status')} "
        f"package_rank={package.package_rank} package_id={package.package_id} "
        f"subject={json.dumps(payload.get('primary_symbol'), ensure_ascii=True)} "
        f"tests={json.dumps(_bounded_code_review_sequence(payload.get('executing_tests')), ensure_ascii=True)} "
        f"relations={json.dumps(_bounded_code_review_sequence(payload.get('relation_ids')), ensure_ascii=True)} "
        f"missing_lines={json.dumps(_bounded_code_review_sequence(coverage_scope.get('missing_line_ranges')), ensure_ascii=True)} "
        f"missing_branches={json.dumps(_bounded_code_review_sequence(coverage_scope.get('missing_branch_arcs')), ensure_ascii=True)} "
        f"details_truncated={int(bool(coverage_scope.get('missing_line_ranges_truncated')) or bool(coverage_scope.get('missing_branch_arcs_truncated')))} "
        f"gate={bounded_gate.get('status')} "
        f"reason={json.dumps(bounded_gate.get('reason'), ensure_ascii=True)}"
    )


def _emit_code_review_work_package(package: CodeReviewWorkPackage) -> None:
    _emit_code_review_work_package_supply_chain(package)
    _emit_code_review_work_package_summary(package)
    _emit_code_review_work_package_unused(package)
    _emit_code_review_work_package_architecture(package)
    _emit_code_review_work_package_coverage(package)


def _emit_code_review_ready(result: CodeReviewResult) -> int:
    complete_evidence = _complete_code_review_evidence(result)
    if complete_evidence is None:
        return _error("code-review", RuntimeError("ready result is incomplete"))
    snapshot, coverage, digest = complete_evidence
    _emit_code_review_header(result, snapshot, coverage, digest)
    _emit_code_review_external_evidence(result)
    _emit_code_review_architecture_result(result)
    _emit_code_review_test_coverage_result(result)
    _emit_code_review_unused_result(result)
    _emit_code_review_supply_chain_result(result)
    _emit_code_review_abstention_statuses(result)
    _emit_code_review_question_summary(result)
    for package in result.work_packages:
        _emit_code_review_work_package(package)
    _emit_code_review_ranked_evidence(result)
    return 0


def run_code_review(args: argparse.Namespace) -> int:
    """Rank confirmed Python hotspots in the published self-analysis snapshot."""

    from neocortex.code.code_review import review_code_state

    try:
        result = review_code_state(
            args.state_directory,
            limit=args.code_review_limit,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-review", exc)
    if args.code_json:
        return _emit_code_review_json(result)
    if result.status != "ready":
        return _emit_code_review_abstention(result)
    return _emit_code_review_ready(result)


def run_code_validate_change(args: argparse.Namespace) -> int:
    """Run the sole local Linux validation path for source changes."""

    runtime_window = None
    try:
        from neocortex.runtime.config.app_paths import source_repository_directory
        from neocortex.code.code_validation_resources import (
            code_validation_runtime_window,
            current_code_validation_resource_admission,
            inside_code_validation_resource_boundary,
            run_code_validation_in_resource_boundary,
        )

        if not inside_code_validation_resource_boundary():
            command: list[str | os.PathLike[str]] = [
                sys.executable,
                "-m",
                "neocortex",
                "--code-validate-change",
                "--code-validation-baseline",
                str(args.code_validation_baseline),
                "--code-validation-max-tests",
                str(args.code_validation_max_tests),
                "--code-validation-time-budget-seconds",
                str(args.code_validation_time_budget_seconds),
                "--state-directory",
                str(args.state_directory),
            ]
            if args.code_json:
                command.append("--code-json")
            return run_code_validation_in_resource_boundary(
                command,
                cwd=source_repository_directory(),
                json_output=bool(args.code_json),
            )

        from neocortex.code.code_change_validation import validate_code_change
        from neocortex.code.code_validation_receipts import publish_code_validation_receipt

        admission = current_code_validation_resource_admission()
        if admission is None:
            raise RuntimeError("code_validation_resource_admission_disappeared")
        runtime_window = code_validation_runtime_window(admission)
        result = validate_code_change(
            baseline=args.code_validation_baseline,
            max_tests=args.code_validation_max_tests,
            time_budget_seconds=args.code_validation_time_budget_seconds,
            progress=lambda message: _print_console_line(
                f"CODE_CHANGE_VALIDATION_PROGRESS {message}",
                file=sys.stderr,
            ),
            runtime_window=runtime_window,
        )
        runtime_replay_passed = any(
            gate.gate_id == "trusted_deep_replay" and gate.status == "passed"
            for gate in result.gates
        )
        receipt = (
            publish_code_validation_receipt(result.as_payload())
            if result.status == "passed" and runtime_replay_passed
            else None
        )
    except KeyboardInterrupt:
        if runtime_window is None:
            raise
        reason = (
            "code_validation_overall_runtime_expired"
            if time.monotonic_ns() >= runtime_window.hard_deadline_monotonic_ns
            else "code_validation_resource_boundary_interrupted"
        )
        return _error("code-validate-change", RuntimeError(reason))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _error("code-validate-change", exc)
    if args.code_json:
        _emit(result.as_payload(), json_output=True)
    else:
        _print_console_line(
            "CODE_CHANGE_VALIDATION "
            f"status={result.status} changed={len(result.git.changed_paths)} "
            f"tests={len(result.selection.selectors)} "
            f"selection={result.selection.strategy} gates={len(result.gates)} "
            f"digest={result.digest} source_unchanged={int(result.source_unchanged)}"
        )
        if result.reason is not None:
            _print_console_line(f"CODE_CHANGE_VALIDATION_REASON {result.reason}")
        for gate in result.gates:
            _print_console_line(
                "CODE_CHANGE_VALIDATION_GATE "
                f"id={gate.gate_id} status={gate.status} reason={gate.reason} "
                f"duration_ms={gate.duration_ms}"
            )
        if result.executable_experiments:
            _print_console_line(
                "CODE_CHANGE_VALIDATION_EXPERIMENTS "
                + json.dumps(result.executable_experiments, ensure_ascii=True)
            )
    if receipt is not None:
        _print_console_line(
            "CODE_CHANGE_VALIDATION_RECEIPT "
            f"path={receipt.receipt_path} digest={receipt.receipt_digest} "
            f"head={receipt.head_sha}",
            file=sys.stderr if args.code_json else sys.stdout,
        )
    return 0 if result.status == "passed" else 2


def run_code_experiment(args: argparse.Namespace) -> int:
    """Execute one proposal and append its non-mutating Code evidence receipt."""

    try:
        from neocortex.code.code_experiment_executor import execute_code_experiment
        from neocortex.code.code_experiment_store import (
            code_review_digest_identity,
            record_code_experiment_receipt,
        )
        from neocortex.code.code_review import review_code_state

        result = review_code_state(args.state_directory, limit=50)
        if result.status != "ready" or result.snapshot is None or result.experiment_plan is None:
            raise ValueError(f"code review cannot plan experiments: {result.reason}")
        proposal_id = str(args.code_experiment_run)
        matches = tuple(
            proposal
            for proposal in result.experiment_plan.proposals
            if proposal.proposal_id == proposal_id
        )
        if len(matches) != 1:
            raise ValueError("experiment proposal is absent from the current code-review plan")
        proposal = matches[0]
        if proposal.planning_status != "planned" or proposal.runner_kind == "none":
            raise ValueError("experiment proposal has no executable allow-listed runner")
        source_root = Path(result.snapshot.root).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="neocortex-code-experiment-") as temporary:
            receipt = execute_code_experiment(
                proposal,
                source_root=source_root,
                code_database_path=_state_path(args),
                scratch_root=Path(temporary),
                source_version=result.snapshot.processing_signature,
                expected_source_root=source_root,
            )
        if result.digest is None:
            raise ValueError("code review has no digest for experiment receipt provenance")
        stored_receipt = record_code_experiment_receipt(
            _state_path(args),
            receipt,
            proposal,
            analysis_run_id=result.snapshot.analysis_run_id,
            processing_signature=result.snapshot.processing_signature,
            review_digest=code_review_digest_identity(result.digest),
        )
    except (ImportError, OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        return _error("code-experiment", exc)
    if args.code_json:
        _emit(stored_receipt.as_payload(), json_output=True)
    else:
        _print_console_line(
            "CODE_EXPERIMENT_RECEIPT "
            f"status={receipt.status} receipt_id={receipt.receipt_id} "
            f"proposal_id={receipt.proposal_id} passed={receipt.passed} "
            f"failed={receipt.failed} skipped={receipt.skipped} "
            f"duration_ms={receipt.duration_ms} "
            f"persisted=1 recorded_ns={stored_receipt.recorded_ns} "
            f"code_database_unchanged={int(receipt.code_database_unchanged)} "
            f"authority={receipt.authority} "
            f"mutation_authority={int(receipt.mutation_authority)}"
        )
        if receipt.reason is not None:
            _print_console_line(f"CODE_EXPERIMENT_REASON {receipt.reason}")
        for limitation in receipt.limitations:
            _print_console_line(f"CODE_EXPERIMENT_LIMITATION {limitation}")
    return 0 if receipt.status == "passed" else 2


def _emit_code_publication_unused(analysis: CodeUnusedAnalysisDelta) -> None:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_UNUSED status={analysis.status} "
        f"common={analysis.common} added={analysis.added} removed={analysis.removed} "
        f"state_changes={analysis.state_changes} "
        f"high_consensus_added={analysis.high_consensus_added} "
        f"high_consensus_resolved={analysis.high_consensus_resolved} "
        f"gate={analysis.gate} "
        f"gate_reason={json.dumps(analysis.gate_reason, ensure_ascii=True)} "
        f"reason={json.dumps(analysis.reason, ensure_ascii=True)}"
    )
    for change in analysis.state_change_examples[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_UNUSED_STATE "
            f"id={change.candidate_id} "
            f"baseline={change.baseline_state} current={change.current_state} "
            f"path={json.dumps(change.relative_path, ensure_ascii=True)} "
            f"symbol={json.dumps(change.symbol, ensure_ascii=True)}"
        )
    for candidate in analysis.added_examples[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_UNUSED_ADDED "
            f"id={candidate.candidate_id} state={candidate.state} "
            f"path={json.dumps(candidate.relative_path, ensure_ascii=True)} "
            f"symbol={json.dumps(candidate.symbol, ensure_ascii=True)}"
        )
    for candidate in analysis.removed_examples[:_CODE_CLI_UNUSED_EXAMPLE_LIMIT]:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_UNUSED_REMOVED "
            f"id={candidate.candidate_id} state={candidate.state} "
            f"path={json.dumps(candidate.relative_path, ensure_ascii=True)} "
            f"symbol={json.dumps(candidate.symbol, ensure_ascii=True)}"
        )


def _emit_code_publication_supply_chain(analysis: CodeSupplyChainDelta) -> None:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_SUPPLY_CHAIN status={analysis.status} "
        f"baseline_status={analysis.baseline_status} current_status={analysis.current_status} "
        f"common_visible={analysis.common_visible} added_visible={analysis.added_visible} "
        f"resolved_visible={analysis.resolved_visible} changed_visible={analysis.changed_visible} "
        f"baseline_truncated={int(analysis.baseline_observations_truncated)} "
        f"current_truncated={int(analysis.current_observations_truncated)} "
        f"reason={json.dumps(analysis.reason, ensure_ascii=True)}"
    )
    for category in analysis.categories:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_SUPPLY_CHAIN_CATEGORY id={category.category} "
            f"baseline={category.baseline} current={category.current} delta={category.delta:+d}"
        )
    for provider in analysis.providers:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_SUPPLY_CHAIN_PROVIDER id={provider.provider_id} "
            f"baseline_status={provider.baseline_status} current_status={provider.current_status} "
            f"baseline_freshness={provider.baseline_freshness} "
            f"current_freshness={provider.current_freshness} "
            f"findings_delta={provider.findings_delta:+d} "
            f"metrics_delta={provider.metrics_delta:+d} "
            f"relations_delta={provider.relations_delta:+d}"
        )
    for gate in analysis.gates:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_SUPPLY_CHAIN_GATE id={gate.gate} "
            f"provider={gate.provider_id} baseline={gate.baseline_status} "
            f"current={gate.current_status} evidence_delta={gate.evidence_count_delta:+d} "
            f"reason={json.dumps(gate.current_reason, ensure_ascii=True)}"
        )
    for example in analysis.examples[:_CODE_CLI_ARCHITECTURE_EXAMPLE_LIMIT]:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_SUPPLY_CHAIN_OBSERVATION change={example.change} "
            f"provider={example.provider_id} category={example.category} "
            f"kind={example.evidence_kind} code={example.code} id={example.observation_id}"
        )


def _emit_code_publication_engineering(
    analysis: CodeEngineeringAnalyticsDelta,
) -> None:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_ENGINEERING status={analysis.status} "
        f"baseline_mutation_score={analysis.baseline_mutation_score} "
        f"current_mutation_score={analysis.current_mutation_score} "
        f"mutation_score_delta={analysis.mutation_score_delta} "
        f"reason={json.dumps(analysis.reason, ensure_ascii=True)}"
    )
    for gate in analysis.gates:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_ENGINEERING_GATE id={gate.gate} "
            f"status={gate.status} reason={json.dumps(gate.reason, ensure_ascii=True)}"
        )


@dataclass(frozen=True, slots=True)
class _CodePublicationReadyEvidence:
    baseline: CodePublicationSnapshot
    current: CodePublicationSnapshot
    calls: CodeCallResolutionDelta
    hotspots: CodeHotspotDelta
    probable_dead_delta: int
    external_evidence: CodeExternalEvidenceDelta
    architecture: CodeArchitectureDelta
    test_coverage: CoverageComparison
    digest: CodePublicationDiffDigest


def _emit_code_publication_json(result: CodePublicationDiffResult) -> int:
    _emit(result.as_payload(), json_output=True)
    return 0 if result.status == "ready" else 2


def _emit_code_publication_abstention(result: CodePublicationDiffResult) -> int:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF status=abstained reason={result.reason} "
        f"baseline={json.dumps(result.baseline_database, ensure_ascii=True)} "
        f"current={json.dumps(result.current_database, ensure_ascii=True)}"
    )
    return 2


def _complete_code_publication_evidence(
    result: CodePublicationDiffResult,
) -> _CodePublicationReadyEvidence | None:
    baseline = result.baseline
    current = result.current
    calls = result.calls
    hotspots = result.hotspots
    probable_dead_delta = result.probable_dead_delta
    external_evidence = result.external_evidence
    architecture = result.architecture
    test_coverage = result.test_coverage
    digest = result.digest
    if (
        baseline is None
        or current is None
        or calls is None
        or hotspots is None
        or probable_dead_delta is None
        or external_evidence is None
        or architecture is None
        or test_coverage is None
        or digest is None
    ):
        return None
    return _CodePublicationReadyEvidence(
        baseline=baseline,
        current=current,
        calls=calls,
        hotspots=hotspots,
        probable_dead_delta=probable_dead_delta,
        external_evidence=external_evidence,
        architecture=architecture,
        test_coverage=test_coverage,
        digest=digest,
    )


def _emit_code_publication_summary(
    evidence: _CodePublicationReadyEvidence,
) -> None:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF status=ready digest={evidence.digest.xxh3_128} "
        f"baseline_calls={evidence.baseline.resolved_call_edges}/"
        f"{evidence.baseline.call_edges} current_calls="
        f"{evidence.current.resolved_call_edges}/{evidence.current.call_edges}"
    )
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_CALLS common={evidence.calls.common_call_sites} "
        f"baseline_only={evidence.calls.baseline_only_call_sites} "
        f"current_only={evidence.calls.current_only_call_sites} "
        f"newly_resolved={evidence.calls.newly_resolved} "
        f"corrected={evidence.calls.corrected} lost={evidence.calls.lost}"
    )
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_HOTSPOTS common={evidence.hotspots.common} "
        f"added={evidence.hotspots.added} removed={evidence.hotspots.removed} "
        f"changed_evidence={evidence.hotspots.changed_evidence} "
        f"probable_dead_delta={evidence.probable_dead_delta:+d}"
    )
    _print_console_line(
        "CODE_PUBLICATION_DIFF_EXTERNAL provider=ruff "
        f"status={evidence.external_evidence.status} "
        f"common={evidence.external_evidence.common} "
        f"added={evidence.external_evidence.added} "
        f"resolved={evidence.external_evidence.resolved} "
        f"gate={evidence.external_evidence.gate}"
    )


def _emit_code_publication_providers(result: CodePublicationDiffResult) -> None:
    _print_console_line(
        f"CODE_PUBLICATION_DIFF_PROVIDERS profile={result.analysis_profile} "
        f"verdict={result.verdict}"
    )
    for provider in result.providers:
        _print_console_line(
            f"CODE_PUBLICATION_DIFF_PROVIDER id={provider.provider_id} "
            f"status={provider.status} common={provider.common} "
            f"added={provider.added} resolved={provider.resolved} "
            f"relocated={provider.relocated} gate={provider.gate}"
        )


def _emit_code_publication_coverage(coverage: CoverageComparison) -> None:
    coverage_delta = asdict(coverage)
    _print_console_line(
        "CODE_PUBLICATION_DIFF_COVERAGE "
        f"status={coverage_delta.get('status')} "
        f"line_delta={coverage_delta.get('line_coverage_percent_delta')} "
        f"branch_delta={coverage_delta.get('branch_coverage_percent_delta')} "
        f"covered_lines_delta={coverage_delta.get('covered_lines_delta')} "
        f"missing_lines_delta={coverage_delta.get('missing_lines_delta')} "
        f"covered_branches_delta={coverage_delta.get('covered_branch_exits_delta')} "
        f"missing_branches_delta={coverage_delta.get('missing_branch_exits_delta')} "
        f"reason={json.dumps(coverage_delta.get('reason'), ensure_ascii=True)}"
    )
    coverage_gates = coverage_delta.get("gates")
    if isinstance(coverage_gates, (list, tuple)):
        for gate in coverage_gates[:_CODE_CLI_COVERAGE_EXAMPLE_LIMIT]:
            if not isinstance(gate, dict):
                continue
            _print_console_line(
                f"CODE_PUBLICATION_DIFF_COVERAGE_GATE id={gate.get('gate')} "
                f"status={gate.get('status')} "
                f"reason={json.dumps(gate.get('reason'), ensure_ascii=True)}"
            )


def _emit_code_publication_unused_result(result: CodePublicationDiffResult) -> None:
    unused_delta = getattr(result, "unused_analysis", None)
    if unused_delta is None:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_UNUSED status=not_evaluated "
            'gate=not_evaluated reason="unused_delta_missing"'
        )
        return
    _emit_code_publication_unused(unused_delta)


def _emit_code_publication_supply_chain_result(
    result: CodePublicationDiffResult,
) -> None:
    supply_chain_delta = getattr(result, "supply_chain", None)
    if supply_chain_delta is None:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_SUPPLY_CHAIN status=not_evaluated "
            'reason="supply_chain_delta_missing"'
        )
        return
    _emit_code_publication_supply_chain(supply_chain_delta)


def _emit_code_publication_engineering_result(
    result: CodePublicationDiffResult,
) -> None:
    engineering_delta = getattr(result, "engineering_analytics", None)
    if engineering_delta is None:
        _print_console_line(
            "CODE_PUBLICATION_DIFF_ENGINEERING status=not_comparable "
            'reason="engineering_analytics_delta_missing"'
        )
        return
    _emit_code_publication_engineering(engineering_delta)


def _emit_code_publication_ready(result: CodePublicationDiffResult) -> int:
    evidence = _complete_code_publication_evidence(result)
    if evidence is None:
        return _error(
            "code-publication-diff",
            RuntimeError("ready result is incomplete"),
        )
    _emit_code_publication_summary(evidence)
    _emit_code_publication_providers(result)
    _emit_code_publication_architecture(evidence.architecture)
    _emit_code_publication_coverage(evidence.test_coverage)
    _emit_code_publication_unused_result(result)
    _emit_code_publication_supply_chain_result(result)
    _emit_code_publication_engineering_result(result)
    for limitation in result.limitations:
        _print_console_line(f"CODE_PUBLICATION_DIFF_LIMITATION {limitation}")
    return 0


def run_code_publication_diff(args: argparse.Namespace) -> int:
    """Compare two completed Code publications without mutating either state."""

    from neocortex.code.code_publication_diff import compare_code_publications

    try:
        result = compare_code_publications(
            Path(args.code_publication_diff),
            args.state_directory,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-publication-diff", exc)
    if args.code_json:
        return _emit_code_publication_json(result)
    if result.status != "ready":
        return _emit_code_publication_abstention(result)
    return _emit_code_publication_ready(result)


def run_code_doctor(args: argparse.Namespace) -> int:
    """Validate schema, FTS and optional tools without loading heavy analyzers."""

    from neocortex.code.code_analyzers import builtin_analyzer_registry
    from neocortex.code.code_external_evidence import RuffEvidenceProvider
    from neocortex.code.code_schema import code_database, validate_code_schema
    from neocortex.code.external_evidence_providers import provider_tool_versions

    path = _state_path(args)
    ruff_version = RuffEvidenceProvider.tool_version()
    provider_versions = provider_tool_versions()
    report: dict[str, object] = {
        "kind": "code-doctor",
        "database": str(path),
        "exists": path.is_file(),
        "analyzers": builtin_analyzer_registry().status(),
        "tools": {
            name: shutil.which(name)
            for name in ("cargo", "rustc", "rust-analyzer", "mypy", "node", "pyright")
        },
        "external_evidence": {
            "provider": "ruff",
            "available": ruff_version is not None,
            "version": ruff_version,
            "runtime": sys.executable,
            "resolution": "runtime-distribution",
        },
        "external_evidence_providers": {
            provider_id: {
                "available": version is not None,
                "version": version,
                "authority": "advisory",
                "mutation_authority": False,
            }
            for provider_id, version in provider_versions.items()
        },
    }
    if path.is_file():
        try:
            with code_database(path, readonly=True) as connection:
                validate_code_schema(connection)
                connection.execute(
                    "SELECT rowid FROM code_fts WHERE code_fts MATCH 'neocortex' LIMIT 1"
                ).fetchall()
                report["schema"] = "ok"
                report["foreign_key_violations"] = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            report["schema"] = "error"
            report["error"] = f"{type(exc).__name__}: {exc}"
            _emit(report, json_output=True)
            return 2
    else:
        report["schema"] = "not-initialized"
    _emit(report, json_output=True)
    return 0


# endregion [01]


# region [02] Search and reconstruction


def run_code_search(args: argparse.Namespace) -> int:
    from neocortex.code.code_contracts import CodeSearchQuery
    from neocortex.code.code_search import search_code
    from neocortex.code.code_semantic_links import code_semantic_search_availability

    try:
        query = CodeSearchQuery(
            text=args.code_search,
            modes=tuple(args.code_search_mode or ("hybrid",)),
            path=args.code_path,
            language=args.code_language,
            project=args.code_project,
            symbol=args.code_symbol,
            diagnostic=args.code_diagnostic,
            minimum_complexity=args.code_min_complexity,
            limit=args.code_search_limit,
        )
        semantic_requested = any(mode in {"semantic", "hybrid"} for mode in query.modes)
        semantic_availability = (
            code_semantic_search_availability(
                args.state_directory,
                model_cache_override=args.semantic_model_cache,
            )
            if semantic_requested
            else None
        )
        hits = search_code(
            _state_path(args),
            query,
            semantic_model_cache=args.semantic_model_cache,
            semantic_threads=args.semantic_threads,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-search", exc)
    if semantic_availability is not None:
        semantic_payload = {
            "kind": "code-search-channel",
            "channel": "semantic",
            **asdict(semantic_availability),
        }
        if args.code_json:
            _emit(semantic_payload, json_output=True)
        else:
            _print_console_line(
                "CODE_SEARCH_CHANNEL name=semantic "
                f"available={int(semantic_availability.available)} "
                f"reason={semantic_availability.reason} "
                f"generation={semantic_availability.generation_id or '-'} "
                f"current_links={semantic_availability.current_links} "
                f"calibration={semantic_availability.calibration}"
            )
    for hit in hits:
        if args.code_json:
            _emit({"kind": "code-search-hit", **asdict(hit)}, json_output=True)
        else:
            _print_console_line(
                f"CODE_HIT score={hit.score:.6f} matches={','.join(hit.match_types)} "
                f"language={hit.language or '-'} project={hit.project or '-'} "
                f"path={json.dumps(hit.path, ensure_ascii=False)} "
                f"lines={hit.start_line}-{hit.end_line} "
                f"symbol={json.dumps(hit.symbol, ensure_ascii=False)} "
                f"snippet={json.dumps(hit.snippet, ensure_ascii=False)}"
            )
    if (
        semantic_availability is not None
        and query.modes == ("semantic",)
        and not semantic_availability.available
    ):
        return 2
    return 0


def run_code_projects(args: argparse.Namespace) -> int:
    from neocortex.code.code_projects import list_projects

    try:
        projects = list_projects(_state_path(args))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _error("code-projects", exc)
    for project in projects:
        if args.code_json:
            _emit({"kind": "code-project", **asdict(project)}, json_output=True)
        else:
            _print_console_line(
                f"CODE_PROJECT id={project.project_id} name={json.dumps(project.name)} "
                f"ecosystem={project.ecosystem} status={project.status} "
                f"confidence={project.confidence:.3f} current={project.current_files} "
                f"historical={project.historical_files} "
                f"root={json.dumps(project.probable_root, ensure_ascii=False)}"
            )
    return 0


def run_code_reconstruct(args: argparse.Namespace) -> int:
    from neocortex.code.code_projects import reconstruct_project

    project: str | int = args.code_reconstruct
    if str(project).isdigit():
        project = int(project)
    try:
        manifest = reconstruct_project(
            _state_path(args),
            project,
            strategy=args.code_reconstruct_strategy,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError, LookupError) as exc:
        return _error("code-reconstruct", exc)
    if args.code_json:
        _emit({"kind": "code-reconstruction", **asdict(manifest)}, json_output=True)
        return 0
    _print_console_line(
        f"CODE_RECONSTRUCTION project_id={manifest.project_id} "
        f"name={json.dumps(manifest.project_name)} ecosystem={manifest.ecosystem} "
        f"strategy={manifest.strategy} conflicts={len(manifest.conflicts)}"
    )
    for entry in manifest.entries:
        _print_console_line(
            f"CODE_RECONSTRUCTION_ENTRY selected={str(entry.selected).lower()} "
            f"confidence={entry.confidence:.3f} relation={entry.relation} "
            f"proposed={json.dumps(entry.proposed_path)} "
            f"source={json.dumps(entry.source_path, ensure_ascii=False)} "
            f"version={entry.version_id} xxh3_128={entry.xxh3_128} "
            f"conflict={entry.conflict_group or '-'}"
        )
    for conflict in manifest.conflicts:
        _print_console_line(f"CODE_RECONSTRUCTION_CONFLICT {conflict}")
    return 0


# endregion [02]


__all__ = [
    "run_code_doctor",
    "run_code_experiment",
    "run_code_projects",
    "run_code_publication_diff",
    "run_code_query",
    "run_code_reconstruct",
    "run_code_review",
    "run_code_search",
    "run_code_status",
    "run_code_validate_change",
]


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_code')
