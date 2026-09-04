"""Append-only persistence for AuthorizationGrant inside Framework SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from neocortex.persistence.framework_authorization_schema import (
    AUTHORIZATION_GRANTS_TABLE,
    create_authorization_extension,
    validate_authorization_extension,
)
from neocortex.persistence.framework_connection import connect_existing_framework
from neocortex.persistence.framework_schema import (
    SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION,
    validate_framework_schema_v22,
)
from neocortex.workflow.authorization.contracts import AuthorizationGrant


class AuthorizationGrantRepositoryError(RuntimeError):
    """The Framework AuthorizationGrant extension is absent or contradictory."""


class AuthorizationGrantConflict(AuthorizationGrantRepositoryError):
    """An idempotency key already names a different immutable grant."""


@dataclass(frozen=True, slots=True)
class AuthorizationGrantResult:
    grant: AuthorizationGrant
    idempotent: bool


_COLUMNS = (
    "grant_id,authorization_key,scope,task_type,selector_signature,plan_digest,"
    "snapshot_id,source_snapshot_fingerprint,root,actor,action,backend,item_ids_json,"
    "task_ids_json,max_actions,max_bytes,issued_ns,expires_ns,receipt_json,"
    "extension_schema_version"
)


def _require_framework(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1 or str(rows[0][0]) != str(FRAMEWORK_SCHEMA_VERSION):
        observed = None if not rows else str(rows[0][0])
        raise AuthorizationGrantRepositoryError(
            f"AuthorizationGrant requires Framework schema {FRAMEWORK_SCHEMA_VERSION}; observed {observed!r}"
        )
    try:
        validate_framework_schema_v22(connection)
    except RuntimeError as exc:
        raise AuthorizationGrantRepositoryError(
            "Framework schema is incompatible with AuthorizationGrant"
        ) from exc


def _json_array(value: object, label: str) -> tuple[str, ...]:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise AuthorizationGrantRepositoryError(f"{label} is not a string array")
    return tuple(payload)


def _grant_from_row(row: sqlite3.Row) -> AuthorizationGrant:
    try:
        grant = AuthorizationGrant(
            grant_id=str(row["grant_id"]),
            authorization_key=str(row["authorization_key"]),
            scope=str(row["scope"]),
            task_type=str(row["task_type"]),
            selector_signature=str(row["selector_signature"]),
            plan_digest=str(row["plan_digest"]),
            snapshot_id=str(row["snapshot_id"]),
            source_snapshot_fingerprint=str(row["source_snapshot_fingerprint"]),
            root=str(row["root"]),
            actor=str(row["actor"]),
            action=str(row["action"]),
            backend=str(row["backend"]),
            item_ids=_json_array(row["item_ids_json"], "item_ids_json"),
            task_ids=_json_array(row["task_ids_json"], "task_ids_json"),
            max_actions=int(row["max_actions"]),
            max_bytes=int(row["max_bytes"]),
            issued_ns=int(row["issued_ns"]),
            expires_ns=int(row["expires_ns"]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuthorizationGrantRepositoryError("persisted AuthorizationGrant is invalid") from exc
    if int(row["extension_schema_version"]) != 1 or str(row["receipt_json"]) != grant.to_json():
        raise AuthorizationGrantRepositoryError("persisted AuthorizationGrant receipt is not canonical")
    return grant


def _select_existing(connection: sqlite3.Connection, grant: AuthorizationGrant) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT {_COLUMNS} FROM {AUTHORIZATION_GRANTS_TABLE} "
        "WHERE grant_id=? OR authorization_key=?",
        (grant.grant_id, grant.authorization_key),
    ).fetchall()


def issue_authorization_grant(
    database: str | Path,
    grant: AuthorizationGrant,
    *,
    timeout_seconds: float = 60.0,
) -> AuthorizationGrantResult:
    """Persist one immutable grant, creating only its explicit extension."""

    if not isinstance(grant, AuthorizationGrant):
        raise TypeError("grant must be an AuthorizationGrant")
    connection = connect_existing_framework(Path(database), readonly=False, timeout_seconds=timeout_seconds)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_framework(connection)
            create_authorization_extension(connection)
            validate_authorization_extension(connection)
            rows = _select_existing(connection, grant)
            if len(rows) > 1:
                raise AuthorizationGrantRepositoryError("AuthorizationGrant id/key is ambiguous")
            if rows:
                existing = _grant_from_row(rows[0])
                if not existing.replay_equivalent(grant):
                    raise AuthorizationGrantConflict("authorization idempotency key changed payload")
                connection.commit()
                return AuthorizationGrantResult(existing, True)
            connection.execute(
                f"""INSERT INTO {AUTHORIZATION_GRANTS_TABLE}(
                {_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.grant_id,
                    grant.authorization_key,
                    grant.scope,
                    grant.task_type,
                    grant.selector_signature,
                    grant.plan_digest,
                    grant.snapshot_id,
                    grant.source_snapshot_fingerprint,
                    grant.root,
                    grant.actor,
                    grant.action,
                    grant.backend,
                    json.dumps(list(grant.item_ids), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(grant.task_ids), ensure_ascii=False, separators=(",", ":")),
                    grant.max_actions,
                    grant.max_bytes,
                    grant.issued_ns,
                    grant.expires_ns,
                    grant.to_json(),
                    1,
                ),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    except sqlite3.IntegrityError as exc:
        raise AuthorizationGrantConflict("AuthorizationGrant uniqueness conflict") from exc
    finally:
        connection.close()
    return AuthorizationGrantResult(grant, False)


def read_authorization_grant(
    database: str | Path,
    *,
    grant_id: str | None = None,
    authorization_key: str | None = None,
) -> AuthorizationGrant | None:
    """Read one immutable grant without creating extension state."""

    if (grant_id is None) == (authorization_key is None):
        raise ValueError("provide exactly one grant_id or authorization_key")
    connection = connect_existing_framework(Path(database), readonly=True)
    try:
        connection.execute("BEGIN")
        try:
            _require_framework(connection)
            if not _extension_exists(connection):
                connection.commit()
                return None
            validate_authorization_extension(connection)
            column = "grant_id" if grant_id is not None else "authorization_key"
            value = grant_id if grant_id is not None else authorization_key
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM {AUTHORIZATION_GRANTS_TABLE} WHERE {column}=?",
                (value,),
            ).fetchall()
            if len(rows) > 1:
                raise AuthorizationGrantRepositoryError("AuthorizationGrant lookup is ambiguous")
            result = None if not rows else _grant_from_row(rows[0])
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
    finally:
        connection.close()


def _extension_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=? LIMIT 1",
        (AUTHORIZATION_GRANTS_TABLE,),
    ).fetchone()
    return row is not None and str(row[0]) == "table"


__all__ = (
    "AuthorizationGrantConflict",
    "AuthorizationGrantRepositoryError",
    "AuthorizationGrantResult",
    "issue_authorization_grant",
    "read_authorization_grant",
)
