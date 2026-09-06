"""Owner-local persistence for bounded, append-only ReviewTask facts.

The Framework owner must already be at schema 22.  This module never creates or
migrates state.  Page publication and cursor advancement share one Framework
transaction; human/system transitions are append-only compare-and-swap events.
"""

from __future__ import annotations
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from neocortex.workflow.review import review_task_contracts as _contracts
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.framework_schema import validate_framework_schema_v22
from neocortex.workflow.review.review_task_contracts import (
    MAX_REVIEW_TASK_READ_PAGE,
    MAX_REVIEW_TASKS_PER_PAGE,
    REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
    REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION,
    REVIEW_TASK_PRIORITY_ALGORITHM,
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskCoverage,
    ReviewTaskDraft,
    ReviewTaskEvent,
    ReviewTaskEventResult,
    ReviewTaskInput,
    ReviewTaskListCursor,
    ReviewTaskPublication,
    ReviewTaskPublicationResult,
    ReviewTaskRecord,
    ReviewTaskRecordPage,
    ReviewTaskScanProgress,
    ReviewTaskSourceFence,
    ReviewTaskSourcePublication,
    ReviewTaskState,
    ReviewTaskTransition,
    ReviewTaskVersionHead,
)
from neocortex.persistence.sqlite_cancellation import (
    CancellationCheck,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)


_FaultInjector = Callable[[str], None]


class ReviewTaskRepositoryError(RuntimeError):
    """Framework review facts are missing, contradictory, or non-canonical."""


class ReviewTaskCASConflict(ReviewTaskRepositoryError):
    """A durable task/progress head does not match the caller's expected head."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("review task JSON value is invalid") from exc


def _load_json(value: str, *, label: str) -> object:
    def reject_constant(raw: str) -> object:
        raise ValueError(f"non-finite JSON constant: {raw}")

    try:
        payload = json.loads(value, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewTaskRepositoryError(f"{label} is invalid JSON") from exc
    if _canonical_json(payload) != value:
        raise ReviewTaskRepositoryError(f"{label} is not canonical JSON")
    return payload


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ReviewTaskRepositoryError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _require_sequence(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReviewTaskRepositoryError(f"{label} must be a JSON array")
    return value


def _require_review_task_schema(connection: sqlite3.Connection) -> None:
    try:
        version_rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise ReviewTaskRepositoryError("Framework review schema cannot be inspected") from exc
    if len(version_rows) != 1 or str(version_rows[0][0]) != str(
        REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION
    ):
        observed = None if not version_rows else str(version_rows[0][0])
        raise ReviewTaskRepositoryError(
            "ReviewTask requires exact Framework schema "
            f"{REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION}; observed {observed!r}"
        )
    try:
        validate_framework_schema_v22(connection)
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        raise ReviewTaskRepositoryError(
            "Framework schema 22 does not match the exact ReviewTask contract"
        ) from exc


def _digest_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _progress_id(fence: ReviewTaskSourceFence) -> str:
    return _digest_id(
        "review-task-progress-v1",
        fence.scope,
        fence.task_type,
        fence.selector_signature,
        fence.source_snapshot_fingerprint,
    )


def _source_publication_id(batch_id: str) -> str:
    return _digest_id("review-task-source-publication-v1", batch_id)


def _source_publication_key(batch_id: str) -> str:
    return _digest_id("review-task-source-publication-key-v1", batch_id)


def _batch_membership_id(batch_id: str, task_id: str) -> str:
    return _digest_id("review-task-batch-membership-v1", batch_id, task_id)


def _checkpoint(bridge: SQLiteCancellationBridge) -> None:
    bridge.checkpoint()


def _fault(fault_injector: _FaultInjector | None, stage: str) -> None:
    if fault_injector is not None:
        fault_injector(stage)


def _event_from_row(row: sqlite3.Row, *, prefix: str = "") -> ReviewTaskEvent:
    def column(name: str) -> object:
        return row[prefix + name]

    def integer(name: str) -> int:
        value = column(name)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ReviewTaskRepositoryError(f"persisted review task event {name} is not an integer")
        try:
            return int(value)
        except ValueError as exc:
            raise ReviewTaskRepositoryError(
                f"persisted review task event {name} is not an integer"
            ) from exc

    provenance = CanonicalJsonObject(str(column("provenance_json")))
    raw_decision = column("decision_json")
    decision = None if raw_decision is None else CanonicalJsonObject(str(raw_decision))
    try:
        return ReviewTaskEvent(
            event_id=str(column("event_id")),
            event_key=str(column("event_key")),
            task_id=str(column("task_id")),
            sequence=integer("sequence"),
            previous_event_id=(
                None if column("previous_event_id") is None else str(column("previous_event_id"))
            ),
            from_state=(
                None if column("from_state") is None else ReviewTaskState(str(column("from_state")))
            ),
            to_state=ReviewTaskState(str(column("to_state"))),
            actor_kind=ReviewTaskActorKind(str(column("actor_kind"))),
            actor_id=str(column("actor_id")),
            provenance=provenance,
            decision=decision,
            note=None if column("note") is None else str(column("note")),
            observed_ns=integer("observed_ns"),
            recorded_ns=integer("recorded_ns"),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("persisted review task event is invalid") from exc


def _evidence_from_json(payload_json: str) -> tuple[object, ...]:
    if len(payload_json.encode("utf-8")) > _contracts.MAX_REVIEW_TASK_JSON_BYTES:
        raise ReviewTaskRepositoryError("review task evidence exceeds its byte limit")
    raw = _require_sequence(
        _load_json(payload_json, label="review task evidence"), label="evidence"
    )
    try:
        return tuple(_contracts.evidence_ref_from_payload(item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("persisted ReviewTask evidence is invalid") from exc


def _suggestions_from_json(payload_json: str) -> tuple[str, ...]:
    if len(payload_json.encode("utf-8")) > _contracts.MAX_REVIEW_TASK_JSON_BYTES:
        raise ReviewTaskRepositoryError("review task suggestions exceed its byte limit")
    raw = _require_sequence(
        _load_json(payload_json, label="review task suggestions"),
        label="suggestions",
    )
    if any(not isinstance(item, str) for item in raw):
        raise ReviewTaskRepositoryError("persisted ReviewTask suggestions are invalid")
    return tuple(cast(str, item) for item in raw)


def _task_record_from_row(row: sqlite3.Row) -> ReviewTaskRecord:
    try:
        source = ReviewTaskInput.from_json(str(row["source_ref_json"]))
        draft = ReviewTaskDraft(
            task_id=str(row["task_id"]),
            logical_key=str(row["logical_key"]),
            task_version=int(row["task_version"]),
            task_type=str(row["task_type"]),
            scope=str(row["scope"]),
            source_kind=str(row["source_kind"]),
            source_input_id=str(row["source_input_id"]),
            snapshot=CanonicalJsonObject(str(row["snapshot_json"])),
            evidence=cast(tuple, _evidence_from_json(str(row["evidence_json"]))),
            reason_code=str(row["reason_code"]),
            uncertainty_detail=CanonicalJsonObject(str(row["uncertainty_json"])),
            impact=float(row["impact"]),
            uncertainty=float(row["uncertainty"]),
            irreversibility=float(row["irreversibility"]),
            suggestions=_suggestions_from_json(str(row["suggestions_json"])),
            supersedes_task_id=(
                None if row["supersedes_task_id"] is None else str(row["supersedes_task_id"])
            ),
            created_ns=int(row["created_ns"]),
        )
        priority = float(row["priority"])
        if (
            str(row["priority_algorithm"]) != REVIEW_TASK_PRIORITY_ALGORITHM
            or abs(priority - draft.priority) > 1e-12
        ):
            raise ReviewTaskRepositoryError("persisted ReviewTask priority is invalid")
        try:
            batch_snapshot = CanonicalJsonObject(str(row["batch_source_snapshot_json"]))
            batch_fence = ReviewTaskSourceFence(
                scope=str(row["scope"]),
                task_type=str(row["task_type"]),
                selector_signature=str(row["batch_selector_signature"]),
                source_snapshot=batch_snapshot,
                source_snapshot_fingerprint=str(row["batch_source_snapshot_fingerprint"]),
            )
        except ValueError as exc:
            raise ReviewTaskRepositoryError(
                "ReviewTask batch source fingerprint is invalid"
            ) from exc
        if batch_fence.source_snapshot_fingerprint != str(row["source_snapshot_fingerprint"]):
            raise ReviewTaskRepositoryError("ReviewTask/batch source fence disagrees")
        if row["current_event_id"] is None or row["current_to_state"] is None:
            raise ReviewTaskRepositoryError("ReviewTask lacks a current event")
        current_event = _event_from_row(row, prefix="current_")
        return ReviewTaskRecord(
            task=draft,
            source=source,
            source_snapshot_fingerprint=str(row["source_snapshot_fingerprint"]),
            batch_id=str(row["batch_id"]),
            selector_signature=batch_fence.selector_signature,
            current_event=current_event,
        )
    except ReviewTaskRepositoryError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("persisted ReviewTask is invalid") from exc


def _draft_from_payload(value: object) -> ReviewTaskDraft:
    payload = _require_mapping(value, label="ReviewTask receipt task")
    raw_evidence = _require_sequence(payload.get("evidence"), label="ReviewTask receipt evidence")
    raw_suggestions = _require_sequence(
        payload.get("suggestions"), label="ReviewTask receipt suggestions"
    )
    if any(not isinstance(item, str) for item in raw_suggestions):
        raise ReviewTaskRepositoryError("ReviewTask receipt suggestions are invalid")
    try:
        draft = ReviewTaskDraft(
            task_id=cast(str, payload.get("task_id")),
            logical_key=cast(str, payload.get("logical_key")),
            task_version=cast(int, payload.get("task_version")),
            task_type=cast(str, payload.get("task_type")),
            scope=cast(str, payload.get("scope")),
            source_kind=cast(str, payload.get("source_kind")),
            source_input_id=cast(str, payload.get("source_input_id")),
            snapshot=CanonicalJsonObject.from_mapping(
                _require_mapping(payload.get("snapshot"), label="ReviewTask snapshot")
            ),
            evidence=tuple(_contracts.evidence_ref_from_payload(item) for item in raw_evidence),
            reason_code=cast(str, payload.get("reason_code")),
            uncertainty_detail=CanonicalJsonObject.from_mapping(
                _require_mapping(payload.get("uncertainty_detail"), label="ReviewTask uncertainty")
            ),
            impact=cast(float, payload.get("impact")),
            uncertainty=cast(float, payload.get("uncertainty")),
            irreversibility=cast(float, payload.get("irreversibility")),
            suggestions=tuple(cast(str, item) for item in raw_suggestions),
            supersedes_task_id=cast(str | None, payload.get("supersedes_task_id")),
            created_ns=cast(int, payload.get("created_ns")),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("ReviewTask receipt task is invalid") from exc
    if draft.to_dict() != payload:
        raise ReviewTaskRepositoryError("ReviewTask receipt task is not canonical")
    return draft


_BATCH_COLUMNS = """batch_id,batch_key,scope,task_type,selector_signature,
source_snapshot_fingerprint,source_snapshot_json,cursor_before_json,
cursor_after_json,previous_batch_id,scan_revision,page_size,scanned_count,
selected_count,cumulative_scanned_count,cumulative_selected_count,coverage,
evidence_complete,evidence_reason,cumulative_evidence_complete,
cumulative_evidence_reason,producer_signature,receipt_json,confirmed_ns,
receipt_schema_version"""
_SOURCE_PUBLICATION_COLUMNS = """publication_id,publication_key,scope,task_type,
selector_signature,source_snapshot_fingerprint,source_snapshot_json,batch_id,
previous_publication_id,revision,confirmed_ns,receipt_json,receipt_schema_version"""
MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS = 1_024
MAX_REVIEW_TASK_SOURCE_CHAIN_BATCHES = 10_000
MAX_REVIEW_TASK_SOURCE_CHAIN_MEMBERSHIPS = 1_000_000
MAX_REVIEW_TASK_SOURCE_AUDIT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReviewTaskSourcePublicationAudit:
    publications: tuple[ReviewTaskSourcePublication, ...]
    batch_count: int
    membership_count: int
    progress_count: int
    estimated_bytes: int


def _publication_from_batch_row(row: sqlite3.Row) -> ReviewTaskPublication:
    receipt_json = str(row["receipt_json"])
    if len(receipt_json.encode("utf-8")) > _contracts.MAX_REVIEW_TASK_PUBLICATION_BYTES:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt exceeds its byte limit")
    payload = _require_mapping(
        _load_json(receipt_json, label="ReviewTask batch receipt"),
        label="ReviewTask batch receipt",
    )
    inputs = _require_sequence(payload.get("inputs"), label="ReviewTask receipt inputs")
    tasks = _require_sequence(payload.get("tasks"), label="ReviewTask receipt tasks")
    if (
        int(row["receipt_schema_version"]) != REVIEW_TASK_CONTRACT_SCHEMA_VERSION
        or len(inputs) != int(row["scanned_count"])
        or len(tasks) != int(row["selected_count"])
        or int(row["page_size"]) != len(inputs)
        or len(inputs) > _contracts.MAX_REVIEW_TASK_INPUTS_PER_PAGE
        or len(tasks) > _contracts.MAX_REVIEW_TASKS_PER_PAGE
    ):
        raise ReviewTaskRepositoryError("ReviewTask batch receipt count is inconsistent")
    scan_revision = int(row["scan_revision"])
    previous_batch_id = row["previous_batch_id"]
    if (
        scan_revision < 1
        or (previous_batch_id is None) != (scan_revision == 1)
        or int(row["cumulative_scanned_count"]) < int(row["scanned_count"])
        or int(row["cumulative_selected_count"]) < int(row["selected_count"])
        or int(row["cumulative_selected_count"]) > int(row["cumulative_scanned_count"])
        or bool(int(row["cumulative_evidence_complete"]))
        != (row["cumulative_evidence_reason"] is None)
    ):
        raise ReviewTaskRepositoryError("ReviewTask batch chain receipt is invalid")
    try:
        publication = ReviewTaskPublication(
            batch_id=str(row["batch_id"]),
            batch_key=str(row["batch_key"]),
            fence=ReviewTaskSourceFence(
                scope=str(row["scope"]),
                task_type=str(row["task_type"]),
                selector_signature=str(row["selector_signature"]),
                source_snapshot=CanonicalJsonObject(str(row["source_snapshot_json"])),
                source_snapshot_fingerprint=str(row["source_snapshot_fingerprint"]),
            ),
            cursor_before=(
                None
                if row["cursor_before_json"] is None
                else CanonicalJsonObject(str(row["cursor_before_json"]))
            ),
            cursor_after=(
                None
                if row["cursor_after_json"] is None
                else CanonicalJsonObject(str(row["cursor_after_json"]))
            ),
            inputs=tuple(ReviewTaskInput.from_json(_canonical_json(item)) for item in inputs),
            tasks=tuple(_draft_from_payload(item) for item in tasks),
            coverage=ReviewTaskCoverage(str(row["coverage"])),
            producer_signature=str(row["producer_signature"]),
            confirmed_ns=int(row["confirmed_ns"]),
            evidence_complete=bool(int(row["evidence_complete"])),
            evidence_reason=(
                None if row["evidence_reason"] is None else str(row["evidence_reason"])
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt bindings are invalid") from exc
    if publication.to_json() != receipt_json:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt is not canonical")
    return publication


def _validate_batch_chain_pair(
    row: sqlite3.Row,
    previous: sqlite3.Row | None,
) -> None:
    publication = _publication_from_batch_row(row)
    revision = int(row["scan_revision"])
    cumulative = (
        int(row["cumulative_scanned_count"]),
        int(row["cumulative_selected_count"]),
        bool(int(row["cumulative_evidence_complete"])),
        row["cumulative_evidence_reason"],
    )
    if revision == 1:
        expected = (
            publication.scanned_count,
            publication.selected_count,
            publication.evidence_complete,
            publication.evidence_reason,
        )
        if row["previous_batch_id"] is not None or publication.cursor_before is not None:
            raise ReviewTaskRepositoryError("initial ReviewTask batch chain is invalid")
    else:
        if previous is None:
            raise ReviewTaskRepositoryError("ReviewTask batch predecessor is missing")
        previous_publication = _publication_from_batch_row(previous)
        previous_evidence_complete = bool(int(previous["cumulative_evidence_complete"]))
        expected = (
            int(previous["cumulative_scanned_count"]) + publication.scanned_count,
            int(previous["cumulative_selected_count"]) + publication.selected_count,
            previous_evidence_complete and publication.evidence_complete,
            (
                previous["cumulative_evidence_reason"]
                if not previous_evidence_complete
                else publication.evidence_reason
            ),
        )
        if (
            int(previous["scan_revision"]) != revision - 1
            or previous_publication.fence != publication.fence
            or previous_publication.coverage is not ReviewTaskCoverage.PARTIAL
            or previous_publication.cursor_after != publication.cursor_before
            or previous_publication.confirmed_ns >= publication.confirmed_ns
        ):
            raise ReviewTaskRepositoryError("ReviewTask batch predecessor link is invalid")
    if cumulative != expected:
        raise ReviewTaskRepositoryError("ReviewTask batch cumulative receipt is invalid")


def _validate_batch_chain_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    """Validate one immutable chain link without walking the source history."""

    previous = None
    if int(row["scan_revision"]) > 1:
        previous = connection.execute(
            f"SELECT {_BATCH_COLUMNS} FROM review_task_batches WHERE batch_id=?",
            (row["previous_batch_id"],),
        ).fetchone()
    _validate_batch_chain_pair(row, previous)


def _source_publication_from_row(row: sqlite3.Row) -> ReviewTaskSourcePublication:
    receipt_json = str(row["receipt_json"])
    if len(receipt_json.encode("utf-8")) > _contracts.MAX_REVIEW_TASK_JSON_BYTES:
        raise ReviewTaskRepositoryError("ReviewTask source publication receipt is too large")
    try:
        publication = ReviewTaskSourcePublication(
            publication_id=str(row["publication_id"]),
            publication_key=str(row["publication_key"]),
            fence=ReviewTaskSourceFence(
                scope=str(row["scope"]),
                task_type=str(row["task_type"]),
                selector_signature=str(row["selector_signature"]),
                source_snapshot=CanonicalJsonObject(str(row["source_snapshot_json"])),
                source_snapshot_fingerprint=str(row["source_snapshot_fingerprint"]),
            ),
            batch_id=str(row["batch_id"]),
            previous_publication_id=(
                None
                if row["previous_publication_id"] is None
                else str(row["previous_publication_id"])
            ),
            revision=int(row["revision"]),
            confirmed_ns=int(row["confirmed_ns"]),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError(
            "persisted ReviewTask source publication is invalid"
        ) from exc
    if (
        int(row["receipt_schema_version"]) != REVIEW_TASK_CONTRACT_SCHEMA_VERSION
        or publication.publication_id != _source_publication_id(publication.batch_id)
        or publication.publication_key != _source_publication_key(publication.batch_id)
        or publication.to_json() != receipt_json
    ):
        raise ReviewTaskRepositoryError(
            "persisted ReviewTask source publication receipt is invalid"
        )
    return publication


def _validate_latest_review_task_source_publication_final_receipts(
    connection: sqlite3.Connection,
    *,
    limit: int = MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
) -> tuple[ReviewTaskSourcePublication, ...]:
    """Validate and return current source heads with bounded owner-local receipts."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_024:
        raise ValueError("ReviewTask source publication limit must be between 1 and 1024")
    rows = connection.execute(
        f"SELECT {_SOURCE_PUBLICATION_COLUMNS} "
        "FROM review_task_source_publications head WHERE NOT EXISTS("
        "SELECT 1 FROM review_task_source_publications later "
        "WHERE later.scope=head.scope AND later.task_type=head.task_type "
        "AND later.selector_signature=head.selector_signature "
        "AND later.revision>head.revision) "
        "ORDER BY scope,task_type,selector_signature LIMIT ?",
        (limit + 1,),
    ).fetchall()
    if len(rows) > limit:
        raise ReviewTaskRepositoryError(
            f"ReviewTask source publications exceed the bounded limit {limit}"
        )
    publications = tuple(_source_publication_from_row(row) for row in rows)
    if not publications:
        return ()

    previous_publication_ids = tuple(
        publication.previous_publication_id
        for publication in publications
        if publication.previous_publication_id is not None
    )
    previous_publications: dict[str, ReviewTaskSourcePublication] = {}
    if previous_publication_ids:
        placeholders = ",".join("?" for _ in previous_publication_ids)
        previous_rows = connection.execute(
            f"SELECT {_SOURCE_PUBLICATION_COLUMNS} "
            f"FROM review_task_source_publications WHERE publication_id IN ({placeholders})",
            previous_publication_ids,
        ).fetchall()
        previous_publications = {
            item.publication_id: item
            for item in (_source_publication_from_row(row) for row in previous_rows)
        }

    batch_ids = tuple(publication.batch_id for publication in publications)
    placeholders = ",".join("?" for _ in batch_ids)
    batch_rows = connection.execute(
        f"SELECT {_BATCH_COLUMNS} FROM review_task_batches WHERE batch_id IN ({placeholders})",
        batch_ids,
    ).fetchall()
    batch_by_id = {str(row["batch_id"]): row for row in batch_rows}
    previous_batch_ids = tuple(
        str(row["previous_batch_id"]) for row in batch_rows if row["previous_batch_id"] is not None
    )
    previous_batch_by_id: dict[str, sqlite3.Row] = {}
    if previous_batch_ids:
        previous_placeholders = ",".join("?" for _ in previous_batch_ids)
        previous_batch_rows = connection.execute(
            f"SELECT {_BATCH_COLUMNS} FROM review_task_batches "
            f"WHERE batch_id IN ({previous_placeholders})",
            previous_batch_ids,
        ).fetchall()
        previous_batch_by_id = {str(row["batch_id"]): row for row in previous_batch_rows}

    progress_rows = connection.execute(
        """SELECT progress_id,scope,task_type,selector_signature,
        source_snapshot_fingerprint,source_snapshot_json,cursor_json,last_batch_id,
        scanned_count,selected_count,complete,evidence_complete,evidence_reason,
        revision,created_ns,updated_ns FROM review_task_scan_progress
        WHERE last_batch_id IN ("""
        + placeholders
        + ")",
        batch_ids,
    ).fetchall()
    progress_by_batch: dict[str, ReviewTaskScanProgress] = {}
    for row in progress_rows:
        progress = _progress_from_row(row)
        if progress.last_batch_id in progress_by_batch:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication has multiple progress receipts"
            )
        progress_by_batch[progress.last_batch_id] = progress

    membership_rows = connection.execute(
        """SELECT membership.membership_id,membership.batch_id,
        membership.task_id,membership.source_input_id,membership.recorded_ns,
        membership.membership_schema_version,
        task.batch_id AS linked_task_batch_id,
        task.source_input_id AS linked_task_source_input_id
        FROM review_task_batch_memberships membership
        LEFT JOIN review_tasks task ON task.task_id=membership.task_id
        WHERE membership.batch_id IN ("""
        + placeholders
        + ") ORDER BY membership.batch_id,membership.task_id",
        batch_ids,
    ).fetchall()
    memberships_by_batch: dict[str, list[tuple[object, ...]]] = {
        batch_id: [] for batch_id in batch_ids
    }
    for row in membership_rows:
        memberships_by_batch.setdefault(str(row["batch_id"]), []).append(tuple(row))

    for publication in publications:
        previous_publication = (
            None
            if publication.previous_publication_id is None
            else previous_publications.get(publication.previous_publication_id)
        )
        if publication.revision == 1:
            if previous_publication is not None:
                raise ReviewTaskRepositoryError(
                    "initial ReviewTask source publication has a predecessor"
                )
        elif (
            previous_publication is None
            or previous_publication.fence.scope != publication.fence.scope
            or previous_publication.fence.task_type != publication.fence.task_type
            or previous_publication.fence.selector_signature != publication.fence.selector_signature
            or previous_publication.revision != publication.revision - 1
            or previous_publication.confirmed_ns >= publication.confirmed_ns
        ):
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication predecessor receipt is invalid"
            )

        batch_row = batch_by_id.get(publication.batch_id)
        current_progress = progress_by_batch.get(publication.batch_id)
        if batch_row is None or current_progress is None:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication lacks its final owner-local receipt"
            )
        batch = _publication_from_batch_row(batch_row)
        previous_batch_id = batch_row["previous_batch_id"]
        previous_batch = (
            None if previous_batch_id is None else previous_batch_by_id.get(str(previous_batch_id))
        )
        _validate_batch_chain_pair(batch_row, previous_batch)
        if (
            batch.batch_id != publication.batch_id
            or batch.fence != publication.fence
            or batch.coverage is not ReviewTaskCoverage.COMPLETE
            or not batch.evidence_complete
            or batch.confirmed_ns != publication.confirmed_ns
            or current_progress.fence != publication.fence
            or current_progress.last_batch_id != publication.batch_id
            or not current_progress.complete
            or not current_progress.evidence_complete
            or current_progress.revision != int(batch_row["scan_revision"])
            or current_progress.scanned_count != int(batch_row["cumulative_scanned_count"])
            or current_progress.selected_count != int(batch_row["cumulative_selected_count"])
            or current_progress.evidence_reason != batch_row["cumulative_evidence_reason"]
            or (None if current_progress.cursor is None else current_progress.cursor.payload_json)
            != (None if batch.cursor_after is None else batch.cursor_after.payload_json)
        ):
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication disagrees with final scan progress"
            )
        expected_memberships = [
            (
                _batch_membership_id(batch.batch_id, task.task_id),
                batch.batch_id,
                task.task_id,
                task.source_input_id,
                batch.confirmed_ns,
                REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
                batch.batch_id,
                task.source_input_id,
            )
            for task in sorted(batch.tasks, key=lambda item: item.task_id)
        ]
        if memberships_by_batch.get(batch.batch_id, []) != expected_memberships:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication membership receipts disagree"
            )
    return publications


def _row_text_bytes(row: sqlite3.Row) -> int:
    return sum(
        len(value.encode("utf-8")) if isinstance(value, str) else len(value)
        for value in row
        if isinstance(value, (str, bytes))
    )


def _bounded_audit_bytes(current: int, row: sqlite3.Row) -> int:
    total = current + _row_text_bytes(row)
    if total > MAX_REVIEW_TASK_SOURCE_AUDIT_BYTES:
        raise ReviewTaskRepositoryError(
            "ReviewTask source publication audit exceeds its byte limit"
        )
    return total


_LATEST_SOURCE_PUBLICATIONS_CTE = """WITH latest AS (
    SELECT head.* FROM review_task_source_publications head
    WHERE NOT EXISTS(
        SELECT 1 FROM review_task_source_publications later
        WHERE later.scope=head.scope AND later.task_type=head.task_type
          AND later.selector_signature=head.selector_signature
          AND later.revision>head.revision
    )
)"""

_PUBLISHED_BATCH_CHAIN_CTE = (
    _LATEST_SOURCE_PUBLICATIONS_CTE
    + """,
chain(batch_id) AS (
    SELECT batch_id FROM latest
    UNION
    SELECT batch.previous_batch_id
    FROM chain
    JOIN review_task_batches batch ON batch.batch_id=chain.batch_id
    WHERE batch.previous_batch_id IS NOT NULL
)"""
)


def _audit_latest_review_task_source_publications_from_prevalidated_connection(
    connection: sqlite3.Connection,
    *,
    limit: int = MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
) -> ReviewTaskSourcePublicationAudit:
    """Audit heads after the caller validated an exact compatible owner schema."""

    publications = _validate_latest_review_task_source_publication_final_receipts(
        connection,
        limit=limit,
    )
    if not publications:
        return ReviewTaskSourcePublicationAudit((), 0, 0, 0, 0)

    estimated_bytes = 0
    source_rows = connection.execute(
        _LATEST_SOURCE_PUBLICATIONS_CTE + f" SELECT {_SOURCE_PUBLICATION_COLUMNS} FROM latest "
        "ORDER BY scope,task_type,selector_signature LIMIT ?",
        (limit + 1,),
    ).fetchall()
    if len(source_rows) != len(publications):
        raise ReviewTaskRepositoryError("ReviewTask source publication audit head set changed")
    for row in source_rows:
        estimated_bytes = _bounded_audit_bytes(estimated_bytes, row)

    batch_rows: dict[str, sqlite3.Row] = {}
    batch_publications: dict[str, ReviewTaskPublication] = {}
    batch_cursor = connection.execute(
        _PUBLISHED_BATCH_CHAIN_CTE + f" SELECT batch.{_BATCH_COLUMNS.replace(',', ',batch.')} "
        "FROM chain JOIN review_task_batches batch ON batch.batch_id=chain.batch_id "
        "LIMIT ?",
        (MAX_REVIEW_TASK_SOURCE_CHAIN_BATCHES + 1,),
    )
    for row in batch_cursor:
        if len(batch_rows) >= MAX_REVIEW_TASK_SOURCE_CHAIN_BATCHES:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication batch chain exceeds its row limit"
            )
        estimated_bytes = _bounded_audit_bytes(estimated_bytes, row)
        batch = _publication_from_batch_row(row)
        if batch.batch_id in batch_rows:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication batch chain is ambiguous"
            )
        batch_rows[batch.batch_id] = row
        batch_publications[batch.batch_id] = batch
    for row in batch_rows.values():
        previous_batch_id = row["previous_batch_id"]
        previous = None if previous_batch_id is None else batch_rows.get(str(previous_batch_id))
        _validate_batch_chain_pair(row, previous)
    if any(publication.batch_id not in batch_rows for publication in publications):
        raise ReviewTaskRepositoryError("ReviewTask source publication batch chain is incomplete")

    progress_rows = connection.execute(
        _LATEST_SOURCE_PUBLICATIONS_CTE
        + """ SELECT progress.progress_id,progress.scope,progress.task_type,
        progress.selector_signature,progress.source_snapshot_fingerprint,
        progress.source_snapshot_json,progress.cursor_json,
        progress.last_batch_id,progress.evidence_reason
        FROM latest head JOIN review_task_scan_progress progress
          ON progress.last_batch_id=head.batch_id
        ORDER BY progress.scope,progress.task_type,progress.selector_signature
        LIMIT ?""",
        (limit + 1,),
    ).fetchall()
    if len(progress_rows) != len(publications):
        raise ReviewTaskRepositoryError(
            "ReviewTask source publication progress receipt set is incomplete"
        )
    for row in progress_rows:
        estimated_bytes = _bounded_audit_bytes(estimated_bytes, row)

    task_maps = {
        batch_id: {task.task_id: task for task in batch.tasks}
        for batch_id, batch in batch_publications.items()
    }
    membership_counts: dict[str, int] = dict.fromkeys(batch_rows, 0)
    membership_count = 0
    membership_cursor = connection.execute(
        _PUBLISHED_BATCH_CHAIN_CTE
        + """ SELECT membership.membership_id,membership.batch_id,
        membership.task_id,membership.source_input_id,membership.recorded_ns,
        membership.membership_schema_version,
        task.batch_id AS linked_task_batch_id,
        task.source_input_id AS linked_task_source_input_id
        FROM chain
        CROSS JOIN review_task_batch_memberships membership
          ON membership.batch_id=chain.batch_id
        LEFT JOIN review_tasks task ON task.task_id=membership.task_id
        LIMIT ?""",
        (MAX_REVIEW_TASK_SOURCE_CHAIN_MEMBERSHIPS + 1,),
    )
    for row in membership_cursor:
        if membership_count >= MAX_REVIEW_TASK_SOURCE_CHAIN_MEMBERSHIPS:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication memberships exceed their row limit"
            )
        estimated_bytes = _bounded_audit_bytes(estimated_bytes, row)
        batch_id = str(row["batch_id"])
        task_id = str(row["task_id"])
        membership_batch = batch_publications.get(batch_id)
        task = task_maps.get(batch_id, {}).get(task_id)
        if membership_batch is None or task is None:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication membership lacks its batch task"
            )
        expected = (
            _batch_membership_id(batch_id, task_id),
            batch_id,
            task_id,
            task.source_input_id,
            membership_batch.confirmed_ns,
            REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            batch_id,
            task.source_input_id,
        )
        if tuple(row) != expected:
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication membership receipt disagrees"
            )
        membership_counts[batch_id] += 1
        membership_count += 1
    if any(
        membership_counts[batch_id] != len(batch.tasks)
        for batch_id, batch in batch_publications.items()
    ):
        raise ReviewTaskRepositoryError(
            "ReviewTask source publication membership set is incomplete"
        )
    return ReviewTaskSourcePublicationAudit(
        publications,
        len(batch_rows),
        membership_count,
        len(progress_rows),
        estimated_bytes,
    )


def audit_latest_review_task_source_publications_from_connection(
    connection: sqlite3.Connection,
    *,
    limit: int = MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
) -> ReviewTaskSourcePublicationAudit:
    """Audit source heads after requiring the exact current Framework schema."""

    _require_review_task_schema(connection)
    return _audit_latest_review_task_source_publications_from_prevalidated_connection(
        connection,
        limit=limit,
    )


def validate_latest_review_task_source_publications_from_connection(
    connection: sqlite3.Connection,
    *,
    limit: int = MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS,
) -> tuple[ReviewTaskSourcePublication, ...]:
    """Validate and return current source heads with complete bounded receipts."""

    return audit_latest_review_task_source_publications_from_connection(
        connection,
        limit=limit,
    ).publications


def _validate_records_against_batch_receipts(
    connection: sqlite3.Connection,
    records: tuple[ReviewTaskRecord, ...],
) -> None:
    records_by_batch: dict[str, list[ReviewTaskRecord]] = {}
    for record in records:
        records_by_batch.setdefault(record.batch_id, []).append(record)
    batch_ids = tuple(records_by_batch)
    if not batch_ids:
        return
    placeholders = ",".join("?" for _ in batch_ids)
    seen: set[str] = set()
    cursor = connection.execute(
        f"SELECT {_BATCH_COLUMNS} FROM review_task_batches "
        f"WHERE batch_id IN ({placeholders}) ORDER BY batch_id",
        batch_ids,
    )
    for row in cursor:
        publication = _publication_from_batch_row(row)
        seen.add(publication.batch_id)
        task_by_id = {task.task_id: task for task in publication.tasks}
        input_by_id = {item.input_id: item for item in publication.inputs}
        for record in records_by_batch[publication.batch_id]:
            expected_task = task_by_id.get(record.task.task_id)
            expected_source = input_by_id.get(record.task.source_input_id)
            if expected_task != record.task or expected_source != record.source:
                raise ReviewTaskRepositoryError(
                    "ReviewTask row is not backed by its exact batch receipt"
                )
    if seen != set(batch_ids):
        raise ReviewTaskRepositoryError("ReviewTask row lacks its owner-local batch receipt")


def _validate_record_event_chains(
    connection: sqlite3.Connection,
    records: tuple[ReviewTaskRecord, ...],
) -> None:
    if not records:
        return
    record_by_id = {record.task.task_id: record for record in records}
    task_ids = tuple(record_by_id)
    placeholders = ",".join("?" for _ in task_ids)
    rows = connection.execute(
        "SELECT " + _EVENT_COLUMNS + f" FROM review_task_events WHERE task_id IN ({placeholders}) "
        "ORDER BY task_id,sequence LIMIT ?",
        (*task_ids, len(task_ids) * 4 + 1),
    ).fetchall()
    if len(rows) > len(task_ids) * 4:
        raise ReviewTaskRepositoryError("ReviewTask event history exceeds its state bound")
    events_by_task: dict[str, list[ReviewTaskEvent]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        event = _event_from_row(row)
        events_by_task.setdefault(event.task_id, []).append(event)
    allowed = {
        ReviewTaskState.OPEN: frozenset(
            {
                ReviewTaskState.IN_REVIEW,
                ReviewTaskState.RESOLVED,
                ReviewTaskState.DISMISSED,
                ReviewTaskState.SUPERSEDED,
            }
        ),
        ReviewTaskState.IN_REVIEW: frozenset(
            {
                ReviewTaskState.RESOLVED,
                ReviewTaskState.DISMISSED,
                ReviewTaskState.SUPERSEDED,
            }
        ),
        ReviewTaskState.RESOLVED: frozenset({ReviewTaskState.SUPERSEDED}),
        ReviewTaskState.DISMISSED: frozenset({ReviewTaskState.SUPERSEDED}),
    }
    for task_id, record in record_by_id.items():
        events = events_by_task.get(task_id, [])
        if not events:
            raise ReviewTaskRepositoryError(f"ReviewTask lacks a current event: {task_id}")
        previous: ReviewTaskEvent | None = None
        for event in events:
            if event.observed_ns < record.task.created_ns:
                raise ReviewTaskRepositoryError("ReviewTask event predates its task")
            if previous is None:
                if (
                    event.sequence != 1
                    or event.previous_event_id is not None
                    or event.from_state is not None
                    or event.to_state is not ReviewTaskState.OPEN
                ):
                    raise ReviewTaskRepositoryError("ReviewTask event chain is invalid")
            elif (
                event.sequence != previous.sequence + 1
                or event.previous_event_id != previous.event_id
                or event.from_state is not previous.to_state
                or event.to_state not in allowed.get(previous.to_state, frozenset())
                or event.recorded_ns <= previous.recorded_ns
                or event.observed_ns < previous.observed_ns
            ):
                raise ReviewTaskRepositoryError("ReviewTask event chain is invalid")
            previous = event
        if previous != record.current_event:
            raise ReviewTaskRepositoryError("ReviewTask current event disagrees with its history")


def _validated_task_records(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
) -> tuple[ReviewTaskRecord, ...]:
    records = tuple(_task_record_from_row(row) for row in rows)
    _validate_records_against_batch_receipts(connection, records)
    _validate_record_event_chains(connection, records)
    return _apply_effective_source_publications(connection, rows, records)


def _progress_from_row(row: sqlite3.Row) -> ReviewTaskScanProgress:
    try:
        source_snapshot = CanonicalJsonObject(str(row["source_snapshot_json"]))
        fence = ReviewTaskSourceFence(
            scope=str(row["scope"]),
            task_type=str(row["task_type"]),
            selector_signature=str(row["selector_signature"]),
            source_snapshot=source_snapshot,
            source_snapshot_fingerprint=str(row["source_snapshot_fingerprint"]),
        )
        raw_cursor = row["cursor_json"]
        complete_value = int(row["complete"])
        if complete_value not in (0, 1):
            raise ValueError("invalid progress complete flag")
        progress = ReviewTaskScanProgress(
            progress_id=str(row["progress_id"]),
            fence=fence,
            cursor=None if raw_cursor is None else CanonicalJsonObject(str(raw_cursor)),
            last_batch_id=str(row["last_batch_id"]),
            scanned_count=int(row["scanned_count"]),
            selected_count=int(row["selected_count"]),
            complete=bool(complete_value),
            revision=int(row["revision"]),
            created_ns=int(row["created_ns"]),
            updated_ns=int(row["updated_ns"]),
            evidence_complete=bool(int(row["evidence_complete"])),
            evidence_reason=(
                None if row["evidence_reason"] is None else str(row["evidence_reason"])
            ),
        )
        if progress.progress_id != _progress_id(fence):
            raise ValueError("progress identifier does not match its source fence")
        return progress
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("persisted ReviewTask progress is invalid") from exc


def _validate_progress_batch_receipt(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    progress: ReviewTaskScanProgress,
) -> None:
    receipt_json = str(row["batch_receipt_json"])
    if len(receipt_json.encode("utf-8")) > _contracts.MAX_REVIEW_TASK_PUBLICATION_BYTES:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt exceeds its byte limit")
    payload = _require_mapping(
        _load_json(receipt_json, label="ReviewTask batch receipt"),
        label="ReviewTask batch receipt",
    )
    inputs = _require_sequence(payload.get("inputs"), label="ReviewTask receipt inputs")
    tasks = _require_sequence(payload.get("tasks"), label="ReviewTask receipt tasks")
    if (
        len(inputs) != int(row["batch_scanned_count"])
        or len(tasks) != int(row["batch_selected_count"])
        or len(inputs) > _contracts.MAX_REVIEW_TASK_INPUTS_PER_PAGE
        or len(tasks) > _contracts.MAX_REVIEW_TASKS_PER_PAGE
    ):
        raise ReviewTaskRepositoryError("ReviewTask batch receipt count is inconsistent")
    try:
        typed_inputs = tuple(ReviewTaskInput.from_json(_canonical_json(item)) for item in inputs)
        typed_tasks = tuple(_draft_from_payload(item) for item in tasks)
    except (TypeError, ValueError) as exc:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt bindings are invalid") from exc
    expected_scalars: dict[str, object] = {
        "schema_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        "kind": "review_task_publication",
        "batch_id": str(row["last_batch_id"]),
        "batch_key": str(row["batch_key"]),
        "scope": progress.fence.scope,
        "task_type": progress.fence.task_type,
        "selector_signature": progress.fence.selector_signature,
        "source_snapshot_fingerprint": progress.fence.source_snapshot_fingerprint,
        "page_size": int(row["batch_page_size"]),
        "scanned_count": int(row["batch_scanned_count"]),
        "selected_count": int(row["batch_selected_count"]),
        "coverage": str(row["batch_coverage"]),
        "evidence_complete": bool(int(row["batch_evidence_complete"])),
        "evidence_reason": (
            None if row["batch_evidence_reason"] is None else str(row["batch_evidence_reason"])
        ),
        "producer_signature": str(row["batch_producer_signature"]),
        "confirmed_ns": int(row["batch_confirmed_ns"]),
    }
    for key, expected in expected_scalars.items():
        if payload.get(key) != expected:
            raise ReviewTaskRepositoryError(f"ReviewTask batch receipt disagrees with {key}")
    if payload.get("source_snapshot") != progress.fence.source_snapshot.to_dict():
        raise ReviewTaskRepositoryError("ReviewTask batch receipt source snapshot disagrees")
    for key, column in (
        ("cursor_before", "batch_cursor_before_json"),
        ("cursor_after", "batch_cursor_after_json"),
    ):
        raw_cursor = row[column]
        expected_cursor = None
        if raw_cursor is not None:
            expected_cursor = CanonicalJsonObject(str(raw_cursor)).to_dict()
        if payload.get(key) != expected_cursor:
            raise ReviewTaskRepositoryError(f"ReviewTask batch receipt {key} disagrees")
    batch_snapshot = CanonicalJsonObject(str(row["batch_source_snapshot_json"]))
    batch_fence = ReviewTaskSourceFence(
        scope=progress.fence.scope,
        task_type=progress.fence.task_type,
        selector_signature=progress.fence.selector_signature,
        source_snapshot=batch_snapshot,
        source_snapshot_fingerprint=str(row["batch_source_snapshot_fingerprint"]),
    )
    if batch_fence != progress.fence:
        raise ReviewTaskRepositoryError("ReviewTask progress/batch source fence disagrees")
    batch_cursor_after = row["batch_cursor_after_json"]
    progress_cursor = None if progress.cursor is None else progress.cursor.payload_json
    if batch_cursor_after != progress_cursor:
        raise ReviewTaskRepositoryError("ReviewTask progress/batch cursor disagrees")
    if (str(row["batch_coverage"]) == ReviewTaskCoverage.COMPLETE.value) != progress.complete:
        raise ReviewTaskRepositoryError("ReviewTask progress/batch coverage disagrees")
    publication = ReviewTaskPublication(
        batch_id=str(row["last_batch_id"]),
        batch_key=str(row["batch_key"]),
        fence=progress.fence,
        cursor_before=(
            None
            if row["batch_cursor_before_json"] is None
            else CanonicalJsonObject(str(row["batch_cursor_before_json"]))
        ),
        cursor_after=(
            None
            if row["batch_cursor_after_json"] is None
            else CanonicalJsonObject(str(row["batch_cursor_after_json"]))
        ),
        inputs=typed_inputs,
        tasks=typed_tasks,
        coverage=ReviewTaskCoverage(str(row["batch_coverage"])),
        producer_signature=str(row["batch_producer_signature"]),
        confirmed_ns=int(row["batch_confirmed_ns"]),
        evidence_complete=bool(int(row["batch_evidence_complete"])),
        evidence_reason=(
            None if row["batch_evidence_reason"] is None else str(row["batch_evidence_reason"])
        ),
    )
    if publication.to_json() != receipt_json:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt is not canonical")
    persisted_rows = connection.execute(
        """WITH current_events AS (
            SELECT e.* FROM review_task_events e
            WHERE NOT EXISTS(
                SELECT 1 FROM review_task_events later
                WHERE later.task_id=e.task_id AND later.sequence>e.sequence
            )
        )
        SELECT t.*,b.selector_signature AS batch_selector_signature,
        b.source_snapshot_json AS batch_source_snapshot_json,
        b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,"""
        + _CURRENT_EVENT_COLUMNS
        + " FROM review_tasks t JOIN review_task_batches b ON b.batch_id=t.batch_id "
        "JOIN current_events e ON e.task_id=t.task_id WHERE t.batch_id=?",
        (publication.batch_id,),
    ).fetchall()
    records = tuple(_task_record_from_row(item) for item in persisted_rows)
    record_by_id = {record.task.task_id: record for record in records}
    if len(record_by_id) != len(records) or set(record_by_id) != {
        task.task_id for task in typed_tasks
    }:
        raise ReviewTaskRepositoryError("ReviewTask batch receipt/task facts disagree")
    memberships = connection.execute(
        """SELECT membership_id,batch_id,task_id,source_input_id,recorded_ns,
        membership_schema_version FROM review_task_batch_memberships
        WHERE batch_id=? ORDER BY task_id""",
        (publication.batch_id,),
    ).fetchall()
    expected_memberships = tuple(
        (
            _batch_membership_id(publication.batch_id, task.task_id),
            publication.batch_id,
            task.task_id,
            task.source_input_id,
            publication.confirmed_ns,
            REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        )
        for task in sorted(typed_tasks, key=lambda item: item.task_id)
    )
    if tuple(tuple(row) for row in memberships) != expected_memberships:
        raise ReviewTaskRepositoryError("ReviewTask batch membership receipts disagree")
    input_by_id = {item.input_id: item for item in typed_inputs}
    for task in typed_tasks:
        record = record_by_id[task.task_id]
        expected_source = input_by_id.get(task.source_input_id)
        if record.task != task or expected_source is None or record.source != expected_source:
            raise ReviewTaskRepositoryError("ReviewTask batch receipt/task payload disagrees")
    _validate_record_event_chains(connection, records)


def _validate_progress_cumulative_counts(
    connection: sqlite3.Connection,
    progress: ReviewTaskScanProgress,
) -> None:
    """Validate one immutable accumulator head and its bounded final page."""

    row = connection.execute(
        f"SELECT {_BATCH_COLUMNS},"
        "(SELECT COUNT(*) FROM review_task_batch_memberships membership "
        " WHERE membership.batch_id=review_task_batches.batch_id) "
        "AS membership_count FROM review_task_batches WHERE batch_id=?",
        (progress.last_batch_id,),
    ).fetchone()
    if row is None:
        raise ReviewTaskRepositoryError("ReviewTask cumulative receipt head is unavailable")
    _validate_batch_chain_row(connection, row)
    observed = (
        int(row["scan_revision"]),
        int(row["cumulative_scanned_count"]),
        int(row["cumulative_selected_count"]),
        bool(int(row["cumulative_evidence_complete"])),
        row["cumulative_evidence_reason"],
        int(row["membership_count"]),
    )
    expected = (
        progress.revision,
        progress.scanned_count,
        progress.selected_count,
        progress.evidence_complete,
        progress.evidence_reason,
        int(row["selected_count"]),
    )
    if observed != expected:
        raise ReviewTaskRepositoryError(
            "ReviewTask cumulative receipt disagrees with durable progress"
        )


_EVENT_COLUMNS = """event_id,event_key,task_id,sequence,previous_event_id,
from_state,to_state,actor_kind,actor_id,provenance_json,decision_json,note,
observed_ns,recorded_ns"""
_CURRENT_EVENT_COLUMNS = """e.event_id AS current_event_id,
e.event_key AS current_event_key,e.task_id AS current_task_id,
e.sequence AS current_sequence,e.previous_event_id AS current_previous_event_id,
e.from_state AS current_from_state,e.to_state AS current_to_state,
e.actor_kind AS current_actor_kind,e.actor_id AS current_actor_id,
e.provenance_json AS current_provenance_json,
e.decision_json AS current_decision_json,e.note AS current_note,
e.observed_ns AS current_observed_ns,e.recorded_ns AS current_recorded_ns"""
_EFFECTIVE_SOURCE_PUBLICATION_PREDICATE = """h.publication_id IS NOT NULL
AND e.to_state IN ('open','in_review')
AND t.source_snapshot_fingerprint<>h.source_snapshot_fingerprint
AND t.created_ns<=h.confirmed_ns AND b.confirmed_ns<h.confirmed_ns
AND e.observed_ns<=h.confirmed_ns AND e.recorded_ns<h.confirmed_ns
AND replacement.logical_key IS NULL"""
_EFFECTIVE_SOURCE_PUBLICATION_JOIN = """ LEFT JOIN
review_task_source_publications h ON h.scope=t.scope AND h.task_type=t.task_type
AND h.selector_signature=b.selector_signature AND NOT EXISTS(
    SELECT 1 FROM review_task_source_publications later_head
    WHERE later_head.scope=h.scope AND later_head.task_type=h.task_type
      AND later_head.selector_signature=h.selector_signature
      AND later_head.revision>h.revision
) LEFT JOIN (
    SELECT logical_key,scope,task_type,source_snapshot_fingerprint
    FROM review_tasks
    GROUP BY logical_key,scope,task_type,source_snapshot_fingerprint
) replacement ON replacement.logical_key=t.logical_key
AND replacement.scope=t.scope AND replacement.task_type=t.task_type
AND replacement.source_snapshot_fingerprint=h.source_snapshot_fingerprint """
_EFFECTIVE_SOURCE_PUBLICATION_COLUMNS = (
    "CASE WHEN "
    + _EFFECTIVE_SOURCE_PUBLICATION_PREDICATE
    + " THEN h.publication_id END AS effective_source_publication_id,"
    "CASE WHEN "
    + _EFFECTIVE_SOURCE_PUBLICATION_PREDICATE
    + " THEN h.source_snapshot_fingerprint END "
    "AS effective_source_snapshot_fingerprint,"
    "CASE WHEN "
    + _EFFECTIVE_SOURCE_PUBLICATION_PREDICATE
    + " THEN h.confirmed_ns END AS effective_source_confirmed_ns"
)


def _source_supersession_event(
    record: ReviewTaskRecord,
    publication: ReviewTaskSourcePublication,
) -> ReviewTaskEvent:
    previous = record.current_event
    event_key = _digest_id(
        "review-task-source-supersession-key-v1",
        record.task.task_id,
        previous.event_id,
        publication.publication_id,
    )
    return ReviewTaskEvent(
        event_id=_digest_id("review-task-source-supersession-event-v1", event_key),
        event_key=event_key,
        task_id=record.task.task_id,
        sequence=previous.sequence + 1,
        previous_event_id=previous.event_id,
        from_state=previous.to_state,
        to_state=ReviewTaskState.SUPERSEDED,
        actor_kind=ReviewTaskActorKind.SYSTEM,
        actor_id="review-task-source-publication",
        provenance=CanonicalJsonObject.from_mapping(
            {
                "derived": True,
                "reason_code": "absent_from_complete_source_snapshot",
                "source_publication_id": publication.publication_id,
                "source_snapshot_fingerprint": (publication.fence.source_snapshot_fingerprint),
            }
        ),
        decision=None,
        note=None,
        observed_ns=publication.confirmed_ns,
        recorded_ns=publication.confirmed_ns,
    )


def _effective_source_publications_by_id(
    connection: sqlite3.Connection,
    publication_ids: Sequence[str],
) -> dict[str, ReviewTaskSourcePublication]:
    ids = tuple(dict.fromkeys(publication_ids))
    if not ids:
        return {}
    if len(ids) > MAX_REVIEW_TASK_READ_PAGE + 1:
        raise ReviewTaskRepositoryError("effective source publication lookup is unbounded")
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT p.{_SOURCE_PUBLICATION_COLUMNS.replace(',', ',p.')},"
        "b.batch_id AS linked_batch_id,progress.progress_id AS linked_progress_id,"
        "previous.publication_id AS linked_previous_publication_id "
        "FROM review_task_source_publications p "
        "LEFT JOIN review_task_batches b ON b.batch_id=p.batch_id "
        "AND b.scope=p.scope AND b.task_type=p.task_type "
        "AND b.selector_signature=p.selector_signature "
        "AND b.source_snapshot_fingerprint=p.source_snapshot_fingerprint "
        "AND b.source_snapshot_json=p.source_snapshot_json "
        "AND b.coverage='complete' AND b.evidence_complete=1 "
        "AND b.confirmed_ns=p.confirmed_ns "
        "AND (SELECT COUNT(*) FROM review_task_batch_memberships membership "
        "     WHERE membership.batch_id=b.batch_id)=b.selected_count "
        "LEFT JOIN review_task_scan_progress progress "
        "ON progress.last_batch_id=p.batch_id AND progress.scope=p.scope "
        "AND progress.task_type=p.task_type "
        "AND progress.selector_signature=p.selector_signature "
        "AND progress.source_snapshot_fingerprint=p.source_snapshot_fingerprint "
        "AND progress.source_snapshot_json=p.source_snapshot_json "
        "AND progress.complete=1 AND progress.evidence_complete=1 "
        "AND b.scan_revision=progress.revision "
        "AND b.cumulative_scanned_count=progress.scanned_count "
        "AND b.cumulative_selected_count=progress.selected_count "
        "AND b.cumulative_evidence_complete=progress.evidence_complete "
        "AND b.cumulative_evidence_reason IS progress.evidence_reason "
        "LEFT JOIN review_task_source_publications previous "
        "ON previous.publication_id=p.previous_publication_id "
        "AND previous.scope=p.scope AND previous.task_type=p.task_type "
        "AND previous.selector_signature=p.selector_signature "
        "AND previous.revision=p.revision-1 "
        "AND previous.confirmed_ns<p.confirmed_ns "
        f"WHERE p.publication_id IN ({placeholders}) ORDER BY p.publication_id",
        ids,
    ).fetchall()
    result: dict[str, ReviewTaskSourcePublication] = {}
    for row in rows:
        publication = _source_publication_from_row(row)
        if row["linked_batch_id"] is None or row["linked_progress_id"] is None:
            raise ReviewTaskRepositoryError(
                "effective source publication lacks its final owner-local receipt"
            )
        if publication.revision > 1 and row["linked_previous_publication_id"] is None:
            raise ReviewTaskRepositoryError(
                "effective source publication lacks its predecessor receipt"
            )
        result[publication.publication_id] = publication
    if set(result) != set(ids):
        raise ReviewTaskRepositoryError("effective source publication is missing")
    return result


def _apply_effective_source_publications(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    records: tuple[ReviewTaskRecord, ...],
) -> tuple[ReviewTaskRecord, ...]:
    if not rows or "effective_source_publication_id" not in rows[0].keys():
        return records
    publication_ids = tuple(
        str(row["effective_source_publication_id"])
        for row in rows
        if row["effective_source_publication_id"] is not None
    )
    publications = _effective_source_publications_by_id(connection, publication_ids)
    effective: list[ReviewTaskRecord] = []
    for row, record in zip(rows, records, strict=True):
        raw_publication_id = row["effective_source_publication_id"]
        if raw_publication_id is None:
            effective.append(record)
            continue
        publication = publications[str(raw_publication_id)]
        if (
            row["effective_source_snapshot_fingerprint"]
            != publication.fence.source_snapshot_fingerprint
            or int(row["effective_source_confirmed_ns"]) != publication.confirmed_ns
            or record.current_event.to_state
            not in {ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW}
            or record.current_event.recorded_ns >= publication.confirmed_ns
        ):
            raise ReviewTaskRepositoryError(
                "effective source supersession receipt disagrees with its task"
            )
        effective.append(
            ReviewTaskRecord(
                task=record.task,
                source=record.source,
                source_snapshot_fingerprint=record.source_snapshot_fingerprint,
                batch_id=record.batch_id,
                selector_signature=record.selector_signature,
                current_event=_source_supersession_event(record, publication),
            )
        )
    return tuple(effective)


def _validated_records_by_task_ids(
    connection: sqlite3.Connection,
    task_ids: Sequence[str],
) -> tuple[ReviewTaskRecord, ...]:
    ids = tuple(task_ids)
    if not ids:
        return ()
    if len(ids) > MAX_REVIEW_TASKS_PER_PAGE or len(set(ids)) != len(ids):
        raise ReviewTaskRepositoryError("bounded ReviewTask identity lookup is invalid")
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        """WITH current_events AS (
            SELECT e.* FROM review_task_events e
            WHERE NOT EXISTS(
                SELECT 1 FROM review_task_events later
                WHERE later.task_id=e.task_id AND later.sequence>e.sequence
            )
        )
        SELECT t.*,b.selector_signature AS batch_selector_signature,
        b.source_snapshot_json AS batch_source_snapshot_json,
        b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,"""
        + _CURRENT_EVENT_COLUMNS
        + ","
        + _EFFECTIVE_SOURCE_PUBLICATION_COLUMNS
        + " FROM review_tasks t LEFT JOIN current_events e ON e.task_id=t.task_id "
        "LEFT JOIN review_task_batches b ON b.batch_id=t.batch_id "
        + _EFFECTIVE_SOURCE_PUBLICATION_JOIN
        + f"WHERE t.task_id IN ({placeholders}) ORDER BY t.task_id",
        ids,
    ).fetchall()
    records = _validated_task_records(connection, rows)
    if {record.task.task_id for record in records} != set(ids):
        raise ReviewTaskRepositoryError("ReviewTask identity lookup is incomplete")
    return records


def _insert_event(connection: sqlite3.Connection, event: ReviewTaskEvent) -> None:
    connection.execute(
        """INSERT INTO review_task_events(
        event_id,event_key,task_id,sequence,previous_event_id,from_state,to_state,
        actor_kind,actor_id,provenance_json,decision_json,note,observed_ns,
        recorded_ns,event_schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event.event_id,
            event.event_key,
            event.task_id,
            event.sequence,
            event.previous_event_id,
            None if event.from_state is None else event.from_state.value,
            event.to_state.value,
            event.actor_kind.value,
            event.actor_id,
            event.provenance.payload_json,
            None if event.decision is None else event.decision.payload_json,
            event.note,
            event.observed_ns,
            event.recorded_ns,
            REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        ),
    )


def _latest_event(connection: sqlite3.Connection, task_id: str) -> ReviewTaskEvent | None:
    row = connection.execute(
        "SELECT " + _EVENT_COLUMNS + " FROM review_task_events "
        "WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return None if row is None else _event_from_row(row)


def _initial_event(
    publication: ReviewTaskPublication,
    task: ReviewTaskDraft,
) -> ReviewTaskEvent:
    provenance = CanonicalJsonObject.from_mapping(
        {
            "batch_id": publication.batch_id,
            "batch_key": publication.batch_key,
            "producer_signature": publication.producer_signature,
            "source_snapshot_fingerprint": (publication.fence.source_snapshot_fingerprint),
        }
    )
    return ReviewTaskEvent(
        event_id=_digest_id("review-task-event-v1", task.task_id, "open"),
        event_key=_digest_id("review-task-event-key-v1", task.task_id, "open"),
        task_id=task.task_id,
        sequence=1,
        previous_event_id=None,
        from_state=None,
        to_state=ReviewTaskState.OPEN,
        actor_kind=ReviewTaskActorKind.SYSTEM,
        actor_id="review-task-publisher",
        provenance=provenance,
        decision=None,
        note=None,
        observed_ns=task.created_ns,
        recorded_ns=publication.confirmed_ns,
    )


def _supersede_event(
    *,
    task_id: str,
    previous: ReviewTaskEvent,
    source_snapshot_fingerprint: str,
    observed_ns: int,
    reason_code: str,
    replacement_task_id: str | None = None,
) -> ReviewTaskEvent:
    provenance_payload: dict[str, object] = {
        "reason_code": reason_code,
        "source_snapshot_fingerprint": source_snapshot_fingerprint,
    }
    if replacement_task_id is not None:
        provenance_payload["replacement_task_id"] = replacement_task_id
    event_key = _digest_id(
        "review-task-supersede-key-v1",
        task_id,
        previous.event_id,
        source_snapshot_fingerprint,
        reason_code,
    )
    return ReviewTaskEvent(
        event_id=_digest_id("review-task-supersede-event-v1", event_key),
        event_key=event_key,
        task_id=task_id,
        sequence=previous.sequence + 1,
        previous_event_id=previous.event_id,
        from_state=previous.to_state,
        to_state=ReviewTaskState.SUPERSEDED,
        actor_kind=ReviewTaskActorKind.SYSTEM,
        actor_id="review-task-refresh",
        provenance=CanonicalJsonObject.from_mapping(provenance_payload),
        decision=None,
        note=None,
        observed_ns=observed_ns,
        recorded_ns=observed_ns,
    )


def _insert_batch(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
    previous_progress: ReviewTaskScanProgress | None,
) -> None:
    if previous_progress is None:
        previous_batch_id = None
        scan_revision = 1
        cumulative_scanned_count = publication.scanned_count
        cumulative_selected_count = publication.selected_count
        cumulative_evidence_complete = publication.evidence_complete
        cumulative_evidence_reason = publication.evidence_reason
    else:
        previous_batch_id = previous_progress.last_batch_id
        scan_revision = previous_progress.revision + 1
        cumulative_scanned_count = previous_progress.scanned_count + publication.scanned_count
        cumulative_selected_count = previous_progress.selected_count + publication.selected_count
        cumulative_evidence_complete = (
            previous_progress.evidence_complete and publication.evidence_complete
        )
        cumulative_evidence_reason = (
            previous_progress.evidence_reason
            if not previous_progress.evidence_complete
            else publication.evidence_reason
        )
    connection.execute(
        """INSERT INTO review_task_batches(
        batch_id,batch_key,scope,task_type,selector_signature,
        source_snapshot_fingerprint,source_snapshot_json,cursor_before_json,
        cursor_after_json,previous_batch_id,scan_revision,page_size,scanned_count,
        selected_count,cumulative_scanned_count,cumulative_selected_count,
        coverage,evidence_complete,evidence_reason,cumulative_evidence_complete,
        cumulative_evidence_reason,producer_signature,receipt_json,confirmed_ns,
        receipt_schema_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            publication.batch_id,
            publication.batch_key,
            publication.fence.scope,
            publication.fence.task_type,
            publication.fence.selector_signature,
            publication.fence.source_snapshot_fingerprint,
            publication.fence.source_snapshot.payload_json,
            (None if publication.cursor_before is None else publication.cursor_before.payload_json),
            (None if publication.cursor_after is None else publication.cursor_after.payload_json),
            previous_batch_id,
            scan_revision,
            publication.page_size,
            publication.scanned_count,
            publication.selected_count,
            cumulative_scanned_count,
            cumulative_selected_count,
            publication.coverage.value,
            int(publication.evidence_complete),
            publication.evidence_reason,
            int(cumulative_evidence_complete),
            cumulative_evidence_reason,
            publication.producer_signature,
            publication.to_json(),
            publication.confirmed_ns,
            REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        ),
    )


def _current_source_publication(
    connection: sqlite3.Connection,
    fence: ReviewTaskSourceFence,
) -> ReviewTaskSourcePublication | None:
    row = connection.execute(
        f"SELECT {_SOURCE_PUBLICATION_COLUMNS} "
        "FROM review_task_source_publications "
        "WHERE scope=? AND task_type=? AND selector_signature=? "
        "ORDER BY revision DESC LIMIT 1",
        (fence.scope, fence.task_type, fence.selector_signature),
    ).fetchone()
    return None if row is None else _source_publication_from_row(row)


def _source_publication_for_batch(
    connection: sqlite3.Connection,
    batch_id: str,
) -> ReviewTaskSourcePublication | None:
    row = connection.execute(
        f"SELECT {_SOURCE_PUBLICATION_COLUMNS} "
        "FROM review_task_source_publications WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    return None if row is None else _source_publication_from_row(row)


def _publish_source_publication(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
    progress: ReviewTaskScanProgress,
) -> ReviewTaskSourcePublication | None:
    """Promote one complete/evidence-complete source with one append-only head fact."""

    if not progress.complete or not progress.evidence_complete:
        return None
    if progress.last_batch_id != publication.batch_id or progress.fence != publication.fence:
        raise ReviewTaskRepositoryError(
            "ReviewTask source publication does not match final scan progress"
        )
    existing = _source_publication_for_batch(connection, publication.batch_id)
    current = _current_source_publication(connection, publication.fence)
    if existing is not None:
        if (
            existing.fence != publication.fence
            or existing.confirmed_ns != publication.confirmed_ns
            or current is None
            or current.revision < existing.revision
        ):
            raise ReviewTaskRepositoryError(
                "ReviewTask source publication idempotency payload changed"
            )
        return existing
    revision = 1 if current is None else current.revision + 1
    source_publication = ReviewTaskSourcePublication(
        publication_id=_source_publication_id(publication.batch_id),
        publication_key=_source_publication_key(publication.batch_id),
        fence=publication.fence,
        batch_id=publication.batch_id,
        previous_publication_id=(None if current is None else current.publication_id),
        revision=revision,
        confirmed_ns=publication.confirmed_ns,
    )
    connection.execute(
        """INSERT INTO review_task_source_publications(
        publication_id,publication_key,scope,task_type,selector_signature,
        source_snapshot_fingerprint,source_snapshot_json,batch_id,
        previous_publication_id,revision,confirmed_ns,receipt_json,
        receipt_schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source_publication.publication_id,
            source_publication.publication_key,
            source_publication.fence.scope,
            source_publication.fence.task_type,
            source_publication.fence.selector_signature,
            source_publication.fence.source_snapshot_fingerprint,
            source_publication.fence.source_snapshot.payload_json,
            source_publication.batch_id,
            source_publication.previous_publication_id,
            source_publication.revision,
            source_publication.confirmed_ns,
            source_publication.to_json(),
            REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        ),
    )
    return source_publication


def _insert_tasks(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
    supersession_events: dict[str, ReviewTaskEvent],
) -> None:
    inputs = {item.input_id: item for item in publication.inputs}
    for task in publication.tasks:
        source = inputs[task.source_input_id]
        evidence_json = _canonical_json([item.to_dict() for item in task.evidence])
        suggestions_json = _canonical_json(list(task.suggestions))
        connection.execute(
            """INSERT INTO review_tasks(
            task_id,logical_key,task_version,task_type,scope,source_kind,
            source_input_id,source_ref_json,source_snapshot_fingerprint,
            snapshot_json,evidence_json,reason_code,uncertainty_json,impact,
            uncertainty,irreversibility,priority,priority_algorithm,
            suggestions_json,batch_id,supersedes_task_id,supersession_event_id,
            created_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.task_id,
                task.logical_key,
                task.task_version,
                task.task_type,
                task.scope,
                task.source_kind,
                task.source_input_id,
                source.to_json(),
                publication.fence.source_snapshot_fingerprint,
                task.snapshot.payload_json,
                evidence_json,
                task.reason_code,
                task.uncertainty_detail.payload_json,
                task.impact,
                task.uncertainty,
                task.irreversibility,
                task.priority,
                REVIEW_TASK_PRIORITY_ALGORITHM,
                suggestions_json,
                publication.batch_id,
                task.supersedes_task_id,
                (
                    None
                    if task.supersedes_task_id is None
                    else supersession_events[task.task_id].event_id
                ),
                task.created_ns,
            ),
        )
        _insert_event(connection, _initial_event(publication, task))


def _insert_batch_memberships(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
) -> None:
    for task in publication.tasks:
        connection.execute(
            """INSERT INTO review_task_batch_memberships(
            membership_id,batch_id,task_id,source_input_id,recorded_ns,
            membership_schema_version) VALUES(?,?,?,?,?,?)""",
            (
                _batch_membership_id(publication.batch_id, task.task_id),
                publication.batch_id,
                task.task_id,
                task.source_input_id,
                publication.confirmed_ns,
                REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
            ),
        )


_SCOPED_DECISION_SCHEMA = "neocortex.review-task-decision/v1"


def _scoped_terminal_decision(
    record: ReviewTaskRecord,
    *,
    selector_signature: str,
) -> tuple[str, str, str]:
    decision = record.current_event.decision
    if decision is None:
        raise ReviewTaskCASConflict("terminal ReviewTask lacks a scoped human decision")
    payload = decision.to_dict()
    expected_keys = {
        "decision",
        "schema",
        "scope",
        "selector_signature",
        "source_input_fingerprint",
        "source_snapshot_fingerprint",
    }
    if set(payload) != expected_keys:
        # Legacy decisions are deliberately permanent.  Do not invent a scope
        # for an older human judgment that did not record one.
        raise ReviewTaskCASConflict("legacy terminal ReviewTask cannot be reopened")
    decision_value = payload.get("decision")
    scope = payload.get("scope")
    input_fingerprint = payload.get("source_input_fingerprint")
    if (
        payload.get("schema") != _SCOPED_DECISION_SCHEMA
        or decision_value != record.current_event.to_state.value
        or scope not in {"until-source-change", "until-policy-change", "permanent"}
        or input_fingerprint != record.source.fingerprint
        or payload.get("source_snapshot_fingerprint") != record.source_snapshot_fingerprint
        or payload.get("selector_signature") != selector_signature
    ):
        raise ReviewTaskRepositoryError("terminal ReviewTask decision contract is contradictory")
    return str(scope), str(input_fingerprint), selector_signature


def _validate_terminal_scope_expiration(
    connection: sqlite3.Connection,
    predecessor_record: ReviewTaskRecord,
    *,
    task: ReviewTaskDraft,
    publication: ReviewTaskPublication,
) -> None:
    row = connection.execute(
        "SELECT selector_signature FROM review_task_batches WHERE batch_id=?",
        (predecessor_record.batch_id,),
    ).fetchone()
    if row is None:
        raise ReviewTaskRepositoryError("terminal ReviewTask lost its owner batch")
    previous_selector = str(row[0])
    scope, previous_fingerprint, previous_selector = _scoped_terminal_decision(
        predecessor_record,
        selector_signature=previous_selector,
    )
    if scope == "permanent":
        raise ReviewTaskCASConflict("permanent terminal ReviewTask cannot be reopened")
    source_by_id = {item.input_id: item for item in publication.inputs}
    successor_source = source_by_id.get(task.source_input_id)
    if successor_source is None:
        raise ReviewTaskRepositoryError("ReviewTask successor lacks its exact source input")
    previous_resource = predecessor_record.source.resource
    successor_resource = successor_source.resource
    if (
        previous_resource is None
        or successor_resource is None
        or previous_resource.resource_id != successor_resource.resource_id
        or previous_resource.physical_identity != successor_resource.physical_identity
    ):
        raise ReviewTaskCASConflict("terminal ReviewTask successor changed durable resource")
    expired = (
        successor_source.fingerprint != previous_fingerprint
        if scope == "until-source-change"
        else publication.fence.selector_signature != previous_selector
    )
    if not expired:
        raise ReviewTaskCASConflict("terminal ReviewTask decision scope has not expired")


def _prepare_predecessor_events(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
) -> dict[str, ReviewTaskEvent]:
    predecessor_ids = tuple(
        dict.fromkeys(
            task.supersedes_task_id
            for task in publication.tasks
            if task.supersedes_task_id is not None
        )
    )
    predecessor_by_id = {
        record.task.task_id: record
        for record in _validated_records_by_task_ids(connection, predecessor_ids)
    }
    result: dict[str, ReviewTaskEvent] = {}
    for task in publication.tasks:
        if task.supersedes_task_id is None:
            continue
        predecessor_record = predecessor_by_id.get(task.supersedes_task_id)
        if predecessor_record is None:
            raise ReviewTaskCASConflict("ReviewTask predecessor does not exist")
        predecessor = predecessor_record.task
        expected = (
            task.logical_key,
            task.task_version - 1,
            task.scope,
            task.task_type,
            task.source_kind,
        )
        observed = (
            predecessor.logical_key,
            predecessor.task_version,
            predecessor.scope,
            predecessor.task_type,
            predecessor.source_kind,
        )
        if observed != expected:
            raise ReviewTaskCASConflict("ReviewTask predecessor changed logical owner/type/source")
        if predecessor.created_ns >= task.created_ns:
            raise ReviewTaskCASConflict(
                "ReviewTask successor timestamp must advance its predecessor"
            )
        latest_version = int(
            connection.execute(
                "SELECT MAX(task_version) FROM review_tasks WHERE logical_key=?",
                (task.logical_key,),
            ).fetchone()[0]
        )
        if latest_version != task.task_version - 1:
            raise ReviewTaskCASConflict("ReviewTask predecessor is not the latest version")
        previous = predecessor_record.current_event
        terminal_reopen = previous.to_state in {
            ReviewTaskState.RESOLVED,
            ReviewTaskState.DISMISSED,
        }
        if terminal_reopen:
            _validate_terminal_scope_expiration(
                connection,
                predecessor_record,
                task=task,
                publication=publication,
            )
        if previous.to_state is ReviewTaskState.SUPERSEDED:
            actual_previous = _latest_event(connection, task.supersedes_task_id)
            if actual_previous is None:
                raise ReviewTaskRepositoryError("superseded ReviewTask has no event history")
            if actual_previous.event_id != previous.event_id:
                provenance = previous.provenance.to_dict()
                if (
                    provenance.get("derived") is not True
                    or provenance.get("source_publication_id") is None
                    or previous.previous_event_id != actual_previous.event_id
                    or previous.from_state is not actual_previous.to_state
                ):
                    raise ReviewTaskRepositoryError(
                        "effective ReviewTask supersession cannot be materialized"
                    )
            result[task.task_id] = previous
            continue
        result[task.task_id] = _supersede_event(
            task_id=task.supersedes_task_id,
            previous=previous,
            source_snapshot_fingerprint=(publication.fence.source_snapshot_fingerprint),
            observed_ns=publication.confirmed_ns,
            reason_code=(
                "terminal_decision_scope_expired"
                if terminal_reopen
                else "replacement_task_published"
            ),
            replacement_task_id=task.task_id,
        )
    return result


def _materialize_predecessor_events(
    connection: sqlite3.Connection,
    supersession_events: dict[str, ReviewTaskEvent],
) -> None:
    for event in dict.fromkeys(supersession_events.values()):
        actual = _latest_event(connection, event.task_id)
        if actual is None:
            raise ReviewTaskRepositoryError("ReviewTask predecessor event history disappeared")
        if actual.event_id == event.event_id:
            continue
        if actual.event_id != event.previous_event_id:
            raise ReviewTaskCASConflict("ReviewTask predecessor event changed")
        _insert_event(
            connection,
            event,
        )


def _read_progress_in_connection(
    connection: sqlite3.Connection,
    fence: ReviewTaskSourceFence,
) -> ReviewTaskScanProgress | None:
    row = connection.execute(
        """SELECT p.progress_id,p.scope,p.task_type,p.selector_signature,
        p.source_snapshot_fingerprint,p.source_snapshot_json,p.cursor_json,
        p.last_batch_id,p.scanned_count,p.selected_count,p.complete,
        p.evidence_complete,p.evidence_reason,p.revision,p.created_ns,p.updated_ns,
        b.batch_key,
        b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,
        b.source_snapshot_json AS batch_source_snapshot_json,
        b.cursor_before_json AS batch_cursor_before_json,
        b.cursor_after_json AS batch_cursor_after_json,
        b.page_size AS batch_page_size,b.scanned_count AS batch_scanned_count,
        b.selected_count AS batch_selected_count,b.coverage AS batch_coverage,
        b.evidence_complete AS batch_evidence_complete,
        b.evidence_reason AS batch_evidence_reason,
        b.producer_signature AS batch_producer_signature,
        b.receipt_json AS batch_receipt_json,b.confirmed_ns AS batch_confirmed_ns
        FROM review_task_scan_progress p
        LEFT JOIN review_task_batches b ON b.batch_id=p.last_batch_id
        WHERE p.scope=? AND p.task_type=? AND p.selector_signature=?
          AND p.source_snapshot_fingerprint=?""",
        (
            fence.scope,
            fence.task_type,
            fence.selector_signature,
            fence.source_snapshot_fingerprint,
        ),
    ).fetchone()
    if row is None:
        return None
    progress = _progress_from_row(row)
    if progress.fence.source_snapshot.payload_json != fence.source_snapshot.payload_json:
        raise ReviewTaskRepositoryError(
            "source snapshot fingerprint aliases two different canonical snapshots"
        )
    _validate_progress_cumulative_counts(connection, progress)
    _validate_progress_batch_receipt(connection, row, progress)
    return progress


def _advance_progress(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
    *,
    expected_progress_revision: int | None,
    current: ReviewTaskScanProgress | None,
) -> ReviewTaskScanProgress:
    cursor_before = (
        None if publication.cursor_before is None else publication.cursor_before.payload_json
    )
    cursor_after = (
        None if publication.cursor_after is None else publication.cursor_after.payload_json
    )
    complete = publication.coverage is ReviewTaskCoverage.COMPLETE
    progress_id = _progress_id(publication.fence)
    if current is None:
        if expected_progress_revision is not None:
            raise ReviewTaskCASConflict("ReviewTask progress does not exist at expected revision")
        if cursor_before is not None:
            raise ReviewTaskCASConflict("initial ReviewTask page must start without a cursor")
        connection.execute(
            """INSERT INTO review_task_scan_progress(
            progress_id,scope,task_type,selector_signature,source_snapshot_fingerprint,
            source_snapshot_json,cursor_json,last_batch_id,scanned_count,
            selected_count,complete,evidence_complete,evidence_reason,revision,
            created_ns,updated_ns)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                progress_id,
                publication.fence.scope,
                publication.fence.task_type,
                publication.fence.selector_signature,
                publication.fence.source_snapshot_fingerprint,
                publication.fence.source_snapshot.payload_json,
                cursor_after,
                publication.batch_id,
                publication.scanned_count,
                publication.selected_count,
                int(complete),
                int(publication.evidence_complete),
                publication.evidence_reason,
                1,
                publication.confirmed_ns,
                publication.confirmed_ns,
            ),
        )
    else:
        if expected_progress_revision != current.revision:
            raise ReviewTaskCASConflict("ReviewTask progress revision changed")
        current_cursor = None if current.cursor is None else current.cursor.payload_json
        if current.complete:
            raise ReviewTaskCASConflict("completed ReviewTask progress cannot be advanced")
        if cursor_before != current_cursor:
            raise ReviewTaskCASConflict("ReviewTask page cursor does not match durable progress")
        if publication.confirmed_ns <= current.updated_ns:
            raise ReviewTaskCASConflict("ReviewTask progress timestamp did not advance")
        updated = connection.execute(
            """UPDATE review_task_scan_progress SET
            cursor_json=?,last_batch_id=?,scanned_count=?,selected_count=?,
            complete=?,evidence_complete=?,evidence_reason=?,revision=?,updated_ns=?
            WHERE progress_id=? AND revision=? AND complete=0""",
            (
                cursor_after,
                publication.batch_id,
                current.scanned_count + publication.scanned_count,
                current.selected_count + publication.selected_count,
                int(complete),
                int(current.evidence_complete and publication.evidence_complete),
                (
                    current.evidence_reason
                    if not current.evidence_complete
                    else publication.evidence_reason
                ),
                current.revision + 1,
                publication.confirmed_ns,
                current.progress_id,
                current.revision,
            ),
        )
        if updated.rowcount != 1:
            raise ReviewTaskCASConflict("ReviewTask progress CAS failed")
    result = _read_progress_in_connection(connection, publication.fence)
    if result is None:  # pragma: no cover - transaction invariant
        raise ReviewTaskRepositoryError("ReviewTask progress disappeared after publication")
    return result


def _validate_progress_precondition(
    publication: ReviewTaskPublication,
    current: ReviewTaskScanProgress | None,
    expected_progress_revision: int | None,
) -> None:
    cursor_before = (
        None if publication.cursor_before is None else publication.cursor_before.payload_json
    )
    if current is None:
        if expected_progress_revision is not None:
            raise ReviewTaskCASConflict("ReviewTask progress does not exist at expected revision")
        if cursor_before is not None:
            raise ReviewTaskCASConflict("initial ReviewTask page must start without a cursor")
        return
    if expected_progress_revision != current.revision:
        raise ReviewTaskCASConflict("ReviewTask progress revision changed")
    if current.complete:
        raise ReviewTaskCASConflict("completed ReviewTask progress cannot be advanced")
    current_cursor = None if current.cursor is None else current.cursor.payload_json
    if cursor_before != current_cursor:
        raise ReviewTaskCASConflict("ReviewTask page cursor does not match durable progress")
    if publication.confirmed_ns <= current.updated_ns:
        raise ReviewTaskCASConflict("ReviewTask progress timestamp did not advance")


def _existing_batch_result(
    connection: sqlite3.Connection,
    publication: ReviewTaskPublication,
) -> ReviewTaskPublicationResult | None:
    rows = connection.execute(
        f"SELECT {_BATCH_COLUMNS} FROM review_task_batches WHERE batch_id=? OR batch_key=?",
        (publication.batch_id, publication.batch_key),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ReviewTaskRepositoryError("ReviewTask batch id/key resolve to different facts")
    row = rows[0]
    persisted_publication = _publication_from_batch_row(row)
    _validate_batch_chain_row(connection, row)
    if persisted_publication != publication:
        raise ReviewTaskRepositoryError("ReviewTask batch idempotency key changed payload")
    progress = _read_progress_in_connection(connection, publication.fence)
    if progress is None:
        raise ReviewTaskRepositoryError("committed ReviewTask batch lacks durable progress")
    if (
        progress.last_batch_id == publication.batch_id
        and progress.complete
        and progress.evidence_complete
        and _source_publication_for_batch(connection, publication.batch_id) is None
    ):
        raise ReviewTaskRepositoryError(
            "committed complete ReviewTask batch lacks its source publication receipt"
        )
    return ReviewTaskPublicationResult(
        batch_id=publication.batch_id,
        task_ids=tuple(task.task_id for task in publication.tasks),
        progress=progress,
        idempotent=True,
    )


def publish_review_task_page(
    database: str | Path,
    publication: ReviewTaskPublication,
    *,
    expected_progress_revision: int | None,
    cancellation_check: CancellationCheck | None = None,
    timeout_seconds: float = 60.0,
    _fault_injector: _FaultInjector | None = None,
) -> ReviewTaskPublicationResult:
    """Atomically publish one bounded page, OPEN events, and its resumable cursor."""

    if not isinstance(publication, ReviewTaskPublication):
        raise TypeError("publication must be a ReviewTaskPublication")
    if expected_progress_revision is not None and (
        isinstance(expected_progress_revision, bool)
        or not isinstance(expected_progress_revision, int)
        or expected_progress_revision < 1
    ):
        raise ValueError("expected_progress_revision must be positive when present")
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(
        Path(database), readonly=False, timeout_seconds=timeout_seconds
    )
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN IMMEDIATE")
            try:
                _require_review_task_schema(connection)
                existing = _existing_batch_result(connection, publication)
                if existing is not None:
                    connection.commit()
                    return existing
                previous_progress = _read_progress_in_connection(connection, publication.fence)
                _validate_progress_precondition(
                    publication, previous_progress, expected_progress_revision
                )
                _insert_batch(connection, publication, previous_progress)
                _fault(_fault_injector, "after_batch")
                _checkpoint(bridge)
                supersession_events = _prepare_predecessor_events(connection, publication)
                _fault(_fault_injector, "after_predecessors")
                _checkpoint(bridge)
                _insert_tasks(connection, publication, supersession_events)
                _fault(_fault_injector, "after_tasks")
                _checkpoint(bridge)
                _materialize_predecessor_events(connection, supersession_events)
                _fault(_fault_injector, "after_events")
                _checkpoint(bridge)
                _insert_batch_memberships(connection, publication)
                _fault(_fault_injector, "after_memberships")
                _checkpoint(bridge)
                progress = _advance_progress(
                    connection,
                    publication,
                    expected_progress_revision=expected_progress_revision,
                    current=previous_progress,
                )
                _fault(_fault_injector, "after_progress")
                _checkpoint(bridge)
                _fault(_fault_injector, "before_source_head")
                _publish_source_publication(connection, publication, progress)
                _fault(_fault_injector, "after_source_head")
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return ReviewTaskPublicationResult(
        batch_id=publication.batch_id,
        task_ids=tuple(task.task_id for task in publication.tasks),
        progress=progress,
        idempotent=False,
    )


def read_review_task_publication(
    database: str | Path,
    batch_id: str,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskPublication | None:
    """Read one immutable ReviewTask batch receipt without creating state."""

    _contracts._required_text("batch_id", batch_id)
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                rows = connection.execute(
                    f"SELECT {_BATCH_COLUMNS} FROM review_task_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchall()
                if len(rows) > 1:
                    raise ReviewTaskRepositoryError("ReviewTask batch identifier is ambiguous")
                result = None if not rows else _publication_from_batch_row(rows[0])
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def read_current_review_task_source_publication(
    database: str | Path, fence: ReviewTaskSourceFence, *,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskSourcePublication | None:
    """Return an exact current source head only after validating final receipts."""

    if not isinstance(fence, ReviewTaskSourceFence):
        raise TypeError("fence must be a ReviewTaskSourceFence")
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            _require_review_task_schema(connection)
            selected = _current_source_publication(connection, fence)
            if selected is None or selected.fence != fence:
                return None
            validated = _effective_source_publications_by_id(connection, (selected.publication_id,))
            _checkpoint(bridge)
            connection.commit()
            return validated[selected.publication_id]
    finally:
        connection.close()


def read_review_task_progress(
    database: str | Path,
    fence: ReviewTaskSourceFence,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskScanProgress | None:
    """Read one exact selector/snapshot cursor without creating or migrating state."""

    if not isinstance(fence, ReviewTaskSourceFence):
        raise TypeError("fence must be a ReviewTaskSourceFence")
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                result = _read_progress_in_connection(connection, fence)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def has_review_task_scan_history(
    database: str | Path,
    *,
    scope: str,
    task_type: str,
    selector_signature: str,
    cancellation_check: CancellationCheck | None = None,
) -> bool:
    """Report whether this selector has any committed scan, including empty ones."""

    _contracts._required_text(
        "scope",
        scope,
        limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS,
    )
    _contracts._required_text(
        "task_type",
        task_type,
        limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS,
    )
    _contracts._required_text(
        "selector_signature",
        selector_signature,
        limit=_contracts.MAX_REVIEW_TASK_SELECTOR_CHARS,
    )
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                found = (
                    connection.execute(
                        """SELECT 1 FROM review_task_scan_progress
                        WHERE scope=? AND task_type=? AND selector_signature=?
                        LIMIT 1""",
                        (scope, task_type, selector_signature),
                    ).fetchone()
                    is not None
                )
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return found


def find_review_task_scan_progress(
    database: str | Path,
    *,
    scope: str,
    task_type: str,
    selector_signature: str,
    owner_source_snapshot: CanonicalJsonObject,
    cancellation_check: CancellationCheck | None = None,
) -> tuple[ReviewTaskScanProgress | None, ReviewTaskScanProgress | None]:
    """Return newest incomplete/complete epochs for one exact owner snapshot.

    The evaluation epoch is deliberately removed inside SQLite.  This keeps
    lookup work bounded no matter how many daily epochs have been published.
    """

    _contracts._required_text("scope", scope, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    _contracts._required_text("task_type", task_type, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    _contracts._required_text(
        "selector_signature",
        selector_signature,
        limit=_contracts.MAX_REVIEW_TASK_SELECTOR_CHARS,
    )
    owner_json = owner_source_snapshot.payload_json
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                parameters = (scope, task_type, selector_signature, owner_json)
                statement = """SELECT p.progress_id,p.scope,p.task_type,
                    p.selector_signature,p.source_snapshot_fingerprint,
                    p.source_snapshot_json,p.cursor_json,p.last_batch_id,
                    p.scanned_count,p.selected_count,p.complete,
                    p.evidence_complete,p.evidence_reason,p.revision,
                    p.created_ns,p.updated_ns
                    FROM review_task_scan_progress p
                    WHERE p.scope=? AND p.task_type=? AND p.selector_signature=?
                      AND json_remove(p.source_snapshot_json,'$.reference_day_ns')=?
                      AND p.complete=?
                    ORDER BY p.updated_ns DESC,p.revision DESC,p.progress_id
                    LIMIT 1"""
                # Query each class independently.  A shared LIMIT could let two
                # legacy incomplete epochs hide the last published complete
                # epoch and make a durable queue appear to have no head.
                rows = [
                    *connection.execute(statement, (*parameters, 0)).fetchall(),
                    *connection.execute(statement, (*parameters, 1)).fetchall(),
                ]
                incomplete: ReviewTaskScanProgress | None = None
                complete: ReviewTaskScanProgress | None = None
                for row in rows:
                    progress = _progress_from_row(row)
                    validated = _read_progress_in_connection(connection, progress.fence)
                    if validated is None:  # pragma: no cover - same transaction invariant
                        raise ReviewTaskRepositoryError(
                            "ReviewTask progress disappeared during bounded lookup"
                        )
                    if validated.complete:
                        if complete is None:
                            complete = validated
                    elif incomplete is None:
                        incomplete = validated
                result = (incomplete, complete)
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def read_latest_complete_review_task_progress(
    database: str | Path,
    *,
    scope: str,
    task_type: str,
    selector_signature: str,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskScanProgress | None:
    """Return the last fully published epoch for one selector.

    This lookup deliberately ignores the owner snapshot.  It is the stable
    public head used while a changed source is being rescanned; exact-source
    progress remains the authority for resumable writes.
    """

    _contracts._required_text("scope", scope, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    _contracts._required_text("task_type", task_type, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    _contracts._required_text(
        "selector_signature",
        selector_signature,
        limit=_contracts.MAX_REVIEW_TASK_SELECTOR_CHARS,
    )
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                row = connection.execute(
                    """SELECT p.progress_id,p.scope,p.task_type,
                    p.selector_signature,p.source_snapshot_fingerprint,
                    p.source_snapshot_json,p.cursor_json,p.last_batch_id,
                    p.scanned_count,p.selected_count,p.complete,
                    p.evidence_complete,p.evidence_reason,p.revision,
                    p.created_ns,p.updated_ns
                    FROM review_task_source_publications h
                    JOIN review_task_scan_progress p ON p.last_batch_id=h.batch_id
                    WHERE h.scope=? AND h.task_type=? AND h.selector_signature=?
                      AND p.complete=1
                    ORDER BY h.revision DESC LIMIT 1""",
                    (scope, task_type, selector_signature),
                ).fetchone()
                if row is None:
                    result = None
                else:
                    candidate = _progress_from_row(row)
                    result = _read_progress_in_connection(connection, candidate.fence)
                    if result is None or not result.complete:
                        raise ReviewTaskRepositoryError(
                            "published ReviewTask source head lacks complete progress"
                        )
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def _normalized_states(
    states: Iterable[ReviewTaskState] | None,
) -> tuple[ReviewTaskState, ...]:
    if states is None:
        return (ReviewTaskState.OPEN, ReviewTaskState.IN_REVIEW)
    if isinstance(states, (str, bytes)):
        raise TypeError("states must contain ReviewTaskState values")
    normalized = tuple(states)
    if not normalized or any(not isinstance(item, ReviewTaskState) for item in normalized):
        raise ValueError("states must contain at least one ReviewTaskState")
    if len(set(normalized)) != len(normalized):
        raise ValueError("states cannot contain duplicates")
    return normalized


def list_current_review_tasks(
    database: str | Path,
    *,
    limit: int,
    scope: str | None = None,
    task_type: str | None = None,
    source_snapshot_fingerprint: str | None = None,
    source_snapshot_as_published: bool = False,
    states: Iterable[ReviewTaskState] | None = None,
    after: ReviewTaskListCursor | None = None,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskRecordPage:
    """Read current task heads using bounded keyset pagination."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_REVIEW_TASK_READ_PAGE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_REVIEW_TASK_READ_PAGE}")
    if scope is not None:
        _contracts._required_text("scope", scope, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    if task_type is not None:
        _contracts._required_text(
            "task_type", task_type, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS
        )
    if source_snapshot_fingerprint is not None:
        expected_length = len(_contracts.REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX) + 64
        suffix = source_snapshot_fingerprint[
            len(_contracts.REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX) :
        ]
        if (
            len(source_snapshot_fingerprint) != expected_length
            or not source_snapshot_fingerprint.startswith(
                _contracts.REVIEW_TASK_SOURCE_FINGERPRINT_PREFIX
            )
            or any(character not in "0123456789abcdef" for character in suffix)
        ):
            raise ValueError("source_snapshot_fingerprint is invalid")
    if not isinstance(source_snapshot_as_published, bool):
        raise TypeError("source_snapshot_as_published must be a boolean")
    if source_snapshot_as_published and source_snapshot_fingerprint is None:
        raise ValueError("source_snapshot_as_published requires source_snapshot_fingerprint")
    if after is not None and not isinstance(after, ReviewTaskListCursor):
        raise TypeError("after must be a ReviewTaskListCursor when present")
    selected_states = _normalized_states(states)
    effective_state = (
        "e.to_state"
        if source_snapshot_as_published
        else (
            "CASE WHEN "
            + _EFFECTIVE_SOURCE_PUBLICATION_PREDICATE
            + " THEN 'superseded' ELSE e.to_state END"
        )
    )
    clauses = [
        "("
        + effective_state
        + " IN ("
        + ",".join("?" for _ in selected_states)
        + ") OR e.task_id IS NULL)"
    ]
    parameters: list[object] = []
    parameters.extend(item.value for item in selected_states)
    if scope is not None:
        clauses.append("t.scope=?")
        parameters.append(scope)
    if task_type is not None:
        clauses.append("t.task_type=?")
        parameters.append(task_type)
    if source_snapshot_fingerprint is not None:
        clauses.append("t.source_snapshot_fingerprint=?")
        parameters.append(source_snapshot_fingerprint)
    if after is not None:
        clauses.append(
            "(t.priority<? OR (t.priority=? AND "
            "(t.created_ns>? OR (t.created_ns=? AND t.task_id>?))))"
        )
        parameters.extend(
            (after.priority, after.priority, after.created_ns, after.created_ns, after.task_id)
        )
    parameters.append(limit + 1)
    unpublished_replacement = """event.to_state='superseded'
        AND event.actor_kind='system' AND event.actor_id='review-task-refresh'
        AND json_type(event.provenance_json,'$.replacement_task_id')='text'
        AND NOT EXISTS(
            SELECT 1 FROM review_tasks replacement
            JOIN review_task_source_publications published
              ON published.batch_id=replacement.batch_id
            WHERE replacement.task_id=json_extract(
                event.provenance_json,'$.replacement_task_id'
            )
        )"""
    current_events_sql = (
        """SELECT e.* FROM review_task_events e
        WHERE NOT EXISTS(
            SELECT 1 FROM review_task_events later
            WHERE later.task_id=e.task_id AND later.sequence>e.sequence
        )"""
        if not source_snapshot_as_published
        else (
            "SELECT event.* FROM review_task_events event WHERE NOT EXISTS("
            "SELECT 1 FROM review_task_events later WHERE later.task_id=event.task_id "
            "AND later.sequence>event.sequence) AND NOT ("
            + unpublished_replacement
            + ") UNION ALL SELECT previous.* FROM review_task_events event "
            "JOIN review_task_events previous ON previous.event_id=event.previous_event_id "
            "AND previous.task_id=event.task_id WHERE NOT EXISTS(SELECT 1 FROM "
            "review_task_events later WHERE later.task_id=event.task_id AND "
            "later.sequence>event.sequence) AND (" + unpublished_replacement + ")"
        )
    )
    sql = (
        """WITH current_events AS (
            """
        + current_events_sql
        + """
        )
        SELECT t.*,b.selector_signature AS batch_selector_signature,
        b.source_snapshot_json AS batch_source_snapshot_json,
        b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,"""
        + _CURRENT_EVENT_COLUMNS
        + ","
        + _EFFECTIVE_SOURCE_PUBLICATION_COLUMNS
        + " FROM review_tasks t LEFT JOIN current_events e ON e.task_id=t.task_id "
        "LEFT JOIN review_task_batches b ON b.batch_id=t.batch_id "
        + _EFFECTIVE_SOURCE_PUBLICATION_JOIN
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY t.priority DESC,t.created_ns,t.task_id LIMIT ?"
    )
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                rows = connection.execute(sql, parameters).fetchall()
                if source_snapshot_as_published:
                    records = tuple(_task_record_from_row(row) for row in rows)
                    current_by_id = {
                        record.task.task_id: record
                        for record in _validated_records_by_task_ids(
                            connection,
                            tuple(record.task.task_id for record in records),
                        )
                    }
                    for record in records:
                        current = current_by_id.get(record.task.task_id)
                        if (
                            current is None
                            or current.task != record.task
                            or current.source != record.source
                            or current.batch_id != record.batch_id
                        ):
                            raise ReviewTaskRepositoryError(
                                "published ReviewTask view disagrees with current immutable facts"
                            )
                else:
                    records = _validated_task_records(connection, rows)
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    items = records[:limit]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1].task
        next_cursor = ReviewTaskListCursor(last.priority, last.created_ns, last.task_id)
    return ReviewTaskRecordPage(items=items, next_cursor=next_cursor)


def lookup_review_task_version_heads(
    database: str | Path,
    logical_keys: Sequence[str],
    *,
    scope: str,
    task_type: str,
    cancellation_check: CancellationCheck | None = None,
) -> tuple[ReviewTaskVersionHead, ...]:
    """Resolve up to 100 logical predecessors/current events in one SQL query."""

    if isinstance(logical_keys, (str, bytes)):
        raise TypeError("logical_keys must be a sequence of strings")
    keys = tuple(logical_keys)
    if len(keys) > MAX_REVIEW_TASKS_PER_PAGE:
        raise ValueError(f"logical_keys cannot exceed {MAX_REVIEW_TASKS_PER_PAGE}")
    if len(set(keys)) != len(keys):
        raise ValueError("logical_keys cannot contain duplicates")
    for key in keys:
        _contracts._required_text(
            "logical_key", key, limit=_contracts.MAX_REVIEW_TASK_LOGICAL_KEY_CHARS
        )
    _contracts._required_text("scope", scope, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    _contracts._required_text("task_type", task_type, limit=_contracts.MAX_REVIEW_TASK_DOMAIN_CHARS)
    if not keys:
        return ()
    placeholders = ",".join("?" for _ in keys)
    sql = f"""WITH latest_tasks AS (
        SELECT t.* FROM review_tasks t
        WHERE t.logical_key IN ({placeholders})
          AND NOT EXISTS(
              SELECT 1 FROM review_tasks newer
              WHERE newer.logical_key=t.logical_key
                AND newer.task_version>t.task_version
          )
    ), current_events AS (
        SELECT e.* FROM review_task_events e
        JOIN latest_tasks t ON t.task_id=e.task_id
        WHERE NOT EXISTS(
            SELECT 1 FROM review_task_events later
            WHERE later.task_id=e.task_id AND later.sequence>e.sequence
        )
    )
    SELECT t.*,b.selector_signature AS batch_selector_signature,
    b.source_snapshot_json AS batch_source_snapshot_json,
    b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,
    {_CURRENT_EVENT_COLUMNS},{_EFFECTIVE_SOURCE_PUBLICATION_COLUMNS}
    FROM latest_tasks t LEFT JOIN current_events e ON e.task_id=t.task_id
    LEFT JOIN review_task_batches b ON b.batch_id=t.batch_id
    {_EFFECTIVE_SOURCE_PUBLICATION_JOIN}"""
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                rows = connection.execute(sql, keys).fetchall()
                records = _validated_task_records(connection, rows)
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    by_key: dict[str, ReviewTaskVersionHead] = {}
    for record in records:
        key = record.task.logical_key
        if record.task.scope != scope or record.task.task_type != task_type:
            raise ReviewTaskRepositoryError(
                "logical ReviewTask key is already owned by another scope/type"
            )
        by_key[key] = ReviewTaskVersionHead(
            logical_key=key,
            task_id=record.task.task_id,
            task_version=record.task.task_version,
            state=record.state,
            event_id=record.current_event.event_id,
            source_snapshot_fingerprint=record.source_snapshot_fingerprint,
            source_input_fingerprint=record.source.fingerprint,
            selector_signature=str(
                next(
                    row["batch_selector_signature"]
                    for row in rows
                    if str(row["task_id"]) == record.task.task_id
                )
            ),
            decision=record.current_event.decision,
        )
    return tuple(by_key[key] for key in keys if key in by_key)


def read_review_task(
    database: str | Path,
    task_id: str,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskRecord | None:
    """Read one exact current ReviewTask record without creating state."""

    _contracts._required_text("task_id", task_id)
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                rows = connection.execute(
                    """WITH current_events AS (
                        SELECT e.* FROM review_task_events e WHERE e.task_id=?
                        AND NOT EXISTS(
                            SELECT 1 FROM review_task_events later
                            WHERE later.task_id=e.task_id AND later.sequence>e.sequence
                        )
                    )
                    SELECT t.*,b.selector_signature AS batch_selector_signature,
                    b.source_snapshot_json AS batch_source_snapshot_json,
                    b.source_snapshot_fingerprint AS batch_source_snapshot_fingerprint,"""
                    + _CURRENT_EVENT_COLUMNS
                    + ","
                    + _EFFECTIVE_SOURCE_PUBLICATION_COLUMNS
                    + " FROM review_tasks t LEFT JOIN current_events e ON e.task_id=t.task_id "
                    "LEFT JOIN review_task_batches b ON b.batch_id=t.batch_id "
                    + _EFFECTIVE_SOURCE_PUBLICATION_JOIN
                    + " WHERE t.task_id=?",
                    (task_id, task_id),
                ).fetchall()
                records = _validated_task_records(connection, rows)
                result = None if not records else records[0]
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def read_review_task_history(
    database: str | Path,
    task_id: str,
    *,
    limit: int = MAX_REVIEW_TASK_READ_PAGE,
    cancellation_check: CancellationCheck | None = None,
) -> tuple[ReviewTaskEvent, ...]:
    """Read one bounded, validated append-only event history."""

    _contracts._required_text("task_id", task_id)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= (MAX_REVIEW_TASK_READ_PAGE)
    ):
        raise ValueError(f"limit must be between 1 and {MAX_REVIEW_TASK_READ_PAGE}")
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                records = _validated_records_by_task_ids(connection, (task_id,))
                if not records:
                    result: tuple[ReviewTaskEvent, ...] = ()
                else:
                    rows = connection.execute(
                        "SELECT " + _EVENT_COLUMNS + " FROM review_task_events "
                        "WHERE task_id=? ORDER BY sequence LIMIT ?",
                        (task_id, limit + 1),
                    ).fetchall()
                    if len(rows) > limit:
                        raise ReviewTaskRepositoryError(
                            "ReviewTask event history exceeds the bounded read limit"
                        )
                    result = tuple(_event_from_row(row) for row in rows)
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


def _event_matches_transition(
    event: ReviewTaskEvent,
    transition: ReviewTaskTransition,
) -> bool:
    return (
        event.event_id == transition.event_id
        and event.event_key == transition.event_key
        and event.task_id == transition.task_id
        and event.previous_event_id == transition.expected_event_id
        and event.from_state is transition.expected_state
        and event.to_state is transition.to_state
        and event.actor_kind is transition.actor_kind
        and event.actor_id == transition.actor_id
        and event.provenance == transition.provenance
        and event.decision == transition.decision
        and event.note == transition.note
        and event.observed_ns == transition.observed_ns
        and event.recorded_ns == transition.recorded_ns
    )


def append_review_task_event(
    database: str | Path,
    transition: ReviewTaskTransition,
    *,
    cancellation_check: CancellationCheck | None = None,
    timeout_seconds: float = 60.0,
    _fault_injector: _FaultInjector | None = None,
) -> ReviewTaskEventResult:
    """Append one idempotent task event after an exact owner-local CAS read."""

    if not isinstance(transition, ReviewTaskTransition):
        raise TypeError("transition must be a ReviewTaskTransition")
    if transition.to_state is ReviewTaskState.SUPERSEDED:
        raise ValueError("SUPERSEDED is reserved for receipt-backed repository transitions")
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(
        Path(database), readonly=False, timeout_seconds=timeout_seconds
    )
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN IMMEDIATE")
            try:
                _require_review_task_schema(connection)
                target = _validated_records_by_task_ids(connection, (transition.task_id,))[0]
                existing_rows = connection.execute(
                    "SELECT " + _EVENT_COLUMNS + " FROM review_task_events "
                    "WHERE event_id=? OR event_key=?",
                    (transition.event_id, transition.event_key),
                ).fetchall()
                if existing_rows:
                    if len(existing_rows) != 1:
                        raise ReviewTaskRepositoryError(
                            "review event id/key resolve to different facts"
                        )
                    existing = _event_from_row(existing_rows[0])
                    if not _event_matches_transition(existing, transition):
                        raise ReviewTaskRepositoryError(
                            "review event idempotency key changed payload"
                        )
                    connection.commit()
                    return ReviewTaskEventResult(existing, True)
                previous = target.current_event
                if (
                    previous.event_id != transition.expected_event_id
                    or previous.to_state is not transition.expected_state
                ):
                    raise ReviewTaskCASConflict("ReviewTask current event/state changed")
                if (
                    transition.observed_ns < target.task.created_ns
                    or transition.observed_ns < previous.observed_ns
                    or transition.recorded_ns <= previous.recorded_ns
                ):
                    raise ReviewTaskCASConflict(
                        "ReviewTask event time does not advance its current history"
                    )
                event = ReviewTaskEvent(
                    event_id=transition.event_id,
                    event_key=transition.event_key,
                    task_id=transition.task_id,
                    sequence=previous.sequence + 1,
                    previous_event_id=previous.event_id,
                    from_state=previous.to_state,
                    to_state=transition.to_state,
                    actor_kind=transition.actor_kind,
                    actor_id=transition.actor_id,
                    provenance=transition.provenance,
                    decision=transition.decision,
                    note=transition.note,
                    observed_ns=transition.observed_ns,
                    recorded_ns=transition.recorded_ns,
                )
                _insert_event(connection, event)
                _fault(_fault_injector, "after_event")
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return ReviewTaskEventResult(event, False)


def read_review_task_event_by_key(
    database: str | Path,
    event_key: str,
    *,
    cancellation_check: CancellationCheck | None = None,
) -> ReviewTaskEvent | None:
    """Read one exact idempotency event without creating state."""

    _contracts._required_text("event_key", event_key)
    bridge = SQLiteCancellationBridge(cancellation_check)
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        with sqlite_cancellation_scope(connection, bridge):
            _checkpoint(bridge)
            connection.execute("BEGIN")
            try:
                _require_review_task_schema(connection)
                rows = connection.execute(
                    "SELECT " + _EVENT_COLUMNS + " FROM review_task_events WHERE event_key=?",
                    (event_key,),
                ).fetchall()
                if len(rows) > 1:
                    raise ReviewTaskRepositoryError("ReviewTask event key is ambiguous")
                result = None if not rows else _event_from_row(rows[0])
                _checkpoint(bridge)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    finally:
        connection.close()
    return result


__all__ = (
    "MAX_REVIEW_TASK_SOURCE_AUDIT_BYTES",
    "MAX_REVIEW_TASK_SOURCE_CHAIN_BATCHES",
    "MAX_REVIEW_TASK_SOURCE_CHAIN_MEMBERSHIPS",
    "MAX_REVIEW_TASK_SOURCE_PUBLICATION_HEADS",
    "ReviewTaskCASConflict",
    "ReviewTaskRepositoryError",
    "ReviewTaskSourcePublicationAudit",
    "append_review_task_event",
    "audit_latest_review_task_source_publications_from_connection",
    "find_review_task_scan_progress",
    "has_review_task_scan_history",
    "list_current_review_tasks",
    "lookup_review_task_version_heads",
    "publish_review_task_page",
    "read_current_review_task_source_publication",
    "read_latest_complete_review_task_progress",
    "read_review_task",
    "read_review_task_event_by_key",
    "read_review_task_history",
    "read_review_task_progress",
    "read_review_task_publication",
    "validate_latest_review_task_source_publications_from_connection",
)
