"""Detached snapshots supplied by an existing, explicitly owned SQLite writer.

This is not an alternative read mode for an uncoordinated published owner.
Only its writer may lend the connection, with no transaction in progress. The
backup pins one committed SQLite view and never opens a source-side reader.
"""

from __future__ import annotations

import math
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    capture_sqlite_immutable_fence,
)


def _require_owner_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ImmutableSQLiteUnavailable(
            "SQLite coordinated snapshot owner is unavailable"
        ) from exc
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != expected:
        raise ImmutableSQLiteUnavailable("SQLite coordinated snapshot owner identity changed")


@contextmanager
def writer_coordinated_sqlite_snapshot(
    connection: sqlite3.Connection,
    source: Path,
    *,
    owner_identity: tuple[int, int],
    timeout_seconds: float = 60.0,
    temp_root: Path | None = None,
) -> Iterator[Path]:
    """Publish one complete, checked temporary snapshot for parallel readers.

    The caller must own ``connection`` on the current thread and retain its
    main-file identity from connection acquisition. A pinned read transaction
    allows other WAL writers (including heartbeat) to proceed without mixing
    candidate generations. A pending transaction is rejected rather than
    committed, rolled back, or passed to SQLite backup, which can deadlock on
    its own source write transaction. Source physical replacement is rejected;
    ordinary writes remain the SQLite owner's responsibility, not a relaxed
    filesystem fence on ``SQLiteReadSession``.
    """

    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("SQLite coordinated snapshot timeout must be finite and positive")
    if connection.in_transaction:
        raise ImmutableSQLiteUnavailable(
            "SQLite coordinated snapshot requires an idle owner transaction"
        )
    deadline = time.monotonic() + timeout_seconds
    source = Path(source).absolute()
    _require_owner_identity(source, owner_identity)
    if temp_root is not None:
        root = Path(temp_root)
        if not root.is_dir() or root.is_symlink():
            raise ImmutableSQLiteUnavailable("SQLite snapshot temp root is not a real directory")

    def check_budget(_status: int = 0, _remaining: int = 0, _total: int = 0) -> None:
        if time.monotonic() >= deadline:
            raise ImmutableSQLiteUnavailable("SQLite coordinated snapshot exceeded its time budget")

    with tempfile.TemporaryDirectory(
        prefix="neocortex-route-snapshot-", dir=temp_root
    ) as directory:
        destination = Path(directory) / source.name
        began_read = False
        previous_busy_timeout: int | None = None
        try:
            check_budget()
            previous_busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            remaining_ms = max(1, math.floor((deadline - time.monotonic()) * 1000))
            connection.execute(f"PRAGMA busy_timeout={min(previous_busy_timeout, remaining_ms)}")
            main = next(
                (row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"),
                None,
            )
            if main is None or Path(main).absolute() != source:
                raise ImmutableSQLiteUnavailable(
                    "SQLite coordinated snapshot connection does not own source"
                )
            check_budget()
            connection.execute("BEGIN")
            began_read = True
            # BEGIN alone is deferred: step a main-database query to establish
            # the read version before backup and before concurrent WAL writes.
            connection.execute("SELECT rootpage FROM sqlite_schema LIMIT 1").fetchone()
            check_budget()
            with closing(sqlite3.connect(destination)) as target:
                target.execute("PRAGMA trusted_schema=OFF")
                connection.backup(target, pages=256, progress=check_budget, sleep=0.01)
                check_budget()
                # The output must be self-contained before any worker sees it.
                if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise ImmutableSQLiteUnavailable(
                        "SQLite coordinated snapshot is not self-contained"
                    )
                target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                try:
                    integrity = target.execute("PRAGMA quick_check").fetchall()
                finally:
                    target.set_progress_handler(None, 0)
                if integrity != [("ok",)]:
                    raise ImmutableSQLiteUnavailable(
                        "SQLite coordinated snapshot integrity check failed"
                    )
                check_budget()
        except sqlite3.Error as exc:
            if (
                getattr(exc, "sqlite_errorcode", None)
                in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                and time.monotonic() >= deadline
            ):
                message = "SQLite coordinated snapshot exceeded its time budget"
            else:
                message = f"SQLite coordinated snapshot could not be completed: {exc}"
            raise ImmutableSQLiteUnavailable(message) from exc
        finally:
            primary = sys.exception()
            cleanup_error: sqlite3.Error | None = None
            if began_read:
                try:
                    connection.rollback()
                except sqlite3.Error as exc:
                    cleanup_error = exc
            if previous_busy_timeout is not None:
                try:
                    connection.execute(f"PRAGMA busy_timeout={previous_busy_timeout}")
                except sqlite3.Error as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                    else:
                        cleanup_error.add_note(
                            f"SQLite snapshot busy timeout restore failed: {exc}"
                        )
            if cleanup_error is not None:
                if primary is None:
                    raise ImmutableSQLiteUnavailable(
                        "SQLite coordinated snapshot could not restore its owner connection"
                    ) from cleanup_error
                primary.add_note(f"SQLite snapshot owner cleanup failed: {cleanup_error}")
        _require_owner_identity(source, owner_identity)
        capture_sqlite_immutable_fence(destination)
        yield destination


__all__ = ["writer_coordinated_sqlite_snapshot"]
