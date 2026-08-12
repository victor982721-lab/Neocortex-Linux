"""Fenced immutable SQLite reads that never create or update sidecars."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .sqlite_paths import readonly_sqlite_uri

_INACTIVE_SHM_SIZE_BYTES = 32_768


class ImmutableSQLiteUnavailable(RuntimeError):
    """A database cannot be proven safe for an immutable read."""


@dataclass(frozen=True, slots=True)
class SQLiteFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class SQLiteImmutableFence:
    main: SQLiteFileIdentity
    sidecars: tuple[tuple[str, SQLiteFileIdentity], ...]


def _file_identity(
    path: Path,
    *,
    label: str,
    allow_empty: bool = False,
) -> SQLiteFileIdentity:
    try:
        value = path.stat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(f"{label} cannot be inspected: {path.name}") from exc
    if (
        not stat.S_ISREG(value.st_mode)
        or path.is_symlink()
        or (value.st_size <= 0 and not allow_empty)
    ):
        raise ImmutableSQLiteUnavailable(f"{label} is not a stable regular file: {path.name}")
    return SQLiteFileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        mtime_ns=int(value.st_mtime_ns),
        ctime_ns=int(value.st_ctime_ns),
    )


def capture_sqlite_immutable_fence(path: Path) -> SQLiteImmutableFence:
    """Capture main/sidecar identities without opening SQLite."""

    selected = Path(path)
    main = _file_identity(selected, label="SQLite owner")
    sidecars: list[tuple[str, SQLiteFileIdentity]] = []
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(f"{selected}{suffix}")
        try:
            identity = _file_identity(
                candidate,
                label=f"SQLite sidecar {suffix}",
                allow_empty=True,
            )
        except FileNotFoundError:
            continue
        sidecars.append((suffix, identity))
    fence = SQLiteImmutableFence(main=main, sidecars=tuple(sidecars))
    require_inactive_sqlite_sidecars(fence)
    return fence


def require_inactive_sqlite_sidecars(fence: SQLiteImmutableFence) -> None:
    """Accept no sidecars or the exact inactive WAL/SHM layout SQLite leaves."""

    sidecars = dict(fence.sidecars)
    journal = sidecars.get("-journal")
    wal = sidecars.get("-wal")
    shm = sidecars.get("-shm")
    if journal is not None and journal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty rollback journal")
    if wal is not None and wal.size > 0:
        raise ImmutableSQLiteUnavailable("SQLite owner has a non-empty WAL")
    if not sidecars:
        return
    if (
        set(sidecars) == {"-wal", "-shm"}
        and wal is not None
        and wal.size == 0
        and shm is not None
        and shm.size == _INACTIVE_SHM_SIZE_BYTES
    ):
        return
    raise ImmutableSQLiteUnavailable("SQLite owner sidecars are not proven inactive")


@contextmanager
def immutable_sqlite_database(
    path: Path,
    *,
    timeout_seconds: float = 60.0,
) -> Iterator[sqlite3.Connection]:
    """Read one stable owner without creating, deleting, or touching sidecars."""

    if timeout_seconds <= 0:
        raise ValueError("immutable SQLite timeout must be positive")
    selected = Path(path)
    before = capture_sqlite_immutable_fence(selected)
    confirmed = capture_sqlite_immutable_fence(selected)
    if before != confirmed:
        raise ImmutableSQLiteUnavailable("SQLite owner changed before immutable read")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{readonly_sqlite_uri(selected)}&immutable=1",
            uri=True,
            timeout=timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        safeguards = (
            connection.execute("PRAGMA foreign_keys").fetchone(),
            connection.execute("PRAGMA query_only").fetchone(),
            connection.execute("PRAGMA trusted_schema").fetchone(),
        )
        if (
            safeguards[0] is None
            or int(safeguards[0][0]) != 1
            or safeguards[1] is None
            or int(safeguards[1][0]) != 1
            or safeguards[2] is None
            or int(safeguards[2][0]) != 0
        ):
            raise ImmutableSQLiteUnavailable("SQLite immutable safeguards are unavailable")
        yield connection
    finally:
        if connection is not None:
            connection.close()
        after = capture_sqlite_immutable_fence(selected)
        if before != after:
            raise ImmutableSQLiteUnavailable("SQLite owner changed during immutable read")


__all__ = [
    "ImmutableSQLiteUnavailable",
    "SQLiteFileIdentity",
    "SQLiteImmutableFence",
    "capture_sqlite_immutable_fence",
    "immutable_sqlite_database",
    "require_inactive_sqlite_sidecars",
]
