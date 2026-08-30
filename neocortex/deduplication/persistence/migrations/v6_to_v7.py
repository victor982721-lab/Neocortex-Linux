"""Inventory schema migration from v6 to isolated generations in v7."""

from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ...domain.errors import InventoryError
from ..contracts import inventory_v6_schema_contract
from ..ddl import (
    CURRENT_SHARED_DDL_START,
    SCHEMA_LABEL,
    V8_CHECKPOINT_DDL,
    V9_DDL,
)


def migrate(connection: sqlite3.Connection) -> None:
    """Rebuild path rows as isolated generations after an exact v6 preflight."""

    validate_sqlite_schema_contract(
        connection,
        inventory_v6_schema_contract(),
        label=f"{SCHEMA_LABEL} v6 migration source",
        exact=True,
    )
    invalid_flag = connection.execute(
        "SELECT 1 FROM inventory_checkpoints WHERE valid NOT IN (0,1) LIMIT 1"
    ).fetchone()
    if invalid_flag is not None:
        raise InventoryError("dedup inventory v6 has a non-boolean checkpoint flag")
    orphan_checkpoint = connection.execute(
        """SELECT 1 FROM inventory_checkpoints c
        WHERE NOT EXISTS(SELECT 1 FROM scans s WHERE s.scan_id=c.scan_id)
        LIMIT 1"""
    ).fetchone()
    orphan_file = connection.execute(
        """SELECT 1 FROM files f
        WHERE NOT EXISTS(SELECT 1 FROM scans s WHERE s.scan_id=f.scan_id)
        LIMIT 1"""
    ).fetchone()
    if orphan_checkpoint is not None or orphan_file is not None:
        raise InventoryError("dedup inventory v6 contains orphan generation references")

    file_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    checkpoint_count = int(
        connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0]
    )
    connection.execute("ALTER TABLE files RENAME TO files_v6")
    connection.execute("ALTER TABLE inventory_checkpoints RENAME TO inventory_checkpoints_v6")
    connection.execute(
        """ALTER TABLE scans ADD COLUMN status TEXT NOT NULL DEFAULT 'building'
        CHECK(status IN ('building','complete','partial'))"""
    )
    connection.execute(
        """UPDATE scans SET status=CASE
        WHEN completed_ns IS NULL THEN 'building'
        WHEN errors=0
          AND files_seen IS NOT NULL AND files_seen>=0
          AND directories_seen IS NOT NULL AND directories_seen>=0
          AND bytes_seen IS NOT NULL AND bytes_seen>=0
          AND skipped_links IS NOT NULL AND skipped_links>=0
          AND excluded_directories IS NOT NULL AND excluded_directories>=0
          AND files_seen=(SELECT COUNT(*) FROM files_v6 f
                          WHERE f.scan_id=scans.scan_id)
          AND bytes_seen=(SELECT COALESCE(SUM(size),0) FROM files_v6 f
                          WHERE f.scan_id=scans.scan_id)
        THEN 'complete' ELSE 'partial' END"""
    )
    connection.execute(V8_CHECKPOINT_DDL)
    connection.execute(V9_DDL[3])
    connection.execute(
        """INSERT INTO files(
        scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns)
        SELECT scan_id,path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files_v6"""
    )
    connection.execute(
        """INSERT INTO inventory_checkpoints(
        root,scan_id,volume,journal_id,next_usn,valid,updated_ns)
        SELECT c.root,c.scan_id,c.volume,c.journal_id,c.next_usn,
               CASE WHEN c.valid=1 AND s.status='complete' THEN 1 ELSE 0 END,
               c.updated_ns
        FROM inventory_checkpoints_v6 c
        JOIN scans s ON s.scan_id=c.scan_id"""
    )
    if int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]) != file_count:
        raise InventoryError("dedup inventory v7 file count changed during migration")
    if (
        int(connection.execute("SELECT COUNT(*) FROM inventory_checkpoints").fetchone()[0])
        != checkpoint_count
    ):
        raise InventoryError("dedup inventory v7 checkpoint count changed during migration")
    connection.execute("DROP TABLE inventory_checkpoints_v6")
    connection.execute("DROP TABLE files_v6")
    for statement in V9_DDL[4:CURRENT_SHARED_DDL_START]:
        connection.execute(statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InventoryError("dedup inventory v7 foreign-key validation failed")
