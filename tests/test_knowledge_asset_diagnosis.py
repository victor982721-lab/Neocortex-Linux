"""Diagnosis separates file facts from processing, indexing, and authority."""

from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.knowledge.knowledge_asset_diagnosis import build_knowledge_asset_diagnosis
from neocortex.knowledge.knowledge_asset_diagnosis_contracts import (
    AssetDiagnosticEvidenceRef,
    AssetDiagnosticObservation,
    AssetDiagnosticObservationKind as Kind,
    AssetDiagnosticRecommendation,
    AssetProblemScope as Scope,
)
from neocortex.knowledge.knowledge_asset_health_contracts import (
    KnowledgeAssetFactSnapshot,
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthFact,
    KnowledgeAssetHealthReport,
    KnowledgeAssetHealthStage as Stage,
    KnowledgeAssetHealthState,
    KnowledgeAssetHealthValue,
)


RESOURCE = "resource:file:11:3:-1"


def _report(*, status: str = "complete", path: str = "/fixture/report.txt",
            observations: tuple[AssetDiagnosticObservation, ...] = ()) -> KnowledgeAssetHealthReport:
    facts = tuple(
        KnowledgeAssetHealthFact(
            stage=stage, owner=owner, schema_version=1, record_id=f"{owner}:1",
            status=state, projection_digest="sha256:" + str(index) * 64,
            values=(KnowledgeAssetHealthValue("path", path),),
        )
        for index, (stage, owner, state) in enumerate((
            (Stage.INVENTORY, "inventory", "published"),
            (Stage.SOURCE_OWNER, "pdf" if status == "protected" else "text", status),
            (Stage.CATALOG, "catalog", "classified"),
            (Stage.KNOWLEDGE_SEARCH, "knowledge", "eligible"),
        ), 1)
    )
    snapshot = KnowledgeAssetFactSnapshot.create(
        resource_id=RESOURCE, knowledge_snapshot_id="snapshot:1", facts=facts,
        diagnostic_observations=observations,
    )
    return KnowledgeAssetHealthReport(
        resource_id=RESOURCE, health=KnowledgeAssetHealthState.UNKNOWN,
        completeness=KnowledgeAssetHealthCompleteness.COMPLETE, reason_code="fixture",
        knowledge_snapshot_id=snapshot.knowledge_snapshot_id,
        fact_snapshot_id=snapshot.fact_snapshot_id, snapshot_consistency="stable", attempts=1,
        facts=facts, diagnostic_observations=observations,
    )


def _observation(kind: Kind, code: str) -> AssetDiagnosticObservation:
    return AssetDiagnosticObservation(
        kind, RESOURCE, code,
        (AssetDiagnosticEvidenceRef("inventory" if kind is Kind.DUPLICATE_CONTENT else "text",
                                    "record:1", RESOURCE, "snapshot:1", "sha256:" + "a" * 64),),
        (RESOURCE, "resource:file:11:4:-1") if kind is Kind.DUPLICATE_CONTENT else (),
    )


def _codes(report: KnowledgeAssetHealthReport) -> dict[str, dict[str, object]]:
    payload = build_knowledge_asset_diagnosis(report).to_dict()
    return {item["code"]: item for item in payload["findings"]}  # type: ignore[index,union-attr]


def test_pipeline_alignment_and_eligible_do_not_claim_integrity_or_index_presence() -> None:
    report = _report()
    payload = report.to_dict()["diagnosis"]
    codes = _codes(report)
    assert codes["physical_integrity_not_assessed"]["certainty"] == "unknown"
    assert codes["search_eligible_not_index_verified"]["missing_checks"] == ["published_index_posting"]
    assert {item["scope"] for item in payload["findings"]} == {item.value for item in Scope}
    assert "not_file_integrity" in payload["health_semantics"]
    assert payload == report.to_dict()["diagnosis"]


def test_protected_pdf_is_not_inspected_not_corrupt() -> None:
    report = _report(status="protected", path="/fixture/protected.pdf")
    codes = _codes(report)
    assert codes["protected_not_inspected"]["certainty"] == "unknown"
    assert codes["processing_blocked_by_protection"]["scope"] == "processing"
    assert not any("corrupt" in code for code in codes)
    recommendation = report.to_dict()["diagnosis"]["recommendations"][0]
    assert recommendation["action"] == "inspect_with_authorized_access"
    assert recommendation["evidence_refs"] and recommendation["missing_checks"]
    assert recommendation["preconditions"] and recommendation["executable"] is False


def test_failed_processing_does_not_infer_a_bad_file() -> None:
    codes = _codes(_report(status="error"))
    assert codes["published_processing_failed"]["scope"] == "processing"
    assert codes["physical_integrity_not_assessed"]["certainty"] == "unknown"


def test_duplicate_proof_does_not_choose_a_contextual_keeper_or_authorize_disposal() -> None:
    observation = _observation(Kind.DUPLICATE_CONTENT, "byte_for_byte_equal")
    report = _report(observations=(observation,))
    diagnosis = report.to_dict()["diagnosis"]
    codes = _codes(report)
    assert codes["duplicate_content_proved"]["certainty"] == "observed"
    assert codes["preferred_keeper_location_unresolved"]["certainty"] == "unknown"
    assert diagnosis["observations"][0]["member_resource_ids"] == list(observation.member_resource_ids)
    assert diagnosis["recommendations"][0]["executable"] is False
    assert diagnosis["mutation_authorized"] is False


def test_ott_inside_zip_supports_identification_not_deletion() -> None:
    observation = _observation(Kind.LOGICAL_FORMAT, "logical_format_ott")
    report = _report(path="/fixture/template.zip", observations=(observation,))
    codes = _codes(report)
    assert codes["logical_format_ott"]["certainty"] == "inferred"
    assert codes["format_identification_not_disposal_evidence"]["scope"] == "policy"
    recommendations = report.to_dict()["diagnosis"]["recommendations"]
    assert [item["action"] for item in recommendations] == ["review_format_identification"]
    assert recommendations[0]["executable"] is False


def test_explicit_keeper_preference_is_preserved_without_claiming_disposal_authority() -> None:
    observation = replace(_observation(Kind.DUPLICATE_CONTENT, "byte_for_byte_equal"),
                          selection_basis="explicit_user_decision")
    report = _report(observations=(observation,))
    codes = _codes(report)
    assert "preferred_keeper_location_unresolved" not in codes
    assert codes["keeper_preference_recorded"]["certainty"] == "observed"
    recommendation = report.to_dict()["diagnosis"]["recommendations"][0]
    assert "contextual_keeper_selection" not in recommendation["missing_checks"]
    assert recommendation["mutation_authorized"] is False and recommendation["executable"] is False


def test_tmp_suffix_is_not_evidence_of_dispensability() -> None:
    report = _report(path="/fixture/irreplaceable.tmp")
    codes = _codes(report)
    assert codes["temporary_suffix_observed"]["certainty"] == "observed"
    assert codes["dispensability_not_demonstrated"]["certainty"] == "unknown"
    assert all("delete" not in item["action"] for item in report.to_dict()["diagnosis"]["recommendations"])


def test_document_condition_is_about_document_claim_not_file_damage() -> None:
    observation = _observation(Kind.DOCUMENT_CONDITION, "pressure_loss_reported")
    codes = _codes(_report(observations=(observation,)))
    assert codes["condition_described_in_document"]["scope"] == "document_condition"
    assert codes["physical_integrity_not_assessed"]["certainty"] == "unknown"


def test_changed_snapshot_suppresses_assertions_and_recommendations() -> None:
    report = replace(_report(status="protected"), snapshot_consistency="snapshot_changed")
    diagnosis = report.to_dict()["diagnosis"]
    assert all(item["certainty"] == "unknown" for item in diagnosis["findings"])
    assert diagnosis["recommendations"] == []


def test_observations_are_snapshot_bound_and_cannot_make_actions_executable() -> None:
    observation = _observation(Kind.LOGICAL_FORMAT, "logical_format_ott")
    report = _report(observations=(observation,))
    with pytest.raises(ValueError, match="fact_snapshot_id"):
        replace(report, diagnostic_observations=())
    with pytest.raises(ValueError, match=r"another resource|bound to"):
        build_knowledge_asset_diagnosis(report, observations=(replace(observation, resource_id="other"),))
    with pytest.raises(ValueError, match="executable"):
        AssetDiagnosticRecommendation(Scope.FILE, "rename", observation.evidence_refs, (), (), True)
