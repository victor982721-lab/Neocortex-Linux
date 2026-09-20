"""Explicit, backup-first removal of NeoCortex SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Literal

from neocortex.persistence.sqlite_backup import (
    SQLiteBackupPolicy,
    backup_sqlite_online,
)
from neocortex.persistence.sqlite_integrity import (
    IntegrityCheckMode,
    SQLiteIntegrityPolicy,
    SQLiteIntegrityReport,
    check_sqlite_integrity,
)
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    immutable_sqlite_database,
)
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    read_application_schema_version,
)
from neocortex.persistence.state_publication import (
    StateEpoch,
    StateOwnerHead,
    StatePublication,
    StatePublicationCommitError,
    StatePublicationError,
    abort_state_publication,
    publication_idempotency_key,
    read_state_epoch,
    read_state_publication_state,
    read_state_publications,
    record_state_publication,
)
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


DATABASE_PURGE_SCHEMA = "neocortex.database-purge/v1"
DATABASE_PURGE_CONFIRMATION = "DELETE_DATABASES"
DATABASE_BACKUP_SCHEMA = "neocortex.state-backup/v1"
DATABASE_RESTORE_CONFIRMATION = "RESTORE_DATABASES"
DATABASE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
DATABASE_STORE_NAMES = tuple(
    store.state_owner_id for store in STATE_STORE_REGISTRY.stores
)


class DatabasePurgeError(RuntimeError):
    """Base class for fail-closed database purge errors."""


class DatabasePurgeConfirmationError(DatabasePurgeError):
    """The caller did not provide the exact destructive confirmation token."""


class DatabasePurgeBusyError(DatabasePurgeError):
    """A framework, release, route, or watcher lock is currently held."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self.paths = tuple(paths)
        names = ", ".join(str(path) for path in self.paths)
        super().__init__(f"NeoCortex state is in use: {names}")


class DatabasePurgeChangedError(DatabasePurgeError):
    """The state changed between planning, backup, and deletion."""


class DatabaseRestoreError(DatabasePurgeError):
    """A state restore could not be validated or published safely."""


class DatabaseRestoreConfirmationError(DatabaseRestoreError):
    """The caller did not provide the exact destructive restore token."""


class DatabaseRestoreRecoveryRequiredError(DatabaseRestoreError):
    """Restore cannot safely roll back or claim a durable publication."""

    def __init__(
        self,
        message: str,
        *,
        recovery_directory: Path | None = None,
        stage_directory: Path | None = None,
        pre_restore_backup: Path | None = None,
    ) -> None:
        super().__init__(f"restore recovery_required: {message}")
        self.recovery_directory = recovery_directory
        self.stage_directory = stage_directory
        self.pre_restore_backup = pre_restore_backup

    def __str__(self) -> str:
        paths = (
            ("recovery_directory", self.recovery_directory),
            ("stage_directory", self.stage_directory),
            ("pre_restore_backup", self.pre_restore_backup),
        )
        suffix = "".join(f"; {name}={path}" for name, path in paths if path is not None)
        return super().__str__() + suffix


FileRole = Literal["database", "sidecar"]


@dataclass(frozen=True, slots=True)
class DatabaseFileSnapshot:
    path: Path
    role: FileRole
    size: int
    device: int
    inode: int
    mtime_ns: int
    mode: int
    uid: int
    gid: int

    def as_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "role": self.role,
            "size": self.size,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            # Keep the source ownership and permission bits in the manifest;
            # the backup bytes alone are not enough to reproduce a state
            # owner safely during restore.
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
        }


@dataclass(frozen=True, slots=True)
class DatabasePurgeTarget:
    owner: str
    database_name: str
    database: Path
    files: tuple[DatabaseFileSnapshot, ...]

    @property
    def bytes(self) -> int:
        return sum(item.size for item in self.files)

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "database_name": self.database_name,
            "database": str(self.database),
            "files": [item.as_payload() for item in self.files],
            "bytes": self.bytes,
        }


@dataclass(frozen=True, slots=True)
class DatabasePurgePlan:
    state_directory: Path
    stores: tuple[str, ...]
    targets: tuple[DatabasePurgeTarget, ...]
    lock_conflicts: tuple[Path, ...]
    unknown_sqlite_files: tuple[Path, ...]
    plan_digest: str

    @property
    def files(self) -> tuple[DatabaseFileSnapshot, ...]:
        return tuple(item for target in self.targets for item in target.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def as_payload(self, *, mode: str = "preview") -> dict[str, object]:
        return {
            "schema": DATABASE_PURGE_SCHEMA,
            "mode": mode,
            "state_directory": str(self.state_directory),
            "stores": list(self.stores),
            "plan_digest": self.plan_digest,
            "lock_conflicts": [str(path) for path in self.lock_conflicts],
            "unknown_sqlite_files": [str(path) for path in self.unknown_sqlite_files],
            "targets": [target.as_payload() for target in self.targets],
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class DatabasePurgeResult:
    plan: DatabasePurgePlan
    backup_directory: Path | None
    manifest: Path | None
    deleted: tuple[DatabaseFileSnapshot, ...]

    def as_payload(self) -> dict[str, object]:
        payload = self.plan.as_payload(mode="applied")
        payload.update(
            {
                "backup_directory": (
                    None if self.backup_directory is None else str(self.backup_directory)
                ),
                "manifest": None if self.manifest is None else str(self.manifest),
                "deleted": [item.as_payload() for item in self.deleted],
                "deleted_file_count": len(self.deleted),
                "deleted_bytes": sum(item.size for item in self.deleted),
            }
        )
        return payload


BackupEntryStatus = Literal["backed_up", "absent", "orphan_sidecar_only"]


@dataclass(frozen=True, slots=True)
class DatabaseBackupEntry:
    """One owner entry in a verified state backup manifest."""

    owner: str
    database_name: str
    status: BackupEntryStatus
    source: Path
    backup: Path | None
    source_files: tuple[DatabaseFileSnapshot, ...]
    source_sha256: str | None
    backup_sha256: str | None
    backup_size: int | None
    user_version: int | None
    schema_version: int | None
    integrity: SQLiteIntegrityReport | None
    reason: str | None = None

    def as_payload(self, *, backup_directory: Path | None = None) -> dict[str, object]:
        lifecycle = STATE_STORE_REGISTRY.by_owner(self.owner)
        backup_value = self.backup
        if backup_directory is not None and backup_value is not None:
            try:
                backup_value = backup_value.relative_to(backup_directory)
            except ValueError:
                pass
        return {
            "owner": self.owner,
            "database_name": self.database_name,
            "status": self.status,
            "source": str(self.source),
            "backup": None if backup_value is None else str(backup_value),
            "source_files": [_source_file_payload(item) for item in self.source_files],
            "source_sha256": self.source_sha256,
            "backup_sha256": self.backup_sha256,
            "backup_size": self.backup_size,
            "user_version": self.user_version,
            "schema_version": self.schema_version,
            "lifecycle_policy_version": lifecycle.lifecycle_policy_version,
            "authority_tables": sorted(rule.table for rule in lifecycle.lifecycle_rules
                                       if rule.role == "authoritative"),
            "integrity": None if self.integrity is None else self.integrity.as_payload(),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DatabaseBackupResult:
    """Verified multi-owner backup and its publication evidence."""

    state_directory: Path
    backup_directory: Path
    manifest: Path
    manifest_sha256: str
    state_epoch: StateEpoch
    entries: tuple[DatabaseBackupEntry, ...]
    unknown_sqlite_files: tuple[Path, ...]
    complete: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": DATABASE_BACKUP_SCHEMA,
            "state_directory": str(self.state_directory),
            "backup_directory": str(self.backup_directory),
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "state_epoch": self.state_epoch.as_payload(),
            "entries": [
                item.as_payload(backup_directory=self.backup_directory)
                for item in self.entries
            ],
            "unknown_sqlite_files": [str(path) for path in self.unknown_sqlite_files],
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class DatabaseRestoreResult:
    """Result of a staged, validated multi-owner restore."""

    state_directory: Path
    backup_directory: Path
    manifest: Path
    manifest_sha256: str
    restored: tuple[str, ...]
    pre_restore_backup: DatabaseBackupResult | None
    state_epoch: StateEpoch
    complete: bool
    publication_warning: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": DATABASE_BACKUP_SCHEMA,
            "mode": "restored",
            "state_directory": str(self.state_directory),
            "backup_directory": str(self.backup_directory),
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "restored": list(self.restored),
            "pre_restore_backup": (
                None
                if self.pre_restore_backup is None
                else self.pre_restore_backup.as_payload()
            ),
            "state_epoch": self.state_epoch.as_payload(),
            "complete": self.complete,
            "publication_warning": self.publication_warning,
        }


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise DatabasePurgeError(f"database purge path cannot contain symlinks: {path}")


def _safe_state_directory(path: str | Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise DatabasePurgeError("state directory must be absolute")
    if selected.name.casefold() != "state":
        raise DatabasePurgeError("state directory must have the final component 'state'")
    _reject_symlink_components(selected)
    normalized = Path(os.path.abspath(os.fspath(selected)))
    if normalized == Path(normalized.anchor) or normalized == Path.home():
        raise DatabasePurgeError("refusing to purge a filesystem or home root")
    try:
        if selected.exists() and not selected.is_dir():
            raise DatabasePurgeError("state directory is not a directory")
    except OSError as exc:
        raise DatabasePurgeError(f"state directory cannot be inspected: {selected}") from exc
    return normalized


def _safe_backup_directory(path: str | Path, state_directory: Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise DatabasePurgeError("backup directory must be absolute")
    _reject_symlink_components(selected)
    normalized = Path(os.path.abspath(os.fspath(selected)))
    try:
        normalized.relative_to(state_directory)
    except ValueError:
        pass
    else:
        raise DatabasePurgeError("backup directory must be outside state directory")
    if normalized == Path(normalized.anchor) or normalized == Path.home():
        raise DatabasePurgeError("refusing to use a filesystem or home root as backup")
    return normalized


def _requested_stores(stores: Sequence[str] | None) -> tuple[str, ...]:
    requested = DATABASE_STORE_NAMES if stores is None or not stores else tuple(stores)
    if len(set(requested)) != len(requested):
        raise DatabasePurgeError("database stores cannot repeat")
    unknown = sorted(set(requested) - set(DATABASE_STORE_NAMES))
    if unknown:
        raise DatabasePurgeError(f"unknown database store: {unknown[0]}")
    return tuple(name for name in DATABASE_STORE_NAMES if name in requested)


def _snapshot(path: Path, role: FileRole) -> DatabaseFileSnapshot | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatabasePurgeError(f"database file cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise DatabasePurgeError(f"refusing to purge symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise DatabasePurgeError(f"database target is not a regular file: {path}")
    return DatabaseFileSnapshot(
        path=path,
        role=role,
        size=int(metadata.st_size),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mtime_ns=int(metadata.st_mtime_ns),
        mode=int(stat.S_IMODE(metadata.st_mode)),
        uid=int(metadata.st_uid),
        gid=int(metadata.st_gid),
    )


def _lock_paths(state_directory: Path) -> tuple[Path, ...]:
    if not state_directory.is_dir():
        return ()
    paths = {
        state_directory / "framework.lock",
        state_directory / "release.lock",
    }
    paths.update(state_directory.glob("*.route.lock"))
    paths.update(state_directory.glob("watcher-life-*.lock"))
    return tuple(sorted(paths, key=os.fspath))


def _probe_lock(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    snapshot = _snapshot(path, "sidecar")
    if snapshot is None:
        return False
    if os.name == "nt":
        raise DatabasePurgeError("database purge lock probing is Linux-only")
    import fcntl

    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(descriptor)
    return False


def _lock_conflicts(state_directory: Path) -> tuple[Path, ...]:
    return tuple(path for path in _lock_paths(state_directory) if _probe_lock(path))


def _database_targets(
    state_directory: Path,
    stores: tuple[str, ...],
) -> tuple[DatabasePurgeTarget, ...]:
    by_owner = {store.state_owner_id: store for store in STATE_STORE_REGISTRY.stores}
    targets: list[DatabasePurgeTarget] = []
    for owner in stores:
        database_name = by_owner[owner].database_name
        database = state_directory / database_name
        files: list[DatabaseFileSnapshot] = []
        main = _snapshot(database, "database")
        if main is not None:
            files.append(main)
        for suffix in DATABASE_SIDECAR_SUFFIXES:
            sidecar = _snapshot(Path(f"{database}{suffix}"), "sidecar")
            if sidecar is not None:
                files.append(sidecar)
        if files:
            targets.append(DatabasePurgeTarget(owner, database_name, database, tuple(files)))
    return tuple(targets)


def _unknown_sqlite_files(state_directory: Path, stores: tuple[str, ...]) -> tuple[Path, ...]:
    if not state_directory.is_dir():
        return ()
    known_database_names = {item.database_name for item in STATE_STORE_REGISTRY.stores}
    known = {
        name
        for database_name in known_database_names
        for name in (database_name, *(database_name + suffix for suffix in DATABASE_SIDECAR_SUFFIXES))
    }
    unknown: list[Path] = []
    for path in sorted(state_directory.iterdir(), key=os.fspath):
        if path.name in known or not path.name.casefold().endswith(
            (".sqlite3", "-wal", "-shm", "-journal")
        ):
            continue
        if path.is_symlink() or path.is_file():
            unknown.append(path)
    return tuple(unknown)


def _plan_digest(
    state_directory: Path,
    stores: tuple[str, ...],
    targets: tuple[DatabasePurgeTarget, ...],
) -> str:
    payload = {
        "schema": DATABASE_PURGE_SCHEMA,
        "state_directory": str(state_directory),
        "stores": stores,
        "targets": [target.as_payload() for target in targets],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan_database_purge(
    state_directory: str | Path,
    *,
    stores: Sequence[str] | None = None,
) -> DatabasePurgePlan:
    """Build a read-only plan for canonical databases and exact sidecars."""

    selected = _safe_state_directory(state_directory)
    requested = _requested_stores(stores)
    targets = _database_targets(selected, requested)
    return DatabasePurgePlan(
        state_directory=selected,
        stores=requested,
        targets=targets,
        lock_conflicts=_lock_conflicts(selected),
        unknown_sqlite_files=_unknown_sqlite_files(selected, requested),
        plan_digest=_plan_digest(selected, requested, targets),
    )


@contextmanager
def _held_locks(state_directory: Path) -> Iterator[None]:
    if os.name == "nt":
        raise DatabasePurgeError("database purge is Linux-only")
    import fcntl

    paths = list(_lock_paths(state_directory))
    paths.extend(
        path
        for path in (state_directory / "framework.lock", state_directory / "release.lock")
        if path not in paths
    )
    paths = sorted(set(paths), key=os.fspath)
    streams: list[BinaryIO] = []
    try:
        for path in paths:
            if os.path.lexists(path) and path.is_symlink():
                raise DatabasePurgeError(f"refusing to lock symlink: {path}")
            if path.name not in {"framework.lock", "release.lock"} and not os.path.lexists(path):
                continue
            stream = open(path, "a+b", buffering=0)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                stream.close()
                raise DatabasePurgeBusyError((path,)) from exc
            streams.append(stream)
        yield
    finally:
        for held_stream in reversed(streams):
            try:
                fcntl.flock(held_stream.fileno(), fcntl.LOCK_UN)
            finally:
                held_stream.close()


def _default_backup_directory(state_directory: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return state_directory.parent / "database-backups" / f"{stamp}-{time.time_ns()}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Durably commit directory-entry changes made by maintenance actions."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as exc:
        raise DatabasePurgeError(
            f"database maintenance directory cannot be synchronized: {path}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise DatabasePurgeError(
            f"database maintenance directory cannot be synchronized: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def _manifest_sha256(path: Path) -> str:
    """Hash one already-published manifest without following links."""

    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DatabasePurgeError(f"manifest is not a regular file: {path}")
    return _sha256(path)


def _source_file_payload(item: DatabaseFileSnapshot) -> dict[str, object]:
    payload = item.as_payload()
    try:
        payload["sha256"] = _sha256(item.path)
    except OSError as exc:
        raise DatabasePurgeError(f"database file cannot be hashed: {item.path}") from exc
    return payload


def _sqlite_metadata(path: Path) -> tuple[int | None, int | None]:
    """Read version metadata from a standalone backup without creating state."""

    try:
        with immutable_sqlite_database(path, timeout_seconds=60.0) as connection:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            try:
                schema_row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                schema_row = None
            schema_version = None
            if schema_row is not None:
                try:
                    schema_version = int(schema_row[0])
                except (TypeError, ValueError):
                    schema_version = None
            return user_version, schema_version
    except (OSError, sqlite3.Error):
        return None, None


def _new_backup_directory(path: str | Path, state_directory: Path) -> Path:
    selected = _safe_backup_directory(path, state_directory)
    if os.path.lexists(selected):
        raise DatabasePurgeError(f"backup directory already exists: {selected}")
    try:
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.mkdir(mode=0o700)
        selected.chmod(0o700)
    except OSError as exc:
        raise DatabasePurgeError(
            f"database backup directory could not be created: {selected}"
        ) from exc
    return selected


def _owner_database_targets(
    state_directory: Path,
    stores: tuple[str, ...],
) -> tuple[DatabasePurgeTarget, ...]:
    """Build owner targets while preserving absent and orphaned evidence."""

    return _database_targets(state_directory, stores)


def _source_files_after_backup(
    target: DatabasePurgeTarget,
) -> tuple[DatabaseFileSnapshot, ...]:
    files: list[DatabaseFileSnapshot] = []
    for role, path in (
        ("database", target.database),
        *(
            ("sidecar", Path(f"{target.database}{suffix}"))
            for suffix in DATABASE_SIDECAR_SUFFIXES
        ),
    ):
        snapshot = _snapshot(path, role)  # type: ignore[arg-type]
        if snapshot is not None:
            files.append(snapshot)
    return tuple(files)


def _write_state_backup_manifest(
    backup_directory: Path,
    payload: dict[str, object],
) -> Path:
    path = backup_directory / "state-backup-manifest.json"
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".state-backup-manifest-",
        suffix=".json",
        dir=backup_directory,
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(
            backup_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def backup_state_owners(
    state_directory: str | Path,
    backup_directory: str | Path,
    *,
    stores: Sequence[str] | None = None,
    release_sha: str | None = None,
    integrity_mode: IntegrityCheckMode = "full",
    expected_epoch: int | None = None,
    _assume_locks_held: bool = False,
) -> DatabaseBackupResult:
    """Create and verify a bounded backup of all selected state owners.

    The operation never replaces an existing destination.  Each database is
    copied through SQLite's online backup API and is then validated with the
    requested quick/full integrity mode.  Source file identities are captured
    again after every copy, so a writer or a newly-created sidecar causes the
    set to be reported as incomplete instead of being presented as a coherent
    backup.  The publication epoch is read-only evidence and is never created
    by this function.
    """

    state = _safe_state_directory(state_directory)
    if integrity_mode not in {"quick", "full"}:
        raise ValueError("integrity_mode must be 'quick' or 'full'")
    if release_sha is not None:
        if (
            not isinstance(release_sha, str)
            or len(release_sha) != 40
            or any(character not in "0123456789abcdefABCDEF" for character in release_sha)
        ):
            raise ValueError("release_sha must be a 40-character hexadecimal SHA")
        release_sha = release_sha.lower()
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch < 0):
        raise ValueError("expected_epoch must be a non-negative integer")
    requested = _requested_stores(stores)
    conflicts = () if _assume_locks_held else _lock_conflicts(state)
    if conflicts:
        raise DatabasePurgeBusyError(conflicts)
    epoch = read_state_epoch(state)
    if expected_epoch is not None and epoch.epoch != expected_epoch:
        raise DatabasePurgeChangedError(
            f"state publication epoch changed: expected {expected_epoch}, observed {epoch.epoch}"
        )
    plan = _owner_database_targets(state, requested)
    unknown = _unknown_sqlite_files(state, requested)
    destination = _new_backup_directory(backup_directory, state)
    entries: list[DatabaseBackupEntry] = []
    complete = not unknown
    policy = SQLiteBackupPolicy(
        integrity=SQLiteIntegrityPolicy(check_mode=integrity_mode)
    )
    try:
        for target in plan:
            main = next((item for item in target.files if item.role == "database"), None)
            if main is None:
                entries.append(
                    DatabaseBackupEntry(
                        owner=target.owner,
                        database_name=target.database_name,
                        status="orphan_sidecar_only",
                        source=target.database,
                        backup=None,
                        source_files=target.files,
                        source_sha256=None,
                        backup_sha256=None,
                        backup_size=None,
                        user_version=None,
                        schema_version=None,
                        integrity=None,
                        reason="orphan_sidecar_only",
                    )
                )
                complete = False
                continue
            destination_file = destination / target.database_name
            before = target.files
            try:
                result = backup_sqlite_online(
                    main.path,
                    destination_file,
                    policy=policy,
                )
            except Exception as exc:
                raise DatabasePurgeError(
                    f"verified backup failed for owner {target.owner}: {main.path}"
                ) from exc
            after = _source_files_after_backup(target)
            before_main = next((item for item in before if item.role == "database"), None)
            after_main = next((item for item in after if item.role == "database"), None)
            if before_main != after_main:
                raise DatabasePurgeChangedError(
                    f"database target changed while backing up: {main.path}"
                )
            try:
                source_sha = _sha256(main.path)
                backup_sha = _sha256(destination_file)
                backup_size = destination_file.stat().st_size
            except OSError as exc:
                raise DatabasePurgeError(
                    f"verified backup could not be hashed: {destination_file}"
                ) from exc
            user_version, schema_version = _sqlite_metadata(destination_file)
            entries.append(
                DatabaseBackupEntry(
                    owner=target.owner,
                    database_name=target.database_name,
                    status="backed_up",
                    source=main.path,
                    backup=destination_file,
                    source_files=after,
                    source_sha256=source_sha,
                    backup_sha256=backup_sha,
                    backup_size=backup_size,
                    user_version=user_version,
                    schema_version=schema_version,
                    integrity=result.integrity,
                )
            )
            if result.integrity.check_mode != integrity_mode or not result.integrity.healthy:
                complete = False
        present_owners = {item.owner for item in entries}
        registry_by_owner = {
            store.state_owner_id: store for store in STATE_STORE_REGISTRY.stores
        }
        for owner in requested:
            if owner in present_owners:
                continue
            store = registry_by_owner[owner]
            entries.append(
                DatabaseBackupEntry(
                    owner=owner,
                    database_name=store.database_name,
                    status="absent",
                    source=state / store.database_name,
                    backup=None,
                    source_files=(),
                    source_sha256=None,
                    backup_sha256=None,
                    backup_size=None,
                    user_version=None,
                    schema_version=None,
                    integrity=None,
                )
            )
        observed_epoch = read_state_epoch(state)
        if observed_epoch != epoch:
            raise DatabasePurgeChangedError(
                f"state publication epoch changed while backing up: {epoch.epoch} to {observed_epoch.epoch}"
            )
        payload: dict[str, object] = {
            "schema": DATABASE_BACKUP_SCHEMA,
            "created_at": datetime.now(UTC).isoformat(),
            "state_directory": str(state),
            "release_sha": release_sha,
            "state_epoch": epoch.as_payload(),
            "stores": list(requested),
            "integrity_mode": integrity_mode,
            "unknown_sqlite_files": [str(path) for path in unknown],
            "entries": [item.as_payload(backup_directory=destination) for item in entries],
            "complete": complete,
        }
        manifest = _write_state_backup_manifest(destination, payload)
        return DatabaseBackupResult(
            state_directory=state,
            backup_directory=destination,
            manifest=manifest,
            manifest_sha256=_manifest_sha256(manifest),
            state_epoch=epoch,
            entries=tuple(entries),
            unknown_sqlite_files=unknown,
            complete=complete,
        )
    except BaseException:
        # A failed set is intentionally retained for diagnosis, but the source
        # is never modified by this operation.
        raise


def _load_state_backup_manifest(
    backup_directory: Path,
) -> tuple[Path, dict[str, object], str]:
    manifest = backup_directory / "state-backup-manifest.json"
    try:
        metadata = manifest.lstat()
    except FileNotFoundError as exc:
        raise DatabaseRestoreError("state backup manifest does not exist") from exc
    except OSError as exc:
        raise DatabaseRestoreError("state backup manifest cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DatabaseRestoreError("state backup manifest is not a regular file")
    if metadata.st_size > 16 * 1024 * 1024:
        raise DatabaseRestoreError("state backup manifest exceeds its bound")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DatabaseRestoreError("state backup manifest is malformed") from exc
    if not isinstance(payload, dict) or payload.get("schema") != DATABASE_BACKUP_SCHEMA:
        raise DatabaseRestoreError("state backup manifest schema is incompatible")
    return manifest, payload, _manifest_sha256(manifest)


def _manifest_epoch(payload: dict[str, object]) -> int:
    value = payload.get("state_epoch")
    if not isinstance(value, dict):
        raise DatabaseRestoreError("state backup manifest lacks a state epoch")
    epoch = value.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise DatabaseRestoreError("state backup manifest epoch is invalid")
    return epoch


def _manifest_source_mode(raw: dict[str, object], owner: str) -> int | None:
    """Read optional source permission bits while keeping v1 compatibility."""

    source_files = raw.get("source_files")
    if source_files is None:
        # Manifests produced before permission evidence was added remain
        # readable, but restore cannot claim to preserve their mode.
        return None
    if not isinstance(source_files, list):
        raise DatabaseRestoreError(f"state backup source files are invalid: {owner}")
    database_file: dict[str, object] | None = None
    for item in source_files:
        if not isinstance(item, dict):
            raise DatabaseRestoreError(f"state backup source file is invalid: {owner}")
        if item.get("role") == "database":
            if database_file is not None:
                raise DatabaseRestoreError(
                    f"state backup repeats the database source file: {owner}"
                )
            database_file = item
    if database_file is None or "mode" not in database_file:
        return None
    mode = database_file.get("mode")
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise DatabaseRestoreError(f"state backup source mode is invalid: {owner}")
    return mode


_RESTORE_SCHEMA_INITIALIZERS = {
    "inventory": (
        "neocortex.deduplication.persistence.lifecycle", "initialize_inventory_schema"
    ),
    "catalog": ("neocortex.documents.document_catalog", "initialize_document_catalog"),
    "pdf": ("neocortex.capabilities.formats.pdf.pdf_state", "initialize_pdf_state"),
    "docx": ("neocortex.capabilities.formats.docx.state", "initialize_docx_state"),
    "office": ("neocortex.capabilities.formats.office.state", "initialize_office_state"),
    "audio": ("neocortex.capabilities.formats.audio.state", "initialize_audio_state"),
    "video": ("neocortex.capabilities.formats.video.state", "initialize_video_state"),
    "image": ("neocortex.capabilities.formats.image.state", "initialize_image_state"),
    "semantic": ("neocortex.semantic.semantic_schema", "initialize_semantic_state"),
    "text": ("neocortex.capabilities.formats.text.text_state", "initialize_text_state"),
}


def _migrate_restore_staged(path: Path, owner: str) -> None:
    """Delegate compatibility and migration to the owner, on a copy only."""

    try:
        if owner == "framework":
            # Its canonical writer supplies the required route-phase backfill;
            # invoking the schema function with a no-op callback would not.
            from neocortex.persistence.framework_state_writer import FrameworkState

            with FrameworkState(path, existing_only=True):
                pass
        else:
            module_name, initializer_name = _RESTORE_SCHEMA_INITIALIZERS[owner]
            initializer = getattr(import_module(module_name), initializer_name)
            initializer(path)
    except Exception as exc:
        raise DatabaseRestoreError(
            f"restore schema failed owner validation: {owner}"
        ) from exc


def _validate_restore_schema(
    path: Path, owner: str, *, staged: bool = False, allow_migration: bool = True
) -> int:
    """Check the owner contract without migrating the only backup copy.

    Version ceilings come from the registry.  Legacy compatibility is proven
    by the owner's existing initializer, not by assuming every lower number is
    supported.  Preview runs it on a disposable copy and apply on staging;
    neither operation ever migrates the backup source or destination baseline.
    Current schemas are structurally checked through the same owner API;
    merely forging the current version marker does not make a schema valid.
    SQLite's zero user_version is permitted for metadata-only owners, not as
    evidence that an unversioned or future application schema is compatible.
    """

    supported = STATE_STORE_REGISTRY.by_owner(owner).expected_schema_version
    try:
        with immutable_sqlite_database(path, timeout_seconds=60.0) as connection:
            version = read_application_schema_version(connection, label=owner)
            pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (OSError, sqlite3.Error, SQLiteSchemaContractError, ImmutableSQLiteUnavailable) as exc:
        raise DatabaseRestoreError(f"restore schema cannot be verified: {owner}") from exc
    if version is None or version < 1 or pragma_version < 0:
        raise DatabaseRestoreError(f"restore schema is not identified: {owner}")
    if version > supported or pragma_version > supported:
        raise DatabaseRestoreError(
            f"restore schema is newer than supported: {owner}; supported={supported}"
        )
    if pragma_version not in {0, version}:
        raise DatabaseRestoreError(f"restore schema version markers disagree: {owner}")
    if not allow_migration:
        if version != supported:
            raise DatabaseRestoreError(
                f"restore schema lacks verified compatibility: {owner}; "
                f"observed={version}, supported={supported}"
            )
        return version
    if owner != "framework" and owner not in _RESTORE_SCHEMA_INITIALIZERS:
        raise DatabaseRestoreError(f"restore schema has no owner validation route: {owner}")
    if staged:
        _migrate_restore_staged(path, owner)
        return _validate_restore_schema(path, owner, allow_migration=False)
    with tempfile.TemporaryDirectory(prefix="neocortex-restore-schema-") as raw:
        copy = Path(raw) / path.name
        shutil.copyfile(path, copy)
        _migrate_restore_staged(copy, owner)
        _validate_restore_schema(copy, owner, allow_migration=False)
        if not check_sqlite_integrity(
            copy, policy=SQLiteIntegrityPolicy(check_mode="full")
        ).healthy:
            raise DatabaseRestoreError(f"restore migrated schema integrity failed: {owner}")
    return version


def _manifest_entries(
    payload: dict[str, object],
    backup_directory: Path,
    stores: tuple[str, ...],
    *,
    integrity_mode: IntegrityCheckMode = "full",
) -> tuple[dict[str, object], ...]:
    if payload.get("complete") is not True:
        raise DatabaseRestoreError("incomplete state backup cannot be restored")
    unknown = payload.get("unknown_sqlite_files")
    if not isinstance(unknown, list) or unknown:
        raise DatabaseRestoreError("state backup contains unknown SQLite files")
    entries_value = payload.get("entries")
    if not isinstance(entries_value, list):
        raise DatabaseRestoreError("state backup entries are missing")
    by_owner: dict[str, dict[str, object]] = {}
    registry_by_owner = {item.state_owner_id: item for item in STATE_STORE_REGISTRY.stores}
    for raw in entries_value:
        if not isinstance(raw, dict):
            raise DatabaseRestoreError("state backup entry is not an object")
        owner = raw.get("owner")
        database_name = raw.get("database_name")
        if not isinstance(owner, str) or owner not in registry_by_owner:
            raise DatabaseRestoreError("state backup entry has an unknown owner")
        if owner in by_owner:
            raise DatabaseRestoreError("state backup repeats an owner")
        expected_name = registry_by_owner[owner].database_name
        lifecycle = registry_by_owner[owner]
        policy_version = raw.get("lifecycle_policy_version")
        if policy_version is not None:
            if type(policy_version) is not int or policy_version != lifecycle.lifecycle_policy_version:
                raise DatabaseRestoreError(f"state backup lifecycle policy is incompatible: {owner}")
            authority_tables = sorted(rule.table for rule in lifecycle.lifecycle_rules
                                      if rule.role == "authoritative")
            if raw.get("authority_tables") != authority_tables:
                raise DatabaseRestoreError(f"state backup authority declaration differs: {owner}")
        elif "authority_tables" in raw:
            raise DatabaseRestoreError(f"state backup authority declaration lacks its policy: {owner}")
        # Older complete-owner backups remain readable under the current
        # owner's schema checks; absent metadata never supplies new authority.
        if database_name != expected_name:
            raise DatabaseRestoreError(
                f"state backup owner/database mismatch: {owner}"
            )
        status = raw.get("status")
        if status not in {"backed_up", "absent"}:
            raise DatabaseRestoreError(
                f"state backup entry cannot be restored: {owner}"
            )
        if status == "backed_up":
            raw = dict(raw)
            raw["source_mode"] = _manifest_source_mode(raw, owner)
            backup_value = raw.get("backup")
            if not isinstance(backup_value, str) or not backup_value:
                raise DatabaseRestoreError(f"state backup file is missing: {owner}")
            candidate = Path(backup_value)
            if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
                raise DatabaseRestoreError(f"state backup path escapes its directory: {owner}")
            selected = backup_directory / candidate
            try:
                selected.relative_to(backup_directory)
                metadata = selected.lstat()
            except (ValueError, FileNotFoundError) as exc:
                raise DatabaseRestoreError(f"state backup file is unavailable: {owner}") from exc
            except OSError as exc:
                raise DatabaseRestoreError(f"state backup file cannot be inspected: {owner}") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise DatabaseRestoreError(f"state backup file is not regular: {owner}")
            expected_sha = raw.get("backup_sha256")
            expected_size = raw.get("backup_size")
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                raise DatabaseRestoreError(f"state backup hash is missing: {owner}")
            if type(expected_size) is not int or expected_size <= 0:
                raise DatabaseRestoreError(f"state backup size is invalid: {owner}")
            if metadata.st_size != expected_size or _sha256(selected) != expected_sha:
                raise DatabaseRestoreError(f"state backup hash mismatch: {owner}")
            integrity = check_sqlite_integrity(
                selected,
                policy=SQLiteIntegrityPolicy(check_mode=integrity_mode),
            )
            if not integrity.healthy:
                raise DatabaseRestoreError(f"state backup integrity failed: {owner}")
            _validate_restore_schema(selected, owner)
            raw["resolved_backup"] = selected
        by_owner[owner] = raw
    missing = sorted(set(stores) - set(by_owner))
    if missing:
        raise DatabaseRestoreError(f"state backup lacks owners: {missing[0]}")
    return tuple(by_owner[owner] for owner in stores)


def _restore_live_path(state_directory: Path, database_name: str) -> Path:
    path = state_directory / database_name
    try:
        value = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise DatabaseRestoreError(f"restore target cannot be inspected: {database_name}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise DatabaseRestoreError(f"restore target is not a regular file: {database_name}")
    return path


def _restore_stage(
    entries: tuple[dict[str, object], ...],
    state_directory: Path,
) -> tuple[Path, dict[str, Path]]:
    try:
        stage_directory = Path(
            tempfile.mkdtemp(prefix=".neocortex-state-restore-", dir=state_directory.parent)
        )
        os.chmod(stage_directory, 0o700)
    except OSError as exc:
        raise DatabaseRestoreError("restore staging directory could not be created") from exc
    staged: dict[str, Path] = {}
    try:
        policy = SQLiteBackupPolicy(
            integrity=SQLiteIntegrityPolicy(check_mode="full")
        )
        for entry in entries:
            if entry.get("status") != "backed_up":
                continue
            owner = str(entry["owner"])
            database_name = str(entry["database_name"])
            source = entry.get("resolved_backup")
            if not isinstance(source, Path):
                raise DatabaseRestoreError(f"restore source is missing: {owner}")
            destination = stage_directory / database_name
            backup_sqlite_online(source, destination, policy=policy)
            _validate_restore_schema(destination, owner, staged=True)
            verification = check_sqlite_integrity(
                destination,
                policy=SQLiteIntegrityPolicy(check_mode="full"),
            )
            if not verification.healthy:
                raise DatabaseRestoreError(f"restore staging integrity failed: {owner}")
            if _sha256(source) != entry["backup_sha256"]:
                raise DatabaseRestoreError(f"restore source changed during staging: {owner}")
            source_mode = entry.get("source_mode")
            if source_mode is not None:
                if type(source_mode) is not int:
                    raise DatabaseRestoreError(
                        f"restore staging permissions are invalid: {owner}"
                    )
                try:
                    os.chmod(destination, source_mode, follow_symlinks=False)
                except OSError as exc:
                    raise DatabaseRestoreError(
                        f"restore staging permissions could not be applied: {owner}"
                    ) from exc
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            staged[owner] = destination
        _fsync_directory(stage_directory)
        _fsync_directory(stage_directory.parent)
        return stage_directory, staged
    except BaseException:
        shutil.rmtree(stage_directory, ignore_errors=True)
        raise


def _restore_owner_heads(
    state_directory: Path, owners: tuple[str, ...], *, revision: int
) -> tuple[StateOwnerHead, ...]:
    """Fingerprint exact physical owner sets, including sidecars and absence.

    No SQLite connection is opened against a live owner.  These are physical
    restore heads, not invented owner-local logical generations.
    """

    heads: list[StateOwnerHead] = []
    for owner in owners:
        database = state_directory / STATE_STORE_REGISTRY.by_owner(owner).database_name
        files: list[dict[str, object]] = []
        before: list[tuple[Path, DatabaseFileSnapshot | None]] = []
        for suffix in ("", *DATABASE_SIDECAR_SUFFIXES):
            path = database if not suffix else Path(f"{database}{suffix}")
            role: FileRole = "database" if not suffix else "sidecar"
            snapshot = _snapshot(path, role)
            before.append((path, snapshot))
            files.append(
                {
                    "suffix": suffix,
                    "sha256": None if snapshot is None else _sha256(path),
                    "mode": None if snapshot is None else snapshot.mode,
                    "size": None if snapshot is None else snapshot.size,
                    "device": None if snapshot is None else snapshot.device,
                    "inode": None if snapshot is None else snapshot.inode,
                    "uid": None if snapshot is None else snapshot.uid,
                    "gid": None if snapshot is None else snapshot.gid,
                }
            )
        for path, snapshot in before:
            role = "database" if path == database else "sidecar"
            if _snapshot(path, role) != snapshot:
                raise DatabaseRestoreError(f"restore owner changed while hashing: {owner}")
        digest = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        heads.append(StateOwnerHead(owner, revision, digest))
    return tuple(sorted(heads, key=lambda head: head.owner))


@dataclass(frozen=True, slots=True)
class _RestoreRollbackResult:
    complete: bool
    errors: tuple[str, ...]


def _restore_commit(
    entries: tuple[dict[str, object], ...],
    state_directory: Path,
    staged: dict[str, Path],
) -> tuple[Path, list[tuple[Path, Path]], list[Path]]:
    rollback_directory = Path(
        tempfile.mkdtemp(prefix=".neocortex-state-restore-old-", dir=state_directory.parent)
    )
    os.chmod(rollback_directory, 0o700)
    _fsync_directory(rollback_directory.parent)
    moved_old: list[tuple[Path, Path]] = []
    moved_new: list[Path] = []
    try:
        for entry in entries:
            owner = str(entry["owner"])
            staged_path = staged.get(owner)
            # An absent backup entry is not authority to delete a live owner.
            if staged_path is None:
                continue
            database_name = str(entry["database_name"])
            live = _restore_live_path(state_directory, database_name)
            for suffix in ("", *DATABASE_SIDECAR_SUFFIXES):
                current = live if not suffix else Path(f"{live}{suffix}")
                if not os.path.lexists(current):
                    continue
                old = rollback_directory / f"{database_name}{suffix}"
                # Record the intent first: replace can take effect and still
                # raise (or be interrupted) before Python regains control.
                moved_old.append((current, old))
                os.replace(current, old)
            moved_new.append(live)
            os.replace(staged_path, live)
        for directory in {path.parent for path in staged.values()}:
            _fsync_directory(directory)
        _fsync_directory(rollback_directory)
        _fsync_directory(state_directory)
        _fsync_directory(state_directory.parent)
        # Keep the old files until the cross-owner publication event is
        # durable.  The caller removes this directory only after that commit.
        return rollback_directory, moved_old, moved_new
    except BaseException as exc:
        reverted = _restore_revert(moved_old, moved_new, rollback_directory, exc)
        if not reverted.complete:
            raise DatabaseRestoreRecoveryRequiredError(
                "owner swap and rollback did not complete; old files retained",
                recovery_directory=rollback_directory,
            ) from exc
        raise


def _restore_revert(
    moved_old: list[tuple[Path, Path]],
    moved_new: list[Path],
    rollback_directory: Path,
    primary: BaseException,
) -> _RestoreRollbackResult:
    """Revert what was moved, retaining every unreturned recovery artifact."""

    errors: list[str] = []
    replaced = {original for original, _old in moved_old}
    for original, old in reversed(moved_old):
        try:
            if not os.path.lexists(old) and os.path.lexists(original):
                # A recorded intent may have failed before its effect.  The
                # caller still has to prove the entire baseline before abort.
                continue
            # Replace directly, rather than deleting the new file first: a
            # failed rename must leave both the old evidence and live bytes.
            os.replace(old, original)
        except OSError as rollback_error:
            errors.append(f"restore rollback failed for {original}: {rollback_error}")
    for path in reversed(moved_new):
        if path in replaced:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as rollback_error:
            errors.append(f"restore rollback removal failed for {path}: {rollback_error}")
    if not errors:
        try:
            for directory in {path.parent for path, _old in moved_old} | {
                path.parent for path in moved_new
            }:
                _fsync_directory(directory)
            _fsync_directory(rollback_directory)
            # Unknown/residual bytes must never be erased just because a move
            # did not reach the Python bookkeeping after its physical effect.
            rollback_directory.rmdir()
            _fsync_directory(rollback_directory.parent)
        except (OSError, DatabasePurgeError) as rollback_error:
            errors.append(f"restore rollback synchronization failed: {rollback_error}")
    for note in errors:
        primary.add_note(note)
    return _RestoreRollbackResult(not errors, tuple(errors))


def restore_state_owners(
    state_directory: str | Path,
    backup_directory: str | Path,
    *,
    stores: Sequence[str] | None = None,
    apply: bool = False,
    confirmation: str | None = None,
    expected_epoch: int | None = None,
    expected_manifest_sha256: str | None = None,
) -> DatabaseRestoreResult:
    """Validate or publish a complete multi-owner state backup.

    Validation is read-only by default.  Applying stages every selected
    owner, creates a verified pre-restore backup when live state exists, then
    swaps owner files under the existing state locks.  A filesystem journal
    records the cross-owner result; a failed swap restores the files moved so
    far and leaves the staging evidence for diagnosis.
    """

    state = _safe_state_directory(state_directory)
    selected = _requested_stores(stores)
    backup = _safe_backup_directory(backup_directory, state)
    if not backup.is_dir() or backup.is_symlink():
        raise DatabaseRestoreError("backup directory must be a real directory")
    manifest, payload, manifest_sha = _load_state_backup_manifest(backup)
    if expected_manifest_sha256 is not None:
        if (
            not isinstance(expected_manifest_sha256, str)
            or len(expected_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in expected_manifest_sha256
            )
            or manifest_sha != expected_manifest_sha256.lower()
        ):
            raise DatabaseRestoreError("state backup manifest hash mismatch")
    # The backup's epoch is provenance, not the destination CAS token.  A
    # historical backup is precisely what recovery after a later epoch needs.
    _manifest_epoch(payload)
    entries = _manifest_entries(payload, backup, selected)
    if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch < 0):
        raise ValueError("expected_epoch must be a non-negative integer")
    current_epoch = read_state_epoch(state)
    if expected_epoch is not None and current_epoch.epoch != expected_epoch:
        raise DatabaseRestoreError(
            f"state publication epoch changed: expected {expected_epoch}, observed {current_epoch.epoch}"
        )
    if not apply:
        if confirmation is not None:
            raise DatabaseRestoreConfirmationError("confirmation is only valid with --apply")
        return DatabaseRestoreResult(
            state_directory=state,
            backup_directory=backup,
            manifest=manifest,
            manifest_sha256=manifest_sha,
            restored=(),
            pre_restore_backup=None,
            state_epoch=current_epoch,
            complete=True,
        )
    if confirmation != DATABASE_RESTORE_CONFIRMATION:
        raise DatabaseRestoreConfirmationError(
            f"apply requires confirmation token {DATABASE_RESTORE_CONFIRMATION!r}"
        )
    with _held_locks(state):
        view = read_state_publication_state(state)
        if view.status not in {"absent", "complete"}:
            raise DatabaseRestoreError(
                "destination publication requires recovery before another restore"
            )
        locked_epoch = view.epoch
        if (
            locked_epoch.epoch != current_epoch.epoch
            or locked_epoch.event_id != current_epoch.event_id
        ):
            raise DatabaseRestoreError("state changed before restore publication")
        pre_restore: DatabaseBackupResult | None = None
        if _owner_database_targets(state, selected):
            pre_path = (
                state.parent
                / "database-backups"
                / f"pre-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{time.time_ns()}"
            )
            pre_restore = backup_state_owners(
                state,
                pre_path,
                stores=selected,
                integrity_mode="full",
                expected_epoch=locked_epoch.epoch,
                _assume_locks_held=True,
            )
            if not pre_restore.complete:
                raise DatabaseRestoreError("pre-restore backup is incomplete")
        baseline = _restore_owner_heads(state, selected, revision=locked_epoch.epoch)
        stage_directory, staged = _restore_stage(entries, state)
        key = publication_idempotency_key(
            "database-restore",
            str(state),
            str(backup),
            manifest_sha,
            selected,
            locked_epoch.epoch,
        )
        restored: tuple[str, ...] = ()
        rollback_directory: Path | None = None
        moved_old: list[tuple[Path, Path]] = []
        moved_new: list[Path] = []
        prepared: StatePublication | None = None
        committed: StatePublication | None = None
        publication_warning: str | None = None
        commit_attempted = False
        cleanup_allowed = False
        try:
            if _manifest_sha256(manifest) != manifest_sha:
                raise DatabaseRestoreError("restore manifest changed before publication")
            prepared = record_state_publication(
                state,
                operation="database-restore",
                owners=selected,
                status="partial",
                idempotency_key=key,
                expected_epoch=locked_epoch.epoch,
                manifest_sha256=manifest_sha,
                detail="restore staged; owner publication in progress",
                owner_heads=baseline,
            )
            if prepared.status != "partial":
                raise DatabaseRestoreError("restore prepare is already resolved")
            if _restore_owner_heads(state, selected, revision=locked_epoch.epoch) != baseline:
                raise DatabaseRestoreError("restore baseline changed before owner swap")
            rollback_directory, moved_old, moved_new = _restore_commit(
                entries,
                state,
                staged,
            )
            restored = tuple(
                str(entry["owner"])
                for entry in entries
                if entry.get("status") == "backed_up"
            )
            final_heads = _restore_owner_heads(
                state, selected, revision=locked_epoch.epoch + 1
            )
            commit_attempted = True
            try:
                committed = record_state_publication(
                    state,
                    operation="database-restore",
                    owners=selected,
                    status="complete",
                    idempotency_key=key,
                    expected_epoch=locked_epoch.epoch,
                    manifest_sha256=manifest_sha,
                    owner_heads=final_heads,
                )
            except StatePublicationCommitError as exc:
                if not exc.durable:
                    raise DatabaseRestoreRecoveryRequiredError(
                        "complete journal append has uncertain durability",
                        recovery_directory=rollback_directory,
                    ) from exc
                committed = exc.publication
                publication_warning = str(exc)
            final_epoch = read_state_epoch(state)
            if (
                final_epoch.event_id != committed.event_id
                or final_epoch.owner_heads != final_heads
            ):
                raise DatabaseRestoreRecoveryRequiredError(
                    "committed publication does not match the observed epoch",
                    recovery_directory=rollback_directory,
                )
            cleanup_allowed = True
        except BaseException as exc:
            # Once a complete append is visible, uncertain, or durable, the
            # old owners MUST NOT replace the new set.  Append-only history
            # cannot be undone by restoring filesystem bytes.
            if isinstance(exc, DatabaseRestoreRecoveryRequiredError):
                recovery = exc
            else:
                recovery = None
                if committed is not None or isinstance(exc, StatePublicationCommitError):
                    recovery = DatabaseRestoreRecoveryRequiredError(
                        "publication outcome requires reconciliation",
                        recovery_directory=rollback_directory,
                    )
                elif commit_attempted and prepared is not None:
                    try:
                        complete_seen = any(
                            item.status == "complete"
                            and item.idempotency_key == prepared.idempotency_key
                            for item in read_state_publications(state)
                        )
                    except StatePublicationError:
                        complete_seen = True
                    if complete_seen:
                        recovery = DatabaseRestoreRecoveryRequiredError(
                            "complete journal outcome cannot be safely undone",
                            recovery_directory=rollback_directory,
                        )
            if recovery is not None:
                recovery.stage_directory = stage_directory
                recovery.pre_restore_backup = (
                    None if pre_restore is None else pre_restore.backup_directory
                )
                if recovery is exc:
                    raise
                raise recovery from exc
            if rollback_directory is not None:
                reverted = _restore_revert(moved_old, moved_new, rollback_directory, exc)
                if not reverted.complete:
                    raise DatabaseRestoreRecoveryRequiredError(
                        "owner rollback failed; old files retained",
                        recovery_directory=rollback_directory,
                        stage_directory=stage_directory,
                        pre_restore_backup=(
                            None if pre_restore is None else pre_restore.backup_directory
                        ),
                    ) from exc
                rollback_directory = None
            try:
                observed = _restore_owner_heads(state, selected, revision=locked_epoch.epoch)
                if observed != baseline:
                    raise DatabaseRestoreError("physical owner heads do not prove rollback")
                if prepared is not None:
                    abort_state_publication(
                        state,
                        event_id=prepared.event_id,
                        observed_owner_heads=observed,
                        expected_epoch=locked_epoch.epoch,
                    )
            except (StatePublicationError, DatabasePurgeError, OSError) as abort_error:
                raise DatabaseRestoreRecoveryRequiredError(
                    "baseline or publication abort could not be verified",
                    recovery_directory=rollback_directory,
                    stage_directory=stage_directory,
                    pre_restore_backup=(
                        None if pre_restore is None else pre_restore.backup_directory
                    ),
                ) from abort_error
            cleanup_allowed = True
            if not isinstance(exc, Exception):
                raise
            raise DatabaseRestoreError(
                "restore publication journal could not be committed; rollback verified"
            ) from exc
        finally:
            if cleanup_allowed:
                if rollback_directory is not None:
                    try:
                        for _original, old in moved_old:
                            old.unlink(missing_ok=True)
                        rollback_directory.rmdir()
                        _fsync_directory(rollback_directory.parent)
                    except (OSError, DatabasePurgeError):
                        retained = f"rollback cleanup incomplete: {rollback_directory}"
                        publication_warning = (
                            retained if publication_warning is None
                            else f"{publication_warning}; {retained}"
                        )
                shutil.rmtree(stage_directory, ignore_errors=True)
    return DatabaseRestoreResult(
        state_directory=state,
        backup_directory=backup,
        manifest=manifest,
        manifest_sha256=manifest_sha,
        restored=restored,
        pre_restore_backup=pre_restore,
        state_epoch=final_epoch,
        complete=True,
        publication_warning=publication_warning,
    )


def _integrity_payload(report: SQLiteIntegrityReport) -> dict[str, object]:
    """Convert the bounded SQLite integrity report to JSON-safe evidence."""
    return report.as_payload()


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    descriptor, raw_path = tempfile.mkstemp(prefix=".database-purge-", suffix=".json", dir=path)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path / "database-purge-manifest.json")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _same_snapshot(expected: DatabaseFileSnapshot, path: Path) -> bool:
    current = _snapshot(path, expected.role)
    return current == expected


def _require_post_backup_source_stable(
    target: DatabasePurgeTarget,
    after: tuple[DatabaseFileSnapshot, ...],
) -> None:
    """Allow only the empty sidecars SQLite may create during a read.

    The online backup source can cause SQLite to materialize an empty WAL/SHM
    pair.  Those files must be recaptured before purge; a new non-empty WAL or
    a changed pre-existing sidecar is evidence of a concurrent writer and is
    never silently deleted.
    """

    before_by_path = {item.path: item for item in target.files}
    after_by_path = {item.path: item for item in after}
    for path, previous in before_by_path.items():
        current = after_by_path.get(path)
        if current is None:
            raise DatabasePurgeChangedError(
                f"database sidecar disappeared while backing up: {path}"
            )
        if current != previous:
            raise DatabasePurgeChangedError(
                f"database sidecar changed while backing up: {path}"
            )
    for current in after:
        if current.role != "sidecar" or current.path in before_by_path:
            continue
        if current.path.name.endswith("-wal") and current.size > 0:
            raise DatabasePurgeBusyError((current.path,))
        if current.path.name.endswith("-journal") and current.size > 0:
            raise DatabasePurgeBusyError((current.path,))


def _prepare_backup(
    plan: DatabasePurgePlan,
    backup_directory: Path,
) -> tuple[Path, dict[str, object]]:
    if backup_directory.exists():
        raise DatabasePurgeError(f"backup directory already exists: {backup_directory}")
    initial_epoch = read_state_epoch(plan.state_directory)
    try:
        backup_directory.parent.mkdir(parents=True, exist_ok=True)
        backup_directory.mkdir(mode=0o700)
        backup_directory.chmod(0o700)
    except OSError as exc:
        raise DatabasePurgeError(
            f"database purge backup directory could not be created: {backup_directory}"
        ) from exc
    entries: list[dict[str, object]] = []
    policy = SQLiteBackupPolicy(
        integrity=SQLiteIntegrityPolicy(check_mode="full")
    )
    try:
        for target in plan.targets:
            main = next((item for item in target.files if item.role == "database"), None)
            if main is None:
                raise DatabasePurgeError(
                    f"cannot purge orphan sidecars without a database backup: {target.database}"
                )
            destination = backup_directory / target.database_name
            try:
                result = backup_sqlite_online(main.path, destination, policy=policy)
            except Exception as exc:
                raise DatabasePurgeError(
                    f"verified backup failed for {main.path}"
                ) from exc
            if not result.integrity.healthy:
                raise DatabasePurgeError(f"backup integrity failed: {main.path}")
            after = _source_files_after_backup(target)
            _require_post_backup_source_stable(target, after)
            try:
                source_sha256 = _sha256(main.path)
                backup_size = destination.stat().st_size
                backup_sha256 = _sha256(destination)
            except OSError as exc:
                raise DatabasePurgeError(
                    f"verified backup could not be re-read: {destination}"
                ) from exc
            entries.append(
                {
                    "owner": target.owner,
                    "database": str(main.path),
                    "backup": str(destination),
                    "source_size": main.size,
                    "source_sha256": source_sha256,
                    "backup_size": backup_size,
                    "backup_sha256": backup_sha256,
                    "source_files_after_backup": [_source_file_payload(item) for item in after],
                    "user_version": _sqlite_metadata(destination)[0],
                    "schema_version": _sqlite_metadata(destination)[1],
                    "integrity_mode": result.integrity.check_mode,
                    "integrity": _integrity_payload(result.integrity),
                }
            )
    except BaseException:
        # Keep a failed backup directory for diagnosis; no source is deleted.
        raise
    observed_epoch = read_state_epoch(plan.state_directory)
    if observed_epoch != initial_epoch:
        raise DatabasePurgeChangedError(
            "state publication epoch changed while preparing database backup"
        )
    manifest_payload: dict[str, object] = {
        "schema": DATABASE_PURGE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "state_directory": str(plan.state_directory),
        "stores": list(plan.stores),
        "plan_digest": plan.plan_digest,
        "integrity_mode": "full",
        "state_epoch": initial_epoch.as_payload(),
        "entries": entries,
    }
    try:
        _write_manifest(backup_directory, manifest_payload)
    except OSError as exc:
        raise DatabasePurgeError("database purge backup manifest could not be written") from exc
    return backup_directory, manifest_payload


def _delete_planned_files(plan: DatabasePurgePlan) -> tuple[DatabaseFileSnapshot, ...]:
    deleted: list[DatabaseFileSnapshot] = []
    for item in sorted(plan.files, key=lambda value: (value.role == "database", os.fspath(value.path))):
        if not _same_snapshot(item, item.path):
            raise DatabasePurgeChangedError(f"database target changed: {item.path}")
        try:
            item.path.unlink()
        except FileNotFoundError as exc:
            raise DatabasePurgeChangedError(f"database target disappeared: {item.path}") from exc
        except OSError as exc:
            raise DatabasePurgeError(f"database target could not be removed: {item.path}") from exc
        if item.path.exists() or item.path.is_symlink():
            raise DatabasePurgeError(f"database target remains after removal: {item.path}")
        deleted.append(item)
    if deleted:
        _fsync_directory(plan.state_directory)
    return tuple(deleted)


def _reconcile_post_backup_plan(
    before: DatabasePurgePlan,
    after: DatabasePurgePlan,
) -> DatabasePurgePlan:
    """Adopt sidecars materialized by backup without accepting owner drift."""

    if before.stores != after.stores:
        raise DatabasePurgeChangedError("database store selection changed after backup")
    if before.unknown_sqlite_files != after.unknown_sqlite_files:
        raise DatabasePurgeChangedError("unknown SQLite files changed after backup")
    before_targets = {target.owner: target for target in before.targets}
    after_targets = {target.owner: target for target in after.targets}
    if set(before_targets) != set(after_targets):
        raise DatabasePurgeChangedError("database owner set changed after backup")
    for owner, previous in before_targets.items():
        current = after_targets[owner]
        if previous.database != current.database or previous.database_name != current.database_name:
            raise DatabasePurgeChangedError(f"database target changed after backup: {owner}")
        previous_main = next((item for item in previous.files if item.role == "database"), None)
        current_main = next((item for item in current.files if item.role == "database"), None)
        if previous_main != current_main:
            raise DatabasePurgeChangedError(f"database target changed after backup: {owner}")
        _require_post_backup_source_stable(previous, current.files)
    return replace(after, lock_conflicts=())


def _refresh_purge_manifest(
    manifest: Path,
    *,
    initial_plan: DatabasePurgePlan,
    final_plan: DatabasePurgePlan,
) -> None:
    """Bind the purge manifest to the exact sidecar set about to be removed."""

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DatabasePurgeError("database purge manifest could not be reread") from exc
    if not isinstance(payload, dict):
        raise DatabasePurgeError("database purge manifest is not an object")
    payload["initial_plan_digest"] = initial_plan.plan_digest
    payload["plan_digest"] = final_plan.plan_digest
    payload["post_backup_targets"] = [
        target.as_payload() for target in final_plan.targets
    ]
    payload["post_backup_file_count"] = len(final_plan.files)
    payload["post_backup_total_bytes"] = final_plan.total_bytes
    try:
        _write_manifest(manifest.parent, payload)
    except OSError as exc:
        raise DatabasePurgeError("database purge manifest could not be refreshed") from exc


def execute_database_purge(
    state_directory: str | Path,
    *,
    stores: Sequence[str] | None = None,
    backup_directory: str | Path | None = None,
    apply: bool = False,
    confirmation: str | None = None,
) -> DatabasePurgePlan | DatabasePurgeResult:
    """Preview or apply an explicit backup-first purge of canonical state."""

    plan = plan_database_purge(state_directory, stores=stores)
    if not apply:
        if confirmation is not None:
            raise DatabasePurgeConfirmationError(
                "confirmation is only valid with --apply"
            )
        return plan
    if confirmation != DATABASE_PURGE_CONFIRMATION:
        raise DatabasePurgeConfirmationError(
            f"apply requires confirmation token {DATABASE_PURGE_CONFIRMATION!r}"
        )
    if plan.lock_conflicts:
        raise DatabasePurgeBusyError(plan.lock_conflicts)
    if not plan.files:
        return DatabasePurgeResult(plan, None, None, ())
    with _held_locks(plan.state_directory):
        locked_plan = plan_database_purge(plan.state_directory, stores=plan.stores)
        if locked_plan.plan_digest != plan.plan_digest:
            raise DatabasePurgeChangedError("database purge plan changed before backup")
        # The command itself holds every known lock at this point.  Keep the
        # applied receipt free of those self-held lock paths.
        locked_plan = replace(locked_plan, lock_conflicts=())
        selected_backup = _safe_backup_directory(
            _default_backup_directory(plan.state_directory)
            if backup_directory is None
            else backup_directory,
            plan.state_directory,
        )
        backup_path, _manifest_payload = _prepare_backup(locked_plan, selected_backup)
        # SQLite may have materialized an empty WAL/SHM while creating the
        # verified copy.  Recapture the complete target set before deletion so
        # those files cannot become orphaned; any non-empty new sidecar is
        # treated as a concurrent writer and aborts the purge.
        post_backup_plan = _reconcile_post_backup_plan(
            locked_plan,
            plan_database_purge(locked_plan.state_directory, stores=locked_plan.stores),
        )
        _refresh_purge_manifest(
            backup_path / "database-purge-manifest.json",
            initial_plan=locked_plan,
            final_plan=post_backup_plan,
        )
        deleted = _delete_planned_files(post_backup_plan)
        return DatabasePurgeResult(
            post_backup_plan,
            backup_path,
            backup_path / "database-purge-manifest.json",
            deleted,
        )


__all__ = [
    "DATABASE_BACKUP_SCHEMA",
    "DATABASE_PURGE_CONFIRMATION",
    "DATABASE_PURGE_SCHEMA",
    "DATABASE_RESTORE_CONFIRMATION",
    "DATABASE_SIDECAR_SUFFIXES",
    "DATABASE_STORE_NAMES",
    "DatabaseBackupEntry",
    "DatabaseBackupResult",
    "DatabaseFileSnapshot",
    "DatabasePurgeBusyError",
    "DatabasePurgeChangedError",
    "DatabasePurgeConfirmationError",
    "DatabasePurgeError",
    "DatabasePurgePlan",
    "DatabasePurgeResult",
    "DatabasePurgeTarget",
    "DatabaseRestoreConfirmationError",
    "DatabaseRestoreError",
    "DatabaseRestoreRecoveryRequiredError",
    "DatabaseRestoreResult",
    "backup_state_owners",
    "execute_database_purge",
    "plan_database_purge",
    "restore_state_owners",
]
