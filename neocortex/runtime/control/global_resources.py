"""Fair, adaptive resource coordination shared by concurrent content routes."""

from __future__ import annotations

import math
import os  # noqa: F401 - retained as a compatibility patch surface for grants
import shutil
import stat  # noqa: F401 - historical module-level patch surface
import tempfile
import threading
import time
from contextvars import ContextVar
from collections import deque
from contextlib import contextmanager
from collections.abc import Callable, Mapping
from pathlib import Path  # noqa: F401 - compatibility patch surface for grants
from typing import Any

from .cancellation import CancellationRequested, CancellationToken  # noqa: F401
from .cpu_runtime import CpuLoadSampler, effective_cpu_count
from .memory_runtime import (
    MemoryBudgetExceeded,
    MemoryHeadroomTimeout,  # noqa: F401 - historical module-level re-export
    MemorySnapshot,
    MemoryResourceLimits,
    memory_snapshot,
)
from .resource_admission import (
    NativeThreadBudgetExceeded,
    ResourceWaitTimeout,
    admission_scope,
)
from .resource_grants import (
    _CombinedCancellationToken,  # noqa: F401
    _CURRENT_RESOURCE_GRANT,
    _DRAINING_RESOURCE_GRANT,  # noqa: F401
    ResourceGrant,
)
from .resource_scheduling import fits, next_route


# region [01] Configuration and observable summaries

from .resource_models import (
    GIB,  # noqa: F401
    MIB,
    GlobalResourceLimits,
    GlobalResourceSample,  # noqa: F401
    GlobalResourceSummary,
    ResourcePressureSample,  # noqa: F401
    ResourceSample,
    ResourceUsage,  # noqa: F401
    GlobalResourceUsage,  # noqa: F401
    RouteResourceSummary,
    _MutableRouteMetrics,
    _Request,
)
from .resource_observation import (
    adaptive_cpu_slots as _adaptive_cpu_slots_impl,
    adaptive_memory_budget as _adaptive_memory_budget,
    adaptive_memory_headroom as _adaptive_memory_headroom,
    coerce_resource_sample as _coerce_resource_sample,
    linux_memory_pressure_sample as _linux_memory_pressure_sample,
    memory_snapshot_from_resource_sample as _memory_snapshot_from_resource_sample,
    observe_live_resources,
    update_pressure_locked,
)
from .resource_observation import _PROC_MEMORY_PRESSURE  # noqa: F401

# These records were historically defined in this module. Keep their pickle
# identity stable while their definitions live in the data-contract module.
for _legacy_resource_type in (
    GlobalResourceLimits,
    ResourceSample,
    RouteResourceSummary,
    GlobalResourceSummary,
    ResourceGrant,
    ResourceWaitTimeout,
    NativeThreadBudgetExceeded,
):
    _legacy_resource_type.__module__ = __name__
del _legacy_resource_type


def _adaptive_cpu_slots() -> int:
    # Keep the facade's patchable CPU probe as part of the compatibility
    # surface; the pure helper receives the probe explicitly.
    return _adaptive_cpu_slots_impl(effective_cpu_count)


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
        resource_probe: Callable[[], object] | None = None,
        effective_cpu_probe: Callable[[], int] | None = None,
        clock: Callable[[], float] | None = None,
        checkpoint: Callable[[], None] | None = None,
        route_memory_budgets: Mapping[str, int] | None = None,
    ):
        if not route_order or len(route_order) != len(set(route_order)):
            raise ValueError("route_order must contain unique route names")
        if limits.wait_timeout_seconds is not None and limits.wait_timeout_seconds < 0:
            raise ValueError("global resource wait timeout cannot be negative")
        if limits.poll_interval_seconds <= 0:
            raise ValueError("global resource poll interval must be positive")
        if limits.sample_interval_seconds <= 0:
            raise ValueError("global resource sample interval must be positive")
        if not 0 < limits.max_cpu_load_percent <= 100:
            raise ValueError("global maximum CPU load must be in (0, 100]")
        if limits.cpu_hysteresis_percent < 0:
            raise ValueError("global CPU hysteresis cannot be negative")
        if not 0 <= limits.memory_pressure_recovery_percent <= limits.memory_pressure_high_percent:
            raise ValueError(
                "global memory pressure recovery must not exceed the high threshold"
            )
        if limits.memory_hysteresis_bytes is not None and limits.memory_hysteresis_bytes < 0:
            raise ValueError("global memory hysteresis cannot be negative")
        if limits.native_thread_slots is not None and limits.native_thread_slots < 1:
            raise ValueError("global native thread slots must be positive")
        if limits.temp_budget_bytes is not None and limits.temp_budget_bytes < 1:
            raise ValueError("global temporary resource budget must be positive")
        if limits.io_slots is not None and limits.io_slots < 1:
            raise ValueError("global I/O concurrency must be positive")
        if any(value < 1 for value in (limits.io_device_slots or {}).values()):
            raise ValueError("device I/O concurrency must be positive")
        if any(value < 1 for value in (limits.gpu_memory_bytes or {}).values()):
            raise ValueError("GPU memory capacities must be positive")
        if not 0 <= limits.io_pressure_recovery_percent <= limits.io_pressure_high_percent:
            raise ValueError("I/O recovery threshold must not exceed pressure threshold")
        self._route_memory_budgets: dict[str, int] = {}
        for name, budget in (route_memory_budgets or {}).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("route memory budgets require nonempty route names")
            if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
                raise ValueError("route memory budgets must be positive integers")
            name = name.strip()
            if name in self._route_memory_budgets:
                raise ValueError("route memory budgets require unique route names")
            self._route_memory_budgets[name] = budget

        self._resource_probe = resource_probe
        self._default_sampler: Any | None = None
        self._last_owned_snapshot: Any | None = None
        self._clock = clock or time.monotonic
        self._checkpoint_callback = checkpoint
        self._effective_cpu_probe = effective_cpu_probe or effective_cpu_count
        initial_sample = self._read_resource_sample()
        snapshot = _memory_snapshot_from_resource_sample(initial_sample) or memory_snapshot()
        automatic_budget = _adaptive_memory_budget(snapshot.total_physical)
        automatic_headroom = _adaptive_memory_headroom(snapshot.total_physical)
        self._automatic_memory_budget = limits.memory_budget_bytes is None
        self._automatic_cpu_slots = limits.cpu_slots is None
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
        self._using_default_cpu_probe = cpu_load_probe is None
        self._cpu_load_probe = cpu_load_probe or sampler.sample
        self.native_thread_slots = (
            self.cpu_slots
            if limits.native_thread_slots is None
            else limits.native_thread_slots
        )
        self._automatic_native_thread_slots = limits.native_thread_slots is None
        self.temp_budget_bytes = (
            shutil.disk_usage(tempfile.gettempdir()).free
            if limits.temp_budget_bytes is None else limits.temp_budget_bytes
        )
        self._memory_hysteresis_bytes = (
            max(1, min(256 * MIB, automatic_headroom // 10))
            if limits.memory_hysteresis_bytes is None
            else limits.memory_hysteresis_bytes
        )
        self.route_order = route_order
        self._condition = threading.Condition()
        self._queues: dict[str, deque[_Request]] = {
            name: deque() for name in route_order
        }
        self._metrics = {name: _MutableRouteMetrics() for name in route_order}
        self._last_granted_index = -1
        self._reserved_bytes = 0
        self._resident_bytes = 0
        self._transient_bytes = 0
        self._temp_bytes = 0
        self._cpu_in_use = 0
        self._native_threads_in_use = 0
        self._active_requests = 0
        self._peak_reserved_bytes = 0
        self._peak_resident_bytes = 0
        self._peak_transient_bytes = 0
        self._peak_temp_bytes = 0
        self._peak_cpu_slots = 0
        self._peak_native_threads = 0
        self._peak_active_requests = 0
        # A keyed resident allocation is charged once while any request using
        # that key is admitted.  The request's transient/temp charges remain
        # per-admission, so nested phases cannot count shared resident state
        # twice.
        self._resident_claims: dict[tuple[str, str], dict[int, int]] = {}
        self._min_available_memory: int | None = None
        self._min_available_commit: int | None = None
        self._max_cpu_load: float | None = None
        self._min_effective_cpu_slots = self.cpu_slots
        self._last_cpu_load: float | None = None
        self._last_effective_cpu_slots = self.cpu_slots
        self._last_external_cpu_cores: float | None = None
        self._last_cpu_capacity = self.cpu_slots
        self._last_memory_budget = self.memory_budget_bytes
        self._previous_sample = initial_sample or ResourceSample(
            available_physical=snapshot.available_physical,
            available_commit=snapshot.available_commit,
            total_physical=snapshot.total_physical,
            total_commit=snapshot.total_commit,
        )
        self._last_available_memory_delta: int | None = None
        self._last_available_commit_delta: int | None = None
        self._sample_count = 0
        self._pressure_events = 0
        self._last_memory_pressure_value: float | None = None
        self._last_memory_pressure_delta: float | None = None
        self._last_memory_pressure_total_delta_us: int | None = None
        self._memory_pressure = False
        self._memory_pressure_from_psi = False
        self._cpu_pressure = False
        self._last_cpu_sample_at: float | None = None
        self._active_execution_requests = 0
        self._active_reservations: dict[int, _Request] = {}
        self._io_in_use: dict[str, int] = {}
        self._gpu_in_use: dict[str, int] = {}
        self._peak_io_slots = 0
        self._peak_gpu_bytes = 0
        self._gpu_capacity = dict(limits.gpu_memory_bytes or {})
        self._gpu_available_probes: dict[str, Callable[[], int | None]] = {}
        self._gpu_available_cache: dict[str, tuple[int | None, float]] = {}
        self._gpu_materialized_probes: dict[
            str, Callable[[], Mapping[tuple[int, int], int]]
        ] = {}
        self._gpu_materialized_cache: dict[str, Mapping[tuple[int, int], int]] = {}
        self._gpu_probe_generations: dict[str, int] = {}
        self._gpu_sample_lock = threading.Lock()
        self._gpu_monitor_thread: threading.Thread | None = None
        self._gpu_monitor_stop = threading.Event()
        self._gpu_monitor_wakeup = threading.Event()
        self._io_pressure = False
        self._materialized_credit_bytes = 0
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        if resource_probe is None and cpu_load_probe is None:
            from .resource_sampler import OwnedResourceSampler

            self._default_sampler = OwnedResourceSampler(
                sample_interval_seconds=limits.sample_interval_seconds
            )
            self._default_sampler.sample()

    def cancel(self) -> None:
        """Wake all queued admissions so cancellation is observed immediately."""

        self.cancellation.cancel()
        with self._condition:
            self._condition.notify_all()

    def checkpoint(self) -> None:
        """Check cancellation and a caller-supplied deadline in that caller.

        The callback must be pure and thread-safe; monitors never invoke it.
        It must not query SQLite, whose connection belongs to its writer.
        """
        self.cancellation.checkpoint()
        if self._checkpoint_callback is not None:
            self._checkpoint_callback()

    def register_route(self, route_name: str) -> None:
        """Join another execution phase to this scope's existing accounting."""
        name = str(route_name).strip()
        if not name:
            raise ValueError("resource route cannot be empty")
        with self._condition:
            if name not in self._queues:
                self.route_order += (name,)
                self._queues[name] = deque()
                self._metrics[name] = _MutableRouteMetrics()

    def route_memory_budget_bytes(self, route_name: str) -> int:
        """Return a route's absolute ceiling within the live shared budget.

        This is a capacity query, not another reservation or memory gate.
        Unconfigured routes keep the global automatic policy.
        """
        with self._condition:
            self._observe_live_resources()
            return min(
                self._last_memory_budget,
                self._route_memory_budgets.get(route_name, self._last_memory_budget),
            )

    def register_gpu_device(
        self, device: str, memory_bytes: int,
        *, available_probe: Callable[[], int | None] | None = None,
        materialized_probe: Callable[[], Mapping[tuple[int, int], int]] | None = None,
    ) -> None:
        """Register a backend-observed device capacity; never invent free VRAM."""
        if memory_bytes < 1:
            raise ValueError("GPU capacity must be positive")
        device = str(device)
        with self._condition:
            self._gpu_capacity[device] = int(memory_bytes)
            if available_probe is not None:
                self._gpu_available_probes[device] = available_probe
            if materialized_probe is not None:
                self._gpu_materialized_probes[device] = materialized_probe
            self._gpu_probe_generations[device] = (
                self._gpu_probe_generations.get(device, 0) + 1
            )
        # Preserve the synchronous registration contract. A healthy existing
        # sample remains usable within its normal TTL while its replacement
        # is read; an artificial unknown value here could retire live models.
        # Driver queries never hold the accounting lock or the CPU monitor.
        self._sample_gpu_resources()
        with self._condition:
            if (
                self._monitor_thread is not None
                and self._monitor_thread.is_alive()
                and not self._monitor_stop.is_set()
            ):
                self._start_gpu_monitor_locked()
            self._condition.notify_all()

    def _sample_gpu_resources(self, stop: threading.Event | None = None) -> None:
        # A restarted observer must not overlap a driver query still finishing
        # for the previous scope. Synchronous, unmonitored GPU callers wait for
        # this one producer; CPU/RAM admission never takes this lock.
        if not self._gpu_sample_lock.acquire(blocking=stop is None):
            return
        try:
            if stop is not None and stop.is_set():
                return
            with self._condition:
                probes = dict(self._gpu_available_probes)
                materialized_probes = dict(self._gpu_materialized_probes)
                generations = dict(self._gpu_probe_generations)
            observed: dict[str, tuple[int | None, float]] = {}
            materialized: dict[str, Mapping[tuple[int, int], int]] = {}
            for device, probe in probes.items():
                if stop is not None and stop.is_set():
                    return
                try:
                    value = probe()
                    available = None if value is None else max(0, int(value))
                except (OSError, RuntimeError, ValueError, TypeError):
                    available = None
                observed[device] = available, time.monotonic()
            for device, materialized_probe in materialized_probes.items():
                if stop is not None and stop.is_set():
                    return
                try:
                    materialized[device] = dict(materialized_probe())
                except (OSError, RuntimeError, ValueError, TypeError):
                    materialized[device] = {}
            with self._condition:
                # Closing a scope or replacing a registration cannot publish a
                # late result as the new owner's current GPU observation.
                if stop is not None and stop.is_set():
                    return
                for device, observation in observed.items():
                    if self._gpu_probe_generations.get(device) == generations.get(device):
                        self._gpu_available_cache[device] = observation
                for device, values in materialized.items():
                    if self._gpu_probe_generations.get(device) == generations.get(device):
                        self._gpu_materialized_cache[device] = values
                self._condition.notify_all()
        finally:
            self._gpu_sample_lock.release()

    def _gpu_monitor_running(self) -> bool:
        return (
            self._gpu_monitor_thread is not None
            and self._gpu_monitor_thread.is_alive()
            and not self._gpu_monitor_stop.is_set()
        )

    def _start_gpu_monitor_locked(self) -> None:
        """Start one advisory GPU producer while the accounting lock is held."""
        if (
            not self._gpu_available_probes and not self._gpu_materialized_probes
        ) or self._gpu_monitor_running():
            return
        # Each lifetime has its own stop token: start() after a bounded close
        # cannot revive a previous observer whose tool has not returned yet.
        stop = self._gpu_monitor_stop = threading.Event()
        wakeup = self._gpu_monitor_wakeup = threading.Event()
        self._gpu_monitor_thread = threading.Thread(
            target=self._monitor_gpu_resources, args=(stop, wakeup),
            name="neocortex-gpu-monitor", daemon=True,
        )
        self._gpu_monitor_thread.start()

    def _monitor_gpu_resources(
        self, stop: threading.Event, wakeup: threading.Event,
    ) -> None:
        while not stop.is_set():
            wakeup.clear()
            try:
                self._sample_gpu_resources(stop)
            except (OSError, RuntimeError, ValueError, TypeError):
                # Freshness is checked per device. A failed GPU observation
                # does not stop publication of healthy CPU/RAM samples.
                pass
            if not stop.is_set():
                wakeup.wait(self.limits.sample_interval_seconds)

    def _gpu_available_locked(self, device: str) -> int | None:
        sample = self._gpu_available_cache.get(device)
        if sample is None or time.monotonic() - sample[1] > max(
            2.0, self.limits.sample_interval_seconds * 4,
        ):
            return None
        return sample[0]

    def _gpu_materialized_credit_locked(self, device: str) -> int:
        observed = self._gpu_materialized_cache.get(device, {})
        seen: set[tuple[int, int]] = set()
        credit = 0
        for request in self._active_reservations.values():
            if (request.gpu_device or "default") != device:
                continue
            owned = 0
            for identity in request.process_identities:
                if identity not in seen and identity in observed:
                    seen.add(identity)
                    owned += max(0, int(observed[identity]))
            credit += min(request.gpu_bytes, owned)
        return min(self._gpu_in_use.get(device, 0), credit)

    def gpu_worker_capacity(self, route_name: str, device: str, estimated_bytes: int,
                            *, reusable_resident_bytes: int = 0) -> int:
        """Plan model replicas using device capacity and unmaterialized promises.

        A provider may supply verified PID/start-time GPU bytes for credit.
        Without that attribution, reservations remain conservatively pending;
        a free-VRAM sample alone cannot prove which lease has materialized.
        """
        if estimated_bytes <= 0 or reusable_resident_bytes < 0:
            raise ValueError("GPU worker memory must be positive and reusable bytes nonnegative")
        if not self._gpu_monitor_running():
            self._sample_gpu_resources()
        with self._condition:
            configured = self._gpu_capacity.get(device, 0)
            if estimated_bytes > configured:
                raise MemoryBudgetExceeded("GPU worker exceeds the registered device capacity")
            own = sum(request.gpu_bytes for request in self._active_reservations.values()
                      if request.route_name == route_name and (request.gpu_device or "default") == device)
            reusable = min(own, reusable_resident_bytes)
            reserved = self._gpu_in_use.get(device, 0)
            remaining = configured - reserved + reusable
            if device in self._gpu_available_probes:
                available = self._gpu_available_locked(device)
                if available is None:
                    return 0
                pending = reserved - self._gpu_materialized_credit_locked(device)
                remaining = min(remaining, available - pending + reusable)
            return max(0, remaining // estimated_bytes)

    def start(self) -> None:
        """Observe pressure independently of admissions during an active scope."""
        with self._condition:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_resources,
                name="neocortex-resource-monitor", daemon=True,
            )
            self._monitor_thread.start()
            self._start_gpu_monitor_locked()

    def close(self) -> None:
        """Stop monitoring; held leases retain ownership until their owners exit."""
        with self._condition:
            self._monitor_stop.set()
            self._gpu_monitor_stop.set()
            self._gpu_monitor_wakeup.set()
            self._condition.notify_all()
            monitors = self._monitor_thread, self._gpu_monitor_thread
        deadline = time.monotonic() + max(1.0, self.limits.sample_interval_seconds * 2)
        for monitor in monitors:
            if monitor is not None and monitor is not threading.current_thread():
                monitor.join(max(0.0, deadline - time.monotonic()))

    def _monitor_resources(self) -> None:
        while not self._monitor_stop.is_set():
            try:
                # Warm the cached system sample outside the accounting lock.
                if self._default_sampler is not None:
                    self._default_sampler.sample()
                with self._condition:
                    self._observe_live_resources()
                    self._condition.notify_all()
            except (OSError, RuntimeError, ValueError, TypeError):
                # Unknown observations never revoke running work. The next
                # interval retries; admissions retain existing safety checks.
                pass
            self._monitor_stop.wait(self.limits.sample_interval_seconds)

    def _io_capacity(self, device: str) -> int:
        configured = (self.limits.io_device_slots or {}).get(device)
        if configured is not None:
            return max(1, int(configured))
        if self.limits.io_slots is not None:
            return max(1, self.limits.io_slots)
        return max(1, self._last_cpu_capacity)

    def _materialized_credit_locked(self) -> int:
        """Credit only measured private bytes bound to a live request/process.

        Aggregate scope RSS/USS is insufficient attribution: uncharged caches,
        shared mappings or another phase must not spend a request's headroom.
        Each PID/start-time identity is counted at most once across leases.
        """
        observed = self._last_owned_snapshot
        process_memory = {} if observed is None else observed.process_memory_bytes
        seen: set[tuple[int, int]] = set()
        credit = 0
        for request in self._active_reservations.values():
            own = 0
            for identity in request.process_identities:
                if identity not in seen and identity in process_memory:
                    seen.add(identity)
                    own += max(0, int(process_memory[identity]))
            credit += min(request.memory_bytes, own)
        self._materialized_credit_bytes = min(self._reserved_bytes, credit)
        return self._materialized_credit_bytes

    def worker_capacity(
        self, route_name: str | None = None, *, max_workers: int | None = None,
        estimated_bytes: int = 0, native_threads: int = 1,
        reusable_resident_bytes: int = 0,
    ) -> int:
        """Return this route's current target, including its existing work.

        Zero means temporary pressure, not a permanent one-worker fallback.
        Oversized individual work raises instead of waiting forever at zero.
        """
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be positive or None")
        if estimated_bytes < 0 or native_threads < 0 or reusable_resident_bytes < 0:
            raise ValueError("worker resource demand cannot be negative")
        with self._condition:
            snapshot, cpu_capacity = self._observe_live_resources()
            if estimated_bytes > self._last_memory_budget:
                raise MemoryBudgetExceeded("one worker exceeds the current memory capacity")
            route_budget = (
                None if route_name is None else self._route_memory_budgets.get(route_name)
            )
            if route_budget is not None and estimated_bytes > route_budget:
                raise MemoryBudgetExceeded("one worker exceeds the configured route memory budget")
            if self._memory_pressure or self._cpu_pressure:
                return 0
            metrics = self._metrics.get(route_name) if route_name is not None else None
            own_cpu = 0 if metrics is None else metrics.cpu_slots
            own_native = 0 if metrics is None else metrics.native_threads
            # The target may replace this route's work/result reservations,
            # but model/interpreter residence remains charged at every target.
            own_transient = 0 if metrics is None else metrics.transient_bytes
            # A process pool's per-worker estimate includes its interpreter.
            # Count already resident interpreters once in the TOTAL target;
            # actual admissions still retain their separate live leases.
            reusable_residence = (
                0 if metrics is None else min(metrics.resident_bytes, reusable_resident_bytes)
            )
            slots = max(0, cpu_capacity - self._cpu_in_use + own_cpu)
            if native_threads:
                native_available = min(self.native_thread_slots, cpu_capacity) - self._native_threads_in_use + own_native
                slots = min(slots, max(0, native_available // native_threads))
            if estimated_bytes:
                remaining = self._last_memory_budget - self._reserved_bytes + own_transient + reusable_residence
                if route_budget is not None:
                    route_reserved = 0 if metrics is None else metrics.reserved_bytes
                    remaining = min(
                        remaining,
                        route_budget - route_reserved + own_transient + reusable_residence,
                    )
                # A negative remainder represents this route's verified
                # materialized work. Preserve that credit when computing
                # a TOTAL concurrency target, not just new free slots.
                unmaterialized = self._reserved_bytes - self._materialized_credit_locked() - own_transient - reusable_residence
                for available, headroom in (
                    (snapshot.available_physical, self.min_free_memory_bytes),
                    (snapshot.available_commit, self.min_free_commit_bytes),
                ):
                    if available is not None:
                        remaining = min(remaining, available - headroom - unmaterialized)
                slots = min(slots, max(0, remaining // estimated_bytes))
            if max_workers is not None:
                slots = min(slots, max_workers)
            return max(0, int(slots))

    def _read_resource_sample(self) -> ResourceSample | None:
        probe = self._resource_probe
        if probe is None:
            if self._default_sampler is not None:
                monitor_running = self._monitor_thread is not None and self._monitor_thread.is_alive()
                owned = (self._default_sampler.current_sample() if monitor_running
                         else self._default_sampler.sample())
                if owned is None:
                    return None
                self._last_owned_snapshot = owned
                memory = owned.memory_snapshot
                if monitor_running and time.monotonic() - owned.sampled_at > max(
                    2.0, self.limits.sample_interval_seconds * 4,
                ):
                    # A stalled observer must not keep spending old headroom.
                    # Existing work retains ownership and may reach a safe
                    # checkpoint; new work waits for a fresh observation.
                    return ResourceSample(
                        available_physical=0, total_physical=memory.total_physical,
                        external_cpu_cores=owned.effective_cpu_capacity,
                        effective_cpu_capacity=owned.effective_cpu_capacity,
                    )
                return ResourceSample(
                    available_physical=memory.available_physical,
                    available_commit=memory.available_commit,
                    total_physical=memory.total_physical,
                    total_commit=memory.total_commit,
                    cpu_load_percent=owned.host_cpu_percent,
                    external_cpu_cores=owned.external_cpu_cores,
                    own_cpu_cores=owned.own_cpu_cores,
                    effective_cpu_capacity=owned.effective_cpu_capacity,
                    owned_materialized_bytes=owned.owned_materialized_bytes,
                    memory_pressure_some_percent=owned.memory_pressure_some_percent,
                    memory_pressure_full_percent=owned.memory_pressure_full_percent,
                    memory_pressure_some_total_us=owned.memory_pressure_some_total_us,
                    memory_pressure_full_total_us=owned.memory_pressure_full_total_us,
                    io_pressure_some_percent=owned.io_pressure_some_percent,
                    io_pressure_full_percent=owned.io_pressure_full_percent,
                )
            return _linux_memory_pressure_sample()
        try:
            return _coerce_resource_sample(probe())
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def _resource_snapshot(self, sample: ResourceSample | None) -> MemorySnapshot:
        """Resolve one sample without treating missing fields as zero."""

        return _memory_snapshot_from_resource_sample(sample) or memory_snapshot()

    def _effective_memory_budget(self, snapshot: MemorySnapshot) -> int:
        if not self._automatic_memory_budget:
            return self.memory_budget_bytes
        return _adaptive_memory_budget(snapshot.total_physical)

    def _resident_charge_for_request_locked(self, request: _Request) -> int:
        if request.resident_bytes <= 0 or request.resident_key is None:
            return request.resident_bytes
        claims = self._resident_claims.get(
            (request.route_name, request.resident_key), {}
        )
        current = max(claims.values(), default=0)
        return max(0, request.resident_bytes - current)

    def _update_pressure_locked(
        self,
        snapshot: MemorySnapshot,
        sample: ResourceSample | None,
        cpu_load: float | None,
        *,
        cpu_load_is_explicit: bool,
    ) -> None:
        return update_pressure_locked(
            self, snapshot, sample, cpu_load,
            cpu_load_is_explicit=cpu_load_is_explicit,
        )

    def _observe_live_resources(self):
        return observe_live_resources(self)

    def _effective_cpu_capacity(
        self,
        load_percent: float | None,
        cpu_slots: int | None = None,
    ) -> int:
        available_slots = self.cpu_slots if cpu_slots is None else max(1, cpu_slots)
        if load_percent is None:
            return available_slots
        remaining = max(0.0, self.max_cpu_load_percent - load_percent)
        capacity = math.ceil(available_slots * remaining / self.max_cpu_load_percent)
        return max(1, min(available_slots, capacity))

    def _fits(self, request: _Request, snapshot, effective_cpu_slots: int) -> bool:
        """Return whether a queued request fits the current live sample.

        The caller holds ``self._condition``; the pure predicate is kept in
        ``resource_scheduling`` so admission policy is independently readable.
        """
        return fits(self, request, snapshot, effective_cpu_slots)

    def _next_route(self, snapshot, effective_cpu_slots: int) -> str | None:
        """Select the next fair fitting route while the condition is held."""
        return next_route(self, snapshot, effective_cpu_slots)

    def _record_wait_locked(self, request: _Request, now: float) -> None:
        """Account a request's queue time exactly once while holding the lock."""

        del now
        if request.waited and not request.wait_accounted:
            elapsed_ns = max(0, int(self._clock() * 1_000_000_000) - request.enqueued_at_ns)
            metrics = self._metrics[request.route_name]
            metrics.wait_ns += elapsed_ns
            metrics.wait_seconds += elapsed_ns / 1_000_000_000
            request.wait_accounted = True

    def _discard_queued_request_locked(self, request: _Request) -> None:
        """Remove a queued request idempotently, preserving round-robin state."""

        if not request.queued:
            return
        queue = self._queues[request.route_name]
        for index, queued in enumerate(queue):
            if queued is request:
                del queue[index]
                break
        # The request may already have been removed by an interrupted grant, so
        # clear the ownership marker even when the identity is no longer found.
        request.queued = False
        self._record_wait_locked(request, self._clock())
        self._condition.notify_all()

    def _grant_request_locked(self, request: _Request, route_index: int) -> None:
        """Commit one admission as a small rollback-safe state transition."""

        metrics = self._metrics[request.route_name]
        claim_key = (
            (request.route_name, request.resident_key)
            if request.resident_key is not None
            else None
        )
        previous_claim = (
            None
            if claim_key is None or claim_key not in self._resident_claims
            else dict(self._resident_claims[claim_key])
        )
        previous_phase_admissions = dict(metrics.phase_admissions)
        extra_previous = (
            dict(self._io_in_use), dict(self._gpu_in_use),
            self._active_execution_requests, metrics.active_execution_requests,
            metrics.io_slots, metrics.peak_io_slots, metrics.gpu_bytes, metrics.peak_gpu_bytes,
            self._peak_io_slots, self._peak_gpu_bytes,
        )
        previous_memory_bytes = request.memory_bytes
        previous_resident_charge = request.resident_charge_bytes
        previous = (
            self._reserved_bytes,
            self._resident_bytes,
            self._transient_bytes,
            self._temp_bytes,
            self._cpu_in_use,
            self._native_threads_in_use,
            self._active_requests,
            self._last_granted_index,
            metrics.admissions,
            metrics.reserved_bytes,
            metrics.cpu_slots,
            metrics.active_requests,
            metrics.peak_reserved_bytes,
            metrics.peak_cpu_slots,
            metrics.resident_bytes,
            metrics.transient_bytes,
            metrics.temp_bytes,
            metrics.native_threads,
            metrics.peak_resident_bytes,
            metrics.peak_transient_bytes,
            metrics.peak_temp_bytes,
            metrics.peak_native_threads,
            self._peak_reserved_bytes,
            self._peak_resident_bytes,
            self._peak_transient_bytes,
            self._peak_temp_bytes,
            self._peak_cpu_slots,
            self._peak_native_threads,
            self._peak_active_requests,
            metrics.wait_seconds,
            metrics.wait_ns,
            request.wait_accounted,
        )
        queue = self._queues[request.route_name]
        try:
            queue.popleft()
            request.queued = False
            self._last_granted_index = route_index

            request.resident_charge_bytes = self._resident_charge_for_request_locked(request)
            request.memory_bytes = (
                request.resident_charge_bytes
                + request.transient_bytes
            )
            self._reserved_bytes += request.memory_bytes
            self._resident_bytes += request.resident_charge_bytes
            self._transient_bytes += request.transient_bytes
            self._temp_bytes += request.temp_bytes
            self._cpu_in_use += request.cpu_slots
            self._native_threads_in_use += request.native_threads
            self._active_requests += 1
            if claim_key is not None:
                claims = self._resident_claims.setdefault(claim_key, {})
                claims[id(request)] = request.resident_bytes
            metrics.admissions += 1
            metrics.reserved_bytes += request.memory_bytes
            metrics.cpu_slots += request.cpu_slots
            metrics.active_requests += 1
            metrics.resident_bytes += request.resident_charge_bytes
            metrics.transient_bytes += request.transient_bytes
            metrics.temp_bytes += request.temp_bytes
            metrics.native_threads += request.native_threads
            if request.phase is not None:
                metrics.phase_admissions[request.phase] = (
                    metrics.phase_admissions.get(request.phase, 0) + 1
                )
            metrics.peak_reserved_bytes = max(
                metrics.peak_reserved_bytes, metrics.reserved_bytes
            )
            metrics.peak_cpu_slots = max(metrics.peak_cpu_slots, metrics.cpu_slots)
            metrics.peak_resident_bytes = max(
                metrics.peak_resident_bytes, metrics.resident_bytes
            )
            metrics.peak_transient_bytes = max(
                metrics.peak_transient_bytes, metrics.transient_bytes
            )
            metrics.peak_temp_bytes = max(metrics.peak_temp_bytes, metrics.temp_bytes)
            metrics.peak_native_threads = max(
                metrics.peak_native_threads, metrics.native_threads
            )
            if request.waited:
                self._record_wait_locked(request, self._clock())
            self._peak_reserved_bytes = max(
                self._peak_reserved_bytes, self._reserved_bytes
            )
            self._peak_resident_bytes = max(self._peak_resident_bytes, self._resident_bytes)
            self._peak_transient_bytes = max(
                self._peak_transient_bytes, self._transient_bytes
            )
            self._peak_temp_bytes = max(self._peak_temp_bytes, self._temp_bytes)
            self._peak_cpu_slots = max(self._peak_cpu_slots, self._cpu_in_use)
            self._peak_native_threads = max(
                self._peak_native_threads, self._native_threads_in_use
            )
            self._peak_active_requests = max(
                self._peak_active_requests, self._active_requests
            )
            request.admitted = True
            request.released = False
            request.execution_active = bool(request.cpu_slots or request.native_threads or request.io_slots)
            self._active_reservations[id(request)] = request
            if request.execution_active:
                self._active_execution_requests += 1
                metrics.active_execution_requests += 1
            if request.io_slots:
                device = request.io_device or "default"
                self._io_in_use[device] = self._io_in_use.get(device, 0) + request.io_slots
                metrics.io_slots += request.io_slots
                metrics.peak_io_slots = max(metrics.peak_io_slots, metrics.io_slots)
                self._peak_io_slots = max(self._peak_io_slots, sum(self._io_in_use.values()))
            if request.gpu_bytes:
                device = request.gpu_device or "default"
                self._gpu_in_use[device] = self._gpu_in_use.get(device, 0) + request.gpu_bytes
                metrics.gpu_bytes += request.gpu_bytes
                metrics.peak_gpu_bytes = max(metrics.peak_gpu_bytes, metrics.gpu_bytes)
                self._peak_gpu_bytes = max(self._peak_gpu_bytes, sum(self._gpu_in_use.values()))
        except BaseException:
            (
                self._io_in_use, self._gpu_in_use,
                self._active_execution_requests, metrics.active_execution_requests,
                metrics.io_slots, metrics.peak_io_slots, metrics.gpu_bytes, metrics.peak_gpu_bytes,
                self._peak_io_slots, self._peak_gpu_bytes,
            ) = extra_previous
            self._active_reservations.pop(id(request), None)
            request.execution_active = False
            (
                self._reserved_bytes,
                self._resident_bytes,
                self._transient_bytes,
                self._temp_bytes,
                self._cpu_in_use,
                self._native_threads_in_use,
                self._active_requests,
                self._last_granted_index,
                metrics.admissions,
                metrics.reserved_bytes,
                metrics.cpu_slots,
                metrics.active_requests,
                metrics.peak_reserved_bytes,
                metrics.peak_cpu_slots,
                metrics.resident_bytes,
                metrics.transient_bytes,
                metrics.temp_bytes,
                metrics.native_threads,
                metrics.peak_resident_bytes,
                metrics.peak_transient_bytes,
                metrics.peak_temp_bytes,
                metrics.peak_native_threads,
                self._peak_reserved_bytes,
                self._peak_resident_bytes,
                self._peak_transient_bytes,
                self._peak_temp_bytes,
                self._peak_cpu_slots,
                self._peak_native_threads,
                self._peak_active_requests,
                metrics.wait_seconds,
                metrics.wait_ns,
                request.wait_accounted,
            ) = previous
            if claim_key is not None:
                if previous_claim is None:
                    self._resident_claims.pop(claim_key, None)
                else:
                    self._resident_claims[claim_key] = previous_claim
            metrics.phase_admissions.clear()
            metrics.phase_admissions.update(previous_phase_admissions)
            request.memory_bytes = previous_memory_bytes
            request.resident_charge_bytes = previous_resident_charge
            request.queued = False
            request.admitted = False
            request.released = False
            raise

    def _release_admission_locked(self, request: _Request) -> None:
        """Release an admission exactly once, even if notification is interrupted."""

        if not request.admitted or request.released:
            return
        metrics = self._metrics[request.route_name]
        extra_previous = (
            dict(self._io_in_use), dict(self._gpu_in_use),
            self._active_execution_requests, metrics.active_execution_requests,
            metrics.io_slots, metrics.gpu_bytes, request.execution_active,
        )
        claim_key = (
            (request.route_name, request.resident_key)
            if request.resident_key is not None
            else None
        )
        resident_released = request.resident_charge_bytes
        previous_claim = (
            None
            if claim_key is None or claim_key not in self._resident_claims
            else dict(self._resident_claims[claim_key])
        )
        if claim_key is not None:
            claims = self._resident_claims.get(claim_key)
            if claims is not None:
                before = max(claims.values(), default=0)
                claims.pop(id(request), None)
                after = max(claims.values(), default=0)
                resident_released = max(0, before - after)
                if not claims:
                    self._resident_claims.pop(claim_key, None)
        released_memory = resident_released + request.transient_bytes
        previous = (
            self._reserved_bytes,
            self._resident_bytes,
            self._transient_bytes,
            self._temp_bytes,
            self._cpu_in_use,
            self._native_threads_in_use,
            self._active_requests,
            metrics.reserved_bytes,
            metrics.cpu_slots,
            metrics.active_requests,
            metrics.resident_bytes,
            metrics.transient_bytes,
            metrics.temp_bytes,
            metrics.native_threads,
        )
        try:
            self._reserved_bytes -= released_memory
            self._resident_bytes -= resident_released
            self._transient_bytes -= request.transient_bytes
            self._temp_bytes -= request.temp_bytes
            self._cpu_in_use -= request.cpu_slots
            self._native_threads_in_use -= request.native_threads
            self._active_requests -= 1
            metrics.reserved_bytes -= released_memory
            metrics.cpu_slots -= request.cpu_slots
            metrics.active_requests -= 1
            metrics.resident_bytes -= resident_released
            metrics.transient_bytes -= request.transient_bytes
            metrics.temp_bytes -= request.temp_bytes
            metrics.native_threads -= request.native_threads
            if request.execution_active:
                self._active_execution_requests -= 1
                metrics.active_execution_requests -= 1
                request.execution_active = False
            if request.io_slots:
                device = request.io_device or "default"
                self._io_in_use[device] -= request.io_slots
                metrics.io_slots -= request.io_slots
            if request.gpu_bytes:
                device = request.gpu_device or "default"
                self._gpu_in_use[device] -= request.gpu_bytes
                metrics.gpu_bytes -= request.gpu_bytes
        except BaseException:
            (
                self._io_in_use, self._gpu_in_use,
                self._active_execution_requests, metrics.active_execution_requests,
                metrics.io_slots, metrics.gpu_bytes, request.execution_active,
            ) = extra_previous
            (
                self._reserved_bytes,
                self._resident_bytes,
                self._transient_bytes,
                self._temp_bytes,
                self._cpu_in_use,
                self._native_threads_in_use,
                self._active_requests,
                metrics.reserved_bytes,
                metrics.cpu_slots,
                metrics.active_requests,
                metrics.resident_bytes,
                metrics.transient_bytes,
                metrics.temp_bytes,
                metrics.native_threads,
            ) = previous
            if claim_key is not None:
                if previous_claim is None:
                    self._resident_claims.pop(claim_key, None)
                else:
                    self._resident_claims[claim_key] = previous_claim
            raise
        # Mark the reservation released before waking waiters. If notification
        # itself is interrupted, a retry cannot double-release the counters.
        request.admitted = False
        request.released = True
        self._active_reservations.pop(id(request), None)
        self._condition.notify_all()

    def _release_execution(self, request: _Request) -> None:
        """Return CPU/native/I/O while retaining result/model memory ownership."""
        with self._condition:
            if not request.admitted or request.released or not request.execution_active:
                return
            metrics = self._metrics[request.route_name]
            self._cpu_in_use -= request.cpu_slots
            self._native_threads_in_use -= request.native_threads
            metrics.cpu_slots -= request.cpu_slots
            metrics.native_threads -= request.native_threads
            if request.io_slots:
                device = request.io_device or "default"
                self._io_in_use[device] -= request.io_slots
                metrics.io_slots -= request.io_slots
            request.cpu_slots = 0
            request.native_threads = 0
            request.io_slots = 0
            request.execution_active = False
            self._active_execution_requests -= 1
            metrics.active_execution_requests -= 1
            self._condition.notify_all()

    def _cleanup_request(
        self,
        request: _Request,
        primary: BaseException | None,
    ) -> None:
        """Clean queued/admitted state without masking a primary exception."""

        try:
            with self._condition:
                if request.queued:
                    self._discard_queued_request_locked(request)
                elif request.admitted and not request.released:
                    self._release_admission_locked(request)
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(
                "global resource admission cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )

    @contextmanager
    def admit(
        self,
        route_name: str,
        memory_bytes: int | None = None,
        cpu_slots: int = 1,
        *,
        resident_bytes: int = 0,
        transient_bytes: int | None = None,
        temp_bytes: int = 0,
        native_threads: int = 0,
        phase: str | None = None,
        resident_key: str | None = None,
        cancellation: CancellationToken | None = None,
        io_slots: int = 0,
        io_device: str | None = None,
        gpu_bytes: int = 0,
        gpu_device: str | None = None,
        _draining_from: _Request | None = None,
        _adaptive_native_threads: bool = False,
        _native_thread_limit: int | None = None,
        _renewing_from: _Request | None = None,
    ):
        """Wait for and hold one bounded route admission on this coordinator."""
        with admission_scope(
            self,
            route_name,
            memory_bytes,
            cpu_slots,
            resident_bytes=resident_bytes,
            transient_bytes=transient_bytes,
            temp_bytes=temp_bytes,
            native_threads=native_threads,
            phase=phase,
            resident_key=resident_key,
            cancellation=cancellation,
            io_slots=io_slots,
            io_device=io_device,
            gpu_bytes=gpu_bytes,
            gpu_device=gpu_device,
            _draining_from=_draining_from,
            _adaptive_native_threads=_adaptive_native_threads,
            _native_thread_limit=_native_thread_limit,
            _renewing_from=_renewing_from,
        ) as grant:
            yield grant

    def route_peak_reserved_bytes(self, route_name: str) -> int:
        with self._condition:
            return self._metrics[route_name].peak_reserved_bytes

    def route_wait_count(self, route_name: str) -> int:
        with self._condition:
            return self._metrics[route_name].waits

    def route_wait_ns(self, route_name: str) -> int:
        with self._condition:
            return self._metrics[route_name].wait_ns

    def route_active_request_count(self, route_name: str) -> int:
        """Return currently admitted jobs, excluding queued requests."""

        with self._condition:
            return self._metrics[route_name].active_requests

    def route_resource_usage(self, route_name: str) -> ResourceSample:
        """Return current coordinator-owned usage for one route."""

        with self._condition:
            metrics = self._metrics[route_name]
            return ResourceSample(
                resident_bytes=metrics.resident_bytes,
                transient_bytes=metrics.transient_bytes,
                temp_bytes=metrics.temp_bytes,
                native_threads=metrics.native_threads,
            )

    def resource_usage(self) -> ResourceSample:
        """Return current usage without exposing mutable internal counters."""

        with self._condition:
            return ResourceSample(
                resident_bytes=self._resident_bytes,
                transient_bytes=self._transient_bytes,
                temp_bytes=self._temp_bytes,
                native_threads=self._native_threads_in_use,
                cpu_load_percent=self._last_cpu_load,
            )

    # These read-only properties keep simple adapters from reaching into the
    # private counters while retaining the old ``peak_reserved_bytes`` fields.
    @property
    def resident_bytes(self) -> int:
        with self._condition:
            return self._resident_bytes

    @property
    def transient_bytes(self) -> int:
        with self._condition:
            return self._transient_bytes

    @property
    def temp_bytes(self) -> int:
        with self._condition:
            return self._temp_bytes

    @property
    def native_threads(self) -> int:
        with self._condition:
            return self._native_threads_in_use

    def summary(self) -> GlobalResourceSummary:
        with self._condition:
            routes = {
                name: RouteResourceSummary(
                    admissions=metrics.admissions,
                    waits=metrics.waits,
                    wait_seconds=round(metrics.wait_seconds, 6),
                    peak_reserved_bytes=metrics.peak_reserved_bytes,
                    peak_cpu_slots=metrics.peak_cpu_slots,
                    wait_ns=metrics.wait_ns,
                    resident_bytes=metrics.resident_bytes,
                    transient_bytes=metrics.transient_bytes,
                    temp_bytes=metrics.temp_bytes,
                    native_threads=metrics.native_threads,
                    peak_resident_bytes=metrics.peak_resident_bytes,
                    peak_transient_bytes=metrics.peak_transient_bytes,
                    peak_temp_bytes=metrics.peak_temp_bytes,
                    peak_native_threads=metrics.peak_native_threads,
                    phases=dict(metrics.phase_admissions),
                    io_slots=metrics.io_slots,
                    peak_io_slots=metrics.peak_io_slots,
                    gpu_bytes=metrics.gpu_bytes,
                    peak_gpu_bytes=metrics.peak_gpu_bytes,
                    active_execution_requests=metrics.active_execution_requests,
                    cpu_slots_in_use=metrics.cpu_slots,
                )
                for name, metrics in self._metrics.items()
            }
            return GlobalResourceSummary(
                memory_budget_bytes=self.memory_budget_bytes,
                min_free_memory_bytes=self.min_free_memory_bytes,
                min_free_commit_bytes=self.min_free_commit_bytes,
                cpu_slots=self.cpu_slots,
                cpu_slots_in_use=self._cpu_in_use,
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
                native_thread_slots=self.native_thread_slots,
                temp_budget_bytes=self.temp_budget_bytes,
                resident_bytes=self._resident_bytes,
                transient_bytes=self._transient_bytes,
                temp_bytes=self._temp_bytes,
                native_threads=self._native_threads_in_use,
                peak_resident_bytes=self._peak_resident_bytes,
                peak_transient_bytes=self._peak_transient_bytes,
                peak_temp_bytes=self._peak_temp_bytes,
                peak_native_threads=self._peak_native_threads,
                admission_paused=self._memory_pressure or self._cpu_pressure,
                pressure_events=self._pressure_events,
                sample_count=self._sample_count,
                last_available_memory_delta_bytes=self._last_available_memory_delta,
                last_available_commit_delta_bytes=self._last_available_commit_delta,
                memory_pressure_some_percent=(
                    None
                    if self._previous_sample.memory_pressure_some_percent is None
                    else round(self._previous_sample.memory_pressure_some_percent, 3)
                ),
                memory_pressure_full_percent=(
                    None
                    if self._previous_sample.memory_pressure_full_percent is None
                    else round(self._previous_sample.memory_pressure_full_percent, 3)
                ),
                last_memory_pressure_delta_percent=(
                    None
                    if self._last_memory_pressure_delta is None
                    else round(self._last_memory_pressure_delta, 3)
                ),
                last_memory_pressure_total_delta_us=self._last_memory_pressure_total_delta_us,
                effective_cpu_slots=self._last_effective_cpu_slots,
                effective_memory_budget_bytes=self._last_memory_budget,
                own_cpu_cores=self._previous_sample.own_cpu_cores,
                external_cpu_cores=self._previous_sample.external_cpu_cores,
                materialized_credit_bytes=self._materialized_credit_locked(),
                io_slots=sum(self._io_in_use.values()),
                peak_io_slots=self._peak_io_slots,
                gpu_bytes=sum(self._gpu_in_use.values()),
                peak_gpu_bytes=self._peak_gpu_bytes,
                active_execution_requests=self._active_execution_requests,
            )


# endregion [02]


# region [03] Route memory gate adapter

_CURRENT_RESOURCE_COORDINATOR: ContextVar[GlobalResourceCoordinator | None] = ContextVar(
    "neocortex_resource_coordinator", default=None
)
def current_resource_coordinator() -> GlobalResourceCoordinator | None:
    return _CURRENT_RESOURCE_COORDINATOR.get()


def current_resource_grant() -> "ResourceGrant | None":
    grant = _CURRENT_RESOURCE_GRANT.get()
    while grant is not None and getattr(grant, "_owner_grant", None) is not None:
        grant = grant._owner_grant
    return grant


@contextmanager
def resource_scope(coordinator: GlobalResourceCoordinator):
    previous = current_resource_coordinator()
    token = _CURRENT_RESOURCE_COORDINATOR.set(coordinator)
    if previous is not coordinator:
        coordinator.start()
    try:
        yield coordinator
    finally:
        _CURRENT_RESOURCE_COORDINATOR.reset(token)
        if previous is not coordinator:
            coordinator.close()


@contextmanager
def resource_grant_scope(grant: "ResourceGrant"):
    token = _CURRENT_RESOURCE_GRANT.set(grant)
    try:
        yield grant
    finally:
        _CURRENT_RESOURCE_GRANT.reset(token)


def resource_gate(route_name: str, coordinator: GlobalResourceCoordinator | None = None):
    shared = current_resource_coordinator() if coordinator is None else coordinator
    if shared is None:
        return None
    shared.register_route(route_name)
    return CoordinatedMemoryGate(shared, route_name)


@contextmanager
def gate_for_limits(
    route_name: str, limits: MemoryResourceLimits, *,
    cancellation: CancellationToken | None = None,
):
    """Use the run scope, or own a monitored standalone route scope.

    A standalone caller's explicit memory policy remains authoritative. The
    shared framework already owns that policy and must not create another
    controller for each format. Only the scope created here is closed here.
    """
    shared = current_resource_coordinator()
    if shared is not None:
        shared.register_route(route_name)
        yield CoordinatedMemoryGate(shared, route_name, cancellation=cancellation)
        return
    coordinator = GlobalResourceCoordinator(
        (route_name,),
        GlobalResourceLimits(
            memory_budget_bytes=limits.memory_budget_bytes,
            min_free_memory_bytes=limits.min_free_memory_bytes,
            min_free_commit_bytes=limits.min_free_commit_bytes,
            wait_timeout_seconds=limits.wait_timeout_seconds,
        ), cancellation=cancellation,
    )
    with resource_scope(coordinator):
        yield CoordinatedMemoryGate(coordinator, route_name, cancellation=cancellation)


class CoordinatedMemoryGate:
    def __init__(
        self,
        coordinator: GlobalResourceCoordinator,
        route_name: str,
        *,
        cancellation: CancellationToken | None = None,
    ):
        self.coordinator = coordinator
        self.route_name = route_name
        self.cancellation = cancellation

    @property
    def peak_reserved_bytes(self) -> int:
        return self.coordinator.route_peak_reserved_bytes(self.route_name)

    @property
    def wait_count(self) -> int:
        return self.coordinator.route_wait_count(self.route_name)

    @property
    def wait_ns(self) -> int:
        return self.coordinator.route_wait_ns(self.route_name)

    def worker_capacity(self, *, max_workers=None, estimated_bytes=0, native_threads=1,
                        reusable_resident_bytes=0):
        return self.coordinator.worker_capacity(
            self.route_name, max_workers=max_workers,
            estimated_bytes=estimated_bytes, native_threads=native_threads,
            reusable_resident_bytes=reusable_resident_bytes,
        )

    def gpu_worker_capacity(self, device: str, estimated_bytes: int,
                            *, reusable_resident_bytes: int = 0) -> int:
        return self.coordinator.gpu_worker_capacity(
            self.route_name, device, estimated_bytes,
            reusable_resident_bytes=reusable_resident_bytes,
        )

    @contextmanager
    def admit(
        self, estimated_bytes: int, *, cpu_slots: int = 1, native_threads: int = 1,
        resident_bytes: int = 0, temp_bytes: int = 0,
        io_slots: int = 0, io_device: str | None = None,
        gpu_bytes: int = 0, gpu_device: str | None = None,
        phase: str | None = None, resident_key: str | None = None,
        cancellation: CancellationToken | None = None,
    ):
        # The adapter's first argument is transient working/result memory;
        # explicit resident/temp components are separate, never aliases.
        with self.coordinator.admit(
            self.route_name, None, cpu_slots,
            resident_bytes=resident_bytes, transient_bytes=int(estimated_bytes),
            temp_bytes=temp_bytes, native_threads=native_threads,
            io_slots=io_slots, io_device=io_device, gpu_bytes=gpu_bytes,
            gpu_device=gpu_device, phase=phase, resident_key=resident_key,
            cancellation=cancellation or self.cancellation,
        ) as grant:
            yield grant

    @contextmanager
    def resident(
        self, estimated_bytes: int, *, resident_key: str,
        phase: str = "resident", gpu_bytes: int = 0, gpu_device: str | None = None,
    ):
        with self.admit(
            0, cpu_slots=0, native_threads=0, resident_bytes=estimated_bytes,
            resident_key=resident_key, phase=phase,
            gpu_bytes=gpu_bytes, gpu_device=gpu_device,
        ) as grant:
            yield grant

    @contextmanager
    def native_budget(self, estimated_bytes: int = 0, *, max_threads: int | None = None, **kwargs):
        if max_threads is not None and max_threads < 1:
            raise ValueError("max_threads must be positive or None")
        # A one-thread minimum waits under pressure; selection increases it
        # to the largest current CPU/native grant within the caller's ceiling.
        # Memory describes the shared batch once, independently of its width.
        cancellation = kwargs.pop("cancellation", None) or self.cancellation
        with self.coordinator.admit(
            self.route_name, None, 1,
            transient_bytes=int(estimated_bytes), native_threads=1,
            cancellation=cancellation, _adaptive_native_threads=True,
            _native_thread_limit=max_threads, **kwargs,
        ) as grant:
            yield grant
# endregion [03]
