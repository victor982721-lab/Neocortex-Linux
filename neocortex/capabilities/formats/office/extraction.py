"""Bounded OOXML and ODF container extraction."""

from __future__ import annotations

import re
import stat
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from neocortex.platform.zip_safety import ZipStructureError, inspect_zip_structure
from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from .extraction_support import (
    CancellationCheckpoint,
    _ReadBudget,
    _TextAccumulator,
    _extract_part_text,
    _local_name,
)
from .models import (
    MAX_CORE_PROPERTIES_BYTES,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    MAX_XLSX_CELLS,
    MAX_ZIP_MEMBERS,
    ExtractedOfficeDocument,
    OfficeExtractionError,
)
from .xlsx import SharedStringsExtractor, _extract_xlsx, _extract_xlsx_shared_strings


def extract_office_document(
    path: Path,
    format_name: Literal["xlsx", "pptx", "odt"],
    *,
    max_text_chars: int,
    cancellation: CancellationCheckpoint,
    max_xlsx_cells: int = MAX_XLSX_CELLS,
    shared_strings_extractor: SharedStringsExtractor = _extract_xlsx_shared_strings,
) -> ExtractedOfficeDocument:
    """Extract bounded Office text plus typed XLSX cell evidence."""

    cancellation.checkpoint()
    try:
        inspect_zip_structure(path, max_members=MAX_ZIP_MEMBERS)
        with zipfile.ZipFile(path) as archive:
            infos = _validated_members(archive)
            names = {info.filename.casefold(): info for info in infos}
            _validate_required_parts(names, format_name)
            metadata = _core_properties(archive, names, cancellation)
            accumulator = _TextAccumulator(max_text_chars)
            budget = _ReadBudget(MAX_TOTAL_UNCOMPRESSED_BYTES)
            selected = tuple(
                info for info in infos if _is_classification_part(info.filename, format_name)
            )
            if format_name == "xlsx":
                cells = _extract_xlsx(
                    archive,
                    path=path,
                    names=names,
                    selected=selected,
                    accumulator=accumulator,
                    budget=budget,
                    cancellation=cancellation,
                    max_cells=max_xlsx_cells,
                    shared_strings_extractor=shared_strings_extractor,
                )
            else:
                cells = ()
                for info in selected:
                    cancellation.checkpoint()
                    _extract_part_text(
                        archive,
                        info,
                        format_name=format_name,
                        accumulator=accumulator,
                        budget=budget,
                        cancellation=cancellation,
                    )
    except OfficeExtractionError:
        raise
    except (
        ET.ParseError,
        ZipStructureError,
        zipfile.BadZipFile,
        RuntimeError,
        zlib.error,
    ) as exc:
        raise OfficeExtractionError(
            "office_corrupt_container",
            f"{type(exc).__name__}: {exc}",
            recommendation="deletion_candidate",
            retryable=False,
        ) from exc
    document = ExtractedOfficeDocument(
        format=format_name,
        title=metadata.get("title", "") or path.stem,
        author=metadata.get("creator", ""),
        subject=metadata.get("subject", ""),
        text=accumulator.value(),
        part_count=len(selected),
        xlsx_cells=cells,
    )
    cancellation.checkpoint()
    return document


def _validated_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    total = 0
    names: set[str] = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or not name:
            raise OfficeExtractionError(
                "office_unsafe_member_name",
                f"unsafe office member name: {info.filename}",
                recommendation="deletion_candidate",
                retryable=False,
            )
        unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
            raise OfficeExtractionError(
                "office_special_member",
                f"special office member is unsupported: {info.filename}",
                recommendation="manual_review",
                retryable=False,
            )
        folded = name.casefold()
        if folded in names:
            raise OfficeExtractionError(
                "office_duplicate_member",
                f"duplicate office member name: {info.filename}",
                recommendation="deletion_candidate",
                retryable=False,
            )
        names.add(folded)
        if info.flag_bits & 0x1:
            raise OfficeExtractionError(
                "office_encrypted_member",
                f"encrypted office member is unsupported: {info.filename}",
                recommendation="manual_review",
                retryable=False,
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise OfficeExtractionError(
                "office_member_limit",
                f"office member exceeds {MAX_MEMBER_BYTES} bytes: {info.filename}",
                recommendation="manual_review",
                retryable=False,
            )
        total += int(info.file_size)
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise OfficeExtractionError(
                "office_uncompressed_limit",
                "office container exceeds the uncompressed byte limit",
                recommendation="manual_review",
                retryable=False,
            )
    return infos


def _validate_required_parts(
    names: Mapping[str, zipfile.ZipInfo],
    format_name: str,
) -> None:
    required = {
        "xlsx": ("[content_types].xml", "xl/workbook.xml"),
        "pptx": ("[content_types].xml", "ppt/presentation.xml"),
        "odt": ("mimetype", "content.xml"),
    }[format_name]
    missing = tuple(name for name in required if name not in names)
    if missing:
        raise OfficeExtractionError(
            "office_missing_required_part",
            f"office container lacks required parts: {', '.join(missing)}",
            recommendation="deletion_candidate",
            retryable=False,
        )


def _core_properties(
    archive: zipfile.ZipFile,
    names: Mapping[str, zipfile.ZipInfo],
    cancellation: CancellationCheckpoint,
) -> dict[str, str]:
    info = names.get("docprops/core.xml") or names.get("meta.xml")
    if info is None:
        return {}
    cancellation.checkpoint()
    with archive.open(info) as source:
        payload = source.read(MAX_CORE_PROPERTIES_BYTES + 1)
    if len(payload) > MAX_CORE_PROPERTIES_BYTES:
        raise OfficeExtractionError(
            "office_metadata_limit",
            "office metadata exceeds its byte limit",
            recommendation="manual_review",
            retryable=False,
        )
    root = safe_xml_fromstring(payload)
    values: dict[str, str] = {}
    aliases = {
        "title": "title",
        "creator": "creator",
        "initial-creator": "creator",
        "subject": "subject",
        "description": "subject",
    }
    for element in root.iter():
        local = _local_name(element.tag)
        key = aliases.get(local)
        if key and element.text and key not in values:
            values[key] = re.sub(r"\s+", " ", element.text).strip()
    return values


def _is_classification_part(name: str, format_name: str) -> bool:
    lower = name.casefold()
    if format_name == "xlsx":
        return (
            lower in {"xl/sharedstrings.xml", "xl/workbook.xml"}
            or lower.startswith(("xl/worksheets/", "xl/comments", "xl/tables/"))
        ) and lower.endswith(".xml")
    if format_name == "pptx":
        return lower.startswith(
            ("ppt/slides/", "ppt/notesslides/", "ppt/comments/")
        ) and lower.endswith(".xml")
    return lower in {"content.xml", "styles.xml"}
