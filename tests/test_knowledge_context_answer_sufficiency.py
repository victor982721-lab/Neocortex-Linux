"""Reference and retrieval evidence are not an assessment of an answer."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge.knowledge_context_v2 import (
    _measure, build_context_response_v2, emitted_response_characters,
    render_context_response, validate_context_response,
)


def _build(query, body, *, transport="json", operation="context"):
    reference = {"evidence_id": "evidence:fixture", "resource_id": "resource:fixture",
        "revision_id": "revision:fixture", "method": "extracted", "snippet": body}
    entries = [{"scope": "personal", "result": {"complete": True, "rankings": [],
        "snapshot": {"snapshot_id": "snapshot:fixture", "consistency": "stable", "owners": []},
        "hits": [{"rank": 1,
            "resource": {"resource_id": "resource:fixture", "source_kind": "text", "owner": "text",
                         "current_path": "/synthetic/fixture.txt"},
            "revision": {"revision_id": "revision:fixture", "state": "current",
                         "processing_signature": "fixture"},
            "evidence": reference,
            "evidence_hydration": {"status": "owner_verified", "inspected_scope": "published_evidence_reference"},
            "signals": [{"source": "semantic_text", "raw_score": 999999,
                "evidence": dict(reference), "query_support": {"answer_sufficiency": "supported"}}],
        }]}}]
    before = copy.deepcopy(entries)
    payload = build_context_response_v2(entries, query=query, scope="personal", request_id="fixture",
        max_characters=12000, transport=transport, operation=operation)
    assert entries == before
    assert emitted_response_characters(payload, transport) <= 12000
    assert payload["budget"]["characters_used"] == emitted_response_characters(payload, transport)
    return payload


@pytest.mark.parametrize(("query", "body", "disposition", "checks"), [
    ("¿Quién autorizó el retiro del aislador?", "La supervisora Lucía autorizó el retiro del aislador.",
     "evidence_candidate", "necessary_checks_not_failed"),
    ("¿De qué color es la cubierta?", "La cubierta es verde.", "evidence_candidate", "not_assessed"),
    ("¿Qué torque causó el daño en el perno?", "El golpe ocurrió al descargar el equipo, no durante el apriete.",
     "contradictory", "missing"),
])
@pytest.mark.parametrize("transport", ("json", "text", "mcp"))
def test_reference_candidate_and_necessary_checks_never_claim_answer_sufficiency(query, body, disposition, checks, transport):
    payload = _build(query, body, transport=transport)
    citation = payload["citations"][0]
    assert citation["hydration"]["status"] == "owner_verified"
    assert citation["evidence_disposition"] == disposition
    assert citation["witness_checks"]["status"] == checks
    assert citation["answer_sufficiency"] == "not_assessed"
    assert citation["excerpt"] == body
    if disposition == "contradictory":
        assert citation["witness_checks"]["counterevidence"] or citation["role_counterevidence"]
        for witness in citation["role_counterevidence"]:
            assert witness["text"] == body[witness["start_char"]:witness["end_char"]]
    rendered = render_context_response(payload)
    assert "Referencia verificada: identidad y texto" in rendered
    assert "candidata recuperada: resultado de búsqueda" in rendered
    assert "suficiencia de respuesta: no evaluada" in rendered


def test_direct_evidence_response_uses_the_same_explicit_unassessed_default():
    payload = _build("cubierta", "La cubierta es verde.", operation="evidence")
    assert payload["schema"] == "neocortex.evidence-response/v2"
    assert payload["citations"][0]["answer_sufficiency"] == "not_assessed"


def test_older_v2_without_the_additive_field_remains_unassessed_not_implicitly_supported():
    payload = _build("cubierta", "La cubierta es verde.")
    del payload["citations"][0]["answer_sufficiency"]
    _measure(payload)
    assert validate_context_response(payload) == payload
    assert "suficiencia de respuesta: no evaluada" in render_context_response(payload)


@pytest.mark.parametrize("value", ("supported", "sufficient", True, None))
def test_a_response_cannot_smuggle_a_sufficiency_claim_into_the_context_contract(value):
    payload = _build("cubierta", "La cubierta es verde.")
    payload["citations"][0]["answer_sufficiency"] = value
    with pytest.raises(ValueError, match="does not assess answer sufficiency"):
        validate_context_response(payload)


def test_common_unassessed_reason_is_preserved_without_reclassifying_or_hiding_text():
    body = "No se reemplazaron los rodamientos de Q7, que permanecieron instalados."
    payload = _build("¿Qué factura demuestra que no se reemplazaron los rodamientos de Q7?", body)
    citation = payload["citations"][0]
    assert citation["witness_checks"]["not_assessed_reason"] == "query_polarity_scope_not_supported"
    assert citation["answer_sufficiency"] == "not_assessed"
    assert citation["evidence_disposition"] == "evidence_candidate"
    assert citation["witness_checks"]["status"] == "not_assessed"
    assert citation["excerpt"] == body
