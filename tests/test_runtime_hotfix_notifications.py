"""Proposed regression cases for RT-01/RT-03; run only in the local test owner.

These cases use bounded synchronization to distinguish progress notifications
from periodic polling. Their waits are fixture safety bounds, not benchmarks.
"""
from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from typing import Any, cast

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.elastic_workers import (
    ImmediateResult,
    current_worker_cancellation,
    elastic_map,
)
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from neocortex.runtime.control.resource_sampler import OwnedResourceSnapshot


class _OwnerWait(threading.Event):
    def __init__(self, allow_admission: threading.Event) -> None:
        super().__init__()
        self.allow_admission = allow_admission
        self.outcomes: list[bool] = []

    def wait(self, timeout: float | None = None) -> bool:
        # Force admission to occur after the owner has checked pending state.
        self.allow_admission.set()
        result = super().wait(2.0)
        self.outcomes.append(result)
        if not result:
            raise AssertionError("owner progress required a polling timeout")
        return result


class _AdmissionAfterWait:
    def __init__(self, permit: threading.Event, *, fail: bool = False) -> None:
        self.permit = permit
        self.fail = fail

    @contextmanager
    def admit(self, *_args, **_kwargs):
        if not self.permit.wait(2.0):
            raise AssertionError("the owner never reached its progress wait")
        if self.fail:
            raise RuntimeError("fixture admission failed")
        yield None


class ElasticOwnerNotificationsTests(unittest.TestCase):
    def test_idle_capacity_wait_observes_parent_cancellation_without_event_timeout(self) -> None:
        parent = CancellationToken()
        event_timeouts: list[bool] = []

        class CancelAtEventWait(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                # Bound a regression's otherwise sixty-second wait. The
                # parent token is not a producer of owner progress events.
                parent.cancel()
                result = super().wait(0.05)
                event_timeouts.append(not result)
                return result

        with elastic_map(
            lambda item: item, [0], capacity=lambda: 0,
            cancellation=parent, estimated_bytes=0, poll_interval=60.0,
        ) as results:
            original_token_wait = results._stop.wait

            def cancel_at_token_wait(timeout: float | None = None) -> bool:
                parent.cancel()
                return original_token_wait(timeout)

            cast(Any, results._stop).wait = cancel_at_token_wait
            results._owner_wakeup = CancelAtEventWait()
            with self.assertRaises(CancellationRequested):
                next(results)
        self.assertEqual(event_timeouts, [])

    def test_admission_wakes_owner_before_result_can_exist(self) -> None:
        permit = threading.Event()
        owner = threading.get_ident()
        prepared_by: list[int] = []
        called: list[int] = []

        def prepare(item: int):
            prepared_by.append(threading.get_ident())
            return ImmediateResult(item) if item % 2 == 0 else item

        def worker(item: int) -> int:
            called.append(item)
            return item

        with elastic_map(
            worker, range(4), capacity=lambda: 1,
            gate=_AdmissionAfterWait(permit), prepare=prepare,
            estimated_bytes=0, poll_interval=60.0,
        ) as results:
            wakeup = _OwnerWait(permit)
            results._owner_wakeup = wakeup
            self.assertEqual(list(results), [0, 1, 2, 3])
        self.assertEqual(prepared_by, [owner] * 4)
        self.assertEqual(called, [1, 3])
        self.assertTrue(wakeup.outcomes)
        self.assertTrue(all(wakeup.outcomes))

    def test_failure_before_admission_wakes_the_owner(self) -> None:
        permit = threading.Event()
        with self.assertRaisesRegex(RuntimeError, "fixture admission failed"):
            with elastic_map(
                lambda item: item, [0], capacity=lambda: 1,
                gate=_AdmissionAfterWait(permit, fail=True),
                estimated_bytes=0, poll_interval=60.0,
            ) as results:
                wakeup = _OwnerWait(permit)
                results._owner_wakeup = wakeup
                next(results)
        self.assertEqual(wakeup.outcomes, [True])

    def test_later_failure_wakes_owner_behind_a_cooperative_first_item(self) -> None:
        first_started = threading.Event()

        def worker(item: int) -> int:
            token = current_worker_cancellation()
            if token is None:
                raise AssertionError("worker has no cancellation token")
            if item == 0:
                first_started.set()
                if not token.wait(2.0):
                    raise AssertionError("later sibling failure was hidden")
                token.checkpoint()
            if not first_started.wait(2.0):
                raise AssertionError("first worker never started")
            raise RuntimeError("later fixture sibling failed")

        with self.assertRaisesRegex(RuntimeError, "later fixture sibling failed"):
            with elastic_map(
                worker, [0, 1], capacity=lambda: 2,
                estimated_bytes=0, poll_interval=60.0,
            ) as results:
                next(results)


_GIB = 1024 ** 3


class _OwnedSampler:
    def __init__(self, gpu_entered: threading.Event) -> None:
        self.gpu_entered = gpu_entered
        self.advanced_while_gpu_waited = threading.Event()
        self.cached: OwnedResourceSnapshot | None = None
        self.sample()

    def sample(self) -> OwnedResourceSnapshot:
        self.cached = OwnedResourceSnapshot(
            sampled_at=time.monotonic(), effective_cpu_capacity=4.0,
            memory_snapshot=MemorySnapshot(8 * _GIB, 8 * _GIB, 16 * _GIB, 16 * _GIB),
            host_cpu_percent=0.0, external_cpu_cores=0.0, own_cpu_cores=0.0,
            cpu_observation_complete=True,
        )
        if self.gpu_entered.is_set():
            self.advanced_while_gpu_waited.set()
        return self.cached

    def current_sample(self) -> OwnedResourceSnapshot | None:
        return self.cached


def _coordinator() -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("fixture",),
        GlobalResourceLimits(
            memory_budget_bytes=_GIB, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=4, temp_budget_bytes=_GIB,
            sample_interval_seconds=0.01, poll_interval_seconds=0.01,
            wait_timeout_seconds=0.5,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=8 * _GIB, available_commit=8 * _GIB,
            total_physical=16 * _GIB, total_commit=16 * _GIB,
            effective_cpu_capacity=4.0, external_cpu_cores=0.0,
            cpu_load_percent=0.0,
        ),
        cpu_load_probe=lambda: 0.0, effective_cpu_probe=lambda: 4,
    )


class IndependentGpuNotificationsTests(unittest.TestCase):
    def test_first_registration_in_active_scope_returns_with_a_sample(self) -> None:
        coordinator = _coordinator()
        try:
            coordinator.start()
            coordinator.register_gpu_device("gpu0", 100, available_probe=lambda: 100)
            self.assertEqual(coordinator.gpu_worker_capacity("fixture", "gpu0", 1), 100)
            self.assertEqual(coordinator.worker_capacity("fixture", estimated_bytes=1), 4)
        finally:
            coordinator.close()

    def test_reregistration_does_not_insert_unknown_between_healthy_samples(self) -> None:
        coordinator = _coordinator()
        entered = threading.Event()
        release = threading.Event()
        registration_finished = threading.Event()
        errors: list[BaseException] = []

        def replacement() -> int | None:
            entered.set()
            return 100 if release.wait(5.0) else None

        def register_again() -> None:
            try:
                coordinator.register_gpu_device("gpu0", 100, available_probe=replacement)
            except BaseException as exc:
                errors.append(exc)
            finally:
                registration_finished.set()

        coordinator.register_gpu_device("gpu0", 100, available_probe=lambda: 100)
        registration = threading.Thread(target=register_again, daemon=True)
        try:
            coordinator.start()
            registration.start()
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(registration_finished.is_set())
            self.assertEqual(coordinator.gpu_worker_capacity("fixture", "gpu0", 1), 100)
            release.set()
            self.assertTrue(registration_finished.wait(2.0))
            self.assertEqual(errors, [])
            self.assertEqual(coordinator.gpu_worker_capacity("fixture", "gpu0", 1), 100)
        finally:
            release.set()
            if registration.ident is not None:
                registration.join(2.0)
            coordinator.close()

    def test_replaced_probe_cannot_publish_its_inflight_old_generation(self) -> None:
        coordinator = _coordinator()
        old_entered = threading.Event()
        new_entered = threading.Event()
        release_old = threading.Event()
        release_new = threading.Event()
        registration_reached_sampling = threading.Event()
        registration_finished = threading.Event()
        errors: list[BaseException] = []
        calls = 0

        def original_probe() -> int | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 100
            old_entered.set()
            return 900 if release_old.wait(5.0) else None

        def replacement() -> int | None:
            new_entered.set()
            return 300 if release_new.wait(5.0) else None

        coordinator.register_gpu_device("gpu0", 1000, available_probe=original_probe)
        original_sample = coordinator._sample_gpu_resources

        def sample_after_registration(stop=None) -> None:
            # Registration has published its new probe generation before this
            # boundary, but has not acquired the single-producer sampling lock.
            if threading.current_thread().name == "fixture-reregister":
                registration_reached_sampling.set()
            original_sample(stop)

        def register_again() -> None:
            try:
                coordinator.register_gpu_device("gpu0", 1000, available_probe=replacement)
            except BaseException as exc:
                errors.append(exc)
            finally:
                registration_finished.set()

        registration = threading.Thread(
            target=register_again, name="fixture-reregister", daemon=True,
        )
        try:
            coordinator.start()
            self.assertTrue(old_entered.wait(2.0))
            cast(Any, coordinator)._sample_gpu_resources = sample_after_registration
            registration.start()
            self.assertTrue(registration_reached_sampling.wait(2.0))
            release_old.set()
            self.assertTrue(new_entered.wait(2.0))
            # 900 is a late answer from the replaced probe. Until the new
            # probe completes, the still-fresh prior publication stays 100.
            self.assertEqual(coordinator._gpu_available_cache["gpu0"][0], 100)
            release_new.set()
            self.assertTrue(registration_finished.wait(2.0))
            self.assertEqual(errors, [])
            self.assertEqual(coordinator.gpu_worker_capacity("fixture", "gpu0", 1), 300)
        finally:
            release_old.set()
            release_new.set()
            if registration.ident is not None:
                registration.join(2.0)
            coordinator.close()

    def test_cpu_sampler_advances_while_the_gpu_probe_is_blocked(self) -> None:
        coordinator = _coordinator()
        gpu_entered = threading.Event()
        release_gpu = threading.Event()
        sampler = _OwnedSampler(gpu_entered)
        coordinator._resource_probe = None
        coordinator._default_sampler = sampler
        calls = 0

        def probe() -> int | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 100
            gpu_entered.set()
            return 100 if release_gpu.wait(5.0) else None

        coordinator.register_gpu_device("gpu0", 100, available_probe=probe)
        try:
            coordinator.start()
            self.assertTrue(gpu_entered.wait(2.0))
            self.assertTrue(sampler.advanced_while_gpu_waited.wait(2.0))
            self.assertFalse(release_gpu.is_set())
            self.assertEqual(coordinator.worker_capacity("fixture", estimated_bytes=1), 4)
            with coordinator.admit("fixture", 1):
                pass
            # A second background attempt must not start an overlapping query.
            coordinator._sample_gpu_resources(threading.Event())
            self.assertEqual(calls, 2)
        finally:
            release_gpu.set()
            coordinator.close()

    def test_unknown_gpu_does_not_close_cpu_capacity(self) -> None:
        coordinator = _coordinator()
        coordinator.register_gpu_device("gpu0", 100, available_probe=lambda: None)
        try:
            self.assertEqual(coordinator.gpu_worker_capacity("fixture", "gpu0", 1), 0)
            self.assertEqual(coordinator.worker_capacity("fixture", estimated_bytes=1), 4)
            with coordinator.admit("fixture", 1):
                pass
        finally:
            coordinator.close()

    def test_cpu_admission_does_not_refresh_gpu_when_unmonitored(self) -> None:
        coordinator = _coordinator()
        calls = 0

        def probe() -> int:
            nonlocal calls
            calls += 1
            return 100

        coordinator.register_gpu_device("gpu0", 100, available_probe=probe)
        self.assertEqual(calls, 1)
        try:
            with coordinator.admit("fixture", 1):
                pass
            self.assertEqual(calls, 1)
        finally:
            coordinator.close()

    def test_late_gpu_result_after_close_cannot_replace_the_cached_observation(self) -> None:
        coordinator = _coordinator()
        gpu_entered = threading.Event()
        release_gpu = threading.Event()
        calls = 0

        def probe() -> int | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 100
            gpu_entered.set()
            return 200 if release_gpu.wait(5.0) else None

        coordinator.register_gpu_device("gpu0", 1000, available_probe=probe)
        try:
            coordinator.start()
            self.assertTrue(gpu_entered.wait(2.0))
            observer = coordinator._gpu_monitor_thread
            if observer is None:
                self.fail("GPU monitor was not started")
            coordinator.close()
            release_gpu.set()
            observer.join(2.0)
            self.assertFalse(observer.is_alive())
            self.assertEqual(coordinator._gpu_available_cache["gpu0"][0], 100)
        finally:
            release_gpu.set()
            coordinator.close()

    def test_stale_gpu_observation_is_unavailable_without_closing_cpu_capacity(self) -> None:
        coordinator = _coordinator()
        coordinator.register_gpu_device("gpu0", 100, available_probe=lambda: 100)
        try:
            with coordinator._condition:
                coordinator._gpu_available_cache["gpu0"] = (100, time.monotonic() - 10.0)
                self.assertIsNone(coordinator._gpu_available_locked("gpu0"))
            self.assertEqual(coordinator.worker_capacity("fixture", estimated_bytes=1), 4)
        finally:
            coordinator.close()
