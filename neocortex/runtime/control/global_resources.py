"""Fair, adaptive resource coordination shared by concurrent content routes."""

from __future__ import annotations

import math
import os
import threading
import time
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
    # These fields are intentionally additive.  The first six fields are the
    # long-standing positional contract consumed by application projections.
    native_thread_slots: int | None = None
    cpu_hysteresis_percent: float = 5.0
    memory_hysteresis_bytes: int | None = None
    sample_interval_seconds: float = 0.25
    temp_budget_bytes: int | None = None
    memory_pressure_high_percent: float = 10.0
    memory_pressure_recovery_percent: float = 5.0


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


def _adaptive_memory_budget(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return GIB
    # Live admission still preserves physical and commit headroom.  A quarter
    # of RAM under-admitted the measured PDF workload on 12-16 GiB hosts: four
    # bounded workers collapsed to two while CPU stayed mostly idle.  Permit a
    # larger aggregate reservation, then let _fits reduce concurrency when the
    # processes actually consume that headroom.
    # Do not impose a product-wide upper bound here.  The cgroup-bounded
    # ``total_physical`` value is already the effective memory ceiling; the
    # live headroom check below remains the safety boundary.
    return max(GIB, total_physical * 3 // 8)


def _adaptive_memory_headroom(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return GIB
    return max(GIB, min(3 * GIB, total_physical // 6))


def _adaptive_cpu_slots() -> int:
    detected = effective_cpu_count()
    # One CPU remains available to the host, but large affinity/cgroup quotas
    # are not silently truncated to the historical eight-slot preset.
    return max(1, detected - 1)


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
    ):
        if not route_order or len(route_order) != len(set(route_order)):
            raise ValueError("route_order must contain unique route names")
        if limits.wait_timeout_seconds < 0:
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

        self._resource_probe = resource_probe
        self._clock = clock or time.monotonic
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
            self.memory_budget_bytes
            if limits.temp_budget_bytes is None
            else limits.temp_budget_bytes
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

    def cancel(self) -> None:
        """Wake all queued admissions so cancellation is observed immediately."""

        self.cancellation.cancel()
        with self._condition:
            self._condition.notify_all()

    def _read_resource_sample(self) -> ResourceSample | None:
        probe = self._resource_probe
        if probe is None:
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
        memory_low = (
            available_memory is not None
            and available_memory < self.min_free_memory_bytes + self._reserved_bytes
        ) or (
            available_commit is not None
            and available_commit < self.min_free_commit_bytes + self._reserved_bytes
        )
        memory_recovered = (
            available_memory is None
            or available_memory
            >= self.min_free_memory_bytes
            + self._reserved_bytes
            + self._memory_hysteresis_bytes
        ) and (
            available_commit is None
            or available_commit
            >= self.min_free_commit_bytes
            + self._reserved_bytes
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
        if self._automatic_cpu_slots:
            try:
                detected = max(1, int(self._effective_cpu_probe()))
            except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
                detected = self.cpu_slots + 1
            cpu_capacity = max(1, detected - 1)
        else:
            cpu_capacity = self.cpu_slots
        if self._automatic_native_thread_slots:
            self.native_thread_slots = cpu_capacity
        self._last_cpu_capacity = cpu_capacity
        cpu_load_is_explicit = not self._using_default_cpu_probe or (
            sample is not None and sample.cpu_load_percent is not None
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
        resident_charge = self._resident_charge_for_request_locked(request)
        future_memory = (
            self._reserved_bytes
            + resident_charge
            + request.transient_bytes
            + request.temp_bytes
        )
        if future_memory > self._last_memory_budget:
            return False
        if self._memory_pressure or self._cpu_pressure:
            return False
        if self._cpu_in_use + request.cpu_slots > effective_cpu_slots:
            return False
        native_capacity = self.native_thread_slots
        if native_capacity is not None and (
            self._native_threads_in_use + request.native_threads > native_capacity
        ):
            return False
        if request.temp_bytes + self._temp_bytes > self.temp_budget_bytes:
            return False
        physical_ok = (
            snapshot.available_physical is None
            or snapshot.available_physical
            >= self.min_free_memory_bytes + future_memory
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
                + request.temp_bytes
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
        except BaseException:
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
        released_memory = resident_released + request.transient_bytes + request.temp_bytes
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
        except BaseException:
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
    ):
        """Wait for and hold one bounded route admission.

        ``memory_bytes`` is the original aggregate reservation API.  When any
        resource components are supplied, it is an optional total check (not
        an additional charge); the components are charged exactly once as
        ``resident + transient + temp``.  A ``resident_key`` shares the
        resident component between concurrent phases of one route.
        """

        self.cancellation.checkpoint()
        if cancellation is not None:
            cancellation.checkpoint()
        if route_name not in self._queues:
            raise ValueError(f"route is not coordinated: {route_name}")
        try:
            aggregate_memory = 0 if memory_bytes is None else int(memory_bytes)
            requested_resident = int(resident_bytes)
            requested_temp = int(temp_bytes)
            requested_native = int(native_threads)
            requested_transient = (
                None if transient_bytes is None else int(transient_bytes)
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("global resource reservations must be integers") from exc
        if aggregate_memory < 0:
            raise ValueError("global memory reservation cannot be negative")
        if requested_resident < 0 or requested_temp < 0 or requested_native < 0:
            raise ValueError("global resource components cannot be negative")
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
            requested_transient = 0 if requested_transient is None else requested_transient
            component_total = requested_resident + requested_transient + requested_temp
            if aggregate_memory not in (0, component_total):
                raise ValueError(
                    "memory_bytes is an aggregate alias when resource components "
                    "are supplied; it must equal their sum"
                )
            if component_total < 1:
                requested_transient = 1
                component_total = 1
            aggregate_memory = component_total
        requested_memory = aggregate_memory
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
        if requested_native > self.native_thread_slots:
            raise NativeThreadBudgetExceeded(
                f"{route_name} requires {requested_native} native threads but only "
                f"{self.native_thread_slots} are configured"
            )
        if requested_temp > self.temp_budget_bytes:
            raise MemoryBudgetExceeded(
                f"{route_name} requires {requested_temp} temporary bytes but only "
                f"{self.temp_budget_bytes} are configured"
            )

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
        )
        route_index = self.route_order.index(route_name)
        headroom_blocked_since: float | None = None
        try:
            with self._condition:
                self._queues[route_name].append(request)
                request.queued = True
                self._condition.notify_all()
                while True:
                    if self.cancellation.is_cancelled or (
                        cancellation is not None and cancellation.is_cancelled
                    ):
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
                    if selected_route is None and self._active_requests == 0:
                        now = self._clock()
                        if headroom_blocked_since is None:
                            headroom_blocked_since = now
                        remaining = self.limits.wait_timeout_seconds - (
                            now - headroom_blocked_since
                        )
                        if remaining <= 0:
                            reason = "memory" if self._memory_pressure else "cpu"
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
        try:
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if request.admitted and not request.released:
                self._cleanup_request(request, primary_error)

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
            )


# endregion [02]


# region [03] Route memory gate adapter


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

    @contextmanager
    def admit(self, estimated_bytes: int):
        admission = (
            self.coordinator.admit(self.route_name, estimated_bytes, 1)
            if self.cancellation is None
            else self.coordinator.admit(
                self.route_name, estimated_bytes, 1, cancellation=self.cancellation
            )
        )
        with admission:
            yield
# endregion [03]
