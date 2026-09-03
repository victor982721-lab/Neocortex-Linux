"""Focused fixture coverage for the additive Code graph generation contract."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.code import code_schema
from neocortex.code.code_graph_generations import (
    CodeGraphGenerationStore,
    CodeInput,
    GenerationConflict,
    GenerationHeadConflict,
    GenerationSchemaError,
    GraphMembership,
)
from neocortex.code.code_schema import initialize_code_state
from neocortex.code.code_state import CodeState


def test_generation_fixture_replay_checkpoint_and_head_cas_leave_legacy_reader_unchanged(
    tmp_path: Path,
) -> None:
    """A prepared graph becomes visible only after an explicit head CAS."""

    code_owner = tmp_path / "code.sqlite3"
    initialize_code_state(code_owner)
    inputs = (
        CodeInput("file:b", "digest-b", 2, "b.py", {"kind": "source"}),
        CodeInput("file:a", "digest-a", 1, "a.py", {"kind": "source"}),
    )
    with CodeState(code_owner) as state:
        store = state.graph_generation_store
        snapshot = store.create_input_snapshot(
            "snapshot:one", 10, inputs, metadata={"fixture": True}, created_ns=1
        )
        assert snapshot.input_count == 2
        assert snapshot.status == "sealed"
        assert (
            store.create_input_snapshot(
                "snapshot:one",
                10,
                reversed(inputs),
                metadata={"fixture": True},
                created_ns=2,
            )
            == snapshot
        )
        assert store.get_input_items("snapshot:one") == tuple(
            sorted(inputs, key=lambda item: item.key)
        )

        store.start_generation("snapshot:one", "generation:one", created_ns=2)
        first_batch = store.append_batch(
            "generation:one",
            0,
            (
                GraphMembership("symbol:b", "symbol-digest-b", 2),
                GraphMembership("symbol:a", "symbol-digest-a", 1),
            ),
            cursor="file:a",
            created_ns=3,
        )
        assert (
            store.append_batch(
                "generation:one",
                0,
                (
                    GraphMembership("symbol:a", "symbol-digest-a", 1),
                    GraphMembership("symbol:b", "symbol-digest-b", 2),
                ),
                cursor="file:a",
                created_ns=4,
            )
            == first_batch
        )
        checkpoint = store.checkpoint("generation:one", 0, "file:a", created_ns=5)
        assert checkpoint.checkpoint_index == 0
        assert (
            store.checkpoint("generation:one", 0, "file:a", checkpoint_index=0, created_ns=6)
            == checkpoint
        )

        completed = store.complete_generation("generation:one", completed_ns=7)
        assert completed.status == "completed"
        assert completed.generation_digest
        head = store.compare_and_swap_head(
            "default",
            expected_revision=0,
            expected_generation_id=None,
            generation_id="generation:one",
        )
        assert head.revision == 1
        assert store.get_head() == head
        assert store.list_memberships("generation:one") == (
            GraphMembership("symbol:a", "symbol-digest-a", 1, {}),
            GraphMembership("symbol:b", "symbol-digest-b", 2, {}),
        )

        store.create_input_snapshot("snapshot:two", 11, inputs, created_ns=8)
        store.start_generation("snapshot:two", "generation:two", created_ns=9)
        store.append_batch(
            "generation:two",
            0,
            (GraphMembership("symbol:c", "symbol-digest-c", 3),),
            cursor="file:c",
            created_ns=10,
        )
        store.checkpoint("generation:two", 0, "file:c", created_ns=11)
        second = store.complete_generation("generation:two", completed_ns=12)
        promoted = store.compare_and_swap_head(
            "default",
            expected_revision=head.revision,
            expected_generation_id=head.generation_id,
            generation_id=second.generation_id,
        )
        assert promoted.revision == 2
        with pytest.raises(GenerationHeadConflict):
            store.compare_and_swap_head(
                "default",
                expected_revision=head.revision,
                expected_generation_id=head.generation_id,
                generation_id=second.generation_id,
            )
        with pytest.raises(GenerationConflict):
            store.create_input_snapshot("snapshot:one", 10, inputs, metadata={"fixture": "changed"})

    with sqlite3.connect(code_owner) as legacy_reader:
        table_names = {
            str(row[0])
            for row in legacy_reader.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "graph_generations" in table_names
    assert "graph_heads" in table_names
    # Existing readers still see the same product tables; no new owner or
    # legacy table is introduced for graph generation state.
    assert "files" in table_names and "file_versions" in table_names


def test_additive_graph_schema_migration_preserves_code_rows(tmp_path: Path) -> None:
    """A pre-generation current Code owner is upgraded without data loss."""

    database = tmp_path / "code-v7.sqlite3"
    with sqlite3.connect(database) as connection:
        code_schema._execute(connection, code_schema._CURRENT_V1_DDL)
        code_schema._execute(connection, code_schema._PRODUCT_V2_DDL)
        for version in range(1, 8):
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, f"fixture-v{version}", version),
            )
        connection.execute("INSERT INTO metadata VALUES('schema_version','7')")
        connection.execute("PRAGMA user_version=7")
        connection.execute("INSERT INTO metadata VALUES('fixture-key','fixture-value')")
        connection.commit()

    initialize_code_state(database)

    with CodeState(database) as state:
        assert (
            state.connection.execute(
                "SELECT value FROM metadata WHERE key='fixture-key'"
            ).fetchone()[0]
            == "fixture-value"
        )
        store = CodeGraphGenerationStore(state.connection)
        assert store.get_head() is None
        assert (
            state.connection.execute("SELECT version FROM graph_generation_migrations").fetchall()[
                0
            ][0]
            == 1
        )


def test_generation_store_rejects_future_generation_metadata(tmp_path: Path) -> None:
    database = tmp_path / "future-code.sqlite3"
    initialize_code_state(database)
    with CodeState(database) as state:
        state.connection.execute(
            "UPDATE graph_generation_metadata SET value='99' WHERE key='schema_version'"
        )
        state.connection.commit()
        with pytest.raises(GenerationSchemaError, match="unsupported"):
            CodeGraphGenerationStore(state.connection)
