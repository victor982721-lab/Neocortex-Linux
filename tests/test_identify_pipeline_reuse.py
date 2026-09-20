"""Regression coverage for the single Identify pass in FrameworkActions."""

from __future__ import annotations

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def test_route_publication_reuses_identify_decision(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    source = root / "image.txt"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    calls: list[str] = []
    detector = actions_module.detect_content_type

    def counted_detector(path):
        calls.append(str(path))
        return detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)

        identified = runner.identify_and_normalize()
        summary = runner.execute(plan, cleanup_empty_directories=False)
        route_rows = state._connection.execute(
            "SELECT mime,path FROM route_candidates WHERE run_id=?", (run_id,)
        ).fetchall()

    assert calls == [str(source)]
    assert identified.type_cache_misses == 1
    assert summary.type_cache_misses == 1
    assert summary.files_checked == 1
    assert summary.rename_candidates == 1
    assert route_rows == [("image/png", str(source))]


def test_detector_version_change_invalidates_in_memory_identify(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    source = root / "document.txt"
    source.write_text("fixture", encoding="utf-8")

    calls: list[str] = []
    detector = actions_module.detect_content_type

    def counted_detector(path):
        calls.append(str(path))
        return detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(root)
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)
        runner.identify_and_normalize()

        monkeypatch.setattr(actions_module, "DETECTOR_VERSION", "content-types-test-v5")
        runner.execute(plan, cleanup_empty_directories=False)

    assert calls == [str(source), str(source)]

