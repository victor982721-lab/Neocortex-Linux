from __future__ import annotations

# region [01] Fixtures

import argparse
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.api.cli.cli_direct import run_review_candidates
from neocortex.workflow.review.review import (
    MAX_EVIDENCE_BYTES,
    ReviewCandidate,
    ReviewDecision,
    ReviewDecisionStatus,
    get_review_decision_by_key,
    list_review_candidates,
    list_review_decisions,
)
from neocortex.persistence.state import (
    REVIEW_RECONCILIATION_BATCH_SIZE,
    SCHEMA_VERSION,
    FrameworkRouteState,
    FrameworkState,
    ReviewCandidateReconciliation,
)


def _snapshot() -> FileSnapshot:
    return FileSnapshot(
        r"C:\corpus\damaged.pdf",
        0xAA,
        0xBB,
        1234,
        5678,
        4567,
    )


def _candidate(reason_code: str) -> ReviewCandidate:
    return ReviewCandidate(
        route_name="image",
        snapshot=_snapshot(),
        reason_code=reason_code,
        source_status="done",
        recommendation="manual_review",
        retryable=False,
        confidence=0.9,
        evidence={"reason": reason_code},
        detector_version="image-review-v1",
    )


# endregion [01]


# region [02] Persistence and lifecycle


def test_review_candidate_is_advisory_persistent_and_resolvable(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass

    route_state = FrameworkRouteState(database)
    candidate = ReviewCandidate(
        route_name="pdf",
        snapshot=_snapshot(),
        reason_code="corrupt_container_unrecoverable",
        source_status="error",
        recommendation="deletion_candidate",
        retryable=False,
        confidence=0.99,
        evidence={"probe": "parser_fallback_failed", "destructive": False},
        detector_version="pdf-integrity-v1",
    )
    route_state.store_review_candidates(7, (candidate,))

    records = list_review_candidates(database, limit=10)
    assert len(records) == 1
    assert records[0].recommendation == "deletion_candidate"
    assert records[0].path == _snapshot().path
    assert records[0].evidence["destructive"] is False

    assert (
        route_state.resolve_review_candidates(
            8,
            "pdf",
            _snapshot(),
            "strict extraction succeeded",
        )
        == 1
    )
    assert list_review_candidates(database, limit=10) == []
    resolved = list_review_candidates(database, limit=10, status="resolved")
    assert len(resolved) == 1


def test_schema_11_migration_adds_review_table_without_losing_metadata(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID"
    )
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES(?,?)",
        (("schema_version", "11"), ("preserved", "yes")),
    )
    connection.commit()
    connection.close()

    with FrameworkState(database):
        pass

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='preserved'"
        ).fetchone() == ("yes",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='review_candidates'"
        ).fetchone() == ("review_candidates",)
    finally:
        connection.close()


def test_reconciliation_is_scoped_by_reason_and_generation(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(
        10,
        (
            _candidate("adult_content"),
            _candidate("document_candidate"),
            _candidate("damaged_content"),
        ),
    )

    # Generation 11 still detects a document, but no longer detects adult content.
    route_state.store_review_candidates(11, (_candidate("document_candidate"),))
    assert (
        route_state.reconcile_review_candidates(
            11,
            "image",
            _snapshot(),
            "image review detectors completed",
            evaluated_reason_codes=("adult_content", "document_candidate"),
            active_reason_codes=("document_candidate",),
        )
        == 1
    )

    open_reasons = {record.reason_code for record in list_review_candidates(database, limit=10)}
    assert open_reasons == {"document_candidate", "damaged_content"}
    resolved = list_review_candidates(database, limit=10, status="resolved")
    assert [(record.reason_code, record.resolved_generation) for record in resolved] == [
        ("adult_content", 11)
    ]
    assert resolved[0].last_detected_generation == 10

    # Reconciliation is idempotent and never resolves a current-generation row.
    assert (
        route_state.reconcile_review_candidates(
            11,
            "image",
            _snapshot(),
            "image review detectors completed",
            evaluated_reason_codes=("adult_content", "document_candidate"),
            active_reason_codes=(),
        )
        == 0
    )


def test_exact_generation_resolution_does_not_close_other_reasons(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(
        12,
        (
            _candidate("adult_content"),
            _candidate("document_candidate"),
        ),
    )

    assert (
        route_state.resolve_review_candidate_generation(
            12,
            "image",
            _snapshot(),
            "adult_content",
            "verified terminal action completed",
        )
        == 1
    )
    assert {record.reason_code for record in list_review_candidates(database, limit=10)} == {
        "document_candidate"
    }
    resolved = list_review_candidates(database, limit=10, status="resolved")
    assert [(record.reason_code, record.resolved_generation) for record in resolved] == [
        ("adult_content", 12)
    ]
    assert (
        route_state.resolve_review_candidate_generation(
            11,
            "image",
            _snapshot(),
            "document_candidate",
            "stale terminal action",
        )
        == 0
    )


def test_batched_reconciliation_is_bounded_atomic_and_idempotent(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(
        30,
        (
            _candidate("adult_content"),
            _candidate("document_candidate"),
        ),
    )
    reconciliation = ReviewCandidateReconciliation(
        snapshot=_snapshot(),
        resolution_note="batched image detectors completed",
        evaluated_reason_codes=("adult_content", "document_candidate"),
        active_reason_codes=("document_candidate",),
    )

    assert (
        route_state.reconcile_review_candidates_batch(
            31,
            "image",
            (reconciliation,),
        )
        == 1
    )
    assert (
        route_state.reconcile_review_candidates_batch(
            31,
            "image",
            (reconciliation,),
        )
        == 0
    )
    with pytest.raises(ValueError, match="exceeds"):
        route_state.reconcile_review_candidates_batch(
            32,
            "image",
            (reconciliation for _ in range(REVIEW_RECONCILIATION_BATCH_SIZE + 1)),
        )
    assert {record.reason_code for record in list_review_candidates(database, limit=10)} == {
        "document_candidate"
    }


def test_concurrent_older_reconciliation_cannot_close_newer_finding(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    FrameworkRouteState(database).store_review_candidates(
        40,
        (_candidate("adult_content"),),
    )
    barrier = threading.Barrier(2)

    def reconcile_older_generation() -> None:
        barrier.wait()
        FrameworkRouteState(database).reconcile_review_candidates_batch(
            41,
            "image",
            (
                ReviewCandidateReconciliation(
                    snapshot=_snapshot(),
                    resolution_note="older detector generation completed",
                    evaluated_reason_codes=("adult_content",),
                ),
            ),
        )

    def store_newer_generation() -> None:
        barrier.wait()
        FrameworkRouteState(database).store_review_candidates(
            42,
            (_candidate("adult_content"),),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(reconcile_older_generation),
            executor.submit(store_newer_generation),
        )
        for future in futures:
            future.result()

    records = list_review_candidates(database, limit=10)
    assert len(records) == 1
    assert records[0].reason_code == "adult_content"
    assert records[0].last_detected_generation == 42


def test_human_review_decision_is_append_only_queryable_and_idempotent(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(21, (_candidate("document_candidate"),))
    decision = ReviewDecision(
        idempotency_key="review-ui:case-42:decision-1",
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        candidate_generation=21,
        source_status="done",
        recommendation="manual_review",
        retryable=False,
        confidence=0.9,
        evidence={"reason": "document_candidate"},
        detector_version="image-review-v1",
        status="confirmed",
        actor="victor",
        provenance={"source": "review-ui", "version": "1"},
        note="Confirmed rasterized document",
        decided_ns=123_456_789,
    )

    decision_id = route_state.record_review_decision(decision)
    assert route_state.record_review_decision(decision) == decision_id
    records = list_review_decisions(
        database,
        limit=10,
        route_name="image",
        reason_code="document_candidate",
        status="confirmed",
        volume_id=_snapshot().volume_id,
        file_id=_snapshot().file_id,
        candidate_generation=21,
    )
    assert len(records) == 1
    assert records[0].decision_id == decision_id
    assert records[0].actor == "victor"
    assert records[0].size == _snapshot().size
    assert records[0].mtime_ns == _snapshot().mtime_ns
    assert records[0].birthtime_ns == _snapshot().birthtime_ns
    assert records[0].source_status == "done"
    assert records[0].recommendation == "manual_review"
    assert records[0].retryable is False
    assert records[0].confidence == 0.9
    assert records[0].evidence == {"reason": "document_candidate"}
    assert records[0].detector_version == "image-review-v1"
    assert records[0].provenance == {"source": "review-ui", "version": "1"}
    assert records[0].decided_ns == 123_456_789
    assert records[0].recorded_ns > 0

    conflicting_snapshot_retry = ReviewDecision(
        idempotency_key=decision.idempotency_key,
        route_name=decision.route_name,
        snapshot=decision.snapshot,
        reason_code=decision.reason_code,
        candidate_generation=decision.candidate_generation,
        source_status=decision.source_status,
        recommendation=decision.recommendation,
        retryable=decision.retryable,
        confidence=decision.confidence,
        evidence={"reason": "changed-evidence"},
        detector_version="image-review-v2",
        status=decision.status,
        actor=decision.actor,
        provenance=decision.provenance,
        note=decision.note,
        decided_ns=decision.decided_ns,
    )
    with pytest.raises(ValueError, match="different decision candidate snapshot"):
        route_state.record_review_decision(conflicting_snapshot_retry)

    conflicting_retry = ReviewDecision(
        idempotency_key=decision.idempotency_key,
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        candidate_generation=21,
        source_status=decision.source_status,
        recommendation=decision.recommendation,
        retryable=decision.retryable,
        confidence=decision.confidence,
        evidence=decision.evidence,
        detector_version=decision.detector_version,
        status="dismissed",
        actor="victor",
        provenance=decision.provenance,
        decided_ns=decision.decided_ns,
    )
    with pytest.raises(ValueError, match="different decision"):
        route_state.record_review_decision(conflicting_retry)

    stale = ReviewDecision(
        idempotency_key="review-ui:case-42:stale",
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        candidate_generation=20,
        source_status=decision.source_status,
        recommendation=decision.recommendation,
        retryable=decision.retryable,
        confidence=decision.confidence,
        evidence=decision.evidence,
        detector_version=decision.detector_version,
        status="confirmed",
        actor="victor",
        provenance=decision.provenance,
        decided_ns=decision.decided_ns,
    )
    with pytest.raises(ValueError, match="stale"):
        route_state.record_review_decision(stale)


def test_decision_candidate_snapshot_is_immutable_after_finding_changes(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    original = _candidate("document_candidate")
    route_state.store_review_candidates(21, (original,))
    decision = ReviewDecision(
        idempotency_key="review-ui:case-43:decision-1",
        route_name=original.route_name,
        snapshot=original.snapshot,
        reason_code=original.reason_code,
        candidate_generation=21,
        source_status=original.source_status,
        recommendation=original.recommendation,
        retryable=original.retryable,
        confidence=original.confidence,
        evidence=original.evidence,
        detector_version=original.detector_version,
        status="confirmed",
        actor="victor",
        provenance={"source": "review-ui", "version": "1"},
        decided_ns=123_456_790,
    )
    decision_id = route_state.record_review_decision(decision)

    changed = ReviewCandidate(
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        source_status="partial",
        recommendation="retry",
        retryable=True,
        confidence=0.25,
        evidence={"reason": "transient_decode_failure"},
        detector_version="image-review-v2",
    )
    route_state.store_review_candidates(22, (changed,))
    assert (
        route_state.resolve_review_candidate_generation(
            22,
            "image",
            _snapshot(),
            "document_candidate",
            "finding resolved after human decision",
        )
        == 1
    )

    record = get_review_decision_by_key(database, decision.idempotency_key)
    assert record is not None
    assert record.decision_id == decision_id
    assert (
        record.source_status,
        record.recommendation,
        record.retryable,
        record.confidence,
        record.evidence,
        record.detector_version,
    ) == (
        "done",
        "manual_review",
        False,
        0.9,
        {"reason": "document_candidate"},
        "image-review-v1",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT evidence_json FROM review_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone() == ('{"reason":"document_candidate"}',)


def test_concurrent_identical_decision_retries_share_one_snapshot(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    candidate = _candidate("document_candidate")
    FrameworkRouteState(database).store_review_candidates(21, (candidate,))
    decision = ReviewDecision(
        idempotency_key="review-ui:case-43:concurrent-decision",
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
        status="confirmed",
        actor="victor",
        provenance={"source": "review-ui", "version": "1"},
        decided_ns=123_456_794,
    )
    barrier = threading.Barrier(2)

    def record_retry() -> int:
        barrier.wait()
        return FrameworkRouteState(database).record_review_decision(decision)

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision_ids = tuple(executor.map(lambda _: record_retry(), range(2)))

    assert decision_ids[0] == decision_ids[1]
    records = list_review_decisions(database, limit=10)
    assert len(records) == 1
    assert records[0].evidence == candidate.evidence


def test_decision_rejects_candidate_snapshot_changed_in_same_generation(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    original = _candidate("document_candidate")
    route_state.store_review_candidates(21, (original,))
    decision = ReviewDecision(
        idempotency_key="review-ui:case-44:decision-1",
        route_name=original.route_name,
        snapshot=original.snapshot,
        reason_code=original.reason_code,
        candidate_generation=21,
        source_status=original.source_status,
        recommendation=original.recommendation,
        retryable=original.retryable,
        confidence=original.confidence,
        evidence=original.evidence,
        detector_version=original.detector_version,
        status="confirmed",
        actor="victor",
        provenance={"source": "review-ui", "version": "1"},
        decided_ns=123_456_791,
    )
    route_state.store_review_candidates(
        21,
        (
            ReviewCandidate(
                route_name="image",
                snapshot=_snapshot(),
                reason_code="document_candidate",
                source_status="partial",
                recommendation="retry",
                retryable=True,
                confidence=0.2,
                evidence={"changed": True},
                detector_version="image-review-v2",
            ),
        ),
    )

    with pytest.raises(ValueError, match="snapshot changed"):
        route_state.record_review_decision(decision)
    assert list_review_decisions(database, limit=10) == []


def test_decision_candidate_evidence_is_bounded(tmp_path) -> None:
    candidate = _candidate("document_candidate")
    with pytest.raises(ValueError, match=f"{MAX_EVIDENCE_BYTES}-byte"):
        ReviewDecision(
            idempotency_key="review-ui:case-45:decision-1",
            route_name=candidate.route_name,
            snapshot=candidate.snapshot,
            reason_code=candidate.reason_code,
            candidate_generation=21,
            source_status=candidate.source_status,
            recommendation=candidate.recommendation,
            retryable=candidate.retryable,
            confidence=candidate.confidence,
            evidence={"payload": "x" * MAX_EVIDENCE_BYTES},
            detector_version=candidate.detector_version,
            status="confirmed",
            actor="victor",
            provenance={"source": "review-ui", "version": "1"},
            decided_ns=123_456_792,
        )


def test_decision_listing_and_exact_lookup_are_read_only(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    candidate = _candidate("document_candidate")
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(21, (candidate,))
    decision = ReviewDecision(
        idempotency_key="review-ui:case-46:decision-1",
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
        status="deferred",
        actor="victor",
        provenance={"source": "review-ui", "version": "1"},
        decided_ns=123_456_793,
    )
    route_state.record_review_decision(decision)
    before = database.read_bytes()

    assert len(list_review_decisions(database, limit=10)) == 1
    assert get_review_decision_by_key(database, decision.idempotency_key) is not None
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("filters", "message"),
    (
        ({"limit": 0}, "review decision limit must be between 1 and 10000"),
        ({"limit": 10_001}, "review decision limit must be between 1 and 10000"),
        ({"limit": 10, "route_name": ""}, "review route_name must be non-empty"),
        (
            {"limit": 10, "route_name": " image"},
            "review route_name must be non-empty and trimmed",
        ),
        (
            {"limit": 10, "route_name": "image "},
            "review route_name must be non-empty and trimmed",
        ),
        (
            {"limit": 10, "route_name": "r" * 257},
            "review route_name exceeds 256 characters",
        ),
        ({"limit": 10, "reason_code": ""}, "review reason_code must be non-empty"),
        (
            {"limit": 10, "reason_code": "document_candidate "},
            "review reason_code must be non-empty and trimmed",
        ),
        (
            {"limit": 10, "reason_code": "r" * 257},
            "review reason_code exceeds 256 characters",
        ),
        (
            {"limit": 10, "status": "open"},
            "invalid review decision status: open",
        ),
        (
            {"limit": 10, "volume_id": 0xAA},
            "review identity filtering requires volume_id and file_id",
        ),
        (
            {"limit": 10, "file_id": 0xBB},
            "review identity filtering requires volume_id and file_id",
        ),
        (
            {"limit": 10, "candidate_generation": -1},
            "review candidate_generation must be non-negative",
        ),
        (
            {"limit": 10, "candidate_generation": True},
            "review candidate_generation must be non-negative",
        ),
    ),
)
def test_decision_listing_validates_filters_before_opening_state(
    tmp_path,
    filters: dict[str, Any],
    message: str,
) -> None:
    database = tmp_path / "missing.sqlite3"

    with pytest.raises(ValueError, match=message):
        list_review_decisions(database, **filters)

    assert not database.exists()


def test_decision_listing_filters_and_tie_break_order_are_exact(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    decision_ids: list[int] = []
    cases: tuple[tuple[str, str, ReviewDecisionStatus, int, int, int], ...] = (
        ("image", "document_candidate", "confirmed", 21, 0xA1, 0xB1),
        ("pdf", "corrupt_container", "dismissed", 22, 0xA2, 0xB2),
        ("image", "adult_content", "deferred", 23, 0xA3, 0xB3),
        ("image", "document_candidate", "confirmed", 24, 0xA4, 0xB4),
    )
    for index, (route_name, reason_code, status, generation, volume_id, file_id) in enumerate(
        cases,
        start=1,
    ):
        snapshot = FileSnapshot(
            f"C:\\corpus\\case-{index}.pdf",
            volume_id,
            file_id,
            1_000 + index,
            2_000 + index,
            3_000 + index,
        )
        candidate = ReviewCandidate(
            route_name=route_name,
            snapshot=snapshot,
            reason_code=reason_code,
            source_status="done",
            recommendation="manual_review",
            retryable=False,
            confidence=0.9,
            evidence={"case": index},
            detector_version="review-filter-characterization-v1",
        )
        route_state.store_review_candidates(generation, (candidate,))
        decision_ids.append(
            route_state.record_review_decision(
                ReviewDecision(
                    idempotency_key=f"review-filter-characterization:{index}",
                    route_name=route_name,
                    snapshot=snapshot,
                    reason_code=reason_code,
                    candidate_generation=generation,
                    source_status=candidate.source_status,
                    recommendation=candidate.recommendation,
                    retryable=candidate.retryable,
                    confidence=candidate.confidence,
                    evidence=candidate.evidence,
                    detector_version=candidate.detector_version,
                    status=status,
                    actor="victor",
                    provenance={"source": "filter-characterization"},
                    decided_ns=10_000 + index,
                )
            )
        )

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE review_decisions SET recorded_ns=123456789")
    before = database.read_bytes()

    def ids(**filters: Any) -> list[int]:
        return [
            record.decision_id for record in list_review_decisions(database, limit=10, **filters)
        ]

    expected = list(reversed(decision_ids))
    assert ids() == expected
    assert [record.decision_id for record in list_review_decisions(database, limit=2)] == expected[
        :2
    ]
    assert ids(route_name="image") == [decision_ids[3], decision_ids[2], decision_ids[0]]
    assert ids(reason_code="document_candidate") == [decision_ids[3], decision_ids[0]]
    assert ids(status="dismissed") == [decision_ids[1]]
    assert ids(volume_id=0xA3, file_id=0xB3) == [decision_ids[2]]
    assert ids(candidate_generation=21) == [decision_ids[0]]
    assert ids(
        route_name="image",
        reason_code="document_candidate",
        status="confirmed",
        volume_id=0xA4,
        file_id=0xB4,
        candidate_generation=24,
    ) == [decision_ids[3]]
    assert ids(route_name="audio") == []
    assert database.read_bytes() == before


def test_decision_waiting_on_resolution_rejects_closed_candidate(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    route_state = FrameworkRouteState(database)
    route_state.store_review_candidates(22, (_candidate("document_candidate"),))
    decision = ReviewDecision(
        idempotency_key="review-cli:race:decision-1",
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        candidate_generation=22,
        source_status="done",
        recommendation="manual_review",
        retryable=False,
        confidence=0.9,
        evidence={"reason": "document_candidate"},
        detector_version="image-review-v1",
        status="confirmed",
        actor="victor",
        provenance={"source": "review-cli", "version": "1"},
        decided_ns=222,
    )
    writer_locked = threading.Event()
    decision_started = threading.Event()

    def resolve_in_open_transaction() -> None:
        connection = sqlite3.connect(database, timeout=60)
        try:
            connection.execute("PRAGMA busy_timeout=60000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE review_candidates SET status='resolved',resolved_ns=1,
                resolved_run_id=22,resolution_note='resolved concurrently'
                WHERE route_name='image' AND volume_id='aa' AND file_id='bb'
                AND reason_code='document_candidate'"""
            )
            writer_locked.set()
            assert decision_started.wait(5)
            connection.commit()
        finally:
            connection.close()

    def record_waiting_decision() -> int:
        assert writer_locked.wait(5)
        decision_started.set()
        return FrameworkRouteState(database).record_review_decision(decision)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolving = executor.submit(resolve_in_open_transaction)
        deciding = executor.submit(record_waiting_decision)
        resolving.result()
        with pytest.raises(ValueError, match="no longer open"):
            deciding.result()

    assert list_review_decisions(database, limit=10) == []


def test_schema_14_migration_preserves_legacy_decision_and_adds_snapshot(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO metadata(key,value) VALUES('schema_version','14');
        CREATE TABLE review_decisions (
            decision_id INTEGER PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            route_name TEXT NOT NULL,
            volume_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            candidate_generation INTEGER NOT NULL,
            path TEXT NOT NULL COLLATE NOCASE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            birthtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL,
            actor TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            note TEXT,
            decided_ns INTEGER NOT NULL,
            recorded_ns INTEGER NOT NULL
        );
        INSERT INTO review_decisions VALUES(
            7,'legacy-review-key','image','aa','bb','document_candidate',21,
            'C:\\corpus\\damaged.pdf',1234,5678,4567,'confirmed','victor',
            '{"source":"legacy-ui"}','preserve me',111,222
        );
        """
    )
    connection.commit()
    connection.close()

    with FrameworkState(database):
        pass

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert connection.execute(
            """SELECT decision_id,idempotency_key,note,source_status,
            recommendation,retryable,confidence,evidence_json,detector_version
            FROM review_decisions"""
        ).fetchone() == (
            7,
            "legacy-review-key",
            "preserve me",
            None,
            None,
            None,
            None,
            None,
            None,
        )
    finally:
        connection.close()

    legacy_retry = ReviewDecision(
        idempotency_key="legacy-review-key",
        route_name="image",
        snapshot=_snapshot(),
        reason_code="document_candidate",
        candidate_generation=21,
        source_status="done",
        recommendation="manual_review",
        retryable=False,
        confidence=0.9,
        evidence={"reason": "not-recorded-in-schema-14"},
        detector_version="image-review-v1",
        status="confirmed",
        actor="victor",
        provenance={"source": "legacy-ui"},
        note="preserve me",
        decided_ns=111,
    )
    # The v14 row remains explicitly snapshot-unavailable; its historical
    # identity can be retried without inventing evidence that was never stored.
    assert FrameworkRouteState(database).record_review_decision(legacy_retry) == 7

    records = list_review_decisions(database, limit=10)
    assert len(records) == 1
    assert records[0].decision_id == 7
    assert records[0].size == 1234
    assert records[0].source_status is None
    assert records[0].evidence is None


def test_schema_13_migration_preserves_findings_and_adds_feedback(tmp_path) -> None:
    database = tmp_path / "framework.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        INSERT INTO metadata(key,value) VALUES('schema_version','13');
        CREATE TABLE review_candidates (
            route_name TEXT NOT NULL, volume_id TEXT NOT NULL, file_id TEXT NOT NULL,
            reason_code TEXT NOT NULL, path TEXT NOT NULL COLLATE NOCASE,
            size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            birthtime_ns INTEGER NOT NULL, source_status TEXT NOT NULL,
            recommendation TEXT NOT NULL, retryable INTEGER NOT NULL,
            confidence REAL NOT NULL, evidence_json TEXT NOT NULL,
            detector_version TEXT NOT NULL, status TEXT NOT NULL,
            first_detected_ns INTEGER NOT NULL, last_detected_ns INTEGER NOT NULL,
            last_seen_run_id INTEGER NOT NULL, resolved_ns INTEGER,
            resolution_note TEXT,
            PRIMARY KEY(route_name,volume_id,file_id,reason_code)
        ) WITHOUT ROWID;
        INSERT INTO review_candidates VALUES(
            'image','aa','bb','document_candidate','C:\\corpus\\damaged.pdf',
            1234,5678,4567,'done','manual_review',0,0.9,'{}',
            'image-review-v1','open',1,2,13,NULL,NULL
        );
        """
    )
    connection.commit()
    connection.close()

    with FrameworkState(database):
        pass

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (str(SCHEMA_VERSION),)
        assert connection.execute(
            "SELECT reason_code,last_seen_run_id FROM review_candidates"
        ).fetchone() == ("document_candidate", 13)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(review_candidates)")}
        assert "resolved_run_id" in columns
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='review_decisions'"
        ).fetchone() == ("review_decisions",)
    finally:
        connection.close()


def test_schema_16_migration_rejects_unknown_columns_without_losing_them(
    tmp_path,
) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE review_candidates ADD COLUMN external_note TEXT")
        connection.execute(
            """INSERT INTO review_candidates(
            route_name,volume_id,file_id,reason_code,path,size,mtime_ns,birthtime_ns,
            source_status,recommendation,retryable,confidence,evidence_json,
            detector_version,status,first_detected_ns,last_detected_ns,last_seen_run_id,
            external_note) VALUES(
            'image','volume','file','document_candidate','C:\\corpus\\source.pdf',
            10,20,30,'done','manual_review',0,0.9,'{}','legacy-v16','open',
            1,2,16,'must survive')"""
        )
        connection.execute("UPDATE metadata SET value='16' WHERE key='schema_version'")

    with pytest.raises(RuntimeError, match="unexpected legacy column layout"):
        FrameworkState(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == ("16",)
        assert connection.execute("SELECT external_note FROM review_candidates").fetchone() == (
            "must survive",
        )
        assert "external_note" in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(review_candidates)")
        }
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='__neocortex_schema_17_review_candidates'"
            ).fetchone()
            is None
        )


# endregion [02]


# region [03] Direct command


def test_review_cli_lists_candidate_without_mutating_state(tmp_path, capsys) -> None:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    FrameworkRouteState(database).store_review_candidates(
        1,
        (
            ReviewCandidate(
                route_name="image",
                snapshot=_snapshot(),
                reason_code="truncated_content_unrecoverable",
                source_status="error",
                recommendation="deletion_candidate",
                retryable=False,
                confidence=0.95,
                evidence={"decode": "strict_and_tolerant_failed"},
                detector_version="image-decode-v1",
            ),
        ),
    )
    args = argparse.Namespace(
        state_directory=tmp_path,
        review_candidates=5,
        review_route="image",
        review_recommendation="deletion_candidate",
        review_status="open",
    )

    assert run_review_candidates(args) == 0
    output = capsys.readouterr().out
    assert "recommendation=deletion_candidate" in output
    assert "reason=truncated_content_unrecoverable" in output
    assert _snapshot().path in output


# endregion [03]
