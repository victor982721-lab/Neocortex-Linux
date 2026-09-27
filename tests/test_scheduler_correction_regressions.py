"""Behavioral regressions for scheduler admission, completion order and I/O IDs."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from neocortex.runtime.control.elastic_workers import elastic_map
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)
from neocortex.runtime.control.io_identity import (
    canonical_io_device,
    normalize_io_device_slots,
)


def _coordinator(
    *,
    memory: int = 1024 * 1024,
    cpu: int = 2,
    io: int = 1,
    device_slots: dict[str, int] | None = None,
) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("audio", "other"),
        GlobalResourceLimits(
            memory_budget_bytes=memory,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=cpu,
            native_thread_slots=cpu,
            io_slots=io,
            io_device_slots=device_slots,
            wait_timeout_seconds=None,
            poll_interval_seconds=0.005,
        ),
        effective_cpu_probe=lambda: cpu,
        resource_probe=lambda: ResourceSample(
            available_physical=memory,
            available_commit=memory,
            total_physical=memory * 2,
            total_commit=memory * 2,
            cpu_load_percent=0,
        ),
        cpu_load_probe=lambda: 0,
    )


def test_producer_probe_cannot_cycle_with_unprepared_worker_grant() -> None:
    """A producer-side probe and its next worker finish without watchdog cancel."""

    coordinator = _coordinator(device_slots={"dev:7": 1})
    base = CoordinatedMemoryGate(coordinator, "audio")
    admitted = threading.Event()
    prepared: list[int] = []
    executed: list[int] = []
    coordinator_ref = coordinator

    def prepare_item(item: int) -> int:
        prepared.append(item)
        return item

    def execute_item(item: int) -> int:
        executed.append(item)
        return item

    class NotifyingGate:
        coordinator = coordinator_ref
        cancellation = coordinator_ref.cancellation

        def worker_capacity(self, **kwargs):
            return base.worker_capacity(**kwargs)

        @contextmanager
        def admit(self, *args, **kwargs):
            with base.admit(*args, **kwargs) as grant:
                admitted.set()
                yield grant

    def source():
        yield 1
        assert admitted.wait(1), "first worker was not admitted"
        # This is the same dependency as an Audio probe.  Before the fix the
        # owner called this while the first worker still awaited preparation.
        with base.native_budget(
            1, max_threads=1, io_slots=1, io_device="dev:7", phase="probe"
        ):
            pass
        yield 2

    started = time.monotonic()
    try:
        with elastic_map(
            execute_item,
            source(),
            gate=NotifyingGate(),
            max_workers=2,
            estimated_bytes=1,
            io_slots=1,
            io_device="dev:7",
            prepare=prepare_item,
            poll_interval=0.005,
        ) as results:
            assert list(results) == [1, 2]
    finally:
        coordinator.close()

    assert prepared == executed == [1, 2]
    assert time.monotonic() - started < 1


def test_ready_result_releases_memory_before_blocked_next_admission() -> None:
    """A contraction cannot deadlock a retained result and the next producer."""

    available = [2]
    coordinator = GlobalResourceCoordinator(
        ("route",),
        GlobalResourceLimits(
            memory_budget_bytes=2,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=2,
            native_thread_slots=2,
            wait_timeout_seconds=0.1,
            poll_interval_seconds=0.005,
        ),
        effective_cpu_probe=lambda: 2,
        resource_probe=lambda: ResourceSample(
            available_physical=available[0],
            available_commit=available[0],
            total_physical=2,
            total_commit=2,
            cpu_load_percent=0,
        ),
        cpu_load_probe=lambda: 0,
    )
    gate = CoordinatedMemoryGate(coordinator, "route")

    def source():
        yield 1
        # The first result remains retained in the bounded map window.  The
        # next request must not be awaited forever when live headroom contracts.
        available[0] = 1
        yield 2
        yield 3

    try:
        with elastic_map(
            lambda value: value,
            source(),
            gate=gate,
            estimated_bytes=1,
            max_workers=2,
            poll_interval=0.005,
        ) as results:
            assert list(results) == [1, 2, 3]
    finally:
        coordinator.close()


def test_default_admission_producer_waits_before_owner_memory_admission() -> None:
    """A producer's own memory request cannot strand a ready predecessor."""

    coordinator = _coordinator(memory=2, cpu=2, io=2)
    gate = CoordinatedMemoryGate(coordinator, "audio")
    producer_admission_entered = threading.Event()

    def source():
        yield 1
        # No I/O dimension is involved: this catches the generic owner-memory
        # cycle rather than only Audio's device slot alias.
        with gate.admit(2, cpu_slots=1, native_threads=1, phase="producer"):
            producer_admission_entered.set()
        yield 2

    try:
        with elastic_map(
            lambda value: value,
            source(),
            gate=gate,
            estimated_bytes=1,
            max_workers=2,
            poll_interval=0.005,
        ) as results:
            assert list(results) == [1, 2]
        assert producer_admission_entered.is_set()
    finally:
        coordinator.close()


def test_completion_order_opt_in_keeps_owner_publication_and_bounded_window() -> None:
    first_release = threading.Event()
    second_done = threading.Event()
    third_started = threading.Event()
    owner = threading.get_ident()
    published: list[tuple[int, int]] = []

    def work(item: int) -> int:
        if item == 0:
            assert first_release.wait(2)
        elif item == 1:
            second_done.set()
        elif item == 2:
            third_started.set()
        return item

    try:
        with elastic_map(
            work,
            range(4),
            capacity=lambda: 2,
            max_workers=2,
            completion_order=True,
            poll_interval=0.005,
        ) as results:
            first = next(results)
            assert first == 1
            assert second_done.is_set()
            # The third item is admitted while item 0 is still blocked; the
            # completed result did not remain a head-of-line worker lease.
            # A second owner pull advances the bounded producer after the
            # first completion has been consumed.
            second = next(results)
            assert third_started.wait(1)
            published.append((owner, 1))
            published.append((owner, second))
            first_release.set()
            for result in results:
                published.append((threading.get_ident(), result))
    finally:
        first_release.set()

    values = [value for _thread, value in published]
    assert values[0] == 1
    assert sorted(values) == [0, 1, 2, 3]
    assert {thread_id for thread_id, _value in published} == {owner}


def test_pure_producer_opt_in_retains_prefetch_with_a_real_coordinator() -> None:
    coordinator = _coordinator(memory=1024 * 1024, cpu=2, io=2)
    gate = CoordinatedMemoryGate(coordinator, "audio")
    first_release = threading.Event()
    second_done = threading.Event()

    def work(item: int) -> int:
        if item == 0:
            assert first_release.wait(2)
        else:
            second_done.set()
        return item

    fallback = threading.Timer(1, first_release.set)
    fallback.start()
    try:
        with elastic_map(
            work,
            range(3),
            gate=gate,
            estimated_bytes=1,
            max_workers=2,
            completion_order=True,
            producer_mode="pure",
            poll_interval=0.005,
        ) as results:
            first = next(results)
            assert first == 1
            first_release.set()
            values = [first, *results]
    finally:
        first_release.set()
        fallback.cancel()
        fallback.join(1)
        coordinator.close()

    assert sorted(values) == [0, 1, 2]


def test_io_device_aliases_share_limit_and_conflicting_config_is_rejected() -> None:
    assert canonical_io_device(2049) == "dev:801"
    assert canonical_io_device("2049") == "dev:801"
    assert canonical_io_device("dev:801") == "dev:801"
    assert canonical_io_device("8:0") == "8:0"
    assert normalize_io_device_slots({"2049": 1, "dev:801": 1}) == {"dev:801": 1}
    with pytest.raises(ValueError, match="conflicting aliases"):
        normalize_io_device_slots({"2049": 1, "dev:801": 2})

    coordinator = _coordinator(device_slots={"2049": 1}, cpu=2, io=2)
    first = CoordinatedMemoryGate(coordinator, "audio")
    second = CoordinatedMemoryGate(coordinator, "other")
    entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with first.admit(1, io_slots=1, io_device="2049"):
            entered.set()
            release.wait(2)

    def wait_second() -> None:
        assert entered.wait(1)
        with second.admit(1, io_slots=1, io_device="dev:801"):
            second_entered.set()

    left = threading.Thread(target=hold_first)
    right = threading.Thread(target=wait_second)
    try:
        left.start()
        assert entered.wait(1)
        right.start()
        time.sleep(0.05)
        assert not second_entered.is_set()
        release.set()
        left.join(1)
        right.join(1)
        assert second_entered.is_set()
    finally:
        release.set()
        left.join(1)
        right.join(1)
        coordinator.close()
