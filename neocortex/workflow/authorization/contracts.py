"""Immutable, bounded AuthorizationGrant contracts.

The grant is a human authorization record, not an effect receipt.  Its scope
is bound to one curation plan snapshot and an exact ordered set of ReviewTask
heads; a later apply implementation must revalidate the source again before
creating a ``file_actions`` attempt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


AUTHORIZATION_GRANT_SCHEMA_VERSION = 1
AUTHORIZATION_GRANT_SCHEMA = "neocortex.authorization-grant/v1"
AUTHORIZATION_SCOPE = "personal"
AUTHORIZATION_TASK_TYPE = "curation-review"
AUTHORIZATION_SELECTOR_SIGNATURE = "curation-plan-v1"
AUTHORIZATION_BACKEND = "linux"
AUTHORIZATION_ACTIONS = frozenset({"trash", "move", "rename"})
MAX_AUTHORIZATION_ITEMS = 100
MAX_AUTHORIZATION_IDENTIFIER_CHARS = 512
MAX_AUTHORIZATION_ROOT_CHARS = 4_096
MAX_AUTHORIZATION_JSON_BYTES = 65_536


def _text(label: str, value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > limit:
        raise ValueError(f"{label} must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _digest(label: str, value: object) -> str:
    text = _text(label, value, 71)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{label} must be sha256:<64 lowercase hex characters>")
    return text


def _source_fingerprint(value: object) -> str:
    text = _text("source_snapshot_fingerprint", value, 102)
    prefix = "review-task-source-snapshot-v1:sha256:"
    if (
        len(text) != 102
        or not text.startswith(prefix)
        or any(character not in "0123456789abcdef" for character in text[len(prefix) :])
    ):
        raise ValueError("source_snapshot_fingerprint is invalid")
    return text


def _positive_integer(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


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
        raise ValueError("authorization grant is not canonical JSON") from exc


def _bounded_json(value: object) -> str:
    payload = _canonical_json(value)
    if len(payload.encode("utf-8")) > MAX_AUTHORIZATION_JSON_BYTES:
        raise ValueError("authorization grant JSON exceeds its byte limit")
    return payload


def _identifiers(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    if not 1 <= len(values) <= MAX_AUTHORIZATION_ITEMS:
        raise ValueError(f"{label} must contain between 1 and {MAX_AUTHORIZATION_ITEMS} items")
    result = tuple(_text(f"{label} item", value, MAX_AUTHORIZATION_IDENTIFIER_CHARS) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    grant_id: str
    authorization_key: str
    scope: str
    task_type: str
    selector_signature: str
    plan_digest: str
    snapshot_id: str
    source_snapshot_fingerprint: str
    root: str
    actor: str
    action: str
    backend: str
    item_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    max_actions: int
    max_bytes: int
    issued_ns: int
    expires_ns: int

    def __post_init__(self) -> None:
        _text("grant_id", self.grant_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _text("authorization_key", self.authorization_key, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        if self.scope != AUTHORIZATION_SCOPE:
            raise ValueError("authorization scope is unsupported")
        if self.task_type != AUTHORIZATION_TASK_TYPE:
            raise ValueError("authorization task_type is unsupported")
        if self.selector_signature != AUTHORIZATION_SELECTOR_SIGNATURE:
            raise ValueError("authorization selector_signature is unsupported")
        _digest("plan_digest", self.plan_digest)
        _digest("snapshot_id", self.snapshot_id)
        _source_fingerprint(self.source_snapshot_fingerprint)
        root = _text("root", self.root, MAX_AUTHORIZATION_ROOT_CHARS)
        if not root.startswith("/"):
            raise ValueError("authorization root must be absolute")
        _text("actor", self.actor, 256)
        if self.action not in AUTHORIZATION_ACTIONS:
            raise ValueError("authorization action is unsupported")
        if self.backend != AUTHORIZATION_BACKEND:
            raise ValueError("authorization backend is unsupported")
        item_ids = _identifiers("item_ids", self.item_ids)
        task_ids = _identifiers("task_ids", self.task_ids)
        if len(item_ids) != len(task_ids):
            raise ValueError("item_ids and task_ids must have equal cardinality")
        object.__setattr__(self, "item_ids", item_ids)
        object.__setattr__(self, "task_ids", task_ids)
        max_actions = _positive_integer("max_actions", self.max_actions)
        if max_actions != len(item_ids):
            raise ValueError("max_actions must equal the authorized item count")
        object.__setattr__(self, "max_actions", max_actions)
        object.__setattr__(self, "max_bytes", _nonnegative_integer("max_bytes", self.max_bytes))
        issued_ns = _positive_integer("issued_ns", self.issued_ns)
        expires_ns = _positive_integer("expires_ns", self.expires_ns)
        if expires_ns <= issued_ns:
            raise ValueError("expires_ns must be later than issued_ns")
        object.__setattr__(self, "issued_ns", issued_ns)
        object.__setattr__(self, "expires_ns", expires_ns)
        self.to_json()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": AUTHORIZATION_GRANT_SCHEMA_VERSION,
            "schema": AUTHORIZATION_GRANT_SCHEMA,
            "kind": "authorization_grant",
            "grant_id": self.grant_id,
            "authorization_key": self.authorization_key,
            "scope": self.scope,
            "task_type": self.task_type,
            "selector_signature": self.selector_signature,
            "plan_digest": self.plan_digest,
            "snapshot_id": self.snapshot_id,
            "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
            "root": self.root,
            "actor": self.actor,
            "action": self.action,
            "backend": self.backend,
            "item_ids": list(self.item_ids),
            "task_ids": list(self.task_ids),
            "max_actions": self.max_actions,
            "max_bytes": self.max_bytes,
            "issued_ns": self.issued_ns,
            "expires_ns": self.expires_ns,
        }

    def to_json(self) -> str:
        return _bounded_json(self.to_dict())

    def replay_identity(self) -> dict[str, object]:
        payload = self.to_dict()
        payload.pop("issued_ns", None)
        return payload

    def replay_equivalent(self, other: AuthorizationGrant) -> bool:
        return self.replay_identity() == other.replay_identity()


__all__ = (
    "AUTHORIZATION_ACTIONS",
    "AUTHORIZATION_BACKEND",
    "AUTHORIZATION_GRANT_SCHEMA",
    "AUTHORIZATION_GRANT_SCHEMA_VERSION",
    "AUTHORIZATION_SCOPE",
    "AUTHORIZATION_SELECTOR_SIGNATURE",
    "AUTHORIZATION_TASK_TYPE",
    "MAX_AUTHORIZATION_ITEMS",
    "AuthorizationGrant",
)
