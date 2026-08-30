"""Bounded generational retention for the Code owner."""

from __future__ import annotations

from pathlib import Path

from neocortex.code.code_retention import (
    CodeRetentionPolicy,
    apply_code_retention,
    plan_code_retention,
)
from neocortex.code.code_schema import connect_code_state, initialize_code_state
from neocortex.code.code_state import CodeState


def _database(tmp_path: Path):
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


def _insert_tool(database: Path, run_id: int, *, tool_name: str = "fixture") -> int:
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            cursor = connection.execute(
                """INSERT INTO external_tool_runs(
                analysis_run_id,tool_name,tool_version,configuration_signature,
                status,started_ns,completed_ns,provenance_json)
                VALUES(?,?,?,'config','completed',?,?, '{}')""",
                (run_id, tool_name, "1", run_id, run_id + 1),
            )
            tool_run_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO external_run_counters(tool_run_id,name,value) VALUES(?,?,?)",
                (tool_run_id, "rows", 1),
            )
            return tool_run_id
    finally:
        connection.close()


def _insert_receipt(database: Path, run_id: int) -> None:
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            connection.execute(
                """INSERT INTO code_experiment_receipts(
                receipt_id,analysis_run_id,source_evaluation_id,question_id,
                subject_key,proposal_id,template_id,template_version,
                source_processing_signature,review_digest,envelope_digest,
                receipt_schema,receipt_status,payload_json,payload_xxh3_128,
                payload_xxh3_64_guard,payload_bytes,recorded_ns,authority,
                mutation_authority)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"receipt-{run_id}",
                    run_id,
                    "evaluation",
                    "question",
                    "subject",
                    "proposal",
                    "template",
                    "1",
                    "processing",
                    "review",
                    "envelope",
                    "neocortex.code-experiment-receipt/v4",
                    "passed",
                    "{}",
                    "digest",
                    "guard",
                    2,
                    run_id,
                    "advisory",
                    0,
                ),
            )
    finally:
        connection.close()


def test_plan_preserves_recent_runs_receipts_and_replay_sources(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 8)
    source_tool = _insert_tool(database, 1, tool_name="source")
    replay_tool = _insert_tool(database, 8, tool_name="replay")
    _insert_receipt(database, 2)
    connection = connect_code_state(database, create=False)
    try:
        with connection:
            connection.execute(
                """INSERT INTO external_run_replays(
                tool_run_id,source_tool_run_id,verification_signature,
                files_verified,bytes_verified)
                VALUES(?,?,?,1,1)""",
                (replay_tool, source_tool, "verification"),
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
        assert {1, 2, 7, 8}.issubset(set(plan.protected_run_ids))
        assert 1 not in plan.deletable_run_ids
        assert 2 not in plan.deletable_run_ids
        assert 3 in plan.deletable_run_ids
    finally:
        connection.close()


def test_apply_removes_only_run_scoped_rows_inside_owner_transaction(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_runs(database, 3)
    old_tool = _insert_tool(database, 1)
    receipt_tool = _insert_tool(database, 2)
    _insert_receipt(database, 2)
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
        assert result.deleted_run_ids == (1,)
        assert result.deleted_tool_runs == 1
        assert result.deleted_rows == 1
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM analysis_runs WHERE analysis_run_id=1"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM analysis_runs WHERE analysis_run_id=2"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM external_tool_runs WHERE tool_run_id=?", (old_tool,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM external_tool_runs WHERE tool_run_id=?", (receipt_tool,)
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM code_experiment_receipts WHERE analysis_run_id=2"
        ).fetchone() is not None
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
    old_tool = _insert_tool(database, 1)
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
        assert state.connection.execute(
            "SELECT 1 FROM analysis_runs WHERE analysis_run_id=1"
        ).fetchone() is None
        assert state.connection.execute(
            "SELECT 1 FROM external_tool_runs WHERE tool_run_id=?", (old_tool,)
        ).fetchone() is None
        state.connection.rollback()
