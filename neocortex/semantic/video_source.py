"""Read-only Semantic projection for the dedicated Video owner.

Video is not a text route, but its durable frame OCR is text evidence.  This
adapter exposes that evidence as ordinary ``TextSourceRecord`` values while
retaining the frame/timestamp locator and the owner coverage status.  The
adapter is intentionally independent from the Semantic planner so it can be
introduced and tested before a planner rollout selects video by default.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .semantic_models import SemanticItem, TextSection, fingerprint_text
from .semantic_sources import TextSourceRecord
from neocortex.platform.content_capability_manifest import content_capability_for_source
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    immutable_sqlite_database,
)


VIDEO_SOURCE_KIND = "video"
VIDEO_SOURCE_DATABASE_NAME = "video.sqlite3"
VIDEO_SOURCE_ADAPTER_VERSION = "semantic-video-source-v1"
VIDEO_SOURCE_HEAD_SCHEMA = "neocortex.semantic-video-source-head/v1"

VideoCoverage = Literal["complete", "partial", "blocked"]


class VideoSourceBlocked(RuntimeError):
    """The Video owner cannot be read without risking a stale projection."""


@dataclass(frozen=True, slots=True)
class VideoSourceHead:
    """Stable owner projection used to bind Semantic output to Video state."""

    source_kind: str
    database_name: str
    adapter_version: str
    row_count: int
    digest: str
    coverage: VideoCoverage
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.coverage == "complete"

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": VIDEO_SOURCE_HEAD_SCHEMA,
            "source_kind": self.source_kind,
            "database_name": self.database_name,
            "adapter_version": self.adapter_version,
            "row_count": self.row_count,
            "digest": self.digest,
            "coverage": self.coverage,
            "complete": self.complete,
            "reason": self.reason,
        }


def _owner_stamp(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    values: list[tuple[str, int, int, int]] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            stat = candidate.lstat()
        except FileNotFoundError:
            continue
        values.append((suffix, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(values)


def _require_sidecar_safe(path: Path) -> None:
    """Reject symlinked or non-regular owners before opening a read session."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise VideoSourceBlocked("video owner cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise VideoSourceBlocked("video owner is not a regular file")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar_metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(sidecar_metadata.st_mode) or not stat.S_ISREG(sidecar_metadata.st_mode):
            raise VideoSourceBlocked(f"video owner sidecar is not regular: {suffix}")
        if sidecar_metadata.st_size > 0:
            raise VideoSourceBlocked(f"video owner has active {suffix.lstrip('-')}")


@contextmanager
def _readonly_video_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open Video through the sidecar-safe immutable or temporary snapshot."""

    _require_sidecar_safe(path)
    try:
        # Video is a published Semantic source: unlike a bounded general
        # reader, it abstains while a writer-owned WAL is active so a frame
        # projection can never silently omit the latest OCR rows.
        with immutable_sqlite_database(path, timeout_seconds=60.0) as connection:
            yield connection
    except ImmutableSQLiteUnavailable as exc:
        raise VideoSourceBlocked(str(exc)) from exc


def _item_from_row(row: sqlite3.Row) -> SemanticItem:
    source_status = str(row["status"])
    coverage = "complete" if source_status == "complete" else "partial"
    processing_signature = str(row["processing_signature"])
    source_identity = str(row["file_key"])
    descriptor = "\0".join(
        (
            VIDEO_SOURCE_ADAPTER_VERSION,
            source_identity,
            processing_signature,
            str(row["size"]),
            str(row["mtime_ns"]),
            str(row["birthtime_ns"]),
            str(row["frame_count"]),
            str(row["ocr_text_chars"]),
        )
    )
    return SemanticItem(
        item_id=f"item:{VIDEO_SOURCE_KIND}:{source_identity}",
        source_kind=VIDEO_SOURCE_KIND,
        source_identity=source_identity,
        identity_version=f"{VIDEO_SOURCE_ADAPTER_VERSION}|{processing_signature}",
        fingerprint=fingerprint_text(descriptor),
        path=str(row["path"]),
        source_revision={
            "size": int(row["size"]),
            "mtime_ns": int(row["mtime_ns"]),
            "birthtime_ns": int(row["birthtime_ns"]),
            "processing_signature": processing_signature,
            "frame_count": int(row["frame_count"]),
            "ocr_frame_count": int(row["ocr_frame_count"]),
            "ocr_text_chars": int(row["ocr_text_chars"]),
            "audio_file_key": row["audio_file_key"],
            "audio_status": row["audio_status"],
        },
        provenance={
            "adapter": VIDEO_SOURCE_ADAPTER_VERSION,
            "source_status": source_status,
            "coverage": coverage,
            "mime": str(row["mime"]),
            "title": str(row["title"]),
            "duration_seconds": row["duration_seconds"],
            "frame_count": int(row["frame_count"]),
            "ocr_frame_count": int(row["ocr_frame_count"]),
            "audio_status": row["audio_status"],
        },
    )


def _frame_section(row: sqlite3.Row, *, source_status: str) -> TextSection:
    timestamp_ms = int(row["timestamp_ms"])
    frame_index = int(row["frame_index"])
    return TextSection(
        section_kind="video_frame_ocr",
        section_id=str(frame_index),
        text=str(row["body"]),
        provenance={
            "adapter": VIDEO_SOURCE_ADAPTER_VERSION,
            "source_status": source_status,
            "coverage": "complete" if source_status == "complete" else "partial",
            "locator": {
                "kind": "video_frame",
                "frame_index": frame_index,
                "timestamp_ms": timestamp_ms,
            },
            "ocr_truncated": bool(row["ocr_text_truncated"]),
        },
    )


def _video_rows(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    rows = connection.execute(
        """SELECT d.file_key,d.path,d.mime,d.size,d.mtime_ns,d.birthtime_ns,
        d.processing_signature,d.status,d.title,d.duration_seconds,d.frame_count,
        d.ocr_frame_count,d.ocr_text_chars,d.audio_file_key,d.audio_status,
        fr.frame_index,f.timestamp_ms,f.body,0 AS ocr_text_truncated
        FROM documents d JOIN frame_fts f ON f.file_key=d.file_key
        JOIN frames fr ON fr.file_key=f.file_key AND fr.timestamp_ms=f.timestamp_ms
        WHERE d.status IN ('complete','partial') AND trim(f.body)<>''
        ORDER BY d.file_key,f.timestamp_ms,fr.frame_index"""
    )
    yield from rows


def iter_video_source_records(
    state_directory: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Yield frame OCR as Semantic text evidence with explicit locators."""

    path = state_directory / content_capability_for_source(VIDEO_SOURCE_KIND).state_database
    if not path.is_file():
        return
    borrowed = connection is not None
    context = _borrowed_connection(connection) if borrowed else _readonly_video_database(path)
    with context as owner:
        current_file_key: str | None = None
        current_item: SemanticItem | None = None
        for row in _video_rows(owner):
            file_key = str(row["file_key"])
            if file_key != current_file_key:
                current_item = _item_from_row(row)
                current_file_key = file_key
                title = str(row["title"] or "").strip()
                if title:
                    yield TextSourceRecord(
                        current_item,
                        TextSection(
                            section_kind="video_metadata_title",
                            section_id="title",
                            text=title,
                            provenance={
                                "adapter": VIDEO_SOURCE_ADAPTER_VERSION,
                                "source_status": str(row["status"]),
                                "coverage": (
                                    "complete"
                                    if str(row["status"]) == "complete"
                                    else "partial"
                                ),
                                "locator": {"kind": "video_title"},
                            },
                        ),
                    )
            assert current_item is not None
            yield TextSourceRecord(
                current_item,
                _frame_section(row, source_status=str(row["status"])),
            )


@contextmanager
def _borrowed_connection(connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if connection is None:
        raise AssertionError("borrowed video connection cannot be absent")
    yield connection


def video_source_head(state_directory: Path) -> VideoSourceHead:
    """Compute a bounded deterministic head without reading original videos."""

    path = state_directory / content_capability_for_source(VIDEO_SOURCE_KIND).state_database
    digest = hashlib.sha256()
    row_count = 0
    statuses: set[str] = set()
    coverage: VideoCoverage
    reason: str | None
    try:
        with _readonly_video_database(path) as connection:
            before = _owner_stamp(path)
            rows = connection.execute(
                """SELECT file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,title,duration_seconds,frame_count,ocr_frame_count,ocr_text_chars,
                audio_file_key,audio_status FROM documents ORDER BY file_key"""
            )
            for row in rows:
                status = str(row["status"])
                statuses.add(status)
                digest.update(
                    json.dumps(
                        {key: row[key] for key in row.keys()},
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                row_count += 1
            after = _owner_stamp(path)
        if before != after:
            raise VideoSourceBlocked("video owner changed during head projection")
    except (OSError, sqlite3.Error, TypeError, ValueError, VideoSourceBlocked) as exc:
        coverage = "blocked"
        reason = type(exc).__name__
    else:
        coverage = "partial" if "partial" in statuses or "error" in statuses else "complete"
        reason = None
    return VideoSourceHead(
        source_kind=VIDEO_SOURCE_KIND,
        database_name=path.name,
        adapter_version=VIDEO_SOURCE_ADAPTER_VERSION,
        row_count=row_count,
        digest="sha256:" + digest.hexdigest(),
        coverage=coverage,
        reason=reason,
    )


__all__ = (
    "VIDEO_SOURCE_ADAPTER_VERSION",
    "VIDEO_SOURCE_DATABASE_NAME",
    "VIDEO_SOURCE_HEAD_SCHEMA",
    "VIDEO_SOURCE_KIND",
    "VideoSourceBlocked",
    "VideoSourceHead",
    "iter_video_source_records",
    "video_source_head",
)
