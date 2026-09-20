"""Initial-run finalization and terminal publication.

This slice owns the Framework run boundary after the inventory pipeline has
produced its hand-off record.  It keeps scratch maintenance, heartbeats, and
terminal state transitions together without owning route scheduling.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from neocortex.integrations.inventory.inventory_boundary import (
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from neocortex.progress import ProgressEvent, emit_progress
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.runtime.models import InitialRunResult, RouteOnlyRunResult
from neocortex.runtime.orchestration.orchestrator_types import (
    InitialExecution,
    InitialWork,
)
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.safety.corpus_access import CorpusAccessPolicy

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRouteSummary
    from neocortex.capabilities.formats.text.text_route import TextRouteSummary
    from neocortex.capabilities.formats.video.models import VideoRouteSummary
    from neocortex.integrations.inventory.inventory_boundary import NormalInventoryBoundary
    from neocortex.persistence.framework_state_writer import FrameworkState


_InitialWork = InitialWork
_InitialExecution = InitialExecution


class InitialFinalizationMixin:
    """Initial-run entry, recovery, and terminal publication."""

    def run(
        self,
    ) -> InitialRunResult | RouteOnlyRunResult:
        """Dispatch a full inventory run or an explicitly isolated route run."""

        if self.config.route_only or self.config.resume_run_id is not None:
            return self.run_route_only()
        return self.run_initial()

    def run_initial(self) -> InitialRunResult:
        """Run the pre-index stage, optionally applying explicitly enabled actions."""

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
            observe_regenerable_artifacts=bool(self.selected_routes),
        )
        boundary.verify()
        with FrameworkRunLock(self.config.state_directory / "framework.lock"):
            from neocortex.foundation.processing_provenance import (
                clear_processing_provenance_caches,
            )

            clear_processing_provenance_caches()
            self._prepare_run_contract(boundary)
            with self._run_resource_scope():
                return self._run_initial_locked(boundary)

    @classmethod
    def _scratch_plan_value(
        cls,
        plan: object,
        name: str,
        *aliases: str,
    ) -> object | None:
        """Read one bounded scratch-plan field without coupling its model.

        ``ScratchPlan`` is owned by ``neocortex.runtime.scratch``.  Keeping
        this adapter intentionally small lets Framework consume the public
        ``plan()/apply()`` contract while remaining compatible with a mapping
        or a frozen result object during a staged rollout.
        """

        for candidate in (name, *aliases):
            if isinstance(plan, Mapping) and candidate in plan:
                return plan[candidate]
            if hasattr(plan, candidate):
                return getattr(plan, candidate)
        return None

    @classmethod
    def _bounded_scratch_count(cls, value: object | None) -> int:
        """Normalize a plan count for one compact Framework event."""

        if isinstance(value, bool) or value is None:
            return 0
        if type(value) is int:
            return max(0, min(value, cls._SCRATCH_REPORT_COUNT_LIMIT))
        # Some early adapters expose buckets as tuples/lists instead of
        # counters.  Taking only their bounded length is safe and avoids
        # serializing arbitrary record payloads into the Framework owner.
        try:
            size = len(value)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            return 0
        if isinstance(size, bool) or type(size) is not int:
            return 0
        return max(0, min(size, cls._SCRATCH_REPORT_COUNT_LIMIT))

    @classmethod
    def _bounded_scratch_bytes(cls, value: object | None) -> int:
        """Normalize one byte counter without claiming physical disk gain."""

        if isinstance(value, bool) or value is None or type(value) is not int:
            return 0
        return max(0, min(value, cls._SCRATCH_REPORT_BYTES_LIMIT))

    @classmethod
    def _scratch_maintenance_details(
        cls,
        plan: object,
        *,
        apply_requested: bool,
        root: Path,
    ) -> dict[str, object]:
        """Build a bounded, JSON-safe result from the scratch owner."""

        counters = {
            name: cls._bounded_scratch_count(cls._scratch_plan_value(plan, name, f"{name}_records"))
            for name in (
                "planned",
                "applied",
                "kept",
                "blocked",
                "failed",
                "recovery_required",
            )
        }
        byte_counters = {
            f"{name}_bytes": cls._bounded_scratch_bytes(
                cls._scratch_plan_value(
                    plan,
                    f"{name}_bytes",
                    f"bytes_{name}",
                )
            )
            for name in (
                "planned",
                "applied",
                "kept",
                "blocked",
                "failed",
                "recovery_required",
            )
        }
        status_value = cls._scratch_plan_value(plan, "status", "state")
        status = str(status_value) if status_value is not None else ""
        if status.startswith("ScratchState."):
            status = status.rsplit(".", 1)[-1]
        status = status.casefold()
        if status not in {
            "planned",
            "applied",
            "kept",
            "blocked",
            "failed",
            "recovery_required",
            "completed",
            "partial",
        }:
            status = "applied" if apply_requested else "planned"
        root_blocked = cls._scratch_plan_value(plan, "root_blocked")
        unmanaged = cls._bounded_scratch_count(
            cls._scratch_plan_value(plan, "unmanaged", "unmanaged_records")
        )
        if root_blocked and root_blocked != "scratch root is absent":
            status = "blocked"
        if counters["recovery_required"]:
            status = "recovery_required"
        elif counters["failed"]:
            status = "failed"
        elif counters["blocked"]:
            status = "blocked"
        elif apply_requested and status == "planned":
            status = "applied"
        return {
            "schema": "neocortex.scratch-maintenance/v1",
            "scope": cls._SCRATCH_SCOPE,
            "owner": cls._SCRATCH_OWNER,
            "root": str(root),
            "mode": "apply" if apply_requested else "plan",
            "status": status,
            "root_blocked": (None if root_blocked is None else str(root_blocked)[:8192]),
            "unmanaged": unmanaged,
            **counters,
            **byte_counters,
        }

    @classmethod
    def _record_scratch_maintenance(
        cls,
        state: FrameworkState,
        run_id: int,
        details: Mapping[str, object],
    ) -> None:
        """Publish optional scratch evidence through existing Framework APIs."""

        status = str(details.get("status", "failed"))
        attention = status in {"blocked", "failed", "recovery_required", "unavailable", "partial"}
        publish_stage = getattr(state, "publish_run_stage", None)
        if callable(publish_stage):
            try:
                publish_stage(
                    run_id,
                    "scratch-owned-temp",
                    "partial" if attention else "completed",
                    details=dict(details),
                    idempotency_key="scratch:owned-temp",
                )
            except Exception:
                # Scratch is an optional category.  A legacy state adapter or
                # a conflicting historical marker must not hide the completed
                # Framework run; the event below is attempted independently.
                pass
        record_event = getattr(state, "record_event", None)
        if not callable(record_event):
            return
        try:
            record_event(
                run_id,
                "warning" if attention else "info",
                "maintenance",
                "Mantenimiento de scratch owned-temp evaluado",
                dict(details),
            )
        except Exception:
            # Event publication is best-effort for compatibility doubles.  It
            # is deliberately not a second ledger or a finalization gate.
            pass

    def _run_initial_scratch_maintenance(
        self,
        state: FrameworkState,
        run_id: int,
        *,
        primary_work_status: str = "complete",
    ) -> dict[str, object]:
        """Plan/apply only registered Framework-owned scratch workspaces.

        The manager is imported lazily so ordinary route execution does not
        load maintenance code.  ``create_root=False`` is important: a normal
        ``--all`` query must not create ``state/scratch/owned-temp`` merely to
        discover that no registered workspace exists.  The manager itself is
        the sole owner of manifest validation and of any eligible deletion.
        """

        scratch_root = Path(self.config.state_directory) / "scratch" / self._SCRATCH_SCOPE
        apply_requested = bool(getattr(self.config, "apply_actions", False))
        try:
            from neocortex.runtime.orchestration.maintenance import (
                MaintenanceBlocked,
                MaintenanceRequest,
                ScopeAuthority,
                configured_scratch_maintenance,
            )

            read_budget = getattr(state, "read_run_budget", None)
            live_budget = read_budget(run_id) if callable(read_budget) else None
            if live_budget is not None and not isinstance(live_budget, Mapping):
                raise MaintenanceBlocked("run_budget_invalid")
            limits = {"max_entries": 100_000, "max_bytes": 1 << 40}
            for ceiling, remaining_key, config_key in (
                ("max_entries", "remaining_items", "run_max_items"),
                ("max_bytes", "remaining_bytes", "run_max_bytes"),
            ):
                configured = getattr(self.config, config_key, None)
                remaining = None if live_budget is None else live_budget.get(remaining_key)
                for value in (configured, remaining):
                    if value is None:
                        continue
                    if type(value) is not int or value <= 0:
                        raise MaintenanceBlocked("run_budget_exhausted_or_invalid:" + remaining_key)
                    limits[ceiling] = min(limits[ceiling], value)
            configured_limits = any(
                getattr(self.config, key, None) is not None
                for key in (
                    "run_max_items",
                    "run_max_bytes",
                    "run_time_budget_seconds",
                )
            )
            if live_budget is None and configured_limits:
                raise MaintenanceBlocked("configured_run_budget_not_observable")
            deadline_ns = None
            if live_budget is not None:
                if live_budget.get("cancel_requested") or live_budget.get("expired"):
                    raise MaintenanceBlocked("run_budget_cancelled_or_expired")
                deadline = live_budget.get("deadline_ns")
                if deadline is not None:
                    if type(deadline) is not int or deadline <= time.time_ns():
                        raise MaintenanceBlocked("run_budget_deadline_expired")
                    deadline_ns = time.monotonic_ns() + (deadline - time.time_ns())

            reserved_entries = 0
            reserved_bytes = 0

            def record_outcome(payload: Mapping[str, object]) -> None:
                nonlocal reserved_entries, reserved_bytes
                if live_budget is not None and payload.get("phase") == "prepared":
                    entries, byte_count = payload.get("budget_entries"), payload.get("budget_bytes")
                    if (
                        type(entries) is not int
                        or type(byte_count) is not int
                        or entries < reserved_entries
                        or byte_count < reserved_bytes
                    ):
                        raise MaintenanceBlocked("maintenance_verification_budget_unavailable")
                    # Verification spends the same run budget as planning.
                    # Admission of its delta occurs before the owner's effect.
                    state.reserve_run_budget(
                        run_id,
                        "maintenance-verification:"
                        + self._SCRATCH_SCOPE
                        + ":"
                        + str(payload["fingerprint"]),
                        items=entries - reserved_entries,
                        bytes=byte_count - reserved_bytes,
                        worker="maintenance",
                        stage="maintenance",
                    )
                    reserved_entries, reserved_bytes = entries, byte_count
                # Unlike the optional display event below, this durable receipt
                # is required before/after the owner's effect.
                state.record_event(
                    run_id, "info", "maintenance-receipt", "Recibo de mantenimiento", dict(payload)
                )

            coordinator = configured_scratch_maintenance(
                Path(self.config.state_directory),
                owner=self._SCRATCH_OWNER,
                scopes=(self._SCRATCH_SCOPE,),
                record_outcome=record_outcome,
            )
            request = MaintenanceRequest(
                scopes=(self._SCRATCH_SCOPE,),
                apply_requested=apply_requested,
                authorities=(ScopeAuthority(self._SCRATCH_SCOPE, "configured-run-apply"),)
                if apply_requested
                else (),
                max_entries=limits["max_entries"],
                max_bytes=limits["max_bytes"],
                deadline_ns=deadline_ns,
                cancelled=lambda: self._cancellation.is_cancelled,
            )
            planned = coordinator.plan(request)
            if live_budget is not None:
                reserve = getattr(state, "reserve_run_budget", None)
                if not callable(reserve):
                    raise MaintenanceBlocked("run_budget_reservation_owner_unavailable")
                fingerprint = planned.component_fingerprints.get(self._SCRATCH_SCOPE, "empty")
                reserve(
                    run_id,
                    "maintenance:" + self._SCRATCH_SCOPE + ":" + fingerprint,
                    items=planned.observed_entries,
                    bytes=planned.observed_bytes,
                    worker="maintenance",
                    stage="maintenance",
                )
                reserved_entries, reserved_bytes = planned.observed_entries, planned.observed_bytes
            outcome = coordinator.execute(
                planned,
                primary_work_status=primary_work_status,
            )
            owner_plan = planned.component_plans.get(self._SCRATCH_SCOPE, {})
            details = self._scratch_maintenance_details(
                owner_plan,
                apply_requested=False,
                root=scratch_root,
            )
            details["mode"] = "apply" if apply_requested else "plan"
            if apply_requested:
                owner_outcome = outcome.get("scopes", {}).get(self._SCRATCH_SCOPE, {})
                details.update(
                    applied=owner_outcome.get("retired", 0),
                    applied_bytes=owner_outcome.get("deleted_apparent_bytes", 0),
                    status="applied" if outcome["maintenance_status"] == "complete" else "blocked",
                )
            elif planned.blocked:
                details["status"] = "blocked"
            details["maintenance"] = outcome
        except Exception as exc:
            details = {
                "schema": "neocortex.scratch-maintenance/v1",
                "scope": self._SCRATCH_SCOPE,
                "owner": self._SCRATCH_OWNER,
                "root": str(scratch_root),
                "mode": "apply" if apply_requested else "plan",
                # Keep the public status vocabulary aligned with the scratch
                # maintenance contract.  The diagnostic distinguishes an
                # absent/incompatible optional owner through error_type.
                "status": "failed",
                "root_blocked": None,
                "unmanaged": 0,
                "planned": 0,
                "applied": 0,
                "kept": 0,
                "blocked": 0,
                "failed": 0,
                "recovery_required": 0,
                "planned_bytes": 0,
                "applied_bytes": 0,
                "kept_bytes": 0,
                "blocked_bytes": 0,
                "failed_bytes": 0,
                "recovery_required_bytes": 0,
                "error_type": type(exc).__name__,
                "error": str(exc)[:8192],
                "maintenance": {
                    "operation_status": "partial",
                    "primary_work_status": primary_work_status,
                    "maintenance_status": "partial",
                    "requested_scopes": [self._SCRATCH_SCOPE],
                    "blocked_scopes": {
                        self._SCRATCH_SCOPE: type(exc).__name__ + ":" + str(exc)[:512]
                    },
                    "receipt_refs": [],
                },
            }
        self._record_scratch_maintenance(state, run_id, details)
        return details

    def _finalize_initial_run(
        self,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        work: _InitialWork,
        journal_after: None,
    ) -> None:
        boundary.verify()
        state.set_run_phase(run_id, "finalize")
        transient_rows_pruned = state.prune_route_candidates((run_id,))
        inventory = work.inventory
        scratch_maintenance = self._run_initial_scratch_maintenance(
            state,
            run_id,
            primary_work_status="partial" if work.route_failures else "complete",
        )
        outcome = scratch_maintenance.get("maintenance")
        if isinstance(outcome, dict):
            work.maintenance.update(outcome)
        state.complete_initial_run(
            run_id,
            inventory.scan.scan_id,
            journal_after,
            inventory.reconciliation_records,
            inventory.inventory_attempts,
            inventory.inventory_mode,
        )
        state.record_event(
            run_id,
            "warning" if work.route_failures else "info",
            "run",
            "Ejecución incompleta: capacidades no disponibles"
            if work.route_failures
            else "Ejecución completada",
            {
                "inventory_mode": inventory.inventory_mode,
                "scan_id": inventory.scan.scan_id,
                "transient_route_rows_pruned": transient_rows_pruned,
                "route_failures": work.route_failures,
                "scratch_maintenance": scratch_maintenance,
            },
        )

    def _execute_initial_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
        finalize: bool = True,
    ) -> _InitialExecution:
        self._record_initial_start(
            state,
            run_id,
            boundary,
            journal_before,
            journal_error,
            excluded_paths,
        )
        work = self._execute_initial_work(
            state=state,
            run_id=run_id,
            boundary=boundary,
            journal_before=journal_before,
            excluded_paths=excluded_paths,
        )
        journal_after = self._initial_journal_after(work.inventory)
        if finalize:
            self._finalize_initial_run(
                state,
                run_id,
                boundary,
                work,
                journal_after,
            )
        return _InitialExecution(work, journal_after)

    @staticmethod
    def _persist_initial_termination(
        state: FrameworkState,
        run_id: int,
        exc: BaseException,
        *,
        cancelled: bool,
    ) -> None:
        keep_runs = set(state.resumable_route_candidate_run_ids())
        keep_runs.add(run_id)
        state.prune_route_candidates(tuple(sorted(keep_runs)))
        state.record_event(
            run_id,
            "warning" if cancelled else "error",
            "run",
            "Ejecución cancelada por el usuario" if cancelled else "Ejecución fallida",
            None if cancelled else {"error_type": type(exc).__name__, "detail": str(exc)},
        )
        if cancelled:
            request_cancel = getattr(state, "request_run_cancellation", None)
            if callable(request_cancel):
                request_cancel(
                    run_id,
                    "budget" if isinstance(exc, RunBudgetExceeded) else "user",
                )
        transitioned = (
            state.cancel_initial_run(run_id) if cancelled else state.fail_initial_run(run_id)
        )
        abandoned_actions = state.mark_abandoned_actions()
        state.record_event(
            run_id,
            "info" if transitioned else "warning",
            "lifecycle",
            "Transición durable de terminación registrada",
            {
                "status": "cancelled" if cancelled else "failed",
                "transitioned": transitioned,
                "abandoned_actions": abandoned_actions,
            },
        )

    def _manage_initial_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
        finalize: bool = True,
    ) -> _InitialExecution:
        heartbeat = self._start_run_heartbeat(run_id)
        try:
            return self._execute_initial_run(
                state=state,
                run_id=run_id,
                boundary=boundary,
                journal_before=journal_before,
                journal_error=journal_error,
                excluded_paths=excluded_paths,
                finalize=finalize,
            )
        except KeyboardInterrupt as exc:
            self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except RunBudgetExceeded as exc:
            self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            self._persist_initial_termination(state, run_id, exc, cancelled=False)
            raise
        finally:
            heartbeat.stop()
            self._active_run = None

    def _run_initial_lifecycle_stage(
        self,
        *,
        run_id: int,
        boundary: NormalInventoryBoundary,
        work: _InitialWork,
        journal_after: None,
    ) -> None:
        """Run the dependent lifecycle stage before finalizing Framework.

        The callback executes outside the Framework writer connection so an
        owner-local Semantic stage can use its own bounded
        transactions.  The run remains ``running`` and has a pending stage
        marker until the callback returns; interruption therefore remains
        recoverable instead of being hidden behind a completed Framework row.
        """

        runner = self._lifecycle_stage_runner
        if runner is None:
            with self._framework_state() as state:
                self._finalize_initial_run(
                    state,
                    run_id,
                    boundary,
                    work,
                    journal_after,
                )
            return
        stage_heartbeat = self._start_run_heartbeat(run_id)
        try:
            runner(run_id)
        except KeyboardInterrupt as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except RunBudgetExceeded as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=False)
            raise
        finally:
            stage_heartbeat.stop()
        try:
            with self._framework_state() as state:
                self._finalize_initial_run(
                    state,
                    run_id,
                    boundary,
                    work,
                    journal_after,
                )
        except KeyboardInterrupt as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except RunBudgetExceeded as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            with self._framework_state() as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=False)
            raise

    @staticmethod
    def _initial_result(
        run_id: int,
        execution: _InitialExecution,
    ) -> InitialRunResult:
        work = execution.work
        inventory = work.inventory
        routes = work.route_results
        return InitialRunResult(
            run_id=run_id,
            scan=inventory.scan,
            dedup_plan=work.dedup_plan,
            journal_before=inventory.journal_before,
            journal_after=execution.journal_after,
            reconciliation_records=inventory.reconciliation_records,
            inventory_attempts=inventory.inventory_attempts,
            inventory_mode=inventory.inventory_mode,
            actions=work.actions,
            pdf=cast("PdfRouteSummary | None", routes.get("pdf")),
            docx=cast("DocxRouteSummary | None", routes.get("docx")),
            office=cast("OfficeRouteSummary | None", routes.get("office")),
            archive=cast("ArchiveRouteSummary | None", routes.get("archive")),
            text=cast("TextRouteSummary | None", routes.get("text")),
            audio=cast("AudioRouteSummary | None", routes.get("audio")),
            video=cast("VideoRouteSummary | None", routes.get("video")),
            image=work.image,
            route_results=routes,
            global_resources=work.global_resources,
            organization_plan=work.organization_plan,
            organization_apply=work.organization_apply,
            route_failures=work.route_failures,
            maintenance=dict(work.maintenance),
        )

    def _require_publication_ready(self) -> None:
        """Check cross-owner recovery before inventory or route owner writes."""

        from neocortex.persistence.state_publication import (
            StatePublicationRecoveryRequired,
            read_state_publication_state,
        )

        try:
            self.config.state_directory.lstat()
        except FileNotFoundError:
            return
        view = read_state_publication_state(self.config.state_directory)
        if view.status not in {"absent", "complete"}:
            raise StatePublicationRecoveryRequired(view.reason or view.status)

    def _run_initial_locked(
        self,
        boundary: NormalInventoryBoundary,
    ) -> InitialRunResult:
        self._require_publication_ready()
        excluded_paths = tuple(Path(path) for path in boundary.exclusion_policy.explicit_roots)
        journal_before, journal_error = self._prepare_initial_run(boundary)
        with self._framework_state() as state:
            state.mark_abandoned_runs()
            state.mark_abandoned_actions()
            run_id = state.begin_initial_run(
                boundary.access_policy.root,
                journal_before,
                inventory_policy_signature=boundary.effective_signature,
            )
            execution = self._manage_initial_run(
                state=state,
                run_id=run_id,
                boundary=boundary,
                journal_before=journal_before,
                journal_error=journal_error,
                excluded_paths=excluded_paths,
                finalize=self._lifecycle_stage_runner is None,
            )
        if self._lifecycle_stage_runner is not None:
            self._run_initial_lifecycle_stage(
                run_id=run_id,
                boundary=boundary,
                work=execution.work,
                journal_after=execution.journal_after,
            )
        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "complete",
                "Etapa previa completada",
                1,
                1,
                "fase",
                True,
            ),
        )
        return self._initial_result(run_id, execution)
