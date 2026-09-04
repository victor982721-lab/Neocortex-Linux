"""Durable, deterministic inventory checkpoint and replay regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.deduplication import (
    DedupIndex,
    InventoryScanBudgetExceeded,
    InventoryScanCancelled,
    InventoryWorkBudget,
)
from neocortex.deduplication.inventory.resume import (
    InventoryResumeCheckpointStore,
    InventoryResumeConflictError,
)
from neocortex.deduplication.inventory.policy import InventoryExclusionPolicy


def _snapshot_key(index: DedupIndex, scan_id: int, root: Path) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            Path(snapshot.path).relative_to(root).as_posix(),
            snapshot.file_id,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
        )
        for snapshot in index.snapshots(scan_id)
    )


def _fixture(root: Path) -> None:
    (root / "alpha" / "deep").mkdir(parents=True)
    (root / "beta").mkdir()
    for path, payload in (
        (root / "alpha" / "first.bin", b"first"),
        (root / "alpha" / "deep" / "third.bin", b"third"),
        (root / "beta" / "second.bin", b"second"),
        (root / "root.bin", b"root"),
    ):
        path.write_bytes(payload)


def test_inventory_resume_matches_clean_deterministic_scan_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _fixture(root)
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanBudgetExceeded, match="budget exhausted"):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=2,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=2),
            )
        partial = InventoryResumeCheckpointStore(checkpoint, root=root).read()
        assert partial.status == "partial"
        assert partial.last_relative_cursor is not None
        assert partial.batch_index == 1

        resumed = index.scan(
            root,
            excluded_paths=(),
            batch_size=2,
            checkpoint_path=checkpoint,
            resume=True,
            work_budget=InventoryWorkBudget(max_files=10),
        )
        first_rows = _snapshot_key(index, resumed.scan_id, root)
        replayed = index.scan(
            root,
            excluded_paths=(),
            batch_size=2,
            checkpoint_path=checkpoint,
            resume=True,
            work_budget=InventoryWorkBudget(max_files=10),
        )
        assert replayed == resumed
        assert _snapshot_key(index, replayed.scan_id, root) == first_rows
        final = InventoryResumeCheckpointStore(checkpoint, root=root).read()
        assert final.status == "complete"
        assert final.stop_reason is None
        assert final.files_seen == 4

    with DedupIndex(tmp_path / "clean.sqlite3") as clean:
        clean_scan = clean.scan(root, excluded_paths=(), deterministic=True, batch_size=2)
        assert _snapshot_key(clean, clean_scan.scan_id, root) == first_rows


def test_inventory_resume_with_no_committed_batch_restarts_from_empty_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "one.bin").write_bytes(b"one")
    (root / "two.bin").write_bytes(b"two")
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanCancelled):
            index.scan(
                root,
                excluded_paths=(),
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(
                    cancellation_check=lambda: True,
                ),
            )
        partial = InventoryResumeCheckpointStore(checkpoint, root=root).read()
        assert partial.last_relative_cursor is None
        assert partial.batch_files == 0
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone() == (0,)
        resumed = index.scan(
            root,
            excluded_paths=(),
            checkpoint_path=checkpoint,
            resume=True,
        )
        assert resumed.files_seen == 2
        assert InventoryResumeCheckpointStore(checkpoint, root=root).read().status == "complete"


def test_inventory_resume_uses_dfs_order_when_names_have_directory_prefixes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "z.bin").write_bytes(b"z")
    (root / "a.txt").write_bytes(b"a")
    (root / "b.txt").write_bytes(b"b")
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=1,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=1),
            )
        resumed = index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True)
        resumed_paths = {
            Path(snapshot.path).relative_to(root).as_posix() for snapshot in index.snapshots(resumed.scan_id)
        }
    assert resumed_paths == {"a/z.bin", "a.txt", "b.txt"}


def test_inventory_resume_rejects_prefix_drift_before_publishing_tail(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _fixture(root)
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=2,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=2),
            )
        partial = InventoryResumeCheckpointStore(checkpoint, root=root).read()
        assert partial.last_relative_cursor is not None
        first = root / partial.last_relative_cursor
        first.write_bytes(b"other")
        with pytest.raises(InventoryResumeConflictError, match="prefix"):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=2,
                checkpoint_path=checkpoint,
                resume=True,
                work_budget=InventoryWorkBudget(max_files=10),
            )
        scan_id = InventoryResumeCheckpointStore(checkpoint, root=root).read().scan_id
        assert index._connection.execute(
            "SELECT status FROM scans WHERE scan_id=?", (scan_id,)
        ).fetchone() == ("partial",)


def test_inventory_resume_rejects_policy_drift_without_touching_owner(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _fixture(root)
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=2,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=2),
            )
        before = index._connection.execute("SELECT COUNT(*) FROM files").fetchone()
        with pytest.raises(InventoryResumeConflictError, match="policy"):
            index.scan(
                root,
                exclusion_policy=InventoryExclusionPolicy.compile(file_names=("root.bin",)),
                checkpoint_path=checkpoint,
                resume=True,
                work_budget=InventoryWorkBudget(max_files=10),
            )
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone() == before


def test_inventory_resume_skips_ancestor_symlink_and_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    _fixture(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"secret")
    database = tmp_path / "resume.sqlite3"
    checkpoint = tmp_path / "state" / "inventory.json"

    with DedupIndex(database) as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=1,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=1),
            )
        original = root / "alpha"
        original.rename(root / "alpha.saved")
        os.symlink(outside, original, target_is_directory=True)
        with pytest.raises(InventoryResumeConflictError, match=r"cursor|prefix|root"):
            index.scan(
                root,
                excluded_paths=(),
                batch_size=1,
                checkpoint_path=checkpoint,
                resume=True,
                work_budget=InventoryWorkBudget(max_files=10),
            )
        assert not any(path.name == "secret.bin" for path in root.rglob("*"))
