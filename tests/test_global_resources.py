from __future__ import annotations

import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from neocortex.runtime.control.cpu_runtime import CpuLoadSampler, CpuTimes
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
)
from neocortex.runtime.control.memory_runtime import (
    MemoryBudgetExceeded,
    MemoryHeadroomTimeout,
    MemoryResourceLimits,
    MemorySnapshot,
    WeightedMemoryGate,
)


class _InjectedBaseException(BaseException):
    pass


# region [01] Fair global admission


class CpuLoadSamplerTests(unittest.TestCase):
    def test_calculates_whole_system_load_from_counter_deltas(self) -> None:
        samples = [CpuTimes(600, 1_000), CpuTimes(650, 1_100)]
        with patch(
            "neocortex.runtime.control.cpu_runtime.cpu_times",
            side_effect=samples,
        ):
            sampler = CpuLoadSampler()
            self.assertEqual(sampler.sample(), 50.0)

    def test_preserves_last_load_when_counters_do_not_advance(self) -> None:
        samples = [CpuTimes(600, 1_000), CpuTimes(600, 1_000)]
        with patch(
            "neocortex.runtime.control.cpu_runtime.cpu_times",
            side_effect=samples,
        ):
            sampler = CpuLoadSampler()
            self.assertIsNone(sampler.sample())


class WeightedMemoryGateTests(unittest.TestCase):
    def test_spurious_wake_only_counts_one_wait(self) -> None:
        gate = WeightedMemoryGate(
            MemoryResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                wait_timeout_seconds=1,
            )
        )
        gate._reserved = 100
        wait_calls = 0

        def spurious_then_release(_timeout: float) -> None:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 2:
                gate._reserved = 0

        with patch.object(gate._condition, "wait", side_effect=spurious_then_release):
            with gate.admit(1):
                self.assertEqual(gate._reserved, 1)

        self.assertEqual(wait_calls, 2)
        self.assertEqual(gate.wait_count, 1)
        self.assertEqual(gate._reserved, 0)


class GlobalResourceCoordinatorTests(unittest.TestCase):
    def _coordinator(self, *, cpu_slots: int = 1):
        return GlobalResourceCoordinator(
            ("pdf", "image"),
            GlobalResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                cpu_slots=cpu_slots,
                wait_timeout_seconds=2,
                poll_interval_seconds=0.01,
            ),
            cpu_load_probe=lambda: 0.0,
        )

    def test_automatic_limits_use_available_machine_scale_without_fixed_preset(self):
        gib = 1024 * 1024 * 1024
        snapshot = MemorySnapshot(8 * gib, 12 * gib, 16 * gib, 24 * gib)
        with (
            patch(
                "neocortex.runtime.control.global_resources.memory_snapshot",
                return_value=snapshot,
            ),
            patch("neocortex.runtime.control.global_resources.effective_cpu_count", return_value=16),
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "audio"),
                GlobalResourceLimits(),
                cpu_load_probe=lambda: 0.0,
            )

        summary = coordinator.summary()
        # Automatic capacity is derived from the effective host rather than
        # being capped at the historical 5 GiB preset.
        self.assertEqual(summary.memory_budget_bytes, 16 * gib - gib // 2)
        self.assertEqual(summary.min_free_memory_bytes, gib // 2)
        self.assertEqual(summary.min_free_commit_bytes, gib // 2)
        self.assertEqual(summary.cpu_slots, 16)

    def test_default_cpu_sample_does_not_count_admitted_work_twice(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        current_load = [0.0]
        with (
            patch(
                "neocortex.runtime.control.global_resources.memory_snapshot",
                return_value=snapshot,
            ),
            patch(
                "neocortex.runtime.control.global_resources.CpuLoadSampler.sample",
                side_effect=lambda: current_load[0],
            ),
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf",),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                    cpu_slots=4,
                ),
            )
            # A broken admission policy would wait behind its own live jobs.
            # Fail immediately instead of relying on a wall-clock timeout.
            with patch.object(
                coordinator._condition, "wait", side_effect=AssertionError("self-throttled")
            ), ExitStack() as active:
                active.enter_context(coordinator.admit("pdf", 10))
                current_load[0] = 95.0
                for _ in range(3):
                    coordinator._last_cpu_sample_at = None
                    active.enter_context(coordinator.admit("pdf", 10))
                self.assertEqual(coordinator.route_active_request_count("pdf"), 4)
                self.assertEqual(coordinator.summary().min_effective_cpu_slots, 4)
                self.assertEqual(coordinator.summary().max_observed_cpu_load_percent, 95.0)
            self.assertEqual(coordinator.route_active_request_count("pdf"), 0)

    def test_default_cpu_sample_still_obeys_a_reduced_live_quota(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        effective_cpus = [5]
        with (
            patch(
                "neocortex.runtime.control.global_resources.memory_snapshot",
                return_value=snapshot,
            ),
            patch(
                "neocortex.runtime.control.global_resources.effective_cpu_count",
                side_effect=lambda: effective_cpus[0],
            ),
            patch(
                "neocortex.runtime.control.global_resources.CpuLoadSampler.sample",
                return_value=95.0,
            ),
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf",),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                ),
                resource_probe=lambda: {
                    "available_physical": snapshot.available_physical,
                    "total_physical": snapshot.total_physical,
                    "effective_cpu_capacity": effective_cpus[0],
                },
            )
            with coordinator.admit("pdf", 10):
                effective_cpus[0] = 1
                with patch.object(
                    coordinator._condition, "wait", side_effect=RuntimeError("quota wait")
                ):
                    with self.assertRaisesRegex(RuntimeError, "quota wait"):
                        with coordinator.admit("pdf", 10):
                            self.fail("the reduced cgroup quota was ignored")
                self.assertEqual(coordinator.route_active_request_count("pdf"), 1)
                self.assertEqual(coordinator.summary().min_effective_cpu_slots, 1)
            self.assertEqual(coordinator.route_active_request_count("pdf"), 0)

    def test_live_scale_budget_can_admit_four_bounded_pdf_workers(self):
        gib = 1024 * 1024 * 1024
        snapshot = MemorySnapshot(9 * gib, 10 * gib, 14 * gib, 15 * gib)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf",),
                GlobalResourceLimits(cpu_slots=4),
                cpu_load_probe=lambda: 0.0,
            )
            reservation = 5 * gib // 4
            with coordinator.admit("pdf", reservation):
                with coordinator.admit("pdf", reservation):
                    with coordinator.admit("pdf", reservation):
                        with coordinator.admit("pdf", reservation):
                            self.assertEqual(
                                coordinator.route_active_request_count("pdf"),
                                4,
                            )

        self.assertEqual(coordinator.summary().peak_active_requests, 4)

    def test_round_robin_prevents_one_route_from_monopolizing_slots(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator()
            first_entered = threading.Event()
            release_first = threading.Event()
            order: list[str] = []
            errors: list[BaseException] = []

            def run(route: str, first: bool = False) -> None:
                try:
                    with coordinator.admit(route, 60):
                        order.append(route)
                        if first:
                            first_entered.set()
                            release_first.wait(2)
                        else:
                            time.sleep(0.02)
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=run, args=("pdf", True))
            queued_pdf = threading.Thread(target=run, args=("pdf",))
            queued_image = threading.Thread(target=run, args=("image",))
            first.start()
            self.assertTrue(first_entered.wait(1))
            queued_pdf.start()
            queued_image.start()
            time.sleep(0.05)
            release_first.set()
            for thread in (first, queued_pdf, queued_image):
                thread.join(2)

            self.assertFalse(errors)
            self.assertEqual(order, ["pdf", "image", "pdf"])
            summary = coordinator.summary()
            self.assertEqual(summary.peak_cpu_slots, 1)
            self.assertEqual(summary.peak_reserved_bytes, 60)
            self.assertEqual(summary.routes["pdf"].admissions, 2)
            self.assertEqual(summary.routes["image"].admissions, 1)

    def test_internal_contention_does_not_become_a_headroom_timeout(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "image"),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                    cpu_slots=2,
                    wait_timeout_seconds=0.03,
                    poll_interval_seconds=0.005,
                ),
                cpu_load_probe=lambda: 0.0,
            )
            entered = threading.Event()
            errors: list[BaseException] = []

            def wait_for_image() -> None:
                try:
                    with coordinator.admit("image", 10):
                        entered.set()
                except BaseException as exc:
                    errors.append(exc)

            with coordinator.admit("pdf", 100):
                thread = threading.Thread(target=wait_for_image)
                thread.start()
                time.sleep(0.08)
                self.assertTrue(thread.is_alive())
                self.assertFalse(entered.is_set())

            thread.join(1)
            self.assertFalse(errors)
            self.assertTrue(entered.is_set())

    def test_inactive_fitting_route_precedes_route_with_active_work(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "docx", "image"),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                    cpu_slots=3,
                    wait_timeout_seconds=1,
                    poll_interval_seconds=0.005,
                ),
                cpu_load_probe=lambda: 0.0,
            )
            order: list[str] = []
            pdf_entered = threading.Event()
            image_entered = threading.Event()
            release_pdf = threading.Event()
            release_image = threading.Event()

            def active_pdf() -> None:
                with coordinator.admit("pdf", 60):
                    order.append("pdf-active")
                    pdf_entered.set()
                    release_pdf.wait(2)

            def active_image() -> None:
                with coordinator.admit("image", 40):
                    order.append("image-active")
                    image_entered.set()
                    release_image.wait(2)

            def queued(route_name: str) -> None:
                with coordinator.admit(route_name, 40):
                    order.append(route_name)

            threads = [
                threading.Thread(target=active_pdf),
                threading.Thread(target=active_image),
            ]
            threads[0].start()
            self.assertTrue(pdf_entered.wait(1))
            threads[1].start()
            self.assertTrue(image_entered.wait(1))
            queued_pdf = threading.Thread(target=queued, args=("pdf",))
            queued_docx = threading.Thread(target=queued, args=("docx",))
            threads.extend((queued_pdf, queued_docx))
            queued_pdf.start()
            queued_docx.start()
            time.sleep(0.03)
            release_image.set()
            queued_docx.join(1)
            self.assertEqual(order[:3], ["pdf-active", "image-active", "docx"])
            release_pdf.set()
            for thread in threads:
                thread.join(1)

    def test_rejects_one_item_larger_than_the_shared_budget(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator()
            with self.assertRaises(MemoryBudgetExceeded):
                with coordinator.admit("image", 101):
                    self.fail("oversized global reservation was admitted")

    def test_cpu_and_memory_can_expand_when_both_are_available(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator(cpu_slots=2)
            first_entered = threading.Event()
            second_entered = threading.Event()
            release = threading.Event()

            def run(route: str, entered: threading.Event) -> None:
                with coordinator.admit(route, 50):
                    entered.set()
                    release.wait(2)

            first = threading.Thread(target=run, args=("pdf", first_entered))
            second = threading.Thread(target=run, args=("image", second_entered))
            first.start()
            second.start()
            self.assertTrue(first_entered.wait(1))
            self.assertTrue(second_entered.wait(1))
            release.set()
            first.join(2)
            second.join(2)
            summary = coordinator.summary()
            self.assertEqual(summary.peak_cpu_slots, 2)
            self.assertEqual(summary.peak_reserved_bytes, 100)

    def test_live_headroom_is_rechecked_until_the_computer_recovers(self):
        current = [MemorySnapshot(10_000, 10_000, 20_000, 20_000)]
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            side_effect=lambda: current[0],
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "image"),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=100,
                    min_free_commit_bytes=100,
                    cpu_slots=2,
                    wait_timeout_seconds=2,
                    poll_interval_seconds=0.01,
                ),
                cpu_load_probe=lambda: 0.0,
            )
            current[0] = MemorySnapshot(120, 120, 20_000, 20_000)
            admitted = threading.Event()
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    with coordinator.admit("image", 50):
                        admitted.set()
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            time.sleep(0.05)
            self.assertFalse(admitted.is_set())
            current[0] = MemorySnapshot(1_000, 1_000, 20_000, 20_000)
            thread.join(2)

            self.assertFalse(errors)
            self.assertTrue(admitted.is_set())
            summary = coordinator.summary()
            self.assertEqual(summary.routes["image"].waits, 1)
            self.assertGreater(summary.routes["image"].wait_ns, 0)
            self.assertEqual(summary.min_observed_available_memory_bytes, 120)

    def test_live_headroom_timeout_is_explicit(self):
        initial = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        low = MemorySnapshot(10, 10, 20_000, 20_000)
        calls = [initial]

        def snapshot() -> MemorySnapshot:
            return calls.pop() if calls else low

        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            side_effect=snapshot,
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "image"),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=50,
                    min_free_commit_bytes=50,
                    cpu_slots=1,
                    wait_timeout_seconds=0.03,
                    poll_interval_seconds=0.005,
                ),
                cpu_load_probe=lambda: 0.0,
            )
            with self.assertRaises(MemoryHeadroomTimeout):
                with coordinator.admit("pdf", 25):
                    self.fail("request was admitted below live headroom floors")

    def test_live_cpu_load_reduces_capacity_until_the_computer_recovers(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        current_load = [0.0]
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = GlobalResourceCoordinator(
                ("pdf", "image"),
                GlobalResourceLimits(
                    memory_budget_bytes=100,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                    cpu_slots=2,
                    max_cpu_load_percent=90,
                    wait_timeout_seconds=2,
                    poll_interval_seconds=0.01,
                ),
                cpu_load_probe=lambda: current_load[0],
            )
            first_entered = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()

            def first() -> None:
                with coordinator.admit("pdf", 40):
                    first_entered.set()
                    release_first.wait(2)

            def second() -> None:
                with coordinator.admit("image", 40):
                    second_entered.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            self.assertTrue(first_entered.wait(1))
            current_load[0] = 95.0
            second_thread.start()
            time.sleep(0.05)
            self.assertFalse(second_entered.is_set())
            current_load[0] = 0.0
            self.assertTrue(second_entered.wait(1))
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)

            summary = coordinator.summary()
            self.assertEqual(summary.min_effective_cpu_slots, 1)
            self.assertEqual(summary.max_observed_cpu_load_percent, 95.0)
            self.assertEqual(summary.routes["image"].waits, 1)
            self.assertGreater(summary.routes["image"].wait_ns, 0)

    def test_baseexception_from_probe_removes_queued_request(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator()
            failure = _InjectedBaseException("probe interrupted")
            with patch.object(
                coordinator, "_observe_live_resources", side_effect=failure
            ):
                with self.assertRaises(_InjectedBaseException) as raised:
                    with coordinator.admit("pdf", 10):
                        self.fail("interrupted admission unexpectedly entered")

            self.assertIs(raised.exception, failure)
            self.assertEqual(sum(map(len, coordinator._queues.values())), 0)
            self.assertEqual(coordinator.route_active_request_count("pdf"), 0)
            self.assertEqual(coordinator.summary().peak_reserved_bytes, 0)

    def test_baseexception_from_wait_removes_queued_request(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator()
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def owner() -> None:
                with coordinator.admit("pdf", 100):
                    entered.set()
                    release.wait(2)

            def waiter() -> None:
                try:
                    with coordinator.admit("image", 10):
                        self.fail("interrupted waiter unexpectedly entered")
                except BaseException as exc:
                    errors.append(exc)

            owner_thread = threading.Thread(target=owner)
            owner_thread.start()
            self.assertTrue(entered.wait(1))
            failure = _InjectedBaseException("condition wait interrupted")
            with patch.object(coordinator._condition, "wait", side_effect=failure):
                waiter_thread = threading.Thread(target=waiter)
                waiter_thread.start()
                waiter_thread.join(1)
            release.set()
            owner_thread.join(2)

            self.assertEqual(errors, [failure])
            self.assertEqual(sum(map(len, coordinator._queues.values())), 0)
            self.assertEqual(coordinator.route_active_request_count("image"), 0)
            self.assertEqual(coordinator.summary().peak_reserved_bytes, 100)

    def test_baseexception_in_body_releases_reservation_once(self):
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            coordinator = self._coordinator()
            failure = _InjectedBaseException("worker interrupted")
            with self.assertRaises(_InjectedBaseException) as raised:
                with coordinator.admit("pdf", 40):
                    raise failure

            self.assertIs(raised.exception, failure)
            self.assertEqual(coordinator.route_active_request_count("pdf"), 0)
            self.assertEqual(coordinator.summary().peak_reserved_bytes, 40)
            # A subsequent admission proves that the previous cleanup did not
            # leave a reservation or release the same reservation twice.
            with coordinator.admit("pdf", 40):
                self.assertEqual(coordinator.route_active_request_count("pdf"), 1)
            self.assertEqual(coordinator.route_active_request_count("pdf"), 0)


# endregion [01]


if __name__ == "__main__":
    unittest.main()
