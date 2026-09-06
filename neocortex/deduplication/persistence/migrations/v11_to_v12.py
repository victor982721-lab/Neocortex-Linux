"""Add actual per-member proof without reinterpreting legacy global labels."""

from __future__ import annotations

import hashlib
import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v11_schema_contract
from ..ddl import SCHEMA_LABEL, V12_EVIDENCE_DDL, execute_ddl


def _legacy_plan_digest(connection: sqlite3.Connection) -> bytes:
    digest = hashlib.sha256()
    for query in (
        "SELECT scan_id,group_count,redundant_files,reclaimable_bytes,completed_ns,verification_mode "
        "FROM duplicate_plan_summaries ORDER BY scan_id",
        "SELECT group_id,scan_id,size,keep_path,redundant_count,reclaimable_bytes,full_fingerprint "
        "FROM planned_duplicate_groups ORDER BY group_id",
        "SELECT group_id,member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns "
        "FROM planned_duplicate_members ORDER BY group_id,member_order",
    ):
        digest.update(query.encode())
        for row in connection.execute(query):
            digest.update(repr(tuple(row)).encode("utf-8", "surrogatepass"))
            digest.update(b"\n")
    return digest.digest()


def migrate(connection: sqlite3.Connection) -> None:
    """Run under the lifecycle's existing outer transaction, never commit here."""

    validate_sqlite_schema_contract(
        connection, inventory_v11_schema_contract(),
        label=f"{SCHEMA_LABEL} v11 migration source", exact=True,
    )
    before = _legacy_plan_digest(connection)
    execute_ddl(connection, V12_EVIDENCE_DDL)
    if _legacy_plan_digest(connection) != before:
        raise InventoryError("dedup inventory v12 migration altered legacy evidence")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v12 foreign-key validation failed")
