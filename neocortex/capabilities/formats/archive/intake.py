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
import uuid
import zipfile
import zlib
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
from neocortex.progress import ProgressEvent, ProgressMetric, emit_progress

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
    mime: str | None = None

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
            "mime": self.mime,
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


@dataclass(slots=True)
class PublishReceipt:
    """Evidence returned by a publisher after a no-replace publication."""

    destination: Path
    staged_path: Path
    published: bool = True
    destination_identity: tuple[int, ...] | None = None
    destination_parent_fd: int | None = None
    destination_parent_identity: tuple[int, int] | None = None
    staged_parent_fd: int | None = None
    staged_parent_identity: tuple[int, int] | None = None

    @property
    def destination_name(self) -> str:
        return self.destination.name

    @property
    def staged_name(self) -> str:
        return self.staged_path.name

    def close(self) -> None:
        for field_name in ("destination_parent_fd", "staged_parent_fd"):
            descriptor = getattr(self, field_name)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, field_name, None)


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
        descriptor = _open_private_directory(self.root, label="staging root", create=False)
        try:
            metadata = os.fstat(descriptor)
            self._root_identity = (int(metadata.st_dev), int(metadata.st_ino))
        finally:
            os.close(descriptor)

    def create(self, *, source: Path, metadata: Mapping[str, object]) -> StageLease:
        del source, metadata
        parent_fd = _open_private_directory(self.root, label="staging root", create=False)
        try:
            parent_metadata = os.fstat(parent_fd)
            if (int(parent_metadata.st_dev), int(parent_metadata.st_ino)) != self._root_identity:
                raise ZipIntakeError("dependency", "staging_root_identity_changed")
            for _attempt in range(16):
                name = f".zip-intake-{uuid.uuid4().hex}"
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                child_fd = -1
                try:
                    child_fd = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                    child_metadata = os.fstat(child_fd)
                    if not stat.S_ISDIR(child_metadata.st_mode) or child_metadata.st_mode & 0o077:
                        raise ZipIntakeError("dependency", "staging_workspace_not_private")
                except BaseException:
                    shutil.rmtree(name, dir_fd=parent_fd, ignore_errors=True)
                    raise
                finally:
                    if child_fd >= 0:
                        os.close(child_fd)
                path = self.root / name
                return _FilesystemStageLease(path, parent_fd, name)
            raise ZipIntakeError("dependency", "staging_workspace_name_collision")
        except BaseException:
            os.close(parent_fd)
            raise


class _FilesystemStageLease:
    def __init__(self, path: Path, parent_fd: int, name: str):
        self.path = path
        self._parent_fd = parent_fd
        self._name = name
        self._closed = False

    def complete(self) -> None:
        if not self._closed:
            try:
                shutil.rmtree(self._name, ignore_errors=False, dir_fd=self._parent_fd)
            finally:
                os.close(self._parent_fd)
                self._closed = True

    def fail(self, reason: str) -> None:
        del reason
        if not self._closed:
            try:
                shutil.rmtree(self._name, ignore_errors=True, dir_fd=self._parent_fd)
            finally:
                os.close(self._parent_fd)
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
        destination_parent_expected = _assert_destination_parent(destination)
        staged_parent_fd, staged_parent_identity = _open_directory_path(
            staged_path.parent,
            label="staged publication parent",
        )
        destination_parent_fd, destination_parent_identity = _open_directory_path(
            destination.parent,
            label="destination parent",
            expected_identity=destination_parent_expected,
        )
        try:
            staged_metadata = os.stat(
                staged_path.name,
                dir_fd=staged_parent_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(staged_metadata.st_mode):
                raise ZipIntakeError("unsafe", "staged_publication_not_directory")
            try:
                os.stat(destination.name, dir_fd=destination_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ZipIntakeError("collision", "destination_collision", "destination already exists")
            _rename_directory_noreplace_fds(
                staged_parent_fd,
                staged_path.name,
                destination_parent_fd,
                destination.name,
            )
            try:
                current_destination_parent = destination.parent.lstat()
                parent_unchanged = (
                    int(current_destination_parent.st_dev),
                    int(current_destination_parent.st_ino),
                ) == destination_parent_identity
            except OSError:
                parent_unchanged = False
            if not parent_unchanged:
                _rename_directory_noreplace_fds(
                    destination_parent_fd,
                    destination.name,
                    staged_parent_fd,
                    staged_path.name,
                )
                os.fsync(destination_parent_fd)
                os.fsync(staged_parent_fd)
                raise ZipIntakeError("source_changed", "destination_parent_changed_after_publish")
            published_metadata = os.stat(
                destination.name,
                dir_fd=destination_parent_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(published_metadata.st_mode):
                raise ZipIntakeError("recovery_required", "published_destination_not_directory")
            os.fsync(destination_parent_fd)
            return PublishReceipt(
                destination=destination,
                staged_path=staged_path,
                destination_identity=_directory_identity(published_metadata),
                destination_parent_fd=destination_parent_fd,
                destination_parent_identity=destination_parent_identity,
                staged_parent_fd=staged_parent_fd,
                staged_parent_identity=staged_parent_identity,
            )
        except BaseException:
            os.close(staged_parent_fd)
            os.close(destination_parent_fd)
            raise

    def rollback(self, receipt: PublishReceipt) -> bool:
        try:
            if not receipt.published:
                return True
            if (
                receipt.destination_parent_fd is None
                or receipt.staged_parent_fd is None
                or receipt.destination_parent_identity is None
                or receipt.staged_parent_identity is None
                or receipt.destination_identity is None
            ):
                return False
            if not _fd_identity_matches(
                receipt.destination_parent_fd,
                receipt.destination_parent_identity,
            ) or not _fd_identity_matches(
                receipt.staged_parent_fd,
                receipt.staged_parent_identity,
            ):
                return False
            destination_metadata = os.stat(
                receipt.destination_name,
                dir_fd=receipt.destination_parent_fd,
                follow_symlinks=False,
            )
            if _directory_identity(destination_metadata) != receipt.destination_identity:
                return False
            try:
                os.stat(
                    receipt.staged_name,
                    dir_fd=receipt.staged_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return False
            _rename_directory_noreplace_fds(
                receipt.destination_parent_fd,
                receipt.destination_name,
                receipt.staged_parent_fd,
                receipt.staged_name,
            )
            os.fsync(receipt.destination_parent_fd)
            os.fsync(receipt.staged_parent_fd)
            return True
        except (OSError, ZipIntakeError):
            return False
        finally:
            receipt.close()


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


@dataclass(frozen=True, slots=True)
class ZipDecision:
    """One bounded ZIP decision tied to the physical source identity.

    A decision is intentionally process-local.  It may be handed from the
    inventory/intake discovery pass to the apply pass, but it is never valid
    merely because the path string is unchanged: the complete
    :class:`SourceIdentity` must still match.  ``_preflight`` is an internal
    reuse seam that avoids rebuilding the central-directory decision before a
    generic ZIP is extracted; it is not serialized in the public payload.
    """

    identity: SourceIdentity
    classification: ZipIntakeClassification
    _preflight: _Preflight | None = field(default=None, repr=False, compare=False)

    def matches(self, path: Path, identity: SourceIdentity | None = None) -> bool:
        """Return whether this decision is safe to reuse for ``path``."""

        if os.fspath(path) != self.identity.path:
            return False
        current = self.identity if identity is None else identity
        return _same_physical_identity(self.identity, current)


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


_PRIVATE_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)


def _private_path_components(path: Path, *, label: str) -> tuple[str, ...]:
    if not path.is_absolute():
        raise ZipIntakeError("dependency", f"{label}_not_absolute")
    if "\x00" in os.fspath(path):
        raise ZipIntakeError("dependency", f"{label}_contains_nul")
    if ".." in path.parts:
        raise ZipIntakeError("dependency", f"{label}_contains_parent")
    return path.parts


def _open_private_directory(path: Path, *, label: str, create: bool) -> int:
    """Open/create a private directory through descriptor-anchored components.

    Every component is opened with ``O_DIRECTORY|O_NOFOLLOW`` and new
    components are created with ``mkdir(..., dir_fd=...)``.  A path swap after
    an ancestor is opened therefore cannot redirect creation through a symlink;
    the identity comparison also rejects a path that changed while walking.
    """

    components = _private_path_components(path, label=label)
    owned: list[int] = []
    created: list[tuple[int, str]] = []

    def cleanup_created() -> None:
        for parent_fd, name in reversed(created):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass

    try:
        root_fd = os.open(os.sep, _PRIVATE_DIRECTORY_FLAGS)
        owned.append(root_fd)
        for component in components[1:]:
            parent_fd = owned[-1]
            try:
                child_fd = os.open(
                    component,
                    _PRIVATE_DIRECTORY_FLAGS,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError as exc:
                if not create:
                    raise ZipIntakeError(
                        "dependency", f"{label}_unavailable", str(exc)
                    ) from exc
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    created.append((parent_fd, component))
                except FileExistsError:
                    pass
                child_fd = os.open(
                    component,
                    _PRIVATE_DIRECTORY_FLAGS,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise ZipIntakeError("dependency", f"{label}_unavailable", str(exc)) from exc
            owned.append(child_fd)

        metadata = os.fstat(owned[-1])
        if not stat.S_ISDIR(metadata.st_mode):
            raise ZipIntakeError("dependency", f"{label}_not_directory")
        if metadata.st_mode & 0o077:
            raise ZipIntakeError("dependency", f"{label}_not_private")

        # Ensure the lexical path still names the descriptors we opened.  This
        # catches a controlled ancestor replacement after the anchored mkdir;
        # cleanup remains anchored to the original descriptors.
        current = Path(components[0])
        for index, _component in enumerate(components[1:], start=1):
            current /= _component
            observed = current.lstat()
            expected = os.fstat(owned[index])
            if (
                (int(observed.st_dev), int(observed.st_ino), int(observed.st_mode))
                != (int(expected.st_dev), int(expected.st_ino), int(expected.st_mode))
            ):
                raise ZipIntakeError("dependency", f"{label}_identity_changed")

        result = owned[-1]
        for descriptor in owned[:-1]:
            os.close(descriptor)
        owned.clear()
        return result
    except ZipIntakeError:
        cleanup_created()
        raise
    except OSError as exc:
        cleanup_created()
        raise ZipIntakeError("dependency", f"{label}_unavailable", str(exc)) from exc
    finally:
        for descriptor in owned:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _private_directory(path: Path, *, label: str, create: bool) -> Path:
    try:
        descriptor = _open_private_directory(path, label=label, create=create)
    except ZipIntakeError:
        raise
    try:
        return path
    finally:
        os.close(descriptor)


def _open_directory_path(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    """Open an absolute directory path without following any ancestor link."""

    components = _private_path_components(path, label=label)
    owned: list[int] = []
    try:
        owned.append(os.open(os.sep, _PRIVATE_DIRECTORY_FLAGS))
        for component in components[1:]:
            try:
                child = os.open(
                    component,
                    _PRIVATE_DIRECTORY_FLAGS,
                    dir_fd=owned[-1],
                )
            except OSError as exc:
                raise ZipIntakeError("dependency", f"{label}_unavailable", str(exc)) from exc
            owned.append(child)
        for index, _component in enumerate(components[1:], start=1):
            # Compare the path-bound object to the descriptor we opened.  A
            # real-directory replacement is rejected just like a symlink.
            current = Path(components[0])
            for item in components[1 : index + 1]:
                current /= item
            observed = current.lstat()
            expected = os.fstat(owned[index])
            if (int(observed.st_dev), int(observed.st_ino), int(observed.st_mode)) != (
                int(expected.st_dev), int(expected.st_ino), int(expected.st_mode)
            ):
                raise ZipIntakeError("dependency", f"{label}_identity_changed")
        result = owned[-1]
        for descriptor in owned[:-1]:
            os.close(descriptor)
        owned.clear()
        metadata = os.fstat(result)
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        if expected_identity is not None and identity != expected_identity:
            os.close(result)
            raise ZipIntakeError("source_changed", f"{label}_identity_changed")
        return result, identity
    except ZipIntakeError:
        raise
    except OSError as exc:
        raise ZipIntakeError("dependency", f"{label}_unavailable", str(exc)) from exc
    finally:
        for descriptor in owned:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(metadata.st_nlink),
        int(metadata.st_mode),
    )


def _open_directory_child(parent_fd: int, name: str, *, create: bool) -> int:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ZipIntakeError("unsafe", "stage_directory_name_invalid", name)
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(
            name,
            _PRIVATE_DIRECTORY_FLAGS,
            dir_fd=parent_fd,
        )
    except FileExistsError as exc:
        raise ZipIntakeError("collision", "stage_directory_collision", name) from exc
    except OSError as exc:
        raise ZipIntakeError("unsafe", "stage_directory_unavailable", str(exc)) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ZipIntakeError("unsafe", "stage_directory_not_directory", name)
        if metadata.st_mode & 0o077:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ZipIntakeError("dependency", f"{label}_missing", str(exc)) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ZipIntakeError("unsafe", f"{label}_not_directory")


def _assert_destination_parent(destination: Path) -> tuple[int, int]:
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
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ZipIntakeError("blocked", "destination_parent_missing", str(exc)) from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ZipIntakeError("blocked", "destination_parent_missing")
    return int(parent_metadata.st_dev), int(parent_metadata.st_ino)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fd_identity_matches(descriptor: int, expected: tuple[int, int]) -> bool:
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        return False
    return (int(metadata.st_dev), int(metadata.st_ino)) == expected


def _rename_directory_noreplace_fds(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Rename using already-pinned parent descriptors; never reopen by path."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ZipIntakeError("dependency", "rename_noreplace_unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_parent_fd,
            os.fsencode(source_name),
            destination_parent_fd,
            os.fsencode(destination_name),
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


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Compatibility seam that pins both parents before the rename."""

    source_parent_fd, _source_identity = _open_directory_path(
        source.parent,
        label="staged publication parent",
    )
    destination_parent_fd, _destination_identity = _open_directory_path(
        destination.parent,
        label="destination parent",
    )
    try:
        _rename_directory_noreplace_fds(
            source_parent_fd,
            source.name,
            destination_parent_fd,
            destination.name,
        )
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)


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


def _preflight_from_archive(
    archive: zipfile.ZipFile,
    structure: ZipStructure,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    progress: "_ZipProgress | None" = None,
) -> _Preflight:
    """Validate one already-open archive and retain its reusable members."""

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
        if progress is not None:
            progress.tick("preflight", 1, total=len(infos))
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
        # A file cannot also be a parent directory and a directory cannot be
        # repeated under a different spelling.
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


def _preflight_zip(
    path: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    progress: "_ZipProgress | None" = None,
) -> _Preflight:
    deadline.check()
    try:
        structure = inspect_zip_structure(
            path,
            max_members=limits.max_members,
            max_central_directory_bytes=limits.max_central_directory_bytes,
        )
        with zipfile.ZipFile(path, "r") as archive:
            return _preflight_from_archive(
                archive,
                structure,
                limits=limits,
                deadline=deadline,
                progress=progress,
            )
    except ZipIntakeError:
        raise
    except PermissionError as exc:
        raise ZipIntakeError("blocked", "zip_permission_denied", str(exc)) from exc
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "zip_structure_invalid", f"{type(exc).__name__}: {exc}") from exc


class _ZipProgress:
    """Bounded progress adapter for one source ZIP.

    The engine may check cancellation at chunk granularity, but it must not
    turn every 64 KiB read into a terminal/UI event.  Updates are coalesced by
    work units or a short wall-clock interval and a terminal event is emitted
    only once for each phase.
    """

    def __init__(self, callback: object | None, *, source: Path) -> None:
        self._callback: Callable[[ProgressEvent], None] | None = (
            cast(Callable[[ProgressEvent], None], callback) if callable(callback) else None
        )
        self._source = source
        self._counts: dict[str, int] = {}
        self._last_emitted: dict[str, int] = {}
        self._last_at: dict[str, float] = {}
        self._finished: set[str] = set()

    def tick(
        self,
        phase: str,
        increment: int = 1,
        *,
        total: int | None = None,
        metrics: tuple[ProgressMetric, ...] = (),
        force: bool = False,
    ) -> None:
        if self._callback is None or phase in self._finished:
            return
        count = self._counts.get(phase, 0) + max(0, int(increment))
        self._counts[phase] = count
        now = time.monotonic()
        previous = self._last_emitted.get(phase, -1)
        last_at = self._last_at.get(phase, now)
        if not force and previous >= 0 and count - previous < 32 and now - last_at < 0.25:
            return
        self._last_emitted[phase] = count
        self._last_at[phase] = now
        emit_progress(
            self._callback,
            ProgressEvent(
                operation="zip-intake",
                phase=phase,
                description="Procesando ZIP",
                completed=count,
                total=total,
                unit="elementos",
                finished=force,
                metrics=metrics,
            ),
        )
        if force:
            self._finished.add(phase)

    def finish(
        self,
        phase: str,
        *,
        metrics: tuple[ProgressMetric, ...] = (),
    ) -> None:
        self.tick(phase, 0, metrics=metrics, force=True)


def _read_marker(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limit: int,
    deadline: _Deadline,
    progress: _ZipProgress | None = None,
) -> bytes:
    if int(info.flag_bits) & 0x1:
        raise ZipIntakeError("password", "encrypted_marker", info.filename[:512])
    if int(info.file_size) > limit:
        raise ZipIntakeError("budget", "marker_size_budget", info.filename[:512])
    try:
        with archive.open(info, "r") as stream:
            payload = bytearray()
            while True:
                deadline.check()
                chunk = stream.read(min(CHUNK_BYTES, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if progress is not None:
                    progress.tick("classification", len(chunk))
                if len(payload) > limit:
                    raise ZipIntakeError("budget", "marker_size_budget", info.filename[:512])
            # Reading through EOF makes zipfile perform CRC validation.
            return bytes(payload)
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


def _decide_zip(
    path: Path,
    *,
    identity: SourceIdentity,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    progress: _ZipProgress | None = None,
) -> ZipDecision:
    """Make one bounded decision and retain its preflight for apply mode."""

    try:
        deadline.check()
        structure = inspect_zip_structure(
            path,
            max_members=limits.max_members,
            max_central_directory_bytes=limits.max_central_directory_bytes,
        )
        with zipfile.ZipFile(path, "r") as archive:
            # The central directory and member policy are validated in the
            # same open handle used for marker reads.  Generic apply can then
            # reuse this preflight instead of inspecting the source again.
            preflight = _preflight_from_archive(
                archive,
                structure,
                limits=limits,
                deadline=deadline,
                progress=progress,
            )
            members = preflight.members
            by_name = {member.relative_name: member.info for member in members}
            normalized = tuple(member.relative_name for member in members)
            total = preflight.total_uncompressed_bytes
            member_count = len(members)
            deadline.check()
            evidence: list[str] = []
            content_types = by_name.get("[Content_Types].xml")
            required_office: tuple[str, str] | None = None
            if content_types is not None:
                _parse_xml_marker(
                    _read_marker(
                        archive,
                        content_types,
                        limit=MAX_MARKER_BYTES,
                        deadline=deadline,
                        progress=progress,
                    ),
                    "[Content_Types].xml",
                )
                if "word/document.xml" in by_name:
                    required_office = ("docx", "word/document.xml")
                elif "xl/workbook.xml" in by_name:
                    required_office = ("xlsx", "xl/workbook.xml")
                elif "ppt/presentation.xml" in by_name:
                    required_office = ("pptx", "ppt/presentation.xml")
                if required_office is not None:
                    marker = by_name[required_office[1]]
                    _parse_xml_marker(
                        _read_marker(
                            archive,
                            marker,
                            limit=MAX_MARKER_BYTES,
                            deadline=deadline,
                            progress=progress,
                        ),
                        required_office[1],
                    )
                    evidence.extend(("[Content_Types].xml", required_office[1]))
                    classification = ZipIntakeClassification(
                        "atomic_package",
                        "validated",
                        required_office[0],
                        tuple(evidence),
                        member_count,
                        total,
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
                # A malformed/incomplete OOXML-looking package is ambiguous,
                # not permission to expand arbitrary package internals.
                if any(name.startswith(("word/", "xl/", "ppt/")) for name in normalized):
                    classification = ZipIntakeClassification(
                        "invalid",
                        "ambiguous",
                        "invalid",
                        ("content_types_without_functional_marker",),
                        member_count,
                        total,
                        detail="incomplete OOXML package",
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
            mimetype = by_name.get("mimetype")
            if mimetype is not None:
                payload = _read_marker(
                    archive,
                    mimetype,
                    limit=MAX_MIMETYPE_BYTES,
                    deadline=deadline,
                    progress=progress,
                )
                try:
                    declared_mime = payload.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ZipIntakeError("ambiguous", "mimetype_not_ascii") from exc
                if declared_mime == "application/epub+zip" and "META-INF/container.xml" in by_name:
                    _read_marker(
                        archive,
                        by_name["META-INF/container.xml"],
                        limit=MAX_MARKER_BYTES,
                        deadline=deadline,
                        progress=progress,
                    )
                    classification = ZipIntakeClassification(
                        "atomic_package",
                        "validated",
                        "epub",
                        ("mimetype", "META-INF/container.xml"),
                        member_count,
                        total,
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
                if declared_mime in _known_odf_mimes() and "content.xml" in by_name and "META-INF/manifest.xml" in by_name:
                    _read_marker(
                        archive,
                        by_name["content.xml"],
                        limit=MAX_MARKER_BYTES,
                        deadline=deadline,
                        progress=progress,
                    )
                    _read_marker(
                        archive,
                        by_name["META-INF/manifest.xml"],
                        limit=MAX_MARKER_BYTES,
                        deadline=deadline,
                        progress=progress,
                    )
                    classification = ZipIntakeClassification(
                        "atomic_package",
                        "validated",
                        "odf",
                        ("mimetype", "content.xml", "META-INF/manifest.xml"),
                        member_count,
                        total,
                        mime=declared_mime,
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
                if declared_mime in _known_odf_mimes() or declared_mime == "application/epub+zip":
                    classification = ZipIntakeClassification(
                        "invalid",
                        "ambiguous",
                        "invalid",
                        ("mimetype",),
                        member_count,
                        total,
                        detail="incomplete package markers",
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
            android_manifest = by_name.get("AndroidManifest.xml")
            if android_manifest is not None:
                _read_marker(
                    archive,
                    android_manifest,
                    limit=MAX_MARKER_BYTES,
                    deadline=deadline,
                    progress=progress,
                )
                classification = ZipIntakeClassification(
                    "atomic_package",
                    "validated",
                    "apk",
                    ("AndroidManifest.xml",),
                    member_count,
                    total,
                    structure=structure,
                )
                return ZipDecision(identity, classification, preflight)
            jar_manifest = by_name.get("META-INF/MANIFEST.MF")
            if jar_manifest is not None:
                manifest_payload = _read_marker(
                    archive,
                    jar_manifest,
                    limit=MAX_MARKER_BYTES,
                    deadline=deadline,
                    progress=progress,
                )
                if b"Manifest-Version:" in manifest_payload:
                    classification = ZipIntakeClassification(
                        "atomic_package",
                        "validated",
                        "jar",
                        ("META-INF/MANIFEST.MF",),
                        member_count,
                        total,
                        structure=structure,
                    )
                    return ZipDecision(identity, classification, preflight)
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
            project_evidence = tuple(
                name for name in normalized if PurePosixPath(name).name.casefold() in project_markers
            )
            if project_evidence:
                evidence.extend(f"project:{name}" for name in project_evidence[:8])
                classification = ZipIntakeClassification(
                    "generic_zip",
                    "validated",
                    "project",
                    tuple(evidence),
                    member_count,
                    total,
                    structure=structure,
                )
            else:
                classification = ZipIntakeClassification(
                    "generic_zip",
                    "validated",
                    "storage_archive",
                    ("no_atomic_markers",),
                    member_count,
                    total,
                    structure=structure,
                )
            return ZipDecision(identity, classification, preflight)
    except ZipIntakeError as exc:
        classification = ZipIntakeClassification("invalid", exc.status, "invalid", detail=exc.detail)
        return ZipDecision(identity, classification)
    except PermissionError as exc:
        classification = ZipIntakeClassification("invalid", "blocked", "invalid", detail=str(exc))
        return ZipDecision(identity, classification)
    except (ZipStructureError, zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        classification = ZipIntakeClassification(
            "invalid",
            "corrupt",
            "invalid",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return ZipDecision(identity, classification)


def decide_zip(
    source: str | os.PathLike[str],
    *,
    limits: ZipIntakeLimits | None = None,
    max_file_bytes: int | None = None,
    deadline: float | None = None,
    cancellation: object | None = None,
    progress: object | None = None,
) -> ZipDecision:
    """Return the canonical bounded ZIP decision for one source identity."""

    effective_limits = ZipIntakeLimits() if limits is None else limits
    effective_limits.validate()
    path = Path(source)
    identity = SourceIdentity.capture(path)
    reporter = _ZipProgress(progress, source=path)
    if max_file_bytes is not None:
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 0:
            raise ValueError("max_file_bytes must be a non-negative integer or None")
        if identity.size > max_file_bytes:
            classification = ZipIntakeClassification(
                "invalid",
                "skipped_by_size",
                "invalid",
                detail="source exceeds global size admission",
            )
            reporter.finish("classification", metrics=(ProgressMetric("blocked", 1),))
            return ZipDecision(identity, classification)
    if identity.size > effective_limits.max_input_bytes:
        classification = ZipIntakeClassification(
            "invalid",
            "budget",
            "invalid",
            detail="source exceeds max_input_bytes",
        )
        reporter.finish("classification", metrics=(ProgressMetric("blocked", 1),))
        return ZipDecision(identity, classification)
    decision = _decide_zip(
        path,
        identity=identity,
        limits=effective_limits,
        deadline=_deadline_for(effective_limits, deadline, cancellation),
        progress=reporter,
    )
    metric_name = "atomic" if decision.classification.is_atomic else "generic" if decision.classification.is_generic else "blocked"
    reporter.finish("classification", metrics=(ProgressMetric(metric_name, 1),))
    return decision


def decide_zip_candidate(
    source: str | os.PathLike[str],
    *,
    limits: ZipIntakeLimits | None = None,
    max_file_bytes: int | None = None,
    deadline: float | None = None,
    cancellation: object | None = None,
    progress: object | None = None,
) -> ZipDecision | None:
    """Find and decide one ZIP candidate without a second detector layer.

    A non-``.zip`` path is admitted by its four-byte ZIP signature.  The
    signature probe is owned by this function, so callers must not probe and
    then call :func:`decide_zip` independently for the same source.
    """

    path = Path(source)
    if path.suffix.casefold() != ".zip":
        try:
            with path.open("rb") as stream:
                if stream.read(4) not in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x06"}:
                    return None
        except OSError:
            return None
    return decide_zip(
        path,
        limits=limits,
        max_file_bytes=max_file_bytes,
        deadline=deadline,
        cancellation=cancellation,
        progress=progress,
    )


def classify_zip(
    source: str | os.PathLike[str],
    *,
    limits: ZipIntakeLimits | None = None,
    deadline: float | None = None,
    cancellation: object | None = None,
    progress: object | None = None,
) -> ZipIntakeClassification:
    """Return the one bounded content-based classification for ``source``."""

    return decide_zip(
        source,
        limits=limits,
        deadline=deadline,
        cancellation=cancellation,
        progress=progress,
    ).classification


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


def _source_sha256(
    path: Path,
    *,
    deadline: _Deadline,
    progress: _ZipProgress | None = None,
) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                deadline.check()
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                if progress is not None:
                    progress.tick("hash", len(chunk))
    except ZipIntakeError:
        raise
    except OSError as exc:
        raise ZipIntakeError("blocked", "source_digest_unavailable", str(exc)) from exc
    return digest.hexdigest()


def _open_member_directory(root_fd: int, components: tuple[str, ...]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for component in components:
            child_fd = _open_directory_child(current_fd, component, create=True)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_output(path: Path, *, parent_fd: int | None = None, name: str | None = None) -> int:
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if parent_fd is None:
            return os.open(path, flags, 0o600)
        if name is None:
            raise ZipIntakeError("dependency", "stage_output_name_missing")
        return os.open(name, flags, 0o600, dir_fd=parent_fd)
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
    output_parent_fd: int | None = None,
    output_name: str | None = None,
    progress: _ZipProgress | None = None,
) -> None:
    info = member.info
    declared = int(info.file_size)
    if declared > limits.max_member_bytes or budget.total_uncompressed_bytes + declared > limits.max_total_uncompressed_bytes:
        raise ZipIntakeError("budget", "member_or_total_budget", member.relative_name)
    _check_disk(disk_root, declared, limits=limits)
    descriptor = _open_output(output, parent_fd=output_parent_fd, name=output_name)
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
                    if progress is not None:
                        progress.tick("extract", len(chunk))
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


def _nested_zip_decision(
    path: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    progress: _ZipProgress | None = None,
) -> ZipDecision | None:
    """Observe a nested candidate once, including its reusable preflight."""

    if not _is_zip_candidate(path):
        return None
    identity = SourceIdentity.capture(path)
    return _decide_zip(
        path,
        identity=identity,
        limits=limits,
        deadline=deadline,
        progress=progress,
    )


def _extract_zip_tree(
    archive_path: Path,
    destination: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    budget: _ExtractionBudget,
    depth: int,
    preflight: _Preflight | None = None,
    progress: _ZipProgress | None = None,
) -> int:
    deadline.check()
    if depth > limits.depth_limit:
        raise ZipIntakeError("budget", "nested_depth_budget")
    effective_preflight = preflight
    if effective_preflight is None:
        effective_preflight = _preflight_zip(
            archive_path,
            limits=limits,
            deadline=deadline,
            progress=progress,
        )
    if effective_preflight.total_uncompressed_bytes + budget.total_uncompressed_bytes > limits.max_total_uncompressed_bytes:
        raise ZipIntakeError("budget", "nested_total_uncompressed_budget")
    destination_parent_fd, _destination_parent_identity = _open_directory_path(
        destination.parent,
        label="staged destination parent",
    )
    try:
        destination_fd = _open_directory_child(
            destination_parent_fd,
            destination.name,
            create=True,
        )
    finally:
        os.close(destination_parent_fd)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in effective_preflight.members:
                deadline.check()
                components = tuple(member.relative_name.split("/"))
                target = destination.joinpath(*components)
                if member.is_directory:
                    member_fd = _open_member_directory(destination_fd, components)
                    os.close(member_fd)
                    continue
                member_parent_fd = _open_member_directory(destination_fd, components[:-1])
                try:
                    _stream_member(
                        archive,
                        member,
                        target,
                        limits=limits,
                        deadline=deadline,
                        budget=budget,
                        disk_root=destination,
                        output_parent_fd=member_parent_fd,
                        output_name=components[-1],
                        progress=progress,
                    )
                finally:
                    os.close(member_parent_fd)
    except ZipIntakeError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
        raise ZipIntakeError("corrupt", "zip_extraction_failed", str(exc)) from exc
    finally:
        os.close(destination_fd)
    _verify_tree(destination, limits=limits, deadline=deadline, progress=progress)
    # Generic nested archives are expanded in-place before publication.  A
    # nested atomic package is left as the regular file for normal routes.
    for child in sorted(destination.rglob("*"), key=lambda item: (len(item.parts), os.fsencode(os.fspath(item)))):
        deadline.check()
        if not child.is_file() or child.is_symlink():
            continue
        nested_decision = _nested_zip_decision(
            child,
            limits=limits,
            deadline=deadline,
            progress=progress,
        )
        if nested_decision is None:
            continue
        nested = nested_decision.classification
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
            preflight=nested_decision._preflight,
            progress=progress,
        )
        try:
            child.unlink()
        except OSError as exc:
            raise ZipIntakeError("blocked", "nested_source_cleanup_failed", str(exc)) from exc
    _verify_tree(destination, limits=limits, deadline=deadline, progress=progress)
    return budget.members


def _verify_tree(
    root: Path,
    *,
    limits: ZipIntakeLimits,
    deadline: _Deadline,
    progress: _ZipProgress | None = None,
) -> None:
    root_fd, _root_identity = _open_directory_path(root, label="staged tree")
    total = 0
    pending = [root_fd]
    try:
        while pending:
            current_fd = pending.pop()
            try:
                deadline.check()
                scan_fd = os.dup(current_fd)
                try:
                    with os.scandir(scan_fd) as entries:
                        for entry in entries:
                            deadline.check()
                            metadata = os.stat(
                                entry.name,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                            if stat.S_ISDIR(metadata.st_mode):
                                if metadata.st_mode & 0o777 != 0o700:
                                    raise ZipIntakeError("unsafe", "staged_directory_permissions", entry.name)
                                pending.append(
                                    _open_directory_child(
                                        current_fd,
                                        entry.name,
                                        create=False,
                                    )
                                )
                                continue
                            if (
                                stat.S_ISLNK(metadata.st_mode)
                                or not stat.S_ISREG(metadata.st_mode)
                                or metadata.st_nlink != 1
                            ):
                                raise ZipIntakeError("unsafe", "staged_special_or_link", entry.name)
                            if metadata.st_mode & 0o777 != 0o600 or metadata.st_mode & 0o111:
                                raise ZipIntakeError("unsafe", "staged_file_permissions", entry.name)
                            total += int(metadata.st_size)
                            if total > limits.max_total_uncompressed_bytes:
                                raise ZipIntakeError("budget", "staged_tree_budget")
                            if progress is not None:
                                progress.tick("verify", 1)
                finally:
                    try:
                        os.close(scan_fd)
                    except OSError:
                        pass
            finally:
                os.close(current_fd)
    finally:
        for descriptor in pending:
            try:
                os.close(descriptor)
            except OSError:
                pass


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


def _rollback_after_publication(
    publisher: PublishHook,
    receipt: PublishReceipt,
    *,
    reason: str,
    detail: str | None = None,
) -> None:
    """Convert every rollback failure after publication into recovery state."""

    try:
        rollback_ok = publisher.rollback(receipt)
    except BaseException as exc:
        rollback_detail = f"{detail}; " if detail else ""
        raise ZipIntakeError(
            "recovery_required",
            reason,
            f"{rollback_detail}publication rollback failed: {type(exc).__name__}: {exc}",
        ) from exc
    if not rollback_ok:
        raise ZipIntakeError("recovery_required", reason, detail)


def _first_not_none(*values: object | None) -> object | None:
    for value in values:
        if value is not None:
            return value
    return None


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
    decision: ZipDecision | None = None,
) -> ZipIntakeOutcome | dict[str, object]:
    """Plan or physically intake one ZIP, always failing closed.

    ``apply=False`` performs no staging, publication, or Trash effect.  Apply
    requires a caller-owned Trash hook because removing the source is a KIO
    effect, never an ``unlink`` fallback.  A source over ``max_file_bytes`` is
    rejected before opening or classifying it.  ``decision`` may only come
    from the canonical :func:`decide_zip`/ :func:`decide_zip_candidate` pass;
    it is reused only when its complete ``SourceIdentity`` still matches the
    current path.  A stale decision is discarded and the source is decided
    again before any extraction or effect.
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
            cancellation=_first_not_none(cancellation, cancellation_token, cancel),
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
    reporter: _ZipProgress
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
    reporter = _ZipProgress(progress, source=path)
    effective_cancellation = _first_not_none(cancellation, cancellation_token, cancel)
    deadline_obj = _deadline_for(effective_limits, deadline, effective_cancellation)
    try:
        deadline_obj.check()
    except ZipIntakeError as exc:
        classification = ZipIntakeClassification("invalid", exc.status, "invalid", detail=exc.detail)
        reporter.finish("classification", metrics=(ProgressMetric("blocked", 1),))
        return ZipIntakeOutcome(
            exc.status,
            source_text,
            identity,
            classification,
            apply=apply,
            reason=exc.reason,
            detail=exc.detail,
        )
    if decision is not None and not isinstance(decision, ZipDecision):
        raise TypeError("decision must be a ZipDecision or None")
    if decision is not None and decision.matches(path, identity):
        selected_decision = decision
    else:
        selected_decision = _decide_zip(
            path,
            identity=identity,
            limits=effective_limits,
            deadline=deadline_obj,
            progress=reporter,
        )
    classification = selected_decision.classification
    reporter.finish(
        "classification",
        metrics=(
            ProgressMetric(
                "atomic" if classification.is_atomic else "generic" if classification.is_generic else "blocked",
                1,
            ),
        ),
    )
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
        reporter.finish("intake", metrics=(ProgressMetric("atomic", 1),))
        return ZipIntakeOutcome("atomic", source_text, identity, classification, apply=apply, members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, reason="atomic_package", detail="preserved as a functional package")
    try:
        source_digest = _source_sha256(path, deadline=deadline_obj, progress=reporter)
        reporter.finish("hash")
    except ZipIntakeError as exc:
        return ZipIntakeOutcome(exc.status, source_text, identity, classification, apply=apply, reason=exc.reason, detail=exc.detail)
    destination_path = _validate_destination(Path(destination) if destination is not None else _default_destination(path))
    if not apply:
        return ZipIntakeOutcome("planned", source_text, identity, classification, apply=False, destination=os.fspath(destination_path), members=classification.member_count, uncompressed_bytes=classification.estimated_uncompressed_bytes, reason="generic_zip", source_sha256=source_digest)
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
        workspace_fd = _open_private_directory(lease.path, label="workspace", create=False)
        payload_root = lease.path / "payload"
        try:
            payload_fd = _open_directory_child(workspace_fd, "payload", create=True)
        finally:
            os.close(workspace_fd)
        os.close(payload_fd)
        destination_name = destination_path.name
        staged_destination = payload_root / destination_name
        _check_disk(lease.path, classification.estimated_uncompressed_bytes, limits=effective_limits)
        _extract_zip_tree(
            path,
            staged_destination,
            limits=effective_limits,
            deadline=deadline_obj,
            budget=budget,
            depth=0,
            preflight=selected_decision._preflight,
            progress=reporter,
        )
        reporter.finish("extract")
        _verify_tree(staged_destination, limits=effective_limits, deadline=deadline_obj, progress=reporter)
        reporter.finish("verify")
        if not identity.matches(path):
            raise ZipIntakeError("source_changed", "source_changed_before_publish")
        _assert_destination_parent(destination_path)
        if os.path.lexists(destination_path):
            raise ZipIntakeError("collision", "destination_collision")
        publish_receipt = _publisher(publisher).publish(staged_destination, destination_path)
        if not isinstance(publish_receipt, PublishReceipt):
            raise ZipIntakeError("recovery_required", "publish_receipt_invalid")
        if not identity.matches(path):
            _rollback_after_publication(
                _publisher(publisher),
                publish_receipt,
                reason="source_changed_after_publish",
            )
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
            _rollback_after_publication(
                _publisher(publisher),
                publish_receipt,
                reason="trash_failed_publication_rollback_failed",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise ZipIntakeError(
                "blocked",
                "trash_hook_failed",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        if trash_result.status != "applied":
            _rollback_after_publication(
                _publisher(publisher),
                publish_receipt,
                reason="trash_failed_publication_rollback_failed",
                detail=trash_result.detail,
            )
            trash_status: IntakeStatus = (
                "recovery_required"
                if trash_result.status == "recovery_required"
                else "blocked"
            )
            raise ZipIntakeError(trash_status, "trash_not_applied", trash_result.detail)
        if os.path.lexists(path):
            raise ZipIntakeError("recovery_required", "trash_claim_unverified")
        if publish_receipt is not None:
            publish_receipt.close()
        try:
            lease.complete()
        except BaseException as exc:
            reporter.finish("intake", metrics=(ProgressMetric("blocked", 1),))
            return ZipIntakeOutcome(
                "recovery_required",
                source_text,
                identity,
                classification,
                apply=True,
                destination=os.fspath(destination_path),
                members=budget.members,
                uncompressed_bytes=budget.total_uncompressed_bytes,
                published=True,
                trashed=True,
                successor_paths=(os.fspath(destination_path),),
                reason="scratch_completion_failed",
                detail=f"{type(exc).__name__}: {exc}",
                source_sha256=source_digest,
            )
        reporter.finish("intake", metrics=(ProgressMetric("applied", 1),))
        return ZipIntakeOutcome("applied", source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=budget.members, uncompressed_bytes=budget.total_uncompressed_bytes, published=True, trashed=True, successor_paths=(os.fspath(destination_path),), reason="generic_zip_published", detail=trash_result.evidence, source_sha256=source_digest)
    except ZipIntakeError as exc:
        if publish_receipt is not None:
            publish_receipt.close()
        if lease is not None:
            try:
                lease.fail(exc.detail)
            except BaseException:
                pass
        reporter.finish("intake", metrics=(ProgressMetric("blocked", 1),))
        return ZipIntakeOutcome(exc.status, source_text, identity, classification, apply=True, destination=os.fspath(destination_path), members=budget.members, uncompressed_bytes=budget.total_uncompressed_bytes, published=publish_receipt is not None and exc.status == "recovery_required", reason=exc.reason, detail=exc.detail, source_sha256=source_digest)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        if publish_receipt is not None:
            publish_receipt.close()
        if lease is not None:
            try:
                lease.fail(f"{type(exc).__name__}: {exc}")
            except BaseException:
                pass
        reporter.finish("intake", metrics=(ProgressMetric("blocked", 1),))
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
    progress: object | None = None,
    decision: ZipDecision | None = None,
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
    token = _first_not_none(cancellation, cancellation_token, cancel)
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
            progress=progress,
            decision=decision,
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

    del config, state, run_id
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
            progress=progress,
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
    "ZipDecision",
    "ZipIntakeClassification",
    "ZipIntakeError",
    "ZipIntakeLimits",
    "ZipIntakeOutcome",
    "classify_zip",
    "decide_zip",
    "decide_zip_candidate",
    "intake_zip",
    "plan_zip_intake",
    "run_zip_intake",
)
