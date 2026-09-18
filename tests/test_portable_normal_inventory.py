"""Portable normal-inventory integration without an NTFS USN dependency."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.enumeration import UnsupportedPlatformError
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.api.public import FrameworkConfig, FrameworkOrchestrator
from neocortex.integrations.inventory import inventory_coordinator as inventory_module
from neocortex.runtime.orchestration import orchestrator as orchestrator_module
from tests.synthetic_usn import SyntheticUsnJournal


def _journal_unavailable(_volume: str | Path):
    raise UnsupportedPlatformError("portable fixture has no USN journal")


@contextmanager
def _without_usn() -> Iterator[None]:
    with (
        patch.object(
            orchestrator_module,
            "query_journal_cursor",
            _journal_unavailable,
        ),
        patch.object(
            inventory_module,
            "query_journal_cursor",
            _journal_unavailable,
        ),
    ):
        yield


def _run(root: Path, state: Path, *, route: str = "none"):
    with _without_usn():
        return FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=state,
                route=route,
                code_candidate_scope="broad",
                document_catalog_enabled=False,
                global_memory_budget_bytes=256 * 1024**2,
                global_min_free_memory_bytes=128 * 1024**2,
                global_min_free_commit_bytes=128 * 1024**2,
            )
        ).run_initial()


def _snapshot(state: Path, root: Path, scan_id: int) -> dict[str, tuple[int, ...]]:
    with DedupIndex(state / "dedup.sqlite3") as index:
        return {
            str(Path(item.path).relative_to(root)): (
                item.volume_id,
                item.file_id,
                item.size,
                item.mtime_ns,
                item.birthtime_ns,
            )
            for item in index.snapshots(scan_id)
        }


def test_portable_normal_run_is_published_and_code_remains_incremental(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "portable-state"
    root.mkdir()
    for index in range(20):
        (root / f"module_{index:02d}.py").write_text(
            f"VALUE_{index} = {index}\n",
            encoding="utf-8",
        )

    first = _run(root, state, route="code")
    second = _run(root, state, route="code")

    assert first.inventory_mode == second.inventory_mode == "full"
    assert first.inventory_attempts == second.inventory_attempts == 1
    assert first.scan.scan_id == second.scan.scan_id
    assert first.reconciliation_records == second.reconciliation_records == 0
    assert first.journal_before is first.journal_after is None
    assert second.journal_before is second.journal_after is None
    assert first.journal_usn_span is second.journal_usn_span is None
    assert first.code is not None and second.code is not None
    assert (first.code.processed, first.code.cache_hits) == (20, 0)
    assert (second.code.processed, second.code.cache_hits) == (0, 20)

    (root / "module_00.py").write_text("VALUE_0 = 1000\n", encoding="utf-8")
    (root / "module_01.py").unlink()
    (root / "module_02.py").rename(root / "renamed_module.py")
    (root / "added_module.py").write_text("ADDED = True\n", encoding="utf-8")

    changed = _run(root, state, route="code")
    replay = _run(root, state, route="code")

    assert changed.code is not None and replay.code is not None
    assert changed.scan.files_seen == replay.scan.files_seen == 20
    assert changed.scan.scan_id != first.scan.scan_id
    assert changed.scan.scan_id == replay.scan.scan_id
    assert (changed.code.processed, changed.code.cache_hits) == (3, 17)
    assert (replay.code.processed, replay.code.cache_hits) == (0, 20)
    assert _snapshot(state, root, changed.scan.scan_id) == _snapshot(
        state,
        root,
        replay.scan.scan_id,
    )

    with DedupIndex(state / "dedup.sqlite3") as index:
        checkpoint = index.inventory_checkpoint(root)
        published = list(index.published_snapshots(root))
    assert checkpoint is not None
    assert checkpoint.scan_id == replay.scan.scan_id
    assert checkpoint.valid and not checkpoint.journal_available
    assert len(published) == 20

    with sqlite3.connect(state / "framework.sqlite3") as connection:
        latest = connection.execute(
            """SELECT journal_volume,journal_id,start_usn,end_usn,status
            FROM initial_runs ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        gate = connection.execute(
            """SELECT details_json FROM run_events
            WHERE phase='normal-incremental-gate'
            ORDER BY event_id DESC LIMIT 1"""
        ).fetchone()
    assert latest == (None, None, None, None, "completed")
    assert gate is not None
    assert "journal_unavailable_portable_full_scan" in str(gate[0])


def test_portable_snapshot_matches_the_usn_inventory_for_the_same_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    portable_state = tmp_path / "portable-state"
    usn_state = tmp_path / "usn-state"
    root.mkdir()
    for index in range(25):
        (root / f"item_{index:02d}.txt").write_text(
            f"contenido {index}\n",
            encoding="utf-8",
        )

    portable = _run(root, portable_state)
    with SyntheticUsnJournal(root):
        usn_orchestrator = FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=usn_state,
                document_catalog_enabled=False,
                global_memory_budget_bytes=256 * 1024**2,
                global_min_free_memory_bytes=128 * 1024**2,
                global_min_free_commit_bytes=128 * 1024**2,
            )
        )
        usn = usn_orchestrator.run_initial()
        usn_replay = usn_orchestrator.run_initial()

    assert _snapshot(portable_state, root, portable.scan.scan_id) == _snapshot(
        usn_state,
        root,
        usn.scan.scan_id,
    )
    with DedupIndex(portable_state / "dedup.sqlite3") as portable_index:
        portable_checkpoint = portable_index.inventory_checkpoint(root)
    with DedupIndex(usn_state / "dedup.sqlite3") as usn_index:
        usn_checkpoint = usn_index.inventory_checkpoint(root)
    assert portable_checkpoint is not None and usn_checkpoint is not None
    assert not portable_checkpoint.journal_available
    assert usn_checkpoint.journal_available is (os.name == "nt")
    # A first publication is a complete inventory on every platform.  The
    # durable USN cursor accelerates only a subsequent compatible run.
    assert usn.inventory_mode == "full"
    assert usn_replay.inventory_mode == ("incremental" if os.name == "nt" else "full")


def test_portable_run_recovers_after_an_interruption(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    originals = {}
    for index in range(20):
        path = root / f"item_{index:02d}.bin"
        payload = f"payload-{index}".encode()
        path.write_bytes(payload)
        originals[path.name] = payload

    def interrupt_plan(_planner, *_args, **_kwargs):
        raise KeyboardInterrupt

    with (
        _without_usn(),
        patch.object(DedupPlanner, "plan", interrupt_plan),
        pytest.raises(KeyboardInterrupt),
    ):
        FrameworkOrchestrator(
            FrameworkConfig(
                root=root,
                state_directory=state,
                document_catalog_enabled=False,
                global_memory_budget_bytes=256 * 1024**2,
                global_min_free_memory_bytes=128 * 1024**2,
                global_min_free_commit_bytes=128 * 1024**2,
            )
        ).run_initial()

    recovered = _run(root, state)

    assert recovered.scan.files_seen == 20
    assert recovered.journal_before is recovered.journal_after is None
    assert {path.name: path.read_bytes() for path in root.iterdir()} == originals
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        statuses = [
            str(row[0])
            for row in connection.execute("SELECT status FROM initial_runs ORDER BY run_id")
        ]
    assert statuses == ["cancelled", "completed"]
    with DedupIndex(state / "dedup.sqlite3") as index:
        checkpoint = index.inventory_checkpoint(root)
    assert checkpoint is not None
    assert checkpoint.scan_id == recovered.scan.scan_id
    assert not checkpoint.journal_available
