"""Receipt-bound, no-replace restoration of framework Trash effects.

Restoration is a separate explicit effect.  It creates its own
``file_actions`` intent before moving anything, consumes only the original
Trash receipt, and never exposes a restore mutation through MCP.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    FULL_ALGORITHM,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.workflow.actions.action_policy import validate_mutation_path
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from neocortex.persistence.sqlite_immutable import SQLiteReadSession, preferred_sqlite_read_mode
from neocortex.safety.kio_trash import (
    is_metadata_binding,
    metadata_binding,
    trash_receipt_paths,
    verify_trash_receipt_evidence,
)
# Restore keeps its receipt lifecycle, but shares the neutral descriptor
# traversal and no-replace syscall with forward mutations.
from neocortex.workflow.mutations import _open_parent_dirfd, _renameat2_noreplace


CURATION_RESTORE_SCHEMA = "neocortex.curation-restore/v1"
RESTORE_INTENT_SCHEMA = "neocortex.curation-restore-intent/v1"
RestoreStatus = Literal["restored", "already_restored", "blocked", "recovery_required"]


@dataclass(slots=True)
class _ReadonlyState:
    path: Path
    _connection: sqlite3.Connection


@contextmanager
def _readonly_state(database: Path):
    mode = preferred_sqlite_read_mode(database)
    with SQLiteReadSession(database, mode=mode, timeout_seconds=30.0) as connection:
        yield _ReadonlyState(database, connection)


@dataclass(frozen=True, slots=True)
class _RestoreEffect:
    action: str
    source: FileSnapshot
    source_digest: str
    target_path: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreCandidate:
    action_id: int
    original_action_id: int
    effect: _RestoreEffect
    root: Path
    root_snapshot: FileSnapshot
    trash_path: Path
    info_path: Path
    trash_root: Path
    trash_root_snapshot: FileSnapshot
    trash_volume_id: int
    trash_file_id: int


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    action_id: int
    status: RestoreStatus
    reason: str
    detail: str | None = None
    receipt_json: str | None = None
    idempotent: bool = False

    def __post_init__(self) -> None:
        if type(self.action_id) is not int or self.action_id < 1:
            raise ValueError("restore outcome action_id must be positive")
        if not isinstance(self.status, str) or self.status not in {
            "restored",
            "already_restored",
            "blocked",
            "recovery_required",
        }:
            raise ValueError("restore outcome status is unsupported")
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or self.reason.strip() != self.reason
        ):
            raise ValueError("restore outcome reason must be trimmed non-empty text")
        if len(self.reason.encode("utf-8")) > 512:
            raise ValueError("restore outcome reason is too long")
        if self.detail is not None and (
            not isinstance(self.detail, str) or len(self.detail.encode("utf-8")) > 4_096
        ):
            raise ValueError("restore outcome detail is invalid")
        if self.receipt_json is not None and (
            not isinstance(self.receipt_json, str)
            or len(self.receipt_json.encode("utf-8")) > 65_536
        ):
            raise ValueError("restore outcome receipt is invalid")
        if self.status == "restored" and not self.receipt_json:
            raise ValueError("restored outcome requires a receipt")
        if not isinstance(self.idempotent, bool):
            raise ValueError("restore outcome idempotent must be boolean")


class RestoreBackend(Protocol):
    name: str

    def restore(self, candidate: RestoreCandidate) -> RestoreOutcome:
        """Restore one verified trash item without replacing a destination."""

        ...


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(snapshot: FileSnapshot) -> str:
    return f"{FULL_ALGORITHM}:" + full_fingerprint(snapshot).hex()


def _binding_matches_snapshot(snapshot: FileSnapshot, binding: str) -> bool:
    if is_metadata_binding(binding):
        return metadata_binding(snapshot) == binding
    return _digest(snapshot) == binding


def _snapshot_identity(snapshot: FileSnapshot) -> tuple[int, int, int]:
    return snapshot.volume_id, snapshot.file_id, snapshot.birthtime_ns


def _strict_path_under(root: Path, path: Path, *, role: str) -> tuple[str, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{role} escapes its configured root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{role} has an unsafe relative path")
    return relative.parts


def _open_dirfd(root: Path, path: Path, *, role: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, flags)
    try:
        parts = _strict_path_under(root, path, role=role)
        return _open_parent_dirfd(root_fd, parts)
    finally:
        os.close(root_fd)


def _rename_noreplace(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    destination_root: Path,
    expected_source: FileSnapshot | None = None,
) -> None:
    source_fd: int | None = None
    destination_fd: int | None = None
    source_file_fd: int | None = None
    try:
        source_fd, source_name = _open_dirfd(source_root, source, role="trash source")
        destination_fd, destination_name = _open_dirfd(
            destination_root,
            destination,
            role="restore destination",
        )
        if expected_source is not None:
            source_file_fd = os.open(
                source_name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=source_fd,
            )
            source_metadata = os.fstat(source_file_fd)
            if (
                not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_nlink != 1
                or not stat_matches_snapshot(expected_source, source_metadata)
            ):
                raise FileChangedError("restore source changed before rename")
        _renameat2_noreplace(
            source_fd,
            source_name,
            destination_fd,
            destination_name,
        )
    finally:
        for descriptor in (source_file_fd, source_fd, destination_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_confirmation_token(action_id: int, receipt_json: str) -> str:
    """Return the exact human confirmation token for one original action."""

    if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id < 1:
        raise ValueError("action_id must be positive")
    if not isinstance(receipt_json, str) or not receipt_json:
        raise ValueError("receipt_json must be non-empty")
    digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
    return f"RESTORE_CURATION:{action_id}:{digest}"


def _restore_receipt_valid(
    receipt_json: str,
    *,
    candidate: RestoreCandidate,
) -> bool:
    try:
        receipt = json.loads(receipt_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt, dict):
        return False
    return bool(
        receipt.get("schema_version") == 1
        and receipt.get("receipt_type") == "curation_restore/v1"
        and receipt.get("operation") == "restore"
        and receipt.get("source_restored") is True
        and receipt.get("action_id") == candidate.action_id
        and receipt.get("original_action_id") == candidate.original_action_id
        and receipt.get("info_removed") is True
        and receipt.get("source_path") == candidate.effect.source.path
        and receipt.get("trash_path") == str(candidate.trash_path)
        and receipt.get("info_path") == str(candidate.info_path)
        and receipt.get("digest") == candidate.effect.source_digest
    )


def _original_receipt_parts(
    receipt_json: str,
    *,
    action_id: int,
    source: FileSnapshot,
    source_digest: str,
) -> tuple[Path, Path, Path, FileSnapshot, int, int]:
    """Validate one action receipt and return its fixture Trash paths."""

    try:
        receipt = json.loads(receipt_json)
        if not isinstance(receipt, dict):
            raise ValueError("original trash receipt is not an object")
        trash = receipt["trash"]
        trash_root, trash_path, info_path = trash_receipt_paths(trash, source, source_digest)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("original trash receipt is malformed") from exc
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_type") != "successful_return_and_observation"
        or receipt.get("operation") != "trash"
        or receipt.get("source_absent") is not True
        or receipt.get("source_path") != source.path
        or receipt.get("target_path") is not None
        or receipt.get("source_digest") != source_digest
    ):
        raise ValueError(f"trash receipt for action {action_id} is not bound to its source")
    try:
        root_snapshot = snapshot_path(trash_root)
    except OSError as exc:
        raise ValueError("trash root cannot be snapshotted") from exc
    return (
        trash_root,
        trash_path,
        info_path,
        root_snapshot,
        source.volume_id,
        source.file_id,
    )


def _verify_trash_candidate(candidate: RestoreCandidate) -> FileSnapshot:
    effect = candidate.effect
    return verify_trash_receipt_evidence(
        {
            "trash_root": str(candidate.trash_root),
            "trash_path": str(candidate.trash_path),
            "info_path": str(candidate.info_path),
            "volume_id": f"{candidate.trash_volume_id:x}",
            "file_id": f"{candidate.trash_file_id:x}",
            "size": effect.source.size,
            "digest": effect.source_digest,
        },
        effect.source,
        effect.source_digest,
    )


def _verify_restored_source(candidate: RestoreCandidate) -> None:
    path = Path(candidate.effect.source.path)
    validate_mutation_path(candidate.root, path, role="restored source")
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("restored source is not a regular file")
    if metadata.st_nlink != 1:
        raise ValueError("restored source has additional hard links")
    restored = snapshot_path(path)
    if (
        restored != candidate.effect.source
        or not stat_matches_snapshot(candidate.effect.source, metadata)
        or not _binding_matches_snapshot(restored, candidate.effect.source_digest)
    ):
        raise ValueError("restored source identity or digest differs")


def _verify_restore_postconditions(candidate: RestoreCandidate) -> None:
    _verify_root(candidate)
    _verify_restored_source(candidate)
    for path, role in (
        (candidate.trash_path, "restored trash file"),
        (candidate.info_path, "restored trash info"),
    ):
        validate_mutation_path(candidate.trash_root, path, role=role, allow_missing_leaf=True)
        if os.path.lexists(path):
            raise ValueError("restore receipt conflicts with retained Trash evidence")


def _verify_root(candidate: RestoreCandidate) -> None:
    expected = candidate.root_snapshot
    metadata = os.lstat(candidate.root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("restore root is not a real directory")
    current = snapshot_path(candidate.root)
    if _snapshot_identity(current) != (
        expected.volume_id,
        expected.file_id,
        expected.birthtime_ns,
    ):
        raise ValueError("restore root identity changed")


class PosixRestoreBackend:
    """Restore a trash receipt into its original root with no-replace semantics."""

    name = "posix-restore-no-replace-v1"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = Path(trash_root)
        if not self.trash_root.is_absolute():
            raise ValueError("trash_root must be absolute")

    def restore(self, candidate: RestoreCandidate) -> RestoreOutcome:
        effect = candidate.effect
        source = Path(effect.source.path)
        if effect.action != "trash":
            return RestoreOutcome(candidate.action_id, "blocked", "restore_supports_trash_only")
        try:
            root_stat = os.lstat(candidate.root)
            root_snapshot = snapshot_path(candidate.root)
        except OSError:
            return RestoreOutcome(candidate.action_id, "blocked", "restore_root_unavailable")
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return RestoreOutcome(candidate.action_id, "blocked", "restore_root_not_directory")
        if (
            self.trash_root != candidate.trash_root
            or _snapshot_identity(root_snapshot) != _snapshot_identity(candidate.root_snapshot)
        ):
            return RestoreOutcome(candidate.action_id, "blocked", "restore_root_identity_changed")
        files_root = self.trash_root / "files"
        info_root = self.trash_root / "info"
        try:
            trash_root_stat = os.lstat(self.trash_root)
            if stat.S_ISLNK(trash_root_stat.st_mode) or not stat.S_ISDIR(trash_root_stat.st_mode):
                return RestoreOutcome(candidate.action_id, "blocked", "trash_root_not_directory")
            current_trash_root = snapshot_path(self.trash_root)
            if _snapshot_identity(current_trash_root) != _snapshot_identity(
                candidate.trash_root_snapshot
            ):
                return RestoreOutcome(candidate.action_id, "blocked", "trash_root_identity_changed")
            validate_mutation_path(
                candidate.root,
                source,
                role="restore destination",
                allow_missing_leaf=True,
            )
            _strict_path_under(files_root, candidate.trash_path, role="trash file")
            _strict_path_under(info_root, candidate.info_path, role="trash info")
            if candidate.info_path.name != candidate.trash_path.name + ".trashinfo":
                return RestoreOutcome(candidate.action_id, "blocked", "trash_info_name_mismatch")
            if os.path.lexists(source):
                return RestoreOutcome(candidate.action_id, "blocked", "restore_destination_exists")
            trash_stat = os.lstat(candidate.trash_path)
            info_stat = os.lstat(candidate.info_path)
            if (
                stat.S_ISLNK(trash_stat.st_mode)
                or not stat.S_ISREG(trash_stat.st_mode)
                or trash_stat.st_nlink != 1
                or stat.S_ISLNK(info_stat.st_mode)
                or not stat.S_ISREG(info_stat.st_mode)
                or info_stat.st_nlink != 1
            ):
                return RestoreOutcome(candidate.action_id, "blocked", "trash_evidence_not_regular")
            trash_snapshot = _verify_trash_candidate(candidate)
            if os.stat(source.parent, follow_symlinks=False).st_dev != trash_snapshot.volume_id:
                return RestoreOutcome(
                    candidate.action_id,
                    "blocked",
                    "exdev_restore_requires_same_filesystem",
                )
            _rename_noreplace(
                candidate.trash_path,
                source,
                source_root=files_root,
                destination_root=candidate.root,
                expected_source=candidate.effect.source,
            )
            _fsync_directory(files_root)
            _fsync_directory(candidate.root)
        except FileExistsError:
            return RestoreOutcome(candidate.action_id, "blocked", "restore_destination_exists")
        except FileChangedError as exc:
            return RestoreOutcome(
                candidate.action_id,
                "blocked",
                "trash_content_changed",
                str(exc),
            )
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return RestoreOutcome(
                    candidate.action_id,
                    "blocked",
                    "exdev_restore_requires_same_filesystem",
                )
            return RestoreOutcome(
                candidate.action_id,
                "recovery_required",
                "restore_effect_ambiguous",
                str(exc),
            )
        except (RuntimeError, ValueError) as exc:
            return RestoreOutcome(
                candidate.action_id, "blocked", "restore_preflight_failed", str(exc)
            )
        try:
            _verify_restored_source(candidate)
            # The .trashinfo is deliberately removed only after the restored
            # bytes are verified.  If that cleanup fails, the action remains
            # recoverable and its evidence is retained for reconciliation.
            os.unlink(candidate.info_path)
            _fsync_directory(info_root)
            receipt = _canonical_json(
                {
                    "action_id": candidate.action_id,
                    "backend": self.name,
                    "digest": effect.source_digest,
                    "info_path": str(candidate.info_path),
                    "info_removed": True,
                    "operation": "restore",
                    "original_action_id": candidate.original_action_id,
                    "receipt_type": "curation_restore/v1",
                    "schema_version": 1,
                    "source_path": effect.source.path,
                    "source_restored": True,
                    "trash_path": str(candidate.trash_path),
                }
            )
            return RestoreOutcome(
                candidate.action_id,
                "restored",
                "restore_verified",
                receipt_json=receipt,
            )
        except (FileChangedError, OSError, RuntimeError, ValueError) as exc:
            return RestoreOutcome(
                candidate.action_id,
                "recovery_required",
                "restore_postcondition_failed",
                str(exc),
            )


def _source_snapshot_from_expected(raw: object, *, source_path: str) -> FileSnapshot:
    """Decode the action owner's canonical identity without legacy state."""

    try:
        document = json.loads(str(raw))
        source = document["source"]
        if (
            not isinstance(document, dict)
            or not isinstance(source, dict)
            or document.get("schema_version") != 1
            or document.get("target_path") is not None
            or source.get("path") != source_path
        ):
            raise ValueError("expected identity is not bound to the Trash source")
        volume_id = int(str(source["volume_id"]), 16)
        file_id = int(str(source["file_id"]), 16)
        size = source["size"]
        mtime_ns = source["mtime_ns"]
        birthtime_ns = source["birthtime_ns"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("expected identity is malformed") from exc
    if (
        volume_id < 0
        or file_id < 0
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(mtime_ns, bool)
        or not isinstance(mtime_ns, int)
        or mtime_ns < 0
        or isinstance(birthtime_ns, bool)
        or not isinstance(birthtime_ns, int)
        or birthtime_ns < -1
    ):
        raise ValueError("expected identity contains invalid metadata")
    snapshot = FileSnapshot(source_path, volume_id, file_id, size, mtime_ns, birthtime_ns)
    if json.loads(expected_identity_json(snapshot, source_path=source_path, target_path=None)) != document:
        raise ValueError("expected identity is not canonical")
    return snapshot


def _candidate_from_action(
    state: FrameworkState | _ReadonlyState,
    action_id: int,
) -> tuple[RestoreCandidate, int, str]:
    row = state._connection.execute(
        """SELECT run_id,status,action_type,source_path,target_path,
        effect_receipt_json,expected_identity_json
        FROM file_actions WHERE action_id=?""",
        (action_id,),
    ).fetchone()
    if row is None:
        raise ValueError("file action does not exist")
    if not str(row[2]).startswith("trash_"):
        raise ValueError("only a Trash action can be restored")
    if str(row[1]) not in {"applied", "recovery_required"}:
        raise ValueError("file action is not restorable from its current status")
    if row[3] is None or row[4] is not None or row[5] is None or row[6] is None:
        raise ValueError("file action lacks a complete Trash receipt")
    try:
        source = _source_snapshot_from_expected(str(row[6]), source_path=str(row[3]))
        receipt = json.loads(str(row[5]))
        source_digest = receipt.get("source_digest")
        if not isinstance(source_digest, str) or not source_digest:
            raise ValueError("Trash receipt has no source digest")
        (
            trash_root,
            trash_path,
            info_path,
            trash_root_snapshot,
            trash_volume_id,
            trash_file_id,
        ) = _original_receipt_parts(
            str(row[5]),
            action_id=action_id,
            source=source,
            source_digest=source_digest,
        )
        root_rows = state._connection.execute(
            "SELECT root FROM initial_runs WHERE run_id=? LIMIT 2",
            (int(row[0]),),
        ).fetchall()
        if len(root_rows) != 1:
            raise ValueError("Trash action run has no unique root")
        root = Path(str(root_rows[0][0]))
        if not root.is_absolute():
            raise ValueError("Trash action root is not absolute")
        root_snapshot = snapshot_path(root)
        effect = _RestoreEffect(
            action="trash",
            source=source,
            source_digest=source_digest,
            target_path=None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        raise ValueError("file action Trash receipt is malformed") from exc
    candidate = RestoreCandidate(
        action_id=action_id,
        original_action_id=action_id,
        effect=effect,
        root=root,
        root_snapshot=root_snapshot,
        trash_path=trash_path,
        info_path=info_path,
        trash_root=trash_root,
        trash_root_snapshot=trash_root_snapshot,
        trash_volume_id=trash_volume_id,
        trash_file_id=trash_file_id,
    )
    _verify_root(candidate)
    return candidate, int(row[0]), str(row[5])


def restore_curation_preview(database: Path, action_id: int) -> dict[str, object]:
    """Return read-only restoration evidence and its exact confirmation token."""

    database = Path(database)
    with _readonly_state(database) as state:
        candidate, _run_id, receipt = _candidate_from_action(state, action_id)
        restorable = True
        detail = None
        try:
            validate_mutation_path(
                candidate.root,
                candidate.effect.source.path,
                role="restore destination",
                allow_missing_leaf=True,
            )
            _verify_trash_candidate(candidate)
        except (OSError, RuntimeError, ValueError, FileChangedError) as exc:
            restorable = False
            detail = str(exc)
        return {
            "schema": CURATION_RESTORE_SCHEMA,
            "schema_version": 1,
            "action_id": action_id,
            "action_type": "trash",
            "source_path": candidate.effect.source.path,
            "trash_path": str(candidate.trash_path),
            "info_path": str(candidate.info_path),
            "receipt_digest": hashlib.sha256(receipt.encode("utf-8")).hexdigest(),
            "confirmation": restore_confirmation_token(action_id, receipt),
            "restorable": restorable,
            "detail": detail,
            "read_only": True,
            "effects": {"state": "none", "corpus": "none", "external": "none"},
        }


def restore_curation_action(
    database: Path,
    action_id: int,
    *,
    backend: RestoreBackend,
    confirmation: str,
    actor: str,
    state: FrameworkState | None = None,
) -> RestoreOutcome:
    """Restore one trash action with a separate exact human confirmation."""

    if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id < 1:
        raise ValueError("action_id must be positive")
    if not isinstance(actor, str) or not actor or actor.strip() != actor:
        raise ValueError("restore actor must be a trimmed non-empty string")
    database = Path(database)
    owned = state is None
    effective_state = FrameworkState(database, existing_only=True) if state is None else state
    try:
        with FrameworkRunLock(database.parent / "framework.lock"):
            original, run_id, original_receipt = _candidate_from_action(effective_state, action_id)
            expected_confirmation = restore_confirmation_token(action_id, original_receipt)
            if confirmation != expected_confirmation:
                return RestoreOutcome(action_id, "blocked", "restore_confirmation_mismatch")
            intent = _canonical_json(
                {
                    "actor": actor,
                    "original_action_id": action_id,
                    "original_receipt_digest": hashlib.sha256(
                        original_receipt.encode("utf-8")
                    ).hexdigest(),
                    "source_path": original.effect.source.path,
                    "schema": RESTORE_INTENT_SCHEMA,
                }
            )
            row = effective_state._connection.execute(
                "SELECT action_id,status,effect_receipt_json FROM file_actions WHERE evidence=? LIMIT 1",
                (intent,),
            ).fetchone()
            if row is None:
                restore_id = effective_state.begin_file_action(
                    run_id,
                    "restore_curation",
                    original.effect.source.path,
                    None,
                    None,
                    intent,
                    True,
                )
                status = "started"
            else:
                restore_id = int(row[0])
                status = str(row[1])
            candidate = replace(original, action_id=restore_id)
            if status == "applied":
                receipt = row[2] if row is not None else None
                if receipt is None or not _restore_receipt_valid(str(receipt), candidate=candidate):
                    raise ValueError("stored restore receipt is invalid")
                try:
                    _verify_restore_postconditions(candidate)
                except (OSError, RuntimeError, ValueError, FileChangedError) as exc:
                    return RestoreOutcome(
                        restore_id,
                        "recovery_required",
                        "restore_receipt_conflicts",
                        str(exc),
                        idempotent=True,
                    )
                return RestoreOutcome(
                    restore_id,
                    "already_restored",
                    "already_restored",
                    idempotent=True,
                )
            if status in {"recovery_required", "applying"}:
                if status == "applying":
                    effective_state.require_file_action_recovery(
                        (restore_id,),
                        "restore replay requires reconciliation before retry",
                    )
                return RestoreOutcome(
                    restore_id,
                    "recovery_required",
                    "reconcile_before_retry",
                    idempotent=True,
                )
            if status in {"failed", "skipped", "planned"}:
                return RestoreOutcome(
                    restore_id, "blocked", "existing_terminal_restore", idempotent=True
                )
            expected = expected_identity_json(
                original.effect.source,
                source_path=original.effect.source.path,
                target_path=None,
            )
            payload = json.loads(expected)
            payload.update(
                {
                    "original_action_id": action_id,
                    "restore_actor": actor,
                    "source_digest": original.effect.source_digest,
                }
            )
            effective_state.mark_file_actions_applying(((restore_id, _canonical_json(payload)),))
            try:
                outcome = backend.restore(candidate)
                if not isinstance(outcome, RestoreOutcome):
                    raise ValueError("restore backend returned an unsupported outcome")
                outcome = RestoreOutcome(
                    outcome.action_id,
                    outcome.status,
                    outcome.reason,
                    outcome.detail,
                    outcome.receipt_json,
                    outcome.idempotent,
                )
                if outcome.action_id != restore_id or outcome.idempotent:
                    raise ValueError("restore backend outcome is not bound to the fresh action")
            except BaseException as exc:
                effective_state.require_file_action_recovery(
                    (restore_id,),
                    f"restore backend exception: {type(exc).__name__}: {exc}",
                )
                return RestoreOutcome(
                    restore_id,
                    "recovery_required",
                    "restore_backend_exception",
                    str(exc),
                )
            if outcome.status == "restored" and outcome.receipt_json is not None:
                if not _restore_receipt_valid(outcome.receipt_json, candidate=candidate):
                    effective_state.require_file_action_recovery(
                        (restore_id,),
                        "restore receipt is invalid",
                    )
                    return RestoreOutcome(
                        restore_id, "recovery_required", "restore_receipt_invalid"
                    )
                try:
                    _verify_restore_postconditions(candidate)
                except (OSError, RuntimeError, ValueError, FileChangedError) as exc:
                    effective_state.require_file_action_recovery(
                        (restore_id,),
                        f"restore postcondition failed: {type(exc).__name__}: {exc}",
                    )
                    return RestoreOutcome(
                        restore_id,
                        "recovery_required",
                        "restore_postcondition_failed",
                        str(exc),
                    )
                try:
                    effective_state.confirm_file_actions_applied(
                        ((restore_id, outcome.receipt_json),)
                    )
                except BaseException as exc:
                    effective_state.require_file_action_recovery(
                        (restore_id,),
                        f"restore receipt persistence failed: {type(exc).__name__}: {exc}",
                    )
                    return RestoreOutcome(
                        restore_id,
                        "recovery_required",
                        "restore_receipt_persistence_failed",
                        str(exc),
                    )
                return outcome
            detail = outcome.detail or outcome.reason
            effective_state.require_file_action_recovery((restore_id,), detail)
            return RestoreOutcome(restore_id, "recovery_required", outcome.reason, detail)
    finally:
        if owned:
            effective_state.close()


__all__ = (
    "CURATION_RESTORE_SCHEMA",
    "PosixRestoreBackend",
    "RestoreBackend",
    "RestoreCandidate",
    "RestoreOutcome",
    "restore_confirmation_token",
    "restore_curation_action",
    "restore_curation_preview",
)
