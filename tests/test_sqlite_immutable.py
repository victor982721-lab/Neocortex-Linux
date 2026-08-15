from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from _04_Nucleo_Operativo import sqlite_immutable
from _04_Nucleo_Operativo.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
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
    ) -> sqlite3.Connection:
        database.unlink()
        return real_connect(database_arg, uri=uri, timeout=timeout)

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
