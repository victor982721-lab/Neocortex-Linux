"""Initial inventory and content pipeline owned by Framework.

The pipeline keeps the ordering and persistence fences in the coordinator,
while this module groups preparation, inventory, curation, deduplication, and
route hand-off code into one cohesive slice.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from neocortex.deduplication import DedupIndex, DedupPlan, DedupPlanner, InventoryExclusionPolicy
from neocortex.deduplication.admission import size_is_admitted, validate_max_file_bytes
from neocortex.integrations.inventory.inventory_coordinator import (
    PreparedInventory,
    prepare_inventory,
)
from neocortex.progress import ProgressEvent, ProgressMetric, emit_progress
from neocortex.runtime.config.runtime_cache import XDG_CACHE_HOME_ENVIRONMENT
from neocortex.runtime.control.global_resources import GlobalResourceSummary, resource_gate
from neocortex.runtime.models import ActionSummary
from neocortex.runtime.orchestration.orchestrator_types import (
    _FrameworkOrchestratorOwner,
    InitialWork as _InitialWork,
    _complete_root_identity,
)
from neocortex.runtime.orchestration.route_selection import ORGANIZABLE_ROUTE_NAMES
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.workflow.actions.actions import FrameworkActions

if TYPE_CHECKING:
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.documents.document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )
    from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.orchestration.run_manifest import RunBudget


_ZIP_PROGRESS_OPERATION = "zip-intake"
_ZIP_PROGRESS_PHASE = "process"
_ZIP_PROGRESS_DESCRIPTION = "Procesando ZIPs"
_ZIP_PROGRESS_MAX_EVENTS = 128
_ZIP_PROGRESS_INTERVAL_SECONDS = 0.25


def _bounded_counter(value: object) -> int:
    """Return a safe non-negative counter for the live ZIP projection."""

    if isinstance(value, bool):
        return int(value)
    if type(value) is int and value >= 0:
        return value
    return 0


class _BoundedZipProgress:
    """Coalesce engine progress into one small, stable ZIP task.

    The intake engine may report member-level work.  Framework must not relay
    one terminal event per member to a human reporter, nor should it inspect
    the corpus a second time merely to discover the ZIP count.  This adapter
    therefore forwards only the engine's already-observed counters and caps
    the number of updates for one run.
    """

    _COUNTER_ALIASES = {
        "generic": ("generic", "generic_candidates", "generic_zip"),
        "atomic": ("atomic", "atomic_packages", "atomic_package"),
        "applied": ("applied", "applied_containers"),
        "blocked": ("blocked", "blocked_containers"),
        "members": ("members", "member_count"),
        "extracted": ("extracted", "extracted_files", "published_files", "published"),
    }

    def __init__(self, callback, *, total_hint: int) -> None:
        self._callback = callback
        self._total_hint = max(0, int(total_hint))
        self._completed = 0
        self._total: int | None = None
        self._counters = dict.fromkeys(self._COUNTER_ALIASES, 0)
        self._status = "running"
        self._started = False
        self._finished = False
        self._events = 0
        self._last_emitted_completed = -1
        self._last_emit_at = 0.0
        self._clock = time.monotonic

    @staticmethod
    def _metric_value(metrics: Mapping[str, object], names: tuple[str, ...]) -> int:
        for name in names:
            if name in metrics:
                return _bounded_counter(metrics[name])
        return 0

    def _merge_event(self, event: ProgressEvent) -> None:
        metrics = {metric.name: metric.value for metric in event.metrics}
        for name, aliases in self._COUNTER_ALIASES.items():
            # Engine counters are cumulative.  Max protects the projection
            # from a retry or a backend that reports a stale intermediate
            # snapshot without ever inventing progress.
            self._counters[name] = max(
                self._counters[name], self._metric_value(metrics, aliases)
            )
        status = metrics.get("status")
        if isinstance(status, str) and status:
            self._status = status[:64]
        if type(event.completed) is int and event.completed >= 0:
            self._completed = max(self._completed, event.completed)
        if type(event.total) is int and event.total >= 0:
            self._total = event.total

    def _event(self, *, finished: bool, total: int | None = None) -> ProgressEvent:
        effective_total = self._total if self._total is not None else total
        if effective_total is None:
            effective_total = self._total_hint or None
        completed = self._completed
        if effective_total is not None:
            completed = min(completed, effective_total)
        metrics = (
            *(ProgressMetric(name, value) for name, value in self._counters.items()),
            ProgressMetric("status", self._status),
        )
        return ProgressEvent(
            _ZIP_PROGRESS_OPERATION,
            _ZIP_PROGRESS_PHASE,
            _ZIP_PROGRESS_DESCRIPTION,
            completed,
            effective_total,
            "ZIPs",
            finished,
            metrics,
        )

    def _emit(self, *, finished: bool = False, total: int | None = None) -> None:
        if self._callback is None:
            return
        if not finished and self._events >= _ZIP_PROGRESS_MAX_EVENTS:
            return
        event = self._event(finished=finished, total=total)
        self._callback(event)
        self._events += 1
        self._started = True
        self._last_emitted_completed = event.completed
        self._last_emit_at = self._clock()

    def __call__(self, event: ProgressEvent) -> None:
        if not isinstance(event, ProgressEvent) or self._finished:
            return
        self._merge_event(event)
        now = self._clock()
        completed_delta = self._completed - self._last_emitted_completed
        interval = max(1, (self._total or self._total_hint or 1) // 100)
        should_emit = (
            not self._started
            or event.finished
            or completed_delta >= interval
            or now - self._last_emit_at >= _ZIP_PROGRESS_INTERVAL_SECONDS
        )
        if should_emit:
            self._emit(finished=event.finished)
            if event.finished:
                self._finished = True

    def finish(self, payload: dict[str, object], *, status: str | None = None) -> None:
        """Publish one terminal event from the engine's bounded result."""

        if self._finished:
            return
        candidates = _bounded_counter(payload.get("candidates"))
        self._total = candidates or self._total
        if status is None and isinstance(payload.get("status"), str):
            status = str(payload["status"])
        if status:
            self._status = status[:64]
        for name, aliases in self._COUNTER_ALIASES.items():
            values = [payload.get(alias) for alias in aliases]
            for value in values:
                if type(value) is int and value >= 0:
                    self._counters[name] = max(self._counters[name], value)
        statuses = payload.get("statuses")
        if isinstance(statuses, dict):
            self._counters["blocked"] = max(
                self._counters["blocked"], _bounded_counter(statuses.get("blocked"))
            )
        self._completed = max(self._completed, candidates)
        if candidates and not self._started:
            self._emit(total=candidates)
        if candidates or self._started:
            self._emit(finished=True, total=candidates or None)
        self._finished = True

    def partial(self, *, status: str) -> dict[str, object]:
        """Return only observed counters for cancellation/failure evidence."""

        self._status = status[:64]
        return {
            "status": self._status,
            "candidates": self._completed,
            **self._counters,
            "progress_events": self._events,
        }

    @property
    def event_count(self) -> int:
        return self._events


def collect_size_admission_metrics(
    snapshots: Iterable[object],
    max_file_bytes: int | None,
    *,
    total_files: int | None = None,
) -> dict[str, object]:
    """Aggregate global size admission from metadata-only snapshots.

    The canonical validation/decision lives in
    :mod:`neocortex.deduplication.admission`; this helper only shapes the
    bounded run/status counters consumed by the orchestration owner.
    """

    limit = validate_max_file_bytes(max_file_bytes)
    if total_files is not None and (type(total_files) is not int or total_files < 0):
        raise ValueError("total_files must be a non-negative integer or null")
    if limit is None and total_files is not None:
        return {
            "total_files": total_files,
            "eligible_files": total_files,
            "size_skipped_files": 0,
            "size_skipped_bytes": 0,
            "max_file_bytes": None,
        }
    total = eligible = skipped = skipped_bytes = 0
    for snapshot in snapshots:
        total += 1
        size = int(snapshot.size)  # type: ignore[attr-defined]
        if size_is_admitted(size, limit):
            eligible += 1
        else:
            skipped += 1
            skipped_bytes += size
    if total_files is not None and total != total_files:
        raise RuntimeError(
            f"size-admission inventory count mismatch: expected {total_files}, observed {total}"
        )
    return {
        "total_files": total,
        "eligible_files": eligible,
        "size_skipped_files": skipped,
        "size_skipped_bytes": skipped_bytes,
        "max_file_bytes": limit,
    }


class InitialPipelineMixin(_FrameworkOrchestratorOwner):
    """Preparation, inventory, curation, and initial route hand-off."""

    if TYPE_CHECKING:
        def _reserve_lifecycle_stage_work(
            self,
            state: FrameworkState,
            run_id: int,
            stage: str,
            reservation_id: str,
            *,
            items: int = 0,
            bytes_count: int = 0,
            worker: str | None = None,
        ) -> dict[str, object] | None: ...

        def _durable_run_budget(self) -> RunBudget: ...

        def _bind_resource_deadline(self, state: FrameworkState, run_id: int) -> None: ...

        def _run_content_routes(
            self,
            *,
            root: Path,
            state: FrameworkState,
            run_id: int,
            scan_id: int,
        ) -> tuple[dict[str, object], GlobalResourceSummary | None]: ...

    def _run_document_organization(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
    ) -> tuple["OrganizationPlanSummary | None", "OrganizationApplySummary | None"]:
        """Complete the durable planning obligation before later consumers."""

        if not self._organization_resume_pending and not (
            self.config.document_catalog_enabled
            and ORGANIZABLE_ROUTE_NAMES.intersection(self.selected_routes)
        ):
            return None, None
        from .organization_lifecycle import run_organization_stages

        return run_organization_stages(
            self.config,
            root=root,
            state=state,
            run_id=run_id,
            progress=self.progress,
            cancellation=self._cancellation,
            reserve=lambda stage, reservation, items: self._reserve_lifecycle_stage_work(
                state,
                run_id,
                stage,
                reservation,
                items=items,
                bytes_count=0,
                worker="organization",
            ),
        )

    def _prepare_run_contract(self, boundary: NormalInventoryBoundary) -> None:
        from neocortex.runtime.orchestration.preparation import prepare_framework_run

        self._run_preparation = prepare_framework_run(
            self.config,
            boundary,
            self.selected_routes,
            semantic_requested=self._lifecycle_stage_runner is not None,
            cancelled=lambda: self._cancellation.is_cancelled,
        )

    def _record_run_preparation(self, state: FrameworkState, run_id: int) -> None:
        report = getattr(self, "_run_preparation", None)
        if report is not None:
            state.record_event(
                run_id, "info", "preparation", "Preparación de la ejecución", report.to_dict()
            )

    def _prepare_initial_run(
        self,
        boundary: NormalInventoryBoundary,
    ) -> tuple[None, str | None]:
        boundary.verify()
        emit_progress(
            self.progress,
            ProgressEvent("framework", "prepare", "Preparando ejecución", 0, 1, "fase"),
        )
        # NeoCortex is a Linux/Kubuntu product.  Inventory is deliberately
        # portable and metadata-first; there is no optional NTFS/USN branch to
        # probe or to represent as a partially available acceleration path.
        journal_before = None
        journal_error = "portable_inventory_backend"
        boundary.verify()
        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "prepare",
                "Ejecución preparada",
                1,
                1,
                "fase",
                True,
            ),
        )
        return journal_before, journal_error

    def _initial_configuration_payload(
        self,
        boundary: NormalInventoryBoundary,
        excluded_paths: tuple[Path, ...],
    ) -> dict[str, object]:
        from neocortex.workflow.actions.redlist import (
            redlist_policy_digest,
            redlist_policy_payload,
        )
        max_file_bytes = validate_max_file_bytes(
            getattr(self.config, "max_file_bytes", None)
        )

        return {
            "route": self.config.route,
            "selected_routes": list(self.selected_routes),
            "route_capabilities": {
                name: self.route_registry[name].lifecycle_capability
                for name in self.selected_routes
            },
            "run_max_items": getattr(self.config, "run_max_items", None),
            "run_max_bytes": getattr(self.config, "run_max_bytes", None),
            # Persist None explicitly: it is the reproducible unlimited mode.
            "max_file_bytes": max_file_bytes,
            "size_admission": {
                "max_file_bytes": max_file_bytes,
                "unlimited": max_file_bytes is None,
            },
            # ZIP Intake is a Framework stage, not a content route.  Keep its
            # effective admission contract in the immutable run manifest so a
            # dry-run/apply result can be interpreted without consulting
            # process-local configuration.
            "zip_intake": {
                "enabled": self.config.route.casefold() == "all" and not self.config.route_only,
                "apply": bool(self.config.apply_actions),
                "max_file_bytes": max_file_bytes,
            },
            "run_time_budget_seconds": getattr(
                self.config,
                "run_time_budget_seconds",
                None,
            ),
            "global_memory_budget_bytes": self.config.global_memory_budget_bytes,
            "global_min_free_memory_bytes": self.config.global_min_free_memory_bytes,
            "global_min_free_commit_bytes": self.config.global_min_free_commit_bytes,
            "global_cpu_slots": self.config.global_cpu_slots,
            "global_max_cpu_load_percent": self.config.global_max_cpu_load_percent,
            "global_resource_wait_timeout_seconds": (
                self.config.global_resource_wait_timeout_seconds
            ),
            "dedup_policy": self.config.dedup_policy,
            "dedup_keep_paths": [
                os.path.abspath(path.expanduser()) for path in self.config.dedup_keep_paths
            ],
            "dedup_prefer_roots": [
                os.path.abspath(path.expanduser()) for path in self.config.dedup_prefer_roots
            ],
            "apply_actions": self.config.apply_actions,
            "corpus_redlist": redlist_policy_payload(),
            "corpus_redlist_digest": redlist_policy_digest(),
            "runtime_cache_home": os.environ.get(XDG_CACHE_HOME_ENVIRONMENT),
            "excluded_paths": [str(path) for path in excluded_paths],
            "inventory_exclusion_signature": boundary.exclusion_policy.signature,
            "internal_paths_signature": boundary.internal_paths_policy.signature,
            "inventory_policy_signature": boundary.effective_signature,
            "document_catalog_enabled": self.config.document_catalog_enabled,
            "document_taxonomy_path": (
                None
                if self.config.document_taxonomy_path is None
                else str(self.config.document_taxonomy_path)
            ),
            "document_classification_max_chars": (self.config.document_classification_max_chars),
            "organization_root": (
                None
                if self.config.organization_root is None
                else str(self.config.organization_root)
            ),
            "organization_min_confidence": self.config.organization_min_confidence,
            "image_workers": self.config.image_workers,
            "image_max_file_bytes": self.config.image_max_file_bytes,
            "image_max_documents": self.config.image_max_documents,
            "image_memory_budget_bytes": self.config.image_memory_budget_bytes,
            "image_worker_timeout_seconds": self.config.image_worker_timeout_seconds,
            "pdf_max_file_bytes": self.config.pdf_max_file_bytes,
            "pdf_max_documents": self.config.pdf_max_documents,
            "pdf_workers": self.config.pdf_workers,
            "pdf_ocr_workers": self.config.pdf_ocr_workers,
            "pdf_cache_validation": self.config.pdf_cache_validation,
            "pdf_document_timeout_seconds": self.config.pdf_document_timeout_seconds,
            "pdf_timeout_mode": self.config.pdf_timeout_mode,
            "pdf_max_document_timeout_seconds": (self.config.pdf_max_document_timeout_seconds),
            "pdf_memory_backpressure_bytes": self.config.pdf_memory_backpressure_bytes,
            "pdf_commit_backpressure_bytes": self.config.pdf_commit_backpressure_bytes,
            "pdf_memory_budget_bytes": self.config.pdf_memory_budget_bytes,
            "pdf_worker_memory_bytes": self.config.pdf_worker_memory_bytes,
            "docx_max_file_bytes": self.config.docx_max_file_bytes,
            "docx_max_documents": self.config.docx_max_documents,
            "docx_max_text_chars": self.config.docx_max_text_chars,
            "docx_memory_budget_bytes": self.config.docx_memory_budget_bytes,
            "docx_min_free_memory_bytes": self.config.docx_min_free_memory_bytes,
            "docx_min_free_commit_bytes": self.config.docx_min_free_commit_bytes,
            "office_max_file_bytes": self.config.office_max_file_bytes,
            "office_max_documents": self.config.office_max_documents,
            "office_max_text_chars": self.config.office_max_text_chars,
            "office_memory_budget_bytes": self.config.office_memory_budget_bytes,
            "office_min_free_memory_bytes": self.config.office_min_free_memory_bytes,
            "office_min_free_commit_bytes": self.config.office_min_free_commit_bytes,
            "audio_model_name": self.config.audio_model_name,
            "audio_device": self.config.audio_device,
            "audio_compute_type": self.config.audio_compute_type,
            "audio_language": self.config.audio_language,
            "audio_include_video": self.config.audio_include_video,
            "audio_max_file_bytes": self.config.audio_max_file_bytes,
            "audio_max_documents": self.config.audio_max_documents,
            "audio_max_duration_seconds": self.config.audio_max_duration_seconds,
            "audio_beam_size": self.config.audio_beam_size,
            "audio_vad_filter": self.config.audio_vad_filter,
            "audio_max_transcript_chars": self.config.audio_max_transcript_chars,
            "audio_max_segments": self.config.audio_max_segments,
            "audio_file_timeout_seconds": self.config.audio_file_timeout_seconds,
            "audio_worker_startup_timeout_seconds": (
                self.config.audio_worker_startup_timeout_seconds
            ),
            "audio_model_cache_directory": (
                None
                if self.config.audio_model_cache_directory is None
                else str(self.config.audio_model_cache_directory)
            ),
            "audio_local_models_only": self.config.audio_local_models_only,
            "audio_memory_wait_timeout_seconds": self.config.audio_memory_wait_timeout_seconds,
            "audio_memory_budget_bytes": self.config.audio_memory_budget_bytes,
            "audio_worker_memory_bytes": self.config.audio_worker_memory_bytes,
            "text_max_file_bytes": self.config.text_max_file_bytes,
            "text_max_documents": self.config.text_max_documents,
            "text_max_text_chars": self.config.text_max_text_chars,
            "text_worker_timeout_seconds": self.config.text_worker_timeout_seconds,
            "text_worker_memory_bytes": self.config.text_worker_memory_bytes,
            "text_retry_errors": self.config.text_retry_errors,
            "video_max_file_bytes": self.config.video_max_file_bytes,
            "video_max_documents": self.config.video_max_documents,
            "video_max_duration_seconds": self.config.video_max_duration_seconds,
            "video_max_frames": self.config.video_max_frames,
            "video_interval_seconds": self.config.video_interval_seconds,
            "video_scene_threshold": self.config.video_scene_threshold,
            "video_include_scenes": self.config.video_include_scenes,
            "video_include_keyframes": self.config.video_include_keyframes,
            "video_max_frame_pixels": self.config.video_max_frame_pixels,
            "video_max_frame_side": self.config.video_max_frame_side,
            "video_probe_timeout_seconds": self.config.video_probe_timeout_seconds,
            "video_discovery_timeout_seconds": self.config.video_discovery_timeout_seconds,
            "video_frame_timeout_seconds": self.config.video_frame_timeout_seconds,
            "video_file_timeout_seconds": self.config.video_file_timeout_seconds,
            "video_worker_memory_bytes": self.config.video_worker_memory_bytes,
            "video_retry_errors": self.config.video_retry_errors,
            "video_ocr_mode": self.config.video_ocr_mode,
            "video_ocr_lang": self.config.video_ocr_lang,
            "video_ocr_profile": self.config.video_ocr_profile,
            "video_ocr_timeout_seconds": self.config.video_ocr_timeout_seconds,
        }

    def _record_initial_start(
        self,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
    ) -> None:
        configuration = self._initial_configuration_payload(boundary, excluded_paths)
        self._record_run_preparation(state, run_id)
        state.record_event(
            run_id,
            "info",
            "run",
            "Ejecución iniciada",
            {
                "root": str(boundary.access_policy.root),
                "apply_actions": self.config.apply_actions,
                "journal_status": ("available" if journal_before is not None else "unavailable"),
                "journal_error": journal_error,
                "inventory_exclusion_signature": boundary.exclusion_policy.signature,
                "internal_paths_policy": boundary.internal_paths_policy.manifest(),
                "inventory_policy_signature": boundary.effective_signature,
            },
        )
        state.record_event(
            run_id,
            "info",
            "configuration",
            "Configuración efectiva",
            configuration,
        )
        budget_names = (
            "global_memory_budget_bytes",
            "global_min_free_memory_bytes",
            "global_min_free_commit_bytes",
            "global_cpu_slots",
            "global_resource_wait_timeout_seconds",
            "pdf_max_documents",
            "image_max_documents",
        )
        budget = {name: getattr(self.config, name, None) for name in budget_names}
        budget["durable"] = self._durable_run_budget().as_mapping()
        manifest = RunManifest(
            run_id=run_id,
            run_kind="initial",
            source_run_id=self.config.resume_run_id,
            root=str(boundary.access_policy.root),
            root_identity=_complete_root_identity(boundary.access_policy),
            selected_routes=tuple(self.selected_routes),
            route_capabilities={
                name: self.route_registry[name].lifecycle_capability
                for name in self.selected_routes
            },
            configuration=configuration,
            budget=budget,
            input_snapshot={
                "inventory_policy_signature": boundary.effective_signature,
                "inventory_exclusion_signature": boundary.exclusion_policy.signature,
                "journal_before": (
                    None
                    if journal_before is None
                    else {
                        "volume": journal_before.volume,
                        "journal_id": str(journal_before.journal_id),
                        "next_usn": journal_before.next_usn,
                    }
                ),
                "excluded_paths": [str(path) for path in excluded_paths],
            },
        )
        publish_manifest = getattr(state, "publish_run_manifest", None)
        if callable(publish_manifest):
            publish_manifest(run_id, manifest.event_payload())
            self._bind_resource_deadline(state, run_id)
            if self.config.document_catalog_enabled and ORGANIZABLE_ROUTE_NAMES.intersection(
                self.selected_routes
            ):
                from .organization_lifecycle import register_organization_stages

                register_organization_stages(
                    state, run_id, self.config, boundary.access_policy.root
                )
            if self._lifecycle_stage_runner is not None:
                # Publish the dependent stage before any worker starts.  If
                # the process dies in the hand-off to the stage runner, the
                # next status/resume still sees a durable pending stage rather
                # than mistaking the Framework row for a complete --all run.
                state.publish_run_stage(
                    run_id,
                    "semantic",
                    "pending",
                    details=self._lifecycle_stage_details,
                    idempotency_key="semantic:pending",
                )
            self._active_run = (self.config.framework_database, run_id)

    def _prepare_normal_inventory(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        dedup_index: DedupIndex,
        journal_before: None,
        phase: str = "inventory",
    ) -> PreparedInventory:
        state.set_run_phase(run_id, phase)
        read_budget = getattr(state, "read_run_budget", None)
        if callable(read_budget) and read_budget(run_id) is not None:
            state.check_run_budget(run_id)
        allow_incremental = False
        gate_reason = "portable_inventory_full_scan"
        source_run_id = None
        state.record_event(
            run_id,
            "info" if allow_incremental else "warning",
            "normal-incremental-gate",
            "Reutilización incremental normal evaluada",
            {
                "allowed": allow_incremental,
                "reason": gate_reason,
                "source_run_id": source_run_id,
                "inventory_exclusion_signature": boundary.exclusion_policy.signature,
                "inventory_policy_signature": boundary.effective_signature,
            },
        )
        from neocortex.deduplication.inventory.scanner import (
            InventoryWorkBudget,
            MAX_SCAN_BYTES,
            MAX_SCAN_FILES,
        )

        durable_budget = read_budget(run_id) if callable(read_budget) else None
        deadline = None
        maximum_files, maximum_bytes = MAX_SCAN_FILES, MAX_SCAN_BYTES
        if durable_budget is not None:
            for dimension, target in (("items", "files"), ("bytes", "bytes")):
                limit = durable_budget.get(f"max_{dimension}")
                if limit is None:
                    continue
                remaining = int(limit) - int(durable_budget[f"consumed_{dimension}"])
                if remaining <= 0:
                    raise RunBudgetExceeded(dimension, durable_budget)
                if target == "files":
                    maximum_files = min(maximum_files, remaining)
                else:
                    maximum_bytes = min(maximum_bytes, remaining)
            if durable_budget.get("deadline_ns") is not None:
                deadline = time.monotonic() + max(
                    0.0,
                    (int(durable_budget["deadline_ns"]) - time.time_ns()) / 1e9,
                )
        last_budget_check = time.monotonic()

        def inventory_checkpoint() -> None:
            nonlocal last_budget_check
            self._cancellation.checkpoint()
            now = time.monotonic()
            if durable_budget is not None and now - last_budget_check >= 0.1:
                state.check_run_budget(run_id)
                last_budget_check = now

        work_budget = InventoryWorkBudget(
            max_files=maximum_files,
            max_bytes=maximum_bytes,
            deadline_monotonic=deadline,
            cancellation_check=inventory_checkpoint,
        )
        inventory = prepare_inventory(
            dedup_index,
            state,
            run_id,
            boundary.access_policy.root,
            journal_before,
            progress=self.progress,
            exclusion_policy=boundary.exclusion_policy,
            allow_incremental=allow_incremental,
            publish_portable_checkpoint=True,
            work_budget=work_budget,
        )
        boundary.verify()
        if inventory.inventory_policy_signature != boundary.exclusion_policy.signature:
            raise RuntimeError("inventory result escaped its effective exclusion boundary")
        self._record_size_admission_metrics(
            state,
            run_id,
            dedup_index,
            inventory,
        )
        self._reserve_lifecycle_stage_work(
            state,
            run_id,
            "inventory",
            f"inventory:scan:{inventory.scan.scan_id}",
            items=int(inventory.scan.files_seen),
            bytes_count=int(inventory.scan.bytes_seen),
            worker="inventory",
        )
        return inventory

    def _record_size_admission_metrics(
        self,
        state: FrameworkState,
        run_id: int,
        dedup_index: DedupIndex,
        inventory: PreparedInventory,
    ) -> None:
        """Publish one metadata-only size decision after complete inventory."""

        max_file_bytes = validate_max_file_bytes(
            getattr(self.config, "max_file_bytes", None)
        )
        metrics = collect_size_admission_metrics(
            dedup_index.snapshots(inventory.scan.scan_id),
            max_file_bytes,
            total_files=int(inventory.scan.files_seen),
        )
        self._size_admission_metrics = metrics
        state.record_event(
            run_id,
            "info",
            "size-admission",
            "Política global de tamaño aplicada después del inventario",
            metrics,
        )

    def _emit_zip_reconciliation_progress(
        self,
        *,
        completed: int,
        total: int,
        source_files: int,
        successor_files: int | None = None,
        successor_scan_id: int | None = None,
        status: str = "running",
        finished: bool = False,
    ) -> None:
        """Expose the physical successor scan as a distinct visible phase."""

        metrics = [
            ProgressMetric("source_files", max(0, source_files)),
            ProgressMetric("status", status),
        ]
        if successor_files is not None:
            metrics.append(ProgressMetric("successor_files", max(0, successor_files)))
        if successor_scan_id is not None:
            metrics.append(ProgressMetric("successor_scan_id", max(0, successor_scan_id)))
        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "zip-intake-reconciliation",
                "Reconciliando inventario tras ZIP Intake",
                max(0, completed),
                max(0, total),
                "fase",
                finished,
                tuple(metrics),
            ),
        )

    def _zip_intake_effects_summary(self) -> dict[str, object]:
        """Return compact evidence of ZIP effects already past commit."""

        payload = getattr(self, "_last_zip_intake_result", {})
        if not isinstance(payload, dict):
            return {}

        def counter(*names: str) -> int:
            for name in names:
                value = payload.get(name)
                if type(value) is int and value >= 0:
                    return value
            return 0

        applied = counter("applied", "applied_containers")
        published = counter("published", "published_files", "extracted")
        trashed = counter("trashed", "trashed_sources")
        changed = bool(payload.get("filesystem_changed"))
        if not (applied or published or trashed or changed):
            return {}
        return {
            "status": str(payload.get("status", "unknown"))[:64],
            "applied_containers": applied,
            "published_files": published,
            "trashed_sources": trashed,
            "filesystem_changed": changed,
            "reconciliation_required": bool(payload.get("reconciliation_required")),
        }

    def _run_zip_intake_stage(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        root: Path,
        boundary: NormalInventoryBoundary,
        inventory: PreparedInventory,
        dedup_index: DedupIndex,
    ) -> tuple[PreparedInventory, dict[str, object]]:
        """Run physical ZIP Intake between Inventory and Identify.

        Inventory is the source of truth for admission.  The intake adapter
        receives only snapshots at or below the global ceiling; it therefore
        cannot open, classify, hash, or stage an oversize source.  When an
        apply transaction publishes files or trashes a source, one bounded
        successor inventory is prepared before Identify so all later stages
        see physical paths rather than the stale pre-intake generation.
        """

        # Reset per-run evidence before the first source is considered.  The
        # value is also used by terminal cancellation reporting when Identify
        # is interrupted after ZIP effects have already crossed their commit
        # frontier.
        self._last_zip_intake_result: dict[str, object] = {}

        empty_payload: dict[str, object] = {
            "schema": "neocortex.zip-intake/v1",
            "status": "skipped",
            "mode": "disabled",
            "apply": bool(self.config.apply_actions),
            "max_file_bytes": validate_max_file_bytes(
                getattr(self.config, "max_file_bytes", None)
            ),
            "total_files": int(inventory.scan.files_seen),
            "eligible_files": int(inventory.scan.files_seen),
            "size_skipped_files": 0,
            "size_skipped_bytes": 0,
            "filesystem_changed": False,
            "reconciliation_required": False,
        }
        if self.config.route.casefold() != "all" or self.config.route_only:
            return inventory, empty_payload

        from neocortex.workflow.zip_intake_orchestrator import (
            build_zip_intake_admission,
            run_zip_intake_stage,
        )

        max_file_bytes = validate_max_file_bytes(
            getattr(self.config, "max_file_bytes", None)
        )
        # ZIP Intake is a Framework stage, so its only public size ceiling is
        # the global admission already established by Inventory.
        effective_limit = max_file_bytes
        admission = build_zip_intake_admission(
            dedup_index.snapshots(inventory.scan.scan_id),
            effective_limit,
        )
        admission_payload = admission.payload()
        state.set_run_phase(run_id, "zip_intake")
        state.record_event(
            run_id,
            "info",
            "zip-intake",
            "Admisión de ZIP Intake preparada después del inventario",
            {
                **admission_payload,
                "global_max_file_bytes": max_file_bytes,
                "effective_max_file_bytes": effective_limit,
                "apply": bool(self.config.apply_actions),
            },
        )
        publish_stage = getattr(state, "publish_run_stage", None)
        if callable(publish_stage):
            publish_stage(
                run_id,
                "zip-intake",
                "running",
                details={
                    **admission_payload,
                    "global_max_file_bytes": max_file_bytes,
                    "effective_max_file_bytes": effective_limit,
                    "apply": bool(self.config.apply_actions),
                },
                idempotency_key="zip-intake:running",
            )
        zip_progress = _BoundedZipProgress(
            self.progress,
            total_hint=int(admission.eligible_files),
        )
        try:
            outcome = run_zip_intake_stage(
                root=root,
                admission=admission,
                config=self.config,
                apply=bool(self.config.apply_actions),
                state_directory=self.config.state_directory,
                run_id=run_id,
                state=state,
                # The adapter forwards this real callback to the intake
                # engine.  Coalescing here keeps member traversal bounded for
                # both Rich and line-oriented reporters without a second
                # content/header observation.
                progress=zip_progress,
                cancellation=self._cancellation,
            )
        except BaseException as exc:
            partial_status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
            self._last_zip_intake_result = zip_progress.partial(status=partial_status)
            if callable(publish_stage):
                try:
                    publish_stage(
                        run_id,
                        "zip-intake",
                        "failed",
                        details={
                            "error_type": type(exc).__name__,
                            "detail": str(exc)[:8192],
                            "observed_progress": dict(self._last_zip_intake_result),
                        },
                        idempotency_key="zip-intake:failed",
                    )
                except BaseException:
                    # Preserve the intake exception; Framework termination
                    # records the primary failure and recovery state.
                    pass
            raise
        # Atomic ZIP decisions are already validated by the intake owner.
        # Seed the existing content-type cache on this caller thread so the
        # subsequent Identify pass reuses the identity-bound decision instead
        # of reopening the package.  No ZIP or SQLite work crosses workers.
        if outcome.atomic_decisions:
            store_cache = getattr(state, "store_content_type_cache_batch", None)
            if callable(store_cache):
                from neocortex.platform.content_types import DETECTOR_VERSION

                store_cache(outcome.atomic_decisions, DETECTOR_VERSION, run_id)
        payload = dict(outcome.as_dict())
        payload.update(
            {
                "global_max_file_bytes": max_file_bytes,
                "effective_max_file_bytes": effective_limit,
            }
        )
        zip_progress.finish(payload, status=str(payload.get("status", "completed")))
        payload["progress_events"] = zip_progress.event_count
        self._last_zip_intake_result = payload
        # ZIP Intake may change children but must never replace the corpus
        # root itself.  Revalidate the physical boundary before any successor
        # inventory or downstream route can consume the result.
        boundary.verify()
        state.record_event(
            run_id,
            "warning" if outcome.status in {"partial", "failed", "blocked"} else "info",
            "zip-intake",
            "ZIP Intake aplicado" if self.config.apply_actions else "ZIP Intake planificado",
            payload,
        )
        successor = inventory
        if outcome.reconciliation_required:
            # Reuse the normal bounded inventory owner rather than creating a
            # ZIP-specific state table or attempting to patch rows manually.
            # This is intentionally one successor scan for the whole intake
            # batch, not one scan per source ZIP.
            state.record_event(
                run_id,
                "info",
                "zip-intake-reconciliation",
                "Preparando inventario sucesor después de ZIP Intake",
                {
                    "source_scan_id": inventory.scan.scan_id,
                    "reason": "physical_zip_intake_changes",
                },
            )
            self._emit_zip_reconciliation_progress(
                completed=0,
                total=1,
                source_files=int(inventory.scan.files_seen),
            )
            try:
                successor = self._prepare_normal_inventory(
                    state=state,
                    run_id=run_id,
                    boundary=boundary,
                    dedup_index=dedup_index,
                    journal_before=None,
                    phase="inventory_reconciliation",
                )
            except BaseException:
                self._emit_zip_reconciliation_progress(
                    completed=0,
                    total=1,
                    source_files=int(inventory.scan.files_seen),
                    status="cancelled",
                    finished=True,
                )
                raise
            successor = replace(
                successor,
                inventory_attempts=(
                    int(inventory.inventory_attempts) + int(successor.inventory_attempts)
                ),
                reconciliation_records=(
                    int(inventory.reconciliation_records)
                    + int(successor.reconciliation_records)
                ),
            )
            payload["successor_scan_id"] = successor.scan.scan_id
            payload["source_scan_id"] = inventory.scan.scan_id
            state.record_event(
                run_id,
                "info",
                "zip-intake-reconciliation",
                "Inventario sucesor listo para Identify",
                {
                    "source_scan_id": inventory.scan.scan_id,
                    "successor_scan_id": successor.scan.scan_id,
                    "files": successor.scan.files_seen,
                },
            )
            self._emit_zip_reconciliation_progress(
                completed=1,
                total=1,
                source_files=int(inventory.scan.files_seen),
                successor_files=int(successor.scan.files_seen),
                successor_scan_id=int(successor.scan.scan_id),
                status="completed",
                finished=True,
            )
        if callable(publish_stage):
            stage_status = (
                "partial"
                if outcome.status in {"partial", "failed", "blocked"}
                else "completed"
            )
            publish_stage(
                run_id,
                "zip-intake",
                stage_status,
                details=payload,
                idempotency_key="zip-intake:completed",
            )
        return successor, payload

    def _plan_initial_dedup(
        self,
        state: FrameworkState,
        run_id: int,
        dedup_index: DedupIndex,
        scan_id: int,
    ) -> DedupPlan:
        state.set_run_phase(run_id, "dedup_plan")
        started = time.perf_counter_ns()
        from .dedup_keeper import resolve_keeper_inputs

        selection = resolve_keeper_inputs(
            dedup_index,
            scan_id,
            keep_paths=self.config.dedup_keep_paths,
            preferred_roots=self.config.dedup_prefer_roots,
        )
        plan = DedupPlanner(
            dedup_index,
            keeper_policy=selection.policy,
            keeper_validation=selection.verify,
            resource_gate=resource_gate("dedup", self._active_coordinator),
            cancellation=self._cancellation,
            max_file_bytes=validate_max_file_bytes(
                getattr(self.config, "max_file_bytes", None)
            ),
        ).plan(
            scan_id,
            progress=self.progress,
            preview_limit=self.config.preview_group_limit,
            exact_compare=self.config.dedup_policy == "exact",
        )
        state.record_event(
            run_id,
            "info",
            "dedup-plan",
            "Plan de duplicados completado",
            {
                "elapsed_ns": time.perf_counter_ns() - started,
                "groups": plan.group_count,
                "reclaimable_bytes": plan.reclaimable_bytes,
                "keeper_explicit_identities": len(selection.policy.explicit_keep_identities),
                "keeper_preferred_roots": len(selection.policy.preferred_roots),
            },
        )
        self._reserve_lifecycle_stage_work(
            state,
            run_id,
            "dedup",
            f"dedup:plan:{scan_id}",
            items=int(plan.group_count),
            bytes_count=int(plan.reclaimable_bytes),
            worker="dedup",
        )
        return plan

    def _build_initial_action_runner(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        dedup_index: DedupIndex,
        scan_id: int,
        excluded_paths: tuple[Path, ...],
        inventory_policy: InventoryExclusionPolicy,
    ) -> FrameworkActions:

        read_budget = getattr(state, "read_run_budget", None)
        budgeted = callable(read_budget) and read_budget(run_id) is not None
        last_budget_check = time.monotonic()

        def action_checkpoint() -> None:
            nonlocal last_budget_check
            self._cancellation.checkpoint()
            now = time.monotonic()
            if budgeted and now - last_budget_check >= 0.1:
                state.check_run_budget(run_id)
                last_budget_check = now

        def reserve_action_work(key: str, items: int, bytes_count: int) -> None:
            action_checkpoint()
            self._reserve_lifecycle_stage_work(
                state,
                run_id,
                "actions",
                key,
                items=items,
                bytes_count=bytes_count,
                worker="corpus-curation",
            )

        trash_backend = None
        if self.config.apply_actions and os.name != "nt":
            # The CLI capability gate has already checked the active Linux
            # policy.  Keep construction lazy so read-only runs never resolve
            # KIO, create a bus, or touch Trash configuration.
            from neocortex.workflow.mutations import KioTrashBackend

            trash_backend = KioTrashBackend()
        return FrameworkActions(
            dedup_index,
            state,
            run_id,
            scan_id,
            apply=self.config.apply_actions,
            verify_bytes_before_trash=True,
            max_file_bytes=validate_max_file_bytes(
                getattr(self.config, "max_file_bytes", None)
            ),
            excluded_paths=excluded_paths,
            exclusion_policy=inventory_policy,
            progress=self.progress,
            trash_backend=trash_backend,
            cancellation_check=action_checkpoint,
            reserve_work=reserve_action_work if budgeted else None,
        )

    def _execute_initial_actions(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        dedup_index: DedupIndex,
        scan_id: int,
        plan: DedupPlan,
        excluded_paths: tuple[Path, ...],
        inventory_policy: InventoryExclusionPolicy,
        runner: FrameworkActions | None = None,
    ) -> tuple[FrameworkActions, ActionSummary]:
        runner = runner or self._build_initial_action_runner(
            state=state,
            run_id=run_id,
            dedup_index=dedup_index,
            scan_id=scan_id,
            excluded_paths=excluded_paths,
            inventory_policy=inventory_policy,
        )
        state.set_run_phase(run_id, "actions")
        actions = runner.execute(
            plan,
            cleanup_empty_directories=not self.selected_routes,
        )
        self._reserve_lifecycle_stage_work(
            state,
            run_id,
            "actions",
            f"actions:scan:{scan_id}",
            # Duplicate work is reserved before its plan is consumed. This
            # terminal reservation accounts only for content-type checks.
            items=int(actions.files_checked),
            bytes_count=0,
            worker="actions",
        )
        return runner, actions

    def _run_initial_routes(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        plan: DedupPlan,
        actions: ActionSummary,
        excluded_paths: tuple[Path, ...],
        inventory_policy: InventoryExclusionPolicy,
    ) -> tuple[
        ActionSummary,
        dict[str, object],
        ImageRouteSummary | None,
        GlobalResourceSummary | None,
        OrganizationPlanSummary | None,
        OrganizationApplySummary | None,
    ]:
        route_results, global_resources = self._run_content_routes(
            root=root,
            state=state,
            run_id=run_id,
            scan_id=scan_id,
        )
        image_summary = cast("ImageRouteSummary | None", route_results.get("image"))
        organization_plan, organization_apply = self._run_document_organization(
            root=root,
            state=state,
            run_id=run_id,
        )
        if self.selected_routes:
            # The inventory owner used by Identify/Dedupe must be closed
            # before PDF/Image route workers open their own connections. A
            # fresh short-lived owner is sufficient for the post-route empty
            # directory reconciliation and avoids a live WAL forcing a
            # multi-hundred-megabyte snapshot in sibling routes.
            with DedupIndex(self.config.dedup_database) as cleanup_index:
                cleanup_runner = self._build_initial_action_runner(
                    state=state,
                    run_id=run_id,
                    dedup_index=cleanup_index,
                    scan_id=scan_id,
                    excluded_paths=excluded_paths,
                    inventory_policy=inventory_policy,
                )
                actions = cleanup_runner.cleanup_empty_directories(plan, actions)
        return (
            actions,
            route_results,
            image_summary,
            global_resources,
            organization_plan,
            organization_apply,
        )

    def _execute_initial_work(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: None,
        excluded_paths: tuple[Path, ...],
    ) -> _InitialWork:
        with DedupIndex(self.config.dedup_database) as dedup_index:
            inventory = self._prepare_normal_inventory(
                state=state,
                run_id=run_id,
                boundary=boundary,
                dedup_index=dedup_index,
                journal_before=journal_before,
            )
            inventory, zip_intake_result = self._run_zip_intake_stage(
                state=state,
                run_id=run_id,
                root=boundary.access_policy.root,
                boundary=boundary,
                inventory=inventory,
                dedup_index=dedup_index,
            )
            # Identify/normalize is the first content-aware phase after the
            # metadata-only inventory.  The same action owner then evaluates
            # the explicit redlist against the normalized successor paths,
            # before duplicate planning can read a full fingerprint.
            action_runner = self._build_initial_action_runner(
                state=state,
                run_id=run_id,
                dedup_index=dedup_index,
                scan_id=inventory.scan.scan_id,
                excluded_paths=excluded_paths,
                inventory_policy=boundary.exclusion_policy,
            )
            state.set_run_phase(run_id, "identify")
            action_runner.identify_and_normalize()
            if self.config.apply_actions:
                successor_scan_id = dedup_index.current_scan_id(inventory.scan.scan_id)
                if successor_scan_id != inventory.scan.scan_id:
                    inventory = replace(
                        inventory,
                        scan=dedup_index.scan_summary(successor_scan_id),
                    )
            if self.config.route.casefold() == "all" and not self.config.route_only:
                from neocortex.workflow.actions.redlist import redlist_policy_digest

                state.set_run_phase(run_id, "redlist")
                action_runner.apply_redlist_prepass(
                    policy_digest=redlist_policy_digest(),
                )
                if self.config.apply_actions:
                    successor_scan_id = dedup_index.current_scan_id(inventory.scan.scan_id)
                    if successor_scan_id != inventory.scan.scan_id:
                        inventory = replace(
                            inventory,
                            scan=dedup_index.scan_summary(successor_scan_id),
                        )
            plan = self._plan_initial_dedup(
                state,
                run_id,
                dedup_index,
                inventory.scan.scan_id,
            )
            action_runner, actions = self._execute_initial_actions(
                state=state,
                run_id=run_id,
                dedup_index=dedup_index,
                scan_id=inventory.scan.scan_id,
                plan=plan,
                excluded_paths=excluded_paths,
                inventory_policy=boundary.exclusion_policy,
                runner=action_runner,
            )
            candidate_rows = state.route_candidate_run_count(run_id)
            state.publish_initial_routing_snapshot(
                run_id,
                inventory.scan.scan_id,
                inventory.reconciliation_records,
                inventory.inventory_attempts,
                inventory.inventory_mode,
                candidate_rows,
            )
        (
            actions,
            route_results,
            image_summary,
            global_resources,
            organization_plan,
            organization_apply,
        ) = self._run_initial_routes(
            root=boundary.access_policy.root,
            state=state,
            run_id=run_id,
            scan_id=inventory.scan.scan_id,
            plan=plan,
            actions=actions,
            excluded_paths=excluded_paths,
            inventory_policy=boundary.exclusion_policy,
        )
        # ZIP Intake is a Framework stage rather than a content route. Keep
        # the compact projection in route_results for status/replay consumers
        # and pass the same mapping through InitialWork below; no second
        # archive state is created.
        route_results["zip_intake"] = dict(zip_intake_result)
        return _InitialWork(
            inventory,
            plan,
            actions,
            route_results,
            image_summary,
            global_resources,
            organization_plan,
            organization_apply,
            dict(getattr(self, "_unavailable_routes", {})),
            size_admission=dict(getattr(self, "_size_admission_metrics", {})),
            zip_intake=dict(zip_intake_result),
        )

    @staticmethod
    def _initial_journal_after(inventory: PreparedInventory) -> None:
        """Portable Linux inventory has no journal successor cursor."""

        del inventory
        return None
