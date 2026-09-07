"""Independent, development-only checks of the emitted context-v2 contract.

This supplement is deliberately not an entailment model. It neither imports
the product's witness classifier nor changes frozen relevance judgments. A
recognized necessary-witness policy can justify presenting related material,
but only source-backed, explicitly bounded output can receive that treatment.
Unknown dispositions remain a gate failure, never an automatic exclusion.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
from typing import Any
import unicodedata


METRIC_SCHEMA = "neocortex.functional-context-operationalization/v2.2"
PREVIOUS_ADAPTER_SHA256 = "acb0728fb351c67457d03fff4da7b745de21c2cfb6fd9ad26ec1840c2ea82795"
PREVIOUS_OPERATIONALIZATION_SHA256 = (
    "5c44480eb54b18268a2133e8c8ea958a6a9167137f7d7c1a9f331d2a624dfb5e"
)
CHECKS_POLICY = "query-necessary-evidence-checks-v1"
ROLE_POLICY = "query-role-counterevidence-v1"
DISPOSITIONS = {"related_only", "contradictory", "evidence_candidate"}
ORIGINAL_HARNESS_SHA256 = "cce9ebcd0dbd37a34fb31cb9bf36b2307e3b6acad8e3a58220664b7e923ed0e8"
_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_AUTHORIZATION = re.compile(r"\b(?:autorizo|autorizaron|fue autorizado|fue autorizada)\b")
_ROLE = re.compile(
    r"\b(?:supervisor|supervisora|responsable|jefe|jefa|ingeniero|ingeniera|coordinador|coordinadora|director|directora|gerente|fabricante|operador|operadora|representante)\b"
)
_RECEIPT = re.compile(
    r"\b(?:acuse de (?:recepcion|recibido)|se recibio|fue recibido|se recibieron|se entrego|fue entregado)\b"
)
_NEGATION = re.compile(r"\b(?:no|nadie|nunca|jamas|tampoco)\b")
_NOT_ACTORS = {
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
_IDENTIFIER = re.compile(r"[a-z]+\d+\Z")
_NON_NOMINAL_WORDS = frozenset(
    {
        "a",
        "al",
        "de",
        "del",
        "para",
        "por",
        "con",
        "sin",
        "en",
        "entre",
        "sobre",
        "hacia",
        "desde",
        "hasta",
        "segun",
        "contra",
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "unos",
        "unas",
        "y",
        "o",
        "pero",
        "sino",
        "excepto",
        "que",
        "quien",
        "cual",
        "como",
        "cuando",
        "donde",
        "si",
        "no",
        "es",
        "era",
        "fue",
        "se",
    }
)
_NONASSERTED_EXCLUSION = re.compile(
    r"\b(?:si|podria|pudiera|puede|pueda|deberia|supongamos|supuesto|hipotesis|"
    r"posible|posiblemente|quizas?|tal vez|en caso|cuando|a menos|salvo que|niega|nego|negado|refuta|refuto|"
    r"desmiente|desmintio)\b"
)


def verify_operationalization(path: Path, *, frozen_dataset_sha256: str) -> str:
    """Require a separately recorded supplement, never rewrite the old freeze."""
    data = path.read_bytes()
    contract = json.loads(data)
    expected = {
        "schema": METRIC_SCHEMA,
        "frozen_dataset_sha256": frozen_dataset_sha256,
        "original_harness_sha256": ORIGINAL_HARNESS_SHA256,
        "authorized_criterion": "do_not_present_unsupported_material_as_sufficient_evidence",
        "legacy_baseline_reclassified": False,
        "frozen_queries_or_judgments_changed": False,
        "recorded_before_candidate_sha_freeze": True,
        "previous_adapter_sha256": PREVIOUS_ADAPTER_SHA256,
        "previous_operationalization_sha256": PREVIOUS_OPERATIONALIZATION_SHA256,
        "change_kind": "independent_scoped_policy_v2_compatibility_not_new_judgments",
    }
    if not isinstance(contract, dict) or any(
        contract.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("context-v2 operationalization supplement is absent or incompatible")
    adapter_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if contract.get("adapter_sha256") != adapter_sha:
        raise ValueError("context-v2 metric implementation differs from its recorded supplement")
    scoped_sha = hashlib.sha256(
        Path(__file__).with_name("knowledge_scoped_observation_metrics.py").read_bytes()
    ).hexdigest()
    if contract.get("scoped_verifier_sha256") != scoped_sha:
        raise ValueError("scoped metric implementation differs from its recorded supplement")
    return hashlib.sha256(data).hexdigest()


def _fold(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def _normalize(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _source_excerpt(citation: dict[str, Any]) -> str:
    text = citation["excerpt"]
    if citation.get("fragment_state") == "truncated" and text.endswith(" …[truncated]"):
        text = text.removesuffix(" …[truncated]")
    return re.sub(r"\[([^\[\]\n]+)\]", r"\1", _normalize(text)).strip(" …")


def _clauses(text: str) -> list[str]:
    # Preserve decimal values. These are literal necessary-witness checks,
    # not assertions that a clause entails an answer or names a real actor.
    return [
        part.strip()
        for part in re.split(
            r"[!?\r\n;]+|(?<!\d)[.,]|[.,](?!\d)|\b(?:y|pero|aunque|sin embargo)\b", text, flags=re.I
        )
        if part.strip()
    ]


def expected_necessary_checks(query: str, excerpt: str) -> tuple[set[str], set[str]]:
    """Recompute the six recognized *necessary* checks from final text only."""
    folded_query = _fold(query)
    clauses = [(raw, _fold(raw)) for raw in _clauses(excerpt)]
    affirmative = [(raw, folded) for raw, folded in clauses if not _NEGATION.search(folded)]
    required: set[str] = set()
    missing: set[str] = set()
    if re.search(r"\bquien\b", folded_query) and re.search(r"\bautoriz\w*\b", folded_query):
        required.update(("authorization_event", "identified_authorizing_actor"))
        events = [(raw, folded) for raw, folded in affirmative if _AUTHORIZATION.search(folded)]
        if not events:
            missing.add("authorization_event")
        actor = False
        for raw, folded in events:
            if passive := re.search(r"\bautorizad[oa] por (.+)", folded):
                actor |= bool(_ROLE.search(passive[1]))
                continue
            match = re.search(r"\b(?:autorizo|autorizaron)\b", folded)
            if match is None:
                continue
            prefix = folded[: match.start()].strip()
            if not prefix or re.search(r"\bse$", prefix):
                continue
            if _ROLE.search(prefix):
                actor = True
            # Use tokens, not normalized offsets, for case-sensitive names.
            prior_count = len(_TERM.findall(prefix))
            actor |= any(
                len(word) > 1 and word[0].isupper() and _fold(word) not in _NOT_ACTORS
                for word in _TERM.findall(raw)[:prior_count]
            )
        if not actor:
            missing.add("identified_authorizing_actor")
    if re.search(r"\btorque\b", folded_query) and re.search(
        r"\b(?:causo|causa|provoco)\b", folded_query
    ):
        required.update(("applied_torque_value_with_unit", "asserted_torque_to_damage_causal_link"))
        torque = [
            folded for _, folded in affirmative if re.search(r"\b(?:torque|apriete)\b", folded)
        ]
        if not any(
            re.search(r"\d+(?:[.,]\d+)?\s*(?:n\s*[·.*]?\s*m|nm)\b", clause) for clause in torque
        ):
            missing.add("applied_torque_value_with_unit")
        if not any(re.search(r"\b(?:causo|provoco|origino|debido)\b", clause) for clause in torque):
            missing.add("asserted_torque_to_damage_causal_link")
    if re.search(r"\bacuse\b", folded_query) or (
        re.search(r"\bdemuestra\b", folded_query) and re.search(r"\brecib\w*\b", folded_query)
    ):
        required.add("affirmative_receipt_or_delivery_acknowledgment")
        receipts = [
            folded
            for _, folded in affirmative
            if _RECEIPT.search(folded) and not re.search(r"\b(?:pendiente|todavia)\b", folded)
        ]
        if not receipts:
            missing.add("affirmative_receipt_or_delivery_acknowledgment")
        dates = re.findall(r"\b\d{1,2} de [a-z]+\b", folded_query)
        if dates:
            required.add("receipt_linked_to_requested_date")
            if not any(date in receipt for date in dates for receipt in receipts):
                missing.add("receipt_linked_to_requested_date")
    return required, missing


def _witness_sentence(excerpt: str, start: int, end: int) -> str | None:
    """Return only the final-text sentence containing the exact witness.

    Newlines can be PDF soft wraps, so they are not sentence boundaries here.
    A witness spanning substantive text on both sides of sentence punctuation
    is deliberately unverified instead of collecting unrelated context.
    """
    left = 0
    for boundary in re.finditer(r"[!?]+|(?<!\d)\.|\.(?!\d)", excerpt):
        if boundary.end() <= start:
            left = boundary.end()
            continue
        if boundary.start() < end and excerpt[boundary.end() : end].strip():
            return None
        return excerpt[left : boundary.end()]
    return excerpt[left:]


def _requested_subject_excluded(folded_query: str, folded_span: str, folded_sentence: str) -> bool:
    """Recognize a literal exclusion of the same explicitly named subject.

    A bare requested ID remains supported. A qualified ID additionally needs
    one to three adjacent nominal words appearing verbatim with that ID in
    the query. Prepositions, a different first ID, clause boundaries, multiple
    negations or hypothetical/denied statements remain unverified. This does
    not infer anything about the asset's condition or the reported event.
    """
    query_terms = _TERM.findall(folded_query)
    query_ids = {term for term in query_terms if _IDENTIFIER.fullmatch(term)}
    if not query_ids or len(_NEGATION.findall(folded_sentence)) != 1:
        return False
    if _NONASSERTED_EXCLUSION.search(folded_sentence):
        return False
    matches = list(
        re.finditer(r"\bno corresponde (?:al|a(?:\s+(?:el|la|los|las))?)\s+", folded_span)
    )
    if len(matches) != 1:
        return False
    tail = folded_span[matches[0].end() :]
    terms = list(_TERM.finditer(tail))
    for index, term in enumerate(terms[:4]):
        if not _IDENTIFIER.fullmatch(term.group()):
            continue
        identifier = term.group()
        if identifier not in query_ids:
            return False
        qualifiers = [item.group() for item in terms[:index]]
        if any(
            not value.isalpha() or len(value) < 2 or value in _NON_NOMINAL_WORDS
            for value in qualifiers
        ):
            return False
        nominal = tail[: term.end()]
        if not re.fullmatch(r"[^\W_]+(?:\s+[^\W_]+){0,3}", nominal):
            return False
        if not qualifiers:
            return True
        requested_nominal = [*qualifiers, identifier]
        width = len(requested_nominal)
        return any(
            query_terms[start : start + width] == requested_nominal
            for start in range(len(query_terms) - width + 1)
        )
    return False


def _role_errors(query: str, excerpt: str, witness: object) -> list[str]:
    if not isinstance(witness, dict):
        return ["counterevidence_not_an_object"]
    errors = []
    start, end = witness.get("start_char"), witness.get("end_char")
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in (start, end)
    ) or not 0 <= start < end <= len(excerpt):
        return ["counterevidence_span_outside_final_excerpt"]
    if witness.get("text") != excerpt[start:end]:
        errors.append("counterevidence_text_not_exact_final_span")
    if witness.get("policy_signature") != ROLE_POLICY or witness.get("basis") != "input_text":
        errors.append("unknown_counterevidence_policy")
    if witness.get("interpretation") != "literal_counterevidence_not_entailment_or_authority":
        errors.append("counterevidence_claims_unjustified_authority")
    if (
        witness.get("evaluation_truncated") is not False
        or witness.get("query_truncated") is not False
    ):
        errors.append("counterevidence_evaluation_incomplete")
    folded = _fold(excerpt[start:end])
    query_folded = _fold(query)
    observed = bool(
        re.search(
            r"\b(?:que paso|ocurrio|ocurrieron|sucedio|cayo|murio|reanudaron|termino|recupero|corto|comprobo|pararon|detuvo|quedo|tenia|sustituyeron|indico|bajo|acordo|confirmo|autorizo|causo|recibieron)\b",
            query_folded,
        )
    )
    requested_instruction = bool(
        re.search(r"\b(?:paso a paso|manual|guia|instrucciones|procedimiento)\b", query_folded)
    )
    if (requested_instruction and not observed) or not (
        observed or re.search(r"\b(?:incidente|accidente)\b", query_folded)
    ):
        errors.append("counterevidence_not_applicable_to_requested_role")
    reasons = witness.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        return [*errors, "counterevidence_missing_reasons"]
    for reason in reasons:
        if reason == "source_explicitly_limits_observed_event_evidence":
            valid = bool(
                re.search(
                    r"\b(?:no (?:es )?(?:un )?registro|no describe un hecho ocurrido|no demuestra su ejecucion|todavia no ejecutados|no acredita que se|solo en el catalogo)\b",
                    folded,
                )
            )
        elif reason == "requested_named_subject_is_explicitly_excluded":
            sentence = _witness_sentence(excerpt, start, end)
            valid = sentence is not None and _requested_subject_excluded(
                query_folded, folded, _fold(sentence)
            )
        elif reason == "literal_requested_event_occurrence_is_negated":
            matches = re.findall(
                r"\bno (?:se (?:presento|produjo|registro)|ocurrio|hubo|aparecio)\s+(?:(?:un|una|ningun|ninguna|el|la|los|las|ninguno|algun|alguna)\s+)*([^\W_]+)",
                folded,
            )
            valid = bool(set(matches) & set(_TERM.findall(query_folded)))
        else:
            valid = False
        if not valid:
            errors.append("counterevidence_reason_not_verified_in_final_text")
    return errors


def _citation_errors(
    query: str, citation: dict[str, Any], source: dict[str, Any], entry: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    excerpt = citation.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return ["missing_final_text"]
    path = Path(source["path"])
    try:
        data = path.read_bytes()
    except OSError:
        return ["source_bytes_unavailable"]
    if (
        not path.is_file()
        or path.is_symlink()
        or hashlib.sha256(data).hexdigest() != entry["sha256"]
    ):
        errors.append("source_bytes_not_pinned")
    if source.get("revision_state") != "current" or not source.get("processing_signature"):
        errors.append("source_revision_not_pinned_current")
    if (
        not source.get("resource_id")
        or not source.get("revision_id")
        or not citation.get("evidence_id")
    ):
        errors.append("citation_identity_missing")
    literal = _source_excerpt(citation)
    fragments = [part.strip() for part in re.split(r"\.{3}|…", literal) if part.strip()]
    source_variants = (
        _normalize(entry["source_text"]),
        _normalize(data.decode("utf-8", errors="ignore")),
    )
    if not fragments or not any(
        all(fragment in variant for fragment in fragments) for variant in source_variants
    ):
        errors.append("excerpt_not_in_frozen_source")
    locator = citation.get("locator")
    if not isinstance(locator, dict) or not (
        (isinstance(locator.get("section_id"), str) and locator["section_id"])
        or (type(locator.get("page")) is int and locator["page"] == 0)
    ):
        errors.append("missing_concrete_source_locator")
    elif "page" in locator and (isinstance(locator["page"], bool) or locator["page"] != 0):
        errors.append("page_outside_single_page_fixture")
    if isinstance(locator, dict) and any(key in locator for key in ("start_char", "end_char")):
        start, end = locator.get("start_char"), locator.get("end_char")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= max(len(data), len(entry["source_text"]))
        ):
            errors.append("invalid_source_character_locator")
    owner_extent = citation.get("extent")
    if owner_extent is not None:
        if not isinstance(owner_extent, dict) or owner_extent.get("units") != "characters":
            errors.append("unknown_owner_extent")
        else:
            if type(owner_extent.get("bounded")) is not bool:
                errors.append("owner_extent_bound_not_declared")
            for name in ("exact_reference_range", "chunk_range", "returned_range"):
                declared_range = owner_extent.get(name)
                if declared_range is None:
                    continue
                if not isinstance(declared_range, dict):
                    errors.append("invalid_owner_extent_range")
                    continue
                start, end = declared_range.get("start_char"), declared_range.get("end_char")
                if type(start) is not int or type(end) is not int or not 0 <= start < end:
                    errors.append("invalid_owner_extent_range")
                if declared_range.get("basis") not in {"source_section", "normalized_chunk"}:
                    errors.append("unknown_owner_extent_coordinate_space")
                if (
                    name == "exact_reference_range"
                    and isinstance(locator, dict)
                    and any(
                        key in locator and locator[key] != declared_range.get(key)
                        for key in ("start_char", "end_char")
                    )
                ):
                    errors.append("owner_extent_disagrees_with_source_locator")
    emitted_extent = citation.get("emitted_extent")
    expected_extent = {
        "units": "characters",
        "basis": "emitted_excerpt",
        "start_char": 0,
        "end_char": len(excerpt),
    }
    if (
        not isinstance(emitted_extent, dict)
        or any(emitted_extent.get(key) != value for key, value in expected_extent.items())
        or any(type(emitted_extent.get(key)) is not int for key in ("start_char", "end_char"))
    ):
        errors.append("final_excerpt_extent_unverified")
    elif emitted_extent.get("document_completeness", "not_asserted") != "not_asserted":
        errors.append("emitted_extent_overclaims_document_completeness")
    checks = citation.get("witness_checks")
    if not isinstance(checks, dict):
        return [*errors, "missing_necessary_witness_checks"]
    # The compact v2 projection omits false flags and redundant input_text
    # basis. Its explicit final scope and measured length remain mandatory.
    if (
        not isinstance(checks.get("policy_signature"), str)
        or checks.get("policy_signature")
        not in {CHECKS_POLICY, "query-necessary-evidence-checks-v2"}
        or checks.get("basis", "input_text") != "input_text"
    ):
        errors.append("unknown_necessary_witness_policy")
    if (
        checks.get("interpretation")
        != "necessary_conditions_only_not_answer_entailment_or_authority"
    ):
        errors.append("necessary_checks_claim_entailment_or_authority")
    if (
        checks.get("recomputed_for") != "emitted_excerpt"
        or checks.get("inspected_scope") != "emitted_excerpt_only"
    ):
        errors.append("necessary_checks_do_not_bind_final_scope")
    if (
        type(checks.get("evaluated_chars")) is not int
        or checks.get("evaluated_chars") != len(excerpt)
        or checks.get("evaluation_truncated", False) is not False
        or checks.get("query_truncated", False) is not False
        or len(excerpt) > 32768
        or len(query) > 4096
    ):
        errors.append("necessary_checks_not_complete_over_final_excerpt")
    required, missing = expected_necessary_checks(query, excerpt)
    scoped_counter: set[str] = set()
    scoped_disposition = None
    if checks.get("policy_signature") == "query-necessary-evidence-checks-v2":
        if __package__:
            from .knowledge_scoped_observation_metrics import validate_scoped_checks
        else:
            from knowledge_scoped_observation_metrics import validate_scoped_checks
        raw_roles = citation.get("role_counterevidence", [])
        verified_legacy_counter = (
            isinstance(raw_roles, list)
            and any(not _role_errors(query, excerpt, witness) for witness in raw_roles)
        ) or (
            "asserted_torque_to_damage_causal_link" in required
            and bool(re.search(r"\bno durante el apriete\b", _fold(excerpt)))
        )
        required, missing, scoped_counter, scoped_disposition, scoped_errors = (
            validate_scoped_checks(
                query,
                excerpt,
                checks,
                required,
                missing,
                verified_legacy_counter=verified_legacy_counter,
            )
        )
        errors.extend(scoped_errors)
    declared = [checks.get("required_witnesses"), checks.get("missing_necessary_witnesses")]
    if any(
        not isinstance(values, list)
        or any(not isinstance(value, str) for value in values)
        or len(values) != len(set(values))
        for values in declared
    ):
        return [*errors, "invalid_necessary_witness_lists"]
    if set(declared[0]) != required or set(declared[1]) != missing:
        errors.append("necessary_witness_claim_not_reproducible_from_final_text")
    expected_status = (
        "missing" if missing else "necessary_checks_not_failed" if required else "not_assessed"
    )
    if checks.get("status") != expected_status:
        errors.append("necessary_witness_status_mismatch")
    role_witnesses = citation.get("role_counterevidence", [])
    if not isinstance(role_witnesses, list):
        errors.append("invalid_role_counterevidence")
        role_witnesses = []
    for witness in role_witnesses:
        errors.extend(_role_errors(query, excerpt, witness))
    counter = checks.get("counterevidence", [])
    if not isinstance(counter, list) or any(not isinstance(reason, str) for reason in counter):
        errors.append("invalid_necessary_counterevidence")
        counter = []
    for reason in counter:
        if reason in scoped_counter:
            continue
        if (
            reason != "source_explicitly_places_incident_outside_tightening"
            or not re.search(r"\bno durante el apriete\b", _fold(excerpt))
            or "asserted_torque_to_damage_causal_link" not in required
        ):
            errors.append("necessary_counterevidence_not_verified")
    if not scoped_counter.issubset(counter):
        errors.append("scoped_counterevidence_not_reported")
    disposition = citation.get("evidence_disposition")
    if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
        errors.append("unknown_evidence_disposition")
    elif disposition == "contradictory" and not (role_witnesses or counter):
        errors.append("contradictory_without_verified_counterwitness")
    elif disposition == "related_only" and not (required and missing):
        errors.append("related_only_without_verified_missing_requirement")
    elif disposition == "evidence_candidate" and (missing or role_witnesses or counter):
        errors.append("evidence_candidate_with_missing_or_counterwitness")
    if scoped_disposition in {"related_evidence_only", "contradictory_evidence"}:
        expected_disposition = {
            "related_evidence_only": "related_only",
            "contradictory_evidence": "contradictory",
        }[scoped_disposition]
        if disposition != expected_disposition:
            errors.append("citation_disposition_disagrees_with_scoped_evidence")
    return sorted(set(errors))


def score_context_v2(
    query: dict[str, Any],
    payload: dict[str, Any],
    entries_by_path: dict[str, dict[str, Any]],
    search_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score typed presentation separately; never reinterpret the v1 baseline."""
    result: dict[str, Any] = {
        "query_id": query["query_id"],
        "kind": query["kind"],
        "schema": METRIC_SCHEMA,
        "legacy_negative_context_selections": 0,
        "unsupported_sufficient_evidence": 0,
        "verified_related_material": 0,
        "unknown_disposition_citations": 0,
        "positive_sufficient_proven": False,
        "positive_sufficiency_unknown": query["kind"] == "positive",
        "contract_errors": [],
        "citations": [],
    }
    if (
        payload.get("schema") != "neocortex.context-response/v2"
        or payload.get("response_version") != 2
        or payload.get("read_only") is not True
    ):
        result["contract_errors"].append("unrecognized_context_v2_contract")
        return result
    if not isinstance(payload.get("query"), str) or _normalize(payload["query"]) != _normalize(
        query["text"]
    ):
        result["contract_errors"].append("response_query_mismatch")
    sources = payload.get("sources", [])
    citations = payload.get("citations", [])
    if not isinstance(sources, list) or not isinstance(citations, list):
        result["contract_errors"].append("invalid_sources_or_citations")
        return result
    source_map = {
        source["source_id"]: source
        for source in sources
        if isinstance(source, dict)
        and isinstance(source.get("source_id"), str)
        and source["source_id"]
    }
    if len(source_map) != len(sources):
        result["contract_errors"].append("ambiguous_source_identifiers")
    citation_ids = [
        citation["citation_id"]
        for citation in citations
        if isinstance(citation, dict) and isinstance(citation.get("citation_id"), str)
    ]
    if len(set(citation_ids)) != len(citations) or any(not value for value in citation_ids):
        result["contract_errors"].append("ambiguous_citation_identifiers")
    if query["kind"] == "negative":
        result["legacy_negative_context_selections"] = len(citations)

    def reference_key(
        path: object, resource_id: object, revision_id: object, evidence_id: object
    ) -> tuple[str, ...] | None:
        values = (path, resource_id, revision_id, evidence_id)
        return tuple(values) if all(isinstance(value, str) and value for value in values) else None

    references = set()
    for hit in search_hits:
        resource_ref, revision = hit.get("resource", {}), hit.get("revision", {})
        evidences = [hit.get("evidence", {})] + [
            signal["evidence"]
            for signal in hit.get("signals", [])
            if isinstance(signal.get("evidence"), dict)
        ]
        for evidence in evidences:
            key = reference_key(
                resource_ref.get("current_path"),
                resource_ref.get("resource_id"),
                revision.get("revision_id"),
                evidence.get("evidence_id"),
            )
            if key is not None:
                references.add(key)
    for citation in citations:
        if not isinstance(citation, dict):
            result["contract_errors"].append("invalid_citation_object")
            continue
        source_id = citation.get("source_id")
        source = source_map.get(source_id) if isinstance(source_id, str) else None
        entry = (
            entries_by_path.get(source.get("path"))
            if isinstance(source, dict) and isinstance(source.get("path"), str)
            else None
        )
        errors = (
            ["citation_does_not_resolve_to_frozen_source"]
            if entry is None
            else _citation_errors(query["text"], citation, source, entry)
        )
        key = (
            reference_key(
                source.get("path"),
                source.get("resource_id"),
                source.get("revision_id"),
                citation.get("evidence_id"),
            )
            if source is not None
            else None
        )
        if key is None or key not in references:
            errors.append("citation_not_pinned_to_captured_retrieval")
        disposition = citation.get("evidence_disposition")
        if errors:
            result["unknown_disposition_citations"] += 1
        if query["kind"] == "negative":
            if errors or disposition == "evidence_candidate":
                result["unsupported_sufficient_evidence"] += 1
            else:
                result["verified_related_material"] += 1
        elif not errors and disposition == "evidence_candidate" and entry is not None:
            body = _normalize(entry["source_text"].partition("\n\n")[2])
            gold = query["relevance"].get(entry["logical_resource_id"], 0) == 3
            full = (
                citation.get("fragment_state") == "full"
                and body
                and body in _source_excerpt(citation)
            )
            if gold and full:
                result["positive_sufficient_proven"] = True
                result["positive_sufficiency_unknown"] = False
        result["citations"].append(
            {
                "citation_id": citation.get("citation_id"),
                "evidence_disposition": disposition,
                "errors": errors,
            }
        )
    return result


def aggregate_context_v2(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": METRIC_SCHEMA,
        "queries": len(rows),
        "positive_queries": sum(row["kind"] == "positive" for row in rows),
        "negative_queries": sum(row["kind"] == "negative" for row in rows),
        "legacy_negative_context_selections": sum(
            row["legacy_negative_context_selections"] for row in rows
        ),
        "unsupported_sufficient_evidence": sum(
            row["unsupported_sufficient_evidence"] for row in rows
        ),
        "verified_related_material": sum(row["verified_related_material"] for row in rows),
        "unknown_disposition_citations": sum(row["unknown_disposition_citations"] for row in rows),
        "positive_sufficient_proven_queries": sum(
            row["positive_sufficient_proven"] for row in rows
        ),
        "positive_sufficiency_unknown_queries": sum(
            row["positive_sufficiency_unknown"] for row in rows
        ),
        "contract_invalid_queries": sum(bool(row["contract_errors"]) for row in rows),
    }


def acceptance_v2(
    candidate_retrieval: dict[str, Any],
    candidate_presentation: dict[str, Any],
    baseline_retrieval: dict[str, Any],
) -> dict[str, bool]:
    """Apply the explicit sufficient-evidence plan without rewriting v1 counts.

    A tiny excerpt that might answer a query remains UNKNOWN unless the frozen
    whole-document judgment can establish its sufficient factual body. This
    function never converts that uncertainty or all-abstention into a pass.
    """
    return {
        "success_at_5": candidate_retrieval["success_at_5"] >= 0.90,
        "all_eight_reserved_positives": candidate_retrieval["positive_queries"] == 8
        and candidate_retrieval["positive_successes_at_5"] == 8,
        "ndcg_not_below_baseline": candidate_retrieval["ndcg_at_10"] + 1e-12
        >= baseline_retrieval["ndcg_at_10"],
        "unsupported_sufficient_evidence_zero": candidate_presentation[
            "unsupported_sufficient_evidence"
        ]
        == 0,
        "all_positive_factual_bodies_proven": candidate_presentation["positive_queries"] == 8
        and candidate_presentation["positive_sufficient_proven_queries"] == 8,
        "no_unknown_dispositions_or_sufficiency": candidate_presentation[
            "unknown_disposition_citations"
        ]
        == 0
        and candidate_presentation["positive_sufficiency_unknown_queries"] == 0,
        "v2_contract_verified": candidate_presentation["schema"] == METRIC_SCHEMA
        and candidate_presentation["contract_invalid_queries"] == 0
        and candidate_presentation["queries"] == candidate_retrieval["queries"],
        "legacy_counts_preserved": "legacy_negative_context_selections" in candidate_presentation
        and "negative_unsupported_evidence" in candidate_retrieval
        and "negative_unsupported_evidence" in baseline_retrieval,
        "locator_integrity": candidate_retrieval["locator_integrity"] == 1.0,
        "citation_integrity": candidate_retrieval["citation_invalid_queries"] == 0
        and candidate_retrieval["citation_checks"] > 0,
        "execution_complete": candidate_retrieval["execution_invalid_queries"] == 0,
        "real_model_used": candidate_retrieval["real_vector_queries"]
        == candidate_retrieval["queries"],
    }
