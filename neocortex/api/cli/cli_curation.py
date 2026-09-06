"""Read-only curation preview command adapter."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text
from neocortex.curation import CurationStateError, build_curation_preview


_CLI_TEXT_LIMIT = 800
_CLI_EVIDENCE_LIMIT = 16_384
_CLI_ITEMS_LIMIT = 10_000
_SANITIZATION_TRUNCATION = "[contenido omitido por límite]"
_SANITIZATION_TRUNCATION_KEY = "__neocortex_sanitization_truncated__"
_ITEM_FIELDS = (
    "action",
    "destination_path",
    "evidence",
    "item_id",
    "kind",
    "reason",
    "source_path",
    "status",
)


def _safe_text(value: object, *, limit: int = _CLI_TEXT_LIMIT) -> str:
    """Bound one state-derived value before it reaches a terminal line."""

    try:
        return sanitize_untrusted_text(value, limit=limit, single_line=True)
    except Exception:  # pragma: no cover - only hostile producer objects reach this path
        return _SANITIZATION_TRUNCATION


def _safe_optional_text(value: object, *, limit: int = _CLI_TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    return _safe_text(value, limit=limit)


def _safe_payload(value: object) -> object:
    """Apply the shared bounded JSON boundary without letting rendering fail."""

    try:
        return sanitize_untrusted_payload(value)
    except Exception:  # pragma: no cover - only malformed producer objects reach this path
        return _SANITIZATION_TRUNCATION


def _json_text(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return json.dumps(_SANITIZATION_TRUNCATION, ensure_ascii=False)


def _safe_evidence(value: object) -> object:
    """Keep evidence JSON-safe and prevent one item from dominating CLI output."""

    sanitized = _safe_payload(value)
    if len(_json_text(sanitized).encode("utf-8")) <= _CLI_EVIDENCE_LIMIT:
        return sanitized
    return {_SANITIZATION_TRUNCATION_KEY: _SANITIZATION_TRUNCATION}


def _mapping_value(value: object, key: str) -> object:
    if not isinstance(value, Mapping):
        return None
    try:
        return value.get(key)
    except Exception:  # pragma: no cover - defensive boundary for malformed mappings
        return None


def _safe_item_payload(value: object) -> dict[str, object]:
    """Copy only the stable item fields, sanitizing paths, reasons and evidence."""

    result: dict[str, object] = {}
    for field in _ITEM_FIELDS:
        raw = _mapping_value(value, field)
        if field == "evidence":
            result[field] = _safe_evidence(raw)
        elif field == "destination_path":
            result[field] = _safe_optional_text(raw)
        else:
            result[field] = _safe_text(raw)
    return result


def _safe_preview_payload(preview: Any) -> dict[str, object]:
    """Prepare both output modes from one bounded, renderer-safe payload."""

    try:
        raw = preview.to_dict()
    except Exception:  # pragma: no cover - preview is a trusted dataclass in production
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}

    summary = {key: value for key, value in raw.items() if key != "items"}
    sanitized_summary = _safe_payload(summary)
    payload = dict(sanitized_summary) if isinstance(sanitized_summary, Mapping) else {}

    payload["kind"] = "curation-preview"
    if "coverage" in raw:
        payload["coverage"] = _safe_text(raw["coverage"])
    if "root" in raw:
        payload["root"] = _safe_optional_text(raw["root"])
    if "preview_fingerprint" in raw:
        payload["preview_fingerprint"] = _safe_text(raw["preview_fingerprint"])

    owners = raw.get("missing_owners")
    if isinstance(owners, (list, tuple)):
        payload["missing_owners"] = [_safe_text(owner) for owner in owners[:64]]

    items = raw.get("items")
    if isinstance(items, (list, tuple)):
        payload["items"] = [_safe_item_payload(item) for item in items[:_CLI_ITEMS_LIMIT]]
    else:
        payload["items"] = []
    return payload


def _display_value(value: object) -> str:
    return _safe_text("None" if value is None else value)


def _display_missing_owners(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    return ",".join(_safe_text(owner) for owner in value) or "-"


def _root_was_explicit(args: argparse.Namespace) -> bool:
    return "root" in getattr(args, "_explicit_options", ())


def run_curation_preview(args: argparse.Namespace) -> int:
    """Show bounded duplicate, organization, and empty-file proposals."""

    if _root_was_explicit(args):
        if not getattr(args, "curation_json", False):
            print("ERROR curation-preview cannot be combined with --root")
            return 2
        return _print_curation_error(
            args,
            CurationStateError(
                "curation-preview cannot be combined with --root", code="invalid_arguments"
            ),
        )

    try:
        preview = build_curation_preview(
            args.state_directory,
            limit=args.curation_preview,
        )
    except (CurationStateError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        return _print_curation_error(args, exc)

    payload = _safe_preview_payload(preview)
    if args.curation_json:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if payload.get("coverage") == "complete" else 2

    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    print(
        f"CURATION_PREVIEW scan={_display_value(payload.get('scan_id'))} "
        f"root={_display_value(payload.get('root'))} "
        f"coverage={_display_value(payload.get('coverage'))} "
        f"missing={_display_missing_owners(payload.get('missing_owners'))} "
        f"inventory_files={_display_value(payload.get('inventory_files'))} "
        f"duplicate_groups={_display_value(payload.get('duplicate_groups'))} "
        f"duplicate_members={_display_value(payload.get('duplicate_members'))} "
        f"reclaimable_bytes={_display_value(payload.get('reclaimable_bytes'))} "
        f"organization_plans={_display_value(payload.get('organization_plans'))} "
        f"empty_files={_display_value(payload.get('empty_files'))} "
        f"items={len(items)}/{_display_value(payload.get('items_total'))} "
        f"truncated={int(bool(payload.get('items_truncated')))} "
        f"preview_fingerprint={_display_value(payload.get('preview_fingerprint'))}"
    )
    for item in items:
        if not isinstance(item, Mapping):
            continue
        evidence = _json_text(item.get("evidence"))
        destination = item.get("destination_path")
        print(
            f"CURATION_ITEM id={_safe_text(item.get('item_id'))} "
            f"kind={_safe_text(item.get('kind'))} status={_safe_text(item.get('status'))} "
            f"action={_safe_text(item.get('action'))} "
            f"reason={_safe_text(item.get('reason'))} "
            f"source={_safe_text(item.get('source_path'))} "
            f"destination={_safe_text(destination) if destination else '-'} "
            f"evidence={evidence}"
        )
    return 0 if payload.get("coverage") == "complete" else 2


def _print_curation_error(args: argparse.Namespace, error: BaseException) -> int:
    if getattr(args, "curation_json", False):
        payload = (
            error.to_dict()
            if isinstance(error, CurationStateError)
            else {
                "kind": "curation-error",
                "coverage": "unavailable",
                "executable": False,
                "code": "curation_read_failed",
                "error_type": type(error).__name__,
                "message": str(error),
                "context": {},
            }
        )
        print(_json_text(_safe_payload(payload)))
    else:
        print(f"ERROR curation-preview {_safe_text(type(error).__name__)}: {_safe_text(error)}")
        if isinstance(error, CurationStateError) and error.context:
            print(f"CURATION_ERROR_CONTEXT {_json_text(_safe_payload(error.context))}")
    return 2


__all__ = ["run_curation_preview"]
