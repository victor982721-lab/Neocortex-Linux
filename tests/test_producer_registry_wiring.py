"""Focused producer-to-artifact-registry wiring contracts."""

from __future__ import annotations

import os
from pathlib import Path

from neocortex.capabilities.formats.archive import materialization
from neocortex.runtime.artifact_registry import ArtifactRegistry, ArtifactState
from neocortex.runtime.scratch import ScratchManager


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def test_canonical_archive_scratch_creates_manifest_and_registry_claim(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "state")
    scratch = state / "scratch" / "archive-materialization"
    registry_root = state / "artifacts"

    with materialization._registered_scratch_workspace(scratch) as workspace_path:
        scratch_manifest = workspace_path / "manifest.json"
        registry_manifests = tuple(registry_root.glob("artifact-*.json"))

        assert scratch_manifest.is_file()
        assert len(registry_manifests) == 1
        records = ArtifactRegistry(
            registry_root,
            owner=materialization.REGISTERED_SCRATCH_OWNER,
            create_root=False,
        ).records()
        assert len(records) == 1
        record = records[0]
        assert record.owner == materialization.REGISTERED_SCRATCH_OWNER
        assert record.path == workspace_path
        assert record.root == registry_root
        assert record.valid is True

    # Successful lifecycle cleanup removes only the fixture workspace.  The
    # private control journal is durable, while the registry keeps a retired
    # tombstone rather than an active claim.
    scratch_entries = tuple(scratch.iterdir())
    assert {entry.name for entry in scratch_entries} == {".scratch-control"}
    assert len(scratch_entries) == 1
    scratch_control = scratch_entries[0]
    assert scratch_control.is_dir()
    assert not scratch_control.is_symlink()
    assert scratch_control.stat().st_mode & 0o077 == 0
    assert not tuple(scratch.glob("workspace-*"))
    registry_records_after = ArtifactRegistry(
        registry_root,
        owner=materialization.REGISTERED_SCRATCH_OWNER,
        create_root=False,
    ).records()
    assert len(registry_records_after) == 1
    retired_record = registry_records_after[0]
    assert retired_record.state == ArtifactState.RETIRED.value
    assert retired_record.valid is True
    assert registry_manifests[0].is_file()


def test_artifact_registry_root_stays_absent_for_read_only_scratch_plan(
    tmp_path: Path,
) -> None:
    state = _private_directory(tmp_path / "state")
    scratch = state / "scratch" / "archive-materialization"
    registry_root = state / "artifacts"
    manager = ScratchManager(
        scratch,
        owner=materialization.REGISTERED_SCRATCH_OWNER,
        create_root=False,
        artifact_registry_root=registry_root,
    )

    assert manager.records() == ()
    assert manager.plan().records == ()
    assert not scratch.exists()
    assert not registry_root.exists()
