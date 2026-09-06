"""Only exact relationships group typed reviews; all decisions stay intact."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.knowledge.knowledge_contracts import EvidenceMethod, EvidenceRef
from neocortex.workflow.review.review_task_contracts import ReviewTaskListCursor, ReviewTaskRecordPage
from neocortex.workflow.review.review_task_grouping import (
    ReviewRelationshipMember,
    VerifiedReviewRelationship,
    group_review_task_page,
)
from neocortex.workflow.review.review_task_query import (
    ReviewTaskReadQuery, ReviewTaskReadResult, query_current_review_tasks,
)
from neocortex.workflow.review.review_task_repository import publish_review_task_page
from test_review_tasks import _database, _publication


def _page(tmp_path: Path) -> ReviewTaskRecordPage:
    database = _database(tmp_path)
    publish_review_task_page(database, _publication(1, (1, 2, 3)), expected_progress_revision=None)
    return query_current_review_tasks(database, ReviewTaskReadQuery(limit=10)).page


def _relation(page: ReviewTaskRecordPage) -> VerifiedReviewRelationship:
    members = tuple(sorted(ReviewRelationshipMember(
        record.source.resource.resource_id, record.source.revision.revision_id,
        record.source_snapshot_fingerprint,
    ) for record in page.items[:2]))  # type: ignore[union-attr]
    evidence = tuple(EvidenceRef(
        evidence_id=f"evidence:relation:{index}", resource_id=member.resource_id,
        revision_id=member.revision_id, method=EvidenceMethod.STRUCTURAL,
        identifiers=(("relationship_id", "document:bundle:1"),),
    ) for index, member in enumerate(members))
    return VerifiedReviewRelationship("document:bundle:1", "document_bundle", members, evidence)


def test_reasons_and_priority_alone_do_not_group(tmp_path: Path) -> None:
    page = _page(tmp_path)
    groups = group_review_task_page(page)
    assert len(groups) == 3
    assert all(len(group.members) == 1 for group in groups)


def test_verified_relationship_groups_without_losing_members_snapshots_or_decisions(tmp_path: Path) -> None:
    page = _page(tmp_path)
    relation = _relation(page)
    before = tuple(record.current_event.to_dict() for record in page.items)
    groups = group_review_task_page(page, relationships=(relation,))
    assert [len(group.members) for group in groups] == [2, 1]
    grouped = groups[0].to_dict()
    assert grouped["human_decision_generated"] is False and grouped["mutation_authorized"] is False
    assert "not_probability_or_authority" in grouped["score_semantics"]
    assert tuple(record.current_event.to_dict() for group in groups for record in group.members) == before
    assert tuple(record for group in groups for record in group.members) == page.items
    assert group_review_task_page(page, relationships=(relation,)) == groups


def test_relation_requires_every_revision_snapshot_and_structural_proof(tmp_path: Path) -> None:
    page = _page(tmp_path)
    relation = _relation(page)
    inferred = tuple(replace(item, method=EvidenceMethod.INFERRED) for item in relation.evidence)
    with pytest.raises(ValueError, match="structural"):
        replace(relation, evidence=inferred)
    with pytest.raises(ValueError, match="every exact member"):
        replace(relation, evidence=relation.evidence[:1])
    old = tuple(replace(item, source_snapshot_fingerprint=
                        "review-task-source-snapshot-v1:sha256:" + "a" * 64) for item in relation.members)
    groups = group_review_task_page(page, relationships=(replace(relation, members=old),))
    assert len(groups) == 3


def test_matching_relationship_does_not_merge_different_reasons(tmp_path: Path) -> None:
    page = _page(tmp_path)
    relation = _relation(page)
    changed = replace(page.items[1], task=replace(page.items[1].task, reason_code="other_reason"))
    groups = group_review_task_page(replace(page, items=(page.items[0], changed, page.items[2])),
                                   relationships=(relation,))
    assert len(groups) == 3


def test_page_counts_do_not_claim_an_empty_queue_from_an_empty_suffix() -> None:
    cursor = ReviewTaskListCursor(0.5, 123, "task:cursor")
    suffix = ReviewTaskReadResult(ReviewTaskReadQuery(after=cursor), ReviewTaskRecordPage((), None))
    assert suffix.to_dict()["counts"]["returned"] == 0  # type: ignore[index]
    assert suffix.total_matching is None and suffix.to_dict()["complete"] is False
    empty = ReviewTaskReadResult(ReviewTaskReadQuery(), ReviewTaskRecordPage((), None))
    assert empty.total_matching == 0 and empty.to_dict()["complete"] is True
