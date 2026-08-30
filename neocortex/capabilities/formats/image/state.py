"""Persistent, resumable state for the integrated image route."""

from __future__ import annotations
import sqlite3
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

import xxhash

from neocortex.deduplication import FileSnapshot

from neocortex.foundation.file_identity import file_key_from_snapshot as file_key
from .policy import DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.sqlite_paths import readonly_sqlite_uri
from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


# region [01] Connection and schema
# Keep the image cache independent while preserving explicit schema evolution.

SCHEMA_VERSION = 5


_IMAGE_TABLE_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS images(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL,
        processing_signature TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        category TEXT,
        confidence REAL,
        confidence_kind TEXT,
        winner_score REAL,
        runner_up TEXT,
        runner_up_score REAL,
        score_margin REAL,
        document_candidate INTEGER NOT NULL DEFAULT 0,
        document_candidate_score REAL,
        document_candidate_uncertainty TEXT,
        adult_candidate INTEGER NOT NULL DEFAULT 0,
        adult_analyzed INTEGER NOT NULL DEFAULT 0,
        adult_classification TEXT,
        adult_confidence REAL,
        adult_provenance TEXT,
        adult_evidence_json TEXT,
        ocr_text_zlib BLOB,
        ocr_text_chars INTEGER,
        ocr_text_xxh3_128 TEXT,
        ocr_text_truncated INTEGER NOT NULL DEFAULT 0,
        decode_quality TEXT,
        decode_provenance TEXT,
        features_json TEXT,
        attributes_json TEXT,
        semantic_json TEXT,
        evidence_json TEXT,
        error_type TEXT,
        error_message TEXT,
        error_phase TEXT,
        error_retryable INTEGER,
        error_disposition TEXT,
        error_provenance TEXT,
        updated_ns INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID""",
)

_IMAGE_INDEX_DDL = (
    """CREATE INDEX IF NOT EXISTS images_run_path_idx
        ON images(last_seen_run_id,path)""",
    """CREATE INDEX IF NOT EXISTS images_category_idx
        ON images(category,confidence DESC) WHERE status='done'""",
    """CREATE INDEX IF NOT EXISTS images_document_review_idx
        ON images(document_candidate,document_candidate_score DESC)
        WHERE status='done' AND document_candidate=1""",
    """CREATE INDEX IF NOT EXISTS images_error_review_idx
        ON images(error_disposition,error_retryable) WHERE status='error'""",
    """CREATE INDEX IF NOT EXISTS images_adult_review_idx
        ON images(adult_classification,adult_confidence DESC)
        WHERE status='done' AND adult_candidate=1""",
    """CREATE INDEX IF NOT EXISTS images_ocr_text_idx
        ON images(updated_ns,file_key)
        WHERE status='done' AND ocr_text_zlib IS NOT NULL""",
)

# The v1 DDL is retained by existing installations.  Later schema versions were
# additive; keeping declarations grouped by their target version documents that
# history while permitting safe repair of any pre-v5 cache during its upgrade.
_IMAGE_COLUMN_MIGRATIONS: tuple[tuple[int, tuple[tuple[str, str], ...]], ...] = (
    (
        2,
        (
            ("semantic_json", "TEXT"),
            ("confidence_kind", "TEXT"),
            ("winner_score", "REAL"),
            ("score_margin", "REAL"),
            ("document_candidate", "INTEGER NOT NULL DEFAULT 0"),
            ("document_candidate_score", "REAL"),
            ("document_candidate_uncertainty", "TEXT"),
            ("error_phase", "TEXT"),
            ("error_retryable", "INTEGER"),
            ("error_disposition", "TEXT"),
            ("error_provenance", "TEXT"),
        ),
    ),
    (
        3,
        (
            ("adult_candidate", "INTEGER NOT NULL DEFAULT 0"),
            ("adult_analyzed", "INTEGER NOT NULL DEFAULT 0"),
            ("adult_classification", "TEXT"),
            ("adult_confidence", "REAL"),
            ("adult_provenance", "TEXT"),
        ),
    ),
    (
        4,
        (
            ("adult_evidence_json", "TEXT"),
            ("decode_quality", "TEXT"),
            ("decode_provenance", "TEXT"),
        ),
    ),
    (
        5,
        (
            ("ocr_text_zlib", "BLOB"),
            ("ocr_text_chars", "INTEGER"),
            ("ocr_text_xxh3_128", "TEXT"),
            ("ocr_text_truncated", "INTEGER NOT NULL DEFAULT 0"),
        ),
    ),
)


def _execute_statements(
    connection: sqlite3.Connection,
    statements: tuple[str, ...],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _create_image_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(connection, _IMAGE_TABLE_DDL)
    _execute_statements(connection, _IMAGE_INDEX_DDL)


@lru_cache(maxsize=1)
def _image_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_image_schema)


def connect_image_state(
    path: Path,
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(
            readonly_sqlite_uri(path),
            uri=True,
            timeout=30.0,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("image state could not enable foreign keys")
        connection.execute("PRAGMA busy_timeout=30000")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("image state could not enforce query-only mode")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def image_database(path: Path, *, readonly: bool = False):
    connection = connect_image_state(path, readonly=readonly)
    try:
        yield connection
        if not readonly:
            connection.commit()
    except BaseException:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


def _add_missing_image_columns(connection: sqlite3.Connection) -> None:
    columns = {str(column[1]) for column in connection.execute("PRAGMA table_info(images)")}
    for _target_version, additions in _IMAGE_COLUMN_MIGRATIONS:
        for column, declaration in additions:
            if column not in columns:
                connection.execute(f'ALTER TABLE images ADD COLUMN "{column}" {declaration}')
                columns.add(column)


def _migrate_image_schema(connection: sqlite3.Connection) -> None:
    _execute_statements(connection, _IMAGE_TABLE_DDL)
    _add_missing_image_columns(connection)
    _execute_statements(connection, _IMAGE_INDEX_DDL)


def _validate_current_image_schema(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        _image_schema_contract(),
        label="image",
        exact=True,
    )


def initialize_image_state(path: Path) -> None:
    """Create or additively migrate image state without rewriting current v5."""

    if path.is_file():
        with image_database(path, readonly=True) as connection:
            prior = read_metadata_schema_version(connection, label="image")
            if prior is not None and prior > SCHEMA_VERSION:
                raise RuntimeError(
                    f"image schema {prior} is newer than supported schema {SCHEMA_VERSION}"
                )
            if prior == SCHEMA_VERSION:
                _validate_current_image_schema(connection)
                return

    with image_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _migrate_image_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            _validate_current_image_schema(connection)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


# endregion [01]


# region [02] Inventory staging and bounded selection


def stage_inventory_batch(
    path: Path,
    run_id: int,
    rows: Iterable[tuple[str, FileSnapshot]],
) -> None:
    with image_database(path) as connection:
        connection.executemany(
            """INSERT INTO images(
                file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,status)
            VALUES(?,?,?,?,?,?,?,'pending')
            ON CONFLICT(file_key) DO UPDATE SET
                path=excluded.path,
                mime=excluded.mime,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                birthtime_ns=excluded.birthtime_ns,
                last_seen_run_id=excluded.last_seen_run_id,
                status=CASE WHEN
                    images.path<>excluded.path OR images.mime<>excluded.mime OR
                    images.size<>excluded.size OR images.mtime_ns<>excluded.mtime_ns OR
                    images.birthtime_ns<>excluded.birthtime_ns
                    THEN 'pending' ELSE images.status END,
                processing_signature=CASE WHEN
                    images.path<>excluded.path OR images.mime<>excluded.mime OR
                    images.size<>excluded.size OR images.mtime_ns<>excluded.mtime_ns OR
                    images.birthtime_ns<>excluded.birthtime_ns
                    THEN NULL ELSE images.processing_signature END""",
            (
                (
                    file_key(snapshot),
                    snapshot.path,
                    mime,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    run_id,
                )
                for mime, snapshot in rows
            ),
        )


def candidate_counts(
    path: Path,
    run_id: int,
    max_file_bytes: int | None,
    selection: CandidateSelection | None = None,
) -> tuple[int, int]:
    clauses, parameters = _selection_clauses(selection)
    clauses.insert(0, "last_seen_run_id=?")
    parameters.insert(0, run_id)
    where = " AND ".join(clauses)
    with image_database(path) as connection:
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM images WHERE {where}",
                parameters,
            ).fetchone()[0]
        )
        if max_file_bytes is None:
            return total, total
        eligible = int(
            connection.execute(
                f"SELECT COUNT(*) FROM images WHERE {where} AND size<=?",
                (*parameters, max_file_bytes),
            ).fetchone()[0]
        )
        return total, eligible


def iter_candidates(
    path: Path,
    run_id: int,
    max_file_bytes: int | None,
    max_documents: int | None,
    *,
    processing_signature: str | None = None,
    retry_errors: bool = False,
    prefer_current_cache: bool = False,
    selection: CandidateSelection | None = None,
) -> Iterator[sqlite3.Row]:
    """Yield a stable candidate snapshot while result rows change underneath it.

    Classification updates ``status`` and ``processing_signature`` in batches.  Those
    fields also determine priority, so paginating the live table can otherwise move an
    already-yielded row into a later priority and yield it twice.  A connection-local
    temporary table keeps only bounded identity/order data on SQLite-managed storage;
    the current image payload is still read incrementally in small batches.
    """

    yielded = 0
    last_priority = -1
    last_path = ""
    last_file_key = ""
    with image_database(path) as connection:
        if processing_signature is None:
            priority_sql = "0"
            priority_parameters: list[object] = []
        elif prefer_current_cache:
            # An unlimited replay has no work-selection tradeoff, so expose
            # durable cache reuse before starting fresh decoder/model work.
            # Bounded runs retain the work-first ordering below so a limit is
            # never consumed only by already-current rows.
            priority_sql = """CASE
                WHEN processing_signature IS ? AND (
                    status='done' OR (status='error' AND ?=0)) THEN 0
                WHEN status='error' AND
                    (processing_signature IS NOT ? OR ?=1) THEN 1
                WHEN status='pending' OR processing_signature IS NULL THEN 2
                WHEN processing_signature IS NOT ? THEN 3
                ELSE 4 END"""
            priority_parameters = [
                processing_signature,
                int(retry_errors),
                processing_signature,
                int(retry_errors),
                processing_signature,
            ]
        else:
            priority_sql = """CASE
                WHEN status='error' AND
                    (processing_signature IS NOT ? OR ?=1) THEN 0
                WHEN status='pending' OR processing_signature IS NULL THEN 1
                WHEN processing_signature IS NOT ? THEN 2
                ELSE 3 END"""
            priority_parameters = [
                processing_signature,
                int(retry_errors),
                processing_signature,
            ]
        snapshot_sql = (
            "INSERT INTO image_route_candidates "
            "SELECT file_key," + priority_sql + ",path FROM images WHERE last_seen_run_id=?"
        )
        parameters = [*priority_parameters, run_id]
        if max_file_bytes is not None:
            snapshot_sql += " AND size<=?"
            parameters.append(max_file_bytes)
        selected_clauses, selected_parameters = _selection_clauses(selection)
        for clause in selected_clauses:
            snapshot_sql += " AND " + clause
        parameters.extend(selected_parameters)
        snapshot_sql += " ORDER BY 2,3,1"
        if max_documents is not None:
            snapshot_sql += " LIMIT ?"
            parameters.append(max_documents)
        connection.execute("DROP TABLE IF EXISTS temp.image_route_candidates")
        connection.execute(
            """CREATE TEMP TABLE image_route_candidates(
                file_key TEXT NOT NULL,
                selection_priority INTEGER NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY(selection_priority,path,file_key)
            ) WITHOUT ROWID"""
        )
        connection.execute(snapshot_sql, parameters)

        while max_documents is None or yielded < max_documents:
            batch_limit = 512
            if max_documents is not None:
                batch_limit = min(batch_limit, max_documents - yielded)
            rows = connection.execute(
                """SELECT images.*,candidate.selection_priority
                FROM image_route_candidates candidate
                JOIN images ON images.file_key=candidate.file_key
                WHERE candidate.selection_priority>? OR
                    (candidate.selection_priority=? AND (
                        candidate.path>? OR
                        (candidate.path=? AND candidate.file_key>?)))
                ORDER BY candidate.selection_priority,candidate.path,
                    candidate.file_key LIMIT ?""",
                (
                    last_priority,
                    last_priority,
                    last_path,
                    last_path,
                    last_file_key,
                    batch_limit,
                ),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
            last_priority = int(rows[-1]["selection_priority"])
            last_path = str(rows[-1]["path"])
            last_file_key = str(rows[-1]["file_key"])


def _selection_clauses(
    selection: CandidateSelection | None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if selection is None:
        return clauses, parameters
    if selection.statuses:
        placeholders = ",".join("?" for _ in selection.statuses)
        clauses.append(f"COALESCE(status,'pending') IN ({placeholders})")
        parameters.extend(selection.statuses)
    if selection.error_types:
        placeholders = ",".join("?" for _ in selection.error_types)
        clauses.append(f"error_type IN ({placeholders})")
        parameters.extend(selection.error_types)
    return clauses, parameters


def snapshot_from_row(row: sqlite3.Row) -> FileSnapshot:
    volume, identity = str(row["file_key"]).split(":", 1)
    return FileSnapshot(
        str(row["path"]),
        int(volume, 16),
        int(identity, 16),
        int(row["size"]),
        int(row["mtime_ns"]),
        int(row["birthtime_ns"]),
    )


# endregion [02]


# region [03] Result persistence and pruning


def store_success_batch(
    path: Path,
    rows: Iterable[tuple],
) -> None:
    with image_database(path) as connection:
        connection.executemany(
            """UPDATE images SET
                processing_signature=?,status='done',category=?,confidence=?,
                confidence_kind=?,winner_score=?,runner_up=?,runner_up_score=?,
                score_margin=?,document_candidate=?,document_candidate_score=?,
                document_candidate_uncertainty=?,adult_candidate=?,adult_analyzed=?,
                adult_classification=?,adult_confidence=?,adult_provenance=?,
                adult_evidence_json=?,ocr_text_zlib=?,ocr_text_chars=?,
                ocr_text_xxh3_128=?,ocr_text_truncated=?,decode_quality=?,decode_provenance=?,
                features_json=?,attributes_json=?,semantic_json=?,evidence_json=?,
                error_type=NULL,error_message=NULL,error_phase=NULL,
                error_retryable=NULL,error_disposition=NULL,error_provenance=NULL,
                updated_ns=?
            WHERE file_key=?""",
            rows,
        )


def store_error_batch(path: Path, rows: Iterable[tuple]) -> None:
    with image_database(path) as connection:
        connection.executemany(
            """UPDATE images SET
                processing_signature=?,status='error',category=NULL,confidence=NULL,
                confidence_kind=NULL,winner_score=NULL,runner_up=NULL,
                runner_up_score=NULL,score_margin=NULL,document_candidate=0,
                document_candidate_score=NULL,document_candidate_uncertainty=NULL,
                adult_candidate=0,adult_analyzed=0,adult_classification=NULL,
                adult_confidence=NULL,adult_provenance=NULL,adult_evidence_json=NULL,
                ocr_text_zlib=NULL,ocr_text_chars=NULL,ocr_text_xxh3_128=NULL,
                ocr_text_truncated=0,
                decode_quality=NULL,decode_provenance=NULL,features_json=NULL,
                attributes_json=NULL,semantic_json=NULL,evidence_json=NULL,
                error_type=?,error_message=?,error_phase=?,error_retryable=?,
                error_disposition=?,error_provenance=?,
                updated_ns=? WHERE file_key=?""",
            rows,
        )


def prune_missing(path: Path, run_id: int, batch_size: int = 1000) -> int:
    removed = 0
    with image_database(path) as connection:
        while True:
            keys = connection.execute(
                "SELECT file_key FROM images WHERE last_seen_run_id<>? LIMIT ?",
                (run_id, batch_size),
            ).fetchall()
            if not keys:
                return removed
            connection.executemany(
                "DELETE FROM images WHERE file_key=?",
                ((str(row[0]),) for row in keys),
            )
            removed += len(keys)
            connection.commit()


def iter_explicit_adult_candidates(
    path: Path,
    run_id: int,
    processing_signature: str,
) -> Iterator[tuple[FileSnapshot, str]]:
    """Yield only current-signature explicit candidates for verified recycling."""

    with image_database(path) as connection:
        rows = connection.execute(
            """SELECT * FROM images
            WHERE last_seen_run_id=? AND status='done' AND processing_signature=?
                AND adult_candidate=1 AND adult_analyzed=1
                AND adult_classification='explicit'
            ORDER BY path,file_key""",
            (run_id, processing_signature),
        )
        while batch := rows.fetchmany(256):
            for row in batch:
                evidence = (
                    f"classification=explicit;confidence="
                    f"{float(row['adult_confidence'] or 0.0):.4f};"
                    f"provenance={row['adult_provenance'] or 'unknown'!s}"
                )
                yield snapshot_from_row(row), evidence


# endregion [03]


# region [04] Bounded OCR text codec and incremental reader


@dataclass(frozen=True, slots=True)
class EncodedOcrText:
    compressed: bytes | None
    characters: int
    xxh3_128: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ImageOcrTextRecord:
    file_key: str
    path: str
    processing_signature: str | None
    text: str
    characters: int
    xxh3_128: str
    truncated: bool
    updated_ns: int


def prepare_ocr_text_storage(
    text: str,
    *,
    truncated: bool,
) -> EncodedOcrText:
    """Compress one bounded UTF-8 value and attach its non-cryptographic identity."""

    encoded = text.encode("utf-8")
    if len(encoded) > DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES:
        raise ValueError(
            "OCR text exceeds the configured UTF-8 persistence limit: "
            f"{len(encoded)} > {DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES}"
        )
    if not encoded and not truncated:
        return EncodedOcrText(None, 0, None, False)
    return EncodedOcrText(
        compressed=zlib.compress(encoded),
        characters=len(text),
        xxh3_128=xxhash.xxh3_128_hexdigest(encoded),
        truncated=truncated,
    )


def _decode_ocr_text(row: sqlite3.Row) -> ImageOcrTextRecord:
    payload = row["ocr_text_zlib"]
    if payload is None:
        raise ValueError("OCR text row has no compressed payload")
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(
        bytes(payload),
        DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES + 1,
    )
    if (
        len(decoded) > DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES
        or decoder.unconsumed_tail
        or not decoder.eof
        or decoder.unused_data
    ):
        raise ValueError("OCR text payload is invalid or exceeds its bounded limit")
    text = decoded.decode("utf-8", "strict")
    characters = int(row["ocr_text_chars"])
    if characters != len(text):
        raise ValueError("OCR text character count does not match its payload")
    fingerprint = str(row["ocr_text_xxh3_128"] or "")
    actual_fingerprint = xxhash.xxh3_128_hexdigest(decoded)
    if fingerprint != actual_fingerprint:
        raise ValueError("OCR text XXH3 fingerprint does not match its payload")
    return ImageOcrTextRecord(
        file_key=str(row["file_key"]),
        path=str(row["path"]),
        processing_signature=(
            None if row["processing_signature"] is None else str(row["processing_signature"])
        ),
        text=text,
        characters=characters,
        xxh3_128=fingerprint,
        truncated=bool(row["ocr_text_truncated"]),
        updated_ns=int(row["updated_ns"]),
    )


def iter_ocr_text_records(
    path: Path,
    *,
    run_id: int | None = None,
    processing_signature: str | None = None,
    batch_size: int = 256,
) -> Iterator[ImageOcrTextRecord]:
    """Yield verified OCR text incrementally without loading the corpus at once."""

    if batch_size <= 0:
        raise ValueError("OCR text batch size must be positive")
    clauses = ["status='done'", "ocr_text_zlib IS NOT NULL"]
    parameters: list[object] = []
    if run_id is not None:
        clauses.append("last_seen_run_id=?")
        parameters.append(run_id)
    if processing_signature is not None:
        clauses.append("processing_signature=?")
        parameters.append(processing_signature)
    query = (
        "SELECT file_key,path,processing_signature,ocr_text_zlib,ocr_text_chars,"
        "ocr_text_xxh3_128,ocr_text_truncated,updated_ns FROM images WHERE "
        + " AND ".join(clauses)
        + " ORDER BY updated_ns,file_key"
    )
    with image_database(path) as connection:
        cursor = connection.execute(query, parameters)
        while batch := cursor.fetchmany(batch_size):
            for row in batch:
                yield _decode_ocr_text(row)


# endregion [04]
