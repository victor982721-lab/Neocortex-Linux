"""Pure, deterministic context construction from a Knowledge search result.

The builder performs no retrieval, database access or model invocation.  It
selects already-resolved evidence, emits unambiguous citation targets, and
honours a hard Unicode-codepoint budget without silently cutting citations.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .knowledge_contracts import (
    ContextBudget,
    ContextBundle,
    ContextContradictionRef,
    ContextEntityRef,
    ContextGraphBudget,
    ContextPlanRef,
    ContextPlanStepRef,
    ContextRelationRef,
    EvidenceMethod,
    EvidenceRef,
    KnowledgeCompleteness,
    KnowledgeHit,
    SnapshotConsistency,
)
from .semantic_models import canonical_json, fingerprint_text

if TYPE_CHECKING:
    from .knowledge_search import KnowledgeSearchResult


# region [01] Public limits and deterministic token estimate


DEFAULT_CONTEXT_CHARACTER_LIMIT = 12_000
DEFAULT_CONTEXT_MAX_HITS = 12
MAX_CONTEXT_CHARACTER_LIMIT = 1_000_000
MAX_CONTEXT_HITS = 100
MAX_CONTEXT_INPUT_HITS = 2_000
MAX_CONTEXT_NOTICES = 64
MAX_NOTICE_CHARACTERS = 512
TOKEN_ESTIMATOR_SIGNATURE = "unicode-codepoints-ceil-div4-v1"
TRUNCATION_MARKER = "…[truncated]"
_TRUST_BOUNDARY_MARKER = (
    'trust_boundary={"signature":"untrusted-corpus-data-v1",'
    '"content_class":"recovered_corpus_evidence","trust":"untrusted",'
    '"instruction_authority":false,"tools_authorized":false,'
    '"actions_authorized":false}'
)


def estimate_context_tokens(rendered_context: str) -> int:
    """Return the documented deterministic approximation ``ceil(chars / 4)``."""

    return (len(rendered_context) + 3) // 4


# endregion [01]


# region [02] Stable citation rendering


@dataclass(frozen=True, slots=True)
class _ContextEntry:
    citation_id: str
    hit: KnowledgeHit
    normalized_snippet: str | None
    snippet_state: str
    rendered_snippet: str


@dataclass(frozen=True, slots=True)
class _ContextState:
    completeness: KnowledgeCompleteness
    contradictions: tuple[ContextContradictionRef, ...]
    missing_information: tuple[str, ...]
    warnings: tuple[str, ...]
    omitted_candidates: int


type _EntityKey = tuple[str, str, str]
type _RelationKey = tuple[
    _EntityKey,
    _EntityKey,
    str,
    EvidenceMethod,
    tuple[str, ...],
    float | None,
]


@dataclass(slots=True)
class _EntityAccumulator:
    entity_kind: str
    label: str
    evidence_ids: list[str]
    resource_ids: list[str]


@dataclass(slots=True)
class _RelationAccumulator:
    source_key: _EntityKey
    target_key: _EntityKey
    relation_kind: str
    method: EvidenceMethod
    provenance: tuple[str, ...]
    confidence: float | None
    evidence_ids: list[str]


@dataclass(slots=True)
class _ContextGraphAccumulator:
    entities: dict[_EntityKey, _EntityAccumulator] = field(default_factory=dict)
    relations: dict[_RelationKey, _RelationAccumulator] = field(default_factory=dict)

    def add_entity(
        self,
        *,
        entity_kind: str,
        label: str,
        evidence_id: str,
        resource_id: str,
    ) -> _EntityKey:
        key = (entity_kind, label, resource_id)
        entity = self.entities.get(key)
        if entity is None:
            entity = _EntityAccumulator(entity_kind, label, [], [])
            self.entities[key] = entity
        _append_unique(entity.evidence_ids, evidence_id)
        _append_unique(entity.resource_ids, resource_id)
        return key

    def add_relation(
        self,
        *,
        source_key: _EntityKey,
        target_key: _EntityKey,
        relation_kind: str,
        method: EvidenceMethod,
        provenance: tuple[str, ...],
        confidence: float | None,
        evidence_id: str,
    ) -> None:
        if source_key == target_key:
            return
        key = (
            source_key,
            target_key,
            relation_kind,
            method,
            provenance,
            confidence,
        )
        relation = self.relations.get(key)
        if relation is None:
            relation = _RelationAccumulator(
                source_key=source_key,
                target_key=target_key,
                relation_kind=relation_kind,
                method=method,
                provenance=provenance,
                confidence=confidence,
                evidence_ids=[],
            )
            self.relations[key] = relation
        _append_unique(relation.evidence_ids, evidence_id)


@dataclass(frozen=True, slots=True)
class _CodeRelationIdentifiers:
    family: str
    relation_id: str
    relation_kind: str
    relation_name: str
    source_resource: str
    target_resource: str
    resolved: str
    confirmed: str
    confidence: str
    provenance: str


@dataclass(frozen=True, slots=True)
class _ValidatedCodeRelation:
    source_resource: str
    target_resource: str
    relation_kind: str
    method: EvidenceMethod
    provenance: tuple[str, ...]
    confidence: float


_CODE_RELATION_SOURCE_TABLES = {
    "reference": "code_references",
    "dependency": "dependencies",
}
_CODE_RELATION_OPTIONAL_PROVENANCE = (
    "code_relation_scope",
    "code_relation_version_spec",
)


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _normalize_snippet(snippet: str | None) -> str | None:
    if snippet is None:
        return None
    normalized = " ".join(snippet.split())
    return normalized or None


def _clip_visible(value: str, limit: int = MAX_NOTICE_CHARACTERS) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _locator_payload(evidence: EvidenceRef) -> dict[str, object]:
    locator: dict[str, object] = {}
    for name in (
        "page",
        "start_line",
        "end_line",
        "sheet",
        "cell_range",
        "start_ms",
        "end_ms",
        "coordinate_space",
        "start_char",
        "end_char",
        "symbol",
        "section_kind",
        "section_id",
        "generation",
    ):
        value = getattr(evidence, name)
        if value is not None:
            locator[name] = value
    if evidence.bounding_box is not None:
        locator["bounding_box"] = list(evidence.bounding_box)
    if evidence.identifiers:
        locator["identifiers"] = [
            {"namespace": namespace, "value": value}
            for namespace, value in evidence.identifiers
        ]
    return locator


def _citation_target(hit: KnowledgeHit) -> dict[str, object]:
    provenance: dict[str, object] = {
        "evidence_method": hit.evidence.method.value,
    }
    for name in ("extractor", "extractor_version", "generation"):
        value = getattr(hit.evidence, name)
        if value is not None:
            provenance[name] = value
    target: dict[str, object] = {
        "evidence_id": hit.evidence.evidence_id,
        "locator": _locator_payload(hit.evidence),
        "owner": hit.resource.owner,
        "processing_signature": hit.revision.processing_signature,
        "provenance": provenance,
        "resource_id": hit.resource.resource_id,
        "revision_state": hit.revision.state.value,
        "revision_id": hit.revision.revision_id,
        "source_kind": hit.resource.source_kind,
    }
    if hit.resource.current_path is not None:
        target["current_path"] = hit.resource.current_path
    if hit.resource.disposition is not None:
        target["resource_disposition"] = hit.resource.disposition.value
    if hit.revision.generation is not None:
        target["revision_generation"] = hit.revision.generation
    if hit.revision.observed_at_utc is not None:
        target["revision_observed_at_utc"] = hit.revision.observed_at_utc
    return target


def _render_entry(entry: _ContextEntry) -> str:
    reason_payload: dict[str, object] = {
        "reasons": list(entry.hit.reasons),
        "retrieval_rank": entry.hit.rank,
    }
    return "\n".join(
        (
            f"[{entry.citation_id}] target="
            f"{canonical_json(_citation_target(entry.hit))}",
            f"why={canonical_json(reason_payload)}",
            f"snippet={entry.rendered_snippet}",
        )
    )


def _new_entry(citation_id: str, hit: KnowledgeHit) -> _ContextEntry:
    snippet = _normalize_snippet(hit.evidence.snippet)
    if snippet is None:
        return _ContextEntry(
            citation_id=citation_id,
            hit=hit,
            normalized_snippet=None,
            snippet_state="unavailable",
            rendered_snippet="[not available from owner]",
        )
    return _ContextEntry(
        citation_id=citation_id,
        hit=hit,
        normalized_snippet=snippet,
        snippet_state="budget_omitted",
        rendered_snippet="[omitted: character budget]",
    )


def _ordered_unique_hits(
    hits: tuple[KnowledgeHit, ...],
) -> tuple[tuple[KnowledgeHit, ...], int]:
    bounded = hits[:MAX_CONTEXT_INPUT_HITS]
    ordered = sorted(
        bounded,
        key=lambda hit: (
            hit.rank,
            hit.resource.resource_id,
            hit.revision.revision_id,
            hit.evidence.evidence_id,
        ),
    )
    result: list[KnowledgeHit] = []
    seen: set[str] = set()
    for hit in ordered:
        key = hit.evidence.evidence_id
        if key in seen:
            continue
        seen.add(key)
        result.append(hit)
    omitted_as_duplicate_or_overflow = len(hits) - len(result)
    return tuple(result), omitted_as_duplicate_or_overflow


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _stable_graph_id(prefix: str, identity: dict[str, object]) -> str:
    fingerprint = fingerprint_text(canonical_json(identity))
    return (
        f"{prefix}-v1:{fingerprint.xxh3_128}:{fingerprint.byte_count}:"
        f"{fingerprint.xxh3_64_guard}"
    )


def _identifiers_by_namespace(
    identifiers: tuple[tuple[str, str], ...],
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for namespace, value in identifiers:
        grouped.setdefault(namespace.casefold(), []).append(value)
    return grouped


def _single_identifier(
    identifiers_by_namespace: dict[str, list[str]],
    namespace: str,
) -> str | None:
    values = identifiers_by_namespace.get(namespace)
    if values is None or len(values) != 1:
        return None
    return values[0]


def _required_code_relation_identifiers(
    identifiers_by_namespace: dict[str, list[str]],
) -> _CodeRelationIdentifiers | None:
    family = _single_identifier(identifiers_by_namespace, "code_relation_family")
    relation_id = _single_identifier(identifiers_by_namespace, "code_relation_id")
    relation_kind = _single_identifier(identifiers_by_namespace, "code_relation_kind")
    relation_name = _single_identifier(identifiers_by_namespace, "code_relation_name")
    source_resource = _single_identifier(
        identifiers_by_namespace,
        "code_relation_source_resource",
    )
    target_resource = _single_identifier(
        identifiers_by_namespace,
        "code_relation_target_resource",
    )
    resolved = _single_identifier(identifiers_by_namespace, "code_relation_resolved")
    confirmed = _single_identifier(
        identifiers_by_namespace,
        "code_relation_confirmed",
    )
    confidence = _single_identifier(
        identifiers_by_namespace,
        "code_relation_confidence",
    )
    provenance = _single_identifier(
        identifiers_by_namespace,
        "code_relation_provenance",
    )
    if (
        family is None
        or relation_id is None
        or relation_kind is None
        or relation_name is None
        or source_resource is None
        or target_resource is None
        or resolved is None
        or confirmed is None
        or confidence is None
        or provenance is None
    ):
        return None
    return _CodeRelationIdentifiers(
        family=family,
        relation_id=relation_id,
        relation_kind=relation_kind,
        relation_name=relation_name,
        source_resource=source_resource,
        target_resource=target_resource,
        resolved=resolved,
        confirmed=confirmed,
        confidence=confidence,
        provenance=provenance,
    )


def _canonical_code_relation_source(section_id: str | None) -> tuple[str, str] | None:
    if section_id is None:
        return None
    source_table, separator, source_row_id = section_id.partition(":")
    if separator != ":" or not source_row_id.isdecimal():
        return None
    if source_row_id != str(int(source_row_id)) or int(source_row_id) < 1:
        return None
    return source_table, source_row_id


def _parse_code_relation_confirmation(raw_value: str) -> bool | None:
    normalized = raw_value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _parse_code_relation_confidence(raw_value: str) -> float | None:
    try:
        confidence = float(raw_value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _validated_code_relation(hit: KnowledgeHit) -> _ValidatedCodeRelation | None:
    identifiers_by_namespace = _identifiers_by_namespace(hit.evidence.identifiers)
    identifiers = _required_code_relation_identifiers(identifiers_by_namespace)
    source = _canonical_code_relation_source(hit.evidence.section_id)
    if identifiers is None or source is None:
        return None
    confirmed = _parse_code_relation_confirmation(identifiers.confirmed)
    confidence = _parse_code_relation_confidence(identifiers.confidence)
    if confirmed is None or confidence is None:
        return None
    source_table, source_row_id = source
    method = EvidenceMethod.STRUCTURAL if confirmed is True else EvidenceMethod.INFERRED
    expected_source_table = _CODE_RELATION_SOURCE_TABLES.get(identifiers.family)
    consistent = (
        identifiers.relation_id == hit.evidence.section_id,
        identifiers.source_resource == hit.resource.resource_id,
        identifiers.target_resource != identifiers.source_resource,
        identifiers.resolved.casefold() == "true",
        source_table == expected_source_table,
        hit.evidence.method is method,
    )
    if not all(consistent):
        return None
    provenance = [
        f"code:{source_table}:{source_row_id}",
        f"analyzer:{identifiers.provenance}",
        f"name:{identifiers.relation_name}",
    ]
    for namespace in _CODE_RELATION_OPTIONAL_PROVENANCE:
        optional_value = _single_identifier(identifiers_by_namespace, namespace)
        if optional_value is not None:
            provenance.append(f"{namespace}:{optional_value}")
    return _ValidatedCodeRelation(
        source_resource=identifiers.source_resource,
        target_resource=identifiers.target_resource,
        relation_kind=f"code_{identifiers.family}:{identifiers.relation_kind}",
        method=method,
        provenance=tuple(provenance),
        confidence=confidence,
    )


def _accumulate_code_relation(
    graph: _ContextGraphAccumulator,
    hit: KnowledgeHit,
) -> None:
    relation = _validated_code_relation(hit)
    if relation is None:
        return
    evidence_id = hit.evidence.evidence_id
    resource_id = hit.resource.resource_id
    source_key = graph.add_entity(
        entity_kind="resource",
        label=relation.source_resource,
        evidence_id=evidence_id,
        resource_id=resource_id,
    )
    target_key = graph.add_entity(
        entity_kind="resource_reference",
        label=relation.target_resource,
        evidence_id=evidence_id,
        resource_id=relation.target_resource,
    )
    graph.add_relation(
        source_key=source_key,
        target_key=target_key,
        relation_kind=relation.relation_kind,
        method=relation.method,
        provenance=relation.provenance,
        confidence=relation.confidence,
        evidence_id=evidence_id,
    )


def _accumulate_entry_identifiers(
    graph: _ContextGraphAccumulator,
    hit: KnowledgeHit,
) -> None:
    evidence_id = hit.evidence.evidence_id
    resource_id = hit.resource.resource_id
    for namespace, value in hit.evidence.identifiers:
        normalized_namespace = namespace.casefold()
        if (
            hit.evidence.section_kind == "code_relation"
            and normalized_namespace.startswith("code_relation_")
        ):
            continue
        if normalized_namespace != "planned_duplicate_of":
            graph.add_entity(
                entity_kind=f"identifier:{namespace}",
                label=value,
                evidence_id=evidence_id,
                resource_id=resource_id,
            )
            continue
        source_key = graph.add_entity(
            entity_kind="resource",
            label=resource_id,
            evidence_id=evidence_id,
            resource_id=resource_id,
        )
        target_key = graph.add_entity(
            entity_kind="resource_reference",
            label=value,
            evidence_id=evidence_id,
            resource_id=value,
        )
        graph.add_relation(
            source_key=source_key,
            target_key=target_key,
            relation_kind="planned_duplicate_of",
            method=EvidenceMethod.AMBIGUOUS,
            provenance=("inventory:planned_duplicate_plan",),
            confidence=None,
            evidence_id=evidence_id,
        )


def _accumulate_context_entry(
    graph: _ContextGraphAccumulator,
    entry: _ContextEntry,
) -> None:
    hit = entry.hit
    if hit.evidence.symbol is not None:
        graph.add_entity(
            entity_kind="code_symbol",
            label=hit.evidence.symbol,
            evidence_id=hit.evidence.evidence_id,
            resource_id=hit.resource.resource_id,
        )
    if hit.evidence.section_kind == "code_relation":
        _accumulate_code_relation(graph, hit)
    _accumulate_entry_identifiers(graph, hit)


def _materialize_context_entities(
    entities: dict[_EntityKey, _EntityAccumulator],
) -> tuple[dict[_EntityKey, str], tuple[ContextEntityRef, ...]]:
    entity_ids: dict[_EntityKey, str] = {}
    entity_refs: list[ContextEntityRef] = []
    for key, entity in entities.items():
        entity_id = _stable_graph_id(
            "context-entity",
            {
                "entity_kind": entity.entity_kind,
                "label": entity.label,
                "resource_ids": sorted(entity.resource_ids),
            },
        )
        entity_ids[key] = entity_id
        entity_refs.append(
            ContextEntityRef(
                entity_id=entity_id,
                entity_kind=entity.entity_kind,
                label=entity.label,
                evidence_ids=tuple(entity.evidence_ids),
                resource_ids=tuple(entity.resource_ids),
            )
        )
    return entity_ids, tuple(entity_refs)


def _materialize_context_relations(
    relations: dict[_RelationKey, _RelationAccumulator],
    entity_ids: dict[_EntityKey, str],
) -> tuple[ContextRelationRef, ...]:
    relation_refs: list[ContextRelationRef] = []
    for relation in relations.values():
        source_entity_id = entity_ids[relation.source_key]
        target_entity_id = entity_ids[relation.target_key]
        relation_id = _stable_graph_id(
            "context-relation",
            {
                "relation_kind": relation.relation_kind,
                "method": relation.method.value,
                "provenance": list(relation.provenance),
                "confidence": relation.confidence,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
            },
        )
        relation_refs.append(
            ContextRelationRef(
                relation_id=relation_id,
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
                relation_kind=relation.relation_kind,
                method=relation.method,
                provenance=relation.provenance,
                evidence_ids=tuple(relation.evidence_ids),
                confidence=relation.confidence,
            )
        )
    return tuple(relation_refs)


def _derive_context_graph(
    entries: tuple[_ContextEntry, ...],
) -> tuple[tuple[ContextEntityRef, ...], tuple[ContextRelationRef, ...]]:
    graph = _ContextGraphAccumulator()
    for entry in entries:
        _accumulate_context_entry(graph, entry)
    entity_ids, entity_refs = _materialize_context_entities(graph.entities)
    relation_refs = _materialize_context_relations(graph.relations, entity_ids)
    return entity_refs, relation_refs


# endregion [02]


# region [03] Evidence-backed contradictions and completeness


def _contradictions(
    entries: tuple[_ContextEntry, ...],
) -> tuple[ContextContradictionRef, ...]:
    claims: dict[str, dict[str, tuple[str, set[str]]]] = {}
    citation_order = {entry.citation_id: index for index, entry in enumerate(entries)}
    for entry in entries:
        for namespace, raw_value in entry.hit.evidence.identifiers:
            prefix, separator, topic = namespace.partition(":")
            if separator != ":" or prefix.casefold() != "claim":
                continue
            normalized_topic = topic.strip()
            normalized_value = raw_value.strip()
            if not normalized_topic or not normalized_value:
                continue
            topic_key = normalized_topic.casefold()
            value_key = normalized_value.casefold()
            topic_claims = claims.setdefault(topic_key, {})
            display, citations = topic_claims.setdefault(
                value_key,
                (normalized_value, set()),
            )
            citations.add(entry.citation_id)
            topic_claims[value_key] = (display, citations)

    result: list[ContextContradictionRef] = []
    for topic_key in sorted(claims):
        values = claims[topic_key]
        if len(values) < 2:
            continue
        cited = {
            citation_id
            for _display, citation_ids in values.values()
            for citation_id in citation_ids
        }
        if len(cited) < 2:
            continue
        ordered_citations = tuple(
            sorted(cited, key=lambda citation_id: citation_order[citation_id])
        )
        displayed_values = tuple(
            sorted(
                (display for display, _citations in values.values()),
                key=str.casefold,
            )
        )
        result.append(
            ContextContradictionRef.create(
                contradiction_kind="conflicting_structured_claim",
                topic=topic_key,
                values=displayed_values,
                citation_ids=ordered_citations,
            )
        )
    return tuple(result)


def _bounded_unique_notices(*groups: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for group in groups:
        for notice in group:
            normalized = _clip_visible(notice)
            if not normalized or normalized in seen:
                continue
            if len(result) == MAX_CONTEXT_NOTICES:
                omitted += 1
                continue
            seen.add(normalized)
            result.append(normalized)
    if omitted:
        marker = f"{omitted} additional context warning(s) omitted by bound."
        if len(result) == MAX_CONTEXT_NOTICES:
            result[-1] = marker
        else:
            result.append(marker)
    return tuple(result)


def _context_state(
    result: KnowledgeSearchResult,
    entries: tuple[_ContextEntry, ...],
    *,
    duplicate_or_overflow_hits: int,
) -> _ContextState:
    contradictions = _contradictions(entries)
    source_hit_count = len(result.hits)
    context_omitted = max(0, source_hit_count - len(entries))
    # The planned top-k presentation window is complete for its requested
    # scope.  Only omissions caused by real upstream truncation make context
    # evidence incomplete.
    search_omitted = max(
        0,
        result.omitted_candidates - result.window_omitted_candidates,
    )
    available_execution = any(
        ranking.executed and ranking.available for ranking in result.rankings
    )
    missing: list[str] = []

    if source_hit_count == 0:
        if result.rankings and not available_execution:
            completeness = KnowledgeCompleteness.UNSUPPORTED
            missing.append("No retrieval owner was available for this query.")
        elif (
            result.complete
            and result.snapshot.consistency is SnapshotConsistency.STABLE
            and not result.truncated
            and not search_omitted
        ):
            completeness = KnowledgeCompleteness.NO_EVIDENCE
            missing.append("No evidence matched the query in the captured snapshot.")
        else:
            completeness = KnowledgeCompleteness.PARTIAL
            missing.append(
                "Retrieval was incomplete; missing evidence cannot be ruled out."
            )
    else:
        completeness = KnowledgeCompleteness.COMPLETE
        if not result.complete:
            missing.append(
                "One or more retrieval rankings were incomplete or unavailable."
            )
        if not entries:
            missing.append(
                "No exact citation target fit within the context character budget."
            )

    if result.snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED:
        missing.append("The owner snapshot changed during retrieval.")
    if result.truncated:
        missing.append("Search candidates were truncated before context construction.")
    if search_omitted:
        missing.append(
            f"Search omitted {search_omitted} candidate(s) before context construction."
        )
    if context_omitted:
        missing.append(
            f"Context omitted {context_omitted} retrieved hit(s) because of its bounds."
        )

    unavailable_snippets = sum(
        entry.snippet_state == "unavailable" for entry in entries
    )
    budget_omitted_snippets = sum(
        entry.snippet_state == "budget_omitted" for entry in entries
    )
    truncated_snippets = sum(entry.snippet_state == "truncated" for entry in entries)
    if unavailable_snippets:
        missing.append(
            f"{unavailable_snippets} selected evidence item(s) had no textual snippet."
        )
    if budget_omitted_snippets:
        missing.append(
            f"{budget_omitted_snippets} evidence snippet(s) were omitted by the "
            "character budget."
        )
    if truncated_snippets:
        missing.append(
            f"{truncated_snippets} evidence snippet(s) were visibly truncated."
        )
    if contradictions:
        missing.append("Contradictory structured claims require resolution.")

    if source_hit_count and (
        not result.complete
        or result.snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
        or result.truncated
        or search_omitted
        or context_omitted
        or unavailable_snippets
        or budget_omitted_snippets
        or truncated_snippets
        or contradictions
    ):
        completeness = KnowledgeCompleteness.PARTIAL

    selected_hit_warnings = tuple(
        warning for entry in entries for warning in entry.hit.warnings
    )
    internal_warnings: tuple[str, ...] = ()
    if duplicate_or_overflow_hits:
        internal_warnings = (
            f"Context ignored {duplicate_or_overflow_hits} duplicate or "
            "out-of-bound hit(s).",
        )
    warnings = _bounded_unique_notices(
        result.warnings,
        result.snapshot.warnings,
        result.plan.notices,
        selected_hit_warnings,
        internal_warnings,
    )
    return _ContextState(
        completeness=completeness,
        contradictions=contradictions,
        missing_information=tuple(missing),
        warnings=warnings,
        omitted_candidates=search_omitted + context_omitted,
    )


# endregion [03]


# region [04] Hard-budget assembly


def _context_plan_ref(result: KnowledgeSearchResult) -> ContextPlanRef:
    plan = result.plan
    return ContextPlanRef(
        plan_id=plan.plan_id,
        normalized_query=plan.normalized_query,
        retrieval_mode=plan.retrieval_mode.value,
        intents=plan.intents,
        exact_terms=plan.exact_terms,
        source_kinds=plan.source_kinds,
        formats=plan.formats,
        project=plan.project,
        date_from=plan.date_from,
        date_to=plan.date_to,
        include_history=plan.include_history,
        limit=plan.limit,
        max_per_resource=plan.max_per_resource,
        min_section_distance=plan.min_section_distance,
        max_vectors=plan.max_vectors,
        steps=tuple(
            ContextPlanStepRef(
                channel=step.channel,
                ranking_name=step.ranking_name,
                reason=step.reason,
                candidate_limit=step.candidate_limit,
                required=step.required,
            )
            for step in plan.steps
        ),
        notices=plan.notices,
    )


def _render_header(
    result: KnowledgeSearchResult,
    plan: ContextPlanRef,
) -> str:
    rendered_intents = json.dumps(
        list(result.plan.intents),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return "\n".join(
        (
            "KNOWLEDGE CONTEXT v1",
            _TRUST_BOUNDARY_MARKER,
            f"query={_json_string(result.plan.normalized_query)}",
            f"intents={rendered_intents}",
            f"retrieval_mode={result.plan.retrieval_mode.value}",
            f"plan_id={_json_string(result.plan.plan_id)}",
            f"plan={plan.to_json()}",
            f"snapshot_id={_json_string(result.snapshot.snapshot_id)}",
        )
    )


def _render_evidence_header(result: KnowledgeSearchResult) -> str:
    """Render the minimum trust/query envelope needed before cited evidence."""

    return "\n".join(
        (
            "KNOWLEDGE CONTEXT v1",
            _TRUST_BOUNDARY_MARKER,
            f"query={_json_string(result.plan.normalized_query)}",
            f"snapshot_id={_json_string(result.snapshot.snapshot_id)}",
        )
    )


def _render_graph(
    entities: tuple[ContextEntityRef, ...],
    relations: tuple[ContextRelationRef, ...],
) -> tuple[str, ...]:
    blocks: list[str] = []
    if entities:
        blocks.append("ENTITIES\n" + "\n".join(entity.to_json() for entity in entities))
    if relations:
        blocks.append(
            "RELATIONS\n" + "\n".join(relation.to_json() for relation in relations)
        )
    return tuple(blocks)


def _render_status(state: _ContextState) -> str:
    lines = ("STATUS", f"completeness={state.completeness.value}")
    sections: list[str] = ["\n".join(lines)]
    if state.missing_information:
        sections.append(
            "MISSING INFORMATION\n"
            + "\n".join(f"- {notice}" for notice in state.missing_information)
        )
    if state.contradictions:
        sections.append(
            "CONTRADICTIONS\n"
            + "\n".join(
                f"- {contradiction.summary} [{', '.join(contradiction.citation_ids)}]"
                for contradiction in state.contradictions
            )
        )
    if state.warnings:
        sections.append(
            "WARNINGS\n" + "\n".join(f"- {warning}" for warning in state.warnings)
        )
    return "\n".join(sections)


def _render_compact_status(state: _ContextState) -> str:
    """Keep essential status after evidence when diagnostics do not fit."""

    sections = [
        "\n".join(
            (
                "STATUS",
                f"completeness={state.completeness.value}",
                "diagnostics=[omitted: character budget]",
            )
        )
    ]
    # Contradictions are evidence-bearing and ContextBundle requires them to
    # remain visible whenever their citations are selected.
    if state.contradictions:
        sections.append(
            "CONTRADICTIONS\n"
            + "\n".join(
                f"- {contradiction.summary} [{', '.join(contradiction.citation_ids)}]"
                for contradiction in state.contradictions
            )
        )
    return "\n".join(sections)


def _compose(
    result: KnowledgeSearchResult,
    entries: tuple[_ContextEntry, ...],
    *,
    plan: ContextPlanRef,
    graph: tuple[tuple[ContextEntityRef, ...], tuple[ContextRelationRef, ...]],
    duplicate_or_overflow_hits: int,
    include_diagnostics: bool = True,
) -> tuple[str, _ContextState]:
    state = _context_state(
        result,
        entries,
        duplicate_or_overflow_hits=duplicate_or_overflow_hits,
    )
    entities, relations = graph
    blocks = [
        _render_header(result, plan)
        if include_diagnostics
        else _render_evidence_header(result)
    ]
    if entries:
        blocks.append("EVIDENCE\n" + "\n\n".join(map(_render_entry, entries)))
    blocks.extend(_render_graph(entities, relations))
    blocks.append(
        _render_status(state)
        if include_diagnostics
        else _render_compact_status(state)
    )
    return "\n\n".join(blocks), state


def _visible_budget_fallback(character_limit: int) -> str:
    message = "[context omitted: character budget too small]"
    if len(message) <= character_limit:
        return message
    if character_limit <= len(TRUNCATION_MARKER):
        return "…" * character_limit
    return message[: character_limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _replace_entry(
    entries: tuple[_ContextEntry, ...],
    index: int,
    entry: _ContextEntry,
) -> tuple[_ContextEntry, ...]:
    return (*entries[:index], entry, *entries[index + 1 :])


def _try_full_or_truncated_snippet(
    result: KnowledgeSearchResult,
    entries: tuple[_ContextEntry, ...],
    index: int,
    *,
    plan: ContextPlanRef,
    graph: tuple[tuple[ContextEntityRef, ...], tuple[ContextRelationRef, ...]],
    character_limit: int,
    duplicate_or_overflow_hits: int,
    include_diagnostics: bool = True,
) -> tuple[_ContextEntry, ...]:
    entry = entries[index]
    snippet = entry.normalized_snippet
    if snippet is None:
        return entries

    full = replace(
        entry,
        snippet_state="full",
        rendered_snippet=_json_string(snippet),
    )
    proposal = _replace_entry(entries, index, full)
    rendered, _state = _compose(
        result,
        proposal,
        plan=plan,
        graph=graph,
        duplicate_or_overflow_hits=duplicate_or_overflow_hits,
        include_diagnostics=include_diagnostics,
    )
    if len(rendered) <= character_limit:
        return proposal

    low = 1
    high = max(0, len(snippet) - 1)
    best: tuple[_ContextEntry, ...] | None = None
    while low <= high:
        middle = (low + high) // 2
        visibly_truncated = snippet[:middle] + TRUNCATION_MARKER
        truncated = replace(
            entry,
            snippet_state="truncated",
            rendered_snippet=_json_string(visibly_truncated),
        )
        proposal = _replace_entry(entries, index, truncated)
        rendered, _state = _compose(
            result,
            proposal,
            plan=plan,
            graph=graph,
            duplicate_or_overflow_hits=duplicate_or_overflow_hits,
            include_diagnostics=include_diagnostics,
        )
        if len(rendered) <= character_limit:
            best = proposal
            low = middle + 1
        else:
            high = middle - 1
    return entries if best is None else best


def build_context_bundle(
    result: KnowledgeSearchResult,
    *,
    character_limit: int = DEFAULT_CONTEXT_CHARACTER_LIMIT,
    max_hits: int = DEFAULT_CONTEXT_MAX_HITS,
) -> ContextBundle:
    """Build a bounded context solely from an immutable search result.

    ``claim:<topic>`` evidence identifiers are the only contradiction signal;
    free text is never interpreted as a claim.  A contradiction is emitted
    only when at least two selected citations carry distinct structured values.
    """

    if isinstance(character_limit, bool) or not 1 <= character_limit <= (
        MAX_CONTEXT_CHARACTER_LIMIT
    ):
        raise ValueError(
            f"character_limit must be between 1 and {MAX_CONTEXT_CHARACTER_LIMIT}"
        )
    if isinstance(max_hits, bool) or not 1 <= max_hits <= MAX_CONTEXT_HITS:
        raise ValueError(f"max_hits must be between 1 and {MAX_CONTEXT_HITS}")

    plan = _context_plan_ref(result)
    ordered_hits, duplicate_or_overflow_hits = _ordered_unique_hits(result.hits)
    bounded_hits = ordered_hits[:max_hits]
    entries: tuple[_ContextEntry, ...] = ()
    graph: tuple[
        tuple[ContextEntityRef, ...],
        tuple[ContextRelationRef, ...],
    ] = ((), ())
    for hit in bounded_hits:
        proposal = (
            *entries,
            _new_entry(f"K{len(entries) + 1}", hit),
        )
        proposal_graph = _derive_context_graph(proposal)
        # Select citations against the evidence-first representation.  Full
        # plan/status diagnostics are added only after the bounded evidence is
        # secured, so they can never consume the entire budget ahead of K1.
        rendered, _state = _compose(
            result,
            proposal,
            plan=plan,
            graph=proposal_graph,
            duplicate_or_overflow_hits=duplicate_or_overflow_hits,
            include_diagnostics=False,
        )
        if len(rendered) > character_limit:
            proposal = _try_full_or_truncated_snippet(
                result,
                proposal,
                len(proposal) - 1,
                plan=plan,
                graph=proposal_graph,
                character_limit=character_limit,
                duplicate_or_overflow_hits=duplicate_or_overflow_hits,
                include_diagnostics=False,
            )
            rendered, _state = _compose(
                result,
                proposal,
                plan=plan,
                graph=proposal_graph,
                duplicate_or_overflow_hits=duplicate_or_overflow_hits,
                include_diagnostics=False,
            )
        if len(rendered) > character_limit:
            break
        entries = proposal
        graph = proposal_graph

    for index in range(len(entries)):
        entries = _try_full_or_truncated_snippet(
            result,
            entries,
            index,
            plan=plan,
            graph=graph,
            character_limit=character_limit,
            duplicate_or_overflow_hits=duplicate_or_overflow_hits,
            include_diagnostics=False,
        )

    rendered_context, state = _compose(
        result,
        entries,
        plan=plan,
        graph=graph,
        duplicate_or_overflow_hits=duplicate_or_overflow_hits,
    )
    if len(rendered_context) > character_limit:
        rendered_context, state = _compose(
            result,
            entries,
            plan=plan,
            graph=graph,
            duplicate_or_overflow_hits=duplicate_or_overflow_hits,
            include_diagnostics=False,
        )
    if len(rendered_context) > character_limit:
        entries = ()
        graph = ((), ())
        rendered_context = _visible_budget_fallback(character_limit)
        state = _context_state(
            result,
            entries,
            duplicate_or_overflow_hits=duplicate_or_overflow_hits,
        )

    truncated_evidence_ids = tuple(
        entry.hit.evidence.evidence_id
        for entry in entries
        if entry.snippet_state == "truncated"
    )
    budget = ContextBudget(
        character_limit=character_limit,
        characters_used=len(rendered_context),
        estimated_tokens=estimate_context_tokens(rendered_context),
        estimator_signature=TOKEN_ESTIMATOR_SIGNATURE,
        omitted_candidates=state.omitted_candidates,
        truncated_evidence_ids=truncated_evidence_ids,
        measurement_scope="rendered_context",
    )
    entities, relations = graph
    graph_budget = ContextGraphBudget(
        identifiers_considered=sum(
            len(entry.hit.evidence.identifiers) for entry in entries
        ),
        entities_included=len(entities),
        relations_included=len(relations),
    )
    return ContextBundle(
        normalized_query=result.plan.normalized_query,
        intents=result.plan.intents,
        plan_id=result.plan.plan_id,
        plan=plan,
        snapshot=result.snapshot,
        selected_hits=tuple(entry.hit for entry in entries),
        citation_ids=tuple(
            (entry.citation_id, entry.hit.evidence.evidence_id) for entry in entries
        ),
        graph_budget=graph_budget,
        budget=budget,
        rendered_context=rendered_context,
        completeness=state.completeness,
        entities=entities,
        relations=relations,
        contradictions=state.contradictions,
        missing_information=state.missing_information,
        warnings=state.warnings,
        blocking_owners=result.blocking_owners,
    )


build_knowledge_context = build_context_bundle


# endregion [04]


__all__ = (
    "DEFAULT_CONTEXT_CHARACTER_LIMIT",
    "DEFAULT_CONTEXT_MAX_HITS",
    "MAX_CONTEXT_CHARACTER_LIMIT",
    "MAX_CONTEXT_HITS",
    "TOKEN_ESTIMATOR_SIGNATURE",
    "build_context_bundle",
    "build_knowledge_context",
    "estimate_context_tokens",
)
