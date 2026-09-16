"""Focused regressions for state-owner and physical-effect boundaries."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from neocortex.curation import recovery
from neocortex.deduplication import FULL_ALGORITHM, full_fingerprint, snapshot_path
from neocortex.persistence import sqlite_backup, state_publication
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.runtime.control.locking import FrameworkRunLock


def test_writable_framework_connection_rejects_symlink_owner(tmp_path: Path) -> None:
    owner = tmp_path / "owner.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    with sqlite3.connect(owner) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    alias.symlink_to(owner)

    with pytest.raises(sqlite3.OperationalError, match=r"regular file|symlink"):
        connect_existing_framework(alias, readonly=False)


def test_framework_lock_is_private_and_rejects_symlink(tmp_path: Path) -> None:
    lock = tmp_path / "framework.lock"
    previous = os.umask(0o022)
    try:
        with FrameworkRunLock(lock):
            pass
    finally:
        os.umask(previous)
    assert lock.stat().st_mode & 0o777 == 0o600

    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    lock.unlink()
    lock.symlink_to(victim)
    with pytest.raises(RuntimeError, match=r"symlink|directory"):
        with FrameworkRunLock(lock):
            pass


def test_publication_lock_rejects_symlink_endpoint(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    (state / state_publication.STATE_PUBLICATION_LOCK_FILENAME).symlink_to(victim)

    with pytest.raises(state_publication.StatePublicationError, match="symlink"):
        with state_publication._publication_lock(state):
            pass


def test_sqlite_backup_fsyncs_destination_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE marker(value INTEGER)")
        connection.execute("INSERT INTO marker VALUES (1)")
    calls: list[int] = []
    real_fsync = sqlite_backup.os.fsync

    def record_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(sqlite_backup.os, "fsync", record_fsync)
    sqlite_backup.backup_sqlite_online(source, destination)
    assert destination.is_file()
    assert calls


def test_sqlite_backup_rejects_parent_replacement_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    safe = tmp_path / "safe"
    evil = tmp_path / "evil"
    safe.mkdir()
    evil.mkdir()
    staging = safe / "staging.sqlite3"
    staging.write_bytes(b"staging")
    destination = safe / "destination.sqlite3"
    parent_fd = sqlite_backup._open_real_directory(safe)
    fence = sqlite_backup._capture_sqlite_owner_fence(staging, label="test staging")
    assert fence is not None
    original_link = cast(Any, sqlite_backup.os.link)

    def replace_parent(*args: object, **kwargs: object) -> None:
        safe.rename(tmp_path / "safe-old")
        safe.symlink_to(evil, target_is_directory=True)
        original_link(*args, **kwargs)

    monkeypatch.setattr(sqlite_backup.os, "link", replace_parent)
    try:
        with pytest.raises(sqlite_backup.SQLiteBackupPublicationError, match="parent changed"):
            sqlite_backup._publish_no_replace(
                staging,
                destination,
                parent_fd=parent_fd,
                expected_fence=fence,
            )
    finally:
        os.close(parent_fd)
    assert not (evil / destination.name).exists()


def test_restore_rejects_trash_directory_replacement_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    trash = tmp_path / "trash"
    files = trash / "files"
    info = trash / "info"
    files.mkdir(parents=True)
    info.mkdir()
    trash_item = files / "item"
    trash_item.write_bytes(b"ORIGINAL")
    info_item = info / "item.trashinfo"
    info_item.write_text("[Trash Info]\nPath=/intended/restored.txt\n", encoding="utf-8")
    root_snapshot = snapshot_path(corpus)
    trash_root_snapshot = snapshot_path(trash)
    item_snapshot = snapshot_path(trash_item)
    source = corpus / "restored.txt"
    source_snapshot = recovery.FileSnapshot(
        str(source),
        item_snapshot.volume_id,
        item_snapshot.file_id,
        item_snapshot.size,
        item_snapshot.mtime_ns,
        item_snapshot.birthtime_ns,
    )
    digest = FULL_ALGORITHM + ":" + full_fingerprint(item_snapshot).hex()
    effect = SimpleNamespace(action="trash", source=source_snapshot, source_digest=digest)
    candidate = cast(
        recovery.RestoreCandidate,
        SimpleNamespace(
            action_id=1,
            original_action_id=1,
            effect=effect,
            root=corpus,
            trash_path=trash_item,
            info_path=info_item,
            trash_root=trash,
            trash_root_snapshot=trash_root_snapshot,
            trash_volume_id=item_snapshot.volume_id,
            trash_file_id=item_snapshot.file_id,
            grant=SimpleNamespace(root_snapshot=root_snapshot),
        ),
    )
    monkeypatch.setattr(recovery, "_verify_trash_candidate", lambda _candidate: item_snapshot)
    real_rename = recovery._rename_noreplace

    def replace_files_root(
        source_path: Path,
        destination_path: Path,
        *,
        source_root: Path,
        destination_root: Path,
        expected_source=None,
    ) -> None:
        source_root.rename(tmp_path / "files-old")
        source_root.mkdir()
        (source_root / source_path.name).write_bytes(b"MALICIOUS")
        real_rename(
            source_path,
            destination_path,
            source_root=source_root,
            destination_root=destination_root,
            expected_source=expected_source,
        )

    monkeypatch.setattr(recovery, "_rename_noreplace", replace_files_root)
    outcome = recovery.PosixRestoreBackend(trash).restore(candidate)

    assert outcome.status == "blocked"
    assert outcome.reason == "trash_content_changed"
    assert not source.exists()
    assert trash_item.exists()
