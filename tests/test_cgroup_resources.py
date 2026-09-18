"""Deterministic resource probes and real admission against contained fixtures."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from neocortex.runtime.control import cgroup_runtime, cpu_runtime, memory_runtime
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
)


GIB = 1024**3


def _files(root: Path, **values: str | int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (root / name.replace("_", ".")).write_text(str(value) + "\n", encoding="ascii")
    return root


def _memory_stat(root: Path, **overrides: int) -> None:
    values = {
        "file": 800, "shmem": 100, "inactive_file": 500,
        "file_dirty": 30, "file_writeback": 20, "file_mapped": 40,
        "unevictable": 10, "active_file": 200, "slab_reclaimable": 100,
    }
    values.update(overrides)
    (root / "memory.stat").write_text(
        "\n".join(f"{key} {value}" for key, value in values.items()) + "\n",
        encoding="ascii",
    )


def _proc_fixture(tmp_path: Path, membership: str, mount_root: str = "/"):
    mount = tmp_path / "unified"
    mount.mkdir(exist_ok=True)
    cgroup = tmp_path / "proc-cgroup"
    cgroup.write_text(f"5:memory:/legacy\n0::{membership}\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    escaped = str(mount).replace(" ", r"\040")
    mountinfo.write_text(
        f"31 22 0:28 {mount_root} {escaped} ro,nosuid - cgroup2 cgroup rw\n",
        encoding="utf-8",
    )
    return mount, cgroup, mountinfo


def test_mount_membership_collects_parents_through_mount_root(tmp_path):
    mount, membership, mountinfo = _proc_fixture(tmp_path, "/team/job")
    leaf = _files(mount / "team" / "job")

    assert cgroup_runtime.cgroup_v2_directories(membership, mountinfo) == (
        leaf,
        leaf.parent,
        mount,
    )


def test_subtree_bind_mount_does_not_repeat_its_root_or_escape(tmp_path):
    mount, membership, mountinfo = _proc_fixture(tmp_path, "/team/job", "/team")
    leaf = _files(mount / "job")
    _files(tmp_path, memory_max=1, memory_current=1)

    paths = cgroup_runtime.cgroup_v2_directories(membership, mountinfo)

    assert paths == (leaf, mount)
    assert cgroup_runtime.cgroup_memory_snapshot(paths).limit_bytes is None


def test_namespaced_membership_root_reads_limits_at_visible_mount(tmp_path):
    mount, membership, mountinfo = _proc_fixture(tmp_path, "/")
    _files(mount, memory_max=4 * GIB, memory_current=3 * GIB)

    paths = cgroup_runtime.cgroup_v2_directories(membership, mountinfo)
    snapshot = cgroup_runtime.cgroup_memory_snapshot(paths)

    assert paths == (mount,)
    assert snapshot.limit_bytes == 4 * GIB
    assert snapshot.available_bytes == GIB


def test_multiple_views_keep_accessible_ancestors_of_subtree_mount(tmp_path):
    broad, membership, mountinfo = _proc_fixture(tmp_path, "/team/job")
    leaf = _files(broad / "team" / "job")
    narrow = _files(tmp_path / "subtree")
    _files(narrow / "job")
    _files(broad / "team", memory_max=4 * GIB, memory_current=3 * GIB)
    mountinfo.write_text(
        mountinfo.read_text(encoding="utf-8")
        + f"32 22 0:28 /team {narrow} ro - cgroup2 cgroup rw\n",
        encoding="utf-8",
    )

    paths = cgroup_runtime.cgroup_v2_directories(membership, mountinfo)

    assert leaf in paths and leaf.parent in paths and broad in paths
    assert narrow in paths
    assert cgroup_runtime.cgroup_memory_snapshot(paths).available_bytes == GIB


def test_mount_escaping_and_unicode_membership_are_not_lost(tmp_path):
    parent = tmp_path / "with space"
    parent.mkdir()
    mount, membership, mountinfo = _proc_fixture(parent, "/équipe/niño")
    leaf = _files(mount / "équipe" / "niño")

    assert cgroup_runtime.cgroup_v2_directories(membership, mountinfo) == (
        leaf,
        leaf.parent,
        mount,
    )


@pytest.mark.parametrize("membership,root", [("/other", "/team"), ("/../other", "/")])
def test_unmappable_membership_is_not_guessed(tmp_path, membership, root):
    mount, membership_path, mountinfo = _proc_fixture(tmp_path, membership, root)
    _files(mount, memory_max=4 * GIB, memory_current=GIB)

    assert cgroup_runtime.cgroup_v2_directories(membership_path, mountinfo) == ()


def test_cgroup_v1_only_and_absent_proc_fall_back(tmp_path):
    _mount, membership, mountinfo = _proc_fixture(tmp_path, "/")
    membership.write_text("5:memory:/legacy\n", encoding="ascii")

    assert cgroup_runtime.cgroup_v2_directories(membership, mountinfo) == ()
    assert cgroup_runtime.cgroup_v2_directories(tmp_path / "missing", mountinfo) == ()


def test_each_ancestor_uses_its_own_current_including_siblings(tmp_path):
    parent = _files(tmp_path / "parent", memory_max=4 * GIB, memory_current=7 * GIB // 2)
    child = _files(parent / "child", memory_max=3 * GIB, memory_current=GIB)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((child, parent))

    assert snapshot.limit_bytes == 3 * GIB
    assert snapshot.available_bytes == GIB // 2
    assert snapshot.visible_cgroups == 2
    assert snapshot.unreadable_current == ()


@pytest.mark.parametrize("maximum,current", [(0, 0), (100, 101)])
def test_zero_limit_and_temporary_over_limit_have_no_headroom(tmp_path, maximum, current):
    root = _files(tmp_path, memory_max=maximum, memory_current=current)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.limit_bytes == maximum
    assert snapshot.available_bytes == 0


@pytest.mark.parametrize("current", [None, "garbage", "-1"])
def test_known_limit_with_unreadable_use_is_not_reported_as_free(tmp_path, current):
    root = _files(tmp_path, memory_max=4 * GIB)
    if current is not None:
        _files(root, memory_current=current)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.limit_bytes == 4 * GIB
    assert snapshot.available_bytes == 0
    assert snapshot.unreadable_current == (root / "memory.current",)


@pytest.mark.parametrize("maximum", ["max", "garbage", "-5", "1.5", ""])
def test_unlimited_or_malformed_limit_does_not_invent_capacity(tmp_path, maximum):
    root = _files(tmp_path, memory_max=maximum, memory_current=100)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.limit_bytes is None
    assert snapshot.available_bytes is None


@pytest.mark.parametrize("maximum", ["max", 4 * GIB])
def test_memory_high_reduces_headroom_without_becoming_physical_capacity(
    tmp_path,
    monkeypatch,
    maximum,
):
    root = _files(tmp_path, memory_max=maximum, memory_high=2 * GIB, memory_current=2 * GIB)
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(
        memory_runtime,
        "_host_physical_memory_snapshot",
        lambda *args, **kwargs: (6 * GIB, 5 * GIB),
    )

    snapshot = cgroup_runtime.cgroup_memory_snapshot()
    physical = memory_runtime.memory_snapshot()

    assert snapshot.high_bytes == 2 * GIB
    assert snapshot.limit_bytes == (None if maximum == "max" else maximum)
    assert snapshot.available_bytes == physical.available_physical == 0
    assert physical.total_physical == (6 * GIB if maximum == "max" else maximum)


def test_ancestor_memory_high_includes_sibling_pressure(tmp_path):
    parent = _files(tmp_path, memory_max="max", memory_high=3 * GIB, memory_current=3 * GIB)
    child = _files(parent / "job", memory_max=2 * GIB, memory_high="max", memory_current=GIB)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((child, parent))

    assert snapshot.limit_bytes == 2 * GIB
    assert snapshot.high_bytes == 3 * GIB
    assert snapshot.available_bytes == 0


def test_clean_inactive_file_cache_restores_only_conservative_headroom(tmp_path, monkeypatch):
    root = _files(tmp_path, memory_max=1000, memory_current=900)
    _memory_stat(root)
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(
        memory_runtime, "_host_physical_memory_snapshot", lambda *args, **kwargs: (2000, 2000),
    )

    snapshot = cgroup_runtime.cgroup_memory_snapshot()

    assert snapshot.raw_available_bytes == 100
    assert snapshot.estimated_reclaimable_file_bytes == 400
    assert snapshot.available_bytes == 500
    # Exercise the same gate used by routes: the existing reserve is maintained.
    gate = memory_runtime.WeightedMemoryGate(
        memory_runtime.MemoryResourceLimits(
            memory_budget_bytes=300,
            min_free_memory_bytes=200,
            min_free_commit_bytes=0,
            wait_timeout_seconds=0,
        )
    )
    with gate.admit(250):
        assert gate._reserved == 250
    with pytest.raises(memory_runtime.MemoryBudgetExceeded):
        with gate.admit(301):
            pytest.fail("the configured budget must still constrain admission")
    _files(root, memory_current=951)
    with pytest.raises(memory_runtime.MemoryHeadroomTimeout):
        with gate.admit(250):
            pytest.fail("the configured free-memory floor must still constrain admission")
    assert gate._reserved == 0


@pytest.mark.parametrize("excluded", ["file_dirty", "file_writeback", "file_mapped", "unevictable"])
def test_dirty_mapped_writeback_or_unevictable_file_is_not_added(tmp_path, excluded):
    root = _files(tmp_path, memory_max=1000, memory_current=900)
    _memory_stat(root, **{excluded: 500})

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.available_bytes == snapshot.raw_available_bytes == 100
    assert snapshot.estimated_reclaimable_file_bytes == 0


@pytest.mark.parametrize("overrides", [
    {"inactive_file": 0, "active_file": 700, "slab_reclaimable": 800},
    {"file": 800, "shmem": 800, "inactive_file": 0},
    {"file": 800, "shmem": 700, "inactive_file": 500},
    {"file": 950},
    {"file_dirty": 801},
    {"unevictable": 901},
])
def test_other_cache_or_contradictory_counters_never_add_capacity(tmp_path, overrides):
    root = _files(tmp_path, memory_max=1000, memory_current=900)
    _memory_stat(root, **overrides)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.available_bytes == snapshot.raw_available_bytes == 100
    assert snapshot.estimated_reclaimable_file_bytes == 0


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "negative", "malformed", "non_ascii"])
def test_incomplete_or_malformed_memory_stat_falls_back_to_raw_margin(tmp_path, mutation):
    root = _files(tmp_path, memory_max=1000, memory_current=900)
    _memory_stat(root)
    path = root / "memory.stat"
    original = path.read_text(encoding="ascii")
    changed = {
        "missing": original.replace("file_mapped 40\n", ""),
        "duplicate": original + "inactive_file 500\n",
        "negative": original.replace("file_mapped 40", "file_mapped -1"),
        "malformed": original.replace("file_mapped 40", "file_mapped 40 40"),
        "non_ascii": original + "counter é\n",
    }[mutation]
    path.write_text(changed, encoding="utf-8")

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.available_bytes == snapshot.raw_available_bytes == 100
    assert snapshot.estimated_reclaimable_file_bytes == 0


@pytest.mark.parametrize("high,expected", [(950, 50), (900, 0), (850, 0)])
def test_memory_high_remains_a_raw_pressure_boundary_with_reclaimable_file(
    tmp_path, high, expected,
):
    root = _files(tmp_path, memory_max=1000, memory_high=high, memory_current=900)
    _memory_stat(root)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.limit_bytes == 1000
    assert snapshot.high_bytes == high
    assert snapshot.available_bytes == snapshot.raw_available_bytes == expected
    assert snapshot.estimated_reclaimable_file_bytes == 0


def test_parent_margin_still_constrains_child_reclaim_estimate(tmp_path):
    parent = _files(tmp_path, memory_max=1100, memory_current=900)
    child = _files(parent / "job", memory_max=1000, memory_current=900)
    _memory_stat(child)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((child, parent))

    assert snapshot.raw_available_bytes == 100
    assert snapshot.available_bytes == 200
    assert snapshot.estimated_reclaimable_file_bytes == 100


@pytest.mark.parametrize("recaptured,expected", [("950", 450), ("800", 500), (None, 0)])
def test_reclaim_estimate_recaptures_current_and_never_uses_a_failed_read(
    tmp_path, monkeypatch, recaptured, expected,
):
    root = _files(tmp_path, memory_max=1000, memory_current=900)
    _memory_stat(root)
    original_read = cgroup_runtime._read_ascii
    current_reads = iter(("900", recaptured))

    def read(path):
        return next(current_reads) if path.name == "memory.current" else original_read(path)

    monkeypatch.setattr(cgroup_runtime, "_read_ascii", read)

    snapshot = cgroup_runtime.cgroup_memory_snapshot((root,))

    assert snapshot.available_bytes == expected
    assert snapshot.unreadable_current == ((root / "memory.current",) if recaptured is None else ())


@pytest.mark.parametrize("host_available,expected", [(5 * GIB, GIB), (GIB // 2, GIB // 2)])
def test_posix_snapshot_combines_host_pressure_with_cgroup_margin(
    tmp_path,
    monkeypatch,
    host_available,
    expected,
):
    root = _files(tmp_path / "group", memory_max=4 * GIB, memory_current=3 * GIB)
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(
        memory_runtime,
        "_host_physical_memory_snapshot",
        lambda *args, **kwargs: (6 * GIB, host_available),
    )

    snapshot = memory_runtime.memory_snapshot()

    assert snapshot.total_physical == 4 * GIB
    assert snapshot.available_physical == expected
    # Fresh reads matter when another process consumes this shared cgroup.
    _files(root, memory_current=4 * GIB)
    assert memory_runtime.memory_snapshot().available_physical == 0


def test_cgroup_capacity_still_works_when_host_probe_is_unavailable(tmp_path, monkeypatch):
    root = _files(tmp_path, memory_max=2 * GIB, memory_current=GIB)
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(
        memory_runtime,
        "_host_physical_memory_snapshot",
        lambda *args, **kwargs: (None, None),
    )

    assert memory_runtime.posix_physical_memory_snapshot() == (2 * GIB, GIB)


@pytest.mark.parametrize("maximum,high", [(1000, "max"), (2000, 1000)])
def test_real_memory_gate_blocks_then_retries_after_cgroup_headroom_recovers(
    tmp_path,
    monkeypatch,
    maximum,
    high,
):
    root = _files(tmp_path, memory_max=maximum, memory_high=high, memory_current=950)
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    gate = memory_runtime.WeightedMemoryGate(
        memory_runtime.MemoryResourceLimits(
            memory_budget_bytes=100,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            wait_timeout_seconds=0,
        )
    )

    with pytest.raises(memory_runtime.MemoryHeadroomTimeout, match="fisica=50"):
        with gate.admit(60):
            pytest.fail("the cgroup margin must block this reservation")
    assert gate._reserved == 0
    _files(root, memory_current=900)
    with gate.admit(60):
        assert gate._reserved == 60
    assert gate._reserved == 0


def test_cpu_quota_and_cpuset_intersect_all_visible_ancestors(tmp_path):
    parent = _files(tmp_path, cpu_max="150000 100000", cpuset_cpus_effective="0-3,7")
    child = _files(parent / "child", cpu_max="400000 100000", cpuset_cpus_effective="2-5")

    snapshot = cgroup_runtime.cgroup_cpu_snapshot((child, parent))

    assert snapshot.quota_cpus == Fraction(3, 2)
    assert snapshot.cpuset_ranges == ((2, 3),)


@pytest.mark.parametrize("maximum", ["max 100000", "bad", "0 100000", "100000 0", "-1 100000"])
def test_absent_unlimited_and_malformed_cpu_quota_preserve_other_limits(tmp_path, maximum):
    parent = _files(tmp_path, cpu_max="200000 100000")
    child = _files(parent / "child", cpu_max=maximum)

    assert cgroup_runtime.cgroup_cpu_snapshot((child, parent)).quota_cpus == 2
    assert cgroup_runtime.cgroup_cpu_snapshot(()).quota_cpus is None


@pytest.mark.parametrize(
    "quota,affinity,expected",
    [
        ("400000 100000", {0, 1, 2, 3, 4}, 4),
        ("150000 100000", {0, 1, 2}, 1.5),
        ("50000 100000", {0, 1}, 0.5),
        ("400000 100000", {2}, 1),
    ],
)
def test_effective_cpu_retains_fractional_quota_and_obeys_affinity(
    tmp_path,
    monkeypatch,
    quota,
    affinity,
    expected,
):
    root = _files(tmp_path, cpu_max=quota, cpuset_cpus_effective="0-15")
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: 5)
    monkeypatch.setattr(cpu_runtime.os, "sched_getaffinity", lambda pid: affinity)

    snapshot = cpu_runtime.cpu_capacity_snapshot()

    assert snapshot.system_cpu_count == 5
    assert snapshot.affinity_cpu_count == len(affinity)
    assert snapshot.effective_cpus == expected
    assert cpu_runtime.effective_cpu_count() == max(1, int(expected))


def test_cpuset_and_affinity_use_intersection_not_just_smallest_count(tmp_path, monkeypatch):
    root = _files(tmp_path, cpu_max="max 100000", cpuset_cpus_effective="2-5")
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(cpu_runtime.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3})

    snapshot = cpu_runtime.cpu_capacity_snapshot()

    assert snapshot.cgroup_cpuset_count == snapshot.affinity_cpu_count == 4
    assert snapshot.effective_cpus == 2


def test_cpu_falls_back_to_known_host_capacity_when_probes_are_unavailable(monkeypatch):
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: ())
    monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: 3)
    monkeypatch.delattr(cpu_runtime.os, "sched_getaffinity")

    assert cpu_runtime.effective_cpu_count() == 3
    monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: None)
    assert cpu_runtime.effective_cpu_count() == 1


def test_large_cpuset_ranges_are_not_expanded():
    assert cgroup_runtime._cpu_ranges("0-999999999,5-10,1000000000") == ((0, 1000000000),)
    assert cgroup_runtime._cpu_ranges("3-2") is None
    assert cgroup_runtime._cpu_ranges("0--1") is None


def test_global_default_coordination_uses_cgroup_scale_without_replacing_config(
    tmp_path,
    monkeypatch,
):
    root = _files(tmp_path, memory_max=4 * GIB, memory_current=GIB, cpu_max="400000 100000")
    monkeypatch.setattr(cgroup_runtime, "cgroup_v2_directories", lambda: (root,))
    monkeypatch.setattr(cpu_runtime.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(cpu_runtime.os, "sched_getaffinity", lambda pid: set(range(16)))
    monkeypatch.setattr(
        memory_runtime,
        "_host_physical_memory_snapshot",
        lambda *args, **kwargs: (6 * GIB, 5 * GIB),
    )
    coordinator = GlobalResourceCoordinator(
        ("text",),
        GlobalResourceLimits(),
        cpu_load_probe=lambda: 0.0,
    )

    with coordinator.admit("text", GIB // 4):
        pass
    summary = coordinator.summary()
    assert summary.cpu_slots == 3  # Existing policy leaves one usable CPU aside.
    assert summary.memory_budget_bytes == 3 * GIB // 2
    assert summary.min_observed_available_memory_bytes == 3 * GIB
    explicit = GlobalResourceCoordinator(
        ("text",),
        GlobalResourceLimits(cpu_slots=2),
        cpu_load_probe=lambda: 0.0,
    )
    assert explicit.cpu_slots == 2
