"""Fence older Inventory readers before owner-local reset barriers are used."""
from __future__ import annotations

import sqlite3

from ..validation import validate_inventory_schema


def migrate(connection: sqlite3.Connection) -> None:
    """Validate the unchanged v13 table shape; the registry advances metadata."""
    validate_inventory_schema(connection)
