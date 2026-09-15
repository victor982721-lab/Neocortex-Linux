"""Hardening contracts for common owner retention planning.

These fixtures are deliberately disposable.  The common planner is a
read-only diagnostic: it must never turn an incomplete or unbounded owner
state into an eligible deletion claim, and it must not claim physical SQLite
recovery from logical payload estimates.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence import framework_schema
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.semantic import semantic_schema
from neocortex.workflow.retention import planner as retention_module
from neocortex.workflow.retention.planner import (
    RetentionPolicy,
    plan_retention,
    retention_plan_payload,
)
from tests.test_retention_planner import (
    NOW_NS,
    _populate_framework,
    _populate_semantic,
)


def test_accounting_separates_observed_proposed_and_physical_recovery(
    tmp_path: Path,
) -> None:
    _populate_semantic(tmp_path / "semantic.sqlite3")

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    store = plan.stores[0]
    payload = retention_plan_payload(plan)
    accounting = payload["accounting"]
    assert isinstance(accounting, dict)

    assert store.observed_rows >= store.proposed_rows >= store.eligible_rows
    assert store.observed_bytes >= store.proposed_bytes >= store.eligible_bytes
    assert store.retired_rows == store.retired_bytes == 0
    assert store.physically_recoverable_bytes is None
    assert store.physical_recovery_status == "not_verified"
    assert accounting["observed_rows"] == plan.observed_rows
    assert accounting["proposed_bytes"] == plan.proposed_bytes
    assert accounting["retired_bytes"] == 0
    assert accounting["physically_recoverable_bytes"] is None
    assert accounting["physical_recovery_status"] == "not_verified"
    assert payload["compaction_supported"] is False
    assert payload["deletion_supported"] is False


@pytest.mark.parametrize("store", ("semantic", "framework"))
def test_future_schema_marker_blocks_without_mutation(
    tmp_path: Path,
    store: str,
) -> None:
    database = tmp_path / f"{store}.sqlite3"
    if store == "semantic":
        _populate_semantic(database)
        future = semantic_schema.SEMANTIC_SCHEMA_VERSION + 1
    else:
        _populate_framework(database)
        future = framework_schema.SCHEMA_VERSION + 1

    with sqlite3.connect(database) as connection:
        if store == "semantic":
            connection.execute(f"PRAGMA user_version={future}")
        else:
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                (str(future),),
            )
    before = database.read_bytes()

    plan = plan_retention(tmp_path, stores=(store,), now_ns=NOW_NS)
    result = plan.stores[0]
    assert result.status == "blocked"
    assert result.items == ()
    assert "newer" in (result.detail or "") or "expected" in (result.detail or "")
    assert database.read_bytes() == before


def test_unknown_application_table_blocks_framework_owner(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    _populate_framework(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unknown_retention_extension(value TEXT)")
        connection.execute("INSERT INTO unknown_retention_extension VALUES('keep')")

    plan = plan_retention(tmp_path, stores=("framework",), now_ns=NOW_NS)
    result = plan.stores[0]
    assert result.status == "blocked"
    assert result.items == ()
    assert "unexpected table" in (result.detail or "")


def test_partial_and_recovery_states_are_not_eligible(tmp_path: Path) -> None:
    semantic = tmp_path / "semantic.sqlite3"
    _populate_semantic(semantic)
    with sqlite3.connect(semantic) as connection:
        connection.execute(
            "UPDATE embedding_generations SET status='ready_partial' "
            "WHERE generation_id=8"
        )

    semantic_plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    semantic_item = next(
        item for item in semantic_plan.stores[0].items if item.key == 8
    )
    assert semantic_item.disposition == "blocked"
    assert "partial_or_recovery_state" in semantic_item.reasons

    framework = tmp_path / "framework.sqlite3"
    _populate_framework(framework)
    with sqlite3.connect(framework) as connection:
        connection.execute(
            "UPDATE initial_runs SET status='recovery_required' WHERE run_id=1"
        )

    framework_plan = plan_retention(
        tmp_path,
        stores=("framework",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    framework_item = next(
        item for item in framework_plan.stores[0].items if item.key == 1
    )
    assert framework_item.disposition != "eligible"
    assert (
        "partial_or_recovery_state" in framework_item.reasons
        or "last_completed_run" in framework_item.reasons
    )


def test_source_reachability_is_bounded_and_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,completed_ns,status,run_kind,source_run_id)
            VALUES(?, 'C:/fixture', 1, 1, 'failed', 'initial', ?)""",
            ((run_id, run_id - 1 if run_id > 1 else None) for run_id in range(1, 4_201)),
        )

    plan = plan_retention(
        tmp_path,
        stores=("framework",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0, batch_size=10),
    )
    result = plan.stores[0]
    assert result.status == "blocked"
    assert result.items
    assert all(item.disposition != "eligible" for item in result.items)
    assert all("bounded_reachability_incomplete" in item.reasons for item in result.items)
    assert "reachability" in (result.detail or "")


def test_planner_does_not_compact_or_delete_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    before_names = {path.name for path in tmp_path.iterdir()}
    before = database.read_bytes()

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(minimum_age_ns=0),
    )
    json.dumps(retention_plan_payload(plan), sort_keys=True)

    assert database.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} - before_names <= {
        "semantic.sqlite3-shm",
        "semantic.sqlite3-wal",
    }
    assert not hasattr(retention_module, "apply_retention")
    assert not hasattr(retention_module, "compact_retention")
