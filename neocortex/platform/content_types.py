"""Bounded, signature-based detection of common file formats.
# region [00] Contexto del módulo
# Módulo canónico: neocortex/platform/content_types.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The detector deliberately returns ``None`` when the available bytes are not
strong enough evidence.  Guessing from an extension would defeat validation.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from neocortex.deduplication.io import absolute_display_path, native_io_path

from .zip_safety import ZipStructureError, inspect_zip_structure
# endregion [01]

# region [02] Implementación


HEADER_LIMIT = 64 * 1024
ZIP_MEMBER_LIMIT = 4096
ZIP_STRUCTURE_MEMBER_LIMIT = 10_000
ZIP_MIMETYPE_LIMIT = 256
DETECTOR_VERSION = "content-types-v3"

_TEXT_EXTENSIONS = frozenset(
    {
        "",
        ".adoc",
        ".bash",
        ".bat",
        ".before",
        ".c",
        ".cfg",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".directory",
        ".env",
        ".example",
        ".gitignore",
        ".go",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".local",
        ".log",
        ".lua",
        ".md",
        ".php",
        ".properties",
        ".ps1",
        ".py",
        ".r",
        ".rb",
        ".rels",
        ".rs",
        ".rst",
        ".service",
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
_RFC5322_HEADER = re.compile(
    rb"(?im)^(?:from|to|date|subject|message-id|mime-version):[^\r\n]+\r?$"
)


@dataclass(frozen=True, slots=True)
class DetectedType:
    mime: str
    canonical_extension: str
    accepted_extensions: frozenset[str]
    evidence: str

    def accepts(self, path: str | Path) -> bool:
        return Path(path).suffix.casefold() in self.accepted_extensions


def _type(mime: str, canonical: str, accepted: tuple[str, ...], evidence: str) -> DetectedType:
    return DetectedType(
        mime,
        canonical,
        frozenset(extension.casefold() for extension in accepted),
        evidence,
    )


def _detect_zip(path: str | Path) -> DetectedType:
    """Distinguish common ZIP container formats without extracting content."""

    try:
        inspect_zip_structure(path, max_members=ZIP_STRUCTURE_MEMBER_LIMIT)
        with zipfile.ZipFile(path) as archive:
            name_counts: dict[str, int] = {}
            for index, info in enumerate(archive.infolist()):
                if index >= ZIP_MEMBER_LIMIT:
                    break
                name_counts[info.filename] = name_counts.get(info.filename, 0) + 1

            # Package signatures are exact member names, not directory hints.
            # A normal ZIP can contain a ``word/`` directory or a copied XML
            # fragment without being an OOXML document.  Duplicate or competing
            # package markers remain a conventional/ambiguous ZIP so a caller
            # cannot select a route from an arbitrary path-looking member.
            content_types_count = name_counts.get("[Content_Types].xml", 0)
            ooxml_markers = {
                "word/document.xml": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".docx",
                    (".docx", ".dotx", ".docm", ".dotm"),
                    "zip:ooxml-word",
                ),
                "xl/workbook.xml": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ".xlsx",
                    (".xlsx", ".xltx", ".xlsm", ".xltm"),
                    "zip:ooxml-excel",
                ),
                "ppt/presentation.xml": (
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    ".pptx",
                    (".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm"),
                    "zip:ooxml-powerpoint",
                ),
            }
            present_ooxml = tuple(name for name in ooxml_markers if name_counts.get(name, 0))

            mimetype_present = "mimetype" in name_counts
            mimetype_read = False
            value = ""
            if mimetype_present and name_counts["mimetype"] == 1:
                try:
                    with archive.open("mimetype") as member:
                        value = member.read(ZIP_MIMETYPE_LIMIT).decode("ascii", "strict")
                    mimetype_read = True
                except (KeyError, OSError, UnicodeError, RuntimeError):
                    pass

            if mimetype_present:
                # A declared/duplicate/unreadable MIME must not be overridden
                # by a second, inferred package signature.
                if present_ooxml:
                    return _type("application/zip", ".zip", (".zip",), "zip:ambiguous-package")
                open_formats = {
                    "application/vnd.oasis.opendocument.text": (
                        ".odt",
                        (".odt", ".ott"),
                    ),
                    "application/vnd.oasis.opendocument.spreadsheet": (
                        ".ods",
                        (".ods", ".ots"),
                    ),
                    "application/vnd.oasis.opendocument.presentation": (
                        ".odp",
                        (".odp", ".otp"),
                    ),
                    # OTT has no dedicated runtime route; retain the archive
                    # owner while exposing its canonical logical extension so
                    # validation never proposes a misleading ``.zip``.
                    "application/vnd.oasis.opendocument.text-template": (
                        ".ott",
                        (".ott",),
                    ),
                    "application/epub+zip": (".epub", (".epub",)),
                }
                if mimetype_read and value in open_formats:
                    canonical, accepted = open_formats[value]
                    mime = (
                        "application/zip"
                        if value == "application/vnd.oasis.opendocument.text-template"
                        else value
                    )
                    evidence = (
                        "zip:odf-template" if value.endswith("text-template") else "zip:mimetype"
                    )
                    return _type(mime, canonical, accepted, evidence)

                if present_ooxml or content_types_count > 1:
                    return _type("application/zip", ".zip", (".zip",), "zip:ambiguous-package")

            if content_types_count == 1 and len(present_ooxml) == 1:
                marker = present_ooxml[0]
                if name_counts[marker] == 1:
                    mime, canonical, accepted, evidence = ooxml_markers[marker]
                    return _type(mime, canonical, accepted, evidence)

            if content_types_count and present_ooxml:
                return _type("application/zip", ".zip", (".zip",), "zip:ambiguous-package")

            folded_names = {name.replace("\\", "/").casefold() for name in name_counts}
            if "androidmanifest.xml" in folded_names:
                return _type(
                    "application/vnd.android.package-archive",
                    ".apk",
                    (".apk",),
                    "zip:android-manifest",
                )
            if "meta-inf/manifest.mf" in folded_names:
                return _type("application/java-archive", ".jar", (".jar",), "zip:java-manifest")
    except (
        OSError,
        RuntimeError,
        ZipStructureError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ):
        pass
    return _type("application/zip", ".zip", (".zip",), "magic:zip")


def _detect_iso_bmff(header: bytes) -> DetectedType | None:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return None
    brands = {header[8:12]}
    brands.update(header[offset : offset + 4] for offset in range(16, min(len(header), 64), 4))
    if brands & {b"avif", b"avis"}:
        return _type("image/avif", ".avif", (".avif",), "isobmff:avif")
    if brands & {
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"heim",
        b"heis",
        b"mif1",
        b"msf1",
    }:
        return _type("image/heic", ".heic", (".heic", ".heif"), "isobmff:heif")
    if brands & {b"M4A ", b"M4B ", b"M4P "}:
        return _type("audio/mp4", ".m4a", (".m4a", ".m4b", ".mp4"), "isobmff:m4a")
    if brands & {b"qt  "}:
        return _type("video/quicktime", ".mov", (".mov", ".qt"), "isobmff:quicktime")
    return _type("video/mp4", ".mp4", (".mp4", ".m4v"), "isobmff:mp4")


def _detect_ebml_video(header: bytes) -> DetectedType | None:
    """Identify bounded Matroska/WebM EBML headers by their declared DocType.

    Matroska-derived containers share the four-byte EBML signature.  The
    signature alone is not enough to call arbitrary EBML data video, so this
    detector also requires the DocType element (0x4282) and one bounded ASCII
    value.  A one-byte EBML size is sufficient for the only accepted values
    (``webm`` and ``matroska``) and avoids implementing a permissive container
    parser at the content-routing boundary.
    """

    if not header.startswith(b"\x1aE\xdf\xa3"):
        return None
    marker = b"\x42\x82"
    offset = header.find(marker, 4)
    if offset < 0 or offset + len(marker) + 1 > len(header):
        return None
    size_marker = header[offset + len(marker)]
    if size_marker & 0x80 == 0:
        return None
    size = size_marker & 0x7F
    if size not in {4, 8}:
        return None
    start = offset + len(marker) + 1
    end = start + size
    if end > len(header):
        return None
    doc_type = header[start:end]
    if doc_type == b"webm":
        return _type("video/webm", ".webm", (".webm",), "ebml:doctype:webm")
    if doc_type == b"matroska":
        return _type(
            "video/x-matroska",
            ".mkv",
            (".mkv", ".mka", ".mks", ".mk3d"),
            "ebml:doctype:matroska",
        )
    return None


def _detect_pe(path: str | Path, header: bytes) -> DetectedType:
    canonical = ".exe"
    accepted = (".exe", ".dll", ".sys", ".scr", ".cpl", ".ocx")
    evidence = "magic:dos-executable"
    if len(header) >= 64:
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        try:
            with open(path, "rb", buffering=0) as stream:
                stream.seek(pe_offset)
                pe_header = stream.read(24)
            if pe_header[:4] == b"PE\0\0" and len(pe_header) >= 24:
                characteristics = struct.unpack_from("<H", pe_header, 22)[0]
                if characteristics & 0x2000:
                    canonical = ".dll"
                evidence = "magic:pe"
        except OSError:
            pass
    return _type("application/vnd.microsoft.portable-executable", canonical, accepted, evidence)


def _detect_document_or_image(header: bytes) -> DetectedType | None:
    if header.startswith(b"%PDF-"):
        return _type("application/pdf", ".pdf", (".pdf",), "magic:pdf")
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return _type("image/png", ".png", (".png",), "magic:png")
    if header.startswith(b"\xff\xd8\xff"):
        return _type("image/jpeg", ".jpg", (".jpg", ".jpeg", ".jpe"), "magic:jpeg")
    if header.startswith((b"GIF87a", b"GIF89a")):
        return _type("image/gif", ".gif", (".gif",), "magic:gif")
    if header.startswith((b"II*\x00", b"MM\x00*")):
        if header[8:10] == b"CR":
            return _type("image/x-canon-cr2", ".cr2", (".cr2",), "magic:cr2")
        return _type(
            "image/tiff",
            ".tif",
            (".tif", ".tiff", ".dng", ".nef", ".arw"),
            "magic:tiff",
        )
    if (
        len(header) >= 26
        and header.startswith(b"BM")
        and header[6:10] == b"\0\0\0\0"
        and struct.unpack_from("<I", header, 10)[0] >= 14
        and struct.unpack_from("<I", header, 14)[0] in {12, 40, 52, 56, 64, 108, 124}
    ):
        return _type("image/bmp", ".bmp", (".bmp", ".dib"), "magic:bmp")
    if header.startswith(b"8BPS"):
        return _type("image/vnd.adobe.photoshop", ".psd", (".psd",), "magic:psd")
    if len(header) >= 6 and header.startswith(b"\x00\x00\x01\x00") and header[4:6] != b"\0\0":
        return _type("image/x-icon", ".ico", (".ico",), "magic:ico")
    if len(header) >= 6 and header.startswith(b"\x00\x00\x02\x00") and header[4:6] != b"\0\0":
        return _type("image/x-win-bitmap", ".cur", (".cur",), "magic:cursor")
    return None


def _detect_media(header: bytes) -> DetectedType | None:
    if len(header) >= 12 and header[:4] == b"RIFF":
        if header[8:12] == b"WEBP":
            return _type("image/webp", ".webp", (".webp",), "riff:webp")
        if header[8:12] == b"WAVE":
            return _type("audio/wav", ".wav", (".wav", ".wave"), "riff:wave")
        if header[8:12] == b"AVI ":
            return _type("video/x-msvideo", ".avi", (".avi",), "riff:avi")
    bmff = _detect_iso_bmff(header)
    if bmff is not None:
        return bmff
    ebml = _detect_ebml_video(header)
    if ebml is not None:
        return ebml
    if header.startswith(b"fLaC"):
        return _type("audio/flac", ".flac", (".flac",), "magic:flac")
    if header.startswith(b"OggS"):
        return _type("application/ogg", ".ogg", (".ogg", ".oga", ".ogv", ".opus"), "magic:ogg")
    mpeg_header = int.from_bytes(header[:4], "big") if len(header) >= 4 else 0
    valid_mpeg_frame = (
        mpeg_header >> 21 == 0x7FF
        and (mpeg_header >> 19) & 0x3 != 0x1
        and (mpeg_header >> 17) & 0x3 != 0
        and (mpeg_header >> 12) & 0xF not in {0, 0xF}
        and (mpeg_header >> 10) & 0x3 != 0x3
    )
    if header.startswith(b"ID3") or valid_mpeg_frame:
        return _type("audio/mpeg", ".mp3", (".mp3",), "magic:mpeg-audio")
    return None


def _detect_archive(path: str, header: bytes) -> DetectedType | None:
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _detect_zip(path)
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return _type("application/x-7z-compressed", ".7z", (".7z",), "magic:7z")
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return _type("application/vnd.rar", ".rar", (".rar",), "magic:rar")
    if header.startswith(b"\x1f\x8b"):
        return _type("application/gzip", ".gz", (".gz", ".gzip"), "magic:gzip")
    if header.startswith(b"BZh"):
        return _type("application/x-bzip2", ".bz2", (".bz2", ".bzip2"), "magic:bzip2")
    if header.startswith(b"\xfd7zXZ\x00"):
        return _type("application/x-xz", ".xz", (".xz",), "magic:xz")
    return None


def _detect_database_or_executable(
    path: str,
    header: bytes,
) -> DetectedType | None:
    if header.startswith(b"SQLite format 3\x00"):
        return _type(
            "application/vnd.sqlite3",
            ".sqlite3",
            (".sqlite", ".sqlite3", ".db"),
            "magic:sqlite",
        )
    if header.startswith(b"MZ"):
        return _detect_pe(path, header)
    if header.startswith(b"\x7fELF"):
        return _type("application/x-elf", ".elf", (".elf", ".so"), "magic:elf")
    if header.startswith(b"\x4c\x00\x00\x00\x01\x14\x02\x00"):
        return _type("application/x-ms-shortcut", ".lnk", (".lnk",), "magic:lnk")
    return None


def _text_decoding(header: bytes) -> tuple[str, str] | None:
    encodings = (
        ("utf-32",)
        if header.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        else ("utf-16",)
        if header.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "cp1252")
    )
    for encoding in encodings:
        try:
            value = header.decode(encoding, "strict")
        except UnicodeError:
            continue
        sample = value[:32_768]
        if not sample:
            return None
        printable = sum(character.isprintable() or character.isspace() for character in sample)
        controls = sum(
            ord(character) < 32 and character not in "\b\t\n\f\r\x1b" for character in sample
        )
        if printable / len(sample) >= 0.90 and controls / len(sample) <= 0.01:
            return value, encoding
    return None


def _detect_text(path: str, header: bytes) -> DetectedType | None:
    suffix = Path(path).suffix.casefold()
    if suffix == ".eml" and len(_RFC5322_HEADER.findall(header)) >= 2:
        return _type("message/rfc822", ".eml", (".eml",), "rfc5322:headers")
    if suffix not in _TEXT_EXTENSIONS:
        return None
    decoded = _text_decoding(header)
    if decoded is None:
        return None
    value, encoding = decoded
    stripped = value.lstrip("\ufeff \t\r\n")
    if suffix in {".htm", ".html"} and re.match(r"(?is)<!doctype\s+html|<html\b", stripped):
        return _type("text/html", ".html", (".htm", ".html"), f"text:{encoding}:html")
    if suffix in {".xml", ".rels"} and stripped.startswith(("<?xml", "<")):
        return _type("application/xml", ".xml", (".xml", ".rels"), f"text:{encoding}:xml")
    if suffix in {".json", ".jsonl"} and stripped.startswith(("{", "[")):
        return _type("application/json", ".json", (".json", ".jsonl"), f"text:{encoding}:json")
    if suffix == ".csv":
        return _type("text/csv", ".csv", (".csv",), f"text:{encoding}:csv")
    if suffix == ".tsv":
        return _type(
            "text/tab-separated-values",
            ".tsv",
            (".tsv",),
            f"text:{encoding}:tsv",
        )
    if suffix in {".md", ".rst", ".adoc"}:
        return _type(
            "text/markdown",
            ".md",
            (".md", ".rst", ".adoc"),
            f"text:{encoding}:markup",
        )
    accepted = tuple(sorted(_TEXT_EXTENSIONS))
    return _type("text/plain", ".txt", accepted, f"text:{encoding}:printable")


def detect_content_type(path: str | Path) -> DetectedType | None:
    """Detect a known type from bounded header/container evidence."""

    resolved = absolute_display_path(path)
    native = native_io_path(resolved)
    with open(native, "rb", buffering=0) as stream:
        header = stream.read(HEADER_LIMIT)
    if not header:
        return None
    for detector in (
        _detect_document_or_image,
        _detect_media,
    ):
        detected = detector(header)
        if detected is not None:
            return detected
    archive = _detect_archive(native, header)
    if archive is not None:
        return archive
    database_or_executable = _detect_database_or_executable(native, header)
    if database_or_executable is not None:
        return database_or_executable
    return _detect_text(native, header)


# endregion [02]
