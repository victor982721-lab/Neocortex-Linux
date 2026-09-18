"""Granted producer profiles remain consistent through registry and retirement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neocortex.api.agent_activity import AgentActivity, AgentActivityConflict
from neocortex.runtime.artifact_registry import ArtifactRegistry


def test_activity_fixture_grant_covers_registry_seal_retirement_without_touching_external(tmp_path: Path):
    state = tmp_path / "state"
    external = tmp_path / "external"
    external.write_bytes(b"original")
    external.chmod(0o600)
    before = external.stat()
    activity = AgentActivity.prepare(state, "posix", payload_profile="fixture_posix_v1",
                    fixture_creation_grant_id="fixture-operation", fixture_authorized=True)
    os.mkfifo(activity.path / "fifo", 0o600)
    (activity.path / "symlink").symlink_to(external)
    (activity.path / "broken").symlink_to(tmp_path / "absent")
    os.link(external, activity.path / "shared")
    activity.close()
    registry = ArtifactRegistry(state / "artifacts", owner="neocortex-framework")
    record = registry.verify(activity.artifact_id)
    assert record.verified, record.to_dict()
    assert record.allocated_bytes is not None
    assert record.to_dict()["exclusive_reclaimable_bytes"] is None
    activity.retire()
    assert not activity.path.exists()
    assert external.read_bytes() == b"original"
    assert external.stat().st_ino == before.st_ino
    assert external.stat().st_mode == before.st_mode


def test_metadata_cannot_request_fixture_profile(tmp_path: Path):
    with pytest.raises(AgentActivityConflict, match="explicit creation grant"):
        AgentActivity.prepare(tmp_path / "state", "fake", payload_profile="fixture_posix_v1",
                              metadata={"fixture_authorized": True})


def test_posix_name_roundtrips_through_registry_and_hygiene_fingerprint(tmp_path: Path):
    from neocortex.runtime.hygiene import HygieneManager
    from neocortex.runtime.path_identity import PathIdentity
    path = tmp_path / os.fsdecode(b"unencoded-\xff")
    path.mkdir(mode=0o700)
    (path / os.fsdecode(b"payload-\xfe")).write_bytes(b"content")
    registry = ArtifactRegistry(tmp_path / "registry", owner="fixture", create_root=True)
    registry.register("posix", producer="fixture", purpose="explicit exact source", path=path,
                      root=registry.root, state="completed")
    record = registry.verify("posix")
    assert record.verified
    value = record.to_dict()["posix_path_identity"]
    assert PathIdentity.from_dict(value).to_path() == path
    plan = HygieneManager(artifact_registry=registry).plan()
    assert plan.fingerprint.startswith("sha256:")
