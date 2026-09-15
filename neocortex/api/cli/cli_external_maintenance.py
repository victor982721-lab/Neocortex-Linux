"""CLI leaf for the explicit, read-only external maintenance diagnostic."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text

_MAX_RECORDS = 100
_MAX_TEXT = 800


def _bounded_payload(result: object, *, root: Path, category: str) -> dict[str, object]:
    """Return a closed, bounded JSON envelope for one diagnostic result."""

    to_dict = getattr(result, "to_dict", None)
    raw = to_dict() if callable(to_dict) else result
    if isinstance(raw, Mapping):
        payload = {str(key): value for key, value in raw.items()}
    else:
        payload = {"result": raw}
    records = payload.get("records")
    if isinstance(records, list):
        payload["records_returned"] = min(len(records), _MAX_RECORDS)
        payload["records_truncated"] = len(records) > _MAX_RECORDS
        payload["records"] = records[:_MAX_RECORDS]
    payload.setdefault("schema", "neocortex.external-maintenance/v1")
    payload["operation"] = "external-maintenance"
    payload["root"] = str(root)
    payload["category"] = category
    payload["read_only"] = True
    payload["diagnostic_only"] = True
    payload["candidates"] = 0
    payload["applied"] = 0
    return payload


def run_external_maintenance(args) -> int:
    """Execute only the external owner's bounded metadata plan."""

    root = getattr(args, "external_root", None)
    category = getattr(args, "external_category", None)
    try:
        from neocortex.runtime.external_maintenance import ExternalMaintenanceManager

        if not isinstance(root, (Path, str)) or not isinstance(category, str):
            raise ValueError("external root and category are required")
        manager = ExternalMaintenanceManager(
            root,
            category,
            max_entries=args.external_max_entries,
            max_depth=args.external_max_depth,
            max_bytes=args.external_max_bytes,
        )
        result = manager.plan()
        payload = _bounded_payload(result, root=Path(root), category=category)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        payload = {
            "schema": "neocortex.external-maintenance/v1",
            "operation": "external-maintenance",
            "root": None if not isinstance(root, (Path, str)) else str(root),
            "category": category if isinstance(category, str) else None,
            "read_only": True,
            "diagnostic_only": True,
            "status": "blocked",
            "candidates": 0,
            "applied": 0,
            "reason": {
                "code": type(exc).__name__,
                "message": sanitize_untrusted_text(exc, limit=_MAX_TEXT, single_line=False),
            },
        }
        if bool(getattr(args, "external_json", False) or getattr(args, "json_output", False)):
            print(
                json.dumps(
                    sanitize_untrusted_payload(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(
                "ERROR external-maintenance: "
                + sanitize_untrusted_text(exc, limit=_MAX_TEXT, single_line=False),
                file=sys.stderr,
            )
        return 2

    json_output = bool(
        getattr(args, "external_json", False) or getattr(args, "json_output", False)
    )
    safe = sanitize_untrusted_payload(payload)
    if json_output:
        print(json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        counts = payload.get("counts")
        scanned = counts.get("scanned", 0) if isinstance(counts, Mapping) else 0
        print(
            "EXTERNAL-MAINTENANCE "
            f"category={category} status={payload.get('status', 'unknown')} "
            f"scanned={scanned} "
            f"truncated={payload.get('truncated', False)}"
        )
    return 0


__all__ = ["run_external_maintenance"]
