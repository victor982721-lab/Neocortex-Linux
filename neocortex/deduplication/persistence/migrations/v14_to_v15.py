"""Add change observations without inventing versions for historical files."""

from __future__ import annotations

import sqlite3

from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

from ..contracts import inventory_v13_schema_contract
from ..ddl import V15_FILE_CHANGE_VERSIONS_DDL


def migrate(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection, inventory_v13_schema_contract(),
        label="dedup inventory v14 migration source", exact=True,
    )
    connection.execute(V15_FILE_CHANGE_VERSIONS_DDL)
