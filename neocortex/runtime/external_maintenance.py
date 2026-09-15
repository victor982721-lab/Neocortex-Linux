"""Bounded, read-only diagnostics for explicitly selected external roots.

The external environment is deliberately *not* a cleanup owner.  This module
only records what can be observed safely at one caller-supplied root and one
caller-supplied category.  It does not discover roots from ``HOME`` or
configuration, inspect file contents, open databases, invoke a provider, or
offer an apply/delete operation.

The distinction between a filesystem observation and authority to change it is
important here.  Most categories in the registry belong to another program or
to the operating system.  They therefore produce ``out_of_profile`` or
``preserved`` records, never candidates.  A missing category owner is treated
as ``out_of_profile`` even when the root is readable.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal


EXTERNAL_MAINTENANCE_SCHEMA = "neocortex.external-maintenance/v1"

ExternalStatus = Literal[
    "absent",
    "observed",
    "preserved",
    "blocked",
    "unknown",
    "out_of_profile",
]

# These values are intentionally stable machine-readable causes.  They are
# explanatory evidence, not permissions or cleanup instructions.
ROOT_OMITTED = "root_omitted"
ROOT_NOT_ABSOLUTE = "root_not_absolute"
ROOT_ABSENT = "root_absent"
ROOT_SYMLINK = "root_symlink"
ROOT_PATH_SYMLINK = "root_path_symlink"
ROOT_NOT_DIRECTORY = "root_not_directory"
ROOT_PERMISSION_DENIED = "permission_denied"
ROOT_IDENTITY_CHANGED = "root_identity_changed"
ROOT_UNAVAILABLE = "root_unavailable"
NO_OWNER = "no_owner"
OUT_OF_PROFILE = "out_of_profile"
PRESERVED_OWNER = "preserved_owner"
DIAGNOSTIC_ONLY = "diagnostic_only"
FOREIGN_OWNER = "foreign_owner"
ENTRY_SYMLINK = "symlink"
ENTRY_HARDLINK = "hardlink"
ENTRY_NON_REGULAR = "non_regular"
ENTRY_MOUNT_BOUNDARY = "mount_boundary"
ENTRY_IDENTITY_UNAVAILABLE = "identity_unavailable"
ENTRY_IDENTITY_CHANGED = "identity_changed"
ENTRY_DISAPPEARED = "disappeared"
DEPTH_LIMIT = "depth_limit"
ENTRY_LIMIT = "entry_limit"
BYTE_LIMIT = "byte_limit"
CANCELLED = "cancelled"
SCAN_UNAVAILABLE = "scan_unavailable"

_MAX_CATEGORY_BYTES = 256
_MAX_REASON_BYTES = 2_048
_MAX_RECORDS = 100_000
_MAX_DEPTH = 64
_MAX_BYTES = 1 << 50


class ExternalMaintenanceError(RuntimeError):
    """Base error for invalid external-diagnostic configuration."""


class ExternalRootError(ValueError, ExternalMaintenanceError):
    """The caller did not provide a safe explicit root."""


class ExternalCategoryError(ValueError, ExternalMaintenanceError):
    """The caller did not provide a valid category identifier."""


@dataclass(frozen=True, slots=True)
class ExternalCategorySpec:
    """Static ownership metadata for one external diagnostic category.

    ``profile`` is intentionally narrower than a general ownership claim:
    ``observed`` means a known external project owner is visible but this
    diagnostic has no effect authority; ``preserved`` means a known owner
    exists and the artifact is retained; ``out_of_profile`` means the category
    is not owned by a NeoCortex maintenance policy; and ``unknown`` means
    evidence must remain unresolved (for example, an unadopted historical
    temporary).
    """

    name: str
    owner: str | None
    provenance: str
    profile: Literal["observed", "preserved", "out_of_profile", "unknown"]
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "provenance": self.provenance,
            "profile": self.profile,
            "description": self.description,
        }


def _category(
    name: str,
    *,
    owner: str | None,
    provenance: str,
    profile: Literal["observed", "preserved", "out_of_profile", "unknown"],
    description: str,
) -> ExternalCategorySpec:
    return ExternalCategorySpec(
        name=name,
        owner=owner,
        provenance=provenance,
        profile=profile,
        description=description,
    )


# This registry is a classification aid only.  It does not discover any of
# these roots and it does not make a category eligible for an effect.
_CATEGORY_REGISTRY: dict[str, ExternalCategorySpec] = {
    "desktop_thumbnail_cache": _category(
        "desktop_thumbnail_cache",
        owner=None,
        provenance="KDE desktop thumbnail cache",
        profile="out_of_profile",
        description="KDE-generated thumbnails have no NeoCortex cleanup owner.",
    ),
    "application_cache": _category(
        "application_cache",
        owner=None,
        provenance="application-managed cache",
        profile="out_of_profile",
        description="A generic application cache has no single verified owner.",
    ),
    "project_generated_artifact": _category(
        "project_generated_artifact",
        owner="project-producer",
        provenance="project build/output producer",
        profile="observed",
        description="Build, dist, and wheelhouse trees belong to their project.",
    ),
    "model_cache": _category(
        "model_cache",
        owner="neocortex-model-management",
        provenance="NeoCortex model-management owner",
        profile="preserved",
        description="Model material is preserved; this diagnostic never prepares or removes it.",
    ),
    "runtime_cache_override": _category(
        "runtime_cache_override",
        owner=None,
        provenance="caller-selected runtime cache override",
        profile="out_of_profile",
        description="An external cache override has no automatic cleanup owner.",
    ),
    "external_backup": _category(
        "external_backup",
        owner="external-backup-provider",
        provenance="backup provider",
        profile="preserved",
        description="Backups are retained and are not inferred to be disposable.",
    ),
    "package_cache": _category(
        "package_cache",
        owner=None,
        provenance="package manager cache",
        profile="out_of_profile",
        description="Package caches remain with the package manager.",
    ),
    "system_journal": _category(
        "system_journal",
        owner=None,
        provenance="system journal",
        profile="out_of_profile",
        description="System journal data is outside the NeoCortex profile.",
    ),
    "coredump": _category(
        "coredump",
        owner=None,
        provenance="system coredump service",
        profile="out_of_profile",
        description="Coredumps remain under the operating-system owner.",
    ),
    "codex_session": _category(
        "codex_session",
        owner=None,
        provenance="Codex desktop session",
        profile="out_of_profile",
        description="Codex sessions are not a NeoCortex maintenance owner.",
    ),
    "historical_temp_unadopted": _category(
        "historical_temp_unadopted",
        owner="neocortex-historical-audit",
        provenance="historical adoption evidence",
        profile="unknown",
        description="Historical data without adoption evidence stays unresolved.",
    ),
    "kio_trash": _category(
        "kio_trash",
        owner=None,
        provenance="KDE KIO Trash",
        profile="out_of_profile",
        description="KIO Trash requires its own human and desktop gate.",
    ),
    "semantic_exact_index_external": _category(
        "semantic_exact_index_external",
        owner="neocortex-semantic",
        provenance="explicit Semantic exact-index artifact",
        profile="preserved",
        description="The explicit index remains with the Semantic owner.",
    ),
}

_CATEGORY_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "kde_thumbnail_cache": "desktop_thumbnail_cache",
        "thumbnail_cache": "desktop_thumbnail_cache",
        "cache": "application_cache",
        "backups": "external_backup",
        "journal": "system_journal",
        "core_dump": "coredump",
        "semantic_exact_index": "semantic_exact_index_external",
        "historical_temp": "historical_temp_unadopted",
    }
)

EXTERNAL_CATEGORY_SPECS: Mapping[str, ExternalCategorySpec] = MappingProxyType(
    _CATEGORY_REGISTRY
)
# Public aliases make the registry discoverable without implying that any
# category is authorized for cleanup.
EXTERNAL_CATEGORIES = EXTERNAL_CATEGORY_SPECS
SUPPORTED_EXTERNAL_CATEGORIES = tuple(_CATEGORY_REGISTRY)
CATEGORY_REGISTRY = EXTERNAL_CATEGORY_SPECS


def _bounded_reason(value: object) -> str:
    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_REASON_BYTES:
        return text
    return encoded[:_MAX_REASON_BYTES].decode("utf-8", errors="replace")


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return Linux physical identity without substituting ctime for birthtime."""

    birthtime = getattr(metadata, "st_birthtime_ns", None)
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(birthtime) if birthtime is not None else -1,
    )


def filesystem_identity(path: Path | str) -> tuple[int, int, int] | None:
    """Read one no-follow identity for callers that need diagnostic evidence."""

    try:
        metadata = os.lstat(Path(path))
    except OSError:
        return None
    return _identity(metadata)


def _validate_category(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalCategoryError("category must be a non-empty string")
    if "\x00" in value:
        raise ExternalCategoryError("category must not contain NUL")
    if len(value.encode("utf-8")) > _MAX_CATEGORY_BYTES:
        raise ExternalCategoryError("category exceeds its bounded size")
    return value


def _validate_limits(
    *, max_entries: int, max_depth: int, max_bytes: int
) -> tuple[int, int, int]:
    for label, value in (
        ("max_entries", max_entries),
        ("max_depth", max_depth),
        ("max_bytes", max_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if not 1 <= max_entries <= _MAX_RECORDS:
        raise ValueError(f"max_entries must be between 1 and {_MAX_RECORDS}")
    if max_depth > _MAX_DEPTH:
        raise ValueError(f"max_depth must be at most {_MAX_DEPTH}")
    if max_bytes > _MAX_BYTES:
        raise ValueError(f"max_bytes must be at most {_MAX_BYTES}")
    return max_entries, max_depth, max_bytes


def _path_components_have_no_symlinks(path: Path) -> None:
    """Reject an explicit root whose existing path component is a symlink."""

    cursor = Path(path.anchor)
    # Leave the final component to the root lstat below so a symlink root is
    # reported as ``root_symlink`` rather than as an ambiguous parent escape.
    for component in path.parts[1:-1]:
        cursor /= component
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            # The final root validator reports absence.  A missing intermediate
            # component cannot later be followed by this read-only operation.
            break
        except OSError as exc:
            raise ExternalRootError(f"root is unavailable: {_bounded_reason(exc)}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExternalRootError(ROOT_PATH_SYMLINK)


def _type_name(metadata: os.stat_result) -> str:
    mode = metadata.st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "char_device"
    return "other"


def _raw_sizes(metadata: os.stat_result) -> tuple[int, int]:
    """Return apparent/allocated bytes without following a link or opening data."""

    if not (
        stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        return 0, 0
    return max(0, int(metadata.st_size)), max(0, int(metadata.st_blocks)) * 512


def _sorted_names(descriptor: int, limit: int) -> tuple[tuple[str, ...], bool]:
    """Collect at most ``limit`` names and detect one bounded overflow."""

    names: list[str] = []
    overflow = False
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if len(names) >= limit:
                overflow = True
                break
            names.append(entry.name)
    names.sort(key=os.fsencode)
    return tuple(names), overflow


@dataclass(slots=True)
class _Budget:
    max_entries: int
    max_bytes: int
    entries: int = 0
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    truncated: bool = False

    @property
    def observed_bytes(self) -> int:
        return self.apparent_bytes + self.allocated_bytes

    def consume(self, metadata: os.stat_result) -> tuple[int, int, int, bool] | None:
        if self.entries >= self.max_entries:
            self.truncated = True
            return None
        self.entries += 1
        apparent, allocated = _raw_sizes(metadata)
        remaining = max(0, self.max_bytes - self.observed_bytes)
        apparent_credit = min(apparent, remaining)
        allocated_credit = min(allocated, max(0, remaining - apparent_credit))
        credited = apparent_credit + allocated_credit
        if credited < apparent + allocated:
            self.truncated = True
        self.apparent_bytes += apparent_credit
        self.allocated_bytes += allocated_credit
        return apparent_credit, allocated_credit, credited, credited < apparent + allocated


@dataclass(frozen=True, slots=True)
class ExternalMaintenanceRecord:
    """One bounded metadata observation; no file payload is read."""

    path: Path
    relative_path: str
    name: str
    category: str
    status: ExternalStatus
    reason_code: str
    reason: str | None
    owner: str | None
    provenance: str
    owner_class: str
    path_identity: tuple[int, int, int] | None
    root_identity: tuple[int, int, int] | None
    observed_uid: int | None
    observed_gid: int | None
    mode: int | None
    nlink: int | None
    apparent_bytes: int
    allocated_bytes: int
    observed_bytes: int
    depth: int
    file_type: str
    is_directory: bool
    truncated: bool = False

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
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "owner": self.owner,
            "provenance": self.provenance,
            "owner_class": self.owner_class,
            "path_identity": None if self.path_identity is None else list(self.path_identity),
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "observed_uid": self.observed_uid,
            "observed_gid": self.observed_gid,
            "mode": self.mode,
            "nlink": self.nlink,
            "apparent_bytes": self.apparent_bytes,
            "allocated_bytes": self.allocated_bytes,
            "observed_bytes": self.observed_bytes,
            "depth": self.depth,
            "file_type": self.file_type,
            "is_directory": self.is_directory,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ExternalMaintenancePlan:
    """Read-only external diagnostic result."""

    root: Path
    category: str
    category_name: str | None
    owner: str | None
    provenance: str
    status: ExternalStatus
    reason_code: str
    reason: str | None
    records: tuple[ExternalMaintenanceRecord, ...] = ()
    scanned: int = 0
    observed: int = 0
    preserved: int = 0
    blocked: int = 0
    unknown: int = 0
    out_of_profile: int = 0
    absent: int = 0
    observed_bytes: int = 0
    observed_apparent_bytes: int = 0
    observed_allocated_bytes: int = 0
    truncated: bool = False
    max_entries: int = 0
    max_depth: int = 0
    max_bytes: int = 0
    root_identity: tuple[int, int, int] | None = None
    root_uid: int | None = None
    root_gid: int | None = None
    root_mode: int | None = None
    root_exists: bool = False
    read_only: bool = True
    diagnostic_only: bool = True
    candidates: int = 0
    applied: int = 0

    @property
    def entries(self) -> tuple[ExternalMaintenanceRecord, ...]:
        return self.records

    @property
    def items(self) -> tuple[ExternalMaintenanceRecord, ...]:
        return self.records

    @property
    def zero_candidates(self) -> bool:
        return self.candidates == 0

    @property
    def counts(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "observed": self.observed,
            "preserved": self.preserved,
            "blocked": self.blocked,
            "unknown": self.unknown,
            "out_of_profile": self.out_of_profile,
            "absent": self.absent,
            "candidates": self.candidates,
            "applied": self.applied,
        }

    @property
    def bytes(self) -> dict[str, int]:
        return {
            "observed": self.observed_bytes,
            "observed_apparent": self.observed_apparent_bytes,
            "observed_allocated": self.observed_allocated_bytes,
            "candidates": 0,
            "applied": 0,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EXTERNAL_MAINTENANCE_SCHEMA,
            "root": str(self.root),
            "category": self.category,
            "category_name": self.category_name,
            "owner": self.owner,
            "provenance": self.provenance,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "root_identity": None if self.root_identity is None else list(self.root_identity),
            "root_uid": self.root_uid,
            "root_gid": self.root_gid,
            "root_mode": self.root_mode,
            "root_exists": self.root_exists,
            "read_only": self.read_only,
            "diagnostic_only": self.diagnostic_only,
            "truncated": self.truncated,
            "zero_candidates": self.zero_candidates,
            "counts": self.counts,
            "bytes": self.bytes,
            "limits": {
                "max_entries": self.max_entries,
                "max_depth": self.max_depth,
                "max_bytes": self.max_bytes,
            },
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(slots=True)
class _ScanContext:
    root: Path
    root_identity: tuple[int, int, int]
    root_device: int
    category: str
    spec: ExternalCategorySpec
    budget: _Budget
    max_depth: int
    cancelled: Callable[[], bool] | None
    records: list[ExternalMaintenanceRecord]
    issue: str | None = None
    issue_code: str | None = None

    def stop(self, code: str, reason: str) -> None:
        self.budget.truncated = True
        if self.issue is None:
            self.issue_code = code
            self.issue = reason


def _canonical_category(value: str) -> tuple[str | None, ExternalCategorySpec]:
    canonical = _CATEGORY_ALIASES.get(value, value)
    spec = _CATEGORY_REGISTRY.get(canonical)
    if spec is not None:
        return canonical, spec
    # An unregistered category has no trusted owner.  It can still be
    # described at an explicitly supplied root, but all safe observations are
    # outside the automatic profile.
    return None, ExternalCategorySpec(
        name=value,
        owner=None,
        provenance="unregistered external category",
        profile="out_of_profile",
        description="No owner is registered for this category.",
    )


def external_category_spec(category: str) -> ExternalCategorySpec:
    """Return a static category specification without touching the filesystem."""

    value = _validate_category(category)
    _, spec = _canonical_category(value)
    return spec


def _base_classification(spec: ExternalCategorySpec) -> tuple[ExternalStatus, str, str]:
    if spec.owner is None:
        return "out_of_profile", NO_OWNER, "category has no verified maintenance owner"
    if spec.profile == "unknown":
        return "unknown", "schema_unverified", "category requires owner evidence outside this diagnostic"
    if spec.profile == "observed":
        return "observed", DIAGNOSTIC_ONLY, "category is observed only; its project owner is external"
    if spec.profile == "preserved":
        return "preserved", PRESERVED_OWNER, "category is retained by its owning subsystem"
    return "out_of_profile", OUT_OF_PROFILE, "category is outside the automatic profile"


def _path_for(root: Path, components: tuple[str, ...]) -> Path:
    path = root
    for component in components:
        path /= component
    return path


def _relative_path(components: tuple[str, ...]) -> str:
    return "/".join(components)


def _record_from_metadata(
    context: _ScanContext,
    *,
    path: Path,
    components: tuple[str, ...],
    metadata: os.stat_result | None,
    depth: int,
    credits: tuple[int, int, int, bool] | None,
    status: ExternalStatus | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
) -> ExternalMaintenanceRecord:
    if metadata is None:
        return ExternalMaintenanceRecord(
            path=path,
            relative_path=_relative_path(components),
            name=components[-1] if components else path.name,
            category=context.category,
            status=status or "unknown",
            reason_code=reason_code or ENTRY_IDENTITY_UNAVAILABLE,
            reason=reason or "entry identity could not be observed",
            owner=context.spec.owner,
            provenance=context.spec.provenance,
            owner_class="unknown",
            path_identity=None,
            root_identity=context.root_identity,
            observed_uid=None,
            observed_gid=None,
            mode=None,
            nlink=None,
            apparent_bytes=0,
            allocated_bytes=0,
            observed_bytes=0,
            depth=depth,
            file_type="unknown",
            is_directory=False,
            truncated=credits is not None and credits[3],
        )
    apparent, allocated, observed, was_bounded = credits or (0, 0, 0, False)
    base_status, base_code, base_reason = _base_classification(context.spec)
    final_status = status or base_status
    final_code = reason_code or base_code
    final_reason = reason if reason is not None else base_reason
    owner_class = "owned-category" if context.spec.owner is not None else "unowned-category"
    if int(metadata.st_uid) != os.geteuid():
        owner_class = "foreign-filesystem-owner"
        if status is None and final_status == "preserved":
            final_code = FOREIGN_OWNER
            final_reason = "filesystem owner differs from the current user; entry is preserved"
        elif status is None and final_status == "out_of_profile":
            final_code = FOREIGN_OWNER
            final_reason = "filesystem owner is external to the current user"
    return ExternalMaintenanceRecord(
        path=path,
        relative_path=_relative_path(components),
        name=components[-1] if components else path.name,
        category=context.category,
        status=final_status,
        reason_code=final_code,
        reason=final_reason,
        owner=context.spec.owner,
        provenance=context.spec.provenance,
        owner_class=owner_class,
        path_identity=_identity(metadata),
        root_identity=context.root_identity,
        observed_uid=int(metadata.st_uid),
        observed_gid=int(metadata.st_gid),
        mode=stat.S_IMODE(metadata.st_mode),
        nlink=int(metadata.st_nlink),
        apparent_bytes=apparent,
        allocated_bytes=allocated,
        observed_bytes=observed,
        depth=depth,
        file_type=_type_name(metadata),
        is_directory=stat.S_ISDIR(metadata.st_mode),
        truncated=was_bounded,
    )


def _replace_record(record: ExternalMaintenanceRecord, **changes: object) -> ExternalMaintenanceRecord:
    values = {
        field: getattr(record, field)
        for field in record.__dataclass_fields__
    }
    values.update(changes)
    return ExternalMaintenanceRecord(**values)


def _inspect_entry(
    context: _ScanContext,
    descriptor: int,
    components: tuple[str, ...],
    depth: int,
) -> None:
    name = components[-1]
    path = _path_for(context.root, components)
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        context.records.append(
            _record_from_metadata(
                context,
                path=path,
                components=components,
                metadata=None,
                depth=depth,
                credits=None,
                status="unknown",
                reason_code=ENTRY_DISAPPEARED,
                reason="entry disappeared during the bounded observation",
            )
        )
        context.budget.entries += 1
        return
    except OSError as exc:
        context.records.append(
            _record_from_metadata(
                context,
                path=path,
                components=components,
                metadata=None,
                depth=depth,
                credits=None,
                status="blocked",
                reason_code=ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else ENTRY_IDENTITY_UNAVAILABLE,
                reason=_bounded_reason(f"entry metadata unavailable: {exc}"),
            )
        )
        context.budget.entries += 1
        return

    credits = context.budget.consume(metadata)
    if credits is None:
        context.stop(ENTRY_LIMIT, "external diagnostic entry limit exceeded")
        return
    is_directory = stat.S_ISDIR(metadata.st_mode)
    status, code, reason = _base_classification(context.spec)
    if stat.S_ISLNK(metadata.st_mode):
        status, code, reason = "blocked", ENTRY_SYMLINK, "symlink payload is never followed"
    elif int(metadata.st_dev) != context.root_device:
        status, code, reason = "blocked", ENTRY_MOUNT_BOUNDARY, "entry crosses the root filesystem boundary"
    elif stat.S_ISREG(metadata.st_mode) and int(metadata.st_nlink) > 1:
        status, code, reason = "blocked", ENTRY_HARDLINK, "hardlinked payload is shared with another name"
    elif not is_directory and not stat.S_ISREG(metadata.st_mode):
        status, code, reason = "blocked", ENTRY_NON_REGULAR, "non-regular payload is outside the diagnostic profile"
    if credits[3]:
        context.stop(BYTE_LIMIT, "external diagnostic byte limit exceeded")
    record = _record_from_metadata(
        context,
        path=path,
        components=components,
        metadata=metadata,
        depth=depth,
        credits=credits,
        status=status,
        reason_code=code,
        reason=reason,
    )
    context.records.append(record)
    if context.budget.truncated:
        return
    if not is_directory:
        return
    if depth >= context.max_depth:
        try:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                context.records[-1] = _replace_record(
                    record,
                    status="blocked",
                    reason_code=ROOT_PERMISSION_DENIED,
                    reason="directory depth could not be checked due to permissions",
                )
            return
        try:
            child_names, overflow = _sorted_names(child_fd, 1)
            has_child = bool(child_names) or overflow
        except OSError:
            has_child = True
        finally:
            os.close(child_fd)
        if has_child:
            context.stop(DEPTH_LIMIT, "external diagnostic depth limit exceeded")
        return
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
    except OSError as exc:
        context.records[-1] = _replace_record(
            record,
            status="blocked",
            reason_code=ROOT_PERMISSION_DENIED
            if exc.errno in {errno.EACCES, errno.EPERM}
            else ENTRY_IDENTITY_UNAVAILABLE,
            reason=_bounded_reason(f"directory could not be inspected: {exc}"),
        )
        return
    try:
        opened = os.fstat(child_fd)
        if _identity(opened) != _identity(metadata):
            context.records[-1] = _replace_record(
                record,
                status="unknown",
                reason_code=ENTRY_IDENTITY_CHANGED,
                reason="directory identity changed before descent",
            )
            return
        _scan_directory(context, child_fd, path, components, depth + 1)
    finally:
        os.close(child_fd)


def _scan_directory(
    context: _ScanContext,
    descriptor: int,
    path: Path,
    components: tuple[str, ...],
    depth: int,
) -> None:
    if context.cancelled is not None and context.cancelled():
        context.stop(CANCELLED, "external diagnostic was cancelled")
        return
    remaining = context.budget.max_entries - context.budget.entries
    if remaining <= 0:
        context.stop(ENTRY_LIMIT, "external diagnostic entry limit exceeded")
        return
    try:
        names, overflow = _sorted_names(descriptor, remaining)
    except OSError as exc:
        context.stop(
            ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else SCAN_UNAVAILABLE,
            _bounded_reason(f"directory could not be inspected: {exc}"),
        )
        return
    if overflow:
        # We still inspect the bounded prefix, then retain an explicit
        # truncation claim rather than pretending the root was complete.
        pass
    for name in names:
        if context.cancelled is not None and context.cancelled():
            context.stop(CANCELLED, "external diagnostic was cancelled")
            return
        if context.budget.entries >= context.budget.max_entries:
            context.stop(ENTRY_LIMIT, "external diagnostic entry limit exceeded")
            return
        _inspect_entry(context, descriptor, (*components, name), depth)
        if context.budget.truncated:
            return
    if overflow:
        context.stop(ENTRY_LIMIT, "external diagnostic entry limit exceeded")


def _aggregate_status(
    records: tuple[ExternalMaintenanceRecord, ...], spec: ExternalCategorySpec
) -> tuple[ExternalStatus, str, str | None]:
    if not records:
        base_status, base_code, base_reason = _base_classification(spec)
        return base_status, base_code, base_reason
    statuses = {record.status for record in records}
    if "blocked" in statuses:
        return "blocked", "entry_blocked", "one or more entries failed a safe metadata boundary"
    if "unknown" in statuses:
        return "unknown", "evidence_incomplete", "one or more entries lack complete evidence"
    if len(statuses) == 1:
        record = records[0]
        return record.status, record.reason_code, record.reason
    return (
        "observed",
        "mixed_observations",
        "bounded observation contains multiple preserved classifications",
    )


def _make_plan(
    *,
    root: Path,
    category: str,
    category_name: str | None,
    spec: ExternalCategorySpec,
    records: tuple[ExternalMaintenanceRecord, ...],
    budget: _Budget,
    max_entries: int,
    max_depth: int,
    max_bytes: int,
    root_identity: tuple[int, int, int] | None,
    root_metadata: os.stat_result | None,
    root_exists: bool,
    issue_code: str | None = None,
    issue: str | None = None,
) -> ExternalMaintenancePlan:
    status, reason_code, reason = _aggregate_status(records, spec)
    if issue_code is not None:
        if issue_code == ROOT_ABSENT:
            status = "absent"
        elif issue_code in {
            ROOT_PERMISSION_DENIED,
            ROOT_SYMLINK,
            ROOT_PATH_SYMLINK,
            ROOT_NOT_DIRECTORY,
        }:
            status = "blocked"
        else:
            status = "unknown"
        reason_code = issue_code
        reason = issue
    counts = {name: sum(record.status == name for record in records) for name in (
        "observed", "preserved", "blocked", "unknown", "out_of_profile", "absent"
    )}
    return ExternalMaintenancePlan(
        root=root,
        category=category,
        category_name=category_name,
        owner=spec.owner,
        provenance=spec.provenance,
        status=status,
        reason_code=reason_code,
        reason=reason,
        records=records,
        scanned=budget.entries,
        observed=counts["observed"],
        preserved=counts["preserved"],
        blocked=counts["blocked"],
        unknown=counts["unknown"],
        out_of_profile=counts["out_of_profile"],
        absent=1 if issue_code == ROOT_ABSENT else counts["absent"],
        observed_bytes=budget.observed_bytes,
        observed_apparent_bytes=budget.apparent_bytes,
        observed_allocated_bytes=budget.allocated_bytes,
        truncated=budget.truncated,
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        root_identity=root_identity,
        root_uid=None if root_metadata is None else int(root_metadata.st_uid),
        root_gid=None if root_metadata is None else int(root_metadata.st_gid),
        root_mode=None if root_metadata is None else stat.S_IMODE(root_metadata.st_mode),
        root_exists=root_exists,
    )


class ExternalMaintenanceManager:
    """Perform one explicit, bounded, metadata-only external diagnostic."""

    def __init__(
        self,
        root: Path | str | None,
        category: str,
        *,
        max_entries: int = 10_000,
        max_depth: int = 2,
        max_bytes: int = 1 << 40,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if root is None:
            raise ExternalRootError(ROOT_OMITTED)
        try:
            candidate = Path(root)
        except (TypeError, ValueError) as exc:
            raise ExternalRootError("root must be an absolute path") from exc
        if not candidate.is_absolute():
            raise ExternalRootError(ROOT_NOT_ABSOLUTE)
        if "\x00" in str(candidate):
            raise ExternalRootError("root must not contain NUL")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        self.root = candidate
        self.category = _validate_category(category)
        self.category_name, self.spec = _canonical_category(self.category)
        self.max_entries, self.max_depth, self.max_bytes = _validate_limits(
            max_entries=max_entries,
            max_depth=max_depth,
            max_bytes=max_bytes,
        )
        self.cancelled = cancelled

    def plan(self) -> ExternalMaintenancePlan:
        """Return a read-only bounded result; no state or payload is written."""

        try:
            _path_components_have_no_symlinks(self.root)
        except ExternalRootError as exc:
            return _make_plan(
                root=self.root,
                category=self.category,
                category_name=self.category_name,
                spec=self.spec,
                records=(),
                budget=_Budget(self.max_entries, self.max_bytes),
                max_entries=self.max_entries,
                max_depth=self.max_depth,
                max_bytes=self.max_bytes,
                root_identity=None,
                root_metadata=None,
                root_exists=False,
                issue_code=ROOT_PATH_SYMLINK,
                issue=_bounded_reason(exc),
            )
        try:
            metadata = os.lstat(self.root)
        except FileNotFoundError:
            return _make_plan(
                root=self.root,
                category=self.category,
                category_name=self.category_name,
                spec=self.spec,
                records=(),
                budget=_Budget(self.max_entries, self.max_bytes),
                max_entries=self.max_entries,
                max_depth=self.max_depth,
                max_bytes=self.max_bytes,
                root_identity=None,
                root_metadata=None,
                root_exists=False,
                issue_code=ROOT_ABSENT,
                issue="explicit external root is absent",
            )
        except OSError as exc:
            return _make_plan(
                root=self.root,
                category=self.category,
                category_name=self.category_name,
                spec=self.spec,
                records=(),
                budget=_Budget(self.max_entries, self.max_bytes),
                max_entries=self.max_entries,
                max_depth=self.max_depth,
                max_bytes=self.max_bytes,
                root_identity=None,
                root_metadata=None,
                root_exists=False,
                issue_code=ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else ROOT_UNAVAILABLE,
                issue=_bounded_reason(f"explicit external root is unavailable: {exc}"),
            )
        root_identity = _identity(metadata)
        issue_code: str | None
        issue: str | None
        if stat.S_ISLNK(metadata.st_mode):
            issue_code, issue = ROOT_SYMLINK, "explicit external root must not be a symlink"
        elif not stat.S_ISDIR(metadata.st_mode):
            issue_code, issue = ROOT_NOT_DIRECTORY, "explicit external root must be a directory"
        else:
            issue_code = issue = None
        if issue_code is not None:
            return _make_plan(
                root=self.root,
                category=self.category,
                category_name=self.category_name,
                spec=self.spec,
                records=(),
                budget=_Budget(self.max_entries, self.max_bytes),
                max_entries=self.max_entries,
                max_depth=self.max_depth,
                max_bytes=self.max_bytes,
                root_identity=root_identity,
                root_metadata=metadata,
                root_exists=True,
                issue_code=issue_code,
                issue=issue,
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            return _make_plan(
                root=self.root,
                category=self.category,
                category_name=self.category_name,
                spec=self.spec,
                records=(),
                budget=_Budget(self.max_entries, self.max_bytes),
                max_entries=self.max_entries,
                max_depth=self.max_depth,
                max_bytes=self.max_bytes,
                root_identity=root_identity,
                root_metadata=metadata,
                root_exists=True,
                issue_code=ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else ROOT_UNAVAILABLE,
                issue=_bounded_reason(f"explicit external root cannot be opened: {exc}"),
            )
        budget = _Budget(self.max_entries, self.max_bytes)
        records: list[ExternalMaintenanceRecord] = []
        context = _ScanContext(
            root=self.root,
            root_identity=root_identity,
            root_device=int(metadata.st_dev),
            category=self.category,
            spec=self.spec,
            budget=budget,
            max_depth=self.max_depth,
            cancelled=self.cancelled,
            records=records,
        )
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) != root_identity:
                context.stop(ROOT_IDENTITY_CHANGED, "root identity changed before the bounded observation")
            elif self.cancelled is not None and self.cancelled():
                context.stop(CANCELLED, "external diagnostic was cancelled")
            else:
                _scan_directory(context, descriptor, self.root, (), 1)
            final = os.fstat(descriptor)
            if _identity(final) != root_identity:
                context.stop(ROOT_IDENTITY_CHANGED, "root identity changed during the bounded observation")
        except OSError as exc:
            context.stop(
                ROOT_PERMISSION_DENIED if exc.errno in {errno.EACCES, errno.EPERM} else SCAN_UNAVAILABLE,
                _bounded_reason(f"external root observation failed: {exc}"),
            )
        finally:
            os.close(descriptor)
        # A bounded root with a top-level overflow is incomplete even when all
        # returned records themselves were safe.  Keep that fact at plan level.
        return _make_plan(
            root=self.root,
            category=self.category,
            category_name=self.category_name,
            spec=self.spec,
            records=tuple(records),
            budget=budget,
            max_entries=self.max_entries,
            max_depth=self.max_depth,
            max_bytes=self.max_bytes,
            root_identity=root_identity,
            root_metadata=metadata,
            root_exists=True,
            issue_code=context.issue_code,
            issue=context.issue,
        )

    # Descriptive aliases are read-only and preserve one implementation path.
    diagnose = plan
    audit = plan


def plan_external_maintenance(
    root: Path | str,
    category: str,
    *,
    max_entries: int = 10_000,
    max_depth: int = 2,
    max_bytes: int = 1 << 40,
    cancelled: Callable[[], bool] | None = None,
) -> ExternalMaintenancePlan:
    """Plan an external diagnostic for one explicit root/category pair."""

    return ExternalMaintenanceManager(
        root,
        category,
        max_entries=max_entries,
        max_depth=max_depth,
        max_bytes=max_bytes,
        cancelled=cancelled,
    ).plan()


# Common embedding name used by diagnostic callers.
diagnose_external_maintenance = plan_external_maintenance
diagnose_external = plan_external_maintenance


__all__ = [
    "CATEGORY_REGISTRY",
    "EXTERNAL_CATEGORIES",
    "EXTERNAL_CATEGORY_SPECS",
    "EXTERNAL_MAINTENANCE_SCHEMA",
    "SUPPORTED_EXTERNAL_CATEGORIES",
    "ExternalCategoryError",
    "ExternalCategorySpec",
    "ExternalMaintenanceError",
    "ExternalMaintenanceManager",
    "ExternalMaintenancePlan",
    "ExternalMaintenanceRecord",
    "ExternalRootError",
    "diagnose_external",
    "diagnose_external_maintenance",
    "external_category_spec",
    "filesystem_identity",
    "plan_external_maintenance",
]
