"""Contract tests for the additive Knowledge evidence projection."""

from __future__ import annotations

from dataclasses import replace

from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeHit,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_evidence_projection import (
    EVIDENCE_PROJECTION_SCHEMA,
    KnowledgeEvidenceProjection,
    project_knowledge_hit,
)


def _hit(*, snippet: str | None, with_locator: bool, disposition: ResourceDisposition | None = None) -> KnowledgeHit:
    resource = ResourceRef(
        resource_id="resource:fixture",
        source_kind="pdf",
        owner="archive",
        physical_identity=PhysicalIdentityRef("archive-member", "archive:fixture", 1),
        disposition=disposition,
        canonical_resource_id="resource:canonical" if disposition is ResourceDisposition.DUPLICATE else None,
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id="revision:fixture:7",
        producer="fixture-owner",
        processing_signature="fixture-v1",
        generation=7,
        state=RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        evidence_id="evidence:fixture:1",
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.EXTRACTED,
        page=3 if with_locator else None,
        section_kind="page" if with_locator else None,
        section_id="3" if with_locator else None,
        snippet=snippet,
        extractor="fixture",
        extractor_version="1",
    )
    return KnowledgeHit(
        rank=1,
        resource=resource,
        revision=revision,
        evidence=evidence,
        signals=(RankingSignal("semantic", "cosine_similarity", 0.99, 1),),
        fused_score=0.99,
        reasons=("retrieved candidate",),
    )


def test_projection_has_stable_categories_and_advisory_similarity() -> None:
    projected = project_knowledge_hit(_hit(snippet="valor exacto", with_locator=True), scope="personal")

    assert projected["schema"] == EVIDENCE_PROJECTION_SCHEMA
    assert projected["version"] == 1
    assert projected["identity"] == {
        "resource_id": "resource:fixture",
        "revision_id": "revision:fixture:7",
        "evidence_id": "evidence:fixture:1",
        "owner": "archive",
        "source_kind": "pdf",
        "physical_identity": {
            "scheme": "archive-member",
            "value": "archive:fixture",
            "identity_version": 1,
        },
    }
    assert projected["equality"]["key"] == [
        "resource:fixture",
        "revision:fixture:7",
        "evidence:fixture:1",
    ]
    assert projected["equality"]["status"] == "not_assessed"
    assert projected["similarity"]["authority"] == "advisory_only"
    assert projected["similarity"]["interpretation"].endswith("permission")
    assert projected["coverage"]["answer_sufficiency"] == "not_assessed"
    assert projected["locator"] == {"page": 3, "section_kind": "page", "section_id": "3"}
    assert projected["evidence"]["replayable"] is True


def test_missing_locator_preserves_reference_only_even_with_text() -> None:
    projected = project_knowledge_hit(_hit(snippet="texto sin rango", with_locator=False))

    assert projected["value"]["kind"] == "reference_only"
    assert projected["evidence"]["modality"] == "reference_only"
    assert projected["evidence"]["snippet"] == "texto sin rango"
    assert projected["evidence"]["replayable"] is False
    assert projected["locator"] is None
    assert projected["coverage"] == {
        "status": "partial",
        "reasons": ["locator_unavailable"],
        "has_value": True,
        "has_locator": False,
        "answer_sufficiency": "not_assessed",
    }


def test_score_changes_do_not_change_identity_or_equality() -> None:
    original = _hit(snippet="same", with_locator=True)
    changed = replace(
        original,
        signals=(RankingSignal("semantic", "cosine_similarity", 0.01, 99),),
        fused_score=0.01,
    )

    left = project_knowledge_hit(original)
    right = project_knowledge_hit(changed)
    assert left["identity"] == right["identity"]
    assert left["equality"] == right["equality"]
    assert left["disposition"] == right["disposition"]
    assert left["similarity"] != right["similarity"]


def test_duplicate_disposition_is_preserved_without_bytewise_claim() -> None:
    projected = project_knowledge_hit(
        _hit(snippet="duplicate", with_locator=True, disposition=ResourceDisposition.DUPLICATE)
    )

    assert projected["disposition"]["resource"] == "duplicate"
    assert projected["disposition"]["canonical_resource_id"] == "resource:canonical"
    assert projected["equality"]["bytewise"] == "not_assessed"


def test_contract_wrapper_is_deterministic_and_does_not_mutate_payload() -> None:
    wrapped = KnowledgeEvidenceProjection.from_hit(_hit(snippet="stable", with_locator=True))
    first = wrapped.to_json()
    payload = wrapped.to_dict()
    payload["identity"]["resource_id"] = "mutated"
    assert wrapped.to_json() == first
