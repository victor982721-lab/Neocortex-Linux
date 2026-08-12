"""Synthetic schema-v4 consumer regressions for architecture evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

from _04_Nucleo_Operativo.code_architecture_analysis import (
    CODE_ARCHITECTURE_CENTRALITY_FORMULA,
    CODE_ARCHITECTURE_REACHABILITY_LIMIT,
    read_code_architecture_analysis,
)
from _04_Nucleo_Operativo.code_external_evidence import ExternalEvidencePublication
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderPublication,
    ExternalProviderRelation,
    ExternalRunInput,
    ProviderDescriptor,
    ProviderLimits,
    external_metric_identity,
    external_provider_result_digest,
    external_relation_identity,
)
from _04_Nucleo_Operativo.external_evidence_store import publish_external_provider
from tests.test_external_provider_schema_v4 import _create_current_owner


def _descriptor(provider_id: str) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id,
        f"neocortex.{provider_id}/v1",
        provider_id,
        "trusted-static",
        "trusted-static",
        "project",
        f"external:{provider_id}",
        "fixture-configuration",
        None,
        "fixture-environment",
        f"fixture-comparability:{provider_id}",
        "bounded-fixture",
        "project_wide",
        "exact-input",
        ProviderLimits(1.0, 1_000_000, 1_000_000, 1_000_000, 100),
    )


def _metric(
    provider_id: str,
    subject_kind: str,
    subject_key: str,
    metric_name: str,
    value: float,
    *,
    metadata: dict[str, object] | None = None,
) -> ExternalProviderMetric:
    return ExternalProviderMetric(
        external_metric_identity(
            provider_id,
            subject_kind=subject_kind,  # type: ignore[arg-type]
            subject_key=subject_key,
            category="architecture",
            metric_name=metric_name,
            unit="count",
        ),
        subject_kind,  # type: ignore[arg-type]
        subject_key,
        "architecture",
        metric_name,
        value,
        "count",
        metadata={} if metadata is None else metadata,
    )


def _relation(provider_id: str, source: str, target: str) -> ExternalProviderRelation:
    return ExternalProviderRelation(
        external_relation_identity(
            provider_id,
            relation_kind="module_import",
            source_kind="module",
            source_key=source,
            target_kind="module",
            target_key=target,
        ),
        "module_import",
        "module",
        source,
        "module",
        target,
        confidence=1.0,
        metadata={"oracle": provider_id},
    )


def _publication(provider_id: str) -> ExternalProviderPublication:
    metrics: tuple[ExternalProviderMetric, ...] = ()
    relations: tuple[ExternalProviderRelation, ...] = ()
    findings: tuple[ExternalProviderFinding, ...] = ()
    if provider_id == "ruff-analyze-imports":
        relations = (
            _relation(provider_id, "pkg.a", "pkg.b"),
            _relation(provider_id, "pkg.b", "pkg.a"),
            _relation(provider_id, "pkg.b", "pkg.c"),
            _relation(provider_id, "pkg.c", "other.api"),
            _relation(provider_id, "other.api", "third.worker"),
        )
    elif provider_id == "grimp-architecture":
        relations = (
            _relation(provider_id, "pkg.a", "pkg.b"),
            _relation(provider_id, "pkg.b", "pkg.a"),
            _relation(provider_id, "pkg.b", "pkg.d"),
            _relation(provider_id, "pkg.c", "other.api"),
            _relation(provider_id, "other.api", "third.worker"),
        )
        metrics = (
            _metric(
                provider_id,
                "contract",
                "layers",
                "architecture_contract_evaluated",
                1,
            ),
            _metric(
                provider_id,
                "contract",
                "layers",
                "architecture_contract_violations",
                1,
            ),
        )
        findings = (
            ExternalProviderFinding(
                "layers:a-b",
                1,
                "a.py",
                "architecture",
                "layers",
                "error",
                "fixture architecture violation",
                True,
                1.0,
                None,
                "advisory",
                1,
                0,
                1,
                1,
                metadata={
                    "contract_schema": "neocortex.architecture-contract/v1",
                    "importer_module": "pkg.a",
                    "imported_module": "pkg.b",
                    "import_chain": ["pkg.a", "pkg.b"],
                },
            ),
        )
    else:
        metrics = (
            _metric(
                provider_id,
                "module",
                "pkg.a",
                "module_cognitive_complexity_total",
                10,
            ),
            _metric(
                provider_id,
                "module",
                "pkg.a",
                "module_cognitive_complexity_max",
                7,
            ),
            _metric(
                provider_id,
                "module",
                "pkg.b",
                "module_cognitive_complexity_total",
                20,
            ),
            _metric(
                provider_id,
                "module",
                "pkg.b",
                "module_cognitive_complexity_max",
                12,
            ),
            _metric(
                provider_id,
                "module",
                "solo.node",
                "module_cognitive_complexity_total",
                1,
            ),
            _metric(
                provider_id,
                "module",
                "solo.node",
                "module_cognitive_complexity_max",
                1,
            ),
            _metric(
                provider_id,
                "symbol",
                "pkg.a:run:1:2",
                "cognitive_complexity",
                7,
                metadata={"module": "pkg.a"},
            ),
        )
    digest = external_provider_result_digest(findings, metrics, relations)
    return ExternalProviderPublication(
        _descriptor(provider_id),
        ExternalEvidencePublication(
            provider_id,
            "1.0",
            "fixture-configuration",
            "completed",
            1,
            2,
            {"execution": "full"},
        ),
        "C:/fixture",
        "fixture-root",
        "fixture-input",
        (ExternalRunInput(1, "input:a.py", "a.py", True, True, None, 4, "digest"),),
        findings,
        {
            "eligible_files": 1,
            "covered_files": 1,
            "findings": len(findings),
            "metrics": len(metrics),
            "relations": len(relations),
            "comparable": 0,
        },
        True,
        digest,
        f"publication:{provider_id}:full",
        metrics=metrics,
        relations=relations,
    )


def _replay(
    source: ExternalProviderPublication,
    source_tool_run_id: int,
) -> ExternalProviderPublication:
    return ExternalProviderPublication(
        source.descriptor,
        replace(
            source.publication,
            status="skipped",
            started_ns=3,
            completed_ns=4,
            provenance={"execution": "cache_replay"},
        ),
        source.observed_root,
        source.root_identity,
        source.input_signature,
        source.inputs,
        (),
        {
            "eligible_files": 1,
            "covered_files": 1,
            "files_verified": 1,
            "bytes_verified": 4,
            "findings": len(source.findings),
            "metrics": len(source.metrics),
            "relations": len(source.relations),
            "comparable": 1,
            "added": 0,
            "resolved": 0,
        },
        True,
        source.result_digest,
        f"publication:{source.descriptor.provider_id}:replay",
        source_tool_run_id,
        "verification:fixture",
    )


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "code.sqlite3"
    _create_current_owner(database, 1, 2)
    publications = tuple(
        _publication(provider)
        for provider in (
            "ruff-analyze-imports",
            "grimp-architecture",
            "complexipy-cognitive",
        )
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        source_ids = tuple(
            publish_external_provider(connection, 1, publication) for publication in publications
        )
        for publication, source_id in zip(publications, source_ids, strict=True):
            publish_external_provider(connection, 2, _replay(publication, source_id))
        connection.commit()
    finally:
        connection.close()
    return database


def test_architecture_analysis_is_ready_and_replay_exact(tmp_path: Path) -> None:
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        baseline = read_code_architecture_analysis(connection, 1, database="fixture")
        replay = read_code_architecture_analysis(connection, 2, database="fixture")
    finally:
        connection.close()

    assert baseline.status == replay.status == "ready"
    assert baseline.gate == replay.gate == "observed"
    gates = {item.gate: item.status for item in baseline.gates}
    assert gates == {
        "import_graph_consensus": "failed",
        "architecture_contracts": "failed",
        "module_complexity_displacement": "not_evaluated",
    }
    assert baseline.digest_payload() == replay.digest_payload()
    assert {item.comparison for item in baseline.imports} == {
        "both",
        "ruff_only",
        "grimp_only",
    }
    assert {item.modules for item in baseline.cycles} == {("pkg.a", "pkg.b")}
    assert baseline.contracts[0].contract_id == "layers"
    assert baseline.contracts[0].status == "failed"
    assert baseline.contracts[0].import_chains == (("pkg.a", "pkg.b"),)
    modules = {item.module_id: item for item in baseline.modules}
    assert modules["pkg.a"].cognitive_complexity_total == 10
    assert modules["pkg.a"].fan_in == 1
    assert modules["pkg.b"].fan_out == 3
    assert modules["pkg.a"].path_namespace_id == "pkg"
    assert modules["pkg.a"].dependency_reach == 5
    assert modules["pkg.a"].dependency_reach_truncated is False
    assert modules["pkg.a"].dependency_path_namespace_ids == ("other", "third")
    assert modules["third.worker"].blast_radius == 4
    assert modules["third.worker"].consumer_path_namespace_ids == ("other", "pkg")
    assert modules["pkg.b"].directed_degree_centrality == 0.333333333333
    assert modules["pkg.c"].cross_path_namespace_fan_out == 1
    assert modules["other.api"].cross_path_namespace_fan_in == 1
    assert modules["solo.node"].dependency_reach == 0
    assert modules["solo.node"].blast_radius == 0
    assert modules["solo.node"].directed_degree_centrality == 0.0
    payload_modules = {
        item["module_id"]: item
        for item in cast(list[dict[str, object]], baseline.as_payload()["modules"])
    }
    assert payload_modules["pkg.a"]["dependency_reach"] == 5
    assert payload_modules["pkg.a"]["path_namespace_id"] == "pkg"
    assert (
        "directed_degree_centrality_formula:" + CODE_ARCHITECTURE_CENTRALITY_FORMULA
        in baseline.limitations
    )
    assert (
        f"transitive_reach_is_exact_until_module_limit:{CODE_ARCHITECTURE_REACHABILITY_LIMIT}"
        in baseline.limitations
    )
    assert baseline.symbols[0].symbol_id == "pkg.a:run:1:2"
    assert "pkg.a:run:1:2" not in modules
    assert all(item.execution == "cache_replay" for item in replay.providers)
    assert all(item.source_tool_run_id in {1, 2, 3} for item in replay.providers)


def test_architecture_reachability_limit_is_deterministic_and_explicit(tmp_path: Path) -> None:
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        first = read_code_architecture_analysis(
            connection,
            1,
            database="fixture",
            reachability_limit=2,
        )
        second = read_code_architecture_analysis(
            connection,
            1,
            database="fixture",
            reachability_limit=2,
        )
    finally:
        connection.close()

    assert first.digest_payload() == second.digest_payload()
    modules = {item.module_id: item for item in first.modules}
    assert modules["pkg.a"].dependency_reach == 2
    assert modules["pkg.a"].dependency_reach_truncated is True
    assert modules["third.worker"].blast_radius == 2
    assert modules["third.worker"].blast_radius_truncated is True
    assert modules["solo.node"].dependency_reach_truncated is False
    assert modules["solo.node"].blast_radius_truncated is False
    assert "transitive_reach_is_exact_until_module_limit:2" in first.limitations


def test_architecture_analysis_abstains_when_provider_is_missing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("DELETE FROM external_tool_runs WHERE tool_run_id IN (2,3,5,6)")
        analysis = read_code_architecture_analysis(connection, 1, database="fixture")
    finally:
        connection.close()

    assert analysis.status == "abstained"
    assert analysis.gate == "abstained"
    assert analysis.reason is not None
    assert "required_provider_not_ready" in analysis.reason
    assert {item.status for item in analysis.gates} == {"not_evaluated"}
    assert {item.provider_id for item in analysis.providers if item.status == "abstained"} == {
        "complexipy-cognitive",
        "grimp-architecture",
    }
