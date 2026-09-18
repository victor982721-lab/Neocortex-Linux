"""Public exact adoption with producer evidence, protected copies and replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.historical_adoption import HistoricalSelection
from neocortex.runtime.historical_audit import HistoricalAuditError, HistoricalAuditManager


def _fixture(tmp_path: Path, *, shared: bool = False):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    root = tmp_path / "historical"
    root.mkdir(mode=0o700)
    if shared:
        root.chmod(0o1777)
    entry = root / "ordinary-name"
    entry.mkdir(mode=0o700)
    (entry / "payload").write_bytes(b"unique evidence becomes durable elsewhere")
    (entry / "payload").chmod(0o600)
    copy = tmp_path / "published"
    copy.mkdir(mode=0o700)
    (copy / "payload").write_bytes((entry / "payload").read_bytes())
    (copy / "payload").chmod(0o600)
    registry = ArtifactRegistry(state / "artifacts", owner="neocortex-framework", create_root=True)
    registry.register("producer:historical", producer="neocortex-audit", purpose="completed audit workspace",
                      path=entry, root=registry.root, state="completed", disposable=True)
    registry.register("deliverable:historical", producer="neocortex-audit", purpose="published audit",
                      path=copy, root=copy, kind="canonical", state="completed", disposable=False)
    manager = HistoricalAuditManager(root, state_directory=state, max_depth=8)
    selection = HistoricalSelection(entry, "producer:historical", "deliverable:historical")
    return manager, selection, registry, copy


@pytest.mark.parametrize("shared", [False, True])
def test_explicit_adoption_preserves_neighbor_and_durable_copy_and_replays(tmp_path: Path, shared: bool):
    manager, selection, registry, copy = _fixture(tmp_path, shared=shared)
    neighbor = manager.root / "same-prefix-does-not-authorize"
    neighbor.mkdir(mode=0o700)
    (neighbor / "keep").write_bytes(b"not selected")
    plan = manager.plan_selected([selection])
    assert plan.status == "planned", plan.to_dict()
    assert not (manager.state_directory / "historical-adoptions").exists()
    manager.prepare_adoption(plan)
    with pytest.raises(FileNotFoundError):
        manager.apply_selected(plan.digest)
    approval = manager.approve_adoption(plan.digest)
    assert approval["selected_ids"] == [plan.records[0]["selected_id"]]
    applied = manager.apply_selected(plan.digest)
    assert applied["operation_status"] == "complete", applied
    assert not selection.path.exists()
    assert (copy / "payload").read_bytes() == b"unique evidence becomes durable elsewhere"
    assert (neighbor / "keep").read_bytes() == b"not selected"
    assert registry.verify("producer:historical").state == "retired"
    replay = HistoricalAuditManager(manager.root, state_directory=manager.state_directory).apply_selected(plan.digest)
    assert replay["operation_status"] == "complete"
    assert replay["records"][0]["replayed"] is True


def test_digest_or_self_approved_json_is_not_authority(tmp_path: Path):
    manager, selection, _, _ = _fixture(tmp_path)
    plan = manager.prepare_adoption(manager.plan_selected([selection]))
    proposal_path = manager.state_directory / "historical-adoptions" / (plan.digest[7:] + ".proposal.json")
    proposal = json.loads(proposal_path.read_bytes())
    proposal["body"]["approved"] = True
    proposal_path.write_text(json.dumps(proposal))
    with pytest.raises(HistoricalAuditError, match="authentication"):
        manager.approve_adoption(plan.digest)
    assert selection.path.exists()


def test_unknown_provenance_and_unique_copy_remain_visible_in_partial_selection(tmp_path: Path):
    manager, selection, _, _ = _fixture(tmp_path)
    unique = manager.root / "unknown"
    unique.mkdir(mode=0o700)
    (unique / "payload").write_bytes(b"keep")
    unknown = HistoricalSelection(unique, "missing-producer")
    unique_selection = HistoricalSelection(selection.path, selection.provenance_artifact_id)
    unique_plan = manager.plan_selected([unique_selection])
    assert "unique_copy" in unique_plan.records[0]["reason"]
    total = manager.prepare_adoption(manager.plan_selected([selection, unknown]))
    with pytest.raises(HistoricalAuditError, match="complete_selection"):
        manager.approve_adoption(total.digest)
    partial = manager.prepare_adoption(manager.plan_selected([selection, unknown], partial=True))
    manager.approve_adoption(partial.digest)
    applied = manager.apply_selected(partial.digest)
    assert applied["operation_status"] == "complete"
    assert unique.exists()


def test_replaced_selection_after_approval_blocks_before_effect(tmp_path: Path):
    manager, selection, _, _ = _fixture(tmp_path)
    plan = manager.prepare_adoption(manager.plan_selected([selection]))
    manager.approve_adoption(plan.digest)
    selection.path.rename(selection.path.with_name("old"))
    selection.path.mkdir(mode=0o700)
    (selection.path / "innocent").write_bytes(b"keep")
    with pytest.raises(HistoricalAuditError, match="changed_before_effect"):
        manager.apply_selected(plan.digest)
    assert (selection.path / "innocent").read_bytes() == b"keep"


def test_cancelled_selection_has_no_clean_status_or_approval(tmp_path: Path):
    manager, selection, _, _ = _fixture(tmp_path)
    plan = manager.plan_selected([selection], cancelled=lambda: True)
    assert not plan.coverage_complete
    assert plan.records[0]["reason"] == "cancelled"
    assert selection.path.exists()


def test_symlink_ancestor_cannot_turn_selection_into_authority(tmp_path: Path):
    manager, selection, _, _ = _fixture(tmp_path)
    link = manager.root / "alias"
    link.symlink_to(selection.path, target_is_directory=True)
    plan = manager.plan_selected([HistoricalSelection(link, selection.provenance_artifact_id,
                                                     selection.preserved_artifact_id)])
    assert plan.records[0]["status"] == "unknown"
    assert selection.path.exists()


def test_final_receipt_failure_reconciles_in_new_session_without_second_unlink(tmp_path: Path, monkeypatch):
    from neocortex.runtime.historical_adoption import HistoricalAdoption
    manager, selection, registry, copy = _fixture(tmp_path)
    plan = manager.prepare_adoption(manager.plan_selected([selection]))
    manager.approve_adoption(plan.digest)
    original_write = HistoricalAdoption._write
    failed = False
    def fail_confirmation(fd, name, data, *, exclusive=False):
        nonlocal failed
        if ".effect.json" in name and json.loads(data)["body"]["state"] == "retired" and not failed:
            failed = True
            raise OSError("injected receipt fsync failure")
        return original_write(fd, name, data, exclusive=exclusive)
    monkeypatch.setattr(HistoricalAdoption, "_write", staticmethod(fail_confirmation))
    first = manager.apply_selected(plan.digest)
    assert first["operation_status"] == "partial"
    assert first["records"][0]["state"] == "recovery_required"
    assert not selection.path.exists()
    fresh = HistoricalAuditManager(manager.root, state_directory=manager.state_directory)
    second = fresh.apply_selected(plan.digest)
    assert second["operation_status"] == "complete"
    assert second["records"][0]["replayed"] is True
    assert registry.verify(selection.provenance_artifact_id).state == "retired"
    assert (copy / "payload").exists()


def test_private_adoption_receipts_cannot_cross_a_mount_boundary(tmp_path: Path, monkeypatch):
    from neocortex.runtime import scratch_tree
    manager, selection, _, _ = _fixture(tmp_path)
    plan = manager.plan_selected([selection])
    directory = manager.state_directory / "historical-adoptions"
    directory.mkdir(mode=0o700)
    identity = directory.stat().st_ino
    original = scratch_tree._mount_id
    monkeypatch.setattr(scratch_tree, "_mount_id", lambda fd: original(fd) + 1
                        if __import__("os").fstat(fd).st_ino == identity else original(fd))
    with pytest.raises(HistoricalAuditError, match="mount_boundary"):
        manager.prepare_adoption(plan)
    assert not list(directory.glob("*.proposal.json"))
    assert selection.path.exists()


def test_current_registered_manifest_needs_no_duplicate_payload_copy(tmp_path: Path):
    from neocortex.api.agent_activity import AgentActivity
    activity = AgentActivity.prepare(tmp_path / "state", "manifest-source")
    (activity.path / "derived").write_bytes(b"rebuildable")
    activity.close()
    manager = HistoricalAuditManager(activity.path.parent, state_directory=tmp_path / "state")
    plan = manager.plan_selected([HistoricalSelection(activity.path, activity.artifact_id)])
    assert plan.records[0]["status"] == "eligible", plan.to_dict()
    manager.prepare_adoption(plan)
    manager.approve_adoption(plan.digest)
    assert manager.apply_selected(plan.digest)["operation_status"] == "complete"


def test_different_local_principal_cannot_approve_private_proposal(tmp_path: Path, monkeypatch):
    from neocortex.runtime import historical_adoption
    manager, selection, _, _ = _fixture(tmp_path)
    plan = manager.prepare_adoption(manager.plan_selected([selection]))
    original_uid = historical_adoption.os.geteuid()
    monkeypatch.setattr(historical_adoption.os, "geteuid", lambda: original_uid + 123)
    with pytest.raises(HistoricalAuditError, match="private"):
        manager.approve_adoption(plan.digest)
    assert selection.path.exists()
