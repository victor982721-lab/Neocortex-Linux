"""Inventory schema migration from v1 to v2."""

import sqlite3

from .common import ensure_v2_objects


def migrate(connection: sqlite3.Connection) -> None:
    ensure_v2_objects(connection)
