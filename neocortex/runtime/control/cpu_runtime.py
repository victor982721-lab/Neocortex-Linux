"""Low-overhead system CPU load sampling without a runtime dependency."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module


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


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.cpu_runtime")


# endregion [02]
