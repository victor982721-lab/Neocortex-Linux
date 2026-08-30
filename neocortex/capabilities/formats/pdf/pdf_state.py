"""SQLite lifecycle and conservative migrations for persistent PDF state."""

from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)

from .pdf_schema import (
    PDF_SCHEMA_VERSION,
    UNKNOWN_BIRTHTIME_NS as PDF_UNKNOWN_BIRTHTIME_NS,
    create_fresh_pdf_schema,
    migrate_pdf_schema,
    validate_pdf_metadata,
    validate_pdf_schema,
)
from neocortex.persistence.sqlite_schema_lifecycle import initialize_versioned_sqlite_schema


# region [01] Schema constants
# Keep migrations additive so existing extracted text and derived indexes survive upgrades.

SCHEMA_VERSION = PDF_SCHEMA_VERSION
UNKNOWN_BIRTHTIME_NS = PDF_UNKNOWN_BIRTHTIME_NS

# endregion [01]


# region [02] Connections
# Apply one connection policy everywhere that reads or changes the PDF database.

_PDF_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="PDF state",
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


def connect_pdf_state(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    return connect_sqlite(
        path,
        mode=READONLY_EXISTING if readonly else READWRITE_CREATE,
        policy=_PDF_SQLITE_POLICY,
    )


@contextmanager
def pdf_database(path: Path, *, readonly: bool = False):
    connection = connect_pdf_state(path, readonly=readonly)
    try:
        if readonly:
            yield connection
        else:
            with connection:
                yield connection
    finally:
        connection.close()


# endregion [02]


# region [03] Initialization and migration


def initialize_pdf_state(path: Path) -> None:
    """Create or migrate PDF state after a read-only structural probe."""

    initialize_versioned_sqlite_schema(
        path,
        label="PDF",
        current_version=SCHEMA_VERSION,
        connect=connect_pdf_state,
        validate_metadata=validate_pdf_metadata,
        validate_current=validate_pdf_schema,
        create_fresh=create_fresh_pdf_schema,
        migrate=migrate_pdf_schema,
    )


# endregion [03]
