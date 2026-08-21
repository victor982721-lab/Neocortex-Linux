"""Inventory schema migration from v3 to v4."""

import sqlite3

from .common import add_scan_counters, ensure_v2_objects, invalidate_checkpoints


def migrate(connection: sqlite3.Connection) -> None:
    ensure_v2_objects(connection)
    add_scan_counters(connection)
    invalidate_checkpoints(connection)
