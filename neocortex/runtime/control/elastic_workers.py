"""Bounded, ordered work with live capacity and one caller-owned consumer.

Workers receive independent payloads.  Iteration, optional preparation, and
consumption stay in the calling thread, so a route can keep its SQLite writer
there.  The input is never submitted eagerly.  A completed value retains its
resource lease until the caller requests the next value or closes the map.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from .cancellation import CancellationToken
from .cpu_runtime import effective_cpu_count
from .memory_runtime import MemoryBudgetExceeded

_Input = TypeVar("_Input")
_Output = TypeVar("_Output")
_MISSING = object()
_WORKER_CANCELLATION: ContextVar[CancellationToken | None] = ContextVar(
    "neocortex_elastic_worker_cancellation", default=None
)
_PROCESS_CANCELLATION_EVENT: Any = None


class _ProcessCancellationToken(CancellationToken):
    """Observe the owning map without sending local cancellation to siblings."""

    def __init__(self, event: Any) -> None:
        super().__init__()
        self._process_event = event

    @property
    def is_cancelled(self) -> bool:
        return super().is_cancelled or bool(self._process_event.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_cancelled:
            remaining = 0.05 if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return self.is_cancelled
            self._event.wait(min(0.05, remaining))
        return True


def current_worker_cancellation() -> CancellationToken | None:
    """Return the current map's cooperative token inside a worker invocation.

    Process workers observe the owner's shared signal through this local token.
    Cancelling the returned token affects only that invocation, never a sibling.
    Parsers should check it at their existing record/page/chunk boundaries.
    """

    return _WORKER_CANCELLATION.get()


def _initialize_process(event: Any, native_threads: int) -> None:
    global _PROCESS_CANCELLATION_EVENT
    _PROCESS_CANCELLATION_EVENT = event
    from .worker_priority import configure_worker_priority

    configure_worker_priority()
    # Set limits before task unpickling can import a numerical/parser backend.
    # This interpreter is owned by the map; the parent environment is untouched.
    threads = str(max(1, native_threads))
    os.environ.update(dict.fromkeys((
        "OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ), threads))


def _process_call(function: Callable[..., Any], payload: Any,
                  environment: dict[str, str]) -> Any:
    """Apply the granted native limits in this owned child, never the parent."""

    previous = {name: os.environ.get(name) for name in environment}
    os.environ.update(environment)
    cancellation = _ProcessCancellationToken(_PROCESS_CANCELLATION_EVENT)
    token = _WORKER_CANCELLATION.set(cancellation)
    try:
        cancellation.checkpoint()
        result = function(payload)
        cancellation.checkpoint()
        return result
    finally:
        _WORKER_CANCELLATION.reset(token)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@dataclass(frozen=True, slots=True)
class ImmediateResult(Generic[_Output]):
    """A prepared cache hit that needs no worker invocation."""

    value: _Output


@dataclass(slots=True)
class _Cohort:
    executor: Executor
    capacity: int
    futures: list[Future[Any]] = field(default_factory=list)

    def active(self) -> int:
        self.futures[:] = [future for future in self.futures if not future.done()]
        return len(self.futures)


class _ElasticPool:
    """Grow using public executor APIs; retire idle cohorts on contraction.

    Individual executor ceilings never change.  Additional cohorts provide
    capacity that appeared after startup, without private executor fields or
    a fixed 4/8/32-worker default.  Submitted work is bounded by ElasticMap.
    """

    def __init__(self) -> None:
        self._cohorts: list[_Cohort] = []
        self._target = 0
        self._closed = False
        self._lock = threading.Lock()

    def resize(self, target: int) -> None:
        retired: list[_Cohort] = []
        with self._lock:
            self._target = max(0, target)
            total = sum(cohort.capacity for cohort in self._cohorts)
            for cohort in reversed(self._cohorts):
                if total <= self._target:
                    break
                if cohort.active() == 0:
                    retired.append(cohort)
                    total -= cohort.capacity
            for cohort in retired:
                self._cohorts.remove(cohort)
        for cohort in retired:
            cohort.executor.shutdown(wait=True, cancel_futures=True)

    def submit(self, function: Callable[..., Any], *args: Any) -> Future[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("elastic worker pool is closed")
            for cohort in self._cohorts:
                if cohort.active() < cohort.capacity:
                    future = cohort.executor.submit(function, *args)
                    cohort.futures.append(future)
                    return future
            capacity = max(1, self._target - sum(c.capacity for c in self._cohorts))
            executor = ThreadPoolExecutor(
                max_workers=capacity, thread_name_prefix="neocortex-elastic"
            )
            cohort = _Cohort(executor, capacity)
            self._cohorts.append(cohort)
            future = executor.submit(function, *args)
            cohort.futures.append(future)
            return future

    def close(self) -> None:
        with self._lock:
            self._closed = True
            cohorts, self._cohorts = self._cohorts, []
        for cohort in cohorts:
            cohort.executor.shutdown(wait=True, cancel_futures=True)

@dataclass(slots=True)
class _ProcessCohort:
    capacity: int
    cancellation: CancellationToken
    claims: int = 0
    waiting_claims: int = 0
    executor: ProcessPoolExecutor | None = None
    error: BaseException | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


class _ProcessClaim:
    def __init__(self, pool: _ResidentProcessPool, cohort: _ProcessCohort) -> None:
        self.pool = pool
        self.cohort = cohort
        self.waiting = True
        self.active = False

    @property
    def executor(self) -> ProcessPoolExecutor | None:
        return self.cohort.executor

    def activate(self) -> bool:
        with self.pool._lock:
            if self.cohort not in self.pool._cohorts or self.cohort.stop.is_set():
                return False
            self.cohort.waiting_claims -= 1
            self.waiting = False
            self.cohort.claims += 1
            self.active = True
            return True

    def release(self) -> None:
        with self.pool._lock:
            if self.waiting:
                self.cohort.waiting_claims -= 1
                self.waiting = False
            if self.active:
                self.cohort.claims -= 1
                self.active = False


class _ResidentProcessPool:
    """Keep interpreter memory charged until each owned cohort has stopped."""

    def __init__(self, gate: Any, resident_bytes: int, cancellation: CancellationToken,
                 poll_interval: float, *, eager_growth: bool = True,
                 native_threads: int = 1) -> None:
        self._gate = gate
        self._resident_bytes = resident_bytes
        self._stop = cancellation
        self._poll = poll_interval
        self._eager_growth = eager_growth
        self._native_threads = native_threads
        self._target = 0
        self._closed = False
        self._cohorts: list[_ProcessCohort] = []
        self._lock = threading.Lock()

    def _manage(self, cohort: _ProcessCohort) -> None:
        try:
            admission = (
                self._gate.admit(
                    0, cpu_slots=0, native_threads=0,
                    resident_bytes=cohort.capacity * self._resident_bytes,
                    resident_key=f"elastic-process:{id(self)}:{id(cohort)}",
                    phase="elastic-process-resident", cancellation=cohort.cancellation,
                ) if self._gate is not None and self._resident_bytes else nullcontext()
            )
            # Admission happens before any task lease, on this same thread
            # throughout the cohort's lifetime.  No caller waits under a pool
            # lock or tries to grow residence while holding all task memory.
            with admission:
                context = multiprocessing.get_context("spawn")
                cancelled = context.Event()
                executor = ProcessPoolExecutor(
                    max_workers=cohort.capacity,
                    mp_context=context,
                    initializer=_initialize_process,
                    initargs=(cancelled, self._native_threads),
                )
                cohort.executor = executor
                cohort.ready.set()
                try:
                    while not cohort.stop.wait(self._poll):
                        if self._stop.is_cancelled:
                            break
                finally:
                    if self._stop.is_cancelled:
                        cancelled.set()
                    executor.shutdown(wait=True, cancel_futures=True)
        except BaseException as exc:
            cohort.error = exc
        finally:
            cohort.ready.set()
            cohort.finished.set()

    @contextmanager
    def claim(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("elastic process pool is closed")
            cohort = next((item for item in self._cohorts
                           if item.claims + item.waiting_claims < item.capacity), None)
            if cohort is None:
                size = max(1, self._target - sum(item.capacity for item in self._cohorts))
                if not self._eager_growth:
                    # A cache hit may cost zero, while the next file needs
                    # substantial RAM. Reserve only the interpreter actually
                    # being claimed until another item's demand is known.
                    size = 1
                cohort = _ProcessCohort(size, CancellationToken(parent=self._stop))
                self._cohorts.append(cohort)
                threading.Thread(
                    target=self._manage, args=(cohort,), name="neocortex-process-residence"
                ).start()
            cohort.waiting_claims += 1
            claim = _ProcessClaim(self, cohort)
        try:
            while not cohort.ready.wait(self._poll):
                self._stop.checkpoint()
            self._stop.checkpoint()
            if cohort.stop.is_set():
                yield claim
                return
            if cohort.error is not None:
                raise cohort.error
            if cohort.executor is None:
                raise RuntimeError("elastic process residence has no executor")
            yield claim
        finally:
            claim.release()

    def resident_bytes(self) -> int:
        with self._lock:
            return self._resident_bytes * sum(
                cohort.capacity for cohort in self._cohorts
                if cohort.executor is not None and not cohort.finished.is_set()
            )

    def resize(self, target: int) -> None:
        retired = []
        with self._lock:
            self._target = max(0, target)
            total = sum(cohort.capacity for cohort in self._cohorts)
            for cohort in reversed(self._cohorts):
                if total <= self._target:
                    break
                if cohort.claims == 0:
                    retired.append(cohort)
                    total -= cohort.capacity
            for cohort in retired:
                self._cohorts.remove(cohort)
                cohort.stop.set()
                cohort.cancellation.cancel()
        for cohort in retired:
            cohort.finished.wait()
            if (cohort.error is not None and not cohort.cancellation.is_cancelled):
                raise cohort.error

    def close(self) -> None:
        with self._lock:
            self._closed = True
            cohorts, self._cohorts = self._cohorts, []
            for cohort in cohorts:
                cohort.stop.set()
                cohort.cancellation.cancel()
        for cohort in cohorts:
            cohort.finished.wait()


def _grant_scope(grant: Any) -> AbstractContextManager[Any]:
    if grant is None:
        return nullcontext()
    from .global_resources import resource_grant_scope

    return resource_grant_scope(grant)


@contextmanager
def _consumer_grant_scope(grant: Any):
    drain = getattr(grant, "drain_scope", None)
    with _grant_scope(grant), (drain() if callable(drain) else nullcontext()):
        if callable(drain):
            # Serialization/SQLite publication is work too. Every owner gets
            # the same bounded finalization admission, without requiring each
            # route to remember an extra checkpoint after obtaining a result.
            grant.checkpoint(drain=True)
        yield grant


class _Work(Generic[_Output]):
    def __init__(self, item: Any, estimated_bytes: int, device: str | None) -> None:
        self.item = item
        self.estimated_bytes = estimated_bytes
        self.device = device
        self.grant: Any = None
        self.admitted = threading.Event()
        self.prepared = threading.Event()
        self.ready = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.value: Any = _MISSING
        self.payload: Any = _MISSING
        self.error: BaseException | None = None
        self.prepare_error: BaseException | None = None


class ElasticMap(AbstractContextManager["ElasticMap[_Input, _Output]"],
                 Iterator[_Output], Generic[_Input, _Output]):
    """Use with ``with`` so early breaks drain only this map's work.

    ``prepare(item)`` runs in the caller after admission and may return an
    ``ImmediateResult`` to bypass computation.  Otherwise its return value is
    passed to the worker.  Process workers must be pure, picklable functions;
    each item is a cancellation/cooperation boundary.  Active synchronous work
    is drained on close, not interrupted by killing unrelated processes.

    Task estimates include payload/result copies.  ``process_resident_bytes``
    separately reserves interpreter memory until each process cohort stops.
    A caller retaining values after its next ``next()`` owns that additional
    lifetime.  Idle process cohorts are retired on contraction.
    """

    def __init__(
        self,
        function: Callable[..., _Output],
        iterable: Iterable[_Input],
        *,
        gate: Any = None,
        capacity: Callable[[], int] | None = None,
        max_workers: int | None = None,
        estimated_bytes: int | Callable[[_Input], int] = 0,
        native_threads: int = 1,
        io_slots: int = 0,
        io_device: str | Callable[[_Input], str | None] | None = None,
        phase: str | None = None,
        cancellation: CancellationToken | None = None,
        executor_kind: Literal["thread", "process"] = "thread",
        process_resident_bytes: int = 64 * 1024 * 1024,
        process_predicate: Callable[[Any], bool] | None = None,
        prepare: Callable[[_Input], Any] | None = None,
        on_admission_error: Callable[[_Input, MemoryBudgetExceeded], ImmediateResult[_Output]] | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be positive or None")
        if native_threads < 0 or io_slots < 0:
            raise ValueError("native_threads and io_slots cannot be negative")
        if not callable(estimated_bytes) and estimated_bytes < 0:
            raise ValueError("estimated_bytes cannot be negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if executor_kind not in {"thread", "process"}:
            raise ValueError("executor_kind must be 'thread' or 'process'")
        if process_resident_bytes < 0:
            raise ValueError("process_resident_bytes cannot be negative")
        self._function = function
        self._source = iter(iterable)
        self._gate = gate
        self._capacity = capacity
        self._max_workers = max_workers
        self._estimate = estimated_bytes
        self._native_threads = native_threads
        self._io_slots = io_slots
        self._io_device = io_device
        self._phase = phase
        self._prepare = prepare
        self._on_admission_error = on_admission_error
        self._process_predicate = process_predicate
        self._poll = poll_interval
        self._stop = CancellationToken(
            parent=cancellation if cancellation is not None else getattr(gate, "cancellation", None)
        )
        self._supervisors = _ElasticPool()
        self._process_resident_bytes = process_resident_bytes if executor_kind == "process" else 0
        self._processes = (
            _ResidentProcessPool(
                gate, process_resident_bytes, self._stop, poll_interval,
                eager_growth=not callable(estimated_bytes),
                native_threads=native_threads,
            )
            if executor_kind == "process" else None
        )
        self._pending: deque[_Work[_Output]] = deque()
        self._current: _Work[_Output] | None = None
        self._consumer_scope: AbstractContextManager[Any] | None = None
        self._exhausted = False
        self._closed = False
        self._owner = threading.get_ident()
        # Workers publish their durable per-work state before setting this
        # event. One notification covers admission, completion and failure;
        # preparation and consumption still belong to the caller alone.
        self._owner_wakeup = threading.Event()

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("elastic iteration and consumption belong to their caller")

    def _checkpoint(self) -> None:
        self._stop.checkpoint()
        coordinator = getattr(self._gate, "coordinator", None)
        checkpoint = getattr(coordinator, "checkpoint", None)
        if callable(checkpoint):
            # The owner must observe its deadline while workers are busy,
            # too. A failure closes this map and signals its cooperative
            # children, preserving sibling scopes and the original error.
            checkpoint()

    def __enter__(self) -> ElasticMap[_Input, _Output]:
        self._check_owner()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close_preserving_error(exc)

    def _close_preserving_error(self, primary: BaseException | None) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(
                f"elastic map cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
            )

    def __iter__(self) -> ElasticMap[_Input, _Output]:
        return self

    def _target(self) -> int:
        estimate = (
            self._estimate if not callable(self._estimate)
            else max((work.estimated_bytes for work in self._pending
                      if work.error is None and not work.finished.is_set()), default=0)
        )
        estimate += self._process_resident_bytes
        reusable_residence = 0 if self._processes is None else self._processes.resident_bytes()
        capacity_kwargs = {
            "max_workers": self._max_workers, "estimated_bytes": estimate,
            "native_threads": self._native_threads,
        }
        if reusable_residence:
            capacity_kwargs["reusable_resident_bytes"] = reusable_residence
        if self._capacity is not None:
            value = int(self._capacity())
        elif self._gate is not None and callable(getattr(self._gate, "worker_capacity", None)):
            value = int(self._gate.worker_capacity(**capacity_kwargs))
        elif self._gate is not None and getattr(self._gate, "coordinator", None) is not None:
            value = int(self._gate.coordinator.worker_capacity(
                self._gate.route_name,
                **capacity_kwargs,
            ))
        else:
            value = effective_cpu_count() // max(1, self._native_threads)
        if self._max_workers is not None:
            value = min(value, self._max_workers)
        return max(0, value)

    def _admission_target(self) -> int:
        try:
            return self._target()
        except MemoryBudgetExceeded:
            if self._on_admission_error is None:
                raise
            # Only one candidate proceeds to admission to obtain its typed
            # absolute-budget error. It never invokes an oversized worker.
            return 1

    def _fill(self) -> None:
        target = self._admission_target()
        self._supervisors.resize(target)
        if self._processes is not None:
            self._processes.resize(target)
        # One admission waiter lets the governor own pressure timeouts even
        # when capacity is zero.  It cannot start work until granted resources.
        limit = max(1, target) if self._gate is not None else target
        while not self._exhausted and len(self._pending) < limit:
            self._checkpoint()
            try:
                item = next(self._source)
            except StopIteration:
                self._exhausted = True
                break
            estimate = int(self._estimate(item) if callable(self._estimate) else self._estimate)
            if estimate < 0:
                raise ValueError("estimated_bytes cannot be negative")
            device = self._io_device(item) if callable(self._io_device) else self._io_device
            work: _Work[_Output] = _Work(item, estimate, device)
            self._pending.append(work)
            # Varying file estimates can lower the next admission target.
            # Apply that target before creating a resident process cohort.
            if callable(self._estimate):
                target = self._admission_target()
                limit = max(1, target) if self._gate is not None else target
                self._supervisors.resize(target)
                if self._processes is not None:
                    self._processes.resize(target)
            context = copy_context()
            self._supervisors.submit(context.run, self._run, work)

    def _prepare_ready(self) -> None:
        for work in self._pending:
            if (work.ready.is_set() and isinstance(work.error, MemoryBudgetExceeded)
                    and not work.admitted.is_set() and self._on_admission_error is not None):
                # Policy belongs to the owner. The callback may create only
                # its small diagnostic record; any persistence or additional
                # buffer has to use the owner's administrative admission.
                self._stop.checkpoint()
                result = self._on_admission_error(work.item, work.error)
                if not isinstance(result, ImmediateResult):
                    raise TypeError("on_admission_error must return ImmediateResult")
                work.value = result.value
                work.error = None
                work.item = None
                work.estimated_bytes = 0
            if work.prepared.is_set() or not work.admitted.is_set() or work.ready.is_set():
                continue
            try:
                self._stop.checkpoint()
                with _grant_scope(work.grant):
                    work.payload = self._prepare(work.item) if self._prepare else work.item
            except BaseException as exc:
                work.prepare_error = exc
            finally:
                work.item = None
                work.prepared.set()

    def _run(self, work: _Work[_Output]) -> None:
        try:
            while True:
                process_claim = (
                    self._processes.claim() if self._processes is not None else nullcontext()
                )
                with process_claim as claim:
                    admission = (
                        self._gate.admit(
                            work.estimated_bytes,
                            native_threads=self._native_threads,
                            io_slots=self._io_slots,
                            io_device=work.device,
                            phase=self._phase,
                            cancellation=self._stop,
                        ) if self._gate is not None else nullcontext()
                    )
                    with admission as grant:
                        if claim is not None and not claim.activate():
                            # The pool became idle while this task waited for
                            # resources and was retired under pressure. Return
                            # the task grant before reserving another process.
                            self._stop.checkpoint()
                            continue
                        self._execute(work, grant, claim)
                        break
        except BaseException as exc:
            work.error = exc
        finally:
            if not (isinstance(work.error, MemoryBudgetExceeded)
                    and not work.admitted.is_set() and self._on_admission_error is not None):
                work.item = None
            work.payload = _MISSING
            work.prepare_error = None
            if self._stop.is_cancelled:
                work.value = _MISSING
            work.ready.set()
            work.finished.set()
            self._owner_wakeup.set()

    def _execute(self, work: _Work[_Output], grant: Any,
                 claim: _ProcessClaim | None) -> None:
        executor = None if claim is None else claim.executor
        work.grant = grant
        work.admitted.set()
        # The result cannot become ready until its caller prepares it. Wake
        # that caller on admission instead of waiting for its result poll.
        self._owner_wakeup.set()
        while not work.prepared.wait(self._poll):
            self._stop.checkpoint()
        self._stop.checkpoint()
        if work.prepare_error is not None:
            raise work.prepare_error
        payload, work.payload = work.payload, _MISSING
        try:
            if isinstance(payload, ImmediateResult):
                value = payload.value
            elif executor is None or (
                self._process_predicate is not None and not self._process_predicate(payload)
            ):
                cancellation_token = _WORKER_CANCELLATION.set(CancellationToken(parent=self._stop))
                try:
                    with _grant_scope(grant):
                        value = self._function(payload)
                finally:
                    _WORKER_CANCELLATION.reset(cancellation_token)
            else:
                environment = {} if grant is None else dict(grant.native_env)
                future = executor.submit(
                    _process_call, self._function, payload, environment
                )
                while True:
                    try:
                        value = future.result(timeout=self._poll)
                        break
                    except FutureTimeout:
                        if future.done():
                            # A payload may itself raise TimeoutError.
                            # That is not a polling timeout.
                            raise
                        if self._stop.is_cancelled:
                            future.cancel()
                            # Keep its lease until the owned process
                            # finishes; a cancelled wait is not a kill.
                            try:
                                future.result()
                            finally:
                                self._stop.checkpoint()
                del future
        finally:
            payload = None
            if claim is not None:
                claim.release()
            if grant is not None:
                grant.release_cpu()
        self._stop.checkpoint()
        work.value = value
        del value
        work.ready.set()
        self._owner_wakeup.set()
        while not work.release.wait(self._poll):
            self._stop.checkpoint()
        work.value = _MISSING

    def _release_current(self) -> None:
        work, self._current = self._current, None
        scope, self._consumer_scope = self._consumer_scope, None
        if work is not None and work.grant is not None:
            # The owner may have renewed execution for serialization/SQLite.
            # Close that context here, before the supervisor releases the
            # retained result lease on its own thread.
            work.grant.release_cpu()
        if scope is not None:
            scope.__exit__(None, None, None)
        if work is not None:
            work.value = _MISSING
            work.release.set()
            work.finished.wait()
            if work.error is not None and not self._stop.is_cancelled:
                raise work.error

    def __next__(self) -> _Output:
        self._check_owner()
        if self._closed:
            raise StopIteration
        try:
            self._release_current()
            while True:
                # Clear BEFORE checking per-work state. A publication before
                # this clear is still visible in admitted/ready; one after it
                # leaves the event set through wait(), avoiding a lost wakeup.
                self._owner_wakeup.clear()
                self._checkpoint()
                self._fill()
                self._prepare_ready()
                if not self._pending and self._exhausted:
                    self.close()
                    raise StopIteration
                if self._pending and self._pending[0].ready.is_set():
                    self._stop.checkpoint()
                    work = self._pending.popleft()
                    self._current = work
                    if work.error is not None:
                        raise work.error
                    scope = _consumer_grant_scope(work.grant)
                    scope.__enter__()
                    self._consumer_scope = scope
                    return work.value
                # Preserve a ready first result for publication, but never
                # hide a fatal later sibling behind an earlier slow task.
                # Closing signals only this map and drains its live leases.
                for pending in self._pending:
                    if pending.ready.is_set() and pending.error is not None:
                        raise pending.error
                # All workers may make preparation or a fatal error ready,
                # including siblings behind a slow first result. The timeout
                # remains a fallback for capacity/deadline/cancellation probes.
                if self._pending:
                    self._owner_wakeup.wait(self._poll)
                else:
                    # With no producer to signal this map, retain the token's
                    # bounded observation of parent cancellation while idle.
                    self._stop.wait(self._poll)
        except StopIteration:
            raise
        except BaseException as primary:
            self._close_preserving_error(primary)
            raise

    def close(self) -> None:
        self._check_owner()
        if self._closed:
            return
        self._closed = True
        self._stop.cancel()
        if self._current is not None and self._current.grant is not None:
            self._current.grant.release_cpu()
        if self._consumer_scope is not None:
            self._consumer_scope.__exit__(None, None, None)
            self._consumer_scope = None
        work_items = list(self._pending)
        if self._current is not None:
            work_items.append(self._current)
        for work in work_items:
            work.value = _MISSING
            work.release.set()
            work.prepared.set()
        # Supervisors own admission lifetime and drain payloads before process
        # executors close.  No cancellation escapes to a sibling map/token.
        self._supervisors.close()
        if self._processes is not None:
            self._processes.close()
        self._pending.clear()
        self._current = None
        close_source = getattr(self._source, "close", None)
        if callable(close_source):
            close_source()


def elastic_map(
    function: Callable[..., _Output],
    iterable: Iterable[_Input],
    **kwargs: Any,
) -> ElasticMap[_Input, _Output]:
    """Create an ordered elastic map; see :class:`ElasticMap` for its contract."""

    return ElasticMap(function, iterable, **kwargs)
