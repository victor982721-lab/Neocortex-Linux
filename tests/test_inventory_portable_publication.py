"""Portable snapshots publish once and admit work while acquiring the lock."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, InventoryError, InventoryExclusionPolicy
from neocortex.deduplication.inventory import portable
from neocortex.deduplication.inventory.generation import inventory_content_digest
from neocortex.deduplication.inventory.scanner import (
    InventoryScanCancelled,
    InventoryScanDeadlineExceeded,
    InventoryWorkBudget,
)
from neocortex.integrations.inventory.inventory_coordinator import prepare_inventory


class _FrameworkEvents:
    def record_event(self, *_args):
        pass

    def referenced_inventory_scan_ids(self):
        return ()

    def update_run_start_cursor(self, *_args):
        pass


def _prepare(index: DedupIndex, root: Path):
    return prepare_inventory(
        index, _FrameworkEvents(), 1, root, None, progress=lambda _event: None,
        exclusion_policy=InventoryExclusionPolicy.compile(()),
        allow_incremental=False, publish_portable_checkpoint=True,
    )


def _corpus(tmp_path: Path, count: int = 20) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    for number in range(count):
        (root / f"{number:04d}").write_text(f"item {number}")
    return root


def _historical_rows(connection: sqlite3.Connection, scan_id: int):
    return connection.execute(
        "SELECT * FROM files WHERE scan_id=? ORDER BY path", (scan_id,),
    ).fetchall()


@pytest.mark.parametrize("change", ("rewrite", "delete", "rename", "empty"))
def test_successor_materializes_only_surviving_observations_once(tmp_path: Path, change: str) -> None:
    root = _corpus(tmp_path)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        first = _prepare(index, root)
        connection = index._connection
        original = _historical_rows(connection, first.scan.scan_id)
        original_versions = connection.execute(
            "SELECT * FROM inventory_file_change_versions WHERE scan_id=? ORDER BY path",
            (first.scan.scan_id,),
        ).fetchall()
        connection.execute("CREATE TEMP TABLE portable_test_writes(kind TEXT)")
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TEMP TRIGGER count_file_{operation.lower()} AFTER {operation} ON main.files "
                f"BEGIN INSERT INTO portable_test_writes VALUES('{operation}'); END"
            )
        if change == "rewrite":
            (root / "0000").write_text("replacement")
        elif change == "rename":
            (root / "0000").rename(root / "renamed")
        else:
            for number in range(20 if change == "empty" else 18):
                (root / f"{number:04d}").unlink()

        second = _prepare(index, root)
        current_count = len(tuple(root.iterdir()))
        assert second.scan.scan_id != first.scan.scan_id
        assert second.persistent_file_rows_written == current_count
        assert connection.execute(
            "SELECT kind,COUNT(*) FROM portable_test_writes GROUP BY kind"
        ).fetchall() == ([] if current_count == 0 else [("INSERT", current_count)])
        assert _historical_rows(connection, first.scan.scan_id) == original
        assert connection.execute(
            "SELECT * FROM inventory_file_change_versions WHERE scan_id=? ORDER BY path",
            (first.scan.scan_id,),
        ).fetchall() == original_versions
        assert index.scan_content_digest(second.scan.scan_id) == inventory_content_digest(connection, second.scan.scan_id)
        assert len(tuple(index.published_snapshots(root))) == current_count
        assert _prepare(index, root).reused_generation
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_digest_is_prepared_before_the_main_writer_lock(tmp_path: Path, monkeypatch) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        _prepare(index, root)
        (root / "0000").write_text("changed")
        original = portable.portable_observation_content_digest
        called = False

        def observe(connection, **kwargs):
            nonlocal called
            called = True
            assert not connection.in_transaction
            with sqlite3.connect(database, timeout=0) as competitor:
                competitor.execute("BEGIN IMMEDIATE")
                competitor.rollback()
            return original(connection, **kwargs)

        monkeypatch.setattr(portable, "portable_observation_content_digest", observe)
        result = _prepare(index, root)
        assert called
        assert index.scan_content_digest(result.scan.scan_id) == inventory_content_digest(index._connection, result.scan.scan_id)


def test_external_owner_commit_before_lock_abstains_without_publishing(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        first = _prepare(index, root)
        checkpoint = index.inventory_checkpoint(root)
        (root / "0000").write_text("changed")
        injected = False

        def trace(sql):
            nonlocal injected
            if sql == "BEGIN IMMEDIATE" and not injected:
                injected = True
                with sqlite3.connect(database, timeout=0) as competitor:
                    competitor.execute("INSERT INTO metadata(key,value) VALUES('portable-test-drift','1')")

        index._connection.set_trace_callback(trace)
        with pytest.raises(InventoryError, match="changed during observation"):
            _prepare(index, root)
        index._connection.set_trace_callback(None)
        assert injected
        assert index.inventory_checkpoint(root) == checkpoint
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (1,)
        assert index.scan_summary(first.scan.scan_id) == first.scan
        assert _prepare(index, root).scan.scan_id != first.scan.scan_id


def test_publication_revalidation_holds_the_writer_lock(tmp_path: Path, monkeypatch) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        _prepare(index, root)
        (root / "0000").write_text("changed")
        original = index._create_inventory_successor
        blocked = False

        def compete(*args, **kwargs):
            nonlocal blocked
            with sqlite3.connect(database, timeout=0) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("INSERT INTO metadata(key,value) VALUES('portable-test-competitor','1')")
                blocked = True
            return original(*args, **kwargs)

        monkeypatch.setattr(index, "_create_inventory_successor", compete)
        _prepare(index, root)
        assert blocked
        assert index._connection.execute("SELECT value FROM metadata WHERE key='portable-test-competitor'").fetchone() is None


def test_foreign_transaction_is_not_committed_by_observation(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        _prepare(index, root)
        connection = index._connection
        connection.execute("INSERT INTO metadata(key,value) VALUES('uncommitted-owner-change','1')")
        with pytest.raises(InventoryError, match="own transaction"):
            portable.prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()))
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT value FROM metadata WHERE key='uncommitted-owner-change'").fetchone() is None


def test_unchanged_replay_preserves_the_published_duplicate_plan(tmp_path: Path) -> None:
    from neocortex.deduplication import DedupPlanner

    root = _corpus(tmp_path, 0)
    (root / "a").write_bytes(b"same")
    (root / "b").write_bytes(b"same")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        first = _prepare(index, root)
        plan = DedupPlanner(index).plan(first.scan.scan_id)
        previous = index._connection.execute("SELECT * FROM duplicate_plan_heads").fetchall()
        replay = _prepare(index, root)
        assert replay.reused_generation
        assert replay.scan == first.scan
        assert plan.group_count == 1
        assert index._connection.execute("SELECT * FROM duplicate_plan_heads").fetchall() == previous
        assert index._connection.execute("SELECT status FROM duplicate_plan_heads").fetchall() == [("published",)]


@pytest.mark.parametrize("deadline", (False, True))
def test_writer_contention_observes_budget_and_restores_timeout(tmp_path: Path, deadline: bool) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        _prepare(index, root)
        checkpoint = index.inventory_checkpoint(root)
        (root / "0000").write_text("changed")
        connection = index._connection
        connection.execute("PRAGMA busy_timeout=4321")
        waiting_since = None

        def trace(sql):
            nonlocal waiting_since
            if sql == "BEGIN IMMEDIATE" and waiting_since is None:
                waiting_since = time.monotonic()

        connection.set_trace_callback(trace)
        budget = InventoryWorkBudget(
            cancellation_check=None if deadline else lambda: waiting_since is not None,
            deadline_monotonic=10 if deadline else None,
            monotonic_clock=lambda: 11 if waiting_since is not None else 9,
        )
        with sqlite3.connect(database, timeout=0) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            with pytest.raises(InventoryScanDeadlineExceeded if deadline else InventoryScanCancelled):
                portable.prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()), work_budget=budget)
            assert waiting_since is not None
            assert time.monotonic() - waiting_since < 1.0
            competitor.rollback()
        connection.set_trace_callback(None)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (4321,)
        assert not connection.in_transaction
        assert index.inventory_checkpoint(root) == checkpoint
        assert connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (1,)
        assert connection.execute("SELECT name FROM sqlite_temp_master WHERE name IN ('portable_observed_files','portable_file_delta')").fetchall() == []
        assert _prepare(index, root).scan.scan_id != checkpoint.scan_id


def test_materialization_failure_rolls_back_generation_and_plan_head(tmp_path: Path) -> None:
    from neocortex.deduplication import DedupPlanner

    root = _corpus(tmp_path, 0)
    (root / "a").write_bytes(b"same")
    (root / "b").write_bytes(b"same")
    with DedupIndex(tmp_path / "inventory.sqlite3") as index:
        first = _prepare(index, root)
        DedupPlanner(index).plan(first.scan.scan_id)
        checkpoint = index.inventory_checkpoint(root)
        connection = index._connection
        original = _historical_rows(connection, first.scan.scan_id)
        (root / "a").write_bytes(b"changed")
        connection.execute(
            "CREATE TEMP TRIGGER abort_new_inventory_row BEFORE INSERT ON main.files "
            f"WHEN NEW.scan_id>{first.scan.scan_id} BEGIN SELECT RAISE(ABORT,'fixture materialization failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="fixture materialization failure"):
            _prepare(index, root)
        connection.execute("DROP TRIGGER abort_new_inventory_row")
        assert index.inventory_checkpoint(root) == checkpoint
        assert _historical_rows(connection, first.scan.scan_id) == original
        assert connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (1,)
        assert connection.execute("SELECT status FROM duplicate_plan_heads").fetchall() == [("published",)]
        assert connection.execute("SELECT * FROM inventory_scan_successors").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_writer_contention_preserves_the_original_total_wait_limit(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        _prepare(index, root)
        checkpoint = index.inventory_checkpoint(root)
        (root / "0000").write_text("changed")
        connection = index._connection
        connection.execute("PRAGMA busy_timeout=40")
        with sqlite3.connect(database, timeout=0) as competitor:
            competitor.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                portable.prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()))
            assert time.monotonic() - started < 1.0
            competitor.rollback()
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (40,)
        assert not connection.in_transaction
        assert index.inventory_checkpoint(root) == checkpoint


def test_writer_lock_can_be_acquired_after_a_bounded_retry(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    database = tmp_path / "inventory.sqlite3"
    with DedupIndex(database) as index:
        _prepare(index, root)
        (root / "0000").write_text("changed")
        locked = threading.Event()
        release = threading.Event()
        connection = index._connection
        connection.execute("PRAGMA busy_timeout=2000")
        attempts = 0

        def writer():
            with sqlite3.connect(database, timeout=0) as competitor:
                competitor.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(timeout=5)
                competitor.rollback()

        def trace(sql):
            nonlocal attempts
            if sql == "BEGIN IMMEDIATE":
                attempts += 1
                if attempts == 2:
                    release.set()

        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert locked.wait(timeout=2)
            connection.set_trace_callback(trace)
            result = portable.prepare_portable_inventory(index, root, exclusion_policy=InventoryExclusionPolicy.compile(()))
        finally:
            release.set()
            worker.join(timeout=2)
            connection.set_trace_callback(None)
        assert not worker.is_alive()
        assert attempts == 2
        assert result.scan.scan_id == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (2000,)
        assert index.inventory_checkpoint(root).scan_id == result.scan.scan_id
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
