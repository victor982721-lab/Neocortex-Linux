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
