"""Code retention must preserve graph publication and lineage roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.code.code_retention import (
    CodeRetentionPolicy,
    apply_code_retention,
    plan_code_retention,
)
from neocortex.code.code_schema import connect_code_state, initialize_code_state
from neocortex.code.code_state import CodeState


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "code.sqlite3"
    initialize_code_state(database)
    return database


def _insert_runs(database: Path, count: int) -> None:
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            connection.executemany(
                """INSERT INTO analysis_runs(
                analysis_run_id,framework_run_id,scan_id,processing_signature,
                status,started_ns,completed_ns,summary_json)
                VALUES(?,?,?,?,'completed',?,?, '{}')""",
                (
                    (run_id, 10_000 + run_id, 20_000 + run_id, f"fixture-{run_id}", run_id, run_id)
                    for run_id in range(1, count + 1)
                ),
            )
    finally:
        connection.close()


def _policy() -> CodeRetentionPolicy:
    return CodeRetentionPolicy(
        keep_completed_runs=1,
        keep_incident_runs=0,
        minimum_age_ns=0,
        max_terminal_runs=64,
        batch_size=100,
    )


def test_published_graph_head_protects_its_old_source_run_and_apply_keeps_readability(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 4)

    with CodeState(database) as state:
        published = state.graph_generation_store.publish_legacy_graph(1)
        plan = plan_code_retention(state.connection, policy=_policy(), now_ns=100)

        assert published.source_run_id == 1
        assert 1 in plan.protected_run_ids
        assert plan.deletable_run_ids == (2, 3)

        state.connection.execute("BEGIN IMMEDIATE")
        result = apply_code_retention(
            state.connection,
            policy=_policy(),
            now_ns=100,
        )
        assert result.deleted_run_ids == (2, 3)
        state.connection.commit()

        retained = state.graph_generation_store.read_published()
        assert retained is not None
        assert retained.generation.metadata["source_run_id"] == 1
        assert state.connection.execute(
            "SELECT 1 FROM analysis_runs WHERE analysis_run_id=1"
        ).fetchone() is not None


def test_unheaded_snapshot_lineage_protects_source_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)

    with CodeState(database) as state:
        state.graph_generation_store.create_input_snapshot(
            "lineage:old",
            1,
            (),
            created_ns=1,
        )
        plan = plan_code_retention(state.connection, policy=_policy(), now_ns=100)

        assert 1 in plan.protected_run_ids
        assert 1 not in plan.deletable_run_ids
        assert plan.deletable_run_ids == (2,)


def test_generation_metadata_lineage_is_checked_even_without_typed_snapshot_source(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)

    with CodeState(database) as state:
        store = state.graph_generation_store
        store.create_input_snapshot("lineage:metadata", 0, (), created_ns=1)
        store.start_generation(
            "lineage:metadata",
            "generation:metadata",
            metadata={"source_run_id": 1},
            created_ns=2,
        )
        plan = plan_code_retention(state.connection, policy=_policy(), now_ns=100)

        assert 1 in plan.protected_run_ids
        assert 1 not in plan.deletable_run_ids


def test_partial_graph_lineage_schema_abstains_before_planning_mutation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)

    with CodeState(database) as state:
        state.connection.execute("DROP TABLE graph_heads")
        state.connection.commit()
        with pytest.raises(RuntimeError, match="graph lineage; schema is incomplete"):
            plan_code_retention(state.connection, policy=_policy(), now_ns=100)
