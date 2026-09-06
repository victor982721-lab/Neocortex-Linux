"""Pure query aliases retain the original constraint-bearing query verbatim."""

from __future__ import annotations

import json
import unicodedata

import pytest

from neocortex.semantic.semantic_query_variants import (
    MAX_EXPANSION_QUERY_CHARS,
    MAX_EXPANSION_QUERY_TERMS,
    MAX_TEXT_QUERY_EXPANSIONS,
    cooling_pressure_concepts,
    text_query_expansions,
)


TEST_CAPABILITIES = ("base",)
pytestmark = pytest.mark.capability("base")


@pytest.mark.parametrize("query", (
    "equipos de enfriamiento sin presión",
    "sistema de enfriamiento despresurizado",
    "sistemas de enfriamiento despresurizados",
    "radiador con ausencia de presión",
    "radiadores despresurizados",
    "enfriador despresurizado",
    "enfriadores despresurizados",
    "enfriadores con conexiones despresurizadas",
))
def test_spanish_cooling_pressure_conjunction_has_two_bounded_deterministic_additions(query: str) -> None:
    variants = text_query_expansions(query)
    assert cooling_pressure_concepts(query) is True
    assert len(variants) == MAX_TEXT_QUERY_EXPANSIONS == 2
    assert variants == text_query_expansions(query)
    assert [variant["variant_id"] for variant in variants] == ["report_context", "cooling_pressure_aliases"]
    assert variants[0]["effective_query"] == f"reporte sobre {query}"
    assert variants[1]["effective_query"] == f"reporte sobre {query} (radiadores o enfriadores sin presión)"
    assert all(variant["language"] == "es" for variant in variants)


@pytest.mark.parametrize("query", (
    "cooling equipment no pressure",
    "cooling system without pressure",
    "cooling units unpressurized",
    "radiator depressurized",
    "radiators depressurised",
    "cooler unpressurised",
    "coolers unpressurized",
))
def test_english_queries_use_stable_english_templates_including_british_spelling(query: str) -> None:
    variants = text_query_expansions(query)
    assert cooling_pressure_concepts(query) is True
    assert len(variants) == 2
    assert variants[0]["effective_query"] == f"report about {query}"
    assert variants[1]["effective_query"] == f"report about {query} (radiators or coolers without pressure)"
    assert all(variant["language"] == "en" for variant in variants)


@pytest.mark.parametrize("query", (
    "radiadores no llegaron despresurizados",
    "radiadores nunca llegaron sin presión",
    "radiadores sin presión ni aceite",
    "radiadores sin presión sin daños",
    "ningún radiador despresurizado",
    "radiadores no despresurizados",
    "radiadores sin ausencia de presión",
    "radiators not delivered without pressure",
    "cooling equipment no pressure never arrived",
    "radiators without pressure and without oil",
    "radiators didn't arrive unpressurized",
    "radiators didn\u2019t arrive unpressurised",
    "radiators won't arrive without pressure",
    "radiadores sin presión excepto R9",
))
def test_every_extra_explicit_negation_or_exclusion_outside_condition_blocks_aliases(query: str) -> None:
    assert cooling_pressure_concepts(query) is False
    assert text_query_expansions(query) == ()


@pytest.mark.parametrize("query", (
    "radiadores sin presión",
    "cooling equipment no pressure",
    "radiators without pressure",
    "radiadores con ausencia de presión",
    "radiators no pressure and without pressure",
))
def test_negation_within_the_recognized_absence_condition_is_retained_not_rejected(query: str) -> None:
    variants = text_query_expansions(query)
    assert len(variants) == 2
    assert all(query in variant["effective_query"] for variant in variants)


@pytest.mark.parametrize("query", (
    "RAM agotada y presión de memoria",
    "presión ausente en el registro del kernel",
    "What planet has microbial life?",
    "planetas sin presión atmosférica",
    "radiadores con presión",
    "cooling equipment with pressure",
    "radiadores con pérdida de presión",
    "procedimiento de despresurización de radiadores",
    "sin presión",
    "radiadores",
    "",
    "   ",
))
def test_unrelated_or_positive_pressure_queries_do_not_expand(query: str) -> None:
    assert cooling_pressure_concepts(query) is False
    assert text_query_expansions(query) == ()


@pytest.mark.parametrize("decomposed", (False, True))
def test_original_unicode_identifiers_dates_temporal_and_negative_constraints_remain_verbatim(decomposed: bool) -> None:
    query = "  Radiadores R-Ω7 / NS \uff11\uff10\uff11 llegaron sin presión después del 20 de junio de 2026  "
    if decomposed:
        query = unicodedata.normalize("NFD", query)
    original_bytes = query.encode("utf-8")
    variants = text_query_expansions(query)
    assert len(variants) == 2
    for variant in variants:
        assert variant["original_query"] == query
        assert variant["original_query"].encode("utf-8") == original_bytes
        assert query in variant["effective_query"]
        assert variant["preserved_constraints"] == "original_verbatim"
        assert variant["profile"] == variant["policy_signature"] == "semantic-cooling-pressure-query-v1"
        assert variant["interpretation"] == "retrieval_aliases_not_equipment_equivalence_or_arrival_or_cause"
        assert "probability" not in variant and "score" not in variant
        json.dumps(variant, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert query.encode("utf-8") == original_bytes


def test_more_than_512_input_characters_skips_without_truncating_the_query() -> None:
    query = "radiadores sin presión " + "x" * MAX_EXPANSION_QUERY_CHARS
    original = query
    assert text_query_expansions(query) == () and cooling_pressure_concepts(query) is False
    assert query == original


def test_word_budget_is_shared_by_boolean_and_expansion_helpers() -> None:
    query_at_limit = "dato " * (MAX_EXPANSION_QUERY_TERMS - 3) + "radiadores sin presión"
    query_over_limit = "dato " + query_at_limit
    assert len(query_over_limit) < MAX_EXPANSION_QUERY_CHARS
    assert cooling_pressure_concepts(query_at_limit) is True
    assert len(text_query_expansions(query_at_limit)) == 2
    assert cooling_pressure_concepts(query_over_limit) is False
    assert text_query_expansions(query_over_limit) == ()


@pytest.mark.parametrize("query", ("radiadores without pressure", "cooling equipment sin presión"))
def test_mixed_language_choice_is_explicit_and_deterministic(query: str) -> None:
    assert {variant["language"] for variant in text_query_expansions(query)} == {"es"}
    assert text_query_expansions(query) == text_query_expansions(query)


def test_returned_mutable_metadata_is_not_shared_between_calls_or_variants() -> None:
    first = text_query_expansions("radiadores sin presión")
    first[0]["alias_concepts"].append("not-an-actual-concept")
    first[0]["effective_query"] = "changed by caller"
    second = text_query_expansions("radiadores sin presión")
    assert "not-an-actual-concept" not in first[1]["alias_concepts"]
    assert second[0]["alias_concepts"] == ["documentary_report_context"]
    assert second[0]["effective_query"] == "reporte sobre radiadores sin presión"


@pytest.mark.parametrize("control", ("\n", "\t", "\x00", "\u202e"))
def test_controls_skip_instead_of_sanitizing_and_changing_original_constraints(control: str) -> None:
    query = f"radiadores sin presión{control}R9"
    assert cooling_pressure_concepts(query) is False
    assert text_query_expansions(query) == ()


@pytest.mark.parametrize("function", (cooling_pressure_concepts, text_query_expansions))
def test_nonstring_queries_fail_explicitly(function) -> None:
    with pytest.raises(ValueError, match="string query"):
        function(None)
