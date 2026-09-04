"""Fixed-root API for issuing explicit curation AuthorizationGrants.

The operation writes one immutable grant in the Framework owner. It is not an
``apply`` endpoint: no corpus path is accepted, no ``file_actions`` row is
created, and no KIO process is started.
"""

from __future__ import annotations

import sqlite3
import time
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text
from neocortex.curation.authorization import (
    CurationAuthorizationError,
    CurationAuthorizationSnapshotChanged,
    CurationAuthorizationUnavailable,
    authorize_curation_items,
)
from neocortex.workflow.authorization.contracts import (
    AUTHORIZATION_GRANT_SCHEMA,
    AUTHORIZATION_GRANT_SCHEMA_VERSION,
    AUTHORIZATION_ACTIONS,
    MAX_AUTHORIZATION_ITEMS,
)
from neocortex.runtime.config.app_paths import default_state_directory


CURATION_AUTHORIZATION_API_SCHEMA = AUTHORIZATION_GRANT_SCHEMA


def _request_id(value: str | None) -> str:
    if value is None:
        return f"curation-authorize-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 4_096
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


def _item_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("item_ids must be an array")
    if not 1 <= len(value) <= MAX_AUTHORIZATION_ITEMS:
        raise ValueError(f"item_ids must contain between 1 and {MAX_AUTHORIZATION_ITEMS} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item.strip() != item or len(item) > 4_096:
            raise ValueError("item_ids must contain bounded strings")
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError("item_ids cannot contain duplicates")
    return result


def _integer(label: str, value: object, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (value <= 0 if positive else value < 0):
        raise ValueError(f"{label} must be {'a positive' if positive else 'a non-negative'} integer")
    return value


def _safe_text(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _error_code(error: BaseException) -> tuple[str, bool]:
    message = _safe_text(error).casefold()
    if isinstance(error, CurationAuthorizationSnapshotChanged) or any(
        marker in message for marker in ("digest", "snapshot", "changed", "head")
    ):
        return "snapshot_changed", True
    if isinstance(error, ValueError):
        return "invalid_request", False
    if isinstance(error, CurationAuthorizationUnavailable):
        return "unavailable", False
    if isinstance(error, sqlite3.DatabaseError):
        if any(marker in message for marker in ("corrupt", "malformed", "not a database")):
            return "corrupt", False
        return "unavailable", False
    if isinstance(error, CurationAuthorizationError):
        return "authorization_denied", False
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
        "schema": CURATION_AUTHORIZATION_API_SCHEMA,
        "schema_version": AUTHORIZATION_GRANT_SCHEMA_VERSION,
        "kind": "neocortex_curation_authorization",
        "operation": operation,
        "request_id": request_id,
        "plan_id": plan_id,
        "scope": "personal",
        "status": "unavailable",
        "read_only": False,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
            "physical_effect_applied": False,
        },
        "grant": None,
        "error": {"code": code, "message": _safe_text(error), "retryable": retryable},
        "exit_code": 2 if code == "invalid_request" else 5 if code == "snapshot_changed" else 1,
    }


def curation_authorize_payload(
    plan_id: str,
    item_ids: list[str] | tuple[str, ...],
    *,
    action: str,
    actor: str,
    expires_ns: int,
    max_bytes: int,
    authorization_key: str | None = None,
    request_id: str | None = None,
    clock_ns=time.time_ns,
) -> dict[str, object]:
    """Issue one explicit, digest-bound grant without applying it."""

    try:
        request = _request_id(request_id)
    except ValueError as exc:
        return _error_payload(operation="curation-authorize", request_id=_request_id(None), plan_id=None, error=exc)
    normalized_plan: str | None = None
    try:
        normalized_plan = _plan_id(plan_id)
        ids = _item_ids(item_ids)
        if action not in AUTHORIZATION_ACTIONS:
            raise ValueError("authorization action is unsupported")
        expiry = _integer("expires_ns", expires_ns, positive=True)
        budget = _integer("max_bytes", max_bytes)
        state = default_state_directory()
        outcome = authorize_curation_items(
            state,
            state / "framework.sqlite3",
            plan_digest=normalized_plan,
            item_ids=ids,
            action=action,
            actor=actor,
            expires_ns=expiry,
            max_bytes=budget,
            authorization_key=authorization_key,
            clock_ns=clock_ns,
        )
    except Exception as exc:
        return _error_payload(
            operation="curation-authorize",
            request_id=request,
            plan_id=normalized_plan,
            error=exc,
        )
    grant = sanitize_untrusted_payload(outcome.grant.to_dict(), budget=[4_096])
    return {
        "schema": CURATION_AUTHORIZATION_API_SCHEMA,
        "schema_version": AUTHORIZATION_GRANT_SCHEMA_VERSION,
        "kind": "neocortex_curation_authorization",
        "operation": "curation-authorize",
        "request_id": request,
        "plan_id": normalized_plan,
        "scope": "personal",
        "status": "complete",
        "idempotent": outcome.idempotent,
        "read_only": False,
        "effects": {"state": "authorization_grant", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": True,
            "physical_effect_applied": False,
        },
        "grant": grant,
        "error": None,
        "exit_code": 0,
    }


__all__ = (
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "curation_authorize_payload",
)
