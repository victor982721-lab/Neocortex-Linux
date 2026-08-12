"""Durable SQLite state for bounded spreadsheet and presentation extraction."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from neocortex.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)
from neocortex.platform_policy import sqlite_path_collation

from .sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Connections and schema


OFFICE_SCHEMA_VERSION = 3
_PATH_COLLATION = sqlite_path_collation()

_OFFICE_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="Office state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32_768,
        wal_autocheckpoint_pages=2_048,
        journal_size_limit_bytes=134_217_728,
    ),
)


def _office_v1_schema_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported Office path collation: {path_collation}")
    return (
        """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
        f"""CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        format TEXT NOT NULL,
        path TEXT NOT NULL COLLATE {path_collation},
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT,
        author TEXT,
        subject TEXT,
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        part_count INTEGER NOT NULL DEFAULT 0,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        review_disposition TEXT NOT NULL DEFAULT 'none',
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE UNIQUE INDEX IF NOT EXISTS office_documents_path_idx
        ON documents(path)""",
        """CREATE INDEX IF NOT EXISTS office_documents_status_idx
        ON documents(format,status,review_disposition,path)""",
        f"""CREATE TABLE IF NOT EXISTS office_inventory(
        file_key TEXT PRIMARY KEY,
        format TEXT NOT NULL,
        path TEXT NOT NULL COLLATE {path_collation},
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE INDEX IF NOT EXISTS office_inventory_run_idx
        ON office_inventory(last_seen_run_id,format,file_key)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS office_inventory_path_idx
        ON office_inventory(path)""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        file_key UNINDEXED,
        format UNINDEXED,
        path UNINDEXED,
        title,
        author,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    )


def _office_xlsx_schema_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported Office path collation: {path_collation}")
    return (
        """CREATE TABLE IF NOT EXISTS xlsx_cells(
        file_key TEXT NOT NULL,
        workbook TEXT NOT NULL,
        sheet TEXT NOT NULL,
        sheet_ordinal INTEGER NOT NULL,
        cell_reference TEXT NOT NULL,
        cell_type TEXT NOT NULL,
        value TEXT NOT NULL,
        raw_value TEXT,
        formula TEXT,
        cached_value TEXT,
        style_index INTEGER,
        number_format TEXT,
        PRIMARY KEY(file_key,sheet_ordinal,cell_reference),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
        f"""CREATE INDEX IF NOT EXISTS xlsx_cells_location_idx
        ON xlsx_cells(workbook COLLATE {path_collation},sheet_ordinal,cell_reference)""",
        """CREATE TRIGGER IF NOT EXISTS xlsx_cells_document_path_update
        AFTER UPDATE OF path ON documents
        WHEN OLD.path<>NEW.path
        BEGIN
            UPDATE xlsx_cells SET workbook=NEW.path WHERE file_key=NEW.file_key;
        END""",
    )


_OFFICE_V1_SCHEMA_DDL = _office_v1_schema_ddl("NOCASE")
_OFFICE_V2_SCHEMA_DDL = _office_xlsx_schema_ddl("NOCASE")
_OFFICE_SCHEMA_DDL = _office_v1_schema_ddl(_PATH_COLLATION) + _office_xlsx_schema_ddl(
    _PATH_COLLATION
)


def _create_office_schema(connection: sqlite3.Connection) -> None:
    for statement in _OFFICE_SCHEMA_DDL:
        connection.execute(statement)


def _create_office_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in _OFFICE_V1_SCHEMA_DDL:
        connection.execute(statement)


def _create_office_v2_schema(connection: sqlite3.Connection) -> None:
    for statement in _OFFICE_V1_SCHEMA_DDL + _OFFICE_V2_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _office_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_office_schema)


@lru_cache(maxsize=1)
def _office_v1_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_office_v1_schema)


@lru_cache(maxsize=1)
def _office_v2_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_office_v2_schema)


def _migrate_office_v1(connection: sqlite3.Connection) -> None:
    for statement in _OFFICE_V2_SCHEMA_DDL:
        connection.execute(statement)


def _migrate_office_v2_path_collation(connection: sqlite3.Connection) -> None:
    if _PATH_COLLATION == "NOCASE":
        return
    row_counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("documents", "office_inventory", "xlsx_cells", "document_fts")
    }
    connection.execute("CREATE TEMP TABLE office_documents_v2 AS SELECT * FROM documents")
    connection.execute("CREATE TEMP TABLE office_inventory_v2 AS SELECT * FROM office_inventory")
    connection.execute("CREATE TEMP TABLE office_xlsx_cells_v2 AS SELECT * FROM xlsx_cells")
    connection.execute(
        """CREATE TEMP TABLE office_document_fts_v2 AS
        SELECT rowid AS source_rowid,file_key,format,path,title,author,body
        FROM document_fts"""
    )
    connection.execute("DROP TABLE document_fts")
    connection.execute("DROP TRIGGER xlsx_cells_document_path_update")
    connection.execute("DROP TABLE xlsx_cells")
    connection.execute("DROP TABLE office_inventory")
    connection.execute("DROP TABLE documents")
    _create_office_schema(connection)
    connection.execute("INSERT INTO documents SELECT * FROM office_documents_v2")
    connection.execute("INSERT INTO office_inventory SELECT * FROM office_inventory_v2")
    connection.execute("INSERT INTO xlsx_cells SELECT * FROM office_xlsx_cells_v2")
    connection.execute(
        """INSERT INTO document_fts(rowid,file_key,format,path,title,author,body)
        SELECT source_rowid,file_key,format,path,title,author,body
        FROM office_document_fts_v2"""
    )
    for table, expected in row_counts.items():
        actual = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if actual != expected:
            raise RuntimeError(f"Office path migration changed {table} row count")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Office path migration foreign-key validation failed")
    connection.execute("DROP TABLE office_documents_v2")
    connection.execute("DROP TABLE office_inventory_v2")
    connection.execute("DROP TABLE office_xlsx_cells_v2")
    connection.execute("DROP TABLE office_document_fts_v2")


@contextmanager
def office_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
):
    """Open Office state, optionally refusing creation after initialization."""

    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    connection = connect_sqlite(
        path,
        mode=mode,
        policy=_OFFICE_SQLITE_POLICY,
    )
    try:
        yield connection
    finally:
        connection.close()


def initialize_office_state(path: Path) -> None:
    """Create or additively migrate Office state after a read-only probe."""

    prior: int | None = None
    if path.is_file():
        with office_database(path, readonly=True) as connection:
            prior = read_metadata_schema_version(connection, label="office")
            if prior is not None and prior > OFFICE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"office schema {prior} is newer than supported schema {OFFICE_SCHEMA_VERSION}"
                )
            if prior == OFFICE_SCHEMA_VERSION:
                validate_sqlite_schema_contract(
                    connection,
                    _office_schema_contract(),
                    label="office",
                    exact=True,
                )
                return
            if prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _office_v1_schema_contract(),
                    label="office schema 1 migration source",
                    exact=True,
                )
            elif prior == 2:
                validate_sqlite_schema_contract(
                    connection,
                    _office_v2_schema_contract(),
                    label="office schema 2 migration source",
                    exact=True,
                )
            elif prior not in {None, 0}:
                raise RuntimeError(f"unsupported office migration start: {prior}")

    with office_database(path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            locked_prior = read_metadata_schema_version(connection, label="office")
            if locked_prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _office_v1_schema_contract(),
                    label="office schema 1 migration source",
                    exact=True,
                )
                _migrate_office_v1(connection)
                locked_prior = 2
            if locked_prior == 2:
                validate_sqlite_schema_contract(
                    connection,
                    _office_v2_schema_contract(),
                    label="office schema 2 migration source",
                    exact=True,
                )
                _migrate_office_v2_path_collation(connection)
            elif locked_prior not in {None, 0, OFFICE_SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported office migration start: {locked_prior}")
            _create_office_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(OFFICE_SCHEMA_VERSION),),
            )
            validate_sqlite_schema_contract(
                connection,
                _office_schema_contract(),
                label="office",
                exact=True,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


# endregion [01]
