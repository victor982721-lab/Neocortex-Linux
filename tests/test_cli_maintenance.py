"""CLI contracts for the registered scratch maintenance leaf."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.runtime.artifact_registry import ArtifactRegistry
from neocortex.runtime.scratch import ScratchManager


def _invoke(args: list[str], capsys) -> tuple[int, dict[str, object]]:
    exit_code = main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    return exit_code, json.loads(captured.out)


def test_missing_root_is_read_only_and_not_created(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "owned-temp",
            "--maintenance-json",
            "--state-directory",
            str(state),
        ],
        capsys,
    )
    assert exit_code == 0
    assert payload["schema"] == "neocortex.maintenance/v1"
    assert payload["status"] == "planned"
    assert payload["planned"] == 0
    assert payload["read_only"] is True
    assert not state.exists()


def test_apply_retires_only_registered_completed_workspace(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    root = state / "scratch" / "owned-temp"
    manager = ScratchManager(root, owner="neocortex-framework", create_root=True)
    workspace = manager.create(retain_on_success=True)
    payload_path = workspace.path / "payload"
    payload_path.write_bytes(b"fixture")
    os.chmod(payload_path, 0o600)
    workspace.complete(retain=True)
    assert workspace.path.exists()

    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "owned-temp",
            "--apply",
            "--maintenance-json",
            "--state-directory",
            str(state),
        ],
        capsys,
    )
    assert exit_code == 0
    assert payload["status"] == "applied"
    assert payload["planned"] == 1
    assert payload["applied"] == 1
    assert not workspace.path.exists()


def test_apply_closes_the_canonical_artifact_registry_claim(
    tmp_path: Path,
    capsys,
) -> None:
    """The isolated leaf must compose the same registry as Framework."""

    state = tmp_path / "state"
    root = state / "scratch" / "owned-temp"
    registry_root = state / "artifacts"
    owner = "neocortex-framework"
    producer = ScratchManager(
        root,
        owner=owner,
        create_root=True,
        artifact_registry_root=registry_root,
    )
    workspace = producer.create(retain_on_success=True)
    (workspace.path / "payload").write_bytes(b"fixture")
    workspace.complete(retain=True)
    artifact_id = workspace.artifact_id
    assert artifact_id is not None

    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "owned-temp",
            "--apply",
            "--maintenance-json",
            "--state-directory",
            str(state),
        ],
        capsys,
    )

    registry = ArtifactRegistry(registry_root, owner=owner, create_root=False)
    verified = registry.verify(artifact_id)
    assert exit_code == 0
    assert payload["status"] == "applied"
    assert payload["planned"] == 1
    assert payload["applied"] == 1
    assert not workspace.path.exists()
    assert verified.valid is True
    assert verified.issue is None
    assert verified.state == "retired"
    assert registry.plan(now_ns=10**30).blocked == 0


def test_maintenance_scope_is_required_and_state_derived_is_not_supported() -> None:
    with pytest.raises(SystemExit):
        main(["maintenance"])
    with pytest.raises(SystemExit):
        main(["maintenance", "--scope", "state-derived"])


def test_maintenance_does_not_enter_framework_routes(tmp_path: Path, monkeypatch, capsys) -> None:
    def fail_framework(*_args, **_kwargs):
        raise AssertionError("maintenance must not run Framework routes")

    monkeypatch.setattr("neocortex.api.cli.cli_app.run_framework", fail_framework)
    exit_code, payload = _invoke(
        [
            "maintenance",
            "--scope",
            "audit-work",
            "--maintenance-json",
            "--state-directory",
            str(tmp_path / "state"),
        ],
        capsys,
    )
    assert exit_code == 0
    assert payload["operation"] == "maintenance"
