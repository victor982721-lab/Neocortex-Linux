"""Explicit child leases remain observable without proc task child lists."""

from __future__ import annotations

from contextlib import ExitStack
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.runtime.control import global_resources as gr
from neocortex.runtime.control import resource_sampler
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from tests.test_owned_resource_sampler import ProcFixture


KIB = 1024


def _environment(tmp_path, monkeypatch, *, interval=0, physical=2000 * KIB,
                 available=1000 * KIB, limits=None):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.process(50, parent=40, start=20, private=400)
    for pid in (40, 50):
        (proc.root / str(pid) / "task" / str(pid) / "children").unlink()
    (proc.root / "meminfo").write_text(
        f"MemTotal: {physical // KIB} kB\nMemAvailable: {available // KIB} kB\n",
    )
    sampler = proc.sampler(sample_interval_seconds=interval)
    monkeypatch.setattr(resource_sampler, "OwnedResourceSampler", lambda **_kwargs: sampler)
    monkeypatch.setattr(gr, "os", SimpleNamespace(getpid=lambda: 40, name="posix"))
    monkeypatch.setattr(
        gr, "Path", lambda value: proc.root / str(value)[6:]
        if str(value).startswith("/proc/") else Path(value),
    )
    monkeypatch.setattr(
        gr, "memory_snapshot", lambda: MemorySnapshot(available, None, physical, None),
    )
    coordinator = gr.GlobalResourceCoordinator(
        ("pdf", "other"),
        limits or gr.GlobalResourceLimits(
            memory_budget_bytes=1000 * KIB, min_free_memory_bytes=100 * KIB,
            min_free_commit_bytes=0, cpu_slots=4, native_thread_slots=4,
            memory_hysteresis_bytes=0, sample_interval_seconds=0.01,
            poll_interval_seconds=0.01, wait_timeout_seconds=0.03,
        ),
        effective_cpu_probe=lambda: 4,
    )
    return proc, sampler, coordinator


def _materialize(proc, *, available=400):
    (proc.root / "meminfo").write_text(
        f"MemTotal: 2000 kB\nMemAvailable: {available} kB\n",
    )


def test_registered_child_renews_with_measured_credit_when_children_are_unavailable(tmp_path, monkeypatch):
    proc, sampler, coordinator = _environment(tmp_path, monkeypatch, interval=60)
    before = sampler.current_sample()
    assert before is not None and before.owned_pids == (40,)
    with coordinator.admit("pdf", 600 * KIB, native_threads=1) as grant:
        assert grant.register_process(50, 20) == (50, 20)
        _materialize(proc)
        grant.checkpoint()
        summary = coordinator.summary()
        assert summary.materialized_credit_bytes == 400 * KIB
        assert summary.transient_bytes == 600 * KIB
        assert summary.cpu_slots_in_use == summary.native_threads == 1
        observed = sampler.current_sample()
        assert observed is not None and observed is not before
        assert observed.process_memory_bytes[(50, 20)] == 400 * KIB
        assert not observed.owned_memory_complete
        assert not observed.cpu_observation_complete
        assert observed.owned_materialized_bytes is observed.external_cpu_cores is None
    assert coordinator.summary().materialized_credit_bytes == 0


def test_registered_child_with_unknown_private_bytes_still_waits_for_memory(tmp_path, monkeypatch):
    proc, sampler, coordinator = _environment(tmp_path, monkeypatch)
    (proc.root / "50" / "smaps_rollup").unlink()
    with coordinator.admit("pdf", 600 * KIB, native_threads=1) as grant:
        grant.register_process(50, 20)
        _materialize(proc)
        with pytest.raises(gr.ResourceWaitTimeout) as failure:
            grant.checkpoint()
        assert failure.value.reason == "memory"
        summary = coordinator.summary()
        assert summary.materialized_credit_bytes == 0
        assert summary.cpu_slots_in_use == summary.native_threads == 0
        observed = sampler.current_sample()
        assert observed is not None and (50, 20) not in observed.process_memory_bytes


def test_registered_child_recovers_observed_pressure_with_default_headroom_and_hysteresis(tmp_path, monkeypatch):
    # Reproduce the installed PDF's observed quantities with actual defaults.
    # The first observation sees the child's allocation before registration.
    physical = 20 * 1024**3
    proc, _sampler, coordinator = _environment(
        tmp_path, monkeypatch, physical=physical, available=1_921_511_424,
        limits=gr.GlobalResourceLimits(
            cpu_slots=4, poll_interval_seconds=0.01, sample_interval_seconds=0.01,
            wait_timeout_seconds=0.03,
        ),
    )
    proc.process(50, parent=40, start=20, private=137_236_480 // KIB)
    (proc.root / "50" / "task" / "50" / "children").unlink()
    with coordinator.admit("pdf", 1_263_979_776, native_threads=1) as grant:
        (proc.root / "meminfo").write_text(
            f"MemTotal: {physical // KIB} kB\nMemAvailable: {1_730_465_792 // KIB} kB\n",
        )
        assert coordinator.worker_capacity("pdf") == 0
        before = coordinator.summary()
        assert before.admission_paused
        assert before.materialized_credit_bytes == 0
        assert before.min_free_memory_bytes == 512 * 1024**2
        grant.register_process(50, 20)
        grant.checkpoint()
        after = coordinator.summary()
        assert not after.admission_paused
        assert after.materialized_credit_bytes == 137_236_480
        assert after.transient_bytes == 1_263_979_776
        assert after.cpu_slots_in_use == after.native_threads == 1


@pytest.mark.parametrize("change", ["reuse", "disappear", "outside_cgroup"])
def test_registered_child_loses_credit_when_identity_or_scope_is_no_longer_valid(tmp_path, monkeypatch, change):
    proc, sampler, coordinator = _environment(tmp_path, monkeypatch)
    with coordinator.admit("pdf", 600 * KIB, native_threads=1) as grant:
        grant.register_process(50, 20)
        coordinator.worker_capacity("pdf")
        assert coordinator.summary().materialized_credit_bytes == 400 * KIB
        if change == "reuse":
            proc.process(50, parent=1, start=30, private=900)
        elif change == "disappear":
            (proc.root / "50" / "stat").unlink()
        else:
            (proc.root / "50" / "cgroup").write_text("0::/other-job\n")
        _materialize(proc)
        with pytest.raises(gr.ResourceWaitTimeout):
            grant.checkpoint()
        assert coordinator.summary().materialized_credit_bytes == 0
        observed = sampler.current_sample()
        assert observed is not None and (50, 20) not in observed.process_memory_bytes


def test_registered_process_credit_is_deduplicated_and_bound_to_live_leases(tmp_path, monkeypatch):
    _proc, _sampler, coordinator = _environment(tmp_path, monkeypatch)
    with ExitStack() as first_stack:
        first = first_stack.enter_context(coordinator.admit("pdf", 200 * KIB, cpu_slots=0))
        # Independent owners may release leases in either order. Keep each
        # admission's ambient ContextVar scope with its own owner as well.
        second_owner = Context()
        second_admission = coordinator.admit("other", 200 * KIB, cpu_slots=0)
        second = second_owner.run(second_admission.__enter__)
        try:
            first.register_process(50, 20)
            second.register_process(50, 20)
            coordinator.worker_capacity()
            assert coordinator.summary().materialized_credit_bytes == 200 * KIB
            first_stack.close()
            assert coordinator.summary().materialized_credit_bytes == 200 * KIB
            assert coordinator.summary().transient_bytes == 200 * KIB
        finally:
            second_owner.run(second_admission.__exit__, None, None, None)
    assert coordinator.summary().materialized_credit_bytes == 0
    assert coordinator.summary().transient_bytes == 0
