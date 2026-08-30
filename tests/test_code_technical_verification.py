from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import neocortex.code.code_technical_verification as technical_verification_module
from neocortex.code.code_architecture_questions import architecture_questions
from neocortex.code.code_experiment_planner import plan_code_experiments
from neocortex.code.code_experiment_store import (
    apply_code_experiment_receipts,
    read_code_experiment_receipts,
    record_code_experiment_receipt,
)
from neocortex.code.code_change_evolution_analysis import (
    analyze_code_change_evolution,
    expected_code_change_evolution_questions,
)
from neocortex.code.code_interface_surface_analysis import (
    CLI_SURFACE_QUESTION,
    interface_surface_questions,
)
from neocortex.code.code_knowledge_asset_health_analysis import (
    KNOWLEDGE_ASSET_HEALTH_QUESTION,
    knowledge_asset_health_questions,
)
from neocortex.code.code_knowledge_pdf_asset_health_analysis import (
    KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
    knowledge_pdf_asset_health_questions,
)
from neocortex.code.code_state_projection_analysis import (
    analyze_text_semantic_projection,
    state_projection_questions,
)
from neocortex.code.code_retention_analysis import (
    analyze_code_retention,
    retention_questions,
)
from neocortex.code.code_review_task_analysis import (
    FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID,
    framework_review_task_questions,
)
from neocortex.code.code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
    security_dependency_questions,
)
from neocortex.code.code_supply_chain_analysis import read_code_supply_chain_analysis
from neocortex.code.code_technical_verification import (
    build_code_technical_verification,
    parse_code_technical_verification_payload,
)
from tests.test_code_change_evolution_analysis import _build_transition
from tests.test_code_architecture_questions import _ready_architecture
from tests.test_code_experiment_store import _database, _receipt
from tests.test_code_interface_surface_analysis import _analysis as _interface_analysis
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


def _closed_framework_review_task_protocol(tmp_path: Path):
    specs, evaluations = framework_review_task_questions(
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank=1,
    )
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    experiment_root = tmp_path / "framework-review-task-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:framework-review-task-fixture",
        recorded_ns=10,
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


def _closed_public_cli_contract(tmp_path: Path):
    interface_root = tmp_path / "interface-surface"
    interface_root.mkdir()
    analysis = _interface_analysis(interface_root)
    all_specs, all_evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness="current",
        rank_offset=0,
    )
    cli_spec = next(item for item in all_specs if item == CLI_SURFACE_QUESTION)
    cli_evaluation = next(
        item for item in all_evaluations if item.question_id == CLI_SURFACE_QUESTION.question_id
    )
    specs = (cli_spec,)
    evaluations = (replace(cli_evaluation, rank=1),)
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    experiment_root = tmp_path / "public-cli-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:public-cli-fixture",
        recorded_ns=11,
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


def _closed_knowledge_asset_health_contract(tmp_path: Path):
    specs, evaluations = knowledge_asset_health_questions(
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank=1,
    )
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    experiment_root = tmp_path / "knowledge-asset-health-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:knowledge-asset-health-fixture",
        recorded_ns=12,
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


def _closed_knowledge_pdf_asset_health_contract(tmp_path: Path):
    specs, evaluations = knowledge_pdf_asset_health_questions(
        snapshot_id="snapshot:fixture",
        snapshot_freshness="current",
        rank=1,
    )
    plan = plan_code_experiments(specs, evaluations)
    proposal = plan.proposals[0]
    experiment_root = tmp_path / "knowledge-pdf-asset-health-experiment"
    experiment_root.mkdir()
    database = _database(experiment_root)
    stored = record_code_experiment_receipt(
        database,
        _receipt(proposal),
        proposal,
        analysis_run_id=1,
        processing_signature="snapshot:fixture",
        review_digest="review:knowledge-pdf-asset-health-fixture",
        recorded_ns=13,
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


def test_framework_review_task_receipt_closes_exact_contract_and_outcome_evidence(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = (
        _closed_framework_review_task_protocol(tmp_path)
    )
    proposal = plan.proposals[0]
    assert proposal.question_id == FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID
    assert proposal.template_id == "framework.review_task_protocol_acceptance"
    assert proposal.scenario_ids == ("framework.review_task_protocol_acceptance",)
    assert proposal.acceptance_gates == (
        "exact_human_claim_and_terminal_decision_retries_are_idempotent",
        "faulted_publication_and_event_transactions_preserve_previous_heads",
        "page_publication_is_atomic_resumable_and_idempotent",
        "progress_and_event_heads_reject_stale_compare_and_swap",
        "semantically_changed_retry_is_rejected_as_snapshot_changed",
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
    assert verification.reviews[0].question_id == (
        "framework.review_task_lifecycle_preserves_atomicity_and_human_authority"
    )
    assert verification.reviews[0].reason_code == (
        "framework_review_task_protocol_matrix_passed_without_a_change_signal"
    )
    assert verification.reviews[0].disposition == "no_change_required_within_verified_scope"
    assert verification.reviews[0].receipt_ids == (receipts[0].receipt.receipt_id,)
    assert not verification.mutation_authority
    assert (
        parse_code_technical_verification_payload(json.loads(json.dumps(verification.as_payload())))
        == verification
    )


def test_public_cli_receipt_closes_runtime_counterevidence_and_exact_disposition(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = _closed_public_cli_contract(tmp_path)
    proposal = plan.proposals[0]
    assert proposal.template_id == "interfaces.public_cli_contract_acceptance"
    assert proposal.scenario_ids == ("interfaces.public_cli_and_static_surface",)
    evaluation = projected[0]
    assert evaluation.decision_readiness == "human_review_required"
    requirements = {item.requirement_id: item for item in evaluation.requirements}
    runtime_evidence = next(
        item
        for item in evaluation.evidence
        if item.evidence_id
        in requirements["effective_runtime_parser_contract_observed"].evidence_ids
    )
    counterevidence = next(
        item
        for item in evaluation.evidence
        if item.evidence_id
        in requirements["dynamic_cli_construction_counterevidence_evaluated"].evidence_ids
    )
    assert runtime_evidence.evidence_kind == "runtime_observation"
    assert runtime_evidence.role == "supporting"
    assert counterevidence.evidence_kind == "runtime_observation"
    assert counterevidence.role == "counterevidence"

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.reviewed_count == 1
    assert verification.reviews[0].question_id == CLI_SURFACE_QUESTION.question_id
    assert verification.reviews[0].reason_code == (
        "public_cli_parser_help_dispatch_and_focal_observability_matrix_passed_without_a_"
        "change_signal"
    )
    assert verification.reviews[0].receipt_ids == (receipts[0].receipt.receipt_id,)


def test_public_cli_policy_rejects_incomplete_static_projection(tmp_path: Path) -> None:
    specs, _evaluations, _plan, receipts, projected = _closed_public_cli_contract(tmp_path)
    evaluation = projected[0]
    static = next(
        item
        for item in evaluation.evidence
        if item.source_record_kind == "entrypoint_surface_projection"
    )
    forged = replace(
        static,
        facts=tuple(
            replace(item, value=1) if item.name == "incomplete_cli_files" else item
            for item in static.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged if item.evidence_id == static.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(specs, (forged_evaluation,), receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_knowledge_asset_health_receipt_closes_exact_causal_disposition(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = (
        _closed_knowledge_asset_health_contract(tmp_path)
    )
    proposal = plan.proposals[0]
    assert proposal.template_id == "knowledge.asset_health_causal_acceptance"
    assert proposal.scenario_ids == ("knowledge.asset_health_causal_acceptance",)
    evaluation = projected[0]
    assert evaluation.decision_readiness == "human_review_required"
    assert evaluation.counterevidence_status == "evaluated"
    assert all(item.status == "satisfied" for item in evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.reviewed_count == 1
    assert verification.reviews[0].question_id == KNOWLEDGE_ASSET_HEALTH_QUESTION.question_id
    assert verification.reviews[0].reason_code == (
        "knowledge_asset_health_causal_matrix_passed_without_a_change_signal"
    )
    assert verification.reviews[0].disposition == "no_change_required_within_verified_scope"
    assert verification.reviews[0].receipt_ids == (receipts[0].receipt.receipt_id,)


@pytest.mark.parametrize(
    ("record_kind", "fact_name", "forged_value"),
    (
        ("knowledge_asset_health_owner_store_contract", "text_schema_version", 3),
        ("knowledge_asset_health_causal_identity_contract", "text_route_version", "v0"),
        ("knowledge_asset_health_public_read_contract", "read_only", False),
        ("knowledge_asset_health_public_read_contract", "mutation_authority", True),
    ),
)
def test_knowledge_asset_health_policy_rejects_contract_or_authority_drift(
    tmp_path: Path,
    record_kind: str,
    fact_name: str,
    forged_value: object,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_knowledge_asset_health_contract(tmp_path)
    )
    evaluation = projected[0]
    evidence = next(
        item for item in evaluation.evidence if item.source_record_kind == record_kind
    )
    forged = replace(
        evidence,
        facts=tuple(
            replace(fact, value=forged_value) if fact.name == fact_name else fact
            for fact in evidence.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged if item.evidence_id == evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(
        specs,
        (forged_evaluation,),
        receipts,
    )

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_knowledge_pdf_asset_health_receipt_closes_exact_causal_disposition(
    tmp_path: Path,
) -> None:
    specs, _evaluations, plan, receipts, projected = (
        _closed_knowledge_pdf_asset_health_contract(tmp_path)
    )
    proposal = plan.proposals[0]
    assert proposal.template_id == "knowledge.pdf_asset_health_causal_acceptance"
    assert proposal.scenario_ids == ("knowledge.pdf_asset_health_causal_acceptance",)
    evaluation = projected[0]
    assert evaluation.decision_readiness == "human_review_required"
    assert evaluation.counterevidence_status == "evaluated"
    assert all(item.status == "satisfied" for item in evaluation.requirements)

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.reviewed_count == 1
    assert verification.reviews[0].question_id == KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION.question_id
    assert verification.reviews[0].reason_code == (
        "knowledge_pdf_asset_health_causal_matrix_passed_without_a_change_signal"
    )
    assert verification.reviews[0].disposition == "no_change_required_within_verified_scope"
    assert verification.reviews[0].receipt_ids == (receipts[0].receipt.receipt_id,)
    assert verification.authority == "advisory"
    assert verification.mutation_authority is False


@pytest.mark.parametrize(
    ("record_kind", "fact_name", "forged_value"),
    (
        ("knowledge_pdf_asset_health_owner_store_contract", "pdf_schema_version", 12),
        (
            "knowledge_pdf_asset_health_page_state_and_recovery_contract",
            "pdf_structural_recovery_version",
            "pdf-structural-recovery-v1",
        ),
        ("knowledge_pdf_asset_health_public_read_contract", "read_only", False),
        ("knowledge_pdf_asset_health_public_read_contract", "advisory_only", False),
        ("knowledge_pdf_asset_health_public_read_contract", "mutation_authority", True),
    ),
)
def test_knowledge_pdf_asset_health_policy_rejects_contract_or_authority_drift(
    tmp_path: Path,
    record_kind: str,
    fact_name: str,
    forged_value: object,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_knowledge_pdf_asset_health_contract(tmp_path)
    )
    evaluation = projected[0]
    evidence = next(
        item for item in evaluation.evidence if item.source_record_kind == record_kind
    )
    forged = replace(
        evidence,
        facts=tuple(
            replace(fact, value=forged_value) if fact.name == fact_name else fact
            for fact in evidence.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged if item.evidence_id == evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(
        specs,
        (forged_evaluation,),
        receipts,
    )

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


@pytest.mark.parametrize(
    ("requirement_id", "fact_name", "forged_value"),
    (
        (
            "knowledge_pdf_asset_health_partial_protected_recovery_counterevidence_evaluated",
            "relation_count",
            8,
        ),
        (
            "isolated_knowledge_pdf_asset_health_causal_experiment_result",
            "gate_count",
            3,
        ),
        (
            "isolated_knowledge_pdf_asset_health_causal_experiment_result",
            "source_evaluation_replayed",
            1,
        ),
    ),
)
def test_knowledge_pdf_asset_health_policy_rejects_receipt_projection_tamper(
    tmp_path: Path,
    requirement_id: str,
    fact_name: str,
    forged_value: object,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_knowledge_pdf_asset_health_contract(tmp_path)
    )
    evaluation = projected[0]
    requirement = next(
        item for item in evaluation.requirements if item.requirement_id == requirement_id
    )
    evidence = next(
        item for item in evaluation.evidence if item.evidence_id == requirement.evidence_ids[0]
    )
    forged = replace(
        evidence,
        facts=tuple(
            replace(fact, value=forged_value) if fact.name == fact_name else fact
            for fact in evidence.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged if item.evidence_id == evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(
        specs,
        (forged_evaluation,),
        receipts,
    )

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_framework_review_task_policy_revalidates_declared_experiment_outcomes(
    tmp_path: Path,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_framework_review_task_protocol(tmp_path)
    )
    evaluation = projected[0]
    experiment_requirement = next(
        item
        for item in evaluation.requirements
        if item.requirement_id == "isolated_review_task_protocol_experiment_result"
    )
    experiment_evidence = next(
        item
        for item in evaluation.evidence
        if item.evidence_id == experiment_requirement.evidence_ids[0]
    )
    forged_outcome = replace(
        experiment_evidence,
        facts=tuple(
            replace(fact, value=7) if fact.name == "relation_count" else fact
            for fact in experiment_evidence.facts
        ),
    )
    forged_evaluation = replace(
        evaluation,
        evidence=tuple(
            forged_outcome if item.evidence_id == experiment_evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(specs, (forged_evaluation,), receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


@pytest.mark.parametrize(
    ("authority_fact", "forged_value"),
    (
        ("terminal_decisions_require_human", False),
        ("terminal_decisions_require_human", 1),
        ("superseded_repository_only", False),
    ),
)
def test_framework_review_task_policy_rejects_weakened_human_authority_contract(
    tmp_path: Path,
    authority_fact: str,
    forged_value: bool | int,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_framework_review_task_protocol(tmp_path)
    )
    evaluation = projected[0]
    contract_evidence = next(
        item
        for item in evaluation.evidence
        if item.source_record_kind == "framework_review_task_public_protocol_contract"
    )
    weakened_contract = replace(
        contract_evidence,
        facts=tuple(
            replace(fact, value=forged_value) if fact.name == authority_fact else fact
            for fact in contract_evidence.facts
        ),
    )
    weakened_evaluation = replace(
        evaluation,
        evidence=tuple(
            weakened_contract if item.evidence_id == contract_evidence.evidence_id else item
            for item in evaluation.evidence
        ),
    )

    verification = build_code_technical_verification(specs, (weakened_evaluation,), receipts)

    assert verification.status == "partial"
    assert verification.reviews == ()
    assert verification.gaps[0].reason == "technical_policy_negative_control_not_satisfied"


def test_technical_policy_selection_keeps_same_question_subject_policies_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs, _evaluations, _plan, receipts, projected = (
        _closed_framework_review_task_protocol(tmp_path)
    )
    current = next(
        item
        for item in technical_verification_module._TECHNICAL_POLICIES
        if item.question_id == FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID
    )
    sibling = replace(
        current,
        subject_prefix="contract:framework-review-task-secondary-protocol",
    )
    monkeypatch.setattr(
        technical_verification_module,
        "_TECHNICAL_POLICIES",
        (*technical_verification_module._TECHNICAL_POLICIES, sibling),
    )

    verification = build_code_technical_verification(specs, projected, receipts)

    assert verification.status == "ready"
    assert verification.reviews[0].subject_key == "contract:framework-review-task-protocol"
    assert verification.gaps == ()


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
