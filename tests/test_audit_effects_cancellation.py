"""Cancellation checks for action-ledger frontiers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.runtime.models import ActionSummary
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.mutations import BackendOutcome
from tests.internal_paths_test_support import begin_signed_normal_run


class _CancellingIndividualBackend:
    name = "fixture-cancelling-individual"

    def __init__(self) -> None:
        self.calls = 0

    def apply_snapshot(self, *_args: object, **_kwargs: object) -> BackendOutcome:
        self.calls += 1
        raise CancellationRequested("fixture operator cancellation")


class _AppliedBatchBackend:
    name = "fixture-applied-batch"

    def apply_many_snapshots(
        self,
        items: tuple[tuple[object, str], ...],
        *,
        root: Path,
    ) -> tuple[BackendOutcome, ...]:
        del root
        return tuple(
            BackendOutcome(
                "applied",
                "fixture_batch_applied",
                receipt_json='{"schema":"fixture-receipt"}',
            )
            for _item in items
        )


class _BlockedIndividualBackend:
    name = "fixture-blocked-individual"

    def apply_snapshot(self, *_args: object, **_kwargs: object) -> BackendOutcome:
        return BackendOutcome("blocked", "fixture_blocked", "fixture preflight block")


def test_cancellation_closes_uninvoked_trash_intents(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    paths = (root / "first.bin", root / "second.bin", root / "keeper.bin")
    for path in paths:
        path.write_bytes(b"same payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        backend = _CancellingIndividualBackend()
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=backend,  # type: ignore[arg-type]
        )

        with pytest.raises(CancellationRequested):
            actions._trash_duplicates(plan, ActionSummary(apply_actions=True))

        rows = state._connection.execute(
            "SELECT status,detail FROM file_actions WHERE run_id=? ORDER BY source_path",
            (run_id,),
        ).fetchall()

    assert backend.calls == 1
    assert {row[0] for row in rows} == {"recovery_required", "skipped"}
    assert any(row[1] == "kio_cancelled_before_effect" for row in rows)
    assert all(path.exists() for path in paths)


def test_cancelled_uninvoked_intents_fallback_to_recovery_on_skip_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    paths = (root / "first.bin", root / "second.bin", root / "keeper.bin")
    for path in paths:
        path.write_bytes(b"same payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_CancellingIndividualBackend(),  # type: ignore[arg-type]
        )

        with (
            patch.object(
                state,
                "finish_file_actions",
                side_effect=RuntimeError("fixture skip persistence fault"),
            ),
            pytest.raises(CancellationRequested),
        ):
            actions._trash_duplicates(plan, ActionSummary(apply_actions=True))

        rows = state._connection.execute(
            "SELECT status,detail FROM file_actions WHERE run_id=? ORDER BY source_path",
            (run_id,),
        ).fetchall()

    assert {row[0] for row in rows} == {"recovery_required"}
    assert any("terminal skip persistence failed" in str(row[1]) for row in rows)


def test_batch_confirmation_cancellation_marks_every_member_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    paths = (root / "first.bin", root / "second.bin", root / "keeper.bin")
    for path in paths:
        path.write_bytes(b"same payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_AppliedBatchBackend(),  # type: ignore[arg-type]
        )

        with (
            patch.object(actions, "_validate_trash_receipt", return_value=None),
            patch.object(
                state,
                "confirm_file_actions_applied",
                side_effect=CancellationRequested("fixture confirmation cancellation"),
            ),
            pytest.raises(CancellationRequested),
        ):
            actions._trash_duplicates(plan, ActionSummary(apply_actions=True))

        statuses = state._connection.execute(
            "SELECT status FROM file_actions WHERE run_id=? ORDER BY source_path",
            (run_id,),
        ).fetchall()

    assert statuses
    assert {row[0] for row in statuses} == {"recovery_required"}


def test_blocked_outcome_persistence_fault_does_not_leave_applying_intent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()
    paths = (root / "first.bin", root / "second.bin", root / "keeper.bin")
    for path in paths:
        path.write_bytes(b"same payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_BlockedIndividualBackend(),  # type: ignore[arg-type]
        )

        with patch.object(
            state,
            "finish_file_action",
            side_effect=RuntimeError("fixture skip persistence fault"),
        ):
            summary = actions._trash_duplicates(plan, ActionSummary(apply_actions=True))

        statuses = state._connection.execute(
            "SELECT status FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchall()

    assert summary.errors == 2
    assert {row[0] for row in statuses} == {"recovery_required"}
