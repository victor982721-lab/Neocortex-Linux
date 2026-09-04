"""Durable, bounded checkpoints for read-only curation work.

The checkpoint owner is deliberately a small filesystem manifest, not a
SQLite table or a Framework publication.  It records the exact snapshot that
was observed by a scan or verification batch, and it can therefore be
revalidated before a caller repeats that bounded batch.  Checkpoint files are
published with a temporary file, an ``fsync`` and an atomic hard-link with
no-replace semantics.  A pre-existing path is never overwritten.

This module does not open curation owners, inspect the corpus or authorize a
filesystem effect.  Callers provide the root identity and source observation,
and may inject a read-only snapshot observer when resuming.

The manifest is opt-in: legacy scan/verify callers neither read nor write this
owner and therefore continue unchanged.  A rollback simply stops publishing
new manifests; existing immutable files remain inspectable by a compatible
reader, while a future schema is rejected rather than reinterpreted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast


CURATION_CHECKPOINT_SCHEMA_VERSION = 1
CURATION_CHECKPOINT_CONTRACT = "neocortex.curation-checkpoint/v1"
CURATION_BATCH_CONTRACT = "neocortex.curation-batch/v1"

# The manifest is intentionally small enough to pass through a receipt or a
# caller-held token without becoming an unbounded state channel.
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_BATCH_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SOURCE_HEADS = 128
MAX_SOURCE_HEAD_BYTES = 48 * 1024
MAX_STRING_BYTES = 8 * 1024
MAX_EVENT_ID_BYTES = 256
MAX_PATH_BYTES = 4 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_JSON_DEPTH = 32

MAX_WORK_ITEMS = 1_000_000
MAX_WORK_FILES = 10_000_000
MAX_WORK_BYTES = 1 << 40

CheckpointOperation = Literal["scan", "verify"]
CheckpointState = Literal[
    "partial",
    "complete",
    "cancelled",
    "snapshot_changed",
    "invalid",
]
CheckpointValidationStatus = Literal["valid", "snapshot_changed", "invalid"]
ResumeStatus = Literal["resume", "complete", "snapshot_changed", "invalid"]
SourceHeadCoverage = Literal["complete", "partial", "unavailable"]

_CHECKPOINT_KEYS = frozenset(
    {
        "batch_digest",
        "budget",
        "contract",
        "cursor",
        "event_id",
        "operation",
        "plan_digest",
        "previous_checkpoint_digest",
        "root",
        "schema_version",
        "snapshot_id",
        "source_heads",
        "source_heads_digest",
        "state",
    }
)
_ROOT_KEYS = frozenset({"birthtime_ns", "dev", "inode", "path"})
_SOURCE_HEAD_KEYS = frozenset(
    {
        "coverage",
        "digest",
        "head_id",
        "item_count",
        "kind",
        "metadata",
        "owner",
        "reason",
        "revision",
        "root",
        "verification_mode",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "bytes_checked",
        "files_checked",
        "items_completed",
        "max_bytes",
        "max_files",
        "max_items",
    }
)


class CurationCheckpointError(ValueError):
    """The checkpoint contract or its storage cannot be trusted."""


class CurationCheckpointCorruptError(CurationCheckpointError):
    """A checkpoint file is truncated, non-canonical or otherwise invalid."""


class CurationCheckpointConflictError(CurationCheckpointError):
    """A no-replace write found different evidence at the target path."""


class CurationCheckpointStorageError(CurationCheckpointError):
    """The checkpoint owner cannot be read or durably published."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not accepted")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _validate_json_value(value: object, *, label: str, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise CurationCheckpointError(f"{label} exceeds the JSON nesting bound")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CurationCheckpointError(f"{label} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CurationCheckpointError(f"{label} has a non-string object key")
            if not key or len(key.encode("utf-8")) > MAX_STRING_BYTES:
                raise CurationCheckpointError(f"{label} has an invalid object key")
            _validate_json_value(child, label=label, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_value(child, label=label, depth=depth + 1)
        return
    raise CurationCheckpointError(f"{label} is not JSON-compatible")


def _canonical_json_bytes(value: object, *, label: str, maximum: int | None = None) -> bytes:
    _validate_json_value(value, label=label)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise CurationCheckpointError(f"{label} is not canonical JSON") from error
    if maximum is not None and len(encoded) > maximum:
        raise CurationCheckpointError(f"{label} exceeds its byte bound")
    return encoded


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_json(value: object, *, label: str, maximum: int | None = None) -> str:
    return _sha256(_canonical_json_bytes(value, label=label, maximum=maximum))


def _bounded_string(
    value: object,
    *,
    label: str,
    maximum: int,
    empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not empty and not value) or value.strip() != value:
        raise CurationCheckpointError(f"{label} must be a bounded trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise CurationCheckpointError(f"{label} is not valid UTF-8") from error
    if len(encoded) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CurationCheckpointError(f"{label} is outside its string bound")
    return value


def _digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise CurationCheckpointError(f"{label} must be sha256:<64 lowercase hex characters>")
    return value


def _optional_digest(value: object, *, label: str) -> str | None:
    return None if value is None else _digest(value, label=label)


def _operation(value: object, *, label: str = "operation") -> CheckpointOperation:
    if not isinstance(value, str) or value not in {"scan", "verify"}:
        raise CurationCheckpointError(f"{label} is invalid")
    return cast(CheckpointOperation, value)


def _state(value: object, *, label: str = "state") -> CheckpointState:
    allowed = {"partial", "complete", "cancelled", "snapshot_changed", "invalid"}
    if not isinstance(value, str) or value not in allowed:
        raise CurationCheckpointError(f"{label} is invalid")
    return cast(CheckpointState, value)


def _integer(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CurationCheckpointError(f"{label} is outside its integer bound")
    return value


def _canonical_path(value: object, *, label: str) -> str:
    path = _bounded_string(value, label=label, maximum=MAX_PATH_BYTES)
    if not os.path.isabs(path):
        raise CurationCheckpointError(f"{label} must be absolute")
    normalized = os.path.normpath(path)
    if normalized != path:
        raise CurationCheckpointError(f"{label} is not canonical")
    return path


def _optional_path(value: object, *, label: str) -> str | None:
    return None if value is None else _canonical_path(value, label=label)


@dataclass(frozen=True, slots=True)
class CurationCheckpointRoot:
    """Stable identity of the root observed by the producer."""

    path: str
    dev: int
    inode: int
    birthtime_ns: int

    def __post_init__(self) -> None:
        if _canonical_path(self.path, label="root.path") != self.path:
            raise CurationCheckpointError("root.path is not canonical")
        _integer(self.dev, label="root.dev", minimum=0, maximum=(1 << 63) - 1)
        _integer(self.inode, label="root.inode", minimum=0, maximum=(1 << 63) - 1)
        _integer(self.birthtime_ns, label="root.birthtime_ns", minimum=-1, maximum=(1 << 63) - 1)

    @classmethod
    def from_mapping(cls, value: object) -> "CurationCheckpointRoot":
        mapping = _mapping(value, label="root")
        _exact_keys(mapping, _ROOT_KEYS, label="root")
        return cls(
            path=_canonical_path(mapping["path"], label="root.path"),
            dev=_integer(mapping["dev"], label="root.dev", minimum=0, maximum=(1 << 63) - 1),
            inode=_integer(
                mapping["inode"], label="root.inode", minimum=0, maximum=(1 << 63) - 1
            ),
            birthtime_ns=_integer(
                mapping["birthtime_ns"],
                label="root.birthtime_ns",
                minimum=-1,
                maximum=(1 << 63) - 1,
            ),
        )

    @classmethod
    def from_root_identity(cls, value: object) -> "CurationCheckpointRoot":
        """Adapt the inventory ``RootIdentity`` without importing its owner."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        try:
            path = getattr(value, "path")  # noqa: B009 - adapt a legacy identity object
            dev = getattr(value, "dev", None)
            if dev is None:
                dev = getattr(value, "volume_id")  # noqa: B009 - legacy inventory field
            inode = getattr(value, "inode", None)
            if inode is None:
                inode = getattr(value, "file_id")  # noqa: B009 - legacy inventory field
            birthtime_ns = getattr(value, "birthtime_ns")  # noqa: B009 - identity field
        except AttributeError as error:
            raise CurationCheckpointError("root identity has an unsupported shape") from error
        return cls(
            path=_canonical_path(path, label="root.path"),
            dev=_integer(dev, label="root.dev", minimum=0, maximum=(1 << 63) - 1),
            inode=_integer(inode, label="root.inode", minimum=0, maximum=(1 << 63) - 1),
            birthtime_ns=_integer(
                birthtime_ns,
                label="root.birthtime_ns",
                minimum=-1,
                maximum=(1 << 63) - 1,
            ),
        )

    @classmethod
    def from_stat_result(cls, path: str | Path, result: os.stat_result) -> "CurationCheckpointRoot":
        """Build an identity from a caller-owned, already captured stat result."""

        birthtime_ns = getattr(result, "st_birthtime_ns", -1)
        return cls(
            path=os.path.normpath(os.path.abspath(os.fspath(path))),
            dev=int(result.st_dev),
            inode=int(result.st_ino),
            birthtime_ns=int(birthtime_ns),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "birthtime_ns": self.birthtime_ns,
            "dev": self.dev,
            "inode": self.inode,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class CurationCheckpointSourceHead:
    """Canonical, bounded representation of one source owner head."""

    owner: str
    kind: str
    head_id: str | None
    digest: str
    root: str | None
    revision: int | None
    item_count: int
    coverage: SourceHeadCoverage
    reason: str | None = None
    verification_mode: str | None = None
    metadata: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        _bounded_string(self.owner, label="source_head.owner", maximum=256)
        _bounded_string(self.kind, label="source_head.kind", maximum=256)
        if self.head_id is not None:
            _bounded_string(self.head_id, label="source_head.head_id", maximum=512)
        _digest(self.digest, label="source_head.digest")
        _optional_path(self.root, label="source_head.root")
        if self.revision is not None:
            _integer(self.revision, label="source_head.revision", minimum=0, maximum=(1 << 63) - 1)
        _integer(self.item_count, label="source_head.item_count", minimum=0, maximum=MAX_WORK_ITEMS)
        if not isinstance(self.coverage, str) or self.coverage not in {
            "complete",
            "partial",
            "unavailable",
        }:
            raise CurationCheckpointError("source_head.coverage is invalid")
        if self.reason is not None:
            _bounded_string(self.reason, label="source_head.reason", maximum=4 * 1024)
        if self.verification_mode is not None:
            _bounded_string(
                self.verification_mode,
                label="source_head.verification_mode",
                maximum=256,
            )
        names: list[str] = []
        for key, value in self.metadata:
            _bounded_string(key, label="source_head.metadata key", maximum=MAX_STRING_BYTES)
            names.append(key)
            _validate_json_value(value, label="source_head.metadata")
        if names != sorted(names) or len(set(names)) != len(names):
            raise CurationCheckpointError("source_head.metadata must be sorted and unique")
        _canonical_json_bytes(dict(self.metadata), label="source_head.metadata", maximum=MAX_METADATA_BYTES)

    @classmethod
    def from_mapping(cls, value: object) -> "CurationCheckpointSourceHead":
        if isinstance(value, cls):
            return value
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            value = to_dict()
        mapping = _mapping(value, label="source_head")
        _exact_keys(mapping, _SOURCE_HEAD_KEYS, label="source_head")
        metadata_mapping = _mapping(mapping["metadata"], label="source_head.metadata")
        metadata = tuple(sorted(metadata_mapping.items()))
        coverage = mapping["coverage"]
        if not isinstance(coverage, str) or coverage not in {
            "complete",
            "partial",
            "unavailable",
        }:
            raise CurationCheckpointError("source_head.coverage is invalid")
        return cls(
            owner=_bounded_string(mapping["owner"], label="source_head.owner", maximum=256),
            kind=_bounded_string(mapping["kind"], label="source_head.kind", maximum=256),
            head_id=(
                None
                if mapping["head_id"] is None
                else _bounded_string(mapping["head_id"], label="source_head.head_id", maximum=512)
            ),
            digest=_digest(mapping["digest"], label="source_head.digest"),
            root=_optional_path(mapping["root"], label="source_head.root"),
            revision=(
                None
                if mapping["revision"] is None
                else _integer(
                    mapping["revision"],
                    label="source_head.revision",
                    minimum=0,
                    maximum=(1 << 63) - 1,
                )
            ),
            item_count=_integer(
                mapping["item_count"],
                label="source_head.item_count",
                minimum=0,
                maximum=MAX_WORK_ITEMS,
            ),
            coverage=cast(SourceHeadCoverage, coverage),
            reason=(
                None
                if mapping["reason"] is None
                else _bounded_string(mapping["reason"], label="source_head.reason", maximum=4 * 1024)
            ),
            verification_mode=(
                None
                if mapping["verification_mode"] is None
                else _bounded_string(
                    mapping["verification_mode"],
                    label="source_head.verification_mode",
                    maximum=256,
                )
            ),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage": self.coverage,
            "digest": self.digest,
            "head_id": self.head_id,
            "item_count": self.item_count,
            "kind": self.kind,
            "metadata": dict(self.metadata),
            "owner": self.owner,
            "reason": self.reason,
            "revision": self.revision,
            "root": self.root,
            "verification_mode": self.verification_mode,
        }


@dataclass(frozen=True, slots=True)
class CurationCheckpointBudget:
    """Bounded limits and completed counters carried across a resume."""

    max_items: int
    max_files: int
    max_bytes: int
    items_completed: int = 0
    files_checked: int = 0
    bytes_checked: int = 0

    def __post_init__(self) -> None:
        _integer(self.max_items, label="budget.max_items", minimum=1, maximum=MAX_WORK_ITEMS)
        _integer(self.max_files, label="budget.max_files", minimum=1, maximum=MAX_WORK_FILES)
        _integer(self.max_bytes, label="budget.max_bytes", minimum=1, maximum=MAX_WORK_BYTES)
        _integer(
            self.items_completed,
            label="budget.items_completed",
            minimum=0,
            maximum=self.max_items,
        )
        _integer(
            self.files_checked,
            label="budget.files_checked",
            minimum=0,
            maximum=self.max_files,
        )
        _integer(
            self.bytes_checked,
            label="budget.bytes_checked",
            minimum=0,
            maximum=self.max_bytes,
        )

    @classmethod
    def from_mapping(cls, value: object) -> "CurationCheckpointBudget":
        if isinstance(value, cls):
            return value
        mapping = _mapping(value, label="budget")
        _exact_keys(mapping, _BUDGET_KEYS, label="budget")
        return cls(
            max_items=_integer(
                mapping["max_items"], label="budget.max_items", minimum=1, maximum=MAX_WORK_ITEMS
            ),
            max_files=_integer(
                mapping["max_files"], label="budget.max_files", minimum=1, maximum=MAX_WORK_FILES
            ),
            max_bytes=_integer(
                mapping["max_bytes"], label="budget.max_bytes", minimum=1, maximum=MAX_WORK_BYTES
            ),
            items_completed=_integer(
                mapping["items_completed"],
                label="budget.items_completed",
                minimum=0,
                maximum=MAX_WORK_ITEMS,
            ),
            files_checked=_integer(
                mapping["files_checked"],
                label="budget.files_checked",
                minimum=0,
                maximum=MAX_WORK_FILES,
            ),
            bytes_checked=_integer(
                mapping["bytes_checked"],
                label="budget.bytes_checked",
                minimum=0,
                maximum=MAX_WORK_BYTES,
            ),
        )

    @property
    def items_remaining(self) -> int:
        return self.max_items - self.items_completed

    @property
    def files_remaining(self) -> int:
        return self.max_files - self.files_checked

    @property
    def bytes_remaining(self) -> int:
        return self.max_bytes - self.bytes_checked

    def to_dict(self) -> dict[str, int]:
        return {
            "bytes_checked": self.bytes_checked,
            "files_checked": self.files_checked,
            "items_completed": self.items_completed,
            "max_bytes": self.max_bytes,
            "max_files": self.max_files,
            "max_items": self.max_items,
        }


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CurationCheckpointError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _exact_keys(mapping: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    if set(mapping) != expected:
        raise CurationCheckpointError(f"{label} has unknown or missing fields")


def _validate_cursor(value: object, *, label: str = "cursor") -> str | None:
    if value is None:
        return None
    return _bounded_string(value, label=label, maximum=2_048)


def _normalize_source_heads(
    source_heads: Sequence[CurationCheckpointSourceHead | Mapping[str, object] | object],
) -> tuple[CurationCheckpointSourceHead, ...]:
    if len(source_heads) > MAX_SOURCE_HEADS:
        raise CurationCheckpointError("source_heads exceed the count bound")
    normalized = tuple(CurationCheckpointSourceHead.from_mapping(head) for head in source_heads)
    normalized = tuple(
        sorted(normalized, key=lambda head: (head.owner, head.kind, head.head_id or "", head.digest))
    )
    identity_keys = [(head.owner, head.kind, head.head_id) for head in normalized]
    if len(set(identity_keys)) != len(identity_keys):
        raise CurationCheckpointError("source_heads contain duplicate owner heads")
    return normalized


def compute_source_heads_digest(
    source_heads: Sequence[CurationCheckpointSourceHead | Mapping[str, object] | object],
) -> str:
    """Hash the sorted canonical source-head set without reading any owner."""

    normalized = _normalize_source_heads(source_heads)
    return _digest_json(
        {
            "contract": "neocortex.curation-source-heads/v1",
            "source_heads": [head.to_dict() for head in normalized],
        },
        label="source_heads",
        maximum=MAX_SOURCE_HEAD_BYTES,
    )


@dataclass(frozen=True, slots=True)
class CurationSnapshotObservation:
    """Read-only snapshot returned by an injected resume observer."""

    root: CurationCheckpointRoot
    source_heads: tuple[CurationCheckpointSourceHead, ...]
    plan_digest: str
    snapshot_id: str

    def __post_init__(self) -> None:
        normalized = _normalize_source_heads(self.source_heads)
        if normalized != self.source_heads:
            raise CurationCheckpointError("snapshot source_heads are not canonical")
        _digest(self.plan_digest, label="snapshot.plan_digest")
        _digest(self.snapshot_id, label="snapshot.snapshot_id")

    @property
    def source_heads_digest(self) -> str:
        return compute_source_heads_digest(self.source_heads)


@dataclass(frozen=True, slots=True)
class CurationCheckpoint:
    """One immutable checkpoint manifest for a bounded scan or verify batch."""

    schema_version: int
    event_id: str
    operation: CheckpointOperation
    state: CheckpointState
    root: CurationCheckpointRoot
    source_heads: tuple[CurationCheckpointSourceHead, ...]
    source_heads_digest: str
    plan_digest: str
    snapshot_id: str
    cursor: str | None
    batch_digest: str
    budget: CurationCheckpointBudget
    previous_checkpoint_digest: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != CURATION_CHECKPOINT_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise CurationCheckpointError("unsupported checkpoint schema version")
        _bounded_string(self.event_id, label="event_id", maximum=MAX_EVENT_ID_BYTES)
        _operation(self.operation, label="checkpoint.operation")
        _state(self.state, label="checkpoint.state")
        if not isinstance(self.root, CurationCheckpointRoot):
            raise CurationCheckpointError("checkpoint.root has the wrong type")
        if not isinstance(self.budget, CurationCheckpointBudget):
            raise CurationCheckpointError("checkpoint.budget has the wrong type")
        normalized = _normalize_source_heads(self.source_heads)
        if normalized != self.source_heads:
            raise CurationCheckpointError("checkpoint source_heads are not canonical")
        actual_source_heads_digest = compute_source_heads_digest(self.source_heads)
        if _digest(self.source_heads_digest, label="source_heads_digest") != actual_source_heads_digest:
            raise CurationCheckpointError("source_heads_digest does not match source_heads")
        _digest(self.plan_digest, label="plan_digest")
        _digest(self.snapshot_id, label="snapshot_id")
        _validate_cursor(self.cursor)
        _digest(self.batch_digest, label="batch_digest")
        _optional_digest(
            self.previous_checkpoint_digest,
            label="previous_checkpoint_digest",
        )
        if self.state == "complete" and self.cursor is not None:
            raise CurationCheckpointError("complete checkpoint must have a null cursor")
        encoded = _canonical_json_bytes(self.to_dict(), label="checkpoint", maximum=MAX_CHECKPOINT_BYTES)
        if not encoded:
            raise CurationCheckpointError("checkpoint cannot be empty")

    @classmethod
    def from_mapping(cls, value: object) -> "CurationCheckpoint":
        mapping = _mapping(value, label="checkpoint")
        _exact_keys(mapping, _CHECKPOINT_KEYS, label="checkpoint")
        if mapping["contract"] != CURATION_CHECKPOINT_CONTRACT:
            raise CurationCheckpointError("checkpoint contract is unsupported")
        schema_version = _integer(
            mapping["schema_version"],
            label="schema_version",
            minimum=0,
            maximum=CURATION_CHECKPOINT_SCHEMA_VERSION,
        )
        if schema_version != CURATION_CHECKPOINT_SCHEMA_VERSION:
            raise CurationCheckpointError("unsupported checkpoint schema version")
        source_value = mapping["source_heads"]
        if not isinstance(source_value, list):
            raise CurationCheckpointError("source_heads must be a JSON array")
        source_heads = _normalize_source_heads(source_value)
        operation = _operation(mapping["operation"], label="checkpoint.operation")
        state = _state(mapping["state"], label="checkpoint.state")
        return cls(
            schema_version=schema_version,
            event_id=_bounded_string(mapping["event_id"], label="event_id", maximum=MAX_EVENT_ID_BYTES),
            operation=operation,
            state=state,
            root=CurationCheckpointRoot.from_mapping(mapping["root"]),
            source_heads=source_heads,
            source_heads_digest=_digest(mapping["source_heads_digest"], label="source_heads_digest"),
            plan_digest=_digest(mapping["plan_digest"], label="plan_digest"),
            previous_checkpoint_digest=_optional_digest(
                mapping["previous_checkpoint_digest"],
                label="previous_checkpoint_digest",
            ),
            snapshot_id=_digest(mapping["snapshot_id"], label="snapshot_id"),
            cursor=_validate_cursor(mapping["cursor"]),
            batch_digest=_digest(mapping["batch_digest"], label="batch_digest"),
            budget=CurationCheckpointBudget.from_mapping(mapping["budget"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_digest": self.batch_digest,
            "budget": self.budget.to_dict(),
            "contract": CURATION_CHECKPOINT_CONTRACT,
            "cursor": self.cursor,
            "event_id": self.event_id,
            "operation": self.operation,
            "plan_digest": self.plan_digest,
            "previous_checkpoint_digest": self.previous_checkpoint_digest,
            "root": self.root.to_dict(),
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_heads": [head.to_dict() for head in self.source_heads],
            "source_heads_digest": self.source_heads_digest,
            "state": self.state,
        }

    def to_json(self) -> str:
        return _canonical_json_bytes(
            self.to_dict(), label="checkpoint", maximum=MAX_CHECKPOINT_BYTES
        ).decode("utf-8")


def create_checkpoint(
    *,
    operation: CheckpointOperation,
    state: CheckpointState,
    root: object,
    source_heads: Sequence[CurationCheckpointSourceHead | Mapping[str, object] | object],
    plan_digest: str,
    snapshot_id: str,
    cursor: str | None,
    batch_digest: str,
    budget: CurationCheckpointBudget | Mapping[str, object],
    event_id: str | None = None,
    previous_checkpoint_digest: str | None = None,
) -> CurationCheckpoint:
    """Create and fully validate a checkpoint without opening durable state."""

    normalized_root = CurationCheckpointRoot.from_root_identity(root)
    normalized_heads = _normalize_source_heads(source_heads)
    normalized_budget = (
        budget if isinstance(budget, CurationCheckpointBudget) else CurationCheckpointBudget.from_mapping(budget)
    )
    effective_event_id = event_id if event_id is not None else f"checkpoint-{uuid.uuid4().hex}"
    normalized_operation = _operation(operation, label="checkpoint.operation")
    normalized_state = _state(state, label="checkpoint.state")
    return CurationCheckpoint(
        schema_version=CURATION_CHECKPOINT_SCHEMA_VERSION,
        event_id=_bounded_string(effective_event_id, label="event_id", maximum=MAX_EVENT_ID_BYTES),
        operation=normalized_operation,
        state=normalized_state,
        root=normalized_root,
        source_heads=normalized_heads,
        source_heads_digest=compute_source_heads_digest(normalized_heads),
        plan_digest=_digest(plan_digest, label="plan_digest"),
        previous_checkpoint_digest=_optional_digest(
            previous_checkpoint_digest,
            label="previous_checkpoint_digest",
        ),
        snapshot_id=_digest(snapshot_id, label="snapshot_id"),
        cursor=_validate_cursor(cursor),
        batch_digest=_digest(batch_digest, label="batch_digest"),
        budget=normalized_budget,
    )


def parse_checkpoint_json(value: str | bytes) -> CurationCheckpoint:
    """Parse one canonical checkpoint receipt, rejecting future/unknown fields."""

    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeError as error:
            raise CurationCheckpointCorruptError("checkpoint is not valid UTF-8") from error
    elif isinstance(value, bytes):
        raw = value
    else:
        raise CurationCheckpointCorruptError("checkpoint JSON must be str or bytes")
    if not raw or len(raw) > MAX_CHECKPOINT_BYTES:
        raise CurationCheckpointCorruptError("checkpoint exceeds its byte bound")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
        checkpoint = CurationCheckpoint.from_mapping(payload)
    except CurationCheckpointCorruptError:
        raise
    except CurationCheckpointError as error:
        raise CurationCheckpointCorruptError("checkpoint payload is invalid") from error
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CurationCheckpointCorruptError("checkpoint JSON is invalid") from error
    if checkpoint.to_json().encode("utf-8") != raw:
        raise CurationCheckpointCorruptError("checkpoint JSON is not canonical")
    return checkpoint


def serialize_checkpoint(checkpoint: CurationCheckpoint) -> str:
    """Return the receipt-compatible canonical JSON representation."""

    if not isinstance(checkpoint, CurationCheckpoint):
        raise CurationCheckpointError("checkpoint has the wrong type")
    return checkpoint.to_json()


class CurationSnapshotObserver(Protocol):
    """Callable-free form accepted by :func:`validate_checkpoint`."""

    def observe(self) -> CurationSnapshotObservation:
        """Return a read-only, already captured source snapshot."""


def _observe(
    observer: CurationSnapshotObserver | Callable[[], CurationSnapshotObservation],
) -> CurationSnapshotObservation:
    callback = getattr(observer, "observe", None)
    value = callback() if callable(callback) else observer()
    if not isinstance(value, CurationSnapshotObservation):
        raise CurationCheckpointError("snapshot observer returned the wrong type")
    return value


@dataclass(frozen=True, slots=True)
class CurationCheckpointValidation:
    """Result of comparing a checkpoint to a fresh injected observation."""

    status: CheckpointValidationStatus
    reason_code: str
    detail: str
    checkpoint: CurationCheckpoint
    observed: CurationSnapshotObservation | None = None

    @property
    def resumable(self) -> bool:
        return self.status == "valid" and self.checkpoint.state in {"partial", "cancelled"}


def _validation(
    checkpoint: CurationCheckpoint,
    status: CheckpointValidationStatus,
    reason_code: str,
    detail: str,
    observed: CurationSnapshotObservation | None = None,
) -> CurationCheckpointValidation:
    return CurationCheckpointValidation(
        status=status,
        reason_code=reason_code,
        detail=detail,
        checkpoint=checkpoint,
        observed=observed,
    )


def validate_checkpoint(
    checkpoint: CurationCheckpoint,
    observer: CurationSnapshotObserver | Callable[[], CurationSnapshotObservation],
) -> CurationCheckpointValidation:
    """Revalidate root, heads and digests without touching their owners."""

    if not isinstance(checkpoint, CurationCheckpoint):
        raise CurationCheckpointError("checkpoint has the wrong type")
    if checkpoint.state == "invalid":
        return _validation(checkpoint, "invalid", "checkpoint_invalid", "checkpoint is marked invalid")
    if checkpoint.state == "snapshot_changed":
        return _validation(
            checkpoint,
            "snapshot_changed",
            "checkpoint_snapshot_changed",
            "checkpoint is marked snapshot_changed",
        )
    try:
        observed = _observe(observer)
    except Exception:
        return _validation(
            checkpoint,
            "invalid",
            "snapshot_observer_failed",
            "snapshot observer did not return a trusted observation",
        )
    comparisons: tuple[tuple[bool, str, str], ...] = (
        (checkpoint.root == observed.root, "root_identity_changed", "root identity changed"),
        (
            checkpoint.source_heads == observed.source_heads,
            "source_heads_changed",
            "source heads changed",
        ),
        (
            checkpoint.source_heads_digest == observed.source_heads_digest,
            "source_heads_digest_changed",
            "source heads digest changed",
        ),
        (checkpoint.plan_digest == observed.plan_digest, "plan_digest_changed", "plan digest changed"),
        (checkpoint.snapshot_id == observed.snapshot_id, "snapshot_id_changed", "snapshot id changed"),
    )
    for matches, reason_code, detail in comparisons:
        if not matches:
            return _validation(checkpoint, "snapshot_changed", reason_code, detail, observed)
    return _validation(checkpoint, "valid", "snapshot_match", "checkpoint snapshot matches", observed)


def compute_batch_digest(
    *,
    operation: CheckpointOperation,
    plan_digest: str,
    snapshot_id: str,
    cursor_before: str | None,
    cursor_after: str | None,
    batch: object,
    budget: CurationCheckpointBudget | Mapping[str, object],
) -> str:
    """Hash one ordered batch and its post-batch counters for replay checks."""

    normalized_operation = _operation(operation, label="batch.operation")
    normalized_budget = (
        budget if isinstance(budget, CurationCheckpointBudget) else CurationCheckpointBudget.from_mapping(budget)
    )
    _digest(plan_digest, label="batch.plan_digest")
    _digest(snapshot_id, label="batch.snapshot_id")
    _validate_cursor(cursor_before, label="batch.cursor_before")
    _validate_cursor(cursor_after, label="batch.cursor_after")
    _canonical_json_bytes(batch, label="batch", maximum=MAX_BATCH_PAYLOAD_BYTES)
    return _digest_json(
        {
            "batch": batch,
            "budget": normalized_budget.to_dict(),
            "contract": CURATION_BATCH_CONTRACT,
            "cursor_after": cursor_after,
            "cursor_before": cursor_before,
            "operation": normalized_operation,
            "plan_digest": plan_digest,
            "snapshot_id": snapshot_id,
        },
        label="batch envelope",
        maximum=MAX_BATCH_PAYLOAD_BYTES,
    )


@dataclass(frozen=True, slots=True)
class CurationCheckpointResume:
    """Safe continuation decision for one checkpoint."""

    status: ResumeStatus
    reason_code: str
    checkpoint: CurationCheckpoint
    cursor: str | None
    budget: CurationCheckpointBudget
    replay_required: bool
    validation: CurationCheckpointValidation


def resume_checkpoint(
    checkpoint: CurationCheckpoint,
    observer: CurationSnapshotObserver | Callable[[], CurationSnapshotObservation],
    *,
    cursor_before: str | None = None,
    batch: object | None = None,
) -> CurationCheckpointResume:
    """Validate a checkpoint and return a bounded, replay-safe continuation.

    ``batch`` and ``cursor_before`` are optional because a caller may not yet
    have materialized the prior batch.  When supplied, their canonical digest
    must match the immutable checkpoint before the caller is allowed to
    repeat it.
    """

    validation = validate_checkpoint(checkpoint, observer)
    if validation.status != "valid":
        status: ResumeStatus = validation.status
        return CurationCheckpointResume(
            status=status,
            reason_code=validation.reason_code,
            checkpoint=checkpoint,
            cursor=checkpoint.cursor,
            budget=checkpoint.budget,
            replay_required=False,
            validation=validation,
        )
    if batch is not None:
        if validation.observed is None:
            raise CurationCheckpointError("valid checkpoint has no observation")
        expected = compute_batch_digest(
            operation=checkpoint.operation,
            plan_digest=checkpoint.plan_digest,
            snapshot_id=checkpoint.snapshot_id,
            cursor_before=cursor_before,
            cursor_after=checkpoint.cursor,
            batch=batch,
            budget=checkpoint.budget,
        )
        if expected != checkpoint.batch_digest:
            invalid = _validation(
                checkpoint,
                "invalid",
                "batch_digest_mismatch",
                "replay batch digest does not match checkpoint",
                validation.observed,
            )
            return CurationCheckpointResume(
                status="invalid",
                reason_code=invalid.reason_code,
                checkpoint=checkpoint,
                cursor=checkpoint.cursor,
                budget=checkpoint.budget,
                replay_required=False,
                validation=invalid,
            )
    if checkpoint.state == "complete":
        return CurationCheckpointResume(
            status="complete",
            reason_code="checkpoint_complete",
            checkpoint=checkpoint,
            cursor=None,
            budget=checkpoint.budget,
            replay_required=False,
            validation=validation,
        )
    return CurationCheckpointResume(
        status="resume",
        reason_code="snapshot_match",
        checkpoint=checkpoint,
        cursor=checkpoint.cursor,
        budget=checkpoint.budget,
        replay_required=True,
        validation=validation,
    )


def _absolute_storage_path(path: str | Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    if candidate.name in {"", ".", ".."}:
        raise CurationCheckpointStorageError("checkpoint path must name a file")
    return candidate


def _assert_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CurationCheckpointStorageError(f"{label} cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CurationCheckpointStorageError(f"{label} is not a regular directory")


def _assert_private_directory(path: Path, *, label: str) -> None:
    _assert_directory(path, label=label)
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError as error:
        raise CurationCheckpointStorageError(f"{label} cannot be inspected") from error
    if mode & 0o077:
        raise CurationCheckpointStorageError(f"{label} has unsafe permissions")


def _ensure_storage_parent(path: Path) -> None:
    parent = path.parent
    absolute_parent = Path(os.path.abspath(parent))
    missing: list[Path] = []
    current = absolute_parent
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current == current.parent:
                raise CurationCheckpointStorageError(
                    "checkpoint parent has no existing ancestor"
                ) from None
            current = current.parent
            continue
        except OSError as error:
            raise CurationCheckpointStorageError("checkpoint parent cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CurationCheckpointStorageError("checkpoint parent contains an unsafe component")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _assert_directory(directory, label="checkpoint parent")
    _assert_private_directory(absolute_parent, label="checkpoint parent")


def _check_storage_parent(path: Path) -> None:
    """Validate an existing parent without creating any filesystem entry."""

    immediate = Path(os.path.abspath(path.parent))
    _assert_private_directory(immediate, label="checkpoint parent")
    current = immediate
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise CurationCheckpointStorageError("checkpoint parent is missing") from None
        except OSError as error:
            raise CurationCheckpointStorageError("checkpoint parent cannot be inspected") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CurationCheckpointStorageError("checkpoint parent contains an unsafe component")
        if current == current.parent:
            return
        current = current.parent


def _has_recovery_link(path: Path, metadata: os.stat_result) -> bool:
    """Recognize the one temporary hard-link left by a crash after publish."""

    prefix = f".{path.name}."
    try:
        candidates = path.parent.iterdir()
        for index, candidate in enumerate(candidates):
            if index >= 256:
                break
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(".tmp"):
                continue
            try:
                sibling = candidate.lstat()
            except OSError:
                continue
            if (
                stat.S_ISREG(sibling.st_mode)
                and sibling.st_nlink == 2
                and (sibling.st_dev, sibling.st_ino) == (metadata.st_dev, metadata.st_ino)
                and stat.S_IMODE(sibling.st_mode) == 0o600
            ):
                return True
    except OSError:
        return False
    return False


def _assert_target(
    path: Path,
    *,
    must_exist: bool,
    allow_recovery_link: bool = False,
) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise CurationCheckpointStorageError("checkpoint file is missing") from None
        return None
    except OSError as error:
        raise CurationCheckpointStorageError("checkpoint file cannot be inspected") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise CurationCheckpointStorageError("checkpoint file is a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise CurationCheckpointStorageError("checkpoint file is not regular")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CurationCheckpointStorageError("checkpoint file has unsafe links or permissions")
    if metadata.st_nlink != 1 and not (
        allow_recovery_link and metadata.st_nlink == 2 and _has_recovery_link(path, metadata)
    ):
        raise CurationCheckpointStorageError("checkpoint file has unsafe links or permissions")
    return metadata


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise CurationCheckpointStorageError("checkpoint parent cannot be opened") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise CurationCheckpointStorageError("checkpoint parent cannot be synced") from error
    finally:
        os.close(descriptor)


def read_checkpoint(path: str | Path) -> CurationCheckpoint:
    """Read a checkpoint through a descriptor, ignoring crash leftovers."""

    target = _absolute_storage_path(path)
    _check_storage_parent(target)
    before = _assert_target(target, must_exist=True, allow_recovery_link=True)
    assert before is not None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise CurationCheckpointStorageError("checkpoint file cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise CurationCheckpointStorageError("checkpoint file identity changed")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_CHECKPOINT_BYTES or after.st_size != len(raw):
            raise CurationCheckpointCorruptError("checkpoint file exceeds its byte bound")
    except CurationCheckpointError:
        raise
    except (OSError, ValueError) as error:
        raise CurationCheckpointStorageError("checkpoint file cannot be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    current = _assert_target(target, must_exist=True, allow_recovery_link=True)
    assert current is not None
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise CurationCheckpointStorageError("checkpoint file was replaced while reading")
    return parse_checkpoint_json(raw)


def write_checkpoint(path: str | Path, checkpoint: CurationCheckpoint) -> CurationCheckpoint:
    """Publish one immutable checkpoint with atomic no-replace semantics."""

    if not isinstance(checkpoint, CurationCheckpoint):
        raise CurationCheckpointError("checkpoint has the wrong type")
    target = _absolute_storage_path(path)
    _ensure_storage_parent(target)
    payload = checkpoint.to_json().encode("utf-8")
    existing = _assert_target(target, must_exist=False, allow_recovery_link=True)
    if existing is not None:
        current = read_checkpoint(target)
        if current.to_json().encode("utf-8") == payload:
            return current
        raise CurationCheckpointConflictError("checkpoint target already contains different evidence")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            current = read_checkpoint(target)
            if current.to_json().encode("utf-8") == payload:
                return current
            raise CurationCheckpointConflictError(
                "checkpoint target was concurrently published with different evidence"
            ) from None
        except OSError as error:
            raise CurationCheckpointStorageError("checkpoint no-replace publication failed") from error
        _fsync_directory(target.parent)
        return checkpoint
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(target.parent)


__all__ = [
    "CURATION_BATCH_CONTRACT",
    "CURATION_CHECKPOINT_CONTRACT",
    "CURATION_CHECKPOINT_SCHEMA_VERSION",
    "MAX_BATCH_PAYLOAD_BYTES",
    "MAX_CHECKPOINT_BYTES",
    "MAX_SOURCE_HEADS",
    "CurationCheckpoint",
    "CurationCheckpointBudget",
    "CurationCheckpointConflictError",
    "CurationCheckpointCorruptError",
    "CurationCheckpointError",
    "CurationCheckpointResume",
    "CurationCheckpointRoot",
    "CurationCheckpointSourceHead",
    "CurationCheckpointStorageError",
    "CurationCheckpointValidation",
    "CurationSnapshotObservation",
    "CurationSnapshotObserver",
    "compute_batch_digest",
    "compute_source_heads_digest",
    "create_checkpoint",
    "parse_checkpoint_json",
    "read_checkpoint",
    "resume_checkpoint",
    "serialize_checkpoint",
    "validate_checkpoint",
    "write_checkpoint",
]
