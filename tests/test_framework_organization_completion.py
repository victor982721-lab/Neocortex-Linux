"""Framework completion retains durable organization work for recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.orchestration.run_manifest import RunManifest


def _run(state: FrameworkState, root: Path, kind: str) -> int:
    source = state.begin_initial_run(root, None)
    state.publish_initial_routing_snapshot(source, 1, 0, 1, "full", 0)
    if kind == "initial":
        run_id = source
    else:
        state.complete_initial_run(source, 1, None, 0, 1, "full")
        run_id = state.begin_operational_run(root, run_kind=kind, source_run_id=source)
    state.publish_run_manifest(
        run_id,
        RunManifest(
            run_id=run_id,
            run_kind=kind,
            root=str(root),
            root_identity=(1, 2, -1),
            selected_routes=(),
        ).event_payload(),
    )
    return run_id


def _complete(state: FrameworkState, run_id: int, kind: str) -> bool:
    if kind == "initial":
        return state.complete_initial_run(run_id, 1, None, 0, 1, "full")
    return state.complete_operational_run(run_id)


@pytest.mark.parametrize("kind", ("initial", "route_only", "resume"))
@pytest.mark.parametrize("stage", ("organization_plan", "organization_apply"))
@pytest.mark.parametrize("status", ("pending", "running", "interrupted", "failed", "partial"))
def test_unfinished_organization_blocks_completion_and_remains_recoverable(
    tmp_path: Path, kind: str, stage: str, status: str,
) -> None:
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        run_id = _run(state, tmp_path, kind)
        state.publish_run_stage(run_id, stage, status)

        with pytest.raises(RuntimeError, match=f"pending organization stages: {stage}"):
            _complete(state, run_id, kind)

        assert state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?", (run_id,),
        ).fetchone() == ("running", None)
        recovery = state.run_recovery_plan(run_id)
        assert recovery["recoverable"] is True
        assert recovery["pending"] == []
        assert recovery["pending_stages"] == [stage]
        assert not state._connection.in_transaction


@pytest.mark.parametrize("kind", ("initial", "route_only", "resume"))
@pytest.mark.parametrize("terminal", ("completed", "skipped"))
def test_latest_terminal_organization_allows_idempotent_completion(
    tmp_path: Path, kind: str, terminal: str,
) -> None:
    with FrameworkState(tmp_path / "framework.sqlite3") as state:
        run_id = _run(state, tmp_path, kind)
        for stage in ("organization_plan", "organization_apply"):
            state.publish_run_stage(run_id, stage, "pending")
            state.publish_run_stage(run_id, stage, terminal)
        # Framework historically completes before its integrated Semantic
        # publication; only organization obligations belong to this guard.
        state.publish_run_stage(run_id, "semantic", "pending")

        assert _complete(state, run_id, kind)
        assert not _complete(state, run_id, kind)
        recovery = state.run_recovery_plan(run_id)
        assert recovery["pending_stages"] == []
        assert recovery["recoverable"] is False
