"""Fresh exclusion samples must never turn into complete equality proofs."""

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner, PARTIAL_ALGORITHM, snapshot_path


def test_different_fresh_samples_read_no_full_content_or_publish_full_evidence(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(3):
        (root / str(i)).write_bytes(bytes([65 + i]) * (9 * 1024 * 1024))
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        scan = index.scan(root, excluded_paths=())
        observation = index.observe_fingerprint(snapshot_path(root / "0"), PARTIAL_ALGORITHM)
        assert observation is not None and observation.full_digest is None
        assert observation.partial_reads == 1 and observation.full_reads == 0
        for _warm in (False, True):
            plan = DedupPlanner(index).plan(scan.scan_id)
            assert plan.group_count == 0
            assert plan.statistics.full_hash_files == 0
            assert plan.statistics.exact_compare_files == 0
            assert plan.statistics.partial_hash_files == 3
            assert plan.statistics.hash_read_bytes == 9 * 256 * 1024
            assert index._connection.execute(
                "SELECT COUNT(*) FROM planning_full_observations WHERE full_digest IS NOT NULL"
            ).fetchone()[0] == 0
            assert index._connection.execute(
                "SELECT COUNT(*) FROM fingerprint_content_evidence"
            ).fetchone()[0] == 0


def test_equal_samples_but_different_unobserved_middle_bytes_require_full_hash(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    content = b"x" * (3 * 1024 * 1024)
    (root / "a").write_bytes(content)
    changed = bytearray(content)
    changed[512 * 1024] = ord("y")  # Outside first/middle/last 256 KiB regions.
    (root / "b").write_bytes(changed)
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index, partial_threshold=0).plan(scan.scan_id)
        assert plan.group_count == 0
        assert plan.statistics.partial_hash_files == 2
        assert plan.statistics.full_hash_files == 2
        assert plan.statistics.hash_read_bytes == 2 * len(content) + 6 * 256 * 1024
        assert plan.statistics.exact_compare_files == 0
