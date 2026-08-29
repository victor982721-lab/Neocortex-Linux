"""Bounded resource admission for concurrent PDF processing."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import ctypes
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.memory_runtime import posix_physical_memory_snapshot


# region [01] Limits, snapshots and errors
# Keep resource policy explicit and independently testable from extraction logic.

MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    total_physical: int | None
    available_physical: int | None
    total_commit: int | None
    available_commit: int | None


@dataclass(frozen=True, slots=True)
class PdfResourceLimits:
    min_free_bytes: int = 512 * MIB
    memory_backpressure_bytes: int | None = None
    memory_wait_timeout_seconds: float = 60.0
    large_document_bytes: int = 128 * MIB
    large_document_workers: int = 2
    memory_budget_bytes: int | None = None
    worker_memory_bytes: int = 512 * MIB
    commit_backpressure_bytes: int | None = None


class PdfResourceError(RuntimeError):
    """Raised before dispatch when the configured resource floor is unavailable."""


# endregion [01]


# region [02] Live resource probes
# Query the operating system directly without retaining monitoring processes.


def memory_snapshot() -> MemorySnapshot:
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
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return MemorySnapshot(
                int(status.total_physical),
                int(status.available_physical),
                int(status.total_page_file),
                int(status.available_page_file),
            )
        return MemorySnapshot(None, None, None, None)

    total_physical, available_physical = posix_physical_memory_snapshot()
    return MemorySnapshot(
        total_physical,
        available_physical,
        None,
        None,
    )


def _automatic_limit(total_physical: int | None, minimum: int, maximum: int) -> int:
    if total_physical is None or total_physical <= 0:
        return minimum
    return max(minimum, min(maximum, total_physical // 4))


def ensure_free_space(path: Path, minimum_bytes: int) -> None:
    if minimum_bytes <= 0:
        return
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    available = shutil.disk_usage(probe).free
    if available < minimum_bytes:
        raise PdfResourceError(
            f"insufficient free space at {probe}: {available} < {minimum_bytes} bytes"
        )


def wait_for_available_memory(
    minimum_bytes: int,
    timeout_seconds: float,
    minimum_commit_bytes: int = 0,
    cancellation: CancellationToken | None = None,
) -> None:
    if minimum_bytes <= 0 and minimum_commit_bytes <= 0:
        return
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancellation is not None:
            cancellation.checkpoint()
        snapshot = memory_snapshot()
        available = snapshot.available_physical
        commit = snapshot.available_commit if minimum_commit_bytes > 0 else None
        physical_ok = available is None or available >= minimum_bytes
        commit_ok = commit is None or commit >= minimum_commit_bytes
        if physical_ok and commit_ok:
            return
        if time.monotonic() >= deadline:
            raise PdfResourceError(
                "memory headroom remained below its configured floor; "
                f"physical={available} floor={minimum_bytes} "
                f"commit={commit} commit_floor={minimum_commit_bytes}"
            )
        if cancellation is None:
            time.sleep(0.25)
        elif cancellation.wait(0.25):
            cancellation.checkpoint()


# endregion [02]


# region [03] Weighted in-process reservation budget
# Bound aggregate active-worker estimates instead of only checking memory once.


class _ReservationBudget:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("memory budget must be positive")
        self.capacity = capacity
        self._reserved = 0
        self._condition = threading.Condition()
        self.wait_count = 0
        self._active_reservations = 0

    @property
    def observed_wait_count(self) -> int:
        with self._condition:
            return self.wait_count

    @property
    def active_reservation_count(self) -> int:
        with self._condition:
            return self._active_reservations

    @contextmanager
    def reserve(self, requested: int, cancellation: CancellationToken | None = None):
        amount = min(self.capacity, max(1, requested))
        with self._condition:
            waited = False
            while self._reserved + amount > self.capacity:
                if not waited:
                    self.wait_count += 1
                    waited = True
                if cancellation is not None:
                    cancellation.checkpoint()
                self._condition.wait(0.25)
            if cancellation is not None:
                cancellation.checkpoint()
            self._reserved += amount
            self._active_reservations += 1
        try:
            yield
        finally:
            with self._condition:
                self._reserved -= amount
                self._active_reservations -= 1
                self._condition.notify_all()


# endregion [03]


# region [04] Admission gate
# Combine reservations, physical/commit headroom and large-document serialization.


class PdfResourceGate:
    def __init__(
        self,
        limits: PdfResourceLimits,
        state_path: Path,
        *,
        global_coordinator=None,
        route_name: str = "pdf",
        cancellation: CancellationToken | None = None,
    ):
        if limits.large_document_workers < 1:
            raise ValueError("large_document_workers must be positive")
        if limits.memory_wait_timeout_seconds < 0:
            raise ValueError("memory_wait_timeout_seconds cannot be negative")
        if limits.worker_memory_bytes < 1:
            raise ValueError("worker_memory_bytes must be positive")
        for name in ("memory_backpressure_bytes", "commit_backpressure_bytes"):
            value = getattr(limits, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if limits.memory_budget_bytes is not None and limits.memory_budget_bytes < 1:
            raise ValueError("memory_budget_bytes must be positive when configured")
        if (
            limits.memory_budget_bytes is not None
            and limits.memory_budget_bytes < limits.worker_memory_bytes
        ):
            raise ValueError("memory_budget_bytes cannot be smaller than worker_memory_bytes")

        snapshot = memory_snapshot()
        automatic = _automatic_limit(snapshot.total_physical, 512 * MIB, 2 * GIB)
        self.limits = limits
        self.state_path = state_path
        self.global_coordinator = global_coordinator
        self.route_name = route_name
        self.cancellation = cancellation or CancellationToken()
        self.memory_floor_bytes = (
            automatic
            if limits.memory_backpressure_bytes is None
            else limits.memory_backpressure_bytes
        )
        self.commit_floor_bytes = (
            automatic
            if limits.commit_backpressure_bytes is None
            else limits.commit_backpressure_bytes
        )
        self.memory_budget_bytes = (
            max(limits.worker_memory_bytes, automatic)
            if limits.memory_budget_bytes is None
            else limits.memory_budget_bytes
        )
        self._budget = _ReservationBudget(self.memory_budget_bytes)
        self._large_slots = threading.BoundedSemaphore(limits.large_document_workers)

    @property
    def wait_count(self) -> int:
        if self.global_coordinator is not None:
            return self.global_coordinator.route_wait_count(self.route_name)
        return self._budget.observed_wait_count

    @property
    def active_count(self) -> int:
        """Return admitted PDF jobs, not futures waiting for admission."""

        if self.global_coordinator is not None:
            return self.global_coordinator.route_active_request_count(self.route_name)
        return self._budget.active_reservation_count

    @contextmanager
    def admit(self, size: int, *, reservation_bytes: int | None = None):
        large = size >= self.limits.large_document_bytes
        if large:
            while not self._large_slots.acquire(timeout=0.25):
                self.cancellation.checkpoint()
        try:
            self.cancellation.checkpoint()
            requested = reservation_bytes or self.limits.worker_memory_bytes
            if self.global_coordinator is not None:
                try:
                    with self.global_coordinator.admit(self.route_name, requested, 1):
                        ensure_free_space(self.state_path, self.limits.min_free_bytes)
                        yield
                except MemoryError as exc:
                    raise PdfResourceError(str(exc)) from exc
            else:
                with self._budget.reserve(requested, self.cancellation):
                    ensure_free_space(self.state_path, self.limits.min_free_bytes)
                    wait_for_available_memory(
                        self.memory_floor_bytes,
                        self.limits.memory_wait_timeout_seconds,
                        self.commit_floor_bytes,
                        self.cancellation,
                    )
                    yield
        finally:
            if large:
                self._large_slots.release()


# endregion [04]

_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.pdf_runtime")
