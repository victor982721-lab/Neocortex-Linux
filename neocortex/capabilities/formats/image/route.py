"""Incremental, memory-bounded image classification route."""

from __future__ import annotations
import json
import threading
import time
import zlib
from concurrent.futures import Future
from contextlib import nullcontext
from neocortex.runtime.control.elastic_workers import current_worker_cancellation, elastic_map, ImmediateResult
from neocortex.runtime.control.global_resources import current_resource_grant
from ..media_resources import ResidentMediaGate, current_media_resource, media_gate_scope
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, cast

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

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from .analysis import (
    ANALYSIS_VERSION as ANALYSIS_VERSION,
    DEFAULT_VISUAL_CLASSIFIER as DEFAULT_VISUAL_CLASSIFIER,
    Decision,
    Features,
    ImageMemoryGate,
    ImageResourceLimits,
    cached_features_are_compatible,
    classify,
    requires_document_verification,
)
from .document import (
    DocumentTextEvidence,
    DocumentVerifierConfig as DocumentVerifierConfig,
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
    MIN_IMAGE_WORKER_BYTES,
    image_worker_memory_reservation,
)
from .state import (
    EncodedOcrText,
    _decode_ocr_text,
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
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded, MemoryHeadroomTimeout
from .contracts import (
    IMAGE_ROUTE_VERSION as IMAGE_ROUTE_VERSION,
    ImageRouteConfig as ImageRouteConfig,
    ImageRouteSummary as ImageRouteSummary,
    _document_verifier_config as _document_verifier_config,
    _image_processing_provenance as _image_processing_provenance,
)
from neocortex.workflow.review.review import ReviewCandidate
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.framework_route_state import ReviewCandidateReconciliation


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
IMAGE_SUCCESS_REVIEW_REASON_CODES = frozenset(
    {
        "image_raster_document_candidate",
        "image_recovered_truncated_decode",
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
# and traversed every failure phase, which lets reconciliation retire stale
# results without conflating unrelated review reasons.
IMAGE_COMPLETE_REVIEW_REASON_CODES = (
    IMAGE_SUCCESS_REVIEW_REASON_CODES | IMAGE_FAILURE_REVIEW_REASON_CODES
)


def _ocr_language_packs_missing(verifier: DocumentVerifierRuntime) -> bool:
    reason = verifier.unavailable_reason or ""
    return reason.startswith("missing OCR languages:")


@dataclass(frozen=True, slots=True)
class _AnalysisResult:
    key: str
    snapshot: FileSnapshot
    decision: Decision | None
    feature_cache_used: bool = False
    failure: ImageFailure | None = None


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    row: Any
    snapshot: FileSnapshot
    features: Features | None
    memory_reservation: int | None

    @property
    def transient_bytes(self) -> int:
        return max(0, (self.memory_reservation or MIN_IMAGE_WORKER_BYTES) - MIN_IMAGE_WORKER_BYTES)


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


@dataclass(slots=True)
class _ImageWorkState:
    work_submitted: int = 0
    pending_admissions: int = 0
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
        if config.workers is not None and config.workers < 1:
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
        self._recoverable_retry_keys: set[str] = set()
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
        self._provided_memory_gate = memory_gate
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
        self._memory_limits = getattr(self.memory_gate, "limits", None)
        initialize_image_state(config.state_path)

    def _claim_recoverable_retry(self, snapshot: FileSnapshot) -> bool:
        """Claim the single automatic retry slot for one image in this run."""

        claimed = getattr(self, "_recoverable_retry_keys", None)
        if claimed is None:
            claimed = set()
            self._recoverable_retry_keys = claimed
        key = file_key(snapshot)
        if key in claimed:
            return False
        claimed.add(key)
        return True

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
        snapshot = snapshot_from_row(row)
        if row["status"] == "done" and signature_matches:
            if not _cached_ocr_result_is_valid(row):
                return None
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
                recovered_decodes=int(row["decode_quality"] == "recovered_truncated"),
            )
        if (
            row["status"] == "error"
            and signature_matches
            and not retry_selected
            and self.config.retry_recoverable_errors
            and _cached_error_is_recoverable(row)
            and self._claim_recoverable_retry(snapshot)
        ):
            # Automatic recovery is deliberately opt-in and typed.  The normal
            # candidate path will execute this row exactly once for this run.
            return None
        if row["status"] != "error" or not signature_matches or retry_selected:
            return None
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
        stored_ocr_text = _prepare_decision_ocr_text(decision.document_text)
        success_batch.append(
            self._success_storage_row(result, decision, stored_ocr_text, now)
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
            document_ocr_attempts=attempted,
            document_ocr_positive=positive,
            document_ocr_failures=ocr_failure,
        )

    def _success_storage_row(
        self,
        result: _AnalysisResult,
        decision: Decision,
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
        pool = ResidentMediaGate(
            self.memory_gate, ImageWorkerSupervisor, resident_bytes=MIN_IMAGE_WORKER_BYTES,
            cancellation=self.cancellation,
        )
        pending_keys: dict[str, Future[_AnalysisResult]] = {}

        def observe(row):
            snapshot = snapshot_from_row(row)
            features = _cached_features_from_row(row)
            try:
                total = image_worker_memory_reservation(
                    Path(snapshot.path), features,
                    document_ocr=self.document_verifier.enabled,
                )
            except (OSError, ValueError):
                # Preserve the analyzer's typed failure/refinement path if
                # metadata could not be observed. Never cache that fallback.
                total = None
            return _ImageCandidate(row, snapshot, features, total)

        def candidates():
            for row in rows:
                cached = self._cached_row_delta(row, retry_selected, review_batch, reconciliations)
                if cached is not None:
                    apply_delta(cached)
                    flush_results()
                    report()
                    continue
                candidate = observe(row)
                budget = getattr(getattr(self.memory_gate, "limits", None), "memory_budget_bytes", None)
                coordinator = getattr(self.memory_gate, "coordinator", None)
                if coordinator is not None:
                    route_budget = getattr(coordinator, "route_memory_budget_bytes", None)
                    budget = (
                        coordinator.memory_budget_bytes
                        if route_budget is None
                        else route_budget(getattr(self.memory_gate, "route_name", "Image"))
                    )
                if budget is not None and candidate.transient_bytes + MIN_IMAGE_WORKER_BYTES > budget:
                    snapshot = candidate.snapshot
                    failure = MemoryBudgetExceeded("image estimate exceeds the configured memory budget")
                    result = _AnalysisResult(
                        key=file_key(snapshot), snapshot=snapshot, decision=None,
                        feature_cache_used=False, failure=classify_image_failure(failure),
                    )
                    apply_delta(self._analysis_result_delta(
                        result, success_batch, error_batch, review_batch, reconciliations,
                    ))
                    flush_results()
                    report()
                    continue
                work.pending_admissions += 1
                report()
                yield candidate

        def prepare(candidate):
            work.pending_admissions -= 1
            if work.work_submitted >= selected_work:
                return ImmediateResult(None)
            work.work_submitted += 1
            row = candidate.row
            snapshot = candidate.snapshot
            self._ensure_full_fingerprint(snapshot)
            features = candidate.features
            work.feature_cache_hits += int(features is not None)
            work.retried_images += int(row["status"] == "error")
            work.new_images += int(row["status"] != "error" and row["processing_signature"] is None)
            work.reclassified_images += int(row["status"] != "error" and row["processing_signature"] is not None)
            token: Future[_AnalysisResult] = Future()
            work.pending.add(token)
            pending_keys[file_key(snapshot)] = token
            report()
            return snapshot, features, candidate.memory_reservation

        def analyze(payload):
            return self._analyze(*payload)

        try:
            with elastic_map(
                analyze, candidates(), gate=pool,
                max_workers=self.config.workers, estimated_bytes=lambda item: item.transient_bytes,
                native_threads=1, io_slots=1,
                io_device=lambda item: f"dev:{item.snapshot.volume_id:x}",
                phase="image-classify", cancellation=self.cancellation,
                prepare=prepare,
            ) as results:
                for result in results:
                    if result is None:
                        continue
                    token = pending_keys.pop(result.key, None)
                    if token is not None:
                        work.pending.discard(token)
                    apply_delta(self._analysis_result_delta(
                        result, success_batch, error_batch, review_batch, reconciliations,
                    ))
                    # The map keeps transient result bytes until the next item.
                    flush_results()
                    report()
        except BaseException:
            self.cancellation.cancel()
            raise
        finally:
            pool.close()

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
        with media_gate_scope(
            "Image", self._provided_memory_gate, self._memory_limits, self.cancellation,
        ) as gate:
            self.memory_gate = gate
            return self._run_with_resources()

    def _run_with_resources(self) -> ImageRouteSummary:
        self.cancellation.checkpoint()
        retry_keys = getattr(self, "_recoverable_retry_keys", None)
        if retry_keys is None:
            self._recoverable_retry_keys = set()
        else:
            retry_keys.clear()
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
            nonlocal industrial_context_candidates
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
                        ProgressMetric("pending_admissions", work.pending_admissions),
                        ProgressMetric("remaining", max(0, selection_total - processed)),
                        ProgressMetric("cached_errors", cached_errors),
                        ProgressMetric("completed_work", classified),
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
        except BaseException as failure:
            # Keep consumed results resumable without replacing the first
            # failure if persistence or worker cleanup fails as well.
            for label, cleanup in (
                ("result flush", flush_results),
                ("candidate cursor close", lambda: self._close_candidate_rows(rows)),
                ("worker close", self._close_image_workers),
            ):
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    failure.add_note(f"image {label} failed: {cleanup_error!r}")
            raise
        else:
            try:
                self._close_candidate_rows(rows)
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

    @staticmethod
    def _close_candidate_rows(rows: Iterator[Any]) -> None:
        # ``iter_candidates`` owns a thread-affine SQLite connection. Close it
        # in the route thread so GC cannot finalize it in an unrelated worker.
        close_rows = getattr(rows, "close", None)
        if close_rows is not None:
            close_rows()

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
        memory_reservation: int | None = None,
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
                )
            elif self.config.isolate_decoders:
                # The bounded producer observation belongs to this exact
                # snapshot, revalidated above and again after classification.
                reservation = memory_reservation
                if reservation is None:
                    reservation = image_worker_memory_reservation(
                        path,
                        cached_features,
                        document_ocr=needs_document_ocr,
                    )
                with (nullcontext() if current_media_resource() is not None or current_resource_grant() is not None
                      else self.memory_gate.admit(reservation)):
                    decision = self._image_worker().classify(
                        path,
                        self.config.root,
                        memory_limit_bytes=reservation,
                        timeout_seconds=self.config.worker_timeout_seconds,
                        cancellation=current_worker_cancellation() or self.cancellation,
                        features=cached_features,
                        document_verifier=self.document_verifier,
                    )
            else:
                decision = classify(
                    path,
                    self.config.root,
                    None if current_media_resource() is not None else self.memory_gate,
                    features=cached_features,
                    document_verifier=self.document_verifier,
                )
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
        pooled = current_media_resource()
        if pooled is not None:
            return pooled
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
        failure: BaseException | None = None
        for supervisor in supervisors:
            try:
                supervisor.close()
            except BaseException as cleanup_error:
                if failure is None:
                    failure = cleanup_error
                else:
                    failure.add_note(f"another image worker close failed: {cleanup_error!r}")
        if failure is not None:
            raise failure


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


def _cached_error_is_recoverable(row: Any) -> bool:
    """Accept only explicit durable retry evidence, never message text."""

    disposition = str(row["error_disposition"] or "")
    if disposition in {
        "",
        "unknown",
        "none",
        "manual_review",
        "deletion_candidate",
        "keep_protected",
        "protected",
    }:
        return False
    return disposition == "retry" or row["error_retryable"] in (1, True)


def _cached_ocr_result_is_valid(row: Any) -> bool:
    """Validate searchable OCR evidence without opening or decoding the image."""

    payload = row["ocr_text_zlib"]
    if payload is None:
        if (
            row["ocr_text_chars"] not in (None, 0)
            or row["ocr_text_xxh3_128"] is not None
            or bool(row["ocr_text_truncated"])
        ):
            return False
        try:
            evidence = json.loads(str(row["evidence_json"] or "{}"))
        except (TypeError, ValueError):
            return False
        document_text = evidence.get("document_text")
        if isinstance(document_text, dict) and bool(document_text.get("available")):
            try:
                return int(document_text.get("recognized_text_chars", 0)) == 0
            except (TypeError, ValueError):
                return False
        return True
    try:
        decoded = _decode_ocr_text(row)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, zlib.error):
        return False
    try:
        evidence = json.loads(str(row["evidence_json"] or "{}"))
    except (TypeError, ValueError):
        return False
    document_text = evidence.get("document_text")
    if not isinstance(document_text, dict):
        return True
    try:
        expected_chars = int(
            document_text.get("recognized_text_chars", decoded.characters)
        )
    except (TypeError, ValueError):
        return False
    expected_digest = document_text.get("recognized_text_xxh3_128")
    expected_truncated = document_text.get("recognized_text_truncated")
    return expected_chars == decoded.characters and (
        expected_digest is None or str(expected_digest) == decoded.xxh3_128
    ) and (
        expected_truncated is None
        or bool(expected_truncated) == decoded.truncated
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
        resolution_note=f"{note}; detector_policy=raster-observation-v2",
        evaluated_reason_codes=tuple(sorted(IMAGE_COMPLETE_REVIEW_REASON_CODES)),
        active_reason_codes=tuple(sorted(_review_reason_codes(candidates))),
    )


def _success_review_candidates(
    snapshot: FileSnapshot,
    decision: Decision,
) -> tuple[ReviewCandidate, ...]:
    candidates: list[ReviewCandidate] = []
    # Raster detection is an extraction/association observation, not a defect or
    # an individual human decision. Its evidence remains in the image owner.
    # Retain the reason in IMAGE_COMPLETE_REVIEW_REASON_CODES so a successful
    # replay reconciles old detector generations without deleting human history.
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
    return tuple(candidates)


def _cached_success_review_candidates(
    row: Any,
    snapshot: FileSnapshot,
) -> tuple[ReviewCandidate, ...]:
    candidates: list[ReviewCandidate] = []
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
    if not _cached_ocr_result_is_valid(row):
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
