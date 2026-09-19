"""Read-only Framework run-id allocator contracts on isolated SQLite fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from neocortex.persistence.framework_run_ids import (
    FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,
    FrameworkRunIdOverflowError,
    FrameworkRunIdSchemaError,
    framework_next_run_id,
)
from neocortex.persistence.framework_schema import initialize_framework_schema


_MAX_SQLITE_INT = 9223372036854775807


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    initialize_framework_schema(connection, lambda: None)
    try:
        yield connection
    finally:
        connection.close()


def test_next_run_id_honors_legacy_floor_without_writing(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?)",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY, "41"),
    )
    connection.execute(
        "INSERT INTO initial_runs(run_id,root,started_ns,status) "
        "VALUES(?,?,?,'completed')",
        (7, "/fixture", 7),
    )
    connection.commit()
    before_changes = connection.total_changes

    assert framework_next_run_id(connection) == 42
    assert connection.total_changes == before_changes
    assert connection.execute(
        "SELECT value FROM metadata WHERE key=?",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,),
    ).fetchone() == ("41",)


@pytest.mark.parametrize("floor", ("not-a-number", "-1", str(_MAX_SQLITE_INT)))
def test_malformed_or_out_of_range_floor_fails_closed(
    connection: sqlite3.Connection, floor: str
) -> None:
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?)",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY, floor),
    )
    connection.commit()

    with pytest.raises(FrameworkRunIdSchemaError, match="floor"):
        framework_next_run_id(connection)


def test_floor_near_sqlite_limit_fails_without_reuse(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?)",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY, str(_MAX_SQLITE_INT - 1)),
    )
    connection.commit()

    with pytest.raises(FrameworkRunIdOverflowError, match="exhausted"):
        framework_next_run_id(connection)


def test_preserved_reference_controls_next_identifier(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO initial_runs(run_id,root,started_ns,status) "
        "VALUES(?,?,?,'completed')",
        (10, "/fixture", 10),
    )
    connection.execute(
        """INSERT INTO content_type_cache(
            volume_id,file_id,size,mtime_ns,detector_version,status,
            last_seen_run_id,updated_ns
        ) VALUES(?,?,?,?,?,?,?,?)""",
        ("fixture", "item", 1, 1, "fixture", "unknown", 99, 1),
    )
    connection.commit()

    assert framework_next_run_id(connection) == 100


def test_malformed_preserved_reference_fails_closed(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO content_type_cache(
            volume_id,file_id,size,mtime_ns,detector_version,status,
            last_seen_run_id,updated_ns
        ) VALUES(?,?,?,?,?,?,?,?)""",
        ("fixture", "item", 1, 1, "fixture", "unknown", -1, 1),
    )
    connection.commit()

    with pytest.raises(FrameworkRunIdSchemaError, match="reference"):
        framework_next_run_id(connection)
