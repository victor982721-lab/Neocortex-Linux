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
COOLING_PRESSURE_ARRIVAL_QUERY_POLICY = "semantic-cooling-pressure-arrival-query-v1"
COOLING_PRESSURE_ARRIVAL_INTERPRETATION = (
    "retrieval_aliases_not_equipment_equivalence_or_arrival_or_pressure_state_or_cause"
)

_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_COOLING_ES = re.compile(
    r"\b(?:(?:equipos?|sistemas?)\s+de\s+enfriamiento|enfriamiento|"
    r"radiad(?:or|ores)|enfriad(?:or|ores))\b"
)
_COOLING_EN = re.compile(r"\b(?:cooling(?:\s+(?:equipment|systems?|units?))?|radiators?|coolers?)\b")
_COOLING_ISSUE_ES = re.compile(
    r"\b(?:problema[s]?|incidente[s]?|anomalia[s]?|dan(?:o|os)?|golpe[s]?|"
    r"fuga[s]?|hallazgo[s]?|condicion(?:es)?\s+anormal(?:es)?)\b"
)
_COOLING_ISSUE_EN = re.compile(
    r"\b(?:problem[s]?|issue[s]?|incident[s]?|anomal(?:y|ies)|damage|"
    r"leak[s]?|finding[s]?|abnormal\s+condition[s]?)\b"
)
_ABSENCE_ES = re.compile(r"\b(?:despresurizad[oa]s?|sin\s+presion|ausencia\s+de\s+presion)\b")
_ABSENCE_EN = re.compile(r"\b(?:(?:un|de)pressuri[sz]ed|without\s+pressure|no\s+pressure)\b")
_ARRIVAL_ES = re.compile(
    r"\b(?:lleg(?:o|aron|an|ar|ado|ada|ados|adas)|"
    r"recib(?:io|ieron|en|ir|ido|ida|idos|idas)|"
    r"entreg(?:o|aron|an|ar|ado|ada|ados|adas))\b"
)
_ARRIVAL_EN = re.compile(
    r"\b(?:arriv(?:e|ed|es|ing|al)|receiv(?:e|ed|es|ing)|deliver(?:y|ed|s|ing))\b"
)
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


def _has_arrival_context(query: str, language: str) -> bool:
    """Recognize an explicit arrival/receipt relation without resolving its fact."""

    folded = _fold(query)
    return bool((_ARRIVAL_ES if language == "es" else _ARRIVAL_EN).search(folded))


def _cooling_issue_language(query: str) -> str | None:
    """Recognize a generic cooling-equipment issue without inventing its cause."""

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
    issue_es = _COOLING_ISSUE_ES.search(folded)
    issue_en = _COOLING_ISSUE_EN.search(folded)
    if not (cooling_es or cooling_en) or not (issue_es or issue_en):
        return None
    if any(term.group() in _NEGATIONS for term in terms) or _CONTRACTED_NEGATION.search(folded):
        return None
    return "es" if cooling_es or issue_es else "en"


def _cooling_issue_expansions(query: str, language: str) -> tuple[dict[str, object], ...]:
    prefix, aliases = (
        ("reporte sobre", "radiadores o enfriadores")
        if language == "es"
        else ("report about", "radiators or coolers")
    )
    report_query = f"{prefix} {query}"
    return tuple({
        "variant_id": variant_id,
        "profile": "semantic-cooling-issue-query-v1",
        "policy_signature": "semantic-cooling-issue-query-v1",
        "language": language,
        "effective_query": effective_query,
        "original_query": query,
        "preserved_constraints": "original_verbatim",
        "interpretation": "retrieval_aliases_not_equipment_equivalence_or_pressure_state_or_cause",
        "alias_concepts": alias_concepts,
    } for variant_id, effective_query, alias_concepts in (
        ("report_context", report_query, ["documentary_report_context"]),
        ("cooling_component_aliases", f"{report_query} ({aliases})",
         ["documentary_report_context", "cooling_components"]),
    ))


def text_query_expansions(query: str) -> tuple[dict[str, object], ...]:
    """Return at most two additional queries; the original belongs to the caller."""
    language = _expansion_language(query)
    if language is None:
        issue_language = _cooling_issue_language(query)
        if issue_language is not None:
            return _cooling_issue_expansions(query, issue_language)
        return _thermal_query_expansions(query)
    prefix, aliases = (
        ("reporte sobre", "radiadores o enfriadores sin presión")
        if language == "es"
        else ("report about", "radiators or coolers without pressure")
    )
    report_query = f"{prefix} {query}"
    if _has_arrival_context(query, language):
        arrival_aliases = (
            "radiadores o enfriadores sin presión; incidente documentado"
            if language == "es"
            else "radiators or coolers without pressure; documented incident"
        )
        return (
            {
                "variant_id": "report_context",
                "profile": QUERY_EXPANSION_POLICY,
                "policy_signature": QUERY_EXPANSION_POLICY,
                "language": language,
                "effective_query": report_query,
                "original_query": query,
                "preserved_constraints": "original_verbatim",
                "interpretation": QUERY_EXPANSION_INTERPRETATION,
                "alias_concepts": ["documentary_report_context"],
            },
            {
                "variant_id": "cooling_pressure_arrival_aliases",
                "profile": COOLING_PRESSURE_ARRIVAL_QUERY_POLICY,
                "policy_signature": COOLING_PRESSURE_ARRIVAL_QUERY_POLICY,
                "language": language,
                "effective_query": f"{report_query} ({arrival_aliases})",
                "original_query": query,
                "preserved_constraints": "original_verbatim",
                "interpretation": COOLING_PRESSURE_ARRIVAL_INTERPRETATION,
                "alias_concepts": [
                    "documentary_report_context",
                    "cooling_components",
                    "pressure_absence",
                    "arrival_condition",
                ],
            },
        )
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


THERMAL_QUERY_EXPANSION_POLICY = "semantic-thermal-comparison-query-v1"
_THERMAL_ES = re.compile(
    r"\b(?:calent(?:aba[n]?|amiento|ando|ar|o|aron)|calient(?:e[s]?|a[n]?)|temperaturas?)\b"
)
_THERMAL_EN = re.compile(r"\b(?:hotter|hottest|hot|heating|heated|heats?|temperatures?)\b")
_THERMAL_COMPARISON = re.compile(
    r"\b(?:mas|mayor(?:es)?|superior(?:es)?|hotter|hottest|higher|highest)\b"
)
_THERMAL_LOWER_OR_EQUAL = re.compile(
    r"\b(?:menos|menor(?:es)?|inferior(?:es)?|colder|lower|less|igual(?:es)?|"
    r"misma[s]?|mismo[s]?|same|equal)\b|\bcooler\s+than\b|\bmas\s+fri[oa]s?\b"
)
_THERMAL_CORRECTION_ES = re.compile(
    r"\b(?:corrig(?:io|ieron)|corregir|correccion(?:es)?|correctiv[oa]s?|"
    r"ajust(?:e[s]?|o|aron)|solucion(?:o|aron)|resolvio|repar(?:o|aron|acion))\b"
)
_THERMAL_CORRECTION_EN = re.compile(
    r"\b(?:fix(?:ed)?|corrected|corrections?|corrective|adjusted|adjustments?|"
    r"remed(?:y|ied)|repaired|resolved)\b"
)


def _thermal_query_expansions(query: str) -> tuple[dict[str, object], ...]:
    """Normalize a positive comparative thermal question, not its explanation."""
    if not query.strip() or len(query) > MAX_EXPANSION_QUERY_CHARS:
        return ()
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in query):
        return ()
    folded = _fold(query)
    terms = tuple(_TERM.findall(folded))
    if len(terms) > MAX_EXPANSION_QUERY_TERMS:
        return ()
    thermal_es = _THERMAL_ES.search(folded)
    thermal_en = _THERMAL_EN.search(folded)
    if not (thermal_es or thermal_en) or not _THERMAL_COMPARISON.search(folded):
        return ()
    if (
        set(terms).intersection(_NEGATIONS)
        or _CONTRACTED_NEGATION.search(folded)
        or _THERMAL_LOWER_OR_EQUAL.search(folded)
    ):
        return ()
    language = "es" if thermal_es else "en"
    correction = bool(_THERMAL_CORRECTION_ES.search(folded) or _THERMAL_CORRECTION_EN.search(folded))
    connection = bool(re.search(r"\b(?:conexi(?:on|ones)|connections?)\b", folded))
    if language == "es":
        heating = "calentamiento de conexión" if connection else "calentamiento"
        comparison = "comparación de temperatura" + (" y acciones de corrección" if correction else "")
        report = "reporte sobre"
        gloss = heating + " y diferencia térmica" + (", corrección y ajuste" if correction else "")
    else:
        heating = "connection heating" if connection else "heating"
        comparison = "temperature comparison" + (" and corrective actions" if correction else "")
        report = "report about"
        gloss = heating + " and thermal difference" + (", correction and adjustment" if correction else "")
    concepts = ["thermal_condition", "thermal_comparison"]
    if correction:
        concepts.append("requested_corrective_action")
    return tuple({
        "variant_id": variant_id,
        "profile": THERMAL_QUERY_EXPANSION_POLICY,
        "policy_signature": THERMAL_QUERY_EXPANSION_POLICY,
        "language": language,
        "effective_query": effective_query,
        "original_query": query,
        "preserved_constraints": "original_verbatim",
        "interpretation": "retrieval_aliases_not_temperature_measurement_or_cause_or_corrective_efficacy",
        "alias_concepts": list(concepts),
    } for variant_id, effective_query in (
        ("thermal_correction", f"{comparison}: {query} ({heating})"),
        ("thermal_report", f"{report} {query} ({gloss})"),
    ))
