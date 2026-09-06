"""Resumable semantic embedding generations and bounded job queue."""

from __future__ import annotations
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .semantic_item_repository import _decode_chunk_text
from .semantic_lineage_repository import (
    _embedding_member_binding_by_id,
    _payload_causation_receipts,
    _producer_receipts_for_embedding_members,
    _record_discarded_embedding_execution,
    _record_embedding_clone_batch,
    _record_embedding_attempt_failure,
    _record_embedding_receipt,
    _record_generation_publication_receipt,
    _snapshot_chunk_revision,
    _snapshot_item_revision,
)
from .semantic_models import (
    EmbeddingJobLease,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    GenerationSummary,
    SemanticEntityKind,
    canonical_json,
    encode_vector,
)
from .semantic_repository_common import (
    MAX_ERROR_CHARS,
    MAX_WRITE_BATCH,
    StaleEmbeddingJobError,
    _batches,
    _check_batch_size,
    _fingerprint_from_row,
    _load_model,
    _now,
    _same_fingerprint,
)
from .semantic_schema import SemanticStateError, semantic_database
from .semantic_work_budget import SemanticWorkBudget


@dataclass(frozen=True, slots=True)
class EnqueueJobBatchResult:
    """Exact durable queue growth for one ordered entity prefix."""

    touched: int
    new_jobs: int
    complete: bool
    rebound_members: int = 0


class EmbeddingGenerationRebaseRequiredError(SemanticStateError):
    """A building snapshot lost the compare-and-swap race for its head."""

    def __init__(
        self,
        generation_id: int,
        *,
        expected_head: int | None,
        observed_head: int | None,
    ) -> None:
        self.generation_id = generation_id
        self.expected_head = expected_head
        self.observed_head = observed_head
        super().__init__(
            "published embedding head changed; generation must be rebased "
            f"(generation={generation_id}, expected={expected_head}, "
            f"observed={observed_head})"
        )


# region [05] Resumable generations and job queue


_BASE_CLONE_CURSOR_PROTOCOL = "base-member-snapshot-v1"


def _base_clone_cursor_int(
    value: Mapping[str, object],
    name: str,
) -> int:
    selected = value.get(name)
    if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
        raise SemanticStateError(f"generation base clone cursor {name} is invalid")
    return selected


def _load_base_clone_generation(
    connection: sqlite3.Connection,
    generation_id: int,
) -> sqlite3.Row:
    generation = connection.execute(
        """SELECT status,model_signature,base_generation_id,
            base_clone_complete,cursor_json
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise KeyError(f"unknown embedding generation {generation_id}")
    if str(generation["status"]) != "building":
        raise SemanticStateError(f"generation {generation_id} stopped building during base clone")
    return generation


def _decode_base_clone_cursor(raw: object) -> dict[str, object]:
    try:
        cursor = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise SemanticStateError("generation cursor is not valid JSON during base clone") from exc
    if not isinstance(cursor, dict):
        raise SemanticStateError("generation cursor is not an object during base clone")
    return cursor


def _validate_base_clone_snapshot(
    connection: sqlite3.Connection,
    generation: sqlite3.Row,
    base_generation_id: int,
) -> tuple[int, int]:
    base = connection.execute(
        """SELECT status,model_signature FROM embedding_generations
        WHERE generation_id=?""",
        (base_generation_id,),
    ).fetchone()
    if base is None or str(base["status"]) != "ready":
        raise SemanticStateError("generation base snapshot is absent or not immutable-ready")
    if str(base["model_signature"]) != str(generation["model_signature"]):
        raise SemanticStateError("generation base snapshot model differs from candidate")
    snapshot = connection.execute(
        """SELECT COALESCE(MAX(member_id),0),COUNT(*)
        FROM embedding_generation_members WHERE generation_id=?""",
        (base_generation_id,),
    ).fetchone()
    return int(snapshot[0]), int(snapshot[1])


def _new_base_clone_cursor(
    connection: sqlite3.Connection,
    generation_id: int,
    base_generation_id: int,
    *,
    last_member_id: int,
    base_member_count: int,
) -> dict[str, object]:
    existing = connection.execute(
        """SELECT COALESCE(MAX(base_member_id),0)
        FROM embedding_generation_members WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    after_member_id = int(existing[0])
    scanned_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_generation_members
            WHERE generation_id=? AND member_id<=?""",
            (base_generation_id, after_member_id),
        ).fetchone()[0]
    )
    return {
        "protocol": _BASE_CLONE_CURSOR_PROTOCOL,
        "base_generation_id": base_generation_id,
        "after_member_id": after_member_id,
        "last_member_id": last_member_id,
        "base_member_count": base_member_count,
        "scanned_members": scanned_members,
        "complete": False,
    }


def _resume_base_clone_cursor(
    connection: sqlite3.Connection,
    generation_id: int,
    raw_cursor: object,
    base_generation_id: int,
) -> dict[str, object]:
    if not isinstance(raw_cursor, dict):
        raise SemanticStateError("generation base clone cursor is not an object")
    clone_cursor = dict(raw_cursor)
    if clone_cursor.get("protocol") != _BASE_CLONE_CURSOR_PROTOCOL:
        raise SemanticStateError("generation base clone cursor protocol is incompatible")
    if _base_clone_cursor_int(clone_cursor, "base_generation_id") != base_generation_id:
        raise SemanticStateError("generation base clone cursor changed its base snapshot")
    after_member_id = _base_clone_cursor_int(clone_cursor, "after_member_id")
    last_member_id = _base_clone_cursor_int(clone_cursor, "last_member_id")
    base_member_count = _base_clone_cursor_int(clone_cursor, "base_member_count")
    scanned_members = _base_clone_cursor_int(clone_cursor, "scanned_members")
    if (
        after_member_id > last_member_id
        or scanned_members > base_member_count
        or not isinstance(clone_cursor.get("complete"), bool)
    ):
        raise SemanticStateError("generation base clone cursor bounds are invalid")
    observed_after = int(
        connection.execute(
            """SELECT COALESCE(MAX(base_member_id),0)
            FROM embedding_generation_members WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()[0]
    )
    if observed_after > after_member_id:
        raise SemanticStateError("generation base members advanced beyond their durable cursor")
    return clone_cursor


def _prepare_base_clone_cursor(
    connection: sqlite3.Connection,
    generation: sqlite3.Row,
    generation_id: int,
    base_generation_id: int,
) -> dict[str, object]:
    last_member_id, base_member_count = _validate_base_clone_snapshot(
        connection,
        generation,
        base_generation_id,
    )
    cursor = _decode_base_clone_cursor(generation["cursor_json"])
    raw_clone_cursor = cursor.get("base_clone")
    if raw_clone_cursor is None:
        return _new_base_clone_cursor(
            connection,
            generation_id,
            base_generation_id,
            last_member_id=last_member_id,
            base_member_count=base_member_count,
        )
    return _resume_base_clone_cursor(
        connection,
        generation_id,
        raw_clone_cursor,
        base_generation_id,
    )


def _base_clone_rows(
    connection: sqlite3.Connection,
    base_generation_id: int,
    generation_id: int,
    clone_cursor: Mapping[str, object],
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT base_member.generation_id,base_member.member_id,
            base_member.model_signature,base_member.entity_kind,
            base_member.entity_id,base_member.item_id,
            base_member.item_revision_id,base_member.chunk_revision_id,
            base_member.payload_id,base_member.content_xxh3_128,
            base_member.content_bytes,base_member.content_xxh3_64_guard,
            base_member.provenance_json,base_member.updated_ns,
            EXISTS(
                SELECT 1 FROM embedding_jobs target_job
                WHERE target_job.generation_id=?
                  AND target_job.entity_kind=base_member.entity_kind
                  AND target_job.entity_id=base_member.entity_id
            ) AS has_target_job
        FROM embedding_generation_members base_member
        WHERE base_member.generation_id=? AND base_member.member_id>?
          AND base_member.member_id<=?
        ORDER BY base_member.member_id LIMIT ?""",
        (
            generation_id,
            base_generation_id,
            _base_clone_cursor_int(clone_cursor, "after_member_id"),
            _base_clone_cursor_int(clone_cursor, "last_member_id"),
            MAX_WRITE_BATCH,
        ),
    ).fetchall()


def _insert_base_clone_rows(
    connection: sqlite3.Connection,
    generation_id: int,
    rows: Sequence[sqlite3.Row],
) -> None:
    connection.executemany(
        """INSERT INTO embedding_generation_members(
            generation_id,model_signature,entity_kind,entity_id,item_id,
            item_revision_id,chunk_revision_id,payload_id,
            content_xxh3_128,content_bytes,content_xxh3_64_guard,
            provenance_json,updated_ns,base_member_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(generation_id,entity_kind,entity_id) DO NOTHING""",
        (
            (
                generation_id,
                str(row["model_signature"]),
                str(row["entity_kind"]),
                str(row["entity_id"]),
                str(row["item_id"]),
                int(row["item_revision_id"]),
                None if row["chunk_revision_id"] is None else int(row["chunk_revision_id"]),
                int(row["payload_id"]),
                str(row["content_xxh3_128"]),
                int(row["content_bytes"]),
                str(row["content_xxh3_64_guard"]),
                str(row["provenance_json"]),
                int(row["updated_ns"]),
                int(row["member_id"]),
            )
            for row in rows
            if not bool(row["has_target_job"])
        ),
    )
    _record_embedding_clone_batch(
        connection,
        generation_id=generation_id,
        base_generation_id=int(rows[0]["generation_id"]),
        base_member_ids=tuple(int(row["member_id"]) for row in rows),
        now_ns=_now(None),
    )


def _finish_base_clone(
    connection: sqlite3.Connection,
    generation_id: int,
    base_generation_id: int,
    cursor: dict[str, object],
    clone_cursor: dict[str, object],
) -> None:
    last_member_id = _base_clone_cursor_int(clone_cursor, "last_member_id")
    base_member_count = _base_clone_cursor_int(clone_cursor, "base_member_count")
    scanned_members = _base_clone_cursor_int(clone_cursor, "scanned_members")
    observed_snapshot = connection.execute(
        """SELECT COALESCE(MAX(member_id),0),COUNT(*)
        FROM embedding_generation_members WHERE generation_id=?""",
        (base_generation_id,),
    ).fetchone()
    if (
        int(observed_snapshot[0]) != last_member_id
        or int(observed_snapshot[1]) != base_member_count
        or scanned_members != base_member_count
    ):
        raise SemanticStateError("generation base snapshot changed during resumable clone")
    clone_cursor.update(
        {
            "after_member_id": last_member_id,
            "scanned_members": base_member_count,
            "complete": True,
        }
    )
    cursor["base_clone"] = clone_cursor
    connection.execute(
        """UPDATE embedding_generations
        SET base_clone_complete=1,cursor_json=? """
        "WHERE generation_id=? AND status='building'",
        (canonical_json(cursor), generation_id),
    )


def _persist_base_clone_page(
    connection: sqlite3.Connection,
    generation_id: int,
    cursor: dict[str, object],
    clone_cursor: dict[str, object],
    rows: Sequence[sqlite3.Row],
) -> None:
    # Metadata-only overrides intentionally keep base_member_id NULL.
    # Advance by the scanned base page as well as by inserted rows so a fully
    # overridden page cannot make the resumable clone loop spin.
    clone_cursor.update(
        {
            "after_member_id": int(rows[-1]["member_id"]),
            "scanned_members": _base_clone_cursor_int(
                clone_cursor,
                "scanned_members",
            )
            + len(rows),
            "complete": False,
        }
    )
    cursor["base_clone"] = clone_cursor
    updated = connection.execute(
        """UPDATE embedding_generations SET cursor_json=?
        WHERE generation_id=? AND status='building'
          AND base_clone_complete=0""",
        (canonical_json(cursor), generation_id),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("generation changed while persisting its base clone cursor")


def _clone_published_members(
    path: Path,
    generation_id: int,
    *,
    work_budget: SemanticWorkBudget | None = None,
) -> None:
    """Resume a deadline-aware copy of one pinned immutable base snapshot."""

    while True:
        if work_budget is not None:
            work_budget.checkpoint()
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = _load_base_clone_generation(connection, generation_id)
            if bool(generation["base_clone_complete"]):
                return
            cursor = _decode_base_clone_cursor(generation["cursor_json"])
            base_generation_id = generation["base_generation_id"]
            if base_generation_id is None:
                cursor["base_clone"] = {
                    "protocol": _BASE_CLONE_CURSOR_PROTOCOL,
                    "base_generation_id": None,
                    "after_member_id": 0,
                    "last_member_id": 0,
                    "base_member_count": 0,
                    "scanned_members": 0,
                    "complete": True,
                }
                connection.execute(
                    """UPDATE embedding_generations
                    SET base_clone_complete=1,cursor_json=? """
                    "WHERE generation_id=? AND status='building'",
                    (canonical_json(cursor), generation_id),
                )
                return
            selected_base_id = int(base_generation_id)
            clone_cursor = _prepare_base_clone_cursor(
                connection,
                generation,
                generation_id,
                selected_base_id,
            )
            rows = _base_clone_rows(
                connection,
                selected_base_id,
                generation_id,
                clone_cursor,
            )
            if not rows:
                _finish_base_clone(
                    connection,
                    generation_id,
                    selected_base_id,
                    cursor,
                    clone_cursor,
                )
                return
            _insert_base_clone_rows(connection, generation_id, rows)
            _persist_base_clone_page(
                connection,
                generation_id,
                cursor,
                clone_cursor,
                rows,
            )


def _published_head_id(
    connection: sqlite3.Connection,
    model_signature: str,
) -> int | None:
    row = connection.execute(
        "SELECT generation_id FROM published_embedding_heads WHERE model_signature=?",
        (model_signature,),
    ).fetchone()
    return None if row is None else int(row[0])


def _record_generation_failure(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    completed_ns: int,
    details: Mapping[str, object],
    summary: GenerationSummary | None = None,
) -> None:
    """Close a building candidate without deleting its jobs or snapshots."""

    selected_summary = summary or _generation_summary_row(connection, generation_id)
    if selected_summary.status != "building":
        raise SemanticStateError("only a building generation can be invalidated")
    cursor = dict(selected_summary.cursor)
    cursor.update(details)
    updated = connection.execute(
        """UPDATE embedding_generations SET status='failed',completed_ns=?,
            cursor_json=?,pending_count=?,leased_count=?,done_count=?,
            error_count=?,stale_count=?
        WHERE generation_id=? AND status='building'""",
        (
            completed_ns,
            canonical_json(cursor),
            selected_summary.pending,
            selected_summary.leased,
            selected_summary.done,
            selected_summary.errors,
            selected_summary.stale,
            generation_id,
        ),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("generation changed before its failure was recorded")


def _mark_generation_head_conflict(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    observed_head: int | None,
    completed_ns: int,
    summary: GenerationSummary | None = None,
) -> EmbeddingGenerationRebaseRequiredError:
    """Persist one terminal CAS loss without deleting referenced work."""

    generation = connection.execute(
        """SELECT model_signature,base_generation_id,status
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise KeyError(f"unknown embedding generation {generation_id}")
    expected_head = (
        None if generation["base_generation_id"] is None else int(generation["base_generation_id"])
    )
    conflict = EmbeddingGenerationRebaseRequiredError(
        generation_id,
        expected_head=expected_head,
        observed_head=observed_head,
    )
    if str(generation["status"]) != "building":
        return conflict
    _record_generation_failure(
        connection,
        generation_id,
        completed_ns=completed_ns,
        summary=summary,
        details={
            "failure_reason": "published_head_changed",
            "expected_head": expected_head,
            "observed_head": observed_head,
            "retryable": True,
        },
    )
    return conflict


def invalidate_embedding_generations_for_source_change(
    path: Path,
    generation_ids: Sequence[int],
    *,
    expected_source_heads: Sequence[Mapping[str, object]],
    observed_source_heads: Sequence[Mapping[str, object]],
    completed_ns: int | None = None,
) -> None:
    """Atomically fail only this invocation's text or image/OCR candidates."""

    ids = tuple(generation_ids)
    if (
        not 1 <= len(ids) <= 2
        or len(set(ids)) != len(ids)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in ids)
    ):
        raise ValueError("source invalidation requires one or two unique generation IDs")
    expected = list(expected_source_heads)
    observed = list(observed_source_heads)
    if expected == observed:
        raise ValueError("source heads have not changed")
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for generation_id in ids:
            row = connection.execute(
                """SELECT generation.status,generation.provenance_json,
                    EXISTS(SELECT 1 FROM published_embedding_heads head
                        WHERE head.generation_id=generation.generation_id) AS published
                FROM embedding_generations generation WHERE generation.generation_id=?""",
                (generation_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "building" or bool(row["published"]):
                raise SemanticStateError("source invalidation requires an unpublished candidate")
            provenance = json.loads(str(row["provenance_json"]))
            if not isinstance(provenance, dict) or provenance.get("source_heads") != expected:
                raise SemanticStateError("source invalidation does not match candidate provenance")
        now_ns = _now(completed_ns)
        for generation_id in ids:
            _record_generation_failure(
                connection,
                generation_id,
                completed_ns=now_ns,
                details={
                    "failure_reason": "source_heads_changed",
                    "observed_source_heads": observed,
                    "retryable": True,
                },
            )


def _fail_empty_superseded_generations(
    connection: sqlite3.Connection,
    *,
    model_signature: str,
    processing_signature: str,
    completed_ns: int,
) -> None:
    """Close incompatible candidates that never cloned or queued durable work."""

    rows = connection.execute(
        """SELECT generation_id,cursor_json FROM embedding_generations AS generation
        WHERE generation.model_signature=? AND generation.status='building'
          AND generation.processing_signature<>?
          AND generation.base_clone_complete=0
          AND NOT EXISTS(
            SELECT 1 FROM embedding_generation_members AS member
            WHERE member.generation_id=generation.generation_id)
          AND NOT EXISTS(
            SELECT 1 FROM embedding_jobs AS job
            WHERE job.generation_id=generation.generation_id)
        ORDER BY generation.generation_id""",
        (model_signature, processing_signature),
    ).fetchall()
    for row in rows:
        cursor = _decode_base_clone_cursor(row["cursor_json"])
        cursor.update(
            {
                "failure_reason": "processing_signature_superseded_before_work",
                "retryable": False,
                "superseded_by_processing_signature": processing_signature,
            }
        )
        updated = connection.execute(
            """UPDATE embedding_generations
            SET status='failed',completed_ns=?,cursor_json=?
            WHERE generation_id=? AND status='building'""",
            (
                completed_ns,
                canonical_json(cursor),
                int(row["generation_id"]),
            ),
        )
        if updated.rowcount != 1:
            raise SemanticStateError("empty embedding generation changed while being superseded")


def start_embedding_generation(
    path: Path,
    *,
    model_signature: str,
    processing_signature: str,
    provenance: Mapping[str, object] | None = None,
    cursor: Mapping[str, object] | None = None,
    started_ns: int | None = None,
    materialize_base: bool = True,
    work_budget: SemanticWorkBudget | None = None,
) -> int:
    """Return an existing compatible building generation or start a new one."""

    if not processing_signature.strip():
        raise ValueError("processing_signature cannot be blank")
    if not isinstance(materialize_base, bool):
        raise TypeError("materialize_base must be a boolean")
    selected_ns = _now(started_ns)
    provenance_json = canonical_json(provenance)
    cursor_json = canonical_json(cursor)
    generation_id: int
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _load_model(connection, model_signature)
        base_generation_id = _published_head_id(connection, model_signature)
        _fail_empty_superseded_generations(
            connection,
            model_signature=model_signature,
            processing_signature=processing_signature,
            completed_ns=selected_ns,
        )
        existing = connection.execute(
            """SELECT generation_id,provenance_json,base_generation_id,
                base_clone_complete FROM embedding_generations
            WHERE model_signature=? AND processing_signature=? AND status='building'
            ORDER BY generation_id DESC LIMIT 1""",
            (model_signature, processing_signature),
        ).fetchone()
        if existing is not None:
            existing_base = (
                None
                if existing["base_generation_id"] is None
                else int(existing["base_generation_id"])
            )
            if existing_base != base_generation_id:
                _mark_generation_head_conflict(
                    connection,
                    int(existing["generation_id"]),
                    observed_head=base_generation_id,
                    completed_ns=selected_ns,
                )
                existing = None
        if existing is not None:
            if str(existing["provenance_json"]) != provenance_json:
                # Source adapters are content-addressed inputs.  A published
                # owner may advance while a building generation is paused;
                # retaining the old candidate as building makes every later
                # resume fail with a provenance mismatch and strands its
                # durable jobs.  Close that candidate, preserve its jobs and
                # start a fresh generation against the current manifest.
                _record_generation_failure(
                    connection,
                    int(existing["generation_id"]),
                    completed_ns=selected_ns,
                    details={
                        "failure_reason": "provenance_changed",
                        "retryable": True,
                    },
                )
                existing = None
            else:
                generation_id = int(existing["generation_id"])
                if existing["base_generation_id"] is None and not bool(
                    existing["base_clone_complete"]
                ):
                    connection.execute(
                        """UPDATE embedding_generations
                        SET base_generation_id=?,base_clone_complete=?
                        WHERE generation_id=? AND status='building'""",
                        (
                            base_generation_id,
                            int(base_generation_id is None),
                            generation_id,
                        ),
                    )
        if existing is None:
            cursor_row = connection.execute(
                """INSERT INTO embedding_generations(
                    model_signature,processing_signature,status,provenance_json,
                    cursor_json,started_ns,base_generation_id,base_clone_complete)
                VALUES(?,?,'building',?,?,?,?,?)""",
                (
                    model_signature,
                    processing_signature,
                    provenance_json,
                    cursor_json,
                    selected_ns,
                    base_generation_id,
                    int(base_generation_id is None),
                ),
            )
            if cursor_row.lastrowid is None:
                raise SemanticStateError("generation insert did not return an identifier")
            generation_id = int(cursor_row.lastrowid)
    if materialize_base:
        _clone_published_members(
            path,
            generation_id,
            work_budget=work_budget,
        )
    return generation_id


def _generation_model(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    require_building: bool,
) -> EmbeddingModelSpec:
    row = connection.execute(
        "SELECT model_signature,status FROM embedding_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown embedding generation {generation_id}")
    if require_building and str(row["status"]) != "building":
        raise SemanticStateError(f"generation {generation_id} is not building")
    return _load_model(connection, str(row["model_signature"]))


@dataclass(frozen=True, slots=True)
class _QueueJobContext:
    generation_id: int
    model_signature: str
    entity_kind: SemanticEntityKind
    role: EmbeddingRole
    max_attempts: int
    now_ns: int
    rebind_causations: Mapping[int, _RebindCausation]


@dataclass(frozen=True, slots=True)
class _RebindCausation:
    execution_mode: str
    receipt_id: int


@dataclass(frozen=True, slots=True)
class _QueueSelection:
    rows: tuple[sqlite3.Row, ...]
    force_pending_ids: tuple[str, ...]
    new_jobs: int
    complete: bool
    rebound_members: int


_REBIND_GENERATION_MEMBER_SQL = """INSERT INTO embedding_generation_members(
    generation_id,model_signature,entity_kind,entity_id,item_id,
    item_revision_id,chunk_revision_id,payload_id,
    content_xxh3_128,content_bytes,content_xxh3_64_guard,
    provenance_json,updated_ns,base_member_id)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
ON CONFLICT(generation_id,entity_kind,entity_id) DO UPDATE SET
    item_id=excluded.item_id,
    item_revision_id=excluded.item_revision_id,
    chunk_revision_id=excluded.chunk_revision_id,
    payload_id=excluded.payload_id,
    content_xxh3_128=excluded.content_xxh3_128,
    content_bytes=excluded.content_bytes,
    content_xxh3_64_guard=excluded.content_xxh3_64_guard,
    provenance_json=excluded.provenance_json,
    updated_ns=excluded.updated_ns,
    base_member_id=NULL"""


_UPSERT_EMBEDDING_JOB_SQL = """INSERT INTO embedding_jobs(
        generation_id,model_signature,role,entity_kind,entity_id,item_id,
        input_item_revision_id,input_chunk_revision_id,
        content_xxh3_128,content_bytes,content_xxh3_64_guard,status,
        max_attempts,available_ns,created_ns,updated_ns)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)
    ON CONFLICT(generation_id,entity_kind,entity_id) DO UPDATE SET
        item_id=excluded.item_id,
        input_item_revision_id=excluded.input_item_revision_id,
        input_chunk_revision_id=excluded.input_chunk_revision_id,
        content_xxh3_128=excluded.content_xxh3_128,
        content_bytes=excluded.content_bytes,
        content_xxh3_64_guard=excluded.content_xxh3_64_guard,
        status=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN 'pending' ELSE embedding_jobs.status END,
        attempts=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN 0 ELSE embedding_jobs.attempts END,
        max_attempts=excluded.max_attempts,
        available_ns=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN excluded.available_ns ELSE embedding_jobs.available_ns END,
        lease_owner=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN NULL ELSE embedding_jobs.lease_owner END,
        lease_until_ns=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN NULL ELSE embedding_jobs.lease_until_ns END,
        error_type=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN NULL ELSE embedding_jobs.error_type END,
        error_message=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN NULL ELSE embedding_jobs.error_message END,
        attempt_started_ns=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN NULL ELSE embedding_jobs.attempt_started_ns END,
        attempt_sequence=CASE WHEN
            embedding_jobs.content_xxh3_128<>excluded.content_xxh3_128 OR
            embedding_jobs.content_bytes<>excluded.content_bytes OR
            embedding_jobs.content_xxh3_64_guard<>excluded.content_xxh3_64_guard
            THEN 0 ELSE embedding_jobs.attempt_sequence END,
        updated_ns=excluded.updated_ns"""


def _queue_entity_rows(
    connection: sqlite3.Connection,
    entity_kind: SemanticEntityKind,
    identifiers: tuple[str, ...],
) -> tuple[sqlite3.Row, ...]:
    placeholders = ",".join("?" for _ in identifiers)
    if entity_kind is SemanticEntityKind.TEXT_CHUNK:
        rows = connection.execute(
            f"""SELECT c.chunk_id AS entity_id,c.item_id,
                c.content_xxh3_128,c.content_bytes,c.content_xxh3_64_guard,
                COALESCE(
                    derivation.item_revision_id,
                    (SELECT legacy.item_revision_id
                     FROM embedding_generation_members legacy
                     JOIN published_embedding_heads legacy_head
                       ON legacy_head.generation_id=legacy.generation_id
                     WHERE legacy.chunk_revision_id=revision.chunk_revision_id
                       AND legacy.entity_kind='text_chunk'
                     ORDER BY legacy.member_id DESC LIMIT 1)
                ) AS input_item_revision_id,
                revision.chunk_revision_id AS input_chunk_revision_id
            FROM text_chunks c JOIN semantic_items i ON i.item_id=c.item_id
            JOIN semantic_chunk_revisions revision ON revision.chunk_id=c.chunk_id
            LEFT JOIN semantic_chunk_derivations derivation
              ON derivation.derivation_id=(
                SELECT MAX(candidate.derivation_id)
                FROM semantic_chunk_derivations candidate
                WHERE candidate.chunk_revision_id=revision.chunk_revision_id
                  AND candidate.refresh_token=c.refresh_token)
            WHERE c.active=1 AND i.active=1 AND c.chunk_id IN ({placeholders})""",
            identifiers,
        ).fetchall()
    else:
        rows = connection.execute(
            f"""SELECT item_id AS entity_id,item_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,
                NULL AS input_item_revision_id,NULL AS input_chunk_revision_id
            FROM semantic_items WHERE active=1 AND path IS NOT NULL
                AND item_id IN ({placeholders})""",
            identifiers,
        ).fetchall()
    rows_by_id = {str(row["entity_id"]): row for row in rows}
    missing = set(identifiers).difference(rows_by_id)
    if missing:
        raise KeyError(f"unknown, inactive or payload-less entities: {sorted(missing)}")
    return tuple(rows_by_id[identifier] for identifier in identifiers)


def _queue_generation(
    connection: sqlite3.Connection,
    generation_id: int,
    model_signature: str,
) -> sqlite3.Row:
    generation = connection.execute(
        """SELECT status,model_signature,base_generation_id,base_clone_complete
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise KeyError(f"unknown embedding generation {generation_id}")
    if str(generation["status"]) != "building":
        raise SemanticStateError(f"generation {generation_id} is not building")
    if str(generation["model_signature"]) != model_signature:
        raise ValueError("embedding generation model does not match queued entities")
    return generation


def _queue_existing_jobs(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    identifiers: tuple[str, ...],
) -> dict[str, sqlite3.Row]:
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"""SELECT job_id,entity_id,status,content_xxh3_128,content_bytes,
        content_xxh3_64_guard FROM embedding_jobs
        WHERE generation_id=? AND entity_kind=?
          AND entity_id IN ({placeholders})""",
        (context.generation_id, context.entity_kind.value, *identifiers),
    ).fetchall()
    return {str(row["entity_id"]): row for row in rows}


def _member_rows(
    connection: sqlite3.Connection,
    generation_id: int,
    entity_kind: SemanticEntityKind,
    identifiers: tuple[str, ...],
) -> tuple[sqlite3.Row, ...]:
    placeholders = ",".join("?" for _ in identifiers)
    return tuple(
        connection.execute(
            f"""SELECT member_id,model_signature,entity_kind,entity_id,item_id,
                item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
                content_bytes,content_xxh3_64_guard,provenance_json,updated_ns,
                base_member_id
            FROM embedding_generation_members
            WHERE generation_id=? AND entity_kind=?
              AND entity_id IN ({placeholders})""",
            (generation_id, entity_kind.value, *identifiers),
        ).fetchall()
    )


def _queue_generation_members(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    generation: sqlite3.Row,
    identifiers: tuple[str, ...],
) -> dict[str, sqlite3.Row]:
    members = {
        str(row["entity_id"]): row
        for row in _member_rows(
            connection,
            context.generation_id,
            context.entity_kind,
            identifiers,
        )
    }
    if bool(generation["base_clone_complete"]) or generation["base_generation_id"] is None:
        return members
    missing = tuple(identifier for identifier in identifiers if identifier not in members)
    if not missing:
        return members
    base_rows = _member_rows(
        connection,
        int(generation["base_generation_id"]),
        context.entity_kind,
        missing,
    )
    members.update(
        (str(row["entity_id"]), row) for row in base_rows if str(row["entity_id"]) not in members
    )
    return members


def _snapshot_queue_revisions(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    row: sqlite3.Row,
    item_revisions: dict[str, int],
) -> tuple[int, int | None]:
    item_id = str(row["item_id"])
    item_revision_id = item_revisions.get(item_id)
    if item_revision_id is None:
        item_revision_id = _snapshot_item_revision(connection, item_id, context.now_ns)
        item_revisions[item_id] = item_revision_id
    chunk_revision_id = (
        _snapshot_chunk_revision(
            connection,
            str(row["entity_id"]),
            _fingerprint_from_row(row),
            context.now_ns,
            require_published=False,
        )
        if context.entity_kind is SemanticEntityKind.TEXT_CHUNK
        else None
    )
    return item_revision_id, chunk_revision_id


def _same_item_identity(
    connection: sqlite3.Connection,
    prior_item_revision_id: int,
    item_revision_id: int,
) -> bool:
    rows = connection.execute(
        """SELECT item_revision_id,source_kind,source_identity,identity_version
        FROM semantic_item_revisions WHERE item_revision_id IN (?,?)""",
        (prior_item_revision_id, item_revision_id),
    ).fetchall()
    identities = {
        int(identity["item_revision_id"]): (
            str(identity["source_kind"]),
            str(identity["source_identity"]),
            str(identity["identity_version"]),
        )
        for identity in rows
    }
    return (
        identities.get(prior_item_revision_id) == identities.get(item_revision_id)
        and identities.get(item_revision_id) is not None
    )


def _finish_existing_job(
    connection: sqlite3.Connection,
    prior: sqlite3.Row | None,
    now_ns: int,
) -> None:
    if prior is None or str(prior["status"]) == "done":
        return
    connection.execute(
        """UPDATE embedding_jobs SET status='done',lease_owner=NULL,
            lease_until_ns=NULL,error_type=NULL,error_message=NULL,
            updated_ns=? WHERE job_id=?""",
        (now_ns, int(prior["job_id"])),
    )


def _rebind_generation_member(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    row: sqlite3.Row,
    member: sqlite3.Row,
    item_revision_id: int,
    chunk_revision_id: int | None,
    prior: sqlite3.Row | None,
) -> None:
    source_member_id = int(member["member_id"])
    source_member_binding = _embedding_member_binding_by_id(
        connection,
        source_member_id,
    )
    if source_member_id not in context.rebind_causations:
        raise SemanticStateError("semantic rebind member was not causally classified")
    causation = context.rebind_causations[source_member_id]
    execution_mode = causation.execution_mode
    source_generation_id = source_member_binding.get("generation_id")
    if source_generation_id == context.generation_id:
        if execution_mode != "cache_hit":
            raise SemanticStateError(
                "semantic same-generation rebind must reuse its immutable payload"
            )
        deleted = connection.execute(
            """DELETE FROM embedding_generation_members
            WHERE member_id=? AND generation_id=?""",
            (source_member_id, context.generation_id),
        )
        if deleted.rowcount != 1:
            raise SemanticStateError("semantic cloned member changed before immutable rebind")
    connection.execute(
        _REBIND_GENERATION_MEMBER_SQL,
        (
            context.generation_id,
            str(member["model_signature"]),
            context.entity_kind.value,
            str(row["entity_id"]),
            str(row["item_id"]),
            item_revision_id,
            chunk_revision_id,
            int(member["payload_id"]),
            str(member["content_xxh3_128"]),
            int(member["content_bytes"]),
            str(member["content_xxh3_64_guard"]),
            str(member["provenance_json"]),
            context.now_ns,
        ),
    )
    _record_embedding_receipt(
        connection,
        generation_id=context.generation_id,
        entity_kind=context.entity_kind.value,
        entity_id=str(row["entity_id"]),
        item_revision_id=item_revision_id,
        chunk_revision_id=chunk_revision_id,
        model_signature=str(member["model_signature"]),
        payload_id=int(member["payload_id"]),
        job_id=None if prior is None else int(prior["job_id"]),
        attempt=None,
        execution_mode=execution_mode,
        now_ns=context.now_ns,
        source_member_id=source_member_id if execution_mode == "replay" else None,
        source_member_binding=(source_member_binding if execution_mode == "replay" else None),
        source_causation_receipt_id=(causation.receipt_id if execution_mode == "replay" else None),
        payload_causation_receipt_id=(
            causation.receipt_id if execution_mode == "cache_hit" else None
        ),
    )
    if prior is not None:
        connection.execute(
            """UPDATE embedding_jobs SET status='done',lease_owner=NULL,
                lease_until_ns=NULL,error_type=NULL,error_message=NULL,
                updated_ns=? WHERE job_id=?""",
            (context.now_ns, int(prior["job_id"])),
        )


def _reuse_generation_member(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    row: sqlite3.Row,
    prior: sqlite3.Row | None,
    member: sqlite3.Row | None,
    item_revisions: dict[str, int],
) -> tuple[bool, bool]:
    if member is None or not _same_fingerprint(member, _fingerprint_from_row(row)):
        return False, False
    if context.entity_kind is SemanticEntityKind.TEXT_CHUNK:
        try:
            _snapshot_chunk_revision(
                connection,
                str(row["entity_id"]),
                _fingerprint_from_row(row),
                context.now_ns,
                require_published=True,
            )
        except StaleEmbeddingJobError:
            # Staging may enqueue before the owner publishes its chunk set.  Keep
            # the job pending; cache reuse can attach only after publication.
            return False, False
    item_revision_id, chunk_revision_id = _snapshot_queue_revisions(
        connection,
        context,
        row,
        item_revisions,
    )
    prior_item_revision_id = int(member["item_revision_id"])
    prior_chunk_revision_id = (
        None if member["chunk_revision_id"] is None else int(member["chunk_revision_id"])
    )
    if item_revision_id == prior_item_revision_id and chunk_revision_id == prior_chunk_revision_id:
        _finish_existing_job(connection, prior, context.now_ns)
        return True, False
    if chunk_revision_id != prior_chunk_revision_id or not _same_item_identity(
        connection,
        prior_item_revision_id,
        item_revision_id,
    ):
        return False, False
    _rebind_generation_member(
        connection,
        context,
        row,
        member,
        item_revision_id,
        chunk_revision_id,
        prior,
    )
    return True, True


def _rebind_candidate_member_ids(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    ordered_rows: tuple[sqlite3.Row, ...],
    members: Mapping[str, sqlite3.Row],
) -> tuple[int, ...]:
    item_revisions: dict[str, int] = {}
    candidates: list[int] = []
    for row in ordered_rows:
        member = members.get(str(row["entity_id"]))
        if member is None or not _same_fingerprint(member, _fingerprint_from_row(row)):
            continue
        if context.entity_kind is SemanticEntityKind.TEXT_CHUNK:
            try:
                _snapshot_chunk_revision(
                    connection,
                    str(row["entity_id"]),
                    _fingerprint_from_row(row),
                    context.now_ns,
                    require_published=True,
                )
            except StaleEmbeddingJobError:
                continue
        item_revision_id, chunk_revision_id = _snapshot_queue_revisions(
            connection,
            context,
            row,
            item_revisions,
        )
        prior_item_revision_id = int(member["item_revision_id"])
        prior_chunk_revision_id = (
            None if member["chunk_revision_id"] is None else int(member["chunk_revision_id"])
        )
        if (
            item_revision_id != prior_item_revision_id
            and chunk_revision_id == prior_chunk_revision_id
            and _same_item_identity(
                connection,
                prior_item_revision_id,
                item_revision_id,
            )
        ):
            candidates.append(int(member["member_id"]))
    return tuple(candidates)


def _rebind_causations(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    member_ids: tuple[int, ...],
    now_ns: int,
) -> dict[int, _RebindCausation]:
    unique_ids = tuple(dict.fromkeys(member_ids))
    member_rows: list[sqlite3.Row] = []
    for offset in range(0, len(unique_ids), 250):
        batch = unique_ids[offset : offset + 250]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        member_rows.extend(
            connection.execute(
                f"""SELECT member.member_id,member.generation_id,member.payload_id,
                    payload.legacy_before_receipts
            FROM embedding_generation_members member
            JOIN vector_payloads payload ON payload.payload_id=member.payload_id
            WHERE member.member_id IN ({placeholders})""",
                batch,
            )
        )
    row_by_member = {int(row["member_id"]): row for row in member_rows}
    missing = set(unique_ids).difference(row_by_member)
    if missing:
        raise SemanticStateError(f"semantic source embedding member disappeared: {min(missing)}")
    same_generation = tuple(
        member_id
        for member_id in unique_ids
        if int(row_by_member[member_id]["generation_id"]) == generation_id
    )
    same_generation_set = frozenset(same_generation)
    cross_generation = tuple(
        member_id for member_id in unique_ids if member_id not in same_generation_set
    )
    attributed: set[int] = set()
    for offset in range(0, len(cross_generation), 250):
        batch = cross_generation[offset : offset + 250]
        materialization_to_member = {
            f"materialization:semantic:embedding-member:{member_id}": member_id
            for member_id in batch
        }
        if not materialization_to_member:
            continue
        placeholders = ",".join("?" for _ in materialization_to_member)
        receipt_rows = connection.execute(
            f"""SELECT DISTINCT
                json_extract(output.value,'$.materialization.materialization_id')
                  AS materialization_id
            FROM semantic_work_receipts receipt,
                 json_each(receipt.receipt_json,'$.outputs') output
            WHERE json_extract(
                output.value,'$.materialization.materialization_id'
            ) IN ({placeholders})""",
            tuple(materialization_to_member),
        ).fetchall()
        attributed.update(
            materialization_to_member[str(row["materialization_id"])] for row in receipt_rows
        )
    unattributed_legacy = tuple(
        member_id
        for member_id in cross_generation
        if bool(row_by_member[member_id]["legacy_before_receipts"]) and member_id not in attributed
    )
    unattributed_legacy_set = frozenset(unattributed_legacy)
    exact_cross_generation = tuple(
        member_id for member_id in cross_generation if member_id not in unattributed_legacy_set
    )
    classified = {
        member_id: _RebindCausation("replay", receipt_id)
        for member_id, receipt_id in _producer_receipts_for_embedding_members(
            connection,
            exact_cross_generation,
        ).items()
    }
    payload_members = (*same_generation, *unattributed_legacy)
    payload_ids = tuple(
        dict.fromkeys(int(row_by_member[member_id]["payload_id"]) for member_id in payload_members)
    )
    payload_receipts = _payload_causation_receipts(
        connection,
        payload_ids,
        now_ns=now_ns,
    )
    classified.update(
        (
            member_id,
            _RebindCausation(
                "cache_hit",
                payload_receipts[int(row_by_member[member_id]["payload_id"])],
            ),
        )
        for member_id in payload_members
    )
    unclassified = set(unique_ids).difference(classified)
    if unclassified:
        raise SemanticStateError(
            f"semantic rebind member was not causally classified: {min(unclassified)}"
        )
    return classified


def _select_queue_rows(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    ordered_rows: tuple[sqlite3.Row, ...],
    existing: Mapping[str, sqlite3.Row],
    members: Mapping[str, sqlite3.Row],
    max_new_jobs: int | None,
) -> _QueueSelection:
    item_revisions: dict[str, int] = {}
    selected_rows: list[sqlite3.Row] = []
    force_pending_ids: list[str] = []
    new_jobs = rebound_members = 0
    complete = True
    for row in ordered_rows:
        entity_id = str(row["entity_id"])
        prior = existing.get(entity_id)
        reused, rebound = _reuse_generation_member(
            connection,
            context,
            row,
            prior,
            members.get(entity_id),
            item_revisions,
        )
        if reused:
            rebound_members += int(rebound)
            continue
        changed = prior is None or not _same_fingerprint(
            prior,
            _fingerprint_from_row(row),
        )
        if changed and max_new_jobs is not None and new_jobs >= max_new_jobs:
            complete = False
            break
        selected_rows.append(row)
        if prior is not None and not changed:
            force_pending_ids.append(entity_id)
        new_jobs += int(changed)
    return _QueueSelection(
        tuple(selected_rows),
        tuple(force_pending_ids),
        new_jobs,
        complete,
        rebound_members,
    )


def _upsert_queue_jobs(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    rows: tuple[sqlite3.Row, ...],
) -> None:
    connection.executemany(
        _UPSERT_EMBEDDING_JOB_SQL,
        (
            (
                context.generation_id,
                context.model_signature,
                context.role.value,
                context.entity_kind.value,
                str(row["entity_id"]),
                str(row["item_id"]),
                (
                    None
                    if row["input_item_revision_id"] is None
                    else int(row["input_item_revision_id"])
                ),
                (
                    None
                    if row["input_chunk_revision_id"] is None
                    else int(row["input_chunk_revision_id"])
                ),
                str(row["content_xxh3_128"]),
                int(row["content_bytes"]),
                str(row["content_xxh3_64_guard"]),
                context.max_attempts,
                context.now_ns,
                context.now_ns,
                context.now_ns,
            )
            for row in rows
        ),
    )


def _reset_queue_jobs_pending(
    connection: sqlite3.Connection,
    context: _QueueJobContext,
    entity_ids: tuple[str, ...],
) -> None:
    if not entity_ids:
        return
    placeholders = ",".join("?" for _ in entity_ids)
    connection.execute(
        f"""UPDATE embedding_jobs SET status='pending',attempts=0,
            available_ns=?,lease_owner=NULL,lease_until_ns=NULL,
            error_type=NULL,error_message=NULL,attempt_started_ns=NULL,
            attempt_sequence=0,updated_ns=?
        WHERE generation_id=? AND entity_kind=?
          AND entity_id IN ({placeholders})""",
        (
            context.now_ns,
            context.now_ns,
            context.generation_id,
            context.entity_kind.value,
            *entity_ids,
        ),
    )


def _queue_job_rows_bounded(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    model: EmbeddingModelSpec,
    entity_kind: SemanticEntityKind,
    role: EmbeddingRole,
    identifiers: tuple[str, ...],
    max_attempts: int,
    now_ns: int,
    max_new_jobs: int | None,
) -> EnqueueJobBatchResult:
    if max_new_jobs is not None and max_new_jobs < 0:
        raise ValueError("max_new_jobs cannot be negative")
    context = _QueueJobContext(
        generation_id,
        model.model_signature,
        entity_kind,
        role,
        max_attempts,
        now_ns,
        {},
    )
    ordered_rows = _queue_entity_rows(connection, entity_kind, identifiers)
    generation = _queue_generation(connection, generation_id, model.model_signature)
    existing = _queue_existing_jobs(connection, context, identifiers)
    members = _queue_generation_members(
        connection,
        context,
        generation,
        identifiers,
    )
    rebind_member_ids = _rebind_candidate_member_ids(
        connection,
        context,
        ordered_rows,
        members,
    )
    context = _QueueJobContext(
        generation_id,
        model.model_signature,
        entity_kind,
        role,
        max_attempts,
        now_ns,
        _rebind_causations(
            connection,
            generation_id=generation_id,
            member_ids=rebind_member_ids,
            now_ns=now_ns,
        ),
    )
    selection = _select_queue_rows(
        connection,
        context,
        ordered_rows,
        existing,
        members,
        max_new_jobs,
    )
    _upsert_queue_jobs(connection, context, selection.rows)
    _reset_queue_jobs_pending(connection, context, selection.force_pending_ids)
    return EnqueueJobBatchResult(
        len(selection.rows),
        selection.new_jobs,
        selection.complete,
        selection.rebound_members,
    )


def _queue_job_rows(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    model: EmbeddingModelSpec,
    entity_kind: SemanticEntityKind,
    role: EmbeddingRole,
    identifiers: tuple[str, ...],
    max_attempts: int,
    now_ns: int,
) -> int:
    return _queue_job_rows_bounded(
        connection,
        generation_id=generation_id,
        model=model,
        entity_kind=entity_kind,
        role=role,
        identifiers=identifiers,
        max_attempts=max_attempts,
        now_ns=now_ns,
        max_new_jobs=None,
    ).touched


def _enqueue_job_batch(
    connection: sqlite3.Connection,
    generation_id: int,
    identifiers: tuple[str, ...],
    *,
    entity_kind: SemanticEntityKind,
    expected_modality: EmbeddingModality,
    role: EmbeddingRole,
    max_attempts: int,
    now_ns: int,
) -> int:
    """Queue one already-bounded entity batch in the caller's transaction."""

    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    if len(identifiers) > MAX_WRITE_BATCH:
        raise ValueError(f"embedding job batch cannot exceed {MAX_WRITE_BATCH} identifiers")
    batch = tuple(dict.fromkeys(identifiers))
    if any(not identifier.strip() for identifier in batch):
        raise ValueError("entity identifiers cannot be blank")
    if not batch:
        return 0
    model = _generation_model(
        connection,
        generation_id,
        require_building=True,
    )
    if model.modality is not expected_modality or role not in model.supported_roles:
        raise ValueError("generation model is incompatible with requested entities")
    return _queue_job_rows(
        connection,
        generation_id=generation_id,
        model=model,
        entity_kind=entity_kind,
        role=role,
        identifiers=batch,
        max_attempts=max_attempts,
        now_ns=now_ns,
    )


def _enqueue_job_batch_bounded(
    connection: sqlite3.Connection,
    generation_id: int,
    identifiers: tuple[str, ...],
    *,
    entity_kind: SemanticEntityKind,
    expected_modality: EmbeddingModality,
    role: EmbeddingRole,
    max_attempts: int,
    now_ns: int,
    max_new_jobs: int | None,
) -> EnqueueJobBatchResult:
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    if len(identifiers) > MAX_WRITE_BATCH:
        raise ValueError(f"embedding job batch cannot exceed {MAX_WRITE_BATCH} identifiers")
    batch = tuple(dict.fromkeys(identifiers))
    if any(not identifier.strip() for identifier in batch):
        raise ValueError("entity identifiers cannot be blank")
    if not batch:
        return EnqueueJobBatchResult(0, 0, True)
    model = _generation_model(
        connection,
        generation_id,
        require_building=True,
    )
    if model.modality is not expected_modality or role not in model.supported_roles:
        raise ValueError("generation model is incompatible with requested entities")
    return _queue_job_rows_bounded(
        connection,
        generation_id=generation_id,
        model=model,
        entity_kind=entity_kind,
        role=role,
        identifiers=batch,
        max_attempts=max_attempts,
        now_ns=now_ns,
        max_new_jobs=max_new_jobs,
    )


def _enqueue_text_chunk_batch(
    connection: sqlite3.Connection,
    generation_id: int,
    chunk_ids: tuple[str, ...],
    *,
    max_attempts: int = 3,
    now_ns: int,
) -> int:
    """Queue one bounded text-chunk batch without opening another connection."""

    return _enqueue_job_batch(
        connection,
        generation_id,
        chunk_ids,
        entity_kind=SemanticEntityKind.TEXT_CHUNK,
        expected_modality=EmbeddingModality.TEXT,
        role=EmbeddingRole.PASSAGE,
        max_attempts=max_attempts,
        now_ns=now_ns,
    )


def _enqueue_text_chunk_batch_bounded(
    connection: sqlite3.Connection,
    generation_id: int,
    chunk_ids: tuple[str, ...],
    *,
    max_new_jobs: int | None,
    max_attempts: int = 3,
    now_ns: int,
) -> EnqueueJobBatchResult:
    return _enqueue_job_batch_bounded(
        connection,
        generation_id,
        chunk_ids,
        entity_kind=SemanticEntityKind.TEXT_CHUNK,
        expected_modality=EmbeddingModality.TEXT,
        role=EmbeddingRole.PASSAGE,
        max_attempts=max_attempts,
        now_ns=now_ns,
        max_new_jobs=max_new_jobs,
    )


def _enqueue_jobs(
    path: Path,
    generation_id: int,
    identifiers: Iterable[str],
    *,
    entity_kind: SemanticEntityKind,
    expected_modality: EmbeddingModality,
    role: EmbeddingRole,
    max_attempts: int,
    batch_size: int,
    now_ns: int | None,
) -> int:
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    _check_batch_size(batch_size)
    selected_ns = _now(now_ns)
    changed = 0
    for raw_batch in _batches(identifiers, batch_size):
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed += _enqueue_job_batch(
                connection,
                generation_id,
                raw_batch,
                entity_kind=entity_kind,
                expected_modality=expected_modality,
                role=role,
                max_attempts=max_attempts,
                now_ns=selected_ns,
            )
    return changed


def _enqueue_jobs_bounded(
    path: Path,
    generation_id: int,
    identifiers: Iterable[str],
    *,
    entity_kind: SemanticEntityKind,
    expected_modality: EmbeddingModality,
    role: EmbeddingRole,
    max_attempts: int,
    batch_size: int,
    now_ns: int | None,
    max_new_jobs: int | None,
) -> EnqueueJobBatchResult:
    if max_new_jobs is not None and max_new_jobs < 0:
        raise ValueError("max_new_jobs cannot be negative")
    if not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    _check_batch_size(batch_size)
    selected_ns = _now(now_ns)
    touched = new_jobs = rebound_members = 0
    for raw_batch in _batches(identifiers, batch_size):
        remaining = None if max_new_jobs is None else max_new_jobs - new_jobs
        with semantic_database(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = _enqueue_job_batch_bounded(
                connection,
                generation_id,
                raw_batch,
                entity_kind=entity_kind,
                expected_modality=expected_modality,
                role=role,
                max_attempts=max_attempts,
                now_ns=selected_ns,
                max_new_jobs=remaining,
            )
        touched += result.touched
        new_jobs += result.new_jobs
        rebound_members += result.rebound_members
        if not result.complete:
            return EnqueueJobBatchResult(
                touched,
                new_jobs,
                False,
                rebound_members,
            )
    return EnqueueJobBatchResult(touched, new_jobs, True, rebound_members)


def enqueue_text_chunk_jobs(
    path: Path,
    generation_id: int,
    chunk_ids: Iterable[str],
    *,
    max_attempts: int = 3,
    batch_size: int = MAX_WRITE_BATCH,
    now_ns: int | None = None,
) -> int:
    return _enqueue_jobs(
        path,
        generation_id,
        chunk_ids,
        entity_kind=SemanticEntityKind.TEXT_CHUNK,
        expected_modality=EmbeddingModality.TEXT,
        role=EmbeddingRole.PASSAGE,
        max_attempts=max_attempts,
        batch_size=batch_size,
        now_ns=now_ns,
    )


def enqueue_text_chunk_jobs_bounded(
    path: Path,
    generation_id: int,
    chunk_ids: Iterable[str],
    *,
    max_new_jobs: int | None,
    max_attempts: int = 3,
    batch_size: int = MAX_WRITE_BATCH,
    now_ns: int | None = None,
) -> EnqueueJobBatchResult:
    return _enqueue_jobs_bounded(
        path,
        generation_id,
        chunk_ids,
        entity_kind=SemanticEntityKind.TEXT_CHUNK,
        expected_modality=EmbeddingModality.TEXT,
        role=EmbeddingRole.PASSAGE,
        max_attempts=max_attempts,
        batch_size=batch_size,
        now_ns=now_ns,
        max_new_jobs=max_new_jobs,
    )


def enqueue_image_item_jobs(
    path: Path,
    generation_id: int,
    item_ids: Iterable[str],
    *,
    max_attempts: int = 3,
    batch_size: int = MAX_WRITE_BATCH,
    now_ns: int | None = None,
) -> int:
    return _enqueue_jobs(
        path,
        generation_id,
        item_ids,
        entity_kind=SemanticEntityKind.IMAGE_ITEM,
        expected_modality=EmbeddingModality.IMAGE,
        role=EmbeddingRole.IMAGE,
        max_attempts=max_attempts,
        batch_size=batch_size,
        now_ns=now_ns,
    )


def enqueue_image_item_jobs_bounded(
    path: Path,
    generation_id: int,
    item_ids: Iterable[str],
    *,
    max_new_jobs: int | None,
    max_attempts: int = 3,
    batch_size: int = MAX_WRITE_BATCH,
    now_ns: int | None = None,
) -> EnqueueJobBatchResult:
    return _enqueue_jobs_bounded(
        path,
        generation_id,
        item_ids,
        entity_kind=SemanticEntityKind.IMAGE_ITEM,
        expected_modality=EmbeddingModality.IMAGE,
        role=EmbeddingRole.IMAGE,
        max_attempts=max_attempts,
        batch_size=batch_size,
        now_ns=now_ns,
        max_new_jobs=max_new_jobs,
    )


def _current_job_matches_source(modality: EmbeddingModality) -> str:
    if modality is EmbeddingModality.TEXT:
        return """EXISTS(
            SELECT 1 FROM text_chunks c
            JOIN semantic_items i ON i.item_id=c.item_id
            WHERE c.chunk_id=embedding_jobs.entity_id AND c.active=1 AND i.active=1
              AND c.content_xxh3_128=embedding_jobs.content_xxh3_128
              AND c.content_bytes=embedding_jobs.content_bytes
              AND c.content_xxh3_64_guard=embedding_jobs.content_xxh3_64_guard)"""
    return """EXISTS(
        SELECT 1 FROM semantic_items i
        WHERE i.item_id=embedding_jobs.entity_id AND i.active=1
          AND i.path IS NOT NULL
          AND i.content_xxh3_128=embedding_jobs.content_xxh3_128
          AND i.content_bytes=embedding_jobs.content_bytes
          AND i.content_xxh3_64_guard=embedding_jobs.content_xxh3_64_guard)"""


def _mark_stale_jobs(
    connection: sqlite3.Connection,
    generation_id: int,
    modality: EmbeddingModality,
    now_ns: int,
) -> None:
    current_match = _current_job_matches_source(modality)
    leased_rows = connection.execute(
        f"""SELECT * FROM embedding_jobs
        WHERE generation_id=? AND status='leased' AND NOT {current_match}
        ORDER BY job_id LIMIT ?""",
        (generation_id, MAX_WRITE_BATCH),
    ).fetchall()
    for row in leased_rows:
        _record_embedding_attempt_failure(
            connection,
            row=row,
            status="failed",
            error_type="source_changed",
            error_message="source changed or became inactive during embedding",
            retryable=False,
            now_ns=now_ns,
        )
    connection.execute(
        f"""UPDATE embedding_jobs SET status='stale',lease_owner=NULL,
            lease_until_ns=NULL,error_type='source_changed',
            error_message='source changed or became inactive before embedding',
            attempt_started_ns=NULL,updated_ns=?
        WHERE generation_id=? AND status='pending' AND NOT {current_match}""",
        (now_ns, generation_id),
    )
    if leased_rows:
        job_ids = tuple(int(row["job_id"]) for row in leased_rows)
        placeholders = ",".join("?" for _ in job_ids)
        connection.execute(
            f"""UPDATE embedding_jobs SET status='stale',lease_owner=NULL,
                lease_until_ns=NULL,error_type='source_changed',
                error_message='source changed or became inactive before embedding',
                attempt_started_ns=NULL,updated_ns=?
            WHERE job_id IN ({placeholders}) AND status='leased'""",
            (now_ns, *job_ids),
        )


def _remove_superseded_completed_jobs(
    connection: sqlite3.Connection,
    generation_id: int,
    modality: EmbeddingModality,
) -> int:
    """Drop completed staging work whose source entity was superseded.

    The immutable payload remains content-addressed.  The obsolete job cannot
    contribute a member to the candidate snapshot and must not keep a resumed
    generation permanently unpublishable.
    """

    current_match = _current_job_matches_source(modality)
    deleted = connection.execute(
        f"""DELETE FROM embedding_jobs
        WHERE generation_id=? AND status='done' AND NOT {current_match}""",
        (generation_id,),
    )
    return max(0, int(deleted.rowcount))


def _attach_payload(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload_id: int,
    provenance_json: str,
    now_ns: int,
    *,
    execution_mode: str,
) -> None:
    generation = connection.execute(
        """SELECT status,model_signature,base_clone_complete
        FROM embedding_generations WHERE generation_id=?""",
        (int(row["generation_id"]),),
    ).fetchone()
    if (
        generation is None
        or str(generation["status"]) != "building"
        or str(generation["model_signature"]) != str(row["model_signature"])
        or not bool(generation["base_clone_complete"])
    ):
        raise SemanticStateError(
            "embedding result cannot attach outside a cloned building generation"
        )
    item_revision_id = (
        int(row["input_item_revision_id"])
        if row["input_item_revision_id"] is not None
        else _snapshot_item_revision(
            connection,
            str(row["item_id"]),
            now_ns,
        )
    )
    if (
        connection.execute(
            """SELECT 1 FROM semantic_item_revisions WHERE item_revision_id=?""",
            (item_revision_id,),
        ).fetchone()
        is None
    ):
        raise StaleEmbeddingJobError("queued semantic item revision disappeared")
    current_chunk_revision_id = (
        _snapshot_chunk_revision(
            connection,
            str(row["entity_id"]),
            _fingerprint_from_row(row),
            now_ns,
            require_published=True,
        )
        if str(row["entity_kind"]) == SemanticEntityKind.TEXT_CHUNK.value
        else None
    )
    if (
        current_chunk_revision_id is not None
        and row["input_chunk_revision_id"] is not None
        and current_chunk_revision_id != int(row["input_chunk_revision_id"])
    ):
        raise StaleEmbeddingJobError("queued semantic chunk revision changed")
    chunk_revision_id = (
        int(row["input_chunk_revision_id"])
        if row["input_chunk_revision_id"] is not None
        else current_chunk_revision_id
    )
    values = (
        str(row["entity_id"]),
        str(row["model_signature"]),
        payload_id,
        int(row["generation_id"]),
        str(row["content_xxh3_128"]),
        int(row["content_bytes"]),
        str(row["content_xxh3_64_guard"]),
        provenance_json,
        now_ns,
    )
    if str(row["entity_kind"]) == SemanticEntityKind.TEXT_CHUNK.value:
        connection.execute(
            """INSERT INTO text_embeddings(
                chunk_id,model_signature,payload_id,generation_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chunk_id,model_signature) DO UPDATE SET
                payload_id=excluded.payload_id,
                generation_id=excluded.generation_id,
                content_xxh3_128=excluded.content_xxh3_128,
                content_bytes=excluded.content_bytes,
                content_xxh3_64_guard=excluded.content_xxh3_64_guard,
                provenance_json=excluded.provenance_json,
                updated_ns=excluded.updated_ns""",
            values,
        )

    else:
        connection.execute(
            """INSERT INTO image_embeddings(
                item_id,model_signature,payload_id,generation_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id,model_signature) DO UPDATE SET
                payload_id=excluded.payload_id,
                generation_id=excluded.generation_id,
                content_xxh3_128=excluded.content_xxh3_128,
                content_bytes=excluded.content_bytes,
                content_xxh3_64_guard=excluded.content_xxh3_64_guard,
                provenance_json=excluded.provenance_json,
                updated_ns=excluded.updated_ns""",
            values,
        )

    connection.execute(
        """INSERT INTO embedding_generation_members(
            generation_id,model_signature,entity_kind,entity_id,item_id,
            item_revision_id,chunk_revision_id,payload_id,content_xxh3_128,
            content_bytes,content_xxh3_64_guard,provenance_json,updated_ns,
            base_member_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
        ON CONFLICT(generation_id,entity_kind,entity_id) DO UPDATE SET
            item_id=excluded.item_id,
            item_revision_id=excluded.item_revision_id,
            chunk_revision_id=excluded.chunk_revision_id,
            payload_id=excluded.payload_id,
            content_xxh3_128=excluded.content_xxh3_128,
            content_bytes=excluded.content_bytes,
            content_xxh3_64_guard=excluded.content_xxh3_64_guard,
            provenance_json=excluded.provenance_json,
            updated_ns=excluded.updated_ns,
            base_member_id=NULL""",
        (
            int(row["generation_id"]),
            str(row["model_signature"]),
            str(row["entity_kind"]),
            str(row["entity_id"]),
            str(row["item_id"]),
            item_revision_id,
            chunk_revision_id,
            payload_id,
            str(row["content_xxh3_128"]),
            int(row["content_bytes"]),
            str(row["content_xxh3_64_guard"]),
            provenance_json,
            now_ns,
        ),
    )
    _record_embedding_receipt(
        connection,
        generation_id=int(row["generation_id"]),
        entity_kind=str(row["entity_kind"]),
        entity_id=str(row["entity_id"]),
        item_revision_id=item_revision_id,
        chunk_revision_id=chunk_revision_id,
        model_signature=str(row["model_signature"]),
        payload_id=payload_id,
        job_id=int(row["job_id"]),
        attempt=max(1, int(row["attempt_sequence"])),
        execution_mode=execution_mode,
        now_ns=now_ns,
    )


def reuse_cached_jobs(
    path: Path,
    generation_id: int,
    *,
    limit: int = MAX_WRITE_BATCH,
    now_ns: int | None = None,
) -> int:
    """Attach identical model/content payloads without invoking a backend."""

    if not 1 <= limit <= MAX_WRITE_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_WRITE_BATCH}")
    selected_ns = _now(now_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        model = _generation_model(connection, generation_id, require_building=True)
        _mark_stale_jobs(connection, generation_id, model.modality, selected_ns)
        rows = connection.execute(
            """SELECT j.*,p.payload_id,p.provenance_json AS payload_provenance_json
            FROM embedding_jobs j JOIN vector_payloads p
              ON p.model_signature=j.model_signature
             AND p.content_xxh3_128=j.content_xxh3_128
             AND p.content_bytes=j.content_bytes
             AND p.content_xxh3_64_guard=j.content_xxh3_64_guard
            WHERE j.generation_id=? AND j.status='pending'
            ORDER BY j.job_id LIMIT ?""",
            (generation_id, limit),
        ).fetchall()
        for row in rows:
            _attach_payload(
                connection,
                row,
                int(row["payload_id"]),
                canonical_json(
                    {
                        "reuse": "exact-xxh3-content",
                        "payload_provenance": json.loads(str(row["payload_provenance_json"])),
                    }
                ),
                selected_ns,
                execution_mode="cache_hit",
            )
            connection.execute(
                "UPDATE embedding_jobs SET status='done',lease_owner=NULL,"
                "lease_until_ns=NULL,error_type=NULL,error_message=NULL,updated_ns=? "
                "WHERE job_id=?",
                (selected_ns, int(row["job_id"])),
            )
        return len(rows)


def _lease_rows(
    connection: sqlite3.Connection,
    generation_id: int,
    modality: EmbeddingModality,
    now_ns: int,
    limit: int,
) -> list[sqlite3.Row]:
    common = """j.generation_id=? AND j.attempts<j.max_attempts
        AND j.status='pending' AND j.available_ns<=?
        AND NOT EXISTS(
            SELECT 1 FROM embedding_jobs active
            WHERE active.generation_id=j.generation_id
              AND active.model_signature=j.model_signature
              AND active.content_xxh3_128=j.content_xxh3_128
              AND active.content_bytes=j.content_bytes
              AND active.content_xxh3_64_guard=j.content_xxh3_64_guard
              AND active.status='leased')
        AND j.job_id=(
            SELECT MIN(candidate.job_id) FROM embedding_jobs candidate
            WHERE candidate.generation_id=j.generation_id
              AND candidate.model_signature=j.model_signature
              AND candidate.content_xxh3_128=j.content_xxh3_128
              AND candidate.content_bytes=j.content_bytes
              AND candidate.content_xxh3_64_guard=j.content_xxh3_64_guard
              AND candidate.status='pending'
              AND candidate.attempts<candidate.max_attempts
              AND candidate.available_ns<=?)"""
    if modality is EmbeddingModality.TEXT:
        # The previous correlated derivation/legacy-members predicates ran
        # once per pending job and became quadratic after a large text
        # generation was staged.  Read a bounded candidate page, resolve the
        # two publication contracts in set-based queries, then lease the first
        # eligible content identities in job order.
        leased_keys = {
            (
                str(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
            )
            for row in connection.execute(
                """SELECT model_signature,content_xxh3_128,content_bytes,
                    content_xxh3_64_guard FROM embedding_jobs
                    WHERE generation_id=? AND status='leased'""",
                (generation_id,),
            )
        }
        selected: list[sqlite3.Row] = []
        selected_keys: set[tuple[str, str, int, str]] = set()
        last_job_id = 0
        page_size = min(MAX_WRITE_BATCH, max(limit * 4, 64))
        while len(selected) < limit:
            rows = connection.execute(
                """SELECT j.*,m.vector_space,c.text_zlib,c.refresh_token,
                    revision.chunk_revision_id
                    FROM embedding_jobs j
                    JOIN embedding_models m ON m.model_signature=j.model_signature
                    JOIN text_chunks c ON c.chunk_id=j.entity_id
                    JOIN semantic_items i ON i.item_id=c.item_id
                    JOIN semantic_chunk_revisions revision ON revision.chunk_id=c.chunk_id
                    WHERE j.generation_id=? AND j.attempts<j.max_attempts
                      AND j.status='pending' AND j.available_ns<=? AND j.job_id>?
                      AND c.active=1 AND i.active=1
                      AND c.content_xxh3_128=j.content_xxh3_128
                      AND c.content_bytes=j.content_bytes
                      AND c.content_xxh3_64_guard=j.content_xxh3_64_guard
                    ORDER BY j.job_id LIMIT ?""",
                (generation_id, now_ns, last_job_id, page_size),
            ).fetchall()
            if not rows:
                break
            last_job_id = int(rows[-1]["job_id"])
            revision_ids = tuple(dict.fromkeys(int(row["chunk_revision_id"]) for row in rows))
            placeholders = ",".join("?" for _ in revision_ids)
            derivation_rows = connection.execute(
                f"""SELECT chunk_revision_id,refresh_token,publication_receipt_id
                    FROM semantic_chunk_derivations
                    WHERE chunk_revision_id IN ({placeholders})""",
                revision_ids,
            ).fetchall()
            derivations: dict[int, list[tuple[str, object]]] = {}
            for derivation in derivation_rows:
                derivations.setdefault(int(derivation[0]), []).append(
                    (str(derivation[1]), derivation[2])
                )
            legacy_rows = connection.execute(
                f"""SELECT member.chunk_revision_id
                    FROM embedding_generation_members member
                    JOIN published_embedding_heads head
                      ON head.generation_id=member.generation_id
                    WHERE member.chunk_revision_id IN ({placeholders})
                      AND member.entity_kind='text_chunk'""",
                revision_ids,
            ).fetchall()
            legacy_revisions = {int(row[0]) for row in legacy_rows}
            for row in rows:
                key = (
                    str(row["model_signature"]),
                    str(row["content_xxh3_128"]),
                    int(row["content_bytes"]),
                    str(row["content_xxh3_64_guard"]),
                )
                if key in leased_keys or key in selected_keys:
                    continue
                revision_id = int(row["chunk_revision_id"])
                current_derivations = derivations.get(revision_id, [])
                eligible = (
                    any(
                        refresh_token == str(row["refresh_token"])
                        and receipt_id is not None
                        for refresh_token, receipt_id in current_derivations
                    )
                    if current_derivations
                    else revision_id in legacy_revisions
                )
                if not eligible:
                    continue
                selected.append(row)
                selected_keys.add(key)
                if len(selected) >= limit:
                    break
        return selected
    return connection.execute(
        f"""SELECT j.*,m.vector_space,i.path,i.source_revision_json
        FROM embedding_jobs j
        JOIN embedding_models m ON m.model_signature=j.model_signature
        JOIN semantic_items i ON i.item_id=j.entity_id
        WHERE {common} AND i.active=1 AND i.path IS NOT NULL
          AND i.content_xxh3_128=j.content_xxh3_128
          AND i.content_bytes=j.content_bytes
          AND i.content_xxh3_64_guard=j.content_xxh3_64_guard
        ORDER BY j.job_id LIMIT ?""",
        (generation_id, now_ns, now_ns, limit),
    ).fetchall()


def _reconcile_expired_embedding_leases(
    connection: sqlite3.Connection,
    generation_id: int,
    now_ns: int,
) -> None:
    rows = connection.execute(
        """SELECT * FROM embedding_jobs
        WHERE generation_id=? AND status='leased' AND lease_until_ns<=?
        ORDER BY job_id LIMIT ?""",
        (generation_id, now_ns, MAX_WRITE_BATCH),
    ).fetchall()
    if not rows:
        return
    retry_ids: list[int] = []
    terminal_ids: list[int] = []
    for row in rows:
        retryable = int(row["attempts"]) < int(row["max_attempts"])
        _record_embedding_attempt_failure(
            connection,
            row=row,
            status="abandoned",
            error_type="lease_expired",
            error_message="embedding worker lease expired before terminal result",
            retryable=retryable,
            now_ns=now_ns,
        )
        target = retry_ids if retryable else terminal_ids
        target.append(int(row["job_id"]))
    if retry_ids:
        placeholders = ",".join("?" for _ in retry_ids)
        connection.execute(
            f"""UPDATE embedding_jobs SET status='pending',available_ns=?,
                lease_owner=NULL,lease_until_ns=NULL,error_type='lease_expired',
                error_message='worker lease expired; retry is available',
                attempt_started_ns=NULL,updated_ns=?
            WHERE job_id IN ({placeholders}) AND status='leased'""",
            (now_ns, now_ns, *retry_ids),
        )
    if terminal_ids:
        placeholders = ",".join("?" for _ in terminal_ids)
        connection.execute(
            f"""UPDATE embedding_jobs SET status='error',lease_owner=NULL,
                lease_until_ns=NULL,error_type='lease_expired',
                error_message='maximum attempts reached after lease expiration',
                attempt_started_ns=NULL,updated_ns=?
            WHERE job_id IN ({placeholders}) AND status='leased'""",
            (now_ns, *terminal_ids),
        )


def claim_embedding_jobs(
    path: Path,
    generation_id: int,
    *,
    worker_id: str,
    limit: int = 32,
    lease_seconds: float = 300.0,
    now_ns: int | None = None,
) -> tuple[EmbeddingJobLease, ...]:
    """Atomically lease bounded jobs, including their text/path payloads."""

    if not worker_id.strip():
        raise ValueError("worker_id cannot be blank")
    if not 1 <= limit <= MAX_WRITE_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_WRITE_BATCH}")
    if not math.isfinite(lease_seconds) or not 1.0 <= lease_seconds <= 86_400.0:
        raise ValueError("lease_seconds must be between 1 and 86400")
    selected_ns = _now(now_ns)
    lease_until_ns = selected_ns + int(lease_seconds * 1_000_000_000)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        model = _generation_model(connection, generation_id, require_building=True)
        _reconcile_expired_embedding_leases(connection, generation_id, selected_ns)
        _mark_stale_jobs(connection, generation_id, model.modality, selected_ns)
        rows = _lease_rows(
            connection,
            generation_id,
            model.modality,
            selected_ns,
            limit,
        )
        if not rows:
            return ()
        job_ids = tuple(int(row["job_id"]) for row in rows)
        placeholders = ",".join("?" for _ in job_ids)
        connection.execute(
            f"""UPDATE embedding_jobs SET status='leased',attempts=attempts+1,
                attempt_sequence=attempt_sequence+1,lease_owner=?,lease_until_ns=?,
                attempt_started_ns=?,updated_ns=?
            WHERE job_id IN ({placeholders})""",
            (worker_id, lease_until_ns, selected_ns, selected_ns, *job_ids),
        )
        leases: list[EmbeddingJobLease] = []
        for row in rows:
            fingerprint = _fingerprint_from_row(row)
            text = (
                _decode_chunk_text(bytes(row["text_zlib"]), fingerprint)
                if model.modality is EmbeddingModality.TEXT
                else None
            )
            image_path = (
                Path(str(row["path"])) if model.modality is EmbeddingModality.IMAGE else None
            )
            source_revision: Mapping[str, object] = {}
            if model.modality is EmbeddingModality.IMAGE:
                raw_revision = json.loads(str(row["source_revision_json"]))
                if not isinstance(raw_revision, dict):
                    raise SemanticStateError("source revision is not a JSON object")
                source_revision = raw_revision
            leases.append(
                EmbeddingJobLease(
                    job_id=int(row["job_id"]),
                    generation_id=generation_id,
                    model_signature=model.model_signature,
                    vector_space=str(row["vector_space"]),
                    modality=model.modality,
                    role=EmbeddingRole(str(row["role"])),
                    entity_kind=SemanticEntityKind(str(row["entity_kind"])),
                    entity_id=str(row["entity_id"]),
                    item_id=str(row["item_id"]),
                    fingerprint=fingerprint,
                    attempt=int(row["attempts"]) + 1,
                    lease_until_ns=lease_until_ns,
                    text=text,
                    image_path=image_path,
                    source_revision=source_revision,
                )
            )
        return tuple(leases)


def embedding_request_from_lease(lease: EmbeddingJobLease) -> EmbeddingRequest:
    """Convert a durable lease into the exact backend request contract."""

    return EmbeddingRequest(
        request_id=str(lease.job_id),
        role=lease.role,
        fingerprint=lease.fingerprint,
        text=lease.text,
        image_path=lease.image_path,
        source_revision=lease.source_revision,
    )


def heartbeat_embedding_jobs(
    path: Path,
    job_ids: Iterable[int],
    *,
    worker_id: str,
    lease_seconds: float = 300.0,
    now_ns: int | None = None,
) -> int:
    """Atomically extend one bounded set of live leases owned by one worker."""

    selected_ids = tuple(job_ids)
    if not selected_ids:
        raise ValueError("heartbeat job_ids cannot be empty")
    if len(selected_ids) > MAX_WRITE_BATCH:
        raise ValueError(f"heartbeat cannot exceed {MAX_WRITE_BATCH} embedding jobs")
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("heartbeat job_ids must be unique")
    if any(
        isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1
        for job_id in selected_ids
    ):
        raise ValueError("heartbeat job_ids must be positive integers")
    if not worker_id.strip():
        raise ValueError("worker_id cannot be blank")
    if not math.isfinite(lease_seconds) or not 1.0 <= lease_seconds <= 86_400.0:
        raise ValueError("lease_seconds must be between 1 and 86400")
    selected_ns = _now(now_ns)
    until = selected_ns + int(lease_seconds * 1_000_000_000)
    placeholders = ",".join("?" for _ in selected_ids)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            f"""UPDATE embedding_jobs
            SET lease_until_ns=MAX(lease_until_ns,?),updated_ns=?
            WHERE job_id IN ({placeholders}) AND status='leased'
              AND lease_owner=? AND lease_until_ns>?""",
            (until, selected_ns, *selected_ids, worker_id, selected_ns),
        )
        if cursor.rowcount != len(selected_ids):
            raise SemanticStateError(
                "one or more job leases are absent, expired or owned elsewhere"
            )
    return until


def heartbeat_embedding_job(
    path: Path,
    job_id: int,
    *,
    worker_id: str,
    lease_seconds: float = 300.0,
    now_ns: int | None = None,
) -> int:
    """Extend a live lease owned by exactly one worker."""

    return heartbeat_embedding_jobs(
        path,
        (job_id,),
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        now_ns=now_ns,
    )


def _job_is_current(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if str(row["entity_kind"]) == SemanticEntityKind.TEXT_CHUNK.value:
        current = connection.execute(
            """SELECT c.content_xxh3_128,c.content_bytes,c.content_xxh3_64_guard
            FROM text_chunks c JOIN semantic_items i ON i.item_id=c.item_id
            WHERE c.chunk_id=? AND c.active=1 AND i.active=1""",
            (str(row["entity_id"]),),
        ).fetchone()
    else:
        current = connection.execute(
            """SELECT content_xxh3_128,content_bytes,content_xxh3_64_guard
            FROM semantic_items WHERE item_id=? AND active=1 AND path IS NOT NULL""",
            (str(row["entity_id"]),),
        ).fetchone()
    return current is not None and _same_fingerprint(current, _fingerprint_from_row(row))


def complete_embedding_job(
    path: Path,
    job_id: int,
    *,
    worker_id: str,
    vector: Sequence[float],
    provenance: Mapping[str, object] | None = None,
    now_ns: int | None = None,
) -> int:
    """Atomically persist/reuse a compact vector and complete its lease."""

    if not worker_id.strip():
        raise ValueError("worker_id cannot be blank")
    selected_ns = _now(now_ns)
    provenance_json = canonical_json(provenance)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT j.* FROM embedding_jobs j WHERE j.job_id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown embedding job {job_id}")
        if (
            str(row["status"]) != "leased"
            or str(row["lease_owner"]) != worker_id
            or row["lease_until_ns"] is None
            or int(row["lease_until_ns"]) <= selected_ns
        ):
            raise SemanticStateError("job lease is absent, expired or owned elsewhere")
        if not _job_is_current(connection, row):
            _record_embedding_attempt_failure(
                connection,
                row=row,
                status="failed",
                error_type="source_changed",
                error_message="source changed before vector completion",
                retryable=False,
                now_ns=selected_ns,
            )
            connection.execute(
                """UPDATE embedding_jobs SET status='stale',lease_owner=NULL,
                lease_until_ns=NULL,error_type='source_changed',
                error_message='source changed before vector completion',
                attempt_started_ns=NULL,updated_ns=?
                WHERE job_id=?""",
                (selected_ns, job_id),
            )
            connection.commit()
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
                selected_ns,
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
                now_ns=selected_ns,
            )
        _attach_payload(
            connection,
            row,
            payload_id,
            provenance_json,
            selected_ns,
            execution_mode=("executed" if inserted_payload.rowcount == 1 else "cache_hit"),
        )
        connection.execute(
            """UPDATE embedding_jobs SET status='done',lease_owner=NULL,
            lease_until_ns=NULL,error_type=NULL,error_message=NULL,updated_ns=?
            WHERE job_id=?""",
            (selected_ns, job_id),
        )
        return payload_id


def fail_embedding_job(
    path: Path,
    job_id: int,
    *,
    worker_id: str,
    error_type: str,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: float = 0.0,
    now_ns: int | None = None,
) -> str:
    """Release a failed lease to a bounded retry or a terminal error."""

    if not worker_id.strip() or not error_type.strip():
        raise ValueError("worker_id and error_type cannot be blank")
    if (
        not math.isfinite(retry_delay_seconds)
        or retry_delay_seconds < 0
        or retry_delay_seconds > 86_400
    ):
        raise ValueError("retry_delay_seconds must be between 0 and 86400")
    selected_ns = _now(now_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
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
            or int(row["lease_until_ns"]) <= selected_ns
        ):
            raise SemanticStateError("job lease is absent, expired or owned elsewhere")
        should_retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
        status = "pending" if should_retry else "error"
        available = selected_ns + int(retry_delay_seconds * 1_000_000_000)
        updated = connection.execute(
            """UPDATE embedding_jobs SET status=?,available_ns=?,lease_owner=NULL,
            lease_until_ns=NULL,error_type=?,error_message=?,updated_ns=?
            WHERE job_id=? AND status='leased' AND lease_owner=?
              AND lease_until_ns>?""",
            (
                status,
                available,
                error_type[:256],
                error_message[:MAX_ERROR_CHARS],
                selected_ns,
                job_id,
                worker_id,
                selected_ns,
            ),
        )
        if updated.rowcount != 1:  # pragma: no cover - protected by the write lock
            raise SemanticStateError("job lease changed before failure was recorded")
        _record_embedding_attempt_failure(
            connection,
            row=row,
            status="failed",
            error_type=error_type[:256],
            error_message=error_message[:MAX_ERROR_CHARS],
            retryable=should_retry,
            now_ns=selected_ns,
        )
        return status


def release_embedding_job_lease_for_deadline(
    path: Path,
    job_id: int,
    *,
    worker_id: str,
    now_ns: int | None = None,
) -> None:
    """Return a timed-out owned lease to pending without spending an attempt."""

    if not worker_id.strip():
        raise ValueError("worker_id cannot be blank")
    selected_ns = _now(now_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM embedding_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown embedding job {job_id}")
        updated = connection.execute(
            """UPDATE embedding_jobs SET status='pending',
                attempts=MAX(0,attempts-1),available_ns=?,lease_owner=NULL,
                lease_until_ns=NULL,error_type=NULL,error_message=NULL,
                attempt_started_ns=NULL,updated_ns=?
            WHERE job_id=? AND status='leased' AND lease_owner=?""",
            (selected_ns, selected_ns, job_id, worker_id),
        )
        if updated.rowcount != 1:
            raise SemanticStateError(
                "timed-out embedding lease changed before it could be released"
            )
        _record_embedding_attempt_failure(
            connection,
            row=row,
            status="cancelled",
            error_type="deadline_cancelled",
            error_message="embedding lease returned before the work deadline",
            retryable=True,
            now_ns=selected_ns,
        )


def update_embedding_generation_cursor(
    path: Path,
    generation_id: int,
    cursor: Mapping[str, object],
) -> None:
    """Persist an explicit source-enumeration checkpoint for resumption."""

    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT cursor_json FROM embedding_generations
            WHERE generation_id=? AND status='building'""",
            (generation_id,),
        ).fetchone()
        if existing is None:
            raise SemanticStateError("generation is absent or no longer building")
        try:
            existing_cursor = json.loads(str(existing["cursor_json"]))
        except (TypeError, ValueError) as exc:
            raise SemanticStateError("generation cursor is not valid JSON") from exc
        if not isinstance(existing_cursor, dict):
            raise SemanticStateError("generation cursor is not a JSON object")
        selected_cursor = dict(cursor)
        if "base_clone" not in selected_cursor and "base_clone" in existing_cursor:
            selected_cursor["base_clone"] = existing_cursor["base_clone"]
        cursor_json = canonical_json(selected_cursor)
        result = connection.execute(
            "UPDATE embedding_generations SET cursor_json=? "
            "WHERE generation_id=? AND status='building'",
            (cursor_json, generation_id),
        )
        if result.rowcount != 1:
            raise SemanticStateError("generation is absent or no longer building")


def _generation_summary_from_row(row: sqlite3.Row) -> GenerationSummary:
    cursor = json.loads(str(row["cursor_json"]))
    if not isinstance(cursor, dict):
        raise SemanticStateError("generation cursor is not a JSON object")
    building = str(row["status"]) == "building"
    return GenerationSummary(
        generation_id=int(row["generation_id"]),
        model_signature=str(row["model_signature"]),
        processing_signature=str(row["processing_signature"]),
        status=str(row["status"]),
        pending=int(row["pending"] if building else row["stored_pending"]),
        leased=int(row["leased"] if building else row["stored_leased"]),
        done=int(row["done"] if building else row["stored_done"]),
        errors=int(row["errors"] if building else row["stored_errors"]),
        stale=int(row["stale"] if building else row["stored_stale"]),
        cursor=cursor,
    )


def _generation_summary_rows(
    connection: sqlite3.Connection,
    generation_ids: Sequence[int],
) -> tuple[GenerationSummary, ...]:
    """Load one bounded ordered summary page with a single aggregate query."""

    if not generation_ids:
        return ()
    ordered_ids = tuple(int(value) for value in generation_ids)
    if len(ordered_ids) > 1_000:
        raise ValueError("generation summary batch cannot exceed 1000 identifiers")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("generation summary identifiers must be unique")
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = connection.execute(
        """SELECT g.generation_id,g.model_signature,g.processing_signature,g.status,
            g.cursor_json,g.pending_count AS stored_pending,
            g.leased_count AS stored_leased,g.done_count AS stored_done,
            g.error_count AS stored_errors,g.stale_count AS stored_stale,
            COALESCE(SUM(j.status='pending'),0) AS pending,
            COALESCE(SUM(j.status='leased'),0) AS leased,
            COALESCE(SUM(j.status='done'),0) AS done,
            COALESCE(SUM(j.status='error'),0) AS errors,
            COALESCE(SUM(j.status='stale'),0) AS stale
        FROM embedding_generations g
        LEFT JOIN embedding_jobs j ON j.generation_id=g.generation_id
        WHERE g.generation_id IN ({placeholders}) GROUP BY g.generation_id""".format(
            placeholders=placeholders
        ),
        ordered_ids,
    ).fetchall()
    summaries = {int(row["generation_id"]): _generation_summary_from_row(row) for row in rows}
    missing = tuple(value for value in ordered_ids if value not in summaries)
    if missing:
        raise KeyError(f"unknown embedding generation {missing[0]}")
    return tuple(summaries[value] for value in ordered_ids)


def _generation_summary_row(
    connection: sqlite3.Connection,
    generation_id: int,
) -> GenerationSummary:
    return _generation_summary_rows(connection, (generation_id,))[0]


def generation_summary(
    path: Path,
    generation_id: int,
    *,
    writer_coordinated: bool = False,
) -> GenerationSummary:
    """Read one generation summary without copying a large active owner.

    Generation workers already own the semantic writer cohort.  Reopening a
    multi-gigabyte owner through a temporary immutable snapshot for every
    progress tick both exhausts the bounded snapshot budget and leaves a
    resumable run unable to make progress.  The worker-only flag reuses the
    owner write connection policy for its bounded read; public callers retain
    the fenced read-only default.
    """

    with semantic_database(path, readonly=not writer_coordinated) as connection:
        return _generation_summary_row(connection, generation_id)


def find_exact_published_generation(
    path: Path,
    *,
    model_signature: str,
    required_provenance: Mapping[str, object] | None = None,
    required_source_head_ledger: Mapping[str, object] | None = None,
    writer_coordinated: bool = False,
) -> GenerationSummary | None:
    """Read an exact head, reusing the owner writer during an active index."""

    if not path.is_file():
        return None
    try:
        with semantic_database(path, readonly=not writer_coordinated) as connection:
            row = connection.execute(
                """SELECT g.generation_id,g.status,g.provenance_json
                FROM published_embedding_heads h
                JOIN embedding_generations g ON g.generation_id=h.generation_id
                WHERE h.model_signature=? AND g.model_signature=?""",
                (model_signature, model_signature),
            ).fetchone()
            if row is None or str(row["status"]) != "ready":
                return None
            provenance = json.loads(str(row["provenance_json"]))
            required = required_provenance or {}
            ledger_required = required_source_head_ledger or {}
            ledger = provenance.get("source_head_ledger") if isinstance(provenance, dict) else None
            if (
                not isinstance(provenance, dict)
                or any(provenance.get(key) != value for key, value in required.items())
                or (
                    ledger_required
                    and (
                        not isinstance(ledger, dict)
                        or any(ledger.get(key) != value for key, value in ledger_required.items())
                    )
                )
            ):
                return None
            summary = _generation_summary_row(connection, int(row["generation_id"]))
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return None
    if (
        summary.status != "ready"
        or summary.unfinished
        or summary.errors
        or summary.stale
        or summary.cursor.get("enumeration_complete") is not True
    ):
        return None
    return summary


def published_source_head_ledger(
    path: Path,
    *,
    model_signature: str,
    writer_coordinated: bool = False,
) -> dict[str, object]:
    """Read the bounded source ledger, optionally through the active writer."""

    if not path.is_file():
        return {}
    try:
        with semantic_database(path, readonly=not writer_coordinated) as connection:
            row = connection.execute(
                """SELECT g.provenance_json FROM published_embedding_heads h
                JOIN embedding_generations g ON g.generation_id=h.generation_id
                WHERE h.model_signature=? AND g.model_signature=? AND g.status='ready'""",
                (model_signature, model_signature),
            ).fetchone()
        if row is None:
            return {}
        provenance = json.loads(str(row["provenance_json"]))
        ledger = provenance.get("source_head_ledger") if isinstance(provenance, dict) else None
        if not isinstance(ledger, dict) or len(ledger) > 64:
            return {}
        return {str(key): value for key, value in ledger.items()}
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return {}


def merge_source_head_ledger(
    ledger: Mapping[str, object],
    *,
    scope_key: str,
    entry: Mapping[str, object],
) -> dict[str, object]:
    """Replace overlapping scope claims while retaining disjoint source heads."""

    channel = entry.get("channel")
    source_kinds = entry.get("source_kinds")
    if not isinstance(channel, str) or not channel.strip() or not isinstance(source_kinds, list):
        raise ValueError("source head ledger entry is invalid")
    selected = {value for value in source_kinds if isinstance(value, str) and value.strip()}
    if len(selected) != len(source_kinds) or not selected or not scope_key.strip():
        raise ValueError("source head ledger scope is invalid")
    merged: dict[str, object] = {}
    for key, value in ledger.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        existing_sources = value.get("source_kinds")
        overlaps = (
            value.get("channel") == channel
            and isinstance(existing_sources, list)
            and bool(
                selected.intersection(item for item in existing_sources if isinstance(item, str))
            )
        )
        if not overlaps:
            merged[key] = dict(value)
    merged[scope_key] = dict(entry)
    if len(merged) > 64:
        raise ValueError("source head ledger exceeds its bound")
    return merged


def _generation_cleanup_profile(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[str | None, tuple[str, ...]]:
    generation = connection.execute(
        "SELECT provenance_json FROM embedding_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise KeyError(f"unknown embedding generation {generation_id}")
    try:
        provenance = json.loads(str(generation["provenance_json"]))
    except (TypeError, ValueError) as exc:
        raise SemanticStateError("embedding generation provenance is invalid") from exc
    if not isinstance(provenance, dict):
        raise SemanticStateError("embedding generation provenance must be an object")

    chunking_signature = provenance.get("chunking_signature")
    selected_sources: tuple[str, ...] = ()
    if chunking_signature is not None:
        if not isinstance(chunking_signature, str) or not chunking_signature.strip():
            raise SemanticStateError("embedding generation chunking signature is invalid")
        raw_sources = provenance.get("sources")
        if raw_sources is not None:
            if not isinstance(raw_sources, list) or any(
                not isinstance(source, str) or not source.strip() for source in raw_sources
            ):
                raise SemanticStateError("embedding generation text sources are invalid")
            selected_sources = tuple(
                dict.fromkeys(
                    "image" if source == "image-ocr" else source for source in raw_sources
                )
            )
        elif provenance.get("source") == "image-ocr":
            selected_sources = ("image",)
        else:
            raise SemanticStateError(
                "embedding generation chunking profile has no selected sources"
            )
    return chunking_signature, selected_sources


def _count_obsolete_generation_members(
    connection: sqlite3.Connection,
    member_generation_id: int,
    *,
    policy_generation_id: int,
) -> int:
    """Count inherited rows that a complete candidate would remove."""

    chunking_signature, selected_sources = _generation_cleanup_profile(
        connection,
        policy_generation_id,
    )
    profile_members = 0
    if selected_sources:
        placeholders = ",".join("?" for _ in selected_sources)
        profile_members = int(
            connection.execute(
                f"""SELECT COUNT(*) FROM embedding_generation_members AS member
                WHERE member.generation_id=? AND member.entity_kind='text_chunk'
                  AND EXISTS(
                    SELECT 1 FROM semantic_item_revisions item_revision
                    JOIN semantic_chunk_revisions chunk_revision
                      ON chunk_revision.chunk_revision_id=member.chunk_revision_id
                     AND chunk_revision.item_id=item_revision.item_id
                    WHERE item_revision.item_revision_id=member.item_revision_id
                      AND item_revision.source_kind IN ({placeholders})
                      AND chunk_revision.chunking_signature<>?)""",
                (
                    member_generation_id,
                    *selected_sources,
                    chunking_signature,
                ),
            ).fetchone()[0]
        )
    text_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_generation_members AS member
            WHERE member.generation_id=? AND member.entity_kind='text_chunk'
              AND NOT EXISTS(
                SELECT 1 FROM text_chunks c
                JOIN semantic_items i ON i.item_id=c.item_id
                WHERE c.chunk_id=member.entity_id AND c.item_id=member.item_id
                  AND c.active=1 AND i.active=1
                  AND c.content_xxh3_128=member.content_xxh3_128
                  AND c.content_bytes=member.content_bytes
                  AND c.content_xxh3_64_guard=member.content_xxh3_64_guard)""",
            (member_generation_id,),
        ).fetchone()[0]
    )
    image_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_generation_members AS member
            WHERE member.generation_id=? AND member.entity_kind='image_item'
              AND NOT EXISTS(
                SELECT 1 FROM semantic_items i
                WHERE i.item_id=member.entity_id AND i.active=1
                  AND i.path IS NOT NULL
                  AND i.content_xxh3_128=member.content_xxh3_128
                  AND i.content_bytes=member.content_bytes
                  AND i.content_xxh3_64_guard=member.content_xxh3_64_guard)""",
            (member_generation_id,),
        ).fetchone()[0]
    )
    return profile_members + text_members + image_members


def _remove_obsolete_candidate_members(
    connection: sqlite3.Connection,
    generation_id: int,
) -> int:
    """Remove inherited/current members that no longer match active source state."""

    chunking_signature, selected_sources = _generation_cleanup_profile(
        connection,
        generation_id,
    )

    profile_members = 0
    if selected_sources:
        placeholders = ",".join("?" for _ in selected_sources)
        profile = connection.execute(
            f"""DELETE FROM embedding_generation_members AS member
            WHERE member.generation_id=? AND member.entity_kind='text_chunk'
              AND EXISTS(
                SELECT 1 FROM semantic_item_revisions item_revision
                JOIN semantic_chunk_revisions chunk_revision
                  ON chunk_revision.chunk_revision_id=member.chunk_revision_id
                 AND chunk_revision.item_id=item_revision.item_id
                WHERE item_revision.item_revision_id=member.item_revision_id
                  AND item_revision.source_kind IN ({placeholders})
                  AND chunk_revision.chunking_signature<>?)""",
            (generation_id, *selected_sources, chunking_signature),
        )
        profile_members = max(0, int(profile.rowcount))

    text = connection.execute(
        """DELETE FROM embedding_generation_members AS member
        WHERE member.generation_id=? AND member.entity_kind='text_chunk'
          AND NOT EXISTS(
            SELECT 1 FROM text_chunks c
            JOIN semantic_items i ON i.item_id=c.item_id
            WHERE c.chunk_id=member.entity_id AND c.item_id=member.item_id
              AND c.active=1 AND i.active=1
              AND c.content_xxh3_128=member.content_xxh3_128
              AND c.content_bytes=member.content_bytes
              AND c.content_xxh3_64_guard=member.content_xxh3_64_guard)""",
        (generation_id,),
    )
    image = connection.execute(
        """DELETE FROM embedding_generation_members AS member
        WHERE member.generation_id=? AND member.entity_kind='image_item'
          AND NOT EXISTS(
            SELECT 1 FROM semantic_items i
            WHERE i.item_id=member.entity_id AND i.active=1 AND i.path IS NOT NULL
              AND i.content_xxh3_128=member.content_xxh3_128
              AND i.content_bytes=member.content_bytes
              AND i.content_xxh3_64_guard=member.content_xxh3_64_guard)""",
        (generation_id,),
    )
    return profile_members + max(0, int(text.rowcount)) + max(0, int(image.rowcount))


@dataclass(frozen=True, slots=True)
class _EmbeddingGenerationPreparation:
    generation_id: int
    model_signature: str
    processing_signature: str
    provenance_json: str
    expected_head: int | None
    base_clone_complete: bool


@dataclass(frozen=True, slots=True)
class _EmbeddingGenerationPreparationDecision:
    conflict: EmbeddingGenerationRebaseRequiredError | None = None
    reused_summary: GenerationSummary | None = None
    materialize_base: bool = False


def _load_embedding_generation_preparation(
    connection: sqlite3.Connection,
    generation_id: int,
) -> _EmbeddingGenerationPreparation:
    model = _generation_model(connection, generation_id, require_building=True)
    generation = connection.execute(
        """SELECT processing_signature,provenance_json,base_generation_id,
            base_clone_complete
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:  # pragma: no cover - protected by model load
        raise KeyError(f"unknown embedding generation {generation_id}")
    expected_head = (
        None if generation["base_generation_id"] is None else int(generation["base_generation_id"])
    )
    return _EmbeddingGenerationPreparation(
        generation_id=generation_id,
        model_signature=model.model_signature,
        processing_signature=str(generation["processing_signature"]),
        provenance_json=str(generation["provenance_json"]),
        expected_head=expected_head,
        base_clone_complete=bool(generation["base_clone_complete"]),
    )


def _deferred_generation_work_counts(
    connection: sqlite3.Connection,
    generation_id: int,
) -> tuple[int, int]:
    job_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
    )
    candidate_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM embedding_generation_members
            WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()[0]
    )
    return job_count, candidate_members


def _embedding_base_has_exact_contract(
    connection: sqlite3.Connection,
    preparation: _EmbeddingGenerationPreparation,
) -> bool:
    base = connection.execute(
        """SELECT status,processing_signature,provenance_json
        FROM embedding_generations WHERE generation_id=?""",
        (preparation.expected_head,),
    ).fetchone()
    if base is None or str(base["status"]) != "ready":
        raise SemanticStateError("published embedding base is absent or not ready")
    return (
        str(base["processing_signature"]) == preparation.processing_signature
        and str(base["provenance_json"]) == preparation.provenance_json
    )


def _exact_replay_candidate_dependents(
    connection: sqlite3.Connection,
    generation_id: int,
) -> int:
    return int(
        connection.execute(
            """SELECT
                (SELECT COUNT(*) FROM text_embeddings WHERE generation_id=?) +
                (SELECT COUNT(*) FROM image_embeddings WHERE generation_id=?) +
                (SELECT COUNT(*) FROM semantic_evidence WHERE generation_id=?) +
                (SELECT COUNT(*) FROM embedding_generations WHERE base_generation_id=?)""",
            (generation_id, generation_id, generation_id, generation_id),
        ).fetchone()[0]
    )


def _elide_exact_embedding_replay(
    connection: sqlite3.Connection,
    preparation: _EmbeddingGenerationPreparation,
) -> GenerationSummary:
    if _exact_replay_candidate_dependents(connection, preparation.generation_id):
        raise SemanticStateError("exact replay candidate has unexpected durable dependents")
    deleted = connection.execute(
        """DELETE FROM embedding_generations
        WHERE generation_id=? AND status='building'""",
        (preparation.generation_id,),
    )
    if deleted.rowcount != 1:
        raise SemanticStateError("exact replay candidate changed before elision")
    if preparation.expected_head is None:  # pragma: no cover - caller invariant
        raise SemanticStateError("exact replay candidate has no published base")
    return _generation_summary_row(connection, preparation.expected_head)


def _prepare_enumerated_embedding_generation(
    connection: sqlite3.Connection,
    preparation: _EmbeddingGenerationPreparation,
    *,
    job_count: int,
    candidate_members: int,
) -> _EmbeddingGenerationPreparationDecision:
    exact_contract = _embedding_base_has_exact_contract(connection, preparation)
    if preparation.expected_head is None:  # pragma: no cover - caller invariant
        raise SemanticStateError("enumerated generation has no published base")
    obsolete_members = _count_obsolete_generation_members(
        connection,
        preparation.expected_head,
        policy_generation_id=preparation.generation_id,
    )
    exact_replay = (
        exact_contract and job_count == 0 and candidate_members == 0 and obsolete_members == 0
    )
    if not exact_replay:
        return _EmbeddingGenerationPreparationDecision(materialize_base=True)
    return _EmbeddingGenerationPreparationDecision(
        reused_summary=_elide_exact_embedding_replay(connection, preparation)
    )


def _prepare_deferred_embedding_generation(
    connection: sqlite3.Connection,
    preparation: _EmbeddingGenerationPreparation,
    *,
    enumeration_complete: bool,
) -> _EmbeddingGenerationPreparationDecision:
    if preparation.expected_head is None:
        connection.execute(
            """UPDATE embedding_generations SET base_clone_complete=1
            WHERE generation_id=? AND status='building'""",
            (preparation.generation_id,),
        )
        return _EmbeddingGenerationPreparationDecision()
    job_count, candidate_members = _deferred_generation_work_counts(
        connection,
        preparation.generation_id,
    )
    if not enumeration_complete:
        return _EmbeddingGenerationPreparationDecision(materialize_base=bool(job_count))
    return _prepare_enumerated_embedding_generation(
        connection,
        preparation,
        job_count=job_count,
        candidate_members=candidate_members,
    )


def _prepare_embedding_generation_transaction(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    enumeration_complete: bool,
) -> _EmbeddingGenerationPreparationDecision:
    preparation = _load_embedding_generation_preparation(connection, generation_id)
    observed_head = _published_head_id(connection, preparation.model_signature)
    if observed_head != preparation.expected_head:
        return _EmbeddingGenerationPreparationDecision(
            conflict=_mark_generation_head_conflict(
                connection,
                generation_id,
                observed_head=observed_head,
                completed_ns=_now(None),
            )
        )
    if preparation.base_clone_complete:
        return _EmbeddingGenerationPreparationDecision()
    return _prepare_deferred_embedding_generation(
        connection,
        preparation,
        enumeration_complete=enumeration_complete,
    )


def prepare_embedding_generation(
    path: Path,
    generation_id: int,
    *,
    enumeration_complete: bool,
    work_budget: SemanticWorkBudget | None = None,
) -> GenerationSummary | None:
    """Materialize a deferred delta, or elide an exact fully enumerated replay.

    ``None`` means the caller still owns a building generation. A returned
    summary is the unchanged published head reused by a proven exact replay.
    """

    if not isinstance(enumeration_complete, bool):
        raise TypeError("enumeration_complete must be a boolean")
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        decision = _prepare_embedding_generation_transaction(
            connection,
            generation_id,
            enumeration_complete=enumeration_complete,
        )
    if decision.conflict is not None:
        raise decision.conflict
    if decision.reused_summary is not None:
        return decision.reused_summary
    if decision.materialize_base:
        _clone_published_members(
            path,
            generation_id,
            work_budget=work_budget,
        )
    return None


@dataclass(frozen=True, slots=True)
class _EmbeddingGenerationFinalization:
    generation_id: int
    model: EmbeddingModelSpec
    summary: GenerationSummary
    expected_head: int | None
    partial: bool
    status: str
    completed_ns: int


_COMPLETE_SOURCE_STATUSES = frozenset({"complete", "done", "no_speech"})


def _source_coverage_failure(provenance: Mapping[str, object]) -> str | None:
    """Return a reason when a source head is not safe to publish as complete.

    Source enumeration can finish successfully even when an upstream route
    only exposed a partial projection.  Keep that distinction at the
    generation boundary: callers may request ``ready_partial`` explicitly,
    but no Video/Image coverage gap can become a normal ``ready`` head.
    Older generations without ``source_heads`` remain compatible and are
    judged by their existing job/error contract.
    """

    raw_heads = provenance.get("source_heads")
    if raw_heads is None:
        return None
    if not isinstance(raw_heads, list):
        return "source_head_ledger_invalid"
    for index, raw_head in enumerate(raw_heads):
        if not isinstance(raw_head, Mapping):
            return f"source_head_{index}_invalid"
        source_kind = str(raw_head.get("source_kind", ""))
        # The multimodal route contract carries explicit coverage semantics.
        # Keep legacy text-owner fixtures/generations compatible: their
        # ``complete`` bit describes enumeration/readability, not the Video or
        # Image projection completeness guarded here.
        if source_kind not in {"video", "image", "image_ocr"}:
            continue
        complete = raw_head.get("complete")
        coverage = raw_head.get("coverage")
        if coverage is None:
            coverage = "complete" if complete is True else "partial"
        if coverage not in {"complete", "partial", "blocked"}:
            return f"source_head_{index}_coverage_invalid"
        # A blocked owner cannot be enumerated in the first place and is
        # surfaced by the route/planner before generation finalization.  The
        # publication guard below is specifically for an enumerated but
        # incomplete multimodal projection, represented by ``partial``.
        if coverage == "blocked":
            continue
        raw_truncated = raw_head.get("truncated", False)
        if not isinstance(raw_truncated, bool):
            return f"source_head_{index}_truncation_invalid"
        raw_status = raw_head.get("source_status")
        if raw_status is not None and (
            not isinstance(raw_status, str) or not raw_status.strip()
        ):
            return f"source_head_{index}_status_invalid"
        status_incomplete = (
            raw_status is not None
            and raw_status.casefold() not in _COMPLETE_SOURCE_STATUSES
        )
        if complete is not True or coverage != "complete" or raw_truncated or status_incomplete:
            return f"source_coverage_incomplete:{source_kind}"
    return None


def _finalization_status(
    summary: GenerationSummary,
    *,
    allow_partial: bool,
    provenance: Mapping[str, object] | None = None,
) -> tuple[bool, str]:
    if "enumeration=bounded-v1" in summary.processing_signature and (
        summary.cursor.get("protocol") != "bounded-v1"
        or summary.cursor.get("enumeration_complete") is not True
    ):
        raise SemanticStateError("bounded generation source enumeration is not complete")
    if summary.unfinished:
        raise SemanticStateError(f"generation still has {summary.unfinished} unfinished jobs")
    source_failure = (
        None if provenance is None else _source_coverage_failure(provenance)
    )
    if (summary.errors or summary.stale or source_failure is not None) and not allow_partial:
        details = f"source coverage {source_failure}" if source_failure else None
        raise SemanticStateError(
            f"generation has {summary.errors} errors and {summary.stale} stale jobs"
            + (f"; {details}" if details else "")
        )
    partial = bool(summary.errors or summary.stale or source_failure is not None)
    return partial, "ready_partial" if partial else "ready"


def _load_generation_finalization(
    connection: sqlite3.Connection,
    generation_id: int,
    *,
    allow_partial: bool,
    completed_ns: int,
) -> _EmbeddingGenerationFinalization:
    model = _generation_model(connection, generation_id, require_building=True)
    _mark_stale_jobs(connection, generation_id, model.modality, completed_ns)
    _remove_superseded_completed_jobs(
        connection,
        generation_id,
        model.modality,
    )
    summary = _generation_summary_row(connection, generation_id)
    generation = connection.execute(
        """SELECT base_generation_id,base_clone_complete,provenance_json
        FROM embedding_generations WHERE generation_id=?""",
        (generation_id,),
    ).fetchone()
    if generation is None:  # pragma: no cover - protected by model load
        raise KeyError(f"unknown embedding generation {generation_id}")
    if not bool(generation["base_clone_complete"]):
        raise SemanticStateError("generation base snapshot is not fully cloned")
    try:
        provenance = json.loads(str(generation["provenance_json"]))
    except (TypeError, ValueError) as exc:
        raise SemanticStateError("embedding generation provenance is invalid") from exc
    if not isinstance(provenance, dict):
        raise SemanticStateError("embedding generation provenance must be an object")
    partial, status = _finalization_status(
        summary,
        allow_partial=allow_partial,
        provenance=provenance,
    )
    expected_head = (
        None if generation["base_generation_id"] is None else int(generation["base_generation_id"])
    )
    return _EmbeddingGenerationFinalization(
        generation_id=generation_id,
        model=model,
        summary=summary,
        expected_head=expected_head,
        partial=partial,
        status=status,
        completed_ns=completed_ns,
    )


def _missing_finalization_member(
    connection: sqlite3.Connection,
    generation_id: int,
) -> int | None:
    row = connection.execute(
        """SELECT j.job_id FROM embedding_jobs j
        LEFT JOIN embedding_generation_members member
          ON member.generation_id=j.generation_id
         AND member.entity_kind=j.entity_kind
         AND member.entity_id=j.entity_id
        WHERE j.generation_id=? AND j.status='done'
          AND member.member_id IS NULL LIMIT 1""",
        (generation_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def _unpublished_text_finalization_member(
    connection: sqlite3.Connection,
    generation_id: int,
) -> int | None:
    row = connection.execute(
        """SELECT member.member_id
        FROM embedding_generation_members member
        JOIN semantic_chunk_revisions revision
          ON revision.chunk_revision_id=member.chunk_revision_id
        LEFT JOIN text_chunks chunk ON chunk.chunk_id=revision.chunk_id
        WHERE member.generation_id=? AND member.entity_kind='text_chunk'
          AND NOT (
            chunk.active=1 AND (
              EXISTS(
                SELECT 1 FROM semantic_chunk_derivations derivation
                WHERE derivation.chunk_revision_id=revision.chunk_revision_id
                  AND derivation.refresh_token=chunk.refresh_token
                  AND derivation.publication_receipt_id IS NOT NULL)
              OR (
                NOT EXISTS(
                  SELECT 1 FROM semantic_chunk_derivations derivation
                  WHERE derivation.chunk_revision_id=revision.chunk_revision_id)
                AND EXISTS(
                  SELECT 1 FROM embedding_generation_members legacy_member
                  JOIN published_embedding_heads head
                    ON head.generation_id=legacy_member.generation_id
                  WHERE legacy_member.chunk_revision_id=revision.chunk_revision_id
                    AND legacy_member.entity_kind='text_chunk'))))
        ORDER BY member.member_id LIMIT 1""",
        (generation_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def _reconcile_generation_finalization(
    connection: sqlite3.Connection,
    finalization: _EmbeddingGenerationFinalization,
) -> EmbeddingGenerationRebaseRequiredError | None:
    if finalization.partial:
        return None
    _remove_obsolete_candidate_members(connection, finalization.generation_id)
    missing_member = _missing_finalization_member(
        connection,
        finalization.generation_id,
    )
    if missing_member is not None:
        raise SemanticStateError(f"completed job {missing_member} has no candidate member")
    unpublished_member = _unpublished_text_finalization_member(
        connection,
        finalization.generation_id,
    )
    if unpublished_member is not None:
        raise SemanticStateError(
            f"candidate member {unpublished_member} depends on an unpublished text chunk"
        )
    current_head = _published_head_id(
        connection,
        finalization.model.model_signature,
    )
    if current_head == finalization.expected_head:
        return None
    return _mark_generation_head_conflict(
        connection,
        finalization.generation_id,
        observed_head=current_head,
        completed_ns=finalization.completed_ns,
        summary=finalization.summary,
    )


def _persist_generation_finalization(
    connection: sqlite3.Connection,
    finalization: _EmbeddingGenerationFinalization,
) -> None:
    summary = finalization.summary
    updated = connection.execute(
        """UPDATE embedding_generations SET status=?,completed_ns=?,
            pending_count=?,leased_count=?,done_count=?,error_count=?,stale_count=?
        WHERE generation_id=? AND status='building'""",
        (
            finalization.status,
            finalization.completed_ns,
            summary.pending,
            summary.leased,
            summary.done,
            summary.errors,
            summary.stale,
            finalization.generation_id,
        ),
    )
    if updated.rowcount != 1:
        raise SemanticStateError("generation changed before finalization")


def _publish_generation_finalization(
    connection: sqlite3.Connection,
    finalization: _EmbeddingGenerationFinalization,
) -> None:
    if finalization.partial:
        return
    if finalization.expected_head is None:
        connection.execute(
            """INSERT INTO published_embedding_heads(
                model_signature,generation_id,published_ns)
            VALUES(?,?,?)""",
            (
                finalization.model.model_signature,
                finalization.generation_id,
                finalization.completed_ns,
            ),
        )
        _record_generation_publication_receipt(
            connection,
            generation_id=finalization.generation_id,
            published_ns=finalization.completed_ns,
        )
        return
    published = connection.execute(
        """UPDATE published_embedding_heads
        SET generation_id=?,published_ns=?
        WHERE model_signature=? AND generation_id=?""",
        (
            finalization.generation_id,
            finalization.completed_ns,
            finalization.model.model_signature,
            finalization.expected_head,
        ),
    )
    if published.rowcount != 1:  # protected by BEGIN IMMEDIATE + CAS
        raise SemanticStateError("published embedding head changed during finalization")
    _record_generation_publication_receipt(
        connection,
        generation_id=finalization.generation_id,
        published_ns=finalization.completed_ns,
    )


def _finalized_generation_summary(
    finalization: _EmbeddingGenerationFinalization,
) -> GenerationSummary:
    summary = finalization.summary
    return GenerationSummary(
        generation_id=summary.generation_id,
        model_signature=summary.model_signature,
        processing_signature=summary.processing_signature,
        status=finalization.status,
        pending=summary.pending,
        leased=summary.leased,
        done=summary.done,
        errors=summary.errors,
        stale=summary.stale,
        cursor=summary.cursor,
    )


def finalize_embedding_generation(
    path: Path,
    generation_id: int,
    *,
    allow_partial: bool = False,
    completed_ns: int | None = None,
) -> GenerationSummary:
    """Finalize work and atomically publish only a complete CAS-safe snapshot."""

    selected_ns = _now(completed_ns)
    with semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        finalization = _load_generation_finalization(
            connection,
            generation_id,
            allow_partial=allow_partial,
            completed_ns=selected_ns,
        )
        conflict = _reconcile_generation_finalization(connection, finalization)
        if conflict is None:
            _persist_generation_finalization(connection, finalization)
            _publish_generation_finalization(connection, finalization)
            outcome: GenerationSummary | EmbeddingGenerationRebaseRequiredError = (
                _finalized_generation_summary(finalization)
            )
        else:
            outcome = conflict
    if isinstance(outcome, EmbeddingGenerationRebaseRequiredError):
        raise outcome
    return outcome


# endregion [05]
