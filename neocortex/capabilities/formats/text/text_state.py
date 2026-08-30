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

from neocortex.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


TEXT_SCHEMA_VERSION = 2
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

_TEXT_V1_SCHEMA_DDL = (
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

_TEXT_V2_BASE_SCHEMA_DDL = (
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
        updated_ns INTEGER NOT NULL,
        revision_id TEXT,
        FOREIGN KEY(revision_id) REFERENCES text_input_revisions(revision_id)
    ) WITHOUT ROWID""",
    """CREATE UNIQUE INDEX IF NOT EXISTS text_documents_path_idx
        ON documents(path)""",
    """CREATE INDEX IF NOT EXISTS text_documents_status_idx
        ON documents(status,content_kind,path)""",
    """CREATE INDEX IF NOT EXISTS text_documents_revision_idx
        ON documents(revision_id,file_key)""",
    """CREATE TRIGGER IF NOT EXISTS text_documents_revision_no_downgrade
        BEFORE UPDATE OF revision_id ON documents
        WHEN OLD.revision_id IS NOT NULL AND NEW.revision_id IS NULL BEGIN
            SELECT RAISE(ABORT,'published Text revision cannot become legacy');
        END""",
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

_TEXT_V2_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS text_input_revisions(
        revision_id TEXT PRIMARY KEY,
        resource_id TEXT NOT NULL,
        producer TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        generation INTEGER,
        revision_state TEXT NOT NULL,
        observed_at_utc TEXT,
        fingerprint_algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        recorded_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS text_input_revisions_resource_idx
        ON text_input_revisions(resource_id,recorded_ns,revision_id)""",
    """CREATE INDEX IF NOT EXISTS text_input_revisions_fingerprint_idx
        ON text_input_revisions(
            resource_id,fingerprint_algorithm,fingerprint,revision_id)""",
    """CREATE TRIGGER IF NOT EXISTS text_input_revisions_no_update
        BEFORE UPDATE ON text_input_revisions BEGIN
            SELECT RAISE(ABORT,'text input revisions are immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_input_revisions_no_delete
        BEFORE DELETE ON text_input_revisions BEGIN
            SELECT RAISE(ABORT,'text input revisions are immutable');
        END""",
    """CREATE TABLE IF NOT EXISTS text_derivation_attempts(
        attempt_id TEXT PRIMARY KEY,
        stage_id TEXT NOT NULL,
        stage_version TEXT NOT NULL,
        processing_signature TEXT NOT NULL,
        implementation_digest TEXT,
        provider TEXT,
        provider_version TEXT,
        model TEXT,
        model_version TEXT,
        model_digest TEXT,
        effective_configuration_json TEXT NOT NULL,
        runtime_json TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        started_monotonic_ns INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
        run_id TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        causation_id TEXT,
        status TEXT NOT NULL CHECK(status IN(
            'running','succeeded','failed','cancelled','abandoned')),
        receipt_id TEXT UNIQUE,
        finished_at_utc TEXT,
        duration_ns INTEGER CHECK(duration_ns IS NULL OR duration_ns>=0),
        execution_mode TEXT CHECK(execution_mode IS NULL OR execution_mode IN(
            'executed','cache_hit','replay','attempted','unknown')),
        reproducibility_class TEXT CHECK(
            reproducibility_class IS NULL OR reproducibility_class IN(
                'exact','environment_bound','seeded','equivalent','best_effort',
                'non_replayable')),
        failure_json TEXT,
        recorded_ns INTEGER NOT NULL,
        terminal_ns INTEGER,
        CHECK((status='running' AND receipt_id IS NULL AND finished_at_utc IS NULL
            AND duration_ns IS NULL AND execution_mode IS NULL
            AND reproducibility_class IS NULL AND failure_json IS NULL
            AND terminal_ns IS NULL)
          OR (status<>'running' AND receipt_id IS NOT NULL
            AND finished_at_utc IS NOT NULL AND duration_ns IS NOT NULL
            AND reproducibility_class IS NOT NULL AND terminal_ns IS NOT NULL)),
        FOREIGN KEY(receipt_id) REFERENCES text_work_receipts(receipt_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS text_derivation_attempts_status_idx
        ON text_derivation_attempts(status,stage_id,started_at_utc,attempt_id)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS text_derivation_attempts_correlation_idx
        ON text_derivation_attempts(correlation_id,attempt_number)""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_attempts_terminal_update_only
        BEFORE UPDATE ON text_derivation_attempts
        WHEN OLD.status<>'running' OR NEW.status='running'
          OR NEW.attempt_id IS NOT OLD.attempt_id
          OR NEW.stage_id IS NOT OLD.stage_id
          OR NEW.stage_version IS NOT OLD.stage_version
          OR NEW.processing_signature IS NOT OLD.processing_signature
          OR NEW.implementation_digest IS NOT OLD.implementation_digest
          OR NEW.provider IS NOT OLD.provider
          OR NEW.provider_version IS NOT OLD.provider_version
          OR NEW.model IS NOT OLD.model
          OR NEW.model_version IS NOT OLD.model_version
          OR NEW.model_digest IS NOT OLD.model_digest
          OR NEW.effective_configuration_json IS NOT OLD.effective_configuration_json
          OR NEW.runtime_json IS NOT OLD.runtime_json
          OR NEW.started_at_utc IS NOT OLD.started_at_utc
          OR NEW.started_monotonic_ns IS NOT OLD.started_monotonic_ns
          OR NEW.attempt_number IS NOT OLD.attempt_number
          OR NEW.run_id IS NOT OLD.run_id
          OR NEW.correlation_id IS NOT OLD.correlation_id
          OR NEW.causation_id IS NOT OLD.causation_id
          OR NEW.recorded_ns IS NOT OLD.recorded_ns
        BEGIN
            SELECT RAISE(ABORT,'Text derivation attempts permit one terminal transition only');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_attempts_no_delete
        BEFORE DELETE ON text_derivation_attempts BEGIN
            SELECT RAISE(ABORT,'Text derivation attempts are append-only');
        END""",
    """CREATE TABLE IF NOT EXISTS text_derivation_input_bindings(
        attempt_id TEXT NOT NULL,
        binding_name TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        fingerprint_algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        materialization_owner TEXT,
        materialization_kind TEXT,
        materialization_id TEXT,
        materialization_schema_version INTEGER,
        materialization_generation INTEGER,
        materialization_json TEXT,
        PRIMARY KEY(attempt_id,binding_name),
        FOREIGN KEY(attempt_id) REFERENCES text_derivation_attempts(attempt_id),
        FOREIGN KEY(revision_id) REFERENCES text_input_revisions(revision_id),
        CHECK((materialization_owner IS NULL AND materialization_kind IS NULL
            AND materialization_id IS NULL AND materialization_schema_version IS NULL
            AND materialization_generation IS NULL AND materialization_json IS NULL)
          OR (materialization_owner IS NOT NULL AND materialization_kind IS NOT NULL
            AND materialization_id IS NOT NULL
            AND materialization_schema_version IS NOT NULL
            AND materialization_json IS NOT NULL))
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS text_derivation_inputs_revision_idx
        ON text_derivation_input_bindings(revision_id,attempt_id,binding_name)""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_input_bindings_no_update
        BEFORE UPDATE ON text_derivation_input_bindings BEGIN
            SELECT RAISE(ABORT,'Text derivation input bindings are immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_input_bindings_no_delete
        BEFORE DELETE ON text_derivation_input_bindings BEGIN
            SELECT RAISE(ABORT,'Text derivation input bindings are immutable');
        END""",
    """CREATE TABLE IF NOT EXISTS text_materializations(
        owner TEXT NOT NULL,
        materialization_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK(schema_version>=1),
        resource_id TEXT,
        revision_id TEXT,
        generation INTEGER,
        producer_receipt_id TEXT NOT NULL,
        fingerprint_algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        materialization_json TEXT NOT NULL,
        recorded_ns INTEGER NOT NULL,
        PRIMARY KEY(owner,materialization_id),
        FOREIGN KEY(revision_id) REFERENCES text_input_revisions(revision_id),
        FOREIGN KEY(producer_receipt_id) REFERENCES text_work_receipts(receipt_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS text_materializations_resource_idx
        ON text_materializations(resource_id,recorded_ns,materialization_id)""",
    """CREATE INDEX IF NOT EXISTS text_materializations_receipt_idx
        ON text_materializations(producer_receipt_id,owner,materialization_id)""",
    """CREATE TRIGGER IF NOT EXISTS text_materializations_no_update
        BEFORE UPDATE ON text_materializations BEGIN
            SELECT RAISE(ABORT,'Text materializations are immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_materializations_no_delete
        BEFORE DELETE ON text_materializations BEGIN
            SELECT RAISE(ABORT,'Text materializations are immutable');
        END""",
    """CREATE TABLE IF NOT EXISTS text_derivation_output_bindings(
        attempt_id TEXT NOT NULL,
        binding_name TEXT NOT NULL,
        materialization_owner TEXT NOT NULL,
        materialization_id TEXT NOT NULL,
        fingerprint_algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        PRIMARY KEY(attempt_id,binding_name),
        FOREIGN KEY(attempt_id) REFERENCES text_derivation_attempts(attempt_id),
        FOREIGN KEY(materialization_owner,materialization_id)
            REFERENCES text_materializations(owner,materialization_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS text_derivation_outputs_materialization_idx
        ON text_derivation_output_bindings(
            materialization_owner,materialization_id,attempt_id,binding_name)""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_output_bindings_no_update
        BEFORE UPDATE ON text_derivation_output_bindings BEGIN
            SELECT RAISE(ABORT,'Text derivation output bindings are immutable');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_output_bindings_no_delete
        BEFORE DELETE ON text_derivation_output_bindings BEGIN
            SELECT RAISE(ABORT,'Text derivation output bindings are immutable');
        END""",
    """CREATE TABLE IF NOT EXISTS text_work_receipts(
        receipt_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL UNIQUE,
        receipt_json TEXT NOT NULL,
        receipt_fingerprint TEXT NOT NULL UNIQUE,
        outcome TEXT NOT NULL CHECK(outcome IN(
            'succeeded','failed','cancelled','abandoned')),
        recorded_ns INTEGER NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES text_derivation_attempts(attempt_id)
    ) WITHOUT ROWID""",
    """CREATE TRIGGER IF NOT EXISTS text_work_receipts_no_update
        BEFORE UPDATE ON text_work_receipts BEGIN
            SELECT RAISE(ABORT,'text work receipts are append-only');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_work_receipts_no_delete
        BEFORE DELETE ON text_work_receipts BEGIN
            SELECT RAISE(ABORT,'text work receipts are append-only');
        END""",
    """CREATE TABLE IF NOT EXISTS text_materialization_heads(
        resource_id TEXT NOT NULL,
        materialization_kind TEXT NOT NULL,
        materialization_owner TEXT NOT NULL,
        materialization_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        producer_receipt_id TEXT NOT NULL,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(resource_id,materialization_kind),
        FOREIGN KEY(materialization_owner,materialization_id)
            REFERENCES text_materializations(owner,materialization_id),
        FOREIGN KEY(revision_id) REFERENCES text_input_revisions(revision_id),
        FOREIGN KEY(producer_receipt_id) REFERENCES text_work_receipts(receipt_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS text_derivation_outbox(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        receipt_id TEXT NOT NULL UNIQUE,
        occurred_ns INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES text_derivation_attempts(attempt_id),
        FOREIGN KEY(receipt_id) REFERENCES text_work_receipts(receipt_id)
    )""",
    """CREATE INDEX IF NOT EXISTS text_derivation_outbox_attempt_idx
        ON text_derivation_outbox(attempt_id,sequence)""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_outbox_no_update
        BEFORE UPDATE ON text_derivation_outbox BEGIN
            SELECT RAISE(ABORT,'text derivation outbox is append-only');
        END""",
    """CREATE TRIGGER IF NOT EXISTS text_derivation_outbox_no_delete
        BEFORE DELETE ON text_derivation_outbox BEGIN
            SELECT RAISE(ABORT,'text derivation outbox is append-only');
        END""",
)


def _create_text_schema(connection: sqlite3.Connection) -> None:
    for statement in _TEXT_V2_BASE_SCHEMA_DDL + _TEXT_V2_SCHEMA_DDL:
        connection.execute(statement)


def _create_text_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in _TEXT_V1_SCHEMA_DDL:
        connection.execute(statement)


def _create_text_derivation_schema(connection: sqlite3.Connection) -> None:
    for statement in _TEXT_V2_SCHEMA_DDL:
        connection.execute(statement)


def _migrate_text_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add owner-local lineage while preserving every legacy document and FTS row."""

    _create_text_derivation_schema(connection)
    connection.execute("ALTER TABLE documents RENAME TO documents_v1")
    connection.execute(_TEXT_V2_BASE_SCHEMA_DDL[1].replace(" IF NOT EXISTS", ""))
    connection.execute(
        """INSERT INTO documents(
        file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
        text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
        last_seen_run_id,updated_ns,revision_id)
        SELECT file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
        text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
        last_seen_run_id,updated_ns,NULL FROM documents_v1"""
    )
    connection.execute("DROP TABLE documents_v1")
    for statement in _TEXT_V2_BASE_SCHEMA_DDL[2:-1]:
        connection.execute(statement)


@lru_cache(maxsize=1)
def text_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_text_schema)


@lru_cache(maxsize=1)
def _text_v1_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_text_v1_schema)


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
            if prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _text_v1_schema_contract(),
                    label="text schema 1 migration source",
                    exact=True,
                )
            elif prior not in {None, 0}:
                raise RuntimeError(f"unsupported text migration start: {prior}")
    with text_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            locked_prior = read_metadata_schema_version(connection, label="text")
            if locked_prior == 1:
                validate_sqlite_schema_contract(
                    connection,
                    _text_v1_schema_contract(),
                    label="text schema 1 migration source",
                    exact=True,
                )
            elif locked_prior not in {None, 0, TEXT_SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported text migration start: {locked_prior}")
            if locked_prior == 1:
                _migrate_text_v1_to_v2(connection)
            else:
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
    from neocortex.semantic.semantic_lexical import compile_natural_fts_query

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
