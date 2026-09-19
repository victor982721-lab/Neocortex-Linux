"""Explicit route ceilings share the coordinator's existing resource ledger."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

import pytest

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    ResourceWaitTimeout,
)
from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded


def _sample(total: int = 8192) -> ResourceSample:
    return ResourceSample(
        available_physical=total, available_commit=total,
        total_physical=total, total_commit=total,
        effective_cpu_capacity=8, external_cpu_cores=0,
    )


def _coordinator(
    budgets: Mapping[str, int] | None = None,
    *,
    samples: list[ResourceSample] | None = None,
    memory_budget_bytes: int | None = 1024,
    checkpoint: Callable[[], None] | None = None,
    wait_timeout_seconds: float = 0.3,
) -> GlobalResourceCoordinator:
    values = samples if samples is not None else [_sample()]
    return GlobalResourceCoordinator(
        ("image", "other"),
        GlobalResourceLimits(
            memory_budget_bytes=memory_budget_bytes, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=8, poll_interval_seconds=0.005,
            sample_interval_seconds=0.01, wait_timeout_seconds=wait_timeout_seconds,
            memory_hysteresis_bytes=0, temp_budget_bytes=4096,
            gpu_memory_bytes={"gpu": 1024},
        ),
        route_memory_budgets=budgets, resource_probe=lambda: values[0],
        effective_cpu_probe=lambda: 8, checkpoint=checkpoint,
    )


def _reserved(coordinator: GlobalResourceCoordinator, route: str) -> int:
    usage = coordinator.route_resource_usage(route)
    return (usage.resident_bytes or 0) + (usage.transient_bytes or 0)


def _wait_for_waiter(coordinator: GlobalResourceCoordinator, route: str = "image") -> None:
    deadline = time.monotonic() + 1
    while coordinator.route_wait_count(route) == 0 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert coordinator.route_wait_count(route) == 1


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, None, "128"])
def test_route_budget_requires_a_positive_integer(budget):
    with pytest.raises(ValueError, match="positive integers"):
        _coordinator({"image": budget})


@pytest.mark.parametrize("name", ["", "   ", 1])
def test_route_budget_rejects_invalid_names(name):
    with pytest.raises(ValueError, match="route names"):
        _coordinator({name: 128})


def test_route_budgets_are_copied_and_allow_later_route_registration():
    budgets = {"image": 128, "late": 256}
    coordinator = _coordinator(budgets)
    budgets["image"] = 1
    budgets["other"] = 1
    assert coordinator.route_memory_budget_bytes("image") == 128
    assert coordinator.route_memory_budget_bytes("other") == 1024
    coordinator.register_route("late")
    assert coordinator.route_memory_budget_bytes("late") == 256
    with coordinator.admit("late", 256):
        assert _reserved(coordinator, "late") == 256


@pytest.mark.parametrize("budgets", [None, {}])
def test_unconfigured_routes_keep_automatic_global_growth_and_contraction(budgets):
    samples = [_sample(1024)]
    coordinator = _coordinator(budgets, samples=samples, memory_budget_bytes=None)
    first = coordinator.route_memory_budget_bytes("image")
    assert first == 992
    samples[0] = _sample(4096)
    assert coordinator.route_memory_budget_bytes("image") == 3968
    samples[0] = _sample(512)
    assert coordinator.route_memory_budget_bytes("other") == 496


def test_route_ceiling_cannot_override_live_global_budget():
    samples = [_sample(1024)]
    coordinator = _coordinator({"image": 2000}, samples=samples, memory_budget_bytes=None)
    assert coordinator.route_memory_budget_bytes("image") == 992
    samples[0] = _sample(4096)
    assert coordinator.route_memory_budget_bytes("image") == 2000
    samples[0] = _sample(512)
    assert coordinator.route_memory_budget_bytes("image") == 496
    with pytest.raises(MemoryBudgetExceeded, match="global budget"):
        with coordinator.admit("image", 500):
            pytest.fail("work exceeded the live global budget")


def test_oversized_route_item_fails_before_queue_or_worker_creation():
    coordinator = _coordinator({"image": 128})
    gate = CoordinatedMemoryGate(coordinator, "image")
    with pytest.raises(MemoryBudgetExceeded, match="route memory budget"):
        gate.worker_capacity(estimated_bytes=129)
    with pytest.raises(MemoryBudgetExceeded, match="route budget"):
        with gate.admit(65, resident_bytes=64):
            pytest.fail("oversized item was admitted")
    assert coordinator.route_wait_count("image") == 0
    assert coordinator.route_active_request_count("image") == 0
    assert _reserved(coordinator, "image") == 0


def test_concurrent_route_cap_waits_without_blocking_an_independent_route():
    coordinator = _coordinator({"image": 128})
    cancellation = CancellationToken()
    gate = CoordinatedMemoryGate(coordinator, "image", cancellation=cancellation)
    entered = threading.Event()
    errors: list[BaseException] = []

    def waiter():
        try:
            with gate.admit(96):
                assert _reserved(coordinator, "image") <= 128
                entered.set()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=waiter)
    try:
        with gate.admit(96):
            worker.start()
            _wait_for_waiter(coordinator)
            assert not entered.is_set()
            with coordinator.admit("other", 512):
                assert _reserved(coordinator, "image") == 96
                assert _reserved(coordinator, "other") == 512
                assert coordinator.summary().transient_bytes == 608
        worker.join(1)
        assert not worker.is_alive()
        assert entered.is_set()
        assert not errors
    finally:
        cancellation.cancel()
        worker.join(1)
    assert _reserved(coordinator, "image") == _reserved(coordinator, "other") == 0
    assert coordinator.route_peak_reserved_bytes("image") == 96


def test_shared_residence_uses_its_incremental_charge_and_refunds_growth():
    coordinator = _coordinator({"image": 256})
    gate = CoordinatedMemoryGate(coordinator, "image")
    with gate.resident(128, resident_key="model"):
        with gate.admit(64, cpu_slots=0, native_threads=0,
                        resident_bytes=192, resident_key="model"):
            assert _reserved(coordinator, "image") == 256
            assert coordinator.summary().resident_bytes == 192
            with gate.resident(128, resident_key="model"):
                assert _reserved(coordinator, "image") == 256
            # RAM policy does not accidentally count disk or GPU reservations.
            with gate.admit(0, cpu_slots=0, native_threads=0, temp_bytes=512,
                            gpu_bytes=512, gpu_device="gpu"):
                assert _reserved(coordinator, "image") == 256
                assert coordinator.summary().temp_bytes == coordinator.summary().gpu_bytes == 512
        assert _reserved(coordinator, "image") == 128
        with gate.admit(128, cpu_slots=0, native_threads=0,
                        resident_bytes=64, resident_key="model"):
            assert _reserved(coordinator, "image") == 256
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0
    assert coordinator.route_peak_reserved_bytes("image") == 256


def test_worker_target_reuses_only_its_route_residence_without_double_counting():
    coordinator = _coordinator({"image": 224})
    image = CoordinatedMemoryGate(coordinator, "image")
    other = CoordinatedMemoryGate(coordinator, "other")
    assert image.worker_capacity(estimated_bytes=112) == 2
    assert other.worker_capacity(estimated_bytes=112) == 8
    with image.resident(128, resident_key="two-interpreters"):
        assert image.worker_capacity(estimated_bytes=112) == 0
        assert image.worker_capacity(estimated_bytes=112, reusable_resident_bytes=128) == 2
        with image.admit(48):
            assert image.worker_capacity(estimated_bytes=112, reusable_resident_bytes=128) == 2
            with other.admit(512):
                assert image.worker_capacity(estimated_bytes=112, reusable_resident_bytes=128) == 2
    assert image.worker_capacity(estimated_bytes=112) == 2


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_blocked_route_preserves_original_interrupt_and_cleans_queue(interrupt):
    cancellation = CancellationToken()
    original = RuntimeError("route owner deadline")
    armed = True

    def checkpoint():
        if armed and coordinator.route_wait_count("image"):
            if interrupt == "cancel":
                cancellation.cancel()
            else:
                raise original

    coordinator = _coordinator({"image": 128}, checkpoint=checkpoint)
    gate = CoordinatedMemoryGate(coordinator, "image", cancellation=cancellation)
    with gate.resident(128, resident_key="retained"):
        expected = CancellationRequested if interrupt == "cancel" else RuntimeError
        with pytest.raises(expected) as failure:
            with gate.admit(1):
                pytest.fail("route ceiling was bypassed")
        if interrupt == "deadline":
            assert failure.value is original
        armed = False
        assert _reserved(coordinator, "image") == 128
        assert coordinator.route_active_request_count("image") == 1
        assert not coordinator.cancellation.is_cancelled
        with coordinator.admit("other", 512):
            assert _reserved(coordinator, "other") == 512
    with CoordinatedMemoryGate(coordinator, "image").admit(128):
        assert _reserved(coordinator, "image") == 128
    assert coordinator.route_active_request_count("image") == 0


def test_retained_route_capacity_timeout_is_typed_as_memory():
    coordinator = _coordinator({"image": 128}, wait_timeout_seconds=0.02)
    gate = CoordinatedMemoryGate(coordinator, "image")
    with gate.resident(128, resident_key="retained"):
        with pytest.raises(ResourceWaitTimeout) as failure:
            with gate.admit(1):
                pytest.fail("route ceiling was bypassed")
        assert failure.value.reason == "memory"
        assert _reserved(coordinator, "image") == 128
    assert coordinator.route_active_request_count("image") == 0


def test_releasing_transient_workspace_wakes_route_waiter_without_new_scope():
    coordinator = _coordinator({"image": 128})
    cancellation = CancellationToken()
    gate = CoordinatedMemoryGate(coordinator, "image", cancellation=cancellation)
    entered = threading.Event()
    errors: list[BaseException] = []

    def waiter():
        try:
            with gate.admit(64, cpu_slots=0, native_threads=0):
                assert _reserved(coordinator, "image") == 128
                entered.set()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=waiter)
    try:
        with gate.admit(128, cpu_slots=0, native_threads=0) as workspace:
            worker.start()
            _wait_for_waiter(coordinator)
            assert not entered.is_set()
            workspace.shrink_transient_bytes(64)
            worker.join(1)
            assert entered.is_set()
            assert not worker.is_alive()
            assert not errors
            assert _reserved(coordinator, "image") == 64
    finally:
        cancellation.cancel()
        worker.join(1)
    assert _reserved(coordinator, "image") == 0


def test_native_renewal_and_drain_keep_real_width_and_one_memory_charge():
    coordinator = _coordinator({"image": 128})
    gate = CoordinatedMemoryGate(coordinator, "image")
    with gate.resident(64, resident_key="backend"):
        with gate.native_budget(64, max_threads=4) as grant:
            assert grant.native_threads == 4
            grant.checkpoint()
            assert grant.native_threads == coordinator.summary().native_threads == 4
            assert _reserved(coordinator, "image") == 128
            grant.release_cpu()
            with grant.drain_admission(io_slots=0):
                assert coordinator.summary().native_threads == 1
                assert _reserved(coordinator, "image") == 128
            grant.checkpoint()
            assert grant.native_threads == coordinator.summary().native_threads == 4
        with gate.native_budget(64) as fresh:
            assert fresh.native_threads == 8
            assert _reserved(coordinator, "image") == 128
    assert _reserved(coordinator, "image") == coordinator.summary().native_threads == 0
