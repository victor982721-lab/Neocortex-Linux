"""Composed reset plans must settle all producer authority before any effect."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.persistence import state_reset as reset
from neocortex.persistence import state_reset_inventory as inventory
from neocortex.runtime.artifact_registry import ArtifactRegistry


def _registered(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    registry = ArtifactRegistry(state / "artifacts", owner="fixture", create_root=True)
    return state, registry


def _claim(registry: ArtifactRegistry, name: str, path: Path, **kwargs):
    return registry.register(name, producer="fixture", path=path, root=registry.root,
                             purpose="fixture", state="completed", **kwargs)


def test_disposable_directory_cannot_remove_nested_canonical_claim(tmp_path: Path):
    state, registry = _registered(tmp_path)
    workspace = state / "workspaces"
    workspace.mkdir(mode=0o700)
    evidence = workspace / "only-copy"
    evidence.write_bytes(b"authoritative original")
    evidence.chmod(0o600)
    _claim(registry, "parent", workspace, disposable=True)
    _claim(registry, "canonical", evidence, kind="canonical", disposable=False)
    before = evidence.stat().st_ino
    plan = reset.plan_state_reset(state, scope="all")
    assert any("overlapping" in blocker for blocker in plan.inventory.blockers)
    with pytest.raises(reset.StateResetError):
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert evidence.read_bytes() == b"authoritative original"
    assert evidence.stat().st_ino == before
    assert registry.verify("parent").state == "completed"


@pytest.mark.parametrize("case", ["retained-dependent", "cycle", "budget", "unregistered-receipt"])
def test_incomplete_or_contradictory_inventory_blocks_before_effect(tmp_path: Path, monkeypatch, case: str):
    state, registry = _registered(tmp_path)
    a, b = state / "a", state / "b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    a.chmod(0o600)
    b.chmod(0o600)
    _claim(registry, "a", a)
    _claim(registry, "b", b, dependencies=("a",), kind="canonical" if case == "retained-dependent" else "temporary",
           disposable=case != "retained-dependent")
    if case == "cycle":
        registry.update("a", dependencies=("b",))
    elif case == "budget":
        monkeypatch.setattr(inventory, "MAX_INVENTORY_ENTRIES", 0)
    elif case == "unregistered-receipt":
        root = state / "state-reset-operations"
        root.mkdir(mode=0o700)
        (root / "unknown.json").write_text("{}")
    plan = reset.plan_state_reset(state, scope="all")
    assert plan.inventory.blockers
    with pytest.raises(reset.StateResetError):
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert a.read_bytes() == b"a" and b.read_bytes() == b"b"


def test_immediate_rollback_preserves_concurrent_edit_and_keeps_verified_raw(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    first, second = cache / "a", cache / "b"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    original = Path.unlink
    edited = False
    def unlink(path, *args, **kwargs):
        nonlocal edited
        result = original(path, *args, **kwargs)
        if path == first and not edited:
            edited = True
            second.write_bytes(b"new unique personal content")
        return result
    plan = reset.plan_state_reset(state, scope="all")
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert second.read_bytes() == b"new unique personal content"
    receipt = failure.value.operation_manifest
    assert json.loads(receipt.read_text())["status"] == "rollback-failed"
    assert (receipt.parent / "reset-files" / "runtime-cache" / "a").read_bytes() == b"first"


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("enrolled", [True, False])
def test_registry_rollback_compensates_only_previously_enrolled_claims(tmp_path: Path, monkeypatch, kind: str, enrolled: bool):
    state, registry = _registered(tmp_path)
    artifact = state / "cache-item"
    if kind == "directory":
        artifact.mkdir(mode=0o700)
        payload = artifact / "payload"
    else:
        payload = artifact
    payload.write_bytes(b"rebuildable")
    payload.chmod(0o600)
    _claim(registry, "cache", artifact)
    if not enrolled:
        monkeypatch.setattr(ArtifactRegistry, "prepare_retirement_compensation",
                            lambda self, artifact_id, **_kwargs: self.verify(artifact_id))
    retire = inventory.retire_inventory_targets
    def fail_after_retirement(*args, **kwargs):
        retire(*args, **kwargs)
        raise OSError("post-retirement fixture")
    monkeypatch.setattr(inventory, "retire_inventory_targets", fail_after_retirement)
    plan = reset.plan_state_reset(state, scope="all")
    if enrolled:
        with pytest.raises(reset.StateResetError) as failure:
            reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
        assert not isinstance(failure.value, reset.StateResetRecoveryRequiredError)
        restored = registry.verify("cache")
        assert restored.verified and restored.state == "completed"
        assert restored.metadata["retirement_compensation_receipt"]["original_manifest_digest"]
        assert payload.read_bytes() == b"rebuildable"
        assert not list(tmp_path.glob(".neocortex-state-reset-raw-*"))
        return
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    assert payload.read_bytes() == b"rebuildable"
    assert failure.value.operation_manifest.exists()
    record = registry.for_owner("state-reset").verify(failure.value.operation_id)
    assert record.state == "recovery_required"
    preview = reset.reconcile_state_reset(state, failure.value.operation_id)
    with pytest.raises(reset.StateResetRecoveryRequiredError, match="producer binding"):
        reset.reconcile_state_reset(state, failure.value.operation_id, apply=True,
            expected_receipt_digest=preview["receipt_digest"], confirmation=reset.STATE_RESET_CONFIRMATION)
    assert Path(preview["storage"]).exists()


@pytest.mark.parametrize("count", [20, 400])
def test_reset_reserves_recovery_metadata_capacity_before_any_effect(tmp_path: Path, monkeypatch, count: int):
    state = tmp_path / "state"
    cache = state / "runtime-cache"
    cache.mkdir(parents=True)
    for index in range(count):
        (cache / f"payload-{index:04d}").write_bytes(b"x")
    plan = reset.plan_state_reset(state, scope="all")
    capacity = plan.as_payload()["recovery_metadata"]
    effects = 0
    remove = reset._delete_entries
    def fail_after_remove(entries):
        nonlocal effects
        effects += 1
        remove(entries)
        raise OSError("post-effect capacity fixture")
    monkeypatch.setattr(reset, "_delete_entries", fail_after_remove)
    with pytest.raises(reset.StateResetError) as failure:
        reset.apply_state_reset(plan, confirmation=reset.STATE_RESET_CONFIRMATION)
    if count == 400:
        assert "reset-recovery-metadata-budget-exceeded" in plan.as_payload()["blocked_by"]
        assert capacity["required_bytes"] > capacity["limit_bytes"]
        assert effects == 0
        assert not (state / "state-reset-operations").exists()
    else:
        assert capacity["required_bytes"] <= capacity["limit_bytes"]
        assert effects == 1 and not isinstance(failure.value, reset.StateResetRecoveryRequiredError)
        registry = ArtifactRegistry(state / "artifacts", owner="state-reset")
        operation = next(record.artifact_id for record in registry.records() if record.purpose == "state-reset-operation")
        assert reset.reconcile_state_reset(state, operation)["status"] == "no_changes"
    assert len(tuple(cache.iterdir())) == count
    assert all(path.read_bytes() == b"x" for path in cache.iterdir())


def test_compensation_rebinds_inputs_before_consumers(tmp_path: Path, monkeypatch):
    state, registry = _registered(tmp_path)
    parent, child = state / "parent", state / "child"
    for path in (parent, child):
        path.write_bytes(b"value")
        path.chmod(0o600)
    _claim(registry, "z-parent", parent)
    _claim(registry, "a-child", child, dependencies=("z-parent",))
    retire = inventory.retire_inventory_targets
    def fail_after_retirement(*args, **kwargs):
        retire(*args, **kwargs)
        raise OSError("post-compound-retirement fixture")
    monkeypatch.setattr(inventory, "retire_inventory_targets", fail_after_retirement)
    with pytest.raises(reset.StateResetError) as failure:
        reset.apply_state_reset(reset.plan_state_reset(state, scope="all"), confirmation=reset.STATE_RESET_CONFIRMATION)
    assert not isinstance(failure.value, reset.StateResetRecoveryRequiredError)
    assert registry.verify("z-parent").verified and registry.verify("a-child").verified
    assert registry.verify("a-child").dependencies == ("z-parent",)
    assert not list(tmp_path.glob(".neocortex-state-reset-raw-*"))
