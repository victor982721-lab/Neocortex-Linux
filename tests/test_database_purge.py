"""Safe preview, backup and deletion contracts for NeoCortex state databases."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from neocortex.persistence.database_purge import (
    DATABASE_PURGE_CONFIRMATION,
    DatabasePurgeBusyError,
    DatabasePurgeChangedError,
    DatabasePurgeConfirmationError,
    DatabasePurgeError,
    DatabasePurgeResult,
    execute_database_purge,
    plan_database_purge,
)
from neocortex.integrations.inventory.inventory_boundary import canonical_state_mutation_paths
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


def _create_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE payload(value TEXT NOT NULL)")
        connection.execute("INSERT INTO payload(value) VALUES('preserve')")


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))


def test_state_mutation_boundary_covers_every_registered_database(tmp_path: Path) -> None:
    state = tmp_path / "state"
    paths = {path.name for path in canonical_state_mutation_paths(state)}
    expected = {store.database_name for store in STATE_STORE_REGISTRY.stores}
    assert expected <= paths
    assert {f"{name}-wal" for name in expected} <= paths
    assert {f"{name}-shm" for name in expected} <= paths


def test_preview_is_read_only_and_covers_all_registered_owners(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3")
    _create_database(state / "archive.sqlite3")
    for sidecar in _sidecars(state / "image.sqlite3"):
        sidecar.write_bytes(b"stale-sidecar")
    unknown = state / "legacy.sqlite3"
    unknown.write_bytes(b"legacy")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in state.iterdir()
    }

    plan = plan_database_purge(state)

    assert plan.stores == tuple(store.state_owner_id for store in STATE_STORE_REGISTRY.stores)
    assert {target.owner for target in plan.targets} == {"image", "archive"}
    assert unknown in plan.unknown_sqlite_files
    assert plan.lock_conflicts == ()
    assert {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in state.iterdir()
    } == before


def test_apply_requires_exact_confirmation_and_does_not_touch_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database)

    with pytest.raises(DatabasePurgeConfirmationError, match="DELETE_DATABASES"):
        execute_database_purge(state, stores=("image",), apply=True)

    assert database.is_file()
    assert not (state.parent / "database-backups").exists()


def test_apply_backups_and_removes_selected_database_and_sidecars(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    other = state / "text.sqlite3"
    _create_database(database)
    _create_database(other)
    for sidecar in _sidecars(database):
        sidecar.write_bytes(b"")
    backup = tmp_path / "backups" / "image-purge"

    result = execute_database_purge(
        state,
        stores=("image",),
        backup_directory=backup,
        apply=True,
        confirmation=DATABASE_PURGE_CONFIRMATION,
    )

    assert isinstance(result, DatabasePurgeResult)
    assert len(result.deleted) == 4
    assert not database.exists()
    assert all(not sidecar.exists() for sidecar in _sidecars(database))
    assert other.is_file()
    assert result.manifest == backup / "database-purge-manifest.json"
    assert backup.stat().st_mode & 0o777 == 0o700
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["plan_digest"] == result.plan.plan_digest
    assert manifest["entries"][0]["integrity"]["healthy"] is True
    with sqlite3.connect(backup / "image.sqlite3") as connection:
        assert connection.execute("SELECT value FROM payload").fetchone() == ("preserve",)


def test_apply_rejects_active_framework_lock_without_deleting(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database)
    lock = state / "framework.lock"
    stream = lock.open("a+b", buffering=0)
    try:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DatabasePurgeBusyError):
            execute_database_purge(
                state,
                stores=("image",),
                backup_directory=tmp_path / "backup",
                apply=True,
                confirmation=DATABASE_PURGE_CONFIRMATION,
            )
    finally:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
    assert database.is_file()


def test_apply_leaves_source_when_verified_backup_fails(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    database.write_bytes(b"not sqlite")

    with pytest.raises(DatabasePurgeError, match="verified backup failed"):
        execute_database_purge(
            state,
            stores=("image",),
            backup_directory=tmp_path / "backup",
            apply=True,
            confirmation=DATABASE_PURGE_CONFIRMATION,
        )
    assert database.is_file()


def test_changed_source_after_preview_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database)
    original = plan_database_purge(state, stores=("image",))
    replacement = state / "replacement.sqlite3"
    _create_database(replacement)

    import neocortex.persistence.database_purge as purge

    calls = 0
    real_plan = purge.plan_database_purge

    def changing_plan(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            database.unlink()
            os.replace(replacement, database)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(purge, "plan_database_purge", changing_plan)
    with pytest.raises(DatabasePurgeChangedError):
        execute_database_purge(
            state,
            stores=original.stores,
            backup_directory=tmp_path / "backup",
            apply=True,
            confirmation=DATABASE_PURGE_CONFIRMATION,
        )
    assert database.is_file()
