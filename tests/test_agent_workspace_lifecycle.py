"""Deterministic external-activity demonstration over existing owners."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.scratch import ScratchManager


def test_external_activity_uses_registered_scratch_and_publishes_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    scratch_root = state / "scratch" / "owned-temp"
    registry_root = state / "artifacts"
    published_root = tmp_path / "published"
    published_root.mkdir(mode=0o700)
    existing = published_root / "preexisting.txt"
    existing.write_bytes(b"do not rewrite")
    os.chmod(existing, 0o600)

    owner = "agent-fixture"
    registry = ArtifactRegistry(registry_root, owner=owner, create_root=True)
    manager = ScratchManager(
        scratch_root,
        owner=owner,
        create_root=True,
        artifact_registry=registry,
    )
    workspace = manager.create(
        run_id="agent-run-1",
        retain_on_success=True,
        metadata={"activity": "deterministic-agent-fixture", "provenance": "local-test"},
    )

    interrupted = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "p=Path(sys.argv[1])/'draft.txt'; p.write_bytes(b'partial'); sys.exit(7)",
            str(workspace.path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert interrupted.returncode == 7
    assert manager.plan(now_ns=10**30).kept == 1

    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "p=Path(sys.argv[1])/'draft.txt'; p.write_bytes(b'final deliverable')",
            str(workspace.path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0
    draft = workspace.path / "draft.txt"
    deliverable = published_root / "deliverable.txt"
    deliverable.write_bytes(draft.read_bytes())
    os.chmod(deliverable, 0o600)
    published = registry.register(
        "agent-deliverable",
        path=deliverable,
        root=published_root,
        producer=owner,
        purpose="agent published deliverable",
        state="completed",
        kind="canonical",
        disposable=False,
        source_ref={"workspace_id": workspace.record_id, "run_id": "agent-run-1"},
        created_ns=1,
    )
    workspace.complete((draft,), retain=True)
    assert registry.verify(published.artifact_id).verified is True
    assert existing.read_bytes() == b"do not rewrite"

    failed = manager.create(retain_on_success=True)
    failed.fail("deterministic exception")
    assert manager.plan(now_ns=10**30).failed == 1

    applied = manager.apply(now_ns=10**30)
    assert applied.applied == 1
    assert not workspace.path.exists()
    assert deliverable.exists()
    assert registry.verify(published.artifact_id).verified is True

    replay = manager.apply(now_ns=10**30)
    assert replay.applied == 0
    assert replay.planned == 0
    assert existing.read_bytes() == b"do not rewrite"
