from __future__ import annotations

import shutil
import os
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import intake as intake_module
from neocortex.capabilities.formats.archive.intake import (
    FilesystemStageFactory,
    FilesystemPublishHook,
    SourceIdentity,
    TrashDisposition,
    ZipIntakeError,
    intake_zip,
)


class _FixtureTrash:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, source: Path, identity: SourceIdentity) -> TrashDisposition:
        assert identity.matches(source)
        self.root.mkdir(mode=0o700)
        source.rename(self.root / source.name)
        return TrashDisposition("applied", evidence="fixture-trash")


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.txt", b"published")


def test_replay_after_trash_does_not_claim_already_applied(tmp_path: Path) -> None:
    """Replay uses source absence/collision fences, not an ephemeral fast path."""

    source = tmp_path / "source.zip"
    destination = tmp_path / "published"
    trash = _FixtureTrash(tmp_path / "trash")
    _write_zip(source)

    first = intake_zip(source, destination, apply=True, trash=trash)
    assert first.status == "applied"

    replay_source = trash.root / source.name
    second = intake_module.run_zip_intake(
        replay_source,
        apply=True,
        destination=destination,
        trash=None,
    )

    assert second.status == "dependency"
    assert second.reason == "kio_trash_hook_required"
    assert second.reason != "replay_proven"


def test_private_staging_rejects_an_intermediate_symlink(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "staging-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ZipIntakeError):
        FilesystemStageFactory(symlink_parent / "workspace")

    assert not (real_parent / "workspace").exists()


def test_private_staging_is_descriptor_anchored_against_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    displaced = tmp_path / "anchor-displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = anchor / "workspace"
    original_mkdir = intake_module.os.mkdir
    swapped = False

    def racing_mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and name == "workspace":
            anchor.rename(displaced)
            anchor.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(intake_module.os, "mkdir", racing_mkdir)
    with pytest.raises(ZipIntakeError, match="identity_changed"):
        FilesystemStageFactory(requested)

    assert not (outside / "workspace").exists()
    assert not (displaced / "workspace").exists()


def test_extraction_walk_does_not_follow_swapped_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("nested/payload.txt", b"published")
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    displaced = tmp_path / "anchor-displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = anchor / "workspace"
    original_mkdir = intake_module.os.mkdir
    swapped = False

    def racing_mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and name == "workspace":
            anchor.rename(displaced)
            anchor.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(intake_module.os, "mkdir", racing_mkdir)
    with pytest.raises(ZipIntakeError):
        intake_module._extract_zip_tree(
            source,
            destination,
            limits=intake_module.ZipIntakeLimits(),
            deadline=intake_module._Deadline(intake_module.time.monotonic() + 30),
            budget=intake_module._ExtractionBudget(),
            depth=0,
        )

    assert not (outside / "nested" / "payload.txt").exists()
    assert not (displaced / "workspace" / "nested" / "payload.txt").exists()


def test_publish_rollback_abstains_after_destination_replacement(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "payload.txt").write_text("published", encoding="utf-8")
    destination = tmp_path / "published"
    publisher = FilesystemPublishHook()

    receipt = publisher.publish(staged, destination)
    shutil.rmtree(destination)
    destination.mkdir()
    (destination / "changed.txt").write_text("changed", encoding="utf-8")

    assert publisher.rollback(receipt) is False
    assert (destination / "changed.txt").read_text(encoding="utf-8") == "changed"
    assert not staged.exists()


def test_publish_parent_swap_does_not_publish_into_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "payload.txt").write_text("published", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "parent-displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = parent / "published"
    publisher = FilesystemPublishHook()
    original_rename = intake_module._rename_directory_noreplace_fds
    swapped = False

    def racing_rename(source_fd, source_name, destination_fd, destination_name):
        nonlocal swapped
        if not swapped:
            parent.rename(displaced)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(intake_module, "_rename_directory_noreplace_fds", racing_rename)
    with pytest.raises(ZipIntakeError):
        publisher.publish(staged, destination)

    assert staged.exists()
    assert not (outside / "published").exists()
    assert not (displaced / "published").exists()


def test_publish_rejects_real_parent_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "payload.txt").write_text("published", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    destination = parent / "published"
    publisher = FilesystemPublishHook()
    original_open = intake_module._open_directory_path
    swapped = False

    def racing_open(path, *, label, expected_identity=None):
        nonlocal swapped
        if path == parent and label == "destination parent" and not swapped:
            parent.rename(tmp_path / "parent-displaced-before-open")
            parent.mkdir()
            (parent / "sentinel").write_text("replacement", encoding="utf-8")
            swapped = True
        return original_open(path, label=label, expected_identity=expected_identity)

    monkeypatch.setattr(intake_module, "_open_directory_path", racing_open)
    with pytest.raises(ZipIntakeError):
        publisher.publish(staged, destination)

    assert staged.exists()
    assert not (parent / "published").exists()
    assert not (tmp_path / "parent-displaced-before-open" / "published").exists()


def test_repeated_applies_do_not_leak_verification_descriptors(tmp_path: Path) -> None:
    def fd_count() -> int:
        return sum(1 for _entry in os.scandir("/proc/self/fd"))

    baseline = fd_count()
    for index in range(3):
        source = tmp_path / f"source-{index}.zip"
        destination = tmp_path / f"published-{index}"
        trash = _FixtureTrash(tmp_path / f"trash-{index}")
        _write_zip(source)
        result = intake_zip(source, destination, apply=True, trash=trash)
        assert result.status == "applied"
        assert fd_count() <= baseline + 2
