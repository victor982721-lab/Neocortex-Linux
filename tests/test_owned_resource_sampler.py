"""Owned/external attribution and bounded polling without a real corpus."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from neocortex.runtime.control import cpu_runtime, resource_sampler


class ProcFixture:
    def __init__(self, root: Path, monkeypatch, *, cpus: int = 4, affinity=None):
        self.root = root / "proc"
        self.root.mkdir()
        self.now = 0.0
        self.cpus = cpus
        self.affinity = set(range(cpus)) if affinity is None else affinity
        self.membership = "/job"
        self.cgroup = root / "cgroup"
        self.cgroup.mkdir()
        (self.cgroup / "job").mkdir()
        self.leaf = self.cgroup / "job"
        self._busy = [100] * cpus
        self._idle = [100] * cpus
        monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: cpus)
        monkeypatch.setattr(cpu_runtime.os, "sched_getaffinity", lambda _pid: self.affinity)
        self.process(40)
        (self.root / "meminfo").write_text("MemTotal: 8192000 kB\nMemAvailable: 4096000 kB\n")
        (self.root / "40" / "mountinfo").write_text(
            f"1 0 0:1 / {self.cgroup} rw - cgroup2 cgroup rw\n",
        )
        self.advance(0, [0] * cpus)

    def process(
        self, pid=40, *, parent=1, cpu=0, reaped=0, start=10, private=8, threads=None,
    ):
        path = self.root / str(pid)
        path.mkdir(exist_ok=True)
        tasks = {pid: []} if threads is None else threads
        fields = ["0"] * 22
        fields[0] = "S"
        fields[1] = str(parent)
        fields[11] = str(cpu)
        fields[13] = str(reaped)
        fields[17] = str(len(tasks))
        fields[19] = str(start)
        (path / "stat").write_text(f"{pid} (test process ) name) {' '.join(fields)}\n")
        (path / "cgroup").write_text(f"0::{self.membership}\n")
        (path / "smaps_rollup").write_text(
            f"Rss: 65536 kB\nShared_Clean: 65528 kB\nPrivate_Clean: 0 kB\n"
            f"Private_Dirty: {private} kB\n",
        )
        for tid, children in tasks.items():
            task = path / "task" / str(tid)
            task.mkdir(parents=True, exist_ok=True)
            (task / "children").write_text(" ".join(str(child) for child in children))

    def advance(self, seconds, busy):
        self.now += seconds
        for cpu, used in enumerate(busy):
            self._busy[cpu] += used
            self._idle[cpu] += seconds * 100 - used
        fields = [
            f"cpu{cpu} {int(self._busy[cpu])} 0 0 {int(self._idle[cpu])} 0 0 0 0 0 0"
            for cpu in range(self.cpus)
        ]
        aggregate = f"cpu {int(sum(self._busy))} 0 0 {int(sum(self._idle))} 0 0 0 0 0 0"
        (self.root / "stat").write_text("\n".join([aggregate, *fields]) + "\n")
        (self.root / "uptime").write_text(f"{100 + self.now:.2f} 0.00\n")

    def quota(self, cores, usage, *, directory=None):
        directory = self.leaf if directory is None else directory
        (directory / "cpu.max").write_text(f"{int(cores * 100000)} 100000\n")
        (directory / "cpu.stat").write_text(f"usage_usec {usage}\n")

    def sampler(self, **kwargs):
        sampler = resource_sampler.OwnedResourceSampler(
            proc_root=self.root, pid=40, clock=lambda: self.now, **kwargs,
        )
        sampler._ticks_per_second = 100
        return sampler


def test_external_load_reduces_capacity_and_release_recovers_without_self_throttle(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    sampler = proc.sampler()
    first = sampler.sample()
    assert first.own_cpu_cores is first.external_cpu_cores is None
    proc.process(cpu=100)
    proc.advance(1, [100, 100, 100, 100])
    loaded = sampler.sample()
    assert loaded.host_cpu_percent == 100
    assert loaded.own_cpu_cores == 1
    assert loaded.external_cpu_cores == 3
    assert loaded.effective_cpu_capacity - loaded.external_cpu_cores == 1
    proc.process(cpu=500)
    proc.advance(1, [100, 100, 100, 100])
    recovered = sampler.sample()
    assert recovered.own_cpu_cores == 4
    assert recovered.external_cpu_cores == 0
    assert recovered.effective_cpu_capacity == 4


def test_external_work_outside_affinity_does_not_consume_capacity(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch, cpus=4, affinity={0, 1})
    sampler = proc.sampler()
    sampler.sample()
    proc.advance(1, [0, 0, 100, 100])
    sampled = sampler.sample()
    assert sampled.effective_cpu_capacity == 2
    assert sampled.host_cpu_percent == 50
    assert sampled.external_cpu_cores == 0


def test_small_quota_does_not_subtract_work_on_spare_host_cpus(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch, cpus=16)
    proc.quota(2, 0)
    sampler = proc.sampler()
    sampler.sample()
    proc.process(cpu=100)
    proc.advance(1, [100] * 5 + [0] * 11)
    proc.quota(2, 1_000_000)
    sampled = sampler.sample()
    assert sampled.effective_cpu_capacity == 2
    assert sampled.own_cpu_cores == 1
    assert sampled.external_cpu_cores == 0


def test_shared_parent_quota_observes_siblings_and_fractional_capacity(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch, cpus=8)
    proc.quota(6, 0)
    proc.quota(3.5, 0, directory=proc.cgroup)
    sampler = proc.sampler()
    sampler.sample()
    proc.process(cpu=100)
    proc.advance(1, [100] * 3 + [0] * 5)
    proc.quota(6, 1_000_000)
    proc.quota(3.5, 3_000_000, directory=proc.cgroup)
    sampled = sampler.sample()
    assert sampled.effective_cpu_capacity == 3.5
    assert sampled.own_cpu_cores == 1
    assert sampled.external_cpu_cores == 2
    proc.advance(1, [0] * 8)
    assert sampler.sample().external_cpu_cores == 0


def test_child_spawned_by_nonleader_thread_is_observed_once(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    sampler = proc.sampler()
    sampler.sample()
    proc.process(cpu=100, threads={40: [], 41: [50]})
    proc.process(50, parent=40, start=10050, cpu=100, private=32)
    proc.advance(1, [100, 100, 0, 0])
    sampled = sampler.sample()
    assert sampled.owned_pids == (40, 50)
    assert sampled.observed_thread_count == 3
    assert sampled.own_cpu_cores == 2
    assert sampled.external_cpu_cores == 0
    assert sampled.owned_materialized_bytes == 40 * 1024
    assert sampled.process_memory_bytes[(50, 10050)] == 32 * 1024


def test_reaped_child_lifetime_is_not_counted_twice(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.process(threads={40: [50]})
    proc.process(50, parent=40, start=30, cpu=50)
    sampler = proc.sampler()
    sampler.sample()
    proc.process(cpu=25, reaped=70)
    shutil.rmtree(proc.root / "50")
    proc.advance(1, [45, 0, 0, 0])
    sampled = sampler.sample()
    assert sampled.own_cpu_cores == pytest.approx(0.45)
    assert sampled.external_cpu_cores == 0


def test_known_child_is_retained_after_reparenting_but_pid_reuse_is_rejected(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.process(threads={40: [50]})
    proc.process(50, parent=40, start=30)
    sampler = proc.sampler()
    sampler.sample()
    proc.process()
    proc.process(50, parent=1, start=30, cpu=100)
    proc.advance(1, [100, 0, 0, 0])
    retained = sampler.sample()
    assert retained.own_cpu_cores == 1
    assert (50, 30) in retained.process_identities
    proc.process(50, parent=1, start=60, private=65536)
    proc.advance(1, [0, 0, 0, 0])
    reused = sampler.sample()
    assert (50, 60) not in reused.process_identities
    assert (50, 30) not in reused.process_memory_bytes
    assert not reused.owned_memory_complete


def test_missing_private_memory_never_falls_back_to_shared_rss(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    (proc.root / "40" / "smaps_rollup").unlink()
    sampled = proc.sampler().sample()
    assert sampled.owned_materialized_bytes is None
    assert not sampled.owned_memory_complete
    assert sampled.process_memory_bytes == {}


def test_snapshot_reuses_all_reads_and_immutable_identity_map(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    sampler = proc.sampler(sample_interval_seconds=60)
    sampled = sampler.sample()
    def forbidden(*_args, **_kwargs):
        pytest.fail("cached sampling performed a filesystem read")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    for _ in range(100):
        assert sampler.sample() is sampled
    with pytest.raises(TypeError):
        sampled.process_memory_bytes[(40, 10)] = 0


def test_slow_sample_still_waits_an_interval_before_repeating(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    sampler = proc.sampler(sample_interval_seconds=0.25)
    original = sampler._sample
    def slow(now):
        result = original(now)
        proc.now += 1
        return result
    monkeypatch.setattr(sampler, "_sample", slow)
    sampled = sampler.sample()
    assert sampler.sample() is sampled


def test_incomplete_tree_is_unknown_instead_of_zero_external_load(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    sampler = proc.sampler()
    sampler.sample()
    (proc.root / "40" / "task" / "40" / "children").unlink()
    proc.advance(1, [100, 100, 100, 100])
    sampled = sampler.sample()
    assert sampled.external_cpu_cores is None
    assert not sampled.cpu_observation_complete
    assert sampled.owned_materialized_bytes is None


def test_unreadable_shared_quota_usage_grants_no_cpu_credit(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.quota(2, 0)
    sampler = proc.sampler()
    sampler.sample()
    (proc.leaf / "cpu.stat").unlink()
    proc.advance(1, [0, 0, 0, 0])
    sampled = sampler.sample()
    assert sampled.external_cpu_cores is None
    assert not sampled.cpu_observation_complete


def test_process_moved_outside_owned_cgroup_loses_cpu_attribution(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.process(threads={40: [50]})
    proc.process(50, parent=40)
    sampler = proc.sampler()
    sampler.sample()
    (proc.root / "50" / "cgroup").write_text("0::/somewhere-else\n")
    proc.advance(1, [0, 0, 0, 0])
    sampled = sampler.sample()
    assert sampled.external_cpu_cores is None
    assert (50, 10) not in sampled.process_memory_bytes
    assert sampled.owned_materialized_bytes is None


def test_pressure_observes_host_and_ancestors_with_malformed_values_unknown(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    pressure = proc.root / "pressure"
    pressure.mkdir()
    (pressure / "memory").write_text("some avg10=2.5 total=100\nfull avg10=nan total=-1\n")
    (proc.leaf / "memory.pressure").write_text("some avg10=5.5 total=50\n")
    (proc.cgroup / "io.pressure").write_text("some avg10=9.0 total=250\n")
    sampled = proc.sampler().sample()
    assert sampled.memory_pressure_some_percent == 5.5
    assert sampled.memory_pressure_some_total_us == 100
    assert sampled.memory_pressure_full_percent is None
    assert sampled.memory_pressure_full_total_us is None
    assert sampled.io_pressure_some_percent == 9


def test_bounded_process_traversal_reports_incomplete_coverage(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch)
    proc.process(threads={40: [50]})
    proc.process(50, parent=40)
    sampled = proc.sampler(max_processes=1).sample()
    assert sampled.owned_pids == (40,)
    assert sampled.external_cpu_cores is None
    assert not sampled.owned_memory_complete


def test_host_guest_ticks_are_not_double_counted(tmp_path, monkeypatch):
    path = tmp_path / "stat"
    path.write_text("cpu 100 10 20 70 0 0 0 0 40 5\n")
    assert resource_sampler._host_cpu_counters(path)[-1].total == 200
    monkeypatch.setattr(cpu_runtime, "Path", lambda _path: path)
    assert cpu_runtime._proc_cpu_times().total == 200


def test_cpu_capacity_updates_upward_after_quota_change(tmp_path, monkeypatch):
    proc = ProcFixture(tmp_path, monkeypatch, cpus=16)
    proc.quota(2, 0)
    sampler = proc.sampler()
    assert sampler.sample().effective_cpu_capacity == 2
    proc.quota(12, 0)
    proc.advance(1, [0] * 16)
    assert sampler.sample().effective_cpu_capacity == 12
