"""Dedicated durable owner for bounded visual-video evidence.

Only compact metadata, hashes, timestamps and OCR text are retained.  Raster
frames remain ephemeral and are never copied into this database or the corpus.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

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

from neocortex.foundation.file_identity import file_key_from_snapshot
from _04_Nucleo_Operativo.sqlite_schema_contract import (
    SQLiteSchemaContract,
    read_metadata_schema_version,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)
from .models import VideoMediaProbe, VideoProcessingError


VIDEO_SCHEMA_VERSION = 2
_PATH_COLLATION = sqlite_path_collation()
MAX_STORED_VIDEO_FRAMES = 256
MAX_STORED_VIDEO_FRAME_PIXELS = 40_000_000
MAX_STORED_VIDEO_OCR_UTF8_BYTES = 16 * 1024
MAX_STORED_VIDEO_WARNINGS = 64
MAX_STORED_VIDEO_ERROR_EVIDENCE_BYTES = 64 * 1024
_FRAME_REASONS = frozenset({"interval", "scene", "keyframe"})
_NATURAL_QUERY_TERM = re.compile(r"[^\W_]+", re.UNICODE)

_VIDEO_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="video state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32_768,
        wal_autocheckpoint_pages=1_024,
        journal_size_limit_bytes=134_217_728,
    ),
)


def _video_schema_ddl(path_collation: str) -> tuple[str, ...]:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported video path collation: {path_collation}")
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
        title TEXT NOT NULL,
        duration_seconds REAL,
        format_name TEXT,
        video_streams INTEGER NOT NULL DEFAULT 0,
        audio_streams INTEGER NOT NULL DEFAULT 0,
        subtitle_streams INTEGER NOT NULL DEFAULT 0,
        chapters INTEGER NOT NULL DEFAULT 0,
        frame_count INTEGER NOT NULL DEFAULT 0,
        ocr_frame_count INTEGER NOT NULL DEFAULT 0,
        ocr_text_chars INTEGER NOT NULL DEFAULT 0,
        probe_json TEXT NOT NULL DEFAULT '{{}}',
        warnings_json TEXT NOT NULL DEFAULT '[]',
        audio_file_key TEXT,
        audio_processing_signature TEXT,
        audio_status TEXT,
        error_type TEXT,
        error_message TEXT,
        retryable INTEGER NOT NULL DEFAULT 0,
        review_disposition TEXT NOT NULL DEFAULT 'none',
        last_seen_run_id INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE UNIQUE INDEX IF NOT EXISTS video_documents_path_idx
        ON documents(path)""",
        """CREATE INDEX IF NOT EXISTS video_documents_status_idx
        ON documents(status,review_disposition,path)""",
        f"""CREATE TABLE IF NOT EXISTS video_inventory(
        file_key TEXT PRIMARY KEY,
        path TEXT NOT NULL COLLATE {path_collation},
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
        """CREATE UNIQUE INDEX IF NOT EXISTS video_inventory_path_idx
        ON video_inventory(path)""",
        """CREATE INDEX IF NOT EXISTS video_inventory_run_idx
        ON video_inventory(last_seen_run_id,file_key)""",
        """CREATE TABLE IF NOT EXISTS frames(
        file_key TEXT NOT NULL,
        frame_index INTEGER NOT NULL,
        timestamp_ms INTEGER NOT NULL,
        sampling_reasons_json TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        content_xxh3_128 TEXT NOT NULL,
        ocr_available INTEGER NOT NULL DEFAULT 0,
        ocr_text TEXT NOT NULL DEFAULT '',
        ocr_mean_confidence REAL,
        ocr_provenance TEXT,
        ocr_error_type TEXT,
        ocr_error_message TEXT,
        PRIMARY KEY(file_key,frame_index),
        FOREIGN KEY(file_key) REFERENCES documents(file_key) ON DELETE CASCADE
    ) WITHOUT ROWID""",
        """CREATE INDEX IF NOT EXISTS video_frames_time_idx
        ON frames(file_key,timestamp_ms,frame_index)""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS frame_fts USING fts5(
        file_key UNINDEXED,
        path UNINDEXED,
        title,
        timestamp_ms UNINDEXED,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    )


_VIDEO_SCHEMA_DDL = _video_schema_ddl(_PATH_COLLATION)
_VIDEO_V1_SCHEMA_DDL = _video_schema_ddl("NOCASE")


@dataclass(frozen=True, slots=True)
class VideoFrameEvidence:
    frame_index: int
    timestamp_ms: int
    sampling_reasons: tuple[str, ...]
    width: int
    height: int
    content_xxh3_128: str
    ocr_available: bool = False
    ocr_text: str = ""
    ocr_mean_confidence: float | None = None
    ocr_provenance: str | None = None
    ocr_error_type: str | None = None
    ocr_error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedAudioLink:
    file_key: str
    processing_signature: str
    status: str
    segment_count: int
    text_chars: int


def _create_video_schema(connection: sqlite3.Connection) -> None:
    for statement in _VIDEO_SCHEMA_DDL:
        connection.execute(statement)


def _create_video_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in _VIDEO_V1_SCHEMA_DDL:
        connection.execute(statement)


@lru_cache(maxsize=1)
def _video_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_video_schema)


@lru_cache(maxsize=1)
def _video_v1_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_create_video_v1_schema)


def _migrate_video_v1(connection: sqlite3.Connection) -> None:
    if _PATH_COLLATION == "NOCASE":
        return
    expected_counts = _video_row_counts(connection)
    _copy_video_v1_tables(connection)
    _drop_video_v1_tables(connection)
    _create_video_schema(connection)
    _restore_video_v1_tables(connection)
    _validate_video_migration(connection, expected_counts)
    _drop_video_v1_copies(connection)


def _video_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("documents", "video_inventory", "frames", "frame_fts")
    }


def _copy_video_v1_tables(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TEMP TABLE video_documents_copy AS SELECT * FROM documents")
    connection.execute("CREATE TEMP TABLE video_inventory_copy AS SELECT * FROM video_inventory")
    connection.execute("CREATE TEMP TABLE video_frames_copy AS SELECT * FROM frames")
    connection.execute(
        "CREATE TEMP TABLE video_fts_copy AS SELECT rowid AS source_rowid,* FROM frame_fts"
    )


def _drop_video_v1_tables(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE frame_fts")
    connection.execute("DROP TABLE frames")
    connection.execute("DROP TABLE video_inventory")
    connection.execute("DROP TABLE documents")


def _quoted_columns(connection: sqlite3.Connection, table: str) -> str:
    columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
    return ",".join(f'"{column}"' for column in columns)


def _restore_video_v1_tables(connection: sqlite3.Connection) -> None:
    quoted_documents = _quoted_columns(connection, "documents")
    quoted_inventory = _quoted_columns(connection, "video_inventory")
    quoted_frames = _quoted_columns(connection, "frames")
    quoted_fts = _quoted_columns(connection, "frame_fts")
    connection.execute(
        f"INSERT INTO documents({quoted_documents}) SELECT {quoted_documents} "
        "FROM video_documents_copy"
    )
    connection.execute(
        f"INSERT INTO video_inventory({quoted_inventory}) SELECT {quoted_inventory} "
        "FROM video_inventory_copy"
    )
    connection.execute(
        f"INSERT INTO frames({quoted_frames}) SELECT {quoted_frames} FROM video_frames_copy"
    )
    connection.execute(
        f"INSERT INTO frame_fts(rowid,{quoted_fts}) "
        f"SELECT source_rowid,{quoted_fts} FROM video_fts_copy"
    )


def _validate_video_migration(
    connection: sqlite3.Connection,
    expected_counts: dict[str, int],
) -> None:
    if _video_row_counts(connection) != expected_counts:
        raise RuntimeError("video schema migration changed persisted row counts")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("video schema migration produced foreign-key violations")


def _drop_video_v1_copies(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE video_documents_copy")
    connection.execute("DROP TABLE video_inventory_copy")
    connection.execute("DROP TABLE video_frames_copy")
    connection.execute("DROP TABLE video_fts_copy")


@contextmanager
def video_database(path: Path, *, readonly: bool = False, create: bool = True):
    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    connection = connect_sqlite(path, mode=mode, policy=_VIDEO_SQLITE_POLICY)
    try:
        yield connection
    finally:
        connection.close()


def initialize_video_state(path: Path) -> None:
    """Create or validate the exact video schema without replacing evidence."""

    if _existing_video_state_is_current(path):
        return
    with video_database(path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _initialize_locked_video_state(connection)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _existing_video_state_is_current(path: Path) -> bool:
    if not path.is_file():
        return False
    with video_database(path, readonly=True) as connection:
        prior = read_metadata_schema_version(connection, label="video")
        if prior is not None and prior > VIDEO_SCHEMA_VERSION:
            raise RuntimeError(
                f"video schema {prior} is newer than supported schema {VIDEO_SCHEMA_VERSION}"
            )
        if prior == VIDEO_SCHEMA_VERSION:
            validate_sqlite_schema_contract(
                connection,
                _video_schema_contract(),
                label="video",
                exact=True,
            )
            return True
        _validate_video_migration_source(connection, prior)
    return False


def _validate_video_migration_source(
    connection: sqlite3.Connection,
    prior: int | None,
) -> None:
    if prior == 1:
        validate_sqlite_schema_contract(
            connection,
            _video_v1_schema_contract(),
            label="video schema 1 migration source",
            exact=True,
        )
    elif prior not in {None, 0, VIDEO_SCHEMA_VERSION}:
        raise RuntimeError(f"unsupported video migration start: {prior}")


def _initialize_locked_video_state(connection: sqlite3.Connection) -> None:
    prior = read_metadata_schema_version(connection, label="video")
    _validate_video_migration_source(connection, prior)
    if prior == 1:
        _migrate_video_v1(connection)
    _create_video_schema(connection)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(VIDEO_SCHEMA_VERSION),),
    )
    validate_sqlite_schema_contract(
        connection,
        _video_schema_contract(),
        label="video",
        exact=True,
    )


def _validate_video_reader(connection: sqlite3.Connection) -> int:
    version = read_metadata_schema_version(connection, label="video")
    if version != VIDEO_SCHEMA_VERSION:
        raise RuntimeError(
            f"video schema {version!r} is not the supported schema {VIDEO_SCHEMA_VERSION}"
        )
    validate_sqlite_schema_contract(
        connection,
        _video_schema_contract(),
        label="video",
        exact=True,
    )
    return version


def validate_video_schema(connection: sqlite3.Connection) -> None:
    """Validate the exact current Video owner contract without mutating state."""

    _validate_video_reader(connection)


def store_video_inventory(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    run_id: int,
) -> None:
    key = file_key_from_snapshot(snapshot)
    connection.execute(
        f"DELETE FROM video_inventory WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, key),
    )
    connection.execute(
        """INSERT INTO video_inventory(
        file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_key) DO UPDATE SET
        path=excluded.path,mime=excluded.mime,size=excluded.size,
        mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
        last_seen_run_id=excluded.last_seen_run_id""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            run_id,
        ),
    )


def cached_video_document(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    processing_signature: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT status,error_type,error_message,retryable,review_disposition,
        frame_count,ocr_frame_count,ocr_text_chars,warnings_json,audio_status,
        audio_streams
        FROM documents WHERE file_key=? AND size=? AND mtime_ns=?
        AND birthtime_ns=? AND processing_signature=?""",
        (
            file_key_from_snapshot(snapshot),
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
        ),
    ).fetchone()


def _remove_path_conflict(connection: sqlite3.Connection, snapshot: FileSnapshot) -> None:
    key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        f"SELECT file_key FROM documents WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
        (snapshot.path, key),
    ).fetchone()
    if conflict is None:
        return
    stale = str(conflict[0])
    connection.execute("DELETE FROM frame_fts WHERE file_key=?", (stale,))
    connection.execute("DELETE FROM documents WHERE file_key=?", (stale,))


def find_published_audio_link(
    audio_state_path: Path | None,
    snapshot: FileSnapshot,
) -> PublishedAudioLink | None:
    """Resolve only a complete immutable audio projection for the same identity."""

    if audio_state_path is None or not audio_state_path.is_file():
        return None
    from ..audio.state import audio_database

    try:
        with audio_database(audio_state_path, readonly=True) as connection:
            row = connection.execute(
                """SELECT file_key,processing_signature,status,segment_count,text_chars
                FROM documents WHERE file_key=? AND size=? AND mtime_ns=?
                AND birthtime_ns=? AND status IN ('complete','no_speech')""",
                (
                    file_key_from_snapshot(snapshot),
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                ),
            ).fetchone()
    except (OSError, sqlite3.Error, RuntimeError):
        return None
    if row is None:
        return None
    return PublishedAudioLink(
        file_key=str(row["file_key"]),
        processing_signature=str(row["processing_signature"]),
        status=str(row["status"]),
        segment_count=int(row["segment_count"]),
        text_chars=int(row["text_chars"]),
    )


def refresh_cached_video(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    run_id: int,
    audio_link: PublishedAudioLink | None,
) -> None:
    _remove_path_conflict(connection, snapshot)
    key = file_key_from_snapshot(snapshot)
    connection.execute(
        """UPDATE documents SET mime=?,path=?,audio_file_key=?,
        audio_processing_signature=?,audio_status=?,last_seen_run_id=?,updated_ns=?
        WHERE file_key=?""",
        (
            mime,
            snapshot.path,
            None if audio_link is None else audio_link.file_key,
            None if audio_link is None else audio_link.processing_signature,
            None if audio_link is None else audio_link.status,
            run_id,
            time.time_ns(),
            key,
        ),
    )
    connection.execute("UPDATE frame_fts SET path=? WHERE file_key=?", (snapshot.path, key))


def _probe_json(probe: VideoMediaProbe) -> str:
    return json.dumps(asdict(probe), ensure_ascii=False, sort_keys=True, allow_nan=False)


def _validate_success_evidence(
    probe: VideoMediaProbe,
    frames: tuple[VideoFrameEvidence, ...],
    warnings: tuple[str, ...],
) -> None:
    _validate_evidence_collection(frames, warnings)
    _validate_frame_sequence(frames)
    duration_limit_ms = math.ceil(probe.duration_seconds * 1000) + 1000
    for frame in frames:
        _validate_frame_evidence(frame, duration_limit_ms)


def _validate_evidence_collection(
    frames: tuple[VideoFrameEvidence, ...],
    warnings: tuple[str, ...],
) -> None:
    if not 1 <= len(frames) <= MAX_STORED_VIDEO_FRAMES:
        raise ValueError(
            f"video evidence must contain between 1 and {MAX_STORED_VIDEO_FRAMES} frames"
        )
    if len(warnings) > MAX_STORED_VIDEO_WARNINGS:
        raise ValueError(f"video warnings cannot exceed {MAX_STORED_VIDEO_WARNINGS}")
    if any(not warning or len(warning.encode("utf-8")) > 128 for warning in warnings):
        raise ValueError("video warning codes must be non-empty and at most 128 UTF-8 bytes")


def _validate_frame_sequence(frames: tuple[VideoFrameEvidence, ...]) -> None:
    indexes = tuple(frame.frame_index for frame in frames)
    if indexes != tuple(range(len(frames))):
        raise ValueError("video frame indexes must be contiguous from zero")
    timestamps = tuple(frame.timestamp_ms for frame in frames)
    if len(set(timestamps)) != len(timestamps) or timestamps != tuple(sorted(timestamps)):
        raise ValueError("video frame timestamps must be unique and sorted")


def _validate_frame_evidence(frame: VideoFrameEvidence, duration_limit_ms: int) -> None:
    _validate_frame_position(frame, duration_limit_ms)
    _validate_frame_dimensions(frame)
    _validate_frame_digest(frame.content_xxh3_128)
    _validate_frame_ocr(frame)


def _validate_frame_position(frame: VideoFrameEvidence, duration_limit_ms: int) -> None:
    if not 0 <= frame.timestamp_ms <= duration_limit_ms:
        raise ValueError("video frame timestamp is outside the probed duration")
    if not frame.sampling_reasons or not set(frame.sampling_reasons) <= _FRAME_REASONS:
        raise ValueError("video frame sampling reasons are invalid")


def _validate_frame_dimensions(frame: VideoFrameEvidence) -> None:
    if frame.width < 1 or frame.height < 1:
        raise ValueError("video frame dimensions must be positive")
    if frame.width * frame.height > MAX_STORED_VIDEO_FRAME_PIXELS:
        raise ValueError("video frame dimensions exceed the stored pixel bound")


def _validate_frame_digest(digest: str) -> None:
    if len(digest) != 32 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("video frame digest must be a lowercase XXH3-128 hex value")


def _validate_frame_ocr(frame: VideoFrameEvidence) -> None:
    if len(frame.ocr_text.encode("utf-8")) > MAX_STORED_VIDEO_OCR_UTF8_BYTES:
        raise ValueError("video frame OCR text exceeds its UTF-8 byte bound")
    if not frame.ocr_available and frame.ocr_text:
        raise ValueError("unavailable video frame OCR cannot publish recognized text")
    confidence = frame.ocr_mean_confidence
    if confidence is not None and not (math.isfinite(confidence) and 0 <= confidence <= 100):
        raise ValueError("video frame OCR confidence must be finite and between 0 and 100")


def store_video_success(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    processing_signature: str,
    probe: VideoMediaProbe,
    frames: tuple[VideoFrameEvidence, ...],
    warnings: tuple[str, ...],
    audio_link: PublishedAudioLink | None,
    run_id: int,
) -> None:
    _validate_success_evidence(probe, frames, warnings)
    _remove_path_conflict(connection, snapshot)
    key = file_key_from_snapshot(snapshot)
    title = Path(snapshot.path).stem
    normalized_warnings = tuple(sorted(set(warnings)))
    status = "partial" if normalized_warnings else "complete"
    ocr_frames = sum(frame.ocr_available and bool(frame.ocr_text) for frame in frames)
    ocr_chars = sum(len(frame.ocr_text) for frame in frames)
    connection.execute(
        """INSERT INTO documents(
        file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,duration_seconds,format_name,video_streams,audio_streams,
        subtitle_streams,chapters,frame_count,ocr_frame_count,ocr_text_chars,
        probe_json,warnings_json,audio_file_key,audio_processing_signature,
        audio_status,error_type,error_message,retryable,review_disposition,
        last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,'none',?,?)
        ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status=excluded.status,
        title=excluded.title,duration_seconds=excluded.duration_seconds,
        format_name=excluded.format_name,video_streams=excluded.video_streams,
        audio_streams=excluded.audio_streams,
        subtitle_streams=excluded.subtitle_streams,chapters=excluded.chapters,
        frame_count=excluded.frame_count,ocr_frame_count=excluded.ocr_frame_count,
        ocr_text_chars=excluded.ocr_text_chars,probe_json=excluded.probe_json,
        warnings_json=excluded.warnings_json,audio_file_key=excluded.audio_file_key,
        audio_processing_signature=excluded.audio_processing_signature,
        audio_status=excluded.audio_status,error_type=NULL,error_message=NULL,
        retryable=0,review_disposition='none',
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            status,
            title,
            probe.duration_seconds,
            probe.format_name,
            probe.video_streams,
            probe.audio_streams,
            len(probe.subtitles),
            probe.chapters,
            len(frames),
            ocr_frames,
            ocr_chars,
            _probe_json(probe),
            json.dumps(normalized_warnings, ensure_ascii=True),
            None if audio_link is None else audio_link.file_key,
            None if audio_link is None else audio_link.processing_signature,
            None if audio_link is None else audio_link.status,
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM frames WHERE file_key=?", (key,))
    connection.execute("DELETE FROM frame_fts WHERE file_key=?", (key,))
    connection.executemany(
        """INSERT INTO frames(
        file_key,frame_index,timestamp_ms,sampling_reasons_json,width,height,
        content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence,
        ocr_provenance,ocr_error_type,ocr_error_message)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            (
                key,
                frame.frame_index,
                frame.timestamp_ms,
                json.dumps(frame.sampling_reasons, ensure_ascii=True),
                frame.width,
                frame.height,
                frame.content_xxh3_128,
                int(frame.ocr_available),
                frame.ocr_text,
                frame.ocr_mean_confidence,
                frame.ocr_provenance,
                frame.ocr_error_type,
                frame.ocr_error_message,
            )
            for frame in frames
        ),
    )
    connection.executemany(
        "INSERT INTO frame_fts(file_key,path,title,timestamp_ms,body) VALUES(?,?,?,?,?)",
        ((key, snapshot.path, title, frame.timestamp_ms, frame.ocr_text) for frame in frames),
    )


def store_video_error(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    mime: str,
    processing_signature: str,
    run_id: int,
    error: VideoProcessingError,
) -> None:
    error_payload = json.dumps(
        {"evidence": error.evidence},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )
    if len(error_payload.encode("utf-8")) > MAX_STORED_VIDEO_ERROR_EVIDENCE_BYTES:
        raise ValueError("video error evidence exceeds its UTF-8 byte bound")
    _remove_path_conflict(connection, snapshot)
    key = file_key_from_snapshot(snapshot)
    connection.execute(
        """INSERT INTO documents(
        file_key,path,mime,size,mtime_ns,birthtime_ns,processing_signature,status,
        title,duration_seconds,format_name,video_streams,audio_streams,
        subtitle_streams,chapters,frame_count,ocr_frame_count,ocr_text_chars,
        probe_json,warnings_json,audio_file_key,audio_processing_signature,
        audio_status,error_type,error_message,retryable,review_disposition,
        last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,'error',?,NULL,NULL,0,0,0,0,0,0,0,?,
        '[]',NULL,NULL,NULL,?,?,?,?,?,?)
        ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
        size=excluded.size,mtime_ns=excluded.mtime_ns,
        birthtime_ns=excluded.birthtime_ns,
        processing_signature=excluded.processing_signature,status='error',
        title=excluded.title,duration_seconds=NULL,format_name=NULL,
        video_streams=0,audio_streams=0,subtitle_streams=0,chapters=0,
        frame_count=0,ocr_frame_count=0,ocr_text_chars=0,
        probe_json=excluded.probe_json,warnings_json='[]',audio_file_key=NULL,
        audio_processing_signature=NULL,audio_status=NULL,
        error_type=excluded.error_type,error_message=excluded.error_message,
        retryable=excluded.retryable,
        review_disposition=excluded.review_disposition,
        last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
        (
            key,
            snapshot.path,
            mime,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            processing_signature,
            Path(snapshot.path).stem,
            error_payload,
            error.code,
            str(error)[:2000],
            int(error.retryable),
            error.recommendation,
            run_id,
            time.time_ns(),
        ),
    )
    connection.execute("DELETE FROM frames WHERE file_key=?", (key,))
    connection.execute("DELETE FROM frame_fts WHERE file_key=?", (key,))


def prune_stale_video_documents(connection: sqlite3.Connection, run_id: int) -> int:
    stale = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT file_key FROM documents WHERE last_seen_run_id<>?", (run_id,)
        )
    )
    for offset in range(0, len(stale), 256):
        batch = stale[offset : offset + 256]
        placeholders = ",".join("?" for _ in batch)
        connection.execute(f"DELETE FROM frame_fts WHERE file_key IN ({placeholders})", batch)
        connection.execute(f"DELETE FROM documents WHERE file_key IN ({placeholders})", batch)
    connection.execute("DELETE FROM video_inventory WHERE last_seen_run_id<>?", (run_id,))
    return len(stale)


def _format_timestamp(timestamp_ms: int) -> str:
    hours, remainder = divmod(max(0, timestamp_ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def search_video_state(
    path: Path,
    query: str,
    limit: int = 20,
    *,
    audio_state_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Search frame OCR and an optional published audio transcript with mm:ss evidence."""

    from _04_Nucleo_Operativo.semantic_lexical import compile_natural_fts_query

    normalized_query = compile_natural_fts_query(query)
    if not 1 <= limit <= 1000:
        raise ValueError("video search limit must be between 1 and 1000")
    with video_database(path, readonly=True) as connection:
        _validate_video_reader(connection)
        rows = connection.execute(
            """SELECT f.file_key,f.path,f.title,CAST(f.timestamp_ms AS INTEGER) AS timestamp_ms,
            snippet(frame_fts,4,'[',']',' ... ',24) AS snippet,
            d.duration_seconds,d.format_name,fr.sampling_reasons_json,
            fr.ocr_mean_confidence,d.size,d.mtime_ns,d.birthtime_ns,
            d.processing_signature,d.status,
            fr.frame_index,fr.content_xxh3_128,fr.ocr_provenance
            FROM frame_fts AS f
            JOIN documents AS d ON d.file_key=f.file_key
            JOIN frames AS fr ON fr.file_key=f.file_key
                AND fr.timestamp_ms=CAST(f.timestamp_ms AS INTEGER)
            WHERE frame_fts MATCH ? AND d.status IN ('complete','partial')
            ORDER BY rank LIMIT ?""",
            (normalized_query, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["channel"] = "frame_ocr"
            item["evidence"] = _format_timestamp(int(item["timestamp_ms"]))
            item["sampling_reasons"] = json.loads(str(item.pop("sampling_reasons_json")))
            results.append(item)
        linked_keys = {
            str(row[0])
            for row in connection.execute(
                """SELECT audio_file_key FROM documents
                WHERE audio_file_key IS NOT NULL AND status IN ('complete','partial')"""
            )
        }
    if len(results) >= limit or not linked_keys or audio_state_path is None:
        return results[:limit]
    results.extend(
        _search_linked_audio(
            audio_state_path,
            query,
            linked_keys,
            limit - len(results),
        )
    )
    return results[:limit]


def _search_linked_audio(
    audio_state_path: Path,
    query: str,
    linked_keys: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not audio_state_path.is_file() or limit <= 0:
        return []
    from ..audio.state import audio_database
    from _04_Nucleo_Operativo.semantic_lexical import compile_natural_fts_query

    normalized_query = compile_natural_fts_query(query)

    try:
        with audio_database(audio_state_path, readonly=True) as connection:
            rows = connection.execute(
                """SELECT f.file_key,f.path,f.title,
                snippet(transcript_fts,3,'[',']',' ... ',24) AS snippet,
                d.duration_seconds,d.language,d.model_name
                FROM transcript_fts AS f JOIN documents AS d ON d.file_key=f.file_key
                WHERE transcript_fts MATCH ? AND d.status='complete'
                ORDER BY rank LIMIT ?""",
                (normalized_query, min(1000, max(limit * 4, limit))),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                key = str(row["file_key"])
                if key not in linked_keys:
                    continue
                segment = _best_audio_segment(connection, key, query)
                timestamp_ms = 0 if segment is None else int(segment["start_ms"])
                item = dict(row)
                item.update(
                    {
                        "channel": "audio_transcript",
                        "timestamp_ms": timestamp_ms,
                        "evidence": _format_timestamp(timestamp_ms),
                        "sampling_reasons": [],
                        "ocr_mean_confidence": None,
                    }
                )
                if segment is not None:
                    item["snippet"] = str(segment["text"])
                results.append(item)
                if len(results) >= limit:
                    break
            return results
    except (OSError, sqlite3.Error, RuntimeError):
        return []


def _best_audio_segment(
    connection: sqlite3.Connection,
    file_key: str,
    query: str,
) -> sqlite3.Row | None:
    tokens = tuple(
        sorted(
            {part.casefold() for part in _NATURAL_QUERY_TERM.findall(query)},
            key=len,
            reverse=True,
        )
    )
    for token in tokens[:8]:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = connection.execute(
            """SELECT start_ms,end_ms,text FROM segments
            WHERE file_key=? AND text LIKE ? ESCAPE '\\'
            ORDER BY segment_index LIMIT 1""",
            (file_key, f"%{escaped}%"),
        ).fetchone()
        if row is not None:
            return row
    return connection.execute(
        """SELECT start_ms,end_ms,text FROM segments
        WHERE file_key=? ORDER BY segment_index LIMIT 1""",
        (file_key,),
    ).fetchone()


def video_state_status(path: Path) -> dict[str, Any]:
    with video_database(path, readonly=True) as connection:
        schema = _validate_video_reader(connection)
        rows = connection.execute(
            """SELECT status,COUNT(*) AS documents,COALESCE(SUM(frame_count),0) AS frames,
            COALESCE(SUM(ocr_frame_count),0) AS ocr_frames,
            COALESCE(SUM(ocr_text_chars),0) AS ocr_chars
            FROM documents GROUP BY status ORDER BY status"""
        ).fetchall()
    return {
        "schema_version": schema,
        "statuses": [dict(row) for row in rows],
    }


__all__ = (
    "VIDEO_SCHEMA_VERSION",
    "PublishedAudioLink",
    "VideoFrameEvidence",
    "cached_video_document",
    "find_published_audio_link",
    "initialize_video_state",
    "prune_stale_video_documents",
    "refresh_cached_video",
    "search_video_state",
    "store_video_error",
    "store_video_inventory",
    "store_video_success",
    "validate_video_schema",
    "video_database",
    "video_state_status",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.video_state"
del _defined_value
