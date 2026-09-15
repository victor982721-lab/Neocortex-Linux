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
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Self


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

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
) -> tuple[int, str | None, bool]:
    """Observe apparent bytes under a claimed directory without following links."""

    total = 0
    entries_seen = 0
    truncated = False
    issue: str | None = None
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            return total, "artifact_missing", truncated
        try:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > max_entries:
                    truncated = True
                    issue = "size_truncated"
                    break
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    issue = "artifact_identity_drift"
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    issue = issue or "artifact_symlink"
                    continue
                # The claimed artifact root is the private boundary.  Files
                # nested inside a generated directory may intentionally use
                # ordinary read permissions; ownership, links, and type are
                # still fail-closed below.  Enforcing 0600 on every nested
                # output would reject valid producer output without improving
                # the registry's no-follow guarantee.
                if metadata.st_uid != os.geteuid():
                    issue = issue or "artifact_owner_drift"
                if stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink != 1:
                        issue = issue or "artifact_hardlink"
                    total += max(0, int(metadata.st_size))
                elif stat.S_ISDIR(metadata.st_mode):
                    stack.append(Path(entry.path))
                else:
                    issue = issue or "artifact_type_drift"
                if total > max_bytes:
                    truncated = True
                    issue = "size_truncated"
                    break
        finally:
            iterator.close()
        if truncated:
            break
    return min(total, max_bytes), issue, truncated


def _path_size_no_follow(path: Path, *, max_entries: int, max_bytes: int) -> tuple[int, str | None]:
    try:
        metadata = path.lstat()
    except OSError:
        return 0, "artifact_missing"
    if stat.S_ISREG(metadata.st_mode):
        return min(max(0, int(metadata.st_size)), max_bytes), None
    if stat.S_ISDIR(metadata.st_mode):
        size, issue, _ = _directory_size_no_follow(
            path,
            max_entries=max_entries,
            max_bytes=max_bytes,
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
        return {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "artifact_id": self.artifact_id,
            "owner": self.owner,
            "producer": self.producer,
            "run_id": self.run_id,
            "purpose": self.purpose,
            "path": str(self.path),
            "root": str(self.root),
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
        return {
            "schema": ARTIFACT_REGISTRY_SCHEMA,
            "root": str(self.root),
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "root_blocked": self.root_blocked,
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
        path = _validate_absolute_path(payload.get("path"), label="artifact path")
        root = _validate_absolute_path(payload.get("root"), label="artifact root")
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
        size, size_issue = _path_size_no_follow(
            record.path,
            max_entries=self.max_records,
            max_bytes=self.max_bytes,
        )
        if size_issue is not None:
            return replace(record, size_bytes=size, valid=False, issue=size_issue, reason=size_issue)
        return replace(record, size_bytes=size, valid=True, issue=None, reason=None)

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
            root=self.root,
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
            raise ArtifactConflictError("artifact id is already registered with different fields")
        return self._load_record(manifest_path)

    def _write_registration(self, manifest_path: Path, payload: Mapping[str, Any]) -> None:
        if len(_canonical_json(payload).encode("utf-8")) > self.max_manifest_bytes:
            raise ValueError("artifact manifest exceeds the configured size limit")
        _write_json_atomic(manifest_path, payload, exclusive=True)
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

    def update(
        self,
        artifact: str | ArtifactRecord,
        *,
        producer: str | object = _UNSET,
        run_id: int | str | None | object = _UNSET,
        purpose: str | object = _UNSET,
        state: str | object = _UNSET,
        source_ref: Any = _UNSET,
        digest: str | None | object = _UNSET,
        dependencies: Iterable[str] | None | object = _UNSET,
        retain_until_ns: int | None | object = _UNSET,
        ttl_ns: int | None | object = _UNSET,
        ttl: int | None | object = _UNSET,
        disposable: bool | object = _UNSET,
        metadata: Mapping[str, Any] | None | object = _UNSET,
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
        if not current.verified:
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
            merged["retain_until_ns"] = (
                None
                if normalized["ttl_ns"] is None
                else current.created_ns + normalized["ttl_ns"]
            )
        if "retain_until_ns" in normalized and normalized["retain_until_ns"] is None:
            # Clearing an explicit deadline also clears no TTL; a non-null TTL
            # still gives the record its derived deadline at classification.
            pass
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
                        raise ArtifactManifestError(result.reason or "artifact manifest invalid")
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
                target,
                label="artifact_id",
                limit=MAX_ARTIFACT_ID_BYTES,
            )
        self._ensure_root(create=False)
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
        categories = {name: 0 for name in ("protected", "eligible", "blocked", "unknown")}
        byte_categories = {name: 0 for name in categories}
        reasons: dict[str, int] = {}
        observed_records: list[ArtifactRecord] = []
        total_bytes = 0
        byte_truncated = False
        for record in records:
            category, reason = self._classify_for_owner(record, now_ns=now)
            eligible = category == "eligible"
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


# endregion [03]


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_REGISTRY_SCHEMA",
    "ARTIFACT_STATES",
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
    "MANIFEST_SUFFIX",
    "MAX_DEPENDENCIES",
    "MAX_MANIFEST_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_RECORDS",
]
