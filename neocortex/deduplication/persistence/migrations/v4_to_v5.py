"""Inventory schema migration from v4 to v5."""

import sqlite3

from .common import add_fingerprint_birthtime, add_scan_counters, ensure_v2_objects


def migrate(connection: sqlite3.Connection) -> None:
    ensure_v2_objects(connection)
    add_scan_counters(connection)
    add_fingerprint_birthtime(connection)
