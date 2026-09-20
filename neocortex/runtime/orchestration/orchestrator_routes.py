"""Bounded route scheduling and route-owner coordination.

This module owns the route execution mechanics while the public
``FrameworkOrchestrator`` retains the lifecycle and configuration surface.
The mixin deliberately uses only coordinator attributes and callbacks; it does
not create a second orchestration object or a second persistence owner.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, cast

from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceSummary,
)
from neocortex.runtime.orchestration.orchestrator_types import (
    RouteExecutionError,
    _FrameworkOrchestratorOwner,
)
from neocortex.runtime.orchestration.route_registry import RouteExecutionContext

if TYPE_CHECKING:
    from neocortex.progress import ProgressEvent
    from neocortex.persistence.framework_state_writer import FrameworkState


class RouteExecutionMixin(_FrameworkOrchestratorOwner):
    """Route DAG scheduling and durable route result publication."""

    if TYPE_CHECKING:
        def _resource_coordinator(self) -> GlobalResourceCoordinator: ...

        def _coordinated_progress(self, event: ProgressEvent) -> None: ...

        def _finish_route_progress(self, route_name: str, outcome: str) -> None: ...

        def request_cancellation(self) -> None: ...

    _active_coordinator: GlobalResourceCoordinator | None
    _unavailable_routes: dict[str, str]

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
        if type(items) is not int or items < 0 or type(bytes_count) is not int or bytes_count < 0:
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
            raise ValueError("route dependency is unavailable: " + ", ".join(unavailable))
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
        previous_coordinator: GlobalResourceCoordinator | None = self._active_coordinator
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
                            dependency in selected
                            and dependency not in settled
                            and not (
                                route_name == "video"
                                and dependency == "audio"
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
