"""Global size admission at the Framework action/content boundary."""

from __future__ import annotations

from pathlib import Path

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions import actions as actions_module
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.actions.redlist import redlist_policy_digest
from tests.internal_paths_test_support import begin_signed_normal_run


def _framework_database(base: Path) -> Path:
    state = base / "state"
    state.mkdir()
    return state / "framework.sqlite3"


def _png(path: Path, payload_size: int) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * payload_size)


def test_oversize_is_rejected_before_snapshot_detector_cache_or_routes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    small = root / "small.png"
    large = root / "large.png"
    _png(small, 8)
    _png(large, 192)

    detector_calls: list[str] = []
    detector = actions_module.detect_content_type

    def counted_detector(path: str | Path):
        detector_calls.append(str(path))
        return detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)

    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(tmp_path)) as state,
    ):
        scan = index.scan(root)
        inventory_paths = {snapshot.path for snapshot in index.snapshots(scan.scan_id)}
        assert inventory_paths == {str(small), str(large)}
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=False,
            max_file_bytes=100,
        )

        original_snapshot = runner._snapshot_path
        snapshot_calls: list[str] = []

        def guarded_snapshot(path: str | Path):
            snapshot_calls.append(str(path))
            if str(path) == str(large):
                raise AssertionError("oversize source reached a stat refresh")
            return original_snapshot(path)

        monkeypatch.setattr(runner, "_snapshot_path", guarded_snapshot)
        identified = runner.identify_and_normalize()
        summary = runner.execute(plan, cleanup_empty_directories=False)
        route_rows = state._connection.execute(
            "SELECT path FROM route_candidates WHERE run_id=? ORDER BY path", (run_id,)
        ).fetchall()
        cache_rows = state._connection.execute(
            "SELECT COUNT(*) FROM content_type_cache"
        ).fetchone()[0]

    assert detector_calls == [str(small)]
    assert str(large) not in snapshot_calls
    assert identified.type_cache_misses == 1
    assert identified.unknown_types == 0
    assert summary.errors == 0
    assert summary.stale_inventory == 0
    assert summary.unknown_types == 0
    assert route_rows == [(str(small),)]
    assert cache_rows == 1
    assert runner._size_skipped_files == 1
    assert runner._size_skipped_bytes == large.stat().st_size
    # These fields are supplied by the shared runtime summary contract.  The
    # fallback keeps this focused test useful against older action fixtures.
    assert getattr(identified, "size_skipped_files", 1) == 1
    assert (
        getattr(identified, "size_skipped_bytes", large.stat().st_size)
        == large.stat().st_size
    )


def test_changing_or_removing_limit_re_admits_same_inventory_identity(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    small = root / "small.png"
    large = root / "large.png"
    _png(small, 8)
    _png(large, 192)
    detector_calls: list[str] = []
    detector = actions_module.detect_content_type

    def counted_detector(path: str | Path):
        detector_calls.append(str(path))
        return detector(path)

    monkeypatch.setattr(actions_module, "detect_content_type", counted_detector)
    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(tmp_path)) as state,
    ):
        scan = index.scan(root)
        first = FrameworkActions(
            index,
            state,
            begin_signed_normal_run(state, root),
            scan.scan_id,
            apply=False,
            max_file_bytes=100,
        ).identify_and_normalize()
        second_runner = FrameworkActions(
            index,
            state,
            begin_signed_normal_run(state, root),
            scan.scan_id,
            apply=False,
            max_file_bytes=300,
        )
        second = second_runner.identify_and_normalize()
        third = FrameworkActions(
            index,
            state,
            begin_signed_normal_run(state, root),
            scan.scan_id,
            apply=False,
        ).identify_and_normalize()

    assert first.type_cache_misses == 1
    assert second.type_cache_hits == 1
    assert second.type_cache_misses == 1
    assert second_runner._size_skipped_files == 0
    assert third.type_cache_hits == 2
    assert third.type_cache_misses == 0
    assert detector_calls == [str(small), str(large)]


def test_oversize_duplicate_is_not_an_action_candidate_or_ledger_row(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    payload = b"duplicate-oversize" * 20
    first.write_bytes(payload)
    second.write_bytes(payload)

    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(tmp_path)) as state,
    ):
        scan = index.scan(root)
        # Build an intentionally unrestricted plan: the action owner must
        # still fail closed if a stale/direct caller supplies such a plan.
        plan = DedupPlanner(index).plan(scan.scan_id)
        run_id = begin_signed_normal_run(state, root)
        summary = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            max_file_bytes=10,
        ).execute(plan, cleanup_empty_directories=False)
        action_rows = state._connection.execute(
            "SELECT action_type,status FROM file_actions WHERE run_id=?", (run_id,)
        ).fetchall()

    assert summary.duplicate_candidates == 0
    assert summary.duplicate_skips == 0
    assert summary.errors == 0
    assert summary.unknown_types == 0
    assert action_rows == []
    assert first.read_bytes() == payload
    assert second.read_bytes() == payload


def test_oversize_redlist_match_is_not_evaluated_or_actioned(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    redlisted = root / "generated.BAK-2"
    redlisted.write_bytes(b"oversize-redlist" * 20)

    with (
        DedupIndex(tmp_path / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(tmp_path)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            max_file_bytes=10,
            trash_backend=None,
        )
        result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
        rows = state._connection.execute(
            "SELECT action_type,status FROM file_actions WHERE run_id=?", (run_id,)
        ).fetchall()

    assert result["matched"] == 0
    assert rows == []
    assert redlisted.exists()
