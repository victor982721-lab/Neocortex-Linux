"""SQLite restore failure boundaries, historical backups and owner admission."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import neocortex.persistence.database_purge as restore
import neocortex.persistence.state_publication as publication
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


def _database(path: Path, value: str, owner: str = "image", version: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if owner == "framework":
        path.touch()
    restore._migrate_restore_staged(path, owner)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO metadata VALUES('fixture_payload', ?)", (value,))
        if version is not None:
            connection.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'", (str(version),)
            )


def _value(path: Path) -> str:
    # Fixtures only; do not use this helper on a production owner under fence.
    with closing(sqlite3.connect(path)) as connection, connection:
        return str(connection.execute(
            "SELECT value FROM metadata WHERE key='fixture_payload'"
        ).fetchone()[0])


def _change(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='fixture_payload'", (value,)
        )


def _fixture(tmp_path: Path) -> tuple[Path, restore.DatabaseBackupResult]:
    state = tmp_path / "state"
    _database(state / "image.sqlite3", "backup")
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=("image",))
    _change(state / "image.sqlite3", "live")
    return state, backup


def _apply(state: Path, backup: restore.DatabaseBackupResult, **kwargs: object):
    return restore.restore_state_owners(
        state,
        backup.backup_directory,
        stores=("image",),
        apply=True,
        confirmation=restore.DATABASE_RESTORE_CONFIRMATION,
        **kwargs,
    )


def _journal_fd(descriptor: int) -> bool:
    return os.readlink(f"/proc/self/fd/{descriptor}").endswith(
        publication.STATE_PUBLICATION_JOURNAL_FILENAME
    )


@pytest.mark.parametrize("boundary", ["permissions", "directory"])
def test_preappend_failure_reverts_and_verifiably_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    state, backup = _fixture(tmp_path)
    tripped = False
    real_chmod = publication.os.fchmod
    real_directory = publication._fsync_directory

    def has_prepare() -> bool:
        return len(publication.read_state_publications(state)) == 1

    def fail_permissions(descriptor: int, mode: int) -> None:
        nonlocal tripped
        if not tripped and _journal_fd(descriptor) and has_prepare():
            tripped = True
            raise OSError("injected preappend permissions failure")
        real_chmod(descriptor, mode)

    def fail_directory(path: Path) -> None:
        nonlocal tripped
        if not tripped and has_prepare():
            tripped = True
            raise publication.StatePublicationError("injected preappend directory failure")
        real_directory(path)

    if boundary == "permissions":
        monkeypatch.setattr(publication.os, "fchmod", fail_permissions)
    else:
        monkeypatch.setattr(publication, "_fsync_directory", fail_directory)
    with pytest.raises(restore.DatabaseRestoreError, match="rollback verified"):
        _apply(state, backup)

    assert tripped
    assert _value(state / "image.sqlite3") == "live"
    events = publication.read_state_publications(state)
    assert [event.status for event in events] == ["partial", "failed"]
    assert events[0].owner_heads == events[1].owner_heads
    assert publication.read_state_publication_state(state).status == "absent"
    assert publication.read_state_epoch(state).epoch == 0
    assert not tuple(tmp_path.glob(".neocortex-state-restore-*"))


@pytest.mark.parametrize("boundary", ["write", "permissions", "directory_fsync"])
def test_pointer_failure_preserves_durable_complete_owner_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    state, backup = _fixture(tmp_path)
    real_write = publication._atomic_write_json
    real_chmod = publication.os.chmod
    real_fsync = publication.os.fsync

    def fail_write(path: Path, payload) -> None:
        if path.name == publication.STATE_EPOCH_FILENAME:
            raise OSError("injected pointer write failure")
        real_write(path, payload)

    def fail_permissions(path, mode, **kwargs) -> None:
        if Path(path).name.startswith(f".{publication.STATE_EPOCH_FILENAME}."):
            raise OSError("injected pointer chmod failure")
        real_chmod(path, mode, **kwargs)

    def fail_fsync(descriptor: int) -> None:
        if (
            os.readlink(f"/proc/self/fd/{descriptor}") == str(state)
            and (state / publication.STATE_EPOCH_FILENAME).exists()
        ):
            raise OSError("injected pointer directory fsync failure")
        real_fsync(descriptor)

    if boundary == "write":
        monkeypatch.setattr(publication, "_atomic_write_json", fail_write)
    elif boundary == "permissions":
        monkeypatch.setattr(publication.os, "chmod", fail_permissions)
    else:
        monkeypatch.setattr(publication.os, "fsync", fail_fsync)
    result = _apply(state, backup)

    assert result.complete
    assert result.publication_warning is not None
    assert result.state_epoch.epoch == 1
    assert _value(state / "image.sqlite3") == "backup"
    assert publication.read_state_publication_state(state).status == "complete"
    assert [event.status for event in publication.read_state_publications(state)] == [
        "partial", "complete"
    ]
    assert not tuple(tmp_path.glob(".neocortex-state-restore-*"))


@pytest.mark.parametrize("fsync_succeeded", [False, True])
def test_uncertain_complete_append_never_reverts_and_retains_recovery_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fsync_succeeded: bool
) -> None:
    state, backup = _fixture(tmp_path)
    real_fsync = publication.os.fsync

    def fail_complete_fsync(descriptor: int) -> None:
        if _journal_fd(descriptor) and any(
            event.status == "complete"
            for event in publication.read_state_publications(state)
        ):
            if fsync_succeeded:
                real_fsync(descriptor)
            raise OSError("injected ambiguous complete fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(publication.os, "fsync", fail_complete_fsync)
    with pytest.raises(restore.DatabaseRestoreRecoveryRequiredError) as caught:
        _apply(state, backup)

    error = caught.value
    assert error.recovery_directory is not None
    assert _value(error.recovery_directory / "image.sqlite3") == "live"
    assert error.stage_directory is not None and error.stage_directory.is_dir()
    assert error.pre_restore_backup is not None and error.pre_restore_backup.is_dir()
    assert _value(state / "image.sqlite3") == "backup"
    assert publication.read_state_epoch(state).epoch == 1
    # Complete bytes may be visible despite an fsync error; never invalidate
    # those append-only bytes by physically resurrecting the older owners.
    assert publication.read_state_publications(state)[-1].status == "complete"


def test_failed_rollback_rename_keeps_old_bytes_and_blocked_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    real_record = restore.record_state_publication
    real_replace = restore.os.replace

    def fail_complete(*args, **kwargs):
        if kwargs.get("status") == "complete":
            raise publication.StatePublicationError("injected before complete append")
        return real_record(*args, **kwargs)

    def fail_revert(source, destination) -> None:
        if Path(source).parent.name.startswith(".neocortex-state-restore-old-"):
            raise OSError("injected rollback rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(restore, "record_state_publication", fail_complete)
    monkeypatch.setattr(restore.os, "replace", fail_revert)
    with pytest.raises(restore.DatabaseRestoreRecoveryRequiredError) as caught:
        _apply(state, backup)

    recovery = caught.value.recovery_directory
    assert recovery is not None and recovery.is_dir()
    assert _value(recovery / "image.sqlite3") == "live"
    assert _value(state / "image.sqlite3") == "backup"
    assert publication.read_state_publication_state(state).status == "blocked"
    assert publication.read_state_epoch(state).epoch == 0


@pytest.mark.parametrize("after_effect", ["old_owner", "new_owner"])
def test_replace_that_takes_effect_before_raising_cannot_lose_old_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_effect: str
) -> None:
    state, backup = _fixture(tmp_path)
    real_replace = restore.os.replace
    tripped = False

    def raise_after_effect(source, destination) -> None:
        nonlocal tripped
        old_move = Path(destination).parent.name.startswith(".neocortex-state-restore-old-")
        new_move = (
            Path(source).parent.name.startswith(".neocortex-state-restore-")
            and not Path(source).parent.name.startswith(".neocortex-state-restore-old-")
        )
        real_replace(source, destination)
        if not tripped and (old_move if after_effect == "old_owner" else new_move):
            tripped = True
            raise OSError("injected error after physical replace")

    monkeypatch.setattr(restore.os, "replace", raise_after_effect)
    with pytest.raises(restore.DatabaseRestoreError, match="rollback verified"):
        _apply(state, backup)
    assert tripped
    assert _value(state / "image.sqlite3") == "live"
    assert publication.read_state_publication_state(state).status == "absent"
    assert not tuple(tmp_path.glob(".neocortex-state-restore-*"))


def test_rollback_retains_unaccounted_material_instead_of_recursive_cleanup(tmp_path: Path) -> None:
    recovery = tmp_path / "rollback"
    recovery.mkdir()
    unique = recovery / "unaccounted-owner.sqlite3"
    unique.write_bytes(b"unique recovery material")
    primary = RuntimeError("injected bookkeeping interruption")
    result = restore._restore_revert([], [], recovery, primary)
    assert not result.complete
    assert unique.read_bytes() == b"unique recovery material"


def test_second_owner_post_effect_failure_rolls_back_the_whole_selected_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    for owner in ("image", "semantic"):
        _database(state / f"{owner}.sqlite3", f"{owner}-backup", owner)
    backup = restore.backup_state_owners(
        state, tmp_path / "backup", stores=("image", "semantic")
    )
    for owner in ("image", "semantic"):
        _change(state / f"{owner}.sqlite3", f"{owner}-live")
    real_replace = restore.os.replace
    tripped = False

    def fail_second_owner(source, destination) -> None:
        nonlocal tripped
        real_replace(source, destination)
        if (
            not tripped
            and Path(destination) == state / "semantic.sqlite3"
            and not Path(source).parent.name.startswith(".neocortex-state-restore-old-")
        ):
            tripped = True
            raise OSError("injected after second owner replacement")

    monkeypatch.setattr(restore.os, "replace", fail_second_owner)
    with pytest.raises(restore.DatabaseRestoreError, match="rollback verified"):
        restore.restore_state_owners(
            state, backup.backup_directory, stores=("image", "semantic"),
            apply=True, confirmation=restore.DATABASE_RESTORE_CONFIRMATION,
        )
    assert tripped
    for owner in ("image", "semantic"):
        assert _value(state / f"{owner}.sqlite3") == f"{owner}-live"
    assert publication.read_state_publication_state(state).status == "absent"


def test_postappend_lock_cleanup_error_is_still_a_durable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    real_lock = publication._publication_lock

    @contextmanager
    def fail_cleanup(path: Path):
        with real_lock(path):
            yield
        if any(event.status == "complete" for event in publication.read_state_publications(state)):
            raise OSError("injected lock cleanup error after complete")

    monkeypatch.setattr(publication, "_publication_lock", fail_cleanup)
    result = _apply(state, backup)
    assert result.complete
    assert result.publication_warning is not None
    assert "lock cleanup" in result.publication_warning
    assert result.state_epoch.epoch == 1
    assert _value(state / "image.sqlite3") == "backup"


def test_swap_fsync_failure_restores_baseline_and_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    real_sync = restore._fsync_directory
    tripped = False

    def fail_once(path: Path) -> None:
        nonlocal tripped
        if path == state and not tripped:
            tripped = True
            raise restore.DatabasePurgeError("injected swap fsync failure")
        real_sync(path)

    monkeypatch.setattr(restore, "_fsync_directory", fail_once)
    with pytest.raises(restore.DatabaseRestoreError, match="rollback verified"):
        _apply(state, backup)
    assert tripped
    assert _value(state / "image.sqlite3") == "live"
    assert publication.read_state_publication_state(state).status == "absent"
    assert publication.read_state_publications(state)[-1].status == "failed"


def test_historical_backup_can_be_restored_after_later_epochs_repeatedly(tmp_path: Path) -> None:
    state, backup = _fixture(tmp_path)
    publication.record_state_publication(
        state, operation="later-work", owners=("image",), status="complete",
        idempotency_key="later-work", expected_epoch=0,
    )
    preview = restore.restore_state_owners(
        state, backup.backup_directory, stores=("image",), expected_epoch=1
    )
    assert preview.state_epoch.epoch == 1
    first = _apply(state, backup, expected_epoch=1)
    assert first.state_epoch.epoch == 2
    _change(state / "image.sqlite3", "newer-work")
    second = _apply(state, backup, expected_epoch=2)
    assert second.state_epoch.epoch == 3
    assert _value(state / "image.sqlite3") == "backup"
    events = publication.read_state_publications(state)
    assert [event.status for event in events] == [
        "complete", "partial", "complete", "partial", "complete"
    ]
    assert events[1].idempotency_key != events[3].idempotency_key
    assert publication.read_state_publication_state(state).status == "complete"
    with pytest.raises(restore.DatabaseRestoreError, match="epoch changed"):
        _apply(state, backup, expected_epoch=2)


def test_restore_cas_is_revalidated_under_state_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    real_locks = restore._held_locks

    @contextmanager
    def epoch_race(path: Path):
        publication.record_state_publication(
            state, operation="concurrent", owners=("image",), status="complete",
            idempotency_key="concurrent", expected_epoch=0,
        )
        with real_locks(path):
            yield

    monkeypatch.setattr(restore, "_held_locks", epoch_race)
    with pytest.raises(restore.DatabaseRestoreError, match="changed before restore"):
        _apply(state, backup, expected_epoch=0)
    assert _value(state / "image.sqlite3") == "live"
    assert len(publication.read_state_publications(state)) == 1


@pytest.mark.parametrize("owner", [item.state_owner_id for item in STATE_STORE_REGISTRY.stores])
def test_future_owner_schema_is_rejected_without_effects(tmp_path: Path, owner: str) -> None:
    contract = STATE_STORE_REGISTRY.by_owner(owner)
    state = tmp_path / "state"
    database = state / contract.database_name
    _database(database, "future", owner, contract.expected_schema_version + 1)
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=(owner,))
    old_bytes = database.read_bytes()
    backup_bytes = (backup.backup_directory / contract.database_name).read_bytes()
    for apply in (False, True):
        with pytest.raises(restore.DatabaseRestoreError, match="newer than supported"):
            restore.restore_state_owners(
                state, backup.backup_directory, stores=(owner,), apply=apply,
                confirmation=restore.DATABASE_RESTORE_CONFIRMATION if apply else None,
            )
        assert database.read_bytes() == old_bytes
        assert (backup.backup_directory / contract.database_name).read_bytes() == backup_bytes
        assert publication.read_state_publications(state) == ()


@pytest.mark.parametrize("schema", [0, -1])
def test_unidentified_or_unproven_older_schema_is_not_silently_migrated(
    tmp_path: Path, schema: int
) -> None:
    state = tmp_path / "state"
    _database(state / "image.sqlite3", "unsupported", version=schema)
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=("image",))
    before = (backup.backup_directory / "image.sqlite3").read_bytes()
    with pytest.raises(restore.DatabaseRestoreError, match="schema"):
        _apply(state, backup)
    assert (backup.backup_directory / "image.sqlite3").read_bytes() == before
    assert publication.read_state_publications(state) == ()


def test_staging_schema_is_validated_again_without_modifying_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    source = backup.backup_directory / "image.sqlite3"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    real_backup = restore.backup_sqlite_online

    def corrupt_staging(source_path, destination_path, **kwargs):
        result = real_backup(source_path, destination_path, **kwargs)
        if Path(destination_path).parent.name.startswith(".neocortex-state-restore-"):
            with closing(sqlite3.connect(destination_path)) as connection, connection:
                connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        return result

    monkeypatch.setattr(restore, "backup_sqlite_online", corrupt_staging)
    with pytest.raises(restore.DatabaseRestoreError, match="newer than supported"):
        _apply(state, backup)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert _value(state / "image.sqlite3") == "live"
    assert publication.read_state_publications(state) == ()


def test_current_marker_does_not_admit_a_structurally_malformed_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    database = state / "image.sqlite3"
    _database(database, "malformed")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP INDEX images_run_path_idx")
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=("image",))
    before = database.read_bytes()
    source = backup.backup_directory / "image.sqlite3"
    source_bytes = source.read_bytes()
    with pytest.raises(restore.DatabaseRestoreError, match="owner validation"):
        _apply(state, backup)
    assert database.read_bytes() == before
    assert source.read_bytes() == source_bytes
    assert publication.read_state_publications(state) == ()


def test_future_pragma_cannot_hide_behind_supported_metadata(tmp_path: Path) -> None:
    state = tmp_path / "state"
    database = state / "image.sqlite3"
    _database(database, "future pragma")
    future = STATE_STORE_REGISTRY.by_owner("image").expected_schema_version + 1
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={future}")
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=("image",))
    with pytest.raises(restore.DatabaseRestoreError, match="newer than supported"):
        _apply(state, backup)
    assert publication.read_state_publications(state) == ()


@pytest.mark.parametrize("owner", ["semantic", "catalog"])
def test_known_legacy_schema_is_migrated_only_on_disposable_copies(
    tmp_path: Path, owner: str
) -> None:
    from tests.test_document_catalog_schema_contract import _create_legacy_catalog
    from tests.test_semantic_schema_contract import _create_version_two

    state = tmp_path / "state"
    state.mkdir()
    contract = STATE_STORE_REGISTRY.by_owner(owner)
    database = state / contract.database_name
    if owner == "semantic":
        _create_version_two(database)
    else:
        _create_legacy_catalog(database, 1)
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=(owner,))
    source = backup.backup_directory / contract.database_name
    source_bytes = source.read_bytes()
    live_bytes = database.read_bytes()
    preview = restore.restore_state_owners(state, backup.backup_directory, stores=(owner,))
    assert preview.complete
    assert source.read_bytes() == source_bytes
    assert database.read_bytes() == live_bytes
    assert publication.read_state_publications(state) == ()
    result = restore.restore_state_owners(
        state, backup.backup_directory, stores=(owner,), apply=True,
        confirmation=restore.DATABASE_RESTORE_CONFIRMATION,
    )
    assert result.complete
    assert source.read_bytes() == source_bytes
    with closing(sqlite3.connect(database)) as connection, connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        assert int(version) == contract.expected_schema_version


def test_absent_backup_entry_does_not_authorize_live_owner_deletion(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    backup = restore.backup_state_owners(state, tmp_path / "backup", stores=("image",))
    _database(state / "image.sqlite3", "created-after-backup")
    result = _apply(state, backup)
    assert result.complete and result.restored == ()
    assert _value(state / "image.sqlite3") == "created-after-backup"
    assert result.pre_restore_backup is not None
    assert _value(result.pre_restore_backup.backup_directory / "image.sqlite3") == (
        "created-after-backup"
    )
    assert publication.read_state_publication_state(state).status == "complete"


def test_restore_retains_append_only_history_on_retry_after_verified_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, backup = _fixture(tmp_path)
    real_record = restore.record_state_publication
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if kwargs.get("status") == "complete" and not failed:
            failed = True
            raise publication.StatePublicationError("injected before append")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(restore, "record_state_publication", fail_once)
    with pytest.raises(restore.DatabaseRestoreError, match="rollback verified"):
        _apply(state, backup)
    journal = state / publication.STATE_PUBLICATION_JOURNAL_FILENAME
    prefix = journal.read_bytes()
    result = _apply(state, backup)
    assert result.state_epoch.epoch == 1
    assert journal.read_bytes().startswith(prefix)
    assert [event.status for event in publication.read_state_publications(state)] == [
        "partial", "failed", "partial", "complete"
    ]
    assert publication.read_state_publication_state(state).status == "complete"


def test_manifest_provenance_epoch_remains_historical(tmp_path: Path) -> None:
    state, backup = _fixture(tmp_path)
    original_manifest = backup.manifest.read_bytes()
    _apply(state, backup)
    _apply(state, backup)
    assert backup.manifest.read_bytes() == original_manifest
    assert json.loads(original_manifest)["state_epoch"]["epoch"] == 0


def test_public_cli_previews_and_restores_historical_backup_with_live_cas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from neocortex.interface.entrypoint import entrypoint

    state, backup = _fixture(tmp_path)
    publication.record_state_publication(
        state, operation="later-work", owners=("image",), status="complete",
        idempotency_key="later-work", expected_epoch=0,
    )
    args = (
        "databases", "restore", "--state-directory", str(state),
        "--backup-directory", str(backup.backup_directory), "--store", "image",
        "--expected-epoch", "1", "--json",
    )
    assert entrypoint(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["result"]["mode"] == "preview"
    assert preview["result"]["state_epoch"]["epoch"] == 1
    assert _value(state / "image.sqlite3") == "live"
    apply_args = (
        *args, "--apply", "--manifest-sha256", backup.manifest_sha256,
        "--confirm-database-restore", restore.DATABASE_RESTORE_CONFIRMATION,
    )
    assert entrypoint(apply_args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["mode"] == "applied"
    assert result["result"]["state_epoch"]["epoch"] == 2
    assert _value(state / "image.sqlite3") == "backup"
