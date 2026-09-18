"""Bounded lexical evidence checks, never entailment or authorization decisions.

Only explicit occurrence/record cues and necessary-witness relationships
are recognized.  Unknown questions stay unassessed.  A missing
witness describes the assessed text, not the entire source or corpus.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from typing import TypedDict


MAX_EVIDENCE_CHECK_CHARS = 32_768
MAX_EVIDENCE_QUERY_CHARS = 4_096
MAX_ROLE_WITNESSES = 4
MAX_ROLE_EXCERPT_CHARS = 240
ROLE_POLICY_SIGNATURE = "query-role-counterevidence-v1"
CHECKS_POLICY_SIGNATURE = "query-necessary-evidence-checks-v2"

_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_BREAK = re.compile(r"[!?\r\n]+|(?<!\d)\.|\.(?!\d)")
_CLAUSE_BREAK = re.compile(r";|(?<!\d),|,(?!\d)|\b(?:y|pero|aunque|sin embargo)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(?:no|nadie|nunca|jamas|tampoco)\b")
_PAST_EVENT = re.compile(
    r"\b(?:que paso|ocurrio|ocurrieron|sucedio|cayo|murio|reanudaron|termino|"
    r"recupero|corto|comprobo|pararon|detuvo|quedo|tenia|sustituyeron|indico|"
    r"bajo|acordo|confirmo|autorizo|causo|recibieron|hubo|existio)\b"
)
_INSTRUCTION = re.compile(r"\b(?:paso a paso|manual|guia|instrucciones|procedimiento)\b")
_NONRECORD = re.compile(
    r"\b(?:no (?:es )?(?:un )?registro|no describe un hecho ocurrido|"
    r"no demuestra su ejecucion|todavia no ejecutados|no acredita que se|"
    r"solo en el catalogo)\b"
)
_NONOCCURRENCE = re.compile(r"\bno (?:se (?:presento|produjo|registro)|ocurrio|hubo|aparecio)\b")
_HYPOTHETICAL_WARNING = re.compile(
    r"\b(?:puede[n]?|podria[n]?|es posible que|can|could|may|might)\s+"
    r"(?:ocurrir|suceder|presentarse|aparecer|occur|happen|arise)\b|"
    r"\b(?:en caso de|in case of)\b"
)
_OBSERVED_EVENT = re.compile(
    r"\b(?:hubo|existio|ocurrio|ocurrieron|sucedio|se produjo|se presento|"
    r"aparecio|estallo|occurred|happened|there was|was observed|was reported)\b"
)
_EVENT_QUERY_GRAMMAR = frozenset(
    {
        "a",
        "al",
        "an",
        "de",
        "del",
        "durante",
        "el",
        "en",
        "la",
        "las",
        "los",
        "que",
        "the",
        "una",
        "un",
        "when",
        "what",
        "where",
        "with",
        "hubo",
        "existio",
        "ocurrio",
        "ocurrieron",
        "sucedio",
        "paso",
    }
)
_EXCLUDED_SUBJECT = re.compile(r"\bno corresponde (?:a|al)\b")
_DETERMINERS = frozenset(
    {"un", "una", "ningun", "ninguna", "el", "la", "los", "las", "ninguno", "algun", "alguna"}
)
_AUTHORIZATION = re.compile(r"\b(?:autorizo|autorizaron|fue autorizado|fue autorizada)\b")
_ACTIVE_AUTHORIZATION = re.compile(r"\b(?:autorizo|autorizaron)\b")
_ACTOR_ROLES = re.compile(
    r"\b(?:supervisor|supervisora|responsable|jefe|jefa|ingeniero|ingeniera|"
    r"coordinador|coordinadora|director|directora|gerente|fabricante|operador|"
    r"operadora|representante)\b"
)
_NOT_NAMED_ACTORS = frozenset(
    {
        "despues",
        "antes",
        "entonces",
        "hoy",
        "ayer",
        "manana",
        "posteriormente",
        "finalmente",
        "alguien",
        "nadie",
        "el",
        "ella",
        "ellos",
        "ellas",
        "se",
        "cuando",
        "durante",
        "segun",
        "si",
        "en",
        "una",
        "un",
        "la",
        "los",
        "las",
    }
)
_TORQUE = re.compile(r"\b(?:torque|apriete)\b")
_TORQUE_VALUE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:n\s*[·.*]?\s*m|nm)\b")
_CAUSAL = re.compile(r"\b(?:causo|provoco|origino|debido)\b")
_RECEIPT = re.compile(
    r"\b(?:acuse de (?:recepcion|recibido)|se recibio|fue recibido|"
    r"se recibieron|se entrego|fue entregado)\b"
)
_DATE = re.compile(r"\b\d{1,2} de [a-z]+\b")


class _LegacyEvidenceChecks(TypedDict):
    policy_signature: str
    basis: str
    interpretation: str
    status: str
    required_witnesses: list[str]
    missing_necessary_witnesses: list[str]
    counterevidence: list[str]
    retrieval_disposition: str
    evaluated_chars: int
    evaluation_truncated: bool
    query_truncated: bool


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _prepare(query: str, text: str) -> tuple[str, str]:
    if not isinstance(query, str) or not isinstance(text, str):
        raise ValueError("query evidence checks require string query and text")
    return _fold(query[:MAX_EVIDENCE_QUERY_CHARS]), text[:MAX_EVIDENCE_CHECK_CHARS]


def _query_polarity_unassessed(folded_query: str) -> bool:
    """The literal helper does not resolve the scope of a negated request."""
    return bool(
        _ACTION_NEGATION.search(folded_query)
        or re.search(r"\bcannot\b|\b\w+n['\u2019]t\b", folded_query)
    )


def _split(text: str, boundary_pattern: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    start = 0
    for boundary in boundary_pattern.finditer(text):
        yield start, text[start : boundary.start()]
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
    text: str,
    start: int,
    end: int,
    focus: tuple[int, int],
    reasons: list[str],
    *,
    evaluation_truncated: bool,
    query_truncated: bool,
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
    if _query_polarity_unassessed(folded_query):
        return []  # A negated fact may be precisely the requested evidence.
    past = bool(_PAST_EVENT.search(folded_query))
    if (_INSTRUCTION.search(folded_query) and not past) or not (
        past or re.search(r"\b(?:incidente|accidente)\b", folded_query)
    ):
        return []
    terms = set(_TERM.findall(folded_query))
    requested_event_terms = terms.difference(_EVENT_QUERY_GRAMMAR)
    instructional_source = bool(_INSTRUCTION.search(_fold(bounded)))
    observed_event = any(
        _OBSERVED_EVENT.search(_fold(sentence))
        and requested_event_terms.intersection(_TERM.findall(_fold(sentence)))
        for _, _, sentence in _sentences(bounded)
    )
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
                head = next(
                    (
                        term
                        for term in _TERM.finditer(clause, match.end())
                        if term.group() not in _DETERMINERS
                    ),
                    None,
                )
                if head is not None and head.group() in terms:
                    reasons.append("literal_requested_event_occurrence_is_negated")
                    focuses.append((clause_start + match.start(), clause_start + head.end()))
            if match := _EXCLUDED_SUBJECT.search(clause):
                excluded = next(
                    (
                        term
                        for term in _TERM.finditer(clause, match.end())
                        if term.group() in identifiers
                    ),
                    None,
                )
                if excluded is not None:
                    reasons.append("requested_named_subject_is_explicitly_excluded")
                    focuses.append((clause_start + match.start(), clause_start + excluded.end()))
        if (
            instructional_source
            and not observed_event
            and requested_event_terms.intersection(_TERM.findall(folded))
            and (warning := _HYPOTHETICAL_WARNING.search(folded)) is not None
        ):
            # A manual's conditional warning is related material, not a record
            # that the requested incident occurred.  Reuse the established
            # non-record reason so the v2 projection keeps it ``related_only``.
            reasons.append("source_explicitly_limits_observed_event_evidence")
            focuses.append(warning.span())
        if focuses:
            focus_start, focus_end = _original_span(sentence, *min(focuses))
            witnesses.append(
                _role_witness(
                    bounded,
                    sentence_start,
                    sentence_end,
                    (sentence_start + focus_start, sentence_start + focus_end),
                    reasons,
                    evaluation_truncated=len(text) > len(bounded),
                    query_truncated=len(query) > MAX_EVIDENCE_QUERY_CHARS,
                )
            )
            if len(witnesses) == MAX_ROLE_WITNESSES:
                break
    return witnesses


def _identified_actor(raw_clause: str, folded_clause: str) -> bool:
    if passive := re.search(r"\bautorizad[oa] por (.+)", folded_clause):
        return bool(_ACTOR_ROLES.search(passive[1]))
    active = _ACTIVE_AUTHORIZATION.search(folded_clause)
    if active is None:
        return False
    prefix = folded_clause[: active.start()].strip()
    if not prefix or re.search(r"\bse$", prefix):
        return False
    if _ACTOR_ROLES.search(prefix):
        return True
    original_end, _ = _original_span(raw_clause, active.start(), active.end())
    return any(
        len(term.group()) > 1
        and term.group()[0].isupper()
        and _fold(term.group()) not in _NOT_NAMED_ACTORS
        for term in _TERM.finditer(raw_clause[:original_end])
    )


def _legacy_requested_evidence_checks(query: str, text: str) -> _LegacyEvidenceChecks:
    """Check necessary actors, causal witnesses or receipts without claiming truth.

    Missing checks preserve evidence as related material.  Passing checks do
    not establish an answer, entailment, permission, approval or authority.
    """
    folded_query, bounded = _prepare(query, text)
    clauses = [
        (raw, _fold(raw))
        for _, _, sentence in _sentences(bounded)
        for _, raw in _clauses(sentence)
        if raw.strip()
    ]
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
    if re.search(r"\btorque\b", folded_query) and re.search(
        r"\b(?:causo|causa|provoco)\b", folded_query
    ):
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
        receipts = [
            folded
            for _, folded in positive
            if _RECEIPT.search(folded) and not re.search(r"\b(?:pendiente|todavia)\b", folded)
        ]
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
        "status": "missing"
        if missing
        else "necessary_checks_not_failed"
        if required
        else "not_assessed",
        "required_witnesses": required,
        "missing_necessary_witnesses": missing,
        "counterevidence": counterevidence,
        "retrieval_disposition": "related_evidence_only" if missing else "unchanged",
        "evaluated_chars": len(bounded),
        "evaluation_truncated": len(text) > len(bounded),
        "query_truncated": len(query) > MAX_EVIDENCE_QUERY_CHARS,
    }


_SUBJECT_ID = re.compile(r"\b[a-z]+(?:-?\d+)+\b", re.IGNORECASE)
_NOT_SUBJECTS = _NOT_NAMED_ACTORS | frozenset(
    {
        "que",
        "cual",
        "cuales",
        "quien",
        "como",
        "donde",
        "cuando",
        "en",
        "por",
        "busca",
        "encuentra",
        "muestra",
        "what",
        "which",
        "when",
        "where",
        "who",
        "find",
        "show",
        "how",
        "sha256",
        "sha512",
        "md5",
        "xxh3",
        "utf8",
        "utf16",
        "proyecto",
        "equipo",
        "respaldo",
        "factura",
        "invoice",
        "project",
        "backup",
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    }
)
_NAME_ANCHORS = frozenset(
    {"de", "del", "para", "of", "for", "proyecto", "project", "equipo", "respaldo", "backup"}
)
_SCOPED_BOUNDARY = re.compile(r";|(?<!\d),|,(?!\d)|\b(?:y|pero|aunque|ni|sin)\b", re.IGNORECASE)
_ACTION_REPLACEMENT = re.compile(r"\b(?:reemplaz\w*|sustit\w*|cambi\w*|replac\w*)\b")
_ACTION_RESTORE = re.compile(r"\b(?:restaur\w*|restor\w*)\b")
_ACTION_COMPARE = re.compile(r"\b(?:compar\w*|cotej\w*|verific\w*|verif\w*)\b")
_PENDING_ACTION = re.compile(
    r"\b(?:pendiente|programad\w*|planificad\w*|previst\w*|debera|debe|deben|"
    r"se requiere|se propone|futuro|probar|probado|planned|pending|scheduled|will)\b"
)
# Keep query polarity conservative across the languages already supported by
# lexical retrieval.  A negated request is left unassessed rather than having
# its requested absence silently interpreted as a completed positive action.
_ACTION_NEGATION = re.compile(
    r"\b(?:no|sin|ni|nadie|nunca|jamas|tampoco|ningun(?:o|a|os|as)?|"
    r"not|without|never|none|nobody|nothing|neither|nor|"
    r"nicht|ohne|kein(?:e|en|em|er|es)?)\b"
)
_COMPLETED_ACTION = re.compile(
    r"\b(?:reemplazo|reemplazaron|reemplazad[oa]s?|sustituyo|sustituyeron|sustituid[oa]s?|"
    r"cambio|cambiaron|cambiad[oa]s?|restauro|restauraron|restaurad[oa]s?|"
    r"comparo|compararon|comparad[oa]s?|cotejo|cotejaron|cotejad[oa]s?|"
    r"verifico|verificaron|verificad[oa]s?|restored|replaced|compared|verified)\b"
)
_UNCONFIRMED_ACTION = re.compile(
    r"\b(?:no (?:se )?(?:(?:ha|han|pudo|pudieron|puede|pueden|podido)\s+){0,2}"
    r"(?:comprobado|confirmado|determinado|verificado|comprobar|confirmar|determinar|verificar)|"
    r"no (?:se )?(?:sabe|conoce|consta)|se desconoce)\b"
)
_EVENT_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
    r"\d{1,2}\s+(?:de\s+)?(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)(?:\s+(?:de\s+)?\d{4})?)\b"
)


def _requested_subjects(query: str) -> tuple[str, ...]:
    values = [
        match.group()
        for match in _SUBJECT_ID.finditer(query)
        if _fold(match.group()).replace("-", "") not in _NOT_SUBJECTS
    ]
    # Literal names require a relationship anchor, not title capitalization or
    # an inferred entity from a filename/catalogue.  Numeric asset IDs are exact.
    previous = ""
    for match in _TERM.finditer(query):
        token = match.group()
        if (
            previous in _NAME_ANCHORS
            and token[0].isupper()
            and len(token) > 1
            and _fold(token) not in _NOT_SUBJECTS
        ):
            if not any(_fold(token) == _fold(value) for value in values):
                values.append(token)
        previous = _fold(token)
    return tuple(values[:8])


def _mentions(value: str, subject: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(_fold(subject)) + r"(?!\w)", _fold(value)) is not None


def _subject_scope(text: str, subjects: tuple[str, ...]) -> str:
    if not subjects:
        return "not_requested"
    folded = _fold(text)
    exclusions = [
        re.compile(
            r"\b(?:no (?:es|son|corresponde a|corresponde al|se trata de)|is not|not)\s+"
            + re.escape(_fold(subject))
            + r"\b"
        )
        for subject in subjects
    ]
    positive_mentions = folded
    for exclusion in exclusions:
        positive_mentions = exclusion.sub("", positive_mentions)
    if all(_mentions(positive_mentions, subject) for subject in subjects):
        return "aligned"
    if any(exclusion.search(folded) for exclusion in exclusions):
        return "different"
    requested_prefixes: set[str] = set()
    for subject in subjects:
        if not _SUBJECT_ID.fullmatch(subject):
            continue
        prefix = re.match(r"[a-z]+", _fold(subject))
        if prefix is not None:
            requested_prefixes.add(prefix.group(0))
    different_subject_id = False
    for subject_match in _SUBJECT_ID.finditer(folded):
        prefix = re.match(r"[a-z]+", subject_match.group())
        if (
            prefix is not None
            and prefix.group(0) in requested_prefixes
            and not any(subject_match.group() == _fold(subject) for subject in subjects)
        ):
            different_subject_id = True
            break
    if requested_prefixes and different_subject_id:
        return "different"
    if any(not _SUBJECT_ID.fullmatch(subject) for subject in subjects):
        named = _requested_subjects(text)
        if any(
            not _SUBJECT_ID.fullmatch(name)
            and not any(_mentions(name, subject) for subject in subjects)
            for name in named
        ):
            return "different"
    return "unresolved"


def _scoped_units(text: str, subjects: tuple[str, ...]) -> list[dict[str, object]]:
    units: list[dict[str, object]] = []
    inherited_scope = "unresolved" if subjects else "not_requested"
    for sentence_id, (sentence_start, _, sentence) in enumerate(_sentences(text)):
        # A leading hypothetical "Si"/"If" scopes every coordinated action in
        # this sentence.  Affirmative "Sí" is distinct: do not fold accents.
        conditional = re.match(r"(?:si|if)\b", sentence, re.IGNORECASE) is not None
        folded_sentence = _fold(sentence)
        unconfirmed = _UNCONFIRMED_ACTION.search(folded_sentence)
        # An unconfirmed evaluation can scope coordinated questions ("cuándo
        # restauraron ... ni si compararon ..."). Its negation concerns our
        # knowledge of the event, not the event's occurrence. A completed
        # action preceding that marker retains its own clause's scope.
        unconfirmed_sentence = bool(
            unconfirmed and not _COMPLETED_ACTION.search(folded_sentence[:unconfirmed.start()])
        )
        start = 0
        connector = ""
        boundaries = list(_SCOPED_BOUNDARY.finditer(sentence))
        for boundary in (*boundaries, None):
            end = len(sentence) if boundary is None else boundary.start()
            raw = sentence[start:end]
            stripped = raw.strip()
            if stripped:
                offset = sentence_start + start + len(raw) - len(raw.lstrip())
                scope = _subject_scope(stripped, subjects)
                if scope in {"aligned", "different"}:
                    inherited_scope = scope
                elif scope == "unresolved":
                    # A heading/previous clause may establish the subject, but
                    # an intervening explicit subject changes that scope.  A
                    # later unrelated target mention cannot lend it backwards.
                    scope = inherited_scope
                units.append(
                    {
                        "text": stripped,
                        "folded": _fold(stripped),
                        "start": offset,
                        "end": offset + len(stripped),
                        "scope": scope,
                        "sentence": sentence_id,
                        "connector": connector,
                        "conditional": conditional,
                        "unconfirmed": unconfirmed_sentence or bool(_UNCONFIRMED_ACTION.search(_fold(stripped))),
                    }
                )
            if boundary is None:
                break
            connector = _fold(boundary.group())
            start = boundary.start() if connector in {"ni", "sin"} else boundary.end()
    return units


def _unit_int(unit: Mapping[str, object], name: str) -> int:
    value = unit.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"scoped evidence unit has an invalid {name}")
    return value


def _noun_key(value: str) -> str:
    value = _fold(value)
    if value.endswith("es") and value[:-2].endswith(("r", "l", "n")):
        return value[:-2]
    return value[:-1] if value.endswith("s") else value


def _replacement_object(query: str) -> str | None:
    match = _ACTION_REPLACEMENT.search(query)
    if match is None:
        return None
    tail = query[match.end() :]
    words = [
        value
        for value in _TERM.findall(tail)
        if value not in {"de", "del", "el", "la", "los", "las", "the", "of"}
    ]
    return _noun_key(words[0]) if words else None


def _action_matches(unit: dict[str, object], action: str, object_term: str | None) -> bool:
    value = str(unit["folded"])
    pattern = {
        "replacement": _ACTION_REPLACEMENT,
        "restore": _ACTION_RESTORE,
        "hash_comparison": _ACTION_COMPARE,
    }[action]
    match = pattern.search(value)
    if match is None:
        return False
    if action == "hash_comparison":
        return re.search(r"\bhash(?:es)?\b", value) is not None
    if action == "replacement" and object_term:
        following = [
            word
            for word in _TERM.findall(value[match.end() :])
            if word not in {"de", "del", "el", "la", "los", "las", "the", "of"}
        ]
        preceding = value[max(0, match.start() - 80) : match.start()]
        passive_object = re.search(
            r"\b" + re.escape(object_term) + r"(?:s|es)?\s+"
            r"(?:(?:de|del)\s+\S+\s+)?(?:se|fue|fueron|ha sido|han sido|was|were)\s*$",
            preceding,
        )
        return (
            bool(following and _noun_key(following[0]) == object_term) or passive_object is not None
        )
    return True


def _action_state(unit: dict[str, object], action: str) -> str:
    value = str(unit["folded"])
    if unit["conditional"] or unit.get("unconfirmed"):
        return "unknown"  # Neither execution nor its negation was asserted.
    if _ACTION_NEGATION.search(value):
        return "negated"
    if _PENDING_ACTION.search(value):
        return "pending"
    pattern = {
        "replacement": _ACTION_REPLACEMENT,
        "restore": _ACTION_RESTORE,
        "hash_comparison": _ACTION_COMPARE,
    }[action]
    event = pattern.search(value)
    if event is None or not _COMPLETED_ACTION.fullmatch(event.group()):
        return "unknown"
    if event.group() in {"reemplazo", "cambio", "restauro", "comparo", "cotejo", "verifico"}:
        start, end = _original_span(str(unit["text"]), event.start(), event.end())
        actual = str(unit["text"])[start:end].casefold()
        if not actual.endswith("ó") and not re.search(r"\bse\s+$", value[: event.start()]):
            return "unknown"  # A noun such as "reemplazo" is not a past event.
    return "necessary_marker_present"


def _requested_document_kind(query: str) -> str | None:
    kind = re.search(r"\b(?:que|cual|what|which)\s+(?:[a-z]+\s+)?(factura|invoice)\b", query)
    if kind is None:
        kind = re.search(
            r"\b(?:encuentra|busca|muestra|find|show)\s+(?:(?:la|una|the|an?)\s+)?(factura|invoice)\b",
            query,
        )
    return "invoice" if kind else None


def requested_evidence_checks(query: str, text: str) -> dict[str, object]:
    """Scope necessary evidence to the requested subject, action and event.

    A related source, invoice reference or completed sync does not establish a
    different requested action.  Recognized absence/negation is explicit;
    unknown questions remain unassessed, never silently upgraded to entailment.
    """
    folded_query, bounded = _prepare(query, text)
    legacy = _legacy_requested_evidence_checks(query, text)
    if _query_polarity_unassessed(folded_query):
        # Preserve the retrieved passage without asserting either execution or
        # contradiction when the request's polarity/scope is not interpreted.
        return {
            **legacy,
            "status": "not_assessed",
            "not_assessed_reason": "query_polarity_scope_not_supported",
            "required_witnesses": [],
            "missing_necessary_witnesses": [],
            "counterevidence": [],
            "retrieval_disposition": "unchanged",
            "applicability": {
                "families": [],
                "requested_subjects": [],
                "subject_scope": "not_requested",
            },
            "scoped_observations": [],
        }
    required = list(legacy["required_witnesses"])
    missing = list(legacy["missing_necessary_witnesses"])
    counter = list(legacy["counterevidence"])
    families: list[str] = ["legacy_necessary_witnesses"] if required else []
    kind = _requested_document_kind(folded_query)
    replacement = bool(kind and _ACTION_REPLACEMENT.search(folded_query))
    dated_actions = bool(
        re.search(r"\b(?:fecha|cuando|date|when)\b", folded_query)
        and (
            _ACTION_RESTORE.search(folded_query)
            or (_ACTION_COMPARE.search(folded_query) and "hash" in folded_query)
        )
    )
    if kind:
        families.append("documented_action")
    if dated_actions:
        families.append("dated_completed_actions")
    subjects = _requested_subjects(query[:MAX_EVIDENCE_QUERY_CHARS]) if families else ()
    scope = _subject_scope(bounded, subjects)
    units = _scoped_units(bounded, subjects)
    aligned_text = "\n".join(
        str(unit["text"]) for unit in units if unit["scope"] in {"aligned", "not_requested"}
    )
    if subjects and legacy["required_witnesses"]:
        scoped_legacy = _legacy_requested_evidence_checks(query, aligned_text)
        missing = list(scoped_legacy["missing_necessary_witnesses"])
        counter = list(scoped_legacy["counterevidence"])
    observations: list[dict[str, object]] = []

    def observe(requirement: str, state: str, unit: dict[str, object] | None = None) -> None:
        if len(observations) < 16:
            observations.append(
                {
                    "requirement": requirement,
                    "state": state,
                    "subject_scope": str(unit["scope"]) if unit is not None else scope,
                    "start_char": _unit_int(unit, "start") if unit is not None else None,
                    "end_char": _unit_int(unit, "end") if unit is not None else None,
                }
            )

    if subjects:
        required.append("requested_subject")
        subject_unit = next(
            (
                unit
                for unit in units
                if all(_mentions(str(unit["text"]), value) for value in subjects)
            ),
            None,
        )
        if subject_unit is None and scope == "aligned":
            # This span witnesses literal identity mentions, not a relationship
            # between actions in distinct clauses or a cross-subject conclusion.
            subject_unit = {"start": 0, "end": len(bounded), "scope": scope}
        if scope != "aligned":
            missing.append("requested_subject")
            if scope == "different":
                counter.append("requested_subject_explicitly_different_or_excluded")
        observe(
            "requested_subject",
            "necessary_marker_present"
            if scope == "aligned"
            else "different_subject"
            if scope == "different"
            else "unknown",
            subject_unit,
        )
    if required and subjects and scope != "aligned":
        # Do not retain legacy positive witnesses from an unrelated subject.
        missing.extend(str(value) for value in legacy["required_witnesses"])
    if kind:
        requirement = "requested_document_kind:invoice"
        required.append(requirement)
        heading = next(
            (
                unit
                for unit in units
                if not bounded[: _unit_int(unit, "start")].strip()
                and re.match(r"^(?:factura|invoice)\b", str(unit["folded"]))
                and not re.search(
                    r"\b(?:ejemplo|modelo|referencia|citada|borrador|proforma|example|draft)\b",
                    str(unit["folded"]),
                )
            ),
            None,
        )
        if heading is None:
            missing.append(requirement)
        observe(requirement, "necessary_marker_present" if heading else "unknown", heading)
    actions: list[str] = []
    if replacement:
        actions.append("replacement")
    if dated_actions:
        if _ACTION_RESTORE.search(folded_query):
            actions.append("restore")
        if _ACTION_COMPARE.search(folded_query) and "hash" in folded_query:
            actions.append("hash_comparison")
    aligned_negation = False
    completed: dict[str, dict[str, object]] = {}
    object_term = _replacement_object(folded_query)
    for action in actions:
        requirement = f"completed_action:{action}"
        required.append(requirement)
        matching = [
            unit
            for unit in units
            if _action_matches(unit, action, object_term)
            and unit["scope"] in {"aligned", "not_requested"}
        ]
        states = [(unit, _action_state(unit, action)) for unit in matching]
        affirmative = next(
            (unit for unit, state in states if state == "necessary_marker_present"), None
        )
        negative = next((unit for unit, state in states if state == "negated"), None)
        if affirmative is None:
            missing.append(requirement)
        else:
            completed[action] = affirmative
            observe(requirement, "necessary_marker_present", affirmative)
        if negative is not None:
            aligned_negation = True
            counter.append(f"requested_action_negated:{action}")
            observe(requirement, "negated", negative)
        elif affirmative is None:
            pending = next((unit for unit, state in states if state == "pending"), None)
            observe(requirement, "pending" if pending else "unknown", pending)
    if dated_actions:
        for action in actions:
            requirement = f"event_linked_date:{action}"
            required.append(requirement)
            unit = completed.get(action)
            dated = unit if unit is not None and _EVENT_DATE.search(str(unit["folded"])) else None
            if dated is None and unit is not None and unit["connector"] == "y":
                # One coordinated timestamp may frame both requested actions;
                # a preparation date or a different sentence cannot lend it.
                dated = next(
                    (
                        prior
                        for prior in completed.values()
                        if prior["sentence"] == unit["sentence"]
                        and _unit_int(prior, "end") < _unit_int(unit, "start")
                        and _EVENT_DATE.search(str(prior["folded"]))
                    ),
                    None,
                )
                if dated is not None:
                    # The witness for the second action must include its
                    # coordinated clause as well as the earlier timestamp;
                    # citing only the first action loses the relationship.
                    dated = {**dated, "end": _unit_int(unit, "end")}
            if dated is None:
                missing.append(requirement)
            observe(requirement, "necessary_marker_present" if dated else "unknown", dated)
    role_counter = query_role_counterevidence(query, aligned_text if subjects else bounded)
    disposition = (
        "related_evidence_only"
        if subjects and scope != "aligned"
        else "contradictory_evidence"
        if aligned_negation or counter
        else "related_evidence_only"
        if missing
        else "contradictory_evidence"
        if role_counter
        else "unchanged"
    )
    return {
        **legacy,
        "policy_signature": CHECKS_POLICY_SIGNATURE,
        "status": "missing"
        if missing
        else "necessary_checks_not_failed"
        if required
        else "not_assessed",
        "required_witnesses": list(dict.fromkeys(required)),
        "missing_necessary_witnesses": list(dict.fromkeys(missing)),
        "counterevidence": list(dict.fromkeys(counter)),
        "retrieval_disposition": disposition,
        "applicability": {
            "families": families,
            "requested_subjects": list(subjects),
            "subject_scope": scope,
        },
        "scoped_observations": observations,
    }
