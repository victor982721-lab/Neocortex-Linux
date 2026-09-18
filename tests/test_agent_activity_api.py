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
from neocortex.workflow.retention.planner import TerminalRetentionPolicy


def test_public_activity_publication_replays_from_new_process_and_preserves_seal(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    published = tmp_path / "published"
    published.mkdir(mode=0o700)
    existing = published / "preexisting.txt"
    existing.write_bytes(b"keep this file")
    os.chmod(existing, 0o600)

    activity = AgentActivity.prepare(state, "agent-run-1")
    source = activity.path / "deliverable.txt"
    source.write_bytes(b"agent output")
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


def test_failed_activity_can_be_explicitly_reconciled_and_terminal_tombstone_replayed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    activity = AgentActivity.prepare(state, "terminal-release")
    (activity.path / "failed.bin").write_bytes(b"failed payload")
    activity.reconcile("fail", reason="producer crashed")

    # A fresh facade performs the explicit owner-authorized reconciliation;
    # age/PID absence alone never changes the failed-retained state.
    resumed = AgentActivity.resume(state, "terminal-release")
    released = resumed.reconcile(
        "release",
        release_authorized=True,
        evidence={"recovered": True},
    )
    assert released.state == "completed"
    retired = resumed.retire()
    assert retired.state == "retired"

    policy = TerminalRetentionPolicy(minimum_age_ns=0, tombstone_count=0)
    plan = resumed.terminal_retention_plan(policy=policy, now_ns=10**30)
    assert plan.status == "ready"
    assert plan.tombstone_count == 1
    assert plan.eligible_count == 1
    assert plan.physical_accounting_complete is True
    receipt = resumed.apply_terminal_retention(
        policy=policy,
        now_ns=10**30,
        release_authorized=True,
        operation_id="terminal-release-op",
    )["receipt"]
    assert receipt["status"] == "applied"
    replay = resumed.apply_terminal_retention(
        policy=policy,
        now_ns=10**30,
        release_authorized=True,
        operation_id="terminal-release-op",
    )["receipt"]
    assert replay["status"] == "applied"
    assert not list((state / "artifacts").glob("scratch-*.json"))


def test_maintenance_owner_honors_activity_seal_for_equal_size_late_change(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    activity = AgentActivity.prepare(state, "sealed-maintenance")
    payload = activity.path / "payload.bin"
    payload.write_bytes(b"abc")
    activity.close((payload,))
    payload.write_bytes(b"xyz")

    registry = ArtifactRegistry(state / "artifacts", owner=DEFAULT_AGENT_OWNER)
    manager = ScratchManager(
        state / "scratch" / "owned-temp",
        owner=DEFAULT_AGENT_OWNER,
        create_root=False,
        artifact_registry=registry,
    )
    result = manager.apply(now_ns=10**30)
    assert result.applied == 0
    assert result.blocked == 1
    assert result.records[0].issue == "workspace_seal_drift"
    assert payload.exists()


def test_public_activity_rejects_unregistered_owner(tmp_path: Path) -> None:
    with pytest.raises(AgentActivityConflict, match="not registered"):
        AgentActivity.prepare(tmp_path / "state", "unregistered", owner="external-agent")


def test_activity_cli_surfaces_external_process_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", type=Path, required=True)
    register_agent_activity_arguments(parser)
    state = tmp_path / "state"
    prepare = parser.parse_args(
        [
            "--state-directory",
            str(state),
            "--agent-activity-id",
            "failed-cli",
            "--agent-action",
            "prepare",
            "--agent-json",
        ]
    )
    assert run_agent_activity(prepare) == 0
    capsys.readouterr()
    failed = parser.parse_args(
        [
            "--state-directory",
            str(state),
            "--agent-activity-id",
            "failed-cli",
            "--agent-action",
            "run",
            "--agent-json",
            "--agent-command",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
    )
    assert run_agent_activity(failed) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["result"]["returncode"] == 7
    assert AgentActivity.resume(state, "failed-cli").state == "failed-retained"
