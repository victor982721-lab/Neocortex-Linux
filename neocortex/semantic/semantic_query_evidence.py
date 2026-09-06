"""Bounded lexical evidence checks, never entailment or authorization decisions.

Only strong Spanish occurrence/record cues and three necessary-witness
families are recognized.  Unknown questions stay unassessed.  A missing
witness describes the assessed text, not the entire source or corpus.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator


MAX_EVIDENCE_CHECK_CHARS = 32_768
MAX_EVIDENCE_QUERY_CHARS = 4_096
MAX_ROLE_WITNESSES = 4
MAX_ROLE_EXCERPT_CHARS = 240
ROLE_POLICY_SIGNATURE = "query-role-counterevidence-v1"
CHECKS_POLICY_SIGNATURE = "query-necessary-evidence-checks-v1"

_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_BREAK = re.compile(r"[!?\r\n]+|(?<!\d)\.|\.(?!\d)")
_CLAUSE_BREAK = re.compile(r";|(?<!\d),|,(?!\d)|\b(?:y|pero|aunque|sin embargo)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:no|nadie|nunca|jamas|tampoco)\b")
_PAST_EVENT = re.compile(
    r"\b(?:que paso|ocurrio|ocurrieron|sucedio|cayo|murio|reanudaron|termino|"
    r"recupero|corto|comprobo|pararon|detuvo|quedo|tenia|sustituyeron|indico|"
    r"bajo|acordo|confirmo|autorizo|causo|recibieron)\b"
)
_INSTRUCTION = re.compile(r"\b(?:paso a paso|manual|guia|instrucciones|procedimiento)\b")
_NONRECORD = re.compile(
    r"\b(?:no (?:es )?(?:un )?registro|no describe un hecho ocurrido|"
    r"no demuestra su ejecucion|todavia no ejecutados|no acredita que se|"
    r"solo en el catalogo)\b"
)
_NONOCCURRENCE = re.compile(
    r"\bno (?:se (?:presento|produjo|registro)|ocurrio|hubo|aparecio)\b"
)
_EXCLUDED_SUBJECT = re.compile(r"\bno corresponde (?:a|al)\b")
_DETERMINERS = frozenset({"un", "una", "ningun", "ninguna", "el", "la", "los", "las", "ninguno", "algun", "alguna"})
_AUTHORIZATION = re.compile(r"\b(?:autorizo|autorizaron|fue autorizado|fue autorizada)\b")
_ACTIVE_AUTHORIZATION = re.compile(r"\b(?:autorizo|autorizaron)\b")
_ACTOR_ROLES = re.compile(
    r"\b(?:supervisor|supervisora|responsable|jefe|jefa|ingeniero|ingeniera|"
    r"coordinador|coordinadora|director|directora|gerente|fabricante|operador|"
    r"operadora|representante)\b"
)
_NOT_NAMED_ACTORS = frozenset({
    "despues", "antes", "entonces", "hoy", "ayer", "manana", "posteriormente",
    "finalmente", "alguien", "nadie", "el", "ella", "ellos", "ellas", "se",
    "cuando", "durante", "segun", "si", "en", "una", "un", "la", "los", "las",
})
_TORQUE = re.compile(r"\b(?:torque|apriete)\b")
_TORQUE_VALUE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:n\s*[·.*]?\s*m|nm)\b")
_CAUSAL = re.compile(r"\b(?:causo|provoco|origino|debido)\b")
_RECEIPT = re.compile(
    r"\b(?:acuse de (?:recepcion|recibido)|se recibio|fue recibido|"
    r"se recibieron|se entrego|fue entregado)\b"
)
_DATE = re.compile(r"\b\d{1,2} de [a-z]+\b")


def _fold(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _prepare(query: str, text: str) -> tuple[str, str]:
    if not isinstance(query, str) or not isinstance(text, str):
        raise ValueError("query evidence checks require string query and text")
    return _fold(query[:MAX_EVIDENCE_QUERY_CHARS]), text[:MAX_EVIDENCE_CHECK_CHARS]


def _split(text: str, boundary_pattern: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    start = 0
    for boundary in boundary_pattern.finditer(text):
        yield start, text[start:boundary.start()]
        start = boundary.end()
    yield start, text[start:]


def _sentences(text: str) -> Iterator[tuple[int, int, str]]:
    for offset, raw in _split(text, _SENTENCE_BREAK):
        trimmed = raw.strip()
        if trimmed:
            start = offset + len(raw) - len(raw.lstrip())
            yield start, start + len(trimmed), trimmed


def _clauses(text: str) -> Iterator[tuple[int, str]]:
    yield from _split(text, _CLAUSE_BREAK)


def _original_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Translate a folded match back to exact input-character coordinates."""
    offsets: list[int] = []
    for index, character in enumerate(text):
        offsets.extend([index] * len(_fold(character)))
        if len(offsets) >= end:
            break
    return offsets[start], offsets[end - 1] + 1


def _role_witness(
    text: str, start: int, end: int, focus: tuple[int, int], reasons: list[str],
    *, evaluation_truncated: bool, query_truncated: bool,
) -> dict[str, object]:
    focus_start, focus_end = focus
    width = min(MAX_ROLE_EXCERPT_CHARS, end - start)
    excerpt_start = max(start, focus_start - 32)
    excerpt_start = min(excerpt_start, end - width)
    if focus_end - excerpt_start > width:
        excerpt_start = max(start, focus_end - width)
    excerpt_end = excerpt_start + width
    result: dict[str, object] = {
        "policy_signature": ROLE_POLICY_SIGNATURE,
        "basis": "input_text",
        "interpretation": "literal_counterevidence_not_entailment_or_authority",
        "start_char": excerpt_start,
        "end_char": excerpt_end,
        "reasons": sorted(set(reasons)),
        "evaluation_truncated": evaluation_truncated,
        "query_truncated": query_truncated,
    }
    excerpt = text[excerpt_start:excerpt_end]
    if focus_end - focus_start > MAX_ROLE_EXCERPT_CHARS:
        result["start_char"], result["end_char"] = focus_start, focus_end
        result["text_omitted_reason"] = "counterevidence_span_exceeds_excerpt_limit"
    elif any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in excerpt):
        result["text_omitted_reason"] = "control_characters"
    else:
        result["text"] = excerpt
    return result


def query_role_counterevidence(query: str, text: str) -> list[dict[str, object]]:
    """Return literal counter-witnesses only for an observed-event request.

    A manual title, prior timing or unrelated negation is insufficient.  The
    result may inform evidence preference, never hide citations or grant an
    action; an empty result is not proof of a positive occurrence.
    """
    folded_query, bounded = _prepare(query, text)
    past = bool(_PAST_EVENT.search(folded_query))
    if (_INSTRUCTION.search(folded_query) and not past) or not (
        past or re.search(r"\b(?:incidente|accidente)\b", folded_query)
    ):
        return []
    terms = set(_TERM.findall(folded_query))
    identifiers = tuple(dict.fromkeys(re.findall(r"\b[a-z]+\d+\b", folded_query)))[:16]
    witnesses: list[dict[str, object]] = []
    for sentence_start, sentence_end, sentence in _sentences(bounded):
        folded = _fold(sentence)
        reasons: list[str] = []
        focuses: list[tuple[int, int]] = []
        if match := _NONRECORD.search(folded):
            reasons.append("source_explicitly_limits_observed_event_evidence")
            focuses.append(match.span())
        for clause_start, clause in _clauses(folded):
            if match := _NONOCCURRENCE.search(clause):
                head = next((term for term in _TERM.finditer(clause, match.end())
                             if term.group() not in _DETERMINERS), None)
                if head is not None and head.group() in terms:
                    reasons.append("literal_requested_event_occurrence_is_negated")
                    focuses.append((clause_start + match.start(), clause_start + head.end()))
            if match := _EXCLUDED_SUBJECT.search(clause):
                excluded = next((term for term in _TERM.finditer(clause, match.end())
                                 if term.group() in identifiers), None)
                if excluded is not None:
                    reasons.append("requested_named_subject_is_explicitly_excluded")
                    focuses.append((clause_start + match.start(), clause_start + excluded.end()))
        if focuses:
            focus_start, focus_end = _original_span(sentence, *min(focuses))
            witnesses.append(_role_witness(
                bounded, sentence_start, sentence_end,
                (sentence_start + focus_start, sentence_start + focus_end), reasons,
                evaluation_truncated=len(text) > len(bounded),
                query_truncated=len(query) > MAX_EVIDENCE_QUERY_CHARS,
            ))
            if len(witnesses) == MAX_ROLE_WITNESSES:
                break
    return witnesses


def _identified_actor(raw_clause: str, folded_clause: str) -> bool:
    if passive := re.search(r"\bautorizad[oa] por (.+)", folded_clause):
        return bool(_ACTOR_ROLES.search(passive[1]))
    active = _ACTIVE_AUTHORIZATION.search(folded_clause)
    if active is None:
        return False
    prefix = folded_clause[:active.start()].strip()
    if not prefix or re.search(r"\bse$", prefix):
        return False
    if _ACTOR_ROLES.search(prefix):
        return True
    original_end, _ = _original_span(raw_clause, active.start(), active.end())
    return any(
        len(term.group()) > 1 and term.group()[0].isupper()
        and _fold(term.group()) not in _NOT_NAMED_ACTORS
        for term in _TERM.finditer(raw_clause[:original_end])
    )


def requested_evidence_checks(query: str, text: str) -> dict[str, object]:
    """Check necessary actors, causal witnesses or receipts without claiming truth.

    Missing checks preserve evidence as related material.  Passing checks do
    not establish an answer, entailment, permission, approval or authority.
    """
    folded_query, bounded = _prepare(query, text)
    clauses = [(raw, _fold(raw)) for _, _, sentence in _sentences(bounded)
               for _, raw in _clauses(sentence) if raw.strip()]
    positive = [(raw, folded) for raw, folded in clauses if not _NEGATION.search(folded)]
    required: list[str] = []
    missing: list[str] = []
    counterevidence: list[str] = []
    if re.search(r"\bquien\b", folded_query) and re.search(r"\bautoriz\w*\b", folded_query):
        required.extend(("authorization_event", "identified_authorizing_actor"))
        authorization = [(raw, folded) for raw, folded in positive if _AUTHORIZATION.search(folded)]
        if not authorization:
            missing.append("authorization_event")
        if not any(_identified_actor(raw, folded) for raw, folded in authorization):
            missing.append("identified_authorizing_actor")
    if re.search(r"\btorque\b", folded_query) and re.search(r"\b(?:causo|causa|provoco)\b", folded_query):
        required.extend(("applied_torque_value_with_unit", "asserted_torque_to_damage_causal_link"))
        torque = [folded for _, folded in positive if _TORQUE.search(folded)]
        if not any(_TORQUE_VALUE.search(clause) for clause in torque):
            missing.append("applied_torque_value_with_unit")
        if not any(_CAUSAL.search(clause) for clause in torque):
            missing.append("asserted_torque_to_damage_causal_link")
        if any(re.search(r"\bno durante el apriete\b", folded) for _, folded in clauses):
            counterevidence.append("source_explicitly_places_incident_outside_tightening")
    if re.search(r"\bacuse\b", folded_query) or (
        re.search(r"\bdemuestra\b", folded_query) and re.search(r"\brecib\w*\b", folded_query)
    ):
        required.append("affirmative_receipt_or_delivery_acknowledgment")
        receipts = [folded for _, folded in positive if _RECEIPT.search(folded)
                    and not re.search(r"\b(?:pendiente|todavia)\b", folded)]
        if not receipts:
            missing.append("affirmative_receipt_or_delivery_acknowledgment")
        dates = _DATE.findall(folded_query)
        if dates:
            required.append("receipt_linked_to_requested_date")
            if not any(date in receipt for date in dates for receipt in receipts):
                missing.append("receipt_linked_to_requested_date")
    return {
        "policy_signature": CHECKS_POLICY_SIGNATURE,
        "basis": "input_text",
        "interpretation": "necessary_conditions_only_not_answer_entailment_or_authority",
        "status": "missing" if missing else "necessary_checks_not_failed" if required else "not_assessed",
        "required_witnesses": required,
        "missing_necessary_witnesses": missing,
        "counterevidence": counterevidence,
        "retrieval_disposition": "related_evidence_only" if missing else "unchanged",
        "evaluated_chars": len(bounded),
        "evaluation_truncated": len(text) > len(bounded),
        "query_truncated": len(query) > MAX_EVIDENCE_QUERY_CHARS,
    }
