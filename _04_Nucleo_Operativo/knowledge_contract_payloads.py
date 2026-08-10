"""Deterministic payload builders for immutable Knowledge contracts.
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/knowledge_contract_payloads.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


Contract classes remain defined by ``knowledge_contracts`` for historical type
identity and pickle compatibility. Runtime imports never point back to that
facade; class names below are available only to static type checkers.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from .knowledge_contract_protocols import (
        KnowledgePhaseTiming,
        KnowledgeQueryTelemetry,
        PhysicalIdentityRef,
        ResourceRef,
        RevisionRef,
        EvidenceRef,
        RankingSignal,
        KnowledgeHit,
        PublicationHead,
        LogicalWatermark,
        ActiveModel,
        OwnerSnapshot,
        KnowledgeSnapshot,
        ContextPlanStepRef,
        ContextPlanRef,
        ContextGraphBudget,
        ContextBudget,
        ContextEntityRef,
        ContextContradictionRef,
        ContextRelationRef,
        ContextBundle,
    )

BasePayload = Callable[[str], dict[str, object]]
CanonicalJson = Callable[[Mapping[str, object]], str]


def base_payload(kind: str, *, schema_version: int) -> dict[str, object]:
    return {"schema_version": schema_version, "kind": kind}


def canonical_output(
    payload: Mapping[str, object],
    *,
    canonical_json_fn: CanonicalJson,
) -> str:
    return canonical_json_fn(payload)


def knowledge_phase_timing_payload(contract: KnowledgePhaseTiming) -> dict[str, object]:
    payload: dict[str, object] = {
        "phase": contract.phase.value,
        "duration_ns": contract.duration_ns,
        "service_attempt": contract.service_attempt,
        "executed": contract.executed,
    }
    if contract.owner is not None:
        payload["owner"] = contract.owner
    if contract.ranking_names:
        payload["ranking_names"] = list(contract.ranking_names)
    if contract.snapshot_id is not None:
        payload["snapshot_id"] = contract.snapshot_id
    return payload


def knowledge_query_telemetry_payload(
    contract: KnowledgeQueryTelemetry, *, telemetry_schema_version: int
) -> dict[str, object]:
    return {
        "schema_version": telemetry_schema_version,
        "kind": "knowledge_query_telemetry",
        "operation": contract.operation.value,
        "clock_signature": contract.clock_signature,
        "total_duration_ns": contract.total_duration_ns,
        "phases": [phase.to_dict() for phase in contract.phases],
    }


def physical_identity_ref_payload(contract: PhysicalIdentityRef) -> dict[str, object]:
    return {
        "scheme": contract.scheme,
        "value": contract.value,
        "identity_version": contract.identity_version,
    }


def resource_ref_payload(
    contract: ResourceRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("resource_ref")
    payload.update(
        {
            "resource_id": contract.resource_id,
            "source_kind": contract.source_kind,
            "owner": contract.owner,
        }
    )
    if contract.physical_identity is not None:
        payload["physical_identity"] = contract.physical_identity.to_dict()
    if contract.current_path is not None:
        payload["current_path"] = contract.current_path
    if contract.disposition is not None:
        payload["disposition"] = contract.disposition.value
    if contract.canonical_resource_id is not None:
        payload["canonical_resource_id"] = contract.canonical_resource_id
    return payload


def revision_ref_payload(
    contract: RevisionRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("revision_ref")
    payload.update(
        {
            "resource_id": contract.resource_id,
            "revision_id": contract.revision_id,
            "producer": contract.producer,
            "processing_signature": contract.processing_signature,
            "state": contract.state.value,
        }
    )
    if contract.generation is not None:
        payload["generation"] = contract.generation
    if contract.observed_at_utc is not None:
        payload["observed_at_utc"] = contract.observed_at_utc
    return payload


def evidence_ref_payload(
    contract: EvidenceRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("evidence_ref")
    payload.update(
        {
            "evidence_id": contract.evidence_id,
            "resource_id": contract.resource_id,
            "revision_id": contract.revision_id,
            "method": contract.method.value,
        }
    )
    optional: tuple[tuple[str, object | None], ...] = (
        ("page", contract.page),
        ("start_line", contract.start_line),
        ("end_line", contract.end_line),
        ("sheet", contract.sheet),
        ("cell_range", contract.cell_range),
        ("start_ms", contract.start_ms),
        ("end_ms", contract.end_ms),
        ("coordinate_space", contract.coordinate_space),
        ("start_char", contract.start_char),
        ("end_char", contract.end_char),
        ("symbol", contract.symbol),
        ("section_kind", contract.section_kind),
        ("section_id", contract.section_id),
        ("snippet", contract.snippet),
        ("extractor", contract.extractor),
        ("extractor_version", contract.extractor_version),
        ("generation", contract.generation),
    )
    for name, value in optional:
        if value is not None:
            payload[name] = value
    if contract.bounding_box is not None:
        payload["bounding_box"] = list(contract.bounding_box)
    if contract.identifiers:
        payload["identifiers"] = [
            {"namespace": namespace, "value": value}
            for namespace, value in contract.identifiers
        ]
    return payload


def ranking_signal_payload(contract: RankingSignal) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": contract.source,
        "score_kind": contract.score_kind,
        "raw_score": contract.raw_score,
        "source_rank": contract.source_rank,
    }
    if contract.model_signature is not None:
        payload["model_signature"] = contract.model_signature
    if contract.query_model_signature is not None:
        payload["query_model_signature"] = contract.query_model_signature
    if contract.generation is not None:
        payload["generation"] = contract.generation
    if contract.contribution is not None:
        payload["contribution"] = contract.contribution
    return payload


def knowledge_hit_payload(
    contract: KnowledgeHit, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("knowledge_hit")
    payload.update(
        {
            "rank": contract.rank,
            "resource": contract.resource.to_dict(),
            "revision": contract.revision.to_dict(),
            "evidence": contract.evidence.to_dict(),
            "signals": [signal.to_dict() for signal in contract.signals],
            "fused_score": contract.fused_score,
            "reasons": list(contract.reasons),
        }
    )
    if contract.confidence is not None:
        payload["confidence"] = contract.confidence
    if contract.warnings:
        payload["warnings"] = list(contract.warnings)
    return payload


def publication_head_payload(contract: PublicationHead) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": contract.scope,
        "publication_id": contract.publication_id,
        "generation": contract.generation,
    }
    if contract.model_signature is not None:
        payload["model_signature"] = contract.model_signature
    return payload


def logical_watermark_payload(contract: LogicalWatermark) -> dict[str, object]:
    return {"name": contract.name, "value": contract.value}


def active_model_payload(contract: ActiveModel) -> dict[str, object]:
    return {
        "signature": contract.signature,
        "vector_space": contract.vector_space,
        "modality": contract.modality,
        "dimensions": contract.dimensions,
        "generation": contract.generation,
    }


def owner_snapshot_identity_payload(contract: OwnerSnapshot) -> dict[str, object]:
    payload: dict[str, object] = {
        "owner": contract.owner,
        "state": contract.state.value,
        "expected_schema_version": contract.expected_schema_version,
        "publications": [
            item.to_dict()
            for item in sorted(contract.publications, key=lambda value: value.scope)
        ],
        "watermarks": [
            item.to_dict()
            for item in sorted(contract.watermarks, key=lambda value: value.name)
        ],
    }
    if contract.observed_schema_version is not None:
        payload["observed_schema_version"] = contract.observed_schema_version
    if contract.error_code is not None:
        payload["error_code"] = contract.error_code
    if contract.identity_changed:
        payload["identity_changed"] = True
    return payload


def owner_snapshot_payload(contract: OwnerSnapshot) -> dict[str, object]:
    payload = contract.identity_dict()
    if contract.data_version_before is not None:
        payload["data_version_before"] = contract.data_version_before
    if contract.data_version_after is not None:
        payload["data_version_after"] = contract.data_version_after
    if contract.warning is not None:
        payload["warning"] = contract.warning
    return payload


def knowledge_snapshot_payload(
    contract: KnowledgeSnapshot, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("knowledge_snapshot")
    payload.update(
        {
            "source_version": contract.source_version,
            "captured_at_utc": contract.captured_at_utc,
            "captured_monotonic_ns": contract.captured_monotonic_ns,
            "owners": [owner.to_dict() for owner in contract.owners],
            "active_models": [model.to_dict() for model in contract.active_models],
            "snapshot_id": contract.snapshot_id,
            "consistency": contract.consistency.value,
            "attempts": contract.attempts,
        }
    )
    if contract.changed_owners:
        payload["changed_owners"] = list(contract.changed_owners)
    if contract.warnings:
        payload["warnings"] = list(contract.warnings)
    return payload


def context_plan_step_ref_payload(
    contract: ContextPlanStepRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_plan_step_ref")
    payload.update(
        {
            "channel": contract.channel,
            "ranking_name": contract.ranking_name,
            "reason": contract.reason,
            "candidate_limit": contract.candidate_limit,
            "required": contract.required,
        }
    )
    return payload


def context_plan_ref_payload(
    contract: ContextPlanRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_plan_ref")
    payload.update(
        {
            "plan_id": contract.plan_id,
            "normalized_query": contract.normalized_query,
            "retrieval_mode": contract.retrieval_mode,
            "intents": list(contract.intents),
            "exact_terms": list(contract.exact_terms),
            "source_kinds": list(contract.source_kinds),
            "formats": list(contract.formats),
            "include_history": contract.include_history,
            "limit": contract.limit,
            "max_per_resource": contract.max_per_resource,
            "min_section_distance": contract.min_section_distance,
            "max_vectors": contract.max_vectors,
            "steps": [step.to_dict() for step in contract.steps],
        }
    )
    if contract.project is not None:
        payload["project"] = contract.project
    if contract.date_from is not None:
        payload["date_from"] = contract.date_from
    if contract.date_to is not None:
        payload["date_to"] = contract.date_to
    if contract.notices:
        payload["notices"] = list(contract.notices)
    return payload


def context_graph_budget_payload(contract: ContextGraphBudget) -> dict[str, object]:
    return {
        "identifiers_considered": contract.identifiers_considered,
        "entities_included": contract.entities_included,
        "relations_included": contract.relations_included,
        "omitted_identifiers": contract.omitted_identifiers,
        "omitted_entities": contract.omitted_entities,
        "omitted_relations": contract.omitted_relations,
        "identifier_limit_per_evidence": contract.identifier_limit_per_evidence,
        "measurement_scope": contract.measurement_scope,
    }


def context_budget_payload(contract: ContextBudget) -> dict[str, object]:
    payload: dict[str, object] = {
        "character_limit": contract.character_limit,
        "characters_used": contract.characters_used,
        "estimated_tokens": contract.estimated_tokens,
        "estimator_signature": contract.estimator_signature,
        "omitted_candidates": contract.omitted_candidates,
        "measurement_scope": contract.measurement_scope,
    }
    if contract.truncated_evidence_ids:
        payload["truncated_evidence_ids"] = list(contract.truncated_evidence_ids)
    return payload


def context_entity_ref_payload(
    contract: ContextEntityRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_entity_ref")
    payload.update(
        {
            "entity_id": contract.entity_id,
            "entity_kind": contract.entity_kind,
            "label": contract.label,
            "evidence_ids": list(contract.evidence_ids),
            "resource_ids": list(contract.resource_ids),
        }
    )
    return payload


def context_contradiction_ref_payload(
    contract: ContextContradictionRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_contradiction_ref")
    payload.update(
        {
            "contradiction_id": contract.contradiction_id,
            "contradiction_kind": contract.contradiction_kind,
            "topic": contract.topic,
            "values": list(contract.values),
            "summary": contract.summary,
            "citation_ids": list(contract.citation_ids),
        }
    )
    return payload


def context_relation_ref_payload(
    contract: ContextRelationRef, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_relation_ref")
    payload.update(
        {
            "relation_id": contract.relation_id,
            "source_entity_id": contract.source_entity_id,
            "target_entity_id": contract.target_entity_id,
            "relation_kind": contract.relation_kind,
            "method": contract.method.value,
            "provenance": list(contract.provenance),
            "evidence_ids": list(contract.evidence_ids),
        }
    )
    if contract.confidence is not None:
        payload["confidence"] = contract.confidence
    return payload


def context_bundle_payload(
    contract: ContextBundle, *, base_payload_fn: BasePayload
) -> dict[str, object]:
    payload = base_payload_fn("context_bundle")
    payload.update(
        {
            "normalized_query": contract.normalized_query,
            "intents": list(contract.intents),
            "plan_id": contract.plan_id,
            "plan": contract.plan.to_dict(),
            "snapshot": contract.snapshot.to_dict(),
            "selected_hits": [hit.to_dict() for hit in contract.selected_hits],
            "citation_ids": [
                {"citation_id": citation_id, "evidence_id": evidence_id}
                for citation_id, evidence_id in contract.citation_ids
            ],
            "entities": [entity.to_dict() for entity in contract.entities],
            "relations": [relation.to_dict() for relation in contract.relations],
            "graph_budget": contract.graph_budget.to_dict(),
            "budget": contract.budget.to_dict(),
            "rendered_context": contract.rendered_context,
            "completeness": contract.completeness.value,
        }
    )
    if contract.contradictions:
        payload["contradictions"] = [
            contradiction.to_dict() for contradiction in contract.contradictions
        ]
    if contract.missing_information:
        payload["missing_information"] = list(contract.missing_information)
    if contract.warnings:
        payload["warnings"] = list(contract.warnings)
    if contract.telemetry is not None:
        payload["telemetry"] = contract.telemetry.to_dict()
    if contract.blocking_owners:
        payload["blocking_owners"] = list(contract.blocking_owners)
    return payload


__all__ = [
    "base_payload",
    "canonical_output",
    "knowledge_phase_timing_payload",
    "knowledge_query_telemetry_payload",
    "physical_identity_ref_payload",
    "resource_ref_payload",
    "revision_ref_payload",
    "evidence_ref_payload",
    "ranking_signal_payload",
    "knowledge_hit_payload",
    "publication_head_payload",
    "logical_watermark_payload",
    "active_model_payload",
    "owner_snapshot_identity_payload",
    "owner_snapshot_payload",
    "knowledge_snapshot_payload",
    "context_plan_step_ref_payload",
    "context_plan_ref_payload",
    "context_graph_budget_payload",
    "context_budget_payload",
    "context_entity_ref_payload",
    "context_contradiction_ref_payload",
    "context_relation_ref_payload",
    "context_bundle_payload",
]
# endregion [02]
