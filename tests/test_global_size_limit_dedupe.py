"""Global run-scoped size admission for duplicate planning."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.deduplication.fingerprinting import snapshot_path as real_snapshot_path
from neocortex.deduplication.planning import planner as planner_module


def _duplicate_pair(root: Path, stem: str, payload: bytes) -> tuple[Path, Path]:
    first = root / f"{stem}-a.bin"
    second = root / f"{stem}-b.bin"
    first.write_bytes(payload)
    second.write_bytes(payload)
    return first, second


def test_inventory_keeps_oversize_rows_but_dedup_does_not_observe_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    small = _duplicate_pair(root, "small", b"small")
    large = _duplicate_pair(root, "large", b"L" * 11)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        inventory = tuple(index.snapshots(scan.scan_id))
        assert {item.path for item in inventory} == {str(path) for path in (*small, *large)}

        captured: list[str] = []

        def capture(path: str | Path):
            captured.append(str(path))
            return real_snapshot_path(path)

        with patch.object(planner_module, "snapshot_path", side_effect=capture):
            plan = DedupPlanner(index).plan(
                scan.scan_id, max_file_bytes=10, preview_limit=None,
            )

        assert plan.group_count == 1
        assert plan.statistics.inventory_files == 4
        assert plan.statistics.size_candidate_files == 2
        assert plan.statistics.full_hash_files == 2
        assert all(path not in captured for path in map(str, large))
        assert {Path(group.keep.path).name for group in plan.groups} == {"small-a.bin"}
        assert {Path(item.path).name for group in plan.groups for item in group.redundant} == {
            "small-b.bin",
        }
        assert list(index.size_collision_sizes(scan.scan_id, max_file_bytes=10)) == [(5, 2)]

    with sqlite3.connect(tmp_path / "inventory.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM fingerprints WHERE size > 10"
        ).fetchone() == (0,)


def test_global_limit_is_inclusive_and_oversize_duplicate_never_forms_a_group(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _duplicate_pair(root, "boundary", b"B" * 10)
    _duplicate_pair(root, "over", b"O" * 11)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id, max_file_bytes=10, preview_limit=None)

    assert plan.group_count == 1
    assert plan.groups[0].size == 10
    assert plan.statistics.size_candidate_files == 2


def test_changing_or_removing_the_limit_readmits_the_same_inventory_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _duplicate_pair(root, "small", b"s" * 5)
    _duplicate_pair(root, "large", b"l" * 11)

    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        limited = DedupPlanner(index).plan(scan.scan_id, max_file_bytes=10, preview_limit=None)
        expanded = DedupPlanner(index).plan(scan.scan_id, max_file_bytes=11, preview_limit=None)
        unlimited = DedupPlanner(index).plan(scan.scan_id, preview_limit=None)

    assert limited.group_count == 1
    assert expanded.group_count == unlimited.group_count == 2
    assert expanded.statistics.size_candidate_files == unlimited.statistics.size_candidate_files == 4


def test_size_limit_does_not_delete_existing_fingerprint_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _duplicate_pair(root, "small", b"s" * 5)
    _duplicate_pair(root, "large", b"l" * 11)
    database = tmp_path / "inventory.sqlite3"

    with DedupIndex(database) as index:
        scan = index.scan(root, excluded_paths=())
        DedupPlanner(index).plan(scan.scan_id, preview_limit=None)
        with sqlite3.connect(database) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE size=11"
            ).fetchone()[0]
        assert before == 2

        limited = DedupPlanner(index).plan(
            scan.scan_id, max_file_bytes=10, preview_limit=None,
        )
        assert limited.group_count == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM fingerprints WHERE size=11"
        ).fetchone() == (2,)


@pytest.mark.parametrize("invalid", (0, -1, True, 1.5, "10"))
def test_dedup_rejects_invalid_size_limits(tmp_path: Path, invalid: object) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _duplicate_pair(root, "item", b"same")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        with pytest.raises(ValueError, match="max_file_bytes"):
            DedupPlanner(index).plan(scan.scan_id, max_file_bytes=invalid)
