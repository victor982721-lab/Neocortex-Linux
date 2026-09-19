"""Action-owner budget gates stay before proof and physical effects."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState, RunBudgetExceeded
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.runtime.models import ActionSummary
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


class _Backend:
    def __init__(self) -> None:
        self.calls = 0

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        self.calls += 1
        return tuple(
            BackendOutcome(
                "applied",
                "test",
                receipt_json='{"schema":"test-receipt/v1"}',
            )
            for _snapshot, _digest in items
        )


def test_budget_exhaustion_happens_before_third_party_proof_or_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "vendor" / "library.py"
    candidate.parent.mkdir()
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    # This is only a bounded source-list entry; the reservation must fail
    # before the proof reader attempts to inspect it.
    (candidate.parent / "source.whl").write_bytes(b"PK\x03\x04fixture")

    backend = _Backend()
    reservations: list[tuple[str, int, int]] = []

    def reserve(key: str, items: int, bytes_count: int) -> None:
        reservations.append((key, items, bytes_count))
        if "admission-prefix" in key:
            raise RunBudgetExceeded("bytes", {"remaining_bytes": 0})

    def unexpected_proof(*_args, **_kwargs):
        raise AssertionError("regeneration proof was called after budget exhaustion")

    monkeypatch.setattr(
        "neocortex.workflow.actions.regeneration.find_regeneration_proof",
        unexpected_proof,
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
                apply=True,
                trash_backend=backend,  # type: ignore[arg-type]
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                reserve_work=reserve,
            )
            with pytest.raises(RunBudgetExceeded):
                runner._trash_third_party_code(plan, ActionSummary(apply_actions=True))
            action_rows = int(
                state._connection.execute(
                    "SELECT COUNT(*) FROM file_actions WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )

    assert reservations
    assert reservations[0][2] > 0
    assert backend.calls == 0
    assert action_rows == 0
    assert candidate.exists()


def test_trash_batch_reservation_is_single_and_conservative(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "candidate.txt"
    candidate.write_text("candidate\n", encoding="utf-8")
    reference = root / "reference.txt"
    reference.write_text("reference\n", encoding="utf-8")
    reservations: list[tuple[str, int, int]] = []

    def reserve(key: str, items: int, bytes_count: int) -> None:
        reservations.append((key, items, bytes_count))

    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            snapshot = next(item for item in index.snapshots(scan.scan_id) if item.path == str(candidate))
            keeper = next(item for item in index.snapshots(scan.scan_id) if item.path == str(reference))
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=False,
                reserve_work=reserve,
            )
            result = runner._apply_trash_batch(
                "trash_duplicate",
                ((snapshot.path, "test"),),
                expected_snapshots=(snapshot,),
                reference_snapshots=(keeper,),
            )

    assert result == (0, 0, 0)
    assert len(reservations) == 1
    assert reservations[0][1] == 1
    assert reservations[0][2] >= snapshot.size + keeper.size


def test_no_archive_skips_heavy_proof_and_proof_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "vendor" / "library.py"
    candidate.parent.mkdir()
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    reservations: list[tuple[str, int, int]] = []

    def reserve(key: str, items: int, bytes_count: int) -> None:
        reservations.append((key, items, bytes_count))

    def unexpected_proof(*_args, **_kwargs):
        raise AssertionError("proof must not run without an archive or pyc source")

    monkeypatch.setattr(
        "neocortex.workflow.actions.regeneration.find_regeneration_proof",
        unexpected_proof,
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
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                reserve_work=reserve,
            )
            summary = runner._trash_third_party_code(
                plan, ActionSummary(apply_actions=False)
            )

    assert summary.regeneration_unproven == 1
    assert not any("third-party-proof" in key for key, _items, _bytes in reservations)


def test_proof_reservation_uses_fixture_input_sizes_not_constant_per_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "vendor" / "library.py"
    candidate.parent.mkdir()
    candidate.write_text("VALUE = 1\n", encoding="utf-8")
    source = candidate.parent / "source.whl"
    source.write_bytes(b"PK\x03\x04small-invalid-fixture")
    reservations: list[tuple[str, int, int]] = []

    def reserve(key: str, items: int, bytes_count: int) -> None:
        reservations.append((key, items, bytes_count))

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
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                reserve_work=reserve,
            )
            summary = runner._trash_third_party_code(
                plan, ActionSummary(apply_actions=False)
            )

    proof_reservations = [
        (items, bytes_count)
        for key, items, bytes_count in reservations
        if "third-party-proof" in key
    ]
    assert summary.regeneration_unproven == 1
    assert proof_reservations == [(1, candidate.stat().st_size + source.stat().st_size)]


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
        if "admission-prefix" in key:
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
        reservation for reservation in reservations if "admission-prefix" in reservation[0]
    ]
    assert prefix_reservations
    assert prefix_reservations[0][1] == 0
    assert prefix_reservations[0][2] >= len("document\n")
