"""Thermal normalization stays faithful while preserving cooling profile v1."""

from __future__ import annotations

import json
import unicodedata

import pytest

from neocortex.semantic.semantic_query_variants import cooling_pressure_concepts, text_query_expansions


TEST_CAPABILITIES = ("base",)
pytestmark = pytest.mark.capability("base")


@pytest.mark.parametrize("query", (
    "¿Qué conexión se calentaba más que las otras y cómo se corrigió?",
    "¿Qué conexiones estaban más calientes que las otras y cómo se ajustaron?",
    "¿Qué conexión tenía más temperatura que las demás?",
    "¿Qué motor estaba más caliente que los otros?",
    "Which connection was hotter than the others and how was it corrected?",
    "Which component had a higher temperature than the others?",
))
def test_positive_thermal_comparison_has_at_most_two_faithful_additional_queries(query: str) -> None:
    variants = text_query_expansions(query)
    assert len(variants) == 2 and variants == text_query_expansions(query)
    assert cooling_pressure_concepts(query) is False
    assert [value["variant_id"] for value in variants] == ["thermal_correction", "thermal_report"]
    for value in variants:
        assert value["original_query"] == query and query in value["effective_query"]
        assert value["profile"] == "semantic-thermal-comparison-query-v1"
        assert value["preserved_constraints"] == "original_verbatim"
        assert value["interpretation"] == "retrieval_aliases_not_temperature_measurement_or_cause_or_corrective_efficacy"
        assert "causó" not in value["effective_query"] and "because" not in value["effective_query"]


@pytest.mark.parametrize("query", (
    "La conexión no se calentaba más que las otras",
    "Ninguna conexión estaba más caliente que las otras",
    "La conexión estaba más caliente pero no se corrigió",
    "The connection wasn't hotter than the others",
    "The temperature was not higher than the others",
    "La conexión tenía menos temperatura que las otras",
    "La conexión estaba más fría que las otras",
    "The connection was colder than the others",
    "The temperature was lower than the others",
    "La conexión tenía la misma temperatura que las otras",
    "La conexión se calentaba y se corrigió",
    "La conexión estaba caliente",
    "¿Qué conexión estaba diferente de las otras y cómo se corrigió?",
    "¿Cómo se corrigió el problema?",
    "¿Qué temperatura tienen otras conexiones?",
    "What temperature do other connections have?",
))
def test_negated_inverted_or_single_cue_queries_do_not_gain_thermal_aliases(query: str) -> None:
    assert text_query_expansions(query) == ()


def test_correction_and_connection_are_not_invented_when_the_query_did_not_request_them() -> None:
    variants = text_query_expansions("¿Qué motor estaba más caliente que los otros?")
    assert len(variants) == 2
    for variant in variants:
        assert "conexión" not in variant["effective_query"]
        assert "corrección" not in variant["effective_query"]
        assert "ajuste" not in variant["effective_query"]
        assert "requested_corrective_action" not in variant["alias_concepts"]


def test_english_templates_stay_english_and_preserve_the_requested_correction() -> None:
    query = "Which connection was hotter than the others and how was it fixed?"
    first, second = text_query_expansions(query)
    assert first["language"] == second["language"] == "en"
    assert first["effective_query"] == f"temperature comparison and corrective actions: {query} (connection heating)"
    assert second["effective_query"] == f"report about {query} (connection heating and thermal difference, correction and adjustment)"


def test_spanish_connection_and_correction_glosses_match_the_declared_templates() -> None:
    query = "¿Qué conexión se calentaba más que las otras y cuál fue la corrección?"
    first, second = text_query_expansions(query)
    assert first["effective_query"] == f"comparación de temperatura y acciones de corrección: {query} (calentamiento de conexión)"
    assert second["effective_query"] == f"reporte sobre {query} (calentamiento de conexión y diferencia térmica, corrección y ajuste)"


@pytest.mark.parametrize("decomposed", (False, True))
def test_unicode_identity_dates_and_temporal_scope_are_preserved_verbatim(decomposed: bool) -> None:
    query = "¿Qué conexión Z-Ω8 estaba más caliente que las otras el 9 de abril de 2026 y cómo se corrigió?"
    if decomposed:
        query = unicodedata.normalize("NFD", query)
    variants = text_query_expansions(query)
    assert len(variants) == 2
    for value in variants:
        assert query in value["effective_query"]
        assert value["original_query"].encode("utf-8") == query.encode("utf-8")
        added = value["effective_query"].replace(query, "", 1)
        assert not any(character.isdigit() for character in added)
        assert "fase" not in added and "terminal" not in added and "torque" not in added


@pytest.mark.parametrize("query", (
    "¿Qué conexión estaba más caliente que las otras? " + "x" * 512,
    "dato " * 60 + "¿Qué conexión estaba más caliente que las otras?",
    "¿Qué conexión estaba más caliente que las otras?\n",
))
def test_existing_length_term_and_control_bounds_apply_to_thermal_dispatch(query: str) -> None:
    assert text_query_expansions(query) == ()


@pytest.mark.parametrize(("query", "language", "prefix", "aliases"), (
    ("radiadores sin presión", "es", "reporte sobre", "radiadores o enfriadores sin presión"),
    ("cooling equipment no pressure", "en", "report about", "radiators or coolers without pressure"),
    ("radiadores sin presión más calientes que los otros", "es", "reporte sobre", "radiadores o enfriadores sin presión"),
))
def test_cooling_profile_v1_keeps_exact_serialized_bytes_and_dispatch_precedence(
    query: str, language: str, prefix: str, aliases: str,
) -> None:
    expected = tuple({
        "variant_id": variant_id,
        "profile": "semantic-cooling-pressure-query-v1",
        "policy_signature": "semantic-cooling-pressure-query-v1",
        "language": language,
        "effective_query": effective,
        "original_query": query,
        "preserved_constraints": "original_verbatim",
        "interpretation": "retrieval_aliases_not_equipment_equivalence_or_arrival_or_cause",
        "alias_concepts": concepts,
    } for variant_id, effective, concepts in (
        ("report_context", f"{prefix} {query}", ["documentary_report_context"]),
        ("cooling_pressure_aliases", f"{prefix} {query} ({aliases})",
         ["documentary_report_context", "cooling_components", "pressure_absence"]),
    ))
    def dump(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert cooling_pressure_concepts(query) is True
    assert dump(text_query_expansions(query)) == dump(expected)
