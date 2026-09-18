from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from neocortex.capabilities.formats.pdf import pdf_runtime
from neocortex.runtime.control import memory_runtime
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.cpu_runtime import CpuLoadSampler
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    ResourceWaitTimeout,
)


def _sample(cpu_load: float | None = None) -> ResourceSample:
    return ResourceSample(10_000, 10_000, 20_000, 20_000, cpu_load_percent=cpu_load)


def _coordinator(
    *,
    resource_probe: Callable[[], object] = _sample,
    cpu_load_probe: Callable[[], float | None] | None = None,
) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("holder", "pdf", "sibling"),
        GlobalResourceLimits(
            memory_budget_bytes=1_000,
            min_free_memory_bytes=10,
            min_free_commit_bytes=10,
            cpu_slots=4,
            native_thread_slots=4,
            max_cpu_load_percent=90,
            cpu_hysteresis_percent=5,
            memory_hysteresis_bytes=0,
            wait_timeout_seconds=0,
            poll_interval_seconds=5,
        ),
        resource_probe=resource_probe,
        cpu_load_probe=cpu_load_probe,
    )


def _pdf_gate(
    root: Path,
    token: CancellationToken,
    coordinator: GlobalResourceCoordinator | None = None,
) -> pdf_runtime.PdfResourceGate:
    return pdf_runtime.PdfResourceGate(
        pdf_runtime.PdfResourceLimits(
            min_free_bytes=1,
            memory_backpressure_bytes=0,
            commit_backpressure_bytes=0,
            memory_wait_timeout_seconds=0,
            memory_budget_bytes=100,
            worker_memory_bytes=20,
            large_document_bytes=1,
            large_document_workers=1,
        ),
        root,
        global_coordinator=coordinator,
        cancellation=token,
    )


def _assert_released(coordinator: GlobalResourceCoordinator) -> None:
    summary = coordinator.summary()
    assert summary.resident_bytes == 0
    assert summary.transient_bytes == 0
    assert summary.temp_bytes == 0
    assert summary.native_threads == 0
    assert all(coordinator.route_active_request_count(route) == 0 for route in summary.routes)


def test_pdf_local_cancel_releases_global_wait_without_waiting_for_another_route(
    tmp_path: Path,
) -> None:
    parent = CancellationToken()
    token = CancellationToken(parent=parent)
    coordinator = _coordinator(cpu_load_probe=lambda: 0)
    gate = _pdf_gate(tmp_path, token, coordinator)
    finished = threading.Event()
    entered = threading.Event()
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            with gate.admit(2):
                entered.set()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker)
    try:
        with coordinator.admit("holder", 40, cpu_slots=4):
            thread.start()
            deadline = time.monotonic() + 2
            while coordinator.route_wait_count("pdf") == 0:
                assert time.monotonic() < deadline, "PDF did not reach the global queue"
                finished.wait(0.005)
            token.cancel()
            assert finished.wait(0.75), "PDF waited for unrelated work after local cancellation"
            assert not entered.is_set()
            assert len(failures) == 1 and isinstance(failures[0], CancellationRequested)
            assert not parent.is_cancelled and not coordinator.cancellation.is_cancelled
            summary = coordinator.summary()
            assert summary.routes["pdf"].admissions == 0
            assert summary.routes["pdf"].waits == 1
            assert summary.routes["pdf"].wait_ns > 0
            assert summary.transient_bytes == 40
            assert coordinator.route_active_request_count("holder") == 1
            assert gate.active_count == 0
            assert gate._large_slots.acquire(blocking=False)
            gate._large_slots.release()
        with coordinator.admit("sibling", 1_000, cpu_slots=4):
            assert coordinator.resource_usage().transient_bytes == 1_000
        _assert_released(coordinator)
    finally:
        token.cancel()
        coordinator.cancel()
        if thread.ident is not None:
            thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("headroom", [0, 10_000])
def test_weighted_memory_probe_cancellation_precedes_admission_and_timeout(
    monkeypatch: pytest.MonkeyPatch, headroom: int,
) -> None:
    token = CancellationToken()
    gate = memory_runtime.WeightedMemoryGate(
        memory_runtime.MemoryResourceLimits(
            memory_budget_bytes=100,
            min_free_memory_bytes=1,
            min_free_commit_bytes=1,
            wait_timeout_seconds=0,
        ),
        cancellation=token,
    )

    def probe() -> memory_runtime.MemorySnapshot:
        token.cancel()
        return memory_runtime.MemorySnapshot(headroom, headroom, 20_000, 20_000)

    monkeypatch.setattr(memory_runtime, "memory_snapshot", probe)
    with pytest.raises(CancellationRequested), gate.admit(20):
        pytest.fail("cancelled memory admission entered the work body")
    assert gate._reserved == 0
    assert not gate._headroom_admission_lock.locked()
    assert gate.peak_reserved_bytes == 20


@pytest.mark.parametrize("headroom", [0, 10_000])
def test_pdf_memory_probe_cancellation_precedes_admission_and_timeout(
    monkeypatch: pytest.MonkeyPatch, headroom: int,
) -> None:
    token = CancellationToken()

    def probe() -> pdf_runtime.MemorySnapshot:
        token.cancel()
        return pdf_runtime.MemorySnapshot(20_000, headroom, 20_000, headroom)

    monkeypatch.setattr(pdf_runtime, "memory_snapshot", probe)
    with pytest.raises(CancellationRequested):
        pdf_runtime.wait_for_available_memory(1, 0, 1, token)


@pytest.mark.parametrize("coordinated", [False, True])
def test_pdf_disk_probe_cancellation_releases_admission_before_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coordinated: bool,
) -> None:
    token = CancellationToken()
    coordinator = _coordinator(cpu_load_probe=lambda: 0) if coordinated else None
    gate = _pdf_gate(tmp_path, token, coordinator)

    def probe(_path: Path, _minimum_bytes: int) -> None:
        token.cancel()

    monkeypatch.setattr(pdf_runtime, "ensure_free_space", probe)
    with pytest.raises(CancellationRequested), gate.admit(2):
        pytest.fail("cancelled PDF admission entered the work body")
    assert gate.active_count == 0
    assert gate._large_slots.acquire(blocking=False)
    gate._large_slots.release()
    if coordinator is not None:
        assert not coordinator.cancellation.is_cancelled
        _assert_released(coordinator)
        with coordinator.admit("sibling", 1_000, cpu_slots=4):
            pass
    else:
        assert gate._budget._reserved == 0


@pytest.mark.parametrize("signal", ["cpu_probe", "resource_sample", "resource_mapping"])
def test_explicit_cpu_signals_share_pressure_and_exact_hysteresis(
    monkeypatch: pytest.MonkeyPatch, signal: str,
) -> None:
    load: list[float | None] = [89.99]
    monkeypatch.setattr(CpuLoadSampler, "sample", lambda _self: 0)

    def resource_probe() -> object:
        if signal == "resource_mapping":
            return {
                "available_physical": 10_000,
                "available_commit": 10_000,
                "total_physical": 20_000,
                "total_commit": 20_000,
                "cpu_load": load[0],
            }
        return _sample(load[0] if signal == "resource_sample" else None)

    coordinator = _coordinator(
        resource_probe=resource_probe,
        cpu_load_probe=(lambda: load[0]) if signal == "cpu_probe" else None,
    )
    with coordinator.admit("pdf", 20):
        pass
    assert not coordinator.summary().admission_paused

    # A missing owned signal cannot use the default sampler to prove recovery.
    for value in (90, 85.01, None):
        load[0] = value
        with pytest.raises(ResourceWaitTimeout) as raised, coordinator.admit("pdf", 20):
            pytest.fail("explicit CPU pressure was ignored or recovered too early")
        assert raised.value.reason == "cpu"
        assert coordinator.summary().admission_paused
        _assert_released(coordinator)

    load[0] = 85
    with coordinator.admit("pdf", 20):
        pass
    summary = coordinator.summary()
    assert not summary.admission_paused
    assert summary.pressure_events == 2
    assert summary.routes["pdf"].admissions == 2
    assert summary.max_observed_cpu_load_percent == 90
    _assert_released(coordinator)


def test_default_cpu_sample_remains_telemetry_at_full_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CpuLoadSampler, "sample", lambda _self: 100)
    coordinator = _coordinator()
    with coordinator.admit("holder", 20, cpu_slots=3):
        with coordinator.admit("pdf", 20):
            summary = coordinator.summary()
            assert summary.peak_cpu_slots == 4
            assert summary.min_effective_cpu_slots == 4
            assert not summary.admission_paused
            assert summary.max_observed_cpu_load_percent == 100
            assert summary.pressure_events == 0
    _assert_released(coordinator)
