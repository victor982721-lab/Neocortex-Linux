"""Explicit, backup-first removal of NeoCortex SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from neocortex.persistence.sqlite_backup import backup_sqlite_online
from neocortex.persistence.sqlite_integrity import SQLiteIntegrityReport
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


DATABASE_PURGE_SCHEMA = "neocortex.database-purge/v1"
DATABASE_PURGE_CONFIRMATION = "DELETE_DATABASES"
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


FileRole = Literal["database", "sidecar"]


@dataclass(frozen=True, slots=True)
class DatabaseFileSnapshot:
    path: Path
    role: FileRole
    size: int
    device: int
    inode: int
    mtime_ns: int

    def as_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "role": self.role,
            "size": self.size,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
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


def _integrity_payload(report: SQLiteIntegrityReport) -> dict[str, object]:
    """Convert the bounded SQLite integrity report to JSON-safe evidence."""

    return {
        "database_path": str(report.database_path),
        "quick_check_errors": list(report.quick_check_errors),
        "quick_check_observed_error_count": report.quick_check_observed_error_count,
        "quick_check_complete": report.quick_check_complete,
        "foreign_key_violations": [
            asdict(item) for item in report.foreign_key_violations
        ],
        "foreign_key_observed_violation_count": report.foreign_key_observed_violation_count,
        "foreign_key_check_complete": report.foreign_key_check_complete,
        "healthy": report.healthy,
    }


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


def _prepare_backup(
    plan: DatabasePurgePlan,
    backup_directory: Path,
) -> tuple[Path, dict[str, object]]:
    if backup_directory.exists():
        raise DatabasePurgeError(f"backup directory already exists: {backup_directory}")
    try:
        backup_directory.parent.mkdir(parents=True, exist_ok=True)
        backup_directory.mkdir()
    except OSError as exc:
        raise DatabasePurgeError(
            f"database purge backup directory could not be created: {backup_directory}"
        ) from exc
    entries: list[dict[str, object]] = []
    try:
        for target in plan.targets:
            main = next((item for item in target.files if item.role == "database"), None)
            if main is None:
                entries.append(
                    {
                        "owner": target.owner,
                        "database": str(target.database),
                        "backup": None,
                        "reason": "orphan_sidecar_only",
                    }
                )
                continue
            destination = backup_directory / target.database_name
            try:
                result = backup_sqlite_online(main.path, destination)
            except Exception as exc:
                raise DatabasePurgeError(
                    f"verified backup failed for {main.path}"
                ) from exc
            if not result.integrity.healthy:
                raise DatabasePurgeError(f"backup integrity failed: {main.path}")
            try:
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
                    "backup_size": backup_size,
                    "backup_sha256": backup_sha256,
                    "integrity": _integrity_payload(result.integrity),
                }
            )
    except BaseException:
        # Keep a failed backup directory for diagnosis; no source is deleted.
        raise
    manifest_payload: dict[str, object] = {
        "schema": DATABASE_PURGE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "state_directory": str(plan.state_directory),
        "stores": list(plan.stores),
        "plan_digest": plan.plan_digest,
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
    return tuple(deleted)


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
        # Recheck every source after the backup and before the first unlink.
        for item in locked_plan.files:
            if not _same_snapshot(item, item.path):
                raise DatabasePurgeChangedError(f"database target changed after backup: {item.path}")
        deleted = _delete_planned_files(locked_plan)
        return DatabasePurgeResult(locked_plan, backup_path, backup_path / "database-purge-manifest.json", deleted)


__all__ = [
    "DATABASE_PURGE_CONFIRMATION",
    "DATABASE_PURGE_SCHEMA",
    "DATABASE_SIDECAR_SUFFIXES",
    "DATABASE_STORE_NAMES",
    "DatabaseFileSnapshot",
    "DatabasePurgeBusyError",
    "DatabasePurgeChangedError",
    "DatabasePurgeConfirmationError",
    "DatabasePurgeError",
    "DatabasePurgePlan",
    "DatabasePurgeResult",
    "DatabasePurgeTarget",
    "execute_database_purge",
    "plan_database_purge",
]
