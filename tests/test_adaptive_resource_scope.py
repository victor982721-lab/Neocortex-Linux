from __future__ import annotations

import threading
import time
import tempfile
from contextlib import ExitStack
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from neocortex.runtime.control import global_resources as gr
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.elastic_workers import ImmediateResult, elastic_map


def coordinator(sample, **limits):
    config = {
        "memory_budget_bytes": 10_000,
        "min_free_memory_bytes": 0,
        "min_free_commit_bytes": 0,
        "cpu_slots": 16,
        "poll_interval_seconds": 0.005,
        "sample_interval_seconds": 0.01,
        "wait_timeout_seconds": 0.02,
        "memory_hysteresis_bytes": 0,
    }
    config.update(limits)
    return gr.GlobalResourceCoordinator(
        ("a", "b"), gr.GlobalResourceLimits(**config), resource_probe=lambda: sample[0]
    )


def sample(external=0.0, memory=100_000, capacity=16.0):
    return gr.ResourceSample(
        memory,
        None,
        100_000,
        effective_cpu_capacity=capacity,
        own_cpu_cores=max(0.0, capacity - external),
        external_cpu_cores=external,
    )


def test_external_pressure_reduces_then_restores_all_cores():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    assert gate.worker_capacity() == 16
    values[0] = sample(external=12)
    assert gate.worker_capacity() == 4
    values[0] = sample(external=16)
    assert gate.worker_capacity() == 0
    values[0] = sample(external=0)
    assert gate.worker_capacity() == 16
    with ExitStack() as stack:
        for _ in range(16):
            stack.enter_context(gate.admit(1))
        assert c.summary().peak_cpu_slots == 16
        assert c.summary().peak_native_threads == 16


def test_lost_tree_attribution_does_not_prove_cpu_recovery():
    values = [sample(external=12)]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    assert gate.worker_capacity() == 4
    values[0] = gr.ResourceSample(100_000, None, 100_000, effective_cpu_capacity=16)
    assert gate.worker_capacity() == 4
    values[0] = sample(external=0)
    assert gate.worker_capacity() == 16


def test_spool_growth_is_disk_budget_not_ram_and_failure_preserves_lease(monkeypatch):
    values = [sample()]
    c = coordinator(values, memory_budget_bytes=100, temp_budget_bytes=1000)
    gate = gr.CoordinatedMemoryGate(c, "a")
    monkeypatch.setattr(gr.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10_000))
    with gate.resident(10, resident_key="spool") as grant:
        grant.resize_temp_bytes(900)
        assert c.summary().resident_bytes == 10
        assert c.summary().temp_bytes == 900
        with pytest.raises(gr.MemoryBudgetExceeded):
            grant.resize_temp_bytes(1001)
        assert c.summary().temp_bytes == 900
        grant.resize_temp_bytes(200)
        assert c.summary().temp_bytes == 200
    assert c.summary().temp_bytes == 0


def test_model_residency_neither_occupies_cpu_nor_suppresses_headroom_timeout():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.resident(100, resident_key="model"):
        assert c.summary().active_execution_requests == 0
        assert c.summary().native_threads == 0
        values[0] = sample(external=16)
        with pytest.raises(gr.ResourceWaitTimeout), gate.admit(1):
            pass
        assert c.summary().resident_bytes == 100
    assert c.summary().resident_bytes == 0


def test_release_execution_retains_results_and_checkpoint_renegotiates():
    values = [sample()]
    c = coordinator(values, io_slots=1)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.admit(100, io_slots=1, io_device="disk", native_threads=2) as grant:
        grant.release_cpu()
        grant.release_cpu()
        summary = c.summary()
        assert summary.transient_bytes == 100
        assert summary.native_threads == 0 and summary.io_slots == 0
        grant.checkpoint()
        summary = c.summary()
        assert summary.native_threads == 2 and summary.io_slots == 1
        assert summary.active_execution_requests == 1
    assert c.summary().transient_bytes == 0
    assert c.summary().native_threads == 0
    assert c.summary().io_slots == 0


def test_native_environment_is_per_grant_not_process_global(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.admit(1, native_threads=3) as grant:
        environment = grant.subprocess_env({"CUSTOM": "yes"})
        assert environment["OMP_NUM_THREADS"] == "3"
        assert environment["OMP_THREAD_LIMIT"] == "3"
        assert environment["CUSTOM"] == "yes"
    import os

    assert os.environ["OMP_NUM_THREADS"] == "99"


def test_gpu_requires_real_registered_capacity_and_is_released():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with pytest.raises(gr.MemoryBudgetExceeded), gate.admit(1, gpu_bytes=10, gpu_device="gpu0"):
        pass
    c.register_gpu_device("gpu0", 100, available_probe=lambda: 100)
    with gate.admit(1, gpu_bytes=70, gpu_device="gpu0"):
        assert c.summary().gpu_bytes == 70
        assert c.summary().routes["a"].gpu_bytes == 70
    assert c.summary().gpu_bytes == 0


def test_bound_process_uss_credit_is_not_aggregate_scope_credit():
    values = [sample(memory=1000)]
    c = coordinator(values, memory_budget_bytes=1000, min_free_memory_bytes=100)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.admit(600) as grant:
        c._last_owned_snapshot = SimpleNamespace(process_memory_bytes={(123, 456): 600})
        values[0] = sample(memory=400)
        with c._condition:
            c._observe_live_resources()
            assert c._materialized_credit_locked() == 0
        grant._request.process_identities.add((123, 456))
        with gate.admit(200):
            assert c.summary().materialized_credit_bytes == 600
        c._last_owned_snapshot = SimpleNamespace(process_memory_bytes={(123, 789): 600})
        with c._condition:
            assert c._materialized_credit_locked() == 0


def test_scope_monitors_without_new_admissions_and_nested_scope_preserves_monitor():
    values = [sample()]
    c = coordinator(values)
    with gr.resource_scope(c):
        monitor = c._monitor_thread
        with gr.resource_scope(c):
            assert gr.current_resource_coordinator() is c
            assert gr.resource_gate("inventory").coordinator is c
        assert monitor is not None and monitor.is_alive()
        values[0] = sample(external=16)
        deadline = time.monotonic() + 1
        while not c.summary().admission_paused and time.monotonic() < deadline:
            time.sleep(0.005)
        assert c.summary().admission_paused
    assert not monitor.is_alive()
    assert gr.current_resource_coordinator() is None


def test_checkpoint_cancellation_keeps_memory_until_owner_cleanup():
    values = [sample()]
    c = coordinator(values)
    token = CancellationToken()
    gate = gr.CoordinatedMemoryGate(c, "a", cancellation=token)
    with pytest.raises(CancellationRequested):
        with gate.admit(100) as grant:
            token.cancel()
            grant.checkpoint()
    assert c.summary().transient_bytes == 0
    assert c.summary().active_execution_requests == 0


def test_automatic_capacity_expansion_uses_current_not_startup_limits(monkeypatch):
    counts = [2]
    values = [
        gr.ResourceSample(900_000, None, 1_000_000, effective_cpu_capacity=2, external_cpu_cores=0)
    ]
    monkeypatch.setattr(gr, "effective_cpu_count", lambda: counts[0])
    c = gr.GlobalResourceCoordinator(
        ("a",), gr.GlobalResourceLimits(), resource_probe=lambda: values[0]
    )
    assert c.cpu_slots == 2
    assert c.memory_budget_bytes > 900_000
    counts[0] = 16
    values[0] = gr.ResourceSample(
        15_000_000, None, 16_000_000, effective_cpu_capacity=16, external_cpu_cores=0
    )
    with c.admit("a", 2_000_000, cpu_slots=16, native_threads=16):
        assert c.summary().peak_cpu_slots == 16
        assert c.summary().effective_memory_budget_bytes > 15_000_000


def test_io_per_device_quota_can_be_released_before_result_consumption():
    values = [sample()]
    c = coordinator(values, io_device_slots={"disk": 1})
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.admit(100, io_slots=1, io_device="disk") as grant:
        grant.release_cpu()
        with gate.admit(100, io_slots=1, io_device="disk"):
            assert c.summary().io_slots == 1
            assert c.summary().transient_bytes == 200


def test_cancelling_waiter_does_not_release_another_routes_residency():
    values = [sample()]
    c = coordinator(values, cpu_slots=1)
    gate = gr.CoordinatedMemoryGate(c, "a")
    token = CancellationToken()
    errors = []

    def waiter():
        try:
            with c.admit("b", 1, cancellation=token):
                pytest.fail("waiter unexpectedly entered")
        except CancellationRequested:
            errors.append(True)

    with gate.resident(100, resident_key="model"), gate.admit(1):
        thread = threading.Thread(target=waiter)
        thread.start()
        deadline = time.monotonic() + 1
        while c.route_wait_count("b") == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        token.cancel()
        thread.join(1)
        assert not thread.is_alive() and errors == [True]
        assert c.summary().resident_bytes == 100
    assert c.summary().resident_bytes == 0


def test_variable_cost_cache_hit_does_not_reserve_all_ram_for_idle_processes():
    values = [sample()]
    c = coordinator(values, cpu_slots=4, memory_budget_bytes=256)
    gate = gr.CoordinatedMemoryGate(c, "a")
    residence = threading.Event()

    class ObservedGate:
        cancellation = None

        def worker_capacity(self, **kwargs):
            return gate.worker_capacity(**kwargs)

        @contextmanager
        def admit(self, *args, **kwargs):
            with gate.admit(*args, **kwargs) as grant:
                if kwargs.get("phase") == "elastic-process-resident":
                    residence.set()
                yield grant

    def source():
        yield 0
        assert residence.wait(1)
        yield 1

    with elastic_map(
        abs,
        source(),
        gate=ObservedGate(),
        estimated_bytes=lambda item: 0 if item == 0 else 32,
        prepare=lambda item: ImmediateResult(item),
        executor_kind="process",
        process_resident_bytes=64,
        poll_interval=0.005,
    ) as results:
        assert list(results) == [0, 1]


def test_ready_result_drains_under_memory_pressure_ahead_of_new_work():
    values = [sample()]
    c = coordinator(values, cpu_slots=2, io_slots=1)
    gate = gr.CoordinatedMemoryGate(c, "a")
    waiter_cancel = CancellationToken()
    entered = []

    def waiter():
        try:
            with c.admit("a", 1, cancellation=waiter_cancel):
                entered.append(True)
        except (CancellationRequested, gr.ResourceWaitTimeout):
            pass

    with elastic_map(abs, [-2], gate=gate, estimated_bytes=100, io_slots=1,
                     poll_interval=0.005) as results:
        assert next(results) == 2
        values[0] = gr.ResourceSample(
            0, None, 100_000, effective_cpu_capacity=2, external_cpu_cores=2,
            memory_pressure_some_percent=30, io_pressure_some_percent=40,
        )
        thread = threading.Thread(target=waiter)
        thread.start()
        deadline = time.monotonic() + 1
        while c.route_wait_count("a") == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        grant = gr.current_resource_grant()
        assert grant is not None
        grant.checkpoint()
        summary = c.summary()
        assert summary.admission_paused
        assert summary.transient_bytes == 100
        assert summary.cpu_slots_in_use == 1 and summary.native_threads == 1
        assert summary.io_slots == 1
        assert entered == []
        waiter_cancel.cancel()
        thread.join(1)
        assert not thread.is_alive()
    assert c.summary().transient_bytes == 0
    assert c.summary().cpu_slots_in_use == 0


def test_drain_does_not_exempt_new_work_or_override_cancellation():
    values = [sample()]
    token = CancellationToken()
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a", cancellation=token)
    with gate.admit(100) as grant:
        grant.release_cpu()
        values[0] = sample(memory=0)
        with pytest.raises(gr.ResourceWaitTimeout):
            grant.checkpoint(drain=False)
        token.cancel()
        with pytest.raises(CancellationRequested):
            grant.checkpoint(drain=True)
        assert c.summary().transient_bytes == 100
    assert c.summary().transient_bytes == 0


def test_owner_can_normalize_absolute_candidate_budget_error_and_continue():
    values = [sample()]
    c = coordinator(values, memory_budget_bytes=100)
    gate = gr.CoordinatedMemoryGate(c, "a")
    owner = threading.get_ident()
    computed = []

    def failed(item, error):
        assert threading.get_ident() == owner
        assert isinstance(error, gr.MemoryBudgetExceeded)
        return ImmediateResult(("too-large", item))

    def compute(item):
        computed.append(item)
        return ("ok", item)

    with elastic_map(compute, [200, 10], gate=gate, estimated_bytes=lambda item: item,
                     on_admission_error=failed, poll_interval=0.005) as results:
        assert list(results) == [("too-large", 200), ("ok", 10)]
    assert computed == [10]
    assert c.summary().transient_bytes == 0


def test_native_threads_share_batch_ram_and_result_can_return_workspace():
    values = [sample()]
    c = coordinator(values, memory_budget_bytes=1000)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.resident(500, resident_key="model"):
        with gate.native_budget(500) as grant:
            assert grant.native_threads == 16
            grant.release_cpu()
            grant.shrink_transient_bytes(40)
            assert c.summary().transient_bytes == 40
            assert c.summary().resident_bytes == 500
            with pytest.raises(ValueError):
                grant.shrink_transient_bytes(41)
            assert c.summary().transient_bytes == 40
        assert c.summary().transient_bytes == 0


def test_resident_final_batch_has_explicit_drain_execution_under_pressure():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with gate.resident(100, resident_key="writer-buffer") as resident:
        values[0] = sample(external=16, memory=0)
        with resident.drain_admission(io_slots=1, io_device="db") as grant:
            assert grant.cpu_slots == 1 and grant.native_threads == 1
            assert c.summary().resident_bytes == 100
            assert c.summary().io_slots == 1
        assert c.summary().cpu_slots_in_use == 0
        assert c.summary().resident_bytes == 100


def test_gpu_pending_promises_are_not_allocated_twice_from_the_same_free_sample():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    c.register_gpu_device("gpu0", 1000, available_probe=lambda: 500)
    with gate.resident(1, resident_key="first", gpu_bytes=300, gpu_device="gpu0"):
        assert gate.gpu_worker_capacity("gpu0", 300, reusable_resident_bytes=300) == 1
        with pytest.raises(gr.ResourceWaitTimeout):
            with gate.resident(1, resident_key="second", gpu_bytes=300, gpu_device="gpu0"):
                pytest.fail("two 300-byte promises exceeded the 500-byte free sample")
    assert c.summary().gpu_bytes == 0


def test_gpu_credit_requires_registered_identity_and_measured_materialization():
    values = [sample()]
    free = [1000]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    c.register_gpu_device("gpu0", 1000, available_probe=lambda: free[0],
                          materialized_probe=lambda: {(123, 456): 400})
    with gate.resident(1, resident_key="model", gpu_bytes=400, gpu_device="gpu0") as model:
        model._request.process_identities.add((123, 456))
        free[0] = 600
        assert gate.gpu_worker_capacity("gpu0", 400, reusable_resident_bytes=400) == 2
        with gate.resident(1, resident_key="other", gpu_bytes=400, gpu_device="gpu0"):
            assert c.summary().gpu_bytes == 800
    assert c.summary().gpu_bytes == 0


def test_default_pressure_wait_recovers_after_more_than_five_minutes():
    values = [sample(external=16)]
    clock = [0.0]
    token = CancellationToken()
    c = gr.GlobalResourceCoordinator(
        ("a",), gr.GlobalResourceLimits(
            memory_budget_bytes=10_000, min_free_memory_bytes=0, min_free_commit_bytes=0,
            cpu_slots=16, poll_interval_seconds=0.005,
        ), cancellation=token, resource_probe=lambda: values[0], clock=lambda: clock[0],
    )
    entered = []
    errors = []

    def run():
        try:
            with c.admit("a", 1):
                entered.append(True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1
    while c.route_wait_count("a") == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    clock[0] = 601.0
    with c._condition:
        c._condition.notify_all()
    time.sleep(0.02)
    assert thread.is_alive() and entered == [] and errors == []
    values[0] = sample()
    with c._condition:
        c._condition.notify_all()
    thread.join(1)
    if thread.is_alive():
        token.cancel()
        thread.join(1)
    assert not thread.is_alive() and entered == [True] and errors == []


def test_indefinite_wait_propagates_pure_deadline_checkpoint_in_waiting_caller():
    values = [sample(external=16)]
    visits = []
    owner = threading.get_ident()

    def deadline():
        visits.append(threading.get_ident())
        if len(visits) == 3:
            raise TimeoutError("run deadline")

    c = gr.GlobalResourceCoordinator(
        ("a",), gr.GlobalResourceLimits(
            memory_budget_bytes=100, min_free_memory_bytes=0, min_free_commit_bytes=0,
            cpu_slots=16, poll_interval_seconds=0.005,
        ), resource_probe=lambda: values[0], checkpoint=deadline,
    )
    with gr.resource_scope(c):
        with pytest.raises(TimeoutError, match="run deadline"), c.admit("a", 1):
            pytest.fail("work entered under pressure")
    assert visits == [owner] * 3
    assert c.route_active_request_count("a") == 0


def test_checkpoint_local_deadline_preserves_typed_failure_and_original_cancellation():
    values = [sample()]
    c = coordinator(values, wait_timeout_seconds=None)
    owner_cancel = CancellationToken()
    gate = gr.CoordinatedMemoryGate(c, "a", cancellation=owner_cancel)

    class FileDeadline(CancellationToken):
        count = 0

        def checkpoint(self):
            self.count += 1
            if self.count >= 3:
                raise TimeoutError("file deadline")

    with gate.admit(100) as grant:
        values[0] = sample(external=16)
        with pytest.raises(TimeoutError, match="file deadline"):
            grant.checkpoint(cancellation=FileDeadline())
        assert c.summary().transient_bytes == 100
        assert c.summary().cpu_slots_in_use == 0
        values[0] = sample()
        grant.checkpoint(cancellation=CancellationToken())
        owner_cancel.cancel()
        with pytest.raises(CancellationRequested):
            grant.check_cancellation()
    assert c.summary().transient_bytes == 0


def test_owner_publication_gets_native_and_io_even_after_a_supervisor_producer():
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    with elastic_map(abs, [-1], gate=gate, estimated_bytes=10,
                     native_threads=0, io_slots=0) as results:
        assert next(results) == 1
        grant = gr.current_resource_grant()
        assert grant is not None
        assert grant.cpu_slots == 1 and grant.native_threads == 1
        assert c.summary().io_slots == 1
    assert c.summary().native_threads == 0 and c.summary().io_slots == 0


def test_temporary_credit_excludes_python_buffers_and_uses_owned_file_identity(monkeypatch):
    values = [sample()]
    c = coordinator(values)
    gate = gr.CoordinatedMemoryGate(c, "a")
    free = [1000]
    monkeypatch.setattr(gr.os, "fstatvfs", lambda _fd: SimpleNamespace(f_bavail=free[0], f_frsize=1))
    with gate.resident(10, resident_key="one") as first, gate.resident(10, resident_key="two") as second:
        with tempfile.TemporaryFile(buffering=8192) as stream, tempfile.TemporaryFile() as other:
            first.resize_temp_bytes(900, file_descriptor=stream.fileno())
            stream.write(b"x" * 900)
            assert gr.os.fstat(stream.fileno()).st_size == 0
            first.resize_temp_bytes(950, file_descriptor=stream.fileno())
            assert first._request.temp_materialized_bytes == 0
            with pytest.raises(gr.MemoryBudgetExceeded):
                second.resize_temp_bytes(100, file_descriptor=other.fileno())
            assert second._request.temp_bytes == 0
            stream.flush()
            free[0] = 100
            first.resize_temp_bytes(950, file_descriptor=stream.fileno())
            assert first._request.temp_materialized_bytes == 900
            second.resize_temp_bytes(50, file_descriptor=other.fileno())
            assert c.summary().temp_bytes == 1000
            with pytest.raises(ValueError, match="identity"):
                first.resize_temp_bytes(950, file_descriptor=other.fileno())
    assert c.summary().temp_bytes == 0
