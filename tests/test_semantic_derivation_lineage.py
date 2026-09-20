"""Semantic owner receipts and reconstructible derivation lineage v1."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, HASH_ALGORITHM_64, sha256

from neocortex.semantic import semantic_evidence_repository, semantic_generation_repository, semantic_schema
from neocortex.semantic.derivation_contracts import WorkReceipt
from neocortex.semantic.derivation_projection import (
    projection_event_from_semantic_outbox,
    rebuild_derivation_projection,
)
from neocortex.semantic.semantic_chunking import TextChunkingConfig, chunk_text_sections
from neocortex.semantic.semantic_lineage_repository import (
    explain_text_chunk_lineage,
    find_text_chunks_for_source_revision,
    read_semantic_derivation_outbox,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    SemanticItem,
    TextChunk,
    TextSection,
    encode_vector,
    fingerprint_bytes,
    fingerprint_text,
)
from neocortex.semantic.semantic_state import (
    claim_embedding_jobs,
    complete_embedding_job,
    deactivate_text_chunks_for_item,
    enqueue_image_item_jobs,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    initialize_semantic_state,
    register_embedding_model,
    release_embedding_job_lease_for_deadline,
    reuse_cached_jobs,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from tests.test_semantic_generation_publication_v6 import _create_populated_v5


TEST_CAPABILITIES = ("base", 'inference')
pytestmark = pytest.mark.capability("base", 'inference')


def _restore_semantic_receipt_update_trigger(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """CREATE TRIGGER semantic_work_receipts_no_update
        BEFORE UPDATE ON semantic_work_receipts BEGIN
            SELECT RAISE(ABORT,'semantic work receipts are append-only');
        END"""
    )


def _restore_semantic_vector_payload_update_trigger(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """CREATE TRIGGER vector_payloads_no_update
        BEFORE UPDATE ON vector_payloads BEGIN
            SELECT RAISE(ABORT,'semantic vector payloads are append-only');
        END"""
    )


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "lineage-model-v1",
        "lineage-space-v1",
        EmbeddingModality.TEXT,
        "fixture/lineage-model",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _image_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "lineage-image-model-v1",
        "lineage-image-space-v1",
        EmbeddingModality.IMAGE,
        "fixture/lineage-image-model",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.IMAGE,),
    )


def _initialize(path: Path) -> EmbeddingModelSpec:
    initialize_semantic_state(path)
    model = _model()
    register_embedding_model(path, model, allow_test_provider=True)
    return model


def _stage(
    path: Path,
    *,
    item_id: str,
    source_revision_id: str,
    text: str,
    ordinal: int,
    publish: bool = True,
) -> TextChunk:
    item = SemanticItem(
        item_id,
        "text",
        f"identity:{item_id}",
        "fixture-source-v1",
        fingerprint_text(text),
        provenance={"fixture": "semantic-lineage"},
        source_revision={
            "revision_id": source_revision_id,
            "owner_revision": {
                "owner": "text",
                "revision": {
                    "schema_version": 1,
                    "kind": "revision_ref",
                    "resource_id": f"resource:text:{item_id}",
                    "revision_id": source_revision_id,
                    "producer": "text.extract",
                    "processing_signature": "text-extract-v1",
                    "state": "current",
                    "generation": ordinal,
                    "observed_at_utc": "2026-08-11T00:00:00Z",
                },
                "fingerprint_algorithm": HASH_ALGORITHM_128,
                "fingerprint": fingerprint_text(text).xxh3_128,
            },
            "consumed_materialization": {
                "materialization": {
                    "schema_version": 1,
                    "kind": "materialization_ref",
                    "owner": "text",
                    "materialization_kind": "text_representation",
                    "materialization_id": (
                        f"materialization:text:representation:{item_id}:{ordinal}"
                    ),
                    "owner_schema_version": 2,
                    "resource": None,
                    "revision": {
                        "schema_version": 1,
                        "kind": "revision_ref",
                        "resource_id": f"resource:text:{item_id}",
                        "revision_id": source_revision_id,
                        "producer": "text.extract",
                        "processing_signature": "text-extract-v1",
                        "state": "current",
                        "generation": ordinal,
                        "observed_at_utc": "2026-08-11T00:00:00Z",
                    },
                    "generation": ordinal,
                },
                "fingerprint_algorithm": HASH_ALGORITHM_128,
                "fingerprint": fingerprint_text(text).xxh3_128,
            },
        },
    )
    upsert_semantic_item(
        path,
        item,
        refresh_token=f"item:{ordinal}",
        updated_ns=ordinal * 100,
    )
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
        algorithm_version="lineage-window-v1",
    )
    chunks = chunk_text_sections(
        item_id,
        (TextSection("text", "body", text, {"fixture": "semantic-lineage"}),),
        config,
    )
    assert len(chunks) == 1
    refresh = f"chunk-refresh:{ordinal}:{item_id}"
    stage_text_chunks(
        path,
        chunks,
        refresh_token=refresh,
        updated_ns=ordinal * 100 + 10,
    )
    if publish:
        finalize_text_chunk_refresh(
            path,
            item_id=item_id,
            chunking_signature=config.signature,
            refresh_token=refresh,
            updated_ns=ordinal * 100 + 20,
        )
    return chunks[0]


def _generation(
    path: Path,
    model: EmbeddingModelSpec,
    chunking_signature: str,
    *,
    processing_signature: str,
    started_ns: int,
    extra_provenance: dict[str, object] | None = None,
) -> int:
    provenance: dict[str, object] = {
        "sources": ["text"],
        "chunking_signature": chunking_signature,
    }
    if extra_provenance is not None:
        provenance.update(extra_provenance)
    return start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        provenance=provenance,
        started_ns=started_ns,
    )


def _execute(path: Path, generation_id: int, *, now_ns: int) -> None:
    leases = claim_embedding_jobs(
        path,
        generation_id,
        worker_id="lineage-worker",
        now_ns=now_ns,
    )
    assert len(leases) == 1
    complete_embedding_job(
        path,
        leases[0].job_id,
        worker_id="lineage-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        provenance={"fixture": "executed"},
        now_ns=now_ns + 10,
    )


def test_executed_text_lineage_is_owner_local_and_rebuildable(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="source-a",
        source_revision_id="revision:text:a",
        text="protección diferencial de transformador",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="lineage-pipeline-v1",
        started_ns=1_000,
        extra_provenance={
            "note": "NEOCORTEX_SECRET_NEUTRAL_KEY_MUST_NOT_ESCAPE",
            "provider_config": {
                "api_key": "DERIVATION_SECRET_MUST_NOT_ESCAPE",
                "region": "offline",
            },
            "providerConfig": {
                "apiKey": "CAMEL_API_KEY_MUST_NOT_ESCAPE",
                "clientSecret": "CAMEL_CLIENT_SECRET_MUST_NOT_ESCAPE",
                "accessToken": "CAMEL_ACCESS_TOKEN_MUST_NOT_ESCAPE",
            },
        },
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=1_100) == 1
    _execute(database, generation_id, now_ns=1_200)
    finalize_embedding_generation(database, generation_id, completed_ns=1_300)

    lineage = explain_text_chunk_lineage(database, chunk_id=chunk.chunk_id)
    assert lineage.lineage_status == "recorded"
    assert lineage.chunk_stage_id == "semantic.text.chunk.materialize"
    assert lineage.chunk_stage_version == "lineage-window-v1"
    assert len(lineage.origins) == 1
    assert lineage.origins[0].source_revision["revision_id"] == "revision:text:a"
    assert lineage.published is True
    assert len(lineage.embeddings) == 1
    embedding = lineage.embeddings[0]
    assert embedding.execution_mode == "executed"
    assert embedding.generation_id == generation_id
    assert embedding.model_signature == model.model_signature
    assert embedding.published is True

    dependency_page = find_text_chunks_for_source_revision(
        database,
        revision_id="revision:text:a",
        limit=1,
    )
    assert dependency_page.chunk_ids == (chunk.chunk_id,)
    assert dependency_page.truncated is False

    events = read_semantic_derivation_outbox(database, limit=20)
    assert tuple(event.event_kind for event in events) == (
        "semantic_chunk_materialized",
        "semantic_derivation_manifest_materialized",
        "semantic_chunk_set_published",
        "semantic_embedding_materialized",
        "semantic_derivation_manifest_materialized",
        "semantic_generation_published",
    )
    assert all(event.payload["receipt"] == event.receipt for event in events)
    assert all(event.receipt["kind"] == "work_receipt" for event in events)
    chunk_receipt = events[0].receipt
    assert chunk_receipt["inputs"][0]["revision"] == {
        "schema_version": 1,
        "kind": "revision_ref",
        "resource_id": "resource:text:source-a",
        "revision_id": "revision:text:a",
        "producer": "text.extract",
        "processing_signature": "text-extract-v1",
        "state": "current",
        "generation": 1,
        "observed_at_utc": "2026-08-11T00:00:00Z",
    }
    assert (
        chunk_receipt["inputs"][0]["materialization"]["materialization_id"]
        == "materialization:text:representation:source-a:1"
    )
    projection = rebuild_derivation_projection(
        tuple(projection_event_from_semantic_outbox(event) for event in events)
    )
    embedding_event = next(
        event for event in events if event.event_kind == "semantic_embedding_materialized"
    )
    embedding_materialization = str(
        embedding_event.receipt["outputs"][0]["materialization"]["materialization_id"]
    )
    explanation = projection.explain(embedding_materialization)
    assert "revision:text:a" in {node.node_id for node in explanation.nodes}
    published_generation = str(
        next(
            event for event in events if event.event_kind == "semantic_generation_published"
        ).receipt["outputs"][0]["materialization"]["materialization_id"]
    )
    chunk_materialization = str(
        chunk_receipt["outputs"][0]["materialization"]["materialization_id"]
    )
    chunker_impact = projection.impact("semantic.text.chunk.materialize", "new-chunker-v2")
    assert published_generation in chunker_impact.stale_node_ids
    assert embedding_materialization in chunker_impact.stale_node_ids
    assert "revision:text:a" in chunker_impact.reusable_node_ids
    model_impact = projection.impact("semantic.embedding", "new-model-space-v2")
    assert published_generation in model_impact.stale_node_ids
    assert embedding_materialization in model_impact.stale_node_ids
    assert chunk_materialization in model_impact.reusable_node_ids
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        payloads = tuple(
            json.loads(str(row[0]))
            for row in connection.execute(
                "SELECT receipt_json FROM semantic_work_receipts ORDER BY receipt_id"
            )
        )
        derivation_storage = "\n".join(
            str(row[0])
            for row in connection.execute(
                """SELECT receipt_json FROM semantic_work_receipts
                UNION ALL SELECT payload_json FROM semantic_derivation_outbox"""
            )
        )
    assert all(payload["schema_version"] == 1 for payload in payloads)
    assert all(payload["reproducibility"] == "environment_bound" for payload in payloads)
    assert "DERIVATION_SECRET_MUST_NOT_ESCAPE" not in derivation_storage
    assert "CAMEL_API_KEY_MUST_NOT_ESCAPE" not in derivation_storage
    assert "CAMEL_CLIENT_SECRET_MUST_NOT_ESCAPE" not in derivation_storage
    assert "CAMEL_ACCESS_TOKEN_MUST_NOT_ESCAPE" not in derivation_storage
    assert "NEOCORTEX_SECRET_NEUTRAL_KEY_MUST_NOT_ESCAPE" not in derivation_storage
    assert WorkReceipt.__name__ == "WorkReceipt"
    with semantic_schema.semantic_database(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE semantic_work_receipts SET committed_ns=committed_ns+1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM semantic_derivation_outbox")


def test_cache_hit_and_generation_clone_materialization_are_explicit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _initialize(database)
    text = "mantenimiento preventivo de subestación"
    first = _stage(
        database,
        item_id="first",
        source_revision_id="revision:text:first",
        text=text,
        ordinal=1,
    )
    baseline = _generation(
        database,
        model,
        first.chunking_signature,
        processing_signature="lineage-baseline-v1",
        started_ns=1_000,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (first.chunk_id,), now_ns=1_100) == 1
    _execute(database, baseline, now_ns=1_200)
    finalize_embedding_generation(database, baseline, completed_ns=1_300)

    second = _stage(
        database,
        item_id="second",
        source_revision_id="revision:text:second",
        text=text,
        ordinal=2,
    )
    successor = _generation(
        database,
        model,
        second.chunking_signature,
        processing_signature="lineage-successor-v1",
        started_ns=2_000,
    )
    assert enqueue_text_chunk_jobs(database, successor, (second.chunk_id,), now_ns=2_100) == 1
    assert reuse_cached_jobs(database, successor, now_ns=2_200) == 1
    finalize_embedding_generation(database, successor, completed_ns=2_300)

    cached = explain_text_chunk_lineage(database, chunk_id=second.chunk_id)
    assert tuple(item.execution_mode for item in cached.embeddings) == ("cache_hit",)
    cloned = explain_text_chunk_lineage(database, chunk_id=first.chunk_id)
    assert tuple(item.execution_mode for item in cloned.embeddings) == ("executed",)
    assert tuple(item.stage_id for item in cloned.embeddings) == ("semantic.embedding.clone",)
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        clone_receipt = connection.execute(
            """SELECT receipt_json FROM semantic_work_receipts
            WHERE receipt_id=? AND stage_id='semantic.embedding.clone'""",
            (cloned.embeddings[0].receipt_id,),
        ).fetchone()
    assert clone_receipt is not None
    clone_contract = WorkReceipt.from_json(str(clone_receipt["receipt_json"]))
    assert clone_contract.stage.stage_id == "semantic.embedding.clone"
    assert all(binding.materialization is not None for binding in clone_contract.inputs)
    assert all(
        binding.materialization.kind == "semantic_embedding_member"
        for binding in clone_contract.inputs
        if binding.materialization is not None
    )


def test_chunk_lineage_origins_are_sql_bounded_with_exact_truncation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bounded.sqlite3"
    _initialize(database)
    chunk = None
    for ordinal in range(1, 5):
        chunk = _stage(
            database,
            item_id="bounded-item",
            source_revision_id=f"revision:text:bounded:{ordinal}",
            text="contenido estable con revisiones sucesivas",
            ordinal=ordinal,
        )
    assert chunk is not None

    lineage = explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        origin_limit=2,
        embedding_limit=1,
    )

    assert lineage.origin_count == 4
    assert len(lineage.origins) == 2
    assert lineage.origins_truncated is True
    assert lineage.published is True
    assert lineage.embedding_count == 0
    assert lineage.embeddings_truncated is False


def test_chunk_lineage_filters_models_and_bounds_embedding_members(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bounded-embeddings.sqlite3"
    primary_model = _initialize(database)
    alternate_model = EmbeddingModelSpec(
        "lineage-model-v2",
        "lineage-space-v2",
        EmbeddingModality.TEXT,
        "fixture/lineage-model-alternate",
        "2",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    register_embedding_model(database, alternate_model, allow_test_provider=True)
    chunk = _stage(
        database,
        item_id="multi-model-item",
        source_revision_id="revision:text:multi-model",
        text="representacion semantica con dos espacios vectoriales",
        ordinal=1,
    )
    generation_ids: list[int] = []
    for ordinal, model in enumerate((primary_model, alternate_model), start=1):
        generation_id = _generation(
            database,
            model,
            chunk.chunking_signature,
            processing_signature=f"lineage-multi-model-v{ordinal}",
            started_ns=ordinal * 1_000,
        )
        assert (
            enqueue_text_chunk_jobs(
                database,
                generation_id,
                (chunk.chunk_id,),
                now_ns=ordinal * 1_000 + 100,
            )
            == 1
        )
        _execute(database, generation_id, now_ns=ordinal * 1_000 + 200)
        finalize_embedding_generation(
            database,
            generation_id,
            completed_ns=ordinal * 1_000 + 300,
        )
        generation_ids.append(generation_id)

    bounded = explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        embedding_limit=1,
    )
    assert bounded.embedding_count == 2
    assert len(bounded.embeddings) == 1
    assert bounded.embeddings_truncated is True
    assert bounded.embeddings[0].generation_id == generation_ids[0]

    selected = explain_text_chunk_lineage(
        database,
        chunk_id=chunk.chunk_id,
        model_signature=alternate_model.model_signature,
        embedding_limit=1,
    )
    assert selected.embedding_count == 1
    assert selected.embeddings_truncated is False
    assert tuple(embedding.model_signature for embedding in selected.embeddings) == (
        alternate_model.model_signature,
    )
    assert tuple(embedding.generation_id for embedding in selected.embeddings) == (
        generation_ids[1],
    )


@pytest.mark.parametrize(
    ("chunk_id", "origin_limit", "embedding_limit", "message"),
    (
        ("   ", 1, 1, "chunk_id cannot be blank"),
        ("chunk", 0, 1, "origin_limit must be between"),
        (
            "chunk",
            1_001,
            1,
            "origin_limit must be between",
        ),
        (
            "chunk",
            1,
            0,
            "embedding_limit must be between",
        ),
        (
            "chunk",
            1,
            1_001,
            "embedding_limit must be between",
        ),
    ),
)
def test_chunk_lineage_rejects_invalid_public_bounds_before_opening_state(
    tmp_path: Path,
    chunk_id: str,
    origin_limit: int,
    embedding_limit: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        explain_text_chunk_lineage(
            tmp_path / "absent.sqlite3",
            chunk_id=chunk_id,
            origin_limit=origin_limit,
            embedding_limit=embedding_limit,
        )


def test_published_chunk_refresh_rejects_late_members_without_rewriting_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "late-chunk.sqlite3"
    _initialize(database)
    original = _stage(
        database,
        item_id="late-item",
        source_revision_id="revision:text:late",
        text="primer bloque publicado",
        ordinal=1,
    )
    late_text = "segundo bloque tardío"
    late = TextChunk(
        chunk_id="chunk:late-member",
        item_id=original.item_id,
        ordinal=1,
        section_kind="text",
        section_id="late",
        start_char=0,
        end_char=len(late_text),
        text=late_text,
        fingerprint=fingerprint_text(late_text),
        chunking_signature=original.chunking_signature,
    )

    with pytest.raises(
        semantic_schema.SemanticStateError,
        match="already published and immutable",
    ):
        stage_text_chunks(
            database,
            (late,),
            refresh_token="chunk-refresh:1:late-item",
            updated_ns=130,
        )

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM text_chunks WHERE item_id='late-item'"
            ).fetchone()[0]
            == 1
        )
        publication = json.loads(
            str(
                connection.execute(
                    """SELECT receipt_json FROM semantic_work_receipts
                    WHERE stage_id='semantic.text.chunk.publish'"""
                ).fetchone()[0]
            )
        )
    assert publication["outputs"][0]["materialization"]["materialization_id"].startswith(
        "materialization:semantic:chunk-set:"
    )


def test_v5_to_current_migration_does_not_fabricate_legacy_receipts(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    _model_spec, chunk = _create_populated_v5(database)

    initialize_semantic_state(database)

    lineage = explain_text_chunk_lineage(database, chunk_id=chunk.chunk_id)
    assert lineage.lineage_status == "legacy_unattributed"
    assert lineage.origins == ()
    assert len(lineage.embeddings) == 1
    assert lineage.embeddings[0].execution_mode == "legacy_unattributed"
    assert lineage.embeddings[0].receipt_id is None
    assert read_semantic_derivation_outbox(database) == ()
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == semantic_schema.SEMANTIC_SCHEMA_VERSION
        )
        assert connection.execute("SELECT COUNT(*) FROM semantic_work_receipts").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM semantic_chunk_derivations").fetchone()[0] == 0
        )


@pytest.mark.parametrize("completion_mode", ("cache", "provider_collision"))
def test_v5_payload_is_attested_on_demand_without_fabricating_embedding_work(
    tmp_path: Path,
    completion_mode: str,
) -> None:
    database = tmp_path / f"legacy-{completion_mode}.sqlite3"
    model, _legacy_chunk = _create_populated_v5(database)
    initialize_semantic_state(database)
    fresh = _stage(
        database,
        item_id=f"fresh-{completion_mode}",
        source_revision_id=f"revision:text:fresh:{completion_mode}",
        text="legacy published transformer record",
        ordinal=2,
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=f"legacy-adoption-{completion_mode}-v1",
        provenance={
            "sources": ["text"],
            "chunking_signature": fresh.chunking_signature,
        },
        started_ns=1_000,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (fresh.chunk_id,),
            now_ns=1_100,
        )
        == 1
    )
    if completion_mode == "cache":
        assert reuse_cached_jobs(database, generation_id, now_ns=1_200) == 1
    else:
        lease = claim_embedding_jobs(
            database,
            generation_id,
            worker_id="legacy-collision-worker",
            now_ns=1_200,
        )[0]
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="legacy-collision-worker",
            vector=(0.0, 1.0, 0.0, 0.0),
            now_ns=1_300,
        )
    finalize_embedding_generation(database, generation_id, completed_ns=1_400)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        attestation_row = connection.execute(
            """SELECT receipt_key,receipt_json FROM semantic_work_receipts
            WHERE stage_id='semantic.vector_payload.legacy_attest'"""
        ).fetchone()
        cache_row = connection.execute(
            """SELECT receipt_json FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
              AND execution_mode='cache_hit' AND entity_id=?""",
            (generation_id, fresh.chunk_id),
        ).fetchone()
        assert (
            connection.execute(
                """SELECT legacy_before_receipts FROM vector_payloads
            ORDER BY payload_id LIMIT 1"""
            ).fetchone()[0]
            == 1
        )
    assert attestation_row is not None
    assert cache_row is not None
    attestation = WorkReceipt.from_json(str(attestation_row["receipt_json"]))
    cache_receipt = WorkReceipt.from_json(str(cache_row["receipt_json"]))
    assert attestation.stage.provider == "neocortex"
    assert attestation.stage.model is None
    assert attestation.inputs[0].materialization is not None
    assert attestation.inputs[0].materialization.kind == "semantic_vector_payload"
    assert attestation.outputs[0].materialization.kind == ("legacy_vector_payload_attestation")
    assert cache_receipt.causation_id == str(attestation_row["receipt_key"])
    assert {binding.name for binding in cache_receipt.inputs} >= {
        "reused_vector_payload",
        "legacy_payload_attestation",
    }
    assert (
        explain_text_chunk_lineage(
            database,
            chunk_id=fresh.chunk_id,
        ).lineage_status
        == "recorded"
    )
    events = read_semantic_derivation_outbox(database, limit=100)
    projection = rebuild_derivation_projection(
        tuple(projection_event_from_semantic_outbox(event) for event in events)
    )
    assert projection.events_applied == len(events)


def test_receipt_and_outbox_roll_back_with_chunk_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.sqlite3"
    _initialize(database)
    text = "coordinación de protecciones eléctricas"
    item = SemanticItem(
        "rollback-item",
        "text",
        "identity:rollback-item",
        "fixture-v1",
        fingerprint_text(text),
        source_revision={"revision_id": "revision:text:rollback"},
    )
    upsert_semantic_item(database, item, refresh_token="item", updated_ns=10)
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item.item_id,
        (TextSection("text", "body", text),),
        config,
    )
    import neocortex.semantic.semantic_lineage_repository as lineage_repository

    real_record = lineage_repository._record_work_receipt

    def interrupt_after_receipt(*args, **kwargs):
        real_record(*args, **kwargs)
        raise KeyboardInterrupt("injected receipt interruption")

    monkeypatch.setattr(lineage_repository, "_record_work_receipt", interrupt_after_receipt)
    with pytest.raises(KeyboardInterrupt, match="injected receipt interruption"):
        stage_text_chunks(database, chunks, refresh_token="refresh", updated_ns=20)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM semantic_work_receipts").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM semantic_derivation_outbox").fetchone()[0] == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_chunk_crash_before_finalize_is_explicitly_unpublished_and_not_consumable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unpublished.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="unpublished-item",
        source_revision_id="revision:text:unpublished",
        text="resultado parcial que no debe publicarse",
        ordinal=1,
        publish=False,
    )

    lineage = explain_text_chunk_lineage(database, chunk_id=chunk.chunk_id)
    assert lineage.published is False
    assert lineage.lineage_status == "materialized_unpublished"
    assert lineage.origins[0].published is False
    assert lineage.origins[0].publication_receipt_id is None

    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="unpublished-consumer-v1",
        started_ns=1_000,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (chunk.chunk_id,),
            now_ns=1_100,
        )
        == 1
    )
    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="must-not-consume-staged-output",
            now_ns=1_200,
        )
        == ()
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        predicate = semantic_evidence_repository._published_text_chunk_predicate(
            connection,
            "chunk",
        )
        assert (
            connection.execute(
                f"""SELECT COUNT(*) FROM text_chunks chunk
            WHERE chunk.chunk_id=? AND {predicate}""",
                (chunk.chunk_id,),
            ).fetchone()[0]
            == 0
        )

    finalize_text_chunk_refresh(
        database,
        item_id=chunk.item_id,
        chunking_signature=chunk.chunking_signature,
        refresh_token="chunk-refresh:1:unpublished-item",
        updated_ns=1_300,
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        predicate = semantic_evidence_repository._published_text_chunk_predicate(
            connection,
            "chunk",
        )
        assert (
            connection.execute(
                f"""SELECT COUNT(*) FROM text_chunks chunk
            WHERE chunk.chunk_id=? AND {predicate}""",
                (chunk.chunk_id,),
            ).fetchone()[0]
            == 1
        )
    assert (
        len(
            claim_embedding_jobs(
                database,
                generation_id,
                worker_id="published-output-consumer",
                now_ns=1_400,
            )
        )
        == 1
    )


def test_head_receipt_failure_rolls_back_generation_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "head-rollback.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="head-item",
        source_revision_id="revision:text:head",
        text="pruebas de resistencia de aislamiento",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="head-rollback-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=1_100)
    _execute(database, generation_id, now_ns=1_200)
    real_record = semantic_generation_repository._record_generation_publication_receipt

    def interrupt_after_receipt(*args, **kwargs):
        real_record(*args, **kwargs)
        raise KeyboardInterrupt("injected head receipt interruption")

    monkeypatch.setattr(
        semantic_generation_repository,
        "_record_generation_publication_receipt",
        interrupt_after_receipt,
    )
    with pytest.raises(KeyboardInterrupt, match="injected head receipt interruption"):
        finalize_embedding_generation(database, generation_id, completed_ns=1_300)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0] == 0
        )
        status = connection.execute(
            "SELECT status FROM embedding_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
        assert status == "building"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts WHERE stage_id='semantic.embedding.publish'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_and_repeated_cancelled_attempts_keep_distinct_terminal_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "attempts.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="attempt-item",
        source_revision_id="revision:text:attempt",
        text="diagnóstico del interruptor de potencia",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="attempt-receipts-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=1_100)

    failed_lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="failed-worker",
        now_ns=1_200,
    )[0]
    assert (
        fail_embedding_job(
            database,
            failed_lease.job_id,
            worker_id="failed-worker",
            error_type="ERROR_TYPE_SECRET_MUST_NOT_ESCAPE",
            error_message="api_key=FAILURE_SECRET_MUST_NOT_ESCAPE",
            retryable=True,
            now_ns=1_210,
        )
        == "pending"
    )

    for started_ns in (1_300, 1_400):
        cancelled_lease = claim_embedding_jobs(
            database,
            generation_id,
            worker_id="cancelled-worker",
            now_ns=started_ns,
        )[0]
        release_embedding_job_lease_for_deadline(
            database,
            cancelled_lease.job_id,
            worker_id="cancelled-worker",
            now_ns=started_ns + 10,
        )

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        rows = connection.execute(
            """SELECT receipt_key,status,attempt,receipt_json
            FROM semantic_work_receipts
            WHERE stage_id='semantic.embedding' AND status<>'succeeded'
            ORDER BY receipt_id"""
        ).fetchall()
    assert tuple(str(row["status"]) for row in rows) == (
        "failed",
        "cancelled",
        "cancelled",
    )
    assert tuple(int(row["attempt"]) for row in rows) == (1, 2, 3)
    assert len({str(row["receipt_key"]) for row in rows}) == 3
    receipts = tuple(json.loads(str(row["receipt_json"])) for row in rows)
    serialized_receipts = json.dumps(receipts, ensure_ascii=False)
    assert "FAILURE_SECRET_MUST_NOT_ESCAPE" not in serialized_receipts
    assert "ERROR_TYPE_SECRET_MUST_NOT_ESCAPE" not in serialized_receipts
    assert (
        sha256.sha256_128_hexdigest(b"api_key=FAILURE_SECRET_MUST_NOT_ESCAPE")
        not in serialized_receipts
    )
    assert (
        sha256.sha256_128_hexdigest(b"ERROR_TYPE_SECRET_MUST_NOT_ESCAPE") not in serialized_receipts
    )
    assert "message_capture" in serialized_receipts
    assert all(receipt["outputs"] == [] for receipt in receipts)
    assert tuple(receipt["outcome"] for receipt in receipts) == (
        "failed",
        "cancelled",
        "cancelled",
    )
    assert all(receipt["execution_mode"] == "attempted" for receipt in receipts)
    events = read_semantic_derivation_outbox(database, limit=20)
    attempt_events = tuple(
        event.event_kind
        for event in events
        if event.event_kind in {"semantic_work_failed", "semantic_work_cancelled"}
    )
    assert attempt_events == (
        "semantic_work_failed",
        "semantic_work_cancelled",
        "semantic_work_cancelled",
    )


@pytest.mark.parametrize(
    ("max_attempts", "expected_attempt", "expected_status"),
    ((3, 2, "leased"), (1, None, "error")),
)
def test_expired_worker_attempt_is_abandoned_before_retry_or_terminal_error(
    tmp_path: Path,
    max_attempts: int,
    expected_attempt: int | None,
    expected_status: str,
) -> None:
    database = tmp_path / f"expired-{max_attempts}.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id=f"expired-item-{max_attempts}",
        source_revision_id=f"revision:text:expired:{max_attempts}",
        text="trabajo de vector interrumpido por muerte del worker",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature=f"expired-worker-v{max_attempts}",
        started_ns=1_000,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (chunk.chunk_id,),
            max_attempts=max_attempts,
            now_ns=1_100,
        )
        == 1
    )
    first = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="worker-that-crashes",
        lease_seconds=1.0,
        now_ns=1_200,
    )
    assert first[0].attempt == 1
    reclaimed = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="recovery-worker",
        lease_seconds=1.0,
        now_ns=1_000_001_201,
    )
    if expected_attempt is None:
        assert reclaimed == ()
    else:
        assert reclaimed[0].attempt == expected_attempt

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        abandoned = connection.execute(
            """SELECT attempt,status,execution_mode,receipt_json
            FROM semantic_work_receipts
            WHERE stage_id='semantic.embedding' AND status='abandoned'"""
        ).fetchone()
        job_status = str(
            connection.execute(
                "SELECT status FROM embedding_jobs WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
    assert abandoned is not None
    assert int(abandoned["attempt"]) == 1
    assert str(abandoned["execution_mode"]) == "unknown"
    assert json.loads(str(abandoned["receipt_json"]))["outcome"] == "abandoned"
    assert job_status == expected_status


def test_source_change_during_completion_records_failed_attempt_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source-changed.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="source-changed-item",
        source_revision_id="revision:text:source-changed",
        text="contenido que cambia mientras el worker calcula",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="source-changed-pipeline-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=1_100)
    lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="source-change-worker",
        now_ns=1_200,
    )[0]
    with semantic_schema.semantic_database(database) as connection:
        connection.execute(
            "UPDATE text_chunks SET active=0 WHERE chunk_id=?",
            (chunk.chunk_id,),
        )

    with pytest.raises(
        semantic_generation_repository.StaleEmbeddingJobError,
        match="source changed",
    ):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="source-change-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=1_300,
        )

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            """SELECT receipt.status,receipt.execution_mode,job.status AS job_status
            FROM semantic_work_receipts receipt
            JOIN embedding_jobs job ON job.job_id=receipt.job_id
            WHERE receipt.job_id=? AND receipt.status='failed'""",
            (lease.job_id,),
        ).fetchone()
    assert row is not None
    assert str(row["status"]) == "failed"
    assert str(row["execution_mode"]) == "attempted"
    assert str(row["job_status"]) == "stale"


def test_failed_embeddings_with_different_pipelines_rebuild_without_false_revision_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failed-projection.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="failed-projection-item",
        source_revision_id="revision:text:failed-projection",
        text="misma entrada falla bajo dos configuraciones",
        ordinal=1,
    )
    for ordinal, signature in enumerate(("failed-pipeline-a", "failed-pipeline-b"), 1):
        generation_id = _generation(
            database,
            model,
            chunk.chunking_signature,
            processing_signature=signature,
            started_ns=ordinal * 1_000,
        )
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (chunk.chunk_id,),
            max_attempts=1,
            now_ns=ordinal * 1_000 + 100,
        )
        lease = claim_embedding_jobs(
            database,
            generation_id,
            worker_id=f"failed-worker-{ordinal}",
            now_ns=ordinal * 1_000 + 200,
        )[0]
        assert (
            fail_embedding_job(
                database,
                lease.job_id,
                worker_id=f"failed-worker-{ordinal}",
                error_type="provider_failed",
                error_message="provider failed without outputs",
                retryable=False,
                now_ns=ordinal * 1_000 + 300,
            )
            == "error"
        )

    events = read_semantic_derivation_outbox(database, limit=100)
    projection = rebuild_derivation_projection(
        tuple(projection_event_from_semantic_outbox(event) for event in events)
    )
    assert projection.events_applied == len(events)
    failed_receipts = tuple(
        event.receipt for event in events if event.event_kind == "semantic_work_failed"
    )
    assert len(failed_receipts) == 2
    assert {receipt["inputs"][-1]["revision"]["revision_id"] for receipt in failed_receipts} == {
        "revision:semantic:chunk:1"
    }


def test_restage_keeps_the_previous_chunk_truth_visible_until_finalize(
    tmp_path: Path,
) -> None:
    database = tmp_path / "clone-unpublished.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="clone-unpublished-item",
        source_revision_id="revision:text:clone-unpublished",
        text="vector base no autoriza una publicación de chunks incompleta",
        ordinal=1,
    )
    baseline = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="clone-baseline-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, baseline, (chunk.chunk_id,), now_ns=1_100)
    _execute(database, baseline, now_ns=1_200)
    finalize_embedding_generation(database, baseline, completed_ns=1_300)

    stage_text_chunks(
        database,
        (chunk,),
        refresh_token="clone-unpublished-refresh",
        updated_ns=1_400,
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        predicate = semantic_evidence_repository._published_text_chunk_predicate(
            connection,
            "chunk",
        )
        staged = connection.execute(
            f"""SELECT chunk.refresh_token,
                EXISTS(
                    SELECT 1 FROM semantic_chunk_derivations derivation
                    JOIN semantic_chunk_revisions revision
                      ON revision.chunk_revision_id=derivation.chunk_revision_id
                    WHERE revision.chunk_id=chunk.chunk_id
                      AND derivation.refresh_token='clone-unpublished-refresh'
                      AND derivation.publication_receipt_id IS NULL)
                    AS has_unpublished_successor
            FROM text_chunks chunk
            WHERE chunk.chunk_id=? AND {predicate}""",
            (chunk.chunk_id,),
        ).fetchone()
    assert staged is not None
    assert str(staged["refresh_token"]) == "chunk-refresh:1:clone-unpublished-item"
    assert bool(staged["has_unpublished_successor"])

    successor = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="clone-successor-v1",
        started_ns=1_500,
    )
    assert finalize_embedding_generation(database, successor, completed_ns=1_600).status == "ready"

    finalize_text_chunk_refresh(
        database,
        item_id=chunk.item_id,
        chunking_signature=chunk.chunking_signature,
        refresh_token="clone-unpublished-refresh",
        updated_ns=1_700,
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert (
            str(
                connection.execute(
                    "SELECT refresh_token FROM text_chunks WHERE chunk_id=?",
                    (chunk.chunk_id,),
                ).fetchone()[0]
            )
            == "clone-unpublished-refresh"
        )


def test_reenable_inactive_published_chunk_stages_hidden_successor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reactivate-published.sqlite3"
    model = _initialize(database)
    chunk = _stage(
        database,
        item_id="reactivate-item",
        source_revision_id="revision:text:reactivate",
        text="OCR publicado que puede desactivarse y reactivarse",
        ordinal=1,
    )
    assert deactivate_text_chunks_for_item(database, item_id=chunk.item_id) == 1

    refresh_token = "chunk-refresh:reactivate:2"
    assert (
        stage_text_chunks(
            database,
            (chunk,),
            refresh_token=refresh_token,
            updated_ns=1_000,
        )
        == 1
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="reactivate-pending-v1",
        started_ns=1_100,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (chunk.chunk_id,),
            now_ns=1_200,
        )
        == 1
    )
    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="reactivate-worker",
            now_ns=1_300,
        )
        == ()
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        predicate = semantic_evidence_repository._published_text_chunk_predicate(
            connection,
            "chunk",
        )
        row = connection.execute(
            f"""SELECT chunk.active,chunk.refresh_token,
                CASE WHEN {predicate} THEN 1 ELSE 0 END AS published
            FROM text_chunks chunk WHERE chunk.chunk_id=?""",
            (chunk.chunk_id,),
        ).fetchone()
        assert row is not None
        assert bool(row["active"])
        assert str(row["refresh_token"]) == refresh_token
        assert not bool(row["published"])

    finalize_text_chunk_refresh(
        database,
        item_id=chunk.item_id,
        chunking_signature=chunk.chunking_signature,
        refresh_token=refresh_token,
        updated_ns=1_400,
    )
    assert (
        len(
            claim_embedding_jobs(
                database,
                generation_id,
                worker_id="reactivate-worker",
                now_ns=1_500,
            )
        )
        == 1
    )


def test_embedding_success_keeps_the_item_revision_captured_at_queue_time(
    tmp_path: Path,
) -> None:
    database = tmp_path / "queued-revision.sqlite3"
    model = _initialize(database)
    text = "contenido estable con metadata cambiante"
    chunk = _stage(
        database,
        item_id="queued-revision-item",
        source_revision_id="revision:text:queued-a",
        text=text,
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="queued-input-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=1_100)
    lease = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="queued-input-worker",
        now_ns=1_200,
    )[0]
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        queued_revision_id = int(
            connection.execute(
                "SELECT input_item_revision_id FROM embedding_jobs WHERE job_id=?",
                (lease.job_id,),
            ).fetchone()[0]
        )
    upsert_semantic_item(
        database,
        SemanticItem(
            "queued-revision-item",
            "text",
            "identity:queued-revision-item",
            "fixture-source-v1",
            fingerprint_text(text),
            provenance={"metadata_revision": "B"},
            source_revision={"revision_id": "revision:text:queued-b"},
        ),
        refresh_token="item-metadata-b",
        updated_ns=1_250,
    )
    with semantic_schema.semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_revision_id = semantic_generation_repository._snapshot_item_revision(
            connection,
            "queued-revision-item",
            1_260,
        )
    assert current_revision_id != queued_revision_id

    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="queued-input-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=1_300,
    )
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        member_revision_id = int(
            connection.execute(
                """SELECT item_revision_id FROM embedding_generation_members
                WHERE generation_id=? AND entity_id=?""",
                (generation_id, chunk.chunk_id),
            ).fetchone()[0]
        )
        receipt = json.loads(
            str(
                connection.execute(
                    """SELECT receipt_json FROM semantic_work_receipts
                    WHERE job_id=? AND status='succeeded'""",
                    (lease.job_id,),
                ).fetchone()[0]
            )
        )
    assert member_revision_id == queued_revision_id
    assert receipt["inputs"][0]["revision"]["revision_id"] == (
        f"revision:semantic:item:{queued_revision_id}"
    )


def test_image_cache_hits_reference_the_exact_payload_producer_after_rebind(
    tmp_path: Path,
) -> None:
    database = tmp_path / "image-cache-lineage.sqlite3"
    initialize_semantic_state(database)
    model = _image_model()
    register_embedding_model(database, model, allow_test_provider=True)
    payload = b"same-image-payload-for-cache-lineage"
    fingerprint = fingerprint_bytes(payload)
    image_a = tmp_path / "image-a.png"
    image_b = tmp_path / "image-b.png"
    image_a.write_bytes(payload)
    image_b.write_bytes(payload)
    item_a = SemanticItem(
        "image-a",
        "image",
        "identity:image-a",
        "fixture-image-v1",
        fingerprint,
        path=str(image_a),
        provenance={"revision": "a"},
        source_revision={"revision_id": "image-a-revision-a"},
    )
    upsert_semantic_item(
        database,
        item_a,
        refresh_token="image-a-initial",
        updated_ns=100,
    )
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="image-baseline-v1",
        started_ns=200,
    )
    assert enqueue_image_item_jobs(database, baseline, (item_a.item_id,), now_ns=210) == 1
    _execute(database, baseline, now_ns=220)
    finalize_embedding_generation(database, baseline, completed_ns=240)

    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="image-successor-v1",
        started_ns=290,
    )
    upsert_semantic_item(
        database,
        SemanticItem(
            "image-a",
            "image",
            "identity:image-a",
            "fixture-image-v1",
            fingerprint,
            path=str(image_a),
            provenance={"revision": "b"},
            source_revision={"revision_id": "image-a-revision-b"},
        ),
        refresh_token="image-a-rebound",
        updated_ns=300,
    )
    item_b = SemanticItem(
        "image-b",
        "image",
        "identity:image-b",
        "fixture-image-v1",
        fingerprint,
        path=str(image_b),
        provenance={"revision": "a"},
        source_revision={"revision_id": "image-b-revision-a"},
    )
    upsert_semantic_item(
        database,
        item_b,
        refresh_token="image-b-initial",
        updated_ns=310,
    )
    assert enqueue_image_item_jobs(database, successor, (item_a.item_id,), now_ns=330) == 0
    assert enqueue_image_item_jobs(database, successor, (item_b.item_id,), now_ns=340) == 1
    assert reuse_cached_jobs(database, successor, now_ns=350) == 1
    finalize_embedding_generation(database, successor, completed_ns=360)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        baseline_receipt = connection.execute(
            """SELECT receipt_key FROM semantic_work_receipts
            WHERE generation_id=? AND execution_mode='executed'
              AND status='succeeded' AND stage_id='semantic.embedding'""",
            (baseline,),
        ).fetchone()
        clone_receipt = connection.execute(
            """SELECT receipt_key FROM semantic_work_receipts
            WHERE generation_id=? AND execution_mode='executed'
              AND status='succeeded' AND stage_id='semantic.embedding.clone'""",
            (successor,),
        ).fetchone()
        reused_receipts = connection.execute(
            """SELECT execution_mode,receipt_json FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
              AND execution_mode IN ('cache_hit','replay')
            ORDER BY execution_mode""",
            (successor,),
        ).fetchall()
    assert baseline_receipt is not None
    assert clone_receipt is not None
    assert [str(row["execution_mode"]) for row in reused_receipts] == [
        "cache_hit",
        "cache_hit",
    ]
    assert {
        str(json.loads(str(row["receipt_json"]))["causation_id"]) for row in reused_receipts
    } == {str(baseline_receipt["receipt_key"])}
    assert str(clone_receipt["receipt_key"]) != str(
        json.loads(str(reused_receipts[0]["receipt_json"]))["causation_id"]
    )

    events = read_semantic_derivation_outbox(database, limit=100)
    projection = rebuild_derivation_projection(
        tuple(projection_event_from_semantic_outbox(event) for event in events)
    )
    assert projection.events_applied == len(events)


def test_duplicate_provider_completions_keep_one_payload_producer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "duplicate-provider-completion.sqlite3"
    initialize_semantic_state(database)
    model = _image_model()
    register_embedding_model(database, model, allow_test_provider=True)
    payload = b"concurrent-identical-image-content"
    fingerprint = fingerprint_bytes(payload)
    item_ids = ("duplicate-image-a", "duplicate-image-b", "duplicate-image-c")
    for ordinal, item_id in enumerate(item_ids, 1):
        image = tmp_path / f"{item_id}.png"
        image.write_bytes(payload)
        upsert_semantic_item(
            database,
            SemanticItem(
                item_id,
                "image",
                f"identity:{item_id}",
                "fixture-image-v1",
                fingerprint,
                path=str(image),
            ),
            refresh_token=f"duplicate-item:{ordinal}",
            updated_ns=ordinal * 10,
        )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="duplicate-completion-v1",
        started_ns=100,
    )
    assert enqueue_image_item_jobs(database, generation_id, item_ids[:2], now_ns=110) == 2
    leases = claim_embedding_jobs(
        database,
        generation_id,
        worker_id="duplicate-worker",
        now_ns=120,
    )
    assert len(leases) == 1
    with semantic_schema.semantic_database(database) as connection:
        legacy_race = connection.execute(
            """SELECT job_id FROM embedding_jobs
            WHERE generation_id=? AND status='pending'""",
            (generation_id,),
        ).fetchone()
        assert legacy_race is not None
        legacy_job_id = int(legacy_race["job_id"])
        connection.execute(
            """UPDATE embedding_jobs SET status='leased',attempts=1,
            attempt_sequence=1,attempt_started_ns=120,
            lease_owner='duplicate-worker',lease_until_ns=1000000
            WHERE job_id=?""",
            (legacy_job_id,),
        )
    first_payload = complete_embedding_job(
        database,
        leases[0].job_id,
        worker_id="duplicate-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=130,
    )
    duplicate_payload = complete_embedding_job(
        database,
        legacy_job_id,
        worker_id="duplicate-worker",
        vector=(0.0, 1.0, 0.0, 0.0),
        now_ns=140,
    )
    assert duplicate_payload == first_payload
    assert enqueue_image_item_jobs(database, generation_id, item_ids[2:], now_ns=150) == 1
    assert reuse_cached_jobs(database, generation_id, now_ns=160) == 1
    finalize_embedding_generation(database, generation_id, completed_ns=170)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        receipts = connection.execute(
            """SELECT execution_mode,receipt_key,receipt_json
            FROM semantic_work_receipts
            WHERE generation_id=? AND stage_id='semantic.embedding'
              AND status='succeeded' ORDER BY receipt_id""",
            (generation_id,),
        ).fetchall()
        discarded = connection.execute(
            """SELECT execution_mode,receipt_json FROM semantic_work_receipts
            WHERE generation_id=?
              AND stage_id='semantic.embedding.provider_execution_discard'""",
            (generation_id,),
        ).fetchall()
        assert int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]) == 1
    assert [str(row["execution_mode"]) for row in receipts] == [
        "executed",
        "cache_hit",
        "cache_hit",
    ]
    producer_key = str(receipts[0]["receipt_key"])
    assert {str(json.loads(str(row["receipt_json"]))["causation_id"]) for row in receipts[1:]} == {
        producer_key
    }
    assert len(discarded) == 1
    discard_contract = WorkReceipt.from_json(str(discarded[0]["receipt_json"]))
    assert discarded[0]["execution_mode"] == "executed"
    assert discard_contract.reproducibility.value == "non_replayable"
    assert dict(discard_contract.effective_configuration)["disposition"] == (
        "discarded_duplicate_content_payload"
    )
    candidate_blob, _candidate_norm = encode_vector(
        (0.0, 1.0, 0.0, 0.0),
        model.dimensions,
        model.vector_dtype,
    )
    candidate_fingerprint = fingerprint_bytes(candidate_blob)
    assert discard_contract.outputs[0].fingerprint == (
        f"{candidate_fingerprint.xxh3_128};bytes={candidate_fingerprint.byte_count};"
        f"{HASH_ALGORITHM_64}-guard={candidate_fingerprint.xxh3_64_guard}"
    )

    events = read_semantic_derivation_outbox(database, limit=100)
    projection = rebuild_derivation_projection(
        tuple(projection_event_from_semantic_outbox(event) for event in events)
    )
    assert projection.events_applied == len(events)


@pytest.mark.parametrize(
    "fault",
    (
        "forged_cache_input",
        "corrupt_causation",
        "forged_producer_input",
        "forged_producer_provider",
        "mutated_vector_blob",
        "forged_clone_input",
    ),
)
def test_lineage_reader_fails_closed_on_exact_semantic_fact_corruption(
    tmp_path: Path,
    fault: str,
) -> None:
    database = tmp_path / f"corrupt-{fault}.sqlite3"
    model = _initialize(database)
    text = "payload compartido para validar causalidad exacta"
    first = _stage(
        database,
        item_id="corrupt-first",
        source_revision_id="revision:text:corrupt:first",
        text=text,
        ordinal=1,
    )
    baseline = _generation(
        database,
        model,
        first.chunking_signature,
        processing_signature="corrupt-baseline-v1",
        started_ns=1_000,
    )
    enqueue_text_chunk_jobs(database, baseline, (first.chunk_id,), now_ns=1_100)
    _execute(database, baseline, now_ns=1_200)
    finalize_embedding_generation(database, baseline, completed_ns=1_300)
    second = _stage(
        database,
        item_id="corrupt-second",
        source_revision_id="revision:text:corrupt:second",
        text=text,
        ordinal=2,
    )
    successor = _generation(
        database,
        model,
        second.chunking_signature,
        processing_signature="corrupt-successor-v1",
        started_ns=1_400,
    )
    enqueue_text_chunk_jobs(database, successor, (second.chunk_id,), now_ns=1_500)
    assert reuse_cached_jobs(database, successor, now_ns=1_600) == 1
    finalize_embedding_generation(database, successor, completed_ns=1_700)

    selected_chunk_id = second.chunk_id
    with semantic_schema.semantic_database(database) as connection:
        if fault in {
            "forged_cache_input",
            "corrupt_causation",
            "forged_producer_input",
            "forged_producer_provider",
            "forged_clone_input",
        }:
            connection.execute("DROP TRIGGER semantic_work_receipts_no_update")
        if fault == "forged_cache_input":
            row = connection.execute(
                """SELECT receipt_id,receipt_json FROM semantic_work_receipts
                WHERE generation_id=? AND stage_id='semantic.embedding'
                  AND execution_mode='cache_hit'""",
                (successor,),
            ).fetchone()
            payload = json.loads(str(row["receipt_json"]))
            payload["inputs"][0]["revision"]["processing_signature"] = "forged"
            payload["inputs"][0]["materialization"]["revision"]["processing_signature"] = "forged"
            forged = WorkReceipt.from_dict(payload).to_json()
            connection.execute(
                "UPDATE semantic_work_receipts SET receipt_json=? WHERE receipt_id=?",
                (forged, int(row["receipt_id"])),
            )
        elif fault == "corrupt_causation":
            connection.execute(
                """UPDATE semantic_work_receipts SET receipt_json='{}'
                WHERE generation_id=? AND stage_id='semantic.embedding'
                  AND execution_mode='executed'""",
                (baseline,),
            )
        elif fault in {"forged_producer_input", "forged_producer_provider"}:
            row = connection.execute(
                """SELECT receipt_id,receipt_json FROM semantic_work_receipts
                WHERE generation_id=? AND stage_id='semantic.embedding'
                  AND execution_mode='executed'""",
                (baseline,),
            ).fetchone()
            payload = json.loads(str(row["receipt_json"]))
            if fault == "forged_producer_input":
                payload["inputs"][0]["revision"]["processing_signature"] = "forged"
                payload["inputs"][0]["materialization"]["revision"]["processing_signature"] = (
                    "forged"
                )
            else:
                payload["stage"]["provider"] = "forged-provider"
            forged = WorkReceipt.from_dict(payload).to_json()
            connection.execute(
                "UPDATE semantic_work_receipts SET receipt_json=? WHERE receipt_id=?",
                (forged, int(row["receipt_id"])),
            )
        elif fault == "mutated_vector_blob":
            replacement, _norm = encode_vector(
                (0.0, 1.0, 0.0, 0.0),
                model.dimensions,
                model.vector_dtype,
            )
            connection.execute("DROP TRIGGER vector_payloads_no_update")
            connection.execute(
                "UPDATE vector_payloads SET vector_blob=?",
                (replacement,),
            )
            _restore_semantic_vector_payload_update_trigger(connection)
        else:
            row = connection.execute(
                """SELECT receipt_id,receipt_json FROM semantic_work_receipts
                WHERE generation_id=? AND stage_id='semantic.embedding.clone'""",
                (successor,),
            ).fetchone()
            payload = json.loads(str(row["receipt_json"]))
            payload["inputs"][0]["revision"]["processing_signature"] = "forged"
            payload["inputs"][0]["materialization"]["revision"]["processing_signature"] = "forged"
            forged = WorkReceipt.from_dict(payload).to_json()
            connection.execute(
                "UPDATE semantic_work_receipts SET receipt_json=? WHERE receipt_id=?",
                (forged, int(row["receipt_id"])),
            )
            selected_chunk_id = first.chunk_id
        if fault in {
            "forged_cache_input",
            "corrupt_causation",
            "forged_producer_input",
            "forged_producer_provider",
            "forged_clone_input",
        }:
            _restore_semantic_receipt_update_trigger(connection)

    with pytest.raises((ValueError, semantic_schema.SemanticStateError)):
        explain_text_chunk_lineage(database, chunk_id=selected_chunk_id)


def test_semantic_outbox_page_has_a_hard_byte_budget_and_progresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bounded-outbox.sqlite3"
    _initialize(database)
    _stage(
        database,
        item_id="bounded-outbox-item",
        source_revision_id="revision:text:bounded-outbox",
        text="eventos owner-local deben paginarse por bytes",
        ordinal=1,
    )
    import neocortex.semantic.semantic_lineage_repository as lineage_repository

    monkeypatch.setattr(lineage_repository, "_MAX_OUTBOX_PAGE_BYTES", 1)
    cursor = 0
    observed: list[int] = []
    while True:
        page = read_semantic_derivation_outbox(
            database,
            after_event_id=cursor,
            limit=1_000,
        )
        if not page:
            break
        assert len(page) == 1
        assert page[0].event_id > cursor
        cursor = page[0].event_id
        observed.append(cursor)
    with semantic_schema.semantic_database(database, readonly=True) as connection:
        expected = [
            int(row[0])
            for row in connection.execute(
                "SELECT event_id FROM semantic_derivation_outbox ORDER BY event_id"
            )
        ]
    assert observed == expected


def test_semantic_outbox_rejects_a_corrupt_owner_receipt(tmp_path: Path) -> None:
    database = tmp_path / "corrupt-outbox.sqlite3"
    _initialize(database)
    _stage(
        database,
        item_id="corrupt-outbox-item",
        source_revision_id="revision:text:corrupt-outbox",
        text="outbox no debe ocultar receipts corruptos",
        ordinal=1,
    )
    with semantic_schema.semantic_database(database) as connection:
        connection.execute("DROP TRIGGER semantic_work_receipts_no_update")
        connection.execute("UPDATE semantic_work_receipts SET receipt_json='{}' WHERE receipt_id=1")
        _restore_semantic_receipt_update_trigger(connection)
    with pytest.raises(ValueError, match="WorkReceipt"):
        read_semantic_derivation_outbox(database, limit=100)


def test_local_receipts_do_not_make_a_legacy_source_fully_attributed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-source-attribution.sqlite3"
    model = _initialize(database)
    text = "fuente legacy sin RevisionRef owner-native"
    item = SemanticItem(
        "legacy-source-item",
        "text",
        "identity:legacy-source-item",
        "legacy-source-v1",
        fingerprint_text(text),
        source_revision={"size": len(text.encode("utf-8"))},
    )
    upsert_semantic_item(
        database,
        item,
        refresh_token="legacy-source-item",
        updated_ns=100,
    )
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunk = chunk_text_sections(
        item.item_id,
        (TextSection("text", "body", text),),
        config,
    )[0]
    stage_text_chunks(
        database,
        (chunk,),
        refresh_token="legacy-source-chunks",
        updated_ns=110,
    )
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=chunk.chunking_signature,
        refresh_token="legacy-source-chunks",
        updated_ns=120,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="legacy-source-pipeline-v1",
        started_ns=200,
    )
    enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=210)
    _execute(database, generation_id, now_ns=220)
    finalize_embedding_generation(database, generation_id, completed_ns=240)

    lineage = explain_text_chunk_lineage(database, chunk_id=chunk.chunk_id)
    assert lineage.published is True
    assert lineage.lineage_status == "partially_unattributed"
    assert lineage.origins[0].lineage_status == "published_partially_unattributed"
