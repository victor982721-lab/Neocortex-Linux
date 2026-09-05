"""Fair, adaptive resource coordination shared by concurrent content routes."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from .cancellation import CancellationRequested, CancellationToken
from .cpu_runtime import CpuLoadSampler, effective_cpu_count
from .memory_runtime import (
    MemoryBudgetExceeded,
    MemoryHeadroomTimeout,
    memory_snapshot,
)


# region [01] Configuration and observable summaries

MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True, slots=True)
class GlobalResourceLimits:
    memory_budget_bytes: int | None = None
    min_free_memory_bytes: int | None = None
    min_free_commit_bytes: int | None = None
    cpu_slots: int | None = None
    max_cpu_load_percent: float = 90.0
    wait_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 0.25


@dataclass(frozen=True, slots=True)
class RouteResourceSummary:
    admissions: int
    waits: int
    wait_seconds: float
    peak_reserved_bytes: int
    peak_cpu_slots: int


@dataclass(frozen=True, slots=True)
class GlobalResourceSummary:
    memory_budget_bytes: int
    min_free_memory_bytes: int
    min_free_commit_bytes: int
    cpu_slots: int
    max_cpu_load_percent: float
    peak_reserved_bytes: int
    peak_cpu_slots: int
    peak_active_requests: int
    min_observed_available_memory_bytes: int | None
    min_observed_available_commit_bytes: int | None
    max_observed_cpu_load_percent: float | None
    min_effective_cpu_slots: int
    routes: dict[str, RouteResourceSummary]


@dataclass(slots=True)
class _MutableRouteMetrics:
    admissions: int = 0
    waits: int = 0
    wait_seconds: float = 0.0
    reserved_bytes: int = 0
    cpu_slots: int = 0
    active_requests: int = 0
    peak_reserved_bytes: int = 0
    peak_cpu_slots: int = 0


@dataclass(slots=True)
class _Request:
    route_name: str
    memory_bytes: int
    cpu_slots: int
    enqueued_at: float
    waited: bool = False


def _adaptive_memory_budget(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return GIB
    # Live admission still preserves physical and commit headroom.  A quarter
    # of RAM under-admitted the measured PDF workload on 12-16 GiB hosts: four
    # bounded workers collapsed to two while CPU stayed mostly idle.  Permit a
    # larger aggregate reservation, then let _fits reduce concurrency when the
    # processes actually consume that headroom.
    return max(GIB, min(5 * GIB, total_physical * 3 // 8))


def _adaptive_memory_headroom(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return GIB
    return max(GIB, min(3 * GIB, total_physical // 6))


def _adaptive_cpu_slots() -> int:
    detected = effective_cpu_count()
    return max(1, min(8, detected - 1))


# endregion [01]


# region [02] Fair live admission


class GlobalResourceCoordinator:
    """Distribute memory and CPU across route queues using round-robin fairness."""

    def __init__(
        self,
        route_order: tuple[str, ...],
        limits: GlobalResourceLimits,
        *,
        cpu_load_probe: Callable[[], float | None] | None = None,
        cancellation: CancellationToken | None = None,
    ):
        if not route_order or len(route_order) != len(set(route_order)):
            raise ValueError("route_order must contain unique route names")
        if limits.wait_timeout_seconds < 0:
            raise ValueError("global resource wait timeout cannot be negative")
        if limits.poll_interval_seconds <= 0:
            raise ValueError("global resource poll interval must be positive")
        if not 0 < limits.max_cpu_load_percent <= 100:
            raise ValueError("global maximum CPU load must be in (0, 100]")

        snapshot = memory_snapshot()
        automatic_budget = _adaptive_memory_budget(snapshot.total_physical)
        automatic_headroom = _adaptive_memory_headroom(snapshot.total_physical)
        self.memory_budget_bytes = (
            automatic_budget
            if limits.memory_budget_bytes is None
            else limits.memory_budget_bytes
        )
        self.min_free_memory_bytes = (
            automatic_headroom
            if limits.min_free_memory_bytes is None
            else limits.min_free_memory_bytes
        )
        self.min_free_commit_bytes = (
            automatic_headroom
            if limits.min_free_commit_bytes is None
            else limits.min_free_commit_bytes
        )
        self.cpu_slots = (
            _adaptive_cpu_slots() if limits.cpu_slots is None else limits.cpu_slots
        )
        if self.memory_budget_bytes < 1:
            raise ValueError("global memory budget must be positive")
        if self.min_free_memory_bytes < 0 or self.min_free_commit_bytes < 0:
            raise ValueError("global memory headroom cannot be negative")
        if self.cpu_slots < 1:
            raise ValueError("global CPU slots must be positive")

        self.limits = limits
        self.cancellation = cancellation or CancellationToken()
        self.max_cpu_load_percent = float(limits.max_cpu_load_percent)
        sampler = CpuLoadSampler()
        self._cpu_load_probe = cpu_load_probe or sampler.sample
        self.route_order = route_order
        self._condition = threading.Condition()
        self._queues: dict[str, deque[_Request]] = {
            name: deque() for name in route_order
        }
        self._metrics = {name: _MutableRouteMetrics() for name in route_order}
        self._last_granted_index = -1
        self._reserved_bytes = 0
        self._cpu_in_use = 0
        self._active_requests = 0
        self._peak_reserved_bytes = 0
        self._peak_cpu_slots = 0
        self._peak_active_requests = 0
        self._min_available_memory: int | None = None
        self._min_available_commit: int | None = None
        self._max_cpu_load: float | None = None
        self._min_effective_cpu_slots = self.cpu_slots
        self._last_cpu_load: float | None = None
        self._last_effective_cpu_slots = self.cpu_slots

    def cancel(self) -> None:
        """Wake all queued admissions so cancellation is observed immediately."""

        self.cancellation.cancel()
        with self._condition:
            self._condition.notify_all()

    def _observe_live_resources(self):
        snapshot = memory_snapshot()
        if snapshot.available_physical is not None:
            self._min_available_memory = (
                snapshot.available_physical
                if self._min_available_memory is None
                else min(self._min_available_memory, snapshot.available_physical)
            )
        if snapshot.available_commit is not None:
            self._min_available_commit = (
                snapshot.available_commit
                if self._min_available_commit is None
                else min(self._min_available_commit, snapshot.available_commit)
            )
        try:
            cpu_load = self._cpu_load_probe()
        except (OSError, RuntimeError, ValueError):
            cpu_load = None
        if cpu_load is not None:
            cpu_load = max(0.0, min(100.0, float(cpu_load)))
            self._max_cpu_load = (
                cpu_load
                if self._max_cpu_load is None
                else max(self._max_cpu_load, cpu_load)
            )
        effective_cpu_slots = self._effective_cpu_capacity(cpu_load)
        self._min_effective_cpu_slots = min(
            self._min_effective_cpu_slots, effective_cpu_slots
        )
        self._last_cpu_load = cpu_load
        self._last_effective_cpu_slots = effective_cpu_slots
        return snapshot, effective_cpu_slots

    def _effective_cpu_capacity(self, load_percent: float | None) -> int:
        if load_percent is None:
            return self.cpu_slots
        remaining = max(0.0, self.max_cpu_load_percent - load_percent)
        capacity = math.ceil(self.cpu_slots * remaining / self.max_cpu_load_percent)
        return max(1, min(self.cpu_slots, capacity))

    def _fits(self, request: _Request, snapshot, effective_cpu_slots: int) -> bool:
        future_memory = self._reserved_bytes + request.memory_bytes
        if future_memory > self.memory_budget_bytes:
            return False
        if self._cpu_in_use + request.cpu_slots > effective_cpu_slots:
            return False
        physical_ok = (
            snapshot.available_physical is None
            or snapshot.available_physical >= self.min_free_memory_bytes + future_memory
        )
        commit_ok = (
            snapshot.available_commit is None
            or snapshot.available_commit >= self.min_free_commit_bytes + future_memory
        )
        return physical_ok and commit_ok

    def _next_route(self, snapshot, effective_cpu_slots: int) -> str | None:
        route_count = len(self.route_order)
        ordered_routes = tuple(
            self.route_order[(self._last_granted_index + offset) % route_count]
            for offset in range(1, route_count + 1)
        )
        fitting_routes = tuple(
            route_name
            for route_name in ordered_routes
            if self._queues[route_name]
            and self._fits(self._queues[route_name][0], snapshot, effective_cpu_slots)
        )
        # A route that already owns resources must not reacquire the last
        # available capacity ahead of a fitting route that has received none.
        # This prevents a stream of large PDF jobs from starving DOCX/images.
        for route_name in fitting_routes:
            if self._metrics[route_name].cpu_slots == 0:
                return route_name
        if fitting_routes:
            return fitting_routes[0]
        return None

    @contextmanager
    def admit(self, route_name: str, memory_bytes: int, cpu_slots: int = 1):
        self.cancellation.checkpoint()
        if route_name not in self._queues:
            raise ValueError(f"route is not coordinated: {route_name}")
        requested_memory = max(1, int(memory_bytes))
        requested_cpu = max(1, int(cpu_slots))
        if requested_memory > self.memory_budget_bytes:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_memory} bytes but the global "
                f"budget is {self.memory_budget_bytes} bytes"
            )
        if requested_cpu > self.cpu_slots:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_cpu} CPU slots but only "
                f"{self.cpu_slots} are configured"
            )

        started = time.monotonic()
        request = _Request(route_name, requested_memory, requested_cpu, started)
        route_index = self.route_order.index(route_name)
        acquired = False
        headroom_blocked_since: float | None = None
        with self._condition:
            self._queues[route_name].append(request)
            self._condition.notify_all()
            while True:
                if self.cancellation.is_cancelled:
                    self._queues[route_name].remove(request)
                    if request.waited:
                        self._metrics[route_name].wait_seconds += (
                            time.monotonic() - started
                        )
                    self._condition.notify_all()
                    raise CancellationRequested(
                        f"{route_name} cancelled while waiting for global resources"
                    )
                snapshot, effective_cpu_slots = self._observe_live_resources()
                selected_route = self._next_route(snapshot, effective_cpu_slots)
                if (
                    selected_route == route_name
                    and self._queues[route_name]
                    and self._queues[route_name][0] is request
                ):
                    self._queues[route_name].popleft()
                    self._last_granted_index = route_index
                    self._reserved_bytes += requested_memory
                    self._cpu_in_use += requested_cpu
                    self._active_requests += 1
                    metrics = self._metrics[route_name]
                    metrics.admissions += 1
                    metrics.reserved_bytes += requested_memory
                    metrics.cpu_slots += requested_cpu
                    metrics.active_requests += 1
                    metrics.peak_reserved_bytes = max(
                        metrics.peak_reserved_bytes, metrics.reserved_bytes
                    )
                    metrics.peak_cpu_slots = max(
                        metrics.peak_cpu_slots, metrics.cpu_slots
                    )
                    if request.waited:
                        metrics.wait_seconds += time.monotonic() - started
                    self._peak_reserved_bytes = max(
                        self._peak_reserved_bytes, self._reserved_bytes
                    )
                    self._peak_cpu_slots = max(self._peak_cpu_slots, self._cpu_in_use)
                    self._peak_active_requests = max(
                        self._peak_active_requests, self._active_requests
                    )
                    acquired = True
                    self._condition.notify_all()
                    break

                if not request.waited:
                    request.waited = True
                    self._metrics[route_name].waits += 1

                # Active bounded jobs own their reservations legitimately. Their
                # document/worker supervisors enforce the work deadlines, so a
                # queue wait caused only by internal contention must not be
                # mislabeled as a system-memory failure. Apply this timeout only
                # while no work is active and live physical/commit headroom is
                # the reason that no queued request can start.
                remaining: float | None = None
                if selected_route is None and self._active_requests == 0:
                    now = time.monotonic()
                    if headroom_blocked_since is None:
                        headroom_blocked_since = now
                    remaining = self.limits.wait_timeout_seconds - (
                        now - headroom_blocked_since
                    )
                    if remaining <= 0:
                        self._queues[route_name].remove(request)
                        self._metrics[route_name].wait_seconds += now - started
                        self._condition.notify_all()
                        raise MemoryHeadroomTimeout(
                            f"{route_name} timed out waiting for live system "
                            f"headroom; available_physical="
                            f"{snapshot.available_physical}, "
                            f"available_commit={snapshot.available_commit}, "
                            f"reserved={self._reserved_bytes}, "
                            f"cpu_in_use={self._cpu_in_use}, "
                            f"cpu_load={self._last_cpu_load}, "
                            f"effective_cpu_slots="
                            f"{self._last_effective_cpu_slots}"
                        )
                else:
                    headroom_blocked_since = None
                wait_seconds = self.limits.poll_interval_seconds
                if remaining is not None:
                    wait_seconds = min(wait_seconds, remaining)
                self._condition.wait(wait_seconds)

        try:
            yield
        finally:
            if acquired:
                with self._condition:
                    self._reserved_bytes -= requested_memory
                    self._cpu_in_use -= requested_cpu
                    self._active_requests -= 1
                    metrics = self._metrics[route_name]
                    metrics.reserved_bytes -= requested_memory
                    metrics.cpu_slots -= requested_cpu
                    metrics.active_requests -= 1
                    self._condition.notify_all()

    def route_peak_reserved_bytes(self, route_name: str) -> int:
        with self._condition:
            return self._metrics[route_name].peak_reserved_bytes

    def route_wait_count(self, route_name: str) -> int:
        with self._condition:
            return self._metrics[route_name].waits

    def route_active_request_count(self, route_name: str) -> int:
        """Return currently admitted jobs, excluding queued requests."""

        with self._condition:
            return self._metrics[route_name].active_requests

    def summary(self) -> GlobalResourceSummary:
        with self._condition:
            routes = {
                name: RouteResourceSummary(
                    admissions=metrics.admissions,
                    waits=metrics.waits,
                    wait_seconds=round(metrics.wait_seconds, 6),
                    peak_reserved_bytes=metrics.peak_reserved_bytes,
                    peak_cpu_slots=metrics.peak_cpu_slots,
                )
                for name, metrics in self._metrics.items()
            }
            return GlobalResourceSummary(
                memory_budget_bytes=self.memory_budget_bytes,
                min_free_memory_bytes=self.min_free_memory_bytes,
                min_free_commit_bytes=self.min_free_commit_bytes,
                cpu_slots=self.cpu_slots,
                max_cpu_load_percent=self.max_cpu_load_percent,
                peak_reserved_bytes=self._peak_reserved_bytes,
                peak_cpu_slots=self._peak_cpu_slots,
                peak_active_requests=self._peak_active_requests,
                min_observed_available_memory_bytes=self._min_available_memory,
                min_observed_available_commit_bytes=self._min_available_commit,
                max_observed_cpu_load_percent=(
                    None if self._max_cpu_load is None else round(self._max_cpu_load, 3)
                ),
                min_effective_cpu_slots=self._min_effective_cpu_slots,
                routes=routes,
            )


# endregion [02]


# region [03] Route memory gate adapter


class CoordinatedMemoryGate:
    def __init__(self, coordinator: GlobalResourceCoordinator, route_name: str):
        self.coordinator = coordinator
        self.route_name = route_name

    @property
    def peak_reserved_bytes(self) -> int:
        return self.coordinator.route_peak_reserved_bytes(self.route_name)

    @property
    def wait_count(self) -> int:
        return self.coordinator.route_wait_count(self.route_name)

    @contextmanager
    def admit(self, estimated_bytes: int):
        with self.coordinator.admit(self.route_name, estimated_bytes, 1):
            yield
# endregion [03]
