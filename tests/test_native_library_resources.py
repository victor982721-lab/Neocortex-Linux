from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate, GlobalResourceCoordinator, GlobalResourceLimits,
)
from neocortex.runtime.control.native_library_resources import native_library_operation

TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _gate() -> CoordinatedMemoryGate:
    coordinator = GlobalResourceCoordinator(("semantic",), GlobalResourceLimits(
        cpu_slots=4, memory_budget_bytes=1024 * 1024, min_free_memory_bytes=0,
        min_free_commit_bytes=0, wait_timeout_seconds=2, poll_interval_seconds=0.01,
    ), cpu_load_probe=lambda: 0)
    return CoordinatedMemoryGate(coordinator, "semantic")


def test_actual_blas_threads_are_limited_and_restored_before_lease_release() -> None:
    numpy = pytest.importorskip("numpy")
    threadpoolctl = pytest.importorskip("threadpoolctl")
    numpy.ones((8, 8)) @ numpy.ones((8, 8))
    before = {r["filepath"]: r["num_threads"] for r in threadpoolctl.threadpool_info()
              if r["user_api"] == "blas"}
    if not before:
        pytest.skip("NumPy has no controllable BLAS runtime")
    gate = _gate()
    with pytest.raises(ValueError, match="injected"):
        with native_library_operation(gate, 4096, max_threads=2) as grant:
            assert grant.native_threads == 2
            assert all(r["num_threads"] == 2 for r in threadpoolctl.threadpool_info()
                       if r["user_api"] == "blas")
            assert (numpy.ones((8, 8)) @ numpy.ones((8, 8)))[0, 0] == 8
            raise ValueError("injected")
    after = {r["filepath"]: r["num_threads"] for r in threadpoolctl.threadpool_info()
             if r["user_api"] == "blas"}
    assert after == before
    assert gate.coordinator.summary().native_threads == 0


def test_waiting_for_process_blas_lock_holds_no_cpu_and_honors_local_cancellation() -> None:
    pytest.importorskip("threadpoolctl")
    gate = _gate()
    started = threading.Event()
    cancellation = CancellationToken()
    def wait_for_blas():
        started.set()
        with native_library_operation(gate, 4096, cancellation=cancellation):
            pytest.fail("cancelled waiter acquired the BLAS lock")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with native_library_operation(gate, 4096, max_threads=1):
            future = pool.submit(wait_for_blas)
            assert started.wait(1)
            assert gate.coordinator.summary().native_threads == 1
            cancellation.cancel()
            with pytest.raises(CancellationRequested):
                future.result(timeout=2)
    assert gate.coordinator.summary().active_execution_requests == 0
