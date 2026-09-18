from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    ResourceWaitTimeout,
)


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("bounded wait did not observe the expected state")
        time.sleep(0.005)


def _coordinator(
    parent: CancellationToken | None = None,
    *,
    headroom: int = 10_000,
    timeout: float = 2,
    probe: Callable[[], object] | None = None,
) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("holder", "image", "text"),
        GlobalResourceLimits(
            memory_budget_bytes=100,
            min_free_memory_bytes=1,
            min_free_commit_bytes=1,
            cpu_slots=1,
            native_thread_slots=1,
            temp_budget_bytes=100,
            wait_timeout_seconds=timeout,
            poll_interval_seconds=5,
            memory_hysteresis_bytes=0,
        ),
        cancellation=parent,
        cpu_load_probe=lambda: 0,
        resource_probe=probe or (
            lambda: ResourceSample(
                available_physical=headroom,
                available_commit=headroom,
                total_physical=20_000,
                total_commit=20_000,
            )
        ),
    )


def _assert_resources_released(coordinator: GlobalResourceCoordinator) -> None:
    summary = coordinator.summary()
    assert summary.resident_bytes == 0
    assert summary.transient_bytes == 0
    assert summary.temp_bytes == 0
    assert summary.native_threads == 0
    with coordinator.admit("text", 100, native_threads=1, transient_bytes=100):
        pass


def test_child_cancellation_is_local_and_does_not_poison_siblings() -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    sibling = CancellationToken(parent=parent)
    child.cancel()
    assert child.is_cancelled and child.wait(0)
    with pytest.raises(CancellationRequested):
        child.checkpoint()
    assert not parent.is_cancelled and not sibling.is_cancelled
    assert not parent.wait(0) and not sibling.wait(0)
    parent.checkpoint()
    sibling.checkpoint()


@pytest.mark.parametrize("timeout", [None, 5.0])
def test_parent_wakes_multiple_nested_child_waiters(timeout: float | None) -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    grandchild = CancellationToken(parent=child)
    ready = threading.Barrier(3)
    results: list[bool] = []

    def wait(token: CancellationToken) -> None:
        ready.wait(timeout=2)
        results.append(token.wait(timeout))

    threads = [threading.Thread(target=wait, args=(token,)) for token in (child, grandchild)]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=2)
        parent.cancel()
        for thread in threads:
            thread.join(0.75)
        assert not any(thread.is_alive() for thread in threads)
        assert results == [True, True]
        with pytest.raises(CancellationRequested):
            grandchild.checkpoint()
    finally:
        child.cancel()
        grandchild.cancel()
        for thread in threads:
            thread.join(2)


@pytest.mark.parametrize("timeout", [-1.0, 0.0, 0.025])
def test_child_wait_timeout_does_not_cancel_either_token(timeout: float) -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    started = time.monotonic()
    assert child.wait(timeout) is False
    elapsed = time.monotonic() - started
    assert elapsed >= max(0.0, timeout - 0.005)
    assert elapsed < 1
    assert not child.is_cancelled and not parent.is_cancelled


def test_local_cancel_wakes_a_child_waiter_without_parent_cancellation() -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    entered = threading.Event()
    result: list[bool] = []

    def wait() -> None:
        entered.set()
        result.append(child.wait())

    thread = threading.Thread(target=wait)
    thread.start()
    assert entered.wait(1)
    child.cancel()
    thread.join(0.75)
    assert not thread.is_alive()
    assert result == [True]
    assert not parent.is_cancelled


def test_child_created_after_parent_cancellation_is_already_cancelled() -> None:
    parent = CancellationToken()
    parent.cancel()
    child = CancellationToken(parent=parent)
    assert child.is_cancelled
    assert child.wait(0)
    with pytest.raises(CancellationRequested):
        child.checkpoint()


def test_cancelled_request_never_enters_queue_or_charges_resources() -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    coordinator = _coordinator(parent)
    child.cancel()
    with pytest.raises(CancellationRequested), coordinator.admit(
        "image", 20, cancellation=child
    ):
        pytest.fail("cancelled request was granted")
    summary = coordinator.summary()
    assert summary.routes["image"].admissions == 0
    assert summary.routes["image"].waits == 0
    assert not parent.is_cancelled
    _assert_resources_released(coordinator)


def test_cancel_one_of_two_queued_routes_preserves_the_other_and_all_counters() -> None:
    parent = CancellationToken()
    image = CancellationToken(parent=parent)
    text = CancellationToken(parent=parent)
    coordinator = _coordinator(parent)
    outcomes: dict[str, str] = {}

    def run(name: str, token: CancellationToken) -> None:
        try:
            with CoordinatedMemoryGate(coordinator, name, cancellation=token).admit(20):
                outcomes[name] = "granted"
        except CancellationRequested:
            outcomes[name] = "cancelled"

    threads = [
        threading.Thread(target=run, args=("image", image)),
        threading.Thread(target=run, args=("text", text)),
    ]
    try:
        with coordinator.admit("holder", 100):
            for thread in threads:
                thread.start()
            _wait_until(lambda: coordinator.route_wait_count("image") == 1
                        and coordinator.route_wait_count("text") == 1)
            image.cancel()
            threads[0].join(0.75)
            assert not threads[0].is_alive()
            assert outcomes == {"image": "cancelled"}
            assert threads[1].is_alive()
            assert not parent.is_cancelled and not text.is_cancelled
        threads[1].join(1)
        assert not threads[1].is_alive()
        assert outcomes == {"image": "cancelled", "text": "granted"}
        assert coordinator.summary().routes["image"].admissions == 0
        assert coordinator.summary().routes["text"].admissions == 1
        _assert_resources_released(coordinator)
    finally:
        coordinator.cancel()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)


@pytest.mark.parametrize("cancel_owner", ["parent", "coordinator"])
def test_owner_cancellation_still_interrupts_scoped_global_wait(cancel_owner: str) -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    coordinator = _coordinator(parent)
    errors: list[BaseException] = []

    def wait() -> None:
        try:
            with coordinator.admit("image", 20, cancellation=child):
                pytest.fail("owner-cancelled request was granted")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=wait)
    try:
        with coordinator.admit("holder", 100):
            thread.start()
            _wait_until(lambda: coordinator.route_wait_count("image") == 1)
            (parent.cancel if cancel_owner == "parent" else coordinator.cancel)()
            thread.join(0.75)
            assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CancellationRequested)
        assert coordinator.summary().transient_bytes == 0
    finally:
        coordinator.cancel()
        if thread.ident is not None:
            thread.join(2)


def test_cancellation_during_probe_is_checked_before_admission() -> None:
    child = CancellationToken()
    armed = False

    def probe() -> ResourceSample:
        if armed:
            child.cancel()
        return ResourceSample(10_000, 10_000, 20_000, 20_000)

    coordinator = _coordinator(probe=probe)
    armed = True
    with pytest.raises(CancellationRequested), coordinator.admit(
        "image", 20, cancellation=child
    ):
        pytest.fail("request cancelled during its sample was granted")
    assert coordinator.summary().routes["image"].admissions == 0
    armed = False
    _assert_resources_released(coordinator)


def test_primary_error_identity_survives_local_cancel_and_releases_admission() -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    coordinator = _coordinator(parent)
    primary = ResourceWaitTimeout("original image memory timeout", reason="memory")
    with pytest.raises(ResourceWaitTimeout) as captured, coordinator.admit(
        "image", resident_bytes=10, transient_bytes=10, temp_bytes=10,
        native_threads=1, cancellation=child,
    ):
        child.cancel()
        raise primary
    assert captured.value is primary
    assert not parent.is_cancelled
    _assert_resources_released(coordinator)


def test_request_headroom_timeout_keeps_cancellation_and_cleanup_distinct() -> None:
    parent = CancellationToken()
    child = CancellationToken(parent=parent)
    coordinator = _coordinator(parent, headroom=0, timeout=0.025)
    with pytest.raises(ResourceWaitTimeout) as captured, coordinator.admit(
        "image", 20, cancellation=child
    ):
        pytest.fail("impossible headroom was granted")
    assert captured.value.reason == "memory"
    assert not child.is_cancelled and not parent.is_cancelled
    summary = coordinator.summary()
    assert summary.routes["image"].admissions == 0
    assert summary.routes["image"].waits == 1
    assert summary.transient_bytes == 0
