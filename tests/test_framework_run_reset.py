"""Contracts for the staged, Framework run-history-only reset."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence.framework_run_reset import (
    FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,
    FRAMEWORK_RUN_RESET_METADATA_KEY,
    FrameworkRunResetBusyError,
    FrameworkRunResetChangedError,
    FrameworkRunResetSchemaError,
    FrameworkRunResetStagingError,
    apply_framework_run_reset,
    framework_next_run_id,
    plan_framework_run_reset,
    read_framework_run_id_floor,
)
from neocortex.persistence.framework_state_writer import FrameworkState


def _framework_database(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "framework.sqlite3"
    with FrameworkState(path):
        pass
    return path


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _seed_history(connection: sqlite3.Connection, run_ids: tuple[int, ...] = (10, 20)) -> None:
    for run_id in run_ids:
        connection.execute(
            "INSERT INTO initial_runs(run_id,root,started_ns,status) VALUES(?,?,?,'completed')",
            (run_id, "/tmp/fixture-corpus", run_id),
        )
        connection.execute(
            "INSERT INTO run_events(event_id,run_id,occurred_ns,level,phase,message) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, run_id, run_id, "info", "fixture", "completed"),
        )
        connection.execute(
            "INSERT INTO route_runs(run_id,route_name,status,started_ns) VALUES(?,?,?,?)",
            (run_id, "text", "completed", run_id),
        )
        connection.execute(
            "INSERT INTO route_phase_runs(run_id,route_name,phase_name,status,started_ns) "
            "VALUES(?,?,?,?,?)",
            (run_id, "text", "extract", "completed", run_id),
        )
        connection.execute(
            "INSERT INTO run_actions(run_id,apply_actions,duplicate_candidates,"
            "duplicates_trashed,duplicate_skips,files_checked,types_detected,"
            "extensions_matching,unknown_types,type_cache_hits,type_cache_misses,"
            "type_cache_pruned,stale_inventory,rename_candidates,files_renamed,"
            "rename_skips,empty_directory_candidates,empty_directories_trashed,"
            "empty_directory_skips,errors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, *([0] * 19)),
        )
        connection.execute(
            "INSERT INTO route_candidates(run_id,mime,path,volume_id,file_id,size,"
            "mtime_ns,birthtime_ns) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "text/plain", f"/tmp/{run_id}.txt", "v", str(run_id), 1, 1, -1),
        )


def _stage(source: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    staged = sqlite3.connect(path)
    source.backup(staged)
    staged.execute("PRAGMA foreign_keys=ON")
    return staged


def test_preview_is_read_only_and_lists_only_run_history(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    connection = _open(path)
    _seed_history(connection)
    connection.commit()
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "initial_runs",
            "run_events",
            "route_runs",
            "route_phase_runs",
            "run_actions",
            "route_candidates",
        )
    }

    plan = plan_framework_run_reset(connection)

    assert plan.delete_run_ids == (10, 20)
    assert plan.retained_run_ids == ()
    assert plan.rows_to_delete == sum(before.values())
    assert plan.as_payload()["schema"] == "neocortex.framework-run-reset/v1"
    assert {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before
    } == before
    connection.close()


def test_apply_requires_staging_and_never_mutates_source(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    source = _open(path)
    _seed_history(source)
    source.commit()
    staged = _stage(source, tmp_path / "stage.sqlite3")
    plan = plan_framework_run_reset(staged)

    with pytest.raises(FrameworkRunResetStagingError):
        apply_framework_run_reset(staged, plan, staged=False)

    assert source.execute("SELECT COUNT(*) FROM initial_runs").fetchone() == (2,)
    assert staged.execute("SELECT COUNT(*) FROM initial_runs").fetchone() == (2,)
    source.close()
    staged.close()


def test_apply_removes_history_but_keeps_caches_and_allocator_floor(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    source = _open(path)
    _seed_history(source)
    source.commit()
    staged = _stage(source, tmp_path / "stage.sqlite3")
    plan = plan_framework_run_reset(staged)

    result = apply_framework_run_reset(staged, plan, staged=True)

    assert result.verified is True
    assert result.deleted_run_ids == (10, 20)
    assert all(
        staged.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        for table in (
            "initial_runs",
            "run_events",
            "route_runs",
            "route_phase_runs",
            "run_actions",
            "route_candidates",
        )
    )
    assert read_framework_run_id_floor(staged) == 20
    assert framework_next_run_id(staged) == 21
    assert (
        staged.execute(
            "SELECT value FROM metadata WHERE key=?", (FRAMEWORK_RUN_RESET_METADATA_KEY,)
        ).fetchone()
        is not None
    )
    assert staged.execute(
        "SELECT value FROM metadata WHERE key=?", (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY,)
    ).fetchone() == ("20",)
    # The source was only used as a backup input; it remains intact.
    assert source.execute("SELECT COUNT(*) FROM initial_runs").fetchone() == (2,)
    source.close()
    staged.close()


def test_preserved_references_retain_their_parent_and_are_reported(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    source = _open(path)
    _seed_history(source)
    source.execute(
        "INSERT INTO file_actions(action_id,run_id,action_type,source_path,"
        "apply_requested,status,started_ns) VALUES(?,?,?,?,?,?,?)",
        (1, 10, "review", "/tmp/10.txt", 0, "recovery_required", 1),
    )
    source.execute(
        "INSERT INTO content_type_cache(volume_id,file_id,size,mtime_ns,detector_version,"
        "status,last_seen_run_id,updated_ns) VALUES(?,?,?,?,?,?,?,?)",
        ("v", "10", 1, 1, "fixture", "unknown", 10, 1),
    )
    source.commit()
    staged = _stage(source, tmp_path / "stage.sqlite3")
    plan = plan_framework_run_reset(staged)

    assert plan.delete_run_ids == (20,)
    assert plan.retained_run_ids == (10,)
    action_reference = next(item for item in plan.references if item.key == "file_actions.run_id")
    assert action_reference.deleted_run_ids == ()
    assert action_reference.retained_run_ids == (10,)
    assert plan.recovery_run_ids == (10,)

    apply_framework_run_reset(staged, plan, staged=True)

    assert staged.execute("SELECT run_id FROM initial_runs").fetchall() == [(10,)]
    assert staged.execute("SELECT run_id FROM file_actions").fetchall() == [(10,)]
    assert staged.execute("SELECT last_seen_run_id FROM content_type_cache").fetchall() == [(10,)]
    assert framework_next_run_id(staged) == 21
    source.close()
    staged.close()


def test_review_rows_are_never_reset_and_retain_their_run_parent(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    source = _open(path)
    _seed_history(source)
    source.execute(
        "INSERT INTO review_candidates(route_name,volume_id,file_id,reason_code,path,"
        "size,mtime_ns,birthtime_ns,source_status,recommendation,retryable,confidence,"
        "evidence_json,detector_version,status,first_detected_ns,last_detected_ns,"
        "last_seen_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "text",
            "v",
            "10",
            "fixture",
            "/tmp/10.txt",
            1,
            1,
            -1,
            "partial",
            "manual_review",
            1,
            0.5,
            "{}",
            "fixture",
            "open",
            1,
            1,
            10,
        ),
    )
    source.commit()
    staged = _stage(source, tmp_path / "stage.sqlite3")
    plan = plan_framework_run_reset(staged)

    review_reference = next(
        item for item in plan.references if item.key == "review_candidates.last_seen_run_id"
    )
    assert review_reference.retained_run_ids == (10,)
    apply_framework_run_reset(staged, plan, staged=True)

    assert staged.execute("SELECT last_seen_run_id FROM review_candidates").fetchall() == [(10,)]
    assert staged.execute("SELECT run_id FROM initial_runs").fetchall() == [(10,)]
    source.close()
    staged.close()


def test_active_run_and_plan_drift_fail_closed(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    source = _open(path)
    _seed_history(source)
    source.execute("UPDATE initial_runs SET status='running' WHERE run_id=10")
    source.commit()
    staged = _stage(source, tmp_path / "stage.sqlite3")
    plan = plan_framework_run_reset(staged)

    with pytest.raises(FrameworkRunResetBusyError):
        apply_framework_run_reset(staged, plan, staged=True)

    staged.execute("UPDATE initial_runs SET status='completed' WHERE run_id=10")
    staged.commit()
    changed_plan = plan_framework_run_reset(staged)
    staged.execute("UPDATE run_events SET message='changed' WHERE event_id=20")
    staged.commit()
    with pytest.raises(FrameworkRunResetChangedError):
        apply_framework_run_reset(staged, changed_plan, staged=True)
    source.close()
    staged.close()


def test_invalid_floor_is_not_repaired_implicitly(tmp_path: Path) -> None:
    path = _framework_database(tmp_path)
    connection = _open(path)
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?)",
        (FRAMEWORK_RUN_ID_FLOOR_METADATA_KEY, "not-a-number"),
    )
    connection.commit()

    with pytest.raises(FrameworkRunResetSchemaError, match="floor"):
        plan_framework_run_reset(connection)
    connection.close()
