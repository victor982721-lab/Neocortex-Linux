"""Read-only grouping by explicitly evidenced documentary relationships.

Names, adjacent paths, priority scores, and matching reasons alone do not prove
that files belong together. Groups retain every task and its source snapshot;
grouping does not synthesize human decisions or change any ReviewTask state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from neocortex.knowledge.knowledge_contracts import EvidenceMethod, EvidenceRef

from .review_task_contracts import ReviewTaskRecord, ReviewTaskRecordPage


@dataclass(frozen=True, slots=True, order=True)
class ReviewRelationshipMember:
    resource_id: str
    revision_id: str
    source_snapshot_fingerprint: str

    def __post_init__(self) -> None:
        from .review_task_contracts import _validate_source_snapshot_fingerprint

        for value in (self.resource_id, self.revision_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError("relationship members require bounded resource/revision identifiers")
        _validate_source_snapshot_fingerprint(self.source_snapshot_fingerprint)

    def to_dict(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "revision_id": self.revision_id,
            "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VerifiedReviewRelationship:
    relationship_id: str
    relation_kind: str
    members: tuple[ReviewRelationshipMember, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        for value in (self.relationship_id, self.relation_kind):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError("relationship requires bounded non-blank identifiers")
        if not isinstance(self.members, tuple) or not 2 <= len(self.members) <= 100 or any(
            not isinstance(item, ReviewRelationshipMember) for item in self.members
        ) or tuple(sorted(set(self.members))) != self.members:
            raise ValueError("relationship requires 2-100 unique sorted typed members")
        if len({item.resource_id for item in self.members}) != len(self.members):
            raise ValueError("a relationship cannot substitute multiple revisions for distinct members")
        if not isinstance(self.evidence, tuple) or not 1 <= len(self.evidence) <= 100 or any(
            not isinstance(item, EvidenceRef) or item.method not in {
                EvidenceMethod.STRUCTURAL, EvidenceMethod.HUMAN_CONFIRMED,
            }
            for item in self.evidence
        ):
            raise ValueError("verified relationships require structural or human-confirmed evidence")
        locators = {(item.resource_id, item.revision_id) for item in self.evidence}
        if any((item.resource_id, item.revision_id) not in locators for item in self.members):
            raise ValueError("relationship evidence must cover every exact member revision")
        # The asserted relation itself, not merely each document's existence,
        # must be present in the evidence's typed identifiers.
        if any(("relationship_id", self.relationship_id) not in item.identifiers
               for item in self.evidence):
            raise ValueError("each evidence reference must identify the verified relationship")

    def to_dict(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "relation_kind": self.relation_kind,
            "members": [item.to_dict() for item in self.members],
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ReviewTaskGroup:
    group_id: str
    reason_code: str
    members: tuple[ReviewTaskRecord, ...]
    relationships: tuple[VerifiedReviewRelationship, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not self.members or any(item.task.reason_code != self.reason_code for item in self.members):
            raise ValueError("group members must preserve one common reason")
        if len({item.task.scope for item in self.members}) != 1:
            raise ValueError("groups cannot cross review scopes")
        if len({item.task.task_id for item in self.members}) != len(self.members):
            raise ValueError("group members cannot repeat")
        if len(self.members) > 1 and not self.relationships:
            raise ValueError("multi-member groups require verified relationships")
        if not isinstance(self.complete, bool):
            raise ValueError("group completeness must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "reason_code": self.reason_code,
            "members": [{
                "task": record.task.to_dict(),
                "source": record.source.to_dict(),
                "source_snapshot_fingerprint": record.source_snapshot_fingerprint,
                "current_event": record.current_event.to_dict(),
                "state": record.state.value,
                "batch_id": record.batch_id,
            } for record in self.members],
            "relationships": [item.to_dict() for item in self.relationships],
            "member_count": len(self.members),
            "complete": self.complete,
            "grouping_basis": "verified_relationship_and_reason" if self.relationships else "singleton",
            "score_semantics": "member_priority_not_probability_or_authority",
            "mutation_authorized": False,
            "human_decision_generated": False,
        }


def _member(record: ReviewTaskRecord) -> ReviewRelationshipMember | None:
    source = record.source
    if source.resource is None or source.revision is None:
        return None
    return ReviewRelationshipMember(
        source.resource.resource_id, source.revision.revision_id, record.source_snapshot_fingerprint
    )


def group_review_task_page(
    page: ReviewTaskRecordPage,
    *,
    relationships: tuple[VerifiedReviewRelationship, ...] = (),
    complete_scope: bool | None = None,
) -> tuple[ReviewTaskGroup, ...]:
    """Partition a bounded page; never claim that page-local groups are global."""

    if not isinstance(page, ReviewTaskRecordPage):
        raise TypeError("page must be a ReviewTaskRecordPage")
    if complete_scope is not None and not isinstance(complete_scope, bool):
        raise ValueError("complete_scope must be boolean when provided")
    if not isinstance(relationships, tuple) or len(relationships) > 100 or any(
        not isinstance(item, VerifiedReviewRelationship) for item in relationships
    ):
        raise ValueError("relationships must be a bounded typed immutable tuple")
    if len({item.relationship_id for item in relationships}) != len(relationships):
        raise ValueError("relationship identifiers cannot repeat")
    records = page.items
    parents = list(range(len(records)))
    observed_members = {_member(item) for item in records}
    links: dict[int, set[int]] = {index: set() for index in range(len(records))}

    def root(index: int) -> int:
        while parents[index] != index:
            index = parents[index]
        return index

    for relationship_index, relation in enumerate(relationships):
        by_reason: dict[tuple[str, str], list[int]] = {}
        for index, record in enumerate(records):
            if _member(record) in relation.members:
                by_reason.setdefault((record.task.scope, record.task.reason_code), []).append(index)
                links[index].add(relationship_index)
        for indices in by_reason.values():
            for index in indices[1:]:
                parents[root(index)] = root(indices[0])
    partitions: dict[int, list[int]] = {}
    for index in range(len(records)):
        partitions.setdefault(root(index), []).append(index)
    groups: list[ReviewTaskGroup] = []
    for indices in partitions.values():
        members = tuple(records[index] for index in indices)
        relation_indices = set().union(*(links[index] for index in indices))
        relations = tuple(sorted((relationships[index] for index in relation_indices),
                                 key=lambda item: item.relationship_id))
        payload = {
            "reason": members[0].task.reason_code,
            "tasks": [(item.task.task_id, item.source_snapshot_fingerprint) for item in members],
            "relations": [item.to_dict() for item in relations],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode()).hexdigest()
        complete = complete_scope is not False and not page.has_more and all(
            item in observed_members for relation in relations for item in relation.members
        )
        groups.append(ReviewTaskGroup(
            "review-group:" + digest, members[0].task.reason_code, members, relations, complete,
        ))
    return tuple(groups)


__all__ = [
    "ReviewRelationshipMember", "ReviewTaskGroup", "VerifiedReviewRelationship",
    "group_review_task_page",
]
