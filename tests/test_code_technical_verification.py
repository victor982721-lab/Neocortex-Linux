from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_architecture_questions import architecture_questions
from _04_Nucleo_Operativo.code_experiment_planner import plan_code_experiments
from _04_Nucleo_Operativo.code_experiment_store import (
    apply_code_experiment_receipts,
    read_code_experiment_receipts,
    record_code_experiment_receipt,
)
from _04_Nucleo_Operativo.code_change_evolution_analysis import (
    analyze_code_change_evolution,
    expected_code_change_evolution_questions,
)
from _04_Nucleo_Operativo.code_state_projection_analysis import (
    analyze_text_semantic_projection,
    state_projection_questions,
)
from _04_Nucleo_Operativo.code_retention_analysis import (
    analyze_code_retention,
    retention_questions,
)
from _04_Nucleo_Operativo.code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
    security_dependency_questions,
)
from _04_Nucleo_Operativo.code_supply_chain_analysis import read_code_supply_chain_analysis
from _04_Nucleo_Operativo.code_technical_verification import (
    build_code_technical_verification,
    parse_code_technical_verification_payload,
)
from tests.test_code_change_evolution_analysis import _build_transition
from tests.test_code_architecture_questions import _ready_architecture
from tests.test_code_experiment_store import _database, _receipt
from tests.test_code_state_projection_analysis import _build_state
from tests.test_code_retention_analysis import REFERENCE_NS, SOURCE_VERSION, _initialized_state
from tests.test_code_supply_chain_analysis import (
    _NOW_UTC as SUPPLY_NOW_UTC,
    _database as _supply_chain_database,
)


def _closed_semantic_projection(
    tmp_path: Path,
    *,
    aligned: bool = True,
):
    document_state = tmp_path / "document-state"
    _build_state(document_state, include_second_revision=not aligned)
    analysis = analyze_text_semantic_projection(
        document_state,
        source_version="snapshot:fixture",
    )
    specs, evaluations = state_projection_questions(analysis, rank=1)
    base_plan = plan_code_experiments(specs, evaluations)
    proposal = base_plan.proposals[0]
    database_root = tmp_path / "code-state"
    database_root.mkdir()
    database = _database(database_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    resolved = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=base_plan,
    )
    assert resolved == (stored,)
    projected = apply_code_experiment_receipts(
        specs,
        evaluations,
        base_plan,
        resolved,
    )
    return specs, evaluations, base_plan, resolved, projected


def _closed_schema_evolution(tmp_path: Path):
    source_database = _build_transition(tmp_path / "source", history=True)
    analysis = analyze_code_change_evolution(source_database, limit=20)
    specs, evaluations = expected_code_change_evolution_questions(analysis)
    plan = plan_code_experiments(specs, evaluations)
    proposal = next(
        item
        for item in plan.proposals
        if item.question_id == "evolution.code_owner_schema_requires_migration_review"
    )
    experiment_root = tmp_path / "experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    receipts = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert receipts == (stored,)
    projected = apply_code_experiment_receipts(specs, evaluations, plan, receipts)
    return specs, evaluations, plan, receipts, projected


def _closed_architecture_contract(tmp_path: Path):
    architecture = _ready_architecture()
    architecture = replace(
        architecture,
        gates=tuple(
            replace(item, status="passed", reason=None)
            if item.gate == "architecture_contracts"
            else item
            for item in architecture.gates
        ),
        contracts=tuple(
            replace(
                item,
                status="passed",
                violations=0,
                importer_modules=(),
                imported_modules=(),
                import_chains=(),
            )
            for item in architecture.contracts
        ),
    )
    specs, evaluations = architecture_questions(
        architecture,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank_offset=0,
    )
    plan = plan_code_experiments(specs, evaluations)
    proposal = next(
        item
        for item in plan.proposals
        if item.question_id == "architecture.declared_import_contracts_are_evaluated"
    )
    experiment_root = tmp_path / "architecture-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    receipts = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert receipts == (stored,)
    projected = apply_code_experiment_receipts(specs, evaluations, plan, receipts)
    return specs, evaluations, plan, receipts, projected


def _closed_retention_hold_projection(tmp_path: Path):
    analysis = analyze_code_retention(
        _initialized_state(tmp_path / "retention-state"),
        source_version=SOURCE_VERSION,
        reference_time_ns=REFERENCE_NS,
    )
    specs, evaluations = retention_questions(analysis, rank=1)
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    experiment_root = tmp_path / "retention-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:fixture",
    )
    receipts = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert receipts == (stored,)
    projected = apply_code_experiment_receipts(specs, evaluations, plan, receipts)
    return specs, evaluations, plan, receipts, projected


def _closed_supply_chain_projection(tmp_path: Path):
    supply_root = tmp_path / "supply"
    supply_root.mkdir()
    supply_database = _supply_chain_database(supply_root, findings=False)
    with sqlite3.connect(supply_database) as connection:
        connection.row_factory = sqlite3.Row
        analysis = read_code_supply_chain_analysis(
            connection,
            2,
            database=str(supply_database),
            now_utc=SUPPLY_NOW_UTC,
        )
    assert analysis.status == "ready"
    specs, evaluations = security_dependency_questions(
        analysis,
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank_offset=0,
    )
    plan = plan_code_experiments(specs, evaluations)
    assert plan.executable_count == 2
    experiment_root = tmp_path / "supply-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    for proposal in plan.proposals:
        record_code_experiment_receipt(
            database,
            _receipt(proposal),
            proposal,
            analysis_run_id=1,
            processing_signature="snapshot:fixture",
            review_digest="review:fixture",
        )
    receipts = read_code_experiment_receipts(
        database,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        plan=plan,
    )
    assert len(receipts) == 2
    projected = apply_code_experiment_receipts(specs, evaluations, plan, receipts)
    return specs, evaluations, plan, receipts, projected


def test_supply_chain_receipts_close_security_and_dependency_evidence(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = _closed_supply_chain_projection(tmp_path)

    assert {item.question_id for item in plan.proposals} == {
        SECURITY_EVIDENCE_QUESTION.question_id,
        DEPENDENCY_EVIDENCE_QUESTION.question_id,
    }
    assert all(
        item.template_id == "security.bounded_boundary_scenarios"
        and item.runner_kind == "trusted_deep_declared_scenarios"
        and item.scenario_ids == ("security.supply_chain_gate_controls",)
        for item in plan.proposals
    )
    assert all(item.decision_readiness == "human_review_required" for item in projected)
    assert all(item.counterevidence_status == "evaluated" for item in projected)
    assert all(
        requirement.status == "satisfied"
        for evaluation in projected
        for requirement in evaluation.requirements
    )

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.evidence_complete_evaluations == 2
    assert verification.no_change_required_count == 2
    assert verification.unresolved_count == 0
    assert {item.question_id for item in verification.reviews} == {
        SECURITY_EVIDENCE_QUESTION.question_id,
        DEPENDENCY_EVIDENCE_QUESTION.question_id,
    }
    assert {item.reason_code for item in verification.reviews} == {
        "security_provider_gates_passed_without_a_change_signal",
        "dependency_and_installed_artifact_gates_passed_without_a_change_signal",
    }


@pytest.mark.parametrize(
    ("question_id", "provider_id"),
    (
        (SECURITY_EVIDENCE_QUESTION.question_id, "pip-audit-known-vulnerabilities"),
        (DEPENDENCY_EVIDENCE_QUESTION.question_id, "installed-package-inventory"),
    ),
)
def test_passed_supply_fixture_cannot_hide_a_failed_live_provider_gate(
    tmp_path: Path,
    question_id: str,
    provider_id: str,
) -> None:
    specs, _evaluations, _plan, receipts, projected = _closed_supply_chain_projection(tmp_path)
    selected = next(item for item in projected if item.question_id == question_id)
    provider = next(
        item
        for item in selected.evidence
        if item.source_record_kind == "provider_and_gate_projection"
        and any(fact.name == "provider_id" and fact.value == provider_id for fact in item.facts)
    )
    tampered_provider = replace(
        provider,
        facts=tuple(
            replace(fact, value=1) if fact.name == "failed_gate_count" else fact
            for fact in provider.facts
        ),
    )
    tampered_evaluation = replace(
        selected,
        evidence=tuple(
            tampered_provider if item.evidence_id == provider.evidence_id else item
            for item in selected.evidence
        ),
    )
    tampered_projection = tuple(
        tampered_evaluation if item.evaluation_id == selected.evaluation_id else item
        for item in projected
    )

    verification = build_code_technical_verification(specs, tampered_projection, receipts)

    assert verification.status == "partial"
    assert verification.no_change_required_count == 1
    assert verification.unresolved_count == 1
    assert verification.gaps[0].question_id == question_id
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_semantic_process_death_receipt_closes_evidence_and_technical_review(
    tmp_path: Path,
) -> None:
    specs, _base, plan, receipts, projected = _closed_semantic_projection(tmp_path)

    proposal = plan.proposals[0]
    assert proposal.template_id == "state.semantic_process_death_recovery"
    assert proposal.runner_kind == "trusted_deep_declared_scenarios"
    assert proposal.scenario_ids == ("semantic.staging_process_death_resume",)
    assert proposal.acceptance_gates == (
        "committed_staging_prefix_survives_process_death",
        "dead_building_generation_remains_unpublished",
        "resume_publishes_complete_generation_atomically",
    )
    evaluation = projected[0]
    assert evaluation.decision_readiness == "human_review_required"
    assert evaluation.counterevidence_status == "evaluated"
    assert all(item.status == "satisfied" for item in evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.evidence_complete_evaluations == 1
    assert verification.no_change_required_count == 1
    assert verification.unresolved_count == 0
    assert verification.reviews[0].disposition == ("no_change_required_within_verified_scope")
    assert verification.reviews[0].receipt_ids == (receipts[0].receipt.receipt_id,)
    assert (
        parse_code_technical_verification_payload(json.loads(json.dumps(verification.as_payload())))
        == verification
    )


def test_passed_recovery_fixture_cannot_hide_a_live_projection_delta(tmp_path: Path) -> None:
    specs, _base, _plan, receipts, projected = _closed_semantic_projection(
        tmp_path,
        aligned=False,
    )

    verification = build_code_technical_verification(specs, projected, receipts)

    assert projected[0].decision_readiness == "human_review_required"
    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_code_schema_upgrade_receipt_closes_exact_migration_evidence(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = _closed_schema_evolution(tmp_path)
    proposal = next(
        item
        for item in plan.proposals
        if item.question_id == "evolution.code_owner_schema_requires_migration_review"
    )
    assert proposal.template_id == "evolution.code_schema_upgrade_matrix"
    assert proposal.runner_kind == "trusted_deep_declared_scenarios"
    assert proposal.scenario_ids == ("evolution.code_schema_upgrade_matrix",)
    schema_evaluation = next(
        item
        for item in projected
        if item.question_id == "evolution.code_owner_schema_requires_migration_review"
    )
    assert schema_evaluation.decision_readiness == "human_review_required"
    assert schema_evaluation.counterevidence_status == "evaluated"
    assert all(item.status == "satisfied" for item in schema_evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.evidence_complete_evaluations == 1
    assert verification.no_change_required_count == 1
    assert verification.unresolved_count == 0
    assert verification.reviews[0].question_id == (
        "evolution.code_owner_schema_requires_migration_review"
    )
    assert verification.reviews[0].reason_code == (
        "code_owner_schema_upgrade_matrix_passed_without_a_change_signal"
    )


def test_passed_schema_matrix_cannot_hide_a_noncurrent_live_schema_fact(
    tmp_path: Path,
) -> None:
    specs, _evaluations, _plan, receipts, projected = _closed_schema_evolution(tmp_path)
    schema_evaluation = next(
        item
        for item in projected
        if item.question_id == "evolution.code_owner_schema_requires_migration_review"
    )
    schema_evidence = next(
        item
        for item in schema_evaluation.evidence
        if item.source_record_kind == "exact_sqlite_schema_and_migration_ledger"
    )
    stale_evidence = replace(
        schema_evidence,
        facts=tuple(
            replace(fact, value=999) if fact.name == "schema_version" else fact
            for fact in schema_evidence.facts
        ),
    )
    stale_evaluation = replace(
        schema_evaluation,
        evidence=tuple(
            stale_evidence if item.evidence_id == schema_evidence.evidence_id else item
            for item in schema_evaluation.evidence
        ),
    )
    stale_projection = tuple(
        stale_evaluation if item.evaluation_id == schema_evaluation.evaluation_id else item
        for item in projected
    )

    verification = build_code_technical_verification(specs, stale_projection, receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_declared_architecture_contract_receipt_closes_bounded_boundary_evidence(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = _closed_architecture_contract(tmp_path)
    proposal = next(
        item
        for item in plan.proposals
        if item.question_id == "architecture.declared_import_contracts_are_evaluated"
    )
    assert proposal.template_id == "architecture.declared_import_contract_acceptance"
    assert proposal.runner_kind == "trusted_deep_declared_scenarios"
    assert proposal.scenario_ids == ("architecture.declared_import_contract_acceptance",)
    evaluation = next(
        item
        for item in projected
        if item.question_id == "architecture.declared_import_contracts_are_evaluated"
    )
    assert evaluation.decision_readiness == "human_review_required"
    assert evaluation.counterevidence_status == "evaluated"
    assert all(item.status == "satisfied" for item in evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.no_change_required_count == 1
    assert verification.unresolved_count == 0
    assert verification.reviews[0].question_id == (
        "architecture.declared_import_contracts_are_evaluated"
    )
    assert verification.reviews[0].reason_code == (
        "declared_import_contract_matrix_passed_without_a_change_signal"
    )


def test_retention_receipt_closes_exact_hold_and_negative_control_evidence(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = _closed_retention_hold_projection(tmp_path)

    assert plan.proposals[0].template_id == "retention.durable_hold_safety"
    assert plan.proposals[0].scenario_ids == ("retention.durable_hold_safety",)
    evaluation = projected[0]
    assert evaluation.decision_readiness == "human_review_required"
    assert all(item.status == "satisfied" for item in evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.no_change_required_count == 1
    assert verification.unresolved_count == 0
    assert verification.reviews[0].question_id == (
        "retention.dry_run_preserves_declared_durable_holds"
    )
    assert verification.reviews[0].reason_code == (
        "retention_durable_hold_matrix_passed_without_a_change_signal"
    )


def test_passed_retention_scenario_cannot_hide_a_missing_live_hold(tmp_path: Path) -> None:
    specs, _evaluations, _plan, receipts, projected = _closed_retention_hold_projection(tmp_path)
    evaluation = projected[0]
    hold_evidence = next(
        item
        for item in evaluation.evidence
        if item.source_record_kind == "retention_declared_hold_projection"
    )
    forged_hold_evidence = replace(
        hold_evidence,
        facts=tuple(
            replace(fact, value=1) if fact.name == "missing_holds" else fact
            for fact in hold_evidence.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged_hold_evidence if item.evidence_id == hold_evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(specs, (forged_evaluation,), receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


@pytest.mark.parametrize("failed_contracts", [1, True])
def test_passed_architecture_scenario_cannot_hide_a_live_contract_violation_or_forged_bool(
    tmp_path: Path,
    failed_contracts: int | bool,
) -> None:
    specs, _evaluations, _plan, receipts, projected = _closed_architecture_contract(tmp_path)
    evaluation = next(
        item
        for item in projected
        if item.question_id == "architecture.declared_import_contracts_are_evaluated"
    )
    contract_evidence = next(
        item
        for item in evaluation.evidence
        if item.source_record_kind == "versioned_import_contract_evaluations"
    )
    violated_evidence = replace(
        contract_evidence,
        facts=tuple(
            replace(fact, value=failed_contracts) if fact.name == "failed_contracts" else fact
            for fact in contract_evidence.facts
        ),
    )
    violated_evaluation = replace(
        evaluation,
        evidence=tuple(
            violated_evidence if item.evidence_id == contract_evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )
    violated_projection = tuple(
        violated_evaluation if item.evaluation_id == evaluation.evaluation_id else item
        for item in projected
    )

    verification = build_code_technical_verification(specs, violated_projection, receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_technical_verification_rejects_tampering_and_unregistered_authority(
    tmp_path: Path,
) -> None:
    specs, _base, _plan, receipts, projected = _closed_semantic_projection(tmp_path)
    verification = build_code_technical_verification(specs, projected, receipts)

    with pytest.raises(ValueError, match="identity"):
        replace(verification.reviews[0], reason_code="change_now")

    payload = json.loads(json.dumps(verification.as_payload()))
    payload["mutation_authority"] = True
    with pytest.raises(ValueError, match="advisory and non-mutating"):
        parse_code_technical_verification_payload(payload)
