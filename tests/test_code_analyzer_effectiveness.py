"""Regressions for the self-analysis effectiveness and freshness projection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from neocortex.code.code_analyzer_effectiveness import (
    analyze_code_analyzer_effectiveness,
    analyzer_effectiveness_questions,
    parse_code_analyzer_effectiveness_payload,
)
from neocortex.code.code_schema import (
    checkpoint_code_wal,
    remove_checkpointed_code_sidecars,
)
from neocortex.code.code_state import CodeState
from tests.test_code_review import PROCESSING_SIGNATURE, _analysis


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _build_state(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repository"
    root.mkdir()
    files = (root / "alpha.py", root / "beta.py")
    text = "x\n" * 5_000
    for path in files:
        path.write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "--", "alpha.py", "beta.py")

    database = tmp_path / "state" / "code.sqlite3"
    database.parent.mkdir()
    with CodeState(database) as state:
        run_id = state.begin_run(1, 1, PROCESSING_SIGNATURE)
        for identity, path in enumerate(files, start=100):
            state.store_analysis(_analysis(path, identity, symbols=()), run_id)
        state.finalize_graph(run_id)
        state.complete_run(
            run_id,
            {
                "candidates": 2,
                "processed": 2,
                "cache_hits": 0,
                "errors": 0,
                "analyze_milliseconds": 3,
                "graph_milliseconds": 5,
                "external_milliseconds": 7,
            },
            partial=False,
            graph_current=True,
        )
        checkpoint_code_wal(state.connection)
    remove_checkpointed_code_sidecars(database)
    return database, root


def _analyze(database: Path, root: Path):
    return analyze_code_analyzer_effectiveness(
        database,
        root,
        source_version="fixture-review/v1",
        snapshot_freshness="current",
        findings_observed=2,
        recommendations_observed=0,
        work_packages_observed=0,
    )


def test_exact_visible_checkout_is_observed_without_claiming_calibration(tmp_path: Path) -> None:
    database, root = _build_state(tmp_path)

    analysis = _analyze(database, root)
    specs, evaluations = analyzer_effectiveness_questions(analysis, rank_offset=4)

    assert analysis.status == "ready"
    assert analysis.inventory_observation == "exact"
    assert analysis.scoped_recorded_files == analysis.git_visible_files == 2
    assert analysis.exact_content_files == 2
    assert analysis.metadata_changed_content_equal_files == 2
    assert analysis.content_changed_files == 0
    assert analysis.calibration_status == "not_established"
    assert analysis.precision_at_k is analysis.recall is analysis.finding_to_decision_rate is None
    assert len(specs) == len(evaluations) == 2
    assert tuple(item.rank for item in evaluations) == (5, 6)
    assert all(item.observation_status == "confirmed" for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.decision is None for item in evaluations)


def test_content_change_missing_and_unindexed_files_are_distinct_observations(
    tmp_path: Path,
) -> None:
    database, root = _build_state(tmp_path)
    (root / "alpha.py").write_text("changed\n", encoding="utf-8")
    (root / "beta.py").unlink()
    (root / "new.py").write_text("new\n", encoding="utf-8")

    analysis = _analyze(database, root)

    assert analysis.status == "ready"
    assert analysis.inventory_observation == "content_stale_and_scope_incomplete"
    assert analysis.content_changed_files == 1
    assert analysis.missing_recorded_files == 1
    assert analysis.unindexed_git_visible_files == 1
    assert analysis.content_changed_examples == ("alpha.py",)
    assert analysis.missing_recorded_examples == ("beta.py",)
    assert analysis.unindexed_git_visible_examples == ("new.py",)
    assert analysis.recommendations_observed == 0
    assert analysis.authority == "advisory"
    assert analysis.mutation_authority is False


def test_changed_recorded_content_is_not_confused_with_scope_incompleteness(
    tmp_path: Path,
) -> None:
    database, root = _build_state(tmp_path)
    (root / "alpha.py").write_text("changed\n", encoding="utf-8")

    analysis = _analyze(database, root)

    assert analysis.inventory_observation == "content_stale"
    assert analysis.content_changed_files == 1
    assert analysis.unindexed_git_visible_files == 0


def test_ignored_file_is_outside_the_git_visible_inventory_claim(tmp_path: Path) -> None:
    database, root = _build_state(tmp_path)
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _git(root, "add", "--", ".gitignore")
    (root / "ignored.py").write_text("ignored\n", encoding="utf-8")

    analysis = _analyze(database, root)

    assert analysis.status == "ready"
    assert analysis.inventory_observation == "scope_incomplete"
    assert analysis.unindexed_git_visible_examples == (".gitignore",)
    assert "ignored.py" not in analysis.unindexed_git_visible_examples
    assert "git_visible_inventory_excludes_ignored_files" in analysis.limitations


def test_effectiveness_wire_roundtrip_rejects_forged_calibration(tmp_path: Path) -> None:
    database, root = _build_state(tmp_path)
    analysis = _analyze(database, root)
    payload = json.loads(json.dumps(analysis.as_payload()))

    assert parse_code_analyzer_effectiveness_payload(payload) == analysis

    payload["precision_at_k"] = 1.0
    with pytest.raises(ValueError, match="cannot invent calibration"):
        parse_code_analyzer_effectiveness_payload(payload)


def test_unresolvable_checkout_abstains_without_partial_claims(tmp_path: Path) -> None:
    database, _root = _build_state(tmp_path)

    analysis = _analyze(database, tmp_path / "missing")
    _specs, evaluations = analyzer_effectiveness_questions(analysis, rank_offset=0)

    assert analysis.status == "abstained"
    assert analysis.inventory_observation is None
    assert analysis.recorded_current_files == 0
    assert all(item.observation_status == "abstained" for item in evaluations)
    assert all(item.evidence == () for item in evaluations)
    assert all(item.next_action_ids == () for item in evaluations)
