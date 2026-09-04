"""Inventory schema migration from v10 to verification-bound plans in v11."""

from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v10_schema_contract
from ..ddl import SCHEMA_LABEL, V11_VERIFICATION_MODE_DDL, execute_ddl


def migrate(connection: sqlite3.Connection) -> None:
    """Persist the verification mode without inferring legacy plan policy."""

    validate_sqlite_schema_contract(
        connection,
        inventory_v10_schema_contract(),
        label=f"{SCHEMA_LABEL} v10 migration source",
        exact=True,
    )
    before = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(group_count),0),
        COALESCE(SUM(redundant_files),0),COALESCE(SUM(reclaimable_bytes),0)
        FROM duplicate_plan_summaries"""
    ).fetchone()
    execute_ddl(connection, V11_VERIFICATION_MODE_DDL)
    after = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(group_count),0),
        COALESCE(SUM(redundant_files),0),COALESCE(SUM(reclaimable_bytes),0)
        FROM duplicate_plan_summaries"""
    ).fetchone()
    if tuple(map(int, after)) != tuple(map(int, before)):
        raise InventoryError("dedup inventory v11 plan summary evidence changed during migration")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v11 foreign-key validation failed")
