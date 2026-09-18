"""Shared weighted memory admission for bounded content routes."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from .cancellation import CancellationToken
from .cgroup_runtime import cgroup_memory_snapshot


# region [01] Live physical and commit capacity


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    available_physical: int | None
    available_commit: int | None
    total_physical: int | None = None
    total_commit: int | None = None


_PROC_MEMINFO = Path("/proc/meminfo")


def _linux_meminfo_physical_bytes(text: str) -> tuple[int, int] | None:
    """Return total and reclaimable physical bytes from Linux meminfo text."""

    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, raw_value = line.partition(":")
        if not separator or name not in {"MemTotal", "MemAvailable"}:
            continue
        parts = raw_value.split()
        if len(parts) != 2 or parts[1] != "kB":
            return None
        try:
            value_kib = int(parts[0])
        except ValueError:
            return None
        if value_kib < 0:
            return None
        values[name] = value_kib * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0 or available > total:
        return None
    return total, available


def posix_physical_memory_snapshot(
    meminfo_path: Path = _PROC_MEMINFO,
    *,
    sysconf: Callable[[str], int] | None = None,
) -> tuple[int | None, int | None]:
    """Return usable physical capacity, bounded by host pressure and cgroup use."""

    total, available = _host_physical_memory_snapshot(meminfo_path, sysconf=sysconf)
    cgroup = cgroup_memory_snapshot()
    if cgroup.limit_bytes is not None:
        total = cgroup.limit_bytes if total is None else min(total, cgroup.limit_bytes)
    if cgroup.available_bytes is not None:
        available = (
            cgroup.available_bytes if available is None else min(available, cgroup.available_bytes)
        )
    if total is not None and available is not None:
        available = min(total, available)
    return total, available


def _host_physical_memory_snapshot(
    meminfo_path: Path,
    *,
    sysconf: Callable[[str], int] | None,
) -> tuple[int | None, int | None]:
    """Prefer reclaimable MemAvailable over the free-page sysconf fallback."""

    try:
        parsed = _linux_meminfo_physical_bytes(meminfo_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError):
        parsed = None
    if parsed is not None:
        return parsed

    probe = getattr(os, "sysconf", None) if sysconf is None else sysconf
    if probe is None:
        return None, None
    try:
        page_size = int(probe("SC_PAGE_SIZE"))
        total_pages = int(probe("SC_PHYS_PAGES"))
        available_pages = int(probe("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None, None
    if page_size <= 0 or total_pages <= 0 or available_pages < 0:
        return None, None
    return page_size * total_pages, page_size * available_pages


def memory_snapshot() -> MemorySnapshot:
    """Read live physical and commit headroom without retaining system state."""

    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = (
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            )

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        windll = getattr(ctypes, "windll", None)
        if windll is not None and windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return MemorySnapshot(
                int(status.available_physical),
                int(status.available_page_file),
                int(status.total_physical),
                int(status.total_page_file),
            )
        return MemorySnapshot(None, None)

    total_physical, available_physical = posix_physical_memory_snapshot()
    return MemorySnapshot(
        available_physical,
        None,
        total_physical,
        None,
    )


# endregion [01]


# region [02] Weighted admission and explicit failure modes


@dataclass(frozen=True, slots=True)
class MemoryResourceLimits:
    memory_budget_bytes: int = 512 * 1024 * 1024
    min_free_memory_bytes: int = 1024 * 1024 * 1024
    min_free_commit_bytes: int = 1024 * 1024 * 1024
    wait_timeout_seconds: float = 60.0


class MemoryBudgetExceeded(MemoryError):
    """A single item cannot fit inside the configured route budget."""


class MemoryHeadroomTimeout(MemoryError):
    """The live system did not recover the required safe headroom in time."""


class WeightedMemoryGate:
    """Bound aggregate estimates and reserve future physical/commit use."""

    def __init__(
        self,
        limits: MemoryResourceLimits,
        cancellation: CancellationToken | None = None,
    ):
        if limits.memory_budget_bytes < 1:
            raise ValueError("memory_budget_bytes must be positive")
        if limits.min_free_memory_bytes < 0 or limits.min_free_commit_bytes < 0:
            raise ValueError("memory headroom floors cannot be negative")
        if limits.wait_timeout_seconds < 0:
            raise ValueError("wait_timeout_seconds cannot be negative")
        self.limits = limits
        self.cancellation = cancellation or CancellationToken()
        self._condition = threading.Condition()
        self._headroom_admission_lock = threading.Lock()
        self._reserved = 0
        self.peak_reserved_bytes = 0
        self.wait_count = 0

    def _wait_for_headroom(self, deadline: float) -> None:
        while True:
            self.cancellation.checkpoint()
            with self._condition:
                # Reservations may be released while this admission waits for
                # live headroom. Re-read the aggregate instead of retaining the
                # larger value observed when the request first entered.
                aggregate_reservation = self._reserved
            snapshot = memory_snapshot()
            self.cancellation.checkpoint()
            physical_ok = (
                snapshot.available_physical is None
                or snapshot.available_physical
                >= self.limits.min_free_memory_bytes + aggregate_reservation
            )
            commit_ok = (
                snapshot.available_commit is None
                or snapshot.available_commit
                >= self.limits.min_free_commit_bytes + aggregate_reservation
            )
            if physical_ok and commit_ok:
                return
            if time.monotonic() >= deadline:
                raise MemoryHeadroomTimeout(
                    "memoria disponible por debajo del margen seguro: "
                    f"fisica={snapshot.available_physical}, "
                    f"commit={snapshot.available_commit}, "
                    f"reserva_agregada={aggregate_reservation}"
                )
            if self.cancellation.wait(0.25):
                self.cancellation.checkpoint()

    def _acquire_headroom_admission(self, deadline: float) -> None:
        """Serialize reserve+headroom without locking the active work context."""

        while True:
            self.cancellation.checkpoint()
            if self._headroom_admission_lock.acquire(blocking=False):
                try:
                    self.cancellation.checkpoint()
                except BaseException:
                    self._headroom_admission_lock.release()
                    raise
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryHeadroomTimeout("timeout esperando turno de admision de memoria")
            if self.cancellation.wait(min(remaining, 0.25)):
                self.cancellation.checkpoint()

    @contextmanager
    def admit(self, estimated_bytes: int):
        self.cancellation.checkpoint()
        reservation = max(1, int(estimated_bytes))
        if reservation > self.limits.memory_budget_bytes:
            raise MemoryBudgetExceeded(
                f"la estimacion de memoria ({reservation} bytes) excede el "
                f"presupuesto por ruta ({self.limits.memory_budget_bytes} bytes)"
            )

        deadline = time.monotonic() + self.limits.wait_timeout_seconds
        waited = False
        reserved = False
        try:
            self._acquire_headroom_admission(deadline)
            try:
                with self._condition:
                    while self._reserved + reservation > self.limits.memory_budget_bytes:
                        self.cancellation.checkpoint()
                        if not waited:
                            self.wait_count += 1
                            waited = True
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise MemoryHeadroomTimeout(
                                "timeout esperando presupuesto agregado de memoria"
                            )
                        self._condition.wait(min(remaining, 0.25))
                    self.cancellation.checkpoint()
                    self._reserved += reservation
                    reserved = True
                    self.peak_reserved_bytes = max(self.peak_reserved_bytes, self._reserved)
                self._wait_for_headroom(deadline)
            finally:
                self._headroom_admission_lock.release()
            self.cancellation.checkpoint()
            yield
        finally:
            if reserved:
                with self._condition:
                    self._reserved -= reservation
                    self._condition.notify_all()


# endregion [02]
