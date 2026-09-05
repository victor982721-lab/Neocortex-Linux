"""Low-overhead system CPU load sampling without a runtime dependency."""

from __future__ import annotations

import ctypes
import math
import os
from dataclasses import dataclass
from pathlib import Path

from .cgroup_runtime import cgroup_cpu_snapshot


@dataclass(frozen=True, slots=True)
class CpuCapacitySnapshot:
    system_cpu_count: int | None
    affinity_cpu_count: int | None
    cgroup_quota_cpus: float | None
    cgroup_cpuset_count: int | None
    effective_cpus: float


def cpu_capacity_snapshot() -> CpuCapacitySnapshot:
    """Read capacity without confusing logical CPUs with usable CPU bandwidth."""

    detected = os.cpu_count()
    affinity = None
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            affinity = getaffinity(0)
        except (OSError, ValueError):
            pass
    cgroup = cgroup_cpu_snapshot()
    capacities: list[float] = []
    if detected is not None and detected > 0:
        capacities.append(detected)
    if affinity:
        capacities.append(len(affinity))
    cpuset_count = None
    if cgroup.cpuset_ranges is not None:
        cpuset_count = sum(last - first + 1 for first, last in cgroup.cpuset_ranges)
        capacities.append(cpuset_count)
        if affinity:
            capacities.append(sum(
                any(first <= cpu <= last for first, last in cgroup.cpuset_ranges)
                for cpu in affinity
            ))
    quota = None if cgroup.quota_cpus is None else float(cgroup.quota_cpus)
    if quota is not None:
        capacities.append(quota)
    return CpuCapacitySnapshot(
        detected,
        None if affinity is None else len(affinity),
        quota,
        cpuset_count,
        min(capacities, default=1.0),
    )


def effective_cpu_count() -> int:
    """Conservative integral concurrency; fractional quotas still need one worker.

    The precise fractional bandwidth remains visible in cpu_capacity_snapshot().
    Explicit route configuration remains policy, not a replacement for this probe.
    """

    return max(1, math.floor(cpu_capacity_snapshot().effective_cpus))


# region [01] Platform cumulative CPU counters


@dataclass(frozen=True, slots=True)
class CpuTimes:
    idle: int
    total: int


def _windows_cpu_times() -> CpuTimes | None:
    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    idle = FileTime()
    kernel = FileTime()
    user = FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None

    def value(item: FileTime) -> int:
        return (int(item.high) << 32) | int(item.low)

    return CpuTimes(value(idle), value(kernel) + value(user))


def _proc_cpu_times() -> CpuTimes | None:
    path = Path("/proc/stat")
    try:
        fields = path.read_text(encoding="ascii").splitlines()[0].split()
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return CpuTimes(idle, sum(values))


def cpu_times() -> CpuTimes | None:
    if os.name == "nt":
        return _windows_cpu_times()
    return _proc_cpu_times()


# endregion [01]


# region [02] Delta-based utilization sampler


class CpuLoadSampler:
    """Return whole-system CPU utilization from consecutive cumulative samples."""

    def __init__(self):
        self._previous = cpu_times()
        self._last_load_percent: float | None = None

    def sample(self) -> float | None:
        current = cpu_times()
        previous = self._previous
        self._previous = current
        if current is None or previous is None:
            return self._last_load_percent
        total_delta = current.total - previous.total
        idle_delta = current.idle - previous.idle
        if total_delta <= 0 or idle_delta < 0:
            return self._last_load_percent
        load = 100.0 * (1.0 - min(idle_delta, total_delta) / total_delta)
        self._last_load_percent = max(0.0, min(100.0, load))
        return self._last_load_percent
# endregion [02]
