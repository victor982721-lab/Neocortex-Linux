"""C29 hard compound-identifier constraints across retrieval channels."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path

import pytest

from neocortex.semantic import semantic_search_service as service
from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalRanking,
    LexicalStatePaths,
)
from neocortex.semantic.semantic_query_evidence import structured_query_support
from neocortex.semantic.semantic_service_contracts import SemanticRanking

from tests.test_semantic_service import _calibrated_ranking_hit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


_QUERY = "PLANT-ABC-05-001 presión 12 de marzo de 2027"
_WRONG_TEXT = "PLANT-ABC-06-031 presión 0.05 MPa 12 de marzo de 2027"
_RIGHT_TEXT = "PLANT-ABC-05-001 presión 0.05 MPa 12 de marzo de 2027"


def _ranking_with_texts(*texts: str) -> SemanticRanking:
    hits = []
    resolved = []
    for ref_id, text in enumerate(texts, 1):
        hit, witness = _calibrated_ranking_hit(ref_id, source_kind="pdf", score=0.8 - ref_id / 100)
        hits.append(hit)
        resolved.append(replace(witness, snippet=text, end_char=len(text)))
    return SemanticRanking(
        "semantic_text", tuple(hits), tuple(resolved), scanned=len(hits), complete=True,
    )


def test_c29_missing_exact_identifier_abstains_instead_of_fallback() -> None:
    ranking = _ranking_with_texts(_WRONG_TEXT)
    scoped = service.apply_structured_query_constraints(ranking, query=_QUERY, limit=5)
    assert scoped.hits == ()
    details = scoped.provenance["structured_query_constraints"]
    assert isinstance(details, Mapping)
    assert details["hard_identity_required"] is True
    assert details["hard_identity_abstention"] is True
    calibrated = service.apply_text_retrieval_calibration(
        scoped, selected_model=service.multilingual_text_model(),
    )
    abstention = calibrated.provenance["retrieval_abstention"]
    assert isinstance(abstention, Mapping)
    assert abstention["query_abstained"] is True
    assert abstention["abstention_reason"] == "exact_identifier_witness_unavailable"


def test_c29_exact_identifier_witness_wins_before_floor() -> None:
    ranking = _ranking_with_texts(_WRONG_TEXT, _RIGHT_TEXT)
    scoped = service.apply_structured_query_constraints(ranking, query=_QUERY, limit=1)
    assert scoped.hits and scoped.hits[0].ref_id == 2
    assert scoped.resolved[0].snippet == _RIGHT_TEXT


def test_c29_title_metaphor_is_not_a_witness_for_missing_identifier() -> None:
    ranking = _ranking_with_texts("Informe metafórico de la planta y presión")
    title = replace(ranking, name="semantic_title")
    scoped = service.apply_structured_query_constraints(title, query=_QUERY, limit=5)
    assert scoped.hits == ()


def test_c29_lexical_channel_is_filtered_before_fusion() -> None:
    wrong_ranking = _ranking_with_texts(_WRONG_TEXT)
    right_ranking = _ranking_with_texts(_RIGHT_TEXT)
    wrong = LexicalRanking(
        "pdf", None, LexicalAvailability.AVAILABLE, _QUERY, wrong_ranking.resolved,
    )
    right = LexicalRanking(
        "pdf", None, LexicalAvailability.AVAILABLE, _QUERY, right_ranking.resolved,
    )
    context = service._SemanticSearchContext(
        state_directory=Path("/synthetic"),
        query=_QUERY,
        limit=5,
        semantic_candidate_limit=64,
        lexical_candidate_limit=64,
        max_vectors=64,
        database=Path("/synthetic/semantic.sqlite3"),
        database_exists=False,
        cache=Path("/synthetic/models"),
    )

    rankings = service._lexical_search_rankings(
        context,
        include_lexical=True,
        lexical_paths=LexicalStatePaths(),
        lexical_search=lambda *_args, **_kwargs: (wrong, right),
        cancellation_check=None,
    )
    assert rankings[0].hits == ()
    assert rankings[1].hits and rankings[1].hits[0].snippet == _RIGHT_TEXT


def test_c29_numeric_order_id_is_exact_without_matching_longer_content_number() -> None:
    query = "ORDEN 23456 relevadores"
    title = "ORDEN 23456_relevadores"
    body_number = "ORDEN No. 10023456 relevadores"
    assert structured_query_support(query, title)["coherent"] is True
    assert structured_query_support(query, "Orden 23456.")["coherent"] is True
    absent = structured_query_support(query, body_number)
    assert absent["coherent"] is False
    assert absent["hard_mismatch"] is True
    missing = absent["missing_constraints"]
    assert isinstance(missing, list)
    assert "numeric_identifier" in missing


def test_c29_numeric_amount_is_not_an_identifier_or_decimal_prefix() -> None:
    amount_query = "factura CFDI emitida el 13 de agosto de 2026 total 123456 MXN"
    amount_text = "Factura del 13 de agosto de 2026. Total facturado 123456.50 MXN por relevadores."
    support = structured_query_support(amount_query, amount_text)
    assert support["hard_mismatch"] is False
    assert support["coherent"] is True
    constraints = support["constraints"]
    assert isinstance(constraints, list)
    assert not any(
        isinstance(value, Mapping) and value.get("kind") == "numeric_identifier"
        for value in constraints
    )
    order_support = structured_query_support(
        "ORDEN 23456 relevadores", "Total facturado 23456.50 MXN por relevadores."
    )
    assert order_support["hard_mismatch"] is True
    missing = order_support["missing_constraints"]
    assert isinstance(missing, list)
    assert "numeric_identifier" in missing


def test_c29_code_date_alias_coherence_promotes_witness_before_fusion() -> None:
    query = "presión registrada unidad 2 fase A 6 agosto 2027"
    wrong = "Normativa general de seguridad y presión de equipos."
    right = "Unit 2 - Phase A. Inspection Date August 6, 2027. Pressure 0.04 MPa."
    ranking = _ranking_with_texts(wrong, right)
    scoped = service.apply_structured_query_constraints(ranking, query=query, limit=1)
    assert scoped.hits and scoped.hits[0].ref_id == 2


def test_c29_code_date_fallback_remains_when_no_witness_exists() -> None:
    query = "presión registrada unidad 2 fase A 6 agosto 2027"
    ranking = _ranking_with_texts("Normativa general de seguridad y presión de equipos.")
    scoped = service.apply_structured_query_constraints(ranking, query=query, limit=1)
    assert scoped.hits == ranking.hits
    details = scoped.provenance["structured_query_constraints"]
    assert isinstance(details, Mapping)
    assert details["fallback_no_coherent_witness"] is True


def test_c29_wrong_unit_or_date_never_becomes_coherent() -> None:
    query = "presión registrada unidad 2 fase A 6 agosto 2027"
    wrong_unit = "Unit 6 - Phase A. Inspection Date August 6, 2027. Pressure 0.04 MPa."
    wrong_date = "Unit 2 - Phase A. Inspection Date July 6, 2027. Pressure 0.04 MPa."
    assert structured_query_support(query, wrong_unit)["coherent"] is False
    assert structured_query_support(query, wrong_date)["coherent"] is False


def test_c29_cross_channel_filter_drops_lexical_fallback_when_semantic_witness_exists() -> None:
    query = "presión registrada unidad 2 fase A 6 agosto 2027"
    right = _ranking_with_texts(
        "Unit 2 - Phase A. Inspection Date August 6, 2027. Pressure 0.04 MPa."
    )
    wrong = _ranking_with_texts("Normativa general de seguridad y presión de equipos.")
    lexical = LexicalRanking(
        "docx", None, LexicalAvailability.AVAILABLE, query, wrong.resolved,
    )
    scoped, lexical_scoped = service._filter_structured_fusion_scope(
        (right,), (lexical,), query=query,
    )
    assert scoped[0].hits
    assert lexical_scoped[0].hits == ()

    fallback_semantic, fallback_lexical = service._filter_structured_fusion_scope(
        (wrong,), (lexical,), query=query,
    )
    assert fallback_semantic[0].hits
    assert fallback_lexical[0].hits


def test_c29_auto_title_ranking_stays_public_but_out_of_witness_scope() -> None:
    query = "PLANT-ABC-05-001 presión 12 de marzo de 2027"
    content = _ranking_with_texts(_RIGHT_TEXT)
    title = replace(
        _ranking_with_texts(_WRONG_TEXT),
        name="semantic_title",
        provenance={"auto_requested_title_channel": True, "advisory_only": True},
    )
    rankings, _lexical = service._filter_structured_fusion_scope(
        (content, title), (), query=query,
    )
    assert len(rankings) == 2
    assert rankings[0].hits
    assert rankings[1].hits == title.hits
