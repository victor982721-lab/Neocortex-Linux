"""Incremental, bounded indexing of ZIP members, including nested ZIP files."""

from __future__ import annotations

from neocortex.runtime.control.locking import FrameworkRunLock

from ..fts_lookup import (
    delete_format_fts_keys,
    format_fts_key_predicate,
    initialize_format_fts_lookup,
    insert_format_fts_row,
    insert_format_fts_rows,
)

import io
import inspect
import json
import os
import pickle
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import deque
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from neocortex.foundation.hash_compat import xxhash

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import snapshot_path, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from neocortex.workflow.actions.action_policy import same_snapshot
from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_bytes,
    inspect_zip_stream,
)
from .materialization import (
    ArchiveMaterializationLimits,
    ArchiveManifest,
    materialize_archive,
)
from neocortex.foundation.processing_provenance import (
    ProcessingProvenance,
    build_processing_provenance,
    distribution_component,
    resolve_tesseract_runtime,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from .models import ArchiveRouteSummary
from .logical import (
    LOGICAL_MEDIA_TYPES,
    MAX_DECLARED_MIME_BYTES,
    ODF_KINDS,
    LogicalDocumentEvidence,
    identify_logical_document,
    issue_diagnosis,
)
from .state import archive_database, initialize_archive_state


# region [01] Public route contract and safety defaults


ARCHIVE_MIME = "application/zip"
ARCHIVE_ROUTE_VERSION = "archive-route-v3"
DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_MEMBERS = 20_000
DEFAULT_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 2_000_000
DEFAULT_MAX_TOTAL_TEXT_CHARS = 20_000_000
DEFAULT_MAX_COMPRESSION_RATIO = 200.0
DEFAULT_PDF_MAX_PAGES = 500
DEFAULT_PDF_TIMEOUT_SECONDS = 60.0
DEFAULT_PDF_WORKER_MEMORY_BYTES = 768 * 1024 * 1024
DEFAULT_OCR_MAX_PAGES = 50
DEFAULT_OCR_DPI = 200
DEFAULT_OCR_MAX_RENDER_PIXELS = 40_000_000
DEFAULT_OCR_TIMEOUT_SECONDS = 30.0
MAX_MEMBER_NAME_CHARS = 2_048
MAX_MEMBER_SEGMENT_CHARS = 255
MAX_EMBEDDED_DOCUMENT_MEMBERS = 20_000
MAX_EMBEDDED_XML_BYTES = 64 * 1024 * 1024
ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
MAX_ARCHIVE_ADMISSION_PREFIX_BYTES = 8 * 1024
ARCHIVE_MEMBER_ADMISSION_DISABLED_SIGNATURE = "archive-member-admission:none-v1"
_ARCHIVE_RESOURCES: ContextVar[tuple[Any, CancellationToken] | None] = ContextVar(
    "archive_resources", default=None
)


@contextmanager
def _archive_resource_scope(gate, cancellation: CancellationToken):
    token = _ARCHIVE_RESOURCES.set((gate, cancellation))
    try:
        yield
    finally:
        _ARCHIVE_RESOURCES.reset(token)


def _coordinated_archive_gate():
    context = _ARCHIVE_RESOURCES.get()
    if context is None or not callable(getattr(context[0], "worker_capacity", None)):
        return None
    return context[0]


def _archive_cancellation() -> CancellationToken | None:
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    worker = current_worker_cancellation()
    context = _ARCHIVE_RESOURCES.get()
    return worker if worker is not None else (None if context is None else context[1])


@dataclass(frozen=True, slots=True)
class ArchiveMemberAdmissionContext:
    """Verified, bounded metadata exposed to an optional member policy.

    ``prefix`` is the only member content exposed to the callback.  It is
    bounded and is supplied only after the ZIP reader has consumed the member
    to EOF, so the reader has performed its normal CRC/size validation.  The
    complete payload is never handed to a policy and is released immediately
    when the policy denies the member.
    """

    # ``member_name`` is the normalized path within the current ZIP; the
    # chain retains outer nested ZIP notation for policy/localizer decisions.
    member_name: str
    member_chain: str
    depth: int
    container_path: Path
    size: int
    compressed_size: int
    crc32: int
    prefix: bytes


ArchiveMemberAdmission = Callable[[ArchiveMemberAdmissionContext], object]


@dataclass(frozen=True, slots=True)
class ArchiveRouteConfig:
    state_path: Path
    max_file_bytes: int | None = None
    max_documents: int | None = None
    retry_errors: bool = False
    retry_recoverable_errors: bool = field(default=False, kw_only=True)
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    max_depth: int = DEFAULT_MAX_DEPTH
    max_members: int = DEFAULT_MAX_MEMBERS
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES
    max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_total_text_chars: int = DEFAULT_MAX_TOTAL_TEXT_CHARS
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO
    pdf_max_pages: int = DEFAULT_PDF_MAX_PAGES
    pdf_timeout_seconds: float = DEFAULT_PDF_TIMEOUT_SECONDS
    pdf_worker_memory_bytes: int = DEFAULT_PDF_WORKER_MEMORY_BYTES
    ocr_mode: Literal["auto", "never", "always"] = "auto"
    ocr_lang: str = "spa+eng"
    ocr_dpi: int = DEFAULT_OCR_DPI
    ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES
    ocr_max_render_pixels: int = DEFAULT_OCR_MAX_RENDER_PIXELS
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    # Materialization is intentionally opt-in and defaults to virtual/read-only
    # behavior.  The application projection enables it only for an explicit
    # Framework ``apply_actions`` request and points it at state-managed output.
    materialize_on_apply: bool = field(default=False, kw_only=True)
    materialization_directory: Path | None = field(default=None, kw_only=True)
    # Integrated --all may inject a serializable/picklable policy callback.
    # Standalone Archive remains byte-compatible when this is absent.  The
    # signature is deliberately separate so a changed admission policy cannot
    # reuse or publish an earlier, broader member representation.
    member_admission: ArchiveMemberAdmission | None = field(default=None, kw_only=True)
    member_admission_signature: str = field(
        default=ARCHIVE_MEMBER_ADMISSION_DISABLED_SIGNATURE,
        kw_only=True,
    )

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return _archive_processing_provenance(
            self.max_depth,
            self.max_members,
            self.max_central_directory_bytes,
            self.max_member_bytes,
            self.max_total_uncompressed_bytes,
            self.max_text_chars,
            self.max_total_text_chars,
            self.max_compression_ratio,
            self.pdf_max_pages,
            self.pdf_timeout_seconds,
            self.pdf_worker_memory_bytes,
            self.ocr_mode,
            self.ocr_lang,
            self.ocr_dpi,
            self.ocr_max_pages,
            self.ocr_max_render_pixels,
            self.ocr_timeout_seconds,
            self.tesseract_cmd,
            self.tessdata_dir,
            self.member_admission_signature,
        )


@lru_cache(maxsize=64)
def _archive_processing_provenance(
    max_depth: int,
    max_members: int,
    max_central_directory_bytes: int,
    max_member_bytes: int,
    max_total_uncompressed_bytes: int,
    max_text_chars: int,
    max_total_text_chars: int,
    max_compression_ratio: float,
    pdf_max_pages: int,
    pdf_timeout_seconds: float,
    pdf_worker_memory_bytes: int,
    ocr_mode: str,
    ocr_lang: str,
    ocr_dpi: int,
    ocr_max_pages: int,
    ocr_max_render_pixels: int,
    ocr_timeout_seconds: float,
    tesseract_cmd: str | None,
    tessdata_dir: str | None,
    member_admission_signature: str,
) -> ProcessingProvenance:
    ocr_component = (
        {
            "name": "tesseract-runtime",
            "kind": "native-executable",
            "status": "disabled",
        }
        if ocr_mode == "never"
        else resolve_tesseract_runtime(
            command=tesseract_cmd,
            tessdata_dir=tessdata_dir,
            language=ocr_lang,
            timeout_seconds=min(30.0, ocr_timeout_seconds),
        ).component
    )
    return build_processing_provenance(
        "archive-route",
        ARCHIVE_ROUTE_VERSION,
        {
            "max_depth": max_depth,
            "max_members": max_members,
            "max_central_directory_bytes": max_central_directory_bytes,
            "max_member_bytes": max_member_bytes,
            "max_total_uncompressed_bytes": max_total_uncompressed_bytes,
            "max_text_chars": max_text_chars,
            "max_total_text_chars": max_total_text_chars,
            "max_compression_ratio": max_compression_ratio,
            "pdf_max_pages": pdf_max_pages,
            "pdf_timeout_seconds": pdf_timeout_seconds,
            "pdf_worker_memory_bytes": pdf_worker_memory_bytes,
            "ocr_mode": ocr_mode,
            "ocr_language": ocr_lang,
            "ocr_dpi": ocr_dpi,
            "ocr_max_pages": ocr_max_pages,
            "ocr_max_render_pixels": ocr_max_render_pixels,
            "ocr_timeout_seconds": ocr_timeout_seconds,
            "tesseract_source": "explicit" if tesseract_cmd else "path",
            "tessdata_source": "explicit" if tessdata_dir else "default",
            "member_name_policy": "portable-posix-no-traversal-exact-case-v1",
            "nested_path_notation": "container.zip!/member.zip!/file",
            "member_admission_signature": member_admission_signature,
        },
        (
            python_runtime_component(),
            distribution_component("xxhash", "xxhash"),
            distribution_component("pymupdf", "PyMuPDF"),
            distribution_component("pillow", "Pillow"),
            distribution_component("pytesseract", "pytesseract"),
            ocr_component,
        ),
        compatibility_tag=ARCHIVE_ROUTE_VERSION,
    )


class ArchiveExtractionError(ValueError):
    """One top-level container could not be indexed safely."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _ArchiveCacheInvalid(ValueError):
    """Durable Archive representation cannot support a safe cache replay."""


# endregion [01]


# region [02] Safe member names, formats and bounded text extraction


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_PLAIN_TEXT_EXTENSIONS = frozenset(
    {
        ".bat",
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".go",
        ".h",
        ".hpp",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".kt",
        ".log",
        ".lua",
        ".md",
        ".php",
        ".ps1",
        ".py",
        ".r",
        ".rb",
        ".rs",
        ".rst",
        ".sh",
        ".sql",
        ".swift",
        ".tex",
        ".toml",
        ".ts",
        ".tsv",
        ".txt",
        ".vbs",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_HTML_EXTENSIONS = frozenset({".htm", ".html", ".xhtml"})
_NESTED_ARCHIVE_EXTENSIONS = frozenset({".cbz", ".zip", ".zipx"})
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
)
_IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
_SUPPORTED_COMPRESSIONS = frozenset(
    {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }
)


def _normalized_member_name(info: zipfile.ZipInfo) -> str:
    original = str(getattr(info, "orig_filename", info.filename))
    if "\x00" in original:
        raise ArchiveExtractionError(
            "archive_unsafe_member_name",
            "member name contains a NUL byte",
        )
    normalized = original.replace("\\", "/")
    if normalized.startswith(("/", "//")) or _DRIVE_PREFIX.match(normalized):
        raise ArchiveExtractionError(
            "archive_unsafe_member_name",
            f"absolute member name is not allowed: {original!r}",
        )
    normalized = normalized.rstrip("/") if info.is_dir() else normalized
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} for part in parts):
        raise ArchiveExtractionError(
            "archive_unsafe_member_name",
            f"non-portable or traversing member name: {original!r}",
        )
    if len(normalized) > MAX_MEMBER_NAME_CHARS or any(
        len(part) > MAX_MEMBER_SEGMENT_CHARS for part in parts
    ):
        raise ArchiveExtractionError(
            "archive_member_name_limit",
            f"member name exceeds the portable bound: {original!r}",
        )
    if "!/" in normalized:
        raise ArchiveExtractionError(
            "archive_unsafe_member_name",
            f"member name conflicts with nested ZIP path notation: {original!r}",
        )
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ArchiveExtractionError(
            "archive_unsafe_member_name",
            f"member name escapes its archive: {original!r}",
        )
    return normalized


def _member_is_special(info: zipfile.ZipInfo) -> bool:
    unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    return bool(file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR})


def _compression_ratio(info: zipfile.ZipInfo) -> float:
    if info.file_size == 0:
        return 0.0
    return float(info.file_size) / float(max(1, info.compress_size))


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data)


def _decode_text(payload: bytes, *, known_text: bool) -> str | None:
    if not payload:
        return ""
    if b"\x00" in payload[: min(len(payload), 64 * 1024)] and not payload.startswith(
        (b"\xff\xfe", b"\xfe\xff")
    ):
        return None
    encodings = (
        ("utf-8-sig", "utf-16")
        if payload.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8", "cp1252")
    )
    for encoding in encodings:
        if encoding == "cp1252" and not known_text:
            continue
        try:
            value = payload.decode(encoding, "strict")
        except UnicodeError:
            continue
        if known_text:
            return value
        sample = value[:32_768]
        printable = sum(character.isprintable() or character.isspace() for character in sample)
        if sample and printable / len(sample) >= 0.90:
            return value
    return None


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= limit:
        return normalized, False
    return normalized[:limit], True


def _xml_text(payload: bytes) -> str:
    root = safe_xml_fromstring(payload)
    return "\n".join(part.strip() for part in root.itertext() if part.strip())


def _html_text(payload: bytes) -> str | None:
    decoded = _decode_text(payload, known_text=True)
    if decoded is None:
        return None
    parser = _VisibleHTML()
    parser.feed(decoded)
    parser.close()
    return "\n".join(part.strip() for part in parser.parts if part.strip())


def _zip_document_kind(
    payload: bytes,
    *,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> str:
    try:
        inspect_zip_bytes(
            payload,
            max_members=MAX_EMBEDDED_DOCUMENT_MEMBERS,
            max_central_directory_bytes=max_central_directory_bytes,
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            observation, _ = _inspect_logical_document(
                archive,
                budget=_WalkBudget(MAX_EMBEDDED_DOCUMENT_MEMBERS, MAX_DECLARED_MIME_BYTES),
                config=ArchiveRouteConfig(Path("unused"), ocr_mode="never"),
            )
            if observation is not None and observation.identified:
                return observation.logical_kind or "archive"
    except ArchiveExtractionError:
        # A safety bound on identification is not proof of a corrupt archive.
        return "archive"
    except (OSError, RuntimeError, ZipStructureError, zipfile.BadZipFile, zlib.error):
        return "corrupt_archive"
    return "archive"


@dataclass(slots=True)
class _WalkBudget:
    max_members: int
    max_total_bytes: int
    members_seen: int = 0
    bytes_read: int = 0

    def observe_member(self) -> None:
        self.members_seen += 1
        if self.members_seen > self.max_members:
            raise ArchiveExtractionError(
                "archive_member_count_limit",
                f"archive traversal exceeds {self.max_members} visible members",
            )

    def can_read(self, declared_bytes: int) -> bool:
        return declared_bytes <= self.max_total_bytes - self.bytes_read

    def consume(self, actual_bytes: int) -> None:
        if actual_bytes < 0 or actual_bytes > self.max_total_bytes - self.bytes_read:
            raise ArchiveExtractionError(
                "archive_total_uncompressed_limit",
                "archive traversal exceeds its total decompression budget",
            )
        self.bytes_read += actual_bytes


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    budget: _WalkBudget,
    max_bytes: int,
) -> bytes:
    if info.file_size > max_bytes:
        raise ArchiveExtractionError(
            "archive_member_size_limit",
            f"member declares {info.file_size} bytes; limit is {max_bytes}",
        )
    if not budget.can_read(int(info.file_size)):
        raise ArchiveExtractionError(
            "archive_total_uncompressed_limit",
            "member would exceed the total decompression budget",
        )
    chunks: list[bytes] = []
    actual = 0
    with archive.open(info) as source:
        while chunk := source.read(min(ARCHIVE_READ_CHUNK_BYTES, max_bytes - actual + 1)):
            actual += len(chunk)
            if actual > max_bytes:
                raise ArchiveExtractionError(
                    "archive_member_size_limit",
                    f"member output exceeds {max_bytes} bytes",
                )
            chunks.append(chunk)
    if actual != int(info.file_size):
        raise ArchiveExtractionError(
            "archive_member_size_mismatch",
            f"member produced {actual} bytes but declared {info.file_size}",
        )
    budget.consume(actual)
    return b"".join(chunks)


def _inspect_logical_document(
    archive: zipfile.ZipFile,
    *,
    budget: _WalkBudget,
    config: ArchiveRouteConfig,
) -> tuple[LogicalDocumentEvidence | None, dict[str, bytes]]:
    infos = archive.infolist()
    safe_names: list[str] = []
    for info in infos:
        try:
            normalized = _normalized_member_name(info)
        except ArchiveExtractionError:
            continue
        if normalized == info.filename and not info.is_dir() and not _member_is_special(info):
            safe_names.append(normalized)
    names = tuple(safe_names)
    mimetypes = [info for info in infos if info.filename == "mimetype"]
    prefetched: dict[str, bytes] = {}
    declared = None
    if mimetypes:
        if len(mimetypes) != 1:
            raise ArchiveExtractionError(
                "archive_logical_ambiguous_mimetype",
                "duplicate mimetype declarations; no subtype chosen",
            )
        info = mimetypes[0]
        if (
            info.flag_bits & 1
            or info.is_dir()
            or _member_is_special(info)
            or info.compress_type not in _SUPPORTED_COMPRESSIONS
            or _compression_ratio(info) > config.max_compression_ratio
        ):
            raise ArchiveExtractionError(
                "archive_logical_mimetype_unreadable", "mimetype is not safely readable"
            )
        try:
            payload = _read_zip_member(
                archive,
                info,
                budget=budget,
                max_bytes=min(MAX_DECLARED_MIME_BYTES, config.max_member_bytes),
            )
            prefetched["mimetype"] = payload
            declared = payload.decode("ascii", "strict")
            if not declared or any(character.isspace() for character in declared):
                raise ValueError("mimetype is empty or contains whitespace")
        except (
            ArchiveExtractionError,
            OSError,
            UnicodeError,
            ValueError,
            RuntimeError,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            raise ArchiveExtractionError(
                "archive_logical_mimetype_unreadable", f"bounded MIME read failed: {exc}"
            ) from exc
    return identify_logical_document(names, declared), prefetched


def _embedded_part_selected(name: str, kind: str) -> bool:
    lower = name.casefold()
    if kind == "docx":
        return lower == "docprops/core.xml" or (
            lower.startswith("word/")
            and lower.endswith(".xml")
            and any(
                marker in lower
                for marker in (
                    "document.xml",
                    "header",
                    "footer",
                    "comments",
                    "footnotes",
                    "endnotes",
                )
            )
        )
    if kind == "xlsx":
        return lower in {"docprops/core.xml", "xl/workbook.xml", "xl/sharedstrings.xml"} or (
            lower.startswith("xl/worksheets/") and lower.endswith(".xml")
        )
    if kind == "pptx":
        return lower == "docprops/core.xml" or (
            lower.startswith(("ppt/slides/", "ppt/notesslides/")) and lower.endswith(".xml")
        )
    if kind in ODF_KINDS:
        return lower in {"content.xml", "meta.xml", "styles.xml"}
    if kind == "epub":
        return lower.endswith((".xhtml", ".html", ".htm", ".opf", ".ncx"))
    return False


def _extract_embedded_zip_document(
    payload: bytes,
    kind: str,
    *,
    char_limit: int,
    budget: _WalkBudget,
    compression_ratio_limit: float,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> tuple[str, bool]:
    inspect_zip_bytes(
        payload,
        max_members=MAX_EMBEDDED_DOCUMENT_MEMBERS,
        max_central_directory_bytes=max_central_directory_bytes,
    )
    parts: list[str] = []
    total_chars = 0
    truncated = False
    seen: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            budget.observe_member()
            name = _normalized_member_name(info)
            folded = name.casefold()
            if folded in seen:
                raise ArchiveExtractionError(
                    "archive_embedded_duplicate_member",
                    f"duplicate member inside {kind}: {name}",
                )
            seen.add(folded)
            if info.is_dir() or not _embedded_part_selected(name, kind):
                continue
            if info.flag_bits & 0x1 or _member_is_special(info):
                raise ArchiveExtractionError(
                    "archive_embedded_unsafe_member",
                    f"unsafe member inside {kind}: {name}",
                )
            if _compression_ratio(info) > compression_ratio_limit:
                raise ArchiveExtractionError(
                    "archive_compression_ratio_limit",
                    f"embedded member exceeds ratio limit: {name}",
                )
            part = _read_zip_member(
                archive,
                info,
                budget=budget,
                max_bytes=min(DEFAULT_MAX_MEMBER_BYTES, MAX_EMBEDDED_XML_BYTES),
            )
            try:
                text = _html_text(part) if kind == "epub" else _xml_text(part)
            except (ET.ParseError, UnicodeError, ValueError) as exc:
                raise ArchiveExtractionError(
                    "archive_embedded_document_error",
                    f"cannot parse {kind} member {name}: {exc}",
                ) from exc
            if not text:
                continue
            separator = 1 if parts else 0
            remaining = char_limit - total_chars - separator
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            parts.append(text)
            total_chars += len(text) + separator
            if truncated:
                break
    return "\n".join(parts), truncated


def _extract_media_text(
    payload: bytes,
    *,
    kind: Literal["pdf", "image"],
    char_limit: int,
    config: ArchiveRouteConfig,
) -> tuple[str | None, bool, str | None, str | None]:
    command = (
        sys.executable,
        "-m",
        "neocortex.capabilities.formats.archive.text_worker",
        "--kind",
        kind,
        "--max-input-bytes",
        str(config.max_member_bytes),
        "--max-pages",
        str(config.pdf_max_pages),
        "--max-chars",
        str(char_limit),
        "--ocr-mode",
        config.ocr_mode,
        "--ocr-lang",
        config.ocr_lang,
        "--ocr-dpi",
        str(config.ocr_dpi),
        "--ocr-max-pages",
        str(config.ocr_max_pages),
        "--max-render-pixels",
        str(config.ocr_max_render_pixels),
        "--ocr-timeout",
        str(config.ocr_timeout_seconds),
        *(("--tesseract-cmd", config.tesseract_cmd) if config.tesseract_cmd else ()),
        *(("--tessdata-dir", config.tessdata_dir) if config.tessdata_dir else ()),
    )
    gate = _coordinated_archive_gate()
    cancellation = _archive_cancellation()
    stdout_limit = max(64 * 1024, char_limit * 6 + 64 * 1024)
    ocr_enabled = config.ocr_mode != "never"
    # Tesseract is a sequential descendant of the PDF/image worker and
    # inherits its address-space ceiling. Both may be resident concurrently.
    child_resident_bound = config.pdf_worker_memory_bytes * (2 if ocr_enabled else 1)
    # OCR retains at most one bounded page image and its text output at once.
    temporary_bound = len(payload) + (
        config.ocr_max_render_pixels * 8 + 256 * 1024 if ocr_enabled else 0
    )
    parent_grant = None
    if gate is not None:
        from neocortex.runtime.control.global_resources import current_resource_grant

        parent_grant = current_resource_grant()
        if parent_grant is not None:
            parent_grant.release_cpu()
    # The child's address-space ceiling is a conservative admission bound,
    # not a measured RSS claim. Input/capture buffers and its stdin temporary
    # are separate charges while the parent retains its ZIP/member payload.
    admission = (
        nullcontext()
        if gate is None
        else gate.admit(
            len(payload) * 2 + stdout_limit + 256 * 1024,
            resident_bytes=child_resident_bound,
            temp_bytes=temporary_bound,
            cpu_slots=1,
            native_threads=1,
            io_slots=1,
            phase=f"archive-{kind}-worker",
            cancellation=cancellation,
        )
    )
    try:
        if cancellation is not None:
            cancellation.checkpoint()
        with admission as child_grant:
            environment = {
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "OMP_THREAD_LIMIT": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
            if child_grant is not None:
                environment = child_grant.subprocess_env(environment)
            completed = run_bounded_capture(
                command,
                input_bytes=payload,
                timeout_seconds=config.pdf_timeout_seconds,
                stdout_limit_bytes=stdout_limit,
                stderr_limit_bytes=256 * 1024,
                environment=environment,
                memory_limit_bytes=config.pdf_worker_memory_bytes,
                cancellation=cancellation,
                **({"on_started": child_grant.register_process} if child_grant is not None else {}),
            )
            if cancellation is not None:
                cancellation.checkpoint()
    except (OSError, RuntimeError, SubprocessOutputLimitError) as exc:
        return None, False, f"{kind}_worker_error:{type(exc).__name__}", None
    finally:
        if parent_grant is not None:
            parent_grant.checkpoint()
    try:
        result = json.loads(completed.stdout.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        return None, False, f"{kind}_worker_invalid_output", None
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("ok"):
        reason = result.get("reason") if isinstance(result, dict) else None
        detail = str(reason or f"{kind}_worker_exit_{completed.returncode}")
        return None, False, detail, None
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, False, f"{kind}_ocr_no_text", str(result.get("extraction_mode") or "metadata")
    return (
        text,
        bool(result.get("truncated")),
        None,
        str(result.get("extraction_mode") or "native"),
    )


def _image_media_type(name: str, payload: bytes) -> str | None:
    for signature, media_type in _IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return media_type
    if payload.startswith(b"RIFF") and len(payload) >= 12 and payload[8:12] == b"WEBP":
        return "image/webp"
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix in _IMAGE_EXTENSIONS:
        return {
            ".bmp": "image/bmp",
            ".gif": "image/gif",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
        }[suffix]
    return None


@dataclass(frozen=True, slots=True)
class _ExtractedContent:
    text: str | None
    kind: str
    media_type: str
    detail: str | None = None
    issue_code: str | None = None


def _extract_member_content(
    name: str,
    payload: bytes,
    *,
    zip_kind: str | None,
    char_limit: int,
    budget: _WalkBudget,
    config: ArchiveRouteConfig,
) -> _ExtractedContent:
    suffix = PurePosixPath(name).suffix.casefold()
    if zip_kind and zip_kind not in {"archive", "corrupt_archive"}:
        try:
            text, truncated = _extract_embedded_zip_document(
                payload,
                zip_kind,
                char_limit=char_limit,
                budget=budget,
                compression_ratio_limit=config.max_compression_ratio,
                max_central_directory_bytes=config.max_central_directory_bytes,
            )
        except (
            ArchiveExtractionError,
            OSError,
            RuntimeError,
            ZipStructureError,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            return _ExtractedContent(
                None,
                zip_kind,
                LOGICAL_MEDIA_TYPES.get(zip_kind, f"application/{zip_kind}"),
                f"{type(exc).__name__}: {exc}"[:500],
                exc.code
                if isinstance(exc, ArchiveExtractionError)
                else "archive_embedded_document_error",
            )
        return _ExtractedContent(
            text or None,
            zip_kind,
            LOGICAL_MEDIA_TYPES.get(zip_kind, f"application/{zip_kind}"),
            "text_truncated" if truncated else None,
            "archive_text_limit" if truncated else None,
        )
    if payload.startswith(b"%PDF-") or suffix == ".pdf":
        pdf_text, truncated, detail, extraction_mode = _extract_media_text(
            payload,
            kind="pdf",
            char_limit=char_limit,
            config=config,
        )
        issue_code = None
        if detail and detail not in {"pdf_ocr_no_text"}:
            issue_code = "archive_pdf_extraction_error"
        return _ExtractedContent(
            pdf_text,
            "pdf",
            "application/pdf",
            (
                detail
                or ("text_truncated" if truncated else None)
                or (f"extraction={extraction_mode}" if extraction_mode else None)
            ),
            "archive_text_limit" if truncated else issue_code,
        )
    image_media_type = _image_media_type(name, payload)
    if image_media_type is not None:
        image_text, truncated, detail, extraction_mode = _extract_media_text(
            payload,
            kind="image",
            char_limit=char_limit,
            config=config,
        )
        issue_code = None
        if detail and detail not in {"image_ocr_no_text"}:
            issue_code = "archive_image_ocr_error"
        return _ExtractedContent(
            image_text,
            "image",
            image_media_type,
            (
                detail
                or ("text_truncated" if truncated else None)
                or (f"extraction={extraction_mode}" if extraction_mode else None)
            ),
            "archive_text_limit" if truncated else issue_code,
        )
    if suffix in _HTML_EXTENSIONS:
        try:
            value = _html_text(payload)
        except (UnicodeError, ValueError) as exc:
            return _ExtractedContent(
                None,
                "html",
                "text/html",
                str(exc)[:500],
                "archive_text_decode_error",
            )
        if value is None:
            return _ExtractedContent(None, "html", "text/html", "binary_html")
        text, truncated = _bounded_text(value, char_limit)
        return _ExtractedContent(
            text,
            "html",
            "text/html",
            "text_truncated" if truncated else None,
            "archive_text_limit" if truncated else None,
        )
    if suffix == ".xml":
        try:
            value = _xml_text(payload)
        except (ET.ParseError, UnicodeError, ValueError) as exc:
            return _ExtractedContent(
                None,
                "xml",
                "application/xml",
                str(exc)[:500],
                "archive_text_decode_error",
            )
        text, truncated = _bounded_text(value, char_limit)
        return _ExtractedContent(
            text,
            "xml",
            "application/xml",
            "text_truncated" if truncated else None,
            "archive_text_limit" if truncated else None,
        )
    known_text = suffix in _PLAIN_TEXT_EXTENSIONS
    value = _decode_text(payload, known_text=known_text)
    if value is not None:
        text, truncated = _bounded_text(value, char_limit)
        kind = suffix.removeprefix(".") or "text"
        media = "application/json" if suffix in {".json", ".jsonl"} else "text/plain"
        return _ExtractedContent(
            text,
            kind,
            media,
            "text_truncated" if truncated else None,
            "archive_text_limit" if truncated else None,
        )
    return _ExtractedContent(None, "binary", "application/octet-stream")


# endregion [02]


# region [03] Recursive traversal and durable publication


@dataclass(slots=True)
class _ContainerCounters:
    members: int = 0
    indexed: int = 0
    metadata_only: int = 0
    nested_archives: int = 0
    issues: int = 0
    coverage_issues: int = 0
    text_chars: int = 0
    max_depth: int = 0
    materialization_applied: int = 0
    materialization_reused: int = 0
    materialization_pending: int = 0
    materialization_collisions: int = 0
    materialization_units_preserved: int = 0
    materialization_manifest_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _MemberObservation:
    member_chain: str
    member_path: str
    depth: int
    info: zipfile.ZipInfo
    content: _ExtractedContent
    document_role: str
    logical_document_chain: str | None


@dataclass(frozen=True, slots=True)
class _LogicalObservation:
    member_chain: str
    observation: LogicalDocumentEvidence
    name: str
    depth: int


@dataclass(frozen=True, slots=True)
class _IssueObservation:
    member_chain: str | None
    depth: int
    code: str
    detail: str


class _ArchiveObservationSpool:
    """One owned anonymous stream; never load a pickle supplied by the corpus.

    Only the three in-process observation dataclasses are serialized. Each
    record uses its own memo, so previous member text is not retained in RAM.
    The task lease owns actual temporary bytes until the consumer closes it.
    """

    def __init__(self, config: ArchiveRouteConfig, grant) -> None:
        self.stream = tempfile.TemporaryFile()
        self.grant = grant
        self.size = 0
        self.max_record_size = 0
        self.record_limit = (
            config.max_total_text_chars * 4 + config.max_central_directory_bytes * 4 + 1024 * 1024
        )
        self.total_limit = (
            config.max_total_text_chars * 8
            + config.max_members * (MAX_MEMBER_NAME_CHARS * 8 + 20_000)
            + config.max_central_directory_bytes * 4
        )

    def append(self, observation: _MemberObservation | _LogicalObservation | _IssueObservation):
        payload = pickle.dumps(observation, protocol=5)
        if len(payload) > self.record_limit or self.size + len(payload) + 8 > self.total_limit:
            raise ArchiveExtractionError("archive_spool_limit", "archive observation spool is full")
        new_size = self.size + len(payload) + 8
        if self.grant is not None:
            from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

            try:
                self.grant.resize_temp_bytes(
                    new_size, directory=tempfile.gettempdir(), file_descriptor=self.stream.fileno()
                )
            except MemoryBudgetExceeded as exc:
                raise ArchiveExtractionError("archive_spool_limit", str(exc)) from exc
        self.stream.write(len(payload).to_bytes(8, "little"))
        self.stream.write(payload)
        self.size = new_size
        self.max_record_size = max(self.max_record_size, len(payload))

    def observations(self):
        self.stream.seek(0)
        while header := self.stream.read(8):
            if len(header) != 8:
                raise ArchiveExtractionError("archive_spool_invalid", "truncated owned spool header")
            size = int.from_bytes(header, "little")
            if size > self.record_limit:
                raise ArchiveExtractionError("archive_spool_invalid", "oversized owned spool record")
            payload = self.stream.read(size)
            if len(payload) != size:
                raise ArchiveExtractionError("archive_spool_invalid", "truncated owned spool record")
            observation = pickle.loads(payload)
            if not isinstance(observation, (_MemberObservation, _LogicalObservation, _IssueObservation)):
                raise ArchiveExtractionError("archive_spool_invalid", "unknown owned observation")
            yield observation

    def close(self) -> None:
        stream = getattr(self, "stream", None)
        if stream is not None:
            stream.close()

    def __del__(self) -> None:
        self.close()


def _member_key(container_key: str, member_chain: str) -> str:
    digest = xxhash.xxh3_128_hexdigest(
        f"{container_key}\x00{member_chain}".encode("utf-8", "surrogatepass")
    )
    return f"archive:{digest}"


def _virtual_path(container_path: str, member_chain: str) -> str:
    return f"{container_path}!/{member_chain}" if member_chain else container_path


def _delete_container(connection: sqlite3.Connection, container_key: str) -> int:
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents WHERE container_key=?", (container_key,)
        ).fetchone()[0]
    )
    keys = tuple(row[0] for row in connection.execute(
        "SELECT file_key FROM documents WHERE container_key=?", (container_key,)
    ))
    delete_format_fts_keys(connection, "document_fts", keys)
    connection.execute("DELETE FROM containers WHERE container_key=?", (container_key,))
    return count


def _prepare_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
    run_id: int,
) -> str:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    if (
        connection.execute(
            "SELECT 1 FROM containers WHERE container_key=?", (container_key,)
        ).fetchone()
        is not None
    ):
        _delete_container(connection, container_key)
    connection.execute(
        """INSERT INTO containers(
        container_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'building',?,?)""",
        (
            container_key,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            run_id,
            time.time_ns(),
        ),
    )
    return container_key


def _record_issue(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    container_key: str,
    counters: _ContainerCounters,
    *,
    member_chain: str | None,
    depth: int,
    code: str,
    detail: str,
) -> None:
    counters.issues += 1
    if issue_diagnosis(code)[0] != "identification_only":
        counters.coverage_issues += 1
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_IssueObservation(member_chain, depth, code, detail[:2_000]))
        return
    connection.execute(
        """INSERT INTO archive_issues(
        container_key,member_chain,archive_depth,reason_code,detail,created_ns)
        VALUES(?,?,?,?,?,?)""",
        (container_key, member_chain, depth, code, detail[:2_000], time.time_ns()),
    )


def _store_member(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    container_key: str,
    member_chain: str,
    member_path: str,
    depth: int,
    info: zipfile.ZipInfo,
    content: _ExtractedContent,
    signature: str,
    run_id: int,
    *,
    document_role: str = "archive_member",
    logical_document_chain: str | None = None,
) -> None:
    file_key = _member_key(container_key, member_chain)
    path = _virtual_path(snapshot.path, member_chain)
    conflict = connection.execute(
        "SELECT file_key FROM documents WHERE path=? AND file_key<>?",
        (path, file_key),
    ).fetchone()
    if conflict is not None:
        raise ArchiveExtractionError(
            "archive_virtual_path_collision",
            f"two archive members map to the same visible path: {path}",
        )
    text = content.text
    encoded = None if text is None else text.encode("utf-8")
    status = "indexed" if text is not None else "metadata_only"
    if content.kind == "archive":
        status = "archive"
    connection.execute(
        """INSERT INTO documents(
        file_key,container_key,path,container_path,member_chain,member_path,
        archive_depth,content_kind,media_type,size,compressed_size,crc32,
        mtime_ns,birthtime_ns,processing_signature,status,text_zlib,text_chars,
        text_xxh3_128,detail,error_type,error_message,last_seen_run_id,updated_ns,
        document_role,logical_document_chain,independently_organizable)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            file_key,
            container_key,
            path,
            snapshot.path,
            member_chain,
            member_path,
            depth,
            content.kind,
            content.media_type,
            int(info.file_size),
            int(info.compress_size),
            int(info.CRC),
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            status,
            None if encoded is None else zlib.compress(encoded, level=6),
            0 if text is None else len(text),
            None if encoded is None else xxhash.xxh3_128_hexdigest(encoded),
            content.detail,
            content.issue_code,
            content.detail if content.issue_code else None,
            run_id,
            time.time_ns(),
            document_role,
            logical_document_chain,
            # A conventional archive member remains a separately identifiable
            # logical candidate even though it is still a virtual resource and
            # cannot be moved by the current backend.  Only parts of a known
            # document package are inseparable components.
            int(document_role != "document_component"),
        ),
    )
    insert_format_fts_row(
        connection, "document_fts",
        ("file_key", "path", "container_path", "container_name", "member_chain", "content_kind", "body"),
        (
            file_key,
            path,
            snapshot.path,
            Path(snapshot.path).name,
            member_chain,
            content.kind,
            text or "",
        ),
    )


def _observe_member(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    snapshot: FileSnapshot,
    container_key: str,
    member_chain: str,
    member_path: str,
    depth: int,
    info: zipfile.ZipInfo,
    content: _ExtractedContent,
    signature: str,
    run_id: int,
    *,
    document_role: str = "archive_member",
    logical_document_chain: str | None = None,
) -> None:
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_MemberObservation(
            member_chain, member_path, depth, info, content,
            document_role, logical_document_chain,
        ))
        return
    _store_member(
        connection, snapshot, container_key, member_chain, member_path, depth,
        info, content, signature, run_id, document_role=document_role,
        logical_document_chain=logical_document_chain,
    )


def _store_logical_observation(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    container_key: str,
    member_chain: str,
    observation: LogicalDocumentEvidence,
    *,
    name: str,
    depth: int,
    counters: _ContainerCounters,
    diagnose: bool = True,
) -> None:
    if isinstance(connection, _ArchiveObservationSpool):
        connection.append(_LogicalObservation(member_chain, observation, name, depth))
    else:
        connection.execute(
            """INSERT INTO archive_logical_documents(
            container_key,member_chain,physical_media_type,declared_mime,logical_kind,
            proposed_extension,evidence_json,identification_status,integrity_status,opening_status)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                container_key,
                member_chain,
                ARCHIVE_MIME,
                observation.declared_mime,
                observation.logical_kind,
                observation.proposed_extension,
                json.dumps(observation.evidence),
                observation.identification_status,
                observation.integrity_status,
                observation.opening_status,
            ),
        )
    if not diagnose:
        return
    code = None
    if observation.identified:
        if PurePosixPath(name).suffix.casefold() != observation.proposed_extension:
            code = "archive_logical_extension_mismatch"
    else:
        code = f"archive_logical_{observation.identification_status}"
    if code:
        _record_issue(
            connection,
            container_key,
            counters,
            member_chain=member_chain or None,
            depth=depth,
            code=code,
            detail=json.dumps(
                {
                    "physical_media_type": ARCHIVE_MIME,
                    "declared_mime": observation.declared_mime,
                    "logical_kind_inference": observation.logical_kind,
                    "proposed_extension": observation.proposed_extension,
                    "structural_member_names": observation.evidence,
                    "identification_status": observation.identification_status,
                    "integrity_status": observation.integrity_status,
                    "opening_status": observation.opening_status,
                    "effect_authorized": False,
                },
                sort_keys=True,
            ),
        )


def _metadata_content(
    *,
    detail: str | None = None,
    issue_code: str | None = None,
) -> _ExtractedContent:
    return _ExtractedContent(
        None,
        "binary",
        "application/octet-stream",
        detail,
        issue_code,
    )


def _archive_member_admission(
    config: ArchiveRouteConfig,
    snapshot: FileSnapshot,
    info: zipfile.ZipInfo,
    *,
    name: str,
    member_chain: str,
    depth: int,
    payload: bytes,
    cancellation: CancellationToken,
) -> tuple[bool, str | None, str | None]:
    """Run the optional policy after verified member read and before parsing.

    The callback is intentionally a narrow seam: it receives bounded metadata
    and a prefix, never the full payload, and its result is interpreted as a
    disposition only.  A false/sensitive/metadata-only disposition preserves
    the virtual member as metadata while preventing text extraction, nested
    traversal and FTS body publication.
    """

    cancellation.checkpoint()
    callback = config.member_admission
    if callback is None:
        return True, None, None
    context = ArchiveMemberAdmissionContext(
        member_name=name,
        member_chain=member_chain,
        depth=depth,
        container_path=Path(snapshot.path),
        size=int(info.file_size),
        compressed_size=int(info.compress_size),
        crc32=int(info.CRC),
        prefix=payload[:MAX_ARCHIVE_ADMISSION_PREFIX_BYTES],
    )
    try:
        decision = callback(context)
        disposition = getattr(decision, "disposition", decision)
        if disposition is True or disposition == "process":
            allowed = True
        elif disposition is False or disposition in {"metadata_only", "sensitive", "deny"}:
            allowed = False
        else:
            raise ValueError("member admission callback returned an unsupported disposition")
    except CancellationRequested:
        raise
    except Exception as exc:
        cancellation.checkpoint()
        return (
            False,
            "archive_member_admission_error",
            f"member admission policy failed: {type(exc).__name__}",
        )
    cancellation.checkpoint()
    if allowed:
        return True, None, None
    return (
        False,
        "archive_member_admission_denied",
        "member denied by configured archive admission policy",
    )


@dataclass(frozen=True, slots=True)
class _ArchiveMemberWork:
    info: zipfile.ZipInfo
    name: str
    member_chain: str
    payload: bytes | None
    zip_kind: str | None
    content: _ExtractedContent | None
    nested_observation: LogicalDocumentEvidence | None
    config: ArchiveRouteConfig
    extraction_required: bool = False


_STOP_ARCHIVE_WALK = object()
_ARCHIVE_PROCESS_MIN_BYTES = 256 * 1024


class _ArchiveContainerGroup:
    def __init__(self) -> None:
        self.active = 0
        self.lock = threading.Lock()

    @contextmanager
    def extracting(self):
        with self.lock:
            self.active += 1
        token = _ARCHIVE_CONTAINER_GROUP.set(self)
        try:
            yield
        finally:
            _ARCHIVE_CONTAINER_GROUP.reset(token)
            with self.lock:
                self.active -= 1

    def member_capacity(self, gate, config: ArchiveRouteConfig) -> int:
        target = _archive_container_capacity(gate, config, media_possible=False)
        with self.lock:
            count = max(1, self.active)
        # All maps use one route budget. Dividing the conservative parent+
        # child target prevents every ZIP from growing a full independent
        # pool and exhausting memory with idle interpreters. Re-evaluate it
        # as peers finish and resources return.
        return 0 if target == 0 else max(1, target // count)


_ARCHIVE_CONTAINER_GROUP: ContextVar[_ArchiveContainerGroup | None] = ContextVar(
    "archive_container_group", default=None
)


@contextmanager
def _archive_work_admission(
    *, phase: str, estimated_bytes: int = 0, cpu_slots: int = 1, temp_bytes: int = 0
):
    gate = _coordinated_archive_gate()
    if gate is None:
        yield None
        return
    from neocortex.runtime.control.global_resources import current_resource_grant, resource_grant_scope

    parent = current_resource_grant()
    resume_parent = parent is not None and parent.cpu_slots > 0
    if resume_parent and parent is not None:
        parent.release_cpu()
    try:
        with gate.admit(
            estimated_bytes, cpu_slots=cpu_slots, native_threads=cpu_slots, io_slots=1, phase=phase,
            cancellation=_archive_cancellation(), temp_bytes=temp_bytes,
        ) as grant:
            with resource_grant_scope(grant) if grant is not None else nullcontext():
                yield grant
    finally:
        if resume_parent and parent is not None:
            parent.checkpoint()


def _archive_member_memory(work: _ArchiveMemberWork) -> int:
    size = len(work.payload or b"")
    return 4 * 1024 * 1024 + size * 2 + min(size, work.config.max_text_chars) * 12


def _archive_member_uses_process(work: _ArchiveMemberWork) -> bool:
    payload = work.payload or b""
    return not (
        payload.startswith(b"%PDF-")
        or PurePosixPath(work.name).suffix.casefold() == ".pdf"
        or _image_media_type(work.name, payload) is not None
    )


def _extract_archive_member(work: _ArchiveMemberWork) -> _ArchiveMemberWork:
    """Pure text/XML runs in processes; media supervises its bounded child."""

    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    cancellation = current_worker_cancellation()
    if cancellation is not None:
        cancellation.checkpoint()
    content = work.content
    if content is None:
        content = _extract_member_content(
            work.name,
            work.payload or b"",
            zip_kind=None,
            char_limit=work.config.max_text_chars,
            budget=_WalkBudget(work.config.max_members, work.config.max_total_uncompressed_bytes),
            config=work.config,
        )
    if cancellation is not None:
        cancellation.checkpoint()
    return replace(work, payload=None, content=content)


def _iter_archive_members(
    entries: Sequence[zipfile.ZipInfo],
    prepare: Callable[[zipfile.ZipInfo], _ArchiveMemberWork | object | None],
    *,
    budget: _WalkBudget,
    counters: _ContainerCounters,
    config: ArchiveRouteConfig,
    cancellation: CancellationToken,
) -> Generator[_ArchiveMemberWork, None, None]:
    """Drain siblings before nested packages so one shared budget stays exact."""

    from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map

    gate = _coordinated_archive_gate()
    group = _ARCHIVE_CONTAINER_GROUP.get()
    # A new interpreter costs much more than a few tiny text members. Reserve
    # persistent processes when declared *uncompressed* work can amortize it;
    # short packages use their supervisors for bounded reads and brief parsing.
    # This does not claim CPU parallelism for the Python work in those threads.
    declared_work = sum(
        info.file_size for info in entries if 0 < info.file_size <= config.max_member_bytes
    )
    executor_kind: Literal["thread", "process"] = (
        "process" if declared_work >= _ARCHIVE_PROCESS_MIN_BYTES else "thread"
    )
    iterator = iter(entries)
    exhausted = False
    barrier: _ArchiveMemberWork | None = None

    deferred: deque[zipfile.ZipInfo] = deque()
    stopped = False

    def batch() -> Iterator[zipfile.ZipInfo]:
        nonlocal exhausted
        while barrier is None and not stopped:
            cancellation.checkpoint()
            if deferred:
                yield deferred.popleft()
                continue
            try:
                info = next(iterator)
            except StopIteration:
                exhausted = True
                return
            # No blocking admissions or payload reads in source iteration:
            # admitted workers may already be waiting for owner preparation.
            yield info

    def prepared(info: zipfile.ZipInfo):
        nonlocal barrier, stopped
        if stopped:
            return ImmediateResult(None)
        if barrier is not None:
            # Speculation after a nested package stops before reading it or
            # spending the shared budget. This deque is bounded by the map.
            deferred.append(info)
            return ImmediateResult(None)
        work = prepare(info)
        if work is _STOP_ARCHIVE_WALK:
            stopped = True
            deferred.clear()
            return ImmediateResult(None)
        if not isinstance(work, _ArchiveMemberWork):
            return ImmediateResult(None)
        if work.zip_kind not in {None, "corrupt_archive"}:
            barrier = work
            return ImmediateResult(None)
        if work.content is not None:
            return ImmediateResult(replace(work, payload=None))
        if not _archive_member_uses_process(work):
            require_capacity(
                _archive_member_memory(work)
                + config.pdf_worker_memory_bytes * (2 if config.ocr_mode != "never" else 1)
                + len(work.payload or b"") * 2 + config.max_text_chars * 6 + 320 * 1024
            )
        return work

    def require_capacity(demand: int) -> None:
        from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

        interpreter = 64 * 1024 * 1024 if executor_kind == "process" else 0
        if gate is not None:
            try:
                gate.worker_capacity(
                    estimated_bytes=_archive_container_memory(config) + demand + interpreter,
                    native_threads=1,
                )
            except MemoryBudgetExceeded as exc:
                raise ArchiveExtractionError("archive_resource_limit", str(exc)) from exc

    def estimate(info: zipfile.ZipInfo) -> int:
        readable = (
            0 < info.file_size <= config.max_member_bytes
            and not info.is_dir() and not info.flag_bits & 0x1
            and not _member_is_special(info)
            and info.compress_type in _SUPPORTED_COMPRESSIONS
            and _compression_ratio(info) <= config.max_compression_ratio
        )
        size = info.file_size if readable else 0
        demand = 4 * 1024 * 1024 + size * 2 + min(size, config.max_text_chars) * 12
        require_capacity(demand)
        return demand

    while (not exhausted or deferred) and not stopped:
        with elastic_map(
            _extract_archive_member,
            batch(),
            gate=gate,
            capacity=(
                None if group is None
                else lambda: group.member_capacity(gate, config)
            ),
            estimated_bytes=estimate,
            native_threads=1,
            io_slots=1,
            phase="archive-member-extract",
            prepare=prepared,
            executor_kind=executor_kind,
            process_predicate=_archive_member_uses_process,
            cancellation=cancellation,
        ) as results:
            for result in results:
                if result is not None:
                    yield result
        if barrier is not None:
            work, barrier = barrier, None
            with _archive_work_admission(
                phase="archive-package", estimated_bytes=_archive_member_memory(work)
            ):
                if work.content is None:
                    remaining = config.max_total_text_chars - counters.text_chars
                    content = (
                        _metadata_content(
                            detail="container text budget is exhausted",
                            issue_code="archive_total_text_limit",
                        )
                        if remaining < 1 else _extract_member_content(
                            work.name, work.payload or b"", zip_kind=work.zip_kind,
                            char_limit=min(config.max_text_chars, remaining),
                            budget=budget, config=config,
                        )
                    )
                    work = replace(work, content=content)
                yield work


def _walk_zip(
    connection: sqlite3.Connection | _ArchiveObservationSpool,
    archive: zipfile.ZipFile,
    snapshot: FileSnapshot,
    container_key: str,
    *,
    prefix: str,
    depth: int,
    budget: _WalkBudget,
    counters: _ContainerCounters,
    config: ArchiveRouteConfig,
    run_id: int,
    cancellation: CancellationToken,
    component_of: str | None = None,
) -> None:
    logical_document: LogicalDocumentEvidence | None = None
    prefetched: dict[str, bytes] = {}
    if not prefix:
        try:
            logical_document, prefetched = _inspect_logical_document(
                archive, budget=budget, config=config
            )
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=None,
                depth=0,
                code=exc.code,
                detail=str(exc),
            )
        if logical_document is not None:
            _store_logical_observation(
                connection,
                container_key,
                "",
                logical_document,
                name=snapshot.path,
                depth=0,
                counters=counters,
            )
    own_logical_document = logical_document is not None and logical_document.identified
    component_chain = "" if own_logical_document else component_of
    is_component = component_chain is not None
    logical_text_parts: list[str] = []
    seen_names: set[str] = set()
    def prepare_entry(info: zipfile.ZipInfo):
        payload: bytes | None = None
        zip_kind: str | None = None
        cancellation.checkpoint()
        try:
            budget.observe_member()
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=prefix.rstrip("!/") or None,
                depth=depth,
                code=exc.code,
                detail=str(exc),
            )
            return _STOP_ARCHIVE_WALK
        counters.max_depth = max(counters.max_depth, depth)
        try:
            name = _normalized_member_name(info)
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                # Preserve the rejected *name as data* so issues remain
                # filterable without pretending it is a safe virtual path.
                member_chain=f"{prefix}{info.orig_filename[:MAX_MEMBER_NAME_CHARS]}",
                depth=depth,
                code=exc.code,
                detail=str(exc),
            )
            return None
        member_chain = f"{prefix}{name}"
        if name in seen_names:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=member_chain,
                depth=depth,
                code="archive_duplicate_member",
                detail=f"duplicate member name in one archive: {name}",
            )
            return None
        seen_names.add(name)
        if info.is_dir():
            return None
        counters.members += 1
        nested_observation: LogicalDocumentEvidence | None = None
        if info.flag_bits & 0x1:
            content = _metadata_content(
                detail="encrypted ZIP members are not read",
                issue_code="archive_encrypted_member",
            )
        elif _member_is_special(info):
            content = _metadata_content(
                detail="symlink or special ZIP member is not read",
                issue_code="archive_special_member",
            )
        elif info.compress_type not in _SUPPORTED_COMPRESSIONS:
            content = _metadata_content(
                detail=f"unsupported ZIP compression method {info.compress_type}",
                issue_code="archive_unsupported_compression",
            )
        elif _compression_ratio(info) > config.max_compression_ratio:
            content = _metadata_content(
                detail=(
                    f"declared compression ratio {_compression_ratio(info):.2f} "
                    f"exceeds {config.max_compression_ratio:.2f}"
                ),
                issue_code="archive_compression_ratio_limit",
            )
        elif info.file_size > config.max_member_bytes:
            content = _metadata_content(
                detail=(
                    f"member declares {info.file_size} bytes; limit is {config.max_member_bytes}"
                ),
                issue_code="archive_member_size_limit",
            )
        elif name not in prefetched and not budget.can_read(int(info.file_size)):
            content = _metadata_content(
                detail="member would exceed the total decompression budget",
                issue_code="archive_total_uncompressed_limit",
            )
        else:
            try:
                payload = (
                    prefetched.pop(name)
                    if name in prefetched
                    else _read_zip_member(
                        archive,
                        info,
                        budget=budget,
                        max_bytes=config.max_member_bytes,
                    )
                )
            except (
                ArchiveExtractionError,
                OSError,
                RuntimeError,
                zipfile.BadZipFile,
                zlib.error,
            ) as exc:
                code = (
                    exc.code
                    if isinstance(exc, ArchiveExtractionError)
                    else "archive_member_read_error"
                )
                content = _metadata_content(
                    detail=f"{type(exc).__name__}: {exc}"[:500],
                    issue_code=code,
                )
            else:
                admitted, admission_code, admission_detail = _archive_member_admission(
                    config,
                    snapshot,
                    info,
                    name=name,
                    member_chain=member_chain,
                    depth=depth,
                    payload=payload,
                    cancellation=cancellation,
                )
                if not admitted:
                    # Do not retain a denied member's full bytes in the owner
                    # spool.  Metadata/CRC/localizer fields remain durable,
                    # but no text or nested parser is allowed to observe it.
                    payload = None
                    content = _metadata_content(
                        detail=admission_detail,
                        issue_code=admission_code,
                    )
                else:
                    suffix = PurePosixPath(name).suffix.casefold()
                    zip_kind = None
                    if payload.startswith(_ZIP_MAGIC_PREFIXES) or suffix in _NESTED_ARCHIVE_EXTENSIONS:
                        try:
                            inspect_zip_bytes(
                                payload,
                                max_members=MAX_EMBEDDED_DOCUMENT_MEMBERS,
                                max_central_directory_bytes=config.max_central_directory_bytes,
                            )
                            with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                                nested_observation, _ = _inspect_logical_document(
                                    nested,
                                    budget=budget,
                                    config=config,
                                )
                            zip_kind = (
                                nested_observation.logical_kind
                                if nested_observation is not None and nested_observation.identified
                                else "archive"
                            )
                        except ArchiveExtractionError as exc:
                            zip_kind = "archive"
                            _record_issue(
                                connection,
                                container_key,
                                counters,
                                member_chain=member_chain,
                                depth=depth,
                                code=exc.code,
                                detail=str(exc),
                            )
                        except (
                            OSError,
                            RuntimeError,
                            ZipStructureError,
                            zipfile.BadZipFile,
                            zlib.error,
                        ):
                            zip_kind = "corrupt_archive"
                        if nested_observation is not None:
                            _store_logical_observation(
                                connection,
                                container_key,
                                member_chain,
                                nested_observation,
                                name=name,
                                depth=depth,
                                counters=counters,
                            )
                    if zip_kind == "archive":
                        content = _ExtractedContent(
                            None,
                            "archive",
                            "application/zip",
                        )
                    elif zip_kind == "corrupt_archive":
                        content = _metadata_content(
                            detail="nested ZIP structure is corrupt or unsupported",
                            issue_code="archive_nested_corrupt",
                        )
                    else:
                        content = None
        return _ArchiveMemberWork(
            info, name, member_chain, payload, zip_kind, content, nested_observation, config,
            content is None,
        )

    def sequential_members():
        for info in archive.infolist():
            work = prepare_entry(info)
            if work is _STOP_ARCHIVE_WALK:
                break
            if not isinstance(work, _ArchiveMemberWork):
                continue
            if work.content is None:
                remaining = config.max_total_text_chars - counters.text_chars
                content = (
                    _metadata_content(
                        detail="container text budget is exhausted",
                        issue_code="archive_total_text_limit",
                    ) if remaining < 1 else _extract_member_content(
                        work.name, work.payload or b"", zip_kind=work.zip_kind,
                        char_limit=min(config.max_text_chars, remaining),
                        budget=budget, config=config,
                    )
                )
                work = replace(work, content=content)
            yield work

    if _coordinated_archive_gate() is not None:
        from neocortex.runtime.control.global_resources import current_resource_grant

        structure_grant = current_resource_grant()
        if structure_grant is not None:
            # Central-directory and logical-package inspection are finished.
            # Keep their memory; member jobs now own CPU and I/O execution.
            structure_grant.release_cpu()
    members = (
        sequential_members()
        if _coordinated_archive_gate() is None else _iter_archive_members(
            archive.infolist(), prepare_entry, budget=budget, counters=counters,
            config=config, cancellation=cancellation,
        )
    )
    with closing(members):
        for work in members:
            info, name, member_chain = work.info, work.name, work.member_chain
            payload = work.payload
            nested_observation = work.nested_observation
            content = work.content
            if content is None:
                raise RuntimeError("archive member was not extracted")
            # Worker results arrive in original order. Only the owner spends the
            # shared text budget, including results computed speculatively ahead.
            if content.kind != "archive":
                remaining = config.max_total_text_chars - counters.text_chars
                if remaining < 1 and work.extraction_required:
                    content = _metadata_content(
                        detail="container text budget is exhausted",
                        issue_code="archive_total_text_limit",
                    )
                elif content.text is not None and len(content.text) > remaining:
                    content = replace(
                        content, text=content.text[:remaining], detail="text_truncated",
                        issue_code="archive_text_limit",
                    )

            _observe_member(
                connection,
                snapshot,
                container_key,
                member_chain,
                name,
                depth,
                info,
                content,
                config.processing_signature,
                run_id,
                document_role=(
                    "document_component"
                    if is_component
                    else "logical_document"
                    if nested_observation is not None and nested_observation.identified
                    else "archive_member"
                ),
                logical_document_chain=(
                    component_chain
                    if is_component
                    else member_chain
                    if nested_observation is not None and nested_observation.identified
                    else None
                ),
            )
            if (
                is_component
                and content.text
                and logical_document is not None
                and _embedded_part_selected(name, logical_document.logical_kind or "")
            ):
                logical_text_parts.append(content.text)
            if content.text is None:
                counters.metadata_only += 1
            else:
                counters.indexed += 1
                counters.text_chars += len(content.text)
            if content.issue_code:
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code=content.issue_code,
                    detail=content.detail or content.issue_code,
                )

            if content.kind != "archive":
                continue
            counters.nested_archives += 1
            if depth >= config.max_depth:
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code="archive_depth_limit",
                    detail=f"nested ZIP depth exceeds configured limit {config.max_depth}",
                )
                continue
            try:
                if payload is None:
                    raise RuntimeError("nested ZIP payload was released before traversal")
                remaining_members = config.max_members - budget.members_seen
                if remaining_members < 1:
                    raise ArchiveExtractionError(
                        "archive_member_count_limit",
                        "no member budget remains for nested ZIP traversal",
                    )
                inspect_zip_bytes(
                    payload,
                    max_members=remaining_members,
                    max_central_directory_bytes=config.max_central_directory_bytes,
                )
                with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                    from neocortex.runtime.control.global_resources import current_resource_grant

                    parent_grant = current_resource_grant() if _coordinated_archive_gate() else None
                    if parent_grant is not None:
                        parent_grant.release_cpu()
                    try:
                        _walk_zip(
                            connection,
                            nested,
                            snapshot,
                            container_key,
                            prefix=f"{member_chain}!/",
                            depth=depth + 1,
                            budget=budget,
                            counters=counters,
                            config=config,
                            run_id=run_id,
                            cancellation=cancellation,
                            component_of=component_chain,
                        )
                    finally:
                        if parent_grant is not None:
                            parent_grant.checkpoint()
            except (
                ArchiveExtractionError,
                OSError,
                RuntimeError,
                ZipStructureError,
                zipfile.BadZipFile,
                zlib.error,
            ) as exc:
                code = exc.code if isinstance(exc, ArchiveExtractionError) else "archive_nested_corrupt"
                _record_issue(
                    connection,
                    container_key,
                    counters,
                    member_chain=member_chain,
                    depth=depth,
                    code=code,
                    detail=f"{type(exc).__name__}: {exc}"[:2_000],
                )

    if own_logical_document and logical_document is not None:
        if _coordinated_archive_gate() is not None:
            from neocortex.runtime.control.global_resources import current_resource_grant

            final_grant = current_resource_grant()
            if final_grant is not None:
                final_grant.checkpoint(drain=True)
        # The root is a physical file-backed logical document, not a fabricated
        # ZIP entry. Depth/chain/role explicitly distinguish it from members.
        root_info = zipfile.ZipInfo(Path(snapshot.path).name)
        root_info.file_size = root_info.compress_size = snapshot.size
        root_info.CRC = 0  # no member CRC exists for the physical root
        text = "\n".join(logical_text_parts)[: config.max_total_text_chars]
        kind = logical_document.logical_kind or "archive"
        _observe_member(
            connection,
            snapshot,
            container_key,
            "",
            "",
            0,
            root_info,
            _ExtractedContent(
                text or None,
                kind,
                LOGICAL_MEDIA_TYPES.get(kind, ARCHIVE_MIME),
                "logical_document_projection; integrity=not_verified; opening=not_verified",
            ),
            config.processing_signature,
            run_id,
            document_role="logical_document",
            logical_document_chain="",
        )


def _publish_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    container_key: str,
    counters: _ContainerCounters,
    run_id: int,
) -> None:
    status_value = "partial" if counters.coverage_issues else "complete"
    connection.execute(
        """UPDATE containers SET status=?,member_count=?,indexed_count=?,
        metadata_only_count=?,nested_archive_count=?,issue_count=?,text_chars=?,
        max_depth=?,error_type=NULL,error_message=NULL,retryable=0,
        last_seen_run_id=?,updated_ns=? WHERE container_key=?""",
        (
            status_value,
            counters.members,
            counters.indexed,
            counters.metadata_only,
            counters.nested_archives,
            counters.issues,
            counters.text_chars,
            counters.max_depth,
            run_id,
            time.time_ns(),
            container_key,
        ),
    )


def _store_container_error(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
    run_id: int,
    failure: ArchiveExtractionError,
) -> None:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    if (
        connection.execute(
            "SELECT 1 FROM containers WHERE container_key=?", (container_key,)
        ).fetchone()
        is not None
    ):
        _delete_container(connection, container_key)
    connection.execute(
        """INSERT INTO containers(
        container_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
        error_type,error_message,retryable,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,'error',?,?,?,?,?)""",
        (
            container_key,
            snapshot.path,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
            failure.code,
            str(failure)[:2_000],
            int(failure.retryable),
            run_id,
            time.time_ns(),
        ),
    )
    _record_issue(
        connection,
        container_key,
        _ContainerCounters(),
        member_chain=None,
        depth=0,
        code=failure.code,
        detail=str(failure),
    )
    connection.execute(
        "UPDATE containers SET issue_count=1 WHERE container_key=?", (container_key,)
    )


def _cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
) -> sqlite3.Row | None:
    cached = connection.execute(
        """SELECT path,status,member_count,indexed_count,metadata_only_count,
        nested_archive_count,issue_count,text_chars,max_depth,retryable
        FROM containers WHERE container_key=? AND size=? AND mtime_ns=?
        AND birthtime_ns=? AND processing_signature=?""",
        (
            file_key_from_snapshot(snapshot),
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            signature,
        ),
    ).fetchone()
    if cached is not None and str(cached["status"]) not in {"complete", "partial", "error"}:
        return None
    if (
        cached is not None
        and Path(cached["path"]).suffix.casefold() != Path(snapshot.path).suffix.casefold()
        and connection.execute(
            """SELECT 1 FROM archive_logical_documents
            WHERE container_key=? AND member_chain=''""",
            (file_key_from_snapshot(snapshot),),
        ).fetchone()
        is not None
    ):
        # A user rename may resolve (or introduce) the root extension mismatch,
        # even though content identity and the extraction signature stayed equal.
        return None
    return cached


def _cached_archive_text(row: sqlite3.Row, *, max_text_chars: int) -> str:
    """Load one persisted member representation without opening its ZIP source."""

    compressed = row["text_zlib"]
    try:
        text_chars = int(row["text_chars"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text length is malformed"
        ) from exc
    if text_chars < 0 or text_chars > max_text_chars:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text length exceeds the cache bound"
        )
    digest = row["text_xxh3_128"]
    if compressed is None:
        if str(row["status"]) == "indexed" or text_chars != 0 or digest is not None:
            raise _ArchiveCacheInvalid(
                f"Archive member {row['file_key']} has an incomplete text representation"
            )
        return ""
    try:
        decoder = zlib.decompressobj()
        output_limit = text_chars * 4 + 1
        encoded = decoder.decompress(bytes(compressed), output_limit)
        if decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
            raise ValueError("compressed representation exceeded its recorded bound")
        text = encoded.decode("utf-8")
    except (TypeError, UnicodeError, ValueError, OverflowError, zlib.error) as exc:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation is unreadable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if len(text) != text_chars or digest is None:
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation metadata is inconsistent"
        )
    if str(digest) != xxhash.xxh3_128_hexdigest(encoded):
        raise _ArchiveCacheInvalid(
            f"Archive member {row['file_key']} text representation fingerprint changed"
        )
    return text


def _cached_archive_fts_rows(
    connection: sqlite3.Connection,
    container_key: str,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    """Materialize expected FTS rows from durable Archive member records."""

    container = connection.execute(
        """SELECT status,member_count,indexed_count,metadata_only_count
        FROM containers WHERE container_key=?""",
        (container_key,),
    ).fetchone()
    if container is None:
        raise _ArchiveCacheInvalid(f"Archive container {container_key} disappeared")
    rows = connection.execute(
        """SELECT file_key,path,container_path,member_chain,content_kind,status,
        text_zlib,text_chars,text_xxh3_128 FROM documents
        WHERE container_key=? ORDER BY member_chain COLLATE NOCASE,file_key""",
        (container_key,),
    ).fetchall()
    if str(container["status"]) in {"complete", "partial"}:
        try:
            member_count = int(container["member_count"])
            indexed_count = int(container["indexed_count"])
            metadata_only_count = int(container["metadata_only_count"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise _ArchiveCacheInvalid(
                f"Archive container {container_key} counters are malformed"
            ) from exc
        member_documents = sum(str(row["member_chain"]) != "" for row in rows)
        if (
            member_documents != member_count
            or indexed_count + metadata_only_count != member_count
        ):
            raise _ArchiveCacheInvalid(
                f"Archive container {container_key} durable member set is incomplete"
            )
    expected: list[tuple[str, str, str, str, str, str, str]] = []
    for row in rows:
        file_key = str(row["file_key"])
        container_path = str(row["container_path"])
        member_chain = str(row["member_chain"])
        path = str(row["path"])
        if path != _virtual_path(container_path, member_chain):
            raise _ArchiveCacheInvalid(
                f"Archive member {file_key} virtual path is inconsistent"
            )
        expected.append(
            (
                file_key,
                path,
                container_path,
                Path(container_path).name,
                member_chain,
                str(row["content_kind"]),
                _cached_archive_text(row, max_text_chars=max_text_chars),
            )
        )
    return tuple(expected)


def _repair_cached_container_fts(
    connection: sqlite3.Connection,
    container_key: str,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> int:
    """Repair only a damaged Archive FTS projection from durable member text."""

    expected = _cached_archive_fts_rows(
        connection,
        container_key,
        max_text_chars=max_text_chars,
    )
    expected_by_key = {row[0]: row for row in expected}
    actual_rows: list[sqlite3.Row] = []
    keys = tuple(row[0] for row in connection.execute(
        "SELECT file_key FROM documents WHERE container_key=?", (container_key,)
    ))
    for offset in range(0, len(keys), 500):
        predicate, parameters = format_fts_key_predicate(
            connection, "document_fts", keys[offset:offset + 500]
        )
        actual_rows.extend(connection.execute(
            f"""SELECT file_key,path,container_path,container_name,member_chain,
            content_kind,body FROM document_fts WHERE {predicate}""", parameters,
        ).fetchall())
    actual_by_key: dict[str, list[tuple[object, ...]]] = {}
    for row in actual_rows:
        actual_by_key.setdefault(str(row["file_key"]), []).append(
            (
                str(row["file_key"]),
                str(row["path"]),
                str(row["container_path"]),
                str(row["container_name"]),
                str(row["member_chain"]),
                str(row["content_kind"]),
                str(row["body"]),
            )
        )
    complete = len(actual_rows) == len(expected) and all(
        actual_by_key.get(file_key) == [expected_row]
        for file_key, expected_row in expected_by_key.items()
    )
    if complete:
        return 0

    delete_format_fts_keys(connection, "document_fts", keys)
    insert_format_fts_rows(
        connection, "document_fts",
        ("file_key", "path", "container_path", "container_name", "member_chain", "content_kind", "body"),
        expected,
    )
    return len(expected)


def _refresh_cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> int:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    now = time.time_ns()
    connection.execute(
        "UPDATE containers SET path=?,last_seen_run_id=?,updated_ns=? WHERE container_key=?",
        (snapshot.path, run_id, now, container_key),
    )
    # A cache hit only changes the physical container path and run marker.  A
    # per-member Python loop used to issue two SQL statements for every member
    # (1,808 members on the current corpus), turning a no-work replay into the
    # dominant Archive route cost.  Keep the same path/FTS contract but let
    # SQLite update the whole container in two bounded set-based statements.
    connection.execute(
        """UPDATE documents SET
            path=? || CASE WHEN member_chain='' THEN '' ELSE '!/' || member_chain END,
            container_path=?,last_seen_run_id=?,updated_ns=?
        WHERE container_key=?""",
        (snapshot.path, snapshot.path, run_id, now, container_key),
    )
    return _repair_cached_container_fts(
        connection,
        container_key,
        max_text_chars=max_text_chars,
    )


def _prune_stale_containers(
    connection: sqlite3.Connection,
    run_id: int,
) -> tuple[int, int]:
    rows = connection.execute(
        "SELECT container_key FROM containers WHERE last_seen_run_id<>?", (run_id,)
    ).fetchall()
    members = sum(_delete_container(connection, str(row[0])) for row in rows)
    return len(rows), members


# endregion [03]


# region [04] Incremental route facade


@dataclass(frozen=True, slots=True)
class _ContainerOutcome:
    status: Literal["complete", "partial", "error"]
    counters: _ContainerCounters


@dataclass(frozen=True, slots=True)
class _ArchiveContainerTask:
    snapshot: FileSnapshot
    config: ArchiveRouteConfig
    group: _ArchiveContainerGroup
    cached: sqlite3.Row | None = None


@dataclass(slots=True)
class _PreparedArchiveContainer:
    snapshot: FileSnapshot
    counters: _ContainerCounters
    spool: _ArchiveObservationSpool | None = None
    failure: ArchiveExtractionError | None = None


def _archive_container_memory(config: ArchiveRouteConfig) -> int:
    return max(
        1,
        config.max_total_uncompressed_bytes
        + min(config.max_total_text_chars * 8, 256 * 1024 * 1024),
    )


def _archive_container_capacity(
    gate, config: ArchiveRouteConfig, *, media_possible: bool = True
) -> int:
    """Leave progress space for a member before admitting another ZIP parent."""

    from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded

    parent = _archive_container_memory(config)
    member = min(config.max_member_bytes, config.max_total_uncompressed_bytes)
    interpreter = 64 * 1024 * 1024
    member_workspace = 4 * 1024 * 1024 + member * 2 + min(
        member, config.max_text_chars
    ) * 12
    child = interpreter + member_workspace
    if media_possible:
        # A ZIP can contain disguised PDF/images: extensions cannot prove
        # that only text will be processed. Leave enough room for one largest
        # bounded native child per parent until contents have been inspected.
        child += (
            config.pdf_worker_memory_bytes * (2 if config.ocr_mode != "never" else 1)
            + member * 2 + config.max_text_chars * 6 + 320 * 1024
        )
    try:
        return gate.worker_capacity(estimated_bytes=parent + child, native_threads=0)
    except MemoryBudgetExceeded:
        # An archive may contain only short text despite a generous safety
        # limit. Allow one parent; check actual member demand before its pool.
        return gate.worker_capacity(max_workers=1, estimated_bytes=parent, native_threads=0)


def _extract_archive_container(task: _ArchiveContainerTask) -> _PreparedArchiveContainer:
    """Extract an independent ZIP to owned typed observations, without SQLite."""

    with task.group.extracting():
        return _extract_archive_container_owned(task)


def _extract_archive_container_owned(task: _ArchiveContainerTask) -> _PreparedArchiveContainer:

    from neocortex.runtime.control.global_resources import current_resource_grant
    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    grant = current_resource_grant()
    context = _ARCHIVE_RESOURCES.get()
    cancellation = current_worker_cancellation() or (
        CancellationToken() if context is None else context[1]
    )
    config, snapshot = task.config, task.snapshot
    counters = _ContainerCounters()
    spool = _ArchiveObservationSpool(config, grant)
    try:
        cancellation.checkpoint()
        _require_current_source(snapshot, "ZIP source changed after inventory")
        with open(native_io_path(snapshot.path), "rb", buffering=0) as source:
            if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                raise ArchiveExtractionError(
                    "archive_source_changed", "ZIP source changed before opening", retryable=True
                )
            inspect_zip_stream(
                source, snapshot.size, max_members=config.max_members,
                max_central_directory_bytes=config.max_central_directory_bytes,
            )
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                _walk_zip(
                    spool, archive, snapshot, file_key_from_snapshot(snapshot),
                    prefix="", depth=1,
                    budget=_WalkBudget(config.max_members, config.max_total_uncompressed_bytes),
                    counters=counters, config=config, run_id=0, cancellation=cancellation,
                )
            if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                raise ArchiveExtractionError(
                    "archive_source_changed", "ZIP source changed during traversal", retryable=True
                )
        _require_current_source(snapshot, "ZIP source path changed during traversal")
        del archive
        if grant is not None:
            # Traversal buffers and nested ZIP objects have gone out of scope.
            # Keep only enough RAM to decode/compress one owned record during
            # publication; the anonymous stream retains its separate charge.
            grant.shrink_transient_bytes(min(
                _archive_container_memory(config),
                4 * 1024 * 1024 + spool.max_record_size * 12,
            ))
        return _PreparedArchiveContainer(snapshot, counters, spool=spool)
    except CancellationRequested:
        spool.close()
        raise
    except ArchiveExtractionError as failure:
        spool.close()
        return _PreparedArchiveContainer(snapshot, counters, failure=failure)
    except (OSError, RuntimeError, ZipStructureError, zipfile.BadZipFile, zlib.error) as exc:
        spool.close()
        corrupt_failure = ArchiveExtractionError(
            "archive_corrupt_container", f"{type(exc).__name__}: {exc}"
        )
        return _PreparedArchiveContainer(snapshot, counters, failure=corrupt_failure)
    except BaseException:
        spool.close()
        raise


def _require_current_source(snapshot: FileSnapshot, message: str) -> None:
    try:
        current = snapshot_path(snapshot.path)
    except OSError as exc:
        raise ArchiveExtractionError(
            "archive_source_changed",
            f"{message}: {type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc
    if not same_snapshot(snapshot, current):
        raise ArchiveExtractionError(
            "archive_source_changed",
            message,
            retryable=True,
        )


class ArchiveRoute:
    def __init__(
        self,
        config: ArchiveRouteConfig,
        framework_state: FrameworkRouteState,
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
        if not isinstance(self.config.retry_recoverable_errors, bool):
            raise ValueError("archive retry_recoverable_errors must be a boolean")
        if not isinstance(self.config.materialize_on_apply, bool):
            raise ValueError("archive materialize_on_apply must be a boolean")
        admission_signature = self.config.member_admission_signature
        if (
            not isinstance(admission_signature, str)
            or not admission_signature
            or admission_signature.strip() != admission_signature
            or len(admission_signature.encode("utf-8")) > 4096
        ):
            raise ValueError("archive member admission signature is invalid")
        if (
            self.config.member_admission is not None
            and admission_signature == ARCHIVE_MEMBER_ADMISSION_DISABLED_SIGNATURE
        ):
            raise ValueError(
                "archive member admission callbacks require an explicit policy signature"
            )
        if self.config.materialize_on_apply and self.config.materialization_directory is not None:
            directory = self.config.materialization_directory
            if not isinstance(directory, Path) or not directory.is_absolute():
                raise ValueError(
                    "archive materialization_directory must be an absolute path"
                )
        positive = {
            "max_depth": self.config.max_depth,
            "max_members": self.config.max_members,
            "max_central_directory_bytes": self.config.max_central_directory_bytes,
            "max_member_bytes": self.config.max_member_bytes,
            "max_total_uncompressed_bytes": self.config.max_total_uncompressed_bytes,
            "max_text_chars": self.config.max_text_chars,
            "max_total_text_chars": self.config.max_total_text_chars,
            "max_compression_ratio": self.config.max_compression_ratio,
            "pdf_max_pages": self.config.pdf_max_pages,
            "pdf_timeout_seconds": self.config.pdf_timeout_seconds,
            "pdf_worker_memory_bytes": self.config.pdf_worker_memory_bytes,
            "ocr_dpi": self.config.ocr_dpi,
            "ocr_max_pages": self.config.ocr_max_pages,
            "ocr_max_render_pixels": self.config.ocr_max_render_pixels,
            "ocr_timeout_seconds": self.config.ocr_timeout_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"archive {name} must be positive")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("archive max_documents must be positive")
        if self.config.ocr_mode not in {"auto", "never", "always"}:
            raise ValueError("archive ocr_mode is invalid")
        if not self.config.ocr_lang.strip():
            raise ValueError("archive ocr_lang must not be blank")

    def _selected_counts(self) -> tuple[int, int, int]:
        pool, eligible = self.framework_state.selected_route_candidate_counts(
            self.run_id,
            ARCHIVE_MIME,
            self.config.max_file_bytes,
            "archive",
            self.config.selection,
        )
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return pool, eligible, selected

    def _memory_admission(self):
        if self.memory_gate is None:
            return nullcontext()
        nested_payloads = min(
            self.config.max_total_uncompressed_bytes,
            self.config.max_member_bytes * self.config.max_depth,
        )
        estimate = nested_payloads + min(
            self.config.max_total_text_chars * 4,
            128 * 1024 * 1024,
        )
        if callable(getattr(self.memory_gate, "worker_capacity", None)):
            # Ordered extraction may retain several sibling payloads. Their
            # total is still bounded by the shared decompression budget.
            estimate += self.config.max_total_uncompressed_bytes - nested_payloads
            return self.memory_gate.admit(
                max(1, estimate), cpu_slots=0, native_threads=0,
                phase="archive-retained", cancellation=self.cancellation,
            )
        return self.memory_gate.admit(max(1, estimate))

    def _materialization_limits(self) -> ArchiveMaterializationLimits:
        """Project route bounds into the apply-only materialization service."""

        return ArchiveMaterializationLimits(
            max_members=self.config.max_members,
            max_member_bytes=self.config.max_member_bytes,
            max_total_uncompressed_bytes=self.config.max_total_uncompressed_bytes,
            max_total_temp_bytes=self.config.max_total_uncompressed_bytes,
            max_depth=self.config.max_depth,
            max_compression_ratio=self.config.max_compression_ratio,
            max_central_directory_bytes=self.config.max_central_directory_bytes,
            timeout_seconds=max(1.0, float(self.config.pdf_timeout_seconds)),
        )

    def _materialization_destination(self, container_key: str) -> Path:
        base = self.config.materialization_directory
        if base is None:
            base = self.config.state_path.parent / "archive-materialized"
        # State-managed output is separated by the immutable container identity;
        # no source basename or member name can escape this directory.
        safe_key = container_key.replace(":", "_")
        return Path(base) / safe_key

    def _record_materialization_manifest(
        self,
        connection: sqlite3.Connection,
        container_key: str,
        counters: _ContainerCounters,
        manifest: ArchiveManifest,
    ) -> None:
        status = str(getattr(manifest, "status", "partial"))
        digest_value = getattr(manifest, "manifest_digest", None)
        counters.materialization_manifest_digest = (
            None if digest_value is None else str(digest_value)
        )
        outputs = tuple(getattr(manifest, "outputs", ()) or ())
        counters.materialization_applied += sum(
            str(getattr(output, "status", "")) == "applied" for output in outputs
        )
        counters.materialization_reused += sum(
            str(getattr(output, "status", "")) == "reused" for output in outputs
        )
        counters.materialization_collisions += sum(
            str(getattr(output, "status", "")) == "collision" for output in outputs
        )
        normalized = bool(getattr(manifest, "container_normalized", False))
        classification = getattr(manifest, "classification", None)
        preserved_unit = bool(getattr(classification, "preserve_as_unit", False))
        if preserved_unit and status == "complete":
            counters.materialization_units_preserved += 1
        if status != "complete" or counters.materialization_collisions:
            counters.materialization_pending += 1
        reason_code = (
            "archive_materialization_complete"
            if status == "complete" and normalized
            else "archive_materialization_unit_preserved"
            if status == "complete" and preserved_unit
            else "archive_materialization_collision"
            if counters.materialization_collisions
            else f"archive_materialization_{status}"
        )
        classification_kind = getattr(classification, "kind", None)
        evidence = {
            "manifest_digest": counters.materialization_manifest_digest,
            "status": status,
            "container_normalized": normalized,
            "classification": None if classification_kind is None else str(classification_kind),
            "outputs": len(outputs),
            "applied": counters.materialization_applied,
            "reused": counters.materialization_reused,
            "collisions": counters.materialization_collisions,
            "source_preserved": True,
            "manifest_path": getattr(manifest, "manifest_path", None),
        }
        _record_issue(
            connection,
            container_key,
            counters,
            member_chain=None,
            depth=0,
            code=reason_code,
            detail=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        )

    def _materialize_container(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        container_key: str,
        counters: _ContainerCounters,
    ) -> None:
        """Materialize to state-managed staging only after explicit apply."""

        if not self.config.materialize_on_apply:
            return
        destination = self._materialization_destination(container_key)
        try:
            materialize_kwargs: dict[str, object] = {
                "apply": True,
                "limits": self._materialization_limits(),
            }
            # Preserve injected/legacy materializers that implement the
            # pre-registration signature, without a risky TypeError retry
            # that could duplicate a partially applied materialization.
            materialize_parameters: Any
            try:
                materialize_parameters = inspect.signature(materialize_archive).parameters
            except (TypeError, ValueError):
                materialize_parameters = {}
            if (
                "scratch_directory" in materialize_parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in materialize_parameters.values()
                )
            ):
                materialize_kwargs["scratch_directory"] = (
                    self.config.state_path.parent
                    / "scratch"
                    / "archive-materialization"
                )
            for name, value in {
                "manifest_directory": self.config.state_path.parent / "archive-manifests",
                "artifact_registry_root": self.config.state_path.parent / "artifacts",
            }.items():
                if name in materialize_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in materialize_parameters.values()
                ):
                    materialize_kwargs[name] = value
            materialize_callable: Any = materialize_archive
            with _archive_work_admission(
                phase="archive-materialize",
                temp_bytes=self.config.max_total_uncompressed_bytes,
            ):
                manifest = materialize_callable(snapshot.path, destination, **materialize_kwargs)
        except CancellationRequested:
            raise
        except Exception as exc:
            counters.materialization_pending += 1
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=None,
                depth=0,
                code="archive_materialization_error",
                detail=json.dumps(
                    {
                        "status": "partial",
                        "error_type": type(exc).__name__,
                        "detail": str(exc)[:1_000],
                        "destination": str(destination),
                        "source_preserved": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            return
        self._record_materialization_manifest(connection, container_key, counters, manifest)

    def _process_container(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
    ) -> _ContainerOutcome:
        counters = _ContainerCounters()
        try:
            _require_current_source(snapshot, "ZIP source changed after inventory")
            with _archive_resource_scope(self.memory_gate, self.cancellation), self._memory_admission():
                try:
                    source_handle = open(native_io_path(snapshot.path), "rb", buffering=0)
                except OSError as exc:
                    raise ArchiveExtractionError(
                        "archive_source_unavailable",
                        f"cannot open ZIP source: {type(exc).__name__}: {exc}",
                        retryable=True,
                    ) from exc
                with source_handle as source:
                    if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                        raise ArchiveExtractionError(
                            "archive_source_changed",
                            "ZIP source changed before it was opened",
                            retryable=True,
                        )
                    inspect_zip_stream(
                        source,
                        snapshot.size,
                        max_members=self.config.max_members,
                        max_central_directory_bytes=(self.config.max_central_directory_bytes),
                    )
                    container_key = _prepare_container(
                        connection,
                        snapshot,
                        self.config.processing_signature,
                        self.run_id,
                    )
                    source.seek(0)
                    with zipfile.ZipFile(source) as archive:
                        _walk_zip(
                            connection,
                            archive,
                            snapshot,
                            container_key,
                            prefix="",
                            depth=1,
                            budget=_WalkBudget(
                                self.config.max_members,
                                self.config.max_total_uncompressed_bytes,
                            ),
                            counters=counters,
                            config=self.config,
                            run_id=self.run_id,
                            cancellation=self.cancellation,
                        )
                    if not stat_matches_snapshot(snapshot, os.fstat(source.fileno())):
                        raise ArchiveExtractionError(
                            "archive_source_changed",
                            "ZIP source changed during traversal",
                            retryable=True,
                        )
                self._materialize_container(connection, snapshot, container_key, counters)
            _require_current_source(snapshot, "ZIP source path changed during traversal")
            _publish_container(
                connection,
                snapshot,
                file_key_from_snapshot(snapshot),
                counters,
                self.run_id,
            )
            return _ContainerOutcome(
                "partial" if counters.coverage_issues else "complete", counters
            )
        except ArchiveExtractionError:
            raise
        except (
            OSError,
            RuntimeError,
            ZipStructureError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
        ) as exc:
            raise ArchiveExtractionError(
                "archive_corrupt_container",
                f"{type(exc).__name__}: {exc}",
            ) from exc

    def _publish_prepared_container(
        self, connection: sqlite3.Connection, prepared: _PreparedArchiveContainer
    ) -> _ContainerOutcome:
        # elastic_map owns the bounded publication CPU admission and retains
        # the container/spool lease until this result has been consumed.
        if prepared.failure is not None:
            raise prepared.failure
        if prepared.spool is None:
            raise RuntimeError("archive container has no observation spool")
        snapshot, counters = prepared.snapshot, prepared.counters
        _require_current_source(snapshot, "ZIP source changed before publication")
        key = _prepare_container(connection, snapshot, self.config.processing_signature, self.run_id)
        replay_counters = _ContainerCounters()
        for observation in prepared.spool.observations():
            self.cancellation.checkpoint()
            if isinstance(observation, _MemberObservation):
                _store_member(
                    connection, snapshot, key, observation.member_chain,
                    observation.member_path, observation.depth, observation.info,
                    observation.content, self.config.processing_signature, self.run_id,
                    document_role=observation.document_role,
                    logical_document_chain=observation.logical_document_chain,
                )
            elif isinstance(observation, _LogicalObservation):
                _store_logical_observation(
                    connection, key, observation.member_chain, observation.observation,
                    name=observation.name, depth=observation.depth,
                    counters=replay_counters, diagnose=False,
                )
            else:
                _record_issue(
                    connection, key, replay_counters, member_chain=observation.member_chain,
                    depth=observation.depth, code=observation.code, detail=observation.detail,
                )
        _require_current_source(snapshot, "ZIP source changed while publishing observations")
        self._materialize_container(connection, snapshot, key, counters)
        _require_current_source(snapshot, "ZIP source changed before container publication")
        _publish_container(connection, snapshot, key, counters, self.run_id)
        return _ContainerOutcome("partial" if counters.coverage_issues else "complete", counters)

    def run(self) -> ArchiveRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        lock_path = self.config.state_path.with_suffix(
            self.config.state_path.suffix + ".route.lock"
        )
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            FrameworkRunLock(lock_path),
            _archive_resource_scope(self.memory_gate, self.cancellation),
        ):
            return self._run_locked()

    def _run_locked(self) -> ArchiveRouteSummary:
        self.cancellation.checkpoint()
        initialize_archive_state(self.config.state_path)
        candidate_pool, eligible, selected_count = self._selected_counts()
        processed = cache_hits = cached_errors = complete = partial = errors = 0
        members = indexed = metadata_only = nested = text_chars = issues = 0
        fts_rows_repaired = 0
        materialization_applied = materialization_reused = 0
        materialization_pending = materialization_collisions = 0
        materialization_units_preserved = 0
        materialization_manifest_digest: str | None = None

        def report(*, finished: bool = False) -> None:
            emit_progress(
                self.progress,
                ProgressEvent(
                    "archive",
                    "extract",
                    "ZIP indexados" if finished else "Indexando ZIP",
                    processed,
                    selected_count,
                    "archivos ZIP",
                    finished,
                    (
                        ProgressMetric("cache_hits", cache_hits),
                        ProgressMetric("members", members),
                        ProgressMetric("nested_archives", nested),
                        ProgressMetric(
                            "materialized",
                            materialization_applied + materialization_reused,
                        ),
                        ProgressMetric("materialization_pending", materialization_pending),
                        ProgressMetric("issues", issues + errors),
                    ),
                ),
            )

        with archive_database(self.config.state_path, create=False) as connection:
            initialize_format_fts_lookup(
                connection, "document_fts", checkpoint=self.cancellation.checkpoint
            )
            iterator = self.framework_state.iter_selected_route_candidates(
                self.run_id,
                ARCHIVE_MIME,
                "archive",
                self.config.selection,
            )
            def selected_candidates():
                selected = 0
                for snapshot in iterator:
                    if selected >= selected_count:
                        break
                    self.cancellation.checkpoint()
                    if (
                        self.config.max_file_bytes is not None
                        and snapshot.size > self.config.max_file_bytes
                    ):
                        continue
                    selected += 1
                    yield snapshot

            def reuse_cached(snapshot):
                nonlocal processed, cache_hits, cached_errors, complete, partial, errors
                nonlocal members, indexed, metadata_only, nested, text_chars, issues
                nonlocal fts_rows_repaired, materialization_applied, materialization_reused
                nonlocal materialization_pending, materialization_collisions
                nonlocal materialization_units_preserved, materialization_manifest_digest
                cached = _cached_container(
                    connection,
                    snapshot,
                    self.config.processing_signature,
                )
                retryable_error = (
                    cached is not None
                    and str(cached["status"]) == "error"
                    and type(cached["retryable"]) is int
                    and cached["retryable"] == 1
                )
                if cached is not None and not (
                    str(cached["status"]) == "error"
                    and (
                        self.config.retry_errors
                        or (self.config.retry_recoverable_errors and retryable_error)
                    )
                ):
                    connection.execute("BEGIN IMMEDIATE")
                    repaired: int | None = None
                    cached_materialization = _ContainerCounters()
                    try:
                        repaired_count = _refresh_cached_container(
                            connection,
                            snapshot,
                            self.run_id,
                            max_text_chars=self.config.max_text_chars,
                        )
                        if self.config.materialize_on_apply and str(cached["status"]) != "error":
                            self._materialize_container(
                                connection,
                                snapshot,
                                file_key_from_snapshot(snapshot),
                                cached_materialization,
                            )
                    except _ArchiveCacheInvalid:
                        # A missing/corrupt derived projection is repairable,
                        # but a corrupt durable representation must go through
                        # the normal bounded extraction path.
                        connection.rollback()
                        cached = None
                    except BaseException:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                        repaired = repaired_count
                    if repaired is not None:
                        if cached is None:
                            raise RuntimeError("Archive cache row disappeared after refresh")
                        fts_rows_repaired += repaired
                        cache_hits += 1
                        status_value = str(cached["status"])
                        cached_errors += int(status_value == "error")
                        complete += int(status_value == "complete")
                        partial += int(status_value == "partial")
                        members += int(cached["member_count"])
                        indexed += int(cached["indexed_count"])
                        metadata_only += int(cached["metadata_only_count"])
                        nested += int(cached["nested_archive_count"])
                        issues += int(cached["issue_count"])
                        text_chars += int(cached["text_chars"])
                        materialization_applied += cached_materialization.materialization_applied
                        materialization_reused += cached_materialization.materialization_reused
                        materialization_pending += cached_materialization.materialization_pending
                        materialization_collisions += cached_materialization.materialization_collisions
                        materialization_units_preserved += (
                            cached_materialization.materialization_units_preserved
                        )
                        materialization_manifest_digest = (
                            cached_materialization.materialization_manifest_digest
                            or materialization_manifest_digest
                        )
                        issues += cached_materialization.issues
                        processed += 1
                        report()
                        return True

                return False

            def uncached_candidates():
                for snapshot in selected_candidates():
                    if not reuse_cached(snapshot):
                        yield snapshot

            def prepared_containers():
                if not callable(getattr(self.memory_gate, "worker_capacity", None)):
                    yield from uncached_candidates()
                    return
                from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map

                group = _ArchiveContainerGroup()
                selected = iter(selected_candidates())
                exhausted = False
                retries: deque[_ArchiveContainerTask] = deque()

                def tasks():
                    nonlocal exhausted
                    while retries or not exhausted:
                        if retries:
                            yield retries.popleft()
                            continue
                        try:
                            snapshot = next(selected)
                        except StopIteration:
                            exhausted = True
                            return
                        # This indexed metadata lookup does not decode a durable
                        # representation. Cache verification/FTS repair is prepare.
                        cached = _cached_container(connection, snapshot, self.config.processing_signature)
                        if cached is not None and str(cached["status"]) == "error" and (
                            self.config.retry_errors or (
                                self.config.retry_recoverable_errors and cached["retryable"] == 1
                            )
                        ):
                            cached = None
                        yield _ArchiveContainerTask(snapshot, self.config, group, cached)

                def prepare(task: _ArchiveContainerTask):
                    if task.cached is None:
                        return task
                    try:
                        if reuse_cached(task.snapshot):
                            return ImmediateResult(None)
                        # Damaged durable cache needs a fresh, larger extraction
                        # admission after releasing its representation lease.
                        return ImmediateResult(replace(task, cached=None))
                    finally:
                        # Cached apply may renew CPU around materialization.
                        # Close those owner contexts here: this preparation
                        # only returns ImmediateResult, so no parser follows.
                        from neocortex.runtime.control.global_resources import current_resource_grant

                        grant = current_resource_grant()
                        if grant is not None:
                            grant.release_cpu()

                def estimate(task: _ArchiveContainerTask):
                    if task.cached is None:
                        return _archive_container_memory(self.config)
                    try:
                        chars = max(0, min(int(task.cached["text_chars"]), self.config.max_total_text_chars))
                        count = max(0, min(int(task.cached["member_count"]), self.config.max_members))
                    except (ValueError, TypeError, OverflowError):
                        return _archive_container_memory(self.config)
                    # Canonical and actual FTS projections coexist; names and
                    # ancestor paths are bounded by the archive naming contract.
                    return 4 * 1024 * 1024 + chars * 16 + count * 96 * 1024

                worker: Callable[
                    [_ArchiveContainerTask], _PreparedArchiveContainer | _ArchiveContainerTask | None
                ] = _extract_archive_container
                while not exhausted or retries:
                    with elastic_map(
                        worker, tasks(), gate=self.memory_gate,
                        capacity=lambda: _archive_container_capacity(self.memory_gate, self.config),
                        estimated_bytes=estimate, prepare=prepare,
                        phase="archive-container", native_threads=0, io_slots=1,
                        cancellation=self.cancellation,
                    ) as results:
                        for result in results:
                            if isinstance(result, _ArchiveContainerTask):
                                retries.append(result)
                            elif result is not None:
                                yield result

            with closing(prepared_containers()) as prepared_results:
                for prepared in prepared_results:
                    snapshot = (
                        prepared.snapshot if isinstance(prepared, _PreparedArchiveContainer)
                        else prepared
                    )
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            outcome = (
                                self._publish_prepared_container(connection, prepared)
                                if isinstance(prepared, _PreparedArchiveContainer)
                                else self._process_container(connection, snapshot)
                            )
                        except ArchiveExtractionError as failure:
                            connection.rollback()
                            connection.execute("BEGIN IMMEDIATE")
                            try:
                                _store_container_error(
                                    connection,
                                    snapshot,
                                    self.config.processing_signature,
                                    self.run_id,
                                    failure,
                                )
                            except BaseException:
                                connection.rollback()
                                raise
                            else:
                                connection.commit()
                            errors += 1
                            issues += 1
                        except BaseException:
                            connection.rollback()
                            raise
                        else:
                            connection.commit()
                            complete += int(outcome.status == "complete")
                            partial += int(outcome.status == "partial")
                            members += outcome.counters.members
                            indexed += outcome.counters.indexed
                            metadata_only += outcome.counters.metadata_only
                            nested += outcome.counters.nested_archives
                            issues += outcome.counters.issues
                            text_chars += outcome.counters.text_chars
                            materialization_applied += outcome.counters.materialization_applied
                            materialization_reused += outcome.counters.materialization_reused
                            materialization_pending += outcome.counters.materialization_pending
                            materialization_collisions += outcome.counters.materialization_collisions
                            materialization_units_preserved += (
                                outcome.counters.materialization_units_preserved
                            )
                            materialization_manifest_digest = (
                                outcome.counters.materialization_manifest_digest
                                or materialization_manifest_digest
                            )
                        processed += 1
                        report()
                    finally:
                        if isinstance(prepared, _PreparedArchiveContainer) and prepared.spool is not None:
                            prepared.spool.close()

            pruned_containers = pruned_members = 0
            if (
                not self.config.selection.active
                and self.config.max_documents is None
                and self.config.max_file_bytes is None
            ):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    pruned_containers, pruned_members = _prune_stale_containers(
                        connection,
                        self.run_id,
                    )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        report(finished=True)
        provenance = self.config.processing_provenance
        return ArchiveRouteSummary(
            candidate_pool=candidate_pool,
            candidates=selected_count,
            skipped_by_size=candidate_pool - eligible,
            skipped_by_count=eligible - selected_count,
            processed=processed,
            cache_hits=cache_hits,
            fts_rows_repaired=fts_rows_repaired,
            cached_errors=cached_errors,
            containers_complete=complete,
            containers_partial=partial,
            errors=errors,
            members_seen=members,
            members_indexed=indexed,
            metadata_only=metadata_only,
            nested_archives=nested,
            text_chars=text_chars,
            safety_issues=issues,
            cache_containers_pruned=pruned_containers,
            cache_members_pruned=pruned_members,
            peak_reserved_bytes=(
                0 if self.memory_gate is None else int(self.memory_gate.peak_reserved_bytes)
            ),
            memory_waits=(0 if self.memory_gate is None else int(self.memory_gate.wait_count)),
            processing_signature=provenance.signature,
            processing_provenance=provenance.manifest,
            materialization_applied=materialization_applied,
            materialization_reused=materialization_reused,
            materialization_pending=materialization_pending,
            materialization_collisions=materialization_collisions,
            materialization_units_preserved=materialization_units_preserved,
            materialization_manifest_digest=materialization_manifest_digest,
        )


# endregion [04]


__all__ = (
    "ARCHIVE_MEMBER_ADMISSION_DISABLED_SIGNATURE",
    "ARCHIVE_MIME",
    "ARCHIVE_ROUTE_VERSION",
    "ArchiveExtractionError",
    "ArchiveMemberAdmission",
    "ArchiveMemberAdmissionContext",
    "ArchiveRoute",
    "ArchiveRouteConfig",
    "ArchiveRouteSummary",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.archive.route"
del _defined_value
