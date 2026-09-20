"""Federated owner execution uses exact plans, public lifecycle and receipts."""

from __future__ import annotations

from pathlib import Path

from neocortex.api.agent_activity import AgentActivity
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.orchestration.maintenance import (
    MaintenanceCoordinator, MaintenanceRequest, ScopeAuthority, ScratchMaintenanceOwner,
    TerminalRetentionOwner, configured_scratch_maintenance,
)
from neocortex.runtime.scratch import ScratchManager
from neocortex.workflow.retention.planner import TerminalRetentionPolicy


def _activity(state: Path, root: Path, name: str):
    activity = AgentActivity.prepare(state, name, workspace_root=root)
    (activity.path / "result.txt").write_text(name)
    published = state.parent / (name + "-published")
    published.mkdir(mode=0o700)
    activity.publish(activity.path / "result.txt", published / "result.txt")
    activity.close()
    return activity, published / "result.txt"


def test_three_existing_owners_complete_and_second_plan_has_no_physical_effect(tmp_path: Path):
    state = tmp_path / "state"
    first, first_result = _activity(state, state / "scratch" / "owned-temp", "first")
    second, second_result = _activity(state, state / "scratch" / "audit-work", "second")
    coordinator = configured_scratch_maintenance(state, scopes=("owned-temp", "audit-work"))
    registry = ArtifactRegistry(state / "artifacts", owner="neocortex-framework")
    coordinator.owners["terminal-retention"] = TerminalRetentionOwner(registry,
                                policy=TerminalRetentionPolicy(minimum_age_ns=0))
    scopes = ("owned-temp", "audit-work", "terminal-retention")
    request = MaintenanceRequest(scopes=scopes, apply_requested=True,
                                authorities=tuple(ScopeAuthority(s, "explicit-fixture-operation") for s in scopes))
    plan = coordinator.plan(request)
    assert not plan.blocked, plan.to_dict()
    outcome = coordinator.execute(plan)
    assert outcome["maintenance_status"] == "complete", outcome
    assert not first.path.exists() and not second.path.exists()
    assert first_result.read_text() == "first" and second_result.read_text() == "second"
    assert all(Path(ref).exists() for ref in outcome["receipt_refs"])
    repeated = coordinator.execute(coordinator.plan(request))
    assert repeated["maintenance_status"] == "complete", repeated
    assert sum(r["retired"] for r in repeated["scopes"].values()) == 0


def test_plan_digest_does_not_authorize_maintenance(tmp_path: Path):
    state = tmp_path / "state"
    activity, _ = _activity(state, state / "scratch" / "owned-temp", "work")
    coordinator = configured_scratch_maintenance(state)
    outcome = coordinator.execute(coordinator.plan(MaintenanceRequest(apply_requested=True)))
    assert outcome["blocked_scopes"] == {"owned-temp": "explicit_scope_authority_required"}
    assert activity.path.exists()


def test_exact_selection_excludes_newly_completed_workspace(tmp_path: Path):
    state = tmp_path / "state"
    selected, _ = _activity(state, state / "scratch" / "owned-temp", "selected")
    coordinator = configured_scratch_maintenance(state)
    request = MaintenanceRequest(apply_requested=True, selected_ids={"owned-temp": (selected.workspace_id,)},
                                 authorities=(ScopeAuthority("owned-temp", "operator"),))
    plan = coordinator.plan(request)
    later, _ = _activity(state, state / "scratch" / "owned-temp", "later")
    result = coordinator.execute(plan)
    assert result["maintenance_status"] == "complete", result
    assert not selected.path.exists()
    assert later.path.exists()


def test_absent_owner_and_cancelled_budget_are_visible_without_creating_state(tmp_path: Path):
    state = tmp_path / "missing"
    coordinator = configured_scratch_maintenance(state)
    request = MaintenanceRequest(scopes=("owned-temp", "historical-temp"), cancelled=lambda: True)
    plan = coordinator.plan(request)
    assert plan.blocked == {"owned-temp": "cancelled", "historical-temp": "owner_unavailable"}
    outcome = coordinator.execute(plan)
    assert outcome["operation_status"] == "partial"
    assert outcome["maintenance_status"] == "partial"
    assert outcome["primary_work_status"] == "complete"
    assert outcome["blocked_scopes"] == plan.blocked
    assert not state.exists()


def test_mandatory_evidence_failure_prevents_global_success_and_preserves_receipt(tmp_path: Path):
    state = tmp_path / "state"
    activity, published = _activity(state, state / "scratch" / "owned-temp", "record")
    phases = []

    def record_outcome(payload):
        phases.append(payload["phase"])
        if payload["phase"] == "confirmed":
            raise OSError("receipt publication interrupted")

    coordinator = configured_scratch_maintenance(state, record_outcome=record_outcome)
    request = MaintenanceRequest(apply_requested=True, authorities=(ScopeAuthority("owned-temp", "operator"),))
    outcome = coordinator.execute(coordinator.plan(request))
    assert phases == ["prepared", "confirmed"]
    assert outcome["maintenance_status"] == "partial"
    assert outcome["primary_work_status"] == "complete"
    assert outcome["receipt_refs"]
    assert all(Path(ref).exists() for ref in outcome["receipt_refs"])
    assert not activity.path.exists()
    assert published.read_text() == "record"


def test_scope_dependency_cycle_blocks_before_unlink(tmp_path: Path):
    state = tmp_path / "state"
    registry = ArtifactRegistry(state / "artifacts", owner="neocortex-framework", create_root=True)
    manager = ScratchManager(state / "scratch" / "owned-temp", owner="neocortex-framework",
                             create_root=True, artifact_registry=registry)
    a = manager.create(retain_on_success=True)
    b = manager.create(retain_on_success=True)
    a.complete(retain=True)
    b.complete(retain=True)
    # Public registry dependency updates preserve producer ownership. The
    # coordinator's graph is inspected independently from the effect owner.
    class CycleOwner(ScratchMaintenanceOwner):
        def claims(self, plan):
            values = list(super().claims(plan))
            values[0] = {**values[0], "dependencies": (values[1]["artifact_id"],)}
            values[1] = {**values[1], "dependencies": (values[0]["artifact_id"],)}
            return values
    coordinator = MaintenanceCoordinator({"owned-temp": CycleOwner(manager)})
    request = MaintenanceRequest(apply_requested=True, authorities=(ScopeAuthority("owned-temp", "operator"),))
    outcome = coordinator.execute(coordinator.plan(request))
    assert outcome["blocked_scopes"] == {"owned-temp": "dependency_cycle"}
    assert a.path.exists() and b.path.exists()


def test_external_registered_workspace_resumes_by_producer_identity(tmp_path: Path):
    state = tmp_path / "state"
    work = tmp_path / "Auditorias" / "audit-1" / "work"
    activity, published = _activity(state, work, "outside")
    sibling = tmp_path / "Auditorias" / "audit-2" / "work"
    sibling.mkdir(mode=0o700, parents=True)
    (sibling / "result.txt").write_text("unmanaged")
    resumed = AgentActivity.resume(state, "outside")
    assert resumed.path == activity.path
    resumed.retire()
    assert not activity.path.exists()
    assert (sibling / "result.txt").read_text() == "unmanaged"
    assert published.read_text() == "outside"


def test_terminal_owner_applies_purpose_policy_and_preserves_published_deliverable(tmp_path: Path):
    state = tmp_path / "state"
    activity, published = _activity(state, state / "scratch" / "owned-temp", "terminal")
    activity.retire()
    registry = ArtifactRegistry(state / "artifacts", owner="neocortex-framework")
    purpose = registry.verify(activity.artifact_id).purpose
    owner = TerminalRetentionOwner(registry, policy=TerminalRetentionPolicy(minimum_age_ns=0),
              purpose_policies={purpose: TerminalRetentionPolicy(minimum_age_ns=0, tombstone_count=0)})
    coordinator = MaintenanceCoordinator({"terminal-retention": owner})
    request = MaintenanceRequest(scopes=("terminal-retention",), apply_requested=True,
                                authorities=(ScopeAuthority("terminal-retention", "operator"),))
    plan = coordinator.plan(request)
    assert not plan.blocked, plan.to_dict()
    outcome = coordinator.execute(plan)
    assert outcome["maintenance_status"] == "complete", outcome
    assert outcome["scopes"]["terminal-retention"]["retired"] == 1
    assert not registry.manifest_path(activity.artifact_id).exists()
    assert published.read_text() == "terminal"
    assert all(Path(ref).exists() for ref in outcome["receipt_refs"])
