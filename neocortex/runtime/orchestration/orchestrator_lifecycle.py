"""Route-only lifecycle and durable replay preparation.

The route continuation path validates a prior inventory, creates a bounded
operational run, and owns its recovery transitions.  Actual route execution is
provided by :mod:`orchestrator_routes`.
"""

from __future__ import annotations

# mypy: disable-error-code=attr-defined

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from neocortex.deduplication import DedupIndex
from neocortex.integrations.inventory.inventory_boundary import (
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from neocortex.runtime.config.runtime_cache import XDG_CACHE_HOME_ENVIRONMENT
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.runtime.models import RouteOnlyRunResult
from neocortex.runtime.orchestration.orchestrator_types import (
    RouteOnlyExecution,
    RouteOnlySource,
    _complete_root_identity,
)
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.safety.corpus_access import CorpusAccessPolicy

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRouteSummary
    from neocortex.capabilities.formats.text.text_route import TextRouteSummary
    from neocortex.capabilities.formats.video.models import VideoRouteSummary
    from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.orchestration.run_lifecycle import RunHeartbeat


_RouteOnlySource = RouteOnlySource
_RouteOnlyExecution = RouteOnlyExecution


class RouteLifecycleMixin:
    """Route-only continuation and recovery lifecycle."""

    def run_route_only(self) -> RouteOnlyRunResult:
        """Run content routes over durable inputs without common maintenance."""

        self._cancellation = CancellationToken()
        root = self._validated_root()
        access_policy = CorpusAccessPolicy.capture("normal", root)
        state_layout = initialize_authorized_state_directory(
            access_policy,
            self.config.state_directory,
            require_disjoint=False,
        )
        state_directory = state_layout.path
        self.config = replace(
            self.config,
            root=root,
            state_directory=state_directory,
        )
        boundary = build_normal_inventory_boundary(
            root,
            state_directory,
            access_policy=access_policy,
            state_policy=state_layout.state_policy,
            internal_paths_policy=state_layout.internal_paths_policy,
            # Resume resolves its routes from the durable manifest later.
            # An initially empty selection is not an inventory-only run and
            # must keep the source content run's observational boundary.
            observe_regenerable_artifacts=(
                bool(self.selected_routes) or self.config.resume_run_id is not None
            ),
        )
        boundary.verify()
        with FrameworkRunLock(self.config.state_directory / "framework.lock"):
            from neocortex.foundation.processing_provenance import (
                clear_processing_provenance_caches,
            )

            clear_processing_provenance_caches()
            self._prepare_run_contract(boundary)
            with self._run_resource_scope():
                return self._run_route_only_locked(boundary)

    @staticmethod
    def _normalized_root(path: Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _root_identity(path: Path) -> tuple[int, int, int]:
        current = os.stat(path, follow_symlinks=False)
        return (
            int(current.st_dev),
            int(current.st_ino),
            stat_birthtime_ns(current),
        )

    def _reusable_source_scan_id(
        self,
        state: FrameworkState,
        source_run_id: int,
        boundary: NormalInventoryBoundary,
        expected_scan_id: int | None = None,
    ) -> int:
        boundary.verify()
        requested_root = boundary.access_policy.root
        source_root, scan_id = state.source_run_inventory(source_run_id)
        normalized_source = self._normalized_root(source_root)
        if normalized_source != self._normalized_root(requested_root):
            raise ValueError(f"source run {source_run_id} belongs to another corpus root")
        if state.source_inventory_policy_signature(source_run_id) != boundary.effective_signature:
            raise ValueError(f"source run {source_run_id} has an incompatible inventory policy")
        persisted_policy = state.corpus_mutation_guard(source_run_id).policy
        expected_identity = (
            boundary.access_policy.root_device_id,
            boundary.access_policy.root_file_id,
            boundary.access_policy.root_birthtime_ns,
        )
        persisted_identity = (
            persisted_policy.root_device_id,
            persisted_policy.root_file_id,
            persisted_policy.root_birthtime_ns,
        )
        if persisted_policy.mode != "normal" or persisted_identity != expected_identity:
            raise ValueError(f"source run {source_run_id} belongs to a replaced corpus root")
        if scan_id is None:
            evidence = state.recorded_inventory_evidence(source_run_id)
            target_scan_id = evidence.scan_id
        else:
            evidence = None
            target_scan_id = scan_id
        if expected_scan_id is not None and target_scan_id != expected_scan_id:
            raise ValueError(
                f"latest durable inventory run {source_run_id} changed its scan binding"
            )
        with DedupIndex(self.config.dedup_database) as dedup_index:
            checkpoint = dedup_index.inventory_checkpoint(requested_root)
            if (
                checkpoint is None
                or not checkpoint.valid
                or checkpoint.scan_id != target_scan_id
                or checkpoint.inventory_policy_signature != boundary.exclusion_policy.signature
            ):
                raise ValueError(f"source run {source_run_id} has no compatible durable checkpoint")
            dedup_index.require_scan_inventory_policy_signature(
                target_scan_id,
                boundary.exclusion_policy.signature,
            )
            summary = dedup_index.scan_summary(target_scan_id)
            persisted_files = dedup_index.file_count(target_scan_id)
            persisted_root_identity = dedup_index.scan_root_identity(target_scan_id)
        if self._normalized_root(Path(summary.root)) != normalized_source:
            raise ValueError(f"scan {target_scan_id} belongs to another corpus root")
        if persisted_root_identity != self._root_identity(requested_root):
            raise ValueError(f"scan {target_scan_id} belongs to a replaced corpus root")
        if persisted_files != summary.files_seen:
            raise ValueError(f"scan {target_scan_id} has inconsistent durable file counts")
        candidate_rows: int | None = None
        if evidence is not None:
            if summary.files_seen != evidence.files:
                raise ValueError(f"scan {target_scan_id} does not match its durable event evidence")
            candidate_rows = state.route_candidate_run_count(source_run_id)
        elif not state.has_durable_routing_snapshot(source_run_id):
            raise ValueError(f"source run {source_run_id} has no published routing snapshot")
        boundary.verify()
        state.mark_abandoned_runs()
        state.mark_abandoned_actions()
        if evidence is not None:
            assert candidate_rows is not None
            state.recover_initial_routing_snapshot(
                source_run_id,
                evidence,
                candidate_rows,
            )
        boundary.verify()
        return target_scan_id

    def _run_route_only_locked(
        self,
        boundary: NormalInventoryBoundary,
    ) -> RouteOnlyRunResult:
        self._require_publication_ready()
        boundary.verify()
        with self._framework_state() as state:
            source = self._prepare_route_only_source(state, boundary)
            run_id, heartbeat = self._begin_route_only_execution(
                state,
                boundary,
                source,
            )
            execution = self._execute_route_only_run(
                state=state,
                boundary=boundary,
                source=source,
                run_id=run_id,
                heartbeat=heartbeat,
                finalize=self._lifecycle_stage_runner is None,
            )
        if self._lifecycle_stage_runner is not None:
            self._run_route_only_lifecycle_stage(
                run_id=run_id,
                boundary=boundary,
                source=source,
            )
        return self._route_only_result(execution)

    def _route_only_source_run(
        self,
        state: FrameworkState,
        boundary: NormalInventoryBoundary,
    ) -> tuple[int, int | None]:
        if self.config.resume_run_id is not None:
            return self.config.resume_run_id, None
        if self.config.candidate_run_id is not None:
            return self.config.candidate_run_id, None
        latest_inventory = state.latest_durable_inventory_run(
            boundary.access_policy.root,
            corpus_access_mode="normal",
            inventory_policy_signature=boundary.effective_signature,
        )
        if latest_inventory is None:
            raise ValueError(
                "no compatible durable inventory snapshot is available; run normal inventory first"
            )
        return latest_inventory

    def _select_route_only_routes(
        self,
        state: FrameworkState,
        source_run_id: int,
    ) -> None:
        semantic_only_resume = False
        self._organization_resume_pending = False
        if self.config.resume_run_id is not None:
            from .organization_lifecycle import organization_pending

            self._organization_resume_pending = organization_pending(state, source_run_id)
            resumable = state.resumable_route_names(source_run_id)
            unknown = tuple(name for name in resumable if name not in self.route_registry)
            if unknown:
                raise ValueError(
                    "resume source references unavailable routes: " + ", ".join(unknown)
                )
            # An explicit route list is a request to narrow recovery, not a
            # request to rerun a route whose source run already completed.  The
            # resulting empty selection is meaningful when only the linked
            # Semantic lifecycle stage remains resumable.
            requested_routes = self.selected_routes
            resumable_set = set(resumable)
            if requested_routes:
                self.selected_routes = tuple(
                    name for name in requested_routes if name in resumable_set
                )
            else:
                self.selected_routes = resumable
            if not self.selected_routes:
                semantic_only_resume = self._semantic_stage_is_resumable(
                    state,
                    source_run_id,
                )
        if (
            not self.selected_routes
            and not semantic_only_resume
            and not self._organization_resume_pending
        ):
            raise ValueError(f"run {source_run_id} has no resumable content routes")
        read_capabilities = getattr(state, "read_run_route_capabilities", None)
        if callable(read_capabilities) and self.config.resume_run_id is not None:
            raw_capabilities = read_capabilities(source_run_id)
            capabilities = (
                {str(name): str(value) for name, value in raw_capabilities.items()}
                if isinstance(raw_capabilities, Mapping)
                else {}
            )
            unsupported = tuple(
                name
                for name in self.selected_routes
                if capabilities.get(name, "safe_replay") == "not_resumable"
            )
            if unsupported:
                raise ValueError(
                    "resume source declares non-replayable routes: "
                    + ", ".join(sorted(unsupported))
                )

    @staticmethod
    def _semantic_stage_is_resumable(
        state: FrameworkState,
        source_run_id: int,
    ) -> bool:
        """Return whether recovery may advance only the linked Semantic stage."""

        stages = state.read_run_stages(source_run_id)
        for stage in reversed(stages):
            if not isinstance(stage, Mapping) or stage.get("stage") != "semantic":
                continue
            status = stage.get("status")
            return status in {"pending", "running", "partial", "failed", "interrupted"}
        return False

    def _prepare_route_only_source(
        self,
        state: FrameworkState,
        boundary: NormalInventoryBoundary,
    ) -> _RouteOnlySource:
        source_run_id, expected_scan_id = self._route_only_source_run(state, boundary)
        self._select_route_only_routes(state, source_run_id)
        route_input_sources = {
            name: self.route_registry[name].input_source for name in self.selected_routes
        }
        candidate_backed_routes = tuple(
            name
            for name, input_source in route_input_sources.items()
            if input_source != "inventory_snapshot"
        )
        candidate_rows = state.route_candidate_run_count(source_run_id)
        if not self.selected_routes:
            # Stage-only recovery consumes no Framework route input and
            # therefore must not require a retained candidate snapshot or a
            # fresh inventory validation.  Still bind it to the same corpus
            # root before creating the operational lifecycle row.
            source_root, _source_scan_id = state.source_run_inventory(source_run_id)
            if self._normalized_root(source_root) != self._normalized_root(
                boundary.access_policy.root
            ):
                raise ValueError(f"source run {source_run_id} belongs to another corpus root")
            return _RouteOnlySource(
                source_run_id,
                0,
                route_input_sources,
                candidate_backed_routes,
                candidate_rows,
            )
        if candidate_rows == 0 and candidate_backed_routes:
            raise ValueError(
                f"run {source_run_id} has no retained routing candidates "
                "required by routes: " + ", ".join(candidate_backed_routes)
            )
        scan_id = self._reusable_source_scan_id(
            state,
            source_run_id,
            boundary,
            expected_scan_id,
        )
        return _RouteOnlySource(
            source_run_id,
            scan_id,
            route_input_sources,
            candidate_backed_routes,
            candidate_rows,
        )

    def _route_only_start_payload(
        self,
        boundary: NormalInventoryBoundary,
        source: _RouteOnlySource,
        copied_candidates: int,
    ) -> dict[str, object]:
        return {
            "root": str(boundary.access_policy.root),
            "source_run_id": source.run_id,
            "inventory_exclusion_signature": boundary.exclusion_policy.signature,
            "inventory_policy_signature": boundary.effective_signature,
            "candidate_rows": copied_candidates,
            "source_candidate_rows": source.candidate_rows,
            "route_input_sources": source.route_input_sources,
            "image_memory_budget_bytes": self.config.image_memory_budget_bytes,
            "docx_memory_budget_bytes": self.config.docx_memory_budget_bytes,
            "office_memory_budget_bytes": self.config.office_memory_budget_bytes,
            "audio_memory_budget_bytes": self.config.audio_memory_budget_bytes,
            "pdf_memory_budget_bytes": self.config.pdf_memory_budget_bytes,
            "selected_routes": list(self.selected_routes),
            "resume": self.config.resume_run_id is not None,
            "runtime_cache_home": os.environ.get(XDG_CACHE_HOME_ENVIRONMENT),
            "selection_active": self.config.selection.active,
            "document_catalog_enabled": self.config.document_catalog_enabled,
            "document_taxonomy_path": (
                None
                if self.config.document_taxonomy_path is None
                else str(self.config.document_taxonomy_path)
            ),
            "selection": {
                "statuses": list(self.config.selection.statuses),
                "error_types": list(self.config.selection.error_types),
                "recommendations": list(self.config.selection.recommendations),
                "paths": list(self.config.selection.paths),
                "failed_pages_only": self.config.selection.failed_pages_only,
            },
            "pdf_timeout_mode": self.config.pdf_timeout_mode,
            "pdf_document_timeout_seconds": self.config.pdf_document_timeout_seconds,
            "pdf_max_document_timeout_seconds": (self.config.pdf_max_document_timeout_seconds),
        }

    def _begin_route_only_execution(
        self,
        state: FrameworkState,
        boundary: NormalInventoryBoundary,
        source: _RouteOnlySource,
    ) -> tuple[int, RunHeartbeat]:
        run_kind = "resume" if self.config.resume_run_id is not None else "route_only"
        # Semantic belongs to the same run budget even when every physical
        # route was already completed. A no-route continuation cannot renew
        # an expired deadline or replenish the source run's item allowance.
        durable_budget, source_budget = self._route_only_budget(state, source.run_id)
        boundary.verify()
        run_id = state.begin_operational_run(
            boundary.access_policy.root,
            run_kind=run_kind,
            source_run_id=source.run_id,
        )
        heartbeat: RunHeartbeat | None = None

        def abort_start(exc: BaseException) -> None:
            if heartbeat is not None:
                heartbeat.stop()
            try:
                state.abort_run_start(
                    run_id,
                    exc,
                    cancelled=isinstance(exc, KeyboardInterrupt),
                )
            except BaseException as cleanup_error:
                exc.add_note(f"failed to terminalize startup row: {cleanup_error}")

        try:
            copied = (
                state.copy_route_candidates(source.run_id, run_id)
                if source.candidate_backed_routes
                else 0
            )
            self._record_run_preparation(state, run_id)
            heartbeat = self._start_run_heartbeat(run_id)
            state.record_event(
                run_id,
                "info",
                "run",
                "Ejecución aislada de rutas iniciada",
                self._route_only_start_payload(boundary, source, copied),
            )
        except BaseException as exc:
            abort_start(exc)
            raise
        route_payload = self._route_only_start_payload(boundary, source, copied)
        input_snapshot: dict[str, object] = {
            "source_candidate_rows": source.candidate_rows,
            "copied_candidates": copied,
            "route_input_sources": source.route_input_sources,
        }
        if source_budget is not None:
            input_snapshot["source_budget"] = {
                "manifest_digest": source_budget.get("manifest_digest"),
                "remaining_items": source_budget.get("remaining_items"),
                "remaining_bytes": source_budget.get("remaining_bytes"),
                "deadline_ns": source_budget.get("deadline_ns"),
            }
        try:
            publish_manifest = getattr(state, "publish_run_manifest", None)
            if callable(publish_manifest):
                publish_manifest(
                    run_id,
                    RunManifest(
                        run_id=run_id,
                        run_kind=run_kind,
                        source_run_id=source.run_id,
                        root=str(boundary.access_policy.root),
                        root_identity=_complete_root_identity(boundary.access_policy),
                        selected_routes=tuple(self.selected_routes),
                        route_capabilities={
                            name: self.route_registry[name].lifecycle_capability
                            for name in self.selected_routes
                        },
                        configuration=route_payload,
                        budget={
                            "durable": durable_budget.as_mapping(),
                            "global_memory_budget_bytes": self.config.global_memory_budget_bytes,
                            "global_cpu_slots": self.config.global_cpu_slots,
                            "global_resource_wait_timeout_seconds": (
                                self.config.global_resource_wait_timeout_seconds
                            ),
                        },
                        input_snapshot=input_snapshot,
                    ).event_payload(),
                )
                self._bind_resource_deadline(state, run_id)
                if self._lifecycle_stage_runner is not None:
                    state.publish_run_stage(
                        run_id,
                        "semantic",
                        "pending",
                        details=self._lifecycle_stage_details,
                        idempotency_key="semantic:pending",
                    )
                if self._organization_resume_pending:
                    from .organization_lifecycle import copy_organization_stages

                    copy_organization_stages(state, source.run_id, run_id)
                self._active_run = (self.config.framework_database, run_id)
        except BaseException as exc:
            abort_start(exc)
            raise
        assert heartbeat is not None
        return run_id, heartbeat

    @staticmethod
    def _prune_route_only_candidates(
        state: FrameworkState,
        source: _RouteOnlySource,
        run_id: int,
    ) -> None:
        if source.candidate_backed_routes:
            state.prune_route_candidates((run_id,))

    def _complete_route_only_run(
        self,
        state: FrameworkState,
        boundary: NormalInventoryBoundary,
        source: _RouteOnlySource,
        run_id: int,
    ) -> None:
        boundary.verify()
        state.set_run_phase(run_id, "finalize")
        self._prune_route_only_candidates(state, source, run_id)
        state.complete_operational_run(run_id)
        state.record_event(
            run_id,
            "info",
            "run",
            "Ejecución aislada de rutas completada",
            {"source_run_id": source.run_id},
        )

    def _cancel_route_only_run(
        self,
        state: FrameworkState,
        source: _RouteOnlySource,
        run_id: int,
    ) -> None:
        # Keep the immutable route inputs available for a later recovery.  A
        # cancelled run has not reached a terminal publication frontier.
        request_cancel = getattr(state, "request_run_cancellation", None)
        if callable(request_cancel):
            request_cancel(run_id, "user")
        state.cancel_initial_run(run_id)

    def _fail_route_only_run(
        self,
        state: FrameworkState,
        source: _RouteOnlySource,
        run_id: int,
        exc: BaseException,
    ) -> None:
        # Failed route work remains replayable until recovery has either
        # consumed it successfully or classified it as non-replayable.
        if isinstance(exc, RunBudgetExceeded):
            request_cancel = getattr(state, "request_run_cancellation", None)
            if callable(request_cancel):
                request_cancel(run_id, "budget")
        state.record_event(
            run_id,
            "error",
            "run",
            "Ejecución aislada de rutas fallida",
            {"error_type": type(exc).__name__, "detail": str(exc)},
        )
        state.fail_initial_run(run_id)

    def _execute_route_only_run(
        self,
        *,
        state: FrameworkState,
        boundary: NormalInventoryBoundary,
        source: _RouteOnlySource,
        run_id: int,
        heartbeat: RunHeartbeat,
        finalize: bool = True,
    ) -> _RouteOnlyExecution:
        try:
            route_results, global_resources = self._run_content_routes(
                root=boundary.access_policy.root,
                state=state,
                run_id=run_id,
                scan_id=source.scan_id,
            )
            if self._organization_resume_pending:
                self._run_document_organization(
                    root=boundary.access_policy.root, state=state, run_id=run_id
                )
            if finalize:
                self._complete_route_only_run(state, boundary, source, run_id)
        except KeyboardInterrupt:
            self._cancel_route_only_run(state, source, run_id)
            raise
        except RunBudgetExceeded:
            request_cancel = getattr(state, "request_run_cancellation", None)
            if callable(request_cancel):
                request_cancel(run_id, "budget")
            state.cancel_initial_run(run_id)
            raise
        except BaseException as exc:
            self._fail_route_only_run(state, source, run_id, exc)
            raise
        finally:
            heartbeat.stop()
            self._active_run = None
        return _RouteOnlyExecution(
            run_id,
            source.run_id,
            route_results,
            global_resources,
            dict(self._unavailable_routes),
        )

    def _run_route_only_lifecycle_stage(
        self,
        *,
        run_id: int,
        boundary: NormalInventoryBoundary,
        source: _RouteOnlySource,
    ) -> None:
        """Run a dependent owner stage before completing a route continuation."""

        runner = self._lifecycle_stage_runner
        if runner is None:
            with self._framework_state() as state:
                self._complete_route_only_run(state, boundary, source, run_id)
            return
        stage_heartbeat = self._start_run_heartbeat(run_id)
        try:
            runner(run_id)
        except KeyboardInterrupt as exc:
            with self._framework_state() as state:
                self._cancel_route_only_run(state, source, run_id)
                state.record_event(
                    run_id,
                    "warning",
                    "lifecycle-stage",
                    "Etapa dependiente interrumpida",
                    {"error_type": type(exc).__name__, "detail": str(exc)[:8192]},
                )
            raise
        except RunBudgetExceeded as exc:
            with self._framework_state() as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        except BaseException as exc:
            with self._framework_state() as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        finally:
            stage_heartbeat.stop()
        try:
            with self._framework_state() as state:
                self._complete_route_only_run(state, boundary, source, run_id)
        except KeyboardInterrupt as exc:
            with self._framework_state() as state:
                self._cancel_route_only_run(state, source, run_id)
                state.record_event(
                    run_id,
                    "warning",
                    "lifecycle-stage",
                    "Finalización interrumpida",
                    {"error_type": type(exc).__name__, "detail": str(exc)[:8192]},
                )
            raise
        except RunBudgetExceeded as exc:
            with self._framework_state() as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        except BaseException as exc:
            with self._framework_state() as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise

    @staticmethod
    def _route_only_result(execution: _RouteOnlyExecution) -> RouteOnlyRunResult:
        routes = execution.route_results
        return RouteOnlyRunResult(
            run_id=execution.run_id,
            source_run_id=execution.source_run_id,
            pdf=cast("PdfRouteSummary | None", routes.get("pdf")),
            docx=cast("DocxRouteSummary | None", routes.get("docx")),
            office=cast("OfficeRouteSummary | None", routes.get("office")),
            archive=cast("ArchiveRouteSummary | None", routes.get("archive")),
            text=cast("TextRouteSummary | None", routes.get("text")),
            audio=cast("AudioRouteSummary | None", routes.get("audio")),
            video=cast("VideoRouteSummary | None", routes.get("video")),
            image=cast("ImageRouteSummary | None", routes.get("image")),
            route_results=routes,
            global_resources=execution.global_resources,
            route_failures=execution.route_failures,
        )
