"""Synthetic regressions for C08--C10 retrieval utility contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from neocortex.knowledge.knowledge_search import fuse_evidence_rankings
from neocortex.semantic import semantic_search_service
from neocortex.semantic.semantic_query_evidence import (
    query_requests_explicit_title,
    structured_query_support,
)
from neocortex.semantic.semantic_quality import assess_semantic_text
from neocortex.semantic.semantic_service_contracts import SemanticRanking

from tests.test_knowledge_search import _candidate
from tests.test_semantic_service import _calibrated_ranking_hit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def test_c08_electrical_tags_are_not_a_formula_dump_but_real_spreadsheet_noise_is() -> None:
    drawing = (
        "DIAGRAMA TRIFILAR DEL TRANSFORMADOR PRINCIPAL. "
        "-U9 -TC22 X1 IAW1 IBW1 ICW1 15/400 kV 225 MVA. "
        "Las conexiones de control quedan identificadas en la lámina."
    )
    assert assess_semantic_text(drawing, section_kind="pdf_page").eligible
    assert assess_semantic_text(drawing, source_kind="pdf").eligible
    # The exact tag boundaries do not count ``-U9`` as ``-<cell>``.
    assert assess_semantic_text(drawing, section_kind="pdf_page").reason == "eligible"

    formula = "$A$1 $B$2 $C$3 $D$4 $E$5 $F$6 $G$7 $H$8 IF($A$1=1,SUM($B$2:$H$8),0)"
    assert assess_semantic_text(formula, section_kind="xlsx_document").reason == (
        "spreadsheet_formula_dump"
    )


def test_c08_spreadsheet_formula_subtraction_remains_rejected_after_tag_boundary_fix() -> None:
    formula_dump = (
        "A1-B2 C3-D4 E5-F6 G7-H8 I9-J10 K11-L12 "
        "=A1-B2 =C3-D4 =E5-F6"
    )
    assert assess_semantic_text(formula_dump, section_kind="xlsx_document").reason == (
        "spreadsheet_formula_dump"
    )
    drawing = "-U9 -TC22 X1 C3 400 kV 225 MVA IAW1 IBW1"
    assert assess_semantic_text(drawing, section_kind="pdf_page").eligible


def test_c09_compound_identity_date_and_alias_must_share_a_coherent_span() -> None:
    query = "CENTRAL-HCN-05-001 lectura de presión del 6 de agosto de 2026"
    wrong_unit = (
        "CENTRAL-HCN-06-031 Unidad 6. Lectura de presión 0.05 MPa. "
        "Fecha 6 de agosto de 2026."
    )
    right_unit = (
        "CENTRAL-HCN-05-001 Unidad 5. Lectura de presión. "
        "Fecha August 6, 2026."
    )
    wrong = structured_query_support(query, wrong_unit)
    right = structured_query_support(query, right_unit)
    assert wrong["status"] == "mismatch"
    assert "compound_identifier" in wrong["missing_constraints"]
    assert right["coherent"] is True

    alias = structured_query_support(
        "manómetro U6 el 6 de agosto de 2026",
        "Manómetro de nitrógeno de Unidad 6, lectura del 6 de agosto de 2026.",
    )
    assert alias["coherent"] is True


def test_c09_structured_witness_beats_footer_and_can_cross_the_source_floor() -> None:
    query = "lectura del medidor de nitrógeno U6 registrada el 6 de agosto de 2026"
    footer_hit, footer = _calibrated_ranking_hit(1, source_kind="pdf", score=0.491)
    target_hit, target = _calibrated_ranking_hit(2, source_kind="pdf", score=0.3457)
    footer = replace(
        footer,
        snippet="Reporte técnico | 28 de agosto de 2026 Página 1",
        section_provenance={
            "query_support": {
                "structured_support": structured_query_support(
                    query, "Reporte técnico | 28 de agosto de 2026 Página 1"
                )
            }
        },
    )
    target_text = (
        "CENTRAL-HCN-06-031 Transformer Unit 6 - Phase C. "
        "Inspection Date August 6, 2026. Pressure Reading Approx. 0.05 MPa."
    )
    target = replace(
        target,
        snippet=target_text,
        section_provenance={
            "query_support": {
                "structured_support": structured_query_support(query, target_text)
            }
        },
    )
    ranking = SemanticRanking(
        "semantic_text",
        (footer_hit, target_hit),
        (footer, target),
        scanned=31,
        complete=True,
    )
    scoped = semantic_search_service.apply_structured_query_constraints(
        ranking, query=query, limit=1
    )
    assert scoped.hits == (target_hit,)
    calibrated = semantic_search_service.apply_text_retrieval_calibration(
        scoped, selected_model=semantic_search_service.multilingual_text_model()
    )
    assert calibrated.hits == (target_hit,)
    abstention = calibrated.provenance["retrieval_abstention"]
    assert abstention["structured_floor_overrides"] == 1
    assert abstention["rejected_hits"] == 0


def test_c10_filename_request_is_explicit_and_advisory_title_does_not_become_body_evidence() -> None:
    assert query_requests_explicit_title("ORDEN 73142_relevadores") is True
    assert query_requests_explicit_title("1073142 relevadores") is False

    body_hit, body = _calibrated_ranking_hit(10, source_kind="pdf", score=0.8)
    title_hit, title = _calibrated_ranking_hit(11, source_kind="pdf", score=0.9)
    body = replace(body, snippet="Orden impresa 1073142: relevadores")
    title = replace(
        title,
        section_kind="semantic_metadata_title",
        section_id="semantic-content-aware-title-v3",
        snippet="ORDEN 73142_relevadores",
        section_provenance={
            "policy_signature": "semantic-content-aware-title-v3",
            "basis": "basename_without_final_extension",
            "advisory_only": True,
            "mutable_metadata": True,
        },
    )
    body_ranking = SemanticRanking("semantic_text", (body_hit,), (body,), 1, True)
    title_ranking = SemanticRanking(
        "semantic_title",
        (title_hit,),
        (title,),
        1,
        True,
        fusion_weight=0.5,
        provenance={"auto_requested_title_channel": True, "advisory_only": True},
    )
    fused = semantic_search_service._resolve_fused_hits(
        (body_ranking, title_ranking), (), limit=5
    )
    assert fused and {item.ranking for item in fused[0].fused.evidence} == {"semantic_text"}


def test_c10_knowledge_fuser_prefers_scoped_content_without_dropping_citations() -> None:
    weak = _candidate(
        evidence_id="cv-partial", section_id="1", start_char=0, end_char=40,
        ranking="fts_pdf", source_rank=1, snippet="Malpaso CFE",
    )
    strong = _candidate(
        evidence_id="report-full", section_id="2", start_char=0, end_char=120,
        ranking="semantic_text", source_rank=1,
        snippet="Puesta a tierra normativa CFE Malpaso",
    )
    weak = replace(
        weak,
        signal=replace(
            weak.signal,
            query_support={"term_coverage": 0.4, "matched_terms": ["malpaso"]},
        ),
    )
    strong = replace(
        strong,
        signal=replace(
            strong.signal,
            query_support={"term_coverage": 1.0, "phrase_match": True},
        ),
    )
    hits, omitted = fuse_evidence_rankings(
        {"fts_pdf": (weak,), "semantic_text": (strong,)},
        limit=1,
        max_per_resource=2,
        min_section_distance=0,
    )
    assert omitted == 1
    assert hits[0].evidence.evidence_id == "report-full"
    assert hits[0].signals[0].query_support["phrase_match"] is True


def test_c09_knowledge_fuser_drops_a_structured_hard_negative_only_when_a_witness_exists() -> None:
    query = "CENTRAL-HCN-05-001 presión 6 de agosto de 2026"
    hard_negative = _candidate(
        evidence_id="wrong-equipment",
        section_id="1",
        start_char=0,
        end_char=120,
        ranking="fts_pdf",
        source_rank=1,
        snippet="CENTRAL-HCN-06-031 presión 6 de agosto de 2026",
    )
    positive = _candidate(
        evidence_id="matching-equipment",
        section_id="2",
        start_char=0,
        end_char=120,
        ranking="semantic_text",
        source_rank=1,
        snippet="CENTRAL-HCN-05-001 presión 6 de agosto de 2026",
    )
    hard_negative = replace(
        hard_negative,
        signal=replace(
            hard_negative.signal,
            query_support={"structured_support": structured_query_support(query, hard_negative.evidence.snippet or "")},
        ),
    )
    positive = replace(
        positive,
        signal=replace(
            positive.signal,
            query_support={"structured_support": structured_query_support(query, positive.evidence.snippet or "")},
        ),
    )
    hits, omitted = fuse_evidence_rankings(
        {"fts_pdf": (hard_negative,), "semantic_text": (positive,)},
        limit=2,
        max_per_resource=2,
        min_section_distance=0,
    )
    assert [hit.evidence.evidence_id for hit in hits] == ["matching-equipment"]
    assert omitted == 0
