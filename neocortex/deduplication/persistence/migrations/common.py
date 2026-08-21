"""Shared, transaction-neutral operations for sequential inventory migrations."""

from __future__ import annotations

import sqlite3

from ...domain.errors import InventoryError
from ..ddl import (
    SCAN_COUNTER_COLUMNS,
    SCAN_ROOT_COLUMNS,
    V2_OBJECT_DDL,
    execute_ddl,
)


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def add_columns(
    connection: sqlite3.Connection,
    table: str,
    definitions: tuple[tuple[str, str], ...],
) -> None:
    present = column_names(connection, table)
    for name, definition in definitions:
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_v2_objects(connection: sqlite3.Connection) -> None:
    execute_ddl(connection, V2_OBJECT_DDL)


def add_scan_counters(connection: sqlite3.Connection) -> None:
    add_columns(connection, "scans", SCAN_COUNTER_COLUMNS)


def add_fingerprint_birthtime(connection: sqlite3.Connection) -> None:
    add_columns(
        connection,
        "fingerprints",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )


def add_scan_root_identity(connection: sqlite3.Connection) -> None:
    add_columns(connection, "scans", SCAN_ROOT_COLUMNS)


def invalidate_checkpoints(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE inventory_checkpoints SET valid=0 WHERE valid<>0")


def advance_version(connection: sqlite3.Connection, version: int) -> None:
    cursor = connection.execute(
        "UPDATE metadata SET value=? WHERE key='schema_version'",
        (str(version),),
    )
    if cursor.rowcount != 1:
        raise InventoryError("dedup inventory metadata lost its schema version")
