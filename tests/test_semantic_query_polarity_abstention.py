"""An uninterpreted query polarity must not invert its evidence's meaning."""

import pytest

from neocortex.semantic.semantic_query_evidence import (
    MAX_EVIDENCE_CHECK_CHARS,
    query_role_counterevidence,
    requested_evidence_checks,
)


@pytest.mark.parametrize("query_negative", [False, True])
@pytest.mark.parametrize("body_negative", [False, True])
def test_query_and_witness_polarity_matrix_keeps_negated_requests_unassessed(query_negative, body_negative):
    query = "¿Qué factura demuestra que " + ("no " if query_negative else "") + "se reemplazaron los rodamientos de M9?"
    body = "Factura FC-62\n" + ("No " if body_negative else "") + "se reemplazaron los rodamientos de M9."
    result = requested_evidence_checks(query, body)
    assert result["evaluated_chars"] == len(body)
    assert result["policy_signature"] == "query-necessary-evidence-checks-v2"
    if query_negative:
        assert result["status"] == "not_assessed"
        assert result["not_assessed_reason"] == "query_polarity_scope_not_supported"
        assert result["retrieval_disposition"] == "unchanged"
        assert result["required_witnesses"] == result["missing_necessary_witnesses"] == []
        assert result["counterevidence"] == result["scoped_observations"] == []
        assert result["applicability"]["families"] == []
    elif body_negative:
        assert result["status"] == "missing"
        assert result["retrieval_disposition"] == "contradictory_evidence"
        assert "requested_action_negated:replacement" in result["counterevidence"]
        assert "not_assessed_reason" not in result
    else:
        assert result["status"] == "necessary_checks_not_failed"
        assert result["retrieval_disposition"] == "unchanged"
        assert "completed_action:replacement" in result["required_witnesses"]
        assert "not_assessed_reason" not in result


@pytest.mark.parametrize("query", [
    "¿Qué factura demuestra que nunca se cambiaron los rodamientos de M9?",
    "¿Qué se documentó sin cambiar los rodamientos de M9?",
    "Which invoice shows the bearings of M9 were not replaced?",
    "Which invoice shows the bearings of M9 weren't replaced?",
    "Which invoice shows the bearings of M9 weren\u2019t replaced?",
    "Which invoice shows replacement cannot be confirmed for M9?",
])
def test_uninterpreted_negation_scope_is_explicit_without_inventing_action_requirements(query):
    result = requested_evidence_checks(query, "No se reemplazaron los rodamientos de M9.")
    assert result["status"] == "not_assessed"
    assert result["not_assessed_reason"] == "query_polarity_scope_not_supported"
    assert result["retrieval_disposition"] == "unchanged"
    assert result["required_witnesses"] == result["counterevidence"] == []


def test_raw_role_counterevidence_does_not_invert_a_negated_request():
    body = "No hubo daño en M9 durante la inspección."
    assert query_role_counterevidence("¿Qué ocurrió cuando no hubo daño en M9?", body) == []
    positive_request = query_role_counterevidence("¿Qué ocurrió con el daño en M9?", body)
    assert positive_request
    assert any("literal_requested_event_occurrence_is_negated" in item["reasons"] for item in positive_request)


def test_polarity_abstention_keeps_existing_input_and_evaluation_bounds():
    body = "No se reemplazaron los rodamientos de M9. " * 1200
    result = requested_evidence_checks("¿Qué factura demuestra que no se reemplazaron los rodamientos de M9?", body)
    assert result["evaluated_chars"] == MAX_EVIDENCE_CHECK_CHARS
    assert result["evaluation_truncated"] is True
    assert result["query_truncated"] is False
    assert result["basis"] == "input_text"
    assert result["status"] == "not_assessed"


@pytest.mark.parametrize(
    "query",
    (
        "¿Qué factura demuestra que ningún rodamiento se reemplazó en M9?",
        "Which invoice shows none of the bearings were replaced in M9?",
        "Which invoice shows neither bearing was replaced in M9?",
        "Welche Rechnung zeigt, dass keine Lager in M9 ersetzt wurden?",
    ),
)
def test_supported_multilingual_negative_determiners_remain_unassessed(query):
    result = requested_evidence_checks(
        query,
        "Factura FC-62. Se reemplazaron los rodamientos de M9.",
    )
    assert result["status"] == "not_assessed"
    assert result["not_assessed_reason"] == "query_polarity_scope_not_supported"
    assert result["retrieval_disposition"] == "unchanged"
    assert result["required_witnesses"] == result["counterevidence"] == []
