# region [00] Contexto del módulo
# Módulo: tests/test_semantic_schema_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic import semantic_schema
from neocortex.semantic import semantic_state
# endregion [01]

# region [02] Implementación


def _create_version_two(path: Path) -> None:
    with semantic_schema.semantic_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        semantic_schema._migrate_to_v1(connection, 1)
        semantic_schema._migrate_to_v2(connection, 2)
        connection.execute("PRAGMA user_version=2")
        connection.execute("INSERT INTO metadata(key,value) VALUES('schema_version','2')")


def _mutate(path: Path, sql: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(sql)
        connection.commit()
    finally:
        connection.close()


def test_semantic_state_facade_reexports_schema_lifecycle_contract() -> None:
    assert semantic_state.SEMANTIC_SCHEMA_VERSION == 7
    assert semantic_state.SemanticStateError is semantic_schema.SemanticStateError
    assert semantic_state.semantic_database is semantic_schema.semantic_database
    assert semantic_state.initialize_semantic_state is semantic_schema.initialize_semantic_state
    assert semantic_state._migrate_to_v1 is semantic_schema._migrate_to_v1
    assert semantic_state._migrate_to_v2 is semantic_schema._migrate_to_v2


@pytest.mark.parametrize(
    ("malformation", "expected_detail"),
    (
        (
            "DROP TABLE text_channel_revisions",
            "required table 'text_channel_revisions' is missing",
        ),
        (
            "ALTER TABLE text_channel_revisions DROP COLUMN revision_token",
            "missing column 'revision_token'",
        ),
        (
            "DROP INDEX semantic_evidence_model_refresh_idx",
            "required index 'semantic_evidence_model_refresh_idx' is missing",
        ),
        (
            """DROP INDEX semantic_evidence_item_idx;
            CREATE INDEX semantic_evidence_item_idx
            ON semantic_evidence(
                item_id,ontology_id,ontology_version,active,rank,score ASC
            )""",
            "index 'semantic_evidence_item_idx' has incompatible columns",
        ),
        (
            "CREATE TABLE unexpected_semantic_state(value TEXT)",
            "unexpected table 'unexpected_semantic_state'",
        ),
        (
            "ALTER TABLE text_channel_revisions ADD COLUMN unexpected TEXT",
            "table 'text_channel_revisions' has incompatible columns",
        ),
        (
            "CREATE INDEX unexpected_semantic_index ON semantic_items(item_id)",
            "table 'semantic_items' has unexpected indexes",
        ),
    ),
)
def test_declared_current_malformed_schema_is_rejected_without_repair(
    tmp_path: Path,
    malformation: str,
    expected_detail: str,
) -> None:
    database = tmp_path / "malformed-current.sqlite3"
    semantic_schema.initialize_semantic_state(database)
    _mutate(database, malformation)
    before = database.read_bytes()

    with pytest.raises(semantic_schema.SemanticStateError, match=expected_detail):
        semantic_schema.initialize_semantic_state(database)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


@pytest.mark.parametrize(
    "metadata_change",
    (
        "DELETE FROM metadata WHERE key='schema_version'",
        "UPDATE metadata SET value='4' WHERE key='schema_version'",
        "UPDATE metadata SET value='05' WHERE key='schema_version'",
    ),
)
def test_declared_current_requires_exact_version_metadata_without_mutation(
    tmp_path: Path,
    metadata_change: str,
) -> None:
    database = tmp_path / "bad-metadata.sqlite3"
    semantic_schema.initialize_semantic_state(database)
    _mutate(database, metadata_change)
    before = database.read_bytes()

    with pytest.raises(semantic_schema.SemanticStateError):
        semantic_schema.initialize_semantic_state(database)

    assert database.read_bytes() == before


def test_declared_current_requires_complete_migration_history_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bad-history.sqlite3"
    semantic_schema.initialize_semantic_state(database)
    _mutate(database, "DELETE FROM schema_migrations WHERE version=4")
    before = database.read_bytes()

    with pytest.raises(
        semantic_schema.SemanticStateError,
        match="semantic migration history",
    ):
        semantic_schema.initialize_semantic_state(database)

    assert database.read_bytes() == before


def test_declared_current_initialization_is_byte_stable(tmp_path: Path) -> None:
    database = tmp_path / "current.sqlite3"
    semantic_schema.initialize_semantic_state(database)
    before = database.read_bytes()

    semantic_schema.initialize_semantic_state(database)

    assert database.read_bytes() == before


def test_migration_failure_rolls_back_every_intermediate_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.sqlite3"
    _create_version_two(database)

    def fail_v4(connection: sqlite3.Connection, applied_ns: int) -> None:
        del applied_ns
        connection.execute("CREATE TABLE migration_must_rollback(value INTEGER)")
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setitem(semantic_schema._MIGRATIONS_BY_TARGET, 4, fail_v4)

    with pytest.raises(
        semantic_schema.SemanticStateError,
        match="initialization from version 2 failed",
    ):
        semantic_schema.initialize_semantic_state(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("2",)
        assert "source_revision_json" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(semantic_items)")
        }
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='migration_must_rollback'"
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == (1, 2)


def test_new_schema_records_exact_complete_migration_history(tmp_path: Path) -> None:
    database = tmp_path / "new.sqlite3"
    semantic_schema.initialize_semantic_state(database)

    with semantic_schema.semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "7"
        )
        assert tuple(
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ) == (1, 2, 3, 4, 5, 6, 7)


# endregion [02]
