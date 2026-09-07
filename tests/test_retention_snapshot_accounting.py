"""Retention reports bounded detached-view cost, not promised disk recovery."""

from pathlib import Path

import pytest

from neocortex.workflow.retention.planner import (
    RetentionPlanningCancelled,
    RetentionPolicy,
    plan_retention,
    retention_plan_payload,
)
from neocortex.workflow.retention import planner as retention_module
from tests.test_retention_planner import NOW_NS, _populate_catalog, _populate_semantic


def test_retention_rejects_oversized_snapshot_without_writing_temporary_bytes(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    before = path.read_bytes()
    plan = plan_retention(
        tmp_path, stores=("semantic",), now_ns=NOW_NS,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )
    assert plan.stores[0].status == "blocked"
    assert "temporary bytes budget exhausted" in str(plan.stores[0].detail)
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 0
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.stores[0].storage is None
    assert path.read_bytes() == before


def test_retention_uses_fenced_zero_copy_for_oversized_quiescent_semantic_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large quiescent owner must not need a full detached page snapshot."""

    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    before = path.read_bytes()
    # Lower the canonical optimization threshold in this small fixture rather
    # than allocating a multi-gigabyte test database.  The per-operation
    # temporary budget remains one byte, so a detached copy would be blocked.
    monkeypatch.setattr(retention_module, "DEFAULT_SQLITE_SNAPSHOT_MAX_TEMPORARY_BYTES", 1)

    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        now_ns=NOW_NS,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=1),
    )

    assert plan.stores[0].status == "ready"
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] == 0
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert path.read_bytes() == before


def test_retention_exposes_allocated_pages_without_claiming_physical_recovery(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    before = path.read_bytes()
    plan = plan_retention(tmp_path, stores=("semantic",), now_ns=NOW_NS)
    payload = retention_plan_payload(plan)
    storage = plan.stores[0].storage
    assert storage is not None
    assert storage["allocated_page_bytes"] == storage["allocated_pages"] * storage["page_size"]
    assert storage["physically_recoverable_bytes"] is None
    assert payload["source_sidecars_touched"] is False
    assert payload["deletion_supported"] is False
    assert payload["snapshot_budget_scope"] == "aggregate_retained_temporary_bytes_per_operation"
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert 0 < plan.snapshot_metrics["peak_temporary_bytes"] <= 256 * 1024 * 1024
    assert path.read_bytes() == before


def test_retention_cancellation_reaches_snapshot_preparation(tmp_path: Path) -> None:
    path = tmp_path / "semantic.sqlite3"
    _populate_semantic(path)
    before = path.read_bytes()
    checks = [0]

    def cancelled() -> bool:
        checks[0] += 1
        return checks[0] >= 3

    with pytest.raises(RetentionPlanningCancelled):
        plan_retention(tmp_path, stores=("semantic",), now_ns=NOW_NS, cancelled=cancelled)
    assert checks[0] >= 3
    assert path.read_bytes() == before


def test_retention_sql_deadline_marks_semantic_eligibility_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    before = database.read_bytes()
    monkeypatch.setattr(retention_module, "DEFAULT_RETENTION_SQL_TIMEOUT_SECONDS", 0.01)

    def expensive_holds(connection):
        connection.execute(
            "WITH RECURSIVE n(value) AS (VALUES(0) UNION ALL "
            "SELECT value+1 FROM n WHERE value < 50000000) SELECT sum(value) FROM n"
        ).fetchone()
        raise AssertionError("the bounded SQL query should have been interrupted")

    monkeypatch.setattr(retention_module, "_semantic_holds", expensive_holds)
    plan = plan_retention(
        tmp_path,
        stores=("semantic",),
        policy=RetentionPolicy(minimum_age_ns=0),
        now_ns=NOW_NS,
    )

    store = plan.stores[0]
    assert store.status == "blocked"
    assert store.items == ()
    assert "eligibility unknown" in (store.detail or "")
    assert database.read_bytes() == before


def test_retention_sql_cancellation_interrupts_semantic_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    _populate_semantic(database)
    before = database.read_bytes()
    cancellation = {"requested": False}

    def cancelled() -> bool:
        return cancellation["requested"]

    def expensive_holds(connection):
        cancellation["requested"] = True
        connection.execute(
            "WITH RECURSIVE n(value) AS (VALUES(0) UNION ALL "
            "SELECT value+1 FROM n WHERE value < 50000000) SELECT sum(value) FROM n"
        ).fetchone()
        raise AssertionError("the cancelled SQL query should have been interrupted")

    monkeypatch.setattr(retention_module, "_semantic_holds", expensive_holds)
    with pytest.raises(RetentionPlanningCancelled):
        plan_retention(
            tmp_path,
            stores=("semantic",),
            policy=RetentionPolicy(minimum_age_ns=0),
            now_ns=NOW_NS,
            cancelled=cancelled,
        )

    assert database.read_bytes() == before


def test_retention_holds_all_owner_views_under_one_aggregate_limit(tmp_path: Path) -> None:
    semantic = tmp_path / "semantic.sqlite3"
    catalog = tmp_path / "document_catalog.sqlite3"
    _populate_semantic(semantic)
    _populate_catalog(catalog)
    originals = {path: path.read_bytes() for path in (semantic, catalog)}
    limit = sum(len(data) for data in originals.values()) - 1
    assert all(len(data) < limit for data in originals.values())
    plan = plan_retention(
        tmp_path, stores=("semantic", "catalog"), now_ns=NOW_NS,
        policy=RetentionPolicy(snapshot_max_temporary_bytes=limit),
    )
    assert [store.status for store in plan.stores] == ["ready", "blocked"]
    assert plan.snapshot_metrics is not None
    assert plan.snapshot_metrics["prepared_views"] == 1
    assert plan.snapshot_metrics["peak_temporary_bytes"] <= limit
    assert plan.snapshot_metrics["retained_temporary_bytes"] == 0
    assert {path: path.read_bytes() for path in originals} == originals
