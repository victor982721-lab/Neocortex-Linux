"""Bounded ZIP member inspection and content extraction primitives.

This module owns only member-level safety and text derivation.  The route
facade supplies the process-backed media worker so tests and callers retain
its dependency boundary.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
import zlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructureError,
    inspect_zip_bytes,
)

from .logical import (
    LOGICAL_MEDIA_TYPES,
    MAX_DECLARED_MIME_BYTES,
    ODF_KINDS,
    LogicalDocumentEvidence,
    identify_logical_document,
)
from .contracts import ArchiveExtractionError, DEFAULT_MAX_MEMBER_BYTES

# Only used as a type marker in deferred annotations; the route owns the config.
ArchiveRouteConfig = Any

ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
MAX_MEMBER_NAME_CHARS = 2_048
MAX_MEMBER_SEGMENT_CHARS = 255
MAX_EMBEDDED_DOCUMENT_MEMBERS = 20_000
MAX_EMBEDDED_XML_BYTES = 64 * 1024 * 1024

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
                config=__import__("neocortex.capabilities.formats.archive.route", fromlist=["ArchiveRouteConfig"]).ArchiveRouteConfig(Path("unused"), ocr_mode="never"),
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
    media_extractor: Callable[..., tuple[str | None, bool, str | None, str | None]] | None = None,
) -> _ExtractedContent:
    extractor = media_extractor
    if extractor is None:
        raise ArchiveExtractionError(
            "archive_media_extractor_missing",
            "Archive media extraction requires the route-owned worker callback",
        )
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
        pdf_text, truncated, detail, extraction_mode = extractor(
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
        image_text, truncated, detail, extraction_mode = extractor(
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
