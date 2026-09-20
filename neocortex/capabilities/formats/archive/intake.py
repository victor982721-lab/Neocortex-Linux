"""Physical, fail-closed ZIP intake.

This module is deliberately a small boundary between inventory and the normal
filesystem pipeline.  A ZIP is either an atomic package (OOXML/ODF/EPUB/APK/
JAR), a generic storage ZIP that can be expanded as data, or an invalid/unsafe
input.  Generic ZIPs are fully verified in private staging before one
transactional publication.  This module never executes, imports, or opens a
member with an external application.

The orchestration layer owns policy and effects.  It can inject a registered
scratch provider, a publisher, and a KIO-trash hook.  The default staging
provider uses the canonical :class:`ScratchManager`; tests may use the
explicitly named ``FilesystemStageFactory``.  There is intentionally no
virtual-member or archive-state model here.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast, runtime_checkable

from neocortex.platform.zip_safety import (
    DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
    ZipMemberStructure,
    ZipStructure,
    ZipStructureError,
    inspect_zip_structure,
)

# Keep classification marker reads bounded.  Classification is not a second
# complete extraction: only package marker members are read.  Generic ZIPs are
# completely streamed only after the caller selects apply mode.
MAX_MARKER_BYTES = 2 * 1024 * 1024
MAX_MIMETYPE_BYTES = 256
CHUNK_BYTES = 64 * 1024
RENAME_NOREPLACE = 1

IntakeKind = Literal["generic_zip", "atomic_package", "invalid"]
IntakeStatus = Literal[
    "planned",
    "atomic",
    "applied",
    "skipped_by_size",
    "corrupt",
    "unsafe",
    "ambiguous",
    "password",
    "budget",
    "timeout",
    "collision",
    "source_changed",
    "dependency",
    "blocked",
    "recovery_required",
    "already_applied",
]


class ZipIntakeError(RuntimeError):
    """A fail-closed ZIP intake boundary error."""

    def __init__(self, status: IntakeStatus, reason: str, detail: str | None = None):
        super().__init__(detail or reason)
        self.status = status
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True, slots=True)
class ZipIntakeLimits:
    """Independent bounded resources for one physical ZIP intake."""

    max_members: int = 20_000
    max_member_bytes: int = 64 * 1024 * 1024
    max_total_uncompressed_bytes: int = 512 * 1024 * 1024
    max_total_temp_bytes: int = 512 * 1024 * 1024
    max_input_bytes: int = 2 * 1024 * 1024 * 1024
    max_nested_depth: int = 5
    # Compatibility spelling used by the older archive safety contract.  If
    # supplied, it narrows/replaces ``max_nested_depth``; no route-specific
    # policy is created by this alias.
    max_depth: int | None = field(default=None, kw_only=True)
    max_compression_ratio: float = 200.0
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES
    timeout_seconds: float = 60.0
    # Zero means no extra reserve beyond the bytes being extracted.  The
    # observed free-space check is still always performed before writing.
    min_free_bytes: int = 0

    def validate(self) -> None:
        values = {
            "max_members": self.max_members,
            "max_member_bytes": self.max_member_bytes,
            "max_total_uncompressed_bytes": self.max_total_uncompressed_bytes,
            "max_total_temp_bytes": self.max_total_temp_bytes,
            "max_input_bytes": self.max_input_bytes,
            "max_nested_depth": self.max_nested_depth,
            "max_central_directory_bytes": self.max_central_directory_bytes,
            "min_free_bytes": self.min_free_bytes,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "max_members",
            "max_member_bytes",
            "max_total_uncompressed_bytes",
            "max_total_temp_bytes",
            "max_input_bytes",
            "max_central_directory_bytes",
        ):
            if values[name] < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_depth is not None:
            if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int) or self.max_depth < 0:
                raise ValueError("max_depth must be a non-negative integer or None")
        if not isinstance(self.max_compression_ratio, (int, float)) or isinstance(
            self.max_compression_ratio, bool
        ) or not math.isfinite(float(self.max_compression_ratio)) or self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be a finite positive number")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ) or not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")

    @property
    def depth_limit(self) -> int:
        return self.max_nested_depth if self.max_depth is None else self.max_depth


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Path-bound identity captured before a ZIP effect."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int

    @classmethod
    def capture(cls, path: Path) -> "SourceIdentity":
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ZipIntakeError("blocked", "source_unavailable", str(exc)) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ZipIntakeError("unsafe", "source_not_regular", "ZIP source must be a regular non-link file")
        return cls(
            path=os.fspath(path),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            mtime_ns=int(metadata.st_mtime_ns),
            ctime_ns=int(metadata.st_ctime_ns),
            nlink=int(metadata.st_nlink),
        )

    def matches(self, path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return (
            not stat.S_ISLNK(metadata.st_mode)
            and stat.S_ISREG(metadata.st_mode)
            and int(metadata.st_dev) == self.device
            and int(metadata.st_ino) == self.inode
            and int(metadata.st_size) == self.size
            and int(metadata.st_mtime_ns) == self.mtime_ns
            and int(metadata.st_ctime_ns) == self.ctime_ns
            and int(metadata.st_nlink) == self.nlink
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "nlink": self.nlink,
        }


# Process-local proof for a replay in the same run owner.  It is not durable
# archive state and never replaces the caller's action ledger; it only lets a
# KIO fixture/replay recognize the exact inode already published by this
# engine instead of merging into an arbitrary pre-existing destination.
_COMPLETED_DESTINATIONS: dict[str, SourceIdentity] = {}


def _same_physical_identity(left: SourceIdentity, right: SourceIdentity) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.ctime_ns == right.ctime_ns
        and left.nlink == right.nlink
    )


@dataclass(frozen=True, slots=True)
class ZipIntakeClassification:
    """Single reusable content-based ZIP package decision."""

    kind: IntakeKind
    status: str
    unit_kind: str
    evidence: tuple[str, ...] = ()
    member_count: int = 0
    estimated_uncompressed_bytes: int = 0
    detail: str | None = None
    structure: ZipStructure | None = field(default=None, repr=False, compare=False)

    @property
    def is_generic(self) -> bool:
        return self.kind == "generic_zip" and self.status == "validated"

    @property
    def is_atomic(self) -> bool:
        return self.kind == "atomic_package" and self.status == "validated"

    @property
    def valid(self) -> bool:
        return self.status == "validated"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "unit_kind": self.unit_kind,
            "evidence": list(self.evidence),
            "member_count": self.member_count,
            "estimated_uncompressed_bytes": self.estimated_uncompressed_bytes,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ZipIntakeOutcome:
    """Typed, explicit result for a plan or physical transaction."""

    status: IntakeStatus
    source_path: str
    source_identity: SourceIdentity | None
    classification: ZipIntakeClassification
    apply: bool = False
    destination: str | None = None
    members: int = 0
    uncompressed_bytes: int = 0
    published: bool = False
    trashed: bool = False
    successor_paths: tuple[str, ...] = ()
    reason: str | None = None
    detail: str | None = None
    source_sha256: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"planned", "atomic", "applied"}

    @property
    def physical_effect(self) -> bool:
        return self.published or self.trashed

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "neocortex.zip-intake/v1",
            "status": self.status,
            "source_path": self.source_path,
            "source_identity": None if self.source_identity is None else self.source_identity.to_dict(),
            "classification": self.classification.to_dict(),
            "apply": self.apply,
            "destination": self.destination,
            "members": self.members,
            "uncompressed_bytes": self.uncompressed_bytes,
            "published": self.published,
            "trashed": self.trashed,
            "successor_paths": list(self.successor_paths),
            "reason": self.reason,
            "detail": self.detail,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    """Evidence returned by a publisher after a no-replace publication."""

    destination: Path
    staged_path: Path
    published: bool = True


@dataclass(frozen=True, slots=True)
class TrashDisposition:
    """Result returned by a caller-owned KIO Trash adapter."""

    status: Literal["applied", "blocked", "recovery_required"]
    detail: str | None = None
    evidence: str | None = None


@runtime_checkable
class StageLease(Protocol):
    path: Path

    def complete(self) -> None: ...

    def fail(self, reason: str) -> None: ...


@runtime_checkable
class StageFactory(Protocol):
    def create(self, *, source: Path, metadata: Mapping[str, object]) -> StageLease: ...


@runtime_checkable
class PublishHook(Protocol):
    def publish(self, staged_path: Path, destination: Path) -> PublishReceipt: ...

    def rollback(self, receipt: PublishReceipt) -> bool: ...


@runtime_checkable
class TrashHook(Protocol):
    def trash(self, source: Path, identity: SourceIdentity) -> TrashDisposition: ...


class FilesystemStageFactory:
    """Explicit test/local provider; it never uses an unowned system /tmp."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = _private_directory(Path(root), label="staging root", create=True)

    def create(self, *, source: Path, metadata: Mapping[str, object]) -> StageLease:
        path = Path(tempfile.mkdtemp(prefix=".zip-intake-", dir=os.fspath(self.root)))
        os.chmod(path, 0o700)
        return _FilesystemStageLease(path)


class _FilesystemStageLease:
    def __init__(self, path: Path):
        self.path = path
        self._closed = False

    def complete(self) -> None:
        if not self._closed:
            shutil.rmtree(self.path, ignore_errors=False)
            self._closed = True

    def fail(self, reason: str) -> None:
        if not self._closed:
            shutil.rmtree(self.path, ignore_errors=True)
            self._closed = True


class ScratchStageFactory:
    """Adapter over the canonical registered ScratchManager primitive."""

    def __init__(self, root: str | os.PathLike[str], *, artifact_registry_root: Path | None = None):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("staging root must be absolute")
        self.artifact_registry_root = artifact_registry_root

    def create(self, *, source: Path, metadata: Mapping[str, object]) -> StageLease:
        # Lazy import keeps the public intake import independent of lifecycle
        # modules, while production still records every workspace canonically.
        from neocortex.runtime.scratch import ScratchManager

        if self.artifact_registry_root is None:
            manager = ScratchManager(
                self.root,
                owner="archive-intake",
                create_root=True,
            )
        else:
            manager = ScratchManager(
                self.root,
                owner="archive-intake",
                create_root=True,
                artifact_registry_root=self.artifact_registry_root,
            )
        workspace = manager.create(
            run_id=None,
            retain_on_success=False,
            metadata=dict(metadata),
        )
        path = getattr(workspace, "path", None)
        if not isinstance(path, Path):
            raise ZipIntakeError("dependency", "scratch_workspace_invalid", "ScratchManager did not return a Path workspace")
        return _ScratchStageLease(workspace, path)


class _ScratchStageLease:
    def __init__(self, workspace: object, path: Path):
        self.workspace = workspace
        self.path = path
        self._closed = False

    def complete(self) -> None:
        if self._closed:
            return
        complete = getattr(self.workspace, "complete", None)
        if not callable(complete):
            raise ZipIntakeError("dependency", "scratch_complete_unavailable")
        complete()
        self._closed = True

    def fail(self, reason: str) -> None:
        if self._closed:
            return
        fail = getattr(self.workspace, "fail", None)
        if not callable(fail):
            raise ZipIntakeError("dependency", "scratch_fail_unavailable")
        fail(reason)
        self._closed = True


class FilesystemPublishHook:
    """No-replace directory publication with an explicit rollback seam."""

    def publish(self, staged_path: Path, destination: Path) -> PublishReceipt:
        _assert_real_directory(staged_path, label="staged publication")
        _assert_destination_parent(destination)
        if os.path.lexists(destination):
            raise ZipIntakeError("collision", "destination_collision", "destination already exists")
        _rename_directory_noreplace(staged_path, destination)
        _fsync_directory(destination.parent)
        return PublishReceipt(destination=destination, staged_path=staged_path)

    def rollback(self, receipt: PublishReceipt) -> bool:
        try:
            if not receipt.published or not os.path.lexists(receipt.destination):
                return True
            if os.path.lexists(receipt.staged_path):
                return False
            _rename_directory_noreplace(receipt.destination, receipt.staged_path)
            _fsync_directory(receipt.destination.parent)
            return True
        except (OSError, ZipIntakeError):
            return False


@dataclass(frozen=True, slots=True)
class _Member:
    info: zipfile.ZipInfo
    relative_name: str
    is_directory: bool
    structure: ZipMemberStructure | None


@dataclass(frozen=True, slots=True)
class _Preflight:
    structure: ZipStructure
    members: tuple[_Member, ...]
    total_uncompressed_bytes: int


@dataclass(slots=True)
class _ExtractionBudget:
    members: int = 0
    total_uncompressed_bytes: int = 0
    temp_bytes: int = 0
    started: float = 0.0


@dataclass(frozen=True, slots=True)
class _Deadline:
    when: float
    cancellation: object | None = None

    def check(self) -> None:
        token = self.cancellation
        if token is not None:
            checkpoint = getattr(token, "checkpoint", None)
            try:
                if callable(checkpoint):
                    checkpoint()
                elif bool(getattr(token, "is_cancelled", False)):
                    raise ZipIntakeError(
                        "blocked",
                        "cancelled",
                        "ZIP intake cancellation requested",
                    )
            except ZipIntakeError:
                raise
            except BaseException as exc:
                if type(exc).__name__ in {"CancellationRequested", "CancelledError"}:
                    raise ZipIntakeError(
                        "blocked",
                        "cancelled",
                        "ZIP intake cancellation requested",
                    ) from exc
                raise
        if time.monotonic() > self.when:
            raise ZipIntakeError("timeout", "zip_intake_timeout", "ZIP intake deadline exceeded")


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _private_directory(path: Path, *, label: str, create: bool) -> Path:
    if not path.is_absolute():
        raise ZipIntakeError("dependency", f"{label}_not_absolute")
    if "\x00" in os.fspath(path):
        raise ZipIntakeError("dependency", f"{label}_contains_nul")
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
    except OSError as exc:
        raise ZipIntakeError("dependency", f"{label}_unavailable", str(exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ZipIntakeError("dependency", f"{label}_not_directory")
    if metadata.st_mode & 0o077:
        raise ZipIntakeError("dependency", f"{label}_not_private")
    return path


def _assert_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ZipIntakeError("dependency", f"{label}_missing", str(exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ZipIntakeError("unsafe", f"{label}_not_directory")


def _assert_destination_parent(destination: Path) -> None:
    if not destination.is_absolute() or "\x00" in os.fspath(destination):
        raise ZipIntakeError("unsafe", "destination_invalid")
    parent = destination.parent
    # Every existing component between the root and the parent must be a real
    # directory; do not publish through a symlinked path.
    components = list(parent.parts)
    current = Path(components[0])
    for component in components[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ZipIntakeError("blocked", "destination_parent_unavailable", str(exc)) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ZipIntakeError("unsafe", "destination_parent_not_directory")
    if not parent.is_dir():
        raise ZipIntakeError("blocked", "destination_parent_missing")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE); refuse unsafe overwrite fallback."""

    source_parent_fd: int | None = None
    destination_parent_fd: int | None = None
    try:
        source_parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        destination_parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ZipIntakeError("dependency", "rename_noreplace_unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_parent_fd,
            os.fsencode(source.name),
            destination_parent_fd,
            os.fsencode(destination.name),
            RENAME_NOREPLACE,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise ZipIntakeError("collision", "destination_collision")
            if error_number == errno.EXDEV:
                raise ZipIntakeError("dependency", "staging_destination_different_filesystem")
            raise ZipIntakeError("dependency", "publish_rename_failed", os.strerror(error_number))
    except ZipIntakeError:
        raise
    except OSError as exc:
        raise ZipIntakeError("dependency", "publish_rename_failed", str(exc)) from exc
    finally:
        for descriptor in (source_parent_fd, destination_parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _safe_member_name(raw_name: str) -> tuple[bool, str]:
    """Validate a ZIP member without relying on post-join ``resolve``."""

    if not isinstance(raw_name, str) or not raw_name or "\x00" in raw_name:
        return False, raw_name if isinstance(raw_name, str) else ""
    if "\\" in raw_name or raw_name.startswith(("/", "//")):
        return False, raw_name
    if _DRIVE_PREFIX.match(raw_name) or raw_name.startswith("~"):
        return False, raw_name
    if raw_name.endswith("/"):
        normalized = raw_name[:-1]
    else:
        normalized = raw_name
    parts = normalized.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False, raw_name
    # PurePosixPath is used only as a second structural check; no filesystem
    # resolution or symlink following occurs here.
    candidate = PurePosixPath(*parts)
    if candidate.is_absolute() or str(candidate).startswith("../") or str(candidate) == "..":
        return False, raw_name
    return True, "/".join(parts)


def _special_zip_mode(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return bool(file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR})


def _deadline_for(
    limits: ZipIntakeLimits,
    deadline: float | None,
    cancellation: object | None = None,
) -> _Deadline:
    if deadline is None:
        return _Deadline(time.monotonic() + float(limits.timeout_seconds), cancellation)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(float(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp")
    return _Deadline(float(deadline), cancellation)


def _structure_entries(structure: ZipStructure) -> tuple[ZipMemberStructure, ...]:
    return tuple(structure.entries)


def _preflight_zip(path: Path, *, limits: ZipIntakeLimits, deadline: _Deadline) -> _Preflight:
    deadline.check()
    try:
        structure = inspect_zip_structure(
            path,
            max_members=limits.max_members,
            max_central_directory_bytes=limits.max_central_directory_bytes,
        )
        with zipfile.ZipFile(path, "r") as archive:
            infos = tuple(archive.infolist())
            if len(infos) != structure.members or len(infos) > limits.max_members:
                raise ZipIntakeError("unsafe", "member_count_changed")
            entries = _structure_entries(structure)
            if entries and len(entries) != len(infos):
                raise ZipIntakeError("unsafe", "central_directory_member_count_changed")
            structures = {entry.ordinal: entry for entry in entries}
            members: list[_Member] = []
            seen: set[str] = set()
            occupied: dict[str, bool] = {}
            total = 0
            for ordinal, info in enumerate(infos):
                deadline.check()
                valid, name = _safe_member_name(info.filename)
                if not valid:
                    raise ZipIntakeError("unsafe", "unsafe_member_name", info.filename[:512])
                is_directory = bool(info.is_dir() or info.filename.endswith("/"))
                if name in seen:
                    raise ZipIntakeError("unsafe", "duplicate_member_name", name[:512])
                seen.add(name)
                if _special_zip_mode(info):
                    raise ZipIntakeError("unsafe", "special_member", name[:512])
                if int(info.flag_bits) & 0x1:
                    raise ZipIntakeError("password", "encrypted_member", name[:512])
                if info.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                    zipfile.ZIP_BZIP2,
                    zipfile.ZIP_LZMA,
                }:
                    raise ZipIntakeError("dependency", "unsupported_compression", str(info.compress_type))
                declared = int(info.file_size)
                compressed = int(info.compress_size)
                if declared < 0 or declared > limits.max_member_bytes:
                    raise ZipIntakeError("budget", "member_size_budget", name[:512])
                if declared and compressed <= 0:
                    raise ZipIntakeError("budget", "invalid_compression_size", name[:512])
                ratio = float(declared) / float(max(1, compressed))
                if ratio > float(limits.max_compression_ratio):
                    raise ZipIntakeError("budget", "compression_ratio_budget", name[:512])
                if not is_directory:
                    total += declared
                    if total > limits.max_total_uncompressed_bytes:
                        raise ZipIntakeError("budget", "total_uncompressed_budget")
                # A file cannot also be a parent directory and a directory
                # cannot be repeated under a different spelling.
                prefix_parts = name.split("/")
                for index in range(1, len(prefix_parts)):
                    prefix = "/".join(prefix_parts[:index])
                    if occupied.get(prefix) is False:
                        raise ZipIntakeError("unsafe", "file_directory_collision", prefix)
                previous = occupied.get(name)
                if previous is not None and previous != is_directory:
                    raise ZipIntakeError("unsafe", "file_directory_collision", name)
                occupied[name] = is_directory
                for index in range(1, len(prefix_parts)):
                    occupied.setdefault("/".join(prefix_parts[:index]), True)
                members.append(_Member(info, name, is_directory, structures.get(ordinal)))
            return _Preflight(structure, tuple(members), total)
    except ZipIntakeError:
        raise
    except PermissionError as exc:
        raise ZipIntakeError("blocked", "zip_permission_denied", str(exc)) from exc
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "zip_structure_invalid", f"{type(exc).__name__}: {exc}") from exc


def _read_marker(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int) -> bytes:
    if int(info.flag_bits) & 0x1:
        raise ZipIntakeError("password", "encrypted_marker", info.filename[:512])
    if int(info.file_size) > limit:
        raise ZipIntakeError("budget", "marker_size_budget", info.filename[:512])
    try:
        with archive.open(info, "r") as stream:
            payload = stream.read(limit + 1)
            if len(payload) > limit:
                raise ZipIntakeError("budget", "marker_size_budget", info.filename[:512])
            # Consume exactly to EOF; zipfile performs CRC validation when the
            # declared payload is exhausted.
            tail = stream.read(1)
            if tail:
                raise ZipIntakeError("budget", "marker_size_budget", info.filename[:512])
            return payload
    except ZipIntakeError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "marker_integrity_failed", str(exc)) from exc


def _parse_xml_marker(payload: bytes, name: str) -> None:
    try:
        from neocortex.capabilities.formats.xml_safety import safe_xml_fromstring

        safe_xml_fromstring(payload)
    except (ValueError, UnicodeError, RuntimeError) as exc:
        raise ZipIntakeError("ambiguous", "package_marker_invalid", name) from exc


def _known_odf_mimes() -> set[str]:
    # Reuse the existing logical vocabulary without importing the legacy route
    # or its state.  The import is delayed so ``--help`` and inventory remain
    # cheap.
    try:
        from .logical import ODF_MIME_KINDS

        return set(ODF_MIME_KINDS)
    except (ImportError, AttributeError):
        return {
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "application/vnd.oasis.opendocument.graphics",
        }


def _classify_zip(path: Path, *, limits: ZipIntakeLimits, deadline: _Deadline) -> ZipIntakeClassification:
    """Classify only bounded package markers; never extract generic members."""

    try:
        structure = inspect_zip_structure(
            path,
            max_members=limits.max_members,
            max_central_directory_bytes=limits.max_central_directory_bytes,
        )
        with zipfile.ZipFile(path, "r") as archive:
            infos = tuple(archive.infolist())
            if len(infos) != structure.members:
                return ZipIntakeClassification("invalid", "corrupt", "invalid", detail="member count changed")
            names = tuple(info.filename for info in infos)
            normalized: list[str] = []
            for name in names:
                valid, clean = _safe_member_name(name)
                if not valid:
                    return ZipIntakeClassification("invalid", "unsafe", "invalid", member_count=len(infos), detail=f"unsafe member name: {name[:256]}", structure=structure)
                normalized.append(clean)
            duplicate_names = tuple(sorted(name for name, count in Counter(normalized).items() if count > 1))
            if duplicate_names:
                return ZipIntakeClassification("invalid", "unsafe", "invalid", tuple(f"duplicate:{name}" for name in duplicate_names[:8]), len(infos), detail="duplicate member names", structure=structure)
            by_name = dict(zip(normalized, infos, strict=True))
            total = sum(int(info.file_size) for info in infos if not info.is_dir() and not info.filename.endswith("/"))
            if total > limits.max_total_uncompressed_bytes:
                return ZipIntakeClassification("invalid", "budget", "invalid", member_count=len(infos), estimated_uncompressed_bytes=total, detail="total uncompressed budget", structure=structure)
            deadline.check()
            evidence: list[str] = []
            content_types = by_name.get("[Content_Types].xml")
            required_office: tuple[str, str] | None = None
            if content_types is not None:
                _parse_xml_marker(_read_marker(archive, content_types, limit=MAX_MARKER_BYTES), "[Content_Types].xml")
                if "word/document.xml" in by_name:
                    required_office = ("docx", "word/document.xml")
                elif "xl/workbook.xml" in by_name:
                    required_office = ("xlsx", "xl/workbook.xml")
                elif "ppt/presentation.xml" in by_name:
                    required_office = ("pptx", "ppt/presentation.xml")
                if required_office is not None:
                    marker = by_name[required_office[1]]
                    _parse_xml_marker(_read_marker(archive, marker, limit=MAX_MARKER_BYTES), required_office[1])
                    evidence.extend(("[Content_Types].xml", required_office[1]))
                    return ZipIntakeClassification("atomic_package", "validated", required_office[0], tuple(evidence), len(infos), total, structure=structure)
                # A malformed/incomplete OOXML-looking package is ambiguous,
                # not permission to expand arbitrary package internals.
                if any(name.startswith(("word/", "xl/", "ppt/")) for name in normalized):
                    return ZipIntakeClassification("invalid", "ambiguous", "invalid", ("content_types_without_functional_marker",), len(infos), total, detail="incomplete OOXML package", structure=structure)
            mimetype = by_name.get("mimetype")
            if mimetype is not None:
                payload = _read_marker(archive, mimetype, limit=MAX_MIMETYPE_BYTES)
                try:
                    declared_mime = payload.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ZipIntakeError("ambiguous", "mimetype_not_ascii") from exc
                if declared_mime == "application/epub+zip" and "META-INF/container.xml" in by_name:
                    # EPUB/ODF marker payloads are package data.  Reading to
                    # EOF verifies CRC; XML parsing is deliberately left to
                    # the normal package route, because small fixtures and
                    # older producers may use namespace-tolerant markers.
                    _read_marker(archive, by_name["META-INF/container.xml"], limit=MAX_MARKER_BYTES)
                    return ZipIntakeClassification("atomic_package", "validated", "epub", ("mimetype", "META-INF/container.xml"), len(infos), total, structure=structure)
                if declared_mime in _known_odf_mimes() and "content.xml" in by_name and "META-INF/manifest.xml" in by_name:
                    _read_marker(archive, by_name["content.xml"], limit=MAX_MARKER_BYTES)
                    _read_marker(archive, by_name["META-INF/manifest.xml"], limit=MAX_MARKER_BYTES)
                    return ZipIntakeClassification("atomic_package", "validated", "odf", ("mimetype", "content.xml", "META-INF/manifest.xml"), len(infos), total, structure=structure)
                if declared_mime in _known_odf_mimes() or declared_mime == "application/epub+zip":
                    return ZipIntakeClassification("invalid", "ambiguous", "invalid", ("mimetype",), len(infos), total, detail="incomplete package markers", structure=structure)
            android_manifest = by_name.get("AndroidManifest.xml")
            if android_manifest is not None:
                _read_marker(archive, android_manifest, limit=MAX_MARKER_BYTES)
                return ZipIntakeClassification("atomic_package", "validated", "apk", ("AndroidManifest.xml",), len(infos), total, structure=structure)
            jar_manifest = by_name.get("META-INF/MANIFEST.MF")
            if jar_manifest is not None:
                manifest_payload = _read_marker(archive, jar_manifest, limit=MAX_MARKER_BYTES)
                if b"Manifest-Version:" in manifest_payload:
                    return ZipIntakeClassification("atomic_package", "validated", "jar", ("META-INF/MANIFEST.MF",), len(infos), total, structure=structure)
            # A project is intentionally *generic*: its directory layout is
            # data to expand, not a virtual unit to preserve.
            project_markers = {
                "pyproject.toml",
                "package.json",
                "cargo.toml",
                "go.mod",
                "pom.xml",
                "build.gradle",
                "cmakelists.txt",
                "makefile",
                "meson.build",
                "setup.py",
                "setup.cfg",
            }
            project_evidence = tuple(name for name in normalized if PurePosixPath(name).name.casefold() in project_markers)
            if project_evidence:
                evidence.extend(f"project:{name}" for name in project_evidence[:8])
                return ZipIntakeClassification("generic_zip", "validated", "project", tuple(evidence), len(infos), total, structure=structure)
            return ZipIntakeClassification("generic_zip", "validated", "storage_archive", ("no_atomic_markers",), len(infos), total, structure=structure)
    except ZipIntakeError as exc:
        return ZipIntakeClassification("invalid", exc.status, "invalid", detail=exc.detail)
    except PermissionError as exc:
        return ZipIntakeClassification("invalid", "blocked", "invalid", detail=str(exc))
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        return ZipIntakeClassification("invalid", "corrupt", "invalid", detail=f"{type(exc).__name__}: {exc}")


def classify_zip(
    source: str | os.PathLike[str],
    *,
    limits: ZipIntakeLimits | None = None,
    deadline: float | None = None,
) -> ZipIntakeClassification:
    """Return the one bounded content-based classification for ``source``."""

    effective_limits = ZipIntakeLimits() if limits is None else limits
    effective_limits.validate()
    path = Path(source)
    identity = SourceIdentity.capture(path)
    if identity.size > effective_limits.max_input_bytes:
        return ZipIntakeClassification("invalid", "budget", "invalid", detail="source exceeds max_input_bytes")
    return _classify_zip(path, limits=effective_limits, deadline=_deadline_for(effective_limits, deadline))


def _default_destination(source: Path) -> Path:
    if source.suffix.casefold() == ".zip":
        return source.with_name(source.name[:-4])
    return source.with_name(source.name + ".extracted")


def _validate_destination(path: Path) -> Path:
    if not path.is_absolute() or "\x00" in os.fspath(path):
        raise ZipIntakeError("unsafe", "destination_invalid")
    if path.name in {"", ".", ".."} or "/" in path.name:
        raise ZipIntakeError("unsafe", "destination_invalid")
    return path


def _check_disk(path: Path, needed: int, *, limits: ZipIntakeLimits) -> None:
    try:
        free = int(shutil.disk_usage(path).free)
    except OSError as exc:
        raise ZipIntakeError("blocked", "disk_space_unavailable", str(exc)) from exc
    if free < max(0, int(needed)) + limits.min_free_bytes:
        raise ZipIntakeError("budget", "disk_space_budget")


def _source_sha256(path: Path, *, deadline: _Deadline) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                deadline.check()
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except ZipIntakeError:
        raise
    except OSError as exc:
        raise ZipIntakeError("blocked", "source_digest_unavailable", str(exc)) from exc
    return digest.hexdigest()


def _create_directory(path: Path) -> None:
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise ZipIntakeError("unsafe", "stage_path_collision", os.fspath(path))
        os.chmod(path, 0o700)
    else:
        path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _open_output(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise ZipIntakeError("unsafe", "stage_path_collision", os.fspath(path)) from exc
    except OSError as exc:
        raise ZipIntakeError("blocked", "stage_output_unavailable", str(exc)) from exc


def _stream_member(
    archive: zipfile.ZipFile,
    member: _Member,
    output: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    budget: _ExtractionBudget,
    disk_root: Path,
) -> None:
    info = member.info
    declared = int(info.file_size)
    if declared > limits.max_member_bytes or budget.total_uncompressed_bytes + declared > limits.max_total_uncompressed_bytes:
        raise ZipIntakeError("budget", "member_or_total_budget", member.relative_name)
    _check_disk(disk_root, declared, limits=limits)
    descriptor = _open_output(output)
    actual = 0
    crc = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as sink:
            descriptor = -1
            with archive.open(info, "r") as source:
                while True:
                    deadline.check()
                    chunk = source.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    actual += len(chunk)
                    budget.temp_bytes += len(chunk)
                    if actual > declared or actual > limits.max_member_bytes or budget.temp_bytes > limits.max_total_temp_bytes:
                        raise ZipIntakeError("budget", "extraction_budget", member.relative_name)
                    if budget.total_uncompressed_bytes + actual > limits.max_total_uncompressed_bytes:
                        raise ZipIntakeError("budget", "total_uncompressed_budget", member.relative_name)
                    _check_disk(disk_root, max(0, declared - actual), limits=limits)
                    sink.write(chunk)
                    crc = zlib.crc32(chunk, crc)
            sink.flush()
            os.fchmod(sink.fileno(), 0o600)
            os.fsync(sink.fileno())
    except zipfile.BadZipFile as exc:
        raise ZipIntakeError("corrupt", "member_crc_or_eof_failed", member.relative_name) from exc
    except (OSError, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "member_read_failed", str(exc)) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    crc &= 0xFFFFFFFF
    if actual != declared or crc != int(info.CRC):
        raise ZipIntakeError("corrupt", "member_integrity_failed", member.relative_name)
    budget.members += 1
    budget.total_uncompressed_bytes += actual


def _is_zip_candidate(path: Path) -> bool:
    if path.suffix.casefold() == ".zip":
        return True
    try:
        with path.open("rb") as stream:
            return stream.read(4) in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x06"}
    except OSError as exc:
        raise ZipIntakeError("blocked", "nested_candidate_unavailable", str(exc)) from exc


def _extract_zip_tree(
    archive_path: Path,
    destination: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    budget: _ExtractionBudget,
    depth: int,
) -> int:
    deadline.check()
    if depth > limits.depth_limit:
        raise ZipIntakeError("budget", "nested_depth_budget")
    preflight = _preflight_zip(archive_path, limits=limits, deadline=deadline)
    if preflight.total_uncompressed_bytes + budget.total_uncompressed_bytes > limits.max_total_uncompressed_bytes:
        raise ZipIntakeError("budget", "nested_total_uncompressed_budget")
    _create_directory(destination)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in preflight.members:
                deadline.check()
                target = destination.joinpath(*member.relative_name.split("/"))
                if member.is_directory:
                    _create_directory(target)
                    continue
                _create_directory(target.parent)
                _stream_member(
                    archive,
                    member,
                    target,
                    limits=limits,
                    deadline=deadline,
                    budget=budget,
                    disk_root=destination,
                )
    except ZipIntakeError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "zip_extraction_failed", str(exc)) from exc
    # Generic nested archives are expanded in-place before publication.  A
    # nested atomic package is left as the regular file for normal routes.
    for child in sorted(destination.rglob("*"), key=lambda item: (len(item.parts), os.fsencode(os.fspath(item)))):
        deadline.check()
        if not child.is_file() or child.is_symlink() or not _is_zip_candidate(child):
            continue
        nested = _classify_zip(child, limits=limits, deadline=deadline)
        if nested.kind == "invalid":
            nested_status = nested.status
            if nested_status not in {
                "corrupt",
                "unsafe",
                "ambiguous",
                "password",
                "budget",
                "timeout",
                "blocked",
            }:
                nested_status = "corrupt"
            raise ZipIntakeError(
                cast(IntakeStatus, nested_status),
                "nested_zip_invalid",
                nested.detail,
            )
        if nested.kind != "generic_zip":
            continue
        if depth >= limits.depth_limit:
            raise ZipIntakeError("budget", "nested_depth_budget")
        nested_destination = child.with_name(child.name[:-4] if child.suffix.casefold() == ".zip" else child.name + ".extracted")
        if os.path.lexists(nested_destination):
            raise ZipIntakeError("collision", "nested_destination_collision", os.fspath(nested_destination))
        _extract_zip_tree(
            child,
            nested_destination,
            limits=limits,
            deadline=deadline,
            budget=budget,
            depth=depth + 1,
        )
        try:
            child.unlink()
        except OSError as exc:
            raise ZipIntakeError("blocked", "nested_source_cleanup_failed", str(exc)) from exc
    _verify_tree(destination, limits=limits, deadline=deadline)
    return budget.members


def _verify_tree(root: Path, *, limits: ZipIntakeLimits, deadline: _Deadline) -> None:
    _assert_real_directory(root, label="staged tree")
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        deadline.check()
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o777 != 0o700:
                raise ZipIntakeError("unsafe", "staged_directory_permissions", os.fspath(path))
        for name in files:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ZipIntakeError("unsafe", "staged_special_or_link", os.fspath(path))
            if metadata.st_mode & 0o777 != 0o600 or metadata.st_mode & 0o111:
                raise ZipIntakeError("unsafe", "staged_file_permissions", os.fspath(path))
            total += int(metadata.st_size)
            if total > limits.max_total_uncompressed_bytes:
                raise ZipIntakeError("budget", "staged_tree_budget")


def _normalize_trash_result(value: object) -> TrashDisposition:
    if isinstance(value, TrashDisposition):
        return value
    if isinstance(value, bool):
        return TrashDisposition("applied" if value else "recovery_required")
    status = getattr(value, "status", None)
    status_value = getattr(status, "value", None)
    if status_value is not None:
        status = status_value
    if status is None and isinstance(value, Mapping):
        status = value.get("status")
    if status in {"applied", "blocked", "recovery_required"}:
        detail = getattr(value, "detail", None)
        evidence = getattr(value, "evidence", None)
        if isinstance(value, Mapping):
            detail = value.get("detail", detail)
            evidence = value.get("evidence", evidence)
        return TrashDisposition(status, None if detail is None else str(detail), None if evidence is None else str(evidence))
    raise ZipIntakeError("recovery_required", "trash_result_invalid")


def _stage_factory(value: StageFactory | str | os.PathLike[str] | None) -> StageFactory | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        return ScratchStageFactory(value)
    if not callable(getattr(value, "create", None)):
        raise TypeError("staging must implement create()")
    return value


def _publisher(value: PublishHook | None) -> PublishHook:
    return FilesystemPublishHook() if value is None else value


def run_zip_intake(
    source: str | os.PathLike[str] | None = None,
    *,
    apply: bool = False,
    destination: str | os.PathLike[str] | None = None,
    max_file_bytes: int | None = None,
    limits: ZipIntakeLimits | None = None,
    staging: StageFactory | str | os.PathLike[str] | None = None,
    scratch_root: str | os.PathLike[str] | None = None,
    publisher: PublishHook | None = None,
    trash: TrashHook | Callable[[Path, SourceIdentity], object] | None = None,
    deadline: float | None = None,
    cancellation: object | None = None,
    cancellation_token: object | None = None,
    cancel: object | None = None,
    root: Path | None = None,
    snapshots: Iterable[object] | None = None,
    config: object | None = None,
    state: object | None = None,
    run_id: int | None = None,
    progress: object | None = None,
) -> ZipIntakeOutcome | dict[str, object]:
    """Plan or physically intake one ZIP, always failing closed.

    ``apply=False`` performs no staging, publication, or Trash effect.  Apply
    requires a caller-owned Trash hook because removing the source is a KIO
    effect, never an ``unlink`` fallback.  A source over ``max_file_bytes`` is
    rejected before opening or classifying it.
    """

    if source is None and root is not None:
        return _run_framework_batch(
            root=root,
            snapshots=snapshots,
            config=config,
            apply=apply,
            max_file_bytes=max_file_bytes,
            limits=limits,
            staging=staging if staging is not None else scratch_root,
            publisher=publisher,
            trash=trash,
            deadline=deadline,
            cancellation=cancellation or cancellation_token or cancel,
            state=state,
            run_id=run_id,
            progress=progress,
        )
    if source is None:
        raise TypeError("run_zip_intake requires source or root")
    effective_limits = ZipIntakeLimits() if limits is None else limits
    effective_limits.validate()
    path = Path(source)
    source_text = os.fspath(path)
    try:
        identity = SourceIdentity.capture(path)
    except ZipIntakeError as exc:
        classification = ZipIntakeClassification("invalid", exc.status, "invalid", detail=exc.detail)
        return ZipIntakeOutcome(exc.status, source_text, None, classification, apply=apply, reason=exc.reason, detail=exc.detail)
    if identity.size > effective_limits.max_input_bytes:
        classification = ZipIntakeClassification("invalid", "budget", "invalid", detail="source exceeds max_input_bytes")
        return ZipIntakeOutcome("budget", source_text, identity, classification, apply=apply, reason="max_input_bytes", detail=classification.detail)
    if max_file_bytes is not None:
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 0:
            raise ValueError("max_file_bytes must be a non-negative integer or None")
        if identity.size > max_file_bytes:
            classification = ZipIntakeClassification("invalid", "skipped_by_size", "invalid", detail="source exceeds global size admission")
            return ZipIntakeOutcome("skipped_by_size", source_text, identity, classification, apply=apply, reason="max_file_bytes", detail=classification.detail)
    try:
        deadline_obj = _deadline_for(
            effective_limits,
            deadline,
            cancellation or cancellation_token or cancel,
        )
        classification = _classify_zip(path, limits=effective_limits, deadline=deadline_obj)
    except ZipIntakeError as exc:
        classification = ZipIntakeClassification("invalid", exc.status, "invalid", detail=exc.detail)
    if classification.kind == "invalid":
        status_value = classification.status
        if status_value not in {
            "corrupt",
            "unsafe",
            "ambiguous",
            "password",
            "budget",
            "timeout",
            "blocked",
            "dependency",
        }:
            status_value = "corrupt"
        status = cast(IntakeStatus, status_value)
        return ZipIntakeOutcome(status, source_text, identity, classification, apply=apply, reason=classification.status, detail=classification.detail)
    if classification.kind == "atomic_package":
        return ZipIntakeOutcome("atomic", source_text, identity, classification, apply=apply, members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, reason="atomic_package", detail="preserved as a functional package")
    try:
        source_digest = _source_sha256(path, deadline=deadline_obj)
    except ZipIntakeError as exc:
        return ZipIntakeOutcome(exc.status, source_text, identity, classification, apply=apply, reason=exc.reason, detail=exc.detail)
    destination_path = _validate_destination(Path(destination) if destination is not None else _default_destination(path))
    if not apply:
        return ZipIntakeOutcome("planned", source_text, identity, classification, apply=False, destination=os.fspath(destination_path), members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, reason="generic_zip", source_sha256=source_digest)
    prior_identity = _COMPLETED_DESTINATIONS.get(os.fspath(destination_path))
    if prior_identity is not None and _same_physical_identity(prior_identity, identity) and os.path.isdir(destination_path):
        return ZipIntakeOutcome("already_applied", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, published=False, trashed=False, successor_paths=(os.fspath(destination_path),), reason="replay_proven", source_sha256=source_digest)
    if trash is None:
        return ZipIntakeOutcome("dependency", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, reason="kio_trash_hook_required", detail="apply requires a verified KIO Trash adapter", source_sha256=source_digest)
    stage_factory = _stage_factory(staging if staging is not None else scratch_root)
    if stage_factory is None:
        return ZipIntakeOutcome("dependency", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), reason="staging_provider_required", detail="apply requires registered private staging", source_sha256=source_digest)
    lease: StageLease | None = None
    publish_receipt: PublishReceipt | None = None
    budget = _ExtractionBudget(started=time.monotonic())
    try:
        lease = stage_factory.create(source=path, metadata={"owner": "archive-intake", "source": source_text, "classification": classification.to_dict()})
        _private_directory(lease.path, label="workspace", create=False)
        payload_root = lease.path / "payload"
        _create_directory(payload_root)
        destination_name = destination_path.name
        staged_destination = payload_root / destination_name
        _check_disk(lease.path, classification.estimated_uncompressed_bytes, limits=effective_limits)
        _extract_zip_tree(path, staged_destination, limits=effective_limits, deadline=deadline_obj, budget=budget, depth=0)
        _verify_tree(staged_destination, limits=effective_limits, deadline=deadline_obj)
        if not identity.matches(path):
            raise ZipIntakeError("source_changed", "source_changed_before_publish")
        _assert_destination_parent(destination_path)
        if os.path.lexists(destination_path):
            raise ZipIntakeError("collision", "destination_collision")
        publish_receipt = _publisher(publisher).publish(staged_destination, destination_path)
        if not isinstance(publish_receipt, PublishReceipt):
            raise ZipIntakeError("recovery_required", "publish_receipt_invalid")
        if not identity.matches(path):
            rollback_ok = _publisher(publisher).rollback(publish_receipt)
            if not rollback_ok:
                raise ZipIntakeError("recovery_required", "source_changed_after_publish")
            raise ZipIntakeError("source_changed", "source_changed_after_publish")
        trash_callable = getattr(trash, "trash", None)
        if not callable(trash_callable):
            if not callable(trash):
                raise ZipIntakeError("dependency", "trash_hook_invalid")
            trash_callable = trash
        try:
            trash_result = _normalize_trash_result(trash_callable(path, identity))
        except ZipIntakeError:
            raise
        except BaseException as exc:
            # A KIO adapter may fail before or after its own physical
            # frontier.  Roll back only through the publisher seam; never
            # unlink the source or published tree here.
            rollback_ok = _publisher(publisher).rollback(publish_receipt)
            if not rollback_ok:
                raise ZipIntakeError(
                    "recovery_required",
                    "trash_failed_publication_rollback_failed",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            raise ZipIntakeError(
                "blocked",
                "trash_hook_failed",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        if trash_result.status != "applied":
            rollback_ok = _publisher(publisher).rollback(publish_receipt)
            if not rollback_ok:
                raise ZipIntakeError("recovery_required", "trash_failed_publication_rollback_failed", trash_result.detail)
            trash_status: IntakeStatus = (
                "recovery_required"
                if trash_result.status == "recovery_required"
                else "blocked"
            )
            raise ZipIntakeError(trash_status, "trash_not_applied", trash_result.detail)
        if os.path.lexists(path):
            raise ZipIntakeError("recovery_required", "trash_claim_unverified")
        _COMPLETED_DESTINATIONS[os.fspath(destination_path)] = identity
        lease.complete()
        return ZipIntakeOutcome("applied", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=budget.members, uncompressed_bytes=budget.total_uncompressed_bytes, published=True, trashed=True, successor_paths=(os.fspath(destination_path),), reason="generic_zip_published", detail=trash_result.evidence, source_sha256=source_digest)
    except ZipIntakeError as exc:
        if lease is not None:
            try:
                lease.fail(exc.detail)
            except BaseException:
                pass
        return ZipIntakeOutcome(exc.status, source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=budget.members, uncompressed_bytes=budget.total_uncompressed_bytes, published=publish_receipt is not None and exc.status == "recovery_required", reason=exc.reason, detail=exc.detail, source_sha256=source_digest)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        if lease is not None:
            try:
                lease.fail(f"{type(exc).__name__}: {exc}")
            except BaseException:
                pass
        return ZipIntakeOutcome("dependency", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=budget.members, uncompressed_bytes=budget.total_uncompressed_bytes, reason="intake_failed", detail=f"{type(exc).__name__}: {exc}", source_sha256=source_digest)


class _EphemeralStageFactory:
    """Private sibling staging used only by the direct fixture API.

    Production orchestration passes ``scratch_root`` and therefore uses the
    registered ScratchManager adapter.  The direct API has no state directory
    to bind, so it creates one private, explicitly owned sibling and removes
    that root after each operation.
    """

    def __init__(self, parent: Path):
        self.root = Path(tempfile.mkdtemp(prefix=".neocortex-zip-intake-", dir=os.fspath(parent)))
        os.chmod(self.root, 0o700)

    def create(self, *, source: Path, metadata: Mapping[str, object]) -> StageLease:
        return FilesystemStageFactory(self.root).create(source=source, metadata=metadata)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def intake_zip(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None = None,
    *,
    apply: bool = False,
    limits: ZipIntakeLimits | None = None,
    max_file_bytes: int | None = None,
    global_max_file_bytes: int | None = None,
    cancellation: object | None = None,
    cancellation_token: object | None = None,
    cancel: object | None = None,
    trash: TrashHook | Callable[[Path, SourceIdentity], object] | None = None,
    trash_backend: object | None = None,
    trash_service: object | None = None,
    kio_trash: object | None = None,
    staging_root: str | os.PathLike[str] | None = None,
    scratch_root: str | os.PathLike[str] | None = None,
    publisher: PublishHook | None = None,
    deadline: float | None = None,
) -> ZipIntakeOutcome:
    """Compatibility/direct boundary used by E2E tests and small callers.

    It accepts the canonical KIO names used by the runtime while retaining one
    engine implementation.  If no scratch root is supplied, the temporary
    private root is a child of the source directory and is always removed.
    """

    effective_trash = cast(
        TrashHook | Callable[[Path, SourceIdentity], object] | None,
        trash or trash_backend or trash_service or kio_trash,
    )
    token = cancellation or cancellation_token or cancel
    if token is not None and bool(getattr(token, "is_cancelled", False)):
        identity: SourceIdentity | None
        try:
            identity = SourceIdentity.capture(Path(source))
        except ZipIntakeError:
            identity = None
        classification = ZipIntakeClassification("invalid", "blocked", "invalid", detail="cancellation requested")
        return ZipIntakeOutcome("blocked", os.fspath(source), identity, classification, apply=apply, reason="cancelled", detail="ZIP intake cancellation requested")
    ephemeral: _EphemeralStageFactory | None = None
    effective_staging: StageFactory | str | os.PathLike[str] | None
    if staging_root is not None or scratch_root is not None:
        effective_staging = staging_root if staging_root is not None else scratch_root
    elif apply:
        source_parent = Path(source).absolute().parent
        ephemeral = _EphemeralStageFactory(source_parent)
        effective_staging = ephemeral
    else:
        effective_staging = None
    try:
        value = run_zip_intake(
            source,
            apply=apply,
            destination=destination,
            max_file_bytes=max_file_bytes if max_file_bytes is not None else global_max_file_bytes,
            limits=limits,
            staging=effective_staging,
            publisher=publisher,
            trash=effective_trash,
            deadline=deadline,
            cancellation=token,
        )
        if not isinstance(value, ZipIntakeOutcome):
            raise TypeError("run_zip_intake returned a batch result for one source")
        return value
    finally:
        if ephemeral is not None:
            ephemeral.cleanup()


def _run_framework_batch(
    *,
    root: Path,
    snapshots: Iterable[object] | None,
    config: object | None,
    apply: bool,
    max_file_bytes: int | None,
    limits: ZipIntakeLimits | None,
    staging: StageFactory | str | os.PathLike[str] | None,
    publisher: PublishHook | None,
    trash: TrashHook | Callable[[Path, SourceIdentity], object] | None,
    deadline: float | None,
    cancellation: object | None,
    state: object | None,
    run_id: int | None,
    progress: object | None,
) -> dict[str, object]:
    """Adapt the framework batch call without importing its state owner."""

    del config, state, run_id, progress
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    values = tuple(snapshots or ())
    totals = {"total_files": len(values), "eligible_files": 0, "size_skipped_files": 0, "size_skipped_bytes": 0}
    outcomes: list[dict[str, object]] = []
    for snapshot in values:
        token = cancellation
        if token is not None:
            checkpoint = getattr(token, "checkpoint", None)
            if callable(checkpoint):
                try:
                    checkpoint()
                except BaseException:
                    break
        snapshot_path = getattr(snapshot, "path", None)
        if snapshot_path is None:
            continue
        snapshot_size = getattr(snapshot, "size", None)
        if max_file_bytes is not None and isinstance(snapshot_size, int) and snapshot_size > max_file_bytes:
            totals["size_skipped_files"] = int(totals["size_skipped_files"]) + 1
            totals["size_skipped_bytes"] = int(totals["size_skipped_bytes"]) + int(snapshot_size)
            continue
        totals["eligible_files"] = int(totals["eligible_files"]) + 1
        value = run_zip_intake(
            Path(snapshot_path),
            apply=apply,
            max_file_bytes=max_file_bytes,
            limits=limits,
            staging=staging,
            publisher=publisher,
            trash=trash,
            deadline=deadline,
            cancellation=cancellation,
        )
        payload = value.to_dict() if isinstance(value, ZipIntakeOutcome) else dict(value)
        outcomes.append(payload)
    changed = any(bool(item.get("published")) or bool(item.get("trashed")) for item in outcomes)
    return {
        "schema": "neocortex.zip-intake/v1",
        "status": "applied" if apply and changed else ("planned" if not apply else "completed"),
        "apply": bool(apply),
        "filesystem_changed": changed,
        "reconciliation_required": changed,
        "total_files": totals["total_files"],
        "eligible_files": totals["eligible_files"],
        "size_skipped_files": totals["size_skipped_files"],
        "size_skipped_bytes": totals["size_skipped_bytes"],
        "outcomes": outcomes,
    }


def plan_zip_intake(
    source: str | os.PathLike[str],
    *,
    max_file_bytes: int | None = None,
    limits: ZipIntakeLimits | None = None,
    destination: str | os.PathLike[str] | None = None,
    deadline: float | None = None,
) -> ZipIntakeOutcome:
    """Read-only alias for the dry-run ZIP intake plan."""

    value = run_zip_intake(
        source,
        apply=False,
        destination=destination,
        max_file_bytes=max_file_bytes,
        limits=limits,
        deadline=deadline,
    )
    if not isinstance(value, ZipIntakeOutcome):
        raise TypeError("plan_zip_intake returned a framework batch result")
    return value


# The old route called its bounded value object ``ArchiveMaterializationLimits``;
# this descriptive alias is kept only at the new physical boundary, not as a
# second policy implementation.
ArchiveIntakeLimits = ZipIntakeLimits


__all__ = (
    "ArchiveIntakeLimits",
    "FilesystemPublishHook",
    "FilesystemStageFactory",
    "IntakeKind",
    "PublishHook",
    "PublishReceipt",
    "ScratchStageFactory",
    "SourceIdentity",
    "StageFactory",
    "StageLease",
    "TrashDisposition",
    "TrashHook",
    "ZipIntakeClassification",
    "ZipIntakeError",
    "ZipIntakeLimits",
    "ZipIntakeOutcome",
    "classify_zip",
    "intake_zip",
    "plan_zip_intake",
    "run_zip_intake",
)
