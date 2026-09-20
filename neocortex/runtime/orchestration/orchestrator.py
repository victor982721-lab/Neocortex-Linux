"""Coordination of filesystem checkpoints and pre-index deduplication.

Canonical route registry targets remain owned by their product namespaces:
``neocortex.capabilities.formats.audio``,
``neocortex.capabilities.formats.archive``,
``neocortex.capabilities.formats.docx``,
``neocortex.capabilities.formats.image``,
``neocortex.capabilities.formats.office``,
``neocortex.capabilities.formats.pdf``,
``neocortex.capabilities.formats.text``,
``neocortex.capabilities.formats.video``, and ``neocortex.documents``.
Extracted pipeline slices import them through the route registry without
eagerly loading implementations.
"""
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from typing import cast

from neocortex.deduplication.inventory.index import validate_inventory_root
from neocortex.progress import NullProgress, ProgressCallback, ProgressEvent
from neocortex.runtime.config.application_config_projections import (
    global_resource_limits_from_application,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    resource_scope,
)
from neocortex.integrations.inventory.inventory_boundary import (
    NormalInventoryBoundary,  # noqa: F401 - historical module export
    build_normal_inventory_boundary,
    initialize_authorized_state_directory,  # noqa: F401 - historical module export
)
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    builtin_route_registry,
    normalize_route_selection,
)
from neocortex.runtime.orchestration.orchestrator_finalization import InitialFinalizationMixin
from neocortex.runtime.orchestration.orchestrator_lifecycle import RouteLifecycleMixin
from neocortex.runtime.orchestration.orchestrator_pipeline import InitialPipelineMixin
from neocortex.runtime.orchestration.orchestrator_routes import RouteExecutionMixin
from neocortex.runtime.orchestration.orchestrator_types import (
    RouteExecutionError as _RouteExecutionError,
)
from neocortex.runtime.orchestration.run_lifecycle import RunHeartbeat
from neocortex.runtime.orchestration.run_manifest import RunBudget
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded

RouteExecutionError = _RouteExecutionError


# endregion [01]

# region [02] Implementación


class FrameworkOrchestrator(
    InitialFinalizationMixin,
    RouteLifecycleMixin,
    InitialPipelineMixin,
    RouteExecutionMixin,
):
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

    def _framework_state(self) -> FrameworkState:
        """Create the single Framework persistence owner for a lifecycle slice."""

        return FrameworkState(self.config.framework_database)

    def _start_run_heartbeat(self, run_id: int) -> RunHeartbeat:
        """Start a heartbeat through this module's patchable compatibility seam."""

        return RunHeartbeat(
            self.config.framework_database,
            run_id,
            interval_seconds=self.config.heartbeat_interval_seconds,
        ).start()

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
        stages = tuple(
            dict.fromkeys(
                (
                    "inventory",
                    "dedup",
                    *self.selected_routes,
                    "catalog",
                    "semantic",
                    "knowledge",
                    "preparation",
                )
            )
        )
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


# endregion [02]
