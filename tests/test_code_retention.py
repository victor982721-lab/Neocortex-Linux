"""Bounded retention for the product Code owner."""

from __future__ import annotations

import sqlite3
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


def test_plan_preserves_recent_runs_and_holds_legacy_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 8)
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE external_tool_runs(analysis_run_id INTEGER, tool_name TEXT, "
                "tool_version TEXT, configuration_signature TEXT, status TEXT, "
                "started_ns INTEGER, completed_ns INTEGER, provenance_json TEXT)"
            )
            connection.execute(
                "INSERT INTO external_tool_runs(analysis_run_id,tool_name,tool_version,configuration_signature,status,started_ns,completed_ns,provenance_json) VALUES(1,'legacy','1','config','completed',1,2,'{}')"
            )
        plan = plan_code_retention(
            connection,
            policy=CodeRetentionPolicy(
                keep_completed_runs=2,
                keep_incident_runs=0,
                minimum_age_ns=0,
                max_terminal_runs=64,
                batch_size=100,
            ),
            now_ns=100,
        )
        assert {7, 8}.issubset(set(plan.protected_run_ids))
        assert 1 not in plan.deletable_run_ids
        assert 3 in plan.deletable_run_ids
        assert next(item for item in plan.candidates if item.analysis_run_id == 1).held_by_replay
    finally:
        connection.close()


def test_apply_removes_only_unreferenced_run_rows_inside_owner_transaction(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)
    connection = connect_code_state(database, create=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = apply_code_retention(
            connection,
            policy=CodeRetentionPolicy(
                keep_completed_runs=1,
                keep_incident_runs=0,
                minimum_age_ns=0,
                max_terminal_runs=64,
                batch_size=100,
            ),
            now_ns=100,
        )
        assert result.dry_run is False
        assert result.deleted_run_ids == (1, 2)
        assert result.deleted_tool_runs == 0
        assert result.deleted_rows == 0
        connection.commit()
        assert connection.execute("SELECT 1 FROM analysis_runs WHERE analysis_run_id=1").fetchone() is None
        assert connection.execute("SELECT 1 FROM analysis_runs WHERE analysis_run_id=3").fetchone() is not None
    finally:
        connection.close()


def test_terminal_ceiling_overrides_reader_grace_period(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 6)
    connection = connect_code_state(database, create=False)
    try:
        plan = plan_code_retention(
            connection,
            policy=CodeRetentionPolicy(
                keep_completed_runs=1,
                keep_incident_runs=0,
                minimum_age_ns=10_000,
                max_terminal_runs=3,
                batch_size=2,
            ),
            now_ns=6,
        )
        assert plan.deletable_run_ids == (1, 2)
    finally:
        connection.close()


def test_code_state_applies_policy_at_run_boundary(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)
    policy = CodeRetentionPolicy(
        keep_completed_runs=1,
        keep_incident_runs=0,
        minimum_age_ns=0,
        max_terminal_runs=64,
        batch_size=100,
    )
    with CodeState(database, retention_policy=policy) as state:
        current_run_id = state.begin_run(99, 99, "new-run")
        assert current_run_id == 4
        assert state.connection.execute("SELECT 1 FROM analysis_runs WHERE analysis_run_id=1").fetchone() is None
        state.connection.rollback()


def test_retention_requires_analysis_runs_table() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="current Code owner schema"):
            plan_code_retention(connection)
    finally:
        connection.close()
