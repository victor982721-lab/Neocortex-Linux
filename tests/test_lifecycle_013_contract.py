"""Fase 4 contracts for durable --all replay budgets."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.runtime.orchestration.run_manifest import RunBudget, RunManifest
from neocortex.runtime.orchestration.run_status import list_run_status
from tests.test_run_control import _source_run


def _interrupted_source(
    root: Path,
    state_directory: Path,
    budget: RunBudget,
    *,
    reserved: tuple[int, int] | None = None,
) -> int:
    database = state_directory / "framework.sqlite3"
    source_run = _source_run(database, root, route_running=True)
    with FrameworkState(database) as state:
        state.publish_run_manifest(
            source_run,
            RunManifest(
                run_id=source_run,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("probe",),
                route_capabilities={"probe": "safe_replay"},
                budget={"durable": budget.payload()},
            ).event_payload(),
        )
        if reserved is not None:
            state.reserve_run_budget(
                source_run,
                "source-route",
                items=reserved[0],
                bytes=reserved[1],
            )
        state.mark_abandoned_runs()
    return source_run


def _resume_config(root: Path, state_directory: Path, source_run: int) -> FrameworkConfig:
    return FrameworkConfig(
        root=root,
        state_directory=state_directory,
        route="none",
        route_only=True,
        resume_run_id=source_run,
        heartbeat_interval_seconds=0.01,
    )


def test_resume_spends_only_the_source_run_budget_remainder(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    source_run = _interrupted_source(
        root,
        state_directory,
        RunBudget(max_items=3, max_bytes=100),
        reserved=(1, 9),
    )
    with FrameworkState(database) as state:
        source_budget = state.read_run_budget(source_run)
    assert source_budget is not None
    assert source_budget["remaining_items"] == 2
    assert source_budget["remaining_bytes"] == 91

    executed: list[int] = []

    def execute(context):
        executed.append(context.run_id)
        return {"processed": 1}

    result = FrameworkOrchestrator(
        _resume_config(root, state_directory, source_run),
        route_registry={"probe": RouteAdapter("probe", execute)},
    ).run()

    assert executed == [result.run_id]
    status = list_run_status(database, run_id=result.run_id)[0]
    assert status.replayed is True
    assert status.budget is not None
    assert status.budget["max_items"] == 2
    assert status.budget["max_bytes"] == 91
    assert status.budget["consumed_items"] == 1
    assert status.budget["consumed_bytes"] == 9
    assert status.manifest is not None
    assert status.manifest["source_run_id"] == source_run
    assert status.manifest["input_snapshot"]["source_budget"]["remaining_items"] == 2


def test_resume_does_not_open_a_fresh_deadline_after_source_expiry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    source_run = _interrupted_source(
        root,
        state_directory,
        RunBudget(max_duration_seconds=0.001),
    )
    time.sleep(0.01)
    executed: list[int] = []

    def execute(_context):
        executed.append(1)
        return {"processed": 1}

    with pytest.raises(RunBudgetExceeded, match="time"):
        FrameworkOrchestrator(
            _resume_config(root, state_directory, source_run),
            route_registry={"probe": RouteAdapter("probe", execute)},
        ).run()

    assert executed == []
    with FrameworkState(database) as state:
        assert state._connection.execute(
            "SELECT COUNT(*) FROM initial_runs"
        ).fetchone()[0] == 1
