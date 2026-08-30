"""Inventory schema migration from v9 to identity query indexes in v10."""

from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v9_schema_contract
from ..ddl import SCHEMA_LABEL, V10_INDEX_DDL, execute_ddl


def migrate(connection: sqlite3.Connection) -> None:
    """Index identity-bound Knowledge joins without changing evidence."""

    validate_sqlite_schema_contract(
        connection,
        inventory_v9_schema_contract(),
        label=f"{SCHEMA_LABEL} v9 migration source",
        exact=True,
    )
    file_count, file_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()
    member_count, member_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM planned_duplicate_members"
    ).fetchone()

    execute_ddl(connection, V10_INDEX_DDL)

    migrated_file_count, migrated_file_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM files"
    ).fetchone()
    migrated_member_count, migrated_member_bytes = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(size),0) FROM planned_duplicate_members"
    ).fetchone()
    if (int(migrated_file_count), int(migrated_file_bytes)) != (
        int(file_count),
        int(file_bytes),
    ):
        raise InventoryError("dedup inventory v10 file evidence changed during migration")
    if (int(migrated_member_count), int(migrated_member_bytes)) != (
        int(member_count),
        int(member_bytes),
    ):
        raise InventoryError("dedup inventory v10 plan-member evidence changed during migration")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v10 foreign-key validation failed")
