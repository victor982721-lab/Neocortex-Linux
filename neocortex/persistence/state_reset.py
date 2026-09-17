"""Selectable, staged reset of NeoCortex derived state.

This module deliberately sits below the command-line adapters.  It exposes a
small typed contract that an adapter can use to present a read-only preview and
then, after an explicit human confirmation, apply exactly that preview.

There are three stable scopes:

``runs``
    Remove only the Framework execution ledger.  The Framework owner remains
    in place and its caches, review data and publication markers are retained.

``runs-and-caches``
    Remove all registered SQLite owners (including their SQLite sidecars) and
    the cross-owner publication metadata.  Corpus, releases, models and any
    backup outside the selected state directory are not targets.

``all``
    The previous scope plus the explicitly managed non-SQLite state trees:
    runtime-cache and curation/checkpoints.

The implementation is intentionally conservative.  Planning never creates a
database or opens a live owner through an ordinary ``mode=ro`` connection.
Applying is fenced by every known state lock, creates a verified external
backup first, revalidates the exact plan digest, and restores the raw source
files if a physical reset cannot be completed.  Unknown files are reported but
never selected implicitly.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from neocortex.persistence.database_purge import (
    DATABASE_SIDECAR_SUFFIXES,
    DATABASE_STORE_NAMES,
    DatabaseBackupResult,
    backup_state_owners,
)
from neocortex.persistence.framework_run_reset import (
    FrameworkRunResetError,
    FrameworkRunResetPlan,
    FrameworkRunResetResult,
    apply_framework_run_reset,
    plan_framework_run_reset,
)
from neocortex.persistence.sqlite_backup import (
    SQLiteBackupPolicy,
    backup_sqlite_online,
)
from neocortex.persistence.sqlite_integrity import SQLiteIntegrityPolicy
from neocortex.persistence.sqlite_paths import existing_sqlite_uri
from neocortex.persistence.sqlite_immutable import (
    preferred_sqlite_read_mode,
    sqlite_read_session,
)
from neocortex.persistence.state_publication import (
    STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME,
    STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX,
    STATE_EPOCH_FILENAME,
    STATE_PUBLICATION_JOURNAL_FILENAME,
    STATE_PUBLICATION_LOCK_FILENAME,
    StateOwnerHead,
    StatePublicationError,
    read_state_publication_state,
)
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


STATE_RESET_SCHEMA = "neocortex.state-reset/v1"
STATE_RESET_CONFIRMATION = "RESET_STATE"
# ``yes`` is an adapter convenience, not a second authority.  It is accepted
# only by the typed engine/API together with the exact preview digest; the
# human CLI remains responsible for presenting the destructive confirmation.
STATE_RESET_YES = "yes"
STATE_RESET_INTERNAL_TOKEN = "__neocortex_state_reset_internal__"
# A descriptive alias is useful to adapters whose command says ``reset``
# rather than ``state reset``.  Both names are intentionally stable.
RESET_STATE_CONFIRMATION = STATE_RESET_CONFIRMATION
STATE_RESET_SCOPES = ("runs", "runs-and-caches", "all")
StateResetScope = Literal["runs", "runs-and-caches", "all"]

_STATE_RESET_LOCK_FILENAME = "state-reset.lock"
_PUBLICATION_FILES = (
    STATE_EPOCH_FILENAME,
    STATE_PUBLICATION_JOURNAL_FILENAME,
    STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME,
)
_RUN_LEDGER_TABLES = (
    "file_action_reconciliation_events",
    "file_action_events",
    "file_actions",
    "route_candidates",
    "run_actions",
    "route_phase_runs",
    "route_runs",
    "run_events",
    "initial_runs",
)
_RUN_STATUS_TABLES = ("initial_runs", "route_runs", "route_phase_runs")
_ACTIVE_RUN_STATUSES = frozenset({"running"})
_ACTIVE_ACTION_STATUSES = frozenset({"started", "applying", "recovery_required"})
_ALL_DATABASE_SIDECAR_SUFFIXES = tuple(DATABASE_SIDECAR_SUFFIXES)
_MAX_MANAGED_ENTRIES = 100_000
_PRESERVED_TOP_LEVEL_STATE_NAMES = frozenset(
    {
        "installation-receipts",
        "database-backups",
        "state-reset-backups",
    }
)
_CATALOG_MIGRATION_BACKUP_RE = re.compile(
    r"^document_catalog\.sqlite3\.pre-v(?P<prior>[0-9]+)-to-v"
    r"(?P<target>[0-9]+)-(?P<timestamp>[0-9]+)\.sqlite3$"
)
_CATALOG_MIGRATION_RECEIPT_SUFFIX = ".json"
_MAX_CATALOG_MIGRATION_RECEIPT_BYTES = 256 * 1024

# Tables whose rows are evidence, policy, correction or recovery state.  They
# are owner data, not disposable cache.  This list is intentionally explicit;
# an unknown non-empty table is also treated as protected below so a schema
# extension cannot silently turn a whole owner into a delete target.
_NON_REGENERABLE_TABLES: dict[str, frozenset[str]] = {
    "framework": frozenset(
        {
            "curation_authorization_grants",
            "file_actions",
            "file_action_events",
            "file_action_reconciliation_events",
            "review_candidates",
            "review_decisions",
            "review_evidence_examples",
            "review_evidence_progress",
            "review_task_batches",
            "review_tasks",
            "review_task_batch_memberships",
            "review_task_events",
            "review_task_scan_progress",
            "review_task_source_publications",
            "semantic_content_admission_policies",
            "semantic_content_admission_events",
        }
    ),
    "catalog": frozenset(
        {
            "classification_history",
            "classification_corrections",
            "organization_plans",
            "catalog_generation_manifests",
        }
    ),
    "semantic": frozenset(
        {
            "semantic_evidence",
            "semantic_work_receipts",
            "semantic_derivation_outbox",
        }
    ),
    "inventory": frozenset(
        {
            "fingerprint_content_evidence",
            "duplicate_plan_summaries",
            "planned_duplicate_groups",
            "planned_duplicate_members",
        }
    ),
}
_NON_REGENERABLE_NAME_MARKERS = (
    "policy",
    "policie",
    "correction",
    "review",
    "recovery",
    "authorization",
    "decision",
    "evidence",
    "receipt",
)
_SCHEMA_METADATA_TABLES = frozenset({"metadata", "schema_migrations"})

# Explicit cross-owner references to Framework run identifiers.  Owner-local
# identifiers such as ``catalog_run_id`` and Code ``analysis_run_id`` are
# deliberately excluded; the listed columns are the ones whose provenance is
# the integrated Framework run and therefore must retain their parent during a
# runs-only reset.
_CROSS_OWNER_RUN_REFERENCE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("catalog", "catalog_runs", "framework_run_id"),
    ("pdf", "documents", "last_seen_run_id"),
    ("pdf", "pdf_inventory", "last_seen_run_id"),
    ("pdf", "similarity_buckets", "run_id"),
    ("pdf", "similarity_relations", "run_id"),
    ("pdf", "similarity_state", "relation_run_id"),
    ("pdf", "layout_groups", "relation_run_id"),
    ("pdf", "layout_group_members", "relation_run_id"),
    ("docx", "documents", "last_seen_run_id"),
    ("docx", "docx_inventory", "last_seen_run_id"),
    ("docx", "pdf_counterparts", "checked_run_id"),
    ("docx", "layout_groups", "updated_run_id"),
    ("office", "documents", "last_seen_run_id"),
    ("office", "office_inventory", "last_seen_run_id"),
    ("audio", "documents", "last_seen_run_id"),
    ("audio", "audio_inventory", "last_seen_run_id"),
    ("video", "documents", "last_seen_run_id"),
    ("video", "video_inventory", "last_seen_run_id"),
    ("image", "images", "last_seen_run_id"),
    ("code", "analysis_runs", "framework_run_id"),
    ("code", "files", "first_seen_run_id"),
    ("code", "files", "last_seen_run_id"),
    ("code", "file_versions", "first_observed_run_id"),
    ("code", "file_versions", "last_observed_run_id"),
    ("code", "projects", "first_seen_run_id"),
    ("code", "projects", "last_seen_run_id"),
    ("archive", "containers", "last_seen_run_id"),
    ("archive", "documents", "last_seen_run_id"),
    ("text", "documents", "last_seen_run_id"),
)
_MAX_CROSS_OWNER_RUN_REFERENCES = 100_000


class StateResetError(RuntimeError):
    """Base class for a reset that cannot be completed safely."""


class StateResetConfirmationError(StateResetError):
    """The exact apply digest or destructive token was not supplied."""


class StateResetBusyError(StateResetError):
    """A writer, active lifecycle, or unresolved publication still exists."""

    def __init__(self, paths: Sequence[Path], *, reason: str | None = None) -> None:
        self.paths = tuple(paths)
        self.reason = reason
        suffix = ", ".join(str(path) for path in self.paths) or "state fence"
        super().__init__(reason or f"NeoCortex state is in use: {suffix}")


class StateResetChangedError(StateResetError):
    """The planned state changed before the destructive boundary."""


class StateResetBackupError(StateResetError):
    """The external reset backup could not be verified."""


class StateResetRecoveryRequiredError(StateResetError):
    """Reset application became uncertain and needs the preserved backup."""

    def __init__(self, message: str, *, backup_directory: Path | None = None) -> None:
        self.backup_directory = backup_directory
        detail = message
        if backup_directory is not None:
            detail += f"; backup_directory={backup_directory}"
        super().__init__(f"state reset recovery_required: {detail}")


EntryKind = Literal["file", "directory"]
TargetKind = Literal[
    "run-ledger",
    "sqlite-owner",
    "publication-metadata",
    "managed-artifact",
]


@dataclass(frozen=True, slots=True)
class StateResetEntry:
    """One fenced state path selected by a reset plan."""

    path: Path
    relative_path: str
    kind: EntryKind
    size: int
    device: int
    inode: int
    mtime_ns: int
    mode: int
    uid: int
    gid: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.relative_path or Path(self.relative_path).is_absolute():
            raise ValueError("reset entry relative path is invalid")
        if self.kind not in {"file", "directory"}:
            raise ValueError("reset entry kind is invalid")
        if self.kind == "file":
            if self.sha256 is None or len(self.sha256) != 64:
                raise ValueError("reset file entry requires a SHA-256 digest")
        elif self.sha256 is not None:
            raise ValueError("reset directory entry cannot have a digest")

    def as_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "relative_path": self.relative_path,
            "kind": self.kind,
            "size": self.size,
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StateResetTarget:
    """A typed group of paths or rows affected by the selected scope."""

    target_id: str
    kind: TargetKind
    owner: str | None
    action: Literal["clear-rows", "remove-files", "stage-reset"]
    entries: tuple[StateResetEntry, ...]

    @property
    def bytes(self) -> int:
        return sum(entry.size for entry in self.entries if entry.kind == "file")

    @property
    def files(self) -> tuple[StateResetEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "file")

    def as_payload(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "kind": self.kind,
            "owner": self.owner,
            "action": self.action,
            "entries": [entry.as_payload() for entry in self.entries],
            "file_count": len(self.files),
            "bytes": self.bytes,
        }


@dataclass(frozen=True, slots=True)
class StateResetPlan:
    """Read-only, digest-bound description of one selectable reset."""

    state_directory: Path
    scope: StateResetScope
    stores: tuple[str, ...]
    targets: tuple[StateResetTarget, ...]
    run_tables: tuple[str, ...]
    publication_status: str
    publication_epoch: int
    publication_owner_heads: tuple[StateOwnerHead, ...]
    pending_publications: tuple[str, ...]
    cross_owner_run_ids: tuple[int, ...]
    cross_owner_orphan_ids: tuple[int, ...]
    active_run_ids: tuple[int, ...]
    active_action_ids: tuple[int, ...]
    framework_plan: FrameworkRunResetPlan | None
    lock_conflicts: tuple[Path, ...]
    unmanaged_state_entries: tuple[Path, ...]
    plan_digest: str
    # A broad reset removes an owner file only when no durable, non-regenerable
    # rows are present.  Owners in this tuple are transformed from the
    # verified backup instead of being unlinked wholesale.
    protected_tables: tuple[tuple[str, tuple[str, ...]], ...] = ()
    staged_owners: tuple[str, ...] = ()
    # A full reset stages Framework/Catalog owners and keeps uncertain
    # file-action evidence rather than discarding it.  These IDs are reported
    # separately from started/applying actions, which still block every scope.
    preserved_recovery_action_ids: tuple[int, ...] = ()

    @property
    def entries(self) -> tuple[StateResetEntry, ...]:
        return tuple(entry for target in self.targets for entry in target.entries)

    @property
    def files(self) -> tuple[StateResetEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "file")

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    @property
    def has_effect(self) -> bool:
        return bool(self.entries or self.run_tables)

    def as_payload(self, *, mode: str = "preview") -> dict[str, object]:
        return {
            "schema": STATE_RESET_SCHEMA,
            "mode": mode,
            "state_directory": str(self.state_directory),
            "scope": self.scope,
            "stores": list(self.stores),
            "targets": [target.as_payload() for target in self.targets],
            "run_tables": list(self.run_tables),
            "publication_status": self.publication_status,
            "publication_epoch": self.publication_epoch,
            "state_epoch_before": self.publication_epoch,
            "owner_heads_before": [
                item.as_payload() for item in self.publication_owner_heads
            ],
            "pending_publications": list(self.pending_publications),
            "cross_owner_run_ids": list(self.cross_owner_run_ids),
            "cross_owner_orphan_ids": list(self.cross_owner_orphan_ids),
            "active_run_ids": list(self.active_run_ids),
            "active_action_ids": list(self.active_action_ids),
            "framework_plan": (
                None if self.framework_plan is None else self.framework_plan.as_payload()
            ),
            "lock_conflicts": [str(path) for path in self.lock_conflicts],
            "unmanaged_state_entries": [str(path) for path in self.unmanaged_state_entries],
            "preserved": [str(path) for path in self.unmanaged_state_entries],
            "unknown_residual": [str(path) for path in self.unmanaged_state_entries],
            "protected_tables": {
                owner: list(tables) for owner, tables in self.protected_tables
            },
            "staged_owners": list(self.staged_owners),
            "preserved_recovery_action_ids": list(self.preserved_recovery_action_ids),
            "blocked_by": [
                *[str(path) for path in self.lock_conflicts],
                *[f"run:{value}" for value in self.active_run_ids],
                *[f"action:{value}" for value in self.active_action_ids],
                *[
                    f"cross-owner-orphan:{value}"
                    for value in self.cross_owner_orphan_ids
                ],
                *(
                    [f"active_routes:{self.framework_plan.active_route_count}"]
                    if self.framework_plan is not None
                    and self.framework_plan.active_route_count
                    else []
                ),
                *(
                    [f"active_phases:{self.framework_plan.active_phase_count}"]
                    if self.framework_plan is not None
                    and self.framework_plan.active_phase_count
                    else []
                ),
                *(
                    [f"publication:{value}" for value in self.pending_publications]
                    if self.scope == "runs"
                    else []
                ),
                *(
                    ["publication:inconsistent"]
                    if self.scope == "runs" and self.publication_status == "inconsistent"
                    else []
                ),
            ],
            "plan_digest": self.plan_digest,
            "entry_count": len(self.entries),
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class StateResetResult:
    """Verified result of one applied reset."""

    plan: StateResetPlan
    backup_directory: Path | None
    backup_manifest: Path | None
    manifest: Path | None
    deleted: tuple[StateResetEntry, ...]
    cleared_tables: tuple[str, ...]
    framework_result: FrameworkRunResetResult | None = None
    status: Literal["applied", "rolled-back"] = "applied"
    rolled_back: bool = False
    post_publication_status: str | None = None
    post_publication_epoch: int | None = None
    post_pending_publications: tuple[str, ...] = ()
    post_remaining_entries: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        payload = self.plan.as_payload(mode="applied")
        payload.update(
            {
                "backup_directory": (
                    None if self.backup_directory is None else str(self.backup_directory)
                ),
                "backup_manifest": (
                    None if self.backup_manifest is None else str(self.backup_manifest)
                ),
                "manifest": None if self.manifest is None else str(self.manifest),
                "deleted": [entry.as_payload() for entry in self.deleted],
                "deleted_file_count": len(
                    tuple(entry for entry in self.deleted if entry.kind == "file")
                ),
                "deleted_bytes": sum(
                    entry.size for entry in self.deleted if entry.kind == "file"
                ),
                "cleared_tables": list(self.cleared_tables),
                "framework_result": (
                    None
                    if self.framework_result is None
                    else self.framework_result.as_payload()
                ),
                "status": self.status,
                "rolled_back": self.rolled_back,
                "verified": self.status == "applied" and not self.rolled_back,
                "run_count": (
                    0
                    if self.plan.framework_plan is None
                    else (
                        len(self.framework_result.deleted_run_ids)
                        if self.framework_result is not None
                        else len(self.plan.framework_plan.requested_run_ids)
                    )
                ),
                "cache_count": sum(
                    len(target.files)
                    for target in self.plan.targets
                    if target.kind == "sqlite-owner"
                ),
                "database_count": sum(
                    1
                    for target in self.plan.targets
                    if target.kind in {"run-ledger", "sqlite-owner"}
                    and target.files
                ),
                "postcondition": {
                    "publication_status": self.post_publication_status,
                    "publication_epoch": self.post_publication_epoch,
                    "pending_publications": list(self.post_pending_publications),
                    "remaining_entries": list(self.post_remaining_entries),
                },
            }
        )
        return payload


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise StateResetError(f"state path cannot be inspected: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise StateResetError(f"state path cannot contain symlinks: {path}")


def _safe_state_directory(path: str | Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise StateResetError("state directory must be absolute")
    if selected.name.casefold() != "state":
        raise StateResetError("state directory must have the final component 'state'")
    _reject_symlink_components(selected)
    normalized = Path(os.path.abspath(os.fspath(selected)))
    if normalized == Path(normalized.anchor) or normalized == Path.home():
        raise StateResetError("refusing to reset a filesystem or home root")
    try:
        if selected.exists() and not selected.is_dir():
            raise StateResetError("state directory is not a directory")
    except OSError as exc:
        raise StateResetError(f"state directory cannot be inspected: {selected}") from exc
    return normalized


def _safe_backup_directory(path: str | Path, state_directory: Path) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise StateResetError("backup directory must be absolute")
    _reject_symlink_components(selected)
    normalized = Path(os.path.abspath(os.fspath(selected)))
    try:
        normalized.relative_to(state_directory)
    except ValueError:
        pass
    else:
        raise StateResetError("backup directory must be outside state directory")
    if normalized == Path(normalized.anchor) or normalized == Path.home():
        raise StateResetError("refusing to use a filesystem or home root as backup")
    if os.path.lexists(normalized):
        raise StateResetError(f"backup directory already exists: {normalized}")
    return normalized


def _relative(state: Path, path: Path) -> str:
    try:
        return path.relative_to(state).as_posix()
    except ValueError as exc:  # pragma: no cover - module invariant
        raise StateResetError(f"reset target escaped state directory: {path}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateResetError(f"reset file cannot be opened: {path}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise StateResetError(f"reset file cannot be hashed: {path}") from exc
    return digest.hexdigest()


def _entry_for(state: Path, path: Path, *, kind: EntryKind | None = None) -> StateResetEntry | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateResetError(f"reset target cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise StateResetError(f"refusing to reset symlink: {path}")
    actual_kind: EntryKind
    if stat.S_ISREG(metadata.st_mode):
        actual_kind = "file"
    elif stat.S_ISDIR(metadata.st_mode):
        actual_kind = "directory"
    else:
        raise StateResetError(f"reset target is not a regular file or directory: {path}")
    if kind is not None and actual_kind != kind:
        raise StateResetError(f"reset target kind changed: {path}")
    sha = _sha256(path) if actual_kind == "file" else None
    return StateResetEntry(
        path=path,
        relative_path=_relative(state, path),
        kind=actual_kind,
        size=int(metadata.st_size) if actual_kind == "file" else 0,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mtime_ns=int(metadata.st_mtime_ns),
        mode=int(stat.S_IMODE(metadata.st_mode)),
        uid=int(metadata.st_uid),
        gid=int(metadata.st_gid),
        sha256=sha,
    )


def _entry_matches(expected: StateResetEntry, *, hash_file: bool = True) -> bool:
    try:
        metadata = expected.path.lstat()
    except (FileNotFoundError, OSError):
        return False
    if expected.kind == "file" and not stat.S_ISREG(metadata.st_mode):
        return False
    if expected.kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return False
    if (
        int(metadata.st_dev) != expected.device
        or int(metadata.st_ino) != expected.inode
        or (
            expected.kind == "file"
            and int(metadata.st_size) != expected.size
        )
        or int(metadata.st_mtime_ns) != expected.mtime_ns
        or int(stat.S_IMODE(metadata.st_mode)) != expected.mode
    ):
        return False
    return not hash_file or expected.kind != "file" or _sha256(expected.path) == expected.sha256


def _collect_tree(state: Path, root: Path) -> tuple[StateResetEntry, ...]:
    """Collect a managed tree without following links."""

    first = _entry_for(state, root, kind="directory")
    if first is None:
        return ()
    result: list[StateResetEntry] = [first]

    def walk(directory: Path) -> None:
        try:
            children = tuple(sorted(directory.iterdir(), key=os.fspath))
        except OSError as exc:
            raise StateResetError(f"managed state tree cannot be enumerated: {directory}") from exc
        for child in children:
            entry = _entry_for(state, child)
            if entry is None:  # pragma: no cover - child disappeared while enumerating
                raise StateResetChangedError(f"managed state tree changed: {child}")
            result.append(entry)
            if entry.kind == "directory":
                walk(child)
            if len(result) > _MAX_MANAGED_ENTRIES:
                raise StateResetError("managed state tree exceeds the reset entry bound")

    walk(root)
    return tuple(result)


def _content_manifest_entries(state: Path) -> tuple[StateResetEntry, ...]:
    entries: list[StateResetEntry] = []
    for name in _PUBLICATION_FILES:
        entry = _entry_for(state, state / name, kind="file")
        if entry is not None:
            entries.append(entry)
    for candidate in _content_manifest_candidates(state):
        entry = _entry_for(state, candidate, kind="file")
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def _content_manifest_candidates(state: Path) -> tuple[Path, ...]:
    try:
        candidates = tuple(
            sorted(
                state.glob(f"{STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX}*.json"),
                key=os.fspath,
            )
        )
    except OSError as exc:
        raise StateResetError("publication manifest directory cannot be enumerated") from exc
    return tuple(
        candidate
        for candidate in candidates
        if candidate.name != STATE_CONTENT_PUBLICATION_MANIFEST_FILENAME
        and candidate.name.startswith(STATE_CONTENT_PUBLICATION_MANIFEST_PREFIX)
    )


def _known_state_paths(state: Path, scope: StateResetScope) -> set[Path]:
    known: set[Path] = {
        state / store.database_name
        for store in STATE_STORE_REGISTRY.stores
    }
    known.update(
        Path(f"{database}{suffix}")
        for database in tuple(known)
        for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES
    )
    known.update(state / name for name in _PUBLICATION_FILES)
    known.update(_content_manifest_candidates(state))
    known.update(_lock_paths(state))
    if scope == "all":
        known.add(state / "runtime-cache")
        known.add(state / "curation" / "checkpoints")
    return known


def _unmanaged_entries(state: Path, scope: StateResetScope) -> tuple[Path, ...]:
    if not state.is_dir():
        return ()
    known = _known_state_paths(state, scope)
    result: list[Path] = []
    try:
        for path in sorted(state.iterdir(), key=os.fspath):
            if path not in known and path.name != _STATE_RESET_LOCK_FILENAME:
                result.append(path)
    except OSError as exc:
        raise StateResetError("state directory cannot be enumerated") from exc
    return tuple(result)


def _catalog_migration_backup_paths(path: Path) -> tuple[Path, Path] | None:
    backup = path
    if backup.name.endswith(_CATALOG_MIGRATION_RECEIPT_SUFFIX):
        backup = backup.with_name(
            backup.name[: -len(_CATALOG_MIGRATION_RECEIPT_SUFFIX)]
        )
    else:
        for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
            if backup.name.endswith(suffix):
                backup = backup.with_name(backup.name[: -len(suffix)])
                break
    match = _CATALOG_MIGRATION_BACKUP_RE.fullmatch(backup.name)
    if match is None:
        return None
    try:
        prior = int(match.group("prior"))
        target = int(match.group("target"))
    except ValueError:
        return None
    if target != prior + 1:
        return None
    receipt = backup.with_name(backup.name + _CATALOG_MIGRATION_RECEIPT_SUFFIX)
    return backup, receipt


def _regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(metadata.st_mode)


def _valid_catalog_migration_backup_pair(state: Path, path: Path) -> bool:
    pair = _catalog_migration_backup_paths(path)
    if pair is None:
        return False
    backup, receipt = pair
    if backup.parent != state or not _regular_file(backup) or not _regular_file(receipt):
        return False
    for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{backup}{suffix}")
        if os.path.lexists(sidecar) and not _regular_file(sidecar):
            return False
    try:
        metadata = receipt.stat()
        if metadata.st_size > _MAX_CATALOG_MIGRATION_RECEIPT_BYTES:
            return False
        with receipt.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("backup") == str(backup)
        and payload.get("source") == str(state / "document_catalog.sqlite3")
    )


def _unsafe_unmanaged_entries(plan: StateResetPlan) -> tuple[Path, ...]:
    """Return unknown state paths that make a destructive reset ambiguous."""

    result: list[Path] = []
    for path in plan.unmanaged_state_entries:
        if path.name in _PRESERVED_TOP_LEVEL_STATE_NAMES:
            continue
        if _catalog_migration_backup_paths(path) is not None:
            if _valid_catalog_migration_backup_pair(plan.state_directory, path):
                continue
            result.append(path)
            continue
        name = path.name.casefold()
        if (
            name.startswith(".neocortex-")
            or any(token in name for token in ("recovery", "restore", "staging", "temporary"))
            or name.endswith((".sqlite3", "-wal", "-shm", "-journal"))
        ):
            result.append(path)
    return tuple(result)


def _transient_managed_entries(plan: StateResetPlan) -> tuple[Path, ...]:
    """Detect temporary names inside managed trees before removing them.

    Curation/checkpoint publishers use hidden ``*.tmp`` files while their
    no-replace link is in flight.  There is no shared writer lock for every
    historical publisher, so an in-flight temporary is an explicit abstention
    rather than a file that reset may consume.
    """

    return tuple(
        entry.path
        for entry in plan.entries
        if entry.kind == "file"
        and (
            entry.path.name.startswith(".")
            or entry.path.name.casefold().endswith(".tmp")
        )
    )


def _database_entries(state: Path, owner: str) -> tuple[StateResetEntry, ...]:
    database_name = STATE_STORE_REGISTRY.by_owner(owner).database_name
    database = state / database_name
    result: list[StateResetEntry] = []
    main = _entry_for(state, database)
    if main is not None:
        result.append(main)
    for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{database}{suffix}")
        entry = _entry_for(state, sidecar)
        if entry is not None:
            result.append(entry)
    if result and main is None:
        raise StateResetError(f"database owner has orphan sidecars: {database}")
    return tuple(result)


def _protected_owner_tables(
    state: Path,
    owners: Sequence[str],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Find durable rows that make whole-owner unlinking unsafe.

    This is deliberately a small owner-aware read.  It does not infer
    protection from arbitrary payload text and it never opens a live database
    with a bare ``mode=ro`` connection.  Unknown schemas are handled by the
    normal reset/schema fences; this helper only recognizes the concrete
    non-regenerable tables already owned by NeoCortex.
    """

    observed: list[tuple[str, tuple[str, ...]]] = []
    for owner in owners:
        # Owners without a concrete non-regenerable extension have no reason
        # to be opened during reset planning.  In particular, avoid even a
        # detached inspection of ordinary format caches whose historical WAL
        # sidecars are themselves part of the read-only fence.
        if owner not in _NON_REGENERABLE_TABLES:
            continue
        database = state / STATE_STORE_REGISTRY.by_owner(owner).database_name
        if not database.is_file():
            continue
        protected: list[str] = []
        try:
            with sqlite_read_session(
                database,
                mode=preferred_sqlite_read_mode(database),
                timeout_seconds=60.0,
            ) as connection:
                tables = tuple(
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT name,type FROM sqlite_master "
                        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                        "ORDER BY name"
                    )
                )
                explicit = _NON_REGENERABLE_TABLES.get(owner, frozenset())
                for table, kind in tables:
                    if kind != "table":
                        continue
                    quoted = _quote_identifier(table)
                    count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
                    if count <= 0:
                        continue
                    folded = table.casefold()
                    if (
                        table in explicit
                        or any(marker in folded for marker in _NON_REGENERABLE_NAME_MARKERS)
                    ):
                        protected.append(table)
                # Metadata is always structurally retained by a staged owner,
                # but only marker-bearing keys make it a reason to avoid an
                # otherwise safe whole-file removal.
                if any(table == "metadata" for table, _kind in tables):
                    keys = connection.execute("SELECT key FROM metadata").fetchall()
                    if any(
                        any(marker in str(row[0]).casefold() for marker in _NON_REGENERABLE_NAME_MARKERS)
                        for row in keys
                    ):
                        protected.append("metadata")
        except (OSError, sqlite3.Error, StateResetError) as exc:
            raise StateResetError(
                f"protected owner rows cannot be inspected safely: {owner}"
            ) from exc
        if protected:
            observed.append((owner, tuple(sorted(set(protected)))))
    return tuple(observed)


def _framework_lifecycle(
    path: Path,
    *,
    include_recovery_actions: bool,
    preserve_recovery_actions: bool = False,
    keep_run_ids: Sequence[int] = (),
) -> tuple[
    FrameworkRunResetPlan | None,
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    """Read the canonical Framework reset plan from an immutable snapshot.

    ``framework_run_reset`` owns the run-history allow-list, recovery
    references and high-water run-id floor.  This wrapper intentionally does
    not duplicate those rules in the cross-owner reset engine.
    """

    if not path.is_file():
        return None, (), (), tuple(sorted(set(keep_run_ids)))
    try:
        with sqlite_read_session(
            path,
            mode=preferred_sqlite_read_mode(path),
            timeout_seconds=60.0,
        ) as connection:
            existing_run_ids = {
                int(row[0])
                for row in connection.execute("SELECT run_id FROM initial_runs")
            }
            effective_keep = tuple(
                sorted(value for value in set(keep_run_ids) if value in existing_run_ids)
            )
            orphaned_keep = tuple(
                sorted(value for value in set(keep_run_ids) if value not in existing_run_ids)
            )
            framework_plan = plan_framework_run_reset(
                connection,
                keep_run_ids=effective_keep,
            )
            # A started/applying action is always an active effect frontier.
            # ``scope=all`` stages Framework and preserves recovery_required
            # rows, so those uncertain records are surfaced separately rather
            # than blocking removal of the other regenerable owners.  The
            # narrower runs-and-caches scope retains its conservative block.
            action_statuses = "'started','applying'"
            rows = connection.execute(
                "SELECT action_id FROM file_actions "
                f"WHERE status IN ({action_statuses}) ORDER BY action_id"
            ).fetchall()
            active_actions = tuple(int(row[0]) for row in rows)
            preserved_recovery: tuple[int, ...] = ()
            if preserve_recovery_actions:
                rows = connection.execute(
                    "SELECT action_id FROM file_actions "
                    "WHERE status='recovery_required' ORDER BY action_id"
                ).fetchall()
                preserved_recovery = tuple(int(row[0]) for row in rows)
            elif include_recovery_actions:
                rows = connection.execute(
                    "SELECT action_id FROM file_actions "
                    "WHERE status='recovery_required' ORDER BY action_id"
                ).fetchall()
                active_actions += tuple(int(row[0]) for row in rows)
    except FrameworkRunResetError as exc:
        raise StateResetError(f"framework run reset plan is unavailable: {exc}") from exc
    except (OSError, sqlite3.Error, StateResetError) as exc:
        raise StateResetError(f"framework lifecycle cannot be inspected safely: {path}") from exc
    return framework_plan, active_actions, preserved_recovery, orphaned_keep


def _quote_identifier(value: str) -> str:
    """Quote an internal schema identifier after allow-list selection."""

    return '"' + value.replace('"', '""') + '"'


def _cross_owner_run_references(state: Path) -> tuple[int, ...]:
    """Collect Framework run IDs still named by preserved owner projections."""

    observed: set[int] = set()
    for owner, table, column in _CROSS_OWNER_RUN_REFERENCE_SPECS:
        database = state / STATE_STORE_REGISTRY.by_owner(owner).database_name
        try:
            metadata = database.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StateResetError(
                f"cross-owner run reference cannot inspect database: {database}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StateResetError(
                f"cross-owner run reference database is not regular: {database}"
            )
        try:
            with sqlite_read_session(
                database,
                mode=preferred_sqlite_read_mode(database),
                timeout_seconds=60.0,
            ) as connection:
                table_row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if table_row is None:
                    continue
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote_identifier(table)})"
                    )
                }
                if column not in columns:
                    continue
                rows = connection.execute(
                    f"SELECT DISTINCT {_quote_identifier(column)} "
                    f"FROM {_quote_identifier(table)} "
                    f"WHERE {_quote_identifier(column)} IS NOT NULL "
                    f"LIMIT {_MAX_CROSS_OWNER_RUN_REFERENCES + 1}"
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise StateResetError(
                f"cross-owner run references cannot be inspected: {owner}.{table}.{column}"
            ) from exc
        if len(rows) > _MAX_CROSS_OWNER_RUN_REFERENCES:
            raise StateResetError("cross-owner run references exceed their bound")
        for row in rows:
            value = row[0]
            if isinstance(value, bool):
                raise StateResetError(
                    f"cross-owner run reference is boolean: {owner}.{table}.{column}"
                )
            if isinstance(value, int):
                if value < 0:
                    raise StateResetError(
                        f"cross-owner run reference is negative: {owner}.{table}.{column}"
                    )
                if value > 0:
                    observed.add(value)
                continue
            # Text derivation attempts may use opaque correlation IDs.  Only
            # canonical decimal text can identify a Framework run here.
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                parsed = int(value)
                if parsed > 0:
                    observed.add(parsed)
    return tuple(sorted(observed))


def _publication_snapshot(
    state: Path,
) -> tuple[str, int, tuple[str, ...], tuple[StateOwnerHead, ...]]:
    if not state.is_dir():
        return "absent", 0, (), ()
    try:
        view = read_state_publication_state(state)
    except (StatePublicationError, OSError, ValueError) as exc:
        raise StateResetError("state publication metadata cannot be inspected safely") from exc
    return (
        view.status,
        int(view.epoch.epoch),
        tuple(item.event_id for item in view.pending),
        tuple(view.epoch.owner_heads),
    )


def _lock_paths(state: Path) -> tuple[Path, ...]:
    if not state.is_dir():
        return ()
    paths: set[Path] = {
        state / "framework.lock",
        state / "release.lock",
        state / STATE_PUBLICATION_LOCK_FILENAME,
        state / _STATE_RESET_LOCK_FILENAME,
    }
    paths.update(state.glob("*.route.lock"))
    paths.update(state.glob("watcher-life-*.lock"))
    return tuple(sorted(paths, key=os.fspath))


def _probe_lock(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StateResetError(f"state lock cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StateResetError(f"state lock is not a regular file: {path}")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(descriptor)
    return False


@contextmanager
def _held_locks(state: Path) -> Iterator[None]:
    """Hold every known writer fence, creating only canonical lock files."""

    if os.name == "nt":
        raise StateResetError("state reset is Linux-only")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    streams: list[tuple[Path, int]] = []
    try:
        for path in _lock_paths(state):
            if os.path.lexists(path):
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise StateResetError(f"refusing to lock non-regular path: {path}")
            elif path.name not in {
                "framework.lock",
                "release.lock",
                STATE_PUBLICATION_LOCK_FILENAME,
                _STATE_RESET_LOCK_FILENAME,
            }:
                continue
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise StateResetBusyError((path,)) from exc
            streams.append((path, descriptor))
        yield
    finally:
        for _path, descriptor in reversed(streams):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _plan_digest(
    state: Path,
    scope: StateResetScope,
    stores: tuple[str, ...],
    targets: tuple[StateResetTarget, ...],
    run_tables: tuple[str, ...],
    framework_plan: FrameworkRunResetPlan | None,
    publication_status: str,
    publication_epoch: int,
    publication_owner_heads: tuple[StateOwnerHead, ...],
    pending: tuple[str, ...],
    cross_owner_run_ids: tuple[int, ...],
    cross_owner_orphan_ids: tuple[int, ...],
    active_runs: tuple[int, ...],
    active_actions: tuple[int, ...],
    protected_tables: tuple[tuple[str, tuple[str, ...]], ...],
    staged_owners: tuple[str, ...],
    preserved_recovery_action_ids: tuple[int, ...],
) -> str:
    payload = {
        "schema": STATE_RESET_SCHEMA,
        "state_directory": str(state),
        "scope": scope,
        "stores": list(stores),
        "targets": [target.as_payload() for target in targets],
        "run_tables": list(run_tables),
        "framework_plan": (
            None if framework_plan is None else framework_plan.as_payload()
        ),
        "publication_status": publication_status,
        "publication_epoch": publication_epoch,
        "owner_heads_before": [item.as_payload() for item in publication_owner_heads],
        "pending_publications": list(pending),
        "cross_owner_run_ids": list(cross_owner_run_ids),
        "cross_owner_orphan_ids": list(cross_owner_orphan_ids),
        "active_run_ids": list(active_runs),
        "active_action_ids": list(active_actions),
        "protected_tables": {
            owner: list(tables) for owner, tables in protected_tables
        },
        "staged_owners": list(staged_owners),
        "preserved_recovery_action_ids": list(preserved_recovery_action_ids),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plan_state_reset(
    state_directory: str | Path,
    *,
    scope: StateResetScope = "runs",
    _include_lock_conflicts: bool = True,
) -> StateResetPlan:
    """Build a read-only, exact plan for one of the three reset scopes."""

    if not isinstance(scope, str) or scope not in STATE_RESET_SCOPES:
        raise StateResetError(f"unknown reset scope: {scope!r}")
    selected = _safe_state_directory(state_directory)
    selected_stores: tuple[str, ...] = (
        ("framework",)
        if scope == "runs"
        else tuple(DATABASE_STORE_NAMES)
    )
    framework = selected / "framework.sqlite3"
    cross_owner_run_ids = (
        _cross_owner_run_references(selected) if scope == "runs" else ()
    )
    (
        framework_plan,
        active_actions,
        preserved_recovery_actions,
        cross_owner_orphans,
    ) = _framework_lifecycle(
        framework,
        include_recovery_actions=scope == "runs-and-caches",
        preserve_recovery_actions=scope == "all",
        keep_run_ids=cross_owner_run_ids,
    )
    run_tables = () if framework_plan is None else framework_plan.run_tables
    active_runs = () if framework_plan is None else framework_plan.active_run_ids
    (
        publication_status,
        publication_epoch,
        pending,
        publication_owner_heads,
    ) = _publication_snapshot(selected)
    protected_tables = (
        _protected_owner_tables(selected, selected_stores)
        if scope != "runs"
        else ()
    )
    staged_owners = tuple(owner for owner, _tables in protected_tables)

    targets: list[StateResetTarget] = []
    if scope == "runs":
        entries = _database_entries(selected, "framework")
        if entries or run_tables:
            targets.append(
                StateResetTarget(
                    target_id="framework-run-ledger",
                    kind="run-ledger",
                    owner="framework",
                    action="clear-rows",
                    entries=entries,
                )
            )
    else:
        for owner in selected_stores:
            entries = _database_entries(selected, owner)
            if entries:
                targets.append(
                    StateResetTarget(
                    target_id=f"sqlite:{owner}",
                    kind="sqlite-owner",
                    owner=owner,
                    action=("stage-reset" if owner in staged_owners else "remove-files"),
                    entries=entries,
                )
                )
        publication_entries = _content_manifest_entries(selected)
        if publication_entries:
            targets.append(
                StateResetTarget(
                    target_id="state-publication-metadata",
                    kind="publication-metadata",
                    owner=None,
                    action="remove-files",
                    entries=publication_entries,
                )
            )
        if scope == "all":
            for target_id, root in (
                ("runtime-cache", selected / "runtime-cache"),
                ("curation-checkpoints", selected / "curation" / "checkpoints"),
            ):
                entries = _collect_tree(selected, root)
                if entries:
                    targets.append(
                        StateResetTarget(
                            target_id=target_id,
                            kind="managed-artifact",
                            owner=None,
                            action="remove-files",
                            entries=entries,
                        )
                    )
    conflicts = (
        tuple(path for path in _lock_paths(selected) if _probe_lock(path))
        if _include_lock_conflicts
        else ()
    )
    digest = _plan_digest(
        selected,
        scope,
        selected_stores,
        tuple(targets),
        run_tables,
        framework_plan,
        publication_status,
        publication_epoch,
        publication_owner_heads,
        pending,
        cross_owner_run_ids,
        cross_owner_orphans,
        active_runs,
        active_actions,
        protected_tables,
        staged_owners,
        preserved_recovery_actions,
    )
    return StateResetPlan(
        state_directory=selected,
        scope=scope,
        stores=selected_stores,
        targets=tuple(targets),
        run_tables=run_tables,
        publication_status=publication_status,
        publication_epoch=publication_epoch,
        publication_owner_heads=publication_owner_heads,
        pending_publications=pending,
        cross_owner_run_ids=cross_owner_run_ids,
        cross_owner_orphan_ids=cross_owner_orphans,
        active_run_ids=active_runs,
        active_action_ids=active_actions,
        framework_plan=framework_plan,
        lock_conflicts=conflicts,
        unmanaged_state_entries=_unmanaged_entries(selected, scope),
        plan_digest=digest,
        protected_tables=protected_tables,
        staged_owners=staged_owners,
        preserved_recovery_action_ids=preserved_recovery_actions,
    )


def _apply_framework_runs_staged(
    plan: StateResetPlan,
    *,
    backup: DatabaseBackupResult | None,
) -> tuple[FrameworkRunResetResult, tuple[str, ...]]:
    """Transform a verified Framework backup and atomically promote it.

    ``framework_run_reset`` is intentionally a staged-connection API.  The
    cross-owner engine therefore never calls its apply function against the
    live owner: a disposable copy is planned, transformed, integrity-checked,
    flattened into a standalone SQLite file, and only then replaced under the
    Framework lock.
    """

    if plan.framework_plan is None:
        raise StateResetError("Framework run reset has no owner plan")
    entry = next(
        (item for item in plan.entries if item.relative_path == "framework.sqlite3"),
        None,
    )
    if entry is None:
        raise StateResetBackupError("Framework reset backup omitted framework.sqlite3")
    backup_database = (
        plan.state_directory / "framework.sqlite3"
        if backup is None
        else backup.backup_directory / "framework.sqlite3"
    )
    if not backup_database.is_file():
        raise StateResetBackupError("Framework reset source database is missing")

    stage_directory = Path(
        tempfile.mkdtemp(prefix=".neocortex-state-reset-", dir=plan.state_directory)
    )
    stage_database = stage_directory / "framework.sqlite3"
    final_database = stage_directory / "framework-final.sqlite3"
    staged_connection: sqlite3.Connection | None = None
    cleared_cache_rows = 0
    try:
        if backup is None:
            # The live main file may be paired with a WAL.  Use the existing
            # SQLite online-copy primitive for this ephemeral staging source;
            # this is not the durable owner-backup service and does not
            # publish a backup artifact.
            backup_sqlite_online(
                backup_database,
                stage_database,
                policy=SQLiteBackupPolicy(
                    integrity=SQLiteIntegrityPolicy(check_mode="full")
                ),
            )
        else:
            shutil.copyfile(backup_database, stage_database)
        os.chmod(stage_database, entry.mode)
        staged_connection = sqlite3.connect(
            existing_sqlite_uri(stage_database),
            uri=True,
            timeout=60.0,
        )
        staged_connection.execute("PRAGMA busy_timeout=60000")
        staged_connection.execute("PRAGMA foreign_keys=ON")
        staged_plan = plan_framework_run_reset(
            staged_connection,
            keep_run_ids=plan.framework_plan.keep_run_ids,
        )
        if staged_plan.plan_digest != plan.framework_plan.plan_digest:
            raise StateResetChangedError("Framework staged reset plan differs from the preview")
        framework_result = apply_framework_run_reset(
            staged_connection,
            staged_plan,
            staged=True,
        )
        if plan.scope != "runs":
            # ``content_type_cache`` is regenerable cache, unlike the
            # file-action, Review, authorization and recovery tables that the
            # Framework owner preserves.  Clear it only on the disposable
            # staged copy; the live owner is replaced atomically below.
            staged_connection.execute("BEGIN IMMEDIATE")
            cleared_cache_rows = int(
                staged_connection.execute(
                    "SELECT COUNT(*) FROM content_type_cache"
                ).fetchone()[0]
            )
            staged_connection.execute("DELETE FROM content_type_cache")
            staged_connection.commit()
        staged_connection.close()
        staged_connection = None
        _compact_staged_sqlite(stage_database, final_database, mode=entry.mode)
        os.chmod(final_database, entry.mode)

        live_database = plan.state_directory / "framework.sqlite3"
        current = plan_state_reset(
            plan.state_directory,
            scope=plan.scope,
            _include_lock_conflicts=False,
        )
        _assert_plan_current(plan, current)
        _assert_owner_entries_current(plan, "framework")
        # Sidecars belong to the old owner and must not be left attached to the
        # newly published main file.  Their exact source snapshots are already
        # present in the reset backup/raw rollback set.
        old_sidecars = {
            item.path
            for item in plan.entries
            if item.kind == "file" and item.relative_path != "framework.sqlite3"
        }
        os.replace(final_database, live_database)
        # The SQLite online backup is allowed to materialize an empty WAL/SHM
        # pair even when the preview saw no sidecar.  Remove every canonical
        # sidecar after the new main file is installed, but never discard a
        # newly-created non-empty file that is evidence of an uncoordinated
        # writer.
        for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{live_database}{suffix}")
            if os.path.lexists(sidecar):
                metadata = sidecar.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise StateResetChangedError(f"Framework sidecar is not regular: {sidecar}")
                if sidecar not in old_sidecars:
                    raise StateResetChangedError(
                        f"Framework sidecar appeared during reset: {sidecar}"
                    )
                sidecar.unlink()
        directory_fd = os.open(
            plan.state_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        cleared_tables = tuple(
            table for table, count in framework_result.deleted_counts if count
        )
        if cleared_cache_rows:
            cleared_tables += ("content_type_cache",)
        return framework_result, cleared_tables
    except FrameworkRunResetError as exc:
        raise StateResetError(f"Framework staged run reset failed: {exc}") from exc
    except (OSError, sqlite3.Error) as exc:
        raise StateResetError("Framework staged run reset could not be promoted") from exc
    finally:
        if staged_connection is not None:
            staged_connection.close()
        shutil.rmtree(stage_directory, ignore_errors=True)


_CATALOG_REGENERABLE_TABLE_ORDER = (
    "catalog_generation_documents",
    "catalog_generation_manifests",
    "catalog_publications",
    "catalog_generations",
    "catalog_runs",
    "documents",
)


def _compact_staged_sqlite(source: Path, destination: Path, *, mode: int) -> None:
    """Compact a transformed staging owner and verify its final bytes.

    ``backup_sqlite_online`` intentionally preserves page allocation and is
    therefore unsuitable as the last step of a reset: deleting rows from a
    multi-gigabyte owner would leave the old file size in place.  ``VACUUM
    INTO`` runs only against the disposable staging copy, emits a standalone
    owner without WAL/SHM sidecars, and is followed by full integrity checks.
    The live owner is not opened or changed by this helper.
    """

    connection: sqlite3.Connection | None = None
    try:
        if os.path.lexists(destination):
            raise StateResetError(f"staged SQLite destination already exists: {destination}")
        connection = sqlite3.connect(
            existing_sqlite_uri(source),
            uri=True,
            timeout=60.0,
        )
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StateResetError("staged SQLite compaction could not enable foreign_keys")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM INTO ?", (str(destination),))
        connection.close()
        connection = None
        metadata = destination.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StateResetError("staged SQLite compaction produced a non-regular owner")
        os.chmod(destination, mode)
        verification = sqlite3.connect(
            existing_sqlite_uri(destination),
            uri=True,
            timeout=60.0,
        )
        try:
            verification.execute("PRAGMA foreign_keys=ON")
            if verification.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise StateResetError("compacted SQLite owner foreign_keys is unavailable")
            if verification.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StateResetError("compacted SQLite owner failed foreign-key verification")
            integrity = tuple(
                str(row[0]) for row in verification.execute("PRAGMA integrity_check")
            )
            if integrity != ("ok",):
                raise StateResetError("compacted SQLite owner failed integrity verification")
        finally:
            verification.close()
        descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except StateResetError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise StateResetError("staged SQLite compaction could not be verified") from exc
    finally:
        if connection is not None:
            connection.close()


_INVENTORY_STAGED_TABLE_ORDER = (
    "planned_duplicate_members",
    "planned_duplicate_groups",
    "duplicate_plan_summaries",
    "duplicate_plan_heads",
    "fingerprint_content_evidence",
    "inventory_scan_successors",
    "inventory_generation_heads",
    "inventory_checkpoints",
    "files",
    "fingerprints",
    "scans",
)


def _assert_inventory_protected_references(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> None:
    """Reject durable Inventory evidence whose parent rows are missing.

    Older Inventory schemas deliberately omit foreign-key declarations on the
    duplicate-plan tables.  A broad reset must not preserve an apparently
    healthy summary/member row that is already orphaned, because doing so
    would turn corruption into durable evidence while deleting its possible
    parents.
    """

    references = (
        ("duplicate_plan_summaries", "scan_id", "scans", "scan_id"),
        ("planned_duplicate_groups", "scan_id", "scans", "scan_id"),
        ("planned_duplicate_members", "group_id", "planned_duplicate_groups", "group_id"),
    )
    for child, child_column, parent, parent_column in references:
        if child not in table_names or parent not in table_names:
            continue
        orphan = connection.execute(
            f"SELECT 1 FROM {_quote_identifier(child)} AS child "
            f"LEFT JOIN {_quote_identifier(parent)} AS parent "
            f"ON parent.{_quote_identifier(parent_column)} = "
            f"child.{_quote_identifier(child_column)} "
            f"WHERE parent.{_quote_identifier(parent_column)} IS NULL LIMIT 1"
        ).fetchone()
        if orphan is not None:
            raise StateResetError(
                f"inventory protected evidence has an orphan reference: {child}.{child_column}"
            )


def _apply_inventory_owner_staged(
    plan: StateResetPlan,
    *,
    backup: DatabaseBackupResult | None,
) -> tuple[str, ...]:
    """Reset regenerable Inventory payload while retaining plan evidence."""

    entry = next(
        (
            item
            for target in plan.targets
            if target.owner == "inventory"
            for item in target.entries
            if item.relative_path == "dedup.sqlite3"
        ),
        None,
    )
    if entry is None:
        raise StateResetBackupError("inventory staged reset omitted dedup.sqlite3")
    source = (
        plan.state_directory / "dedup.sqlite3"
        if backup is None
        else backup.backup_directory / "dedup.sqlite3"
    )
    if not source.is_file():
        raise StateResetBackupError("inventory staged reset source is missing")
    stage_directory = Path(
        tempfile.mkdtemp(prefix=".neocortex-state-reset-inventory-", dir=plan.state_directory)
    )
    stage_database = stage_directory / "dedup.sqlite3"
    final_database = stage_directory / "dedup-final.sqlite3"
    connection: sqlite3.Connection | None = None
    cleared: list[str] = []
    try:
        if backup is None:
            backup_sqlite_online(
                source,
                stage_database,
                policy=SQLiteBackupPolicy(
                    integrity=SQLiteIntegrityPolicy(check_mode="full")
                ),
            )
        else:
            shutil.copyfile(source, stage_database)
        os.chmod(stage_database, entry.mode)
        connection = sqlite3.connect(existing_sqlite_uri(stage_database), uri=True, timeout=60.0)
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        protected = set(dict(plan.protected_tables).get("inventory", ()))
        _assert_inventory_protected_references(connection, table_names)
        # Plan summaries/groups are durable evidence and their scan parents
        # must remain for foreign-key integrity.  Other generation/checkpoint
        # rows are regenerable and can be discarded in this broad reset.
        preserved_scan_ids: set[int] = set()
        for table in ("duplicate_plan_summaries", "planned_duplicate_groups"):
            if table not in table_names:
                continue
            column = "scan_id"
            preserved_scan_ids.update(
                int(row[0]) for row in connection.execute(f"SELECT {column} FROM \"{table}\"")
            )
        connection.execute("BEGIN IMMEDIATE")
        for table in _INVENTORY_STAGED_TABLE_ORDER:
            if table not in table_names:
                continue
            if table in protected:
                continue
            if table == "scans":
                if preserved_scan_ids:
                    placeholders = ",".join("?" for _ in preserved_scan_ids)
                    connection.execute(
                        f"DELETE FROM \"{table}\" WHERE scan_id NOT IN ({placeholders})",
                        tuple(sorted(preserved_scan_ids)),
                    )
                else:
                    connection.execute("DELETE FROM \"scans\"")
            elif table == "files":
                connection.execute("DELETE FROM \"files\"")
            elif table in {"inventory_checkpoints", "fingerprints"}:
                connection.execute(f"DELETE FROM \"{table}\"")
            else:
                # Unprotected lineage tables have no durable rows to retain;
                # delete them before their scan parents.
                connection.execute(f"DELETE FROM \"{table}\"")
            cleared.append(table)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateResetError("inventory staged reset failed foreign-key verification")
        connection.commit()
        connection.close()
        connection = None
        _compact_staged_sqlite(stage_database, final_database, mode=entry.mode)
        live_database = plan.state_directory / "dedup.sqlite3"
        _assert_owner_entries_current(plan, "inventory")
        old_sidecars = {
            item.path
            for target in plan.targets
            if target.owner == "inventory"
            for item in target.entries
            if item.kind == "file" and item.relative_path != "dedup.sqlite3"
        }
        os.replace(final_database, live_database)
        for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{live_database}{suffix}")
            if not os.path.lexists(sidecar):
                continue
            metadata = sidecar.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise StateResetChangedError(f"inventory sidecar is not regular: {sidecar}")
            if sidecar not in old_sidecars:
                raise StateResetChangedError(
                    f"inventory sidecar appeared during reset: {sidecar}"
                )
            sidecar.unlink()
        directory_fd = os.open(
            plan.state_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return tuple(cleared)
    except sqlite3.Error as exc:
        raise StateResetError("inventory staged reset could not be promoted") from exc
    finally:
        if connection is not None:
            connection.close()
        shutil.rmtree(stage_directory, ignore_errors=True)


def _apply_catalog_owner_staged(
    plan: StateResetPlan,
    *,
    backup: DatabaseBackupResult | None,
) -> None:
    """Reset catalog projections while retaining correction/recovery rows.

    Classification history, durable classification corrections and
    organization plans are owner evidence.  They cannot be recovered from a
    corpus-only rebuild, so a broad state reset replaces a staged catalog
    rather than unlinking it.  The helper intentionally handles only the
    current catalog schema; another owner is never guessed into this policy.
    """

    entry = next(
        (
            item
            for target in plan.targets
            if target.owner == "catalog"
            for item in target.entries
            if item.relative_path == "document_catalog.sqlite3"
        ),
        None,
    )
    if entry is None:
        raise StateResetBackupError("catalog staged reset omitted document_catalog.sqlite3")
    source = (
        plan.state_directory / "document_catalog.sqlite3"
        if backup is None
        else backup.backup_directory / "document_catalog.sqlite3"
    )
    if not source.is_file():
        raise StateResetBackupError("catalog staged reset source is missing")
    stage_directory = Path(
        tempfile.mkdtemp(prefix=".neocortex-state-reset-catalog-", dir=plan.state_directory)
    )
    stage_database = stage_directory / "document_catalog.sqlite3"
    final_database = stage_directory / "document_catalog-final.sqlite3"
    connection: sqlite3.Connection | None = None
    try:
        if backup is None:
            backup_sqlite_online(
                source,
                stage_database,
                policy=SQLiteBackupPolicy(
                    integrity=SQLiteIntegrityPolicy(check_mode="full")
                ),
            )
        else:
            shutil.copyfile(source, stage_database)
        os.chmod(stage_database, entry.mode)
        connection = sqlite3.connect(
            existing_sqlite_uri(stage_database),
            uri=True,
            timeout=60.0,
        )
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        protected = dict(plan.protected_tables).get("catalog", ())
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # Published Catalog generations are append-only evidence protected by
        # schema triggers. A broad reset may retire an unpublished/cancelled
        # generation, but it must retain every published generation and its
        # base ancestors, documents, publication row and catalog run.
        preserved_generations: set[int] = set()
        if "catalog_generations" in table_names:
            rows = connection.execute(
                "SELECT generation_id,base_generation_id,status FROM catalog_generations"
            ).fetchall()
            pending = [int(row[0]) for row in rows if str(row[2]) == "published"]
            base_by_generation = {
                int(row[0]): (None if row[1] is None else int(row[1])) for row in rows
            }
            while pending:
                generation_id = pending.pop()
                if generation_id in preserved_generations:
                    continue
                if generation_id not in base_by_generation:
                    raise StateResetError("catalog published generation base is missing")
                preserved_generations.add(generation_id)
                base = base_by_generation[generation_id]
                if base is not None and base not in preserved_generations:
                    pending.append(base)
        preserved_catalog_runs: set[int] = set()
        if preserved_generations and "catalog_generations" in table_names:
            placeholders = ",".join("?" for _ in preserved_generations)
            preserved_catalog_runs = {
                int(row[0])
                for row in connection.execute(
                    "SELECT catalog_run_id FROM catalog_generations "
                    f"WHERE generation_id IN ({placeholders}) "
                    "AND catalog_run_id IS NOT NULL",
                    tuple(sorted(preserved_generations)),
                ).fetchall()
            }
        # Generation ancestry is a self-referential immediate FK in current
        # Catalog schemas.  Deleting an entire unpublished chain is valid, but
        # only when SQLite defers those checks until the transaction has
        # removed every member of the chain.
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in _CATALOG_REGENERABLE_TABLE_ORDER:
            if (
                table not in table_names
                or (table in protected and table != "catalog_generation_manifests")
                or table in _SCHEMA_METADATA_TABLES
            ):
                continue
            if table in {
                "catalog_generation_manifests",
                "catalog_generation_documents",
                "catalog_generations",
                "catalog_publications",
            }:
                if preserved_generations:
                    placeholders = ",".join("?" for _ in preserved_generations)
                    connection.execute(
                        f"DELETE FROM {_quote_identifier(table)} "
                        f"WHERE generation_id NOT IN ({placeholders})",
                        tuple(sorted(preserved_generations)),
                    )
                else:
                    connection.execute(f"DELETE FROM {_quote_identifier(table)}")
            elif table == "catalog_runs":
                if preserved_catalog_runs:
                    placeholders = ",".join("?" for _ in preserved_catalog_runs)
                    connection.execute(
                        "DELETE FROM \"catalog_runs\" "
                        f"WHERE catalog_run_id NOT IN ({placeholders})",
                        tuple(sorted(preserved_catalog_runs)),
                    )
                else:
                    connection.execute("DELETE FROM \"catalog_runs\"")
            else:
                connection.execute(f"DELETE FROM {_quote_identifier(table)}")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StateResetError("catalog staged reset failed foreign-key verification")
        integrity = tuple(
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        )
        if integrity != ("ok",):
            raise StateResetError("catalog staged reset failed integrity verification")
        connection.commit()
        connection.close()
        connection = None
        _compact_staged_sqlite(stage_database, final_database, mode=entry.mode)
        os.chmod(final_database, entry.mode)
        live_database = plan.state_directory / "document_catalog.sqlite3"
        _assert_owner_entries_current(plan, "catalog")
        old_sidecars = {
            item.path
            for target in plan.targets
            if target.owner == "catalog"
            for item in target.entries
            if item.kind == "file" and item.relative_path != "document_catalog.sqlite3"
        }
        os.replace(final_database, live_database)
        for suffix in _ALL_DATABASE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{live_database}{suffix}")
            if not os.path.lexists(sidecar):
                continue
            metadata = sidecar.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise StateResetChangedError(f"catalog sidecar is not regular: {sidecar}")
            if sidecar not in old_sidecars:
                raise StateResetChangedError(
                    f"catalog sidecar appeared during reset: {sidecar}"
                )
            sidecar.unlink()
        directory_fd = os.open(
            plan.state_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except sqlite3.Error as exc:
        raise StateResetError("catalog staged reset could not be promoted") from exc
    finally:
        if connection is not None:
            connection.close()
        shutil.rmtree(stage_directory, ignore_errors=True)


def _ensure_directory(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if os.path.lexists(current):
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise StateResetError(f"backup parent is not a real directory: {current}")
        else:
            current.mkdir(mode=0o700)
            current.chmod(0o700)


def _new_backup_directory(path: Path) -> Path:
    _ensure_directory(path.parent)
    try:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    except FileExistsError as exc:
        raise StateResetError(f"backup directory already exists: {path}") from exc
    except OSError as exc:
        raise StateResetError(f"backup directory could not be created: {path}") from exc
    return path


def _default_backup_directory(state: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return state.parent / "state-reset-backups" / f"{stamp}-{time.time_ns()}"


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    _ensure_directory(path.parent)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _copy_raw_file(entry: StateResetEntry, destination: Path) -> None:
    if entry.kind != "file":
        return
    _ensure_directory(destination.parent)
    if os.path.lexists(destination):
        raise StateResetBackupError(f"raw reset backup target already exists: {destination}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(entry.path, flags)
    except OSError as exc:
        raise StateResetBackupError(f"raw reset source cannot be opened: {entry.path}") from exc
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(raw_path)
        with os.fdopen(source_descriptor, "rb", closefd=True) as source, os.fdopen(
            descriptor, "wb", closefd=True
        ) as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, entry.mode)
        os.replace(temporary, destination)
        temporary = None
    except (OSError, ValueError) as exc:
        raise StateResetBackupError(f"raw reset backup copy failed: {entry.path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        observed = entry.path.lstat()
    except OSError as exc:
        raise StateResetChangedError(
            f"reset source disappeared while backing up: {entry.path}"
        ) from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or int(observed.st_dev) != entry.device
        or int(observed.st_ino) != entry.inode
        or int(observed.st_size) != entry.size
        or int(observed.st_mtime_ns) != entry.mtime_ns
        or int(stat.S_IMODE(observed.st_mode)) != entry.mode
        or _sha256(entry.path) != entry.sha256
    ):
        raise StateResetChangedError(f"reset source changed while backing up: {entry.path}")
    if _sha256(destination) != entry.sha256:
        raise StateResetBackupError(f"raw reset backup hash mismatch: {destination}")


def _raw_backup_entries(backup: Path, entries: Sequence[StateResetEntry]) -> Path:
    root = backup / "reset-files"
    _ensure_directory(root)
    for entry in entries:
        if entry.kind == "file":
            _copy_raw_file(entry, root / entry.relative_path)
    return root


def _assert_plan_current(plan: StateResetPlan, current: StateResetPlan) -> None:
    if current.plan_digest != plan.plan_digest:
        raise StateResetChangedError(
            f"state reset plan changed: expected {plan.plan_digest}, observed {current.plan_digest}"
        )
    for expected in plan.entries:
        if not _entry_matches(expected):
            raise StateResetChangedError(f"reset target changed before apply: {expected.path}")


def _assert_owner_entries_current(plan: StateResetPlan, owner: str) -> None:
    """Revalidate one SQLite owner immediately before staged promotion.

    A preceding staged owner may legitimately have a new inode by this point,
    so the complete plan digest cannot be reused for every promotion.  The
    owner fence still includes every planned main/sidecar entry, and no new
    canonical or unknown ``<owner>-*`` sibling may appear in the promotion
    window.  Empty sidecars are evidence too and are never silently removed.
    """

    owner_targets = tuple(target for target in plan.targets if target.owner == owner)
    if not owner_targets:
        raise StateResetChangedError(f"staged reset owner is absent from the plan: {owner}")
    expected_paths = {
        entry.path
        for target in owner_targets
        for entry in target.entries
    }
    for path in expected_paths:
        matching = next(
            (
                entry
                for target in owner_targets
                for entry in target.entries
                if entry.path == path
            ),
            None,
        )
        if matching is None or not _entry_matches(matching):
            raise StateResetChangedError(f"staged {owner} owner changed before promotion: {path}")
    database = next(
        (
            entry.path
            for target in owner_targets
            for entry in target.entries
            if entry.relative_path == STATE_STORE_REGISTRY.by_owner(owner).database_name
        ),
        None,
    )
    if database is None:
        raise StateResetChangedError(f"staged {owner} owner has no main database entry")
    prefix = f"{database.name}-"
    try:
        siblings = tuple(database.parent.iterdir())
    except OSError as exc:
        raise StateResetChangedError(
            f"staged {owner} sidecars cannot be enumerated before promotion"
        ) from exc
    for sibling in siblings:
        if sibling.name.startswith(prefix) and sibling not in expected_paths:
            raise StateResetChangedError(
                f"staged {owner} owner gained an unplanned sidecar: {sibling}"
            )


def _assert_staged_owners_have_no_sidecars(
    plan: StateResetPlan,
    observed: StateResetPlan,
) -> None:
    """Require every promoted staged owner to be detached from old sidecars."""

    for owner in plan.staged_owners:
        database_name = STATE_STORE_REGISTRY.by_owner(owner).database_name
        found_main = False
        for target in observed.targets:
            if target.owner != owner:
                continue
            if any(
                entry.kind == "file" and entry.relative_path == database_name
                for entry in target.entries
            ):
                found_main = True
            if any(
                entry.kind == "file" and entry.relative_path != database_name
                for entry in target.entries
            ):
                raise StateResetChangedError(
                    f"staged {owner} owner retained or gained a SQLite sidecar"
                )
        if not found_main:
            raise StateResetChangedError(f"staged {owner} owner main database is missing")


def _delete_entries(entries: Sequence[StateResetEntry]) -> tuple[StateResetEntry, ...]:
    deleted: list[StateResetEntry] = []
    files = [entry for entry in entries if entry.kind == "file"]
    directories = [entry for entry in entries if entry.kind == "directory"]
    for entry in files:
        if not _entry_matches(entry):
            raise StateResetChangedError(f"reset target changed before removal: {entry.path}")
        try:
            entry.path.unlink()
        except FileNotFoundError as exc:
            raise StateResetChangedError(f"reset target disappeared: {entry.path}") from exc
        except OSError as exc:
            raise StateResetError(f"reset target could not be removed: {entry.path}") from exc
        if os.path.lexists(entry.path):
            raise StateResetError(f"reset target remains after removal: {entry.path}")
        deleted.append(entry)
    for entry in sorted(directories, key=lambda value: len(value.path.parts), reverse=True):
        try:
            metadata = entry.path.lstat()
        except FileNotFoundError:
            deleted.append(entry)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise StateResetChangedError(f"managed reset directory changed: {entry.path}")
        try:
            entry.path.rmdir()
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY:
                raise StateResetChangedError(
                    f"managed reset directory gained an unplanned entry: {entry.path}"
                ) from exc
            raise StateResetError(f"managed reset directory could not be removed: {entry.path}") from exc
        deleted.append(entry)
    return tuple(deleted)


def _restore_file(entry: StateResetEntry, raw_root: Path) -> None:
    if entry.kind != "file":
        return
    source = raw_root / entry.relative_path
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
        raise StateResetRecoveryRequiredError(f"raw backup entry is not regular: {source}")
    if _sha256(source) != entry.sha256:
        raise StateResetRecoveryRequiredError(f"raw backup hash mismatch: {source}")
    _ensure_directory(entry.path.parent)
    if os.path.lexists(entry.path):
        current = entry.path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise StateResetRecoveryRequiredError(f"cannot replace non-regular target: {entry.path}")
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{entry.path.name}.restore-", dir=entry.path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, entry.mode)
        os.replace(temporary, entry.path)
    except BaseException as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, StateResetRecoveryRequiredError):
            raise
        raise StateResetRecoveryRequiredError(f"raw target restore failed: {entry.path}") from exc


def _restore_raw(entries: Sequence[StateResetEntry], raw_root: Path) -> None:
    for entry in entries:
        _restore_file(entry, raw_root)
    for entry in entries:
        if entry.kind != "directory":
            continue
        try:
            metadata = entry.path.lstat()
        except FileNotFoundError:
            entry.path.mkdir(parents=True, mode=entry.mode)
            entry.path.chmod(entry.mode)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise StateResetRecoveryRequiredError(f"raw directory restore target is invalid: {entry.path}")
        entry.path.chmod(entry.mode)
    for entry in entries:
        if entry.kind == "file" and not _entry_matches(entry, hash_file=True):
            # inode and mtime naturally change during restoration, so verify
            # content and mode without requiring the pre-reset inode.
            metadata = entry.path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or int(metadata.st_size) != entry.size
                or int(stat.S_IMODE(metadata.st_mode)) != entry.mode
                or _sha256(entry.path) != entry.sha256
            ):
                raise StateResetRecoveryRequiredError(f"raw target verification failed: {entry.path}")


def _database_backup_ok(result: DatabaseBackupResult, stores: tuple[str, ...]) -> None:
    by_owner = {entry.owner: entry for entry in result.entries}
    for owner in stores:
        entry = by_owner.get(owner)
        if entry is None:
            raise StateResetBackupError(f"database backup omitted owner: {owner}")
        if entry.status == "absent":
            continue
        if entry.status != "backed_up" or entry.integrity is None or not entry.integrity.healthy:
            raise StateResetBackupError(f"database backup is incomplete for owner: {owner}")


def _manifest_payload(
    plan: StateResetPlan,
    *,
    status: str,
    backup: Path | None,
    database_backup: DatabaseBackupResult | None,
    raw_root: Path | None,
    error: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": STATE_RESET_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "status": status,
        "scope": plan.scope,
        "state_directory": str(plan.state_directory),
        "plan_digest": plan.plan_digest,
        "stores": list(plan.stores),
        "raw_backup_directory": (
            None
            if raw_root is None or backup is None
            else str(raw_root.relative_to(backup))
        ),
        "database_backup_manifest": (
            None
            if database_backup is None or backup is None
            else str(database_backup.manifest.relative_to(backup))
        ),
        "targets": [target.as_payload() for target in plan.targets],
        "cleared_tables": list(plan.run_tables if plan.scope == "runs" else ()),
        "error": error,
    }
    return payload


def _apply_reset_locked(
    plan: StateResetPlan,
    *,
    backup_directory: Path | None,
) -> StateResetResult:
    current = plan_state_reset(
        plan.state_directory,
        scope=plan.scope,
        _include_lock_conflicts=False,
    )
    _assert_plan_current(plan, current)
    unsafe = _unsafe_unmanaged_entries(current)
    if unsafe:
        raise StateResetError(
            "reset refuses unknown or recovery state entries: "
            + ", ".join(str(path) for path in unsafe[:16])
        )
    if current.cross_owner_orphan_ids and plan.scope == "runs":
        raise StateResetError(
            "reset refuses cross-owner references to absent Framework runs: "
            + ", ".join(str(value) for value in current.cross_owner_orphan_ids[:16])
        )
    transient = _transient_managed_entries(current)
    if transient:
        raise StateResetBusyError(
            transient[:16],
            reason="reset refuses in-flight managed temporary entries",
        )
    if (
        current.active_run_ids
        or current.active_action_ids
        or (
            current.framework_plan is not None
            and current.framework_plan.blocked
        )
    ):
        raise StateResetBusyError(
            (plan.state_directory / "framework.sqlite3",),
            reason=(
                f"active run ids={list(current.active_run_ids)} "
                f"action ids={list(current.active_action_ids)}"
            ),
        )
    if current.pending_publications and plan.scope == "runs":
        raise StateResetBusyError(
            (plan.state_directory / STATE_PUBLICATION_JOURNAL_FILENAME,),
            reason=f"unresolved state publications={list(current.pending_publications)}",
        )
    # A run-only reset leaves the publication boundary in place, so an
    # inconsistent marker still needs owner-aware recovery.  Broader scopes
    # explicitly include the publication files and can retire that marker as
    # part of the same staged set; blocking those scopes would make it
    # impossible to recover a stale publication by selecting ``all``.
    if current.publication_status == "inconsistent" and plan.scope == "runs":
        raise StateResetBusyError(
            (plan.state_directory / STATE_PUBLICATION_JOURNAL_FILENAME,),
            reason=f"state publication status is {current.publication_status}",
        )

    has_database_targets = bool(
        any(
            target.kind in {"run-ledger", "sqlite-owner"} and target.entries
            for target in plan.targets
        )
    )
    database_backup: DatabaseBackupResult | None = None
    if backup_directory is not None and has_database_targets:
        try:
            database_backup = backup_state_owners(
                plan.state_directory,
                backup_directory,
                stores=plan.stores,
                integrity_mode="full",
                expected_epoch=plan.publication_epoch,
                _assume_locks_held=True,
            )
            _database_backup_ok(database_backup, plan.stores)
        except StateResetError:
            raise
        except BaseException as exc:
            raise StateResetBackupError("verified SQLite reset backup failed") from exc
    elif backup_directory is not None:
        _new_backup_directory(backup_directory)

    # Even a no-backup apply gets an ephemeral raw copy while the effect is in
    # flight.  It is not a backup artifact and is removed after a successful
    # operation; retaining it on an uncertain rollback gives the caller a
    # local recovery breadcrumb without silently publishing a backup.
    ephemeral_root: Path | None = None
    raw_root: Path | None
    if plan.entries:
        if backup_directory is None:
            try:
                ephemeral_root = Path(
                    tempfile.mkdtemp(
                        prefix=".neocortex-state-reset-raw-",
                        dir=plan.state_directory.parent,
                    )
                )
            except OSError as exc:
                raise StateResetBackupError(
                    "ephemeral reset rollback area could not be created"
                ) from exc
            raw_root = _raw_backup_entries(ephemeral_root, plan.entries)
        else:
            raw_root = _raw_backup_entries(backup_directory, plan.entries)
    else:
        raw_root = None
    post_backup = plan_state_reset(
        plan.state_directory,
        scope=plan.scope,
        _include_lock_conflicts=False,
    )
    _assert_plan_current(plan, post_backup)
    prepared_manifest: Path | None = None
    if backup_directory is not None:
        if raw_root is None:  # pragma: no cover - backup directory invariant
            raise StateResetBackupError("reset backup has no rollback root")
        prepared_manifest = _write_json(
            backup_directory / "state-reset-manifest.json",
            _manifest_payload(
                plan,
                status="prepared",
                backup=backup_directory,
                database_backup=database_backup,
                raw_root=raw_root,
            ),
        )

    deleted: tuple[StateResetEntry, ...] = ()
    cleared_tables: tuple[str, ...] = ()
    framework_result: FrameworkRunResetResult | None = None
    effect_started = True
    try:
        if plan.scope == "runs":
            framework_result, cleared_tables = _apply_framework_runs_staged(
                plan,
                backup=database_backup,
            )
        else:
            staged_owner_entries = {
                entry
                for target in plan.targets
                if target.owner in set(plan.staged_owners)
                for entry in target.entries
            }
            if "framework" in plan.staged_owners:
                # Framework owns the run ledger and the protected Review /
                # action / authorization / recovery rows.  Its staged helper
                # clears only regenerable cache rows before promotion.
                framework_result, cleared_tables = _apply_framework_runs_staged(
                    plan,
                    backup=database_backup,
                )
            if "inventory" in plan.staged_owners:
                inventory_tables = _apply_inventory_owner_staged(
                    plan,
                    backup=database_backup,
                )
                cleared_tables += inventory_tables
            if "catalog" in plan.staged_owners:
                _apply_catalog_owner_staged(
                    plan,
                    backup=database_backup,
                )
            removable_entries = tuple(
                entry for entry in plan.entries if entry not in staged_owner_entries
            )
            deleted = _delete_entries(removable_entries)
        after = plan_state_reset(
            plan.state_directory,
            scope=plan.scope,
            _include_lock_conflicts=False,
        )
        _assert_staged_owners_have_no_sidecars(plan, after)
        if plan.scope == "runs":
            if after.active_run_ids or after.active_action_ids:
                raise StateResetError("active lifecycle appeared during run reset")
            if framework_result is None or not framework_result.verified:
                raise StateResetError("Framework staged run reset was not verified")
        else:
            remaining_targets = {
                entry
                for target in plan.targets
                if target.owner not in set(plan.staged_owners)
                for entry in target.entries
            }
            remaining_after = {
                entry.path
                for entry in after.entries
            }
            if any(entry.path in remaining_after for entry in remaining_targets):
                raise StateResetError("reset targets remain after removal")
        final_manifest = (
            None
            if prepared_manifest is None
            else _write_json(
                prepared_manifest,
                _manifest_payload(
                    plan,
                    status="applied",
                    backup=backup_directory,
                    database_backup=database_backup,
                    raw_root=raw_root,
                ),
            )
        )
        result = StateResetResult(
            plan=plan,
            backup_directory=backup_directory,
            backup_manifest=(
                None if database_backup is None else database_backup.manifest
            ),
            manifest=final_manifest,
            deleted=deleted,
            cleared_tables=cleared_tables,
            framework_result=framework_result,
            post_publication_status=after.publication_status,
            post_publication_epoch=after.publication_epoch,
            post_pending_publications=after.pending_publications,
            post_remaining_entries=tuple(str(entry.path) for entry in after.entries),
        )
        if ephemeral_root is not None:
            shutil.rmtree(ephemeral_root, ignore_errors=False)
            ephemeral_root = None
        return result
    except BaseException as exc:
        if effect_started:
            try:
                if raw_root is not None:
                    _restore_raw(plan.entries, raw_root)
            except BaseException as rollback_error:
                raise StateResetRecoveryRequiredError(
                    f"reset failed and raw rollback failed: {rollback_error}; "
                    f"ephemeral_rollback={ephemeral_root}",
                    backup_directory=backup_directory,
                ) from exc
            try:
                if prepared_manifest is not None:
                    _write_json(
                        prepared_manifest,
                        _manifest_payload(
                            plan,
                            status="rolled-back",
                            backup=backup_directory,
                            database_backup=database_backup,
                            raw_root=raw_root,
                            error=str(exc),
                        ),
                    )
            except BaseException as manifest_error:
                raise StateResetRecoveryRequiredError(
                    f"reset rolled back but receipt update failed: {manifest_error}",
                    backup_directory=backup_directory,
                ) from exc
            if ephemeral_root is not None:
                shutil.rmtree(ephemeral_root, ignore_errors=True)
                ephemeral_root = None
        if isinstance(exc, StateResetError):
            raise
        raise StateResetError("state reset failed and was rolled back") from exc


def execute_state_reset(
    state_directory: str | Path,
    *,
    scope: StateResetScope = "runs",
    apply: bool = False,
    plan_digest: str | None = None,
    # Older embedders called this fence ``expected_plan_digest``.  Keep it as
    # a strict alias rather than making them bypass the current preview.
    expected_plan_digest: str | None = None,
    confirmation: str | None = None,
    yes: bool = False,
    backup_directory: str | Path | None = None,
) -> StateResetPlan | StateResetResult:
    """Preview by default or apply one exact reset plan.

    ``apply=True`` requires both the SHA-256 ``plan_digest`` returned by the
    preview and the literal :data:`STATE_RESET_CONFIRMATION` token.  A fresh
    plan is always recaptured while all state fences are held before any
    database or metadata is modified.
    """

    plan = plan_state_reset(state_directory, scope=scope)
    if not apply:
        if (
            plan_digest is not None
            or expected_plan_digest is not None
            or confirmation is not None
            or yes is not False
        ):
            raise StateResetConfirmationError("digest and confirmation are only valid with --apply")
        return plan
    if not isinstance(yes, bool):
        raise StateResetConfirmationError("yes must be a boolean")
    if plan_digest is not None and expected_plan_digest is not None and plan_digest != expected_plan_digest:
        raise StateResetConfirmationError(
            "plan_digest and expected_plan_digest disagree"
        )
    selected_digest = plan_digest if plan_digest is not None else expected_plan_digest
    # The aliases deliberately converge on the legacy literal.  No yes-like
    # value is accepted as a digest or as a substitute for the digest fence.
    if yes:
        if confirmation is None:
            confirmation = STATE_RESET_CONFIRMATION
        elif confirmation not in {
            STATE_RESET_CONFIRMATION,
            STATE_RESET_YES,
            STATE_RESET_INTERNAL_TOKEN,
        }:
            raise StateResetConfirmationError("yes conflicts with confirmation")
    if confirmation in {STATE_RESET_YES, STATE_RESET_INTERNAL_TOKEN}:
        confirmation = STATE_RESET_CONFIRMATION
    if confirmation != STATE_RESET_CONFIRMATION:
        raise StateResetConfirmationError(
            f"apply requires confirmation token {STATE_RESET_CONFIRMATION!r}"
        )
    if not isinstance(selected_digest, str) or selected_digest != plan.plan_digest:
        raise StateResetConfirmationError("apply requires the exact preview plan digest")
    # A destructive reset is never implicit-backup.  When the caller supplies
    # ``backup_directory`` the existing verified owner-backup service is used;
    # when omitted, no durable backup service is invoked (only an ephemeral
    # rollback copy is kept for the duration of the effect).
    selected_backup = (
        None
        if backup_directory is None
        else _safe_backup_directory(backup_directory, plan.state_directory)
    )
    if plan.lock_conflicts:
        raise StateResetBusyError(plan.lock_conflicts)
    if (
        plan.active_run_ids
        or plan.active_action_ids
        or (plan.framework_plan is not None and plan.framework_plan.blocked)
    ):
        raise StateResetBusyError(
            (plan.state_directory / "framework.sqlite3",),
            reason=(
                f"active run ids={list(plan.active_run_ids)} "
                f"action ids={list(plan.active_action_ids)}"
            ),
        )
    with _held_locks(plan.state_directory):
        return _apply_reset_locked(plan, backup_directory=selected_backup)


def apply_state_reset(
    plan: StateResetPlan,
    *,
    confirmation: str | None = None,
    yes: bool = False,
    expected_plan_digest: str | None = None,
    backup_directory: str | Path | None = None,
) -> StateResetResult:
    """Apply a previously returned plan without silently replanning its scope."""

    result = execute_state_reset(
        plan.state_directory,
        scope=plan.scope,
        apply=True,
        plan_digest=plan.plan_digest,
        expected_plan_digest=expected_plan_digest,
        confirmation=confirmation,
        yes=yes,
        backup_directory=backup_directory,
    )
    if not isinstance(result, StateResetResult):  # pragma: no cover - type invariant
        raise StateResetError("state reset did not return an applied result")
    return result


# Compatibility-friendly alias for callers that prefer an operation verb.
reset_state = execute_state_reset


__all__ = [
    "RESET_STATE_CONFIRMATION",
    "STATE_RESET_CONFIRMATION",
    "STATE_RESET_INTERNAL_TOKEN",
    "STATE_RESET_SCHEMA",
    "STATE_RESET_SCOPES",
    "STATE_RESET_YES",
    "StateResetBackupError",
    "StateResetBusyError",
    "StateResetChangedError",
    "StateResetConfirmationError",
    "StateResetEntry",
    "StateResetError",
    "StateResetPlan",
    "StateResetRecoveryRequiredError",
    "StateResetResult",
    "StateResetScope",
    "StateResetTarget",
    "apply_state_reset",
    "execute_state_reset",
    "plan_state_reset",
    "reset_state",
]
