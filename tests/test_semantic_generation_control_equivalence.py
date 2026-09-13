"""Characterization and regression coverage for Semantic generation control.

The fixtures are owner-local and use only the public semantic-state and worker
surfaces.  The cases deliberately interleave source changes, leases, retries,
cache reuse, publication CAS and resumable worker invocations so a batched
completion optimization cannot turn an observed stale result into a lost
receipt or roll back unrelated work.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from neocortex.semantic.semantic_chunking import TextChunkingConfig, iter_text_chunks
from neocortex.semantic.semantic_generation_worker import (
    complete_embedding_jobs_batch,
    run_generation,
)
from neocortex.semantic.semantic_lineage_repository import explain_text_chunk_lineage
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    SemanticItem,
    TextChunk,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic.semantic_state import (
    SemanticStateError,
    claim_embedding_jobs,
    complete_embedding_job,
    enqueue_text_chunk_jobs,
    fail_embedding_job,
    finalize_embedding_generation,
    finalize_text_chunk_refresh,
    generation_summary,
    initialize_semantic_state,
    publish_text_channel_revision,
    register_embedding_model,
    release_embedding_job_lease_for_deadline,
    reuse_cached_jobs,
    semantic_database,
    stage_text_chunks,
    start_embedding_generation,
    upsert_semantic_item,
)
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


CHUNKING = TextChunkingConfig(
    max_chars=256,
    max_terms=64,
    overlap_chars=0,
    overlap_terms=0,
    min_natural_break_chars=32,
)


def _model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "generation-control-model-v1",
        "generation-control-space-v1",
        EmbeddingModality.TEXT,
        "fixture/generation-control",
        "1",
        4,
        "test-deterministic",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _stage(
    database: Path,
    item_id: str,
    text: str,
    *,
    source_revision_id: str,
    updated_ns: int,
) -> TextChunk:
    item = SemanticItem(
        item_id,
        "pdf",
        f"identity:{item_id}",
        "generation-control-source-v1",
        fingerprint_text(text),
        path=f"/fixtures/{item_id}.pdf",
        provenance={"fixture": "generation-control", "item": item_id},
        source_revision={"revision_id": source_revision_id},
    )
    upsert_semantic_item(
        database,
        item,
        refresh_token=f"item-refresh:{item_id}:{source_revision_id}",
        updated_ns=updated_ns,
    )
    chunk = next(
        iter_text_chunks(
            item_id,
            (
                TextSection(
                    "pdf_page",
                    "1",
                    text,
                    {"source_revision_id": source_revision_id},
                ),
            ),
            CHUNKING,
        )
    )
    refresh_token = f"chunk-refresh:{item_id}:{source_revision_id}"
    stage_text_chunks(
        database,
        (chunk,),
        refresh_token=refresh_token,
        updated_ns=updated_ns + 1,
    )
    # Keep chunk publication explicit: jobs are claimable only after their
    # owner derivation has a publication receipt.
    finalize_text_chunk_refresh(
        database,
        item_id=item_id,
        chunking_signature=CHUNKING.signature,
        refresh_token=refresh_token,
        updated_ns=updated_ns + 2,
    )
    return chunk


def _fixture(
    tmp_path: Path,
    *,
    count: int = 2,
    max_attempts: int = 3,
) -> tuple[Path, int, EmbeddingModelSpec, tuple[TextChunk, ...]]:
    database = tmp_path / "semantic.sqlite3"
    model = _model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    chunks = tuple(
        _stage(
            database,
            f"generation-control-item-{index}",
            f"Contenido de control de generación Semantic para fixture {index}.",
            source_revision_id=f"revision:fixture:{index}:1",
            updated_ns=10 + index * 10,
        )
        for index in range(count)
    )
    generation_id = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="generation-control-v1",
        provenance={"fixture": "generation-control-v1", "source": "pdf"},
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


def _claim(
    database: Path,
    generation_id: int,
    *,
    worker_id: str,
    limit: int,
    now_ns: int,
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
        {"fixture": "generation-control"},
    )


def _assert_summary_matches_jobs(database: Path, generation_id: int) -> None:
    """Compare the public summary with an independent fixture aggregate."""

    with semantic_database(database, readonly=True) as connection:
        counts = tuple(
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_jobs "
                    "WHERE generation_id=? AND status=?",
                    (generation_id, status),
                ).fetchone()[0]
            )
            for status in ("pending", "leased", "done", "error", "stale")
        )
    summary = generation_summary(database, generation_id)
    assert (summary.pending, summary.leased, summary.done, summary.errors, summary.stale) == counts


def _mutate_source(database: Path, chunk: TextChunk, mutation: str) -> None:
    if mutation == "chunk":
        # Controlled fixture drift: the immutable compressed payload is left
        # intact while its observed identity changes, forcing fail-closed stale
        # handling without inventing a new source revision.
        with semantic_database(database) as connection:
            connection.execute(
                "UPDATE text_chunks SET content_xxh3_128=? WHERE chunk_id=?",
                ("0" * 32, chunk.chunk_id),
            )
        return
    if mutation == "item":
        replacement_text = "Contenido reemplazado para la fuente Semantic."
        upsert_semantic_item(
            database,
            SemanticItem(
                chunk.item_id,
                "pdf",
                f"identity:{chunk.item_id}",
                "generation-control-source-v1",
                fingerprint_text(replacement_text),
                path=f"/fixtures/{chunk.item_id}.pdf",
                provenance={"fixture": "generation-control", "replacement": True},
                source_revision={"revision_id": "revision:fixture:replacement"},
            ),
            refresh_token=f"item-refresh:{chunk.item_id}:replacement",
            updated_ns=500,
        )
        return
    if mutation == "revision":
        assert (
            publish_text_channel_revision(
                database,
                item_id=chunk.item_id,
                channel="pdf_page",
                revision_token="channel-revision-2",
                updated_ns=500,
            )
            == 1
        )
        return
    raise AssertionError(f"unknown fixture mutation {mutation!r}")


@pytest.mark.parametrize("mutation", ("chunk", "item", "revision"))
def test_pending_source_drift_becomes_stale_before_any_new_lease(
    tmp_path: Path,
    mutation: str,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(tmp_path, count=1)
    _mutate_source(database, chunks[0], mutation)

    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="pending-stale-worker",
            now_ns=600,
        )
        == ()
    )
    _assert_summary_matches_jobs(database, generation_id)
    summary = generation_summary(database, generation_id)
    assert (summary.pending, summary.leased, summary.stale) == (0, 0, 1)
    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,attempts,lease_owner,lease_until_ns,error_type "
            "FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        embedding_receipts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert row is not None
    assert tuple(row) == ("stale", 0, None, None, "source_changed")
    # A pending item was never attempted; stale observation must not fabricate
    # or suppress an embedding-attempt receipt.
    assert embedding_receipts == 0


@pytest.mark.parametrize("mutation", ("chunk", "item", "revision"))
def test_leased_source_drift_returns_stale_and_records_the_attempt_receipt(
    tmp_path: Path,
    mutation: str,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(tmp_path, count=1)
    lease = _claim(
        database,
        generation_id,
        worker_id="leased-stale-worker",
        limit=1,
        now_ns=600,
    )[0]
    _mutate_source(database, chunks[0], mutation)

    assert complete_embedding_jobs_batch(
        database,
        (lease,),
        ((0, _output(lease)),),
        worker_id="leased-stale-worker",
        now_ns=700,
    ) == (0, 1)
    with semantic_database(database, readonly=True) as connection:
        job = connection.execute(
            "SELECT status,attempts,lease_owner,lease_until_ns,error_type "
            "FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()
        receipt = connection.execute(
            "SELECT status,execution_mode,attempt,receipt_json "
            "FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' "
            "ORDER BY receipt_id DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        payload_count = int(
            connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]
        )
    assert job is not None and tuple(job) == ("stale", 1, None, None, "source_changed")
    assert receipt is not None
    assert (str(receipt["status"]), str(receipt["execution_mode"]), int(receipt["attempt"])) == (
        "failed",
        "attempted",
        1,
    )
    assert json.loads(str(receipt["receipt_json"]))["outcome"] == "failed"
    assert payload_count == 0
    _assert_summary_matches_jobs(database, generation_id)


def test_batched_completion_isolates_stale_and_invalid_jobs_without_losing_siblings(
    tmp_path: Path,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(tmp_path, count=4)
    leases = _claim(
        database,
        generation_id,
        worker_id="batch-control-worker",
        limit=4,
        now_ns=600,
    )
    _mutate_source(database, chunks[0], "chunk")
    outputs = (
        (0, _output(leases[0])),
        (1, _output(leases[1], (1.0, 0.0))),
        (2, _output(leases[2])),
        (3, _output(leases[3])),
    )

    assert complete_embedding_jobs_batch(
        database,
        leases,
        outputs,
        worker_id="batch-control-worker",
        now_ns=700,
    ) == (2, 2)
    with semantic_database(database, readonly=True) as connection:
        jobs = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT status,lease_owner,error_type FROM embedding_jobs "
                "WHERE generation_id=? ORDER BY job_id",
                (generation_id,),
            )
        )
        embedding_receipts = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT job_id,status,execution_mode FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' ORDER BY receipt_id",
                (generation_id,),
            )
        )
        payload_count = int(
            connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]
        )
        member_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM embedding_generation_members WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
    assert jobs == (
        ("stale", None, "source_changed"),
        ("error", None, "ValueError"),
        ("done", None, None),
        ("done", None, None),
    )
    assert tuple(str(row[1]) for row in embedding_receipts) == (
        "failed",
        "failed",
        "succeeded",
        "succeeded",
    )
    assert tuple(str(row[2]) for row in embedding_receipts[:2]) == ("attempted", "attempted")
    assert len({int(row[0]) for row in embedding_receipts}) == 4
    assert (payload_count, member_count) == (2, 2)
    summary = generation_summary(database, generation_id)
    assert (summary.done, summary.errors, summary.stale, summary.unfinished) == (2, 1, 1, 0)
    _assert_summary_matches_jobs(database, generation_id)


def test_source_change_between_claimed_batches_preserves_the_prior_batch(
    tmp_path: Path,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(tmp_path, count=4)
    first = _claim(
        database,
        generation_id,
        worker_id="first-batch-worker",
        limit=2,
        now_ns=600,
    )
    assert complete_embedding_jobs_batch(
        database,
        first,
        tuple((index, _output(lease)) for index, lease in enumerate(first)),
        worker_id="first-batch-worker",
        now_ns=700,
    ) == (2, 0)

    second = _claim(
        database,
        generation_id,
        worker_id="second-batch-worker",
        limit=2,
        now_ns=800,
    )
    _mutate_source(database, chunks[2], "chunk")
    assert complete_embedding_jobs_batch(
        database,
        second,
        tuple((index, _output(lease)) for index, lease in enumerate(second)),
        worker_id="second-batch-worker",
        now_ns=900,
    ) == (1, 1)
    with semantic_database(database, readonly=True) as connection:
        jobs = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT status,lease_owner,error_type FROM embedding_jobs "
                "WHERE generation_id=? ORDER BY job_id",
                (generation_id,),
            )
        )
        payload_count = int(
            connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]
        )
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert jobs == (
        ("done", None, None),
        ("done", None, None),
        ("stale", None, "source_changed"),
        ("done", None, None),
    )
    assert (payload_count, receipt_count) == (3, 4)
    _assert_summary_matches_jobs(database, generation_id)


def test_same_content_metadata_move_remains_reusable_and_is_not_marked_stale(
    tmp_path: Path,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(tmp_path, count=1)
    lease = _claim(
        database,
        generation_id,
        worker_id="metadata-worker",
        limit=1,
        now_ns=600,
    )[0]
    text = chunks[0].text
    upsert_semantic_item(
        database,
        SemanticItem(
            chunks[0].item_id,
            "pdf",
            f"identity:{chunks[0].item_id}",
            "generation-control-source-v1",
            fingerprint_text(text),
            path=f"/fixtures/moved/{chunks[0].item_id}.pdf",
            provenance={"fixture": "generation-control", "moved": True},
            source_revision={"revision_id": "revision:fixture:moved"},
        ),
        refresh_token="item-refresh:moved",
        updated_ns=650,
    )

    assert complete_embedding_jobs_batch(
        database,
        (lease,),
        ((0, _output(lease)),),
        worker_id="metadata-worker",
        now_ns=700,
    ) == (1, 0)
    assert generation_summary(database, generation_id).stale == 0
    assert (
        finalize_embedding_generation(database, generation_id, completed_ns=701).status == "ready"
    )
    _assert_summary_matches_jobs(database, generation_id)


def test_deadline_lease_release_refunds_attempt_and_replays_same_job(
    tmp_path: Path,
) -> None:
    database, generation_id, _model_spec, _chunks = _fixture(tmp_path, count=1)
    lease = _claim(
        database,
        generation_id,
        worker_id="deadline-worker",
        limit=1,
        now_ns=600,
    )[0]
    release_embedding_job_lease_for_deadline(
        database,
        lease.job_id,
        worker_id="deadline-worker",
        now_ns=700,
    )
    with semantic_database(database, readonly=True) as connection:
        released = connection.execute(
            "SELECT status,attempts,lease_owner,lease_until_ns,error_type "
            "FROM embedding_jobs WHERE job_id=?",
            (lease.job_id,),
        ).fetchone()
        cancelled = connection.execute(
            "SELECT status,execution_mode,attempt FROM semantic_work_receipts "
            "WHERE job_id=? ORDER BY receipt_id DESC LIMIT 1",
            (lease.job_id,),
        ).fetchone()
    assert released is not None and tuple(released) == ("pending", 0, None, None, None)
    assert cancelled is not None
    assert tuple(cancelled) == ("cancelled", "attempted", 1)
    _assert_summary_matches_jobs(database, generation_id)

    resumed = _claim(
        database,
        generation_id,
        worker_id="deadline-resume-worker",
        limit=1,
        now_ns=701,
    )[0]
    assert resumed.attempt == 1
    complete_embedding_job(
        database,
        resumed.job_id,
        worker_id="deadline-resume-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=702,
    )
    assert (
        finalize_embedding_generation(database, generation_id, completed_ns=703).status == "ready"
    )
    with semantic_database(database, readonly=True) as connection:
        statuses = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT status FROM semantic_work_receipts WHERE job_id=? ORDER BY receipt_id",
                (lease.job_id,),
            )
        )
    assert statuses == ("cancelled", "succeeded")


def test_expired_lease_is_reclaimed_and_old_worker_completion_cannot_overwrite_it(
    tmp_path: Path,
) -> None:
    database, generation_id, model, _chunks = _fixture(tmp_path, count=1)
    old = _claim(
        database,
        generation_id,
        worker_id="old-worker",
        limit=1,
        lease_seconds=1.0,
        now_ns=600,
    )[0]
    reclaim_now = old.lease_until_ns + 1
    new = _claim(
        database,
        generation_id,
        worker_id="reclaimer-worker",
        limit=1,
        lease_seconds=60.0,
        now_ns=reclaim_now,
    )[0]
    assert new.job_id == old.job_id
    assert new.attempt == old.attempt + 1
    _assert_summary_matches_jobs(database, generation_id)

    with pytest.raises(SemanticStateError, match="absent, expired or owned elsewhere"):
        complete_embedding_job(
            database,
            old.job_id,
            worker_id="old-worker",
            vector=(1.0, 0.0, 0.0, 0.0),
            now_ns=reclaim_now + 1,
        )
    with semantic_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT status,attempts,lease_owner FROM embedding_jobs WHERE job_id=?",
            (old.job_id,),
        ).fetchone()
        payload_count = int(
            connection.execute("SELECT COUNT(*) FROM vector_payloads").fetchone()[0]
        )
        abandoned_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE job_id=? AND status='abandoned'",
                (old.job_id,),
            ).fetchone()[0]
        )
    assert row is not None and tuple(row) == ("leased", 2, "reclaimer-worker")
    assert (payload_count, abandoned_count) == (0, 1)
    _assert_summary_matches_jobs(database, generation_id)

    complete_embedding_job(
        database,
        new.job_id,
        worker_id="reclaimer-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=reclaim_now + 2,
    )
    summary = finalize_embedding_generation(database, generation_id, completed_ns=reclaim_now + 3)
    assert summary.status == "ready"
    with semantic_database(database, readonly=True) as connection:
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
    assert head is not None and int(head[0]) == generation_id


def test_retry_reaches_terminal_error_then_explicit_requeue_keeps_all_attempt_receipts(
    tmp_path: Path,
) -> None:
    database, generation_id, _model_spec, chunks = _fixture(
        tmp_path,
        count=1,
        max_attempts=2,
    )
    first = _claim(
        database,
        generation_id,
        worker_id="retry-worker-1",
        limit=1,
        now_ns=600,
    )[0]
    assert (
        fail_embedding_job(
            database,
            first.job_id,
            worker_id="retry-worker-1",
            error_type="temporary_fixture_failure",
            error_message="retry once",
            retryable=True,
            now_ns=601,
        )
        == "pending"
    )
    _assert_summary_matches_jobs(database, generation_id)
    second = _claim(
        database,
        generation_id,
        worker_id="retry-worker-2",
        limit=1,
        now_ns=602,
    )[0]
    assert (
        fail_embedding_job(
            database,
            second.job_id,
            worker_id="retry-worker-2",
            error_type="terminal_fixture_failure",
            error_message="max attempts",
            retryable=True,
            now_ns=603,
        )
        == "error"
    )
    _assert_summary_matches_jobs(database, generation_id)
    assert (
        claim_embedding_jobs(
            database,
            generation_id,
            worker_id="retry-worker-3",
            now_ns=604,
        )
        == ()
    )

    # Requeue is explicit owner work.  It resets the terminal job's attempt
    # cursor while preserving both immutable failure receipts.
    assert enqueue_text_chunk_jobs(
        database,
        generation_id,
        (chunks[0].chunk_id,),
        max_attempts=2,
        now_ns=605,
    ) == 1
    with semantic_database(database, readonly=True) as connection:
        reset = connection.execute(
            "SELECT status,attempts,lease_owner,error_type FROM embedding_jobs WHERE job_id=?",
            (first.job_id,),
        ).fetchone()
    assert reset is not None and tuple(reset) == ("pending", 0, None, None)
    _assert_summary_matches_jobs(database, generation_id)
    third = _claim(
        database,
        generation_id,
        worker_id="retry-worker-4",
        limit=1,
        now_ns=606,
    )[0]
    complete_embedding_job(
        database,
        third.job_id,
        worker_id="retry-worker-4",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=607,
    )
    _assert_summary_matches_jobs(database, generation_id)
    assert (
        finalize_embedding_generation(database, generation_id, completed_ns=608).status == "ready"
    )
    with semantic_database(database, readonly=True) as connection:
        receipts = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT receipt_key,status,attempt FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' ORDER BY receipt_id",
                (generation_id,),
            )
        )
    assert tuple(str(row[1]) for row in receipts) == ("failed", "failed", "succeeded")
    assert tuple(int(row[2]) for row in receipts) == (1, 2, 1)
    assert len({str(row[0]) for row in receipts}) == 3


def test_cache_hit_reuses_payload_and_exposes_exact_causation_in_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _model()
    initialize_semantic_state(database)
    register_embedding_model(database, model, allow_test_provider=True)
    original = _stage(
        database,
        "cache-source-original",
        "Contenido estable para probar reutilización de payload Semantic.",
        source_revision_id="revision:cache:original",
        updated_ns=10,
    )
    baseline = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cache-baseline-v1",
        provenance={"fixture": "cache-baseline"},
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, baseline, (original.chunk_id,), now_ns=101) == 1
    baseline_lease = _claim(
        database,
        baseline,
        worker_id="cache-baseline-worker",
        limit=1,
        now_ns=102,
    )[0]
    assert complete_embedding_jobs_batch(
        database,
        (baseline_lease,),
        ((0, _output(baseline_lease)),),
        worker_id="cache-baseline-worker",
        now_ns=103,
    ) == (1, 0)
    assert finalize_embedding_generation(database, baseline, completed_ns=104).status == "ready"
    with semantic_database(database, readonly=True) as connection:
        baseline_member = connection.execute(
            "SELECT payload_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_id=?",
            (baseline, original.chunk_id),
        ).fetchone()
        producer = connection.execute(
            "SELECT receipt_key FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding' AND status='succeeded'",
            (baseline,),
        ).fetchone()
    assert baseline_member is not None and producer is not None
    payload_id = int(baseline_member[0])
    producer_key = str(producer[0])

    replacement = _stage(
        database,
        "cache-source-replacement",
        "Contenido estable para probar reutilización de payload Semantic.",
        source_revision_id="revision:cache:replacement",
        updated_ns=200,
    )
    successor = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cache-successor-v1",
        provenance={"fixture": "cache-successor"},
        started_ns=300,
    )
    assert enqueue_text_chunk_jobs(database, successor, (replacement.chunk_id,), now_ns=301) == 1
    assert reuse_cached_jobs(database, successor, now_ns=302) == 1
    assert finalize_embedding_generation(database, successor, completed_ns=303).status == "ready"

    with semantic_database(database, readonly=True) as connection:
        member = connection.execute(
            "SELECT payload_id,base_member_id FROM embedding_generation_members "
            "WHERE generation_id=? AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()
        receipt_row = connection.execute(
            "SELECT receipt_id,receipt_key,execution_mode,payload_id,receipt_json "
            "FROM semantic_work_receipts WHERE generation_id=? "
            "AND stage_id='semantic.embedding' AND entity_id=?",
            (successor, replacement.chunk_id),
        ).fetchone()
    assert member is not None and tuple(member) == (payload_id, None)
    assert receipt_row is not None
    assert (str(receipt_row["execution_mode"]), int(receipt_row["payload_id"])) == (
        "cache_hit",
        payload_id,
    )
    cached_receipt = json.loads(str(receipt_row["receipt_json"]))
    assert cached_receipt["causation_id"] == producer_key
    assert any(item["name"] == "reused_vector_payload" for item in cached_receipt["inputs"])

    lineage = explain_text_chunk_lineage(
        database,
        chunk_id=replacement.chunk_id,
        model_signature=model.model_signature,
    )
    assert lineage.embedding_count == 1
    assert lineage.embeddings[0].execution_mode == "cache_hit"
    assert lineage.embeddings[0].payload_id == payload_id
    assert lineage.embeddings[0].receipt_id == int(receipt_row["receipt_id"])


def test_same_building_head_and_generation_are_idempotent_and_refs_remain_bound(
    tmp_path: Path,
) -> None:
    database, generation_id, model, chunks = _fixture(tmp_path, count=2)
    repeated = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="generation-control-v1",
        provenance={"fixture": "generation-control-v1", "source": "pdf"},
        materialize_base=False,
        started_ns=200,
    )
    assert repeated == generation_id
    assert enqueue_text_chunk_jobs(
        database,
        generation_id,
        tuple(chunk.chunk_id for chunk in chunks),
        now_ns=201,
    ) == len(chunks)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == len(chunks)
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
            == 0
        )

    leases = _claim(
        database,
        generation_id,
        worker_id="idempotent-worker",
        limit=2,
        now_ns=300,
    )
    assert complete_embedding_jobs_batch(
        database,
        leases,
        tuple((index, _output(lease)) for index, lease in enumerate(leases)),
        worker_id="idempotent-worker",
        now_ns=301,
    ) == (2, 0)
    assert (
        finalize_embedding_generation(database, generation_id, completed_ns=302).status == "ready"
    )
    with semantic_database(database, readonly=True) as connection:
        receipts = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT receipt_key,job_id,item_revision_id,chunk_revision_id "
                "FROM semantic_work_receipts WHERE generation_id=? "
                "AND stage_id='semantic.embedding' ORDER BY receipt_id",
                (generation_id,),
            )
        )
        members = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT item_revision_id,chunk_revision_id,payload_id "
                "FROM embedding_generation_members WHERE generation_id=? ORDER BY member_id",
                (generation_id,),
            )
        )
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(receipts) == len(chunks)
    assert len({str(row[0]) for row in receipts}) == len(chunks)
    assert all(
        row[1] is not None and row[2] is not None and row[3] is not None
        for row in receipts
    )
    assert len(members) == len(chunks)
    assert all(
        row[0] is not None and row[1] is not None and row[2] is not None
        for row in members
    )
    assert head is not None and int(head[0]) == generation_id
    assert foreign_keys == []


def test_competing_candidates_share_initial_head_but_only_cas_winner_publishes(
    tmp_path: Path,
) -> None:
    database, baseline, model, _chunks = _fixture(tmp_path, count=1)
    baseline_lease = _claim(
        database,
        baseline,
        worker_id="cas-baseline-worker",
        limit=1,
        now_ns=200,
    )[0]
    complete_embedding_job(
        database,
        baseline_lease.job_id,
        worker_id="cas-baseline-worker",
        vector=(1.0, 0.0, 0.0, 0.0),
        now_ns=201,
    )
    assert finalize_embedding_generation(database, baseline, completed_ns=202).status == "ready"
    winner = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cas-winner-v1",
        provenance={"fixture": "cas-winner"},
        started_ns=300,
    )
    loser = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cas-loser-v1",
        provenance={"fixture": "cas-loser"},
        started_ns=301,
    )
    with semantic_database(database, readonly=True) as connection:
        bases = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT base_generation_id FROM embedding_generations "
                "WHERE generation_id IN (?,?) ORDER BY generation_id",
                (winner, loser),
            )
        )
    assert bases == (baseline, baseline)
    assert finalize_embedding_generation(database, winner, completed_ns=302).status == "ready"
    with pytest.raises(SemanticStateError, match="must be rebased"):
        finalize_embedding_generation(database, loser, completed_ns=303)
    with semantic_database(database, readonly=True) as connection:
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
        loser_row = connection.execute(
            "SELECT status,cursor_json FROM embedding_generations WHERE generation_id=?",
            (loser,),
        ).fetchone()
    assert head is not None and int(head[0]) == winner
    assert loser_row is not None and str(loser_row["status"]) == "failed"
    loser_cursor = json.loads(str(loser_row["cursor_json"]))
    assert loser_cursor["failure_reason"] == "published_head_changed"
    assert loser_cursor["expected_head"] == baseline
    assert loser_cursor["observed_head"] == winner

    rebased = start_embedding_generation(
        database,
        model_signature=model.model_signature,
        processing_signature="cas-loser-v1",
        provenance={"fixture": "cas-loser"},
        started_ns=304,
    )
    assert rebased != loser
    with semantic_database(database, readonly=True) as connection:
        rebased_base = connection.execute(
            "SELECT base_generation_id FROM embedding_generations WHERE generation_id=?",
            (rebased,),
        ).fetchone()
    assert rebased_base is not None and int(rebased_base[0]) == winner
    assert finalize_embedding_generation(database, rebased, completed_ns=305).status == "ready"


class _Backend:
    def __init__(
        self,
        model: EmbeddingModelSpec,
        *,
        max_batch_size: int,
        clock: list[float] | None = None,
        advance_after_first_call: float | None = None,
        interruption: BaseException | None = None,
    ) -> None:
        self._model = model
        self._max_batch_size = max_batch_size
        self._clock = clock
        self._advance_after_first_call = advance_after_first_call
        self._interruption = interruption
        self.calls = 0

    @property
    def model(self) -> EmbeddingModelSpec:
        return self._model

    @property
    def max_batch_size(self) -> int:
        return self._max_batch_size

    def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
        self.calls += 1
        if self._interruption is not None:
            raise self._interruption
        result = tuple(
            BackendEmbedding(
                request.request_id,
                (1.0, 0.0, 0.0, 0.0),
                {"fixture": "generation-control"},
            )
            for request in requests
        )
        if (
            self._clock is not None
            and self.calls == 1
            and self._advance_after_first_call is not None
        ):
            self._clock[0] = self._advance_after_first_call
        return result


def test_deadline_pause_and_resume_keep_the_same_generation_without_publishing_early(
    tmp_path: Path,
) -> None:
    database, generation_id, model, _chunks = _fixture(tmp_path, count=2)
    clock = [0.0]
    budget = SemanticWorkBudget(deadline=10.0, _clock=lambda: clock[0])
    first_backend = _Backend(
        model,
        max_batch_size=1,
        clock=clock,
        advance_after_first_call=20.0,
    )
    paused = run_generation(
        database,
        generation_id,
        first_backend,
        queued=2,
        work_budget=budget,
    )
    assert first_backend.calls == 1
    assert paused.summary.generation_id == generation_id
    assert (paused.summary.status, paused.summary.done, paused.summary.unfinished) == (
        "building",
        1,
        1,
    )
    assert budget.truncation_reason == "time_budget"
    with semantic_database(database, readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
            == 0
        )

    resumed = run_generation(
        database,
        generation_id,
        _Backend(model, max_batch_size=1),
        queued=0,
    )
    assert resumed.summary.generation_id == generation_id
    assert (resumed.summary.status, resumed.summary.done, resumed.summary.unfinished) == (
        "ready",
        2,
        0,
    )
    with semantic_database(database, readonly=True) as connection:
        head = connection.execute(
            "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
            (model.model_signature,),
        ).fetchone()
    assert head is not None and int(head[0]) == generation_id


def test_cancellation_releases_all_claimed_leases_and_resume_preserves_receipts(
    tmp_path: Path,
) -> None:
    database, generation_id, model, _chunks = _fixture(tmp_path, count=2)
    with pytest.raises(KeyboardInterrupt, match="fixture cancellation"):
        run_generation(
            database,
            generation_id,
            _Backend(
                model,
                max_batch_size=2,
                interruption=KeyboardInterrupt("fixture cancellation"),
            ),
            queued=2,
        )
    with semantic_database(database, readonly=True) as connection:
        jobs = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT status,attempts,lease_owner,lease_until_ns "
                "FROM embedding_jobs WHERE generation_id=? ORDER BY job_id",
                (generation_id,),
            )
        )
        head_count = int(
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
        )
        cancelled_attempts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding' "
                "AND status='failed'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert jobs == (("pending", 1, None, None), ("pending", 1, None, None))
    assert head_count == 0
    assert cancelled_attempts == 2

    resumed = run_generation(
        database,
        generation_id,
        _Backend(model, max_batch_size=2),
        queued=2,
    )
    assert (resumed.summary.status, resumed.summary.done, resumed.summary.unfinished) == (
        "ready",
        2,
        0,
    )
    with semantic_database(database, readonly=True) as connection:
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_work_receipts "
                "WHERE generation_id=? AND stage_id='semantic.embedding'",
                (generation_id,),
            ).fetchone()[0]
        )
    assert receipt_count == 4


def test_writer_interleaving_after_claim_is_revalidated_before_batch_completion(
    tmp_path: Path,
) -> None:
    database, generation_id, model, chunks = _fixture(tmp_path, count=1)

    class _SourceChangingBackend(_Backend):
        def embed(self, requests: Sequence[EmbeddingRequest]) -> Sequence[BackendEmbedding]:
            outputs = super().embed(requests)
            _mutate_source(database, chunks[0], "chunk")
            return outputs

    work = run_generation(
        database,
        generation_id,
        _SourceChangingBackend(model, max_batch_size=1),
        queued=1,
    )
    assert (work.failed, work.summary.status, work.summary.stale) == (1, "ready_partial", 1)
    with semantic_database(database, readonly=True) as connection:
        head_count = int(
            connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
        )
        stale_receipt = connection.execute(
            "SELECT status,execution_mode FROM semantic_work_receipts "
            "WHERE generation_id=? AND stage_id='semantic.embedding'",
            (generation_id,),
        ).fetchone()
    assert head_count == 0
    assert stale_receipt is not None and tuple(stale_receipt) == ("failed", "attempted")
    assert work.summary.generation_id == generation_id
