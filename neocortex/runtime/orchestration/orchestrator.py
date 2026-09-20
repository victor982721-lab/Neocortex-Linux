"""Coordination of filesystem checkpoints and pre-index deduplication."""
# region [00] Contexto del módulo
# Módulo: neocortex/orchestrator.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from neocortex.platform.policy import stat_birthtime_ns
from typing import TYPE_CHECKING, cast

from neocortex.deduplication import (
    DedupIndex,
    DedupPlan,
    DedupPlanner,
    InventoryExclusionPolicy,
)
from neocortex.deduplication.inventory.index import validate_inventory_root
from neocortex.progress import NullProgress, ProgressCallback, ProgressEvent, emit_progress
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.runtime.config.application_config_projections import (
    global_resource_limits_from_application,
)
from neocortex.runtime.config.runtime_cache import XDG_CACHE_HOME_ENVIRONMENT
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.corpus_access import CorpusAccessPolicy
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceSummary,
    resource_gate,
    resource_scope,
)
from neocortex.integrations.inventory.inventory_coordinator import (
    PreparedInventory,
    prepare_inventory,
)
from neocortex.integrations.inventory.inventory_boundary import (
    AuthorizedStateDirectory as AuthorizedStateDirectory,
    NormalInventoryBoundary,
    _same_or_descendant as _same_or_descendant,
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,
)
from neocortex.runtime.control.locking import FrameworkRunLock
from neocortex.runtime.models import (
    ActionSummary,
    FrameworkConfig,
    InitialRunResult,
    RouteOnlyRunResult,
)
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    RouteExecutionContext,
    builtin_route_registry,
    normalize_route_selection,
)
from neocortex.runtime.orchestration.route_selection import ORGANIZABLE_ROUTE_NAMES
from neocortex.runtime.orchestration.run_lifecycle import RunHeartbeat
from neocortex.runtime.orchestration.run_manifest import RunBudget, RunManifest
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import (
    FrameworkState,
    RunBudgetExceeded,
)


def _complete_root_identity(policy: CorpusAccessPolicy) -> tuple[int, int, int]:
    """Return the manifest identity only when all root fields are captured."""

    identity = (
        policy.root_device_id,
        policy.root_file_id,
        policy.root_birthtime_ns,
    )
    if any(type(value) is not int for value in identity):
        raise ValueError("corpus root identity is incomplete")
    return cast(tuple[int, int, int], identity)


# endregion [01]

# region [02] Implementación

if TYPE_CHECKING:
    from neocortex.capabilities.formats.archive.models import ArchiveRouteSummary
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary
    from neocortex.capabilities.formats.docx.route import DocxRouteSummary
    from neocortex.capabilities.formats.image.route import ImageRouteSummary
    from neocortex.capabilities.formats.office.route import OfficeRouteSummary
    from neocortex.capabilities.formats.pdf.pdf_route import PdfRouteSummary
    from neocortex.capabilities.formats.text.text_route import TextRouteSummary
    from neocortex.capabilities.formats.video.models import VideoRouteSummary
    from neocortex.documents.document_organization import (
        OrganizationApplySummary,
        OrganizationPlanSummary,
    )


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
    route_failures: dict[str, str] = field(default_factory=dict)
    maintenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _InitialExecution:
    work: _InitialWork
    journal_after: None


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
    route_failures: dict[str, str] = field(default_factory=dict)


class RouteExecutionError(RuntimeError):
    def __init__(self, failures: Mapping[str, BaseException]):
        self.failures = dict(failures)
        detail = "; ".join(
            f"{name}={type(exc).__name__}: {exc}" for name, exc in self.failures.items()
        )
        super().__init__(f"one or more content routes failed: {detail}")


class FrameworkOrchestrator:
    """The only coordinator; component modules never start work on import."""

    _SCRATCH_OWNER = "neocortex-framework"
    _SCRATCH_SCOPE = "owned-temp"
    # Keep the event payload bounded even if a legacy adapter returns an
    # untrusted collection or counter.  The scratch owner remains the source
    # of truth for the complete plan; Framework only publishes this compact
    # observation.
    _SCRATCH_REPORT_COUNT_LIMIT = 1_000_000
    _SCRATCH_REPORT_BYTES_LIMIT = 16 * 1024 * 1024 * 1024 * 1024

    def __init__(
        self,
        config: FrameworkConfig | None = None,
        *,
        progress: ProgressCallback | None = None,
        route_registry: Mapping[str, RouteAdapter] | None = None,
        run_budget: RunBudget | Mapping[str, object] | None = None,
        lifecycle_stage_runner: Callable[[int], object] | None = None,
        lifecycle_stage_details: Mapping[str, object] | None = None,
    ):
        self.config = config or FrameworkConfig()
        self._unavailable_routes: dict[str, str] = {}
        self._organization_resume_pending = False
        if self.config.dedup_policy not in {"fast", "exact"}:
            raise ValueError("dedup_policy must be 'fast' or 'exact'")
        from .dedup_keeper import preflight_keeper_inputs, validate_keeper_configuration

        validate_keeper_configuration(self.config.dedup_keep_paths, self.config.dedup_prefer_roots)
        preflight_keeper_inputs(
            self.config.root,
            keep_paths=self.config.dedup_keep_paths,
            preferred_roots=self.config.dedup_prefer_roots,
        )
        if (self.config.dedup_keep_paths or self.config.dedup_prefer_roots) and (
            self.config.route_only
            or self.config.candidate_run_id is not None
            or self.config.resume_run_id is not None
        ):
            raise ValueError(
                "keeper preferences require an initial inventory and duplicate-plan run"
            )
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
        self._active_run: tuple[Path, int] | None = None
        self._resource_deadline: tuple[int, Mapping[str, object]] | None = None
        self._run_budget = (
            RunBudget.from_mapping(run_budget)
            if isinstance(run_budget, Mapping)
            else (run_budget or RunBudget())
        )
        self._lifecycle_stage_runner = lifecycle_stage_runner
        self._lifecycle_stage_details = (
            {} if lifecycle_stage_details is None else dict(lifecycle_stage_details)
        )

    def _durable_run_budget(self) -> RunBudget:
        """Resolve explicit lifecycle limits without changing FrameworkConfig.

        The public config grows independently from the lifecycle contract.  A
        caller may pass ``run_budget`` directly, while forward-compatible
        config projections can expose any of the aliases below without making
        old callers reconstruct a new config object.
        """

        configured = {
            "max_items": next(
                (
                    getattr(self.config, name)
                    for name in (
                        "run_max_items",
                        "lifecycle_max_items",
                        "max_items",
                    )
                    if hasattr(self.config, name)
                ),
                None,
            ),
            "max_bytes": next(
                (
                    getattr(self.config, name)
                    for name in (
                        "run_max_bytes",
                        "lifecycle_max_bytes",
                        "max_bytes",
                    )
                    if hasattr(self.config, name)
                ),
                None,
            ),
            "max_duration_seconds": next(
                (
                    getattr(self.config, name)
                    for name in (
                        "run_time_budget_seconds",
                        "lifecycle_time_budget_seconds",
                        "max_duration_seconds",
                    )
                    if hasattr(self.config, name)
                ),
                None,
            ),
        }
        if any(value is not None for value in configured.values()):
            return RunBudget.from_mapping(configured)
        return self._run_budget

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
    ) -> dict[str, object] | None:
        """Account bounded non-route work in the shared lifecycle ledger."""

        read_budget = getattr(state, "read_run_budget", None)
        if not callable(read_budget) or read_budget(run_id) is None:
            return None
        if type(items) is not int or items < 0 or type(bytes_count) is not int or bytes_count < 0:
            raise ValueError("lifecycle stage workload must be non-negative integers")
        reserve = getattr(state, "reserve_run_stage", None)
        if callable(reserve):
            typed_reserve = cast(Callable[..., dict[str, object]], reserve)
            return typed_reserve(
                run_id,
                stage,
                reservation_id,
                items=items,
                bytes=bytes_count,
                worker=worker,
            )
        return state.reserve_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes_count,
            worker=worker,
            stage=stage,
        )

    def _route_only_budget(
        self,
        state: FrameworkState,
        source_run_id: int,
    ) -> tuple[RunBudget, Mapping[str, object] | None]:
        """Carry the source ledger's remaining budget into a resume.

        A resumed route run is a continuation of the same --all work
        boundary, not a fresh budget window.  The writer exposes the source
        snapshot through its owner connection, so derive a conservative
        remaining budget here instead of opening the SQLite owner separately.
        Explicit limits supplied for the new invocation can only tighten the
        source remainder; they must not reset work already reserved.
        """

        requested = self._durable_run_budget()
        if self.config.resume_run_id is None:
            return requested, None
        read_budget = getattr(state, "read_run_budget", None)
        if not callable(read_budget):
            # Legacy state doubles and pre-manifest runs do not have a source
            # ledger.  Preserve their existing compatibility behavior.
            return requested, None
        source = read_budget(source_run_id)
        if source is None:
            return requested, None
        if not isinstance(source, Mapping):
            raise RuntimeError(f"run {source_run_id} lifecycle budget is invalid")

        def capped_integer(name: str, configured: int | None) -> int | None:
            remaining = source.get(name)
            if remaining is None:
                return configured
            if type(remaining) is not int or remaining < 0:
                raise RuntimeError(f"run {source_run_id} lifecycle budget has invalid {name}")
            return remaining if configured is None else min(configured, remaining)

        source_duration: float | None = None
        if source.get("max_duration_seconds") is not None:
            deadline = source.get("deadline_ns")
            if type(deadline) is not int:
                raise RuntimeError(f"run {source_run_id} lifecycle budget has no valid deadline")
            remaining_ns = deadline - time.time_ns()
            if remaining_ns <= 0:
                raise RunBudgetExceeded("time", source)
            source_duration = remaining_ns / 1_000_000_000
        duration = requested.max_duration_seconds
        if source_duration is not None:
            duration = (
                source_duration if duration is None else min(float(duration), source_duration)
            )
        return (
            RunBudget(
                max_items=capped_integer("remaining_items", requested.max_items),
                max_bytes=capped_integer("remaining_bytes", requested.max_bytes),
                max_duration_seconds=duration,
            ),
            source,
        )

    def request_cancellation(self) -> None:
        """Signal every route and wake any coordinator wait immediately."""

        self._cancellation.cancel()
        active_run = self._active_run
        if active_run is not None:
            try:
                FrameworkState.request_run_cancellation_external(
                    active_run[0], active_run[1], "user"
                )
            except (OSError, RuntimeError, sqlite3.Error):
                # The foreground termination path records the same durable
                # request through its owner connection if this side-channel
                # races a SQLite transaction.
                pass
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
        """Return one exclusion policy shared by portable scan and actions."""

        boundary = build_normal_inventory_boundary(
            root,
            self.config.state_directory,
            observe_regenerable_artifacts=bool(self.selected_routes),
        )
        return tuple(Path(path) for path in boundary.exclusion_policy.explicit_roots)

    def _resource_coordinator(self) -> GlobalResourceCoordinator:
        if self._active_coordinator is not None:
            return self._active_coordinator
        stages = tuple(dict.fromkeys((
            "inventory", "dedup", *self.selected_routes,
            "catalog", "semantic", "knowledge", "preparation",
        )))
        return GlobalResourceCoordinator(
            stages,
            global_resource_limits_from_application(self.config),
            cancellation=self._cancellation,
            checkpoint=self._check_resource_deadline,
            route_memory_budgets={
                name: budget
                for name in ("image", "docx", "office", "audio", "pdf")
                if (budget := getattr(self.config, f"{name}_memory_budget_bytes")) is not None
            },
        )

    def _bind_resource_deadline(self, state: FrameworkState, run_id: int) -> None:
        """Copy the published deadline once through the owner connection.

        Resource waits may run on workers or block the inventory owner itself.
        Their checkpoints must neither open SQLite nor renew the run's clock.
        """

        read_budget = getattr(state, "read_run_budget", None)
        budget = read_budget(run_id) if callable(read_budget) else None
        deadline = None if budget is None else budget.get("deadline_ns")
        if budget is None or deadline is None:
            self._resource_deadline = None
        elif type(deadline) is not int:
            raise RuntimeError("run budget has an invalid resource-wait deadline")
        else:
            monotonic_deadline = time.monotonic_ns() + deadline - time.time_ns()
            self._resource_deadline = monotonic_deadline, dict(budget)

    def _check_resource_deadline(self) -> None:
        observation = self._resource_deadline
        if observation is not None and time.monotonic_ns() >= observation[0]:
            raise RunBudgetExceeded("time", observation[1])

    @contextmanager
    def _run_resource_scope(self) -> Iterator[GlobalResourceCoordinator]:
        """Keep one adaptive budget alive through inventory and final consumers."""

        previous = self._active_coordinator
        previous_deadline = self._resource_deadline
        coordinator = self._resource_coordinator()
        with self._coordinator_lock:
            self._active_coordinator = coordinator
        try:
            with resource_scope(coordinator):
                yield coordinator
        finally:
            with self._coordinator_lock:
                self._active_coordinator = previous
            self._resource_deadline = previous_deadline

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
            self.config, root=root, state=state, run_id=run_id,
            progress=self.progress, cancellation=self._cancellation,
            reserve=lambda stage, reservation, items: self._reserve_lifecycle_stage_work(
                state, run_id, stage, reservation, items=items,
                bytes_count=0, worker="organization",
            ),
        )

    def _run_content_routes(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
    ) -> tuple[dict[str, object], GlobalResourceSummary | None]:
        self._unavailable_routes = {}
        if not self.selected_routes:
            return {}, None

        # Candidates and selection evidence have already been committed. Pin
        # that input once through the writer owner, before route/event writers
        # start, rather than copying the changing framework for every batch.
        # The context outlives executor.shutdown(wait=True), including failure
        # and cancellation, so no worker can observe an expired snapshot.
        with state.route_candidate_snapshot(
            run_id=run_id,
            cancellation_check=lambda: self._cancellation.is_cancelled,
        ) as candidate_database:
            return self._run_content_routes_with_snapshot(
                root=root,
                state=state,
                run_id=run_id,
                scan_id=scan_id,
                candidate_database=candidate_database,
            )

    def _reserve_route_work(
        self,
        *,
        state: FrameworkState,
        run_id: int,
        route_name: str,
        input_source: str,
        context: RouteExecutionContext | None = None,
        stage: str | None = None,
    ) -> dict[str, object]:
        """Consume one global route reservation before a worker starts.

        Candidate-backed routes have an exact durable item/byte workload.
        """

        read_budget = getattr(state, "read_run_budget", None)
        if not callable(read_budget) or read_budget(run_id) is None:
            # Legacy/test-created runs may predate lifecycle manifests; keep
            # their route snapshot behavior compatible until a new run opens
            # through the manifest-publishing path.
            return {}
        items = bytes_count = 0
        adapter = self.route_registry[route_name]
        if adapter.estimate_workload is not None and context is not None:
            items, bytes_count = adapter.estimate_workload(context)
        elif input_source == "route_candidates":
            items, bytes_count = state.route_candidate_workload(run_id)
        if (
            type(items) is not int
            or items < 0
            or type(bytes_count) is not int
            or bytes_count < 0
        ):
            raise RuntimeError(f"route {route_name} returned an invalid workload estimate")
        # The manifest is published before routes begin, so this call is also
        # the first live assertion that the durable baseline is available.
        state.check_run_budget(run_id)
        reservation_id = f"route:{route_name}"
        if stage is not None:
            reserve_stage = getattr(state, "reserve_run_stage", None)
            if callable(reserve_stage):
                typed_reserve_stage = cast(Callable[..., dict[str, object]], reserve_stage)
                return typed_reserve_stage(
                    run_id,
                    stage,
                    reservation_id,
                    items=items,
                    bytes=bytes_count,
                    worker=route_name,
                )
        if stage is not None:
            return state.reserve_run_budget(
                run_id,
                reservation_id,
                items=items,
                bytes=bytes_count,
                worker=route_name,
                stage=stage,
            )
        return state.reserve_run_budget(
            run_id,
            reservation_id,
            items=items,
            bytes=bytes_count,
            worker=route_name,
        )

    def _route_execution_stages(self) -> tuple[tuple[str, ...], ...]:
        """Return deterministic dependency waves for the selected routes."""

        selected = tuple(self.selected_routes)
        selected_set = set(selected)
        unavailable = tuple(
            sorted(
                {
                    dependency
                    for name in selected
                    for dependency in self.route_registry[name].depends_on
                    if dependency not in self.route_registry
                }
            )
        )
        if unavailable:
            raise ValueError(
                "route dependency is unavailable: " + ", ".join(unavailable)
            )
        remaining = set(selected)
        completed: set[str] = set()
        stages: list[tuple[str, ...]] = []
        while remaining:
            ready = tuple(
                name
                for name in selected
                if name in remaining
                and all(
                    dependency not in selected_set or dependency in completed
                    for dependency in self.route_registry[name].depends_on
                )
            )
            if not ready:
                unresolved = ", ".join(sorted(remaining))
                raise ValueError(f"route dependency cycle or unavailable stage: {unresolved}")
            stages.append(ready)
            completed.update(ready)
            remaining.difference_update(ready)
        return tuple(stages)

    def _run_content_routes_with_snapshot(
        self,
        *,
        root: Path,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        candidate_database: Path | None,
    ) -> tuple[dict[str, object], GlobalResourceSummary | None]:
        coordinator: GlobalResourceCoordinator | None = None
        previous_coordinator = self._active_coordinator
        executor: ThreadPoolExecutor | None = None
        interrupted = False
        futures: dict[Future[tuple[object, int]], str] = {}
        try:
            state.set_run_phase(run_id, "routes")
            coordinator = self._resource_coordinator()
            coordinator.start()
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
            route_input_sources = {
                name: self.route_registry[name].input_source for name in self.selected_routes
            }
            try:
                state.begin_route_runs(
                    run_id,
                    self.selected_routes,
                    route_input_sources=route_input_sources,
                )
            except TypeError as exc:
                # Small test doubles and legacy state adapters predate the
                # optional immutable source map; do not hide unrelated errors.
                if "route_input_sources" not in str(exc):
                    raise
                state.begin_route_runs(run_id, self.selected_routes)

            source_publications = {name: threading.Event() for name in self.selected_routes}

            def source_published(route_name: str) -> None:
                source_publications[route_name].set()

            def route_context(route_name: str) -> RouteExecutionContext:
                return RouteExecutionContext(
                    config=self.config,
                    root=root,
                    framework_state=FrameworkRouteState(
                        self.config.framework_database,
                        candidate_database=candidate_database,
                        resume_source_run_id=self.config.resume_run_id,
                    ),
                    run_id=run_id,
                    scan_id=scan_id,
                    progress=self._coordinated_progress,
                    resource_coordinator=coordinator,
                    cancellation=self._cancellation,
                    source_published=source_published,
                )

            def execute_route(route_name: str) -> tuple[object, int]:
                adapter = self.route_registry[route_name]
                context = route_context(route_name)
                started = time.perf_counter_ns()
                summary = adapter.execute(context)
                return summary, time.perf_counter_ns() - started

            results: dict[str, object] = {}
            failures: dict[str, BaseException] = {}
            # Validate the complete selected DAG before starting any worker.
            # Dispatch below follows individual terminal dependencies, rather
            # than imposing a barrier on unrelated routes in the same wave.
            self._route_execution_stages()
            executor = ThreadPoolExecutor(
                max_workers=len(self.selected_routes),
                thread_name_prefix="neocortex-route",
            )

            remaining = set(self.selected_routes)
            settled: set[str] = set()
            selected = set(self.selected_routes)
            pending: set[Future[tuple[object, int]]] = set()

            def drain_ready_routes() -> None:
                nonlocal pending
                assert executor is not None
                while pending or remaining:
                    if self._cancellation.is_cancelled:
                        raise KeyboardInterrupt
                    for route_name in self.selected_routes:
                        if route_name not in remaining:
                            continue
                        adapter = self.route_registry[route_name]
                        if any(
                            dependency in selected and dependency not in settled
                            and not (
                                route_name == "video" and dependency == "audio"
                                and source_publications[dependency].is_set()
                            )
                            for dependency in adapter.depends_on
                        ):
                            continue
                        self._reserve_route_work(
                            state=state,
                            run_id=run_id,
                            route_name=route_name,
                            input_source=adapter.input_source,
                            context=route_context(route_name),
                            stage="routes",
                        )
                        future = executor.submit(copy_context().run, execute_route, route_name)
                        futures[future] = route_name
                        pending.add(future)
                        remaining.remove(route_name)
                    if not pending:
                        raise ValueError("route dependency scheduler has no ready work")
                    completed, pending = wait(
                        pending,
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )
                    # A deadline or durable cancellation is checked by the
                    # foreground owner as well as route-local cooperative checks.
                    read_budget = getattr(state, "read_run_budget", None)
                    if callable(read_budget) and read_budget(run_id) is not None:
                        state.check_run_budget(run_id)
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
                        except BaseException as exc:
                            # KeyboardInterrupt is the run-wide cancellation
                            # signal and must reach the outer handler. Other
                            # BaseException subclasses still belong to this route:
                            # persist the failure before aggregating it, otherwise
                            # the durable route run remains ``running``.
                            if isinstance(exc, KeyboardInterrupt):
                                raise
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
                        # Dependencies are optional producer ordering hints.
                        # A typed failure is terminal too, but is recorded
                        # before any consumer can observe the producer.
                        settled.add(route_name)

            drain_ready_routes()
        except KeyboardInterrupt:
            interrupted = True
            self.request_cancellation()
            for future in futures:
                future.cancel()
            raise
        except BaseException:
            # A worker or executor setup can raise outside ``Exception``.  Do
            # not leave already-submitted routes running against a coordinator
            # that is about to be cleared.
            interrupted = bool(futures)
            if futures:
                self.request_cancellation()
                for future in futures:
                    future.cancel()
            raise
        finally:
            try:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=interrupted)
                    if interrupted:
                        for route_name in self.selected_routes:
                            self._finish_route_progress(route_name, "cancelled")
            finally:
                with self._coordinator_lock:
                    if self._active_coordinator is coordinator:
                        self._active_coordinator = previous_coordinator
                if previous_coordinator is None and coordinator is not None:
                    coordinator.close()

        resource_summary = self._complete_resource_coordination(
            state,
            run_id,
            coordinator,
        )
        self._unavailable_routes = {
            name: f"{type(exc).__name__}: {exc}"
            for name, exc in failures.items()
            if getattr(type(exc), "capability_unavailable", False) is True
        }
        if any(name not in self._unavailable_routes for name in failures):
            raise RouteExecutionError(failures)
        if self._unavailable_routes:
            publish_stage = getattr(state, "publish_run_stage", None)
            if callable(publish_stage):
                publish_stage(
                    run_id,
                    "route-capabilities",
                    "partial",
                    details={"unavailable": self._unavailable_routes},
                    idempotency_key="route-capabilities:partial",
                )
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

    def _prepare_run_contract(self, boundary: NormalInventoryBoundary) -> None:
        from neocortex.runtime.orchestration.preparation import prepare_framework_run

        self._run_preparation = prepare_framework_run(
            self.config, boundary, self.selected_routes,
            semantic_requested=self._lifecycle_stage_runner is not None,
            cancelled=lambda: self._cancellation.is_cancelled,
        )

    def _record_run_preparation(self, state: FrameworkState, run_id: int) -> None:
        report = getattr(self, "_run_preparation", None)
        if report is not None:
            state.record_event(run_id, "info", "preparation", "Preparación de la ejecución", report.to_dict())

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

        return {
            "route": self.config.route,
            "selected_routes": list(self.selected_routes),
            "route_capabilities": {
                name: self.route_registry[name].lifecycle_capability
                for name in self.selected_routes
            },
            "run_max_items": getattr(self.config, "run_max_items", None),
            "run_max_bytes": getattr(self.config, "run_max_bytes", None),
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
            "archive_max_file_bytes": self.config.archive_max_file_bytes,
            "archive_max_documents": self.config.archive_max_documents,
            "archive_retry_errors": self.config.archive_retry_errors,
            "archive_max_depth": self.config.archive_max_depth,
            "archive_max_members": self.config.archive_max_members,
            "archive_max_central_directory_bytes": (
                self.config.archive_max_central_directory_bytes
            ),
            "archive_max_member_bytes": self.config.archive_max_member_bytes,
            "archive_max_total_uncompressed_bytes": (
                self.config.archive_max_total_uncompressed_bytes
            ),
            "archive_max_text_chars": self.config.archive_max_text_chars,
            "archive_max_total_text_chars": self.config.archive_max_total_text_chars,
            "archive_max_compression_ratio": self.config.archive_max_compression_ratio,
            "archive_pdf_max_pages": self.config.archive_pdf_max_pages,
            "archive_pdf_timeout_seconds": self.config.archive_pdf_timeout_seconds,
            "archive_pdf_worker_memory_bytes": self.config.archive_pdf_worker_memory_bytes,
            "archive_ocr_mode": self.config.archive_ocr_mode,
            "archive_ocr_lang": self.config.archive_ocr_lang,
            "archive_ocr_dpi": self.config.archive_ocr_dpi,
            "archive_ocr_max_pages": self.config.archive_ocr_max_pages,
            "archive_ocr_max_render_pixels": self.config.archive_ocr_max_render_pixels,
            "archive_ocr_timeout_seconds": self.config.archive_ocr_timeout_seconds,
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
            if self.config.document_catalog_enabled and ORGANIZABLE_ROUTE_NAMES.intersection(self.selected_routes):
                from .organization_lifecycle import register_organization_stages

                register_organization_stages(state, run_id, self.config, boundary.access_policy.root)
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
    ) -> PreparedInventory:
        state.set_run_phase(run_id, "inventory")
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
            InventoryWorkBudget, MAX_SCAN_BYTES, MAX_SCAN_FILES,
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
                    0.0, (int(durable_budget["deadline_ns"]) - time.time_ns()) / 1e9,
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
            max_files=maximum_files, max_bytes=maximum_bytes,
            deadline_monotonic=deadline, cancellation_check=inventory_checkpoint,
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
                state, run_id, "actions", key,
                items=items, bytes_count=bytes_count, worker="corpus-curation",
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
            dict(getattr(self, "_unavailable_routes", {})),
        )

    @staticmethod
    def _initial_journal_after(inventory: PreparedInventory) -> None:
        """Portable Linux inventory has no journal successor cursor."""

        del inventory
        return None

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
            name: cls._bounded_scratch_count(
                cls._scratch_plan_value(plan, name, f"{name}_records")
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

        scratch_root = (
            Path(self.config.state_directory)
            / "scratch"
            / self._SCRATCH_SCOPE
        )
        apply_requested = bool(getattr(self.config, "apply_actions", False))
        try:
            from neocortex.runtime.orchestration.maintenance import (
                MaintenanceBlocked, MaintenanceRequest, ScopeAuthority, configured_scratch_maintenance,
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
            configured_limits = any(getattr(self.config, key, None) is not None for key in (
                "run_max_items", "run_max_bytes", "run_time_budget_seconds",
            ))
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
                    if (type(entries) is not int or type(byte_count) is not int
                            or entries < reserved_entries or byte_count < reserved_bytes):
                        raise MaintenanceBlocked("maintenance_verification_budget_unavailable")
                    # Verification spends the same run budget as planning.
                    # Admission of its delta occurs before the owner's effect.
                    state.reserve_run_budget(
                        run_id, "maintenance-verification:" + self._SCRATCH_SCOPE + ":"
                        + str(payload["fingerprint"]),
                        items=entries - reserved_entries, bytes=byte_count - reserved_bytes,
                        worker="maintenance", stage="maintenance",
                    )
                    reserved_entries, reserved_bytes = entries, byte_count
                # Unlike the optional display event below, this durable receipt
                # is required before/after the owner's effect.
                state.record_event(run_id, "info", "maintenance-receipt",
                                   "Recibo de mantenimiento", dict(payload))

            coordinator = configured_scratch_maintenance(
                Path(self.config.state_directory), owner=self._SCRATCH_OWNER,
                scopes=(self._SCRATCH_SCOPE,), record_outcome=record_outcome,
            )
            request = MaintenanceRequest(
                scopes=(self._SCRATCH_SCOPE,), apply_requested=apply_requested,
                authorities=(ScopeAuthority(self._SCRATCH_SCOPE, "configured-run-apply"),)
                if apply_requested else (),
                max_entries=limits["max_entries"], max_bytes=limits["max_bytes"],
                deadline_ns=deadline_ns,
                cancelled=lambda: self._cancellation.is_cancelled,
            )
            planned = coordinator.plan(request)
            if live_budget is not None:
                reserve = getattr(state, "reserve_run_budget", None)
                if not callable(reserve):
                    raise MaintenanceBlocked("run_budget_reservation_owner_unavailable")
                fingerprint = planned.component_fingerprints.get(self._SCRATCH_SCOPE, "empty")
                reserve(run_id, "maintenance:" + self._SCRATCH_SCOPE + ":" + fingerprint,
                        items=planned.observed_entries, bytes=planned.observed_bytes,
                        worker="maintenance", stage="maintenance")
                reserved_entries, reserved_bytes = planned.observed_entries, planned.observed_bytes
            outcome = coordinator.execute(
                planned, primary_work_status=primary_work_status,
            )
            owner_plan = planned.component_plans.get(self._SCRATCH_SCOPE, {})
            details = self._scratch_maintenance_details(
                owner_plan, apply_requested=False, root=scratch_root,
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
                    "operation_status": "partial", "primary_work_status": primary_work_status,
                    "maintenance_status": "partial", "requested_scopes": [self._SCRATCH_SCOPE],
                    "blocked_scopes": {self._SCRATCH_SCOPE: type(exc).__name__ + ":" + str(exc)[:512]},
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
            state, run_id, primary_work_status="partial" if work.route_failures else "complete",
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
            "Ejecución incompleta: capacidades no disponibles" if work.route_failures else "Ejecución completada",
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
            with FrameworkState(self.config.framework_database) as state:
                self._finalize_initial_run(
                    state,
                    run_id,
                    boundary,
                    work,
                    journal_after,
                )
            return
        stage_heartbeat = RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()
        try:
            runner(run_id)
        except KeyboardInterrupt as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except RunBudgetExceeded as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=False)
            raise
        finally:
            stage_heartbeat.stop()
        try:
            with FrameworkState(self.config.framework_database) as state:
                self._finalize_initial_run(
                    state,
                    run_id,
                    boundary,
                    work,
                    journal_after,
                )
        except KeyboardInterrupt as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except RunBudgetExceeded as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._persist_initial_termination(state, run_id, exc, cancelled=True)
            raise
        except BaseException as exc:
            with FrameworkState(self.config.framework_database) as state:
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
        if not self.selected_routes and not semantic_only_resume and not self._organization_resume_pending:
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
                self._run_document_organization(root=boundary.access_policy.root, state=state, run_id=run_id)
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
            with FrameworkState(self.config.framework_database) as state:
                self._complete_route_only_run(state, boundary, source, run_id)
            return
        stage_heartbeat = RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()
        try:
            runner(run_id)
        except KeyboardInterrupt as exc:
            with FrameworkState(self.config.framework_database) as state:
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
            with FrameworkState(self.config.framework_database) as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        except BaseException as exc:
            with FrameworkState(self.config.framework_database) as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        finally:
            stage_heartbeat.stop()
        try:
            with FrameworkState(self.config.framework_database) as state:
                self._complete_route_only_run(state, boundary, source, run_id)
        except KeyboardInterrupt as exc:
            with FrameworkState(self.config.framework_database) as state:
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
            with FrameworkState(self.config.framework_database) as state:
                self._fail_route_only_run(state, source, run_id, exc)
            raise
        except BaseException as exc:
            with FrameworkState(self.config.framework_database) as state:
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


# endregion [02]
