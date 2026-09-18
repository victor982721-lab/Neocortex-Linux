"""Bounded, evidence-gated adoption of historical NeoCortex directories.

The historical audit is intentionally a separate owner from registered
scratch.  It never discovers a root from configuration and it never treats a
name, an age, or a size as proof that an entry belongs to NeoCortex.  A caller
must pass one absolute directory explicitly.  Planning reads metadata and a
small, allow-listed manifest only; applying revalidates the same claims and
removes only an explicitly approved entry through descriptor-relative,
no-follow operations.

This module does not inspect ``/tmp`` by default, does not open SQLite, does
not invoke KIO, and makes no claim about filesystem ``df`` space.  The public
plan reports only bounded apparent and allocated bytes observed by this
service.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

HISTORICAL_AUDIT_SCHEMA = "neocortex.historical-audit/v1"
HISTORICAL_RECEIPT_SCHEMA = "neocortex.historical-audit-receipt/v1"

# A historical manifest is accepted only when it comes from one of the
# producer contracts that this owner understands.  A broad ``startswith``
# check would make a future/foreign schema look authoritative merely because
# it uses the NeoCortex namespace.
_SUPPORTED_MANIFEST_SCHEMAS = frozenset({"neocortex.scratch/v1"})
_HISTORICAL_OWNER = "neocortex-framework"
_RECEIPT_DIRECTORY = ".neocortex-historical-audit"
_MAX_RECEIPT_BYTES = 64 * 1024

_MANIFEST_NAMES = frozenset(
    {
        "manifest.json",
        ".neocortex-scratch.json",
        "neocortex-release.json",
    }
)
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_REASON_BYTES = 8 * 1024
_MAX_MANIFEST_DIGEST_BYTES = 128
_MAX_ADOPTION_ID_BYTES = 256
_MAX_MOUNTINFO_BYTES = 4 * 1024 * 1024
# Keep the direct owner API bounded as well as the CLI.  A caller using the
# Python surface must not be able to turn a historical audit into an
# unbounded traversal merely by bypassing argparse.
_MAX_API_ENTRIES = 100_000
_MAX_API_DEPTH = 64
_MAX_API_BYTES = 1 << 40


class HistoricalAuditError(RuntimeError):
    """Base class for a historical-audit contract violation."""


class HistoricalRootError(HistoricalAuditError):
    """The explicit historical root cannot safely be inspected."""


class _IdentityDrift(HistoricalAuditError):
    """Internal marker for a concurrent replacement at an effect boundary."""


class _EffectPartial(HistoricalAuditError):
    """Some bytes were removed but the retirement could not be completed."""


def _canonical_json(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if any(0xD800 <= ord(character) <= 0xDFFF for character in rendered):
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                          separators=(",", ":"))
    return rendered


def _bounded_reason(value: object) -> str:
    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_REASON_BYTES:
        return text
    return encoded[:_MAX_REASON_BYTES].decode("utf-8", errors="replace")


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    # Linux does not expose a creation time consistently.  ``-1`` is an
    # explicit unavailable sentinel; ctime is not a creation-time substitute.
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(birthtime) if birthtime is not None else -1,
    )


def _parse_identity(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(type(item) is not int for item in value):
        return None
    return (int(value[0]), int(value[1]), int(value[2]))


def _same_identity(value: object, actual: tuple[int, int, int] | None) -> bool:
    if actual is None:
        return False
    parsed = _parse_identity(value)
    return parsed is not None and parsed == actual


def _is_receipt_name(name: str) -> bool:
    return name.endswith(".receipt.json") and name != ".receipt.json"


def _is_retirement_receipt_name(name: str) -> bool:
    """Recognize the hash-named receipts stored in the private directory."""

    return name.endswith(".json") and not name.startswith(".")


def _is_manifest_name(name: str) -> bool:
    return name in _MANIFEST_NAMES or _is_receipt_name(name)


def _path_components_have_no_symlinks(path: Path) -> None:
    """Reject symlink path components before opening the explicit root."""

    cursor = Path(path.anchor)
    # ``Path.parts`` for an absolute POSIX path starts with ``/``.
    for part in path.parts[1:]:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            # A missing component is handled by the root validator.  Do not
            # attempt to create or resolve it here.
            break
        except OSError as exc:
            raise HistoricalRootError(f"historical root is unavailable: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HistoricalRootError("historical root cannot contain symlink components")


def _safe_mode(metadata: os.stat_result, *, private: bool) -> bool:
    # The root may be searchable/readable by the current user without being
    # private, but no untrusted account may mutate it while an audit runs.
    # Candidate roots/manifests are private claims and therefore use the
    # stricter check.
    mask = 0o077 if private else 0o022
    return not bool(metadata.st_mode & mask)


def _safe_open_read(path: Path, *, limit: int) -> tuple[bytes, os.stat_result]:
    """Read one regular file with O_NOFOLLOW and an identity check."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise HistoricalAuditError(f"manifest is unavailable: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise HistoricalAuditError("manifest is not a regular file")
    if before.st_uid != os.geteuid() or before.st_nlink != 1 or not _safe_mode(before, private=True):
        raise HistoricalAuditError("manifest protection is unsafe")
    if before.st_size > limit:
        raise HistoricalAuditError("manifest exceeds the bounded size limit")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HistoricalAuditError(f"manifest cannot be opened safely: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(before)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or not _safe_mode(opened, private=True)
        ):
            raise HistoricalAuditError("manifest identity changed while reading")
        if opened.st_size > limit:
            raise HistoricalAuditError("manifest exceeds the bounded size limit")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise HistoricalAuditError("manifest exceeds the bounded size limit")
        return bytes(data), opened
    finally:
        os.close(fd)


def _receipt_token(record: "HistoricalRecord") -> str:
    """Derive a filesystem-safe, deterministic receipt name."""

    material = {
        "path": str(record.path),
        "path_identity": None
        if record.path_identity is None
        else list(record.path_identity),
        "manifest_digest": record.manifest_digest,
        "adoption_id": record.adoption_id,
        "adoption_digest": record.adoption_digest,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _receipt_payload(
    record: "HistoricalRecord",
    root_identity: tuple[int, int, int],
) -> dict[str, object]:
    """Build the bounded intent receipt written before a retirement effect."""

    return {
        "schema": HISTORICAL_RECEIPT_SCHEMA,
        "state": "prepared",
        "root": str(record.path.parent),
        "root_identity": list(root_identity),
        "path": str(record.path),
        "path_identity": (
            None if record.path_identity is None else list(record.path_identity)
        ),
        "manifest_path": (
            None if record.manifest_path is None else str(record.manifest_path)
        ),
        "manifest_identity": (
            None
            if record.manifest_identity is None
            else list(record.manifest_identity)
        ),
        "manifest_digest": record.manifest_digest,
        "adoption_id": record.adoption_id,
        "adoption_digest": record.adoption_digest,
        "owner": record.owner,
        "observed_bytes": record.observed_bytes,
        "apparent_bytes": record.apparent_bytes,
        "allocated_bytes": record.allocated_bytes,
    }


def _with_receipt_digest(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a receipt payload with a self-consistency digest."""

    body = dict(payload)
    body.pop("receipt_digest", None)
    body["receipt_digest"] = (
        "sha256:" + hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    )
    return body


def _valid_receipt_digest(payload: Mapping[str, object]) -> bool:
    digest = payload.get("receipt_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return False
    body = dict(payload)
    body.pop("receipt_digest", None)
    return digest == (
        "sha256:" + hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    )


def _receipt_directory_fd(
    root_fd: int,
    *,
    root_path: Path,
    root_device: int,
    mountpoints: frozenset[Path],
    create: bool = True,
) -> int:
    """Open/create the private receipt directory relative to the root fd."""

    receipt_path = _lexical_path(root_path / _RECEIPT_DIRECTORY)
    if receipt_path in mountpoints:
        raise HistoricalAuditError("historical receipt directory is a mount boundary")

    if create:
        try:
            os.mkdir(_RECEIPT_DIRECTORY, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
    try:
        before = os.stat(_RECEIPT_DIRECTORY, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        raise HistoricalAuditError("historical receipt directory is unavailable") from None
    except OSError as exc:
        raise HistoricalAuditError("historical receipt directory is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_dev != root_device
        or not _safe_mode(before, private=True)
    ):
        raise HistoricalAuditError("historical receipt directory is unsafe")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(_RECEIPT_DIRECTORY, flags, dir_fd=root_fd)
    except OSError as exc:
        raise HistoricalAuditError("historical receipt directory cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            _identity(opened) != _identity(before)
            or opened.st_dev != root_device
            or not _safe_mode(opened, private=True)
        ):
            raise _IdentityDrift("historical receipt directory identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _bounded_names_fd(directory_fd: int, max_entries: int) -> tuple[list[str], bool]:
    """Read directory names with a hard memory bound before sorting."""

    names: list[str] = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            if len(names) >= max_entries:
                return sorted(names), True
            names.append(entry.name)
    return sorted(names), False


def _read_receipt_fd(directory_fd: int, name: str) -> Mapping[str, object] | None:
    """Read one existing receipt without following a replacement link."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not _safe_mode(before, private=True)
            or before.st_size > _MAX_RECEIPT_BYTES
        ):
            return None
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            _identity(opened) != _identity(before)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or not _safe_mode(opened, private=True)
            or opened.st_size > _MAX_RECEIPT_BYTES
        ):
            return None
        data = bytearray()
        while len(data) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_RECEIPT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_RECEIPT_BYTES:
            return None
        payload = json.loads(bytes(data).decode("utf-8"))
        if not isinstance(payload, Mapping) or not _valid_receipt_digest(payload):
            return None
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    finally:
        os.close(descriptor)


def _receipt_matches(
    existing: Mapping[str, object] | None,
    expected: Mapping[str, object],
) -> bool:
    """Check the immutable claims of an existing prepared receipt."""

    if existing is None:
        return False
    immutable = (
        "schema",
        "state",
        "root",
        "root_identity",
        "path",
        "path_identity",
        "manifest_path",
        "manifest_identity",
        "manifest_digest",
        "adoption_id",
        "adoption_digest",
        "owner",
        "observed_bytes",
        "apparent_bytes",
        "allocated_bytes",
        "receipt_digest",
    )
    return all(existing.get(name) == expected.get(name) for name in immutable)


def _write_retirement_receipt(
    root_fd: int,
    record: "HistoricalRecord",
    root_identity: tuple[int, int, int],
    mountpoints: frozenset[Path],
) -> None:
    """Durably record retirement intent before removing any candidate bytes."""

    directory_fd = _receipt_directory_fd(
        root_fd,
        root_path=record.path.parent,
        root_device=root_identity[0],
        mountpoints=mountpoints,
    )
    try:
        payload = _with_receipt_digest(_receipt_payload(record, root_identity))
        name = f"{_receipt_token(record)}.json"
        encoded = _canonical_json(payload).encode("utf-8")
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise HistoricalAuditError("historical retirement receipt exceeds its size limit")
        existing = _read_receipt_fd(directory_fd, name)
        if existing is not None:
            if not _receipt_matches(existing, payload):
                raise HistoricalAuditError("historical retirement receipt claim mismatch")
            return
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            # A concurrent writer won the race.  Re-read it and require the
            # exact same immutable claim rather than replacing an unknown
            # receipt.
            concurrent = _read_receipt_fd(directory_fd, name)
            if not _receipt_matches(concurrent, payload):
                raise HistoricalAuditError("historical retirement receipt claim mismatch") from exc
            return
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or not _safe_mode(metadata, private=True)
            ):
                raise HistoricalAuditError("historical retirement receipt is unsafe")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _finalize_retirement_receipt(
    root_fd: int,
    record: "HistoricalRecord",
    root_identity: tuple[int, int, int],
    mountpoints: frozenset[Path],
) -> Path:
    """Mark a prepared receipt applied only after the target is absent."""

    try:
        os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _EffectPartial("retirement postcondition could not be verified") from exc
    else:
        raise _EffectPartial("retirement postcondition failed: entry still exists")

    directory_fd = _receipt_directory_fd(
        root_fd,
        root_path=record.path.parent,
        root_device=root_identity[0],
        mountpoints=mountpoints,
    )
    name = f"{_receipt_token(record)}.json"
    try:
        existing = _read_receipt_fd(directory_fd, name)
        expected = _with_receipt_digest(_receipt_payload(record, root_identity))
        if not _receipt_matches(existing, expected):
            raise _EffectPartial("prepared retirement receipt is missing or mismatched")
        finalized = dict(expected)
        finalized["state"] = "applied"
        finalized["postcondition"] = "entry_absent"
        finalized = _with_receipt_digest(finalized)
        encoded = _canonical_json(finalized).encode("utf-8")
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        return Path(record.path.parent) / _RECEIPT_DIRECTORY / name
    except _EffectPartial:
        raise
    except (OSError, HistoricalAuditError) as exc:
        raise _EffectPartial("retirement receipt could not be finalized") from exc
    finally:
        os.close(directory_fd)


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    # Adoption attestations bind their digest to the manifest, but the digest
    # field itself must not create a recursive hash equation.  Excluding only
    # this narrowly-defined nested field keeps the ordinary scratch/release
    # digest convention intact while making an adoption attestation
    # representable and deterministic.
    adoption = unsigned.get("historical_adoption")
    if isinstance(adoption, Mapping) and (
        "digest" in adoption or "manifest_digest" in adoption
    ):
        unsigned_adoption = dict(adoption)
        unsigned_adoption.pop("digest", None)
        unsigned_adoption.pop("manifest_digest", None)
        unsigned["historical_adoption"] = unsigned_adoption
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _adoption_claim_digest(adoption: Mapping[str, Any]) -> str:
    unsigned = dict(adoption)
    unsigned.pop("digest", None)
    unsigned.pop("manifest_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _verify_optional_digest(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("manifest_digest")
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_MANIFEST_DIGEST_BYTES:
        raise HistoricalAuditError("manifest digest is malformed")
    if value != _manifest_digest(payload):
        raise HistoricalAuditError("manifest digest mismatch")
    return value


def _app_manifest(payload: Mapping[str, Any]) -> bool:
    schema = payload.get("schema")
    if isinstance(schema, str) and schema in _SUPPORTED_MANIFEST_SCHEMAS:
        return True
    kind = payload.get("kind")
    # Release manifests are recognized for classification only.  They do not
    # carry historical-adoption claims and therefore can never become
    # candidates through this service.
    return kind == "linux_release_manifest" and schema is None


def _activity_uncertain(payload: Mapping[str, Any]) -> bool:
    """Require an explicit inactive/uncertain=false attestation."""

    candidates: list[bool] = []
    containers: list[Mapping[str, Any]] = [payload]
    adoption = payload.get("historical_adoption")
    if isinstance(adoption, Mapping):
        containers.append(adoption)
    for container in containers:
        value = container.get("activity_uncertain")
        if type(value) is bool:
            candidates.append(value)
        value = container.get("active")
        if type(value) is bool:
            candidates.append(value)
        activity = container.get("activity")
        if isinstance(activity, Mapping):
            value = activity.get("uncertain")
            if type(value) is bool:
                candidates.append(value)
            value = activity.get("active")
            if type(value) is bool:
                candidates.append(value)
    # A single explicit true must never be overridden by another field.  An
    # explicit ``active=false`` is accepted as the same positive assertion as
    # ``activity_uncertain=false``.
    return any(candidates) if candidates else True


def _claim_string(payload: Mapping[str, Any], names: Sequence[str]) -> str | None:
    values = [payload[name] for name in names if name in payload]
    if not values or any(not isinstance(value, str) for value in values):
        return None
    if len(set(values)) != 1:
        return None
    return values[0]


def _decode_mountinfo_path(value: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return re.sub(r"\\([0-7]{3})", replace_escape, value)


def _lexical_path(path: Path) -> Path:
    """Normalize ``.``/``..`` without resolving symlinks or touching disk."""

    normalized = os.path.normpath(str(path))
    # POSIX permits an implementation-defined special meaning for exactly two
    # leading slashes.  Mountinfo uses the ordinary single-slash spelling;
    # collapse that lexical alias without resolving any component.
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return Path(normalized)


def _mountinfo_snapshot() -> tuple[frozenset[Path], str] | None:
    """Read a bounded mount topology snapshot, or abstain if unverifiable."""

    try:
        with open("/proc/self/mountinfo", "rb") as stream:
            raw = stream.read(_MAX_MOUNTINFO_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_MOUNTINFO_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None
    mountpoints: set[Path] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or "-" not in fields[6:]:
            return None
        mountpoint = _decode_mountinfo_path(fields[4])
        if not mountpoint.startswith("/") or "\x00" in mountpoint:
            return None
        mountpoints.add(_lexical_path(Path(mountpoint)))
    return frozenset(mountpoints), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalRecord:
    """One bounded observation of an explicitly prefixed direct child."""

    path: Path
    name: str
    status: str
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    observed_bytes: int = 0
    proposed_bytes: int = 0
    path_identity: tuple[int, int, int] | None = None
    root_identity: tuple[int, int, int] | None = None
    manifest_path: Path | None = None
    manifest_name: str | None = None
    manifest_identity: tuple[int, int, int] | None = None
    manifest_schema: str | None = None
    manifest_digest: str | None = None
    adoption_id: str | None = None
    adoption_digest: str | None = None
    owner: str | None = None
    reason: str | None = None
    truncated: bool = False
    adoptable: bool = False
    valid_manifest: bool = False
    activity_uncertain: bool = True
    owner_uncertain: bool = False
    identity_uncertain: bool = False
    is_directory: bool = False

    @property
    def state(self) -> str:
        """Compatibility alias for callers that use lifecycle vocabulary."""

        return self.status

    @property
    def eligible(self) -> bool:
        return self.adoptable

    @property
    def identity(self) -> tuple[int, int, int] | None:
        return self.path_identity

    @property
    def size_bytes(self) -> int:
        return self.observed_bytes

    @property
    def bytes(self) -> int:
        return self.observed_bytes

    def to_dict(self) -> dict[str, object]:
        from neocortex.runtime.path_identity import PathIdentity
        return {
            "path": str(self.path),
            "posix_path_identity": PathIdentity.from_path(self.path).as_dict(),
            "name": self.name,
            "status": self.status,
            "apparent_bytes": self.apparent_bytes,
            "allocated_bytes": self.allocated_bytes,
            "observed_bytes": self.observed_bytes,
            "proposed_bytes": self.proposed_bytes,
            "path_identity": (
                None if self.path_identity is None else list(self.path_identity)
            ),
            "root_identity": (
                None if self.root_identity is None else list(self.root_identity)
            ),
            "manifest_path": None if self.manifest_path is None else str(self.manifest_path),
            "manifest_name": self.manifest_name,
            "manifest_identity": (
                None if self.manifest_identity is None else list(self.manifest_identity)
            ),
            "manifest_schema": self.manifest_schema,
            "manifest_digest": self.manifest_digest,
            "adoption_id": self.adoption_id,
            "adoption_digest": self.adoption_digest,
            "owner": self.owner,
            "reason": self.reason,
            "truncated": self.truncated,
            "adoptable": self.adoptable,
            "valid_manifest": self.valid_manifest,
            "activity_uncertain": self.activity_uncertain,
            "owner_uncertain": self.owner_uncertain,
            "identity_uncertain": self.identity_uncertain,
            "is_directory": self.is_directory,
        }


@dataclass(frozen=True, slots=True)
class HistoricalAuditPlan:
    """Bounded result of one plan or apply pass."""

    root: Path
    records: tuple[HistoricalRecord, ...] = ()
    unmanaged: tuple[Path, ...] = ()
    scanned: int = 0
    adoptable: int = 0
    planned: int = 0
    applied: int = 0
    kept: int = 0
    blocked: int = 0
    unknown: int = 0
    active: int = 0
    failed: int = 0
    recovery_required: int = 0
    observed_bytes: int = 0
    proposed_bytes: int = 0
    applied_bytes: int = 0
    active_bytes: int = 0
    observed_apparent_bytes: int = 0
    observed_allocated_bytes: int = 0
    proposed_apparent_bytes: int = 0
    proposed_allocated_bytes: int = 0
    applied_apparent_bytes: int = 0
    applied_allocated_bytes: int = 0
    status: str = "planned"
    reason: str | None = None
    read_only: bool = True
    root_identity: tuple[int, int, int] | None = None
    root_blocked: str | None = None
    truncated: bool = False
    receipts: tuple[Path, ...] = ()
    max_entries: int = 0
    max_depth: int = 0
    max_bytes: int = 0

    @property
    def entries(self) -> tuple[HistoricalRecord, ...]:
        return self.records

    @property
    def items(self) -> tuple[HistoricalRecord, ...]:
        return self.records

    @property
    def planned_bytes(self) -> int:
        return self.proposed_bytes

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "observed": self.observed_bytes,
            "proposed": self.proposed_bytes,
            "applied": self.applied_bytes,
            "active": self.active_bytes,
        }

    @property
    def counts(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "adoptable": self.adoptable,
            "planned": self.planned,
            "applied": self.applied,
            "kept": self.kept,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "active": self.active,
            "failed": self.failed,
            "recovery_required": self.recovery_required,
        }

    @property
    def status_counts(self) -> dict[str, int]:
        """Return bounded lifecycle counts for every observed record."""

        statuses = (
            "adoptable",
            "kept",
            "active",
            "blocked",
            "unknown",
            "failed",
            "recovery_required",
        )
        return {status: sum(item.status == status for item in self.records) for status in statuses}

    @property
    def limits(self) -> dict[str, int]:
        """Return the effective bounded scan limits used by this pass."""

        return {
            "max_entries": self.max_entries,
            "max_depth": self.max_depth,
            "max_bytes": self.max_bytes,
        }

    @property
    def reason_summary(self) -> tuple[dict[str, object], ...]:
        """Explain why records remain protected, bounded and non-adoptable.

        The summary deliberately uses a closed set of reason keys rather than
        echoing arbitrary manifest text or full error messages.  It gives a
        caller an actionable explanation while keeping the output bounded and
        avoiding a second authority for deletion.
        """

        grouped: dict[str, dict[str, object]] = {}
        for record in self.records:
            key = _reason_key(record)
            bucket = grouped.setdefault(
                key,
                {
                    "key": key,
                    "explanation": _reason_explanation(key),
                    "count": 0,
                    "observed_bytes": 0,
                    "sample_paths": [],
                },
            )
            count_value = bucket.get("count")
            bytes_value = bucket.get("observed_bytes")
            bucket["count"] = (count_value if isinstance(count_value, int) else 0) + 1
            bucket["observed_bytes"] = (
                (bytes_value if isinstance(bytes_value, int) else 0)
                + record.observed_bytes
            )
            samples = bucket["sample_paths"]
            if isinstance(samples, list) and len(samples) < 3:
                samples.append(str(record.path))
        def bucket_count(item: Mapping[str, object]) -> int:
            value = item.get("count")
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        return tuple(
            sorted(
                grouped.values(),
                key=lambda item: (-bucket_count(item), str(item.get("key", ""))),
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": HISTORICAL_AUDIT_SCHEMA,
            "root": str(self.root),
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "root_blocked": self.root_blocked,
            "status": self.status,
            "reason": self.reason,
            "read_only": self.read_only,
            "truncated": self.truncated,
            "counts": self.counts,
            "bytes": {
                "observed": self.observed_bytes,
                "proposed": self.proposed_bytes,
                "applied": self.applied_bytes,
                "active": self.active_bytes,
                "observed_apparent": self.observed_apparent_bytes,
                "observed_allocated": self.observed_allocated_bytes,
                "proposed_apparent": self.proposed_apparent_bytes,
                "proposed_allocated": self.proposed_allocated_bytes,
                "applied_apparent": self.applied_apparent_bytes,
                "applied_allocated": self.applied_allocated_bytes,
            },
            "records": [record.to_dict() for record in self.records],
            "unmanaged": [str(path) for path in self.unmanaged],
            "receipts": [str(path) for path in self.receipts],
            "limits": {
                "max_entries": self.max_entries,
                "max_depth": self.max_depth,
                "max_bytes": self.max_bytes,
            },
            "status_counts": self.status_counts,
            "reason_summary": list(self.reason_summary),
        }


@dataclass
class _Budget:
    max_entries: int
    max_bytes: int
    entries: int = 0
    bytes: int = 0
    truncated: bool = False

    def consume(self, metadata: os.stat_result) -> tuple[int, int, int, bool]:
        """Return credited apparent/allocated bytes and whether it was bounded."""

        if self.entries >= self.max_entries:
            self.truncated = True
            return 0, 0, 0, True
        self.entries += 1
        apparent = max(0, int(metadata.st_size)) if (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
        ) else 0
        allocated = (
            max(0, int(metadata.st_blocks)) * 512
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            else 0
        )
        contribution = apparent + allocated
        remaining = max(0, self.max_bytes - self.bytes)
        credited = min(contribution, remaining)
        credited_apparent = min(apparent, credited)
        credited_allocated = min(allocated, max(0, credited - credited_apparent))
        self.bytes += credited
        bounded = credited < contribution
        if bounded:
            self.truncated = True
        return credited_apparent, credited_allocated, credited, bounded


@dataclass
class _TreeResult:
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    observed_bytes: int = 0
    truncated: bool = False
    issues: list[str] = field(default_factory=list)
    owner_uncertain: bool = False
    identity_uncertain: bool = False
    manifest_paths: list[Path] = field(default_factory=list)

    def issue(self, value: str) -> None:
        if value not in self.issues and len(self.issues) < 8:
            self.issues.append(value)


@dataclass(frozen=True)
class _ManifestObservation:
    path: Path
    identity: tuple[int, int, int]
    payload: Mapping[str, Any]
    digest: str | None
    schema: str | None


_REASON_EXPLANATIONS: dict[str, str] = {
    "no_manifest": "No hay manifest allow-listed que vincule productor, owner y ciclo de vida.",
    "manifest_invalid": "El manifest existe, pero su JSON, tamaño, permisos o digest no son verificables.",
    "foreign_manifest": "El manifest pertenece a otra aplicación o a un schema no soportado.",
    "owner_unsupported": "El owner lógico no está registrado en este servicio histórico.",
    "identity_claim_mismatch": "La ruta, raíz o identidad física declarada no coincide con el objeto observado.",
    "activity_uncertain": "No existe una atestación confiable de que el trabajo esté inactivo.",
    "adoption_binding_missing": "Faltan adoption_id/digest ligados al manifest autenticado.",
    "adoption_not_approved": "El estado o la política no declaran explícitamente que el artefacto sea desechable.",
    "active_lifecycle": "El productor declara el artefacto activo o en progreso.",
    "recovery_required": "Existe una condición de recuperación o un efecto previo incompleto.",
    "permissions_unsafe": "Owner, permisos o enlaces permiten una sustitución o acceso no seguro.",
    "symlink": "Se detectó un enlace simbólico; no se sigue ni se usa como autoridad.",
    "hardlink": "Se detectó un hardlink compartido; retirar el nombre podría afectar otro consumidor.",
    "mount_boundary": "El objeto cruza o coincide con un límite de montaje no adoptable.",
    "unsupported_type": "El tipo de archivo no es regular/directorio seguro para este owner.",
    "bounds_exceeded": "La observación alcanzó el límite de entradas, profundidad o bytes; la cobertura es incompleta.",
    "status": "El estado observado no tiene una explicación reconocida por el contrato.",
}


def _reason_key(record: "HistoricalRecord") -> str:
    reason = (record.reason or "").casefold()
    if record.truncated or any(
        marker in reason
        for marker in (
            "bounds",
            "entry limit",
            "depth limit",
            "byte limit",
            "exceeds the entry",
        )
    ):
        return "bounds_exceeded"
    markers = (
        ("no allow-listed manifest", "no_manifest"),
        ("manifest is invalid", "manifest_invalid"),
        ("another application", "foreign_manifest"),
        ("manifest owner is unsupported", "owner_unsupported"),
        ("path/root/identity claim", "identity_claim_mismatch"),
        ("activity is uncertain", "activity_uncertain"),
        ("adoption id/digest binding", "adoption_binding_missing"),
        ("not approved for adoption", "adoption_not_approved"),
        ("active or in progress", "active_lifecycle"),
        ("requires recovery", "recovery_required"),
        ("permissions", "permissions_unsafe"),
        ("symlink", "symlink"),
        ("hardlink", "hardlink"),
        ("mount", "mount_boundary"),
        ("unsupported", "unsupported_type"),
    )
    for marker, key in markers:
        if marker in reason:
            return key
    return record.status if record.status in _REASON_EXPLANATIONS else "status"


def _reason_explanation(key: str) -> str:
    return _REASON_EXPLANATIONS.get(key, _REASON_EXPLANATIONS["status"])


class HistoricalAuditManager:
    """Audit and, only with explicit evidence, retire historical entries."""

    def __init__(
        self,
        root: Path,
        *,
        prefix: str = "neocortex-",
        max_entries: int = 10_000,
        max_depth: int = 2,
        max_bytes: int = 1 << 40,
        state_directory: Path | None = None,
        owner: str = _HISTORICAL_OWNER,
    ) -> None:
        try:
            candidate = Path(root)
        except (TypeError, ValueError) as exc:
            raise HistoricalRootError("historical root must be an absolute path") from exc
        if not candidate.is_absolute():
            raise HistoricalRootError("historical root must be an absolute path")
        if not isinstance(prefix, str) or not prefix or "/" in prefix or "\x00" in prefix:
            raise ValueError("historical prefix must be one non-empty path component")
        if len(prefix.encode("utf-8")) > 256:
            raise ValueError("historical prefix is too long")
        for name, value in (
            ("max_entries", max_entries),
            ("max_depth", max_depth),
            ("max_bytes", max_bytes),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if max_entries > _MAX_API_ENTRIES:
            raise ValueError(f"max_entries cannot exceed {_MAX_API_ENTRIES}")
        if max_depth > _MAX_API_DEPTH:
            raise ValueError(f"max_depth cannot exceed {_MAX_API_DEPTH}")
        if max_bytes > _MAX_API_BYTES:
            raise ValueError(f"max_bytes cannot exceed {_MAX_API_BYTES}")
        if max_entries == 0:
            raise ValueError("max_entries must be positive")
        self.root = candidate
        self.prefix = prefix
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.max_bytes = max_bytes
        if state_directory is not None and not Path(state_directory).is_absolute():
            raise HistoricalRootError("adoption state directory must be absolute")
        self.state_directory = None if state_directory is None else Path(state_directory)
        if not isinstance(owner, str) or not owner or len(owner.encode("utf-8")) > 256:
            raise ValueError("historical owner must be bounded non-empty text")
        self.owner = owner

    def plan_selected(self, selections: Sequence[Any], *, partial: bool = False,
                      deadline_ns: int | None = None, cancelled: Any = None) -> Any:
        """Inspect exact descendants using already registered producer evidence."""
        from neocortex.runtime.historical_adoption import HistoricalAdoption
        return HistoricalAdoption(self).plan(selections, partial=partial,
                                             deadline_ns=deadline_ns, cancelled=cancelled)

    def prepare_adoption(self, plan: Any) -> Any:
        """Persist an immutable proposal; this does not authorize retirement."""
        from neocortex.runtime.historical_adoption import HistoricalAdoption
        return HistoricalAdoption(self).prepare(plan)

    def adoption_plan(self, plan_digest: str) -> Any:
        """Load an authenticated proposal without widening its selection."""
        from neocortex.runtime.historical_adoption import HistoricalAdoption
        return HistoricalAdoption(self).load_plan(plan_digest)

    def approve_adoption(self, plan_digest: str, selected_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Record the explicit local operator's approval for an exact proposal."""
        from neocortex.runtime.historical_adoption import HistoricalAdoption
        return HistoricalAdoption(self).approve(plan_digest, selected_ids)

    def apply_selected(self, plan_digest: str, selected_ids: Sequence[str] | None = None,
                       *, deadline_ns: int | None = None, cancelled: Any = None) -> dict[str, Any]:
        """Consume existing exact approval and reconcile prior effects on replay."""
        from neocortex.runtime.historical_adoption import HistoricalAdoption
        return HistoricalAdoption(self).apply(plan_digest, selected_ids,
                                              deadline_ns=deadline_ns, cancelled=cancelled)

    def _root_metadata(self, *, allow_shared_read: bool = False) -> os.stat_result:
        _path_components_have_no_symlinks(self.root)
        try:
            metadata = self.root.lstat()
        except FileNotFoundError as exc:
            raise HistoricalRootError("historical root is absent") from exc
        except OSError as exc:
            raise HistoricalRootError(f"historical root is unavailable: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HistoricalRootError("historical root must be a directory, not a symlink")
        shared_sticky_root = bool(metadata.st_mode & stat.S_ISVTX) and bool(
            metadata.st_mode & 0o002
        )
        if metadata.st_uid != os.geteuid() and not (allow_shared_read and shared_sticky_root):
            raise HistoricalRootError("historical root is not owned by the current user")
        if not _safe_mode(metadata, private=False) and not (
            allow_shared_read and shared_sticky_root
        ):
            raise HistoricalRootError("historical root permissions are unsafe")
        return metadata

    def _empty_root_plan(self, reason: str, *, read_only: bool) -> HistoricalAuditPlan:
        return HistoricalAuditPlan(
            root=self.root,
            status="blocked",
            reason=reason,
            read_only=read_only,
            root_blocked=reason,
            max_entries=self.max_entries,
            max_depth=self.max_depth,
            max_bytes=self.max_bytes,
        )

    def plan(self, now_ns: int | None = None) -> HistoricalAuditPlan:
        """Return a bounded read-only adoption plan for this explicit root."""

        now = _validate_now(now_ns)
        del now  # The current contract does not infer eligibility from time.
        try:
            root_metadata = self._root_metadata(allow_shared_read=True)
        except HistoricalRootError as exc:
            return self._empty_root_plan(_bounded_reason(exc), read_only=True)
        root_identity = _identity(root_metadata)
        mount_snapshot = _mountinfo_snapshot()
        if mount_snapshot is None:
            return self._empty_root_plan(
                "mount topology could not be verified",
                read_only=True,
            )
        mountpoints, mount_digest = mount_snapshot
        records, unmanaged, truncated, reason = self._scan(root_identity, mountpoints)
        if reason and reason.startswith("historical root scan failed:"):
            return self._empty_root_plan(reason, read_only=True)
        if truncated:
            bound_reason = reason or "historical audit bounds were exceeded"
            records = tuple(
                replace(
                    record,
                    status="blocked" if record.adoptable else record.status,
                    adoptable=False,
                    proposed_bytes=0,
                    reason=bound_reason if record.adoptable else record.reason,
                )
                for record in records
            )
            reason = bound_reason
        final_mount_snapshot = _mountinfo_snapshot()
        if final_mount_snapshot is None or final_mount_snapshot[1] != mount_digest:
            records = tuple(
                replace(
                    record,
                    status="recovery_required",
                    adoptable=False,
                    proposed_bytes=0,
                    reason="mount topology changed during audit",
                )
                for record in records
            )
            reason = "mount topology changed during audit"
            truncated = True
        return self._summarize(
            records,
            unmanaged=unmanaged,
            root_identity=root_identity,
            truncated=truncated,
            reason=reason,
            read_only=True,
        )

    def apply(self, plan: HistoricalAuditPlan | None = None) -> HistoricalAuditPlan:
        """Preserve legacy entries and report the native exact-adoption route.

        A legacy manifest and its ``approved`` field are discovery evidence;
        authenticated selection authority is required by ``apply_selected``.
        """

        del plan
        try:
            root_metadata = self._root_metadata()
        except HistoricalRootError as exc:
            return self._empty_root_plan(_bounded_reason(exc), read_only=False)
        root_identity = _identity(root_metadata)
        mount_snapshot = _mountinfo_snapshot()
        if mount_snapshot is None:
            return self._empty_root_plan(
                "mount topology could not be verified",
                read_only=False,
            )
        mountpoints, mount_digest = mount_snapshot
        records, unmanaged, truncated, reason = self._scan(root_identity, mountpoints)
        if reason and reason.startswith("historical root scan failed:"):
            return self._empty_root_plan(reason, read_only=False)
        if truncated:
            bound_reason = reason or "historical audit bounds were exceeded"
            records = tuple(
                replace(
                    record,
                    status="blocked" if record.adoptable else record.status,
                    adoptable=False,
                    proposed_bytes=0,
                    reason=bound_reason if record.adoptable else record.reason,
                )
                for record in records
            )
            reason = bound_reason
        final_mount_snapshot = _mountinfo_snapshot()
        if final_mount_snapshot is None or final_mount_snapshot[1] != mount_digest:
            records = tuple(
                replace(
                    record,
                    status="recovery_required",
                    adoptable=False,
                    proposed_bytes=0,
                    reason="mount topology changed during audit",
                )
                for record in records
            )
            reason = "mount topology changed during audit"
            truncated = True
        # Legacy manifests are discovery evidence only. The former approved
        # JSON field has no authenticated issuer and cannot supply effect
        # authority. Exact selections use prepare/approve/apply_selected.
        remaining = tuple(replace(record, adoptable=False, proposed_bytes=0,
            status=record.status if record.status in {"active", "failed", "recovery_required"} else "blocked",
            reason=record.reason or "private_adoption_required: use plan_selected, prepare_adoption, approve_adoption, apply_selected")
            for record in records)
        return self._summarize(remaining, unmanaged=unmanaged, root_identity=root_identity,
                               truncated=truncated, reason=reason, read_only=False)

    def _scan_prepared_receipts(
        self,
        root_identity: tuple[int, int, int],
        mountpoints: frozenset[Path],
        budget: _Budget,
    ) -> tuple[tuple[HistoricalRecord, ...], bool, str | None]:
        """Surface orphaned prepared receipts as recovery evidence.

        A crash can occur after the target bytes are removed but before the
        receipt is finalized.  The next plan/apply must not silently report an
        empty root or retry the effect; it exposes a bounded recovery record
        instead.  This helper never creates the receipt directory.
        """

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            root_fd = os.open(self.root, flags)
        except OSError:
            return (), False, None
        try:
            try:
                directory_fd = _receipt_directory_fd(
                    root_fd,
                    root_path=self.root,
                    root_device=root_identity[0],
                    mountpoints=mountpoints,
                    create=False,
                )
            except FileNotFoundError:
                return (), False, None
            except HistoricalAuditError as exc:
                # An unsafe/mounted receipt directory is itself a blocked
                # boundary.  Preserve that evidence instead of making an
                # empty scan look successful.
                return (
                    (),
                    False,
                    _bounded_reason(f"historical receipt directory is not verifiable: {exc}"),
                )
            try:
                names, truncated = _bounded_names_fd(
                    directory_fd,
                    max(1, min(self.max_entries, self.max_entries - budget.entries + 1)),
                )
                recovery: list[HistoricalRecord] = []
                issue: str | None = None
                for name in names:
                    if not _is_retirement_receipt_name(name):
                        continue
                    try:
                        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        issue = _bounded_reason(
                            f"historical receipt metadata is not verifiable: {exc}"
                        )
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    _, _, _, bounded = budget.consume(metadata)
                    if bounded:
                        truncated = True
                        issue = "historical receipt audit bounds were exceeded"
                        break
                    payload = _read_receipt_fd(directory_fd, name)
                    if payload is None:
                        issue = "historical retirement receipt is invalid or unverifiable"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    state = payload.get("state")
                    if state == "applied":
                        continue
                    if state != "prepared":
                        issue = "historical retirement receipt has an invalid state"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    if (
                        payload.get("schema") != HISTORICAL_RECEIPT_SCHEMA
                        or payload.get("root") != str(self.root)
                        or not _same_identity(payload.get("root_identity"), root_identity)
                    ):
                        issue = "historical prepared receipt claims are invalid"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    raw_path = payload.get("path")
                    if not isinstance(raw_path, str):
                        issue = "historical prepared receipt claims are invalid"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    candidate = Path(raw_path)
                    if (
                        not candidate.is_absolute()
                        or candidate.parent != self.root
                        or not candidate.name.startswith(self.prefix)
                    ):
                        issue = "historical prepared receipt claims are invalid"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    path_identity = _parse_identity(payload.get("path_identity"))
                    if path_identity is None:
                        issue = "historical prepared receipt claims are invalid"
                        recovery.append(
                            HistoricalRecord(
                                path=self.root / _RECEIPT_DIRECTORY / name,
                                name=name,
                                status="recovery_required",
                                reason=issue,
                                identity_uncertain=True,
                            )
                        )
                        continue
                    raw_owner = payload.get("owner")
                    raw_manifest_digest = payload.get("manifest_digest")
                    raw_adoption_id = payload.get("adoption_id")
                    raw_adoption_digest = payload.get("adoption_digest")
                    recovery.append(
                        HistoricalRecord(
                            path=candidate,
                            name=candidate.name,
                            status="recovery_required",
                            path_identity=path_identity,
                            root_identity=root_identity,
                            owner=raw_owner if isinstance(raw_owner, str) else None,
                            manifest_digest=(
                                raw_manifest_digest
                                if isinstance(raw_manifest_digest, str)
                                else None
                            ),
                            adoption_id=(
                                raw_adoption_id
                                if isinstance(raw_adoption_id, str)
                                else None
                            ),
                            adoption_digest=(
                                raw_adoption_digest
                                if isinstance(raw_adoption_digest, str)
                                else None
                            ),
                            reason="prepared historical retirement receipt requires reconciliation",
                            identity_uncertain=True,
                            activity_uncertain=True,
                        )
                    )
                return tuple(recovery), truncated, issue
            finally:
                os.close(directory_fd)
        finally:
            os.close(root_fd)

    def _scan(
        self,
        root_identity: tuple[int, int, int],
        mountpoints: frozenset[Path],
    ) -> tuple[tuple[HistoricalRecord, ...], tuple[Path, ...], bool, str | None]:
        budget = _Budget(self.max_entries, self.max_bytes)
        try:
            with os.scandir(self.root) as iterator:
                entries: list[os.DirEntry[str]] = []
                for entry in iterator:
                    if len(entries) >= self.max_entries + 1:
                        budget.truncated = True
                        break
                    entries.append(entry)
        except OSError as exc:
            return (), (), False, _bounded_reason(f"historical root scan failed: {exc}")
        entries.sort(key=lambda item: item.name)
        overflow = len(entries) > self.max_entries
        if overflow:
            entries = entries[: self.max_entries]
        unmanaged = tuple(
            self.root / entry.name for entry in entries if not entry.name.startswith(self.prefix)
        )
        managed = [entry for entry in entries if entry.name.startswith(self.prefix)]
        records: list[HistoricalRecord] = []
        for entry in managed:
            records.append(
                self._inspect_entry(
                    self.root / entry.name,
                    root_identity,
                    mountpoints,
                    budget,
                )
            )
        prepared_records, prepared_truncated, prepared_issue = self._scan_prepared_receipts(
            root_identity,
            mountpoints,
            budget,
        )
        if prepared_issue is not None and not prepared_records:
            prepared_records = (
                HistoricalRecord(
                    path=self.root / _RECEIPT_DIRECTORY,
                    name=_RECEIPT_DIRECTORY,
                    status="recovery_required",
                    reason=prepared_issue,
                    identity_uncertain=True,
                ),
            )
        if prepared_records:
            prepared_by_path = {record.path: record for record in prepared_records}
            for index, record in enumerate(records):
                recovery = prepared_by_path.pop(record.path, None)
                if recovery is not None:
                    records[index] = replace(
                        record,
                        status="recovery_required",
                        adoptable=False,
                        proposed_bytes=0,
                        reason=recovery.reason,
                        identity_uncertain=True,
                    )
            available = max(0, self.max_entries - len(records))
            if len(prepared_by_path) > available:
                prepared_truncated = True
            records.extend(tuple(prepared_by_path.values())[:available])
        truncated = (
            overflow
            or budget.truncated
            or prepared_truncated
            or any(item.truncated for item in records)
        )
        reason = None
        if overflow:
            reason = "historical root exceeds the entry limit"
        elif budget.truncated:
            reason = "historical audit bounds were exceeded"
        elif prepared_truncated:
            reason = "historical receipt directory exceeds the entry limit"
        elif prepared_issue is not None:
            reason = prepared_issue
        elif any(item.status == "recovery_required" for item in records):
            reason = "a prepared historical retirement receipt requires recovery"
        elif any(item.status == "blocked" for item in records):
            reason = "one or more historical entries are blocked"
        elif any(item.status == "unknown" for item in records):
            reason = "one or more historical entries lack sufficient evidence"
        records.sort(key=lambda item: item.name)
        return tuple(records), unmanaged, truncated, reason

    def _inspect_entry(
        self,
        path: Path,
        root_identity: tuple[int, int, int],
        mountpoints: frozenset[Path],
        budget: _Budget,
    ) -> HistoricalRecord:
        name = path.name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return HistoricalRecord(
                path=path,
                name=name,
                status="unknown",
                reason="historical entry disappeared during scan",
                identity_uncertain=True,
            )
        except OSError as exc:
            return HistoricalRecord(
                path=path,
                name=name,
                status="unknown",
                reason=_bounded_reason(f"historical entry metadata unavailable: {exc}"),
                identity_uncertain=True,
            )
        is_directory = stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
        apparent, allocated, observed, bounded = budget.consume(metadata)
        tree = _TreeResult(
            apparent_bytes=apparent,
            allocated_bytes=allocated,
            observed_bytes=observed,
            truncated=bounded,
        )
        if stat.S_ISLNK(metadata.st_mode):
            tree.issue("symlink_entry")
        elif _lexical_path(path) in mountpoints:
            tree.identity_uncertain = True
            tree.issue("mount_boundary")
        elif metadata.st_uid != os.geteuid():
            tree.owner_uncertain = True
            tree.issue("owner_uncertain")
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
            tree.issue("hardlink_entry")
        elif not stat.S_ISREG(metadata.st_mode) and not is_directory:
            tree.issue("unsupported_entry_type")
        elif is_directory:
            if not _safe_mode(metadata, private=True):
                tree.issue("entry_permissions_unsafe")
            if _lexical_path(path) in mountpoints or metadata.st_dev != root_identity[0]:
                tree.identity_uncertain = True
                tree.issue("mount_boundary")
            else:
                self._walk_tree(
                    path,
                    depth=0,
                    root_identity=root_identity,
                    mountpoints=mountpoints,
                    budget=budget,
                    tree=tree,
                )

        manifest_observation: _ManifestObservation | None = None
        manifest_error: str | None = None
        manifest_path: Path | None = None
        if stat.S_ISREG(metadata.st_mode) and _is_manifest_name(name):
            manifest_path = path
        elif is_directory and not budget.truncated:
            # Manifest discovery is limited to the candidate's own root.  It
            # does not recurse or interpret arbitrary payload names.
            try:
                candidate_manifests: list[Path] = []
                inspected_children = 0
                with os.scandir(path) as children:
                    for child in children:
                        inspected_children += 1
                        if inspected_children > self.max_entries:
                            manifest_error = "manifest directory exceeds the entry limit"
                            break
                        if _is_manifest_name(child.name):
                            candidate_manifests.append(path / child.name)
                candidate_manifests.sort(key=lambda item: item.name)
                if manifest_error is not None:
                    pass
                elif len(candidate_manifests) == 1:
                    manifest_path = candidate_manifests[0]
                elif len(candidate_manifests) > 1:
                    manifest_error = "multiple allow-listed manifests are ambiguous"
            except OSError as exc:
                manifest_error = _bounded_reason(f"manifest directory could not be inspected: {exc}")
        elif is_directory:
            # Once the shared scan budget is exhausted, do not perform a
            # second unbounded directory listing merely to look for a claim.
            # The tree is already non-adoptable and the bounded reason remains
            # visible in the record.
            manifest_error = "historical audit bounds were exceeded"
        if manifest_path is not None and manifest_error is None:
            try:
                raw, manifest_metadata = _safe_open_read(
                    manifest_path,
                    limit=_MAX_MANIFEST_BYTES,
                )
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise HistoricalAuditError("manifest JSON is not an object")
                digest = _verify_optional_digest(payload)
                schema = payload.get("schema")
                manifest_observation = _ManifestObservation(
                    path=manifest_path,
                    identity=_identity(manifest_metadata),
                    payload=dict(payload),
                    digest=digest,
                    schema=schema if isinstance(schema, str) else None,
                )
            except (HistoricalAuditError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                manifest_error = _bounded_reason(f"manifest is invalid: {exc}")
        elif manifest_error is None:
            manifest_error = "historical entry has no allow-listed manifest"

        record = self._classify(
            path=path,
            name=name,
            metadata=metadata,
            root_identity=root_identity,
            tree=tree,
            manifest=manifest_observation,
            manifest_error=manifest_error,
        )
        return record

    def _walk_tree(
        self,
        path: Path,
        *,
        depth: int,
        root_identity: tuple[int, int, int],
        mountpoints: frozenset[Path],
        budget: _Budget,
        tree: _TreeResult,
    ) -> None:
        if budget.truncated:
            tree.truncated = True
            tree.issue("audit_bounds_exceeded")
            return
        if depth >= self.max_depth:
            try:
                with os.scandir(path) as depth_iterator:
                    if next(depth_iterator, None) is not None:
                        tree.truncated = True
                        tree.issue("depth_limit_exceeded")
            except OSError as exc:
                tree.issue(_bounded_reason(f"directory could not be inspected: {exc}"))
            return
        try:
            with os.scandir(path) as iterator:
                children: list[os.DirEntry[str]] = []
                for child in iterator:
                    if len(children) >= self.max_entries:
                        tree.truncated = True
                        tree.issue("entry_limit_exceeded")
                        break
                    children.append(child)
        except OSError as exc:
            tree.issue(_bounded_reason(f"directory could not be inspected: {exc}"))
            tree.identity_uncertain = True
            return
        children.sort(key=lambda item: item.name)
        for child in children:
            child_path = path / child.name
            try:
                metadata = child_path.lstat()
            except OSError as exc:
                tree.identity_uncertain = True
                tree.issue(_bounded_reason(f"child identity unavailable: {exc}"))
                continue
            apparent, allocated, observed, bounded = budget.consume(metadata)
            tree.apparent_bytes += apparent
            tree.allocated_bytes += allocated
            tree.observed_bytes += observed
            tree.truncated = tree.truncated or bounded
            if bounded:
                tree.issue("byte_limit_exceeded")
                return
            if _lexical_path(child_path) in mountpoints:
                tree.identity_uncertain = True
                tree.issue("mount_boundary")
                continue
            if _is_manifest_name(child.name) and depth == 0:
                # The root-level manifest will be read separately.  Recording
                # only its name here keeps the traversal bounded and avoids
                # opening arbitrary files.
                pass
            if stat.S_ISLNK(metadata.st_mode):
                tree.issue("symlink_payload")
                continue
            if metadata.st_uid != os.geteuid():
                tree.owner_uncertain = True
                tree.issue("owner_uncertain")
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink > 1:
                    tree.issue("hardlink_payload")
                if metadata.st_mode & 0o022:
                    tree.issue("payload_permissions_unsafe")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if (
                    _lexical_path(child_path) in mountpoints
                    or metadata.st_dev != root_identity[0]
                ):
                    tree.identity_uncertain = True
                    tree.issue("mount_boundary")
                    continue
                if metadata.st_mode & 0o022:
                    tree.issue("payload_permissions_unsafe")
                self._walk_tree(
                    child_path,
                    depth=depth + 1,
                    root_identity=root_identity,
                    mountpoints=mountpoints,
                    budget=budget,
                    tree=tree,
                )
                continue
            tree.issue("unsupported_payload_type")

    def _classify(
        self,
        *,
        path: Path,
        name: str,
        metadata: os.stat_result,
        root_identity: tuple[int, int, int],
        tree: _TreeResult,
        manifest: _ManifestObservation | None,
        manifest_error: str | None,
    ) -> HistoricalRecord:
        path_identity = _identity(metadata)
        owner_uncertain = tree.owner_uncertain
        identity_uncertain = tree.identity_uncertain
        owner: str | None = None
        adoption_id: str | None = None
        adoption_digest: str | None = None
        manifest_schema = manifest.schema if manifest is not None else None
        manifest_digest = manifest.digest if manifest is not None else None
        activity_uncertain = True
        reason = "; ".join(tree.issues) if tree.issues else None
        status = "unknown"
        adoptable = False
        valid_manifest = False
        proposed = 0
        manifest_name = manifest.path.name if manifest is not None else None
        manifest_identity = manifest.identity if manifest is not None else None
        if tree.issues:
            status = "blocked"
            reason = reason or "historical entry failed a safety check"
        elif manifest is None:
            status = "unknown"
            reason = manifest_error or "historical entry has no manifest"
        else:
            payload = manifest.payload
            raw_owner = payload.get("owner")
            owner = raw_owner if isinstance(raw_owner, str) else None
            if not _app_manifest(payload):
                status = "blocked"
                reason = "manifest belongs to another application"
            elif owner != _HISTORICAL_OWNER:
                # The owner field is not trusted merely because it is present
                # in JSON.  This service has one registered producer contract;
                # an unknown or missing logical owner is preserved for review.
                status = "blocked"
                reason = "manifest owner is unsupported"
            else:
                valid_manifest = True
                adoption = payload.get("historical_adoption")
                lifecycle_state = payload.get("state")
                if lifecycle_state in {
                    "active",
                    "committing",
                    "running",
                    "in-progress",
                } or payload.get("active") is True:
                    activity_uncertain = _activity_uncertain(payload)
                    status = "active"
                    reason = "historical entry is active or in progress"
                elif lifecycle_state in {
                    "failed",
                    "failed-retained",
                    "recovery_required",
                    "recovering",
                }:
                    activity_uncertain = _activity_uncertain(payload)
                    status = "recovery_required"
                    reason = "historical entry requires recovery"
                else:
                    adoption_approved = isinstance(adoption, Mapping) and type(
                        adoption.get("approved")
                    ) is bool and adoption.get("approved") is True
                    if isinstance(adoption, Mapping):
                        raw_adoption_id = adoption.get("adoption_id")
                        if isinstance(raw_adoption_id, str) and raw_adoption_id.strip():
                            if len(raw_adoption_id.encode("utf-8")) <= _MAX_ADOPTION_ID_BYTES:
                                adoption_id = raw_adoption_id
                        raw_adoption_digest = adoption.get(
                            "digest", adoption.get("manifest_digest")
                        )
                        if isinstance(raw_adoption_digest, str):
                            adoption_digest = raw_adoption_digest
                    state_completed = payload.get("state") == "completed"
                    disposable = type(payload.get("disposable")) is bool and payload.get(
                        "disposable"
                    ) is True
                    path_claim = payload.get("path")
                    root_claim = _claim_string(
                        payload, ("root", "root_path", "root_directory")
                    )
                    path_identity_claim = payload.get(
                        "path_identity", payload.get("identity")
                    )
                    root_identity_claim = payload.get("root_identity")
                    claims_exact = (
                        isinstance(path_claim, str)
                        and path_claim == str(path)
                        and root_claim == str(self.root)
                        and _same_identity(path_identity_claim, path_identity)
                        and _same_identity(root_identity_claim, root_identity)
                    )
                    adoption_binding_exact = (
                        adoption_id is not None
                        and manifest_digest is not None
                        and isinstance(adoption, Mapping)
                        and adoption_digest in {
                            manifest_digest,
                            _adoption_claim_digest(adoption),
                        }
                    )
                    activity_uncertain = _activity_uncertain(payload)
                    if not claims_exact:
                        identity_uncertain = True
                        status = "unknown"
                        reason = "historical adoption path/root/identity claim is absent or mismatched"
                    elif activity_uncertain:
                        status = "unknown"
                        reason = "historical activity is uncertain"
                    elif not adoption_binding_exact:
                        status = "unknown"
                        reason = "historical adoption id/digest binding is absent or mismatched"
                    elif not adoption_approved or not state_completed or not disposable:
                        status = "kept"
                        reason = "historical entry is not approved for adoption"
                    else:
                        status = "kept"
                        adoptable = False
                        proposed = 0
                        reason = "private_adoption_required: use plan_selected, prepare_adoption, approve_adoption, apply_selected"
        return HistoricalRecord(
            path=path,
            name=name,
            status=status,
            apparent_bytes=tree.apparent_bytes,
            allocated_bytes=tree.allocated_bytes,
            observed_bytes=tree.observed_bytes,
            proposed_bytes=proposed,
            path_identity=path_identity,
            root_identity=root_identity,
            manifest_path=manifest.path if manifest is not None else None,
            manifest_name=manifest_name,
            manifest_identity=manifest_identity,
            manifest_schema=manifest_schema,
            manifest_digest=manifest_digest,
            adoption_id=adoption_id,
            adoption_digest=adoption_digest,
            owner=owner,
            reason=reason or manifest_error,
            truncated=tree.truncated,
            adoptable=adoptable,
            valid_manifest=valid_manifest,
            activity_uncertain=activity_uncertain,
            owner_uncertain=owner_uncertain,
            identity_uncertain=identity_uncertain,
            is_directory=stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode),
        )

    def _summarize(
        self,
        records: Sequence[HistoricalRecord],
        *,
        unmanaged: tuple[Path, ...],
        root_identity: tuple[int, int, int] | None,
        truncated: bool,
        reason: str | None,
        read_only: bool,
    ) -> HistoricalAuditPlan:
        records_tuple = tuple(records)
        counts = {status: sum(item.status == status for item in records_tuple) for status in (
            "adoptable",
            "kept",
            "blocked",
            "unknown",
            "active",
            "failed",
            "recovery_required",
        )}
        adoptable = counts["adoptable"]
        blocked = counts["blocked"]
        unknown = counts["unknown"]
        if counts["recovery_required"]:
            status = "recovery_required"
        elif counts["failed"]:
            status = "failed"
        elif blocked:
            status = "blocked"
        elif counts["active"]:
            status = "active"
        elif adoptable:
            status = "planned"
        elif counts["kept"]:
            status = "kept"
        elif unknown:
            status = "unknown"
        else:
            status = "planned"
        return HistoricalAuditPlan(
            root=self.root,
            records=records_tuple,
            unmanaged=unmanaged,
            scanned=len(records_tuple),
            adoptable=adoptable,
            planned=adoptable,
            applied=0,
            kept=counts["kept"],
            blocked=blocked,
            unknown=unknown,
            active=counts["active"],
            failed=counts["failed"],
            recovery_required=counts["recovery_required"],
            observed_bytes=sum(item.observed_bytes for item in records_tuple),
            proposed_bytes=sum(item.proposed_bytes for item in records_tuple),
            applied_bytes=0,
            active_bytes=sum(
                item.observed_bytes for item in records_tuple if item.status == "active"
            ),
            observed_apparent_bytes=sum(item.apparent_bytes for item in records_tuple),
            observed_allocated_bytes=sum(item.allocated_bytes for item in records_tuple),
            proposed_apparent_bytes=sum(item.apparent_bytes for item in records_tuple if item.adoptable),
            proposed_allocated_bytes=sum(item.allocated_bytes for item in records_tuple if item.adoptable),
            status=status,
            reason=reason,
            read_only=read_only,
            root_identity=root_identity,
            truncated=truncated,
            max_entries=self.max_entries,
            max_depth=self.max_depth,
            max_bytes=self.max_bytes,
        )

    def _retire(
        self,
        record: HistoricalRecord,
        root_identity: tuple[int, int, int],
        mountpoints: frozenset[Path],
        mount_digest: str,
    ) -> Path:
        """Revalidate and remove one candidate using descriptor-relative paths."""

        current_root = self._root_metadata()
        if _identity(current_root) != root_identity:
            raise _IdentityDrift("historical root identity changed before retirement")
        current_mount = _mountinfo_snapshot()
        if current_mount is None or current_mount[1] != mount_digest:
            raise _IdentityDrift("mount topology changed before retirement")
        if _lexical_path(record.path) in mountpoints:
            raise HistoricalAuditError("historical candidate is a mountpoint")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        root_fd = os.open(self.root, flags)
        try:
            opened_root = os.fstat(root_fd)
            if _identity(opened_root) != root_identity:
                raise _IdentityDrift("historical root identity changed during retirement")
            self._validate_root_fd(opened_root, root_identity)
            current = os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(current.st_mode) or _identity(current) != record.path_identity:
                raise _IdentityDrift("historical entry identity changed before retirement")
            self._validate_entry_metadata(current)
            if stat.S_ISREG(current.st_mode):
                self._revalidate_manifest_path(record, root_identity)
                self._assert_mount_stable(mount_digest)
                _write_retirement_receipt(root_fd, record, root_identity, mountpoints)
                self._assert_mount_stable(mount_digest)
                self._validate_root_fd(os.fstat(root_fd), root_identity)
                current = os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
                if _identity(current) != record.path_identity:
                    raise _IdentityDrift("historical entry identity changed after receipt")
                self._validate_entry_metadata(current)
                self._revalidate_manifest_path(record, root_identity)
                # Re-check the directory entry immediately before unlinking;
                # the receipt is immutable, but a same-name replacement must
                # never be treated as the authorized inode.
                current = os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
                if _identity(current) != record.path_identity:
                    raise _IdentityDrift("historical entry identity changed before unlink")
                self._validate_entry_metadata(current)
                os.unlink(record.name, dir_fd=root_fd)
                final_mount = _mountinfo_snapshot()
                if final_mount is None or final_mount[1] != mount_digest:
                    raise _EffectPartial("mount topology changed after historical retirement")
                return _finalize_retirement_receipt(
                    root_fd,
                    record,
                    root_identity,
                    mountpoints,
                )
            if not stat.S_ISDIR(current.st_mode):
                raise HistoricalAuditError("historical candidate is no longer a directory")
            child_fd = os.open(record.name, flags, dir_fd=root_fd)
            try:
                opened_child = os.fstat(child_fd)
                if _identity(opened_child) != record.path_identity:
                    raise _IdentityDrift("historical entry identity changed during retirement")
                self._revalidate_manifest_fd(child_fd, record, root_identity)
                self._validate_tree_fd(
                    child_fd,
                    root_identity,
                    relative_path=record.path,
                    mountpoints=mountpoints,
                    max_entries=self.max_entries,
                    max_depth=self.max_depth,
                    max_bytes=self.max_bytes,
                )
                self._revalidate_manifest_fd(child_fd, record, root_identity)
                self._assert_mount_stable(mount_digest)
                _write_retirement_receipt(root_fd, record, root_identity, mountpoints)
                self._assert_mount_stable(mount_digest)
                self._validate_root_fd(os.fstat(root_fd), root_identity)
                current = os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
                if _identity(current) != record.path_identity:
                    raise _IdentityDrift("historical entry identity changed after receipt")
                self._validate_entry_metadata(current)
                self._revalidate_manifest_fd(child_fd, record, root_identity)
                removed = [0]
                try:
                    _remove_tree_fd(
                        child_fd,
                        relative_path=record.path,
                        mountpoints=mountpoints,
                        mount_digest=mount_digest,
                        root_device=root_identity[0],
                        max_entries=self.max_entries,
                        max_depth=self.max_depth,
                        max_bytes=self.max_bytes,
                        removed=removed,
                    )
                except _EffectPartial:
                    raise
                except OSError as exc:
                    if removed[0]:
                        raise _EffectPartial(
                            "historical retirement stopped after a partial effect"
                        ) from exc
                    raise
            finally:
                os.close(child_fd)
            current = os.stat(record.name, dir_fd=root_fd, follow_symlinks=False)
            if _identity(current) != record.path_identity:
                raise _EffectPartial("historical entry identity changed before directory removal")
            try:
                os.rmdir(record.name, dir_fd=root_fd)
            except OSError as exc:
                if removed[0]:
                    raise _EffectPartial(
                        "historical retirement stopped before removing the candidate directory"
                    ) from exc
                raise
            final_mount = _mountinfo_snapshot()
            if final_mount is None or final_mount[1] != mount_digest:
                raise _EffectPartial("mount topology changed after historical retirement")
            return _finalize_retirement_receipt(
                root_fd,
                record,
                root_identity,
                mountpoints,
            )
        finally:
            os.close(root_fd)

    @staticmethod
    def _validate_root_fd(
        metadata: os.stat_result,
        root_identity: tuple[int, int, int],
    ) -> None:
        if (
            _identity(metadata) != root_identity
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or not _safe_mode(metadata, private=False)
        ):
            raise _IdentityDrift("historical root protection changed during retirement")

    @staticmethod
    def _assert_mount_stable(expected_digest: str) -> None:
        snapshot = _mountinfo_snapshot()
        if snapshot is None or snapshot[1] != expected_digest:
            raise _IdentityDrift("mount topology changed during retirement")

    @staticmethod
    def _validate_entry_metadata(metadata: os.stat_result) -> None:
        """Revalidate the candidate itself immediately before an effect."""

        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise HistoricalAuditError("historical candidate type changed before retirement")
        if metadata.st_uid != os.geteuid() or not _safe_mode(metadata, private=True):
            raise HistoricalAuditError("historical candidate permissions changed before retirement")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise HistoricalAuditError("historical candidate hardlink appeared before retirement")

    def _revalidate_manifest_path(
        self,
        record: HistoricalRecord,
        root_identity: tuple[int, int, int],
    ) -> None:
        if record.manifest_path is None:
            raise HistoricalAuditError("adoptable record has no manifest")
        observation = _read_manifest_observation(record.manifest_path)
        if observation is None or not self._claims_adoptable(
            observation,
            record.path,
            record.path_identity,
            root_identity,
            expected_adoption_id=record.adoption_id,
            expected_adoption_digest=record.adoption_digest,
        ):
            raise _IdentityDrift("historical manifest claim changed before retirement")

    def _revalidate_manifest_fd(
        self,
        directory_fd: int,
        record: HistoricalRecord,
        root_identity: tuple[int, int, int],
    ) -> None:
        if record.manifest_name is None:
            raise HistoricalAuditError("adoptable record has no manifest")
        observation = _read_manifest_observation_fd(directory_fd, record.manifest_name)
        candidate_path = record.path
        if observation is None or not self._claims_adoptable(
            observation,
            candidate_path,
            record.path_identity,
            root_identity,
            expected_adoption_id=record.adoption_id,
            expected_adoption_digest=record.adoption_digest,
        ):
            raise _IdentityDrift("historical manifest claim changed before retirement")
        if record.manifest_identity is not None and observation.identity != record.manifest_identity:
            raise _IdentityDrift("historical manifest identity changed before retirement")

    def _claims_adoptable(
        self,
        observation: _ManifestObservation,
        path: Path,
        path_identity: tuple[int, int, int] | None,
        root_identity: tuple[int, int, int],
        expected_adoption_id: str | None = None,
        expected_adoption_digest: str | None = None,
    ) -> bool:
        payload = observation.payload
        adoption = payload.get("historical_adoption")
        if not (
            # Release manifests are classified for preservation only.  The
            # historical effect boundary accepts exactly the scratch schema
            # owned by this service and repeats the logical-owner gate that
            # was checked during the initial scan.
            isinstance(payload.get("schema"), str)
            and payload.get("schema") in _SUPPORTED_MANIFEST_SCHEMAS
            and payload.get("owner") == _HISTORICAL_OWNER
            and isinstance(adoption, Mapping)
            and type(adoption.get("approved")) is bool
            and adoption.get("approved") is True
            and payload.get("state") == "completed"
            and type(payload.get("disposable")) is bool
            and payload.get("disposable") is True
            and payload.get("path") == str(path)
            and _claim_string(payload, ("root", "root_path", "root_directory")) == str(self.root)
            and _same_identity(
                payload.get("path_identity", payload.get("identity")),
                path_identity,
            )
            and _same_identity(payload.get("root_identity"), root_identity)
            and isinstance(adoption.get("adoption_id"), str)
            and bool(adoption.get("adoption_id", "").strip())
            and len(adoption.get("adoption_id", "").encode("utf-8"))
            <= _MAX_ADOPTION_ID_BYTES
            and observation.digest is not None
            and adoption.get("digest", adoption.get("manifest_digest")) in {
                observation.digest,
                _adoption_claim_digest(adoption),
            }
            and (
                expected_adoption_id is None
                or adoption.get("adoption_id") == expected_adoption_id
            )
            and (
                expected_adoption_digest is None
                or adoption.get("digest", adoption.get("manifest_digest"))
                == expected_adoption_digest
            )
            and not _activity_uncertain(payload)
        ):
            return False
        return True

    @staticmethod
    def _validate_tree_fd(
        directory_fd: int,
        root_identity: tuple[int, int, int],
        *,
        relative_path: Path,
        mountpoints: frozenset[Path],
        max_entries: int,
        max_depth: int,
        max_bytes: int,
        depth: int = 0,
        budget: _Budget | None = None,
    ) -> None:
        """Bound and validate a descriptor tree before any retirement effect."""

        if budget is None:
            budget = _Budget(max_entries, max_bytes)
        names, names_truncated = _bounded_names_fd(directory_fd, max_entries)
        if names_truncated:
            raise HistoricalAuditError("historical audit entry limit exceeded")
        if depth >= max_depth:
            if names:
                raise HistoricalAuditError("historical audit depth limit exceeded")
            return
        root_device = root_identity[0]
        for name in names:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _, _, _, bounded = budget.consume(metadata)
            if bounded:
                raise HistoricalAuditError("historical audit byte limit exceeded")
            child_path = relative_path / name
            if _lexical_path(child_path) in mountpoints:
                raise HistoricalAuditError("mount boundary appeared before retirement")
            if stat.S_ISLNK(metadata.st_mode):
                raise HistoricalAuditError("symlink payload appeared before retirement")
            if metadata.st_uid != os.geteuid():
                raise HistoricalAuditError("payload owner changed before retirement")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise HistoricalAuditError("hardlink payload appeared before retirement")
            if stat.S_ISDIR(metadata.st_mode):
                if metadata.st_dev != root_device:
                    raise HistoricalAuditError("mount boundary appeared before retirement")
                if not _safe_mode(metadata, private=True):
                    raise HistoricalAuditError("payload permissions changed before retirement")
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if _identity(opened) != _identity(metadata):
                        raise _IdentityDrift("historical child identity changed during validation")
                    HistoricalAuditManager._validate_tree_fd(
                        child_fd,
                        root_identity,
                        relative_path=child_path,
                        mountpoints=mountpoints,
                        max_entries=max_entries,
                        max_depth=max_depth,
                        max_bytes=max_bytes,
                        depth=depth + 1,
                        budget=budget,
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if not _safe_mode(metadata, private=True):
                    raise HistoricalAuditError("payload permissions changed before retirement")
            else:
                # FIFO/socket/device entries are never safe to retire as part
                # of a historical tree.  The scan rejects them too; this
                # second check closes the race between scan and effect.
                raise HistoricalAuditError("unsupported payload type appeared before retirement")


def _read_manifest_observation(path: Path) -> _ManifestObservation | None:
    try:
        raw, metadata = _safe_open_read(path, limit=_MAX_MANIFEST_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, Mapping):
            return None
        digest = _verify_optional_digest(payload)
    except (HistoricalAuditError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    schema = payload.get("schema")
    return _ManifestObservation(
        path=path,
        identity=_identity(metadata),
        payload=dict(payload),
        digest=digest,
        schema=schema if isinstance(schema, str) else None,
    )


def _read_manifest_observation_fd(directory_fd: int, name: str) -> _ManifestObservation | None:
    if not _is_manifest_name(name):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not _safe_mode(before, private=True)
            or before.st_size > _MAX_MANIFEST_BYTES
        ):
            return None
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or _identity(metadata) != _identity(before)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or not _safe_mode(metadata, private=True)
            or metadata.st_size > _MAX_MANIFEST_BYTES
        ):
            return None
        data = bytearray()
        while len(data) <= _MAX_MANIFEST_BYTES:
            chunk = os.read(
                fd,
                min(64 * 1024, _MAX_MANIFEST_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > _MAX_MANIFEST_BYTES:
            return None
        payload = json.loads(bytes(data).decode("utf-8"))
        if not isinstance(payload, Mapping):
            return None
        digest = _verify_optional_digest(payload)
    except (HistoricalAuditError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    finally:
        os.close(fd)
    schema = payload.get("schema")
    return _ManifestObservation(
        path=Path(name),
        identity=_identity(metadata),
        payload=dict(payload),
        digest=digest,
        schema=schema if isinstance(schema, str) else None,
    )


def _remove_tree_fd(
    directory_fd: int,
    *,
    relative_path: Path,
    mountpoints: frozenset[Path],
    mount_digest: str,
    root_device: int,
    max_entries: int,
    max_depth: int,
    max_bytes: int,
    removed: list[int],
    depth: int = 0,
    budget: _Budget | None = None,
) -> None:
    """Remove a validated tree with the same bounds used by its preflight."""

    if budget is None:
        budget = _Budget(max_entries, max_bytes)
    current_mount = _mountinfo_snapshot()
    if current_mount is None or current_mount[1] != mount_digest:
        if removed[0]:
            raise _EffectPartial("mount topology changed during retirement")
        raise HistoricalAuditError("mount topology changed during retirement")
    names, names_truncated = _bounded_names_fd(directory_fd, max_entries)
    if names_truncated:
        if removed[0]:
            raise _EffectPartial("historical tree exceeded its entry limit during retirement")
        raise HistoricalAuditError("historical audit entry limit exceeded")
    if depth >= max_depth:
        if names:
            if removed[0]:
                raise _EffectPartial("historical tree exceeded its depth during retirement")
            raise HistoricalAuditError("historical audit depth limit exceeded")
        return
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _, _, _, bounded = budget.consume(metadata)
        if bounded:
            if removed[0]:
                raise _EffectPartial("historical tree exceeded its byte limit during retirement")
            raise HistoricalAuditError("historical audit byte limit exceeded")
        child_path = relative_path / name
        if _lexical_path(child_path) in mountpoints:
            if removed[0]:
                raise _EffectPartial("mount boundary appeared during retirement")
            raise HistoricalAuditError("mount boundary appeared during retirement")
        if stat.S_ISLNK(metadata.st_mode):
            if removed[0]:
                raise _EffectPartial("symlink payload appeared during retirement")
            raise HistoricalAuditError("symlink payload appeared during retirement")
        if metadata.st_uid != os.geteuid() or not _safe_mode(metadata, private=True):
            if removed[0]:
                raise _EffectPartial("payload protection changed during retirement")
            raise HistoricalAuditError("payload protection changed during retirement")
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink > 1:
                if removed[0]:
                    raise _EffectPartial("hardlink payload appeared during retirement")
                raise HistoricalAuditError("hardlink payload appeared during retirement")
            os.unlink(name, dir_fd=directory_fd)
            removed[0] += 1
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            if removed[0]:
                raise _EffectPartial("unsupported payload type appeared during retirement")
            raise HistoricalAuditError("unsupported payload type appeared during retirement")
        if metadata.st_dev != root_device:
            if removed[0]:
                raise _EffectPartial("mount boundary appeared during retirement")
            raise HistoricalAuditError("mount boundary appeared during retirement")
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(child_fd)
            if _identity(opened) != _identity(metadata):
                raise _IdentityDrift("historical child identity changed during retirement")
            _remove_tree_fd(
                child_fd,
                relative_path=child_path,
                mountpoints=mountpoints,
                mount_digest=mount_digest,
                root_device=root_device,
                max_entries=max_entries,
                max_depth=max_depth,
                max_bytes=max_bytes,
                removed=removed,
                depth=depth + 1,
                budget=budget,
            )
        except _EffectPartial:
            raise
        except OSError as exc:
            if removed[0]:
                raise _EffectPartial("historical retirement stopped after a partial effect") from exc
            raise
        finally:
            os.close(child_fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) != _identity(metadata):
            raise _EffectPartial("historical child identity changed before directory removal")
        os.rmdir(name, dir_fd=directory_fd)
        removed[0] += 1


def _validate_now(now_ns: int | None) -> int:
    if now_ns is None:
        return time_ns()
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("historical now_ns must be a non-negative integer")
    return now_ns


def time_ns() -> int:
    """Small seam for tests and future lifecycle time attestations."""

    return __import__("time").time_ns()


__all__ = [
    "HISTORICAL_AUDIT_SCHEMA",
    "HISTORICAL_RECEIPT_SCHEMA",
    "HistoricalAuditError",
    "HistoricalAuditManager",
    "HistoricalAuditPlan",
    "HistoricalRecord",
    "HistoricalRootError",
]
