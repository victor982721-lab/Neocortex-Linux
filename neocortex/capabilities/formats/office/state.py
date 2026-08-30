"""Durable SQLite state for bounded spreadsheet and presentation extraction."""

from __future__ import annotations

import sqlite3
import time
import zlib
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import xxhash

from neocortex.deduplication import FileSnapshot

from neocortex.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)
from neocortex.platform_policy import sqlite_path_collation

from neocortex.foundation.file_identity import file_key_from_snapshot as _file_key
from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from .models import ExtractedOfficeDocument, OfficeExtractionError


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

    if _existing_office_state_is_current(path):
        return
    with office_database(path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _initialize_locked_office_state(connection)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _existing_office_state_is_current(path: Path) -> bool:
    if not path.is_file():
        return False
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
            return True
        _validate_office_migration_source(connection, prior)
    return False


def _validate_office_migration_source(
    connection: sqlite3.Connection,
    prior: int | None,
) -> None:
    contracts = {
        1: (_office_v1_schema_contract(), "office schema 1 migration source"),
        2: (_office_v2_schema_contract(), "office schema 2 migration source"),
    }
    selected = None if prior is None else contracts.get(prior)
    if selected is not None:
        contract, label = selected
        validate_sqlite_schema_contract(connection, contract, label=label, exact=True)
        return
    if prior not in {None, 0, OFFICE_SCHEMA_VERSION}:
        raise RuntimeError(f"unsupported office migration start: {prior}")


def _initialize_locked_office_state(connection: sqlite3.Connection) -> None:
    prior = read_metadata_schema_version(connection, label="office")
    _validate_office_migration_source(connection, prior)
    if prior == 1:
        _migrate_office_v1(connection)
        prior = 2
    if prior == 2:
        _migrate_office_v2_path_collation(connection)
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


# endregion [01]


# region [02] Owner-local cache persistence and search
def _store_inventory(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    format_name: str,
    run_id: int,
) -> None:
    connection.execute(
        f"DELETE FROM office_inventory WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, _file_key(snapshot)),
    )
    connection.execute(
        """INSERT INTO office_inventory(
        file_key,format,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_key) DO UPDATE SET
        format=excluded.format,path=excluded.path,size=excluded.size,
        mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
        last_seen_run_id=excluded.last_seen_run_id""",
        (
            _file_key(snapshot),
            format_name,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def _cached_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    processing_signature: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT status,error_type,error_message,retryable,review_disposition
        FROM documents WHERE file_key=? AND size=?
        AND mtime_ns=? AND birthtime_ns=? AND processing_signature=?""",
        (
            _file_key(snapshot),
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
        ),
    ).fetchone()


def _remove_path_conflict(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
) -> None:
    conflict = connection.execute(
        f"SELECT file_key FROM documents WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, _file_key(snapshot)),
    ).fetchone()
    if conflict is not None:
        connection.execute("DELETE FROM document_fts WHERE file_key=?", (str(conflict[0]),))
        connection.execute("DELETE FROM documents WHERE file_key=?", (str(conflict[0]),))


def _refresh_cached_path(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    format_name: str,
    run_id: int,
) -> None:
    _remove_path_conflict(connection, snapshot)
    connection.execute(
        """UPDATE documents SET format=?,path=?,last_seen_run_id=?,updated_ns=?
        WHERE file_key=?""",
        (format_name, snapshot.path, run_id, time.time_ns(), _file_key(snapshot)),
    )
    connection.execute(
        "UPDATE document_fts SET path=?,format=? WHERE file_key=?",
        (snapshot.path, format_name, _file_key(snapshot)),
    )


def _store_success(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    document: ExtractedOfficeDocument,
    processing_signature: str,
    run_id: int,
) -> None:
    _remove_path_conflict(connection, snapshot)
    text_bytes = document.text.encode("utf-8")
    fingerprint = xxhash.xxh3_128_hexdigest(text_bytes)
    connection.execute(
        """INSERT INTO documents(
        file_key,format,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,author,subject,text_zlib,text_chars,text_xxh3_128,part_count,error_type,
        error_message,retryable,review_disposition,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,NULL,NULL,0,'none',?,?)
        ON CONFLICT(file_key) DO UPDATE SET format=excluded.format,path=excluded.path,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status='complete',
        title=excluded.title,author=excluded.author,subject=excluded.subject,
        text_zlib=excluded.text_zlib,text_chars=excluded.text_chars,
        text_xxh3_128=excluded.text_xxh3_128,part_count=excluded.part_count,
        error_type=NULL,error_message=NULL,retryable=0,review_disposition='none',
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            _file_key(snapshot),
            document.format,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            document.title,
            document.author,
            document.subject,
            zlib.compress(text_bytes, 6),
            len(document.text),
            fingerprint,
            document.part_count,
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM document_fts WHERE file_key=?", (_file_key(snapshot),))
    connection.execute("DELETE FROM xlsx_cells WHERE file_key=?", (_file_key(snapshot),))
    if document.xlsx_cells:
        connection.executemany(
            """INSERT INTO xlsx_cells(
            file_key,workbook,sheet,sheet_ordinal,cell_reference,cell_type,value,
            raw_value,formula,cached_value,style_index,number_format)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                (
                    _file_key(snapshot),
                    cell.workbook,
                    cell.sheet,
                    cell.sheet_ordinal,
                    cell.cell_reference,
                    cell.cell_type,
                    cell.value,
                    cell.raw_value,
                    cell.formula,
                    cell.cached_value,
                    cell.style_index,
                    cell.number_format,
                )
                for cell in document.xlsx_cells
            ),
        )
    connection.execute(
        """INSERT INTO document_fts(file_key,format,path,title,author,body)
        VALUES(?,?,?,?,?,?)""",
        (
            _file_key(snapshot),
            document.format,
            snapshot.path,
            document.title,
            document.author,
            document.text,
        ),
    )


def _store_error(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    format_name: str,
    processing_signature: str,
    run_id: int,
    error: OfficeExtractionError,
) -> None:
    _remove_path_conflict(connection, snapshot)
    connection.execute(
        """INSERT INTO documents(
        file_key,format,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,author,subject,text_zlib,text_chars,text_xxh3_128,part_count,error_type,
        error_message,retryable,review_disposition,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,'error',NULL,NULL,NULL,NULL,0,NULL,0,?,?,?, ?,?,?)
        ON CONFLICT(file_key) DO UPDATE SET format=excluded.format,path=excluded.path,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status='error',
        title=NULL,author=NULL,subject=NULL,text_zlib=NULL,text_chars=0,
        text_xxh3_128=NULL,part_count=0,error_type=excluded.error_type,
        error_message=excluded.error_message,retryable=excluded.retryable,
        review_disposition=excluded.review_disposition,
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            _file_key(snapshot),
            format_name,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            error.code,
            str(error)[:2000],
            int(error.retryable),
            error.recommendation,
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM document_fts WHERE file_key=?", (_file_key(snapshot),))
    connection.execute("DELETE FROM xlsx_cells WHERE file_key=?", (_file_key(snapshot),))


def _prune_stale_documents(connection: sqlite3.Connection, run_id: int) -> int:
    stale_keys = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT file_key FROM documents WHERE last_seen_run_id<>?", (run_id,)
        )
    )
    for offset in range(0, len(stale_keys), 256):
        batch = stale_keys[offset : offset + 256]
        placeholders = ",".join("?" for _ in batch)
        connection.execute(f"DELETE FROM document_fts WHERE file_key IN ({placeholders})", batch)
        connection.execute(f"DELETE FROM documents WHERE file_key IN ({placeholders})", batch)
    connection.execute("DELETE FROM office_inventory WHERE last_seen_run_id<>?", (run_id,))
    return len(stale_keys)


def search_office_state(path: Path, query: str, limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("Office search limit must be between 1 and 1000")
    with office_database(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT file_key,format,path,title,author,
            snippet(document_fts,5,'[',']',' … ',24) AS snippet,
            bm25(document_fts) AS rank FROM document_fts
            WHERE document_fts MATCH ? ORDER BY rank,path LIMIT ?""",
            (query, limit),
        )
        return [dict(row) for row in rows]


# endregion [02]

__all__ = (
    "OFFICE_SCHEMA_VERSION",
    "_cached_document",
    "_prune_stale_documents",
    "_refresh_cached_path",
    "_store_error",
    "_store_inventory",
    "_store_success",
    "initialize_office_state",
    "office_database",
    "search_office_state",
)

for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.office.state"
del _defined_value
