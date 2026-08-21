"""Incremental, memory-bounded image classification route."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Protocol, cast

from neocortex.deduplication import (
    FULL_ALGORITHM,
    FileSnapshot,
    full_fingerprint,
    snapshot_path,
)
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from ....cancellation import CancellationRequested, CancellationToken
from .analysis import (
    ANALYSIS_VERSION,
    DEFAULT_VISUAL_CLASSIFIER,
    AdultContentEvidence,
    Decision,
    Features,
    ImageMemoryGate,
    ImageResourceLimits,
    cached_features_are_compatible,
    classify,
    estimated_image_memory_bytes,
    requires_document_verification,
)
from .adult import (
    ADULT_ANALYSIS_VERSION,
    ADULT_MODEL_PHYSICAL_BYTES,
    ADULT_POLICY_VERSION,
    DEFAULT_ADULT_CLASSIFIER,
    adult_model_component,
    is_adult_model_candidate,
)
from .document import (
    DocumentTextEvidence,
    DocumentVerifierConfig,
    DocumentVerifierRuntime,
    resolve_document_verifier,
)
from .errors import (
    ErrorDisposition,
    ImageFailure,
    classify_image_failure,
    refine_image_failure,
)
from .isolation import (
    ImageWorkerSupervisor,
    image_worker_memory_reservation,
)
from .state import (
    EncodedOcrText,
    candidate_counts,
    file_key,
    initialize_image_state,
    iter_candidates,
    prepare_ocr_text_storage,
    prune_missing,
    snapshot_from_row,
    stage_inventory_batch,
    store_error_batch,
    store_success_batch,
)
from ....memory_runtime import MemoryBudgetExceeded, MemoryHeadroomTimeout
from ....ocr_profiles import OcrProfileName
from ....processing_provenance import (
    ROUTE_SUMMARY_SCHEMA,
    ProcessingProvenance,
    build_processing_provenance,
    distribution_component,
)
from ....review import ReviewCandidate
from ....route_filters import CandidateSelection
from ....state import ReviewCandidateReconciliation


class ImageRouteState(Protocol):
    """Structural state contract for integrated image candidate streams."""

    def iter_route_candidates_by_prefix(
        self,
        run_id: int,
        mime_prefix: str,
    ) -> Any: ...

    def iter_selected_route_candidates_by_prefix(
        self,
        run_id: int,
        mime_prefix: str,
        route_name: str,
        selection: CandidateSelection,
    ) -> Any: ...

    def store_review_candidates(
        self,
        run_id: int,
        candidates: Iterable[ReviewCandidate],
    ) -> None: ...

    def reconcile_review_candidates_batch(
        self,
        run_id: int,
        route_name: str,
        reconciliations: Iterable[ReviewCandidateReconciliation],
    ) -> int: ...


class ImageFingerprintIndex(Protocol):
    """Minimal durable full-fingerprint cache used by Semantic planning."""

    def cached_fingerprint(self, snapshot: FileSnapshot, algorithm: str) -> bytes | None: ...

    def store_fingerprint(
        self,
        snapshot: FileSnapshot,
        algorithm: str,
        digest: bytes,
    ) -> None: ...


# region [01] Configuration and results

INVENTORY_BATCH_SIZE = 1000
RESULT_BATCH_SIZE = 64
IMAGE_ROUTE_VERSION = "image-route-v6"
IMAGE_SUCCESS_REVIEW_REASON_CODES = frozenset(
    {
        "image_raster_document_candidate",
        "image_recovered_truncated_decode",
        "image_explicit_adult_content",
        "image_adult_content_requires_review",
    }
)
IMAGE_FAILURE_REVIEW_REASON_CODES = frozenset(
    {
        "image_analysis_failure",
        "image_container_integrity_failure",
        "image_decode_failure",
        "image_input_access_failure",
        "image_resource_admission_failure",
        "image_source_validation_failure",
        "image_worker_supervision_failure",
        "image_worker_timeout_failure",
    }
)
# A completed current-signature analysis evaluated every bounded image detector
# and traversed every failure phase.  This explicit set prevents one detector
# (for example, raster-document evidence) from resolving an unrelated adult
# finding while still retiring genuinely stale results from older generations.
IMAGE_COMPLETE_REVIEW_REASON_CODES = (
    IMAGE_SUCCESS_REVIEW_REASON_CODES | IMAGE_FAILURE_REVIEW_REASON_CODES
)


@dataclass(frozen=True, slots=True)
class ImageRouteConfig:
    state_path: Path
    root: Path
    workers: int = 4
    max_file_bytes: int | None = None
    max_documents: int | None = None
    retry_errors: bool = False
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
        f"{IMAGE_ROUTE_VERSION}|{ANALYSIS_VERSION}|{ADULT_ANALYSIS_VERSION}",
        {
            "document_ocr_language": config.document_ocr_lang,
            "document_ocr_mode": config.document_ocr_mode,
            "document_ocr_profile": config.document_ocr_profile,
            "document_ocr_timeout_seconds": config.document_ocr_timeout_seconds,
        },
        (
            distribution_component("pillow", "Pillow"),
            adult_model_component(),
            {
                "name": "visual-classifier",
                "kind": "classifier",
                "signature": DEFAULT_VISUAL_CLASSIFIER.signature,
            },
            ocr_component,
        ),
        compatibility_tag=ANALYSIS_VERSION.split("|", 1)[0],
    )


def _ocr_language_packs_missing(verifier: DocumentVerifierRuntime) -> bool:
    reason = verifier.unavailable_reason or ""
    return reason.startswith("missing OCR languages:")


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
    adult_heuristic_candidates: int = 0
    adult_analyzed: int = 0
    adult_explicit: int = 0
    adult_ambiguous: int = 0
    adult_unavailable: int = 0
    adult_recycled: int = 0
    adult_recycle_failed: int = 0
    adult_recycle_protected: int = 0
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


@dataclass(frozen=True, slots=True)
class _AnalysisResult:
    key: str
    snapshot: FileSnapshot
    decision: Decision | None
    feature_cache_used: bool = False
    failure: ImageFailure | None = None


@dataclass(frozen=True, slots=True)
class _ImageCounterDelta:
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    classified: int = 0
    errors: int = 0
    recovered_decodes: int = 0
    retryable_errors: int = 0
    manual_review_errors: int = 0
    deletion_candidates: int = 0
    document_ocr_attempts: int = 0
    document_ocr_positive: int = 0
    document_ocr_failures: int = 0
    document_candidates: int = 0
    photo_candidates: int = 0
    industrial_context_candidates: int = 0
    adult_heuristic_candidates: int = 0
    adult_analyzed: int = 0
    adult_explicit: int = 0
    adult_ambiguous: int = 0
    adult_unavailable: int = 0


@dataclass(slots=True)
class _ImageWorkState:
    work_submitted: int = 0
    feature_cache_hits: int = 0
    new_images: int = 0
    retried_images: int = 0
    reclassified_images: int = 0
    pending: set[Future[_AnalysisResult]] = field(default_factory=set)


# endregion [01]


# region [02] Route orchestration
# Consume the common detected inventory and never walk the filesystem again.


class ImageRoute:
    def __init__(
        self,
        config: ImageRouteConfig,
        framework_state: ImageRouteState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
        dedup_index: ImageFingerprintIndex | None = None,
    ):
        if config.workers < 1:
            raise ValueError("image workers must be positive")
        if config.max_file_bytes is not None and config.max_file_bytes < 1:
            raise ValueError("image max_file_bytes must be positive")
        if config.max_documents is not None and config.max_documents < 1:
            raise ValueError("image max_documents must be positive")
        if config.worker_timeout_seconds <= 0:
            raise ValueError("image worker timeout must be positive")
        self.config = config
        self.framework_state = framework_state
        self.run_id = run_id
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()
        self.dedup_index = dedup_index
        self._full_fingerprint_cache_hits = 0
        self._full_fingerprints_computed = 0
        self._worker_local = threading.local()
        self._supervisor_lock = threading.Lock()
        self._supervisors: set[ImageWorkerSupervisor] = set()
        self.document_verifier = resolve_document_verifier(_document_verifier_config(config))
        if (
            config.document_ocr_mode != "never"
            and not self.document_verifier.enabled
            and (
                config.document_ocr_profile != "configured"
                or _ocr_language_packs_missing(self.document_verifier)
            )
        ):
            raise RuntimeError(
                "image OCR profile preflight failed: "
                f"{self.document_verifier.unavailable_reason or 'runtime unavailable'}"
            )
        self.processing_provenance = _image_processing_provenance(
            config,
            self.document_verifier,
        )
        self.processing_signature = self.processing_provenance.signature
        self.memory_gate = (
            memory_gate
            if memory_gate is not None
            else ImageMemoryGate(
                ImageResourceLimits(
                    memory_budget_bytes=config.memory_budget_bytes,
                    min_free_memory_bytes=config.min_free_memory_bytes,
                    min_free_commit_bytes=config.min_free_commit_bytes,
                    wait_timeout_seconds=config.memory_wait_timeout_seconds,
                ),
                self.cancellation,
            )
        )
        initialize_image_state(config.state_path)

    def _ensure_full_fingerprint(self, snapshot: FileSnapshot) -> None:
        """Persist the exact image bytes identity already required by Semantic."""

        if self.dedup_index is None:
            return
        cached = self.dedup_index.cached_fingerprint(snapshot, FULL_ALGORITHM)
        if cached is not None:
            if len(cached) != 16:
                raise RuntimeError("cached image full fingerprint has an invalid length")
            self._full_fingerprint_cache_hits += 1
            return
        digest = full_fingerprint(snapshot)
        self.dedup_index.store_fingerprint(snapshot, FULL_ALGORITHM, digest)
        self._full_fingerprints_computed += 1

    def _cached_row_delta(
        self,
        row: Any,
        retry_selected: bool,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _ImageCounterDelta | None:
        signature_matches = row["processing_signature"] == self.processing_signature
        if row["status"] == "done" and signature_matches:
            snapshot = snapshot_from_row(row)
            self._ensure_full_fingerprint(snapshot)
            cached_reviews = _cached_success_review_candidates(row, snapshot)
            review_batch.extend(cached_reviews)
            reconciliations.append(
                _successful_review_reconciliation(
                    snapshot,
                    cached_reviews,
                    "current image cache review evidence reconciled",
                )
            )
            return _ImageCounterDelta(
                processed=1,
                cache_hits=1,
                document_candidates=int(bool(row["document_candidate"])),
                photo_candidates=int(row["category"] == "foto"),
                industrial_context_candidates=int(
                    _semantic_json_has_evidence(row["semantic_json"])
                ),
                adult_heuristic_candidates=int(bool(row["adult_candidate"])),
                adult_analyzed=int(bool(row["adult_analyzed"])),
                adult_explicit=int(row["adult_classification"] == "explicit"),
                adult_ambiguous=int(row["adult_classification"] == "ambiguous"),
                adult_unavailable=int(row["adult_classification"] == "unavailable"),
                deletion_candidates=int(row["adult_classification"] == "explicit"),
                recovered_decodes=int(row["decode_quality"] == "recovered_truncated"),
            )
        if row["status"] != "error" or not signature_matches or retry_selected:
            return None
        snapshot = snapshot_from_row(row)
        failure = _cached_failure(row)
        review_batch.append(_error_review_candidate(snapshot, failure))
        return _ImageCounterDelta(
            processed=1,
            cached_errors=1,
            retryable_errors=int(failure.retryable),
            manual_review_errors=int(failure.disposition == "manual_review"),
            deletion_candidates=int(failure.disposition == "deletion_candidate"),
        )

    @staticmethod
    def _cached_batch_ready(
        delta: _ImageCounterDelta,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> bool:
        if delta.cache_hits:
            return len(review_batch) + len(reconciliations) >= RESULT_BATCH_SIZE
        return len(review_batch) >= RESULT_BATCH_SIZE

    def _analysis_result_delta(
        self,
        result: _AnalysisResult,
        success_batch: list[tuple],
        error_batch: list[tuple],
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _ImageCounterDelta:
        now = time.time_ns()
        if result.decision is None:
            return self._failed_analysis_delta(
                result,
                now,
                error_batch,
                review_batch,
            )
        return self._successful_analysis_delta(
            result,
            now,
            success_batch,
            review_batch,
            reconciliations,
        )

    def _failed_analysis_delta(
        self,
        result: _AnalysisResult,
        now: int,
        error_batch: list[tuple],
        review_batch: list[ReviewCandidate],
    ) -> _ImageCounterDelta:
        failure = result.failure or ImageFailure(
            "UnknownError",
            "unknown image error",
            "analysis",
            False,
            "manual_review",
        )
        error_batch.append(
            (
                self.processing_signature,
                failure.error_type,
                failure.message,
                failure.phase,
                int(failure.retryable),
                failure.disposition,
                failure.provenance,
                now,
                result.key,
            )
        )
        review_batch.append(_error_review_candidate(result.snapshot, failure))
        return _ImageCounterDelta(
            processed=1,
            errors=1,
            retryable_errors=int(failure.retryable),
            manual_review_errors=int(failure.disposition == "manual_review"),
            deletion_candidates=int(failure.disposition == "deletion_candidate"),
        )

    def _successful_analysis_delta(
        self,
        result: _AnalysisResult,
        now: int,
        success_batch: list[tuple],
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _ImageCounterDelta:
        decision = result.decision
        assert decision is not None
        adult = decision.adult_content or AdultContentEvidence(
            candidate=False,
            analyzed=False,
            classification="not_analyzed",
            confidence=0.0,
            detections=(),
            evidence=("adult_evidence_missing",),
            provenance=(ADULT_POLICY_VERSION,),
        )
        stored_ocr_text = _prepare_decision_ocr_text(decision.document_text)
        success_batch.append(
            self._success_storage_row(result, decision, adult, stored_ocr_text, now)
        )
        success_reviews = _success_review_candidates(result.snapshot, decision)
        review_batch.extend(success_reviews)
        reconciliations.append(
            _successful_review_reconciliation(
                result.snapshot,
                success_reviews,
                "current image detectors completed successfully",
            )
        )
        document_text = decision.document_text
        attempted = int(document_text.attempted) if document_text is not None else 0
        positive = int(document_text.dense_text) if document_text is not None else 0
        ocr_failure = int(
            document_text is not None and document_text.attempted and not document_text.available
        )
        return _ImageCounterDelta(
            processed=1,
            classified=1,
            document_candidates=int(decision.document_candidate.is_candidate),
            photo_candidates=int(decision.category == "foto"),
            recovered_decodes=int(decision.features.decode_quality == "recovered_truncated"),
            industrial_context_candidates=int(decision.industrial_context.has_evidence),
            adult_heuristic_candidates=int(adult.candidate),
            adult_analyzed=int(adult.analyzed),
            adult_explicit=int(adult.classification == "explicit"),
            adult_ambiguous=int(adult.classification == "ambiguous"),
            adult_unavailable=int(adult.classification == "unavailable"),
            deletion_candidates=int(adult.classification == "explicit"),
            document_ocr_attempts=attempted,
            document_ocr_positive=positive,
            document_ocr_failures=ocr_failure,
        )

    def _success_storage_row(
        self,
        result: _AnalysisResult,
        decision: Decision,
        adult: AdultContentEvidence,
        stored_ocr_text: EncodedOcrText,
        now: int,
    ) -> tuple:
        return (
            self.processing_signature,
            decision.category,
            decision.confidence,
            decision.confidence_kind,
            decision.winner_score,
            decision.runner_up,
            decision.runner_up_score,
            decision.score_margin,
            int(decision.document_candidate.is_candidate),
            decision.document_candidate.heuristic_score,
            decision.document_candidate.uncertainty,
            int(adult.candidate),
            int(adult.analyzed),
            adult.classification,
            adult.confidence,
            "|".join(adult.provenance),
            json.dumps(asdict(adult), ensure_ascii=True),
            stored_ocr_text.compressed,
            stored_ocr_text.characters,
            stored_ocr_text.xxh3_128,
            int(stored_ocr_text.truncated),
            decision.features.decode_quality,
            decision.features.decode_provenance,
            json.dumps(asdict(decision.features), ensure_ascii=True),
            (
                json.dumps(asdict(decision.photo_attributes), ensure_ascii=True)
                if decision.photo_attributes is not None
                else None
            ),
            json.dumps(asdict(decision.industrial_context), ensure_ascii=True),
            json.dumps(
                {
                    "reasons": decision.reasons,
                    "decode": {
                        "quality": decision.features.decode_quality,
                        "provenance": decision.features.decode_provenance,
                    },
                    "document_candidate": asdict(decision.document_candidate),
                    "document_text": _document_text_metadata(
                        decision.document_text,
                        stored_ocr_text,
                    ),
                    "visual_semantics": asdict(decision.visual_semantics),
                    "adult_content": asdict(adult),
                },
                ensure_ascii=True,
            ),
            now,
            result.key,
        )

    def _execute_rows(
        self,
        rows: Iterator[Any],
        retry_selected: bool,
        selected_work: int,
        work: _ImageWorkState,
        success_batch: list[tuple],
        error_batch: list[tuple],
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
        apply_delta: Callable[[_ImageCounterDelta], None],
        flush_results: Callable[[], None],
        report: Callable[[], None],
    ) -> None:
        with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            exhausted = False
            while work.pending or not exhausted:
                self.cancellation.checkpoint()
                exhausted = self._fill_work_queue(
                    rows,
                    retry_selected,
                    selected_work,
                    exhausted,
                    executor,
                    work,
                    review_batch,
                    reconciliations,
                    apply_delta,
                    flush_results,
                    report,
                )
                if not work.pending:
                    continue
                completed, work.pending = wait(
                    work.pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue
                self._consume_completed(
                    completed,
                    success_batch,
                    error_batch,
                    review_batch,
                    reconciliations,
                    apply_delta,
                    flush_results,
                    report,
                )

    def _fill_work_queue(
        self,
        rows: Iterator[Any],
        retry_selected: bool,
        selected_work: int,
        exhausted: bool,
        executor: ThreadPoolExecutor,
        work: _ImageWorkState,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
        apply_delta: Callable[[_ImageCounterDelta], None],
        flush_results: Callable[[], None],
        report: Callable[[], None],
    ) -> bool:
        while not exhausted and len(work.pending) < self.config.workers * 2:
            self.cancellation.checkpoint()
            try:
                row = next(rows)
            except StopIteration:
                return True
            self._consume_candidate_row(
                row,
                retry_selected,
                selected_work,
                executor,
                work,
                review_batch,
                reconciliations,
                apply_delta,
                flush_results,
                report,
            )
        return exhausted

    def _consume_candidate_row(
        self,
        row: Any,
        retry_selected: bool,
        selected_work: int,
        executor: ThreadPoolExecutor,
        work: _ImageWorkState,
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
        apply_delta: Callable[[_ImageCounterDelta], None],
        flush_results: Callable[[], None],
        report: Callable[[], None],
    ) -> None:
        cached_delta = self._cached_row_delta(
            row,
            retry_selected,
            review_batch,
            reconciliations,
        )
        if cached_delta is not None:
            apply_delta(cached_delta)
            if self._cached_batch_ready(
                cached_delta,
                review_batch,
                reconciliations,
            ):
                flush_results()
            report()
            return
        if work.work_submitted >= selected_work:
            return
        work.work_submitted += 1
        snapshot = snapshot_from_row(row)
        self._ensure_full_fingerprint(snapshot)
        cached_features = _cached_features_from_row(row)
        if cached_features is not None:
            work.feature_cache_hits += 1
        if row["status"] == "error":
            work.retried_images += 1
        elif row["processing_signature"] is None:
            work.new_images += 1
        else:
            work.reclassified_images += 1
        work.pending.add(
            executor.submit(
                self._analyze,
                snapshot,
                cached_features,
            )
        )
        report()

    def _consume_completed(
        self,
        completed: set[Future[_AnalysisResult]],
        success_batch: list[tuple],
        error_batch: list[tuple],
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
        apply_delta: Callable[[_ImageCounterDelta], None],
        flush_results: Callable[[], None],
        report: Callable[[], None],
    ) -> None:
        for future in completed:
            try:
                result = future.result()
            except CancellationRequested:
                raise
            except MemoryError:
                flush_results()
                raise
            apply_delta(
                self._analysis_result_delta(
                    result,
                    success_batch,
                    error_batch,
                    review_batch,
                    reconciliations,
                )
            )
            if (
                len(success_batch) + len(error_batch) + len(review_batch) + len(reconciliations)
                >= RESULT_BATCH_SIZE
            ):
                flush_results()
            report()

    def _flush_result_batches(
        self,
        success_batch: list[tuple],
        error_batch: list[tuple],
        review_batch: list[ReviewCandidate],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> int:
        stored_reviews = len(review_batch)
        if success_batch:
            store_success_batch(self.config.state_path, tuple(success_batch))
            success_batch.clear()
        if error_batch:
            store_error_batch(self.config.state_path, tuple(error_batch))
            error_batch.clear()
        if review_batch:
            self.framework_state.store_review_candidates(
                self.run_id,
                tuple(review_batch),
            )
            review_batch.clear()
        if reconciliations:
            self.framework_state.reconcile_review_candidates_batch(
                self.run_id,
                "image",
                tuple(reconciliations),
            )
            reconciliations.clear()
        return stored_reviews

    def run(self) -> ImageRouteSummary:
        self.cancellation.checkpoint()
        self._stage_inventory()
        retry_selected = self.config.retry_errors or self.config.selection.force_incomplete_retry
        candidate_pool, eligible = candidate_counts(
            self.config.state_path,
            self.run_id,
            self.config.max_file_bytes,
            self.config.selection,
        )
        selection_total = min(
            eligible,
            self.config.max_documents if self.config.max_documents is not None else eligible,
        )
        # ``max_documents`` is a hard candidate limit, not merely a limit on
        # decoder work.  This also bounds full-byte fingerprinting introduced
        # for Semantic when otherwise-current cache rows do not have one yet.
        selected_work = selection_total
        processed = cache_hits = cached_errors = 0
        work = _ImageWorkState()
        classified = errors = 0
        recovered_decodes = retryable_errors = manual_review_errors = 0
        deletion_candidates = review_candidates_stored = 0
        document_ocr_attempts = document_ocr_positive = document_ocr_failures = 0
        document_candidates = photo_candidates = industrial_context_candidates = 0
        adult_heuristic_candidates = adult_analyzed = 0
        adult_explicit = adult_ambiguous = adult_unavailable = 0
        success_batch: list[tuple] = []
        error_batch: list[tuple] = []
        review_batch: list[ReviewCandidate] = []
        review_reconciliation_batch: list[ReviewCandidateReconciliation] = []
        rows = iter_candidates(
            self.config.state_path,
            self.run_id,
            self.config.max_file_bytes,
            self.config.max_documents,
            processing_signature=self.processing_signature,
            retry_errors=retry_selected,
            prefer_current_cache=self.config.max_documents is None,
            selection=self.config.selection,
        )

        def apply_delta(delta: _ImageCounterDelta) -> None:
            nonlocal processed, cache_hits, cached_errors, classified, errors
            nonlocal recovered_decodes, retryable_errors, manual_review_errors
            nonlocal deletion_candidates, document_ocr_attempts
            nonlocal document_ocr_positive, document_ocr_failures
            nonlocal document_candidates, photo_candidates
            nonlocal industrial_context_candidates, adult_heuristic_candidates
            nonlocal adult_analyzed, adult_explicit, adult_ambiguous
            nonlocal adult_unavailable
            processed += delta.processed
            cache_hits += delta.cache_hits
            cached_errors += delta.cached_errors
            classified += delta.classified
            errors += delta.errors
            recovered_decodes += delta.recovered_decodes
            retryable_errors += delta.retryable_errors
            manual_review_errors += delta.manual_review_errors
            deletion_candidates += delta.deletion_candidates
            document_ocr_attempts += delta.document_ocr_attempts
            document_ocr_positive += delta.document_ocr_positive
            document_ocr_failures += delta.document_ocr_failures
            document_candidates += delta.document_candidates
            photo_candidates += delta.photo_candidates
            industrial_context_candidates += delta.industrial_context_candidates
            adult_heuristic_candidates += delta.adult_heuristic_candidates
            adult_analyzed += delta.adult_analyzed
            adult_explicit += delta.adult_explicit
            adult_ambiguous += delta.adult_ambiguous
            adult_unavailable += delta.adult_unavailable

        def flush_results() -> None:
            nonlocal review_candidates_stored
            review_candidates_stored += self._flush_result_batches(
                success_batch,
                error_batch,
                review_batch,
                review_reconciliation_batch,
            )

        def report(*, finished: bool = False) -> None:
            emit_progress(
                self.progress,
                ProgressEvent(
                    "image",
                    "classify",
                    (
                        "Clasificación de imágenes actualizada"
                        if finished
                        else "Clasificando imágenes"
                    ),
                    processed,
                    selection_total,
                    "imágenes",
                    finished,
                    (
                        ProgressMetric("cache_hits", cache_hits),
                        ProgressMetric("feature_cache_hits", work.feature_cache_hits),
                        ProgressMetric("new_work", work.new_images),
                        ProgressMetric("retries", work.retried_images),
                        ProgressMetric("reclassified", work.reclassified_images),
                        ProgressMetric("errors", errors),
                        ProgressMetric("in_flight", len(work.pending)),
                        ProgressMetric("remaining", max(0, selection_total - processed)),
                        ProgressMetric("cached_errors", cached_errors),
                        ProgressMetric("completed_work", classified),
                        ProgressMetric("adult_unavailable", adult_unavailable),
                        ProgressMetric("ocr_attempts", document_ocr_attempts),
                        ProgressMetric("memory_waits", self.memory_gate.wait_count),
                        ProgressMetric("review_candidates", len(review_batch)),
                    ),
                ),
            )

        report()
        try:
            self._execute_rows(
                rows,
                retry_selected,
                selected_work,
                work,
                success_batch,
                error_batch,
                review_batch,
                review_reconciliation_batch,
                apply_delta,
                flush_results,
                report,
            )
        except (CancellationRequested, KeyboardInterrupt):
            for future in work.pending:
                future.cancel()
            flush_results()
            raise
        finally:
            try:
                # ``iter_candidates`` owns a thread-affine SQLite connection.
                # Close it in the route thread even when a worker fails so GC
                # cannot finalize it later from an unrelated worker thread.
                close_rows = getattr(rows, "close", None)
                if close_rows is not None:
                    close_rows()
            finally:
                self._close_image_workers()

        flush_results()
        pruned = (
            0
            if self.config.selection.active
            else prune_missing(self.config.state_path, self.run_id)
        )
        report(finished=True)
        return ImageRouteSummary(
            processing_signature=self.processing_signature,
            processing_provenance=self.processing_provenance.manifest,
            candidate_pool=candidate_pool,
            candidates=selection_total,
            skipped_by_size=max(0, candidate_pool - eligible),
            skipped_by_count=max(0, eligible - selection_total),
            processed=processed,
            cache_hits=cache_hits,
            feature_cache_hits=work.feature_cache_hits,
            cached_errors=cached_errors,
            new_images=work.new_images,
            retried_images=work.retried_images,
            reclassified_images=work.reclassified_images,
            classified=classified,
            document_candidates=document_candidates,
            photo_candidates=photo_candidates,
            industrial_context_candidates=industrial_context_candidates,
            adult_heuristic_candidates=adult_heuristic_candidates,
            adult_analyzed=adult_analyzed,
            adult_explicit=adult_explicit,
            adult_ambiguous=adult_ambiguous,
            adult_unavailable=adult_unavailable,
            errors=errors,
            document_ocr_attempts=document_ocr_attempts,
            document_ocr_positive=document_ocr_positive,
            document_ocr_failures=document_ocr_failures,
            document_verifier_available=self.document_verifier.enabled,
            document_verifier_provenance=self.document_verifier.provenance,
            recovered_decodes=recovered_decodes,
            retryable_errors=retryable_errors,
            manual_review_errors=manual_review_errors,
            deletion_candidates=deletion_candidates,
            review_candidates_stored=review_candidates_stored,
            cache_rows_pruned=pruned,
            peak_reserved_bytes=self.memory_gate.peak_reserved_bytes,
            memory_waits=self.memory_gate.wait_count,
            full_fingerprint_cache_hits=self._full_fingerprint_cache_hits,
            full_fingerprints_computed=self._full_fingerprints_computed,
        )

    def _stage_inventory(self) -> None:
        pending: list[tuple[str, FileSnapshot]] = []
        selection = self.config.selection
        if selection.paths or selection.recommendations:
            iterator = self.framework_state.iter_selected_route_candidates_by_prefix(
                self.run_id,
                "image/",
                "image",
                selection,
            )
        else:
            iterator = self.framework_state.iter_route_candidates_by_prefix(self.run_id, "image/")
        for mime, snapshot in iterator:
            self.cancellation.checkpoint()
            pending.append((mime, snapshot))
            if len(pending) >= INVENTORY_BATCH_SIZE:
                stage_inventory_batch(self.config.state_path, self.run_id, pending)
                pending.clear()
        if pending:
            stage_inventory_batch(self.config.state_path, self.run_id, pending)

    def _analyze(
        self,
        snapshot: FileSnapshot,
        cached_features: Features | None = None,
    ) -> _AnalysisResult:
        key = file_key(snapshot)
        try:
            self.cancellation.checkpoint()
            before = snapshot_path(snapshot.path)
            if not _same_snapshot(snapshot, before):
                raise RuntimeError("image metadata changed before classification")
            path = Path(snapshot.path)
            needs_document_ocr = bool(
                self.document_verifier.enabled
                and (
                    cached_features is None
                    or requires_document_verification(
                        path,
                        self.config.root,
                        cached_features,
                    )
                )
            )
            if cached_features is not None and not needs_document_ocr:
                decision = classify(
                    path,
                    self.config.root,
                    features=cached_features,
                    document_verifier=self.document_verifier,
                    analyze_adult=False,
                )
            elif self.config.isolate_decoders:
                reservation = image_worker_memory_reservation(
                    path,
                    cached_features,
                    document_ocr=needs_document_ocr,
                )
                with self.memory_gate.admit(reservation):
                    decision = self._image_worker().classify(
                        path,
                        self.config.root,
                        memory_limit_bytes=reservation,
                        timeout_seconds=self.config.worker_timeout_seconds,
                        cancellation=self.cancellation,
                        features=cached_features,
                        document_verifier=self.document_verifier,
                    )
            else:
                decision = classify(
                    path,
                    self.config.root,
                    self.memory_gate,
                    features=cached_features,
                    document_verifier=self.document_verifier,
                    analyze_adult=False,
                )
            if decision.adult_content is None:
                adult_candidate, _reasons = is_adult_model_candidate(
                    path,
                    decision.category,
                    decision.features,
                    decision.document_candidate,
                )
                if adult_candidate:
                    adult_reservation = ADULT_MODEL_PHYSICAL_BYTES + (
                        estimated_image_memory_bytes(
                            decision.features.width,
                            decision.features.height,
                            decision.features.file_size,
                        )
                    )
                    with self.memory_gate.admit(adult_reservation):
                        adult_content = DEFAULT_ADULT_CLASSIFIER.classify(
                            path,
                            decision.category,
                            decision.features,
                            decision.document_candidate,
                        )
                else:
                    adult_content = DEFAULT_ADULT_CLASSIFIER.classify(
                        path,
                        decision.category,
                        decision.features,
                        decision.document_candidate,
                    )
                decision = replace(decision, adult_content=adult_content)
            self.cancellation.checkpoint()
            after = snapshot_path(snapshot.path)
            if not _same_snapshot(snapshot, after):
                raise RuntimeError("image metadata changed during classification")
            return _AnalysisResult(
                key=key,
                snapshot=snapshot,
                decision=decision,
                feature_cache_used=cached_features is not None,
            )
        except CancellationRequested:
            raise
        except MemoryBudgetExceeded as exc:
            return _AnalysisResult(
                key=key,
                snapshot=snapshot,
                decision=None,
                feature_cache_used=cached_features is not None,
                failure=classify_image_failure(exc),
            )
        except MemoryHeadroomTimeout:
            raise
        except MemoryError:
            raise
        except Exception as exc:
            failure = refine_image_failure(
                Path(snapshot.path),
                classify_image_failure(exc),
            )
            return _AnalysisResult(
                key=key,
                snapshot=snapshot,
                decision=None,
                feature_cache_used=cached_features is not None,
                failure=failure,
            )

    def _image_worker(self) -> ImageWorkerSupervisor:
        supervisor = getattr(self._worker_local, "supervisor", None)
        if supervisor is None:
            supervisor = ImageWorkerSupervisor()
            self._worker_local.supervisor = supervisor
            with self._supervisor_lock:
                self._supervisors.add(supervisor)
        return supervisor

    def _close_image_workers(self) -> None:
        with self._supervisor_lock:
            supervisors = tuple(self._supervisors)
            self._supervisors.clear()
        for supervisor in supervisors:
            supervisor.close()


def _same_snapshot(expected: FileSnapshot, actual: FileSnapshot) -> bool:
    return (
        expected.volume_id == actual.volume_id
        and expected.file_id == actual.file_id
        and expected.size == actual.size
        and expected.mtime_ns == actual.mtime_ns
        and expected.birthtime_ns == actual.birthtime_ns
    )


def _prepare_decision_ocr_text(
    evidence: DocumentTextEvidence | None,
) -> EncodedOcrText:
    if evidence is None or not evidence.available:
        return prepare_ocr_text_storage("", truncated=False)
    return prepare_ocr_text_storage(
        evidence.recognized_text,
        truncated=evidence.recognized_text_truncated,
    )


def _document_text_metadata(
    evidence: DocumentTextEvidence | None,
    stored: EncodedOcrText,
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    payload = asdict(evidence)
    payload.pop("recognized_text", None)
    payload["recognized_text_chars"] = stored.characters
    payload["recognized_text_xxh3_128"] = stored.xxh3_128
    return payload


def _cached_failure(row: Any) -> ImageFailure:
    raw_disposition = str(row["error_disposition"] or "manual_review")
    disposition = (
        cast(ErrorDisposition, raw_disposition)
        if raw_disposition in {"retry", "manual_review", "deletion_candidate"}
        else "manual_review"
    )
    return ImageFailure(
        error_type=str(row["error_type"] or "UnknownError"),
        message=str(row["error_message"] or "unknown image error")[:2000],
        phase=str(row["error_phase"] or "analysis"),
        retryable=bool(row["error_retryable"]),
        disposition=disposition,
        provenance=str(row["error_provenance"] or "image-error-policy-v1"),
    )


def _error_review_candidate(
    snapshot: FileSnapshot,
    failure: ImageFailure,
) -> ReviewCandidate:
    confidence = 0.95 if failure.phase == "decode" else 0.70
    return ReviewCandidate(
        route_name="image",
        snapshot=snapshot,
        reason_code=f"image_{failure.phase}_failure",
        source_status="error",
        recommendation=failure.disposition,
        retryable=failure.retryable,
        confidence=confidence,
        evidence={
            "error_type": failure.error_type,
            "error_message": failure.message[:500],
            "phase": failure.phase,
            "provenance": failure.provenance,
        },
        detector_version=failure.provenance,
    )


def _review_reason_codes(
    candidates: Iterable[ReviewCandidate],
) -> frozenset[str]:
    """Return the active findings emitted by one detector generation."""

    return frozenset(candidate.reason_code for candidate in candidates)


def _successful_review_reconciliation(
    snapshot: FileSnapshot,
    candidates: Iterable[ReviewCandidate],
    note: str,
) -> ReviewCandidateReconciliation:
    """Build one complete, reason-scoped image detector generation."""

    return ReviewCandidateReconciliation(
        snapshot=snapshot,
        resolution_note=note,
        evaluated_reason_codes=tuple(sorted(IMAGE_COMPLETE_REVIEW_REASON_CODES)),
        active_reason_codes=tuple(sorted(_review_reason_codes(candidates))),
    )


def _success_review_candidates(
    snapshot: FileSnapshot,
    decision: Decision,
) -> tuple[ReviewCandidate, ...]:
    candidates: list[ReviewCandidate] = []
    document = decision.document_candidate
    if document.is_candidate:
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code="image_raster_document_candidate",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=document.heuristic_score,
                evidence={
                    "category": decision.category,
                    "uncertainty": document.uncertainty,
                    "kinds": document.kinds,
                    "signals": document.evidence,
                    "provenance": document.provenance,
                },
                detector_version="document-candidate-v1",
            )
        )
    if decision.features.decode_quality == "recovered_truncated":
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code="image_recovered_truncated_decode",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=0.90,
                evidence={
                    "category": decision.category,
                    "decode_quality": decision.features.decode_quality,
                    "decode_provenance": decision.features.decode_provenance,
                    "classification_confidence": decision.confidence,
                },
                detector_version=decision.features.decode_provenance,
            )
        )
    adult = decision.adult_content
    if adult is not None and adult.classification in {
        "explicit",
        "ambiguous",
        "unavailable",
    }:
        explicit = adult.classification == "explicit"
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code=(
                    "image_explicit_adult_content"
                    if explicit
                    else "image_adult_content_requires_review"
                ),
                source_status="done",
                recommendation=("deletion_candidate" if explicit else "manual_review"),
                retryable=adult.classification == "unavailable",
                confidence=adult.confidence if adult.analyzed else 0.50,
                evidence={
                    "classification": adult.classification,
                    "detections": [asdict(value) for value in adult.detections],
                    "signals": adult.evidence,
                    "provenance": adult.provenance,
                },
                detector_version="|".join(adult.provenance),
            )
        )
    return tuple(candidates)


def _cached_success_review_candidates(
    row: Any,
    snapshot: FileSnapshot,
) -> tuple[ReviewCandidate, ...]:
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(row["evidence_json"] or "{}")
        if isinstance(decoded, dict):
            payload = decoded
    except (TypeError, ValueError):
        pass
    candidates: list[ReviewCandidate] = []
    if row["document_candidate"]:
        document = payload.get("document_candidate")
        document_payload = document if isinstance(document, dict) else {}
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code="image_raster_document_candidate",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=float(row["document_candidate_score"] or 0.64),
                evidence={
                    "category": str(row["category"]),
                    "uncertainty": str(row["document_candidate_uncertainty"] or "alta"),
                    "details": document_payload,
                },
                detector_version="document-candidate-v1",
            )
        )
    if row["decode_quality"] == "recovered_truncated":
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code="image_recovered_truncated_decode",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=0.90,
                evidence={
                    "category": str(row["category"]),
                    "decode_quality": "recovered_truncated",
                    "decode_provenance": str(row["decode_provenance"]),
                    "classification_confidence": float(row["confidence"]),
                },
                detector_version=str(row["decode_provenance"] or "pillow-truncated-recovery-v1"),
            )
        )
    classification = str(row["adult_classification"] or "not_analyzed")
    if classification in {"explicit", "ambiguous", "unavailable"}:
        adult_payload: dict[str, Any] = {}
        try:
            decoded_adult = json.loads(row["adult_evidence_json"] or "{}")
            if isinstance(decoded_adult, dict):
                adult_payload = decoded_adult
        except (TypeError, ValueError):
            pass
        explicit = classification == "explicit"
        candidates.append(
            ReviewCandidate(
                route_name="image",
                snapshot=snapshot,
                reason_code=(
                    "image_explicit_adult_content"
                    if explicit
                    else "image_adult_content_requires_review"
                ),
                source_status="done",
                recommendation=("deletion_candidate" if explicit else "manual_review"),
                retryable=classification == "unavailable",
                confidence=(
                    float(row["adult_confidence"] or 0.0) if bool(row["adult_analyzed"]) else 0.50
                ),
                evidence={
                    "classification": classification,
                    "details": adult_payload,
                },
                detector_version=str(row["adult_provenance"] or ADULT_POLICY_VERSION),
            )
        )
    return tuple(candidates)


def _semantic_json_has_evidence(payload: str | None) -> bool:
    if not payload:
        return False
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return False
    return any(
        value.get(name)
        for name in (
            "entities",
            "activities",
            "operational_contexts",
            "safety_conditions",
        )
    )


def _cached_features_from_row(row: Any) -> Features | None:
    """Rehydrate only feature schemas known to be decision-compatible."""

    if row["status"] != "done" or not cached_features_are_compatible(row["processing_signature"]):
        return None
    payload = row["features_json"]
    if not payload:
        return None
    try:
        values = json.loads(payload)
        if not isinstance(values, dict):
            return None
        features = Features(**values)
    except (TypeError, ValueError):
        return None
    if features.width <= 0 or features.height <= 0 or features.file_size != int(row["size"]):
        return None
    return features


# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.image_route")
