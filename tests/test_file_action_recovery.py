"""Regression coverage for uncertain file-action reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest

import neocortex.persistence.framework_schema as framework_schema
from neocortex.deduplication import DedupIndex, DedupPlanner, snapshot_path
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.api.cli.cli_app import main as cli_main
from neocortex.workflow.actions.file_action_recovery import (
    expected_identity_json,
    list_file_action_reconciliations,
)
from neocortex.persistence.framework_schema import (
    SCHEMA_VERSION,
    initialize_framework_schema,
)
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import begin_signed_normal_run
from tests.mutation_containment import ContainedMutationRoot


@pytest.fixture
def mutation_containment(tmp_path: Path) -> Iterator[ContainedMutationRoot]:
    """Confine every native mutation in this module to one canonical root."""

    base = tmp_path / "native-mutation-roots"
    base.mkdir()
    containment = ContainedMutationRoot.create(base, watch_directories=(base,))
    yield containment
    containment.assert_no_leaks()


def _begin_normal_run(state: FrameworkState, root: Path) -> int:
    return begin_signed_normal_run(state, root)


# region [01] Schema migration and transition history


def _create_version_17_database(
    database: Path,
    *,
    extra_schema: str = "",
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        for statement in framework_schema._TABLE_STATEMENTS:
            if "CREATE TABLE IF NOT EXISTS initial_runs (" in statement:
                continue
            if "CREATE TABLE IF NOT EXISTS file_actions (" in statement:
                continue
            if "CREATE TABLE IF NOT EXISTS file_action_events (" in statement:
                continue
            if "CREATE TABLE IF NOT EXISTS file_action_reconciliation_events (" in statement:
                continue
            connection.execute(statement)
        connection.execute(
            """CREATE TABLE initial_runs (
            run_id INTEGER PRIMARY KEY,
            root TEXT NOT NULL,
            started_ns INTEGER NOT NULL,
            completed_ns INTEGER,
            status TEXT NOT NULL,
            run_kind TEXT NOT NULL DEFAULT 'initial',
            source_run_id INTEGER,
            current_phase TEXT,
            owner_pid INTEGER,
            heartbeat_ns INTEGER,
            scan_id INTEGER,
            journal_volume TEXT,
            journal_id TEXT,
            start_usn INTEGER,
            end_usn INTEGER,
            reconciliation_records INTEGER,
            inventory_attempts INTEGER,
            inventory_mode TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE file_actions (
            action_id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            target_path TEXT,
            detected_mime TEXT,
            evidence TEXT,
            apply_requested INTEGER NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            started_ns INTEGER NOT NULL,
            completed_ns INTEGER
            )"""
        )
        for statement in framework_schema._INDEX_STATEMENTS:
            if any(
                name in statement
                for name in (
                    "file_actions_idempotency_key_idx",
                    "file_actions_recovery_idx",
                    "file_action_events_action_idx",
                    "file_action_reconciliation_events_action_idx",
                )
            ):
                continue
            connection.execute(statement)
        for statement in framework_schema._TRIGGER_STATEMENTS:
            if any(
                name in statement
                for name in (
                    "file_action_events_",
                    "file_action_reconciliation_events_",
                    "initial_runs_corpus_policy_",
                    "file_actions_corpus_policy_",
                )
            ):
                continue
            connection.execute(statement)
        connection.execute("INSERT INTO metadata VALUES('schema_version','17')")
        connection.execute(
            """INSERT INTO initial_runs(run_id,root,started_ns,status)
            VALUES(7,'legacy-root',100,'failed')"""
        )
        connection.execute(
            """INSERT INTO file_actions VALUES(
            41,7,'trash_duplicate','legacy-source',NULL,NULL,'legacy-evidence',
            1,'recovery_required','legacy-detail',123,NULL)"""
        )
        if extra_schema:
            connection.executescript(extra_schema)
        connection.commit()


def test_current_schema_migrates_version_17_without_reinterpreting_legacy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_17_database(database)

    with FrameworkState(database):
        pass
    with FrameworkState(database):
        pass

    with closing(sqlite3.connect(database)) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        row = connection.execute(
            """SELECT action_id,run_id,action_type,source_path,status,detail,
            idempotency_key,expected_identity_json,effect_receipt_json,applying_ns,
            corpus_access_mode,protected_root,protected_root_device_id_hex,
            protected_root_file_id_hex,protected_root_birthtime_ns
            FROM file_actions"""
        ).fetchone()
        run_policy = connection.execute(
            """SELECT corpus_access_mode,root_device_id_hex,root_file_id_hex,
            root_birthtime_ns,state_directory,inventory_policy_signature
            FROM initial_runs WHERE run_id=7"""
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM file_action_events").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == str(SCHEMA_VERSION) == "22"
    assert row == (
        41,
        7,
        "trash_duplicate",
        "legacy-source",
        "recovery_required",
        "legacy-detail",
        None,
        None,
        None,
        None,
        "normal",
        None,
        None,
        None,
        None,
    )
    assert run_policy == ("normal", None, None, None, None, None)
    assert event_count == 0
    assert integrity == "ok"
    assert foreign_keys == []


@pytest.mark.parametrize(
    ("extra_schema", "unknown_object"),
    (
        ("ALTER TABLE file_actions ADD COLUMN owner_extension TEXT;", None),
        (
            "CREATE TABLE owner_action_extension(value TEXT);",
            "owner_action_extension",
        ),
        (
            "CREATE INDEX owner_action_index ON file_actions(status);",
            "owner_action_index",
        ),
        (
            """CREATE TRIGGER owner_action_trigger AFTER INSERT ON file_actions
            BEGIN SELECT 1; END;""",
            "owner_action_trigger",
        ),
    ),
)
def test_current_schema_abstains_on_unknown_version_17_objects(
    tmp_path: Path,
    extra_schema: str,
    unknown_object: str | None,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_17_database(database, extra_schema=extra_schema)

    with pytest.raises(
        RuntimeError,
        match=r"initialization from version 17 failed|schema contract validation failed",
    ):
        FrameworkState(database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("17",)
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='file_action_events'"
            ).fetchone()
            is None
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_actions)")}
        if unknown_object is not None:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (unknown_object,)
            ).fetchone() == (unknown_object,)
    assert "idempotency_key" not in columns


def test_current_schema_rolls_back_base_exception_from_version_17(
    tmp_path: Path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    _create_version_17_database(database)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(KeyboardInterrupt):
            initialize_framework_schema(
                connection,
                lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("17",)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_actions)")}
        assert "idempotency_key" not in columns
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='file_action_events'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_file_action_transitions_are_cas_traced_and_events_are_append_only(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    root = tmp_path / "corpus"
    state_directory.mkdir()
    root.mkdir()
    database = state_directory / "framework.sqlite3"
    source = root / "source.bin"
    source.write_bytes(b"payload")
    with FrameworkState(database) as state:
        run_id = _begin_normal_run(state, root)
        action_id = state.begin_file_action(
            run_id,
            "correct_extension",
            str(source),
            str(root / "source.dat"),
            None,
            None,
            True,
        )
        duplicate_id = state.begin_file_action(
            run_id,
            "correct_extension",
            str(source),
            str(root / "source.dat"),
            None,
            None,
            True,
        )
        assert duplicate_id == action_id
        identity = expected_identity_json(
            snapshot_path(source),
            source_path=str(source),
            target_path=str(root / "source.dat"),
        )
        state.mark_file_actions_applying(((action_id, identity),))
        with pytest.raises(RuntimeError, match="applying -> failed"):
            state.finish_file_action(action_id, "failed", "post-frontier failure")
        competing = FrameworkRouteState(database)
        with pytest.raises(RuntimeError, match="cannot enter applying"):
            competing.mark_file_actions_applying(((action_id, identity),))
        state.require_file_action_recovery((action_id,), "uncertain effect")
        state.require_file_action_recovery((action_id,), "uncertain effect")

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """SELECT status,completed_ns,idempotency_key,expected_identity_json
            FROM file_actions WHERE action_id=?""",
            (action_id,),
        ).fetchone()
        events = connection.execute(
            """SELECT from_status,to_status,stage FROM file_action_events
            WHERE action_id=? ORDER BY event_id""",
            (action_id,),
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE file_action_events SET detail='forbidden' WHERE action_id=?",
                (action_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM file_action_events WHERE action_id=?", (action_id,))

    assert row[0] == "recovery_required"
    assert row[1] is None
    assert len(str(row[2])) == 32
    assert json.loads(str(row[3]))["schema_version"] == 1
    assert events == [
        (None, "started", "intent_recorded"),
        ("started", "applying", "mutation_frontier"),
        ("applying", "recovery_required", "recovery_required"),
    ]


# endregion [01]


# region [02] Read-only reconciliation and CLI


def _uncertain_action(
    state: FrameworkState,
    run_id: int,
    action_type: str,
    source: Path,
    target: Path | None,
) -> int:
    snapshot = snapshot_path(source)
    action_id = state.begin_file_action(
        run_id,
        action_type,
        str(source),
        None if target is None else str(target),
        None,
        None,
        True,
    )
    state.mark_file_actions_applying(
        (
            (
                action_id,
                expected_identity_json(
                    snapshot,
                    source_path=str(source),
                    target_path=None if target is None else str(target),
                ),
            ),
        )
    )
    return action_id


def test_reconciler_is_bounded_read_only_and_idempotent(
    mutation_containment: ContainedMutationRoot,
) -> None:
    sandbox = mutation_containment.root
    root = sandbox / "corpus"
    state_directory = sandbox / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database) as state:
        run_id = _begin_normal_run(state, root)

        confirmed_source = root / "confirmed.txt"
        confirmed_target = root / "confirmed.bin"
        confirmed_source.write_bytes(b"confirmed")
        confirmed_id = _uncertain_action(
            state,
            run_id,
            "correct_extension",
            confirmed_source,
            confirmed_target,
        )
        mutation_containment.rename(confirmed_source, confirmed_target)
        state.require_file_action_recovery((confirmed_id,), "receipt write failed")

        untouched_source = root / "untouched.txt"
        untouched_target = root / "untouched.bin"
        untouched_source.write_bytes(b"untouched")
        untouched_id = _uncertain_action(
            state,
            run_id,
            "correct_extension",
            untouched_source,
            untouched_target,
        )
        state.require_file_action_recovery((untouched_id,), "syscall failed")

        conflict_source = root / "conflict.txt"
        conflict_target = root / "conflict.bin"
        conflict_source.write_bytes(b"source")
        conflict_id = _uncertain_action(
            state,
            run_id,
            "correct_extension",
            conflict_source,
            conflict_target,
        )
        conflict_target.write_bytes(b"other object")
        state.require_file_action_recovery((conflict_id,), "target conflict")

        trash_source = root / "trash.bin"
        trash_source.write_bytes(b"trash")
        trash_id = _uncertain_action(
            state,
            run_id,
            "trash_duplicate",
            trash_source,
            None,
        )
        mutation_containment.unlink(trash_source)
        state.require_file_action_recovery((trash_id,), "Recycle Bin return lost")

        legacy_id = state.begin_file_action(
            run_id,
            "trash_duplicate",
            str(root / "legacy.bin"),
            None,
            None,
            None,
            True,
        )
        state.require_file_action_recovery((legacy_id,), "legacy uncertainty")

    before = database.read_bytes()
    first = list_file_action_reconciliations(database, limit=100)
    second = list_file_action_reconciliations(database, limit=100)
    page = list_file_action_reconciliations(database, limit=2)
    continuation = list_file_action_reconciliations(
        database,
        limit=100,
        after_action_id=page[-1].action_id,
    )

    assert first == second
    assert database.read_bytes() == before
    assert len(page) == 2
    assert tuple(page) + tuple(continuation) == first
    assert {item.action_id: item.classification for item in first} == {
        confirmed_id: "confirmed",
        untouched_id: "not_performed",
        conflict_id: "ambiguous",
        trash_id: "ambiguous",
        legacy_id: "impossible_to_check",
    }


def test_action_recovery_cli_json_returns_two_for_ambiguity(
    mutation_containment: ContainedMutationRoot,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sandbox = mutation_containment.root
    root = sandbox / "corpus"
    state_directory = sandbox / "state"
    root.mkdir()
    state_directory.mkdir()
    database = state_directory / "framework.sqlite3"
    source = root / "trash.bin"
    source.write_bytes(b"payload")
    with FrameworkState(database) as state:
        run_id = _begin_normal_run(state, root)
        action_id = _uncertain_action(
            state,
            run_id,
            "trash_duplicate",
            source,
            None,
        )
        mutation_containment.unlink(source)
        state.require_file_action_recovery((action_id,), "receipt lost")

    exit_code = cli_main(
        [
            "--state-directory",
            str(state_directory),
            "--action-recovery-status",
            "--action-recovery-json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["kind"] == "file-action-reconciliation-page"
    assert payload["availability"] == "ready"
    assert payload["returned"] == 1
    assert payload["complete"] is False
    assert payload["items"][0]["classification"] == "ambiguous"
    assert payload["items"][0]["action_id"] == action_id
    legacy_exit = cli_main([
        "--state-directory", str(state_directory), "--action-recovery-status",
        "--action-recovery-json", "--action-recovery-json-lines",
    ])
    legacy = json.loads(capsys.readouterr().out)
    assert legacy_exit == 2
    assert legacy["kind"] == "file-action-reconciliation"
    assert legacy["classification"] == "ambiguous"
    assert legacy["action_id"] == action_id


# endregion [02]


# region [03] Fault injection around the syscall frontier


def test_trash_apply_abstains_before_effect_confirmation(
    mutation_containment: ContainedMutationRoot,
) -> None:
    root = mutation_containment.root
    corpus = root / "corpus"
    corpus.mkdir()
    state_directory = root / "state"
    state_directory.mkdir()
    source = corpus / "candidate.bin"
    source.write_bytes(b"payload")
    with (
        DedupIndex(root / "dedup.sqlite3") as index,
        FrameworkState(state_directory / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        run_id = _begin_normal_run(state, corpus)
        actions = FrameworkActions(index, state, run_id, scan.scan_id, apply=True)

        with (
            patch("neocortex.workflow.actions.actions.send2trash") as trash,
            patch.object(state, "confirm_file_actions_applied") as confirm,
        ):
            result = actions.recycle_verified_files(
                "trash_duplicate",
                ((snapshot_path(source), "fixture"),),
            )
        status, detail = state._connection.execute(
            "SELECT status,detail FROM file_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()

    trash.assert_not_called()
    confirm.assert_not_called()
    assert result == (0, 0, 1)
    assert source.exists()
    assert status == "skipped"
    assert "cannot bind the observed file identity" in detail


@pytest.mark.skipif(os.name != "nt", reason="native mutation backend is Windows-only")
@pytest.mark.parametrize(
    "injected",
    (RuntimeError("receipt write failed"), KeyboardInterrupt()),
)
def test_rename_post_effect_fault_never_becomes_failed(
    mutation_containment: ContainedMutationRoot,
    injected: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = mutation_containment.root
    corpus = root / "corpus"
    corpus.mkdir()
    state_directory = root / "state"
    state_directory.mkdir()
    source = corpus / "image.txt"
    target = corpus / "image.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    original_rename = actions_module.rename_no_replace_by_identity

    def contained_rename(
        source_path: Path,
        destination_path: Path,
        expected: object,
        **kwargs: object,
    ) -> object:
        return mutation_containment.call_rename(
            original_rename,
            source_path,
            destination_path,
            expected,
            **kwargs,
        )

    monkeypatch.setattr(
        actions_module,
        "rename_no_replace_by_identity",
        contained_rename,
    )
    with (
        DedupIndex(root / "dedup.sqlite3") as index,
        FrameworkState(state_directory / "framework.sqlite3") as state,
    ):
        scan = index.scan(corpus)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = _begin_normal_run(state, corpus)
        actions = FrameworkActions(index, state, run_id, scan.scan_id, apply=True)
        context = (
            pytest.raises(type(injected))
            if isinstance(injected, KeyboardInterrupt)
            else _does_not_raise()
        )
        with (
            patch.object(
                state,
                "confirm_file_actions_applied",
                side_effect=injected,
            ),
            context,
        ):
            actions.execute(plan, cleanup_empty_directories=False)
        status = state._connection.execute(
            """SELECT status FROM file_actions
            WHERE action_type='correct_extension'"""
        ).fetchone()[0]

    assert not source.exists()
    assert target.exists()
    assert status == "recovery_required"


class _does_not_raise:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> Literal[False]:
        return False


# endregion [03]
