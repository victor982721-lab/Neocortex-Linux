"""Candidate regressions for reset lifecycle and truthful target accounting."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.persistence import state_reset as reset
from neocortex.api.cli.state_reset import _render
from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.documents.document_catalog import initialize_document_catalog
from neocortex.deduplication.persistence import initialize_inventory_schema
from neocortex.persistence.framework_state_writer import FrameworkState


def _fixture(tmp_path: Path):
    state = tmp_path / "state"
    root = state / "runtime-cache"
    root.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a")
    (root / "b.bin").write_bytes(b"b")
    return state, root


def _apply(state: Path):
    plan = reset.plan_state_reset(state, scope="all")
    return reset.execute_state_reset(
        state, scope="all", apply=True, plan_digest=plan.plan_digest,
        confirmation=reset.STATE_RESET_CONFIRMATION,
    )


def _raw_areas(tmp_path: Path):
    return list(tmp_path.glob(".neocortex-state-reset-raw-*"))


def test_copy_failure_has_intent_before_copy_and_cleans_without_restore(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    observed = []

    def fail_copy(entry, destination):
        receipt = destination.parents[2] / "state-reset-manifest.json"
        payload = json.loads(receipt.read_text())
        observed.append(payload)
        raise OSError("copy failed before effects")

    def forbidden_restore(*args):
        pytest.fail("an incomplete rollback copy must never restore source")

    monkeypatch.setattr(reset, "_copy_raw_file", fail_copy)
    monkeypatch.setattr(reset, "_restore_raw", forbidden_restore)
    with pytest.raises(reset.StateResetError, match="before effects"):
        _apply(state)
    assert observed[0]["status"] == "preparing"
    assert observed[0]["rollback_storage"] == "transient"
    assert observed[0]["raw_backup_directory"].endswith("/reset-files")
    assert (root / "a.bin").read_bytes() == b"a"
    assert not _raw_areas(tmp_path)


def test_first_receipt_failure_cleans_acquired_area(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    def fail_receipt(*args, **kwargs):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(reset, "_write_json", fail_receipt)
    with pytest.raises(reset.StateResetError, match="before effects"):
        _apply(state)
    assert (root / "a.bin").read_bytes() == b"a"
    assert not _raw_areas(tmp_path)


def test_partial_copy_failure_keeps_sources_and_no_anonymous_area(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    copy = reset._copy_raw_file
    def partial(entry, destination):
        if entry.path.name == "b.bin":
            raise OSError("partial copy")
        return copy(entry, destination)
    monkeypatch.setattr(reset, "_copy_raw_file", partial)
    with pytest.raises(reset.StateResetError, match="before effects"):
        _apply(state)
    assert (root / "a.bin").read_bytes() == b"a"
    assert (root / "b.bin").read_bytes() == b"b"
    assert not _raw_areas(tmp_path)


def test_pre_effect_cleanup_failure_retains_explicit_receipt(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    monkeypatch.setattr(reset, "_copy_raw_file", lambda *a: (_ for _ in ()).throw(OSError("copy")))
    monkeypatch.setattr(reset, "_retire_reset_rollback_area", lambda *a: (_ for _ in ()).throw(OSError("cleanup")))
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        _apply(state)
    receipt = failure.value.operation_manifest
    assert receipt is not None and receipt.is_file()
    assert str(receipt) in str(failure.value)
    assert json.loads(receipt.read_text())["status"] == "pre-effect-cleanup-pending"
    assert (root / "b.bin").read_bytes() == b"b"


def test_cleanup_failure_after_verified_effect_does_not_restore_sources(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    monkeypatch.setattr(reset, "_retire_reset_rollback_area", lambda *a: (_ for _ in ()).throw(OSError("cleanup")))
    with pytest.raises(reset.StateResetRecoveryRequiredError, match="reset applied") as failure:
        _apply(state)
    assert not root.exists()
    receipt = failure.value.operation_manifest
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "applied-cleanup-pending"
    assert (receipt.parent / "reset-files/runtime-cache/a.bin").read_bytes() == b"a"


def test_failure_during_effect_restores_raw_and_cleans(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    delete = reset._delete_entries
    def failed_effect(entries):
        delete(entries)
        raise OSError("failure after unlink")
    monkeypatch.setattr(reset, "_delete_entries", failed_effect)
    with pytest.raises(reset.StateResetError, match="rolled back"):
        _apply(state)
    assert (root / "a.bin").read_bytes() == b"a"
    assert (root / "b.bin").read_bytes() == b"b"
    assert not _raw_areas(tmp_path)


def test_failed_rollback_retains_raw_and_status(tmp_path, monkeypatch):
    state, _root = _fixture(tmp_path)
    def fail(*args):
        raise OSError("fixture failure")
    monkeypatch.setattr(reset, "_delete_entries", fail)
    monkeypatch.setattr(reset, "_restore_raw", fail)
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        _apply(state)
    receipt = failure.value.operation_manifest
    assert json.loads(receipt.read_text())["status"] == "rollback-failed"
    assert (receipt.parent / "reset-files/runtime-cache/b.bin").read_bytes() == b"b"


def test_drift_before_effect_preserves_new_source_and_cleans_old_copy(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    copy_all = reset._raw_backup_entries
    def change_after_copy(*args):
        result = copy_all(*args)
        (root / "a.bin").write_bytes(b"new external content")
        return result
    monkeypatch.setattr(reset, "_raw_backup_entries", change_after_copy)
    with pytest.raises(reset.StateResetChangedError):
        _apply(state)
    assert (root / "a.bin").read_bytes() == b"new external content"
    assert not _raw_areas(tmp_path)


def test_unassessed_archive_blocks_before_any_full_reset_effect(tmp_path):
    state, root = _fixture(tmp_path)
    archive = state / "archive-materialized"
    archive.mkdir()
    (archive / "unique.txt").write_bytes(b"preserve")
    plan = reset.plan_state_reset(state, scope="all")
    assert plan.inventory is not None and plan.inventory.complete
    assert any("unclaimed" in reason for reason in plan.inventory.blockers)
    with pytest.raises(reset.StateResetError, match="unclaimed"):
        _apply(state)
    assert (archive / "unique.txt").read_bytes() == b"preserve"
    assert root.exists() and not _raw_areas(tmp_path)


def test_owner_counters_distinguish_selection_and_main_database_removal(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    database = state / "image.sqlite3"
    initialize_image_state(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO metadata VALUES('fixture_payload','preserve')")
    result = _apply(state).as_payload()
    assert result["selected_owner_count"] == 1
    assert result["transformed_owner_count"] == 0
    assert result["removed_owner_database_count"] == 1
    assert result["preserved_owner_database_count"] == 0
    assert result["database_count"] == 1  # compatible legacy selection counter
    _render({"status": "complete", "result": result}, json_output=False)
    rendered = capsys.readouterr().out
    assert "STATE_RESET_SELECTED_OWNER_COUNT value=1" in rendered
    assert "STATE_RESET_REMOVED_OWNER_DATABASE_COUNT value=1" in rendered
    assert "STATE_RESET_DATABASE_COUNT" not in rendered
    assert "operational_freshness=fresh" in rendered


def test_three_protected_owners_are_transformed_not_reported_as_removed(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    with FrameworkState(state / "framework.sqlite3"):
        pass
    initialize_document_catalog(state / "document_catalog.sqlite3")
    initialize_inventory_schema(state / "dedup.sqlite3")
    databases = tuple(state / name for name in ("framework.sqlite3", "document_catalog.sqlite3", "dedup.sqlite3"))
    for database in databases:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO metadata(key,value) VALUES('fixture_policy','preserve')")
    payload = _apply(state).as_payload()
    assert payload["selected_owner_count"] == 3
    assert payload["transformed_owner_count"] == 3
    assert payload["removed_owner_database_count"] == 0
    assert payload["preserved_owner_database_count"] == 3
    assert payload["operational_freshness"] == "fresh"
    for database in databases:
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT value FROM metadata WHERE key='fixture_policy'").fetchall() == [("preserve",)]
    _render({"status": "complete", "result": payload}, json_output=False)
    rendered = capsys.readouterr().out
    assert "STATE_RESET_REMOVED_OWNER_DATABASE_COUNT value=0" in rendered
    assert rendered.count("STATE_RESET_PRESERVED_OWNER owner=") == 3


def test_empty_replay_is_explicit_no_changes(tmp_path):
    state, _root = _fixture(tmp_path)
    _apply(state)
    payload = _apply(state).as_payload()
    assert payload["effect_outcome"] == "no_changes"
    assert payload["selected_owner_count"] == 0
    assert not _raw_areas(tmp_path)


def test_failed_applied_receipt_keeps_verified_effect_and_recovery_evidence(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    write = reset._write_json
    def fail_applied(path, payload):
        if payload["status"] == "applied":
            raise OSError("final receipt publication failed")
        return write(path, payload)
    monkeypatch.setattr(reset, "_write_json", fail_applied)
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        _apply(state)
    assert not root.exists()
    receipt = failure.value.operation_manifest
    assert json.loads(receipt.read_text())["status"] == "applied-cleanup-pending"
    assert (receipt.parent / "reset-files/runtime-cache/a.bin").read_bytes() == b"a"


def test_payload_cleanup_failure_preserves_receipt_until_payload_retirement(tmp_path, monkeypatch):
    state, root = _fixture(tmp_path)
    rmtree = reset.shutil.rmtree
    def partial_cleanup(path, *args, **kwargs):
        if path == "reset-files":
            directory_fd = kwargs["dir_fd"]
            assert "state-reset-manifest.json" in os.listdir(directory_fd)
            os.unlink("reset-files/runtime-cache/a.bin", dir_fd=directory_fd)
            raise OSError("partial payload cleanup")
        return rmtree(path, *args, **kwargs)
    monkeypatch.setattr(reset.shutil, "rmtree", partial_cleanup)
    with pytest.raises(reset.StateResetRecoveryRequiredError) as failure:
        _apply(state)
    assert not root.exists()
    receipt = failure.value.operation_manifest
    assert json.loads(receipt.read_text())["status"] == "applied-cleanup-pending"
    assert not (receipt.parent / "reset-files/runtime-cache/a.bin").exists()
    assert (receipt.parent / "reset-files/runtime-cache/b.bin").read_bytes() == b"b"
