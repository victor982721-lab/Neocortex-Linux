"""Bounded, read-only SQLite integrity inspection with explicit completeness."""

from __future__ import annotations

import errno
import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .sqlite_cancellation import (
    CancellationCheck,
    DEFAULT_PROGRESS_INSTRUCTIONS,
    SQLiteCancellationBridge,
    sqlite_cancellation_scope,
)
from .sqlite_connection import (
    READONLY_EXISTING,
    SQLiteConnectionPolicy,
    connect_sqlite,
)
from .sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    capture_sqlite_read_fence,
)


MAX_REPORTED_ISSUES = 10_000
IntegrityCheckMode = Literal["quick", "full"]


# region [01] Immutable policy and result contracts


def _require_issue_limit(value: int, *, name: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_REPORTED_ISSUES:
        raise ValueError(f"{name} must be an integer from 1 to {MAX_REPORTED_ISSUES}")


@dataclass(frozen=True, slots=True)
class SQLiteIntegrityPolicy:
    """Resource and reporting limits for one consistent integrity snapshot."""

    max_quick_check_errors: int = 100
    max_foreign_key_violations: int = 100
    progress_instructions: int = DEFAULT_PROGRESS_INSTRUCTIONS
    timeout_seconds: float = 60.0
    check_mode: IntegrityCheckMode = "quick"

    def __post_init__(self) -> None:
        _require_issue_limit(
            self.max_quick_check_errors,
            name="max_quick_check_errors",
        )
        _require_issue_limit(
            self.max_foreign_key_violations,
            name="max_foreign_key_violations",
        )
        if type(self.progress_instructions) is not int:
            raise TypeError("progress_instructions must be an integer")
        if self.progress_instructions <= 0:
            raise ValueError("progress_instructions must be positive")
        if isinstance(self.timeout_seconds, bool):
            raise TypeError("timeout_seconds must be a number")
        timeout_seconds = float(self.timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        if self.check_mode not in {"quick", "full"}:
            raise ValueError("check_mode must be 'quick' or 'full'")


_DEFAULT_SQLITE_INTEGRITY_POLICY = SQLiteIntegrityPolicy()


@dataclass(frozen=True, slots=True, order=True)
class SQLiteForeignKeyViolation:
    """One row from SQLite's ``foreign_key_check`` diagnostic."""

    table: str
    rowid: int | None
    parent: str
    foreign_key_index: int


@dataclass(frozen=True, slots=True)
class SQLiteIntegrityReport:
    """Bounded integrity evidence for one read transaction.

    An observed count is exact only when its corresponding ``*_complete`` flag
    is true. Otherwise it is a lower bound and retained details were truncated.
    """

    database_path: Path
    quick_check_errors: tuple[str, ...]
    quick_check_observed_error_count: int
    quick_check_complete: bool
    foreign_key_violations: tuple[SQLiteForeignKeyViolation, ...]
    foreign_key_observed_violation_count: int
    foreign_key_check_complete: bool
    check_mode: IntegrityCheckMode = "quick"
    integrity_check_errors: tuple[str, ...] = ()
    integrity_check_observed_error_count: int = 0
    integrity_check_complete: bool = True

    @property
    def quick_check_truncated(self) -> bool:
        return not self.quick_check_complete

    @property
    def foreign_key_check_truncated(self) -> bool:
        return not self.foreign_key_check_complete

    @property
    def integrity_check_truncated(self) -> bool:
        return not self.integrity_check_complete

    @property
    def complete(self) -> bool:
        return (
            self.quick_check_complete
            and self.foreign_key_check_complete
            and (self.check_mode != "full" or self.integrity_check_complete)
        )

    @property
    def healthy(self) -> bool:
        return (
            self.complete
            and not self.quick_check_errors
            and not self.foreign_key_violations
            and (self.check_mode != "full" or not self.integrity_check_errors)
        )

    def as_payload(self) -> dict[str, object]:
        """Return bounded JSON-safe integrity evidence."""

        return {
            "database_path": str(self.database_path),
            "check_mode": self.check_mode,
            "quick_check_errors": list(self.quick_check_errors),
            "quick_check_observed_error_count": self.quick_check_observed_error_count,
            "quick_check_complete": self.quick_check_complete,
            "foreign_key_violations": [
                {
                    "table": item.table,
                    "rowid": item.rowid,
                    "parent": item.parent,
                    "foreign_key_index": item.foreign_key_index,
                }
                for item in self.foreign_key_violations
            ],
            "foreign_key_observed_violation_count": self.foreign_key_observed_violation_count,
            "foreign_key_check_complete": self.foreign_key_check_complete,
            "integrity_check_errors": list(self.integrity_check_errors),
            "integrity_check_observed_error_count": self.integrity_check_observed_error_count,
            "integrity_check_complete": self.integrity_check_complete,
            "healthy": self.healthy,
        }


# endregion [01]


# region [01b] Path safety and owner fencing


def _absolute_sqlite_path(value: str | Path) -> Path:
    """Normalize a path lexically without resolving a symlink endpoint."""

    return Path(os.path.abspath(os.fspath(value)))


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _open_regular_sqlite_descriptor(
    path: Path,
    *,
    expected: os.stat_result,
    label: str,
) -> None:
    """Open one owner through no-following directory descriptors.

    The SQLite connection itself is opened by the shared fenced reader.  This
    preflight closes the race between the lexical ``lstat`` and that open: a
    swapped ancestor or endpoint is rejected by ``O_NOFOLLOW`` and the
    descriptor identity is compared with the observed entry.
    """

    nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    common_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | nofollow
    )
    directory_flags = common_flags | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        parts = path.parts
        if not path.is_absolute() or len(parts) < 2:
            raise ImmutableSQLiteUnavailable(f"{label} path is not absolute")
        directory_descriptor = os.open(os.sep, directory_flags)
        for component in parts[1:-1]:
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            _close_descriptor(directory_descriptor)
            directory_descriptor = next_descriptor
        file_descriptor = os.open(
            parts[-1],
            common_flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ImmutableSQLiteUnavailable(f"{label} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ImmutableSQLiteUnavailable(f"{label} identity changed during preflight")
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ImmutableSQLiteUnavailable(
                f"{label} path contains a symlink or non-directory"
            ) from exc
        if exc.errno in {errno.ENOENT, errno.ESTALE}:
            raise ImmutableSQLiteUnavailable(f"{label} disappeared during preflight") from exc
        raise
    finally:
        _close_descriptor(file_descriptor)
        _close_descriptor(directory_descriptor)


def _capture_sqlite_owner_fence(
    path: Path,
    *,
    label: str,
) -> SQLiteImmutableFence | None:
    """Reject linked/non-regular owners and capture their physical fence.

    Missing sources intentionally return ``None`` so the existing connection
    policy can preserve its ``sqlite3.OperationalError`` contract without
    creating a file.  Every existing endpoint is opened with ``O_NOFOLLOW``
    before SQLite is allowed to inspect it.
    """

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(f"{label} cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ImmutableSQLiteUnavailable(f"{label} is a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ImmutableSQLiteUnavailable(f"{label} is not a regular file: {path}")
    _open_regular_sqlite_descriptor(path, expected=metadata, label=label)
    try:
        fence = capture_sqlite_read_fence(path)
    except FileNotFoundError as exc:
        raise ImmutableSQLiteUnavailable(f"{label} disappeared during preflight") from exc
    if (fence.main.device, fence.main.inode) != (metadata.st_dev, metadata.st_ino):
        raise ImmutableSQLiteUnavailable(f"{label} identity changed during preflight")
    return fence


def _assert_sqlite_owner_fence(
    path: Path,
    expected: SQLiteImmutableFence,
    *,
    label: str,
) -> None:
    """Require the same physical owner and sidecar fence after a read."""

    observed = _capture_sqlite_owner_fence(path, label=label)
    if observed is None or observed != expected:
        raise ImmutableSQLiteUnavailable(f"{label} changed during fenced inspection")


# endregion [01b]


# region [02] Bounded diagnostics


def _quick_check(
    connection: sqlite3.Connection,
    *,
    maximum_errors: int,
    cancellation: SQLiteCancellationBridge,
) -> tuple[tuple[str, ...], int, bool]:
    retained: list[str] = []
    observed_errors = 0
    observed_rows = 0
    sqlite_limit = maximum_errors + 1
    cursor = connection.execute(f"PRAGMA quick_check({sqlite_limit})")
    try:
        for row in cursor:
            cancellation.checkpoint()
            observed_rows += 1
            message = str(row[0])
            if message.casefold() == "ok":
                continue
            observed_errors += 1
            if len(retained) < maximum_errors:
                retained.append(message)
    finally:
        cursor.close()
    cancellation.checkpoint()
    if observed_rows == 0:
        retained.append("quick_check returned no result")
        observed_errors = 1
    complete = observed_errors <= maximum_errors
    return tuple(retained), observed_errors, complete


def _foreign_key_check(
    connection: sqlite3.Connection,
    *,
    maximum_violations: int,
    cancellation: SQLiteCancellationBridge,
) -> tuple[tuple[SQLiteForeignKeyViolation, ...], int, bool]:
    retained: list[SQLiteForeignKeyViolation] = []
    observed_violations = 0
    cursor = connection.execute("PRAGMA foreign_key_check")
    try:
        for row in cursor:
            cancellation.checkpoint()
            observed_violations += 1
            if len(retained) < maximum_violations:
                retained.append(
                    SQLiteForeignKeyViolation(
                        table=str(row[0]),
                        rowid=None if row[1] is None else int(row[1]),
                        parent=str(row[2]),
                        foreign_key_index=int(row[3]),
                    )
                )
            if observed_violations > maximum_violations:
                break
    finally:
        cursor.close()
    cancellation.checkpoint()
    complete = observed_violations <= maximum_violations
    return tuple(retained), observed_violations, complete


def _integrity_check(
    connection: sqlite3.Connection,
    *,
    maximum_errors: int,
    cancellation: SQLiteCancellationBridge,
) -> tuple[tuple[str, ...], int, bool]:
    """Run SQLite's exhaustive structural check with a bounded result set."""

    retained: list[str] = []
    observed_errors = 0
    observed_rows = 0
    sqlite_limit = maximum_errors + 1
    cursor = connection.execute(f"PRAGMA integrity_check({sqlite_limit})")
    try:
        for row in cursor:
            cancellation.checkpoint()
            observed_rows += 1
            message = str(row[0])
            if message.casefold() == "ok":
                continue
            observed_errors += 1
            if len(retained) < maximum_errors:
                retained.append(message)
    finally:
        cursor.close()
    cancellation.checkpoint()
    if observed_rows == 0:
        retained.append("integrity_check returned no result")
        observed_errors = 1
    complete = observed_errors <= maximum_errors
    return tuple(retained), observed_errors, complete


def check_sqlite_integrity(
    database_path: str | Path,
    *,
    policy: SQLiteIntegrityPolicy = _DEFAULT_SQLITE_INTEGRITY_POLICY,
    mode: IntegrityCheckMode | None = None,
    cancellation_check: CancellationCheck | None = None,
) -> SQLiteIntegrityReport:
    """Inspect one existing database without creating or mutating it.

    Both diagnostics run in one explicit read transaction. The function owns
    and closes that connection; it never commits, checkpoints WAL or changes a
    caller-owned transaction.
    """

    if not isinstance(policy, SQLiteIntegrityPolicy):
        raise TypeError("policy must be a SQLiteIntegrityPolicy")
    if mode is not None and mode not in {"quick", "full"}:
        raise ValueError("mode must be 'quick' or 'full'")
    check_mode = policy.check_mode if mode is None else mode
    path = _absolute_sqlite_path(database_path)
    owner_fence = _capture_sqlite_owner_fence(
        path,
        label="SQLite integrity owner",
    )
    connection = connect_sqlite(
        path,
        mode=READONLY_EXISTING,
        policy=SQLiteConnectionPolicy(
            label="SQLite integrity inspection",
            timeout_seconds=policy.timeout_seconds,
        ),
    )
    cancellation = SQLiteCancellationBridge(cancellation_check)
    try:
        with sqlite_cancellation_scope(
            connection,
            cancellation,
            instructions=policy.progress_instructions,
        ):
            cancellation.checkpoint()
            connection.execute("BEGIN")
            try:
                quick_errors, quick_observed, quick_complete = _quick_check(
                    connection,
                    maximum_errors=policy.max_quick_check_errors,
                    cancellation=cancellation,
                )
                violations, violations_observed, violations_complete = (
                    _foreign_key_check(
                        connection,
                        maximum_violations=policy.max_foreign_key_violations,
                        cancellation=cancellation,
                    )
                )
                if check_mode == "full":
                    integrity_errors, integrity_observed, integrity_complete = (
                        _integrity_check(
                            connection,
                            maximum_errors=policy.max_quick_check_errors,
                            cancellation=cancellation,
                        )
                    )
                else:
                    integrity_errors, integrity_observed, integrity_complete = (), 0, True
            finally:
                if connection.in_transaction:
                    connection.rollback()
    finally:
        connection.close()
    if owner_fence is not None:
        _assert_sqlite_owner_fence(
            path,
            owner_fence,
            label="SQLite integrity owner",
        )
    return SQLiteIntegrityReport(
        database_path=path,
        quick_check_errors=quick_errors,
        quick_check_observed_error_count=quick_observed,
        quick_check_complete=quick_complete,
        foreign_key_violations=violations,
        foreign_key_observed_violation_count=violations_observed,
        foreign_key_check_complete=violations_complete,
        check_mode=check_mode,
        integrity_check_errors=integrity_errors,
        integrity_check_observed_error_count=integrity_observed,
        integrity_check_complete=integrity_complete,
    )


# endregion [02]


__all__ = [
    "MAX_REPORTED_ISSUES",
    "IntegrityCheckMode",
    "SQLiteForeignKeyViolation",
    "SQLiteIntegrityPolicy",
    "SQLiteIntegrityReport",
    "check_sqlite_integrity",
]
