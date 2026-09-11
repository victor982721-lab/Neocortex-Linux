"""Image route configuration and results without loading image decoders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from neocortex.foundation.processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    build_processing_provenance,
    distribution_component,
)
from neocortex.safety.ocr_profiles import OcrProfileName
from neocortex.safety.route_filters import CandidateSelection

from .document import (
    DocumentVerifierConfig,
    DocumentVerifierRuntime,
    resolve_document_verifier,
)
from .policy import ANALYSIS_VERSION
from .visual import DEFAULT_VISUAL_CLASSIFIER


IMAGE_ROUTE_VERSION = "image-route-v7-no-nudenet"


@dataclass(frozen=True, slots=True)
class ImageRouteConfig:
    state_path: Path
    root: Path
    workers: int = 4
    max_file_bytes: int | None = None
    max_documents: int | None = None
    retry_errors: bool = False
    retry_recoverable_errors: bool = False
    selection: CandidateSelection = field(default_factory=CandidateSelection)
    memory_budget_bytes: int = 512 * 1024 * 1024
    min_free_memory_bytes: int = 1024 * 1024 * 1024
    min_free_commit_bytes: int = 1024 * 1024 * 1024
    memory_wait_timeout_seconds: float = 60.0
    worker_timeout_seconds: float = 120.0
    isolate_decoders: bool = True
    document_ocr_mode: Literal["auto", "never"] = "auto"
    document_ocr_lang: str = "spa+eng"
    document_ocr_timeout_seconds: float = 12.0
    tesseract_cmd: str | None = None
    tessdata_dir: str | None = None
    document_ocr_profile: OcrProfileName = "configured"

    @property
    def processing_signature(self) -> str:
        return self.processing_provenance.signature

    @property
    def processing_provenance(self) -> ProcessingProvenance:
        verifier = resolve_document_verifier(_document_verifier_config(self))
        return _image_processing_provenance(self, verifier)


def _document_verifier_config(config: ImageRouteConfig) -> DocumentVerifierConfig:
    return DocumentVerifierConfig(
        mode=config.document_ocr_mode,
        lang=config.document_ocr_lang,
        timeout_seconds=config.document_ocr_timeout_seconds,
        tesseract_cmd=config.tesseract_cmd,
        tessdata_dir=config.tessdata_dir,
        profile=config.document_ocr_profile,
    )


def _image_processing_provenance(
    config: ImageRouteConfig,
    verifier: DocumentVerifierRuntime,
) -> ProcessingProvenance:
    try:
        ocr_manifest = (
            json.loads(verifier.processing_provenance_json)
            if verifier.processing_provenance_json
            else None
        )
    except (TypeError, ValueError):
        ocr_manifest = None
    ocr_component: dict[str, Any] = {
        "name": "document-ocr",
        "kind": "processing-pipeline",
        "status": (
            "disabled"
            if config.document_ocr_mode == "never"
            else "available"
            if verifier.enabled
            else "unavailable"
        ),
        "signature": verifier.signature,
    }
    if ocr_manifest is not None:
        ocr_component["manifest"] = ocr_manifest
    return build_processing_provenance(
        "image",
        f"{IMAGE_ROUTE_VERSION}|{ANALYSIS_VERSION}",
        {
            "document_ocr_language": config.document_ocr_lang,
            "document_ocr_mode": config.document_ocr_mode,
            "document_ocr_profile": config.document_ocr_profile,
            "document_ocr_timeout_seconds": config.document_ocr_timeout_seconds,
        },
        (
            distribution_component("pillow", "Pillow"),
            {
                "name": "visual-classifier",
                "kind": "classifier",
                "signature": DEFAULT_VISUAL_CLASSIFIER.signature,
            },
            ocr_component,
        ),
        compatibility_tag=ANALYSIS_VERSION.split("|", 1)[0],
    )


@dataclass(frozen=True, slots=True)
class ImageRouteSummary:
    processing_signature: str | None = None
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    feature_cache_hits: int = 0
    cached_errors: int = 0
    new_images: int = 0
    retried_images: int = 0
    reclassified_images: int = 0
    classified: int = 0
    document_candidates: int = 0
    photo_candidates: int = 0
    industrial_context_candidates: int = 0
    errors: int = 0
    document_ocr_attempts: int = 0
    document_ocr_positive: int = 0
    document_ocr_failures: int = 0
    document_verifier_available: bool = False
    document_verifier_provenance: str | None = None
    recovered_decodes: int = 0
    retryable_errors: int = 0
    manual_review_errors: int = 0
    deletion_candidates: int = 0
    review_candidates_stored: int = 0
    cache_rows_pruned: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    full_fingerprint_cache_hits: int = 0
    full_fingerprints_computed: int = 0
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA
    # Appended keyword-only fields keep older positional consumers compatible.
    catalog_candidates: int = field(default=0, kw_only=True)
    catalog_classified: int = field(default=0, kw_only=True)
    catalog_cache_hits: int = field(default=0, kw_only=True)
    catalog_review_required: int = field(default=0, kw_only=True)
    catalog_errors: int = field(default=0, kw_only=True)
    catalog_source_stale: int = field(default=0, kw_only=True)
    catalog_stale_marked: int = field(default=0, kw_only=True)
    catalog_source_missing: int = field(default=0, kw_only=True)
    catalog_complete: bool | None = field(default=None, kw_only=True)
