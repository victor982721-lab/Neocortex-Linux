"""Canonical DOCX schema, additive migrations and structural validation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from functools import lru_cache

from neocortex.platform_policy import sqlite_path_collation

from .sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Versions and canonical DDL


DOCX_SCHEMA_VERSION = 6
UNKNOWN_BIRTHTIME_NS = -1
_PATH_COLLATION = sqlite_path_collation()


_DOCX_V5_TABLE_DDL = (
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
        integrity_status TEXT NOT NULL DEFAULT 'unknown',
        failure_code TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        review_disposition TEXT NOT NULL DEFAULT 'unknown',
        recovery_mode TEXT NOT NULL DEFAULT 'none',
        title TEXT,
        author TEXT,
        created TEXT,
        modified TEXT,
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        paragraph_count INTEGER NOT NULL DEFAULT 0,
        table_count INTEGER NOT NULL DEFAULT 0,
        image_count INTEGER NOT NULL DEFAULT 0,
        section_count INTEGER NOT NULL DEFAULT 0,
        layout_class TEXT,
        layout_signature TEXT,
        layout_json TEXT,
        error_type TEXT,
        error_message TEXT,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS docx_inventory(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        last_seen_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS document_parts(
        file_key TEXT NOT NULL,
        part_name TEXT NOT NULL,
        part_kind TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        text_zlib BLOB NOT NULL,
        text_chars INTEGER NOT NULL,
        PRIMARY KEY(file_key,part_name),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS document_diagnostics(
        file_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        part_name TEXT,
        stage TEXT NOT NULL,
        code TEXT NOT NULL,
        message TEXT NOT NULL,
        required INTEGER NOT NULL,
        retryable INTEGER NOT NULL,
        disposition TEXT NOT NULL,
        expected_size INTEGER,
        actual_size INTEGER,
        expected_crc32 INTEGER,
        actual_crc32 INTEGER,
        PRIMARY KEY(file_key,ordinal),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        title,
        author,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TABLE IF NOT EXISTS pdf_counterparts(
        docx_file_key TEXT NOT NULL,
        pdf_path TEXT,
        match_status TEXT NOT NULL,
        match_method TEXT NOT NULL,
        candidate_count INTEGER NOT NULL,
        checked_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(docx_file_key,pdf_path),
        FOREIGN KEY(docx_file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS layout_groups(
        layout_signature TEXT PRIMARY KEY,
        layout_class TEXT NOT NULL,
        member_count INTEGER NOT NULL,
        representative_file_key TEXT NOT NULL,
        updated_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
)


_DOCX_V5_PATH_INDEX_DDL = (
    """CREATE UNIQUE INDEX IF NOT EXISTS docx_documents_path_idx
        ON documents(path COLLATE NOCASE)""",
    """CREATE INDEX IF NOT EXISTS docx_documents_review_idx
        ON documents(review_disposition,status,path COLLATE NOCASE)""",
)


_DOCX_V5_INDEX_DDL = (
    """CREATE INDEX IF NOT EXISTS docx_inventory_run_idx
        ON docx_inventory(last_seen_run_id,file_key)""",
    """CREATE INDEX IF NOT EXISTS pdf_counterparts_status_idx
        ON pdf_counterparts(match_status,docx_file_key)""",
    *_DOCX_V5_PATH_INDEX_DDL,
    """CREATE INDEX IF NOT EXISTS docx_documents_layout_idx
        ON documents(layout_signature,status)""",
    """CREATE INDEX IF NOT EXISTS docx_diagnostics_code_idx
        ON document_diagnostics(code,file_key)""",
)


def _docx_table_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported DOCX path collation: {path_collation}")
    return tuple(
        statement.replace("COLLATE NOCASE", f"COLLATE {path_collation}").replace(
            "pdf_path TEXT,",
            f"pdf_path TEXT COLLATE {path_collation},",
        )
        for statement in _DOCX_V5_TABLE_DDL
    )


def _docx_path_index_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported DOCX path collation: {path_collation}")
    return tuple(
        statement.replace("COLLATE NOCASE", f"COLLATE {path_collation}")
        for statement in _DOCX_V5_PATH_INDEX_DDL
    )


_DOCX_TABLE_DDL = _docx_table_ddl(_PATH_COLLATION)
_DOCX_PATH_INDEX_DDL = _docx_path_index_ddl(_PATH_COLLATION)
_DOCX_INDEX_DDL = (
    tuple(statement for statement in _DOCX_V5_INDEX_DDL if statement not in _DOCX_V5_PATH_INDEX_DDL)
    + _DOCX_PATH_INDEX_DDL
)


_DOCUMENT_ADDITIONS = (
    ("size", "INTEGER NOT NULL DEFAULT 0"),
    ("mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
    ("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),
    ("processing_signature", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'error'"),
    ("integrity_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("failure_code", "TEXT"),
    ("retryable", "INTEGER NOT NULL DEFAULT 0"),
    ("review_disposition", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("recovery_mode", "TEXT NOT NULL DEFAULT 'none'"),
    ("title", "TEXT"),
    ("author", "TEXT"),
    ("created", "TEXT"),
    ("modified", "TEXT"),
    ("text_zlib", "BLOB"),
    ("text_chars", "INTEGER NOT NULL DEFAULT 0"),
    ("text_xxh3_128", "TEXT"),
    ("paragraph_count", "INTEGER NOT NULL DEFAULT 0"),
    ("table_count", "INTEGER NOT NULL DEFAULT 0"),
    ("image_count", "INTEGER NOT NULL DEFAULT 0"),
    ("section_count", "INTEGER NOT NULL DEFAULT 0"),
    ("layout_class", "TEXT"),
    ("layout_signature", "TEXT"),
    ("layout_json", "TEXT"),
    ("error_type", "TEXT"),
    ("error_message", "TEXT"),
    ("last_seen_run_id", "INTEGER NOT NULL DEFAULT 0"),
    ("updated_ns", "INTEGER NOT NULL DEFAULT 0"),
)


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
        "docx_inventory",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )
    _create_indexes_from(connection, index_ddl)


def _ensure_current_structure(connection: sqlite3.Connection) -> None:
    _ensure_structure(
        connection,
        table_ddl=_DOCX_TABLE_DDL,
        index_ddl=_DOCX_INDEX_DDL,
    )


def _ensure_v5_structure(connection: sqlite3.Connection) -> None:
    _ensure_structure(
        connection,
        table_ddl=_DOCX_V5_TABLE_DDL,
        index_ddl=_DOCX_V5_INDEX_DDL,
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
        "docx_inventory",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )


def _migrate_explicit_path_collations(connection: sqlite3.Connection) -> None:
    """Make path uniqueness deterministic for every historical table layout."""

    connection.execute("DROP INDEX IF EXISTS docx_documents_path_idx")
    connection.execute("DROP INDEX IF EXISTS docx_documents_review_idx")
    for statement in _DOCX_V5_PATH_INDEX_DDL:
        connection.execute(statement)


_DOCX_PATH_REBUILD_TABLES = (
    "documents",
    "docx_inventory",
    "document_parts",
    "document_diagnostics",
    "pdf_counterparts",
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
            f"DOCX path migration columns changed for {target}: "
            f"source={source_columns!r} target={target_columns!r}"
        )
    columns = ",".join(_quoted_identifier(column) for column in target_columns)
    connection.execute(
        f"INSERT INTO main.{_quoted_identifier(target)}({columns}) "
        f"SELECT {columns} FROM temp.{_quoted_identifier(source)}"
    )


def _validate_docx_v5_schema(connection: sqlite3.Connection) -> None:
    failures: list[str] = []
    for contract in _docx_v5_schema_contracts():
        try:
            validate_sqlite_schema_contract(
                connection,
                contract,
                label="DOCX schema 5 migration source",
                exact=True,
            )
        except SQLiteSchemaContractError as exc:
            failures.append(str(exc))
        else:
            return
    raise SQLiteSchemaContractError(
        "DOCX schema 5 migration source is invalid for every supported layout: "
        + " | ".join(failures)
    )


def _migrate_platform_path_collation(connection: sqlite3.Connection) -> None:
    """Rebuild path-owning tables without discarding children or FTS rows."""

    _validate_docx_v5_schema(connection)
    row_counts = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
        )
        for table in _DOCX_PATH_REBUILD_TABLES
    }
    fts_count = int(connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0])
    for table in _DOCX_PATH_REBUILD_TABLES:
        backup = f"docx_v5_{table}"
        connection.execute(
            f"CREATE TEMP TABLE {_quoted_identifier(backup)} AS "
            f"SELECT * FROM main.{_quoted_identifier(table)}"
        )

    for table in ("document_parts", "document_diagnostics", "pdf_counterparts"):
        connection.execute(f"DROP TABLE {_quoted_identifier(table)}")
    connection.execute("DROP TABLE documents")
    connection.execute("DROP TABLE docx_inventory")
    _ensure_current_structure(connection)

    for table in _DOCX_PATH_REBUILD_TABLES:
        backup = f"docx_v5_{table}"
        _copy_table_exact(connection, source=backup, target=table)
        connection.execute(f"DROP TABLE temp.{_quoted_identifier(backup)}")
        migrated_count = int(
            connection.execute(f"SELECT COUNT(*) FROM main.{_quoted_identifier(table)}").fetchone()[
                0
            ]
        )
        if migrated_count != row_counts[table]:
            raise RuntimeError(f"DOCX path migration changed {table} row count")
    if int(connection.execute("SELECT COUNT(*) FROM document_fts").fetchone()[0]) != fts_count:
        raise RuntimeError("DOCX path migration changed document FTS rows")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("DOCX path migration foreign-key validation failed")


_DOCX_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _no_data_migration,
    2: _migrate_birthtime,
    3: _no_data_migration,
    4: _migrate_explicit_path_collations,
    5: _migrate_platform_path_collation,
}


def create_fresh_docx_schema(connection: sqlite3.Connection) -> None:
    _ensure_current_structure(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
        (str(DOCX_SCHEMA_VERSION),),
    )


def migrate_docx_schema(connection: sqlite3.Connection, version: int) -> None:
    if version < 5:
        _ensure_v5_structure(connection)
    current = version
    while current < DOCX_SCHEMA_VERSION:
        migration = _DOCX_MIGRATIONS.get(current)
        if migration is None:  # pragma: no cover - module invariant
            raise RuntimeError(f"DOCX schema migration {current} is missing")
        migration(connection)
        current += 1
        updated = connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(current),),
        )
        if updated.rowcount != 1:
            raise RuntimeError("DOCX schema_version disappeared during migration")


# endregion [02]


# region [03] Canonical contracts


def _build_canonical_schema(connection: sqlite3.Connection) -> None:
    _ensure_current_structure(connection)


def _build_docx_v5_canonical_schema(connection: sqlite3.Connection) -> None:
    _ensure_v5_structure(connection)


def _build_docx_v5_legacy_v1_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE metadata(
            key TEXT PRIMARY KEY,value TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE documents(
            file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
            status TEXT NOT NULL
        ) WITHOUT ROWID""",
    )
    for statement in statements:
        connection.execute(statement)
    _ensure_v5_structure(connection)


def _build_docx_v5_legacy_v2_schema(connection: sqlite3.Connection) -> None:
    """Model the additive layout produced from the historical version-2 DDL."""

    statements = (
        """CREATE TABLE metadata(
            key TEXT PRIMARY KEY,value TEXT NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE documents(
            file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
            size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
            processing_signature TEXT NOT NULL,status TEXT NOT NULL,
            last_seen_run_id INTEGER NOT NULL,updated_ns INTEGER NOT NULL
        ) WITHOUT ROWID""",
        """CREATE TABLE docx_inventory(
            file_key TEXT PRIMARY KEY,path TEXT NOT NULL,
            size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
            last_seen_run_id INTEGER NOT NULL
        ) WITHOUT ROWID""",
    )
    for statement in statements:
        connection.execute(statement)
    _ensure_v5_structure(connection)


def _build_metadata_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_DOCX_TABLE_DDL[0])


@lru_cache(maxsize=1)
def _metadata_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_metadata_schema)


@lru_cache(maxsize=1)
def _docx_schema_contracts() -> tuple[SQLiteSchemaContract, ...]:
    return (schema_contract_from_builder(_build_canonical_schema),)


@lru_cache(maxsize=1)
def _docx_v5_schema_contracts() -> tuple[SQLiteSchemaContract, ...]:
    return (
        schema_contract_from_builder(_build_docx_v5_canonical_schema),
        schema_contract_from_builder(_build_docx_v5_legacy_v1_schema),
        schema_contract_from_builder(_build_docx_v5_legacy_v2_schema),
    )


def validate_docx_metadata(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        _metadata_contract(),
        label="DOCX metadata",
    )


def validate_docx_schema(connection: sqlite3.Connection) -> None:
    failures: list[str] = []
    for contract in _docx_schema_contracts():
        try:
            validate_sqlite_schema_contract(
                connection,
                contract,
                label="DOCX",
                exact=True,
            )
        except SQLiteSchemaContractError as exc:
            failures.append(str(exc))
        else:
            return
    raise SQLiteSchemaContractError(
        "DOCX schema contract is invalid for every supported additive layout: "
        + " | ".join(failures)
    )


# endregion [03]


__all__ = [
    "DOCX_SCHEMA_VERSION",
    "UNKNOWN_BIRTHTIME_NS",
    "create_fresh_docx_schema",
    "migrate_docx_schema",
    "validate_docx_metadata",
    "validate_docx_schema",
]
