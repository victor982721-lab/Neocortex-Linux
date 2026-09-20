"""Bounded verification of published curation candidates.

The verifier is deliberately read-only with respect to the corpus and its
owners.  It consumes one already-published :class:`CurationPlanPage`, checks
the recorded physical identities, hashes the current regular files and then
performs the byte comparison required for an exact duplicate claim.  It never
creates workflow state or ``file_actions``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Iterator, Literal, cast

from neocortex.deduplication.domain.errors import FileChangedError
from neocortex.deduplication.domain.models import (
    VALID_VERIFICATION_MODES,
    FileSnapshot,
    VerificationMode,
)
from neocortex.deduplication.persistence.validation import validate_inventory_schema
from neocortex.deduplication.fingerprinting import (
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadSession,
    preferred_sqlite_read_mode,
)
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget, KnowledgeReadBudgetExceeded

from .preview import CurationItem, CurationPlanPage, CurationSourceHead


CURATION_VERIFICATION_SCHEMA_VERSION = 1
MAX_VERIFICATION_ITEMS = 100
MAX_VERIFICATION_FILES = 512
MAX_VERIFICATION_BYTES = 128 * 1024 * 1024
_READ_CHUNK_SIZE = 1024 * 1024
_AUTHORITATIVE_MEMBER_BATCH_SIZE = 64

VerificationStatus = Literal["verified", "source_changed", "not_verified", "not_applicable"]
WorkStopReason = Literal["budget_exhausted", "cancelled", "deadline_exceeded"]


class CurationVerificationError(RuntimeError):
    """The published curation evidence cannot be verified safely."""


class CurationVerificationSnapshotChanged(CurationVerificationError):
    """The plan or one of its physical sources changed during verification."""


class CurationVerificationUnavailable(CurationVerificationError):
    """The requested verification could not run with the available evidence."""

    def __init__(self, message: str, *, reason_code: str = "verification_unavailable") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class CurationWorkBudget:
    """Optional work limits for one read-only verification invocation.

    The monotonic deadline is an absolute value in the clock's domain. The
    injectable clock exists only to make the contract deterministic in tests;
    ordinary callers use :func:`time.monotonic`.
    """

    max_items: int = MAX_VERIFICATION_ITEMS
    max_files: int = MAX_VERIFICATION_FILES
    max_bytes: int = MAX_VERIFICATION_BYTES
    deadline_monotonic: float | None = None
    # ``None`` is the conventional return value of ``CancellationToken``
    # checkpoints; a truthy boolean requests a stop, while any other value is
    # rejected fail-closed.
    cancellation_check: Callable[[], bool | None] | None = None
    monotonic_clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("max_items", self.max_items, MAX_VERIFICATION_ITEMS),
            ("max_files", self.max_files, MAX_VERIFICATION_FILES),
            ("max_bytes", self.max_bytes, MAX_VERIFICATION_BYTES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} is outside the verification bound")
        deadline = self.deadline_monotonic
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or deadline < 0
        ):
            raise ValueError("deadline_monotonic must be a finite non-negative number")
        if self.cancellation_check is not None and not callable(self.cancellation_check):
            raise TypeError("cancellation_check must be callable")
        if not callable(self.monotonic_clock):
            raise TypeError("monotonic_clock must be callable")


class _WorkBudgetStop(CurationVerificationUnavailable):
    """Internal bounded stop signal that preserves completed observations."""

    def __init__(self, reason: WorkStopReason) -> None:
        super().__init__(reason.replace("_", " "), reason_code=reason)


class _WorkBudgetState:
    """Mutable accounting for one immutable :class:`CurationWorkBudget`."""

    def __init__(
        self,
        budget: CurationWorkBudget,
        *,
        max_items: int,
        max_files: int,
        max_bytes: int,
    ) -> None:
        self.budget = budget
        self.max_items = min(max_items, budget.max_items)
        self.max_files = min(max_files, budget.max_files)
        self.max_bytes = min(max_bytes, budget.max_bytes)
        self.items_started = 0
        self.files_checked = 0
        self.bytes_checked = 0
        self.knowledge_budget: KnowledgeReadBudget | None = None

    def _control_reason(self) -> WorkStopReason | None:
        callback = self.budget.cancellation_check
        if callback is not None:
            try:
                decision: object = callback()
                if decision is True:
                    return "cancelled"
                if decision is not False and decision is not None:
                    return "cancelled"
            except Exception:
                return "cancelled"
        deadline = self.budget.deadline_monotonic
        if deadline is not None:
            try:
                observed = self.budget.monotonic_clock()
                if (
                    isinstance(observed, bool)
                    or not isinstance(observed, (int, float))
                    or not math.isfinite(float(observed))
                    or observed >= deadline
                ):
                    return "deadline_exceeded"
            except Exception:
                return "deadline_exceeded"
        return None

    def start_item(self) -> None:
        if self.knowledge_budget is not None:
            try:
                self.knowledge_budget.checkpoint(rows=1)
            except KnowledgeReadBudgetExceeded as exc:
                reason = exc.reason
                if reason in {"rows_exhausted", "vectors_exhausted", "temporary_bytes_exhausted"}:
                    reason = "budget_exhausted"
                raise _WorkBudgetStop(cast(WorkStopReason, reason)) from exc
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)
        if self.items_started >= self.max_items:
            raise _WorkBudgetStop("budget_exhausted")
        self.items_started += 1

    def reserve_file(self, size: int) -> None:
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)
        if (
            self.files_checked >= self.max_files
            or size < 0
            or size > self.max_bytes - self.bytes_checked
        ):
            raise _WorkBudgetStop("budget_exhausted")
        self.files_checked += 1

    def before_read(self) -> None:
        reason = self._control_reason()
        if reason is not None:
            raise _WorkBudgetStop(reason)

    def record_bytes(self, count: int) -> None:
        if count < 0 or count > self.max_bytes - self.bytes_checked:
            raise _WorkBudgetStop("budget_exhausted")
        self.bytes_checked += count

    def record_temporary_bytes(self, count: int) -> None:
        if self.knowledge_budget is None or not count:
            return
        try:
            self.knowledge_budget.checkpoint(temporary_bytes=count)
        except KnowledgeReadBudgetExceeded as exc:
            reason = exc.reason
            if reason in {"rows_exhausted", "vectors_exhausted", "temporary_bytes_exhausted"}:
                reason = "budget_exhausted"
            raise _WorkBudgetStop(cast(WorkStopReason, reason)) from exc


@dataclass(frozen=True, slots=True)
class CurationDuplicateGroupReference:
    """Authoritative identity of one persisted duplicate group.

    ``CurationItem.evidence["members"]`` is intentionally only a bounded
    presentation sample.  This reference is resolved from the published
    inventory owner and therefore remains small even when a group has many
    members.  The scan is the inventory revision and the plan/snapshot values
    bind the lookup to the same published curation generation.
    """

    scan_id: int
    group_id: int
    size: int
    keep_path: str
    redundant_count: int
    reclaimable_bytes: int
    full_fingerprint: str

    @property
    def member_count(self) -> int:
        return self.redundant_count + 1


def _digest_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise CurationVerificationSnapshotChanged(f"{label} is invalid")
    return value


def _db_integer(value: object, *, label: str, minimum: int = 0) -> int:
    """Decode one inventory integer without accepting lossy coercions."""

    try:
        if isinstance(value, (bytes, bytearray, memoryview)):
            number = int.from_bytes(bytes(value), "little")
        elif isinstance(value, int) and not isinstance(value, bool):
            number = value
        elif isinstance(value, str) and value and value.strip() == value:
            number = int(value, 0 if value.lower().startswith("0x") else 10)
        else:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise CurationVerificationSnapshotChanged(f"inventory {label} is invalid") from exc
    if number < minimum:
        raise CurationVerificationSnapshotChanged(f"inventory {label} is invalid")
    return number


def _path_within_root(path: str, root: Path, *, label: str) -> None:
    try:
        Path(os.path.abspath(path)).relative_to(root)
    except (OSError, ValueError) as exc:
        raise CurationVerificationSnapshotChanged(
            f"authoritative {label} escapes the published root: {path}"
        ) from exc


def _member_snapshot_from_inventory_row(
    row: object,
    *,
    item_id: str,
    root: Path,
) -> tuple[int, str, FileSnapshot]:
    if not isinstance(row, (tuple, sqlite3.Row)) or len(row) != 8:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} has malformed authoritative member"
        )
    order = _db_integer(row[0], label="member_order")
    role = row[1]
    path = row[2]
    if not isinstance(role, str) or role not in {"keep", "redundant"}:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} has an unsupported authoritative member role"
        )
    if not isinstance(path, str) or not path.startswith("/") or path != os.path.abspath(path):
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} has a non-canonical authoritative member path"
        )
    _path_within_root(path, root, label="member path")
    volume_id = _db_integer(row[3], label="volume_id")
    file_id = _db_integer(row[4], label="file_id")
    size = _db_integer(row[5], label="size")
    mtime_ns = _db_integer(row[6], label="mtime_ns")
    birthtime_ns = _db_integer(row[7], label="birthtime_ns", minimum=-1)
    return order, role, FileSnapshot(path, volume_id, file_id, size, mtime_ns, birthtime_ns)


def _reference_from_item(
    item: CurationItem,
    *,
    scan_id: int,
    root: Path,
) -> tuple[int, str, str, int, int, int]:
    """Return the bounded lookup key and the expected persisted group fields."""

    if isinstance(scan_id, bool) or not isinstance(scan_id, int) or scan_id <= 0:
        raise CurationVerificationSnapshotChanged("curation inventory revision is invalid")
    group_id = item.evidence.get("group_id")
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item.item_id} lacks an authoritative group id"
        )
    expected_item_id = f"duplicate:{scan_id}:{group_id}"
    if item.item_id != expected_item_id:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item.item_id} is not bound to inventory revision {scan_id}"
        )
    evidence_scan_id = item.evidence.get("scan_id")
    if evidence_scan_id is not None and evidence_scan_id != scan_id:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item.item_id} evidence revision changed"
        )
    full_fingerprint = item.evidence.get("full_fingerprint")
    keep_path = item.evidence.get("keep_path")
    size = item.evidence.get("size")
    reclaimable_bytes = item.evidence.get("reclaimable_bytes")
    member_count = item.evidence.get("member_count")
    if (
        not isinstance(full_fingerprint, str)
        or not full_fingerprint
        or full_fingerprint.strip() != full_fingerprint
        or len(full_fingerprint) > 256
        or not isinstance(keep_path, str)
        or not keep_path.startswith("/")
        or keep_path != os.path.abspath(keep_path)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(reclaimable_bytes, bool)
        or not isinstance(reclaimable_bytes, int)
        or reclaimable_bytes < 0
        or isinstance(member_count, bool)
        or not isinstance(member_count, int)
        or member_count < 2
    ):
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item.item_id} has invalid authoritative group metadata"
        )
    if item.source_path != keep_path:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item.item_id} source path is not its authoritative keeper"
        )
    _path_within_root(keep_path, root, label="keeper path")
    return group_id, keep_path, full_fingerprint, size, reclaimable_bytes, member_count


def _validate_authoritative_inventory(
    connection: sqlite3.Connection,
    *,
    page: CurationPlanPage,
) -> None:
    """Bind one fenced inventory connection to the page publication.

    The owner is read through :class:`SQLiteReadSession`, so this validation
    never opens the live SQLite file with ordinary ``mode=ro``.  We validate
    the exact published scan and root before allowing any group-member query.
    """

    plan_digest = _digest_text(page.plan_digest, label="curation plan digest")
    snapshot_id = _digest_text(page.snapshot_id, label="curation snapshot id")
    expected_snapshot_payload = json.dumps(
        {"contract": "neocortex.curation-snapshot/v1", "plan_digest": plan_digest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_snapshot_id = "sha256:" + hashlib.sha256(expected_snapshot_payload).hexdigest()
    if snapshot_id != expected_snapshot_id:
        raise CurationVerificationSnapshotChanged("curation snapshot is not bound to the plan")
    if page.root is None or not page.root.startswith("/"):
        raise CurationVerificationSnapshotChanged("curation published root is unavailable")
    if (
        page.scan_id is None
        or isinstance(page.scan_id, bool)
        or not isinstance(page.scan_id, int)
        or page.scan_id <= 0
    ):
        raise CurationVerificationSnapshotChanged("curation inventory revision is unavailable")
    heads = tuple(
        head
        for head in page.source_heads
        if head.owner == "dedup.sqlite3" and head.kind == "inventory"
    )
    if len(heads) != 1:
        raise CurationVerificationSnapshotChanged(
            "curation page lacks one authoritative inventory source head"
        )
    head = heads[0]
    if (
        head.head_id != f"scan:{page.scan_id}"
        or head.revision != page.scan_id
        or head.root != page.root
        or head.coverage != "complete"
        or head.item_count != page.inventory_files
    ):
        raise CurationVerificationSnapshotChanged(
            "curation inventory source head is not bound to the published page"
        )
    try:
        head_payload = {
            "coverage": head.coverage,
            "head_id": head.head_id,
            "item_count": head.item_count,
            "kind": head.kind,
            "metadata": dict(head.metadata),
            "owner": head.owner,
            "reason": head.reason,
            "revision": head.revision,
            "root": head.root,
            "verification_mode": head.verification_mode,
        }
        expected_head_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                head_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CurationVerificationSnapshotChanged(
            "curation inventory source-head digest is invalid"
        ) from exc
    if head.digest != expected_head_digest:
        raise CurationVerificationSnapshotChanged(
            "curation inventory source-head digest changed"
        )
    row = connection.execute(
        """SELECT c.scan_id,s.root,s.files_seen,c.updated_ns,
        (SELECT COUNT(*) FROM files f WHERE f.scan_id=s.scan_id) AS stored_files
        FROM inventory_checkpoints c
        JOIN scans s ON s.scan_id=c.scan_id AND s.root=c.root COLLATE BINARY
        WHERE c.valid=1 AND s.status='complete' AND s.errors=0
          AND s.completed_ns IS NOT NULL AND c.scan_id=? AND c.root=?
        LIMIT 1""",
        (page.scan_id, page.root),
    ).fetchone()
    if row is None or any(value is None for value in row):
        raise CurationVerificationSnapshotChanged(
            "published inventory revision is no longer available"
        )
    if _db_integer(row[0], label="scan_id") != page.scan_id:
        raise CurationVerificationSnapshotChanged("published inventory revision changed")
    inventory_files = _db_integer(row[2], label="files_seen")
    stored_files = _db_integer(row[4], label="stored_files")
    if inventory_files != stored_files or inventory_files != page.inventory_files:
        raise CurationVerificationSnapshotChanged("published inventory file count changed")
    metadata = dict(head.metadata)
    if (
        "checkpoint_updated_ns" in metadata
        and metadata["checkpoint_updated_ns"] != _db_integer(
            row[3], label="checkpoint updated timestamp"
        )
    ):
        raise CurationVerificationSnapshotChanged("inventory checkpoint publication changed")
    summary = connection.execute(
        """SELECT group_count,redundant_files,reclaimable_bytes,
        verification_mode FROM duplicate_plan_summaries WHERE scan_id=?""",
        (page.scan_id,),
    ).fetchone()
    if summary is None:
        raise CurationVerificationSnapshotChanged(
            "published duplicate plan summary is unavailable"
        )
    if (
        _db_integer(summary[0], label="duplicate group count") != page.duplicate_groups
        or _db_integer(summary[1], label="duplicate member count") != page.duplicate_members
        or _db_integer(summary[2], label="reclaimable bytes") != page.reclaimable_bytes
        or not isinstance(summary[3], str)
        or summary[3] not in VALID_VERIFICATION_MODES
        or head.verification_mode != summary[3]
    ):
        raise CurationVerificationSnapshotChanged("published duplicate plan summary changed")
    if (
        (
            "duplicate_groups" in metadata
            and metadata["duplicate_groups"] != page.duplicate_groups
        )
        or (
            "duplicate_members" in metadata
            and metadata["duplicate_members"] != page.duplicate_members
        )
        or (
            "reclaimable_bytes" in metadata
            and metadata["reclaimable_bytes"] != page.reclaimable_bytes
        )
    ):
        raise CurationVerificationSnapshotChanged("inventory source-head metadata changed")


def _authoritative_group_reference(
    connection: sqlite3.Connection,
    item: CurationItem,
    *,
    page: CurationPlanPage,
) -> CurationDuplicateGroupReference:
    """Resolve one group header without trusting the preview member sample."""

    if page.scan_id is None or isinstance(page.scan_id, bool) or page.scan_id <= 0:
        raise CurationVerificationSnapshotChanged("curation inventory revision is unavailable")
    scan_id = page.scan_id
    root = _bounded_root(page.root)
    group_id, keep_path, fingerprint, size, reclaimable, member_count = _reference_from_item(
        item,
        scan_id=scan_id,
        root=root,
    )
    row = connection.execute(
        """SELECT group_id,size,keep_path,redundant_count,reclaimable_bytes,
        full_fingerprint FROM planned_duplicate_groups
        WHERE scan_id=? AND group_id=?""",
        (page.scan_id, group_id),
    ).fetchone()
    if row is None:
        raise CurationVerificationSnapshotChanged(
            f"duplicate group {group_id} is not in the published inventory revision"
        )
    persisted_group_id = _db_integer(row[0], label="group_id")
    persisted_size = _db_integer(row[1], label="group size")
    persisted_keep = row[2]
    persisted_redundant = _db_integer(row[3], label="redundant count")
    persisted_reclaimable = _db_integer(row[4], label="reclaimable bytes")
    persisted_fingerprint = row[5]
    if (
        persisted_group_id != group_id
        or not isinstance(persisted_keep, str)
        or persisted_keep != keep_path
        or persisted_size != size
        or persisted_redundant + 1 != member_count
        or persisted_reclaimable != reclaimable
        or not isinstance(persisted_fingerprint, str)
        or persisted_fingerprint != fingerprint
        or persisted_reclaimable != persisted_size * persisted_redundant
    ):
        raise CurationVerificationSnapshotChanged(
            f"duplicate group {group_id} metadata changed in the published revision"
        )
    member_summary = connection.execute(
        """SELECT COUNT(*),
        SUM(CASE WHEN role='keep' THEN 1 ELSE 0 END),
        SUM(CASE WHEN role='redundant' THEN 1 ELSE 0 END),
        MIN(member_order),MAX(member_order)
        FROM planned_duplicate_members WHERE group_id=?""",
        (group_id,),
    ).fetchone()
    if member_summary is None or any(value is None for value in member_summary):
        raise CurationVerificationSnapshotChanged(
            f"duplicate group {group_id} has no authoritative members"
        )
    stored_count = _db_integer(member_summary[0], label="member count")
    keep_count = _db_integer(member_summary[1], label="keeper count")
    redundant_count = _db_integer(member_summary[2], label="redundant member count")
    minimum_order = _db_integer(member_summary[3], label="minimum member order")
    maximum_order = _db_integer(member_summary[4], label="maximum member order")
    if (
        stored_count != member_count
        or keep_count != 1
        or redundant_count != persisted_redundant
        or minimum_order != 0
        or maximum_order != member_count - 1
    ):
        raise CurationVerificationSnapshotChanged(
            f"duplicate group {group_id} membership is incomplete or inconsistent"
        )
    return CurationDuplicateGroupReference(
        scan_id=scan_id,
        group_id=group_id,
        size=persisted_size,
        keep_path=persisted_keep,
        redundant_count=persisted_redundant,
        reclaimable_bytes=persisted_reclaimable,
        full_fingerprint=persisted_fingerprint,
    )


def _authoritative_keeper(
    connection: sqlite3.Connection,
    reference: CurationDuplicateGroupReference,
    *,
    item_id: str,
    root: Path,
) -> FileSnapshot:
    rows = connection.execute(
        """SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM planned_duplicate_members
        WHERE group_id=? AND role='keep' ORDER BY member_order LIMIT 2""",
        (reference.group_id,),
    ).fetchall()
    if len(rows) != 1:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} has an invalid authoritative keeper"
        )
    _order, role, snapshot = _member_snapshot_from_inventory_row(
        rows[0], item_id=item_id, root=root
    )
    if role != "keep" or snapshot.path != reference.keep_path:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} keeper is not bound to its group"
        )
    if snapshot.size != reference.size:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} keeper size changed"
        )
    return snapshot


def _iter_authoritative_redundant_members(
    connection: sqlite3.Connection,
    reference: CurationDuplicateGroupReference,
    *,
    item_id: str,
    root: Path,
    keeper: FileSnapshot,
    batch_size: int = _AUTHORITATIVE_MEMBER_BATCH_SIZE,
) -> Iterator[FileSnapshot]:
    """Yield complete membership in bounded SQLite batches, never as a list."""

    if type(batch_size) is not int or not 1 <= batch_size <= _AUTHORITATIVE_MEMBER_BATCH_SIZE:
        raise ValueError("authoritative member batch size is outside its bound")
    cursor = connection.execute(
        """SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM planned_duplicate_members
        WHERE group_id=? ORDER BY member_order""",
        (reference.group_id,),
    )
    expected_order = 0
    keepers = 0
    yielded = 0
    seen_paths = {keeper.path}
    seen_identities = {keeper.identity}
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            order, role, snapshot = _member_snapshot_from_inventory_row(
                row, item_id=item_id, root=root
            )
            if order != expected_order:
                raise CurationVerificationSnapshotChanged(
                    f"duplicate item {item_id} authoritative member order changed"
                )
            expected_order += 1
            if role == "keep":
                if snapshot != keeper:
                    raise CurationVerificationSnapshotChanged(
                        f"duplicate item {item_id} authoritative keeper changed"
                    )
                keepers += 1
                continue
            if role != "redundant":
                raise CurationVerificationSnapshotChanged(
                    f"duplicate item {item_id} authoritative member role changed"
                )
            if snapshot.size != reference.size:
                raise CurationVerificationSnapshotChanged(
                    f"duplicate item {item_id} authoritative member size changed"
                )
            if snapshot.path in seen_paths or snapshot.identity in seen_identities:
                raise CurationVerificationSnapshotChanged(
                    f"duplicate item {item_id} authoritative member identity is duplicated"
                )
            seen_paths.add(snapshot.path)
            seen_identities.add(snapshot.identity)
            yielded += 1
            yield snapshot
    if keepers != 1 or yielded != reference.redundant_count:
        raise CurationVerificationSnapshotChanged(
            f"duplicate item {item_id} authoritative member count changed"
        )


@contextmanager
def _authoritative_inventory_session(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the inventory owner through the sidecar-safe read contract."""

    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        mode = preferred_sqlite_read_mode(path)
        with SQLiteReadSession(path, mode=mode, timeout_seconds=60.0) as connection:
            validate_inventory_schema(connection)
            yield connection
    except FileNotFoundError as exc:
        raise CurationVerificationUnavailable(
            "authoritative duplicate membership owner is unavailable",
            reason_code="owner_unavailable",
        ) from exc
    except ImmutableSQLiteUnavailable as exc:
        message = str(exc).casefold()
        if "changed" in message:
            raise CurationVerificationSnapshotChanged(
                "authoritative duplicate membership owner changed"
            ) from exc
        raise CurationVerificationUnavailable(
            "authoritative duplicate membership owner cannot be read",
            reason_code="owner_unavailable",
        ) from exc


def _authoritative_member_rows(
    connection: sqlite3.Connection,
    item: CurationItem,
    *,
    page: CurationPlanPage,
) -> tuple[CurationDuplicateGroupReference, FileSnapshot, Iterator[FileSnapshot]]:
    """Resolve a group header, keeper and bounded redundant-member stream."""

    reference = _authoritative_group_reference(connection, item, page=page)
    root = _bounded_root(page.root)
    keeper = _authoritative_keeper(connection, reference, item_id=item.item_id, root=root)
    redundant = _iter_authoritative_redundant_members(
        connection,
        reference,
        item_id=item.item_id,
        root=root,
        keeper=keeper,
    )
    return reference, keeper, redundant


@dataclass(slots=True)
class _VerificationMetrics:
    """Bounded payload-buffer metrics for one verification page."""

    chunks: int = 0
    peak_buffer: int = 0
    keeper_replays: int = 0

    def observe_chunk(self, source_size: int, reference_size: int = 0) -> None:
        self.chunks += 1
        self.peak_buffer = max(self.peak_buffer, source_size + reference_size)


@dataclass(frozen=True, slots=True)
class CurationVerificationItem:
    """Verification result for one curation item."""

    item_id: str
    kind: str
    source_path: str
    persisted_mode: VerificationMode | None
    observed_mode: Literal["full_hash"] | None
    status: VerificationStatus
    reason: str
    checked_files: int
    verified_files: int

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_files": self.checked_files,
            "item_id": self.item_id,
            "kind": self.kind,
            "observed_mode": self.observed_mode,
            "persisted_mode": self.persisted_mode,
            "reason": self.reason,
            "source_path": self.source_path,
            "status": self.status,
            "verified_files": self.verified_files,
        }


@dataclass(frozen=True, slots=True)
class CurationVerificationResult:
    """Bounded result for a page or selected items."""

    plan_digest: str
    snapshot_id: str
    coverage: Literal["complete", "partial"]
    status: Literal["complete", "partial", "snapshot_changed"]
    items_total: int
    items_verified: int
    items_failed: int
    items_skipped: int
    files_checked: int
    bytes_checked: int
    items: tuple[CurationVerificationItem, ...]
    source_heads: tuple[CurationSourceHead, ...] = ()
    metrics: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if self.metrics is not None:
            if any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in self.metrics.items()
            ):
                raise ValueError("verification metrics must contain non-negative integers")
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    def to_dict(self) -> dict[str, object]:
        return {
            "bytes_checked": self.bytes_checked,
            "coverage": self.coverage,
            "files_checked": self.files_checked,
            "items": [item.to_dict() for item in self.items],
            "items_failed": self.items_failed,
            "items_skipped": self.items_skipped,
            "items_total": self.items_total,
            "items_verified": self.items_verified,
            "plan_digest": self.plan_digest,
            "snapshot_id": self.snapshot_id,
            "source_heads": [head.to_dict() for head in self.source_heads],
            "status": self.status,
            "metrics": (None if self.metrics is None else dict(self.metrics)),
        }


def _bounded_root(value: object) -> Path:
    if not isinstance(value, str) or not value or not value.startswith("/"):
        raise CurationVerificationUnavailable(
            "curation plan root is not absolute",
            reason_code="root_invalid",
        )
    return Path(os.path.abspath(value))


def _assert_safe_path_components(root: Path, path: Path) -> None:
    """Reject a root or ancestor symlink before opening corpus content.

    A lexical ``relative_to`` check is insufficient when a directory below the
    root is replaced by a symlink.  Walk every directory component with
    ``lstat``; the final component is checked separately by the caller and by
    the descriptor-based reader below.
    """

    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation root disappeared: {root}"
        ) from exc
    except OSError as exc:
        raise CurationVerificationUnavailable(
            f"curation root cannot be inspected: {root}",
            reason_code="source_inspection_unavailable",
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise CurationVerificationSnapshotChanged(
            f"curation root is not a regular directory: {root}"
        )
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CurationVerificationSnapshotChanged(
            "curation source path escapes the published root"
        ) from exc
    current = root
    for component in relative.parts[:-1]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError as exc:
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor disappeared: {current}"
            ) from exc
        except OSError as exc:
            raise CurationVerificationUnavailable(
                f"curation source ancestor cannot be inspected: {current}",
                reason_code="source_inspection_unavailable",
            ) from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor is a symlink: {current}"
            )
        if not stat.S_ISDIR(component_stat.st_mode):
            raise CurationVerificationSnapshotChanged(
                f"curation source ancestor is not a directory: {current}"
            )


def _bounded_path(value: object, *, root: Path) -> Path:
    if not isinstance(value, str) or not value or not value.startswith("/"):
        raise CurationVerificationSnapshotChanged("curation source path is not absolute")
    path = Path(os.path.abspath(value))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CurationVerificationSnapshotChanged(
            "curation source path escapes the published root"
        ) from exc
    return path


def _identity_numbers(value: object) -> tuple[int, int, int]:
    if not isinstance(value, dict):
        raise CurationVerificationSnapshotChanged("curation member identity is invalid")
    try:
        volume = int(str(value["volume_id"]), 16)
        file_id = int(str(value["file_id"]), 16)
        birthtime = int(value["birthtime_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CurationVerificationSnapshotChanged(
            "curation member identity is invalid"
        ) from exc
    return volume, file_id, birthtime


def _snapshot_for_member(
    member: dict[str, object],
    *,
    root: Path,
) -> tuple[Path, Any]:
    path = _bounded_path(member.get("path"), root=root)
    _assert_safe_path_components(root, path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation source disappeared: {path}"
        ) from exc
    except OSError as exc:
        raise CurationVerificationUnavailable(
            f"curation source cannot be inspected: {path}",
            reason_code="source_inspection_unavailable",
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise CurationVerificationSnapshotChanged(
            f"curation source is not a regular file: {path}"
        )
    try:
        current = snapshot_path(path)
    except (FileChangedError, OSError, ValueError) as exc:
        raise CurationVerificationSnapshotChanged(
            f"curation source cannot be snapshotted: {path}"
        ) from exc
    expected_volume, expected_file, expected_birth = _identity_numbers(member.get("identity"))
    expected_size = member.get("size")
    expected_mtime = member.get("mtime_ns")
    if (
        current.volume_id != expected_volume
        or current.file_id != expected_file
        or current.birthtime_ns != expected_birth
        or current.size != expected_size
        or current.mtime_ns != expected_mtime
    ):
        raise CurationVerificationSnapshotChanged(
            f"curation source identity changed: {path}"
        )
    return path, current


def _open_regular_file_beneath(root: Path, path: Path) -> int:
    """Open ``path`` through no-following directory descriptors.

    This closes the ancestor-symlink race between the lexical/lstat checks and
    the actual read.  The returned descriptor is owned by the caller.
    """

    relative = path.relative_to(root)
    if not relative.parts:
        raise CurationVerificationSnapshotChanged("curation source path is the root")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # ``O_NONBLOCK`` prevents a final-component swap to a FIFO from hanging a
    # supposedly bounded verification before the cooperative deadline can be
    # observed.  The descriptor is still required to be a regular, single-link
    # file immediately after opening.
    common_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_NONBLOCK", 0)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(
            root,
            common_flags | os.O_DIRECTORY,
        )
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                common_flags | os.O_DIRECTORY,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1],
            common_flags,
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(file_fd)
            raise OSError(errno.ELOOP, "curation source is not a regular single-link file")
        os.close(directory_fd)
        directory_fd = None
        return file_fd
    except OSError as exc:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise CurationVerificationSnapshotChanged(
                f"curation source path contains a symlink or non-directory: {path}"
            ) from exc
        if exc.errno in {errno.ENOENT, errno.ESTALE}:
            raise CurationVerificationSnapshotChanged(
                f"curation source disappeared: {path}"
            ) from exc
        raise CurationVerificationUnavailable(
            f"curation source cannot be opened: {path}",
            reason_code="io_unavailable",
        ) from exc


def _read_stable_payload(
    path: Path,
    snapshot: Any,
    *,
    root: Path,
    work: _WorkBudgetState,
    destination: BinaryIO,
    metrics: _VerificationMetrics,
) -> str:
    """Stream one file into a temporary reference and return its digest.

    The destination is temporary storage, never a corpus path.  Keeping the
    reference outside process memory lets every later member compare against
    the keeper without retaining a second copy of the file.
    """

    from neocortex.foundation.hash_compat import sha256
    descriptor = _open_regular_file_beneath(root, path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not stat_matches_snapshot(snapshot, before)
        ):
            raise CurationVerificationSnapshotChanged(f"curation source identity changed: {path}")
        hasher = sha256.sha256_128()
        remaining = snapshot.size
        while remaining:
            work.before_read()
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    f"curation source cannot be read: {path}",
                    reason_code="io_unavailable",
                ) from exc
            if not chunk:
                raise CurationVerificationSnapshotChanged(f"curation source ended early: {path}")
            hasher.update(chunk)
            work.record_bytes(len(chunk))
            try:
                written = destination.write(chunk)
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage cannot be written",
                    reason_code="temporary_storage_unavailable",
                ) from exc
            if written != len(chunk):
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage wrote a short chunk",
                    reason_code="temporary_storage_unavailable",
                )
            work.record_temporary_bytes(written)
            metrics.observe_chunk(len(chunk))
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat_matches_snapshot(snapshot, after)
        ):
            raise CurationVerificationSnapshotChanged(
                f"curation source identity changed: {path}"
            )
        try:
            destination.flush()
        except OSError as exc:
            raise CurationVerificationUnavailable(
                "temporary curation verification storage cannot be flushed",
                reason_code="temporary_storage_unavailable",
            ) from exc
        work.before_read()
        return hasher.digest().hex()
    finally:
        os.close(descriptor)


def _compare_stable_file(
    path: Path,
    snapshot: Any,
    *,
    root: Path,
    reference: BinaryIO,
    work: _WorkBudgetState,
    metrics: _VerificationMetrics,
) -> tuple[bool, str]:
    """Hash and compare one source file against a temporary keeper stream."""

    from neocortex.foundation.hash_compat import sha256
    descriptor = _open_regular_file_beneath(root, path)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not stat_matches_snapshot(snapshot, before)
        ):
            raise CurationVerificationSnapshotChanged(f"curation source identity changed: {path}")
        hasher = sha256.sha256_128()
        equal = True
        remaining = snapshot.size
        reference.seek(0)
        while remaining:
            work.before_read()
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    f"curation source cannot be read: {path}",
                    reason_code="io_unavailable",
                ) from exc
            if not chunk:
                raise CurationVerificationSnapshotChanged(f"curation source ended early: {path}")
            work.record_bytes(len(chunk))
            try:
                reference_chunk = reference.read(len(chunk))
            except OSError as exc:
                raise CurationVerificationUnavailable(
                    "temporary curation verification storage cannot be read",
                    reason_code="temporary_storage_unavailable",
                ) from exc
            hasher.update(chunk)
            if chunk != reference_chunk:
                equal = False
            metrics.observe_chunk(len(chunk), len(reference_chunk))
            remaining -= len(chunk)
        if reference.read(1):
            equal = False
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or not stat_matches_snapshot(snapshot, after)
        ):
            raise CurationVerificationSnapshotChanged(
                f"curation source identity changed: {path}"
            )
        work.before_read()
        return equal, hasher.digest().hex()
    finally:
        os.close(descriptor)


def _duplicate_verification(
    item: CurationItem,
    *,
    root: Path,
    work: _WorkBudgetState,
    metrics: _VerificationMetrics,
    inventory_connection: sqlite3.Connection | None = None,
    page: CurationPlanPage | None = None,
) -> CurationVerificationItem:
    """Verify one duplicate group from a page or its published owner.

    A complete evidence list is retained as a compatibility path for callers
    that already hold a bounded page.  Once an authoritative inventory
    connection is supplied, however, the list is never consulted: the keeper
    and every redundant member are recovered from the fenced owner in bounded
    batches, which prevents the 64-member presentation cap from becoming a
    verification cap.
    """

    evidence = item.evidence
    raw_mode = evidence.get("verification_mode")
    mode: VerificationMode | None = (
        cast(VerificationMode, raw_mode)
        if isinstance(raw_mode, str) and raw_mode in VALID_VERIFICATION_MODES
        else None
    )
    members = evidence.get("members")
    truncated = bool(evidence.get("members_truncated"))
    authoritative = inventory_connection is not None and page is not None
    if not authoritative and (not isinstance(members, list) or truncated):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "not_verified",
            "evidence_truncated",
            0,
            0,
        )
    if not authoritative and (not members or len(members) > MAX_VERIFICATION_FILES):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "not_verified",
            "member_count_out_of_bounds",
            0,
            0,
        )
    if not authoritative:
        expected_member_count = evidence.get("member_count")
        if (
            isinstance(expected_member_count, bool)
            or not isinstance(expected_member_count, int)
                or expected_member_count != len(cast(list[object], members))
        ):
            return CurationVerificationItem(
                item.item_id,
                item.kind,
                item.source_path,
                mode,
                None,
                "not_verified",
                "member_count_changed",
                0,
                0,
            )
    checked = 0
    verified = 0
    try:
        reference: CurationDuplicateGroupReference | None = None
        redundant_members: Iterator[FileSnapshot]
        if authoritative:
            assert inventory_connection is not None and page is not None
            reference, keep_snapshot, redundant_members = _authoritative_member_rows(
                inventory_connection,
                item,
                page=page,
            )
            keep_path = Path(keep_snapshot.path)
            if reference.member_count > MAX_VERIFICATION_FILES:
                return CurationVerificationItem(
                    item.item_id,
                    item.kind,
                    item.source_path,
                    mode,
                    None,
                    "not_verified",
                    "member_count_out_of_bounds",
                    0,
                    0,
                )
            expected_keep_digest = reference.full_fingerprint
        else:
            assert isinstance(members, list)
            typed_members: list[dict[str, object]] = []
            for raw_member in members:
                if not isinstance(raw_member, dict):
                    raise CurationVerificationSnapshotChanged(
                        f"duplicate item {item.item_id} has malformed member evidence"
                    )
                typed_members.append(raw_member)
            keep = next(
                (member for member in typed_members if member.get("role") == "keep"),
                None,
            )
            if keep is None:
                return CurationVerificationItem(
                    item.item_id,
                    item.kind,
                    item.source_path,
                    mode,
                    None,
                    "not_verified",
                    "keep_member_missing",
                    0,
                    0,
                )
            keep_path, keep_snapshot = _snapshot_for_member(keep, root=root)
            expected_keep_digest = str(evidence.get("full_fingerprint"))

            def _compat_redundant_members() -> Iterator[FileSnapshot]:
                for member in typed_members:
                    if member is keep:
                        continue
                    if member.get("role") != "redundant":
                        raise CurationVerificationSnapshotChanged(
                            f"duplicate item {item.item_id} member role is unsupported"
                        )
                    _path, snapshot = _snapshot_for_member(member, root=root)
                    yield snapshot

            redundant_members = _compat_redundant_members()
        work.reserve_file(keep_snapshot.size)
        checked += 1
        with tempfile.TemporaryFile(mode="w+b") as keeper_stream:
            keep_digest = _read_stable_payload(
                keep_path,
                keep_snapshot,
                root=root,
                work=work,
                destination=keeper_stream,
                metrics=metrics,
            )
            if keep_digest != expected_keep_digest:
                raise CurationVerificationSnapshotChanged(
                    f"curation source content changed: {keep_path}"
                )
            verified += 1
            for snapshot in redundant_members:
                path = Path(snapshot.path)
                work.reserve_file(snapshot.size)
                checked += 1
                metrics.keeper_replays += 1
                equal, digest = _compare_stable_file(
                    path,
                    snapshot,
                    root=root,
                    reference=keeper_stream,
                    work=work,
                    metrics=metrics,
                )
                if digest != keep_digest or not equal:
                    raise CurationVerificationSnapshotChanged(
                        f"curation duplicate content changed: {path}"
                    )
                verified += 1
    except CurationVerificationError as exc:
        status: VerificationStatus = (
            "source_changed" if isinstance(exc, CurationVerificationSnapshotChanged) else "not_verified"
        )
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            status,
            (
                "source_changed"
                if status == "source_changed"
                else getattr(exc, "reason_code", "verification_unavailable")
            ),
            checked,
            verified,
        )
    except (FileChangedError, OSError, ValueError):
        return CurationVerificationItem(
            item.item_id,
            item.kind,
            item.source_path,
            mode,
            None,
            "source_changed",
            "source_changed",
            checked,
            verified,
        )
    return CurationVerificationItem(
        item.item_id,
        item.kind,
        item.source_path,
        mode,
        "full_hash",
        "verified",
        "exact_content_verified",
        checked,
        verified,
    )


def _unprocessed_item(item: CurationItem, reason: WorkStopReason) -> CurationVerificationItem:
    raw_mode = item.evidence.get("verification_mode")
    mode: VerificationMode | None = (
        cast(VerificationMode, raw_mode)
        if isinstance(raw_mode, str) and raw_mode in VALID_VERIFICATION_MODES
        else None
    )
    return CurationVerificationItem(
        item.item_id,
        item.kind,
        item.source_path,
        mode,
        None,
        "not_verified",
        reason,
        0,
        0,
    )


def verify_curation_page(
    page: CurationPlanPage,
    *,
    item_ids: tuple[str, ...] | None = None,
    max_items: int = MAX_VERIFICATION_ITEMS,
    max_files: int = MAX_VERIFICATION_FILES,
    max_bytes: int = MAX_VERIFICATION_BYTES,
    budget: CurationWorkBudget | KnowledgeReadBudget | None = None,
    state_directory: str | Path | None = None,
    inventory_database: str | Path | None = None,
) -> CurationVerificationResult:
    """Verify duplicate candidates from one already-published plan page.

    ``state_directory``/``inventory_database`` are optional for compatibility
    with callers that only verify a page whose evidence is complete.  They are
    required to recover a truncated preview sample, and when supplied the
    verifier always uses the fenced inventory owner for duplicate membership,
    even if the page also contains a client-provided member list.
    """

    if not isinstance(page, CurationPlanPage):
        raise TypeError("page must be a CurationPlanPage")
    if not 1 <= max_items <= MAX_VERIFICATION_ITEMS:
        raise ValueError("max_items is outside the verification bound")
    if not 1 <= max_files <= MAX_VERIFICATION_FILES:
        raise ValueError("max_files is outside the verification bound")
    if not 1 <= max_bytes <= MAX_VERIFICATION_BYTES:
        raise ValueError("max_bytes is outside the verification bound")
    knowledge_budget: KnowledgeReadBudget | None = None
    if budget is not None:
        if isinstance(budget, KnowledgeReadBudget):
            knowledge_budget = budget
            # The curation-specific counters remain bounded by their historical
            # defaults; KnowledgeReadBudget supplies the shared row/temporary
            # byte/deadline/cancellation guard.
            effective_budget = CurationWorkBudget()
        elif isinstance(budget, CurationWorkBudget):
            effective_budget = budget
        else:
            raise TypeError("budget must be a CurationWorkBudget or KnowledgeReadBudget")
    else:
        effective_budget = CurationWorkBudget()
    if state_directory is not None and inventory_database is not None:
        raise ValueError("state_directory and inventory_database are mutually exclusive")
    authoritative_database: Path | None = None
    if state_directory is not None:
        authoritative_database = Path(state_directory)
        if not authoritative_database.is_absolute():
            raise ValueError("state_directory must be absolute")
        authoritative_database = authoritative_database / "dedup.sqlite3"
    elif inventory_database is not None:
        authoritative_database = Path(inventory_database)
        if not authoritative_database.is_absolute():
            raise ValueError("inventory_database must be absolute")
    root = _bounded_root(page.root)
    selected = page.items
    if item_ids is not None:
        if not item_ids:
            raise ValueError("item_ids cannot be empty")
        if len(item_ids) > max_items or len(set(item_ids)) != len(item_ids):
            raise ValueError("item_ids are outside the verification bound")
        by_id = {item.item_id: item for item in page.items}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            raise CurationVerificationSnapshotChanged("curation item is not in the published page")
        selected = tuple(by_id[item_id] for item_id in item_ids)
    elif len(selected) > max_items:
        raise CurationVerificationUnavailable("curation verification page exceeds its item bound")
    work = _WorkBudgetState(
        effective_budget,
        max_items=max_items,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    work.knowledge_budget = knowledge_budget
    metrics = _VerificationMetrics()
    results: list[CurationVerificationItem] = []
    inventory_context: AbstractContextManager[Any]
    if authoritative_database is None:
        inventory_context = nullcontext(None)
    else:
        inventory_context = _authoritative_inventory_session(authoritative_database)
    with inventory_context as inventory_connection:
        if inventory_connection is not None:
            _validate_authoritative_inventory(inventory_connection, page=page)
        for index, item in enumerate(selected):
            try:
                work.start_item()
            except _WorkBudgetStop as exc:
                stop_reason = cast(WorkStopReason, exc.reason_code)
                results.extend(_unprocessed_item(pending, stop_reason) for pending in selected[index:])
                break
            if item.kind != "duplicate_group":
                results.append(
                    CurationVerificationItem(
                        item.item_id,
                        item.kind,
                        item.source_path,
                        None,
                        None,
                        "not_applicable",
                        "not_duplicate_kind",
                        0,
                        0,
                    )
                )
                continue
            result = _duplicate_verification(
                item,
                root=root,
                work=work,
                metrics=metrics,
                inventory_connection=inventory_connection,
                page=(page if inventory_connection is not None else None),
            )
            results.append(result)
            if result.reason in {"budget_exhausted", "cancelled", "deadline_exceeded"}:
                stop_reason = cast(WorkStopReason, result.reason)
                results.extend(
                    _unprocessed_item(pending, stop_reason) for pending in selected[index + 1 :]
                )
                break
    verified_count = sum(item.status == "verified" for item in results)
    failed_count = sum(item.status == "source_changed" for item in results)
    # Non-duplicate entries (empty files and organization proposals) are
    # intentionally outside bytewise duplicate verification; they must not
    # downgrade a page whose applicable duplicate groups were all verified.
    skipped_count = sum(item.status == "not_verified" for item in results)
    files_checked = work.files_checked
    bytes_checked = work.bytes_checked
    status: Literal["complete", "partial", "snapshot_changed"] = (
        "snapshot_changed"
        if failed_count
        else "partial"
        if skipped_count or page.coverage != "complete" or page.next_cursor is not None
        else "complete"
    )
    coverage: Literal["complete", "partial"] = "complete" if status == "complete" else "partial"
    return CurationVerificationResult(
        page.plan_digest,
        page.snapshot_id,
        coverage,
        status,
        page.items_total,
        verified_count,
        failed_count,
        skipped_count,
        files_checked,
        bytes_checked,
        tuple(results),
        page.source_heads,
        {
            "files": files_checked,
            "bytes": bytes_checked,
            "chunks": metrics.chunks,
            "peak_buffer": metrics.peak_buffer,
            "keeper_replays": metrics.keeper_replays,
        },
    )


__all__ = (
    "CURATION_VERIFICATION_SCHEMA_VERSION",
    "MAX_VERIFICATION_BYTES",
    "MAX_VERIFICATION_FILES",
    "MAX_VERIFICATION_ITEMS",
    "CurationDuplicateGroupReference",
    "CurationVerificationError",
    "CurationVerificationItem",
    "CurationVerificationResult",
    "CurationVerificationSnapshotChanged",
    "CurationVerificationUnavailable",
    "CurationWorkBudget",
    "verify_curation_page",
)
