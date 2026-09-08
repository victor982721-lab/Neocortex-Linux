"""Current Archive-owner evidence for ReviewTask documentary relationships.

Member and logical-document keys come from published structural columns, never
from parsing names. The member ResourceRef stays virtual; a container is only
its physical anchor, not an invented physical identity for a component.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3

from neocortex.capabilities.formats.archive.logical import (
    identify_logical_document,
    issue_diagnosis,
)
from neocortex.capabilities.formats.archive.state import (
    ARCHIVE_SCHEMA_VERSION,
    archive_database,
    archive_schema_contract,
)
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.persistence.sqlite_immutable import capture_sqlite_read_fence
from neocortex.persistence.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)

from .review_task_contracts import (
    CanonicalJsonObject,
    ReviewTaskInput,
    ReviewTaskRecordPage,
    ReviewTaskSourceFence,
)
from .review_task_grouping import ReviewRelationshipMember, VerifiedReviewRelationship


ARCHIVE_REVIEW_TASK_TYPE = "archive-document-review"
ARCHIVE_REVIEW_PRODUCER = "archive-review-source-v1"
ARCHIVE_REVIEW_SELECTOR = "archive-published-issues-v1"
MAX_ARCHIVE_REVIEW_PAGE = 100


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveReviewIssueSource:
    issue_id: int
    reason_code: str
    source: ReviewTaskInput
    projection: CanonicalJsonObject
    relationship_id: str | None
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class ArchiveReviewSource:
    owner_snapshot_id: str
    issues: tuple[ArchiveReviewIssueSource, ...]
    has_more: bool

    def fence(self, scope: str) -> ReviewTaskSourceFence:
        return ReviewTaskSourceFence.create(
            scope=scope,
            task_type=ARCHIVE_REVIEW_TASK_TYPE,
            selector_signature=ARCHIVE_REVIEW_SELECTOR,
            source_snapshot={
                "schema": "neocortex.archive-review-source/v1",
                "owner": "archive",
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "owner_snapshot_id": self.owner_snapshot_id,
            },
        )


_SOURCE_SQL = """SELECT i.issue_id,i.reason_code,i.member_chain AS issue_member_chain,
NOT EXISTS(SELECT 1 FROM archive_issues prior WHERE prior.container_key=i.container_key
AND prior.member_chain IS i.member_chain AND prior.reason_code=i.reason_code
AND prior.issue_id<i.issue_id) AS primary_issue,
i.container_key,c.path AS container_path,c.size AS container_size,c.mtime_ns AS container_mtime_ns,
c.birthtime_ns AS container_birthtime_ns,c.processing_signature AS container_signature,
c.last_seen_run_id AS container_run_id,c.status AS container_status,
d.file_key,d.path,d.member_chain,d.document_role,d.logical_document_chain,
d.size,d.mtime_ns,d.birthtime_ns,d.processing_signature,d.status,d.crc32,d.text_xxh3_128,
l.declared_mime,l.logical_kind,l.proposed_extension,l.evidence_json,l.identification_status
FROM archive_issues i JOIN containers c USING(container_key)
LEFT JOIN documents d ON d.container_key=i.container_key
AND d.member_chain=COALESCE(i.member_chain,'')
LEFT JOIN archive_logical_documents l ON l.container_key=d.container_key
AND l.member_chain=d.logical_document_chain
WHERE c.status IN ('complete','partial')"""


def _issue_source(row: sqlite3.Row, owner_snapshot_id: str) -> ArchiveReviewIssueSource:
    projection = dict(row)
    marker_payload = projection.pop("evidence_json")
    relationship_id = None
    if marker_payload is not None:
        if not isinstance(marker_payload, str) or len(marker_payload) > 65536:
            raise ValueError("archive relationship evidence exceeds its bound")
        markers = json.loads(marker_payload)
        if (
            not isinstance(markers, list)
            or len(markers) > 32
            or any(not isinstance(marker, str) for marker in markers)
        ):
            raise ValueError("archive relationship markers are invalid")
        identified = identify_logical_document(tuple(markers), row["declared_mime"])
        if (
            row["document_role"] in {"logical_document", "document_component"}
            and row["logical_document_chain"] is not None
            and row["processing_signature"] == row["container_signature"]
            and row["mtime_ns"] == row["container_mtime_ns"]
            and row["birthtime_ns"] == row["container_birthtime_ns"]
            and row["identification_status"] == "identified"
            and identified is not None
            and identified.identified
            and identified.logical_kind == row["logical_kind"]
        ):
            relation = {
                "container_key": row["container_key"],
                "container_signature": row["container_signature"],
                "logical_document_chain": row["logical_document_chain"],
                "logical_kind": row["logical_kind"],
                "markers": markers,
                "owner_snapshot_id": owner_snapshot_id,
            }
            relationship_id = "archive-review-relation:" + _digest(relation)
        projection["structural_markers"] = markers
    projection["owner_snapshot_id"] = owner_snapshot_id
    coverage_impact, recovery = issue_diagnosis(str(row["reason_code"]))
    projection.update(
        {"coverage_impact": coverage_impact, "recoverability": recovery, "effect_authorized": False}
    )
    issue_id = int(row["issue_id"])
    source_digest = _digest(projection)
    resource = None
    revision = None
    evidence: tuple[EvidenceRef, ...] = ()
    if row["file_key"] is not None:
        resource = ResourceRef(
            f"resource:archive:{row['file_key']}",
            "archive",
            "archive",
            current_path=str(row["path"]),
        )
        revision_payload = {
            key: projection[key]
            for key in (
                "file_key",
                "container_key",
                "container_signature",
                "container_size",
                "container_mtime_ns",
                "container_birthtime_ns",
                "size",
                "mtime_ns",
                "birthtime_ns",
                "processing_signature",
                "crc32",
                "text_xxh3_128",
                "document_role",
                "logical_document_chain",
            )
        }
        revision = RevisionRef(
            resource.resource_id,
            "revision:archive-review:" + _digest(revision_payload),
            ARCHIVE_REVIEW_PRODUCER,
            str(row["processing_signature"]),
            int(row["container_run_id"]),
            RevisionState.CURRENT,
        )
        identifiers: tuple[tuple[str, str], ...] = (
            ("owner", "archive"),
            ("archive_issue_id", str(issue_id)),
            ("archive_owner_snapshot", owner_snapshot_id),
        )
        if relationship_id is not None:
            identifiers += (("relationship_id", relationship_id),)
        evidence = (
            EvidenceRef(
                "evidence:archive-review:" + source_digest,
                resource.resource_id,
                revision.revision_id,
                EvidenceMethod.STRUCTURAL,
                identifiers=identifiers,
                extractor=ARCHIVE_REVIEW_PRODUCER,
            ),
        )
    source = ReviewTaskInput(
        f"archive-issue:{issue_id}", "sha256", source_digest, resource, revision
    )
    return ArchiveReviewIssueSource(
        issue_id,
        str(row["reason_code"]),
        source,
        CanonicalJsonObject.from_mapping(projection),
        relationship_id,
        evidence,
    )


def read_archive_review_source(
    path: Path,
    *,
    after_issue_id: int = 0,
    issue_ids: tuple[int, ...] | None = None,
) -> ArchiveReviewSource:
    """Read at most 100 actual issues from a sidecar-safe, unchanged owner view."""

    if type(after_issue_id) is not int or after_issue_id < 0:
        raise ValueError("after_issue_id must be a nonnegative integer")
    if issue_ids is not None and (
        not isinstance(issue_ids, tuple)
        or len(issue_ids) > 100
        or any(type(value) is not int or value <= 0 for value in issue_ids)
    ):
        raise ValueError("issue_ids must be a bounded tuple of positive integers")
    before = capture_sqlite_read_fence(path)
    snapshot_id = "sha256:" + _digest(asdict(before))
    with archive_database(path, readonly=True) as connection:
        connection.execute("BEGIN")
        if read_application_schema_version(connection, label="archive") != ARCHIVE_SCHEMA_VERSION:
            raise ValueError("archive review source schema is incompatible")
        validate_sqlite_schema_contract(
            connection, archive_schema_contract(), label="archive", exact=True
        )
        if issue_ids is None:
            rows = connection.execute(
                _SOURCE_SQL + " AND i.issue_id>? ORDER BY i.issue_id LIMIT 101", (after_issue_id,)
            ).fetchall()
        elif issue_ids:
            rows = connection.execute(
                _SOURCE_SQL
                + " AND i.issue_id IN ("
                + ",".join("?" for _ in issue_ids)
                + ") ORDER BY i.issue_id LIMIT 101",
                issue_ids,
            ).fetchall()
        else:
            rows = []
        issues = tuple(_issue_source(row, snapshot_id) for row in rows[:100])
    if capture_sqlite_read_fence(path) != before:
        raise RuntimeError("archive_review_source_changed")
    return ArchiveReviewSource(snapshot_id, issues, len(rows) > 100)


@dataclass(frozen=True, slots=True)
class ReviewRelationshipResolution:
    relationships: tuple[VerifiedReviewRelationship, ...] = ()
    verified_task_ids: tuple[str, ...] = ()
    unresolved: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": "returned_review_tasks",
            "complete": not self.unresolved,
            "verified_members": len(self.verified_task_ids),
            "unresolved_members": len(self.unresolved),
            "unresolved": [{"task_id": task, "reason": reason} for task, reason in self.unresolved],
            "source": "published_archive_owner",
            "mutation_authorized": False,
        }


def resolve_review_relationships(
    path: Path,
    page: ReviewTaskRecordPage,
    *,
    database: Path,
) -> ReviewRelationshipResolution:
    """Join current owner facts to exact published task inputs, never task labels."""

    unresolved: dict[str, str] = {}
    selected: dict[str, int] = {}
    for record in page.items:
        task_id = record.task.task_id
        if (
            record.task.task_type != ARCHIVE_REVIEW_TASK_TYPE
            or not record.source.input_id.startswith("archive-issue:")
        ):
            unresolved[task_id] = "relationship_source_not_published"
            continue
        number = record.source.input_id.removeprefix("archive-issue:")
        if (
            not number.isascii()
            or not number.isdecimal()
            or str(int(number)) != number
            or int(number) <= 0
        ):
            unresolved[task_id] = "archive_issue_identity_invalid"
            continue
        selected[task_id] = int(number)
    if not selected:
        return ReviewRelationshipResolution(unresolved=tuple(sorted(unresolved.items())))
    try:
        source = read_archive_review_source(path, issue_ids=tuple(sorted(set(selected.values()))))
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        unresolved.update(
            (task_id, "archive_relationship_source_unavailable") for task_id in selected
        )
        return ReviewRelationshipResolution(unresolved=tuple(sorted(unresolved.items())))
    by_issue = {issue.issue_id: issue for issue in source.issues}
    by_relation: dict[str, list[tuple[ReviewRelationshipMember, EvidenceRef]]] = {}
    verified: list[str] = []
    from .review_task_repository import (
        read_current_review_task_source_publication,
        read_review_task_progress,
    )

    source_published: dict[str, bool] = {}
    publication_gaps: dict[str, str] = {}
    for record in page.items:
        task_id = record.task.task_id
        if task_id not in selected:
            continue
        current = by_issue.get(selected[task_id])
        if (
            current is None
            or current.source != record.source
            or source.fence(record.task.scope).source_snapshot_fingerprint
            != record.source_snapshot_fingerprint
        ):
            unresolved[task_id] = "archive_relationship_source_stale"
            continue
        if record.task.scope not in source_published:
            publication = read_current_review_task_source_publication(
                database, source.fence(record.task.scope)
            )
            source_published[record.task.scope] = publication is not None
            if publication is None:
                progress = read_review_task_progress(database, source.fence(record.task.scope))
                publication_gaps[record.task.scope] = (
                    "archive_review_source_publication_missing"
                    if progress is not None and progress.complete
                    else "archive_review_source_staged"
                )
        if not source_published[record.task.scope]:
            unresolved[task_id] = publication_gaps[record.task.scope]
            continue
        if (
            current.relationship_id is None
            or not current.evidence
            or record.source.resource is None
            or record.source.revision is None
        ):
            unresolved[task_id] = "logical_document_relationship_unproved"
            continue
        member = ReviewRelationshipMember(
            record.source.resource.resource_id,
            record.source.revision.revision_id,
            record.source_snapshot_fingerprint,
        )
        by_relation.setdefault(current.relationship_id, []).append((member, current.evidence[0]))
        verified.append(task_id)
    relationships: list[VerifiedReviewRelationship] = []
    for relation_id, values in sorted(by_relation.items()):
        members = tuple(sorted({member for member, _ in values}))
        if len(members) < 2:
            continue
        evidence = tuple({item.evidence_id: item for _, item in values}.values())
        relationships.append(
            VerifiedReviewRelationship(relation_id, "archive_logical_document", members, evidence)
        )
    return ReviewRelationshipResolution(
        tuple(relationships), tuple(sorted(verified)), tuple(sorted(unresolved.items()))
    )


__all__ = [
    "ARCHIVE_REVIEW_TASK_TYPE",
    "ArchiveReviewSource",
    "ReviewRelationshipResolution",
    "read_archive_review_source",
    "resolve_review_relationships",
]
