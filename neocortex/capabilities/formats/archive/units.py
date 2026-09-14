"""Bounded classification of ZIP containers and functional package units.

The Archive route historically treated every readable ZIP as one generic
container.  That is a useful fallback, but it is not safe to apply generic
normalisation to OOXML/ODF/EPUB packages or source projects whose relative
layout is part of their meaning.  This module keeps the decision deliberately
small: structural detection is followed by a bounded read of every member and
XML validation of only the package markers.  It never extracts to the corpus or
authorises a physical effect.
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipStructure,
    ZipStructureError,
    inspect_zip_bytes,
    inspect_zip_structure,
)

from .logical import (
    ODF_MIME_KINDS,
    identify_logical_document,
)


ClassificationKind = Literal[
    "storage_archive",
    "docx",
    "xlsx",
    "pptx",
    "odt",
    "ott",
    "odm",
    "ods",
    "ots",
    "odp",
    "otp",
    "odg",
    "otg",
    "odf",
    "odc",
    "odb",
    "epub",
    "project",
]
ClassificationStatus = Literal[
    "validated",
    "storage",
    "partial",
    "corrupt",
    "password",
    "permission",
    "dependency",
    "timeout",
    "budget",
]

DEFAULT_CLASSIFICATION_MAX_MEMBERS = 20_000
DEFAULT_CLASSIFICATION_MAX_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_CLASSIFICATION_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_CLASSIFICATION_MAX_RATIO = 200.0
MAX_PROJECT_MARKER_BYTES = 2 * 1024 * 1024

_PROJECT_MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "package.json",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "cmakelists.txt",
        "makefile",
        "meson.build",
        "setup.py",
        "setup.cfg",
    }
)
_PROJECT_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".lua",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
    }
)
_OOXML_REQUIRED: dict[str, tuple[str, ...]] = {
    "docx": ("[Content_Types].xml", "word/document.xml"),
    "xlsx": ("[Content_Types].xml", "xl/workbook.xml"),
    "pptx": ("[Content_Types].xml", "ppt/presentation.xml"),
}
_ODF_REQUIRED: dict[str, tuple[str, ...]] = dict.fromkeys(
    ODF_MIME_KINDS.values(), ("mimetype", "content.xml", "META-INF/manifest.xml")
)


@dataclass(frozen=True, slots=True)
class ArchiveUnitClassification:
    """Classification evidence for one bounded ZIP payload.

    ``kind`` is a logical kind for package units and ``storage_archive`` for a
    generic ZIP.  ``unit_kind`` gives the coarser policy class (``office``,
    ``odf``, ``epub``, ``project`` or ``storage_archive``), which is convenient
    for callers that do not need one extension per OOXML/ODF variant.
    """

    kind: ClassificationKind
    status: ClassificationStatus
    unit_kind: str
    evidence: tuple[str, ...] = ()
    member_names: tuple[str, ...] = ()
    functional_members: tuple[str, ...] = ()
    structure: ZipStructure | None = None
    detail: str | None = None
    integrity_verified: bool = False
    opening_verified: bool = False

    @property
    def preserve_as_unit(self) -> bool:
        return self.unit_kind in {"office", "odf", "epub", "project"}

    @property
    def validated(self) -> bool:
        return self.status == "validated" and self.integrity_verified

    @property
    def logical_kind(self) -> str:
        """Compatibility alias for the Archive logical-document vocabulary."""

        return self.kind

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "unit_kind": self.unit_kind,
            "evidence": list(self.evidence),
            "member_names": list(self.member_names),
            "functional_members": list(self.functional_members),
            "detail": self.detail,
            "integrity_verified": self.integrity_verified,
            "opening_verified": self.opening_verified,
        }


def _safe_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        return False
    parts = normalized.rstrip("/").split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _special_member(info: zipfile.ZipInfo) -> bool:
    unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    return bool(file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR})


def _stream_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_member_bytes: int,
    max_total_remaining: int,
    ratio_limit: float,
) -> bytes:
    if info.flag_bits & 0x1:
        raise PermissionError("encrypted ZIP member requires a password")
    if _special_member(info):
        raise PermissionError("special ZIP member cannot be validated")
    if info.compress_type not in {
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    }:
        raise NotImplementedError(f"unsupported ZIP compression method {info.compress_type}")
    if int(info.file_size) > max_member_bytes:
        raise MemoryError("ZIP member exceeds the member budget")
    if int(info.file_size) > max_total_remaining:
        raise MemoryError("ZIP total uncompressed budget exhausted")
    if info.file_size and float(info.file_size) / float(max(1, info.compress_size)) > ratio_limit:
        raise MemoryError("ZIP compression ratio exceeds the safety budget")
    chunks: list[bytes] = []
    actual = 0
    with archive.open(info) as source:
        while chunk := source.read(min(64 * 1024, max_member_bytes - actual + 1)):
            actual += len(chunk)
            if actual > max_member_bytes or actual > max_total_remaining:
                raise MemoryError("ZIP decompression budget exhausted")
            chunks.append(chunk)
    if actual != int(info.file_size):
        raise zipfile.BadZipFile(
            f"member produced {actual} bytes but declares {info.file_size}"
        )
    return b"".join(chunks)


def _package_kind(names: tuple[str, ...], declared_mime: str | None) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Return (logical kind, coarse unit class, required members)."""

    # Use the existing logical detector for exact MIME/OOXML marker policy;
    # duplicate marker names are rejected before this helper is called.
    observation = identify_logical_document(names, declared_mime)
    if observation is not None and observation.identified:
        kind = observation.logical_kind
        if kind is None:
            return None, None, ()
        if kind == "epub":
            return kind, "epub", ("mimetype", "META-INF/container.xml")
        if kind in ODF_MIME_KINDS.values():
            return cast(ClassificationKind, kind), "odf", _ODF_REQUIRED.get(kind, ("mimetype", "content.xml"))
        if kind in {"docx", "xlsx", "pptx"}:
            return kind, "office", _OOXML_REQUIRED[kind]
    # ``mimetype`` values for ODF that the compatibility logical detector may
    # reject are still not generic storage: preserve the package until review.
    if declared_mime is not None and declared_mime in ODF_MIME_KINDS:
        kind = ODF_MIME_KINDS[declared_mime]
        return kind, "odf", _ODF_REQUIRED[kind]
    return None, None, ()


def _project_evidence(names: tuple[str, ...]) -> tuple[str, ...]:
    manifests = sorted(name for name in names if PurePosixPath(name).name.casefold() in _PROJECT_MANIFESTS)
    sources = sorted(
        name
        for name in names
        if PurePosixPath(name).suffix.casefold() in _PROJECT_SOURCE_SUFFIXES
    )
    if manifests and sources:
        return tuple([f"project_manifest:{name}" for name in manifests[:8]] + [f"project_source:{name}" for name in sources[:8]])
    # A repository marker plus source is intentionally weaker than a full
    # project manifest, but enough to preserve relative layout as a unit.
    vcs_markers = sorted(name for name in names if PurePosixPath(name).name.casefold() in {".gitignore", ".gitattributes"})
    if vcs_markers and sources:
        return tuple([f"project_marker:{name}" for name in vcs_markers[:4]] + [f"project_source:{name}" for name in sources[:8]])
    return ()


def _classify_open_archive(
    archive: zipfile.ZipFile,
    structure: ZipStructure,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    ratio_limit: float,
) -> ArchiveUnitClassification:
    infos = tuple(archive.infolist())
    if len(infos) != structure.members:
        return ArchiveUnitClassification(
            "storage_archive", "corrupt", "storage_archive", detail="ZIP entry count changed after preflight", structure=structure
        )
    names = tuple(info.filename for info in infos)
    if any(not _safe_name(name) for name in names):
        return ArchiveUnitClassification(
            "storage_archive", "partial", "storage_archive", ("unsafe_member_name",), names, detail="unsafe member name present", structure=structure
        )
    if len({name.casefold() for name in names}) != len(names):
        duplicate_evidence = tuple(
            f"duplicate_name:{name}" for name in sorted({name for name in names if names.count(name) > 1})[:16]
        )
    else:
        duplicate_evidence = ()
    mimetype_infos = [info for info in infos if info.filename == "mimetype"]
    declared: str | None = None
    payloads: dict[str, bytes] = {}
    total = 0
    try:
        for info in infos:
            if info.is_dir():
                continue
            payload = _stream_member(
                archive,
                info,
                max_member_bytes=max_member_bytes,
                max_total_remaining=max_total_bytes - total,
                ratio_limit=ratio_limit,
            )
            total += len(payload)
            # Package marker contents are retained only while this bounded
            # classification call is active; no payload is persisted here.
            if info.filename in {
                "mimetype",
                "[Content_Types].xml",
                "word/document.xml",
                "xl/workbook.xml",
                "ppt/presentation.xml",
                "content.xml",
                "META-INF/manifest.xml",
                "META-INF/container.xml",
            } and len(payload) <= MAX_PROJECT_MARKER_BYTES:
                payloads[info.filename] = payload
        if len(mimetype_infos) > 1:
            return ArchiveUnitClassification(
                "storage_archive", "partial", "storage_archive", (*duplicate_evidence, "duplicate_mimetype"), names, structure=structure, detail="duplicate mimetype members"
            )
        if mimetype_infos:
            declared = payloads.get("mimetype", b"").decode("ascii", "strict")
            if not declared or any(char.isspace() for char in declared):
                raise ValueError("mimetype is empty or contains whitespace")
    except PermissionError as exc:
        message = str(exc).casefold()
        status: ClassificationStatus = "password" if "encrypt" in message or "password" in message else "permission"
        return ArchiveUnitClassification(
            "storage_archive", status, "storage_archive", (), names, (), structure=structure, detail=str(exc)
        )
    except MemoryError as exc:
        return ArchiveUnitClassification("storage_archive", "budget", "storage_archive", duplicate_evidence, names, detail=str(exc), structure=structure)
    except NotImplementedError as exc:
        return ArchiveUnitClassification("storage_archive", "dependency", "storage_archive", duplicate_evidence, names, detail=str(exc), structure=structure)
    except (OSError, UnicodeError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        return ArchiveUnitClassification("storage_archive", "corrupt", "storage_archive", duplicate_evidence, names, detail=f"{type(exc).__name__}: {exc}", structure=structure)

    if duplicate_evidence and (
        "mimetype" in names
        or "[Content_Types].xml" in names
        or any(marker in names for required in _OOXML_REQUIRED.values() for marker in required[1:])
    ):
        # Duplicate package markers cannot be validated as one functional
        # unit.  Generic storage archives may still be complete: each entry is
        # separately accounted for by ordinal/header offset.
        return ArchiveUnitClassification(
            "storage_archive",
            "partial",
            "storage_archive",
            duplicate_evidence,
            names,
            structure=structure,
            detail="duplicate names prevent package identity",
        )
    kind, unit_kind, required = _package_kind(names, declared)
    if kind is not None and unit_kind is not None:
        logical_kind = cast(ClassificationKind, kind)
        missing = tuple(name for name in required if name not in names)
        if missing:
            return ArchiveUnitClassification(
                logical_kind,
                "partial",
                unit_kind,
                tuple(f"missing:{name}" for name in missing),
                names,
                required,
                structure=structure,
                detail="functional package is incomplete",
            )
        # Every required marker was read above.  Parse XML markers before
        # claiming a validated package; this is not an application-open proof.
        try:
            for marker in required:
                if marker in {"mimetype", "META-INF/manifest.xml"}:
                    continue
                if marker in payloads and marker.endswith(".xml"):
                    safe_xml_fromstring(payloads[marker])
        except (ET.ParseError, UnicodeError, ValueError) as exc:
            return ArchiveUnitClassification(logical_kind, "partial", unit_kind, ("marker_xml_invalid",), names, required, structure=structure, detail=str(exc))
        return ArchiveUnitClassification(
            logical_kind, "validated", unit_kind, tuple([f"required:{name}" for name in required] + ([f"declared_mime:{declared}"] if declared else [])), names, required, structure=structure, integrity_verified=True, opening_verified=False
        )
    project_evidence = _project_evidence(names)
    if project_evidence:
        return ArchiveUnitClassification(
            "project", "validated", "project", project_evidence, names, tuple(names), structure=structure, integrity_verified=True, opening_verified=False
        )
    return ArchiveUnitClassification(
        "storage_archive",
        "storage",
        "storage_archive",
        (*duplicate_evidence, "no_functional_package_markers"),
        names,
        tuple(names),
        structure=structure,
        integrity_verified=True,
        opening_verified=False,
    )


def classify_archive_bytes(
    payload: bytes | bytearray | memoryview,
    *,
    max_members: int = DEFAULT_CLASSIFICATION_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_CLASSIFICATION_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_CLASSIFICATION_MAX_TOTAL_BYTES,
    max_compression_ratio: float = DEFAULT_CLASSIFICATION_MAX_RATIO,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ArchiveUnitClassification:
    """Classify one bounded in-memory ZIP without extracting it to disk."""

    try:
        structure = inspect_zip_bytes(
            payload,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return _classify_open_archive(
                archive,
                structure,
                max_member_bytes=max_member_bytes,
                max_total_bytes=max_total_bytes,
                ratio_limit=max_compression_ratio,
            )
    except PermissionError as exc:
        return ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail=str(exc))
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        return ArchiveUnitClassification("storage_archive", "corrupt", "storage_archive", detail=f"{type(exc).__name__}: {exc}")


def classify_archive(
    source: str | os.PathLike[str],
    *,
    max_members: int = DEFAULT_CLASSIFICATION_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_CLASSIFICATION_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_CLASSIFICATION_MAX_TOTAL_BYTES,
    max_compression_ratio: float = DEFAULT_CLASSIFICATION_MAX_RATIO,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ArchiveUnitClassification:
    """Classify a filesystem ZIP through bounded preflight and member reads."""

    path = Path(source)
    try:
        structure = inspect_zip_structure(
            path,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )
        with zipfile.ZipFile(path) as archive:
            return _classify_open_archive(
                archive,
                structure,
                max_member_bytes=max_member_bytes,
                max_total_bytes=max_total_bytes,
                ratio_limit=max_compression_ratio,
            )
    except PermissionError as exc:
        return ArchiveUnitClassification("storage_archive", "permission", "storage_archive", detail=str(exc))
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        return ArchiveUnitClassification("storage_archive", "corrupt", "storage_archive", detail=f"{type(exc).__name__}: {exc}")


# Descriptive aliases make the service easy to consume from callers that use
# the older ``logical_document`` vocabulary.
classify_zip = classify_archive
classify_zip_bytes = classify_archive_bytes


__all__ = (
    "ArchiveUnitClassification",
    "ClassificationKind",
    "ClassificationStatus",
    "classify_archive",
    "classify_archive_bytes",
    "classify_zip",
    "classify_zip_bytes",
)
