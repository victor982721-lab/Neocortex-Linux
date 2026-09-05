"""Durable Framework lifecycle budget regressions."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.orchestration.run_manifest import RunManifest
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.run_status import list_run_status
from neocortex.runtime.models import FrameworkConfig


def _run_with_budget(tmp_path: Path, budget: dict[str, object]) -> tuple[FrameworkState, int]:
    root = tmp_path / "fixture"
    root.mkdir()
    state = FrameworkState(tmp_path / "framework.sqlite3")
    run_id = state.begin_initial_run(root, None)
    manifest = RunManifest(
        run_id=run_id,
        run_kind="initial",
        root=str(root),
        root_identity=(1, 2, -1),
        selected_routes=("probe",),
        budget=budget,
    )
    assert state.publish_run_manifest(run_id, manifest.event_payload())
    return state, run_id


def test_budget_reservation_is_global_and_replay_idempotent(tmp_path: Path) -> None:
    state, run_id = _run_with_budget(tmp_path, {"max_items": 3, "max_bytes": 10})
    try:
        first = state.reserve_run_budget(run_id, "probe", items=2, bytes=8, worker="probe")
        replay = state.reserve_run_budget(run_id, "probe", items=2, bytes=8, worker="probe")
        assert first["consumed_items"] == replay["consumed_items"] == 2
        assert replay["replayed"] is True
        with pytest.raises(RunBudgetExceeded, match="items"):
            state.reserve_run_budget(run_id, "other", items=2)
    finally:
        state.close()


def test_budget_cancellation_is_durable_and_idempotent(tmp_path: Path) -> None:
    state, run_id = _run_with_budget(tmp_path, {"max_items": 20})
    try:
        assert state.request_run_cancellation(run_id, "fixture-stop")
        assert not state.request_run_cancellation(run_id, "fixture-stop")
        with pytest.raises(RunBudgetExceeded, match="cancelled"):
            state.reserve_run_budget(run_id, "probe", items=1)
        snapshot = state.read_run_budget(run_id)
        assert snapshot is not None
        assert snapshot["cancel_requested"] is True
        assert snapshot["cancel_reason"] == "fixture-stop"
    finally:
        state.close()


def test_budget_deadline_is_durable(tmp_path: Path) -> None:
    state, run_id = _run_with_budget(tmp_path, {"max_duration_seconds": 0.001})
    try:
        time.sleep(0.01)
        with pytest.raises(RunBudgetExceeded, match="time"):
            state.reserve_run_budget(run_id, "probe", items=1)
        snapshot = state.read_run_budget(run_id)
        assert snapshot is not None and snapshot["expired"] is True
    finally:
        state.close()


def test_abrupt_run_recovery_is_idempotent_and_retains_route_inputs(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, JournalCursor("C:", 1, 1))
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("probe",),
                budget={"durable": {"max_items": 5}},
            ).event_payload(),
        )
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        state.begin_route_runs(run_id, ("probe",))
        assert state.mark_abandoned_runs() == 1
        assert state.mark_abandoned_runs() == 0
        recovery = state.run_recovery_plan(run_id)
        assert recovery["pending"] == ["probe"]
        assert recovery["candidates_retained"] is False
        assert state._connection.execute(
            """SELECT COUNT(*) FROM run_events
            WHERE run_id=? AND phase='lifecycle-recovery'""",
            (run_id,),
        ).fetchone()[0] == 1

    status = list_run_status(database, run_id=run_id)[0]
    assert status.status == "interrupted"
    assert status.resumed is False
    assert status.replayed is False
    assert status.non_replayable_routes == ("probe",)
    assert status.recovery is not None
    assert status.budget is not None and status.budget["cancel_requested"] is True


def test_orchestrator_budget_stops_worker_before_route_effects(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    # Reuse the focused source fixture, which also binds the matching durable
    # inventory checkpoint and policy signature, without touching a real corpus.
    from tests.test_run_control import _source_run

    source_run = _source_run(state_directory / "framework.sqlite3", root)

    executed: list[int] = []

    def route(context):
        executed.append(context.run_id)
        return {"processed": 1}

    with pytest.raises(RunBudgetExceeded, match="items"):
        FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=state_directory,
                route="probe",
                route_only=True,
                candidate_run_id=source_run,
                heartbeat_interval_seconds=0.01,
            ),
            route_registry={"probe": RouteAdapter("probe", route)},
            run_budget={"max_items": 0},
        ).run()
    assert executed == []
    status = list_run_status(state_directory / "framework.sqlite3", limit=1)[0]
    assert status.status == "cancelled"
    assert status.budget is not None and status.budget["cancel_requested"] is True


def test_inventory_backed_worker_consumes_global_budget(tmp_path: Path) -> None:
    from tests.test_run_control import _inventory_snapshot_source_run

    root = tmp_path / "fixture"
    root.mkdir()
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source_run, scan_id = _inventory_snapshot_source_run(
        state_directory / "framework.sqlite3",
        root,
        resumable_route="probe",
    )
    assert scan_id > 0
    executed: list[int] = []

    def route(context):
        executed.append(context.run_id)
        return {"processed": 1}

    with pytest.raises(RunBudgetExceeded, match="items"):
        FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=state_directory,
                route="probe",
                route_only=True,
                resume_run_id=source_run,
                heartbeat_interval_seconds=0.01,
            ),
            route_registry={
                "probe": RouteAdapter("probe", route, input_source="inventory_snapshot")
            },
            run_budget={"max_items": 0},
        ).run()
    assert executed == []
