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
import csv
import json
import math
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from neocortex.deduplication.io import absolute_display_path, native_io_path

from .zip_safety import ZipStructureError, inspect_zip_structure
# endregion [01]

# region [02] Implementación


HEADER_LIMIT = 64 * 1024
ZIP_MEMBER_LIMIT = 4096
ZIP_STRUCTURE_MEMBER_LIMIT = 10_000
ZIP_MIMETYPE_LIMIT = 256
DETECTOR_VERSION = "content-types-v5"

# Media parsers below intentionally operate on the already bounded prefix read
# by ``detect_content_type``.  These limits keep malformed container metadata
# from turning Identify into an unbounded parser while still covering the
# small headers emitted by local media encoders.
MAX_MEDIA_CHUNKS = 4096
MAX_ASF_HEADER_OBJECTS = 4096
MAX_AMR_FRAMES = 4096
_AMR_NB_FRAME_BYTES = (12, 13, 15, 17, 19, 20, 26, 31, 5)

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

_HTML_DOCTYPE = re.compile(r"(?is)<!doctype\s+html(?:\s|>)")
_HTML_TAG = re.compile(r"(?is)<(?:html|head|body)\b")
_MARKDOWN_MARKER = re.compile(
    r"(?m)^(?:#{1,6}\s+\S|[-*+]\s+\S|>\s+\S|```|\[[^\]]+\]\([^\)]+\))"
)


@dataclass(frozen=True, slots=True)
class DetectedType:
    mime: str
    canonical_extension: str
    accepted_extensions: frozenset[str]
    evidence: str

    def accepts(self, path: str | Path) -> bool:
        return Path(path).suffix.casefold() in self.accepted_extensions


@dataclass(frozen=True, slots=True)
class FileTypeDecision:
    """One bounded, extension-independent identification decision.

    ``DetectedType`` remains the compact compatibility payload consumed by the
    existing route/cache contracts.  This value adds the physical path and an
    explicit status for the Identify stage without making callers infer that
    ``None`` means ``UNKNOWN``.  Unknown decisions deliberately carry no
    proposed extension.
    """

    path: str
    detected_type: DetectedType | None
    mime: str | None
    canonical_extension: str | None
    accepted_extensions: frozenset[str]
    confidence: Literal["high", "medium", "low", "none"]
    evidence: str
    status: Literal["known", "unknown"]

    @property
    def detected(self) -> DetectedType | None:
        """Compatibility/readability alias for the compact detected value."""

        return self.detected_type

    @property
    def kind(self) -> str | None:
        """Return a stable logical kind without adding a second taxonomy."""

        if self.canonical_extension is None:
            return None
        return self.canonical_extension.removeprefix(".")

    def accepts(self, path: str | Path | None = None) -> bool:
        """Whether the observed extension is already valid for this decision."""

        if self.status != "known":
            return False
        candidate = self.path if path is None else path
        return Path(candidate).suffix.casefold() in self.accepted_extensions


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


def _detect_adts(header: bytes) -> DetectedType | None:
    """Identify a bounded raw AAC/ADTS stream without using its suffix.

    The MPEG audio sync word is shared by several families, so the ADTS layer
    bits, sampling-frequency index, and a complete non-empty frame are checked
    before returning ``audio/aac``.  Only the prefix already owned by Identify
    is inspected; no decoder or subprocess is involved.
    """

    if len(header) < 7:
        return None
    offset = 0
    valid_frames = 0
    while valid_frames < 2 and len(header) - offset >= 7:
        first = header[offset]
        second = header[offset + 1]
        # Twelve sync bits, layer == 0; ID and CRC-present remain variable.
        if first != 0xFF or second & 0xF6 != 0xF0:
            break
        protection_absent = second & 0x01
        header_size = 7 if protection_absent else 9
        third = header[offset + 2]
        sampling_frequency_index = (third >> 2) & 0x0F
        if sampling_frequency_index >= 13:
            return None
        frame_length = (
            ((header[offset + 3] & 0x03) << 11)
            | (header[offset + 4] << 3)
            | (header[offset + 5] >> 5)
        )
        # An ADTS frame must contain payload bytes in addition to its header.
        if frame_length < header_size + 2 or frame_length > len(header) - offset:
            return None
        valid_frames += 1
        offset += frame_length
        if offset == len(header):
            break
    if valid_frames == 0:
        return None
    return _type("audio/aac", ".aac", (".aac",), "adts:aac")


def _aiff_sample_rate(header: bytes, offset: int) -> float | None:
    """Decode the bounded positive 80-bit AIFF sample-rate field."""

    if offset + 10 > len(header):
        return None
    exponent = struct.unpack_from(">H", header, offset)[0]
    mantissa = int.from_bytes(header[offset + 2 : offset + 10], "big")
    if exponent & 0x8000 or not mantissa:
        return None
    try:
        value = math.ldexp(mantissa / float(1 << 63), (exponent & 0x7FFF) - 16383)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) and 0 < value <= 384_000 else None


def _detect_aiff(header: bytes) -> DetectedType | None:
    """Identify structurally valid bounded AIFF/AIFC containers."""

    if len(header) < 12 or header[:4] != b"FORM" or header[8:12] not in {b"AIFF", b"AIFC"}:
        return None
    form_size = struct.unpack_from(">I", header, 4)[0]
    if form_size < 4:
        return None
    container_end = form_size + 8
    available_end = min(len(header), container_end)
    offset = 12
    chunks = 0
    saw_comm = False
    saw_ssnd = False
    while offset + 8 <= available_end and chunks < MAX_MEDIA_CHUNKS:
        chunk_id = header[offset : offset + 4]
        chunk_size = struct.unpack_from(">I", header, offset + 4)[0]
        data_start = offset + 8
        chunk_end = data_start + chunk_size
        padded_end = chunk_end + (chunk_size & 1)
        if chunk_end > container_end or padded_end > container_end:
            return None
        if chunk_id == b"COMM":
            if saw_comm or chunk_size < 18 or data_start + 18 > len(header):
                return None
            channels, frames, sample_size = struct.unpack_from(">H I H", header, data_start)
            if not (
                0 < channels <= 256
                and frames > 0
                and 0 < sample_size <= 64
                and _aiff_sample_rate(header, data_start + 8) is not None
            ):
                return None
            if header[8:12] == b"AIFC":
                if chunk_size < 22 or data_start + 22 > len(header):
                    return None
                compression = header[data_start + 18 : data_start + 22]
                if not all(0x20 <= value < 0x7F for value in compression):
                    return None
            saw_comm = True
        elif chunk_id == b"SSND":
            if saw_ssnd or chunk_size < 8:
                return None
            saw_ssnd = True
        chunks += 1
        if padded_end > len(header):
            # A valid SSND header may precede payload beyond the bounded
            # prefix; no later structural evidence is available to inspect.
            break
        offset = padded_end
        if offset >= container_end:
            break
    if not (saw_comm and saw_ssnd):
        return None
    return _type("audio/x-aiff", ".aiff", (".aiff", ".aif", ".aifc"), "iff:aiff")


def _detect_caf(header: bytes) -> DetectedType | None:
    """Identify a bounded Core Audio Format stream from its ``desc`` chunk."""

    if len(header) < 8 or header[:4] != b"caff":
        return None
    version, _flags = struct.unpack_from(">HH", header, 4)
    if version not in {1, 2}:
        return None
    offset = 8
    chunks = 0
    saw_desc = False
    saw_data = False
    while offset + 12 <= len(header) and chunks < MAX_MEDIA_CHUNKS:
        chunk_id = header[offset : offset + 4]
        chunk_size = struct.unpack_from(">Q", header, offset + 4)[0]
        data_start = offset + 12
        if chunk_id == b"desc":
            if saw_desc or chunk_size != 32 or data_start + 32 > len(header):
                return None
            sample_rate = struct.unpack_from(">d", header, data_start)[0]
            format_id = header[data_start + 8 : data_start + 12]
            bytes_per_packet, frames_per_packet, channels, bits = struct.unpack_from(
                ">IIII", header, data_start + 16
            )
            if not (
                math.isfinite(sample_rate)
                and 0 < sample_rate <= 384_000
                and all(0x20 <= value < 0x7F for value in format_id)
                and 0 < channels <= 256
                and bits <= 64
                and frames_per_packet > 0
                and bytes_per_packet <= 0x7FFFFFFF
            ):
                return None
            saw_desc = True
        elif chunk_id == b"data":
            if saw_data:
                return None
            # CAF data starts with a four-byte edit-count field.  The payload
            # may continue past HEADER_LIMIT, but its bounded header is enough
            # once its declared size is structurally valid.
            if chunk_size != 0xFFFFFFFFFFFFFFFF and chunk_size < 4:
                return None
            if data_start + 4 > len(header):
                return None
            saw_data = True
        if chunk_size == 0xFFFFFFFFFFFFFFFF:
            break
        chunk_end = data_start + chunk_size
        if chunk_end > len(header):
            break
        chunks += 1
        offset = chunk_end
    if not (saw_desc and saw_data):
        return None
    return _type("audio/x-caf", ".caf", (".caf",), "caf:desc")


_ASF_HEADER_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ASF_STREAM_PROPERTIES_GUID = bytes.fromhex("9107dcb7b7a9cf118ee600c00c205365")
_ASF_AUDIO_STREAM_GUID = bytes.fromhex("409e69f84d5bcf11a8fd00805f5c442b")
_ASF_VIDEO_STREAM_GUID = bytes.fromhex("c0ef19bc4d5bcf11a8fd00805f5c442b")
_WMA_FORMAT_TAGS = frozenset({0x000A, 0x0160, 0x0161, 0x0162, 0x0163})


def _detect_wma(header: bytes) -> DetectedType | None:
    """Identify an audio-only ASF header carrying a WMA stream."""

    if len(header) < 30 or header[:16] != _ASF_HEADER_GUID:
        return None
    header_size = struct.unpack_from("<Q", header, 16)[0]
    object_count = struct.unpack_from("<I", header, 24)[0]
    if (
        header_size < 30
        or header_size > len(header)
        or object_count == 0
        or object_count > MAX_ASF_HEADER_OBJECTS
        or header[28:30] != b"\x01\x02"
    ):
        return None
    offset = 30
    saw_audio = False
    saw_video = False
    for _ in range(object_count):
        if offset + 24 > header_size:
            return None
        object_guid = header[offset : offset + 16]
        object_size = struct.unpack_from("<Q", header, offset + 16)[0]
        if object_size < 24 or offset + object_size > header_size:
            return None
        payload_start = offset + 24
        payload_size = object_size - 24
        if object_guid == _ASF_STREAM_PROPERTIES_GUID:
            if payload_size < 54:
                return None
            stream_guid = header[payload_start : payload_start + 16]
            type_specific_size = struct.unpack_from("<I", header, payload_start + 40)[0]
            error_correction_size = struct.unpack_from("<I", header, payload_start + 44)[0]
            type_specific_start = payload_start + 54
            type_specific_end = type_specific_start + type_specific_size
            error_correction_end = type_specific_end + error_correction_size
            if error_correction_end > offset + object_size:
                return None
            if stream_guid == _ASF_AUDIO_STREAM_GUID:
                if type_specific_size < 16:
                    return None
                format_tag, channels, sample_rate, _avg_bytes, block_align, bits = (
                    struct.unpack_from("<HHIIHH", header, type_specific_start)
                )
                if not (
                    format_tag in _WMA_FORMAT_TAGS
                    and 0 < channels <= 256
                    and 0 < sample_rate <= 384_000
                    and block_align > 0
                    and bits <= 64
                ):
                    return None
                saw_audio = True
            elif stream_guid == _ASF_VIDEO_STREAM_GUID:
                saw_video = True
        offset += object_size
    if offset != header_size or not saw_audio or saw_video:
        return None
    return _type("audio/x-ms-wma", ".wma", (".wma", ".asf"), "asf:wma")


def _detect_amr(header: bytes, *, complete: bool = True) -> DetectedType | None:
    """Identify complete bounded AMR-NB file frames after the file magic.

    ``#!AMR\n`` alone is only a container label, not evidence of valid audio.
    The detector therefore validates each available TOC byte and its bounded
    frame length.  An incomplete final frame is accepted only when the input
    is the 64 KiB prefix of a larger file; a complete short file must end on a
    frame boundary.  AMR-WB is intentionally not mapped to ``audio/amr``.
    """

    magic = b"#!AMR\n"
    if not header.startswith(magic) or len(header) == len(magic):
        return None
    offset = len(magic)
    frames = 0
    while offset < len(header) and frames < MAX_AMR_FRAMES:
        toc = header[offset]
        if toc & 0x03:
            return None
        frame_type = (toc >> 3) & 0x0F
        if frame_type >= len(_AMR_NB_FRAME_BYTES):
            return None
        payload_size = _AMR_NB_FRAME_BYTES[frame_type]
        frame_end = offset + 1 + payload_size
        if frame_end > len(header):
            return None if complete or frames == 0 else _type(
                "audio/amr", ".amr", (".amr",), "magic:amr-nb"
            )
        frames += 1
        offset = frame_end
        if frames == MAX_AMR_FRAMES:
            # The bounded prefix has enough independently valid frames to
            # identify the stream; do not reject longer valid recordings just
            # because the parser's evidence sample is intentionally finite.
            return _type("audio/amr", ".amr", (".amr",), "magic:amr-nb")
    if frames == 0 or offset != len(header):
        return None
    return _type("audio/amr", ".amr", (".amr",), "magic:amr-nb")


def _detect_ebml_video(header: bytes) -> DetectedType | None:
    """Identify bounded Matroska/WebM EBML headers by their declared DocType.

    Matroska-derived containers share the four-byte EBML signature.  The
    signature alone is not enough to call arbitrary EBML data video, so this
    detector also requires the DocType element (0x4282) and one bounded ASCII
    value.  A one-byte EBML size is sufficient for the only accepted values
    (``webm`` and ``matroska``) and avoids implementing a permissive container
    parser at the content-routing boundary.  WebM remains a video route
    candidate even when the container happens to carry only an audio track;
    the audio route intentionally accepts that shared MIME as well.
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


def _detect_media(header: bytes, *, complete: bool = True) -> DetectedType | None:
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
    aiff = _detect_aiff(header)
    if aiff is not None:
        return aiff
    caf = _detect_caf(header)
    if caf is not None:
        return caf
    wma = _detect_wma(header)
    if wma is not None:
        return wma
    amr = _detect_amr(header, complete=complete)
    if amr is not None:
        return amr
    adts = _detect_adts(header)
    if adts is not None:
        return adts
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


def _detect_rfc822(header: bytes) -> bool:
    """Require a bounded RFC822 header block, not merely an ``.eml`` suffix."""

    separator = re.search(rb"\r?\n\r?\n", header)
    header_block = header if separator is None else header[: separator.start()]
    matches = _RFC5322_HEADER.findall(header_block)
    if len(matches) < 2:
        return False
    names = {
        match.split(b":", 1)[0].decode("ascii", "ignore").casefold()
        for match in matches
    }
    return bool(names & {"from", "to", "date", "subject", "message-id", "mime-version"})


def _detect_json(value: str, encoding: str, *, complete: bool) -> DetectedType | None:
    """Parse one complete JSON value or a bounded JSON-lines sample."""

    if not complete:
        return None
    stripped = value.lstrip("\ufeff \t\r\n")
    if not stripped:
        return None

    def reject_non_standard_constant(_value: str) -> object:
        raise ValueError("non-standard JSON constant")

    try:
        json.loads(stripped, parse_constant=reject_non_standard_constant)
    except (TypeError, ValueError):
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        # JSON-lines is intentionally limited to record-shaped values here;
        # scalar prose such as ``1\n2`` is too ambiguous to justify a rename.
        if not all(line.startswith(("{", "[")) for line in lines):
            return None
        try:
            for line in lines:
                json.loads(line, parse_constant=reject_non_standard_constant)
        except (TypeError, ValueError):
            return None
        return _type(
            "application/json",
            ".json",
            (".json", ".jsonl"),
            f"text:{encoding}:jsonl",
        )
    return _type(
        "application/json",
        ".json",
        (".json", ".jsonl"),
        f"text:{encoding}:json",
    )


def _detect_xml(value: str, encoding: str, *, complete: bool) -> DetectedType | None:
    """Parse bounded XML while rejecting entity-bearing payloads."""

    if not complete:
        return None
    stripped = value.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("<"):
        return None
    if re.search(r"(?is)<!doctype\b|<!entity\b", stripped):
        return None
    try:
        root = ElementTree.fromstring(stripped)
    except (ElementTree.ParseError, ValueError):
        return None
    if not root.tag or not isinstance(root.tag, str):
        return None
    return _type(
        "application/xml",
        ".xml",
        (".xml", ".rels"),
        f"text:{encoding}:xml",
    )


def _detect_html(value: str, encoding: str, *, complete: bool) -> DetectedType | None:
    """Recognize strong HTML structure without requiring an HTML suffix."""

    if not complete:
        return None
    stripped = value.lstrip("\ufeff \t\r\n")
    if not (
        _HTML_DOCTYPE.search(stripped)
        or (
            _HTML_TAG.search(stripped)
            and re.search(r"(?is)</(?:html|body|head)\s*>", stripped)
        )
    ):
        return None
    return _type("text/html", ".html", (".html", ".htm"), f"text:{encoding}:html")


def _detect_delimited(
    value: str,
    suffix: str,
    encoding: str,
    *,
    complete: bool,
) -> DetectedType | None:
    """Detect only consistently shaped, bounded CSV/TSV data."""

    if not complete:
        return None
    lines = value.splitlines()
    if len(lines) < 2:
        return None
    candidates: list[str] = []
    for delimiter in (",", "\t"):
        try:
            rows = list(csv.reader(lines, delimiter=delimiter, strict=True))
        except (csv.Error, TypeError, ValueError):
            continue
        widths = {len(row) for row in rows}
        if len(widths) != 1 or not widths or next(iter(widths)) < 2:
            continue
        if any(not any(cell.strip() for cell in row) for row in rows):
            continue
        if sum(line.count(delimiter) for line in lines) < 2:
            continue
        candidates.append(delimiter)

    if not candidates:
        return None
    preferred = "," if suffix == ".csv" else "\t" if suffix == ".tsv" else None
    selected = [item for item in candidates if item == preferred] if preferred else candidates
    if len(selected) != 1:
        # A suffix can disambiguate two otherwise valid delimiters, but content
        # alone must not invent a type when both grammars fit.
        return None
    delimiter = selected[0]
    if delimiter == ",":
        return _type("text/csv", ".csv", (".csv",), f"text:{encoding}:csv")
    return _type(
        "text/tab-separated-values",
        ".tsv",
        (".tsv",),
        f"text:{encoding}:tsv",
    )


def _detect_text(
    path: str,
    header: bytes,
    *,
    complete: bool = True,
) -> DetectedType | None:
    suffix = Path(path).suffix.casefold()
    decoded = _text_decoding(header)
    if decoded is None:
        return None
    value, encoding = decoded
    stripped = value.lstrip("\ufeff \t\r\n")
    if _detect_rfc822(header):
        return _type("message/rfc822", ".eml", (".eml",), "rfc5322:headers")

    # Structured text is parsed before the suffix is consulted.  A suffix may
    # select between equally valid CSV/TSV grammars, but never gates JSON/XML/
    # HTML/RFC822 identification.
    for detector in (
        lambda: _detect_json(value, encoding, complete=complete),
        lambda: _detect_html(value, encoding, complete=complete),
        lambda: _detect_xml(value, encoding, complete=complete),
        lambda: _detect_delimited(value, suffix, encoding, complete=complete),
    ):
        detected = detector()
        if detected is not None:
            return detected

    if suffix in {".md", ".rst", ".adoc"}:
        return _type(
            "text/markdown",
            ".md",
            (".md", ".rst", ".adoc"),
            f"text:{encoding}:markup",
        )
    if _MARKDOWN_MARKER.search(stripped):
        return _type(
            "text/markdown",
            ".md",
            (".md", ".rst", ".adoc"),
            f"text:{encoding}:markup-structure",
        )
    if suffix not in _TEXT_EXTENSIONS:
        return None
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
    detected = _detect_document_or_image(header)
    if detected is not None:
        return detected
    detected_media = _detect_media(header, complete=len(header) < HEADER_LIMIT)
    if detected_media is not None:
        return detected_media
    archive = _detect_archive(native, header)
    if archive is not None:
        return archive
    database_or_executable = _detect_database_or_executable(native, header)
    if database_or_executable is not None:
        return database_or_executable
    return _detect_text(native, header, complete=len(header) < HEADER_LIMIT)


def _decision_confidence(detected: DetectedType | None) -> Literal["high", "medium", "low", "none"]:
    if detected is None:
        return "none"
    if detected.evidence.startswith(("magic:", "riff:", "isobmff:", "ebml:", "zip:")):
        return "high"
    if detected.evidence.endswith((":json", ":jsonl", ":xml", ":html", ":csv", ":tsv")):
        return "high"
    if detected.evidence == "rfc5322:headers" or detected.evidence.endswith(":markup-structure"):
        return "medium"
    return "low"


def identify(path: str | Path) -> FileTypeDecision:
    """Return an explicit bounded Identify-stage decision for ``path``."""

    resolved = absolute_display_path(path)
    detected = detect_content_type(resolved)
    if detected is None:
        return FileTypeDecision(
            path=resolved,
            detected_type=None,
            mime=None,
            canonical_extension=None,
            accepted_extensions=frozenset(),
            confidence="none",
            evidence="unknown",
            status="unknown",
        )
    return FileTypeDecision(
        path=resolved,
        detected_type=detected,
        mime=detected.mime,
        canonical_extension=detected.canonical_extension,
        accepted_extensions=detected.accepted_extensions,
        confidence=_decision_confidence(detected),
        evidence=detected.evidence,
        status="known",
    )


# endregion [02]
