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
    validate_framework_schema,
)
from neocortex.workflow.authorization.contracts import (
    AUTHORIZATION_EFFECTS_SCHEMA_VERSION,
    AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION,
    AuthorizationGrant,
    AuthorizationEffect,
    AuthorizationRootSnapshot,
    AuthorizationReviewTaskHead,
    REVIEW_TASK_HEADS_SCHEMA_VERSION,
    _snapshot_from_dict,
    review_task_heads_digest,
    authorized_effects_digest,
)
from neocortex.workflow.review.review_task_contracts import CanonicalJsonObject


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


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant JSON is not canonical") from exc


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
        validate_framework_schema(connection)
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


def _head_from_payload(value: object) -> AuthorizationReviewTaskHead:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AuthorizationGrantRepositoryError("review_task_heads contains a non-object")
    decision = value.get("decision")
    text_fields = (
        "item_id",
        "logical_key",
        "task_id",
        "state",
        "event_id",
        "source_snapshot_fingerprint",
        "source_input_fingerprint",
        "selector_signature",
    )
    if any(not isinstance(value.get(field), str) for field in text_fields):
        raise AuthorizationGrantRepositoryError("review_task_heads contains non-text identity fields")
    task_version = value.get("task_version")
    if isinstance(task_version, bool) or not isinstance(task_version, int):
        raise AuthorizationGrantRepositoryError("review_task_heads task_version is not an integer")
    head_digest = value.get("head_digest")
    if head_digest is not None and not isinstance(head_digest, str):
        raise AuthorizationGrantRepositoryError("review_task_heads head_digest is not text")
    try:
        head = AuthorizationReviewTaskHead(
            item_id=value["item_id"],
            logical_key=value["logical_key"],
            task_id=value["task_id"],
            task_version=task_version,
            state=value["state"],
            event_id=value["event_id"],
            source_snapshot_fingerprint=value["source_snapshot_fingerprint"],
            source_input_fingerprint=value["source_input_fingerprint"],
            selector_signature=value["selector_signature"],
            decision=(
                None
                if decision is None
                else CanonicalJsonObject.from_mapping(decision)
            ),
            head_digest=head_digest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("review_task_heads contains an invalid head") from exc
    if value != head.to_dict():
        raise AuthorizationGrantRepositoryError("review_task_heads contains a non-canonical head")
    return head


def _heads_from_receipt(
    receipt_json: str,
) -> tuple[tuple[AuthorizationReviewTaskHead, ...] | None, str | None]:
    try:
        payload = json.loads(receipt_json)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is invalid JSON") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is not an object")
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is not canonical") from exc
    if canonical != receipt_json:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is not canonical")
    present = {
        key
        for key in (
            "review_task_heads_schema_version",
            "review_task_heads",
            "review_task_heads_digest",
        )
        if key in payload
    }
    if not present:
        # Grants written before the head manifest remain readable as legacy
        # facts, but an effect consumer must reject them as unbound.
        return None, None
    if present != {
        "review_task_heads_schema_version",
        "review_task_heads",
        "review_task_heads_digest",
    }:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant head manifest is incomplete")
    if payload["review_task_heads_schema_version"] != REVIEW_TASK_HEADS_SCHEMA_VERSION:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant head manifest version is unsupported")
    raw_heads = payload["review_task_heads"]
    if not isinstance(raw_heads, list):
        raise AuthorizationGrantRepositoryError("AuthorizationGrant heads are not an array")
    try:
        heads = tuple(_head_from_payload(item) for item in raw_heads)
        digest = payload["review_task_heads_digest"]
        if not isinstance(digest, str):
            raise AuthorizationGrantRepositoryError(
                "AuthorizationGrant head manifest digest is not text"
            )
        expected = review_task_heads_digest(heads)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant head manifest is invalid") from exc
    if digest != expected:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant head manifest digest is invalid")
    return heads, digest


def _root_snapshot_from_receipt(value: object) -> AuthorizationRootSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AuthorizationGrantRepositoryError("AuthorizationGrant root snapshot is invalid")
    try:
        root = str(value["root"])
        volume_id = int(str(value["volume_id"]), 16)
        file_id = int(str(value["file_id"]), 16)
        birthtime_ns = value["birthtime_ns"]
        result = AuthorizationRootSnapshot(root, volume_id, file_id, birthtime_ns)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant root snapshot is malformed") from exc
    if result.to_dict() != value:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant root snapshot is not canonical")
    return result


def _source_heads_from_receipt(
    payload: dict[str, object],
) -> tuple[tuple[CanonicalJsonObject, ...] | None, str | None]:
    present = {
        key
        for key in (
            "source_heads_schema_version",
            "source_heads",
            "source_heads_digest",
        )
        if key in payload
    }
    if not present:
        return None, None
    if present != {
        "source_heads_schema_version",
        "source_heads",
        "source_heads_digest",
    }:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant source-head manifest is incomplete")
    if payload["source_heads_schema_version"] != AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant source-head manifest version is unsupported")
    raw_heads = payload["source_heads"]
    if not isinstance(raw_heads, list) or not raw_heads:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant source heads are invalid")
    try:
        heads = tuple(CanonicalJsonObject.from_mapping(item) for item in raw_heads)
        digest = payload["source_heads_digest"]
        if not isinstance(digest, str):
            raise AuthorizationGrantRepositoryError("AuthorizationGrant source-head digest is not text")
        from neocortex.workflow.authorization.contracts import _source_heads_digest

        expected = _source_heads_digest(heads)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant source-head manifest is invalid") from exc
    if digest != expected or [head.to_dict() for head in heads] != raw_heads:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant source-head manifest digest is invalid")
    return heads, digest


def _effect_from_payload(value: object) -> AuthorizationEffect:
    if not isinstance(value, dict):
        raise AuthorizationGrantRepositoryError("authorized_effects contains a non-object")
    try:
        effect = AuthorizationEffect(
            effect_id=str(value["effect_id"]),
            item_id=str(value["item_id"]),
            task_id=str(value["task_id"]),
            ordinal=value["ordinal"],
            action=str(value["action"]),
            kind=str(value["kind"]),
            source=_snapshot_from_dict(value["source"], label="effect.source"),
            source_digest=str(value["source_digest"]),
            target_path=None if value.get("target_path") is None else str(value["target_path"]),
            keeper=(
                None
                if value.get("keeper") is None
                else _snapshot_from_dict(value["keeper"], label="effect.keeper")
            ),
            keeper_digest=(
                None if value.get("keeper_digest") is None else str(value["keeper_digest"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("authorized_effects contains an invalid effect") from exc
    if value != effect.to_dict():
        raise AuthorizationGrantRepositoryError("authorized_effects contains a non-canonical effect")
    return effect


def _effects_from_receipt(
    payload: dict[str, object],
) -> tuple[tuple[AuthorizationEffect, ...] | None, str | None]:
    present = {
        key
        for key in (
            "authorized_effects_schema_version",
            "authorized_effects",
            "authorized_effects_digest",
        )
        if key in payload
    }
    if not present:
        return None, None
    if present != {
        "authorized_effects_schema_version",
        "authorized_effects",
        "authorized_effects_digest",
    }:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant effect manifest is incomplete")
    if payload["authorized_effects_schema_version"] != AUTHORIZATION_EFFECTS_SCHEMA_VERSION:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant effect manifest version is unsupported")
    raw_effects = payload["authorized_effects"]
    if not isinstance(raw_effects, list) or not raw_effects:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant effects are invalid")
    try:
        effects = tuple(_effect_from_payload(item) for item in raw_effects)
        digest = payload["authorized_effects_digest"]
        if not isinstance(digest, str):
            raise AuthorizationGrantRepositoryError("AuthorizationGrant effect digest is not text")
        expected = authorized_effects_digest(effects)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant effect manifest is invalid") from exc
    if digest != expected or [effect.to_dict() for effect in effects] != raw_effects:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant effect manifest digest is invalid")
    return effects, digest


def _grant_from_row(row: sqlite3.Row) -> AuthorizationGrant:
    receipt_json = str(row["receipt_json"])
    review_task_heads, review_task_heads_digest = _heads_from_receipt(receipt_json)
    try:
        receipt_payload = json.loads(receipt_json)
    except (TypeError, ValueError) as exc:
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is invalid JSON") from exc
    if not isinstance(receipt_payload, dict):
        raise AuthorizationGrantRepositoryError("AuthorizationGrant receipt is not an object")
    root_snapshot = _root_snapshot_from_receipt(receipt_payload.get("root_snapshot"))
    source_heads, source_heads_digest = _source_heads_from_receipt(receipt_payload)
    authorized_effects, authorized_effects_digest_value = _effects_from_receipt(receipt_payload)
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
            review_task_heads=review_task_heads,
            review_task_heads_digest=review_task_heads_digest,
            root_snapshot=root_snapshot,
            source_heads=source_heads,
            source_heads_digest=source_heads_digest,
            authorized_effects=authorized_effects,
            authorized_effects_digest=authorized_effects_digest_value,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuthorizationGrantRepositoryError("persisted AuthorizationGrant is invalid") from exc
    if int(row["extension_schema_version"]) != 1 or receipt_json != grant.to_json():
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
