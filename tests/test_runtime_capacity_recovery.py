"""Live capacity, deadline and process reuse regressions through public owners."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.runtime.control import native_library_resources as native
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.elastic_workers import current_worker_cancellation, elastic_map
from neocortex.runtime.control.global_resources import (
    CoordinatedMemoryGate,
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    ResourceWaitTimeout,
)


def _coordinator(sample, *, checkpoint=None, **limits):
    options = replace(
        GlobalResourceLimits(
            memory_budget_bytes=4096, min_free_memory_bytes=0,
            min_free_commit_bytes=0, cpu_slots=4, poll_interval_seconds=0.01,
            sample_interval_seconds=0.01, wait_timeout_seconds=0.3,
            memory_hysteresis_bytes=0,
        ),
        **limits,
    )
    return GlobalResourceCoordinator(
        ("code", "other"), options,
        resource_probe=lambda: sample[0], checkpoint=checkpoint,
        effective_cpu_probe=lambda: 4,
    )


def _sample(*, commit=4096, capacity=4, external=0):
    return ResourceSample(
        available_physical=4096, available_commit=commit,
        total_physical=8192, total_commit=8192,
        effective_cpu_capacity=capacity, external_cpu_cores=external,
    )


def _identity(value):
    return value


def _cooperative_process(paths):
    started, observed = (Path(value) for value in paths)
    cancellation = current_worker_cancellation()
    assert cancellation is not None
    started.write_text("started", encoding="ascii")
    cancelled = cancellation.wait(1.0)
    observed.write_text("cancelled" if cancelled else "expired", encoding="ascii")
    cancellation.checkpoint()
    return 1


def test_commit_headroom_limits_total_target_and_recovers_without_new_scope():
    values = [_sample(commit=500)]
    coordinator = _coordinator(values, min_free_commit_bytes=100)
    gate = CoordinatedMemoryGate(coordinator, "code")
    assert gate.worker_capacity(estimated_bytes=200) == 2
    with gate.resident(100, resident_key="existing-interpreter"):
        assert gate.worker_capacity(estimated_bytes=200, reusable_resident_bytes=100) == 2
        with gate.admit(100):
            assert gate.worker_capacity(estimated_bytes=200, reusable_resident_bytes=100) == 2
    values[0] = _sample(commit=900)
    assert gate.worker_capacity(estimated_bytes=200) == 4
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_process_residence_respects_commit_headroom_before_growing_pool():
    # Four interpreters fit RAM, but only two complete tasks fit commit space.
    # Reserving all four interpreters first leaves no room for any task.
    values = [_sample(commit=256)]
    coordinator = _coordinator(values)
    with elastic_map(
        _identity, range(6), gate=CoordinatedMemoryGate(coordinator, "code"),
        estimated_bytes=64, executor_kind="process", process_resident_bytes=64,
        poll_interval=0.01,
    ) as results:
        assert list(results) == list(range(6))
    summary = coordinator.summary()
    assert summary.peak_reserved_bytes <= 256
    assert summary.resident_bytes == summary.transient_bytes == 0


@pytest.mark.parametrize("capacity", [1.5, 2.0])
def test_explicit_cpu_ceiling_cannot_override_live_capacity_before_attribution(capacity):
    values = [_sample(capacity=capacity, external=None)]
    coordinator = _coordinator(values, cpu_slots=8)
    gate = CoordinatedMemoryGate(coordinator, "code")
    assert gate.worker_capacity() == 2
    assert coordinator.cpu_slots == 8  # Preserve the user's configured ceiling.
    values[0] = _sample(capacity=6, external=None)
    assert gate.worker_capacity() == 6
    values[0] = _sample(capacity=16, external=None)
    assert gate.worker_capacity() == 8


def test_owner_deadline_cancels_cooperative_thread_during_payload():
    started = threading.Event()
    observed = []
    deadline = RuntimeError("owner deadline expired")

    def checkpoint():
        if started.is_set():
            raise deadline

    def worker(_value):
        cancellation = current_worker_cancellation()
        assert cancellation is not None
        started.set()
        observed.append(cancellation.wait(1.0))
        cancellation.checkpoint()
        return 1

    coordinator = _coordinator([_sample()], checkpoint=checkpoint)
    with pytest.raises(RuntimeError) as error:
        with elastic_map(
            worker, [1], gate=CoordinatedMemoryGate(coordinator, "code"),
            estimated_bytes=64, max_workers=1, poll_interval=0.01,
        ) as results:
            list(results)
    assert error.value is deadline
    assert observed == [True]
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_owner_deadline_cancels_cooperative_process_during_payload(tmp_path):
    started, observed = tmp_path / "started", tmp_path / "observed"
    deadline = RuntimeError("owner deadline expired")

    def checkpoint():
        if started.exists():
            raise deadline

    coordinator = _coordinator([_sample()], checkpoint=checkpoint)
    with pytest.raises(RuntimeError) as error:
        with elastic_map(
            _cooperative_process, [(str(started), str(observed))],
            gate=CoordinatedMemoryGate(coordinator, "code"), estimated_bytes=64,
            executor_kind="process", process_resident_bytes=64,
            max_workers=1, poll_interval=0.01,
        ) as results:
            list(results)
    assert error.value is deadline
    assert observed.read_text(encoding="ascii") == "cancelled"
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_blas_wait_observes_owner_deadline_while_library_lock_is_held():
    expired, finished = threading.Event(), threading.Event()
    errors = []
    deadline = RuntimeError("owner deadline expired")

    def checkpoint():
        if expired.is_set():
            raise deadline

    coordinator = _coordinator([_sample()], checkpoint=checkpoint)
    gate = CoordinatedMemoryGate(coordinator, "code")

    def waiting_operation():
        try:
            with native.native_library_operation(gate, 64):
                raise AssertionError("expired operation must not execute")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with native._NATIVE_LIBRARY_LOCK:
        worker = threading.Thread(target=waiting_operation)
        worker.start()
        expired.set()
        finished_while_locked = finished.wait(0.5)
        assert coordinator.summary().active_execution_requests == 0
    worker.join(2)
    assert not worker.is_alive()
    assert finished_while_locked
    assert errors == [deadline]
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_native_budget_uses_capacity_at_admission_after_planning_race(monkeypatch):
    values = [_sample()]
    coordinator = _coordinator(values)
    admit = coordinator.admit

    @contextmanager
    def competing_admission(*args, **kwargs):
        values[0] = _sample(external=2)
        with admit(*args, **kwargs) as grant:
            yield grant

    monkeypatch.setattr(coordinator, "admit", competing_admission)
    gate = CoordinatedMemoryGate(coordinator, "code")
    with gate.native_budget(256) as grant:
        assert grant.cpu_slots == grant.native_threads == 2
        assert grant.native_env["OMP_NUM_THREADS"] == "2"
        assert coordinator.summary().transient_bytes == 256
    assert coordinator.summary().native_threads == 0


def test_native_budget_uses_unoccupied_capacity_of_its_own_route():
    coordinator = _coordinator([_sample()])
    gate = CoordinatedMemoryGate(coordinator, "code")
    cancellation = CancellationToken()
    timeout = threading.Timer(0.4, cancellation.cancel)
    timeout.start()
    try:
        with gate.admit(64):
            with gate.native_budget(128, cancellation=cancellation) as grant:
                assert grant.cpu_slots == grant.native_threads == 3
                assert coordinator.summary().cpu_slots_in_use == 4
                assert coordinator.summary().transient_bytes == 192
    finally:
        timeout.cancel()
        timeout.join()
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_native_budget_preserves_active_backend_width_and_replans_next_batch():
    values = [_sample(external=2)]
    coordinator = _coordinator(values)
    gate = CoordinatedMemoryGate(coordinator, "code")
    with gate.native_budget(256, max_threads=3) as grant:
        assert grant.cpu_slots == grant.native_threads == 2
        grant.release_cpu()
        values[0] = _sample(external=3)
        with pytest.raises(ResourceWaitTimeout):
            grant.checkpoint()
        assert coordinator.summary().native_threads == 0
        assert coordinator.summary().transient_bytes == 256
        values[0] = _sample()
        grant.checkpoint()
        assert grant.cpu_slots == grant.native_threads == 2
        assert coordinator.summary().transient_bytes == 256
        grant.release_cpu()
        assert coordinator.summary().native_threads == 0
    with gate.native_budget(256, max_threads=3) as grant:
        assert grant.cpu_slots == grant.native_threads == 3
    values[0] = _sample(external=3)
    with gate.native_budget(256, max_threads=3) as grant:
        assert grant.cpu_slots == grant.native_threads == 1
    assert coordinator.summary().transient_bytes == 0


def test_cancelled_native_wait_preserves_other_route_and_returns_no_new_grant():
    values = [_sample(external=4)]
    coordinator = _coordinator(values)
    other = CoordinatedMemoryGate(coordinator, "other")
    gate = CoordinatedMemoryGate(coordinator, "code")
    cancellation = CancellationToken()
    timeout = threading.Timer(0.05, cancellation.cancel)
    with other.resident(96, resident_key="other-model"):
        timeout.start()
        try:
            with pytest.raises(CancellationRequested), gate.native_budget(
                128, max_threads=3, cancellation=cancellation,
            ):
                pytest.fail("native work started during full external CPU pressure")
        finally:
            timeout.cancel()
            timeout.join()
        summary = coordinator.summary()
        assert summary.resident_bytes == 96 and summary.transient_bytes == 0
        assert summary.native_threads == 0 and summary.cpu_slots_in_use == 0
        assert not coordinator.cancellation.is_cancelled
    assert coordinator.summary().resident_bytes == 0


def test_native_grant_obeys_both_native_ceiling_and_other_python_work():
    coordinator = _coordinator([_sample()], native_thread_slots=2)
    gate = CoordinatedMemoryGate(coordinator, "code")
    with coordinator.admit("other", 64, cpu_slots=1, native_threads=0):
        with gate.native_budget(128, max_threads=4) as grant:
            assert grant.cpu_slots == grant.native_threads == 2
            with coordinator.admit("other", 64, cpu_slots=1, native_threads=0):
                assert coordinator.summary().cpu_slots_in_use == 4
                assert coordinator.summary().native_threads == 2
    assert coordinator.summary().native_threads == 0


def test_source_cleanup_error_cannot_replace_an_owner_deadline():
    started = threading.Event()
    deadline = RuntimeError("owner deadline expired")

    def checkpoint():
        if started.is_set():
            raise deadline

    class Source:
        emitted = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.emitted:
                raise StopIteration
            self.emitted = True
            return 1

        def close(self):
            raise ValueError("source cleanup failed")

    def worker(_value):
        token = current_worker_cancellation()
        assert token is not None
        started.set()
        token.wait(1)
        token.checkpoint()

    coordinator = _coordinator([_sample()], checkpoint=checkpoint)
    with pytest.raises(RuntimeError) as error:
        with elastic_map(
            worker, Source(), gate=CoordinatedMemoryGate(coordinator, "code"),
            estimated_bytes=64, max_workers=1, poll_interval=0.01,
        ) as values:
            list(values)
    assert error.value is deadline
    assert any("source cleanup failed" in note for note in deadline.__notes__)
    assert coordinator.summary().resident_bytes == coordinator.summary().transient_bytes == 0


def test_blas_wait_observes_implicit_gate_cancellation_without_cancelling_scope():
    cancellation = CancellationToken()
    coordinator = _coordinator([_sample()])
    gate = CoordinatedMemoryGate(coordinator, "code", cancellation=cancellation)
    finished = threading.Event()
    errors = []

    def waiter():
        try:
            with native.native_library_operation(gate, 64):
                pytest.fail("cancelled waiter entered native work")
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    with native._NATIVE_LIBRARY_LOCK:
        thread = threading.Thread(target=waiter)
        thread.start()
        cancellation.cancel()
        observed_before_unlock = finished.wait(0.5)
    thread.join(1)
    assert observed_before_unlock and not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CancellationRequested)
    assert not coordinator.cancellation.is_cancelled
    assert coordinator.summary().transient_bytes == coordinator.summary().native_threads == 0


@pytest.mark.parametrize("configured_cpu", [None, 8])
def test_live_backend_waits_for_original_width_after_quota_shrinks(configured_cpu):
    values = [_sample()]
    coordinator = _coordinator(values, cpu_slots=configured_cpu)
    gate = CoordinatedMemoryGate(coordinator, "code")
    recovered = threading.Event()

    def restore():
        values[0] = _sample()
        recovered.set()

    with gate.native_budget(256, max_threads=4) as grant:
        assert grant.cpu_slots == grant.native_threads == 4
        values[0] = _sample(capacity=2)
        assert gate.worker_capacity() == 2
        timer = threading.Timer(0.05, restore)
        timer.start()
        try:
            grant.checkpoint()
            assert recovered.is_set()
            assert grant.cpu_slots == grant.native_threads == 4
            assert coordinator.summary().transient_bytes == 256
        finally:
            timer.cancel()
            timer.join()
    assert coordinator.summary().transient_bytes == coordinator.summary().native_threads == 0
