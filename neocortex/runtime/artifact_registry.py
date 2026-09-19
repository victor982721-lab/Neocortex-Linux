"""Private, manifest-backed registry for local runtime artifacts.

The registry is deliberately smaller than a general filesystem inventory.  A
path is managed only when an authenticated manifest in the configured registry
root claims it; names that merely happen to be nearby are reported as
unmanaged and are never adopted.  ``plan`` and ``verify`` are read-only.  This
module does not remove, move, copy, or otherwise modify an artifact path.

Manifests contain bounded metadata and physical identity observations, not
artifact contents.  Every write uses a temporary file followed by an atomic
publication and a directory fsync.  The final manifest, registry root, and
claimed artifact are revalidated without following symlinks before a record is
considered eligible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any


# region [01] Public contract and bounds

ARTIFACT_REGISTRY_SCHEMA = "neocortex.artifact-registry/v1"
MANIFEST_SUFFIX = ".json"

MAX_MANIFEST_BYTES = 512 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_REASON_BYTES = 8 * 1024
MAX_TEXT_BYTES = 8 * 1024
MAX_ARTIFACT_ID_BYTES = 256
MAX_RUN_ID_BYTES = 256
MAX_DEPENDENCIES = 256
MAX_DEPENDENCY_BYTES = 256
MAX_RECORDS = 100_000
MAX_SCAN_BYTES = 4 * 1024 * 1024 * 1024

_DEFAULT_OWNER = "neocortex"
_DEFAULT_PRODUCER = "neocortex"
_DEFAULT_PURPOSE = "runtime-artifact"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_UNSET = object()


class ArtifactKind(StrEnum):
    """Closed vocabulary for the lifecycle/retention role of an artifact."""

    CANONICAL = "canonical"
    OPERATIONAL = "operational"
    REBUILDABLE = "rebuildable"
    TEMPORARY = "temporary"
    CACHE = "cache"
    EXTERNAL = "external"


class ArtifactState(StrEnum):
    """Closed vocabulary for one registered artifact lifecycle."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    RETIRED = "retired"


ARTIFACT_KINDS = frozenset(item.value for item in ArtifactKind)
ARTIFACT_STATES = frozenset(item.value for item in ArtifactState)
_LIVE_DEPENDENT_STATES = frozenset(
    {
        ArtifactState.ACTIVE.value,
        ArtifactState.FAILED.value,
        ArtifactState.RECOVERY_REQUIRED.value,
    }
)
# ``dependencies`` are live claims for the registry owner.  A retirement
# intent is kept in the same manifest (rather than in a second journal) so a
# process may recover an effect after the path has disappeared.  The reserved
# metadata key is deliberately namespaced; producer metadata remains opaque
# to the registry except for this lifecycle claim.
_RETIREMENT_KEY = "neocortex_retirement"
_RETIREMENT_PENDING_PHASES = frozenset(
    {"prepared", "applying", "applied_unverified", "recovery_required"}
)
_RETIREMENT_CONFIRMED_PHASE = "confirmed"
_TOMBSTONE_RETENTION_SCHEMA = "neocortex.artifact-tombstone-retention/v1"
_TOMBSTONE_RETENTION_DIR = ".tombstone-retention"
_MAX_RETENTION_RECEIPT_BYTES = 256 * 1024
_DISPOSABLE_KINDS = frozenset(
    {
        ArtifactKind.REBUILDABLE.value,
        ArtifactKind.TEMPORARY.value,
        ArtifactKind.CACHE.value,
    }
)
_BLOCKING_ISSUES = frozenset(
    {
        "artifact_missing",
        "artifact_identity_drift",
        "artifact_root_drift",
        "artifact_owner_drift",
        "artifact_permission_drift",
        "artifact_symlink",
        "artifact_hardlink",
        "artifact_type_drift",
        "root_identity_drift",
        "root_owner_drift",
        "root_permission_drift",
        "root_type_drift",
        "size_truncated",
    }
)


class ArtifactRegistryError(RuntimeError):
    """Base error for a registry contract violation."""


class ArtifactSecurityError(ArtifactRegistryError):
    """A root, manifest, or artifact failed a fail-closed safety claim."""


class ArtifactRootError(ArtifactSecurityError):
    """The configured registry root is absent or not a private directory."""


class ArtifactManifestError(ArtifactSecurityError):
    """A manifest is malformed, tampered with, or insufficiently protected."""


class ArtifactConflictError(ArtifactRegistryError):
    """An artifact id is already bound to a different registration."""


def _canonical_json(value: object) -> str:
    """Encode JSON deterministically while refusing non-JSON or NaN values."""

    rendered = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # Keep existing UTF-8 manifest digests byte-for-byte. POSIX surrogateescape
    # names use a reversible JSON escape rather than failing UTF-8 encoding.
    if any(0xD800 <= ord(character) <= 0xDFFF for character in rendered):
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                          separators=(",", ":"))
    return rendered


def _bounded_json(value: Any, *, label: str, limit: int) -> Any:
    """Return a JSON-compatible value whose canonical form is bounded."""

    try:
        encoded = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain only finite JSON values") from exc
    if len(encoded) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    # A round-trip removes custom Mapping implementations and gives callers a
    # detached value.  It intentionally does not retain file contents.
    try:
        return json.loads(encoded.decode("utf-8"))
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise ValueError(f"{label} is not a JSON object") from exc


def _bounded_mapping(
    value: Mapping[str, Any] | None,
    *,
    label: str,
    limit: int = MAX_METADATA_BYTES,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    normalized = _bounded_json(dict(value), label=label, limit=limit)
    if not isinstance(normalized, dict):  # pragma: no cover - dict input invariant
        raise TypeError(f"{label} must be an object")
    if any(not isinstance(key, str) or not key for key in normalized):
        raise ValueError(f"{label} keys must be non-empty strings")
    return normalized


def _bounded_text(
    value: object,
    *,
    label: str,
    limit: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be blank")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _bounded_run_id(value: object) -> int | str | None:
    if value is None:
        return None
    if type(value) is int:
        if value < 1:
            raise ValueError("artifact run_id must be a positive integer or string")
        return value
    if isinstance(value, str):
        return _bounded_text(value, label="artifact run_id", limit=MAX_RUN_ID_BYTES)
    raise TypeError("artifact run_id must be a positive integer, string, or null")


def _bounded_identity(value: object, *, label: str) -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain three integers")
    if any(type(item) is not int for item in value):
        raise ValueError(f"{label} must contain three integers")
    # Linux may expose birthtime as -1 when it is unavailable.  Device and
    # inode are unsigned in practice; rejecting negative values here prevents
    # forged identities from being mistaken for observations.
    if value[0] < 0 or value[1] < 0 or value[2] < -1:
        raise ValueError(f"{label} contains an invalid physical identity")
    return int(value[0]), int(value[1]), int(value[2])


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    # Linux does not consistently expose birthtime.  ctime is not a creation
    # timestamp and is never substituted here.
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(birthtime) if birthtime is not None else -1,
    )


def _validate_absolute_path(value: Path | str, *, label: str) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid path") from exc
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    # ``absolute`` normalizes ``..`` lexically but does not follow symlinks.
    try:
        normalized = Path(os.path.abspath(os.fspath(path)))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid path") from exc
    if "\x00" in str(normalized):
        raise ValueError(f"{label} contains NUL")
    return normalized


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ArtifactSecurityError(f"{label} could not be inspected") from exc


def _same_identity(path: Path, expected: Sequence[int]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if len(expected) != 3 or stat.S_ISLNK(metadata.st_mode):
        return False
    try:
        identity = _bounded_identity(expected, label="expected identity")
    except (TypeError, ValueError):
        return False
    return _identity(metadata) == identity


def _private_directory_issue(metadata: os.stat_result, *, root: bool) -> str | None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return "root_type_drift" if root else "artifact_type_drift"
    if metadata.st_uid != os.geteuid():
        return "root_owner_drift" if root else "artifact_owner_drift"
    if metadata.st_mode & 0o077:
        return "root_permission_drift" if root else "artifact_permission_drift"
    return None


def _private_artifact_issue(metadata: os.stat_result) -> str | None:
    if stat.S_ISLNK(metadata.st_mode):
        return "artifact_symlink"
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        return "artifact_type_drift"
    if metadata.st_uid != os.geteuid():
        return "artifact_owner_drift"
    if metadata.st_mode & 0o077:
        return "artifact_permission_drift"
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        return "artifact_hardlink"
    return None


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _safe_manifest_name(artifact_id: str) -> str:
    """Return a deterministic filename without interpreting id text as a path."""

    if _SAFE_ID.fullmatch(artifact_id) is not None and not artifact_id.startswith("."):
        return f"artifact-{artifact_id}{MANIFEST_SUFFIX}"
    digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
    return f"artifact-{digest}{MANIFEST_SUFFIX}"


def _validate_limit(value: object, *, label: str, allow_zero: bool = False) -> int:
    if type(value) is not int or value < 0 or (not allow_zero and value == 0):
        suffix = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a {suffix} integer")
    return value


def _directory_size_no_follow(
    path: Path,
    *,
    max_entries: int,
    max_bytes: int,
    profile: str = "strict",
) -> tuple[int, str | None, bool]:
    """Use the owner's shared descriptor and mount observer for all profiles."""
    from neocortex.runtime.scratch_tree import observe_claimed_tree
    observed = observe_claimed_tree(path, limit=max_entries, max_bytes=max_bytes,
                                    profile=profile, include_control_manifest=True)
    issue = _artifact_observation_issue(observed.issue)
    return observed.apparent_bytes, issue, issue == "size_truncated"


def _artifact_observation_issue(issue: str | None) -> str | None:
    if issue is None:
        return None
    return {
        "symlink_payload": "artifact_symlink", "hardlink_payload": "artifact_hardlink",
        "payload_owner_drift": "artifact_owner_drift", "payload_type_drift": "artifact_type_drift",
        "socket_payload": "artifact_type_drift", "device_payload": "artifact_type_drift",
        "entry_limit": "size_truncated", "byte_limit": "size_truncated",
        "depth_limit": "size_truncated", "fd_limit": "size_truncated",
    }.get(issue, issue)


def _path_size_no_follow(path: Path, *, max_entries: int, max_bytes: int,
                         profile: str = "strict") -> tuple[int, str | None]:
    try:
        metadata = path.lstat()
    except OSError:
        return 0, "artifact_missing"
    if stat.S_ISREG(metadata.st_mode):
        size = max(0, int(metadata.st_size))
        if size > max_bytes:
            # Preserve the physical metadata separately from the credited
            # observation: a clipped file is not a complete, eligible claim.
            return max_bytes, "size_truncated"
        return size, None
    if stat.S_ISDIR(metadata.st_mode):
        size, issue, _ = _directory_size_no_follow(
            path,
            max_entries=max_entries,
            max_bytes=max_bytes, profile=profile,
        )
        return size, issue
    return 0, "artifact_type_drift"


# endregion [01]


# region [02] Public dataclasses


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One durable artifact claim plus its latest bounded observation."""

    artifact_id: str
    owner: str
    producer: str
    run_id: int | str | None
    purpose: str
    path: Path
    root: Path
    path_identity: tuple[int, int, int] | None
    root_identity: tuple[int, int, int] | None
    kind: str
    state: str
    created_ns: int
    updated_ns: int
    source_ref: Any = None
    digest: str | None = None
    dependencies: tuple[str, ...] = ()
    retain_until_ns: int | None = None
    ttl_ns: int | None = None
    disposable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Size and mtime are observations, not identity.  Keeping them alongside
    # the no-follow identity closes the inode-reuse gap on filesystems that do
    # not expose a birth time (where ``birthtime_ns`` is stored as -1).
    path_size_bytes: int | None = None
    path_mtime_ns: int | None = None
    size_bytes: int = 0
    valid: bool = True
    issue: str | None = None
    reason: str | None = None
    classification: str | None = None
    eligible: bool = False
    manifest_digest: str | None = None
    manifest_path: Path | None = None
    allocated_bytes: int | None = None
    observed_payload_entries: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_absolute_path(self.path, label="artifact path"))
        object.__setattr__(self, "root", _validate_absolute_path(self.root, label="artifact root"))
        _bounded_text(self.artifact_id, label="artifact_id", limit=MAX_ARTIFACT_ID_BYTES)
        _bounded_text(self.owner, label="artifact owner")
        _bounded_text(self.producer, label="artifact producer")
        _bounded_text(self.purpose, label="artifact purpose")
        _bounded_run_id(self.run_id)
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind: {self.kind!r}")
        if self.state not in ARTIFACT_STATES:
            raise ValueError(f"unsupported artifact state: {self.state!r}")
        for name in ("created_ns", "updated_ns"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"artifact {name} must be a non-negative integer")
        if self.updated_ns < self.created_ns:
            raise ValueError("artifact updated_ns cannot precede created_ns")
        for name in ("retain_until_ns", "ttl_ns"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"artifact {name} must be a non-negative integer or null")
        if type(self.disposable) is not bool:
            raise TypeError("artifact disposable must be a boolean")
        if self.path_identity is not None:
            object.__setattr__(
                self,
                "path_identity",
                _bounded_identity(self.path_identity, label="artifact path_identity"),
            )
        if self.root_identity is not None:
            object.__setattr__(
                self,
                "root_identity",
                _bounded_identity(self.root_identity, label="artifact root_identity"),
            )
        object.__setattr__(
            self,
            "metadata",
            _bounded_mapping(self.metadata, label="artifact metadata"),
        )
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be a non-negative integer")
        for name in ("path_size_bytes", "path_mtime_ns"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"artifact {name} must be a non-negative integer or null")
        if self.issue is not None:
            _bounded_text(self.issue, label="artifact issue", limit=MAX_REASON_BYTES)
        if self.reason is not None:
            _bounded_text(self.reason, label="artifact reason", limit=MAX_REASON_BYTES)
        if self.manifest_path is not None:
            object.__setattr__(
                self,
                "manifest_path",
                _validate_absolute_path(self.manifest_path, label="artifact manifest path"),
            )

    @property
    def identity(self) -> tuple[int, int, int] | None:
        """Compatibility alias for the no-follow physical artifact identity."""

        return self.path_identity

    @property
    def root_path(self) -> Path:
        return self.root

    @property
    def status(self) -> str:
        return self.state

    @property
    def ttl(self) -> int | None:
        return self.ttl_ns

    @property
    def verified(self) -> bool:
        return self.valid and self.issue is None

    def __bool__(self) -> bool:
        # This makes ``assert registry.verify(id)`` useful while preserving a
        # detailed record for callers that need the reason and identity.
        return self.verified

    def effective_retain_until_ns(self) -> int | None:
        if self.retain_until_ns is not None:
            return self.retain_until_ns
        if self.ttl_ns is not None:
            return self.created_ns + self.ttl_ns
        return None

    def to_dict(self) -> dict[str, object]:
        from neocortex.runtime.path_identity import PathIdentity

        return {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "artifact_id": self.artifact_id,
            "owner": self.owner,
            "producer": self.producer,
            "run_id": self.run_id,
            "purpose": self.purpose,
            "path": str(self.path),
            "root": str(self.root),
            "posix_path_identity": PathIdentity.from_path(self.path).as_dict(),
            "posix_root_identity": PathIdentity.from_path(self.root).as_dict(),
            "path_identity": None if self.path_identity is None else list(self.path_identity),
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "identity": None if self.path_identity is None else list(self.path_identity),
            "kind": self.kind,
            "state": self.state,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
            "source_ref": self.source_ref,
            "digest": self.digest,
            "dependencies": list(self.dependencies),
            "retain_until_ns": self.retain_until_ns,
            "ttl_ns": self.ttl_ns,
            "disposable": self.disposable,
            "metadata": dict(self.metadata),
            "path_size_bytes": self.path_size_bytes,
            "path_mtime_ns": self.path_mtime_ns,
            "size_bytes": self.size_bytes,
            "apparent_bytes": self.size_bytes,
            "allocated_bytes": self.allocated_bytes,
            "exclusive_reclaimable_bytes": None,
            "observed_payload_entries": self.observed_payload_entries,
            "coverage_complete": self.verified,
            "valid": self.valid,
            "verified": self.verified,
            "issue": self.issue,
            "reason": self.reason,
            "classification": self.classification,
            "eligible": self.eligible,
            "manifest_digest": self.manifest_digest,
            "manifest_path": None if self.manifest_path is None else str(self.manifest_path),
        }


@dataclass(frozen=True, slots=True)
class ArtifactPlan:
    """Bounded, read-only classification of registered artifacts."""

    root: Path
    records: tuple[ArtifactRecord, ...] = ()
    unmanaged: tuple[Path, ...] = ()
    protected: int = 0
    eligible: int = 0
    blocked: int = 0
    unknown: int = 0
    protected_bytes: int = 0
    eligible_bytes: int = 0
    blocked_bytes: int = 0
    unknown_bytes: int = 0
    unmanaged_bytes: int = 0
    scanned: int = 0
    returned: int = 0
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    reasons: Mapping[str, int] = field(default_factory=dict)
    status: str = "planned"
    reason: str | None = None
    read_only: bool = True
    root_identity: tuple[int, int, int] | None = None
    root_blocked: str | None = None
    max_records: int = MAX_RECORDS
    max_bytes: int = MAX_SCAN_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _validate_absolute_path(self.root, label="artifact registry root"))
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "unmanaged", tuple(Path(path) for path in self.unmanaged))
        if self.root_identity is not None:
            object.__setattr__(
                self,
                "root_identity",
                _bounded_identity(self.root_identity, label="plan root_identity"),
            )
        object.__setattr__(self, "reasons", dict(self.reasons))
        if self.reason is not None:
            _bounded_text(self.reason, label="artifact plan reason", limit=MAX_REASON_BYTES)

    @property
    def items(self) -> tuple[ArtifactRecord, ...]:
        return self.records

    @property
    def entries(self) -> tuple[ArtifactRecord, ...]:
        return self.records

    @property
    def planned(self) -> int:
        return self.eligible

    @property
    def planned_bytes(self) -> int:
        return self.eligible_bytes

    @property
    def eligible_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(record for record in self.records if record.classification == "eligible")

    @property
    def protected_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(record for record in self.records if record.classification == "protected")

    @property
    def blocked_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(record for record in self.records if record.classification == "blocked")

    @property
    def unknown_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(record for record in self.records if record.classification == "unknown")

    @property
    def unmanaged_count(self) -> int:
        return len(self.unmanaged)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "returned": self.returned,
            "protected": self.protected,
            "eligible": self.eligible,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "unmanaged": len(self.unmanaged),
        }

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "protected": self.protected_bytes,
            "eligible": self.eligible_bytes,
            "blocked": self.blocked_bytes,
            "unknown": self.unknown_bytes,
            "unmanaged": self.unmanaged_bytes,
        }

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(self.reasons)

    @property
    def reason_summary(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {"reason": reason, "count": count}
            for reason, count in sorted(self.reasons.items(), key=lambda item: (-item[1], item[0]))
        )

    @property
    def limits(self) -> dict[str, int]:
        return {"max_records": self.max_records, "max_bytes": self.max_bytes}

    def to_dict(self) -> dict[str, object]:
        unique_records = {record.path_identity: record for record in self.records
                          if record.path_identity is not None}
        coverage_complete = (not self.truncated and not self.unmanaged
                             and all(record.verified for record in self.records))
        allocated = (sum(record.allocated_bytes or 0 for record in unique_records.values())
                     if coverage_complete and all(record.allocated_bytes is not None
                                                  for record in unique_records.values()) else None)
        return {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "root": str(self.root),
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "root_blocked": self.root_blocked,
            "accounting": {"observed_apparent_bytes": sum(record.size_bytes for record in unique_records.values()),
                           "observed_allocated_bytes": allocated,
                           "exclusive_reclaimable_bytes": None,
                           "unknown_bytes": 0 if coverage_complete else None,
                           "coverage_complete": coverage_complete,
                           "allocation_semantics": "observed st_blocks; shared and reflink blocks are not exclusive"},
            "status": self.status,
            "reason": self.reason,
            "read_only": self.read_only,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "counts": self.counts,
            "bytes": self.bytes,
            "records": [record.to_dict() for record in self.records],
            "unmanaged": [str(path) for path in self.unmanaged],
            "reasons": dict(self.reasons),
            "reason_summary": list(self.reason_summary),
            "limits": self.limits,
        }


class _RetirementBatch:
    """Registry-lock-scoped batch used by ScratchManager.apply().

    Keeping the directory lock for one bounded batch lets every target reuse
    the same dependency observation.  Each target is still reloaded and
    revalidated immediately before its own effect; the batch is an
    optimization of discovery, not an authorization cache.
    """

    def __init__(
        self,
        registry: "ArtifactRegistry",
        records: tuple[ArtifactRecord, ...],
        unmanaged: tuple[Path, ...],
        truncated: bool,
    ) -> None:
        self.registry = registry
        self.records = records
        self.unmanaged = unmanaged
        self.truncated = truncated

    @contextmanager
    def guard(self, artifact: str | ArtifactRecord) -> Iterator[ArtifactRecord]:
        yield from self.registry._retirement_guard_locked(
            artifact,
            records=self.records,
            unmanaged=self.unmanaged,
            truncated=self.truncated,
        )


# endregion [02]


# region [03] Atomic storage and registry


_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "artifact_id",
        "owner",
        "producer",
        "run_id",
        "purpose",
        "path",
        "root",
        "path_identity",
        "root_identity",
        "kind",
        "state",
        "created_ns",
        "updated_ns",
        "source_ref",
        "digest",
        "dependencies",
        "retain_until_ns",
        "ttl_ns",
        "disposable",
        "metadata",
        "path_size_bytes",
        "path_mtime_ns",
        "manifest_digest",
    }
)


def _write_json_atomic(path: Path, payload: Mapping[str, Any], *, exclusive: bool) -> None:
    """Publish one bounded JSON document without exposing a partial payload."""

    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("artifact manifest exceeds the durable size limit")
    parent = path.parent
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # link()+unlink() gives an atomic no-clobber publication on Linux:
            # a replay/race cannot replace a manifest owned by another claim.
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _manifest_file_issue(metadata: os.stat_result) -> str | None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "manifest_type_drift"
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        return "manifest_protection_drift"
    return None


def _retention_receipt_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("receipt_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _retention_receipt_name(operation_id: str) -> str:
    return f"receipt-{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()}.json"


def _registry_write_locked(method: Any) -> Any:
    """Serialize manifest reads+writes across registry processes."""

    @wraps(method)
    def wrapped(self: "ArtifactRegistry", *args: Any, **kwargs: Any) -> Any:
        # Registration historically creates its configured root on demand;
        # updates must remain read-only with respect to an absent root.
        self._ensure_root(create=method.__name__ == "register")
        with self._registry_lock():
            return method(self, *args, **kwargs)

    return wrapped


class ArtifactRegistry:
    """Own a private manifest root and revalidate its registered artifacts."""

    def __init__(
        self,
        root: Path | str,
        *,
        owner: str | None = _DEFAULT_OWNER,
        create_root: bool = False,
        max_records: int = MAX_RECORDS,
        max_entries: int | None = None,
        max_bytes: int = MAX_SCAN_BYTES,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
        max_metadata_bytes: int = MAX_METADATA_BYTES,
    ) -> None:
        self.root = _validate_absolute_path(root, label="artifact registry root")
        # ``owner=None`` is an explicitly read-only federated view used by
        # hygiene orchestration to inspect manifests written by multiple
        # producer owners in one registry root.  Registration/update always
        # require an exact owner and therefore cannot accidentally use this
        # view as an authority.
        self.owner = (
            None
            if owner is None
            else _bounded_text(owner, label="artifact registry owner", limit=MAX_TEXT_BYTES)
        )
        self.create_root = create_root
        if type(create_root) is not bool:
            raise TypeError("artifact create_root must be a boolean")
        if max_entries is not None:
            max_records = max_entries
        self.max_records = _validate_limit(max_records, label="artifact max_records")
        self.max_bytes = _validate_limit(max_bytes, label="artifact max_bytes")
        self.max_manifest_bytes = _validate_limit(
            max_manifest_bytes,
            label="artifact max_manifest_bytes",
        )
        self.max_metadata_bytes = _validate_limit(
            max_metadata_bytes,
            label="artifact max_metadata_bytes",
        )
        self._lock_local = threading.local()
        if create_root:
            self._ensure_root(create=True)

    # -- root and manifest primitives ---------------------------------

    def _ensure_root(self, *, create: bool) -> bool:
        try:
            metadata = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            except FileExistsError:
                pass
            try:
                metadata = self.root.lstat()
            except OSError as exc:
                raise ArtifactRootError("artifact registry root could not be inspected") from exc
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be inspected") from exc
        issue = _private_directory_issue(metadata, root=True)
        if issue is not None:
            raise ArtifactRootError(f"artifact registry root failed safety check: {issue}")
        return True

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        """Hold an OS lock on the registry directory without creating files."""

        active_fd = getattr(self._lock_local, "fd", None)
        if active_fd is not None:
            # Nested calls from ScratchManager's retirement guard use the same
            # registry object.  Reusing the descriptor avoids a second flock
            # while preserving the outer process-wide critical section.
            yield
            return
        try:
            import fcntl

            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be locked") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_local.fd = fd
            yield
        finally:
            self._lock_local.fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _manifest_path(self, artifact_id: str) -> Path:
        return self.root / _safe_manifest_name(artifact_id)

    def manifest_path(self, artifact_id: str) -> Path:
        """Return the deterministic manifest path for an artifact id."""

        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        return self._manifest_path(artifact_id)

    def tombstone_retention_receipt_path(self, operation_id: str) -> Path:
        """Locate an owner's existing terminal-retention receipt without IO."""
        operation = _bounded_text(operation_id, label="tombstone retention operation", limit=128)
        return self.root / _TOMBSTONE_RETENTION_DIR / _retention_receipt_name(operation)

    @staticmethod
    def _normalize_dependencies(value: Iterable[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise TypeError("artifact dependencies must be an iterable of strings")
        values: list[str] = []
        for dependency in value:
            normalized = _bounded_text(
                dependency,
                label="artifact dependency",
                limit=MAX_DEPENDENCY_BYTES,
            )
            values.append(normalized)
            if len(values) > MAX_DEPENDENCIES:
                raise ValueError("artifact dependencies exceed the durable limit")
        return tuple(sorted(set(values)))

    @staticmethod
    def _retirement_claim(record: ArtifactRecord) -> dict[str, Any] | None:
        """Return the reserved retirement claim, if one is well formed.

        The claim lives in the owner-controlled manifest metadata.  A
        malformed claim is not treated as absent: callers that need a safety
        decision must abstain instead of silently reverting to the ordinary
        completed/disposable policy.
        """

        value = record.metadata.get(_RETIREMENT_KEY)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ArtifactSecurityError("retirement claim is not an object")
        phase = value.get("phase")
        operation_id = value.get("operation_id")
        if phase not in _RETIREMENT_PENDING_PHASES | {_RETIREMENT_CONFIRMED_PHASE}:
            raise ArtifactSecurityError("retirement claim has an unsupported phase")
        if not isinstance(operation_id, str) or not operation_id:
            raise ArtifactSecurityError("retirement claim has no operation id")
        return dict(value)

    def _metadata_with_retirement(
        self,
        record: ArtifactRecord,
        *,
        phase: str,
        operation_id: str,
        **observations: Any,
    ) -> dict[str, Any]:
        """Build bounded metadata for one durable retirement transition."""

        if phase not in _RETIREMENT_PENDING_PHASES | {_RETIREMENT_CONFIRMED_PHASE}:
            raise ValueError("unsupported retirement phase")
        metadata = dict(record.metadata)
        existing = metadata.get(_RETIREMENT_KEY)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise ArtifactSecurityError("retirement claim is not an object")
            previous_id = existing.get("operation_id")
            if previous_id != operation_id:
                raise ArtifactSecurityError("a different retirement operation is pending")
            claim = dict(existing)
        else:
            claim = {
                "operation_id": operation_id,
                "expected_path_identity": list(record.path_identity or ()),
                "expected_size_bytes": record.path_size_bytes,
                "expected_mtime_ns": record.path_mtime_ns,
                "prepared_ns": time.time_ns(),
            }
        claim["phase"] = phase
        claim.update(observations)
        metadata[_RETIREMENT_KEY] = claim
        # Use the same bounded metadata validator as ordinary registration;
        # it prevents a producer from turning the recovery receipt into an
        # unbounded side channel.
        return _bounded_mapping(
            metadata,
            label="artifact metadata",
            limit=self.max_metadata_bytes,
        )

    def _dependency_observation_complete(
        self,
        records: Sequence[ArtifactRecord],
        unmanaged: Sequence[Path] = (),
        *,
        truncated: bool = False,
    ) -> bool:
        """Whether the registry view can prove absence of live consumers.

        An invalid manifest, an unmanaged registry entry, or a bounded scan is
        an incomplete view of the owner-controlled dependency universe.  It is
        safer to preserve a candidate than to interpret a lost dependency list
        as an empty one.  This is intentionally scoped to this registry root,
        not to every filesystem path on the machine.
        """

        return not truncated and not unmanaged and all(
            record.valid or self.retired_claim_is_historical(record, records)
            for record in records
        )

    @staticmethod
    def retired_claim_is_historical(record: ArtifactRecord,
                                    records: Sequence[ArtifactRecord]) -> bool:
        """Recognize a confirmed old tombstone superseded by a verified claim.

        This only completes dependency observation. It cannot authorize an
        effect against the old identity or the new occupant of the path.
        """
        if record.state != ArtifactState.RETIRED.value or record.issue != "artifact_identity_drift":
            return False
        try:
            claim = ArtifactRegistry._retirement_claim(record)
        except ArtifactSecurityError:
            return False
        if (claim is None or claim.get("phase") != _RETIREMENT_CONFIRMED_PHASE
                or claim.get("observed_path_exists") is not False
                or claim.get("expected_path_identity") != list(record.path_identity or ())):
            return False
        return any(other.artifact_id != record.artifact_id and other.verified
                   and other.state != ArtifactState.RETIRED.value and other.path == record.path
                   and other.path_identity is not None and other.path_identity != record.path_identity
                   for other in records)

    def _validate_dependencies_locked(
        self,
        dependencies: Sequence[str],
        *,
        artifact_id: str,
    ) -> None:
        """Validate live dependency acquisitions while holding the registry lock.

        Dependency names in this owner registry are live artifact claims, not
        free-form provenance.  Requiring a verified, non-retired target closes
        the race where a consumer is registered after its input has already
        been physically retired.  Producers that need historical provenance
        should put it in ``source_ref``/``metadata`` instead.
        """

        for dependency in dependencies:
            if dependency == artifact_id:
                raise ArtifactSecurityError("artifact cannot depend on itself")
            try:
                target = self._load_record(self._manifest_path(dependency))
            except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
                raise ArtifactSecurityError(
                    f"dependency target is unavailable: {dependency}"
                ) from exc
            if not target.verified:
                raise ArtifactSecurityError(
                    f"dependency target is not verified: {dependency}"
                )
            claim = self._retirement_claim(target)
            if target.state == ArtifactState.RETIRED.value or (
                claim is not None and claim.get("phase") in _RETIREMENT_PENDING_PHASES
            ):
                raise ArtifactSecurityError(
                    f"dependency target is no longer usable: {dependency}"
                )

    def _payload_from_record(self, record: ArtifactRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "artifact_id": record.artifact_id,
            "owner": record.owner,
            "producer": record.producer,
            "run_id": record.run_id,
            "purpose": record.purpose,
            "path": str(record.path),
            "root": str(record.root),
            "path_identity": None
            if record.path_identity is None
            else list(record.path_identity),
            "root_identity": None
            if record.root_identity is None
            else list(record.root_identity),
            "kind": record.kind,
            "state": record.state,
            "created_ns": record.created_ns,
            "updated_ns": record.updated_ns,
            "source_ref": record.source_ref,
            "digest": record.digest,
            "dependencies": list(record.dependencies),
            "retain_until_ns": record.retain_until_ns,
            "ttl_ns": record.ttl_ns,
            "disposable": record.disposable,
            "metadata": dict(record.metadata),
            "path_size_bytes": record.path_size_bytes,
            "path_mtime_ns": record.path_mtime_ns,
        }
        payload["manifest_digest"] = _manifest_digest(payload)
        return payload

    def _read_manifest_payload(self, manifest_path: Path) -> dict[str, Any]:
        try:
            metadata = manifest_path.lstat()
        except OSError as exc:
            raise ArtifactManifestError("artifact manifest is unavailable") from exc
        issue = _manifest_file_issue(metadata)
        if issue is not None:
            raise ArtifactManifestError(f"artifact manifest failed safety check: {issue}")
        if metadata.st_size > self.max_manifest_bytes:
            raise ArtifactManifestError("artifact manifest is too large")
        try:
            raw = manifest_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactManifestError("artifact manifest is not valid UTF-8 JSON") from exc
        if not isinstance(payload, Mapping):
            raise ArtifactManifestError("artifact manifest must be an object")
        if set(payload) != _MANIFEST_FIELDS:
            raise ArtifactManifestError("artifact manifest fields are not schema-bound")
        if payload.get("schema") != ARTIFACT_REGISTRY_SCHEMA:
            raise ArtifactManifestError("unsupported artifact registry schema")
        expected = payload.get("manifest_digest")
        if not isinstance(expected, str) or expected != _manifest_digest(payload):
            raise ArtifactManifestError("artifact manifest digest mismatch")
        # Validate size independently of the configured default.  This keeps a
        # caller-supplied larger limit from bypassing the durable hard bound.
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ArtifactManifestError("artifact manifest exceeds the hard size limit")
        return dict(payload)

    def _record_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        manifest_path: Path,
        revalidate: bool = True,
    ) -> ArtifactRecord:
        if payload.get("schema") != ARTIFACT_REGISTRY_SCHEMA:
            raise ArtifactManifestError("unsupported artifact registry schema")
        artifact_id = _bounded_text(
            payload.get("artifact_id"),
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        owner = _bounded_text(payload.get("owner"), label="artifact owner")
        producer = _bounded_text(payload.get("producer"), label="artifact producer")
        purpose = _bounded_text(payload.get("purpose"), label="artifact purpose")
        path_value = payload.get("path")
        root_value = payload.get("root")
        if not isinstance(path_value, (str, Path)) or not isinstance(root_value, (str, Path)):
            raise ArtifactManifestError("artifact path/root claims are invalid")
        path = _validate_absolute_path(path_value, label="artifact path")
        root = _validate_absolute_path(root_value, label="artifact root")
        path_identity = _bounded_identity(payload.get("path_identity"), label="artifact path_identity")
        root_identity = _bounded_identity(payload.get("root_identity"), label="artifact root_identity")
        kind = payload.get("kind")
        state = payload.get("state")
        if kind not in ARTIFACT_KINDS:
            raise ArtifactManifestError("artifact kind is unsupported")
        if state not in ARTIFACT_STATES:
            raise ArtifactManifestError("artifact state is unsupported")
        run_id = _bounded_run_id(payload.get("run_id"))
        created_ns = payload.get("created_ns")
        updated_ns = payload.get("updated_ns")
        if type(created_ns) is not int or created_ns < 0:
            raise ArtifactManifestError("artifact created_ns is invalid")
        if type(updated_ns) is not int or updated_ns < created_ns:
            raise ArtifactManifestError("artifact updated_ns is invalid")
        source_ref = _bounded_json(
            payload.get("source_ref"),
            label="artifact source_ref",
            limit=MAX_TEXT_BYTES,
        )
        digest = payload.get("digest")
        if digest is not None:
            digest = _bounded_text(digest, label="artifact digest", limit=MAX_TEXT_BYTES)
        dependencies = self._normalize_dependencies(payload.get("dependencies"))
        if list(dependencies) != payload.get("dependencies"):
            raise ArtifactManifestError("artifact dependencies are not canonical")
        retain_until_ns = payload.get("retain_until_ns")
        ttl_ns = payload.get("ttl_ns")
        for name, value in (("retain_until_ns", retain_until_ns), ("ttl_ns", ttl_ns)):
            if value is not None and (type(value) is not int or value < 0):
                raise ArtifactManifestError(f"artifact {name} is invalid")
        disposable = payload.get("disposable")
        if type(disposable) is not bool:
            raise ArtifactManifestError("artifact disposable is invalid")
        metadata = _bounded_mapping(
            payload.get("metadata"),
            label="artifact metadata",
            limit=self.max_metadata_bytes,
        )
        path_size_bytes = payload.get("path_size_bytes")
        path_mtime_ns = payload.get("path_mtime_ns")
        for name, value in (
            ("path_size_bytes", path_size_bytes),
            ("path_mtime_ns", path_mtime_ns),
        ):
            if type(value) is not int or value < 0:
                raise ArtifactManifestError(f"artifact {name} is invalid")
        record = ArtifactRecord(
            artifact_id=artifact_id,
            owner=owner,
            producer=producer,
            run_id=run_id,
            purpose=purpose,
            path=path,
            root=root,
            path_identity=path_identity,
            root_identity=root_identity,
            kind=kind,
            state=state,
            created_ns=created_ns,
            updated_ns=updated_ns,
            source_ref=source_ref,
            digest=digest,
            dependencies=dependencies,
            retain_until_ns=retain_until_ns,
            ttl_ns=ttl_ns,
            disposable=disposable,
            metadata=metadata,
            path_size_bytes=path_size_bytes,
            path_mtime_ns=path_mtime_ns,
            manifest_digest=payload.get("manifest_digest"),
            manifest_path=manifest_path,
        )
        if not revalidate:
            return record
        return self._revalidate_record(record)

    def _revalidate_record(self, record: ArtifactRecord) -> ArtifactRecord:
        """Re-read root/path identity and return a detailed fail-closed record."""

        try:
            registry_metadata = self.root.lstat()
        except OSError:
            return replace(record, valid=False, issue="root_identity_drift", reason="root_identity_drift")
        registry_issue = _private_directory_issue(registry_metadata, root=True)
        if registry_issue is not None:
            return replace(record, valid=False, issue=registry_issue, reason=registry_issue)
        try:
            root_metadata = record.root.lstat()
        except OSError:
            return replace(record, valid=False, issue="artifact_root_drift", reason="artifact_root_drift")
        root_issue = _private_directory_issue(root_metadata, root=True)
        if root_issue is not None:
            return replace(record, valid=False, issue="artifact_root_drift", reason=root_issue)
        if record.root_identity is None or _identity(root_metadata) != record.root_identity:
            return replace(record, valid=False, issue="artifact_root_drift", reason="artifact_root_drift")
        try:
            path_metadata = record.path.lstat()
        except OSError:
            if record.state == ArtifactState.RETIRED.value:
                # Retirement is a durable tombstone: the owning lifecycle may
                # have removed the claimed path after recording this state.
                # Keep the manifest protected rather than reopening a cleanup
                # obligation for an intentionally absent artifact.
                return replace(record, valid=True, issue=None, reason=None, size_bytes=0)
            return replace(record, valid=False, issue="artifact_missing", reason="artifact_missing")
        path_issue = _private_artifact_issue(path_metadata)
        if path_issue is not None:
            return replace(record, valid=False, issue=path_issue, reason=path_issue)
        if record.path_identity is None or _identity(path_metadata) != record.path_identity:
            return replace(
                record,
                valid=False,
                issue="artifact_identity_drift",
                reason="artifact_identity_drift",
            )
        # Active producers are allowed to grow/replace their output while the
        # lifecycle claim is still open.  Once an artifact is completed, size
        # and mtime become useful revalidation observations in addition to the
        # physical identity (which may otherwise be reused on Linux).
        if record.state == ArtifactState.COMPLETED.value and stat.S_ISREG(path_metadata.st_mode) and (
            record.path_size_bytes is None
            or record.path_mtime_ns is None
            or int(path_metadata.st_size) != record.path_size_bytes
            or int(path_metadata.st_mtime_ns) != record.path_mtime_ns
        ):
            return replace(
                record,
                valid=False,
                issue="artifact_identity_drift",
                reason="artifact_identity_drift",
            )
        profile = "strict"
        policy = record.metadata.get("scratch_payload_policy")
        if policy is not None:
            try:
                from neocortex.runtime.scratch import verified_workspace_payload_profile
                if not isinstance(policy, Mapping) or not record.artifact_id.startswith("scratch:"):
                    raise ArtifactSecurityError("scratch payload policy has no owner binding")
                profile = verified_workspace_payload_profile(record.path, owner=record.owner,
                                                              expected_policy=policy).value
            except (OSError, RuntimeError, ValueError) as exc:
                return replace(record, valid=False, issue="payload_policy_unverified", reason=str(exc)[:512])
        if stat.S_ISDIR(path_metadata.st_mode):
            from neocortex.runtime.scratch_tree import observe_claimed_tree
            observed = observe_claimed_tree(record.path, limit=self.max_records,
                         max_bytes=self.max_bytes, profile=profile, include_control_manifest=True)
            size, size_issue = observed.apparent_bytes, _artifact_observation_issue(observed.issue)
            allocated = observed.allocated_bytes
            entries = observed.members
        else:
            size, size_issue = _path_size_no_follow(record.path, max_entries=self.max_records,
                                                   max_bytes=self.max_bytes, profile=profile)
            allocated = getattr(path_metadata, "st_blocks", 0) * 512
            entries = 1
        if size_issue is not None:
            return replace(record, size_bytes=size, allocated_bytes=allocated,
                           observed_payload_entries=entries, valid=False, issue=size_issue, reason=size_issue)
        return replace(record, size_bytes=size, allocated_bytes=allocated,
                       observed_payload_entries=entries, valid=True, issue=None, reason=None)

    def _invalid_record(self, manifest_path: Path, issue: str) -> ArtifactRecord:
        bounded_issue = _bounded_text(issue, label="artifact issue", limit=MAX_REASON_BYTES)
        return ArtifactRecord(
            artifact_id=f"invalid:{manifest_path.name}",
            owner="unknown",
            producer="unknown",
            run_id=None,
            purpose="invalid-manifest",
            path=manifest_path,
            root=self.root,
            path_identity=None,
            root_identity=None,
            kind=ArtifactKind.EXTERNAL.value,
            state=ArtifactState.RECOVERY_REQUIRED.value,
            created_ns=0,
            updated_ns=0,
            disposable=False,
            size_bytes=0,
            valid=False,
            issue=bounded_issue,
            reason=bounded_issue,
            classification="unknown",
            manifest_path=manifest_path,
        )

    def _load_record(self, manifest_path: Path) -> ArtifactRecord:
        payload = self._read_manifest_payload(manifest_path)
        return self._record_from_payload(payload, manifest_path=manifest_path)

    # -- registration/update ------------------------------------------

    @_registry_write_locked
    def register(
        self,
        artifact_id: str | ArtifactRecord,
        producer: str | None = None,
        path: Path | str | None = None,
        *,
        owner: str | None = None,
        run_id: int | str | None = None,
        purpose: str | None = None,
        root: Path | str | None = None,
        kind: str = ArtifactKind.TEMPORARY.value,
        state: str = ArtifactState.ACTIVE.value,
        source_ref: Any = None,
        digest: str | None = None,
        dependencies: Iterable[str] | None = None,
        retain_until_ns: int | None = None,
        ttl_ns: int | None = None,
        ttl: int | None = None,
        disposable: bool = True,
        metadata: Mapping[str, Any] | None = None,
        created_ns: int | None = None,
        updated_ns: int | None = None,
        path_identity: Sequence[int] | None = None,
        identity: Sequence[int] | None = None,
        root_identity: Sequence[int] | None = None,
        lifecycle_state: str | None = None,
        retain_on_success: bool | None = None,
        retention: Mapping[str, Any] | None = None,
        manifest_digest: str | None = None,
    ) -> ArtifactRecord:
        """Register one claim, or replay an identical existing registration.

        Registration is an explicit write and may create the configured root.
        ``plan``/``verify`` never use that path.  An existing id is returned
        unchanged only when every durable registration field matches; a
        conflicting or invalid manifest is preserved and rejected.
        """

        if isinstance(artifact_id, ArtifactRecord):
            source_record = artifact_id
            if producer is None:
                producer = source_record.producer
            if path is None:
                path = source_record.path
            if owner is None:
                owner = source_record.owner
            if run_id is None:
                run_id = source_record.run_id
            if purpose is None:
                purpose = source_record.purpose
            if root is None:
                root = source_record.root
            if kind == ArtifactKind.TEMPORARY.value:
                kind = source_record.kind
            if state == ArtifactState.ACTIVE.value:
                state = source_record.state
            if source_ref is None:
                source_ref = source_record.source_ref
            if digest is None:
                digest = source_record.digest
            if dependencies is None:
                dependencies = source_record.dependencies
            if retain_until_ns is None:
                retain_until_ns = source_record.retain_until_ns
            if ttl_ns is None:
                ttl_ns = source_record.ttl_ns
            if ttl is None and source_record.ttl_ns is not None:
                ttl = source_record.ttl_ns
            if disposable is True:
                disposable = source_record.disposable
            if metadata is None:
                metadata = source_record.metadata
            if created_ns is None:
                created_ns = source_record.created_ns
            if updated_ns is None:
                updated_ns = source_record.updated_ns
            artifact_id = source_record.artifact_id
        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for registration"
            )
        if owner is None:
            owner = self.owner
        owner = _bounded_text(owner, label="artifact owner")
        if owner != self.owner:
            raise ArtifactSecurityError("artifact owner does not match this registry")
        producer = _bounded_text(
            _DEFAULT_PRODUCER if producer is None else producer,
            label="artifact producer",
        )
        purpose = _bounded_text(
            _DEFAULT_PURPOSE if purpose is None else purpose,
            label="artifact purpose",
        )
        if path is None:
            raise ValueError("artifact path is required")
        path = _validate_absolute_path(path, label="artifact path")
        root = self.root if root is None else _validate_absolute_path(root, label="artifact root")
        if lifecycle_state is not None:
            if state != ArtifactState.ACTIVE.value and state != lifecycle_state:
                raise ValueError("state and lifecycle_state disagree")
            state = lifecycle_state
        if retention is not None:
            if not isinstance(retention, Mapping):
                raise TypeError("artifact retention must be an object")
            if retain_until_ns is None:
                candidate_deadline = retention.get("retain_until_ns")
                if candidate_deadline is not None:
                    retain_until_ns = candidate_deadline
            if ttl_ns is None:
                candidate_ttl = retention.get("ttl_ns")
                if candidate_ttl is not None:
                    ttl_ns = candidate_ttl
        if retain_on_success is not None and type(retain_on_success) is not bool:
            raise TypeError("artifact retain_on_success must be a boolean or null")
        if retain_on_success is False and state == ArtifactState.COMPLETED.value:
            # The explicit disposable field remains authoritative; this
            # compatibility hint only prevents a producer from accidentally
            # making a retained scratch result appear non-disposable.
            disposable = bool(disposable)
        if manifest_digest is not None and digest is None:
            digest = manifest_digest
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind: {kind!r}")
        if state not in ARTIFACT_STATES:
            raise ValueError(f"unsupported artifact state: {state!r}")
        run_id = _bounded_run_id(run_id)
        if ttl is not None:
            if ttl_ns is not None and ttl_ns != ttl:
                raise ValueError("ttl and ttl_ns disagree")
            ttl_ns = ttl
        for name, value in (("retain_until_ns", retain_until_ns), ("ttl_ns", ttl_ns)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"artifact {name} must be a non-negative integer or null")
        normalized_dependencies = self._normalize_dependencies(dependencies)
        # Dependencies are live registry claims.  Validate them while the
        # same directory lock used by retirement is held so acquisition cannot
        # race a physical retirement of the target.
        self._validate_dependencies_locked(
            normalized_dependencies,
            artifact_id=artifact_id,
        )
        if digest is not None:
            digest = _bounded_text(digest, label="artifact digest", limit=MAX_TEXT_BYTES)
        normalized_source_ref = _bounded_json(
            source_ref,
            label="artifact source_ref",
            limit=MAX_TEXT_BYTES,
        )
        normalized_metadata = _bounded_mapping(
            metadata,
            label="artifact metadata",
            limit=self.max_metadata_bytes,
        )
        if type(disposable) is not bool:
            raise TypeError("artifact disposable must be a boolean")
        self._ensure_root(create=True)
        artifact_root_metadata = _lstat(root, label="artifact root")
        root_issue = _private_directory_issue(artifact_root_metadata, root=True)
        if root_issue is not None:
            raise ArtifactSecurityError(f"artifact root failed safety check: {root_issue}")
        artifact_root_identity = _identity(artifact_root_metadata)
        path_metadata = _lstat(path, label="artifact path")
        path_issue = _private_artifact_issue(path_metadata)
        if path_issue is not None:
            raise ArtifactSecurityError(f"artifact path failed safety check: {path_issue}")
        if path == self.root:
            raise ArtifactSecurityError("artifact path cannot be the registry root")
        actual_path_identity = _identity(path_metadata)
        supplied_identity = path_identity if path_identity is not None else identity
        if supplied_identity is not None:
            expected_path_identity = _bounded_identity(supplied_identity, label="artifact path_identity")
            if expected_path_identity != actual_path_identity:
                raise ArtifactSecurityError("artifact path identity does not match the live path")
        if root_identity is not None:
            expected_root_identity = _bounded_identity(root_identity, label="artifact root_identity")
            if expected_root_identity != artifact_root_identity:
                raise ArtifactSecurityError("artifact root identity does not match the live root")
        now = time.time_ns() if created_ns is None else created_ns
        if type(now) is not int or now < 0:
            raise ValueError("artifact created_ns must be a non-negative integer")
        updated = now if updated_ns is None else updated_ns
        if type(updated) is not int or updated < now:
            raise ValueError("artifact updated_ns must be >= created_ns")
        if retain_until_ns is None and ttl_ns is not None:
            retain_until_ns = now + ttl_ns
        candidate = ArtifactRecord(
            artifact_id=artifact_id,
            owner=owner,
            producer=producer,
            run_id=run_id,
            purpose=purpose,
            path=path,
            # ``self.root`` owns the registry manifests; ``root`` is the
            # separately claimed artifact boundary.  Preserve the latter in
            # the durable record so valid split-root registrations do not
            # manufacture an ``artifact_root_drift`` on first read.
            root=root,
            path_identity=actual_path_identity,
            root_identity=artifact_root_identity,
            kind=kind,
            state=state,
            created_ns=now,
            updated_ns=updated,
            source_ref=normalized_source_ref,
            digest=digest,
            dependencies=normalized_dependencies,
            retain_until_ns=retain_until_ns,
            ttl_ns=ttl_ns,
            disposable=disposable,
            metadata=normalized_metadata,
            path_size_bytes=max(0, int(path_metadata.st_size)),
            path_mtime_ns=max(0, int(path_metadata.st_mtime_ns)),
        )
        payload = self._payload_from_record(candidate)
        manifest_path = self._manifest_path(artifact_id)
        try:
            self._write_registration(manifest_path, payload)
        except FileExistsError:
            try:
                existing = self._load_record(manifest_path)
            except ArtifactRegistryError as exc:
                raise ArtifactConflictError("artifact id is already bound to an invalid manifest") from exc
            if self._registration_equal(existing, candidate):
                return existing
            raise ArtifactConflictError("artifact id is already registered with different fields") from None
        return self._load_record(manifest_path)

    def _write_registration(self, manifest_path: Path, payload: Mapping[str, Any]) -> None:
        if len(_canonical_json(payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        _write_json_atomic(manifest_path, payload, exclusive=True)
        metadata = manifest_path.lstat()
        issue = _manifest_file_issue(metadata)
        if issue is not None:
            raise ArtifactManifestError(f"published manifest failed safety check: {issue}")

    def _write_existing_registration(
        self,
        manifest_path: Path,
        payload: Mapping[str, Any],
    ) -> None:
        """Atomically replace an already-owned manifest."""

        if len(_canonical_json(payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        _write_json_atomic(manifest_path, payload, exclusive=False)
        metadata = manifest_path.lstat()
        issue = _manifest_file_issue(metadata)
        if issue is not None:
            raise ArtifactManifestError(f"published manifest failed safety check: {issue}")

    @staticmethod
    def _registration_equal(existing: ArtifactRecord, candidate: ArtifactRecord) -> bool:
        fields = (
            "artifact_id",
            "owner",
            "producer",
            "run_id",
            "purpose",
            "path",
            "root",
            "path_identity",
            "root_identity",
            "kind",
            "state",
            "created_ns",
            "updated_ns",
            "source_ref",
            "digest",
            "dependencies",
            "retain_until_ns",
            "ttl_ns",
            "disposable",
            "metadata",
            "path_size_bytes",
            "path_mtime_ns",
        )
        return all(getattr(existing, name) == getattr(candidate, name) for name in fields)

    @_registry_write_locked
    def update(
        self,
        artifact: str | ArtifactRecord,
        *,
        producer: str | object = _UNSET,
        run_id: int | str | object | None = _UNSET,
        purpose: str | object = _UNSET,
        state: str | object = _UNSET,
        source_ref: Any = _UNSET,
        digest: str | object | None = _UNSET,
        dependencies: Iterable[str] | object | None = _UNSET,
        retain_until_ns: int | object | None = _UNSET,
        ttl_ns: int | object | None = _UNSET,
        ttl: int | object | None = _UNSET,
        disposable: bool | object = _UNSET,
        metadata: Mapping[str, Any] | object | None = _UNSET,
        updated_ns: int | None = None,
    ) -> ArtifactRecord:
        """Atomically update durable fields after a fresh revalidation.

        An update with no effective change is a replay-safe no-op and does not
        rewrite the manifest or advance ``updated_ns``.
        """

        artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        self._ensure_root(create=False)
        manifest_path = self._manifest_path(artifact_id)
        current = self._load_record(manifest_path)
        try:
            current_retirement = self._retirement_claim(current)
        except ArtifactSecurityError:
            raise
        retiring_missing_path = (
            state == ArtifactState.RETIRED.value and current.issue == "artifact_missing"
            and current_retirement is not None
            and current_retirement.get("phase") in _RETIREMENT_PENDING_PHASES
        )
        if not current.verified and not retiring_missing_path:
            raise ArtifactSecurityError(current.reason or "artifact cannot be updated after drift")
        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for update"
            )
        if current.owner != self.owner:
            raise ArtifactSecurityError("artifact owner does not match this registry")
        updates: dict[str, Any] = {}
        for name, value in (
            ("producer", producer),
            ("run_id", run_id),
            ("purpose", purpose),
            ("state", state),
            ("source_ref", source_ref),
            ("digest", digest),
            ("dependencies", dependencies),
            ("retain_until_ns", retain_until_ns),
            ("ttl_ns", ttl_ns),
            ("disposable", disposable),
            ("metadata", metadata),
        ):
            if value is not _UNSET:
                updates[name] = value
        if ttl is not _UNSET:
            if "ttl_ns" in updates and updates["ttl_ns"] != ttl:
                raise ValueError("ttl and ttl_ns disagree")
            updates["ttl_ns"] = ttl
        normalized: dict[str, Any] = {}
        if "producer" in updates:
            normalized["producer"] = _bounded_text(updates["producer"], label="artifact producer")
        if "run_id" in updates:
            normalized["run_id"] = _bounded_run_id(updates["run_id"])
        if "purpose" in updates:
            normalized["purpose"] = _bounded_text(updates["purpose"], label="artifact purpose")
        if "state" in updates:
            if updates["state"] not in ARTIFACT_STATES:
                raise ValueError(f"unsupported artifact state: {updates['state']!r}")
            normalized["state"] = updates["state"]
        if "source_ref" in updates:
            normalized["source_ref"] = _bounded_json(
                updates["source_ref"],
                label="artifact source_ref",
                limit=MAX_TEXT_BYTES,
            )
        if "digest" in updates:
            normalized["digest"] = (
                None
                if updates["digest"] is None
                else _bounded_text(updates["digest"], label="artifact digest", limit=MAX_TEXT_BYTES)
            )
        if "dependencies" in updates:
            normalized["dependencies"] = list(self._normalize_dependencies(updates["dependencies"]))
            self._validate_dependencies_locked(
                tuple(normalized["dependencies"]),
                artifact_id=current.artifact_id,
            )
        if "retain_until_ns" in updates:
            normalized["retain_until_ns"] = updates["retain_until_ns"]
        if "ttl_ns" in updates:
            normalized["ttl_ns"] = updates["ttl_ns"]
        for name in ("retain_until_ns", "ttl_ns"):
            value = normalized.get(name, getattr(current, name))
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"artifact {name} must be a non-negative integer or null")
        if "disposable" in updates:
            if type(updates["disposable"]) is not bool:
                raise TypeError("artifact disposable must be a boolean")
            normalized["disposable"] = updates["disposable"]
        if "metadata" in updates:
            normalized["metadata"] = _bounded_mapping(
                updates["metadata"],
                label="artifact metadata",
                limit=self.max_metadata_bytes,
            )
        merged = {name: getattr(current, name) for name in _MANIFEST_FIELDS if name not in {"schema", "manifest_digest"}}
        merged.update(normalized)
        if merged.get("ttl_ns") is not None and "retain_until_ns" in updates:
            # An explicit retain_until remains authoritative.  If only ttl is
            # changed, derive a new deadline from the immutable creation time.
            pass
        elif "ttl_ns" in normalized:
            ttl_value = normalized["ttl_ns"]
            if ttl_value is None:
                merged["retain_until_ns"] = None
            elif type(ttl_value) is int:
                merged["retain_until_ns"] = current.created_ns + ttl_value
            else:
                raise ValueError("artifact ttl_ns must be an integer or null")
        if "retain_until_ns" in normalized and normalized["retain_until_ns"] is None:
            # Clearing an explicit deadline also clears no TTL; a non-null TTL
            # still gives the record its derived deadline at classification.
            pass
        if merged["state"] == ArtifactState.RETIRED.value:
            # ScratchManager publishes the terminal state after the physical
            # unlink.  Preserve and close the durable intent even when the
            # caller supplies its own metadata projection; otherwise a
            # successful retirement would lose the receipt on replay.
            candidate_metadata = merged.get("metadata", current.metadata)
            if not isinstance(candidate_metadata, Mapping):
                raise ArtifactSecurityError("artifact metadata is not an object")
            current_retirement = current.metadata.get(_RETIREMENT_KEY)
            if current_retirement is not None and _RETIREMENT_KEY not in candidate_metadata:
                # The scratch projection intentionally carries producer
                # metadata, not registry-internal receipts.  Carry the
                # reserved claim forward before closing it.
                candidate_metadata = dict(candidate_metadata)
                candidate_metadata[_RETIREMENT_KEY] = current_retirement
            candidate = replace(
                current,
                metadata=_bounded_mapping(
                    candidate_metadata,
                    label="artifact metadata",
                    limit=self.max_metadata_bytes,
                ),
            )
            retirement = self._retirement_claim(candidate)
            if retirement is not None and retirement.get("phase") in _RETIREMENT_PENDING_PHASES:
                merged["metadata"] = self._metadata_with_retirement(
                    candidate,
                    phase=_RETIREMENT_CONFIRMED_PHASE,
                    operation_id=str(retirement["operation_id"]),
                    observed_path_exists=False,
                    confirmed_ns=time.time_ns(),
                    receipt={"effect": "removed", "replayed": False},
                )
        changed = any(
            merged.get(name) != getattr(current, name)
            for name in (
                "producer",
                "run_id",
                "purpose",
                "state",
                "source_ref",
                "digest",
                "dependencies",
                "retain_until_ns",
                "ttl_ns",
                "disposable",
                "metadata",
            )
        )
        if not changed:
            return current
        timestamp = time.time_ns() if updated_ns is None else updated_ns
        if type(timestamp) is not int or timestamp < current.created_ns:
            raise ValueError("artifact updated_ns must be >= created_ns")
        merged["updated_ns"] = max(timestamp, current.updated_ns)
        path_size_bytes = current.path_size_bytes
        path_mtime_ns = current.path_mtime_ns
        if merged["state"] == ArtifactState.COMPLETED.value:
            # A producer may legitimately populate an active directory between
            # registration and completion.  Capture the final no-follow
            # observations at the transition instead of treating that normal
            # lifecycle growth as drift.
            try:
                completed_metadata = current.path.lstat()
            except OSError as exc:
                raise ArtifactSecurityError("artifact disappeared before completion") from exc
            completed_issue = _private_artifact_issue(completed_metadata)
            if completed_issue is not None:
                raise ArtifactSecurityError(
                    f"artifact failed completion safety check: {completed_issue}"
                )
            if current.path_identity is None or _identity(completed_metadata) != current.path_identity:
                raise ArtifactSecurityError("artifact identity changed before completion")
            path_size_bytes = max(0, int(completed_metadata.st_size))
            path_mtime_ns = max(0, int(completed_metadata.st_mtime_ns))
        merged_payload: dict[str, Any] = {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "artifact_id": current.artifact_id,
            "owner": current.owner,
            "producer": merged["producer"],
            "run_id": merged["run_id"],
            "purpose": merged["purpose"],
            "path": str(current.path),
            "root": str(current.root),
            "path_identity": list(current.path_identity or ()),
            "root_identity": list(current.root_identity or ()),
            "kind": current.kind,
            "state": merged["state"],
            "created_ns": current.created_ns,
            "updated_ns": merged["updated_ns"],
            "source_ref": merged["source_ref"],
            "digest": merged["digest"],
            "dependencies": list(merged["dependencies"]),
            "retain_until_ns": merged["retain_until_ns"],
            "ttl_ns": merged["ttl_ns"],
            "disposable": merged["disposable"],
            "metadata": merged["metadata"],
            "path_size_bytes": path_size_bytes,
            "path_mtime_ns": path_mtime_ns,
        }
        merged_payload["manifest_digest"] = _manifest_digest(merged_payload)
        if len(_canonical_json(merged_payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        # Refuse to overwrite a manifest that changed after our read.
        latest = self._read_manifest_payload(manifest_path)
        if latest.get("manifest_digest") != current.manifest_digest:
            raise ArtifactConflictError("artifact manifest changed during update")
        _write_json_atomic(manifest_path, merged_payload, exclusive=False)
        return self._load_record(manifest_path)

    # -- read-only verification and planning --------------------------

    def _scan_entries(
        self,
        *,
        max_records: int,
    ) -> tuple[tuple[os.DirEntry[str], ...], bool, tuple[str, ...]]:
        try:
            iterator = os.scandir(self.root)
        except OSError as exc:
            raise ArtifactRootError("artifact registry root could not be scanned") from exc
        entries: list[os.DirEntry[str]] = []
        truncated = False
        try:
            for entry in iterator:
                if entry.name == _TOMBSTONE_RETENTION_DIR:
                    try:
                        internal = entry.stat(follow_symlinks=False)
                    except OSError:
                        # Keep the entry visible as unmanaged so a concurrent
                        # or corrupt retention store fails closed.
                        entries.append(entry)
                        continue
                    if (
                        stat.S_ISDIR(internal.st_mode)
                        and not stat.S_ISLNK(internal.st_mode)
                        and internal.st_uid == os.geteuid()
                        and not internal.st_mode & 0o077
                    ):
                        continue
                if len(entries) >= max_records:
                    truncated = True
                    break
                entries.append(entry)
        finally:
            iterator.close()
        return tuple(sorted(entries, key=lambda item: item.name)), truncated, (
            "record_limit",
        ) if truncated else ()

    def _scan_records(
        self,
        *,
        max_records: int,
    ) -> tuple[tuple[ArtifactRecord, ...], tuple[Path, ...], bool, tuple[str, ...]]:
        entries, truncated, truncation_reasons = self._scan_entries(max_records=max_records)
        records: list[ArtifactRecord] = []
        unmanaged: list[Path] = []
        for entry in entries:
            path = self.root / entry.name
            if entry.name.startswith(".") and entry.name.endswith(".tmp"):
                # A crashed writer's temporary is not a managed artifact.  It
                # is still surfaced as unmanaged below if the caller wants the
                # full neighbor list; never parse it as a manifest.
                unmanaged.append(path)
                continue
            if not entry.name.endswith(MANIFEST_SUFFIX):
                unmanaged.append(path)
                continue
            try:
                records.append(self._load_record(path))
            except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
                records.append(self._invalid_record(path, f"manifest_invalid:{type(exc).__name__}"))
        if truncated:
            truncation_reasons = tuple(dict.fromkeys((*truncation_reasons, "scan_bounded")))
        return tuple(records), tuple(unmanaged), truncated, truncation_reasons

    @staticmethod
    def _classify(record: ArtifactRecord, *, now_ns: int) -> tuple[str, str]:
        if not record.valid and record.issue in _BLOCKING_ISSUES:
            return "blocked", record.issue
        if not record.verified:
            return "unknown", record.issue or "manifest_invalid"
        # A durable retirement intent is a recovery boundary.  Until its
        # receipt is confirmed the artifact is never eligible, even if the
        # ordinary state/kind/retention fields would otherwise qualify it.
        try:
            retirement = ArtifactRegistry._retirement_claim(record)
        except ArtifactSecurityError:
            return "blocked", "retirement_claim_invalid"
        if retirement is not None and retirement.get("phase") in _RETIREMENT_PENDING_PHASES:
            return "blocked", "retirement_recovery_required"
        if record.updated_ns > now_ns:
            return "protected", "future_timestamp"
        if record.state == ArtifactState.ACTIVE.value:
            return "protected", "state_active"
        if record.state == ArtifactState.FAILED.value:
            return "protected", "state_failed"
        if record.state == ArtifactState.RECOVERY_REQUIRED.value:
            return "protected", "state_recovery_required"
        if record.state == ArtifactState.RETIRED.value:
            return "protected", "state_retired"
        if record.state != ArtifactState.COMPLETED.value:
            return "unknown", "state_unknown"
        if not record.disposable:
            return "protected", "not_disposable"
        if record.kind not in _DISPOSABLE_KINDS:
            return "protected", "kind_protected"
        deadline = record.effective_retain_until_ns()
        if deadline is not None and now_ns < deadline:
            return "protected", "retention_active"
        if deadline is not None and now_ns < 0:  # pragma: no cover - defensive
            return "protected", "retention_active"
        return "eligible", "retention_expired" if deadline is not None else "disposable_completed"

    @staticmethod
    def _live_dependents(
        target: ArtifactRecord,
        records: Sequence[ArtifactRecord],
    ) -> tuple[str, ...]:
        """Return active/recoverable claims that still reference ``target``."""

        dependents: list[str] = []
        for record in records:
            if record.artifact_id == target.artifact_id:
                continue
            if target.artifact_id not in record.dependencies:
                continue
            # An invalid or incomplete observation is conservative: its
            # dependency claim remains live until an owner explicitly repairs
            # or releases it.
            if not record.valid or record.state in _LIVE_DEPENDENT_STATES:
                dependents.append(record.artifact_id)
        return tuple(sorted(set(dependents)))

    def verify(
        self,
        target: str | Path | ArtifactRecord | None = None,
        *,
        now_ns: int | None = None,
        raise_on_error: bool = False,
    ) -> ArtifactRecord | tuple[ArtifactRecord, ...]:
        """Re-read and revalidate one record, or all records when target is null.

        The single-record return is truthy only when both the manifest and the
        no-follow root/artifact identity are valid.  No call creates a root or
        changes any filesystem entry.
        """

        del now_ns  # verification is identity-only; retention belongs to plan
        if target is None:
            self._ensure_root(create=False)
            records, _, _, _ = self._scan_records(max_records=self.max_records)
            return records
        if isinstance(target, ArtifactRecord):
            artifact_id = target.artifact_id
        elif isinstance(target, Path):
            candidate = _validate_absolute_path(target, label="artifact verify target")
            artifact_id = None
            if candidate.suffix == MANIFEST_SUFFIX and candidate.parent == self.root:
                try:
                    payload = self._read_manifest_payload(candidate)
                    artifact_id_value = payload.get("artifact_id")
                    if isinstance(artifact_id_value, str):
                        artifact_id = artifact_id_value
                except ArtifactRegistryError:
                    result = self._invalid_record(candidate, "manifest_invalid")
                    if raise_on_error:
                        raise ArtifactManifestError(result.reason or "artifact manifest invalid") from None
                    return result
            if artifact_id is None:
                self._ensure_root(create=False)
                records, _, _, _ = self._scan_records(max_records=self.max_records)
                matches = tuple(record for record in records if record.path == candidate)
                result = matches[0] if matches else self._invalid_record(candidate, "artifact_unmanaged")
                if raise_on_error and not result:
                    raise ArtifactSecurityError(result.reason or "artifact is not verified")
                return result
        else:
            artifact_id = _bounded_text(
                str(target),
                label="artifact_id",
                limit=MAX_ARTIFACT_ID_BYTES,
            )
        self._ensure_root(create=False)
        assert artifact_id is not None
        manifest_path = self._manifest_path(artifact_id)
        try:
            result = self._load_record(manifest_path)
        except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
            result = self._invalid_record(manifest_path, f"manifest_invalid:{type(exc).__name__}")
        if raise_on_error and not result:
            raise ArtifactSecurityError(result.reason or "artifact is not verified")
        return result

    def verify_bool(self, target: str | Path | ArtifactRecord) -> bool:
        """Boolean convenience wrapper around detailed :meth:`verify`."""

        result = self.verify(target)
        return isinstance(result, ArtifactRecord) and result.verified

    @contextmanager
    def observation_guard(self) -> Iterator["ArtifactRegistry"]:
        """Hold the existing registry guard for a coordinated owner snapshot.

        This read-only coordination surface grants no retirement or lifecycle
        authority. A missing registry remains absent; existing consumers can
        use the same guarded instance to verify their exact claims.
        """
        if not self._ensure_root(create=False):
            yield self
            return
        with self._registry_lock():
            yield self

    def for_owner(self, owner: str) -> "ArtifactRegistry":
        """Bind an explicit owner while sharing this instance's nested guard.

        The owning subsystem must supply its own logical owner. This does not
        rewrite claims or bypass the existing owner and dependency checks.
        """
        bound = ArtifactRegistry(self.root, owner=owner, create_root=False,
                                 max_records=self.max_records, max_bytes=self.max_bytes,
                                 max_manifest_bytes=self.max_manifest_bytes,
                                 max_metadata_bytes=self.max_metadata_bytes)
        bound._lock_local = self._lock_local
        return bound

    @contextmanager
    def retirement_guard(self, artifact: str | ArtifactRecord) -> Iterator[ArtifactRecord]:
        """Serialize policy/dependency release with the physical effect.

        Entering the guard durably records an ``applying`` intent in the
        artifact manifest before the caller unlinks anything.  If the caller
        raises after the path has disappeared, the intent is changed to
        ``applied_unverified`` so a fresh process can confirm the tombstone
        without repeating the physical effect.  If the path remains, the
        intent becomes ``recovery_required`` and the artifact is preserved.
        """

        artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        self._ensure_root(create=False)
        with self._registry_lock():
            yield from self._retirement_guard_locked(artifact_id)

    @contextmanager
    def retirement_batch_guard(self) -> Iterator[_RetirementBatch]:
        """Hold one registry lock for a bounded retirement batch.

        The batch shares one dependency observation, eliminating the old
        ``N`` full-registry rescans while preserving per-target reloads,
        policy checks, and identity checks immediately before each effect.
        """

        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for retirement"
            )
        self._ensure_root(create=False)
        with self._registry_lock():
            records, unmanaged, truncated, _reasons = self._scan_records(
                max_records=self.max_records,
            )
            yield _RetirementBatch(self, records, unmanaged, truncated)

    def _retirement_guard_locked(
        self,
        artifact: str | ArtifactRecord,
        *,
        records: tuple[ArtifactRecord, ...] | None = None,
        unmanaged: tuple[Path, ...] = (),
        truncated: bool = False,
    ) -> Iterator[ArtifactRecord]:
        if self.owner is None:
            raise ArtifactSecurityError("federated artifact registry view is read-only for retirement")
        artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactRecord) else artifact
        artifact_id = _bounded_text(
            artifact_id,
            label="artifact_id",
            limit=MAX_ARTIFACT_ID_BYTES,
        )
        current = self._load_record(self._manifest_path(artifact_id))
        if not current.verified:
            raise ArtifactSecurityError(
                current.reason or "artifact cannot be retired after drift"
            )
        if self.owner is not None and current.owner != self.owner:
            raise ArtifactSecurityError("artifact owner does not match this registry")
        if records is None:
            records, unmanaged, truncated, _reasons = self._scan_records(
                max_records=self.max_records,
            )
        if not self._dependency_observation_complete(
            records,
            unmanaged,
            truncated=truncated,
        ):
            raise ArtifactSecurityError("dependency observation incomplete")
        category, reason = self._classify_for_owner(current, now_ns=time.time_ns())
        if category != "eligible":
            raise ArtifactSecurityError(f"artifact policy prevents retirement: {reason}")
        dependents = self._live_dependents(current, records)
        if dependents:
            raise ArtifactSecurityError(
                "artifact has live dependents: " + ", ".join(dependents[:16])
            )
        prepared = self._prepare_retirement_locked(current)
        try:
            yield prepared
        except BaseException:
            # Never let a recovery bookkeeping failure hide the primary effect
            # error.  The original manifest remains protected if this write is
            # itself interrupted; a later recover_retirements() can retry the
            # metadata-only reconciliation.
            try:
                self._record_retirement_failure_locked(prepared)
            except BaseException:
                pass
            raise
        else:
            # A caller may use the guard only to inspect/coordinate and then
            # decide not to perform the effect.  If the path and completed
            # state are still intact, remove the pending intent so it does
            # not permanently block a valid dependency acquisition.  A
            # missing path or terminal state is left durable for recovery.
            try:
                latest = self._load_record(self._manifest_path(prepared.artifact_id))
                if (
                    latest.state == ArtifactState.COMPLETED.value
                    and latest.verified
                    and latest.path.exists()
                ):
                    self._clear_retirement_intent_locked(latest)
            except BaseException:
                # Conservatively retain the intent; the next apply/recovery
                # pass will reconcile it without repeating an effect.
                pass

    def _prepare_retirement_locked(self, record: ArtifactRecord) -> ArtifactRecord:
        existing = self._retirement_claim(record)
        if existing is not None and existing.get("phase") in _RETIREMENT_PENDING_PHASES:
            raise ArtifactSecurityError("artifact retirement already requires recovery")
        operation_id = uuid.uuid4().hex
        metadata = self._metadata_with_retirement(
            record,
            phase="applying",
            operation_id=operation_id,
            applying_ns=time.time_ns(),
        )
        updated = replace(
            record,
            metadata=metadata,
            updated_ns=max(record.updated_ns, time.time_ns()),
        )
        payload = self._payload_from_record(updated)
        manifest_path = record.manifest_path or self._manifest_path(record.artifact_id)
        _write_json_atomic(manifest_path, payload, exclusive=False)
        metadata_stat = manifest_path.lstat()
        issue = _manifest_file_issue(metadata_stat)
        if issue is not None:
            raise ArtifactManifestError(f"published manifest failed safety check: {issue}")
        return self._record_from_payload(
            payload,
            manifest_path=manifest_path,
        )

    def _record_retirement_failure_locked(self, record: ArtifactRecord) -> None:
        """Persist the post-exception observation without performing effects."""

        manifest_path = record.manifest_path or self._manifest_path(record.artifact_id)
        try:
            payload = self._read_manifest_payload(manifest_path)
            raw = self._record_from_payload(payload, manifest_path=manifest_path, revalidate=False)
            claim = self._retirement_claim(raw)
            if claim is None:
                return
            try:
                raw.path.lstat()
            except FileNotFoundError:
                phase = "applied_unverified"
                observed: dict[str, Any] = {
                    "observed_path_exists": False,
                    "observed_ns": time.time_ns(),
                }
                state = raw.state
            else:
                phase = "recovery_required"
                observed = {
                    "observed_path_exists": True,
                    "observed_ns": time.time_ns(),
                }
                state = ArtifactState.RECOVERY_REQUIRED.value
            metadata = self._metadata_with_retirement(
                raw,
                phase=phase,
                operation_id=str(claim["operation_id"]),
                **observed,
            )
            updated = replace(
                raw,
                state=state,
                metadata=metadata,
                updated_ns=max(raw.updated_ns, time.time_ns()),
            )
            updated_payload = self._payload_from_record(updated)
            self._write_existing_registration(manifest_path, updated_payload)
        except FileNotFoundError:
            # A missing manifest cannot be safely reconstructed.  Preserve the
            # absence as an external recovery obligation rather than inventing
            # a retired row.
            return

    def _clear_retirement_intent_locked(self, record: ArtifactRecord) -> None:
        metadata = dict(record.metadata)
        metadata.pop(_RETIREMENT_KEY, None)
        updated = replace(
            record,
            metadata=_bounded_mapping(metadata, label="artifact metadata"),
            updated_ns=max(record.updated_ns, time.time_ns()),
        )
        manifest_path = record.manifest_path or self._manifest_path(record.artifact_id)
        self._write_existing_registration(manifest_path, self._payload_from_record(updated))

    @_registry_write_locked
    def recover_retirements(self) -> dict[str, object]:
        """Reconcile durable retirement intents without repeating effects.

        A missing claimed path plus an ``applying``/``applied_unverified``
        intent is sufficient evidence that the physical effect happened.  The
        recovery only publishes the terminal manifest state; it never creates
        or removes a workspace.  Existing paths are retained and moved to a
        recovery-required state for explicit reconciliation.
        """

        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for recovery"
            )
        if not self._ensure_root(create=False):
            return {
                "schema": "neocortex.artifact-retirement-recovery/v1",
                "status": "ready",
                "confirmed": 0,
                "recovery_required": 0,
                "truncated": False,
            }
        entries, truncated, _reasons = self._scan_entries(max_records=self.max_records)
        confirmed: list[str] = []
        recovery: list[str] = []
        for entry in entries:
            if not entry.name.endswith(MANIFEST_SUFFIX):
                continue
            manifest_path = self.root / entry.name
            try:
                payload = self._read_manifest_payload(manifest_path)
                raw = self._record_from_payload(
                    payload,
                    manifest_path=manifest_path,
                    revalidate=False,
                )
                claim = self._retirement_claim(raw)
            except (ArtifactRegistryError, OSError, TypeError, ValueError):
                continue
            # A root-wide lock coordinates owners; it does not authorize this
            # owner to reconcile another producer's durable intent.
            if raw.owner != self.owner:
                continue
            if claim is None or claim.get("phase") not in _RETIREMENT_PENDING_PHASES:
                continue
            try:
                raw.path.lstat()
            except FileNotFoundError:
                metadata = self._metadata_with_retirement(
                    raw,
                    phase=_RETIREMENT_CONFIRMED_PHASE,
                    operation_id=str(claim["operation_id"]),
                    observed_path_exists=False,
                    confirmed_ns=time.time_ns(),
                    receipt={"effect": "removed", "replayed": True},
                )
                updated = replace(
                    raw,
                    state=ArtifactState.RETIRED.value,
                    metadata=metadata,
                    updated_ns=max(raw.updated_ns, time.time_ns()),
                )
                updated_payload = self._payload_from_record(updated)
                self._write_existing_registration(manifest_path, updated_payload)
                confirmed.append(raw.artifact_id)
            else:
                metadata = self._metadata_with_retirement(
                    raw,
                    phase="recovery_required",
                    operation_id=str(claim["operation_id"]),
                    observed_path_exists=True,
                    recovery_ns=time.time_ns(),
                )
                updated = replace(
                    raw,
                    state=ArtifactState.RECOVERY_REQUIRED.value,
                    metadata=metadata,
                    updated_ns=max(raw.updated_ns, time.time_ns()),
                )
                updated_payload = self._payload_from_record(updated)
                self._write_existing_registration(manifest_path, updated_payload)
                recovery.append(raw.artifact_id)
        return {
            "schema": "neocortex.artifact-retirement-recovery/v1",
            "status": "blocked" if recovery or truncated else "applied",
            "confirmed": len(confirmed),
            "confirmed_artifacts": confirmed,
            "recovery_required": len(recovery),
            "recovery_artifacts": recovery,
            "truncated": truncated,
        }

    def _classify_for_owner(self, record: ArtifactRecord, *, now_ns: int) -> tuple[str, str]:
        if not record.valid:
            if record.issue in _BLOCKING_ISSUES:
                return "blocked", record.issue
            return "unknown", record.issue or "manifest_invalid"
        if self.owner is not None and record.owner != self.owner:
            return "blocked", "owner_mismatch"
        return self._classify(record, now_ns=now_ns)

    def plan(
        self,
        *,
        now_ns: int | None = None,
        max_records: int | None = None,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> ArtifactPlan:
        """Return a bounded classification without deleting or moving anything."""

        now = time.time_ns() if now_ns is None else now_ns
        if type(now) is not int or now < 0:
            raise ValueError("artifact plan now_ns must be a non-negative integer")
        if max_entries is not None:
            max_records = max_entries
        effective_records = self.max_records if max_records is None else _validate_limit(
            max_records,
            label="artifact plan max_records",
        )
        effective_bytes = self.max_bytes if max_bytes is None else _validate_limit(
            max_bytes,
            label="artifact plan max_bytes",
        )
        if not self._ensure_root(create=False):
            return ArtifactPlan(
                root=self.root,
                reason="artifact registry root is absent",
                root_blocked="artifact registry root is absent",
                max_records=effective_records,
                max_bytes=effective_bytes,
            )
        root_metadata = self.root.lstat()
        root_identity = _identity(root_metadata)
        records, unmanaged, truncated, truncation_reasons = self._scan_records(
            max_records=effective_records,
        )
        categories: dict[str, int] = dict.fromkeys(
            ("protected", "eligible", "blocked", "unknown"), 0
        )
        byte_categories: dict[str, int] = dict.fromkeys(categories, 0)
        reasons: dict[str, int] = {}
        observed_records: list[ArtifactRecord] = []
        total_bytes = 0
        byte_truncated = False
        dependency_complete = self._dependency_observation_complete(
            records,
            unmanaged,
            truncated=truncated,
        )
        for record in records:
            category, reason = self._classify_for_owner(record, now_ns=now)
            eligible = category == "eligible"
            if record.issue == "size_truncated":
                byte_truncated = True
                category = "blocked"
                reason = "size_truncated"
                eligible = False
            if eligible:
                if not dependency_complete:
                    # A malformed/foreign/omitted registry entry may be the
                    # consumer that protects this artifact.  Do not turn the
                    # missing information into an empty dependency set.
                    category = "blocked"
                    reason = "dependency_observation_incomplete"
                    eligible = False
                else:
                    dependents = self._live_dependents(record, records)
                if eligible and dependents:
                    category = "protected"
                    reason = "dependency_live"
                    eligible = False
                elif eligible and truncated:
                    # A bounded registry scan cannot prove that no unseen
                    # consumer references this artifact.  Fail closed rather
                    # than presenting a partial selection as disposable.
                    category = "blocked"
                    reason = "dependency_observation_incomplete"
                    eligible = False
            observed = record
            if total_bytes + record.size_bytes > effective_bytes:
                byte_truncated = True
                credited = max(0, effective_bytes - total_bytes)
                observed = replace(record, size_bytes=credited, valid=False, issue="size_truncated", reason="size_truncated")
                # A byte fence means the complete artifact observation was not
                # obtained.  No category may remain eligible on a partial
                # observation; preserve it as blocked instead.
                category = "blocked"
                reason = "size_truncated"
                eligible = False
            total_bytes += min(record.size_bytes, max(0, effective_bytes - total_bytes))
            observed = replace(observed, classification=category, eligible=eligible, reason=reason)
            observed_records.append(observed)
            categories[category] += 1
            byte_categories[category] += observed.size_bytes
            reasons[reason] = reasons.get(reason, 0) + 1
        if byte_truncated:
            truncated = True
            truncation_reasons = tuple(dict.fromkeys((*truncation_reasons, "byte_limit")))
        unmanaged_bytes = 0
        for path in unmanaged:
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                unmanaged_bytes += min(max(0, int(metadata.st_size)), max(0, effective_bytes - unmanaged_bytes))
        status = "unknown" if categories["unknown"] else "blocked" if categories["blocked"] else "planned"
        return ArtifactPlan(
            root=self.root,
            records=tuple(observed_records),
            unmanaged=unmanaged,
            protected=categories["protected"],
            eligible=categories["eligible"],
            blocked=categories["blocked"],
            unknown=categories["unknown"],
            protected_bytes=byte_categories["protected"],
            eligible_bytes=byte_categories["eligible"],
            blocked_bytes=byte_categories["blocked"],
            unknown_bytes=byte_categories["unknown"],
            unmanaged_bytes=unmanaged_bytes,
            scanned=len(records) + len(unmanaged),
            returned=len(records) + len(unmanaged),
            truncated=truncated,
            truncation_reasons=tuple(truncation_reasons),
            reasons=reasons,
            status=status,
            reason=truncation_reasons[0] if truncation_reasons else None,
            read_only=True,
            root_identity=root_identity,
            max_records=effective_records,
            max_bytes=effective_bytes,
        )

    def records(self) -> tuple[ArtifactRecord, ...]:
        """Return the bounded manifest view without creating the root."""

        if not self._ensure_root(create=False):
            return ()
        records, _, _, _ = self._scan_records(max_records=self.max_records)
        return records

    def to_dict(self, *, include_records: bool = True) -> dict[str, object]:
        """Return a bounded registry description with no artifact contents."""

        result: dict[str, object] = {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "root": str(self.root),
            "owner": self.owner,
            "limits": {
                "max_records": self.max_records,
                "max_bytes": self.max_bytes,
                "max_manifest_bytes": self.max_manifest_bytes,
                "max_metadata_bytes": self.max_metadata_bytes,
            },
        }
        if include_records:
            result["records"] = [record.to_dict() for record in self.records()]
        return result

    @_registry_write_locked
    def apply_tombstone_retention(
        self,
        artifact_ids: Iterable[str],
        *,
        release_authorized: bool,
        evidence: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        """Prune explicitly released retired manifests, never their payloads.

        This is deliberately narrower than artifact retirement.  It accepts
        only already-retired, verified tombstones whose claimed payload is
        absent and whose metadata does not retain recovery/replay/pin/grant
        obligations.  A small durable receipt is written before each unlink;
        replay observes missing manifests and confirms the prior effect rather
        than attempting it again.  Unknown, corrupt, duplicate or truncated
        selections remain blocked.
        """

        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact registry view is read-only for retention"
            )
        if type(release_authorized) is not bool or not release_authorized:
            raise ArtifactSecurityError("tombstone retention requires release authorization")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ArtifactSecurityError("tombstone retention requires explicit evidence")
        evidence_payload = _bounded_mapping(
            evidence,
            label="tombstone retention evidence",
            limit=_MAX_RETENTION_RECEIPT_BYTES,
        )
        operation = operation_id or uuid.uuid4().hex
        operation = _bounded_text(operation, label="tombstone retention operation", limit=128)
        identifiers: list[str] = []
        seen: set[str] = set()
        for raw in artifact_ids:
            identifier = _bounded_text(raw, label="tombstone artifact id", limit=MAX_ARTIFACT_ID_BYTES)
            if identifier in seen:
                raise ArtifactSecurityError("tombstone retention selection contains duplicates")
            seen.add(identifier)
            identifiers.append(identifier)
            if len(identifiers) > min(self.max_records, 1_000):
                raise ArtifactSecurityError("tombstone retention selection is truncated")

        retention_root = self.root / _TOMBSTONE_RETENTION_DIR
        try:
            retention_root.mkdir(mode=0o700, exist_ok=True)
            metadata = retention_root.lstat()
        except OSError as exc:
            raise ArtifactRootError("tombstone retention journal is unavailable") from exc
        if _private_directory_issue(metadata, root=True) is not None:
            raise ArtifactRootError("tombstone retention journal failed its private-directory check")
        receipt_path = retention_root / _retention_receipt_name(operation)
        try:
            existing_raw = receipt_path.read_bytes()
        except FileNotFoundError:
            existing_raw = None
        except OSError as exc:
            raise ArtifactManifestError("tombstone retention receipt is unavailable") from exc
        if existing_raw is not None:
            try:
                existing = json.loads(existing_raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ArtifactManifestError("tombstone retention receipt is invalid") from exc
            if not isinstance(existing, Mapping) or existing.get("receipt_digest") != _retention_receipt_digest(existing):
                raise ArtifactManifestError("tombstone retention receipt digest mismatch")
            if existing.get("operation_id") != operation:
                raise ArtifactConflictError("tombstone retention operation id collides")
            return dict(existing)

        receipt: dict[str, Any] = {
            "schema": _TOMBSTONE_RETENTION_SCHEMA,
            "operation_id": operation,
            "owner": self.owner,
            "artifact_ids": identifiers,
            "evidence": evidence_payload,
            "release_authorized": True,
            "status": "applying",
            "items": [],
            "created_ns": time.time_ns(),
        }

        def write_receipt() -> None:
            receipt["receipt_digest"] = _retention_receipt_digest(receipt)
            _write_json_atomic(receipt_path, receipt, exclusive=not receipt_path.exists())

        write_receipt()
        blocked: list[dict[str, object]] = []
        confirmed: list[str] = []
        for identifier in identifiers:
            try:
                manifest_path = self._manifest_path(identifier)
                if not manifest_path.exists():
                    blocked.append(
                        {"artifact_id": identifier, "reason": "tombstone manifest is absent"}
                    )
                    receipt["items"].append(
                        {
                            "artifact_id": identifier,
                            "phase": "recovery_required",
                            "reason": "tombstone manifest is absent",
                        }
                    )
                    write_receipt()
                    continue
                record = self._load_record(manifest_path)
                if not record.verified or record.owner != self.owner:
                    raise ArtifactSecurityError("tombstone is not verified by this owner")
                if record.state != ArtifactState.RETIRED.value:
                    raise ArtifactSecurityError("only retired tombstones can be pruned")
                if record.dependencies:
                    raise ArtifactSecurityError("tombstone retains dependency claims")
                claim = self._retirement_claim(record)
                if claim is not None and claim.get("phase") in _RETIREMENT_PENDING_PHASES:
                    raise ArtifactSecurityError("tombstone has pending recovery")
                record_metadata = record.metadata
                protected_keys = (
                    "pinned",
                    "pin",
                    "grant_active",
                    "authorization_active",
                    "replay_required",
                    "recovery_required",
                    "evidence_required",
                )
                if any(record_metadata.get(key) is True for key in protected_keys):
                    raise ArtifactSecurityError("tombstone retains an active obligation")
                try:
                    record.path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise ArtifactSecurityError("retired payload is still present")
                manifest_metadata = manifest_path.lstat()
                issue = _manifest_file_issue(manifest_metadata)
                if issue is not None:
                    raise ArtifactManifestError(f"tombstone manifest failed safety check: {issue}")
                item: dict[str, Any] = {
                    "artifact_id": identifier,
                    "manifest": str(manifest_path),
                    "manifest_identity": list(_identity(manifest_metadata)),
                    "phase": "applying",
                }
                receipt["items"].append(item)
                write_receipt()
                current = manifest_path.lstat()
                if _identity(current) != tuple(item["manifest_identity"]):
                    raise ArtifactSecurityError("tombstone manifest changed before pruning")
                os.unlink(manifest_path)
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                item["phase"] = "confirmed"
                item["confirmed_ns"] = time.time_ns()
                confirmed.append(identifier)
                write_receipt()
            except (ArtifactRegistryError, OSError, TypeError, ValueError) as exc:
                blocked.append({"artifact_id": identifier, "reason": str(exc)})
                receipt["items"].append(
                    {"artifact_id": identifier, "phase": "recovery_required", "reason": str(exc)}
                )
                write_receipt()

        receipt["status"] = "recovery_required" if blocked else "applied"
        receipt["confirmed"] = len(confirmed)
        receipt["blocked"] = blocked
        receipt["completed_ns"] = time.time_ns()
        write_receipt()
        return dict(receipt)

    @_registry_write_locked
    def recover_tombstone_retention(self) -> dict[str, object]:
        """Confirm or preserve interrupted tombstone-retention receipts."""

        if self.owner is None:
            raise ArtifactSecurityError(
                "federated artifact view is read-only for retention recovery"
            )
        journal = self.root / _TOMBSTONE_RETENTION_DIR
        if not journal.exists():
            return {"schema": _TOMBSTONE_RETENTION_SCHEMA, "status": "ready", "confirmed": 0, "recovery_required": 0}
        entries = sorted(journal.glob("receipt-*.json"))[: min(self.max_records, 1_000)]
        confirmed = 0
        recovery_required = 0
        for receipt_path in entries:
            try:
                payload = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping) or payload.get("receipt_digest") != _retention_receipt_digest(payload):
                    recovery_required += 1
                    continue
                if payload.get("owner") != self.owner:
                    recovery_required += 1
                    continue
                changed = False
                items = payload.get("items")
                if not isinstance(items, list):
                    recovery_required += 1
                    continue
                for item in items:
                    if not isinstance(item, dict) or item.get("phase") != "applying":
                        continue
                    manifest_value = item.get("manifest")
                    if not isinstance(manifest_value, str) or Path(manifest_value).parent != self.root:
                        item["phase"] = "recovery_required"
                        item["reason"] = "manifest path is not registry-local"
                        changed = True
                        continue
                    if not Path(manifest_value).exists():
                        item["phase"] = "confirmed"
                        item["replayed"] = True
                        changed = True
                    else:
                        item["phase"] = "recovery_required"
                        item["reason"] = "manifest still exists after interrupted effect"
                        changed = True
                if changed:
                    phases = [item.get("phase") for item in items if isinstance(item, Mapping)]
                    payload = dict(payload)
                    payload["status"] = "recovery_required" if "recovery_required" in phases else "applied"
                    payload["items"] = items
                    payload["recovered_ns"] = time.time_ns()
                    payload["receipt_digest"] = _retention_receipt_digest(payload)
                    _write_json_atomic(receipt_path, payload, exclusive=False)
                if payload.get("status") == "recovery_required":
                    recovery_required += 1
                else:
                    confirmed += 1
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                recovery_required += 1
        return {
            "schema": _TOMBSTONE_RETENTION_SCHEMA,
            "status": "recovery_required" if recovery_required else "applied",
            "confirmed": confirmed,
            "recovery_required": recovery_required,
        }


# endregion [03]


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_REGISTRY_SCHEMA",
    "ARTIFACT_STATES",
    "MANIFEST_SUFFIX",
    "MAX_DEPENDENCIES",
    "MAX_MANIFEST_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_RECORDS",
    "ArtifactConflictError",
    "ArtifactKind",
    "ArtifactManifestError",
    "ArtifactPlan",
    "ArtifactRecord",
    "ArtifactRegistry",
    "ArtifactRegistryError",
    "ArtifactRootError",
    "ArtifactSecurityError",
    "ArtifactState",
]
