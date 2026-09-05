"""Incremental, bounded indexing of ZIP members, including nested ZIP files."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import stat
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Literal

import xxhash

from neocortex.deduplication import FileSnapshot
from neocortex.deduplication.fingerprinting import snapshot_path, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from neocortex.workflow.actions.action_policy import same_snapshot
from neocortex.runtime.control.bounded_subprocess import (
    SubprocessOutputLimitError,
    run_bounded_capture,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_bytes,
    inspect_zip_stream,
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
from .models import ArchiveRouteSummary
from .state import archive_database, initialize_archive_state


# region [01] Public route contract and safety defaults


ARCHIVE_MIME = "application/zip"
ARCHIVE_ROUTE_VERSION = "archive-route-v2"
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


@dataclass(frozen=True, slots=True)
class ArchiveRouteConfig:
    state_path: Path
    max_file_bytes: int | None = None
    max_documents: int | None = None
    retry_errors: bool = False
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
    root = ET.fromstring(payload)
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
            names = {info.filename.replace("\\", "/").casefold() for info in archive.infolist()}
            if "[content_types].xml" in names:
                if any(name.startswith("word/") for name in names):
                    return "docx"
                if any(name.startswith("xl/") for name in names):
                    return "xlsx"
                if any(name.startswith("ppt/") for name in names):
                    return "pptx"
            if "mimetype" in names:
                try:
                    value = archive.read("mimetype").decode("ascii", "strict")
                except (KeyError, OSError, UnicodeError, RuntimeError):
                    value = ""
                return {
                    "application/vnd.oasis.opendocument.text": "odt",
                    "application/vnd.oasis.opendocument.spreadsheet": "ods",
                    "application/vnd.oasis.opendocument.presentation": "odp",
                    "application/epub+zip": "epub",
                }.get(value, "archive")
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
    if kind in {"odt", "ods", "odp"}:
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
    try:
        completed = run_bounded_capture(
            command,
            input_bytes=payload,
            timeout_seconds=config.pdf_timeout_seconds,
            stdout_limit_bytes=max(64 * 1024, char_limit * 6 + 64 * 1024),
            stderr_limit_bytes=256 * 1024,
            environment={
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            },
            memory_limit_bytes=config.pdf_worker_memory_bytes,
        )
    except (OSError, RuntimeError, SubprocessOutputLimitError) as exc:
        return None, False, f"{kind}_worker_error:{type(exc).__name__}", None
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
        except (ArchiveExtractionError, ZipStructureError, zipfile.BadZipFile, zlib.error) as exc:
            return _ExtractedContent(
                None,
                zip_kind,
                f"application/{zip_kind}",
                f"{type(exc).__name__}: {exc}"[:500],
                "archive_embedded_document_error",
            )
        return _ExtractedContent(
            text or None,
            zip_kind,
            f"application/{zip_kind}",
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
    text_chars: int = 0
    max_depth: int = 0


def _member_key(container_key: str, member_chain: str) -> str:
    digest = xxhash.xxh3_128_hexdigest(
        f"{container_key}\x00{member_chain}".encode("utf-8", "surrogatepass")
    )
    return f"archive:{digest}"


def _virtual_path(container_path: str, member_chain: str) -> str:
    return f"{container_path}!/{member_chain}"


def _delete_container(connection: sqlite3.Connection, container_key: str) -> int:
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM documents WHERE container_key=?", (container_key,)
        ).fetchone()[0]
    )
    connection.execute(
        "DELETE FROM document_fts WHERE file_key IN "
        "(SELECT file_key FROM documents WHERE container_key=?)",
        (container_key,),
    )
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
    connection: sqlite3.Connection,
    container_key: str,
    counters: _ContainerCounters,
    *,
    member_chain: str | None,
    depth: int,
    code: str,
    detail: str,
) -> None:
    counters.issues += 1
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
        text_xxh3_128,detail,error_type,error_message,last_seen_run_id,updated_ns)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        ),
    )
    connection.execute(
        "INSERT INTO document_fts(file_key,path,container_path,container_name,"
        "member_chain,content_kind,body) VALUES(?,?,?,?,?,?,?)",
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


def _walk_zip(
    connection: sqlite3.Connection,
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
) -> None:
    seen_names: set[str] = set()
    for info in archive.infolist():
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
            return
        counters.max_depth = max(counters.max_depth, depth)
        try:
            name = _normalized_member_name(info)
        except ArchiveExtractionError as exc:
            _record_issue(
                connection,
                container_key,
                counters,
                member_chain=None,
                depth=depth,
                code=exc.code,
                detail=str(exc),
            )
            continue
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
            continue
        seen_names.add(name)
        if info.is_dir():
            continue
        counters.members += 1
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
        elif not budget.can_read(int(info.file_size)):
            content = _metadata_content(
                detail="member would exceed the total decompression budget",
                issue_code="archive_total_uncompressed_limit",
            )
        else:
            try:
                payload = _read_zip_member(
                    archive,
                    info,
                    budget=budget,
                    max_bytes=config.max_member_bytes,
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
                suffix = PurePosixPath(name).suffix.casefold()
                zip_kind = (
                    _zip_document_kind(
                        payload,
                        max_central_directory_bytes=config.max_central_directory_bytes,
                    )
                    if payload.startswith(_ZIP_MAGIC_PREFIXES)
                    or suffix in _NESTED_ARCHIVE_EXTENSIONS
                    else None
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
                    remaining_text = config.max_total_text_chars - counters.text_chars
                    if remaining_text < 1:
                        content = _metadata_content(
                            detail="container text budget is exhausted",
                            issue_code="archive_total_text_limit",
                        )
                    else:
                        content = _extract_member_content(
                            name,
                            payload,
                            zip_kind=zip_kind,
                            char_limit=min(config.max_text_chars, remaining_text),
                            budget=budget,
                            config=config,
                        )

        _store_member(
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
        )
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
                )
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


def _publish_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    container_key: str,
    counters: _ContainerCounters,
    run_id: int,
) -> None:
    status_value = "partial" if counters.issues else "complete"
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


def _cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    signature: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT status,member_count,indexed_count,metadata_only_count,
        nested_archive_count,issue_count,text_chars,max_depth
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


def _refresh_cached_container(
    connection: sqlite3.Connection,
    snapshot: FileSnapshot,
    run_id: int,
) -> None:
    container_key = file_key_from_snapshot(snapshot)
    conflict = connection.execute(
        "SELECT container_key FROM containers WHERE path=? AND container_key<>?",
        (snapshot.path, container_key),
    ).fetchone()
    if conflict is not None:
        _delete_container(connection, str(conflict[0]))
    rows = connection.execute(
        "SELECT file_key,member_chain FROM documents WHERE container_key=?",
        (container_key,),
    ).fetchall()
    now = time.time_ns()
    connection.execute(
        "UPDATE containers SET path=?,last_seen_run_id=?,updated_ns=? WHERE container_key=?",
        (snapshot.path, run_id, now, container_key),
    )
    for row in rows:
        file_key = str(row["file_key"])
        path = _virtual_path(snapshot.path, str(row["member_chain"]))
        connection.execute(
            "UPDATE documents SET path=?,container_path=?,last_seen_run_id=?,updated_ns=? "
            "WHERE file_key=?",
            (path, snapshot.path, run_id, now, file_key),
        )
        connection.execute(
            "UPDATE document_fts SET path=?,container_path=?,container_name=? WHERE file_key=?",
            (path, snapshot.path, Path(snapshot.path).name, file_key),
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
        return self.memory_gate.admit(max(1, estimate))

    def _process_container(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
    ) -> _ContainerOutcome:
        counters = _ContainerCounters()
        try:
            _require_current_source(snapshot, "ZIP source changed after inventory")
            with self._memory_admission():
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
            _require_current_source(snapshot, "ZIP source path changed during traversal")
            _publish_container(
                connection,
                snapshot,
                file_key_from_snapshot(snapshot),
                counters,
                self.run_id,
            )
            return _ContainerOutcome("partial" if counters.issues else "complete", counters)
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

    def run(self) -> ArchiveRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        initialize_archive_state(self.config.state_path)
        candidate_pool, eligible, selected_count = self._selected_counts()
        processed = cache_hits = cached_errors = complete = partial = errors = 0
        members = indexed = metadata_only = nested = text_chars = issues = 0

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
                        ProgressMetric("issues", issues + errors),
                    ),
                ),
            )

        with archive_database(self.config.state_path, create=False) as connection:
            iterator = self.framework_state.iter_selected_route_candidates(
                self.run_id,
                ARCHIVE_MIME,
                "archive",
                self.config.selection,
            )
            for snapshot in iterator:
                if processed >= selected_count:
                    break
                self.cancellation.checkpoint()
                if (
                    self.config.max_file_bytes is not None
                    and snapshot.size > self.config.max_file_bytes
                ):
                    continue
                cached = _cached_container(
                    connection,
                    snapshot,
                    self.config.processing_signature,
                )
                if cached is not None and not (
                    str(cached["status"]) == "error" and self.config.retry_errors
                ):
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _refresh_cached_container(connection, snapshot, self.run_id)
                    except BaseException:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                    cache_hits += 1
                    status_value = str(cached["status"])
                    cached_errors += int(status_value == "error")
                    complete += int(status_value == "complete")
                    partial += int(status_value == "partial")
                    errors += int(status_value == "error")
                    members += int(cached["member_count"])
                    indexed += int(cached["indexed_count"])
                    metadata_only += int(cached["metadata_only_count"])
                    nested += int(cached["nested_archive_count"])
                    issues += int(cached["issue_count"])
                    text_chars += int(cached["text_chars"])
                    processed += 1
                    report()
                    continue

                connection.execute("BEGIN IMMEDIATE")
                try:
                    outcome = self._process_container(connection, snapshot)
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
                processed += 1
                report()

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
        )


# endregion [04]


__all__ = (
    "ARCHIVE_MIME",
    "ARCHIVE_ROUTE_VERSION",
    "ArchiveExtractionError",
    "ArchiveRoute",
    "ArchiveRouteConfig",
    "ArchiveRouteSummary",
)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.archive.route"
del _defined_value
