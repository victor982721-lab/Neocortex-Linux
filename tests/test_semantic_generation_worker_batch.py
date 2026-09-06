"""Focused regression coverage for batched semantic completions."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from pathlib import Path
import sqlite3

import pytest

from neocortex.semantic import semantic_generation_worker
from neocortex.semantic.semantic_chunking import TextChunkingConfig, iter_text_chunks
from neocortex.semantic.semantic_generation_worker import complete_embedding_jobs_batch
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRole,
    SemanticItem,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic.semantic_state import (
    enqueue_text_chunk_jobs,
    finalize_text_chunk_refresh,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)


CHUNKING = TextChunkingConfig(
    max_chars=256,
    max_terms=64,
    overlap_chars=0,
    overlap_terms=0,
    min_natural_break_chars=32,
)


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "batch-completion-model-v1",
        "batch-completion-space-v1",
        EmbeddingModality.TEXT,
        "fixture/batch-completion",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _fixture_generation(tmp_path: Path, count: int = 3) -> tuple[Path, int, EmbeddingModelSpec]:
    database = tmp_path / "semantic.sqlite3"
    model = _model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    chunk_ids: list[str] = []
    for index in range(count):
        identity = f"batch-completion-{index}"
        item = SemanticItem(
            item_id=f"item:pdf:{identity}",
            source_kind="pdf",
            source_identity=identity,
            identity_version="batch-completion-source-v1",
            fingerprint=fingerprint_text(f"source:{identity}"),
            path=f"/fixtures/{identity}.pdf",
            provenance={"fixture": True},
        )
        upsert_semantic_item(database, item, refresh_token=f"item-refresh:{index}")
        chunk = next(
            iter_text_chunks(
                item.item_id,
                (TextSection("pdf_page", "1", f"Texto de prueba {identity}."),),
                CHUNKING,
            )
        )
        refresh_token = f"chunk-refresh:{index}"
        stage_text_chunks(database, (chunk,), refresh_token=refresh_token)
        finalize_text_chunk_refresh(
            database,
            item_id=item.item_id,
            chunking_signature=CHUNKING.signature,
            refresh_token=refresh_token,
        )
        chunk_ids.append(chunk.chunk_id)
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="batch-completion-generation-v1",
    )
    assert enqueue_text_chunk_jobs(database, generation_id, chunk_ids) == count
    return database, generation_id, model


def _claimed(
    database: Path,
    generation_id: int,
    *,
    limit: int,
    worker_id: str = "batch-completion-worker",
) -> tuple[EmbeddingJobLease, ...]:
    leases = semantic_generation_worker.claim_embedding_jobs(
        database,
        generation_id,
        worker_id=worker_id,
        limit=limit,
        lease_seconds=60.0,
    )
    assert len(leases) == limit
    return leases


def _outputs(leases: Sequence[EmbeddingJobLease]) -> tuple[tuple[int, BackendEmbedding], ...]:
    return tuple(
        (
            index,
            BackendEmbedding(
                str(lease.job_id),
                (1.0, 0.0, 0.0, 0.0),
                {"fixture": "batch-completion"},
            ),
        )
        for index, lease in enumerate(leases)
    )


def test_batch_completion_uses_one_write_connection_and_preserves_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, generation_id, _model_spec = _fixture_generation(tmp_path, count=3)
    leases = _claimed(database, generation_id, limit=3)
    statements: list[str] = []
    opens = 0
    real_database = semantic_generation_worker.semantic_database

    @contextmanager
    def traced_database(
        *args: object,
        **kwargs: object,
    ) -> Iterator[sqlite3.Connection]:
        nonlocal opens
        opens += 1
        with real_database(*args, **kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(semantic_generation_worker, "semantic_database", traced_database)
    result = complete_embedding_jobs_batch(
        database,
        leases,
        _outputs(leases),
        worker_id="batch-completion-worker",
    )

    assert result == (3, 0)
    assert opens == 1
    assert sum(statement.upper() == "BEGIN IMMEDIATE" for statement in statements) == 1
    with semantic_database(database, readonly=True) as connection:
        assert tuple(
            tuple(row)
            for row in connection.execute("SELECT status FROM embedding_jobs ORDER BY job_id")
        ) == (("done",), ("done",), ("done",))
        assert connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' AND status='succeeded'",
                (generation_id,),
            ).fetchone()[0]
            == 3
        )


def test_batch_completion_isolates_invalid_vector_as_one_job_error(tmp_path: Path) -> None:
    database, generation_id, _model_spec = _fixture_generation(tmp_path, count=2)
    leases = _claimed(database, generation_id, limit=2)
    outputs = (
        _outputs(leases)[0],
        (
            1,
            BackendEmbedding(
                str(leases[1].job_id),
                (1.0, 0.0),
                {"fixture": "invalid-vector"},
            ),
        ),
    )

    assert complete_embedding_jobs_batch(
        database,
        leases,
        outputs,
        worker_id="batch-completion-worker",
    ) == (1, 1)
    with semantic_database(database, readonly=True) as connection:
        jobs = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT status,lease_owner,lease_until_ns,error_type "
                "FROM embedding_jobs ORDER BY job_id"
            )
        )
        assert jobs == (
            ("done", None, None, None),
            ("error", None, None, "ValueError"),
        )
        assert connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0] == 1
        failed_receipts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts WHERE job_id=? AND status='failed'",
                (leases[1].job_id,),
            ).fetchone()[0]
        )
    assert failed_receipts == 1


def test_batch_completion_stales_changed_source_without_leaking_lease(tmp_path: Path) -> None:
    database, generation_id, _model_spec = _fixture_generation(tmp_path, count=2)
    leases = _claimed(database, generation_id, limit=2)
    with semantic_database(database) as connection:
        connection.execute(
            "UPDATE text_chunks SET content_xxh3_128=? WHERE chunk_id=?",
            ("0" * 32, leases[0].entity_id),
        )

    assert complete_embedding_jobs_batch(
        database,
        leases,
        _outputs(leases),
        worker_id="batch-completion-worker",
    ) == (1, 1)
    with semantic_database(database, readonly=True) as connection:
        jobs = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT status,lease_owner,lease_until_ns,error_type "
                "FROM embedding_jobs ORDER BY job_id"
            )
        )
    assert jobs == (
        ("stale", None, None, "source_changed"),
        ("done", None, None, None),
    )
