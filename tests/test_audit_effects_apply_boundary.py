"""Public apply-boundary regressions for Trash receipts."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.actions.file_action_recovery import effect_receipt_json
from neocortex.workflow.mutations import BackendOutcome
from tests.internal_paths_test_support import begin_signed_normal_run


class _GenericIndividualTrashBackend:
    name = "fixture-generic-individual-trash"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root

    def apply_snapshot(
        self,
        snapshot,
        *,
        root: Path,
        source_digest: str,
    ) -> BackendOutcome:  # type: ignore[no-untyped-def]
        del root, source_digest
        destination = self.trash_root / "files" / Path(snapshot.path).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        Path(snapshot.path).rename(destination)
        return BackendOutcome(
            "applied",
            "fixture_generic_applied",
            receipt_json=effect_receipt_json(
                operation="trash",
                source_path=snapshot.path,
                target_path=None,
            ),
        )


class _GenericBatchTrashBackend:
    name = "fixture-generic-batch-trash"

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root

    def apply_many_snapshots(
        self,
        items,
        *,
        root: Path,
    ) -> tuple[BackendOutcome, ...]:  # type: ignore[no-untyped-def]
        del root
        outcomes: list[BackendOutcome] = []
        for snapshot, _source_digest in items:
            destination = self.trash_root / "files" / Path(snapshot.path).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            Path(snapshot.path).rename(destination)
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_generic_applied",
                    receipt_json=effect_receipt_json(
                        operation="trash",
                        source_path=snapshot.path,
                        target_path=None,
                    ),
                )
            )
        return tuple(outcomes)


@pytest.mark.parametrize(
    "action_type",
    ("trash_empty_file", "trash_duplicate", "trash_redlist", "trash_unrecoverable_pdf"),
)
def test_generic_individual_applied_receipt_requires_recovery(
    tmp_path: Path,
    action_type: str,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    trash_root = tmp_path / "Trash"
    root.mkdir()
    state_directory.mkdir()
    source = root / "candidate.bin"
    source.write_bytes(b"payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_GenericIndividualTrashBackend(trash_root),  # type: ignore[arg-type]
        )
        result = actions._apply_trash_batch(
            action_type,
            ((str(source), "fixture"),),
            expected_snapshots=(snapshot_path(source),),
        )
        status = state._connection.execute(
            "SELECT status FROM file_actions WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]

    assert result == (0, 1, 0)
    assert status == "recovery_required"
    assert not source.exists()


def test_generic_batch_applied_receipts_require_recovery_before_reconcile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    trash_root = tmp_path / "Trash"
    root.mkdir()
    state_directory.mkdir()
    sources = (root / "first.bin", root / "second.bin")
    for source in sources:
        source.write_bytes(b"payload")
    database = state_directory / "framework.sqlite3"

    with (
        DedupIndex(state_directory / "dedup.sqlite3") as index,
        FrameworkState(database) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        actions = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_GenericBatchTrashBackend(trash_root),  # type: ignore[arg-type]
        )
        result = actions._apply_trash_batch(
            "trash_duplicate",
            tuple((str(source), "fixture") for source in sources),
            expected_snapshots=tuple(snapshot_path(source) for source in sources),
        )
        statuses = state._connection.execute(
            "SELECT status FROM file_actions WHERE run_id=? ORDER BY source_path",
            (run_id,),
        ).fetchall()

    assert result == (0, 2, 0)
    assert [row[0] for row in statuses] == ["recovery_required", "recovery_required"]
    assert all(not source.exists() for source in sources)
