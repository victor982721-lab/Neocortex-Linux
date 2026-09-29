"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import FULL_ALGORITHM
from neocortex.platform.policy import sqlite_path_collation
from neocortex.runtime.models import ActionSummary
from neocortex.persistence.framework_state_types import _FrameworkStateOwner
from neocortex.persistence.framework_state_common import (
    FileActionSpec,
    begin_file_actions,
    confirm_file_actions_applied,
    finish_file_actions,
    mark_file_actions_applying,
)
from neocortex.persistence.sqlite_cancellation import (
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from neocortex.safety.kio_trash import (
    is_metadata_binding,
    metadata_binding,
    trash_receipt_paths,
)
from neocortex.workflow.actions.file_action_recovery import FileActionReconciliation
from neocortex.workflow.actions.file_action_reconciliation_store import (
    RecordedFileActionReconciliation,
    record_file_action_reconciliation,
)


_PATH_COLLATION = sqlite_path_collation()
_TRASH_REPLAY_ACTION_TYPES = (
    "trash_artifact",
    "trash_duplicate",
    "trash_redlist",
    "trash_empty_file",
)


def _escaped_like_prefix(value: str) -> str:
    """Escape a root before using it in a bounded SQLite LIKE predicate."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _canonical_hex(value: object, *, label: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value or value != value.casefold():
        raise ValueError(f"{label} is not canonical hexadecimal text")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} is not hexadecimal text")
    parsed = int(value, 16)
    if f"{parsed:x}" != value:
        raise ValueError(f"{label} is not canonical hexadecimal text")
    return value, parsed


def _validate_trash_replay_identity(
    identity: Mapping[str, object],
    *,
    source_path: str,
    expected_device: int,
    expected_inode: int,
    expected_size: int,
    expected_mtime_ns: int,
) -> tuple[FileSnapshot, dict[str, object]]:
    """Decode the owner-written expected identity without observing the corpus."""

    if identity.get("schema_version") != 1:
        raise ValueError("file action expected identity schema is unsupported")
    source = identity.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("file action expected identity has no source")
    raw_path = source.get("path")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
        or "\x00" in raw_path
        or _path_key(raw_path) != _path_key(source_path)
    ):
        raise ValueError("file action expected identity source path differs")
    if identity.get("target_path") is not None:
        raise ValueError("trash action expected identity has a target")
    _, device = _canonical_hex(source.get("volume_id"), label="source volume id")
    _, inode = _canonical_hex(source.get("file_id"), label="source file id")
    size = source.get("size")
    mtime_ns = source.get("mtime_ns")
    birthtime_ns = source.get("birthtime_ns")
    if (
        type(size) is not int
        or type(mtime_ns) is not int
        or type(birthtime_ns) is not int
        or size < 0
        or mtime_ns < 0
        or birthtime_ns < -1
    ):
        raise ValueError("file action expected identity metadata is invalid")
    if (
        device != expected_device
        or inode != expected_inode
        or size != expected_size
        or mtime_ns != expected_mtime_ns
    ):
        raise ValueError("file action expected identity differs from the attachment")
    snapshot = FileSnapshot(
        str(raw_path), device, inode, size, mtime_ns, birthtime_ns
    )
    normalized = {
        "path": str(raw_path),
        "device": device,
        "inode": inode,
        "size": size,
        "mtime_ns": mtime_ns,
        "birthtime_ns": birthtime_ns,
        # FileSnapshot does not carry link count.  The effect owner admitted a
        # regular source and the receipt validator enforces the Trash object;
        # this field is a compatibility marker, not an independent authority.
        "nlink": 1,
    }
    return snapshot, normalized


def _validate_trash_replay_row(
    row: Mapping[str, object],
    *,
    root: str,
    source_sha256: str,
    expected_device: int,
    expected_inode: int,
    expected_size: int,
    expected_mtime_ns: int,
) -> dict[str, object] | None:
    """Validate one applied action and return a receipt-bound replay fact."""

    action_type = row.get("action_type")
    if action_type not in _TRASH_REPLAY_ACTION_TYPES or row.get("status") != "applied":
        return None
    if action_type == "trash_empty_file" and expected_size != 0:
        return None
    source_path = row.get("source_path")
    if not isinstance(source_path, str) or not Path(source_path).is_absolute():
        return None
    normalized_root = _path_key(root)
    if _path_key(source_path) != normalized_root and not _path_key(source_path).startswith(
        normalized_root.rstrip(os.sep) + os.sep
    ):
        return None
    expected_raw = row.get("expected_identity_json")
    receipt_raw = row.get("effect_receipt_json")
    if not isinstance(expected_raw, str) or not isinstance(receipt_raw, str):
        return None
    if len(expected_raw.encode("utf-8")) > 65_536 or len(receipt_raw.encode("utf-8")) > 65_536:
        return None
    try:
        expected_value = json.loads(expected_raw)
        receipt = json.loads(receipt_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(expected_value, Mapping) or not isinstance(receipt, Mapping):
        return None
    try:
        snapshot, source_identity = _validate_trash_replay_identity(
            expected_value,
            source_path=source_path,
            expected_device=expected_device,
            expected_inode=expected_inode,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
        )
    except (TypeError, ValueError):
        return None
    source_digest = receipt.get("source_digest")
    if not isinstance(source_digest, str):
        return None
    if is_metadata_binding(source_digest):
        if metadata_binding(snapshot) != source_digest:
            return None
    elif source_digest == f"{FULL_ALGORITHM}:{source_sha256}":
        pass
    else:
        return None
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_type") != "successful_return_and_observation"
        or receipt.get("operation") != "trash"
        or receipt.get("source_absent") is not True
        or receipt.get("target_path") is not None
        or receipt.get("source_path") != source_path
    ):
        return None
    try:
        # This is deliberately structural.  The effect owner already called
        # verify_trash_receipt_evidence before recording status=applied; this
        # reader must not re-open or mutate the user's Trash while replaying an
        # EML child whose source has since disappeared.
        trash_receipt_paths(receipt.get("trash"), snapshot, source_digest)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    action_id = row.get("action_id")
    run_id = row.get("run_id")
    if type(action_id) is not int or action_id < 1 or type(run_id) is not int or run_id < 1:
        return None
    return {
        "schema": "neocortex.file-action-consumption/v1",
        "authority": "framework.file_actions",
        "status": "applied",
        "published": True,
        "trashed": True,
        "action_id": action_id,
        "run_id": run_id,
        "action_type": action_type,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_digest": source_digest,
        "source_identity": source_identity,
        "expected_identity": dict(expected_value),
        "receipt": dict(receipt),
        "effect_receipt": dict(receipt),
        "effect_receipt_json": receipt_raw,
        "root": root,
    }


class FrameworkStateActionsMixin(_FrameworkStateOwner):
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def begin_file_action(
        self,
        run_id: int,
        action_type: str,
        source_path: str,
        target_path: str | None,
        detected_mime: str | None,
        evidence: str | None,
        apply_requested: bool,
    ) -> int:
        return self.begin_file_actions(
            run_id,
            (
                (
                    action_type,
                    source_path,
                    target_path,
                    detected_mime,
                    evidence,
                    apply_requested,
                ),
            ),
        )[0]

    def begin_file_actions(
        self,
        run_id: int,
        actions: Iterable[FileActionSpec],
    ) -> list[int]:
        """Insert a bounded action batch in one transaction."""

        return begin_file_actions(self._connection, run_id, actions)

    def finish_file_action(self, action_id: int, status: str, detail: str | None = None) -> None:
        self.finish_file_actions((action_id,), status, detail)

    def finish_file_actions(
        self,
        action_ids: Iterable[int],
        status: str,
        detail: str | None = None,
    ) -> None:
        """Complete a bounded action batch in one transaction."""

        finish_file_actions(self._connection, action_ids, status, detail)

    def mark_file_actions_applying(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Persist expected identities before any filesystem syscall."""

        mark_file_actions_applying(self._connection, actions)

    def confirm_file_actions_applied(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Store successful syscall receipts through an applying-state CAS."""

        confirm_file_actions_applied(self._connection, actions)

    def require_file_action_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
    ) -> None:
        """Preserve an uncertain post-frontier effect without retrying it."""

        finish_file_actions(
            self._connection,
            action_ids,
            "recovery_required",
            detail,
        )

    def record_file_action_reconciliation(
        self,
        reconciliation: FileActionReconciliation,
        *,
        actor: str,
        provenance_json: str,
        expected_previous_event_id: int | None,
        observed_ns: int | None = None,
    ) -> RecordedFileActionReconciliation:
        """Append read-only observation evidence; never retry the action."""

        return record_file_action_reconciliation(
            self._connection,
            reconciliation,
            actor=actor,
            provenance_json=provenance_json,
            expected_previous_event_id=expected_previous_event_id,
            observed_ns=observed_ns,
        )

    def read_historical_trash_consumption(
        self,
        root: Path,
        *,
        source_sha256: str,
        child_identity: Mapping[str, object] | None = None,
        child_device: int | None = None,
        child_inode: int | None = None,
        child_size: int | None = None,
        child_mtime_ns: int | None = None,
        max_candidates: int = 64,
        checkpoint: Callable[[], None] | None = None,
        sql_checkpoint: Callable[[], None] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Read owner-validated Trash receipts for one consumed EML child.

        The lookup is intentionally separate from the legacy ZIP lifecycle
        projection.  It is bounded in SQL by the source root, physical child
        identity, action kind, and terminal ``applied`` status; the returned
        receipt is accepted only after revalidating the owner-written expected
        identity and the original effect receipt.  No corpus or Trash path is
        opened by this method.
        """

        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("root must be an absolute Path")
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in source_sha256)
        ):
            raise ValueError("source_sha256 must be a SHA-256 string")
        if type(max_candidates) is not int or not 1 <= max_candidates <= 256:
            raise ValueError("max_candidates must be between 1 and 256")
        if child_identity is None:
            child_identity = {
                "device": child_device,
                "inode": child_inode,
                "size": child_size,
                "mtime_ns": child_mtime_ns,
            }
        if not isinstance(child_identity, Mapping):
            raise ValueError("child_identity must be a mapping")
        identity_values: dict[str, int] = {}
        for field in ("device", "inode", "size", "mtime_ns"):
            value = child_identity.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"child identity {field} is invalid")
            identity_values[field] = value
        if callable(checkpoint):
            checkpoint()
        normalized_root = str(Path(os.path.abspath(os.path.realpath(root))))
        root_prefix = _escaped_like_prefix(normalized_root.rstrip(os.sep) + os.sep) + "%"
        volume_id = f"{identity_values['device']:x}"
        file_id = f"{identity_values['inode']:x}"
        parameters: tuple[object, ...] = (
            *_TRASH_REPLAY_ACTION_TYPES,
            normalized_root,
            root_prefix,
            volume_id,
            file_id,
            identity_values["size"],
            identity_values["mtime_ns"],
            max_candidates,
        )
        rows: list[dict[str, object]] = []
        bridge = SQLiteCancellationBridge(sql_checkpoint if sql_checkpoint is not None else checkpoint)
        with sqlite_cancellation_scope(self._connection, bridge, instructions=100):
            cursor = self._connection.execute(
                f"""SELECT action_id,run_id,action_type,status,source_path,
                expected_identity_json,effect_receipt_json
                FROM file_actions
                WHERE status='applied'
                AND action_type IN (?,?,?,?)
                AND (source_path=? COLLATE {_PATH_COLLATION}
                    OR source_path LIKE ? ESCAPE '\\' COLLATE {_PATH_COLLATION})
                AND json_valid(expected_identity_json)
                AND json_extract(expected_identity_json,'$.schema_version')=1
                AND json_extract(expected_identity_json,'$.source.volume_id')=?
                AND json_extract(expected_identity_json,'$.source.file_id')=?
                AND json_extract(expected_identity_json,'$.source.size')=?
                AND json_extract(expected_identity_json,'$.source.mtime_ns')=?
                ORDER BY action_id DESC LIMIT ?""",
                parameters,
            )
            for row in cursor:
                if callable(checkpoint):
                    checkpoint()
                row_mapping = {
                    key: row[key]
                    for key in row.keys()
                } if hasattr(row, "keys") else {
                    "action_id": row[0],
                    "run_id": row[1],
                    "action_type": row[2],
                    "status": row[3],
                    "source_path": row[4],
                    "expected_identity_json": row[5],
                    "effect_receipt_json": row[6],
                }
                candidate = _validate_trash_replay_row(
                    row_mapping,
                    root=normalized_root,
                    source_sha256=source_sha256.lower(),
                    expected_device=identity_values["device"],
                    expected_inode=identity_values["inode"],
                    expected_size=identity_values["size"],
                    expected_mtime_ns=identity_values["mtime_ns"],
                )
                if candidate is not None:
                    rows.append(candidate)
                    if len(rows) >= max_candidates:
                        break
        if callable(checkpoint):
            checkpoint()
        return tuple(rows)

    def read_historical_file_action_consumption(
        self,
        root: Path,
        *,
        source_sha256: str,
        child_identity: Mapping[str, object] | None = None,
        child_device: int | None = None,
        child_inode: int | None = None,
        child_size: int | None = None,
        child_mtime_ns: int | None = None,
        max_candidates: int = 64,
        checkpoint: Callable[[], None] | None = None,
        sql_checkpoint: Callable[[], None] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Compatibility name for the owner-scoped Trash replay projection."""

        return self.read_historical_trash_consumption(
            root,
            source_sha256=source_sha256,
            child_identity=child_identity,
            child_device=child_device,
            child_inode=child_inode,
            child_size=child_size,
            child_mtime_ns=child_mtime_ns,
            max_candidates=max_candidates,
            checkpoint=checkpoint,
            sql_checkpoint=sql_checkpoint,
        )

    def store_action_summary(self, run_id: int, summary: ActionSummary) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO run_actions("
                "run_id,apply_actions,duplicate_candidates,duplicates_trashed,"
                "duplicate_skips,files_checked,types_detected,extensions_matching,"
                "unknown_types,type_cache_hits,type_cache_misses,type_cache_pruned,"
                "stale_inventory,"
                "rename_candidates,files_renamed,rename_skips,"
                "empty_directory_candidates,empty_directories_trashed,"
                "empty_directory_skips,errors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    int(summary.apply_actions),
                    summary.duplicate_candidates,
                    summary.duplicates_trashed,
                    summary.duplicate_skips,
                    summary.files_checked,
                    summary.types_detected,
                    summary.extensions_matching,
                    summary.unknown_types,
                    summary.type_cache_hits,
                    summary.type_cache_misses,
                    summary.type_cache_pruned,
                    summary.stale_inventory,
                    summary.rename_candidates,
                    summary.files_renamed,
                    summary.rename_skips,
                    summary.empty_directory_candidates,
                    summary.empty_directories_trashed,
                    summary.empty_directory_skips,
                    summary.errors,
                ),
            )

__all__ = ["FrameworkStateActionsMixin"]
