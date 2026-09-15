"""Focused safety and lifecycle regressions for the private artifact registry."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from neocortex.runtime.artifact_registry import (
    ARTIFACT_REGISTRY_SCHEMA,
    ArtifactConflictError,
    ArtifactRegistry,
    ArtifactSecurityError,
)
from neocortex.runtime.scratch import ScratchManager


def _private_file(path: Path, payload: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _registry(tmp_path: Path) -> tuple[Path, ArtifactRegistry]:
    root = tmp_path / "artifact-registry"
    registry = ArtifactRegistry(root)
    return root, registry


def _register(
    registry: ArtifactRegistry,
    tmp_path: Path,
    name: str = "cache.bin",
    **kwargs: object,
):
    path = tmp_path / name
    _private_file(path, b"012345")
    return registry.register(
        name,
        path=path,
        producer="fixture-producer",
        purpose="fixture-purpose",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=10,
        **kwargs,
    )


def test_plan_is_read_only_and_does_not_create_missing_root(tmp_path: Path) -> None:
    root, registry = _registry(tmp_path)
    before = tuple(tmp_path.iterdir())

    plan = registry.plan(now_ns=100)

    assert plan.root_blocked == "artifact registry root is absent"
    assert plan.read_only is True
    assert plan.counts == {
        "scanned": 0,
        "returned": 0,
        "protected": 0,
        "eligible": 0,
        "blocked": 0,
        "unknown": 0,
        "unmanaged": 0,
    }
    assert tuple(tmp_path.iterdir()) == before
    assert not root.exists()


def test_register_writes_private_atomic_schema_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    root, registry = _registry(tmp_path)
    record = _register(registry, tmp_path)
    manifest = registry.manifest_path(record.artifact_id)

    assert root.stat().st_mode & 0o777 == 0o700
    assert manifest.stat().st_mode & 0o777 == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == ARTIFACT_REGISTRY_SCHEMA
    assert payload["artifact_id"] == record.artifact_id
    assert payload["path_identity"] == list(record.path_identity)
    assert "012345" not in manifest.read_text(encoding="utf-8")
    first_bytes = manifest.read_bytes()

    replay = registry.register(
        record.artifact_id,
        path=record.path,
        producer=record.producer,
        purpose=record.purpose,
        state=record.state,
        kind=record.kind,
        disposable=record.disposable,
        created_ns=record.created_ns,
        retain_until_ns=record.retain_until_ns,
        ttl_ns=record.ttl_ns,
    )

    assert replay == record
    assert manifest.read_bytes() == first_bytes
    assert tuple(root.glob("*.json")) == (manifest,)
    with pytest.raises(ArtifactConflictError):
        registry.register(
            record.artifact_id,
            path=record.path,
            producer="different-producer",
            purpose=record.purpose,
            state=record.state,
            kind=record.kind,
            disposable=record.disposable,
        )


def test_ttl_protects_then_expires_without_an_effect(tmp_path: Path) -> None:
    root, registry = _registry(tmp_path)
    record = _register(registry, tmp_path, ttl_ns=20)
    manifest_before = registry.manifest_path(record.artifact_id).read_bytes()

    protected = registry.plan(now_ns=29)
    eligible = registry.plan(now_ns=30)

    assert protected.protected == 1
    assert protected.eligible == 0
    assert protected.reason_counts == {"retention_active": 1}
    assert eligible.protected == 0
    assert eligible.eligible == 1
    assert eligible.eligible_bytes == 6
    assert registry.manifest_path(record.artifact_id).read_bytes() == manifest_before
    assert root.exists()
    assert record.path.exists()


def test_identity_and_permission_drift_are_blocked_fail_closed(tmp_path: Path) -> None:
    _, registry = _registry(tmp_path)
    record = _register(registry, tmp_path)
    record.path.unlink()
    _private_file(record.path, b"replacement")

    drift = registry.plan(now_ns=100)
    assert drift.blocked == 1
    assert drift.unknown == 0
    assert drift.blocked_records[0].issue == "artifact_identity_drift"

    os.chmod(record.path, 0o644)
    permissions = registry.plan(now_ns=100)
    assert permissions.blocked == 1
    assert permissions.blocked_records[0].issue == "artifact_permission_drift"
    assert record.path.exists()


def test_symlink_and_hardlink_drift_never_become_eligible(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    _private_file(outside, b"outside")
    _, registry = _registry(tmp_path)
    record = _register(registry, tmp_path, name="links.bin")
    record.path.unlink()
    os.symlink(outside, record.path)
    symlink_plan = registry.plan(now_ns=100)
    assert symlink_plan.blocked == 1
    assert symlink_plan.blocked_records[0].issue == "artifact_symlink"

    record.path.unlink()
    _private_file(record.path, b"hardlink")
    alias = tmp_path / "hardlink-alias.bin"
    os.link(record.path, alias)
    hardlink_plan = registry.plan(now_ns=100)
    assert hardlink_plan.blocked == 1
    assert hardlink_plan.blocked_records[0].issue == "artifact_hardlink"
    assert record.path.exists() and alias.exists()


def test_unknown_manifest_and_unmanaged_neighbor_are_separate(tmp_path: Path) -> None:
    root, registry = _registry(tmp_path)
    _ = _register(registry, tmp_path)
    unknown = root / "foreign.json"
    unknown.write_text("{\"schema\": \"future\"}", encoding="utf-8")
    os.chmod(unknown, 0o600)
    neighbor = root / "neighbor.bin"
    _private_file(neighbor, b"do-not-adopt")
    before = (unknown.read_bytes(), neighbor.read_bytes())

    plan = registry.plan(now_ns=100)

    assert plan.unknown == 1
    assert plan.unmanaged == (neighbor,)
    assert plan.unknown_records[0].path == unknown
    assert unknown.read_bytes() == before[0]
    assert neighbor.read_bytes() == before[1]


def test_update_is_atomic_and_replay_is_a_noop(tmp_path: Path) -> None:
    _, registry = _registry(tmp_path)
    record = _register(registry, tmp_path, ttl_ns=100)
    manifest = registry.manifest_path(record.artifact_id)
    first = manifest.read_bytes()

    updated = registry.update(record.artifact_id, state="failed", updated_ns=20)
    second = manifest.read_bytes()
    replay = registry.update(record.artifact_id, state="failed", updated_ns=20)

    assert updated.state == "failed"
    assert updated.updated_ns == 20
    assert second != first
    assert replay == updated
    assert manifest.read_bytes() == second
    assert registry.plan(now_ns=10_000).protected == 1


def test_active_artifact_may_grow_before_completion(tmp_path: Path) -> None:
    _, registry = _registry(tmp_path)
    path = tmp_path / "growing.bin"
    _private_file(path, b"a")
    record = registry.register(
        "growing",
        path=path,
        producer="fixture-producer",
        purpose="fixture-purpose",
        kind="temporary",
        state="active",
        disposable=True,
        created_ns=10,
    )
    path.write_bytes(b"grown")
    os.chmod(path, 0o600)

    completed = registry.update(record.artifact_id, state="completed", updated_ns=20)
    assert completed.valid is True
    assert completed.size_bytes == 5
    assert registry.plan(now_ns=100).eligible == 1


def test_bounds_surface_truncation_without_adopting_neighbors(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry = ArtifactRegistry(root, max_records=1)
    _register(registry, tmp_path, name="one.bin")
    _private_file(root / "unmanaged.bin", b"neighbor")

    plan = registry.plan(now_ns=100)

    assert plan.truncated is True
    assert "record_limit" in plan.truncation_reasons
    assert plan.returned <= 1
    assert (root / "unmanaged.bin").exists()


def test_verify_is_detailed_but_truthy_only_when_revalidated(tmp_path: Path) -> None:
    _, registry = _registry(tmp_path)
    record = _register(registry, tmp_path)

    verified = registry.verify(record.artifact_id)
    assert verified
    assert verified.verified is True
    assert registry.verify_bool(record.artifact_id) is True
    record.path.unlink()
    missing = registry.verify(record.artifact_id)
    assert not missing
    assert missing.issue == "artifact_missing"
    assert registry.verify_bool(record.artifact_id) is False


def test_register_rejects_links_and_non_private_artifacts(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _private_file(outside)
    _, registry = _registry(tmp_path)
    symlink = tmp_path / "symlink"
    os.symlink(outside, symlink)
    with pytest.raises(ArtifactSecurityError):
        registry.register("symlink", path=symlink)

    public = tmp_path / "public"
    _private_file(public)
    os.chmod(public, 0o644)
    with pytest.raises(ArtifactSecurityError):
        registry.register("public", path=public)


def test_federated_read_view_classifies_multiple_producer_owners(tmp_path: Path) -> None:
    root = tmp_path / "artifact-registry"
    first_path = tmp_path / "first.bin"
    second_path = tmp_path / "second.bin"
    _private_file(first_path, b"first")
    _private_file(second_path, b"second")

    first = ArtifactRegistry(root, owner="producer-a")
    second = ArtifactRegistry(root, owner="producer-b")
    first_record = first.register(
        "first",
        path=first_path,
        producer="producer-a",
        purpose="fixture",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=1,
    )
    second.register(
        "second",
        path=second_path,
        producer="producer-b",
        purpose="fixture",
        state="completed",
        kind="cache",
        disposable=True,
        created_ns=1,
    )

    federated = ArtifactRegistry(root, owner=None)
    plan = federated.plan(now_ns=2)

    assert plan.eligible == 2
    assert {record.owner for record in plan.eligible_records} == {
        "producer-a",
        "producer-b",
    }
    with pytest.raises(ArtifactSecurityError, match="read-only"):
        federated.update(first_record.artifact_id, state="failed")
    with pytest.raises(ArtifactSecurityError, match="read-only"):
        federated.register(
            "third",
            path=tmp_path / "third.bin",
            producer="producer-c",
            purpose="fixture",
        )


def test_scratch_lifecycle_can_publish_and_retire_a_registered_directory(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    registry = ArtifactRegistry(
        tmp_path / "registry",
        owner="fixture-owner",
        create_root=True,
    )
    manager = ScratchManager(
        scratch_root,
        owner="fixture-owner",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)
    output = workspace.path / "output.txt"
    # Ordinary nested output permissions are allowed inside the private
    # artifact boundary; links and ownership remain guarded.
    output.write_text("fixture", encoding="utf-8")
    workspace.mark_committing()
    workspace.complete(retain=True)

    completed = registry.plan(now_ns=10**30)
    assert completed.eligible == 1
    workspace.retire()
    retired = registry.plan(now_ns=10**30)
    assert retired.protected == 1
    assert retired.blocked == 0
    assert not workspace.path.exists()
