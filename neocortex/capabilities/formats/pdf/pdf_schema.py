"""Canonical PDF schema, additive migrations and structural validation."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import sqlite3
from collections.abc import Callable
from functools import lru_cache

from neocortex.platform_policy import sqlite_path_collation

from .pdf_derived_schema import initialize_derived_schema
from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Versions and canonical DDL


PDF_SCHEMA_VERSION = 13
UNKNOWN_BIRTHTIME_NS = -1
_PATH_COLLATION = sqlite_path_collation()


_PDF_V12_TABLE_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        page_count INTEGER,
        completed_pages INTEGER NOT NULL DEFAULT 0,
        native_pages INTEGER NOT NULL DEFAULT 0,
        ocr_pages INTEGER NOT NULL DEFAULT 0,
        native_chars INTEGER NOT NULL DEFAULT 0,
        ocr_chars INTEGER NOT NULL DEFAULT 0,
        normalized_text_xxh3_128 TEXT,
        normalized_text_chars INTEGER NOT NULL DEFAULT 0,
        binary_xxh3_128 TEXT,
        page_start INTEGER,
        page_end INTEGER,
        is_partial INTEGER NOT NULL DEFAULT 0,
        page_errors_count INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT,
        error_type TEXT,
        error_message TEXT,
        transient_retry_count INTEGER NOT NULL DEFAULT 0,
        next_retry_ns INTEGER,
        last_seen_run_id INTEGER,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS page_staging(
        file_key TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        source TEXT NOT NULL,
        text_zlib BLOB NOT NULL,
        text_chars INTEGER NOT NULL,
        ocr_provenance_json TEXT,
        PRIMARY KEY(file_key, processing_signature, page_number)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS pages(
        file_key TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        source TEXT NOT NULL,
        text_zlib BLOB NOT NULL,
        text_chars INTEGER NOT NULL,
        ocr_provenance_json TEXT,
        PRIMARY KEY(file_key, page_number),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS page_errors(
        file_key TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(file_key, processing_signature, page_number)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS document_warnings(
        file_key TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        stage TEXT NOT NULL,
        warning_count INTEGER NOT NULL,
        samples_json TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(file_key,processing_signature,stage)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS pdf_inventory(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        last_seen_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
)


_PDF_V12_INDEX_DDL = (
    """CREATE INDEX IF NOT EXISTS pdf_inventory_run_idx
        ON pdf_inventory(last_seen_run_id,file_key)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS documents_path_idx
        ON documents(path)""",
    """CREATE INDEX IF NOT EXISTS documents_text_idx
        ON documents(normalized_text_xxh3_128,normalized_text_chars,status)""",
)


def _pdf_table_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported PDF path collation: {path_collation}")
    return tuple(
        statement.replace("COLLATE NOCASE", f"COLLATE {path_collation}")
        for statement in _PDF_V12_TABLE_DDL
    )


def _pdf_index_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported PDF path collation: {path_collation}")
    return tuple(
        statement.replace(
            "ON documents(path)",
            f"ON documents(path COLLATE {path_collation})",
        )
        for statement in _PDF_V12_INDEX_DDL
    )


_PDF_TABLE_DDL = _pdf_table_ddl(_PATH_COLLATION)
_PDF_INDEX_DDL = _pdf_index_ddl(_PATH_COLLATION)


_DOCUMENT_ADDITIONS = (
    ("size", "INTEGER NOT NULL DEFAULT 0"),
    ("mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
    ("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),
    ("processing_signature", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'error'"),
    ("page_count", "INTEGER"),
    ("completed_pages", "INTEGER NOT NULL DEFAULT 0"),
    ("native_pages", "INTEGER NOT NULL DEFAULT 0"),
    ("ocr_pages", "INTEGER NOT NULL DEFAULT 0"),
    ("native_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("ocr_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("normalized_text_xxh3_128", "TEXT"),
    ("normalized_text_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("binary_xxh3_128", "TEXT"),
    ("page_start", "INTEGER"),
    ("page_end", "INTEGER"),
    ("is_partial", "INTEGER NOT NULL DEFAULT 0"),
    ("page_errors_count", "INTEGER NOT NULL DEFAULT 0"),
    ("metadata_json", "TEXT"),
    ("error_type", "TEXT"),
    ("error_message", "TEXT"),
    ("transient_retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("next_retry_ns", "INTEGER"),
    ("last_seen_run_id", "INTEGER"),
    ("updated_ns", "INTEGER NOT NULL DEFAULT 0"),
)

_PAGE_OCR_PROVENANCE_ADDITIONS = (("ocr_provenance_json", "TEXT"),)


# endregion [01]


# region [02] Additive structure and logical migrations


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    quoted = _quoted_identifier(table)
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")}


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: tuple[tuple[str, str], ...],
) -> None:
    existing = _column_names(connection, table)
    for name, declaration in additions:
        if name in existing:
            continue
        quoted_table = _quoted_identifier(table)
        quoted_name = _quoted_identifier(name)
        connection.execute(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_name} {declaration}")


def _create_tables_from(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _create_indexes_from(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _ensure_structure(
    connection: sqlite3.Connection,
    *,
    table_ddl: tuple[str, ...],
    index_ddl: tuple[str, ...],
) -> None:
    _create_tables_from(connection, table_ddl)
    _add_columns(connection, "documents", _DOCUMENT_ADDITIONS)
    _add_columns(
        connection,
        "page_staging",
        _PAGE_OCR_PROVENANCE_ADDITIONS,
    )
    _add_columns(connection, "pages", _PAGE_OCR_PROVENANCE_ADDITIONS)
    _add_columns(
        connection,
        "pdf_inventory",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )
    initialize_derived_schema(connection)
    _create_indexes_from(connection, index_ddl)


def _ensure_current_structure(connection: sqlite3.Connection) -> None:
    _ensure_structure(
        connection,
        table_ddl=_PDF_TABLE_DDL,
        index_ddl=_PDF_INDEX_DDL,
    )


def _ensure_v12_structure(connection: sqlite3.Connection) -> None:
    _ensure_structure(
        connection,
        table_ddl=_PDF_V12_TABLE_DDL,
        index_ddl=_PDF_V12_INDEX_DDL,
    )


def _no_data_migration(connection: sqlite3.Connection) -> None:
    del connection


def _migrate_birthtime(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "documents",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )
    _add_columns(
        connection,
        "pdf_inventory",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )


def _migrate_legacy_ocr_control(connection: sqlite3.Connection) -> None:
    migrated = int(
        connection.execute(
            """UPDATE page_errors SET error_type='LegacyOcrControlError'
            WHERE error_type='AttributeError'
            AND error_message='''BoundedSemaphore'' object has no attribute ''get'''"""
        ).rowcount
    )
    connection.execute(
        """UPDATE documents SET transient_retry_count=0,next_retry_ns=NULL
        WHERE status='partial' AND EXISTS(
            SELECT 1 FROM page_errors e WHERE e.file_key=documents.file_key
            AND e.processing_signature=documents.processing_signature
            AND e.error_type='LegacyOcrControlError')"""
    )
    connection.execute(
        """INSERT OR REPLACE INTO metadata(key,value)
        VALUES('legacy_ocr_control_rows_migrated',?)""",
        (str(migrated),),
    )


def _migrate_durable_timeouts(connection: sqlite3.Connection) -> None:
    migrated = int(
        connection.execute(
            """UPDATE documents SET status='partial',is_partial=1
            WHERE status='error' AND error_type='PdfDocumentTimeout'
            AND completed_pages>0
            AND (page_count IS NULL OR completed_pages<page_count)
            AND EXISTS(SELECT 1 FROM page_staging s
                WHERE s.file_key=documents.file_key
                AND s.processing_signature=documents.processing_signature
                AND s.source<>'error')"""
        ).rowcount
    )
    connection.execute(
        """INSERT OR REPLACE INTO metadata(key,value)
        VALUES('durable_timeout_rows_migrated',?)""",
        (str(migrated),),
    )


_PDF_PATH_REBUILD_TABLES = (
    "documents",
    "pdf_inventory",
    "pages",
    "page_layouts",
    "document_layouts",
)


def _ordered_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    schema: str = "main",
) -> tuple[str, ...]:
    if schema not in {"main", "temp"}:  # pragma: no cover - internal invariant
        raise ValueError(f"unsupported SQLite schema: {schema}")
    quoted = _quoted_identifier(table)
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA {schema}.table_info({quoted})"))


def _copy_table_exact(
    connection: sqlite3.Connection,
    *,
    source: str,
    target: str,
) -> None:
    source_columns = _ordered_columns(connection, source, schema="temp")
    target_columns = _ordered_columns(connection, target)
    if not source_columns or set(source_columns) != set(target_columns):
        raise RuntimeError(
            f"PDF path migration columns changed for {target}: "
            f"source={source_columns!r} target={target_columns!r}"
        )
    columns = ",".join(_quoted_identifier(column) for column in target_columns)
    connection.execute(
        f"INSERT INTO main.{_quoted_identifier(target)}({columns}) "
        f"SELECT {columns} FROM temp.{_quoted_identifier(source)}"
    )


def _validate_pdf_v12_schema(connection: sqlite3.Connection) -> None:
    failures: list[str] = []
    for contract in _pdf_v12_schema_contracts():
        try:
            validate_sqlite_schema_contract(
                connection,
                contract,
                label="PDF schema 12 migration source",
                exact=True,
            )
        except SQLiteSchemaContractError as exc:
            failures.append(str(exc))
        else:
            return
    raise SQLiteSchemaContractError(
        "PDF schema 12 migration source is invalid for every supported layout: "
        + " | ".join(failures)
    )


def _migrate_platform_path_collation(connection: sqlite3.Connection) -> None:
    """Rebuild path-owning tables without discarding child or FTS evidence."""

    _validate_pdf_v12_schema(connection)
    row_counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
        )
        for table in _PDF_PATH_REBUILD_TABLES
    }
    fts_count = int(connection.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0])
    for table in _PDF_PATH_REBUILD_TABLES:
        backup = f"pdf_v12_{table}"
        connection.execute(
            f"CREATE TEMP TABLE {_quoted_identifier(backup)} AS "
            f"SELECT * FROM main.{_quoted_identifier(table)}"
        )

    for table in ("page_layouts", "document_layouts", "pages"):
        connection.execute(f"DROP TABLE {_quoted_identifier(table)}")
    connection.execute("DROP TABLE documents")
    connection.execute("DROP TABLE pdf_inventory")
    _ensure_current_structure(connection)

    for table in ("documents", "pdf_inventory", "pages", "page_layouts", "document_layouts"):
        backup = f"pdf_v12_{table}"
        _copy_table_exact(connection, source=backup, target=table)
        connection.execute(f"DROP TABLE temp.{_quoted_identifier(backup)}")
        migrated_count = int(
            connection.execute(f"SELECT COUNT(*) FROM main.{_quoted_identifier(table)}").fetchone()[
                0
            ]
        )
        if migrated_count != row_counts[table]:
            raise RuntimeError(f"PDF path migration changed {table} row count")
    if int(connection.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0]) != fts_count:
        raise RuntimeError("PDF path migration changed page FTS rows")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("PDF path migration foreign-key validation failed")


_PDF_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _no_data_migration,
    2: _no_data_migration,
    3: _no_data_migration,
    4: _no_data_migration,
    5: _no_data_migration,
    6: _no_data_migration,
    7: _no_data_migration,
    8: _migrate_birthtime,
    9: _migrate_legacy_ocr_control,
    10: _migrate_durable_timeouts,
    11: _no_data_migration,
    12: _migrate_platform_path_collation,
}


def create_fresh_pdf_schema(connection: sqlite3.Connection) -> None:
    _ensure_current_structure(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
        (str(PDF_SCHEMA_VERSION),),
    )


def migrate_pdf_schema(connection: sqlite3.Connection, version: int) -> None:
    if version < 12:
        _ensure_v12_structure(connection)
    current = version
    while current < PDF_SCHEMA_VERSION:
        migration = _PDF_MIGRATIONS.get(current)
        if migration is None:  # pragma: no cover - module invariant
            raise RuntimeError(f"PDF schema migration {current} is missing")
        migration(connection)
        current += 1
        updated = connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(current),),
        )
        if updated.rowcount != 1:
            raise RuntimeError("PDF schema_version disappeared during migration")


# endregion [02]


# region [03] Canonical contracts


def _build_canonical_schema(connection: sqlite3.Connection) -> None:
    _ensure_current_structure(connection)


def _build_pdf_v12_canonical_schema(connection: sqlite3.Connection) -> None:
    _ensure_v12_structure(connection)


def _build_pdf_v12_legacy_v1_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE metadata(
            key TEXT PRIMARY KEY,value TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE documents(
            file_key TEXT PRIMARY KEY,path TEXT,
            normalized_text_xxh3_128 TEXT,
            normalized_text_chars INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'done'
        ) WITHOUT ROWID""",
        """CREATE TABLE pages(
            file_key TEXT NOT NULL,page_number INTEGER NOT NULL,
            source TEXT NOT NULL,text_zlib BLOB NOT NULL,text_chars INTEGER NOT NULL,
            PRIMARY KEY(file_key,page_number)
        ) WITHOUT ROWID""",
    )
    for statement in statements:
        connection.execute(statement)
    _ensure_v12_structure(connection)


def _build_metadata_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_PDF_TABLE_DDL[0])


@lru_cache(maxsize=1)
def _metadata_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_metadata_schema)


@lru_cache(maxsize=1)
def _pdf_schema_contracts() -> tuple[SQLiteSchemaContract, ...]:
    return (schema_contract_from_builder(_build_canonical_schema),)


@lru_cache(maxsize=1)
def _pdf_v12_schema_contracts() -> tuple[SQLiteSchemaContract, ...]:
    return (
        schema_contract_from_builder(_build_pdf_v12_canonical_schema),
        schema_contract_from_builder(_build_pdf_v12_legacy_v1_schema),
    )


def validate_pdf_metadata(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        _metadata_contract(),
        label="PDF metadata",
    )


def validate_pdf_schema(connection: sqlite3.Connection) -> None:
    failures: list[str] = []
    for contract in _pdf_schema_contracts():
        try:
            validate_sqlite_schema_contract(
                connection,
                contract,
                label="PDF",
                exact=True,
            )
        except SQLiteSchemaContractError as exc:
            failures.append(str(exc))
        else:
            return
    raise SQLiteSchemaContractError(
        "PDF schema contract is invalid for every supported additive layout: "
        + " | ".join(failures)
    )


# endregion [03]


__all__ = [
    "PDF_SCHEMA_VERSION",
    "UNKNOWN_BIRTHTIME_NS",
    "create_fresh_pdf_schema",
    "migrate_pdf_schema",
    "validate_pdf_metadata",
    "validate_pdf_schema",
]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_schema")
