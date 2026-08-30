"""Stable JSON-ready payload builders for Semantic service contracts."""
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_contract_payloads.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
from .semantic_models import canonical_json
from .semantic_service_contracts import (
    SemanticPlan,
    SemanticSourcePlan,
    SemanticWorkloadPlan,
)
# endregion [01]

# region [02] Implementación


def _source_plan_payload(source: SemanticSourcePlan) -> dict[str, object]:
    return {
        "chunks": source.chunks,
        "database": str(source.database),
        "embedding_entities": source.embedding_entities,
        "input_bytes": source.input_bytes,
        "resources": source.resources,
        "schema_version": source.schema_version,
        "section_text_bytes": source.section_text_bytes,
        "sections": source.sections,
        "source_bytes": source.source_bytes,
        "source_kind": source.source_kind,
        "snapshot_xxh3_128": source.snapshot_xxh3_128,
    }


def _workload_plan_payload(workload: SemanticWorkloadPlan) -> dict[str, object]:
    return {
        "cost_calibrated": workload.cost_calibrated,
        "cost_calibration_signature": workload.cost_calibration_signature,
        "cost_calibration_contents_per_second": (
            workload.cost_calibration_contents_per_second
        ),
        "cost_calibration_sample_contents": workload.cost_calibration_sample_contents,
        "cost_calibration_sample_input_bytes": (
            workload.cost_calibration_sample_input_bytes
        ),
        "cost_complete": workload.cost_complete,
        "cost_execution_signature": workload.cost_execution_signature,
        "cost_unavailable_reason": workload.cost_unavailable_reason,
        "distance": workload.distance,
        "dimensions": workload.dimensions,
        "embedding_entities": workload.embedding_entities,
        "estimated_model_seconds": workload.estimated_model_seconds,
        "estimated_model_seconds_lower_bound": (
            workload.estimated_model_seconds_lower_bound
        ),
        "estimated_model_seconds_upper_bound": (
            workload.estimated_model_seconds_upper_bound
        ),
        "input_bytes": workload.input_bytes,
        "modality": workload.modality,
        "model_id": workload.model_id,
        "model_provenance_json": workload.model_provenance_json,
        "model_signature": workload.model_signature,
        "model_version": workload.model_version,
        "model_request_contents_lower_bound": (
            workload.model_request_contents_lower_bound
        ),
        "model_request_contents_upper_bound": (
            workload.model_request_contents_upper_bound
        ),
        "name": workload.name,
        "new_unique_contents": workload.new_unique_contents,
        "new_vector_blob_bytes_lower_bound": (
            workload.new_vector_blob_bytes_lower_bound
        ),
        "planned_reusable_contents": workload.planned_reusable_contents,
        "preexisting_reusable_contents": workload.preexisting_reusable_contents,
        "processing_signature": workload.processing_signature,
        "provider": workload.provider,
        "role": workload.role,
        "supported_roles": list(workload.supported_roles),
        "unique_contents": workload.unique_contents,
        "unique_input_bytes": workload.unique_input_bytes,
        "normalization": workload.normalization,
        "vector_dtype": workload.vector_dtype,
        "vector_space": workload.vector_space,
    }


def build_semantic_plan_payload(plan: SemanticPlan) -> dict[str, object]:
    """Return the canonical public JSON-ready representation of a plan."""

    payload: dict[str, object] = {
        "complete": plan.complete,
        "content_set_xxh3_128": plan.content_set_xxh3_128,
        "cost_calibrated": plan.cost_calibrated,
        "cost_complete": plan.cost_complete,
        "dry_run": plan.dry_run,
        "embedding_entities": plan.embedding_entities,
        "estimate_kind": plan.estimate_kind,
        "estimated_model_seconds": plan.estimated_model_seconds,
        "estimated_model_seconds_lower_bound": (
            plan.estimated_model_seconds_lower_bound
        ),
        "estimated_model_seconds_upper_bound": (
            plan.estimated_model_seconds_upper_bound
        ),
        "execution_ready": plan.execution_ready,
        "input_bytes": plan.input_bytes,
        "jobs_created": plan.jobs_created,
        "max_scratch_bytes": plan.max_scratch_bytes,
        "model_request_contents_lower_bound": plan.model_request_contents_lower_bound,
        "model_request_contents_upper_bound": plan.model_request_contents_upper_bound,
        "new_unique_contents": plan.new_unique_contents,
        "new_vector_blob_bytes_lower_bound": plan.new_vector_blob_bytes_lower_bound,
        "originals_verified": plan.originals_verified,
        "plan_signature": plan.plan_signature,
        "resources": plan.resources,
        "reusable_unique_contents": plan.reusable_unique_contents,
        "scope": plan.scope,
        "scratch_storage_bytes": plan.scratch_storage_bytes,
        "section_text_bytes": plan.section_text_bytes,
        "sections": plan.sections,
        "selected_sources": list(plan.selected_sources),
        "semantic_database": str(plan.semantic_database),
        "semantic_schema_version": plan.semantic_schema_version,
        "semantic_snapshot_xxh3_128": plan.semantic_snapshot_xxh3_128,
        "snapshot_scope": plan.snapshot_scope,
        "source_bytes": plan.source_bytes,
        "sources": [_source_plan_payload(source) for source in plan.source_plans],
        "sqlite_read_snapshot_may_touch_shm": plan.sqlite_read_snapshot_may_touch_shm,
        "state_mutated": plan.state_mutated,
        "text_chunking_signature": plan.text_chunking_signature,
        "unique_contents": plan.unique_contents,
        "unique_input_bytes": plan.unique_input_bytes,
        "vector_bytes_kind": plan.vector_bytes_kind,
        "workloads": [_workload_plan_payload(workload) for workload in plan.workloads],
    }
    canonical_json(payload)
    return payload


__all__ = ["build_semantic_plan_payload"]
# endregion [02]
