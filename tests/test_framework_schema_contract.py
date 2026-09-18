"""Regression tests for the framework SQLite schema contract."""
# region [00] Contexto del módulo
# Módulo: tests/test_framework_schema_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3

import pytest

from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence import framework_schema
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


def test_failed_legacy_migration_rolls_back_ddl_and_version(tmp_path) -> None:
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

    with pytest.raises(RuntimeError, match="initialization from version 14 failed"):
        FrameworkState(database)

    connection = sqlite3.connect(database)
    try:
        assert _objects(connection) == before
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("14",)
        decision_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(review_decisions)")
        }
        assert decision_columns == {"decision_id"}
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='initial_runs'"
            ).fetchone()
            is None
        )
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


@pytest.mark.parametrize("version", (20, 21, 22, 23))
def test_route_identity_index_migration_preserves_legacy_rows(version: int) -> None:
    builders = {
        20: framework_schema._build_v20_exact_schema,
        21: framework_schema._build_v21_exact_schema,
        22: framework_schema._build_v23_exact_schema,
        23: framework_schema._build_v23_exact_schema,
    }
    connection = sqlite3.connect(":memory:")
    try:
        builders[version](connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version',?)", (str(version),))
        rows = (
            (1, "text/plain", "/fixture/a", "1", "2", 10, 20, -1),
            (1, "text/plain", "/fixture/hardlink", "1", "2", 10, 20, -1),
            (2, "text/plain", "/fixture/other-run", "1", "2", 10, 20, -1),
        )
        connection.executemany("INSERT INTO route_candidates VALUES(?,?,?,?,?,?,?,?)", rows)
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='route_candidates_identity_idx'"
        ).fetchone() is None

        initialize_framework_schema(connection, lambda: None)

        assert connection.execute(
            "SELECT * FROM route_candidates ORDER BY run_id,path"
        ).fetchall() == list(rows)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert tuple(row[2] for row in connection.execute(
            "PRAGMA index_info(route_candidates_identity_idx)"
        )) == ("run_id", "volume_id", "file_id")
        framework_schema.validate_framework_schema(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_route_identity_index_migration_rolls_back_on_interruption() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        framework_schema._build_v23_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','23')")
        connection.commit()
        before = _objects(connection)

        def interrupted() -> None:
            assert connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='route_candidates_identity_idx'"
            ).fetchone() == (1,)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            initialize_framework_schema(connection, interrupted)
        assert not connection.in_transaction
        assert _objects(connection) == before
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("23",)
        framework_schema.validate_framework_schema_v23(connection)
    finally:
        connection.close()


def test_route_identity_index_migration_rejects_unknown_legacy_index() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        framework_schema._build_v23_exact_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','23')")
        connection.execute(
            "CREATE INDEX route_candidates_identity_idx ON route_candidates(path)"
        )
        connection.commit()
        before = _objects(connection)
        with pytest.raises(RuntimeError, match="schema contract validation failed"):
            initialize_framework_schema(connection, lambda: None)
        assert _objects(connection) == before
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("23",)
    finally:
        connection.close()
# endregion [02]
