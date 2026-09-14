"""Explicit third-party Code cleanup remains bounded and receipt-bound."""

from __future__ import annotations

import json
from pathlib import Path

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex
from neocortex.deduplication import DedupPlanner
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.runtime.models import ActionSummary
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


class _FixtureBatchBackend:
    """Tiny injected backend that models verified reversible effects."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        items = tuple(items)
        self.calls.append(len(items))
        outcomes = []
        for snapshot, _digest in items:
            Path(snapshot.path).unlink()
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_verified",
                    receipt_json=json.dumps({"schema": "fixture-receipt/v1", "path": snapshot.path}),
                )
            )
        return tuple(outcomes)


def test_explicit_third_party_cleanup_moves_only_strong_signals(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    owned = root / "owned"
    owned.mkdir()
    (owned / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "vendor" / "library.py").parent.mkdir()
    (root / "vendor" / "library.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "libfoo.so").write_bytes(b"\x7fELF\x02\x01\x01\x00fixture")
    (root / "vendor" / "archive.zip").write_bytes(b"PK\x03\x04not-a-code-action")
    generated = root / "generated" / "client.py"
    generated.parent.mkdir()
    generated.write_text("VALUE = 4\n", encoding="utf-8")
    (root / "loose.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "LICENSE").write_text("keep attribution\n", encoding="utf-8")

    backend = _FixtureBatchBackend()
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
                trash_backend=backend,
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
                third_party_project_roots=(owned,),
            )
            summary = runner._trash_third_party_code(
                plan,
                ActionSummary(apply_actions=True),
            )

    assert backend.calls == [2]
    assert summary.third_party_candidates == 2
    assert summary.third_party_trashed == 2
    assert summary.third_party_skips == 0
    assert (owned / "main.py").exists()
    assert (root / "loose.py").exists()
    assert (root / "LICENSE").exists()
    assert (root / "vendor" / "archive.zip").exists()
    assert generated.exists()
    assert not (root / "vendor" / "library.py").exists()
    assert not (root / "libfoo.so").exists()


def test_third_party_cleanup_is_preview_only_without_apply(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    candidate = root / "vendor" / "library.py"
    candidate.parent.mkdir()
    candidate.write_text("VALUE = 1\n", encoding="utf-8")

    backend = _FixtureBatchBackend()
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
                trash_backend=backend,
                third_party_policy=CodeThirdPartyPolicy(action="trash"),
            )
            summary = runner._trash_third_party_code(
                plan,
                ActionSummary(apply_actions=False),
            )

    assert summary.third_party_candidates == 1
    assert summary.third_party_trashed == 0
    assert backend.calls == []
    assert candidate.exists()
