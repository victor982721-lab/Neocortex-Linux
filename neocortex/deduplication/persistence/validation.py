"""Read-only validation for inventory schema versions."""

from __future__ import annotations

import sqlite3

from neocortex.sqlite_schema_contract import validate_sqlite_schema_contract

from .contracts import (
    inventory_schema_contract,
    metadata_contract,
)
from .ddl import SCHEMA_LABEL


def validate_metadata(connection: sqlite3.Connection) -> None:
    """Validate only the version metadata needed before migration dispatch."""

    validate_sqlite_schema_contract(
        connection,
        metadata_contract(),
        label=f"{SCHEMA_LABEL} metadata",
    )


def validate_inventory_schema(connection: sqlite3.Connection) -> None:
    """Validate every persistent v10 table and index without changing state."""

    validate_sqlite_schema_contract(
        connection,
        inventory_schema_contract(),
        label=SCHEMA_LABEL,
        exact=True,
    )
