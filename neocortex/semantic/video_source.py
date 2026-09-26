"""Read-only Semantic projection for the dedicated Video owner.

Video is not a text route, but its durable frame OCR is text evidence.  This
adapter exposes that evidence as ordinary ``TextSourceRecord`` values while
retaining the frame/timestamp locator and the owner coverage status.  The
adapter remains independent from the Semantic planner while the common source
contract can select Video explicitly or through the default textual source set.
"""

from __future__ import annotations

from .semantic_source_budget import install_source_progress, source_read_checkpoint, source_snapshot_budget

import hashlib
import json
import sqlite3
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .semantic_models import SemanticItem, TextSection, TextSourceRecord, fingerprint_text
from neocortex.platform.content_capability_manifest import content_capability_for_source
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteImmutableFence,
    SQLiteReadMode,
    SQLiteReadSession,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)


VIDEO_SOURCE_KIND = "video"
VIDEO_SOURCE_DATABASE_NAME = content_capability_for_source(VIDEO_SOURCE_KIND).state_database
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
    source_status: str = "complete"
    truncated: bool = False

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
            "source_status": self.source_status,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class _AudioSegment:
    """Bounded transcript evidence copied out of the linked Audio owner."""

    file_key: str
    processing_signature: str
    status: str
    segment_index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class _AudioDependencySnapshot:
    """Read-only dependency projection for the video source adapter."""

    declared: bool
    available: bool
    documents: Mapping[str, tuple[str, str]]
    segments: Mapping[str, tuple[_AudioSegment, ...]]
    reason: str | None = None


def _assert_owner_fence_unchanged(
    path: Path,
    expected_fence: SQLiteImmutableFence,
    *,
    owner: str,
) -> None:
    """Reject owner drift after a Semantic head projection."""

    try:
        observed_fence = capture_sqlite_read_fence(path)
    except (FileNotFoundError, ImmutableSQLiteUnavailable) as exc:
        raise VideoSourceBlocked(f"{owner} owner changed during head projection") from exc
    if observed_fence != expected_fence:
        raise VideoSourceBlocked(f"{owner} owner changed during head projection")


def _required_owner_fence(path: Path, *, owner: str) -> SQLiteImmutableFence:
    """Capture one required owner fence before opening its read session."""

    try:
        return capture_sqlite_read_fence(path)
    except FileNotFoundError as exc:
        raise VideoSourceBlocked(f"{owner} owner cannot be inspected") from exc
    except ImmutableSQLiteUnavailable as exc:
        raise VideoSourceBlocked(str(exc)) from exc


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
def _readonly_video_database(
    path: Path,
    *,
    expected_fence: SQLiteImmutableFence | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open Video through the sidecar-safe immutable or temporary snapshot."""

    _require_sidecar_safe(path)
    try:
        # Video is a published Semantic source: unlike a bounded general
        # reader, it abstains while a writer-owned WAL is active so a frame
        # projection can never silently omit the latest OCR rows.
        session = SQLiteReadSession(
            path,
            mode=SQLiteReadMode.IMMUTABLE_STRICT,
            timeout_seconds=60.0,
            budget=source_snapshot_budget(),
        )
        with session as connection:
            install_source_progress(connection)
            if expected_fence is not None and session.source_fence != expected_fence:
                raise VideoSourceBlocked("video owner changed before head snapshot")
            yield connection
    except ImmutableSQLiteUnavailable as exc:
        raise VideoSourceBlocked(str(exc)) from exc


def _audio_dependency_declared() -> bool:
    """Read the canonical manifest instead of duplicating route topology."""

    capability = content_capability_for_source(VIDEO_SOURCE_KIND)
    return any(
        dependency.capability_id == "audio"
        for dependency in capability.route_dependencies
    )


@contextmanager
def _readonly_audio_database(
    path: Path,
    *,
    expected_fence: SQLiteImmutableFence | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open the optional Audio owner through the shared sidecar-safe kernel."""

    try:
        mode = preferred_sqlite_read_mode(path)
        session = SQLiteReadSession(path, mode=mode, timeout_seconds=60.0, budget=source_snapshot_budget())
        with session as connection:
            install_source_progress(connection)
            if expected_fence is not None and session.source_fence != expected_fence:
                raise VideoSourceBlocked("audio owner changed before dependency snapshot")
            yield connection
    except ImmutableSQLiteUnavailable as exc:
        raise VideoSourceBlocked(f"audio dependency is unavailable: {exc}") from exc


def _audio_dependency_snapshot(
    state_directory: Path,
    linked_keys: Sequence[str],
) -> _AudioDependencySnapshot:
    """Capture linked Audio rows without touching the live owner sidecars.

    The dependency is optional for visual-only videos.  When a Video row says
    that an audio stream exists but its complete Audio projection is missing,
    we retain the visual evidence and mark its coverage partial rather than
    silently presenting it as complete.
    """

    declared = _audio_dependency_declared()
    selected_keys = tuple(dict.fromkeys(key for key in linked_keys if key))
    if not declared or not selected_keys:
        return _AudioDependencySnapshot(declared, True, {}, {})
    audio_capability = content_capability_for_source("audio")
    audio_path = state_directory / audio_capability.state_database
    if not audio_path.is_file():
        return _AudioDependencySnapshot(
            declared,
            False,
            {},
            {},
            "audio_state_missing",
        )
    documents: dict[str, tuple[str, str]] = {}
    segments: dict[str, list[_AudioSegment]] = {}
    try:
        audio_fence = _required_owner_fence(audio_path, owner="audio")
        with _readonly_audio_database(audio_path, expected_fence=audio_fence) as connection:
            # Keep each IN list below SQLite's portable variable limit.
            for offset in range(0, len(selected_keys), 500):
                keys = selected_keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in keys)
                rows = connection.execute(
                    f"""SELECT file_key,processing_signature,status
                    FROM documents WHERE file_key IN ({placeholders})""",
                    keys,
                ).fetchall()
                for row in rows:
                    documents[str(row["file_key"])] = (
                        str(row["processing_signature"]),
                        str(row["status"]),
                    )
                rows = connection.execute(
                    f"""SELECT file_key,segment_index,start_ms,end_ms,text
                    FROM segments WHERE file_key IN ({placeholders}) AND trim(text)<>''
                    ORDER BY file_key,segment_index""",
                    keys,
                ).fetchall()
                for row in rows:
                    key = str(row["file_key"])
                    processing_signature, status = documents.get(key, ("", "unknown"))
                    segments.setdefault(key, []).append(
                        _AudioSegment(
                            key,
                            processing_signature,
                            status,
                            int(row["segment_index"]),
                            int(row["start_ms"]),
                            int(row["end_ms"]),
                            str(row["text"]),
                        )
                    )
        _assert_owner_fence_unchanged(audio_path, audio_fence, owner="audio")
    except (OSError, sqlite3.Error, RuntimeError, VideoSourceBlocked) as exc:
        return _AudioDependencySnapshot(
            declared,
            False,
            {},
            {},
            type(exc).__name__,
        )
    return _AudioDependencySnapshot(
        declared,
        True,
        documents,
        {key: tuple(values) for key, values in segments.items()},
    )


def _video_row_coverage(
    row: sqlite3.Row,
    audio: _AudioDependencySnapshot,
) -> tuple[VideoCoverage, str | None]:
    """Return coverage and a stable reason for one Video document."""

    source_status = str(row["status"])
    if source_status == "not_applicable":
        # Audio-only containers are valid route inputs but intentionally do not
        # publish visual Video evidence.  Keep the owner head complete while
        # carrying an explicit exclusion reason; Semantic generation accepts
        # this route-level no-op and never sees a duplicate transcript.
        return "complete", "video_source_status_not_applicable"
    if source_status == "partial":
        return "partial", "video_source_status_partial"
    if source_status == "error":
        return "partial", "video_source_status_error"
    if source_status != "complete":
        # The Video schema keeps status extensible at the SQL boundary.  An
        # unknown/future/corrupt value is never allowed to fall through to a
        # complete head simply because it is not named ``partial``/``error``.
        return "partial", "video_source_status_unknown"
    if int(row["audio_streams"] or 0) <= 0:
        return "complete", None
    audio_key = row["audio_file_key"]
    if audio_key is None or not str(audio_key):
        return "partial", "audio_link_missing"
    declared_audio_status = row["audio_status"]
    if declared_audio_status is None or str(declared_audio_status) not in {
        "complete",
        "no_speech",
    }:
        return "partial", "audio_source_status_partial"
    if not audio.available:
        return "partial", audio.reason or "audio_dependency_unavailable"
    linked = audio.documents.get(str(audio_key))
    if linked is None:
        return "partial", "audio_projection_missing"
    _processing_signature, status = linked
    if status not in {"complete", "no_speech"}:
        return "partial", "audio_source_status_partial"
    return "complete", None


def _item_from_row(
    row: sqlite3.Row,
    *,
    coverage: VideoCoverage,
    coverage_reason: str | None,
    audio_dependency: _AudioDependencySnapshot,
) -> SemanticItem:
    source_status = str(row["status"])
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
            str(row["audio_file_key"] or ""),
            str(row["audio_processing_signature"] or ""),
            str(row["audio_status"] or ""),
            coverage,
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
            "audio_dependency_declared": audio_dependency.declared,
            "audio_dependency_available": audio_dependency.available,
            "coverage": coverage,
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
            "audio_dependency_declared": audio_dependency.declared,
            "coverage_reason": coverage_reason,
        },
    )


def _frame_section(
    row: sqlite3.Row,
    *,
    source_status: str,
    coverage: VideoCoverage,
) -> TextSection:
    timestamp_ms = int(row["timestamp_ms"])
    frame_index = int(row["frame_index"])
    return TextSection(
        section_kind="video_frame_ocr",
        section_id=str(frame_index),
        text=str(row["body"]),
        provenance={
            "adapter": VIDEO_SOURCE_ADAPTER_VERSION,
            "source_status": source_status,
            "coverage": coverage,
            "locator": {
                "kind": "video_frame",
                "frame_index": frame_index,
                "timestamp_ms": timestamp_ms,
            },
            "ocr_truncated": bool(row["ocr_text_truncated"]),
        },
    )


def _audio_section(
    segment: _AudioSegment,
    *,
    coverage: VideoCoverage,
) -> TextSection:
    """Project a linked Audio transcript with a time-based locator."""

    return TextSection(
        section_kind="video_audio_transcript",
        section_id=str(segment.segment_index),
        text=segment.text,
        provenance={
            "adapter": VIDEO_SOURCE_ADAPTER_VERSION,
            "dependency": "audio",
            "source_status": segment.status,
            "coverage": coverage,
            "processing_signature": segment.processing_signature,
            "locator": {
                "kind": "audio_segment",
                "segment_index": segment.segment_index,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
            },
        },
    )


def _video_rows(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    rows = connection.execute(
        """SELECT d.file_key,d.path,d.mime,d.size,d.mtime_ns,d.birthtime_ns,
        d.processing_signature,d.status,d.title,d.duration_seconds,d.frame_count,
        d.ocr_frame_count,d.ocr_text_chars,d.audio_streams,d.audio_file_key,
        d.audio_processing_signature,d.audio_status,
        fr.frame_index,fr.ocr_text AS frame_ocr_text,
        f.timestamp_ms,f.body,0 AS ocr_text_truncated
        FROM documents d JOIN frame_fts f ON f.file_key=d.file_key
        JOIN frames fr ON fr.file_key=f.file_key AND fr.timestamp_ms=f.timestamp_ms
        WHERE d.status IN ('complete','partial') AND trim(f.body)<>''
        ORDER BY d.file_key,f.timestamp_ms,fr.frame_index"""
    )
    for row in rows:
        # ``frame_fts`` is a durable index copy of ``frames.ocr_text``.  Do
        # not silently feed stale text to Semantic if one owner was updated
        # without the other; a blocked projection is safer than a false
        # fresh result.
        if str(row["body"]) != str(row["frame_ocr_text"]):
            raise VideoSourceBlocked("video frame OCR projection is inconsistent")
        yield row


def iter_video_source_records(
    state_directory: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> Iterator[TextSourceRecord]:
    """Yield Video OCR and any declared, complete linked Audio evidence."""

    path = state_directory / content_capability_for_source(VIDEO_SOURCE_KIND).state_database
    if not path.is_file():
        return
    borrowed = connection is not None
    context = _borrowed_connection(connection) if borrowed else _readonly_video_database(path)
    with context as owner:
        linked_keys = tuple(
            dict.fromkeys(
                str(row[0])
                for row in owner.execute(
                    """SELECT audio_file_key FROM documents
                    WHERE audio_file_key IS NOT NULL
                    AND status IN ('complete','partial')
                    ORDER BY audio_file_key"""
                ).fetchall()
            )
        )
        audio_dependency = _audio_dependency_snapshot(state_directory, linked_keys)
        current_file_key: str | None = None
        current_item: SemanticItem | None = None
        for row in _video_rows(owner):
            file_key = str(row["file_key"])
            if file_key != current_file_key:
                coverage, coverage_reason = _video_row_coverage(row, audio_dependency)
                current_item = _item_from_row(
                    row,
                    coverage=coverage,
                    coverage_reason=coverage_reason,
                    audio_dependency=audio_dependency,
                )
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
                                "coverage": coverage,
                                "coverage_reason": coverage_reason,
                                "locator": {"kind": "video_title"},
                            },
                        ),
                    )
                if int(row["audio_streams"] or 0) > 0 and row["audio_file_key"]:
                    # The linked Audio owner normally shares the physical
                    # file identity, but that is not a contract: a route may
                    # publish a distinct dependency key.  Resolve the
                    # dependency by the durable link, never by the Video
                    # document key, and do not project a stale link when the
                    # owner says the stream is absent.
                    audio_key = str(row["audio_file_key"])
                    for segment in audio_dependency.segments.get(audio_key, ()):
                        yield TextSourceRecord(
                            current_item,
                            _audio_section(segment, coverage=coverage),
                        )
            assert current_item is not None
            yield TextSourceRecord(
                current_item,
                _frame_section(
                    row,
                    source_status=str(row["status"]),
                    coverage=coverage,
                ),
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
    coverage_reasons: set[str] = set()
    excluded_not_applicable = False
    coverage: VideoCoverage
    reason: str | None
    try:
        before_fence = _required_owner_fence(path, owner="video")
        with _readonly_video_database(path, expected_fence=before_fence) as connection:
            rows = connection.execute(
                """SELECT file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,title,duration_seconds,frame_count,ocr_frame_count,ocr_text_chars,
                audio_streams,audio_file_key,audio_processing_signature,audio_status
                FROM documents ORDER BY file_key"""
            ).fetchall()
            linked_keys = tuple(
                dict.fromkeys(
                    str(row["audio_file_key"])
                    for row in rows
                    if row["audio_file_key"] is not None
                )
            )
            audio_dependency = _audio_dependency_snapshot(state_directory, linked_keys)
            for row in rows:
                status = str(row["status"])
                statuses.add(status)
                row_coverage, row_reason = _video_row_coverage(row, audio_dependency)
                # A route-owned ``partial`` status is expected source
                # metadata, not a read failure; preserve the historical
                # ``reason=None`` contract while the explicit coverage field
                # carries the publication guard.
                if row_reason == "video_source_status_not_applicable":
                    excluded_not_applicable = True
                elif row_reason is not None and row_reason != "video_source_status_partial":
                    coverage_reasons.add(row_reason)
                source_read_checkpoint()
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
                source_read_checkpoint()
                digest.update(
                    json.dumps(
                        {
                            "file_key": str(row["file_key"]),
                            "coverage": row_coverage,
                            "reason": row_reason,
                            "audio": [
                                {
                                    "processing_signature": segment.processing_signature,
                                    "status": segment.status,
                                    "segment_index": segment.segment_index,
                                    "start_ms": segment.start_ms,
                                    "end_ms": segment.end_ms,
                                    "text": segment.text,
                                }
                                for segment in audio_dependency.segments.get(
                                    str(row["audio_file_key"]), ()
                                )
                            ],
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                row_count += 1
            # The document row carries aggregate OCR counts, but not the
            # actual indexed frame text or frame locator.  Include the exact
            # bounded projection consumed by ``iter_video_source_records`` in
            # the head digest so a same-size OCR rewrite, locator change, or
            # FTS/frame divergence cannot be mistaken for an exact replay.
            for frame_row in _video_rows(connection):
                source_read_checkpoint()
                digest.update(
                    json.dumps(
                        {key: frame_row[key] for key in frame_row.keys()},
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
        _assert_owner_fence_unchanged(path, before_fence, owner="video")
    except (OSError, sqlite3.Error, TypeError, ValueError, VideoSourceBlocked) as exc:
        coverage = "blocked"
        reason = type(exc).__name__
        source_status = "blocked"
    else:
        coverage = "partial" if coverage_reasons or statuses.intersection(
            {"partial", "error"}
        ) else "complete"
        reason = sorted(coverage_reasons)[0] if coverage_reasons else (
            "video_source_status_not_applicable" if excluded_not_applicable else None
        )
        source_status = "complete" if coverage == "complete" else "partial"
    return VideoSourceHead(
        source_kind=VIDEO_SOURCE_KIND,
        database_name=path.name,
        adapter_version=VIDEO_SOURCE_ADAPTER_VERSION,
        row_count=row_count,
        digest="sha256:" + digest.hexdigest(),
        coverage=coverage,
        reason=reason,
        source_status=source_status,
        truncated=False,
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
