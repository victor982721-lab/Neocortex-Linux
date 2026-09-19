"""Reconcile a scoped inventory with a finite USN journal window."""
# region [00] Contexto del módulo
# Módulo: neocortex/reconcile.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neocortex.enumeration.errors import VolumeAccessError
from neocortex.deduplication import (
    DedupIndex,
    FileChangedError,
    FileSnapshot,
    InventoryCheckpoint,
    InventoryExclusionPolicy,
    snapshot_path,
)
from neocortex.deduplication.inventory.scan import (
    resolve_inventory_exclusion_policy,
)
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress

if TYPE_CHECKING:
    from neocortex.enumeration.models import JournalCursor, NtfsEntry, UsnChangeBatch
    from neocortex.deduplication.inventory.scanner import InventoryWorkBudget


def consume_changes(*args: Any, **kwargs: Any):
    """Lazy compatibility seam for the optional NTFS/USN reconciler."""

    from neocortex.enumeration.ntfs.journal import consume_changes as reader

    return reader(*args, **kwargs)
# endregion [01]

# region [02] Implementación


USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
MAX_CROSS_BATCH_DIRECTORY_RENAMES = 100_000


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    cursor: JournalCursor
    records_seen: int
    files_upserted: int
    files_removed: int
    requires_rescan: bool


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: str | Path, parent: str | Path) -> bool:
    path_value = _path_key(path)
    parent_value = _path_key(parent)
    try:
        return os.path.commonpath((path_value, parent_value)) == parent_value
    except ValueError:
        return False


def _is_in_scope(
    path: str | Path,
    root: Path,
    exclusion_policy: InventoryExclusionPolicy,
    *,
    leaf_is_directory: bool = False,
) -> bool:
    if not _is_within(path, root):
        return False
    if not leaf_is_directory and exclusion_policy.excludes_file(path):
        return False
    path_object = Path(os.path.abspath(os.fspath(path)))
    candidate = path_object if leaf_is_directory else path_object.parent
    root_key = _path_key(root)
    while _path_key(candidate) != root_key and _is_within(candidate, root):
        if exclusion_policy.excludes_directory(candidate):
            return False
        candidate = candidate.parent
    return True


@dataclass(slots=True)
class _ReconcileState:
    records_seen: int = 0
    files_upserted: int = 0
    files_removed: int = 0
    requires_rescan: bool = False
    directory_old_scope: dict[int, bool] = field(default_factory=dict)
    resolved_paths: dict[int, str | None] = field(default_factory=dict)


@dataclass(slots=True)
class _BatchChanges:
    upserts: dict[tuple[int, int], FileSnapshot] = field(default_factory=dict)
    remove_paths: set[str] = field(default_factory=set)
    remove_identities: set[tuple[int, int]] = field(default_factory=set)


def _resolve_path(reader: Any, file_id: int, state: _ReconcileState) -> str | None:
    if file_id not in state.resolved_paths:
        try:
            state.resolved_paths[file_id] = reader.resolve_path(file_id)
        except (VolumeAccessError, OSError):
            state.resolved_paths[file_id] = None
    return state.resolved_paths[file_id]


def _record_path(
    reader: Any,
    record: NtfsEntry,
    state: _ReconcileState,
) -> str | None:
    parent_path = _resolve_path(reader, record.parent_reference_number, state)
    if parent_path is None:
        # Advancing beyond an event whose scope cannot be resolved can lose a
        # newly created or renamed in-scope identity permanently.  Abstain and
        # force a full scan even when the identity was not known previously.
        state.requires_rescan = True
        return None
    return os.path.join(parent_path, record.name)


def _process_directory_record(
    record: NtfsEntry,
    record_path: str | None,
    root: Path,
    exclusion_policy: InventoryExclusionPolicy,
    state: _ReconcileState,
) -> None:
    if record.reason & USN_REASON_RENAME_OLD_NAME:
        old_scope = record_path is not None and _is_in_scope(
            record_path,
            root,
            exclusion_policy,
            leaf_is_directory=True,
        )
        state.directory_old_scope[record.file_reference_number] = old_scope
        state.requires_rescan |= old_scope
    elif record.reason & USN_REASON_RENAME_NEW_NAME:
        new_scope = record_path is not None and _is_in_scope(
            record_path,
            root,
            exclusion_policy,
            leaf_is_directory=True,
        )
        state.requires_rescan |= new_scope or state.directory_old_scope.pop(
            record.file_reference_number, False
        )
    elif (
        record.reason & USN_REASON_FILE_DELETE
        and record_path is not None
        and _is_in_scope(
            record_path,
            root,
            exclusion_policy,
            leaf_is_directory=True,
        )
    ):
        state.requires_rescan = True


def _process_file_record(
    index: DedupIndex,
    scan_id: int,
    record: NtfsEntry,
    record_path: str | None,
    identity: tuple[int, int],
    root: Path,
    exclusion_policy: InventoryExclusionPolicy,
    changes: _BatchChanges,
    state: _ReconcileState,
) -> None:
    if record.reason & USN_REASON_RENAME_OLD_NAME:
        if record_path is not None and _is_in_scope(
            record_path,
            root,
            exclusion_policy,
        ):
            changes.remove_paths.add(record_path)
        return
    if record.reason & USN_REASON_FILE_DELETE:
        if record_path is not None and _is_in_scope(
            record_path,
            root,
            exclusion_policy,
        ):
            changes.remove_identities.add(identity)
        elif index.contains_identity(scan_id, *identity):
            changes.remove_identities.add(identity)
        changes.upserts.pop(identity, None)
        return
    if record_path is None:
        if index.contains_identity(scan_id, *identity):
            state.requires_rescan = True
        return
    if not _is_in_scope(record_path, root, exclusion_policy):
        return
    try:
        snapshot = snapshot_path(record_path)
    except (OSError, FileChangedError):
        state.requires_rescan = True
        return
    changes.upserts[identity] = snapshot
    changes.remove_identities.discard(identity)


def _process_usn_record(
    index: DedupIndex,
    scan_id: int,
    reader: Any,
    record: NtfsEntry,
    volume_id: int,
    root: Path,
    exclusion_policy: InventoryExclusionPolicy,
    changes: _BatchChanges,
    state: _ReconcileState,
) -> None:
    state.records_seen += 1
    identity = (volume_id, record.file_reference_number)
    record_path = _record_path(reader, record, state)
    if record.file_attributes & FILE_ATTRIBUTE_DIRECTORY:
        _process_directory_record(
            record,
            record_path,
            root,
            exclusion_policy,
            state,
        )
        return
    _process_file_record(
        index,
        scan_id,
        record,
        record_path,
        identity,
        root,
        exclusion_policy,
        changes,
        state,
    )


def _apply_reconcile_batch(
    index: DedupIndex,
    scan_id: int,
    root: Path,
    batch: UsnChangeBatch,
    changes: _BatchChanges,
    state: _ReconcileState,
    *,
    persist_checkpoint: bool,
    inventory_policy_signature: str,
    progress: ProgressCallback | None,
) -> bool:
    if len(state.directory_old_scope) > MAX_CROSS_BATCH_DIRECTORY_RENAMES:
        state.requires_rescan = True
        state.directory_old_scope.clear()
    if state.requires_rescan:
        state.resolved_paths.clear()
        return False
    checkpoint = (
        InventoryCheckpoint(
            str(root),
            scan_id,
            batch.cursor_after.volume,
            batch.cursor_after.journal_id,
            batch.cursor_after.next_usn,
            True,
            inventory_policy_signature,
        )
        if persist_checkpoint
        else None
    )
    index.apply_reconciliation(
        scan_id,
        upserts=changes.upserts.values(),
        remove_paths=changes.remove_paths,
        remove_identities=changes.remove_identities,
        checkpoint=checkpoint,
    )
    state.files_upserted += len(changes.upserts)
    state.files_removed += len(changes.remove_paths) + len(changes.remove_identities)
    state.resolved_paths.clear()
    emit_progress(
        progress,
        ProgressEvent(
            "framework",
            "reconcile",
            "Reconciliando cambios USN",
            state.records_seen,
            unit="registros",
        ),
    )
    return True


def reconcile_usn_window(
    index: DedupIndex,
    scan_id: int,
    root: Path,
    start: JournalCursor,
    target: JournalCursor,
    *,
    progress: ProgressCallback | None = None,
    persist_checkpoint: bool = False,
    excluded_paths: Iterable[str | Path] | None = None,
    exclusion_policy: InventoryExclusionPolicy | None = None,
    work_budget: InventoryWorkBudget | None = None,
) -> ReconcileResult:
    """Apply file changes through *target* and flag unsafe directory moves."""

    if work_budget is not None:
        work_budget.checkpoint()
    if start.volume != target.volume or start.journal_id != target.journal_id:
        raise RuntimeError("USN reconciliation boundaries do not share one journal")
    state = _ReconcileState()
    volume_id = os.stat(root, follow_symlinks=False).st_dev
    effective_policy = resolve_inventory_exclusion_policy(
        excluded_paths,
        exclusion_policy,
    )
    index.require_scan_inventory_policy_signature(
        scan_id,
        effective_policy.signature,
    )

    emit_progress(
        progress,
        ProgressEvent("framework", "reconcile", "Reconciliando cambios USN", 0, unit="registros"),
    )

    cursor = start
    with consume_changes(
        start.volume,
        start,
        timeout_seconds=0,
        bytes_to_wait_for=0,
    ) as reader:
        for batch in reader.iter_until(target.next_usn):
            if work_budget is not None:
                work_budget.checkpoint()
            changes = _BatchChanges()
            for record in batch.records:
                if work_budget is not None:
                    work_budget.checkpoint()
                _process_usn_record(
                    index,
                    scan_id,
                    reader,
                    record,
                    volume_id,
                    root,
                    effective_policy,
                    changes,
                    state,
                )
            if work_budget is not None:
                work_budget.checkpoint()
            safe_batch = _apply_reconcile_batch(
                index,
                scan_id,
                root,
                batch,
                changes,
                state,
                persist_checkpoint=persist_checkpoint,
                inventory_policy_signature=effective_policy.signature,
                progress=progress,
            )
            if not safe_batch:
                break
            cursor = batch.cursor_after

    emit_progress(
        progress,
        ProgressEvent(
            "framework",
            "reconcile",
            "Cambios USN reconciliados",
            state.records_seen,
            state.records_seen,
            "registros",
            True,
        ),
    )
    return ReconcileResult(
        cursor,
        state.records_seen,
        state.files_upserted,
        state.files_removed,
        state.requires_rescan,
    )


# endregion [02]
