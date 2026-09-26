"""Knowledge-to-inventory regressions for current empty and malformed plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner, snapshot_path
from neocortex.knowledge import knowledge_search
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths
from tests.test_knowledge_search_inventory_extraction_contract import (
    _candidate,
    _publication,
    _snapshot,
)


def _plan_fixture(tmp_path: Path, *, corruption: str | None = None):
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    paths.inventory.parent.mkdir(parents=True)
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    with DedupIndex(paths.inventory) as index:
        initial = index.scan(root, excluded_paths=())
        DedupPlanner(index).plan(initial.scan_id, exact_compare=True, preview_limit=None)
        if corruption == "member_path":
            with index._connection:
                index._connection.execute(
                    "UPDATE planned_duplicate_members SET path=path||'.mismatch'"
                )
        elif corruption == "fingerprint":
            with index._connection:
                index._connection.execute(
                    "UPDATE planned_duplicate_groups SET full_fingerprint='bad'"
                )
        summary = index._connection.execute(
            """SELECT completed_ns,group_count,redundant_files,reclaimable_bytes
            FROM duplicate_plan_summaries WHERE scan_id=?""",
            (initial.scan_id,),
        ).fetchone()
        assert summary is not None
        identity = (*snapshot_path(first).identity, snapshot_path(first).birthtime_ns)
        snapshot = _snapshot(
            _publication(
                initial.scan_id,
                f"duplicate-plan-v1:{summary[0]}:{summary[1]}"
                f":{summary[2]}:{summary[3]}",
                scope_suffix="-fixture",
            )
        )
        candidate = _candidate(identity, marker="inventory-plan")
    return paths, snapshot, {"fts_text": (candidate,)}, candidate


def test_current_empty_plan_is_complete_without_historical_member_leakage(
    tmp_path: Path,
) -> None:
    paths = KnowledgeStatePaths.from_directory(tmp_path / "state")
    paths.inventory.parent.mkdir(parents=True)
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    with DedupIndex(paths.inventory) as index:
        initial = index.scan(root, excluded_paths=())
        DedupPlanner(index).plan(initial.scan_id, exact_compare=True, preview_limit=None)
        second.unlink()
        current = index.scan(root, excluded_paths=())
        DedupPlanner(index).plan(current.scan_id, exact_compare=True, preview_limit=None)
        summary = index._connection.execute(
            """SELECT completed_ns,group_count,redundant_files,reclaimable_bytes
            FROM duplicate_plan_summaries WHERE scan_id=?""",
            (current.scan_id,),
        ).fetchone()
        assert summary is not None
        candidate = _candidate(
            (*snapshot_path(first).identity, snapshot_path(first).birthtime_ns),
            marker="empty-current-plan",
        )
        snapshot = _snapshot(
            _publication(
                current.scan_id,
                    f"duplicate-plan-v1:{summary[0]}:{summary[1]}"
                    f":{summary[2]}:{summary[3]}",
                scope_suffix="-fixture",
            )
        )

    updated, report = knowledge_search._apply_inventory_dispositions(
        paths, snapshot, {"fts_text": (candidate,)}
    )
    assert updated == {"fts_text": (candidate,)}
    assert report.executed and report.available and report.complete
    assert report.reason is None
    assert report.returned == 0
    assert report.rows_scanned == 1
    assert candidate.warnings == ("existing_warning",)


@pytest.mark.parametrize("corruption", ("fingerprint", "member_path"))
def test_nonempty_plan_corruption_remains_partial(
    tmp_path: Path,
    corruption: str,
) -> None:
    paths, snapshot, rankings, _unused_candidate = _plan_fixture(
        tmp_path, corruption=corruption
    )
    updated, report = knowledge_search._apply_inventory_dispositions(
        paths, snapshot, rankings
    )
    assert "inventory_duplicate_plan_ambiguous" in updated["fts_text"][0].warnings
    assert not report.complete
    assert report.reason == "invalid_or_conflicting_duplicate_plan"
    assert report.rows_scanned == 1
