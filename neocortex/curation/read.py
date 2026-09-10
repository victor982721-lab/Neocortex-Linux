"""Sidecar-safe, read-only projections of curation authorization state.

This module is intentionally below the curation domain rather than the public
API/agent server.  The desktop can inspect grants, physical attempts,
receipts, and durable recovery observations without importing a writer or
inventing a mutation control.  It never probes the corpus and never creates a
missing Framework owner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from neocortex.persistence.framework_authorization_schema import (
    AUTHORIZATION_GRANTS_TABLE,
    authorization_extension_present,
    validate_authorization_extension,
)
from neocortex.persistence.framework_schema import (
    SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION,
    validate_framework_schema_v22,
)
from neocortex.persistence.sqlite_immutable import (
    SQLiteReadSession,
    preferred_sqlite_read_mode,
)
from neocortex.workflow.authorization.repository import _COLUMNS as AUTHORIZATION_COLUMNS
from neocortex.workflow.authorization.repository import _grant_from_row


CURATION_READ_SCHEMA = "neocortex.curation-read/v1"
CURATION_READ_MAX_ITEMS = 100


def _bounded_text(value: object, *, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "…[truncated]"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_object(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _intent_metadata(value: object) -> tuple[str | None, str | None]:
    payload = _json_object(value)
    if payload is None:
        return None, None
    grant_id = payload.get("grant_id")
    effect = payload.get("effect")
    effect_id = effect.get("effect_id") if isinstance(effect, dict) else None
    return (
        grant_id if isinstance(grant_id, str) else None,
        effect_id if isinstance(effect_id, str) else None,
    )


def _receipt_metadata(value: object) -> tuple[str | None, str, str | None, str | None]:
    if value is None:
        return None, "absent", None, None
    raw = str(value)
    payload = _json_object(raw)
    if payload is None:
        return _digest(raw), "invalid", None, None
    receipt_type = payload.get("receipt_type")
    operation = payload.get("operation")
    return (
        _digest(raw),
        "object",
        receipt_type if isinstance(receipt_type, str) else None,
        operation if isinstance(operation, str) else None,
    )


@dataclass(frozen=True, slots=True)
class CurationGrantView:
    """Bounded grant metadata; the actor label is not treated as a principal."""

    grant_id: str
    plan_digest: str
    root: str
    actor: str
    action: str
    backend: str
    item_count: int
    effect_count: int
    max_actions: int
    max_bytes: int
    issued_ns: int
    expires_ns: int
    receipt_digest: str
    receipt_state: Literal["canonical", "invalid"]
    receipt_effects_digest: str | None
    review_heads_digest: str | None
    principal_state: Literal["not_authenticated"] = "not_authenticated"

    def to_dict(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "plan_digest": self.plan_digest,
            "root": self.root,
            "actor": self.actor,
            "action": self.action,
            "backend": self.backend,
            "item_count": self.item_count,
            "effect_count": self.effect_count,
            "max_actions": self.max_actions,
            "max_bytes": self.max_bytes,
            "issued_ns": self.issued_ns,
            "expires_ns": self.expires_ns,
            "receipt_digest": self.receipt_digest,
            "receipt_state": self.receipt_state,
            "receipt_effects_digest": self.receipt_effects_digest,
            "review_heads_digest": self.review_heads_digest,
            "principal_state": self.principal_state,
        }


@dataclass(frozen=True, slots=True)
class CurationAttemptView:
    """One durable file-action attempt and its receipt metadata."""

    action_id: int
    run_id: int
    action_type: str
    status: str
    source_path: str
    target_path: str | None
    grant_id: str | None
    effect_id: str | None
    started_ns: int
    completed_ns: int | None
    detail: str | None
    expected_identity_present: bool
    receipt_digest: str | None
    receipt_state: str
    receipt_type: str | None
    receipt_operation: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "action_type": self.action_type,
            "status": self.status,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "grant_id": self.grant_id,
            "effect_id": self.effect_id,
            "started_ns": self.started_ns,
            "completed_ns": self.completed_ns,
            "detail": self.detail,
            "expected_identity_present": self.expected_identity_present,
            "receipt_digest": self.receipt_digest,
            "receipt_state": self.receipt_state,
            "receipt_type": self.receipt_type,
            "receipt_operation": self.receipt_operation,
        }


@dataclass(frozen=True, slots=True)
class CurationRecoveryView:
    """Latest append-only recovery observation for one uncertain attempt."""

    action_id: int
    status: str
    reconciliation_event_id: int | None
    classification: str | None
    recommendation: str | None
    detail: str | None
    observed_ns: int | None
    recorded_ns: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "status": self.status,
            "reconciliation_event_id": self.reconciliation_event_id,
            "classification": self.classification,
            "recommendation": self.recommendation,
            "detail": self.detail,
            "observed_ns": self.observed_ns,
            "recorded_ns": self.recorded_ns,
        }


@dataclass(frozen=True, slots=True)
class CurationReadSnapshot:
    """Safe desktop projection with no corpus or Framework writes."""

    status: Literal["complete", "unavailable"]
    grants: tuple[CurationGrantView, ...] = ()
    attempts: tuple[CurationAttemptView, ...] = ()
    recovery: tuple[CurationRecoveryView, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def read_only(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CURATION_READ_SCHEMA,
            "schema_version": 1,
            "kind": "neocortex_curation_read",
            "status": self.status,
            "read_only": True,
            "effects": {"state": "none", "corpus": "none", "external": "none"},
            "grants": [grant.to_dict() for grant in self.grants],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "recovery": [item.to_dict() for item in self.recovery],
            "error": (
                None
                if self.error_code is None
                else {"code": self.error_code, "detail": self.error_detail}
            ),
        }


def _unavailable(code: str, detail: object) -> CurationReadSnapshot:
    return CurationReadSnapshot(
        status="unavailable",
        error_code=code,
        error_detail=_bounded_text(detail, limit=800),
    )


def _validate_framework_read(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1 or str(rows[0][0]) != str(FRAMEWORK_SCHEMA_VERSION):
        observed = None if not rows else str(rows[0][0])
        raise sqlite3.DatabaseError(
            "Framework schema is incompatible: "
            f"expected {FRAMEWORK_SCHEMA_VERSION}, observed {observed!r}"
        )
    validate_framework_schema_v22(connection)


def _read_grants(connection: sqlite3.Connection, limit: int) -> tuple[CurationGrantView, ...]:
    if not authorization_extension_present(connection):
        return ()
    validate_authorization_extension(connection)
    rows = connection.execute(
        f"SELECT {AUTHORIZATION_COLUMNS} FROM {AUTHORIZATION_GRANTS_TABLE} "
        "ORDER BY issued_ns DESC, grant_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result: list[CurationGrantView] = []
    for row in rows:
        grant = _grant_from_row(row)
        receipt_json = str(row["receipt_json"])
        receipt = _json_object(receipt_json)
        result.append(
            CurationGrantView(
                grant_id=grant.grant_id,
                plan_digest=grant.plan_digest,
                root=_bounded_text(grant.root, limit=4_096),
                actor=_bounded_text(grant.actor, limit=256),
                action=grant.action,
                backend=grant.backend,
                item_count=len(grant.item_ids),
                effect_count=len(grant.authorized_effects or ()),
                max_actions=grant.max_actions,
                max_bytes=grant.max_bytes,
                issued_ns=grant.issued_ns,
                expires_ns=grant.expires_ns,
                receipt_digest=_digest(receipt_json),
                receipt_state="canonical" if receipt is not None else "invalid",
                receipt_effects_digest=grant.authorized_effects_digest,
                review_heads_digest=grant.review_task_heads_digest,
            )
        )
    return tuple(result)


def _read_attempt_rows(
    connection: sqlite3.Connection,
    limit: int,
) -> tuple[tuple[CurationAttemptView, ...], tuple[CurationRecoveryView, ...]]:
    rows = connection.execute(
        """SELECT f.action_id,f.run_id,f.action_type,f.status,f.source_path,
        f.target_path,f.evidence,f.started_ns,f.completed_ns,f.detail,
        f.expected_identity_json,f.effect_receipt_json,
        r.reconciliation_event_id,r.classification,r.recommendation,
        r.detail AS reconciliation_detail,r.observed_ns,r.recorded_ns
        FROM file_actions AS f
        LEFT JOIN file_action_reconciliation_events AS r
          ON r.reconciliation_event_id=(
            SELECT latest.reconciliation_event_id
            FROM file_action_reconciliation_events AS latest
            WHERE latest.action_id=f.action_id
            ORDER BY latest.reconciliation_event_id DESC LIMIT 1
          )
        ORDER BY f.action_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    attempts: list[CurationAttemptView] = []
    recovery: list[CurationRecoveryView] = []
    for row in rows:
        grant_id, effect_id = _intent_metadata(row["evidence"])
        receipt_digest, receipt_state, receipt_type, receipt_operation = _receipt_metadata(
            row["effect_receipt_json"]
        )
        attempt = CurationAttemptView(
            action_id=int(row["action_id"]),
            run_id=int(row["run_id"]),
            action_type=_bounded_text(row["action_type"], limit=128),
            status=_bounded_text(row["status"], limit=64),
            source_path=_bounded_text(row["source_path"], limit=4_096),
            target_path=(
                None
                if row["target_path"] is None
                else _bounded_text(row["target_path"], limit=4_096)
            ),
            grant_id=grant_id,
            effect_id=effect_id,
            started_ns=int(row["started_ns"]),
            completed_ns=None if row["completed_ns"] is None else int(row["completed_ns"]),
            detail=None if row["detail"] is None else _bounded_text(row["detail"], limit=800),
            expected_identity_present=row["expected_identity_json"] is not None,
            receipt_digest=receipt_digest,
            receipt_state=receipt_state,
            receipt_type=receipt_type,
            receipt_operation=receipt_operation,
        )
        attempts.append(attempt)
        if attempt.status in {"applying", "recovery_required"}:
            recovery.append(
                CurationRecoveryView(
                    action_id=attempt.action_id,
                    status=attempt.status,
                    reconciliation_event_id=(
                        None
                        if row["reconciliation_event_id"] is None
                        else int(row["reconciliation_event_id"])
                    ),
                    classification=(
                        None
                        if row["classification"] is None
                        else _bounded_text(row["classification"], limit=128)
                    ),
                    recommendation=(
                        None
                        if row["recommendation"] is None
                        else _bounded_text(row["recommendation"], limit=256)
                    ),
                    detail=(
                        None
                        if row["reconciliation_detail"] is None
                        else _bounded_text(row["reconciliation_detail"], limit=800)
                    ),
                    observed_ns=(
                        None if row["observed_ns"] is None else int(row["observed_ns"])
                    ),
                    recorded_ns=(
                        None if row["recorded_ns"] is None else int(row["recorded_ns"])
                    ),
                )
            )
    return tuple(attempts), tuple(recovery)


def read_curation_snapshot(
    database: str | Path,
    *,
    limit: int = CURATION_READ_MAX_ITEMS,
) -> CurationReadSnapshot:
    """Read grants, attempts, receipts and recovery without creating state."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= CURATION_READ_MAX_ITEMS
    ):
        raise ValueError(
            f"curation read limit must be between 1 and {CURATION_READ_MAX_ITEMS}"
        )
    path = Path(database)
    if not path.is_file():
        return _unavailable("state_absent", f"Framework owner does not exist: {path}")
    try:
        with SQLiteReadSession(
            path,
            mode=preferred_sqlite_read_mode(path),
            timeout_seconds=30.0,
        ) as connection:
            connection.row_factory = sqlite3.Row
            _validate_framework_read(connection)
            grants = _read_grants(connection, limit)
            attempts, recovery = _read_attempt_rows(connection, limit)
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        return _unavailable("state_unavailable", exc)
    return CurationReadSnapshot(
        status="complete",
        grants=grants,
        attempts=attempts,
        recovery=recovery,
    )


__all__ = (
    "CURATION_READ_MAX_ITEMS",
    "CURATION_READ_SCHEMA",
    "CurationAttemptView",
    "CurationGrantView",
    "CurationReadSnapshot",
    "CurationRecoveryView",
    "read_curation_snapshot",
)
