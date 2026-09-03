"""Schema lifecycle for the bounded SQLite NTFS path index."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/path_index/schema.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from neocortex.persistence.sqlite_immutable import open_sidecar_safe_sqlite_connection
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.sqlite_schema_lifecycle import initialize_versioned_sqlite_schema
# endregion [01]

# region [02] Implementación


SCHEMA_VERSION = 1
_SCHEMA_LABEL = "path-index"
_METADATA_DDL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""
_CURRENT_DDL = (
    _METADATA_DDL,
    """
    CREATE TABLE nodes (
        frn BLOB PRIMARY KEY,
        parent_frn BLOB NOT NULL,
        name TEXT NOT NULL,
        file_attributes INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    "CREATE INDEX nodes_parent_idx ON nodes(parent_frn)",
)


def _configure_owner_connection(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
) -> sqlite3.Connection:
    try:
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("path-index connection could not enable foreign keys")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("path-index connection is not query-only")
    except BaseException:
        connection.close()
        raise
    return connection


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        try:
            connection = open_sidecar_safe_sqlite_connection(path, timeout_seconds=60.0)
        except FileNotFoundError as exc:
            raise sqlite3.OperationalError(f"unable to open database file: {path}") from exc
    else:
        connection = sqlite3.connect(path, timeout=60.0)
    return _configure_owner_connection(connection, readonly=readonly)


def connect_existing_path_index_database(path: Path) -> sqlite3.Connection:
    """Open an accepted path-index database without recreating missing state."""

    connection = sqlite3.connect(existing_sqlite_uri(path), uri=True, timeout=60.0)
    return _configure_owner_connection(connection, readonly=False)


def _execute_ddl(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _build_metadata_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_METADATA_DDL)


def _build_current_schema(connection: sqlite3.Connection) -> None:
    _execute_ddl(connection, _CURRENT_DDL)


@lru_cache(maxsize=1)
def _metadata_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_metadata_schema)


@lru_cache(maxsize=1)
def path_index_schema_contract() -> SQLiteSchemaContract:
    """Return the exact structural contract for path-index schema v1."""

    return schema_contract_from_builder(_build_current_schema)


def _validate_metadata(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        _metadata_contract(),
        label=f"{_SCHEMA_LABEL} metadata",
    )


def validate_path_index_schema(connection: sqlite3.Connection) -> None:
    """Validate the exact v1 schema without executing persistent statements."""

    validate_sqlite_schema_contract(
        connection,
        path_index_schema_contract(),
        label=_SCHEMA_LABEL,
        exact=True,
    )


def _create_fresh(connection: sqlite3.Connection) -> None:
    _build_current_schema(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )


def _reject_legacy(_connection: sqlite3.Connection, version: int) -> None:
    raise RuntimeError(
        f"{_SCHEMA_LABEL} state schema {version} is unsupported; expected {SCHEMA_VERSION}"
    )


def initialize_path_index_schema(database: str | Path) -> None:
    """Create or validate the path index, probing existing state read-only."""

    initialize_versioned_sqlite_schema(
        Path(database),
        label=_SCHEMA_LABEL,
        current_version=SCHEMA_VERSION,
        connect=_connect,
        validate_metadata=_validate_metadata,
        validate_current=validate_path_index_schema,
        create_fresh=_create_fresh,
        migrate=_reject_legacy,
    )


def configure_path_index_connection(connection: sqlite3.Connection) -> None:
    """Apply bounded operational settings after schema acceptance."""

    for statement in (
        "PRAGMA busy_timeout=60000",
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-32768",
        "PRAGMA wal_autocheckpoint=4096",
        "PRAGMA journal_size_limit=268435456",
    ):
        connection.execute(statement)
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("path-index connection could not enable foreign keys")


__all__ = [
    "SCHEMA_VERSION",
    "configure_path_index_connection",
    "initialize_path_index_schema",
    "path_index_schema_contract",
    "validate_path_index_schema",
]
# endregion [02]
