from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator


class _FakeScratchPlan:
    def __init__(self, *, planned: int = 0, kept: int = 0, applied: int = 0):
        self.planned = planned
        self.kept = kept
        self.applied = applied
        self.blocked = 0
        self.failed = 0
        self.recovery_required = 0
        self.planned_bytes = 0
        self.kept_bytes = 0
        self.applied_bytes = 0
        self.blocked_bytes = 0
        self.failed_bytes = 0
        self.recovery_required_bytes = 0


class _FakeState:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.events: list[tuple[object, ...]] = []

    def publish_run_stage(self, *args, **kwargs):
        self.calls.append(("stage", (args, kwargs)))

    def record_event(self, *args):
        self.events.append(args)


def _install_fake_scratch(monkeypatch, manager_type):
    module = ModuleType("neocortex.runtime.scratch")
    module.ScratchManager = manager_type
    monkeypatch.setitem(sys.modules, "neocortex.runtime.scratch", module)


def _orchestrator(tmp_path, *, apply_actions: bool):
    orchestrator = object.__new__(FrameworkOrchestrator)
    orchestrator.config = SimpleNamespace(
        state_directory=tmp_path / "state",
        apply_actions=apply_actions,
    )
    return orchestrator


def test_initial_scratch_plan_is_read_only_and_does_not_create_root(tmp_path, monkeypatch):
    calls: list[tuple[str, object]] = []

    class FakeManager:
        def __init__(self, root, *, owner, create_root):
            calls.append(("init", (root, owner, create_root)))

        def plan(self):
            calls.append(("plan", None))
            return _FakeScratchPlan(planned=2, kept=2)

        def apply(self, plan):
            calls.append(("apply", plan))
            raise AssertionError("read-only Framework finalization must not apply scratch")

    _install_fake_scratch(monkeypatch, FakeManager)
    state = _FakeState()
    orchestrator = _orchestrator(tmp_path, apply_actions=False)

    result = orchestrator._run_initial_scratch_maintenance(state, 42)

    assert calls == [
        (
            "init",
            (tmp_path / "state" / "scratch" / "owned-temp", "neocortex-framework", False),
        ),
        ("plan", None),
    ]
    assert result["mode"] == "plan"
    assert result["status"] == "planned"
    assert result["planned"] == 2
    assert not (tmp_path / "state" / "scratch" / "owned-temp").exists()
    assert state.events[-1][2] == "maintenance"


def test_initial_scratch_apply_uses_manager_eligibility_and_keeps_noop_safe(tmp_path, monkeypatch):
    calls: list[tuple[str, object]] = []

    class FakeManager:
        def __init__(self, root, *, owner, create_root):
            calls.append(("init", (root, owner, create_root)))
            self.plan_result = None

        def plan(self):
            calls.append(("plan", None))
            self.plan_result = _FakeScratchPlan()
            calls[-1] = ("plan", self.plan_result)
            return self.plan_result

        def apply(self):
            calls.append(("apply", None))
            return _FakeScratchPlan()

    _install_fake_scratch(monkeypatch, FakeManager)
    state = _FakeState()
    orchestrator = _orchestrator(tmp_path, apply_actions=True)

    result = orchestrator._run_initial_scratch_maintenance(state, 43)

    assert [name for name, _ in calls] == ["init", "plan", "apply"]
    assert calls[2][1] is None
    assert result["mode"] == "apply"
    assert result["status"] == "applied"
    assert result["applied"] == 0
    assert not (tmp_path / "state" / "scratch" / "owned-temp").exists()


def test_scratch_maintenance_failure_is_recorded_without_blocking_finalization(tmp_path, monkeypatch):
    class FailingManager:
        def __init__(self, root, *, owner, create_root):
            pass

        def plan(self):
            raise OSError("scratch root is not a registered private root")

    _install_fake_scratch(monkeypatch, FailingManager)
    state = _FakeState()
    orchestrator = _orchestrator(tmp_path, apply_actions=False)

    result = orchestrator._run_initial_scratch_maintenance(state, 44)

    assert result["status"] == "failed"
    assert result["error_type"] == "OSError"
    assert state.events[-1][1] == "warning"
