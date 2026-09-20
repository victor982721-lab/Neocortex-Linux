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
