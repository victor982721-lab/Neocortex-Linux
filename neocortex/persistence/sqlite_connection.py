"""Shared low-level SQLite connection policy without transaction ownership."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from neocortex.persistence.sqlite_paths import existing_sqlite_uri, readonly_sqlite_uri


SQLiteRowFactory = Callable[[sqlite3.Cursor, tuple[Any, ...]], Any]


# region [01] Explicit modes and validated policy values


class SQLiteOpenMode(Enum):
    """Filesystem behavior for one SQLite connection."""

    READONLY_EXISTING = "readonly_existing"
    READWRITE_EXISTING = "readwrite_existing"
    READWRITE_CREATE = "readwrite_create"


READONLY_EXISTING: Final = SQLiteOpenMode.READONLY_EXISTING
READWRITE_EXISTING: Final = SQLiteOpenMode.READWRITE_EXISTING
READWRITE_CREATE: Final = SQLiteOpenMode.READWRITE_CREATE

_JOURNAL_MODES = frozenset({"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"})
_SYNCHRONOUS_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
# Keep the historical injected-connection seam used by focused tests and
# embedders.  The production branch below never enters it because the module
# function remains the original sqlite3.connect object.
_CANONICAL_SQLITE_CONNECT = sqlite3.connect


def _require_positive_integer(value: int | None, *, name: str) -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SQLiteWriterPragmas:
    """Bounded writer settings expressed in their operational units."""

    journal_mode: str | None = None
    synchronous: str | None = None
    cache_size_kib: int | None = None
    wal_autocheckpoint_pages: int | None = None
    journal_size_limit_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.journal_mode is not None:
            journal_mode = self.journal_mode.upper()
            if journal_mode not in _JOURNAL_MODES:
                raise ValueError(
                    f"unsupported SQLite journal mode: {self.journal_mode}"
                )
            object.__setattr__(self, "journal_mode", journal_mode)
        if self.synchronous is not None:
            synchronous = self.synchronous.upper()
            if synchronous not in _SYNCHRONOUS_MODES:
                raise ValueError(
                    f"unsupported SQLite synchronous mode: {self.synchronous}"
                )
            object.__setattr__(self, "synchronous", synchronous)
        _require_positive_integer(self.cache_size_kib, name="cache_size_kib")
        _require_positive_integer(
            self.wal_autocheckpoint_pages,
            name="wal_autocheckpoint_pages",
        )
        _require_positive_integer(
            self.journal_size_limit_bytes,
            name="journal_size_limit_bytes",
        )


@dataclass(frozen=True, slots=True)
class SQLiteConnectionPolicy:
    """Connection-local safeguards and optional owner-specific writer settings."""

    label: str
    timeout_seconds: float = 60.0
    row_factory: SQLiteRowFactory | None = None
    enable_foreign_keys: bool = True
    verify_foreign_keys: bool = True
    enforce_query_only: bool = True
    verify_query_only: bool = True
    writer_pragmas: SQLiteWriterPragmas | None = None

    def __post_init__(self) -> None:
        if not self.label or self.label.strip() != self.label:
            raise ValueError("SQLite policy label must be non-empty and trimmed")
        if isinstance(self.timeout_seconds, bool):
            raise ValueError("SQLite timeout must be a finite positive number")
        timeout_seconds = float(self.timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("SQLite timeout must be a finite positive number")
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        if self.row_factory is not None and not callable(self.row_factory):
            raise TypeError("SQLite row_factory must be callable or None")
        for name in (
            "enable_foreign_keys",
            "verify_foreign_keys",
            "enforce_query_only",
            "verify_query_only",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if self.verify_foreign_keys and not self.enable_foreign_keys:
            raise ValueError("foreign-key verification requires enabling foreign keys")
        if self.verify_query_only and not self.enforce_query_only:
            raise ValueError(
                "query-only verification requires enforcing query-only mode"
            )


# endregion [01]


# region [02] Opening and connection-local configuration


def _open_sqlite(
    path: Path,
    *,
    mode: SQLiteOpenMode,
    timeout_seconds: float,
) -> sqlite3.Connection:
    if mode is READONLY_EXISTING:
        if sqlite3.connect is not _CANONICAL_SQLITE_CONNECT:
            return sqlite3.connect(
                readonly_sqlite_uri(path),
                uri=True,
                timeout=timeout_seconds,
            )
        # All owner reads pass through the fenced kernel.  It returns an
        # immutable connection for a quiescent owner and an owned temporary
        # snapshot when a WAL/sidecar is active, so the generic policy cannot
        # accidentally materialize SQLite sidecars.
        from neocortex.persistence.sqlite_immutable import (
            open_sidecar_safe_sqlite_connection,
        )

        try:
            return open_sidecar_safe_sqlite_connection(
                path,
                timeout_seconds=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    if mode is READWRITE_EXISTING:
        return sqlite3.connect(
            existing_sqlite_uri(path),
            uri=True,
            timeout=timeout_seconds,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=timeout_seconds)


def _pragma_is_enabled(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    return row is not None and int(row[0]) == 1


def _configure_writer(
    connection: sqlite3.Connection,
    pragmas: SQLiteWriterPragmas,
) -> None:
    if pragmas.journal_mode is not None:
        connection.execute(f"PRAGMA journal_mode={pragmas.journal_mode}")
    if pragmas.synchronous is not None:
        connection.execute(f"PRAGMA synchronous={pragmas.synchronous}")
    if pragmas.cache_size_kib is not None:
        connection.execute(f"PRAGMA cache_size=-{pragmas.cache_size_kib}")
    if pragmas.wal_autocheckpoint_pages is not None:
        connection.execute(
            f"PRAGMA wal_autocheckpoint={pragmas.wal_autocheckpoint_pages}"
        )
    if pragmas.journal_size_limit_bytes is not None:
        connection.execute(
            f"PRAGMA journal_size_limit={pragmas.journal_size_limit_bytes}"
        )


def _disable_checkpoint_on_readonly_close(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> None:
    """Keep a WAL reader from checkpointing owner bytes when it closes."""

    option = sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE
    connection.setconfig(option, True)
    if not connection.getconfig(option):
        raise RuntimeError(f"{label} could not disable checkpoint-on-close")


def connect_sqlite(
    path: str | Path,
    *,
    mode: SQLiteOpenMode,
    policy: SQLiteConnectionPolicy,
) -> sqlite3.Connection:
    """Open and configure SQLite while leaving transaction ownership to the caller."""

    if not isinstance(mode, SQLiteOpenMode):
        raise TypeError("mode must be a SQLiteOpenMode")
    connection = _open_sqlite(
        Path(path),
        mode=mode,
        timeout_seconds=policy.timeout_seconds,
    )
    try:
        if mode is READONLY_EXISTING:
            _disable_checkpoint_on_readonly_close(
                connection,
                label=policy.label,
            )
        if policy.row_factory is not None:
            connection.row_factory = policy.row_factory
        busy_timeout_ms = max(1, round(policy.timeout_seconds * 1000))
        connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        if policy.enable_foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")
        if policy.verify_foreign_keys and not _pragma_is_enabled(
            connection,
            "foreign_keys",
        ):
            raise RuntimeError(f"{policy.label} could not enable foreign keys")
        if mode is READONLY_EXISTING:
            if policy.enforce_query_only:
                connection.execute("PRAGMA query_only=ON")
            if policy.verify_query_only and not _pragma_is_enabled(
                connection,
                "query_only",
            ):
                raise RuntimeError(f"{policy.label} could not enforce query-only mode")
        elif policy.writer_pragmas is not None:
            _configure_writer(connection, policy.writer_pragmas)
    except BaseException:
        connection.close()
        raise
    return connection


__all__ = [
    "READONLY_EXISTING",
    "READWRITE_CREATE",
    "READWRITE_EXISTING",
    "SQLiteConnectionPolicy",
    "SQLiteOpenMode",
    "SQLiteRowFactory",
    "SQLiteWriterPragmas",
    "connect_sqlite",
]


# endregion [02]
