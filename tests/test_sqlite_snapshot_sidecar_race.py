"""Bounded retry coverage for a sidecar disappearing during snapshot copy."""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence import sqlite_immutable


def _wal_fixture(tmp_path: Path) -> tuple[Path, sqlite3.Connection, Path]:
    database = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts(value TEXT)")
        connection.execute("INSERT INTO facts VALUES('base')")
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO facts VALUES('wal')")
    writer.commit()
    wal = Path(f"{database}-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    return database, writer, wal


def test_sidecar_enoent_recaptures_fence_and_retries_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, writer, wal = _wal_fixture(tmp_path)
    original_copy = sqlite_immutable._copy_regular_file
    disappeared = False

    def copy_with_one_sidecar_race(
        source: Path,
        destination: Path,
        *,
        budget_state=None,
    ) -> None:
        nonlocal disappeared
        if Path(source) == wal and not disappeared:
            disappeared = True
            writer.close()
        original_copy(source, destination, budget_state=budget_state)

    monkeypatch.setattr(
        sqlite_immutable,
        "_copy_regular_file",
        copy_with_one_sidecar_race,
    )
    try:
        with sqlite_immutable.SQLiteReadSession(
            database,
            mode=sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP,
            max_attempts=2,
        ) as connection:
            assert [tuple(row) for row in connection.execute("SELECT value FROM facts")] == [
                ("base",),
                ("wal",),
            ]
        assert disappeared
    finally:
        writer.close()


def test_repeated_sidecar_enoent_exhausts_bounded_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, writer, wal = _wal_fixture(tmp_path)
    original_copy = sqlite_immutable._copy_regular_file
    races = 0

    def copy_with_repeated_sidecar_race(
        source: Path,
        destination: Path,
        *,
        budget_state=None,
    ) -> None:
        nonlocal races
        if Path(source) == wal:
            races += 1
            raise FileNotFoundError(errno.ENOENT, "sidecar disappeared", str(wal))
        original_copy(source, destination, budget_state=budget_state)

    monkeypatch.setattr(
        sqlite_immutable,
        "_copy_regular_file",
        copy_with_repeated_sidecar_race,
    )
    try:
        with pytest.raises(sqlite_immutable.ImmutableSQLiteUnavailable):
            with sqlite_immutable.SQLiteReadSession(
                database,
                mode=sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP,
                max_attempts=2,
            ):
                pass
        assert races == 2
    finally:
        writer.close()


def test_missing_main_database_is_not_reclassified_as_a_sidecar_race(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError) as raised:
        with sqlite_immutable.SQLiteReadSession(
            database,
            mode=sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP,
            max_attempts=2,
        ):
            pass
    assert raised.value.filename == str(database)


def test_destination_enoent_is_not_retried_as_a_source_sidecar_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, writer, wal = _wal_fixture(tmp_path)
    original_copy = sqlite_immutable._copy_regular_file
    attempts = 0
    destination: Path | None = None

    def fail_destination(
        source: Path,
        target: Path,
        *,
        budget_state=None,
    ) -> None:
        nonlocal attempts, destination
        if Path(source) == wal:
            attempts += 1
            destination = target
            raise FileNotFoundError(errno.ENOENT, "destination missing", str(target))
        original_copy(source, target, budget_state=budget_state)

    monkeypatch.setattr(sqlite_immutable, "_copy_regular_file", fail_destination)
    try:
        with pytest.raises(FileNotFoundError) as raised:
            with sqlite_immutable.SQLiteReadSession(
                database,
                mode=sqlite_immutable.SQLiteReadMode.SNAPSHOT_TEMP,
                max_attempts=2,
            ):
                pass
        assert attempts == 1
        assert destination is not None
        assert raised.value.filename == str(destination)
    finally:
        writer.close()


def test_force_snapshot_detaches_after_copy_while_default_strict_fences_source(
    tmp_path: Path,
) -> None:
    database = tmp_path / "detached.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE facts(value TEXT)")
        connection.execute("INSERT INTO facts VALUES('before')")

    detached = sqlite_immutable.open_sidecar_safe_sqlite_connection(
        database,
        force_snapshot=True,
    )
    try:
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE facts SET value='after'")
        assert tuple(detached.execute("SELECT value FROM facts").fetchone()) == ("before",)
    finally:
        detached.close()
    with sqlite3.connect(database) as connection:
        assert tuple(connection.execute("SELECT value FROM facts").fetchone()) == ("after",)

    strict = sqlite_immutable.open_sidecar_safe_sqlite_connection(database)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with sqlite3.connect(database, timeout=0.1) as writer:
                writer.execute("UPDATE facts SET value='strict-drift'")
    finally:
        strict.close()
