from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from neocortex.code.code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    analysis_identity,
)
from neocortex.code.code_knowledge_pdf_asset_health_analysis import (
    CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA,
    CODE_KNOWLEDGE_PDF_ASSET_HEALTH_POLICY,
    KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,
    KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_ID,
    KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_VERSION,
    KNOWLEDGE_PDF_ASSET_HEALTH_SUBJECT_KEY,
    build_knowledge_pdf_asset_health_contract_analysis,
    knowledge_pdf_asset_health_questions,
)


def _facts_by_record_kind(
    evaluation: AnalysisQuestionEvaluation,
) -> dict[str, dict[str, object]]:
    return {
        item.source_record_kind: {fact.name: fact.value for fact in item.facts}
        for item in evaluation.evidence
    }


def test_pdf_asset_health_contract_projection_is_canonical_and_fails_closed() -> None:
    analysis = build_knowledge_pdf_asset_health_contract_analysis()
    repeated = build_knowledge_pdf_asset_health_contract_analysis()
    identity_payload = {
        key: value for key, value in asdict(analysis).items() if key != "analysis_id"
    }

    assert repeated == analysis
    assert analysis.as_payload() == {
        "schema": CODE_KNOWLEDGE_PDF_ASSET_HEALTH_ANALYSIS_SCHEMA,
        **asdict(analysis),
    }
    assert analysis.analysis_id == analysis_identity(
        "code-knowledge-pdf-asset-health-analysis-v1",
        identity_payload,
    )
    assert analysis.policy_id == CODE_KNOWLEDGE_PDF_ASSET_HEALTH_POLICY
    assert analysis.logical_owner_id == analysis.state_owner_id == "pdf"
    assert analysis.state_store_id == "sqlite:pdf.sqlite3"
    assert analysis.database_name == "pdf.sqlite3"
    assert (
        analysis.pdf_schema_version,
        analysis.inventory_schema_version,
        analysis.catalog_schema_version,
        analysis.knowledge_contract_version,
    ) == (13, 10, 7, 1)
    assert (
        analysis.pdf_route_version,
        analysis.pdf_failure_version,
        analysis.pdf_structural_recovery_version,
    ) == ("pdf-route-v3", "pdf-failure-v3", "pdf-structural-recovery-v2")
    assert analysis.integrated_phases == (
        "extraction",
        "text_dedup",
        "derived",
        "catalog",
    )
    assert analysis.nonterminal_document_statuses == ("processing",)
    assert analysis.terminal_document_statuses == (
        "done",
        "partial",
        "protected",
        "error",
    )
    assert analysis.catalog_accepted_document_statuses == ("done", "partial")
    assert analysis.resource_id_scheme == "resource:file:{volume_id}:{file_id}:{birthtime_ns}"
    assert analysis.public_read_symbol == "asset_health_payload"
    assert analysis.service_symbol == "inspect_knowledge_asset_health"
    assert analysis.operation == "knowledge-health"
    assert analysis.read_only is analysis.advisory_only is True
    assert analysis.mutation_authority is False

    with pytest.raises(ValueError, match="analysis fields are incompatible"):
        replace(analysis, state_owner_id="text")
    with pytest.raises(ValueError, match="analysis fields are incompatible"):
        replace(analysis, terminal_document_statuses=("done",))
    with pytest.raises(ValueError, match="analysis identity is invalid"):
        replace(analysis, analysis_id="code-knowledge-pdf-asset-health-analysis-v1:invalid")


def test_pdf_asset_health_question_requires_partial_protected_and_recovery_experiment() -> None:
    specs, evaluations = knowledge_pdf_asset_health_questions(
        snapshot_id="snapshot:knowledge-pdf-health-fixture",
        snapshot_freshness="current",
        rank=5,
    )

    assert specs == (KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION,)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.question_id == KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_ID
    assert evaluation.question_version == KNOWLEDGE_PDF_ASSET_HEALTH_QUESTION_VERSION
    assert evaluation.subject.subject_kind == "capability"
    assert evaluation.subject.subject_key == KNOWLEDGE_PDF_ASSET_HEALTH_SUBJECT_KEY
    assert evaluation.rank == 5
    assert evaluation.observation_status == "confirmed"
    assert evaluation.question_readiness == "ready"
    assert evaluation.decision_readiness == "experiment_required"
    assert evaluation.counterevidence_status == "not_evaluated"
    assert evaluation.next_action_ids == ("run_knowledge_pdf_asset_health_causal_experiment",)
    assert evaluation.authority == "advisory"
    assert evaluation.mutation_authority is False

    assert tuple(item.requirement_id for item in evaluation.requirements) == (
        "knowledge_pdf_asset_health_owner_store_contract",
        "knowledge_pdf_asset_health_page_state_and_recovery_contract",
        "knowledge_pdf_asset_health_public_read_contract",
        "knowledge_pdf_asset_health_partial_protected_recovery_counterevidence_evaluated",
        "isolated_knowledge_pdf_asset_health_causal_experiment_result",
    )
    assert tuple(item.status for item in evaluation.requirements) == (
        "satisfied",
        "satisfied",
        "satisfied",
        "not_evaluated",
        "missing",
    )
    assert all(item.role == "supporting" for item in evaluation.evidence)
    assert all(item.evidence_kind == "contract" for item in evaluation.evidence)
    assert all(item.mutation_authority is False for item in evaluation.evidence)

    facts = _facts_by_record_kind(evaluation)
    assert facts == {
        "knowledge_pdf_asset_health_owner_store_contract": {
            "state_store_registry_schema": "neocortex.state-store-registry/v1",
            "logical_owner_id": "pdf",
            "state_owner_id": "pdf",
            "state_store_id": "sqlite:pdf.sqlite3",
            "database_name": "pdf.sqlite3",
            "pdf_schema_version": 13,
            "inventory_schema_version": 10,
            "catalog_schema_version": 7,
            "knowledge_contract_version": 1,
        },
        "knowledge_pdf_asset_health_page_state_and_recovery_contract": {
            "pdf_route_version": "pdf-route-v3",
            "pdf_failure_version": "pdf-failure-v3",
            "pdf_structural_recovery_version": "pdf-structural-recovery-v2",
            "integrated_phases": "extraction,text_dedup,derived,catalog",
            "nonterminal_document_statuses": "processing",
            "terminal_document_statuses": "done,partial,protected,error",
            "catalog_accepted_document_statuses": "done,partial",
        },
        "knowledge_pdf_asset_health_public_read_contract": {
            "health_contract_version": 1,
            "health_schema": "neocortex.knowledge-asset-health/v1",
            "health_source_version": "knowledge-asset-health-v1",
            "resource_id_scheme": "resource:file:{volume_id}:{file_id}:{birthtime_ns}",
            "identity_components": "volume_id,file_id,birthtime_ns",
            "public_read": "neocortex.read_api.asset_health_payload",
            "service": (
                "neocortex.knowledge.knowledge_asset_health.inspect_knowledge_asset_health"
            ),
            "operation": "knowledge-health",
            "read_only": True,
            "advisory_only": True,
            "mutation_authority": False,
        },
    }
