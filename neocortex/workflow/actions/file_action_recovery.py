"""Read-only, idempotent reconciliation of uncertain filesystem actions."""
# region [00] Contexto del módulo
# Módulo: neocortex/file_action_recovery.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.framework_schema import SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION
# endregion [01]

# region [02] Implementación


RECOVERY_BATCH_LIMIT = 1000
FILE_ACTION_RECONCILER_SIGNATURE = "file-action-reconciler-v1"
_RECOVERABLE_STATUSES = ("applying", "recovery_required")


def _reject_future_framework_schema(connection: sqlite3.Connection) -> None:
    """Refuse semantics newer than this read-only reconciler understands."""

    metadata = connection.execute("SELECT type FROM sqlite_master WHERE name='metadata'").fetchone()
    if metadata is None:
        return
    if tuple(metadata) != ("table",):
        raise sqlite3.DatabaseError("framework metadata is not a table")
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise sqlite3.DatabaseError("framework metadata has no unique schema_version")
    raw = str(rows[0][0])
    try:
        version = int(raw)
    except ValueError as exc:
        raise sqlite3.DatabaseError("framework schema_version is not an integer") from exc
    if raw != str(version):
        raise sqlite3.DatabaseError("framework schema_version is not canonical")
    if version > FRAMEWORK_SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"framework schema {version} is newer than supported schema {FRAMEWORK_SCHEMA_VERSION}"
        )


@dataclass(frozen=True, slots=True)
class FileActionReconciliation:
    """One deterministic classification; it never authorizes a mutation."""

    action_id: int
    run_id: int
    idempotency_key: str | None
    action_type: str
    source_path: str
    target_path: str | None
    recorded_status: str
    reconciler_signature: str
    classification: str
    recommendation: str
    detail: str


@dataclass(frozen=True, slots=True)
class _RecordedAction:
    action_id: int
    run_id: int
    idempotency_key: str | None
    action_type: str
    source_path: str
    target_path: str | None
    recorded_status: str


def _result(
    action: _RecordedAction,
    *,
    classification: str,
    recommendation: str,
    detail: str,
) -> FileActionReconciliation:
    return FileActionReconciliation(
        action_id=action.action_id,
        run_id=action.run_id,
        idempotency_key=action.idempotency_key,
        action_type=action.action_type,
        source_path=action.source_path,
        target_path=action.target_path,
        recorded_status=action.recorded_status,
        reconciler_signature=FILE_ACTION_RECONCILER_SIGNATURE,
        classification=classification,
        recommendation=recommendation,
        detail=detail,
    )


def expected_identity_json(
    snapshot: FileSnapshot,
    *,
    source_path: str,
    target_path: str | None,
) -> str:
    """Serialize the physical identity expected at the mutation frontier."""

    return json.dumps(
        {
            "schema_version": 1,
            "source": {
                "birthtime_ns": snapshot.birthtime_ns,
                "file_id": f"{snapshot.file_id:x}",
                "mtime_ns": snapshot.mtime_ns,
                "path": source_path,
                "size": snapshot.size,
                "volume_id": f"{snapshot.volume_id:x}",
            },
            "target_path": target_path,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def effect_receipt_json(
    *,
    operation: str,
    source_path: str,
    target_path: str | None,
    target_snapshot: FileSnapshot | None = None,
) -> str:
    """Serialize a successful syscall return and its immediate observation."""

    target_identity = None
    if target_snapshot is not None:
        target_identity = {
            "birthtime_ns": target_snapshot.birthtime_ns,
            "file_id": f"{target_snapshot.file_id:x}",
            "mtime_ns": target_snapshot.mtime_ns,
            "path": target_snapshot.path,
            "size": target_snapshot.size,
            "volume_id": f"{target_snapshot.volume_id:x}",
        }
    return json.dumps(
        {
            "operation": operation,
            "receipt_type": "successful_return_and_observation",
            "schema_version": 1,
            "source_absent": True,
            "source_path": source_path,
            "target_identity": target_identity,
            "target_path": target_path,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def list_file_action_reconciliations(
    database_path: Path,
    *,
    limit: int = 100,
    after_action_id: int = 0,
    run_id: int | None = None,
) -> tuple[FileActionReconciliation, ...]:
    """Classify a bounded keyset page without creating or migrating SQLite state."""

    if limit < 1 or limit > RECOVERY_BATCH_LIMIT:
        raise ValueError(f"recovery limit must be between 1 and {RECOVERY_BATCH_LIMIT}")
    if after_action_id < 0:
        raise ValueError("after action identifier cannot be negative")
    if run_id is not None and run_id < 1:
        raise ValueError("recovery run identifier must be positive")
    connection = connect_existing_framework(database_path, readonly=True, timeout_seconds=10)
    try:
        _reject_future_framework_schema(connection)
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(file_actions)")
        }
        required = {
            "action_id",
            "run_id",
            "action_type",
            "source_path",
            "target_path",
            "status",
        }
        if not required.issubset(columns):
            raise sqlite3.DatabaseError("framework state has no compatible file_actions table")
        optional = {
            name: name if name in columns else f"NULL AS {name}"
            for name in (
                "idempotency_key",
                "expected_identity_json",
                "effect_receipt_json",
            )
        }
        clauses = ["action_id>?", "status IN ('applying','recovery_required')"]
        parameters: list[object] = [after_action_id]
        if run_id is not None:
            clauses.append("run_id=?")
            parameters.append(run_id)
        rows = connection.execute(
            f"""SELECT action_id,run_id,action_type,source_path,target_path,status,
            {optional["idempotency_key"]},{optional["expected_identity_json"]},
            {optional["effect_receipt_json"]} FROM file_actions
            WHERE {" AND ".join(clauses)} ORDER BY action_id LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        return tuple(_classify_row(row) for row in rows)
    finally:
        connection.close()


def _classify_row(row: sqlite3.Row) -> FileActionReconciliation:
    action = _RecordedAction(
        action_id=int(row["action_id"]),
        run_id=int(row["run_id"]),
        idempotency_key=(None if row["idempotency_key"] is None else str(row["idempotency_key"])),
        action_type=str(row["action_type"]),
        source_path=str(row["source_path"]),
        target_path=None if row["target_path"] is None else str(row["target_path"]),
        recorded_status=str(row["status"]),
    )
    try:
        expected = _parse_expected_identity(
            row["expected_identity_json"],
            source_path=action.source_path,
            target_path=action.target_path,
        )
    except ValueError as exc:
        return _result(
            action,
            classification="impossible_to_check",
            recommendation="preserve_evidence_and_review_manually",
            detail=str(exc),
        )

    source_state = _observe_path(action.source_path, expected)
    if source_state[0] == "error":
        return _result(
            action,
            classification="impossible_to_check",
            recommendation="preserve_evidence_and_review_manually",
            detail=f"source observation failed: {source_state[1]}",
        )
    if action.action_type.startswith("trash_"):
        return _classify_trash(action, source_state, row["effect_receipt_json"])
    if action.target_path is None:
        return _result(
            action,
            classification="impossible_to_check",
            recommendation="preserve_evidence_and_review_manually",
            detail="non-trash action has no recorded target path",
        )
    target_state = _observe_path(action.target_path, expected)
    if target_state[0] == "error":
        return _result(
            action,
            classification="impossible_to_check",
            recommendation="preserve_evidence_and_review_manually",
            detail=f"target observation failed: {target_state[1]}",
        )
    return _classify_move(action, source_state, target_state)


def _parse_expected_identity(
    raw: object,
    *,
    source_path: str,
    target_path: str | None,
) -> tuple[int, int]:
    if raw is None:
        raise ValueError("legacy action has no expected physical identity")
    try:
        document = json.loads(str(raw))
        source = document["source"]
        version = int(document["schema_version"])
        volume_id = int(str(source["volume_id"]), 16)
        file_id = int(str(source["file_id"]), 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("expected physical identity evidence is malformed") from exc
    if version != 1 or volume_id < 0 or file_id < 0:
        raise ValueError("expected physical identity evidence is unsupported")
    if _path_key(str(source.get("path", ""))) != _path_key(source_path):
        raise ValueError("expected identity source path conflicts with the action")
    recorded_target = document.get("target_path")
    if (None if recorded_target is None else _path_key(str(recorded_target))) != (
        None if target_path is None else _path_key(target_path)
    ):
        raise ValueError("expected identity target path conflicts with the action")
    return volume_id, file_id


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _observe_path(path: str, expected: tuple[int, int]) -> tuple[str, str | None]:
    try:
        current = snapshot_path(path)
    except FileNotFoundError:
        return "missing", None
    except OSError as exc:
        return "error", f"{type(exc).__name__}: {exc}"
    if current.identity == expected:
        return "expected", None
    return "different", f"observed identity={current.volume_id:x}:{current.file_id:x}"


def _classify_trash(
    action: _RecordedAction,
    source_state: tuple[str, str | None],
    raw_receipt: object,
) -> FileActionReconciliation:
    if source_state[0] == "expected":
        return _result(
            action,
            classification="not_performed",
            recommendation="review_before_new_authorized_attempt",
            detail="the expected source identity remains at the source path",
        )
    if source_state[0] == "different":
        return _result(
            action,
            classification="ambiguous",
            recommendation="preserve_evidence_and_review_manually",
            detail=f"source path now names another object; {source_state[1]}",
        )
    if _valid_success_receipt(raw_receipt, operation="trash", action=action):
        return _result(
            action,
            classification="confirmed",
            recommendation="confirm_action_record",
            detail="source is absent and a successful trash receipt was recorded",
        )
    return _result(
        action,
        classification="ambiguous",
        recommendation="preserve_evidence_and_review_manually",
        detail=(
            "source is absent but the Recycle Bin receipt is missing or does not match this action"
        ),
    )


def _classify_move(
    action: _RecordedAction,
    source_state: tuple[str, str | None],
    target_state: tuple[str, str | None],
) -> FileActionReconciliation:
    if source_state[0] == "missing" and target_state[0] == "expected":
        return _result(
            action,
            classification="confirmed",
            recommendation="confirm_action_record",
            detail="source is absent and target retains the expected identity",
        )
    if source_state[0] == "expected" and target_state[0] == "missing":
        return _result(
            action,
            classification="not_performed",
            recommendation="review_before_new_authorized_attempt",
            detail="source retains the expected identity and target is absent",
        )
    details = (
        f"source={source_state[0]}; target={target_state[0]}"
        + (f"; source_detail={source_state[1]}" if source_state[1] else "")
        + (f"; target_detail={target_state[1]}" if target_state[1] else "")
    )
    return _result(
        action,
        classification="ambiguous",
        recommendation="preserve_evidence_and_review_manually",
        detail=details,
    )


def _valid_success_receipt(
    raw: object,
    *,
    operation: str,
    action: _RecordedAction,
) -> bool:
    if raw is None:
        return False
    try:
        receipt = json.loads(str(raw))
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(receipt, dict)
        and receipt.get("schema_version") == 1
        and receipt.get("operation") == operation
        and receipt.get("receipt_type") == "successful_return_and_observation"
        and receipt.get("source_absent") is True
        and _path_key(str(receipt.get("source_path", ""))) == _path_key(action.source_path)
        and (None if receipt.get("target_path") is None else _path_key(str(receipt["target_path"])))
        == (None if action.target_path is None else _path_key(action.target_path))
    )


__all__ = [
    "FILE_ACTION_RECONCILER_SIGNATURE",
    "RECOVERY_BATCH_LIMIT",
    "FileActionReconciliation",
    "effect_receipt_json",
    "expected_identity_json",
    "list_file_action_reconciliations",
]
# endregion [02]
