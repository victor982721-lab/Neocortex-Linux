"""Necessary literal witnesses are scoped, never answerability or permission."""

import pytest

from neocortex.semantic.semantic_query_evidence import requested_evidence_checks


INVOICE = "¿Qué factura demuestra el reemplazo de los rodamientos de M9?"
RESTORE = "¿En qué fecha se restauraron y compararon los hashes de Lira?"


@pytest.mark.parametrize("body", [
    "Factura FC-62\nSe reemplazaron los rodamientos de M9.",
    "Factura FC-62\nLos rodamientos de M9 fueron sustituidos.",
    "Factura FC-62\nEl técnico sustituyó los rodamientos de M9, sin cambiar el rotor.",
])
def test_invoice_and_completed_requested_object_are_only_necessary_markers(body):
    result = requested_evidence_checks(INVOICE, body)
    assert result["missing_necessary_witnesses"] == []
    assert result["retrieval_disposition"] == "unchanged"
    assert result["status"] == "necessary_checks_not_failed"
    assert "not_answer_entailment_or_authority" in result["interpretation"]


@pytest.mark.parametrize("body", [
    "Informe de M9. Se mencionó una factura. Se reemplazaron los rodamientos de M9.",
    "Informe de M9. Factura FC-62. Se reemplazaron los rodamientos de M9.",
    "Referencia:\nFactura FC-62\nSe reemplazaron los rodamientos de M9.",
    "Factura citada FC-62\nSe reemplazaron los rodamientos de M9.",
])
def test_invoice_reference_is_not_an_observed_document_kind(body):
    result = requested_evidence_checks(INVOICE, body)
    assert "requested_document_kind:invoice" in result["missing_necessary_witnesses"]
    assert result["retrieval_disposition"] == "related_evidence_only"


@pytest.mark.parametrize("body", [
    "Factura FC-62\nCompra de rodamientos para M9.",
    "Factura FC-62\nReemplazo de rodamientos de M9.",
    "Factura FC-62\nSe inspeccionaron los rodamientos de M9 antes de reemplazar el rotor.",
    "Factura FC-62\nSe reemplazó el rotor de M9 sin retirar los rodamientos.",
])
def test_purchase_noun_or_another_replaced_part_does_not_complete_requested_action(body):
    result = requested_evidence_checks(INVOICE, body)
    assert "completed_action:replacement" in result["missing_necessary_witnesses"]
    assert result["retrieval_disposition"] == "related_evidence_only"


@pytest.mark.parametrize("body", [
    "Factura FC-62\nEl equipo M8 no necesitó sustituir los rodamientos. No es M9.",
    "Factura FC-62\nNo se cambiaron los rodamientos de M8. Se inspeccionó M9.",
])
def test_other_assets_negation_is_not_counterevidence_about_requested_replacement(body):
    result = requested_evidence_checks(INVOICE, body)
    assert result["retrieval_disposition"] == "related_evidence_only"
    assert "requested_action_negated:replacement" not in result["counterevidence"]


def test_same_subject_negated_object_is_preserved_as_counterevidence():
    body = "El equipo M9 se corrigió mediante limpieza y balanceo, sin cambiar los rodamientos."
    result = requested_evidence_checks(INVOICE, body)
    assert result["retrieval_disposition"] == "contradictory_evidence"
    assert "requested_action_negated:replacement" in result["counterevidence"]


def test_named_subject_scope_changes_at_explicit_other_project_and_does_not_transfer_date():
    body = ("Se inspeccionó el respaldo de Lira. El 12 de marzo de 2026 se restauraron "
            "los archivos de Vega y se compararon sus hashes.")
    result = requested_evidence_checks(RESTORE, body)
    assert result["retrieval_disposition"] == "related_evidence_only"
    assert {"completed_action:restore", "completed_action:hash_comparison"} <= set(result["missing_necessary_witnesses"])


def test_other_subject_does_not_poison_later_requested_completed_event():
    body = ("El respaldo de Vega no fue restaurado. El 12 de marzo de 2026 se restauraron "
            "los archivos de Lira y se compararon sus hashes.")
    result = requested_evidence_checks(RESTORE, body)
    assert result["missing_necessary_witnesses"] == []
    assert result["retrieval_disposition"] == "unchanged"
    assert not result["counterevidence"]


@pytest.mark.parametrize("body,state", [
    ("El respaldo de Lira tiene pendiente restaurar los archivos y comparar sus hashes.", "pending"),
    ("El respaldo de Lira está sincronizado. No se han restaurado archivos ni se han comparado hashes.", "negated"),
])
def test_requested_action_pending_or_negation_is_explicit_and_has_exact_span(body, state):
    result = requested_evidence_checks(RESTORE, body)
    assert "completed_action:restore" in result["missing_necessary_witnesses"]
    observation = next(item for item in result["scoped_observations"] if item["requirement"] == "completed_action:restore")
    assert observation["state"] == state
    assert "restaur" in body[observation["start_char"]:observation["end_char"]]


def test_an_unrelated_header_or_preparation_date_does_not_date_completed_actions():
    body = ("Informe del 12 de marzo de 2026 sobre Lira. Se restauraron los archivos de Lira "
            "y se compararon sus hashes.")
    result = requested_evidence_checks(RESTORE, body)
    assert set(result["missing_necessary_witnesses"]) == {"event_linked_date:restore", "event_linked_date:hash_comparison"}


def test_query_title_capitalization_does_not_invent_requested_subjects():
    result = requested_evidence_checks(INVOICE.upper(), "Factura FC-62\nSe reemplazaron los rodamientos de M9.")
    assert result["applicability"]["requested_subjects"] == ["M9"]
    assert result["missing_necessary_witnesses"] == []


def test_legacy_named_subject_does_not_borrow_another_equipment_authorizer():
    result = requested_evidence_checks("¿Quién autorizó la prueba de M9?", "Se inspeccionó M9. Ana autorizó la prueba de M8.")
    assert {"authorization_event", "identified_authorizing_actor"} <= set(result["missing_necessary_witnesses"])


def test_unknown_family_is_not_blanket_abstention():
    result = requested_evidence_checks("¿Cómo se corrigió la temperatura de M9?", "Se limpió la conexión de M9.")
    assert result["status"] == "not_assessed"
    assert result["retrieval_disposition"] == "unchanged"
    assert result["applicability"]["families"] == []


def test_every_nonmissing_observation_has_exact_input_span_with_combining_accents():
    query = RESTORE.replace("Lira", "Céu")
    body = "El 12 de marzo de 2026 se restauraron archivos de Ce\u0301u y se compararon sus hashes."
    result = requested_evidence_checks(query, body)
    assert result["missing_necessary_witnesses"] == []
    for observation in result["scoped_observations"]:
        start, end = observation["start_char"], observation["end_char"]
        assert 0 <= start < end <= len(body)
        assert body[start:end]


@pytest.mark.parametrize("negation", ["", "no "])
def test_conditional_sentence_does_not_assert_coordinated_actions_or_their_dates(negation):
    query = "¿En qué fecha se restauró el respaldo Lira y se compararon los hashes?"
    body = (f"Si el respaldo Lira {negation}fue restaurado el 12 de marzo de 2026 "
            "y se compararon los hashes ese día, se documentará.")
    result = requested_evidence_checks(query, body)
    assert result["retrieval_disposition"] == "related_evidence_only"
    assert result["status"] == "missing"
    assert not result["counterevidence"]
    assert set(result["missing_necessary_witnesses"]) == {
        "completed_action:restore", "completed_action:hash_comparison",
        "event_linked_date:restore", "event_linked_date:hash_comparison",
    }
    assert all(item["state"] == "unknown" for item in result["scoped_observations"]
               if item["requirement"].startswith(("completed_action:", "event_linked_date:")))


@pytest.mark.parametrize("prefix", ["", "Sí, "])
def test_same_completed_clause_without_conditional_retains_necessary_markers(prefix):
    query = "¿En qué fecha se restauró el respaldo Lira y se compararon los hashes?"
    body = (prefix + "el respaldo Lira fue restaurado el 12 de marzo de 2026 "
            "y se compararon los hashes ese día.")
    result = requested_evidence_checks(query, body)
    assert result["missing_necessary_witnesses"] == []
    assert result["retrieval_disposition"] == "unchanged"
    assert result["status"] == "necessary_checks_not_failed"


def test_conditional_does_not_leak_to_a_later_independent_asserted_sentence():
    query = "¿En qué fecha se restauró el respaldo Lira y se compararon los hashes?"
    body = ("Si el respaldo Lira no fue restaurado y no se compararon hashes, se documentará. "
            "El respaldo Lira fue restaurado el 12 de marzo de 2026 y se compararon los hashes ese día.")
    result = requested_evidence_checks(query, body)
    assert result["missing_necessary_witnesses"] == []
    assert result["retrieval_disposition"] == "unchanged"
    assert not result["counterevidence"]
    assert all("Si el" not in body[item["start_char"]:item["end_char"]]
               for item in result["scoped_observations"] if item["requirement"].startswith("completed_action:"))


@pytest.mark.parametrize("conditional", [False, True])
def test_english_if_is_conditional_but_the_same_unprefixed_clause_is_asserted(conditional):
    query = "When was backup Lira restored and its hashes compared?"
    body = ("If " if conditional else "") + "backup Lira was restored on 12 March 2026 and its hashes were compared that day."
    result = requested_evidence_checks(query, body)
    assert result["retrieval_disposition"] == ("related_evidence_only" if conditional else "unchanged")
    assert result["status"] == ("missing" if conditional else "necessary_checks_not_failed")
    assert not result["counterevidence"]
    assert bool(result["missing_necessary_witnesses"]) is conditional


def test_english_conditional_does_not_leak_into_a_later_asserted_sentence():
    query = "When was backup Lira restored and its hashes compared?"
    body = ("If backup Lira was not restored and its hashes were not compared, document the result. "
            "Backup Lira was restored on 12 March 2026 and its hashes were compared that day.")
    result = requested_evidence_checks(query, body)
    assert result["missing_necessary_witnesses"] == []
    assert result["retrieval_disposition"] == "unchanged"
    assert not result["counterevidence"]
