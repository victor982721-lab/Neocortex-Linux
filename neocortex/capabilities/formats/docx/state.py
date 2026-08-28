"""Persistent, incrementally migratable state for the DOCX route."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from neocortex.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)

from _04_Nucleo_Operativo.sqlite_schema_lifecycle import initialize_versioned_sqlite_schema
from .schema import (
    DOCX_SCHEMA_VERSION,
    UNKNOWN_BIRTHTIME_NS as DOCX_UNKNOWN_BIRTHTIME_NS,
    create_fresh_docx_schema,
    migrate_docx_schema,
    validate_docx_metadata,
    validate_docx_schema,
)


# region [01] Schema and connections

SCHEMA_VERSION = DOCX_SCHEMA_VERSION
UNKNOWN_BIRTHTIME_NS = DOCX_UNKNOWN_BIRTHTIME_NS

_DOCX_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="DOCX state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32768,
        wal_autocheckpoint_pages=4096,
        journal_size_limit_bytes=268435456,
    ),
)


def connect_docx_state(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    return connect_sqlite(
        path,
        mode=READONLY_EXISTING if readonly else READWRITE_CREATE,
        policy=_DOCX_SQLITE_POLICY,
    )


@contextmanager
def docx_database(path: Path, *, readonly: bool = False):
    connection = connect_docx_state(path, readonly=readonly)
    try:
        if readonly:
            yield connection
        else:
            with connection:
                yield connection
    finally:
        connection.close()


# endregion [01]


# region [02] Initialization


def initialize_docx_state(path: Path) -> None:
    """Create or migrate DOCX state after a read-only structural probe."""

    initialize_versioned_sqlite_schema(
        path,
        label="DOCX",
        current_version=SCHEMA_VERSION,
        connect=connect_docx_state,
        validate_metadata=validate_docx_metadata,
        validate_current=validate_docx_schema,
        create_fresh=create_fresh_docx_schema,
        migrate=migrate_docx_schema,
    )


# endregion [02]


for _historical_symbol in (
    connect_docx_state,
    docx_database,
    initialize_docx_state,
):
    _historical_symbol.__module__ = "_04_Nucleo_Operativo.docx_state"
del _historical_symbol
