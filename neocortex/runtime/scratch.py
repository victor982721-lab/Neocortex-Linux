"""Registered, private scratch workspaces for bounded NeoCortex work.

This module is deliberately a small filesystem owner.  It does not inspect
``/tmp`` (or any other parent) looking for names that happen to look like
NeoCortex artifacts.  A workspace is discoverable only when NeoCortex created
its private directory and its authenticated manifest.  The manifest binds the
owner, lifecycle state and POSIX identity of the directory; every retirement
re-validates those claims immediately before unlinking the workspace.

The owner is intentionally independent of SQLite and KIO.  SQLite snapshots,
release staging, corpus actions and recovery checkpoints have stronger,
separate contracts and must not be adopted by this cleaner.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Self

SCRATCH_SCHEMA = "neocortex.scratch/v1"
MANIFEST_NAME = "manifest.json"
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_REASON_BYTES = 8 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_RECORDS = 100_000


class ScratchError(RuntimeError):
    """Base class for a scratch contract violation."""


class ScratchSecurityError(ScratchError):
    """The configured scratch root or a workspace failed a safety claim."""


class ScratchRootError(ScratchSecurityError):
    """The requested scratch root is absent or cannot be made private."""


class ScratchManifestError(ScratchSecurityError):
    """A workspace manifest is malformed or fails its authenticated digest."""


class ScratchState(str, Enum):
    """Durable workspace lifecycle states."""

    ACTIVE = "active"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED_RETAINED = "failed-retained"
    RECOVERY_REQUIRED = "recovery_required"
    RETIRED = "retired"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bounded_text(value: object, *, label: str, limit: int = _MAX_REASON_BYTES) -> str:
    text = str(value)
    if not text.strip():
        raise ValueError(f"{label} must not be blank")
    if len(text.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    return text


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    # Linux does not expose a creation time consistently.  ``-1`` is an
    # explicit unavailable sentinel, never a ctime masquerading as birthtime.
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(birthtime) if birthtime is not None else -1,
    )


def _same_identity(path: Path, expected: Sequence[int]) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(current.st_mode) or len(expected) != 3:
        return False
    return _identity(current) == tuple(int(value) for value in expected)


def _validate_absolute_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _directory_size(path: Path) -> int:
    """Count apparent bytes in one private workspace without following links."""

    total = 0
    try:
        for entry in os.scandir(path):
            if entry.name == MANIFEST_NAME or entry.name.startswith(f".{MANIFEST_NAME}."):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += max(0, int(metadata.st_size))
            elif stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                total += _directory_size(Path(entry.path))
    except OSError:
        return total
    return total


def _workspace_payload_issue(path: Path) -> str | None:
    """Return a conservative issue for links or non-private payload files."""

    try:
        entries = tuple(os.scandir(path))
    except OSError as exc:
        return f"workspace payload could not be inspected: {exc}"
    for entry in entries:
        if entry.name == MANIFEST_NAME or entry.name.startswith(f".{MANIFEST_NAME}."):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            return "payload_identity_unavailable"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink_payload"
        if metadata.st_uid != os.geteuid():
            return "payload_owner_drift"
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            return "hardlink_payload"
        if stat.S_ISDIR(metadata.st_mode):
            nested = _workspace_payload_issue(Path(entry.path))
            if nested is not None:
                return nested
    return None


def _remove_directory_fd(directory_fd: int) -> None:
    """Remove children through a directory descriptor, never following links."""

    for name in os.listdir(directory_fd):
        child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(child_stat.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (child_stat.st_dev, child_stat.st_ino):
                    raise ScratchSecurityError("scratch child identity changed during retirement")
                _remove_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            # Unlinking a symlink or a regular hardlink removes only that name;
            # it cannot remove the target or an outside inode.
            os.unlink(name, dir_fd=directory_fd)


def _remove_tree_no_follow(path: Path, *, expected_identity: Sequence[int] | None = None) -> None:
    """Remove one claimed workspace through descriptor-relative operations."""

    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch workspace is not a directory")
        if expected_identity is not None and (
            len(expected_identity) != 3
            or _identity(metadata) != tuple(int(value) for value in expected_identity)
        ):
            raise ScratchSecurityError("scratch workspace identity changed before retirement")
        directory_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ScratchSecurityError("scratch workspace identity changed during retirement")
            _remove_directory_fd(directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ValueError("scratch manifest exceeds the durable size limit")
    parent = path.parent
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class ScratchRecord:
    """One bounded inspection result for a registered workspace."""

    record_id: str
    owner: str
    run_id: int | str | None
    path: Path
    state: ScratchState | str
    created_ns: int
    updated_ns: int
    path_identity: tuple[int, int, int] | None
    root_identity: tuple[int, int, int] | None
    size_bytes: int
    retain_on_success: bool
    retire_after_ns: int | None
    result_paths: tuple[Path, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    manifest_digest: str | None = None
    eligible: bool = False
    valid: bool = True
    issue: str | None = None

    @property
    def status(self) -> str:
        """Alias used by lightweight adapters."""

        return self.state.value if isinstance(self.state, ScratchState) else self.state

    @property
    def identity(self) -> tuple[int, int, int] | None:
        """Alias for the physical workspace identity."""

        return self.path_identity


@dataclass(frozen=True, slots=True)
class ScratchPlan:
    """Bounded result of a plan or apply pass."""

    root: Path
    records: tuple[ScratchRecord, ...] = ()
    planned: int = 0
    applied: int = 0
    kept: int = 0
    blocked: int = 0
    failed: int = 0
    recovery_required: int = 0
    planned_bytes: int = 0
    applied_bytes: int = 0
    kept_bytes: int = 0
    blocked_bytes: int = 0
    failed_bytes: int = 0
    recovery_required_bytes: int = 0
    status: str = "planned"
    reason: str | None = None
    read_only: bool = True
    unmanaged: tuple[Path, ...] = ()
    root_blocked: str | None = None

    @property
    def items(self) -> tuple[ScratchRecord, ...]:
        return self.records

    @property
    def entries(self) -> tuple[ScratchRecord, ...]:
        return self.records


class ScratchWorkspace:
    """Context manager for one manager-owned private workspace."""

    def __init__(
        self,
        manager: "ScratchManager",
        record: ScratchRecord,
        *,
        retain_on_success: bool,
    ) -> None:
        self._manager = manager
        self._record_id = record.record_id
        self._path = record.path
        self._retain_on_success = retain_on_success
        self._state = ScratchState(record.state)
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record_id(self) -> str:
        return self._record_id

    @property
    def state(self) -> ScratchState:
        record = self.record
        if not record.valid or not isinstance(record.state, ScratchState):
            raise ScratchManifestError(record.reason or "scratch manifest is invalid")
        self._state = record.state
        return record.state

    @property
    def record(self) -> ScratchRecord:
        record = self._manager._record_for_path(self._path)
        if record is None:
            raise ScratchSecurityError("workspace manifest no longer matches its claim")
        return record

    def mark_committing(self) -> "ScratchWorkspace":
        self._ensure_open()
        if self._state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise ScratchError(f"workspace is not active: {self._state.value}")
        self._state = ScratchState.COMMITTING
        self._manager._update_state(self._path, self._record_id, ScratchState.COMMITTING)
        return self

    def complete(
        self,
        result_paths: Iterable[Path | str] = (),
        *,
        retain: bool | None = None,
    ) -> ScratchRecord | None:
        self._ensure_open()
        if self._state not in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            raise ScratchError(f"workspace is not active: {self._state.value}")
        # Validate the durable-result claim before changing the state.  A
        # rejected result must leave an active workspace retryable, not turn it
        # into a misleading ``committing`` row.
        normalized_results = self._manager._validate_result_paths(self._path, result_paths)
        self._state = ScratchState.COMMITTING
        self._manager._update_state(self._path, self._record_id, ScratchState.COMMITTING)
        if retain is not None and type(retain) is not bool:
            raise ValueError("scratch retain must be a boolean or null")
        keep = self._retain_on_success if retain is None else retain
        record = self._manager._update_state(
            self._path,
            self._record_id,
            ScratchState.COMPLETED,
            retain_on_success=keep,
            result_paths=normalized_results,
            retire_after_ns=time.time_ns(),
        )
        self._state = ScratchState.COMPLETED
        if keep:
            return record
        try:
            self._manager._retire_record(record)
        except BaseException as exc:
            self._manager._update_state(
                self._path,
                self._record_id,
                ScratchState.RECOVERY_REQUIRED,
                reason=f"success cleanup failed: {type(exc).__name__}: {exc}",
            )
            raise
        self._closed = True
        return None

    def fail(self, reason: object) -> ScratchRecord:
        self._ensure_open()
        bounded = _bounded_text(reason, label="scratch failure reason")
        self._state = ScratchState.FAILED_RETAINED
        return self._manager._update_state(
            self._path,
            self._record_id,
            ScratchState.FAILED_RETAINED,
            reason=bounded,
        )

    def retire(self) -> None:
        self._ensure_open()
        record = self._manager._record_for_path(self._path)
        if record is None or record.record_id != self._record_id:
            raise ScratchSecurityError("workspace manifest no longer matches its claim")
        if record.state != ScratchState.COMPLETED.value:
            raise ScratchError("only a completed workspace can be retired")
        self._manager._retire_record(record)
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ScratchError("workspace is already closed")
        try:
            metadata = self._path.lstat()
        except OSError as exc:
            raise ScratchSecurityError("scratch workspace is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch workspace is no longer a directory")

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._closed:
            return None
        if exc is not None:
            try:
                self.fail(f"{type(exc).__name__}: {exc}")
            except BaseException:
                # Preserve the primary exception; the manager will report the
                # recovery-required workspace on the next plan.
                pass
            return None
        if self._state in {ScratchState.ACTIVE, ScratchState.COMMITTING}:
            self.complete()
        return None


class ScratchManager:
    """Create, inspect and retire only one private scratch scope."""

    def __init__(
        self,
        root: Path,
        *,
        owner: str = "neocortex",
        create_root: bool = False,
    ) -> None:
        self.root = _validate_absolute_path(Path(root), label="scratch root")
        self.owner = _bounded_text(owner, label="scratch owner", limit=128)
        self.create_root = bool(create_root)
        if create_root:
            self._ensure_root(create=True)

    def _ensure_root(self, *, create: bool) -> bool:
        try:
            metadata = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return False
            self._mkdir_private(self.root)
            metadata = self.root.lstat()
        except OSError as exc:
            raise ScratchSecurityError(f"cannot inspect scratch root: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("scratch root must be a regular directory, not a symlink")
        if metadata.st_mode & 0o077:
            raise ScratchSecurityError("scratch root must be private (mode 0700 or stricter)")
        return True

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ScratchSecurityError(f"cannot inspect created scratch root: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ScratchSecurityError("created scratch root is not a directory")
        if metadata.st_uid != os.geteuid():
            raise ScratchSecurityError("scratch root is not owned by the current user")
        if metadata.st_mode & 0o077:
            # Never chmod an existing directory: doing so could alter a path
            # that another owner created between the existence check and this
            # call.  Newly-created directories already inherit 0700 or stricter
            # from mkdir/umask.
            detail = "scratch root must be private (mode 0700 or stricter)"
            raise ScratchSecurityError(detail)

    def create(
        self,
        *,
        run_id: int | str | None = None,
        retain_on_success: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ScratchWorkspace:
        """Create one private registered workspace after an explicit claim."""

        if not self._ensure_root(create=self.create_root):
            raise ScratchRootError("scratch root is absent")
        if type(retain_on_success) is not bool:
            raise ValueError("scratch retain_on_success must be a boolean")
        if run_id is not None and (
            (type(run_id) is int and run_id < 1)
            or type(run_id) is bool
            or (not isinstance(run_id, (int, str)))
            or (isinstance(run_id, str) and not run_id.strip())
        ):
            raise ValueError("scratch run_id must be a positive integer, string or null")
        if isinstance(run_id, str) and len(run_id.encode("utf-8")) > 256:
            raise ValueError("scratch run_id exceeds 256 UTF-8 bytes")
        safe_metadata: dict[str, Any] = {} if metadata is None else dict(metadata)
        if len(_canonical_json(safe_metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
            raise ValueError("scratch metadata exceeds the durable size limit")
        for _ in range(8):
            record_id = uuid.uuid4().hex
            path = self.root / f"workspace-{record_id}"
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            created = time.time_ns()
            identity = _identity(path.lstat())
            root_identity = _identity(self.root.lstat())
            payload: dict[str, Any] = {
                "schema": SCRATCH_SCHEMA,
                "record_id": record_id,
                "owner": self.owner,
                "run_id": run_id,
                "path": str(path),
                "path_identity": list(identity),
                "root_identity": list(root_identity),
                "state": ScratchState.ACTIVE.value,
                "created_ns": created,
                "updated_ns": created,
                "retain_on_success": bool(retain_on_success),
                "retire_after_ns": None,
                "result_paths": [],
                "metadata": safe_metadata,
                "reason": None,
            }
            payload["manifest_digest"] = _manifest_digest(payload)
            try:
                _write_json_atomic(path / MANIFEST_NAME, payload)
            except BaseException:
                _remove_tree_no_follow(path, expected_identity=identity)
                raise
            record = self._record_from_payload(path, payload, size_bytes=0)
            return ScratchWorkspace(self, record, retain_on_success=bool(retain_on_success))
        raise ScratchError("could not allocate a unique scratch workspace")

    # Compatibility spelling for producer adapters that prefer an explicit
    # noun.  It is deliberately just an alias, not another implementation.
    create_workspace = create

    def records(self) -> tuple[ScratchRecord, ...]:
        if not self._ensure_root(create=False):
            return ()
        return tuple(self._scan_records(now_ns=time.time_ns()))

    def plan(self, *, now_ns: int | None = None) -> ScratchPlan:
        now = time.time_ns() if now_ns is None else now_ns
        if type(now) is not int or now < 0:
            raise ValueError("scratch now_ns must be a non-negative integer")
        if not self._ensure_root(create=False):
            return ScratchPlan(
                self.root,
                reason="scratch root is absent",
                root_blocked="scratch root is absent",
            )
        records = tuple(self._scan_records(now_ns=now))
        return self._summarize(
            records,
            read_only=True,
            unmanaged=self._unmanaged_entries(),
        )

    def apply(
        self,
        plan: ScratchPlan | None = None,
        *,
        now_ns: int | None = None,
    ) -> ScratchPlan:
        """Re-scan and retire only completed, still-eligible records."""

        del plan  # stale plans are never trusted as an effect authorization
        now = time.time_ns() if now_ns is None else now_ns
        if type(now) is not int or now < 0:
            raise ValueError("scratch now_ns must be a non-negative integer")
        if not self._ensure_root(create=False):
            return ScratchPlan(
                self.root,
                reason="scratch root is absent",
                read_only=False,
                root_blocked="scratch root is absent",
            )
        observed = tuple(self._scan_records(now_ns=now))
        candidates = [record for record in observed if record.eligible]
        applied_records: list[ScratchRecord] = []
        remaining: list[ScratchRecord] = []
        for record in observed:
            if not record.eligible:
                remaining.append(record)
                continue
            try:
                self._retire_record(record)
            except ScratchSecurityError as exc:
                remaining.append(
                    replace(record, state="blocked", reason=str(exc), eligible=False)
                )
            except OSError as exc:
                remaining.append(
                    replace(record, state="failed", reason=str(exc), eligible=False)
                )
            else:
                applied_records.append(record)
        records_after = tuple(remaining)
        summary = self._summarize(
            records_after,
            read_only=False,
            unmanaged=self._unmanaged_entries(),
        )
        status = (
            "recovery_required"
            if summary.recovery_required
            else "failed"
            if summary.failed
            else "blocked"
            if summary.blocked
            else "applied"
        )
        return ScratchPlan(
            root=summary.root,
            records=records_after,
            planned=len(candidates),
            applied=len(applied_records),
            kept=summary.kept,
            blocked=summary.blocked,
            failed=summary.failed,
            recovery_required=summary.recovery_required,
            planned_bytes=sum(record.size_bytes for record in candidates),
            applied_bytes=sum(record.size_bytes for record in applied_records),
            kept_bytes=summary.kept_bytes,
            blocked_bytes=summary.blocked_bytes,
            failed_bytes=summary.failed_bytes,
            recovery_required_bytes=summary.recovery_required_bytes,
            status=status,
            reason=summary.reason,
            read_only=False,
            unmanaged=summary.unmanaged,
            root_blocked=summary.root_blocked,
        )

    def _unmanaged_entries(self) -> tuple[Path, ...]:
        """List suspicious neighbours without treating them as candidates."""

        try:
            entries = tuple(os.scandir(self.root))
        except OSError:
            return ()
        return tuple(
            self.root / entry.name
            for entry in sorted(entries, key=lambda item: item.name)
            if entry.name.startswith(("neocortex-", "scratch-"))
            and not entry.name.startswith("workspace-")
        )

    def _scan_records(self, *, now_ns: int) -> Iterable[ScratchRecord]:
        try:
            entries = tuple(os.scandir(self.root))
        except OSError as exc:
            raise ScratchSecurityError(f"cannot scan scratch root: {exc}") from exc
        if len(entries) > _MAX_RECORDS:
            raise ScratchSecurityError("scratch root exceeds the registered-record limit")
        for entry in sorted(entries, key=lambda item: item.name):
            path = self.root / entry.name
            try:
                metadata = path.lstat()
            except OSError as exc:
                yield self._invalid_record(path, f"workspace disappeared: {exc}")
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                # Unregistered neighbours are never candidates.  Surface a
                # bounded blocked row only for a manifest-shaped entry; this
                # avoids treating arbitrary files in the private root as ours.
                if entry.name.startswith("workspace-"):
                    yield self._invalid_record(path, "workspace claim is not a directory")
                continue
            manifest_path = path / MANIFEST_NAME
            try:
                manifest_exists = manifest_path.lstat()
            except FileNotFoundError:
                if entry.name.startswith("workspace-"):
                    yield self._invalid_record(path, "workspace manifest is absent")
                continue
            except OSError as exc:
                yield self._invalid_record(path, f"workspace manifest is unavailable: {exc}")
                continue
            try:
                if stat.S_ISLNK(manifest_exists.st_mode) or not stat.S_ISREG(
                    manifest_exists.st_mode
                ):
                    raise ScratchManifestError("scratch manifest is not a regular file")
                if (
                    manifest_exists.st_uid != os.geteuid()
                    or manifest_exists.st_mode & 0o077
                    or manifest_exists.st_nlink != 1
                ):
                    raise ScratchManifestError("scratch manifest protection drifted")
                raw = manifest_path.read_bytes()
                if len(raw) > _MAX_MANIFEST_BYTES:
                    raise ScratchSecurityError("scratch manifest is too large")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ScratchSecurityError("scratch manifest is not an object")
                record = self._record_from_payload(
                    path,
                    payload,
                    size_bytes=_directory_size(path),
                    now_ns=now_ns,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ScratchError, TypeError, ValueError) as exc:
                yield self._invalid_record(
                    path,
                    f"invalid scratch manifest: {type(exc).__name__}: {exc}",
                    issue=(
                        "workspace_mode_drift"
                        if "workspace mode" in str(exc)
                        else "manifest_invalid"
                    ),
                )
                continue
            yield record

    def _invalid_record(
        self,
        path: Path,
        reason: str,
        *,
        issue: str = "manifest_invalid",
    ) -> ScratchRecord:
        try:
            metadata = path.lstat()
        except OSError:
            metadata = None
        is_directory = metadata is not None and stat.S_ISDIR(metadata.st_mode)
        return ScratchRecord(
            record_id=f"invalid:{path.name}",
            owner="",
            run_id=None,
            path=path,
            state=ScratchState.RECOVERY_REQUIRED,
            created_ns=0,
            updated_ns=0,
            path_identity=None,
            size_bytes=_directory_size(path) if is_directory else 0,
            retain_on_success=True,
            retire_after_ns=None,
            reason=_bounded_text(reason, label="scratch reason"),
            eligible=False,
            valid=False,
            issue=issue,
            root_identity=None,
        )

    def _record_from_payload(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        size_bytes: int,
        now_ns: int | None = None,
    ) -> ScratchRecord:
        if payload.get("schema") != SCRATCH_SCHEMA:
            raise ScratchSecurityError("unsupported scratch manifest schema")
        expected_digest = payload.get("manifest_digest")
        if not isinstance(expected_digest, str) or expected_digest != _manifest_digest(payload):
            raise ScratchSecurityError("scratch manifest digest mismatch")
        record_id = payload.get("record_id")
        owner = payload.get("owner")
        state = payload.get("state")
        manifest_path = payload.get("path")
        identity = payload.get("path_identity")
        root_identity = payload.get("root_identity")
        created_ns = payload.get("created_ns")
        updated_ns = payload.get("updated_ns")
        run_id = payload.get("run_id")
        if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
            raise ScratchSecurityError("scratch record id is invalid")
        if not isinstance(owner, str) or not owner or len(owner.encode("utf-8")) > 128:
            raise ScratchSecurityError("scratch owner is invalid")
        if state not in {member.value for member in ScratchState}:
            raise ScratchSecurityError("scratch state is invalid")
        if not isinstance(manifest_path, str) or Path(manifest_path) != path:
            raise ScratchSecurityError("scratch manifest path claim changed")
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or any(type(value) is not int for value in identity)
            or not _same_identity(path, identity)
        ):
            raise ScratchSecurityError("scratch workspace identity changed")
        if (
            not isinstance(root_identity, list)
            or len(root_identity) != 3
            or any(type(value) is not int for value in root_identity)
            or not _same_identity(self.root, root_identity)
        ):
            raise ScratchSecurityError("scratch root identity changed")
        if type(created_ns) is not int or created_ns < 0 or type(updated_ns) is not int or updated_ns < 0:
            raise ScratchSecurityError("scratch manifest timestamps are invalid")
        if run_id is not None and (
            (type(run_id) is int and run_id < 1)
            or not isinstance(run_id, (int, str))
            or (isinstance(run_id, str) and not run_id.strip())
        ):
            raise ScratchSecurityError("scratch run id is invalid")
        if isinstance(run_id, str) and len(run_id.encode("utf-8")) > 256:
            raise ScratchSecurityError("scratch run id is too large")
        retain = payload.get("retain_on_success", False)
        if type(retain) is not bool:
            raise ScratchSecurityError("scratch retention flag is invalid")
        retire_after = payload.get("retire_after_ns")
        if retire_after is not None and (type(retire_after) is not int or retire_after < 0):
            raise ScratchSecurityError("scratch retirement time is invalid")
        results = payload.get("result_paths", [])
        if not isinstance(results, list) or any(not isinstance(value, str) for value in results):
            raise ScratchSecurityError("scratch result paths are invalid")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping) or len(_canonical_json(metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
            raise ScratchSecurityError("scratch metadata is invalid")
        reason = payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason.encode("utf-8")) > _MAX_REASON_BYTES):
            raise ScratchSecurityError("scratch reason is invalid")
        workspace_metadata = path.lstat()
        issue: str | None = None
        if workspace_metadata.st_uid != os.geteuid() or workspace_metadata.st_mode & 0o077:
            issue = "workspace_mode_drift"
        else:
            issue = _workspace_payload_issue(path)
        eligible = (
            owner == self.owner
            and state == ScratchState.COMPLETED.value
            and (retire_after is None or retire_after <= (time.time_ns() if now_ns is None else now_ns))
            and issue is None
        )
        if owner != self.owner:
            issue = "owner_mismatch"
            reason = "workspace belongs to another owner"
        elif issue is not None and reason is None:
            reason = issue
        return ScratchRecord(
            record_id=record_id,
            owner=owner,
            run_id=run_id,
            path=path,
            state=ScratchState(state),
            created_ns=created_ns,
            updated_ns=updated_ns,
            path_identity=tuple(identity),
            root_identity=tuple(root_identity),
            size_bytes=max(0, int(size_bytes)),
            retain_on_success=retain,
            retire_after_ns=retire_after,
            result_paths=tuple(Path(value) for value in results),
            metadata=dict(metadata),
            reason=reason,
            manifest_digest=expected_digest,
            eligible=eligible,
            issue=issue,
        )

    def _summarize(
        self,
        records: Sequence[ScratchRecord],
        *,
        read_only: bool,
        unmanaged: tuple[Path, ...] = (),
    ) -> ScratchPlan:
        planned = tuple(record for record in records if record.eligible)
        blocked = tuple(
            record
            for record in records
            if (
                record.issue is not None
                and record.state != ScratchState.RECOVERY_REQUIRED
            )
            or record.state == "blocked"
        )
        failed = tuple(
            record
            for record in records
            if record.state in {"failed", ScratchState.FAILED_RETAINED}
            and record not in blocked
        )
        recovery = tuple(
            record
            for record in records
            if record.state == ScratchState.RECOVERY_REQUIRED
        )
        kept = tuple(
            record
            for record in records
            if not record.eligible
            and record not in blocked
            and record not in failed
            and record not in recovery
        )
        if recovery:
            status = "recovery_required"
        elif failed:
            status = "failed"
        elif blocked:
            status = "blocked"
        elif planned:
            status = "planned"
        else:
            status = "kept" if kept else "planned"
        return ScratchPlan(
            root=self.root,
            records=tuple(records),
            planned=len(planned),
            kept=len(kept),
            blocked=len(blocked),
            failed=len(failed),
            recovery_required=len(recovery),
            planned_bytes=sum(record.size_bytes for record in planned),
            kept_bytes=sum(record.size_bytes for record in kept),
            blocked_bytes=sum(record.size_bytes for record in blocked),
            failed_bytes=sum(record.size_bytes for record in failed),
            recovery_required_bytes=sum(record.size_bytes for record in recovery),
            status=status,
            read_only=read_only,
            unmanaged=unmanaged,
        )

    def _record_for_path(self, path: Path) -> ScratchRecord | None:
        for record in self._scan_records(now_ns=time.time_ns()):
            if record.path == path:
                return record
        return None

    def _validate_result_paths(
        self,
        workspace: Path,
        result_paths: Iterable[Path | str],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in result_paths:
            path = Path(raw)
            if not path.is_absolute():
                raise ScratchSecurityError("scratch result paths must be absolute")
            resolved = path.resolve(strict=False)
            if not _path_is_within(resolved, workspace.resolve(strict=True)):
                raise ScratchSecurityError("scratch result must remain inside the workspace")
            if not path.exists():
                raise ScratchSecurityError(f"scratch result does not exist: {path}")
            normalized.append(str(path))
        return tuple(normalized)

    def _update_state(
        self,
        path: Path,
        record_id: str,
        state: ScratchState,
        *,
        retain_on_success: bool | None = None,
        result_paths: Sequence[str] | None = None,
        retire_after_ns: int | None = None,
        reason: str | None = None,
    ) -> ScratchRecord:
        record = self._record_for_path(path)
        if record is None or record.record_id != record_id or record.owner != self.owner:
            raise ScratchSecurityError("scratch manifest no longer matches its owner")
        manifest_path = path / MANIFEST_NAME
        manifest_metadata = manifest_path.lstat()
        if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(
            manifest_metadata.st_mode
        ):
            raise ScratchManifestError("scratch manifest is not a regular file")
        if manifest_metadata.st_uid != os.geteuid() or manifest_metadata.st_mode & 0o077:
            raise ScratchManifestError("scratch manifest protection drifted")
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping) or payload.get("manifest_digest") != record.manifest_digest:
            raise ScratchSecurityError("scratch manifest changed during update")
        updated = dict(payload)
        updated["state"] = state.value
        updated["updated_ns"] = time.time_ns()
        if retain_on_success is not None:
            updated["retain_on_success"] = bool(retain_on_success)
        if result_paths is not None:
            updated["result_paths"] = list(result_paths)
        if state is ScratchState.COMPLETED:
            updated["retire_after_ns"] = retire_after_ns
        if reason is not None:
            updated["reason"] = _bounded_text(reason, label="scratch reason")
        updated["manifest_digest"] = _manifest_digest(updated)
        _write_json_atomic(manifest_path, updated)
        return self._record_from_payload(path, updated, size_bytes=_directory_size(path))

    def _retire_record(self, record: ScratchRecord) -> None:
        if record.owner != self.owner or record.state != ScratchState.COMPLETED.value:
            raise ScratchSecurityError("only this owner's completed scratch can be retired")
        self._ensure_root(create=False)
        parent_metadata = self.root.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_mode & 0o077
        ):
            raise ScratchSecurityError("scratch root changed its private-directory claim")
        current = self._record_for_path(record.path)
        if current is None or current.record_id != record.record_id or not current.eligible:
            raise ScratchSecurityError("scratch record is no longer eligible")
        if current.path_identity is None or not _same_identity(record.path, current.path_identity):
            raise ScratchSecurityError("scratch workspace identity changed before retirement")
        _remove_tree_no_follow(record.path, expected_identity=current.path_identity)


__all__ = [
    "MANIFEST_NAME",
    "SCRATCH_SCHEMA",
    "ScratchError",
    "ScratchManager",
    "ScratchManifestError",
    "ScratchPlan",
    "ScratchRecord",
    "ScratchRootError",
    "ScratchSecurityError",
    "ScratchState",
    "ScratchWorkspace",
]
