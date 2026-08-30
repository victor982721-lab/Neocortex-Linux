"""Stable DOCX failure taxonomy and bounded ZIP-member recovery helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Callable, Iterable

from neocortex.deduplication import FileChangedError

from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
from neocortex.platform.zip_safety import (
    RawDeflateMember,
    ZipStructureError,
    read_raw_deflate_member,
)
from .models import (
    DocxDiagnostic,
    DocxFailure,
    DocxIntegrityStatus,
    DocxProcessingError,
    DocxReviewDisposition,
)


# region [01] ZIP member boundaries and recovery


def member_upper_bounds(
    infos: Iterable[zipfile.ZipInfo],
    central_directory_offset: int,
) -> dict[int, int]:
    """Return the next structural boundary for every unique local header."""

    offsets = sorted(int(info.header_offset) for info in infos)
    if len(offsets) != len(set(offsets)):
        raise ZipStructureError("ZIP members share a local header offset")
    if any(offset < 0 or offset >= central_directory_offset for offset in offsets):
        raise ZipStructureError("ZIP local header points outside member data")
    boundaries: dict[int, int] = {}
    for index, offset in enumerate(offsets):
        boundaries[offset] = (
            offsets[index + 1] if index + 1 < len(offsets) else central_directory_offset
        )
    return boundaries


def recover_raw_deflate_member(
    path: str | Path,
    info: zipfile.ZipInfo,
    *,
    upper_bound: int,
    max_member_bytes: int,
    checkpoint: Callable[[], None] | None = None,
) -> RawDeflateMember:
    """Recover one raw member under the central and local structural bounds."""

    return read_raw_deflate_member(
        path,
        header_offset=int(info.header_offset),
        compressed_size=int(info.compress_size),
        upper_bound=upper_bound,
        max_compressed_bytes=max_member_bytes,
        max_output_bytes=max_member_bytes,
        checkpoint=checkpoint,
    )


def recovered_member_diagnostic(
    info: zipfile.ZipInfo,
    recovered: RawDeflateMember,
    *,
    stage: str,
    required: bool,
) -> DocxDiagnostic:
    size_matches = recovered.actual_size == int(info.file_size)
    crc_matches = recovered.actual_crc32 == int(info.CRC)
    code = (
        "zip_member_crc_mismatch_recovered"
        if size_matches and not crc_matches
        else "zip_member_metadata_mismatch_recovered"
    )
    message = (
        f"Recovered {info.filename} with validated XML despite inconsistent "
        "ZIP size or CRC metadata"
    )
    return DocxDiagnostic(
        code=code,
        message=message,
        stage=stage,
        part_name=info.filename,
        required=required,
        disposition="manual_review",
        expected_size=int(info.file_size),
        actual_size=recovered.actual_size,
        expected_crc32=int(info.CRC),
        actual_crc32=recovered.actual_crc32,
    )


# endregion [01]


# region [02] Stable member diagnostics


def _is_policy_limit(message: str) -> bool:
    lower = message.casefold()
    return "limit" in lower or "exceed" in lower or "too many" in lower


def diagnostic_for_member(
    info: zipfile.ZipInfo,
    exc: Exception,
    *,
    stage: str,
    required: bool,
) -> DocxDiagnostic:
    """Normalize implementation exceptions without leaking class-name semantics."""

    message = str(exc)[:8192]
    lower = message.casefold()
    retryable = False
    if isinstance(exc, zlib.error):
        code = "zip_member_deflate_corrupt"
    elif isinstance(exc, zipfile.BadZipFile):
        code = "zip_member_crc_mismatch" if "crc" in lower else "zip_local_header_invalid"
    elif isinstance(exc, ET.ParseError):
        code = "ooxml_required_xml_invalid" if required else "ooxml_optional_xml_invalid"
    elif isinstance(exc, NotImplementedError):
        code = "zip_compression_unsupported"
    elif isinstance(exc, ZipStructureError):
        if "encrypted" in lower:
            code = "zip_encrypted_unsupported"
        elif _is_policy_limit(message):
            code = "policy_member_limit"
        elif "deflate" in lower and "supports only" in lower:
            code = "zip_compression_unsupported"
        else:
            code = "zip_local_header_invalid"
    elif isinstance(exc, RuntimeError) and ("password" in lower or "encrypted" in lower):
        code = "zip_encrypted_unsupported"
    elif isinstance(exc, ValueError) and _is_policy_limit(message):
        code = "policy_text_limit"
    elif isinstance(exc, OSError):
        code = "source_unavailable"
        retryable = True
    else:
        code = "internal_parser_error"
        retryable = True

    disposition: DocxReviewDisposition
    if retryable:
        disposition = "retry"
    elif code in {
        "zip_encrypted_unsupported",
        "zip_compression_unsupported",
        "policy_member_limit",
        "policy_text_limit",
    }:
        disposition = "manual_review"
    elif required:
        disposition = "deletion_candidate"
    else:
        disposition = "manual_review"
    return DocxDiagnostic(
        code=code,
        message=message,
        stage=stage,
        part_name=info.filename,
        required=required,
        retryable=retryable,
        disposition=disposition,
        expected_size=int(info.file_size),
        expected_crc32=int(info.CRC),
    )


def fatal_member_error(
    info: zipfile.ZipInfo,
    exc: Exception,
    *,
    stage: str,
) -> DocxProcessingError:
    diagnostic = diagnostic_for_member(info, exc, stage=stage, required=True)
    integrity: DocxIntegrityStatus
    if diagnostic.retryable:
        integrity = "unavailable"
    elif diagnostic.code.startswith("policy_"):
        integrity = "policy_rejected"
    elif diagnostic.code.endswith("unsupported"):
        integrity = "unsupported"
    else:
        integrity = "corrupt"
    return DocxProcessingError(
        DocxFailure(
            code=diagnostic.code,
            message=diagnostic.message,
            integrity_status=integrity,
            retryable=diagnostic.retryable,
            disposition=diagnostic.disposition,
            diagnostics=(diagnostic,),
        )
    )


# endregion [02]


# region [03] Route-level exception normalization


def classify_docx_exception(exc: Exception) -> DocxFailure:
    """Return a stable retry and review policy for a fatal route exception."""

    if isinstance(exc, DocxProcessingError):
        return exc.failure
    message = str(exc)[:8192]
    lower = message.casefold()
    code: str
    integrity: DocxIntegrityStatus
    retryable: bool
    disposition: DocxReviewDisposition
    if isinstance(exc, MemoryBudgetExceeded):
        code, integrity, retryable, disposition = (
            "resource_budget_exceeded",
            "unavailable",
            True,
            "retry",
        )
    elif isinstance(exc, FileChangedError) or (
        isinstance(exc, RuntimeError) and "metadata changed" in lower
    ):
        code, integrity, retryable, disposition = (
            "source_changed",
            "unavailable",
            True,
            "retry",
        )
    elif isinstance(exc, OSError):
        code, integrity, retryable, disposition = (
            "source_unavailable",
            "unavailable",
            True,
            "retry",
        )
    elif isinstance(exc, ZipStructureError):
        if _is_policy_limit(message):
            code, integrity = "policy_zip_limit", "policy_rejected"
        else:
            code, integrity = "zip_structure_invalid", "invalid"
        retryable, disposition = False, "manual_review"
    elif isinstance(exc, zipfile.BadZipFile):
        code, integrity, retryable, disposition = (
            "zip_archive_corrupt",
            "corrupt",
            False,
            "deletion_candidate",
        )
    elif isinstance(exc, zlib.error):
        code, integrity, retryable, disposition = (
            "zip_member_deflate_corrupt",
            "corrupt",
            False,
            "deletion_candidate",
        )
    elif isinstance(exc, ET.ParseError):
        code, integrity, retryable, disposition = (
            "ooxml_required_xml_invalid",
            "corrupt",
            False,
            "deletion_candidate",
        )
    elif isinstance(exc, RuntimeError) and ("password" in lower or "encrypted" in lower):
        code, integrity, retryable, disposition = (
            "zip_encrypted_unsupported",
            "unsupported",
            False,
            "manual_review",
        )
    elif isinstance(exc, (ValueError, NotImplementedError)):
        code = "policy_processing_limit" if _is_policy_limit(message) else "ooxml_invalid"
        integrity = "policy_rejected" if _is_policy_limit(message) else "invalid"
        retryable, disposition = False, "manual_review"
    else:
        code, integrity, retryable, disposition = (
            "internal_parser_error",
            "unavailable",
            True,
            "retry",
        )
    diagnostic = DocxDiagnostic(
        code=code,
        message=message,
        stage="document",
        retryable=retryable,
        disposition=disposition,
    )
    return DocxFailure(
        code=code,
        message=message,
        integrity_status=integrity,
        retryable=retryable,
        disposition=disposition,
        diagnostics=(diagnostic,),
    )


# endregion [03]


for _historical_symbol in (
    member_upper_bounds,
    recover_raw_deflate_member,
    recovered_member_diagnostic,
    _is_policy_limit,
    diagnostic_for_member,
    fatal_member_error,
    classify_docx_exception,
):
    _historical_symbol.__module__ = "neocortex.capabilities.formats.docx.integrity"
del _historical_symbol
