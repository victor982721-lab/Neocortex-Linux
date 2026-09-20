from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions, RedlistPrepassError
from neocortex.workflow.actions.redlist import redlist_policy_digest
from tests.internal_paths_test_support import begin_signed_normal_run


def _framework_database(root: Path) -> Path:
    return root / "framework.sqlite3"


class _NeverCalledBackend:
    def apply_many_snapshots(self, items, *, root: Path):
        del items, root
        raise AssertionError("a protected redlist item must not reach the backend")


def test_protected_redlist_is_partial_and_excluded_from_downstream(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "protected.timer"
    candidate.write_text("keep", encoding="utf-8")

    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(state_root)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=_NeverCalledBackend(),  # type: ignore[arg-type]
        )

        def protected_batch(*_args, **_kwargs):
            runner._record_redlist_batch_diagnostic(
                "protected", "fixture_protected", str(candidate)
            )
            return [], 1

        runner._begin_trash_candidates = protected_batch  # type: ignore[method-assign]
        result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
        plan = DedupPlanner(index).plan(scan.scan_id)
        summary = runner.execute(plan, cleanup_empty_directories=False)
        route_rows = state._connection.execute(
            "SELECT COUNT(*) FROM route_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    assert candidate.exists()
    assert result["matched"] == 1
    assert result["applied"] == 0
    assert result["protected"] == 1
    assert result["blocked"] == 0
    assert result["failed_pre_effect"] == 0
    assert result["recovery_required"] == 0
    assert result["skipped"] == 1
    assert result["reason_codes"]
    assert result["examples"]
    assert summary.files_checked == 0
    assert route_rows == 0


def test_only_real_redlist_ambiguity_is_fatal(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "discard.BAK-2"
    candidate.write_bytes(b"discard")

    class AmbiguousBackend:
        def apply_many_snapshots(self, items, *, root: Path):
            del root
            return tuple(
                BackendOutcome("recovery_required", "fixture_ambiguous", "inspect fixture")
                for _snapshot, _binding in items
            )

    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(state_root)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=True,
            trash_backend=AmbiguousBackend(),  # type: ignore[arg-type]
        )
        with pytest.raises(RedlistPrepassError) as raised:
            runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())

    error = raised.value
    assert error.matched == 1
    assert error.applied == 0
    assert error.protected == 0
    assert error.recovery_required == 1
    assert error.failed_pre_effect == 0


def test_blocked_redlist_is_not_recovery(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "blocked.timer"
    candidate.write_text("keep", encoding="utf-8")

    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(state_root)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=True)

        def blocked_batch(*_args, **_kwargs):
            runner._record_redlist_batch_diagnostic(
                "blocked", "fixture_blocked", str(candidate)
            )
            return [], 1

        runner._begin_trash_candidates = blocked_batch  # type: ignore[method-assign]
        result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())

    assert result["matched"] == 1
    assert result["blocked"] == 1
    assert result["protected"] == 0
    assert result["failed_pre_effect"] == 0
    assert result["recovery_required"] == 0
    assert candidate.exists()


def test_identify_normalize_does_not_hash_or_publish_routes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    source = root / "image.txt"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("Identify/Normalize must not calculate a full hash")

    monkeypatch.setattr("neocortex.workflow.actions.actions.full_fingerprint", forbidden_hash)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(state_root)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)
        summary = runner.identify_and_normalize()
        route_rows = state._connection.execute(
            "SELECT COUNT(*) FROM route_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]

    assert summary.rename_candidates == 1
    assert summary.files_renamed == 0
    assert source.exists()
    assert not (root / "image.png").exists()
    assert route_rows == 0


def test_redlist_uses_normalized_policy_path_without_payload_hash(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    source = root / "extensionless"
    source.write_bytes(b"MZ" + b"\0" * 100)

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("Identify/Normalize must not calculate a full hash")

    monkeypatch.setattr("neocortex.workflow.actions.actions.full_fingerprint", forbidden_hash)
    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(_framework_database(state_root)) as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(index, state, run_id, scan.scan_id, apply=False)
        normalize = runner.identify_and_normalize()
        result = runner.apply_redlist_prepass(policy_digest=redlist_policy_digest())
        rows = state._connection.execute(
            "SELECT action_type,status,evidence FROM file_actions "
            "WHERE run_id=? ORDER BY action_id", (run_id,)
        ).fetchall()

    assert normalize.rename_candidates == 1
    assert result["matched"] == 1
    assert result["planned"] == 1
    assert result["recovery_required"] == 0
    assert source.exists()
    assert any(row[0:2] == ("trash_redlist", "planned") for row in rows)
    assert any(
        row[0] == "trash_redlist" and json.loads(row[2]).get("redlist_entry") == ".exe"
        for row in rows
    )
