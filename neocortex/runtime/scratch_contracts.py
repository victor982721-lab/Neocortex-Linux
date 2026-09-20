"""Shared contracts and bounded filesystem primitives for registered scratch.

The manager owns workspace orchestration and lifecycle effects.  This module
keeps schemas, value objects, validation, sealed observations, and atomic
manifest primitives independent so lifecycle code can compose them without
creating a second owner.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

from .scratch_tree import (
    ScratchTreeError, read_private_manifest, remove_claimed_tree, PayloadProfile,
    policy_revision, observe_claimed_tree, opened_claimed_tree,
    _checked_child,
)
from .path_identity import PathIdentity
from .control.process_scope import verified_process_quiescence

SCRATCH_SCHEMA = "neocortex.scratch/v1"
MANIFEST_NAME = "manifest.json"
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
_MAX_REASON_BYTES = 8 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_RECORDS = 100_000
_MAX_SCAN_DEPTH = 64
_MAX_SCAN_BYTES = 1 << 50
_ARTIFACT_REGISTRY_MODULES = (
    "neocortex.runtime.artifact_registry",
    "neocortex.runtime.artifacts",
    "neocortex.persistence.artifact_registry",
    "neocortex.persistence.artifacts",
    "neocortex.artifact_registry",
)
_ARTIFACT_PRODUCER = "scratch"
_ARTIFACT_KIND = "temporary"

# Explicit federation limits may narrow the hard owner defaults. ``None``
# keeps those defaults and never starts an unbounded member/byte observation.
ENTRY_LIMIT = "entry_limit"
DEPTH_LIMIT = "depth_limit"
BYTE_LIMIT = "byte_limit"
_SEAL_SCHEMA = "neocortex.scratch-seal/v1"
_CONTROL_DIRECTORY = ".scratch-control"
_FIXTURE_GRANT_SCHEMA = "neocortex.scratch-fixture-grant/v1"


def _validate_scan_limit(
    value: int | None,
    *,
    label: str,
    maximum: int,
) -> int | None:
    """Validate one optional bounded-observation limit."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"scratch {label} must be a non-negative integer or None")
    if value > maximum:
        raise ValueError(f"scratch {label} must be at most {maximum}")
    return value


def _validate_scan_limits(
    *,
    max_entries: int | None,
    max_depth: int | None,
    max_bytes: int | None,
) -> tuple[int | None, int | None, int | None]:
    return (
        _validate_scan_limit(max_entries, label="max_entries", maximum=_MAX_RECORDS),
        _validate_scan_limit(max_depth, label="max_depth", maximum=_MAX_SCAN_DEPTH),
        _validate_scan_limit(max_bytes, label="max_bytes", maximum=_MAX_SCAN_BYTES),
    )


class ScratchError(RuntimeError):
    """Base class for a scratch contract violation."""


class ScratchSecurityError(ScratchError):
    """The configured scratch root or a workspace failed a safety claim."""


class ScratchRootError(ScratchSecurityError):
    """The requested scratch root is absent or cannot be made private."""


class ScratchManifestError(ScratchSecurityError):
    """A workspace manifest is malformed or fails its authenticated digest."""


class ScratchState(str, Enum):
    """Durable workspace lifecycle states."""

    ACTIVE = "active"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED_RETAINED = "failed-retained"
    RECOVERY_REQUIRED = "recovery_required"
    RETIRED = "retired"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="backslashreplace").decode("utf-8")


def _bounded_text(value: object, *, label: str, limit: int = _MAX_REASON_BYTES) -> str:
    text = str(value)
    if not text.strip():
        raise ValueError(f"{label} must not be blank")
    if len(text.encode("utf-8", errors="backslashreplace")) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    return text


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    birthtime = getattr(metadata, "st_birthtime_ns", None)
    # Linux does not expose a creation time consistently.  ``-1`` is an
    # explicit unavailable sentinel, never a ctime masquerading as birthtime.
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(birthtime) if birthtime is not None else -1,
    )


def _same_identity(path: Path, expected: Sequence[int]) -> bool:
    if (not isinstance(expected, (list, tuple)) or len(expected) != 3
            or any(type(value) is not int for value in expected)):
        return False
    try:
        current = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(current.st_mode):
        return False
    return _identity(current) == tuple(int(value) for value in expected)


def _validate_absolute_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_control_manifest(directory: Path, root: Path, name: str) -> bool:
    # Only the exact root manifest is control metadata. Publication leftovers
    # and same-named nested files remain payload until their owner accounts them.
    return directory == root and name == MANIFEST_NAME


def _read_manifest_bytes(path: Path, *, limit: int = _MAX_MANIFEST_BYTES) -> bytes:
    try:
        return read_private_manifest(path, limit=limit)
    except ScratchTreeError as exc:
        raise ScratchManifestError(str(exc)) from exc


def _directory_size(path: Path, *, profile: PayloadProfile | str = PayloadProfile.STRICT) -> int:
    """Bounded descriptor accounting; incomplete coverage is never zero success."""
    observed = observe_claimed_tree(path, profile=profile)
    if not observed.complete:
        raise ScratchSecurityError(f"scratch size observation is incomplete: {observed.issue}")
    return observed.apparent_bytes


def _seal_mapping(value: object) -> dict[str, Any] | None:
    """Validate the stable top-level workspace seal, if one is present."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ScratchSecurityError("scratch seal is not an object")
    digest = value.get("digest")
    members = value.get("members")
    apparent = value.get("apparent_bytes")
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or len(digest) != len("sha256:") + 64
        or any(character not in "0123456789abcdef" for character in digest[7:])
        or type(members) is not int
        or members < 0
        or type(apparent) is not int
        or apparent < 0
    ):
        raise ScratchSecurityError("scratch seal claims are invalid")
    schema = value.get("schema", _SEAL_SCHEMA)
    if schema != _SEAL_SCHEMA:
        raise ScratchSecurityError("unsupported scratch seal schema")
    return {
        "schema": _SEAL_SCHEMA,
        "digest": digest,
        "members": members,
        "apparent_bytes": apparent,
    }


def _seal_from_lifecycle_reason(reason: object) -> dict[str, Any] | None:
    """Compatibility bridge for the public activity note format.

    New callers should use ``ScratchManager.seal_workspace``.  The bridge
    promotes the existing AgentActivity close note into the authenticated
    top-level manifest once, so future planning never depends on parsing that
    note as its authority.
    """

    if not isinstance(reason, str) or not reason.startswith("agent-activity-note:"):
        return None
    try:
        value = json.loads(reason[len("agent-activity-note:") :])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    try:
        return _seal_mapping(value.get("seal"))
    except ScratchSecurityError:
        return None


def _activity_process_scope_issue(metadata: Mapping[str, Any], reason: object) -> str | None:
    """Keep completed legacy activities behind the producer's quiescence gate."""
    if "agent_activity" not in metadata:
        return None
    activity = metadata["agent_activity"]
    if not isinstance(activity, Mapping):
        return "process_scope_incomplete"
    activity_id = activity.get("activity_id")
    if not isinstance(activity_id, str) or not activity_id:
        return "process_scope_incomplete"
    note: Mapping[str, Any] = {}
    if reason is not None:
        if not isinstance(reason, str) or not reason.startswith("agent-activity-note:"):
            return "process_scope_incomplete"
        try:
            parsed = json.loads(reason[len("agent-activity-note:"):])
        except (ValueError, TypeError):
            return "process_scope_incomplete"
        if (not isinstance(parsed, Mapping)
                or parsed.get("schema") != "neocortex.agent-activity-note/v1"):
            return "process_scope_incomplete"
        note = parsed
    if not verified_process_quiescence(note, activity_id=activity_id,
                                       process_pid=activity.get("process_pid")):
        return "process_scope_incomplete"
    return None


def _sealed_workspace_digest(
    path: Path, *, max_file_bytes: int = _MAX_SCAN_BYTES,
    profile: PayloadProfile | str = PayloadProfile.STRICT,
    include_control_manifest: bool = False,
    max_entries: int = _MAX_RECORDS, max_bytes: int = _MAX_SCAN_BYTES,
    max_depth: int = 2048, max_fds: int = 2048,
) -> tuple[str, int, int]:
    """Canonical byte-preserving seal over descriptor-validated members.

    Valid ordinary v1 seals retain their byte representation. Fixture links
    hash their exact readlink bytes; FIFO hashes its type and physical identity,
    without opening a FIFO for I/O or claiming ownership of a link's target.
    """
    digest = hashlib.sha256()
    members = apparent = 0
    inode_hashes: dict[tuple[int, int], tuple[tuple[Any, ...], str, int]] = {}
    stack: list[tuple[tuple[str, ...], str]] = [((), "")]
    try:
        with opened_claimed_tree(path, profile=profile) as (root_fd, mount_id):
            while stack:
                components, relative = stack.pop()
                if len(components) > max_depth or max_fds < 5:
                    raise ScratchSecurityError("workspace seal exceeds depth/fd limit")
                directory_fd = os.dup(root_fd)
                try:
                    for component in components:
                        following, _ = _checked_child(directory_fd, component, mount_id, profile=profile)
                        os.close(directory_fd)
                        directory_fd = following
                    entries: list[str] = []
                    with os.scandir(directory_fd) as iterator:
                        for entry in iterator:
                            if not components and entry.name == MANIFEST_NAME and not include_control_manifest:
                                continue
                            if len(entries) >= max_entries - members:
                                raise ScratchSecurityError("workspace seal exceeds the entry limit")
                            entries.append(entry.name)
                    entries.sort(key=os.fsencode)
                    for name in entries:
                        members += 1
                        child_relative = f"{relative}/{name}" if relative else name
                        child_fd, metadata = _checked_child(directory_fd, name, mount_id, profile=profile)
                        try:
                            if stat.S_ISDIR(metadata.st_mode):
                                digest.update(os.fsencode(f"D:{child_relative}:{_identity(metadata)}\n"))
                                stack.append(((*components, name), child_relative))
                                continue
                            if stat.S_ISLNK(metadata.st_mode):
                                target = os.fsencode(os.readlink(name, dir_fd=directory_fd))
                                digest.update(os.fsencode(f"L:{child_relative}:{_identity(metadata)}:")
                                              + len(target).to_bytes(8, "big") + target + b"\n")
                                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                                if (_identity(final), final.st_ctime_ns) != (_identity(metadata), metadata.st_ctime_ns):
                                    raise ScratchSecurityError("workspace seal link changed")
                                continue
                            if stat.S_ISFIFO(metadata.st_mode):
                                digest.update(os.fsencode(f"P:{child_relative}:{_identity(metadata)}\n"))
                                continue
                            inode_key = (metadata.st_dev, metadata.st_ino)
                            inode_claim = (_identity(metadata), metadata.st_mode, metadata.st_uid,
                                           metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns,
                                           metadata.st_ctime_ns)
                            cached = inode_hashes.get(inode_key)
                            if cached is not None:
                                if cached[0] != inode_claim:
                                    raise ScratchSecurityError("workspace shared inode changed during seal")
                                file_digest_value, total = cached[1], cached[2]
                                digest.update(os.fsencode(f"F:{child_relative}:{_identity(metadata)}:{total}:{int(metadata.st_mtime_ns)}:{file_digest_value}\n"))
                                continue
                            if metadata.st_size > max_file_bytes:
                                raise ScratchSecurityError("workspace seal exceeds the per-file byte limit")
                            if metadata.st_size > max_bytes - apparent:
                                raise ScratchSecurityError("workspace seal exceeds the byte limit")
                            fd = os.open(f"/proc/self/fd/{child_fd}", os.O_RDONLY | os.O_NONBLOCK)
                            file_digest = hashlib.sha256()
                            total = 0
                            def claims(value: os.stat_result) -> tuple[Any, ...]:
                                return (_identity(value), value.st_mode, value.st_uid,
                                        value.st_nlink, value.st_size, value.st_mtime_ns,
                                        value.st_ctime_ns)
                            with os.fdopen(fd, "rb") as stream:
                                if claims(os.fstat(stream.fileno())) != claims(metadata):
                                    raise ScratchSecurityError("workspace seal identity changed")
                                while chunk := stream.read(min(1024 * 1024, max_file_bytes - total + 1)):
                                    total += len(chunk)
                                    if total > max_file_bytes or total > max_bytes - apparent:
                                        raise ScratchSecurityError("workspace seal exceeds the byte limit")
                                    file_digest.update(chunk)
                                after = os.fstat(stream.fileno())
                            final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                            if claims(final) != claims(metadata) or claims(after) != claims(metadata) or total != metadata.st_size:
                                raise ScratchSecurityError("workspace seal payload changed")
                            file_digest_value = "sha256:" + file_digest.hexdigest()
                            digest.update(os.fsencode(f"F:{child_relative}:{_identity(metadata)}:{total}:{int(metadata.st_mtime_ns)}:{file_digest_value}\n"))
                            apparent += total
                            inode_hashes[inode_key] = (inode_claim, file_digest_value, total)
                        finally:
                            os.close(child_fd)
                finally:
                    os.close(directory_fd)
    except (OSError, ScratchTreeError) as exc:
        raise ScratchSecurityError(f"workspace could not be sealed: {exc}") from exc
    return "sha256:" + digest.hexdigest(), members, apparent


# Public adapter over the one canonical seal; callers do not copy its protocol.
workspace_payload_digest = _sealed_workspace_digest


def verified_workspace_payload_profile(
    path: Path, *, owner: str, expected_policy: Mapping[str, Any] | None = None,
) -> PayloadProfile:
    """Read an owner-issued policy without trusting a registry metadata label.

    This is read-only and does not import the registry or inspect the payload.
    Consumers still enforce their lifecycle, mount, dependency and intent guards.
    """
    path = _validate_absolute_path(Path(path), label="scratch workspace")
    # Import the owner lazily: the contract module is loaded by
    # ``runtime.scratch`` and must remain independent at import time.
    scratch_module = importlib.import_module("neocortex.runtime.scratch")
    manager_type = scratch_module.ScratchManager
    manager = manager_type(path.parent, owner=owner)
    manager._ensure_root(create=False)
    root_metadata = path.parent.lstat()
    if root_metadata.st_uid != os.geteuid():
        raise ScratchSecurityError("scratch policy root owner changed")
    payload = json.loads(_read_manifest_bytes(path / MANIFEST_NAME))
    if (not isinstance(payload, dict) or payload.get("schema") != SCRATCH_SCHEMA
            or payload.get("owner") != owner or payload.get("path") != str(path)
            or payload.get("manifest_digest") != _manifest_digest(payload)
            or not _same_identity(path, payload.get("path_identity", []))
            or not _same_identity(path.parent, payload.get("root_identity", []))):
        raise ScratchSecurityError("scratch payload policy has no verified owner claim")
    profile = manager._payload_profile(path, payload)
    policy = {"schema": "neocortex.scratch-payload-policy/v1", "payload_profile": profile.value,
              "policy_revision": policy_revision(profile), "fixture_grant": payload.get("fixture_grant")}
    if expected_policy is not None and policy != dict(expected_policy):
        raise ScratchSecurityError("scratch payload policy projection changed")
    return profile


def _bounded_payload_observation(
    path: Path,
    budget: _ScratchScanBudget,
    *,
    profile: PayloadProfile | str = PayloadProfile.STRICT,
) -> _PayloadObservation:
    """One descriptor/mount/type policy with shared record and member budgets."""
    remaining_entries = max(0, (_MAX_RECORDS if budget.max_entries is None else budget.max_entries)
                            - budget.payload_entries)
    remaining_bytes = max(0, (_MAX_SCAN_BYTES if budget.max_bytes is None else budget.max_bytes)
                          - budget.observed_bytes)
    observed = observe_claimed_tree(
        path, limit=remaining_entries, max_depth=budget.max_depth if budget.max_depth is not None else 2048,
        max_bytes=remaining_bytes, max_fds=budget.max_fds, profile=profile,
    )
    budget.payload_entries += observed.members
    budget.observed_bytes += observed.apparent_bytes
    if observed.issue in {ENTRY_LIMIT, DEPTH_LIMIT, BYTE_LIMIT, "fd_limit"}:
        budget.note(observed.issue)
    return _PayloadObservation(
        size_bytes=observed.apparent_bytes, observed_bytes=observed.apparent_bytes,
        issue=observed.issue, truncated=budget.truncated, size_complete=observed.complete,
    )


@dataclass(slots=True)
class _ScratchScanBudget:
    """Shared budget for one bounded scratch observation."""

    max_entries: int | None
    max_depth: int | None
    max_bytes: int | None
    max_fds: int = 2048
    entries: int = 0
    payload_entries: int = 0
    observed_bytes: int = 0
    hashed_entries: int = 0
    hashed_bytes: int = 0
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)

    def reserve_entry(self) -> bool:
        # max_entries bounds top-level records and the aggregate payload
        # members independently. Root slots do not consume the member budget.
        if self.max_entries is not None and self.payload_entries >= self.max_entries:
            self.note(ENTRY_LIMIT)
            return False
        self.payload_entries += 1
        return True

    def account_bytes(self, apparent_bytes: int) -> tuple[int, bool]:
        apparent = max(0, int(apparent_bytes))
        if self.max_bytes is None:
            self.observed_bytes += apparent
            return apparent, False
        remaining = max(0, self.max_bytes - self.observed_bytes)
        credited = min(apparent, remaining)
        self.observed_bytes += credited
        bounded = credited < apparent
        if bounded:
            self.note(BYTE_LIMIT)
        return credited, bounded


@dataclass(frozen=True, slots=True)
class _PayloadObservation:
    """No-follow payload accounting for one workspace."""

    size_bytes: int
    observed_bytes: int
    issue: str | None = None
    truncated: bool = False
    size_complete: bool = True


def _workspace_payload_issue(path: Path, *, profile: PayloadProfile | str = PayloadProfile.STRICT) -> str | None:
    """The same strict type/owner policy is used by every observation route."""

    observed = _bounded_payload_observation(path, _ScratchScanBudget(None, None, None), profile=profile)
    return observed.issue


def _remove_tree_no_follow(path: Path, *, expected_identity: Sequence[int] | None = None,
                           profile: PayloadProfile | str = PayloadProfile.STRICT,
                           effect_callback: Any | None = None) -> None:
    """Delegate effect primitives; existing lifecycle/dependency guards remain."""

    try:
        identity = None if expected_identity is None else tuple(expected_identity)
        if identity is not None and len(identity) != 3:
            raise ScratchSecurityError("scratch workspace identity is invalid")
        # The seal excludes the one root control manifest; physical retirement
        # also visits that regular file in addition to bounded payload members.
        remove_claimed_tree(
            path, expected_identity=identity,
            limit=_MAX_RECORDS + 1, profile=profile, effect_callback=effect_callback,
        )
    except ScratchTreeError as exc:
        raise ScratchSecurityError(str(exc)) from exc


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("manifest_digest", None)
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any], *, limit: int = _MAX_MANIFEST_BYTES) -> None:
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("scratch manifest exceeds the durable size limit")
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


def _load_artifact_registry_type() -> Any:
    """Load the optional ArtifactRegistry without making scratch import it.

    Artifact registration is a persistence concern owned by another runtime
    module.  Keeping this lookup lazy avoids an import cycle and, more
    importantly, leaves read-only scratch inspection independent from that
    optional integration.  A test or embedding application may also expose a
    type directly on this module; that is intentionally kept as a narrow
    dependency-injection seam.
    """

    injected = globals().get("ArtifactRegistry")
    if not callable(injected):
        # Preserve the narrow test/embedding injection seam exposed by the
        # public ``runtime.scratch`` module after this helper moved out of it.
        try:
            scratch_module = importlib.import_module("neocortex.runtime.scratch")
            injected = getattr(scratch_module, "ArtifactRegistry", None)
        except ImportError:
            injected = None
    if callable(injected):
        return injected
    for module_name in _ARTIFACT_REGISTRY_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Do not hide a missing dependency raised from inside an existing
            # registry module.  Only an absent candidate module is optional.
            if exc.name != module_name:
                raise
            continue
        registry_type = getattr(module, "ArtifactRegistry", None)
        if callable(registry_type):
            return registry_type
    raise ScratchSecurityError("configured artifact registry is unavailable")


def _invoke_artifact_callable(
    method: Any,
    payload: Mapping[str, Any],
    *,
    operation: str,
) -> Any:
    """Invoke one registry adapter while respecting its concrete signature.

    The ArtifactRegistry contract is intentionally small, but existing
    producers can expose a mapping helper, ``update(artifact_id, **fields)``
    or a strict keyword signature.  Inspecting the bound callable lets this
    adapter support those forms without a retry that could duplicate a
    successful filesystem-backed write after an implementation-level
    ``TypeError``.
    """

    if not callable(method):
        raise TypeError(f"artifact registry {operation} hook is not callable")
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        # Python extension callables may not expose a signature.  The
        # preferred public contract is keyword based, so let any resulting
        # TypeError remain an actionable registration failure.
        return method(**dict(payload))

    parameters = tuple(signature.parameters.values())
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    has_var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    mapping_names = {"record", "artifact", "entry", "payload", "data", "fields", "changes"}

    # A small helper-style registry commonly accepts one mapping rather than
    # the expanded keyword contract.  Do not mistake ``artifact_id`` for such
    # a parameter: it is the ordinary positional form of update().
    if (
        len(positional) == 1
        and not has_var_keyword
        and positional[0].name in mapping_names
        and positional[0].name not in payload
    ):
        return method(dict(payload))

    # ``update(artifact_id, changes)`` is another explicit equivalent of
    # ``update(artifact_id, **fields)``.  The mapping is copied so a registry
    # cannot mutate the scratch manifest data held by the caller.
    if (
        operation == "update"
        and len(positional) >= 2
        and positional[1].name in mapping_names
        and positional[0].name in payload
        and positional[1].name not in payload
    ):
        first = payload[positional[0].name]
        changes = {
            key: value
            for key, value in payload.items()
            if key not in {"artifact_id", "record_id"}
        }
        return method(first, changes)

    positional_args: list[Any] = []
    keyword_args: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            keyword_args.update(payload)
            continue
        if parameter.name not in payload:
            # Optional parameters can be omitted.  Required parameters are
            # deliberately left for the normal TypeError below; wrapping it
            # at the manager boundary preserves fail-closed behavior.
            continue
        value = payload[parameter.name]
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional_args.append(value)
        else:
            keyword_args[parameter.name] = value
    return method(*positional_args, **keyword_args)


def _scratch_write_locked(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any):
        if not self._ensure_root(create=False):
            return method(self, *args, **kwargs)
        with self._scratch_lock():
            return method(self, *args, **kwargs)
    return wrapped


@dataclass(frozen=True, slots=True)
class FixturePayloadGrant:
    """Reference to an explicit, durable owner grant; metadata is not a grant."""

    grant_id: str
    owner: str
    activity_id: str
    creation_grant_id: str


@dataclass(frozen=True, slots=True)
class ScratchRecord:
    """One bounded inspection result for a registered workspace."""

    record_id: str
    owner: str
    run_id: int | str | None
    path: Path
    state: ScratchState | str
    created_ns: int
    updated_ns: int
    path_identity: tuple[int, int, int] | None
    root_identity: tuple[int, int, int] | None
    size_bytes: int
    retain_on_success: bool
    retire_after_ns: int | None
    result_paths: tuple[Path, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    manifest_digest: str | None = None
    eligible: bool = False
    valid: bool = True
    issue: str | None = None
    artifact_id: str | None = None
    payload_size_bytes: int | None = None
    seal: Mapping[str, Any] | None = None
    size_complete: bool = True
    payload_profile: str = PayloadProfile.STRICT.value
    policy_revision: str = "strict/v1"
    fixture_grant: Mapping[str, Any] | None = None

    @property
    def posix_path_identity(self) -> Mapping[str, str]:
        return PathIdentity.from_path(self.path).as_dict()

    @property
    def status(self) -> str:
        """Alias used by lightweight adapters."""

        return self.state.value if isinstance(self.state, ScratchState) else self.state

    @property
    def identity(self) -> tuple[int, int, int] | None:
        """Alias for the physical workspace identity."""

        return self.path_identity


@dataclass(frozen=True, slots=True)
class ScratchPlan:
    """Bounded result of a plan or apply pass."""

    root: Path
    records: tuple[ScratchRecord, ...] = ()
    planned: int = 0
    applied: int = 0
    kept: int = 0
    blocked: int = 0
    failed: int = 0
    recovery_required: int = 0
    planned_bytes: int = 0
    applied_bytes: int = 0
    kept_bytes: int = 0
    blocked_bytes: int = 0
    failed_bytes: int = 0
    recovery_required_bytes: int = 0
    status: str = "planned"
    reason: str | None = None
    read_only: bool = True
    unmanaged: tuple[Path, ...] = ()
    root_blocked: str | None = None
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    max_entries: int | None = None
    max_depth: int | None = None
    max_bytes: int | None = None
    max_fds: int = 2048
    observed_members: int = 0
    observed_bytes: int = 0

    @property
    def coverage(self) -> str:
        if self.complete:
            return "complete"
        return "partial" if self.observed_members or any(record.size_complete for record in self.records) else "not_measured"

    @property
    def complete(self) -> bool:
        """Whether the bounded observation covered its requested scope."""

        return (not self.truncated and self.root_blocked is None
                and all(record.size_complete for record in self.records))

    @property
    def limits(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for name, value in (
            ("max_entries", self.max_entries),
            ("max_depth", self.max_depth),
            ("max_bytes", self.max_bytes),
        ):
            if value is not None:
                result[name] = value
        return result

    @property
    def counts(self) -> dict[str, int]:
        return {
            "observed": len(self.records),
            "planned": self.planned,
            "applied": self.applied,
            "kept": self.kept,
            "blocked": self.blocked,
            "failed": self.failed,
            "recovery_required": self.recovery_required,
            "unmanaged": len(self.unmanaged),
        }

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "planned": self.planned_bytes,
            "applied": self.applied_bytes,
            "kept": self.kept_bytes,
            "blocked": self.blocked_bytes,
            "failed": self.failed_bytes,
            "recovery_required": self.recovery_required_bytes,
        }

    @property
    def items(self) -> tuple[ScratchRecord, ...]:
        return self.records

    @property
    def entries(self) -> tuple[ScratchRecord, ...]:
        return self.records


