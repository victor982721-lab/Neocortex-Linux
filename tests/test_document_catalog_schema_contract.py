# region [00] Contexto del módulo
# Módulo: tests/test_document_catalog_schema_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.document_catalog as catalog_module
import _04_Nucleo_Operativo.document_catalog_schema as catalog_schema_module
from _04_Nucleo_Operativo.document_catalog import (
    CATALOG_SCHEMA_VERSION,
    document_catalog_database,
    initialize_document_catalog,
)
from _04_Nucleo_Operativo.document_catalog_schema import (
    document_catalog_schema_contract,
)
from _04_Nucleo_Operativo.sqlite_schema_contract import (
    SQLiteSchemaContractError,
    validate_sqlite_schema_contract,
)
# endregion [01]

# region [02] Implementación


def _metadata_value(database: Path, key: str) -> str | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _create_legacy_catalog(database: Path, version: int) -> None:
    """Build the requested historical schema rather than forging its version."""

    if version < 1 or version >= CATALOG_SCHEMA_VERSION:
        raise ValueError("legacy catalog version must be supported and positive")
    with sqlite3.connect(database) as connection:
        catalog_schema_module._migrate_to_v1(connection)
        catalog_schema_module._set_schema_version(connection, 1)
        if version >= 2:
            catalog_schema_module._migrate_to_v2(connection)
            catalog_schema_module._set_schema_version(connection, 2)
        if version >= 3:
            catalog_schema_module._migrate_to_v3(connection, lambda _connection: None)
            catalog_schema_module._set_schema_version(connection, 3)
        if version >= 4:
            catalog_schema_module._migrate_to_v4(connection)
            catalog_schema_module._set_schema_version(connection, 4)
        if version >= 5:
            catalog_schema_module._migrate_to_v5(connection)
            catalog_schema_module._set_schema_version(connection, 5)
        if version >= 6:
            catalog_schema_module._migrate_to_v6(connection)
            catalog_schema_module._set_schema_version(connection, 6)
        connection.commit()


def _record_connection_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bool]:
    modes: list[bool] = []
    original = catalog_module.connect_document_catalog

    def recording_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        modes.append(readonly)
        return original(path, readonly=readonly)

    monkeypatch.setattr(catalog_module, "connect_document_catalog", recording_connect)
    return modes


def test_fresh_catalog_matches_contract_and_current_reopen_is_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with document_catalog_database(database, readonly=True) as connection:
        validate_sqlite_schema_contract(
            connection,
            document_catalog_schema_contract(),
            label="document catalog",
        )
    original_bytes = database.read_bytes()
    modes = _record_connection_modes(monkeypatch)

    initialize_document_catalog(database)

    assert modes == [True]
    assert database.read_bytes() == original_bytes
    assert _metadata_value(database, "schema_version") == str(CATALOG_SCHEMA_VERSION)


def test_malformed_current_catalog_is_rejected_without_writable_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX catalog_documents_review_idx")
        connection.commit()
    original_bytes = database.read_bytes()
    modes = _record_connection_modes(monkeypatch)

    with pytest.raises(SQLiteSchemaContractError, match="lacks indexes"):
        initialize_document_catalog(database)

    assert modes == [True]
    assert database.read_bytes() == original_bytes


@pytest.mark.parametrize(
    ("raw_version", "error_type", "message"),
    [
        (str(CATALOG_SCHEMA_VERSION + 1), RuntimeError, "newer than supported"),
        (f"0{CATALOG_SCHEMA_VERSION}", SQLiteSchemaContractError, "not canonical"),
    ],
)
def test_unknown_catalog_versions_are_rejected_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_version: str,
    error_type: type[Exception],
    message: str,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    initialize_document_catalog(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (raw_version,))
        connection.commit()
    original_bytes = database.read_bytes()
    modes = _record_connection_modes(monkeypatch)

    with pytest.raises(error_type, match=message):
        initialize_document_catalog(database)

    assert modes == [True]
    assert database.read_bytes() == original_bytes


@pytest.mark.parametrize("prior_version", range(1, CATALOG_SCHEMA_VERSION))
def test_every_legacy_version_migrates_without_losing_rows(
    tmp_path: Path,
    prior_version: int,
) -> None:
    database = tmp_path / f"catalog-v{prior_version}.sqlite3"
    _create_legacy_catalog(database, prior_version)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO metadata(key,value) VALUES('sentinel','preserved')")
        connection.execute(
            """INSERT INTO catalog_runs(
                framework_run_id,source_kind,mode,status,started_ns,summary_json
            ) VALUES(7,'pdf','incremental','complete',11,'{}')"""
        )
        connection.commit()

    initialize_document_catalog(database)

    with document_catalog_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT framework_run_id,source_kind,status FROM catalog_runs"
        ).fetchone()
        validate_sqlite_schema_contract(
            connection,
            document_catalog_schema_contract(),
            label="document catalog",
        )
    assert _metadata_value(database, "schema_version") == str(CATALOG_SCHEMA_VERSION)
    assert _metadata_value(database, "sentinel") == "preserved"
    assert tuple(row) == (7, "pdf", "complete")


@pytest.mark.parametrize("prior_version", [3, 4])
def test_structural_v3_and_v4_migrations_preserve_document_rows(
    tmp_path: Path,
    prior_version: int,
) -> None:
    database = tmp_path / f"catalog-structural-v{prior_version}.sqlite3"
    _create_legacy_catalog(database, prior_version)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
                source_kind,file_key,path,volume_id,file_id,size,mtime_ns,
                birthtime_ns,source_status,processing_signature,
                classifier_signature,primary_kind,confidence,uncertainty,
                standard_references_json,organizations_json,topics_json,
                classification_json,catalog_status,last_seen_catalog_run_id,
                updated_ns
            ) VALUES(
                'pdf','1:2','C:\\Normativa\\fixture.pdf','1','2',10,11,12,
                'complete','source-v1','classifier-v1','normativa',0.9,'baja',
                '[]','[]','[]','{}','classified',1,13
            )"""
        )
        connection.commit()

    initialize_document_catalog(database)

    with document_catalog_database(database, readonly=True) as connection:
        row = connection.execute(
            """SELECT path,clients_json,projects_json,workstreams_json,
            equipment_json,activities_json FROM documents"""
        ).fetchone()
    assert tuple(row) == (
        r"C:\Normativa\fixture.pdf",
        "[]",
        "[]",
        "[]",
        "[]",
        "[]",
    )


def test_failed_legacy_migration_rolls_back_all_partial_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "document_catalog.sqlite3"
    _create_legacy_catalog(database, 2)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO metadata(key,value) VALUES('sentinel','before')")
        connection.commit()

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE metadata SET value='partial' WHERE key='sentinel'")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(
        catalog_module,
        "_migrate_identity_text_to_decimal",
        fail_after_write,
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        initialize_document_catalog(database)

    assert _metadata_value(database, "schema_version") == "2"
    assert _metadata_value(database, "sentinel") == "before"


def _insert_v5_document(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            source_kind,file_key,path,volume_id,file_id,size,mtime_ns,birthtime_ns,
            source_status,processing_signature,text_fingerprint,classifier_signature,
            primary_kind,confidence,uncertainty,standard_references_json,
            organizations_json,topics_json,classification_json,catalog_status,
            active,last_seen_catalog_run_id,updated_ns)
            VALUES('pdf','1:2','C:\\Fixture\\legacy.pdf','1','2',10,11,12,
            'done','source-v1','text-v1','classifier-v1','normativa',0.9,'baja',
            '[]','[]','[]','{}','classified',1,7,13)"""
        )
        connection.commit()


def test_populated_v5_migrates_to_published_generation(tmp_path: Path) -> None:
    database = tmp_path / "catalog-v5-populated.sqlite3"
    _create_legacy_catalog(database, 5)
    _insert_v5_document(database)

    initialize_document_catalog(database)

    with document_catalog_database(database, readonly=True) as connection:
        published = connection.execute(
            """SELECT p.source_kind,g.status,d.path
            FROM catalog_publications AS p
            JOIN catalog_generations AS g USING(generation_id)
            JOIN catalog_generation_documents AS d USING(generation_id)
            WHERE d.source_kind=p.source_kind"""
        ).fetchone()
        projection = connection.execute(
            "SELECT source_kind,file_key,path,active FROM documents"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert tuple(published) == ("pdf", "published", r"C:\Fixture\legacy.pdf")
    assert tuple(projection) == ("pdf", "1:2", r"C:\Fixture\legacy.pdf", 1)
    assert integrity == "ok"
    assert foreign_keys == []
    migrated_bytes = database.read_bytes()
    initialize_document_catalog(database)
    assert database.read_bytes() == migrated_bytes


@pytest.mark.parametrize("unknown_kind", ["table", "column", "index", "trigger"])
def test_v5_migration_abstains_on_unknown_objects_without_writable_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_kind: str,
) -> None:
    database = tmp_path / f"catalog-v5-unknown-{unknown_kind}.sqlite3"
    _create_legacy_catalog(database, 5)
    with sqlite3.connect(database) as connection:
        if unknown_kind == "table":
            connection.execute("CREATE TABLE unknown_catalog_data(value TEXT)")
        elif unknown_kind == "column":
            connection.execute("ALTER TABLE documents ADD COLUMN unknown_value TEXT")
        elif unknown_kind == "index":
            connection.execute("CREATE INDEX unknown_catalog_index ON documents(updated_ns)")
        else:
            connection.execute(
                """CREATE TRIGGER unknown_catalog_trigger AFTER INSERT ON documents
                BEGIN SELECT 1; END"""
            )
        connection.commit()
    original_bytes = database.read_bytes()
    modes = _record_connection_modes(monkeypatch)

    with pytest.raises(SQLiteSchemaContractError, match=r"unexpected|incompatible"):
        initialize_document_catalog(database)

    assert modes == [True]
    assert database.read_bytes() == original_bytes
    assert _metadata_value(database, "schema_version") == "5"


@pytest.mark.parametrize(
    "injected",
    (
        RuntimeError("injected v6 migration failure"),
        KeyboardInterrupt("injected v6 migration interruption"),
    ),
    ids=("exception", "base-exception"),
)
def test_v5_to_v6_failure_rolls_back_populated_generation_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected: BaseException,
) -> None:
    database = tmp_path / "catalog-v5-rollback.sqlite3"
    _create_legacy_catalog(database, 5)
    _insert_v5_document(database)
    original = catalog_schema_module._migrate_to_v6

    def fail_after_generation_writes(connection: sqlite3.Connection) -> None:
        original(connection)
        raise injected

    monkeypatch.setattr(catalog_schema_module, "_migrate_to_v6", fail_after_generation_writes)

    with pytest.raises(type(injected), match="injected v6 migration"):
        initialize_document_catalog(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        generation_objects = connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
            WHERE name IN ('catalog_generations','catalog_publications',
            'catalog_generation_documents')"""
        ).fetchone()[0]
        path = connection.execute("SELECT path FROM documents").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert version == "5"
    assert generation_objects == 0
    assert path == r"C:\Fixture\legacy.pdf"
    assert integrity == "ok"


# endregion [02]
