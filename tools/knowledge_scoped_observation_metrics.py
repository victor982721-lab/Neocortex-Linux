"""Independent literal validation of scoped necessary-witness observations.

Development only: this module imports no NeoCortex code and knows no fixture
names, IDs, relevance grades or answers. Its bounded grammar verifies markers,
scope and polarity, not entailment. Unrecognized claims fail verification.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import unicodedata


POLICY = "query-necessary-evidence-checks-v2"
SCOPES = {"aligned", "different", "unresolved", "not_requested"}
STATES = {"necessary_marker_present", "negated", "pending", "different_subject", "unknown"}
_ID = re.compile(r"\b[^\W\d_]+\d+[a-z]?\b", re.I)
_NAMED = re.compile(
    r"\b(?:de|del|of|for|backup|respaldo|proyecto|project|equipo|asset|files|archivos|en|in)\s+"
    r"(?:el\s+|la\s+|the\s+)?([A-ZÁÉÍÓÚÑ][\wáéíóúñ-]*)\b"
)
_ACTION = {
    "replacement": re.compile(r"\b(?:reemplaz\w*|sustitu\w*|cambi\w*|replac\w*)\b"),
    "restore": re.compile(r"\b(?:restaur\w*|restor\w*)\b"),
    "hash_comparison": re.compile(r"\b(?:compar\w*|cotej\w*|verific\w*|check\w*|match\w*)\b"),
}
_COMPLETED = {
    "replacement": re.compile(
        r"(?:reemplazo|reemplazaron|reemplazad[oa]s?|sustituyo|sustituyeron|sustituid[oa]s?|cambio|cambiaron|cambiad[oa]s?|replaced)\Z"
    ),
    "restore": re.compile(r"(?:restauro|restauraron|restaurad[oa]s?|restored)\Z"),
    "hash_comparison": re.compile(
        r"(?:comparo|compararon|comparad[oa]s?|cotejo|cotejaron|cotejad[oa]s?|verifico|verificaron|verificad[oa]s?|compared|checked|matched)\Z"
    ),
}
_HASH = re.compile(r"\b(?:hash(?:es)?|sha\s*[- ]?256)\b")
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}|"
    r"\d{1,2}(?: de)? [a-z]+(?: de)? \d{4}|[a-z]+ \d{1,2},? \d{4})\b"
)
_PENDING = re.compile(
    r"\b(?:pendiente|programad\w*|previst\w*|preve|planned|pending|scheduled|will|shall)\b"
)
_NEG = re.compile(r"\b(?:no|nunca|tampoco|sin|ni|not|never|without|neither|nor)\b")
_UNASSERTED = re.compile(
    r"\b(?:no es cierto que|no es verdad que|no se confirmo que|no se afirma que|"
    r"it is not true that|not confirmed that|niega que|desmiente que|supongamos|"
    r"hipotesis|hypothetical|would|could|podria|pudiera|en caso de que|unless)\b"
)


def _fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )


def _names(value: str) -> list[str]:
    names = [(match.start(), match.group()) for match in _ID.finditer(value)]
    names.extend((match.start(1), match[1]) for match in _NAMED.finditer(value))
    result = []
    for _, name in sorted(names):
        if _fold(name) not in {_fold(item) for item in result}:
            result.append(name)
    return result


def _contains(value: str, subject: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(_fold(subject)) + r"(?!\w)", _fold(value)) is not None


def _scope(value: str, subjects: list[str]) -> str:
    if not subjects:
        return "not_requested"
    folded = _fold(value)
    for subject in subjects:
        identity = re.escape(_fold(subject))
        if re.search(
            r"\b(?:no es|no corresponde (?:a|al)|is not|not)\s+(?:el\s+|la\s+)?" + identity + r"\b",
            folded,
        ):
            return "different"
    if any(_contains(value, subject) for subject in subjects):
        return "aligned"
    return "different" if _names(value) else "unresolved"


def _unasserted(sentence: str) -> bool:
    # Preserve the distinction between Spanish conditional Si and affirmative Sí.
    stripped = sentence.lstrip(' \t\r\n"{([:')
    return bool(re.search(r"\b(?:si|if)\b", stripped, re.I) or _UNASSERTED.search(_fold(sentence)))


@dataclass(frozen=True)
class Unit:
    start: int
    end: int
    text: str
    sentence: str
    scope: str
    date: bool


def _units(excerpt: str, subjects: list[str]) -> list[Unit]:
    result = []
    inherited = "not_requested" if not subjects else "unresolved"
    # Decimal points do not split a sentence. Newlines can be soft PDF wraps.
    boundaries = list(re.finditer(r"[!?;]+|(?<!\d)\.|\.(?!\d)", excerpt))
    left = 0
    for boundary in [*boundaries, None]:
        end = boundary.end() if boundary else len(excerpt)
        sentence = excerpt[left:end]
        part_left = left
        for split in [
            *re.finditer(
                r"\b(?:y|and|pero|but|aunque|although|mientras|while|whereas|ni|nor)\b|,",
                sentence,
                re.I,
            ),
            None,
        ]:
            part_end = left + split.start() if split else end
            raw = excerpt[part_left:part_end]
            local = _scope(raw, subjects)
            if local not in {"unresolved", "not_requested"}:
                inherited = local
            scope = inherited if local == "unresolved" else local
            has_date = bool(_DATE.search(_fold(raw)))
            result.append(Unit(part_left, part_end, raw, sentence, scope, has_date))
            # Keep the conjunction with the following clause so ni/nor remains visible.
            part_left = left + split.start() if split else end
        left = end
    return result


def _marker(action: str, text: str) -> bool:
    folded = _fold(text)
    return bool(_ACTION[action].search(folded)) and (
        action != "hash_comparison" or bool(_HASH.search(folded))
    )


def _action_state(action: str, unit: Unit) -> str:
    if not _marker(action, unit.text) or _unasserted(unit.sentence):
        return "unknown"
    folded = _fold(unit.text)
    match = _ACTION[action].search(folded)
    assert match is not None
    before = folded[: match.start()]
    if _NEG.search(before[-75:]):
        return "negated"
    if _PENDING.search(folded):
        return "pending"
    token = match.group()
    # Only explicit past/completed forms establish this necessary marker.
    # Future, conditional, infinitive and event nouns remain unknown.
    if not _COMPLETED[action].fullmatch(token):
        return "unknown"
    if re.search(r"\b(?:el|un|the|a|su)\s*$", before) and token.endswith("o"):
        return "unknown"
    return "necessary_marker_present"


def _event_has_date(units: list[Unit], index: int) -> bool:
    unit = units[index]
    if re.search(
        r"\b(?:unknown date|date unknown|fecha desconocida|sin fecha)\b", _fold(unit.text)
    ):
        return False
    if unit.date:
        return True
    if not index or not re.match(r"\s*(?:y|and)\b", unit.text, re.I):
        return False
    previous = units[index - 1]
    # Shared dates can carry across affirmative coordinated actions, not an
    # inspection, an unrelated sentence, a contrast or another asset.
    return (
        previous.sentence == unit.sentence
        and previous.scope == unit.scope
        and any(_action_state(action, previous) == "necessary_marker_present" for action in _ACTION)
        and _event_has_date(units, index - 1)
    )


def _invoice_ranges(excerpt: str) -> list[tuple[int, int]]:
    ranges = []
    offset = 0
    for line in excerpt.splitlines(keepends=True):
        # A heading is evidence of document kind; a quotation or mention is not.
        if re.match(
            r"^\s*(?:Factura|Invoice)\b(?:\s+(?:[A-Z]{1,8}[- ]?)?\d[\w/-]*)?\s*$",
            line.rstrip(),
            re.I,
        ):
            ranges.append((offset, offset + len(line.rstrip())))
        offset += len(line)
    return ranges


def validate_scoped_checks(
    query: str,
    excerpt: str,
    checks: dict[str, Any],
    legacy_required: set[str],
    legacy_missing: set[str],
    *,
    verified_legacy_counter: bool = False,
) -> tuple[set[str], set[str], set[str], str, list[str]]:
    """Verify declared observations against independent final-text predicates."""
    errors: list[str] = []
    folded_query = _fold(query)
    families = {"legacy_necessary_witnesses"} if legacy_required else set()
    actions: set[str] = set()
    if re.search(r"\b(?:factura|invoice)\b", folded_query):
        families.add("documented_action")
        if _ACTION["replacement"].search(folded_query):
            actions.add("replacement")
    dated = bool(re.search(r"\b(?:fecha|cuando|when|date)\b", folded_query))
    dated_actions = {
        action for action in ("restore", "hash_comparison") if dated and _marker(action, query)
    }
    if dated_actions:
        families.add("dated_completed_actions")
        actions |= dated_actions
    subjects = _names(query) if families else []
    units = _units(excerpt, subjects)
    requested_scope = _scope(excerpt, subjects)
    required, missing = set(legacy_required), set(legacy_missing)
    proofs: dict[str, list[tuple[str, str, int | None, int | None]]] = {}
    if subjects:
        required.add("requested_subject")
        subject_proofs = [
            (
                "necessary_marker_present"
                if _scope(unit.text, subjects) == "aligned"
                else "different_subject",
                _scope(unit.text, subjects),
                unit.start,
                unit.end,
            )
            for unit in units
            if _scope(unit.text, subjects) in {"aligned", "different"}
        ]
        proofs["requested_subject"] = subject_proofs
        if requested_scope != "aligned":
            missing.add("requested_subject")
    if "documented_action" in families:
        name = "requested_document_kind:invoice"
        required.add(name)
        proofs[name] = [
            ("necessary_marker_present", "unresolved", start, end)
            for start, end in _invoice_ranges(excerpt)
        ]
        if not proofs[name]:
            missing.add(name)
    counters: set[str] = set()
    if subjects and requested_scope == "different":
        counters.add("requested_subject_explicitly_different_or_excluded")
    for action in sorted(actions):
        name = f"completed_action:{action}"
        required.add(name)
        proofs[name] = []
        for unit in units:
            state = _action_state(action, unit)
            if (
                state != "unknown"
                and unit.scope in {"aligned", "not_requested"}
                and requested_scope != "different"
            ):
                proofs[name].append((state, unit.scope, unit.start, unit.end))
        if not any(proof[0] == "necessary_marker_present" for proof in proofs[name]):
            missing.add(name)
        if any(proof[0] == "negated" for proof in proofs[name]):
            counters.add(f"requested_action_negated:{action}")
        if action in dated_actions:
            date_name = f"event_linked_date:{action}"
            required.add(date_name)
            proofs[date_name] = [
                ("necessary_marker_present", unit.scope, unit.start, unit.end)
                for index, unit in enumerate(units)
                if _event_has_date(units, index)
                and unit.scope in {"aligned", "not_requested"}
                and requested_scope != "different"
                and _action_state(action, unit) == "necessary_marker_present"
            ]
            if not proofs[date_name]:
                missing.add(date_name)
    applicability = checks.get("applicability")
    if not isinstance(applicability, dict) or (
        not isinstance(applicability.get("families"), list)
        or any(not isinstance(family, str) for family in applicability["families"])
        or len(applicability["families"]) != len(families)
        or set(applicability["families"]) != families
        or applicability.get("requested_subjects") != subjects
        or applicability.get("subject_scope") != requested_scope
    ):
        errors.append("scoped_applicability_not_verified")
    observations = checks.get("scoped_observations")
    if not isinstance(observations, list) or len(observations) > 16:
        observations = []
        errors.append("invalid_scoped_observations")
    seen = set()
    for observation in observations:
        if not isinstance(observation, dict):
            errors.append("invalid_scoped_observation")
            continue
        name, state = observation.get("requirement"), observation.get("state")
        scope = observation.get("subject_scope")
        start, end = observation.get("start_char"), observation.get("end_char")
        if (
            not isinstance(name, str)
            or name not in proofs
            or name in seen
            or not isinstance(state, str)
            or state not in STATES
            or not isinstance(scope, str)
            or scope not in SCOPES
        ):
            errors.append("unknown_or_duplicate_scoped_observation")
            continue
        seen.add(name)
        if start is None and end is None:
            if state != "unknown" or proofs[name]:
                errors.append("scoped_absence_not_verified")
            if scope != requested_scope:
                errors.append("scoped_absence_subject_mismatch")
            continue
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(excerpt):
            errors.append("scoped_observation_outside_final_excerpt")
            continue
        possible = [
            proof
            for proof in proofs[name]
            if proof[0] == state
            and proof[2] is not None
            and proof[3] is not None
            and start < proof[3]
            and end > proof[2]
            and (proof[1] == scope or name == "requested_document_kind:invoice")
        ]
        marker_valid = False
        for proof in possible:
            # Merely overlapping another valid unit by one character cannot
            # borrow its scope for a marker situated in an unrelated unit.
            span = excerpt[max(start, proof[2]) : min(end, proof[3])]
            marker_valid |= (
                (
                    any(_contains(span, subject) for subject in subjects)
                    if state == "necessary_marker_present"
                    else _scope(span, subjects) == "different"
                )
                if name == "requested_subject"
                else bool(re.search(r"\b(?:factura|invoice)\b", _fold(span)))
                if name.endswith(":invoice")
                else _marker(name.partition(":")[2], span)
            )
        if not possible or not marker_valid:
            errors.append("scoped_observation_claim_not_verified_in_final_text")
    if seen != set(proofs):
        errors.append("scoped_required_observation_missing")
    if subjects and requested_scope != "aligned":
        disposition = "related_evidence_only"
    elif verified_legacy_counter or any(
        reason.startswith("requested_action_negated:") for reason in counters
    ):
        disposition = "contradictory_evidence"
    else:
        disposition = "related_evidence_only" if missing else "unchanged"
    if checks.get("retrieval_disposition") != disposition:
        errors.append("scoped_retrieval_disposition_not_verified")
    return required, missing, counters, disposition, errors
