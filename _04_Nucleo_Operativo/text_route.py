"""Incremental extraction for physical text, email, and legacy Office files."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import xxhash

from _02_Deduplicacion import FileChangedError, FileSnapshot
from _02_Deduplicacion.hashing import snapshot_path, stat_matches_snapshot
from _02_Deduplicacion.path_io import native_io_path
from _03_Progreso import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from .bounded_subprocess import SubprocessOutputLimitError, run_bounded_capture
from .cancellation import CancellationToken
from .file_identity import file_key_from_snapshot
from .processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    build_processing_provenance,
    executable_component,
    python_runtime_component,
)
from .route_filters import CandidateSelection
from .text_state import initialize_text_state, text_database


TEXT_ROUTE_VERSION = "text-route-v1"
TEXT_ROUTE_MIMES = (
    "text/plain",
    "text/csv",
    "text/tab-separated-values",
    "text/markdown",
    "text/html",
    "application/xml",
    "application/json",
    "message/rfc822",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
)


class TextFrameworkState(Protocol):
    def selected_route_candidate_counts(
        self,
        run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        selection: CandidateSelection,
    ) -> tuple[int, int]: ...

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: CandidateSelection,
    ) -> Iterable[FileSnapshot]: ...


@dataclass(frozen=True, slots=True)
class TextRouteConfig:
    state_path: Path
    max_file_bytes: int | None = 64 * 1024 * 1024
    max_documents: int | None = None
    max_text_chars: int = 4_000_000
    worker_timeout_seconds: float = 60.0
    worker_memory_bytes: int = 1024 * 1024 * 1024
    retry_errors: bool = False
    libreoffice_cmd: str | None = None
    selection: CandidateSelection = field(default_factory=CandidateSelection)

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return build_processing_provenance(
            "text-route",
            TEXT_ROUTE_VERSION,
            {
                "max_text_chars": self.max_text_chars,
                "worker_timeout_seconds": self.worker_timeout_seconds,
                "worker_memory_bytes": self.worker_memory_bytes,
                "email_policy": "stdlib-default-visible-text-v1",
                "plain_text_decoder": "strict-bom-utf8-cp1252-v1",
            },
            (
                python_runtime_component(),
                executable_component(
                    "soffice",
                    default_name="soffice",
                    explicit=self.libreoffice_cmd,
                ),
            ),
            compatibility_tag=TEXT_ROUTE_VERSION,
        )

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature


@dataclass(frozen=True, slots=True)
class TextRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    extracted: int = 0
    plain_text: int = 0
    emails: int = 0
    legacy_office: int = 0
    text_chars: int = 0
    truncated: int = 0
    errors: int = 0
    retryable_errors: int = 0
    cache_documents_pruned: int = 0
    catalog_candidates: int = 0
    catalog_classified: int = 0
    catalog_cache_hits: int = 0
    catalog_review_required: int = 0
    catalog_errors: int = 0
    catalog_source_stale: int = 0
    catalog_stale_marked: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    processing_signature: str | None = None
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA


@dataclass(frozen=True, slots=True)
class _ExtractedText:
    text: str
    content_kind: str
    media_type: str
    title: str | None = None
    author: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False
    detail: str | None = None


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _decode_text(payload: bytes) -> tuple[str, str]:
    encodings = (
        ("utf-32",)
        if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        else ("utf-16",)
        if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "cp1252")
    )
    for encoding in encodings:
        try:
            return payload.decode(encoding, "strict"), encoding
        except UnicodeError:
            continue
    raise UnicodeError("text payload cannot be decoded safely")


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized[:limit], len(normalized) > limit


def _visible_html(value: str) -> str:
    parser = _VisibleHTML()
    parser.feed(value)
    parser.close()
    return "\n".join(parser.parts)


def _email_text(payload: bytes, limit: int) -> _ExtractedText:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            raw = part.get_payload(decode=True)
            if not isinstance(raw, bytes):
                continue
            content, _encoding = _decode_text(raw)
        if not isinstance(content, str):
            continue
        parts.append(_visible_html(content) if content_type == "text/html" else content)
    text, truncated = _bounded("\n".join(parts), limit)
    metadata: dict[str, object] = {
        key: str(message.get(key, ""))[:4096]
        for key in ("date", "from", "to", "cc", "message-id")
        if message.get(key)
    }
    return _ExtractedText(
        text=text,
        content_kind="email",
        media_type="message/rfc822",
        title=(str(message.get("subject"))[:1024] if message.get("subject") else None),
        author=(str(message.get("from"))[:1024] if message.get("from") else None),
        metadata=metadata,
        truncated=truncated,
        detail="stdlib_email_visible_text",
    )


def _legacy_office_text(
    payload: bytes,
    kind: str,
    config: TextRouteConfig,
) -> _ExtractedText:
    command = (
        sys.executable,
        "-m",
        "_04_Nucleo_Operativo.legacy_office_worker",
        "--kind",
        kind,
        "--max-input-bytes",
        str(config.max_file_bytes or len(payload)),
        "--max-chars",
        str(config.max_text_chars),
        "--timeout",
        str(config.worker_timeout_seconds),
        *(("--libreoffice-cmd", config.libreoffice_cmd) if config.libreoffice_cmd else ()),
    )
    completed = run_bounded_capture(
        command,
        input_bytes=payload,
        timeout_seconds=config.worker_timeout_seconds,
        stdout_limit_bytes=max(64 * 1024, config.max_text_chars * 6 + 64 * 1024),
        stderr_limit_bytes=256 * 1024,
        environment={
            **os.environ,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        },
        memory_limit_bytes=config.worker_memory_bytes,
    )
    try:
        result = json.loads(completed.stdout.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy Office worker returned invalid output") from exc
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("ok"):
        reason = result.get("reason") if isinstance(result, dict) else None
        raise ValueError(str(reason or f"legacy_office_worker_exit_{completed.returncode}"))
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("legacy Office worker did not return visible text")
    return _ExtractedText(
        text=text,
        content_kind=kind,
        media_type={
            "doc": "application/msword",
            "xls": "application/vnd.ms-excel",
            "ppt": "application/vnd.ms-powerpoint",
        }[kind],
        truncated=bool(result.get("truncated")),
        detail=f"backend={result.get('backend', 'unknown')}",
    )


def _extract(payload: bytes, mime: str, path: str, config: TextRouteConfig) -> _ExtractedText:
    suffix = Path(path).suffix.casefold()
    if mime == "message/rfc822":
        return _email_text(payload, config.max_text_chars)
    legacy_kind = {
        "application/msword": "doc",
        "application/vnd.ms-excel": "xls",
        "application/vnd.ms-powerpoint": "ppt",
    }.get(mime)
    if legacy_kind is not None:
        return _legacy_office_text(payload, legacy_kind, config)
    value, encoding = _decode_text(payload)
    if mime == "text/html":
        value = _visible_html(value)
    elif mime == "application/xml":
        root = ET.fromstring(value)
        value = "\n".join(part.strip() for part in root.itertext() if part.strip())
    text, truncated = _bounded(value, config.max_text_chars)
    kind = {
        "text/csv": "csv",
        "text/tab-separated-values": "tsv",
        "text/markdown": "markdown",
        "text/html": "html",
        "application/xml": "xml",
        "application/json": "json",
    }.get(mime, suffix.removeprefix(".") or "text")
    return _ExtractedText(
        text=text,
        content_kind=kind,
        media_type=mime,
        metadata={"encoding": encoding},
        truncated=truncated,
        detail=f"encoding={encoding}",
    )


def _read_exact(snapshot: FileSnapshot, limit: int, cancellation: CancellationToken) -> bytes:
    path = native_io_path(snapshot.path)
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise FileChangedError("refusing non-regular or linked text source")
    if snapshot.size > limit:
        raise ValueError("text source exceeds configured size limit")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            descriptor = -1
            before = os.fstat(stream.fileno())
            if not stat_matches_snapshot(snapshot, before):
                raise FileChangedError("text source changed before reading")
            chunks: list[bytes] = []
            remaining = snapshot.size
            while remaining:
                cancellation.checkpoint()
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise FileChangedError("unexpected end of text source")
                chunks.append(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise FileChangedError("text source grew while reading")
            if not stat_matches_snapshot(snapshot, os.fstat(stream.fileno())):
                raise FileChangedError("text source changed while reading")
            return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class TextRoute:
    route_name = "text"

    def __init__(
        self,
        config: TextRouteConfig,
        framework_state: TextFrameworkState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.memory_gate = memory_gate
        self.cancellation = cancellation or CancellationToken()

    def _validate(self) -> None:
        if self.config.max_file_bytes is not None and self.config.max_file_bytes < 1:
            raise ValueError("text max_file_bytes must be positive")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("text max_documents must be positive")
        if self.config.max_text_chars < 1:
            raise ValueError("text max_text_chars must be positive")
        if self.config.worker_timeout_seconds <= 0 or self.config.worker_memory_bytes < 1:
            raise ValueError("text worker limits must be positive")

    def _counts(self) -> tuple[int, int, int]:
        pool = eligible = 0
        for mime in TEXT_ROUTE_MIMES:
            mime_pool, mime_eligible = self.framework_state.selected_route_candidate_counts(
                self.run_id,
                mime,
                self.config.max_file_bytes,
                self.route_name,
                self.config.selection,
            )
            pool += mime_pool
            eligible += mime_eligible
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return pool, eligible, selected

    def _candidates(self):
        yielded = 0
        for mime in TEXT_ROUTE_MIMES:
            for snapshot in self.framework_state.iter_selected_route_candidates(
                self.run_id,
                mime,
                self.route_name,
                self.config.selection,
            ):
                if (
                    self.config.max_file_bytes is not None
                    and snapshot.size > self.config.max_file_bytes
                ):
                    continue
                if self.config.max_documents is not None and yielded >= self.config.max_documents:
                    return
                yielded += 1
                yield mime, snapshot

    def _admission(self, snapshot: FileSnapshot):
        if self.memory_gate is None:
            return nullcontext()
        return self.memory_gate.admit(
            max(4 * 1024 * 1024, snapshot.size * 3 + self.config.max_text_chars * 4)
        )

    def _cache_hit(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        signature: str,
    ) -> tuple[bool, bool, int]:
        key = file_key_from_snapshot(snapshot)
        row = connection.execute(
            "SELECT path,size,mtime_ns,birthtime_ns,processing_signature,status,text_chars "
            "FROM documents WHERE file_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return False, False, 0
        matches = (
            int(row["size"]) == snapshot.size
            and int(row["mtime_ns"]) == snapshot.mtime_ns
            and int(row["birthtime_ns"]) == snapshot.birthtime_ns
            and str(row["processing_signature"]) == signature
        )
        status = str(row["status"])
        reusable = matches and (
            status == "complete" or (status == "error" and not self.config.retry_errors)
        )
        if not reusable:
            return False, False, 0
        if str(row["path"]) != snapshot.path:
            conflict = connection.execute(
                "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
                (snapshot.path, key),
            ).fetchone()
            if conflict is not None:
                self._delete_document(connection, str(conflict["file_key"]))
            connection.execute(
                "UPDATE documents SET path=?,last_seen_run_id=?,updated_ns=? WHERE file_key=?",
                (snapshot.path, self.run_id, time.time_ns(), key),
            )
            connection.execute(
                "UPDATE document_fts SET path=? WHERE file_key=?", (snapshot.path, key)
            )
        else:
            connection.execute(
                "UPDATE documents SET last_seen_run_id=?,updated_ns=? WHERE file_key=?",
                (self.run_id, time.time_ns(), key),
            )
        return True, status == "error", int(row["text_chars"])

    @staticmethod
    def _delete_document(connection: sqlite3.Connection, key: str) -> None:
        connection.execute("DELETE FROM document_fts WHERE file_key=?", (key,))
        connection.execute("DELETE FROM documents WHERE file_key=?", (key,))

    def _store_success(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        extracted: _ExtractedText,
        signature: str,
    ) -> None:
        key = file_key_from_snapshot(snapshot)
        conflict = connection.execute(
            "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
            (snapshot.path, key),
        ).fetchone()
        if conflict is not None:
            self._delete_document(connection, str(conflict["file_key"]))
        self._delete_document(connection, key)
        encoded = extracted.text.encode("utf-8")
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,title,author,metadata_json,text_zlib,text_chars,
            text_xxh3_128,text_truncated,detail,error_type,error_message,retryable,
            last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,?,?)""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                extracted.content_kind,
                extracted.media_type,
                extracted.title,
                extracted.author,
                json.dumps(extracted.metadata, ensure_ascii=False, sort_keys=True),
                zlib.compress(encoded, 6),
                len(extracted.text),
                xxhash.xxh3_128_hexdigest(encoded),
                int(extracted.truncated),
                extracted.detail,
                self.run_id,
                time.time_ns(),
            ),
        )
        connection.execute(
            "INSERT INTO document_fts(file_key,path,content_kind,title,author,body) "
            "VALUES(?,?,?,?,?,?)",
            (
                key,
                snapshot.path,
                extracted.content_kind,
                extracted.title or "",
                extracted.author or "",
                extracted.text,
            ),
        )

    def _store_error(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        mime: str,
        signature: str,
        exc: BaseException,
    ) -> bool:
        key = file_key_from_snapshot(snapshot)
        conflict = connection.execute(
            "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
            (snapshot.path, key),
        ).fetchone()
        if conflict is not None:
            self._delete_document(connection, str(conflict["file_key"]))
        self._delete_document(connection, key)
        retryable = isinstance(exc, (FileChangedError, OSError))
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,metadata_json,text_chars,text_truncated,
            error_type,error_message,retryable,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'error',?,?,'{}',0,0,?,?,?,?,?)""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                signature,
                Path(snapshot.path).suffix.casefold().removeprefix(".") or "text",
                mime,
                type(exc).__name__,
                str(exc)[:2_000],
                int(retryable),
                self.run_id,
                time.time_ns(),
            ),
        )
        return retryable

    def _emit(self, completed: int, total: int, summary: dict[str, int], *, finished=False) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "text",
                "extract",
                "Extracción incremental de texto genérico",
                completed,
                total,
                "documentos",
                finished,
                (
                    ProgressMetric("cache_hits", summary["cache_hits"]),
                    ProgressMetric("errors", summary["errors"]),
                ),
            ),
        )

    def run(self) -> TextRouteSummary:
        self._validate()
        initialize_text_state(self.config.state_path)
        provenance = self.config.processing_provenance
        signature = provenance.signature
        pool, eligible, selected = self._counts()
        counters = {
            "processed": 0,
            "cache_hits": 0,
            "cached_errors": 0,
            "extracted": 0,
            "plain_text": 0,
            "emails": 0,
            "legacy_office": 0,
            "text_chars": 0,
            "truncated": 0,
            "errors": 0,
            "retryable_errors": 0,
        }
        self._emit(0, selected, counters)
        with text_database(self.config.state_path, create=False) as connection:
            for mime, snapshot in self._candidates():
                self.cancellation.checkpoint()
                hit, cached_error, chars = self._cache_hit(connection, snapshot, signature)
                if hit:
                    counters["cache_hits"] += 1
                    counters["cached_errors"] += int(cached_error)
                    counters["text_chars"] += chars
                else:
                    try:
                        with self._admission(snapshot):
                            payload = _read_exact(
                                snapshot,
                                self.config.max_file_bytes or snapshot.size,
                                self.cancellation,
                            )
                            extracted = _extract(payload, mime, snapshot.path, self.config)
                            refreshed = snapshot_path(snapshot.path)
                            if refreshed != snapshot:
                                raise FileChangedError("text source changed after extraction")
                        self._store_success(connection, snapshot, extracted, signature)
                        counters["processed"] += 1
                        counters["extracted"] += 1
                        counters["text_chars"] += len(extracted.text)
                        counters["truncated"] += int(extracted.truncated)
                        if extracted.content_kind == "email":
                            counters["emails"] += 1
                        elif extracted.content_kind in {"doc", "xls", "ppt"}:
                            counters["legacy_office"] += 1
                        else:
                            counters["plain_text"] += 1
                    except (
                        FileChangedError,
                        OSError,
                        RuntimeError,
                        SubprocessOutputLimitError,
                        UnicodeError,
                        ValueError,
                        ET.ParseError,
                    ) as exc:
                        counters["processed"] += 1
                        counters["errors"] += 1
                        counters["retryable_errors"] += int(
                            self._store_error(connection, snapshot, mime, signature, exc)
                        )
                connection.commit()
                completed = counters["processed"] + counters["cache_hits"]
                self._emit(completed, selected, counters)
            pruned = 0
            if self.config.max_documents is None and not self.config.selection.active:
                stale = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT file_key FROM documents WHERE last_seen_run_id<>?",
                        (self.run_id,),
                    )
                )
                for key in stale:
                    self._delete_document(connection, key)
                pruned = len(stale)
                connection.commit()
        self._emit(selected, selected, counters, finished=True)
        peak = int(getattr(self.memory_gate, "peak_reserved_bytes", 0))
        waits = int(getattr(self.memory_gate, "wait_count", 0))
        return TextRouteSummary(
            candidate_pool=pool,
            candidates=selected,
            skipped_by_size=max(0, pool - eligible),
            skipped_by_count=max(0, eligible - selected),
            cache_documents_pruned=pruned,
            peak_reserved_bytes=peak,
            memory_waits=waits,
            processing_signature=signature,
            processing_provenance=provenance.manifest,
            processed=counters["processed"],
            cache_hits=counters["cache_hits"],
            cached_errors=counters["cached_errors"],
            extracted=counters["extracted"],
            plain_text=counters["plain_text"],
            emails=counters["emails"],
            legacy_office=counters["legacy_office"],
            text_chars=counters["text_chars"],
            truncated=counters["truncated"],
            errors=counters["errors"],
            retryable_errors=counters["retryable_errors"],
        )


__all__ = (
    "TEXT_ROUTE_MIMES",
    "TEXT_ROUTE_VERSION",
    "TextRoute",
    "TextRouteConfig",
    "TextRouteSummary",
)
