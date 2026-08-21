"""Transactional publication tests for protected self-analysis completion."""
# region [00] Contexto del módulo
# Módulo: tests/test_self_analysis_finalization.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import FileSnapshot, InventoryExclusionPolicy
from _04_Nucleo_Operativo.corpus_access import CorpusAccessPolicy
from _04_Nucleo_Operativo.models import ActionSummary
from _04_Nucleo_Operativo.self_analysis import (
    SELF_ANALYSIS_MANIFEST_MESSAGE,
    SELF_ANALYSIS_MANIFEST_PHASE,
    SELF_ANALYSIS_MANIFEST_SCHEMA,
    build_self_analysis_completion_manifest,
)
from _04_Nucleo_Operativo.state import FrameworkState
# endregion [01]

# region [02] Implementación


_CODE_SIGNATURE = "code-v2:fixture|code-analyzers-v1:fixture"


def _commands(root: Path, state_directory: Path) -> dict[str, list[str]]:
    return {
        "analyze": [
            "Neocortex",
            "--self-analysis",
            "--root",
            str(root),
            "--state-directory",
            str(state_directory),
        ],
        "status": [
            "Neocortex",
            "--state-directory",
            str(state_directory),
            "--code-status",
            "--code-json",
        ],
    }


def _ready_run(
    state: FrameworkState,
    root: Path,
    state_directory: Path,
    *,
    scan_id: int = 7,
) -> tuple[int, JournalCursor, InventoryExclusionPolicy]:
    cursor = JournalCursor(root.drive or "C:", 17, 100)
    access = CorpusAccessPolicy.capture("analyze_only", root)
    inventory_policy = InventoryExclusionPolicy.compile((state_directory,))
    run_id = state.begin_self_analysis_run(
        access,
        cursor,
        state_directory=state_directory,
        inventory_policy_signature=inventory_policy.signature,
    )
    state.publish_initial_routing_snapshot(run_id, scan_id, 0, 1, "full", 0)
    state.begin_route_runs(run_id, ("code",))
    state.complete_route_run(
        run_id,
        "code",
        {
            "elapsed_ns": 5,
            "processing_signature": _CODE_SIGNATURE,
            "candidates": 1,
            "processed": 1,
            "errors": 0,
        },
    )
    return run_id, cursor, inventory_policy


def _complete(
    state: FrameworkState,
    run_id: int,
    cursor: JournalCursor,
    inventory_policy: InventoryExclusionPolicy,
    root: Path,
    state_directory: Path,
) -> dict[str, object]:
    return state.complete_self_analysis_run(
        run_id,
        cursor,
        inventory_policy=inventory_policy,
        code_processing_signature=_CODE_SIGNATURE,
        commands=_commands(root, state_directory),
    )


def test_self_analysis_completion_atomically_publishes_one_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id, cursor, inventory_policy = _ready_run(state, root, state_directory)
        statements: list[str] = []
        state._connection.set_trace_callback(statements.append)
        try:
            manifest = _complete(
                state,
                run_id,
                cursor,
                inventory_policy,
                root,
                state_directory,
            )
        finally:
            state._connection.set_trace_callback(None)
        with pytest.raises(ValueError, match="not a running"):
            _complete(
                state,
                run_id,
                cursor,
                inventory_policy,
                root,
                state_directory,
            )
        assert state.fail_initial_run(run_id) is False
        assert state.cancel_initial_run(run_id) is False

    transaction_statements = [statement.strip() for statement in statements]
    assert transaction_statements.count("BEGIN IMMEDIATE") == 1
    assert transaction_statements.count("COMMIT") == 1
    assert "ROLLBACK" not in transaction_statements
    update_index = next(
        index
        for index, statement in enumerate(transaction_statements)
        if statement.startswith("UPDATE initial_runs SET completed_ns=")
    )
    insert_index = next(
        index
        for index, statement in enumerate(transaction_statements)
        if statement.startswith("INSERT INTO run_events(")
    )
    commit_index = transaction_statements.index("COMMIT")
    assert update_index < insert_index < commit_index

    assert manifest["schema"] == SELF_ANALYSIS_MANIFEST_SCHEMA
    assert manifest["safety"] == {
        "route_candidates": 0,
        "file_actions": 0,
        "run_actions": 0,
        "organization_events": 0,
    }
    with sqlite3.connect(database) as verification:
        row = verification.execute(
            "SELECT status,current_phase,completed_ns FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        events = verification.execute(
            """SELECT message,details_json FROM run_events
            WHERE run_id=? AND phase=?""",
            (run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchall()
    assert row[0:2] == ("completed", "completed")
    assert row[2] is not None
    assert len(events) == 1
    assert events[0][0] == SELF_ANALYSIS_MANIFEST_MESSAGE
    access = CorpusAccessPolicy.capture("analyze_only", root)
    expected_manifest, expected_json = build_self_analysis_completion_manifest(
        run={
            "run_id": run_id,
            "run_kind": "self_analysis",
            "status": "completed",
            "corpus_access_mode": "analyze_only",
            "root": str(access.root),
            "root_identity": {
                "device_id_hex": access.root_device_id_hex,
                "file_id_hex": access.root_file_id_hex,
                "birthtime_ns": access.root_birthtime_ns,
            },
            "state_directory": str(state_directory),
        },
        inventory={
            "scan_id": 7,
            "mode": "full",
            "attempts": 1,
            "reconciliation_records": 0,
            "journal": {
                "status": "available",
                "volume": cursor.volume,
                "journal_id": str(cursor.journal_id),
                "start_usn": cursor.next_usn,
                "end_usn": cursor.next_usn,
            },
        },
        inventory_policy=inventory_policy,
        code_processing_signature=_CODE_SIGNATURE,
        code_summary={
            "elapsed_ns": 5,
            "processing_signature": _CODE_SIGNATURE,
            "candidates": 1,
            "processed": 1,
            "errors": 0,
        },
        safety_counts={
            "route_candidates": 0,
            "file_actions": 0,
            "run_actions": 0,
            "organization_events": 0,
        },
        commands=_commands(root, state_directory),
    )
    assert manifest == expected_manifest
    assert events[0][1] == expected_json
    assert json.loads(events[0][1]) == expected_manifest


def test_self_analysis_completion_rejects_mismatched_code_signature(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, cursor, inventory_policy = _ready_run(
            state,
            root,
            state_directory,
        )
        with pytest.raises(ValueError, match="does not match its route summary"):
            state.complete_self_analysis_run(
                run_id,
                cursor,
                inventory_policy=inventory_policy,
                code_processing_signature="code-v2:different",
                commands=_commands(root, state_directory),
            )
        assert state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone() == ("running", None)
        assert state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase=?",
            (run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "violation",
    ("route_candidate", "file_action", "run_action", "organization", "extra_route"),
)
def test_self_analysis_completion_rejects_nonzero_or_extra_work(
    tmp_path: Path,
    violation: str,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id, cursor, inventory_policy = _ready_run(state, root, state_directory)
        if violation == "route_candidate":
            state.store_route_candidates(
                run_id,
                (("text/plain", FileSnapshot(str(root / "x.py"), 1, 2, 3, 4, 5)),),
            )
        elif violation == "file_action":
            access = CorpusAccessPolicy.capture("analyze_only", root)
            with state._connection:
                state._connection.execute(
                    """INSERT INTO file_actions(
                    run_id,action_type,source_path,apply_requested,status,started_ns,
                    corpus_access_mode,protected_root,protected_root_device_id_hex,
                    protected_root_file_id_hex,protected_root_birthtime_ns)
                    VALUES(?,'fixture',?,0,'started',1,'analyze_only',?,?,?,?)""",
                    (
                        run_id,
                        str(root / "x.py"),
                        str(access.root),
                        access.root_device_id_hex,
                        access.root_file_id_hex,
                        access.root_birthtime_ns,
                    ),
                )
        elif violation == "run_action":
            state.store_action_summary(run_id, ActionSummary(False))
        elif violation == "organization":
            state.record_event(
                run_id,
                "info",
                "document-organization-plan",
                "forbidden organization evidence",
                None,
            )
        else:
            state.begin_route_runs(run_id, ("pdf",))
            state.complete_route_run(run_id, "pdf", {"processed": 0})

        with pytest.raises(ValueError):
            _complete(
                state,
                run_id,
                cursor,
                inventory_policy,
                root,
                state_directory,
            )
        row = state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        manifest_count = state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase=?",
            (run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchone()[0]
        assert row == ("running", None)
        assert manifest_count == 0


def test_manifest_insert_fault_rolls_back_and_allows_retry(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"

    with FrameworkState(database) as state:
        run_id, cursor, inventory_policy = _ready_run(state, root, state_directory)
        with state._connection:
            state._connection.execute(
                f"""CREATE TRIGGER deny_self_analysis_manifest
                BEFORE INSERT ON run_events
                WHEN NEW.phase='{SELF_ANALYSIS_MANIFEST_PHASE}'
                BEGIN
                    SELECT RAISE(ABORT, 'injected manifest failure');
                END"""
            )
        statements: list[str] = []
        state._connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="injected manifest failure",
            ):
                _complete(
                    state,
                    run_id,
                    cursor,
                    inventory_policy,
                    root,
                    state_directory,
                )
        finally:
            state._connection.set_trace_callback(None)
        assert not state._connection.in_transaction
        assert state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone() == ("running", None)
        assert state._connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND phase=?",
            (run_id, SELF_ANALYSIS_MANIFEST_PHASE),
        ).fetchone() == (0,)
        transaction_statements = [statement.strip() for statement in statements]
        assert transaction_statements.count("BEGIN IMMEDIATE") == 1
        assert transaction_statements.count("ROLLBACK") == 1
        assert "COMMIT" not in transaction_statements

        with state._connection:
            state._connection.execute("DROP TRIGGER deny_self_analysis_manifest")
        manifest = _complete(
            state,
            run_id,
            cursor,
            inventory_policy,
            root,
            state_directory,
        )
        assert manifest["run"]["status"] == "completed"


def test_generic_finalizers_cannot_complete_self_analysis(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    with FrameworkState(state_directory / "framework.sqlite3") as state:
        run_id, cursor, _inventory_policy = _ready_run(state, root, state_directory)
        with pytest.raises(RuntimeError):
            state.complete_initial_run(run_id, 7, cursor, 0, 1, "full")
        with pytest.raises(RuntimeError):
            state.complete_operational_run(run_id)
        assert state._connection.execute(
            "SELECT status,completed_ns FROM initial_runs WHERE run_id=?",
            (run_id,),
        ).fetchone() == ("running", None)


# endregion [02]
