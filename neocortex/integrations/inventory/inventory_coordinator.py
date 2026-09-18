"""USN-aware preparation of one durable inventory generation."""

from __future__ import annotations
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from neocortex.enumeration.errors import JournalDiscontinuityError, NtfsUsnError
from neocortex.enumeration.models import JournalCursor
from neocortex.deduplication import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryError,
    InventoryExclusionPolicy,
    ScanSummary,
)
from neocortex.progress import ProgressCallback, ProgressEvent, emit_progress

from neocortex.integrations.inventory.reconcile import reconcile_usn_window
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.deduplication.inventory.portable import prepare_portable_inventory

if TYPE_CHECKING:
    from neocortex.integrations.inventory.reconcile import ReconcileResult


def query_journal_cursor(volume: str) -> JournalCursor:
    """Lazy compatibility seam for the optional NTFS/USN provider."""

    from neocortex.enumeration.ntfs.enumeration import query_journal_cursor as reader

    return reader(volume)


# region [01] Inventory result contract

MAX_INVENTORY_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PreparedInventory:
    scan: ScanSummary
    journal_before: JournalCursor | None
    reconciliation: ReconcileResult | None
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: Literal["full", "incremental"]
    inventory_policy_signature: str
    observed_files: int = 0
    changed_files: int = 0
    persistent_file_rows_written: int = 0
    reused_generation: bool = False


# endregion [01]


# region [02] Incremental checkpoint reuse


def _checkpoint_cursor(
    index: DedupIndex,
    root: Path,
    journal_before: JournalCursor,
    inventory_policy_signature: str,
) -> tuple[InventoryCheckpoint | None, JournalCursor | None]:
    checkpoint = index.inventory_checkpoint(root)
    if not (
        checkpoint is not None
        and checkpoint.valid
        and checkpoint.inventory_policy_signature == inventory_policy_signature
        and checkpoint.volume is not None
        and checkpoint.journal_id is not None
        and checkpoint.next_usn is not None
        and checkpoint.volume == journal_before.volume
        and checkpoint.journal_id == journal_before.journal_id
        and checkpoint.next_usn is not None
        and checkpoint.next_usn <= journal_before.next_usn
    ):
        return checkpoint, None
    return checkpoint, JournalCursor(
        checkpoint.volume,
        checkpoint.journal_id,
        checkpoint.next_usn,
    )


def _try_incremental_inventory(
    index: DedupIndex,
    root: Path,
    journal_before: JournalCursor,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
) -> PreparedInventory | None:
    checkpoint, cursor = _checkpoint_cursor(
        index,
        root,
        journal_before,
        exclusion_policy.signature,
    )
    if checkpoint is None or cursor is None:
        return None
    try:
        reconciliation = reconcile_usn_window(
            index,
            checkpoint.scan_id,
            root,
            cursor,
            journal_before,
            progress=progress,
            persist_checkpoint=True,
            exclusion_policy=exclusion_policy,
        )
    except JournalDiscontinuityError:
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                checkpoint.root,
                checkpoint.scan_id,
                checkpoint.volume,
                checkpoint.journal_id,
                checkpoint.next_usn,
                False,
                checkpoint.inventory_policy_signature,
            )
        )
        return None
    if reconciliation.requires_rescan:
        return None
    index.refresh_scan_aggregates(checkpoint.scan_id)
    return PreparedInventory(
        scan=index.scan_summary(checkpoint.scan_id),
        journal_before=cursor,
        reconciliation=reconciliation,
        reconciliation_records=reconciliation.records_seen,
        inventory_attempts=0,
        inventory_mode="incremental",
        inventory_policy_signature=exclusion_policy.signature,
    )


# endregion [02]


# region [03] Full inventory with finite USN reconciliation


def _full_inventory_without_journal(
    index: DedupIndex,
    root: Path,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
    publish_checkpoint: bool,
) -> PreparedInventory:
    """Capture one honest portable snapshot without inventing a USN cursor."""

    observation = None
    # ``allow_incremental`` gates journal replay, not reuse after a complete
    # portable filesystem observation. Linux still observes every file.
    if publish_checkpoint:
        observation = prepare_portable_inventory(index, root, exclusion_policy=exclusion_policy, progress=progress)
        scan = observation.scan
    else:
        scan = index.scan(root, exclusion_policy=exclusion_policy, progress=progress)
    if scan.errors:
        raise InventoryError(
            f"inventory scan {scan.scan_id} was partial with "
            f"{scan.errors} traversal errors; no checkpoint was published"
        )
    if observation is None or not observation.reused_generation:
        index.refresh_scan_aggregates(scan.scan_id)
    if publish_checkpoint:
        index.bind_inventory_checkpoint(
            InventoryCheckpoint(
                str(root),
                scan.scan_id,
                None,
                None,
                None,
                True,
                exclusion_policy.signature,
            )
        )
    return PreparedInventory(
        scan=index.scan_summary(scan.scan_id),
        journal_before=None,
        reconciliation=None,
        reconciliation_records=0,
        inventory_attempts=1,
        inventory_mode="full",
        inventory_policy_signature=exclusion_policy.signature,
        observed_files=scan.files_seen if observation is None else observation.observed_files,
        changed_files=scan.files_seen if observation is None else observation.changed_files,
        persistent_file_rows_written=scan.files_seen if observation is None else observation.persistent_file_rows_written,
        reused_generation=observation is not None and observation.reused_generation,
    )


def _full_inventory(
    index: DedupIndex,
    root: Path,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
) -> PreparedInventory:
    attempt_cursor = query_journal_cursor(root.drive)
    reconciliation_records = 0
    for attempt in range(1, MAX_INVENTORY_ATTEMPTS + 1):
        scan = index.scan(
            root,
            exclusion_policy=exclusion_policy,
            progress=progress,
        )
        if scan.errors:
            raise InventoryError(
                f"inventory scan {scan.scan_id} was partial with "
                f"{scan.errors} traversal errors; the prior checkpoint was retained"
            )
        target_cursor = query_journal_cursor(root.drive)
        reconciliation = reconcile_usn_window(
            index,
            scan.scan_id,
            root,
            attempt_cursor,
            target_cursor,
            progress=progress,
            exclusion_policy=exclusion_policy,
        )
        reconciliation_records += reconciliation.records_seen
        if not reconciliation.requires_rescan:
            index.refresh_scan_aggregates(scan.scan_id)
            index.bind_inventory_checkpoint(
                InventoryCheckpoint(
                    str(root),
                    scan.scan_id,
                    reconciliation.cursor.volume,
                    reconciliation.cursor.journal_id,
                    reconciliation.cursor.next_usn,
                    True,
                    exclusion_policy.signature,
                )
            )
            return PreparedInventory(
                scan=index.scan_summary(scan.scan_id),
                journal_before=attempt_cursor,
                reconciliation=reconciliation,
                reconciliation_records=reconciliation_records,
                inventory_attempts=attempt,
                inventory_mode="full",
                inventory_policy_signature=exclusion_policy.signature,
            )
        if attempt == MAX_INVENTORY_ATTEMPTS:
            raise RuntimeError("directory structure changed during all inventory attempts")
        emit_progress(
            progress,
            ProgressEvent(
                "framework",
                "retry",
                "Repitiendo inventario por cambio de directorio",
                attempt,
                MAX_INVENTORY_ATTEMPTS,
                "intentos",
            ),
        )
        attempt_cursor = query_journal_cursor(root.drive)
    raise RuntimeError("unreachable inventory retry state")


# endregion [03]


# region [04] Public preparation entry point


def prepare_inventory(
    index: DedupIndex,
    state: FrameworkState,
    run_id: int,
    root: Path,
    journal_before: JournalCursor | None,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
    allow_incremental: bool = True,
    publish_portable_checkpoint: bool = False,
) -> PreparedInventory:
    started = time.perf_counter_ns()
    recovered_scan_ids = index.mark_abandoned_scans()
    if recovered_scan_ids:
        state.record_event(
            run_id,
            "warning",
            "inventory-recovery",
            "Inventarios interrumpidos conservados como parciales",
            {
                "count": len(recovered_scan_ids),
                "scan_ids": list(recovered_scan_ids[:32]),
                "scan_ids_truncated": len(recovered_scan_ids) > 32,
            },
        )
    prepared = None
    if journal_before is None:
        prepared = _full_inventory_without_journal(
            index,
            root,
            progress=progress,
            exclusion_policy=exclusion_policy,
            publish_checkpoint=publish_portable_checkpoint,
        )
    else:
        try:
            if allow_incremental:
                prepared = _try_incremental_inventory(
                    index,
                    root,
                    journal_before,
                    progress=progress,
                    exclusion_policy=exclusion_policy,
                )
            if prepared is None:
                prepared = _full_inventory(
                    index,
                    root,
                    progress=progress,
                    exclusion_policy=exclusion_policy,
                )
        except (NtfsUsnError, OSError) as exc:
            state.record_event(
                run_id,
                "warning",
                "inventory-journal",
                "USN no disponible; usando snapshot portable",
                {
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
            )
            prepared = _full_inventory_without_journal(
                index,
                root,
                progress=progress,
                exclusion_policy=exclusion_policy,
                publish_checkpoint=publish_portable_checkpoint,
            )

    protected_scan_ids = set(state.referenced_inventory_scan_ids())
    protected_scan_ids.update(recovered_scan_ids)
    removed_state = index.prune_obsolete_state(protected_scan_ids=sorted(protected_scan_ids))
    state.update_run_start_cursor(run_id, prepared.journal_before)
    state.record_event(
        run_id,
        "info",
        "inventory",
        "Inventario preparado",
        {
            "schema": "neocortex.inventory-prepared/v1",
            "mode": prepared.inventory_mode,
            "journal_status": (
                "available" if prepared.journal_before is not None else "unavailable"
            ),
            "scan_id": prepared.scan.scan_id,
            "inventory_policy_signature": prepared.inventory_policy_signature,
            "files": prepared.scan.files_seen,
            "reconciliation_records": prepared.reconciliation_records,
            "attempts": prepared.inventory_attempts,
            "pruned": removed_state,
            "elapsed_ns": time.perf_counter_ns() - started,
            "observed_files": prepared.observed_files,
            "changed_files": prepared.changed_files,
            "persistent_file_rows_written": prepared.persistent_file_rows_written,
            "reused_generation": prepared.reused_generation,
        },
    )
    if prepared.inventory_mode == "incremental":
        emit_progress(
            progress,
            ProgressEvent(
                "framework",
                "inventory",
                "Inventario vigente confirmado",
                prepared.scan.files_seen,
                prepared.scan.files_seen,
                "archivos",
                True,
            ),
        )
    return prepared


# endregion [04]
