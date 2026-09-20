"""Regression coverage for the size-to-full-SHA-256 pipeline."""

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner


def test_different_same_size_files_are_hashed_once_and_not_grouped(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"a" * (3 * 1024 * 1024))
    (root / "b").write_bytes(b"b" * (3 * 1024 * 1024))
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 0
    assert plan.statistics.partial_hash_files == 0
    assert plan.statistics.full_hash_files == 2
    assert plan.statistics.hash_read_bytes == 2 * 3 * 1024 * 1024


def test_equal_full_sha_candidates_still_require_byte_comparison(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = b"x" * (3 * 1024 * 1024)
    (root / "a").write_bytes(payload)
    (root / "b").write_bytes(payload)
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)

    assert plan.group_count == 1
    assert plan.statistics.full_hash_files == 2
    assert plan.statistics.exact_compare_files == 1
    assert plan.groups[0].proof is not None
    assert plan.groups[0].proof.comparison_method == "byte_for_byte"
