"""Non-destructive online SQLite backup with verified atomic publication."""

from __future__ import annotations

import errno
import math
import os
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
    check_sqlite_integrity,
)


MAX_BACKUP_PAGES_PER_STEP = 65_536
SQLiteBackupProgressCallback = Callable[["SQLiteBackupProgress"], None]


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
        super().__init__(
            "SQLite backup verification did not produce a complete healthy report"
        )


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


def _path_exists_including_dangling_links(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _require_destination_available(destination: Path) -> None:
    if _path_exists_including_dangling_links(destination):
        raise FileExistsError(
            errno.EEXIST,
            "SQLite backup destination already exists",
            destination,
        )
    parent = destination.parent
    if not parent.exists():
        raise FileNotFoundError(
            errno.ENOENT,
            "SQLite backup destination parent does not exist",
            parent,
        )
    if not parent.is_dir():
        raise NotADirectoryError(
            errno.ENOTDIR,
            "SQLite backup destination parent is not a directory",
            parent,
        )


def _create_staging_file(parent: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".neocortex-sqlite-backup-",
        suffix=".sqlite3.tmp",
        dir=parent,
    )
    try:
        os.close(descriptor)
    except BaseException:
        Path(raw_path).unlink(missing_ok=True)
        raise
    return Path(raw_path)


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
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{staging_path}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size > 0:
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


def _publish_no_replace(staging_path: Path, destination_path: Path) -> None:
    try:
        os.link(staging_path, destination_path, follow_symlinks=False)
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
    source = Path(source_path).resolve(strict=False)
    destination = Path(os.path.abspath(os.fspath(destination_path)))
    _require_destination_available(destination)
    cancellation = SQLiteCancellationBridge(cancellation_check)
    cancellation.checkpoint()

    staging: Path | None = None
    published = False
    try:
        staging = _create_staging_file(destination.parent)
        invocations, page_count, page_size = _copy_online(
            source,
            staging,
            policy=policy,
            cancellation=cancellation,
            progress_callback=progress_callback,
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
        destination_size = staging.stat().st_size
        _publish_no_replace(staging, destination)
        published = True
        try:
            _cleanup_staging(staging)
        except OSError as exc:
            raise SQLiteBackupPublishedCleanupError(destination, staging) from exc
    except BaseException as exc:
        if staging is not None and not published:
            try:
                _cleanup_staging(staging)
            except OSError as cleanup_error:
                exc.add_note(f"staging cleanup also failed: {cleanup_error}")
        raise

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
