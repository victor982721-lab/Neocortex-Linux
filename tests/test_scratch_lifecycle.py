"""Focused lifecycle and containment regressions for runtime scratch."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from neocortex.runtime.scratch import (
    SCRATCH_SCHEMA,
    ScratchManager,
    ScratchManifestError,
    ScratchRootError,
    ScratchSecurityError,
    ScratchState,
)


def _private_write(path: Path, data: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _manager(tmp_path: Path) -> tuple[Path, ScratchManager]:
    root = tmp_path / "scratch-root"
    manager = ScratchManager(root, create_root=True)
    assert root.stat().st_mode & 0o777 == 0o700
    return root, manager


def test_create_complete_retained_plan_apply_and_replay(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(run_id="run/with:odd-name", retain_on_success=True)
    result = workspace.path / "resultado [ñ] \n.txt"
    _private_write(result, b"12345")

    with workspace as entered:
        assert entered is workspace
        entered.mark_committing().complete((result,))

    record = manager.records()[0]
    assert record.state is ScratchState.COMPLETED
    assert record.run_id == "run/with:odd-name"
    assert record.result_paths == (result,)
    assert record.size_bytes == 5

    plan = manager.plan(now_ns=10**30)
    assert plan.planned == 1
    assert plan.planned_bytes == 5
    assert plan.applied == 0
    assert plan.kept == plan.blocked == plan.failed == plan.recovery_required == 0

    applied = manager.apply(plan)
    assert applied.planned == 1
    assert applied.applied == 1
    assert applied.applied_bytes == 5
    assert not workspace.path.exists()
    assert manager.records() == ()

    # Applying the same frozen plan again is an idempotent no-op.
    replay = manager.apply(plan)
    assert replay.planned == 0
    assert replay.applied == 0
    assert replay.blocked == replay.failed == replay.recovery_required == 0


def test_success_without_retention_retires_immediately(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create()
    _private_write(workspace.path / "out.bin")
    workspace.complete()
    assert not workspace.path.exists()
    assert manager.records() == ()


def test_crash_like_active_workspace_is_kept_and_not_applied(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _private_write(workspace.path / "partial.bin")

    plan = manager.plan(now_ns=10**30)
    assert plan.planned == 0
    assert plan.kept == 1
    assert plan.records[0].state is ScratchState.ACTIVE

    applied = manager.apply(plan)
    assert applied.applied == 0
    assert workspace.path.exists()
    assert manager.records()[0].state is ScratchState.ACTIVE


def test_failed_workspace_is_retained_and_separated_from_active(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create()
    _private_write(workspace.path / "failed.bin")
    workspace.fail("producer failed")

    plan = manager.plan(now_ns=10**30)
    assert plan.failed == 1
    assert plan.kept == plan.planned == 0
    assert plan.records[0].state is ScratchState.FAILED_RETAINED
    assert manager.apply(plan).applied == 0
    assert workspace.path.exists()


def test_manifest_digest_tamper_is_recovery_required_and_never_deleted(
    tmp_path: Path,
) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _private_write(workspace.path / "payload")
    workspace.complete(retain=True)
    manifest = workspace.path / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["state"] = "completed"
    payload["owner"] = "different-owner"
    # Deliberately preserve the old digest: this is a forged manifest, not a
    # supported lifecycle update.
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(manifest, 0o600)

    plan = manager.plan(now_ns=10**30)
    assert plan.recovery_required == 1
    assert plan.planned == plan.applied == 0
    assert workspace.path.exists()
    applied = manager.apply(plan)
    assert applied.applied == 0
    assert workspace.path.exists()
    assert manager.records()[0].valid is False


def test_workspace_permission_drift_is_blocked_without_removal(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _private_write(workspace.path / "payload")
    workspace.complete(retain=True)
    os.chmod(workspace.path, 0o750)

    plan = manager.plan(now_ns=10**30)
    assert plan.blocked == 1
    assert plan.recovery_required == 0
    assert plan.records[0].issue == "workspace_mode_drift"
    assert manager.apply(plan).applied == 0
    assert workspace.path.exists()


def test_symlink_and_hardlink_payloads_are_blocked(tmp_path: Path) -> None:
    root, manager = _manager(tmp_path)
    outside = tmp_path / "outside"
    _private_write(outside, b"outside")

    symlink_workspace = manager.create(retain_on_success=True)
    symlink_workspace.complete(retain=True)
    os.symlink(outside, symlink_workspace.path / "escape")
    symlink_plan = manager.plan(now_ns=10**30)
    assert symlink_plan.blocked == 1
    assert symlink_workspace.path.exists()

    hardlink_workspace = manager.create(retain_on_success=True)
    payload = hardlink_workspace.path / "payload"
    _private_write(payload)
    hardlink_workspace.complete(retain=True)
    os.link(payload, hardlink_workspace.path / "payload-alias")
    # A later hardlink drift is the guarded apply case.
    hardlink_plan = manager.plan(now_ns=10**30)
    assert hardlink_plan.blocked == 2
    assert hardlink_workspace.path.exists()
    assert root.exists()


def test_prefix_neighbor_without_manifest_is_unmanaged_and_untouched(tmp_path: Path) -> None:
    root, manager = _manager(tmp_path)
    neighbor = root / "scratch-neighbor"
    neighbor.mkdir(mode=0o700)
    marker = neighbor / "do-not-touch"
    _private_write(marker)
    unrelated = root / "ordinary-neighbor"
    unrelated.mkdir(mode=0o700)
    _private_write(unrelated / "keep")

    plan = manager.plan(now_ns=10**30)
    assert plan.records == ()
    assert plan.unmanaged == (neighbor,)
    assert marker.exists()
    assert (unrelated / "keep").exists()
    assert manager.apply(plan).applied == 0
    assert marker.exists()


def test_read_only_manager_never_creates_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "not-created"
    manager = ScratchManager(root)
    assert manager.records() == ()
    plan = manager.plan(now_ns=123)
    assert plan.root_blocked == "scratch root is absent"
    assert not root.exists()
    with pytest.raises(ScratchRootError):
        manager.create()
    assert not root.exists()


def test_manifest_is_exclusive_and_schema_bound(tmp_path: Path) -> None:
    root, manager = _manager(tmp_path)
    workspace = manager.create(run_id=7, metadata={"schema_hint": SCRATCH_SCHEMA})
    manifest = workspace.path / "manifest.json"
    assert manifest.stat().st_mode & 0o777 == 0o600
    first = manifest.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    assert manifest.read_text(encoding="utf-8") == first
    assert workspace.record.root_identity is not None
    assert workspace.record.identity[0] == root.stat().st_dev


def test_result_path_outside_workspace_is_rejected_without_state_change(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    outside = tmp_path / "not-a-result"
    _private_write(outside)
    with pytest.raises(ScratchSecurityError):
        workspace.complete((outside,), retain=True)
    assert workspace.record.state is ScratchState.ACTIVE
    assert workspace.path.exists()


def test_workspace_handle_reads_invalid_manifest_as_untrusted(tmp_path: Path) -> None:
    _, manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    manifest = workspace.path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    os.chmod(manifest, 0o600)
    with pytest.raises(ScratchManifestError):
        _ = workspace.state
