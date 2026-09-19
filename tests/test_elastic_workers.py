from __future__ import annotations

import os
import sqlite3
import threading
import time
import unittest
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.elastic_workers import (
    ImmediateResult,
    current_worker_cancellation,
    elastic_map,
)


_CURRENT_GRANT: ContextVar[object | None] = ContextVar("test_elastic_grant", default=None)


@contextmanager
def _grant_scope(grant):
    token = _CURRENT_GRANT.set(grant)
    try:
        yield grant
    finally:
        _CURRENT_GRANT.reset(token)


class _Grant:
    def __init__(self, gate, reservation, cpu_slots):
        self.gate = gate
        self.reservation = reservation
        self.cpu_slots = cpu_slots
        self.released_cpu = False

    @property
    def native_env(self):
        return {"OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"}

    def release_cpu(self):
        with self.gate.condition:
            if not self.released_cpu:
                self.released_cpu = True
                self.gate.cpu_active -= self.cpu_slots
                self.gate.condition.notify_all()


class _Gate:
    def __init__(self, capacity=2):
        self.capacity = capacity
        self.cpu_active = 0
        self.memory_active = 0
        self.leases = 0
        self.peak_cpu = 0
        self.peak_memory = 0
        self.requests = []
        self.condition = threading.Condition()

    def worker_capacity(self, **_kwargs):
        return self.capacity

    @contextmanager
    def admit(self, estimate, *, cancellation, **kwargs):
        cpu_slots = kwargs.get("cpu_slots", 1)
        total_memory = estimate + kwargs.get("resident_bytes", 0)
        with self.condition:
            self.requests.append((estimate, kwargs))
            while cpu_slots and self.cpu_active + cpu_slots > self.capacity:
                cancellation.checkpoint()
                self.condition.wait(0.01)
            cancellation.checkpoint()
            self.cpu_active += cpu_slots
            self.memory_active += total_memory
            self.leases += 1
            self.peak_cpu = max(self.peak_cpu, self.cpu_active)
            self.peak_memory = max(self.peak_memory, self.memory_active)
        grant = _Grant(self, total_memory, cpu_slots)
        owner = threading.get_ident()
        try:
            yield grant
        finally:
            if threading.get_ident() != owner:
                raise AssertionError("admission context exited in another thread")
            grant.release_cpu()
            with self.condition:
                self.memory_active -= total_memory
                self.leases -= 1
                self.condition.notify_all()


def _process_identity(value):
    time.sleep(0.02)
    return value, os.getpid()


def _process_timeout(_value):
    raise TimeoutError("payload timeout")


def _process_environment(_value):
    return os.environ.get("OMP_THREAD_LIMIT"), os.environ.get("OMP_NUM_THREADS")


def _process_cooperative_wait(marker):
    token = current_worker_cancellation()
    if token is None:
        raise AssertionError("process worker has no cancellation token")
    Path(marker).write_text(str(os.getpid()), encoding="ascii")
    if not token.wait(5):
        raise AssertionError("process did not observe parent cancellation")
    token.checkpoint()


class ElasticWorkersTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "neocortex.runtime.control.elastic_workers._grant_scope", _grant_scope
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_released(self, gate):
        self.assertEqual(gate.cpu_active, 0)
        self.assertEqual(gate.memory_active, 0)
        self.assertEqual(gate.leases, 0)

    def test_later_fatal_sibling_stops_cooperative_first_worker_promptly(self):
        gate = _Gate(2)
        first_started = threading.Event()

        def worker(item):
            token = current_worker_cancellation()
            self.assertIsNotNone(token)
            if item == 0:
                first_started.set()
                self.assertTrue(token.wait(5))
                token.checkpoint()
            self.assertTrue(first_started.wait(1))
            raise RuntimeError("later sibling failed")

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "later sibling failed"):
            with elastic_map(worker, [0, 1], gate=gate, estimated_bytes=10,
                             poll_interval=0.01) as results:
                next(results)
        self.assertLess(time.monotonic() - started, 2)
        self.assert_released(gate)

    def test_process_worker_observes_parent_cancel_without_kill_or_leaked_lease(self):
        gate = _Gate(1)
        cancellation = CancellationToken()
        with TemporaryDirectory(prefix="neocortex-elastic-cancel-") as directory:
            marker = Path(directory) / "started"

            def cancel_when_started():
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                cancellation.cancel()

            cancelling = threading.Thread(target=cancel_when_started)
            cancelling.start()
            started = time.monotonic()
            with self.assertRaises(CancellationRequested):
                with elastic_map(_process_cooperative_wait, [str(marker)], gate=gate,
                                 cancellation=cancellation, executor_kind="process",
                                 estimated_bytes=10, poll_interval=0.01) as results:
                    list(results)
            cancelling.join(1)
            self.assertFalse(cancelling.is_alive())
            self.assertTrue(marker.exists())
            self.assertLess(time.monotonic() - started, 3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(marker.read_text(encoding="ascii")), 0)
        self.assert_released(gate)

    def test_waiting_admission_does_not_pin_an_idle_process_cohort_under_pressure(self):
        waiting = threading.Event()
        resume = threading.Event()
        retired = threading.Event()

        class WaitingGate(_Gate):
            @contextmanager
            def admit(self, estimate, *, cancellation, **kwargs):
                # Supervisors can reach admission in either order after the
                # shared process cohort becomes ready. Block payload 2, not
                # whichever supervisor happens to arrive second.
                if estimate == 12 and kwargs.get("phase") != "elastic-process-resident":
                    waiting.set()
                    while not resume.wait(0.01):
                        cancellation.checkpoint()
                with super().admit(estimate, cancellation=cancellation, **kwargs) as grant:
                    yield grant

        gate = WaitingGate(2)

        def observe_retirement():
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with gate.condition:
                    if gate.memory_active == 0:
                        retired.set()
                        break
                time.sleep(0.005)
            gate.capacity = 2
            resume.set()

        with elastic_map(_process_identity, [1, 2], gate=gate,
                         estimated_bytes=lambda item: 10 + item,
                         executor_kind="process", process_resident_bytes=64,
                         poll_interval=0.01) as results:
            first = next(results)
            self.assertTrue(waiting.wait(1))
            gate.capacity = 0
            observer = threading.Thread(target=observe_retirement)
            observer.start()
            second = next(results)
            observer.join(1)
            self.assertFalse(observer.is_alive())
            self.assertTrue(retired.is_set())
            self.assertEqual([first[0], second[0]], [1, 2])
        self.assert_released(gate)

    def test_ordered_bounded_input_and_result_memory_through_owner_consumption(self):
        gate = _Gate(3)
        owner = threading.get_ident()
        pulled = []
        worker_threads = []
        first_can_finish = threading.Event()

        def items():
            for item in range(9):
                self.assertEqual(threading.get_ident(), owner)
                pulled.append(item)
                yield item

        def worker(item):
            worker_threads.append(threading.get_ident())
            self.assertIsNotNone(_CURRENT_GRANT.get())
            if item == 0:
                self.assertTrue(first_can_finish.wait(2))
            if item == 2:
                first_can_finish.set()
            return item * 10

        with elastic_map(worker, items(), gate=gate, estimated_bytes=10) as results:
            self.assertEqual(next(results), 0)
            self.assertEqual(pulled, [0, 1, 2])
            self.assertEqual(gate.memory_active, 30)
            with gate.condition:
                self.assertTrue(gate.condition.wait_for(lambda: gate.cpu_active == 0, timeout=2))
            self.assertIsNotNone(_CURRENT_GRANT.get())
            values = [0]
            for result in results:
                self.assertEqual(threading.get_ident(), owner)
                self.assertGreater(gate.memory_active, 0)
                values.append(result)
            self.assertEqual(values, list(range(0, 90, 10)))
        self.assertTrue(worker_threads)
        self.assertNotIn(owner, worker_threads)
        self.assertIsNone(_CURRENT_GRANT.get())
        self.assertLessEqual(gate.peak_memory, 30)
        self.assert_released(gate)

    def test_prepare_and_sqlite_writer_stay_in_owner_with_admitted_memory(self):
        gate = _Gate(3)
        owner = threading.get_ident()
        calls = []
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("create table results (value integer)")

        def prepare(item):
            self.assertEqual(threading.get_ident(), owner)
            self.assertGreaterEqual(gate.memory_active, 10)
            self.assertIsNotNone(_CURRENT_GRANT.get())
            connection.execute("insert into results values (?)", (item,))
            return ImmediateResult(12) if item == 1 else item * 2

        def compute(value):
            calls.append(value)
            return value + 10

        with elastic_map(compute, range(4), prepare=prepare, gate=gate,
                         estimated_bytes=10) as results:
            self.assertEqual(list(results), [10, 12, 14, 16])
        self.assertEqual(sorted(calls), [0, 4, 6])
        self.assertEqual(connection.execute("select count(*) from results").fetchone(), (4,))
        self.assert_released(gate)

    def test_live_capacity_shrinks_to_zero_and_recovers_above_startup_ceiling(self):
        gate = _Gate(2)
        started = []
        consumed = []
        failures = []
        condition = threading.Condition()
        release = [threading.Event() for _ in range(8)]
        paused_consumer = threading.Event()
        continue_consumer = threading.Event()

        def worker(item):
            with condition:
                started.append(item)
                condition.notify_all()
            if not release[item].wait(4):
                raise AssertionError("test did not release bounded work")
            return item

        def owner():
            try:
                with elastic_map(worker, range(8), gate=gate, estimated_bytes=1,
                                 poll_interval=0.01) as results:
                    for result in results:
                        consumed.append(result)
                        if result == 1:
                            paused_consumer.set()
                            if not continue_consumer.wait(4):
                                raise AssertionError("test did not resume consumer")
            except BaseException as exc:
                failures.append(exc)

        runner = threading.Thread(target=owner)
        runner.start()
        try:
            with condition:
                self.assertTrue(condition.wait_for(lambda: len(started) == 2, timeout=2))
            gate.capacity = 0
            release[0].set()
            release[1].set()
            self.assertTrue(paused_consumer.wait(2))
            self.assertEqual(sorted(started), [0, 1])
            gate.capacity = 5
            continue_consumer.set()
            with condition:
                self.assertTrue(condition.wait_for(lambda: len(started) == 7, timeout=2))
            self.assertEqual(gate.cpu_active, 5)
        finally:
            gate.capacity = 5
            continue_consumer.set()
            for event in release:
                event.set()
            runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(consumed, list(range(8)))
        self.assert_released(gate)

    def test_capacity_growth_is_observed_while_first_result_is_still_running(self):
        target = [1]
        started = []
        lock = threading.Condition()
        release = threading.Event()
        failures = []

        def worker(item):
            with lock:
                started.append(item)
                lock.notify_all()
            if not release.wait(3):
                raise AssertionError("growth failed to fill workers")
            return item

        def owner():
            try:
                with elastic_map(worker, range(4), capacity=lambda: target[0],
                                 poll_interval=0.01) as results:
                    self.assertEqual(list(results), [0, 1, 2, 3])
            except BaseException as exc:
                failures.append(exc)

        runner = threading.Thread(target=owner)
        runner.start()
        try:
            with lock:
                self.assertTrue(lock.wait_for(lambda: len(started) == 1, timeout=2))
            target[0] = 4
            with lock:
                self.assertTrue(lock.wait_for(lambda: len(started) == 4, timeout=2))
        finally:
            release.set()
            runner.join(5)
        self.assertFalse(runner.is_alive())
        self.assertEqual(failures, [])

    def test_automatic_worker_count_has_no_eight_worker_ceiling(self):
        barrier = threading.Barrier(12)

        def worker(item):
            barrier.wait(timeout=3)
            return item

        with patch("neocortex.runtime.control.elastic_workers.effective_cpu_count", return_value=12):
            with elastic_map(worker, range(12), poll_interval=0.01) as results:
                self.assertEqual(list(results), list(range(12)))

    def test_early_close_releases_completed_results_and_does_not_cancel_parent(self):
        gate = _Gate(4)
        cancellation = CancellationToken()
        with elastic_map(lambda item: item, range(100), gate=gate,
                         estimated_bytes=10, cancellation=cancellation) as results:
            self.assertEqual(next(results), 0)
            self.assertGreater(gate.memory_active, 0)
        self.assertFalse(cancellation.is_cancelled)
        self.assert_released(gate)

    def test_early_close_releases_input_cursor_in_owner(self):
        owner = threading.get_ident()
        closed = []

        def items():
            try:
                yield from range(100)
            finally:
                closed.append(threading.get_ident())

        with elastic_map(lambda item: item, items(), max_workers=1) as results:
            self.assertEqual(next(results), 0)
        self.assertEqual(closed, [owner])

    def test_gate_cancellation_is_inherited_when_no_caller_token_is_supplied(self):
        gate = _Gate(0)
        gate.cancellation = CancellationToken()
        gate.cancellation.cancel()
        with self.assertRaises(CancellationRequested):
            with elastic_map(lambda item: item, [1], gate=gate) as results:
                list(results)
        self.assert_released(gate)

    def test_cancellation_wakes_admission_wait_without_starting_workers(self):
        gate = _Gate(0)
        cancellation = CancellationToken()
        timer = threading.Timer(0.05, cancellation.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        calls = []
        with self.assertRaises(CancellationRequested):
            with elastic_map(calls.append, range(20), gate=gate,
                             estimated_bytes=10, cancellation=cancellation) as results:
                list(results)
        self.assertEqual(calls, [])
        self.assertEqual(len(gate.requests), 1)
        self.assert_released(gate)

    def test_prepare_worker_and_source_failures_release_every_lease(self):
        for location in ("prepare", "worker", "source"):
            with self.subTest(location=location):
                gate = _Gate(3)

                def items(location=location):
                    yield 0
                    if location == "source":
                        raise ValueError("source failure")
                    yield 1

                def prepare(item, location=location):
                    if location == "prepare":
                        raise ValueError("prepare failure")
                    return item

                def worker(item, location=location):
                    if location == "worker":
                        raise ValueError("worker failure")
                    return item

                with self.assertRaisesRegex(ValueError, location + " failure"):
                    with elastic_map(worker, items(), prepare=prepare,
                                     gate=gate, estimated_bytes=10) as results:
                        list(results)
                self.assert_released(gate)

    def test_process_payload_isolated_and_owner_preparation_not_pickled(self):
        owner = os.getpid()
        gate = _Gate(2)
        with elastic_map(_process_identity, range(6), gate=gate, estimated_bytes=10,
                         prepare=lambda item: item + 1,
                         executor_kind="process") as results:
            outputs = list(results)
        self.assertEqual([item for item, _pid in outputs], list(range(1, 7)))
        pids = {pid for _item, pid in outputs}
        self.assertNotIn(owner, pids)
        self.assertEqual(len(pids), 2)
        self.assert_released(gate)

    def test_process_payload_timeout_is_propagated_not_retried_as_poll_timeout(self):
        gate = _Gate(1)
        with self.assertRaisesRegex(TimeoutError, "payload timeout"):
            with elastic_map(_process_timeout, [1], gate=gate, estimated_bytes=10,
                             executor_kind="process", poll_interval=0.01) as results:
                list(results)
        self.assert_released(gate)

    def test_process_limits_apply_in_child_without_mutating_parent_environment(self):
        gate = _Gate(1)
        with patch.dict(os.environ, {"OMP_THREAD_LIMIT": "23", "OMP_NUM_THREADS": "23"}):
            with elastic_map(_process_environment, [1], gate=gate, estimated_bytes=10,
                             executor_kind="process") as results:
                self.assertEqual(list(results), [("1", "1")])
            self.assertEqual(os.environ["OMP_THREAD_LIMIT"], "23")
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "23")
        self.assert_released(gate)

    def test_process_interpreter_residence_survives_consumption_and_is_reused(self):
        gate = _Gate(1)
        with elastic_map(_process_identity, [1, 2], gate=gate, estimated_bytes=10,
                         process_resident_bytes=64, executor_kind="process") as results:
            first = next(results)
            self.assertEqual(gate.memory_active, 74)
            second = next(results)
            self.assertEqual(first[1], second[1])
            self.assertEqual(gate.memory_active, 74)
            residences = [kwargs for _estimate, kwargs in gate.requests
                          if kwargs.get("phase") == "elastic-process-resident"]
            self.assertEqual(len(residences), 1)
            self.assertEqual(residences[0]["resident_bytes"], 64)
            self.assertEqual(residences[0]["cpu_slots"], 0)
            self.assertEqual(residences[0]["native_threads"], 0)
        self.assert_released(gate)
        with self.assertRaises(ProcessLookupError):
            os.kill(first[1], 0)

    def test_mixed_payloads_use_process_predicate_after_owner_preparation(self):
        gate = _Gate(2)
        with elastic_map(_process_identity, [1, 2], gate=gate, estimated_bytes=10,
                         process_resident_bytes=64, executor_kind="process",
                         prepare=lambda item: item * 10,
                         process_predicate=lambda item: item == 20) as results:
            outputs = list(results)
        self.assertEqual([item for item, _pid in outputs], [10, 20])
        self.assertEqual(outputs[0][1], os.getpid())
        self.assertNotEqual(outputs[1][1], os.getpid())
        self.assert_released(gate)

    def test_idle_process_cohort_retires_on_contraction_then_can_grow_again(self):
        gate = _Gate(3)
        with elastic_map(_process_identity, range(8), gate=gate, estimated_bytes=10,
                         process_resident_bytes=64, executor_kind="process") as results:
            outputs = [next(results)]
            self.assertGreaterEqual(gate.memory_active, 3 * 64)
            gate.capacity = 1
            outputs.extend([next(results), next(results)])
            outputs.append(next(results))
            self.assertEqual(gate.memory_active, 64 + 10)
            gate.capacity = 4
            outputs.append(next(results))
            self.assertGreaterEqual(gate.memory_active, 4 * 64)
            outputs.extend(results)
        self.assertEqual([value for value, _pid in outputs], list(range(8)))
        self.assert_released(gate)

    def test_resource_dimensions_are_forwarded_without_global_environment_changes(self):
        gate = _Gate(1)
        with elastic_map(lambda item: item, [1], gate=gate, estimated_bytes=16,
                         native_threads=2, io_slots=1, io_device=lambda _item: "8:0",
                         phase="extract") as results:
            self.assertEqual(list(results), [1])
        self.assertEqual(gate.requests, [(16, {
            "native_threads": 2, "io_slots": 1, "io_device": "8:0", "phase": "extract",
        })])
        self.assert_released(gate)


class ElasticGlobalResourceIntegrationTests(unittest.TestCase):
    def test_process_residence_and_callable_payload_estimate_share_real_budget(self):
        from neocortex.runtime.control.global_resources import (
            CoordinatedMemoryGate, GlobalResourceCoordinator,
            GlobalResourceLimits, ResourceSample,
        )

        coordinator = GlobalResourceCoordinator(
            ("code",),
            GlobalResourceLimits(
                memory_budget_bytes=512,
                min_free_memory_bytes=0, min_free_commit_bytes=0,
                cpu_slots=4, native_thread_slots=4,
                wait_timeout_seconds=0.3, poll_interval_seconds=0.01,
            ),
            resource_probe=lambda: ResourceSample(
                available_physical=4096, available_commit=4096,
                total_physical=8192, total_commit=8192, cpu_load_percent=0,
            ),
            cpu_load_probe=lambda: 0,
        )
        self.addCleanup(coordinator.close)
        with elastic_map(
            _process_identity, [1, 2], gate=CoordinatedMemoryGate(coordinator, "code"),
            executor_kind="process", process_resident_bytes=128,
            estimated_bytes=lambda _item: 384,
        ) as results:
            first = next(results)
            self.assertEqual(coordinator.summary().resident_bytes, 128)
            self.assertEqual(coordinator.summary().transient_bytes, 384)
            second = next(results)
            self.assertEqual(first[1], second[1])
        summary = coordinator.summary()
        self.assertEqual(summary.peak_reserved_bytes, 512)
        self.assertEqual(summary.resident_bytes, 0)
        self.assertEqual(summary.transient_bytes, 0)

    def test_real_grant_context_checkpoint_and_io_release_keep_result_memory(self):
        from neocortex.runtime.control.global_resources import (
            CoordinatedMemoryGate,
            GlobalResourceCoordinator,
            GlobalResourceLimits,
            ResourceSample,
            current_resource_grant,
        )

        coordinator = GlobalResourceCoordinator(
            ("code",),
            GlobalResourceLimits(
                memory_budget_bytes=1024,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                cpu_slots=2,
                native_thread_slots=2,
                io_slots=2,
                wait_timeout_seconds=1,
                poll_interval_seconds=0.01,
            ),
            resource_probe=lambda: ResourceSample(
                available_physical=2048, available_commit=2048,
                total_physical=4096, total_commit=4096, cpu_load_percent=0,
            ),
            cpu_load_probe=lambda: 0,
        )
        self.addCleanup(coordinator.close)
        gate = CoordinatedMemoryGate(coordinator, "code")
        owner = threading.get_ident()
        observed = []

        def prepare(item):
            self.assertEqual(threading.get_ident(), owner)
            self.assertIsNotNone(current_resource_grant())
            return item

        def worker(item):
            grant = current_resource_grant()
            self.assertIsNotNone(grant)
            self.assertEqual(grant.native_threads, 1)
            grant.checkpoint()
            self.assertEqual(grant.cpu_slots, 1)
            observed.append(item)
            return item

        with elastic_map(worker, [1], gate=gate, prepare=prepare,
                         estimated_bytes=64, io_slots=1, io_device="8:0") as results:
            self.assertEqual(next(results), 1)
            self.assertIsNotNone(current_resource_grant())
            summary = coordinator.summary()
            self.assertEqual(summary.routes["code"].transient_bytes, 64)
            self.assertEqual(current_resource_grant().cpu_slots, 1)
            self.assertEqual(summary.routes["code"].native_threads, 1)
            self.assertEqual(summary.routes["code"].io_slots, 1)
        self.assertEqual(observed, [1])
        self.assertIsNone(current_resource_grant())
        self.assertEqual(coordinator.summary().routes["code"].transient_bytes, 0)


if __name__ == "__main__":
    unittest.main()
