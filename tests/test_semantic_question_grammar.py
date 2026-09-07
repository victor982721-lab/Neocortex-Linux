"""Question grammar removal preserves comparison, negation and exact queries."""

from neocortex.semantic.semantic_lexical import _compile_natural_fts_query_plan, _query_support_terms


def test_interrogative_auxiliaries_are_not_required_but_comparison_still_is():
    query = "¿Qué conexión se calentaba mucho más que las otras y cómo se corrigió?"
    plan = _compile_natural_fts_query_plan(query)
    assert plan.original_query == query
    assert '"se"' in plan.normalized_query and '"cómo"' in plan.normalized_query
    assert plan.primary_strategy == "question_content_terms_all"
    assert plan.primary_query == '"conexión" AND "calentaba" AND "mucho" AND "más" AND "otras" AND "corrigió"'
    assert not plan.fallbacks  # Six remaining relations do not become any-two.
    assert set(_query_support_terms(query)) == {"conexion", "calentaba", "mucho", "mas", "otras", "corrigio"}


def test_negation_time_and_asset_survive_grammar_removal():
    plan = _compile_natural_fts_query_plan("¿Cómo se corrigió M9 sin cambiar los rodamientos antes de marzo?")
    assert all(f'"{term}"' in plan.primary_query for term in ("M9", "sin", "cambiar", "rodamientos", "antes", "marzo"))
    assert not plan.fallbacks


def test_unprefixed_literal_se_is_not_silently_removed():
    plan = _compile_natural_fts_query_plan("SE terminal")
    assert plan.primary_query == '"SE" AND "terminal"'
    assert plan.primary_strategy == "strict_all_terms"
    assert "se" in _query_support_terms("SE terminal")


def test_polite_question_prefixes_are_grammar_not_required_content():
    for query in (
        "¿Me puedes mostrar el reporte de presión interna?",
        "Could you find information about internal pressure?",
        "Bitte zeigen Sie den Bericht über den Transformator",
    ):
        plan = _compile_natural_fts_query_plan(query)
        assert plan.primary_strategy == "question_content_terms_all"
        assert all(
            token.casefold() not in plan.primary_query.casefold()
            for token in ("me", "puedes", "could", "you", "find", "information", "bitte", "sie")
        )
        assert any(
            token in plan.primary_query.casefold()
            for token in ("reporte", "presión", "internal", "pressure", "bericht", "transformator")
        )
