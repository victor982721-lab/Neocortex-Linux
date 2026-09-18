"""Selectable state-reset engine contracts on isolated fixture owners."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.capabilities.formats.image.state import initialize_image_state
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.persistence.state_publication import (
    publication_idempotency_key,
    record_state_publication,
)
from neocortex.persistence.state_reset import (
    STATE_RESET_CONFIRMATION,
    StateResetBusyError,
    StateResetChangedError,
    StateResetConfirmationError,
    StateResetError,
    StateResetPlan,
    StateResetResult,
    execute_state_reset,
    plan_state_reset,
)


def _framework_database(state: Path) -> Path:
    database = state / "framework.sqlite3"
    with FrameworkState(database):
        pass
    return database


def _seed_runs(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        for run_id in (10, 20):
            connection.execute(
                "INSERT INTO initial_runs(run_id,root,started_ns,status) "
                "VALUES(?,?,?,'completed')",
                (run_id, "/tmp/fixture-corpus", run_id),
            )
            connection.execute(
                "INSERT INTO run_events(event_id,run_id,occurred_ns,level,phase,message) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, run_id, run_id, "info", "fixture", "completed"),
            )
            connection.execute(
                "INSERT INTO route_runs(run_id,route_name,status,started_ns) "
                "VALUES(?,?,?,?)",
                (run_id, "text", "completed", run_id),
            )
            connection.execute(
                """INSERT INTO route_phase_runs(
                    run_id,route_name,phase_name,status,started_ns
                ) VALUES(?,?,?,?,?)""",
                (run_id, "text", "extract", "completed", run_id),
            )
            connection.execute(
                """INSERT INTO run_actions(
                    run_id,apply_actions,duplicate_candidates,duplicates_trashed,
                    duplicate_skips,files_checked,types_detected,extensions_matching,
                    unknown_types,rename_candidates,files_renamed,rename_skips,
                    empty_directory_candidates,empty_directories_trashed,
                    empty_directory_skips,errors
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, *([0] * 15)),
            )
            connection.execute(
                """INSERT INTO route_candidates(
                    run_id,mime,path,volume_id,file_id,size,mtime_ns,birthtime_ns
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, "text/plain", f"/tmp/{run_id}.txt", "v", str(run_id), 1, 1, -1),
            )


def _create_image_database(state: Path) -> Path:
    database = state / "image.sqlite3"
    initialize_image_state(database)
    # A Connection context commits/rolls back; it does not close. Leaving it
    # for cyclic GC can checkpoint this source while the reset backs it up.
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO metadata VALUES('fixture_payload','preserve')")
    return database


def _write_catalog_migration_backup(state: Path) -> tuple[Path, Path]:
    backup = state / "document_catalog.sqlite3.pre-v7-to-v8-123456789.sqlite3"
    receipt = Path(f"{backup}.json")
    backup.write_bytes(b"fixture catalog migration backup")
    receipt.write_text(
        json.dumps(
            {
                "source": str(state / "document_catalog.sqlite3"),
                "backup": str(backup),
                "prior_schema": 7,
                "target_schema": 8,
                "sha256": "fixture",
                "bytes": backup.stat().st_size,
            }
        ),
        encoding="utf-8",
    )
    return backup, receipt


def test_preview_is_read_only_and_scope_selection_is_explicit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    framework = _framework_database(state)
    image = _create_image_database(state)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in state.rglob("*")
        if path.is_file()
    }

    plans = {
        scope: plan_state_reset(state, scope=scope)
        for scope in ("runs", "runs-and-caches", "all")
    }

    assert all(isinstance(plan, StateResetPlan) for plan in plans.values())
    assert plans["runs"].stores == ("framework",)
    assert {target.target_id for target in plans["runs"].targets} == {
        "framework-run-ledger"
    }
    assert {target.target_id for target in plans["runs-and-caches"].targets} == {
        "sqlite:framework",
        "sqlite:image",
    }
    assert {target.target_id for target in plans["all"].targets} >= {
        "runtime-cache"
    }
    assert framework.is_file() and image.is_file()
    assert {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in state.rglob("*")
        if path.is_file()
    } == before


def test_runs_uses_staged_framework_reset_and_keeps_recovery_parent_and_floor(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = _framework_database(state)
    _seed_runs(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO file_actions(
                action_id,run_id,action_type,source_path,apply_requested,status,started_ns
            ) VALUES(?,?,?,?,?,?,?)""",
            (1, 10, "fixture", "/tmp/fixture", 0, "recovery_required", 1),
        )
        connection.execute(
            """INSERT INTO content_type_cache(
                volume_id,file_id,size,mtime_ns,detector_version,status,
                last_seen_run_id,updated_ns
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("v", "10", 1, 1, "fixture", "unknown", 10, 1),
        )

    preview = plan_state_reset(state, scope="runs")
    assert preview.framework_plan is not None
    assert preview.framework_plan.delete_run_ids == (20,)
    assert preview.framework_plan.retained_run_ids == (10,)
    result = execute_state_reset(
        state,
        scope="runs",
        apply=True,
        plan_digest=preview.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup",
    )

    assert isinstance(result, StateResetResult)
    assert result.framework_result is not None
    assert result.framework_result.deleted_run_ids == (20,)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT run_id FROM initial_runs").fetchall() == [(10,)]
        assert connection.execute("SELECT run_id FROM file_actions").fetchall() == [(10,)]
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='framework_run_id_floor'"
        ).fetchone() == ("20",)
    assert result.manifest is not None and result.manifest.is_file()
    assert json.loads(result.manifest.read_text(encoding="utf-8"))["status"] == "applied"


def test_runs_and_caches_preserves_framework_identity_floor_and_retires_cache_metadata(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    framework = _framework_database(state)
    image = _create_image_database(state)
    (state / "state-epoch.json").write_text(
        json.dumps(
            {
                "schema": "neocortex.state-publication/v1",
                "epoch": 0,
                "event_id": None,
                "operation": None,
                "owners": [],
                "manifest_sha256": None,
                "source": "pointer",
                "owner_heads": [],
            }
        ),
        encoding="utf-8",
    )
    preview = plan_state_reset(state, scope="runs-and-caches")
    result = execute_state_reset(
        state,
        scope="runs-and-caches",
        apply=True,
        plan_digest=preview.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup",
    )

    assert isinstance(result, StateResetResult)
    assert framework.exists() and not image.exists()
    with closing(sqlite3.connect(framework)) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='operational_reset_barrier'").fetchone() is not None
    assert not (state / "state-epoch.json").exists()
    assert result.backup_manifest is not None and result.backup_manifest.is_file()
    assert result.backup_directory is not None
    assert (result.backup_directory / "reset-files" / "framework.sqlite3").is_file()


def test_all_blocks_unknown_then_retires_managed_trees_and_preserves_receipts(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    (state / "runtime-cache").mkdir()
    (state / "runtime-cache" / "fixture.bin").write_bytes(b"cache")
    (state / "curation" / "checkpoints").mkdir(parents=True)
    (state / "curation" / "checkpoints" / "checkpoint.json").write_text(
        "fixture", encoding="utf-8"
    )
    (state / "installation-receipts").mkdir()
    receipt = state / "installation-receipts" / "keep.json"
    receipt.write_text("keep", encoding="utf-8")
    unknown = state / "future-state.json"
    unknown.write_text("future", encoding="utf-8")
    managed_manifest = state / f"content-publication-manifest.{'a' * 64}.json"
    managed_manifest.write_text("managed", encoding="utf-8")
    route_lock = state / "text.sqlite3.route.lock"
    route_lock.write_text("lock", encoding="utf-8")

    preview = plan_state_reset(state, scope="all")
    planned_paths = {entry.path for entry in preview.entries}
    assert managed_manifest in planned_paths
    assert planned_paths.isdisjoint(preview.unmanaged_state_entries)
    assert route_lock not in preview.unmanaged_state_entries
    with pytest.raises(StateResetError, match="unclaimed"):
        execute_state_reset(state, scope="all", apply=True, plan_digest=preview.plan_digest,
                            confirmation=STATE_RESET_CONFIRMATION)
    assert image.exists() and unknown.read_text() == "future"
    # Explicit fixture custody outside state resolves the unknown object; reset
    # itself never adopts or removes it.
    preserved_unknown = tmp_path / "preserved-future.json"
    unknown.rename(preserved_unknown)
    preview = plan_state_reset(state, scope="all")
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=preview.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup",
    )

    assert isinstance(result, StateResetResult)
    assert not image.exists()
    assert not (state / "runtime-cache").exists()
    assert not (state / "curation" / "checkpoints").exists()
    assert not managed_manifest.exists()
    assert route_lock.read_text(encoding="utf-8") == "lock"
    assert receipt.read_text(encoding="utf-8") == "keep"
    assert preserved_unknown.read_text(encoding="utf-8") == "future"


def test_all_preserves_verified_catalog_migration_backup_pair(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    backup, receipt = _write_catalog_migration_backup(state)
    sidecars = {
        Path(f"{backup}{suffix}"): f"fixture {suffix}".encode("ascii")
        for suffix in ("-journal", "-wal", "-shm")
    }
    for path, content in sidecars.items():
        path.write_bytes(content)
    preserved_bytes = {
        path: path.read_bytes() for path in (backup, receipt, *sidecars)
    }

    preview = plan_state_reset(state, scope="all")
    assert backup in preview.unmanaged_state_entries
    assert receipt in preview.unmanaged_state_entries
    assert all(path in preview.unmanaged_state_entries for path in sidecars)
    result = execute_state_reset(
        state,
        scope="all",
        apply=True,
        plan_digest=preview.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "backup",
    )

    assert isinstance(result, StateResetResult)
    assert not image.exists()
    assert {path: path.read_bytes() for path in (backup, receipt, *sidecars)} == preserved_bytes


def test_all_still_blocks_unknown_sqlite_with_verified_catalog_backup(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    backup, receipt = _write_catalog_migration_backup(state)
    unknown = state / "unknown.sqlite3"
    unknown.write_bytes(b"unknown")

    preview = plan_state_reset(state, scope="all")
    with pytest.raises(StateResetError, match="unknown or recovery state entries"):
        execute_state_reset(
            state,
            scope="all",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "blocked-backup",
        )
    assert image.is_file()
    assert backup.is_file() and receipt.is_file() and unknown.is_file()


@pytest.mark.parametrize("missing", ["backup", "receipt"])
def test_all_blocks_incomplete_catalog_migration_backup_pair(
    tmp_path: Path,
    missing: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    backup, receipt = _write_catalog_migration_backup(state)
    if missing == "backup":
        backup.unlink()
    else:
        receipt.unlink()

    preview = plan_state_reset(state, scope="all")
    with pytest.raises(StateResetError, match="unknown or recovery state entries"):
        execute_state_reset(
            state,
            scope="all",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "incomplete-backup",
        )
    assert image.is_file()


@pytest.mark.parametrize("field", ["backup", "source"])
def test_all_blocks_catalog_migration_backup_with_wrong_receipt_path(
    tmp_path: Path,
    field: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    backup, receipt = _write_catalog_migration_backup(state)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload[field] = str(state / "other.sqlite3")
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    preview = plan_state_reset(state, scope="all")
    with pytest.raises(StateResetError, match="unknown or recovery state entries"):
        execute_state_reset(
            state,
            scope="all",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "wrong-receipt-backup",
        )
    assert image.is_file() and backup.is_file() and receipt.is_file()


def test_apply_requires_exact_token_and_digest_without_mutating_fixture(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    preview = plan_state_reset(state, scope="runs-and-caches")

    with pytest.raises(StateResetConfirmationError):
        execute_state_reset(
            state,
            scope="runs-and-caches",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation="RESET",
            backup_directory=tmp_path / "bad-token",
        )
    with pytest.raises(StateResetConfirmationError):
        execute_state_reset(
            state,
            scope="runs-and-caches",
            apply=True,
            plan_digest="0" * 64,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "bad-digest",
        )
    assert image.is_file()
    assert not (tmp_path / "bad-token").exists()
    assert not (tmp_path / "bad-digest").exists()


def test_plan_drift_is_rejected_before_backup_or_removal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    preview = plan_state_reset(state, scope="runs-and-caches")
    with sqlite3.connect(image) as connection:
        connection.execute("INSERT INTO metadata VALUES('changed','yes')")

    with pytest.raises(StateResetConfirmationError):
        execute_state_reset(
            state,
            scope="runs-and-caches",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "stale",
        )
    assert image.is_file()


def test_pending_publication_blocks_runs_but_is_removed_by_cache_scope(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _framework_database(state)
    record_state_publication(
        state,
        operation="fixture",
        owners=("image",),
        status="partial",
        idempotency_key=publication_idempotency_key("fixture"),
    )
    runs = plan_state_reset(state, scope="runs")
    with pytest.raises(StateResetBusyError):
        execute_state_reset(
            state,
            scope="runs",
            apply=True,
            plan_digest=runs.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "runs-backup",
        )
    caches = plan_state_reset(state, scope="runs-and-caches")
    execute_state_reset(
        state,
        scope="runs-and-caches",
        apply=True,
        plan_digest=caches.plan_digest,
        confirmation=STATE_RESET_CONFIRMATION,
        backup_directory=tmp_path / "cache-backup",
    )
    assert not (state / "state-publication-journal.jsonl").exists()


def test_reset_rollback_restores_raw_files_when_removal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    image = _create_image_database(state)
    original_bytes = image.read_bytes()
    preview = plan_state_reset(state, scope="runs-and-caches")

    import neocortex.persistence.state_reset as engine

    real_delete = engine._delete_entries

    def remove_then_fail(entries):
        real_delete(entries)
        raise StateResetChangedError("fixture failure after removal")

    monkeypatch.setattr(engine, "_delete_entries", remove_then_fail)
    with pytest.raises(StateResetChangedError):
        execute_state_reset(
            state,
            scope="runs-and-caches",
            apply=True,
            plan_digest=preview.plan_digest,
            confirmation=STATE_RESET_CONFIRMATION,
            backup_directory=tmp_path / "rollback-backup",
        )
    assert image.is_file()
    assert image.read_bytes() == original_bytes
    manifest = tmp_path / "rollback-backup" / "state-reset-manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "rolled-back"
