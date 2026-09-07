from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence import sqlite_backup as backup_module
from neocortex.persistence import sqlite_integrity as integrity_module
from neocortex.persistence.sqlite_backup import backup_sqlite_online
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
from neocortex.persistence.sqlite_integrity import check_sqlite_integrity


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE probe(value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES(7)")


def _assert_no_staging(directory: Path) -> None:
    assert tuple(directory.glob(".neocortex-sqlite-backup-*")) == ()


def test_integrity_rejects_symlink_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    _database(target)
    alias.symlink_to(target)
    target_bytes = target.read_bytes()

    def unexpected_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SQLite must not open a symlinked owner")

    monkeypatch.setattr(integrity_module, "connect_sqlite", unexpected_open)
    with pytest.raises(ImmutableSQLiteUnavailable, match="symlink"):
        check_sqlite_integrity(alias)

    assert alias.is_symlink()
    assert target.read_bytes() == target_bytes


@pytest.mark.parametrize("kind", ("directory", "fifo"))
def test_integrity_rejects_non_regular_owner_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    owner = tmp_path / f"owner-{kind}.sqlite3"
    if kind == "directory":
        owner.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixtures require POSIX")
        os.mkfifo(owner)

    def unexpected_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SQLite must not open a non-regular owner")

    monkeypatch.setattr(integrity_module, "connect_sqlite", unexpected_open)
    with pytest.raises(ImmutableSQLiteUnavailable, match="regular"):
        check_sqlite_integrity(owner)


def test_backup_rejects_symlink_source_without_staging_or_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    _database(target)
    alias.symlink_to(target)

    def unexpected_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SQLite must not open a symlinked source")

    monkeypatch.setattr(backup_module, "connect_sqlite", unexpected_open)
    with pytest.raises(ImmutableSQLiteUnavailable, match="symlink"):
        backup_sqlite_online(alias, destination)

    assert not destination.exists()
    _assert_no_staging(tmp_path)


def test_integrity_rejects_symlinked_source_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    linked_parent = tmp_path / "linked-parent"
    real_parent.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    source = linked_parent / "source.sqlite3"
    _database(real_parent / "source.sqlite3")

    with pytest.raises(ImmutableSQLiteUnavailable, match="symlink"):
        check_sqlite_integrity(source)


@pytest.mark.parametrize("kind", ("directory", "fifo"))
def test_backup_rejects_non_regular_source_without_staging(
    tmp_path: Path,
    kind: str,
) -> None:
    source = tmp_path / f"source-{kind}.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    if kind == "directory":
        source.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixtures require POSIX")
        os.mkfifo(source)

    with pytest.raises(ImmutableSQLiteUnavailable, match="regular"):
        backup_sqlite_online(source, destination)

    assert not destination.exists()
    _assert_no_staging(tmp_path)


def test_backup_rejects_symlinked_destination_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    real_destination_parent = tmp_path / "real-destination"
    linked_destination_parent = tmp_path / "linked-destination"
    real_destination_parent.mkdir()
    linked_destination_parent.symlink_to(real_destination_parent, target_is_directory=True)
    destination = linked_destination_parent / "backup.sqlite3"
    _database(source)

    with pytest.raises(backup_module.SQLiteBackupPublicationError, match="symlink"):
        backup_sqlite_online(source, destination)

    assert not (real_destination_parent / "backup.sqlite3").exists()
    _assert_no_staging(tmp_path)


def test_backup_fence_rejects_source_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(source)
    _database(replacement)

    def replace_source(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        os.replace(replacement, source)
        return (0, 1, 4096)

    monkeypatch.setattr(backup_module, "_copy_online", replace_source)
    with pytest.raises(ImmutableSQLiteUnavailable, match="changed"):
        backup_sqlite_online(source, destination)

    assert not destination.exists()
    _assert_no_staging(tmp_path)


def test_integrity_rejects_post_open_replacement_and_preserves_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _database(source)
    _database(replacement)
    real_connect = integrity_module.connect_sqlite

    def connect_and_replace(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        os.replace(replacement, source)
        return connection

    monkeypatch.setattr(integrity_module, "connect_sqlite", connect_and_replace)
    with pytest.raises(ImmutableSQLiteUnavailable, match="changed"):
        check_sqlite_integrity(source)

    assert source.is_file()
