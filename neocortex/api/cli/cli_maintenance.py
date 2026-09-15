"""Direct CLI adapter for registered scratch and historical audit maintenance.

This module deliberately has no import-time dependency on either runtime
owner.  The command is a small control-plane leaf: registered scopes resolve
below the configured state directory, while the historical scope requires an
explicit audit root.  Both owners are asked for a bounded plan and the
result is rendered without starting the Framework route graph.

The historical scope is intentionally not a compatibility alias for scratch.
It dispatches only to ``HistoricalAuditManager``.  If a historical artifact
cannot prove adoption, that owner must return a blocked/kept result; this
adapter never falls back to ``rm``, KIO, or the registered-scratch owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

__all__ = [
    "HISTORICAL_AUDIT_SCOPE",
    "MAINTENANCE_SCHEMA",
    "MAINTENANCE_SCOPES",
    "maintenance_root",
    "run_maintenance",
]


MAINTENANCE_SCHEMA = "neocortex.maintenance/v1"
HISTORICAL_AUDIT_SCOPE = "historical-temp"
MAINTENANCE_SCOPES = ("owned-temp", "audit-work", HISTORICAL_AUDIT_SCOPE)
_BUCKETS = ("planned", "applied", "kept", "blocked", "failed", "recovery_required")
_HISTORICAL_BUCKETS = (
    "unknown",
    "active",
    "blocked",
    "preservation",
    "applied",
    "recovery_required",
)
_MAX_RECORDS = 100
_MAX_TEXT = 800
_MAX_COUNT = 1_000_000
_MAX_BYTES = 16 * 1024 * 1024 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_HISTORICAL_RECEIPT_SCHEMA = "neocortex.historical-audit-receipt/v1"
_MAX_RECEIPT_EFFECTS = 100_000


def _maintenance_scope(args: argparse.Namespace) -> str | None:
    """Return the scope selected by the shared flat ``--scope`` action."""

    value = getattr(args, "scope", None)
    if isinstance(value, str):
        return value
    value = getattr(args, "knowledge_scope", None)
    return value if isinstance(value, str) else None


def maintenance_root(args: argparse.Namespace, scope: str) -> Path:
    """Resolve the only directory this command is allowed to inspect.

    No ``resolve()``, ``expanduser()`` or user-provided path is applied here:
    the runtime owner is responsible for refusing a symlinked/non-private
    root.  Keeping this expression deliberately literal also makes it clear
    that ``--root`` (the corpus root) cannot redirect registered maintenance.
    Historical maintenance is different: its explicit audit root is returned
    literally and is never replaced by ``--root``, the corpus root, or ``/tmp``.
    """

    if scope == HISTORICAL_AUDIT_SCOPE:
        audit_root = getattr(args, "maintenance_audit_root", None)
        if isinstance(audit_root, Path):
            root = audit_root
        elif isinstance(audit_root, str):
            root = Path(audit_root)
        else:
            raise ValueError("historical maintenance requires --maintenance-audit-root")
        if not root.is_absolute():
            raise ValueError("--maintenance-audit-root must be absolute")
        return root

    state_directory = getattr(args, "state_directory", None)
    if isinstance(state_directory, Path):
        state = state_directory
    elif isinstance(state_directory, str):
        state = Path(state_directory)
    else:
        raise ValueError("state directory is required")
    return state / "scratch" / scope


def _historical_scope(scope: str) -> bool:
    return scope == HISTORICAL_AUDIT_SCOPE


def _historical_manager(
    root: Path,
    args: argparse.Namespace,
    *,
    apply: bool,
) -> object:
    """Construct the historical owner without importing it for other scopes.

    The runtime owner is the only component allowed to classify or adopt
    historical artifacts.  ``create_root=False`` is passed when the owner
    exposes the scratch-compatible constructor flag; a read-only historical
    query must never create an audit root.  The small signature adaptation is
    useful for test doubles and for owners that make this default implicit,
    without introducing a second implementation in the CLI.
    """

    from neocortex.runtime.historical_audit import HistoricalAuditManager

    parameters: Mapping[str, object]
    try:
        import inspect

        parameters = inspect.signature(HistoricalAuditManager).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, object] = {}
    if "owner" in parameters:
        kwargs["owner"] = "neocortex-framework"
    if "create_root" in parameters:
        kwargs["create_root"] = False
    if "read_only" in parameters:
        kwargs["read_only"] = not apply
    for name, value in (
        ("corpus_root", getattr(args, "root", None)),
        ("state_root", getattr(args, "state_directory", None)),
        ("state_directory", getattr(args, "state_directory", None)),
        ("max_entries", getattr(args, "maintenance_max_entries", None)),
        ("max_depth", getattr(args, "maintenance_max_depth", None)),
        ("max_bytes", getattr(args, "maintenance_max_bytes", None)),
    ):
        if name not in parameters:
            continue
        if isinstance(value, (Path, str)):
            kwargs[name] = Path(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            kwargs[name] = value
    manager_type: Any = HistoricalAuditManager
    return manager_type(root, **kwargs)


def _value(value: object, *, depth: int = 0) -> object:
    """Convert an owner result to a bounded JSON-compatible value.

    The scratch owner is local code, but its metadata can contain producer
    supplied paths and notes.  Keep those values behind the same terminal
    sanitization boundary as the other CLI read adapters.
    """

    if depth > 8:
        return "[contenido omitido por límite]"
    if isinstance(value, Enum):
        return _value(value.value, depth=depth + 1)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        try:
            value = asdict(value)
        except Exception:
            return sanitize_untrusted_text(value, limit=_MAX_TEXT, single_line=False)
    if isinstance(value, Mapping):
        # Preserve only a bounded first page.  The canonical plan fields are
        # selected separately below; this helper is mainly for record details.
        return {
            str(key): _value(item, depth=depth + 1)
            for index, (key, item) in enumerate(value.items())
            if index < _MAX_RECORDS
        }
    if isinstance(value, (tuple, list)):
        return [_value(item, depth=depth + 1) for item in value[:_MAX_RECORDS]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return sanitize_untrusted_text(value, limit=_MAX_TEXT, single_line=False)


def _owner_value(owner_result: object, name: str) -> object:
    """Read one plan field from a dataclass, mapping, or owner-compatible object."""

    if isinstance(owner_result, Mapping):
        return owner_result.get(name)
    return getattr(owner_result, name, None)


def _count(owner_result: object, name: str) -> int:
    """Extract one bounded category count across compatible plan shapes."""

    value = _owner_value(owner_result, name)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, min(value, _MAX_COUNT))
    if isinstance(value, (tuple, list, set, frozenset)):
        return min(len(value), _MAX_COUNT)
    for alias in (f"{name}_count", f"{name}_records", f"{name}_items"):
        value = _owner_value(owner_result, alias)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(0, min(value, _MAX_COUNT))
        if isinstance(value, (tuple, list, set, frozenset)):
            return min(len(value), _MAX_COUNT)
    nested = _owner_value(owner_result, "counts")
    if isinstance(nested, Mapping):
        value = nested.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, min(value, _MAX_COUNT))
    return 0


def _bytes(owner_result: object, name: str) -> int:
    """Extract one category byte count without treating it as disk savings."""

    aliases = (
        f"bytes_{name}",
        f"{name}_bytes",
        f"total_{name}_bytes",
    )
    for alias in aliases:
        value = _owner_value(owner_result, alias)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, min(value, _MAX_BYTES))
    nested = _owner_value(owner_result, "bytes")
    if isinstance(nested, Mapping):
        value = nested.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, min(value, _MAX_BYTES))
    return 0


def _raw_records(owner_result: object) -> Sequence[object]:
    for name in ("records", "items", "entries"):
        value = _owner_value(owner_result, name)
        if isinstance(value, (tuple, list)):
            return value[:_MAX_RECORDS]
    return ()


def _all_bounded_records(owner_result: object) -> Sequence[object]:
    """Return enough records for effect validation, not just first-page output."""

    for name in ("records", "items", "entries"):
        value = _owner_value(owner_result, name)
        if isinstance(value, (tuple, list)):
            return value[:_MAX_RECEIPT_EFFECTS]
    return ()


def _historical_metric_count(owner_result: object, name: str) -> int:
    """Extract one historical category without collapsing safety states."""

    aliases = {
        "unknown": ("unknown", "unknown_records", "unknown_items", "unmanaged"),
        "active": ("active", "active_records", "active_items"),
        "blocked": ("blocked", "blocked_records", "blocked_items"),
        "preservation": (
            "preservation",
            "preservation_records",
            "preserved",
            "preserved_records",
            "kept",
        ),
        "applied": ("applied", "applied_records", "applied_items"),
        "recovery_required": (
            "recovery_required",
            "recovery_required_records",
            "recovery",
        ),
    }[name]
    for alias in aliases:
        value = _owner_value(owner_result, alias)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(0, min(value, _MAX_COUNT))
        if isinstance(value, (tuple, list, set, frozenset)):
            return min(len(value), _MAX_COUNT)
    nested = _owner_value(owner_result, "counts")
    if isinstance(nested, Mapping):
        for alias in aliases:
            value = nested.get(alias)
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, min(value, _MAX_COUNT))
            if isinstance(value, (tuple, list, set, frozenset)):
                return min(len(value), _MAX_COUNT)

    # A record-level status is useful for owners that deliberately keep the
    # top-level plan compact.  This remains classification only; no path is
    # inferred and no record is made eligible by the CLI.
    status_aliases = {
        "unknown": {"unknown", "unmanaged", "unverified", "unadopted"},
        "active": {"active", "running", "in-use"},
        "blocked": {"blocked", "unsafe", "adoption-required"},
        "preservation": {"preservation", "preserved", "kept", "retain"},
        "applied": {"applied", "retired", "removed"},
        "recovery_required": {"recovery_required", "recovery-required"},
    }
    total = 0
    for record in _raw_records(owner_result):
        status = _owner_value(record, "status")
        if isinstance(status, Enum):
            status = status.value
        if isinstance(status, str) and status.casefold() in status_aliases[name]:
            total += 1
    return min(total, _MAX_COUNT)


def _historical_metric_bytes(owner_result: object, name: str) -> int:
    aliases = {
        "unknown": ("unknown_bytes", "bytes_unknown"),
        "active": ("active_bytes", "bytes_active"),
        "blocked": ("blocked_bytes", "bytes_blocked"),
        "preservation": (
            "preservation_bytes",
            "preserved_bytes",
            "kept_bytes",
            "bytes_preservation",
        ),
        "applied": ("applied_bytes", "bytes_applied"),
        "recovery_required": (
            "recovery_required_bytes",
            "recovery_bytes",
            "bytes_recovery_required",
        ),
    }[name]
    for alias in aliases:
        value = _owner_value(owner_result, alias)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, min(value, _MAX_BYTES))
    nested = _owner_value(owner_result, "bytes")
    if isinstance(nested, Mapping):
        for alias in (name, *aliases):
            value = nested.get(alias)
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, min(value, _MAX_BYTES))
    return 0


def _record_payload(record: object) -> object:
    """Expose only useful bounded record metadata, never arbitrary objects."""

    converted = _value(record)
    if isinstance(converted, Mapping):
        # Paths/status/identity are the stable audit details.  If an owner
        # supplies a newer record shape, retain its already-sanitized fields
        # but cap both its breadth and value sizes.
        return {
            str(key): item
            for index, (key, item) in enumerate(converted.items())
            if index < 32
        }
    return converted


def _read_applied_receipt(value: object) -> Mapping[str, object] | None:
    """Read and verify one owner receipt without following an untrusted link.

    The historical owner writes these receipts atomically before/after an
    effect.  The CLI presentation guard must not turn a plan claim into an
    applied result when the effect receipt is absent, malformed, or replaced.
    This is deliberately a small bounded read; it never removes or repairs a
    receipt.
    """

    if isinstance(value, Path):
        path = value
    elif isinstance(value, str):
        path = Path(value)
    else:
        return None
    if not path.is_absolute() or len(str(path).encode("utf-8")) > 4096:
        return None
    try:
        before = path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or bool(before.st_mode & 0o077)
        or before.st_size > _MAX_RECEIPT_BYTES
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or bool(opened.st_mode & 0o077)
            or opened.st_size > _MAX_RECEIPT_BYTES
        ):
            return None
        data = bytearray()
        while len(data) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_RECEIPT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_RECEIPT_BYTES:
            return None
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    if (
        parsed.get("schema") != _HISTORICAL_RECEIPT_SCHEMA
        or parsed.get("state") != "applied"
        or parsed.get("postcondition") != "entry_absent"
    ):
        return None
    digest = parsed.get("receipt_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    body = dict(parsed)
    body.pop("receipt_digest", None)
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return parsed if digest == expected else None


def _normalized_claim(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return tuple(_normalized_claim(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _receipt_claim_key(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        _normalized_claim(payload.get(name))
        for name in (
            "root",
            "root_identity",
            "path",
            "path_identity",
            "manifest_path",
            "manifest_identity",
            "manifest_digest",
            "adoption_id",
            "adoption_digest",
            "owner",
        )
    )


def _record_claim_key(record: object, root: object) -> tuple[object, ...]:
    return tuple(
        _normalized_claim(value)
        for value in (
            root,
            _owner_value(record, "root_identity"),
            _owner_value(record, "path"),
            _owner_value(record, "path_identity"),
            _owner_value(record, "manifest_path"),
            _owner_value(record, "manifest_identity"),
            _owner_value(record, "manifest_digest"),
            _owner_value(record, "adoption_id"),
            _owner_value(record, "adoption_digest"),
            _owner_value(record, "owner"),
        )
    )


def _has_verified_receipts(
    owner_result: object,
    effect_count: int,
    authorization_plan: object | None,
) -> bool:
    """Require applied receipts matching distinct authorized records."""

    if effect_count <= 0 or effect_count > _MAX_RECEIPT_EFFECTS:
        return False
    receipts = _owner_value(owner_result, "receipts")
    if not isinstance(receipts, (tuple, list)) or len(receipts) < effect_count:
        return False
    if authorization_plan is None:
        return False
    root = _owner_value(authorization_plan, "root")
    if root is None:
        return False
    plan_records = _all_bounded_records(authorization_plan)
    expected: set[tuple[object, ...]] = set()
    for record in plan_records:
        status = _owner_value(record, "status")
        if isinstance(status, Enum):
            status = status.value
        if not (
            _owner_value(record, "adoptable") is True
            or _owner_value(record, "eligible") is True
            or status in {"adoptable", "approved"}
        ):
            continue
        expected.add(_record_claim_key(record, root))
    if len(expected) < effect_count:
        return False
    for item in receipts[:effect_count]:
        parsed = _read_applied_receipt(item)
        if parsed is None:
            return False
        claim = _receipt_claim_key(parsed)
        if claim not in expected:
            return False
        expected.remove(claim)
    return True

def _plan_payload(
    owner_result: object,
    *,
    root: Path,
    scope: str,
    apply: bool,
) -> dict[str, object]:
    """Build the closed maintenance envelope from one owner plan."""

    counts = {name: _count(owner_result, name) for name in _BUCKETS}
    byte_counts = {name: _bytes(owner_result, name) for name in _BUCKETS}
    owner_status = _owner_value(owner_result, "status")
    if isinstance(owner_status, Enum):
        owner_status = owner_status.value
    if isinstance(owner_status, str):
        owner_status = owner_status.rsplit(".", 1)[-1].casefold()
    else:
        owner_status = None
    if apply:
        status = owner_status if owner_status in _BUCKETS else "applied"
        if counts["recovery_required"]:
            status = "recovery_required"
        elif counts["failed"]:
            status = "failed"
        elif counts["blocked"]:
            status = "blocked"
        elif counts["kept"] and not counts["applied"]:
            status = "kept"
    else:
        status = owner_status if owner_status in _BUCKETS else "planned"
        if counts["recovery_required"]:
            status = "recovery_required"
        elif counts["failed"]:
            status = "failed"
        elif counts["blocked"]:
            status = "blocked"
        elif counts["kept"] and not counts["planned"]:
            status = "kept"

    # The top-level counters are intentionally duplicated in ``counts`` for
    # straightforward shell consumers.  They are small integers, not a
    # claim that those bytes are reclaimable disk space.
    payload: dict[str, object] = {
        "schema": MAINTENANCE_SCHEMA,
        "operation": "maintenance",
        "owner": "neocortex-framework",
        "scope": scope,
        "root": str(root),
        "mode": "apply" if apply else "plan",
        "read_only": not apply,
        "status": status,
        "exit_code": 0,
        "error": None,
        "planned": counts["planned"],
        "applied": counts["applied"],
        "kept": counts["kept"],
        "blocked": counts["blocked"],
        "failed": counts["failed"],
        "recovery_required": counts["recovery_required"],
        **{f"{name}_bytes": byte_counts[name] for name in _BUCKETS},
        "counts": counts,
        "bytes": byte_counts,
        "records": [_record_payload(record) for record in _raw_records(owner_result)],
        "root_exists": root.exists(),
    }
    if _historical_scope(scope):
        # Keep the established maintenance envelope while making the
        # historical boundary machine-readable.  These flags describe the
        # selected owner; they do not claim that any bytes are reclaimable.
        payload["audit"] = "historical-audit"
        payload["historical_audit"] = True
        historical_counts = {
            name: _historical_metric_count(owner_result, name)
            for name in _HISTORICAL_BUCKETS
        }
        historical_bytes = {
            name: _historical_metric_bytes(owner_result, name)
            for name in _HISTORICAL_BUCKETS
        }
        if historical_counts["recovery_required"]:
            payload["status"] = "recovery_required"
        elif historical_counts["blocked"]:
            payload["status"] = "blocked"
        elif historical_counts["active"]:
            payload["status"] = "active"
        elif historical_counts["preservation"] and not historical_counts["applied"]:
            payload["status"] = "kept"
        elif historical_counts["unknown"] and not historical_counts["applied"]:
            payload["status"] = "unknown"
        elif historical_counts["applied"]:
            payload["status"] = "applied"
        for name in (
            "observed_bytes",
            "proposed_bytes",
        "observed_apparent_bytes",
        "observed_allocated_bytes",
        "active_bytes",
        "proposed_apparent_bytes",
            "proposed_allocated_bytes",
            "applied_apparent_bytes",
            "applied_allocated_bytes",
        ):
            value = _owner_value(owner_result, name)
            if isinstance(value, int) and not isinstance(value, bool):
                historical_bytes[name] = max(0, min(value, _MAX_BYTES))
                payload[name] = historical_bytes[name]
        payload["historical_counts"] = historical_counts
        payload["historical_bytes"] = historical_bytes
        for name in _HISTORICAL_BUCKETS:
            payload[name] = historical_counts[name]
            payload[f"{name}_bytes"] = historical_bytes[name]
        payload["preserved"] = historical_counts["preservation"]
        payload["preserved_bytes"] = historical_bytes["preservation"]
        # Expose the bounded observation envelope without requiring shell
        # consumers to know the owner's dataclass shape.  These values are
        # copied only when the owner supplies them; they never widen the scan
        # or turn a path into an authorization claim.
        for name in ("scanned", "adoptable", "truncated"):
            value = _owner_value(owner_result, name)
            if isinstance(value, bool):
                payload[name] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                payload[name] = max(0, min(value, _MAX_COUNT))
        for name in ("root_blocked", "root_identity"):
            value = _owner_value(owner_result, name)
            if value is not None:
                payload[name] = _value(value)
        unmanaged = _owner_value(owner_result, "unmanaged")
        if isinstance(unmanaged, (tuple, list)):
            payload["unmanaged"] = [_value(item) for item in unmanaged[:_MAX_RECORDS]]
            payload["unmanaged_returned"] = min(len(unmanaged), _MAX_RECORDS)
            payload["unmanaged_truncated"] = len(unmanaged) > _MAX_RECORDS
        receipts = _owner_value(owner_result, "receipts")
        if isinstance(receipts, (tuple, list)):
            payload["receipts"] = [_value(item) for item in receipts[:_MAX_RECORDS]]
        all_records = _owner_value(owner_result, "records")
        if isinstance(all_records, (tuple, list)):
            payload["records_returned"] = min(len(all_records), _MAX_RECORDS)
            payload["records_truncated"] = len(all_records) > _MAX_RECORDS

            def record_observed_bytes(record: object) -> int:
                value = _owner_value(record, "observed_bytes")
                return value if isinstance(value, int) and not isinstance(value, bool) else 0

            ranked_records = sorted(
                all_records,
                key=lambda record: (
                    -record_observed_bytes(record),
                    str(_owner_value(record, "path") or ""),
                ),
            )
            payload["largest_records"] = [
                _record_payload(record) for record in ranked_records[:20]
            ]
        for name in ("status_counts", "reason_summary"):
            value = _owner_value(owner_result, name)
            if isinstance(value, Mapping):
                payload[name] = _value(value)
            elif isinstance(value, (tuple, list)):
                payload[name] = _value(value[:64])
        limits = _owner_value(owner_result, "limits")
        if isinstance(limits, Mapping):
            payload["limits"] = _value(limits)
        for name in ("adoption_id", "adoption_digest", "digest"):
            value = _owner_value(owner_result, name)
            if value is not None:
                payload[name] = _value(value)
    # Let an owner expose a bounded explanatory field without allowing it to
    # replace the closed envelope.  ``reason`` is useful for empty/unavailable
    # plans and is omitted when absent.
    reason = _owner_value(owner_result, "reason")
    if reason is not None:
        payload["reason"] = _value(reason)
    return payload


def _historical_result_is_unverified(
    owner_result: object,
    *,
    authorization_plan: object | None = None,
) -> bool:
    """Return whether an apply result explicitly lacks adoption proof.

    The historical owner remains the authority for effect eligibility.  This
    narrow adapter guard only prevents a permissive/legacy owner result from
    being presented as an applied cleanup when it says that adoption is not
    verified.  It never performs an effect itself.
    """

    for name in ("adoption_verified", "verified", "adopted"):
        value = _owner_value(owner_result, name)
        if value is False:
            return True
    status = _owner_value(owner_result, "status")
    if isinstance(status, Enum):
        status = status.value
    if isinstance(status, str):
        normalized = status.rsplit(".", 1)[-1].casefold().replace("_", "-")
        if normalized in {
            "unverified",
            "unknown",
            "unadopted",
            "adoption-required",
        }:
            return True
    else:
        normalized = ""

    # An apply result that claims an effect must carry the exact adoption
    # identity and digest returned by the historical owner.  The owner still
    # performs the equality/revalidation check; this is only a presentation
    # guard against turning a legacy result into an apparent success.
    effect_count = _historical_metric_count(owner_result, "applied")
    records = _raw_records(owner_result)

    def top_level_has_claim(value: object) -> bool:
        adoption_id = _owner_value(value, "adoption_id")
        if not (
            isinstance(adoption_id, (str, int))
            and not isinstance(adoption_id, bool)
            and str(adoption_id).strip()
        ):
            return False
        for digest_name in (
            "adoption_digest",
            "digest",
            "manifest_digest",
            "claim_digest",
        ):
            digest = _owner_value(value, digest_name)
            if isinstance(digest, str) and digest.strip():
                return True
        return False

    has_top_level_claim = top_level_has_claim(owner_result) or (
        authorization_plan is not None and top_level_has_claim(authorization_plan)
    )
    def record_has_claim(record: object, *, require_adoption_id: bool) -> bool:
        digest_present = False
        for digest_name in (
            "adoption_digest",
            "digest",
            "manifest_digest",
            "claim_digest",
        ):
            digest = _owner_value(record, digest_name)
            if isinstance(digest, str) and digest.strip():
                digest_present = True
                break
        if not digest_present:
            return False
        if not require_adoption_id:
            return True
        adoption_id_value = _owner_value(record, "adoption_id")
        return (
            isinstance(adoption_id_value, (str, int))
            and not isinstance(adoption_id_value, bool)
            and bool(str(adoption_id_value).strip())
        )

    has_record_claims = bool(records) and all(
        record_has_claim(record, require_adoption_id=True) for record in records
    )
    plan_records = (
        _all_bounded_records(authorization_plan)
        if authorization_plan is not None
        else ()
    )
    # The currently shipped historical owner uses its authenticated manifest
    # digest as the per-entry adoption claim and does not repeat removed
    # records in the apply result.  Preserve that proof across the apply
    # boundary.  A newer owner may additionally provide adoption_id; either
    # exact-claim shape is accepted here while the owner remains authoritative.
    def plan_record_has_claim(record: object) -> bool:
        adoptable = _owner_value(record, "adoptable")
        eligible = _owner_value(record, "eligible")
        status_value = _owner_value(record, "status")
        if isinstance(status_value, Enum):
            status_value = status_value.value
        explicitly_adoptable = (
            adoptable is True
            or eligible is True
            or status_value in {"adoptable", "approved"}
        )
        return explicitly_adoptable and record_has_claim(
            record,
            require_adoption_id=False,
        )

    has_plan_claims = bool(plan_records) and all(
        plan_record_has_claim(record) for record in plan_records
    )
    unknown = _historical_metric_count(owner_result, "unknown")
    if normalized in {"applied", "retired", "removed", "planned", "candidate"} and (
        effect_count or records or unknown
    ):
        # A preview claim is necessary to identify what was authorized, but it
        # is not evidence that an effect completed.  Applied output must also
        # carry bounded, self-consistent receipts from the owner.
        has_receipts = _has_verified_receipts(
            owner_result,
            effect_count,
            authorization_plan,
        )
        return not (
            has_receipts
            and (has_top_level_claim or has_record_claims or has_plan_claims)
        )
    return False


def _historical_blocked_payload(
    payload: dict[str, object],
    owner_result: object,
    *,
    authorization_plan: object | None = None,
) -> dict[str, object]:
    """Mark an unverified historical apply as blocked without mutating data."""

    if not _historical_result_is_unverified(
        owner_result,
        authorization_plan=authorization_plan,
    ):
        return payload
    # Preserve any owner-supplied blocked accounting; only add one bounded
    # marker when the owner supplied no explicit category.  No path is
    # fabricated and no record is turned into a deletion candidate here.
    applied_count = _historical_metric_count(owner_result, "applied")
    if applied_count:
        # An effect without a verifiable adoption claim is indeterminate, not
        # a successful application.  Do not emit an envelope that says both
        # ``applied`` and ``blocked``; surface recovery instead.
        payload["applied"] = 0
        payload["applied_bytes"] = 0
        payload["applied_apparent_bytes"] = 0
        payload["applied_allocated_bytes"] = 0
        counts = payload.get("counts")
        if isinstance(counts, dict):
            counts["applied"] = 0
            counts["recovery_required"] = max(
                1,
                counts.get("recovery_required", 0)
                if isinstance(counts.get("recovery_required"), int)
                else 0,
            )
        payload["recovery_required"] = 1
        payload["recovery_required_bytes"] = 0
        historical_counts = payload.get("historical_counts")
        if isinstance(historical_counts, dict):
            historical_counts["applied"] = 0
            historical_counts["recovery_required"] = max(
                1,
                historical_counts.get("recovery_required", 0)
                if isinstance(historical_counts.get("recovery_required"), int)
                else 0,
            )
        raw_historical_bytes = payload.get("historical_bytes")
        historical_bytes_payload: dict[str, object] = (
            {str(key): value for key, value in raw_historical_bytes.items()}
            if isinstance(raw_historical_bytes, Mapping)
            else {}
        )
        payload["historical_bytes"] = historical_bytes_payload
        historical_bytes_payload["applied"] = 0
        historical_bytes_payload["applied_bytes"] = 0
        historical_bytes_payload["recovery_required"] = 0
        historical_bytes_payload["recovery_required_bytes"] = 0
        payload["applied_bytes"] = 0
        payload["status"] = "recovery_required"
        payload["exit_code"] = 2
        payload["reason"] = "historical effect lacks verifiable adoption evidence"
        return payload
    counts = payload.get("counts")
    explicit_failure = False
    if isinstance(counts, dict):
        for name in ("blocked", "failed", "recovery_required"):
            value = counts.get(name)
            if isinstance(value, int) and value > 0:
                explicit_failure = True
                break
    if isinstance(counts, dict) and not explicit_failure:
        counts["blocked"] = 1
        payload["blocked"] = 1
        historical_counts = payload.get("historical_counts")
        if isinstance(historical_counts, dict):
            historical_counts["blocked"] = max(
                1,
                int(historical_counts.get("blocked", 0))
                if isinstance(historical_counts.get("blocked"), int)
                else 0,
            )
            payload["blocked"] = historical_counts["blocked"]
        payload["blocked_bytes"] = 0
    payload["status"] = "blocked"
    payload["exit_code"] = 2
    payload["reason"] = "historical artifact adoption is not verified"
    return payload


def _call_historical_plan(manager: object) -> object:
    """Run the historical owner read interface without a mutation fallback.

    Newer owners expose the two explicit read phases ``audit()`` and
    ``plan()``; the plan may accept the audit result.  A compatibility owner
    may expose only ``plan()``.  Both paths remain read-only and bounded by
    the owner.  No discovery implementation belongs in this adapter.
    """

    audit_method = getattr(manager, "audit", None)
    audit_result: object | None = None
    if callable(audit_method):
        audit_result = audit_method()
    plan_method = getattr(manager, "plan", None)
    if not callable(plan_method):
        if audit_result is not None:
            return audit_result
        raise TypeError("historical maintenance owner does not expose plan()")
    if audit_result is None:
        return plan_method()

    try:
        import inspect

        parameters = tuple(inspect.signature(plan_method).parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        )
        required = tuple(
            parameter
            for parameter in positional
            if parameter.default is parameter.empty
        )
    except (TypeError, ValueError):
        required = ()
    return plan_method(audit_result) if required else plan_method()


def _call_historical_apply(manager: object, plan: object) -> object:
    """Delegate historical application exclusively to its owner."""

    apply_method = getattr(manager, "apply", None)
    if not callable(apply_method):
        raise TypeError("historical maintenance owner does not expose apply()")
    # The owner revalidates the plan's adoption identity/digest at the effect
    # boundary.  Passing the complete plan, rather than deriving paths here,
    # keeps that exact-claim gate in one place.
    return apply_method(plan)


def _call_registered_plan(manager: object) -> object:
    plan_method = getattr(manager, "plan", None)
    if not callable(plan_method):
        raise TypeError("maintenance owner does not expose plan()")
    return plan_method()


def _call_registered_apply(manager: object, plan: object) -> object:
    apply_method = getattr(manager, "apply", None)
    if not callable(apply_method):
        raise TypeError("maintenance owner does not expose apply()")
    return apply_method(plan)


def _error_payload(
    *,
    scope: str | None,
    root: Path | None,
    apply: bool,
    code: str,
    message: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": MAINTENANCE_SCHEMA,
        "operation": "maintenance",
        "scope": scope,
        "root": None if root is None else str(root),
        "mode": "apply" if apply else "plan",
        "read_only": not apply,
        "status": "failed",
        "exit_code": 2,
        "planned": 0,
        "applied": 0,
        "kept": 0,
        "blocked": 0,
        "failed": 1,
        "recovery_required": 0,
        **{f"{name}_bytes": 0 for name in _BUCKETS},
        "counts": {name: (1 if name == "failed" else 0) for name in _BUCKETS},
        "bytes": dict.fromkeys(_BUCKETS, 0),
        "records": [],
        "error": {
            "code": sanitize_untrusted_text(code, limit=128),
            "message": sanitize_untrusted_text(message, limit=_MAX_TEXT, single_line=False),
        },
    }
    if scope == HISTORICAL_AUDIT_SCOPE:
        payload["audit"] = "historical-audit"
        payload["historical_audit"] = True
        historical_counts = {
            name: (1 if name == "blocked" else 0) for name in _HISTORICAL_BUCKETS
        }
        payload["historical_counts"] = historical_counts
        payload["historical_bytes"] = dict.fromkeys(_HISTORICAL_BUCKETS, 0)
        for name in _HISTORICAL_BUCKETS:
            payload[name] = historical_counts[name]
            payload[f"{name}_bytes"] = 0
    return payload


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    safe_payload = sanitize_untrusted_payload(payload)
    if json_output:
        print(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return

    line = (
        "MAINTENANCE "
        f"scope={payload.get('scope', '-')} "
        f"status={payload.get('status', 'failed')} "
        f"mode={payload.get('mode', 'plan')} "
        f"planned={payload.get('planned', 0)} "
        f"applied={payload.get('applied', 0)} "
        f"kept={payload.get('kept', 0)} "
        f"blocked={payload.get('blocked', 0)} "
        f"failed={payload.get('failed', 0)}"
    )
    if payload.get("scope") == HISTORICAL_AUDIT_SCOPE:
        line += (
            f" unknown={payload.get('unknown', 0)}"
            f" active={payload.get('active', 0)}"
            f" preservation={payload.get('preservation', 0)}"
            f" recovery_required={payload.get('recovery_required', 0)}"
        )
    print(line)
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            "ERROR maintenance "
            f"code={error.get('code', 'maintenance_failed')}: "
            + sanitize_untrusted_text(error.get("message", "unknown error"), limit=_MAX_TEXT),
            file=sys.stderr,
        )


def run_maintenance(args: argparse.Namespace) -> int:
    """Plan or apply maintenance for one registered or historical scope.

    Importing the manager and invoking it are both lazy.  In particular,
    missing roots are observed with creation disabled so a read-only query
    remains a true no-creation operation.  Historical paths are *never*
    passed to ``ScratchManager`` and no filesystem fallback exists here.
    """

    scope = _maintenance_scope(args)
    apply = bool(getattr(args, "apply", False))
    json_output = bool(
        getattr(args, "maintenance_json", False) or getattr(args, "json_output", False)
    )
    root: Path | None = None
    try:
        if scope not in MAINTENANCE_SCOPES:
            raise ValueError(
                "maintenance scope must be one of " + ", ".join(MAINTENANCE_SCOPES)
            )
        root = maintenance_root(args, scope)
        # This is intentionally the sole runtime-owner import in the command.
        # It must not pull in route engines, SQLite owners, model loaders,
        # KIO, or a shell/file-removal fallback.
        if _historical_scope(scope):
            manager = _historical_manager(root, args, apply=apply)
        else:
            from neocortex.runtime.scratch import ScratchManager

            manager = ScratchManager(
                root,
                owner="neocortex-framework",
                create_root=False,
            )
        try:
            plan = (
                _call_historical_plan(manager)
                if _historical_scope(scope)
                else _call_registered_plan(manager)
            )
        except FileNotFoundError:
            # A missing registered-scratch root is an empty, safe query.  Do
            # not turn it into a service failure and, importantly, do not
            # create it just to report zero candidates.
            payload = _plan_payload(
                {},
                root=root,
                scope=scope,
                apply=apply,
            )
            _emit(payload, json_output=json_output)
            return 0
        if apply:
            result = (
                _call_historical_apply(manager, plan)
                if _historical_scope(scope)
                else _call_registered_apply(manager, plan)
            )
        else:
            result = plan
        payload = _plan_payload(result, root=root, scope=scope, apply=apply)
        if _historical_scope(scope) and apply:
            payload = _historical_blocked_payload(
                payload,
                result,
                authorization_plan=plan,
            )
        # A successful plan is a safe read even when it reports protected or
        # failed-retained records.  Application failures are surfaced as code
        # 2 so callers cannot mistake an incomplete effect for success.
        exit_code = 0
        if apply and payload["status"] in {
            "active",
            "failed",
            "blocked",
            "recovery_required",
        }:
            exit_code = 2
            payload["exit_code"] = exit_code
        _emit(payload, json_output=json_output)
        return exit_code
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        payload = _error_payload(
            scope=scope,
            root=root,
            apply=apply,
            code=type(exc).__name__,
            message=exc,
        )
        _emit(payload, json_output=json_output)
        return 2
