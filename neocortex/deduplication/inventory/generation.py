"""Content identities for immutable inventory and duplicate-plan heads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable


def _update_value(digest: "hashlib._Hash", value: object) -> None:
    """Hash SQLite values with type and length framing to avoid ambiguity."""

    if value is None:
        digest.update(b"N\0")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = bytes(value)
        digest.update(b"B")
    else:
        encoded = str(value).encode("utf-8", "surrogatepass")
        digest.update(b"T")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _hash_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Iterable[object],
    *,
    domain: bytes,
    work_check: Callable[[], None] | None = None,
) -> bytes:
    if work_check is not None:
        work_check()
    digest = hashlib.sha256(domain)
    count = 0
    for row in connection.execute(query, tuple(parameters)):
        count += 1
        if work_check is not None and count % 128 == 0:
            work_check()
        digest.update(b"R")
        for value in row:
            _update_value(digest, value)
    digest.update(b"C")
    digest.update(count.to_bytes(8, "big"))
    if work_check is not None:
        work_check()
    return digest.digest()


def inventory_content_digest(
    connection: sqlite3.Connection, scan_id: int, *, work_check: Callable[[], None] | None = None,
) -> bytes:
    """Return the canonical identity of one persisted inventory generation."""

    return _hash_rows(
        connection,
        """SELECT path,volume_id,file_id,size,mtime_ns,birthtime_ns
        FROM files WHERE scan_id=? ORDER BY path""",
        (scan_id,),
        domain=b"NEOCORTEX_INVENTORY_CONTENT_V1\0",
        work_check=work_check,
    )


def duplicate_plan_digest(connection: sqlite3.Connection, scan_id: int) -> bytes:
    """Return the canonical identity of all persisted plan proof rows."""

    digest = hashlib.sha256(b"NEOCORTEX_DUPLICATE_PLAN_V1\0")
    groups = connection.execute(
        """SELECT group_id,size,keep_path,redundant_count,reclaimable_bytes,
        full_fingerprint,verification_mode,proof_json
        FROM planned_duplicate_groups WHERE scan_id=?
        ORDER BY size,keep_path,redundant_count,reclaimable_bytes,
        full_fingerprint,verification_mode,proof_json""",
        (scan_id,),
    )
    for group in groups:
        group_id, *group_values = group
        digest.update(b"G")
        for value in group_values:
            _update_value(digest, _stable_proof_json(value))
        members = connection.execute(
            """SELECT member_order,role,path,volume_id,file_id,size,mtime_ns,
            birthtime_ns,proof_json FROM planned_duplicate_members
            WHERE group_id=? ORDER BY member_order""",
            (group_id,),
        )
        for member in members:
            digest.update(b"M")
            for value in member:
                _update_value(digest, _stable_proof_json(value))
    return digest.digest()


def _stable_proof_json(value: object) -> object:
    """Ignore compute-vs-cache provenance when identifying equal plan content."""

    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(decoded, dict):
        return value
    decoded.pop("fingerprint_source", None)
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["duplicate_plan_digest", "inventory_content_digest"]
