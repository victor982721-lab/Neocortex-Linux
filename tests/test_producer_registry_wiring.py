"""Focused producer-to-artifact-registry wiring contracts."""

from __future__ import annotations

import os
from pathlib import Path

from neocortex.capabilities.formats.archive import materialization
from neocortex.runtime.artifact_registry import ArtifactRegistry
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

    # Successful lifecycle cleanup removes only the fixture workspace; the
    # producer registry claim remains durable for the maintenance owner.
    assert tuple(scratch.iterdir()) == ()
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
