"""Bounded, resumable embedding generation worker with renewable leases."""

from __future__ import annotations
import itertools
import math
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol, TypeVar

from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from .semantic_backends import (
    EmbeddingBackend,
    SourceRevisionMismatchError,
    TextTokenLimitExceededError,
)
from .semantic_config import SEMANTIC_PIPELINE_VERSION
from .semantic_generation_repository import (
    _attach_payload,
    _job_is_current,
)
from .semantic_lineage_repository import (
    _record_discarded_embedding_execution,
    _record_embedding_attempt_failure,
)
from .semantic_models import (
    BackendEmbedding,
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingRequest,
    GenerationSummary,
    canonical_json,
    encode_vector,
)
from .semantic_repository_common import (
    MAX_ERROR_CHARS,
    _load_model,
    _now,
)
from .semantic_service_contracts import (
    JOB_BATCH_SIZE,
    LEASE_HEARTBEAT_INTERVAL_SECONDS,
    LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS,
    WORKER_LEASE_SECONDS,
    GenerationWorkResult,
)
from .semantic_work_budget import (
    SemanticIndexDeadlineExceeded,
    SemanticWorkBudget,
    unlimited_semantic_work_budget,
)
from .semantic_state import (
    StaleEmbeddingJobError,
    claim_embedding_jobs,
    deactivate_semantic_item_if_fingerprint,
    embedding_request_from_lease,
    fail_embedding_job,
    finalize_embedding_generation,
    generation_summary,
    heartbeat_embedding_jobs,
    release_embedding_job_lease_for_deadline,
    reuse_cached_jobs,
)
from .semantic_schema import SemanticStateError, semantic_database

_T = TypeVar("_T")
EmbeddingOutcome = tuple[
    tuple[tuple[int, BackendEmbedding], ...],
    tuple[tuple[int, Exception], ...],
]


class GenerationRunner(Protocol):
    def __call__(
        self,
        database: Path,
        generation_id: int,
        backend: EmbeddingBackend,
        *,
        queued: int,
        work_budget: SemanticWorkBudget,
        publish_if_complete: bool,
    ) -> GenerationWorkResult: ...


# region [01] Bounded batches and failure isolation


def batches(values: Iterable[_T], size: int) -> Iterator[tuple[_T, ...]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    iterator = iter(values)
    while batch := tuple(itertools.islice(iterator, size)):
        yield batch


def safe_error(exc: BaseException) -> str:
    return str(exc).encode("utf-8", "replace").decode("utf-8")[:8_000]


def embed_requests_isolated(
    backend: EmbeddingBackend,
    requests: Sequence[EmbeddingRequest],
) -> EmbeddingOutcome:
    """Isolate only failures proven local to one mutable or oversized payload."""

    successes: list[tuple[int, BackendEmbedding]] = []
    failures: list[tuple[int, Exception]] = []

    def submit(start: int, batch: Sequence[EmbeddingRequest]) -> None:
        try:
            outputs = tuple(backend.embed(batch))
            if len(outputs) != len(batch):
                raise RuntimeError("embedding backend returned an incomplete batch")
            if any(
                output.request_id != request.request_id
                for request, output in zip(batch, outputs, strict=True)
            ):
                raise RuntimeError("embedding backend changed request order")
        except SemanticIndexDeadlineExceeded:
            raise
        except Exception as exc:
            payload_local = isinstance(
                exc,
                (
                    SourceRevisionMismatchError,
                    TextTokenLimitExceededError,
                ),
            )
            if len(batch) == 1 or not payload_local:
                failures.extend((start + offset, exc) for offset in range(len(batch)))
                return
            midpoint = len(batch) // 2
            submit(start, batch[:midpoint])
            submit(start + midpoint, batch[midpoint:])
            return
        successes.extend((start + offset, output) for offset, output in enumerate(outputs))

    submit(0, requests)
    successes.sort(key=lambda value: value[0])
    failures.sort(key=lambda value: value[0])
    return tuple(successes), tuple(failures)


# endregion [01]


# region [02] Lease heartbeat


def embed_requests_with_heartbeat(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    *,
    worker_id: str,
    backend: EmbeddingBackend,
    requests: Sequence[EmbeddingRequest],
    lease_seconds: float = WORKER_LEASE_SECONDS,
    heartbeat_interval_seconds: float = LEASE_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_jobs: Callable[..., int] = heartbeat_embedding_jobs,
) -> EmbeddingOutcome:
    """Keep a bounded lease batch alive only while synchronous inference runs."""

    if not leases or len(leases) != len(requests):
        raise ValueError("heartbeat requires one lease per embedding request")
    if not math.isfinite(lease_seconds) or not 1.0 <= lease_seconds <= 86_400.0:
        raise ValueError("lease_seconds must be between 1 and 86400")
    if not math.isfinite(heartbeat_interval_seconds) or not (
        0.0 < heartbeat_interval_seconds < lease_seconds
    ):
        raise ValueError("heartbeat interval must be positive and below the lease")
    job_ids = tuple(int(lease.job_id) for lease in leases)
    stop = threading.Event()
    heartbeat_errors: list[Exception] = []

    def maintain_leases() -> None:
        while not stop.wait(heartbeat_interval_seconds):
            try:
                heartbeat_jobs(
                    database,
                    job_ids,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            except Exception as exc:  # surfaced synchronously after inference
                heartbeat_errors.append(exc)
                return

    heartbeat = threading.Thread(
        target=maintain_leases,
        name=f"neocortex-semantic-lease:{leases[0].generation_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        result = embed_requests_isolated(backend, requests)
    finally:
        stop.set()
        heartbeat.join(LEASE_HEARTBEAT_JOIN_TIMEOUT_SECONDS)
    if heartbeat.is_alive():
        raise RuntimeError("semantic lease heartbeat did not stop cleanly")
    if heartbeat_errors:
        raise RuntimeError("semantic lease heartbeat failed") from heartbeat_errors[0]
    return result


# endregion [02]


# region [03] Generation state transitions


def _record_embedding_failures(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    failures: Sequence[tuple[int, Exception]],
    *,
    worker_id: str,
) -> int:
    for index, exc in failures:
        retryable = isinstance(exc, OSError)
        lease = leases[index]
        fail_embedding_job(
            database,
            lease.job_id,
            worker_id=worker_id,
            error_type=type(exc).__name__,
            error_message=safe_error(exc),
            retryable=retryable,
            retry_delay_seconds=30.0 if retryable else 0.0,
        )
        if isinstance(exc, SourceRevisionMismatchError):
            deactivate_semantic_item_if_fingerprint(
                database,
                item_id=lease.item_id,
                fingerprint=lease.fingerprint,
            )
    return len(failures)


def _complete_embedding_job_on_connection(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: str,
    vector: Sequence[float],
    provenance: Mapping[str, object] | None,
    now_ns: int,
) -> int:
    """Complete one leased job without opening a second database connection.

    The single-job public repository function intentionally owns its connection
    lifecycle.  Generation workers already have a bounded completion batch,
    however, so reopening that connection for every vector is needlessly
    expensive.  Keep the exact CAS, payload, member and receipt sequence here,
    while letting the caller isolate each invocation with a savepoint.
    """

    provenance_json = canonical_json(provenance)
    row = connection.execute(
        "SELECT j.* FROM embedding_jobs j WHERE j.job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown embedding job {job_id}")
    if (
        str(row["status"]) != "leased"
        or str(row["lease_owner"]) != worker_id
        or row["lease_until_ns"] is None
        or int(row["lease_until_ns"]) <= now_ns
    ):
        raise SemanticStateError("job lease is absent, expired or owned elsewhere")
    if not _job_is_current(connection, row):
        raise StaleEmbeddingJobError("source changed before vector completion")

    model = _load_model(connection, str(row["model_signature"]))
    vector_blob, original_norm = encode_vector(
        vector,
        model.dimensions,
        model.vector_dtype,
    )
    inserted_payload = connection.execute(
        """INSERT INTO vector_payloads(
            model_signature,content_xxh3_128,content_bytes,
            content_xxh3_64_guard,dimensions,vector_dtype,vector_blob,
            original_norm,provenance_json,created_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(model_signature,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard) DO NOTHING""",
        (
            model.model_signature,
            str(row["content_xxh3_128"]),
            int(row["content_bytes"]),
            str(row["content_xxh3_64_guard"]),
            model.dimensions,
            model.vector_dtype.value,
            vector_blob,
            original_norm,
            provenance_json,
            now_ns,
        ),
    )
    payload = connection.execute(
        """SELECT payload_id FROM vector_payloads
        WHERE model_signature=? AND content_xxh3_128=? AND content_bytes=?
          AND content_xxh3_64_guard=?""",
        (
            model.model_signature,
            str(row["content_xxh3_128"]),
            int(row["content_bytes"]),
            str(row["content_xxh3_64_guard"]),
        ),
    ).fetchone()
    if payload is None:
        raise SemanticStateError("vector payload upsert did not produce a row")
    payload_id = int(payload["payload_id"])
    if inserted_payload.rowcount not in {0, 1}:
        raise SemanticStateError("vector payload insert returned an invalid outcome")
    if inserted_payload.rowcount == 0:
        _record_discarded_embedding_execution(
            connection,
            row=row,
            incumbent_payload_id=payload_id,
            candidate_vector_blob=vector_blob,
            dimensions=model.dimensions,
            vector_dtype=model.vector_dtype.value,
            original_norm=original_norm,
            now_ns=now_ns,
        )
    _attach_payload(
        connection,
        row,
        payload_id,
        provenance_json,
        now_ns,
        execution_mode=("executed" if inserted_payload.rowcount == 1 else "cache_hit"),
    )
    updated = connection.execute(
        """UPDATE embedding_jobs SET status='done',lease_owner=NULL,
        lease_until_ns=NULL,error_type=NULL,error_message=NULL,updated_ns=?
        WHERE job_id=? AND status='leased' AND lease_owner=?
          AND lease_until_ns>?""",
        (now_ns, job_id, worker_id, now_ns),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("job lease changed before completion was recorded")
    return payload_id


def _record_embedding_failure_on_connection(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: str,
    error_type: str,
    error_message: str,
    retryable: bool = False,
    retry_delay_seconds: float = 0.0,
    now_ns: int,
) -> str:
    """Persist one completion error on the caller's already-open connection."""

    if not worker_id.strip() or not error_type.strip():
        raise ValueError("worker_id and error_type cannot be blank")
    if (
        not math.isfinite(retry_delay_seconds)
        or retry_delay_seconds < 0
        or retry_delay_seconds > 86_400
    ):
        raise ValueError("retry_delay_seconds must be between 0 and 86400")
    row = connection.execute(
        """SELECT attempts,max_attempts,status,lease_owner,lease_until_ns,
            generation_id,model_signature,entity_kind,entity_id,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,job_id,
            attempt_started_ns,attempt_sequence,input_item_revision_id,
            input_chunk_revision_id
        FROM embedding_jobs WHERE job_id=?""",
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown embedding job {job_id}")
    if (
        str(row["status"]) != "leased"
        or str(row["lease_owner"]) != worker_id
        or row["lease_until_ns"] is None
        or int(row["lease_until_ns"]) <= now_ns
    ):
        raise SemanticStateError("job lease is absent, expired or owned elsewhere")
    should_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
    status = "pending" if should_retry else "error"
    available = now_ns + int(retry_delay_seconds * 1_000_000_000)
    bounded_type = error_type[:256]
    bounded_message = error_message[:MAX_ERROR_CHARS]
    updated = connection.execute(
        """UPDATE embedding_jobs SET status=?,available_ns=?,lease_owner=NULL,
        lease_until_ns=NULL,error_type=?,error_message=?,updated_ns=?
        WHERE job_id=? AND status='leased' AND lease_owner=?
          AND lease_until_ns>?""",
        (
            status,
            available,
            bounded_type,
            bounded_message,
            now_ns,
            job_id,
            worker_id,
            now_ns,
        ),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("job lease changed before failure was recorded")
    _record_embedding_attempt_failure(
        connection,
        row=row,
        status="failed",
        error_type=bounded_type,
        error_message=bounded_message,
        retryable=should_retry,
        now_ns=now_ns,
    )
    return status


def _record_stale_embedding_job_on_connection(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    worker_id: str,
    now_ns: int,
) -> None:
    """Convert a stale completion into a durable per-job terminal outcome."""

    row = connection.execute(
        "SELECT * FROM embedding_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown embedding job {job_id}")
    if (
        str(row["status"]) != "leased"
        or str(row["lease_owner"]) != worker_id
        or row["lease_until_ns"] is None
        or int(row["lease_until_ns"]) <= now_ns
    ):
        raise SemanticStateError("job lease is absent, expired or owned elsewhere")
    _record_embedding_attempt_failure(
        connection,
        row=row,
        status="failed",
        error_type="source_changed",
        error_message="source changed before vector completion",
        retryable=False,
        now_ns=now_ns,
    )
    updated = connection.execute(
        """UPDATE embedding_jobs SET status='stale',lease_owner=NULL,
        lease_until_ns=NULL,error_type='source_changed',
        error_message='source changed before vector completion',
        attempt_started_ns=NULL,updated_ns=?
        WHERE job_id=? AND status='leased' AND lease_owner=?
          AND lease_until_ns>?""",
        (now_ns, job_id, worker_id, now_ns),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("job lease changed before stale state was recorded")


def complete_embedding_jobs_batch(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    successes: Sequence[tuple[int, BackendEmbedding]],
    *,
    worker_id: str,
    now_ns: int | None = None,
) -> tuple[int, int]:
    """Complete a bounded set of embedding results in one write transaction.

    Each result retains the single-job CAS and receipt contract, while a
    savepoint prevents one malformed vector or stale source from rolling back
    unrelated results from the same inference batch.
    """

    if not worker_id.strip():
        raise ValueError("worker_id cannot be blank")
    if not successes:
        return 0, 0
    embedded = failed = 0
    with semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for ordinal, (index, output) in enumerate(successes):
            lease = leases[index]
            savepoint = f"embedding_completion_{ordinal}"
            connection.execute(f"SAVEPOINT {savepoint}")
            selected_ns = _now(now_ns)
            try:
                _complete_embedding_job_on_connection(
                    connection,
                    lease.job_id,
                    worker_id=worker_id,
                    vector=output.vector,
                    provenance={
                        **dict(output.provenance),
                        "pipeline": SEMANTIC_PIPELINE_VERSION,
                    },
                    now_ns=selected_ns,
                )
            except StaleEmbeddingJobError:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                _record_stale_embedding_job_on_connection(
                    connection,
                    lease.job_id,
                    worker_id=worker_id,
                    now_ns=_now(now_ns),
                )
                failed += 1
            except Exception as exc:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                _record_embedding_failure_on_connection(
                    connection,
                    lease.job_id,
                    worker_id=worker_id,
                    error_type=type(exc).__name__,
                    error_message=safe_error(exc),
                    now_ns=_now(now_ns),
                )
                failed += 1
            else:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                embedded += 1
    return embedded, failed


def _record_embedding_successes(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    successes: Sequence[tuple[int, BackendEmbedding]],
    *,
    worker_id: str,
) -> tuple[int, int]:
    return complete_embedding_jobs_batch(
        database,
        leases,
        successes,
        worker_id=worker_id,
    )


def _release_interrupted_leases(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    *,
    worker_id: str,
    interruption: BaseException,
) -> None:
    """Return every still-owned lease to durable retry without masking a cancel."""

    cleanup_errors: list[BaseException] = []
    for lease in leases:
        try:
            fail_embedding_job(
                database,
                lease.job_id,
                worker_id=worker_id,
                error_type=type(interruption).__name__,
                error_message=safe_error(interruption),
                retryable=True,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        interruption.add_note(
            "semantic lease cleanup encountered "
            f"{len(cleanup_errors)} error(s); first={safe_error(cleanup_errors[0])}"
        )


def _release_timed_out_leases(
    database: Path,
    leases: Sequence[EmbeddingJobLease],
    *,
    worker_id: str,
    interruption: BaseException,
) -> None:
    """Release timed-out inference leases without charging a model attempt."""

    cleanup_errors: list[BaseException] = []
    for lease in leases:
        try:
            release_embedding_job_lease_for_deadline(
                database,
                lease.job_id,
                worker_id=worker_id,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        interruption.add_note(
            "semantic timeout lease cleanup encountered "
            f"{len(cleanup_errors)} error(s); first={safe_error(cleanup_errors[0])}"
        )


def _generation_description(
    summary: GenerationSummary,
    backend: EmbeddingBackend,
    *,
    finished: bool,
    truncated: bool,
) -> str:
    selected_sources = summary.cursor.get("selected_sources", ())
    sources = (
        {str(value) for value in selected_sources if isinstance(value, str)}
        if isinstance(selected_sources, (list, tuple))
        else set()
    )
    if backend.model.modality is EmbeddingModality.IMAGE:
        label = "Embeddings visuales"
    elif "image-ocr" in sources:
        label = "Embeddings OCR de imágenes"
    else:
        label = "Embeddings de texto"
    if not finished:
        return label
    if truncated or summary.unfinished:
        return f"{label} pausados"
    if summary.errors or summary.stale:
        return f"{label} completados con incidencias"
    return f"{label} publicados"


def _emit_generation_progress(
    progress: ProgressCallback | None,
    summary: GenerationSummary,
    backend: EmbeddingBackend,
    *,
    reused: int,
    embedded: int,
    failed: int,
    finished: bool = False,
    truncated: bool = False,
) -> None:
    completed = summary.done + summary.errors + summary.stale
    total = completed + summary.unfinished
    observed_reused = max(reused, summary.done - embedded)
    emit_progress(
        progress,
        ProgressEvent(
            "semantic",
            f"generation:{summary.generation_id}",
            _generation_description(
                summary,
                backend,
                finished=finished,
                truncated=truncated,
            ),
            completed,
            total,
            "vectores",
            finished,
            (
                ProgressMetric("reused", observed_reused),
                ProgressMetric("embedded", embedded),
                ProgressMetric("errors", max(summary.errors, failed)),
                ProgressMetric("remaining", summary.unfinished),
                ProgressMetric("generation", summary.generation_id),
                ProgressMetric("status", summary.status),
            ),
        ),
    )


def _reuse_generation_jobs(
    database: Path,
    generation_id: int,
    backend: EmbeddingBackend,
    *,
    budget: SemanticWorkBudget,
    progress: ProgressCallback | None,
    reused: int,
    embedded: int,
    failed: int,
) -> tuple[GenerationSummary, int]:
    while count := reuse_cached_jobs(database, generation_id):
        reused += count
        summary = generation_summary(database, generation_id, writer_coordinated=True)
        _emit_generation_progress(
            progress,
            summary,
            backend,
            reused=reused,
            embedded=embedded,
            failed=failed,
        )
        if budget.deadline_expired():
            break
    return generation_summary(database, generation_id, writer_coordinated=True), reused


def _run_generation_batch(
    database: Path,
    generation_id: int,
    backend: EmbeddingBackend,
    *,
    worker_id: str,
    heartbeat_jobs: Callable[..., int],
    work_budget: SemanticWorkBudget | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, int, bool] | None:
    leases = claim_embedding_jobs(
        database,
        generation_id,
        worker_id=worker_id,
        limit=min(JOB_BATCH_SIZE, backend.max_batch_size),
        lease_seconds=WORKER_LEASE_SECONDS,
    )
    if not leases:
        return None

    batch_embedded = batch_failed = 0
    try:
        requests = tuple(embedding_request_from_lease(lease) for lease in leases)
        successes, embedding_failures = embed_requests_with_heartbeat(
            database,
            leases,
            worker_id=worker_id,
            backend=backend,
            requests=requests,
            heartbeat_jobs=heartbeat_jobs,
        )
        if work_budget is not None and work_budget.retry_recoverable_errors:
            retry_indices = tuple(
                index for index, exc in embedding_failures
                if isinstance(exc, OSError) and not isinstance(exc, SourceRevisionMismatchError)
            )
            if retry_indices:
                work_budget.checkpoint()
                emit_progress(progress, ProgressEvent(
                    "semantic", "retry", "Reintento único de inferencia local recuperable",
                    0, len(retry_indices), "trabajos",
                ))
                retry_successes, retry_failures = embed_requests_with_heartbeat(
                    database, tuple(leases[index] for index in retry_indices),
                    worker_id=worker_id, backend=backend,
                    requests=tuple(requests[index] for index in retry_indices),
                    heartbeat_jobs=heartbeat_jobs,
                )
                successes = tuple(sorted((
                    *successes,
                    *((retry_indices[index], output) for index, output in retry_successes),
                ), key=lambda value: value[0]))
                retry_set = set(retry_indices)
                embedding_failures = tuple(
                    (index, exc) for index, exc in embedding_failures if index not in retry_set
                ) + tuple((retry_indices[index], exc) for index, exc in retry_failures)
        batch_failed += _record_embedding_failures(
            database,
            leases,
            embedding_failures,
            worker_id=worker_id,
        )
        batch_embedded, completion_failures = _record_embedding_successes(
            database,
            leases,
            successes,
            worker_id=worker_id,
        )
        batch_failed += completion_failures
    except SemanticIndexDeadlineExceeded as exc:
        _release_timed_out_leases(
            database,
            leases,
            worker_id=worker_id,
            interruption=exc,
        )
        return batch_embedded, batch_failed, True
    except BaseException as exc:
        _release_interrupted_leases(
            database,
            leases,
            worker_id=worker_id,
            interruption=exc,
        )
        raise
    return batch_embedded, batch_failed, False


def _finish_generation(
    database: Path,
    generation_id: int,
    *,
    budget: SemanticWorkBudget,
    publish_if_complete: bool,
) -> tuple[GenerationSummary, bool]:
    summary = generation_summary(database, generation_id, writer_coordinated=True)
    deadline_expired = budget.deadline_expired()
    if (
        not summary.unfinished
        and publish_if_complete
        and not budget.truncated
        and not deadline_expired
    ):
        summary = finalize_embedding_generation(
            database,
            generation_id,
            allow_partial=True,
        )
    return summary, deadline_expired


def run_generation(
    database: Path,
    generation_id: int,
    backend: EmbeddingBackend,
    *,
    queued: int,
    work_budget: SemanticWorkBudget | None = None,
    publish_if_complete: bool = True,
    heartbeat_jobs: Callable[..., int] = heartbeat_embedding_jobs,
    progress: ProgressCallback | None = None,
) -> GenerationWorkResult:
    budget = work_budget or unlimited_semantic_work_budget()
    reused = 0
    embedded = failed = 0
    worker_id = f"semantic-worker:{os.getpid()}:{generation_id}"
    stop_reason: str | None = None
    while True:
        summary = generation_summary(database, generation_id, writer_coordinated=True)
        _emit_generation_progress(
            progress,
            summary,
            backend,
            reused=reused,
            embedded=embedded,
            failed=failed,
        )
        if budget.deadline_expired() or not summary.unfinished:
            break
        summary, reused = _reuse_generation_jobs(
            database,
            generation_id,
            backend,
            budget=budget,
            progress=progress,
            reused=reused,
            embedded=embedded,
            failed=failed,
        )
        if not summary.unfinished or budget.deadline_expired():
            break
        batch = _run_generation_batch(
            database,
            generation_id,
            backend,
            worker_id=worker_id,
            heartbeat_jobs=heartbeat_jobs,
            work_budget=budget,
            progress=progress,
        )
        if batch is None:
            stop_reason = "no_progress"
            break
        batch_embedded, batch_failed, batch_deadline_expired = batch
        embedded += batch_embedded
        failed += batch_failed
        if batch_deadline_expired:
            budget.mark_truncated("time_budget")
            break
        if batch_embedded == batch_failed == 0:
            stop_reason = "no_progress"
            break
        if batch_failed and budget.retry_recoverable_errors:
            # A persistent failure stays durable/retryable, but must not be
            # claimed again after its backoff within this same invocation.
            # Other models retain the unspent shared budget.
            current = generation_summary(database, generation_id, writer_coordinated=True)
            stop_reason = "review_required" if current.errors else "retry_required"
            break

    if stop_reason is not None:
        emit_progress(progress, ProgressEvent(
            "semantic", "blocked", "Modelo incompleto; se conserva el avance para reanudación",
            0, None, "trabajos", metrics=(ProgressMetric("reason", stop_reason),),
        ))

    summary, deadline_expired = _finish_generation(
        database,
        generation_id,
        budget=budget,
        publish_if_complete=publish_if_complete and stop_reason != "no_progress",
    )
    _emit_generation_progress(
        progress,
        summary,
        backend,
        reused=reused,
        embedded=embedded,
        failed=failed,
        finished=True,
        truncated=budget.truncated or deadline_expired,
    )
    return GenerationWorkResult(summary, queued, reused, embedded, failed, stop_reason=stop_reason)


# endregion [03]
