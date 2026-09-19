"""Model residence is distinct from renewable work and retained result memory."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Callable

import pytest

from neocortex.capabilities.formats.media_resources import (
    MediaTaskGate, ResidentMediaGate, current_media_resource,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.elastic_workers import current_worker_cancellation, elastic_map


class _Grant:
    def __init__(self, gate, cpu):
        self.gate = gate
        self.cpu = cpu
        self.native_threads = cpu
        self.released = False

    def release_cpu(self):
        with self.gate.condition:
            if not self.released:
                self.gate.cpu -= self.cpu
                self.released = True
                self.gate.condition.notify_all()


class _Gate:
    def __init__(self, capacity=8):
        self.coordinator = self
        self.limits = SimpleNamespace(wait_timeout_seconds=2.0)
        self.capacity = capacity
        self.cpu = self.ram = self.resident = 0
        self.requests = []
        self.condition = threading.Condition()
        self.block_execution = False
        self.cancellation: CancellationToken | None = None
        self.checkpoint: Callable[[], None] = lambda: None

    def worker_capacity(self, *, max_workers=None, **_kwargs):
        return self.capacity if max_workers is None else min(self.capacity, max_workers)

    @contextmanager
    def native_budget(self, amount, *, max_threads, **kwargs):
        with self.admit(amount, cpu_slots=max_threads, native_threads=max_threads, **kwargs) as grant:
            yield grant

    @contextmanager
    def admit(self, amount, *, cpu_slots=1, resident_bytes=0, cancellation, **kwargs):
        owner = threading.get_ident()
        with self.condition:
            while cpu_slots and (self.block_execution or self.cpu + cpu_slots > self.capacity):
                cancellation.checkpoint()
                self.condition.wait(0.01)
            cancellation.checkpoint()
            self.requests.append((amount, cpu_slots, resident_bytes, kwargs))
            self.cpu += cpu_slots
            self.ram += amount + resident_bytes
            self.resident += resident_bytes
        grant = _Grant(self, cpu_slots)
        try:
            yield grant
        finally:
            assert threading.get_ident() == owner
            grant.release_cpu()
            with self.condition:
                self.ram -= amount + resident_bytes
                self.resident -= resident_bytes
                self.condition.notify_all()


class _Model:
    def __init__(self, gate, closed):
        self.gate = gate
        self.closed = closed

    def close(self):
        assert self.gate.resident > 0
        self.closed.append(self)


def _eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_multifile_parallelism_exceeds_old_four_worker_ceiling_and_holds_results():
    base, closed = _Gate(), []
    pool = ResidentMediaGate(base, lambda: _Model(base, closed), resident_bytes=10,
                             cancellation=CancellationToken())
    all_started = threading.Event()
    release = threading.Event()
    started = []
    lock = threading.Lock()

    def work(value):
        assert current_media_resource() is not None
        with lock:
            started.append(value)
            if len(started) == 8:
                all_started.set()
        assert release.wait(2)
        return value

    def observe():
        assert all_started.wait(2)
        assert base.cpu == 8
        release.set()

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        with elastic_map(work, range(8), gate=pool, estimated_bytes=5,
                         cancellation=CancellationToken()) as results:
            assert next(results) == 0
            assert base.ram == 8 * (10 + 5)
            assert base.resident == 80
            assert list(results) == list(range(1, 8))
        assert base.cpu == 0
        assert base.ram == base.resident
    finally:
        release.set()
        observer.join()
        pool.close()
    assert base.ram == 0
    assert len(closed) == 8


def test_single_model_renews_native_budget_and_reuses_only_while_resident():
    base, closed = _Gate(), []
    pool = ResidentMediaGate(base, lambda: _Model(base, closed), resident_bytes=10,
                             cancellation=CancellationToken(), variable_native_threads=True)
    identities = []
    try:
        for capacity in (8, 1, 8):
            base.capacity = capacity
            assert pool.worker_capacity(max_workers=1, estimated_bytes=5) == 1
            with pool.admit(5) as grant:
                identities.append(current_media_resource())
                assert grant.native_threads == capacity
            assert base.cpu == 0 and base.resident == 10
        assert identities[0] is identities[1] is identities[2]
        base.capacity = 0
        assert pool.worker_capacity(max_workers=1, estimated_bytes=5) == 0
        assert base.ram == 0
        base.capacity = 8
        assert pool.worker_capacity(max_workers=1, estimated_bytes=5) == 1
        with pool.admit(5):
            assert current_media_resource() is not identities[0]
    finally:
        pool.close()
    assert len(closed) == 2


def test_waiting_execution_drops_idle_model_on_pressure_then_reloads():
    base, closed = _Gate(1), []
    pool = ResidentMediaGate(base, lambda: _Model(base, closed), resident_bytes=10,
                             cancellation=CancellationToken())
    pool.worker_capacity(max_workers=1, estimated_bytes=5)
    with pool.admit(5):
        pass
    base.block_execution = True

    def work():
        with pool.admit(5):
            return current_media_resource()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(work)
            _eventually(lambda: any(slot.waiting is not None for slot in pool._slots))
            base.capacity = 0
            pool.worker_capacity(max_workers=1, estimated_bytes=5)
            _eventually(lambda: base.resident == 0)
            assert not future.done()
            base.capacity = 1
            base.block_execution = False
            pool.worker_capacity(max_workers=1, estimated_bytes=5)
            assert future.result(timeout=2) is not closed[0]
    finally:
        pool.close()
    assert base.ram == base.cpu == 0


@pytest.mark.parametrize("resident", [False, True])
def test_wrapped_gate_observes_owner_deadline_while_media_is_running(resident):
    base = _Gate(1)
    closed: list[_Model] = []
    started = threading.Event()
    cancellation = CancellationToken()
    base.cancellation = cancellation

    def checkpoint():
        if started.is_set():
            raise TimeoutError("owning route deadline")

    base.checkpoint = checkpoint
    gate = (
        ResidentMediaGate(base, lambda: _Model(base, closed), resident_bytes=10,
                          cancellation=cancellation)
        if resident else MediaTaskGate(base)
    )

    def work(_value):
        token = current_worker_cancellation()
        assert token is not None
        started.set()
        token.wait(0.5)
        token.checkpoint()
        raise AssertionError("the owner did not observe its deadline")

    try:
        with pytest.raises(TimeoutError, match="owning route deadline"):
            with elastic_map(work, [1], gate=gate, max_workers=1,
                             estimated_bytes=5, cancellation=cancellation) as results:
                next(results)
    finally:
        if isinstance(gate, ResidentMediaGate):
            gate.close()
    assert started.is_set()
    assert base.cpu == base.ram == 0
    assert not cancellation.is_cancelled
