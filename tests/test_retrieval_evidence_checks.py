"""Pure bounded role/necessary-witness checks, independent of models and IDs."""

from __future__ import annotations

import json
import unicodedata

import pytest

from neocortex.semantic.semantic_query_evidence import (
    MAX_EVIDENCE_CHECK_CHARS,
    MAX_EVIDENCE_QUERY_CHARS,
    query_role_counterevidence,
    requested_evidence_checks,
)


TEST_CAPABILITIES = ("base",)
pytestmark = pytest.mark.capability("base")


@pytest.mark.parametrize("text", (
    "No se presentó incidente durante la instalación de la bomba.",
    "Es una guía general, no es un registro de un incidente.",
))
def test_explicit_nonoccurrence_or_nonrecord_can_deprioritize_event_evidence(text: str) -> None:
    witnesses = query_role_counterevidence("Encuentra el registro del incidente al instalar la bomba", text)
    assert witnesses
    for witness in witnesses:
        assert text[witness["start_char"]:witness["end_char"]] == witness["text"]
        assert len(witness["text"]) <= 240
        assert witness["basis"] == "input_text"
        assert witness["interpretation"] == "literal_counterevidence_not_entailment_or_authority"


@pytest.mark.parametrize("text", (
    "El incidente ocurrió al colocar la bomba, no durante el apriete.",
    "No hubo agua junto al equipo. El incidente ocurrió durante el izaje.",
    "Antes de la maniobra ocurrió un incidente, quedó registrado en esta hoja.",
    "Manual de operación. El anexo registra el incidente ocurrido durante instalación.",
))
def test_unrelated_negation_prior_timing_or_manual_title_alone_never_downgrades(text: str) -> None:
    assert query_role_counterevidence("Encuentra el registro del incidente al instalar el equipo", text) == []


@pytest.mark.parametrize("query", (
    "¿Cómo instalar una bomba?",
    "Explica el procedimiento paso a paso",
    "Muéstrame un manual paso a paso para registrar un incidente",
    "Guía paso a paso del torque aplicado",
))
def test_instruction_and_step_by_step_requests_do_not_require_an_observed_event(query: str) -> None:
    assert query_role_counterevidence(query, "Guía genérica, no es un registro de un incidente.") == []


def test_what_happened_remains_a_factual_request_despite_step_by_step_presentation() -> None:
    assert query_role_counterevidence(
        "¿Qué pasó durante el incidente? Explícalo paso a paso",
        "No se presentó incidente durante la instalación",
    )


def test_authorization_absence_is_not_filled_from_incident_citation() -> None:
    checks = requested_evidence_checks("¿Quién autorizó energizar la bomba?", "La bomba sufrió un golpe y se retiró.")
    assert checks["status"] == "missing"
    assert checks["missing_necessary_witnesses"] == ["authorization_event", "identified_authorizing_actor"]
    assert checks["retrieval_disposition"] == "related_evidence_only"


@pytest.mark.parametrize("text", (
    "Se autorizó energizar la bomba.",
    "Durante la reunión se autorizó energizar la bomba.",
    "Después autorizó energizar la bomba.",
))
def test_passive_or_implicit_authorization_does_not_identify_an_actor(text: str) -> None:
    checks = requested_evidence_checks("¿Quién autorizó energizar la bomba?", text)
    assert checks["missing_necessary_witnesses"] == ["identified_authorizing_actor"]


@pytest.mark.parametrize("text", (
    "La supervisora autorizó energizar la bomba.",
    "Ana autorizó energizar la bomba. No se modificó el equipo.",
    "La supervisora autorizó energizar la bomba; no cambió el procedimiento.",
    "La supervisora autorizó energizar la bomba y no modificó el ajuste.",
    "No se modificó el ajuste y la supervisora autorizó energizar la bomba.",
))
def test_actor_and_event_presence_are_necessary_not_sufficient_answer_checks(text: str) -> None:
    checks = requested_evidence_checks("¿Quién autorizó energizar la bomba?", text)
    assert checks["status"] == "necessary_checks_not_failed"
    assert checks["interpretation"] == "necessary_conditions_only_not_answer_entailment_or_authority"


def test_nobody_authorized_is_not_an_actor_or_an_authorization_event() -> None:
    checks = requested_evidence_checks("¿Quién autorizó energizar la bomba?", "Nadie autorizó energizar la bomba.")
    assert checks["missing_necessary_witnesses"] == ["authorization_event", "identified_authorizing_actor"]


def test_damage_during_lifting_does_not_invent_torque_or_torque_causality() -> None:
    checks = requested_evidence_checks("¿Qué torque aplicado causó el golpe?", "El incidente ocurrió al izar, no durante el apriete.")
    assert checks["missing_necessary_witnesses"] == ["applied_torque_value_with_unit", "asserted_torque_to_damage_causal_link"]
    assert checks["counterevidence"] == ["source_explicitly_places_incident_outside_tightening"]


def test_torque_value_and_literal_causal_link_are_kept_distinct() -> None:
    checks = requested_evidence_checks("¿Qué torque aplicado causó el golpe?", "Se aplicó torque de 35 Nm durante el apriete.")
    assert checks["missing_necessary_witnesses"] == ["asserted_torque_to_damage_causal_link"]


@pytest.mark.parametrize("value", ("35.5 Nm", "35,5 N·m"))
def test_decimal_torque_values_are_not_split_into_different_sentences_or_clauses(value: str) -> None:
    checks = requested_evidence_checks(
        "¿Qué torque aplicado causó el golpe?", f"Se aplicó torque de {value} durante el apriete."
    )
    assert checks["missing_necessary_witnesses"] == ["asserted_torque_to_damage_causal_link"]


def test_planned_delivery_or_negative_receipt_is_not_an_acknowledgment() -> None:
    checks = requested_evidence_checks("Encuentra el acuse del 3 de agosto", "Se acordó entregar el 3 de agosto. El paquete no fue recibido.")
    assert checks["missing_necessary_witnesses"] == ["affirmative_receipt_or_delivery_acknowledgment", "receipt_linked_to_requested_date"]


def test_receipt_date_must_be_in_the_affirmative_receipt_witness() -> None:
    checks = requested_evidence_checks("Encuentra el acuse del 3 de agosto", "Fue recibido el 4 de agosto. El 3 de agosto se preparó el paquete.")
    assert checks["missing_necessary_witnesses"] == ["receipt_linked_to_requested_date"]


def test_never_received_is_not_an_affirmative_receipt() -> None:
    checks = requested_evidence_checks("Encuentra el acuse del 3 de agosto", "Nunca fue recibido el paquete el 3 de agosto.")
    assert checks["missing_necessary_witnesses"] == ["affirmative_receipt_or_delivery_acknowledgment", "receipt_linked_to_requested_date"]


def test_negation_in_another_sentence_does_not_erase_affirmative_receipt() -> None:
    checks = requested_evidence_checks("Encuentra el acuse del 3 de agosto", "Fue recibido el paquete el 3 de agosto. No se alteraron los planos.")
    assert checks["status"] == "necessary_checks_not_failed"


def test_unassessed_questions_never_become_answer_sufficient_by_default() -> None:
    checks = requested_evidence_checks("¿Por qué se detuvo la bomba?", "Se detuvo durante la revisión.")
    assert checks["status"] == "not_assessed"
    assert checks["interpretation"] == "necessary_conditions_only_not_answer_entailment_or_authority"


@pytest.mark.parametrize("decomposed", (False, True))
def test_role_spans_are_exact_with_accents_and_late_trigger(decomposed: bool) -> None:
    text = "Ámbito técnico " * 35 + "No se presentó incidente durante la instalación."
    if decomposed:
        text = unicodedata.normalize("NFD", text)
    witness, = query_role_counterevidence("Encuentra el incidente de instalación", text)
    assert witness["start_char"] > 0
    assert text[witness["start_char"]:witness["end_char"]] == witness["text"]
    assert "No se present" in witness["text"]
    assert len(witness["text"]) <= 240


@pytest.mark.parametrize("control", ("\x00", "\t", "\u202e"))
def test_control_characters_omit_excerpt_without_destroying_local_span(control: str) -> None:
    text = f"No se presentó incidente durante{control}la instalación."
    witness, = query_role_counterevidence("Encuentra el incidente de instalación", text)
    assert "text" not in witness and witness["text_omitted_reason"] == "control_characters"
    assert 0 <= witness["start_char"] < witness["end_char"] <= len(text)
    json.dumps(witness, ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_role_witness_count_and_evaluated_text_are_bounded() -> None:
    witnesses = query_role_counterevidence("Encuentra el incidente", "No se presentó incidente. " * 5_000)
    assert len(witnesses) == 4
    assert all(witness["evaluation_truncated"] is True for witness in witnesses)
    assert all(witness["end_char"] <= MAX_EVIDENCE_CHECK_CHARS for witness in witnesses)
    assert query_role_counterevidence("Encuentra el incidente", "x" * MAX_EVIDENCE_CHECK_CHARS + ". No se presentó incidente.") == []


def test_late_full_source_can_reevaluate_missing_checks_without_claiming_global_absence() -> None:
    query = "¿Quién autorizó energizar la bomba?"
    tail = "La supervisora autorizó energizar la bomba."
    limited = requested_evidence_checks(query, "x" * MAX_EVIDENCE_CHECK_CHARS + ". " + tail)
    reevaluated = requested_evidence_checks(query, tail)
    assert limited["status"] == "missing" and limited["evaluation_truncated"] is True
    assert limited["evaluated_chars"] == MAX_EVIDENCE_CHECK_CHARS
    assert reevaluated["status"] == "necessary_checks_not_failed"
    assert reevaluated["evaluation_truncated"] is False


def test_overlong_query_is_explicitly_bounded_without_inventing_unread_requirements() -> None:
    checks = requested_evidence_checks("x" * MAX_EVIDENCE_QUERY_CHARS + " ¿Quién autorizó?", "No hubo autorización.")
    assert checks["query_truncated"] is True and checks["status"] == "not_assessed"


@pytest.mark.parametrize("function", (query_role_counterevidence, requested_evidence_checks))
@pytest.mark.parametrize(("query", "text"), ((None, "text"), ("query", [])))
def test_nonstring_inputs_fail_before_evidence_evaluation(function, query, text) -> None:
    with pytest.raises(ValueError, match="string query and text"):
        function(query, text)
