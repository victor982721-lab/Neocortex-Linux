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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from neocortex.deduplication.persistence.validation import validate_inventory_schema
from neocortex.documents.document_catalog_schema import document_catalog_schema_contract
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    SQLiteReadSession,
    preferred_sqlite_read_mode,
)
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "empty_files": self.empty_files,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_members": self.duplicate_members,
            "inventory_files": self.inventory_files,
            "items": [item.to_dict() for item in self.items],
            "items_total": self.items_total,
            "items_truncated": self.items_truncated,
            "missing_owners": list(self.missing_owners),
            "organization_plans": self.organization_plans,
            "preview_fingerprint": self.preview_fingerprint,
            "preview_limit": self.preview_limit,
            "reclaimable_bytes": self.reclaimable_bytes,
            "root": self.root,
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
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

    @property
    def items_truncated(self) -> bool:
        return self.next_cursor is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage,
            "cursor": self.cursor,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_members": self.duplicate_members,
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
            "root": self.root,
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@contextmanager
def _readonly_sqlite_connection(
    path: Path,
    *,
    label: str,
) -> Iterator[sqlite3.Connection]:
    """Read one owner through the shared lstat/O_NOFOLLOW/fence kernel."""

    try:
        mode = preferred_sqlite_read_mode(path)
        with SQLiteReadSession(path, mode=mode, timeout_seconds=60.0) as connection:
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
) -> dict[str, object]:
    def _identity_number(value: object) -> int:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return int.from_bytes(bytes(value), "little")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip() == value and value:
            return int(value, 0 if value.lower().startswith("0x") else 10)
        raise ValueError("identity value is not a supported integer representation")

    try:
        volume = _identity_number(volume_id)
        file_number = _identity_number(file_id)
        if isinstance(birthtime_ns, bool) or not isinstance(birthtime_ns, (int, str)):
            raise ValueError("birth-time value is not an integer")
        birthtime = int(birthtime_ns)
    except (TypeError, ValueError) as error:
        raise CurationStateError("curation inventory contains a malformed file identity") from error
    return {
        "birthtime_ns": birthtime,
        "file_id": f"{file_number:x}",
        "volume_id": f"{volume:x}",
    }


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
    summary = connection.execute(
        """SELECT group_count,redundant_files,reclaimable_bytes,completed_ns
        FROM duplicate_plan_summaries WHERE scan_id=?""",
        (scan_id,),
    ).fetchone()
    if summary is None:
        return _DuplicatePlanState(False, 0, 0, 0, None, actual_groups)

    expected_groups = int(summary[0])
    expected_redundant = int(summary[1])
    expected_reclaimable = int(summary[2])
    completed_ns = int(summary[3])
    stored_members = int(
        connection.execute(
            """SELECT COUNT(*) FROM planned_duplicate_members m
            JOIN planned_duplicate_groups g ON g.group_id=m.group_id
            WHERE g.scan_id=?""",
            (scan_id,),
        ).fetchone()[0]
    )
    complete = (
        (actual_groups, actual_redundant, actual_reclaimable)
        == (expected_groups, expected_redundant, expected_reclaimable)
        and stored_members == expected_groups + expected_redundant
    )
    if not complete:
        return _DuplicatePlanState(False, 0, 0, 0, completed_ns, actual_groups)
    return _DuplicatePlanState(
        True,
        expected_groups,
        expected_redundant,
        expected_reclaimable,
        completed_ns,
        0,
    )


def _duplicate_item(
    connection: sqlite3.Connection,
    scan_id: int,
    row: Any,
    digest: Any,
) -> CurationItem:
    group_id = int(row[0])
    redundant_count = int(row[3])
    verification_mode = "legacy_unknown"
    _digest_record(
        digest,
        "duplicate_group",
        {
            "full_fingerprint": str(row[5]),
            "group_id": group_id,
            "keep_path": str(row[2]),
            "reclaimable_bytes": int(row[4]),
            "redundant_count": redundant_count,
            "scan_id": scan_id,
            "size": int(row[1]),
            "verification_mode": verification_mode,
        },
    )
    member_payload: list[dict[str, object]] = []
    member_count = 0
    members = connection.execute(
        """SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM planned_duplicate_members WHERE group_id=?
        ORDER BY member_order""",
        (group_id,),
    )
    for member in members:
        identity = _identity_payload(member[3], member[4], member[7])
        member_record = {
            "identity": identity,
            "member_order": int(member[0]),
            "mtime_ns": int(member[6]),
            "path": str(member[2]),
            "role": str(member[1]),
            "size": int(member[5]),
        }
        _digest_record(
            digest,
            "duplicate_member",
            {"group_id": group_id, **member_record},
        )
        if member_count < _MAX_GROUP_MEMBERS_IN_EVIDENCE:
            member_payload.append(
                member_record
            )
        member_count += 1
    if member_count != redundant_count + 1:
        raise CurationStateError(f"duplicate group {group_id} member count is inconsistent")
    return CurationItem(
        item_id=f"duplicate:{scan_id}:{group_id}",
        kind="duplicate_group",
        status="review",
        action="review_duplicate_group",
        source_path=str(row[2]),
        destination_path=None,
        reason="duplicate_content_candidate",
        evidence={
            "full_fingerprint": str(row[5]),
            "group_id": group_id,
            "keep_path": str(row[2]),
            "member_count": member_count,
            "members": member_payload,
            "members_truncated": member_count > _MAX_GROUP_MEMBERS_IN_EVIDENCE,
            "reclaimable_bytes": int(row[4]),
            "size": int(row[1]),
            "verification_mode": verification_mode,
        },
    )


def _iter_duplicate_items(
    connection: sqlite3.Connection,
    scan_id: int,
    digest: Any,
) -> Iterator[tuple[_SortKey, CurationItem]]:
    rows = connection.execute(
        """SELECT group_id,size,keep_path,redundant_count,reclaimable_bytes,
        full_fingerprint FROM planned_duplicate_groups WHERE scan_id=?
        ORDER BY reclaimable_bytes DESC,keep_path COLLATE BINARY,group_id""",
        (scan_id,),
    )
    for row in rows:
        item = _duplicate_item(connection, scan_id, row, digest)
        yield (
            (0, -int(row[4]), str(row[2]), int(row[0])),
            item,
        )


def _empty_file_summary(connection: sqlite3.Connection, scan_id: int) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id=? AND size=0", (scan_id,)
        ).fetchone()[0]
    )


def _iter_empty_file_items(
    connection: sqlite3.Connection,
    scan_id: int,
    digest: Any,
) -> Iterator[tuple[_SortKey, CurationItem]]:
    rows = connection.execute(
        """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files WHERE scan_id=? AND size=0 ORDER BY path COLLATE BINARY""",
        (scan_id,),
    )
    for row in rows:
        evidence = {
            "identity": _identity_payload(row[1], row[2], row[5]),
            "mtime_ns": int(row[4]),
            "size": int(row[3]),
        }
        item = CurationItem(
            item_id=f"empty:{scan_id}:{row[0]!s}",
            kind="empty_file",
            status="review",
            action="review_empty_file",
            source_path=str(row[0]),
            destination_path=None,
            reason="empty_file_requires_human_review",
            evidence=evidence,
        )
        _digest_record(digest, "empty_file", item.to_dict())
        yield (2, 0, str(row[0]), 0), item


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
    rows = connection.execute(
        """SELECT plan_id,catalog_run_id,source_kind,file_key,source_path,
        destination_path,organization_root,volume_id,file_id,size,mtime_ns,
        birthtime_ns,classifier_signature,primary_kind,confidence,status,reason,
        evidence_json FROM organization_plans
        WHERE catalog_run_id=?
          AND organization_root COLLATE BINARY=? COLLATE BINARY
          AND status<>'superseded'
        ORDER BY plan_id DESC""",
        (scope.catalog_run_id, scope.organization_root),
    )
    for row in rows:
        if _path_is_within_root(row[4], inventory_root):
            yield row


def _organization_summary(
    connection: sqlite3.Connection,
    *,
    inventory_root: str,
    scope: _OrganizationPlanScope | None,
) -> int:
    return sum(
        1
        for _row in _iter_organization_rows(
            connection,
            inventory_root=inventory_root,
            scope=scope,
        )
    )


def _iter_organization_items(
    connection: sqlite3.Connection,
    digest: Any,
    *,
    inventory_root: str,
    scope: _OrganizationPlanScope | None,
) -> Iterator[tuple[_SortKey, CurationItem]]:
    for row in _iter_organization_rows(
        connection,
        inventory_root=inventory_root,
        scope=scope,
    ):
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
        item = CurationItem(
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
                    "identity": _identity_payload(row[7], row[8], row[11]),
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
        _digest_record(digest, "organization_plan", item.to_dict())
        yield (1, -int(row[0]), "", 0), item


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
    payload = _canonical_json(
        {"contract": _SNAPSHOT_CONTRACT, "plan_digest": plan_digest}
    ).encode("utf-8")
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
                "redundant_members": duplicate_plan.redundant_members,
                "verification_mode": "legacy_unknown",
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
    if cursor_state is not None and not cursor_found[0]:
        if key == cursor_state.key:
            cursor_found[0] = True
        return
    if len(page_items) < limit:
        page_items.append(item)
        page_keys.append(key)
    else:
        has_more[0] = True


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
) -> CurationPlanPage:
    organization_scope = (
        None
        if catalog is None
        else _organization_plan_scope(catalog, inventory_root=head.root)
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
    coverage = (
        "complete"
        if not missing_owners and duplicate_plan.complete
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
        ),
    )

    page_items: list[CurationItem] = []
    page_keys: list[_SortKey] = []
    cursor_found = [cursor_state is None]
    has_more = [False]
    items_total = 0

    if duplicate_plan.complete:
        for key, item in _iter_duplicate_items(inventory, head.scan_id, digest):
            items_total += 1
            _consume_page_item(
                key,
                item,
                cursor_state=cursor_state,
                page_items=page_items,
                page_keys=page_keys,
                limit=limit,
                cursor_found=cursor_found,
                has_more=has_more,
            )
    if catalog is not None:
        for key, item in _iter_organization_items(
            catalog,
            digest,
            inventory_root=head.root,
            scope=organization_scope,
        ):
            items_total += 1
            _consume_page_item(
                key,
                item,
                cursor_state=cursor_state,
                page_items=page_items,
                page_keys=page_keys,
                limit=limit,
                cursor_found=cursor_found,
                has_more=has_more,
            )
    for key, item in _iter_empty_file_items(inventory, head.scan_id, digest):
        items_total += 1
        _consume_page_item(
            key,
            item,
            cursor_state=cursor_state,
            page_items=page_items,
            page_keys=page_keys,
            limit=limit,
            cursor_found=cursor_found,
            has_more=has_more,
        )

    expected_total = duplicate_plan.groups + organization_plans + empty_files
    if items_total != expected_total:
        raise CurationStateError("curation plan item count changed while reading its snapshot")
    plan_digest = "sha256:" + digest.hexdigest()
    snapshot_id = _semantic_snapshot_id(plan_digest)
    if cursor_state is not None and cursor_state.snapshot_id != snapshot_id:
        raise CurationStateError("curation cursor snapshot changed")
    if cursor_state is not None and not cursor_found[0]:
        raise CurationStateError("curation cursor key is not present in its snapshot")
    next_cursor = (
        _encode_cursor(snapshot_id, page_keys[-1])
        if has_more[0] and page_keys
        else None
    )
    return CurationPlanPage(
        schema_version=CURATION_PLAN_PAGE_SCHEMA_VERSION,
        coverage=coverage,
        missing_owners=missing_owners,
        root=head.root,
        scan_id=head.scan_id,
        inventory_files=head.inventory_files,
        duplicate_groups=duplicate_plan.groups,
        duplicate_members=duplicate_plan.redundant_members,
        reclaimable_bytes=duplicate_plan.reclaimable_bytes,
        organization_plans=organization_plans,
        empty_files=empty_files,
        limit=limit,
        cursor=cursor,
        next_cursor=next_cursor,
        snapshot_id=snapshot_id,
        plan_digest=plan_digest,
        items_total=items_total,
        items=tuple(page_items),
    )


def _unavailable_plan_page(
    *,
    missing_owners: tuple[str, ...],
    limit: int,
    cursor: str | None,
    cursor_state: _CursorState | None,
) -> CurationPlanPage:
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
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_curation_plan_page(
    state_directory: Path,
    limit: int,
    cursor: str | None = None,
) -> CurationPlanPage:
    """Read one stable keyset page without mutating owners or corpus content."""

    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("curation page limit must be between 1 and 10000")
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

    with _readonly_sqlite_connection(inventory_path, label="dedup") as inventory:
        validate_inventory_schema(inventory)
        head = _published_inventory_head(inventory)
        duplicate_plan = _duplicate_plan_state(inventory, head.scan_id)
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
            )
        with _readonly_sqlite_connection(
            catalog_path,
            label="document catalog",
        ) as catalog:
            validate_sqlite_schema_contract(
                catalog,
                document_catalog_schema_contract(),
                label="document catalog",
                exact=True,
            )
            return _build_plan_page_from_connections(
                inventory=inventory,
                catalog=catalog,
                missing_owners=missing_owners,
                head=head,
                duplicate_plan=duplicate_plan,
                limit=limit,
                cursor=cursor,
                cursor_state=cursor_state,
            )


def build_curation_preview(state_directory: Path, *, limit: int) -> CurationPreview:
    """Compatibility facade over the first immutable curation-plan page."""

    page = build_curation_plan_page(state_directory, limit, None)
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
        ),
        items=tuple(sampled_items),
    )


__all__ = [
    "CURATION_PLAN_PAGE_SCHEMA_VERSION",
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CurationItem",
    "CurationPlanPage",
    "CurationPreview",
    "CurationStateError",
    "build_curation_plan_page",
    "build_curation_preview",
]
