"""Agent-facing API for the non-mutating curation review lifecycle.

The API writes only Framework review facts.  It never creates ``file_actions``,
invokes KIO, or accepts a filesystem path from the caller.  A terminal review
decision is deliberately not an authorization grant.
"""

from __future__ import annotations

import sqlite3
import time
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text
from neocortex.curation.lifecycle import (
    CURATION_REVIEW_SCHEMA_VERSION,
    CurationLifecycleError,
    CurationLifecycleSnapshotChanged,
    CurationLifecycleUnavailable,
    _decide_curation_item_result,
    review_curation_page,
)
from neocortex.runtime.config.app_paths import default_state_directory


CURATION_REVIEW_API_SCHEMA = "neocortex.curation-review/v1"
CURATION_DECISION_API_SCHEMA = "neocortex.curation-decision/v1"
MAX_CURATION_REVIEW_API_PAGE = 100
MAX_CURATION_REVIEW_API_REQUEST_ID = 4_096


def _request_id(value: str | None) -> str:
    if value is None:
        return f"curation-review-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_CURATION_REVIEW_API_REQUEST_ID
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("request_id is invalid")
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


def _limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CURATION_REVIEW_API_PAGE
    ):
        raise ValueError(
            f"curation review limit must be between 1 and {MAX_CURATION_REVIEW_API_PAGE}"
        )
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
        raise ValueError("curation review cursor is invalid")
    return value


def _safe_text(value: object, *, limit: int = 4_096) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _safe_item(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"item_id": "", "item": {}, "state": None, "task_id": None}
    raw_item = value.get("item")
    item = raw_item if isinstance(raw_item, dict) else {}
    safe_item: dict[str, object] = {}
    for field in (
        "item_id",
        "kind",
        "status",
        "action",
        "source_path",
        "destination_path",
        "reason",
    ):
        raw = item.get(field)
        safe_item[field] = None if raw is None else _safe_text(raw, limit=4_096)
    evidence = sanitize_untrusted_payload(item.get("evidence", {}), budget=[4_096])
    safe_item["evidence"] = evidence if isinstance(evidence, dict) else {}
    decision = sanitize_untrusted_payload(value.get("decision"), budget=[256])
    return {
        "item_id": _safe_text(value.get("item_id", item.get("item_id", ""))),
        "item": safe_item,
        "task_id": (
            None
            if value.get("task_id") is None
            else _safe_text(value.get("task_id"), limit=512)
        ),
        "task_version": value.get("task_version"),
        "state": (
            None if value.get("state") is None else _safe_text(value.get("state"), limit=64)
        ),
        "current_event_id": (
            None
            if value.get("current_event_id") is None
            else _safe_text(value.get("current_event_id"), limit=512)
        ),
        "decision": decision if isinstance(decision, dict) else None,
    }


def _error_code(error: BaseException) -> tuple[str, bool]:
    message = _safe_text(error, limit=800).casefold()
    if isinstance(error, CurationLifecycleSnapshotChanged) or "snapshot" in message or "digest" in message:
        return "snapshot_changed", True
    if isinstance(error, ValueError):
        return "invalid_request", False
    if isinstance(error, CurationLifecycleUnavailable):
        return "unavailable", False
    if isinstance(error, sqlite3.DatabaseError):
        if any(token in message for token in ("corrupt", "malformed", "not a database")):
            return "corrupt", False
        return "unavailable", False
    if isinstance(error, CurationLifecycleError):
        return "unavailable", False
    return "unavailable", False


def _error_payload(
    *,
    operation: str,
    request_id: str,
    plan_id: str | None,
    error: BaseException,
) -> dict[str, object]:
    code, retryable = _error_code(error)
    return {
        "schema": CURATION_REVIEW_API_SCHEMA,
        "schema_version": CURATION_REVIEW_SCHEMA_VERSION,
        "kind": "neocortex_curation_review",
        "operation": operation,
        "request_id": request_id,
        "plan_id": plan_id,
        "scope": "personal",
        "status": "unavailable",
        "coverage": "unavailable",
        "read_only": False,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
        },
        "snapshot": None,
        "page": {
            "limit": None,
            "cursor": None,
            "next_cursor": None,
            "items_total": 0,
            "items": [],
        },
        "publication": None,
        "error": {
            "code": code,
            "message": _safe_text(error, limit=800),
            "retryable": retryable,
        },
        "exit_code": {
            "invalid_request": 2,
            "snapshot_changed": 5,
            "corrupt": 7,
            "unavailable": 1,
        }.get(code, 1),
    }


def curation_review_payload(
    plan_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    request_id: str | None = None,
    clock_ns=time.time_ns,
) -> dict[str, object]:
    """Publish one immutable curation page as advisory ReviewTasks."""

    try:
        normalized_request_id = _request_id(request_id)
    except ValueError as exc:
        return _error_payload(
            operation="curation-review",
            request_id=_request_id(None),
            plan_id=None,
            error=exc,
        )
    normalized_plan: str | None = None
    try:
        normalized_plan = _plan_id(plan_id)
        normalized_limit = _limit(limit)
        normalized_cursor = _cursor(cursor)
        state = default_state_directory()
        result = review_curation_page(
            state,
            state / "framework.sqlite3",
            plan_digest=normalized_plan,
            limit=normalized_limit,
            cursor=normalized_cursor,
            clock_ns=clock_ns,
        )
    except Exception as exc:
        return _error_payload(
            operation="curation-review",
            request_id=normalized_request_id,
            plan_id=normalized_plan,
            error=exc,
        )
    raw = result.to_dict()
    raw_items = raw.get("items")
    items = [_safe_item(item) for item in raw_items] if isinstance(raw_items, (list, tuple)) else []
    return {
        "schema": CURATION_REVIEW_API_SCHEMA,
        "schema_version": CURATION_REVIEW_SCHEMA_VERSION,
        "kind": "neocortex_curation_review",
        "operation": "curation-review",
        "request_id": normalized_request_id,
        "plan_id": normalized_plan,
        "scope": "personal",
        "status": "complete",
        "coverage": "complete",
        "read_only": False,
        "effects": {
            "state": "review_task_publication",
            "corpus": "none",
            "external": "none",
        },
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
        },
        "snapshot": {
            "plan_digest": raw["plan_digest"],
            "snapshot_id": raw["snapshot_id"],
        },
        "page": {
            "limit": normalized_limit,
            "cursor": raw["cursor"],
            "next_cursor": raw["next_cursor"],
            "items_total": raw["items_total"],
            "items": items,
        },
        "publication": raw["publication"],
        "error": None,
        "exit_code": 0,
    }


def curation_decide_payload(
    plan_id: str,
    item_id: str,
    *,
    expected_event_id: str,
    decision: str,
    decision_scope: str,
    actor: str,
    note: str | None = None,
    request_id: str | None = None,
    clock_ns=time.time_ns,
) -> dict[str, object]:
    """Append one human decision; it never authorizes a physical effect."""

    try:
        normalized_request_id = _request_id(request_id)
    except ValueError as exc:
        payload = _error_payload(
            operation="curation-decide",
            request_id=_request_id(None),
            plan_id=None,
            error=exc,
        )
        payload["schema"] = CURATION_DECISION_API_SCHEMA
        payload["kind"] = "neocortex_curation_decision"
        return payload
    normalized_plan: str | None = None
    try:
        normalized_plan = _plan_id(plan_id)
        state = default_state_directory()
        result = _decide_curation_item_result(
            state,
            state / "framework.sqlite3",
            plan_digest=normalized_plan,
            item_id=item_id,
            expected_event_id=expected_event_id,
            decision=decision,
            decision_scope=decision_scope,
            actor=actor,
            note=note,
            clock_ns=clock_ns,
        )
    except Exception as exc:
        payload = _error_payload(
            operation="curation-decide",
            request_id=normalized_request_id,
            plan_id=normalized_plan,
            error=exc,
        )
        payload["schema"] = CURATION_DECISION_API_SCHEMA
        payload["kind"] = "neocortex_curation_decision"
        return payload
    return {
        "schema": CURATION_DECISION_API_SCHEMA,
        "schema_version": CURATION_REVIEW_SCHEMA_VERSION,
        "kind": "neocortex_curation_decision",
        "operation": "curation-decide",
        "request_id": normalized_request_id,
        "plan_id": normalized_plan,
        "item_id": _safe_text(item_id, limit=4_096),
        "scope": "personal",
        "status": "complete",
        "idempotent": result.idempotent,
        "read_only": False,
        "effects": {"state": "review_task_event", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
        },
        "event": sanitize_untrusted_payload(result.event.to_dict(), budget=[1_024]),
        "error": None,
        "exit_code": 0,
    }


__all__ = (
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "MAX_CURATION_REVIEW_API_PAGE",
    "curation_decide_payload",
    "curation_review_payload",
)
