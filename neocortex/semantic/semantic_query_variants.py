"""Bounded retrieval aliases for an explicit cooling/pressure-absence query.

The original query is preserved verbatim in every additional query.  These
aliases neither equate equipment nor infer arrival, pressure loss or cause;
the caller retains responsibility for evidence and the original search.
"""

from __future__ import annotations

import re
import unicodedata


MAX_EXPANSION_QUERY_CHARS = 512
MAX_EXPANSION_QUERY_TERMS = 64
MAX_TEXT_QUERY_EXPANSIONS = 2
QUERY_EXPANSION_POLICY = "semantic-cooling-pressure-query-v1"
QUERY_EXPANSION_INTERPRETATION = (
    "retrieval_aliases_not_equipment_equivalence_or_arrival_or_cause"
)

_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_COOLING_ES = re.compile(
    r"\b(?:(?:equipos?|sistemas?)\s+de\s+enfriamiento|radiad(?:or|ores)|enfriad(?:or|ores))\b"
)
_COOLING_EN = re.compile(r"\b(?:cooling\s+(?:equipment|systems?|units?)|radiators?|coolers?)\b")
_ABSENCE_ES = re.compile(r"\b(?:despresurizad[oa]s?|sin\s+presion|ausencia\s+de\s+presion)\b")
_ABSENCE_EN = re.compile(r"\b(?:(?:un|de)pressuri[sz]ed|without\s+pressure|no\s+pressure)\b")
_NEGATIONS = frozenset({
    "no", "sin", "nunca", "jamas", "ni", "tampoco", "nadie", "ningun", "ninguno",
    "ninguna", "ningunos", "ningunas", "ausencia", "ausente", "ausentes",
    "not", "without", "never", "neither", "nor", "none", "nobody", "nothing",
    "absence", "absent", "lack", "lacking", "except", "excluding", "excepto", "salvo",
    "nicht", "ohne", "kein", "keine", "keinen", "keinem", "keiner", "keines",
})
_CONTRACTED_NEGATION = re.compile(r"\b[^\W_]+n['\u2019]t\b", re.UNICODE)


def _fold(text: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def _expansion_language(query: str) -> str | None:
    if not isinstance(query, str):
        raise ValueError("text query expansion requires a string query")
    if not query.strip() or len(query) > MAX_EXPANSION_QUERY_CHARS:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in query):
        return None
    folded = _fold(query)
    terms = tuple(_TERM.finditer(folded))
    if len(terms) > MAX_EXPANSION_QUERY_TERMS:
        return None
    cooling_es = _COOLING_ES.search(folded)
    cooling_en = _COOLING_EN.search(folded)
    absence_es = tuple(_ABSENCE_ES.finditer(folded))
    absence_en = tuple(_ABSENCE_EN.finditer(folded))
    absence_spans = tuple(match.span() for match in (*absence_es, *absence_en))
    if not (cooling_es or cooling_en) or not absence_spans:
        return None

    def within_condition(start: int, end: int) -> bool:
        return any(left <= start and end <= right for left, right in absence_spans)

    if any(term.group() in _NEGATIONS and not within_condition(*term.span()) for term in terms):
        return None
    if any(not within_condition(*match.span()) for match in _CONTRACTED_NEGATION.finditer(folded)):
        return None
    # Prefer an explicit Spanish condition/subject in mixed queries; otherwise
    # use the English templates.  No locale/model detection changes the query.
    return "es" if absence_es or cooling_es else "en"


def cooling_pressure_concepts(query: str) -> bool:
    """Share the same bounded, negation-preserving eligibility across readers."""
    return _expansion_language(query) is not None


def text_query_expansions(query: str) -> tuple[dict[str, object], ...]:
    """Return at most two additional queries; the original belongs to the caller."""
    language = _expansion_language(query)
    if language is None:
        return ()
    prefix, aliases = (
        ("reporte sobre", "radiadores o enfriadores sin presión")
        if language == "es"
        else ("report about", "radiators or coolers without pressure")
    )
    report_query = f"{prefix} {query}"
    additional = (
        ("report_context", report_query, ["documentary_report_context"]),
        ("cooling_pressure_aliases", f"{report_query} ({aliases})",
         ["documentary_report_context", "cooling_components", "pressure_absence"]),
    )
    return tuple({
        "variant_id": variant_id,
        "profile": QUERY_EXPANSION_POLICY,
        "policy_signature": QUERY_EXPANSION_POLICY,
        "language": language,
        "effective_query": effective_query,
        "original_query": query,
        "preserved_constraints": "original_verbatim",
        "interpretation": QUERY_EXPANSION_INTERPRETATION,
        "alias_concepts": alias_concepts,
    } for variant_id, effective_query, alias_concepts in additional)
