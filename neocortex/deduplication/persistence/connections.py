"""SQLite connection policy for the deduplication inventory owner."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from neocortex.sqlite_schema_lifecycle import existing_sqlite_uri, readonly_sqlite_uri

from ..domain.errors import InventoryError


class ConnectionFactory(Protocol):
    """Callable accepted by the versioned schema lifecycle."""

    def __call__(
        self,
        path: Path,
        *,
        readonly: bool = False,
    ) -> sqlite3.Connection: ...


def configure_owner_connection(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
) -> sqlite3.Connection:
    """Apply invariants required before a connection can own inventory state."""

    try:
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise InventoryError("dedup inventory could not enable foreign keys")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise InventoryError("dedup inventory connection is not query-only")
    except BaseException:
        connection.close()
        raise
    return connection


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a lifecycle connection, creating state only in writable mode."""

    if readonly:
        connection = sqlite3.connect(readonly_sqlite_uri(path), uri=True, timeout=60.0)
    else:
        connection = sqlite3.connect(path, timeout=60.0)
    return configure_owner_connection(connection, readonly=readonly)


def connect_existing_inventory_database(path: Path) -> sqlite3.Connection:
    """Open an accepted inventory database without recreating missing state."""

    connection = sqlite3.connect(existing_sqlite_uri(path), uri=True, timeout=60.0)
    return configure_owner_connection(connection, readonly=False)


def configure_inventory_connection(connection: sqlite3.Connection) -> None:
    """Apply bounded operational settings after the schema is accepted."""

    for statement in (
        "PRAGMA busy_timeout=60000",
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-32768",
        "PRAGMA wal_autocheckpoint=4096",
        "PRAGMA journal_size_limit=268435456",
    ):
        connection.execute(statement)
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise InventoryError("dedup inventory connection has foreign keys disabled")
