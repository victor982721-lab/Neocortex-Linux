"""Framework uses the real maintenance coordinator and its exact owner plans."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.scratch import ScratchManager


class _FakeState:
    def __init__(self):
        self.calls = []
        self.events = []
    def publish_run_stage(self, *args, **kwargs):
        self.calls.append((args, kwargs))
    def record_event(self, *args):
        self.events.append(args)


def _orchestrator(tmp_path, *, apply_actions):
    orchestrator = object.__new__(FrameworkOrchestrator)
    orchestrator.config = SimpleNamespace(state_directory=tmp_path / "state", apply_actions=apply_actions)
    orchestrator._cancellation = CancellationToken()
    orchestrator._route_failures = {}
    return orchestrator


def test_initial_scratch_plan_is_read_only_and_does_not_create_root(tmp_path):
    state = _FakeState()
    result = _orchestrator(tmp_path, apply_actions=False)._run_initial_scratch_maintenance(state, 42)
    assert result["mode"] == "plan" and result["status"] == "planned"
    assert result["planned"] == 0
    assert result["maintenance"]["requested_scopes"] == ["owned-temp"]
    assert not (tmp_path / "state").exists()
    assert state.events[-1][2] == "maintenance"


def test_initial_scratch_apply_uses_manager_eligibility_and_keeps_noop_safe(tmp_path):
    state_root = tmp_path / "state"
    manager = ScratchManager(state_root / "scratch" / "owned-temp", owner="neocortex-framework",
                             create_root=True, artifact_registry_root=state_root / "artifacts")
    workspace = manager.create(retain_on_success=True)
    (workspace.path / "payload").write_bytes(b"completed fixture")
    workspace.complete(retain=True)
    state = _FakeState()
    orchestrator = _orchestrator(tmp_path, apply_actions=True)
    result = orchestrator._run_initial_scratch_maintenance(state, 43)
    assert result["status"] == "applied" and result["applied"] == 1, result
    assert not workspace.path.exists()
    phases = [event[4]["phase"] for event in state.events if event[2] == "maintenance-receipt"]
    assert phases == ["prepared", "confirmed"]
    replay = orchestrator._run_initial_scratch_maintenance(state, 44)
    assert replay["status"] == "applied" and replay["applied"] == 0


def test_scratch_maintenance_failure_is_recorded_without_blocking_finalization(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("scratch owner unavailable")
    monkeypatch.setattr("neocortex.runtime.orchestration.maintenance.configured_scratch_maintenance", unavailable)
    state = _FakeState()
    result = _orchestrator(tmp_path, apply_actions=False)._run_initial_scratch_maintenance(state, 44)
    assert result["status"] == "failed" and result["error_type"] == "OSError"
    assert state.events[-1][1] == "warning"


def test_required_receipt_failure_retains_workspace(tmp_path):
    state_root = tmp_path / "state"
    manager = ScratchManager(state_root / "scratch" / "owned-temp", owner="neocortex-framework",
                             create_root=True, artifact_registry_root=state_root / "artifacts")
    workspace = manager.create(retain_on_success=True)
    (workspace.path / "payload").write_bytes(b"retained fixture")
    workspace.complete(retain=True)
    class FailingState(_FakeState):
        def record_event(self, *args):
            if args[2] == "maintenance-receipt":
                raise OSError("receipt publication failed")
            super().record_event(*args)
    result = _orchestrator(tmp_path, apply_actions=True)._run_initial_scratch_maintenance(FailingState(), 45)
    assert result["maintenance"]["operation_status"] == "partial"
    assert workspace.path.is_dir()


def test_live_exhausted_budget_preserves_workspace(tmp_path):
    state_root = tmp_path / "state"
    manager = ScratchManager(state_root / "scratch" / "owned-temp", owner="neocortex-framework",
                             create_root=True, artifact_registry_root=state_root / "artifacts")
    workspace = manager.create(retain_on_success=True)
    (workspace.path / "payload").write_bytes(b"budget fixture")
    workspace.complete(retain=True)
    class ExhaustedState(_FakeState):
        def read_run_budget(self, run_id):
            return {"remaining_items": 0, "remaining_bytes": 0, "deadline_ns": 1}
        def reserve_run_budget(self, *args, **kwargs):
            raise AssertionError("exhausted work must not be admitted")
    result = _orchestrator(tmp_path, apply_actions=True)._run_initial_scratch_maintenance(ExhaustedState(), 46)
    assert result["maintenance"]["operation_status"] == "partial"
    assert result["maintenance"]["primary_work_status"] == "complete"
    assert workspace.path.is_dir()


@pytest.mark.parametrize("reject_verification", [False, True])
def test_live_ledger_reserves_verification_before_any_retirement(tmp_path, reject_verification):
    state_root = tmp_path / "state"
    manager = ScratchManager(state_root / "scratch" / "owned-temp", owner="neocortex-framework",
                             create_root=True, artifact_registry_root=state_root / "artifacts")
    workspace = manager.create(retain_on_success=True)
    payload = workspace.path / "payload"
    payload.write_bytes(b"ledger fixture")
    workspace.complete(retain=True)
    class LedgerState(_FakeState):
        def __init__(self):
            super().__init__()
            self.reservations = []
        def read_run_budget(self, run_id):
            return {"remaining_items": 1000, "remaining_bytes": 1000000, "deadline_ns": None}
        def reserve_run_budget(self, run_id, reservation_id, **kwargs):
            assert payload.read_bytes() == b"ledger fixture", "reservation happened after effect"
            self.reservations.append((reservation_id, kwargs))
            if reject_verification and reservation_id.startswith("maintenance-verification:"):
                raise RuntimeError("concurrent owner exhausted the run budget")
    state = LedgerState()
    result = _orchestrator(tmp_path, apply_actions=True)._run_initial_scratch_maintenance(state, 47)
    assert len(state.reservations) == 2
    assert state.reservations[1][1]["items"] > 0
    assert state.reservations[1][1]["bytes"] > 0
    if reject_verification:
        assert result["maintenance"]["operation_status"] == "partial"
        assert payload.read_bytes() == b"ledger fixture"
        assert not any(event[2] == "maintenance-receipt" for event in state.events)
    else:
        assert result["maintenance"]["operation_status"] == "complete"
        assert not workspace.path.exists()
        prepared = next(event[4] for event in state.events
                        if event[2] == "maintenance-receipt" and event[4]["phase"] == "prepared")
        assert sum(values["items"] for _, values in state.reservations) == prepared["budget_entries"]
        assert sum(values["bytes"] for _, values in state.reservations) == prepared["budget_bytes"]
