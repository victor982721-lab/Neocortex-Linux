"""Inventory schema migration from v7 to policy-bound scans in v8."""

from __future__ import annotations

import sqlite3

from neocortex.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v7_schema_contract
from ..ddl import SCHEMA_LABEL
from .common import add_columns, invalidate_checkpoints


def migrate(connection: sqlite3.Connection) -> None:
    """Bind future scans to policy signatures without inventing legacy evidence."""

    validate_sqlite_schema_contract(
        connection,
        inventory_v7_schema_contract(),
        label=f"{SCHEMA_LABEL} v7 migration source",
        exact=True,
    )
    scan_count = int(connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    file_count, total_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()

    add_columns(
        connection,
        "scans",
        (("inventory_policy_signature", "TEXT"),),
    )
    invalidate_checkpoints(connection)

    if int(connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0]) != scan_count:
        raise InventoryError("dedup inventory v8 scan count changed during migration")
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v8 checkpoint count changed during migration")
    migrated_file_count, migrated_total_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()
    if (int(migrated_file_count), int(migrated_total_bytes)) != (
        int(file_count),
        int(total_bytes),
    ):
        raise InventoryError("dedup inventory v8 file evidence changed during migration")
    if (
        connection.execute("SELECT 1 FROM inventory_checkpoints WHERE valid<>0 LIMIT 1").fetchone()
        is not None
    ):
        raise InventoryError("dedup inventory v8 retained an unbound checkpoint")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v8 foreign-key validation failed")
