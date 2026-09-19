"""Retention and confidence guards at the action-owner frontier."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from neocortex.code.ingestion.code_detection import classify_third_party_artifact
from neocortex.deduplication import FileSnapshot
from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.config.third_party_policy import CodeThirdPartyPolicy
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.actions.corpus_admission import (
    CorpusAdmissionPolicy,
    assess_file,
)
from tests.internal_paths_test_support import begin_signed_normal_run


class _State:
    def begin_file_actions(self, _run_id, actions):
        self.rows = tuple(actions)
        return list(range(1, len(self.rows) + 1))

    def finish_file_actions(self, ids, status, detail=None):
        self.finished = (tuple(ids), status, detail)


class _Guard:
    def mutation_path_protection_reasons(self, *paths):
        return (None,) * len(paths)


class _Index:
    def __init__(self, root: Path) -> None:
        self.root = root

    def scan_root(self, _scan_id: int) -> str:
        return str(self.root)


def _runner(root: Path, policy: CorpusAdmissionPolicy) -> FrameworkActions:
    runner = object.__new__(FrameworkActions)
    runner._index = _Index(root)
    runner._scan_id = 1
    runner._run_id = 1
    runner._state = _State()
    runner._apply = False
    runner._admission_policy = policy
    runner._preservation_policy = policy
    runner._cancellation_check = None
    runner._preservation_reasons = {}
    runner._preservation_examples = []
    runner._admission_reasons = {}
    runner._admission_examples = []
    runner._effective_mutation_guard = lambda: _Guard()
    return runner


@pytest.mark.parametrize(
    ("relative", "payload", "reason"),
    (
        (".env", b"TOKEN=fixture", "credential"),
        ("fixtures/example.py", b"VALUE = 1\n", "fixture"),
        ("package.whl", b"not-a-real-wheel", "retained_witness"),
        ("LICENSE-MIT", b"attribution", "legal_metadata"),
    ),
)
def test_preservation_categories_are_vetoed_before_file_action_ledger(
    tmp_path: Path, relative: str, payload: bytes, reason: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stat = path.stat()
    snapshot = FileSnapshot(
        str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, -1
    )
    policy = CorpusAdmissionPolicy((root / "owned-project",), "projects")
    runner = _runner(root, policy)
    decision = assess_file(snapshot, root=root, policy=policy)
    eligible, protected = runner._begin_trash_candidates(
        "trash_duplicate",
        ((str(path), "fixture"),),
        (snapshot,),
        (None,),
        mutation_guard=_Guard(),
    )

    if relative in {".env", "fixtures/example.py"}:
        assert decision.disposition in {"sensitive", "metadata_only"}
    assert eligible == []
    assert protected == 1
    assert not hasattr(runner._state, "rows")
    assert runner._preservation_reasons == {reason: 1}


def test_third_party_min_confidence_is_not_replaced_by_a_constant() -> None:
    classification = classify_third_party_artifact("/tmp/project/build/generated.bin")
    policy = CodeThirdPartyPolicy(action="trash", min_confidence=1.0)

    assert classification.confidence == 0.95
    assert not policy.admits(classification.kind.value, classification.confidence)
    assert policy.admits(classification.kind.value, 1.0)


def test_preservation_veto_is_durable_bounded_evidence_without_action_row(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    keeper = root / "keeper.txt"
    secret = root / "vendor" / ".env"
    keeper.write_text("TOKEN=fixture\n", encoding="utf-8")
    secret.parent.mkdir()
    secret.write_text("TOKEN=fixture\n", encoding="utf-8")

    with DedupIndex(state_root / "dedup.sqlite3") as index:
        scan = index.scan(root, excluded_paths=())
        plan = DedupPlanner(index).plan(scan.scan_id)
        with FrameworkState(state_root / "framework.sqlite3") as state:
            run_id = begin_signed_normal_run(state, root)
            runner = FrameworkActions(
                index,
                state,
                run_id,
                scan.scan_id,
                apply=True,
                excluded_paths=(),
            )
            summary = runner.execute(plan, cleanup_empty_directories=False)
            event = state._connection.execute(
                """SELECT details_json FROM run_events
                WHERE run_id=? AND phase='corpus-admission'
                ORDER BY event_id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            action_count = int(
                state._connection.execute(
                    "SELECT COUNT(*) FROM file_actions WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )

    assert summary.duplicate_skips == 1
    assert action_count == 0
    assert event is not None
    payload = json.loads(str(event[0]))
    assert payload["veto_total"] >= 1
    assert payload["veto_reasons"]["credential"] >= 1
    assert payload["file_actions_created_for_vetoes"] == 0
    assert payload["preservation_examples"]
    assert "path" not in payload["preservation_examples"][0]
    assert "TOKEN=fixture" not in str(payload)
