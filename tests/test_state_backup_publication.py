"""Cross-owner backup, restore, epoch and sidecar-safe purge contracts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import neocortex.persistence.database_purge as purge
from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.persistence.database_purge import (
    DATABASE_RESTORE_CONFIRMATION,
    DatabasePurgeError,
    DatabaseRestoreConfirmationError,
    DatabaseRestoreError,
    backup_state_owners,
    execute_database_purge,
    restore_state_owners,
)
from neocortex.persistence.sqlite_integrity import (
    SQLiteIntegrityPolicy,
    check_sqlite_integrity,
)
from neocortex.persistence.state_publication import (
    StatePublicationConflictError,
    publication_idempotency_key,
    read_state_epoch,
    read_state_publications,
    record_state_publication,
)


def _create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_image_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO metadata VALUES('fixture_payload', ?)", (value,)
        )


def test_full_integrity_distinguishes_quick_and_exhaustive_checks(tmp_path: Path) -> None:
    database = tmp_path / "owner.sqlite3"
    _create_database(database, "ok")

    quick = check_sqlite_integrity(database)
    full = check_sqlite_integrity(
        database,
        policy=SQLiteIntegrityPolicy(check_mode="full"),
    )

    assert quick.check_mode == "quick"
    assert quick.integrity_check_complete is True
    assert full.check_mode == "full"
    assert full.integrity_check_errors == ()
    assert full.integrity_check_complete is True
    assert full.healthy is True


def test_publication_epoch_is_read_only_until_a_complete_event(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    before = tuple(path.name for path in state.iterdir())

    assert read_state_epoch(state).epoch == 0
    assert tuple(path.name for path in state.iterdir()) == before
    key = publication_idempotency_key("cache", "item-1")
    partial = record_state_publication(
        state,
        operation="cache-sync",
        owners=("text",),
        status="partial",
        idempotency_key=key,
    )
    assert partial.epoch == 0
    assert read_state_epoch(state).epoch == 0
    complete = record_state_publication(
        state,
        operation="cache-sync",
        owners=("text",),
        status="complete",
        idempotency_key=key,
    )
    assert complete.epoch == 1
    assert read_state_epoch(state).epoch == 1
    replay = record_state_publication(
        state,
        operation="cache-sync",
        owners=("text",),
        status="complete",
        idempotency_key=key,
        expected_epoch=1,
    )
    assert replay == complete
    assert len(read_state_publications(state)) == 2
    with pytest.raises(StatePublicationConflictError):
        record_state_publication(
            state,
            operation="other",
            owners=("text",),
            status="complete",
            idempotency_key="different",
            expected_epoch=0,
        )
    with pytest.raises(StatePublicationConflictError, match="different manifest"):
        record_state_publication(
            state,
            operation="cache-sync",
            owners=("text",),
            status="complete",
            idempotency_key=key,
            manifest_sha256="0" * 64,
            expected_epoch=1,
        )


def test_backup_state_owners_writes_complete_manifest_with_absent_owners(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3", "image")

    result = backup_state_owners(
        state,
        tmp_path / "backup",
        stores=("image", "semantic"),
    )

    assert result.complete is True
    assert result.state_epoch.epoch == 0
    assert result.manifest.is_file()
    payload = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert payload["schema"] == "neocortex.state-backup/v1"
    assert payload["integrity_mode"] == "full"
    assert {entry["owner"] for entry in payload["entries"]} == {"image", "semantic"}
    assert next(entry for entry in payload["entries"] if entry["owner"] == "semantic")["status"] == "absent"
    assert not (state / "state-epoch.json").exists()
    assert not (state / "state-publication-journal.jsonl").exists()


def test_backup_manifest_records_source_permissions_and_restore_preserves_them(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database, "image")
    database.chmod(0o640)

    backup = backup_state_owners(state, tmp_path / "backup", stores=("image",))
    payload = json.loads(backup.manifest.read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    source_file = next(item for item in entry["source_files"] if item["role"] == "database")
    assert source_file["mode"] == 0o640

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE metadata SET value='changed' WHERE key='fixture_payload'")
    restored = restore_state_owners(
        state,
        backup.backup_directory,
        stores=("image",),
        apply=True,
        confirmation=DATABASE_RESTORE_CONFIRMATION,
    )

    assert restored.complete is True
    assert database.stat().st_mode & 0o7777 == 0o640


def test_backup_rejects_orphan_sidecar_as_incomplete_source(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    orphan = state / "image.sqlite3-wal"
    orphan.write_bytes(b"orphan")

    result = backup_state_owners(state, tmp_path / "backup", stores=("image",))

    assert result.complete is False
    assert result.entries[0].status == "orphan_sidecar_only"
    with pytest.raises(DatabaseRestoreError, match="incomplete state backup"):
        restore_state_owners(state, result.backup_directory, stores=("image",))


@pytest.mark.parametrize("mutation", ["future_policy", "authority_change"])
def test_restore_rejects_incompatible_lifecycle_declaration_before_effect(tmp_path: Path, mutation: str):
    state = tmp_path / "state"
    database = state / "image.sqlite3"
    _create_database(database, "preserved")
    backup = backup_state_owners(state, tmp_path / "backup", stores=("image",))
    before = database.read_bytes()
    payload = json.loads(backup.manifest.read_text())
    entry = payload["entries"][0]
    assert entry["lifecycle_policy_version"] == 1
    assert isinstance(entry["authority_tables"], list)
    if mutation == "future_policy":
        entry["lifecycle_policy_version"] = 2
    else:
        entry["authority_tables"].append("forged_authority")
    backup.manifest.write_text(json.dumps(payload))
    with pytest.raises(DatabaseRestoreError, match=r"lifecycle|authority"):
        restore_state_owners(state, backup.backup_directory, stores=("image",))
    assert database.read_bytes() == before


def test_restore_validates_then_publishes_selected_owners(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _create_database(state / "image.sqlite3", "before")
    backup = backup_state_owners(state, tmp_path / "backup", stores=("image",))
    with closing(sqlite3.connect(state / "image.sqlite3")) as connection, connection:
        connection.execute("UPDATE metadata SET value='changed' WHERE key='fixture_payload'")

    preview = restore_state_owners(state, backup.backup_directory, stores=("image",))
    assert preview.complete is True
    assert preview.restored == ()
    with pytest.raises(DatabaseRestoreConfirmationError):
        restore_state_owners(
            state,
            backup.backup_directory,
            stores=("image",),
            apply=True,
        )

    result = restore_state_owners(
        state,
        backup.backup_directory,
        stores=("image",),
        apply=True,
        confirmation=DATABASE_RESTORE_CONFIRMATION,
    )

    assert result.complete is True
    assert result.restored == ("image",)
    assert result.state_epoch.epoch == 1
    with closing(sqlite3.connect(state / "image.sqlite3")) as connection, connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='fixture_payload'").fetchone() == ("before",)
    assert result.pre_restore_backup is not None
    assert result.pre_restore_backup.complete is True
    assert not tuple(state.parent.glob(".neocortex-state-restore-*"))


def test_restore_reverts_owner_files_when_epoch_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database, "before")
    backup = backup_state_owners(state, tmp_path / "backup", stores=("image",))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE metadata SET value='changed' WHERE key='fixture_payload'")

    original_record = purge.record_state_publication

    def fail_complete(*args: object, **kwargs: object):
        if kwargs.get("status") == "complete":
            raise purge.StatePublicationError("injected epoch failure")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(purge, "record_state_publication", fail_complete)
    with pytest.raises(DatabaseRestoreError, match="publication journal"):
        restore_state_owners(
            state,
            backup.backup_directory,
            stores=("image",),
            apply=True,
            confirmation=DATABASE_RESTORE_CONFIRMATION,
        )

    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='fixture_payload'").fetchone() == ("changed",)
    assert read_state_epoch(state).epoch == 0


def test_purge_recaptures_empty_sidecars_created_during_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    _create_database(database, "purge")
    real_backup = purge.backup_sqlite_online

    def backup_and_materialize_empty_sidecars(*args: object, **kwargs: object):
        result = real_backup(*args, **kwargs)
        source = Path(args[0])
        Path(f"{source}-wal").write_bytes(b"")
        Path(f"{source}-shm").write_bytes(b"\x00" * 32_768)
        return result

    monkeypatch.setattr(purge, "backup_sqlite_online", backup_and_materialize_empty_sidecars)
    result = execute_database_purge(
        state,
        stores=("image",),
        backup_directory=tmp_path / "backup",
        apply=True,
        confirmation="DELETE_DATABASES",
    )

    assert result.deleted
    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["plan_digest"] == result.plan.plan_digest


def test_purge_refuses_orphan_sidecars_without_a_recoverable_database(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    orphan = state / "image.sqlite3-wal"
    orphan.write_bytes(b"orphan")

    with pytest.raises(DatabasePurgeError, match="orphan sidecars"):
        execute_database_purge(
            state,
            stores=("image",),
            apply=True,
            confirmation="DELETE_DATABASES",
        )
    assert orphan.is_file()
