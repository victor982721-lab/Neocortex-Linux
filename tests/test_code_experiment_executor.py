from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.code.code_experiment_executor import (
    CodeExperimentGateOutcome,
    CodeExperimentOutcome,
    CodeExperimentReceipt,
    parse_code_experiment_receipt_payload,
)
from neocortex.code.code_experiment_planner import CodeExperimentProposal
from neocortex.code.code_invariant_contracts import RUNTIME_SCENARIOS, runtime_scenario
from neocortex.code.external_evidence_models import (
    ExternalProviderMetric,
    ExternalProviderRelation,
    external_metric_identity,
    external_relation_identity,
)


def _proposal() -> CodeExperimentProposal:
    from neocortex.code.code_analysis_epistemics import analysis_identity
    from neocortex.code.code_experiment_planner import experiment_template

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
    from neocortex.code.code_analysis_epistemics import analysis_identity

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
    payload = receipt.as_payload()

    assert receipt.status == "passed"
    assert receipt.passed == len(receipt.selected_scenarios)
    assert receipt.failed == receipt.skipped == 0
    assert receipt.code_database_unchanged
    assert receipt.gate_outcomes
    assert all(item.status == "passed" for item in receipt.gate_outcomes)
    assert receipt.mutation_authority is False
    assert parse_code_experiment_receipt_payload(json.loads(json.dumps(payload))) == receipt
    assert payload["schema"] == "neocortex.code-experiment-receipt/v3"
    assert "evidence_mode" not in payload


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
    from neocortex.code.code_experiment_executor import execute_code_experiment

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
    import neocortex.code.code_experiment_executor as executor

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
    import neocortex.code.code_experiment_executor as executor

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
    import neocortex.code.code_experiment_executor as executor

    database = tmp_path / "code.sqlite3"
    database.write_bytes(b"SQLite format 3\x00fixture")
    Path(f"{database}-wal").write_bytes(b"active")

    with pytest.raises(ValueError, match="cannot be fenced"):
        executor._file_digest(database)


def test_complete_aggregate_counts_are_recovered_when_relation_payload_is_bounded() -> None:
    import neocortex.code.code_experiment_executor as executor

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


def test_retention_parameter_variants_have_exact_terminal_gate_evidence() -> None:
    import neocortex.code.code_experiment_executor as executor

    scenario = runtime_scenario("retention.durable_hold_safety")
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                "pytest-coverage-trusted-deep",
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:retention-fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:retention-fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "retention-fixture",
            },
        )
        for nodeid in scenario.test_nodeids
    )
    publication = SimpleNamespace(relations=relations)

    outcomes = executor._outcomes(publication, (scenario.scenario_id,))
    gates = executor._gate_outcomes(publication, (scenario.scenario_id,))

    assert len(outcomes) == 1
    assert outcomes[0].outcome == "passed"
    assert all(item.status == "passed" for item in gates)
    incomplete = next(
        item
        for item in gates
        if item.gate_id == "incomplete_review_receipt_or_schema_drift_fails_closed_without_mutation"
    )
    assert len(incomplete.test_nodeids) == 7
    assert all("[" in nodeid for nodeid in incomplete.test_nodeids[:6])


def test_public_cli_scenario_has_exact_measured_gates_and_fails_closed_if_incomplete() -> None:
    import neocortex.code.code_experiment_executor as executor
    from neocortex.code.code_experiment_planner import experiment_template

    scenario = runtime_scenario("interfaces.public_cli_and_static_surface")
    template = experiment_template("interfaces.public_cli_contract_acceptance")
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                "pytest-coverage-trusted-deep",
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:public-cli-fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:public-cli-fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "public-cli-fixture",
            },
        )
        for nodeid in scenario.test_nodeids
    )

    publication = SimpleNamespace(relations=relations)
    outcomes = executor._outcomes(publication, (scenario.scenario_id,))
    gates = executor._gate_outcomes(publication, (scenario.scenario_id,))

    assert template.authority == "advisory"
    assert template.mutation_authority is False
    assert outcomes[0].outcome == "passed"
    assert tuple(item.gate_id for item in gates) == template.acceptance_gates
    assert tuple(len(item.relation_ids) for item in gates) == (5, 3, 4, 10, 4)
    assert all(item.status == "passed" for item in gates)

    incomplete = SimpleNamespace(relations=relations[:-1])
    assert executor._outcomes(incomplete, (scenario.scenario_id,)) == ()
    incomplete_gates = executor._gate_outcomes(incomplete, (scenario.scenario_id,))
    assert any(item.status == "not_evaluated" for item in incomplete_gates)


def test_knowledge_health_scenario_has_exact_gates_and_fails_closed_if_incomplete() -> None:
    import neocortex.code.code_experiment_executor as executor
    from neocortex.code.code_experiment_planner import experiment_template

    scenario = runtime_scenario("knowledge.asset_health_causal_acceptance")
    template = experiment_template("knowledge.asset_health_causal_acceptance")
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                "pytest-coverage-trusted-deep",
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:knowledge-health-fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:knowledge-health-fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "knowledge-health-fixture",
            },
        )
        for nodeid in scenario.test_nodeids
    )

    publication = SimpleNamespace(relations=relations)
    outcomes = executor._outcomes(publication, (scenario.scenario_id,))
    gates = executor._gate_outcomes(publication, (scenario.scenario_id,))

    assert template.authority == "advisory"
    assert template.mutation_authority is False
    assert outcomes[0].outcome == "passed"
    assert tuple(item.gate_id for item in gates) == template.acceptance_gates
    assert tuple(len(item.relation_ids) for item in gates) == (1, 7, 2, 2)
    assert all(item.status == "passed" for item in gates)

    incomplete = SimpleNamespace(relations=relations[:-1])
    assert executor._outcomes(incomplete, (scenario.scenario_id,)) == ()
    incomplete_gates = executor._gate_outcomes(incomplete, (scenario.scenario_id,))
    assert any(item.status == "not_evaluated" for item in incomplete_gates)


def test_pdf_health_scenario_has_exact_gates_and_fails_closed_if_incomplete() -> None:
    import neocortex.code.code_experiment_executor as executor
    from neocortex.code.code_experiment_planner import experiment_template

    scenario = runtime_scenario("knowledge.pdf_asset_health_causal_acceptance")
    template = experiment_template("knowledge.pdf_asset_health_causal_acceptance")
    relations = tuple(
        ExternalProviderRelation(
            external_relation_identity(
                "pytest-coverage-trusted-deep",
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:knowledge-pdf-health-fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:knowledge-pdf-health-fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "knowledge-pdf-health-fixture",
            },
        )
        for nodeid in scenario.test_nodeids
    )

    publication = SimpleNamespace(relations=relations)
    outcomes = executor._outcomes(publication, (scenario.scenario_id,))
    gates = executor._gate_outcomes(publication, (scenario.scenario_id,))

    assert template.authority == "advisory"
    assert template.mutation_authority is False
    assert outcomes[0].outcome == "passed"
    assert tuple(item.gate_id for item in gates) == template.acceptance_gates
    assert tuple(len(item.relation_ids) for item in gates) == (5, 3, 3, 1)
    assert all(item.status == "passed" for item in gates)

    incomplete = SimpleNamespace(relations=relations[:-1])
    assert executor._outcomes(incomplete, (scenario.scenario_id,)) == ()
    incomplete_gates = executor._gate_outcomes(incomplete, (scenario.scenario_id,))
    assert any(item.status == "not_evaluated" for item in incomplete_gates)


@pytest.mark.parametrize(
    ("published", "after"),
    (("source:changed", "source:exact"), ("source:exact", "source:changed")),
)
def test_experiment_rejects_outcomes_if_the_exact_source_input_changes(
    published: str,
    after: str,
) -> None:
    import neocortex.code.code_experiment_executor as executor

    with pytest.raises(ValueError, match="source input changed"):
        executor._require_stable_source_input("source:exact", published, after)

    executor._require_stable_source_input(
        "source:exact",
        "source:exact",
        "source:exact",
    )


def _attestation_relations(
    proposal: CodeExperimentProposal,
) -> tuple[ExternalProviderRelation, ...]:
    return tuple(
        ExternalProviderRelation(
            external_relation_identity(
                "pytest-coverage-trusted-deep",
                relation_kind="declared_test_outcome",
                source_kind="contract",
                source_key=f"pytest-nodeid:{nodeid}",
                target_kind="run",
                target_key="coverage-run:attestation-fixture",
            ),
            "declared_test_outcome",
            "contract",
            f"pytest-nodeid:{nodeid}",
            "run",
            "coverage-run:attestation-fixture",
            confidence=1.0,
            metadata={
                "nodeid": nodeid,
                "outcome": "passed",
                "suite_selection": "full",
                "measurement_complete": True,
                "content_executed": True,
                "tool_versions": {"coverage": "7.14.1", "pytest": "9.0.2"},
                "suite_signature": "coverage-suite:fixture",
                "code_input_signature": "coverage-code-input:fixture",
                "support_signature": "coverage-support:fixture",
                "configuration_signature": "coverage-configuration:fixture",
                "publication_input_signature": "coverage-input:fixture",
                "subprocess_coverage": False,
                "coverage_scope": "main_process_only",
                "claim_scope": "exact_selected_test_execution_outcome",
                "assertion_or_invariant_proof": False,
                "measurement_scope_signature": "attestation-fixture",
            },
        )
        for scenario_id in proposal.scenario_ids
        for nodeid in runtime_scenario(scenario_id).test_nodeids
    )


@pytest.mark.parametrize(
    ("omit_last_relation", "expected_status"), ((False, "passed"), (True, "abstained"))
)
def test_attestation_reuses_exact_current_coverage_without_relaunching_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omit_last_relation: bool,
    expected_status: str,
) -> None:
    import neocortex.code.code_experiment_executor as executor
    import neocortex.code.external_evidence_store as evidence_store
    from neocortex.code.code_external_evidence import ExternalEvidenceFile
    from neocortex.code.external_deep_coverage import (
        DEEP_COVERAGE_PROVIDER_SCHEMA,
        PYTEST_COVERAGE_PROVIDER_ID,
    )
    from neocortex.code.external_evidence_models import (
        ExternalProviderAttestation,
        ExternalRunInput,
        external_provider_result_digest,
        external_root_identity,
    )

    source = tmp_path / "source"
    source.mkdir()
    observed = source / "module.py"
    observed.write_text("VALUE = 1\n", encoding="utf-8")
    database = tmp_path / "code.sqlite3"
    database.write_bytes(b"SQLite format 3\x00attestation-fixture")
    proposal = _proposal()
    all_relations = _attestation_relations(proposal)
    relations = all_relations[:-1] if omit_last_relation else all_relations
    selected_count = len(relations)
    metrics = tuple(
        ExternalProviderMetric(
            external_metric_identity(
                PYTEST_COVERAGE_PROVIDER_ID,
                subject_kind="run",
                subject_key="coverage-run:attestation-fixture",
                category="coverage",
                metric_name=name,
                unit="count",
            ),
            "run",
            "coverage-run:attestation-fixture",
            "coverage",
            name,
            float(value),
            "count",
        )
        for name, value in (
            ("tests_selected", selected_count),
            ("tests_passed", selected_count),
            ("tests_failed", 0),
            ("tests_skipped", 0),
        )
    )
    files = (
        ExternalEvidenceFile(
            1,
            str(observed),
            "module.py",
            observed.stat().st_size,
            observed.stat().st_mtime_ns,
            "raw-xxh3-128",
            "raw-xxh3-64",
        ),
    )
    attestation = ExternalProviderAttestation(
        analysis_run_id=41,
        processing_signature="processing-signature:fixture",
        provider_id=PYTEST_COVERAGE_PROVIDER_ID,
        provider_schema=DEEP_COVERAGE_PROVIDER_SCHEMA,
        profile="trusted-deep",
        tool_run_id=73,
        effective_tool_run_id=72,
        tool_name="pytest+coverage",
        tool_version="pytest 9+coverage 7",
        tool_status="skipped",
        execution="cache_replay",
        observed_root=str(source.resolve()),
        root_identity=external_root_identity(source),
        input_signature="coverage-input:fixture",
        descriptor_configuration_signature="coverage-descriptor:fixture",
        environment_signature="coverage-environment:fixture",
        comparability_signature="coverage-comparability:fixture",
        result_digest=external_provider_result_digest((), metrics, relations),
        portable_publication_id="coverage-publication:fixture",
        coverage_complete=True,
        content_executed=True,
        eligible_files=1,
        covered_files=1,
        counters={"process_invocations": 23},
        inputs=(ExternalRunInput.from_file(files[0], covered=True),),
        metrics=metrics,
        relations=relations,
    )

    monkeypatch.setattr(
        executor,
        "readonly_code_database",
        lambda _path: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr(executor, "_require_current_analysis_run", lambda *_a, **_k: None)
    monkeypatch.setattr(executor, "read_external_evidence_files", lambda *_a: files)
    monkeypatch.setattr(
        evidence_store,
        "read_external_provider_attestation",
        lambda *_a, **_k: attestation,
    )
    monkeypatch.setattr(
        executor,
        "PytestCoverageTrustedDeepProvider",
        lambda *_a, **_k: pytest.fail("attestation must not launch a provider"),
    )

    (receipt,) = executor.attest_code_experiments(
        (proposal,),
        source_root=source,
        code_database_path=database,
        source_version="processing-signature:fixture",
        analysis_run_id=41,
        provider_tool_run_id=73,
        provider_effective_tool_run_id=72,
        provider_suite_selection="full",
        provider_configuration_signature="coverage-configuration:fixture",
        provider_suite_signature="coverage-suite:fixture",
        provider_measurement_scope_signature="attestation-fixture",
    )

    assert receipt.status == expected_status
    assert receipt.process_invocations == 0
    assert receipt.stdout_bytes == receipt.stderr_bytes == 0
    assert receipt.provider_execution == "cache_replay"
    assert receipt.analysis_run_id == 41
    assert receipt.provider_tool_run_id == 73
    assert receipt.provider_effective_tool_run_id == 72
    assert receipt.provider_tool_status == "skipped"
    assert receipt.as_payload()["schema"] == "neocortex.code-experiment-receipt/v4"
    assert parse_code_experiment_receipt_payload(receipt.as_payload()) == receipt
    assert "exact_current_trusted_deep_test_relations_reused_without_test_reexecution" in (
        receipt.limitations
    )
    if omit_last_relation:
        assert receipt.reason == "published_coverage_outcomes_incomplete"
    else:
        assert receipt.reason is None
        assert receipt.passed == len(proposal.scenario_ids)


def test_attestation_rejects_forged_declared_test_relation_contracts() -> None:
    import neocortex.code.code_experiment_executor as executor

    relations = _attestation_relations(_proposal())

    def metric(name: str, value: int) -> ExternalProviderMetric:
        return ExternalProviderMetric(
            external_metric_identity(
                "pytest-coverage-trusted-deep",
                subject_kind="run",
                subject_key="coverage-run:attestation-fixture",
                category="coverage",
                metric_name=name,
                unit="count",
            ),
            "run",
            "coverage-run:attestation-fixture",
            "coverage",
            name,
            float(value),
            "count",
        )

    metrics = (
        metric("tests_selected", len(relations)),
        metric("tests_passed", len(relations)),
        metric("tests_failed", 0),
        metric("tests_skipped", 0),
    )
    first, *remaining = relations
    forged = (
        replace(first, portable_relation_id="forged-relation-id"),
        replace(first, target_key="coverage-run:wrong-scope"),
        replace(first, confidence=0.5),
        replace(first, metadata={**first.metadata, "claim_scope": "formal-proof"}),
        replace(first, metadata={**first.metadata, "unbound_claim": True}),
    )
    for candidate in forged:
        publication = SimpleNamespace(
            provider_id="pytest-coverage-trusted-deep",
            input_signature="coverage-input:fixture",
            metrics=metrics,
            relations=(candidate, *remaining),
        )
        with pytest.raises(ValueError, match="declared test outcome contract changed"):
            executor.validate_code_experiment_declared_test_relations(
                publication,
                suite_selection="full",
                configuration_signature="coverage-configuration:fixture",
                suite_signature="coverage-suite:fixture",
                measurement_scope_signature="attestation-fixture",
            )
