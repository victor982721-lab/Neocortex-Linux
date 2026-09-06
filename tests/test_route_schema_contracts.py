# region [00] Contexto del módulo
# Módulo: tests/test_route_schema_contracts.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import sqlite3
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import state as archive_state
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
from neocortex.persistence.sqlite_schema_contract import SQLiteSchemaContractError
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


def _archive_artifact_hashes(path: Path) -> dict[str, str | None]:
    return {
        suffix: hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.exists() else None
        for suffix in ("", "-wal", "-shm", "-journal")
        for artifact in (Path(f"{path}{suffix}"),)
    }


def _archive_v1_fixture(path: Path, *, journal_mode: str = "WAL") -> None:
    assert journal_mode in {"DELETE", "WAL"}
    payload = b"legacy archive text"
    compressed = zlib.compress(payload)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        archive_state._create_archive_v1_schema(connection)
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (("schema_version", "1"), ("legacy_marker", "preserve-me")),
        )
        connection.execute(
            """INSERT INTO containers(container_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,member_count,indexed_count,last_seen_run_id,updated_ns)
            VALUES('9:42','/fixture/archive.zip',100,1,-1,'legacy-signature','complete',1,1,1,1)"""
        )
        connection.execute(
            """INSERT INTO documents(file_key,container_key,path,container_path,
            member_chain,member_path,archive_depth,content_kind,media_type,size,
            compressed_size,crc32,mtime_ns,birthtime_ns,processing_signature,status,
            last_seen_run_id,updated_ns,text_zlib,text_chars)
            VALUES('archive:legacy','9:42','/fixture/archive.zip!/entry.txt',
            '/fixture/archive.zip','entry.txt','entry.txt',1,'txt','text/plain',
            ?,?,0,1,-1,'legacy-signature','indexed',1,1,?,?)""",
            (len(payload), len(compressed), compressed, len(payload)),
        )
        connection.execute(
            """INSERT INTO document_fts(file_key,path,container_path,container_name,
            member_chain,content_kind,body) VALUES('archive:legacy',
            '/fixture/archive.zip!/entry.txt','/fixture/archive.zip',
            'archive.zip','entry.txt','txt',?)""", (payload.decode(),),
        )
        connection.commit()
    finally:
        connection.close()


def _archive_preserved_rows(path: Path) -> tuple[tuple[object, ...], ...]:
    with archive_state.archive_database(path, readonly=True) as connection:
        return tuple(
            tuple(row) for row in connection.execute(
                """SELECT c.container_key,c.path,c.size,c.status,d.file_key,d.path,
                d.member_chain,d.text_zlib,d.text_chars,f.body
                FROM containers AS c JOIN documents AS d USING(container_key)
                JOIN document_fts AS f ON f.file_key=d.file_key ORDER BY d.file_key"""
            )
        )


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
    if label == "archive":
        # Archive v2 migrates a real, exact v1 layout. A current layout merely
        # relabelled as v0 is not a historical migration fixture.
        _archive_v1_fixture(database)
        rows_before = _archive_preserved_rows(database)
        initialize(database)
        assert _archive_preserved_rows(database) == rows_before
        assert _metadata(database) == {
            "legacy_marker": "preserve-me", "schema_version": str(version),
        }
        with archive_state.archive_database(database, readonly=True) as connection:
            assert tuple(connection.execute(
                """SELECT document_role,logical_document_chain,
                independently_organizable,independently_disposable FROM documents"""
            ).fetchone()) == ("archive_member", None, 0, 0)
        return
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label, initialize, _version, _index_name = route_schema
    if label == "archive":
        for journal_mode in ("DELETE", "WAL"):
            database = tmp_path / f"archive-{journal_mode}.sqlite3"
            _archive_v1_fixture(database, journal_mode=journal_mode)
            schema_before = _schema_objects(database)
            rows_before = _archive_preserved_rows(database)
            hashes_before = _archive_artifact_hashes(database)
            with monkeypatch.context() as patch:
                patch.setattr(archive_state, "_ARCHIVE_V2_DDL", (
                    *archive_state._ARCHIVE_V2_DDL,
                    "CREATE TABLE archive_injected_failure(duplicate,duplicate)",
                ))
                # All valid migration DDL runs first; this is a real mid-
                # transaction failure, not a preflight rejection/no-op.
                with pytest.raises(sqlite3.OperationalError, match="duplicate column name: duplicate"):
                    initialize(database)
            assert _archive_artifact_hashes(database) == hashes_before
            assert _schema_objects(database) == schema_before
            assert _archive_preserved_rows(database) == rows_before
            assert _metadata(database) == {
                "legacy_marker": "preserve-me", "schema_version": "1",
            }
        return
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


@pytest.mark.parametrize("journal_mode", ("DELETE", "WAL"))
def test_archive_empty_v0_bootstrap_preserves_metadata_and_journal_mode(
    tmp_path: Path, journal_mode: str,
) -> None:
    database = tmp_path / "archive-v0.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(f"PRAGMA journal_mode={journal_mode}")
        archive_state._create_archive_v0_bootstrap(connection)
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (("schema_version", "0"), ("legacy_marker", "preserve-me")),
        )
        connection.commit()
    finally:
        connection.close()
    expected_journal_bytes = b"\x02\x02" if journal_mode == "WAL" else b"\x01\x01"
    assert database.read_bytes()[18:20] == expected_journal_bytes
    initialize_archive_state(database)
    assert _metadata(database) == {
        "schema_version": str(ARCHIVE_SCHEMA_VERSION), "legacy_marker": "preserve-me",
    }
    # The immutable reader/snapshot itself reports DELETE even for a WAL
    # owner. Inspect the persisted owner's read/write version bytes instead.
    assert database.read_bytes()[18:20] == expected_journal_bytes
    with archive_state.archive_database(database, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


@pytest.mark.parametrize("layout", ("v1", "v2", "incomplete"))
def test_archive_v0_nonempty_layout_is_rejected_without_any_owner_byte_changes(
    tmp_path: Path, layout: str,
) -> None:
    database = tmp_path / "archive-mislabelled-v0.sqlite3"
    if layout == "v1":
        _archive_v1_fixture(database)
    elif layout == "v2":
        initialize_archive_state(database)
    connection = sqlite3.connect(database)
    try:
        if layout == "incomplete":
            archive_state._create_archive_v0_bootstrap(connection)
            connection.execute("CREATE TABLE documents(file_key TEXT PRIMARY KEY) WITHOUT ROWID")
        connection.execute(
            "INSERT INTO metadata VALUES('schema_version','0') "
            "ON CONFLICT(key) DO UPDATE SET value='0'"
        )
        connection.commit()
    finally:
        connection.close()
    hashes_before = _archive_artifact_hashes(database)
    with pytest.raises(SQLiteSchemaContractError, match="archive v0 metadata-only bootstrap"):
        initialize_archive_state(database)
    assert _archive_artifact_hashes(database) == hashes_before


# endregion [02]
