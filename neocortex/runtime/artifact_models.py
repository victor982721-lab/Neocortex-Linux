"""Value objects and bounded validation for the artifact registry.

The registry lifecycle remains in :mod:`artifact_registry`; this module owns
the closed vocabularies, manifest-safe value objects, and no-follow
observation primitives shared by registration, planning, and recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
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

