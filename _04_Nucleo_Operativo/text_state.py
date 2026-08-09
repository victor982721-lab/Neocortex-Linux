"""Durable state and read-only queries for generic physical text documents."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
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

from .sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


TEXT_SCHEMA_VERSION = 1
MAX_TEXT_QUERY_RESULTS = 1_000

_TEXT_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="text state",
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

_TEXT_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        content_kind TEXT NOT NULL,
        media_type TEXT NOT NULL,
        title TEXT,
        author TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        text_truncated INTEGER NOT NULL DEFAULT 0,
        detail TEXT,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS text_documents_path_idx
        ON documents(path)""",
    """CREATE INDEX IF NOT EXISTS text_documents_status_idx
        ON documents(status,content_kind,path)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        content_kind,
        title,
        author,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
)


def _create_text_schema(connection: sqlite3.Connection) -> None:
    for statement in _TEXT_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def text_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_text_schema)


@contextmanager
def text_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
):
    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    connection = connect_sqlite(path, mode=mode, policy=_TEXT_SQLITE_POLICY)
    try:
        yield connection
    finally:
        connection.close()


def initialize_text_state(path: Path) -> None:
    prior: int | None = None
    if path.is_file():
        with text_database(path, readonly=True) as connection:
            prior = read_metadata_schema_version(connection, label="text")
            if prior is not None and prior > TEXT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"text schema {prior} is newer than supported schema {TEXT_SCHEMA_VERSION}"
                )
            if prior == TEXT_SCHEMA_VERSION:
                validate_sqlite_schema_contract(
                    connection,
                    text_schema_contract(),
                    label="text",
                    exact=True,
                )
                return
    with text_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _create_text_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(TEXT_SCHEMA_VERSION),),
            )
            validate_sqlite_schema_contract(
                connection,
                text_schema_contract(),
                label="text",
                exact=True,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _validate_reader(connection: sqlite3.Connection) -> None:
    observed = read_metadata_schema_version(connection, label="text")
    if observed != TEXT_SCHEMA_VERSION:
        raise RuntimeError(
            f"text schema {observed!r} is incompatible with schema {TEXT_SCHEMA_VERSION}"
        )
    validate_sqlite_schema_contract(
        connection,
        text_schema_contract(),
        label="text",
        exact=True,
    )


@dataclass(frozen=True, slots=True)
class TextStatus:
    available: bool
    schema_version: int | None = None
    documents: int = 0
    complete: int = 0
    errors: int = 0
    text_chars: int = 0
    content_kinds: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TextSearchHit:
    file_key: str
    path: str
    content_kind: str
    media_type: str
    status: str
    title: str | None
    author: str | None
    size: int
    snippet: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_text_status(path: Path) -> TextStatus:
    if not path.is_file():
        return TextStatus(False)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            """SELECT COUNT(*) AS documents,SUM(status='complete') AS complete,
            SUM(status='error') AS errors,COALESCE(SUM(text_chars),0) AS text_chars,
            COUNT(DISTINCT content_kind) AS content_kinds FROM documents"""
        ).fetchone()
    assert row is not None
    return TextStatus(
        True,
        TEXT_SCHEMA_VERSION,
        int(row["documents"]),
        int(row["complete"] or 0),
        int(row["errors"] or 0),
        int(row["text_chars"] or 0),
        int(row["content_kinds"] or 0),
    )


def search_text_state(path: Path, query: str, limit: int = 20) -> tuple[TextSearchHit, ...]:
    from .semantic_lexical import compile_natural_fts_query

    if not 1 <= limit <= MAX_TEXT_QUERY_RESULTS:
        raise ValueError(f"text result limit must be between 1 and {MAX_TEXT_QUERY_RESULTS}")
    normalized = compile_natural_fts_query(query)
    with text_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """WITH ranked AS MATERIALIZED (
            SELECT f.rowid AS fts_rowid,f.file_key,f.path,
            bm25(document_fts) AS raw_bm25
            FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
            WHERE document_fts MATCH ?1 AND d.status='complete'
            ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?2
            )
            SELECT d.file_key,d.path,d.content_kind,d.media_type,d.status,
            d.title,d.author,d.size,
            snippet(document_fts,5,'[',']',' ... ',24) AS snippet
            FROM ranked JOIN document_fts ON document_fts.rowid=ranked.fts_rowid
            JOIN documents AS d ON d.file_key=ranked.file_key
            WHERE document_fts MATCH ?1
            ORDER BY ranked.raw_bm25,ranked.path COLLATE NOCASE""",
            (normalized, limit),
        ).fetchall()
    return tuple(
        TextSearchHit(
            file_key=str(row["file_key"]),
            path=str(row["path"]),
            content_kind=str(row["content_kind"]),
            media_type=str(row["media_type"]),
            status=str(row["status"]),
            title=None if row["title"] is None else str(row["title"]),
            author=None if row["author"] is None else str(row["author"]),
            size=int(row["size"]),
            snippet=None if row["snippet"] is None else str(row["snippet"]),
        )
        for row in rows
    )


__all__ = (
    "TEXT_SCHEMA_VERSION",
    "TextSearchHit",
    "TextStatus",
    "initialize_text_state",
    "read_text_status",
    "search_text_state",
    "text_database",
    "text_schema_contract",
)
