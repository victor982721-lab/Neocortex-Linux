"""Coordination of filesystem checkpoints and pre-index deduplication."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/orchestrator.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from neocortex.platform_policy import stat_birthtime_ns
from typing import TYPE_CHECKING, Mapping, cast

from neocortex.enumeration import JournalCursor, NtfsUsnError, query_journal_cursor
from neocortex.deduplication import (
    DedupIndex,
    DedupPlan,
    DedupPlanner,
    InventoryExclusionPolicy,
)
from neocortex.deduplication.inventory.index import validate_inventory_root
from neocortex.progress import NullProgress, ProgressCallback, ProgressEvent, emit_progress

from .actions import FrameworkActions
from .application_config_projections import global_resource_limits_from_application
from .cancellation import CancellationToken
from .corpus_access import CorpusAccessPolicy, path_trees_intersect
from .global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceSummary,
)
from .incremental_gate import IncrementalGateRequest, evaluate_incremental_gate
from .inventory_coordinator import PreparedInventory, prepare_inventory
from .internal_paths import InternalPathsPolicy
from .inventory_boundary import (
    AuthorizedStateDirectory as AuthorizedStateDirectory,
    NormalInventoryBoundary,
    _same_or_descendant as _same_or_descendant,
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from .locking import FrameworkRunLock
from .models import (
    ActionSummary,
    FrameworkConfig,
    InitialRunResult,
    RouteOnlyRunResult,
    SelfAnalysisRunResult,
)
from .route_registry import (
    RouteAdapter,
    RouteExecutionContext,
    builtin_route_registry,
    normalize_route_selection,
)
from .route_selection import ORGANIZABLE_ROUTE_NAMES
from .run_lifecycle import RunHeartbeat
from .self_analysis import (
    build_self_analysis_inventory_policy,
    self_analysis_commands,
)
from .state import FrameworkRouteState, FrameworkState
# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
    from .capabilities.formats.audio.models import AudioRouteSummary
    from .code_contracts import CodeRouteSummary
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary
    from .capabilities.formats.image.route import ImageRouteSummary
    from .capabilities.formats.office.route import OfficeRouteSummary
    from .pdf_route import PdfRouteSummary
    from .text_route import TextRouteSummary
    from .capabilities.formats.video.models import VideoRouteSummary
    from .document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )


@dataclass(frozen=True, slots=True)
class _SelfAnalysisExecution:
    inventory: PreparedInventory
    journal_after: JournalCursor | None
    code: CodeRouteSummary
    route_results: dict[str, object]
    global_resources: GlobalResourceSummary | None
    safety: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _InitialWork:
    inventory: PreparedInventory
    dedup_plan: DedupPlan
    actions: ActionSummary
    route_results: dict[str, object]
    image: ImageRouteSummary | None
    global_resources: GlobalResourceSummary | None
    organization_plan: OrganizationPlanSummary | None
    organization_apply: OrganizationApplySummary | None


@dataclass(frozen=True, slots=True)
class _InitialExecution:
    work: _InitialWork
    journal_after: JournalCursor | None


@dataclass(frozen=True, slots=True)
class _RouteOnlySource:
    run_id: int
    scan_id: int
    route_input_sources: Mapping[str, str]
    candidate_backed_routes: tuple[str, ...]
    candidate_rows: int


@dataclass(frozen=True, slots=True)
class _RouteOnlyExecution:
    run_id: int
    source_run_id: int
    route_results: dict[str, object]
    global_resources: GlobalResourceSummary | None


class RouteExecutionError(RuntimeError):
    def __init__(self, failures: Mapping[str, BaseException]):
        self.failures = dict(failures)
        detail = "; ".join(
            f"{name}={type(exc).__name__}: {exc}" for name, exc in self.failures.items()
        )
        super().__init__(f"one or more content routes failed: {detail}")


class FrameworkOrchestrator:
    """The only coordinator; component modules never start work on import."""

    def __init__(
        self,
        config: FrameworkConfig | None = None,
        *,
        progress: ProgressCallback | None = None,
        route_registry: Mapping[str, RouteAdapter] | None = None,
    ):
        self.config = config or FrameworkConfig()
        if self.config.dedup_policy not in {"fast", "exact"}:
            raise ValueError("dedup_policy must be 'fast' or 'exact'")
        self.route_registry = dict(route_registry or builtin_route_registry())
        self.selected_routes = normalize_route_selection(
            self.config.route, tuple(self.route_registry)
        )
        self.progress = progress or NullProgress()
        self._progress_lock = threading.Lock()
        self._active_progress: dict[tuple[str, str], ProgressEvent] = {}
        self._cancellation = CancellationToken()
        self._coordinator_lock = threading.Lock()
        self._active_coordinator: GlobalResourceCoordinator | None = None

    def request_cancellation(self) -> None:
        """Signal every route and wake any coordinator wait immediately."""

        self._cancellation.cancel()
        with self._coordinator_lock:
            coordinator = self._active_coordinator
        if coordinator is not None:
            coordinator.cancel()

    def _coordinated_progress(self, event: ProgressEvent) -> None:
        with self._progress_lock:
            if event.finished:
                self._active_progress.pop(event.key, None)
            else:
                self._active_progress[event.key] = event
            self.progress(event)

    def _finish_route_progress(
        self,
        route_name: str,
        outcome: str,
    ) -> None:
        """Stop every live task for a route, preserving partial failure progress."""

        descriptions = {
            "completed": "completada",
            "failed": "falló",
            "cancelled": "cancelada",
        }
        suffix = descriptions[outcome]
        with self._progress_lock:
            active = tuple(
                event for key, event in self._active_progress.items() if key[0] == route_name
            )
            for event in active:
                terminal = ProgressEvent(
                    event.operation,
                    event.phase,
                    f"{event.description} — {suffix}",
                    event.completed,
                    event.total if outcome == "completed" else None,
                    event.unit,
                    True,
                    event.metrics,
                )
                self._active_progress.pop(event.key, None)
                self.progress(terminal)

    def _validated_root(self) -> Path:
        root = validate_inventory_root(self.config.root)
        if os.name == "nt" and not root.drive:
            raise ValueError(f"framework root is not on a drive-letter volume: {root}")
        return root

    def _effective_excluded_paths(self, root: Path) -> tuple[Path, ...]:
        """Return one exclusion policy shared by scan, USN and actions."""

        boundary = build_normal_inventory_boundary(
            root,
            self.config.state_directory,
        )
        return tuple(Path(path) for path in boundary.exclusion_policy.explicit_roots)

    def _validated_self_analysis(
        self,
        root: Path,
    ) -> tuple[
        CorpusAccessPolicy,
        InventoryExclusionPolicy,
        Path,
        InternalPathsPolicy,
        CorpusAccessPolicy,
    ]:
        """Validate the preset and initialize only its exact disjoint state tree."""

        if not self.config.self_analysis:
            raise ValueError("self-analysis execution requires self_analysis=True")
        if self.config.corpus_access_mode != "analyze_only":
            raise ValueError("self-analysis requires analyze_only corpus access")
        if self.config.apply_actions:
            raise ValueError("self-analysis cannot apply corpus actions")
        if self.selected_routes != ("code",):
            raise ValueError("self-analysis requires exactly the code route")
        code_adapter = self.route_registry.get("code")
        if code_adapter is None or code_adapter.input_source != "inventory_snapshot":
            raise ValueError("self-analysis code route must consume inventory_snapshot")
        if (
            self.config.route_only
            or self.config.candidate_run_id is not None
            or self.config.resume_run_id is not None
            or self.config.selection.active
        ):
            raise ValueError("self-analysis cannot use route-only or selection controls")
        if self.config.document_catalog_enabled or self.config.organization_root is not None:
            raise ValueError("self-analysis cannot enable catalog or organization work")
        if self.config.code_include_generated or self.config.code_include_vendored:
            raise ValueError("self-analysis requires generated and vendored exclusions")
        if self.config.code_candidate_scope != "projects":
            raise ValueError("self-analysis requires project-scoped Code selection")

        access_policy = CorpusAccessPolicy.capture("analyze_only", root)
        state_layout = initialize_authorized_state_directory(
            access_policy,
            self.config.state_directory,
            require_disjoint=True,
        )
        state_directory = state_layout.path
        internal_paths_policy = state_layout.internal_paths_policy
        internal_paths_policy.validate_corpus_access(access_policy)
        self.config = replace(
            self.config,
            root=access_policy.root,
            state_directory=state_directory,
        )
        inventory_policy = build_self_analysis_inventory_policy(
            access_policy.root,
            state_directory,
        )
        return (
            access_policy,
            inventory_policy,
            state_directory,
            internal_paths_policy,
            state_layout.state_policy,
        )

    def _revalidate_self_analysis_boundary(
        self,
        access_policy: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
        state_directory: Path,
        *,
        require_state: bool,
        expected_state_identity: CorpusAccessPolicy | None = None,
    ) -> CorpusAccessPolicy | None:
        """Recheck the protected root and canonical state identity at I/O fences."""

        internal_paths_policy.validate_corpus_access(access_policy)
        state_directory = Path(os.path.abspath(state_directory))
        try:
            lexical_key = os.path.normcase(os.fspath(state_directory))
            physical_key = os.path.normcase(os.path.abspath(os.path.realpath(state_directory)))
            if lexical_key != physical_key:
                raise ValueError(
                    "self-analysis state_directory cannot use an alias or reparse path"
                )
            if path_trees_intersect(access_policy.root, state_directory):
                raise ValueError("self-analysis root and state directory must remain disjoint")
            if not require_state:
                observed_state_identity = None
            else:
                observed_state_identity = CorpusAccessPolicy.capture(
                    "analyze_only",
                    state_directory,
                )
                if os.path.normcase(os.fspath(observed_state_identity.root)) != lexical_key:
                    raise ValueError("self-analysis state_directory is not canonical")
                if expected_state_identity is not None and (
                    observed_state_identity.root_device_id,
                    observed_state_identity.root_file_id,
                    observed_state_identity.root_birthtime_ns,
                ) != (
                    expected_state_identity.root_device_id,
                    expected_state_identity.root_file_id,
                    expected_state_identity.root_birthtime_ns,
                ):
                    raise ValueError("self-analysis state_directory identity changed")
        except (OSError, ValueError) as exc:
            internal_paths_policy.validate_corpus_access(access_policy)
            raise ValueError("self-analysis state boundary cannot be verified") from exc
        internal_paths_policy.validate_corpus_access(access_policy)
        return observed_state_identity

    def _self_analysis_incremental_gate(
        self,
        *,
        state: FrameworkState,
        dedup_index: DedupIndex,
        root: Path,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        journal_before: JournalCursor,
    ) -> tuple[bool, str, int | None]:
        """Authorize checkpoint reuse only behind three matching owners."""

        request = IncrementalGateRequest.from_access_policy(
            access_policy,
            framework_policy_signature=inventory_policy.signature,
            inventory_policy_signature=inventory_policy.signature,
            journal_before=journal_before,
            verify_final=access_policy.verify_root_identity,
        )
        if request.root != root:
            raise ValueError("self-analysis gate root differs from its access policy")
        return evaluate_incremental_gate(
            request,
            state=state,
            inventory=dedup_index,
        ).as_tuple()

    def _normal_incremental_gate(
        self,
        *,
        state: FrameworkState,
        dedup_index: DedupIndex,
        boundary: NormalInventoryBoundary,
        journal_before: JournalCursor,
    ) -> tuple[bool, str, int | None]:
        """Authorize normal incremental reuse only from one exact durable owner."""

        request = IncrementalGateRequest.from_access_policy(
            boundary.access_policy,
            framework_policy_signature=boundary.effective_signature,
            inventory_policy_signature=boundary.exclusion_policy.signature,
            journal_before=journal_before,
            verify_final=boundary.verify,
        )
        return evaluate_incremental_gate(
            request,
            state=state,
            inventory=dedup_index,
        ).as_tuple()

    def _resource_coordinator(self) -> GlobalResourceCoordinator | None:
        if len(self.selected_routes) <= 1:
            return None
        return GlobalResourceCoordinator(
            self.selected_routes,
            global_resource_limits_from_application(self.config),
            cancellation=self._cancellation,
        )

    def _run_document_organization(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
    ) -> tuple["OrganizationPlanSummary | None", "OrganizationApplySummary | None"]:
        """Plan and consume every safe technical-document move under ``--apply``."""

        if not (
            self.config.apply_actions
            and self.config.document_catalog_enabled
            and ORGANIZABLE_ROUTE_NAMES.intersection(self.selected_routes)
        ):
            return None, None
        from .document_organization import (
            apply_all_document_organization,
            default_organization_root,
            plan_document_organization,
        )

        organization_root = self.config.organization_root
        if organization_root is None:
            organization_root = default_organization_root(
                self.config.framework_database,
                analysis_root=root,
            )
        else:
            organization_root = Path(os.path.abspath(organization_root.expanduser()))

        if self._cancellation.is_cancelled:
            raise KeyboardInterrupt
        state.set_run_phase(run_id, "organization_plan")
        plan_summary = plan_document_organization(
            self.config.document_catalog_database,
            organization_root,
            min_confidence=self.config.organization_min_confidence,
            progress=self.progress,
            mutation_guard=state.corpus_mutation_guard(run_id),
        )
        state.record_event(
            run_id,
            "warning" if plan_summary.blocked else "info",
            "document-organization-plan",
            "Plan de organización técnica completado",
            {"organization_root": str(organization_root), **asdict(plan_summary)},
        )
        if self._cancellation.is_cancelled:
            raise KeyboardInterrupt
        state.set_run_phase(run_id, "organization_apply")
        apply_summary = apply_all_document_organization(
            self.config.document_catalog_database,
            organization_root,
            progress=self.progress,
            mutation_guard=state.corpus_mutation_guard(run_id),
        )
        apply_issues = (
            apply_summary.stale
            + apply_summary.blocked
            + apply_summary.failed
            + apply_summary.cache_pending
        )
        state.record_event(
            run_id,
            "warning" if apply_issues else "info",
            "document-organization-apply",
            "Aplicación de organización técnica completada",
            {"organization_root": str(organization_root), **asdict(apply_summary)},
        )
        return plan_summary, apply_summary

    def _apply_explicit_adult_images(
        self,
        action_runner: FrameworkActions,
        image_summary: ImageRouteSummary | None,
        state: FrameworkState,
        run_id: int,
    ) -> ImageRouteSummary | None:
        """Plan or recycle only current-signature high-confidence image results."""

        if image_summary is None:
            return None
        from .capabilities.formats.image.state import iter_explicit_adult_candidates

        if image_summary.processing_signature is None:
            return image_summary
        candidates = iter_explicit_adult_candidates(
            self.config.image_database,
            run_id,
            image_summary.processing_signature,
        )
        applied, failed, protected = action_runner.recycle_verified_files(
            "trash_explicit_adult_image",
            candidates,
        )
        updated = replace(
            image_summary,
            adult_recycled=applied,
            adult_recycle_failed=failed,
            adult_recycle_protected=protected,
        )
        state.record_event(
            run_id,
            "warning" if failed else "info",
            "image-adult-actions",
            "Candidatos explícitos procesados con política de Papelera",
            {
                "apply_actions": self.config.apply_actions,
                "explicit_candidates": image_summary.adult_explicit,
                "recycled": applied,
                "failed": failed,
                "protected": protected,
            },
        )
        return updated

    def _run_content_routes(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
    ) -> tuple[dict[str, object], GlobalResourceSummary | None]:
        if not self.selected_routes:
            return {}, None

        state.set_run_phase(run_id, "routes")
        coordinator = self._resource_coordinator()
        with self._coordinator_lock:
            self._active_coordinator = coordinator
        if coordinator is not None:
            state.record_event(
                run_id,
                "info",
                "resource-coordinator",
                "Coordinador global iniciado",
                asdict(coordinator.summary()),
            )
        state.begin_route_runs(run_id, self.selected_routes)

        def execute_route(route_name: str):
            adapter = self.route_registry[route_name]
            context = RouteExecutionContext(
                config=self.config,
                root=root,
                framework_state=FrameworkRouteState(self.config.framework_database),
                run_id=run_id,
                scan_id=scan_id,
                progress=self._coordinated_progress,
                resource_coordinator=coordinator,
                cancellation=self._cancellation,
            )
            started = time.perf_counter_ns()
            summary = adapter.execute(context)
            return summary, time.perf_counter_ns() - started

        results: dict[str, object] = {}
        failures: dict[str, Exception] = {}
        executor = ThreadPoolExecutor(
            max_workers=len(self.selected_routes),
            thread_name_prefix="neocortex-route",
        )
        interrupted = False
        futures = {}
        try:
            futures = {
                executor.submit(execute_route, route_name): route_name
                for route_name in self.selected_routes
            }
            pending = set(futures)
            while pending:
                if self._cancellation.is_cancelled:
                    raise KeyboardInterrupt
                completed, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                if self._cancellation.is_cancelled:
                    raise KeyboardInterrupt
                for future in completed:
                    route_name = futures[future]
                    adapter = self.route_registry[route_name]
                    try:
                        summary, elapsed_ns = future.result()
                        mapping = dict(adapter.summary_mapping(summary))
                        persisted = {"elapsed_ns": elapsed_ns, **mapping}
                        state.complete_route_run(run_id, route_name, persisted)
                        state.record_event(
                            run_id,
                            "info",
                            route_name,
                            f"Ruta {route_name} completada",
                            persisted,
                        )
                        results[route_name] = summary
                        self._finish_route_progress(route_name, "completed")
                    except Exception as exc:
                        failures[route_name] = exc
                        state.fail_route_run(run_id, route_name, exc)
                        state.record_event(
                            run_id,
                            "error",
                            route_name,
                            f"Ruta {route_name} fallida",
                            {
                                "error_type": type(exc).__name__,
                                "detail": str(exc),
                            },
                        )
                        self._finish_route_progress(route_name, "failed")
        except KeyboardInterrupt:
            interrupted = True
            self.request_cancellation()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=interrupted)
            if interrupted:
                for route_name in self.selected_routes:
                    self._finish_route_progress(route_name, "cancelled")
            with self._coordinator_lock:
                if self._active_coordinator is coordinator:
                    self._active_coordinator = None

        resource_summary = self._complete_resource_coordination(
            state,
            run_id,
            coordinator,
        )
        if failures:
            raise RouteExecutionError(failures)
        return results, resource_summary

    @staticmethod
    def _complete_resource_coordination(
        state: FrameworkState,
        run_id: int,
        coordinator: GlobalResourceCoordinator | None,
    ) -> GlobalResourceSummary | None:
        if coordinator is None:
            return None
        summary = coordinator.summary()
        state.record_event(
            run_id,
            "info",
            "resource-coordinator",
            "Coordinación global completada",
            asdict(summary),
        )
        return summary

    def run(
        self,
    ) -> InitialRunResult | SelfAnalysisRunResult | RouteOnlyRunResult:
        """Dispatch a full inventory run or an explicitly isolated route run."""

        if self.config.self_analysis:
            return self.run_self_analysis()
        if self.config.route_only or self.config.resume_run_id is not None:
            return self.run_route_only()
        return self.run_initial()

    def run_self_analysis(self) -> SelfAnalysisRunResult:
        """Analyze one protected source root without common corpus work."""

        self._cancellation = CancellationToken()
        root = self._validated_root()
        (
            access_policy,
            inventory_policy,
            state_directory,
            internal_paths_policy,
            state_identity,
        ) = self._validated_self_analysis(root)
        observed_state_identity = self._revalidate_self_analysis_boundary(
            access_policy,
            internal_paths_policy,
            state_directory,
            require_state=True,
            expected_state_identity=state_identity,
        )
        if observed_state_identity is None:
            raise RuntimeError("self-analysis state identity was not captured")
        with FrameworkRunLock(state_directory / "framework.lock"):
            return self._run_self_analysis_locked(
                access_policy,
                inventory_policy,
                state_identity,
                internal_paths_policy,
            )

    def _run_self_analysis_locked(
        self,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
    ) -> SelfAnalysisRunResult:
        root = access_policy.root
        journal_before, journal_error = self._prepare_self_analysis(
            access_policy,
            state_identity,
            internal_paths_policy,
        )
        commands = self_analysis_commands(
            self.config,
            root,
            self.config.state_directory,
        )
        self._revalidate_self_analysis_boundary(
            access_policy,
            internal_paths_policy,
            self.config.state_directory,
            require_state=True,
            expected_state_identity=state_identity,
        )
        with FrameworkState(self.config.framework_database) as state:
            state.mark_abandoned_runs()
            state.mark_abandoned_actions()
            run_id = state.begin_self_analysis_run(
                access_policy,
                journal_before,
                state_directory=self.config.state_directory,
                inventory_policy_signature=inventory_policy.signature,
            )
            execution = self._manage_self_analysis_run(
                state=state,
                run_id=run_id,
                access_policy=access_policy,
                inventory_policy=inventory_policy,
                state_identity=state_identity,
                internal_paths_policy=internal_paths_policy,
                journal_before=journal_before,
                journal_error=journal_error,
                commands=commands,
            )

        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "complete",
                "Autoanálisis protegido completado",
                1,
                1,
                "fase",
                True,
            ),
        )
        return self._self_analysis_result(run_id, inventory_policy, execution)

    def _prepare_self_analysis(
        self,
        access_policy: CorpusAccessPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
    ) -> tuple[JournalCursor | None, str | None]:
        self._revalidate_self_analysis_boundary(
            access_policy,
            internal_paths_policy,
            self.config.state_directory,
            require_state=True,
            expected_state_identity=state_identity,
        )
        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "prepare",
                "Preparando autoanálisis protegido",
                0,
                1,
                "fase",
            ),
        )
        journal_error: str | None = None
        if os.name != "nt":
            journal_before = None
            journal_error = "portable_inventory_backend"
        else:
            try:
                journal_before = query_journal_cursor(access_policy.root.drive)
            except (NtfsUsnError, OSError) as exc:
                journal_before = None
                journal_error = f"{type(exc).__name__}: {exc}"
        emit_progress(
            self.progress,
            ProgressEvent(
                "framework",
                "prepare",
                "Autoanálisis preparado",
                1,
                1,
                "fase",
                True,
            ),
        )
        return journal_before, journal_error

    def _manage_self_analysis_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
        journal_before: JournalCursor | None,
        journal_error: str | None,
        commands: dict[str, list[str]],
    ) -> _SelfAnalysisExecution:
        heartbeat = RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()
        try:
            return self._execute_self_analysis_run(
                state=state,
                run_id=run_id,
                access_policy=access_policy,
                inventory_policy=inventory_policy,
                state_identity=state_identity,
                internal_paths_policy=internal_paths_policy,
                journal_before=journal_before,
                journal_error=journal_error,
                commands=commands,
            )
        except KeyboardInterrupt as exc:
            self._persist_self_analysis_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            self._persist_self_analysis_termination(state, run_id, exc, cancelled=False)
            raise
        finally:
            heartbeat.stop()

    @staticmethod
    def _persist_self_analysis_termination(
        state: FrameworkState,
        run_id: int,
        exc: BaseException,
        *,
        cancelled: bool,
    ) -> None:
        transition_name = "cancellation" if cancelled else "failure"
        try:
            transitioned = (
                state.cancel_initial_run(run_id) if cancelled else state.fail_initial_run(run_id)
            )
        except Exception as transition_exc:
            exc.add_note(
                f"{transition_name} status could not be persisted: "
                f"{type(transition_exc).__name__}: {transition_exc}"
            )
            return
        if not transitioned:
            return
        try:
            state.record_event(
                run_id,
                "warning" if cancelled else "error",
                "run",
                ("Autoanálisis cancelado por el usuario" if cancelled else "Autoanálisis fallido"),
                None if cancelled else {"error_type": type(exc).__name__, "detail": str(exc)},
            )
        except Exception as event_exc:
            exc.add_note(
                f"{transition_name} event could not be persisted: "
                f"{type(event_exc).__name__}: {event_exc}"
            )

    def _execute_self_analysis_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
        journal_before: JournalCursor | None,
        journal_error: str | None,
        commands: dict[str, list[str]],
    ) -> _SelfAnalysisExecution:
        self._record_self_analysis_start(
            state,
            run_id,
            access_policy,
            inventory_policy,
            internal_paths_policy,
            journal_before,
            journal_error,
        )
        inventory = self._prepare_self_analysis_inventory(
            state=state,
            run_id=run_id,
            access_policy=access_policy,
            inventory_policy=inventory_policy,
            state_identity=state_identity,
            internal_paths_policy=internal_paths_policy,
            journal_before=journal_before,
        )
        code_summary, route_results, global_resources, journal_after = (
            self._run_self_analysis_code_routes(
                state=state,
                run_id=run_id,
                root=access_policy.root,
                inventory=inventory,
            )
        )
        safety = self._finalize_self_analysis(
            state=state,
            run_id=run_id,
            access_policy=access_policy,
            inventory_policy=inventory_policy,
            state_identity=state_identity,
            internal_paths_policy=internal_paths_policy,
            journal_after=journal_after,
            code_processing_signature=code_summary.processing_signature,
            commands=commands,
        )
        return _SelfAnalysisExecution(
            inventory,
            journal_after,
            code_summary,
            route_results,
            global_resources,
            safety,
        )

    def _record_self_analysis_start(
        self,
        state: FrameworkState,
        run_id: int,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        internal_paths_policy: InternalPathsPolicy,
        journal_before: JournalCursor | None,
        journal_error: str | None,
    ) -> None:
        state.record_event(
            run_id,
            "info",
            "run",
            "Autoanálisis protegido iniciado",
            {
                "root": str(access_policy.root),
                "state_directory": str(self.config.state_directory),
                "corpus_access_mode": "analyze_only",
                "inventory_policy_signature": inventory_policy.signature,
                "journal_status": ("available" if journal_before is not None else "unavailable"),
                "journal_error": journal_error,
                "internal_paths_policy": internal_paths_policy.manifest(),
                "selected_routes": ["code"],
            },
        )

    def _prepare_self_analysis_inventory(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
        journal_before: JournalCursor | None,
    ) -> PreparedInventory:
        state.set_run_phase(run_id, "inventory")
        with DedupIndex(self.config.dedup_database) as dedup_index:
            allow_incremental = False
            gate_reason = "journal_unavailable_full_scan"
            source_run_id = None
            if journal_before is not None:
                allow_incremental, gate_reason, source_run_id = (
                    self._self_analysis_incremental_gate(
                        state=state,
                        dedup_index=dedup_index,
                        root=access_policy.root,
                        access_policy=access_policy,
                        inventory_policy=inventory_policy,
                        journal_before=journal_before,
                    )
                )
            state.record_event(
                run_id,
                "info" if allow_incremental else "warning",
                "self-analysis-incremental-gate",
                "Reutilización incremental evaluada",
                {
                    "allowed": allow_incremental,
                    "reason": gate_reason,
                    "source_run_id": source_run_id,
                    "inventory_policy_signature": inventory_policy.signature,
                },
            )
            inventory = prepare_inventory(
                dedup_index,
                state,
                run_id,
                access_policy.root,
                journal_before,
                progress=self.progress,
                allow_incremental=allow_incremental,
                exclusion_policy=inventory_policy,
            )
            self._revalidate_self_analysis_boundary(
                access_policy,
                internal_paths_policy,
                self.config.state_directory,
                require_state=True,
                expected_state_identity=state_identity,
            )
        candidate_rows = state.route_candidate_run_count(run_id)
        if candidate_rows != 0:
            raise RuntimeError("self-analysis produced MIME route candidates")
        state.publish_initial_routing_snapshot(
            run_id,
            inventory.scan.scan_id,
            inventory.reconciliation_records,
            inventory.inventory_attempts,
            inventory.inventory_mode,
            candidate_rows,
        )
        return inventory

    def _run_self_analysis_code_routes(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        root: Path,
        inventory: PreparedInventory,
    ) -> tuple[
        CodeRouteSummary,
        dict[str, object],
        GlobalResourceSummary | None,
        JournalCursor | None,
    ]:
        route_results, global_resources = self._run_content_routes(
            root=root,
            state=state,
            run_id=run_id,
            scan_id=inventory.scan.scan_id,
        )
        code_summary = cast("CodeRouteSummary | None", route_results.get("code"))
        if code_summary is None:
            raise RuntimeError("self-analysis code route returned no summary")
        code_processing_signature = code_summary.processing_signature
        if not isinstance(code_processing_signature, str) or not code_processing_signature:
            raise RuntimeError("self-analysis code route returned no effective signature")
        journal_after = self._self_analysis_journal_after(inventory)
        return code_summary, route_results, global_resources, journal_after

    @staticmethod
    def _self_analysis_journal_after(
        inventory: PreparedInventory,
    ) -> JournalCursor | None:
        reconciliation = inventory.reconciliation
        journal_after = None if reconciliation is None else reconciliation.cursor
        journal_before = inventory.journal_before
        if (journal_after is None) != (journal_before is None):
            raise RuntimeError("self-analysis inventory returned partial journal evidence")
        if (
            journal_after is not None
            and journal_before is not None
            and journal_after.journal_id != journal_before.journal_id
        ):
            raise RuntimeError("the USN journal changed during protected self-analysis")
        return journal_after

    def _finalize_self_analysis(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        access_policy: CorpusAccessPolicy,
        inventory_policy: InventoryExclusionPolicy,
        state_identity: CorpusAccessPolicy,
        internal_paths_policy: InternalPathsPolicy,
        journal_after: JournalCursor | None,
        code_processing_signature: str,
        commands: dict[str, list[str]],
    ) -> Mapping[str, int]:
        self._revalidate_self_analysis_boundary(
            access_policy,
            internal_paths_policy,
            self.config.state_directory,
            require_state=True,
            expected_state_identity=state_identity,
        )
        state.set_run_phase(run_id, "finalize")
        completion_manifest = state.complete_self_analysis_run(
            run_id,
            journal_after,
            inventory_policy=inventory_policy,
            code_processing_signature=code_processing_signature,
            commands=commands,
        )
        return cast(Mapping[str, int], completion_manifest["safety"])

    @staticmethod
    def _self_analysis_result(
        run_id: int,
        inventory_policy: InventoryExclusionPolicy,
        execution: _SelfAnalysisExecution,
    ) -> SelfAnalysisRunResult:
        inventory = execution.inventory
        return SelfAnalysisRunResult(
            run_id=run_id,
            scan=inventory.scan,
            journal_before=inventory.journal_before,
            journal_after=execution.journal_after,
            reconciliation_records=inventory.reconciliation_records,
            inventory_attempts=inventory.inventory_attempts,
            inventory_mode=inventory.inventory_mode,
            inventory_policy_signature=inventory_policy.signature,
            code=execution.code,
            route_results=execution.route_results,
            global_resources=execution.global_resources,
            corpus_action_count=execution.safety["file_actions"],
            route_candidate_count=execution.safety["route_candidates"],
        )

    def run_initial(self) -> InitialRunResult:
        """Run the pre-index stage, optionally applying explicitly enabled actions."""

        if self.config.self_analysis or self.config.corpus_access_mode != "normal":
            raise ValueError("run_initial cannot execute an analyze-only configuration")
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
        )
        boundary.verify()
        with FrameworkRunLock(self.config.state_directory / "framework.lock"):
            return self._run_initial_locked(boundary)

    def _prepare_initial_run(
        self,
        boundary: NormalInventoryBoundary,
    ) -> tuple[JournalCursor | None, str | None]:
        boundary.verify()
        emit_progress(
            self.progress,
            ProgressEvent("framework", "prepare", "Preparando ejecución", 0, 1, "fase"),
        )
        journal_error: str | None = None
        if os.name != "nt":
            journal_before = None
            journal_error = "portable_inventory_backend"
        else:
            try:
                journal_before = query_journal_cursor(boundary.access_policy.root.drive)
            except (NtfsUsnError, OSError) as exc:
                journal_before = None
                journal_error = f"{type(exc).__name__}: {exc}"
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
        return {
            "route": self.config.route,
            "selected_routes": list(self.selected_routes),
            "global_memory_budget_bytes": self.config.global_memory_budget_bytes,
            "global_min_free_memory_bytes": self.config.global_min_free_memory_bytes,
            "global_min_free_commit_bytes": self.config.global_min_free_commit_bytes,
            "global_cpu_slots": self.config.global_cpu_slots,
            "global_max_cpu_load_percent": self.config.global_max_cpu_load_percent,
            "global_resource_wait_timeout_seconds": (
                self.config.global_resource_wait_timeout_seconds
            ),
            "dedup_policy": self.config.dedup_policy,
            "code_max_file_bytes": self.config.code_max_file_bytes,
            "code_max_documents": self.config.code_max_documents,
            "code_cache_validation": self.config.code_cache_validation,
            "code_candidate_scope": self.config.code_candidate_scope,
            "code_include_generated": self.config.code_include_generated,
            "code_include_vendored": self.config.code_include_vendored,
            "apply_actions": self.config.apply_actions,
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
            "audio_memory_budget_bytes": self.config.audio_memory_budget_bytes,
            "audio_worker_memory_bytes": self.config.audio_worker_memory_bytes,
        }

    def _record_initial_start(
        self,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: JournalCursor | None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
    ) -> None:
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
            self._initial_configuration_payload(boundary, excluded_paths),
        )

    def _prepare_normal_inventory(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        dedup_index: DedupIndex,
        journal_before: JournalCursor | None,
    ) -> PreparedInventory:
        state.set_run_phase(run_id, "inventory")
        if journal_before is None:
            allow_incremental = False
            gate_reason = "journal_unavailable_portable_full_scan"
            source_run_id = None
        else:
            allow_incremental, gate_reason, source_run_id = self._normal_incremental_gate(
                state=state,
                dedup_index=dedup_index,
                boundary=boundary,
                journal_before=journal_before,
            )
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
        )
        boundary.verify()
        if inventory.inventory_policy_signature != boundary.exclusion_policy.signature:
            raise RuntimeError("inventory result escaped its effective exclusion boundary")
        return inventory

    def _plan_initial_dedup(
        self,
        state: FrameworkState,
        run_id: int,
        dedup_index: DedupIndex,
        scan_id: int,
    ) -> DedupPlan:
        state.set_run_phase(run_id, "dedup_plan")
        started = time.perf_counter_ns()
        plan = DedupPlanner(dedup_index).plan(
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
            },
        )
        return plan

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
    ) -> tuple[FrameworkActions, ActionSummary]:
        runner = FrameworkActions(
            dedup_index,
            state,
            run_id,
            scan_id,
            apply=self.config.apply_actions,
            verify_bytes_before_trash=True,
            excluded_paths=excluded_paths,
            exclusion_policy=inventory_policy,
            progress=self.progress,
        )
        state.set_run_phase(run_id, "actions")
        actions = runner.execute(
            plan,
            cleanup_empty_directories=not self.selected_routes,
        )
        return runner, actions

    def _run_initial_routes(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        action_runner: FrameworkActions,
        plan: DedupPlan,
        actions: ActionSummary,
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
        image_summary = self._apply_explicit_adult_images(
            action_runner,
            image_summary,
            state,
            run_id,
        )
        if image_summary is not None:
            route_results["image"] = image_summary
        organization_plan, organization_apply = self._run_document_organization(
            root=root,
            state=state,
            run_id=run_id,
        )
        if self.selected_routes:
            actions = action_runner.cleanup_empty_directories(plan, actions)
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
        journal_before: JournalCursor | None,
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
                action_runner=action_runner,
                plan=plan,
                actions=actions,
            )
        return _InitialWork(
            inventory,
            plan,
            actions,
            route_results,
            image_summary,
            global_resources,
            organization_plan,
            organization_apply,
        )

    @staticmethod
    def _initial_journal_after(inventory: PreparedInventory) -> JournalCursor | None:
        reconciliation = inventory.reconciliation
        journal_after = None if reconciliation is None else reconciliation.cursor
        journal_before = inventory.journal_before
        if (journal_after is None) != (journal_before is None):
            raise RuntimeError("normal inventory returned partial journal evidence")
        if (
            journal_after is not None
            and journal_before is not None
            and journal_after.journal_id != journal_before.journal_id
        ):
            raise RuntimeError("the USN journal changed during the initial framework run")
        return journal_after

    @staticmethod
    def _finalize_initial_run(
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        work: _InitialWork,
        journal_after: JournalCursor | None,
    ) -> None:
        boundary.verify()
        state.set_run_phase(run_id, "finalize")
        transient_rows_pruned = state.prune_route_candidates((run_id,))
        inventory = work.inventory
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
            "info",
            "run",
            "Ejecución completada",
            {
                "inventory_mode": inventory.inventory_mode,
                "scan_id": inventory.scan.scan_id,
                "transient_route_rows_pruned": transient_rows_pruned,
            },
        )

    def _execute_initial_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: JournalCursor | None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
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
        state.prune_route_candidates((run_id,))
        state.record_event(
            run_id,
            "warning" if cancelled else "error",
            "run",
            "Ejecución cancelada por el usuario" if cancelled else "Ejecución fallida",
            None if cancelled else {"error_type": type(exc).__name__, "detail": str(exc)},
        )
        if cancelled:
            state.cancel_initial_run(run_id)
        else:
            state.fail_initial_run(run_id)

    def _manage_initial_run(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        boundary: NormalInventoryBoundary,
        journal_before: JournalCursor | None,
        journal_error: str | None,
        excluded_paths: tuple[Path, ...],
    ) -> _InitialExecution:
        heartbeat = RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()
        try:
            return self._execute_initial_run(
                state=state,
                run_id=run_id,
                boundary=boundary,
                journal_before=journal_before,
                journal_error=journal_error,
                excluded_paths=excluded_paths,
            )
        except KeyboardInterrupt as exc:
            self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            self._persist_initial_termination(state, run_id, exc, cancelled=False)
            raise
        finally:
            heartbeat.stop()

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
            code=cast("CodeRouteSummary | None", routes.get("code")),
            route_results=routes,
            global_resources=work.global_resources,
            organization_plan=work.organization_plan,
            organization_apply=work.organization_apply,
        )

    def _run_initial_locked(
        self,
        boundary: NormalInventoryBoundary,
    ) -> InitialRunResult:
        excluded_paths = tuple(Path(path) for path in boundary.exclusion_policy.explicit_roots)
        journal_before, journal_error = self._prepare_initial_run(boundary)
        with FrameworkState(self.config.framework_database) as state:
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

    def run_route_only(self) -> RouteOnlyRunResult:
        """Run content routes over durable inputs without common maintenance."""

        if self.config.self_analysis or self.config.corpus_access_mode != "normal":
            raise ValueError("run_route_only cannot execute self-analysis")
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
        )
        boundary.verify()
        with FrameworkRunLock(self.config.state_directory / "framework.lock"):
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
        boundary.verify()
        with FrameworkState(self.config.framework_database) as state:
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
        if self.config.resume_run_id is not None and not self.selected_routes:
            resumable = state.resumable_route_names(source_run_id)
            unknown = tuple(name for name in resumable if name not in self.route_registry)
            if unknown:
                raise ValueError(
                    "resume source references unavailable routes: " + ", ".join(unknown)
                )
            self.selected_routes = resumable
        if not self.selected_routes:
            raise ValueError(f"run {source_run_id} has no resumable content routes")

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
            "selected_routes": list(self.selected_routes),
            "resume": self.config.resume_run_id is not None,
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
        boundary.verify()
        run_id = state.begin_operational_run(
            boundary.access_policy.root,
            run_kind=run_kind,
            source_run_id=source.run_id,
        )
        copied = (
            state.copy_route_candidates(source.run_id, run_id)
            if source.candidate_backed_routes
            else 0
        )
        heartbeat = RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()
        state.record_event(
            run_id,
            "info",
            "run",
            "Ejecución aislada de rutas iniciada",
            self._route_only_start_payload(boundary, source, copied),
        )
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
        self._prune_route_only_candidates(state, source, run_id)
        state.cancel_initial_run(run_id)

    def _fail_route_only_run(
        self,
        state: FrameworkState,
        source: _RouteOnlySource,
        run_id: int,
        exc: BaseException,
    ) -> None:
        self._prune_route_only_candidates(state, source, run_id)
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
    ) -> _RouteOnlyExecution:
        try:
            route_results, global_resources = self._run_content_routes(
                root=boundary.access_policy.root,
                state=state,
                run_id=run_id,
                scan_id=source.scan_id,
            )
            self._complete_route_only_run(state, boundary, source, run_id)
        except KeyboardInterrupt:
            self._cancel_route_only_run(state, source, run_id)
            raise
        except BaseException as exc:
            self._fail_route_only_run(state, source, run_id, exc)
            raise
        finally:
            heartbeat.stop()
        return _RouteOnlyExecution(
            run_id,
            source.run_id,
            route_results,
            global_resources,
        )

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
            code=cast("CodeRouteSummary | None", routes.get("code")),
            route_results=routes,
            global_resources=execution.global_resources,
        )


# endregion [02]
