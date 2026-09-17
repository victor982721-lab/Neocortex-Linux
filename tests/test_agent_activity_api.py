"""Public external-activity lifecycle over the canonical scratch/registry owners."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from neocortex.api.agent_activity import (
    AgentActivity,
    AgentActivityChanged,
    AgentActivityConflict,
    AgentActivityRecoveryRequired,
    DEFAULT_AGENT_OWNER,
)
from neocortex.api.cli.cli_agent_activity import register_agent_activity_arguments, run_agent_activity
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.scratch import ScratchManager


def test_public_activity_survives_external_process_and_replays_from_new_process(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    published = tmp_path / "published"
    published.mkdir(mode=0o700)
    existing = published / "preexisting.txt"
    existing.write_bytes(b"keep this file")
    os.chmod(existing, 0o600)

    activity = AgentActivity.prepare(state, "agent-run-1")
    result = activity.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('deliverable.txt').write_bytes(b'agent output')",
        ]
    )
    assert result.returncode == 0
    source = activity.path / "deliverable.txt"
    published_result = activity.publish(source, published / "deliverable.txt")
    assert published_result.status == "published"
    source.write_bytes(b"changed before close")
    with pytest.raises(AgentActivityChanged, match="source changed after publication"):
        activity.close((source,))
    source.write_bytes(b"agent output")
    closed = activity.close((source,))
    assert closed.state == "completed"

    # The second process gets only the state root and stable activity id; no
    # Python workspace object or manually coordinated manifest is reused.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from neocortex.api.agent_activity import AgentActivity; "
                "a=AgentActivity.resume(__import__('sys').argv[1], 'agent-run-1'); "
                "print(a.snapshot().to_dict()['state'])"
            ),
            str(state),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "completed"
    replay = activity.publish(source, published / "deliverable.txt")
    assert replay.status == "already_published"
    assert existing.read_bytes() == b"keep this file"

    # Equal-size replacement is a material late change and cannot be retired.
    source.write_bytes(b"changed now")
    with pytest.raises(AgentActivityChanged, match="changed after close"):
        activity.retire()
    assert source.exists()


def test_activity_publication_does_not_adopt_preexisting_destination(tmp_path: Path) -> None:
    state = tmp_path / "state"
    published = tmp_path / "published"
    published.mkdir(mode=0o700)
    destination = published / "result.txt"
    destination.write_bytes(b"personal")
    os.chmod(destination, 0o600)
    activity = AgentActivity.prepare(state, "collision")
    source = activity.path / "result.txt"
    source.write_bytes(b"agent")
    with pytest.raises(AgentActivityConflict, match="already exists"):
        activity.publish(source, destination)
    assert destination.read_bytes() == b"personal"


def test_activity_reconciles_registry_failure_after_publication_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    published = tmp_path / "published"
    published.mkdir(mode=0o700)
    activity = AgentActivity.prepare(state, "registry-retry")
    source = activity.path / "result.txt"
    source.write_bytes(b"retryable")
    original_register = activity._registry.register
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated registry outage")
        return original_register(*args, **kwargs)

    monkeypatch.setattr(activity._registry, "register", fail_once)
    with pytest.raises(AgentActivityRecoveryRequired, match="registry confirmation"):
        activity.publish(source, published / "result.txt")
    assert (published / "result.txt").read_bytes() == b"retryable"
    recovered = activity.reconcile("publish")
    assert recovered.status in {"published", "already_published"}


def test_activity_cli_uses_public_surface_and_canonical_owner(tmp_path: Path, capsys) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", type=Path, required=True)
    register_agent_activity_arguments(parser)
    state = tmp_path / "state"
    args = parser.parse_args(
        [
            "--state-directory",
            str(state),
            "--agent-activity-id",
            "cli-run",
            "--agent-action",
            "prepare",
            "--agent-json",
        ]
    )
    assert run_agent_activity(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "neocortex.agent-activity-cli/v1"
    assert payload["result"]["owner"] == DEFAULT_AGENT_OWNER

    status_args = parser.parse_args(
        [
            "--state-directory",
            str(state),
            "--agent-activity-id",
            "cli-run",
            "--agent-action",
            "status",
            "--agent-json",
        ]
    )
    assert run_agent_activity(status_args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["state"] == "active"


def test_activity_registry_projection_uses_real_owner_and_no_parallel_store(tmp_path: Path) -> None:
    state = tmp_path / "state"
    activity = AgentActivity.prepare(state, "projection")
    manager = ScratchManager(
        state / "scratch" / "owned-temp",
        owner=DEFAULT_AGENT_OWNER,
        create_root=False,
        artifact_registry=ArtifactRegistry(state / "artifacts", owner=DEFAULT_AGENT_OWNER),
    )
    records = manager.records()
    assert len(records) == 1
    assert records[0].artifact_id == f"scratch:{activity.workspace_id}"
    registry = ArtifactRegistry(state / "artifacts", owner=DEFAULT_AGENT_OWNER)
    projected = registry.verify(records[0].artifact_id)
    assert projected.owner == DEFAULT_AGENT_OWNER
    assert projected.metadata["agent_activity"]["activity_id"] == "projection"
