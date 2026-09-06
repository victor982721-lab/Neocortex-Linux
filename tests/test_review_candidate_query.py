"""Legacy candidates remain visible through explicit bounded queue envelopes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.review.review import ReviewCandidate
from neocortex.workflow.review.review_candidate_query import (
    ReviewCandidateListCursor,
    list_review_candidates_page,
)


def _database(tmp_path: Path, count: int = 3) -> Path:
    database = tmp_path / "framework.sqlite3"
    with FrameworkState(database):
        pass
    candidates = tuple(ReviewCandidate(
        route_name="pdf", snapshot=FileSnapshot(f"/fixture/{index}.pdf", 1, index + 1, 100, 10, -1),
        reason_code="protected", source_status="protected", recommendation="keep_protected",
        retryable=False, confidence=0.9, evidence={"encrypted": True}, detector_version="fixture-v1",
    ) for index in range(count))
    if candidates:
        FrameworkRouteState(database).store_review_candidates(1, candidates)
    return database


def test_page_preserves_legacy_candidates_exact_counts_and_cursor(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = list_review_candidates_page(database, limit=2)
    assert first.availability == "ready"
    assert len(first.items) == 2 and first.total_matching == 3 and first.has_more
    assert first.next_cursor is not None
    recommendation = first.to_dict()["items"][0]["recommendation_detail"]  # type: ignore[index]
    assert recommendation["evidence_refs"] and recommendation["missing_checks"]
    assert recommendation["preconditions"] and recommendation["executable"] is False
    token = first.next_cursor.to_token()
    assert ReviewCandidateListCursor.from_token(token) == first.next_cursor
    second = list_review_candidates_page(database, limit=2, after=token)
    assert second.availability == "ready" and second.total_matching == 3
    assert len(second.items) == 1 and not second.has_more
    assert {item.file_id for item in (*first.items, *second.items)} == {1, 2, 3}
    assert second.to_dict()["complete"] is False
    assert list_review_candidates_page(database, limit=2).to_dict() == first.to_dict()


def test_empty_absent_and_failed_are_not_conflated(tmp_path: Path) -> None:
    empty = list_review_candidates_page(_database(tmp_path, 0), limit=2)
    assert empty.availability == "ready" and empty.total_matching == 0
    absent_path = tmp_path / "absent.sqlite3"
    absent = list_review_candidates_page(absent_path, limit=2)
    assert absent.availability == "absent" and absent.total_matching is None
    assert not absent_path.exists()
    broken_path = tmp_path / "broken.sqlite3"
    broken_path.write_bytes(b"not sqlite")
    failed = list_review_candidates_page(broken_path, limit=2)
    assert failed.availability == "failed" and failed.total_matching is None


def test_cursor_rejects_new_filters_and_changed_owner(tmp_path: Path) -> None:
    database = _database(tmp_path)
    page = list_review_candidates_page(database, limit=1)
    assert page.next_cursor is not None
    with pytest.raises(ValueError, match="filters"):
        list_review_candidates_page(database, limit=1, route_name="text", after=page.next_cursor)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE review_candidates SET confidence=0.8 WHERE file_id='1'")
    connection.commit()
    connection.close()
    changed = list_review_candidates_page(database, limit=1, after=page.next_cursor)
    assert changed.availability == "snapshot_changed"
    assert changed.items == () and changed.total_matching is None


def test_invalid_cursor_cannot_turn_a_score_into_probability() -> None:
    cursor = ReviewCandidateListCursor("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                                       1, 0.8, "pdf", "/fixture/one.pdf", "reason", "1", "2")
    with pytest.raises(ValueError):
        replace(cursor, confidence=float("nan"))
    with pytest.raises(ValueError):
        ReviewCandidateListCursor.from_token(cursor.to_token() + "junk")
