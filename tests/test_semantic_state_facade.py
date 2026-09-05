# region [00] Contexto del módulo
# Módulo: tests/test_semantic_state_facade.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.semantic import semantic_evidence_repository as evidence_repository
from neocortex.semantic import semantic_generation_repository as generation_repository
from neocortex.semantic import semantic_item_repository as item_repository
from neocortex.semantic import semantic_schema, semantic_state
from neocortex.semantic import semantic_search_repository as search_repository


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')
# endregion [01]

# region [02] Implementación


_HISTORICAL_OPERATIONS = {
    "claim_embedding_jobs",
    "complete_embedding_job",
    "deactivate_semantic_item_if_fingerprint",
    "deactivate_text_chunks_for_item",
    "embedding_request_from_lease",
    "enqueue_image_item_jobs",
    "enqueue_text_chunk_jobs",
    "fail_embedding_job",
    "finalize_embedding_generation",
    "finalize_label_prototype_refresh",
    "finalize_semantic_evidence_model_refresh",
    "finalize_semantic_evidence_refresh",
    "finalize_semantic_item_refresh",
    "finalize_text_chunk_refresh",
    "generation_summary",
    "has_active_embeddings",
    "heartbeat_embedding_job",
    "heartbeat_embedding_jobs",
    "initialize_semantic_state",
    "iter_active_embedding_pages",
    "list_semantic_evidence",
    "load_active_embedding_page",
    "load_embedding_model",
    "load_label_prototypes",
    "load_semantic_item",
    "publish_semantic_evidence_entities",
    "publish_text_channel_revision",
    "record_semantic_evidence",
    "register_embedding_model",
    "resolve_search_hits",
    "reuse_cached_jobs",
    "search_exact_page",
    "semantic_database",
    "scrub_retired_image_provenance",
    "stage_label_prototypes",
    "stage_semantic_evidence",
    "stage_semantic_items",
    "stage_text_chunks",
    "start_embedding_generation",
    "store_label_prototype",
    "update_embedding_generation_cursor",
    "upsert_semantic_item",
}


def test_facade_preserves_historical_operations() -> None:
    assert _HISTORICAL_OPERATIONS <= vars(semantic_state).keys()


def test_facade_routes_operations_to_cohesive_repositories() -> None:
    assert semantic_state.stage_semantic_items is item_repository.stage_semantic_items
    assert semantic_state.stage_text_chunks is item_repository.stage_text_chunks
    assert (
        semantic_state.claim_embedding_jobs
        is generation_repository.claim_embedding_jobs
    )
    assert semantic_state.search_exact_page is search_repository.search_exact_page
    assert (
        semantic_state.stage_semantic_evidence
        is evidence_repository.stage_semantic_evidence
    )


def test_facade_preserves_every_schema_migration_reexport() -> None:
    assert semantic_state.SemanticStateError is semantic_schema.SemanticStateError
    assert semantic_state.semantic_database is semantic_schema.semantic_database
    assert (
        semantic_state.initialize_semantic_state
        is semantic_schema.initialize_semantic_state
    )
    for version in range(1, semantic_state.SEMANTIC_SCHEMA_VERSION + 1):
        name = f"_migrate_to_v{version}"
        assert getattr(semantic_state, name) is getattr(semantic_schema, name)


def test_evidence_publication_wrapper_forwards_facade_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> tuple[int, int]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 7, 3

    monkeypatch.setattr(
        semantic_state,
        "_publish_semantic_evidence_entities",
        _capture,
    )
    monkeypatch.setattr(semantic_state, "MAX_EVIDENCE_ENTITIES_PER_PUBLICATION", 9)
    monkeypatch.setattr(semantic_state, "MAX_EVIDENCE_ROWS_PER_PUBLICATION", 27)

    result = semantic_state.publish_semantic_evidence_entities(
        Path("semantic.sqlite3"),
        (),
        entities=(("item", "entity"),),
        ontology_id="ontology",
        ontology_version="v1",
        query_model_signature="query",
        indexed_model_signature="indexed",
        vector_space="space",
        refresh_token="refresh",
    )

    assert result == (7, 3)
    assert captured["kwargs"] == {
        "entities": (("item", "entity"),),
        "ontology_id": "ontology",
        "ontology_version": "v1",
        "query_model_signature": "query",
        "indexed_model_signature": "indexed",
        "vector_space": "space",
        "refresh_token": "refresh",
        "updated_ns": None,
        "_max_entities": 9,
        "_max_rows": 27,
    }
# endregion [02]
