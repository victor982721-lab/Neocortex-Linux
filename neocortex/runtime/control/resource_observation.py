"""Pure observation and adaptive-capacity helpers for resource coordination.

No function in this module owns coordinator state.  Probes are converted into
immutable :class:`ResourceSample` values here; the coordinator remains the
only place that latches pressure or publishes a sample to waiters.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .cpu_runtime import effective_cpu_count
from .memory_runtime import MemorySnapshot
from .resource_models import GIB, MIB, ResourceSample


_PROC_MEMORY_PRESSURE = Path("/proc/pressure/memory")


def linux_memory_pressure_sample(
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
    if all(value is None for value in (some_percent, full_percent, some_total_us, full_total_us)):
        return None
    return ResourceSample(
        memory_pressure_some_percent=some_percent,
        memory_pressure_full_percent=full_percent,
        memory_pressure_some_total_us=some_total_us,
        memory_pressure_full_total_us=full_total_us,
    )


def coerce_resource_sample(value: object) -> ResourceSample | None:
    """Accept a public sample, memory snapshot, or bounded mapping.

    The mapping form is deliberately small and useful for tests/adapters that
    already expose JSON-like telemetry. Unknown keys are ignored; malformed
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


def memory_snapshot_from_resource_sample(
    sample: ResourceSample | None,
) -> MemorySnapshot | None:
    """Return a memory snapshot only when at least one memory field exists."""

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


def adaptive_memory_budget(total_physical: int | None) -> int:
    """Compute the automatic budget while preserving live headroom."""

    if total_physical is None or total_physical <= 0:
        return GIB
    return max(1, total_physical - adaptive_memory_headroom(total_physical))


def adaptive_memory_headroom(total_physical: int | None) -> int:
    if total_physical is None or total_physical <= 0:
        return 64 * MIB
    return max(1, min(512 * MIB, total_physical // 32))


def adaptive_cpu_slots(
    cpu_count_probe=effective_cpu_count,
) -> int:
    return max(1, cpu_count_probe())


def update_pressure_locked(
    coordinator,
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
    previous = coordinator._previous_sample
    if current_sample.available_physical is not None and previous.available_physical is not None:
        coordinator._last_available_memory_delta = (
            current_sample.available_physical - previous.available_physical
        )
    else:
        coordinator._last_available_memory_delta = None
    if current_sample.available_commit is not None and previous.available_commit is not None:
        coordinator._last_available_commit_delta = (
            current_sample.available_commit - previous.available_commit
        )
    else:
        coordinator._last_available_commit_delta = None
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
    coordinator._last_memory_pressure_delta = (
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
    coordinator._last_memory_pressure_total_delta_us = (
        None
        if not current_total or not previous_total
        else max(current_total) - max(previous_total)
    )
    coordinator._last_memory_pressure_value = pressure_value
    coordinator._previous_sample = current_sample
    coordinator._sample_count += 1

    available_memory = snapshot.available_physical
    available_commit = snapshot.available_commit
    outstanding_growth = max(
        0, coordinator._reserved_bytes - coordinator._materialized_credit_locked()
    )
    memory_low = (
        available_memory is not None
        and available_memory < coordinator.min_free_memory_bytes + outstanding_growth
    ) or (
        available_commit is not None
        and available_commit < coordinator.min_free_commit_bytes + outstanding_growth
    )
    memory_recovered = (
        available_memory is None
        or available_memory
        >= coordinator.min_free_memory_bytes
        + outstanding_growth
        + coordinator._memory_hysteresis_bytes
    ) and (
        available_commit is None
        or available_commit
        >= coordinator.min_free_commit_bytes
        + outstanding_growth
        + coordinator._memory_hysteresis_bytes
    )
    pressure_high = (
        pressure_value is not None
        and pressure_value >= coordinator.limits.memory_pressure_high_percent
    )
    pressure_recovered = (
        pressure_value is None
        or pressure_value <= coordinator.limits.memory_pressure_recovery_percent
    )
    if coordinator._memory_pressure:
        if memory_recovered and pressure_recovered:
            coordinator._memory_pressure = False
            coordinator._memory_pressure_from_psi = False
            coordinator._pressure_events += 1
    elif memory_low or pressure_high:
        coordinator._memory_pressure = True
        coordinator._memory_pressure_from_psi = pressure_high
        coordinator._pressure_events += 1

    # The built-in sampler observes the whole system, including this
    # process; treating its own high utilization as external competition
    # would create the very self-throttling loop this coordinator is meant
    # to avoid. Both explicit CPU inputs retain the load-cap behavior;
    # the default sampler cannot prove recovery of a missing owned signal.
    if (
        cpu_load_is_explicit
        and cpu_load is not None
        and cpu_load >= coordinator.max_cpu_load_percent
    ):
        if not coordinator._cpu_pressure:
            coordinator._cpu_pressure = True
            coordinator._pressure_events += 1
    elif (
        cpu_load_is_explicit
        and coordinator._cpu_pressure
        and cpu_load is not None
        and cpu_load <= coordinator.max_cpu_load_percent - coordinator.limits.cpu_hysteresis_percent
    ):
        coordinator._cpu_pressure = False
        coordinator._pressure_events += 1
    io_values = (
        current_sample.io_pressure_some_percent,
        current_sample.io_pressure_full_percent,
    )
    io_pressure = max((x for x in io_values if x is not None), default=None)
    if io_pressure is not None:
        if coordinator._io_pressure:
            coordinator._io_pressure = io_pressure > coordinator.limits.io_pressure_recovery_percent
        else:
            coordinator._io_pressure = io_pressure >= coordinator.limits.io_pressure_high_percent


def observe_live_resources(coordinator):
    sample = coordinator._read_resource_sample()
    snapshot = coordinator._resource_snapshot(sample)
    if snapshot.available_physical is not None:
        coordinator._min_available_memory = (
            snapshot.available_physical
            if coordinator._min_available_memory is None
            else min(coordinator._min_available_memory, snapshot.available_physical)
        )
    if snapshot.available_commit is not None:
        coordinator._min_available_commit = (
            snapshot.available_commit
            if coordinator._min_available_commit is None
            else min(coordinator._min_available_commit, snapshot.available_commit)
        )

    cpu_load: float | None
    if sample is not None and sample.cpu_load_percent is not None:
        cpu_load = sample.cpu_load_percent
    else:
        now = coordinator._clock()
        should_sample_cpu = (
            not coordinator._using_default_cpu_probe
            or coordinator._last_cpu_sample_at is None
            or now - coordinator._last_cpu_sample_at >= coordinator.limits.sample_interval_seconds
        )
        if should_sample_cpu:
            try:
                cpu_load = coordinator._cpu_load_probe()
            except (OSError, RuntimeError, TypeError, ValueError):
                cpu_load = None
            coordinator._last_cpu_sample_at = now
        else:
            cpu_load = coordinator._last_cpu_load
    if cpu_load is not None:
        try:
            cpu_load = float(cpu_load)
        except (TypeError, ValueError, OverflowError):
            cpu_load = None
        if cpu_load is not None and math.isfinite(cpu_load):
            cpu_load = max(0.0, min(100.0, cpu_load))
            coordinator._max_cpu_load = (
                cpu_load
                if coordinator._max_cpu_load is None
                else max(coordinator._max_cpu_load, cpu_load)
            )
        else:
            cpu_load = None

    coordinator._last_memory_budget = coordinator._effective_memory_budget(snapshot)
    try:
        detected = max(
            1,
            math.ceil(
                sample.effective_cpu_capacity
                if sample is not None and sample.effective_cpu_capacity is not None
                else coordinator._effective_cpu_probe()
            ),
        )
    except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
        detected = coordinator._last_cpu_capacity
    # An explicit CPU count is a ceiling, never permission to exceed a
    # newly narrowed affinity/quota. Capacity is already known on the
    # first sample, even before two observations can attribute CPU use.
    cpu_capacity = (
        detected if coordinator._automatic_cpu_slots else min(coordinator.cpu_slots, detected)
    )
    if coordinator._automatic_native_thread_slots:
        coordinator.native_thread_slots = cpu_capacity
    coordinator._last_cpu_capacity = cpu_capacity
    cpu_load_is_explicit = not coordinator._using_default_cpu_probe or (
        coordinator._resource_probe is not None
        and sample is not None
        and sample.cpu_load_percent is not None
    )
    coordinator._update_pressure_locked(
        snapshot, sample, cpu_load, cpu_load_is_explicit=cpu_load_is_explicit
    )
    # The built-in whole-system sample includes the work already charged
    # to _cpu_in_use.  Reducing the total capacity by that same load would
    # count our active jobs twice.  Keep it as telemetry; only an explicit
    # caller-owned load probe can reduce the affinity/cgroup capacity.
    admission_cpu_load = cpu_load if cpu_load_is_explicit else None
    effective_cpu_slots = coordinator._effective_cpu_capacity(admission_cpu_load, cpu_capacity)
    if (
        sample is not None
        and sample.external_cpu_cores is not None
        and math.isfinite(sample.external_cpu_cores)
        and sample.external_cpu_cores >= 0
    ):
        coordinator._last_external_cpu_cores = sample.external_cpu_cores
        effective_capacity = (
            float(cpu_capacity)
            if sample.effective_cpu_capacity is None
            else min(float(cpu_capacity), sample.effective_cpu_capacity)
        )
        available_cores = max(0.0, effective_capacity - sample.external_cpu_cores)
        effective_cpu_slots = min(effective_cpu_slots, max(0, math.ceil(available_cores)))
        saturated = available_cores <= 0.01
        recovered = available_cores >= max(
            0.05, effective_capacity * coordinator.limits.cpu_hysteresis_percent / 100
        )
        if saturated and not coordinator._cpu_pressure:
            coordinator._cpu_pressure = True
            coordinator._pressure_events += 1
        elif recovered and coordinator._cpu_pressure and not cpu_load_is_explicit:
            coordinator._cpu_pressure = False
            coordinator._pressure_events += 1
    elif coordinator._last_external_cpu_cores is not None and not cpu_load_is_explicit:
        # Losing tree attribution is not evidence that competitors left.
        effective_cpu_slots = min(effective_cpu_slots, coordinator._last_effective_cpu_slots)
    if cpu_load is None and coordinator._cpu_pressure:
        effective_cpu_slots = min(
            effective_cpu_slots,
            max(1, coordinator._last_effective_cpu_slots),
        )
    coordinator._min_effective_cpu_slots = min(
        coordinator._min_effective_cpu_slots, effective_cpu_slots
    )
    coordinator._last_cpu_load = cpu_load
    coordinator._last_effective_cpu_slots = effective_cpu_slots
    return snapshot, effective_cpu_slots
