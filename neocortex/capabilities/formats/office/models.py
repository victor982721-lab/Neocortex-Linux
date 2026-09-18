"""Stable contracts and bounds for bounded Office extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    processing_provenance_cache,
    build_processing_provenance,
    distribution_component,
    python_runtime_component,
)
from neocortex.safety.route_filters import CandidateSelection

OFFICE_ROUTE_VERSION = "office-route-v2"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT_MIME = "application/vnd.oasis.opendocument.text"
OFFICE_MIME_FORMATS: Mapping[str, Literal["xlsx", "pptx", "odt"]] = {
    XLSX_MIME: "xlsx",
    PPTX_MIME: "pptx",
    ODT_MIME: "odt",
}
MAX_ZIP_MEMBERS = 20_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_CORE_PROPERTIES_BYTES = 2 * 1024 * 1024
MAX_XLSX_CELLS = 250_000
MAX_XLSX_SHARED_STRINGS = 250_000
OFFICE_COMMIT_BATCH = 16
OFFICE_REVIEW_REASON_CODES = frozenset(
    {
        "office_corrupt_container",
        "office_duplicate_cell_reference",
        "office_duplicate_member",
        "office_encrypted_member",
        "office_invalid_cell_reference",
        "office_invalid_shared_string",
        "office_io_error",
        "office_member_limit",
        "office_metadata_limit",
        "office_missing_required_part",
        "office_shared_string_limit",
        "office_source_changed",
        "office_text_limit",
        "office_uncompressed_limit",
        "office_unsafe_member_name",
        "office_xlsx_cell_limit",
    }
)
ReviewRecommendation = Literal[
    "retry",
    "keep_protected",
    "manual_review",
    "deletion_candidate",
]


@dataclass(frozen=True, slots=True)
class OfficeRouteConfig:
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
    memory_wait_timeout_seconds: float = 60.0

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return _office_processing_provenance(self.max_text_chars)


@processing_provenance_cache(maxsize=64)
def _office_processing_provenance(max_text_chars: int) -> ProcessingProvenance:
    return build_processing_provenance(
        "office-route",
        OFFICE_ROUTE_VERSION,
        {"max_text_chars": max_text_chars},
        (
            python_runtime_component(),
            distribution_component("xxhash", "xxhash"),
        ),
        compatibility_tag=OFFICE_ROUTE_VERSION,
    )


@dataclass(frozen=True, slots=True)
class OfficeRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    extracted: int = 0
    errors: int = 0
    cache_documents_pruned: int = 0
    review_candidates: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
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


@dataclass(frozen=True, slots=True)
class XlsxCell:
    workbook: str
    sheet: str
    sheet_ordinal: int
    cell_reference: str
    cell_type: str
    value: str
    raw_value: str | None = None
    formula: str | None = None
    cached_value: str | None = None
    style_index: int | None = None
    number_format: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedOfficeDocument:
    format: str
    title: str
    author: str
    subject: str
    text: str
    part_count: int
    xlsx_cells: tuple[XlsxCell, ...] = ()


class OfficeExtractionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recommendation: ReviewRecommendation,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recommendation: ReviewRecommendation = recommendation
        self.retryable = retryable


for _name in (
    "OfficeRouteConfig",
    "OfficeRouteSummary",
    "XlsxCell",
    "ExtractedOfficeDocument",
    "OfficeExtractionError",
):
    globals()[_name].__module__ = "neocortex.capabilities.formats.office.route"
del _name
