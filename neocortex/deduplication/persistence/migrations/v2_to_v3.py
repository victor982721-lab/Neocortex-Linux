"""Inventory schema migration from v2 to v3."""

import sqlite3

from .common import add_scan_counters, ensure_v2_objects


def migrate(connection: sqlite3.Connection) -> None:
    ensure_v2_objects(connection)
    add_scan_counters(connection)
