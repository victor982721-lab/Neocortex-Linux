"""Persistence contracts for the durable Framework lifecycle tranche."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neocortex.persistence import framework_state_writer
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.orchestration.run_manifest import (
    RunBudget,
    RunManifest,
    verify_event_payload,
)
from neocortex.runtime.orchestration.run_status import list_run_status


def _manifest(run_id: int, root: Path, *, capability: str = "safe_replay") -> dict[str, object]:
    return RunManifest(
        run_id=run_id,
        run_kind="initial",
        root=str(root),
        root_identity=(1, 2, -1),
        selected_routes=("probe",),
        route_capabilities={"probe": capability},
    ).event_payload()


def test_terminal_completion_rechecks_expired_budget_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100_000_000_000]
    monkeypatch.setattr(framework_state_writer.time, "time_ns", lambda: clock[0])
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.publish_run_budget(run_id, {"max_duration_seconds": 1.0})
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        clock[0] += 1_000_000_001
        with pytest.raises(RunBudgetExceeded, match="time"):
            state.complete_initial_run(run_id, 1, None, 0, 1, "full")
        assert state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone() == ("running", None)


def test_cursorless_completion_accepts_bounded_successor_full_inventory(tmp_path: Path) -> None:
    """ZIP Intake may publish a second full scan before terminal completion."""

    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(tmp_path, None)
        state.publish_initial_routing_snapshot(run_id, 2, 3, 2, "full", 0)
        assert state.complete_initial_run(run_id, 2, None, 3, 2, "full") is True
        assert state._connection.execute(
            "SELECT status FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone() == ("completed",)


def test_stage_checkpoint_and_stage_budget_are_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root))
        first = state.reserve_run_stage(
            run_id,
            "routes",
            "routes:probe",
            items=2,
            bytes=10,
            worker="probe",
        )
        replay = state.reserve_run_stage(
            run_id,
            "routes",
            "routes:probe",
            items=2,
            bytes=10,
            worker="probe",
        )
        assert first["consumed_items"] == replay["consumed_items"] == 2
        assert replay["replayed"] is True
        assert state.read_run_stage_budget(run_id) == {
            "routes": {"items": 2, "bytes": 10, "reservations": 1}
        }

        assert state.publish_run_stage(
            run_id,
            "routes",
            "running",
            details={"worker": "probe"},
            checkpoint={"last_path": "one.pdf", "processed": 2},
            idempotency_key="routes:started",
        )
        assert not state.publish_run_stage(
            run_id,
            "routes",
            "running",
            details={"worker": "probe"},
            checkpoint={"last_path": "one.pdf", "processed": 2},
            idempotency_key="routes:started",
        )
        checkpoints = state.read_run_checkpoints(run_id)
        assert len(checkpoints) == 1
        assert checkpoints[0]["checkpoint"] == {"last_path": "one.pdf", "processed": 2}
        assert state.read_run_stage_state(run_id)["routes"]["status"] == "running"


def test_recovery_carries_not_resumable_capability_into_status(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(run_id, _manifest(run_id, root, capability="not_resumable"))
        state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", 0)
        state.begin_route_runs(run_id, ("probe",), route_input_sources={"probe": "route_candidates"})
        assert state.mark_abandoned_runs() == 1
        recovery = state.run_recovery_plan(run_id)
        assert recovery["route_capabilities"] == {"probe": "not_resumable"}

    status = list_run_status(database, run_id=run_id)[0]
    assert status.non_replayable_routes == ("probe",)
    assert status.recovery is not None
    assert status.recovery["route_capabilities"] == {"probe": "not_resumable"}


def test_start_abort_closes_operational_row_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        source = state.begin_initial_run(root, None)
        state.publish_initial_routing_snapshot(source, 1, 0, 1, "full", 0)
        state.complete_initial_run(source, 1, None, 0, 1, "full")
        run_id = state.begin_operational_run(root, run_kind="resume", source_run_id=source)
        assert state.abort_run_start(run_id, RuntimeError("manifest setup failed"))
        assert state._connection.execute(
            "SELECT status,current_phase FROM initial_runs WHERE run_id=?", (run_id,)
        ).fetchone() == ("failed", "failed")
        assert not state.abort_run_start(run_id, RuntimeError("duplicate"))


def test_v1_manifest_without_later_route_capabilities_remains_readable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    payload = RunManifest(
        run_id=7,
        run_kind="initial",
        root=str(root),
        root_identity=(1, 2, -1),
        selected_routes=("probe",),
    ).event_payload()
    payload.pop("route_capabilities")
    unsigned = dict(payload)
    unsigned.pop("digest")
    payload["digest"] = "sha256:" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    assert verify_event_payload(payload) == payload


def test_stage_budget_alias_preserves_unlimited_legacy_reservations(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = state.begin_initial_run(root, None)
        state.publish_run_manifest(
            run_id,
            RunManifest(
                run_id=run_id,
                run_kind="initial",
                root=str(root),
                root_identity=(1, 2, -1),
                selected_routes=("probe",),
                budget={"durable": RunBudget().payload()},
            ).event_payload(),
        )
        state.reserve_run_budget(run_id, "legacy", items=1, worker="probe")
        assert state.run_stage_budget(run_id) == {
            "unattributed": {"items": 1, "bytes": 0, "reservations": 1}
        }
