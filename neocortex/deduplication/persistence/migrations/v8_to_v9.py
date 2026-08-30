"""Inventory schema migration from v8 to optional USN cursors in v9."""

from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v8_schema_contract
from ..ddl import (
    PATH_COLLATION,
    SCHEMA_LABEL,
    V9_CHECKPOINT_DDL,
    V9_PLANNED_MEMBERS_PATH_INDEX_DDL,
)


def migrate(connection: sqlite3.Connection) -> None:
    """Decouple snapshot publication from the optional USN acceleration cursor."""

    validate_sqlite_schema_contract(
        connection,
        inventory_v8_schema_contract(),
        label=f"{SCHEMA_LABEL} v8 migration source",
        exact=True,
    )
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    connection.execute("ALTER TABLE inventory_checkpoints RENAME TO inventory_checkpoints_v8")
    connection.execute(V9_CHECKPOINT_DDL)
    connection.execute(
        """INSERT INTO inventory_checkpoints(
        root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
        SELECT root,scan_id,volume,journal_id,next_usn,valid,updated_ns
        FROM inventory_checkpoints_v8"""
    )
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v9 publication count changed during migration")
    connection.execute("DROP TABLE inventory_checkpoints_v8")
    if PATH_COLLATION != "NOCASE":
        connection.execute("DROP INDEX planned_members_path_idx")
        connection.execute(V9_PLANNED_MEMBERS_PATH_INDEX_DDL)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v9 foreign-key validation failed")
