"""Portable replay preserves publication and observes changes without full rewrites."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner, InventoryExclusionPolicy, snapshot_path
from neocortex.deduplication.inventory.portable import prepare_portable_inventory
from neocortex.deduplication.inventory.scanner import InventoryScanCancelled, InventoryScanDeadlineExceeded, InventoryWorkBudget
from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.integrations.inventory.inventory_coordinator import prepare_inventory


class _FrameworkEvents:
    def __init__(self):
        self.events = []

    def record_event(self, *args):
        self.events.append(args)

    def referenced_inventory_scan_ids(self):
        return ()

    def update_run_start_cursor(self, *_args):
        pass


def _prepare(index, root, *, policy=None):
    return prepare_inventory(index, _FrameworkEvents(), 1, root, None,
                             progress=lambda event: None,
                             exclusion_policy=policy or InventoryExclusionPolicy.compile(()),
                             allow_incremental=False, publish_portable_checkpoint=True)


def _corpus(tmp_path, count=20):
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(count):
        (root / f"{number:04d}").write_text(f"item {number}")
    return root


def test_portable_warm_reuses_generation_without_persistent_file_inserts(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 1000)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        first = _prepare(index, root)
        statements = []
        index._connection.set_trace_callback(statements.append)
        second = _prepare(index, root)
        index._connection.set_trace_callback(None)
        assert first.scan == second.scan
        assert second.reused_generation
        assert second.observed_files == 1000
        assert second.changed_files == second.persistent_file_rows_written == 0
        assert not any("INSERT INTO files(" in item for item in statements)
        assert not any("UPDATE scans SET files_seen=" in item for item in statements)
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_same_stat_rewrite_creates_successor_without_corrupting_history(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 0)
    for name in ("a", "b"):
        (root / name).write_bytes(b"same")
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        first = _prepare(index, root)
        assert DedupPlanner(index).plan(first.scan.scan_id).group_count == 1
        before = snapshot_path(root / "a")
        (root / "a").write_bytes(b"diff")
        os.utime(root / "a", ns=(before.mtime_ns, before.mtime_ns))
        assert snapshot_path(root / "a") == before
        changed = _prepare(index, root)
        assert changed.scan.scan_id != first.scan.scan_id
        assert changed.changed_files == 1
        assert changed.persistent_file_rows_written == 3  # Two COW rows plus one upsert.
        assert index._connection.execute(
            "SELECT status FROM duplicate_plan_heads WHERE scan_id=?", (first.scan.scan_id,),
        ).fetchone()[0] == "superseded"
        assert index._connection.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id=?", (first.scan.scan_id,),
        ).fetchone()[0] == 2
        assert DedupPlanner(index).plan(changed.scan.scan_id).group_count == 0
        replay = _prepare(index, root)
        assert replay.scan == changed.scan and replay.reused_generation
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_portable_delta_preserves_add_remove_rename_and_hardlink_members(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 3)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        _prepare(index, root)
        (root / "0000").rename(root / "renamed")
        (root / "0001").unlink()
        os.link(root / "0002", root / "alias")
        result = _prepare(index, root)
        actual = {Path(item.path).name: item for item in index.published_snapshots(root)}
        assert set(actual) == {"renamed", "0002", "alias"}
        assert actual["0002"].identity == actual["alias"].identity
        assert result.scan.files_seen == 3
        assert _prepare(index, root).reused_generation


def test_policy_or_root_identity_change_never_reuses_old_generation(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 2)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        first = _prepare(index, root)
        policy = InventoryExclusionPolicy.compile((), file_names=("0001",))
        restricted = _prepare(index, root, policy=policy)
        assert restricted.scan.scan_id != first.scan.scan_id
        assert restricted.scan.files_seen == 1 and not restricted.reused_generation
        root.rename(tmp_path / "historical")
        root.mkdir()
        (root / "new").write_bytes(b"new")
        replaced = _prepare(index, root, policy=policy)
        assert replaced.scan.scan_id != restricted.scan.scan_id
        assert not replaced.reused_generation
        assert [Path(item.path).name for item in index.published_snapshots(root)] == ["new"]


def test_cancelled_portable_observation_preserves_published_generation(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        first = _prepare(index, root)
        old_checkpoint = index.inventory_checkpoint(root)
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 6

        with pytest.raises(InventoryScanCancelled):
            prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()),
                                       work_budget=InventoryWorkBudget(cancellation_check=cancel))
        assert index.inventory_checkpoint(root) == old_checkpoint
        assert index.scan_summary(first.scan.scan_id) == first.scan
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        assert _prepare(index, root).reused_generation


def test_v14_migration_preserves_rows_and_requires_fresh_change_observations(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 2)
    database = tmp_path / "index.sqlite3"
    with DedupIndex(database) as index:
        original = _prepare(index, root)
        before = index._connection.execute("SELECT * FROM files ORDER BY scan_id,path").fetchall()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE inventory_file_change_versions")
        connection.execute("UPDATE metadata SET value='14' WHERE key='schema_version'")
    initialize_inventory_schema(database)
    with DedupIndex(database) as index:
        assert index._connection.execute("SELECT * FROM files ORDER BY scan_id,path").fetchall() == before
        assert index._connection.execute("SELECT COUNT(*) FROM inventory_file_change_versions").fetchone()[0] == 0
        refreshed = _prepare(index, root)
        assert refreshed.scan.scan_id != original.scan.scan_id and not refreshed.reused_generation
        assert _prepare(index, root).reused_generation
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pruning_files_cascades_only_their_change_observations(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 2)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        for number in range(4):
            policy = InventoryExclusionPolicy.compile((), file_names=(f"unused-{number}",))
            _prepare(index, root, policy=policy)
        files = index._connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        versions = index._connection.execute("SELECT COUNT(*) FROM inventory_file_change_versions").fetchone()[0]
        assert files == versions == 4
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("phase", ("delta", "copy", "digest"))
@pytest.mark.parametrize("deadline", (False, True))
def test_sql_cancellation_rolls_back_and_clears_handler(tmp_path: Path, phase: str, deadline: bool) -> None:
    root = _corpus(tmp_path, 1000)
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        first = _prepare(index, root)
        checkpoint = index.inventory_checkpoint(root)
        (root / "0000").write_text("changed")
        reached = False

        def trace(statement: str) -> None:
            nonlocal reached
            sql = " ".join(statement.split())
            if phase == "delta" and sql.startswith("CREATE TEMP TABLE portable_file_delta AS"):
                reached = True
            elif phase == "copy" and sql.startswith("INSERT INTO files("):
                reached = True
            elif phase == "digest" and sql.startswith("SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns FROM files WHERE scan_id="):
                reached = True

        index._connection.set_trace_callback(trace)
        budget = InventoryWorkBudget(
            cancellation_check=None if deadline else lambda: reached,
            deadline_monotonic=10 if deadline else None,
            monotonic_clock=lambda: 11 if reached else 9,
        )
        with pytest.raises(InventoryScanDeadlineExceeded if deadline else InventoryScanCancelled):
            prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()), work_budget=budget)
        index._connection.set_trace_callback(None)
        assert reached
        assert index.inventory_checkpoint(root) == checkpoint
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        assert index.scan_summary(first.scan.scan_id) == first.scan
        assert index._connection.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'portable_%'").fetchall() == []
        assert index._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # A captured cancellation must not leak its SQLite callback into the
        # next owner operation, including work with >1000 VM instructions.
        assert index._connection.execute(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5000) SELECT SUM(x) FROM n"
        ).fetchone()[0] == 12_502_500
        assert _prepare(index, root).scan.scan_id != first.scan.scan_id
