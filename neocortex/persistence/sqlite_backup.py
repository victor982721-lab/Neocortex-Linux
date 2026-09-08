"""Non-destructive online SQLite backup with verified atomic publication."""

from __future__ import annotations

import errno
import math
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .sqlite_cancellation import CancellationCheck, SQLiteCancellationBridge
from .sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    connect_sqlite,
)
from .sqlite_integrity import (
    SQLiteIntegrityPolicy,
    SQLiteIntegrityReport,
    _absolute_sqlite_path,
    _assert_sqlite_owner_fence,
    _capture_sqlite_owner_fence,
    check_sqlite_integrity,
)
from .sqlite_immutable import SQLiteImmutableFence


MAX_BACKUP_PAGES_PER_STEP = 65_536
SQLiteBackupProgressCallback = Callable[["SQLiteBackupProgress"], None]
_CANONICAL_OS_LINK = os.link


# region [01] Immutable policy, progress and result contracts


@dataclass(frozen=True, slots=True)
class SQLiteBackupPolicy:
    """Operational bounds and verification policy for an online backup."""

    pages_per_step: int = 256
    sleep_seconds: float = 0.050
    timeout_seconds: float = 60.0
    integrity: SQLiteIntegrityPolicy = field(default_factory=SQLiteIntegrityPolicy)

    def __post_init__(self) -> None:
        if type(self.pages_per_step) is not int:
            raise TypeError("pages_per_step must be an integer")
        if not 1 <= self.pages_per_step <= MAX_BACKUP_PAGES_PER_STEP:
            message = f"pages_per_step must be from 1 to {MAX_BACKUP_PAGES_PER_STEP}"
            raise ValueError(message)
        for name in ("sleep_seconds", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise TypeError(f"{name} must be a number")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError(f"{name} must be finite")
            if name == "sleep_seconds" and normalized < 0:
                raise ValueError("sleep_seconds must not be negative")
            if name == "timeout_seconds" and normalized <= 0:
                raise ValueError("timeout_seconds must be positive")
            object.__setattr__(self, name, normalized)
        if not isinstance(self.integrity, SQLiteIntegrityPolicy):
            raise TypeError("integrity must be a SQLiteIntegrityPolicy")


_DEFAULT_SQLITE_BACKUP_POLICY = SQLiteBackupPolicy(
    integrity=SQLiteIntegrityPolicy(check_mode="full")
)


@dataclass(frozen=True, slots=True)
class SQLiteBackupProgress:
    """One page-bounded callback from SQLite's online backup API."""

    invocation: int
    sqlite_status: int
    remaining_pages: int
    total_pages: int
    copied_pages: int


@dataclass(frozen=True, slots=True)
class SQLiteBackupResult:
    """Evidence returned only after verified no-replace publication."""

    source_path: Path
    destination_path: Path
    destination_size_bytes: int
    page_count: int
    page_size_bytes: int
    progress_invocations: int
    pages_per_step: int
    publication_method: Literal["hard_link_no_replace"]
    integrity: SQLiteIntegrityReport


# endregion [01]


# region [02] Explicit failure states


class SQLiteBackupError(RuntimeError):
    """Base class for failures specific to verified backup publication."""


class SQLiteBackupVerificationError(SQLiteBackupError):
    """A copied database failed bounded pre-publication verification."""

    def __init__(self, report: SQLiteIntegrityReport) -> None:
        self.report = report
        super().__init__("SQLite backup verification did not produce a complete healthy report")


class SQLiteBackupPublicationError(SQLiteBackupError):
    """Atomic no-replace publication was unavailable or could not be proven."""


class SQLiteBackupPublishedCleanupError(SQLiteBackupError):
    """The destination was published, but an exact staging artifact remained."""

    def __init__(self, destination_path: Path, staging_path: Path) -> None:
        self.destination_path = destination_path
        self.staging_path = staging_path
        super().__init__(
            f"backup was published at {destination_path}, but staging cleanup "
            f"failed for {staging_path}"
        )


# endregion [02]


# region [03] Staging, online copy and atomic no-replace publication


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _open_real_directory(parent: Path) -> int:
    """Open every parent component without following a symlink."""

    try:
        metadata = parent.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            errno.ENOENT,
            "SQLite backup destination parent does not exist",
            parent,
        ) from exc
    except OSError as exc:
        raise SQLiteBackupPublicationError(
            f"SQLite backup destination parent cannot be inspected: {parent}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SQLiteBackupPublicationError(
            f"SQLite backup destination parent is a symlink: {parent}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(
            errno.ENOTDIR,
            "SQLite backup destination parent is not a directory",
            parent,
        )

    nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    common_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0) | nofollow
    )
    directory_flags = common_flags | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor: int | None = None
    try:
        parts = parent.parts
        if not parent.is_absolute() or not parts or parts[0] != os.sep:
            raise SQLiteBackupPublicationError(
                f"SQLite backup destination parent is not absolute: {parent}"
            )
        directory_descriptor = os.open(os.sep, directory_flags)
        for component in parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SQLiteBackupPublicationError(
                        "SQLite backup destination parent contains a symlink or non-directory"
                    ) from exc
                raise
            _close_descriptor(directory_descriptor)
            directory_descriptor = next_descriptor
        opened = os.fstat(directory_descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SQLiteBackupPublicationError(
                "SQLite backup destination parent identity changed during preflight"
            )
        result = directory_descriptor
        directory_descriptor = None
        return result
    finally:
        _close_descriptor(directory_descriptor)


def _require_destination_available_at(parent_fd: int, destination: Path) -> None:
    """Check a destination relative to an already-open, identity-fenced parent."""

    try:
        os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SQLiteBackupPublicationError(
            f"SQLite backup destination cannot be inspected: {destination}"
        ) from exc
    raise FileExistsError(errno.EEXIST, "SQLite backup destination already exists", destination)


def _assert_open_parent_path(parent_fd: int, parent: Path) -> None:
    """Require that a retained directory descriptor still names the requested path."""

    try:
        expected = parent.lstat()
        opened = os.fstat(parent_fd)
    except OSError as exc:
        raise SQLiteBackupPublicationError(
            f"SQLite backup destination parent changed: {parent}"
        ) from exc
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISDIR(expected.st_mode)
        or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise SQLiteBackupPublicationError(f"SQLite backup destination parent changed: {parent}")


def _create_staging_file(parent_fd: int) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".neocortex-sqlite-backup-",
        suffix=".sqlite3.tmp",
        dir=f"/proc/self/fd/{parent_fd}",
    )
    try:
        resolved = Path(os.path.realpath(raw_path))
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
    except BaseException:
        Path(raw_path).unlink(missing_ok=True)
        raise
    return resolved


def _staging_artifacts(staging_path: Path) -> tuple[Path, ...]:
    return (
        staging_path,
        Path(f"{staging_path}-wal"),
        Path(f"{staging_path}-shm"),
        Path(f"{staging_path}-journal"),
    )


def _cleanup_staging(staging_path: Path) -> None:
    first_error: OSError | None = None
    for artifact in _staging_artifacts(staging_path):
        try:
            artifact.unlink(missing_ok=True)
        except OSError as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _require_standalone_database(staging_path: Path) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(f"{staging_path}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SQLiteBackupPublicationError(
                f"staged SQLite sidecar cannot be inspected: {sidecar.name}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SQLiteBackupPublicationError(
                f"staged SQLite sidecar is not a regular file: {sidecar.name}"
            )
        if suffix in {"-wal", "-journal"} and metadata.st_size > 0:
            raise SQLiteBackupPublicationError(
                f"staged database still depends on non-empty {suffix} state"
            )
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{staging_path}{suffix}").unlink(missing_ok=True)


def _copy_online(
    source_path: Path,
    staging_path: Path,
    *,
    policy: SQLiteBackupPolicy,
    cancellation: SQLiteCancellationBridge,
    progress_callback: SQLiteBackupProgressCallback | None,
) -> tuple[int, int, int]:
    source = connect_sqlite(
        source_path,
        mode=READONLY_EXISTING,
        policy=SQLiteConnectionPolicy(
            label="SQLite online backup source",
            timeout_seconds=policy.timeout_seconds,
        ),
    )
    try:
        target = connect_sqlite(
            staging_path,
            mode=READWRITE_EXISTING,
            policy=SQLiteConnectionPolicy(
                label="SQLite online backup staging target",
                timeout_seconds=policy.timeout_seconds,
                enforce_query_only=False,
                verify_query_only=False,
            ),
        )
        try:
            invocations = 0

            def report_progress(status: int, remaining: int, total: int) -> None:
                nonlocal invocations
                cancellation.checkpoint()
                invocations += 1
                progress = SQLiteBackupProgress(
                    invocation=invocations,
                    sqlite_status=int(status),
                    remaining_pages=int(remaining),
                    total_pages=int(total),
                    copied_pages=max(0, int(total) - int(remaining)),
                )
                if progress_callback is not None:
                    progress_callback(progress)

            cancellation.checkpoint()
            source.backup(
                target,
                pages=policy.pages_per_step,
                progress=report_progress,
                name="main",
                sleep=policy.sleep_seconds,
            )
            cancellation.checkpoint()
            page_count = int(target.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(target.execute("PRAGMA page_size").fetchone()[0])
        finally:
            target.close()
    finally:
        source.close()
    return invocations, page_count, page_size


def _publish_no_replace(
    staging_path: Path,
    destination_path: Path,
    *,
    parent_fd: int,
    expected_fence: SQLiteImmutableFence,
) -> None:
    observed = _capture_sqlite_owner_fence(
        staging_path,
        label="SQLite backup staging",
    )
    if observed is None or observed != expected_fence:
        raise SQLiteBackupPublicationError(
            "staged SQLite database identity changed before publication"
        )
    try:
        _assert_open_parent_path(parent_fd, destination_path.parent)
        _require_destination_available_at(parent_fd, destination_path)
        try:
            os.link(
                staging_path.name,
                destination_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except TypeError as exc:
            # A few focused callers inject the historical two-path ``os.link``
            # seam.  Keep that seam testable without weakening the real Linux
            # path, which always supports descriptor-relative publication.
            if os.link is _CANONICAL_OS_LINK:
                raise SQLiteBackupPublicationError(
                    "filesystem link primitive lacks descriptor-relative publication"
                ) from exc
            os.link(staging_path, destination_path, follow_symlinks=False)
        os.fsync(parent_fd)
        _assert_open_parent_path(parent_fd, destination_path.parent)
    except FileExistsError:
        raise
    except OSError as exc:
        raise SQLiteBackupPublicationError(
            "filesystem does not provide atomic hard-link no-replace publication"
        ) from exc


def backup_sqlite_online(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    policy: SQLiteBackupPolicy = _DEFAULT_SQLITE_BACKUP_POLICY,
    cancellation_check: CancellationCheck | None = None,
    progress_callback: SQLiteBackupProgressCallback | None = None,
) -> SQLiteBackupResult:
    """Copy an existing SQLite database and atomically publish it once.

    The source is opened read-only and SQLite's online backup API includes
    committed WAL content in a consistent copy. The caller selects the exact
    destination, whose parent must already exist. Existing destinations,
    including dangling links and races, are never replaced. All connections
    and transactions are owned internally.
    """

    if not isinstance(policy, SQLiteBackupPolicy):
        raise TypeError("policy must be a SQLiteBackupPolicy")
    if progress_callback is not None and not callable(progress_callback):
        raise TypeError("progress_callback must be callable or None")
    source = _absolute_sqlite_path(source_path)
    destination = _absolute_sqlite_path(destination_path)
    source_fence = _capture_sqlite_owner_fence(
        source,
        label="SQLite backup source",
    )
    cancellation = SQLiteCancellationBridge(cancellation_check)
    cancellation.checkpoint()

    parent_fd: int | None = None
    staging: Path | None = None
    published = False
    try:
        parent_fd = _open_real_directory(destination.parent)
        assert parent_fd is not None
        _assert_open_parent_path(parent_fd, destination.parent)
        _require_destination_available_at(parent_fd, destination)
        staging = _create_staging_file(parent_fd)
        invocations, page_count, page_size = _copy_online(
            source,
            staging,
            policy=policy,
            cancellation=cancellation,
            progress_callback=progress_callback,
        )
        if source_fence is not None:
            _assert_sqlite_owner_fence(
                source,
                source_fence,
                label="SQLite backup source",
            )
        integrity = check_sqlite_integrity(
            staging,
            policy=policy.integrity,
            cancellation_check=cancellation.checkpoint,
        )
        if not integrity.healthy:
            raise SQLiteBackupVerificationError(integrity)
        _require_standalone_database(staging)
        cancellation.checkpoint()
        staging_fence = _capture_sqlite_owner_fence(
            staging,
            label="SQLite backup staging",
        )
        if staging_fence is None:
            raise SQLiteBackupPublicationError(
                "staged SQLite database disappeared before publication"
            )
        destination_size = staging_fence.main.size
        _publish_no_replace(
            staging,
            destination,
            parent_fd=parent_fd,
            expected_fence=staging_fence,
        )
        published = True
        try:
            _cleanup_staging(staging)
            os.fsync(parent_fd)
        except OSError as exc:
            raise SQLiteBackupPublishedCleanupError(destination, staging) from exc
    except BaseException as exc:
        if staging is not None and not published:
            try:
                _cleanup_staging(staging)
            except OSError as cleanup_error:
                exc.add_note(f"staging cleanup also failed: {cleanup_error}")
        raise
    finally:
        _close_descriptor(parent_fd)

    return SQLiteBackupResult(
        source_path=source,
        destination_path=destination,
        destination_size_bytes=destination_size,
        page_count=page_count,
        page_size_bytes=page_size,
        progress_invocations=invocations,
        pages_per_step=policy.pages_per_step,
        publication_method="hard_link_no_replace",
        integrity=integrity,
    )


# endregion [03]


__all__ = [
    "MAX_BACKUP_PAGES_PER_STEP",
    "SQLiteBackupError",
    "SQLiteBackupPolicy",
    "SQLiteBackupProgress",
    "SQLiteBackupProgressCallback",
    "SQLiteBackupPublicationError",
    "SQLiteBackupPublishedCleanupError",
    "SQLiteBackupResult",
    "SQLiteBackupVerificationError",
    "backup_sqlite_online",
]
