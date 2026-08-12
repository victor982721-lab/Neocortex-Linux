"""Durability contracts for explicit file-action reconciliation evidence."""
# region [00] Contexto del módulo
# Módulo: tests/test_file_action_reconciliation_store.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import _04_Nucleo_Operativo.file_action_reconciliation_store as store_module
import _04_Nucleo_Operativo.framework_schema as framework_schema
from _04_Nucleo_Operativo.file_action_reconciliation_store import (
    FileActionReconciliationConflict,
    RecordedFileActionReconciliation,
    record_file_action_reconciliation,
)
from _04_Nucleo_Operativo.file_action_recovery import (
    FILE_ACTION_RECONCILER_SIGNATURE,
    FileActionReconciliation,
    list_file_action_reconciliations,
)
from _04_Nucleo_Operativo.framework_schema import (
    SCHEMA_VERSION,
    initialize_framework_schema,
)
from _04_Nucleo_Operativo.state import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
# endregion [01]

# region [02] Implementación


_TABLE = "file_action_reconciliation_events"
_ACTOR = "neocortex-audit-fixture"
_PROVENANCE = '{"kind":"controlled-test","schema_version":1}'


def _create_version_18_database(
    database: Path,
    *,
    extra_schema: str = "",
) -> None:
    """Build the exact populated predecessor without editing a version number."""

    with closing(sqlite3.connect(database)) as connection:
        for statement in framework_schema._TABLE_STATEMENTS:
            if _TABLE not in statement:
                connection.execute(statement)
        for statement in framework_schema._INDEX_STATEMENTS:
            if _TABLE not in statement:
                connection.execute(statement)
        for statement in framework_schema._TRIGGER_STATEMENTS:
            if _TABLE not in statement:
                connection.execute(statement)
        connection.execute("INSERT INTO metadata VALUES('schema_version','18')")
        connection.execute(
            """INSERT INTO initial_runs(
            run_id,root,started_ns,status,run_kind)
            VALUES(7,'fixture-root',100,'failed','initial')"""
        )
        connection.execute(
            """INSERT INTO file_actions(
            action_id,run_id,action_type,source_path,target_path,detected_mime,
            evidence,apply_requested,status,detail,started_ns,completed_ns,
            idempotency_key,expected_identity_json,effect_receipt_json,applying_ns)
            VALUES(41,7,'correct_extension','fixture-source','fixture-target',
            NULL,'fixture evidence',1,'recovery_required','uncertain',101,NULL,
            'fixture-key','{\"schema_version\":1}',NULL,102)"""
        )
        connection.execute(
            """INSERT INTO file_action_events(
            event_id,action_id,occurred_ns,from_status,to_status,stage,detail)
            VALUES(51,41,103,'applying','recovery_required',
            'recovery_required','uncertain')"""
        )
        if extra_schema:
            connection.executescript(extra_schema)
        connection.commit()


def test_schema_20_migrates_populated_version_18_idempotently(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_18_database(database)

    with FrameworkState(database):
        pass
    with FrameworkState(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        action = connection.execute(
            """SELECT action_id,run_id,status,detail,idempotency_key,
            expected_identity_json,applying_ns FROM file_actions"""
        ).fetchone()
        prior_event = connection.execute(
            """SELECT event_id,action_id,from_status,to_status,stage,detail
            FROM file_action_events"""
        ).fetchone()
        reconciliation_count = connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == (str(SCHEMA_VERSION),) == ("22",)
    assert action == (
        41,
        7,
        "recovery_required",
        "uncertain",
        "fixture-key",
        '{"schema_version":1}',
        102,
    )
    assert prior_event == (
        51,
        41,
        "applying",
        "recovery_required",
        "recovery_required",
        "uncertain",
    )
    assert reconciliation_count == (0,)
    assert integrity == ("ok",)
    assert foreign_keys == []


@pytest.mark.parametrize(
    "extra_schema",
    (
        "ALTER TABLE file_actions ADD COLUMN owner_extension TEXT;",
        "CREATE TABLE owner_recovery_extension(value TEXT);",
        "CREATE INDEX owner_recovery_index ON file_actions(status);",
        """CREATE TRIGGER owner_recovery_trigger AFTER INSERT ON file_actions
        BEGIN SELECT 1; END;""",
    ),
)
def test_schema_20_abstains_and_rolls_back_on_unknown_version_18_objects(
    tmp_path: Path,
    extra_schema: str,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_18_database(database, extra_schema=extra_schema)

    with pytest.raises(RuntimeError, match="schema contract validation failed"):
        FrameworkState(database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("18",)
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
            is None
        )


def test_schema_20_rolls_back_on_base_exception_after_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_18_database(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(KeyboardInterrupt):
            initialize_framework_schema(
                connection,
                lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("18",)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert not connection.in_transaction
    finally:
        connection.close()


def test_schema_20_rolls_back_runtime_error_after_migration(tmp_path: Path) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_18_database(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(RuntimeError, match="injected post-migration failure"):
            initialize_framework_schema(
                connection,
                lambda: (_ for _ in ()).throw(RuntimeError("injected post-migration failure")),
            )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("18",)
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert not connection.in_transaction
    finally:
        connection.close()


def test_schema_20_publication_preserves_concurrent_version_18_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_18_database(database)
    with closing(sqlite3.connect(database)) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)

    reader = sqlite3.connect(database)
    writer = sqlite3.connect(database)
    try:
        reader.execute("BEGIN")
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("18",)
        assert reader.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)

        def observe_unpublished_schema() -> None:
            assert reader.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone() == ("18",)
            assert (
                reader.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
                is None
            )

        initialize_framework_schema(writer, observe_unpublished_schema)
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("18",)
        assert (
            reader.execute("SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)).fetchone()
            is None
        )
        reader.rollback()
        assert reader.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("22",)
        assert reader.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (_TABLE,)
        ).fetchone() == (_TABLE,)
        assert reader.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
    finally:
        if reader.in_transaction:
            reader.rollback()
        reader.close()
        writer.close()


def _action_sandbox(base: Path, *, state_name: str = "state") -> tuple[Path, Path]:
    root = base / "corpus"
    state_directory = base / state_name
    root.mkdir(exist_ok=True)
    state_directory.mkdir(exist_ok=True)
    return state_directory / "framework.sqlite3", root


def _seed_action(database: Path, root: Path, *, status: str) -> tuple[int, int, str]:
    with FrameworkState(database) as state:
        run_id = begin_signed_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "correct_extension",
            str(root / "source.fixture"),
            str(root / "target.fixture"),
            None,
            "controlled fixture",
            True,
        )
        if status == "applying":
            state.mark_file_actions_applying(
                (
                    (
                        action_id,
                        json.dumps(
                            {
                                "schema_version": 1,
                                "source": {"volume_id": "1", "file_id": "2"},
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            )
        elif status == "recovery_required":
            state.require_file_action_recovery((action_id,), "uncertain fixture")
        else:  # pragma: no cover - test helper invariant
            raise AssertionError(status)
        idempotency_key = state._connection.execute(
            "SELECT idempotency_key FROM file_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()[0]
    return run_id, action_id, str(idempotency_key)


def _reconciliation(
    root: Path,
    run_id: int,
    action_id: int,
    idempotency_key: str,
    *,
    status: str = "recovery_required",
    classification: str = "impossible_to_check",
    recommendation: str = "preserve_evidence_and_review_manually",
    detail: str = "controlled fixture has no physical identity",
) -> FileActionReconciliation:
    return FileActionReconciliation(
        action_id=action_id,
        run_id=run_id,
        idempotency_key=idempotency_key,
        action_type="correct_extension",
        source_path=str(root / "source.fixture"),
        target_path=str(root / "target.fixture"),
        recorded_status=status,
        reconciler_signature=FILE_ACTION_RECONCILER_SIGNATURE,
        classification=classification,
        recommendation=recommendation,
        detail=detail,
    )


def _record_state(
    state: FrameworkState,
    reconciliation: FileActionReconciliation,
    *,
    expected_previous_event_id: int | None,
    observed_ns: int | None = None,
    actor: str = _ACTOR,
    provenance_json: str = _PROVENANCE,
) -> RecordedFileActionReconciliation:
    return state.record_file_action_reconciliation(
        reconciliation,
        actor=actor,
        provenance_json=provenance_json,
        expected_previous_event_id=expected_previous_event_id,
        observed_ns=observed_ns,
    )


def _record_connection(
    connection: sqlite3.Connection,
    reconciliation: FileActionReconciliation,
    *,
    expected_previous_event_id: int | None,
    observed_ns: int | None = None,
    actor: str = _ACTOR,
    provenance_json: str = _PROVENANCE,
) -> RecordedFileActionReconciliation:
    return record_file_action_reconciliation(
        connection,
        reconciliation,
        actor=actor,
        provenance_json=provenance_json,
        expected_previous_event_id=expected_previous_event_id,
        observed_ns=observed_ns,
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"action_id": 0}, "identifiers must be positive"),
        ({"action_type": ""}, "type cannot be empty"),
        ({"source_path": ""}, "source path cannot be empty"),
        ({"recorded_status": "failed"}, "status must be applying"),
        ({"reconciler_signature": ""}, "reconciler signature cannot be empty"),
        ({"classification": "unknown"}, "invalid file-action classification"),
        (
            {"recommendation": "confirm_action_record"},
            "recommendation is incompatible",
        ),
        ({"detail": ""}, "detail cannot be empty"),
    ),
)
def test_record_rejects_invalid_reconciliation_contract_before_transaction(
    tmp_path: Path,
    updates: dict[str, Any],
    message: str,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)

    with FrameworkState(database) as state:
        with pytest.raises(ValueError, match=message):
            _record_state(
                state,
                replace(reconciliation, **updates),
                expected_previous_event_id=None,
                observed_ns=100,
            )
        assert not state._connection.in_transaction
        assert state._connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone() == (0,)


def test_record_abstains_on_invalid_frontiers_missing_action_and_identity(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)

    with FrameworkState(database) as state:
        with pytest.raises(ValueError, match="actor cannot be empty"):
            _record_state(
                state,
                reconciliation,
                actor=" ",
                expected_previous_event_id=None,
                observed_ns=99,
            )
        with pytest.raises(ValueError, match="provenance must be valid JSON"):
            _record_state(
                state,
                reconciliation,
                provenance_json="{",
                expected_previous_event_id=None,
                observed_ns=99,
            )
        with pytest.raises(ValueError, match="provenance must be a JSON object"):
            _record_state(
                state,
                reconciliation,
                provenance_json="[]",
                expected_previous_event_id=None,
                observed_ns=99,
            )
        with pytest.raises(ValueError, match="previous reconciliation event"):
            _record_state(
                state,
                reconciliation,
                expected_previous_event_id=0,
                observed_ns=100,
            )
        with pytest.raises(ValueError, match="nonnegative integer"):
            _record_state(
                state,
                reconciliation,
                expected_previous_event_id=None,
                observed_ns=-1,
            )
        state._connection.execute("BEGIN")
        try:
            with pytest.raises(RuntimeError, match="no active transaction"):
                _record_state(
                    state,
                    reconciliation,
                    expected_previous_event_id=None,
                    observed_ns=100,
                )
        finally:
            state._connection.rollback()
        with pytest.raises(FileActionReconciliationConflict, match="disappeared"):
            _record_state(
                state,
                replace(reconciliation, action_id=action_id + 1000),
                expected_previous_event_id=None,
                observed_ns=100,
            )
        with pytest.raises(FileActionReconciliationConflict, match="identity changed"):
            _record_state(
                state,
                replace(reconciliation, source_path=str(root / "different")),
                expected_previous_event_id=None,
                observed_ns=100,
            )
        recorded = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
        )
        assert recorded.observed_ns > 0

    connection = sqlite3.connect(database)
    try:
        with pytest.raises(RuntimeError, match="foreign_keys=ON"):
            _record_connection(
                connection,
                reconciliation,
                expected_previous_event_id=recorded.event_id,
                observed_ns=101,
            )
    finally:
        connection.close()


def test_record_abstains_on_corrupt_or_colliding_existing_evidence(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)
    with FrameworkState(database) as state:
        first = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=150,
        )
        with (
            patch.object(
                store_module,
                "_record_from_row",
                return_value=replace(first, evidence_json="{"),
            ),
            pytest.raises(sqlite3.DatabaseError, match="not valid JSON"),
        ):
            _record_state(
                state,
                reconciliation,
                expected_previous_event_id=None,
                observed_ns=151,
            )
        changed = replace(reconciliation, detail="different evidence")
        with (
            patch.object(
                store_module,
                "_reconciliation_key",
                return_value=first.reconciliation_key,
            ),
            pytest.raises(RuntimeError, match="reconciliation-key collision"),
        ):
            _record_state(
                state,
                changed,
                expected_previous_event_id=None,
                observed_ns=152,
            )
    with pytest.raises(sqlite3.DatabaseError, match="not a SQLite integer"):
        store_module._required_sqlite_integer("1", label="fixture")


def test_status_is_strictly_read_only_and_record_is_explicit_append_only(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")

    before = database.read_bytes()
    status = list_file_action_reconciliations(database)
    assert database.read_bytes() == before
    assert len(status) == 1
    assert status[0].classification == "impossible_to_check"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone() == (0,)

    reconciliation = _reconciliation(root, run_id, action_id, action_key)
    with FrameworkState(database) as state:
        first = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=200,
        )
        repeated = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=999,
            actor=f"  {_ACTOR}  ",
            provenance_json='{ "schema_version": 1, "kind": "controlled-test" }',
        )
        action_status = state._connection.execute(
            "SELECT status FROM file_actions WHERE action_id=?", (action_id,)
        ).fetchone()

    assert repeated == first
    assert first.sequence == 1
    assert first.previous_event_id is None
    assert first.observed_ns == 200
    assert action_status == ("recovery_required",)
    assert not (root / "source.fixture").exists()
    assert not (root / "target.fixture").exists()

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            f"""SELECT action_id,sequence,previous_event_id,action_status,
            reconciler_signature,event_schema_version,actor,provenance_json,
            classification,recommendation,detail,evidence_json FROM {_TABLE}"""
        ).fetchone()
        assert row[:11] == (
            action_id,
            1,
            None,
            "recovery_required",
            FILE_ACTION_RECONCILER_SIGNATURE,
            1,
            _ACTOR,
            _PROVENANCE,
            "impossible_to_check",
            "preserve_evidence_and_review_manually",
            "controlled fixture has no physical identity",
        )
        evidence = json.loads(str(row[11]))
        assert evidence["schema_version"] == 1
        assert evidence["authorizes_filesystem_mutation"] is False
        assert evidence["actor"] == _ACTOR
        assert evidence["provenance"] == {
            "kind": "controlled-test",
            "schema_version": 1,
        }
        assert (
            evidence["reconciliation"]["reconciler_signature"] == FILE_ACTION_RECONCILER_SIGNATURE
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                f"UPDATE {_TABLE} SET detail='forbidden' WHERE action_id=?",
                (action_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(f"DELETE FROM {_TABLE} WHERE action_id=?", (action_id,))


def test_reconciliation_schema_rejects_incompatible_and_cross_action_events(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)
    with FrameworkState(database) as state:
        first = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=250,
        )
    _other_run, other_action, _other_key = _seed_action(database, root, status="recovery_required")

    statement = f"""INSERT INTO {_TABLE}(
    action_id,sequence,previous_event_id,reconciliation_key,observed_ns,
    recorded_ns,action_status,reconciler_signature,event_schema_version,actor,
    provenance_json,classification,recommendation,detail,evidence_json)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                statement,
                (
                    action_id,
                    2,
                    first.event_id,
                    "invalid-recommendation",
                    251,
                    252,
                    "recovery_required",
                    FILE_ACTION_RECONCILER_SIGNATURE,
                    1,
                    _ACTOR,
                    _PROVENANCE,
                    "confirmed",
                    "preserve_evidence_and_review_manually",
                    "invalid classification pair",
                    "{}",
                ),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                statement,
                (
                    other_action,
                    2,
                    first.event_id,
                    "cross-action-predecessor",
                    253,
                    254,
                    "recovery_required",
                    FILE_ACTION_RECONCILER_SIGNATURE,
                    1,
                    _ACTOR,
                    _PROVENANCE,
                    "ambiguous",
                    "preserve_evidence_and_review_manually",
                    "predecessor belongs to another action",
                    "{}",
                ),
            )


def test_record_enforces_action_and_event_compare_and_swap(tmp_path: Path) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)

    with FrameworkState(database) as state:
        first = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=300,
        )
        with pytest.raises(FileActionReconciliationConflict, match="latest event"):
            _record_state(
                state,
                reconciliation,
                actor="different-actor",
                expected_previous_event_id=None,
                observed_ns=300,
            )
        changed = replace(
            reconciliation,
            classification="ambiguous",
            detail="a second controlled observation is ambiguous",
        )
        with pytest.raises(FileActionReconciliationConflict, match="latest event"):
            _record_state(
                state,
                changed,
                expected_previous_event_id=None,
                observed_ns=301,
            )
        second = _record_state(
            state,
            changed,
            expected_previous_event_id=first.event_id,
            observed_ns=302,
        )
        assert second.sequence == 2
        assert second.previous_event_id == first.event_id

    stale_database, stale_root = _action_sandbox(
        tmp_path,
        state_name="stale-state",
    )
    stale_run, stale_action, stale_key = _seed_action(stale_database, stale_root, status="applying")
    stale = _reconciliation(
        stale_root,
        stale_run,
        stale_action,
        stale_key,
        status="applying",
    )
    with FrameworkState(stale_database) as state:
        state.require_file_action_recovery((stale_action,), "frontier uncertainty")
        with pytest.raises(FileActionReconciliationConflict, match="status changed"):
            _record_state(
                state,
                stale,
                expected_previous_event_id=None,
                observed_ns=400,
            )
        assert state._connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone() == (0,)


def test_record_rolls_back_base_exception_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)

    with FrameworkState(database) as state:
        with (
            patch.object(
                store_module,
                "_after_reconciliation_insert",
                side_effect=KeyboardInterrupt(),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _record_state(
                state,
                reconciliation,
                expected_previous_event_id=None,
                observed_ns=500,
            )
        assert not state._connection.in_transaction
        assert state._connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone() == (0,)
        recorded = _record_state(
            state,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=501,
        )
        assert recorded.sequence == 1


def _concurrent_record(
    database: Path,
    barrier: threading.Barrier,
    reconciliation: FileActionReconciliation,
) -> tuple[str, int | str]:
    try:
        with FrameworkState(database) as state:
            barrier.wait(timeout=10)
            recorded = _record_state(
                state,
                reconciliation,
                expected_previous_event_id=None,
                observed_ns=600,
            )
        return "recorded", recorded.event_id
    except FileActionReconciliationConflict as exc:
        return "conflict", str(exc)


def test_concurrent_identical_record_is_idempotent_and_conflict_does_not_fork(
    tmp_path: Path,
) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_concurrent_record, database, barrier, reconciliation) for _ in range(2)
        ]
        identical = [future.result(timeout=20) for future in futures]
    assert {result[0] for result in identical} == {"recorded"}
    assert len({result[1] for result in identical}) == 1

    conflict_database, conflict_root = _action_sandbox(
        tmp_path,
        state_name="conflict-state",
    )
    conflict_run, conflict_action, conflict_key = _seed_action(
        conflict_database,
        conflict_root,
        status="recovery_required",
    )
    first = _reconciliation(
        conflict_root,
        conflict_run,
        conflict_action,
        conflict_key,
        detail="first",
    )
    second = replace(first, classification="ambiguous", detail="second")
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_concurrent_record, conflict_database, barrier, item)
            for item in (first, second)
        ]
        competing = [future.result(timeout=20) for future in futures]
    assert sorted(result[0] for result in competing) == ["conflict", "recorded"]
    with closing(sqlite3.connect(conflict_database)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone() == (1,)


def test_temporary_sqlite_lock_leaves_no_partial_record(tmp_path: Path) -> None:
    database, root = _action_sandbox(tmp_path)
    run_id, action_id, action_key = _seed_action(database, root, status="recovery_required")
    reconciliation = _reconciliation(root, run_id, action_id, action_key)
    blocker = sqlite3.connect(database, timeout=0.1)
    candidate = sqlite3.connect(database, timeout=0.01)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        candidate.execute("PRAGMA busy_timeout=1")
        candidate.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _record_connection(
                candidate,
                reconciliation,
                expected_previous_event_id=None,
                observed_ns=700,
            )
        assert not candidate.in_transaction
        blocker.rollback()
        recorded = _record_connection(
            candidate,
            reconciliation,
            expected_previous_event_id=None,
            observed_ns=701,
        )
        assert recorded.sequence == 1
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        candidate.close()


# endregion [02]
