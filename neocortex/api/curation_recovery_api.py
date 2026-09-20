"""Fixed-root status, preview and explicitly confirmed curation restoration."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Required, TypedDict, cast
from uuid import uuid4

from neocortex.api.read_contract import sanitize_untrusted_text
from neocortex.curation.recovery import (
    CURATION_RESTORE_SCHEMA,
    RestoreBackend,
    restore_curation_action,
    restore_curation_preview,
)
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.config.app_paths import default_state_directory
from neocortex.workflow.actions.file_action_recovery import list_file_action_reconciliations


CURATION_RECOVERY_STATUS_API_SCHEMA = "neocortex.curation-recovery-status/v1"
CURATION_RESTORE_API_SCHEMA = CURATION_RESTORE_SCHEMA


class CurationRecoveryStatusOutput(TypedDict, total=False):
    schema: Required[str]
    schema_version: Required[int]
    kind: Required[str]
    operation: Required[str]
    request_id: Required[str]
    status: Required[str]
    read_only: Required[bool]
    effects: Required[dict[str, str]]
    result: Required[dict[str, object] | None]
    error: Required[dict[str, object] | None]
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


def _safe_text(value: object, *, limit: int = 800) -> str:
    return sanitize_untrusted_text(value, limit=limit, single_line=True)


def _error_payload(
    *,
    schema: str,
    operation: str,
    request_id: str,
    error: BaseException | str,
    code: str,
    retryable: bool,
    kind: str,
) -> dict[str, object]:
    return {
        "schema": schema,
        "schema_version": 1,
        "kind": kind,
        "operation": operation,
        "request_id": request_id,
        "status": "blocked" if code == "invalid_request" else "unavailable",
        "read_only": True,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "result": None,
        "error": {"code": code, "message": _safe_text(error), "retryable": retryable},
        "exit_code": 2 if code == "invalid_request" else 1,
    }


def _paths(
    state_directory: Path | str | None,
    database: Path | str | None,
) -> tuple[Path, Path]:
    state = default_state_directory() if state_directory is None else Path(state_directory)
    return state, state / "framework.sqlite3" if database is None else Path(database)


def _error(
    *,
    schema: str,
    operation: str,
    request_id: str,
    error: BaseException | str,
    code: str,
    kind: str,
) -> dict[str, object]:
    return cast(
        dict[str, object],
        _error_payload(
            schema=schema,
            operation=operation,
            request_id=request_id,
            code=code,
            error=error,
            retryable=False,
            kind=kind,
        ),
    )


def curation_recovery_status_payload(
    *,
    action_id: int | None = None,
    limit: int = 100,
    after_action_id: int = 0,
    run_id: int | None = None,
    state_directory=None,
    database=None,
    request_id: str | None = None,
) -> CurationRecoveryStatusOutput:
    """Read bounded recovery classifications without creating state."""

    try:
        request = _request_id(request_id, prefix="curation-recovery-status")
    except ValueError as exc:
        return cast(
            CurationRecoveryStatusOutput,
            _error(
                schema=CURATION_RECOVERY_STATUS_API_SCHEMA,
                operation="curation-recovery-status",
                request_id=f"curation-recovery-status-{uuid4().hex}",
                error=exc,
                code="invalid_request",
                kind="neocortex_curation_recovery_status",
            ),
        )
    if action_id is not None:
        if isinstance(action_id, bool) or not isinstance(action_id, int) or action_id < 1:
            return cast(
                CurationRecoveryStatusOutput,
                _error(
                    schema=CURATION_RECOVERY_STATUS_API_SCHEMA,
                    operation="curation-recovery-status",
                    request_id=request,
                    error="action_id must be positive",
                    code="invalid_request",
                    kind="neocortex_curation_recovery_status",
                ),
            )
        after_action_id = action_id - 1
        limit = 1
    try:
        _state, database_path = _paths(state_directory, database)
        rows = list_file_action_reconciliations(
            database_path,
            limit=limit,
            after_action_id=after_action_id,
            run_id=run_id,
        )
    except ValueError as exc:
        return cast(
            CurationRecoveryStatusOutput,
            _error(
                schema=CURATION_RECOVERY_STATUS_API_SCHEMA,
                operation="curation-recovery-status",
                request_id=request,
                error=exc,
                code="invalid_request",
                kind="neocortex_curation_recovery_status",
            ),
        )
    except (sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return cast(
            CurationRecoveryStatusOutput,
            _error(
                schema=CURATION_RECOVERY_STATUS_API_SCHEMA,
                operation="curation-recovery-status",
                request_id=request,
                error=exc,
                code="unavailable",
                kind="neocortex_curation_recovery_status",
            ),
        )
    result = {
        "count": len(rows),
        "items": [
            {
                "action_id": row.action_id,
                "run_id": row.run_id,
                "action_type": row.action_type,
                "source_path": row.source_path,
                "target_path": row.target_path,
                "recorded_status": row.recorded_status,
                "classification": row.classification,
                "recommendation": row.recommendation,
                "detail": _safe_text(row.detail),
            }
            for row in rows
        ],
    }
    return {
        "schema": CURATION_RECOVERY_STATUS_API_SCHEMA,
        "schema_version": 1,
        "kind": "neocortex_curation_recovery_status",
        "operation": "curation-recovery-status",
        "request_id": request,
        "status": "complete",
        "read_only": True,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "result": result,
        "error": None,
        "exit_code": 0,
    }


def curation_restore_preview_payload(
    action_id: int,
    *,
    state_directory=None,
    database=None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Return fixed-root restore evidence and confirmation token."""

    try:
        request = _request_id(request_id, prefix="curation-restore-preview")
        _state, database_path = _paths(state_directory, database)
        result = restore_curation_preview(database_path, action_id)
    except ValueError as exc:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore-preview",
            request_id=request
            if "request" in locals()
            else f"curation-restore-preview-{uuid4().hex}",
            error=exc,
            code="invalid_request",
            kind="neocortex_curation_restore_preview",
        )
    except (sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore-preview",
            request_id=request,
            error=exc,
            code="unavailable",
            kind="neocortex_curation_restore_preview",
        )
    return {
        "schema": CURATION_RESTORE_API_SCHEMA,
        "schema_version": 1,
        "kind": "neocortex_curation_restore_preview",
        "operation": "curation-restore-preview",
        "request_id": request,
        "status": "complete",
        "read_only": True,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "result": result,
        "error": None,
        "exit_code": 0,
    }


def curation_restore_payload(
    action_id: int,
    *,
    confirm_action_id: int | None = None,
    confirmation: str | None = None,
    actor: str | None = None,
    backend: RestoreBackend | None = None,
    state_directory=None,
    database=None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Restore only after an exact action/receipt confirmation and backend injection."""

    try:
        request = _request_id(request_id, prefix="curation-restore")
    except ValueError as exc:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore",
            request_id=f"curation-restore-{uuid4().hex}",
            error=exc,
            code="invalid_request",
            kind="neocortex_curation_restore",
        )
    if confirm_action_id != action_id or confirmation is None or actor is None:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore",
            request_id=request,
            error="exact action, receipt confirmation and actor are required",
            code="invalid_request",
            kind="neocortex_curation_restore",
        )
    if backend is None:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore",
            request_id=request,
            error="no injected restore backend was supplied",
            code="backend_unavailable",
            kind="neocortex_curation_restore",
        )
    _state, database_path = _paths(state_directory, database)
    try:
        with FrameworkState(database_path, existing_only=True) as state:
            result = restore_curation_action(
                database_path,
                action_id,
                backend=backend,
                confirmation=confirmation,
                actor=actor,
                state=state,
            )
    except (ValueError, sqlite3.DatabaseError, OSError, RuntimeError) as exc:
        return _error(
            schema=CURATION_RESTORE_API_SCHEMA,
            operation="curation-restore",
            request_id=request,
            error=exc,
            code="unavailable",
            kind="neocortex_curation_restore",
        )
    return {
        "schema": CURATION_RESTORE_API_SCHEMA,
        "schema_version": 1,
        "kind": "neocortex_curation_restore",
        "operation": "curation-restore",
        "request_id": request,
        "status": result.status,
        "read_only": False,
        "effects": {
            "state": "file_actions",
            "corpus": "restore_or_recovery",
            "external": "none",
        },
        "result": {
            "action_id": result.action_id,
            "status": result.status,
            "reason": result.reason,
            "detail": result.detail,
            "idempotent": result.idempotent,
            "receipt_json": result.receipt_json,
        },
        "error": None,
        "exit_code": 0 if result.status in {"restored", "already_restored"} else 3,
    }


__all__ = (
    "CURATION_RECOVERY_STATUS_API_SCHEMA",
    "CURATION_RESTORE_API_SCHEMA",
    "CurationRecoveryStatusOutput",
    "curation_recovery_status_payload",
    "curation_restore_payload",
    "curation_restore_preview_payload",
)
