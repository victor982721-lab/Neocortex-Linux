"""Focused integration checks for the optional scratch artifact registry hook."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from neocortex.runtime import scratch
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.scratch import ScratchManager, ScratchSecurityError, ScratchState


class _FakeArtifactRegistry:
    def __init__(self, *, fail_register: bool = False, fail_update: bool = False) -> None:
        self.fail_register = fail_register
        self.fail_update = fail_update
        self.registered: dict[str, dict[str, Any]] = {}
        self.register_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def register(self, **record: Any) -> str:
        self.register_calls.append(dict(record))
        if self.fail_register:
            raise RuntimeError("fixture registry registration failure")
        artifact_id = str(record["artifact_id"])
        self.registered[artifact_id] = dict(record)
        return artifact_id

    def update(self, artifact_id: str, **fields: Any) -> str:
        call = {"artifact_id": artifact_id, **fields}
        self.update_calls.append(call)
        if self.fail_update:
            raise RuntimeError("fixture registry update failure")
        self.registered.setdefault(artifact_id, {}).update(fields)
        return artifact_id


def _manifest(workspace: object) -> dict[str, Any]:
    path = workspace.path
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def test_create_projects_scratch_manifest_into_registry(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    registry = _FakeArtifactRegistry()
    manager = ScratchManager(
        scratch_root,
        owner="fixture-owner",
        create_root=True,
        artifact_registry=registry,
    )

    workspace = manager.create(
        run_id="run-7",
        retain_on_success=True,
        metadata={
            "component": "fixture-component",
            "operation": "fixture-operation",
            "source_ref": {"source_id": "fixture-source"},
            "dependencies": ["fixture-dependency"],
        },
    )

    manifest = _manifest(workspace)
    assert len(registry.register_calls) == 1
    registered = registry.register_calls[0]
    assert registered["artifact_id"] == f"scratch:{workspace.record_id}"
    assert registered["artifact_id"] == manifest["artifact_id"]
    assert registered["owner"] == manifest["owner"] == "fixture-owner"
    assert registered["producer"] == manifest["producer"] == "scratch"
    assert registered["run_id"] == "run-7"
    assert registered["purpose"] == "fixture-operation"
    assert registered["path"] == workspace.path
    assert registered["root"] == scratch_root
    assert registered["kind"] == manifest["kind"] == "temporary"
    assert registered["state"] == manifest["state"] == "active"
    assert registered["path_size_bytes"] == manifest["path_size_bytes"]
    assert registered["path_mtime_ns"] == manifest["path_mtime_ns"]
    assert registered["path_identity"] == tuple(manifest["path_identity"])
    assert registered["source_ref"] == {"source_id": "fixture-source"}
    assert registered["dependencies"] == ["fixture-dependency"]
    assert registered["disposable"] is manifest["disposable"] is True
    assert registered["retain_on_success"] is True
    assert registered["retain_until_ns"] is None
    assert registered["digest"] == manifest["manifest_digest"]
    assert manager.records()[0].artifact_id == manifest["artifact_id"]


def test_lifecycle_updates_registry_before_retirement(tmp_path: Path) -> None:
    registry = _FakeArtifactRegistry()
    manager = ScratchManager(
        tmp_path / "scratch",
        owner="fixture-owner",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)

    workspace.mark_committing()
    assert _manifest(workspace)["state"] == ScratchState.COMMITTING.value
    assert registry.update_calls[-1]["state"] == ScratchState.ACTIVE.value
    completed = workspace.complete(retain=True)
    assert completed is not None
    assert _manifest(workspace)["state"] == ScratchState.COMPLETED.value
    assert [call["state"] for call in registry.update_calls] == [
        ScratchState.ACTIVE.value,
        ScratchState.ACTIVE.value,
        ScratchState.COMPLETED.value,
    ]

    workspace.retire()
    assert not workspace.path.exists()
    assert registry.update_calls[-1]["state"] == ScratchState.RETIRED.value
    assert manager.records() == ()


def test_registry_registration_failure_is_fail_closed_without_cleanup(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    manager = ScratchManager(
        scratch_root,
        create_root=True,
        artifact_registry=_FakeArtifactRegistry(fail_register=True),
    )

    with pytest.raises(ScratchSecurityError, match="artifact registry register failed"):
        manager.create(run_id="failed-registration")

    workspaces = tuple(scratch_root.glob("workspace-*"))
    assert len(workspaces) == 1
    assert workspaces[0].is_dir()
    assert (workspaces[0] / "manifest.json").is_file()
    assert manager.records()[0].state is ScratchState.ACTIVE


def test_registry_update_failure_does_not_publish_scratch_transition(
    tmp_path: Path,
) -> None:
    registry = _FakeArtifactRegistry()
    manager = ScratchManager(
        tmp_path / "scratch",
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(retain_on_success=True)
    registry.fail_update = True

    with pytest.raises(ScratchSecurityError, match="artifact registry update failed"):
        workspace.mark_committing()

    assert _manifest(workspace)["state"] == ScratchState.ACTIVE.value
    assert workspace.path.is_dir()


def test_registry_root_is_constructed_lazily_and_read_only_scans_do_not_call_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[tuple[Path, str, bool]] = []

    class _RootRegistry(_FakeArtifactRegistry):
        def __init__(self, *, root: Path, owner: str, create_root: bool) -> None:
            super().__init__()
            instances.append((root, owner, create_root))

    monkeypatch.setattr(scratch, "ArtifactRegistry", _RootRegistry, raising=False)
    scratch_root = tmp_path / "scratch"
    registry_root = tmp_path / "state" / "artifacts"
    manager = ScratchManager(
        scratch_root,
        owner="fixture-owner",
        create_root=True,
        artifact_registry_root=registry_root,
    )

    assert instances == []
    assert manager.records() == ()
    assert manager.plan().records == ()
    assert instances == []
    workspace = manager.create()
    assert instances == [(registry_root, "fixture-owner", True)]
    assert workspace.path.is_dir()


def test_existing_calls_without_registry_keep_legacy_manifest_shape(tmp_path: Path) -> None:
    manager = ScratchManager(tmp_path / "scratch", create_root=True)
    workspace = manager.create()
    manifest = _manifest(workspace)

    assert "artifact_id" not in manifest
    assert "producer" not in manifest
    assert "kind" not in manifest
    assert "disposable" not in manifest


def test_real_artifact_registry_tracks_a_completed_scratch_workspace(tmp_path: Path) -> None:
    owner = "fixture-owner"
    scratch_root = tmp_path / "scratch"
    registry_root = tmp_path / "state" / "artifacts"
    manager = ScratchManager(
        scratch_root,
        owner=owner,
        create_root=True,
        artifact_registry_root=registry_root,
    )

    workspace = manager.create(
        run_id="real-registry-run",
        retain_on_success=True,
        metadata={"purpose": "real registry fixture"},
    )
    output = workspace.path / "output.bin"
    output.write_bytes(b"durable fixture output")
    output.chmod(0o600)
    workspace.complete(retain=True)

    registry = ArtifactRegistry(registry_root, owner=owner, create_root=False)
    records = registry.records()
    assert len(records) == 1
    record = records[0]
    assert record.artifact_id == workspace.artifact_id
    assert record.owner == owner
    assert record.producer == "scratch"
    assert record.purpose == "real registry fixture"
    assert record.path == workspace.path
    assert record.kind == "temporary"
    assert record.state == "completed"
    assert record.valid is True
    assert record.path_identity == workspace.record.identity
    assert record.disposable is True
    assert record.retain_until_ns is not None


def test_federated_scratch_view_reads_multiple_producer_owners_without_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scratch"
    first = ScratchManager(root, owner="producer-a", create_root=True)
    second = ScratchManager(root, owner="producer-b", create_root=False)
    first.create(retain_on_success=True)
    second.create(retain_on_success=True)

    federated = ScratchManager(root, owner=None, create_root=False)
    records = federated.records()
    assert len(records) == 2
    assert {record.owner for record in records} == {"producer-a", "producer-b"}
    plan = federated.plan()
    assert plan.kept == 2
    with pytest.raises(ScratchSecurityError, match="read-only"):
        federated.create()
