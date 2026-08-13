from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_experiment_executor import (
    CodeExperimentOutcome,
    CodeExperimentReceipt,
    parse_code_experiment_receipt_payload,
)
from _04_Nucleo_Operativo.code_experiment_planner import CodeExperimentProposal
from _04_Nucleo_Operativo.code_invariant_contracts import RUNTIME_SCENARIOS


def _proposal() -> CodeExperimentProposal:
    from _04_Nucleo_Operativo.code_analysis_epistemics import analysis_identity
    from _04_Nucleo_Operativo.code_experiment_planner import experiment_template

    template = experiment_template("analyzer.registered_invariant_scenarios")
    values = {
        "evaluation_id": "evaluation:fixture",
        "question_id": "assurance.declared_invariant_scenarios_are_observed",
        "subject_key": "invariant:fixture",
        "selected_action_id": "run_independent_invariant_scenario",
        "template_id": template.template_id,
        "template_version": template.version,
        "cost_tier": template.cost_tier,
        "estimated_attention_minutes": template.estimated_attention_minutes,
        "timeout_seconds": template.timeout_seconds,
        "max_items": template.max_items,
        "isolation": template.isolation,
        "runner_kind": template.runner_kind,
        "scenario_ids": template.scenario_ids,
        "acceptance_gates": template.acceptance_gates,
        "missing_requirement_ids": ("independent_additional_scenario_result",),
        "alternative_action_ids": (),
        "planning_status": "planned",
        "reason": "cheapest_registered_discriminating_experiment_selected",
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeExperimentProposal(
        proposal_id=analysis_identity("code-experiment-proposal-v1", values),
        **values,  # type: ignore[arg-type]
    )


def _receipt(*, outcome: str = "passed", provider_status: str = "completed"):
    from _04_Nucleo_Operativo.code_analysis_epistemics import analysis_identity

    proposal = _proposal()
    scenarios = tuple(item.scenario_id for item in RUNTIME_SCENARIOS)
    nodeids = tuple(item.test_nodeid for item in RUNTIME_SCENARIOS)
    outcomes = tuple(
        CodeExperimentOutcome(
            item.scenario_id,
            item.test_nodeid,
            outcome,  # type: ignore[arg-type]
            f"relation:{index}",
        )
        for index, item in enumerate(RUNTIME_SCENARIOS)
    )
    status = (
        "abstained"
        if provider_status != "completed"
        else "failed"
        if outcome != "passed"
        else "passed"
    )
    values = {
        "status": status,
        "reason": "provider_failed" if status == "abstained" else None,
        "policy_id": "allowlisted-trusted-deep-scenarios-v1",
        "proposal_id": proposal.proposal_id,
        "template_id": "analyzer.registered_invariant_scenarios",
        "template_version": "v1",
        "runner_kind": "trusted_deep_declared_scenarios",
        "source_root": "/fixture/repository",
        "source_version": "source-fixture",
        "source_manifest_digest": "manifest:fixture",
        "code_database_digest_before": "digest:same",
        "code_database_digest_after": "digest:same",
        "canonical_state_unchanged": True,
        "configuration_signature": "config:fixture",
        "invariant_registry_fingerprint": "registry:fixture",
        "provider_id": "pytest-coverage-trusted-deep",
        "provider_schema": "neocortex.pytest-coverage-trusted-deep/v1",
        "provider_status": provider_status,
        "provider_execution": "full",
        "provider_input_signature": "input:fixture",
        "provider_result_digest": "result:fixture",
        "selected_scenarios": scenarios,
        "selected_nodeids": nodeids,
        "outcomes": outcomes,
        "passed": sum(item.outcome == "passed" for item in outcomes),
        "failed": sum(item.outcome == "failed" for item in outcomes),
        "skipped": sum(item.outcome == "skipped" for item in outcomes),
        "duration_ms": 123,
        "process_invocations": 5,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "limitations": (
            "receipt_proves_selected_test_outcomes_not_formal_invariant_truth",
            "coverage_is_main_process_only",
            "process_death_scenario_is_not_power_loss",
            "no_product_mutation_authority",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    identity_values = dict(values)
    identity_values["duration_ms"] = 0
    identity_values["outcomes"] = tuple(asdict(item) for item in outcomes)
    return CodeExperimentReceipt(
        receipt_id=analysis_identity("code-experiment-receipt-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def test_receipt_round_trip_preserves_exact_scenario_outcomes() -> None:
    receipt = _receipt()

    assert receipt.status == "passed"
    assert receipt.passed == len(RUNTIME_SCENARIOS)
    assert receipt.failed == receipt.skipped == 0
    assert receipt.canonical_state_unchanged
    assert receipt.mutation_authority is False
    assert (
        parse_code_experiment_receipt_payload(json.loads(json.dumps(receipt.as_payload())))
        == receipt
    )


def test_failure_and_provider_abstention_remain_distinct() -> None:
    failed = _receipt(outcome="failed")
    abstained = _receipt(provider_status="failed")

    assert failed.status == "failed"
    assert failed.failed == len(RUNTIME_SCENARIOS)
    assert abstained.status == "abstained"
    assert abstained.reason == "provider_failed"


def test_receipt_identity_excludes_wall_clock_duration_but_not_evidence() -> None:
    first = _receipt()
    second = replace(first, duration_ms=999)
    assert first.receipt_id == second.receipt_id

    with pytest.raises(ValueError, match="identity"):
        replace(first, provider_result_digest="result:forged")


def test_receipt_rejects_canonical_state_change_and_status_smuggling() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="guard contradicts"):
        replace(receipt, code_database_digest_after="digest:changed")
    with pytest.raises(ValueError, match="status is not derived"):
        replace(
            receipt,
            canonical_state_unchanged=False,
            code_database_digest_after="digest:changed",
        )
    with pytest.raises(ValueError, match="advisory and non-mutating"):
        replace(receipt, mutation_authority=True)  # type: ignore[arg-type]


def test_receipt_rejects_forged_provider_and_template_selection() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="provider identity"):
        replace(receipt, provider_id="pytest-delete-production")
    with pytest.raises(ValueError, match="provider status"):
        replace(receipt, provider_status="ready")
    with pytest.raises(ValueError, match="cover selected scenarios"):
        replace(receipt, selected_scenarios=receipt.selected_scenarios[:-1])


def test_outcome_cannot_claim_an_unregistered_nodeid() -> None:
    scenario = RUNTIME_SCENARIOS[0]
    with pytest.raises(ValueError, match="declared scenario"):
        CodeExperimentOutcome(
            scenario.scenario_id,
            "tests/test_fake.py::test_delete_production",
            "passed",
            "relation:fake",
        )


def test_wire_rejects_free_form_runner_and_missing_outcomes() -> None:
    receipt = _receipt()
    payload = json.loads(json.dumps(receipt.as_payload()))
    payload["runner_kind"] = "shell"
    with pytest.raises(ValueError, match="runner"):
        parse_code_experiment_receipt_payload(payload)

    payload = json.loads(json.dumps(receipt.as_payload()))
    payload["outcomes"].pop()
    with pytest.raises(ValueError, match="cover selected scenarios"):
        parse_code_experiment_receipt_payload(payload)


def test_execution_rejects_a_source_root_different_from_the_published_manifest(
    tmp_path,
) -> None:
    from _04_Nucleo_Operativo.code_experiment_executor import execute_code_experiment

    source = tmp_path / "source"
    expected = tmp_path / "expected"
    scratch = tmp_path / "scratch"
    source.mkdir()
    expected.mkdir()
    scratch.mkdir()
    database = tmp_path / "code.sqlite3"
    database.write_bytes(b"not-reached")

    with pytest.raises(ValueError, match="manifest root"):
        execute_code_experiment(
            _proposal(),
            source_root=source,
            code_database_path=database,
            scratch_root=scratch,
            source_version="fixture",
            expected_source_root=expected,
        )


def test_code_database_digest_is_streamed_without_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor

    database = tmp_path / "code.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES('immutable')")

    original = Path.read_bytes

    def reject_database_read_bytes(path: Path) -> bytes:
        if path == database:
            raise AssertionError("Code database digest must be streamed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_database_read_bytes)
    first = executor._file_digest(database)
    second = executor._file_digest(database)

    assert first == second
    assert first.startswith("xxh3_128:")
