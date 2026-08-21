"""Transactional lifecycle orchestration for the inventory schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from neocortex.sqlite_schema_lifecycle import initialize_versioned_sqlite_schema

from ..domain.errors import InventoryError
from .connections import ConnectionFactory, connect
from .ddl import SCHEMA_LABEL, SCHEMA_VERSION, build_current_schema
from .migrations import migrate
from .validation import validate_inventory_schema, validate_metadata


def create_fresh(connection: sqlite3.Connection) -> None:
    """Build a new current schema before its outer transaction commits."""

    build_current_schema(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )


def initialize_inventory_schema(
    database: str | Path,
    *,
    connect_factory: ConnectionFactory | None = None,
) -> None:
    """Create, migrate, or read-only validate one inventory database."""

    try:
        initialize_versioned_sqlite_schema(
            Path(database),
            label=SCHEMA_LABEL,
            current_version=SCHEMA_VERSION,
            connect=connect if connect_factory is None else connect_factory,
            validate_metadata=validate_metadata,
            validate_current=validate_inventory_schema,
            create_fresh=create_fresh,
            migrate=migrate,
        )
    except InventoryError:
        raise
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        raise InventoryError(str(exc)) from exc
