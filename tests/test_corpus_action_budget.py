"""Action-owner budget gates stay before content inspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run










def test_route_none_reserves_content_prefix_before_type_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    (root / "document.txt").write_text("document\n", encoding="utf-8")
    reservations: list[tuple[str, int, int]] = []

    def reserve(key: str, items: int, bytes_count: int) -> None:
        reservations.append((key, items, bytes_count))
        if "content-prefix" in key:
            raise RunBudgetExceeded("bytes", {"remaining_bytes": 0})

    monkeypatch.setattr(
        "neocortex.workflow.actions.actions.detect_content_type",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("content-type detection ran before its budget gate")
        ),
    )
    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=False,
                reserve_work=reserve,
            )
            with pytest.raises(RunBudgetExceeded):
                runner.execute(plan, cleanup_empty_directories=False)

    prefix_reservations = [
        reservation for reservation in reservations if "content-prefix" in reservation[0]
    ]
    assert prefix_reservations
    assert prefix_reservations[0][1] == 0
    assert prefix_reservations[0][2] >= len("document\n")
