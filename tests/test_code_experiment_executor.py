from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_experiment_executor import (
    CodeExperimentGateOutcome,
    CodeExperimentOutcome,
    CodeExperimentReceipt,
    parse_code_experiment_receipt_payload,
)
from _04_Nucleo_Operativo.code_experiment_planner import CodeExperimentProposal
from _04_Nucleo_Operativo.code_invariant_contracts import RUNTIME_SCENARIOS, runtime_scenario
from _04_Nucleo_Operativo.external_evidence_models import (
    ExternalProviderMetric,
    external_metric_identity,
)


def _proposal() -> CodeExperimentProposal:
    from _04_Nucleo_Operativo.code_analysis_epistemics import analysis_identity
    from _04_Nucleo_Operativo.code_experiment_planner import experiment_template

    template = experiment_template("capability.public_route_acceptance")
    values = {
        "evaluation_id": "evaluation:fixture",
        "evaluation_binding_fingerprint": "evaluation-binding:fixture",
        "question_id": "capability.route_reaches_user_visible_outcome",
        "subject_key": "capability:route:text",
        "selected_action_id": "exercise_text_capability_from_public_entrypoint",
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
        "missing_requirement_ids": ("public_acceptance_scenario",),
        "alternative_action_ids": (),
        "planning_status": "planned",
        "reason": "cheapest_registered_discriminating_experiment_selected",
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeExperimentProposal(
        proposal_id=analysis_identity(
            "code-experiment-proposal-v2",
            {key: value for key, value in values.items() if key != "evaluation_id"},
        ),
        **values,  # type: ignore[arg-type]
    )


def _receipt(
    *,
    outcome: str = "passed",
    provider_status: str = "completed",
    observed_outcomes: int | None = None,
):
    from _04_Nucleo_Operativo.code_analysis_epistemics import analysis_identity

    proposal = _proposal()
    scenario_specs = tuple(runtime_scenario(item) for item in proposal.scenario_ids)
    scenarios = tuple(item.scenario_id for item in scenario_specs)
    nodeids = tuple(nodeid for item in scenario_specs for nodeid in item.test_nodeids)
    outcomes = tuple(
        CodeExperimentOutcome(
            item.scenario_id,
            item.test_nodeids,
            outcome,  # type: ignore[arg-type]
            tuple(f"relation:{index}:{ordinal}" for ordinal, _ in enumerate(item.test_nodeids)),
        )
        for index, item in enumerate(scenario_specs)
    )
    if observed_outcomes is not None:
        outcomes = outcomes[:observed_outcomes]
    gate_status = "not_evaluated" if outcome == "skipped" else outcome
    gate_outcomes = tuple(
        CodeExperimentGateOutcome(
            gate.gate_id,
            scenario.scenario_id,
            gate.test_nodeids,
            gate_status,  # type: ignore[arg-type]
            tuple(
                f"gate-relation:{gate.gate_id}:{index}" for index, _ in enumerate(gate.test_nodeids)
            ),
            {
                "passed": "all_bound_test_contracts_passed",
                "failed": "one_or_more_bound_test_contracts_failed",
                "not_evaluated": "bound_test_contracts_not_all_observed_as_terminal_pass_or_fail",
            }[gate_status],
        )
        for scenario in scenario_specs
        for gate in scenario.gate_specs
    )
    status = (
        "abstained"
        if provider_status != "completed" or outcome == "skipped"
        else "failed"
        if outcome != "passed"
        else "passed"
    )
    values = {
        "status": status,
        "reason": "provider_failed" if status == "abstained" else None,
        "policy_id": "allowlisted-measured-gates-trusted-deep-v4",
        "proposal_id": proposal.proposal_id,
        "template_id": proposal.template_id,
        "template_version": proposal.template_version,
        "runner_kind": proposal.runner_kind,
        "source_root": "/fixture/repository",
        "source_version": "source-fixture",
        "source_manifest_digest": "manifest:fixture",
        "code_database_digest_before": "digest:same",
        "code_database_digest_after": "digest:same",
        "code_database_unchanged": True,
        "configuration_signature": "config:fixture",
        "scenario_registry_fingerprint": "registry:fixture",
        "provider_id": "pytest-coverage-trusted-deep",
        "provider_schema": "neocortex.pytest-coverage-trusted-deep/v1",
        "provider_status": provider_status,
        "provider_execution": "full",
        "provider_input_signature": "input:fixture",
        "provider_result_digest": "result:fixture",
        "selected_scenarios": scenarios,
        "selected_nodeids": nodeids,
        "outcomes": outcomes,
        "gate_outcomes": gate_outcomes,
        "passed": sum(item.outcome == "passed" for item in outcomes),
        "failed": sum(item.outcome == "failed" for item in outcomes),
        "skipped": sum(item.outcome == "skipped" for item in outcomes),
        "duration_ms": 123,
        "process_invocations": 5,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "limitations": (
            "receipt_proves_selected_test_outcomes_not_a_question_conclusion_or_formal_proof",
            "coverage_is_main_process_only",
            "code_database_unchanged_uses_identity_sidecar_fence_and_bounded_content_anchors",
            "source_input_is_verified_before_and_after_but_corpus_and_other_state_are_not_guarded",
            "process_death_scenario_is_not_power_loss",
            "no_product_mutation_authority",
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    identity_values = dict(values)
    identity_values["duration_ms"] = 0
    identity_values["outcomes"] = tuple(asdict(item) for item in outcomes)
    identity_values["gate_outcomes"] = tuple(asdict(item) for item in gate_outcomes)
    return CodeExperimentReceipt(
        receipt_id=analysis_identity("code-experiment-receipt-v3", identity_values),
        **values,  # type: ignore[arg-type]
    )


def test_receipt_round_trip_preserves_exact_scenario_outcomes() -> None:
    receipt = _receipt()

    assert receipt.status == "passed"
    assert receipt.passed == len(receipt.selected_scenarios)
    assert receipt.failed == receipt.skipped == 0
    assert receipt.code_database_unchanged
    assert receipt.gate_outcomes
    assert all(item.status == "passed" for item in receipt.gate_outcomes)
    assert receipt.mutation_authority is False
    assert (
        parse_code_experiment_receipt_payload(json.loads(json.dumps(receipt.as_payload())))
        == receipt
    )


def test_failure_and_provider_abstention_remain_distinct() -> None:
    failed = _receipt(outcome="failed")
    abstained = _receipt(provider_status="failed", observed_outcomes=0)

    assert failed.status == "failed"
    assert failed.failed == len(failed.selected_scenarios)
    assert abstained.status == "abstained"
    assert abstained.reason == "provider_failed"
    assert abstained.outcomes == ()
    assert (
        parse_code_experiment_receipt_payload(json.loads(json.dumps(abstained.as_payload())))
        == abstained
    )


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
            code_database_unchanged=False,
            code_database_digest_after="digest:changed",
        )

    with pytest.raises(ValueError, match="derived from execution"):
        replace(
            receipt,
            status="passed",
            gate_outcomes=receipt.gate_outcomes[:-1],
        )
    with pytest.raises(ValueError, match="advisory and non-mutating"):
        replace(receipt, mutation_authority=True)  # type: ignore[arg-type]


def test_receipt_rejects_forged_provider_and_template_selection() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="provider identity"):
        replace(receipt, provider_id="pytest-delete-production")
    with pytest.raises(ValueError, match="provider status"):
        replace(receipt, provider_status="ready")
    with pytest.raises(ValueError, match="selection is not derived"):
        replace(receipt, selected_scenarios=receipt.selected_scenarios[:-1])


def test_outcome_cannot_claim_an_unregistered_nodeid() -> None:
    scenario = RUNTIME_SCENARIOS[0]
    with pytest.raises(ValueError, match="declared scenario"):
        CodeExperimentOutcome(
            scenario.scenario_id,
            ("tests/test_fake.py::test_delete_production",),
            "passed",
            ("relation:fake",),
        )


def test_wire_rejects_free_form_runner_and_missing_outcomes() -> None:
    receipt = _receipt()
    payload = json.loads(json.dumps(receipt.as_payload()))
    payload["runner_kind"] = "shell"
    with pytest.raises(ValueError, match="runner"):
        parse_code_experiment_receipt_payload(payload)

    payload = json.loads(json.dumps(receipt.as_payload()))
    payload["outcomes"].pop()
    with pytest.raises(ValueError, match="counts do not match"):
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


def test_code_database_fence_is_fixed_cost_without_read_bytes(
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
    assert first.startswith("neocortex.code-database-identity-fence/v1:xxh3_128:")


def test_code_database_fence_accepts_large_sparse_history_and_detects_mutation(
    tmp_path: Path,
) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor

    database = tmp_path / "code.sqlite3"
    with database.open("wb") as stream:
        stream.write(b"SQLite format 3\x00")
        stream.seek(5 * 1024 * 1024 * 1024)
        stream.write(b"tail")

    before = executor._file_digest(database)
    with database.open("r+b", buffering=0) as stream:
        stream.seek(64)
        stream.write(b"changed")
    after = executor._file_digest(database)

    assert before != after


def test_code_database_fence_rejects_an_active_wal(tmp_path: Path) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor

    database = tmp_path / "code.sqlite3"
    database.write_bytes(b"SQLite format 3\x00fixture")
    Path(f"{database}-wal").write_bytes(b"active")

    with pytest.raises(ValueError, match="cannot be fenced"):
        executor._file_digest(database)


def test_complete_aggregate_counts_are_recovered_when_relation_payload_is_bounded() -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor

    def metric(name: str, value: int) -> ExternalProviderMetric:
        return ExternalProviderMetric(
            external_metric_identity(
                "pytest-coverage-trusted-deep",
                subject_kind="run",
                subject_key="coverage-run:fixture",
                category="coverage",
                metric_name=name,
                unit="count",
            ),
            "run",
            "coverage-run:fixture",
            "coverage",
            name,
            float(value),
            "count",
        )

    publication = type(
        "Publication",
        (),
        {
            "metrics": (
                metric("tests_selected", 4),
                metric("tests_passed", 4),
                metric("tests_failed", 0),
                metric("tests_skipped", 0),
            )
        },
    )()

    assert executor._provider_test_counts(publication) == (4, 4, 0, 0)

    forged = type(
        "Publication",
        (),
        {"metrics": (*publication.metrics, metric("tests_passed", 4))},
    )()
    with pytest.raises(ValueError, match="not canonical"):
        executor._provider_test_counts(forged)


@pytest.mark.parametrize(
    ("published", "after"),
    (("source:changed", "source:exact"), ("source:exact", "source:changed")),
)
def test_experiment_rejects_outcomes_if_the_exact_source_input_changes(
    published: str,
    after: str,
) -> None:
    import _04_Nucleo_Operativo.code_experiment_executor as executor

    with pytest.raises(ValueError, match="source input changed"):
        executor._require_stable_source_input("source:exact", published, after)

    executor._require_stable_source_input(
        "source:exact",
        "source:exact",
        "source:exact",
    )
