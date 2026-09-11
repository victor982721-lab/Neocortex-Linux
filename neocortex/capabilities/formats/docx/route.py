"""Incremental DOCX extraction, search, layout classification and PDF pairing."""

from __future__ import annotations

import io
import json
import os
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, cast

import xxhash

from neocortex.deduplication import (
    FileChangedError,
    FileSnapshot,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.deduplication.io import native_io_path
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)
from neocortex.platform.policy import sqlite_path_collation

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot as _file_key
from neocortex.runtime.control.memory_runtime import (
    MemoryBudgetExceeded,
    MemoryResourceLimits,
    WeightedMemoryGate,
)
from neocortex.platform.zip_safety import (
    ZipStructureError,
    inspect_zip_structure,
)
from neocortex.workflow.review.review import ReviewCandidate, ReviewRecommendation
from neocortex.persistence.framework_route_state import ReviewCandidateReconciliation
from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring
from .integrity import (
    classify_docx_exception,
    diagnostic_for_member,
    fatal_member_error,
    member_upper_bounds,
    recover_raw_deflate_member,
    recovered_member_diagnostic,
)
from .layout import (
    TextBudget as _TextBudget,
    layout_result as _layout_result,
    normalized_text_digest as _normalized_text_digest,
    xml_text_and_layout as _xml_text_and_layout,
)
from .models import (
    ALGORITHM_VERSION,
    DocxDiagnostic,
    DocxFailure,
    DocxIntegrityStatus,
    DocxPart,
    DocxProcessingError,
    DocxReviewDisposition,
    DocxRouteConfig,
    DocxRouteSummary,
    DocxStatus,
    ExtractedDocx,
)
from .state import (
    UNKNOWN_BIRTHTIME_NS,
    docx_database,
    initialize_docx_state,
)


# region [01] Contracts and bounds

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
MAX_ZIP_MEMBERS = 10_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
DOCX_BASE_WORKSPACE_BYTES = 96 * 1024 * 1024
DOCX_INVENTORY_BATCH = 1000
DOCX_COMMIT_BATCH = 8
DOCX_PRUNE_BATCH = 256
DOCX_REVIEW_BATCH = 64
TEXT_CHUNK_CHARS = 256 * 1024
_PATH_COLLATION = sqlite_path_collation()
DOCX_REVIEW_REASON_CODES = frozenset(
    {
        "internal_parser_error",
        "ooxml_invalid",
        "ooxml_office_relationship_missing",
        "ooxml_optional_xml_invalid",
        "ooxml_required_part_missing",
        "ooxml_required_xml_invalid",
        "ooxml_word_main_type_unsupported_v2",
        "policy_member_limit",
        "policy_processing_limit",
        "policy_text_limit",
        "policy_zip_limit",
        "policy_zip_member_limit",
        "policy_zip_total_limit",
        "resource_budget_exceeded",
        "source_changed",
        "source_unavailable",
        "zip_archive_corrupt",
        "zip_compression_unsupported",
        "zip_duplicate_member",
        "zip_encrypted_unsupported",
        "zip_local_header_invalid",
        "zip_member_crc_mismatch",
        "zip_member_crc_mismatch_recovered",
        "zip_member_deflate_corrupt",
        "zip_member_metadata_mismatch_recovered",
        "zip_structure_invalid",
    }
)
CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
DC = "{http://purl.org/dc/elements/1.1/}"
DCTERMS = "{http://purl.org/dc/terms/}"
WORD_MAIN_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
        "application/vnd.ms-word.document.macroEnabled.main+xml",
        "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
    }
)
OFFICE_DOCUMENT_REL_SUFFIX = "/officeDocument"

_Part = DocxPart
_Extracted = ExtractedDocx


class _LiveDocxCachePathConflict(RuntimeError):
    """A cached path is still associated with another live DOCX identity."""


def _decode_cached_text(
    payload: object,
    expected_chars: object,
    *,
    max_chars: int,
) -> str | None:
    """Decode one bounded durable text representation without opening the source."""

    if isinstance(expected_chars, int):
        chars = expected_chars
    elif isinstance(expected_chars, str):
        try:
            chars = int(expected_chars)
        except ValueError:
            return None
    else:
        return None
    if chars < 0 or chars > max_chars or not isinstance(payload, (bytes, bytearray, memoryview)):
        return None
    decoder = zlib.decompressobj()
    try:
        # ``text_chars`` counts Unicode code points while zlib bounds bytes;
        # four bytes is the maximum UTF-8 width of one code point.
        decoded = decoder.decompress(bytes(payload), chars * 4 + 1)
    except (zlib.error, ValueError):
        return None
    if (
        len(decoded) > chars * 4
        or decoder.unconsumed_tail
        or decoder.unused_data
        or not decoder.eof
    ):
        return None
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if len(text) == chars else None


def _cached_layout_is_valid(row: Any) -> bool:
    """Verify layout evidence before allowing a complete cache reuse."""

    layout_json = row["layout_json"]
    layout_signature = row["layout_signature"]
    layout_class = row["layout_class"]
    if not all(isinstance(value, str) and value for value in (
        layout_json,
        layout_signature,
        layout_class,
    )):
        return False
    try:
        evidence = json.loads(layout_json)
        canonical = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        counts_match = (
            int(row["paragraph_count"]) == int(evidence["paragraph_count"])
            and int(row["table_count"]) == int(evidence["table_count"])
            and int(row["image_count"]) == int(evidence["image_count"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        isinstance(evidence, dict)
        and evidence.get("algorithm") == ALGORITHM_VERSION
        and evidence.get("page_class") is not None
        and evidence.get("structure") is not None
        and layout_class == f"{evidence['page_class']}:{evidence['structure']}"
        and counts_match
        and str(layout_signature) == xxhash.xxh3_128_hexdigest(canonical.encode("utf-8"))
    )


def _cached_docx_parts_are_valid(
    connection: Any,
    key: str,
    document_text: str,
    *,
    max_chars: int,
) -> bool:
    """Validate the persisted part layer against the durable document text."""

    rows = connection.execute(
        """SELECT part_name,part_kind,ordinal,text_zlib,text_chars
        FROM document_parts WHERE file_key=? ORDER BY ordinal,part_name""",
        (key,),
    ).fetchall()
    if not rows:
        return document_text == ""
    valid_kinds = {"body", "header", "footer", "footnotes", "endnotes", "comments"}
    reconstructed: list[str] = []
    ordinals: list[int] = []
    for row in rows:
        part_name = row["part_name"]
        part_kind = row["part_kind"]
        if not isinstance(part_name, str) or not part_name or part_kind not in valid_kinds:
            return False
        try:
            ordinal = int(row["ordinal"])
        except (TypeError, ValueError):
            return False
        part_text = _decode_cached_text(
            row["text_zlib"],
            row["text_chars"],
            max_chars=max_chars,
        )
        if part_text is None or not part_text:
            return False
        ordinals.append(ordinal)
        reconstructed.append(part_text)
    if ordinals != list(range(len(rows))):
        return False
    return "\n\n".join(reconstructed) == document_text


def _cached_docx_representation(
    connection: Any,
    row: Any,
    *,
    max_chars: int,
) -> str | None:
    """Return durable text only when all cache-owned structural evidence is valid."""

    document_text = _decode_cached_text(
        row["text_zlib"],
        row["text_chars"],
        max_chars=max_chars,
    )
    if document_text is None:
        return None
    digest = row["text_xxh3_128"]
    if not isinstance(digest, str) or digest != _normalized_text_digest(document_text):
        return None
    try:
        counts_valid = all(
            int(row[name]) >= 0
            for name in ("paragraph_count", "table_count", "image_count", "section_count")
        )
    except (TypeError, ValueError):
        return None
    if not counts_valid or not _cached_layout_is_valid(row):
        return None
    if not _cached_docx_parts_are_valid(
        connection,
        str(row["file_key"]),
        document_text,
        max_chars=max_chars,
    ):
        return None
    return document_text


def _cached_error_is_recoverable(row: Any) -> bool:
    """Accept only typed retry evidence, never a free-form error message."""

    disposition = str(row["review_disposition"] or "")
    if disposition in {
        "manual_review",
        "deletion_candidate",
        "keep_protected",
        "protected",
    }:
        return False
    return disposition == "retry" or bool(row["retryable"])


def _review_candidates(
    snapshot: FileSnapshot,
    *,
    source_status: str,
    integrity_status: str,
    disposition: str,
    retryable: bool,
    diagnostics: Iterable[DocxDiagnostic],
    detector_version: str,
    recovery_mode: str,
) -> list[ReviewCandidate]:
    if disposition in {"none", "unknown"}:
        return []
    recommendation: ReviewRecommendation
    if disposition == "deletion_candidate":
        recommendation = "deletion_candidate"
        confidence = 0.99
    elif disposition == "retry":
        recommendation = "retry"
        confidence = 0.80
    else:
        recommendation = "manual_review"
        confidence = 0.90
    candidates: list[ReviewCandidate] = []
    for diagnostic in diagnostics:
        evidence = {
            "integrity_status": integrity_status,
            "stage": diagnostic.stage,
            "part_name": diagnostic.part_name,
            "required": diagnostic.required,
            "expected_size": diagnostic.expected_size,
            "actual_size": diagnostic.actual_size,
            "expected_crc32": diagnostic.expected_crc32,
            "actual_crc32": diagnostic.actual_crc32,
            "recovery_mode": recovery_mode,
            "message": diagnostic.message[:512],
        }
        candidates.append(
            ReviewCandidate(
                route_name="docx",
                snapshot=snapshot,
                reason_code=diagnostic.code,
                source_status=source_status,
                recommendation=recommendation,
                retryable=retryable,
                confidence=confidence,
                evidence=evidence,
                detector_version=detector_version,
            )
        )
    return candidates


def _review_reason_codes(
    candidates: Iterable[ReviewCandidate],
) -> frozenset[str]:
    return frozenset(candidate.reason_code for candidate in candidates)


# endregion [01]


# region [02] Bounded OOXML parsing and layout evidence


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise ValueError(f"OOXML member exceeds {limit} bytes: {info.filename}")
    with archive.open(info) as source:
        payload = source.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"OOXML member exceeds {limit} bytes: {info.filename}")
    return payload


def _compress_text(text: str) -> bytes:
    compressor = zlib.compressobj(6)
    output = bytearray()
    for offset in range(0, len(text), TEXT_CHUNK_CHARS):
        output.extend(compressor.compress(text[offset : offset + TEXT_CHUNK_CHARS].encode("utf-8")))
    output.extend(compressor.flush())
    return bytes(output)


def _part_kind(name: str) -> str | None:
    lower = name.casefold()
    if lower == "word/document.xml":
        return "body"
    if lower.startswith("word/header") and lower.endswith(".xml"):
        return "header"
    if lower.startswith("word/footer") and lower.endswith(".xml"):
        return "footer"
    fixed = {
        "word/footnotes.xml": "footnotes",
        "word/endnotes.xml": "endnotes",
        "word/comments.xml": "comments",
    }
    return fixed.get(lower)


def _estimated_docx_memory_bytes(
    infos: list[zipfile.ZipInfo],
    max_text_chars: int,
) -> int:
    relevant = sum(info.file_size for info in infos if _part_kind(info.filename) is not None)
    retained_chars = min(max_text_chars, max(1024 * 1024, relevant * 2))
    return DOCX_BASE_WORKSPACE_BYTES + relevant * 2 + retained_chars * 4


def _processing_error(
    code: str,
    message: str,
    *,
    integrity_status: DocxIntegrityStatus,
    disposition: DocxReviewDisposition,
    stage: str,
    part_name: str | None = None,
) -> DocxProcessingError:
    diagnostic = DocxDiagnostic(
        code=code,
        message=message,
        stage=stage,
        part_name=part_name,
        required=True,
        disposition=disposition,
    )
    return DocxProcessingError(
        DocxFailure(
            code=code,
            message=message,
            integrity_status=integrity_status,
            retryable=False,
            disposition=disposition,
            diagnostics=(diagnostic,),
        )
    )


def _effective_recovery_error(
    primary: Exception,
    recovery: Exception,
) -> Exception:
    if isinstance(recovery, ZipStructureError) and (
        "supports only DEFLATE" in str(recovery) or "data descriptors" in str(recovery)
    ):
        return primary
    return recovery


def _read_xml_root(
    path: str | Path,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limit: int,
    upper_bound: int,
    stage: str,
    required: bool,
    cancellation: CancellationToken | None,
) -> tuple[ET.Element | None, DocxDiagnostic | None]:
    try:
        return safe_xml_fromstring(_read_member(archive, info, limit)), None
    except ValueError as exc:
        diagnostic = diagnostic_for_member(info, exc, stage=stage, required=required)
        if required:
            raise fatal_member_error(info, exc, stage=stage) from exc
        return None, diagnostic
    except (
        zipfile.BadZipFile,
        zlib.error,
        ET.ParseError,
        ZipStructureError,
        RuntimeError,
        NotImplementedError,
    ) as primary:
        try:
            recovered = recover_raw_deflate_member(
                path,
                info,
                upper_bound=upper_bound,
                max_member_bytes=limit,
                checkpoint=(cancellation.checkpoint if cancellation is not None else None),
            )
            root = safe_xml_fromstring(recovered.payload)
        except CancellationRequested:
            raise
        except (
            zipfile.BadZipFile,
            zlib.error,
            ET.ParseError,
            ZipStructureError,
            RuntimeError,
            ValueError,
            NotImplementedError,
        ) as recovery:
            effective = _effective_recovery_error(primary, recovery)
            diagnostic = diagnostic_for_member(info, effective, stage=stage, required=required)
            if required:
                raise fatal_member_error(info, effective, stage=stage) from primary
            return None, diagnostic
        return root, recovered_member_diagnostic(info, recovered, stage=stage, required=required)


def _parse_word_member(
    path: str | Path,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    budget: _TextBudget,
    collect_layout: bool,
    upper_bound: int,
    required: bool,
    cancellation: CancellationToken | None,
) -> tuple[str | None, dict[str, Any] | None, DocxDiagnostic | None]:
    prior_consumed = budget.consumed
    try:
        with archive.open(info) as member:
            text, layout = _xml_text_and_layout(
                member,
                collect_layout=collect_layout,
                budget=budget,
                cancellation=cancellation,
            )
        return text, layout, None
    except ValueError as exc:
        budget.consumed = prior_consumed
        diagnostic = diagnostic_for_member(info, exc, stage="word_xml", required=required)
        if required:
            raise fatal_member_error(info, exc, stage="word_xml") from exc
        return None, None, diagnostic
    except (
        zipfile.BadZipFile,
        zlib.error,
        ET.ParseError,
        ZipStructureError,
        RuntimeError,
        NotImplementedError,
    ) as primary:
        budget.consumed = prior_consumed
        try:
            recovered = recover_raw_deflate_member(
                path,
                info,
                upper_bound=upper_bound,
                max_member_bytes=MAX_MEMBER_BYTES,
                checkpoint=(cancellation.checkpoint if cancellation is not None else None),
            )
            text, layout = _xml_text_and_layout(
                io.BytesIO(recovered.payload),
                collect_layout=collect_layout,
                budget=budget,
                cancellation=cancellation,
            )
        except CancellationRequested:
            raise
        except (
            zipfile.BadZipFile,
            zlib.error,
            ET.ParseError,
            ZipStructureError,
            RuntimeError,
            ValueError,
            NotImplementedError,
        ) as recovery:
            budget.consumed = prior_consumed
            effective = _effective_recovery_error(primary, recovery)
            diagnostic = diagnostic_for_member(info, effective, stage="word_xml", required=required)
            if required:
                raise fatal_member_error(info, effective, stage="word_xml") from primary
            return None, None, diagnostic
        return (
            text,
            layout,
            recovered_member_diagnostic(info, recovered, stage="word_xml", required=required),
        )


@dataclass(slots=True)
class _DocxExtractionAccumulator:
    budget: _TextBudget
    parts: list[_Part] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)
    header_signatures: list[str] = field(default_factory=list)
    footer_signatures: list[str] = field(default_factory=list)
    diagnostics: list[DocxDiagnostic] = field(default_factory=list)

    def add_body(self, info: zipfile.ZipInfo, text: str) -> None:
        if not text:
            return
        self.text_chunks.append(text)
        self.parts.append(
            _Part(info.filename, "body", len(self.parts), _compress_text(text), len(text))
        )

    def add_optional(
        self,
        info: zipfile.ZipInfo,
        kind: str,
        text: str | None,
        prior_consumed: int,
    ) -> None:
        if not text:
            return
        try:
            if self.text_chunks:
                self.budget.consume(2)
        except ValueError as exc:
            self.budget.consumed = prior_consumed
            self.diagnostics.append(
                diagnostic_for_member(
                    info,
                    exc,
                    stage="word_xml",
                    required=False,
                )
            )
            return
        if self.text_chunks:
            self.text_chunks.append("\n\n")
        self.text_chunks.append(text)
        if kind == "header":
            self.header_signatures.append(_normalized_text_digest(text))
        elif kind == "footer":
            self.footer_signatures.append(_normalized_text_digest(text))
        self.parts.append(
            _Part(info.filename, kind, len(self.parts), _compress_text(text), len(text))
        )


def _validate_docx_archive(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ZIP_MEMBERS:
        raise _processing_error(
            "policy_zip_member_limit",
            "DOCX contains too many ZIP members",
            integrity_status="policy_rejected",
            disposition="manual_review",
            stage="zip_preflight",
        )
    if sum(info.file_size for info in infos) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise _processing_error(
            "policy_zip_total_limit",
            "DOCX uncompressed content exceeds the safety limit",
            integrity_status="policy_rejected",
            disposition="manual_review",
            stage="zip_preflight",
        )
    folded_names = [info.filename.casefold() for info in infos]
    if len(folded_names) != len(set(folded_names)):
        raise _processing_error(
            "zip_duplicate_member",
            "DOCX contains duplicate case-insensitive ZIP member names",
            integrity_status="invalid",
            disposition="manual_review",
            stage="zip_preflight",
        )
    encrypted = next((info for info in infos if info.flag_bits & 0x1), None)
    if encrypted is not None:
        raise _processing_error(
            "zip_encrypted_unsupported",
            f"Encrypted DOCX member is unsupported: {encrypted.filename}",
            integrity_status="unsupported",
            disposition="manual_review",
            stage="zip_preflight",
            part_name=encrypted.filename,
        )


def _required_docx_members(
    infos: list[zipfile.ZipInfo],
) -> dict[str, zipfile.ZipInfo]:
    by_name = {info.filename.casefold(): info for info in infos}
    for required_name in ("[content_types].xml", "_rels/.rels", "word/document.xml"):
        if required_name not in by_name:
            raise _processing_error(
                "ooxml_required_part_missing",
                f"OOXML package has no {required_name}",
                integrity_status="invalid",
                disposition="manual_review",
                stage="package_contract",
                part_name=required_name,
            )
    return by_name


def _validate_docx_contract(
    path: str | Path,
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    boundaries: dict[int, int],
    cancellation: CancellationToken | None,
    diagnostics: list[DocxDiagnostic],
) -> None:
    content_info = by_name["[content_types].xml"]
    content_root, diagnostic = _read_xml_root(
        path,
        archive,
        content_info,
        limit=MAX_METADATA_BYTES,
        upper_bound=boundaries[int(content_info.header_offset)],
        stage="content_types",
        required=True,
        cancellation=cancellation,
    )
    if diagnostic is not None:
        diagnostics.append(diagnostic)
    assert content_root is not None
    content_type_valid = any(
        node.tag.rsplit("}", 1)[-1] == "Override"
        and node.get("PartName", "").casefold() == "/word/document.xml"
        and node.get("ContentType", "") in WORD_MAIN_CONTENT_TYPES
        for node in content_root.iter()
    )
    if not content_type_valid:
        raise _processing_error(
            "ooxml_word_main_type_unsupported_v2",
            "OOXML package does not declare a supported Word main type",
            integrity_status="invalid",
            disposition="manual_review",
            stage="package_contract",
            part_name=content_info.filename,
        )
    _validate_docx_relationship(
        path,
        archive,
        by_name["_rels/.rels"],
        boundaries,
        cancellation,
        diagnostics,
    )


def _validate_docx_relationship(
    path: str | Path,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    boundaries: dict[int, int],
    cancellation: CancellationToken | None,
    diagnostics: list[DocxDiagnostic],
) -> None:
    root, diagnostic = _read_xml_root(
        path,
        archive,
        info,
        limit=MAX_METADATA_BYTES,
        upper_bound=boundaries[int(info.header_offset)],
        stage="package_relationships",
        required=True,
        cancellation=cancellation,
    )
    if diagnostic is not None:
        diagnostics.append(diagnostic)
    assert root is not None
    valid = any(
        node.tag.rsplit("}", 1)[-1] == "Relationship"
        and node.get("Type", "").endswith(OFFICE_DOCUMENT_REL_SUFFIX)
        and node.get("Target", "").replace("\\", "/").lstrip("/").casefold() == "word/document.xml"
        for node in root.iter()
    )
    if valid:
        return
    raise _processing_error(
        "ooxml_office_relationship_missing",
        "OOXML package has no relationship to word/document.xml",
        integrity_status="invalid",
        disposition="manual_review",
        stage="package_contract",
        part_name=info.filename,
    )


def _extract_docx_parts(
    path: str | Path,
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    by_name: dict[str, zipfile.ZipInfo],
    boundaries: dict[int, int],
    max_text_chars: int,
    cancellation: CancellationToken | None,
    diagnostics: list[DocxDiagnostic],
) -> tuple[_DocxExtractionAccumulator, dict[str, Any]]:
    accumulator = _DocxExtractionAccumulator(_TextBudget(max_text_chars))
    accumulator.diagnostics = diagnostics
    body_info = by_name["word/document.xml"]
    body_text, body_layout, diagnostic = _parse_word_member(
        path,
        archive,
        body_info,
        budget=accumulator.budget,
        collect_layout=True,
        upper_bound=boundaries[int(body_info.header_offset)],
        required=True,
        cancellation=cancellation,
    )
    assert body_text is not None and body_layout is not None
    if diagnostic is not None:
        diagnostics.append(diagnostic)
    accumulator.add_body(body_info, body_text)
    optional_infos = sorted(
        (info for info in infos if info is not body_info and _part_kind(info.filename) is not None),
        key=lambda item: item.filename.casefold(),
    )
    for info in optional_infos:
        _extract_optional_docx_part(
            path,
            archive,
            info,
            boundaries,
            cancellation,
            accumulator,
        )
    return accumulator, body_layout


def _extract_optional_docx_part(
    path: str | Path,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    boundaries: dict[int, int],
    cancellation: CancellationToken | None,
    accumulator: _DocxExtractionAccumulator,
) -> None:
    if cancellation is not None:
        cancellation.checkpoint()
    kind = _part_kind(info.filename)
    assert kind is not None
    prior_consumed = accumulator.budget.consumed
    text, _layout, diagnostic = _parse_word_member(
        path,
        archive,
        info,
        budget=accumulator.budget,
        collect_layout=False,
        upper_bound=boundaries[int(info.header_offset)],
        required=False,
        cancellation=cancellation,
    )
    if diagnostic is not None:
        accumulator.diagnostics.append(diagnostic)
    accumulator.add_optional(info, kind, text, prior_consumed)


def _docx_metadata(
    path: str | Path,
    archive: zipfile.ZipFile,
    by_name: dict[str, zipfile.ZipInfo],
    boundaries: dict[int, int],
    cancellation: CancellationToken | None,
    diagnostics: list[DocxDiagnostic],
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    core = by_name.get("docprops/core.xml")
    if core is None:
        return metadata
    root, diagnostic = _read_xml_root(
        path,
        archive,
        core,
        limit=MAX_METADATA_BYTES,
        upper_bound=boundaries[int(core.header_offset)],
        stage="core_metadata",
        required=False,
        cancellation=cancellation,
    )
    if diagnostic is not None:
        diagnostics.append(diagnostic)
    if root is None:
        return metadata
    fields = {
        "title": f"{DC}title",
        "author": f"{DC}creator",
        "created": f"{DCTERMS}created",
        "modified": f"{DCTERMS}modified",
        "last_modified_by": f"{CP}lastModifiedBy",
    }
    for name, tag in fields.items():
        node = root.find(tag)
        if node is not None and node.text:
            metadata[name] = node.text.strip()[:4096]
    return metadata


def _build_extracted_docx(
    accumulator: _DocxExtractionAccumulator,
    body: str,
    body_layout: dict[str, Any],
    metadata: dict[str, str],
    image_count: int,
    max_text_chars: int,
) -> _Extracted:
    if len(body) > max_text_chars:
        raise ValueError(f"DOCX text exceeds {max_text_chars} characters")
    layout_class, layout_signature, layout_json = _layout_result(
        body_layout,
        accumulator.header_signatures,
        accumulator.footer_signatures,
        image_count,
    )
    diagnostics = accumulator.diagnostics
    status: DocxStatus = "partial" if diagnostics else "complete"
    recovery_mode = (
        "raw_deflate_validated_xml"
        if any(item.code.endswith("_recovered") for item in diagnostics)
        else "optional_parts_skipped"
        if diagnostics
        else "none"
    )
    return _Extracted(
        tuple(accumulator.parts),
        body,
        metadata,
        int(body_layout["paragraphs"]),
        int(body_layout["tables"]),
        image_count,
        len(body_layout["sections"]),
        layout_class,
        layout_signature,
        layout_json,
        status=status,
        integrity_status="degraded" if diagnostics else "valid",
        review_disposition="manual_review" if diagnostics else "none",
        recovery_mode=recovery_mode,
        diagnostics=tuple(diagnostics),
    )


def extract_docx(
    path: str | Path,
    max_text_chars: int,
    memory_gate: WeightedMemoryGate | None = None,
    cancellation: CancellationToken | None = None,
) -> _Extracted:
    """Extract useful OOXML parts without rendering or creating artifacts."""

    if cancellation is not None:
        cancellation.checkpoint()
    structure = inspect_zip_structure(path, max_members=MAX_ZIP_MEMBERS)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        _validate_docx_archive(infos)
        boundaries = member_upper_bounds(infos, structure.central_directory_offset)
        admission = (
            memory_gate.admit(_estimated_docx_memory_bytes(infos, max_text_chars))
            if memory_gate is not None
            else nullcontext()
        )
        with admission:
            by_name = _required_docx_members(infos)
            diagnostics: list[DocxDiagnostic] = []
            _validate_docx_contract(
                path,
                archive,
                by_name,
                boundaries,
                cancellation,
                diagnostics,
            )

            accumulator, body_layout = _extract_docx_parts(
                path,
                archive,
                infos,
                by_name,
                boundaries,
                max_text_chars,
                cancellation,
                diagnostics,
            )
            metadata = _docx_metadata(
                path,
                archive,
                by_name,
                boundaries,
                cancellation,
                diagnostics,
            )
            image_count = sum(
                1
                for info in infos
                if info.filename.casefold().startswith("word/media/") and not info.is_dir()
            )
            body = "".join(accumulator.text_chunks)
    return _build_extracted_docx(
        accumulator,
        body,
        body_layout,
        metadata,
        image_count,
        max_text_chars,
    )


# endregion [02]


# region [03] Incremental route


@dataclass(frozen=True, slots=True)
class _DocxCandidateOutcome:
    cache_hits: int = 0
    cached_errors: int = 0
    extracted: int = 0
    fts_documents_indexed: int = 0
    errors: int = 0
    layouts: int = 0
    partial_documents: int = 0
    cached_partial_documents: int = 0
    review_candidates: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0


class DocxRoute:
    def __init__(
        self,
        config: DocxRouteConfig,
        framework_state,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if config.max_text_chars < 1:
            raise ValueError("DOCX max_text_chars must be positive")
        if config.max_file_bytes is not None and config.max_file_bytes < 1:
            raise ValueError("DOCX max_file_bytes must be positive")
        if config.max_documents is not None and config.max_documents < 1:
            raise ValueError("DOCX max_documents must be positive")
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()
        self.memory_gate = (
            memory_gate
            if memory_gate is not None
            else WeightedMemoryGate(
                MemoryResourceLimits(
                    memory_budget_bytes=config.memory_budget_bytes,
                    min_free_memory_bytes=config.min_free_memory_bytes,
                    min_free_commit_bytes=config.min_free_commit_bytes,
                    wait_timeout_seconds=config.memory_wait_timeout_seconds,
                ),
                self.cancellation,
            )
        )
        self._recoverable_retry_keys: set[str] = set()
        initialize_docx_state(config.state_path)

    def _claim_recoverable_retry(self, snapshot: FileSnapshot) -> bool:
        claimed = getattr(self, "_recoverable_retry_keys", None)
        if claimed is None:
            claimed = set()
            self._recoverable_retry_keys = claimed
        key = _file_key(snapshot)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    def _candidates(self, connection) -> Iterator[FileSnapshot]:
        """Yield bounded work with errors and degraded documents first."""

        row_limit = self.config.max_documents if self.config.max_documents is not None else -1
        selection_sql, selection_parameters = self._selection_sql()
        rows = connection.execute(
            """SELECT i.file_key,i.path,i.size,i.mtime_ns,i.birthtime_ns
            FROM docx_inventory i LEFT JOIN documents d USING(file_key)
            WHERE (? IS NULL OR i.size<=?) AND """
            + selection_sql
            + """
            ORDER BY CASE
                WHEN d.status='error' THEN 0
                WHEN d.status='partial' THEN 1
                WHEN d.file_key IS NULL THEN 2
                WHEN d.status='complete' AND d.processing_signature<>? THEN 3
                ELSE 4
            END,
            i.path COLLATE NOCASE,i.file_key LIMIT ?""",
            (
                self.config.max_file_bytes,
                self.config.max_file_bytes,
                *selection_parameters,
                self.config.processing_signature,
                row_limit,
            ),
        )
        for row in rows:
            volume_hex, file_hex = str(row["file_key"]).split(":", 1)
            yield FileSnapshot(
                str(row["path"]),
                int(volume_hex, 16),
                int(file_hex, 16),
                int(row["size"]),
                int(row["mtime_ns"]),
                int(row["birthtime_ns"]),
            )

    def _selection_sql(self) -> tuple[str, list[object]]:
        selection = self.config.selection
        clauses = ["i.last_seen_run_id=?"]
        parameters: list[object] = [self.run_id]
        if selection.statuses:
            placeholders = ",".join("?" for _ in selection.statuses)
            clauses.append(f"COALESCE(d.status,'pending') IN ({placeholders})")
            parameters.extend(selection.statuses)
        if selection.error_types:
            placeholders = ",".join("?" for _ in selection.error_types)
            clauses.append(f"d.error_type IN ({placeholders})")
            parameters.extend(selection.error_types)
        return " AND ".join(clauses), parameters

    def _selected_counts(self, connection) -> tuple[int, int]:
        selection_sql, selection_parameters = self._selection_sql()
        total = int(
            connection.execute(
                """SELECT COUNT(*) FROM docx_inventory i
                LEFT JOIN documents d USING(file_key) WHERE """
                + selection_sql,
                selection_parameters,
            ).fetchone()[0]
        )
        if self.config.max_file_bytes is None:
            return total, total
        eligible = int(
            connection.execute(
                """SELECT COUNT(*) FROM docx_inventory i
                LEFT JOIN documents d USING(file_key) WHERE i.size<=? AND """
                + selection_sql,
                (self.config.max_file_bytes, *selection_parameters),
            ).fetchone()[0]
        )
        return total, eligible

    def _stage_inventory(self, connection) -> None:
        batch: list[tuple[str, str, int, int, int, int]] = []
        selection = self.config.selection
        if selection.paths or selection.recommendations:
            iterator = self.framework_state.iter_selected_route_candidates(
                self.run_id,
                DOCX_MIME,
                "docx",
                selection,
            )
        else:
            iterator = self.framework_state.iter_route_candidates(self.run_id, DOCX_MIME)
        for snapshot in iterator:
            self.cancellation.checkpoint()
            batch.append(
                (
                    _file_key(snapshot),
                    snapshot.path,
                    snapshot.size,
                    snapshot.mtime_ns,
                    snapshot.birthtime_ns,
                    self.run_id,
                )
            )
            if len(batch) >= DOCX_INVENTORY_BATCH:
                self._write_inventory_batch(connection, batch)
                connection.commit()
                batch.clear()
        if batch:
            self._write_inventory_batch(connection, batch)
            connection.commit()
        while not self.config.selection.active:
            self.cancellation.checkpoint()
            stale_keys = connection.execute(
                """SELECT file_key FROM docx_inventory
                WHERE last_seen_run_id<>? ORDER BY file_key LIMIT ?""",
                (self.run_id, DOCX_INVENTORY_BATCH),
            ).fetchall()
            if not stale_keys:
                break
            connection.executemany("DELETE FROM docx_inventory WHERE file_key=?", stale_keys)
            connection.commit()
        connection.execute(
            """UPDATE documents SET last_seen_run_id=? WHERE EXISTS(
                SELECT 1 FROM docx_inventory i WHERE i.file_key=documents.file_key
                AND i.size=documents.size AND i.mtime_ns=documents.mtime_ns
                AND i.birthtime_ns=documents.birthtime_ns)""",
            (self.run_id,),
        )
        connection.commit()

    def _prune_stale_documents(self, connection) -> int:
        """Delete obsolete DOCX cache rows in bounded committed batches."""

        removed = 0
        while True:
            self.cancellation.checkpoint()
            # A migrated sentinel cannot prove reuse, but matching identity,
            # size and mtime still prove the source remains in the inventory.
            # Preserve it until an eligible run refreshes the real birth time.
            keys = connection.execute(
                """SELECT d.file_key FROM documents d WHERE NOT EXISTS(
                SELECT 1 FROM docx_inventory i WHERE i.file_key=d.file_key
                AND i.size=d.size AND i.mtime_ns=d.mtime_ns
                AND (i.birthtime_ns=d.birthtime_ns OR d.birthtime_ns=?))
                ORDER BY d.file_key LIMIT ?""",
                (UNKNOWN_BIRTHTIME_NS, DOCX_PRUNE_BATCH),
            ).fetchall()
            if not keys:
                return removed
            connection.executemany("DELETE FROM document_fts WHERE file_key=?", keys)
            removed += int(
                connection.executemany("DELETE FROM documents WHERE file_key=?", keys).rowcount
            )
            connection.commit()

    @staticmethod
    def _write_inventory_batch(
        connection,
        batch: list[tuple[str, str, int, int, int, int]],
    ) -> None:
        connection.executemany(
            """INSERT INTO docx_inventory(
            file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
            VALUES(?,?,?,?,?,?) ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,
            size=excluded.size,mtime_ns=excluded.mtime_ns,
            birthtime_ns=excluded.birthtime_ns,
            last_seen_run_id=excluded.last_seen_run_id""",
            batch,
        )

    def _cache_status(self, connection, snapshot: FileSnapshot) -> str:
        row = connection.execute(
            """SELECT file_key,size,mtime_ns,birthtime_ns,processing_signature,status,
            retryable,failure_code,review_disposition,text_zlib,text_chars,text_xxh3_128,
            paragraph_count,table_count,image_count,section_count,
            layout_class,layout_signature,layout_json
            FROM documents WHERE file_key=?""",
            (_file_key(snapshot),),
        ).fetchone()
        if not (
            row is not None
            and int(row["size"]) == snapshot.size
            and int(row["mtime_ns"]) == snapshot.mtime_ns
            and int(row["birthtime_ns"]) == snapshot.birthtime_ns
            and row["processing_signature"] == self.config.processing_signature
        ):
            return "miss"
        if row["status"] in {"complete", "partial"} and _cached_docx_representation(
            connection,
            row,
            max_chars=self.config.max_text_chars,
        ) is None:
            return "miss"
        if row["status"] == "complete":
            return "complete"
        if row["status"] == "error" and row["failure_code"] == "ooxml_content_type_mismatch":
            # One-time compatibility retry for rows written before Word
            # templates and macro-enabled packages were accepted.
            return "retry"
        force_retry = self.config.selection.force_incomplete_retry
        if row["status"] == "partial" and not self.config.retry_errors and not force_retry:
            return "partial"
        if (
            row["status"] == "error"
            and (self.config.retry_errors or force_retry)
        ):
            return "retry"
        if (
            row["status"] == "error"
            and self.config.retry_recoverable_errors
            and _cached_error_is_recoverable(row)
            and self._claim_recoverable_retry(snapshot)
        ):
            return "retry"
        if row["status"] == "error":
            return "cached_error"
        return "retry"

    def _touch_cache_hit(
        self,
        connection,
        snapshot: FileSnapshot,
        cache_status: str,
    ) -> int | None:
        """Refresh one hit after safely resolving a stale path owner."""

        key = _file_key(snapshot)
        conflict = connection.execute(
            f"""SELECT d.file_key,EXISTS(
                SELECT 1 FROM docx_inventory i
                WHERE i.file_key=d.file_key AND i.last_seen_run_id=?
                AND i.size=d.size AND i.mtime_ns=d.mtime_ns
                AND (i.birthtime_ns=d.birthtime_ns OR d.birthtime_ns=?)
            ) AS owner_is_live
            FROM documents d
            WHERE d.path=? COLLATE {_PATH_COLLATION} AND d.file_key<>?
            LIMIT 1""",
            (
                self.run_id,
                UNKNOWN_BIRTHTIME_NS,
                snapshot.path,
                key,
            ),
        ).fetchone()
        if conflict is not None:
            owner_key = str(conflict["file_key"])
            if bool(conflict["owner_is_live"]):
                raise _LiveDocxCachePathConflict(
                    f"cached DOCX path is still owned by a live inventory identity: {snapshot.path}"
                )
            connection.execute(
                "DELETE FROM document_fts WHERE file_key=?",
                (owner_key,),
            )
            connection.execute(
                "DELETE FROM layout_groups WHERE representative_file_key=?",
                (owner_key,),
            )
            connection.execute(
                "DELETE FROM documents WHERE file_key=?",
                (owner_key,),
            )

        connection.execute(
            "UPDATE documents SET path=?,birthtime_ns=?,"
            "last_seen_run_id=?,updated_ns=? WHERE file_key=?",
            (
                snapshot.path,
                snapshot.birthtime_ns,
                self.run_id,
                time.time_ns(),
                key,
            ),
        )
        # Preserve the narrow historical path-ownership seam used by callers
        # that construct a route probe without a full route configuration.
        if not hasattr(self, "config"):
            connection.execute(
                "UPDATE document_fts SET path=? WHERE file_key=?",
                (snapshot.path, key),
            )
            return 0
        if cache_status not in {"complete", "partial"}:
            # Error caches have no durable text representation.  Remove any
            # orphaned index row rather than allowing a stale search hit.
            connection.execute("DELETE FROM document_fts WHERE file_key=?", (key,))
            return 0
        row = connection.execute(
            """SELECT file_key,path,title,author,text_zlib,text_chars,text_xxh3_128,
            paragraph_count,table_count,image_count,section_count,
            layout_class,layout_signature,layout_json
            FROM documents WHERE file_key=?""",
            (key,),
        ).fetchone()
        if row is None:
            return None
        body = _cached_docx_representation(
            connection,
            row,
            max_chars=self.config.max_text_chars,
        )
        if body is None:
            return None
        expected = (
            snapshot.path,
            str(row["title"] or ""),
            str(row["author"] or ""),
            body,
        )
        fts_rows = connection.execute(
            "SELECT path,title,author,body FROM document_fts WHERE file_key=?",
            (key,),
        ).fetchall()
        if len(fts_rows) == 1 and tuple(fts_rows[0]) == expected:
            return 0
        connection.execute("DELETE FROM document_fts WHERE file_key=?", (key,))
        connection.execute(
            "INSERT INTO document_fts(file_key,path,title,author,body) VALUES(?,?,?,?,?)",
            (key, *expected),
        )
        return 1

    @staticmethod
    def _prior_reviewable(connection, snapshot: FileSnapshot) -> bool:
        row = connection.execute(
            "SELECT status FROM documents WHERE file_key=?",
            (_file_key(snapshot),),
        ).fetchone()
        return row is not None and row["status"] in {"partial", "error"}

    @staticmethod
    def _cached_review(
        connection,
        snapshot: FileSnapshot,
    ) -> tuple[str, str, str, bool, str, tuple[DocxDiagnostic, ...]]:
        """Rehydrate bounded review evidence for a retained partial/error row."""

        key = _file_key(snapshot)
        row = connection.execute(
            """SELECT status,integrity_status,failure_code,retryable,
            review_disposition,recovery_mode,error_type,error_message
            FROM documents WHERE file_key=?""",
            (key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("cached DOCX result disappeared during route execution")
        diagnostic_rows = connection.execute(
            """SELECT part_name,stage,code,message,required,retryable,disposition,
            expected_size,actual_size,expected_crc32,actual_crc32
            FROM document_diagnostics WHERE file_key=? ORDER BY ordinal""",
            (key,),
        ).fetchall()
        diagnostics = tuple(
            DocxDiagnostic(
                code=str(item["code"]),
                message=str(item["message"]),
                stage=str(item["stage"]),
                part_name=item["part_name"],
                required=bool(item["required"]),
                retryable=bool(item["retryable"]),
                disposition=cast(
                    DocxReviewDisposition,
                    str(item["disposition"]),
                ),
                expected_size=item["expected_size"],
                actual_size=item["actual_size"],
                expected_crc32=item["expected_crc32"],
                actual_crc32=item["actual_crc32"],
            )
            for item in diagnostic_rows
        )
        disposition = cast(
            DocxReviewDisposition,
            str(row["review_disposition"] or "manual_review"),
        )
        retryable = bool(row["retryable"])
        if not diagnostics:
            code = str(row["failure_code"] or row["error_type"] or "docx_cached_review")
            diagnostics = (
                DocxDiagnostic(
                    code=code,
                    message=str(row["error_message"] or "cached DOCX requires review"),
                    stage="cached_result",
                    required=row["status"] == "error",
                    retryable=retryable,
                    disposition=disposition,
                ),
            )
        return (
            str(row["status"]),
            str(row["integrity_status"] or "unknown"),
            disposition,
            retryable,
            str(row["recovery_mode"] or "none"),
            diagnostics,
        )

    @staticmethod
    def _write_diagnostics(
        connection,
        key: str,
        diagnostics: Iterable[DocxDiagnostic],
    ) -> None:
        connection.execute("DELETE FROM document_diagnostics WHERE file_key=?", (key,))
        connection.executemany(
            """INSERT INTO document_diagnostics(
            file_key,ordinal,part_name,stage,code,message,required,retryable,
            disposition,expected_size,actual_size,expected_crc32,actual_crc32)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    key,
                    ordinal,
                    item.part_name,
                    item.stage,
                    item.code,
                    item.message[:8192],
                    int(item.required),
                    int(item.retryable),
                    item.disposition,
                    item.expected_size,
                    item.actual_size,
                    item.expected_crc32,
                    item.actual_crc32,
                )
                for ordinal, item in enumerate(diagnostics)
            ),
        )

    def _store_success(self, connection, snapshot: FileSnapshot, result: _Extracted) -> None:
        key = _file_key(snapshot)
        now = time.time_ns()
        failure_code = result.diagnostics[0].code if result.diagnostics else None
        connection.execute(
            f"""DELETE FROM document_fts WHERE file_key IN(
            SELECT file_key FROM documents
            WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?)""",
            (snapshot.path, key),
        )
        connection.execute(
            f"DELETE FROM documents WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
            (snapshot.path, key),
        )
        connection.execute(
            """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,integrity_status,failure_code,retryable,
                review_disposition,recovery_mode,title,author,created,modified,
                text_zlib,text_chars,text_xxh3_128,paragraph_count,table_count,image_count,
                section_count,layout_class,layout_signature,layout_json,last_seen_run_id,updated_ns)
                VALUES(:file_key,:path,:size,:mtime_ns,:birthtime_ns,
                :processing_signature,:status,:integrity_status,:failure_code,0,
                :review_disposition,:recovery_mode,:title,:author,:created,:modified,
                :text_zlib,:text_chars,:text_xxh3_128,:paragraph_count,:table_count,
                :image_count,:section_count,:layout_class,:layout_signature,
                :layout_json,:last_seen_run_id,:updated_ns)
                ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,size=excluded.size,
                mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
                processing_signature=excluded.processing_signature,
                status=excluded.status,integrity_status=excluded.integrity_status,
                failure_code=excluded.failure_code,retryable=0,
                review_disposition=excluded.review_disposition,
                recovery_mode=excluded.recovery_mode,title=excluded.title,
                author=excluded.author,created=excluded.created,
                modified=excluded.modified,text_zlib=excluded.text_zlib,text_chars=excluded.text_chars,
                text_xxh3_128=excluded.text_xxh3_128,paragraph_count=excluded.paragraph_count,
                table_count=excluded.table_count,image_count=excluded.image_count,
                section_count=excluded.section_count,layout_class=excluded.layout_class,
                layout_signature=excluded.layout_signature,layout_json=excluded.layout_json,
                error_type=NULL,error_message=NULL,last_seen_run_id=excluded.last_seen_run_id,
                updated_ns=excluded.updated_ns""",
            {
                "file_key": key,
                "path": snapshot.path,
                "size": snapshot.size,
                "mtime_ns": snapshot.mtime_ns,
                "birthtime_ns": snapshot.birthtime_ns,
                "processing_signature": self.config.processing_signature,
                "status": result.status,
                "integrity_status": result.integrity_status,
                "failure_code": failure_code,
                "review_disposition": result.review_disposition,
                "recovery_mode": result.recovery_mode,
                "title": result.metadata.get("title"),
                "author": result.metadata.get("author"),
                "created": result.metadata.get("created"),
                "modified": result.metadata.get("modified"),
                "text_zlib": _compress_text(result.body),
                "text_chars": len(result.body),
                "text_xxh3_128": _normalized_text_digest(result.body),
                "paragraph_count": result.paragraph_count,
                "table_count": result.table_count,
                "image_count": result.image_count,
                "section_count": result.section_count,
                "layout_class": result.layout_class,
                "layout_signature": result.layout_signature,
                "layout_json": result.layout_json,
                "last_seen_run_id": self.run_id,
                "updated_ns": now,
            },
        )
        connection.execute("DELETE FROM document_parts WHERE file_key=?", (key,))
        connection.executemany(
            """INSERT INTO document_parts(file_key,part_name,part_kind,ordinal,text_zlib,text_chars)
            VALUES(?,?,?,?,?,?)""",
            (
                (
                    key,
                    part.name,
                    part.kind,
                    part.ordinal,
                    part.text_zlib,
                    part.text_chars,
                )
                for part in result.parts
            ),
        )
        self._write_diagnostics(connection, key, result.diagnostics)
        connection.execute("DELETE FROM document_fts WHERE file_key=?", (key,))
        connection.execute(
            "INSERT INTO document_fts(file_key,path,title,author,body) VALUES(?,?,?,?,?)",
            (
                key,
                snapshot.path,
                result.metadata.get("title", ""),
                result.metadata.get("author", ""),
                result.body,
            ),
        )

    def _store_error(
        self,
        connection,
        snapshot: FileSnapshot,
        exc: Exception,
    ) -> DocxFailure:
        key = _file_key(snapshot)
        failure = classify_docx_exception(exc)
        connection.execute(
            f"""DELETE FROM document_fts WHERE file_key=? OR file_key IN(
            SELECT file_key FROM documents
            WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?)""",
            (key, snapshot.path, key),
        )
        connection.execute(
            f"DELETE FROM documents WHERE path=? COLLATE {_PATH_COLLATION} AND file_key<>?",
            (snapshot.path, key),
        )
        connection.execute("DELETE FROM document_parts WHERE file_key=?", (key,))
        connection.execute("DELETE FROM pdf_counterparts WHERE docx_file_key=?", (key,))
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            integrity_status,failure_code,retryable,review_disposition,recovery_mode,
            error_type,error_message,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'error',?,?,?,?, 'none',?,?,?,?)
            ON CONFLICT(file_key) DO UPDATE SET path=excluded.path,size=excluded.size,
            mtime_ns=excluded.mtime_ns,birthtime_ns=excluded.birthtime_ns,
            processing_signature=excluded.processing_signature,
            status='error',integrity_status=excluded.integrity_status,
            failure_code=excluded.failure_code,retryable=excluded.retryable,
            review_disposition=excluded.review_disposition,recovery_mode='none',
            title=NULL,author=NULL,created=NULL,modified=NULL,text_zlib=NULL,
            text_chars=0,text_xxh3_128=NULL,paragraph_count=0,table_count=0,
            image_count=0,section_count=0,layout_class=NULL,layout_signature=NULL,
            layout_json=NULL,error_type=excluded.error_type,
            error_message=excluded.error_message,
            last_seen_run_id=excluded.last_seen_run_id,updated_ns=excluded.updated_ns""",
            (
                key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                self.config.processing_signature,
                failure.integrity_status,
                failure.code,
                int(failure.retryable),
                failure.disposition,
                type(exc).__name__,
                failure.message,
                self.run_id,
                time.time_ns(),
            ),
        )
        self._write_diagnostics(connection, key, failure.diagnostics)
        return failure

    def _pair_pdfs(self, connection) -> tuple[int, int, int, int]:
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS current_docx("
            "file_key TEXT PRIMARY KEY,stem TEXT NOT NULL,parent TEXT NOT NULL) WITHOUT ROWID"
        )
        connection.execute("DELETE FROM current_docx")
        connection.executemany(
            "INSERT INTO current_docx(file_key,stem,parent) VALUES(?,?,?)",
            (
                (
                    str(key),
                    Path(raw_path).stem.casefold(),
                    os.path.normcase(str(Path(raw_path).parent)),
                )
                for key, raw_path in connection.execute(
                    "SELECT file_key,path FROM documents "
                    "WHERE last_seen_run_id=? "
                    "AND status IN ('complete','partial') ORDER BY path",
                    (self.run_id,),
                )
            ),
        )
        candidate_stems = frozenset(
            str(row[0]) for row in connection.execute("SELECT DISTINCT stem FROM current_docx")
        )
        connection.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS current_pdfs("
            f"path TEXT PRIMARY KEY COLLATE {_PATH_COLLATION},"
            "stem TEXT NOT NULL,parent TEXT NOT NULL) WITHOUT ROWID"
        )
        connection.execute("DELETE FROM current_pdfs")
        stale_candidates = 0

        def current_pdf_rows():
            nonlocal stale_candidates
            for planned in self.framework_state.iter_route_candidates(self.run_id, PDF_MIME):
                self.cancellation.checkpoint()
                planned_path = Path(planned.path)
                stem = planned_path.stem.casefold()
                if stem not in candidate_stems:
                    continue
                try:
                    current = snapshot_path(planned.path)
                except (OSError, FileChangedError):
                    stale_candidates += 1
                    continue
                if current != planned:
                    stale_candidates += 1
                    continue
                yield (
                    current.path,
                    stem,
                    os.path.normcase(str(Path(current.path).parent)),
                )

        connection.executemany(
            "INSERT OR IGNORE INTO current_pdfs(path,stem,parent) VALUES(?,?,?)",
            current_pdf_rows(),
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS current_pdfs_stem_idx ON current_pdfs(stem,parent)"
        )
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS chosen_pairs("
            "docx_file_key TEXT NOT NULL,pdf_path TEXT NOT NULL,match_status TEXT NOT NULL,"
            "match_method TEXT NOT NULL,candidate_count INTEGER NOT NULL)"
        )
        connection.execute("DELETE FROM chosen_pairs")
        connection.execute(
            """INSERT INTO chosen_pairs
            SELECT d.file_key,p.path,
                CASE WHEN counts.n=1 THEN 'matched' ELSE 'ambiguous' END,
                CASE WHEN counts.n=1 THEN 'same_directory_stem'
                     ELSE 'multiple_stem_matches' END,
                counts.n
            FROM current_docx d
            JOIN (SELECT stem,parent,COUNT(*) AS n FROM current_pdfs
                  GROUP BY stem,parent) counts
              ON counts.stem=d.stem AND counts.parent=d.parent
            JOIN current_pdfs p ON p.stem=d.stem AND p.parent=d.parent"""
        )
        connection.execute(
            """INSERT INTO chosen_pairs
            SELECT d.file_key,p.path,
                CASE WHEN counts.n=1 THEN 'matched' ELSE 'ambiguous' END,
                CASE WHEN counts.n=1 THEN 'unique_stem'
                     ELSE 'multiple_stem_matches' END,
                counts.n
            FROM current_docx d
            JOIN (SELECT stem,COUNT(*) AS n FROM current_pdfs GROUP BY stem) counts
              ON counts.stem=d.stem
            JOIN current_pdfs p ON p.stem=d.stem
            WHERE NOT EXISTS(
                SELECT 1 FROM chosen_pairs selected
                WHERE selected.docx_file_key=d.file_key)"""
        )
        connection.execute("DELETE FROM pdf_counterparts")
        now = time.time_ns()
        connection.execute(
            """INSERT INTO pdf_counterparts
            SELECT docx_file_key,pdf_path,match_status,match_method,candidate_count,?,?
            FROM chosen_pairs""",
            (self.run_id, now),
        )
        connection.execute(
            """INSERT INTO pdf_counterparts
            SELECT d.file_key,'','missing','no_matching_stem',0,?,?
            FROM current_docx d WHERE NOT EXISTS(
                SELECT 1 FROM chosen_pairs p WHERE p.docx_file_key=d.file_key)""",
            (self.run_id, now),
        )
        matched = int(
            connection.execute(
                "SELECT COUNT(*) FROM pdf_counterparts WHERE match_status='matched'"
            ).fetchone()[0]
        )
        ambiguous = int(
            connection.execute(
                "SELECT COUNT(DISTINCT docx_file_key) FROM pdf_counterparts "
                "WHERE match_status='ambiguous'"
            ).fetchone()[0]
        )
        missing = int(
            connection.execute(
                "SELECT COUNT(*) FROM pdf_counterparts WHERE match_status='missing'"
            ).fetchone()[0]
        )
        return matched, ambiguous, missing, stale_candidates

    @staticmethod
    def _queue_diagnostic_reconciliation(
        reconciliations: list[ReviewCandidateReconciliation],
        snapshot: FileSnapshot,
        candidates: Iterable[ReviewCandidate],
        note: str,
    ) -> None:
        active = _review_reason_codes(candidates)
        reconciliations.append(
            ReviewCandidateReconciliation(
                snapshot=snapshot,
                resolution_note=note,
                evaluated_reason_codes=tuple(sorted(DOCX_REVIEW_REASON_CODES | active)),
                active_reason_codes=tuple(sorted(active)),
            )
        )

    def _flush_reviews(
        self,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        if review_batch:
            self.framework_state.store_review_candidates(
                self.run_id,
                tuple(review_batch),
            )
            review_batch.clear()
        if reconciliations:
            self.framework_state.reconcile_review_candidates_batch(
                self.run_id,
                "docx",
                tuple(reconciliations),
            )
            reconciliations.clear()

    def _consume_cached_candidate(
        self,
        connection,
        snapshot: FileSnapshot,
        cache_status: str,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _DocxCandidateOutcome | None:
        if cache_status not in {"complete", "partial", "cached_error"}:
            return None
        fts_documents_indexed = self._touch_cache_hit(connection, snapshot, cache_status)
        if fts_documents_indexed is None:
            return None
        if cache_status == "complete":
            self._queue_diagnostic_reconciliation(
                reconciliations,
                snapshot,
                (),
                "current DOCX cache has no degraded evidence",
            )
            return _DocxCandidateOutcome(
                cache_hits=1,
                fts_documents_indexed=fts_documents_indexed,
            )
        (
            cached_status,
            integrity_status,
            disposition,
            retryable,
            recovery_mode,
            diagnostics,
        ) = self._cached_review(connection, snapshot)
        cached_reviews = _review_candidates(
            snapshot,
            source_status=cached_status,
            integrity_status=integrity_status,
            disposition=disposition,
            retryable=retryable,
            diagnostics=diagnostics,
            detector_version=self.config.processing_signature,
            recovery_mode=recovery_mode,
        )
        review_batch.extend(cached_reviews)
        if cache_status == "partial":
            self._queue_diagnostic_reconciliation(
                reconciliations,
                snapshot,
                cached_reviews,
                "current partial DOCX cache diagnostics reconciled",
            )
            return _DocxCandidateOutcome(
                cache_hits=1,
                fts_documents_indexed=fts_documents_indexed,
                cached_partial_documents=1,
                review_candidates=1,
                deletion_candidates=int(disposition == "deletion_candidate"),
                retryable_errors=int(retryable),
            )
        return _DocxCandidateOutcome(
            cached_errors=1,
            review_candidates=1,
            deletion_candidates=int(disposition == "deletion_candidate"),
            retryable_errors=int(retryable),
        )

    def _extract_candidate(
        self,
        connection,
        snapshot: FileSnapshot,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _DocxCandidateOutcome:
        if not stat_matches_snapshot(
            snapshot,
            os.stat(native_io_path(snapshot.path), follow_symlinks=False),
        ):
            raise RuntimeError("DOCX metadata changed after inventory")
        result = extract_docx(
            snapshot.path,
            self.config.max_text_chars,
            self.memory_gate,
            self.cancellation,
        )
        if not stat_matches_snapshot(
            snapshot,
            os.stat(native_io_path(snapshot.path), follow_symlinks=False),
        ):
            raise RuntimeError("DOCX metadata changed during extraction")
        self._store_success(connection, snapshot, result)
        if result.status != "partial":
            self._queue_diagnostic_reconciliation(
                reconciliations,
                snapshot,
                (),
                "DOCX extraction completed without degraded evidence",
            )
            return _DocxCandidateOutcome(
                extracted=1,
                fts_documents_indexed=1,
                layouts=1,
            )
        current_reviews = _review_candidates(
            snapshot,
            source_status=result.status,
            integrity_status=result.integrity_status,
            disposition=result.review_disposition,
            retryable=False,
            diagnostics=result.diagnostics,
            detector_version=self.config.processing_signature,
            recovery_mode=result.recovery_mode,
        )
        review_batch.extend(current_reviews)
        self._queue_diagnostic_reconciliation(
            reconciliations,
            snapshot,
            current_reviews,
            "current partial DOCX diagnostics reconciled",
        )
        return _DocxCandidateOutcome(
            extracted=1,
            fts_documents_indexed=1,
            layouts=1,
            partial_documents=1,
            review_candidates=1,
        )

    def _failure_outcome(
        self,
        connection,
        snapshot: FileSnapshot,
        exc: Exception,
        review_batch: list[ReviewCandidate],
    ) -> _DocxCandidateOutcome:
        failure = self._store_error(connection, snapshot, exc)
        review_batch.extend(
            _review_candidates(
                snapshot,
                source_status="error",
                integrity_status=failure.integrity_status,
                disposition=failure.disposition,
                retryable=failure.retryable,
                diagnostics=failure.diagnostics,
                detector_version=self.config.processing_signature,
                recovery_mode="none",
            )
        )
        return _DocxCandidateOutcome(
            errors=1,
            review_candidates=1,
            deletion_candidates=int(failure.disposition == "deletion_candidate"),
            retryable_errors=int(failure.retryable),
        )

    def run(self) -> DocxRouteSummary:
        self.cancellation.checkpoint()
        self._recoverable_retry_keys.clear()
        total = eligible = selected_count = 0
        processed = cache_hits = cached_errors = extracted = errors = layouts = 0
        fts_documents_indexed = 0
        new_documents = retried_documents = 0
        partial_documents = cached_partial_documents = 0
        review_candidates = deletion_candidates = retryable_errors = 0
        review_batch: list[ReviewCandidate] = []
        review_reconciliations: list[ReviewCandidateReconciliation] = []

        def flush_reviews() -> None:
            self._flush_reviews(review_batch, review_reconciliations)

        def report(*, active: int = 0, finished: bool = False) -> None:
            emit_progress(
                self.progress,
                ProgressEvent(
                    "docx",
                    "extract",
                    "DOCX indexados" if finished else "Indexando DOCX",
                    processed,
                    selected_count,
                    "documentos",
                    finished,
                    (
                        ProgressMetric("cache_hits", cache_hits),
                        ProgressMetric("new_work", new_documents),
                        ProgressMetric("retries", retried_documents),
                        ProgressMetric("errors", errors),
                        ProgressMetric("in_flight", active),
                        ProgressMetric("remaining", max(0, selected_count - processed)),
                        ProgressMetric("cached_errors", cached_errors),
                        ProgressMetric("partial", partial_documents),
                        ProgressMetric("review", review_candidates),
                        ProgressMetric("completed_work", extracted),
                        ProgressMetric("memory_waits", self.memory_gate.wait_count),
                    ),
                ),
            )

        report()
        with docx_database(self.config.state_path) as connection:
            self._stage_inventory(connection)
            total, eligible = self._selected_counts(connection)
            selected_count = (
                eligible
                if self.config.max_documents is None
                else min(eligible, self.config.max_documents)
            )
            for snapshot in self._candidates(connection):
                self.cancellation.checkpoint()
                try:
                    cache_status = self._cache_status(connection, snapshot)
                    outcome = self._consume_cached_candidate(
                        connection,
                        snapshot,
                        cache_status,
                        review_batch,
                        review_reconciliations,
                    )
                    if outcome is None:
                        prior_reviewable = self._prior_reviewable(connection, snapshot)
                        if cache_status == "retry" or prior_reviewable:
                            retried_documents += 1
                        else:
                            new_documents += 1
                        report(active=1)
                        outcome = self._extract_candidate(
                            connection,
                            snapshot,
                            review_batch,
                            review_reconciliations,
                        )
                    cache_hits += outcome.cache_hits
                    cached_errors += outcome.cached_errors
                    extracted += outcome.extracted
                    fts_documents_indexed += outcome.fts_documents_indexed
                    errors += outcome.errors
                    layouts += outcome.layouts
                    partial_documents += outcome.partial_documents
                    cached_partial_documents += outcome.cached_partial_documents
                    review_candidates += outcome.review_candidates
                    deletion_candidates += outcome.deletion_candidates
                    retryable_errors += outcome.retryable_errors
                except CancellationRequested:
                    flush_reviews()
                    connection.commit()
                    raise
                except _LiveDocxCachePathConflict:
                    errors += 1
                except (
                    OSError,
                    FileChangedError,
                    RuntimeError,
                    ValueError,
                    zipfile.BadZipFile,
                    zlib.error,
                    ET.ParseError,
                    MemoryBudgetExceeded,
                    ZipStructureError,
                ) as exc:
                    outcome = self._failure_outcome(
                        connection,
                        snapshot,
                        exc,
                        review_batch,
                    )
                    errors += outcome.errors
                    review_candidates += outcome.review_candidates
                    deletion_candidates += outcome.deletion_candidates
                    retryable_errors += outcome.retryable_errors
                processed += 1
                if len(review_batch) >= DOCX_REVIEW_BATCH:
                    flush_reviews()
                if processed % DOCX_COMMIT_BATCH == 0:
                    flush_reviews()
                    connection.commit()
                report()

            flush_reviews()
            connection.commit()
            stale = 0 if self.config.selection.active else self._prune_stale_documents(connection)
            matched, ambiguous, missing, stale_pdfs = self._pair_pdfs(connection)
            connection.execute("DELETE FROM layout_groups")
            connection.execute(
                """INSERT INTO layout_groups(layout_signature,layout_class,member_count,representative_file_key,updated_run_id)
                SELECT layout_signature,MIN(layout_class),COUNT(*),MIN(file_key),?
                FROM documents WHERE status IN ('complete','partial')
                AND layout_signature IS NOT NULL
                GROUP BY layout_signature""",
                (self.run_id,),
            )
            groups = int(connection.execute("SELECT COUNT(*) FROM layout_groups").fetchone()[0])
        report(finished=True)
        count_skipped = max(0, eligible - selected_count)
        processing = self.config.processing_provenance
        return DocxRouteSummary(
            candidate_pool=total,
            candidates=selected_count,
            skipped_by_size=total - eligible,
            skipped_by_count=count_skipped,
            processed=processed,
            cache_hits=cache_hits,
            cached_errors=cached_errors,
            new_documents=new_documents,
            retried_documents=retried_documents,
            extracted=extracted,
            errors=errors,
            fts_documents_indexed=fts_documents_indexed,
            layouts_classified=layouts,
            layout_groups=groups,
            pdf_matched=matched,
            pdf_ambiguous=ambiguous,
            pdf_missing=missing,
            pdf_stale_candidates=stale_pdfs,
            cache_documents_pruned=int(stale),
            peak_reserved_bytes=self.memory_gate.peak_reserved_bytes,
            memory_waits=self.memory_gate.wait_count,
            partial_documents=partial_documents,
            cached_partial_documents=cached_partial_documents,
            review_candidates=review_candidates,
            deletion_candidates=deletion_candidates,
            retryable_errors=retryable_errors,
            processing_signature=processing.signature,
            processing_provenance=processing.manifest,
        )


# endregion [03]


# region [04] Direct queries


def search_docx_state(path: Path, query: str, limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 1000:
        raise ValueError("DOCX search limit must be between 1 and 1000")
    with docx_database(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT file_key,path,title,author,
            snippet(document_fts,4,'[',']',' … ',24) AS snippet,
            bm25(document_fts) AS rank FROM document_fts
            WHERE document_fts MATCH ? ORDER BY rank,path LIMIT ?""",
            (query, limit),
        )
        return [dict(row) for row in rows]


def list_docx_layout_groups(path: Path, limit: int = 20) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("DOCX layout group limit must be between 1 and 100")
    with docx_database(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT g.layout_signature,g.layout_class,g.member_count,d.path AS representative_path
            FROM layout_groups g JOIN documents d ON d.file_key=g.representative_file_key
            ORDER BY g.member_count DESC,g.layout_signature LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]


def list_missing_pdf_counterparts(path: Path, limit: int = 100) -> list[str]:
    if not 1 <= limit <= 1000:
        raise ValueError("DOCX missing-PDF limit must be between 1 and 1000")
    with docx_database(path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT d.path FROM pdf_counterparts p JOIN documents d ON d.file_key=p.docx_file_key
            WHERE p.match_status='missing' ORDER BY d.path LIMIT ?""",
            (limit,),
        )
        return [str(row[0]) for row in rows]


# endregion [04]


for _historical_symbol in (
    _LiveDocxCachePathConflict,
    _review_candidates,
    _review_reason_codes,
    _read_member,
    _compress_text,
    _part_kind,
    _estimated_docx_memory_bytes,
    _processing_error,
    _effective_recovery_error,
    _read_xml_root,
    _parse_word_member,
    _DocxExtractionAccumulator,
    _validate_docx_archive,
    _required_docx_members,
    _validate_docx_contract,
    _validate_docx_relationship,
    _extract_docx_parts,
    _extract_optional_docx_part,
    _docx_metadata,
    _build_extracted_docx,
    extract_docx,
    _DocxCandidateOutcome,
    DocxRoute,
    search_docx_state,
    list_docx_layout_groups,
    list_missing_pdf_counterparts,
):
    _historical_symbol.__module__ = "neocortex.capabilities.formats.docx.route"
del _historical_symbol
