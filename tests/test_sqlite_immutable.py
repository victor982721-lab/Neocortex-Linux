from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence import sqlite_immutable
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadMode,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    SQLiteSnapshotBudgetExceeded,
    SQLiteSnapshotReuseCache,
    immutable_sqlite_database,
)


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "owner.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES(7)")
    return database


def test_failed_open_preserves_operational_error_without_final_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    real_connect = sqlite_immutable.sqlite3.connect

    def remove_before_open(
        database_arg: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        factory: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> sqlite3.Connection:
        database.unlink()
        return real_connect(database_arg, uri=uri, timeout=timeout, factory=factory)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(sqlite_immutable.sqlite3, "connect", remove_before_open)

        with pytest.raises(sqlite3.OperationalError, match="open database"):
            with immutable_sqlite_database(database):
                pytest.fail("an owner deleted before open cannot produce a reader")

    assert not database.exists()
    assert sqlite_immutable.sqlite3.connect is real_connect


def test_owner_deleted_after_open_fails_closed_with_typed_error(tmp_path: Path) -> None:
    database = _database(tmp_path)
    connection: sqlite3.Connection | None = None

    with pytest.raises(
        ImmutableSQLiteUnavailable,
        match="SQLite owner changed during immutable read",
    ):
        with immutable_sqlite_database(database) as connection:
            assert connection.execute("SELECT value FROM probe").fetchone()[0] == 7
            database.unlink()

    assert not database.exists()
    assert connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_snapshot_temp_reads_active_wal_without_touching_source_sidecars(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO probe VALUES(8)")
        writer.commit()
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        before = {
            database: (database.stat().st_ino, database.stat().st_size, database.read_bytes()),
            wal: (wal.stat().st_ino, wal.stat().st_size, wal.read_bytes()),
            shm: (shm.stat().st_ino, shm.stat().st_size, shm.read_bytes()),
        }

        with SQLiteReadSession(
            database,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=tmp_path,
        ) as connection:
            assert [row[0] for row in connection.execute("SELECT value FROM probe ORDER BY value")] == [
                7,
                8,
            ]

        assert all(
            (path.stat().st_ino, path.stat().st_size, path.read_bytes()) == snapshot
            for path, snapshot in before.items()
        )
        assert not any(
            candidate.name.startswith("neocortex-sqlite-read-")
            for candidate in tmp_path.iterdir()
        )
    finally:
        writer.close()


def test_snapshot_preparation_budget_bounds_bytes_and_records_metrics(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with pytest.raises(SQLiteSnapshotBudgetExceeded, match="temporary bytes"):
        with SQLiteReadSession(
            database,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=tmp_path,
            budget=SQLiteSnapshotBudget(max_temporary_bytes=1, block_bytes=1),
        ):
            pytest.fail("an over-budget temporary snapshot must not publish")


def test_snapshot_preparation_cancellation_is_typed_and_bounded(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with pytest.raises(SQLiteSnapshotBudgetExceeded, match="cancelled"):
        with SQLiteReadSession(
            database,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=tmp_path,
            budget=SQLiteSnapshotBudget(cancellation_check=lambda: True),
        ):
            pytest.fail("a cancelled snapshot must not publish")


def test_snapshot_reuse_cache_reuses_one_generation_and_closes_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with SQLiteSnapshotReuseCache() as cache:
        with cache.acquire(database, generation="fixture", temp_root=tmp_path) as first:
            first_path = Path(first.execute("PRAGMA database_list").fetchone()[2])
            assert first.execute("SELECT value FROM probe").fetchone()[0] == 7
        with cache.acquire(database, generation="fixture", temp_root=tmp_path) as second:
            second_path = Path(second.execute("PRAGMA database_list").fetchone()[2])
            assert second_path == first_path
        entry = next(iter(cache._entries.values()))
        assert entry.session.metrics.reused_views == 1
    assert not first_path.exists()


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_snapshot_temp_materializes_wal_or_rollback_journal_to_immutable_copy(
    tmp_path: Path, journal_mode: str
) -> None:
    database = _database(tmp_path)
    writer = sqlite3.connect(database)
    try:
        assert (
            writer.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0].lower()
            == journal_mode.lower()
        )
        if journal_mode == "WAL":
            writer.execute("INSERT INTO probe VALUES(8)")
            writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO probe VALUES(9)")
        source_files = {
            candidate: candidate.read_bytes()
            for candidate in tmp_path.iterdir()
            if candidate.name.startswith(database.name)
        }

        with SQLiteReadSession(
            database,
            mode=SQLiteReadMode.SNAPSHOT_TEMP,
            temp_root=tmp_path,
        ) as connection:
            temporary = connection.execute("PRAGMA database_list").fetchone()[2]
            temporary_database = Path(temporary)
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert [
                row[0] for row in connection.execute("SELECT value FROM probe ORDER BY value")
            ] == ([7, 8] if journal_mode == "WAL" else [7])
            assert all(
                not Path(f"{temporary_database}{suffix}").exists()
                for suffix in ("-journal", "-wal", "-shm")
            )
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM probe")

        assert source_files == {
            candidate: candidate.read_bytes()
            for candidate in tmp_path.iterdir()
            if candidate.name.startswith(database.name)
        }
    finally:
        writer.rollback()
        writer.close()


def test_snapshot_session_cleanup_does_not_mask_body_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO probe VALUES(8)")
    writer.commit()
    real_temporary_directory = sqlite_immutable.tempfile.TemporaryDirectory

    class FailingCleanup:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._delegate = real_temporary_directory(*args, **kwargs)
            self.name = self._delegate.name

        def cleanup(self) -> None:
            self._delegate.cleanup()
            raise RuntimeError("injected temporary cleanup failure")

    monkeypatch.setattr(sqlite_immutable.tempfile, "TemporaryDirectory", FailingCleanup)
    try:
        with pytest.raises(RuntimeError, match="primary body failure") as raised:
            with SQLiteReadSession(
                database,
                mode=SQLiteReadMode.SNAPSHOT_TEMP,
                temp_root=tmp_path,
            ):
                raise RuntimeError("primary body failure")
        assert any("injected temporary cleanup failure" in note for note in raised.value.__notes__)
    finally:
        writer.close()


def test_immutable_strict_rejects_live_wal_and_symlink_owner(tmp_path: Path) -> None:
    database = _database(tmp_path)
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO probe VALUES(9)")
        writer.commit()
        with pytest.raises(ImmutableSQLiteUnavailable, match="non-empty WAL"):
            with SQLiteReadSession(database, mode=SQLiteReadMode.IMMUTABLE_STRICT):
                pytest.fail("a live WAL must not be read as immutable")
    finally:
        writer.close()

    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(database)
    with pytest.raises(ImmutableSQLiteUnavailable, match="stable regular file"):
        with SQLiteReadSession(alias, mode=SQLiteReadMode.SNAPSHOT_TEMP):
            pytest.fail("a symlinked owner must not be followed")


def test_writer_coordinated_mode_is_explicitly_unavailable_to_read_session(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with pytest.raises(ImmutableSQLiteUnavailable, match="writer_coordinated"):
        with SQLiteReadSession(database, mode=SQLiteReadMode.WRITER_COORDINATED):
            pytest.fail("writer mode requires an owner transaction")
