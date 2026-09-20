"""Real content reads, cache replay and adversarial in-run digest reuse."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner, FileChangedError, snapshot_path
from neocortex.deduplication import fingerprinting


@contextmanager
def _measured_reads():
    counters = {"bytes": 0, "full_reads": 0}
    open_stream = fingerprinting._open_regular_stream
    full = fingerprinting.full_fingerprint

    class MeasuredStream:
        def __init__(self, source):
            self.source = source

        def read(self, *args):
            result = self.source.read(*args)
            counters["bytes"] += len(result)
            return result

        def readinto(self, *args):
            result = self.source.readinto(*args)
            counters["bytes"] += result or 0
            return result

        def __getattr__(self, name):
            return getattr(self.source, name)

    @contextmanager
    def measured_stream(snapshot):
        with open_stream(snapshot) as source:
            yield MeasuredStream(source)

    def measured_full(snapshot, **kwargs):
        counters["full_reads"] += 1
        return full(snapshot, **kwargs)

    with patch.object(fingerprinting, "_open_regular_stream", measured_stream), \
            patch.object(fingerprinting, "full_fingerprint", measured_full):
        yield counters


def test_repeated_size_candidates_read_complete_sha256_cold_and_warm(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    size = 9 * 1024 * 1024
    for name, byte in (("a", b"a"), ("b", b"a"), ("c", b"c")):
        (root / name).write_bytes(byte * size)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        for warm in (False, True):
            scan = index.scan(root, excluded_paths=())
            with _measured_reads() as measured:
                plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False)
            assert plan.group_count == 1
            assert measured["full_reads"] == plan.statistics.full_hash_files == 3
            assert measured["bytes"] == plan.statistics.hash_read_bytes
            assert plan.statistics.full_digest_reuses == 0
            assert plan.statistics.partial_hash_files == 0
            assert plan.statistics.cache_validation_reads == (3 if warm else 0)
            assert plan.statistics.cache_validation_bytes == (3 * size if warm else 0)
            assert measured["bytes"] == 3 * size
            if warm:
                assert plan.statistics.fingerprint_cache_hits == 3


def test_same_stat_cache_miss_retains_the_new_full_digest(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"before")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        assert DedupPlanner(index).plan(scan.scan_id).group_count == 1
        recorded = snapshot_path(root / "a")
        (root / "a").write_bytes(b"after!")
        os.utime(root / "a", ns=(recorded.mtime_ns, recorded.mtime_ns))
        assert snapshot_path(root / "a") == recorded
        with _measured_reads() as measured:
            plan = DedupPlanner(index).plan(scan.scan_id, exact_compare=False)
    assert plan.group_count == 0
    assert measured["full_reads"] == plan.statistics.full_hash_files == 2
    assert measured["bytes"] == plan.statistics.hash_read_bytes == 12
    assert plan.statistics.cache_validation_reads == 2
    assert plan.statistics.fingerprint_cache_hits == 1


def test_exact_comparison_bytes_are_separate_from_hash_validation(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"same content")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with _measured_reads() as measured:
            plan = DedupPlanner(index).plan(scan.scan_id)
    assert plan.statistics.exact_compare_files == 1
    assert plan.statistics.exact_comparison_bytes == 24
    assert measured["bytes"] == plan.statistics.hash_read_bytes + plan.statistics.exact_comparison_bytes


def test_warm_full_validation_reports_computed_proof_without_rewriting_cache(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"same content")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        DedupPlanner(index).plan(scan.scan_id)
        statements = []
        index._connection.set_trace_callback(statements.append)
        with _measured_reads() as measured:
            replay = DedupPlanner(index).plan(scan.scan_id, preview_limit=1)
        index._connection.set_trace_callback(None)
    assert all(proof.fingerprint_source == "computed" for proof in replay.groups[0].member_proofs)
    assert replay.statistics.full_hash_files == replay.statistics.fingerprint_cache_hits == 2
    assert replay.statistics.hash_read_bytes == 24
    assert measured["bytes"] == 48
    assert not any("INSERT OR REPLACE INTO fingerprints" in statement for statement in statements)


def test_same_stat_rewrite_during_full_hash_abstains(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"identical")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        planner = DedupPlanner(index)
        original = planner._fingerprint
        changed = False

        def replace_after_hash(snapshot):
            nonlocal changed
            observation = original(snapshot)
            if not changed:
                changed = True
                Path(snapshot.path).write_bytes(b"different")
                os.utime(snapshot.path, ns=(snapshot.mtime_ns, snapshot.mtime_ns))
            return observation

        with patch.object(planner, "_fingerprint", side_effect=replace_after_hash):
            plan = planner.plan(scan.scan_id)
    assert plan.group_count == 0
    assert plan.coverage == "partial"
    assert plan.statistics.changed_or_unreadable_files == 1


def test_exact_comparison_rejects_restored_mtime_during_read(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.write_bytes(b"same")
    right.write_bytes(b"same")
    snapshots = snapshot_path(left), snapshot_path(right)
    rewritten = False

    def mutate(_count: int) -> None:
        nonlocal rewritten
        if not rewritten:
            rewritten = True
            right.write_bytes(b"diff")
            os.utime(right, ns=(snapshots[1].mtime_ns, snapshots[1].mtime_ns))

    with pytest.raises(FileChangedError):
        fingerprinting.files_equal_exact(*snapshots, read_observer=mutate)


def test_failed_exact_comparison_retains_actual_read_metrics(tmp_path: Path) -> None:
    from neocortex.deduplication.planning import planner as planner_module

    root = tmp_path / "corpus"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_bytes(b"same")
    original = fingerprinting.files_equal_exact

    def changed_comparison(left, right, *, read_observer, checkpoint=None):
        changed = False

        def observe(count):
            nonlocal changed
            read_observer(count)
            if count and not changed:
                changed = True
                Path(right.path).write_bytes(b"diff")
                os.utime(right.path, ns=(right.mtime_ns, right.mtime_ns))

        return original(left, right, read_observer=observe, checkpoint=checkpoint)

    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with _measured_reads() as measured, patch.object(planner_module, "files_equal_exact", changed_comparison):
            plan = DedupPlanner(index).plan(scan.scan_id)
    assert plan.coverage == "partial" and plan.group_count == 0
    assert plan.statistics.exact_comparison_bytes == 8
    assert measured["bytes"] == plan.statistics.hash_read_bytes + plan.statistics.exact_comparison_bytes == 16
