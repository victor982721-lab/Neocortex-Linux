"""Add immutable inventory and duplicate-plan publication heads in v13."""

from __future__ import annotations

import sqlite3
import time

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ...inventory.generation import duplicate_plan_digest, inventory_content_digest
from ..contracts import inventory_v12_schema_contract
from ..ddl import (
    SCHEMA_LABEL,
    V13_FINGERPRINT_CONTENT_DDL,
    V13_GENERATION_HEAD_DDL,
    V13_PLAN_HEAD_DDL,
    V13_SUCCESSOR_DDL,
    execute_ddl,
)


def migrate(connection: sqlite3.Connection) -> None:
    """Create additive publication stores and backfill trusted current rows.

    Legacy plan and inventory rows are retained byte-for-byte.  Their new
    content identities are derived from those rows, never inferred from old
    timestamps or verification labels.
    """

    validate_sqlite_schema_contract(
        connection,
        inventory_v12_schema_contract(),
        label=f"{SCHEMA_LABEL} v12 migration source",
        exact=True,
    )
    execute_ddl(
        connection,
        (
            V13_FINGERPRINT_CONTENT_DDL,
            V13_GENERATION_HEAD_DDL,
            V13_SUCCESSOR_DDL,
            V13_PLAN_HEAD_DDL,
        ),
    )

    now = time.time_ns()
    scan_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT scan_id FROM scans WHERE status='complete' ORDER BY scan_id"
        )
    )
    for scan_id in scan_ids:
        content_digest = inventory_content_digest(connection, scan_id)
        connection.execute(
            "INSERT INTO inventory_generation_heads(scan_id,content_digest,created_ns) "
            "VALUES(?,?,?)",
            (scan_id, content_digest, now),
        )
    scan_id_set = set(scan_ids)
    plan_ids = tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT scan_id FROM duplicate_plan_summaries ORDER BY scan_id"
        )
    )
    for scan_id in plan_ids:
        if scan_id not in scan_id_set:
            # An old database may retain a summary for a partial/removed scan;
            # preserve it but do not promote it to a current evidence head.
            continue
        connection.execute(
            "INSERT INTO duplicate_plan_heads("
            "scan_id,inventory_content_digest,plan_digest,status,completed_ns) "
            "VALUES(?,?,?,?,?)",
            (
                scan_id,
                inventory_content_digest(connection, scan_id),
                duplicate_plan_digest(connection, scan_id),
                "published",
                now,
            ),
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v13 foreign-key validation failed")


__all__ = ["migrate"]
