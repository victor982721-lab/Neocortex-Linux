"""Fence older Inventory readers before owner-local reset barriers are used."""
from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ..contracts import inventory_v13_schema_contract


def migrate(connection: sqlite3.Connection) -> None:
    """Validate the unchanged v13 table shape; the registry advances metadata."""
    validate_sqlite_schema_contract(
        connection, inventory_v13_schema_contract(),
        label="dedup inventory v13 migration source", exact=True,
    )
