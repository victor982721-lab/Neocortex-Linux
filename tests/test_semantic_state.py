from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.semantic import semantic_state as state_module
from neocortex.semantic import semantic_generation_repository
from neocortex.semantic import semantic_search_repository
from neocortex.semantic.semantic_backends import merge_exact_search_pages
from neocortex.semantic.semantic_chunking import (
    TextChunkingConfig,
    chunk_text_sections,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    ExactSearchQuery,
    LabelPrototype,
    SemanticEvidence,
    SemanticItem,
    TextChunk,
    TextSection,
    VectorDType,
    encode_vector,
    fingerprint_bytes,
    fingerprint_text,
)
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    claim_embedding_jobs,
    complete_embedding_job,
    embedding_request_from_lease,
    enqueue_image_item_jobs,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    finalize_label_prototype_refresh,
    finalize_semantic_evidence_model_refresh,
    finalize_semantic_evidence_refresh,
    finalize_semantic_item_refresh,
    finalize_text_chunk_refresh,
    generation_summary,
    has_active_embeddings,
    heartbeat_embedding_jobs,
    initialize_semantic_state,
    iter_active_embedding_pages,
    list_semantic_evidence,
    load_active_embedding_page,
    load_embedding_model,
    load_label_prototypes,
    load_semantic_item,
    publish_semantic_evidence_entities,
    publish_text_channel_revision,
    record_semantic_evidence,
    register_embedding_model,
    resolve_search_hits,
    reuse_cached_jobs,
    search_exact_evidence_page,
    search_exact_page,
    semantic_database,
    stage_semantic_evidence,
    stage_label_prototypes,
    stage_semantic_items,
    stage_text_chunks,
    start_embedding_generation,
    store_label_prototype,
    update_embedding_generation_cursor,
    upsert_semantic_item,
)
from neocortex.semantic.semantic_sources import (
    SEMANTIC_TITLE_POLICY,
    SEMANTIC_TITLE_SECTION_KIND,
)


# region [01] Test builders


def _text_model(
    signature: str = "text-model-v1",
    space: str = "text-space-v1",
    dimensions: int = 4,
) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        signature,
        space,
        EmbeddingModality.TEXT,
        f"fixture/{signature}",
        "1",
        dimensions,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _image_model(
    signature: str = "image-model-v1",
    space: str = "clip-space-v1",
    dimensions: int = 4,
) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        signature,
        space,
        EmbeddingModality.IMAGE,
        f"fixture/{signature}",
        "1",
        dimensions,
        "test-deterministic",
        (EmbeddingRole.IMAGE,),
    )


def _initialize(path: Path, *models: EmbeddingModelSpec) -> None:
    initialize_semantic_state(path)
    for model in models:
        register_embedding_model(path, model, allow_test_provider=True)


def _stage_text_item(
    path: Path,
    item_id: str,
    text: str,
    *,
    source_kind: str = "pdf",
    refresh: str = "items-r1",
) -> tuple[SemanticItem, TextChunk]:
    item = SemanticItem(
        item_id=item_id,
        source_kind=source_kind,
        source_identity=f"identity:{item_id}",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text(text),
        path=f"C:/fixtures/{item_id}.pdf",
        provenance={"fixture": True},
        source_revision={"text_chars": len(text)},
    )
    upsert_semantic_item(path, item, refresh_token=refresh, updated_ns=10)
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item_id,
        (TextSection("pdf_page", "1", text, {"page": 1}),),
        config,
    )
    assert len(chunks) == 1
    stage_text_chunks(path, chunks, refresh_token=f"chunks:{item_id}", updated_ns=11)
    finalize_text_chunk_refresh(
        path,
        item_id=item_id,
        chunking_signature=config.signature,
        refresh_token=f"chunks:{item_id}",
        updated_ns=12,
    )
    return item, chunks[0]


def _complete_text_job(
    path: Path,
    model: EmbeddingModelSpec,
    chunk: TextChunk,
    *,
    processing_signature: str,
    vector: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0),
) -> int:
    generation = start_embedding_generation(
        path,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        started_ns=20,
    )
    enqueue_text_chunk_jobs(path, generation, (chunk.chunk_id,), now_ns=21)
    lease = claim_embedding_jobs(
        path,
        generation,
        worker_id="worker",
        limit=1,
        lease_seconds=60,
        now_ns=22,
    )[0]
    complete_embedding_job(
        path,
        lease.job_id,
        worker_id="worker",
        vector=vector,
        provenance={"fixture": "vector"},
        now_ns=23,
    )
    finalize_embedding_generation(path, generation, completed_ns=24)
    return generation


def test_text_embedding_scopes_separate_title_from_content_and_validate_empty_head(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    query = ExactSearchQuery(
        query_model_signature=model.model_signature,
        vector_space=model.vector_space,
        dimensions=model.dimensions,
        vector=(1.0, 0.0, 0.0, 0.0),
        target_modality=EmbeddingModality.TEXT,
        indexed_model_signatures=(model.model_signature,),
    )
    with pytest.raises(ValueError, match="text_scope"):
        search_exact_page(database, query, text_scope="invalid")  # type: ignore[arg-type]

    item = SemanticItem(
        item_id="item:pdf:scoped",
        source_kind="pdf",
        source_identity="scoped",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text("source-scoped"),
        path="C:/fixtures/proteccion-49T.pdf",
    )
    upsert_semantic_item(database, item, refresh_token="item-scope")
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item.item_id,
        (
            TextSection("pdf_page", "1", "contenido de transformador"),
            TextSection(
                SEMANTIC_TITLE_SECTION_KIND,
                SEMANTIC_TITLE_POLICY,
                "proteccion-49T",
                {"advisory_only": True},
            ),
        ),
        config,
    )
    assert len(chunks) == 2
    stage_text_chunks(database, chunks, refresh_token="chunks-scope")
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=config.signature,
        refresh_token="chunks-scope",
    )
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="scope-fixture-v1",
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (chunk.chunk_id for chunk in chunks),
    )
    for lease in claim_embedding_jobs(
        database,
        generation,
        worker_id="scope-worker",
        limit=2,
    ):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="scope-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            provenance={"fixture": "scope"},
        )
    finalize_embedding_generation(database, generation)

    all_page = search_exact_page(database, query, text_scope="all")
    content_page = search_exact_page(database, query, text_scope="content")
    title_page = search_exact_page(database, query, text_scope="title")
    assert (all_page.scanned, content_page.scanned, title_page.scanned) == (2, 1, 1)
    assert resolve_search_hits(database, content_page.hits)[0].section_kind == ("pdf_page")
    assert resolve_search_hits(database, title_page.hits)[0].section_kind == (
        SEMANTIC_TITLE_SECTION_KIND
    )
    assert (
        len(
            load_active_embedding_page(
                database,
                model.model_signature,
                text_scope="content",
            ).records
        )
        == 1
    )
    assert (
        len(
            load_active_embedding_page(
                database,
                model.model_signature,
                text_scope="title",
            ).records
        )
        == 1
    )


# endregion [01]


# region [02] Schema, migrations and model compatibility


def test_schema_is_explicit_current_and_integrity_checked(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "embedding_models",
            "embedding_jobs",
            "vector_payloads",
            "label_prototypes",
            "semantic_evidence",
            "text_channel_revisions",
        }.issubset(tables)


def test_v2_database_migrates_additively_to_current_v6(tmp_path: Path) -> None:
    database = tmp_path / "semantic-v2.sqlite3"
    with semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state_module._migrate_to_v1(connection, 1)
        state_module._migrate_to_v2(connection, 2)
        connection.execute("PRAGMA user_version=2")
    initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(semantic_items)")}
        assert "source_revision_json" in columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='text_channel_revisions'"""
            ).fetchone()[0]
            == 1
        )


def test_model_signatures_are_immutable_and_spaces_enforce_dimensions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    initialize_semantic_state(database)
    with pytest.raises(ValueError, match="explicit test authorization"):
        register_embedding_model(database, model)
    register_embedding_model(database, model, allow_test_provider=True)
    register_embedding_model(database, model, allow_test_provider=True)
    incompatible = _image_model(space=model.vector_space, dimensions=8)
    with pytest.raises(ValueError, match="incompatible with existing vector space"):
        register_embedding_model(database, incompatible, allow_test_provider=True)
    rebound = EmbeddingModelSpec(
        model.model_signature,
        model.vector_space,
        model.modality,
        "different/model",
        model.model_version,
        model.dimensions,
        model.provider,
        model.supported_roles,
    )
    with pytest.raises(ValueError, match="different metadata"):
        register_embedding_model(database, rebound, allow_test_provider=True)
    assert load_embedding_model(database, model.model_signature) == model


# endregion [02]


# region [03] Source and chunk refresh lifecycle


def test_item_refresh_deactivates_unseen_sources_without_deleting_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)
    first = SemanticItem("a", "pdf", "a", "v1", fingerprint_text("a"))
    second = SemanticItem("b", "pdf", "b", "v1", fingerprint_text("b"))
    assert (
        stage_semantic_items(
            database,
            (first, second),
            source_kind="pdf",
            refresh_token="r1",
            updated_ns=1,
            batch_size=1,
        )
        == 2
    )
    assert (
        stage_semantic_items(
            database,
            (first,),
            source_kind="pdf",
            refresh_token="r2",
            updated_ns=2,
        )
        == 1
    )
    assert (
        finalize_semantic_item_refresh(
            database,
            source_kind="pdf",
            refresh_token="r2",
            updated_ns=3,
        )
        == 1
    )
    assert load_semantic_item(database, "a").item_id == "a"
    with pytest.raises(KeyError, match="inactive"):
        load_semantic_item(database, "b")
    assert load_semantic_item(database, "b", include_inactive=True).item_id == "b"
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_items").fetchone()[0] == 2


def test_changed_item_content_keeps_published_snapshot_until_successor_publish(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "doc", "old transformer text")
    _complete_text_job(database, model, chunk, processing_signature="run-1")
    assert len(load_active_embedding_page(database, model.model_signature).records) == 1
    changed = SemanticItem(
        "doc",
        "pdf",
        "identity:doc",
        "fixture-v1",
        fingerprint_text("new breaker text"),
        path="C:/fixtures/doc.pdf",
    )
    upsert_semantic_item(database, changed, refresh_token="items-r2", updated_ns=30)
    assert len(load_active_embedding_page(database, model.model_signature).records) == 1
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM text_chunks WHERE item_id='doc'").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT active FROM text_chunks WHERE item_id='doc'").fetchone()[0]
            == 0
        )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="run-2-cleanup",
        started_ns=31,
    )
    finalize_embedding_generation(database, successor, completed_ns=32)
    assert load_active_embedding_page(database, model.model_signature).records == ()


# endregion [03]


# region [04] Durable jobs, reuse, exact search and streaming


def test_job_lease_exposes_payload_retry_and_cursor_state(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "doc", "relay protection settings")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="run",
        provenance={"source": "fixture"},
        cursor={"after": 0},
        started_ns=100,
    )
    assert (
        start_embedding_generation(
            database,
            model_signature=model.model_signature,
            processing_signature="run",
            provenance={"source": "fixture"},
            started_ns=101,
        )
        == generation
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=102)
    lease = claim_embedding_jobs(
        database,
        generation,
        worker_id="w1",
        now_ns=103,
        lease_seconds=10,
    )[0]
    assert lease.text == chunk.text
    assert embedding_request_from_lease(lease).text == chunk.text
    assert (
        fail_embedding_job(
            database,
            lease.job_id,
            worker_id="w1",
            error_type="temporary",
            error_message="retry",
            retryable=True,
            retry_delay_seconds=1,
            now_ns=104,
        )
        == "pending"
    )
    assert (
        claim_embedding_jobs(
            database,
            generation,
            worker_id="w2",
            now_ns=104,
        )
        == ()
    )
    second = claim_embedding_jobs(
        database,
        generation,
        worker_id="w2",
        now_ns=1_000_000_105,
    )[0]
    complete_embedding_job(
        database,
        second.job_id,
        worker_id="w2",
        vector=(2.0, 0.0, 0.0, 0.0),
        now_ns=1_000_000_106,
    )
    update_embedding_generation_cursor(database, generation, {"after": 1})
    summary = finalize_embedding_generation(
        database,
        generation,
        completed_ns=1_000_000_107,
    )
    assert summary.status == "ready"
    assert summary.done == 1
    assert summary.cursor == {"after": 1}


def test_expired_worker_failure_cannot_overwrite_reclaimed_lease(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "lease-race", "breaker diagnostics")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="lease-race",
        started_ns=10,
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,), now_ns=20)
    first = claim_embedding_jobs(
        database,
        generation,
        worker_id="expired-worker",
        lease_seconds=1,
        now_ns=100,
    )[0]
    after_expiry = first.lease_until_ns + 1
    lease_selected = threading.Event()
    reclaim_finished = threading.Event()
    real_database = semantic_generation_repository.semantic_database

    class PausingCursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            lease_selected.set()
            reclaim_finished.wait(1.0)
            return row

    class CoordinatedConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, parameters: tuple[object, ...] = ()):
            cursor = self._connection.execute(sql, parameters)
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT attempts,max_attempts,status,lease_owner"):
                return PausingCursor(cursor)
            return cursor

    @contextmanager
    def coordinated_database(path: Path, *, readonly: bool = False) -> Iterator:
        with real_database(path, readonly=readonly) as connection:
            if threading.current_thread().name == "stale-failure":
                yield CoordinatedConnection(connection)
            else:
                yield connection

    monkeypatch.setattr(
        semantic_generation_repository,
        "semantic_database",
        coordinated_database,
    )
    failure_errors: list[BaseException] = []
    reclaim_errors: list[BaseException] = []
    reclaimed = []

    def record_stale_failure() -> None:
        try:
            fail_embedding_job(
                database,
                first.job_id,
                worker_id="expired-worker",
                error_type="late_failure",
                error_message="must not replace a newer lease",
                retryable=True,
                now_ns=after_expiry,
            )
        except BaseException as exc:
            failure_errors.append(exc)

    def reclaim_expired_lease() -> None:
        try:
            assert lease_selected.wait(2.0)
            reclaimed.extend(
                claim_embedding_jobs(
                    database,
                    generation,
                    worker_id="new-worker",
                    lease_seconds=1,
                    now_ns=after_expiry,
                )
            )
        except BaseException as exc:
            reclaim_errors.append(exc)
        finally:
            reclaim_finished.set()

    reclaimer = threading.Thread(target=reclaim_expired_lease, name="lease-reclaimer")
    stale_failure = threading.Thread(target=record_stale_failure, name="stale-failure")
    reclaimer.start()
    stale_failure.start()
    stale_failure.join(3.0)
    reclaimer.join(3.0)

    assert not stale_failure.is_alive()
    assert not reclaimer.is_alive()
    assert reclaim_errors == []
    assert len(reclaimed) == 1
    assert len(failure_errors) == 1
    assert isinstance(failure_errors[0], SemanticStateError)
    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,lease_owner FROM embedding_jobs WHERE job_id=?",
            (first.job_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("leased", "new-worker")


def test_job_lease_heartbeat_extends_a_batch_atomically(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, first = _stage_text_item(database, "heartbeat-one", "transformer one")
    _, second = _stage_text_item(database, "heartbeat-two", "transformer two")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="heartbeat-run",
        started_ns=10,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (first.chunk_id, second.chunk_id),
        now_ns=20,
    )
    leases = claim_embedding_jobs(
        database,
        generation,
        worker_id="heartbeat-worker",
        limit=2,
        lease_seconds=1,
        now_ns=100,
    )
    job_ids = tuple(lease.job_id for lease in leases)
    extended_until = heartbeat_embedding_jobs(
        database,
        job_ids,
        worker_id="heartbeat-worker",
        lease_seconds=1,
        now_ns=500_000_000,
    )
    with semantic_database(database, readonly=True) as connection:
        deadlines = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT lease_until_ns FROM embedding_jobs WHERE generation_id=? ORDER BY job_id",
                (generation,),
            )
        )
    assert deadlines == (extended_until, extended_until)

    with pytest.raises(SemanticStateError, match="owned elsewhere"):
        heartbeat_embedding_jobs(
            database,
            job_ids,
            worker_id="different-worker",
            lease_seconds=2,
            now_ns=600_000_000,
        )
    with semantic_database(database, readonly=True) as connection:
        unchanged = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT lease_until_ns FROM embedding_jobs WHERE generation_id=? ORDER BY job_id",
                (generation,),
            )
        )
    assert unchanged == deadlines

    with pytest.raises(ValueError, match="unique"):
        heartbeat_embedding_jobs(
            database,
            (job_ids[0], job_ids[0]),
            worker_id="heartbeat-worker",
        )


def test_xxh3_payload_reuse_and_active_pages_avoid_duplicate_inference(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, first = _stage_text_item(database, "one", "identical transformer paragraph")
    _, second = _stage_text_item(database, "two", "identical transformer paragraph")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="reuse-run",
        started_ns=10,
    )
    enqueue_text_chunk_jobs(
        database,
        generation,
        (first.chunk_id, second.chunk_id),
        now_ns=11,
    )
    lease = claim_embedding_jobs(
        database,
        generation,
        worker_id="worker",
        limit=1,
        now_ns=12,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=13,
    )
    assert reuse_cached_jobs(database, generation, limit=10, now_ns=14) == 1
    assert (
        claim_embedding_jobs(
            database,
            generation,
            worker_id="worker",
            now_ns=15,
        )
        == ()
    )
    finalize_embedding_generation(database, generation, completed_ns=16)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM text_embeddings").fetchone()[0] == 2
    pages = tuple(iter_active_embedding_pages(database, model.model_signature, page_size=1))
    assert len(pages) == 2
    assert all(len(page.records) == 1 for page in pages)
    assert pages[-1].complete is True


def test_exact_search_is_bounded_resumable_resolved_and_space_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    other = _text_model("other-model", "other-space")
    _initialize(database, model, other)
    _, first = _stage_text_item(database, "one", "transformer one")
    _, second = _stage_text_item(database, "two", "breaker two")
    _complete_text_job(database, model, first, processing_signature="one")
    _complete_text_job(
        database,
        model,
        second,
        processing_signature="two",
        vector=(0.0, 1.0, 0.0, 0.0),
    )
    query = ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        EmbeddingModality.TEXT,
    )
    first_page = search_exact_page(database, query, limit=2, max_vectors=1)
    assert first_page.complete is False
    assert first_page.next_cursor is not None
    second_page = search_exact_page(
        database,
        query,
        limit=2,
        max_vectors=1,
        after_ref_id=first_page.next_cursor,
    )
    assert second_page.complete is True
    merged = merge_exact_search_pages((first_page, second_page), limit=2)
    assert merged[0].item_id == "one"
    resolved = resolve_search_hits(database, merged, snippet_chars=12)
    assert resolved[0].path == "C:/fixtures/one.pdf"
    assert resolved[0].snippet == "transformer "
    incompatible = ExactSearchQuery(
        other.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        EmbeddingModality.TEXT,
    )
    with pytest.raises(ValueError, match="incompatible with its registered model"):
        search_exact_page(database, incompatible)


def test_exact_search_keeps_best_chunk_per_item_before_top_k(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    item = SemanticItem(
        item_id="multi",
        source_kind="pdf",
        source_identity="multi",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text("first section second section"),
        path="C:/fixtures/multi.pdf",
    )
    upsert_semantic_item(database, item, refresh_token="items")
    config = TextChunkingConfig(
        max_chars=256,
        max_terms=64,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=32,
    )
    chunks = chunk_text_sections(
        item.item_id,
        (
            TextSection("pdf_page", "1", "transformer primary section"),
            TextSection("pdf_page", "2", "transformer secondary section"),
        ),
        config,
    )
    assert len(chunks) == 2
    stage_text_chunks(database, chunks, refresh_token="multi-chunks")
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=config.signature,
        refresh_token="multi-chunks",
    )
    _complete_text_job(
        database,
        model,
        chunks[0],
        processing_signature="multi-1",
        vector=(1.0, 0.0, 0.0, 0.0),
    )
    _complete_text_job(
        database,
        model,
        chunks[1],
        processing_signature="multi-2",
        vector=(0.99, 0.1, 0.0, 0.0),
    )
    _, other = _stage_text_item(database, "other", "breaker comparison")
    _complete_text_job(
        database,
        model,
        other,
        processing_signature="other",
        vector=(0.8, 0.6, 0.0, 0.0),
    )
    query = ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        EmbeddingModality.TEXT,
    )

    page = search_exact_page(database, query, limit=2, max_vectors=100)
    evidence_page = search_exact_evidence_page(
        database,
        query,
        limit=3,
        max_vectors=100,
    )

    assert [hit.item_id for hit in page.hits] == ["multi", "other"]
    assert page.hits[0].entity_id == chunks[0].chunk_id
    assert [hit.item_id for hit in evidence_page.hits] == [
        "multi",
        "multi",
        "other",
    ]
    assert {hit.entity_id for hit in evidence_page.hits[:2]} == {
        chunks[0].chunk_id,
        chunks[1].chunk_id,
    }


def test_vectorized_exact_scoring_preserves_scalar_scores_ties_and_provenance() -> None:
    model = _text_model(dimensions=4)
    query = ExactSearchQuery(
        model.model_signature,
        model.vector_space,
        model.dimensions,
        (1.0, 0.0, 0.0, 0.0),
        EmbeddingModality.TEXT,
    )
    rows = []
    for index in range(16):
        dtype = VectorDType.FLOAT16 if index % 2 == 0 else VectorDType.FLOAT32
        vector = (1.0, 0.0, 0.0, 0.0) if index < 2 else (1.0, float(index), 0.5, -0.25)
        payload, _ = encode_vector(vector, model.dimensions, dtype)
        rows.append(
            {
                "ref_id": index + 1,
                "entity_id": f"chunk:{index}",
                "item_id": f"item:{index // 2}",
                "model_signature": model.model_signature,
                "vector_space": model.vector_space,
                "modality": EmbeddingModality.TEXT.value,
                "generation_id": 7,
                "provenance_json": json.dumps({"fixture": index}),
                "vector_blob": payload,
                "dimensions": model.dimensions,
                "vector_dtype": dtype.value,
            }
        )
    normalized_query = (1.0, 0.0, 0.0, 0.0)

    expected = tuple(
        semantic_search_repository._exact_search_hit(row, query, normalized_query) for row in rows
    )
    actual = semantic_search_repository._exact_search_hits(
        rows,
        query,
        normalized_query,
    )

    assert [hit.ref_id for hit in actual] == [hit.ref_id for hit in expected]
    assert [hit.provenance for hit in actual] == [hit.provenance for hit in expected]
    assert actual[0].score == actual[1].score == 1.0
    assert [hit.score for hit in actual] == pytest.approx(
        [hit.score for hit in expected],
        abs=1e-12,
    )


@pytest.mark.parametrize("profile_order", (("quality", "compact"), ("compact", "quality")))
def test_distinct_chunking_profiles_remain_active_and_searchable_in_either_order(
    tmp_path: Path,
    profile_order: tuple[str, str],
) -> None:
    database = tmp_path / "semantic.sqlite3"
    models = {
        "quality": _text_model("quality-model", "quality-space"),
        "compact": _text_model("compact-model", "compact-space"),
    }
    configs = {
        "quality": TextChunkingConfig(
            max_chars=256,
            max_terms=64,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=32,
        ),
        "compact": TextChunkingConfig(
            max_chars=128,
            max_terms=32,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=24,
        ),
    }
    _initialize(database, *models.values())
    text = "maintenance record for one power transformer"
    item = SemanticItem(
        item_id="profiled-doc",
        source_kind="pdf",
        source_identity="profiled-doc",
        identity_version="fixture-v1",
        fingerprint=fingerprint_text(text),
        path="C:/fixtures/profiled-doc.pdf",
    )
    upsert_semantic_item(database, item, refresh_token="profiled-item")
    chunks_by_profile: dict[str, TextChunk] = {}
    for ordinal, profile in enumerate(profile_order):
        config = configs[profile]
        chunks = chunk_text_sections(
            item.item_id,
            (TextSection("pdf_page", "1", text),),
            config,
        )
        assert len(chunks) == 1
        chunk = chunks[0]
        chunks_by_profile[profile] = chunk
        refresh_token = f"chunks:{profile}"
        stage_text_chunks(database, chunks, refresh_token=refresh_token)
        finalize_text_chunk_refresh(
            database,
            item_id=item.item_id,
            chunking_signature=config.signature,
            refresh_token=refresh_token,
        )
        _complete_text_job(
            database,
            models[profile],
            chunk,
            processing_signature=f"profile-generation:{ordinal}:{profile}",
        )

    assert chunks_by_profile["quality"].chunk_id != chunks_by_profile["compact"].chunk_id
    with semantic_database(database, readonly=True) as connection:
        active = connection.execute(
            "SELECT chunking_signature FROM text_chunks WHERE item_id=? AND active=1",
            (item.item_id,),
        ).fetchall()
    assert {str(row[0]) for row in active} == {
        configs["quality"].signature,
        configs["compact"].signature,
    }
    for profile in ("quality", "compact"):
        model = models[profile]
        assert has_active_embeddings(database, model.model_signature)
        result = search_exact_page(
            database,
            ExactSearchQuery(
                model.model_signature,
                model.vector_space,
                model.dimensions,
                (1.0, 0.0, 0.0, 0.0),
                EmbeddingModality.TEXT,
                indexed_model_signatures=(model.model_signature,),
            ),
            limit=1,
        )
        assert tuple(hit.item_id for hit in result.hits) == (item.item_id,)


def test_text_channel_revision_change_retires_chunks_embeddings_and_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    text = "OCR text for a power transformer"
    item = SemanticItem(
        item_id="ocr-item",
        source_kind="image",
        source_identity="ocr-item",
        identity_version="fixture-v1",
        fingerprint=fingerprint_bytes(b"stable-image"),
        path="C:/fixtures/ocr-item.png",
    )
    upsert_semantic_item(database, item, refresh_token="image-item")
    assert (
        publish_text_channel_revision(
            database,
            item_id=item.item_id,
            channel="image_ocr",
            revision_token="xxh3-old",
        )
        == 0
    )
    config = TextChunkingConfig(
        max_chars=128,
        max_terms=32,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=24,
    )
    chunks = chunk_text_sections(
        item.item_id,
        (TextSection("image_ocr", "ocr", text),),
        config,
    )
    assert len(chunks) == 1
    chunk = chunks[0]
    stage_text_chunks(database, chunks, refresh_token="ocr-chunks")
    finalize_text_chunk_refresh(
        database,
        item_id=item.item_id,
        chunking_signature=config.signature,
        refresh_token="ocr-chunks",
    )
    generation = _complete_text_job(
        database,
        model,
        chunk,
        processing_signature="ocr-revision-vector",
    )
    prototype = LabelPrototype(
        "prototype:ocr-transformer",
        "industrial-electrical",
        "1",
        "transformer",
        "1",
        model.model_signature,
        model.vector_space,
        "transformador",
        fingerprint_text("transformador"),
    )
    store_label_prototype(database, prototype, (1.0, 0.0, 0.0, 0.0))
    suggestion = SemanticEvidence(
        item_id=item.item_id,
        source_entity_id=chunk.chunk_id,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
        concept_id=prototype.concept_id,
        prototype_id=prototype.prototype_id,
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        score=0.9,
        rank=1,
        generation_id=generation,
    )
    record_semantic_evidence(database, suggestion, refresh_token="ocr-evidence")

    assert (
        publish_text_channel_revision(
            database,
            item_id=item.item_id,
            channel="image_ocr",
            revision_token="xxh3-old",
        )
        == 0
    )
    assert has_active_embeddings(database, model.model_signature)
    assert list_semantic_evidence(
        database,
        item_id=item.item_id,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
    ) == (suggestion,)

    assert (
        publish_text_channel_revision(
            database,
            item_id=item.item_id,
            channel="image_ocr",
            revision_token="xxh3-new",
        )
        == 1
    )
    assert has_active_embeddings(database, model.model_signature)
    cleanup_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="ocr-revision-cleanup",
    )
    finalize_embedding_generation(database, cleanup_generation)
    assert not has_active_embeddings(database, model.model_signature)
    assert (
        list_semantic_evidence(
            database,
            item_id=item.item_id,
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
        )
        == ()
    )


# endregion [04]


# region [05] Image revisions, prototypes and advisory evidence


def test_image_lease_propagates_path_fingerprint_and_source_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _image_model()
    _initialize(database, model)
    image = tmp_path / "subestación.png"
    image.write_bytes(b"bounded-image-fixture")
    stat = image.stat()
    fingerprint = fingerprint_bytes(image.read_bytes())
    item = SemanticItem(
        "image-1",
        "image",
        "volume:file",
        "ntfs-v1",
        fingerprint,
        path=str(image),
        source_revision={
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "raw_content_xxh3_128": fingerprint.xxh3_128,
        },
    )
    upsert_semantic_item(database, item, refresh_token="images-r1", updated_ns=1)
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="image-run",
        started_ns=2,
    )
    enqueue_image_item_jobs(database, generation, (item.item_id,), now_ns=3)
    lease = claim_embedding_jobs(
        database,
        generation,
        worker_id="image-worker",
        now_ns=4,
    )[0]
    assert lease.image_path == image
    assert lease.source_revision == item.source_revision
    request = embedding_request_from_lease(lease)
    assert request.fingerprint == item.fingerprint
    assert load_semantic_item(database, item.item_id).source_revision == (item.source_revision)


def test_versioned_prototypes_and_evidence_cannot_mix_vector_spaces(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    incompatible = _text_model("other-text", "other-space")
    _initialize(database, model, incompatible)
    _, chunk = _stage_text_item(database, "doc", "power transformer maintenance")
    generation = _complete_text_job(
        database,
        model,
        chunk,
        processing_signature="classification-source",
    )
    prototype = LabelPrototype(
        "prototype:transformer:v1",
        "industrial-electrical",
        "1",
        "transformer",
        "1",
        model.model_signature,
        model.vector_space,
        "transformador de potencia",
        fingerprint_text("transformador de potencia"),
    )
    store_label_prototype(database, prototype, (1.0, 0.0, 0.0, 0.0))
    second_prototype = LabelPrototype(
        "prototype:breaker:v1",
        "industrial-electrical",
        "1",
        "breaker",
        "1",
        model.model_signature,
        model.vector_space,
        "interruptor de potencia",
        fingerprint_text("interruptor de potencia"),
    )
    assert (
        stage_label_prototypes(
            database,
            ((second_prototype, (0.0, 1.0, 0.0, 0.0)),),
            batch_size=1,
        )
        == 1
    )
    loaded = load_label_prototypes(
        database,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
        prototype_version=prototype.prototype_version,
        vector_space=model.vector_space,
    )
    assert {value.prototype.prototype_id for value in loaded} == {
        prototype.prototype_id,
        second_prototype.prototype_id,
    }
    valid = SemanticEvidence(
        item_id="doc",
        source_entity_id=chunk.chunk_id,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
        concept_id=prototype.concept_id,
        prototype_id=prototype.prototype_id,
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        score=0.91,
        rank=1,
        generation_id=generation,
    )
    record_semantic_evidence(database, valid, refresh_token="evidence-r1")
    assert list_semantic_evidence(
        database,
        item_id="doc",
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
    ) == (valid,)
    invalid = SemanticEvidence(
        item_id="doc",
        source_entity_id=chunk.chunk_id,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
        concept_id=prototype.concept_id,
        prototype_id=prototype.prototype_id,
        query_model_signature=model.model_signature,
        indexed_model_signature=incompatible.model_signature,
        vector_space=model.vector_space,
        score=0.5,
        rank=2,
    )
    with pytest.raises(ValueError, match="mix incompatible vector spaces"):
        stage_semantic_evidence(database, (invalid,), refresh_token="bad")
    assert (
        finalize_semantic_evidence_model_refresh(
            database,
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
            vector_space=model.vector_space,
            indexed_model_signature=model.model_signature,
            refresh_token="model-refresh-r2",
        )
        == 1
    )
    record_semantic_evidence(database, valid, refresh_token="evidence-r2")
    assert (
        finalize_semantic_evidence_refresh(
            database,
            item_id="doc",
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
            vector_space=model.vector_space,
            refresh_token="new-empty-refresh",
        )
        == 1
    )
    assert (
        list_semantic_evidence(
            database,
            item_id="doc",
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
        )
        == ()
    )


def test_entity_evidence_publication_atomically_replaces_and_clears_abstention(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "doc", "power transformer maintenance")
    generation = _complete_text_job(
        database,
        model,
        chunk,
        processing_signature="atomic-evidence-source",
    )
    prototypes = (
        LabelPrototype(
            "prototype:transformer:atomic",
            "industrial-electrical",
            "1",
            "transformer",
            "1",
            model.model_signature,
            model.vector_space,
            "transformador de potencia",
            fingerprint_text("transformador de potencia"),
        ),
        LabelPrototype(
            "prototype:maintenance:atomic",
            "industrial-electrical",
            "1",
            "maintenance",
            "1",
            model.model_signature,
            model.vector_space,
            "mantenimiento industrial",
            fingerprint_text("mantenimiento industrial"),
        ),
    )
    stage_label_prototypes(
        database,
        (
            (prototypes[0], (1.0, 0.0, 0.0, 0.0)),
            (prototypes[1], (0.0, 1.0, 0.0, 0.0)),
        ),
    )

    def suggestion(index: int, *, score: float, rank: int) -> SemanticEvidence:
        prototype = prototypes[index]
        return SemanticEvidence(
            item_id="doc",
            source_entity_id=chunk.chunk_id,
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
            concept_id=prototype.concept_id,
            prototype_id=prototype.prototype_id,
            query_model_signature=model.model_signature,
            indexed_model_signature=model.model_signature,
            vector_space=model.vector_space,
            score=score,
            rank=rank,
            generation_id=generation,
        )

    entity = (("doc", chunk.chunk_id),)
    old = (suggestion(0, score=0.91, rank=1), suggestion(1, score=0.82, rank=2))
    assert publish_semantic_evidence_entities(
        database,
        old,
        entities=entity,
        ontology_id="industrial-electrical",
        ontology_version="1",
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        refresh_token="reused-refresh-token",
    ) == (2, 0)

    replacement = suggestion(0, score=0.55, rank=1)
    assert publish_semantic_evidence_entities(
        database,
        (replacement,),
        entities=entity,
        ontology_id="industrial-electrical",
        ontology_version="1",
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        refresh_token="reused-refresh-token",
    ) == (1, 1)
    assert list_semantic_evidence(
        database,
        item_id="doc",
        ontology_id="industrial-electrical",
        ontology_version="1",
    ) == (replacement,)

    assert publish_semantic_evidence_entities(
        database,
        (),
        entities=entity,
        ontology_id="industrial-electrical",
        ontology_version="1",
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        refresh_token="abstained-refresh",
    ) == (0, 1)
    assert (
        list_semantic_evidence(
            database,
            item_id="doc",
            ontology_id="industrial-electrical",
            ontology_version="1",
        )
        == ()
    )


def test_query_model_migration_is_entity_atomic_when_later_page_is_interrupted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    indexed_model = _text_model("indexed-model", "shared-evidence-space")
    old_query_model = _text_model("query-model-old", "shared-evidence-space")
    new_query_model = _text_model("query-model-new", "shared-evidence-space")
    _initialize(database, indexed_model, old_query_model, new_query_model)
    entities: list[tuple[str, TextChunk, int]] = []
    for item_id in ("doc-1", "doc-2"):
        _, chunk = _stage_text_item(database, item_id, f"maintenance for {item_id}")
        generation = _complete_text_job(
            database,
            indexed_model,
            chunk,
            processing_signature=f"indexed:{item_id}",
        )
        entities.append((item_id, chunk, generation))

    def prototype(query_model: EmbeddingModelSpec, suffix: str) -> LabelPrototype:
        text = f"transformer prototype {suffix}"
        return LabelPrototype(
            f"prototype:transformer:{suffix}",
            "industrial-electrical",
            "1",
            "transformer",
            "1",
            query_model.model_signature,
            query_model.vector_space,
            text,
            fingerprint_text(text),
        )

    old_prototype = prototype(old_query_model, "old-query")
    new_prototype = prototype(new_query_model, "new-query")
    store_label_prototype(database, old_prototype, (1.0, 0.0, 0.0, 0.0))
    store_label_prototype(database, new_prototype, (1.0, 0.0, 0.0, 0.0))

    def evidence(
        entity: tuple[str, TextChunk, int],
        selected_prototype: LabelPrototype,
        query_model: EmbeddingModelSpec,
    ) -> SemanticEvidence:
        item_id, chunk, generation = entity
        return SemanticEvidence(
            item_id=item_id,
            source_entity_id=chunk.chunk_id,
            ontology_id=selected_prototype.ontology_id,
            ontology_version=selected_prototype.ontology_version,
            concept_id=selected_prototype.concept_id,
            prototype_id=selected_prototype.prototype_id,
            query_model_signature=query_model.model_signature,
            indexed_model_signature=indexed_model.model_signature,
            vector_space=indexed_model.vector_space,
            score=0.8,
            rank=1,
            generation_id=generation,
        )

    entity_ids = tuple((item_id, chunk.chunk_id) for item_id, chunk, _ in entities)
    publish_semantic_evidence_entities(
        database,
        tuple(evidence(entity, old_prototype, old_query_model) for entity in entities),
        entities=entity_ids,
        ontology_id="industrial-electrical",
        ontology_version="1",
        query_model_signature=old_query_model.model_signature,
        indexed_model_signature=indexed_model.model_signature,
        vector_space=indexed_model.vector_space,
        refresh_token="old-query-refresh",
    )

    with pytest.raises(RuntimeError, match="after first publication"):
        publish_semantic_evidence_entities(
            database,
            (evidence(entities[0], new_prototype, new_query_model),),
            entities=(entity_ids[0],),
            ontology_id="industrial-electrical",
            ontology_version="1",
            query_model_signature=new_query_model.model_signature,
            indexed_model_signature=indexed_model.model_signature,
            vector_space=indexed_model.vector_space,
            refresh_token="new-query-refresh",
        )
        raise RuntimeError("deliberate interruption after first publication")

    with semantic_database(database, readonly=True) as connection:
        active = connection.execute(
            """SELECT item_id,query_model_signature FROM semantic_evidence
            WHERE active=1 ORDER BY item_id"""
        ).fetchall()
    assert [(str(row[0]), str(row[1])) for row in active] == [
        (entities[0][0], new_query_model.model_signature),
        (entities[1][0], old_query_model.model_signature),
    ]


def test_entity_evidence_publication_enforces_bounds_and_exact_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "doc", "switchgear maintenance")
    generation = _complete_text_job(
        database,
        model,
        chunk,
        processing_signature="bounded-evidence-source",
    )
    prototype = LabelPrototype(
        "prototype:switchgear:bounded",
        "industrial-electrical",
        "1",
        "switchgear",
        "1",
        model.model_signature,
        model.vector_space,
        "tablero de media tension",
        fingerprint_text("tablero de media tension"),
    )
    store_label_prototype(database, prototype, (1.0, 0.0, 0.0, 0.0))
    valid = SemanticEvidence(
        item_id="doc",
        source_entity_id=chunk.chunk_id,
        ontology_id=prototype.ontology_id,
        ontology_version=prototype.ontology_version,
        concept_id=prototype.concept_id,
        prototype_id=prototype.prototype_id,
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        score=0.7,
        rank=1,
        generation_id=generation,
    )

    def publish(
        values: tuple[SemanticEvidence, ...],
        entities: tuple[tuple[str, str], ...],
    ) -> tuple[int, int]:
        return publish_semantic_evidence_entities(
            database,
            values,
            entities=entities,
            ontology_id=prototype.ontology_id,
            ontology_version=prototype.ontology_version,
            query_model_signature=model.model_signature,
            indexed_model_signature=model.model_signature,
            vector_space=model.vector_space,
            refresh_token="bounded-refresh",
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(state_module, "MAX_EVIDENCE_ENTITIES_PER_PUBLICATION", 1)
        with pytest.raises(ValueError, match="entity bound"):
            publish(
                (),
                (("doc", chunk.chunk_id), ("doc", "another")),
            )
    with monkeypatch.context() as scoped:
        scoped.setattr(state_module, "MAX_EVIDENCE_ROWS_PER_PUBLICATION", 1)
        with pytest.raises(ValueError, match="row bound"):
            publish(
                (valid, valid),
                (("doc", chunk.chunk_id),),
            )
    with pytest.raises(ValueError, match="outside the publication"):
        publish(
            (
                SemanticEvidence(
                    item_id=valid.item_id,
                    source_entity_id=valid.item_id,
                    ontology_id=valid.ontology_id,
                    ontology_version=valid.ontology_version,
                    concept_id=valid.concept_id,
                    prototype_id=valid.prototype_id,
                    query_model_signature=valid.query_model_signature,
                    indexed_model_signature=valid.indexed_model_signature,
                    vector_space=valid.vector_space,
                    score=valid.score,
                    rank=valid.rank,
                    generation_id=valid.generation_id,
                ),
            ),
            (("doc", chunk.chunk_id),),
        )
    with pytest.raises(ValueError, match="not active for its indexed model"):
        publish(
            (),
            (("doc", "missing-chunk"),),
        )


def test_prototype_publication_is_version_exact_atomic_and_model_scoped(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    other_model = _text_model("text-model-v2")
    _initialize(database, model, other_model)
    _, chunk = _stage_text_item(database, "doc", "transformer maintenance")
    generation = _complete_text_job(
        database,
        model,
        chunk,
        processing_signature="prototype-publication-source",
    )

    def prototype(
        prototype_id: str,
        concept_id: str,
        version: str,
        selected_model: EmbeddingModelSpec = model,
    ) -> LabelPrototype:
        text = f"prototype text {prototype_id}"
        return LabelPrototype(
            prototype_id,
            "industrial-electrical",
            "1",
            concept_id,
            version,
            selected_model.model_signature,
            selected_model.vector_space,
            text,
            fingerprint_text(text),
        )

    old = prototype("prototype:old", "transformer", "v1")
    removed = prototype("prototype:removed", "breaker", "v1")
    other_scope = prototype(
        "prototype:other-model",
        "transformer",
        "v1",
        other_model,
    )
    desired = prototype("prototype:desired", "transformer", "v2")
    for value, vector in (
        (old, (1.0, 0.0, 0.0, 0.0)),
        (removed, (0.0, 1.0, 0.0, 0.0)),
        (other_scope, (0.0, 0.0, 1.0, 0.0)),
    ):
        store_label_prototype(database, value, vector)
    stage_label_prototypes(
        database,
        ((desired, (1.0, 0.0, 0.0, 0.0)),),
        activate=False,
    )
    evidence = SemanticEvidence(
        item_id="doc",
        source_entity_id=chunk.chunk_id,
        ontology_id=old.ontology_id,
        ontology_version=old.ontology_version,
        concept_id=old.concept_id,
        prototype_id=old.prototype_id,
        query_model_signature=model.model_signature,
        indexed_model_signature=model.model_signature,
        vector_space=model.vector_space,
        score=0.8,
        rank=1,
        generation_id=generation,
    )
    record_semantic_evidence(database, evidence, refresh_token="old-evidence")

    assert {
        value.prototype.prototype_id
        for value in load_label_prototypes(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v1",
            vector_space=model.vector_space,
            model_signatures=(model.model_signature,),
        )
    } == {old.prototype_id, removed.prototype_id}
    with pytest.raises(SemanticStateError, match="incomplete"):
        finalize_label_prototype_refresh(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v2",
            vector_space=model.vector_space,
            model_signature=model.model_signature,
            active_prototype_ids=(desired.prototype_id, "prototype:missing"),
        )
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT active FROM label_prototypes WHERE prototype_id=?",
                (old.prototype_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT active FROM label_prototypes WHERE prototype_id=?",
                (desired.prototype_id,),
            ).fetchone()[0]
            == 0
        )

    assert (
        finalize_label_prototype_refresh(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v2",
            vector_space=model.vector_space,
            model_signature=model.model_signature,
            active_prototype_ids=(desired.prototype_id,),
        )
        == 2
    )
    assert tuple(
        value.prototype.prototype_id
        for value in load_label_prototypes(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v2",
            vector_space=model.vector_space,
            model_signatures=(model.model_signature,),
        )
    ) == (desired.prototype_id,)
    assert (
        load_label_prototypes(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v1",
            vector_space=model.vector_space,
            model_signatures=(model.model_signature,),
        )
        == ()
    )
    assert tuple(
        value.prototype.prototype_id
        for value in load_label_prototypes(
            database,
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
            prototype_version="v1",
            vector_space=model.vector_space,
            model_signatures=(other_model.model_signature,),
        )
    ) == (other_scope.prototype_id,)
    assert (
        list_semantic_evidence(
            database,
            item_id="doc",
            ontology_id=old.ontology_id,
            ontology_version=old.ontology_version,
        )
        == ()
    )


def test_generation_refuses_unfinished_or_terminal_errors(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model()
    _initialize(database, model)
    _, chunk = _stage_text_item(database, "doc", "switchgear")
    generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="errors",
    )
    enqueue_text_chunk_jobs(database, generation, (chunk.chunk_id,))
    with pytest.raises(SemanticStateError, match="unfinished"):
        finalize_embedding_generation(database, generation)
    lease = claim_embedding_jobs(database, generation, worker_id="w")[0]
    assert (
        fail_embedding_job(
            database,
            lease.job_id,
            worker_id="w",
            error_type="model_error",
            error_message="terminal",
            retryable=False,
        )
        == "error"
    )
    assert generation_summary(database, generation).errors == 1
    with pytest.raises(SemanticStateError, match="has 1 errors"):
        finalize_embedding_generation(database, generation)
    summary = finalize_embedding_generation(database, generation, allow_partial=True)
    assert summary.status == "ready_partial"


# endregion [05]
