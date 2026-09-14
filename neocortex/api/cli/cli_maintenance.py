"""Direct CLI adapter for registered, owned scratch maintenance.

This module deliberately has no import-time dependency on the runtime scratch
owner.  The command is a small control-plane leaf: it resolves one fixed
scope below the configured state directory, asks the owner for a bounded plan,
and renders that plan without starting the Framework route graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

__all__ = [
    "MAINTENANCE_SCHEMA",
    "MAINTENANCE_SCOPES",
    "maintenance_root",
    "run_maintenance",
]


MAINTENANCE_SCHEMA = "neocortex.maintenance/v1"
MAINTENANCE_SCOPES = ("owned-temp", "audit-work")
_BUCKETS = ("planned", "applied", "kept", "blocked", "failed", "recovery_required")
_MAX_RECORDS = 100
_MAX_TEXT = 800
_MAX_COUNT = 1_000_000
_MAX_BYTES = 16 * 1024 * 1024 * 1024 * 1024


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
    that ``--root`` (the corpus root) cannot redirect maintenance.
    """

    state_directory = getattr(args, "state_directory", None)
    if isinstance(state_directory, Path):
        state = state_directory
    elif isinstance(state_directory, str):
        state = Path(state_directory)
    else:
        raise ValueError("state directory is required")
    return state / "scratch" / scope


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
        return len(value)
    for alias in (f"{name}_count", f"{name}_records", f"{name}_items"):
        value = _owner_value(owner_result, alias)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(0, min(value, _MAX_COUNT))
        if isinstance(value, (tuple, list, set, frozenset)):
            return len(value)
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
    # Let an owner expose a bounded explanatory field without allowing it to
    # replace the closed envelope.  ``reason`` is useful for empty/unavailable
    # plans and is omitted when absent.
    reason = _owner_value(owner_result, "reason")
    if reason is not None:
        payload["reason"] = _value(reason)
    return payload


def _error_payload(
    *,
    scope: str | None,
    root: Path | None,
    apply: bool,
    code: str,
    message: object,
) -> dict[str, object]:
    return {
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


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    safe_payload = sanitize_untrusted_payload(payload)
    if json_output:
        print(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return

    print(
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
    error = payload.get("error")
    if isinstance(error, Mapping):
        print(
            "ERROR maintenance "
            f"code={error.get('code', 'maintenance_failed')}: "
            + sanitize_untrusted_text(error.get("message", "unknown error"), limit=_MAX_TEXT),
            file=sys.stderr,
        )


def run_maintenance(args: argparse.Namespace) -> int:
    """Plan or apply maintenance for one registered scratch scope.

    Importing the manager and invoking it are both lazy.  In particular,
    missing roots are passed to ``create_root=False`` so a read-only query
    remains a true no-creation operation.
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
        # This is intentionally the sole runtime import in the command.  It
        # must not pull in route engines, SQLite owners, or model loaders.
        from neocortex.runtime.scratch import ScratchManager

        manager = ScratchManager(
            root,
            owner="neocortex-framework",
            create_root=False,
        )
        try:
            plan = manager.plan()
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
        result = manager.apply(plan) if apply else plan
        payload = _plan_payload(result, root=root, scope=scope, apply=apply)
        # A successful plan is a safe read even when it reports protected or
        # failed-retained records.  Application failures are surfaced as code
        # 2 so callers cannot mistake an incomplete effect for success.
        exit_code = 0
        if apply and payload["status"] in {"failed", "blocked", "recovery_required"}:
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
