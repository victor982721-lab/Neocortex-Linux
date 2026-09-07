"""Independent presentation-contract examples, never model-quality evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.knowledge_functional_v2_metrics import (
    CHECKS_POLICY,
    METRIC_SCHEMA,
    aggregate_context_v2,
    acceptance_v2,
    expected_necessary_checks,
    score_context_v2,
    verify_operationalization,
)


def _case(
    tmp_path: Path,
    *,
    question="¿Quién autorizó energizar el equipo?",
    body="Se documentó la instalación del equipo, sin una decisión de energización.",
    negative=True,
):
    path = tmp_path / "synthetic.txt"
    source_text = "Observación sintética\n\n" + body
    path.write_text(source_text)
    entry = {
        "logical_resource_id": "A",
        "source_text": source_text,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    source = {
        "source_id": "S1",
        "path": str(path),
        "resource_id": "R1",
        "revision_id": "REV1",
        "revision_state": "current",
        "processing_signature": "synthetic-v1",
    }
    required, missing = expected_necessary_checks(question, body)
    checks = {
        "policy_signature": CHECKS_POLICY,
        "basis": "input_text",
        "interpretation": "necessary_conditions_only_not_answer_entailment_or_authority",
        "recomputed_for": "emitted_excerpt",
        "inspected_scope": "emitted_excerpt_only",
        "evaluated_chars": len(body),
        "evaluation_truncated": False,
        "query_truncated": False,
        "required_witnesses": sorted(required),
        "missing_necessary_witnesses": sorted(missing),
        "counterevidence": [],
        "status": "missing"
        if missing
        else "necessary_checks_not_failed"
        if required
        else "not_assessed",
    }
    citation = {
        "citation_id": "C1",
        "source_id": "S1",
        "evidence_id": "E1",
        "excerpt": body,
        "fragment_state": "full",
        "locator": {"section_id": "fulltext"},
        "emitted_extent": {
            "units": "characters",
            "basis": "emitted_excerpt",
            "start_char": 0,
            "end_char": len(body),
        },
        "witness_checks": checks,
        "role_counterevidence": [],
        "evidence_disposition": "related_only" if missing else "evidence_candidate",
    }
    payload = {
        "schema": "neocortex.context-response/v2",
        "response_version": 2,
        "read_only": True,
        "query": question,
        "sources": [source],
        "citations": [citation],
    }
    query = {
        "query_id": "test-query",
        "text": question,
        "kind": "negative" if negative else "positive",
        "relevance": {} if negative else {"A": 3},
    }
    hits = [
        {
            "resource": {"current_path": str(path), "resource_id": "R1"},
            "revision": {"revision_id": "REV1"},
            "evidence": {"evidence_id": "E1"},
        }
    ]
    return query, payload, {str(path): entry}, hits


def test_supported_related_material_keeps_legacy_count_without_becoming_sufficient(tmp_path):
    case = _case(tmp_path)
    result = score_context_v2(*case)
    assert result["legacy_negative_context_selections"] == 1
    assert result["unsupported_sufficient_evidence"] == 0
    assert result["verified_related_material"] == 1
    assert result["unknown_disposition_citations"] == 0


def test_compact_v2_omits_false_flags_but_keeps_final_scope_and_length(tmp_path):
    case = _case(tmp_path)
    checks = case[1]["citations"][0]["witness_checks"]
    for key in ("basis", "query_truncated", "evaluation_truncated"):
        checks.pop(key)
    assert score_context_v2(*case)["verified_related_material"] == 1
    checks["query_truncated"] = True
    assert score_context_v2(*case)["unsupported_sufficient_evidence"] == 1


def test_owner_extent_does_not_masquerade_as_the_final_emitted_range(tmp_path):
    case = _case(tmp_path)
    citation = case[1]["citations"][0]
    citation["locator"].update(start_char=0, end_char=len(citation["excerpt"]))
    citation["extent"] = {
        "units": "characters",
        "bounded": False,
        "exact_reference_range": {"basis": "source_section", "start_char": 0, "end_char": 999},
    }
    result = score_context_v2(*case)
    assert "owner_extent_disagrees_with_source_locator" in result["citations"][0]["errors"]
    assert result["unsupported_sufficient_evidence"] == 1


def test_negative_evidence_candidate_always_counts_as_a_failure(tmp_path):
    case = _case(
        tmp_path,
        question="¿Qué planeta tiene vida demostrada?",
        body="El expediente contiene una medición industrial.",
    )
    result = score_context_v2(*case)
    assert result["unsupported_sufficient_evidence"] == 1
    assert result["legacy_negative_context_selections"] == 1


@pytest.mark.parametrize(
    "mutation, expected_error",
    [
        (
            lambda item: item.update(evidence_disposition="related_only", witness_checks=None),
            "missing_necessary_witness_checks",
        ),
        (
            lambda item: item["witness_checks"].update(
                required_witnesses=[], missing_necessary_witnesses=[], status="not_assessed"
            ),
            "necessary_witness_claim_not_reproducible_from_final_text",
        ),
        (
            lambda item: item["witness_checks"].update(evaluated_chars=999),
            "necessary_checks_not_complete_over_final_excerpt",
        ),
        (
            lambda item: item["emitted_extent"].update(end_char=999),
            "final_excerpt_extent_unverified",
        ),
        (
            lambda item: item["witness_checks"].update(inspected_scope="entire_corpus"),
            "necessary_checks_do_not_bind_final_scope",
        ),
        (
            lambda item: item["witness_checks"].update(required_witnesses=None),
            "invalid_necessary_witness_lists",
        ),
        (lambda item: item.update(evidence_disposition=[]), "unknown_evidence_disposition"),
    ],
)
def test_disposition_or_scope_assertions_do_not_grade_themselves(
    tmp_path, mutation, expected_error
):
    case = _case(tmp_path)
    mutation(case[1]["citations"][0])
    result = score_context_v2(*case)
    assert expected_error in result["citations"][0]["errors"]
    assert result["unknown_disposition_citations"] == 1
    assert result["unsupported_sufficient_evidence"] == 1


def test_unknown_query_cannot_be_blanket_related_to_escape_negative_metric(tmp_path):
    case = _case(tmp_path, question="¿Qué galaxia aparece aquí?")
    case[1]["citations"][0]["evidence_disposition"] = "related_only"
    result = score_context_v2(*case)
    assert "related_only_without_verified_missing_requirement" in result["citations"][0]["errors"]
    assert result["unsupported_sufficient_evidence"] == 1


def test_full_gold_factual_body_is_proven_but_related_or_prefix_is_not(tmp_path):
    case = _case(tmp_path, question="¿Qué se observó en el equipo?", negative=False)
    proven = score_context_v2(*case)
    assert proven["positive_sufficient_proven"] is True
    prefix_case = copy.deepcopy(case)
    citation = prefix_case[1]["citations"][0]
    citation["excerpt"] = citation["excerpt"][:25]
    citation["witness_checks"]["evaluated_chars"] = 25
    citation["emitted_extent"]["end_char"] = 25
    prefix = score_context_v2(*prefix_case)
    assert not prefix["positive_sufficient_proven"]
    assert prefix["positive_sufficiency_unknown"]
    case[1]["citations"][0]["evidence_disposition"] = "related_only"
    assert not score_context_v2(*case)["positive_sufficient_proven"]


def test_all_abstain_cannot_satisfy_positive_sufficiency(tmp_path):
    case = _case(tmp_path, question="¿Qué se observó?", negative=False)
    case[1]["sources"] = []
    case[1]["citations"] = []
    result = aggregate_context_v2([score_context_v2(*case)])
    assert result["positive_sufficient_proven_queries"] == 0
    assert result["positive_sufficiency_unknown_queries"] == 1


def test_outside_retrieval_reference_or_changed_bytes_are_never_verified(tmp_path):
    case = _case(tmp_path)
    case[1]["citations"][0]["evidence_id"] = "invented"
    assert (
        "citation_not_pinned_to_captured_retrieval"
        in score_context_v2(*case)["citations"][0]["errors"]
    )
    case[1]["citations"][0]["evidence_id"] = "E1"
    Path(next(iter(case[2]))).write_text("bytes changed")
    result = score_context_v2(*case)
    assert "source_bytes_not_pinned" in result["citations"][0]["errors"]
    assert result["unsupported_sufficient_evidence"] == 1


def test_final_crop_invalidates_a_counterwitness_from_omitted_text(tmp_path):
    body = "No se presentó incidente alguno durante la instalación."
    case = _case(tmp_path, question="¿Qué ocurrió durante el incidente?", body=body)
    citation = case[1]["citations"][0]
    citation["evidence_disposition"] = "contradictory"
    citation["role_counterevidence"] = [
        {
            "policy_signature": "query-role-counterevidence-v1",
            "basis": "input_text",
            "interpretation": "literal_counterevidence_not_entailment_or_authority",
            "start_char": 0,
            "end_char": len(body),
            "text": body,
            "reasons": ["literal_requested_event_occurrence_is_negated"],
            "evaluation_truncated": False,
            "query_truncated": False,
        }
    ]
    assert score_context_v2(*case)["verified_related_material"] == 1
    citation["excerpt"] = "No se presentó"
    citation["emitted_extent"]["end_char"] = len(citation["excerpt"])
    citation["witness_checks"]["evaluated_chars"] = len(citation["excerpt"])
    result = score_context_v2(*case)
    assert "counterevidence_span_outside_final_excerpt" in result["citations"][0]["errors"]
    assert result["unsupported_sufficient_evidence"] == 1


def test_necessary_authorization_witness_is_literal_not_authority_or_entailment():
    query = "¿Quién autorizó energizar?"
    required, missing = expected_necessary_checks(
        query, "El supervisor autorizó energizar el equipo."
    )
    assert required == {"authorization_event", "identified_authorizing_actor"}
    assert missing == set()
    _, absent = expected_necessary_checks(query, "Se autorizó energizar el equipo.")
    assert absent == {"identified_authorizing_actor"}


def test_v2_gate_never_claims_legacy_zero_or_passes_all_abstention():
    retrieval = {
        "success_at_5": 1.0,
        "positive_queries": 8,
        "positive_successes_at_5": 8,
        "ndcg_at_10": 1.0,
        "negative_unsupported_evidence": 3,
        "locator_integrity": 1.0,
        "citation_invalid_queries": 0,
        "citation_checks": 12,
        "execution_invalid_queries": 0,
        "real_vector_queries": 10,
        "queries": 10,
    }
    typed = {
        "schema": METRIC_SCHEMA,
        "queries": 10,
        "positive_queries": 8,
        "positive_sufficient_proven_queries": 8,
        "positive_sufficiency_unknown_queries": 0,
        "legacy_negative_context_selections": 3,
        "unsupported_sufficient_evidence": 0,
        "unknown_disposition_citations": 0,
        "contract_invalid_queries": 0,
    }
    assert all(acceptance_v2(retrieval, typed, retrieval).values())
    assert retrieval["negative_unsupported_evidence"] == 3
    assert typed["legacy_negative_context_selections"] == 3
    abstention = {
        **typed,
        "positive_sufficient_proven_queries": 0,
        "positive_sufficiency_unknown_queries": 8,
    }
    assert not acceptance_v2(retrieval, abstention, retrieval)["all_positive_factual_bodies_proven"]


def test_supplement_is_explicit_pinned_and_does_not_replace_frozen_judgments(tmp_path):
    root = Path(__file__).parent / "fixtures" / "knowledge_functional_v1"
    path = root / "operationalization-v2.2.json"
    freeze_sha = hashlib.sha256((root / "freeze.json").read_bytes()).hexdigest()
    assert (
        verify_operationalization(path, frozen_dataset_sha256=freeze_sha)
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    contract = json.loads(path.read_text())
    assert contract["original_dev_legacy_negative_context_selections"] == 8
    assert contract["legacy_baseline_reclassified"] is False
    assert (
        contract["documentation_sha256"]
        == hashlib.sha256((root / "OPERATIONALIZATION_V2_2.md").read_bytes()).hexdigest()
    )
    contract["adapter_sha256"] = "0" * 64
    altered = tmp_path / "altered-supplement.json"
    altered.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match="differs from its recorded supplement"):
        verify_operationalization(altered, frozen_dataset_sha256=freeze_sha)


def test_original_v2_contract_remains_immutable_and_explicitly_superseded():
    root = Path(__file__).parent / "fixtures" / "knowledge_functional_v1"
    path = root / "operationalization-v2.json"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "6898c112bf670eec1d29b03881e3275407a6d5fa011d79618700cae7c34f4969"
    )
    contract = json.loads(path.read_text())
    assert (
        contract["adapter_sha256"]
        == "c6acac048f9ed649c8a678ed9ca8608c7fbaaae0442bcf6bce9c18fa2501e25d"
    )
    assert contract["schema"] == "neocortex.functional-context-operationalization/v2"


def test_v2_1_contract_and_its_implementation_remain_historically_pinned():
    root = Path(__file__).parent / "fixtures" / "knowledge_functional_v1"
    path = root / "operationalization-v2.1.json"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "5c44480eb54b18268a2133e8c8ea958a6a9167137f7d7c1a9f331d2a624dfb5e"
    )
    assert (
        json.loads(path.read_text())["adapter_sha256"]
        == "acb0728fb351c67457d03fff4da7b745de21c2cfb6fd9ad26ec1840c2ea82795"
    )
    with pytest.raises(ValueError, match="incompatible"):
        verify_operationalization(
            path,
            frozen_dataset_sha256=hashlib.sha256((root / "freeze.json").read_bytes()).hexdigest(),
        )


def _scoped_invoice_case(tmp_path, *, negative):
    body = (
        "El equipo M9 permaneció identificado. No se reemplazó el sello."
        if negative
        else "Factura MX-62\nSe reemplazó el sello de M9."
    )
    case = _case(
        tmp_path,
        question="¿Qué factura demuestra el reemplazo del sello de M9?",
        body=body,
        negative=negative,
    )
    citation = case[1]["citations"][0]
    action_start = body.index("se reemplazó") if negative else body.index("Se reemplazó")
    subject_start, subject_end = (0, body.index(".")) if negative else (action_start, len(body))
    citation["witness_checks"].update(
        policy_signature="query-necessary-evidence-checks-v2",
        required_witnesses=[
            "requested_subject",
            "requested_document_kind:invoice",
            "completed_action:replacement",
        ],
        missing_necessary_witnesses=[
            "requested_document_kind:invoice",
            "completed_action:replacement",
        ]
        if negative
        else [],
        counterevidence=["requested_action_negated:replacement"] if negative else [],
        status="missing" if negative else "necessary_checks_not_failed",
        retrieval_disposition="contradictory_evidence" if negative else "unchanged",
        applicability={
            "families": ["documented_action"],
            "requested_subjects": ["M9"],
            "subject_scope": "aligned",
        },
        scoped_observations=[
            {
                "requirement": "requested_subject",
                "state": "necessary_marker_present",
                "subject_scope": "aligned",
                "start_char": subject_start,
                "end_char": subject_end,
            },
            {
                "requirement": "requested_document_kind:invoice",
                "state": "unknown" if negative else "necessary_marker_present",
                "subject_scope": "aligned" if negative else "unresolved",
                "start_char": None if negative else 0,
                "end_char": None if negative else body.index("\n"),
            },
            {
                "requirement": "completed_action:replacement",
                "state": "negated" if negative else "necessary_marker_present",
                "subject_scope": "aligned",
                "start_char": action_start,
                "end_char": len(body),
            },
        ],
    )
    citation["evidence_disposition"] = "contradictory" if negative else "evidence_candidate"
    return case


def test_scoped_policy_v2_counter_requires_final_aligned_action_span(tmp_path):
    case = _scoped_invoice_case(tmp_path, negative=True)
    result = score_context_v2(*case)
    assert result["unsupported_sufficient_evidence"] == 0
    assert result["verified_related_material"] == result["legacy_negative_context_selections"] == 1
    assert result["unknown_disposition_citations"] == 0
    case[1]["citations"][0]["witness_checks"]["scoped_observations"][-1].update(
        start_char=0, end_char=9
    )
    altered = score_context_v2(*case)
    assert (
        altered["unknown_disposition_citations"] == altered["unsupported_sufficient_evidence"] == 1
    )


def test_scoped_policy_v2_positive_invoice_is_not_all_abstention(tmp_path):
    case = _scoped_invoice_case(tmp_path, negative=False)
    result = score_context_v2(*case)
    assert result["positive_sufficient_proven"]
    assert result["unknown_disposition_citations"] == 0
    case[1]["citations"][0]["evidence_disposition"] = "related_only"
    assert not score_context_v2(*case)["positive_sufficient_proven"]


def _subject_exclusion_case(tmp_path, question, body):
    case = _case(tmp_path, question=question, body=body)
    citation = case[1]["citations"][0]
    citation["evidence_disposition"] = "contradictory"
    citation["role_counterevidence"] = [
        {
            "policy_signature": "query-role-counterevidence-v1",
            "basis": "input_text",
            "interpretation": "literal_counterevidence_not_entailment_or_authority",
            "start_char": 0,
            "end_char": len(body),
            "text": body,
            "reasons": ["requested_named_subject_is_explicitly_excluded"],
            "evaluation_truncated": False,
            "query_truncated": False,
        }
    ]
    return case


@pytest.mark.parametrize(
    "question,body",
    [
        ("¿Qué ocurrió con el depósito L4?", "El registro no corresponde a L4."),
        ("¿Qué ocurrió con el depósito L4?", "El registro no corresponde al depósito L4."),
        (
            "¿Qué ocurrió con la válvula auxiliar V42?",
            "El registro no corresponde a la válvula auxiliar V42.",
        ),
        (
            "¿Qué ocurrió con el motor principal M77?",
            "El informe no corresponde al motor principal M77.",
        ),
        (
            "¿Qué ocurrió en la unidad experimental X91?",
            "El registro no corresponde a la unidad experimental X91.",
        ),
    ],
)
def test_v2_1_recognizes_only_the_same_explicitly_named_nominal_subject(tmp_path, question, body):
    result = score_context_v2(*_subject_exclusion_case(tmp_path, question, body))
    assert result["unknown_disposition_citations"] == 0
    assert result["verified_related_material"] == 1
    assert result["legacy_negative_context_selections"] == 1
    assert result["unsupported_sufficient_evidence"] == 0


@pytest.mark.parametrize(
    "body",
    [
        "El registro no corresponde al depósito L8.",
        "El registro no corresponde al proveedor de L4.",
        "El registro no corresponde al motor L4.",
        "El registro no corresponde al depósito L8 sino al depósito L4.",
        "Si el registro no corresponde al depósito L4, se preparará otro.",
        "El registro no corresponde al depósito L4 si cambia la numeración.",
        "Cuando cambie la numeración, el registro no corresponde al depósito L4.",
        "Es posible que el registro no corresponde al depósito L4.",
        "No es cierto que el registro no corresponde al depósito L4.",
        "Nunca se afirmó que el registro no corresponde al depósito L4.",
        "Se negó que el registro no corresponde al depósito L4.",
        "El registro no corresponde al depósito. L4 aparece en otra frase.",
    ],
)
def test_v2_1_keeps_other_entities_conditionals_and_negation_unverified(tmp_path, body):
    result = score_context_v2(
        *_subject_exclusion_case(tmp_path, "¿Qué ocurrió con el depósito L4?", body)
    )
    assert result["unknown_disposition_citations"] == 1
    assert result["unsupported_sufficient_evidence"] == 1
    assert "counterevidence_reason_not_verified_in_final_text" in result["citations"][0]["errors"]


def test_v2_1_never_removes_nominal_words_to_make_a_forged_span_match(tmp_path):
    case = _subject_exclusion_case(
        tmp_path, "¿Qué ocurrió con el depósito L4?", "El registro no corresponde al depósito L4."
    )
    witness = case[1]["citations"][0]["role_counterevidence"][0]
    witness["text"] = "El registro no corresponde al L4."
    result = score_context_v2(*case)
    assert result["unknown_disposition_citations"] == 1
    assert "counterevidence_text_not_exact_final_span" in result["citations"][0]["errors"]


@pytest.mark.parametrize(
    "body",
    [
        "No es cierto que el registro no corresponde al depósito L4.",
        "Si el registro no corresponde al depósito L4, se preparará otro.",
        "El registro no corresponde al depósito L4 si cambia la numeración.",
        "No es cierto que\nel registro no corresponde al depósito L4.",
        "Es posible que el registro no corresponde al depósito L4.",
    ],
)
def test_v2_1_internal_span_cannot_hide_same_sentence_negation_or_condition(tmp_path, body):
    phrase = "el registro no corresponde al depósito L4"
    case = _subject_exclusion_case(tmp_path, "¿Qué ocurrió con el depósito L4?", body)
    witness = case[1]["citations"][0]["role_counterevidence"][0]
    start = body.casefold().index(phrase.casefold())
    end = start + len(phrase)
    witness.update(start_char=start, end_char=end, text=body[start:end])
    result = score_context_v2(*case)
    assert result["unknown_disposition_citations"] == 1
    assert result["unsupported_sufficient_evidence"] == 1
    assert "counterevidence_reason_not_verified_in_final_text" in result["citations"][0]["errors"]


@pytest.mark.parametrize(
    "prefix",
    [
        "No se emitió una autorización. ",
        "Si falta un dato, se revisa el informe. ",
    ],
)
def test_v2_1_does_not_borrow_negation_from_another_sentence(tmp_path, prefix):
    phrase = "El registro no corresponde al depósito L4"
    body = prefix + phrase + "."
    case = _subject_exclusion_case(tmp_path, "¿Qué ocurrió con el depósito L4?", body)
    witness = case[1]["citations"][0]["role_counterevidence"][0]
    witness.update(start_char=len(prefix), end_char=len(prefix) + len(phrase), text=phrase)
    result = score_context_v2(*case)
    assert result["unknown_disposition_citations"] == 0
    assert result["verified_related_material"] == 1
