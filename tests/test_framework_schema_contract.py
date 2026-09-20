"""Regression tests for the framework SQLite schema contract."""
# region [00] Contexto del módulo
# Módulo: tests/test_framework_schema_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3

import pytest

from neocortex.persistence.framework_schema import FrameworkStateIncompatible
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_writer import FrameworkState
# endregion [01]

# region [02] Implementación


def _objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def test_current_version_with_malformed_table_is_rejected_without_repair(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        f"""
        CREATE TABLE metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version','{SCHEMA_VERSION}');
        CREATE TABLE initial_runs(run_id INTEGER PRIMARY KEY);
        """
    )
    before = _objects(connection)
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    connection.close()

    with pytest.raises(RuntimeError, match="schema contract validation failed"):
        FrameworkState(database)

    connection = sqlite3.connect(database)
    try:
        assert _objects(connection) == before
        assert connection.execute("PRAGMA journal_mode").fetchone() == journal_mode
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
    finally:
        connection.close()


def test_current_version_with_malformed_named_index_is_rejected(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass

    connection = sqlite3.connect(database)
    connection.executescript(
        """
        DROP INDEX run_events_run_idx;
        CREATE INDEX run_events_run_idx ON run_events(event_id,run_id);
        """
    )
    connection.close()

    with pytest.raises(RuntimeError, match=r"run_events_run_idx.*incompatible columns"):
        FrameworkState(database)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            "CREATE TABLE unexpected_framework_state(value TEXT)",
            "unexpected table 'unexpected_framework_state'",
        ),
        (
            "ALTER TABLE metadata ADD COLUMN unexpected TEXT",
            "table 'metadata' has incompatible columns",
        ),
        (
            "CREATE INDEX unexpected_run_index ON initial_runs(run_id)",
            "table 'initial_runs' has unexpected indexes",
        ),
    ),
)
def test_current_version_rejects_unexpected_schema_objects_without_writes(
    tmp_path,
    mutation: str,
    message: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match=message):
        FrameworkState(database)

    assert database.read_bytes() == before


def test_future_version_is_rejected_without_schema_or_journal_changes(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    future_version = SCHEMA_VERSION + 1
    connection = sqlite3.connect(database)
    connection.executescript(
        f"""
        CREATE TABLE metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version','{future_version}');
        CREATE TABLE sentinel(value TEXT);
        INSERT INTO sentinel VALUES('preserve');
        """
    )
    before = _objects(connection)
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    connection.close()

    with pytest.raises(RuntimeError, match=rf"schema {future_version} is unsupported"):
        FrameworkState(database)

    connection = sqlite3.connect(database)
    try:
        assert _objects(connection) == before
        assert connection.execute("PRAGMA journal_mode").fetchone() == journal_mode
        assert connection.execute("SELECT value FROM sentinel").fetchone() == (
            "preserve",
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(future_version),)
    finally:
        connection.close()


def test_legacy_schema_requires_explicit_factory_reset_without_mutation(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO metadata VALUES('schema_version','14');
        CREATE TABLE run_events(event_id INTEGER PRIMARY KEY);
        CREATE TABLE review_decisions(decision_id INTEGER PRIMARY KEY);
        """
    )
    before = _objects(connection)
    connection.close()

    with pytest.raises(FrameworkStateIncompatible) as raised:
        FrameworkState(database)
    assert raised.value.observed_schema == 14
    assert raised.value.expected_schema == SCHEMA_VERSION
    assert raised.value.action == "factory_reset_required"

    connection = sqlite3.connect(database)
    try:
        assert _objects(connection) == before
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("14",)
        assert connection.execute("SELECT COUNT(*) FROM run_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone() == (0,)
    finally:
        connection.close()


def test_keyboard_interrupt_during_schema_initialization_rolls_back(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)

    def interrupt_post_migration() -> None:
        raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            initialize_framework_schema(connection, interrupt_post_migration)

        assert connection.in_transaction is False
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='metadata'"
        ).fetchone() is None
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    finally:
        connection.close()


def test_keyboard_interrupt_during_state_construction_closes_connection(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "framework.sqlite3"
    opened: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    def interrupt_backfill(self) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(FrameworkState, "_backfill_route_phases", interrupt_backfill)

    with pytest.raises(KeyboardInterrupt):
        FrameworkState(database)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    with original_connect(database) as verification:
        assert verification.execute(
            "SELECT name FROM sqlite_master WHERE name='metadata'"
        ).fetchone() is None


# endregion [02]
