"""Contract tests for the additive Knowledge evidence projection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeHit,
    KnowledgePhaseTiming,
    KnowledgeQueryTelemetry,
    KnowledgeSnapshot,
    KnowledgeTelemetryOperation,
    KnowledgeTimingPhase,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceDisposition,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import KnowledgePlan, RetrievalMode
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget
from neocortex.knowledge.knowledge_search_contracts import (
    KnowledgeSearchResult,
    RankingExecution,
)
from neocortex.knowledge.knowledge_evidence_projection import (
    EVIDENCE_PROJECTION_SCHEMA,
    KnowledgeEvidenceProjection,
    KnowledgeSearchProjection,
    project_knowledge_hit,
    project_knowledge_search,
    validate_knowledge_projection_scope,
)


def _hit(*, snippet: str | None, with_locator: bool, disposition: ResourceDisposition | None = None) -> KnowledgeHit:
    resource = ResourceRef(
        resource_id="resource:fixture",
        source_kind="pdf",
        owner="pdf",
        physical_identity=PhysicalIdentityRef(
            "posix_device_inode_birthtime", "1:2:-1", 1
        ),
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
        "owner": "pdf",
        "source_kind": "pdf",
        "physical_identity": {
            "scheme": "posix_device_inode_birthtime",
            "value": "1:2:-1",
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


def test_identifiers_are_provenance_not_replay_locators() -> None:
    original = _hit(snippet="texto identificado", with_locator=False)
    hit = replace(
        original,
        evidence=replace(original.evidence, identifiers=(("source-note", "entry-1"),)),
    )

    projected = project_knowledge_hit(hit)

    assert projected["locator"] is None
    assert projected["evidence"]["replayable"] is False
    assert projected["value"]["kind"] == "reference_only"
    assert projected["coverage"]["reasons"] == ["locator_unavailable"]
    assert projected["provenance"]["identifiers"] == [
        {"namespace": "source-note", "value": "entry-1"}
    ]


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


def _search_result() -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        plan=KnowledgePlan(
            plan_id="knowledge-plan-v1:projection-fixture",
            normalized_query="consulta de prueba",
            retrieval_mode=RetrievalMode.EVIDENCE,
            intents=("lexical",),
            exact_terms=(),
            source_kinds=(),
            formats=(),
            project=None,
            date_from=None,
            date_to=None,
            include_history=False,
            limit=10,
            max_per_resource=3,
            min_section_distance=128,
            max_vectors=500_000,
            steps=(),
        ),
        snapshot=KnowledgeSnapshot.create(
            source_version="fixture",
            captured_at_utc="2026-09-10T00:00:00Z",
            captured_monotonic_ns=1,
            owners=(),
        ),
        hits=(_hit(snippet="resultado", with_locator=True),),
        rankings=(
            RankingExecution(
                name="fixture",
                channel="lexical",
                executed=True,
                available=True,
                complete=False,
                returned=1,
                rows_scanned=4,
                elapsed_ns=9_000,
                result_window_full=True,
            ),
        ),
        complete=False,
        truncated=True,
        omitted_candidates=5,
        rows_scanned=4,
        vectors_scanned=2,
        elapsed_milliseconds=17,
        warnings=("owner_partial",),
        telemetry=KnowledgeQueryTelemetry(
            operation=KnowledgeTelemetryOperation.SEARCH,
            total_duration_ns=17_000_000,
            phases=(KnowledgePhaseTiming(KnowledgeTimingPhase.BROKER, 9_000, service_attempt=1),),
        ),
        blocking_owners=("semantic",),
        result_window_full=True,
        window_omitted_candidates=3,
    )


def test_search_projection_copies_bounded_metadata_and_budget() -> None:
    budget = KnowledgeReadBudget(max_rows=20, max_vectors=30)
    budget.checkpoint(rows=4, vectors=2)

    projected = project_knowledge_search(
        _search_result(), scope="framework", read_budget=budget
    )

    assert projected["scope"] == "framework"
    assert projected["rankings"] == [
        {
            "name": "fixture",
            "channel": "lexical",
            "executed": True,
            "available": True,
            "complete": False,
            "returned": 1,
            "rows_scanned": 4,
            "row_count_semantics": "materialized_lower_bound",
            "vectors_scanned": 0,
            "elapsed_ns": 9_000,
            "result_window_full": True,
        }
    ]
    assert projected["telemetry"]["operation"] == "search"
    assert projected["blocking_owners"] == ["semantic"]
    assert projected["elapsed_milliseconds"] == 17
    assert projected["result_window_full"] is True
    assert projected["window_omitted_candidates"] == 3
    assert projected["read_budget"] == {
        "schema": "neocortex.knowledge-read-budget/v1",
        "max_rows": 20,
        "max_vectors": 30,
        "max_temporary_bytes": None,
        "deadline_configured": False,
        "rows_used": 4,
        "vectors_used": 2,
        "temporary_bytes_used": 0,
        "checkpoints": 1,
    }
    assert projected["coverage"]["reasons"] == [
        "blocking_owners",
        "candidate_scan_truncated",
        "owner_partial",
        "search_incomplete",
    ]
    assert projected["warnings"] == ["owner_partial"]
    assert projected["snapshot"]["kind"] == "knowledge_snapshot"


@pytest.mark.parametrize("scope", ("personal", "framework", "all"))
def test_projection_scope_accepts_only_fixed_read_scope_names(scope: str) -> None:
    assert validate_knowledge_projection_scope(scope) == scope
    assert project_knowledge_hit(_hit(snippet="v", with_locator=True), scope=scope)[
        "provenance"
    ]["scope"] == scope


@pytest.mark.parametrize("scope", ("", "zip-intake", "Personal", 1, True))
def test_projection_scope_rejects_unrecognized_values(scope: object) -> None:
    with pytest.raises(ValueError, match="scope must be personal, framework or all"):
        validate_knowledge_projection_scope(scope)  # type: ignore[arg-type]


def test_search_projection_wrapper_is_frozen_and_defensively_copied() -> None:
    source = project_knowledge_search(_search_result(), scope="all")
    wrapped = KnowledgeSearchProjection(source)
    source["items"][0]["identity"]["resource_id"] = "changed"
    observed = wrapped.to_dict()

    assert observed["scope"] == "all"
    assert observed["items"][0]["identity"]["resource_id"] == "resource:fixture"
    observed["items"][0]["identity"]["resource_id"] = "changed-again"
    assert wrapped.to_dict()["items"][0]["identity"]["resource_id"] == "resource:fixture"
    with pytest.raises((AttributeError, TypeError)):
        wrapped.payload = {}  # type: ignore[misc]


def test_search_projection_rejects_untrusted_budget_mapping() -> None:
    with pytest.raises(ValueError, match="unsupported fields"):
        project_knowledge_search(
            _search_result(),
            read_budget={"cancellation_check": lambda: None},
        )
