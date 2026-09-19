"""Resident decoder/model lifetimes behind the common execution gate."""

from __future__ import annotations

import threading
import sys
import time
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TypedDict

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.cpu_runtime import effective_cpu_count
from neocortex.runtime.control.elastic_workers import current_worker_cancellation
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded, MemoryHeadroomTimeout
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits, current_resource_coordinator,
    ResourceWaitTimeout, gate_for_limits, resource_scope,
)


class DeadlineCancellation(CancellationToken):
    """An existing producer deadline, observed without a timer thread."""

    def __init__(self, parent, deadline: float):
        super().__init__(parent=parent)
        self.deadline = deadline

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    @property
    def is_cancelled(self) -> bool:
        return self.expired or super().is_cancelled


class NativeSubprocessArguments(TypedDict, total=False):
    environment: Mapping[str, str]
    on_started: Callable[[int, int | None], None]
    cancellation: CancellationToken


class _GrantCancellation(CancellationToken):
    def __init__(self, grant, parent):
        super().__init__(parent=parent)
        self._grant = grant

    def checkpoint(self):
        checker = getattr(self._grant, "check_cancellation", None)
        if checker is not None:
            checker()
        super().checkpoint()


def native_subprocess_arguments(grant, cancellation=None) -> NativeSubprocessArguments:
    token = cancellation or current_worker_cancellation()
    if grant is None:
        return {} if token is None else {"cancellation": token}

    def started(pid: int, start: int | None) -> None:
        grant.register_process(pid, start)

    return {"environment": grant.subprocess_env(), "on_started": started,
            "cancellation": _GrantCancellation(grant, token)}


def checkpoint_before_deadline(grant, deadline, cancellation, timeout_error):
    token = DeadlineCancellation(cancellation, deadline)
    try:
        grant.checkpoint(cancellation=token)
    except CancellationRequested:
        if token.expired and not (cancellation is not None and cancellation.is_cancelled):
            raise timeout_error from None
        raise


@contextmanager
def media_gate_scope(route_name, existing, limits, cancellation):
    """Give direct callers the same monitored contract as framework routes."""
    if existing is not None:
        yield existing
    elif limits is not None:
        with gate_for_limits(route_name, limits, cancellation=cancellation) as gate:
            yield gate
    else:
        shared = current_resource_coordinator()
        if shared is not None:
            shared.register_route(route_name)
            yield CoordinatedMemoryGate(shared, route_name, cancellation=cancellation)
        else:
            coordinator = GlobalResourceCoordinator(
                (route_name,), GlobalResourceLimits(), cancellation=cancellation,
            )
            with resource_scope(coordinator):
                yield CoordinatedMemoryGate(coordinator, route_name, cancellation=cancellation)


class MediaTaskGate:
    """Allocate native execution while accepting explicitly injected legacy gates."""

    def __init__(self, base):
        self.base = base
        self.target = 1

    @property
    def coordinator(self):
        return getattr(self.base, "coordinator", None)

    @property
    def cancellation(self):
        return getattr(self.base, "cancellation", None)

    def worker_capacity(self, *, max_workers=None, estimated_bytes=0, native_threads=1):
        try:
            return self._worker_capacity(
                max_workers=max_workers, estimated_bytes=estimated_bytes, native_threads=native_threads,
            )
        except MemoryBudgetExceeded:
            # Let the owner inspect the next candidate/cache row and let an
            # actual admission report the absolute-budget error if needed.
            return 1

    def _worker_capacity(self, *, max_workers=None, estimated_bytes=0, native_threads=1):
        capacity = getattr(self.base, "worker_capacity", None)
        result = (effective_cpu_count() if capacity is None else capacity(
            max_workers=max_workers, estimated_bytes=estimated_bytes, native_threads=native_threads,
        ))
        self.target = result if max_workers is None else min(result, max_workers)
        return self.target

    @contextmanager
    def admit(self, estimated_bytes, **kwargs):
        if getattr(self.base, "coordinator", None) is None:
            with self.base.admit(estimated_bytes) as grant:
                yield grant
        else:
            available = self.base.worker_capacity(estimated_bytes=0, native_threads=1)
            threads = max(1, (available + max(1, self.target) - 1) // max(1, self.target))
            kwargs.pop("native_threads", None)
            with self.base.native_budget(estimated_bytes, max_threads=threads, **kwargs) as grant:
                yield grant


@dataclass(eq=False)
class _ResidentSlot:
    busy: bool = True
    resource: Any = None
    grant: Any = None
    error: BaseException | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    waiting: CancellationToken | None = None
    task_bytes: int = 0


_CURRENT_SLOT: ContextVar[_ResidentSlot | None] = ContextVar("neocortex_media_slot", default=None)


def current_media_resource() -> Any:
    slot = _CURRENT_SLOT.get()
    return None if slot is None else slot.resource


def register_media_process(pid: int) -> tuple[int, int] | None:
    slot = _CURRENT_SLOT.get()
    if slot is not None and slot.grant is not None:
        return slot.grant.register_process(pid)
    return None


class ResidentMediaGate:
    """Retain idle resources without CPU and retire them as capacity contracts.

    A slot's manager owns its residence context from entry to close, while each
    elastic-map task acquires a separate execution lease. Its result keeps both
    claims until the caller persists it. No SQLite work runs on these threads.
    """

    def __init__(
        self, gate: Any, factory: Callable[[], Any], *, resident_bytes: int,
        cancellation: CancellationToken, variable_native_threads: bool = False,
        gpu_reservation: Callable[[], tuple[str, int] | None] | None = None,
    ) -> None:
        self.base = gate
        self.factory = factory
        self.resident_bytes = resident_bytes
        self.cancellation = cancellation
        self._stop = CancellationToken(parent=cancellation)
        self.variable_native_threads = variable_native_threads
        self.gpu_reservation = gpu_reservation
        self._slots: list[_ResidentSlot] = []
        self._lock = threading.Lock()
        self._target = 1
        self._closed = False

    @property
    def coordinator(self):
        return getattr(self.base, "coordinator", None)

    def _manage(self, slot: _ResidentSlot) -> None:
        try:
            gpu = self.gpu_reservation() if self.gpu_reservation is not None else None
            admission = (
                self.base.admit(
                    0, cpu_slots=0, native_threads=0, resident_bytes=self.resident_bytes,
                    resident_key=f"media:{id(self)}:{id(slot)}", phase="media-resident",
                    cancellation=self._stop,
                    gpu_device=None if gpu is None else gpu[0],
                    gpu_bytes=0 if gpu is None else gpu[1],
                ) if getattr(self.base, "coordinator", None) is not None
                else self.base.admit(self.resident_bytes)
            )
            with admission as grant:
                slot.grant = grant
                slot.resource = self.factory()
                slot.ready.set()
                try:
                    while not slot.stop.wait(0.1):
                        waiting = slot.waiting
                        capacity = getattr(self.base, "worker_capacity", None)
                        if waiting is not None and capacity is not None:
                            if capacity(estimated_bytes=slot.task_bytes, native_threads=1) == 0:
                                waiting.cancel()
                finally:
                    slot.resource.close()
        except BaseException as exc:
            slot.error = exc
        finally:
            slot.ready.set()
            slot.finished.set()

    def _retire(self, target: int) -> None:
        with self._lock:
            self._target = target
            idle = [slot for slot in reversed(self._slots) if not slot.busy]
            retired = idle[:max(0, len(self._slots) - target)]
            for slot in retired:
                self._slots.remove(slot)
                slot.stop.set()
        for slot in retired:
            slot.finished.wait()
            if slot.error is not None:
                raise slot.error

    def worker_capacity(self, *, max_workers=None, estimated_bytes=0, native_threads=1):
        try:
            return self._resident_capacity(
                max_workers=max_workers, estimated_bytes=estimated_bytes, native_threads=native_threads,
            )
        except MemoryBudgetExceeded:
            return 1

    def _resident_capacity(self, *, max_workers=None, estimated_bytes=0, native_threads=1):
        capacity = getattr(self.base, "worker_capacity", None)
        if capacity is not None:
            upper = capacity(max_workers=max_workers, estimated_bytes=estimated_bytes,
                             native_threads=native_threads)
            with self._lock:
                resident_count = len(self._slots)
            low, high = 0, upper
            while low < high:
                count = (low + high + 1) // 2
                extra = max(0, count - resident_count) * self.resident_bytes
                per_worker = estimated_bytes + (extra + count - 1) // count
                if capacity(max_workers=count, estimated_bytes=per_worker,
                            native_threads=native_threads) >= count:
                    low = count
                else:
                    high = count - 1
            target = low
        else:
            target = effective_cpu_count()
            limits = getattr(self.base, "limits", None)
            budget = getattr(limits, "memory_budget_bytes", None)
            if budget is not None:
                target = min(target, max(1, budget // max(1, estimated_bytes + self.resident_bytes)))
        if max_workers is not None:
            target = min(target, max_workers)
        gpu = self.gpu_reservation() if self.gpu_reservation is not None else None
        if gpu is not None:
            with self._lock:
                reusable = sum(gpu[1] for slot in self._slots
                               if slot.grant is not None and not slot.finished.is_set())
            target = min(target, self.base.gpu_worker_capacity(
                gpu[0], gpu[1], reusable_resident_bytes=reusable,
            ))
        self._retire(max(0, int(target)))
        return target

    @contextmanager
    def _claim(self, deadline: float | None):
        while True:
            self._stop.checkpoint()
            with self._lock:
                if self._closed:
                    raise RuntimeError("media residence pool is closed")
                slot = next((item for item in self._slots if not item.busy), None)
                if slot is None and len(self._slots) < self._target:
                    slot = _ResidentSlot()
                    self._slots.append(slot)
                    threading.Thread(target=self._manage, args=(slot,), name="neocortex-media-resident").start()
                if slot is not None:
                    slot.busy = True
                    break
            if deadline is not None and time.monotonic() >= deadline:
                raise self._headroom_timeout("media residence did not recover resource headroom")
            self._stop.wait(0.05)
        try:
            while not slot.ready.wait(0.05):
                self._stop.checkpoint()
            self._stop.checkpoint()
            if slot.error is not None:
                raise slot.error
            token = _CURRENT_SLOT.set(slot)
            try:
                yield slot
            finally:
                _CURRENT_SLOT.reset(token)
        finally:
            with self._lock:
                slot.busy = False

    def _headroom_timeout(self, message: str) -> MemoryHeadroomTimeout:
        if getattr(self.base, "coordinator", None) is not None:
            return ResourceWaitTimeout(message)
        return MemoryHeadroomTimeout(message)

    @contextmanager
    def admit(self, estimated_bytes: int, **kwargs):
        parent = kwargs.pop("cancellation", None) or self._stop
        limits = getattr(getattr(self.base, "coordinator", None), "limits", None)
        timeout = getattr(limits, "wait_timeout_seconds", None)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            parent.checkpoint()
            resources = ExitStack()
            waiting = CancellationToken(parent=parent)
            try:
                slot = resources.enter_context(self._claim(deadline))
                slot.task_bytes = estimated_bytes
                slot.waiting = waiting
                context = self._execution(estimated_bytes, cancellation=waiting, **kwargs)
                grant = resources.enter_context(context)
                slot.waiting = None
                break
            except CancellationRequested:
                resources.close()
                parent.checkpoint()
                self._retire(0)
                if deadline is not None and time.monotonic() >= deadline:
                    raise self._headroom_timeout("media execution did not recover resource headroom") from None
                parent.wait(0.05)
            except BaseException:
                resources.close()
                raise
        try:
            yield grant
        finally:
            resources.close()

    def _execution(self, estimated_bytes, **kwargs):
            native_budget = getattr(self.base, "native_budget", None)
            if self.variable_native_threads and native_budget is not None:
                available = self.base.worker_capacity(estimated_bytes=0, native_threads=1)
                threads = max(1, (available + max(1, self._target) - 1) // max(1, self._target))
                kwargs.pop("native_threads", None)
                return native_budget(estimated_bytes, max_threads=threads, **kwargs)
            elif getattr(self.base, "coordinator", None) is not None:
                return self.base.admit(estimated_bytes, **kwargs)
            else:
                return self.base.admit(estimated_bytes) if estimated_bytes else nullcontext()

    def close(self) -> None:
        primary = sys.exception()
        self._stop.cancel()
        with self._lock:
            self._closed = True
            slots, self._slots = self._slots, []
            for slot in slots:
                slot.stop.set()
        errors = []
        for slot in slots:
            slot.finished.wait()
            if slot.error is not None and not isinstance(slot.error, CancellationRequested):
                errors.append(slot.error)
        if errors:
            if primary is None:
                raise errors[0]
            primary.add_note(f"media resource cleanup also failed: {errors[0]}")
