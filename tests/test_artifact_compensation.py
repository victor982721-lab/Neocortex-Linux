"""Exact pre-effect enrollment, physical rollback proof and owner-safe replay."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from neocortex.persistence.state_reset import plan_state_reset
from neocortex.persistence.state_reset_recovery import ResetOperation
from neocortex.runtime.artifact_registry import ArtifactRegistry, ArtifactSecurityError


def _retired(tmp_path: Path, *, directory: bool = False, enroll: bool = True):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    registry = ArtifactRegistry(state / "artifacts", owner="fixture", create_root=True)
    path = state / "workspace"
    staged = tmp_path / "restored"
    if directory:
        path.mkdir(mode=0o700)
        (path / "nested").mkdir(mode=0o700)
        payload = path / "nested" / "output"
    else:
        payload = path
    payload.write_bytes(b"original verified content")
    payload.chmod(0o600)
    original = registry.register("fixture:payload", producer="fixture", path=path, root=registry.root,
        purpose="fixture", state="completed", kind="temporary", disposable=True,
        metadata={"preserved_producer_intent": "same-value"})
    operation = ResetOperation.prepare(plan_state_reset(state, scope="all"), None)
    operation.update("applying")
    if enroll:
        prepared = registry.prepare_retirement_compensation(original.artifact_id,
                                                recovery_artifact_id=operation.operation_id)
        assert registry.prepare_retirement_compensation(original.artifact_id,
            recovery_artifact_id=operation.operation_id).manifest_digest == prepared.manifest_digest
    # The independently staged inode exists before the original is removed,
    # so inode reuse cannot make missing promotion evidence appear sufficient.
    if directory:
        shutil.copytree(path, staged)
        candidates = (staged, *staged.rglob("*"))
    else:
        shutil.copyfile(path, staged)
        staged.chmod(0o600)
        candidates = (staged,)
    for candidate in candidates:
        target = path / candidate.relative_to(staged) if directory else path
        metadata = candidate.stat()
        proof = {"owner": "raw-rollback", "identity": [metadata.st_dev, metadata.st_ino],
                 "size": metadata.st_size}
        if candidate.is_dir():
            proof["kind"] = "directory"
        else:
            proof["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        operation.promotions[str(target)] = proof
    operation.update("applying")
    with registry.retirement_guard(original.artifact_id):
        shutil.rmtree(path) if directory else path.unlink()
        registry.update(original.artifact_id, state="retired")
    current = registry.verify(original.artifact_id)
    staged.rename(path)
    return registry, original, operation, current, payload


@pytest.mark.parametrize("directory", [False, True])
def test_owner_rebinds_only_verified_restored_payload_and_replays(tmp_path: Path, directory: bool):
    registry, original, operation, current, payload = _retired(tmp_path, directory=directory)
    restored = registry.reconcile_restored_retirement(original.artifact_id,
        recovery_artifact_id=operation.operation_id,
        expected_retirement_manifest_digest=current.manifest_digest)
    assert restored.verified and restored.state == original.state
    assert restored.owner == original.owner and restored.path == original.path
    assert restored.path_identity != original.path_identity
    assert restored.metadata["preserved_producer_intent"] == "same-value"
    assert restored.metadata["retirement_compensation_receipt"]["original_manifest_digest"] == original.manifest_digest
    before = registry.manifest_path(original.artifact_id).read_bytes()
    fresh = ArtifactRegistry(registry.root, owner="fixture")
    replay = fresh.reconcile_restored_retirement(original.artifact_id,
        recovery_artifact_id=operation.operation_id,
        expected_retirement_manifest_digest=restored.manifest_digest)
    assert replay.verified
    assert registry.manifest_path(original.artifact_id).read_bytes() == before
    assert payload.read_bytes() == b"original verified content"


@pytest.mark.parametrize("attack", ["unenrolled", "owner", "operation", "stale", "content", "promotion"])
def test_compensation_refuses_authority_and_content_substitution(tmp_path: Path, attack: str):
    registry, original, operation, current, payload = _retired(tmp_path, enroll=attack != "unenrolled")
    if attack == "content":
        payload.write_bytes(b"personal changed content")
    if attack == "promotion":
        operation.promotions.clear()
        operation.update("rollback-failed")
    before = registry.manifest_path(original.artifact_id).read_bytes()
    owner = registry.for_owner("other") if attack == "owner" else registry
    with pytest.raises(ArtifactSecurityError):
        owner.reconcile_restored_retirement(original.artifact_id,
            recovery_artifact_id="another-operation" if attack == "operation" else operation.operation_id,
            expected_retirement_manifest_digest="stale-digest" if attack == "stale" else current.manifest_digest)
    assert registry.manifest_path(original.artifact_id).read_bytes() == before
    assert payload.exists()


def test_compensation_replay_refuses_a_later_replacement(tmp_path: Path):
    registry, original, operation, current, payload = _retired(tmp_path)
    restored = registry.reconcile_restored_retirement(original.artifact_id,
        recovery_artifact_id=operation.operation_id,
        expected_retirement_manifest_digest=current.manifest_digest)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(payload.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, payload)
    with pytest.raises(ArtifactSecurityError, match="changed after recovery"):
        registry.reconcile_restored_retirement(original.artifact_id,
            recovery_artifact_id=operation.operation_id,
            expected_retirement_manifest_digest=restored.manifest_digest)
