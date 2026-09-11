"""Incremental PDF route for the integrated framework.
# region [00] Contexto del módulo
# Módulo: neocortex/pdf_route.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The route consumes the surviving inventory instead of walking the filesystem
again.  Text is persisted page by page so memory use is bounded by one rendered
page per active worker and interrupted documents can resume.
"""

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import queue
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol, cast, runtime_checkable

from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from .pdf_cache import binary_fingerprint
from .pdf_derived import (
    PdfDerivedCoverage,
    PdfDerivedIndexer,
    PdfDerivedSummary,
    inspect_pdf_derived_coverage,
)
from .pdf_isolation import (
    PdfChildProcessError,
    PdfChildReportedError,
    IsolatedExtractionConfig,
    PdfDocumentTimeout,
    _ocr_page_result,
    stream_isolated_extraction,
)
from neocortex.safety.ocr_profiles import native_text_quality, resolve_ocr_profile
from .pdf_runtime import (
    PdfResourceError,
    PdfResourceGate,
    PdfResourceLimits,
)
from .pdf_route_models import (
    PDF_PAGE_SEQUENCE_ERROR_LIMIT,
    CacheDecision,
    DocumentResult,
    PdfExtractionCoverage,
    PdfRouteConfig,
    PdfRouteSummary,
    FAILURE_DETECTOR_VERSION,
    effective_document_timeout_seconds,
    effective_pdf_job_memory_limit_bytes,
    effective_pdf_worker_memory_bytes,
    resolve_pdf_tesseract_runtime,
)
from .pdf_route_cache import (
    PDF_CACHE_TOUCH_BATCH,
    PdfRouteCacheMixin,
    file_key as _file_key,
    inspect_pdf_extraction_coverage,
)
from .pdf_route_storage import (
    PROMOTION_BATCH_BYTES,
    PdfRouteStorageMixin,
)
from .pdf_state import initialize_pdf_state, pdf_database
from .pdf_writer import serialized_pdf_write
from neocortex.runtime.control.retry_policy import (
    PDF_RETRYABLE_PAGE_ERROR_SQL,
    PdfFailureDiagnostic,
    classify_pdf_failure,
)
from neocortex.workflow.review.review import ReviewCandidate
from neocortex.safety.route_filters import CandidateSelection
from neocortex.persistence.framework_route_state import (
    REVIEW_RECONCILIATION_BATCH_SIZE,
    ReviewCandidateReconciliation,
)
# endregion [01]

# region [02] Implementación


class PdfRouteState(Protocol):
    """Structural state contract used by the PDF route and test adapters."""

    def iter_route_candidates(self, run_id: int, mime: str) -> Any: ...

    def iter_selected_route_candidates(
        self,
        run_id: int,
        mime: str,
        route_name: str,
        selection: CandidateSelection,
    ) -> Any: ...

    def begin_file_actions(self, run_id: int, actions: Any) -> list[int]: ...

    def finish_file_actions(
        self,
        action_ids: Any,
        status: str,
        detail: str | None = None,
    ) -> None: ...


TEXT_BATCH_PAGES = 4
TEXT_DUPLICATE_ACTION_BATCH_SIZE = 256
PDF_INVENTORY_BATCH = 1000
TRANSIENT_RETRIES_PER_RUN = 1
PAGE_PROGRESS_POLL_SECONDS = 1.0
PDF_REVIEW_REASON_CODES = frozenset(
    {
        "pdf_child_exit",
        "pdf_document_timeout",
        "pdf_interrupted_processing",
        "pdf_legacy_child_resource_failure",
        "pdf_legacy_ocr_control_bug",
        "pdf_legacy_structural_retry",
        "pdf_ocr_resource_failure",
        "pdf_password_required",
        "pdf_resource_failure",
        "pdf_source_changed",
        "pdf_state_contention",
        "pdf_structural_damage",
        "pdf_structural_damage_with_recoverable_pages",
        "pdf_structural_recovered",
        "pdf_unclassified_failure",
        "pdf_unrecoverable_structural_damage",
    }
)

_DocumentResult = DocumentResult


_database = pdf_database
_initialize = initialize_pdf_state


@runtime_checkable
class _PdfMinerTextContainer(Protocol):
    """Minimal runtime contract for the optional pdfminer text container."""

    def get_text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class _PdfRunPlan:
    """Immutable phase decisions established before extraction starts."""

    skip_extraction: bool
    skip_text_dedup: bool
    skip_derived: bool
    candidate_pool: int
    eligible_candidates: int
    expected_total: int
    candidates: Iterator[FileSnapshot]
    derived_repair_required: bool = False
    derived_coverage: PdfDerivedCoverage | None = None
    extraction_repair_required: bool = False
    extraction_coverage: PdfExtractionCoverage | None = None


@dataclass(slots=True)
class _ExtractionStats:
    """Mutable, bounded counters for one extraction phase."""

    total: int = 0
    processed: int = 0
    cache_hits: int = 0
    extracted: int = 0
    errors: int = 0
    protected: int = 0
    cached_errors: int = 0
    new_documents: int = 0
    cache_refreshes: int = 0
    retried_documents: int = 0
    retry_pages_planned: int = 0
    native_pages: int = 0
    ocr_pages: int = 0
    partial_documents: int = 0
    page_errors: int = 0
    document_timeouts: int = 0
    unrecoverable_recycled: int = 0
    warning_documents: int = 0
    mupdf_warnings: int = 0

    def register_cache_miss(self, decision: CacheDecision) -> None:
        if decision.prior_status is None:
            self.new_documents += 1
        elif decision.prior_status in {
            "error",
            "partial",
            "protected",
            "processing",
        }:
            self.retried_documents += 1
            self.retry_pages_planned += decision.retry_pages
        else:
            self.cache_refreshes += 1

    def register_result(self, result: _DocumentResult) -> None:
        if result.status == "done":
            self.extracted += 1
        elif result.status == "partial":
            self.extracted += 1
            self.partial_documents += 1
        elif result.status == "protected":
            self.protected += 1
        else:
            self.errors += 1
        self.native_pages += result.native_pages
        self.ocr_pages += result.ocr_pages
        self.page_errors += result.page_errors
        self.document_timeouts += int(result.timed_out)
        self.unrecoverable_recycled += int(result.recycled)
        self.warning_documents += int(result.warning_count > 0)
        self.mupdf_warnings += result.warning_count


@dataclass(slots=True)
class _ExtractionRuntime:
    """Executor state kept separate from durable extraction counters."""

    iterator: Iterator[FileSnapshot]
    expected_total: int
    stats: _ExtractionStats = field(default_factory=_ExtractionStats)
    pending: set[Future[_DocumentResult]] = field(default_factory=set)
    pending_snapshots: dict[Future[_DocumentResult], FileSnapshot] = field(default_factory=dict)
    cache_touches: list[FileSnapshot] = field(default_factory=list)
    exhausted: bool = False
    active_page_progress: str | int = 0
    last_page_progress_poll: float = 0.0


@dataclass(slots=True)
class _IsolatedExtractionState:
    """Incremental child-protocol state for one isolated document."""

    initial_staged_pages: int
    native_pages: int = 0
    ocr_pages: int = 0
    page_errors: int = 0
    page_count: int = 0
    start: int = 0
    end: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    prepared: bool = False
    warning_count: int = 0
    warning_samples: tuple[str, ...] = ()
    recovery_evidence: dict[str, object] | None = None
    page_diagnostic: PdfFailureDiagnostic | None = None
    page_error_limit: dict[str, object] | None = None
    batch: list[tuple[Any, ...]] = field(default_factory=list)
    batch_bytes: int = 0

    def reset_for_structural_recovery(self) -> None:
        self.batch.clear()
        self.batch_bytes = 0
        self.native_pages = 0
        self.ocr_pages = 0
        self.page_errors = 0
        self.page_diagnostic = None
        self.page_error_limit = None
        self.prepared = False


class _OcrTextWithProvenance(str):
    """Keep the legacy string contract while carrying local page evidence."""

    provenance: dict[str, object]

    def __new__(
        cls,
        value: str,
        provenance: dict[str, object],
    ) -> _OcrTextWithProvenance:
        instance = super().__new__(cls, value)
        instance.provenance = provenance
        return instance


@dataclass(slots=True)
class _LocalExtractionState:
    """Page and transaction counters for in-process extraction."""

    file_key: str
    processing_signature: str
    ocr_attempted: int
    native_pages: int = 0
    ocr_pages: int = 0
    page_errors: int = 0
    pages_since_commit: int = 0


@dataclass(slots=True)
class _PdfOwnerRequest:
    """One operation queued for the thread that owns ``pdf.sqlite3``."""

    operation: Callable[[sqlite3.Connection], object]
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    value: object | None = None


_PDF_OWNER_STOP = object()


class _PdfOwnerCoordinator:
    """Serialize PDF-owner operations on one dedicated SQLite owner thread.

    Extraction workers process source documents and enqueue bounded persistence
    operations here; they never open a PDF SQLite connection themselves.  The
    owner commits or rolls back each request so a request cannot leave a write
    transaction open while another worker waits for the coordinator.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._requests: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._connection: sqlite3.Connection | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("PDF owner coordinator cannot be started twice")
        self._thread = threading.Thread(
            target=self._run,
            name="neocortex-pdf-owner",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        try:
            with _database(self.path) as connection:
                self._connection = connection
                self._thread_ident = threading.get_ident()
                self._ready.set()
                while True:
                    request = self._requests.get()
                    if request is _PDF_OWNER_STOP:
                        return
                    assert isinstance(request, _PdfOwnerRequest)
                    try:
                        with serialized_pdf_write():
                            request.value = request.operation(connection)
                            connection.commit()
                    except BaseException as exc:
                        try:
                            connection.rollback()
                        except BaseException as rollback_error:
                            exc.add_note(
                                "PDF owner rollback failed: "
                                f"{type(rollback_error).__name__}: {rollback_error}"
                            )
                        request.value = exc
                        request.ok = False
                    else:
                        request.ok = True
                    finally:
                        request.done.set()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            self._connection = None
            self._thread_ident = None

    def call(self, operation: Callable[[sqlite3.Connection], object]) -> object:
        if self._closed:
            raise RuntimeError("PDF owner coordinator is closed")
        thread = self._thread
        if thread is None:
            raise RuntimeError("PDF owner coordinator is not started")
        if threading.get_ident() == self._thread_ident:
            connection = self._connection
            if connection is None:
                raise RuntimeError("PDF owner coordinator connection is unavailable")
            return operation(connection)
        request = _PdfOwnerRequest(operation)
        self._requests.put(request)
        while not request.done.wait(0.1):
            if not thread.is_alive():
                error = self._error or RuntimeError("PDF owner coordinator stopped")
                raise error
        if request.ok:
            return request.value
        assert isinstance(request.value, BaseException)
        raise request.value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is None:
            return
        self._requests.put(_PDF_OWNER_STOP)
        thread.join()
        if self._error is not None:
            raise self._error


@dataclass(frozen=True, slots=True)
class _PdfWorkerContext:
    """Immutable owner-derived inputs handed to one extraction worker."""

    timeout_seconds: float | None
    resumable_pages: tuple[int, frozenset[int], int]
    initial_staged_pages: int
    extraction: IsolatedExtractionConfig


class PdfRoute(PdfRouteStorageMixin, PdfRouteCacheMixin):
    """Extract, profile, cache and text-deduplicate surviving PDFs."""

    def __init__(
        self,
        config: PdfRouteConfig,
        index: DedupIndex,
        framework_state: PdfRouteState,
        run_id: int,
        scan_id: int,
        *,
        progress: ProgressCallback | None = None,
        global_coordinator=None,
        cancellation: CancellationToken | None = None,
    ):
        if config.workers < 1 or config.ocr_workers < 1:
            raise ValueError("PDF worker counts must be positive")
        if config.dpi < 72:
            raise ValueError("PDF OCR DPI must be at least 72")
        if config.max_page_text_chars < 1 or config.max_render_pixels < 1:
            raise ValueError("PDF page text and render limits must be positive")
        if config.max_file_bytes is not None and config.max_file_bytes < 1:
            raise ValueError("PDF file-size limit must be positive")
        if config.max_documents is not None and config.max_documents < 1:
            raise ValueError("PDF document-count limit must be positive")
        if config.cache_validation not in {"metadata", "full"}:
            raise ValueError("PDF cache validation must be 'metadata' or 'full'")
        if config.page_start is not None and config.page_start < 1:
            raise ValueError("PDF page_start must be positive")
        if config.page_end is not None and config.page_end < 1:
            raise ValueError("PDF page_end must be positive")
        if (
            config.page_start is not None
            and config.page_end is not None
            and config.page_start > config.page_end
        ):
            raise ValueError("PDF page_start cannot exceed page_end")
        if config.document_timeout_seconds is not None and config.document_timeout_seconds <= 0:
            raise ValueError("PDF document timeout must be positive")
        if config.timeout_mode not in {"fixed", "adaptive"}:
            raise ValueError("PDF timeout mode must be fixed or adaptive")
        if config.max_document_timeout_seconds <= 0:
            raise ValueError("PDF maximum document timeout must be positive")
        if (
            config.document_timeout_seconds is not None
            and config.max_document_timeout_seconds < config.document_timeout_seconds
        ):
            raise ValueError("PDF maximum timeout cannot be below its base timeout")
        profile_plan = resolve_ocr_profile(config.ocr_profile, config.ocr_lang)
        ocr_runtime = resolve_pdf_tesseract_runtime(config) if config.ocr_mode != "never" else None
        if (
            config.ocr_mode != "never"
            and ocr_runtime is not None
            and not ocr_runtime.available
            and (
                config.ocr_profile != "configured"
                or ocr_runtime.component.get("status") == "missing-languages"
            )
        ):
            raise RuntimeError(
                "PDF OCR profile preflight failed: "
                f"{ocr_runtime.unavailable_reason or 'runtime unavailable'}"
            )
        self.config = config
        self._ocr_profile_plan = profile_plan
        self._ocr_runtime = ocr_runtime
        self.index = index
        self.framework_state = framework_state
        self.run_id = run_id
        self.scan_id = scan_id
        self.progress = progress
        self.cancellation = cancellation or CancellationToken()
        self._ocr_slots = threading.BoundedSemaphore(config.ocr_workers)
        self._recycle_lock = threading.Lock()
        self._review_reconciliation_lock = threading.Lock()
        self._review_reconciliations: list[ReviewCandidateReconciliation] = []
        self._estimated_worker_memory = effective_pdf_worker_memory_bytes(config)
        self._worker_memory_reservation = self._estimated_worker_memory
        self._job_memory_limit = effective_pdf_job_memory_limit_bytes(config)
        self._resource_gate = PdfResourceGate(
            PdfResourceLimits(
                min_free_bytes=config.min_free_bytes,
                memory_backpressure_bytes=config.memory_backpressure_bytes,
                memory_wait_timeout_seconds=config.memory_wait_timeout_seconds,
                large_document_bytes=config.large_document_bytes,
                large_document_workers=config.large_document_workers,
                memory_budget_bytes=config.memory_budget_bytes,
                worker_memory_bytes=self._worker_memory_reservation,
                commit_backpressure_bytes=config.commit_backpressure_bytes,
            ),
            config.state_path,
            global_coordinator=global_coordinator,
            route_name="pdf",
            cancellation=self.cancellation,
        )
        self._pdf_owner: _PdfOwnerCoordinator | None = None
        _initialize(config.state_path)

    def _owner_call(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run one PDF-owner operation through the active owner coordinator."""

        owner = getattr(self, "_pdf_owner", None)
        if owner is not None:
            return owner.call(operation)
        with serialized_pdf_write(), _database(self.config.state_path) as connection:
            return operation(connection)

    def _record_event(
        self,
        phase: str,
        message: str,
        details: dict,
        *,
        level: str = "info",
    ) -> None:
        recorder = getattr(self.framework_state, "record_event", None)
        if recorder is not None:
            recorder(self.run_id, level, phase, message, details)

    def _completed_source_phases(self) -> frozenset[str]:
        source_run_id = self.config.resume_source_run_id
        reader = getattr(self.framework_state, "completed_route_phases", None)
        if source_run_id is None or reader is None:
            return frozenset()
        return frozenset(reader(source_run_id, "pdf"))

    def _begin_phase(self, phase_name: str) -> None:
        begin = getattr(self.framework_state, "begin_route_phase", None)
        if begin is not None:
            begin(
                self.run_id,
                "pdf",
                phase_name,
                source_run_id=self.config.resume_source_run_id,
            )

    def _complete_phase(
        self,
        phase_name: str,
        summary: dict[str, object],
    ) -> None:
        complete = getattr(self.framework_state, "complete_route_phase", None)
        if complete is not None:
            complete(self.run_id, "pdf", phase_name, summary)

    def _publish_review(
        self,
        snapshot: FileSnapshot,
        diagnostic: PdfFailureDiagnostic,
        source_status: str,
        *,
        evidence: dict[str, object] | None = None,
    ) -> None:
        store = getattr(self.framework_state, "store_review_candidates", None)
        if store is None:
            return
        payload = diagnostic.evidence(str((evidence or {}).get("message", "")))
        if evidence:
            payload.update(evidence)
        store(
            self.run_id,
            [
                ReviewCandidate(
                    route_name="pdf",
                    snapshot=snapshot,
                    reason_code=diagnostic.reason_code,
                    source_status=source_status,
                    recommendation=diagnostic.recommendation,
                    retryable=diagnostic.retryable,
                    confidence=diagnostic.confidence,
                    evidence=payload,
                    detector_version=FAILURE_DETECTOR_VERSION,
                )
            ],
        )

    def _publish_cached_review(
        self,
        snapshot: FileSnapshot,
        cache_decision: CacheDecision,
    ) -> bool:
        """Refresh one persisted PDF finding without reprocessing its source."""

        status = str(cache_decision.prior_status or "")
        diagnostic: PdfFailureDiagnostic | None = None
        evidence: dict[str, object] = {
            "cached": True,
            "persisted_status": status,
        }
        if status == "done" and cache_decision.metadata_json:
            try:
                metadata = json.loads(cache_decision.metadata_json)
            except (TypeError, ValueError):
                metadata = None
            recovery = metadata.get("neocortex_recovery") if isinstance(metadata, dict) else None
            if isinstance(recovery, dict):
                primary_error = str(
                    recovery.get("primary_error")
                    or recovery.get("qpdf_error")
                    or "recovered structural damage"
                )[:2000]
                diagnostic = classify_pdf_failure(
                    "PdfStructuralRecovered",
                    primary_error,
                    phase="structural_recovery",
                    recovered=True,
                )
                evidence.update(
                    {
                        "message": "PDF recovered",
                        "recovery_engine": str(recovery.get("engine") or "unknown")[:256],
                        "primary_error": primary_error,
                    }
                )
        elif status in {"error", "partial", "protected"}:
            error_type = cache_decision.error_type
            error_message = cache_decision.error_message
            phase = "cached_result"
            if not error_type and cache_decision.page_error_type:
                error_type = cache_decision.page_error_type
                error_message = cache_decision.page_error_message
                phase = "page_extraction"
            if status == "protected":
                error_type = error_type or "EncryptedPdf"
                error_message = error_message or "password required"
                phase = "open"
            diagnostic_message = error_message or "cached PDF requires review"
            if error_type == "PdfPageSequenceAborted":
                phase = "page_extraction"
                diagnostic_message = "consecutive page extraction failures; " + diagnostic_message
            diagnostic = classify_pdf_failure(
                error_type or "CachedPartialPdf",
                diagnostic_message,
                phase=phase,
            )
            if status == "partial" and diagnostic.recommendation == "deletion_candidate":
                diagnostic = PdfFailureDiagnostic(
                    diagnostic.error_type,
                    diagnostic.phase,
                    "pdf_structural_damage_with_recoverable_pages",
                    False,
                    "manual_review",
                    diagnostic.confidence,
                    diagnostic.exit_code,
                )
            evidence["message"] = error_message or "cached PDF requires review"
        if diagnostic is None:
            return False
        self._publish_review(snapshot, diagnostic, status, evidence=evidence)
        if status == "done":
            self._reconcile_review(
                snapshot,
                "current PDF cache retains recovered-structure evidence",
                active_reason_codes=(diagnostic.reason_code,),
            )
        return True

    def _resolve_review_generation(
        self,
        snapshot: FileSnapshot,
        reason_code: str,
        note: str,
    ) -> int:
        """Resolve one exact current-generation reason after a terminal action."""

        resolver = getattr(
            self.framework_state,
            "resolve_review_candidate_generation",
            None,
        )
        if resolver is None:
            return 0
        return int(
            resolver(
                self.run_id,
                "pdf",
                snapshot,
                reason_code,
                note,
            )
        )

    def _reconcile_review(
        self,
        snapshot: FileSnapshot,
        note: str,
        *,
        active_reason_codes: tuple[str, ...] = (),
    ) -> None:
        """Queue one fully evaluated PDF extraction generation by reason."""

        active = frozenset(active_reason_codes)
        reconciliation = ReviewCandidateReconciliation(
            snapshot=snapshot,
            resolution_note=note,
            evaluated_reason_codes=tuple(sorted(PDF_REVIEW_REASON_CODES | active)),
            active_reason_codes=tuple(sorted(active)),
        )
        batch: tuple[ReviewCandidateReconciliation, ...] = ()
        with self._review_reconciliation_lock:
            self._review_reconciliations.append(reconciliation)
            if len(self._review_reconciliations) >= REVIEW_RECONCILIATION_BATCH_SIZE:
                batch = tuple(self._review_reconciliations)
                self._review_reconciliations.clear()
        if batch:
            self._persist_review_reconciliations(batch)

    def _persist_review_reconciliations(
        self,
        reconciliations: tuple[ReviewCandidateReconciliation, ...],
    ) -> None:
        batch_reconciler = getattr(
            self.framework_state,
            "reconcile_review_candidates_batch",
            None,
        )
        if batch_reconciler is not None:
            batch_reconciler(self.run_id, "pdf", reconciliations)
            return
        reconciler = getattr(self.framework_state, "reconcile_review_candidates", None)
        if reconciler is None:
            return
        for reconciliation in reconciliations:
            reconciler(
                self.run_id,
                "pdf",
                reconciliation.snapshot,
                reconciliation.resolution_note,
                evaluated_reason_codes=(reconciliation.evaluated_reason_codes),
                active_reason_codes=reconciliation.active_reason_codes,
            )

    def _flush_review_reconciliations(self) -> None:
        with self._review_reconciliation_lock:
            batch = tuple(self._review_reconciliations)
            self._review_reconciliations.clear()
        if batch:
            self._persist_review_reconciliations(batch)

    def run(self) -> PdfRouteSummary:
        retry_keys = getattr(self, "_recoverable_retry_keys", None)
        if retry_keys is None:
            self._recoverable_retry_keys = set()
        else:
            retry_keys.clear()
        plan = self._prepare_run()
        extraction = self._run_extraction_phase(plan)
        text_dedup = self._run_text_dedup_phase(plan.skip_text_dedup)
        derived, cache_documents_pruned, cache_rows_pruned = self._run_derived_phase(
            plan.skip_derived
        )
        return self._build_run_summary(
            plan,
            extraction,
            text_dedup,
            derived,
            cache_documents_pruned,
            cache_rows_pruned,
        )

    def _prepare_run(self) -> _PdfRunPlan:
        """Stage inventory and establish immutable resumable phase decisions."""

        self._flush_review_reconciliations()
        self.cancellation.checkpoint()
        completed_source_phases = self._completed_source_phases()
        skip_extraction = "extraction" in completed_source_phases
        skip_text_dedup = "text_dedup" in completed_source_phases
        skip_derived = "derived" in completed_source_phases
        extraction_repair_required = False
        extraction_coverage: PdfExtractionCoverage | None = None
        derived_repair_required = False
        derived_coverage: PdfDerivedCoverage | None = None
        self._extraction_repair_required = False
        self._stage_pdf_inventory()
        recovered_cache_rows = self._reconcile_legacy_recovered_documents()
        if recovered_cache_rows:
            self._record_event(
                "pdf-cache-migration",
                "Recuperaciones PDF completas reclasificadas como caché válida",
                {"documents": recovered_cache_rows},
            )
        reconciled_done, reconciled_partial = self._reconcile_interrupted_documents()
        if reconciled_done or reconciled_partial:
            self._record_event(
                "pdf-recovery",
                "Estado PDF interrumpido reconciliado",
                {
                    "completed_recovered": reconciled_done,
                    "partial_recovered": reconciled_partial,
                },
            )
        if skip_extraction:
            self._adopt_resumed_inventory()
            extraction_coverage = inspect_pdf_extraction_coverage(
                self.config.state_path,
                self.run_id,
                self.config.processing_signature,
            )
            if not extraction_coverage.complete:
                skip_extraction = False
                extraction_repair_required = True
                self._extraction_repair_required = True
                # Recovered source pages can introduce text duplicates and
                # invalidate every derivative that was published by the
                # source run.  Revisit those phases, but keep the bounded
                # candidate stream and cache hits for unaffected documents.
                skip_text_dedup = False
                skip_derived = False
                derived_repair_required = True
        if skip_derived:
            # A completed source phase is not sufficient evidence when a
            # derived row was removed after that run.  Inspect durable rows
            # before honoring resume; the repair path below consumes cached
            # pages/representation and never schedules extraction or OCR.
            derived_coverage = inspect_pdf_derived_coverage(
                self.config.state_path,
                self.run_id,
            )
            if not derived_coverage.complete:
                skip_derived = False
                derived_repair_required = True
        candidate_pool, eligible_candidates = self._candidate_counts()
        expected_total = (
            0
            if skip_extraction
            else min(
                eligible_candidates,
                self.config.max_documents or eligible_candidates,
            )
        )
        candidates = iter(()) if skip_extraction else self._candidate_snapshots()
        if not skip_extraction:
            self._begin_phase("extraction")
        return _PdfRunPlan(
            skip_extraction=skip_extraction,
            skip_text_dedup=skip_text_dedup,
            skip_derived=skip_derived,
            candidate_pool=candidate_pool,
            eligible_candidates=eligible_candidates,
            expected_total=expected_total,
            candidates=candidates,
            derived_repair_required=derived_repair_required,
            derived_coverage=derived_coverage,
            extraction_repair_required=extraction_repair_required,
            extraction_coverage=extraction_coverage,
        )

    def _run_extraction_phase(self, plan: _PdfRunPlan) -> _ExtractionStats:
        extraction_started = time.perf_counter_ns()
        runtime = _ExtractionRuntime(plan.candidates, plan.expected_total)
        owner = _PdfOwnerCoordinator(self.config.state_path)
        owner.start()
        self._pdf_owner = owner
        try:
            try:
                self._report_extraction(runtime)
                with _database(self.config.state_path) as cache_connection:
                    self._execute_extraction(runtime, cache_connection)
                self._flush_review_reconciliations()
                self.cancellation.checkpoint()
                self._report_extraction(
                    runtime,
                    finished=True,
                    description="Extracción PDF completada",
                )
                self._finish_extraction_phase(plan, runtime.stats, extraction_started)
                return runtime.stats
            finally:
                # Candidate generators retain a readonly, thread-affine SQLite
                # connection.  Finalize them in the route thread on every exit
                # so traceback/GC cleanup cannot migrate the close to another
                # thread.
                close_candidates = getattr(runtime.iterator, "close", None)
                if close_candidates is not None:
                    close_candidates()
        finally:
            self._pdf_owner = None
            owner.close()

    def _execute_extraction(
        self,
        runtime: _ExtractionRuntime,
        cache_connection: sqlite3.Connection,
    ) -> None:
        executor = ThreadPoolExecutor(
            max_workers=self.config.workers,
            thread_name_prefix="neocortex-pdf-worker",
        )
        interrupted = False
        try:
            max_pending = max(self.config.workers, self.config.workers * 2)
            while runtime.pending or not runtime.exhausted:
                self.cancellation.checkpoint()
                self._fill_extraction_queue(
                    runtime,
                    cache_connection,
                    executor,
                    max_pending,
                )
                if runtime.pending:
                    self._poll_extraction_queue(runtime, cache_connection)
        except CancellationRequested:
            interrupted = True
            for future in runtime.pending:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=interrupted)
            self._flush_cache_touches(
                runtime,
                cache_connection,
                interrupted=interrupted,
            )

    def _fill_extraction_queue(
        self,
        runtime: _ExtractionRuntime,
        cache_connection: sqlite3.Connection,
        executor: ThreadPoolExecutor,
        max_pending: int,
    ) -> None:
        while not runtime.exhausted and len(runtime.pending) < max_pending:
            self.cancellation.checkpoint()
            try:
                snapshot = next(runtime.iterator)
            except StopIteration:
                runtime.exhausted = True
                break
            runtime.stats.total += 1
            cache_decision = self._is_cache_hit(
                snapshot,
                connection=cache_connection,
                touch=False,
            )
            if cache_decision:
                self._consume_cache_hit(
                    runtime,
                    cache_connection,
                    snapshot,
                    cache_decision,
                )
            else:
                self._submit_extraction(
                    runtime,
                    executor,
                    snapshot,
                    cache_decision,
                    cache_connection,
                )
            self._report_extraction(runtime)

    def _consume_cache_hit(
        self,
        runtime: _ExtractionRuntime,
        cache_connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        decision: CacheDecision,
    ) -> None:
        stats = runtime.stats
        stats.cache_hits += 1
        stats.cached_errors += int(decision.prior_status == "error")
        active_cached_review = self._publish_cached_review(snapshot, decision)
        if decision.prior_status == "done" and not active_cached_review:
            self._reconcile_review(
                snapshot,
                "current PDF cache completed without extraction findings",
            )
        stats.processed += 1
        runtime.cache_touches.append(snapshot)
        if len(runtime.cache_touches) >= PDF_CACHE_TOUCH_BATCH:
            self._touch_cache_hits(cache_connection, runtime.cache_touches)
            runtime.cache_touches.clear()

    def _prepare_worker_context(
        self,
        snapshot: FileSnapshot,
        connection: sqlite3.Connection,
    ) -> _PdfWorkerContext:
        """Read all owner-derived worker inputs before submitting the task."""

        resumable_pages = self._resumable_pages(snapshot, connection=connection)
        structural_recovery_reason = self._structural_recovery_reason(
            snapshot,
            connection=connection,
        )
        return self._worker_context_from_connection(
            snapshot,
            connection,
            resumable_pages=resumable_pages,
            structural_recovery_reason=structural_recovery_reason,
        )

    def _worker_context_from_connection(
        self,
        snapshot: FileSnapshot,
        connection: sqlite3.Connection,
        *,
        resumable_pages: tuple[int, frozenset[int], int] | None = None,
        structural_recovery_reason: str | None = None,
    ) -> _PdfWorkerContext:
        if resumable_pages is None:
            resumable_pages = self._resumable_pages(snapshot, connection=connection)
        if structural_recovery_reason is None:
            structural_recovery_reason = self._structural_recovery_reason(
                snapshot,
                connection=connection,
            )
        return _PdfWorkerContext(
            timeout_seconds=self._effective_document_timeout(
                snapshot,
                connection=connection,
            ),
            resumable_pages=resumable_pages,
            initial_staged_pages=self._successful_staged_page_count(
                snapshot,
                connection=connection,
            ),
            extraction=self._isolated_extraction_config(
                snapshot,
                1.0,
                resumable_pages=resumable_pages,
                structural_recovery_reason=structural_recovery_reason,
            ),
        )

    def _refresh_worker_context(
        self,
        snapshot: FileSnapshot,
    ) -> _PdfWorkerContext:
        """Refresh retry inputs through the explicit owner boundary."""

        return self._owner_call(
            lambda connection: self._worker_context_from_connection(snapshot, connection)
        )

    def _submit_extraction(
        self,
        runtime: _ExtractionRuntime,
        executor: ThreadPoolExecutor,
        snapshot: FileSnapshot,
        decision: CacheDecision,
        cache_connection: sqlite3.Connection,
    ) -> None:
        runtime.stats.register_cache_miss(decision)
        worker_context = self._prepare_worker_context(snapshot, cache_connection)
        binary_digest = binary_fingerprint(
            self.index,
            snapshot,
            required=self.config.cache_validation == "full",
        )
        future = executor.submit(self._process_document, snapshot, binary_digest, worker_context)
        runtime.pending.add(future)
        runtime.pending_snapshots[future] = snapshot

    def _poll_extraction_queue(
        self,
        runtime: _ExtractionRuntime,
        cache_connection: sqlite3.Connection,
    ) -> None:
        done, runtime.pending = wait(
            runtime.pending,
            timeout=0.1,
            return_when=FIRST_COMPLETED,
        )
        now = time.monotonic()
        if done or now - runtime.last_page_progress_poll >= PAGE_PROGRESS_POLL_SECONDS:
            runtime.active_page_progress = self._active_page_progress(
                cache_connection,
                tuple(runtime.pending_snapshots[future] for future in runtime.pending),
            )
            runtime.last_page_progress_poll = now
            if not done:
                self._report_extraction(runtime)
        for future in done:
            self._consume_extraction_future(runtime, future)
            self._report_extraction(runtime)

    def _consume_extraction_future(
        self,
        runtime: _ExtractionRuntime,
        future: Future[_DocumentResult],
    ) -> None:
        snapshot = runtime.pending_snapshots.pop(future)
        runtime.stats.processed += 1
        try:
            result = future.result()
        except (CancellationRequested, PdfResourceError):
            raise
        except Exception as exc:
            self._record_event(
                "pdf-extraction-worker",
                "Fallo no controlado en trabajador PDF",
                {
                    "path": snapshot.path,
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:2000],
                },
                level="error",
            )
            raise
        runtime.stats.register_result(result)

    def _flush_cache_touches(
        self,
        runtime: _ExtractionRuntime,
        cache_connection: sqlite3.Connection,
        *,
        interrupted: bool,
    ) -> None:
        if not runtime.cache_touches:
            return
        try:
            self._touch_cache_hits(
                cache_connection,
                runtime.cache_touches,
                cancellable=not interrupted,
            )
            runtime.cache_touches.clear()
        except Exception as exc:
            if not interrupted:
                raise
            self._record_event(
                "pdf-cache-touch",
                "No se pudieron persistir toques PDF al cancelar",
                {
                    "pending_touches": len(runtime.cache_touches),
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:2000],
                },
                level="warning",
            )

    def _report_extraction(
        self,
        runtime: _ExtractionRuntime,
        *,
        finished: bool = False,
        description: str = "Procesando ruta PDF",
    ) -> None:
        stats = runtime.stats
        active_work = min(
            len(runtime.pending),
            self._resource_gate.active_count,
        )
        self._report(
            stats.processed,
            runtime.expected_total,
            cache_hits=stats.cache_hits,
            cached_errors=stats.cached_errors,
            new_work=stats.new_documents,
            cache_refreshes=stats.cache_refreshes,
            retries=stats.retried_documents,
            retry_pages=stats.retry_pages_planned,
            completed_work=stats.extracted,
            errors=stats.errors,
            timeouts=stats.document_timeouts,
            recycled=stats.unrecoverable_recycled,
            partial=stats.partial_documents,
            protected=stats.protected,
            active_work=active_work,
            queued_work=max(0, len(runtime.pending) - active_work),
            memory_waits=self._resource_gate.wait_count,
            page_progress=runtime.active_page_progress,
            finished=finished,
            description=description,
        )

    def _finish_extraction_phase(
        self,
        plan: _PdfRunPlan,
        stats: _ExtractionStats,
        extraction_started: int,
    ) -> None:
        self._record_event(
            "pdf-extraction",
            "Extracción PDF completada",
            {
                "elapsed_ns": time.perf_counter_ns() - extraction_started,
                "selected_documents": stats.total,
                "candidate_pool": plan.candidate_pool,
                "skipped_by_size": max(0, plan.candidate_pool - plan.eligible_candidates),
                "skipped_by_count": max(0, plan.eligible_candidates - stats.total),
                "processed": stats.processed,
                "cache_hits": stats.cache_hits,
                "cached_errors": stats.cached_errors,
                "new_documents": stats.new_documents,
                "cache_refreshes": stats.cache_refreshes,
                "retried_documents": stats.retried_documents,
                "retry_pages_planned": stats.retry_pages_planned,
                "errors": stats.errors,
                "unrecoverable_recycled": stats.unrecoverable_recycled,
                "protected": stats.protected,
                "warning_documents": stats.warning_documents,
                "mupdf_warnings": stats.mupdf_warnings,
                "effective_worker_memory_bytes": self._worker_memory_reservation,
                "job_memory_limit_bytes": self._job_memory_limit,
                "cache_repair_required": plan.extraction_repair_required,
                "cache_missing_documents": (
                    0
                    if plan.extraction_coverage is None
                    else plan.extraction_coverage.missing_documents
                ),
                "cache_missing_pages": (
                    0
                    if plan.extraction_coverage is None
                    else plan.extraction_coverage.missing_pages
                ),
            },
        )
        extraction_phase_summary: dict[str, object] = {
            "selected_documents": stats.total,
            "processed": stats.processed,
            "extracted": stats.extracted,
            "errors": stats.errors,
            "partial_documents": stats.partial_documents,
            "document_timeouts": stats.document_timeouts,
            "resumed_skip": plan.skip_extraction,
            "cache_repair_required": plan.extraction_repair_required,
        }
        if not plan.skip_extraction:
            self._complete_phase("extraction", extraction_phase_summary)

    def _run_text_dedup_phase(
        self,
        skip_text_dedup: bool,
    ) -> tuple[int, int, int, int]:
        if skip_text_dedup:
            return 0, 0, 0, 0
        self._begin_phase("text_dedup")
        started = time.perf_counter_ns()
        outcome = self._deduplicate_text()
        self.cancellation.checkpoint()
        groups, candidates, trashed, skips = outcome
        summary: dict[str, object] = {
            "elapsed_ns": time.perf_counter_ns() - started,
            "groups": groups,
            "candidates": candidates,
            "trashed": trashed,
            "skips": skips,
        }
        self._record_event(
            "pdf-text-dedup",
            "Duplicados textuales PDF evaluados",
            summary,
        )
        self._complete_phase("text_dedup", summary)
        return outcome

    def _run_derived_phase(
        self,
        skip_derived: bool,
    ) -> tuple[PdfDerivedSummary, int, int]:
        if skip_derived:
            derived = PdfDerivedSummary()
        else:
            self._begin_phase("derived")
            derived = PdfDerivedIndexer(
                self.config.state_path,
                self.run_id,
                workers=self.config.workers,
                similarity_threshold=self.config.similarity_threshold,
                profile_timeout_seconds=self.config.document_timeout_seconds,
                retry_profile_errors=self.config.retry_errors,
                min_free_bytes=self.config.min_free_bytes,
                resource_gate=self._resource_gate,
                profile_memory_bytes=self._worker_memory_reservation,
                progress=self.progress,
                cancellation=self.cancellation,
            ).run()
        self.cancellation.checkpoint()
        if self.config.selection.active:
            cache_documents_pruned = cache_rows_pruned = 0
        else:
            cache_documents_pruned, cache_rows_pruned = self._prune_pdf_cache()
        self._record_event(
            "pdf-derived",
            "Índices derivados PDF actualizados",
            {
                "fts_elapsed_ns": derived.fts_elapsed_ns,
                "text_signatures_elapsed_ns": derived.text_signatures_elapsed_ns,
                "profiles_elapsed_ns": derived.profiles_elapsed_ns,
                "text_similarity_elapsed_ns": derived.text_similarity_elapsed_ns,
                "template_similarity_elapsed_ns": derived.template_similarity_elapsed_ns,
                "layout_similarity_elapsed_ns": derived.layout_similarity_elapsed_ns,
                "fts_pages_indexed": derived.fts_pages_indexed,
                "profiles_built": derived.profiles_built,
                "profile_errors": derived.profile_errors,
                "layout_pages_mapped": derived.layout_pages_mapped,
                "layout_similarity_pairs": derived.layout_similarity_pairs,
                "layout_groups": derived.layout_groups,
                "fts_rows_repaired": derived.fts_rows_repaired,
                "cache_documents_pruned": cache_documents_pruned,
                "cache_rows_pruned": cache_rows_pruned,
            },
        )
        if not skip_derived:
            self._complete_phase(
                "derived",
                {
                    "fts_pages_indexed": derived.fts_pages_indexed,
                    "profiles_built": derived.profiles_built,
                    "profile_errors": derived.profile_errors,
                    "layout_pages_mapped": derived.layout_pages_mapped,
                    "cache_documents_pruned": cache_documents_pruned,
                    "cache_rows_pruned": cache_rows_pruned,
                },
            )
        return derived, cache_documents_pruned, cache_rows_pruned

    def _build_run_summary(
        self,
        plan: _PdfRunPlan,
        extraction: _ExtractionStats,
        text_dedup: tuple[int, int, int, int],
        derived: PdfDerivedSummary,
        cache_documents_pruned: int,
        cache_rows_pruned: int,
    ) -> PdfRouteSummary:
        groups, text_candidates, trashed, skips = text_dedup
        processing_provenance = self.config.processing_provenance
        coverage = plan.derived_coverage
        extraction_coverage = plan.extraction_coverage
        return PdfRouteSummary(
            processing_signature=processing_provenance.signature,
            processing_provenance=processing_provenance.manifest,
            candidate_pool=plan.candidate_pool,
            candidates=extraction.total,
            skipped_by_size=max(0, plan.candidate_pool - plan.eligible_candidates),
            skipped_by_count=max(0, plan.eligible_candidates - extraction.total),
            processed=extraction.processed,
            cache_hits=extraction.cache_hits,
            cached_errors=extraction.cached_errors,
            new_documents=extraction.new_documents,
            cache_refreshes=extraction.cache_refreshes,
            retried_documents=extraction.retried_documents,
            retry_pages_planned=extraction.retry_pages_planned,
            extracted=extraction.extracted,
            errors=extraction.errors,
            unrecoverable_recycled=extraction.unrecoverable_recycled,
            protected=extraction.protected,
            native_pages=extraction.native_pages,
            ocr_pages=extraction.ocr_pages,
            text_duplicate_groups=groups,
            text_duplicate_candidates=text_candidates,
            text_duplicates_trashed=trashed,
            text_duplicate_skips=skips,
            fts_pages_indexed=derived.fts_pages_indexed,
            text_signatures_built=derived.text_signatures_built,
            profiles_built=derived.profiles_built,
            profile_errors=derived.profile_errors,
            text_similarity_pairs=derived.text_similarity_pairs,
            template_similarity_pairs=derived.template_similarity_pairs,
            layout_similarity_pairs=derived.layout_similarity_pairs,
            layout_groups=derived.layout_groups,
            layout_pages_mapped=derived.layout_pages_mapped,
            partial_documents=extraction.partial_documents,
            page_errors=extraction.page_errors,
            document_timeouts=extraction.document_timeouts,
            warning_documents=extraction.warning_documents,
            mupdf_warnings=extraction.mupdf_warnings,
            pdf_cache_documents_pruned=cache_documents_pruned,
            pdf_cache_rows_pruned=cache_rows_pruned,
            fts_rows_repaired=derived.fts_rows_repaired,
            effective_worker_memory_bytes=self._worker_memory_reservation,
            memory_waits=self._resource_gate.wait_count,
            resumed_from_run_id=self.config.resume_source_run_id,
            extraction_phase_skipped=plan.skip_extraction,
            text_dedup_phase_skipped=plan.skip_text_dedup,
            derived_phase_skipped=plan.skip_derived,
            derived_cache_repair_required=plan.derived_repair_required,
            derived_missing_fts_pages=0 if coverage is None else coverage.missing_fts_pages,
            derived_missing_profile_pages=(
                0 if coverage is None else coverage.missing_profile_pages
            ),
            extraction_cache_repair_required=plan.extraction_repair_required,
            extraction_missing_documents=(
                0
                if extraction_coverage is None
                else extraction_coverage.missing_documents
            ),
            extraction_missing_pages=(
                0 if extraction_coverage is None else extraction_coverage.missing_pages
            ),
        )

    def _adopt_resumed_inventory(self) -> None:
        """Associate unchanged extracted documents with the new route-only run."""

        with serialized_pdf_write(), _database(self.config.state_path) as connection:
            connection.execute(
                """UPDATE documents SET last_seen_run_id=?,
                path=(SELECT i.path FROM pdf_inventory i
                    WHERE i.file_key=documents.file_key),
                updated_ns=? WHERE EXISTS(
                    SELECT 1 FROM pdf_inventory i WHERE i.file_key=documents.file_key
                    AND i.last_seen_run_id=? AND i.size=documents.size
                    AND i.mtime_ns=documents.mtime_ns
                    AND (i.birthtime_ns=documents.birthtime_ns
                        OR documents.birthtime_ns=-1))""",
                (self.run_id, time.time_ns(), self.run_id),
            )

    def _stage_pdf_inventory(self) -> None:
        """Mark every live PDF before size/count selection without retaining paths."""

        batch: list[tuple[str, str, int, int, int, int]] = []

        def flush() -> None:
            self.cancellation.checkpoint()
            if not batch:
                return
            with (
                serialized_pdf_write(),
                _database(self.config.state_path) as connection,
            ):
                connection.executemany(
                    """INSERT OR REPLACE INTO pdf_inventory(
                    file_key,path,size,mtime_ns,birthtime_ns,last_seen_run_id)
                    VALUES(?,?,?,?,?,?)""",
                    batch,
                )
            batch.clear()

        selection = self.config.selection
        if selection.paths or selection.recommendations:
            iterator = self.framework_state.iter_selected_route_candidates(
                self.run_id,
                "application/pdf",
                "pdf",
                selection,
            )
        else:
            iterator = self.framework_state.iter_route_candidates(self.run_id, "application/pdf")
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
            if len(batch) >= PDF_INVENTORY_BATCH:
                flush()
        flush()

    def _candidate_snapshots(self) -> Iterator[FileSnapshot]:
        """Stream selected inventory directly from SQLite in deterministic order."""

        if not self.config.state_path.is_file():
            yield from self._fallback_candidate_snapshots()
            return
        yield from self._database_candidate_snapshots()

    def _fallback_candidate_snapshots(self) -> Iterator[FileSnapshot]:
        limit = self.config.max_documents
        if limit is not None:
            yield from self._limited_fallback_candidates(limit)
            return
        for snapshot in self.framework_state.iter_route_candidates(self.run_id, "application/pdf"):
            if self._candidate_within_size_limit(snapshot):
                yield snapshot

    def _limited_fallback_candidates(
        self,
        limit: int,
    ) -> Iterator[FileSnapshot]:
        priority_keys = self._priority_candidate_keys(limit)
        priority_rank = {key: rank for rank, key in enumerate(priority_keys)}
        prioritized: dict[str, FileSnapshot] = {}
        fallback: list[FileSnapshot] = []
        for snapshot in self.framework_state.iter_route_candidates(self.run_id, "application/pdf"):
            if not self._candidate_within_size_limit(snapshot):
                continue
            key = _file_key(snapshot)
            if key in priority_rank:
                prioritized[key] = snapshot
            elif len(fallback) < limit:
                fallback.append(snapshot)
        yielded = 0
        for key in priority_keys:
            snapshot = prioritized.get(key)
            if snapshot is not None:
                yield snapshot
                yielded += 1
        for snapshot in fallback:
            if yielded >= limit:
                return
            yield snapshot
            yielded += 1

    def _candidate_within_size_limit(self, snapshot: FileSnapshot) -> bool:
        limit = self.config.max_file_bytes
        return limit is None or snapshot.size <= limit

    def _database_candidate_snapshots(self) -> Iterator[FileSnapshot]:
        where_sql, parameters = self._candidate_where_sql()
        protected_priority = (
            3 if self.config.retry_errors or self.config.selection.force_incomplete_retry else 5
        )
        limit_sql = "" if self.config.max_documents is None else " LIMIT ?"
        if self.config.max_documents is not None:
            parameters.append(self.config.max_documents)
        order_sql = self._candidate_order_sql(protected_priority)
        with _database(self.config.state_path, readonly=True) as connection:
            rows = connection.execute(
                """SELECT i.file_key,i.path,i.size,i.mtime_ns,i.birthtime_ns
                FROM pdf_inventory i LEFT JOIN documents d ON d.file_key=i.file_key
                WHERE """
                + where_sql
                + order_sql
                + limit_sql,
                parameters,
            )
            for row in rows:
                self.cancellation.checkpoint()
                yield self._inventory_row_snapshot(row)

    def _candidate_order_sql(self, protected_priority: int) -> str:
        if self._cache_first_candidate_order():
            return """
                ORDER BY CASE
                    WHEN d.status='done' AND i.size=d.size
                        AND i.mtime_ns=d.mtime_ns
                        AND i.birthtime_ns=d.birthtime_ns THEN 0
                    WHEN d.status='done' AND d.birthtime_ns=-1
                        AND d.binary_xxh3_128 IS NOT NULL THEN 1
                    WHEN d.status='protected' AND i.size=d.size
                        AND i.mtime_ns=d.mtime_ns
                        AND i.birthtime_ns=d.birthtime_ns THEN 2
                    WHEN d.status='partial' AND d.error_type IS NULL THEN 3
                    WHEN d.status='done' THEN 4
                    WHEN d.file_key IS NULL THEN 5
                    WHEN d.status='processing' THEN 6
                    WHEN d.status='partial' THEN 7
                    WHEN d.status='error'
                        AND d.error_type<>'PdfDocumentTimeout' THEN 8
                    WHEN d.status='error' THEN 9 ELSE 6 END,
                CASE WHEN d.status IN ('processing','partial','error')
                    THEN i.size ELSE 0 END,
                COALESCE(d.updated_ns,0),i.path"""
        return (
            """
                ORDER BY CASE
                    WHEN d.status='error' THEN 0
                    WHEN d.status='processing' THEN 1
                    WHEN d.status='partial' THEN 2
                    WHEN d.status='protected' THEN """
            + str(protected_priority)
            + """
                    WHEN d.file_key IS NULL THEN 4 ELSE 5 END,
                COALESCE(d.updated_ns,0),i.path"""
        )

    @staticmethod
    def _inventory_row_snapshot(row: sqlite3.Row) -> FileSnapshot:
        volume_hex, file_hex = str(row["file_key"]).split(":", 1)
        return FileSnapshot(
            str(row["path"]),
            int(volume_hex, 16),
            int(file_hex, 16),
            int(row["size"]),
            int(row["mtime_ns"]),
            int(row["birthtime_ns"]),
        )

    def _cache_first_candidate_order(self) -> bool:
        """Prefer reusable rows only for an unbounded normal route run."""

        selection = self.config.selection
        return (
            self.config.max_documents is None
            and not getattr(self, "_extraction_repair_required", False)
            and not self.config.retry_errors
            and not selection.force_incomplete_retry
            and not selection.statuses
            and not selection.error_types
            and not selection.failed_pages_only
        )

    def _candidate_where_sql(
        self,
        *,
        include_size: bool = True,
    ) -> tuple[str, list[object]]:
        selection = self.config.selection
        clauses = ["i.last_seen_run_id=?"]
        parameters: list[object] = [self.run_id]
        if include_size and self.config.max_file_bytes is not None:
            clauses.append("i.size<=?")
            parameters.append(self.config.max_file_bytes)
        if selection.statuses:
            placeholders = ",".join("?" for _ in selection.statuses)
            clauses.append(f"COALESCE(d.status,'pending') IN ({placeholders})")
            parameters.extend(selection.statuses)
        if selection.error_types:
            placeholders = ",".join("?" for _ in selection.error_types)
            clauses.append(f"d.error_type IN ({placeholders})")
            parameters.extend(selection.error_types)
        if selection.failed_pages_only:
            retryable_sql = PDF_RETRYABLE_PAGE_ERROR_SQL.replace(
                "error_type", "e.error_type"
            ).replace("error_message", "e.error_message")
            clauses.append(
                """EXISTS(SELECT 1 FROM page_errors e
                WHERE e.file_key=d.file_key AND e.processing_signature=
                d.processing_signature AND """
                + retryable_sql
                + ")"
            )
        return " AND ".join(clauses), parameters

    def _candidate_counts(self) -> tuple[int, int]:
        with _database(self.config.state_path, readonly=True) as connection:
            total_where, total_parameters = self._candidate_where_sql(include_size=False)
            eligible_where, eligible_parameters = self._candidate_where_sql()
            total = int(
                connection.execute(
                    """SELECT COUNT(*) FROM pdf_inventory i
                    LEFT JOIN documents d ON d.file_key=i.file_key WHERE """
                    + total_where,
                    total_parameters,
                ).fetchone()[0]
            )
            eligible = int(
                connection.execute(
                    """SELECT COUNT(*) FROM pdf_inventory i
                    LEFT JOIN documents d ON d.file_key=i.file_key WHERE """
                    + eligible_where,
                    eligible_parameters,
                ).fetchone()[0]
            )
        return total, eligible

    def _priority_candidate_keys(self, limit: int) -> tuple[str, ...]:
        """Read at most MaxCount unfinished identities in deterministic order."""

        if not self.config.state_path.is_file():
            return ()
        where_sql, parameters = self._candidate_where_sql()
        protected_priority = (
            3 if self.config.retry_errors or self.config.selection.force_incomplete_retry else 5
        )
        parameters.append(limit)
        with _database(self.config.state_path, readonly=True) as connection:
            rows = connection.execute(
                """SELECT i.file_key FROM pdf_inventory i
                LEFT JOIN documents d ON i.file_key=d.file_key WHERE """
                + where_sql
                + """
                ORDER BY CASE d.status
                    WHEN 'error' THEN 0 WHEN 'processing' THEN 1
                    WHEN 'partial' THEN 2 WHEN 'protected' THEN """
                + str(protected_priority)
                + """ ELSE 4 END,
                COALESCE(d.updated_ns,0),i.path LIMIT ?""",
                parameters,
            )
            return tuple(str(row[0]) for row in rows)

    def _process_document(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        worker_context: _PdfWorkerContext | None = None,
    ) -> _DocumentResult:
        self.cancellation.checkpoint()
        if worker_context is None:
            # Direct callers of this private compatibility seam still build a
            # context lazily.  Production extraction always supplies the
            # coordinator-owned context from ``_submit_extraction``.
            resumable_pages = self._resumable_pages(snapshot)
            worker_context = _PdfWorkerContext(
                timeout_seconds=self._effective_document_timeout(snapshot),
                resumable_pages=resumable_pages,
                initial_staged_pages=self._successful_staged_page_count(snapshot),
                extraction=self._isolated_extraction_config(
                    snapshot,
                    1.0,
                    resumable_pages=resumable_pages,
                ),
            )
        with self._resource_gate.admit(
            snapshot.size,
            reservation_bytes=self._worker_memory_reservation,
        ):
            for attempt in range(TRANSIENT_RETRIES_PER_RUN + 1):
                self.cancellation.checkpoint()
                timeout_seconds = worker_context.timeout_seconds
                if timeout_seconds is not None:
                    result = self._process_document_isolated(
                        snapshot,
                        binary_digest,
                        timeout_seconds=timeout_seconds,
                        ocr_scale_factor=0.75**attempt,
                        worker_context=worker_context,
                    )
                else:
                    result = self._process_document_local(
                        snapshot,
                        binary_digest,
                        worker_context=worker_context,
                    )
                if not result.transient or attempt >= TRANSIENT_RETRIES_PER_RUN:
                    return result
                worker_context = self._refresh_worker_context(snapshot)
                if self.cancellation.wait(0.25):
                    self.cancellation.checkpoint()
        raise RuntimeError("unreachable PDF retry state")

    def _process_document_isolated(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        *,
        timeout_seconds: float | None = None,
        ocr_scale_factor: float = 1.0,
        worker_context: _PdfWorkerContext | None = None,
    ) -> _DocumentResult:
        if worker_context is not None:
            if timeout_seconds is None:
                timeout_seconds = worker_context.timeout_seconds
            extraction = replace(
                worker_context.extraction,
                ocr_scale_factor=min(
                    worker_context.extraction.ocr_scale_factor,
                    ocr_scale_factor,
                ),
            )
            initial_staged_pages = worker_context.initial_staged_pages
        else:
            if timeout_seconds is None:
                timeout_seconds = self._effective_document_timeout(snapshot)
            extraction = self._isolated_extraction_config(snapshot, ocr_scale_factor)
            initial_staged_pages = self._successful_staged_page_count(snapshot)
        if timeout_seconds is None:
            raise ValueError("isolated PDF processing requires a timeout")
        state = _IsolatedExtractionState(initial_staged_pages=initial_staged_pages)
        try:
            early_result = self._stream_isolated_document(
                snapshot,
                extraction,
                timeout_seconds,
                state,
            )
            if early_result is not None:
                return early_result
            return self._complete_isolated_document(
                snapshot,
                binary_digest,
                state,
            )
        except CancellationRequested:
            self._handle_isolated_cancellation(snapshot, state)
            raise
        except PdfDocumentTimeout as exc:
            return self._handle_isolated_timeout(snapshot, state, exc)
        except Exception as exc:
            return self._handle_isolated_failure(snapshot, state, exc)

    def _isolated_extraction_config(
        self,
        snapshot: FileSnapshot,
        ocr_scale_factor: float,
        *,
        resumable_pages: tuple[int, frozenset[int], int] | None = None,
        structural_recovery_reason: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> IsolatedExtractionConfig:
        if resumable_pages is None:
            resumable_pages = self._resumable_pages(snapshot, connection=connection)
        skip_before, only_pages, prior_ocr_pages = resumable_pages
        if structural_recovery_reason is None:
            structural_recovery_reason = self._structural_recovery_reason(
                snapshot,
                connection=connection,
            )
        if structural_recovery_reason is not None:
            skip_before = 0
            only_pages = frozenset()
            prior_ocr_pages = 0
        if only_pages or skip_before > (self.config.page_start or 1) - 1:
            ocr_scale_factor = min(ocr_scale_factor, 0.75)
        return IsolatedExtractionConfig(
            self.config.ocr_mode,
            self.config.ocr_lang,
            self.config.dpi,
            self.config.min_page_chars,
            self.config.max_page_text_chars,
            self.config.max_render_pixels,
            self.config.max_ocr_pages,
            self.config.ocr_timeout_seconds,
            self.config.pdfminer_fallback,
            self.config.max_pages,
            self.config.page_start,
            self.config.page_end,
            self.config.fail_fast_pages,
            skip_before,
            only_pages,
            prior_ocr_pages,
            self.config.tesseract_cmd,
            self.config.tessdata_dir,
            ocr_scale_factor,
            structural_recovery_reason,
            self.config.ocr_profile,
            self.config.processing_signature,
            (self._ocr_runtime.traineddata_hashes if self._ocr_runtime is not None else ()),
        )

    def _stream_isolated_document(
        self,
        snapshot: FileSnapshot,
        extraction: IsolatedExtractionConfig,
        timeout_seconds: float,
        state: _IsolatedExtractionState,
    ) -> _DocumentResult | None:
        messages = stream_isolated_extraction(
            snapshot,
            extraction,
            timeout_seconds=timeout_seconds,
            ocr_slots=self._ocr_slots,
            cancellation=self.cancellation,
            memory_limit_bytes=self._job_memory_limit,
        )
        for message in messages:
            self.cancellation.checkpoint()
            result, flush_eligible = self._consume_isolated_message(
                snapshot,
                state,
                message,
            )
            if result is not None:
                return result
            if flush_eligible and (
                len(state.batch) >= TEXT_BATCH_PAGES or state.batch_bytes >= PROMOTION_BATCH_BYTES
            ):
                self._flush_isolated_batch(snapshot, state)
        return None

    def _consume_isolated_message(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        message: tuple[Any, ...],
    ) -> tuple[_DocumentResult | None, bool]:
        kind = message[0]
        if kind == "warnings":
            state.warning_count += int(message[1])
            state.warning_samples = tuple(dict.fromkeys((*state.warning_samples, *message[2])))[:20]
            return None, False
        if kind == "recovery":
            state.recovery_evidence = dict(message[1])
            return None, False
        if kind == "restart":
            self._restart_isolated_attempt(snapshot, state)
            return None, False
        if kind == "protected":
            return self._handle_isolated_protected(snapshot, state), False
        if kind == "fatal":
            raise PdfChildReportedError(
                str(message[1]),
                str(message[2]),
                phase=str(message[3]) if len(message) > 3 else "unknown",
            )
        if kind == "header":
            self._prepare_isolated_header(snapshot, state, message)
            return None, False
        self._consume_isolated_page_message(state, message)
        return None, True

    def _restart_isolated_attempt(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
    ) -> None:
        state.reset_for_structural_recovery()

        def restart(connection: sqlite3.Connection) -> None:
            self._restart_structural_recovery_attempt(connection, snapshot)

        self._owner_call(restart)

    def _handle_isolated_protected(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
    ) -> _DocumentResult:
        self._store_failure(
            snapshot,
            "protected",
            "EncryptedPdf",
            "password required",
        )
        self._store_document_warnings(
            snapshot,
            "extract",
            state.warning_count,
            state.warning_samples,
        )
        self._publish_review(
            snapshot,
            classify_pdf_failure(
                "EncryptedPdf",
                "password required",
                phase="open",
            ),
            "protected",
            evidence={"message": "password required"},
        )
        return _DocumentResult(
            "protected",
            warning_count=state.warning_count,
        )

    def _prepare_isolated_header(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        message: tuple[Any, ...],
    ) -> None:
        _, state.page_count, state.start, state.end, state.metadata = message

        def prepare(connection: sqlite3.Connection) -> None:
            self._prepare_document(
                connection,
                snapshot,
                state.page_count,
                state.metadata,
            )

        self._owner_call(prepare)
        state.prepared = True

    def _consume_isolated_page_message(
        self,
        state: _IsolatedExtractionState,
        message: tuple[Any, ...],
    ) -> None:
        kind = message[0]
        if kind == "page":
            source, text = message[2], message[3]
            state.batch.append(message)
            state.batch_bytes += len(text.encode("utf-8"))
            if len(message) > 4 and message[4] is not None:
                state.batch_bytes += len(
                    json.dumps(
                        message[4],
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            state.native_pages += int(source != "ocr")
            state.ocr_pages += int(source == "ocr")
        elif kind == "page_error":
            state.batch.append(message)
            state.batch_bytes += len(str(message[3]).encode("utf-8"))
            state.page_errors += 1
            state.page_diagnostic = classify_pdf_failure(
                str(message[2]),
                str(message[3]),
                phase="page_extraction",
            )
        elif kind == "page_error_limit":
            self._record_isolated_page_error_limit(state, message)
        elif kind == "done":
            _, state.page_count, state.start, reported_end = message
            if state.page_error_limit is None:
                state.end = reported_end

    @staticmethod
    def _record_isolated_page_error_limit(
        state: _IsolatedExtractionState,
        message: tuple[Any, ...],
    ) -> None:
        (
            _,
            page_number,
            consecutive_errors,
            skipped_pages,
            error_type,
            error_message,
        ) = message
        state.page_error_limit = {
            "last_attempted_page": int(page_number) + 1,
            "consecutive_errors": int(consecutive_errors),
            "skipped_pages": int(skipped_pages),
            "last_error_type": str(error_type),
            "last_error_message": str(error_message),
        }
        state.end = min(state.end, int(page_number) + 1)
        state.page_diagnostic = classify_pdf_failure(
            "PdfPageSequenceAborted",
            f"{consecutive_errors} consecutive page extraction failures; "
            f"last error {error_type}: {error_message}",
            phase="page_extraction",
        )

    def _flush_isolated_batch(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        *,
        clear: bool = True,
    ) -> None:
        if not state.batch:
            return
        self._flush_extraction_batch(snapshot, state.batch)
        if clear:
            state.batch.clear()
            state.batch_bytes = 0

    def _complete_isolated_document(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        state: _IsolatedExtractionState,
    ) -> _DocumentResult:
        if not state.prepared:
            raise RuntimeError("isolated extractor produced no document header")
        self._flush_isolated_batch(snapshot, state, clear=False)
        status: Literal["done", "partial"] = (
            "partial" if state.page_errors or state.page_error_limit is not None else "done"
        )
        is_partial = (
            state.start > 0
            or state.end < state.page_count
            or self.config.page_end is not None
            or self.config.max_pages is not None
            or state.page_error_limit is not None
        )
        self._promote_isolated_document(
            snapshot,
            binary_digest,
            state,
            status,
            is_partial,
        )
        self._reconcile_isolated_findings(snapshot, state, status)
        return _DocumentResult(
            status,
            state.native_pages,
            state.ocr_pages,
            page_errors=state.page_errors,
            warning_count=state.warning_count,
        )

    def _promote_isolated_document(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        state: _IsolatedExtractionState,
        status: Literal["done", "partial"],
        is_partial: bool,
    ) -> None:
        error_limit = state.page_error_limit

        def promote(connection: sqlite3.Connection) -> None:
            self._promote_document(
                connection,
                snapshot,
                state.page_count,
                max(0, state.end - state.start),
                state.metadata,
                binary_digest,
                status=status,
                page_start=state.start + 1,
                page_end=state.end,
                is_partial=is_partial,
                page_errors=state.page_errors,
                document_error_type=("PdfPageSequenceAborted" if error_limit is not None else None),
                document_error_message=(
                    json.dumps(
                        error_limit,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )[:2000]
                    if error_limit is not None
                    else None
                ),
            )
            self._store_document_warnings(
                snapshot,
                "extract",
                state.warning_count,
                state.warning_samples,
                connection=connection,
            )

        self._owner_call(promote)

    def _reconcile_isolated_findings(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        status: Literal["done", "partial"],
    ) -> None:
        if state.recovery_evidence is not None:
            diagnostic = classify_pdf_failure(
                "PdfStructuralRecovered",
                str(
                    state.recovery_evidence.get(
                        "primary_error",
                        "recovered",
                    )
                ),
                phase="structural_recovery",
                recovered=True,
            )
            self._publish_review(
                snapshot,
                diagnostic,
                status,
                evidence={
                    **state.recovery_evidence,
                    "message": "PDF recovered",
                },
            )
            self._reconcile_review(
                snapshot,
                "current PDF extraction recovered structural damage",
                active_reason_codes=(diagnostic.reason_code,),
            )
            return
        if state.page_diagnostic is not None:
            evidence: dict[str, object] = {"message": "one or more PDF pages failed"}
            if state.page_error_limit is not None:
                evidence["page_error_limit"] = state.page_error_limit
            self._publish_review(
                snapshot,
                state.page_diagnostic,
                status,
                evidence=evidence,
            )
            self._reconcile_review(
                snapshot,
                "current PDF extraction completed with page diagnostics",
                active_reason_codes=(state.page_diagnostic.reason_code,),
            )
            return
        self._reconcile_review(snapshot, "PDF extraction completed")

    def _handle_isolated_cancellation(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
    ) -> None:
        self._flush_isolated_batch(snapshot, state)
        staged_pages = self._successful_staged_page_count(snapshot)
        if staged_pages > 0:
            self._store_failure(
                snapshot,
                "partial",
                "InterruptedPdfProcessing",
                f"[durable-progress:{staged_pages}] processing cancelled",
                transient=True,
                reset_retry_count=True,
            )

    def _handle_isolated_timeout(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        exc: PdfDocumentTimeout,
    ) -> _DocumentResult:
        self._flush_isolated_batch(snapshot, state)
        current_staged_pages = self._successful_staged_page_count(snapshot)
        made_progress = current_staged_pages > state.initial_staged_pages
        stored_message = (
            f"[durable-progress:{state.initial_staged_pages}->{current_staged_pages}] "
            if made_progress
            else f"[no-durable-progress:{current_staged_pages}] "
        ) + str(exc)
        diagnostic = classify_pdf_failure(
            type(exc).__name__,
            str(exc),
            phase=exc.phase,
        )
        partial_timeout = current_staged_pages > 0
        stored_status = "partial" if partial_timeout else "error"
        self._store_failure(
            snapshot,
            stored_status,
            type(exc).__name__,
            stored_message[:2000],
            transient=diagnostic.retryable,
            reset_retry_count=made_progress,
        )
        self._store_document_warnings(
            snapshot,
            "extract",
            state.warning_count,
            state.warning_samples,
        )
        self._publish_review(
            snapshot,
            diagnostic,
            stored_status,
            evidence={
                "message": str(exc),
                "initial_staged_pages": state.initial_staged_pages,
                "current_staged_pages": current_staged_pages,
                "durable_progress": made_progress,
            },
        )
        return _DocumentResult(
            "partial" if partial_timeout else "error",
            timed_out=True,
            warning_count=state.warning_count,
        )

    def _handle_isolated_failure(
        self,
        snapshot: FileSnapshot,
        state: _IsolatedExtractionState,
        exc: Exception,
    ) -> _DocumentResult:
        self._flush_isolated_batch(snapshot, state)
        error_type, error_message, phase, exit_code = self._isolated_failure_details(exc)
        diagnostic = classify_pdf_failure(
            error_type,
            error_message,
            phase=phase,
            exit_code=exit_code,
        )
        transient = diagnostic.retryable
        staged_pages = self._successful_staged_page_count(snapshot)
        stored_status: Literal["error", "partial"] = (
            "partial"
            if error_type == "PdfStructuralRecoveryFailed" and staged_pages > 0
            else "error"
        )
        diagnostic = self._preserve_recoverable_page_diagnostic(
            diagnostic,
            stored_status,
        )
        self._store_failure(
            snapshot,
            stored_status,
            error_type,
            error_message[:2000],
            transient=transient,
        )
        self._store_document_warnings(
            snapshot,
            "extract",
            state.warning_count,
            state.warning_samples,
        )
        evidence: dict[str, object] = {
            "message": error_message,
            "successful_staged_pages": staged_pages,
        }
        if isinstance(exc, PdfChildProcessError):
            evidence["memory_limit_bytes"] = exc.memory_limit_bytes
        self._publish_review(
            snapshot,
            diagnostic,
            stored_status,
            evidence=evidence,
        )
        recycled = self._maybe_recycle_unrecoverable_pdf(
            snapshot,
            error_type,
            staged_pages,
            evidence,
        )
        return _DocumentResult(
            stored_status,
            warning_count=state.warning_count,
            transient=transient,
            recycled=recycled,
        )

    @staticmethod
    def _isolated_failure_details(
        exc: Exception,
    ) -> tuple[str, str, str, int | None]:
        if isinstance(exc, PdfChildReportedError):
            return exc.child_error_type, exc.detail, exc.phase, None
        return (
            type(exc).__name__,
            str(exc),
            cast(str, getattr(exc, "phase", "unknown")),
            exc.exit_code if isinstance(exc, PdfChildProcessError) else None,
        )

    @staticmethod
    def _preserve_recoverable_page_diagnostic(
        diagnostic: PdfFailureDiagnostic,
        stored_status: Literal["error", "partial"],
    ) -> PdfFailureDiagnostic:
        if stored_status != "partial" or diagnostic.recommendation != "deletion_candidate":
            return diagnostic
        return PdfFailureDiagnostic(
            diagnostic.error_type,
            diagnostic.phase,
            "pdf_structural_damage_with_recoverable_pages",
            False,
            "manual_review",
            diagnostic.confidence,
            diagnostic.exit_code,
        )

    def _maybe_recycle_unrecoverable_pdf(
        self,
        snapshot: FileSnapshot,
        error_type: str,
        staged_pages: int,
        evidence: dict[str, object],
    ) -> bool:
        if (
            error_type != "PdfStructuralRecoveryFailed"
            or staged_pages != 0
            or not self.config.apply_actions
            or not self.config.pdfminer_fallback
        ):
            return False
        return self._recycle_unrecoverable_pdf(snapshot, evidence)

    def _structural_recovery_reason(
        self,
        snapshot: FileSnapshot,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str | None:
        """Return the one-shot repair reason for current or legacy partial rows."""

        def read(target: sqlite3.Connection) -> sqlite3.Row | None:
            return target.execute(
                """SELECT status,error_type,error_message,
                (SELECT COUNT(*) FROM page_errors e WHERE e.file_key=d.file_key
                    AND e.processing_signature=d.processing_signature) AS errors
                FROM documents d WHERE file_key=?""",
                (_file_key(snapshot),),
            ).fetchone()

        if connection is None:
            row = self._owner_call(read)
        else:
            row = read(connection)
        if row is None or row["status"] != "partial":
            return None
        error_type = str(row["error_type"] or "")
        if error_type == "PdfPageSequenceAborted":
            return f"{error_type}: {row['error_message'] or ''!s}"[:2000]
        if not error_type and int(row["errors"] or 0) >= PDF_PAGE_SEQUENCE_ERROR_LIMIT:
            return (
                "Legacy PdfPageSequenceAborted: retained at least "
                f"{PDF_PAGE_SEQUENCE_ERROR_LIMIT} page errors"
            )
        return None

    def _recycle_unrecoverable_pdf(
        self,
        snapshot: FileSnapshot,
        evidence: dict[str, object],
    ) -> bool:
        """Recycle one unchanged, contentless PDF and synchronize durable state."""

        from neocortex.workflow.actions.actions import FrameworkActions
        from neocortex.persistence.framework_state_writer import FrameworkState

        with self._recycle_lock:
            try:
                actions = FrameworkActions(
                    self.index,
                    cast(FrameworkState, self.framework_state),
                    self.run_id,
                    self.scan_id,
                    apply=True,
                )
                applied, failed, protected = actions.recycle_verified_files(
                    "trash_unrecoverable_pdf",
                    ((snapshot, json.dumps(evidence, ensure_ascii=False)),),
                )
                if applied != 1:
                    self._record_event(
                        "pdf-unrecoverable-recycle",
                        "PDF irrecuperable conservado porque no superó el reciclaje seguro",
                        {
                            "path": snapshot.path,
                            "failed": failed,
                            "protected": protected,
                        },
                        level="warning",
                    )
                    return False

                def delete_cache(connection: sqlite3.Connection) -> None:
                    key = _file_key(snapshot)
                    self._delete_document_cache(connection, key)
                    connection.execute("DELETE FROM documents WHERE file_key=?", (key,))
                    connection.execute("DELETE FROM pdf_inventory WHERE file_key=?", (key,))

                self._owner_call(delete_cache)
                resolved_review = self._resolve_review_generation(
                    snapshot,
                    "pdf_unrecoverable_structural_damage",
                    "PDF irrecuperable enviado a la Papelera de reciclaje",
                )
                if resolved_review != 1:
                    self._record_event(
                        "pdf-unrecoverable-recycle-review",
                        "El hallazgo terminal PDF no coincidió con su generación",
                        {
                            "path": snapshot.path,
                            "reason_code": "pdf_unrecoverable_structural_damage",
                            "candidate_generation": self.run_id,
                            "resolved": resolved_review,
                        },
                        level="warning",
                    )
                self._record_event(
                    "pdf-unrecoverable-recycle",
                    "PDF irrecuperable enviado a la Papelera de reciclaje",
                    {"path": snapshot.path},
                )
                return True
            except Exception as exc:
                self._record_event(
                    "pdf-unrecoverable-recycle",
                    "Falló el reciclaje seguro; el PDF se conservó",
                    {
                        "path": snapshot.path,
                        "error_type": type(exc).__name__,
                        "detail": str(exc)[:2000],
                    },
                    level="error",
                )
                return False

    def _effective_document_timeout(
        self,
        snapshot: FileSnapshot,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> float | None:
        def read(target: sqlite3.Connection) -> tuple[int, int]:
            row = target.execute(
                """SELECT page_count,completed_pages,page_errors_count
                FROM documents WHERE file_key=?""",
                (_file_key(snapshot),),
            ).fetchone()
            if row is None:
                return 0, 0
            page_count = int(row["page_count"] or 0)
            completed = int(row["completed_pages"] or 0)
            failed = int(row["page_errors_count"] or 0)
            return page_count, max(failed, page_count - completed)

        if connection is None:
            page_count, pending_pages = self._owner_call(read)
        else:
            page_count, pending_pages = read(connection)
        return effective_document_timeout_seconds(
            self.config,
            file_size=snapshot.size,
            page_count=page_count,
            pending_pages=pending_pages,
        )

    def _successful_staged_page_count(
        self,
        snapshot: FileSnapshot,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        def read(target: sqlite3.Connection) -> int:
            return int(
                target.execute(
                    """SELECT COUNT(*) FROM page_staging
                    WHERE file_key=? AND processing_signature=? AND source<>'error'""",
                    (_file_key(snapshot), self.config.processing_signature),
                ).fetchone()[0]
            )

        if connection is None:
            return self._owner_call(read)
        return read(connection)

    def _flush_extraction_batch(self, snapshot: FileSnapshot, messages: list[tuple]) -> None:
        """Promote a bounded child-message batch through the sole SQLite writer."""

        self._check_disk()

        def flush(connection: sqlite3.Connection) -> None:
            for message in messages:
                if message[0] == "page":
                    _, page_number, source, text, *tail = message
                    provenance = tail[0] if tail else None
                    self._store_staging_page(
                        connection,
                        _file_key(snapshot),
                        self.config.processing_signature,
                        page_number,
                        source,
                        text,
                        provenance,
                    )
                else:
                    _, page_number, error_type, error_message = message
                    self._store_page_failure(
                        connection,
                        snapshot,
                        page_number,
                        error_type,
                        error_message,
                    )

        self._owner_call(flush)

    def _process_document_local(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        *,
        worker_context: _PdfWorkerContext | None = None,
    ) -> _DocumentResult:
        self.cancellation.checkpoint()
        key = _file_key(snapshot)
        signature = self.config.processing_signature
        if worker_context is None:
            skip_before, only_pages, prior_ocr_pages = self._resumable_pages(snapshot)
        else:
            skip_before, only_pages, prior_ocr_pages = worker_context.resumable_pages
        local_state = _LocalExtractionState(
            file_key=key,
            processing_signature=signature,
            ocr_attempted=prior_ocr_pages,
        )
        document = None
        prepared = False
        try:
            import fitz  # type: ignore[import-untyped]

            fitz.TOOLS.mupdf_display_errors(False)
            fitz.TOOLS.mupdf_display_warnings(False)
            fitz.TOOLS.reset_mupdf_warnings()

            document = fitz.open(snapshot.path)
            if document.needs_pass:
                document.close()
                document = None
                self._store_failure(snapshot, "protected", "EncryptedPdf", "password required")
                return _DocumentResult("protected")
            page_count = int(document.page_count)
            start, end = self._page_bounds(page_count)
            metadata = dict(document.metadata or {})

            def prepare(connection: sqlite3.Connection) -> None:
                self._prepare_document(connection, snapshot, page_count, metadata)

            self._owner_call(prepare)
            prepared = True
            self._extract_local_pages(
                None,
                snapshot,
                document,
                fitz,
                start,
                end,
                skip_before,
                only_pages,
                local_state,
            )
            status: Literal["done", "partial"] = "partial" if local_state.page_errors else "done"
            self._promote_local_document(
                None,
                snapshot,
                page_count,
                start,
                end,
                metadata,
                binary_digest,
                status,
                local_state,
            )
            return self._complete_local_document(snapshot, status, local_state)
        except CancellationRequested:
            raise
        except Exception as exc:
            return self._handle_local_failure(
                snapshot,
                binary_digest,
                prepared,
                exc,
            )
        finally:
            if document is not None:
                document.close()

    def _promote_local_document(
        self,
        connection: sqlite3.Connection | None,
        snapshot: FileSnapshot,
        page_count: int,
        start: int,
        end: int,
        metadata: dict[str, Any],
        binary_digest: str | None,
        status: Literal["done", "partial"],
        state: _LocalExtractionState,
    ) -> None:
        def promote(target: sqlite3.Connection) -> None:
            self._promote_document(
                target,
                snapshot,
                page_count,
                end - start,
                metadata,
                binary_digest,
                status=status,
                page_start=start + 1,
                page_end=end,
                is_partial=start > 0 or end < page_count,
                page_errors=state.page_errors,
            )

        if connection is None:
            self._owner_call(promote)
        else:
            promote(connection)

    def _complete_local_document(
        self,
        snapshot: FileSnapshot,
        status: Literal["done", "partial"],
        state: _LocalExtractionState,
    ) -> _DocumentResult:
        if status == "done":
            self._reconcile_review(
                snapshot,
                "local PDF extraction completed without findings",
            )
        return _DocumentResult(
            status,
            state.native_pages,
            state.ocr_pages,
            page_errors=state.page_errors,
        )

    def _handle_local_failure(
        self,
        snapshot: FileSnapshot,
        binary_digest: str | None,
        prepared: bool,
        exc: Exception,
    ) -> _DocumentResult:
        if self.config.pdfminer_fallback and not prepared:
            try:
                return self._process_with_pdfminer(snapshot, binary_digest)
            except CancellationRequested:
                raise
            except Exception as fallback_exc:
                message = (
                    f"PyMuPDF {type(exc).__name__}: {exc}; "
                    f"pdfminer {type(fallback_exc).__name__}: {fallback_exc}"
                )
                self._store_failure(
                    snapshot,
                    "error",
                    type(fallback_exc).__name__,
                    message[:2000],
                )
                return _DocumentResult("error")
        transient = self._is_transient_error(type(exc).__name__, str(exc))
        self._store_failure(
            snapshot,
            "error",
            type(exc).__name__,
            str(exc)[:2000],
            transient=transient,
        )
        return _DocumentResult("error", transient=transient)

    def _extract_local_pages(
        self,
        connection: sqlite3.Connection | None,
        snapshot: FileSnapshot,
        document: Any,
        fitz: Any,
        start: int,
        end: int,
        skip_before: int,
        only_pages: frozenset[int],
        state: _LocalExtractionState,
    ) -> None:
        for page_number in range(start, end):
            self.cancellation.checkpoint()
            if page_number < skip_before or (only_pages and page_number not in only_pages):
                continue
            source = self._extract_local_page(
                connection,
                snapshot,
                document,
                fitz,
                page_number,
                state,
            )
            self._record_local_page_progress(connection, state, source)

    def _extract_local_page(
        self,
        connection: sqlite3.Connection | None,
        snapshot: FileSnapshot,
        document: Any,
        fitz: Any,
        page_number: int,
        state: _LocalExtractionState,
    ) -> Literal["native", "ocr", "error"]:
        try:
            page = document.load_page(page_number)
            native_text = page.get_text("text") or ""
            quality = native_text_quality(
                native_text,
                min_characters=self.config.min_page_chars,
            )
            provenance: dict[str, object] = {
                "schema": "neocortex.ocr-page/v1",
                "profile": self.config.ocr_profile,
                "configured_languages": list(self._ocr_profile_plan.configured_languages),
                "requested_languages": list(self._ocr_profile_plan.required_languages),
                "effective_languages": [],
                "traineddata": [
                    {"language": language, "xxh3_128": digest}
                    for language, digest in (
                        self._ocr_runtime.traineddata_hashes
                        if self._ocr_runtime is not None
                        else ()
                    )
                ],
                "processing_signature": self.config.processing_signature,
                "native_text_quality": asdict(quality),
                "ocr_attempted": False,
                "ocr_selected": False,
            }
            source: Literal["native", "ocr", "error"] = "native"
            text = native_text
            if self._should_ocr_local_page(quality.usable, state):
                state.ocr_attempted += 1
                ocr_text = self._ocr_page(page, fitz)
                carried = getattr(ocr_text, "provenance", None)
                if isinstance(carried, dict):
                    provenance = dict(carried)
                    provenance["native_text_quality"] = asdict(quality)
                provenance["ocr_attempted"] = True
                if ocr_text.strip() or self.config.ocr_mode == "always":
                    text = ocr_text
                    source = "ocr"
                    provenance["ocr_selected"] = True
                else:
                    provenance["ocr_selected"] = False
                    provenance["ocr_not_selected_reason"] = "empty_ocr_result"
            elif self.config.ocr_mode == "never":
                provenance["ocr_skipped_reason"] = "ocr_disabled"
            elif quality.usable:
                provenance["ocr_skipped_reason"] = "native_text_usable"
            else:
                provenance["ocr_skipped_reason"] = "max_ocr_pages_reached"
            if len(text) > self.config.max_page_text_chars:
                raise RuntimeError(
                    f"page text has {len(text)} characters; limit={self.config.max_page_text_chars}"
                )

            def store(target: sqlite3.Connection) -> None:
                self._store_staging_page(
                    target,
                    state.file_key,
                    state.processing_signature,
                    page_number,
                    source,
                    text,
                    provenance,
                )

            if connection is None:
                self._owner_call(store)
            else:
                store(connection)
            return source
        except CancellationRequested:
            raise
        except Exception as page_exc:
            page_error_type = type(page_exc).__name__
            page_error_message = str(page_exc)[:2000]

            def store_failure(target: sqlite3.Connection) -> None:
                self._store_page_failure(
                    target,
                    snapshot,
                    page_number,
                    page_error_type,
                    page_error_message,
                )

            if connection is None:
                self._owner_call(store_failure)
            else:
                store_failure(connection)
            state.page_errors += 1
            if self.config.fail_fast_pages:
                raise
            return "error"

    def _should_ocr_local_page(
        self,
        native_text_usable: bool,
        state: _LocalExtractionState,
    ) -> bool:
        wants_ocr = self.config.ocr_mode == "always" or (
            self.config.ocr_mode == "auto" and not native_text_usable
        )
        within_limit = (
            self.config.max_ocr_pages is None or state.ocr_attempted < self.config.max_ocr_pages
        )
        return wants_ocr and within_limit

    def _record_local_page_progress(
        self,
        connection: sqlite3.Connection | None,
        state: _LocalExtractionState,
        source: Literal["native", "ocr", "error"],
    ) -> None:
        state.pages_since_commit += 1
        if state.pages_since_commit >= TEXT_BATCH_PAGES:
            if connection is not None:
                self._check_disk()
                connection.commit()
            state.pages_since_commit = 0
        if source == "ocr":
            state.ocr_pages += 1
        elif source != "error":
            state.native_pages += 1

    def _process_with_pdfminer(
        self, snapshot: FileSnapshot, binary_digest: str | None
    ) -> _DocumentResult:
        """Fallback extraction that streams one pdfminer page layout at a time."""

        self.cancellation.checkpoint()
        high_level = import_module("pdfminer.high_level")
        layout_module = import_module("pdfminer.layout")
        extract_pages = cast(
            Callable[[str | Path], Iterable[Iterable[object]]],
            high_level.extract_pages,
        )
        text_container_type = cast(type[_PdfMinerTextContainer], layout_module.LTTextContainer)

        key = _file_key(snapshot)
        signature = self.config.processing_signature
        page_count = processed = page_errors = 0
        start = 0 if self.config.page_start is None else self.config.page_start - 1
        end = self.config.page_end
        if self.config.max_pages is not None:
            end = min(end if end is not None else 2**63 - 1, start + self.config.max_pages)
        metadata = {"engine": "pdfminer", "fallback": True}

        def prepare(connection: sqlite3.Connection) -> None:
            self._prepare_document(connection, snapshot, 0, metadata)
            connection.execute("DELETE FROM page_staging WHERE file_key=?", (key,))

        self._owner_call(prepare)
        for page_number, layout in enumerate(extract_pages(snapshot.path)):
            self.cancellation.checkpoint()
            page_count = page_number + 1
            if end is not None and page_number >= end:
                break
            if page_number < start:
                continue
            try:
                chunks = (
                    element.get_text()
                    for element in layout
                    if isinstance(element, text_container_type)
                )
                text = "".join(chunks)
                if len(text) > self.config.max_page_text_chars:
                    raise RuntimeError(
                        f"page text has {len(text)} characters; "
                        f"limit={self.config.max_page_text_chars}"
                    )

                def store(
                    connection: sqlite3.Connection,
                    *,
                    stored_page_number: int = page_number,
                    stored_text: str = text,
                ) -> None:
                    self._store_staging_page(
                        connection,
                        key,
                        signature,
                        stored_page_number,
                        "pdfminer",
                        stored_text,
                    )

                self._owner_call(store)
            except CancellationRequested:
                raise
            except Exception as page_exc:
                page_error_type = type(page_exc).__name__
                page_error_message = str(page_exc)[:2000]

                def store_failure(
                    connection: sqlite3.Connection,
                    *,
                    stored_page_number: int = page_number,
                    stored_error_type: str = page_error_type,
                    stored_error_message: str = page_error_message,
                ) -> None:
                    self._store_page_failure(
                        connection,
                        snapshot,
                        stored_page_number,
                        stored_error_type,
                        stored_error_message,
                    )

                self._owner_call(store_failure)
                page_errors += 1
                if self.config.fail_fast_pages:
                    raise
            processed += 1
            if processed % TEXT_BATCH_PAGES == 0:
                self._check_disk()
        if processed == 0:
            raise RuntimeError("pdfminer produced no pages in the requested range")
        final_end = start + processed
        status: Literal["done", "partial"] = "partial" if page_errors else "done"

        def promote(connection: sqlite3.Connection) -> None:
            self._promote_document(
                connection,
                snapshot,
                page_count,
                processed,
                metadata,
                binary_digest,
                status=status,
                page_start=start + 1,
                page_end=final_end,
                is_partial=start > 0 or end is not None,
                page_errors=page_errors,
            )

        self._owner_call(promote)
        if status == "done":
            self._reconcile_review(
                snapshot,
                "pdfminer extraction completed without findings",
            )
        return _DocumentResult(
            status,
            native_pages=processed - page_errors,
            page_errors=page_errors,
        )

    def _ocr_page(self, page: Any, fitz: Any) -> str:
        runtime_hashes = (
            self._ocr_runtime.traineddata_hashes if self._ocr_runtime is not None else ()
        )
        extraction = IsolatedExtractionConfig(
            ocr_mode=self.config.ocr_mode,
            ocr_lang=self.config.ocr_lang,
            dpi=self.config.dpi,
            min_page_chars=self.config.min_page_chars,
            max_page_text_chars=self.config.max_page_text_chars,
            max_render_pixels=self.config.max_render_pixels,
            max_ocr_pages=self.config.max_ocr_pages,
            ocr_timeout_seconds=self.config.ocr_timeout_seconds,
            pdfminer_fallback=self.config.pdfminer_fallback,
            max_pages=self.config.max_pages,
            page_start=self.config.page_start,
            page_end=self.config.page_end,
            fail_fast_pages=self.config.fail_fast_pages,
            skip_before=0,
            only_pages=frozenset(),
            prior_ocr_pages=0,
            tesseract_cmd=self.config.tesseract_cmd,
            tessdata_dir=self.config.tessdata_dir,
            ocr_profile=self.config.ocr_profile,
            ocr_processing_signature=self.config.processing_signature,
            ocr_traineddata_hashes=runtime_hashes,
        )
        result = _ocr_page_result(page, fitz, extraction, self._ocr_slots)
        return _OcrTextWithProvenance(result.text, result.provenance)

    def _store_text_duplicate_batch(
        self,
        redundant_batch: list[sqlite3.Row],
        evidence: str,
    ) -> int:
        if not redundant_batch:
            return 0
        rows = tuple(redundant_batch)
        redundant_batch.clear()
        action_ids = self.framework_state.begin_file_actions(
            self.run_id,
            (
                (
                    "review_pdf_text_duplicate",
                    row["path"],
                    None,
                    "application/pdf",
                    f"{evidence};policy=advisory-only",
                    False,
                )
                for row in rows
            ),
        )
        self.framework_state.finish_file_actions(
            action_ids,
            "planned",
            "Advisory only: equal normalized text does not establish "
            "visual or document equivalence",
        )
        return len(rows)

    def _deduplicate_text(self) -> tuple[int, int, int, int]:
        """Persist review candidates without inferring visual equivalence.

        Equal normalized text is useful evidence, but it cannot establish that
        two PDFs are interchangeable: drawings, signatures, page geometry and
        other visual content may differ.  Consequently this phase never mutates
        the filesystem, including when the general ``--apply`` mode is active.
        """

        groups = candidates = trashed = skips = 0
        with serialized_pdf_write(), _database(self.config.state_path) as connection:
            keys = connection.execute(
                """SELECT normalized_text_xxh3_128,normalized_text_chars
                FROM documents WHERE status='done' AND is_partial=0 AND last_seen_run_id=?
                AND normalized_text_xxh3_128 IS NOT NULL
                GROUP BY normalized_text_xxh3_128,normalized_text_chars HAVING COUNT(*)>1""",
                (self.run_id,),
            )
            for key in keys:
                rows = connection.execute(
                    """SELECT file_key,path,size,mtime_ns FROM documents
                    WHERE status='done' AND is_partial=0 AND normalized_text_xxh3_128=?
                    AND normalized_text_chars=? AND last_seen_run_id=?
                    ORDER BY mtime_ns DESC,path DESC""",
                    (key[0], key[1], self.run_id),
                )
                keep = next((row for row in rows if Path(row["path"]).is_file()), None)
                if keep is None:
                    continue
                evidence = f"normalized-text-xxh3-128={key[0]};chars={key[1]};keep={keep['path']}"
                redundant_batch: list[sqlite3.Row] = []
                group_has_redundant = False

                for row in rows:
                    if not Path(row["path"]).is_file():
                        continue
                    redundant_batch.append(row)
                    if len(redundant_batch) >= TEXT_DUPLICATE_ACTION_BATCH_SIZE:
                        stored = self._store_text_duplicate_batch(
                            redundant_batch,
                            evidence,
                        )
                        candidates += stored
                        group_has_redundant = group_has_redundant or stored > 0
                stored = self._store_text_duplicate_batch(redundant_batch, evidence)
                candidates += stored
                group_has_redundant = group_has_redundant or stored > 0
                groups += int(group_has_redundant)
        return groups, candidates, trashed, skips

    def _active_page_progress(
        self,
        connection: sqlite3.Connection,
        snapshots: tuple[FileSnapshot, ...],
    ) -> str | int:
        """Return aggregate durable page progress for active PDF workers."""

        if not snapshots:
            return 0
        keys = tuple(dict.fromkeys(_file_key(snapshot) for snapshot in snapshots))
        placeholders = ",".join("?" for _key in keys)
        rows = connection.execute(
            f"""SELECT d.file_key,d.page_count,COUNT(s.page_number) AS staged_pages
            FROM documents d LEFT JOIN page_staging s
              ON s.file_key=d.file_key
             AND s.processing_signature=d.processing_signature
             AND s.source<>'error'
            WHERE d.file_key IN ({placeholders})
            GROUP BY d.file_key,d.page_count""",
            keys,
        ).fetchall()
        completed = total = 0
        for row in rows:
            page_count = int(row["page_count"] or 0)
            if page_count <= 0:
                continue
            start = max(0, (self.config.page_start or 1) - 1)
            end = min(page_count, self.config.page_end or page_count)
            if self.config.max_pages is not None:
                end = min(end, start + self.config.max_pages)
            selected_pages = max(0, end - start)
            total += selected_pages
            completed += min(selected_pages, int(row["staged_pages"] or 0))
        return f"{completed}/{total}" if total else 0

    def _report(
        self,
        completed: int,
        total: int,
        *,
        cache_hits: int,
        cached_errors: int,
        new_work: int,
        cache_refreshes: int,
        retries: int,
        retry_pages: int,
        completed_work: int,
        errors: int,
        timeouts: int,
        recycled: int,
        partial: int,
        protected: int,
        active_work: int,
        queued_work: int,
        memory_waits: int,
        page_progress: str | int = 0,
        finished: bool = False,
        description: str = "Procesando ruta PDF",
    ) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "pdf",
                "extract",
                description,
                completed,
                total,
                "PDF",
                finished,
                (
                    ProgressMetric("cache_hits", cache_hits),
                    ProgressMetric("new_work", new_work),
                    ProgressMetric("cache_refreshes", cache_refreshes),
                    ProgressMetric("retries", retries),
                    ProgressMetric("retry_pages", retry_pages),
                    ProgressMetric("errors", errors),
                    ProgressMetric("timeouts", timeouts),
                    ProgressMetric("recycled", recycled),
                    ProgressMetric("active_work", active_work),
                    ProgressMetric("queued_work", queued_work),
                    ProgressMetric("page_progress", page_progress),
                    ProgressMetric("remaining", max(0, total - completed)),
                    ProgressMetric("cached_errors", cached_errors),
                    ProgressMetric("completed_work", completed_work),
                    ProgressMetric("partial", partial),
                    ProgressMetric("protected", protected),
                    ProgressMetric("memory_waits", memory_waits),
                ),
            ),
        )


# endregion [02]
