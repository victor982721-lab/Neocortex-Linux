"""Final-excerpt consumption of common subject/action/completion evidence."""
from __future__ import annotations

import copy

import pytest

from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2, emitted_response_characters
from neocortex.semantic.semantic_query_evidence import requested_evidence_checks

INVOICE_QUERY = "¿Qué factura demuestra el reemplazo de los rodamientos de Q7 para corregir la vibración?"
RESTORE_QUERY = "¿En qué fecha se restauraron y compararon los hashes de Zeta para certificar su respaldo?"
INVOICE = ("Factura FA-27\nServicio terminado el 12 de septiembre de 2026: "
           "se reemplazaron los rodamientos de Q7 para corregir la vibración.")
RESTORE = ("El 14 de septiembre de 2026 se restauraron los archivos de Zeta y se compararon "
           "sus hashes SHA-256 con los originales. La prueba terminó con coincidencia de los archivos.")


def _build(query, body, *, transport="json", budget=40000, forged=None):
    evidence = {"evidence_id": "evidence:synthetic", "resource_id": "resource:synthetic",
        "revision_id": "revision:synthetic", "method": "extracted", "snippet": body,
        "section_kind": "document", "section_id": "fulltext", "start_char": 0, "end_char": len(body)}
    hit = {"rank": 1,
        "resource": {"resource_id": evidence["resource_id"], "source_kind": "text", "owner": "text",
                     "current_path": "/synthetic/evidence.txt"},
        "revision": {"revision_id": evidence["revision_id"], "state": "current", "processing_signature": "fixture"},
        "evidence": evidence,
        "evidence_hydration": {"status": "owner_verified", "inspected_scope": "published_evidence_reference"},
        "evidence_extent": {"units": "characters", "bounded": False, "source_total_chars": len(body),
                           "returned_range": {"start_char": 0, "end_char": len(body), "basis": "source_section"}},
        "signals": [{"source": "fts_text", "evidence": dict(evidence), "raw_score": 999999,
                     "query_support": forged or {"support": "full_terms", "missing_negation_terms": []}}],
    }
    entries = [{"scope": "personal", "result": {"complete": True, "rankings": [], "hits": [hit],
        "snapshot": {"snapshot_id": "snapshot:fixture", "consistency": "stable", "owners": []}}}]
    before = copy.deepcopy(entries)
    payload = build_context_response_v2(entries, query=query, scope="personal",
        request_id="fixture", max_characters=budget, transport=transport)
    assert entries == before
    assert emitted_response_characters(payload, transport) <= budget
    return payload


def _assert_final(payload):
    assert payload["citations"], payload
    citation = payload["citations"][0]
    excerpt = citation["excerpt"]
    actual = citation["witness_checks"]
    expected = requested_evidence_checks(payload["query"], excerpt)
    assert actual["policy_signature"] == "query-necessary-evidence-checks-v2"
    for key in ("status", "required_witnesses", "missing_necessary_witnesses", "counterevidence",
                "applicability", "scoped_observations", "retrieval_disposition"):
        assert actual[key] == expected[key]
    assert actual["evaluated_chars"] == len(excerpt)
    assert actual["recomputed_for"] == "emitted_excerpt"
    assert actual["inspected_scope"] == "emitted_excerpt_only"
    for observation in actual["scoped_observations"]:
        start, end = observation["start_char"], observation["end_char"]
        if start is None or end is None:
            assert start is None and end is None
        else:
            assert 0 <= start < end <= len(excerpt)
            assert excerpt[start:end]
    assert citation["emitted_extent"]["start_char"] == 0
    assert citation["emitted_extent"]["end_char"] == len(excerpt)
    assert citation["evidence_id"] == "evidence:synthetic"
    return citation


@pytest.mark.parametrize(("query", "body"), [(INVOICE_QUERY, INVOICE), (RESTORE_QUERY, RESTORE)])
@pytest.mark.parametrize("transport", ("text", "json", "mcp"))
def test_complete_positive_units_keep_scoped_witnesses_without_entailment_claim(query, body, transport):
    payload = _build(query, body, transport=transport, budget=12000)
    citation = _assert_final(payload)
    assert citation["excerpt"] == body
    assert citation["fragment_state"] == "full"
    assert citation["witness_checks"]["status"] == "necessary_checks_not_failed"
    assert citation["witness_checks"]["missing_necessary_witnesses"] == []
    assert citation["evidence_disposition"] == "evidence_candidate"
    assert "not_answer_entailment_or_authority" in citation["witness_checks"]["interpretation"]
    assert "authorization_grant" not in payload and "file_actions" not in payload


def test_wrong_subject_is_related_not_a_claim_that_the_requested_event_did_not_happen():
    body = "Factura FA-27\nSe reemplazaron los rodamientos de Q8 para corregir su vibración. Este equipo no es Q7."
    citation = _assert_final(_build(INVOICE_QUERY, body))
    checks = citation["witness_checks"]
    assert checks["applicability"]["subject_scope"] == "different"
    assert "requested_subject" in checks["missing_necessary_witnesses"]
    assert checks["retrieval_disposition"] == "related_evidence_only"
    assert citation["evidence_disposition"] == "related_only"
    assert citation["excerpt"] == body


@pytest.mark.parametrize(("query", "body", "action"), [
    (INVOICE_QUERY, "El equipo Q7 se reparó mediante limpieza y balanceo, sin cambiar los rodamientos.", "replacement"),
    (RESTORE_QUERY, "Los archivos de Zeta están sincronizados. Todavía no se ha probado restaurar sus archivos ni se han comparado hashes.", "restore"),
])
def test_aligned_negated_actions_are_counterevidence_not_high_score_sufficiency(query, body, action):
    citation = _assert_final(_build(query, body, forged={
        "support": "full_terms", "requested_witness_checks": {"status": "necessary_checks_not_failed"},
    }))
    checks = citation["witness_checks"]
    assert checks["applicability"]["subject_scope"] == "aligned"
    assert checks["retrieval_disposition"] == "contradictory_evidence"
    assert citation["evidence_disposition"] == "contradictory"
    assert any(observation["state"] == "negated" and action in observation["requirement"]
               for observation in checks["scoped_observations"])
    assert citation["excerpt"] == body


def test_a_purchase_invoice_without_completed_replacement_is_only_related():
    body = "Factura FA-27\nVenta de rodamientos para Q7. El material fue entregado al almacén."
    citation = _assert_final(_build(INVOICE_QUERY, body))
    assert "completed_action:replacement" in citation["witness_checks"]["missing_necessary_witnesses"]
    assert citation["evidence_disposition"] == "related_only"


def test_another_events_date_cannot_be_reused_as_the_restore_or_hash_verification_date():
    body = ("El 14 de septiembre de 2026 se actualizó el inventario de Zeta. "
            "Se restauraron los archivos de Zeta y se compararon sus hashes, pero no se registró la fecha de las pruebas.")
    citation = _assert_final(_build(RESTORE_QUERY, body))
    missing = citation["witness_checks"]["missing_necessary_witnesses"]
    assert "event_linked_date:restore" in missing or "event_linked_date:hash_comparison" in missing
    assert citation["evidence_disposition"] == "related_only"


@pytest.mark.parametrize(("body", "disposition", "status"), [
    ("Se inspeccionó el respaldo de Lira. El 12 de marzo de 2026 se restauraron los archivos "
     "de Vega y se compararon sus hashes.", "related_only", "missing"),
    ("El respaldo de Vega no fue restaurado. El 12 de marzo de 2026 se restauraron los archivos "
     "de Lira y se compararon sus hashes.", "evidence_candidate", "necessary_checks_not_failed"),
])
def test_another_subjects_event_or_negation_is_not_transferred_to_the_requested_subject(body, disposition, status):
    query = "¿En qué fecha se restauraron y compararon los hashes de Lira?"
    citation = _assert_final(_build(query, body, budget=12000))
    assert citation["excerpt"] == body
    assert citation["evidence_disposition"] == disposition
    assert citation["witness_checks"]["status"] == status
    missing = citation["witness_checks"]["missing_necessary_witnesses"]
    if disposition == "related_only":
        assert "completed_action:restore" in missing
        assert "completed_action:hash_comparison" in missing
    else:
        assert missing == []


def test_unknown_question_family_is_not_blanket_abstention():
    citation = _assert_final(_build("¿De qué color es la cubierta?", "La cubierta es verde."))
    assert citation["witness_checks"]["status"] == "not_assessed"
    assert citation["witness_checks"]["required_witnesses"] == []
    assert citation["evidence_disposition"] == "evidence_candidate"


@pytest.mark.parametrize("transport", ("text", "json", "mcp"))
def test_a_budget_cut_recomputes_scoped_observations_on_the_text_actually_emitted(transport):
    body = "Factura FA-27\n" + ("Información del embalaje de Q7. " * 400) + INVOICE
    citation = _assert_final(_build(INVOICE_QUERY, body, transport=transport, budget=12000))
    assert citation["fragment_state"] == "truncated"
    assert "completed_action:replacement" in citation["witness_checks"]["missing_necessary_witnesses"]
    assert citation["evidence_disposition"] == "related_only"
