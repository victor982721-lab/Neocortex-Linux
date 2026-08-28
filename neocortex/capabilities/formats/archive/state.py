"""Durable, read-only-queryable state for members contained in ZIP archives."""

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

from _04_Nucleo_Operativo.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Schema and connection contract


ARCHIVE_SCHEMA_VERSION = 1
MAX_ARCHIVE_QUERY_RESULTS = 1_000

_ARCHIVE_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="archive state",
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

_ARCHIVE_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS containers(
        container_key TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        member_count INTEGER NOT NULL DEFAULT 0,
        indexed_count INTEGER NOT NULL DEFAULT 0,
        metadata_only_count INTEGER NOT NULL DEFAULT 0,
        nested_archive_count INTEGER NOT NULL DEFAULT 0,
        issue_count INTEGER NOT NULL DEFAULT 0,
        text_chars INTEGER NOT NULL DEFAULT 0,
        max_depth INTEGER NOT NULL DEFAULT 0,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS archive_containers_path_idx
        ON containers(path)""",
    """CREATE INDEX IF NOT EXISTS archive_containers_status_idx
        ON containers(status,last_seen_run_id,path)""",
    """CREATE TABLE IF NOT EXISTS documents(
        file_key TEXT PRIMARY KEY,
        container_key TEXT NOT NULL REFERENCES containers(container_key)
            ON DELETE CASCADE,
        path TEXT NOT NULL,
        container_path TEXT NOT NULL,
        member_chain TEXT NOT NULL,
        member_path TEXT NOT NULL,
        archive_depth INTEGER NOT NULL,
        content_kind TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        compressed_size INTEGER NOT NULL,
        crc32 INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL,
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0,
        text_xxh3_128 TEXT,
        detail TEXT,
        error_type TEXT,
        error_message TEXT,
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS archive_documents_path_idx
        ON documents(path)""",
    """CREATE INDEX IF NOT EXISTS archive_documents_container_idx
        ON documents(container_key,archive_depth,member_chain)""",
    """CREATE INDEX IF NOT EXISTS archive_documents_status_idx
        ON documents(status,content_kind,container_path,member_chain)""",
    """CREATE TABLE IF NOT EXISTS archive_issues(
        issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        container_key TEXT NOT NULL REFERENCES containers(container_key)
            ON DELETE CASCADE,
        member_chain TEXT,
        archive_depth INTEGER NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_ns INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS archive_issues_container_idx
        ON archive_issues(container_key,issue_id)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        container_path UNINDEXED,
        container_name,
        member_chain,
        content_kind,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
)


def _create_archive_schema(connection: sqlite3.Connection) -> None:
    for statement in _ARCHIVE_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def archive_schema_contract() -> SQLiteSchemaContract:
    """Return the exact schema understood by writers and Knowledge readers."""

    return schema_contract_from_builder(_create_archive_schema)


@contextmanager
def archive_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
):
    """Open archive state, optionally refusing creation or any write access."""

    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    connection = connect_sqlite(path, mode=mode, policy=_ARCHIVE_SQLITE_POLICY)
    try:
        yield connection
    finally:
        connection.close()


def initialize_archive_state(path: Path) -> None:
    """Create schema v1 additively and fail closed on future or corrupt state."""

    prior: int | None = None
    if path.is_file():
        with archive_database(path, readonly=True) as connection:
            prior = read_metadata_schema_version(connection, label="archive")
            if prior is not None and prior > ARCHIVE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"archive schema {prior} is newer than supported schema "
                    f"{ARCHIVE_SCHEMA_VERSION}"
                )
            if prior == ARCHIVE_SCHEMA_VERSION:
                validate_sqlite_schema_contract(
                    connection,
                    archive_schema_contract(),
                    label="archive",
                    exact=True,
                )
                return

    with archive_database(path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _create_archive_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(ARCHIVE_SCHEMA_VERSION),),
            )
            validate_sqlite_schema_contract(
                connection,
                archive_schema_contract(),
                label="archive",
                exact=True,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _validate_reader(connection: sqlite3.Connection) -> None:
    observed = read_metadata_schema_version(connection, label="archive")
    if observed != ARCHIVE_SCHEMA_VERSION:
        raise RuntimeError(
            f"archive schema {observed!r} is incompatible with schema {ARCHIVE_SCHEMA_VERSION}"
        )
    validate_sqlite_schema_contract(
        connection,
        archive_schema_contract(),
        label="archive",
        exact=True,
    )


# endregion [01]


# region [02] Bounded read-only status and query API


@dataclass(frozen=True, slots=True)
class ArchiveStatus:
    available: bool
    schema_version: int | None = None
    containers: int = 0
    complete: int = 0
    partial: int = 0
    errors: int = 0
    members: int = 0
    indexed: int = 0
    metadata_only: int = 0
    nested_archives: int = 0
    issues: int = 0
    text_chars: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArchiveSearchHit:
    file_key: str
    virtual_path: str
    container_path: str
    member_chain: str
    member_path: str
    archive_depth: int
    content_kind: str
    media_type: str
    status: str
    size: int
    snippet: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def read_archive_status(path: Path) -> ArchiveStatus:
    """Read aggregate state without creating, migrating or checkpointing it."""

    if not path.is_file():
        return ArchiveStatus(False)
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        row = connection.execute(
            """SELECT COUNT(*) AS containers,
            SUM(status='complete') AS complete,
            SUM(status='partial') AS partial,
            SUM(status='error') AS errors,
            COALESCE(SUM(member_count),0) AS members,
            COALESCE(SUM(indexed_count),0) AS indexed,
            COALESCE(SUM(metadata_only_count),0) AS metadata_only,
            COALESCE(SUM(nested_archive_count),0) AS nested_archives,
            COALESCE(SUM(issue_count),0) AS issues,
            COALESCE(SUM(text_chars),0) AS text_chars
            FROM containers"""
        ).fetchone()
    assert row is not None
    return ArchiveStatus(
        True,
        ARCHIVE_SCHEMA_VERSION,
        int(row["containers"]),
        int(row["complete"] or 0),
        int(row["partial"] or 0),
        int(row["errors"] or 0),
        int(row["members"] or 0),
        int(row["indexed"] or 0),
        int(row["metadata_only"] or 0),
        int(row["nested_archives"] or 0),
        int(row["issues"] or 0),
        int(row["text_chars"] or 0),
    )


def _validate_result_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_ARCHIVE_QUERY_RESULTS:
        raise ValueError(f"archive result limit must be between 1 and {MAX_ARCHIVE_QUERY_RESULTS}")


def _escaped_like_fragment(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_hit(row: sqlite3.Row) -> ArchiveSearchHit:
    return ArchiveSearchHit(
        file_key=str(row["file_key"]),
        virtual_path=str(row["path"]),
        container_path=str(row["container_path"]),
        member_chain=str(row["member_chain"]),
        member_path=str(row["member_path"]),
        archive_depth=int(row["archive_depth"]),
        content_kind=str(row["content_kind"]),
        media_type=str(row["media_type"]),
        status=str(row["status"]),
        size=int(row["size"]),
        snippet=None if row["snippet"] is None else str(row["snippet"]),
    )


def search_archive_state(
    path: Path,
    query: str,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
) -> tuple[ArchiveSearchHit, ...]:
    """Search member names and extracted text through read-only FTS5 state."""

    from _04_Nucleo_Operativo.semantic_lexical import compile_natural_fts_query

    _validate_result_limit(limit)
    normalized = compile_natural_fts_query(query)
    container_pattern = (
        None
        if container_fragment is None
        else f"%{_escaped_like_fragment(container_fragment.strip())}%"
    )
    if container_fragment is not None and not container_fragment.strip():
        raise ValueError("archive container fragment must be non-empty")
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """WITH ranked AS MATERIALIZED (
            SELECT f.rowid AS fts_rowid,f.file_key,f.path,
            bm25(document_fts) AS raw_bm25
            FROM document_fts AS f JOIN documents AS d ON d.file_key=f.file_key
            WHERE document_fts MATCH ?1
            AND (?2 IS NULL OR d.container_path LIKE ?2 ESCAPE '\\')
            ORDER BY raw_bm25,f.path COLLATE NOCASE LIMIT ?3
            )
            SELECT d.file_key,d.path,d.container_path,d.member_chain,d.member_path,
            d.archive_depth,d.content_kind,d.media_type,d.status,d.size,
            snippet(document_fts,6,'[',']',' ... ',24) AS snippet
            FROM ranked JOIN document_fts ON document_fts.rowid=ranked.fts_rowid
            JOIN documents AS d ON d.file_key=ranked.file_key
            WHERE document_fts MATCH ?1
            ORDER BY ranked.raw_bm25,ranked.path COLLATE NOCASE""",
            (normalized, container_pattern, limit),
        ).fetchall()
    return tuple(_row_to_hit(row) for row in rows)


def list_archive_members(
    path: Path,
    limit: int = 20,
    *,
    container_fragment: str | None = None,
) -> tuple[ArchiveSearchHit, ...]:
    """List a bounded page of members in stable container/chain order."""

    _validate_result_limit(limit)
    container_pattern = (
        None
        if container_fragment is None
        else f"%{_escaped_like_fragment(container_fragment.strip())}%"
    )
    if container_fragment is not None and not container_fragment.strip():
        raise ValueError("archive container fragment must be non-empty")
    with archive_database(path, readonly=True) as connection:
        _validate_reader(connection)
        rows = connection.execute(
            """SELECT file_key,path,container_path,member_chain,member_path,
            archive_depth,content_kind,media_type,status,size,NULL AS snippet
            FROM documents
            WHERE (? IS NULL OR container_path LIKE ? ESCAPE '\\')
            ORDER BY container_path COLLATE NOCASE,member_chain COLLATE NOCASE
            LIMIT ?""",
            (container_pattern, container_pattern, limit),
        ).fetchall()
    return tuple(_row_to_hit(row) for row in rows)


# endregion [02]


__all__ = (
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveSearchHit",
    "ArchiveStatus",
    "archive_database",
    "archive_schema_contract",
    "initialize_archive_state",
    "list_archive_members",
    "read_archive_status",
    "search_archive_state",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.archive_state"
del _defined_value
