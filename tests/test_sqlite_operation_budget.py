from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    SQLiteSnapshotReuseCache,
    capture_sqlite_read_fence,
)


def _database(tmp_path: Path, name: str = "owner.sqlite3") -> Path:
    path = tmp_path / name
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES(7)")
    return path


def _append(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO probe VALUES(8)")


def _copy_counter(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    copied: list[Path] = []
    real_copy = sqlite_immutable._copy_regular_file

    def copy(source: Path, destination: Path, **kwargs: object) -> None:
        copied.append(source)
        real_copy(source, destination, **kwargs)

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", copy)
    return copied


def _snapshot_path(connection: sqlite3.Connection) -> Path:
    return Path(connection.execute("PRAGMA database_list").fetchone()[2])


def _assert_no_snapshots(tmp_path: Path) -> None:
    assert not list(tmp_path.glob("neocortex-sqlite-read-*"))


@pytest.mark.parametrize("sidecars", [False, True])
def test_oversized_fenced_input_fails_before_any_destination_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecars: bool
) -> None:
    database = _database(tmp_path)
    if sidecars:
        # No SQLite open should be reached: all fenced sidecar sizes count,
        # even when the main file alone would fit the allowance.
        Path(f"{database}-wal").write_bytes(b"wal" * 1024)
        Path(f"{database}-shm").write_bytes(b"shm" * 1024)
        Path(f"{database}-journal").write_bytes(b"journal" * 1024)
    fence = capture_sqlite_read_fence(database)
    total = fence.main.size + sum(identity.size for _, identity in fence.sidecars)
    copied = _copy_counter(monkeypatch)
    session = SQLiteReadSession(
        database,
        mode="snapshot_temp",
        temp_root=tmp_path,
        budget=SQLiteSnapshotBudget(max_temporary_bytes=total - 1),
    )
    with pytest.raises(SQLiteSnapshotBudgetExceeded) as raised:
        session.open()
    assert raised.value.reason == "temporary_bytes"
    assert copied == []
    assert session.metrics.attempts == 1
    assert session.metrics.temporary_bytes == 0
    assert capture_sqlite_read_fence(database) == fence
    _assert_no_snapshots(tmp_path)


def test_two_active_snapshots_cannot_exceed_the_aggregate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _database(tmp_path, "first.sqlite3"), _database(tmp_path, "second.sqlite3")
    size = first.stat().st_size
    assert size == second.stat().st_size
    copied = _copy_counter(monkeypatch)
    with SQLiteSnapshotReuseCache(max_temporary_bytes=2 * size - 1) as cache:
        with cache.acquire(first, generation="published", temp_root=tmp_path) as connection:
            with pytest.raises(SQLiteSnapshotBudgetExceeded) as raised:
                with cache.acquire(second, generation="published", temp_root=tmp_path):
                    pytest.fail("both retained copies must not exceed the operation allowance")
            assert raised.value.reason == "temporary_bytes"
            assert copied == [first]
            assert connection.execute("SELECT value FROM probe").fetchone()[0] == 7
            metrics = cache.snapshot_metrics
            assert metrics.retained_temporary_bytes == size
            assert metrics.peak_temporary_bytes == size
            assert metrics.attempts == 2
            assert metrics.prepared_views == 1
            assert cache.remaining_temporary_bytes == size - 1
    assert cache.snapshot_metrics.retained_temporary_bytes == 0
    _assert_no_snapshots(tmp_path)


def test_two_views_fit_exactly_and_reuse_does_not_spend_preparation_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _database(tmp_path, "first.sqlite3"), _database(tmp_path, "second.sqlite3")
    total = first.stat().st_size + second.stat().st_size
    copied = _copy_counter(monkeypatch)
    with SQLiteSnapshotReuseCache(max_temporary_bytes=total) as cache:
        with cache.acquire(first, generation=1, temp_root=tmp_path) as original:
            with cache.acquire(second, generation=1, temp_root=tmp_path):
                with cache.acquire(first, generation=1, temp_root=tmp_path) as reused:
                    assert reused is original
                    assert cache.remaining_temporary_bytes == 0
                    metrics = cache.snapshot_metrics
                    assert metrics.retained_views == 2
                    assert metrics.retained_temporary_bytes == total
                    assert metrics.peak_temporary_bytes == total
                    assert metrics.prepared_views == 2
                    assert metrics.reused_views == 1
                    assert metrics.attempts == 2
                    assert asdict(metrics)["prepare_time_seconds"] >= 0
        assert copied == [first, second]
        metrics.retained_temporary_bytes = 0
        assert cache.snapshot_metrics.retained_temporary_bytes == total
    assert cache.snapshot_metrics.retained_views == 0
    assert cache.remaining_temporary_bytes == total
    _assert_no_snapshots(tmp_path)


def test_idle_view_can_be_evicted_without_raising_the_operation_limit(tmp_path: Path) -> None:
    first, second = _database(tmp_path, "first.sqlite3"), _database(tmp_path, "second.sqlite3")
    size = first.stat().st_size
    with SQLiteSnapshotReuseCache(max_temporary_bytes=size) as cache:
        with cache.acquire(first, generation=1, temp_root=tmp_path) as connection:
            old_path = _snapshot_path(connection)
        with cache.acquire(second, generation=1, temp_root=tmp_path) as connection:
            assert _snapshot_path(connection) != old_path
            assert not old_path.exists()
            assert cache.snapshot_metrics.retained_temporary_bytes == size
            assert cache.snapshot_metrics.peak_temporary_bytes == size
            assert cache.snapshot_metrics.invalidated_views == 1
    _assert_no_snapshots(tmp_path)


def test_publishing_a_new_fence_invalidates_and_removes_idle_snapshot(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache(max_temporary_bytes=database.stat().st_size) as cache:
        with cache.acquire(database, generation="same-label", temp_root=tmp_path) as first:
            previous = _snapshot_path(first)
        _append(database)
        with cache.acquire(database, generation="same-label", temp_root=tmp_path) as second:
            assert second.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
            assert _snapshot_path(second) != previous
            assert not previous.exists()
            assert cache.snapshot_metrics.invalidated_views == 1
            assert cache.snapshot_metrics.prepared_views == 2
            assert cache.snapshot_metrics.retained_views == 1
    _assert_no_snapshots(tmp_path)


def test_active_detached_view_finishes_but_is_not_reused_after_publication(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache(max_temporary_bytes=2 * database.stat().st_size) as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path) as first:
            old_path = _snapshot_path(first)
            _append(database)
            with cache.acquire(database, generation=1, temp_root=tmp_path) as second:
                assert second is not first
                assert first.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
                assert second.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
                assert old_path.exists()
                assert cache.snapshot_metrics.retained_views == 2
        assert not old_path.exists()
        assert cache.snapshot_metrics.retained_views == 1
    _assert_no_snapshots(tmp_path)


def test_successful_preparation_fence_not_earlier_cache_fence_owns_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    original_open = SQLiteReadSession.open
    published = False

    def publish_before_open(session: SQLiteReadSession) -> sqlite3.Connection:
        nonlocal published
        if not published:
            _append(database)
            published = True
        return original_open(session)

    monkeypatch.setattr(SQLiteReadSession, "open", publish_before_open)
    copied = _copy_counter(monkeypatch)
    with SQLiteSnapshotReuseCache() as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path) as first:
            assert first.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
        with cache.acquire(database, generation=1, temp_root=tmp_path) as second:
            assert second is first
        assert copied == [database]
        assert cache.snapshot_metrics.prepared_views == 1
        assert cache.snapshot_metrics.reused_views == 1
    _assert_no_snapshots(tmp_path)


def test_rekey_collision_cannot_overwrite_an_active_invalidated_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    source_fence = capture_sqlite_read_fence(database)
    raced_fence = replace(
        source_fence,
        main=replace(source_fence.main, mtime_ns=source_fence.main.mtime_ns + 1),
    )
    real_capture = sqlite_immutable.capture_sqlite_read_fence
    preflight_pending = True

    def raced_capture(path: Path) -> sqlite_immutable.SQLiteImmutableFence:
        nonlocal preflight_pending
        if path == database and preflight_pending:
            preflight_pending = False
            return raced_fence
        return real_capture(path)

    size = database.stat().st_size
    with SQLiteSnapshotReuseCache(max_temporary_bytes=2 * size) as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path) as original:
            original_path = _snapshot_path(original)
            original_entry = next(iter(cache._entries.values()))
            with monkeypatch.context() as scoped:
                scoped.setattr(sqlite_immutable, "capture_sqlite_read_fence", raced_capture)
                with pytest.raises(ImmutableSQLiteUnavailable, match="collided"):
                    with cache.acquire(database, generation=1, temp_root=tmp_path):
                        pytest.fail("a candidate must not replace an active retained view")
            assert list(cache._entries.values()) == [original_entry]
            assert original_entry.invalidated
            assert original.execute("SELECT value FROM probe").fetchone()[0] == 7
            assert list(tmp_path.glob("neocortex-sqlite-read-*")) == [original_path.parent]
            metrics = cache.snapshot_metrics
            assert metrics.retained_views == 1
            assert metrics.retained_temporary_bytes == size
            assert metrics.peak_temporary_bytes == 2 * size
            assert metrics.prepared_views == 1
            assert metrics.attempts == 2
        assert not original_path.exists()
        assert cache.snapshot_metrics.retained_views == 0
        assert cache.snapshot_metrics.retained_temporary_bytes == 0
        with cache.acquire(database, generation=1, temp_root=tmp_path) as fresh:
            assert fresh is not original
            assert fresh.execute("SELECT value FROM probe").fetchone()[0] == 7
    _assert_no_snapshots(tmp_path)


def test_wal_preparation_peak_and_live_retention_are_distinct(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO probe VALUES(8)")
        writer.commit()
        fence = capture_sqlite_read_fence(database)
        input_bytes = fence.main.size + sum(identity.size for _, identity in fence.sidecars)
        with SQLiteSnapshotReuseCache(max_temporary_bytes=input_bytes) as cache:
            with cache.acquire(database, generation=1, temp_root=tmp_path) as connection:
                assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
                metrics = cache.snapshot_metrics
                assert metrics.retained_temporary_bytes == _snapshot_path(connection).stat().st_size
                assert metrics.retained_temporary_bytes < metrics.peak_temporary_bytes
                assert metrics.peak_temporary_bytes == input_bytes
                assert cache.remaining_temporary_bytes == input_bytes - metrics.retained_temporary_bytes
            assert capture_sqlite_read_fence(database) == fence
    _assert_no_snapshots(tmp_path)


@pytest.mark.parametrize("change", ["generation", "budget"])
def test_generation_or_policy_change_prevents_reuse(tmp_path: Path, change: str) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache() as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path) as first:
            old_path = _snapshot_path(first)
        generation = 2 if change == "generation" else 1
        budget = SQLiteSnapshotBudget(block_bytes=4096) if change == "budget" else None
        with cache.acquire(
            database, generation=generation, temp_root=tmp_path, budget=budget
        ) as second:
            assert second is not first
            assert not old_path.exists()
            assert cache.snapshot_metrics.invalidated_views == 1
    _assert_no_snapshots(tmp_path)


def test_failed_borrowed_connection_is_not_retained_for_next_acquire(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache() as cache:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with cache.acquire(database, generation=1, temp_root=tmp_path) as failed:
                old_path = _snapshot_path(failed)
                failed.execute("SELECT * FROM missing_table")
        assert not old_path.exists()
        assert cache.snapshot_metrics.retained_temporary_bytes == 0
        with cache.acquire(database, generation=1, temp_root=tmp_path) as recovered:
            assert recovered is not failed
            assert recovered.execute("SELECT value FROM probe").fetchone()[0] == 7
            assert cache.snapshot_metrics.prepared_views == 2
    _assert_no_snapshots(tmp_path)


def test_explicitly_closed_borrowed_handle_is_reprepared(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache() as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path) as first:
            first.close()
        with cache.acquire(database, generation=1, temp_root=tmp_path) as second:
            assert second is not first
            assert second.execute("SELECT value FROM probe").fetchone()[0] == 7
            assert cache.snapshot_metrics.invalidated_views == 1
    _assert_no_snapshots(tmp_path)


def test_cancelled_reuse_does_not_return_the_prepared_handle(tmp_path: Path) -> None:
    database = _database(tmp_path)
    cancelled = False
    budget = SQLiteSnapshotBudget(cancellation_check=lambda: cancelled)
    with SQLiteSnapshotReuseCache() as cache:
        with cache.acquire(database, generation=1, temp_root=tmp_path, budget=budget) as first:
            previous = _snapshot_path(first)
        cancelled = True
        with pytest.raises(SQLiteSnapshotBudgetExceeded) as raised:
            with cache.acquire(database, generation=1, temp_root=tmp_path, budget=budget):
                pytest.fail("cancellation must apply to reused snapshots too")
        assert raised.value.reason == "cancelled"
        assert not previous.exists()
        assert cache.snapshot_metrics.cancelled
        assert cache.snapshot_metrics.reused_views == 0
        assert cache.snapshot_metrics.retained_temporary_bytes == 0
    _assert_no_snapshots(tmp_path)


def test_cancelled_preparation_cleans_temp_and_records_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    copied = False
    real_copy = sqlite_immutable._copy_regular_file

    def copy(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal copied
        real_copy(source, destination, **kwargs)
        copied = True

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", copy)
    with SQLiteSnapshotReuseCache() as cache:
        with pytest.raises(SQLiteSnapshotBudgetExceeded) as raised:
            with cache.acquire(
                database,
                generation=1,
                temp_root=tmp_path,
                budget=SQLiteSnapshotBudget(cancellation_check=lambda: copied),
            ):
                pytest.fail("a cancelled candidate must not be retained")
        assert raised.value.reason == "cancelled"
        metrics = cache.snapshot_metrics
        assert metrics.cancelled
        assert metrics.attempts == 1
        assert metrics.prepared_views == 0
        assert metrics.retained_temporary_bytes == 0
        assert metrics.peak_temporary_bytes == database.stat().st_size
        _assert_no_snapshots(tmp_path)


def test_per_snapshot_budget_is_never_raised_to_the_aggregate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    copied = _copy_counter(monkeypatch)
    with SQLiteSnapshotReuseCache(max_temporary_bytes=10 * database.stat().st_size) as cache:
        with pytest.raises(SQLiteSnapshotBudgetExceeded):
            with cache.acquire(
                database,
                generation=1,
                temp_root=tmp_path,
                budget=SQLiteSnapshotBudget(max_temporary_bytes=database.stat().st_size - 1),
            ):
                pytest.fail("the original stricter preparation policy must be preserved")
        assert copied == []
        assert cache.snapshot_metrics.peak_temporary_bytes == 0
        assert cache.snapshot_metrics.retained_temporary_bytes == 0
    _assert_no_snapshots(tmp_path)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_operation_byte_limit_rejects_invalid_values(limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SQLiteSnapshotReuseCache(max_temporary_bytes=limit)
