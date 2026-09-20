"""Focused tests for the authorization-neutral physical mutation backends."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import neocortex.workflow.mutations as mutations
from neocortex.deduplication import FULL_ALGORITHM, full_fingerprint, snapshot_path
from neocortex.safety.kio_trash import KioTrashVerification, metadata_binding


def test_posix_rename_metadata_binding_does_not_hash_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "payload.old"
    target = root / "payload.new"
    source.write_bytes(b"rename without a content-hash frontier")
    snapshot = snapshot_path(source)
    binding = metadata_binding(snapshot)
    effect = SimpleNamespace(
        action="rename",
        source=snapshot,
        source_digest=binding,
        keeper=None,
        keeper_digest=None,
        target_path=str(target),
    )

    def forbidden_hash(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("metadata-only rename must not hash the payload")

    monkeypatch.setattr(mutations, "full_fingerprint", forbidden_hash)
    outcome = mutations.PosixRenameBackend().apply(
        mutations.ApplyCandidate("framework:test", "sha256:" + "0" * 64, root, effect)
    )
    if outcome.reason == "renameat2_unavailable":
        pytest.skip("host does not expose renameat2")

    assert outcome.status == "applied"
    assert not source.exists()
    assert target.read_bytes() == b"rename without a content-hash frontier"
    assert outcome.receipt_json is not None
    assert json.loads(outcome.receipt_json)["source_digest"] == binding


def test_kio_backend_fixture_returns_blocked_without_client(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "blocked.txt"
    source.write_text("blocked", encoding="utf-8")
    snapshot = snapshot_path(source)
    digest = FULL_ALGORITHM + ":" + full_fingerprint(snapshot).hex()

    outcome = mutations.KioTrashBackend(
        which=lambda _name: None,
        environment={"HOME": str(tmp_path / "home")},
        private_config=False,
        private_bus=False,
    ).apply_snapshot(snapshot, root=root, source_digest=digest)

    assert outcome.status == "blocked"
    assert source.exists()


def test_kio_backend_fixture_returns_applied_with_bound_receipt(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "applied.txt"
    source.write_text("applied", encoding="utf-8")
    snapshot = snapshot_path(source)
    digest = FULL_ALGORITHM + ":" + full_fingerprint(snapshot).hex()
    trash = tmp_path / "Trash"
    files = trash / "files"
    info = trash / "info"
    files.mkdir(parents=True)
    info.mkdir()
    client = tmp_path / "kioclient5"
    client.write_text("fixture", encoding="utf-8")
    client.chmod(0o700)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        source_path = Path(command[command.index("move") + 1])
        target = files / source_path.name
        os.rename(source_path, target)
        (info / (target.name + ".trashinfo")).write_text(
            f"[Trash Info]\nPath={source_path}\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def verifier(
        _source: Path, expected, _client: Path
    ) -> KioTrashVerification:
        target = files / Path(expected.path).name
        evidence = {
            "trash_root": str(trash),
            "trash_path": str(target),
            "info_path": str(info / (target.name + ".trashinfo")),
            "volume_id": f"{target.stat().st_dev:x}",
            "file_id": f"{target.stat().st_ino:x}",
            "size": expected.size,
            "digest": digest,
        }
        return KioTrashVerification(True, json.dumps(evidence, sort_keys=True))

    outcome = mutations.KioTrashBackend(
        verifier=verifier,
        runner=runner,
        which=lambda name: str(client) if name == "kioclient5" else None,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
        private_config=False,
        private_bus=False,
    ).apply_snapshot(snapshot, root=root, source_digest=digest)

    assert outcome.status == "applied"
    assert not source.exists()
    assert outcome.receipt_json is not None
    assert json.loads(outcome.receipt_json)["trash"]["digest"] == digest
