"""Verified paraphrases and counter-evidence share the original retrieval order."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2, emitted_response_characters


QUERY = "¿Por qué se detuvo el primer izaje del depósito X8?"
FACT = ("En el primer izaje del depósito X8 se midió una separación insuficiente de la viga, "
        "por lo que se detuvo el movimiento antes del contacto. Se modificó el punto de apoyo "
        "y el segundo intento permitió colocar el depósito sin golpes.")


def _entry():
    rows = []
    bodies = [
        FACT,
        "El manual describe el procedimiento de izaje; no es un registro del incidente ocurrido.",
        "Este documento se preparó antes de la maniobra y no demuestra su ejecución.",
        "El registro describe otra actividad. El registro no corresponde al depósito X8.",
    ]
    for rank, body in enumerate(bodies, 1):
        resource, revision, evidence = f"resource:{rank}", f"revision:{rank}", f"evidence:{rank}"
        reference = {"evidence_id": evidence, "resource_id": resource, "revision_id": revision,
            "method": "extracted", "snippet": body, "section_kind": "document", "section_id": "fulltext"}
        rows.append({
            "rank": rank, "fused_score": 10 - rank,
            "resource": {"resource_id": resource, "source_kind": "text", "owner": "text",
                         "current_path": f"/fixture/{rank}.txt"},
            "revision": {"revision_id": revision, "processing_signature": "fixture", "state": "current"},
            "evidence": reference,
            "evidence_hydration": {"status": "owner_verified", "inspected_scope": "published_evidence_reference"},
            "signals": [{"source": "semantic_text", "evidence": dict(reference), "query_support": {
                "support": "partial_terms" if rank == 1 else "full_terms",
                "missing_terms": ["pararon"] if rank == 1 else [],
                "missing_negation_terms": [],
            }}],
        })
    return {"scope": "personal", "result": {"complete": True, "hits": rows, "rankings": [],
        "snapshot": {"snapshot_id": "snapshot:fixture", "consistency": "stable", "owners": [{
            "owner": "semantic", "publications": [{"scope": "model:" + "s" * 400,
                "publication_id": "semantic:16", "generation": 16, "model_signature": "m" * 400}],
        }]}}}


@pytest.mark.parametrize("transport", ("json", "text", "mcp"))
def test_budget_keeps_verified_factual_unit_before_lower_ranked_duplicate_countermaterial(transport):
    entries = [_entry()]
    before = copy.deepcopy(entries)
    payload = build_context_response_v2(entries, query=QUERY, scope="personal",
        request_id="fixture", max_characters=12000, transport=transport)
    assert payload["citations"]
    first = payload["citations"][0]
    assert first["evidence_id"] == "evidence:1"
    assert first["retrieval_rank"] == 1 and first["candidate_position"] == 1
    assert first["excerpt"] == FACT
    assert first["fragment_state"] == "full"
    assert first["evidence_disposition"] == "evidence_candidate"
    assert first["retrieval_support"]["support"] == "partial_terms"
    assert emitted_response_characters(payload, transport) <= 12000
    assert entries == before


def test_wide_budget_exposes_counter_witness_and_exact_subject_exclusion_without_rewriting():
    payload = build_context_response_v2([_entry()], query=QUERY, scope="personal",
        request_id="fixture", max_characters=100000)
    assert [citation["retrieval_rank"] for citation in payload["citations"]] == [1, 2, 3, 4]
    exclusion = payload["citations"][3]
    assert exclusion["excerpt"] == _entry()["result"]["hits"][3]["evidence"]["snippet"]
    assert exclusion["evidence_disposition"] == "contradictory"
    assert any("requested_named_subject_is_explicitly_excluded" in witness["reasons"]
               for witness in exclusion["role_counterevidence"])
    for witness in exclusion["role_counterevidence"]:
        assert witness["text"] == exclusion["excerpt"][witness["start_char"]:witness["end_char"]]
    assert "depósito X8" in exclusion["excerpt"]


def test_unverified_counter_cannot_monopolize_mcp_budget_before_verified_later_evidence():
    entry = _entry()
    entry["result"]["hits"] = entry["result"]["hits"][:2]
    earlier, later = entry["result"]["hits"]
    counter = ("Planificación previa del equipo. " * 22
               + "Este documento no es un registro del incidente ocurrido.")
    earlier["evidence"]["snippet"] = counter
    earlier["signals"][0]["evidence"]["snippet"] = counter
    earlier["evidence_hydration"]["status"] = "unavailable"
    earlier["evidence_hydration"]["reason"] = "unsupported_evidence_lookup"
    later["evidence"]["snippet"] = FACT
    later["signals"][0]["evidence"]["snippet"] = FACT
    before = copy.deepcopy(entry)
    bounded = build_context_response_v2([entry], query=QUERY, scope="personal",
        request_id="fixture", max_characters=12000, transport="mcp")
    first = bounded["citations"][0]
    assert first["evidence_id"] == "evidence:2"
    assert first["retrieval_rank"] == 2 and first["candidate_position"] == 2
    assert first["excerpt"] == FACT
    assert emitted_response_characters(bounded, "mcp") <= 12000
    counters = [citation for citation in bounded["citations"] if citation["evidence_id"] == "evidence:1"]
    if not counters:
        assert bounded["coverage"]["presentation"]["status"] == "partial"
        assert "omitted_citations:1" in bounded["coverage"]["presentation"]["reasons"]
    wide = build_context_response_v2([entry], query=QUERY, scope="personal",
        request_id="fixture", max_characters=100000, transport="mcp")
    retained = next(citation for citation in wide["citations"] if citation["evidence_id"] == "evidence:1")
    assert retained["excerpt"] == counter and retained["retrieval_rank"] == 1
    assert retained["evidence_disposition"] == "contradictory"
    assert retained["role_counterevidence"]
    for witness in retained["role_counterevidence"]:
        assert witness["text"] == counter[witness["start_char"]:witness["end_char"]]
    assert entry == before
