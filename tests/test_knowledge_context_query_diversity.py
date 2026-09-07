"""Packing adds original-query evidence without rewriting retrieval order."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge.knowledge_context_v2 import (
    build_context_response_v2, emitted_response_characters,
)


def _entries(bodies):
    hits = []
    for rank, body in enumerate(bodies, 1):
        reference = {"evidence_id": f"evidence:{rank}", "resource_id": f"resource:{rank}",
            "revision_id": f"revision:{rank}", "snippet": body, "method": "extracted",
            "section_kind": "document", "section_id": "fulltext"}
        hits.append({"rank": rank, "fused_score": 100 - rank,
            "resource": {"resource_id": reference["resource_id"], "owner": "text",
                "source_kind": "text", "current_path": f"/synthetic/{rank}.txt"},
            "revision": {"revision_id": reference["revision_id"], "state": "current",
                         "processing_signature": "fixture"},
            "evidence": reference,
            "evidence_hydration": {"status": "owner_verified",
                "inspected_scope": "published_evidence_reference"},
            "signals": [{"source": "semantic_text", "evidence": dict(reference),
                "query_support": {"support": "full_terms", "missing_negation_terms": [],
                    "matched_terms": ["invented", "variant", "terms"],
                    "term_coverage": 1.0, "query_expansion": {"winning_variant": "different_query"}}}],
        })
    return [{"scope": "personal", "result": {"hits": hits, "complete": True, "rankings": [],
        "snapshot": {"snapshot_id": "snapshot:fixture", "consistency": "stable", "owners": []}}}]


def _build(entries, *, query="alfa beta gamma delta", transport="json", budget=12000):
    before = copy.deepcopy(entries)
    payload = build_context_response_v2(entries, query=query, scope="personal",
        request_id="fixture", max_characters=budget, transport=transport)
    assert emitted_response_characters(payload, transport) <= budget
    assert entries == before
    return payload


@pytest.mark.parametrize("transport", ("json", "text", "mcp"))
def test_original_query_novelty_keeps_first_witness_then_adds_distinct_terms(transport):
    entries = _entries(["Alfa se midió en el sensor.", "Alfa se midió en el sensor.",
                        "Se registró la válvula.", "Delta se midió en el sensor.",
                        "Beta y gamma se midieron en el sensor."])
    payload = _build(entries, transport=transport)
    assert [citation["retrieval_rank"] for citation in payload["citations"][:2]] == [1, 5]
    assert [citation["candidate_position"] for citation in payload["citations"][:2]] == [1, 5]
    assert payload["citations"][1]["excerpt"] == entries[0]["result"]["hits"][4]["evidence"]["snippet"]
    if len(payload["citations"]) < len(entries[0]["result"]["hits"]):
        assert payload["coverage"]["presentation"]["status"] == "partial"
        assert any(reason.startswith("omitted_citations:")
                   for reason in payload["coverage"]["presentation"]["reasons"])


def test_diversification_never_drops_a_distinct_resource_when_the_budget_can_fit_it():
    entries = _entries(["Alfa se midió.", "Alfa se midió.", "Sin términos de consulta.",
                        "Delta se midió.", "Beta y gamma se midieron."])
    payload = _build(entries, budget=100000)
    assert [citation["retrieval_rank"] for citation in payload["citations"]] == [1, 5, 4, 2, 3]
    assert {source["resource_id"] for source in payload["sources"]} == {f"resource:{i}" for i in range(1, 6)}
    assert not any(reason.startswith("omitted_citations:")
                   for reason in payload["coverage"]["presentation"]["reasons"])


def test_rejected_large_metadata_does_not_consume_terms_for_later_accepted_citations():
    entries = _entries(["Alfa se midió.", "Beta gamma se midieron.", "Beta se midió.", "Delta se midió."])
    entries[0]["result"]["hits"][1]["resource"]["current_path"] = "/synthetic/" + "x" * 5000
    payload = _build(entries, transport="mcp")
    ranks = [citation["retrieval_rank"] for citation in payload["citations"]]
    assert ranks[:2] == [1, 3]
    assert 2 not in ranks
    assert f"omitted_citations:{4 - len(ranks)}" in payload["coverage"]["presentation"]["reasons"]
    assert payload["coverage"]["presentation"]["status"] == "partial"


def test_words_beyond_an_accepted_prefix_do_not_count_as_emitted_coverage():
    entries = _entries(["Alfa. " + "Información del embalaje. " * 400 + " Beta.",
                        "Beta se midió.", "Gamma se midió."])
    payload = _build(entries, query="alfa beta gamma", transport="mcp")
    assert [citation["retrieval_rank"] for citation in payload["citations"][:2]] == [1, 2]
    assert payload["citations"][0]["fragment_state"] == "truncated"
    assert "Beta" not in payload["citations"][0]["excerpt"]


@pytest.mark.parametrize(("first_body", "second_rank", "fragment_state"), [
    ("Alfa. " + "Información del embalaje. " * 400, 2, "truncated"),
    ("Alfa …[truncated]", 3, "full"),
])
def test_only_the_generated_marker_is_excluded_from_accepted_query_coverage(first_body, second_rank, fragment_state):
    entries = _entries([first_body, "Truncated aparece en el registro.", "Gamma se midió."])
    payload = _build(entries, query="alfa truncated gamma", transport="mcp")
    assert [citation["retrieval_rank"] for citation in payload["citations"][:2]] == [1, second_rank]
    assert payload["citations"][0]["fragment_state"] == fragment_state
    assert payload["citations"][0]["excerpt"].endswith(" …[truncated]")
