"""Synthetic Linux empty-directory Trash fixtures; no native KIO is invoked."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.safety import kio_trash
from neocortex.safety.kio_trash import (
    KioTrashService,
    KioTrashStatus,
    metadata_binding,
    restore_trash_receipt,
)
from neocortex.workflow.mutations import KioTrashBackend


@dataclass
class _TrashFixture:
    base: Path

    def __post_init__(self) -> None:
        self.root = self.base / "corpus"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.config = self.base / "config"
        self.config.mkdir()
        self.data = self.base / "data"
        self.trash = self.data / "Trash"
        (self.trash / "files").mkdir(parents=True)
        (self.trash / "info").mkdir()
        self.client = self.base / "kioclient5"
        self.client.write_text("fixture executable", encoding="utf-8")
        self.client.chmod(0o700)
        self.environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_DATA_HOME": str(self.data),
        }
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return str(self.client) if name == "kioclient5" else None

    def runner(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        for raw in command[command.index("move") + 1 : -1]:
            source = Path(raw)
            target = self.trash / "files" / source.name
            os.rename(source, target)
            (self.trash / "info" / f"{source.name}.trashinfo").write_text(
                f"[Trash Info]\nPath={source}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def directory(self, name: str = "empty") -> tuple[Path, FileSnapshot]:
        source = self.root / name
        source.mkdir()
        return source, snapshot_path(source)

    def backend(self) -> KioTrashBackend:
        return KioTrashBackend(
            runner=self.runner,
            which=self.which,
            environment=self.environment,
            home_directory=self.home,
            private_config=False,
            private_bus=False,
        )

    def service(self) -> KioTrashService:
        return KioTrashService(
            which=self.which,
            environment=self.environment,
            home_directory=self.home,
            private_config=False,
            private_bus=False,
        )


@pytest.fixture
def fixture(tmp_path: Path) -> _TrashFixture:
    return _TrashFixture(tmp_path)


def _apply_directory(fixture: _TrashFixture, source: Path, expected: FileSnapshot):
    return fixture.backend().apply_snapshot(
        expected,
        root=fixture.root,
        source_digest=metadata_binding(expected),
        object_kind="empty_directory",
    )


def test_empty_directory_trash_receipt_restore_and_replay(fixture: _TrashFixture) -> None:
    source, expected = fixture.directory()
    outcome = _apply_directory(fixture, source, expected)

    assert outcome.status == "applied"
    assert not source.exists()
    assert outcome.receipt_json is not None
    receipt = json.loads(outcome.receipt_json)
    assert receipt["object_kind"] == "empty_directory"
    trashed = Path(receipt["trash"]["trash_path"])
    assert trashed.is_dir()
    assert list(trashed.iterdir()) == []

    restored = restore_trash_receipt(outcome.receipt_json, root=fixture.root)
    assert restored["status"] == "restored"
    assert source.is_dir()
    assert list(source.iterdir()) == []
    assert not Path(receipt["trash"]["info_path"]).exists()

    replay = restore_trash_receipt(outcome.receipt_json, root=fixture.root)
    assert replay["status"] == "already_restored"


def test_nested_empty_directories_can_be_trashed_postorder(fixture: _TrashFixture) -> None:
    parent = fixture.root / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    child_snapshot = snapshot_path(child)
    child_result = _apply_directory(fixture, child, child_snapshot)
    assert child_result.status == "applied"
    assert parent.is_dir() and list(parent.iterdir()) == []

    parent_snapshot = snapshot_path(parent)
    parent_result = _apply_directory(fixture, parent, parent_snapshot)
    assert parent_result.status == "applied"
    assert not parent.exists()


@pytest.mark.parametrize("case", ["nonempty", "symlink", "missing", "replacement", "mount"])
def test_directory_identity_and_freshness_gates_abstain_without_loss(
    fixture: _TrashFixture,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    source, expected = fixture.directory()
    if case == "nonempty":
        (source / "new.txt").write_text("preserve", encoding="utf-8")
    elif case == "symlink":
        replacement = fixture.root / "real"
        replacement.mkdir()
        source.rmdir()
        source.symlink_to(replacement, target_is_directory=True)
    elif case == "missing":
        source.rmdir()
    elif case == "replacement":
        source.rmdir()
        source.mkdir()
    elif case == "mount":
        monkeypatch.setattr(kio_trash.os.path, "ismount", lambda _path: True)

    outcome = _apply_directory(fixture, source, expected)
    assert outcome.status == "blocked"
    if case in {"nonempty", "mount"}:
        assert source.exists()
    if case == "nonempty":
        assert (source / "new.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(fixture.trash.glob("files/*"))


def test_new_content_racing_after_private_claim_is_recovery_required(
    fixture: _TrashFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, expected = fixture.directory()

    def racing_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        claimed = Path(command[command.index("move") + 1])
        (claimed / "racing.txt").write_text("preserve", encoding="utf-8")
        return fixture.runner(command, **_kwargs)

    monkeypatch.setattr(kio_trash.subprocess, "run", racing_runner)
    result = fixture.service().move(
        expected,
        source_digest=metadata_binding(expected),
        root=fixture.root,
        object_kind="empty_directory",
    )
    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert not source.exists()
    assert any(
        path.read_text(encoding="utf-8") == "preserve"
        for path in fixture.trash.glob("files/*/racing.txt")
    )


@pytest.mark.parametrize("interruption", ["stderr", "timeout", "cancel"])
def test_directory_timeout_and_cancel_are_uncertain_with_claim_recovery(
    fixture: _TrashFixture,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    source, expected = fixture.directory()

    def interrupted_runner(command: list[str], **kwargs: object):
        if interruption == "stderr":
            return subprocess.CompletedProcess(command, 7, "", "synthetic stderr")
        if interruption == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        raise KeyboardInterrupt("synthetic cancellation")

    monkeypatch.setattr(kio_trash.subprocess, "run", interrupted_runner)
    result = fixture.service().move(
        expected,
        source_digest=metadata_binding(expected),
        root=fixture.root,
        object_kind="empty_directory",
    )
    assert result.status is KioTrashStatus.RECOVERY_REQUIRED
    assert not source.exists()
    assert result.detail is not None
    assert ".neocortex-kio-claim-" in result.detail


def test_restore_refuses_destination_collision_and_preserves_trash(fixture: _TrashFixture) -> None:
    source, expected = fixture.directory()
    outcome = _apply_directory(fixture, source, expected)
    assert outcome.receipt_json is not None
    source.mkdir()
    (source / "unrelated").write_text("preserve", encoding="utf-8")

    with pytest.raises(kio_trash.KioTrashUnavailable, match="collision"):
        restore_trash_receipt(outcome.receipt_json, root=fixture.root)
    assert (source / "unrelated").read_text(encoding="utf-8") == "preserve"
    receipt = json.loads(outcome.receipt_json)
    assert Path(receipt["trash"]["trash_path"]).is_dir()
