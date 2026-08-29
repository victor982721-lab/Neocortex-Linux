"""Stable configuration and result contracts for the integrated PDF route."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    TesseractRuntimeProvenance,
    build_processing_provenance,
    distribution_component,
    executable_component,
    resolve_tesseract_runtime,
)
from _04_Nucleo_Operativo.ocr_profiles import OcrProfileName, resolve_ocr_profile
from _04_Nucleo_Operativo.route_filters import CandidateSelection


# region [01] Versioned limits

ALGORITHM_VERSION = "pdf-route-v3"
FAILURE_DETECTOR_VERSION = "pdf-failure-v3"
STRUCTURAL_RECOVERY_VERSION = "pdf-structural-recovery-v2"
PDF_PAGE_SEQUENCE_ERROR_LIMIT = 32
MIB = 1024 * 1024
PDF_BASE_WORKSPACE_BYTES = 128 * MIB
PDF_INTERPRETER_WORKSPACE_BYTES = 192 * MIB
PDF_OCR_PROCESS_TREE_BYTES = 384 * MIB
PDF_MIN_OCR_PROCESS_TREE_BYTES = 1024 * MIB
PDF_JOB_SAFETY_MARGIN_BYTES = 256 * MIB
PDF_RENDER_BYTES_PER_PIXEL = 16


# endregion [01]


# region [02] Public configuration and summary


@dataclass(frozen=True, slots=True)
class PdfRouteConfig:
    state_path: Path
    apply_actions: bool = False
    ocr_mode: Literal["auto", "never", "always"] = "auto"
    ocr_lang: str = "spa+eng"
    dpi: int = 200
    workers: int = 4
    ocr_workers: int = 2
    min_page_chars: int = 40
    max_page_text_chars: int = 5_000_000
    max_render_pixels: int = 40_000_000
    max_pages: int | None = None
    max_file_bytes: int | None = None
    max_documents: int | None = None
    max_ocr_pages: int | None = None
    ocr_timeout_seconds: int = 120
    retry_errors: bool = False
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    resume_source_run_id: int | None = None
    pdfminer_fallback: bool = True
    similarity_threshold: float = 0.92
    cache_validation: Literal["metadata", "full"] = "metadata"
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    fail_fast_pages: bool = False
    document_timeout_seconds: float | None = None
    timeout_mode: Literal["fixed", "adaptive"] = "adaptive"
    max_document_timeout_seconds: float = 1200.0
    min_free_bytes: int = 512 * 1024 * 1024
    memory_backpressure_bytes: int | None = None
    commit_backpressure_bytes: int | None = None
    memory_budget_bytes: int | None = None
    worker_memory_bytes: int = 512 * 1024 * 1024
    memory_wait_timeout_seconds: float = 60.0
    large_document_bytes: int = 128 * 1024 * 1024
    large_document_workers: int = 2
    ocr_profile: OcrProfileName = "configured"

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        return _pdf_processing_provenance(self)


def resolve_pdf_tesseract_runtime(
    config: PdfRouteConfig,
) -> TesseractRuntimeProvenance:
    """Preflight every pack that the selected bounded OCR profile may use.

    The underlying runtime probe is already cached by executable, tessdata,
    languages and timeout.  Keeping a second config-only cache here would hide
    an explicit processing-provenance cache refresh after model artifacts
    change in place.
    """

    plan = resolve_ocr_profile(config.ocr_profile, config.ocr_lang)
    return resolve_tesseract_runtime(
        command=config.tesseract_cmd,
        tessdata_dir=config.tessdata_dir,
        language=plan.required_language_spec,
        timeout_seconds=min(30.0, float(config.ocr_timeout_seconds)),
    )


@lru_cache(maxsize=128)
def _pdf_processing_provenance(config: PdfRouteConfig) -> ProcessingProvenance:
    profile = resolve_ocr_profile(config.ocr_profile, config.ocr_lang)
    components: list[dict[str, Any]] = [
        distribution_component("pymupdf", "PyMuPDF"),
        executable_component("qpdf", default_name="qpdf"),
    ]
    if config.pdfminer_fallback:
        components.append(distribution_component("pdfminer", "pdfminer.six"))
    if config.ocr_mode != "never":
        components.extend(
            (
                distribution_component("pillow", "Pillow"),
                distribution_component("pytesseract", "pytesseract"),
                resolve_pdf_tesseract_runtime(config).component,
            )
        )
    return build_processing_provenance(
        "pdf",
        (f"{ALGORITHM_VERSION}|{FAILURE_DETECTOR_VERSION}|{STRUCTURAL_RECOVERY_VERSION}"),
        {
            "document_timeout_seconds": config.document_timeout_seconds,
            "dpi": config.dpi,
            "fail_fast_pages": config.fail_fast_pages,
            "max_document_timeout_seconds": config.max_document_timeout_seconds,
            "max_ocr_pages": config.max_ocr_pages,
            "max_page_text_chars": config.max_page_text_chars,
            "max_pages": config.max_pages,
            "max_render_pixels": config.max_render_pixels,
            "min_page_chars": config.min_page_chars,
            "ocr_language": config.ocr_lang,
            "ocr_mode": config.ocr_mode,
            "ocr_profile": config.ocr_profile,
            "ocr_required_languages": profile.required_languages,
            "ocr_timeout_seconds": config.ocr_timeout_seconds,
            "page_end": config.page_end,
            "page_start": config.page_start,
            "pdfminer_fallback": config.pdfminer_fallback,
            "similarity_threshold": config.similarity_threshold,
            "tessdata_source": "explicit" if config.tessdata_dir else "default",
            "tesseract_source": "explicit" if config.tesseract_cmd else "path",
            "timeout_mode": config.timeout_mode,
        },
        components,
        compatibility_tag=ALGORITHM_VERSION,
    )


def effective_pdf_worker_memory_bytes(config: PdfRouteConfig) -> int:
    """Estimate the complete Python/MuPDF/Pillow/Tesseract working set."""

    text_bytes = config.max_page_text_chars * 4
    if config.ocr_mode == "never":
        return max(
            config.worker_memory_bytes,
            text_bytes + PDF_BASE_WORKSPACE_BYTES + PDF_INTERPRETER_WORKSPACE_BYTES,
        )
    render_bytes = config.max_render_pixels * PDF_RENDER_BYTES_PER_PIXEL
    return max(
        config.worker_memory_bytes,
        PDF_MIN_OCR_PROCESS_TREE_BYTES,
        render_bytes + text_bytes + PDF_OCR_PROCESS_TREE_BYTES + PDF_INTERPRETER_WORKSPACE_BYTES,
    )


def effective_pdf_job_memory_limit_bytes(config: PdfRouteConfig) -> int:
    """Return a hard Job limit with native-allocation safety headroom."""

    return effective_pdf_worker_memory_bytes(config) + PDF_JOB_SAFETY_MARGIN_BYTES


def effective_document_timeout_seconds(
    config: PdfRouteConfig,
    *,
    file_size: int,
    page_count: int = 0,
    pending_pages: int = 0,
) -> float | None:
    """Scale a durable document timeout from bounded, observable workload."""

    base = config.document_timeout_seconds
    if base is None or config.timeout_mode == "fixed":
        return base
    size_mib = max(0.0, file_size / MIB)
    page_work = max(page_count, pending_pages)
    size_seconds = min(480.0, size_mib * 1.5)
    page_seconds = min(600.0, page_work * 0.8)
    ocr_multiplier = 1.35 if config.ocr_mode != "never" else 1.0
    calculated = (base + size_seconds + page_seconds) * ocr_multiplier
    return round(
        min(config.max_document_timeout_seconds, max(base, calculated)),
        3,
    )


@dataclass(frozen=True, slots=True)
class PdfRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    new_documents: int = 0
    cache_refreshes: int = 0
    retried_documents: int = 0
    retry_pages_planned: int = 0
    extracted: int = 0
    errors: int = 0
    unrecoverable_recycled: int = 0
    protected: int = 0
    native_pages: int = 0
    ocr_pages: int = 0
    text_duplicate_groups: int = 0
    text_duplicate_candidates: int = 0
    text_duplicate_policy: Literal["advisory"] = "advisory"
    text_duplicates_trashed: int = 0
    text_duplicate_skips: int = 0
    fts_pages_indexed: int = 0
    text_signatures_built: int = 0
    profiles_built: int = 0
    profile_errors: int = 0
    text_similarity_pairs: int = 0
    template_similarity_pairs: int = 0
    layout_similarity_pairs: int = 0
    layout_groups: int = 0
    layout_pages_mapped: int = 0
    partial_documents: int = 0
    page_errors: int = 0
    document_timeouts: int = 0
    warning_documents: int = 0
    mupdf_warnings: int = 0
    pdf_cache_documents_pruned: int = 0
    pdf_cache_rows_pruned: int = 0
    fts_rows_repaired: int = 0
    effective_worker_memory_bytes: int = 0
    memory_waits: int = 0
    resumed_from_run_id: int | None = None
    extraction_phase_skipped: bool = False
    text_dedup_phase_skipped: bool = False
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


# endregion [02]


# region [03] Internal per-document results


@dataclass(frozen=True, slots=True)
class DocumentResult:
    status: Literal["done", "partial", "protected", "error"]
    native_pages: int = 0
    ocr_pages: int = 0
    page_errors: int = 0
    timed_out: bool = False
    warning_count: int = 0
    transient: bool = False
    recycled: bool = False


@dataclass(frozen=True, slots=True)
class CacheDecision:
    hit: bool
    prior_status: str | None = None
    retry_pages: int = 0
    error_type: str | None = None
    error_message: str | None = None
    metadata_json: str | None = None
    page_error_type: str | None = None
    page_error_message: str | None = None

    def __bool__(self) -> bool:
        return self.hit


# endregion [03]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_route_models")
