"""Public adapters for the grant-bound 0.11 curation application slice.

The JSON-facing adapter never selects a filesystem backend on its own.  A
controlled caller must inject a backend and an already-signed Framework run;
ordinary CLI/API calls therefore fail closed with ``backend_unavailable`` rather
than silently invoking KIO or mutating the configured corpus.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Required, TypedDict
from uuid import uuid4

from neocortex.curation.application import (
    CURATION_APPLY_SCHEMA,
    CurationApplicationError,
    CurationApplicationResult,
    MutationBackend,
    apply_authorization_grant,
    reconcile_curation_actions,
)
from neocortex.api.read_contract import sanitize_untrusted_payload, sanitize_untrusted_text
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.config.app_paths import default_state_directory


CURATION_RECONCILE_API_SCHEMA = "neocortex.curation-reconcile/v1"
CURATION_APPLY_API_SCHEMA = CURATION_APPLY_SCHEMA


class CurationApplyOutput(TypedDict, total=False):
    schema: Required[str]
    schema_version: Required[int]
    kind: Required[str]
    operation: Required[str]
    request_id: Required[str]
    grant_id: Required[str | None]
    scope: Required[str]
    status: Required[str]
    read_only: Required[bool]
    effects: Required[dict[str, str]]
    trust: Required[dict[str, object]]
    attempt: Required[dict[str, object] | None]
    result: Required[dict[str, object] | None]
    error: Required[dict[str, object] | None]
    exit_code: Required[int]


class CurationReconcileOutput(CurationApplyOutput, total=False):
    """Shared envelope shape for bounded reconciliation evidence."""


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


def _safe_text(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _error_payload(
    *,
    schema: str,
    operation: str,
    request_id: str,
    grant_id: str | None,
    code: str,
    error: BaseException | str,
    retryable: bool,
    kind: str = "neocortex_curation_application",
) -> CurationApplyOutput:
    message = _safe_text(error)
    return {
        "schema": schema,
        "schema_version": 1,
        "kind": kind,
        "operation": operation,
        "request_id": request_id,
        "grant_id": grant_id,
        "scope": "personal",
        "status": "unavailable" if code in {"unavailable", "backend_unavailable"} else "blocked",
        "read_only": False,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
            "physical_effect_applied": False,
        },
        "attempt": None,
        "result": None,
        "error": {"code": code, "message": message, "retryable": retryable},
        "exit_code": (
            2
            if code == "invalid_request"
            else 5
            if code in {"snapshot_changed", "grant_expired", "head_changed"}
            else 7
            if code in {"schema_incompatible", "corrupt"}
            else 1
        ),
    }


def _result_payload(
    result: CurationApplicationResult,
    *,
    request_id: str,
) -> dict[str, object]:
    payload = result.to_dict()
    return {
        "schema": CURATION_APPLY_SCHEMA,
        "schema_version": 1,
        "kind": "neocortex_curation_application",
        "operation": "curation-apply",
        "request_id": request_id,
        "grant_id": result.grant_id,
        "scope": "personal",
        "status": result.status,
        "read_only": False,
        "effects": {
            "state": "file_actions",
            "corpus": "effect_or_recovery",
            "external": "none",
        },
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": True,
            "physical_effect_applied": result.applied > 0,
        },
        "attempt": {
            "actions_attempted": result.actions_attempted,
            "bytes_attempted": result.bytes_attempted,
            "cancelled": result.cancelled,
        },
        "result": payload,
        "error": None,
        "exit_code": (
            0
            if result.status == "complete"
            else 3
            if result.status == "recovery_required"
            else 2
        ),
    }


def curation_apply_payload(
    grant_id: str,
    *,
    confirm_grant_id: str | None = None,
    run_id: int | None = None,
    backend: MutationBackend | None = None,
    state_directory=None,
    database=None,
    request_id: str | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
    cancellation_check: Callable[[], bool] | None = None,
) -> CurationApplyOutput:
    """Apply one exact grant, requiring an explicit confirmation and backend."""

    try:
        request = _request_id(request_id, prefix="curation-apply")
    except ValueError as exc:
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=f"curation-apply-{uuid4().hex}",
            grant_id=None,
            code="invalid_request",
            error=exc,
            retryable=False,
        )
    if not isinstance(grant_id, str) or not grant_id or grant_id.strip() != grant_id:
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=request,
            grant_id=None,
            code="invalid_request",
            error="grant_id is invalid",
            retryable=False,
        )
    if confirm_grant_id != grant_id:
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=request,
            grant_id=grant_id,
            code="invalid_request",
            error="exact grant confirmation is required",
            retryable=False,
        )
    if backend is None or run_id is None:
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=request,
            grant_id=grant_id,
            code="backend_unavailable",
            error="no injected Linux mutation backend and signed run were supplied",
            retryable=False,
        )
    state_root = default_state_directory() if state_directory is None else Path(state_directory)
    database_path = state_root / "framework.sqlite3" if database is None else database
    try:
        with FrameworkState(database_path, existing_only=True) as state:
            result = apply_authorization_grant(
                state_root,
                database_path,
                grant_id,
                run_id=run_id,
                backend=backend,
                state=state,
                clock_ns=clock_ns,
                cancellation_check=cancellation_check,
            )
    except CurationApplicationError as exc:
        message = _safe_text(exc).casefold()
        code = (
            "grant_expired"
            if "expired" in message
            else "snapshot_changed"
            if "changed" in message
            else "authorization_denied"
            if "grant" in message or "reviewtask" in message
            else "unavailable"
        )
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=request,
            grant_id=grant_id,
            code=code,
            error=exc,
            retryable=code == "snapshot_changed",
        )
    except (sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return _error_payload(
            schema=CURATION_APPLY_SCHEMA,
            operation="curation-apply",
            request_id=request,
            grant_id=grant_id,
            code="unavailable",
            error=exc,
            retryable=False,
        )
    return _result_payload(result, request_id=request)


def curation_reconcile_payload(
    *,
    actor: str,
    limit: int = 100,
    after_action_id: int = 0,
    run_id: int | None = None,
    state_directory=None,
    database=None,
    request_id: str | None = None,
    provenance: Mapping[str, object] | None = None,
    confirm: bool = False,
) -> CurationReconcileOutput:
    """Record bounded recovery observations; never retries or mutates corpus."""

    try:
        request = _request_id(request_id, prefix="curation-reconcile")
    except ValueError as exc:
        return _error_payload(
            schema=CURATION_RECONCILE_API_SCHEMA,
            operation="curation-reconcile",
            request_id=f"curation-reconcile-{uuid4().hex}",
            grant_id=None,
            code="invalid_request",
            error=exc,
            retryable=False,
            kind="neocortex_curation_reconciliation",
        )
    if confirm is not True:
        return _error_payload(
            schema=CURATION_RECONCILE_API_SCHEMA,
            operation="curation-reconcile",
            request_id=request,
            grant_id=None,
            code="invalid_request",
            error="explicit reconciliation confirmation is required",
            retryable=False,
            kind="neocortex_curation_reconciliation",
        )
    state_root = default_state_directory() if state_directory is None else Path(state_directory)
    database_path = state_root / "framework.sqlite3" if database is None else database
    try:
        events = reconcile_curation_actions(
            database_path,
            actor=actor,
            provenance=provenance,
            limit=limit,
            after_action_id=after_action_id,
            run_id=run_id,
        )
    except (ValueError, sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return _error_payload(
            schema=CURATION_RECONCILE_API_SCHEMA,
            operation="curation-reconcile",
            request_id=request,
            grant_id=None,
            code="unavailable",
            error=exc,
            retryable=False,
            kind="neocortex_curation_reconciliation",
        )
    result = sanitize_untrusted_payload(
        {
            "events": [event.__dict__ if hasattr(event, "__dict__") else {
                "event_id": event.event_id,
                "action_id": event.action_id,
                "classification": event.classification,
                "recommendation": event.recommendation,
                "sequence": event.sequence,
            } for event in events],
            "count": len(events),
        },
        budget=[10_000],
    )
    return {
        "schema": CURATION_RECONCILE_API_SCHEMA,
        "schema_version": 1,
        "kind": "neocortex_curation_reconciliation",
        "operation": "curation-reconcile",
        "request_id": request,
        "grant_id": None,
        "scope": "personal",
        "status": "complete",
        "read_only": False,
        "effects": {"state": "reconciliation_events", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "actions_authorized": False,
            "physical_effect_applied": False,
        },
        "result": result,
        "error": None,
        "exit_code": 0,
    }


__all__ = (
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CurationApplyOutput",
    "CurationReconcileOutput",
    "curation_apply_payload",
    "curation_reconcile_payload",
)
