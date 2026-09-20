"""Stable configuration and result contracts for the DOCX route."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    processing_provenance_cache,
    build_processing_provenance,
    distribution_component,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection


# region [01] Public route contracts

ALGORITHM_VERSION = "docx-route-v3"

DocxStatus: TypeAlias = Literal["complete", "partial", "error"]
DocxIntegrityStatus: TypeAlias = Literal[
    "valid",
    "degraded",
    "corrupt",
    "invalid",
    "unsupported",
    "unavailable",
    "policy_rejected",
    "unknown",
]
DocxReviewDisposition: TypeAlias = Literal[
    "none",
    "retry",
    "manual_review",
    "deletion_candidate",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class DocxRouteConfig:
    state_path: Path
    max_file_bytes: int | None = None
    max_documents: int | None = None
    max_text_chars: int = 20_000_000
    retry_errors: bool = False
    retry_recoverable_errors: bool = False
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    memory_budget_bytes: int = 512 * 1024 * 1024
    min_free_memory_bytes: int = 1024 * 1024 * 1024
    min_free_commit_bytes: int = 1024 * 1024 * 1024
    memory_wait_timeout_seconds: float | None = None

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return _docx_processing_provenance(self.max_text_chars)


@processing_provenance_cache(maxsize=64)
def _docx_processing_provenance(max_text_chars: int) -> ProcessingProvenance:
    return build_processing_provenance(
        "docx-route",
        ALGORITHM_VERSION,
        {"max_text_chars": max_text_chars},
        (
            python_runtime_component(),
            distribution_component("hashlib", "hashlib"),
        ),
        compatibility_tag=ALGORITHM_VERSION,
    )


@dataclass(frozen=True, slots=True)
class DocxRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    new_documents: int = 0
    retried_documents: int = 0
    extracted: int = 0
    errors: int = 0
    fts_documents_indexed: int = 0
    layouts_classified: int = 0
    layout_groups: int = 0
    pdf_matched: int = 0
    pdf_ambiguous: int = 0
    pdf_missing: int = 0
    pdf_stale_candidates: int = 0
    cache_documents_pruned: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    partial_documents: int = 0
    cached_partial_documents: int = 0
    review_candidates: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    catalog_candidates: int = 0
    catalog_classified: int = 0
    catalog_cache_hits: int = 0
    catalog_review_required: int = 0
    catalog_errors: int = 0
    catalog_source_stale: int = 0
    catalog_stale_marked: int = 0
    processing_signature: str | None = None
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA
    catalog_source_missing: int = field(default=0, kw_only=True)
    catalog_complete: bool | None = field(default=None, kw_only=True)


# endregion [01]


# region [02] Diagnostics and extracted OOXML values


@dataclass(frozen=True, slots=True)
class DocxDiagnostic:
    """Bounded, algorithm-stable evidence about one DOCX processing problem."""

    code: str
    message: str
    stage: str
    part_name: str | None = None
    required: bool = False
    retryable: bool = False
    disposition: DocxReviewDisposition = "manual_review"
    expected_size: int | None = None
    actual_size: int | None = None
    expected_crc32: int | None = None
    actual_crc32: int | None = None


@dataclass(frozen=True, slots=True)
class DocxFailure:
    """Normalized fatal outcome stored by the incremental route."""

    code: str
    message: str
    integrity_status: DocxIntegrityStatus
    retryable: bool
    disposition: DocxReviewDisposition
    diagnostics: tuple[DocxDiagnostic, ...] = ()


class DocxProcessingError(RuntimeError):
    """Fatal DOCX failure with stable classification and structured evidence."""

    def __init__(self, failure: DocxFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class DocxPart:
    name: str
    kind: str
    ordinal: int
    text_zlib: bytes
    text_chars: int


@dataclass(frozen=True, slots=True)
class ExtractedDocx:
    parts: tuple[DocxPart, ...]
    body: str
    metadata: dict[str, str]
    paragraph_count: int
    table_count: int
    image_count: int
    section_count: int
    layout_class: str
    layout_signature: str
    layout_json: str
    status: DocxStatus = "complete"
    integrity_status: DocxIntegrityStatus = "valid"
    review_disposition: DocxReviewDisposition = "none"
    recovery_mode: str = "none"
    diagnostics: tuple[DocxDiagnostic, ...] = ()


# endregion [02]


for _historical_symbol in (
    DocxRouteConfig,
    _docx_processing_provenance,
    DocxRouteSummary,
    DocxDiagnostic,
    DocxFailure,
    DocxProcessingError,
    DocxPart,
    ExtractedDocx,
):
    _historical_symbol.__module__ = "neocortex.capabilities.formats.docx.models"
del _historical_symbol
