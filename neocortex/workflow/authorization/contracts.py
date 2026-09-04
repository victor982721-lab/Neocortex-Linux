"""Immutable, bounded AuthorizationGrant contracts.

The grant is a human authorization record, not an effect receipt.  Its scope
is bound to one curation plan snapshot and an exact ordered set of ReviewTask
heads; a later apply implementation must revalidate the source again before
creating a ``file_actions`` attempt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from neocortex.deduplication.domain.models import FileSnapshot

from neocortex.workflow.review.review_task_contracts import CanonicalJsonObject


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
REVIEW_TASK_HEADS_SCHEMA_VERSION = 1
REVIEW_TASK_HEADS_SCHEMA = "neocortex.authorization-review-task-heads/v1"
REVIEW_TASK_HEAD_DIGEST_SCHEMA = "neocortex.authorization-review-task-head/v1"
AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION = 1
AUTHORIZATION_SOURCE_HEADS_SCHEMA = "neocortex.authorization-source-heads/v1"
AUTHORIZATION_EFFECTS_SCHEMA_VERSION = 1
AUTHORIZATION_EFFECTS_SCHEMA = "neocortex.authorization-effects/v1"
MAX_AUTHORIZATION_EFFECTS = 100
_FULL_DIGEST_PREFIX = "xxh3_128_full_v1:"


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


def _absolute_path(label: str, value: object, limit: int = MAX_AUTHORIZATION_ROOT_CHARS) -> str:
    text = _text(label, value, limit)
    if not text.startswith("/"):
        raise ValueError(f"{label} must be an absolute path")
    return text


def _snapshot_dict(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "birthtime_ns": snapshot.birthtime_ns,
        "file_id": f"{snapshot.file_id:x}",
        "mtime_ns": snapshot.mtime_ns,
        "path": snapshot.path,
        "size": snapshot.size,
        "volume_id": f"{snapshot.volume_id:x}",
    }


def _snapshot_from_dict(value: object, *, label: str) -> FileSnapshot:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        path = _absolute_path(f"{label}.path", value["path"])
        volume_id = int(str(value["volume_id"]), 16)
        file_id = int(str(value["file_id"]), 16)
        size = value["size"]
        mtime_ns = value["mtime_ns"]
        birthtime_ns = value["birthtime_ns"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is malformed") from exc
    if any(
        isinstance(number, bool) or not isinstance(number, int) or number < 0
        for number in (volume_id, file_id, size, mtime_ns)
    ):
        raise ValueError(f"{label} contains invalid non-negative fields")
    if isinstance(birthtime_ns, bool) or not isinstance(birthtime_ns, int) or birthtime_ns < -1:
        raise ValueError(f"{label}.birthtime_ns is invalid")
    snapshot = FileSnapshot(path, volume_id, file_id, size, mtime_ns, birthtime_ns)
    if _snapshot_dict(snapshot) != value:
        raise ValueError(f"{label} is not canonical")
    return snapshot


def _full_digest(label: str, value: object) -> str:
    text = _text(label, value, 64)
    suffix = text[len(_FULL_DIGEST_PREFIX) :]
    if (
        len(text) != len(_FULL_DIGEST_PREFIX) + 32
        or not text.startswith(_FULL_DIGEST_PREFIX)
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError(f"{label} must be {_FULL_DIGEST_PREFIX}<32 lowercase hex characters>")
    return text


def _head_digest_payload(head: "AuthorizationReviewTaskHead") -> dict[str, object]:
    return {
        "schema_version": REVIEW_TASK_HEADS_SCHEMA_VERSION,
        "schema": REVIEW_TASK_HEAD_DIGEST_SCHEMA,
        "item_id": head.item_id,
        "logical_key": head.logical_key,
        "task_id": head.task_id,
        "task_version": head.task_version,
        "state": head.state,
        "event_id": head.event_id,
        "source_snapshot_fingerprint": head.source_snapshot_fingerprint,
        "source_input_fingerprint": head.source_input_fingerprint,
        "selector_signature": head.selector_signature,
        "decision": None if head.decision is None else head.decision.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class AuthorizationReviewTaskHead:
    """Immutable ReviewTask head captured when a grant is issued.

    The manifest is deliberately a compact projection of the owner-local
    ``ReviewTaskVersionHead``.  Its digest is calculated over every field that
    a future effect consumer must revalidate, while the aggregate digest also
    binds the ordered item/task mapping in the parent grant receipt.
    """

    item_id: str
    logical_key: str
    task_id: str
    task_version: int
    state: str
    event_id: str
    source_snapshot_fingerprint: str
    source_input_fingerprint: str
    selector_signature: str
    decision: CanonicalJsonObject | None
    head_digest: str | None = None

    def __post_init__(self) -> None:
        _text("head.item_id", self.item_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _text("head.logical_key", self.logical_key, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _text("head.task_id", self.task_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        task_version = _positive_integer("head.task_version", self.task_version)
        object.__setattr__(self, "task_version", task_version)
        _text("head.state", self.state, 128)
        if self.state != "resolved":
            raise ValueError("authorization ReviewTask heads must be resolved")
        _text("head.event_id", self.event_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        source_snapshot_fingerprint = _source_fingerprint(self.source_snapshot_fingerprint)
        object.__setattr__(self, "source_snapshot_fingerprint", source_snapshot_fingerprint)
        _text(
            "head.source_input_fingerprint",
            self.source_input_fingerprint,
            1_024,
        )
        _text(
            "head.selector_signature",
            self.selector_signature,
            MAX_AUTHORIZATION_IDENTIFIER_CHARS,
        )
        if self.decision is not None and not isinstance(self.decision, CanonicalJsonObject):
            raise ValueError("head.decision must be a CanonicalJsonObject when present")
        if self.decision is None or self.decision.to_dict().get("decision") != "resolved":
            raise ValueError("resolved authorization ReviewTask heads require a resolved decision")
        if self.head_digest is not None:
            _digest("head_digest", self.head_digest)
        expected = _head_digest(self)
        if self.head_digest is not None and self.head_digest != expected:
            raise ValueError("head_digest does not match the immutable ReviewTask head")
        object.__setattr__(self, "head_digest", expected)

    @classmethod
    def create(
        cls,
        *,
        item_id: str,
        logical_key: str,
        task_id: str,
        task_version: int,
        state: str,
        event_id: str,
        source_snapshot_fingerprint: str,
        source_input_fingerprint: str,
        selector_signature: str,
        decision: CanonicalJsonObject | None,
    ) -> "AuthorizationReviewTaskHead":
        return cls(
            item_id=item_id,
            logical_key=logical_key,
            task_id=task_id,
            task_version=task_version,
            state=state,
            event_id=event_id,
            source_snapshot_fingerprint=source_snapshot_fingerprint,
            source_input_fingerprint=source_input_fingerprint,
            selector_signature=selector_signature,
            decision=decision,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_TASK_HEADS_SCHEMA_VERSION,
            "schema": REVIEW_TASK_HEADS_SCHEMA,
            "item_id": self.item_id,
            "logical_key": self.logical_key,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "state": self.state,
            "event_id": self.event_id,
            "source_snapshot_fingerprint": self.source_snapshot_fingerprint,
            "source_input_fingerprint": self.source_input_fingerprint,
            "selector_signature": self.selector_signature,
            "decision": None if self.decision is None else self.decision.to_dict(),
            "head_digest": self.head_digest,
        }


def _head_digest(head: AuthorizationReviewTaskHead) -> str:
    payload = _head_digest_payload(head)
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def review_task_head_digest(head: AuthorizationReviewTaskHead) -> str:
    """Return the canonical digest of one captured ReviewTask head."""

    if not isinstance(head, AuthorizationReviewTaskHead):
        raise TypeError("head must be an AuthorizationReviewTaskHead")
    return _head_digest(head)


def review_task_heads_digest(heads: tuple[AuthorizationReviewTaskHead, ...]) -> str:
    """Return the ordered, versioned digest for one grant head manifest."""

    if not isinstance(heads, tuple) or not 1 <= len(heads) <= MAX_AUTHORIZATION_ITEMS:
        raise ValueError("review_task_heads must be a non-empty immutable tuple")
    if any(not isinstance(head, AuthorizationReviewTaskHead) for head in heads):
        raise ValueError("review_task_heads must contain AuthorizationReviewTaskHead values")
    payload = {
        "schema_version": REVIEW_TASK_HEADS_SCHEMA_VERSION,
        "schema": REVIEW_TASK_HEADS_SCHEMA,
        "heads": [head.to_dict() for head in heads],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _source_heads_digest(heads: tuple[CanonicalJsonObject, ...]) -> str:
    payload = {
        "schema_version": AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION,
        "schema": AUTHORIZATION_SOURCE_HEADS_SCHEMA,
        "heads": [head.to_dict() for head in heads],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def authorized_effects_digest(effects: tuple["AuthorizationEffect", ...]) -> str:
    """Return the ordered digest of a grant's physical effect manifest."""

    if not isinstance(effects, tuple) or not effects:
        raise ValueError("authorized_effects must be a non-empty immutable tuple")
    if any(not isinstance(effect, AuthorizationEffect) for effect in effects):
        raise ValueError("authorized_effects must contain AuthorizationEffect values")
    payload = {
        "schema_version": AUTHORIZATION_EFFECTS_SCHEMA_VERSION,
        "schema": AUTHORIZATION_EFFECTS_SCHEMA,
        "effects": [effect.to_dict() for effect in effects],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizationRootSnapshot:
    """Physical identity of the corpus root captured with a consumable grant."""

    root: str
    volume_id: int
    file_id: int
    birthtime_ns: int

    def __post_init__(self) -> None:
        _absolute_path("root_snapshot.root", self.root)
        for label, value in (
            ("root_snapshot.volume_id", self.volume_id),
            ("root_snapshot.file_id", self.file_id),
        ):
            _nonnegative_integer(label, value)
        if isinstance(self.birthtime_ns, bool) or not isinstance(self.birthtime_ns, int):
            raise ValueError("root_snapshot.birthtime_ns must be an integer")
        if self.birthtime_ns < -1:
            raise ValueError("root_snapshot.birthtime_ns must be -1 or non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "birthtime_ns": self.birthtime_ns,
            "file_id": f"{self.file_id:x}",
            "root": self.root,
            "volume_id": f"{self.volume_id:x}",
        }


@dataclass(frozen=True, slots=True)
class AuthorizationEffect:
    """One physical effect expanded from one reviewed curation item."""

    effect_id: str
    item_id: str
    task_id: str
    ordinal: int
    action: str
    kind: str
    source: FileSnapshot
    source_digest: str
    target_path: str | None = None
    keeper: FileSnapshot | None = None
    keeper_digest: str | None = None

    def __post_init__(self) -> None:
        _text("effect_id", self.effect_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _text("effect.item_id", self.item_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _text("effect.task_id", self.task_id, MAX_AUTHORIZATION_IDENTIFIER_CHARS)
        _positive_integer("effect.ordinal", self.ordinal)
        if self.action not in AUTHORIZATION_ACTIONS:
            raise ValueError("effect action is unsupported")
        _text("effect.kind", self.kind, 128)
        if not isinstance(self.source, FileSnapshot):
            raise ValueError("effect source must be a FileSnapshot")
        _absolute_path("effect.source.path", self.source.path)
        for label, value in (
            ("effect.source.volume_id", self.source.volume_id),
            ("effect.source.file_id", self.source.file_id),
            ("effect.source.size", self.source.size),
            ("effect.source.mtime_ns", self.source.mtime_ns),
        ):
            _nonnegative_integer(label, value)
        if isinstance(self.source.birthtime_ns, bool) or not isinstance(self.source.birthtime_ns, int):
            raise ValueError("effect.source.birthtime_ns must be an integer")
        if self.source.birthtime_ns < -1:
            raise ValueError("effect.source.birthtime_ns must be -1 or non-negative")
        _full_digest("effect.source_digest", self.source_digest)
        if self.target_path is not None:
            _absolute_path("effect.target_path", self.target_path)
        if self.keeper is not None:
            if not isinstance(self.keeper, FileSnapshot):
                raise ValueError("effect keeper must be a FileSnapshot")
            _absolute_path("effect.keeper.path", self.keeper.path)
            if self.keeper.identity == self.source.identity:
                raise ValueError("effect keeper must not have the source identity")
            for label, value in (
                ("effect.keeper.volume_id", self.keeper.volume_id),
                ("effect.keeper.file_id", self.keeper.file_id),
                ("effect.keeper.size", self.keeper.size),
                ("effect.keeper.mtime_ns", self.keeper.mtime_ns),
            ):
                _nonnegative_integer(label, value)
            if isinstance(self.keeper.birthtime_ns, bool) or not isinstance(self.keeper.birthtime_ns, int):
                raise ValueError("effect.keeper.birthtime_ns must be an integer")
            if self.keeper.birthtime_ns < -1:
                raise ValueError("effect.keeper.birthtime_ns must be -1 or non-negative")
            if self.keeper_digest is None:
                raise ValueError("effect keeper_digest is required with keeper")
            _full_digest("effect.keeper_digest", self.keeper_digest)
        elif self.keeper_digest is not None:
            raise ValueError("effect keeper_digest requires keeper")
        if self.action == "trash":
            if self.target_path is not None:
                raise ValueError("trash effects cannot have a target path")
            if self.kind == "duplicate_group" and self.keeper is None:
                raise ValueError("duplicate trash effects require a keeper")
            if self.kind == "empty_file" and self.keeper is not None:
                raise ValueError("empty-file trash effects cannot have a keeper")
        elif self.action in {"move", "rename"}:
            if self.target_path is None or self.keeper is not None:
                raise ValueError("move/rename effects require a target and no keeper")
            if self.target_path == self.source.path:
                raise ValueError("effect target must differ from its source")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "effect_id": self.effect_id,
            "item_id": self.item_id,
            "kind": self.kind,
            "keeper": None if self.keeper is None else _snapshot_dict(self.keeper),
            "keeper_digest": self.keeper_digest,
            "ordinal": self.ordinal,
            "schema": AUTHORIZATION_EFFECTS_SCHEMA,
            "schema_version": AUTHORIZATION_EFFECTS_SCHEMA_VERSION,
            "source": _snapshot_dict(self.source),
            "source_digest": self.source_digest,
            "target_path": self.target_path,
            "task_id": self.task_id,
        }


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
    review_task_heads: tuple[AuthorizationReviewTaskHead, ...] | None = None
    review_task_heads_digest: str | None = None
    root_snapshot: AuthorizationRootSnapshot | None = None
    source_heads: tuple[CanonicalJsonObject, ...] | None = None
    source_heads_digest: str | None = None
    authorized_effects: tuple[AuthorizationEffect, ...] | None = None
    authorized_effects_digest: str | None = None

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
        if max_actions != (
            len(self.authorized_effects)
            if self.authorized_effects is not None
            else len(item_ids)
        ):
            raise ValueError("max_actions must equal the authorized effect count")
        object.__setattr__(self, "max_actions", max_actions)
        object.__setattr__(self, "max_bytes", _nonnegative_integer("max_bytes", self.max_bytes))
        issued_ns = _positive_integer("issued_ns", self.issued_ns)
        expires_ns = _positive_integer("expires_ns", self.expires_ns)
        if expires_ns <= issued_ns:
            raise ValueError("expires_ns must be later than issued_ns")
        object.__setattr__(self, "issued_ns", issued_ns)
        object.__setattr__(self, "expires_ns", expires_ns)
        heads = self.review_task_heads
        heads_digest = self.review_task_heads_digest
        if heads is None:
            if heads_digest is not None:
                raise ValueError("review_task_heads_digest requires review_task_heads")
        else:
            if not isinstance(heads, tuple) or not 1 <= len(heads) <= MAX_AUTHORIZATION_ITEMS:
                raise ValueError("review_task_heads must be a non-empty immutable tuple")
            if len(heads) != len(item_ids):
                raise ValueError("review_task_heads must match the authorized item count")
            if any(not isinstance(head, AuthorizationReviewTaskHead) for head in heads):
                raise ValueError("review_task_heads must contain AuthorizationReviewTaskHead values")
            if tuple(head.item_id for head in heads) != item_ids:
                raise ValueError("review_task_heads must preserve item_ids order")
            if tuple(head.task_id for head in heads) != task_ids:
                raise ValueError("review_task_heads must preserve task_ids order")
            if any(
                head.source_snapshot_fingerprint != self.source_snapshot_fingerprint
                or head.selector_signature != self.selector_signature
                for head in heads
            ):
                raise ValueError("review_task_heads are not bound to the grant source fence")
            if len({head.item_id for head in heads}) != len(heads):
                raise ValueError("review_task_heads cannot contain duplicate item_ids")
            if len({head.logical_key for head in heads}) != len(heads):
                raise ValueError("review_task_heads cannot contain duplicate logical_keys")
            if len({head.task_id for head in heads}) != len(heads):
                raise ValueError("review_task_heads cannot contain duplicate task_ids")
            expected_heads_digest = review_task_heads_digest(heads)
            if heads_digest is not None:
                _digest("review_task_heads_digest", heads_digest)
                if heads_digest != expected_heads_digest:
                    raise ValueError("review_task_heads_digest does not match the manifest")
            object.__setattr__(self, "review_task_heads_digest", expected_heads_digest)
            object.__setattr__(self, "review_task_heads", heads)
        root_snapshot = self.root_snapshot
        if root_snapshot is not None and not isinstance(root_snapshot, AuthorizationRootSnapshot):
            raise ValueError("root_snapshot must be an AuthorizationRootSnapshot")
        if root_snapshot is not None and root_snapshot.root != root:
            raise ValueError("root_snapshot root must match the grant root")
        source_heads = self.source_heads
        source_heads_digest = self.source_heads_digest
        if source_heads is None:
            if source_heads_digest is not None:
                raise ValueError("source_heads_digest requires source_heads")
        else:
            if not isinstance(source_heads, tuple) or not source_heads:
                raise ValueError("source_heads must be a non-empty immutable tuple")
            if any(not isinstance(head, CanonicalJsonObject) for head in source_heads):
                raise ValueError("source_heads must contain canonical JSON objects")
            expected_source_heads_digest = _source_heads_digest(source_heads)
            if source_heads_digest is not None:
                _digest("source_heads_digest", source_heads_digest)
                if source_heads_digest != expected_source_heads_digest:
                    raise ValueError("source_heads_digest does not match the manifest")
            object.__setattr__(self, "source_heads_digest", expected_source_heads_digest)
            object.__setattr__(self, "source_heads", source_heads)
        effects = self.authorized_effects
        effects_digest = self.authorized_effects_digest
        if effects is None:
            if effects_digest is not None:
                raise ValueError("authorized_effects_digest requires authorized_effects")
        else:
            if not isinstance(effects, tuple) or not 1 <= len(effects) <= MAX_AUTHORIZATION_EFFECTS:
                raise ValueError("authorized_effects must be a non-empty immutable tuple")
            if any(not isinstance(effect, AuthorizationEffect) for effect in effects):
                raise ValueError("authorized_effects must contain AuthorizationEffect values")
            effect_item_ids = {effect.item_id for effect in effects}
            if effect_item_ids != set(item_ids):
                raise ValueError("authorized effects contain an item outside the grant")
            if tuple(effect.ordinal for effect in effects) != tuple(range(1, len(effects) + 1)):
                raise ValueError("authorized effect ordinals must be contiguous")
            item_to_task = dict(zip(item_ids, task_ids, strict=True))
            if any(item_to_task.get(effect.item_id) != effect.task_id for effect in effects):
                raise ValueError("authorized effect task does not match its item")
            if any(effect.action != self.action for effect in effects):
                raise ValueError("authorized effects action differs from grant action")
            if sum(effect.source.size for effect in effects) > self.max_bytes:
                raise ValueError("max_bytes is below the authorized effect size")
            if len({effect.effect_id for effect in effects}) != len(effects):
                raise ValueError("authorized effects cannot contain duplicate effect_id")
            expected_effects_digest = authorized_effects_digest(effects)
            if effects_digest is not None:
                _digest("authorized_effects_digest", effects_digest)
                if effects_digest != expected_effects_digest:
                    raise ValueError("authorized_effects_digest does not match the manifest")
            object.__setattr__(self, "authorized_effects_digest", expected_effects_digest)
            object.__setattr__(self, "authorized_effects", effects)
        self.to_json()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
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
        if self.review_task_heads is not None:
            payload.update(
                {
                    "review_task_heads_schema_version": REVIEW_TASK_HEADS_SCHEMA_VERSION,
                    "review_task_heads": [head.to_dict() for head in self.review_task_heads],
                    "review_task_heads_digest": self.review_task_heads_digest,
                }
            )
        if self.root_snapshot is not None:
            payload["root_snapshot"] = self.root_snapshot.to_dict()
        if self.source_heads is not None:
            payload.update(
                {
                    "source_heads_schema_version": AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION,
                    "source_heads": [head.to_dict() for head in self.source_heads],
                    "source_heads_digest": self.source_heads_digest,
                }
            )
        if self.authorized_effects is not None:
            payload.update(
                {
                    "authorized_effects_schema_version": AUTHORIZATION_EFFECTS_SCHEMA_VERSION,
                    "authorized_effects": [effect.to_dict() for effect in self.authorized_effects],
                    "authorized_effects_digest": self.authorized_effects_digest,
                }
            )
        return payload

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
    "AUTHORIZATION_EFFECTS_SCHEMA",
    "AUTHORIZATION_EFFECTS_SCHEMA_VERSION",
    "AUTHORIZATION_GRANT_SCHEMA",
    "AUTHORIZATION_GRANT_SCHEMA_VERSION",
    "AUTHORIZATION_SCOPE",
    "AUTHORIZATION_SELECTOR_SIGNATURE",
    "AUTHORIZATION_SOURCE_HEADS_SCHEMA",
    "AUTHORIZATION_SOURCE_HEADS_SCHEMA_VERSION",
    "AUTHORIZATION_TASK_TYPE",
    "MAX_AUTHORIZATION_EFFECTS",
    "MAX_AUTHORIZATION_ITEMS",
    "REVIEW_TASK_HEADS_SCHEMA",
    "REVIEW_TASK_HEADS_SCHEMA_VERSION",
    "REVIEW_TASK_HEAD_DIGEST_SCHEMA",
    "AuthorizationEffect",
    "AuthorizationGrant",
    "AuthorizationReviewTaskHead",
    "AuthorizationRootSnapshot",
    "authorized_effects_digest",
    "review_task_head_digest",
    "review_task_heads_digest",
)
