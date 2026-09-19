"""Fair, adaptive resource coordination shared by concurrent content routes."""

from __future__ import annotations

import math
import os
import shutil
import stat
import tempfile
import threading
import time
from contextvars import Context, ContextVar, copy_context
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from .cancellation import CancellationRequested, CancellationToken
from .cpu_runtime import CpuLoadSampler, effective_cpu_count
from .memory_runtime import (
    MemoryBudgetExceeded,
    MemoryHeadroomTimeout,
    MemorySnapshot,
    MemoryResourceLimits,
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
    wait_timeout_seconds: float | None = None
    poll_interval_seconds: float = 0.25
    # These fields are intentionally additive.  The first six fields are the
    # long-standing positional contract consumed by application projections.
    native_thread_slots: int | None = None
    cpu_hysteresis_percent: float = 5.0
    memory_hysteresis_bytes: int | None = None
    sample_interval_seconds: float = 0.25
    temp_budget_bytes: int | None = None
    memory_pressure_high_percent: float = 10.0
    memory_pressure_recovery_percent: float = 5.0
    io_slots: int | None = None
    io_device_slots: Mapping[str, int] | None = None
    gpu_memory_bytes: Mapping[str, int] | None = None
    io_pressure_high_percent: float = 20.0
    io_pressure_recovery_percent: float = 10.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Optional deterministic sample for the coordinator's live probes.

    ``memory_snapshot`` and ``cpu_load_probe`` remain the compatibility
    defaults.  A caller that already owns a bounded system sampler can inject
    this value instead; all fields are observations, never control commands.
    The coordinator computes deltas between successive samples and never
    treats a missing field as zero.
    """

    available_physical: int | None = None
    available_commit: int | None = None
    total_physical: int | None = None
    total_commit: int | None = None
    cpu_load_percent: float | None = None
    resident_bytes: int | None = None
    transient_bytes: int | None = None
    temp_bytes: int | None = None
    native_threads: int | None = None
    memory_pressure_some_percent: float | None = None
    memory_pressure_full_percent: float | None = None
    memory_pressure_some_total_us: int | None = None
    memory_pressure_full_total_us: int | None = None
    external_cpu_cores: float | None = None
    own_cpu_cores: float | None = None
    effective_cpu_capacity: float | None = None
    owned_materialized_bytes: int | None = None
    io_pressure_some_percent: float | None = None
    io_pressure_full_percent: float | None = None
    gpu_available_bytes: Mapping[str, int] | None = None

    @property
    def available_physical_bytes(self) -> int | None:
        return self.available_physical

    @property
    def available_commit_bytes(self) -> int | None:
        return self.available_commit


# Descriptive aliases keep the small injected-sample API discoverable without
# creating another coordinator or another resource owner.
GlobalResourceSample = ResourceSample
ResourcePressureSample = ResourceSample
ResourceUsage = ResourceSample
GlobalResourceUsage = ResourceSample


_PROC_MEMORY_PRESSURE = Path("/proc/pressure/memory")


def _linux_memory_pressure_sample(
    path: Path = _PROC_MEMORY_PRESSURE,
) -> ResourceSample | None:
    """Read PSI memory averages when the kernel exposes them."""

    if os.name == "nt":
        return None
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    some_percent: float | None = None
    full_percent: float | None = None
    some_total_us: int | None = None
    full_total_us: int | None = None
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in {"some", "full"}:
            continue
        for item in parts[1:]:
            name, separator, raw = item.partition("=")
            if not separator:
                continue
            if name == "avg10":
                try:
                    if parts[0] == "some":
                        some_percent = float(raw)
                    else:
                        full_percent = float(raw)
                except ValueError:
                    pass
            elif name == "total":
                try:
                    if parts[0] == "some":
                        some_total_us = int(raw)
                    else:
                        full_total_us = int(raw)
                except ValueError:
                    pass
    if all(
        value is None
        for value in (some_percent, full_percent, some_total_us, full_total_us)
    ):
        return None
    return ResourceSample(
        memory_pressure_some_percent=some_percent,
        memory_pressure_full_percent=full_percent,
        memory_pressure_some_total_us=some_total_us,
        memory_pressure_full_total_us=full_total_us,
    )


class ResourceWaitTimeout(MemoryHeadroomTimeout):
    """A global admission wait expired; worker execution did not time out."""

    def __init__(self, message: str, *, reason: str = "resource") -> None:
        super().__init__(message)
        self.reason = reason


class NativeThreadBudgetExceeded(MemoryBudgetExceeded):
    """A request asks for more native threads than the configured cap."""


def _coerce_resource_sample(value: object) -> ResourceSample | None:
    """Accept the public sample, a memory snapshot, or a bounded mapping.

    The mapping form is deliberately small and useful for tests/adapters that
    already expose JSON-like telemetry.  Unknown keys are ignored; malformed
    values make the sample unavailable instead of turning an observation into
    a control decision.
    """

    if value is None:
        return None
    if isinstance(value, ResourceSample):
        return value
    if isinstance(value, MemorySnapshot):
        return ResourceSample(
            available_physical=value.available_physical,
            available_commit=value.available_commit,
            total_physical=value.total_physical,
            total_commit=value.total_commit,
        )
    if not isinstance(value, Mapping):
        return None
    aliases = {
        "available_physical": "available_physical",
        "available_physical_bytes": "available_physical",
        "available_commit": "available_commit",
        "available_commit_bytes": "available_commit",
        "total_physical": "total_physical",
        "total_physical_bytes": "total_physical",
        "total_commit": "total_commit",
        "total_commit_bytes": "total_commit",
        "cpu_load_percent": "cpu_load_percent",
        "cpu_load": "cpu_load_percent",
        "resident_bytes": "resident_bytes",
        "transient_bytes": "transient_bytes",
        "temp_bytes": "temp_bytes",
        "native_threads": "native_threads",
        "memory_pressure_some_percent": "memory_pressure_some_percent",
        "pressure_some_percent": "memory_pressure_some_percent",
        "memory_pressure_full_percent": "memory_pressure_full_percent",
        "pressure_full_percent": "memory_pressure_full_percent",
        "memory_pressure_some_total_us": "memory_pressure_some_total_us",
        "memory_pressure_full_total_us": "memory_pressure_full_total_us",
        "external_cpu_cores": "external_cpu_cores",
        "own_cpu_cores": "own_cpu_cores",
        "effective_cpu_capacity": "effective_cpu_capacity",
        "owned_materialized_bytes": "owned_materialized_bytes",
        "io_pressure_some_percent": "io_pressure_some_percent",
        "io_pressure_full_percent": "io_pressure_full_percent",
    }
    values: dict[str, object] = {}
    for key, target in aliases.items():
        if key in value:
            values[target] = value[key]
    def as_int(name: str) -> int | None:
        raw = values.get(name)
        if raw is None:
            return None
        return int(cast(Any, raw))

    def as_float(name: str) -> float | None:
        raw = values.get(name)
        if raw is None:
            return None
        return float(cast(Any, raw))

    try:
        return ResourceSample(
            available_physical=as_int("available_physical"),
            available_commit=as_int("available_commit"),
            total_physical=as_int("total_physical"),
            total_commit=as_int("total_commit"),
            cpu_load_percent=as_float("cpu_load_percent"),
            resident_bytes=as_int("resident_bytes"),
            transient_bytes=as_int("transient_bytes"),
            temp_bytes=as_int("temp_bytes"),
            native_threads=as_int("native_threads"),
            memory_pressure_some_percent=as_float("memory_pressure_some_percent"),
            memory_pressure_full_percent=as_float("memory_pressure_full_percent"),
            memory_pressure_some_total_us=as_int("memory_pressure_some_total_us"),
            memory_pressure_full_total_us=as_int("memory_pressure_full_total_us"),
            external_cpu_cores=as_float("external_cpu_cores"),
            own_cpu_cores=as_float("own_cpu_cores"),
            effective_cpu_capacity=as_float("effective_cpu_capacity"),
            owned_materialized_bytes=as_int("owned_materialized_bytes"),
            io_pressure_some_percent=as_float("io_pressure_some_percent"),
            io_pressure_full_percent=as_float("io_pressure_full_percent"),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _memory_snapshot_from_resource_sample(sample: ResourceSample | None) -> MemorySnapshot | None:
    if sample is None:
        return None
    if not any(
        value is not None
        for value in (
            sample.available_physical,
            sample.available_commit,
            sample.total_physical,
            sample.total_commit,
        )
    ):
        return None
    return MemorySnapshot(
        sample.available_physical,
        sample.available_commit,
        sample.total_physical,
        sample.total_commit,
    )


@dataclass(frozen=True, slots=True)
class RouteResourceSummary:
    admissions: int
    waits: int
    wait_seconds: float
    peak_reserved_bytes: int
    peak_cpu_slots: int
    wait_ns: int = 0
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    phases: dict[str, int] = field(default_factory=dict)
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0
    cpu_slots_in_use: int = 0


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
    native_thread_slots: int = 0
    temp_budget_bytes: int | None = None
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    admission_paused: bool = False
    pressure_events: int = 0
    sample_count: int = 0
    last_available_memory_delta_bytes: int | None = None
    last_available_commit_delta_bytes: int | None = None
    memory_pressure_some_percent: float | None = None
    memory_pressure_full_percent: float | None = None
    last_memory_pressure_delta_percent: float | None = None
    last_memory_pressure_total_delta_us: int | None = None
    effective_cpu_slots: int = 0
    effective_memory_budget_bytes: int = 0
    own_cpu_cores: float | None = None
    external_cpu_cores: float | None = None
    materialized_credit_bytes: int = 0
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0
    cpu_slots_in_use: int = 0


@dataclass(slots=True)
class _MutableRouteMetrics:
    admissions: int = 0
    waits: int = 0
    wait_seconds: float = 0.0
    wait_ns: int = 0
    reserved_bytes: int = 0
    cpu_slots: int = 0
    active_requests: int = 0
    peak_reserved_bytes: int = 0
    peak_cpu_slots: int = 0
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    peak_resident_bytes: int = 0
    peak_transient_bytes: int = 0
    peak_temp_bytes: int = 0
    peak_native_threads: int = 0
    phase_admissions: dict[str, int] = field(default_factory=dict)
    io_slots: int = 0
    peak_io_slots: int = 0
    gpu_bytes: int = 0
    peak_gpu_bytes: int = 0
    active_execution_requests: int = 0


@dataclass(slots=True)
class _Request:
    route_name: str
    memory_bytes: int
    cpu_slots: int
    enqueued_at: float
    enqueued_at_ns: int
    resident_bytes: int = 0
    transient_bytes: int = 0
    temp_bytes: int = 0
    native_threads: int = 0
    phase: str | None = None
    resident_key: str | None = None
    resident_charge_bytes: int = 0
    waited: bool = False
    queued: bool = False
    admitted: bool = False
    released: bool = False
    wait_accounted: bool = False
    io_slots: int = 0
    io_device: str | None = None
    gpu_bytes: int = 0
    gpu_device: str | None = None
    execution_active: bool = False
    process_identities: set[tuple[int, int]] = field(default_factory=set)
    temp_materialized_bytes: int = 0
    temp_file_identity: tuple[int, int] | None = None
    draining_from: _Request | None = None
    adaptive_native_threads: bool = False
    native_thread_limit: int | None = None


class _CombinedCancellationToken(CancellationToken):
    def __init__(self, owner: CancellationToken | None, extra: CancellationToken):
        super().__init__(parent=extra)
        self._owner = owner
        self._extra = extra

    @property
    def is_cancelled(self) -> bool:
        return super().is_cancelled or (self._owner is not None and self._owner.is_cancelled)

    def checkpoint(self) -> None:
        if self._owner is not None:
            self._owner.checkpoint()
        self._extra.checkpoint()
        super().checkpoint()


def _adaptive_memory_budget(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return GIB
    # The usable effective capacity is the ceiling. Competition is handled by
    # live headroom, not a fixed fraction of otherwise idle RAM.
    return max(1, total_physical - _adaptive_memory_headroom(total_physical))


def _adaptive_memory_headroom(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return 64 * MIB
    return max(1, min(512 * MIB, total_physical // 32))


def _adaptive_cpu_slots() -> int:
    detected = effective_cpu_count()
    return max(1, detected)


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
        """Latch pressure until a materially safer sample is observed.

        Existing admissions are never revoked.  Pressure only prevents a new
        grant; this is the coordinator's safe pause boundary and intentionally
        does not signal or suspend worker threads.
        """

        current_sample = ResourceSample(
            available_physical=snapshot.available_physical,
            available_commit=snapshot.available_commit,
            total_physical=snapshot.total_physical,
            total_commit=snapshot.total_commit,
            cpu_load_percent=cpu_load,
            resident_bytes=None if sample is None else sample.resident_bytes,
            transient_bytes=None if sample is None else sample.transient_bytes,
            temp_bytes=None if sample is None else sample.temp_bytes,
            native_threads=None if sample is None else sample.native_threads,
            memory_pressure_some_percent=(
                None if sample is None else sample.memory_pressure_some_percent
            ),
            memory_pressure_full_percent=(
                None if sample is None else sample.memory_pressure_full_percent
            ),
            memory_pressure_some_total_us=(
                None if sample is None else sample.memory_pressure_some_total_us
            ),
            memory_pressure_full_total_us=(
                None if sample is None else sample.memory_pressure_full_total_us
            ),
            external_cpu_cores=None if sample is None else sample.external_cpu_cores,
            own_cpu_cores=None if sample is None else sample.own_cpu_cores,
            effective_cpu_capacity=None if sample is None else sample.effective_cpu_capacity,
            owned_materialized_bytes=None if sample is None else sample.owned_materialized_bytes,
            io_pressure_some_percent=None if sample is None else sample.io_pressure_some_percent,
            io_pressure_full_percent=None if sample is None else sample.io_pressure_full_percent,
        )
        previous = self._previous_sample
        if (
            current_sample.available_physical is not None
            and previous.available_physical is not None
        ):
            self._last_available_memory_delta = (
                current_sample.available_physical - previous.available_physical
            )
        else:
            self._last_available_memory_delta = None
        if current_sample.available_commit is not None and previous.available_commit is not None:
            self._last_available_commit_delta = (
                current_sample.available_commit - previous.available_commit
            )
        else:
            self._last_available_commit_delta = None
        pressure_values = tuple(
            value
            for value in (
                current_sample.memory_pressure_some_percent,
                current_sample.memory_pressure_full_percent,
            )
            if value is not None and math.isfinite(value)
        )
        previous_pressure_values = tuple(
            value
            for value in (
                previous.memory_pressure_some_percent,
                previous.memory_pressure_full_percent,
            )
            if value is not None and math.isfinite(value)
        )
        pressure_value = max(pressure_values, default=None)
        previous_pressure_value = max(previous_pressure_values, default=None)
        self._last_memory_pressure_delta = (
            None
            if pressure_value is None or previous_pressure_value is None
            else pressure_value - previous_pressure_value
        )
        current_total = tuple(
            value
            for value in (
                current_sample.memory_pressure_some_total_us,
                current_sample.memory_pressure_full_total_us,
            )
            if value is not None
        )
        previous_total = tuple(
            value
            for value in (
                previous.memory_pressure_some_total_us,
                previous.memory_pressure_full_total_us,
            )
            if value is not None
        )
        self._last_memory_pressure_total_delta_us = (
            None
            if not current_total or not previous_total
            else max(current_total) - max(previous_total)
        )
        self._last_memory_pressure_value = pressure_value
        self._previous_sample = current_sample
        self._sample_count += 1

        available_memory = snapshot.available_physical
        available_commit = snapshot.available_commit
        outstanding_growth = max(0, self._reserved_bytes - self._materialized_credit_locked())
        memory_low = (
            available_memory is not None
            and available_memory < self.min_free_memory_bytes + outstanding_growth
        ) or (
            available_commit is not None
            and available_commit < self.min_free_commit_bytes + outstanding_growth
        )
        memory_recovered = (
            available_memory is None
            or available_memory
            >= self.min_free_memory_bytes
            + outstanding_growth
            + self._memory_hysteresis_bytes
        ) and (
            available_commit is None
            or available_commit
            >= self.min_free_commit_bytes
            + outstanding_growth
            + self._memory_hysteresis_bytes
        )
        pressure_high = (
            pressure_value is not None
            and pressure_value >= self.limits.memory_pressure_high_percent
        )
        pressure_recovered = (
            pressure_value is None
            or pressure_value <= self.limits.memory_pressure_recovery_percent
        )
        if self._memory_pressure:
            if memory_recovered and pressure_recovered:
                self._memory_pressure = False
                self._memory_pressure_from_psi = False
                self._pressure_events += 1
        elif memory_low or pressure_high:
            self._memory_pressure = True
            self._memory_pressure_from_psi = pressure_high
            self._pressure_events += 1

        # The built-in sampler observes the whole system, including this
        # process; treating its own high utilization as external competition
        # would create the very self-throttling loop this coordinator is meant
        # to avoid. Both explicit CPU inputs retain the load-cap behavior;
        # the default sampler cannot prove recovery of a missing owned signal.
        if (
            cpu_load_is_explicit
            and cpu_load is not None
            and cpu_load >= self.max_cpu_load_percent
        ):
            if not self._cpu_pressure:
                self._cpu_pressure = True
                self._pressure_events += 1
        elif (
            cpu_load_is_explicit
            and self._cpu_pressure
            and cpu_load is not None
            and cpu_load
            <= self.max_cpu_load_percent - self.limits.cpu_hysteresis_percent
        ):
            self._cpu_pressure = False
            self._pressure_events += 1
        io_values = (
            current_sample.io_pressure_some_percent,
            current_sample.io_pressure_full_percent,
        )
        io_pressure = max((x for x in io_values if x is not None), default=None)
        if io_pressure is not None:
            if self._io_pressure:
                self._io_pressure = io_pressure > self.limits.io_pressure_recovery_percent
            else:
                self._io_pressure = io_pressure >= self.limits.io_pressure_high_percent

    def _observe_live_resources(self):
        sample = self._read_resource_sample()
        snapshot = self._resource_snapshot(sample)
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

        cpu_load: float | None
        if sample is not None and sample.cpu_load_percent is not None:
            cpu_load = sample.cpu_load_percent
        else:
            now = self._clock()
            should_sample_cpu = (
                not self._using_default_cpu_probe
                or self._last_cpu_sample_at is None
                or now - self._last_cpu_sample_at >= self.limits.sample_interval_seconds
            )
            if should_sample_cpu:
                try:
                    cpu_load = self._cpu_load_probe()
                except (OSError, RuntimeError, TypeError, ValueError):
                    cpu_load = None
                self._last_cpu_sample_at = now
            else:
                cpu_load = self._last_cpu_load
        if cpu_load is not None:
            try:
                cpu_load = float(cpu_load)
            except (TypeError, ValueError, OverflowError):
                cpu_load = None
            if cpu_load is not None and math.isfinite(cpu_load):
                cpu_load = max(0.0, min(100.0, cpu_load))
                self._max_cpu_load = (
                    cpu_load
                    if self._max_cpu_load is None
                    else max(self._max_cpu_load, cpu_load)
                )
            else:
                cpu_load = None

        self._last_memory_budget = self._effective_memory_budget(snapshot)
        try:
            detected = max(1, math.ceil(
                sample.effective_cpu_capacity
                if sample is not None and sample.effective_cpu_capacity is not None
                else self._effective_cpu_probe()
            ))
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
            detected = self._last_cpu_capacity
        # An explicit CPU count is a ceiling, never permission to exceed a
        # newly narrowed affinity/quota. Capacity is already known on the
        # first sample, even before two observations can attribute CPU use.
        cpu_capacity = detected if self._automatic_cpu_slots else min(self.cpu_slots, detected)
        if self._automatic_native_thread_slots:
            self.native_thread_slots = cpu_capacity
        self._last_cpu_capacity = cpu_capacity
        cpu_load_is_explicit = not self._using_default_cpu_probe or (
            self._resource_probe is not None
            and sample is not None and sample.cpu_load_percent is not None
        )
        self._update_pressure_locked(
            snapshot, sample, cpu_load, cpu_load_is_explicit=cpu_load_is_explicit
        )
        # The built-in whole-system sample includes the work already charged
        # to _cpu_in_use.  Reducing the total capacity by that same load would
        # count our active jobs twice.  Keep it as telemetry; only an explicit
        # caller-owned load probe can reduce the affinity/cgroup capacity.
        admission_cpu_load = cpu_load if cpu_load_is_explicit else None
        effective_cpu_slots = self._effective_cpu_capacity(admission_cpu_load, cpu_capacity)
        if (sample is not None and sample.external_cpu_cores is not None
                and math.isfinite(sample.external_cpu_cores) and sample.external_cpu_cores >= 0):
            self._last_external_cpu_cores = sample.external_cpu_cores
            effective_capacity = (
                float(cpu_capacity) if sample.effective_cpu_capacity is None
                else min(float(cpu_capacity), sample.effective_cpu_capacity)
            )
            available_cores = max(0.0, effective_capacity - sample.external_cpu_cores)
            effective_cpu_slots = min(effective_cpu_slots, max(0, math.ceil(available_cores)))
            saturated = available_cores <= 0.01
            recovered = available_cores >= max(0.05, effective_capacity * self.limits.cpu_hysteresis_percent / 100)
            if saturated and not self._cpu_pressure:
                self._cpu_pressure = True
                self._pressure_events += 1
            elif recovered and self._cpu_pressure and not cpu_load_is_explicit:
                self._cpu_pressure = False
                self._pressure_events += 1
        elif self._last_external_cpu_cores is not None and not cpu_load_is_explicit:
            # Losing tree attribution is not evidence that competitors left.
            effective_cpu_slots = min(effective_cpu_slots, self._last_effective_cpu_slots)
        if cpu_load is None and self._cpu_pressure:
            effective_cpu_slots = min(
                effective_cpu_slots,
                max(1, self._last_effective_cpu_slots),
            )
        self._min_effective_cpu_slots = min(
            self._min_effective_cpu_slots, effective_cpu_slots
        )
        self._last_cpu_load = cpu_load
        self._last_effective_cpu_slots = effective_cpu_slots
        return snapshot, effective_cpu_slots

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
        draining = request.draining_from is not None
        if draining and self._active_reservations.get(id(request.draining_from)) is not request.draining_from:
            return False
        resident_charge = self._resident_charge_for_request_locked(request)
        future_memory = (
            self._reserved_bytes
            + resident_charge
            + request.transient_bytes
        )
        if not draining and future_memory > self._last_memory_budget:
            return False
        route_budget = self._route_memory_budgets.get(request.route_name)
        if not draining and route_budget is not None and (
            self._metrics[request.route_name].reserved_bytes
            + resident_charge + request.transient_bytes > route_budget
        ):
            return False
        execution = request.cpu_slots > 0 or request.native_threads > 0 or request.io_slots > 0
        if not draining and (self._memory_pressure or (self._cpu_pressure and execution)):
            return False
        pressure = self._memory_pressure or self._cpu_pressure or self._io_pressure
        if draining and pressure:
            # One bounded finalizer may make progress to persist/discard
            # already admitted results and return their memory. It reserves
            # no new memory, GPU or temporary space. New work remains paused.
            if any(active.draining_from is not None and active.execution_active
                   for active in self._active_reservations.values()):
                return False
            effective_cpu_slots = max(1, effective_cpu_slots)
        if self._cpu_in_use + request.cpu_slots > effective_cpu_slots:
            return False
        native_capacity = min(self.native_thread_slots, effective_cpu_slots)
        if native_capacity is not None and (
            self._native_threads_in_use + request.native_threads > native_capacity
        ):
            return False
        if request.temp_bytes + self._temp_bytes > self.temp_budget_bytes:
            return False
        if request.temp_bytes:
            pending = sum(
                max(0, active.temp_bytes - active.temp_materialized_bytes)
                for active in self._active_reservations.values()
            )
            if pending + request.temp_bytes > shutil.disk_usage(tempfile.gettempdir()).free:
                return False
        io_device = request.io_device or "default"
        if request.io_slots:
            if self._io_pressure and not draining:
                return False
            io_capacity = self._io_capacity(io_device)
            if self._io_in_use.get(io_device, 0) + request.io_slots > io_capacity:
                return False
        if request.gpu_bytes:
            gpu_device = request.gpu_device or "default"
            gpu_capacity = self._gpu_capacity.get(gpu_device, 0)
            if self._gpu_in_use.get(gpu_device, 0) + request.gpu_bytes > gpu_capacity:
                return False
            probe = self._gpu_available_probes.get(gpu_device)
            if probe is not None:
                available_gpu = self._gpu_available_locked(gpu_device)
                pending = self._gpu_in_use.get(gpu_device, 0) - self._gpu_materialized_credit_locked(gpu_device)
                if available_gpu is None or pending + request.gpu_bytes > available_gpu:
                    return False
        if draining:
            return True
        incremental_memory = max(0, future_memory - self._materialized_credit_locked())
        physical_ok = (
            snapshot.available_physical is None
            or snapshot.available_physical
            >= self.min_free_memory_bytes + incremental_memory
        )
        commit_ok = (
            snapshot.available_commit is None
            or snapshot.available_commit >= self.min_free_commit_bytes + incremental_memory
        )
        return physical_ok and commit_ok

    def _next_route(self, snapshot, effective_cpu_slots: int) -> str | None:
        route_count = len(self.route_order)
        ordered_routes = tuple(
            self.route_order[(self._last_granted_index + offset) % route_count]
            for offset in range(1, route_count + 1)
        )
        for route_name in ordered_routes:
            if not self._queues[route_name]:
                continue
            request = self._queues[route_name][0]
            if request.adaptive_native_threads:
                # A native batch shares its memory among threads. Choose its
                # execution width from capacity free *now*, including work
                # already held by this same route. A stale pre-queue width
                # must not block useful work after external competition grows.
                available = min(
                    effective_cpu_slots - self._cpu_in_use,
                    min(self.native_thread_slots, effective_cpu_slots) - self._native_threads_in_use,
                )
                if request.native_thread_limit is not None:
                    available = min(available, request.native_thread_limit)
                request.cpu_slots = request.native_threads = max(1, available)
        fitting_routes = tuple(
            route_name
            for route_name in ordered_routes
            if self._queues[route_name]
            and self._fits(self._queues[route_name][0], snapshot, effective_cpu_slots)
        )
        for route_name in fitting_routes:
            if self._queues[route_name][0].draining_from is not None:
                return route_name
        # A route that already owns resources must not reacquire the last
        # available capacity ahead of a fitting route that has received none.
        # This prevents a stream of large PDF jobs from starving DOCX/images.
        for route_name in fitting_routes:
            if self._metrics[route_name].cpu_slots == 0:
                return route_name
        if fitting_routes:
            return fitting_routes[0]
        return None

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
        """Wait for and hold one bounded route admission.

        ``memory_bytes`` is the original aggregate reservation API.  When any
        resource components are supplied, it is an optional total check (not
        an additional charge); the components are charged exactly once as
        ``resident + transient``. Temporary disk bytes have their own budget;
        callers using tmpfs must also declare the corresponding RAM demand.
        A ``resident_key`` shares the
        resident component between concurrent phases of one route.
        """

        self.checkpoint()
        if cancellation is not None:
            cancellation.checkpoint()
        if route_name not in self._queues:
            raise ValueError(f"route is not coordinated: {route_name}")
        try:
            aggregate_memory = 0 if memory_bytes is None else int(memory_bytes)
            requested_resident = int(resident_bytes)
            requested_temp = int(temp_bytes)
            requested_native = int(native_threads)
            requested_io = int(io_slots)
            requested_gpu = int(gpu_bytes)
            requested_transient = (
                None if transient_bytes is None else int(transient_bytes)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("global resource reservations must be integers") from exc
        if aggregate_memory < 0:
            raise ValueError("global memory reservation cannot be negative")
        if min(requested_resident, requested_temp, requested_native, requested_io, requested_gpu) < 0:
            raise ValueError("global resource components cannot be negative")
        if (requested_gpu and self._gpu_available_probes
                and not self._gpu_monitor_running()):
            # An unmonitored GPU request needs a current device observation;
            # an unrelated CPU/RAM request must not invoke driver telemetry.
            self._sample_gpu_resources()
        if requested_transient is not None and requested_transient < 0:
            raise ValueError("global transient reservation cannot be negative")
        component_mode = (
            requested_resident > 0
            or requested_temp > 0
            or requested_native > 0
            or requested_transient is not None
            or phase is not None
            or resident_key is not None
        )
        if not component_mode:
            requested_transient = max(1, aggregate_memory)
            requested_resident = 0
            requested_temp = 0
        else:
            if requested_transient is None:
                requested_transient = max(0, aggregate_memory - requested_resident - requested_temp)
            component_total = requested_resident + requested_transient + requested_temp
            if aggregate_memory not in (0, component_total):
                raise ValueError(
                    "memory_bytes is an aggregate alias when resource components "
                    "are supplied; it must equal their sum"
                )
            aggregate_memory = component_total
        requested_memory = requested_resident + requested_transient
        if _renewing_from is not None and (
            requested_memory or requested_temp or requested_gpu
            or _draining_from is not None or _adaptive_native_threads
        ):
            raise ValueError("execution renewal cannot grow memory or change its native width")
        requested_cpu = int(cpu_slots)
        if requested_cpu < 0:
            raise ValueError("CPU slots cannot be negative")
        if self._automatic_memory_budget or self._automatic_cpu_slots:
            with self._condition:
                self._observe_live_resources()
        memory_ceiling = self._last_memory_budget if self._automatic_memory_budget else self.memory_budget_bytes
        cpu_ceiling = self._last_cpu_capacity if self._automatic_cpu_slots else self.cpu_slots
        if requested_memory > memory_ceiling:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_memory} bytes but the global "
                f"budget is {memory_ceiling} bytes"
            )
        route_budget = self._route_memory_budgets.get(route_name)
        if route_budget is not None and requested_memory > route_budget:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_memory} bytes but its route "
                f"budget is {route_budget} bytes"
            )
        # A backend's already granted width remains real after a temporary
        # quota/affinity contraction. Renewal waits for it without allocating
        # new memory; explicit ceilings and _fits' live capacity still apply.
        if requested_cpu > cpu_ceiling and (
            _renewing_from is None or not self._automatic_cpu_slots
        ):
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_cpu} CPU slots but only "
                f"{cpu_ceiling} are configured"
            )
        if requested_native > self.native_thread_slots and (
            _renewing_from is None or not self._automatic_native_thread_slots
        ):
            raise NativeThreadBudgetExceeded(
                f"{route_name} requires {requested_native} native threads but only "
                f"{self.native_thread_slots} are configured"
            )
        if requested_temp and self.limits.temp_budget_bytes is None:
            with self._condition:
                materialized_temp = sum(
                    active.temp_materialized_bytes for active in self._active_reservations.values()
                )
                self.temp_budget_bytes = shutil.disk_usage(tempfile.gettempdir()).free + materialized_temp
        if requested_temp > self.temp_budget_bytes:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_temp} temporary bytes but only "
                f"{self.temp_budget_bytes} are configured"
            )
        if requested_io > self._io_capacity(str(io_device or "default")):
            raise MemoryBudgetExceeded("one request exceeds the device I/O concurrency limit")
        if requested_gpu > self._gpu_capacity.get(str(gpu_device or "default"), 0):
            raise MemoryBudgetExceeded("GPU device is unregistered or its memory budget is exceeded")

        normalized_phase: str | None
        if phase is None:
            normalized_phase = None
        else:
            normalized_phase = str(phase).strip()
            if not normalized_phase:
                raise ValueError("global resource phase cannot be empty")
        normalized_resident_key: str | None
        if resident_key is None:
            normalized_resident_key = None
        else:
            normalized_resident_key = str(resident_key).strip()
            if not normalized_resident_key:
                raise ValueError("global resident key cannot be empty")

        started = self._clock()
        request = _Request(
            route_name,
            requested_memory,
            requested_cpu,
            started,
            int(started * 1_000_000_000),
            resident_bytes=requested_resident,
            transient_bytes=requested_transient,
            temp_bytes=requested_temp,
            native_threads=requested_native,
            phase=normalized_phase,
            resident_key=normalized_resident_key,
            io_slots=requested_io,
            io_device=None if io_device is None else str(io_device),
            gpu_bytes=requested_gpu,
            gpu_device=None if gpu_device is None else str(gpu_device),
            draining_from=_draining_from,
            adaptive_native_threads=_adaptive_native_threads,
            native_thread_limit=_native_thread_limit,
        )
        route_index = self.route_order.index(route_name)
        headroom_blocked_since: float | None = None
        try:
            with self._condition:
                if _renewing_from is not None and (
                    self._active_reservations.get(id(_renewing_from)) is not _renewing_from
                    or _renewing_from.route_name != route_name
                ):
                    raise RuntimeError("execution renewal requires its original live lease")
                if _draining_from is not None:
                    if (
                        self._active_reservations.get(id(_draining_from)) is not _draining_from
                        or _draining_from.route_name != route_name
                        or requested_memory or requested_temp or requested_gpu
                        or max(requested_cpu, requested_native, requested_io) > 1
                    ):
                        raise ValueError("drain admission must finalize a live lease without resource growth")
                    self._queues[route_name].appendleft(request)
                else:
                    self._queues[route_name].append(request)
                request.queued = True
                self._condition.notify_all()
                while True:
                    if self._checkpoint_callback is not None:
                        self._checkpoint_callback()
                    if cancellation is not None:
                        cancellation.checkpoint()
                    if self.cancellation.is_cancelled or (
                        cancellation is not None and cancellation.is_cancelled
                    ):
                        raise CancellationRequested(
                            f"{route_name} cancelled while waiting for global resources"
                        )
                    if (_renewing_from is not None and
                            self._active_reservations.get(id(_renewing_from)) is not _renewing_from):
                        raise RuntimeError("original lease was released while renewing execution")
                    snapshot, effective_cpu_slots = self._observe_live_resources()
                    selected_route = self._next_route(snapshot, effective_cpu_slots)
                    if (
                        selected_route == route_name
                        and self._queues[route_name]
                        and self._queues[route_name][0] is request
                    ):
                        self.cancellation.checkpoint()
                        if cancellation is not None:
                            cancellation.checkpoint()
                        self._grant_request_locked(request, route_index)
                        self._condition.notify_all()
                        break

                    if not request.waited:
                        request.waited = True
                        self._metrics[route_name].waits += 1

                    # Active bounded jobs own resources legitimately. Apply the
                    # resource-wait timeout only while no work is active and no
                    # queued request can start. Worker execution timeouts are
                    # owned by their route and are never conflated here.
                    remaining: float | None = None
                    if (selected_route is None and self._active_execution_requests == 0
                            and self.limits.wait_timeout_seconds is not None):
                        now = self._clock()
                        if headroom_blocked_since is None:
                            headroom_blocked_since = now
                        remaining = self.limits.wait_timeout_seconds - (
                            now - headroom_blocked_since
                        )
                        if remaining <= 0:
                            route_memory_blocked = route_budget is not None and (
                                self._metrics[route_name].reserved_bytes
                                + self._resident_charge_for_request_locked(request)
                                + request.transient_bytes > route_budget
                            )
                            reason = "memory" if self._memory_pressure or route_memory_blocked else "cpu"
                            raise ResourceWaitTimeout(
                                f"{route_name} timed out waiting for live system "
                                f"headroom; available_physical="
                                f"{snapshot.available_physical}, "
                                f"available_commit={snapshot.available_commit}, "
                                f"reserved={self._reserved_bytes}, "
                                f"cpu_in_use={self._cpu_in_use}, "
                                f"cpu_load={self._last_cpu_load}, "
                                f"effective_cpu_slots="
                                f"{self._last_effective_cpu_slots}, "
                                f"resident={self._resident_bytes}, "
                                f"transient={self._transient_bytes}, "
                                f"temp={self._temp_bytes}, "
                                f"native_threads={self._native_threads_in_use}"
                                ,
                                reason=reason,
                            )
                    else:
                        headroom_blocked_since = None
                    wait_seconds = self.limits.poll_interval_seconds
                    if cancellation is not None:
                        wait_seconds = min(wait_seconds, 0.1)
                    if remaining is not None:
                        wait_seconds = min(wait_seconds, remaining)
                    self._condition.wait(wait_seconds)
        except BaseException as admission_error:
            self._cleanup_request(request, admission_error)
            raise

        primary_error: BaseException | None = None
        grant = ResourceGrant(self, request, cancellation)
        grant_token = _CURRENT_RESOURCE_GRANT.set(grant)
        try:
            yield grant
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                try:
                    grant.release_cpu()
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    if request.admitted and not request.released:
                        self._cleanup_request(request, primary_error or cleanup_error)
            finally:
                _CURRENT_RESOURCE_GRANT.reset(grant_token)
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                primary_error.add_note(f"execution release failed: {cleanup_error}")

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
_CURRENT_RESOURCE_GRANT: ContextVar["ResourceGrant | None"] = ContextVar(
    "neocortex_resource_grant", default=None
)
_DRAINING_RESOURCE_GRANT: ContextVar["ResourceGrant | None"] = ContextVar(
    "neocortex_draining_resource_grant", default=None
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


class ResourceGrant:
    """A real accounting lease with a renewable execution component."""

    def __init__(self, coordinator, request, cancellation=None):
        self.coordinator = coordinator
        self._request = request
        self._cancellation = cancellation
        self._execution_demand = (request.cpu_slots, request.native_threads, request.io_slots)
        self._resumed_context: tuple[Context, Any] | None = None
        self._resumed_grant: ResourceGrant | None = None
        self._owner_grant: ResourceGrant | None = None

    @property
    def cpu_slots(self) -> int:
        return self._request.cpu_slots if self._resumed_grant is None else self._resumed_grant.cpu_slots

    @property
    def native_threads(self) -> int:
        return self._request.native_threads if self._resumed_grant is None else self._resumed_grant.native_threads

    @property
    def native_env(self) -> dict[str, str]:
        threads = str(max(1, self.native_threads))
        return dict.fromkeys((
            "OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
            "BLIS_NUM_THREADS",
        ), threads)

    def subprocess_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment.update(self.native_env)
        return environment

    def check_cancellation(self) -> None:
        """Check both scope and local owner without changing this lease."""
        self.coordinator.checkpoint()
        if self._cancellation is not None:
            self._cancellation.checkpoint()
        if self._resumed_grant is not None and self._resumed_grant._cancellation is not None:
            self._resumed_grant._cancellation.checkpoint()

    def shrink_transient_bytes(self, new_total: int) -> None:
        """Return finished workspace while retaining the result's own bytes.

        This is a nonblocking reduction only. The owner calls it after the
        discarded workspace is no longer reachable; a result remains charged
        until its consumption finishes and the original lease is closed.
        """
        amount = int(new_total)
        with self.coordinator._condition:
            request = self._request
            if not request.admitted or request.released:
                raise RuntimeError("cannot shrink a released resource grant")
            if amount < 0 or amount > request.transient_bytes:
                raise ValueError("transient reduction cannot grow a reservation")
            released = request.transient_bytes - amount
            request.transient_bytes = amount
            request.memory_bytes -= released
            self.coordinator._reserved_bytes -= released
            self.coordinator._transient_bytes -= released
            metrics = self.coordinator._metrics[request.route_name]
            metrics.reserved_bytes -= released
            metrics.transient_bytes -= released
            self.coordinator._materialized_credit_locked()
            self.coordinator._condition.notify_all()

    def release_cpu(self) -> None:
        """Idempotently release execution while retaining resident/result bytes."""
        resumed = self._resumed_context
        if resumed is not None:
            self._resumed_context = None
            self._resumed_grant = None
            context, admission = resumed
            context.run(admission.__exit__, None, None, None)
        self.coordinator._release_execution(self._request)

    @contextmanager
    def drain_scope(self):
        """Mark bounded owner consumption of this already computed result.

        A checkpoint here uses one CPU/native/I/O unit and may proceed under
        pressure so the owner can persist or discard its retained bytes. This
        scope never authorizes new RAM, temporary or GPU allocations.
        """
        token = _DRAINING_RESOURCE_GRANT.set(self)
        try:
            yield self
        finally:
            _DRAINING_RESOURCE_GRANT.reset(token)

    @contextmanager
    def drain_admission(self, *, io_slots: int = 1, io_device: str | None = None,
                        phase: str | None = None):
        """Finalize a retained resident buffer with one bounded execution unit.

        Use this for the last owner batch after worker iteration has ended.
        The original residency remains charged until its owner closes it;
        this admission authorizes no additional RAM, temporary space or GPU.
        """
        self.check_cancellation()
        if not 0 <= io_slots <= 1:
            raise ValueError("drain admission permits at most one I/O unit")
        with self.coordinator.admit(
            self._request.route_name, 0, cpu_slots=1, native_threads=1,
            transient_bytes=0, io_slots=io_slots, io_device=io_device,
            phase=phase or (self._request.phase or "resident") + ":drain",
            cancellation=self._cancellation, _draining_from=self._request,
        ) as grant:
            yield grant

    def checkpoint(self, *, drain: bool | None = None,
                   cancellation: CancellationToken | None = None) -> None:
        """Yield execution and renegotiate, optionally to finish a result.

        ``drain=True`` is only for bounded finalization of existing work, not
        another inference/parser task. Elastic owner consumption marks this
        automatically. Cancellation still takes precedence over finalization.
        """
        self.check_cancellation()
        if cancellation is not None:
            cancellation.checkpoint()
        if not self._request.admitted or self._request.released:
            raise RuntimeError("cannot renew a released resource grant")
        cpu, native, io = self._execution_demand
        draining = _DRAINING_RESOURCE_GRANT.get() is self if drain is None else drain
        if draining:
            # Finalization is a new bounded owner phase even if the producer
            # was a CPU0/native0 supervisor or had no I/O work of its own.
            cpu, native, io = 1, 1, 1
        if not (cpu or native or io):
            return
        self.release_cpu()
        admission = self.coordinator.admit(
            self._request.route_name, 0, cpu_slots=cpu, native_threads=native,
            transient_bytes=0, io_slots=io, io_device=self._request.io_device,
            phase=(self._request.phase or "work") + ":checkpoint",
            cancellation=(self._cancellation if cancellation is None else
                          _CombinedCancellationToken(self._cancellation, cancellation)),
            _draining_from=self._request if draining else None,
            _renewing_from=None if draining else self._request,
        )
        # A lease moves sequentially between preparation, worker execution and
        # result consumption. Keep the renewal's ContextVar token in its own
        # context so another owner can release it without changing either
        # owner's ambient grant or resetting a token in the wrong context.
        context = copy_context()
        grant = context.run(admission.__enter__)
        grant._owner_grant = self
        self._resumed_context = context, admission
        self._resumed_grant = grant

    def register_process(self, pid: int, start_time_ticks: int | None = None) -> tuple[int, int]:
        """Bind a verified descendant identity for measured memory attribution."""
        def identity(process_id: int) -> tuple[int, int]:
            raw = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2:].split()
            return int(fields[1]), int(fields[19])

        process_id = int(pid)
        if process_id <= 0 or process_id == os.getpid():
            raise ValueError("a process lease binds a child, not the entire coordinator")
        parent, started = identity(process_id)
        if start_time_ticks is not None and int(start_time_ticks) != started:
            raise ValueError("process identity changed before resource registration")
        current = parent
        visited = {process_id}
        while current != os.getpid():
            if current <= 1 or current in visited:
                raise ValueError("resource process is outside this execution tree")
            visited.add(current)
            current, _ = identity(current)
        if identity(process_id) != (parent, started):
            raise ValueError("process identity changed during resource registration")
        result = process_id, started
        with self.coordinator._condition:
            if not self._request.admitted or self._request.released:
                raise RuntimeError("cannot register a process on a released grant")
            self._request.process_identities.add(result)
        # Observation may hold the sampler lock while reading proc. Keep this
        # handoff outside the accounting lock; only live leases can earn credit.
        sampler = self.coordinator._default_sampler
        if sampler is not None:
            sampler._track_verified_process(result)
        return result

    def resize_temp_bytes(self, new_total: int, *, directory: str | Path | None = None,
                          file_descriptor: int | None = None) -> None:
        """Reserve spool growth before writing, without holding a blocking wait.

        Pass the owned file descriptor to credit its kernel-observed length;
        bytes still buffered in Python remain pending. Without a descriptor,
        all reserved bytes remain conservative future promises. Growth uses
        that descriptor's filesystem availability. A failure leaves the lease
        unchanged so the owner can publish an explicit bounded result.
        """
        amount = int(new_total)
        if amount < 0:
            raise ValueError("temporary resource size cannot be negative")
        self.coordinator.cancellation.checkpoint()
        if self._cancellation is not None:
            self._cancellation.checkpoint()
        with self.coordinator._condition:
            request = self._request
            if not request.admitted or request.released:
                raise RuntimeError("cannot resize a released resource grant")
            materialized = 0
            identity = None
            if file_descriptor is not None:
                observed = os.fstat(file_descriptor)
                if not stat.S_ISREG(observed.st_mode):
                    raise ValueError("temporary resource descriptor must be a regular file")
                identity = observed.st_dev, observed.st_ino
                if request.temp_file_identity is not None and request.temp_file_identity != identity:
                    raise ValueError("temporary resource descriptor changed identity")
                if any(other is not request and other.temp_bytes
                       and other.temp_file_identity == identity
                       for other in self.coordinator._active_reservations.values()):
                    raise ValueError("temporary file already belongs to another live grant")
                materialized = min(request.temp_bytes, max(0, observed.st_size))
            delta = amount - request.temp_bytes
            future = self.coordinator._temp_bytes + delta
            if delta > 0:
                explicit = self.coordinator.limits.temp_budget_bytes
                if explicit is not None and future > explicit:
                    raise MemoryBudgetExceeded("temporary storage budget exceeded")
                if file_descriptor is None:
                    free = shutil.disk_usage(directory or tempfile.gettempdir()).free
                else:
                    filesystem = os.fstatvfs(file_descriptor)
                    free = filesystem.f_bavail * filesystem.f_frsize
                pending_other = sum(
                    max(0, other.temp_bytes - other.temp_materialized_bytes)
                    for other in self.coordinator._active_reservations.values()
                    if other is not request
                )
                if max(0, amount - materialized) + pending_other > free:
                    raise MemoryBudgetExceeded("insufficient filesystem capacity for temporary growth")
                if explicit is None:
                    other_materialized = sum(
                        other.temp_materialized_bytes
                        for other in self.coordinator._active_reservations.values()
                        if other is not request
                    )
                    self.coordinator.temp_budget_bytes = other_materialized + materialized + free
            request.temp_materialized_bytes = min(amount, materialized)
            request.temp_file_identity = identity if amount else None
            request.temp_bytes = amount
            self.coordinator._temp_bytes = future
            self.coordinator._peak_temp_bytes = max(self.coordinator._peak_temp_bytes, future)
            metrics = self.coordinator._metrics[request.route_name]
            metrics.temp_bytes += delta
            metrics.peak_temp_bytes = max(metrics.peak_temp_bytes, metrics.temp_bytes)
            self.coordinator._condition.notify_all()


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
