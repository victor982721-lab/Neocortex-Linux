"""Second-pass runtime lifecycle measurements and fail-closed regressions."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from neocortex.runtime import scratch, scratch_contracts
from neocortex.runtime.scratch import ScratchManager, ScratchSecurityError, ScratchState
from neocortex.runtime.scratch_tree import PayloadProfile, TreeObservation


def _manager(tmp_path: Path) -> ScratchManager:
    return ScratchManager(tmp_path / "scratch", create_root=True)


def _payload(workspace, *, size: int = 5) -> Path:
    path = workspace.path / "payload.bin"
    path.write_bytes(b"x" * size)
    os.chmod(path, 0o600)
    return path


def _count_observations(monkeypatch: pytest.MonkeyPatch) -> list[TreeObservation]:
    observed: list[TreeObservation] = []
    original = scratch_contracts.observe_claimed_tree

    def counted(
        path: Path,
        *,
        limit: int = 100_000,
        max_depth: int = 2048,
        max_bytes: int = 1 << 50,
        max_fds: int = 2048,
        profile: PayloadProfile | str = PayloadProfile.STRICT,
        include_control_manifest: bool = False,
    ) -> TreeObservation:
        result = original(
            path,
            limit=limit,
            max_depth=max_depth,
            max_bytes=max_bytes,
            max_fds=max_fds,
            profile=profile,
            include_control_manifest=include_control_manifest,
        )
        observed.append(result)
        return result

    monkeypatch.setattr(scratch_contracts, "observe_claimed_tree", counted)
    return observed


def _state_text(state: ScratchState | str) -> str:
    return state.value if isinstance(state, ScratchState) else state


def test_lifecycle_transitions_keep_one_pre_and_one_post_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)

    committing = manager.create(retain_on_success=True)
    _payload(committing)
    observed = _count_observations(monkeypatch)
    committing.mark_committing()
    assert len(observed) == 2
    committing_manifest = json.loads(
        (committing.path / "manifest.json").read_text(encoding="utf-8")
    )
    assert committing_manifest["state"] == "committing"

    failed = manager.create(retain_on_success=True)
    _payload(failed)
    observed.clear()
    failed_record = failed.fail("fixture failure")
    assert _state_text(failed_record.state) == ScratchState.FAILED_RETAINED.value
    assert len(observed) == 2

    completed = manager.create(retain_on_success=True)
    _payload(completed, size=7)
    observed.clear()
    completed_record = completed.complete(retain=True)
    assert completed_record is not None
    assert _state_text(completed_record.state) == ScratchState.COMPLETED.value
    # complete() has separate COMMITTING and COMPLETED lifecycle fences;
    # each transition observes once before and once after its write.
    assert len(observed) == 4
    assert all(item.complete for item in observed)


def test_completed_payload_reuses_only_pre_accounting_and_catches_post_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    payload = _payload(workspace, size=5)
    original_write = scratch._write_json_atomic

    def write_then_mutate(
        path: Path, value: Mapping[str, Any], **kwargs: Any
    ) -> None:
        original_write(path, value, **kwargs)
        if Path(path) == workspace.path / "manifest.json":
            if value.get("state") == "completed":
                payload.write_bytes(b"changed-after-effect")
                os.chmod(payload, 0o600)

    monkeypatch.setattr(scratch, "_write_json_atomic", write_then_mutate)
    record = workspace.complete(retain=True)

    assert record is not None
    assert record.payload_size_bytes == 5
    assert record.size_bytes == len(b"changed-after-effect")
    assert record.issue == "payload_changed_after_completion"
    assert not record.eligible


def test_post_effect_symlink_aborts_incomplete_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _payload(workspace)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    original_write = scratch._write_json_atomic

    def write_then_link(
        path: Path, value: Mapping[str, Any], **kwargs: Any
    ) -> None:
        original_write(path, value, **kwargs)
        if Path(path) == workspace.path / "manifest.json":
            if value.get("state") == "completed":
                os.symlink(outside, workspace.path / "escape")

    monkeypatch.setattr(scratch, "_write_json_atomic", write_then_link)
    with pytest.raises(ScratchSecurityError, match="incomplete"):
        workspace.complete(retain=True)

    manifest = json.loads((workspace.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "completed"
    assert workspace.path.exists()


def test_post_effect_permission_drift_is_reported_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _payload(workspace)
    original_write = scratch._write_json_atomic

    def write_then_relax_mode(
        path: Path, value: Mapping[str, Any], **kwargs: Any
    ) -> None:
        original_write(path, value, **kwargs)
        if Path(path) == workspace.path / "manifest.json":
            if value.get("state") == "completed":
                os.chmod(workspace.path, 0o750)

    monkeypatch.setattr(scratch, "_write_json_atomic", write_then_relax_mode)
    record = workspace.complete(retain=True)

    assert record is not None
    assert record.issue == "workspace_mode_drift"
    assert not record.eligible


def test_incomplete_post_observation_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _payload(workspace)
    original = scratch_contracts.observe_claimed_tree
    calls = 0

    def incomplete_on_post(
        path: Path,
        *,
        limit: int = 100_000,
        max_depth: int = 2048,
        max_bytes: int = 1 << 50,
        max_fds: int = 2048,
        profile: PayloadProfile | str = PayloadProfile.STRICT,
        include_control_manifest: bool = False,
    ) -> TreeObservation:
        nonlocal calls
        calls += 1
        if calls == 2:
            return TreeObservation(members=1, apparent_bytes=5, complete=False, issue="entry_limit")
        return original(
            path,
            limit=limit,
            max_depth=max_depth,
            max_bytes=max_bytes,
            max_fds=max_fds,
            profile=profile,
            include_control_manifest=include_control_manifest,
        )

    monkeypatch.setattr(scratch_contracts, "observe_claimed_tree", incomplete_on_post)
    with pytest.raises(ScratchSecurityError, match="incomplete"):
        workspace.fail("incomplete fixture")

    assert calls == 2
    manifest = json.loads((workspace.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "failed-retained"


def test_post_observation_issue_is_forwarded_to_record_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    workspace = manager.create(retain_on_success=True)
    _payload(workspace)
    original = scratch_contracts.observe_claimed_tree
    calls = 0

    def issue_on_post(
        path: Path,
        *,
        limit: int = 100_000,
        max_depth: int = 2048,
        max_bytes: int = 1 << 50,
        max_fds: int = 2048,
        profile: PayloadProfile | str = PayloadProfile.STRICT,
        include_control_manifest: bool = False,
    ) -> TreeObservation:
        nonlocal calls
        calls += 1
        if calls == 2:
            return TreeObservation(
                members=1,
                apparent_bytes=5,
                complete=True,
                issue="injected-observation-issue",
            )
        return original(
            path,
            limit=limit,
            max_depth=max_depth,
            max_bytes=max_bytes,
            max_fds=max_fds,
            profile=profile,
            include_control_manifest=include_control_manifest,
        )

    monkeypatch.setattr(scratch_contracts, "observe_claimed_tree", issue_on_post)
    record = workspace.fail("observation issue")

    assert calls == 2
    assert record.size_complete
    assert record.issue == "injected-observation-issue"
    assert not record.eligible
