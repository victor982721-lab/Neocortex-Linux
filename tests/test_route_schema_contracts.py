# region [00] Contexto del módulo
# Módulo: tests/test_route_schema_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive.state import (
    ARCHIVE_SCHEMA_VERSION,
    initialize_archive_state,
)
from neocortex.capabilities.formats.audio.state import (
    AUDIO_SCHEMA_VERSION,
    initialize_audio_state,
)
from neocortex.capabilities.formats.office.state import (
    OFFICE_SCHEMA_VERSION,
    initialize_office_state,
)
from neocortex.sqlite_schema_contract import SQLiteSchemaContractError
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION, initialize_text_state
from neocortex.capabilities.formats.video.state import VIDEO_SCHEMA_VERSION, initialize_video_state
# endregion [01]

# region [02] Implementación


Initializer = Callable[[Path], None]


@pytest.fixture(
    params=(
        (
            "archive",
            initialize_archive_state,
            ARCHIVE_SCHEMA_VERSION,
            "archive_documents_status_idx",
        ),
        (
            "audio",
            initialize_audio_state,
            AUDIO_SCHEMA_VERSION,
            "audio_documents_status_idx",
        ),
        (
            "office",
            initialize_office_state,
            OFFICE_SCHEMA_VERSION,
            "office_documents_status_idx",
        ),
        (
            "text",
            initialize_text_state,
            TEXT_SCHEMA_VERSION,
            "text_documents_status_idx",
        ),
        (
            "video",
            initialize_video_state,
            VIDEO_SCHEMA_VERSION,
            "video_documents_status_idx",
        ),
    ),
    ids=("archive", "audio", "office", "text", "video"),
)
def route_schema(
    request: pytest.FixtureRequest,
) -> tuple[str, Initializer, int, str]:
    return request.param


def _metadata(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT key,value FROM metadata"))


def _schema_objects(path: Path) -> list[tuple[str, str, str | None]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """SELECT type,name,sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type,name"""
        ).fetchall()


def test_route_schema_initialization_is_idempotent_and_read_only_when_current(
    tmp_path: Path,
    route_schema: tuple[str, Initializer, int, str],
) -> None:
    label, initialize, version, _index_name = route_schema
    database = tmp_path / f"{label}.sqlite3"

    initialize(database)
    before = database.read_bytes()
    initialize(database)

    assert database.read_bytes() == before
    assert _metadata(database)["schema_version"] == str(version)


def test_current_route_schema_corruption_is_rejected_without_writes(
    tmp_path: Path,
    route_schema: tuple[str, Initializer, int, str],
) -> None:
    label, initialize, _version, index_name = route_schema
    database = tmp_path / f"{label}.sqlite3"
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f'DROP INDEX "{index_name}"')
    before = database.read_bytes()

    with pytest.raises(SQLiteSchemaContractError, match="lacks indexes"):
        initialize(database)

    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("declared_version", "exception", "message"),
    (
        ("future", RuntimeError, "newer than supported"),
        ("01", SQLiteSchemaContractError, "not canonical"),
    ),
)
def test_invalid_route_schema_versions_are_rejected_without_writes(
    tmp_path: Path,
    route_schema: tuple[str, Initializer, int, str],
    declared_version: str,
    exception: type[Exception],
    message: str,
) -> None:
    label, initialize, version, _index_name = route_schema
    database = tmp_path / f"{label}.sqlite3"
    initialize(database)
    stored_version = str(version + 1) if declared_version == "future" else declared_version
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (stored_version,),
        )
    before = database.read_bytes()

    with pytest.raises(exception, match=message):
        initialize(database)

    assert database.read_bytes() == before


def test_version_zero_route_schema_upgrade_preserves_existing_state(
    tmp_path: Path,
    route_schema: tuple[str, Initializer, int, str],
) -> None:
    label, initialize, version, _index_name = route_schema
    database = tmp_path / f"{label}.sqlite3"
    initialize(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value='0' WHERE key='schema_version'")
        connection.execute("INSERT INTO metadata(key,value) VALUES('legacy_marker','preserve-me')")

    initialize(database)

    assert _metadata(database) == {
        "legacy_marker": "preserve-me",
        "schema_version": str(version),
    }


def test_failed_route_schema_upgrade_rolls_back_all_schema_changes(
    tmp_path: Path,
    route_schema: tuple[str, Initializer, int, str],
) -> None:
    label, initialize, _version, _index_name = route_schema
    database = tmp_path / f"{label}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID"""
        )
        connection.execute(
            """CREATE TABLE documents(
                file_key TEXT PRIMARY KEY
            ) WITHOUT ROWID"""
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            (("schema_version", "0"), ("legacy_marker", "preserve-me")),
        )
    schema_before = _schema_objects(database)

    with pytest.raises(sqlite3.OperationalError, match="no such column: path"):
        initialize(database)

    assert _schema_objects(database) == schema_before
    assert _metadata(database) == {
        "legacy_marker": "preserve-me",
        "schema_version": "0",
    }


# endregion [02]
