"""Durable transcript, segment and full-text state for the audio route."""

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


# region [01] Connections and additive schema


AUDIO_SCHEMA_VERSION = 2
_PATH_COLLATION = sqlite_path_collation()

_AUDIO_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="audio state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=65_536,
        wal_autocheckpoint_pages=2_048,
        journal_size_limit_bytes=268_435_456,
    ),
)


def _audio_schema_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported audio path collation: {path_collation}")
    return (
        """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
        f"""CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE {path_collation},
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT,
        duration_seconds REAL,
        speech_duration_seconds REAL,
        language TEXT,
        language_probability REAL,
        model_name TEXT,
        backend_version TEXT,
        device TEXT,
        compute_type TEXT,
        media_metadata_json TEXT NOT NULL DEFAULT '{{}}',
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        segment_count INTEGER NOT NULL DEFAULT 0,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        review_disposition TEXT NOT NULL DEFAULT 'none',
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE UNIQUE INDEX IF NOT EXISTS audio_documents_path_idx
        ON documents(path)""",
        """CREATE INDEX IF NOT EXISTS audio_documents_status_idx
        ON documents(status,review_disposition,path)""",
        f"""CREATE TABLE IF NOT EXISTS audio_inventory(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE {path_collation},
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE UNIQUE INDEX IF NOT EXISTS audio_inventory_path_idx
        ON audio_inventory(path)""",
        """CREATE INDEX IF NOT EXISTS audio_inventory_run_idx
        ON audio_inventory(last_seen_run_id,file_key)""",
        """CREATE TABLE IF NOT EXISTS segments(
        file_key TEXT NOT NULL,
        segment_index INTEGER NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        text TEXT NOT NULL,
        avg_logprob REAL,
        no_speech_probability REAL,
        PRIMARY KEY(file_key,segment_index),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
        """CREATE INDEX IF NOT EXISTS audio_segments_time_idx
        ON segments(file_key,start_ms,end_ms)""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        title,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    )


_AUDIO_SCHEMA_DDL = _audio_schema_ddl(_PATH_COLLATION)
_AUDIO_V1_SCHEMA_DDL = _audio_schema_ddl("NOCASE")


def _create_audio_schema(connection: sqlite3.Connection) -> None:
    for statement in _AUDIO_SCHEMA_DDL:
        connection.execute(statement)


def _create_audio_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in _AUDIO_V1_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _audio_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_audio_schema)


@lru_cache(maxsize=1)
def _audio_v1_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_audio_v1_schema)


def _migrate_audio_v1_path_collation(connection: sqlite3.Connection) -> None:
    if _PATH_COLLATION == "NOCASE":
        return
    row_counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("documents", "audio_inventory", "segments", "transcript_fts")
    }
    connection.execute("CREATE TEMP TABLE audio_documents_v1 AS SELECT * FROM documents")
    connection.execute("CREATE TEMP TABLE audio_inventory_v1 AS SELECT * FROM audio_inventory")
    connection.execute("CREATE TEMP TABLE audio_segments_v1 AS SELECT * FROM segments")
    connection.execute(
        """CREATE TEMP TABLE audio_transcript_fts_v1 AS
        SELECT rowid AS source_rowid,file_key,path,title,body FROM transcript_fts"""
    )
    connection.execute("DROP TABLE transcript_fts")
    connection.execute("DROP TABLE segments")
    connection.execute("DROP TABLE audio_inventory")
    connection.execute("DROP TABLE documents")
    _create_audio_schema(connection)
    connection.execute("INSERT INTO documents SELECT * FROM audio_documents_v1")
    connection.execute("INSERT INTO audio_inventory SELECT * FROM audio_inventory_v1")
    connection.execute("INSERT INTO segments SELECT * FROM audio_segments_v1")
    connection.execute(
        """INSERT INTO transcript_fts(rowid,file_key,path,title,body)
        SELECT source_rowid,file_key,path,title,body FROM audio_transcript_fts_v1"""
    )
    for table, expected in row_counts.items():
        actual = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if actual != expected:
            raise RuntimeError(f"audio path migration changed {table} row count")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("audio path migration foreign-key validation failed")
    connection.execute("DROP TABLE audio_documents_v1")
    connection.execute("DROP TABLE audio_inventory_v1")
    connection.execute("DROP TABLE audio_segments_v1")
    connection.execute("DROP TABLE audio_transcript_fts_v1")


@contextmanager
def audio_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
):
    """Open audio state, optionally refusing creation after initialization."""

    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    connection = connect_sqlite(
        path,
        mode=mode,
        policy=_AUDIO_SQLITE_POLICY,
    )
    try:
        yield connection
    finally:
        connection.close()


def initialize_audio_state(path: Path) -> None:
    """Create the audio cache without replacing prior transcripts."""

    prior: int | None = None
    if path.is_file():
        with audio_database(path, readonly=True) as connection:
            prior = read_metadata_schema_version(connection, label="audio")
            if prior is not None and prior > AUDIO_SCHEMA_VERSION:
                raise RuntimeError(
                    f"audio schema {prior} is newer than supported schema {AUDIO_SCHEMA_VERSION}"
                )
            if prior == AUDIO_SCHEMA_VERSION:
                validate_sqlite_schema_contract(
                    connection,
                    _audio_schema_contract(),
                    label="audio",
                    exact=True,
                )
                return
            if prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _audio_v1_schema_contract(),
                    label="audio schema 1 migration source",
                    exact=True,
                )
            elif prior not in {None, 0}:
                raise RuntimeError(f"unsupported audio migration start: {prior}")

    with audio_database(path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            locked_prior = read_metadata_schema_version(connection, label="audio")
            if locked_prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _audio_v1_schema_contract(),
                    label="audio schema 1 migration source",
                    exact=True,
                )
                _migrate_audio_v1_path_collation(connection)
            elif locked_prior not in {None, 0, AUDIO_SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported audio migration start: {locked_prior}")
            _create_audio_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(AUDIO_SCHEMA_VERSION),),
            )
            validate_sqlite_schema_contract(
                connection,
                _audio_schema_contract(),
                label="audio",
                exact=True,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


# endregion [01]
