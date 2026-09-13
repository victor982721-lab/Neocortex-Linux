"""Adversarial coverage for the v8/v9 Semantic generation-control projection.

The tests keep the existing generation/receipt contracts as their oracle while
exercising the v8 physical counters, source-dirty fanout and cache-payload
hints through the current v9 owner.  All
state is temporary and owner-local; no production database or corpus is used.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from neocortex.semantic import semantic_schema
from neocortex.semantic.semantic_generation_repository import _has_generation_job_control
from neocortex.semantic.semantic_generation_worker import complete_embedding_jobs_batch
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingJobLease,
    EmbeddingModelSpec,
    SemanticItem,
    TextChunk,
    fingerprint_bytes,
)
from neocortex.semantic.semantic_repository_common import MAX_WRITE_BATCH
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    StaleEmbeddingJobError,
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_image_item_jobs,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    generation_summary,
    initialize_semantic_state,
    reuse_cached_jobs,
    semantic_database,
    start_embedding_generation,
    upsert_semantic_item,
)
from tests.test_semantic_generation_publication_v6 import _create_populated_v5
from tests.test_semantic_state import (
    _image_model,
    _initialize,
    _stage_text_item,
    _text_model,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _text_fixture(
    tmp_path: Path,
    *,
    count: int = 1,
    max_attempts: int = 3,
    processing_signature: str = "projection-v1",
) -> tuple[Path, int, EmbeddingModelSpec, tuple[TextChunk, ...]]:
    database = tmp_path / "semantic.sqlite3"
    model = _text_model("projection-text-v1", "projection-text-space-v1")
    _initialize(database, model)
    chunks = tuple(
        _stage_text_item(
            database,
            f"projection-item-{index}",
            f"Contenido controlado de Semantic para la fuente {index}.",
            refresh=f"projection-refresh-{index}",
        )[1]
        for index in range(count)
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        provenance={"fixture": "generation-control-projection", "source": "pdf"},
        started_ns=100,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            tuple(chunk.chunk_id for chunk in chunks),
            max_attempts=max_attempts,
            now_ns=101,
        )
        == count
    )
    return database, generation_id, model, chunks


@pytest.mark.parametrize("operation", ("summary", "claim", "reuse"))
@pytest.mark.parametrize("malformation", ("future", "missing_metadata", "conflicting_metadata"))
def test_generation_control_rejects_unknown_or_inconsistent_owner_before_transition(
    tmp_path: Path, operation: str, malformation: str,
) -> None:
    database, generation_id, _model, _chunks = _text_fixture(tmp_path)
    with semantic_database(database) as connection:
        if malformation == "future":
            connection.execute("PRAGMA user_version=10")
            connection.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")
        elif malformation == "missing_metadata":
            connection.execute("DELETE FROM metadata WHERE key='schema_version'")
        else:
            connection.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")
    before = database.read_bytes()
    with pytest.raises(SemanticStateError):
        if operation == "summary":
            generation_summary(database, generation_id)
        elif operation == "claim":
            claim_embedding_jobs(database, generation_id, worker_id="must-not-lease", now_ns=200)
        else:
            reuse_cached_jobs(database, generation_id, now_ns=200)
    assert database.read_bytes() == before


def _claim(
    database: Path,
    generation_id: int,
    *,
    worker_id: str,
    limit: int = 1,
    now_ns: int = 200,
    lease_seconds: float = 60.0,
) -> tuple[EmbeddingJobLease, ...]:
    leases = claim_embedding_jobs(
        database,
        generation_id,
        worker_id=worker_id,
        limit=limit,
        lease_seconds=lease_seconds,
        now_ns=now_ns,
    )
    assert len(leases) == limit
    return leases


def _output(
    lease: EmbeddingJobLease,
    vector: Sequence[float] = (1.0, 0.0, 0.0, 0.0),
) -> BackendEmbedding:
    return BackendEmbedding(
        str(lease.job_id),
        tuple(vector),
        {"fixture": "generation-control-projection"},
    )


def _job_counts(database: Path, generation_id: int) -> tuple[int, int, int, int, int]:
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_jobs "
                    "WHERE generation_id=? AND status=?",
                    (generation_id, status),
                ).fetchone()[0]
            )
            for status in ("pending", "leased", "done", "error", "stale")
        )


def _assert_summary_matches_jobs(database: Path, generation_id: int) -> None:
    summary = generation_summary(database, generation_id)
    assert (summary.pending, summary.leased, summary.done, summary.errors, summary.stale) == (
        _job_counts(database, generation_id)
    )


def _stage_same_text(
    database: Path,
    item_id: str,
    text: str,
    *,
    refresh: str,
) -> TextChunk:
    return _stage_text_item(
        database,
        item_id,
        text,
        refresh=refresh,
    )[1]


def _delete_reinsert_text_chunk(database: Path, chunk: TextChunk) -> None:
    columns = (
        "chunk_id",
        "item_id",
        "ordinal",
        "section_kind",
        "section_id",
        "start_char",
        "end_char",
        "text_zlib",
        "text_chars",
        "content_xxh3_128",
        "content_bytes",
        "content_xxh3_64_guard",
        "chunking_signature",
        "provenance_json",
        "refresh_token",
        "active",
        "updated_ns",
    )
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT " + ",".join(columns) + " FROM text_chunks WHERE chunk_id=?",
            (chunk.chunk_id,),
        ).fetchone()
        assert row is not None
        connection.execute("DELETE FROM text_chunks WHERE chunk_id=?", (chunk.chunk_id,))
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            "INSERT INTO text_chunks(" + ",".join(columns) + ") VALUES(" + placeholders + ")",
            tuple(row[column] for column in columns),
        )
        connection.commit()
    finally:
        connection.close()


def _delete_reinsert_image_item(database: Path, item_id: str) -> None:
    columns = (
        "item_id",
        "source_kind",
        "source_identity",
        "identity_version",
        "path",
        "content_xxh3_128",
        "content_bytes",
        "content_xxh3_64_guard",
        "provenance_json",
        "source_revision_json",
        "refresh_token",
        "active",
        "updated_ns",
    )
    # The temporary connection intentionally reconstructs the row with its
    # foreign-key references disabled; the row is restored before commit, so
    # the resulting fixture is consistent while exercising both triggers.
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        row = connection.execute(
            "SELECT " + ",".join(columns) + " FROM semantic_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        assert row is not None
        connection.execute("DELETE FROM semantic_items WHERE item_id=?", (item_id,))
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            "INSERT INTO semantic_items(" + ",".join(columns) + ") VALUES(" + placeholders + ")",
            tuple(row[column] for column in columns),
        )
        connection.commit()
    finally:
        connection.close()


def _image_fixture(tmp_path: Path) -> tuple[Path, int, EmbeddingModelSpec, SemanticItem, Path]:
    database = tmp_path / "semantic-image.sqlite3"
    model = _image_model("projection-image-v1", "projection-image-space-v1")
    _initialize(database, model)
    image_path = tmp_path / "projection-source.bin"
    image_path.write_bytes(b"projection-image-fixture")
    fingerprint = fingerprint_bytes(image_path.read_bytes())
    item = SemanticItem(
        "projection-image-item",
        "image",
        "projection-image-identity",
        "projection-image-source-v1",
        fingerprint,
        path=str(image_path),
        provenance={"fixture": "generation-control-projection"},
        source_revision={"revision_id": "projection-image-r1"},
    )
    upsert_semantic_item(database, item, refresh_token="projection-image-r1", updated_ns=10)
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="projection-image-v1",
        provenance={"fixture": "generation-control-projection", "source": "image"},
        started_ns=20,
    )
    assert enqueue_image_item_jobs(database, generation_id, (item.item_id,), now_ns=21) == 1
    return database, generation_id, model, item, image_path


def test_current_v9_schema_persists_v8_control_columns_indexes_triggers_and_metadata(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    initialize_semantic_state(database)

    expected_triggers = {
        "embedding_jobs_generation_counts_insert",
        "embedding_jobs_generation_counts_update",
        "embedding_jobs_generation_counts_delete",
        "embedding_jobs_control_insert",
        "embedding_jobs_control_update",
        "vector_payloads_embedding_jobs_cache_hint",
        "semantic_items_embedding_jobs_source_dirty_insert",
        "semantic_items_embedding_jobs_source_dirty_update",
        "semantic_items_embedding_jobs_source_dirty_delete",
        "text_chunks_embedding_jobs_source_dirty_insert",
        "text_chunks_embedding_jobs_source_dirty_update",
        "text_chunks_embedding_jobs_source_dirty_delete",
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "9"
        assert tuple(
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == tuple(range(1, 10))
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(embedding_jobs)")
        }
        assert columns["source_dirty"] == ("INTEGER", 1, "1")
        assert columns["cached_payload_id"] == ("INTEGER", 0, None)
        foreign_keys = tuple(connection.execute("PRAGMA foreign_key_list(embedding_jobs)"))
        assert any(
            str(row[2]) == "vector_payloads" and str(row[3]) == "cached_payload_id"
            for row in foreign_keys
        )
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(embedding_jobs)")
        }
        assert {
            "embedding_jobs_source_dirty_idx",
            "embedding_jobs_cached_pending_idx",
            "embedding_jobs_source_idx",
            "embedding_jobs_lease_expiry_idx",
        } <= indexes
        triggers = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert expected_triggers <= set(triggers)
        assert all(triggers[name].startswith("CREATE TRIGGER") for name in expected_triggers)


def test_current_v9_initialization_rejects_missing_control_trigger_without_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-trigger.sqlite3"
    initialize_semantic_state(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER embedding_jobs_control_insert")
        connection.commit()
    after_drop = database.read_bytes()

    with pytest.raises(SemanticStateError, match="schema contract"):
        initialize_semantic_state(database)
    assert database.read_bytes() == after_drop
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name='embedding_jobs_control_insert'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("version", "controlled"),
    ((7, False), (8, True), (9, True)),
)
def test_generation_control_gate_covers_v8_v9_not_legacy_v7(
    version: int,
    controlled: bool,
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        semantic_schema._build_exact_schema(connection, version)
        semantic_schema._store_schema_version(connection, version)
        assert _has_generation_job_control(connection) is controlled
    finally:
        connection.close()


def _migrate_v5_to_v7(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        semantic_schema._migrate_to_v6(connection, 6)
        semantic_schema._migrate_to_v7(connection, 7)
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            "UPDATE metadata SET value='7' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()


def test_v7_building_bootstrap_reconciles_counters_and_preserves_v7_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v7-building.sqlite3"
    model, _base_chunk = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    new_chunk = _stage_same_text(
        database,
        "v7-building-item",
        "Nueva entrada de generación v7 para bootstrap de counters.",
        refresh="v7-building-refresh",
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="v7-building-projection",
        provenance={"fixture": "v7-building"},
        started_ns=40,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (new_chunk.chunk_id,), now_ns=41) == 1
    lease = _claim(
        database,
        generation_id,
        worker_id="v7-worker",
        now_ns=42,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="v7-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=43,
    )
    with sqlite3.connect(database) as connection:
        old_receipt = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding'",
            (generation_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE embedding_generations SET pending_count=0,leased_count=0,"
            "done_count=0,error_count=0,stale_count=0 WHERE generation_id=?",
            (generation_id,),
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7

    legacy_summary = generation_summary(database, generation_id)
    assert (
        legacy_summary.pending,
        legacy_summary.leased,
        legacy_summary.done,
        legacy_summary.errors,
        legacy_summary.stale,
    ) == (0, 0, 1, 0, 0)

    initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        generation = connection.execute(
            "SELECT status,pending_count,leased_count,done_count,error_count,stale_count "
            "FROM embedding_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT status,source_dirty,cached_payload_id FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()
        new_receipt = connection.execute(
            "SELECT receipt_json FROM semantic_work_receipts WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()[0]
    assert generation is not None and tuple(generation) == ("building", 0, 0, 1, 0, 0)
    assert job is not None and str(job[0]) == "done" and int(job[1]) == 0
    assert job[2] is not None
    assert json.loads(str(old_receipt)) == json.loads(str(new_receipt))
    assert json.loads(str(new_receipt))["runtime"]["semantic_schema"] == "7"
    _assert_summary_matches_jobs(database, generation_id)


def test_v7_terminal_member_only_head_preserves_stored_counts_after_v8(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v7-terminal.sqlite3"
    model, _chunk = _create_populated_v5(database)
    _migrate_v5_to_v7(database)
    initialize_semantic_state(database)

    with semantic_database(database, readonly=True) as connection:
        generation = connection.execute(
            "SELECT generation_id,status,pending_count,leased_count,done_count,"
            "error_count,stale_count FROM embedding_generations "
            "WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
        jobs = int(connection.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0])
        members = int(
            connection.execute("SELECT COUNT(*) FROM embedding_generation_members").fetchone()[0]
        )
    assert generation is not None
    assert tuple(generation[1:]) == ("ready", 0, 0, 1, 0, 0)
    assert (jobs, members) == (0, 1)
    summary = generation_summary(database, int(generation[0]))
    assert (summary.status, summary.pending, summary.leased, summary.done) == (
        "ready",
        0,
        0,
        1,
    )


def test_current_v9_receipt_metadata_is_truthful_and_refs_remain_structured(
    tmp_path: Path,
) -> None:
    database, generation_id, model, _chunks = _text_fixture(tmp_path)
    lease = _claim(database, generation_id, worker_id="v8-receipt-worker")[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="v8-receipt-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=201,
    )
    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,execution_mode,generation_id,model_signature,job_id,receipt_json "
            "FROM semantic_work_receipts WHERE job_id=? AND stage_id='semantic.embedding'",
            (lease.job_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row)[:5] == (
        "succeeded",
        "executed",
        generation_id,
        model.model_signature,
        lease.job_id,
    )
    receipt = json.loads(str(row[5]))
    assert receipt["runtime"]["semantic_schema"] == "9"
    assert receipt["stage"]["stage_id"] == "semantic.embedding"
    assert receipt["outcome"] == "succeeded"
    assert len(receipt["outputs"]) == 2
    assert {item["name"] for item in receipt["inputs"]} >= {
        "semantic_item_snapshot",
        "semantic_chunk_snapshot",
    }
    assert receipt["outputs"][0]["materialization"]["materialization_kind"] == (
        "semantic_vector_payload"
    )
    assert receipt["outputs"][1]["materialization"]["materialization_kind"] == (
        "semantic_embedding_member"
    )


def test_v8_counter_triggers_cover_status_generation_moves_upserts_and_delete(
    tmp_path: Path,
) -> None:
    database, first_generation, model, chunks = _text_fixture(tmp_path)
    second_generation = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="projection-counter-second",
        provenance={"fixture": "counter-second"},
        materialize_base=False,
        started_ns=300,
    )
    _assert_summary_matches_jobs(database, first_generation)
    _assert_summary_matches_jobs(database, second_generation)
    with semantic_database(database, readonly=True) as connection:
        job = connection.execute(
            "SELECT * FROM embedding_jobs WHERE generation_id=?",
            (first_generation,),
        ).fetchone()
    assert job is not None

    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET generation_id=?,status='leased',"
            "attempts=1,attempt_sequence=1,lease_owner='manual-counter-worker',"
            "lease_until_ns=100000000000000000000,attempt_started_ns=1 WHERE job_id=?",
            (second_generation, int(job["job_id"])),
        )
    assert _job_counts(database, first_generation) == (0, 0, 0, 0, 0)
    assert _job_counts(database, second_generation) == (0, 1, 0, 0, 0)
    _assert_summary_matches_jobs(database, first_generation)
    _assert_summary_matches_jobs(database, second_generation)

    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET status='pending',lease_owner=NULL,"
            "lease_until_ns=NULL,attempt_started_ns=NULL WHERE job_id=?",
            (int(job["job_id"]),),
        )
        connection.execute(
            "UPDATE embedding_jobs SET status='error' WHERE job_id=?",
            (int(job["job_id"]),),
        )
    assert _job_counts(database, second_generation) == (0, 0, 0, 1, 0)
    _assert_summary_matches_jobs(database, second_generation)

    assert enqueue_text_chunk_jobs(
        database,
        second_generation,
        (chunks[0].chunk_id,),
        now_ns=310,
    ) == 1
    assert _job_counts(database, second_generation) == (1, 0, 0, 0, 0)
    _assert_summary_matches_jobs(database, second_generation)

    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET status='stale',lease_owner=NULL,"
            "lease_until_ns=NULL WHERE job_id=?",
            (int(job["job_id"]),),
        )
    assert _job_counts(database, second_generation) == (0, 0, 0, 0, 1)
    _assert_summary_matches_jobs(database, second_generation)
    assert enqueue_text_chunk_jobs(
        database,
        second_generation,
        (chunks[0].chunk_id,),
        now_ns=311,
    ) == 1
    assert _job_counts(database, second_generation) == (1, 0, 0, 0, 0)

    lease = _claim(
        database,
        second_generation,
        worker_id="counter-worker",
        now_ns=312,
    )[0]
    assert _job_counts(database, second_generation) == (0, 1, 0, 0, 0)
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET status='done',lease_owner=NULL,"
            "lease_until_ns=NULL WHERE job_id=?",
            (lease.job_id,),
        )
    assert _job_counts(database, second_generation) == (0, 0, 1, 0, 0)
    assert enqueue_text_chunk_jobs(
        database,
        second_generation,
        (chunks[0].chunk_id,),
        now_ns=313,
    ) == 1
    assert _job_counts(database, second_generation) == (1, 0, 0, 0, 0)

    retry_lease = _claim(
        database,
        second_generation,
        worker_id="counter-retry-worker",
        now_ns=314,
    )[0]
    assert fail_embedding_job(
        database,
        retry_lease.job_id,
        worker_id="counter-retry-worker",
        error_type="counter_retry",
        error_message="fixture retry",
        retryable=True,
        now_ns=315,
    ) == "pending"
    assert _job_counts(database, second_generation) == (1, 0, 0, 0, 0)
    retry_again = _claim(
        database,
        second_generation,
        worker_id="counter-terminal-worker",
        now_ns=316,
    )[0]
    assert fail_embedding_job(
        database,
        retry_again.job_id,
        worker_id="counter-terminal-worker",
        error_type="counter_terminal",
        error_message="fixture terminal",
        retryable=False,
        now_ns=317,
    ) == "error"
    assert _job_counts(database, second_generation) == (0, 0, 0, 1, 0)
    assert enqueue_text_chunk_jobs(
        database,
        second_generation,
        (chunks[0].chunk_id,),
        now_ns=318,
    ) == 1
    assert _job_counts(database, second_generation) == (1, 0, 0, 0, 0)
    with semantic_database(database) as connection:
        connection.execute(
            "DELETE FROM embedding_jobs WHERE generation_id=? AND job_id=?",
            (second_generation, retry_again.job_id),
        )
    assert _job_counts(database, second_generation) == (0, 0, 0, 0, 0)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v8_batch_savepoint_updates_projection_for_success_and_error(
    tmp_path: Path,
) -> None:
    database, generation_id, _model, _chunks = _text_fixture(tmp_path, count=2)
    leases = _claim(
        database,
        generation_id,
        worker_id="projection-batch-worker",
        limit=2,
    )
    assert complete_embedding_jobs_batch(
        database,
        leases,
        ((0, _output(leases[0])), (1, _output(leases[1], (1.0, 0.0)))),
        worker_id="projection-batch-worker",
        now_ns=250,
    ) == (1, 1)
    assert _job_counts(database, generation_id) == (0, 0, 1, 1, 0)
    _assert_summary_matches_jobs(database, generation_id)


def test_v8_full_transaction_rollback_preserves_lease_counters_and_receipts(
    tmp_path: Path,
) -> None:
    database, generation_id, _model, _chunks = _text_fixture(tmp_path, count=1)
    lease = _claim(database, generation_id, worker_id="rollback-worker")[0]
    with semantic_database(database) as connection:
        connection.execute(
            """CREATE TRIGGER fixture_abort_done
            BEFORE UPDATE OF status ON embedding_jobs
            WHEN NEW.status='done'
            BEGIN SELECT RAISE(ABORT,'fixture full rollback'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="fixture full rollback"):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="rollback-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=251,
        )
    with semantic_database(database, readonly=True) as connection:
        job = connection.execute(
            "SELECT status,lease_owner,lease_until_ns FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()
        payloads = int(connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0])
        receipts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert job is not None and str(job[0]) == "leased" and str(job[1]) == "rollback-worker"
    assert job[2] is not None
    assert (payloads, receipts) == (0, 0)
    assert _job_counts(database, generation_id) == (0, 1, 0, 0, 0)
    _assert_summary_matches_jobs(database, generation_id)
    with semantic_database(database) as connection:
        connection.execute("DROP TRIGGER fixture_abort_done")
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id="rollback-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=252,
    )
    assert _job_counts(database, generation_id) == (0, 0, 1, 0, 0)


def test_v8_finalization_forces_full_stale_reconcile_when_hints_and_counters_lie(
    tmp_path: Path,
) -> None:
    database, generation_id, _model, chunks = _text_fixture(tmp_path, count=1)
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE text_chunks SET active=0 WHERE chunk_id=?",
            (chunks[0].chunk_id,),
        )
        connection.execute(
            "UPDATE embedding_jobs SET source_dirty=0 WHERE generation_id=?",
            (generation_id,),
        )
        connection.execute(
            "UPDATE embedding_generations SET pending_count=0,leased_count=0,done_count=0,"
            "error_count=0,stale_count=0 WHERE generation_id=?",
            (generation_id,),
        )

    with pytest.raises(SemanticStateError, match="stale jobs"):
        finalize_embedding_generation(database, generation_id, completed_ns=260)
    with semantic_database(database, readonly=True) as connection:
        rolled_back = connection.execute(
            "SELECT status,source_dirty FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        generation = connection.execute(
            "SELECT status,pending_count,stale_count FROM embedding_generations "
            "WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        head_count = int(
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
        )
    assert rolled_back is not None and tuple(rolled_back) == ("pending", 0)
    assert generation is not None and tuple(generation) == ("building", 0, 0)
    assert head_count == 0

    partial = finalize_embedding_generation(
        database,
        generation_id,
        allow_partial=True,
        completed_ns=261,
    )
    assert partial.status == "ready_partial"
    with semantic_database(database, readonly=True) as connection:
        committed = connection.execute(
            "SELECT status,source_dirty FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        counts = connection.execute(
            "SELECT pending_count,leased_count,done_count,error_count,stale_count "
            "FROM embedding_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        head_count = int(
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
        )
    assert committed is not None and tuple(committed) == ("stale", 0)
    assert counts is not None and tuple(counts) == (0, 0, 0, 0, 1)
    assert head_count == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (("active", "stale"), ("fingerprint", "stale"), ("delete_reinsert", "leased")),
)
def test_text_source_dirty_fanout_handles_active_fingerprint_and_delete_reinsert(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    database, generation_id, _model, chunks = _text_fixture(tmp_path, count=1)
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET source_dirty=0 WHERE generation_id=?",
            (generation_id,),
        )
    if mutation == "active":
        with semantic_database(database) as connection:
            connection.execute(
                "UPDATE text_chunks SET active=0 WHERE chunk_id=?",
                (chunks[0].chunk_id,),
            )
    elif mutation == "fingerprint":
        with semantic_database(database) as connection:
            connection.execute(
                "UPDATE text_chunks SET content_xxh3_128=? WHERE chunk_id=?",
                ("0" * 32, chunks[0].chunk_id),
            )
    else:
        _delete_reinsert_text_chunk(database, chunks[0])
    with semantic_database(database, readonly=True) as connection:
        dirty = int(
            connection.execute(
                "SELECT source_dirty FROM embedding_jobs WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
    assert dirty == 1

    if expected == "stale":
        assert (
            claim_embedding_jobs(
                database,
                generation_id,
                worker_id="text-dirty-worker",
                now_ns=270,
            )
            == ()
        )
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT status,source_dirty FROM embedding_jobs WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            receipt_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_work_receipts "
                    "WHERE generation_id=? AND stage_id='semantic.embedding'",
                    (generation_id,),
                ).fetchone()[0]
            )
        assert row is not None and tuple(row) == ("stale", 0)
        assert receipt_count == 0
    else:
        lease = _claim(database, generation_id, worker_id="text-dirty-worker", now_ns=270)[0]
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="text-dirty-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=271,
        )
        with semantic_database(database, readonly=True) as connection:
            status = connection.execute(
                "SELECT status FROM embedding_jobs WHERE job_id=?",
                (lease.job_id,),
            ).fetchone()[0]
        assert status == "done"
    _assert_summary_matches_jobs(database, generation_id)


def test_text_leased_source_dirty_keeps_the_stale_attempt_receipt(
    tmp_path: Path,
) -> None:
    database, generation_id, _model, chunks = _text_fixture(tmp_path, count=1)
    lease = _claim(database, generation_id, worker_id="leased-dirty-worker", now_ns=280)[0]
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE text_chunks SET active=0 WHERE chunk_id=?",
            (chunks[0].chunk_id,),
        )
        dirty = connection.execute(
            "SELECT source_dirty FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()[0]
    assert dirty == 1
    with pytest.raises(StaleEmbeddingJobError, match="source changed"):
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="leased-dirty-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=281,
        )
    with semantic_database(database, readonly=True) as connection:
        job = connection.execute(
            "SELECT status,source_dirty,lease_owner FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()
        receipt = connection.execute(
            "SELECT status,execution_mode,attempt FROM semantic_work_receipts "
            "WHERE job_id=? AND stage_id='semantic.embedding'",
            (lease.job_id,),
        ).fetchone()
    assert job is not None and tuple(job) == ("stale", 0, None)
    assert receipt is not None and tuple(receipt) == ("failed", "attempted", 1)
    _assert_summary_matches_jobs(database, generation_id)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (("active", "stale"), ("fingerprint", "stale"), ("delete_reinsert", "leased")),
)
def test_image_source_dirty_fanout_handles_item_mutations(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    database, generation_id, _model, item, _image_path = _image_fixture(tmp_path)
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET source_dirty=0 WHERE generation_id=?",
            (generation_id,),
        )
    if mutation == "active":
        with semantic_database(database) as connection:
            connection.execute(
                "UPDATE semantic_items SET active=0 WHERE item_id=?",
                (item.item_id,),
            )
    elif mutation == "fingerprint":
        with semantic_database(database) as connection:
            connection.execute(
                "UPDATE semantic_items SET content_xxh3_128=? WHERE item_id=?",
                ("0" * 32, item.item_id),
            )
    else:
        _delete_reinsert_image_item(database, item.item_id)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT source_dirty FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == 1

    if expected == "stale":
        assert (
            claim_embedding_jobs(
                database,
                generation_id,
                worker_id="image-dirty-worker",
                now_ns=290,
            )
            == ()
        )
        with semantic_database(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT status,source_dirty FROM embedding_jobs WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
        assert row is not None and tuple(row) == ("stale", 0)
    else:
        lease = _claim(database, generation_id, worker_id="image-dirty-worker", now_ns=290)[0]
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="image-dirty-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=291,
        )
        with semantic_database(database, readonly=True) as connection:
            assert connection.execute(
                "SELECT status FROM embedding_jobs WHERE job_id=?",
                (lease.job_id,),
            ).fetchone()[0] == "done"
    _assert_summary_matches_jobs(database, generation_id)


@pytest.mark.parametrize(
    ("new_path", "expected"),
    ((None, "stale"), ("/fixtures/projection-image-moved.bin", "done")),
)
def test_image_path_nullability_change_is_stale_but_nonnull_move_is_safe(
    tmp_path: Path,
    new_path: str | None,
    expected: str,
) -> None:
    database, generation_id, _model, item, _image_path = _image_fixture(tmp_path)
    from neocortex.semantic.semantic_state import upsert_semantic_item

    lease = _claim(database, generation_id, worker_id="image-path-worker", now_ns=300)[0]
    moved = SemanticItem(
        item.item_id,
        item.source_kind,
        item.source_identity,
        item.identity_version,
        item.fingerprint,
        path=new_path,
        provenance={"fixture": "image-path-move"},
        source_revision={"revision_id": "projection-image-r2"},
    )
    upsert_semantic_item(
        database,
        moved,
        refresh_token="projection-image-r2",
        updated_ns=301,
    )
    if expected == "stale":
        with pytest.raises(StaleEmbeddingJobError, match="source changed"):
            complete_embedding_job(
                database,
                lease.job_id,
                worker_id="image-path-worker",
                vector=(1.0, 0.0, 0.0, 0.0),
                now_ns=302,
            )
        status = "stale"
    else:
        complete_embedding_job(
            database,
            lease.job_id,
            worker_id="image-path-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=302,
        )
        status = "done"
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT status FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()[0] == status
    _assert_summary_matches_jobs(database, generation_id)


def _published_text_generation(
    database: Path,
    model: EmbeddingModelSpec,
    *,
    item_id: str,
    text: str,
    processing_signature: str,
    updated_ns: int,
) -> tuple[int, TextChunk, int]:
    chunk = _stage_same_text(
        database,
        item_id,
        text,
        refresh=f"{processing_signature}-refresh",
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature=processing_signature,
        provenance={"fixture": processing_signature},
        started_ns=updated_ns + 10,
    )
    assert (
        enqueue_text_chunk_jobs(
            database,
            generation_id,
            (chunk.chunk_id,),
            now_ns=updated_ns + 11,
        )
        == 1
    )
    lease = _claim(
        database,
        generation_id,
        worker_id=f"{processing_signature}-worker",
        now_ns=updated_ns + 12,
    )[0]
    complete_embedding_job(
        database,
        lease.job_id,
        worker_id=f"{processing_signature}-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=updated_ns + 13,
    )
    assert (
        finalize_embedding_generation(
            database,
            generation_id,
            completed_ns=updated_ns + 14,
        ).status
        == "ready"
    )
    with semantic_database(database, readonly=True) as connection:
        payload_id = int(
            connection.execute(
                "SELECT payload_id FROM embedding_generation_members "
                "WHERE generation_id=? AND entity_id=?",
                (generation_id, chunk.chunk_id),
            ).fetchone()[0]
        )
    return generation_id, chunk, payload_id


def test_cached_payload_hint_is_populated_on_job_insert_and_reused_safely(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cache-hint.sqlite3"
    model = _text_model("cache-hint-text-v1", "cache-hint-space-v1")
    _initialize(database, model)
    text = "Contenido idéntico para cache hint preexistente Semantic."
    _base_generation, original, payload_id = _published_text_generation(
        database,
        model,
        item_id="cache-hint-original",
        text=text,
        processing_signature="cache-hint-base",
        updated_ns=10,
    )
    replacement = _stage_same_text(
        database,
        "cache-hint-replacement",
        text,
        refresh="cache-hint-replacement-refresh",
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cache-hint-successor",
        provenance={"fixture": "cache-hint-successor"},
        started_ns=50,
    )
    assert enqueue_text_chunk_jobs(database, successor, (replacement.chunk_id,), now_ns=51) == 1
    with semantic_database(database, readonly=True) as connection:
        job = connection.execute(
            "SELECT status,source_dirty,cached_payload_id FROM embedding_jobs "
            "WHERE generation_id=? AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()
    assert job is not None and tuple(job) == ("pending", 1, payload_id)
    assert reuse_cached_jobs(database, successor, now_ns=52) == 1
    with semantic_database(database, readonly=True) as connection:
        member = connection.execute(
            "SELECT payload_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()
        hint = connection.execute(
            "SELECT cached_payload_id FROM embedding_jobs WHERE generation_id=? AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()[0]
    assert member is not None and int(member[0]) == payload_id
    assert int(hint) == payload_id
    assert original.chunk_id != replacement.chunk_id


def test_payload_insert_fanout_reuses_pending_old_job_id_between_batches(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cache-between-batches.sqlite3"
    model = _text_model("cache-between-text-v1", "cache-between-space-v1")
    _initialize(database, model)
    text = "Contenido duplicado para fanout de payload entre batches Semantic."
    first = _stage_same_text(
        database,
        "cache-between-first",
        text,
        refresh="cache-between-first-refresh",
    )
    second = _stage_same_text(
        database,
        "cache-between-second",
        text,
        refresh="cache-between-second-refresh",
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cache-between-generation",
        provenance={"fixture": "cache-between"},
        started_ns=30,
    )
    assert enqueue_text_chunk_jobs(
        database,
        generation_id,
        (first.chunk_id, second.chunk_id),
        now_ns=31,
    ) == 2
    first_lease = _claim(
        database,
        generation_id,
        worker_id="cache-between-worker",
        limit=1,
        now_ns=32,
    )[0]
    complete_embedding_job(
        database,
        first_lease.job_id,
        worker_id="cache-between-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=33,
    )
    with semantic_database(database, readonly=True) as connection:
        pending = connection.execute(
            "SELECT job_id,status,cached_payload_id FROM embedding_jobs "
            "WHERE generation_id=? AND job_id<>?",
            (generation_id, first_lease.job_id),
        ).fetchone()
    assert pending is not None and str(pending[1]) == "pending" and pending[2] is not None
    assert reuse_cached_jobs(database, generation_id, now_ns=34) == 1
    assert _job_counts(database, generation_id) == (0, 0, 2, 0, 0)
    _assert_summary_matches_jobs(database, generation_id)


def test_cache_hint_tamper_and_job_key_change_cannot_authorize_wrong_payload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cache-tamper.sqlite3"
    model = _text_model("cache-tamper-text-v1", "cache-tamper-space-v1")
    _initialize(database, model)
    text_a = "Contenido A para verificar hint y key de cache Semantic."
    text_b = "Contenido B distinto para verificar hint y key de cache Semantic."
    _base, chunk_a, payload_a = _published_text_generation(
        database,
        model,
        item_id="cache-tamper-a",
        text=text_a,
        processing_signature="cache-tamper-a",
        updated_ns=10,
    )
    _other, chunk_b, payload_b = _published_text_generation(
        database,
        model,
        item_id="cache-tamper-b",
        text=text_b,
        processing_signature="cache-tamper-b",
        updated_ns=40,
    )
    replacement = _stage_same_text(
        database,
        "cache-tamper-replacement",
        text_a,
        refresh="cache-tamper-replacement-refresh",
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cache-tamper-successor",
        provenance={"fixture": "cache-tamper-successor"},
        started_ns=80,
    )
    assert enqueue_text_chunk_jobs(database, successor, (replacement.chunk_id,), now_ns=81) == 1
    with semantic_database(database, readonly=True) as connection:
        job_id = int(
            connection.execute(
                "SELECT job_id FROM embedding_jobs WHERE generation_id=? AND entity_id=?",
                (successor, replacement.chunk_id),
            ).fetchone()[0]
        )
        assert payload_a != payload_b
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET cached_payload_id=? WHERE job_id=?",
            (payload_b, job_id),
        )
    assert reuse_cached_jobs(database, successor, now_ns=82) == 0
    with semantic_database(database, readonly=True) as connection:
        pending = connection.execute(
            "SELECT status FROM embedding_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
    assert pending == "pending"

    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE embedding_jobs SET content_xxh3_128=?,content_bytes=?,"
            "content_xxh3_64_guard=? WHERE job_id=?",
            (chunk_b.fingerprint.xxh3_128, chunk_b.fingerprint.byte_count,
             chunk_b.fingerprint.xxh3_64_guard, job_id),
        )
    assert reuse_cached_jobs(database, successor, now_ns=83) == 0
    with semantic_database(database, readonly=True) as connection:
        stale = connection.execute(
            "SELECT status,cached_payload_id FROM embedding_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    # The hint may still point to payload B, but the source predicate has
    # rejected the job before any member/payload attachment.
    assert stale is not None and tuple(stale) == ("stale", payload_b)

    assert enqueue_text_chunk_jobs(database, successor, (replacement.chunk_id,), now_ns=84) == 1
    with semantic_database(database, readonly=True) as connection:
        restored = connection.execute(
            "SELECT status,cached_payload_id FROM embedding_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
    assert restored is not None and tuple(restored) == ("pending", payload_a)
    assert reuse_cached_jobs(database, successor, now_ns=85) == 1
    with semantic_database(database, readonly=True) as connection:
        member = connection.execute(
            "SELECT payload_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()
    assert member is not None and int(member[0]) == payload_a
    assert chunk_a.chunk_id != replacement.chunk_id


def test_v8_stale_lease_boundary_drains_beyond_max_write_batch_without_head(
    tmp_path: Path,
) -> None:
    database, generation_id, _model, chunks = _text_fixture(tmp_path, count=1)
    first = _claim(
        database,
        generation_id,
        worker_id="boundary-seed-worker",
        now_ns=400,
    )[0]
    with semantic_database(database, readonly=True) as connection:
        seed = connection.execute(
            "SELECT model_signature,role,entity_kind,item_id,input_item_revision_id,"
            "input_chunk_revision_id,content_xxh3_128,content_bytes,content_xxh3_64_guard "
            "FROM embedding_jobs WHERE job_id=?",
            (first.job_id,),
        ).fetchone()
    assert seed is not None
    with semantic_database(database) as connection:
        for offset in range(MAX_WRITE_BATCH):
            connection.execute(
                """INSERT INTO embedding_jobs(
                    generation_id,model_signature,role,entity_kind,entity_id,item_id,
                    input_item_revision_id,input_chunk_revision_id,content_xxh3_128,
                    content_bytes,content_xxh3_64_guard,status,attempts,max_attempts,
                    available_ns,lease_owner,lease_until_ns,created_ns,updated_ns,
                    attempt_started_ns,attempt_sequence)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    str(seed["model_signature"]),
                    str(seed["role"]),
                    str(seed["entity_kind"]),
                    f"missing-boundary-{offset}",
                    str(seed["item_id"]),
                    seed["input_item_revision_id"],
                    seed["input_chunk_revision_id"],
                    str(seed["content_xxh3_128"]),
                    int(seed["content_bytes"]),
                    str(seed["content_xxh3_64_guard"]),
                    "leased",
                    1,
                    3,
                    0,
                    f"boundary-worker-{offset}",
                    1_000_000_000_000_000,
                    1,
                    1,
                    1,
                    1,
                ),
            )
        connection.execute(
            "UPDATE text_chunks SET active=0 WHERE chunk_id=?",
            (chunks[0].chunk_id,),
        )
    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="boundary-reconcile-1",
            now_ns=401,
        )
        == ()
    )
    assert _job_counts(database, generation_id) == (0, 1, 0, 0, MAX_WRITE_BATCH)
    with semantic_database(database, readonly=True) as connection:
        first_receipts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' AND status='failed'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert first_receipts == MAX_WRITE_BATCH
    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="boundary-reconcile-2",
            now_ns=402,
        )
        == ()
    )
    assert _job_counts(database, generation_id) == (0, 0, 0, 0, MAX_WRITE_BATCH + 1)
    with semantic_database(database, readonly=True) as connection:
        all_receipts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' AND status='failed'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert all_receipts == MAX_WRITE_BATCH + 1
    with pytest.raises(SemanticStateError, match="stale jobs"):
        finalize_embedding_generation(database, generation_id, completed_ns=403)
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
            == 0
        )
