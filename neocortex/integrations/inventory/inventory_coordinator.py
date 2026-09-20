"""Prepare one durable, metadata-first portable inventory generation.

NeoCortex is Linux/Kubuntu-only. Inventory never probes or replays a platform
journal: a complete portable observation is the sole inventory mode. The
legacy ``journal_before`` and ``allow_incremental`` parameters remain in the
call shape temporarily so the lifecycle owner can remove them separately
without creating a second inventory path here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from neocortex.deduplication import (
    DedupIndex,
    InventoryCheckpoint,
    InventoryError,
    InventoryExclusionPolicy,
    InventoryWorkBudget,
    ScanSummary,
)
from neocortex.deduplication.inventory.portable import prepare_portable_inventory
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.progress import ProgressCallback


class _WorkBudgetOptions(TypedDict, total=False):
    work_budget: InventoryWorkBudget


def _work_budget_options(work_budget: InventoryWorkBudget | None) -> _WorkBudgetOptions:
    return {} if work_budget is None else {"work_budget": work_budget}


@dataclass(frozen=True, slots=True)
class PreparedInventory:
    scan: ScanSummary
    journal_before: None
    reconciliation: None
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: Literal["full"]
    inventory_policy_signature: str
    observed_files: int = 0
    changed_files: int = 0
    persistent_file_rows_written: int = 0
    reused_generation: bool = False


def _portable_inventory(
    index: DedupIndex,
    root: Path,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
    publish_checkpoint: bool,
    work_budget: InventoryWorkBudget | None,
) -> PreparedInventory:
    if work_budget is not None:
        work_budget.checkpoint()
    options = _work_budget_options(work_budget)
    observation = None
    if publish_checkpoint:
        observation = prepare_portable_inventory(
            index,
            root,
            exclusion_policy=exclusion_policy,
            progress=progress,
            **options,
        )
        scan = observation.scan
    else:
        scan = index.scan(
            root,
            exclusion_policy=exclusion_policy,
            progress=progress,
            **options,
        )
    if work_budget is not None:
        work_budget.checkpoint()
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
        observed_files=(scan.files_seen if observation is None else observation.observed_files),
        changed_files=(scan.files_seen if observation is None else observation.changed_files),
        persistent_file_rows_written=(
            scan.files_seen if observation is None else observation.persistent_file_rows_written
        ),
        reused_generation=observation is not None and observation.reused_generation,
    )


def prepare_inventory(
    index: DedupIndex,
    state: FrameworkState,
    run_id: int,
    root: Path,
    journal_before: object | None = None,
    *,
    progress: ProgressCallback,
    exclusion_policy: InventoryExclusionPolicy,
    allow_incremental: bool = True,
    publish_portable_checkpoint: bool = False,
    work_budget: InventoryWorkBudget | None = None,
) -> PreparedInventory:
    """Prepare one complete portable inventory and publish no journal cursor."""

    del journal_before, allow_incremental
    if work_budget is not None:
        if not isinstance(work_budget, InventoryWorkBudget):
            raise TypeError("work_budget must be an InventoryWorkBudget")
        work_budget.checkpoint()
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

    prepared = _portable_inventory(
        index,
        root,
        progress=progress,
        exclusion_policy=exclusion_policy,
        publish_checkpoint=publish_portable_checkpoint,
        work_budget=work_budget,
    )
    if work_budget is not None:
        work_budget.checkpoint()

    protected_scan_ids = set(state.referenced_inventory_scan_ids())
    protected_scan_ids.update(recovered_scan_ids)
    removed_state = index.prune_obsolete_state(protected_scan_ids=sorted(protected_scan_ids))
    state.update_run_start_cursor(run_id, None)
    state.record_event(
        run_id,
        "info",
        "inventory",
        "Inventario portable preparado",
        {
            "schema": "neocortex.inventory-prepared/v1",
            "mode": prepared.inventory_mode,
            "journal_status": "unavailable_portable_only",
            "scan_id": prepared.scan.scan_id,
            "inventory_policy_signature": prepared.inventory_policy_signature,
            "files": prepared.scan.files_seen,
            "reconciliation_records": 0,
            "attempts": prepared.inventory_attempts,
            "pruned": removed_state,
            "elapsed_ns": time.perf_counter_ns() - started,
            "observed_files": prepared.observed_files,
            "changed_files": prepared.changed_files,
            "persistent_file_rows_written": prepared.persistent_file_rows_written,
            "reused_generation": prepared.reused_generation,
        },
    )
    return prepared


__all__ = ["PreparedInventory", "prepare_inventory"]
