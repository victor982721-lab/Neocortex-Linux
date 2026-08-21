"""Inventory schema migration from v5 to v6."""

import sqlite3

from .common import (
    add_fingerprint_birthtime,
    add_scan_counters,
    add_scan_root_identity,
    ensure_v2_objects,
    invalidate_checkpoints,
)


def migrate(connection: sqlite3.Connection) -> None:
    ensure_v2_objects(connection)
    add_scan_counters(connection)
    add_fingerprint_birthtime(connection)
    add_scan_root_identity(connection)
    invalidate_checkpoints(connection)
