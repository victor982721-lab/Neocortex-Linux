"""Focused regressions for the Semantic v9 -> v10 text_chunks layout swap."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.semantic import semantic_schema
from neocortex.semantic.semantic_lineage_repository import read_semantic_derivation_outbox
from neocortex.semantic.semantic_models import EmbeddingModelSpec, TextChunk
from neocortex.semantic.semantic_state import (
    enqueue_text_chunk_jobs,
    finalize_embedding_generation,
    initialize_semantic_state,
    register_embedding_model,
    semantic_database,
)
from tests.test_semantic_derivation_lineage import _execute, _generation, _stage
from tests.test_semantic_state import _text_model


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_CHUNK_COLUMNS = (
    "chunk_id",
    "item_id",
    "ordinal",
    "section_kind",
    "section_id",
    "start_char",
    "end_char",
    "text_zlib",
    "text_chars",
    "content_xxh3_128",
    "content_bytes",
    "content_xxh3_64_guard",
    "chunking_signature",
    "provenance_json",
    "refresh_token",
    "active",
    "updated_ns",
)
_CHUNK_TYPES = (
    "TEXT",
    "TEXT",
    "INTEGER",
    "TEXT",
    "TEXT",
    "INTEGER",
    "INTEGER",
    "BLOB",
    "INTEGER",
    "TEXT",
    "INTEGER",
    "TEXT",
    "TEXT",
    "TEXT",
    "TEXT",
    "INTEGER",
    "INTEGER",
)
_SOURCE_TRIGGERS = (
    "semantic_items_embedding_jobs_source_dirty_insert",
    "semantic_items_embedding_jobs_source_dirty_update",
    "semantic_items_embedding_jobs_source_dirty_delete",
    "text_chunks_embedding_jobs_source_dirty_insert",
    "text_chunks_embedding_jobs_source_dirty_update",
    "text_chunks_embedding_jobs_source_dirty_delete",
)


def _create_schema_version(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for target in range(1, version + 1):
            getattr(semantic_schema, f"_migrate_to_v{target}")(connection, target)
            semantic_schema._store_schema_version(connection, target)
        connection.commit()


def _create_v9_owner(
    tmp_path: Path,
    *,
    item_id: str = "layout-v9-item",
) -> tuple[Path, EmbeddingModelSpec, TextChunk]:
    database = tmp_path / "semantic.sqlite3"
    _create_schema_version(database, 9)
    model = _text_model("layout-migration-model", "layout-migration-space")
    register_embedding_model(database, model, allow_test_provider=True)
    chunk = _stage(
        database,
        item_id=item_id,
        source_revision_id=f"revision:text:{item_id}:layout",
        text="contenido de fixture para migración física de text_chunks",
        ordinal=1,
    )
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature=f"layout-migration-generation:{item_id}",
        started_ns=100,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=110) == 1
    _execute(database, generation_id, now_ns=120)
    assert finalize_embedding_generation(database, generation_id, completed_ns=130).status == "ready"
    # The current generation writer also maintains this legacy table.  Rebuild
    # exactly one row from the published member so the migration exercises the
    # real child FK without depending on an implementation detail of the
    # fixture writer.
    with semantic_database(database) as connection:
        connection.execute(
            "DELETE FROM text_embeddings WHERE chunk_id=? AND model_signature=?",
            (chunk.chunk_id, model.model_signature),
        )
        inserted = connection.execute(
            """INSERT INTO text_embeddings(
                chunk_id,model_signature,payload_id,generation_id,
                content_xxh3_128,content_bytes,content_xxh3_64_guard,
                provenance_json,updated_ns)
            SELECT member.entity_id,member.model_signature,member.payload_id,
                member.generation_id,member.content_xxh3_128,member.content_bytes,
                member.content_xxh3_64_guard,member.provenance_json,member.updated_ns
            FROM embedding_generation_members member
            JOIN published_embedding_heads head
              ON head.model_signature=member.model_signature
             AND head.generation_id=member.generation_id
            WHERE member.generation_id=? AND member.entity_kind='text_chunk'
              AND member.entity_id=?""",
            (generation_id, chunk.chunk_id),
        )
        assert inserted.rowcount == 1
        highwater = connection.execute(
            "UPDATE sqlite_sequence SET seq=77 WHERE name='text_embeddings'"
        )
        assert highwater.rowcount == 1
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='text_embeddings'"
        ).fetchone()[0] == 77
    return database, model, chunk


def _chunk_rows(database: Path) -> tuple[tuple[object, ...], ...]:
    columns = ",".join(_CHUNK_COLUMNS)
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(f"SELECT {columns} FROM text_chunks ORDER BY chunk_id")
        )


def _domain_rows(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with semantic_database(database, readonly=True) as connection:
        return {
            "embeddings": tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM text_embeddings ORDER BY ref_id")
            ),
            "vector_payloads": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM vector_payloads ORDER BY payload_id"
                )
            ),
            "jobs": tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM embedding_jobs ORDER BY job_id")
            ),
            "generations": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM embedding_generations ORDER BY generation_id"
                )
            ),
            "members": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM embedding_generation_members ORDER BY member_id"
                )
            ),
            "semantic_item_revisions": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM semantic_item_revisions ORDER BY item_revision_id"
                )
            ),
            "semantic_chunk_revisions": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM semantic_chunk_revisions ORDER BY chunk_revision_id"
                )
            ),
            "semantic_chunk_derivations": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM semantic_chunk_derivations ORDER BY derivation_id"
                )
            ),
            "receipts": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM semantic_work_receipts ORDER BY receipt_id"
                )
            ),
            "outbox": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM semantic_derivation_outbox ORDER BY event_id"
                )
            ),
            "heads": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM published_embedding_heads ORDER BY model_signature"
                )
            ),
            "sqlite_sequence": tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM sqlite_sequence ORDER BY name")
            ),
        }


def _schema_history(database: Path) -> tuple[tuple[object, ...], ...]:
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,description,applied_ns FROM schema_migrations ORDER BY version"
            )
        )


def _text_chunk_table_info(database: Path) -> tuple[tuple[object, ...], ...]:
    with semantic_database(database, readonly=True) as connection:
        return tuple(tuple(row) for row in connection.execute("PRAGMA table_info(text_chunks)"))


def _table_list_row(database: Path) -> tuple[object, ...]:
    with semantic_database(database, readonly=True) as connection:
        row = next(
            (
                candidate
                for candidate in connection.execute("PRAGMA table_list")
                if str(candidate[1]) == "text_chunks"
            ),
            None,
        )
    assert row is not None
    return tuple(row)


def _text_embedding_foreign_keys(database: Path) -> tuple[tuple[object, ...], ...]:
    with semantic_database(database, readonly=True) as connection:
        return tuple(
            tuple(row) for row in connection.execute("PRAGMA foreign_key_list(text_embeddings)")
        )


def _trigger_sql(database: Path) -> dict[str, str]:
    with semantic_database(database, readonly=True) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
            if str(row[0]) in _SOURCE_TRIGGERS
        }


def test_v9_to_v10_preserves_chunk_columns_blob_and_domain_rows(tmp_path: Path) -> None:
    database, _model, _chunk = _create_v9_owner(tmp_path)
    before_chunks = _chunk_rows(database)
    before_domain = _domain_rows(database)
    before_history = _schema_history(database)
    before_fks = _text_embedding_foreign_keys(database)
    before_triggers = _trigger_sql(database)
    assert len(_CHUNK_COLUMNS) == 17
    assert before_chunks
    for table in (
        "embeddings",
        "vector_payloads",
        "semantic_item_revisions",
        "semantic_chunk_revisions",
        "semantic_chunk_derivations",
    ):
        assert before_domain[table]
    assert tuple(row[0] for row in before_history) == tuple(range(1, 10))
    assert any(row[2] == "text_chunks" and row[4] == "chunk_id" for row in before_fks)
    assert set(before_triggers) == set(_SOURCE_TRIGGERS)
    assert _table_list_row(database)[4] == 1

    initialize_semantic_state(database)

    assert _chunk_rows(database) == before_chunks
    assert _domain_rows(database) == before_domain
    after_history = _schema_history(database)
    assert after_history[:9] == before_history
    assert tuple(row[0] for row in after_history) == tuple(range(1, 11))
    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == 10
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    info = _text_chunk_table_info(database)
    assert tuple(str(row[1]) for row in info) == _CHUNK_COLUMNS
    assert tuple(str(row[2]).upper() for row in info) == _CHUNK_TYPES
    assert all(int(row[3]) == 1 for row in info)
    assert int(info[0][3]) == 1
    assert int(info[0][5]) == 1
    assert _table_list_row(database)[4] == 0
    assert _text_embedding_foreign_keys(database) == before_fks
    assert _trigger_sql(database) == before_triggers
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='__semantic_text_chunks_v10_candidate'"
        ).fetchone() is None


def test_empty_v9_to_v10_migration_is_idempotent_in_database_bytes(tmp_path: Path) -> None:
    database = tmp_path / "empty-v9.sqlite3"
    _create_schema_version(database, 9)

    initialize_semantic_state(database)
    after_first = database.read_bytes()
    initialize_semantic_state(database)
    after_second = database.read_bytes()

    assert after_second == after_first
    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == 10
        assert connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v10_writer_emits_wire_v2_after_physical_swap(tmp_path: Path) -> None:
    database, _model, _chunk = _create_v9_owner(tmp_path)
    initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        cursor = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_id),0) FROM semantic_derivation_outbox"
            ).fetchone()[0]
        )
    _stage(
        database,
        item_id="layout-v10-new-item",
        source_revision_id="revision:text:layout-v10-new",
        text="nuevo receipt después del swap físico",
        ordinal=2,
    )
    events = read_semantic_derivation_outbox(database, after_event_id=cursor, limit=100)
    assert events
    with semantic_database(database, readonly=True) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM semantic_derivation_outbox WHERE event_id>? "
            "ORDER BY event_id",
            (cursor,),
        ).fetchall()
    assert all(json.loads(str(row[0]))["schema"] == "neocortex.semantic-derivation-event/v2" for row in rows)
    assert all(event.payload["schema"] == "neocortex.semantic-derivation-event/v1" for event in events)


def test_v10_layout_rejects_null_and_duplicate_chunk_id_without_row_changes(tmp_path: Path) -> None:
    database, _model, chunk = _create_v9_owner(tmp_path)
    initialize_semantic_state(database)
    before = _chunk_rows(database)
    columns = ",".join(_CHUNK_COLUMNS)
    values = ",".join(_CHUNK_COLUMNS[1:])
    for candidate in ("NULL", "chunk_id"):
        with pytest.raises(sqlite3.IntegrityError):
            with semantic_database(database) as connection:
                connection.execute(
                    f"INSERT INTO text_chunks({columns}) SELECT {candidate},{values} "
                    "FROM text_chunks WHERE chunk_id=?",
                    (chunk.chunk_id,),
                )
    assert _chunk_rows(database) == before


def test_v10_recreated_source_dirty_triggers_work_and_rollback_is_atomic(tmp_path: Path) -> None:
    database, _model, chunk = _create_v9_owner(tmp_path)
    initialize_semantic_state(database)
    model = _text_model("layout-trigger-only-model", "layout-trigger-only-space")
    register_embedding_model(database, model, allow_test_provider=True)
    generation_id = _generation(
        database,
        model,
        chunk.chunking_signature,
        processing_signature="layout-trigger-probe",
        started_ns=200,
    )
    assert enqueue_text_chunk_jobs(database, generation_id, (chunk.chunk_id,), now_ns=210) == 1
    with semantic_database(database) as connection:
        connection.execute("UPDATE embedding_jobs SET source_dirty=0 WHERE generation_id=?", (generation_id,))
    trigger_sql = _trigger_sql(database)
    assert set(trigger_sql) == set(_SOURCE_TRIGGERS)
    assert all("text_chunks" in sql for sql in trigger_sql.values())

    with semantic_database(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE text_chunks SET active=0,updated_ns=? WHERE chunk_id=?",
            (300, chunk.chunk_id),
        )
        assert connection.execute(
            "SELECT source_dirty FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == 1
        connection.rollback()
        final = connection.execute(
            "SELECT active,source_dirty FROM text_chunks JOIN embedding_jobs "
            "ON embedding_jobs.entity_id=text_chunks.chunk_id WHERE embedding_jobs.generation_id=?",
            (generation_id,),
        ).fetchone()
        assert final is not None and tuple(final) == (1, 0)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE semantic_items SET path=? WHERE item_id=?",
            ("/fixture/layout-trigger-moved.txt", chunk.item_id),
        )
        assert connection.execute(
            "SELECT source_dirty FROM embedding_jobs WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == 1
        connection.rollback()
        final = connection.execute(
            "SELECT active,source_dirty FROM text_chunks JOIN embedding_jobs "
            "ON embedding_jobs.entity_id=text_chunks.chunk_id WHERE embedding_jobs.generation_id=?",
            (generation_id,),
        ).fetchone()
        assert final is not None and tuple(final) == (1, 0)


@pytest.mark.parametrize("version", (7, 8, 9, 10))
def test_semantic_read_schema_returns_real_version_for_v7_v8_v9_v10(
    tmp_path: Path,
    version: int,
) -> None:
    if version < 10:
        database = tmp_path / f"semantic-v{version}.sqlite3"
        _create_schema_version(database, version)
    else:
        database, _model, _chunk = _create_v9_owner(tmp_path)
        initialize_semantic_state(database)
    with semantic_database(database, readonly=True) as connection:
        assert semantic_schema._validate_semantic_read_schema(connection) == version
        metadata = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
    assert metadata is not None
    assert str(metadata[0]) == str(version)


def test_future_v11_read_is_typed_and_does_not_migrate(tmp_path: Path) -> None:
    database, _model, _chunk = _create_v9_owner(tmp_path)
    initialize_semantic_state(database)
    with semantic_database(database) as connection:
        connection.execute("PRAGMA user_version=11")
        connection.execute("UPDATE metadata SET value='11' WHERE key='schema_version'")
    before = _chunk_rows(database)
    with semantic_database(database, readonly=True) as connection:
        with pytest.raises(semantic_schema.SemanticStateError):
            semantic_schema._validate_semantic_read_schema(connection)
    assert _chunk_rows(database) == before


def test_v9_to_v10_rolls_back_swap_when_post_migration_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _model, _chunk = _create_v9_owner(tmp_path)
    before_chunks = _chunk_rows(database)
    before_domain = _domain_rows(database)
    real_execute_migration = semantic_schema._execute_migration

    def fail_v10(
        connection: sqlite3.Connection,
        statements: tuple[str, ...],
        *,
        version: int,
        description: str,
        applied_ns: int,
    ) -> None:
        if version == 10:
            raise RuntimeError("fixture post-swap migration failure")
        real_execute_migration(
            connection,
            statements,
            version=version,
            description=description,
            applied_ns=applied_ns,
        )

    monkeypatch.setattr(semantic_schema, "_execute_migration", fail_v10)
    with pytest.raises(RuntimeError, match="post-swap migration"):
        initialize_semantic_state(database)
    assert _chunk_rows(database) == before_chunks
    assert _domain_rows(database) == before_domain
    assert _table_list_row(database)[4] == 1
    assert tuple(row[0] for row in _schema_history(database)) == tuple(range(1, 10))


def test_v9_to_v10_copy_fault_rolls_back_before_drop_and_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _model, chunk = _create_v9_owner(tmp_path)
    before_chunks = _chunk_rows(database)
    before_domain = _domain_rows(database)
    before_history = _schema_history(database)
    before_info = _text_chunk_table_info(database)
    before_triggers = _trigger_sql(database)
    before_fks = _text_embedding_foreign_keys(database)
    real_verify = semantic_schema._verify_text_chunks_v10_copy

    def corrupt_candidate_then_verify(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE __semantic_text_chunks_v10_candidate SET text_zlib=? WHERE chunk_id=?",
            (b"corrupt-v10-copy", chunk.chunk_id),
        )
        real_verify(connection)

    monkeypatch.setattr(
        semantic_schema,
        "_verify_text_chunks_v10_copy",
        corrupt_candidate_then_verify,
    )
    with pytest.raises(semantic_schema.SemanticStateError, match="copy value mismatch"):
        initialize_semantic_state(database)

    assert _chunk_rows(database) == before_chunks
    assert _domain_rows(database) == before_domain
    assert _schema_history(database) == before_history
    assert _text_chunk_table_info(database) == before_info
    assert _trigger_sql(database) == before_triggers
    assert _text_embedding_foreign_keys(database) == before_fks
    assert _table_list_row(database)[4] == 1
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='__semantic_text_chunks_v10_candidate'"
        ).fetchone() is None


def test_v9_to_v10_fk_trace_keeps_enforcement_and_rejects_new_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _model, _chunk = _create_v9_owner(tmp_path)
    trace: list[str] = []
    real_configure = semantic_schema._configure_common_connection

    def traced_configure(connection: sqlite3.Connection) -> None:
        connection.set_trace_callback(trace.append)
        real_configure(connection)

    monkeypatch.setattr(
        semantic_schema,
        "_configure_common_connection",
        traced_configure,
    )
    initialize_semantic_state(database)

    normalized = tuple("".join(statement.split()).upper() for statement in trace)
    assert "PRAGMAFOREIGN_KEYS=OFF" not in normalized
    assert "PRAGMADEFER_FOREIGN_KEYS=ON" not in normalized
    assert "PRAGMADEFER_FOREIGN_KEYS=OFF" not in normalized
    fk_checks = tuple(
        index for index, statement in enumerate(normalized)
        if statement == "PRAGMAFOREIGN_KEY_CHECK"
    )
    commits = tuple(index for index, statement in enumerate(normalized) if statement == "COMMIT")
    assert commits
    assert len(fk_checks) >= 2
    assert fk_checks[-1] < commits[-1]

    columns = ",".join(_CHUNK_COLUMNS)
    placeholders = ",".join("?" for _ in _CHUNK_COLUMNS)
    with semantic_database(database, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        with semantic_database(database) as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            row = connection.execute(
                f"SELECT {columns} FROM text_chunks ORDER BY chunk_id LIMIT 1"
            ).fetchone()
            assert row is not None
            values = list(row)
            values[0] = "fk-trace-orphan"
            values[1] = "fk-trace-missing-item"
            connection.execute(
                f"INSERT INTO text_chunks({columns}) VALUES({placeholders})",
                values,
            )
