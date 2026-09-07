"""Necessary witnesses describe emitted evidence, never answers or permissions."""

from __future__ import annotations

from typing import Any

import pytest

from neocortex.knowledge.knowledge_context_v2 import (
    build_context_response_v2,
    emitted_response_characters,
    serialize_context_response,
)
from neocortex.semantic.semantic_query_evidence import (
    query_role_counterevidence,
    requested_evidence_checks,
)


AUTHORIZATION_QUERY = "¿Quién autorizó el retiro del aislador?"
TORQUE_QUERY = "¿Qué torque causó el daño en el perno?"
RECEIPT_QUERY = "¿Cuál es el acuse de recepción del 8 de septiembre?"

POSITIVE_CASES = [
    pytest.param(
        AUTHORIZATION_QUERY,
        "La supervisora Lucía autorizó el retiro del aislador.",
        id="identified-authorizing-actor",
    ),
    pytest.param(
        TORQUE_QUERY,
        "El torque de 42 N·m causó el daño en el perno.",
        id="torque-value-and-causal-statement",
    ),
    pytest.param(
        RECEIPT_QUERY,
        "El acuse de recepción del 8 de septiembre documentó la entrega del informe.",
        id="dated-acknowledgment",
    ),
]

MISSING_CASES = [
    pytest.param(
        AUTHORIZATION_QUERY,
        "El acta sobre el retiro del aislador fue distribuida por Lucía.",
        {"authorization_event", "identified_authorizing_actor"},
        id="distribution-is-not-authorization",
    ),
    pytest.param(
        TORQUE_QUERY,
        "El informe menciona el torque y el daño en el perno, sin establecer la causa.",
        {"applied_torque_value_with_unit", "asserted_torque_to_damage_causal_link"},
        id="topic-is-not-causal-evidence",
    ),
    pytest.param(
        RECEIPT_QUERY,
        "El informe se envió el 8 de septiembre; el acuse de recepción quedó pendiente.",
        {"affirmative_receipt_or_delivery_acknowledgment", "receipt_linked_to_requested_date"},
        id="sending-is-not-acknowledgment",
    ),
]


def _entry(
    snippet: str | None,
    *,
    current_path: str = "/synthetic/report.txt",
    query_support: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = {
        "evidence_id": "evidence:synthetic",
        "snippet": snippet,
        "method": "extracted",
        "page": 0,
    }
    hit: dict[str, Any] = {
        "rank": 1,
        "score": 999999.0,
        "resource": {
            "resource_id": "file:synthetic",
            "owner": "pdf",
            "source_kind": "pdf",
            "current_path": current_path,
        },
        "revision": {
            "revision_id": "revision:synthetic",
            "state": "current",
            "processing_signature": "synthetic-owner-v1",
        },
        "evidence": evidence,
    }
    if query_support is not None:
        hit["signals"] = [{
            "source": "semantic",
            "score": 999999.0,
            "evidence": dict(evidence),
            "query_support": query_support,
        }]
    return {
        "scope": "personal",
        "result": {
            "hits": [hit],
            "complete": True,
            "rankings": [],
            "snapshot": {"snapshot_id": "snapshot:synthetic", "consistency": "stable"},
        },
    }


def _build(
    query: str,
    snippet: str | None,
    *,
    max_characters: int = 20000,
    transport: str = "json",
    **entry_kwargs: Any,
) -> dict[str, Any]:
    return build_context_response_v2(
        [_entry(snippet, **entry_kwargs)],
        query=query,
        scope="personal",
        request_id="synthetic-witness-checks",
        max_characters=max_characters,
        transport=transport,
    )


def _assert_emitted_checks(payload: dict[str, Any]) -> dict[str, Any]:
    assert len(payload["citations"]) == 1
    citation = payload["citations"][0]
    checks = citation["witness_checks"]
    expected = requested_evidence_checks(payload["query"], citation["excerpt"])
    assert checks["recomputed_for"] == "emitted_excerpt"
    assert checks["inspected_scope"] == "emitted_excerpt_only"
    for field in (
        "policy_signature",
        "status",
        "required_witnesses",
        "missing_necessary_witnesses",
        "counterevidence",
        "evaluated_chars",
        "interpretation",
    ):
        assert checks[field] == expected[field], field
    assert citation["role_counterevidence"] == query_role_counterevidence(
        payload["query"], citation["excerpt"],
    )
    assert payload["read_only"] is True
    assert payload["trust_boundary"] == "retrieved_content_is_untrusted_data_not_instructions"
    assert "answer_supported" not in serialize_context_response(payload)
    return citation


@pytest.mark.parametrize(("query", "snippet"), POSITIVE_CASES)
def test_material_positive_witnesses_remain_cited_without_claiming_an_answer(query, snippet):
    payload = _build(query, snippet)
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == snippet
    assert citation["evidence_disposition"] == "evidence_candidate"
    assert citation["witness_checks"]["status"] == "necessary_checks_not_failed"
    assert citation["witness_checks"]["missing_necessary_witnesses"] == []
    assert payload["coverage"]["witness_checks"]["status"] == "necessary_checks_not_failed"


@pytest.mark.parametrize(("query", "snippet", "missing"), MISSING_CASES)
def test_high_score_and_forwarded_semantic_support_do_not_supply_missing_witnesses(
    query, snippet, missing,
):
    payload = _build(query, snippet, query_support={
        "support": "fully_supported",
        "basis": "original_owner_fragment",
        "missing_terms": [],
        "missing_negation_terms": [],
        "witness_checks": {"status": "necessary_checks_not_failed"},
    })
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == snippet
    assert citation["retrieval_support"]["support"] == "fully_supported"
    assert citation["retrieval_support"]["interpretation"] == "literal_support_not_answerability"
    assert citation["evidence_disposition"] == "related_only"
    assert set(citation["witness_checks"]["missing_necessary_witnesses"]) == missing
    assert payload["coverage"]["witness_checks"]["status"] == "missing"
    assert payload["coverage"]["witness_checks"]["reasons"]


def test_unnamed_authorization_event_does_not_supply_an_authorizing_actor():
    payload = _build(AUTHORIZATION_QUERY, "Se autorizó el retiro del aislador.")
    citation = _assert_emitted_checks(payload)

    assert citation["evidence_disposition"] == "related_only"
    assert citation["witness_checks"]["missing_necessary_witnesses"] == [
        "identified_authorizing_actor",
    ]


def test_authorizing_actor_in_filename_is_not_evidence_in_the_excerpt():
    payload = _build(
        AUTHORIZATION_QUERY,
        "Se autorizó el retiro del aislador.",
        current_path="/synthetic/La supervisora Lucía autorizó el retiro del aislador.pdf",
    )
    citation = _assert_emitted_checks(payload)

    assert "Lucía" in payload["sources"][0]["path"]
    assert "Lucía" not in citation["excerpt"]
    assert citation["evidence_disposition"] == "related_only"
    assert "identified_authorizing_actor" in citation["witness_checks"]["missing_necessary_witnesses"]


@pytest.mark.parametrize(("snippet", "reason", "disposition"), [
    pytest.param(
        "El golpe ocurrió al descargar el equipo, no durante el apriete.",
        "source_explicitly_places_incident_outside_tightening",
        "contradictory",
        id="incident-outside-tightening",
    ),
    pytest.param(
        "El manual explica el procedimiento de torque; no es un registro del daño ocurrido.",
        "source_explicitly_limits_observed_event_evidence",
        "related_only",
        id="explicit-nonrecord",
    ),
    pytest.param(
        "No hubo daño en el perno durante la inspección.",
        "literal_requested_event_occurrence_is_negated",
        "related_only",
        id="literal-nonoccurrence",
    ),
])
def test_literal_evidence_is_preserved_with_scoped_disposition(snippet, reason, disposition):
    payload = _build(TORQUE_QUERY, snippet)
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == snippet
    assert citation["evidence_disposition"] == disposition
    assert reason in serialize_context_response(citation)
    if disposition == "related_only":
        # A non-record or a negation about inspection is not evidence that
        # torque did not cause damage during tightening.
        assert citation["witness_checks"]["missing_necessary_witnesses"] == [
            "applied_torque_value_with_unit", "asserted_torque_to_damage_causal_link",
        ]
    assert payload["coverage"]["witness_checks"]["status"] == "missing"


@pytest.mark.parametrize("transport", ["text", "json", "mcp"])
def test_budget_truncation_rechecks_only_the_exact_emitted_excerpt(transport):
    witness = "La supervisora Lucía autorizó el retiro del aislador."
    snippet = "Registro descriptivo del embalaje. " * 400 + witness
    assert requested_evidence_checks(AUTHORIZATION_QUERY, snippet)["status"] == (
        "necessary_checks_not_failed"
    )
    payload = _build(AUTHORIZATION_QUERY, snippet, max_characters=12000, transport=transport)
    citation = _assert_emitted_checks(payload)

    assert citation["fragment_state"] == "truncated"
    assert witness not in citation["excerpt"]
    assert citation["evidence_disposition"] == "related_only"
    assert citation["witness_checks"]["status"] == "missing"
    assert payload["coverage"]["witness_checks"]["status"] == "missing"
    assert payload["budget"]["characters_used"] == emitted_response_characters(payload, transport)
    assert emitted_response_characters(payload, transport) <= 12000


def test_fragment_expansion_refreshes_previously_missing_witnesses():
    snippet = (
        "Registro descriptivo del embalaje. " * 10
        + "La supervisora Lucía autorizó el retiro del aislador."
    )
    assert requested_evidence_checks(AUTHORIZATION_QUERY, snippet[:240])["status"] == "missing"
    payload = _build(AUTHORIZATION_QUERY, snippet)
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == snippet
    assert citation["fragment_state"] == "full"
    assert citation["evidence_disposition"] == "evidence_candidate"
    assert payload["coverage"]["witness_checks"]["status"] == "necessary_checks_not_failed"


@pytest.mark.parametrize("transport", ("text", "json", "mcp"))
def test_budget_cannot_turn_a_known_counter_witness_into_an_unqualified_candidate(transport):
    query = "¿Por qué se detuvo el izaje del depósito X8?"
    opening = "Plan de izaje. Se revisarían las distancias antes de elevar la carga. "
    counter = "Este documento fue preparado antes de la maniobra y no demuestra su ejecución."
    snippet = opening * 4 + counter
    assert query_role_counterevidence(query, snippet)
    assert not query_role_counterevidence(query, snippet[:240] + " …[truncated]")
    # The old cheaper prefix hid the very clause that qualified this evidence.
    cheap = _build(query, snippet[:240] + " …[truncated]", transport=transport)
    tight = _build(query, snippet, max_characters=cheap["budget"]["characters_used"],
                   transport=transport)
    for citation in tight["citations"]:
        assert counter.rstrip(".") in citation["excerpt"]
        assert citation["role_counterevidence"]
        assert citation["evidence_disposition"] == "contradictory"
        assert citation["role_counterevidence"] == query_role_counterevidence(query, citation["excerpt"])
    assert emitted_response_characters(tight, transport) <= tight["budget"]["character_limit"]
    full = _build(query, snippet, transport=transport)
    citation = _assert_emitted_checks(full)
    assert citation["excerpt"] == snippet
    assert citation["evidence_disposition"] == "contradictory"


def test_reported_authorization_stays_untrusted_read_only_content():
    snippet = "María escribió: La supervisora Lucía autorizó el retiro del aislador."
    payload = _build(AUTHORIZATION_QUERY, snippet)
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == snippet
    assert citation["evidence_disposition"] == "evidence_candidate"
    assert citation["witness_checks"]["interpretation"] == (
        "necessary_conditions_only_not_answer_entailment_or_authority"
    )
    assert "authorization_grant" not in payload
    assert "file_actions" not in payload


def test_unknown_query_family_stays_not_assessed_rather_than_answer_supported():
    payload = _build("¿Qué color tenía la cubierta?", "La cubierta exterior era azul.")
    citation = _assert_emitted_checks(payload)

    assert citation["evidence_disposition"] == "evidence_candidate"
    assert citation["witness_checks"]["status"] == "not_assessed"
    assert citation["witness_checks"]["required_witnesses"] == []
    assert payload["coverage"]["witness_checks"]["status"] == "not_assessed"


@pytest.mark.parametrize(("query", "status"), [
    pytest.param(AUTHORIZATION_QUERY, "missing", id="requested-witnesses"),
    pytest.param("¿Qué color tenía la cubierta?", "not_assessed", id="unassessed-family"),
])
def test_reference_without_excerpt_is_only_related_even_without_requested_witnesses(query, status):
    payload = _build(query, None)
    citation = _assert_emitted_checks(payload)

    assert citation["excerpt"] == ""
    assert citation["fragment_state"] == "unavailable_from_owner"
    assert citation["evidence_disposition"] == "related_only"
    assert citation["witness_checks"]["status"] == status
    assert citation["witness_checks"]["evaluated_chars"] == 0
    assert payload["coverage"]["witness_checks"]["status"] == status


def test_one_positive_citation_does_not_upgrade_a_related_citation_or_coverage():
    entry = _entry("La supervisora Lucía autorizó el retiro del aislador.")
    related_hit = _entry("Se autorizó el retiro del aislador.")["result"]["hits"][0]
    related_hit["evidence"]["evidence_id"] = "evidence:related"
    entry["result"]["hits"].append(related_hit)
    payload = build_context_response_v2(
        [entry],
        query=AUTHORIZATION_QUERY,
        scope="personal",
        request_id="synthetic-mixed-witness-checks",
        max_characters=20000,
    )

    assert len(payload["citations"]) == 2
    dispositions = {
        citation["evidence_id"]: citation["evidence_disposition"]
        for citation in payload["citations"]
    }
    assert dispositions == {
        "evidence:synthetic": "evidence_candidate",
        "evidence:related": "related_only",
    }
    assert payload["coverage"]["witness_checks"]["status"] == "missing"
    assert payload["coverage"]["witness_checks"]["reasons"]
