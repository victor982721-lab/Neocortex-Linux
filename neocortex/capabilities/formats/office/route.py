"""Incremental Office route orchestration."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any as Any
from typing import Literal, cast

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress

from _04_Nucleo_Operativo.action_policy import same_snapshot
from _04_Nucleo_Operativo.cancellation import CancellationToken
from neocortex.foundation.file_identity import file_key_from_snapshot as _file_key
from _04_Nucleo_Operativo.memory_runtime import MemoryResourceLimits, WeightedMemoryGate
from _04_Nucleo_Operativo.review import ReviewCandidate
from _04_Nucleo_Operativo.route_filters import CandidateSelection as CandidateSelection
from _04_Nucleo_Operativo.state import FrameworkRouteState, ReviewCandidateReconciliation
from .extraction import extract_office_document as _extract_office_document
from .models import (
    MAX_XLSX_CELLS,
    OFFICE_COMMIT_BATCH,
    OFFICE_MIME_FORMATS,
    OFFICE_REVIEW_REASON_CODES,
    OFFICE_ROUTE_VERSION,
    ODT_MIME,
    PPTX_MIME,
    XLSX_MIME,
    ExtractedOfficeDocument,
    OfficeExtractionError,
    OfficeRouteConfig,
    OfficeRouteSummary,
    ReviewRecommendation,
    XlsxCell,
)
from .state import (
    _cached_document,
    _prune_stale_documents,
    _refresh_cached_path,
    _store_error,
    _store_inventory,
    _store_success,
    initialize_office_state,
    office_database,
    search_office_state,
)
from .xlsx import _extract_xlsx_shared_strings


def extract_office_document(
    path: Path,
    format_name: Literal["xlsx", "pptx", "odt"],
    *,
    max_text_chars: int,
    cancellation: CancellationToken,
) -> ExtractedOfficeDocument:
    """Preserve historical patch seams while delegating bounded extraction."""

    return _extract_office_document(
        path,
        format_name,
        max_text_chars=max_text_chars,
        cancellation=cancellation,
        max_xlsx_cells=MAX_XLSX_CELLS,
        shared_strings_extractor=_extract_xlsx_shared_strings,
    )


@dataclass(frozen=True, slots=True)
class _OfficeCandidateOutcome:
    extracted: int = 0
    errors: int = 0
    reviews: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0


@dataclass(slots=True)
class _OfficeRunMetrics:
    candidate_pool: int
    eligible: int
    selected: int
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    extracted: int = 0
    errors: int = 0
    reviews: int = 0
    deletion_candidates: int = 0
    retryable_errors: int = 0
    pruned: int = 0

    def apply(self, outcome: _OfficeCandidateOutcome) -> None:
        self.extracted += outcome.extracted
        self.errors += outcome.errors
        self.reviews += outcome.reviews
        self.deletion_candidates += outcome.deletion_candidates
        self.retryable_errors += outcome.retryable_errors


class OfficeRoute:
    def __init__(
        self,
        config: OfficeRouteConfig,
        framework_state: FrameworkRouteState,
        run_id: int,
        *,
        progress: ProgressCallback | None = None,
        memory_gate=None,
        cancellation: CancellationToken | None = None,
    ):
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

    def _validate(self) -> None:
        if self.config.max_text_chars < 1:
            raise ValueError("office max_text_chars must be positive")
        if self.config.max_documents is not None and self.config.max_documents < 1:
            raise ValueError("office max_documents must be positive")

    def _selected_counts(self) -> tuple[int, int, int]:
        totals = [
            self.framework_state.selected_route_candidate_counts(
                self.run_id,
                mime,
                self.config.max_file_bytes,
                "office",
                self.config.selection,
            )
            for mime in OFFICE_MIME_FORMATS
        ]
        candidate_pool = sum(item[0] for item in totals)
        eligible = sum(item[1] for item in totals)
        selected = (
            eligible
            if self.config.max_documents is None
            else min(eligible, self.config.max_documents)
        )
        return candidate_pool, eligible, selected

    @staticmethod
    def _queue_success(
        reconciliations: list[ReviewCandidateReconciliation],
        snapshot: FileSnapshot,
        note: str,
    ) -> None:
        reconciliations.append(
            ReviewCandidateReconciliation(
                snapshot=snapshot,
                resolution_note=note,
                evaluated_reason_codes=tuple(sorted(OFFICE_REVIEW_REASON_CODES)),
            )
        )

    def _flush_reviews(
        self,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        if not reconciliations:
            return
        self.framework_state.reconcile_review_candidates_batch(
            self.run_id,
            "office",
            tuple(reconciliations),
        )
        reconciliations.clear()

    def _consume_cached(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        format_name: str,
        cached: sqlite3.Row | None,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> tuple[bool, bool]:
        if cached is None:
            return False, False
        status = str(cached["status"])
        if status != "complete" and self.config.retry_errors:
            return False, False
        _refresh_cached_path(connection, snapshot, format_name, self.run_id)
        if status == "complete":
            self._queue_success(
                reconciliations,
                snapshot,
                "current Office cache completed without structural errors",
            )
            return True, False
        self.framework_state.store_review_candidates(
            self.run_id,
            (_review_candidate(snapshot, _cached_office_failure(cached)),),
        )
        return True, True

    def _extract_snapshot(
        self,
        snapshot: FileSnapshot,
        format_name: Literal["xlsx", "pptx", "odt"],
    ) -> ExtractedOfficeDocument:
        current = snapshot_path(snapshot.path)
        if not same_snapshot(snapshot, current):
            raise OfficeExtractionError(
                "office_source_changed",
                "office source changed after inventory",
                recommendation="retry",
                retryable=True,
            )
        with self.memory_gate.admit(
            _estimated_office_memory_bytes(snapshot, self.config.max_text_chars)
        ):
            document = extract_office_document(
                Path(snapshot.path),
                format_name,
                max_text_chars=self.config.max_text_chars,
                cancellation=self.cancellation,
            )
        final = snapshot_path(snapshot.path)
        if not same_snapshot(snapshot, final):
            raise OfficeExtractionError(
                "office_source_changed",
                "office source changed during extraction",
                recommendation="retry",
                retryable=True,
            )
        return document

    def _process_candidate(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        format_name: Literal["xlsx", "pptx", "odt"],
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> _OfficeCandidateOutcome:
        try:
            document = self._extract_snapshot(snapshot, format_name)
            _store_success(
                connection,
                snapshot,
                document,
                self.config.processing_signature,
                self.run_id,
            )
            self._queue_success(
                reconciliations,
                snapshot,
                "Office extraction completed without structural errors",
            )
            return _OfficeCandidateOutcome(extracted=1)
        except OfficeExtractionError as exc:
            failure = exc
        except (OSError, sqlite3.Error) as exc:
            failure = OfficeExtractionError(
                "office_io_error",
                f"{type(exc).__name__}: {exc}",
                recommendation="retry",
                retryable=True,
            )
        _store_error(
            connection,
            snapshot,
            format_name,
            self.config.processing_signature,
            self.run_id,
            failure,
        )
        self.framework_state.store_review_candidates(
            self.run_id,
            (_review_candidate(snapshot, failure),),
        )
        return _OfficeCandidateOutcome(
            errors=1,
            reviews=1,
            deletion_candidates=int(failure.recommendation == "deletion_candidate"),
            retryable_errors=int(failure.retryable),
        )

    def run(self) -> OfficeRouteSummary:
        self.cancellation.checkpoint()
        self._validate()
        initialize_office_state(self.config.state_path)
        metrics = _OfficeRunMetrics(*self._selected_counts())
        reconciliations: list[ReviewCandidateReconciliation] = []
        with office_database(self.config.state_path, create=False) as connection:
            self._run_candidates(connection, metrics, reconciliations)
            connection.commit()
            self._flush_reviews(reconciliations)
            if self._should_prune():
                metrics.pruned = _prune_stale_documents(connection, self.run_id)
                connection.commit()
        self._report(metrics, finished=True)
        return self._summary(metrics)

    def _run_candidates(
        self,
        connection: sqlite3.Connection,
        metrics: _OfficeRunMetrics,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        for mime, format_name in OFFICE_MIME_FORMATS.items():
            self._run_mime_candidates(
                connection,
                mime,
                format_name,
                metrics,
                reconciliations,
            )
            if metrics.processed >= metrics.selected:
                return

    def _run_mime_candidates(
        self,
        connection: sqlite3.Connection,
        mime: str,
        format_name: Literal["xlsx", "pptx", "odt"],
        metrics: _OfficeRunMetrics,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        iterator = self.framework_state.iter_selected_route_candidates(
            self.run_id,
            mime,
            "office",
            self.config.selection,
        )
        for snapshot in iterator:
            if metrics.processed >= metrics.selected:
                return
            self.cancellation.checkpoint()
            if self._exceeds_file_limit(snapshot):
                continue
            self._run_candidate(
                connection,
                snapshot,
                format_name,
                metrics,
                reconciliations,
            )

    def _run_candidate(
        self,
        connection: sqlite3.Connection,
        snapshot: FileSnapshot,
        format_name: Literal["xlsx", "pptx", "odt"],
        metrics: _OfficeRunMetrics,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        _store_inventory(connection, snapshot, format_name, self.run_id)
        cached = _cached_document(connection, snapshot, self.config.processing_signature)
        consumed, cached_error = self._consume_cached(
            connection,
            snapshot,
            format_name,
            cached,
            reconciliations,
        )
        if consumed:
            metrics.cache_hits += 1
            metrics.cached_errors += int(cached_error)
        else:
            metrics.apply(
                self._process_candidate(connection, snapshot, format_name, reconciliations)
            )
        metrics.processed += 1
        self._commit_batch(connection, metrics, reconciliations)

    def _exceeds_file_limit(self, snapshot: FileSnapshot) -> bool:
        limit = self.config.max_file_bytes
        return limit is not None and snapshot.size > limit

    def _commit_batch(
        self,
        connection: sqlite3.Connection,
        metrics: _OfficeRunMetrics,
        reconciliations: list[ReviewCandidateReconciliation],
    ) -> None:
        if metrics.processed % OFFICE_COMMIT_BATCH:
            return
        connection.commit()
        self._flush_reviews(reconciliations)
        self._report(metrics)

    def _should_prune(self) -> bool:
        return (
            not self.config.selection.active
            and self.config.max_documents is None
            and self.config.max_file_bytes is None
        )

    def _report(self, metrics: _OfficeRunMetrics, *, finished: bool = False) -> None:
        emit_progress(
            self.progress,
            ProgressEvent(
                "office",
                "extract",
                "Office indexados" if finished else "Indexando Office",
                metrics.processed,
                metrics.selected,
                "documentos",
                finished,
                (
                    ProgressMetric("cache_hits", metrics.cache_hits),
                    ProgressMetric("cached_errors", metrics.cached_errors),
                    ProgressMetric("errors", metrics.errors),
                    ProgressMetric("completed_work", metrics.extracted),
                    ProgressMetric("memory_waits", self.memory_gate.wait_count),
                ),
            ),
        )

    def _summary(self, metrics: _OfficeRunMetrics) -> OfficeRouteSummary:
        processing = self.config.processing_provenance
        return OfficeRouteSummary(
            candidate_pool=metrics.candidate_pool,
            candidates=metrics.selected,
            skipped_by_size=metrics.candidate_pool - metrics.eligible,
            skipped_by_count=metrics.eligible - metrics.selected,
            processed=metrics.processed,
            cache_hits=metrics.cache_hits,
            cached_errors=metrics.cached_errors,
            extracted=metrics.extracted,
            errors=metrics.errors,
            cache_documents_pruned=metrics.pruned,
            review_candidates=metrics.reviews,
            deletion_candidates=metrics.deletion_candidates,
            retryable_errors=metrics.retryable_errors,
            peak_reserved_bytes=self.memory_gate.peak_reserved_bytes,
            memory_waits=self.memory_gate.wait_count,
            processing_signature=processing.signature,
            processing_provenance=processing.manifest,
        )


def _estimated_office_memory_bytes(
    snapshot: FileSnapshot,
    max_text_chars: int,
) -> int:
    return (
        64 * 1024 * 1024
        + min(snapshot.size * 2, 128 * 1024 * 1024)
        + min(max_text_chars * 4, 128 * 1024 * 1024)
    )


def _cached_office_failure(row: sqlite3.Row) -> OfficeExtractionError:
    recommendation = str(row["review_disposition"])
    if recommendation not in {"retry", "manual_review", "deletion_candidate"}:
        recommendation = "manual_review"
    return OfficeExtractionError(
        str(row["error_type"] or "office_cached_error"),
        str(row["error_message"] or "cached Office error"),
        recommendation=cast(ReviewRecommendation, recommendation),
        retryable=bool(row["retryable"]),
    )


def _review_candidate(
    snapshot: FileSnapshot,
    error: OfficeExtractionError,
) -> ReviewCandidate:
    return ReviewCandidate(
        route_name="office",
        snapshot=snapshot,
        reason_code=error.code,
        source_status="error",
        recommendation=error.recommendation,
        retryable=error.retryable,
        confidence=0.98 if error.recommendation == "deletion_candidate" else 0.85,
        evidence={"message": str(error)[:512], "route_version": OFFICE_ROUTE_VERSION},
        detector_version=OFFICE_ROUTE_VERSION,
    )


__all__ = (
    "MAX_XLSX_CELLS",
    "ODT_MIME",
    "OFFICE_MIME_FORMATS",
    "OFFICE_REVIEW_REASON_CODES",
    "OFFICE_ROUTE_VERSION",
    "PPTX_MIME",
    "XLSX_MIME",
    "ExtractedOfficeDocument",
    "OfficeExtractionError",
    "OfficeRoute",
    "OfficeRouteConfig",
    "OfficeRouteSummary",
    "XlsxCell",
    "_file_key",
    "extract_office_document",
    "search_office_state",
)

for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.office_route"
del _defined_value
