"""Fixed-root API for curation scan and exact source verification.

The two operations are advisory and corpus-preserving.  ``curation_scan``
projects the currently published plan through the same fixed-root reader as
``curation_plan``; ``curation_verify`` rechecks the physical files referenced by
that plan and performs the byte comparison required for an exact duplicate
claim.  Neither operation creates ReviewTasks, grants, ``file_actions`` or
filesystem effects.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any, Literal, NotRequired, Required, TypedDict
from uuid import uuid4

from neocortex.api.curation_api import curation_plan_payload
from neocortex.api.curation_api import _safe_source_heads
from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.preview import build_curation_plan_page
from neocortex.deduplication.domain.models import VALID_VERIFICATION_MODES
from neocortex.runtime.config.app_paths import default_state_directory


CURATION_SCAN_API_SCHEMA = "neocortex.curation-scan/v1"
CURATION_VERIFY_API_SCHEMA = "neocortex.curation-verify/v1"
CURATION_VERIFICATION_SCHEMA_VERSION = 1
MAX_CURATION_SCAN_PAGE = 100
MAX_CURATION_VERIFY_PAGE = 100
MAX_CURATION_VERIFY_ITEM_IDS = 100
MAX_VERIFICATION_ITEMS = 100

_EFFECTS = {"state": "none", "corpus": "none", "external": "none"}
_TRUST = {
    "content_class": "untrusted_corpus_evidence",
    "instruction_authority": False,
    "tools_authorized": False,
    "actions_authorized": False,
}


class CurationVerificationItemPayload(TypedDict):
    """One bounded physical verification result."""

    checked_files: Required[int]
    item_id: Required[str]
    kind: Required[str]
    observed_mode: Required[Literal["full_hash"] | None]
    persisted_mode: Required[Literal["legacy_unknown", "fast", "partial", "full_hash"] | None]
    reason: Required[str]
    source_path: Required[str]
    status: Required[Literal["verified", "source_changed", "not_verified", "not_applicable"]]
    verified_files: Required[int]


class CurationVerificationResultPayload(TypedDict):
    """Typed result shared by direct API, SDK and MCP adapters."""

    bytes_checked: Required[int]
    coverage: Required[Literal["complete", "partial"]]
    files_checked: Required[int]
    items: Required[list[CurationVerificationItemPayload]]
    items_failed: Required[int]
    items_skipped: Required[int]
    items_total: Required[int]
    items_verified: Required[int]
    plan_digest: Required[str]
    snapshot_id: Required[str]
    source_heads: Required[list[dict[str, Any]]]
    status: Required[Literal["complete", "partial", "snapshot_changed"]]
    # Optional for compatibility with older adapters; the canonical verifier
    # emits the bounded counters whenever it has them.
    metrics: NotRequired[dict[str, int]]


class CurationVerificationErrorPayload(TypedDict):
    """Stable error shape for scan and verify."""

    code: Required[
        Literal[
            "invalid_request",
            "invalid_cursor",
            "snapshot_changed",
            "schema_incompatible",
            "corrupt",
            "unavailable",
            "partial",
            "not_verified",
        ]
    ]
    message: Required[str]
    retryable: Required[bool]


class CurationScanPagePayload(TypedDict, total=False):
    """The bounded plan page carried by scan."""

    limit: NotRequired[int]
    cursor: NotRequired[str | None]
    next_cursor: NotRequired[str | None]
    complete: NotRequired[bool]
    plan_digest: NotRequired[str | None]
    items_total: NotRequired[int]
    items: NotRequired[list[dict[str, Any]]]
    inventory_files: NotRequired[int]
    duplicate_groups: NotRequired[int]
    duplicate_members: NotRequired[int]
    reclaimable_bytes: NotRequired[int]
    organization_plans: NotRequired[int]
    empty_files: NotRequired[int]


class CurationScanResultPayload(TypedDict):
    """Typed scan projection over the current published plan."""

    plan_digest: Required[str | None]
    snapshot_id: Required[str | None]
    scan_id: Required[int | None]
    root: Required[str | None]
    source_heads: Required[list[dict[str, Any]]]
    page: Required[CurationScanPagePayload]
    source: Required[Literal["published_curation_plan"]]


class CurationScanOutput(TypedDict):
    """Canonical scan envelope."""

    schema: Required[Literal["neocortex.curation-scan/v1"]]
    schema_version: Required[Literal[1]]
    kind: Required[Literal["neocortex_curation_scan"]]
    operation: Required[Literal["curation-scan"]]
    request_id: Required[str]
    plan_id: Required[str | None]
    scope: Required[Literal["personal"]]
    status: Required[Literal["complete", "partial", "unavailable"]]
    coverage: Required[Literal["complete", "partial", "unavailable"]]
    read_only: Required[Literal[True]]
    effects: Required[dict[str, Literal["none"]]]
    trust: Required[dict[str, object]]
    snapshot: Required[dict[str, object] | None]
    result: Required[CurationScanResultPayload | None]
    error: Required[CurationVerificationErrorPayload | None]
    exit_code: Required[int]


class CurationVerifyOutput(TypedDict):
    """Canonical exact-verification envelope."""

    schema: Required[Literal["neocortex.curation-verify/v1"]]
    schema_version: Required[Literal[1]]
    kind: Required[Literal["neocortex_curation_verify"]]
    operation: Required[Literal["curation-verify"]]
    request_id: Required[str]
    plan_id: Required[str | None]
    scope: Required[Literal["personal"]]
    status: Required[Literal["complete", "partial", "snapshot_changed", "unavailable"]]
    coverage: Required[Literal["complete", "partial", "unavailable"]]
    read_only: Required[Literal[True]]
    effects: Required[dict[str, Literal["none"]]]
    trust: Required[dict[str, object]]
    snapshot: Required[dict[str, object] | None]
    result: Required[CurationVerificationResultPayload | None]
    error: Required[CurationVerificationErrorPayload | None]
    exit_code: Required[int]


def _request_id(value: str | None, *, prefix: str) -> str:
    if value is None:
        return f"{prefix}-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("request_id is invalid")
    return value


def _limit(value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"curation verification limit must be between 1 and {maximum}")
    return value


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"curation verification {label} is invalid")
    return value


def _cursor(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value.encode("utf-8")) > 2_048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("curation verification cursor is invalid")
    return value


def _plan_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("plan_id must be sha256:<64 lowercase hex characters>")
    return value


def _item_ids(value: Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("item_ids must be an array")
    if not value:
        raise ValueError("item_ids cannot be empty")
    if len(value) > MAX_CURATION_VERIFY_ITEM_IDS:
        raise ValueError("item_ids exceed the verification bound")
    result: list[str] = []
    for item_id in value:
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id.strip() != item_id
            or len(item_id) > 4_096
            or any(ord(character) < 32 or ord(character) == 127 for character in item_id)
        ):
            raise ValueError("item_ids must contain bounded strings")
        result.append(item_id)
    if len(set(result)) != len(result):
        raise ValueError("item_ids cannot contain duplicates")
    return tuple(result)


def _error_code(error: BaseException) -> tuple[str, bool, int]:
    message = sanitize_untrusted_text(error, limit=800).casefold()
    type_name = type(error).__name__
    if type_name == "CurationVerificationSnapshotChanged" or "changed" in message:
        return "snapshot_changed", True, 5
    if isinstance(error, ValueError):
        return "invalid_request", False, 2
    if type_name == "CurationVerificationUnavailable":
        return "unavailable", False, 1
    if isinstance(error, sqlite3.DatabaseError):
        if any(token in message for token in ("corrupt", "malformed", "not a database")):
            return "corrupt", False, 7
        return "unavailable", False, 1
    if type_name.startswith("CurationVerification"):
        return "not_verified", False, 2
    return "unavailable", False, 1


def _error_payload(
    *,
    schema: str,
    kind: str,
    operation: str,
    request_id: str,
    plan_id: str | None,
    error: BaseException,
) -> dict[str, object]:
    code, retryable, exit_code = _error_code(error)
    return {
        "schema": schema,
        "schema_version": CURATION_VERIFICATION_SCHEMA_VERSION,
        "kind": kind,
        "operation": operation,
        "request_id": request_id,
        "plan_id": plan_id,
        "scope": "personal",
        "status": "unavailable",
        "coverage": "unavailable",
        "read_only": True,
        "effects": dict(_EFFECTS),
        "trust": dict(_TRUST),
        "snapshot": None,
        "result": None,
        "error": {
            "code": code,
            "message": sanitize_untrusted_text(error, limit=800),
            "retryable": retryable,
        },
        "exit_code": exit_code,
    }


def _scan_exit_code(coverage: object, error: object) -> int:
    if isinstance(error, dict):
        code = error.get("code")
        return {
            "invalid_request": 2,
            "invalid_cursor": 2,
            "snapshot_changed": 5,
            "corrupt": 7,
            "schema_incompatible": 6,
            "partial": 2,
            "unavailable": 1,
        }.get(code, 2)
    return 0 if coverage == "complete" else 2


def _scan_from_plan(
    page_payload: dict[str, Any],
    *,
    request_id: str,
) -> CurationScanOutput:
    coverage = page_payload.get("coverage")
    if coverage not in {"complete", "partial", "unavailable"}:
        coverage = "unavailable"
    published_error = page_payload.get("error")
    if published_error is not None and not isinstance(published_error, dict):
        raise ValueError("published curation scan error is invalid")
    # An upstream error can never coexist with a successful scan, even if a
    # malformed producer labels the page complete.  Preserve its typed exit
    # code while failing the public coverage closed.
    if published_error is not None and coverage == "complete":
        coverage = "unavailable"
    page = page_payload.get("page")
    if not isinstance(page, dict):
        page = {}
    else:
        # ``curation_plan_payload`` has already sanitized each evidence object
        # at its structural boundary.  Do not run the generic shared-node
        # sanitizer again here: a large but valid page would otherwise lose
        # items and acquire a truncation marker while retaining ``complete``.
        page = dict(page)
        raw_items = page.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("published curation scan page items are invalid")
        copied_items: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("published curation scan item is invalid")
            copied_items.append(dict(item))
        page["items"] = copied_items
    published_snapshot = page_payload.get("snapshot")
    if not isinstance(published_snapshot, dict):
        published_snapshot = {}
    source_heads = published_snapshot.get("source_heads", [])
    if not isinstance(source_heads, list):
        raise ValueError("published curation scan source_heads are invalid")
    if any(not isinstance(head, dict) for head in source_heads):
        raise ValueError("published curation scan source head is invalid")
    result = {
        "plan_digest": page.get("plan_digest"),
        "snapshot_id": (
            published_snapshot.get("snapshot_id")
        ),
        "scan_id": (
            published_snapshot.get("scan_id")
        ),
        "root": (
            published_snapshot.get("root")
        ),
        "source_heads": [dict(head) for head in source_heads],
        "page": page,
        "source": "published_curation_plan",
    }
    return {
        "schema": CURATION_SCAN_API_SCHEMA,
        "schema_version": CURATION_VERIFICATION_SCHEMA_VERSION,
        "kind": "neocortex_curation_scan",
        "operation": "curation-scan",
        "request_id": request_id,
        "plan_id": result["plan_digest"],
        "scope": "personal",
        "status": "complete" if coverage == "complete" else coverage,
        "coverage": coverage,
        "read_only": True,
        "effects": dict(_EFFECTS),
        "trust": dict(_TRUST),
        "snapshot": {
            "plan_digest": result["plan_digest"],
            "snapshot_id": result["snapshot_id"],
            "root": result["root"],
            "scan_id": result["scan_id"],
            "source_heads": result["source_heads"],
        },
        "result": result,
        "error": published_error,
        "exit_code": _scan_exit_code(coverage, published_error),
    }


def curation_scan_payload(
    *,
    limit: int = 50,
    cursor: str | None = None,
    request_id: str | None = None,
) -> CurationScanOutput:
    """Return one bounded scan view over the currently published plan."""

    try:
        normalized_request = _request_id(request_id, prefix="curation-scan")
        normalized_limit = _limit(limit, maximum=MAX_CURATION_SCAN_PAGE)
        normalized_cursor = _cursor(cursor)
    except (TypeError, ValueError) as exc:
        return _error_payload(
            schema=CURATION_SCAN_API_SCHEMA,
            kind="neocortex_curation_scan",
            operation="curation-scan",
            request_id=_request_id(None, prefix="curation-scan"),
            plan_id=None,
            error=exc,
        )
    try:
        page = curation_plan_payload(
            limit=normalized_limit,
            cursor=normalized_cursor,
            request_id=normalized_request,
        )
        return _scan_from_plan(page, request_id=normalized_request)
    except Exception as exc:
        return _error_payload(
            schema=CURATION_SCAN_API_SCHEMA,
            kind="neocortex_curation_scan",
            operation="curation-scan",
            request_id=normalized_request,
            plan_id=None,
            error=exc,
        )


def _verification_success(
    result: dict[str, object],
    *,
    request_id: str,
    plan_id: str,
    cursor: str | None,
) -> CurationVerifyOutput:
    status = result.get("status")
    coverage = result.get("coverage")
    if status not in {"complete", "partial", "snapshot_changed"}:
        status = "partial"
    if coverage not in {"complete", "partial"}:
        coverage = "partial"
    raw_items = result.get("items")
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("curation verification items are invalid")
    normalized_items: list[dict[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("curation verification item is invalid")
        required = {
            "checked_files",
            "item_id",
            "kind",
            "observed_mode",
            "persisted_mode",
            "reason",
            "source_path",
            "status",
            "verified_files",
        }
        if set(raw_item) != required:
            raise ValueError("curation verification item shape is invalid")
        item_status = raw_item["status"]
        if not isinstance(item_status, str) or item_status not in {
            "verified",
            "source_changed",
            "not_verified",
            "not_applicable",
        }:
            raise ValueError("curation verification item status is invalid")
        persisted_mode = raw_item["persisted_mode"]
        observed_mode = raw_item["observed_mode"]
        if (
            persisted_mode is not None
            and (
                not isinstance(persisted_mode, str)
                or persisted_mode not in VALID_VERIFICATION_MODES
            )
        ):
            raise ValueError("curation verification persisted mode is invalid")
        if observed_mode is not None and observed_mode != "full_hash":
            raise ValueError("curation verification observed mode is invalid")
        normalized_items.append(
            {
                "checked_files": _nonnegative_int(
                    raw_item["checked_files"], label="checked_files"
                ),
                "item_id": sanitize_untrusted_text(raw_item["item_id"], limit=4_096),
                "kind": sanitize_untrusted_text(raw_item["kind"], limit=256),
                "observed_mode": observed_mode,
                "persisted_mode": persisted_mode,
                "reason": sanitize_untrusted_text(raw_item["reason"], limit=4_096),
                "source_path": sanitize_untrusted_text(
                    raw_item["source_path"], limit=4_096
                ),
                "status": item_status,
                "verified_files": _nonnegative_int(
                    raw_item["verified_files"], label="verified_files"
                ),
            }
        )
    raw_metrics = result.get("metrics")
    normalized_metrics: dict[str, int] = {}
    if raw_metrics is not None:
        if not isinstance(raw_metrics, dict):
            raise ValueError("curation verification metrics are invalid")
        if len(raw_metrics) > 64:
            raise ValueError("curation verification metrics exceed the bounded key limit")
        for raw_name, raw_value in raw_metrics.items():
            if not isinstance(raw_name, str) or not raw_name or len(raw_name) > 128:
                raise ValueError("curation verification metric name is invalid")
            normalized_metrics[sanitize_untrusted_text(raw_name, limit=128)] = _nonnegative_int(
                raw_value,
                label=f"metric {raw_name}",
            )
    normalized_result = {
        "bytes_checked": _nonnegative_int(result.get("bytes_checked"), label="bytes_checked"),
        "coverage": coverage,
        "files_checked": _nonnegative_int(result.get("files_checked"), label="files_checked"),
        "items": normalized_items,
        "items_failed": _nonnegative_int(result.get("items_failed"), label="items_failed"),
        "items_skipped": _nonnegative_int(result.get("items_skipped"), label="items_skipped"),
        "items_total": _nonnegative_int(result.get("items_total"), label="items_total"),
        "items_verified": _nonnegative_int(result.get("items_verified"), label="items_verified"),
        "plan_digest": sanitize_untrusted_text(result.get("plan_digest"), limit=4_096),
        "snapshot_id": sanitize_untrusted_text(result.get("snapshot_id"), limit=4_096),
        "source_heads": _safe_source_heads(result.get("source_heads", ())),
        "status": status,
        "metrics": normalized_metrics,
    }
    return {
        "schema": CURATION_VERIFY_API_SCHEMA,
        "schema_version": CURATION_VERIFICATION_SCHEMA_VERSION,
        "kind": "neocortex_curation_verify",
        "operation": "curation-verify",
        "request_id": request_id,
        "plan_id": plan_id,
        "scope": "personal",
        "status": status,
        "coverage": coverage,
        "read_only": True,
        "effects": dict(_EFFECTS),
        "trust": dict(_TRUST),
        "snapshot": {
            "plan_digest": normalized_result["plan_digest"],
            "snapshot_id": normalized_result["snapshot_id"],
            "cursor": cursor,
            "source_heads": normalized_result["source_heads"],
        },
        "result": normalized_result,
        "error": (
            None
            if status == "complete"
            else {
                "code": "snapshot_changed" if status == "snapshot_changed" else "not_verified",
                "message": (
                    "curation source changed during verification"
                    if status == "snapshot_changed"
                    else "curation verification is incomplete"
                ),
                "retryable": status == "snapshot_changed",
            }
        ),
        "exit_code": 0 if status == "complete" else 5 if status == "snapshot_changed" else 2,
    }


def curation_verify_payload(
    plan_id: str,
    *,
    item_ids: Sequence[str] | None = None,
    limit: int = 100,
    cursor: str | None = None,
    request_id: str | None = None,
) -> CurationVerifyOutput:
    """Verify duplicate items from one current, digest-bound plan page."""

    try:
        normalized_request = _request_id(request_id, prefix="curation-verify")
        normalized_plan = _plan_id(plan_id)
        normalized_limit = _limit(limit, maximum=MAX_CURATION_VERIFY_PAGE)
        normalized_ids = _item_ids(item_ids)
        normalized_cursor = _cursor(cursor)
    except (TypeError, ValueError) as exc:
        return _error_payload(
            schema=CURATION_VERIFY_API_SCHEMA,
            kind="neocortex_curation_verify",
            operation="curation-verify",
            request_id=_request_id(None, prefix="curation-verify"),
            plan_id=None,
            error=exc,
        )
    try:
        from neocortex.curation.verification import (
            CurationVerificationSnapshotChanged,
            verify_curation_page,
        )

        page = build_curation_plan_page(
            default_state_directory(),
            limit=normalized_limit,
            cursor=normalized_cursor,
        )
        if page.plan_digest != normalized_plan:
            raise CurationVerificationSnapshotChanged("curation plan digest changed")
        verification = verify_curation_page(
            page,
            item_ids=normalized_ids,
            max_items=MAX_VERIFICATION_ITEMS,
            state_directory=default_state_directory(),
        )
        return _verification_success(
            verification.to_dict(),
            request_id=normalized_request,
            plan_id=normalized_plan,
            cursor=normalized_cursor,
        )
    except Exception as exc:
        return _error_payload(
            schema=CURATION_VERIFY_API_SCHEMA,
            kind="neocortex_curation_verify",
            operation="curation-verify",
            request_id=normalized_request,
            plan_id=normalized_plan,
            error=exc,
        )


__all__ = (
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFICATION_SCHEMA_VERSION",
    "CURATION_VERIFY_API_SCHEMA",
    "MAX_CURATION_SCAN_PAGE",
    "MAX_CURATION_VERIFY_PAGE",
    "CurationScanOutput",
    "CurationScanPagePayload",
    "CurationScanResultPayload",
    "CurationVerificationErrorPayload",
    "CurationVerificationItemPayload",
    "CurationVerificationResultPayload",
    "CurationVerifyOutput",
    "curation_scan_payload",
    "curation_verify_payload",
)
