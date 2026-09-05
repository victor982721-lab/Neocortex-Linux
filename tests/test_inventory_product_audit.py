"""Bounded Linux product regressions for inventory cursor publication."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.deduplication import (
    DedupIndex,
    InventoryScanBudgetExceeded,
    InventoryScanCancelled,
    InventoryWorkBudget,
)
from neocortex.deduplication.inventory.resume import (
    InventoryResumeCheckpointStore,
    InventoryResumeConflictError,
)


@pytest.mark.parametrize("batch_size", (1, 5))
@pytest.mark.parametrize("tail", ("directory", "empty_directory", "symlink", "excluded"))
def test_interrupted_batch_records_only_evidence_through_its_file_cursor(
    tmp_path: Path, batch_size: int, tail: str
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"first")
    exclusions: tuple[Path, ...] = ()
    if tail == "directory":
        (root / "m").mkdir()
        (root / "m" / "z").write_bytes(b"last")
    else:
        (root / "z").write_bytes(b"last")
        if tail == "empty_directory":
            (root / "m" / "empty").mkdir(parents=True)
        elif tail == "symlink":
            (root / "m").symlink_to(tmp_path / "outside")
        else:
            (root / "m").mkdir()
            exclusions = (root / "m",)

    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        with pytest.raises(InventoryScanBudgetExceeded):
            index.scan(
                root,
                excluded_paths=exclusions,
                batch_size=batch_size,
                checkpoint_path=checkpoint,
                work_budget=InventoryWorkBudget(max_files=1),
            )
        partial = InventoryResumeCheckpointStore(checkpoint, root=root).read()
        assert partial.last_relative_cursor == "a"
        assert partial.files_seen == 1
        assert partial.directories_seen == 1
        assert partial.skipped_links == 0
        assert partial.excluded_directories == 0

        resumed = index.scan(
            root,
            excluded_paths=exclusions,
            batch_size=batch_size,
            checkpoint_path=checkpoint,
            resume=True,
        )
        rows = tuple(index.snapshots(resumed.scan_id))
        assert resumed.files_seen == len(rows) == 2
        assert resumed.bytes_seen == sum(row.size for row in rows) == 9
        checkpoint_bytes = checkpoint.read_bytes()
        for _ in range(2):
            assert index.scan(
                root,
                excluded_paths=exclusions,
                checkpoint_path=checkpoint,
                resume=True,
            ) == resumed
            assert checkpoint.read_bytes() == checkpoint_bytes
            assert tuple(index.snapshots(resumed.scan_id)) == rows


@pytest.mark.parametrize("drift", ("extra_row", "summary_count", "summary_directories"))
def test_complete_checkpoint_rejects_inconsistent_scan_owner_without_mutation(
    tmp_path: Path, drift: str
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"first")
    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        scan = index.scan(root, excluded_paths=(), checkpoint_path=checkpoint)
        if drift == "extra_row":
            index._connection.execute(
                "INSERT INTO files(path,volume_id,file_id,size,mtime_ns,birthtime_ns,scan_id) "
                "SELECT ?,volume_id,file_id,size,mtime_ns,birthtime_ns,scan_id "
                "FROM files WHERE scan_id=?",
                (str(root / "unobserved"), scan.scan_id),
            )
        elif drift == "summary_count":
            index._connection.execute(
                "UPDATE scans SET files_seen=files_seen+1 WHERE scan_id=?", (scan.scan_id,)
            )
        else:
            index._connection.execute(
                "UPDATE scans SET directories_seen=directories_seen+1 WHERE scan_id=?",
                (scan.scan_id,),
            )
        index._connection.commit()
        changed = index._connection.total_changes
        checkpoint_bytes = checkpoint.read_bytes()
        with pytest.raises(InventoryResumeConflictError, match=r"complete.*owner"):
            index.scan(root, excluded_paths=(), checkpoint_path=checkpoint, resume=True)
        assert index._connection.total_changes == changed
        assert checkpoint.read_bytes() == checkpoint_bytes


def test_complete_checkpoint_owner_reconciliation_honors_cancellation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a").write_bytes(b"first")
    checkpoint = tmp_path / "state" / "inventory.json"
    with DedupIndex(tmp_path / "index.sqlite3") as index:
        index.scan(root, excluded_paths=(), checkpoint_path=checkpoint)
        changed = index._connection.total_changes
        checkpoint_bytes = checkpoint.read_bytes()
        reading_owner_rows = False

        def trace(statement: str) -> None:
            nonlocal reading_owner_rows
            if statement.startswith("SELECT size FROM files WHERE scan_id="):
                reading_owner_rows = True

        index._connection.set_trace_callback(trace)
        try:
            with pytest.raises(InventoryScanCancelled):
                index.scan(
                    root,
                    excluded_paths=(),
                    checkpoint_path=checkpoint,
                    resume=True,
                    work_budget=InventoryWorkBudget(
                        cancellation_check=lambda: reading_owner_rows,
                    ),
                )
        finally:
            index._connection.set_trace_callback(None)
        assert reading_owner_rows
        assert index._connection.total_changes == changed
        assert checkpoint.read_bytes() == checkpoint_bytes
