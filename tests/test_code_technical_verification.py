from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

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
from _04_Nucleo_Operativo.code_technical_verification import (
    build_code_technical_verification,
    parse_code_technical_verification_payload,
)
from tests.test_code_change_evolution_analysis import _build_transition
from tests.test_code_experiment_store import _database, _receipt
from tests.test_code_state_projection_analysis import _build_state


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
