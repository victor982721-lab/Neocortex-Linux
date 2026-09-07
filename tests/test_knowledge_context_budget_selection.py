"""Packing priorities preserve retrieval order but prefer usable bounded evidence."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge.knowledge_context import build_context_bundle
from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2, emitted_response_characters
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod, EvidenceRef, KnowledgeHit, KnowledgeSnapshot, RankingSignal,
    ResourceRef, RevisionRef, RevisionState,
)
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, plan_knowledge_query
from neocortex.knowledge.knowledge_search_contracts import KnowledgeSearchResult


QUERY = "radiadores sin presión"
CONDITION = "Los radiadores se recibieron sin presión, según la inspección de recepción."


def _hit(rank, owner, *, verified, full_terms, missing_negation, body):
    resource, revision, evidence_id = f"resource:{rank}", f"revision:{rank}", f"evidence:{rank}"
    evidence = {"evidence_id": evidence_id, "resource_id": resource,
        "revision_id": revision, "snippet": body, "method": "extracted",
        "section_kind": "document", "section_id": "fulltext"}
    return {"rank": rank, "fused_score": 100 - rank,
        "resource": {"owner": owner, "source_kind": owner, "resource_id": resource,
                     "current_path": f"/fixture/{owner}/{rank}.document"},
        "revision": {"revision_id": revision, "state": "current", "processing_signature": "fixture"},
        "evidence": evidence, "evidence_hydration": {"status": "owner_verified" if verified else "unavailable",
            "inspected_scope": "published_evidence_reference"},
        "signals": [{"source": f"fts_{owner}", "source_rank": rank, "raw_score": 100 - rank,
            "evidence": dict(evidence), "query_support": {
                "support": "full_terms" if full_terms else "partial_terms",
                "basis": "fts_snippet", "missing_negation_terms": ["sin"] if missing_negation else [],
                "missing_terms": ["sin"] if missing_negation else [],
            }}]}


def _entries():
    # A long but irrelevant first source previously monopolized the MCP budget.
    hits = [
        _hit(1, "archive", verified=False, full_terms=False, missing_negation=True,
             body=("Revisión del programa de trabajo. " * 20) + "Verificación de presión en radiadores."),
        _hit(2, "text", verified=True, full_terms=False, missing_negation=True,
             body=("Configuración del sistema y registro de operaciones. " * 20)),
        _hit(3, "pdf", verified=True, full_terms=True, missing_negation=False,
             body=("Datos de recepción y embalaje. " * 20) + CONDITION),
        _hit(4, "docx", verified=True, full_terms=True, missing_negation=False,
             body=("Información general del documento. " * 20) + CONDITION),
        _hit(5, "archive", verified=False, full_terms=False, missing_negation=True,
             body="Revisión de radiadores y presión en el programa."),
    ]
    snapshot = {"snapshot_id": "fixture-snapshot", "consistency": "stable", "owners": [{
        "owner": "semantic", "publications": [{"scope": "model:" + "x" * 400,
        "publication_id": "semantic:16", "generation": 16, "model_signature": "m" * 400}]}]}
    return [{"scope": "personal", "result": {"hits": hits, "snapshot": snapshot,
        "rankings": [], "complete": True, "truncated": False}}]


def test_default_mcp_budget_selects_verified_query_support_before_uninspected_prefixes():
    entries = _entries()
    before = copy.deepcopy(entries)
    payload = build_context_response_v2(entries, query=QUERY, scope="personal",
        request_id="fixture", max_characters=12000, transport="mcp")
    assert payload["citations"][0]["evidence_id"] == "evidence:3"
    first = payload["citations"][0]
    assert first["retrieval_rank"] == 3
    assert first["candidate_position"] == 3
    assert first["hydration"]["status"] == "owner_verified"
    assert CONDITION in first["excerpt"]
    assert first["evidence_disposition"] == "evidence_candidate"
    assert first["witness_checks"]["interpretation"].endswith("not_answer_entailment_or_authority")
    assert "evidence:1" not in {citation["evidence_id"] for citation in payload["citations"]}
    assert payload["coverage"]["presentation"]["status"] == "partial"
    assert payload["budget"]["character_limit"] == 12000
    assert emitted_response_characters(payload, "mcp") <= 12000
    assert entries == before


@pytest.mark.parametrize("transport", ("json", "text", "mcp"))
def test_large_budget_preserves_candidates_stable_ties_and_original_rank_positions(transport):
    entries = _entries()
    payload = build_context_response_v2(entries, query=QUERY, scope="personal",
        request_id="fixture", max_characters=100000, transport=transport)
    assert [item["evidence_id"] for item in payload["citations"]] == [
        "evidence:3", "evidence:4", "evidence:1", "evidence:5", "evidence:2"]
    # Equal usable tiers stay stable unless a later excerpt adds original
    # query terms absent from the prefixes accepted so far.
    assert [item["retrieval_rank"] for item in payload["citations"]] == [3, 4, 1, 5, 2]
    assert [item["candidate_position"] for item in payload["citations"]] == [3, 4, 1, 5, 2]
    assert payload["budget"]["characters_used"] == emitted_response_characters(payload, transport)
    repeated = build_context_response_v2(entries, query=QUERY, scope="personal",
        request_id="fixture", max_characters=100000, transport=transport)
    assert repeated == payload


def test_missing_negation_never_demotes_a_verified_literal_counter_witness():
    entries = _entries()
    counter = "El manual describe el procedimiento; no es un registro del incidente ocurrido."
    entries[0]["result"]["hits"][0]["evidence"]["snippet"] = counter
    entries[0]["result"]["hits"][0]["signals"][0]["evidence"]["snippet"] = counter
    entries[0]["result"]["hits"][0]["evidence_hydration"]["status"] = "owner_verified"
    payload = build_context_response_v2(entries, query="Qué ocurrió durante el incidente",
        scope="personal", request_id="fixture", max_characters=100000)
    first = payload["citations"][0]
    assert first["evidence_id"] == "evidence:1"
    assert first["excerpt"] == counter
    assert first["role_counterevidence"]
    assert first["evidence_disposition"] == "contradictory"
    assert first["retrieval_support"]["missing_negation_terms"] == ["sin"]


def test_v1_keeps_the_original_retrieval_order():
    raw_hits = _entries()[0]["result"]["hits"]
    hits = []
    for raw in raw_hits:
        resource = ResourceRef(raw["resource"]["resource_id"], raw["resource"]["source_kind"],
                               raw["resource"]["owner"], current_path=raw["resource"]["current_path"])
        revision = RevisionRef(resource.resource_id, raw["revision"]["revision_id"],
                               "fixture", "fixture", generation=None, state=RevisionState.CURRENT)
        evidence = EvidenceRef(raw["evidence"]["evidence_id"], resource.resource_id,
            revision.revision_id, EvidenceMethod.EXTRACTED, snippet=raw["evidence"]["snippet"])
        hits.append(KnowledgeHit(raw["rank"], resource, revision, evidence,
            (RankingSignal("fixture", "rank", float(100 - raw["rank"]), raw["rank"]),),
            float(raw["fused_score"]), ("fixture",)))
    snapshot = KnowledgeSnapshot.create(source_version="fixture", captured_at_utc="2026-09-06T00:00:00Z",
        captured_monotonic_ns=1, owners=())
    result = KnowledgeSearchResult(plan=plan_knowledge_query(KnowledgeQuery(QUERY)), snapshot=snapshot,
        hits=tuple(hits), rankings=(), complete=True, truncated=False, omitted_candidates=0,
        rows_scanned=5, vectors_scanned=0, elapsed_milliseconds=1)
    bundle = build_context_bundle(result, character_limit=100000, max_hits=5)
    assert [hit.rank for hit in bundle.selected_hits] == [1, 2, 3, 4, 5]
