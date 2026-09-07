"""Synthetic oracle tests; no runtime helpers, fixture qrels or model claims."""

from __future__ import annotations

import copy

import pytest

from tools.knowledge_scoped_observation_metrics import validate_scoped_checks


INVOICE = "¿Qué factura demuestra el reemplazo del sello de M9?"
DATED = "When was backup Lira restored and its hashes compared?"


def _observation(requirement, state, scope, text=None, span=None):
    start, end = (None, None) if text is None else span or (0, len(text))
    return {
        "requirement": requirement,
        "state": state,
        "subject_scope": scope,
        "start_char": start,
        "end_char": end,
    }


def _checks(families, subjects, scope, observations, disposition):
    return {
        "applicability": {
            "families": families,
            "requested_subjects": subjects,
            "subject_scope": scope,
        },
        "scoped_observations": observations,
        "retrieval_disposition": disposition,
    }


def _invoice(text, *, subject="M9", state="necessary_marker_present", kind=True, scope="aligned"):
    observations = [
        _observation(
            "requested_subject",
            "necessary_marker_present" if scope == "aligned" else "different_subject",
            scope,
            text,
        ),
        _observation(
            "requested_document_kind:invoice",
            "necessary_marker_present" if kind else "unknown",
            "unresolved" if kind else scope,
            text if kind else None,
            (0, text.index("\n")) if kind else None,
        ),
        _observation(
            "completed_action:replacement", state, scope, text if state != "unknown" else None
        ),
    ]
    disposition = (
        "related_evidence_only"
        if scope != "aligned"
        else "contradictory_evidence"
        if state == "negated"
        else "unchanged"
        if kind and state == "necessary_marker_present"
        else "related_evidence_only"
    )
    return _checks(["documented_action"], [subject], scope, observations, disposition)


def _dated(text, *, states=("necessary_marker_present", "necessary_marker_present"), dates=True):
    observations = [_observation("requested_subject", "necessary_marker_present", "aligned", text)]
    for action, state in zip(("restore", "hash_comparison"), states, strict=True):
        observations.append(
            _observation(
                f"completed_action:{action}", state, "aligned", text if state != "unknown" else None
            )
        )
    for action in ("restore", "hash_comparison"):
        observations.append(
            _observation(
                f"event_linked_date:{action}",
                "necessary_marker_present" if dates else "unknown",
                "aligned",
                text if dates else None,
            )
        )
    disposition = (
        "contradictory_evidence"
        if "negated" in states
        else "unchanged"
        if dates and states == ("necessary_marker_present", "necessary_marker_present")
        else "related_evidence_only"
    )
    return _checks(["dated_completed_actions"], ["Lira"], "aligned", observations, disposition)


def _validate(query, text, checks):
    return validate_scoped_checks(query, text, checks, set(), set())


def test_affirmed_invoice_is_necessary_marker_not_answer_or_authority():
    text = "Factura FC-62\nSe reemplazaron los sellos de M9."
    required, missing, counter, disposition, errors = _validate(INVOICE, text, _invoice(text))
    assert not missing and not counter and not errors
    assert required == {
        "requested_subject",
        "requested_document_kind:invoice",
        "completed_action:replacement",
    }
    assert disposition == "unchanged"


@pytest.mark.parametrize("subject", ["N25", "Z882", "Q7"])
def test_consistent_renaming_does_not_use_fixture_entity_catalog(subject):
    text = f"Factura HX-93\nSe reemplazó el sello de {subject}."
    assert not _validate(INVOICE.replace("M9", subject), text, _invoice(text, subject=subject))[-1]


@pytest.mark.parametrize(
    "first_line",
    ["La nota menciona una factura FC-62", "«Factura FC-62»", 'Se citó "Factura FC-62"'],
)
def test_mentions_or_quoted_heading_do_not_prove_document_kind(first_line):
    text = first_line + "\nSe reemplazaron los sellos de M9."
    verified = _validate(INVOICE, text, _invoice(text, kind=False))
    assert not verified[-1] and "requested_document_kind:invoice" in verified[1]
    assert (
        "scoped_observation_claim_not_verified_in_final_text"
        in _validate(INVOICE, text, _invoice(text))[-1]
    )


def test_unqualified_absence_cannot_hide_an_available_positive_marker():
    text = "Factura FC-62\nSe reemplazaron los sellos de M9."
    forged = _invoice(text, state="unknown")
    assert "scoped_absence_not_verified" in _validate(INVOICE, text, forged)[-1]


def test_negation_outside_internal_action_span_remains_negation():
    text = "Factura FC-62\nNo se reemplazaron los sellos de M9."
    checks = _invoice(text, state="negated")
    action = checks["scoped_observations"][-1]
    action.update(start_char=text.index("se reemplazaron"), end_char=len(text) - 1)
    assert not _validate(INVOICE, text, checks)[-1]
    action["state"] = "necessary_marker_present"
    checks["retrieval_disposition"] = "unchanged"
    assert (
        "scoped_observation_claim_not_verified_in_final_text"
        in _validate(INVOICE, text, checks)[-1]
    )


@pytest.mark.parametrize("prefix", ["No es cierto que ", "Si ", "If "])
def test_denial_and_conditional_outside_internal_span_are_not_completed(prefix):
    text = (
        prefix + "backup Lira was restored on 12 March 2026 and its hashes were compared that day."
    )
    checks = _dated(text)
    for observation in checks["scoped_observations"][1:]:
        observation["start_char"] = len(prefix)
    errors = _validate(DATED, text, checks)[-1]
    assert "scoped_observation_claim_not_verified_in_final_text" in errors
    honest = _dated(text, states=("unknown", "unknown"), dates=False)
    assert not _validate(DATED, text, honest)[-1]


def test_affirmative_si_and_coordinated_dates_are_not_blanket_abstention():
    text = "Sí, backup Lira was restored on 12 March 2026 and its hashes were compared that day."
    assert not _validate(DATED, text, _dated(text))[-1]


def test_different_sentence_does_not_import_conditional_polarity():
    text = "If backup Lira was restored, a notice was planned. Backup Lira was restored on 12 March 2026 and its hashes were compared that day."
    assert not _validate(DATED, text, _dated(text))[-1]


def test_other_subject_action_and_date_do_not_certify_requested_backup():
    text = "Se inspeccionó el respaldo de Lira. El 12 de marzo de 2026 se restauraron los archivos de Vega y se compararon sus hashes."
    checks = _dated(text, states=("unknown", "unknown"), dates=False)
    assert not _validate(DATED, text, checks)[-1]
    assert _validate(DATED, text, _dated(text))[-1]


def test_subject_prefix_is_not_an_identity_match():
    text = "Se reemplazaron los sellos de M90."
    checks = _invoice(text, state="unknown", kind=False, scope="different")
    required, missing, counters, _, errors = _validate(INVOICE, text, checks)
    assert not errors and "requested_subject" in required & missing
    assert counters == {"requested_subject_explicitly_different_or_excluded"}


def test_other_subject_negation_does_not_override_requested_affirmative():
    text = "Factura FC-62\nNo se reemplazó el sello de M8, pero se reemplazó el sello de M9."
    assert not _validate(INVOICE, text, _invoice(text))[-1]


def test_same_subject_date_from_other_sentence_is_not_restoration_date():
    text = "Backup Lira was inspected on 12 March 2026. Backup Lira was restored and its hashes were compared."
    checks = _dated(text, dates=False)
    assert not _validate(DATED, text, checks)[-1]
    assert (
        "scoped_observation_claim_not_verified_in_final_text"
        in _validate(DATED, text, _dated(text))[-1]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(start_char=-1),
        lambda item: item.update(end_char=10000),
        lambda item: item.update(start_char=True),
        lambda item: item.update(start_char=None),
        lambda item: item.update(state="entailed"),
        lambda item: item.update(requirement="unrequested_fact"),
        lambda item: item.update(start_char=0, end_char=7),
    ],
)
def test_malformed_or_nonwitness_final_spans_fail_closed(mutation):
    text = "Factura FC-62\nSe reemplazaron los sellos de M9."
    checks = _invoice(text)
    mutation(checks["scoped_observations"][-1])
    assert _validate(INVOICE, text, checks)[-1]


def test_duplicate_and_missing_observation_are_not_filtered_out():
    text = "Factura FC-62\nSe reemplazaron los sellos de M9."
    checks = _invoice(text)
    duplicate = copy.deepcopy(checks)
    duplicate["scoped_observations"].append(copy.deepcopy(duplicate["scoped_observations"][-1]))
    assert "unknown_or_duplicate_scoped_observation" in _validate(INVOICE, text, duplicate)[-1]
    checks["scoped_observations"].pop()
    assert "scoped_required_observation_missing" in _validate(INVOICE, text, checks)[-1]


def test_unknown_family_has_no_blanket_related_permission():
    text = "La temperatura bajó después de ajustar la conexión."
    checks = _checks([], [], "not_requested", [], "unchanged")
    assert not _validate("¿Qué conexión se calentaba?", text, checks)[-1]
    checks["retrieval_disposition"] = "related_evidence_only"
    assert _validate("¿Qué conexión se calentaba?", text, checks)[-1]


def test_future_with_postposed_condition_is_not_completed_replacement():
    text = "Factura\nEl sello de M9 se reemplazará únicamente si se autoriza el paro."
    result = _validate(INVOICE, text, _invoice(text))
    assert "completed_action:replacement" in result[1]
    assert "scoped_observation_claim_not_verified_in_final_text" in result[-1]


def test_while_other_asset_remained_does_not_transfer_action_subject():
    text = "Factura\nSe reemplazó el sello de X77 mientras M9 permaneció intacto."
    result = _validate(INVOICE, text, _invoice(text))
    assert "completed_action:replacement" in result[1] and result[-1]
    control = text.replace("de X77", "de M9").replace("mientras M9", "mientras X77")
    assert not _validate(INVOICE, control, _invoice(control))[-1]


def test_inspection_date_and_explicit_unknown_dates_are_not_borrowed():
    text = "Backup Lira was inspected on 12 March 2031, but restored on an unknown date and its hashes were compared on an unknown date."
    result = _validate(DATED, text, _dated(text))
    assert {"event_linked_date:restore", "event_linked_date:hash_comparison"}.issubset(result[1])
    assert result[-1]
    assert not _validate(DATED, text, _dated(text, dates=False))[-1]


def test_foreign_marker_overlapping_valid_unit_by_one_character_is_not_a_witness():
    text = (
        "Factura\nSe reemplazó el sello de X77 y se inspeccionó M9 y se reemplazó el sello de M9."
    )
    checks = _invoice(text)
    checks["scoped_observations"][-1].update(
        start_char=8, end_char=text.index("y se reemplazó") + 1
    )
    assert (
        "scoped_observation_claim_not_verified_in_final_text"
        in _validate(INVOICE, text, checks)[-1]
    )
    checks["scoped_observations"][-1].update(
        start_char=text.rindex("se reemplazó"), end_char=len(text)
    )
    assert not _validate(INVOICE, text, checks)[-1]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item["applicability"].update(families=[{}]),
        lambda item: item["scoped_observations"][0].update(state=[]),
        lambda item: item["scoped_observations"][0].update(subject_scope={}),
    ],
)
def test_malformed_json_enums_are_errors_not_exceptions(mutation):
    text = "Factura\nSe reemplazaron los sellos de M9."
    checks = _invoice(text)
    mutation(checks)
    assert _validate(INVOICE, text, checks)[-1]
