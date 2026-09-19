"""Factory reset deletes operational files, never snapshots or backs up SQLite."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence import factory_reset as reset


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DOCUMENTS_DIR", "documents"),
    ):
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("NEOCORTEX_CORPUS_ROOT", raising=False)
    state = tmp_path / "xdg-state/Neocortex/state"
    state.mkdir(parents=True)
    return state


def _operational(state: Path) -> list[Path]:
    return [
        p
        for p in state.iterdir()
        if p.name not in reset._FIXED_LOCK_NAMES | {"installation-receipts"}
        and not p.name.endswith(".route.lock")
        and not (
            p.name == "historical-adoptions" and [x.name for x in p.iterdir()] == ["owner.lock"]
        )
    ]


def test_default_large_wal_zip_complete_without_sql_or_backup(lab, monkeypatch):
    db = lab / "code.sqlite3"
    with db.open("wb") as stream:
        stream.truncate(14 * 1024**3)
    (lab / "code.sqlite3-wal").write_bytes(b"not-empty-wal")
    (lab / "code.sqlite3-shm").write_bytes(bytes(32768))
    for relative in (
        "text.sqlite3",
        "archive-materialized/fixture/report.txt",
        "archive-manifests/evidence.json",
        "artifacts/claim.json",
        "scratch/owned-temp/payload/file.txt",
        "curation/checkpoints/old.json",
        "historical-adoptions/old.json",
        "runtime-cache/old.bin",
        "state-reset-operations/old.json",
        "old.sqlite3.pre-migration.sqlite3",
    ):
        file = lab / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("disposable operational fixture")
    receipt = lab / "installation-receipts/install.json"
    receipt.parent.mkdir()
    receipt.write_text("installation metadata")
    originals = lab.parents[2] / "documents/NeoCortex/Corpus"
    originals.mkdir(parents=True)
    (originals / "source.zip").write_bytes(b"original")
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("must not open SQLite"))
    result = reset.factory_reset()
    assert result["status"] == "complete"
    assert result["backup_created"] is False
    assert result["deleted_bytes"] >= 14 * 1024**3
    assert not _operational(lab)
    assert receipt.read_text() == "installation metadata"
    assert (originals / "source.zip").read_bytes() == b"original"
    assert not any("backup" in p.name for p in lab.parent.iterdir())
    lock_ids = {p.name: p.stat().st_ino for p in lab.iterdir() if p.is_file()}
    assert reset.factory_reset()["deleted_count"] == 0
    assert lock_ids == {p.name: p.stat().st_ino for p in lab.iterdir() if p.is_file()}


def test_empty_missing_and_orphaned_owners_are_reset(lab):
    (lab / "empty.sqlite3").touch()
    (lab / "orphan.sqlite3-wal").write_text("interrupted WAL")
    assert reset.factory_reset(lab)["status"] == "complete"
    assert not _operational(lab)
    absent = lab.parent / "elsewhere/state"
    assert reset.factory_reset(absent)["status"] == "complete"
    assert not absent.exists()


def test_symlink_alias_is_removed_target_is_untouched(lab):
    outside = lab.parent / "original.txt"
    outside.write_text("original")
    (lab / "alias").symlink_to(outside)
    result = reset.factory_reset(lab)
    assert result["status"] == "complete"
    assert outside.read_text() == "original"
    assert not (lab / "alias").exists()


@pytest.mark.parametrize("relative", ["framework.lock", "artifacts", "scratch/owned-temp"])
def test_busy_file_or_directory_lock_abstains_before_deletion(lab, relative):
    payload = lab / "payload"
    payload.write_text("retain")
    lock = lab / relative
    if lock.suffix == ".lock":
        lock.touch()
        fd = os.open(lock, os.O_RDWR)
    else:
        lock.mkdir(parents=True)
        fd = os.open(lock, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(reset.FactoryResetError):
            reset.factory_reset(lab)
        assert payload.read_text() == "retain"
    finally:
        os.close(fd)


def test_open_hardlink_alias_is_detected(lab):
    db = lab / "owner.sqlite3"
    db.write_bytes(b"fixture")
    alias = lab.parent / "outside-alias"
    os.link(db, alias)
    with alias.open("rb"):
        with pytest.raises(reset.FactoryResetError, match="active"):
            reset.factory_reset(lab)
    assert db.read_bytes() == b"fixture"


def test_mount_boundary_abstains_before_any_deletion(lab, monkeypatch):
    (lab / "payload").write_text("retain")
    nested = lab / "mounted"
    nested.mkdir()
    original = reset._mount_id_for_directory
    monkeypatch.setattr(
        reset, "_mount_id_for_directory", lambda p: original(p) + (1 if p == nested else 0)
    )
    with pytest.raises(reset.FactoryResetError, match="mount"):
        reset.factory_reset(lab)
    assert (lab / "payload").read_text() == "retain"


def test_protected_roots_and_root_symlink_are_rejected(lab, monkeypatch):
    with pytest.raises(reset.FactoryResetError):
        reset.factory_reset(Path.home())
    alias = lab.parent / "alias"
    alias.symlink_to(lab, target_is_directory=True)
    with pytest.raises(reset.FactoryResetError):
        reset.factory_reset(alias)
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(lab / "originals"))
    (lab / "originals").mkdir()
    with pytest.raises(reset.FactoryResetError, match="protected"):
        reset.factory_reset(lab)


def test_partial_unlink_failure_is_not_success(lab, monkeypatch):
    (lab / "a").write_text("a")
    (lab / "z").write_text("z")
    original = reset._delete_entry

    def fail(root_fd, device, entry):
        if entry.relative == "a":
            raise reset.FactoryResetError("injected failure")
        return original(root_fd, device, entry)

    monkeypatch.setattr(reset, "_delete_entry", fail)
    with pytest.raises(reset.FactoryResetError) as caught:
        reset.factory_reset(lab)
    assert caught.value.partial_result["status"] == "partial"
    assert caught.value.partial_result["deleted_count"] > 0
    assert (lab / "a").exists()


def test_absent_route_lock_is_reserved_and_inode_survives(lab, monkeypatch):
    (lab / "archive.sqlite3").write_bytes(b"fixture")
    lock = lab / "archive.sqlite3.route.lock"
    assert not lock.exists()
    original = reset._delete_entry
    observed = []

    def attempt_concurrent_writer(root_fd, device, entry):
        if entry.path.name == "archive.sqlite3":
            fd = os.open(lock, os.O_RDWR)
            try:
                observed.append(os.fstat(fd).st_ino)
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        return original(root_fd, device, entry)

    monkeypatch.setattr(reset, "_delete_entry", attempt_concurrent_writer)
    assert reset.factory_reset(lab)["status"] == "complete"
    assert observed == [lock.stat().st_ino]


def test_historical_owner_lock_survives_but_authority_data_does_not(lab):
    directory = lab / "historical-adoptions"
    directory.mkdir()
    (directory / "authority.key").write_text("operational authority fixture")
    reset.factory_reset(lab)
    assert [p.name for p in directory.iterdir()] == ["owner.lock"]
    before = (directory / "owner.lock").stat().st_ino
    assert reset.factory_reset(lab)["deleted_count"] == 0
    assert (directory / "owner.lock").stat().st_ino == before


def test_mount_change_at_effect_is_rejected(lab, monkeypatch):
    folder = lab / "nested"
    folder.mkdir()
    (folder / "file").write_text("retain")
    real_mount = reset._mount_id_for_fd
    real_delete = reset._delete_entry

    def changed_mount(descriptor):
        path = os.readlink(f"/proc/self/fd/{descriptor}")
        return real_mount(descriptor) + (1 if path == str(folder) else 0)

    def effect(root_fd, device, entry):
        monkeypatch.setattr(reset, "_mount_id_for_fd", changed_mount)
        return real_delete(root_fd, device, entry)

    monkeypatch.setattr(reset, "_delete_entry", effect)
    with pytest.raises(reset.FactoryResetError):
        reset.factory_reset(lab)
    assert (folder / "file").read_text() == "retain"


@pytest.mark.parametrize("writer", ["activity", "historical"])
def test_new_owner_roots_cannot_be_created_during_reset(lab, monkeypatch, writer):
    from types import SimpleNamespace
    from neocortex.api.agent_activity import AgentActivity
    from neocortex.runtime.historical_adoption import HistoricalAdoption

    lab.chmod(0o700)
    (lab / "payload").write_text("old state")
    original = reset._delete_entry
    checked = []

    def effect(root_fd, device, entry):
        if entry.path.name == "payload":
            if writer == "activity":
                with pytest.raises(RuntimeError, match="factory-reset"):
                    AgentActivity.prepare(lab, "concurrent-activity")
                assert not (lab / "artifacts").exists()
            else:
                with pytest.raises(RuntimeError, match="factory_reset"):
                    with HistoricalAdoption(
                        SimpleNamespace(
                            state_directory=lab,
                            root=lab.parent / "historical",
                            owner="neocortex-framework",
                        )
                    )._locked(create=True):
                        pytest.fail("writer entered during reset")
                assert not (lab / "historical-adoptions").exists()
            checked.append(True)
        return original(root_fd, device, entry)

    monkeypatch.setattr(reset, "_delete_entry", effect)
    assert reset.factory_reset(lab)["status"] == "complete"
    assert checked == [True]


def test_canonical_route_lock_does_not_hide_sqlite_effect_guard(lab, monkeypatch):
    from contextlib import contextmanager

    database = lab / "code.sqlite3"
    database.write_bytes(b"fixture database")
    (lab / "code.sqlite3-wal").write_bytes(b"nonempty WAL fixture")
    (lab / "code.sqlite3-shm").write_bytes(bytes(32768))
    # A route lock and a migration backup are not SQLite sidecar suffixes.
    (lab / "code.sqlite3.route.lock").touch()
    (lab / "code.sqlite3.pre-migration.sqlite3").write_bytes(b"old fixture")
    original = reset.sqlite_owner_effect_guard
    observed = []

    @contextmanager
    def guarded(path):
        with original(path) as fence:
            observed.append(path)
            yield fence

    monkeypatch.setattr(reset, "sqlite_owner_effect_guard", guarded)
    result = reset.factory_reset(lab)
    assert result["status"] == "complete"
    assert database in observed
    assert not _operational(lab)
