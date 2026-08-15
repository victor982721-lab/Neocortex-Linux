"""Public CLI contracts for the integrated code route and direct queries."""
# region [00] Contexto del módulo
# Módulo: tests/test_code_cli.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
import io
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _04_Nucleo_Operativo import cli_code
from _04_Nucleo_Operativo.cli_app import dispatch_direct
from _04_Nucleo_Operativo.cli_config import framework_config_from_args
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.code_schema import initialize_code_state
from _04_Nucleo_Operativo.code_coverage_analysis import (
    CODE_COVERAGE_PROVIDER_ID,
    CodeCoverageAnalysis,
    CoverageComparison,
    CoverageGateEvaluation,
    CoverageScopeSummary,
    CoverageTestOutcomes,
    CoverageToolVersion,
    CoverageTotals,
    WorkPackageCoverageProjection,
)
from _04_Nucleo_Operativo.external_evidence_providers import provider_tool_versions

# endregion [01]

# region [02] Implementación


def _validated(*arguments: str):
    args = build_parser().parse_args(arguments)
    validate_arguments(args)
    return args


def test_unused_human_output_is_bounded_and_surfaces_all_states(
    capsys: pytest.CaptureFixture[str],
) -> None:
    states = (
        "explained_usage",
        "dynamic_usage_possible",
        "insufficient_evidence",
        "probable_unused_high_consensus",
    )
    cli_code._emit_code_unused(
        "CODE_UNUSED",
        {
            "status": "ready",
            "reason": None,
            "counts": {"total": 4, **dict.fromkeys(states, 1)},
            "authority": "advisory",
            "mutation_authority": False,
            "calibration": {
                "signature": "calibration-v1",
                "total": 8,
                "precision": 1.0,
                "recall": 0.75,
                "abstention": 0.25,
                "unsupported": 0,
            },
            "holdout": {
                "signature": "holdout-v1",
                "total": 4,
                "precision": 1.0,
                "recall": 1.0,
                "abstention": 0.0,
                "unsupported": 0,
            },
            "candidates": [
                {
                    "candidate_id": f"candidate-{index}",
                    "state": state,
                    "relative_path": f"pkg/module_{index}.py",
                    "symbol": f"pkg.symbol_{index}",
                    "start_line": index + 1,
                    "provider_ids": ["vulture-unused-static"],
                    "reasons": [state],
                }
                for index, state in enumerate(states)
            ],
            "limitations": ["advisory_only"],
        },
    )

    output = capsys.readouterr().out
    assert "CODE_UNUSED status=ready total=4" in output
    for state in states:
        assert f"{state}=1" in output
        assert f"state={state}" in output
    assert output.count("CODE_UNUSED_CANDIDATE ") == 4
    assert "mutation_authority=0" in output
    assert "CODE_UNUSED_CALIBRATION signature=calibration-v1" in output
    assert "CODE_UNUSED_HOLDOUT signature=holdout-v1" in output
    assert "CODE_UNUSED_LIMITATION advisory_only" in output


def test_code_route_configuration_is_translated_without_eager_analyzers(
    tmp_path: Path,
) -> None:
    args = _validated(
        "--state-directory",
        str(tmp_path),
        "--route",
        "code",
        "--code-max-mb",
        "2",
        "--code-max-count",
        "7",
        "--code-cache-validation",
        "full",
        "--no-code-generated",
        "--no-code-vendored",
    )

    config = framework_config_from_args(args)

    assert config.route == "code"
    assert config.code_database == tmp_path / "code.sqlite3"
    assert config.code_max_file_bytes == 2_000_000
    assert config.code_max_documents == 7
    assert config.code_cache_validation == "full"
    assert config.code_candidate_scope == "projects"
    assert not config.code_include_generated
    assert not config.code_include_vendored


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--code-search", " "), "--code-search must be non-empty"),
        (
            ("--code-language", "python"),
            "code search filters require --code-search",
        ),
        (
            ("--code-reconstruct-strategy", "branches"),
            "--code-reconstruct-strategy requires --code-reconstruct",
        ),
        (
            ("--code-status", "--apply"),
            "direct code operations are read-only and reject --apply",
        ),
        (
            ("--code-projects", "--route", "code"),
            "direct code operations cannot be combined with --route",
        ),
        (
            ("--code-experiment-run", " "),
            "--code-experiment-run must be non-empty",
        ),
    ],
)
def test_code_direct_options_reject_ambiguous_or_unsafe_combinations(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


def test_code_status_and_doctor_do_not_initialize_absent_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_args = _validated("--state-directory", str(tmp_path), "--code-status", "--code-json")
    assert dispatch_direct(status_args) == 0
    status = json.loads(capsys.readouterr().out)

    doctor_args = _validated("--state-directory", str(tmp_path), "--code-doctor", "--code-json")
    assert dispatch_direct(doctor_args) == 0
    doctor = json.loads(capsys.readouterr().out)

    assert status["kind"] == "code-status"
    assert not status["exists"]
    assert status["self_analysis"] is None
    assert status["architecture"]["status"] == "abstained"
    assert status["architecture"]["schema"] == "neocortex.code-architecture-analysis/v3"
    assert status["architecture"]["reason"] == "code_state_missing"
    assert status["architecture"]["summary"] is None
    assert len(status["architecture"]["gates"]) == 3
    assert status["test_coverage"]["status"] == "abstained"
    assert status["test_coverage"]["reason"] == "code_state_missing"
    assert status["unused_analysis"]["status"] == "abstained"
    assert status["unused_analysis"]["mutation_authority"] is False
    assert status["supply_chain"]["status"] == "abstained"
    assert status["supply_chain"]["mutation_authority"] is False
    assert len(status["supply_chain"]["gates"]) == 6
    assert status["engineering_analytics"]["status"] == "abstained"
    assert status["engineering_analytics"]["reason"] == "code_state_missing"
    assert status["engineering_analytics"]["aggregate_score"] is None
    assert status["engineering_analytics"]["defect_probability"] is None
    assert doctor["kind"] == "code-doctor"
    assert doctor["schema"] == "not-initialized"
    assert set(doctor["external_evidence_providers"]) == set(provider_tool_versions())
    assert "node" in doctor["tools"]
    assert "pyright" in doctor["tools"]
    assert all(
        provider["authority"] == "advisory" and provider["mutation_authority"] is False
        for provider in doctor["external_evidence_providers"].values()
    )
    human_status_args = _validated("--state-directory", str(tmp_path), "--code-status")
    assert dispatch_direct(human_status_args) == 0
    human_status = capsys.readouterr().out
    assert "CODE_STATUS" in human_status and "exists=false" in human_status
    assert "CODE_ARCHITECTURE status=abstained gate=abstained" in human_status
    assert "CODE_ARCHITECTURE_SUMMARY status=not_evaluated" in human_status
    assert human_status.count("CODE_ARCHITECTURE_GATE ") == 3
    assert "CODE_COVERAGE status=abstained" in human_status
    assert "CODE_UNUSED status=abstained total=0" in human_status
    assert "CODE_SUPPLY_CHAIN status=abstained" in human_status
    assert human_status.count("CODE_SUPPLY_CHAIN_GATE ") == 6
    assert "CODE_ENGINEERING status=abstained" in human_status
    assert not (tmp_path / "code.sqlite3").exists()
    assert not (tmp_path / "framework.sqlite3").exists()
    assert not (tmp_path / "dedup.sqlite3").exists()


def test_code_review_abstains_without_initializing_absent_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _validated(
        "--state-directory",
        str(tmp_path),
        "--code-review",
        "--code-review-limit",
        "50",
        "--code-json",
    )

    assert args.code_review_limit == 50
    assert dispatch_direct(args) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["kind"] == "code-review"
    assert payload["schema"] == "neocortex.code-review/v19"
    assert payload["compatible_schemas"] == []
    assert payload["status"] == "abstained"
    assert payload["reason"] == "code_state_missing"
    assert payload["actionability_version"] == "python-maintenance-epistemic-gate-v2"
    assert payload["recommendation_status"] == "not_evaluated"
    assert payload["planning_version"] == "python-maintenance-work-packages-v5"
    assert payload["work_package_status"] == "not_evaluated"
    assert payload["work_packages"] == []
    assert not (tmp_path / "code.sqlite3").exists()
    assert not (tmp_path / "framework.sqlite3").exists()
    assert not (tmp_path / "dedup.sqlite3").exists()


def test_code_experiment_rejects_an_unknown_proposal_without_initializing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _validated(
        "--state-directory",
        str(tmp_path),
        "--code-experiment-run",
        "proposal:unknown",
        "--code-json",
    )

    assert dispatch_direct(args) == 2
    assert "code review cannot plan experiments" in capsys.readouterr().err
    assert not (tmp_path / "code.sqlite3").exists()


def test_code_experiment_persists_the_exact_public_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proposal = SimpleNamespace(
        proposal_id="proposal:exact",
        planning_status="planned",
        runner_kind="trusted_deep_declared_scenarios",
    )
    review = SimpleNamespace(
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(
            root=str(tmp_path),
            processing_signature="snapshot:exact",
            analysis_run_id=17,
        ),
        experiment_plan=SimpleNamespace(proposals=(proposal,)),
        digest=SimpleNamespace(
            xxh3_128="review-primary",
            xxh3_64_guard="review-guard",
            byte_count=123,
        ),
    )
    receipt = SimpleNamespace(
        status="passed",
        receipt_id="receipt:exact",
        proposal_id=proposal.proposal_id,
        passed=1,
        failed=0,
        skipped=0,
        duration_ms=9,
        code_database_unchanged=True,
        authority="advisory",
        mutation_authority=False,
        reason=None,
        limitations=(),
    )
    stored = SimpleNamespace(
        recorded_ns=99,
        as_payload=lambda: {
            "schema": "neocortex.code-experiment-store/v1",
            "receipt": {"receipt_id": receipt.receipt_id, "status": "passed"},
        },
    )
    args = _validated(
        "--state-directory",
        str(tmp_path),
        "--code-experiment-run",
        proposal.proposal_id,
        "--code-json",
    )

    with (
        patch(
            "_04_Nucleo_Operativo.code_review.review_code_state",
            return_value=review,
        ),
        patch(
            "_04_Nucleo_Operativo.code_experiment_executor.execute_code_experiment",
            return_value=receipt,
        ) as execute,
        patch(
            "_04_Nucleo_Operativo.code_experiment_store.code_review_digest_identity",
            return_value="review:exact",
        ),
        patch(
            "_04_Nucleo_Operativo.code_experiment_store.record_code_experiment_receipt",
            return_value=stored,
        ) as record,
    ):
        assert dispatch_direct(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == stored.as_payload()
    execute.assert_called_once()
    record.assert_called_once_with(
        tmp_path / "code.sqlite3",
        receipt,
        proposal,
        analysis_run_id=17,
        processing_signature="snapshot:exact",
        review_digest="review:exact",
    )


def test_code_publication_diff_abstains_without_initializing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "baseline"
    current = tmp_path / "current"
    args = _validated(
        "--state-directory",
        str(current),
        "--code-publication-diff",
        str(baseline),
        "--code-json",
    )

    assert dispatch_direct(args) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["kind"] == "code-publication-diff"
    assert payload["status"] == "abstained"
    assert payload["reason"].startswith("baseline_unavailable:FileNotFoundError:")
    assert not baseline.exists()
    assert not current.exists()


def test_code_status_projects_bounded_architecture_summary_and_gates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo.code_architecture_analysis import (
        ArchitectureContract,
        ArchitectureGateEvaluation,
        ArchitectureProviderStatus,
        ArchitectureSummary,
        CodeArchitectureAnalysis,
    )

    analysis = CodeArchitectureAnalysis(
        database=str(tmp_path / "code.sqlite3"),
        analysis_run_id=7,
        status="ready",
        reason=None,
        gate="observed",
        gates=(ArchitectureGateEvaluation("architecture_contracts", "failed", "violation"),),
        providers=(
            ArchitectureProviderStatus(
                provider_id="grimp-architecture",
                status="ready",
                reason=None,
                tool_name="grimp",
                tool_version="3.5",
                provider_schema="fixture/v1",
                comparability_signature="fixture",
                provider_gate="baseline",
                execution="ran",
                tool_run_id=1,
                source_tool_run_id=None,
                metrics=4,
                relations=5,
            ),
        ),
        summary=ArchitectureSummary(
            modules=25,
            import_edges=30,
            consensus_edges=28,
            graph_disagreements=2,
            cyclic_sccs=1,
            grimp_reported_internal_modules=25,
            grimp_reported_import_edges=30,
            grimp_reported_cyclic_sccs=1,
            grimp_counts_consistent=True,
        ),
        modules=tuple(SimpleNamespace() for _ in range(25)),  # type: ignore[arg-type]
        symbols=(),
        imports=tuple(SimpleNamespace() for _ in range(30)),  # type: ignore[arg-type]
        cycles=(SimpleNamespace(),),  # type: ignore[arg-type]
        contracts=(
            ArchitectureContract(
                contract_id="layers",
                status="failed",
                evaluated=True,
                violations=1,
                importer_modules=("app",),
                imported_modules=("core",),
                import_chains=(("app", "core"),),
                contract_schema="fixture/v1",
            ),
        ),
        limitations=(),
    )

    architecture = cli_code._architecture_status_payload(analysis)

    assert architecture["summary"] == {
        "modules": 25,
        "import_edges": 30,
        "consensus_edges": 28,
        "graph_disagreements": 2,
        "cyclic_sccs": 1,
        "grimp_reported_internal_modules": 25,
        "grimp_reported_import_edges": 30,
        "grimp_reported_cyclic_sccs": 1,
        "grimp_counts_consistent": True,
    }
    assert architecture["counts"] == {
        "modules": 25,
        "symbols": 0,
        "imports": 30,
        "cycles": 1,
        "contracts": 1,
        "failed_contracts": 1,
    }
    assert "modules" not in architecture
    snapshot = cli_code._CodeStatusSnapshot(
        schema_version=4,
        counts={},
        latest_run=None,
        external_evidence={"status": "ready"},
        external_evidence_suite={"profile": "full", "status": "ready", "providers": []},
        architecture=architecture,
        test_coverage={
            "status": "ready",
            "reason": None,
            "suite_selection": "selected",
            "measurement_complete": True,
            "content_executed": True,
            "outcomes": {
                "collected": 2,
                "selected": 2,
                "passed": 2,
                "failed": 0,
                "skipped": 0,
            },
            "totals": {
                "covered_lines": 8,
                "executable_lines": 10,
                "covered_branch_exits": 3,
                "branch_exits": 4,
            },
            "gates": [
                {"gate": "tests_passed", "status": "passed", "reason": None},
            ],
        },
    )

    cli_code._emit_code_status(
        tmp_path / "code.sqlite3",
        {},
        snapshot,
        None,
        json_output=False,
    )

    output = capsys.readouterr().out
    assert "CODE_ARCHITECTURE status=ready gate=observed modules=25 imports=30" in output
    assert "CODE_ARCHITECTURE_SUMMARY modules=25 import_edges=30 consensus_edges=28" in output
    assert "CODE_ARCHITECTURE_PROVIDER id=grimp-architecture status=ready" in output
    assert "CODE_ARCHITECTURE_GATE id=architecture_contracts status=failed" in output
    assert "CODE_COVERAGE status=ready suite=selected" in output
    assert "CODE_COVERAGE_GATE id=tests_passed status=passed" in output


def test_run_code_review_signature_json_and_abstention_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_review

    assert str(inspect.signature(cli_code.run_code_review)) == (
        "(args: 'argparse.Namespace') -> 'int'"
    )
    calls: list[tuple[Path, int]] = []
    selected_result: object

    def review_code_state(state_directory: Path, *, limit: int):
        calls.append((state_directory, limit))
        return selected_result

    monkeypatch.setattr(code_review, "review_code_state", review_code_state)
    args = argparse.Namespace(
        state_directory=tmp_path,
        code_review_limit=7,
        code_json=True,
    )
    ready_payload = {"schema": "fixture-review/v1", "status": "ready"}
    selected_result = SimpleNamespace(
        status="ready",
        as_payload=lambda: ready_payload,
    )

    assert cli_code.run_code_review(args) == 0
    assert json.loads(capsys.readouterr().out) == ready_payload

    abstained_payload = {
        "schema": "fixture-review/v1",
        "status": "abstained",
        "reason": "fixture_unavailable",
    }
    selected_result = SimpleNamespace(
        status="abstained",
        as_payload=lambda: abstained_payload,
    )
    assert cli_code.run_code_review(args) == 2
    assert json.loads(capsys.readouterr().out) == abstained_payload

    args.code_json = False
    selected_result = SimpleNamespace(
        status="abstained",
        reason="fixture_unavailable",
        database="C:/fixture/code.sqlite3",
    )
    assert cli_code.run_code_review(args) == 2
    assert capsys.readouterr().out == (
        "CODE_REVIEW status=abstained reason=fixture_unavailable "
        'database="C:/fixture/code.sqlite3"\n'
    )
    assert calls == [(tmp_path, 7), (tmp_path, 7), (tmp_path, 7)]


def test_run_code_review_propagates_cancellation_and_maps_known_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_review

    args = argparse.Namespace(
        state_directory=tmp_path,
        code_review_limit=5,
        code_json=False,
    )
    cancellation = KeyboardInterrupt("cancel review")

    def cancel(*_args, **_kwargs):
        raise cancellation

    monkeypatch.setattr(code_review, "review_code_state", cancel)
    with pytest.raises(KeyboardInterrupt) as raised:
        cli_code.run_code_review(args)
    assert raised.value is cancellation
    cancelled_output = capsys.readouterr()
    assert cancelled_output.out == ""
    assert cancelled_output.err == ""

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(code_review, "review_code_state", fail)
    assert cli_code.run_code_review(args) == 2
    failed_output = capsys.readouterr()
    assert failed_output.out == ""
    assert failed_output.err == "ERROR code-review RuntimeError: fixture failure\n"


@pytest.mark.parametrize("missing", ("snapshot", "coverage", "digest"))
def test_run_code_review_rejects_incomplete_human_ready_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    from _04_Nucleo_Operativo import code_review

    values: dict[str, object | None] = {
        "snapshot": object(),
        "coverage": object(),
        "digest": object(),
    }
    values[missing] = None
    result = SimpleNamespace(status="ready", **values)
    monkeypatch.setattr(
        code_review,
        "review_code_state",
        lambda *_args, **_kwargs: result,
    )

    assert (
        cli_code.run_code_review(
            argparse.Namespace(
                state_directory=tmp_path,
                code_review_limit=5,
                code_json=False,
            )
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR code-review RuntimeError: ready result is incomplete\n"


def test_code_review_human_surfaces_retention_without_deletion_authority(
    capsys: pytest.CaptureFixture[str],
) -> None:
    retention = SimpleNamespace(
        status="ready",
        reason=None,
        policy_id="canonical-four-store-dry-run-hold-projection-v1",
        observation="declared_holds_resolved",
        stores=(
            SimpleNamespace(status="ready", truncated=False),
            SimpleNamespace(status="ready", truncated=False),
            SimpleNamespace(status="ready", truncated=True),
            SimpleNamespace(status="ready", truncated=False),
        ),
        missing_hold_ids=(),
        authority="advisory",
        mutation_authority=False,
    )

    cli_code._emit_code_review_ranked_evidence(
        SimpleNamespace(
            retention_analysis=retention,
            recommendations=(),
            findings=(),
            limitations=(),
        )
    )

    assert capsys.readouterr().out == (
        "CODE_RETENTION_ANALYSIS status=ready reason=null "
        "policy=canonical-four-store-dry-run-hold-projection-v1 "
        "observation=declared_holds_resolved stores=4 ready_stores=4 "
        "missing_holds=0 truncated_stores=1 authority=advisory "
        "mutation_authority=0\n"
    )


def test_code_review_human_surfaces_architecture_and_work_package_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_review

    architecture = SimpleNamespace(
        status="ready",
        gate="observed",
        reason=None,
        summary=SimpleNamespace(
            modules=4,
            import_edges=5,
            consensus_edges=4,
            graph_disagreements=1,
            cyclic_sccs=1,
        ),
        gates=(
            SimpleNamespace(gate="architecture_contracts", status="failed", reason="violation"),
        ),
        contracts=(
            SimpleNamespace(
                status="failed",
                contract_id="layers",
                violations=1,
                importer_modules=("app",),
                imported_modules=("core",),
            ),
        ),
    )
    package = SimpleNamespace(
        package_rank=1,
        change_risk="medium",
        members=(object(),),
        members_truncated=False,
        confidence="high",
        primary_symbol="app.handler",
        package_id="package-1",
        primary_module="app",
        import_chains=(("app", "core"),),
        affected_architecture_contracts=("layers",),
        test_coverage=WorkPackageCoverageProjection(
            "symbol:app.handler:10:30",
            "executed",
            ("tests/test_app.py::test_handler",),
            ("relation:handler",),
            CoverageGateEvaluation("work_package_target_executed_by_passing_suite", "passed", None),
        ),
        test_coverage_scope=CoverageScopeSummary(
            "symbol",
            "symbol:app.handler:10:30",
            "app",
            "symbol:app.handler:10:30",
            "handler",
            10,
            30,
            "app.py",
            CoverageTotals(20, 18, 2, 6, 5, 1, 90.0, 83.333333),
            ((19, 20),),
            ((18, 20),),
            False,
            False,
            ("tests/test_app.py::test_handler",),
        ),
        acceptance_gates=(
            "target_hotspot_removed",
            "architecture_contracts_not_degraded",
            "no_new_import_cycles",
            "module_complexity_not_displaced",
        ),
    )
    result = SimpleNamespace(
        database=str(tmp_path / "code.sqlite3"),
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(freshness="current", current=True),
        coverage=SimpleNamespace(
            current_python_files=4,
            complete_python_files=4,
            candidate_hotspots=1,
            probable_dead_suppressed=0,
            resolved_call_edges=3,
            call_edges=3,
        ),
        digest=SimpleNamespace(xxh3_128="digest"),
        findings=(),
        recommendations=(),
        work_packages=(package,),
        ranking="python-confirmed-hotspots-v2",
        actionability_version="python-maintenance-actionability-v1",
        planning_version="python-maintenance-work-packages-v3",
        external_evidence=SimpleNamespace(
            provider="pyright",
            status="ready",
            execution="executed",
            diagnostics=2,
            added=1,
            resolved=3,
            gate="passed",
        ),
        external_evidence_suite=SimpleNamespace(
            profile="trusted-static",
            status="ready",
            providers=(
                SimpleNamespace(
                    provider_id="pyright",
                    status="ready",
                    findings=2,
                    metrics=1,
                    relations=3,
                    content_executed=True,
                    gate="passed",
                ),
            ),
        ),
        architecture=architecture,
        test_coverage=CodeCoverageAnalysis(
            database="fixture",
            analysis_run_id=1,
            provider_id=CODE_COVERAGE_PROVIDER_ID,
            tool_run_id=1,
            effective_tool_run_id=1,
            status="ready",
            reason=None,
            suite_selection="selected",
            measurement_complete=True,
            content_executed=True,
            tool_versions=(CoverageToolVersion("coverage", "7.14.1"),),
            suite_signature="suite",
            configuration_signature="configuration",
            measurement_scope_signature="scope",
            outcomes=CoverageTestOutcomes(1, 1, 1, 0, 0),
            totals=CoverageTotals(20, 18, 2, 6, 5, 1, 90.0, 83.333333),
            modules=(),
            symbols=(package.test_coverage_scope,),
            test_relations=(),
            failed_test_nodeids=(),
            gates=(CoverageGateEvaluation("tests_passed", "passed", None),),
            limitations=("selected_suite_is_not_claimed_as_full_project_coverage",),
        ),
        recommendation_status="abstained",
        recommendation_reason="none",
        work_package_status="ready",
        work_package_reason=None,
        question_evaluations=(),
        limitations=(),
    )
    monkeypatch.setattr(code_review, "review_code_state", lambda *_args, **_kwargs: result)

    exit_code = cli_code.run_code_review(
        argparse.Namespace(state_directory=tmp_path, code_review_limit=10, code_json=False)
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "CODE_REVIEW_ARCHITECTURE status=ready gate=observed failed_contracts=1" in output
    assert "CODE_REVIEW_EXTERNAL provider=pyright status=ready execution=executed" in output
    assert "CODE_REVIEW_PROVIDER_SUITE profile=trusted-static status=ready" in output
    assert "CODE_REVIEW_PROVIDER id=pyright status=ready findings=2" in output
    assert "CODE_REVIEW_ARCHITECTURE_SUMMARY modules=4 import_edges=5" in output
    assert "CODE_REVIEW_ARCHITECTURE_GATE id=architecture_contracts status=failed" in output
    assert 'CODE_REVIEW_ARCHITECTURE_CONTRACT status=failed id="layers"' in output
    assert 'primary_module="app"' in output
    assert 'import_chains=[["app", "core"]]' in output
    assert 'affected_architecture_contracts=["layers"]' in output
    assert "architecture_contracts_not_degraded" in output
    assert "no_new_import_cycles" in output
    assert "module_complexity_not_displaced" in output
    assert "CODE_REVIEW_TEST_COVERAGE status=ready suite=selected" in output
    assert "CODE_REVIEW_TEST_COVERAGE_GATE id=tests_passed status=passed" in output
    assert (
        "CODE_ANALYSIS_QUESTIONS status=not_evaluated total=0 confirmed=0 "
        "abstained=0 human_review_required=0 experiment_required=0 "
        "decision_abstained=0 decisions=0 mutation_authority=0"
    ) in output
    assert "CODE_REVIEW_WORK_PACKAGE_COVERAGE status=executed" in output
    assert 'tests=["tests/test_app.py::test_handler"]' in output
    assert "missing_lines=[[19, 20]]" in output
    assert "missing_branches=[[18, 20]]" in output
    lines = output.splitlines()
    ordered_prefixes = (
        "CODE_REVIEW status=ready",
        "CODE_REVIEW_COVERAGE ",
        "CODE_REVIEW_EXTERNAL ",
        "CODE_REVIEW_PROVIDER_SUITE ",
        "CODE_REVIEW_PROVIDER ",
        "CODE_REVIEW_ARCHITECTURE ",
        "CODE_REVIEW_TEST_COVERAGE ",
        "CODE_REVIEW_UNUSED ",
        "CODE_REVIEW_SUPPLY_CHAIN ",
        "CODE_REVIEW_RECOMMENDATION status=abstained",
        "CODE_ANALYSIS_QUESTIONS ",
        "CODE_REVIEW_WORK_PACKAGE_SUPPLY_CHAIN ",
        "CODE_REVIEW_WORK_PACKAGE status=ready",
        "CODE_REVIEW_WORK_PACKAGE_ARCHITECTURE ",
        "CODE_REVIEW_WORK_PACKAGE_COVERAGE ",
    )
    phase_positions = tuple(
        next(index for index, line in enumerate(lines) if line.startswith(prefix))
        for prefix in ordered_prefixes
    )
    assert phase_positions == tuple(sorted(phase_positions))


def _ready_code_publication_cli_result(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        baseline_database=str(tmp_path / "baseline" / "code.sqlite3"),
        current_database=str(tmp_path / "current" / "code.sqlite3"),
        status="ready",
        reason=None,
        baseline=SimpleNamespace(resolved_call_edges=2, call_edges=3),
        current=SimpleNamespace(resolved_call_edges=3, call_edges=4),
        calls=SimpleNamespace(
            common_call_sites=2,
            baseline_only_call_sites=1,
            current_only_call_sites=2,
            newly_resolved=1,
            corrected=2,
            lost=3,
        ),
        hotspots=SimpleNamespace(common=4, added=5, removed=6, changed_evidence=7),
        probable_dead_delta=1,
        external_evidence=SimpleNamespace(
            status="ready",
            common=8,
            added=9,
            resolved=10,
            gate="passed",
        ),
        digest=SimpleNamespace(xxh3_128="digest"),
        analysis_profile="trusted-static",
        verdict="improved",
        providers=(
            SimpleNamespace(
                provider_id="mypy-trusted-project",
                status="ready",
                common=11,
                added=12,
                resolved=13,
                relocated=14,
                gate="passed",
            ),
        ),
        architecture=SimpleNamespace(
            status="ready",
            reason=None,
            modules=(),
            added_failed_contracts=(),
            resolved_failed_contracts=(),
            added_cycles=(),
            resolved_cycles=(),
            displaced_complexity=(),
            architecture_contracts_not_degraded="passed",
            no_new_import_cycles="passed",
            module_complexity_not_displaced="passed",
        ),
        test_coverage=CoverageComparison(
            status="comparable",
            reason=None,
            baseline_suite_signature="suite",
            current_suite_signature="suite",
            executable_lines_delta=0,
            covered_lines_delta=1,
            missing_lines_delta=-1,
            branch_exits_delta=0,
            covered_branch_exits_delta=2,
            missing_branch_exits_delta=-2,
            line_coverage_percent_delta=1.5,
            branch_coverage_percent_delta=2.5,
            gates=(
                CoverageGateEvaluation(
                    "line_coverage_not_degraded",
                    "passed",
                    None,
                ),
            ),
        ),
        limitations=("fixture_limit",),
        as_payload=lambda: {
            "kind": "code-publication-diff",
            "status": "ready",
            "digest": "digest",
        },
    )


def test_run_code_publication_diff_signature_phase_order_and_complete_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    assert str(inspect.signature(cli_code.run_code_publication_diff)) == (
        "(args: 'argparse.Namespace') -> 'int'"
    )
    calls: list[tuple[Path, Path]] = []
    result = _ready_code_publication_cli_result(tmp_path)

    def compare_code_publications(baseline: Path, current: Path):
        calls.append((baseline, current))
        return result

    monkeypatch.setattr(
        code_publication_diff,
        "compare_code_publications",
        compare_code_publications,
    )
    baseline = tmp_path / "baseline"
    current = tmp_path / "current"
    args = argparse.Namespace(
        code_publication_diff=str(baseline),
        state_directory=current,
        code_json=False,
    )

    assert cli_code.run_code_publication_diff(args) == 0
    assert calls == [(baseline, current)]
    assert capsys.readouterr().out.splitlines() == [
        "CODE_PUBLICATION_DIFF status=ready digest=digest baseline_calls=2/3 current_calls=3/4",
        "CODE_PUBLICATION_DIFF_CALLS common=2 baseline_only=1 current_only=2 "
        "newly_resolved=1 corrected=2 lost=3",
        "CODE_PUBLICATION_DIFF_HOTSPOTS common=4 added=5 removed=6 "
        "changed_evidence=7 probable_dead_delta=+1",
        "CODE_PUBLICATION_DIFF_EXTERNAL provider=ruff status=ready common=8 "
        "added=9 resolved=10 gate=passed",
        "CODE_PUBLICATION_DIFF_PROVIDERS profile=trusted-static verdict=improved",
        "CODE_PUBLICATION_DIFF_PROVIDER id=mypy-trusted-project status=ready "
        "common=11 added=12 resolved=13 relocated=14 gate=passed",
        "CODE_PUBLICATION_DIFF_ARCHITECTURE status=ready module_deltas=0 "
        "changed_modules=0 added_failed_contracts=0 resolved_failed_contracts=0 "
        "added_cycles=0 resolved_cycles=0 displacements=0 contracts_gate=passed "
        "cycles_gate=passed displacement_gate=passed reason=null",
        "CODE_PUBLICATION_DIFF_ARCHITECTURE_CONTRACTS added=[] resolved=[] "
        "added_truncated=0 resolved_truncated=0",
        "CODE_PUBLICATION_DIFF_ARCHITECTURE_CYCLES added=[] resolved=[] "
        "added_truncated=0 resolved_truncated=0",
        "CODE_PUBLICATION_DIFF_COVERAGE status=comparable line_delta=1.5 "
        "branch_delta=2.5 covered_lines_delta=1 missing_lines_delta=-1 "
        "covered_branches_delta=2 missing_branches_delta=-2 reason=null",
        "CODE_PUBLICATION_DIFF_COVERAGE_GATE id=line_coverage_not_degraded "
        "status=passed reason=null",
        "CODE_PUBLICATION_DIFF_UNUSED status=not_evaluated gate=not_evaluated "
        'reason="unused_delta_missing"',
        "CODE_PUBLICATION_DIFF_SUPPLY_CHAIN status=not_evaluated "
        'reason="supply_chain_delta_missing"',
        "CODE_PUBLICATION_DIFF_ENGINEERING status=not_comparable "
        'reason="engineering_analytics_delta_missing"',
        "CODE_PUBLICATION_DIFF_LIMITATION fixture_limit",
    ]


def test_run_code_publication_diff_json_and_abstention_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    result = _ready_code_publication_cli_result(tmp_path)
    monkeypatch.setattr(
        code_publication_diff,
        "compare_code_publications",
        lambda *_args: result,
    )
    args = argparse.Namespace(
        code_publication_diff=str(tmp_path / "baseline"),
        state_directory=tmp_path / "current",
        code_json=True,
    )

    assert cli_code.run_code_publication_diff(args) == 0
    assert json.loads(capsys.readouterr().out) == result.as_payload()

    result.status = "abstained"
    result.reason = "fixture_unavailable"
    result.as_payload = lambda: {
        "kind": "code-publication-diff",
        "status": "abstained",
        "reason": "fixture_unavailable",
    }
    assert cli_code.run_code_publication_diff(args) == 2
    assert json.loads(capsys.readouterr().out) == result.as_payload()

    args.code_json = False
    assert cli_code.run_code_publication_diff(args) == 2
    assert capsys.readouterr().out == (
        "CODE_PUBLICATION_DIFF status=abstained reason=fixture_unavailable "
        f"baseline={json.dumps(result.baseline_database, ensure_ascii=True)} "
        f"current={json.dumps(result.current_database, ensure_ascii=True)}\n"
    )


def test_run_code_publication_diff_propagates_cancellation_and_maps_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    args = argparse.Namespace(
        code_publication_diff=str(tmp_path / "baseline"),
        state_directory=tmp_path / "current",
        code_json=False,
    )
    cancellation = KeyboardInterrupt("cancel publication diff")

    def cancel(*_args, **_kwargs):
        raise cancellation

    monkeypatch.setattr(code_publication_diff, "compare_code_publications", cancel)
    with pytest.raises(KeyboardInterrupt) as raised:
        cli_code.run_code_publication_diff(args)
    assert raised.value is cancellation
    cancelled_output = capsys.readouterr()
    assert cancelled_output.out == ""
    assert cancelled_output.err == ""

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(code_publication_diff, "compare_code_publications", fail)
    assert cli_code.run_code_publication_diff(args) == 2
    failed_output = capsys.readouterr()
    assert failed_output.out == ""
    assert failed_output.err == ("ERROR code-publication-diff RuntimeError: fixture failure\n")


@pytest.mark.parametrize(
    "missing",
    (
        "baseline",
        "current",
        "calls",
        "hotspots",
        "probable_dead_delta",
        "external_evidence",
        "architecture",
        "test_coverage",
        "digest",
    ),
)
def test_run_code_publication_diff_rejects_incomplete_human_ready_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    result = _ready_code_publication_cli_result(tmp_path)
    setattr(result, missing, None)
    monkeypatch.setattr(
        code_publication_diff,
        "compare_code_publications",
        lambda *_args: result,
    )

    assert (
        cli_code.run_code_publication_diff(
            argparse.Namespace(
                code_publication_diff=str(tmp_path / "baseline"),
                state_directory=tmp_path / "current",
                code_json=False,
            )
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ("ERROR code-publication-diff RuntimeError: ready result is incomplete\n")


def test_code_publication_diff_human_surfaces_bounded_architecture_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from _04_Nucleo_Operativo import code_publication_diff

    modules = tuple(
        argparse.Namespace(
            module_id=f"module_{index:02d}",
            cognitive_complexity_delta=1.0,
            fan_in_delta=1,
            fan_out_delta=0,
            baseline_cycle_ids=(),
            current_cycle_ids=(),
            baseline_contract_ids=(),
            current_contract_ids=(),
        )
        for index in range(21)
    )
    architecture = SimpleNamespace(
        status="ready",
        reason=None,
        modules=modules,
        added_failed_contracts=("layers",),
        resolved_failed_contracts=("legacy",),
        added_cycles=(("app", "core"),),
        resolved_cycles=(("old", "cycle"),),
        displaced_complexity=(
            SimpleNamespace(
                target_module="app",
                target_decrease=2.0,
                recipient_modules=("core",),
                recipient_increase=3.0,
                import_relationships=("current:app->core:both",),
            ),
        ),
        architecture_contracts_not_degraded="failed",
        no_new_import_cycles="failed",
        module_complexity_not_displaced="failed",
    )
    result = SimpleNamespace(
        baseline_database=str(tmp_path / "baseline" / "code.sqlite3"),
        current_database=str(tmp_path / "current" / "code.sqlite3"),
        status="ready",
        reason=None,
        baseline=SimpleNamespace(resolved_call_edges=2, call_edges=3),
        current=SimpleNamespace(resolved_call_edges=3, call_edges=3),
        calls=SimpleNamespace(
            common_call_sites=3,
            baseline_only_call_sites=0,
            current_only_call_sites=0,
            newly_resolved=1,
            corrected=0,
            lost=0,
        ),
        hotspots=SimpleNamespace(common=1, added=0, removed=0, changed_evidence=0),
        probable_dead_delta=0,
        external_evidence=SimpleNamespace(
            status="ready",
            common=1,
            added=0,
            resolved=0,
            gate="passed",
        ),
        digest=SimpleNamespace(xxh3_128="digest"),
        analysis_profile="full",
        verdict="mixed",
        providers=(
            SimpleNamespace(
                provider_id="mypy-trusted-project",
                status="ready",
                common=1,
                added=0,
                resolved=0,
                relocated=2,
                gate="passed",
            ),
        ),
        architecture=architecture,
        test_coverage=CoverageComparison(
            status="comparable",
            reason=None,
            baseline_suite_signature="suite",
            current_suite_signature="suite",
            executable_lines_delta=1,
            covered_lines_delta=2,
            missing_lines_delta=-1,
            branch_exits_delta=0,
            covered_branch_exits_delta=1,
            missing_branch_exits_delta=-1,
            line_coverage_percent_delta=1.5,
            branch_coverage_percent_delta=2.0,
            gates=(
                CoverageGateEvaluation("line_coverage_not_degraded", "passed", None),
                CoverageGateEvaluation("branch_coverage_not_degraded", "passed", None),
            ),
        ),
        limitations=(),
    )
    monkeypatch.setattr(
        code_publication_diff,
        "compare_code_publications",
        lambda *_args, **_kwargs: result,
    )

    exit_code = cli_code.run_code_publication_diff(
        argparse.Namespace(
            code_publication_diff=str(tmp_path / "baseline"),
            state_directory=tmp_path / "current",
            code_json=False,
        )
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "CODE_PUBLICATION_DIFF_ARCHITECTURE status=ready module_deltas=21" in output
    assert "added_failed_contracts=1 resolved_failed_contracts=1" in output
    assert "contracts_gate=failed cycles_gate=failed displacement_gate=failed" in output
    assert 'added=["layers"] resolved=["legacy"]' in output
    assert "CODE_PUBLICATION_DIFF_ARCHITECTURE_CYCLES" in output
    assert output.count("CODE_PUBLICATION_DIFF_ARCHITECTURE_MODULE ") == 20
    assert "module_examples_omitted=1" in output
    assert 'CODE_PUBLICATION_DIFF_ARCHITECTURE_DISPLACEMENT target="app"' in output
    assert "CODE_PUBLICATION_DIFF_COVERAGE status=comparable line_delta=1.5" in output
    assert "covered_lines_delta=2 missing_lines_delta=-1" in output
    assert output.count("CODE_PUBLICATION_DIFF_COVERAGE_GATE ") == 2
    assert (
        "CODE_PUBLICATION_DIFF_PROVIDER id=mypy-trusted-project status=ready common=1 "
        "added=0 resolved=0 relocated=2 gate=passed"
    ) in output


def test_semantic_cli_accepts_code_as_an_explicit_text_source() -> None:
    args = build_parser().parse_args(("--semantic-index", "text", "--semantic-source", "code"))

    validate_arguments(args)

    assert args.semantic_source == ["code"]


def test_code_semantic_search_accepts_its_model_runtime_options(
    tmp_path: Path,
) -> None:
    model_cache = tmp_path / "models"
    args = _validated(
        "--code-search",
        "protección diferencial",
        "--code-search-mode",
        "semantic",
        "--semantic-model-cache",
        str(model_cache),
        "--semantic-threads",
        "2",
    )

    assert args.semantic_model_cache == model_cache
    assert args.semantic_threads == 2

    lexical_only = build_parser().parse_args(
        (
            "--code-search",
            "protección diferencial",
            "--code-search-mode",
            "fts",
            "--semantic-model-cache",
            str(model_cache),
        )
    )
    with pytest.raises(SystemExit, match="semantic options require"):
        validate_arguments(lexical_only)


def test_code_json_is_safe_on_a_cp1252_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    monkeypatch.setattr(cli_code.sys, "stdout", stream)

    cli_code._emit({"snippet": "señal 🚀"}, json_output=True)
    stream.flush()
    payload = raw.getvalue().decode("cp1252")

    assert json.loads(payload) == {"snippet": "señal 🚀"}
    assert "\\ud83d\\ude80" in payload.lower()


def test_explicit_code_semantic_search_abstains_with_exact_coverage_gap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "state"
    initialize_code_state(state_directory / "code.sqlite3")
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(state_directory),
            "--code-search",
            "dónde se valida el acceso",
            "--code-search-mode",
            "semantic",
        ]
    )
    validate_arguments(args)

    assert dispatch_direct(args) == 2
    output = capsys.readouterr().out
    assert "CODE_SEARCH_CHANNEL name=semantic available=0" in output
    assert "reason=semantic_state_missing" in output
    assert "calibration=uncalibrated_similarity" in output


# endregion [02]
