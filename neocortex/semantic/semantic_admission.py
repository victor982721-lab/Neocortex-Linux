"""Local content-admission, identity and reuse boundaries for Semantic.

This module is intentionally small and provider-free.  It deals in source
metadata, bounded fingerprints and policy decisions; it never reads source
payloads and never invokes an embedding backend.  The durable ledger lives in
the existing Framework SQLite owner (``framework_content_admission``).  Keeping
the policy here makes the same decision usable by source projection, staging
and read-only query adapters without creating a second state owner.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, HASH_ALGORITHM_64

from .semantic_models import (
    ContentFingerprint,
    EmbeddingRole,
    SemanticItem,
    canonical_json,
)


# region [01] Bounded identity contracts


ADMISSION_POLICY_SCHEMA = "neocortex.semantic-content-admission/v1"
ADMISSION_IDENTITY_SCHEMA = "neocortex.semantic-identities/v1"
ADMISSION_DECISION_SCHEMA = "neocortex.semantic-content-decision/v1"
CONTENT_IDENTITY_ALGORITHM = f"{HASH_ALGORITHM_128}+{HASH_ALGORITHM_64}-guard-v1"
LEGACY_CONTENT_IDENTITY_ALGORITHM = "xxh3-128+xxh3-64-guard-v1"
FALLBACK_CONTENT_IDENTITY_ALGORITHM = "sha256-128-fallback-v1+sha256-64-fallback-v1-guard-v1"
_SUPPORTED_CONTENT_IDENTITY_ALGORITHMS = frozenset(
    {
        CONTENT_IDENTITY_ALGORITHM,
        LEGACY_CONTENT_IDENTITY_ALGORITHM,
        FALLBACK_CONTENT_IDENTITY_ALGORITHM,
    }
)
WORK_IDENTITY_ALGORITHM = "semantic-work-key-v1"
MAX_IDENTITY_COMPONENT_CHARS = 512
MAX_POLICY_ENTRIES = 4096
MAX_DIAGNOSTIC_KEYS = 64
MAX_DIAGNOSTIC_VALUE_CHARS = 4096


def _required_text(name: str, value: object, *, limit: int = MAX_IDENTITY_COMPONENT_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value or value.strip() != value or len(value) > limit:
        raise ValueError(f"{name} is empty, untrimmed or too large")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value


def _bounded_tuple(name: str, values: Iterable[object], *, limit: int = MAX_POLICY_ENTRIES) -> tuple[str, ...]:
    selected: list[str] = []
    for value in values:
        if len(selected) >= limit:
            raise ValueError(f"{name} exceeds its bound")
        selected.append(_required_text(name, value))
    # Sorting is deliberate: policy signatures must not depend on caller
    # ordering, while dict.fromkeys keeps a deterministic duplicate collapse.
    return tuple(sorted(dict.fromkeys(selected)))


@dataclass(frozen=True, slots=True)
class PhysicalIdentity:
    """Identity of a physical source anchor, never a mutable path."""

    scheme: str
    value: str
    version: int = 1

    def __post_init__(self) -> None:
        _required_text("physical identity scheme", self.scheme)
        _required_text("physical identity value", self.value)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("physical identity version must be positive")

    @property
    def key(self) -> str:
        return f"physical:{self.scheme}:v{self.version}:{self.value}"

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_IDENTITY_SCHEMA,
            "kind": "physical",
            "scheme": self.scheme,
            "value": self.value,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class VirtualIdentity:
    """Identity of a logical/virtual member below a physical container."""

    scheme: str
    container: str
    member: str
    version: int = 1

    def __post_init__(self) -> None:
        _required_text("virtual identity scheme", self.scheme)
        _required_text("virtual container identity", self.container)
        _required_text("virtual member identity", self.member)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("virtual identity version must be positive")

    @property
    def key(self) -> str:
        return (
            f"virtual:{self.scheme}:v{self.version}:"
            f"{self.container}!/{self.member}"
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_IDENTITY_SCHEMA,
            "kind": "virtual",
            "scheme": self.scheme,
            "container": self.container,
            "member": self.member,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    """Content address used for deterministic cross-location reuse."""

    xxh3_128: str
    byte_count: int
    xxh3_64_guard: str
    algorithm: str = CONTENT_IDENTITY_ALGORITHM

    def __post_init__(self) -> None:
        if self.algorithm not in _SUPPORTED_CONTENT_IDENTITY_ALGORITHMS:
            raise ValueError("unsupported content identity algorithm")
        if len(self.xxh3_128) != 32 or any(
            character not in "0123456789abcdef" for character in self.xxh3_128
        ):
            raise ValueError("content xxh3_128 must be lowercase hexadecimal")
        if len(self.xxh3_64_guard) != 16 or any(
            character not in "0123456789abcdef" for character in self.xxh3_64_guard
        ):
            raise ValueError("content xxh3_64_guard must be lowercase hexadecimal")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("content byte_count must be non-negative")

    @classmethod
    def from_fingerprint(cls, fingerprint: ContentFingerprint) -> ContentIdentity:
        return cls(
            fingerprint.xxh3_128,
            fingerprint.byte_count,
            fingerprint.xxh3_64_guard,
        )

    @property
    def key(self) -> str:
        return (
            f"content:{self.algorithm}:{self.xxh3_128}:"
            f"{self.byte_count}:{self.xxh3_64_guard}"
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_IDENTITY_SCHEMA,
            "kind": "content",
            "algorithm": self.algorithm,
            "xxh3_128": self.xxh3_128,
            "byte_count": self.byte_count,
            "xxh3_64_guard": self.xxh3_64_guard,
        }


@dataclass(frozen=True, slots=True)
class WorkIdentity:
    """Backend-work key independent of physical and virtual locations."""

    model_signature: str
    role: str
    content: ContentIdentity
    processing_signature: str = ""

    def __post_init__(self) -> None:
        _required_text("work model signature", self.model_signature)
        _required_text("work role", self.role)
        if not isinstance(self.content, ContentIdentity):
            raise TypeError("work content must be a ContentIdentity")
        if not isinstance(self.processing_signature, str):
            raise ValueError("work processing signature must be a string")
        if self.processing_signature and self.processing_signature.strip() != self.processing_signature:
            raise ValueError("work processing signature must be trimmed")
        if len(self.processing_signature) > MAX_IDENTITY_COMPONENT_CHARS:
            raise ValueError("work processing signature is too large")

    @property
    def key(self) -> str:
        payload = canonical_json(
            {
                "algorithm": WORK_IDENTITY_ALGORITHM,
                "model_signature": self.model_signature,
                "role": self.role,
                "processing_signature": self.processing_signature,
                "content": self.content.as_payload(),
            }
        ).encode("utf-8")
        return f"work:{WORK_IDENTITY_ALGORITHM}:sha256:{hashlib.sha256(payload).hexdigest()}"

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_IDENTITY_SCHEMA,
            "kind": "work",
            "algorithm": WORK_IDENTITY_ALGORITHM,
            "model_signature": self.model_signature,
            "role": self.role,
            "processing_signature": self.processing_signature,
            "content": self.content.as_payload(),
            "key": self.key,
        }


@dataclass(frozen=True, slots=True)
class SemanticIdentity:
    """Explicitly separated physical, virtual, content and work identities."""

    source_kind: str
    source_identity: str
    item_id: str
    content: ContentIdentity
    physical: PhysicalIdentity | None = None
    virtual: VirtualIdentity | None = None
    work: WorkIdentity | None = None

    def __post_init__(self) -> None:
        _required_text("identity source kind", self.source_kind)
        _required_text("identity source identity", self.source_identity)
        _required_text("identity item id", self.item_id)
        if not isinstance(self.content, ContentIdentity):
            raise TypeError("semantic content identity is invalid")
        if self.physical is not None and not isinstance(self.physical, PhysicalIdentity):
            raise TypeError("semantic physical identity is invalid")
        if self.virtual is not None and not isinstance(self.virtual, VirtualIdentity):
            raise TypeError("semantic virtual identity is invalid")
        if self.work is not None and not isinstance(self.work, WorkIdentity):
            raise TypeError("semantic work identity is invalid")

    @property
    def subject_key(self) -> str:
        # Item identity is the durable source subject.  It is deliberately not
        # replaced by a content key: two locations need separate diagnostics.
        return self.item_id

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_IDENTITY_SCHEMA,
            "source_kind": self.source_kind,
            "source_identity": self.source_identity,
            "item_id": self.item_id,
            "physical": None if self.physical is None else self.physical.as_payload(),
            "virtual": None if self.virtual is None else self.virtual.as_payload(),
            "content": self.content.as_payload(),
            "work": None if self.work is None else self.work.as_payload(),
        }


def _identity_from_explicit(value: object, *, kind: str) -> PhysicalIdentity | VirtualIdentity | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"explicit {kind} identity must be an object")
    if kind == "physical":
        return PhysicalIdentity(
            str(value.get("scheme", "")),
            str(value.get("value", "")),
            int(value.get("version", 1)),
        )
    return VirtualIdentity(
        str(value.get("scheme", "")),
        str(value.get("container", "")),
        str(value.get("member", "")),
        int(value.get("version", 1)),
    )


def _file_identity_from_item(item: SemanticItem) -> PhysicalIdentity | None:
    """Recover only explicit file identity, never use a path as identity."""

    for container in (item.source_revision, item.provenance):
        explicit = container.get("physical_identity")
        if explicit is not None:
            return _identity_from_explicit(explicit, kind="physical")  # type: ignore[return-value]
        if "volume_id" in container and "file_id" in container:
            try:
                raw_volume_id = container["volume_id"]
                raw_file_id = container["file_id"]
                if isinstance(raw_volume_id, bool) or not isinstance(raw_volume_id, (int, str)):
                    continue
                if isinstance(raw_file_id, bool) or not isinstance(raw_file_id, (int, str)):
                    continue
                volume_id = int(raw_volume_id)
                file_id = int(raw_file_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if volume_id >= 0 and file_id >= 0:
                return PhysicalIdentity("file", f"{volume_id:x}:{file_id:x}")
    # File-key parsing is deliberately limited to the canonical stable codec;
    # arbitrary source identities must remain physically unknown.
    try:
        from neocortex.foundation.file_identity import decode_file_identity

        decoded = decode_file_identity(item.source_identity)
    except (ImportError, TypeError, ValueError):
        return None
    return PhysicalIdentity("file", decoded.packed_key)


def _virtual_identity_from_item(item: SemanticItem) -> VirtualIdentity | None:
    for container in (item.source_revision, item.provenance):
        if container.get("inside_zip") is not True and container.get("virtual") is not True:
            continue
        container_key = container.get("container_key") or container.get("container_identity")
        member = container.get("member_chain") or container.get("member")
        if isinstance(container_key, str) and container_key.strip() and isinstance(member, str) and member:
            return VirtualIdentity("archive-member", container_key, member)
    return None


def semantic_identity_for_item(
    item: SemanticItem,
    *,
    model_signature: str | None = None,
    role: EmbeddingRole | str | None = None,
    processing_signature: str | None = None,
) -> SemanticIdentity:
    """Build separated identities from one existing Semantic source item."""

    if not isinstance(item, SemanticItem):
        raise TypeError("semantic identity requires a SemanticItem")
    content = ContentIdentity.from_fingerprint(item.fingerprint)
    selected_work: WorkIdentity | None = None
    if model_signature is not None or role is not None:
        if model_signature is None or role is None:
            raise ValueError("model_signature and role must be supplied together")
        selected_processing: object = processing_signature
        if selected_processing is None:
            selected_processing = item.source_revision.get("processing_signature", "")
        selected_work = WorkIdentity(
            model_signature,
            role.value if isinstance(role, EmbeddingRole) else str(role),
            content,
            str(selected_processing),
        )
    return SemanticIdentity(
        source_kind=item.source_kind,
        source_identity=item.source_identity,
        item_id=item.item_id,
        content=content,
        physical=_file_identity_from_item(item),
        virtual=_virtual_identity_from_item(item),
        work=selected_work,
    )


def work_identity_for_item(
    item: SemanticItem,
    *,
    model_signature: str,
    role: EmbeddingRole | str,
    processing_signature: str | None = None,
) -> WorkIdentity:
    """Return a path-independent key suitable for single-flight reuse."""

    identity = semantic_identity_for_item(
        item,
        model_signature=model_signature,
        role=role,
        processing_signature=processing_signature,
    )
    assert identity.work is not None
    return identity.work


# endregion [01]


# region [02] Deterministic policy and decisions


@dataclass(frozen=True, slots=True)
class ContentAdmissionPolicy:
    """A deterministic per-corpus policy for rebuildable Semantic content."""

    policy_id: str = "semantic-content-admission"
    version: int = 1
    allowed_source_kinds: tuple[str, ...] = ()
    excluded_source_kinds: tuple[str, ...] = ()
    excluded_item_ids: tuple[str, ...] = ()
    excluded_content_keys: tuple[str, ...] = ()
    excluded_physical_keys: tuple[str, ...] = ()
    excluded_virtual_keys: tuple[str, ...] = ()
    min_content_bytes: int = 0
    max_content_bytes: int | None = None

    def __post_init__(self) -> None:
        _required_text("admission policy id", self.policy_id)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("admission policy version must be positive")
        for name in (
            "allowed_source_kinds",
            "excluded_source_kinds",
            "excluded_item_ids",
            "excluded_content_keys",
            "excluded_physical_keys",
            "excluded_virtual_keys",
        ):
            object.__setattr__(self, name, _bounded_tuple(name, getattr(self, name)))
        if type(self.min_content_bytes) is not int or self.min_content_bytes < 0:
            raise ValueError("min_content_bytes must be non-negative")
        if self.max_content_bytes is not None and (
            type(self.max_content_bytes) is not int
            or self.max_content_bytes < self.min_content_bytes
        ):
            raise ValueError("max_content_bytes must be >= min_content_bytes")

    def as_payload(self, *, include_signature: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": ADMISSION_POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "version": self.version,
            "allowed_source_kinds": list(self.allowed_source_kinds),
            "excluded_source_kinds": list(self.excluded_source_kinds),
            "excluded_item_ids": list(self.excluded_item_ids),
            "excluded_content_keys": list(self.excluded_content_keys),
            "excluded_physical_keys": list(self.excluded_physical_keys),
            "excluded_virtual_keys": list(self.excluded_virtual_keys),
            "min_content_bytes": self.min_content_bytes,
            "max_content_bytes": self.max_content_bytes,
        }
        if include_signature:
            payload["policy_signature"] = self.signature
        return payload

    @property
    def signature(self) -> str:
        payload = canonical_json(self.as_payload(include_signature=False)).encode("utf-8")
        return f"semantic-content-admission-policy-v1:sha256:{hashlib.sha256(payload).hexdigest()}"

    def evaluate(self, identity: SemanticIdentity) -> ContentAdmissionDecision:
        if not isinstance(identity, SemanticIdentity):
            raise TypeError("admission evaluation requires a SemanticIdentity")
        reason = "eligible"
        eligible = True
        visible = True
        if self.allowed_source_kinds and identity.source_kind not in self.allowed_source_kinds:
            visible, reason = False, "source_kind_not_allowed"
        elif identity.source_kind in self.excluded_source_kinds:
            visible, reason = False, "source_kind_excluded"
        elif identity.item_id in self.excluded_item_ids:
            visible, reason = False, "item_excluded"
        elif identity.content.key in self.excluded_content_keys:
            visible, reason = False, "content_excluded"
        elif identity.physical is not None and identity.physical.key in self.excluded_physical_keys:
            visible, reason = False, "physical_excluded"
        elif identity.virtual is not None and identity.virtual.key in self.excluded_virtual_keys:
            visible, reason = False, "virtual_excluded"
        elif identity.content.byte_count < self.min_content_bytes:
            eligible = visible = False
            reason = "content_below_minimum"
        elif self.max_content_bytes is not None and identity.content.byte_count > self.max_content_bytes:
            eligible = visible = False
            reason = "content_above_maximum"
        return ContentAdmissionDecision(
            subject_key=identity.subject_key,
            identity=identity,
            policy_signature=self.signature,
            policy_version=self.version,
            eligible=eligible,
            visible=visible,
            reason_code=reason,
            diagnostics={"policy": self.policy_id, "policy_version": self.version},
        )


@dataclass(frozen=True, slots=True)
class ContentAdmissionDecision:
    """Policy result; visibility is separate from retained diagnostics/vectors."""

    subject_key: str
    identity: SemanticIdentity
    policy_signature: str
    policy_version: int
    eligible: bool
    visible: bool
    reason_code: str
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    correction_of: int | None = None

    def __post_init__(self) -> None:
        _required_text("admission subject key", self.subject_key)
        _required_text("admission policy signature", self.policy_signature, limit=1024)
        if self.subject_key != self.identity.subject_key:
            raise ValueError("admission subject key does not match identity")
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ValueError("admission policy version must be positive")
        if type(self.eligible) is not bool or type(self.visible) is not bool:
            raise ValueError("admission flags must be booleans")
        if self.visible and not self.eligible:
            raise ValueError("an ineligible content item cannot be visible")
        _required_text("admission reason code", self.reason_code)
        if self.correction_of is not None and (
            type(self.correction_of) is not int or self.correction_of < 1
        ):
            raise ValueError("correction_of must be a positive integer")
        _validate_diagnostics(self.diagnostics)

    @property
    def excluded(self) -> bool:
        return not self.visible

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": ADMISSION_DECISION_SCHEMA,
            "subject_key": self.subject_key,
            "identity": self.identity.as_payload(),
            "policy_signature": self.policy_signature,
            "policy_version": self.policy_version,
            "eligible": self.eligible,
            "visible": self.visible,
            "reason_code": self.reason_code,
            "diagnostics": dict(self.diagnostics),
            "correction_of": self.correction_of,
        }


def _validate_diagnostics(value: object) -> None:
    if not isinstance(value, Mapping) or len(value) > MAX_DIAGNOSTIC_KEYS:
        raise ValueError("admission diagnostics must be a bounded object")
    for key, selected in value.items():
        _required_text("admission diagnostic key", key, limit=256)
        if isinstance(selected, str):
            if len(selected) > MAX_DIAGNOSTIC_VALUE_CHARS:
                raise ValueError("admission diagnostic value is too large")
        elif isinstance(selected, (bool, int, float)) or selected is None:
            continue
        elif isinstance(selected, (list, tuple)) and len(selected) <= 64:
            if any(not isinstance(item, (str, bool, int, float)) and item is not None for item in selected):
                raise ValueError("admission diagnostic list contains unsupported values")
        else:
            raise ValueError("admission diagnostic value is unsupported")
    # Ensure NaN/Infinity and non-JSON values are rejected consistently.
    canonical_json(dict(value))


def evaluate_content_admission(
    item: SemanticItem,
    policy: ContentAdmissionPolicy,
    *,
    model_signature: str | None = None,
    role: EmbeddingRole | str | None = None,
    processing_signature: str | None = None,
) -> ContentAdmissionDecision:
    """Evaluate one item without reading its path or content bytes."""

    return policy.evaluate(
        semantic_identity_for_item(
            item,
            model_signature=model_signature,
            role=role,
            processing_signature=processing_signature,
        )
    )


# endregion [02]


# region [03] Read-only projections and in-process single-flight


def filter_semantic_items(
    items: Iterable[SemanticItem],
    policy: ContentAdmissionPolicy,
) -> Iterator[SemanticItem]:
    """Yield only visible items while retaining the original objects elsewhere."""

    for item in items:
        if evaluate_content_admission(item, policy).visible:
            yield item


def filter_text_source_records(
    records: Iterable[Any],
    policy: ContentAdmissionPolicy,
) -> Iterator[Any]:
    """Apply policy visibility to source records without mutating owner caches."""

    for record in records:
        item = getattr(record, "item", None)
        if isinstance(item, SemanticItem) and evaluate_content_admission(item, policy).visible:
            yield record


def filter_search_hits(
    hits: Iterable[Any],
    policy: ContentAdmissionPolicy,
) -> Iterator[Any]:
    """Suppress current result hits by durable item identity, not by deleting vectors."""

    excluded = frozenset(policy.excluded_item_ids)
    excluded_sources = frozenset(policy.excluded_source_kinds)
    for hit in hits:
        # Search returns both raw ``ResolvedSearchHit`` values and the public
        # ``FusedResolvedHit`` envelope.  Resolve the stable identity from
        # either shape instead of silently letting an excluded item through.
        fused = getattr(hit, "fused", None)
        item_id = getattr(hit, "item_id", None)
        if item_id is None:
            item_id = getattr(fused, "item_id", None)
        provenance = getattr(hit, "provenance", {})
        source_kind = provenance.get("source_kind") if isinstance(provenance, Mapping) else None
        if source_kind is None:
            source_kind = getattr(hit, "source_kind", None)
        if source_kind is None:
            source_kind = getattr(fused, "source_kind", None)
        if item_id in excluded or source_kind in excluded_sources:
            continue
        yield hit


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SingleFlightResult:
    """Outcome metadata for one path-independent unit of backend work."""

    work_key: str
    value: object
    leader: bool
    reused: bool


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    value: object = None
    error: BaseException | None = None


class SemanticSingleFlight:
    """Coalesce concurrent work by ``WorkIdentity.key`` without provider calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._completed: dict[str, object] = {}

    def clear(self) -> None:
        with self._lock:
            self._flights.clear()
            self._completed.clear()

    def run(self, work: WorkIdentity | str, producer: Callable[[], T]) -> SingleFlightResult:
        key = work.key if isinstance(work, WorkIdentity) else _required_text("work key", work, limit=2048)
        if not callable(producer):
            raise TypeError("single-flight producer must be callable")
        with self._lock:
            if key in self._completed:
                return SingleFlightResult(key, self._completed[key], False, True)
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                leader = True
            else:
                leader = False
        if not leader:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return SingleFlightResult(key, flight.value, False, True)
        try:
            value = producer()
        except BaseException as exc:
            with self._lock:
                flight.error = exc
                self._flights.pop(key, None)
                flight.event.set()
            raise
        with self._lock:
            flight.value = value
            self._completed[key] = value
            self._flights.pop(key, None)
            flight.event.set()
        return SingleFlightResult(key, value, True, False)


def group_reusable_work(
    items: Iterable[SemanticItem],
    *,
    model_signature: str,
    role: EmbeddingRole | str,
    processing_signature: str | None = None,
) -> dict[str, tuple[SemanticItem, ...]]:
    """Group locations by content work identity while preserving diagnostics."""

    groups: dict[str, list[SemanticItem]] = {}
    for item in items:
        work = work_identity_for_item(
            item,
            model_signature=model_signature,
            role=role,
            processing_signature=processing_signature,
        )
        groups.setdefault(work.key, []).append(item)
    return {key: tuple(values) for key, values in groups.items()}


def corpus_key_for(value: object) -> str:
    """Build a stable corpus key from a policy/root without using path aliases."""

    if isinstance(value, str):
        return _required_text("corpus key", value, limit=2048)
    root = getattr(value, "root", None)
    if root is None:
        raise TypeError("corpus key requires a string or a CorpusAccessPolicy-like value")
    identity = {
        "root": os.fspath(Path(root)),
        "root_device_id": getattr(value, "root_device_id", None),
        "root_file_id": getattr(value, "root_file_id", None),
        "root_birthtime_ns": getattr(value, "root_birthtime_ns", None),
    }
    payload = canonical_json(identity).encode("utf-8")
    return f"corpus:semantic:v1:sha256:{hashlib.sha256(payload).hexdigest()}"


def framework_content_admission(owner: object):
    """Construct the durable ledger lazily, keeping Semantic imports light."""

    from neocortex.persistence.framework_content_admission import ContentAdmissionLedger

    return ContentAdmissionLedger(owner)


# Friendly compatibility aliases for callers wiring the small boundary.
ContentIdentityRef = ContentIdentity
PhysicalIdentityRef = PhysicalIdentity
VirtualIdentityRef = VirtualIdentity
WorkIdentityRef = WorkIdentity
SemanticContentAdmissionPolicy = ContentAdmissionPolicy
SemanticContentAdmissionDecision = ContentAdmissionDecision
SingleFlight = SemanticSingleFlight
admit_content = evaluate_content_admission
identity_for_item = semantic_identity_for_item


__all__ = (
    "ADMISSION_DECISION_SCHEMA",
    "ADMISSION_IDENTITY_SCHEMA",
    "ADMISSION_POLICY_SCHEMA",
    "CONTENT_IDENTITY_ALGORITHM",
    "FALLBACK_CONTENT_IDENTITY_ALGORITHM",
    "LEGACY_CONTENT_IDENTITY_ALGORITHM",
    "WORK_IDENTITY_ALGORITHM",
    "ContentAdmissionDecision",
    "ContentAdmissionPolicy",
    "ContentIdentity",
    "ContentIdentityRef",
    "PhysicalIdentity",
    "PhysicalIdentityRef",
    "SemanticContentAdmissionDecision",
    "SemanticContentAdmissionPolicy",
    "SemanticIdentity",
    "SemanticSingleFlight",
    "SingleFlight",
    "SingleFlightResult",
    "VirtualIdentity",
    "VirtualIdentityRef",
    "WorkIdentity",
    "WorkIdentityRef",
    "admit_content",
    "corpus_key_for",
    "evaluate_content_admission",
    "filter_search_hits",
    "filter_semantic_items",
    "filter_text_source_records",
    "framework_content_admission",
    "group_reusable_work",
    "identity_for_item",
    "semantic_identity_for_item",
    "work_identity_for_item",
)
