"""Bounded, read-only curation previews over published local state.

The preview composes the existing inventory duplicate plans and technical
organization plans. It never initializes, migrates, or writes a database and
it never authorizes a filesystem action. A bounded sample fingerprint makes
the exact preview repeatable while persisted counters communicate coverage
outside the sample.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from neocortex.deduplication.persistence.connections import connect as connect_inventory
from neocortex.deduplication.persistence.validation import validate_inventory_schema
from neocortex.documents.document_catalog import connect_document_catalog
from neocortex.documents.document_catalog_schema import document_catalog_schema_contract
from neocortex.persistence.sqlite_schema_contract import validate_sqlite_schema_contract

CURATION_PREVIEW_SCHEMA_VERSION = 1
_MAX_GROUP_MEMBERS_IN_EVIDENCE = 64
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
    root: str
    scan_id: int
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
            "empty_files": self.empty_files,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_members": self.duplicate_members,
            "inventory_files": self.inventory_files,
            "items": [item.to_dict() for item in self.items],
            "items_total": self.items_total,
            "items_truncated": self.items_truncated,
            "organization_plans": self.organization_plans,
            "preview_fingerprint": self.preview_fingerprint,
            "preview_limit": self.preview_limit,
            "reclaimable_bytes": self.reclaimable_bytes,
            "root": self.root,
            "scan_id": self.scan_id,
            "schema_version": self.schema_version,
        }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_existing_database(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise CurationStateError(f"{label} state database does not exist: {path}")


def _snapshot_signature(path: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    """Return metadata for one database and any adjacent WAL/SHM owners."""

    paths = [path]
    paths.extend(
        candidate
        for suffix in ("-journal", "-wal", "-shm")
        if (candidate := path.with_name(path.name + suffix)).exists()
    )
    signature: list[tuple[str, int, int, int, int]] = []
    for source in paths:
        try:
            stat = source.stat()
        except FileNotFoundError:
            return ()
        signature.append(
            (source.name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        )
    return tuple(signature)


@contextmanager
def _readonly_sqlite_snapshot(path: Path, *, label: str) -> Iterator[Path]:
    """Copy one stable SQLite owner before opening it, without touching the owner."""

    temporary = tempfile.TemporaryDirectory(
        prefix="neocortex-curation-",
        dir="/tmp",
    )
    try:
        target_directory = Path(temporary.name)
        snapshot: Path | None = None
        for _attempt in range(2):
            before = _snapshot_signature(path)
            if not before:
                raise CurationStateError(f"{label} state changed or disappeared during snapshot")
            if any(name.endswith("-journal") for name, *_rest in before):
                raise CurationStateError(f"{label} SQLite journal is active")
            for name, _device, _inode, _size, _mtime in before:
                shutil.copyfile(path.with_name(name), target_directory / name)
            after = _snapshot_signature(path)
            if before == after:
                snapshot = target_directory / path.name
                break
            for child in target_directory.iterdir():
                child.unlink()
        if snapshot is None:
            raise CurationStateError(f"{label} state changed during read-only snapshot")
        yield snapshot
    finally:
        temporary.cleanup()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = {str(row[1]) for row in rows}
    if not columns:
        raise CurationStateError(f"curation state lacks required table: {table}")
    return columns


def _latest_scan(connection: sqlite3.Connection) -> tuple[int, str, int]:
    _table_columns(connection, "scans")
    row = connection.execute(
        """SELECT scan_id,root,files_seen FROM scans
        WHERE status='complete' AND errors=0 AND completed_ns IS NOT NULL
        ORDER BY scan_id DESC LIMIT 1"""
    ).fetchone()
    if row is None or row[1] is None or row[2] is None:
        raise CurationStateError("curation state has no complete inventory scan")
    return int(row[0]), str(row[1]), int(row[2])


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


def _duplicate_summary(
    connection: sqlite3.Connection,
    scan_id: int,
) -> tuple[int, int, int]:
    row = connection.execute(
        """SELECT COUNT(*),COALESCE(SUM(redundant_count),0),
        COALESCE(SUM(reclaimable_bytes),0)
        FROM planned_duplicate_groups WHERE scan_id=?""",
        (scan_id,),
    ).fetchone()
    if row is None:
        return 0, 0, 0
    return int(row[0]), int(row[1]), int(row[2])


def _duplicate_items(
    connection: sqlite3.Connection,
    scan_id: int,
    limit: int,
) -> list[CurationItem]:
    rows = connection.execute(
        """SELECT group_id,size,keep_path,redundant_count,reclaimable_bytes,
        full_fingerprint FROM planned_duplicate_groups WHERE scan_id=?
        ORDER BY reclaimable_bytes DESC,keep_path,group_id LIMIT ?""",
        (scan_id, limit),
    ).fetchall()
    items: list[CurationItem] = []
    for row in rows:
        members = connection.execute(
            """SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,birthtime_ns
            FROM planned_duplicate_members WHERE group_id=?
            ORDER BY member_order LIMIT ?""",
            (int(row[0]), _MAX_GROUP_MEMBERS_IN_EVIDENCE + 1),
        ).fetchall()
        member_payload = []
        for member in members[:_MAX_GROUP_MEMBERS_IN_EVIDENCE]:
            identity = _identity_payload(member[3], member[4], member[7])
            member_payload.append(
                {
                    "identity": identity,
                    "member_order": int(member[0]),
                    "mtime_ns": int(member[6]),
                    "path": str(member[2]),
                    "role": str(member[1]),
                    "size": int(member[5]),
                }
            )
        items.append(
            CurationItem(
                item_id=f"duplicate:{scan_id}:{int(row[0])}",
                kind="duplicate_group",
                status="review",
                action="review_duplicate_group",
                source_path=str(row[2]),
                destination_path=None,
                reason="exact_duplicate_content",
                evidence={
                    "full_fingerprint": str(row[5]),
                    "group_id": int(row[0]),
                    "keep_path": str(row[2]),
                    "member_count": int(row[3]) + 1,
                    "members": member_payload,
                    "members_truncated": len(members) > _MAX_GROUP_MEMBERS_IN_EVIDENCE,
                    "reclaimable_bytes": int(row[4]),
                    "size": int(row[1]),
                },
            )
        )
    return items


def _empty_file_summary(connection: sqlite3.Connection, scan_id: int) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM files WHERE scan_id=? AND size=0", (scan_id,)
        ).fetchone()[0]
    )


def _empty_file_items(
    connection: sqlite3.Connection,
    scan_id: int,
    limit: int,
) -> list[CurationItem]:
    rows = connection.execute(
        """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files WHERE scan_id=? AND size=0 ORDER BY path LIMIT ?""",
        (scan_id, limit),
    ).fetchall()
    return [
        CurationItem(
            item_id=f"empty:{scan_id}:{row[0]!s}",
            kind="empty_file",
            status="review",
            action="review_empty_file",
            source_path=str(row[0]),
            destination_path=None,
            reason="empty_file_requires_human_review",
            evidence={
                "identity": _identity_payload(row[1], row[2], row[5]),
                "mtime_ns": int(row[4]),
                "size": int(row[3]),
            },
        )
        for row in rows
    ]


def _organization_summary(connection: sqlite3.Connection) -> int:
    columns = _table_columns(connection, "organization_plans")
    missing = _REQUIRED_CATALOG_PLAN_COLUMNS - columns
    if missing:
        missing_text = ",".join(sorted(missing))
        raise CurationStateError(
            f"organization_plans schema lacks required columns: {missing_text}"
        )
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM organization_plans WHERE status<>'superseded'"
        ).fetchone()[0]
    )


def _organization_items(
    connection: sqlite3.Connection,
    limit: int,
) -> list[CurationItem]:
    rows = connection.execute(
        """SELECT plan_id,catalog_run_id,source_kind,file_key,source_path,
        destination_path,organization_root,volume_id,file_id,size,mtime_ns,
        birthtime_ns,classifier_signature,primary_kind,confidence,status,reason,
        evidence_json FROM organization_plans WHERE status<>'superseded'
        ORDER BY plan_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    items: list[CurationItem] = []
    for row in rows:
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
        items.append(
            CurationItem(
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
        )
    return items


def _preview_fingerprint(
    *,
    scan_id: int,
    root: str,
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
        "empty_files": empty_files,
        "duplicate_groups": duplicate_groups,
        "duplicate_members": duplicate_members,
        "inventory_files": inventory_files,
        "items": [item.to_dict() for item in items],
        "organization_plans": organization_plans,
        "preview_limit": preview_limit,
        "reclaimable_bytes": reclaimable_bytes,
        "root": root,
        "scan_id": scan_id,
        "schema_version": CURATION_PREVIEW_SCHEMA_VERSION,
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_curation_preview(state_directory: Path, *, limit: int) -> CurationPreview:
    """Read published inventory and catalog plans without changing their bytes."""

    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("curation preview limit must be between 1 and 10000")
    inventory_path = state_directory / "dedup.sqlite3"
    catalog_path = state_directory / "document_catalog.sqlite3"
    _require_existing_database(inventory_path, label="dedup")
    _require_existing_database(catalog_path, label="document catalog")

    with _readonly_sqlite_snapshot(inventory_path, label="dedup") as inventory_snapshot:
        with connect_inventory(inventory_snapshot, readonly=True) as inventory:
            validate_inventory_schema(inventory)
            scan_id, root, inventory_files = _latest_scan(inventory)
            duplicate_groups, duplicate_members, reclaimable_bytes = _duplicate_summary(
                inventory, scan_id
            )
            empty_files = _empty_file_summary(inventory, scan_id)
            sampled_items = _duplicate_items(inventory, scan_id, limit)
            with _readonly_sqlite_snapshot(catalog_path, label="document catalog") as catalog_snapshot:
                with connect_document_catalog(catalog_snapshot, readonly=True) as catalog:
                    validate_sqlite_schema_contract(
                        catalog,
                        document_catalog_schema_contract(),
                        label="document catalog",
                        exact=True,
                    )
                    organization_plans = _organization_summary(catalog)
                    if len(sampled_items) < limit:
                        sampled_items.extend(
                            _organization_items(catalog, limit - len(sampled_items))
                        )
            if len(sampled_items) < limit:
                sampled_items.extend(
                    _empty_file_items(inventory, scan_id, limit - len(sampled_items))
                )

    total_items = duplicate_groups + organization_plans + empty_files
    return CurationPreview(
        schema_version=CURATION_PREVIEW_SCHEMA_VERSION,
        root=root,
        scan_id=scan_id,
        inventory_files=inventory_files,
        duplicate_groups=duplicate_groups,
        duplicate_members=duplicate_members,
        reclaimable_bytes=reclaimable_bytes,
        organization_plans=organization_plans,
        empty_files=empty_files,
        preview_limit=limit,
        items_total=total_items,
        items_truncated=total_items > len(sampled_items),
        preview_fingerprint=_preview_fingerprint(
            scan_id=scan_id,
            root=root,
            inventory_files=inventory_files,
            duplicate_groups=duplicate_groups,
            duplicate_members=duplicate_members,
            reclaimable_bytes=reclaimable_bytes,
            organization_plans=organization_plans,
            empty_files=empty_files,
            preview_limit=limit,
            items=sampled_items,
        ),
        items=tuple(sampled_items),
    )


__all__ = [
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CurationItem",
    "CurationPreview",
    "CurationStateError",
    "build_curation_preview",
]
