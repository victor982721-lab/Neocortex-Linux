"""Review decisions become bounded, traceable evaluation evidence."""

from __future__ import annotations

# region [01] Fixtures

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    ProtectedPathSpec,
)
from neocortex.workflow.review.review import (
    ReviewCandidate,
    ReviewDecision,
    ReviewDecisionStatus,
)
from neocortex.workflow.review.review_evidence import (
    list_review_evidence,
    materialize_review_evidence,
    review_evidence_metrics,
)
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_schema import SCHEMA_VERSION
from neocortex.persistence.framework_state_writer import FrameworkState
from tests.internal_paths_test_support import disjoint_internal_paths_policy


@pytest.fixture(autouse=True)
def _safe_state_write_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_policy = disjoint_internal_paths_policy(tmp_path.parent / f"{tmp_path.name}-policy")
    protected_policy = ProtectedContentPolicy.capture(())
    monkeypatch.setattr(
        "neocortex.safety.internal_paths.canonical_internal_paths_policy",
        lambda: internal_policy,
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: protected_policy,
    )


def _snapshot() -> FileSnapshot:
    return FileSnapshot(
        r"C:\corpus\document-page.png",
        0xAA,
        0xBB,
        1_234,
        5_678,
        4_567,
    )


def _candidate() -> ReviewCandidate:
    return ReviewCandidate(
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        source_status="done",
        recommendation="manual_review",
        retryable=False,
        confidence=0.9,
        evidence={"reason": "document_candidate", "destructive": False},
        detector_version="image-review-v1",
    )


def _decision(
    status: ReviewDecisionStatus,
    *,
    sequence: int,
) -> ReviewDecision:
    candidate = _candidate()
    return ReviewDecision(
        idempotency_key=f"review-test:document:{sequence}",
        route_name=candidate.route_name,
        snapshot=candidate.snapshot,
        reason_code=candidate.reason_code,
        candidate_generation=21,
        source_status=candidate.source_status,
        recommendation=candidate.recommendation,
        retryable=candidate.retryable,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        detector_version=candidate.detector_version,
        status=status,
        actor="victor",
        provenance={"source": "test-review-ui", "sequence": sequence},
        note=f"review outcome {sequence}",
        decided_ns=1_000 + sequence,
    )


def _record_outcomes(database, *statuses: ReviewDecisionStatus) -> list[int]:
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(21, (_candidate(),))
    return [
        route_state.record_review_decision(_decision(status, sequence=sequence))
        for sequence, status in enumerate(statuses, start=1)
    ]


# endregion [01]


# region [02] Materialized evidence and metrics


def test_recorded_decisions_are_immediately_traceable_and_consumable(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass

    decision_ids = _record_outcomes(
        database,
        "confirmed",
        "dismissed",
        "deferred",
    )
    examples = list_review_evidence(database, limit=10)

    assert [example.decision_id for example in examples] == decision_ids
    assert [example.outcome for example in examples] == [
        "accepted",
        "rejected",
        "abstained",
    ]
    assert all(example.actor == "victor" for example in examples)
    assert all(example.candidate_generation == 21 for example in examples)
    assert all(example.reason_code == "document_candidate" for example in examples)
    assert all(example.target_recommendation == "manual_review" for example in examples)
    assert all(example.candidate_evidence_complete for example in examples)
    assert examples[0].candidate_evidence == {
        "destructive": False,
        "reason": "document_candidate",
    }
    assert examples[0].provenance == {
        "sequence": 1,
        "source": "test-review-ui",
    }
    assert examples[0].decided_ns == 1_001
    assert examples[0].recorded_ns > 0
    assert examples[0].feedback_reference == f"review-decision:{decision_ids[0]}"

    assert [
        value.outcome
        for value in list_review_evidence(
            database,
            limit=10,
            decision_status="dismissed",
            route_name="image",
            reason_code="document_candidate",
            target_recommendation="manual_review",
            detector_version="image-review-v1",
            actor="victor",
            require_complete_candidate_evidence=True,
        )
    ] == ["rejected"]

    metrics = review_evidence_metrics(database)
    assert (
        metrics.total_decisions,
        metrics.materialized_examples,
        metrics.confirmed_examples,
        metrics.dismissed_examples,
        metrics.deferred_examples,
        metrics.decisive_examples,
    ) == (3, 3, 1, 1, 1, 2)
    assert metrics.materialization_coverage == 1.0
    assert metrics.decisive_label_coverage == pytest.approx(2 / 3)
    assert metrics.candidate_evidence_coverage == 1.0
    assert metrics.acceptance_rate == pytest.approx(1 / 3)
    assert metrics.rejection_rate == pytest.approx(1 / 3)
    assert metrics.abstention_rate == pytest.approx(1 / 3)
    assert metrics.evaluation_status == "descriptive_review_outcomes"
    assert metrics.calibration_status == "not_established"
    assert "not an independent representative" in metrics.calibration_reason

    # Human labels remain evaluation evidence and never create file actions.
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM run_actions").fetchone() == (0,)

    # Catching the durable cursor up over writer-materialized rows performs no
    # duplicate inserts and remains bounded to the requested range.
    catchup = materialize_review_evidence(database, batch_size=3)
    assert (
        catchup.scanned_decisions,
        catchup.materialized_examples,
        catchup.last_decision_id,
        catchup.has_more,
    ) == (3, 0, decision_ids[2], False)


def test_materialization_is_bounded_resumable_and_idempotent(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    decision_ids = _record_outcomes(
        database,
        "confirmed",
        "dismissed",
        "deferred",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM review_evidence_examples")

    before = review_evidence_metrics(database)
    assert before.total_decisions == 3
    assert before.materialized_examples == 0
    assert before.materialization_coverage == 0.0
    assert before.acceptance_rate is None
    assert before.evaluation_status == "no_materialized_examples"
    assert before.calibration_status == "not_established"

    first = materialize_review_evidence(database, batch_size=2)
    assert (
        first.scanned_decisions,
        first.materialized_examples,
        first.last_decision_id,
        first.has_more,
    ) == (2, 2, decision_ids[1], True)
    assert review_evidence_metrics(database).materialization_coverage == pytest.approx(2 / 3)

    second = materialize_review_evidence(database, batch_size=2)
    assert (
        second.scanned_decisions,
        second.materialized_examples,
        second.last_decision_id,
        second.has_more,
    ) == (1, 1, decision_ids[2], False)
    assert materialize_review_evidence(database, batch_size=2) == type(second)(
        scanned_decisions=0,
        materialized_examples=0,
        last_decision_id=None,
        has_more=False,
    )
    assert len(list_review_evidence(database, limit=10)) == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT last_scanned_decision_id FROM review_evidence_progress
            WHERE pipeline_key='review-decisions-v1'"""
        ).fetchone() == (decision_ids[2],)


def test_deferred_feedback_is_abstention_without_a_decisive_label(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    _record_outcomes(database, "deferred")

    metrics = review_evidence_metrics(database)
    assert metrics.decisive_examples == 0
    assert metrics.decisive_label_coverage == 0.0
    assert metrics.acceptance_rate == 0.0
    assert metrics.rejection_rate == 0.0
    assert metrics.abstention_rate == 1.0
    assert metrics.calibration_status == "not_established"
    assert "no accepted or rejected" in metrics.calibration_reason


def test_materialization_batch_rolls_back_on_partial_candidate_evidence(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    decision_ids = _record_outcomes(database, "confirmed", "dismissed")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM review_evidence_examples")
        connection.execute(
            "UPDATE review_decisions SET confidence=NULL WHERE decision_id=?",
            (decision_ids[1],),
        )

    with pytest.raises(sqlite3.DatabaseError, match="partially populated"):
        materialize_review_evidence(database, batch_size=2)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_evidence_examples").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM review_evidence_progress").fetchone() == (
            0,
        )


def test_decision_insert_rolls_back_when_atomic_evidence_conflicts(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    first_id = _record_outcomes(database, "confirmed")[0]
    conflicting_key = _decision("dismissed", sequence=2).idempotency_key
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE review_evidence_examples SET idempotency_key=?
            WHERE decision_id=?""",
            (conflicting_key, first_id),
        )

    with pytest.raises(sqlite3.DatabaseError, match="conflicts"):
        FrameworkRouteState(database).record_review_decision(_decision("dismissed", sequence=2))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT decision_id FROM review_decisions WHERE idempotency_key=?",
                (conflicting_key,),
            ).fetchone()
            is None
        )


# endregion [02]


# region [03] Compatible schema migration


def test_schema_15_migrates_without_unbounded_backfill_and_preserves_legacy(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    decision_id = _record_outcomes(database, "confirmed")[0]

    # Recreate the exact v15 delta: decisions exist, the evidence table does not.
    # Null candidate fields represent a decision retained from schema 14.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE review_decisions SET source_status=NULL,recommendation=NULL,
            retryable=NULL,confidence=NULL,evidence_json=NULL,detector_version=NULL
            WHERE decision_id=?""",
            (decision_id,),
        )
        connection.execute("DROP TABLE review_evidence_examples")
        connection.execute("DROP TABLE review_evidence_progress")
        connection.execute("UPDATE metadata SET value='15' WHERE key='schema_version'")

    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone() == (1,)
        # Migration stays bounded: explicit synchronization backfills old rows.
        assert connection.execute("SELECT COUNT(*) FROM review_evidence_examples").fetchone() == (
            0,
        )
        assert {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        } >= {"review_evidence_outcome_idx", "review_evidence_target_idx"}

    assert materialize_review_evidence(database, batch_size=10).materialized_examples == 1
    example = list_review_evidence(database, limit=10)[0]
    assert example.decision_id == decision_id
    assert example.actor == "victor"
    assert example.candidate_generation == 21
    assert example.reason_code == "document_candidate"
    assert example.target_recommendation is None
    assert example.candidate_evidence is None
    assert example.candidate_evidence_complete is False
    metrics = review_evidence_metrics(database)
    assert metrics.candidate_evidence_coverage == 0.0
    assert metrics.calibration_status == "not_established"


def test_failed_schema_15_transition_rolls_back_version_and_ddl(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE review_evidence_examples")
        connection.execute("DROP TABLE review_evidence_progress")
        connection.execute("CREATE TABLE review_evidence_examples(decision_id INTEGER PRIMARY KEY)")
        connection.execute("UPDATE metadata SET value='15' WHERE key='schema_version'")

    with pytest.raises(RuntimeError, match="initialization from version 15 failed"):
        FrameworkState(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("15",)
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(review_evidence_examples)")
        } == {"decision_id"}
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='review_evidence_outcome_idx'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='review_evidence_progress'"
            ).fetchone()
            is None
        )


# endregion [03]


# region [04] Canonical CLI integration


def test_review_evidence_cli_sync_metrics_and_filtered_json_list(
    tmp_path,
    capsys,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    _record_outcomes(database, "confirmed", "dismissed", "deferred")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM review_evidence_examples")
        connection.execute("DELETE FROM review_evidence_progress")

    sync_args = build_parser().parse_args(
        (
            "--state-directory",
            str(tmp_path),
            "--review-evidence-sync",
            "--review-evidence-batch-size",
            "2",
            "--review-json",
        )
    )
    validate_arguments(sync_args)
    assert dispatch_direct(sync_args) == 0
    sync_payload = json.loads(capsys.readouterr().out)
    assert sync_payload == {
        "has_more": True,
        "kind": "review-evidence-sync",
        "last_decision_id": 2,
        "materialized_examples": 2,
        "scanned_decisions": 2,
    }

    metrics_args = build_parser().parse_args(
        (
            "--state-directory",
            str(tmp_path),
            "--review-evidence-metrics",
            "--review-evidence-route",
            "image",
            "--review-json",
        )
    )
    validate_arguments(metrics_args)
    assert dispatch_direct(metrics_args) == 0
    metrics_payload = json.loads(capsys.readouterr().out)
    assert metrics_payload["kind"] == "review-evidence-metrics"
    assert metrics_payload["total_decisions"] == 3
    assert metrics_payload["materialized_examples"] == 2
    assert metrics_payload["calibration_status"] == "not_established"

    list_args = build_parser().parse_args(
        (
            "--state-directory",
            str(tmp_path),
            "--review-evidence-list",
            "10",
            "--review-evidence-status",
            "dismissed",
            "--review-evidence-completeness",
            "complete",
            "--review-evidence-actor",
            "victor",
            "--review-json",
        )
    )
    validate_arguments(list_args)
    assert dispatch_direct(list_args) == 0
    evidence_payload = json.loads(capsys.readouterr().out)
    assert evidence_payload["kind"] == "review-evidence"
    assert evidence_payload["decision_status"] == "dismissed"
    assert evidence_payload["outcome"] == "rejected"
    assert evidence_payload["candidate_evidence_complete"] is True
    assert evidence_payload["actor"] == "victor"

    decision_args = build_parser().parse_args(
        (
            "--state-directory",
            str(tmp_path),
            "--review-decisions",
            "10",
            "--review-route",
            "image",
            "--review-json",
        )
    )
    validate_arguments(decision_args)
    assert dispatch_direct(decision_args) == 0
    decision_payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(decision_payloads) == 3
    assert all(payload["kind"] == "review-decision" for payload in decision_payloads)
    assert all(payload["route"] == "image" for payload in decision_payloads)
    assert all(payload["candidate_snapshot"] is not None for payload in decision_payloads)


@pytest.mark.parametrize("batch_size", (0, 257))
def test_review_evidence_cli_rejects_unbounded_sync_batches(batch_size: int) -> None:
    args = build_parser().parse_args(
        ("--review-evidence-sync", "--review-evidence-batch-size", str(batch_size))
    )
    with pytest.raises(
        SystemExit,
        match="--review-evidence-batch-size must be between 1 and 256",
    ):
        validate_arguments(args)


def test_review_evidence_sync_does_not_create_missing_state(tmp_path, capsys) -> None:
    state = tmp_path / "missing-state"
    args = build_parser().parse_args(("--state-directory", str(state), "--review-evidence-sync"))
    validate_arguments(args)

    assert dispatch_direct(args) == 2
    assert "state database does not exist" in capsys.readouterr().out
    assert not state.exists()


def test_review_evidence_sync_rejects_existing_protected_state_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    state = tmp_path / "protected-state"
    database = state / "framework.sqlite3"
    state.mkdir()
    with FrameworkState(database):
        pass
    before = database.read_bytes()
    protected_policy = ProtectedContentPolicy.capture(
        (
            ProtectedPathSpec(
                "protected-state",
                "tree",
                "exclude",
                state,
            ),
        )
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: protected_policy,
    )
    args = build_parser().parse_args(("--state-directory", str(state), "--review-evidence-sync"))
    validate_arguments(args)

    with patch("neocortex.workflow.review.review_evidence.materialize_review_evidence") as materialize:
        assert dispatch_direct(args) == 2

    materialize.assert_not_called()
    assert "protected content" in capsys.readouterr().out
    assert database.read_bytes() == before


def test_review_evidence_cli_filters_require_a_compatible_query() -> None:
    args = build_parser().parse_args(("--review-evidence-route", "image"))
    with pytest.raises(
        SystemExit,
        match="review evidence filters require --review-evidence-metrics",
    ):
        validate_arguments(args)


def test_review_evidence_cli_does_not_silently_accept_legacy_review_filters() -> None:
    args = build_parser().parse_args(("--review-evidence-metrics", "--review-route", "image"))
    with pytest.raises(SystemExit, match="review options require a review command"):
        validate_arguments(args)


def test_review_json_requires_a_review_operation() -> None:
    args = build_parser().parse_args(("--review-json",))
    with pytest.raises(SystemExit, match="--review-json requires a review command"):
        validate_arguments(args)


# endregion [04]
