"""Bounded, read-only curation pages over published local state.

The preview composes the existing inventory duplicate plans and technical
organization plans. It never initializes, migrates, or writes a database and
it never authorizes a filesystem action. The page contract binds a keyset cursor
to a semantic snapshot and hashes the complete ordered source stream without
materializing that stream in memory.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import stat
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from itertools import chain, groupby
from threading import RLock
from typing import Any, Literal, cast

from neocortex.deduplication.domain.models import VALID_VERIFICATION_MODES, VerificationMode
from neocortex.deduplication.domain.errors import InventoryError
from neocortex.deduplication.domain.evidence import (
    DedupPolicy, DuplicateGroupProof, PlanCoverage,
)
from neocortex.deduplication.inventory.plan_evidence import decode_group_proof, decode_member_proof
from neocortex.deduplication.persistence.validation import validate_inventory_schema
from neocortex.documents.document_catalog_schema import (
    document_catalog_schema_contract,
    validate_v7_document_catalog_schema,
)
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadSession,
    SQLiteSnapshotBudget,
    capture_sqlite_read_fence,
    preferred_sqlite_read_mode,
)
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget, KnowledgeReadBudgetExceeded
from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

CURATION_PREVIEW_SCHEMA_VERSION = 1
CURATION_PLAN_PAGE_SCHEMA_VERSION = 1
_MAX_GROUP_MEMBERS_IN_EVIDENCE = 64
_MAX_CURSOR_BYTES = 2_048
_PLAN_CONTRACT = "neocortex.curation-plan/v1"
_SNAPSHOT_CONTRACT = "neocortex.curation-snapshot/v1"
_CURSOR_CONTRACT = "neocortex.curation-cursor/v1"
_REQUIRED_CATALOG_PLAN_COLUMNS = {
    "plan_id",
    "catalog_run_id",
    "source_kind",
    "file_key",
    "source_path",
    "destination_path",
    "organization_root",
    "volume_id",
    "file_id",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "classifier_signature",
    "primary_kind",
    "confidence",
    "status",
    "reason",
    "evidence_json",
    "planned_ns",
}


class CurationStateError(RuntimeError):
    """Published state is missing or cannot satisfy the preview contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "curation_state_unavailable",
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = {} if context is None else context

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "curation-error",
            "coverage": "unavailable",
            "executable": False,
            "code": self.code,
            "error_type": type(self).__name__,
            "message": str(self),
            "context": self.context,
        }


def _owner_is_regular_file(path: Path, *, label: str) -> bool:
    """Inspect only the owner entry, rejecting endpoint links before any open."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CurationStateError(f"{label} owner cannot be inspected") from error
    if stat.S_ISLNK(mode):
        raise CurationStateError(f"{label} owner is a symlink")
    return stat.S_ISREG(mode)


@dataclass(frozen=True, slots=True)
class CurationItem:
    """One bounded advisory proposal with its source evidence."""

    item_id: str
    kind: str
    status: str
    action: str
    source_path: str
    destination_path: str | None
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "destination_path": self.destination_path,
            "evidence": self.evidence,
            "item_id": self.item_id,
            "kind": self.kind,
            "reason": self.reason,
            "source_path": self.source_path,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CurationPreview:
    """Summary and bounded sample of the current durable curation plans."""

    schema_version: int
    coverage: str
    missing_owners: tuple[str, ...]
    root: str | None
    scan_id: int | None
    inventory_files: int
    duplicate_groups: int
    duplicate_members: int
    reclaimable_bytes: int
    organization_plans: int
    empty_files: int
    preview_limit: int
    items_total: int
    items_truncated: bool
    preview_fingerprint: str
    items: tuple[CurationItem, ...]
    source_heads: tuple["CurationSourceHead", ...] = ()

    @property
    def nominal_redundant_bytes(self) -> int:
        return self.reclaimable_bytes

    @property
    def physical_reclaimable_bytes(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "empty_files": self.empty_files,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_members": self.duplicate_members,
            "dedup_verification": _dedup_summary_from_heads(self.source_heads),
            "inventory_files": self.inventory_files,
            "items": [item.to_dict() for item in self.items],
            "items_total": self.items_total,
            "items_truncated": self.items_truncated,
            "missing_owners": list(self.missing_owners),
            "organization_plans": self.organization_plans,
            "preview_fingerprint": self.preview_fingerprint,
            "preview_limit": self.preview_limit,
            "reclaimable_bytes": self.reclaimable_bytes,
            "nominal_redundant_bytes": self.nominal_redundant_bytes,
            "physical_reclaimable_bytes": self.physical_reclaimable_bytes,
            "root": self.root,
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
            "source_heads": [head.to_dict() for head in self.source_heads],
        }


@dataclass(frozen=True, slots=True)
class CurationPlanPage:
    """One immutable keyset page over a complete, digest-bound curation plan."""

    schema_version: int
    coverage: str
    missing_owners: tuple[str, ...]
    root: str | None
    scan_id: int | None
    inventory_files: int
    duplicate_groups: int
    duplicate_members: int
    reclaimable_bytes: int
    organization_plans: int
    empty_files: int
    limit: int
    cursor: str | None
    next_cursor: str | None
    snapshot_id: str
    plan_digest: str
    items_total: int
    items: tuple[CurationItem, ...]
    source_heads: tuple["CurationSourceHead", ...] = ()

    @property
    def items_truncated(self) -> bool:
        return self.next_cursor is not None

    @property
    def nominal_redundant_bytes(self) -> int:
        return self.reclaimable_bytes

    @property
    def physical_reclaimable_bytes(self) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "cursor": self.cursor,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_members": self.duplicate_members,
            "dedup_verification": _dedup_summary_from_heads(self.source_heads),
            "empty_files": self.empty_files,
            "inventory_files": self.inventory_files,
            "items": [item.to_dict() for item in self.items],
            "items_total": self.items_total,
            "items_truncated": self.items_truncated,
            "limit": self.limit,
            "missing_owners": list(self.missing_owners),
            "next_cursor": self.next_cursor,
            "organization_plans": self.organization_plans,
            "plan_digest": self.plan_digest,
            "reclaimable_bytes": self.reclaimable_bytes,
            "nominal_redundant_bytes": self.nominal_redundant_bytes,
            "physical_reclaimable_bytes": self.physical_reclaimable_bytes,
            "root": self.root,
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_heads": [head.to_dict() for head in self.source_heads],
        }


SourceHeadCoverage = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True, slots=True)
class CurationSourceHead:
    """One owner-local head captured by the published curation snapshot."""

    owner: str
    kind: str
    head_id: str | None
    digest: str
    root: str | None
    revision: int | None
    item_count: int
    coverage: SourceHeadCoverage
    reason: str | None = None
    verification_mode: VerificationMode | None = None
    metadata: tuple[tuple[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage": self.coverage,
            "digest": self.digest,
            "head_id": self.head_id,
            "item_count": self.item_count,
            "kind": self.kind,
            "metadata": dict(self.metadata),
            "owner": self.owner,
            "reason": self.reason,
            "revision": self.revision,
            "root": self.root,
            "verification_mode": self.verification_mode,
        }


def _dedup_summary_from_heads(heads: tuple[CurationSourceHead, ...]) -> dict[str, object]:
    head = next((item for item in heads if item.owner == "dedup.sqlite3"), None)
    metadata = {} if head is None else dict(head.metadata)
    return {
        "requested_policy": metadata.get("requested_policy", "legacy_unknown"),
        "verification_coverage": metadata.get("verification_coverage", "legacy_unknown"),
        "verification_mode": None if head is None else head.verification_mode,
        "verification_scope": "plan",
        "exact_comparisons": metadata.get("exact_comparisons"),
        "changed_or_unreadable_files": metadata.get("changed_or_unreadable_files"),
    }


@dataclass(frozen=True, slots=True)
class _InventoryHead:
    scan_id: int
    root: str
    inventory_files: int
    checkpoint_updated_ns: int


@dataclass(frozen=True, slots=True)
class _DuplicatePlanState:
    complete: bool
    groups: int
    redundant_members: int
    reclaimable_bytes: int
    completed_ns: int | None
    dangling_groups: int
    verification_mode: VerificationMode
    requested_policy: DedupPolicy = "legacy_unknown"
    verification_coverage: PlanCoverage = "legacy_unknown"
    exact_comparisons: int | None = None
    changed_or_unreadable_files: int | None = None


@dataclass(frozen=True, slots=True)
class _OrganizationPlanScope:
    """One completed organization run/root compatible with the inventory head."""

    catalog_run_id: int
    organization_root: str


type _SortKey = tuple[int, int, str, int]


@dataclass(frozen=True, slots=True)
class _CursorState:
    snapshot_id: str
    key: _SortKey


@dataclass(frozen=True, slots=True)
class _PlanPublication:
    """The complete digest and bounded metadata of one published plan.

    The durable owners already publish the rows that make up a curation plan,
    but they do not currently carry a separate curation-plan digest.  Keep the
    derived publication in a small process-local cache keyed by the fenced
    owner identities.  The first reader performs the complete streaming digest
    once; subsequent page reads use only keyset queries against the same owner
    generation.  No row, member, or CurationItem is retained in this cache.
    """

    head: _InventoryHead
    duplicate_plan: _DuplicatePlanState
    organization_scope: _OrganizationPlanScope | None
    organization_plans: int
    empty_files: int
    coverage: str
    source_heads: tuple[CurationSourceHead, ...]
    plan_digest: str
    snapshot_id: str


# A page request must not turn the complete digest into an O(N) operation for
# every cursor.  This cache is deliberately bounded and only retains digest /
# summary metadata, never corpus-derived rows or group members.
_PLAN_PUBLICATION_CACHE: OrderedDict[tuple[object, ...], _PlanPublication] = OrderedDict()
_PLAN_PUBLICATION_CACHE_LIMIT = 8
_PLAN_PUBLICATION_LOCK = RLock()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _owner_generation_key(path: Path) -> tuple[object, ...]:
    """Return a hashable identity for the fenced bytes of one SQLite owner."""

    # ``SQLiteImmutableFence`` is frozen and contains only frozen identities,
    # so retaining it in the cache key is safe.  In particular, include active
    # WAL/journal identities: a later request must not reuse a digest prepared
    # from a different temporary snapshot.
    fence = capture_sqlite_read_fence(path)
    return (str(path.absolute()), fence)


def _cached_plan_publication(cache_key: tuple[object, ...]) -> _PlanPublication | None:
    """Return a publication without touching any SQLite owner rows."""

    with _PLAN_PUBLICATION_LOCK:
        publication = _PLAN_PUBLICATION_CACHE.get(cache_key)
        if publication is not None:
            _PLAN_PUBLICATION_CACHE.move_to_end(cache_key)
        return publication


def _organization_source_predicate(
    inventory_root: str,
    *,
    column: str = "source_path",
) -> tuple[str, tuple[object, ...]]:
    """Return an exact, case-sensitive SQL filter for paths under a root.

    ``substr`` keeps the path-boundary check case-sensitive even when a SQLite
    build uses ASCII-folding for ``LIKE``.  The primary-key/indexed keyset
    predicates still bound the rows visited by page reads, and callers retain
    ``_path_is_within_root`` as the final lexical fence.
    """

    exact = os.path.abspath(inventory_root)
    prefix = exact if exact == os.sep else exact.rstrip(os.sep) + os.sep
    return (
        f"({column} COLLATE BINARY=? COLLATE BINARY OR "
        f"(length({column})>? AND substr({column},1,?) "
        f"COLLATE BINARY=? COLLATE BINARY))",
        (exact, len(prefix), len(prefix), prefix),
    )


def _source_head(
    *,
    owner: str,
    kind: str,
    head_id: str | None,
    root: str | None,
    revision: int | None,
    item_count: int,
    coverage: SourceHeadCoverage,
    reason: str | None = None,
    verification_mode: VerificationMode | None = None,
    extra: dict[str, object] | None = None,
) -> CurationSourceHead:
    payload: dict[str, object] = {
        "coverage": coverage,
        "head_id": head_id,
        "item_count": item_count,
        "kind": kind,
        "owner": owner,
        "reason": reason,
        "revision": revision,
        "root": root,
        "verification_mode": verification_mode,
    }
    metadata = tuple(sorted((extra or {}).items()))
    payload["metadata"] = dict(metadata)
    digest = "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return CurationSourceHead(
        owner=owner,
        kind=kind,
        head_id=head_id,
        digest=digest,
        root=root,
        revision=revision,
        item_count=item_count,
        coverage=coverage,
        reason=reason,
        verification_mode=verification_mode,
        metadata=metadata,
    )


@contextmanager
def _readonly_sqlite_connection(
    path: Path,
    *,
    label: str,
    expected_generation: tuple[object, ...] | None = None,
    budget: KnowledgeReadBudget | None = None,
) -> Iterator[sqlite3.Connection]:
    """Read one owner through the shared lstat/O_NOFOLLOW/fence kernel."""

    try:
        mode = preferred_sqlite_read_mode(path)
        if budget is not None:
            budget.checkpoint()
        sqlite_budget = None
        if (
            budget is not None
            and budget.max_temporary_bytes is not None
            and getattr(mode, "value", mode) == "snapshot_temp"
        ):
            remaining = budget.temporary_bytes_remaining
            if remaining is None or remaining <= 0:
                raise KnowledgeReadBudgetExceeded("temporary_bytes_exhausted")
            sqlite_budget = SQLiteSnapshotBudget(
                max_temporary_bytes=remaining,
                cancellation_check=budget.cancellation_check,
            )
        session = SQLiteReadSession(
            path,
            mode=mode,
            timeout_seconds=60.0,
            budget=sqlite_budget,
            cancellation_check=(
                budget.cancellation_check
                if budget is not None and sqlite_budget is None
                else None
            ),
        )
        with session as connection:
            if budget is not None:
                budget.checkpoint(temporary_bytes=int(session.metrics.temporary_bytes))
            if expected_generation is not None:
                observed_generation = (str(path.absolute()), session.source_fence)
                if observed_generation != expected_generation:
                    raise CurationStateError(f"{label} state changed before read")
            yield connection
    except FileNotFoundError as error:
        raise CurationStateError(f"{label} state changed or disappeared during read") from error
    except ImmutableSQLiteUnavailable as error:
        raise CurationStateError(f"{label} read refused: {error}") from error


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = {str(row[1]) for row in rows}
    if not columns:
        raise CurationStateError(f"curation state lacks required table: {table}")
    return columns


def _published_inventory_head(connection: sqlite3.Connection) -> _InventoryHead:
    """Select one complete inventory only through its valid publication head."""

    _table_columns(connection, "scans")
    _table_columns(connection, "inventory_checkpoints")
    row = connection.execute(
        """SELECT c.scan_id,s.root,s.files_seen,c.updated_ns,
        (SELECT COUNT(*) FROM files f WHERE f.scan_id=s.scan_id) AS stored_files
        FROM inventory_checkpoints c
        JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root COLLATE BINARY
        WHERE c.valid=1 AND s.status='complete' AND s.errors=0
          AND s.completed_ns IS NOT NULL
        ORDER BY c.updated_ns DESC,c.root COLLATE BINARY,c.scan_id DESC
        LIMIT 1"""
    ).fetchone()
    if row is None or any(value is None for value in row):
        raise CurationStateError("curation state has no published complete inventory scan")
    inventory_files = int(row[2])
    if inventory_files != int(row[4]):
        raise CurationStateError("published inventory file count is inconsistent")
    return _InventoryHead(
        scan_id=int(row[0]),
        root=str(row[1]),
        inventory_files=inventory_files,
        checkpoint_updated_ns=int(row[3]),
    )


def _identity_payload(
    volume_id: object,
    file_id: object,
    birthtime_ns: object,
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    from neocortex.documents.document_resource_binding import (
        ResourceBindingError,
        physical_identity_from_components,
    )

    encoding = (
        "unsigned-128-le"
        if isinstance(volume_id, (bytes, bytearray, memoryview))
        else "integer"
        if type(volume_id) is int
        else "legacy-decimal"
    )
    try:
        identity = physical_identity_from_components(volume_id, file_id, encoding=encoding)
        if type(birthtime_ns) is not int or birthtime_ns < -1:
            raise ResourceBindingError(
                "birthtime_ns must be -1 or a non-negative integer",
                field="birthtime_ns",
                encoding="integer",
                value=birthtime_ns,
            )
    except ResourceBindingError as error:
        raise _curation_identity_error(error, context=context) from error
    return {
        "birthtime_ns": birthtime_ns,
        "file_id": f"{identity.file_id:x}",
        "volume_id": f"{identity.volume_id:x}",
    }


def _curation_identity_error(
    error: Any, *, context: dict[str, object] | None
) -> CurationStateError:
    raw = error.value
    # Identifiers are diagnostic data, never terminal control sequences or an
    # unbounded dump of a row. The CLI applies its normal sanitizer as well.
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = {"type": type(raw).__name__, "bytes": len(raw), "hex_prefix": bytes(raw[:64]).hex()}
    else:
        raw = {"type": type(raw).__name__, "value": str(raw)[:512]}
    return CurationStateError(
        f"curation source identity rejected: {error}",
        code=error.code,
        context={
            **(context or {}),
            "field": error.field,
            "encoding": error.encoding,
            "original": raw,
        },
    )


def _duplicate_plan_state(
    connection: sqlite3.Connection,
    scan_id: int,
) -> _DuplicatePlanState:
    """Require the terminal summary before exposing any persisted group."""

    actual = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(redundant_count),0),
        COALESCE(SUM(reclaimable_bytes),0)
        FROM planned_duplicate_groups WHERE scan_id=?""",
        (scan_id,),
    ).fetchone()
    if actual is None:
        actual = (0, 0, 0)
    actual_groups, actual_redundant, actual_reclaimable = (
        int(actual[0]),
        int(actual[1]),
        int(actual[2]),
    )
    individual = "requested_policy" in _table_columns(connection, "duplicate_plan_summaries")
    extras = (
        "requested_policy,coverage,exact_comparisons,changed_or_unreadable_files"
        if individual else "'legacy_unknown','legacy_unknown',NULL,NULL"
    )
    summary = connection.execute(
        f"""SELECT group_count,redundant_files,reclaimable_bytes,completed_ns,
        verification_mode,{extras}
        FROM duplicate_plan_summaries WHERE scan_id=?""",
        (scan_id,),
    ).fetchone()
    if summary is None:
        return _DuplicatePlanState(False, 0, 0, 0, None, actual_groups, "legacy_unknown")

    expected_groups = int(summary[0])
    expected_redundant = int(summary[1])
    expected_reclaimable = int(summary[2])
    completed_ns = int(summary[3])
    verification_mode = str(summary[4])
    if verification_mode not in VALID_VERIFICATION_MODES:
        raise CurationStateError("duplicate plan verification mode is invalid")
    requested_policy, verification_coverage, comparisons, failures = summary[5:9]
    if requested_policy not in {"legacy_unknown", "fast", "exact"} or verification_coverage not in {
        "legacy_unknown", "complete", "partial"
    }:
        raise CurationStateError("duplicate plan requested policy or verification coverage is invalid")
    if requested_policy != "legacy_unknown" and (
        type(comparisons) is not int or comparisons < 0 or type(failures) is not int or failures < 0
        or verification_coverage != ("partial" if failures else "complete")
        or verification_mode != ("partial" if failures else "full_hash" if requested_policy == "exact" else "fast")
        or (requested_policy == "fast" and comparisons != 0)
        or (requested_policy == "exact" and comparisons < expected_redundant)
    ):
        raise CurationStateError("duplicate plan comparison coverage is inconsistent")
    proof_state = (
        cast(DedupPolicy, requested_policy), cast(PlanCoverage, verification_coverage), comparisons, failures,
    )
    stored_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM planned_duplicate_members m
            JOIN planned_duplicate_groups g ON g.group_id=m.group_id
            WHERE g.scan_id=?""",
            (scan_id,),
        ).fetchone()[0]
    )
    complete = (actual_groups, actual_redundant, actual_reclaimable) == (
        expected_groups,
        expected_redundant,
        expected_reclaimable,
    ) and stored_members == expected_groups + expected_redundant
    if not complete:
        return _DuplicatePlanState(
            False,
            0,
            0,
            0,
            completed_ns,
            actual_groups,
            cast(VerificationMode, verification_mode),
            *proof_state,
        )
    return _DuplicatePlanState(
        True,
        expected_groups,
        expected_redundant,
        expected_reclaimable,
        completed_ns,
        0,
        cast(VerificationMode, verification_mode),
        *proof_state,
    )


def _duplicate_group_projection(connection: sqlite3.Connection, *, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    fields = ",".join(prefix + name for name in (
        "group_id", "size", "keep_path", "redundant_count", "reclaimable_bytes", "full_fingerprint",
    ))
    if "proof_json" in _table_columns(connection, "planned_duplicate_groups"):
        return fields + f",{prefix}verification_mode,{prefix}proof_json,1"
    return fields + ",'legacy_unknown','{}',0"


def _duplicate_proof_error(error: object, *, scan_id: int, group_id: int, order: int | None = None) -> CurationStateError:
    return CurationStateError(
        f"duplicate content proof rejected: {error}", code="curation_duplicate_proof_invalid",
        context={
            "owner": "dedup.sqlite3",
            "table": "planned_duplicate_groups" if order is None else "planned_duplicate_members",
            "record_id": {"group_id": group_id, **({} if order is None else {"member_order": order})},
            "publication": {"scan_id": scan_id},
        },
    )


def _duplicate_group_record(
    scan_id: int, row: Any, plan_verification_mode: VerificationMode,
) -> tuple[dict[str, object], DuplicateGroupProof | None]:
    group_id = int(row[0])
    size = int(row[1])
    redundant_count = int(row[3])
    reclaimable_bytes = int(row[4])
    if (
        size <= 0
        or redundant_count < 1
        or reclaimable_bytes != size * redundant_count
    ):
        raise CurationStateError(f"duplicate group {group_id} has inconsistent physical totals")
    raw_mode = str(row[6])
    try:
        proof = decode_group_proof(str(row[7]))
        expected_mode = "legacy_unknown" if proof is None else (
            "full_hash" if proof.requested_policy == "exact" else "fast"
        )
        if raw_mode != expected_mode:
            raise InventoryError("group verification label is not supported by its own proof")
    except InventoryError as error:
        raise _duplicate_proof_error(error, scan_id=scan_id, group_id=group_id) from error
    return {
        "full_fingerprint": str(row[5]), "group_id": group_id, "keep_path": str(row[2]),
        "reclaimable_bytes": reclaimable_bytes, "nominal_redundant_bytes": reclaimable_bytes,
        "physical_reclaimable_bytes": None, "redundant_count": redundant_count,
        "scan_id": scan_id, "size": size, "verification_mode": raw_mode,
        "verification_scope": "group", "plan_verification_mode": plan_verification_mode,
        "requested_policy": "legacy_unknown" if proof is None else proof.requested_policy,
        "group_proof": None if proof is None else proof.as_dict(),
        "actionability": "review_required", "source_scope": "physical_files",
    }, proof


def _duplicate_member_record(
    scan_id: int, group_id: int, member: Any, group_proof: DuplicateGroupProof | None,
    keeper_identity: tuple[int, int] | None,
) -> tuple[dict[str, object], tuple[int, int]]:
    order = int(member[0])
    identity = _identity_payload(
        member[3], member[4], member[7],
        context={"owner": "dedup.sqlite3", "table": "planned_duplicate_members",
                 "record_id": {"group_id": group_id, "member_order": order},
                 "publication": {"scan_id": scan_id}},
    )
    physical_identity = (int(str(identity["volume_id"]), 16), int(str(identity["file_id"]), 16))
    try:
        proof = decode_member_proof(str(member[8]))
        if group_proof is not None:
            keeper_role = str(member[1]) == "keep"
            expected_result = "reference" if keeper_role else (
                "equal" if group_proof.requested_policy == "exact" else "fingerprint_match"
            )
            if (
                proof.comparison_result != expected_result or str(member[2]) not in proof.aliases
                or (not keeper_role and keeper_identity is not None and proof.compared_to_identity != keeper_identity)
                or (keeper_role and keeper_identity is not None and physical_identity != keeper_identity)
                or (proof.comparison_result == "equal" and proof.comparison_bytes != int(member[5]))
            ):
                raise InventoryError("member receipt is not bound to the group's physical snapshots")
        elif proof.proof_version != "legacy_unknown":
            raise InventoryError("individual member proof has no supporting group proof")
    except InventoryError as error:
        raise _duplicate_proof_error(error, scan_id=scan_id, group_id=group_id, order=order) from error
    return {
        "identity": identity, "member_order": order, "mtime_ns": int(member[6]),
        "path": str(member[2]), "role": str(member[1]), "size": int(member[5]),
        "proof": proof.as_dict(),
    }, physical_identity


def _register_duplicate_member(
    group_id: int,
    record: dict[str, object],
    physical_identity: tuple[int, int],
    seen_paths: set[str],
    seen_identities: set[tuple[int, int]],
) -> None:
    """Reject duplicate physical members before publishing preview evidence."""

    path = str(record["path"])
    if path in seen_paths or physical_identity in seen_identities:
        raise CurationStateError(
            f"duplicate group {group_id} member physical identity is duplicated"
        )
    seen_paths.add(path)
    seen_identities.add(physical_identity)


def _duplicate_member_projection(*, has_proof: bool, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    fields = ",".join(prefix + name for name in (
        "member_order", "role", "path", "volume_id", "file_id", "size", "mtime_ns", "birthtime_ns",
    ))
    return fields + (f",{prefix}proof_json" if has_proof else ",'{}'")


def _duplicate_item(
    connection: sqlite3.Connection,
    scan_id: int,
    row: Any,
    digest: Any | None,
    verification_mode: VerificationMode,
) -> CurationItem:
    """Materialize only a selected bounded sample, retaining actual proof scope."""

    group_record, group_proof = _duplicate_group_record(scan_id, row, verification_mode)
    group_id = int(row[0])
    projection = _duplicate_member_projection(has_proof=bool(row[8]))
    limit_clause = "" if digest is not None else " LIMIT ?"
    params = (group_id,) if digest is not None else (group_id, _MAX_GROUP_MEMBERS_IN_EVIDENCE + 1)
    members = connection.execute(
        f"SELECT {projection},COUNT(*) OVER() FROM planned_duplicate_members WHERE group_id=? "
        "ORDER BY member_order" + limit_clause, params,
    )
    if digest is not None:
        _digest_record(digest, "duplicate_group", group_record)
    payload: list[dict[str, object]] = []
    member_count = sampled_count = 0
    keeper_identity = None
    seen_paths: set[str] = set()
    seen_identities: set[tuple[int, int]] = set()
    for member in members:
        member_count = int(member[9])
        record, physical_identity = _duplicate_member_record(
            scan_id, group_id, member, group_proof, keeper_identity,
        )
        _register_duplicate_member(
            group_id, record, physical_identity, seen_paths, seen_identities,
        )
        if keeper_identity is None:
            if record["role"] == "keep":
                keeper_identity = physical_identity
            elif group_proof is not None:
                keeper_identity = cast(tuple[int, int], cast(dict[str, object], record["proof"])["compared_to_identity"])
        if digest is not None:
            _digest_record(digest, "duplicate_member", {"group_id": group_id, **record})
        if sampled_count < _MAX_GROUP_MEMBERS_IN_EVIDENCE:
            payload.append(record)
        sampled_count += 1
    if member_count != int(row[3]) + 1 or sampled_count < min(member_count, _MAX_GROUP_MEMBERS_IN_EVIDENCE + 1):
        raise CurationStateError(f"duplicate group {group_id} member count is inconsistent")
    return CurationItem(
        item_id=f"duplicate:{scan_id}:{group_id}", kind="duplicate_group", status="review",
        action="review_duplicate_group", source_path=str(row[2]), destination_path=None,
        reason="duplicate_content_candidate",
        evidence={**group_record, "member_count": member_count, "members": payload,
                  "members_truncated": member_count > _MAX_GROUP_MEMBERS_IN_EVIDENCE},
    )


def _digest_duplicate_group(
    connection: sqlite3.Connection,
    scan_id: int,
    row: Any,
    digest: Any,
    verification_mode: VerificationMode,
    *,
    members: Iterator[Any] | None = None,
    budget: KnowledgeReadBudget | None = None,
) -> None:
    """Digest all individual proofs, including members omitted from page samples."""

    group_record, group_proof = _duplicate_group_record(scan_id, row, verification_mode)
    group_id = int(row[0])
    _digest_record(digest, "duplicate_group", group_record)
    if members is None:
        projection = _duplicate_member_projection(has_proof=bool(row[8]))
        members = iter(connection.execute(
            f"SELECT {projection} FROM planned_duplicate_members WHERE group_id=? ORDER BY member_order",
            (group_id,),
        ))
    member_count = keeper_count = 0
    keeper_identity = None
    seen_paths: set[str] = set()
    seen_identities: set[tuple[int, int]] = set()
    for member in members:
        if budget is not None:
            budget.checkpoint(rows=1)
        record, physical_identity = _duplicate_member_record(
            scan_id, group_id, member, group_proof, keeper_identity,
        )
        _register_duplicate_member(
            group_id, record, physical_identity, seen_paths, seen_identities,
        )
        if keeper_identity is None:
            # A legacy presentation may order the keeper after the bounded
            # page sample.  A comparison anchor is provisional until this
            # full stream reaches exactly one keep role at g.keep_path and
            # verifies its physical identity against every comparison.
            if record["role"] == "keep":
                keeper_identity = physical_identity
            elif group_proof is not None:
                keeper_identity = cast(tuple[int, int], cast(dict[str, object], record["proof"])["compared_to_identity"])
        if int(member[0]) != member_count or str(member[1]) not in {"keep", "redundant"}:
            raise CurationStateError(f"duplicate group {group_id} roles or member order are inconsistent")
        if str(member[1]) == "keep":
            keeper_count += 1
        if int(member[5]) != int(row[1]) or (str(member[1]) == "keep" and str(member[2]) != str(row[2])):
            raise CurationStateError(f"duplicate group {group_id} member snapshot is inconsistent")
        _digest_record(digest, "duplicate_member", {"group_id": group_id, **record})
        member_count += 1
    if member_count != int(row[3]) + 1 or keeper_count != 1:
        raise CurationStateError(f"duplicate group {group_id} member count is inconsistent")


def _digest_duplicate_groups(
    connection: sqlite3.Connection, scan_id: int, digest: Any, verification_mode: VerificationMode,
    budget: KnowledgeReadBudget | None = None,
) -> int:
    """One ordered join streams every group and member instead of N member queries."""

    groups = _duplicate_group_projection(connection, alias="g")
    member_fields = _duplicate_member_projection(
        has_proof="proof_json" in _table_columns(connection, "planned_duplicate_members"), alias="m",
    )
    rows = connection.execute(
        f"SELECT {groups},{member_fields} FROM planned_duplicate_groups g "
        "JOIN planned_duplicate_members m ON m.group_id=g.group_id WHERE g.scan_id=? "
        "ORDER BY g.reclaimable_bytes DESC,g.keep_path COLLATE BINARY,g.group_id,m.member_order",
        (scan_id,),
    )
    count = 0
    for _group_id, stream in groupby(rows, key=lambda item: item[0]):
        if budget is not None:
            budget.checkpoint(rows=1)
        first = next(stream)
        members = (item[9:] for item in chain((first,), stream))
        _digest_duplicate_group(
            connection, scan_id, first[:9], digest, verification_mode,
            members=members, budget=budget,
        )
        count += 1
    return count


def _iter_duplicate_items(
    connection: sqlite3.Connection, scan_id: int, digest: Any, verification_mode: VerificationMode,
) -> Iterator[tuple[_SortKey, CurationItem]]:
    projection = _duplicate_group_projection(connection)
    rows = connection.execute(
        f"SELECT {projection} FROM planned_duplicate_groups WHERE scan_id=? "
        "ORDER BY reclaimable_bytes DESC,keep_path COLLATE BINARY,group_id", (scan_id,),
    )
    for row in rows:
        item = _duplicate_item(connection, scan_id, row, digest, verification_mode)
        yield (0, -int(row[4]), str(row[2]), int(row[0])), item


def _empty_file_summary(connection: sqlite3.Connection, scan_id: int) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id=? AND size=0", (scan_id,)
        ).fetchone()[0]
    )


def _empty_file_item(scan_id: int, row: Any) -> CurationItem:
    """Materialize one selected empty-file proposal."""

    return CurationItem(
        item_id=f"empty:{scan_id}:{row[0]!s}",
        kind="empty_file",
        status="review",
        action="review_empty_file",
        source_path=str(row[0]),
        destination_path=None,
        reason="empty_file_requires_human_review",
        evidence={
            "identity": _identity_payload(row[1], row[2], row[5], context={"owner": "dedup.sqlite3", "table": "files", "record_id": str(row[0]), "publication": {"scan_id": scan_id}}),
            "mtime_ns": int(row[4]),
            "size": int(row[3]),
        },
    )


def _path_is_within_root(source_path: object, root: str) -> bool:
    """Check a persisted source path lexically, without resolving filesystem links."""

    try:
        if isinstance(source_path, Path):
            source_text = str(source_path)
        elif isinstance(source_path, str):
            source_text = source_path
        else:
            return False
        source = Path(os.path.abspath(source_text))
        root_path = Path(os.path.abspath(root))
    except (OSError, TypeError, ValueError):
        return False
    return source == root_path or root_path in source.parents


def _organization_plan_scope(
    connection: sqlite3.Connection,
    *,
    inventory_root: str,
) -> _OrganizationPlanScope | None:
    """Select one completed plan run and reject ambiguous or foreign sources.

    Organization planning is a separate catalog run and may leave older roots
    active in the append-only table.  The curation page deliberately selects
    the newest completed ``plan`` run whose active rows use exactly one
    destination root and contain at least one source under the published
    inventory root.  Row iteration still applies the same source-root fence,
    so a mixed catalog can never leak a source from another inventory root.
    """

    columns = _table_columns(connection, "organization_plans")
    missing = _REQUIRED_CATALOG_PLAN_COLUMNS - columns
    if missing:
        missing_text = ",".join(sorted(missing))
        raise CurationStateError(
            f"organization_plans schema lacks required columns: {missing_text}"
        )
    _table_columns(connection, "catalog_runs")
    candidates = connection.execute(
        """SELECT r.catalog_run_id,
        MIN(p.organization_root COLLATE BINARY),
        MAX(p.organization_root COLLATE BINARY),
        COUNT(DISTINCT p.organization_root COLLATE BINARY),
        r.completed_ns
        FROM catalog_runs AS r
        JOIN organization_plans AS p ON p.catalog_run_id=r.catalog_run_id
        WHERE p.status<>'superseded'
          AND r.mode='plan' AND r.status='completed'
          AND r.completed_ns IS NOT NULL
        GROUP BY r.catalog_run_id,r.completed_ns
        ORDER BY r.completed_ns DESC,r.catalog_run_id DESC"""
    )
    for row in candidates:
        if int(row[3]) != 1 or row[1] is None or row[2] is None:
            continue
        organization_root = str(row[1])
        if str(row[1]) != str(row[2]):
            continue
        run_id = int(row[0])
        source_rows = connection.execute(
            """SELECT source_path FROM organization_plans
            WHERE catalog_run_id=?
              AND organization_root COLLATE BINARY=? COLLATE BINARY
              AND status<>'superseded'
            ORDER BY plan_id""",
            (run_id, organization_root),
        )
        if any(_path_is_within_root(source[0], inventory_root) for source in source_rows):
            return _OrganizationPlanScope(run_id, organization_root)
    return None


def _iter_organization_rows(
    connection: sqlite3.Connection,
    *,
    inventory_root: str,
    scope: _OrganizationPlanScope | None,
) -> Iterator[Any]:
    if scope is None:
        return
    source_predicate, source_parameters = _organization_source_predicate(inventory_root)
    bindings = _organization_binding_projection(connection)
    rows = connection.execute(
        f"""SELECT plan_id,catalog_run_id,source_kind,file_key,source_path,
        destination_path,organization_root,volume_id,file_id,size,mtime_ns,
        birthtime_ns,classifier_signature,primary_kind,confidence,status,reason,
        evidence_json,{bindings} FROM organization_plans
        WHERE catalog_run_id=?
          AND organization_root COLLATE BINARY=? COLLATE BINARY
          AND status<>'superseded'
          AND """
        + source_predicate
        + """
        ORDER BY plan_id DESC""",
        (scope.catalog_run_id, scope.organization_root, *source_parameters),
    )
    for row in rows:
        if _path_is_within_root(row[4], inventory_root):
            yield row


def _organization_binding_projection(connection: sqlite3.Connection) -> str:
    columns = _table_columns(connection, "organization_plans")
    fields = (
        "resource_binding_json",
        "source_scope_json",
        "source_scope_id",
        "representation_kind",
        "operation_kind",
        "executable",
        "blockers_json",
        "eligibility_status",
    )
    return ",".join(field if field in columns else f"NULL AS {field}" for field in fields)


def _validate_readable_catalog(connection: sqlite3.Connection) -> None:
    from neocortex.persistence.sqlite_schema_contract import read_metadata_schema_version

    version = read_metadata_schema_version(connection, label="document catalog")
    if version == 7:
        validate_v7_document_catalog_schema(connection)
    else:
        validate_sqlite_schema_contract(
            connection, document_catalog_schema_contract(), label="document catalog", exact=True
        )


def _validate_readable_inventory(connection: sqlite3.Connection) -> None:
    from neocortex.persistence.sqlite_schema_contract import read_metadata_schema_version
    from neocortex.deduplication.persistence.contracts import inventory_v11_schema_contract

    version = read_metadata_schema_version(connection, label="dedup inventory")
    if version == 11:
        validate_sqlite_schema_contract(
            connection, inventory_v11_schema_contract(), label="dedup inventory v11", exact=True
        )
    else:
        validate_inventory_schema(connection)


def _organization_summary(
    connection: sqlite3.Connection,
    *,
    inventory_root: str,
    scope: _OrganizationPlanScope | None,
) -> int:
    if scope is None:
        return 0
    source_predicate, source_parameters = _organization_source_predicate(inventory_root)
    row = connection.execute(
        """SELECT COUNT(*) FROM organization_plans
        WHERE catalog_run_id=?
          AND organization_root COLLATE BINARY=? COLLATE BINARY
          AND status<>'superseded'
          AND """
        + source_predicate,
        (scope.catalog_run_id, scope.organization_root, *source_parameters),
    ).fetchone()
    if row is None:
        raise CurationStateError("organization plan count is unavailable")
    return int(row[0])


def _source_heads(
    *,
    missing_owners: tuple[str, ...],
    inventory_head: _InventoryHead | None,
    duplicate_plan: _DuplicatePlanState | None,
    catalog: sqlite3.Connection | None,
    organization_scope: _OrganizationPlanScope | None,
    organization_plans: int,
) -> tuple[CurationSourceHead, ...]:
    """Build the canonical owner-head manifest used by every curation page."""

    heads: list[CurationSourceHead] = []
    if inventory_head is None:
        if "dedup.sqlite3" in missing_owners:
            heads.append(
                _source_head(
                    owner="dedup.sqlite3",
                    kind="inventory",
                    head_id=None,
                    root=None,
                    revision=None,
                    item_count=0,
                    coverage="unavailable",
                    reason="owner_missing",
                )
            )
    else:
        duplicate_complete = bool(duplicate_plan and duplicate_plan.complete)
        duplicate_mode = None if duplicate_plan is None else duplicate_plan.verification_mode
        inventory_coverage: SourceHeadCoverage = (
            "complete" if duplicate_complete and duplicate_mode != "partial" else "partial"
        )
        inventory_reason = (
            None
            if inventory_coverage == "complete"
            else "duplicate_plan_incomplete"
            if duplicate_plan is None or not duplicate_complete
            else "duplicate_verification_partial"
        )
        heads.append(
            _source_head(
                owner="dedup.sqlite3",
                kind="inventory",
                head_id=f"scan:{inventory_head.scan_id}",
                root=inventory_head.root,
                revision=inventory_head.scan_id,
                item_count=inventory_head.inventory_files,
                coverage=inventory_coverage,
                reason=inventory_reason,
                verification_mode=duplicate_mode,
                extra={
                    "checkpoint_updated_ns": inventory_head.checkpoint_updated_ns,
                    "duplicate_groups": 0 if duplicate_plan is None else duplicate_plan.groups,
                    "duplicate_members": (
                        0 if duplicate_plan is None else duplicate_plan.redundant_members
                    ),
                    "reclaimable_bytes": (
                        0 if duplicate_plan is None else duplicate_plan.reclaimable_bytes
                    ),
                    "nominal_redundant_bytes": 0 if duplicate_plan is None else duplicate_plan.reclaimable_bytes,
                    "physical_reclaimable_bytes": None,
                    "requested_policy": "legacy_unknown" if duplicate_plan is None else duplicate_plan.requested_policy,
                    "verification_coverage": "legacy_unknown" if duplicate_plan is None else duplicate_plan.verification_coverage,
                    "verification_scope": "plan",
                    "exact_comparisons": None if duplicate_plan is None else duplicate_plan.exact_comparisons,
                    "changed_or_unreadable_files": None if duplicate_plan is None else duplicate_plan.changed_or_unreadable_files,
                    "duplicate_plan_completed_ns": None if duplicate_plan is None else duplicate_plan.completed_ns,
                },
            )
        )

    if catalog is None:
        if "document_catalog.sqlite3" in missing_owners:
            heads.append(
                _source_head(
                    owner="document_catalog.sqlite3",
                    kind="catalog",
                    head_id=None,
                    root=None,
                    revision=None,
                    item_count=0,
                    coverage="unavailable",
                    reason="owner_missing",
                )
            )
    elif organization_scope is None:
        heads.append(
            _source_head(
                owner="document_catalog.sqlite3",
                kind="catalog",
                head_id=None,
                root=None,
                revision=None,
                item_count=0,
                coverage="partial",
                reason="no_compatible_published_plan",
            )
        )
    else:
        heads.append(
            _source_head(
                owner="document_catalog.sqlite3",
                kind="catalog",
                head_id=f"catalog-run:{organization_scope.catalog_run_id}",
                root=organization_scope.organization_root,
                revision=organization_scope.catalog_run_id,
                item_count=organization_plans,
                coverage="complete",
                extra={"organization_plans": organization_plans},
            )
        )
    return tuple(heads)


def _organization_item_from_row(row: Any) -> CurationItem:
    """Decode one catalog row into the bounded public item shape."""

    from neocortex.documents.document_resource_binding import (
        ResourceBindingError,
        binding_curation_identity,
        legacy_resource_binding,
        parse_resource_binding,
    )

    context = {
        "owner": "document_catalog.sqlite3",
        "table": "organization_plans",
        "record_id": int(row[0]),
        "source_kind": str(row[2]),
        "file_key": str(row[3]),
        "publication": {"catalog_run_id": row[1]},
    }
    raw_binding = row[18] if len(row) > 18 else None
    try:
        binding = (
            parse_resource_binding(raw_binding)
            if raw_binding is not None
            else legacy_resource_binding(
                source_kind=str(row[2]),
                file_key=str(row[3]),
                path=str(row[4]),
                volume_id=row[7],
                file_id=row[8],
                birthtime_ns=row[11],
                size=row[9],
                mtime_ns=row[10],
            )
        )
        if (
            binding["source_kind"],
            binding["file_key"],
            binding["resource_ref"]["current_path"],
        ) != (str(row[2]), str(row[3]), str(row[4])):
            raise ResourceBindingError(
                "binding differs from its organization record",
                field="resource_binding_json",
                encoding=binding["schema"],
                value=raw_binding,
            )
        identity = binding_curation_identity(binding)
        if raw_binding is not None and identity is not None:
            from neocortex.documents.document_resource_binding import physical_identity_from_components
            stored = physical_identity_from_components(row[7], row[8], encoding="legacy-decimal")
            if (format(stored.volume_id, "x"), format(stored.file_id, "x"), row[11]) != (
                identity["volume_id"], identity["file_id"], identity["birthtime_ns"]
            ) or binding["physical_anchor_revision"] != {"size": row[9], "mtime_ns": row[10]}:
                raise ResourceBindingError("physical binding differs from its recorded source revision",
                                           field="resource_binding_json", encoding=binding["schema"], value=raw_binding)
    except ResourceBindingError as error:
        raise _curation_identity_error(error, context=context) from error
    scoped = len(row) > 20 and row[19] is not None and row[20] is not None
    blockers = ["backend_unavailable", "authorization_required"]
    if not scoped:
        blockers.append("legacy_scope_unresolved")
    if identity is None:
        blockers.append("virtual_resource_requires_logical_review")

    try:
        evidence = json.loads(str(row[17]))
    except (TypeError, ValueError) as error:
        raise CurationStateError(
            f"organization plan {int(row[0])} contains malformed evidence"
        ) from error
    if not isinstance(evidence, dict):
        raise CurationStateError(f"organization plan {int(row[0])} evidence is not an object")
    try:
        _canonical_json(evidence)
        confidence = float(row[14])
    except (TypeError, ValueError) as error:
        raise CurationStateError(
            f"organization plan {int(row[0])} evidence is not JSON-safe"
        ) from error
    if not math.isfinite(confidence):
        raise CurationStateError(f"organization plan {int(row[0])} confidence is not finite")
    status = str(row[15])
    action = (
        "review_organization_proposal"
        if status in {"planned", "review"}
        else "review_blocked_organization_proposal"
        if status == "blocked"
        else "observe_organization_plan"
    )
    return CurationItem(
        item_id=f"organization:{int(row[0])}",
        kind="organization_plan",
        status="review",
        action=action,
        source_path=str(row[4]),
        destination_path=None if row[5] is None else str(row[5]),
        reason=str(row[16]),
        evidence={
            "catalog_run_id": None if row[1] is None else int(row[1]),
            "classifier_signature": str(row[12]),
            "confidence": confidence,
            "file_key": str(row[3]),
            "identity": identity,
            "resource_binding": binding,
            "representation_kind": binding["representation_kind"],
            "executable": False,
            "blockers": blockers,
            "source_scope_id": row[20] if scoped else None,
            "source_scope_json": row[19] if scoped else None,
            "operation_kind": row[22] if len(row) > 22 else None,
            "eligibility_status": row[25] if len(row) > 25 else "unverified",
            "organization_root": str(row[6]),
            "plan_id": int(row[0]),
            "primary_kind": str(row[13]),
            "size": int(row[9]),
            "mtime_ns": int(row[10]),
            "source_status": status,
            "source_kind": str(row[2]),
            "taxonomy": evidence,
        },
    )


def _digest_record(digest: Any, kind: str, value: object) -> None:
    payload = _canonical_json({"kind": kind, "value": value}).encode("utf-8")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _valid_snapshot_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _validated_sort_key(value: object) -> _SortKey:
    if not isinstance(value, list) or len(value) != 4:
        raise CurationStateError("curation cursor has an invalid key")
    category, numeric, text, identity = value
    if (
        isinstance(category, bool)
        or not isinstance(category, int)
        or isinstance(numeric, bool)
        or not isinstance(numeric, int)
        or not isinstance(text, str)
        or isinstance(identity, bool)
        or not isinstance(identity, int)
    ):
        raise CurationStateError("curation cursor has an invalid key")
    key = (category, numeric, text, identity)
    valid = (
        (category == 0 and numeric <= 0 and bool(text) and identity > 0)
        or (category == 1 and numeric < 0 and text == "" and identity == 0)
        or (category == 2 and numeric == 0 and bool(text) and identity == 0)
    )
    if not valid:
        raise CurationStateError("curation cursor has an invalid key")
    return key


def _encode_cursor(snapshot_id: str, key: _SortKey) -> str:
    payload = {
        "contract": _CURSOR_CONTRACT,
        "key": list(key),
        "snapshot_id": snapshot_id,
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload).encode("utf-8"))
    return encoded.rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str | None) -> _CursorState | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise CurationStateError("curation cursor is invalid")
    try:
        encoded = cursor.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError) as error:
        raise CurationStateError("curation cursor is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract", "key", "snapshot_id"}
        or payload.get("contract") != _CURSOR_CONTRACT
        or not _valid_snapshot_id(payload.get("snapshot_id"))
    ):
        raise CurationStateError("curation cursor is invalid")
    state = _CursorState(
        snapshot_id=str(payload["snapshot_id"]),
        key=_validated_sort_key(payload["key"]),
    )
    if _encode_cursor(state.snapshot_id, state.key) != cursor:
        raise CurationStateError("curation cursor is not canonical")
    return state


def _semantic_snapshot_id(plan_digest: str) -> str:
    payload = _canonical_json({"contract": _SNAPSHOT_CONTRACT, "plan_digest": plan_digest}).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _plan_envelope(
    *,
    coverage: str,
    missing_owners: tuple[str, ...],
    head: _InventoryHead | None,
    duplicate_plan: _DuplicatePlanState | None,
    organization_plans: int,
    organization_scope: _OrganizationPlanScope | None,
    empty_files: int,
    source_heads: tuple[CurationSourceHead, ...],
) -> dict[str, object]:
    return {
        "contract": _PLAN_CONTRACT,
        "coverage": coverage,
        "duplicate_plan": (
            None
            if duplicate_plan is None
            else {
                "complete": duplicate_plan.complete,
                "completed_ns": duplicate_plan.completed_ns,
                "dangling_groups": duplicate_plan.dangling_groups,
                "groups": duplicate_plan.groups,
                "reclaimable_bytes": duplicate_plan.reclaimable_bytes,
                "nominal_redundant_bytes": duplicate_plan.reclaimable_bytes,
                "physical_reclaimable_bytes": None,
                "redundant_members": duplicate_plan.redundant_members,
                "verification_mode": duplicate_plan.verification_mode,
                "verification_scope": "plan",
                "requested_policy": duplicate_plan.requested_policy,
                "verification_coverage": duplicate_plan.verification_coverage,
                "exact_comparisons": duplicate_plan.exact_comparisons,
                "changed_or_unreadable_files": duplicate_plan.changed_or_unreadable_files,
            }
        ),
        "empty_files": empty_files,
        "inventory_head": (
            None
            if head is None
            else {
                "checkpoint_updated_ns": head.checkpoint_updated_ns,
                "inventory_files": head.inventory_files,
                "root": head.root,
                "scan_id": head.scan_id,
            }
        ),
        "missing_owners": list(missing_owners),
        "organization_plans": organization_plans,
        "organization_scope": (
            None
            if organization_scope is None
            else {
                "catalog_run_id": organization_scope.catalog_run_id,
                "organization_root": organization_scope.organization_root,
            }
        ),
        "schema_version": CURATION_PLAN_PAGE_SCHEMA_VERSION,
        "source_heads": [head.to_dict() for head in source_heads],
    }


def _consume_page_item(
    key: _SortKey,
    item: CurationItem,
    *,
    cursor_state: _CursorState | None,
    page_items: list[CurationItem],
    page_keys: list[_SortKey],
    limit: int,
    cursor_found: list[bool],
    has_more: list[bool],
) -> None:
    # SQL keyset predicates normally make the first branch unnecessary.  Keep
    # this helper defensive for callers that provide an already materialized
    # candidate, and never scan forward looking for the cursor row.
    if cursor_state is not None and key <= cursor_state.key:
        cursor_found[0] = True
        return
    if len(page_items) < limit:
        page_items.append(item)
        page_keys.append(key)
    else:
        has_more[0] = True


def _digest_plan_publication(
    *,
    inventory: sqlite3.Connection,
    catalog: sqlite3.Connection | None,
    missing_owners: tuple[str, ...],
    head: _InventoryHead,
    duplicate_plan: _DuplicatePlanState,
    organization_scope: _OrganizationPlanScope | None,
    organization_plans: int,
    empty_files: int,
    budget: KnowledgeReadBudget | None = None,
) -> _PlanPublication:
    """Publish one complete digest by streaming owner rows exactly once."""

    source_heads = _source_heads(
        missing_owners=missing_owners,
        inventory_head=head,
        duplicate_plan=duplicate_plan,
        catalog=catalog,
        organization_scope=organization_scope,
        organization_plans=organization_plans,
    )
    coverage = (
        "complete"
        if (
            not missing_owners
            and duplicate_plan.complete
            and duplicate_plan.verification_mode != "partial"
        )
        else "partial"
    )
    digest = hashlib.sha256()
    _digest_record(
        digest,
        "plan",
        _plan_envelope(
            coverage=coverage,
            missing_owners=missing_owners,
            head=head,
            duplicate_plan=duplicate_plan,
            organization_plans=organization_plans,
            organization_scope=organization_scope,
            empty_files=empty_files,
            source_heads=source_heads,
        ),
    )

    duplicate_count = 0
    if duplicate_plan.complete:
        duplicate_count = _digest_duplicate_groups(
            inventory, head.scan_id, digest, duplicate_plan.verification_mode, budget,
        )

    organization_count = 0
    if catalog is not None:
        for row in _iter_organization_rows(
            catalog,
            inventory_root=head.root,
            scope=organization_scope,
        ):
            if budget is not None:
                budget.checkpoint(rows=1)
            item = _organization_item_from_row(row)
            _digest_record(digest, "organization_plan", item.to_dict())
            organization_count += 1

    empty_count = 0
    rows = inventory.execute(
        """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files WHERE scan_id=? AND size=0 ORDER BY path COLLATE BINARY""",
        (head.scan_id,),
    )
    for row in rows:
        if budget is not None:
            budget.checkpoint(rows=1)
        _digest_record(digest, "empty_file", _empty_file_item(head.scan_id, row).to_dict())
        empty_count += 1

    if duplicate_count != duplicate_plan.groups:
        raise CurationStateError("duplicate plan item count changed while publishing its digest")
    if organization_count != organization_plans:
        raise CurationStateError("organization plan item count changed while publishing its digest")
    if empty_count != empty_files:
        raise CurationStateError("empty-file item count changed while publishing its digest")

    plan_digest = "sha256:" + digest.hexdigest()
    return _PlanPublication(
        head=head,
        duplicate_plan=duplicate_plan,
        organization_scope=organization_scope,
        organization_plans=organization_plans,
        empty_files=empty_files,
        coverage=coverage,
        source_heads=source_heads,
        plan_digest=plan_digest,
        snapshot_id=_semantic_snapshot_id(plan_digest),
    )


def _plan_publication(
    *,
    inventory: sqlite3.Connection,
    catalog: sqlite3.Connection | None,
    missing_owners: tuple[str, ...],
    head: _InventoryHead,
    duplicate_plan: _DuplicatePlanState,
    cache_key: tuple[object, ...],
    budget: KnowledgeReadBudget | None = None,
) -> _PlanPublication:
    """Get or create the complete publication for one fenced owner set."""

    with _PLAN_PUBLICATION_LOCK:
        cached = _PLAN_PUBLICATION_CACHE.get(cache_key)
        if cached is not None:
            if cached.head == head and cached.duplicate_plan == duplicate_plan:
                _PLAN_PUBLICATION_CACHE.move_to_end(cache_key)
                return cached
            _PLAN_PUBLICATION_CACHE.pop(cache_key, None)

        organization_scope = (
            None if catalog is None else _organization_plan_scope(catalog, inventory_root=head.root)
        )
        organization_plans = (
            0
            if catalog is None
            else _organization_summary(
                catalog,
                inventory_root=head.root,
                scope=organization_scope,
            )
        )
        empty_files = _empty_file_summary(inventory, head.scan_id)
        publication = _digest_plan_publication(
            inventory=inventory,
            catalog=catalog,
            missing_owners=missing_owners,
            head=head,
            duplicate_plan=duplicate_plan,
            organization_scope=organization_scope,
            organization_plans=organization_plans,
            empty_files=empty_files,
            budget=budget,
        )
        _PLAN_PUBLICATION_CACHE[cache_key] = publication
        _PLAN_PUBLICATION_CACHE.move_to_end(cache_key)
        while len(_PLAN_PUBLICATION_CACHE) > _PLAN_PUBLICATION_CACHE_LIMIT:
            _PLAN_PUBLICATION_CACHE.popitem(last=False)
        return publication


def _cursor_key_exists(
    *,
    inventory: sqlite3.Connection,
    catalog: sqlite3.Connection | None,
    publication: _PlanPublication,
    cursor_state: _CursorState,
) -> bool:
    """Validate a cursor boundary with one indexed point lookup."""

    category, numeric, text, identity = cursor_state.key
    if category == 0:
        row = inventory.execute(
            """SELECT 1 FROM planned_duplicate_groups
            WHERE scan_id=? AND reclaimable_bytes=?
              AND keep_path COLLATE BINARY=? COLLATE BINARY AND group_id=?
            LIMIT 1""",
            (publication.head.scan_id, -numeric, text, identity),
        ).fetchone()
    elif category == 1:
        if catalog is None or publication.organization_scope is None:
            return False
        source_predicate, source_parameters = _organization_source_predicate(publication.head.root)
        row = catalog.execute(
            """SELECT 1 FROM organization_plans
            WHERE plan_id=? AND catalog_run_id=?
              AND organization_root COLLATE BINARY=? COLLATE BINARY
              AND status<>'superseded' AND """
            + source_predicate
            + " LIMIT 1",
            (
                -numeric,
                publication.organization_scope.catalog_run_id,
                publication.organization_scope.organization_root,
                *source_parameters,
            ),
        ).fetchone()
    else:
        row = inventory.execute(
            """SELECT 1 FROM files
            WHERE scan_id=? AND size=0 AND path COLLATE BINARY=? COLLATE BINARY
            LIMIT 1""",
            (publication.head.scan_id, text),
        ).fetchone()
    return row is not None


def _duplicate_page_rows(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    after: _SortKey | None,
    limit: int,
) -> tuple[list[Any], bool]:
    """Fetch a duplicate keyset page plus one lookahead row."""

    parameters: list[object] = [scan_id]
    after_sql = ""
    if after is not None:
        _category, numeric, text, identity = after
        reclaimable = -numeric
        after_sql = """ AND (
            reclaimable_bytes < ?
            OR (reclaimable_bytes = ? AND keep_path COLLATE BINARY > ?)
            OR (reclaimable_bytes = ? AND keep_path COLLATE BINARY = ?
                AND group_id > ?)
        )"""
        parameters.extend((reclaimable, reclaimable, text, reclaimable, text, identity))
    parameters.append(limit + 1)
    projection = _duplicate_group_projection(connection)
    rows = connection.execute(
        f"""SELECT {projection} FROM planned_duplicate_groups
        WHERE scan_id=?"""
        + after_sql
        + """
        ORDER BY reclaimable_bytes DESC,keep_path COLLATE BINARY,group_id
        LIMIT ?""",
        tuple(parameters),
    ).fetchmany(limit + 1)
    return rows[:limit], len(rows) > limit


def _organization_page_rows(
    connection: sqlite3.Connection,
    *,
    inventory_root: str,
    scope: _OrganizationPlanScope | None,
    after_plan_id: int | None,
    limit: int,
) -> tuple[list[Any], bool]:
    """Fetch catalog keyset rows plus one lookahead row."""

    if scope is None:
        return [], False
    source_predicate, source_parameters = _organization_source_predicate(inventory_root)
    parameters: list[object] = [scope.catalog_run_id, scope.organization_root]
    after_sql = ""
    if after_plan_id is not None:
        after_sql = " AND plan_id < ?"
        parameters.append(after_plan_id)
    parameters.extend(source_parameters)
    parameters.append(limit + 1)
    bindings = _organization_binding_projection(connection)
    rows = connection.execute(
        f"""SELECT plan_id,catalog_run_id,source_kind,file_key,source_path,
        destination_path,organization_root,volume_id,file_id,size,mtime_ns,
        birthtime_ns,classifier_signature,primary_kind,confidence,status,reason,
        evidence_json,{bindings} FROM organization_plans
        WHERE catalog_run_id=?
          AND organization_root COLLATE BINARY=? COLLATE BINARY
          AND status<>'superseded'"""
        + after_sql
        + " AND "
        + source_predicate
        + """
        ORDER BY plan_id DESC
        LIMIT ?""",
        tuple(parameters),
    ).fetchmany(limit + 1)
    return rows[:limit], len(rows) > limit


def _empty_page_rows(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    after_path: str | None,
    limit: int,
) -> tuple[list[Any], bool]:
    """Fetch empty-file keyset rows plus one lookahead row."""

    parameters: list[object] = [scan_id]
    after_sql = ""
    if after_path is not None:
        after_sql = " AND path COLLATE BINARY > ? COLLATE BINARY"
        parameters.append(after_path)
    parameters.append(limit + 1)
    rows = connection.execute(
        """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files WHERE scan_id=? AND size=0"""
        + after_sql
        + """
        ORDER BY path COLLATE BINARY
        LIMIT ?""",
        tuple(parameters),
    ).fetchmany(limit + 1)
    return rows[:limit], len(rows) > limit


def _build_plan_page_from_connections(
    *,
    inventory: sqlite3.Connection,
    catalog: sqlite3.Connection | None,
    missing_owners: tuple[str, ...],
    head: _InventoryHead,
    duplicate_plan: _DuplicatePlanState,
    limit: int,
    cursor: str | None,
    cursor_state: _CursorState | None,
    cache_key: tuple[object, ...],
    budget: KnowledgeReadBudget | None = None,
) -> CurationPlanPage:
    publication = _plan_publication(
        inventory=inventory,
        catalog=catalog,
        missing_owners=missing_owners,
        head=head,
        duplicate_plan=duplicate_plan,
        cache_key=cache_key,
        budget=budget,
    )
    if cursor_state is not None:
        if cursor_state.snapshot_id != publication.snapshot_id:
            raise CurationStateError("curation cursor snapshot changed")
        if not _cursor_key_exists(
            inventory=inventory,
            catalog=catalog,
            publication=publication,
            cursor_state=cursor_state,
        ):
            raise CurationStateError("curation cursor key is not present in its snapshot")

    page_items: list[CurationItem] = []
    page_keys: list[_SortKey] = []
    cursor_found = [True]
    has_more = [False]
    remaining = limit
    cursor_category = -1 if cursor_state is None else cursor_state.key[0]
    after_key = None if cursor_state is None else cursor_state.key

    def consume(key: _SortKey, item: CurationItem) -> None:
        _consume_page_item(
            key,
            item,
            cursor_state=None,
            page_items=page_items,
            page_keys=page_keys,
            limit=limit,
            cursor_found=cursor_found,
            has_more=has_more,
        )

    if publication.duplicate_plan.complete and cursor_category <= 0:
        query_limit = remaining or 1
        rows, has_more[0] = _duplicate_page_rows(
            inventory,
            scan_id=head.scan_id,
            after=after_key if cursor_category == 0 else None,
            limit=query_limit,
        )
        if budget is not None:
            budget.checkpoint(rows=min(len(rows), query_limit))
        if remaining:
            for row in rows:
                key = (0, -int(row[4]), str(row[2]), int(row[0]))
                consume(
                    key,
                    _duplicate_item(
                        inventory,
                        head.scan_id,
                        row,
                        None,
                        publication.duplicate_plan.verification_mode,
                    ),
                )
            remaining -= len(rows)
        else:
            has_more[0] = bool(rows) or has_more[0]

    if catalog is not None and cursor_category <= 1 and not has_more[0]:
        query_limit = remaining or 1
        rows, has_more[0] = _organization_page_rows(
            catalog,
            inventory_root=head.root,
            scope=publication.organization_scope,
            after_plan_id=(-after_key[1] if cursor_category == 1 and after_key else None),
            limit=query_limit,
        )
        if budget is not None:
            budget.checkpoint(rows=min(len(rows), query_limit))
        if remaining:
            for row in rows:
                consume((1, -int(row[0]), "", 0), _organization_item_from_row(row))
            remaining -= len(rows)
        else:
            has_more[0] = bool(rows) or has_more[0]

    if cursor_category <= 2 and not has_more[0]:
        query_limit = remaining or 1
        rows, has_more[0] = _empty_page_rows(
            inventory,
            scan_id=head.scan_id,
            after_path=(after_key[2] if cursor_category == 2 and after_key else None),
            limit=query_limit,
        )
        if budget is not None:
            budget.checkpoint(rows=min(len(rows), query_limit))
        if remaining:
            for row in rows:
                consume((2, 0, str(row[0]), 0), _empty_file_item(head.scan_id, row))
            remaining -= len(rows)
        else:
            has_more[0] = bool(rows) or has_more[0]

    next_cursor = (
        _encode_cursor(publication.snapshot_id, page_keys[-1])
        if has_more[0] and page_keys
        else None
    )
    return CurationPlanPage(
        schema_version=CURATION_PLAN_PAGE_SCHEMA_VERSION,
        coverage=publication.coverage,
        missing_owners=missing_owners,
        root=head.root,
        scan_id=head.scan_id,
        inventory_files=head.inventory_files,
        duplicate_groups=publication.duplicate_plan.groups,
        duplicate_members=publication.duplicate_plan.redundant_members,
        reclaimable_bytes=publication.duplicate_plan.reclaimable_bytes,
        organization_plans=publication.organization_plans,
        empty_files=publication.empty_files,
        limit=limit,
        cursor=cursor,
        next_cursor=next_cursor,
        snapshot_id=publication.snapshot_id,
        plan_digest=publication.plan_digest,
        items_total=(
            publication.duplicate_plan.groups
            + publication.organization_plans
            + publication.empty_files
        ),
        items=tuple(page_items),
        source_heads=publication.source_heads,
    )


def _unavailable_plan_page(
    *,
    missing_owners: tuple[str, ...],
    limit: int,
    cursor: str | None,
    cursor_state: _CursorState | None,
) -> CurationPlanPage:
    source_heads = _source_heads(
        missing_owners=missing_owners,
        inventory_head=None,
        duplicate_plan=None,
        catalog=None,
        organization_scope=None,
        organization_plans=0,
    )
    digest = hashlib.sha256()
    _digest_record(
        digest,
        "plan",
        _plan_envelope(
            coverage="unavailable",
            missing_owners=missing_owners,
            head=None,
            duplicate_plan=None,
            organization_plans=0,
            organization_scope=None,
            empty_files=0,
            source_heads=source_heads,
        ),
    )
    plan_digest = "sha256:" + digest.hexdigest()
    snapshot_id = _semantic_snapshot_id(plan_digest)
    if cursor_state is not None:
        if cursor_state.snapshot_id != snapshot_id:
            raise CurationStateError("curation cursor snapshot changed")
        raise CurationStateError("curation cursor key is not present in its snapshot")
    return CurationPlanPage(
        schema_version=CURATION_PLAN_PAGE_SCHEMA_VERSION,
        coverage="unavailable",
        missing_owners=missing_owners,
        root=None,
        scan_id=None,
        inventory_files=0,
        duplicate_groups=0,
        duplicate_members=0,
        reclaimable_bytes=0,
        organization_plans=0,
        empty_files=0,
        limit=limit,
        cursor=cursor,
        next_cursor=None,
        snapshot_id=snapshot_id,
        plan_digest=plan_digest,
        items_total=0,
        items=(),
        source_heads=source_heads,
    )


def _preview_fingerprint(
    *,
    coverage: str,
    missing_owners: tuple[str, ...],
    scan_id: int | None,
    root: str | None,
    inventory_files: int,
    duplicate_groups: int,
    duplicate_members: int,
    reclaimable_bytes: int,
    organization_plans: int,
    empty_files: int,
    preview_limit: int,
    items: list[CurationItem],
    source_heads: tuple[CurationSourceHead, ...] = (),
) -> str:
    payload = {
        "coverage": coverage,
        "empty_files": empty_files,
        "duplicate_groups": duplicate_groups,
        "duplicate_members": duplicate_members,
        "inventory_files": inventory_files,
        "items": [item.to_dict() for item in items],
        "missing_owners": list(missing_owners),
        "organization_plans": organization_plans,
        "preview_limit": preview_limit,
        "reclaimable_bytes": reclaimable_bytes,
        "root": root,
        "scan_id": scan_id,
        "schema_version": CURATION_PREVIEW_SCHEMA_VERSION,
        "source_heads": [head.to_dict() for head in source_heads],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_curation_plan_page(
    state_directory: Path,
    limit: int,
    cursor: str | None = None,
    budget: KnowledgeReadBudget | None = None,
) -> CurationPlanPage:
    """Read one stable keyset page without mutating owners or corpus content."""

    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("curation page limit must be between 1 and 10000")
    if budget is not None and not isinstance(budget, KnowledgeReadBudget):
        raise TypeError("budget must be a KnowledgeReadBudget")
    cursor_state = _decode_cursor(cursor)
    state_path = Path(state_directory)
    inventory_path = state_path / "dedup.sqlite3"
    catalog_path = state_path / "document_catalog.sqlite3"
    missing_owners = tuple(
        filename
        for path, filename in (
            (inventory_path, "dedup.sqlite3"),
            (catalog_path, "document_catalog.sqlite3"),
        )
        if not _owner_is_regular_file(path, label=filename)
    )
    if "dedup.sqlite3" in missing_owners:
        return _unavailable_plan_page(
            missing_owners=missing_owners,
            limit=limit,
            cursor=cursor,
            cursor_state=cursor_state,
        )

    # The owner fence is captured before opening SQLite.  It is part of the
    # process-local publication key, so a changed main file or sidecar starts a
    # new digest generation rather than reusing a cursor-bound publication.
    inventory_generation = _owner_generation_key(inventory_path)
    catalog_generation = (
        None
        if "document_catalog.sqlite3" in missing_owners
        else _owner_generation_key(catalog_path)
    )
    cache_key: tuple[object, ...] = (
        str(state_path.absolute()),
        inventory_generation,
        catalog_generation,
        missing_owners,
    )
    cached_publication = _cached_plan_publication(cache_key)

    with _readonly_sqlite_connection(
        inventory_path,
        label="dedup",
        expected_generation=inventory_generation,
        budget=budget,
    ) as inventory:
        _validate_readable_inventory(inventory)
        if cached_publication is None:
            head = _published_inventory_head(inventory)
            duplicate_plan = _duplicate_plan_state(inventory, head.scan_id)
        else:
            # The fenced owner identities are unchanged, so reusing these
            # publication facts avoids recounting every inventory file/member
            # on each cursor request.  The page still reads selected rows from
            # the live fenced connection below.
            head = cached_publication.head
            duplicate_plan = cached_publication.duplicate_plan
        if "document_catalog.sqlite3" in missing_owners:
            return _build_plan_page_from_connections(
                inventory=inventory,
                catalog=None,
                missing_owners=missing_owners,
                head=head,
                duplicate_plan=duplicate_plan,
                limit=limit,
                cursor=cursor,
                cursor_state=cursor_state,
                cache_key=cache_key,
                budget=budget,
            )
        with _readonly_sqlite_connection(
            catalog_path,
            label="document catalog",
            expected_generation=catalog_generation,
            budget=budget,
        ) as catalog:
            _validate_readable_catalog(catalog)
            try:
                return _build_plan_page_from_connections(
                    inventory=inventory,
                    catalog=catalog,
                    missing_owners=missing_owners,
                    head=head,
                    duplicate_plan=duplicate_plan,
                    limit=limit,
                    cursor=cursor,
                    cursor_state=cursor_state,
                    cache_key=cache_key,
                    budget=budget,
                )
            except CurationStateError as error:
                publication = error.context.setdefault("publication", {})
                if isinstance(publication, dict):
                    publication.setdefault("scan_id", head.scan_id)
                    publication.setdefault("inventory_root", head.root)
                raise


def build_curation_preview(
    state_directory: Path,
    *,
    limit: int,
    budget: KnowledgeReadBudget | None = None,
) -> CurationPreview:
    """Compatibility facade over the first immutable curation-plan page."""

    page = build_curation_plan_page(state_directory, limit, None, budget)
    sampled_items = list(page.items)
    return CurationPreview(
        schema_version=CURATION_PREVIEW_SCHEMA_VERSION,
        coverage=page.coverage,
        missing_owners=page.missing_owners,
        root=page.root,
        scan_id=page.scan_id,
        inventory_files=page.inventory_files,
        duplicate_groups=page.duplicate_groups,
        duplicate_members=page.duplicate_members,
        reclaimable_bytes=page.reclaimable_bytes,
        organization_plans=page.organization_plans,
        empty_files=page.empty_files,
        preview_limit=limit,
        items_total=page.items_total,
        items_truncated=page.items_total > len(sampled_items),
        preview_fingerprint=_preview_fingerprint(
            coverage=page.coverage,
            missing_owners=page.missing_owners,
            scan_id=page.scan_id,
            root=page.root,
            inventory_files=page.inventory_files,
            duplicate_groups=page.duplicate_groups,
            duplicate_members=page.duplicate_members,
            reclaimable_bytes=page.reclaimable_bytes,
            organization_plans=page.organization_plans,
            empty_files=page.empty_files,
            preview_limit=limit,
            items=sampled_items,
            source_heads=page.source_heads,
        ),
        items=tuple(sampled_items),
        source_heads=page.source_heads,
    )


__all__ = [
    "CURATION_PLAN_PAGE_SCHEMA_VERSION",
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CurationItem",
    "CurationPlanPage",
    "CurationPreview",
    "CurationSourceHead",
    "CurationStateError",
    "build_curation_plan_page",
    "build_curation_preview",
]
