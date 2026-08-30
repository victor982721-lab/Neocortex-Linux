"""Stable compatibility facade for the modular semantic-state repositories."""
# region [00] Contexto del módulo
# Módulo: neocortex/semantic_state.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# The imports in this module intentionally preserve the historical public and
# private compatibility surface while implementations live in cohesive modules.
# ruff: noqa: F401

# region [01] Dependencias del módulo
from __future__ import annotations
import heapq
import itertools
import json
import math
import sqlite3
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

from .semantic_evidence_repository import (
    _EVIDENCE_UPSERT_SQL,
    _bounded_publication_entities,
    _bounded_publication_evidence,
    _evidence_batches,
    _prototype_batches,
    _publication_evidence_keys,
    _store_label_prototype_row,
    _upsert_evidence_batch,
    _validate_evidence_batch,
    finalize_label_prototype_refresh,
    finalize_semantic_evidence_model_refresh,
    finalize_semantic_evidence_refresh,
    list_semantic_evidence,
    load_label_prototypes,
    publish_semantic_evidence_entities as _publish_semantic_evidence_entities,
    record_semantic_evidence,
    stage_label_prototypes,
    stage_semantic_evidence,
    store_label_prototype,
)
from .semantic_generation_repository import (
    EmbeddingGenerationRebaseRequiredError,
    EnqueueJobBatchResult,
    _attach_payload,
    _enqueue_jobs,
    _generation_model,
    _generation_summary_row,
    _generation_summary_rows,
    _job_is_current,
    _lease_rows,
    _mark_stale_jobs,
    _queue_job_rows,
    claim_embedding_jobs,
    complete_embedding_job,
    embedding_request_from_lease,
    enqueue_image_item_jobs,
    enqueue_image_item_jobs_bounded,
    enqueue_text_chunk_jobs,
    enqueue_text_chunk_jobs_bounded,
    fail_embedding_job,
    finalize_embedding_generation,
    generation_summary,
    heartbeat_embedding_job,
    heartbeat_embedding_jobs,
    prepare_embedding_generation,
    release_embedding_job_lease_for_deadline,
    reuse_cached_jobs,
    start_embedding_generation,
    update_embedding_generation_cursor,
)
from .semantic_item_repository import (
    _decode_chunk_text,
    _encode_chunk_text,
    _upsert_item,
    deactivate_semantic_item_if_fingerprint,
    deactivate_text_chunks_for_item,
    finalize_semantic_item_refresh,
    finalize_text_chunk_refresh,
    publish_text_channel_revision,
    register_embedding_model,
    stage_semantic_items,
    stage_text_chunks,
    upsert_semantic_item,
)
from .semantic_models import (
    ActiveEmbeddingPage,
    ActiveEmbeddingRecord,
    CalibrationStatus,
    ContentFingerprint,
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    EvidenceDisposition,
    ExactSearchPage,
    ExactSearchQuery,
    GenerationSummary,
    LabelPrototype,
    ResolvedSearchHit,
    SearchHit,
    SemanticEntityKind,
    SemanticEvidence,
    SemanticItem,
    StoredLabelPrototype,
    TextChunk,
    VectorDType,
    canonical_json,
    cosine_similarity,
    decode_vector,
    encode_vector,
    fingerprint_text,
    normalize_vector,
)
from .semantic_repository_common import (
    MAX_ERROR_CHARS,
    MAX_EVIDENCE_ENTITIES_PER_PUBLICATION,
    MAX_EVIDENCE_ROWS_PER_PUBLICATION,
    MAX_STORED_CHUNK_BYTES,
    MAX_WRITE_BATCH,
    StaleEmbeddingJobError,
    _batches,
    _check_batch_size,
    _chunk_batches,
    _fingerprint_from_row,
    _item_batches,
    _load_model,
    _model_from_row,
    _now,
    _same_fingerprint,
    load_embedding_model,
    load_semantic_item,
)
from .semantic_schema import (
    SEMANTIC_SCHEMA_VERSION as SEMANTIC_SCHEMA_VERSION,
    SemanticStateError as SemanticStateError,
    _migrate_to_v1 as _migrate_to_v1,
    _migrate_to_v2 as _migrate_to_v2,
    _migrate_to_v3 as _migrate_to_v3,
    _migrate_to_v4 as _migrate_to_v4,
    _migrate_to_v5 as _migrate_to_v5,
    _migrate_to_v6 as _migrate_to_v6,
    _migrate_to_v7 as _migrate_to_v7,
    initialize_semantic_state as initialize_semantic_state,
    semantic_database,
)
from .semantic_search_repository import (
    _exact_search_hit,
    _retain_exact_search_hit,
    _search_models,
    _search_sql,
    has_active_embeddings,
    iter_active_embedding_pages,
    load_active_embedding_page,
    resolve_search_hits,
    search_exact_evidence_page,
    search_exact_page,
)
# endregion [01]

# region [02] Implementación


def publish_semantic_evidence_entities(
    path: Path,
    evidence: Iterable[SemanticEvidence],
    *,
    entities: Iterable[tuple[str, str]],
    ontology_id: str,
    ontology_version: str,
    query_model_signature: str,
    indexed_model_signature: str,
    vector_space: str,
    refresh_token: str,
    updated_ns: int | None = None,
) -> tuple[int, int]:
    """Publish evidence while honoring facade-level compatibility limits."""

    return _publish_semantic_evidence_entities(
        path,
        evidence,
        entities=entities,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
        query_model_signature=query_model_signature,
        indexed_model_signature=indexed_model_signature,
        vector_space=vector_space,
        refresh_token=refresh_token,
        updated_ns=updated_ns,
        _max_entities=MAX_EVIDENCE_ENTITIES_PER_PUBLICATION,
        _max_rows=MAX_EVIDENCE_ROWS_PER_PUBLICATION,
    )


# endregion [02]
