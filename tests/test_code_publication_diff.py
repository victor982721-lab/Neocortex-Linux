"""Read-only publication comparison over isolated completed Code states."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from _04_Nucleo_Operativo import code_schema as code_schema_module
from _04_Nucleo_Operativo.code_architecture_analysis import (
    ArchitectureContract,
    ArchitectureCycle,
    ArchitectureImportEdge,
    ArchitectureModule,
    ArchitectureProviderStatus,
    CodeArchitectureAnalysis,
)
from _04_Nucleo_Operativo.code_contracts import (
    DiagnosticRecord,
    DiagnosticSeverity,
    ReferenceRecord,
    SymbolRecord,
)
from _04_Nucleo_Operativo.code_engineering_analytics import (
    CodeEngineeringAnalytics,
    EngineeringGate,
)
from _04_Nucleo_Operativo.code_publication_diff import (
    _architecture_delta,
    _architecture_module_deltas,
    _engineering_delta,
    _provider_deltas,
    _provider_verdict,
    _unused_delta,
    compare_code_publications,
)
from _04_Nucleo_Operativo.code_schema import (
    checkpoint_code_wal,
    initialize_code_state,
    remove_checkpointed_code_sidecars,
)
from _04_Nucleo_Operativo.code_state import CodeState
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderFinding,
    external_finding_identity,
)
from tests.test_code_review import _analysis, _source_range
from tests.test_external_provider_platform import _run as _run_provider_publication
from tests.test_external_provider_platform import _tree as _provider_source_tree


PROCESSING_SIGNATURE = "code-publication-diff-fixture-v1"
TYPE_PROVIDER_ID = "mypy-trusted-project"


def _typed_finding(
    line: int,
    *,
    message: str = "Incompatible return value type",
) -> ExternalProviderFinding:
    identity = external_finding_identity(
        TYPE_PROVIDER_ID,
        relative_path="pkg/checks.py",
        category="typing",
        code="return-value",
        message=message,
        start_line=line,
        start_column=4,
        end_line=line,
        end_column=12,
    )
    return ExternalProviderFinding(
        identity,
        line,
        "pkg/checks.py",
        "typing",
        "return-value",
        "error",
        message,
        True,
        1.0,
        None,
        "advisory",
        line,
        4,
        line,
        12,
        metadata={"location_precision": "range", "reported_severity": "error"},
    )


def _typed_publication(*findings: ExternalProviderFinding) -> SimpleNamespace:
    provider = SimpleNamespace(
        provider_id=TYPE_PROVIDER_ID,
        status="ready",
        reason=None,
        profile="trusted-static",
        provider_schema="neocortex.mypy-trusted-project/v1",
        tool_name="mypy",
        tool_version="2.1.0",
        comparability_signature="mypy-comparable-v1",
    )
    return SimpleNamespace(
        external_evidence_suite=SimpleNamespace(providers=(provider,)),
        provider_finding_ids={
            TYPE_PROVIDER_ID: frozenset(item.portable_finding_id for item in findings)
        },
        provider_findings={TYPE_PROVIDER_ID: findings},
        external_diagnostic_ids=frozenset(),
    )


def _unused_candidate(candidate_id: str, state: str) -> SimpleNamespace:
    return SimpleNamespace(
        candidate_id=candidate_id,
        relative_path=f"pkg/{candidate_id}.py",
        symbol=f"pkg.{candidate_id}",
        state=state,
    )


def _unused_analysis(
    *candidates: SimpleNamespace,
    provider_signature: str = "providers-v1",
) -> SimpleNamespace:
    return SimpleNamespace(
        status="ready",
        reason=None,
        provider_signature=provider_signature,
        calibration_signature="calibration-v1",
        policy_signature="policy-v1",
        evidence_signature="evidence-v1",
        candidates=tuple(candidates),
        gates=(
            SimpleNamespace(gate="calibration_probable_unused_precision", status="passed"),
            SimpleNamespace(gate="holdout_probable_unused_precision", status="passed"),
        ),
    )


def test_typed_provider_diff_classifies_relocations_without_false_churn() -> None:
    baseline = _typed_publication(
        _typed_finding(10),
        _typed_finding(20),
        _typed_finding(30),
    )
    current = _typed_publication(
        _typed_finding(12),
        _typed_finding(22),
        _typed_finding(30),
    )

    deltas = _provider_deltas(baseline, current)  # type: ignore[arg-type]

    assert len(deltas) == 1
    delta = deltas[0]
    assert delta.common == 1
    assert delta.relocated == 2
    assert delta.added == 0
    assert delta.resolved == 0
    assert delta.gate == "passed"
    assert _provider_verdict(deltas) == "equivalent_under_observed_metrics"
    assert [item.baseline_start_line for item in delta.relocation_examples] == [10, 20]
    assert [item.current_start_line for item in delta.relocation_examples] == [12, 22]
    assert {item.path for item in delta.relocation_examples} == {"pkg/checks.py"}
    assert {item.message for item in delta.relocation_examples} == {
        "Incompatible return value type"
    }


def test_typed_provider_diff_does_not_call_a_message_change_a_relocation() -> None:
    baseline = _typed_publication(_typed_finding(10, message="Expected str"))
    current = _typed_publication(_typed_finding(12, message="Expected bytes"))

    deltas = _provider_deltas(baseline, current)  # type: ignore[arg-type]

    delta = deltas[0]
    assert delta.relocated == 0
    assert delta.relocation_examples == ()
    assert delta.added == 1
    assert delta.resolved == 1
    assert delta.gate == "failed"
    assert _provider_verdict(deltas) == "mixed"


def test_unused_diff_compares_portable_identity_and_state_without_a_magic_score() -> None:
    baseline = _unused_analysis(
        _unused_candidate("candidate-a", "insufficient_evidence"),
        _unused_candidate("candidate-b", "probable_unused_high_consensus"),
    )
    current = _unused_analysis(
        _unused_candidate("candidate-a", "probable_unused_high_consensus"),
        _unused_candidate("candidate-b", "explained_usage"),
        _unused_candidate("candidate-c", "probable_unused_high_consensus"),
    )
    current.evidence_signature = "different-normalized-content"

    delta = _unused_delta(baseline, current)  # type: ignore[arg-type]

    assert delta.status == "ready"
    assert delta.common == 2
    assert delta.added == 1
    assert delta.removed == 0
    assert delta.state_changes == 2
    assert delta.high_consensus_added == 2
    assert delta.high_consensus_resolved == 1
    assert delta.gate == "failed"
    assert delta.gate_reason == "new_probable_unused_high_consensus_candidates"
    assert delta.added_examples[0].candidate_id == "candidate-c"
    assert delta.added_examples[0].relative_path == "pkg/candidate-c.py"
    assert not hasattr(delta, "score")
    assert not hasattr(delta, "defect_probability")


def test_unused_diff_never_passes_when_provider_signatures_are_incomparable() -> None:
    baseline = _unused_analysis(
        _unused_candidate("candidate-a", "insufficient_evidence"),
        provider_signature="providers-v1",
    )
    current = _unused_analysis(
        _unused_candidate("candidate-a", "explained_usage"),
        provider_signature="providers-v2",
    )

    delta = _unused_delta(baseline, current)  # type: ignore[arg-type]

    assert delta.status == "not_evaluated"
    assert delta.reason == "unused_provider_signature_mismatch"
    assert delta.gate == "not_evaluated"
    assert delta.added is None
    assert delta.state_changes is None


def test_unused_diff_never_passes_without_a_calibrated_precision_gate() -> None:
    baseline = _unused_analysis(_unused_candidate("candidate-a", "probable_unused_high_consensus"))
    current = _unused_analysis(_unused_candidate("candidate-a", "probable_unused_high_consensus"))
    current.gates = (
        SimpleNamespace(gate="calibration_probable_unused_precision", status="not_evaluated"),
        SimpleNamespace(gate="holdout_probable_unused_precision", status="passed"),
    )

    delta = _unused_delta(baseline, current)  # type: ignore[arg-type]

    assert delta.status == "ready"
    assert delta.reason is None
    assert delta.gate == "not_evaluated"
    assert delta.gate_reason is not None
    assert delta.gate_reason.startswith("unused_precision_gate_not_evaluated:")


def _diagnostic(
    code: str,
    symbol_range,
    *,
    value: int,
    threshold: int,
) -> DiagnosticRecord:
    return DiagnosticRecord(
        "fixture",
        code,
        DiagnosticSeverity.WARNING,
        f"fixture {code}",
        symbol_range,
        tool_name="fixture-analyzer",
        tool_version="1",
        metadata={"value": value, "threshold": threshold},
    )


def _build_publication(
    state_directory: Path,
    source_root: Path,
    *,
    assignments: dict[str, str | None],
    hotspot: str,
    probable_dead: tuple[str, ...],
) -> Path:
    state_directory.mkdir(parents=True)
    source_root.mkdir(parents=True, exist_ok=True)
    database = state_directory / "code.sqlite3"
    caller_range = _source_range(0, 20)
    first_range = _source_range(1, 230)
    second_range = _source_range(2, 80)
    ranges = {"pkg.first": first_range, "pkg.second": second_range}
    symbols = (
        SymbolRecord(
            "function",
            "caller",
            "pkg.caller",
            "caller()",
            caller_range,
            visibility="public",
            complexity=2,
        ),
        SymbolRecord(
            "function",
            "first",
            "pkg.first",
            "first()",
            first_range,
            visibility="public",
            complexity=30,
        ),
        SymbolRecord(
            "function",
            "second",
            "pkg.second",
            "second()",
            second_range,
            visibility="public",
            complexity=24,
        ),
    )
    references = tuple(
        ReferenceRecord(
            "call",
            name,
            _source_range(index + 3, 1),
            source_qualified_name="pkg.caller",
            target_hint=f"external.{name}",
            confirmed=True,
            confidence=1.0,
            evidence="fixture-call",
        )
        for index, name in enumerate(assignments)
    )
    diagnostics = [
        _diagnostic(
            "high_complexity",
            ranges[hotspot],
            value=30 if hotspot == "pkg.first" else 24,
            threshold=15,
        )
    ]
    if hotspot == "pkg.first":
        diagnostics.append(_diagnostic("long_function", first_range, value=230, threshold=200))
    for symbol in probable_dead:
        diagnostics.append(
            _diagnostic(
                "probable_dead_symbol",
                ranges[symbol],
                value=1,
                threshold=1,
            )
        )

    with CodeState(database) as state:
        analysis_run_id = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        state.store_analysis(
            _analysis(
                source_root / "source.py",
                100,
                symbols=symbols,
                diagnostics=tuple(diagnostics),
                references=references,
            ),
            1,
        )
        state.finalize_graph(1)
        symbol_ids = {
            str(row["qualified_name"]): (int(row["symbol_id"]), int(row["version_id"]))
            for row in state.connection.execute(
                "SELECT symbol_id,version_id,qualified_name FROM symbols"
            )
        }
        for name, target in assignments.items():
            target_ids = None if target is None else symbol_ids[target]
            state.connection.execute(
                "UPDATE code_references SET target_symbol_id=?,target_version_id=? WHERE name=?",
                (
                    None if target_ids is None else target_ids[0],
                    None if target_ids is None else target_ids[1],
                    name,
                ),
            )
        state.complete_run(
            analysis_run_id,
            {
                "candidates": 1,
                "processed": 1,
                "cache_hits": 0,
                "errors": 0,
            },
            partial=False,
            graph_current=True,
        )
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retain_only_legacy_ruff_and_migrate_v2_to_v3(database: Path) -> None:
    """Model an rc22 Ruff publication copied and migrated for read-only diff."""

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """DELETE FROM external_tool_runs WHERE tool_run_id IN (
            SELECT tool_run_id FROM external_run_contracts)"""
        )
        for table in (
            "external_relations",
            "external_metrics",
            "external_run_counters",
            "external_run_replays",
            "external_findings",
            "external_run_inputs",
            "external_run_contracts",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE files RENAME TO files_current_fixture")
        connection.execute(code_schema_module._LEGACY_FILES_TABLE_DDL)
        columns = ",".join(code_schema_module._FILES_COLUMNS)
        connection.execute(
            f"INSERT INTO files({columns}) SELECT {columns} FROM files_current_fixture"
        )
        connection.execute("DROP TABLE files_current_fixture")
        connection.execute(code_schema_module._FILES_CURRENT_PATH_INDEX_DDL)
        connection.execute(code_schema_module._FILES_LAST_SEEN_INDEX_DDL)
        connection.execute("DELETE FROM schema_migrations WHERE version>=3")
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    finally:
        connection.close()

    initialize_code_state(database)
    connection = sqlite3.connect(database)
    try:
        checkpoint_code_wal(connection)
    finally:
        connection.close()
    remove_checkpointed_code_sidecars(database)


def test_migrated_legacy_ruff_compares_with_current_protected_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    baseline_state = tmp_path / "rc22-copy"
    current_state = tmp_path / "current"
    paths = _provider_source_tree(root)
    paths[1].write_text("value = 1\n", encoding="utf-8")

    _run_provider_publication(root, baseline_state, paths, 1, "protected")
    _retain_only_legacy_ruff_and_migrate_v2_to_v3(baseline_state / "code.sqlite3")
    _run_provider_publication(root, current_state, paths, 1, "trusted-static")

    result = compare_code_publications(baseline_state, current_state)

    providers = {item.provider_id: item for item in result.providers}
    protected = providers["ruff-protected-basic"]
    assert result.status == "ready"
    assert result.analysis_profile == "trusted-static"
    assert protected.baseline is not None
    assert protected.current is not None
    assert protected.baseline.profile == protected.current.profile == "protected"
    assert protected.baseline.provider_schema == protected.current.provider_schema
    assert protected.baseline.tool_version == protected.current.tool_version
    assert protected.baseline.comparability_signature.startswith("external-ruff-v1:xxh3_128:")
    assert protected.current.comparability_signature.startswith(
        "ruff-protected-basic-comparable-v1:xxh3_128:"
    )
    assert protected.baseline.comparability_signature != protected.current.comparability_signature
    assert protected.status == "ready"
    assert protected.reason is None
    assert protected.common == 0
    assert protected.added == 0
    assert protected.resolved == 0
    assert protected.gate == "passed"
    assert result.verdict == "equivalent_under_observed_metrics"
    assert {
        provider_id for provider_id, delta in providers.items() if delta.status == "not_evaluated"
    } == {
        "complexipy-cognitive",
        "deptry-project-dependencies",
        "git-history-local",
        "grimp-architecture",
        "installed-package-inventory",
        "mypy-trusted-project",
        "pip-audit-known-vulnerabilities",
        "pyright-trusted-project",
        "ruff-analyze-imports",
        "ruff-trusted-project",
        "semgrep-neocortex-invariants",
        "vulture-unused-static",
    }
    assert all(
        delta.gate == "not_evaluated"
        for provider_id, delta in providers.items()
        if provider_id != "ruff-protected-basic"
    )


def test_publication_diff_reports_only_common_unchanged_call_sites(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "repository"
    baseline_state = tmp_path / "baseline"
    current_state = tmp_path / "current"
    baseline_database = _build_publication(
        baseline_state,
        source_root,
        assignments={
            "new": None,
            "corrected": "pkg.first",
            "lost": "pkg.second",
            "stable": "pkg.first",
            "unresolved": None,
        },
        hotspot="pkg.first",
        probable_dead=("pkg.first", "pkg.second"),
    )
    current_database = _build_publication(
        current_state,
        source_root,
        assignments={
            "new": "pkg.first",
            "corrected": "pkg.second",
            "lost": None,
            "stable": "pkg.first",
            "unresolved": None,
        },
        hotspot="pkg.second",
        probable_dead=("pkg.second",),
    )
    before = (_sha256(baseline_database), _sha256(current_database))

    first = compare_code_publications(baseline_state, current_state)
    second = compare_code_publications(baseline_state, current_state)

    assert first.status == "ready"
    assert first == second
    assert first.calls is not None
    assert first.calls.common_call_sites == 5
    assert first.calls.baseline_only_call_sites == 0
    assert first.calls.current_only_call_sites == 0
    assert first.calls.newly_resolved == 1
    assert first.calls.corrected == 1
    assert first.calls.lost == 1
    assert first.calls.unchanged_resolved == 1
    assert first.calls.still_unresolved == 1
    assert {example.change for example in first.calls.examples} == {
        "newly_resolved",
        "corrected",
        "lost",
    }
    assert first.hotspots is not None
    assert first.hotspots.added == 1
    assert first.hotspots.removed == 1
    assert first.hotspots.changed_evidence == 0
    assert first.probable_dead_delta == -1
    assert first.digest is not None
    assert (_sha256(baseline_database), _sha256(current_database)) == before
    assert not Path(f"{baseline_database}-wal").exists()
    assert not Path(f"{current_database}-wal").exists()


def test_publication_diff_of_the_same_state_is_stable_and_empty(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "repository"
    state = tmp_path / "state"
    _build_publication(
        state,
        source_root,
        assignments={"stable": "pkg.first", "unresolved": None},
        hotspot="pkg.first",
        probable_dead=("pkg.first",),
    )

    result = compare_code_publications(state, state)

    assert result.status == "ready"
    assert result.calls is not None
    assert result.calls.common_call_sites == 2
    assert result.calls.newly_resolved == 0
    assert result.calls.corrected == 0
    assert result.calls.lost == 0
    assert result.hotspots is not None
    assert result.hotspots.added == 0
    assert result.hotspots.removed == 0
    assert result.hotspots.changed_evidence == 0
    assert result.probable_dead_delta == 0
    assert result.test_coverage is not None
    assert result.test_coverage.status == "not_comparable"
    assert all(gate.status == "not_evaluated" for gate in result.test_coverage.gates)
    assert result.supply_chain is not None
    assert result.supply_chain.status == "not_evaluated"
    assert len(result.supply_chain.gates) == 6
    assert result.supply_chain.mutation_authority is False
    assert result.engineering_analytics is not None
    assert result.engineering_analytics.status == "not_comparable"
    assert result.engineering_analytics.baseline_mutation_score is None
    assert result.engineering_analytics.current_mutation_score is None
    payload = result.as_payload()
    assert payload["schema"] == "neocortex.code-publication-diff/v10"
    compatible_schemas = payload["compatible_schemas"]
    assert isinstance(compatible_schemas, list)
    assert compatible_schemas == []
    assert isinstance(payload["engineering_analytics"], dict)
    coverage_payload = payload["test_coverage"]
    assert isinstance(coverage_payload, dict)
    assert coverage_payload["schema"] == "neocortex.code-coverage-analysis/v2"
    assert "test_coverage_delta_not_comparable" in result.limitations


def _architecture_provider(provider_id: str) -> ArchitectureProviderStatus:
    return ArchitectureProviderStatus(
        provider_id=provider_id,
        status="ready",
        reason=None,
        tool_name=provider_id,
        tool_version="1",
        provider_schema=f"neocortex.{provider_id}/v1",
        comparability_signature=f"fixture:{provider_id}",
        provider_gate="passed",
        execution="full",
        tool_run_id=1,
        source_tool_run_id=1,
        metrics=1,
        relations=1,
    )


def _architecture_publications() -> tuple[
    CodeArchitectureAnalysis,
    CodeArchitectureAnalysis,
]:
    providers = tuple(
        _architecture_provider(provider)
        for provider in (
            "complexipy-cognitive",
            "grimp-architecture",
            "ruff-analyze-imports",
        )
    )
    baseline = CodeArchitectureAnalysis(
        "baseline",
        1,
        "ready",
        None,
        "observed",
        (),
        providers,
        None,
        (
            ArchitectureModule("pkg.helper", 1, 0, 5.0, 5.0, 1, (), (), None, None, None, None),
            ArchitectureModule("pkg.target", 0, 1, 10.0, 10.0, 1, (), (), None, None, None, None),
        ),
        (),
        (ArchitectureImportEdge("pkg.target", "pkg.helper", "both", True, True, True, 1.0),),
        (),
        (ArchitectureContract("layers", "passed", True, 0, (), (), (), "v1"),),
        (),
    )
    current = CodeArchitectureAnalysis(
        "current",
        2,
        "ready",
        None,
        "observed",
        (),
        providers,
        None,
        (
            ArchitectureModule(
                "pkg.helper",
                1,
                1,
                8.0,
                8.0,
                1,
                ("cycle",),
                ("layers",),
                None,
                None,
                None,
                None,
            ),
            ArchitectureModule(
                "pkg.target",
                1,
                1,
                8.0,
                8.0,
                1,
                ("cycle",),
                ("layers",),
                None,
                None,
                None,
                None,
            ),
        ),
        (),
        (
            ArchitectureImportEdge("pkg.target", "pkg.helper", "both", True, True, True, 1.0),
            ArchitectureImportEdge("pkg.helper", "pkg.target", "both", True, True, True, 1.0),
        ),
        (ArchitectureCycle("cycle", ("pkg.helper", "pkg.target")),),
        (
            ArchitectureContract(
                "layers",
                "failed",
                True,
                1,
                ("pkg.helper",),
                ("pkg.target",),
                (("pkg.helper", "pkg.target"),),
                "v1",
            ),
        ),
        (),
    )
    return baseline, current


def test_architecture_delta_reports_module_cycle_contract_and_displacement() -> None:
    baseline, current = _architecture_publications()

    delta = _architecture_delta(baseline, current)

    assert delta.status == "ready"
    modules = {item.module_id: item for item in delta.modules}
    assert modules["pkg.target"].cognitive_complexity_delta == -2
    assert modules["pkg.helper"].cognitive_complexity_delta == 3
    assert delta.added_failed_contracts == ("layers",)
    assert delta.added_cycles == (("pkg.helper", "pkg.target"),)
    assert len(delta.displaced_complexity) == 1
    assert delta.displaced_complexity[0].target_module == "pkg.target"
    assert delta.displaced_complexity[0].recipient_modules == ("pkg.helper",)
    assert delta.architecture_contracts_not_degraded == "failed"
    assert delta.no_new_import_cycles == "failed"
    assert delta.module_complexity_not_displaced == "failed"


def test_architecture_delta_never_passes_when_provider_evidence_is_missing() -> None:
    baseline, current = _architecture_publications()
    missing = replace(
        baseline,
        status="abstained",
        reason="required_provider_not_ready:grimp-architecture:provider_missing",
        providers=(),
    )

    delta = _architecture_delta(missing, current)

    assert delta.status == "not_evaluated"
    assert delta.architecture_contracts_not_degraded == "not_evaluated"
    assert delta.no_new_import_cycles == "not_evaluated"
    assert delta.module_complexity_not_displaced == "not_evaluated"


def test_architecture_v2_graph_deltas_preserve_truncation_honesty() -> None:
    baseline = ArchitectureModule(
        "pkg.module",
        1,
        2,
        4.0,
        4.0,
        1,
        (),
        (),
        None,
        None,
        None,
        None,
        path_namespace_id="pkg",
        dependency_reach=4,
        blast_radius=7,
        directed_degree_centrality=0.25,
        cross_path_namespace_fan_in=1,
        cross_path_namespace_fan_out=2,
    )
    current = replace(
        baseline,
        dependency_reach=9,
        dependency_reach_truncated=True,
        blast_radius=10,
        directed_degree_centrality=0.5,
        cross_path_namespace_fan_in=3,
        cross_path_namespace_fan_out=1,
    )

    delta = _architecture_module_deltas(
        {baseline.module_id: baseline}, {current.module_id: current}
    )[0]

    assert delta.dependency_reach_status == "not_comparable"
    assert delta.dependency_reach_delta is None
    assert delta.dependency_reach_reason == "dependency_reach_truncated_lower_bound"
    assert delta.blast_radius_status == "comparable"
    assert delta.blast_radius_delta == 3
    assert delta.directed_degree_centrality_delta == 0.25
    assert delta.cross_path_namespace_fan_in_delta == 2
    assert delta.cross_path_namespace_fan_out_delta == -1
    assert delta.graph_metrics_status == "not_comparable"


def _engineering_analysis(
    status: str,
    *,
    scope: str | None,
    score: float | None,
    gate_status: str,
) -> CodeEngineeringAnalytics:
    return CodeEngineeringAnalytics(
        database="fixture.sqlite3",
        analysis_run_id=1,
        status=status,  # type: ignore[arg-type]
        reason=None if status == "ready" else "one_or_more_engineering_dimensions_not_ready",
        providers=(),
        modules=(),
        gates=(
            EngineeringGate(
                "mutation_measurement_complete",
                gate_status,  # type: ignore[arg-type]
                None if gate_status == "passed" else "mutation_provider_not_ready",
            ),
        ),
        mutation_scope_signature=scope,
        mutation_score=score,
        limitations=(),
        digest="fixture",
    )


def test_engineering_delta_requires_ready_matching_measurement_scope() -> None:
    h5 = _engineering_analysis("partial", scope=None, score=None, gate_status="not_evaluated")
    current = _engineering_analysis("ready", scope="scope-v1", score=0.8, gate_status="passed")

    delta = _engineering_delta(h5, current)

    assert delta.status == "not_comparable"
    assert delta.reason == "engineering_analytics_not_ready:baseline=partial:current=ready"
    assert delta.baseline_mutation_score is None
    assert delta.current_mutation_score is None
    assert delta.mutation_score_delta is None
    assert [item.gate for item in delta.gates] == [
        "mutation_measurement_complete",
        "mutation_score_not_degraded",
    ]
    assert delta.gates[1].status == "not_evaluated"
    assert delta.aggregate_score is None
    assert delta.defect_probability is None


def test_engineering_delta_uses_exact_non_degradation_gate() -> None:
    baseline = _engineering_analysis("ready", scope="scope-v1", score=0.8, gate_status="passed")
    current = _engineering_analysis("ready", scope="scope-v1", score=0.79, gate_status="passed")

    delta = _engineering_delta(baseline, current)

    assert delta.status == "comparable"
    assert delta.baseline_mutation_score == 0.8
    assert delta.current_mutation_score == 0.79
    assert delta.mutation_score_delta == -0.010000000000000009
    assert delta.gates[0].status == "passed"
    assert delta.gates[1].status == "failed"
    assert delta.gates[1].reason == "mutation_score_decreased"
