"""Explicit, revalidated and resumable document-organization application."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_organization_application.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform_policy import stat_birthtime_ns

from _02_Deduplicacion import FileSnapshot, snapshot_path
from _03_Progreso import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from .action_policy import (
    protected_path_reason,
    same_snapshot,
    validate_descendant_path,
    validate_mutation_path,
)
from .corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    ProtectedAnalysisRootError,
    path_trees_intersect,
)
from .document_cache_sync import synchronize_moved_document
from .document_catalog import document_catalog_database, initialize_document_catalog
from .document_organization_models import (
    ORGANIZATION_APPLY_BATCH_SIZE,
    ORGANIZATION_PROGRESS_INTERVAL,
    OrganizationApplyProgress,
    OrganizationApplyProgressCallback,
    OrganizationApplySummary,
    _ApplyRowOutcome,
    _begin_organization_run,
    _complete_organization_run,
    _fail_organization_run,
)
from .document_organization_planning import (
    _reject_state_destination,
    _resolve_plan_destination,
    _same_path,
    _validate_destination,
)
from .internal_paths import InternalPathProtectionError
from .protected_content import ProtectedContentError
from .windows_handle_mutation import (
    IdentityBoundMutationError,
    MutationEffectUncertainError,
    UnsupportedIdentityBoundMutation,
    rename_no_replace_by_identity,
)
# endregion [01]

# region [02] Implementación


@dataclass(slots=True)
class _OrganizationApplyCounters:
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    cache_synced: int = 0
    cache_pending: int = 0

    def record(self, outcome: _ApplyRowOutcome) -> None:
        if outcome.cache_synced:
            self.applied += 1
            self.cache_synced += 1
        elif outcome.cache_pending:
            self.cache_pending += 1
        elif outcome.status == "stale":
            self.stale += 1
        elif outcome.status == "blocked":
            self.blocked += 1
        else:
            self.failed += 1

    def progress(self, selected: int) -> OrganizationApplyProgress:
        return OrganizationApplyProgress(
            selected=selected,
            applied=self.applied,
            stale=self.stale,
            blocked=self.blocked,
            failed=self.failed,
            cache_synced=self.cache_synced,
        )

    def summary(
        self,
        *,
        run_id: int,
        selected: int,
        remaining: int,
    ) -> OrganizationApplySummary:
        return OrganizationApplySummary(
            catalog_run_id=run_id,
            selected=selected,
            applied=self.applied,
            stale=self.stale,
            blocked=self.blocked,
            failed=self.failed,
            cache_synced=self.cache_synced,
            cache_pending=self.cache_pending,
            remaining=remaining,
        )


def apply_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    max_actions: int = 100,
    on_progress: OrganizationApplyProgressCallback | None = None,
) -> OrganizationApplySummary:
    """Apply plans and mark them complete only after every cache is synchronized."""

    root = _validated_organization_apply_request(
        catalog_path,
        organization_root,
        mutation_guard=mutation_guard,
        max_actions=max_actions,
    )
    with document_catalog_database(catalog_path) as connection:
        run_id = _begin_organization_run(connection, "apply", root)
        rows = _select_organization_apply_rows(connection, root, max_actions)
        try:
            return _execute_organization_apply_run(
                connection,
                catalog_path,
                root,
                run_id,
                rows,
                mutation_guard,
                on_progress,
            )
        except BaseException as exc:
            _fail_organization_run(connection, run_id, exc)
            raise


def _validated_organization_apply_request(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    max_actions: int,
) -> Path:
    mutation_guard.reject_run_mutation()
    if max_actions < 1:
        raise ValueError("max_actions must be positive")
    root = Path(os.path.abspath(organization_root.expanduser()))
    if not catalog_path.is_file():
        raise FileNotFoundError(f"document catalog does not exist: {catalog_path}")
    root_reason = protected_path_reason(root, check_attributes=False)
    if root_reason is not None:
        raise ValueError(f"organization root is protected: {root_reason}")
    _reject_state_destination(catalog_path, root)
    initialize_document_catalog(catalog_path)
    return root


def _select_organization_apply_rows(
    connection: sqlite3.Connection,
    root: Path,
    max_actions: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT * FROM organization_plans
        WHERE organization_root=?
        AND status IN ('planned','applying','moved_cache_pending')
        ORDER BY CASE status
            WHEN 'applying' THEN 0
            WHEN 'planned' THEN 1
            ELSE 2 END,plan_id LIMIT ?""",
        (str(root), max_actions),
    ).fetchall()


def _execute_organization_apply_run(
    connection: sqlite3.Connection,
    catalog_path: Path,
    root: Path,
    run_id: int,
    rows: list[sqlite3.Row],
    mutation_guard: CorpusMutationGuard,
    on_progress: OrganizationApplyProgressCallback | None,
) -> OrganizationApplySummary:
    protected_denials, root_stat = _prepare_selected_organization_plans(
        catalog_path,
        root,
        rows,
        mutation_guard,
    )
    counters = _OrganizationApplyCounters()
    _apply_selected_organization_rows(
        connection,
        catalog_path,
        root,
        root_stat,
        rows,
        protected_denials,
        mutation_guard,
        counters,
        on_progress,
    )
    summary = counters.summary(
        run_id=run_id,
        selected=len(rows),
        remaining=_remaining_organization_apply_rows(connection, root),
    )
    _complete_organization_run(connection, run_id, summary)
    return summary


def _prepare_selected_organization_plans(
    catalog_path: Path,
    root: Path,
    rows: list[sqlite3.Row],
    mutation_guard: CorpusMutationGuard,
) -> tuple[dict[str, str], os.stat_result | None]:
    protected_denials = _protected_organization_plan_denials(rows, mutation_guard)
    admitted_rows = [row for row in rows if str(row["plan_id"]) not in protected_denials]
    if not admitted_rows:
        return protected_denials, None
    _preflight_selected_organization_boundaries(
        catalog_path.parent,
        root,
        admitted_rows,
        mutation_guard,
    )
    return protected_denials, _prepare_apply_root(catalog_path, root, mutation_guard)


def _apply_selected_organization_rows(
    connection: sqlite3.Connection,
    catalog_path: Path,
    root: Path,
    root_stat: os.stat_result | None,
    rows: list[sqlite3.Row],
    protected_denials: dict[str, str],
    mutation_guard: CorpusMutationGuard,
    counters: _OrganizationApplyCounters,
    on_progress: OrganizationApplyProgressCallback | None,
) -> None:
    for selected_index, row in enumerate(rows, start=1):
        outcome = _apply_organization_row(
            connection,
            catalog_path,
            root,
            root_stat,
            row,
            protected_denials,
            mutation_guard,
        )
        counters.record(outcome)
        connection.commit()
        _report_organization_apply_progress(
            on_progress,
            counters,
            selected_index=selected_index,
            selected_total=len(rows),
        )


def _apply_organization_row(
    connection: sqlite3.Connection,
    catalog_path: Path,
    root: Path,
    root_stat: os.stat_result | None,
    row: sqlite3.Row,
    protected_denials: dict[str, str],
    mutation_guard: CorpusMutationGuard,
) -> _ApplyRowOutcome:
    plan_id = str(row["plan_id"])
    if plan_id in protected_denials:
        return _record_protected_organization_plan(
            connection,
            row,
            protected_denials[plan_id],
        )
    if root_stat is None:
        raise RuntimeError("organization root was not prepared")
    return _apply_selected_organization_plan(
        connection,
        catalog_path,
        row,
        root,
        root_stat,
        mutation_guard,
    )


def _report_organization_apply_progress(
    on_progress: OrganizationApplyProgressCallback | None,
    counters: _OrganizationApplyCounters,
    *,
    selected_index: int,
    selected_total: int,
) -> None:
    if on_progress is None:
        return
    if selected_index % ORGANIZATION_PROGRESS_INTERVAL != 0 and selected_index != selected_total:
        return
    on_progress(counters.progress(selected_index))


def _remaining_organization_apply_rows(
    connection: sqlite3.Connection,
    root: Path,
) -> int:
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM organization_plans
            WHERE organization_root=?
            AND status IN ('planned','applying','moved_cache_pending')""",
            (str(root),),
        ).fetchone()[0]
    )


def _lexical_path_trees_intersect(left: Path, right: Path) -> bool:
    left_path = Path(os.path.abspath(os.path.normpath(left)))
    right_path = Path(os.path.abspath(os.path.normpath(right)))
    return (
        left_path == right_path
        or left_path.is_relative_to(right_path)
        or right_path.is_relative_to(left_path)
    )


def _protected_organization_plan_denials(
    rows: list[sqlite3.Row],
    mutation_guard: CorpusMutationGuard,
) -> dict[str, str]:
    policy = mutation_guard.protected_content_policy
    if policy is None:
        return {}

    denials: dict[str, str] = {}
    for row in rows:
        source = Path(str(row["source_path"]))
        destination_value = row["destination_path"]
        paths = (source,) if destination_value is None else (source, Path(str(destination_value)))
        ordered_paths = tuple(
            sorted(
                paths,
                key=lambda path: (
                    not any(
                        _lexical_path_trees_intersect(path, entry.canonical_path)
                        for entry in policy.entries
                    )
                ),
            )
        )
        try:
            for path in ordered_paths:
                policy.require_mutation_paths_allowed(path)
        except ProtectedContentError as exc:
            denials[str(row["plan_id"])] = str(exc)
    return denials


def _record_protected_organization_plan(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    detail: str,
) -> _ApplyRowOutcome:
    connection.execute(
        """UPDATE organization_plans
        SET status='blocked',detail=?,completed_ns=?,
        cache_sync_status='not_required',cache_sync_error=NULL
        WHERE plan_id=?""",
        (detail, time.time_ns(), row["plan_id"]),
    )
    return _ApplyRowOutcome("blocked")


def _apply_selected_organization_plan(
    connection: sqlite3.Connection,
    catalog_path: Path,
    row: sqlite3.Row,
    root: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> _ApplyRowOutcome:
    status = str(row["status"])
    detail = str(row["detail"] or "")
    if status != "moved_cache_pending":
        row = _disambiguate_apply_destination(connection, row, mutation_guard)
        status = str(row["status"])
        if _catalog_destination_conflict(connection, row):
            status = "blocked"
            detail = "destination belongs to another active catalog row"
        else:
            if status == "planned":
                connection.execute(
                    """UPDATE organization_plans
                    SET status='applying',detail=?,completed_ns=NULL
                    WHERE plan_id=? AND status='planned'""",
                    (
                        "durable apply intent recorded before filesystem move",
                        row["plan_id"],
                    ),
                )
                connection.commit()
            status, detail = _apply_one_plan(
                row,
                catalog_path.parent,
                root,
                root_stat,
                mutation_guard,
            )
        if status == "moved":
            _record_moved_path(connection, row, detail)
            connection.commit()
            status = "moved_cache_pending"
    if status == "moved_cache_pending":
        return _synchronize_applied_organization_plan(
            connection,
            catalog_path,
            row,
        )
    completed_ns = None if status == "recovery_required" else time.time_ns()
    connection.execute(
        """UPDATE organization_plans
        SET status=?,detail=?,completed_ns=?,
        cache_sync_status='not_required',cache_sync_error=NULL
        WHERE plan_id=?""",
        (status, detail, completed_ns, row["plan_id"]),
    )
    return _ApplyRowOutcome(status)


def _synchronize_applied_organization_plan(
    connection: sqlite3.Connection,
    catalog_path: Path,
    row: sqlite3.Row,
) -> _ApplyRowOutcome:
    sync = synchronize_moved_document(
        catalog_path.parent,
        source_kind=str(row["source_kind"]),
        file_key=str(row["file_key"]),
        old_path=str(row["source_path"]),
        new_path=str(row["destination_path"]),
        volume_id=str(row["volume_id"]),
        file_id=str(row["file_id"]),
    )
    if sync.complete:
        connection.execute(
            """UPDATE organization_plans
            SET status='applied',detail=?,completed_ns=?,
            cache_sync_status='synced',cache_sync_json=?,
            cache_sync_error=NULL WHERE plan_id=?""",
            (
                "filesystem move and cache synchronization completed",
                time.time_ns(),
                sync.as_json(),
                row["plan_id"],
            ),
        )
        return _ApplyRowOutcome("moved_cache_pending", cache_synced=True)
    connection.execute(
        """UPDATE organization_plans
        SET status='moved_cache_pending',detail=?,completed_ns=NULL,
        cache_sync_status='pending',cache_sync_json=?,
        cache_sync_error=? WHERE plan_id=?""",
        (
            "filesystem move completed; cache synchronization pending",
            sync.as_json(),
            sync.error_message,
            row["plan_id"],
        ),
    )
    return _ApplyRowOutcome("moved_cache_pending", cache_pending=True)


def apply_all_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    batch_size: int = ORGANIZATION_APPLY_BATCH_SIZE,
    progress: ProgressCallback | None = None,
    progress_operation: str = "framework",
) -> OrganizationApplySummary:
    """Consume every actionable plan in bounded, resumable apply batches."""

    mutation_guard.reject_run_mutation()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    selected = applied = stale = blocked = failed = cache_synced = 0
    batches = 0
    last_run_id = 0
    remaining = 0
    total = _organization_actionable_count(catalog_path, organization_root)
    _emit_organization_apply_progress(
        progress,
        operation=progress_operation,
        completed=0,
        total=total,
        applied=0,
        stale=0,
        blocked=0,
        failed=0,
        cache_synced=0,
        remaining=total,
    )
    while True:
        selected_before = selected
        applied_before = applied
        stale_before = stale
        blocked_before = blocked
        failed_before = failed
        cache_synced_before = cache_synced
        report_batch = _organization_apply_batch_reporter(
            progress,
            operation=progress_operation,
            total=total,
            selected_before=selected_before,
            applied_before=applied_before,
            stale_before=stale_before,
            blocked_before=blocked_before,
            failed_before=failed_before,
            cache_synced_before=cache_synced_before,
        )

        current = apply_document_organization(
            catalog_path,
            organization_root,
            mutation_guard=mutation_guard,
            max_actions=batch_size,
            on_progress=report_batch,
        )
        batches += 1
        last_run_id = current.catalog_run_id
        selected += current.selected
        applied += current.applied
        stale += current.stale
        blocked += current.blocked
        failed += current.failed
        cache_synced += current.cache_synced
        remaining = current.remaining
        finalized = current.applied + current.stale + current.blocked + current.failed
        if remaining == 0 or current.selected == 0:
            break
        if finalized == 0 and not _has_ready_organization_plans(
            catalog_path,
            organization_root,
        ):
            break
    summary = OrganizationApplySummary(
        catalog_run_id=last_run_id,
        selected=selected,
        applied=applied,
        stale=stale,
        blocked=blocked,
        failed=failed,
        cache_synced=cache_synced,
        cache_pending=remaining,
        batches=batches,
        remaining=remaining,
    )
    _emit_organization_apply_progress(
        progress,
        operation=progress_operation,
        completed=selected,
        total=total,
        applied=applied,
        stale=stale,
        blocked=blocked,
        failed=failed,
        cache_synced=cache_synced,
        remaining=remaining,
        finished=True,
    )
    return summary


def _organization_apply_batch_reporter(
    progress: ProgressCallback | None,
    *,
    operation: str,
    total: int,
    selected_before: int,
    applied_before: int,
    stale_before: int,
    blocked_before: int,
    failed_before: int,
    cache_synced_before: int,
) -> OrganizationApplyProgressCallback:
    def report_batch(current: OrganizationApplyProgress) -> None:
        current_selected = selected_before + current.selected
        _emit_organization_apply_progress(
            progress,
            operation=operation,
            completed=current_selected,
            total=total,
            applied=applied_before + current.applied,
            stale=stale_before + current.stale,
            blocked=blocked_before + current.blocked,
            failed=failed_before + current.failed,
            cache_synced=cache_synced_before + current.cache_synced,
            remaining=max(0, total - current_selected),
        )

    return report_batch


def _organization_actionable_count(
    catalog_path: Path,
    organization_root: Path,
) -> int:
    if not catalog_path.is_file():
        raise FileNotFoundError(f"document catalog does not exist: {catalog_path}")
    root = Path(os.path.abspath(organization_root.expanduser()))
    with document_catalog_database(catalog_path, readonly=True) as connection:
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM organization_plans
                WHERE organization_root=?
                AND status IN ('planned','applying','moved_cache_pending')""",
                (str(root),),
            ).fetchone()[0]
        )


def _emit_organization_apply_progress(
    progress: ProgressCallback | None,
    *,
    operation: str,
    completed: int,
    total: int,
    applied: int,
    stale: int,
    blocked: int,
    failed: int,
    cache_synced: int,
    remaining: int,
    finished: bool = False,
) -> None:
    unresolved = stale + blocked + failed + remaining
    description = (
        "Organización técnica aplicada"
        if finished and not unresolved
        else (
            "Organización técnica aplicada con pendientes"
            if finished
            else "Moviendo y sincronizando documentos técnicos"
        )
    )
    emit_progress(
        progress,
        ProgressEvent(
            operation,
            "organization-apply",
            description,
            completed,
            total,
            "archivos",
            finished,
            (
                ProgressMetric("applied", applied),
                ProgressMetric("cache_synced", cache_synced),
                ProgressMetric("stale", stale),
                ProgressMetric("blocked", blocked),
                ProgressMetric("errors", failed),
                ProgressMetric("remaining", remaining),
            ),
        ),
    )


def _has_ready_organization_plans(
    catalog_path: Path,
    organization_root: Path,
) -> bool:
    """Distinguish untried plans from cache-pending moves that cannot progress."""

    root = Path(os.path.abspath(organization_root.expanduser()))
    with document_catalog_database(catalog_path, readonly=True) as connection:
        return bool(
            connection.execute(
                """SELECT EXISTS(SELECT 1 FROM organization_plans
                WHERE organization_root=? AND status IN ('planned','applying'))""",
                (str(root),),
            ).fetchone()[0]
        )


def _catalog_destination_conflict(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> bool:
    destination = row["destination_path"]
    if destination is None:
        return False
    conflict = connection.execute(
        """SELECT 1 FROM documents WHERE active=1 AND path=? COLLATE NOCASE
        AND NOT (source_kind=? AND file_key=?) LIMIT 1""",
        (destination, row["source_kind"], row["file_key"]),
    ).fetchone()
    return conflict is not None


def _record_moved_path(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    detail: str,
) -> None:
    destination = str(row["destination_path"])
    document = connection.execute(
        """SELECT path FROM documents WHERE source_kind=? AND file_key=?""",
        (row["source_kind"], row["file_key"]),
    ).fetchone()
    if document is None:
        raise RuntimeError("organization plan no longer has a catalog document")
    current = Path(str(document["path"]))
    if not _same_path(current, Path(destination)):
        if not _same_path(current, Path(str(row["source_path"]))):
            raise RuntimeError("catalog path is neither planned source nor destination")
        connection.execute(
            """UPDATE documents SET path=?,updated_ns=?
            WHERE source_kind=? AND file_key=?""",
            (
                destination,
                time.time_ns(),
                row["source_kind"],
                row["file_key"],
            ),
        )
    connection.execute(
        """UPDATE organization_plans
        SET status='moved_cache_pending',detail=?,move_completed_ns=?,
        completed_ns=NULL,cache_sync_status='pending',cache_sync_error=NULL
        WHERE plan_id=?""",
        (detail, time.time_ns(), row["plan_id"]),
    )


def _prepare_apply_root(
    catalog_path: Path,
    root: Path,
    mutation_guard: CorpusMutationGuard,
) -> os.stat_result:
    """Create only the final default directory during an explicit apply."""

    state_directory = catalog_path.parent
    _require_disjoint_path_trees(
        state_directory,
        root,
        detail="organization root and framework state directory",
    )
    if root.exists():
        return _validated_existing_apply_root(
            state_directory,
            root,
            mutation_guard,
        )
    parent, parent_stat = _validated_apply_root_parent(
        state_directory,
        root,
        mutation_guard,
    )
    return _create_validated_apply_root(
        state_directory,
        root,
        parent,
        parent_stat,
        mutation_guard,
    )


def _validated_existing_apply_root(
    state_directory: Path,
    root: Path,
    mutation_guard: CorpusMutationGuard,
) -> os.stat_result:
    _require_organization_tree_allowed(root, mutation_guard)
    if not root.is_dir():
        raise ValueError("organization root exists but is not a directory")
    if root.is_symlink() or _is_junction(root):
        raise ValueError("organization root cannot be a symlink or junction")
    root_reason = protected_path_reason(root)
    if root_reason is not None:
        raise ValueError(f"organization root is protected: {root_reason}")
    root_stat = os.stat(root, follow_symlinks=False)
    _require_disjoint_path_trees(
        state_directory,
        root,
        detail="organization root and framework state directory",
    )
    _require_directory_identity(root, root_stat, role="organization root")
    _require_organization_tree_allowed(root, mutation_guard)
    return root_stat


def _validated_apply_root_parent(
    state_directory: Path,
    root: Path,
    mutation_guard: CorpusMutationGuard,
) -> tuple[Path, os.stat_result]:
    parent = root.parent
    if not parent.is_dir():
        raise ValueError(
            "organization root parent must already exist; intermediate directories "
            "are not created automatically"
        )
    if parent.is_symlink() or _is_junction(parent):
        raise ValueError("organization root parent cannot be a symlink or junction")
    parent_reason = protected_path_reason(parent)
    if parent_reason is not None:
        raise ValueError(f"organization root parent is protected: {parent_reason}")
    parent_stat = os.stat(parent, follow_symlinks=False)
    _require_organization_tree_allowed(parent, mutation_guard)
    _require_disjoint_path_trees(
        state_directory,
        root,
        detail="organization root and framework state directory",
    )
    _require_directory_identity(
        parent,
        parent_stat,
        role="organization root parent",
    )
    mutation_guard.require_paths_allowed(root)
    return parent, parent_stat


def _create_validated_apply_root(
    state_directory: Path,
    root: Path,
    parent: Path,
    parent_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> os.stat_result:
    try:
        root.mkdir()
    except FileExistsError:
        if not root.is_dir() or root.is_symlink() or _is_junction(root):
            raise ValueError("organization root appeared as an unsafe filesystem object") from None
    _require_directory_identity(
        parent,
        parent_stat,
        role="organization root parent",
    )
    _require_disjoint_path_trees(
        state_directory,
        root,
        detail="organization root and framework state directory",
    )
    root_stat = os.stat(root, follow_symlinks=False)
    _require_directory_identity(root, root_stat, role="organization root")
    _require_organization_tree_allowed(root, mutation_guard)
    return root_stat


def _apply_one_plan(
    row: sqlite3.Row,
    state_directory: Path,
    root: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> tuple[str, str]:
    source = Path(str(row["source_path"]))
    destination_value = row["destination_path"]
    if destination_value is None:
        return "blocked", "plan has no destination"
    destination = Path(str(destination_value))
    mutation_guard.require_paths_allowed(source, destination)
    boundary_error = _organization_boundary_error(
        state_directory,
        root,
        source,
        destination,
        root_stat,
        mutation_guard,
    )
    if boundary_error is not None:
        return "blocked", boundary_error
    expected, identity_error = _planned_source_snapshot(row, source)
    if identity_error is not None:
        return identity_error
    assert expected is not None
    recovered = _recover_organization_destination(source, destination, expected)
    if recovered is not None:
        return recovered
    current, source_error = _validated_organization_source(
        source,
        expected,
        int(root_stat.st_dev),
    )
    if source_error is not None:
        return source_error
    assert current is not None
    return _move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        mutation_guard,
    )


def _organization_boundary_error(
    state_directory: Path,
    root: Path,
    source: Path,
    destination: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> str | None:
    try:
        _require_organization_boundaries(
            state_directory,
            root,
            source,
            destination,
            root_stat,
            mutation_guard,
        )
    except ValueError as exc:
        return str(exc)
    return None


def _planned_source_snapshot(
    row: sqlite3.Row,
    source: Path,
) -> tuple[FileSnapshot | None, tuple[str, str] | None]:
    try:
        volume_id = int(row["volume_id"])
        file_id = int(row["file_id"])
    except (TypeError, ValueError):
        return None, (
            "stale",
            "stored source identity is not a native filesystem identity",
        )
    return (
        FileSnapshot(
            path=str(source),
            volume_id=volume_id,
            file_id=file_id,
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            birthtime_ns=int(row["birthtime_ns"]),
        ),
        None,
    )


def _recover_organization_destination(
    source: Path,
    destination: Path,
    expected: FileSnapshot,
) -> tuple[str, str] | None:
    source_present = os.path.lexists(source)
    destination_present = os.path.lexists(destination)
    if not destination_present:
        return None
    if source_present:
        return "blocked", "both source and destination exist during recovery"
    if destination.is_symlink() or _is_junction(destination) or not destination.is_file():
        return "blocked", "recovery destination is not a regular file"
    try:
        recovered = snapshot_path(destination)
    except OSError as exc:
        return "failed", f"destination snapshot failed: {type(exc).__name__}: {exc}"
    if not same_snapshot(expected, recovered):
        return "blocked", "recovery destination does not match the planned snapshot"
    return "moved", "recovered a completed move from its exact destination snapshot"


def _validated_organization_source(
    source: Path,
    expected: FileSnapshot,
    root_volume_id: int,
) -> tuple[FileSnapshot | None, tuple[str, str] | None]:
    reason = protected_path_reason(source)
    if reason is not None:
        return None, ("blocked", reason)
    if source.is_symlink() or not source.is_file():
        return None, ("stale", "source is missing or is no longer a regular file")
    try:
        current = snapshot_path(source)
    except OSError as exc:
        return None, (
            "stale",
            f"source snapshot failed: {type(exc).__name__}: {exc}",
        )
    if not same_snapshot(expected, current):
        return None, ("stale", "source identity or metadata changed after planning")
    if current.volume_id != root_volume_id:
        return None, ("blocked", "cross-volume organization moves are not supported")
    return current, None


def _move_organization_source(
    source: Path,
    destination: Path,
    expected: FileSnapshot,
    state_directory: Path,
    root: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> tuple[str, str]:
    def revalidate_before_native_call() -> None:
        _require_organization_boundaries(
            state_directory,
            root,
            source,
            destination,
            root_stat,
            mutation_guard,
        )

    try:
        _create_destination_parent(
            state_directory,
            source,
            root,
            destination,
            root_stat,
            mutation_guard,
        )
        _require_organization_boundaries(
            state_directory,
            root,
            source,
            destination,
            root_stat,
            mutation_guard,
        )
        if os.path.lexists(destination):
            return "blocked", "destination appeared after directory creation"
        receipt = rename_no_replace_by_identity(
            source,
            destination,
            expected,
            before_native_call=revalidate_before_native_call,
        )
    except (InternalPathProtectionError, ProtectedAnalysisRootError):
        raise
    except FileExistsError:
        return "blocked", "destination appeared while applying the move"
    except UnsupportedIdentityBoundMutation as exc:
        return "blocked", f"identity-bound move unavailable: {exc}"
    except MutationEffectUncertainError as exc:
        return "recovery_required", str(exc)
    except IdentityBoundMutationError as exc:
        return "blocked", str(exc)
    except ValueError as exc:
        return "blocked", str(exc)
    except OSError as exc:
        return "failed", f"{type(exc).__name__}: {exc}"
    try:
        moved = snapshot_path(destination)
    except OSError as exc:
        return (
            "recovery_required",
            f"moved destination snapshot failed: {type(exc).__name__}: {exc}",
        )
    if not same_snapshot(expected, moved):
        return (
            "recovery_required",
            "moved destination does not match the planned snapshot",
        )
    return (
        "moved",
        "identity-bound move confirmed without replacement "
        f"(volume_id={receipt.volume_id}, file_id={receipt.file_id})",
    )


def _disambiguate_apply_destination(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    mutation_guard: CorpusMutationGuard,
) -> sqlite3.Row:
    """Resolve a destination that appeared after planning without replacing it."""

    source = Path(str(row["source_path"]))
    destination_value = row["destination_path"]
    if destination_value is None or not os.path.lexists(source):
        return row
    destination = Path(str(destination_value))
    if not os.path.lexists(destination) and not _catalog_destination_conflict(connection, row):
        return row
    resolved, disambiguated = _resolve_plan_destination(
        connection,
        row,
        destination,
    )
    if resolved is None or not disambiguated:
        return row
    mutation_guard.require_paths_allowed(source, resolved)
    reason = str(row["reason"])
    if "identity_disambiguation" not in reason:
        reason = f"{reason}_with_identity_disambiguation"
    connection.execute(
        """UPDATE organization_plans SET destination_path=?,reason=?,detail=?
        WHERE plan_id=? AND status IN ('planned','applying')""",
        (
            str(resolved),
            reason,
            "destination collision disambiguated during apply without replacement",
            row["plan_id"],
        ),
    )
    connection.commit()
    refreshed = connection.execute(
        "SELECT * FROM organization_plans WHERE plan_id=?",
        (row["plan_id"],),
    ).fetchone()
    if refreshed is None:
        raise RuntimeError("organization plan disappeared during destination recovery")
    return refreshed


def _validate_destination_ancestors(root: Path, destination: Path) -> None:
    try:
        validate_mutation_path(
            root,
            destination,
            role="organization destination",
            allow_missing_tail=True,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _require_disjoint_path_trees(
    left: Path,
    right: Path,
    *,
    detail: str,
) -> None:
    try:
        intersects = path_trees_intersect(left, right)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{detail} boundary cannot be verified") from exc
    if intersects:
        raise ValueError(f"{detail} must be disjoint")


def _preflight_selected_organization_boundaries(
    state_directory: Path,
    root: Path,
    rows: list[sqlite3.Row],
    mutation_guard: CorpusMutationGuard,
) -> None:
    _require_organization_tree_allowed(root, mutation_guard)
    _require_disjoint_path_trees(
        state_directory,
        root,
        detail="organization root and framework state directory",
    )
    for row in rows:
        source = Path(str(row["source_path"]))
        destination_value = row["destination_path"]
        if destination_value is None:
            raise ValueError("selected organization plan has no destination")
        destination = Path(str(destination_value))
        mutation_guard.require_paths_allowed(source, destination)
        for candidate, role in (
            (source, "organization source"),
            (destination, "organization destination"),
        ):
            _require_disjoint_path_trees(
                state_directory,
                candidate,
                detail=f"{role} and framework state directory",
            )
        _require_disjoint_path_trees(
            source,
            destination,
            detail="organization source and destination",
        )
        _validate_destination(root, destination)


def _require_directory_identity(
    path: Path,
    expected: os.stat_result,
    *,
    role: str,
) -> None:
    protected_reason = protected_path_reason(path)
    if protected_reason is not None:
        raise ValueError(f"{role} is protected: {protected_reason}")
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{role} is unavailable: {exc}") from exc
    expected_birthtime = stat_birthtime_ns(expected)
    current_birthtime = stat_birthtime_ns(current)
    identity_changed = (
        int(current.st_dev) != int(expected.st_dev)
        or int(current.st_ino) != int(expected.st_ino)
        or current_birthtime != expected_birthtime
    )
    if identity_changed:
        raise ValueError(f"{role} identity changed during apply")
    if (
        not stat_module.S_ISDIR(current.st_mode)
        or stat_module.S_ISLNK(current.st_mode)
        or _is_junction(path)
    ):
        raise ValueError(f"{role} is no longer a real directory")


def _require_organization_boundaries(
    state_directory: Path,
    root: Path,
    source: Path,
    destination: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> None:
    _require_organization_tree_allowed(root, mutation_guard)
    mutation_guard.require_paths_allowed(source, destination)
    for candidate, role in (
        (root, "organization root"),
        (source, "organization source"),
        (destination, "organization destination"),
    ):
        _require_disjoint_path_trees(
            state_directory,
            candidate,
            detail=f"{role} and framework state directory",
        )
    _require_disjoint_path_trees(
        source,
        destination,
        detail="organization source and destination",
    )
    _require_directory_identity(root, root_stat, role="organization root")
    _validate_destination(root, destination)
    _validate_destination_ancestors(root, destination)
    _require_directory_identity(root, root_stat, role="organization root")
    _require_organization_tree_allowed(root, mutation_guard)
    mutation_guard.require_paths_allowed(source, destination)


def _create_destination_parent(
    state_directory: Path,
    source: Path,
    root: Path,
    destination: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
) -> None:
    """Create missing parents one level at a time with policy revalidation."""

    _require_organization_boundaries(
        state_directory,
        root,
        source,
        destination,
        root_stat,
        mutation_guard,
    )
    if _same_path(root, destination.parent):
        return
    try:
        _, relative = validate_descendant_path(
            root,
            destination.parent,
            role="organization destination parent",
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            entry = validate_mutation_path(
                root,
                current,
                role="organization destination directory",
                allow_missing_leaf=True,
            )
            if entry is None:
                _require_organization_tree_allowed(current.parent, mutation_guard)
                mutation_guard.require_paths_allowed(source, destination, current)
                _require_organization_boundaries(
                    state_directory,
                    root,
                    source,
                    destination,
                    root_stat,
                    mutation_guard,
                )
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                _require_organization_boundaries(
                    state_directory,
                    root,
                    source,
                    destination,
                    root_stat,
                    mutation_guard,
                )
                entry = validate_mutation_path(
                    root,
                    current,
                    role="organization destination directory",
                )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if entry is None or not stat_module.S_ISDIR(entry.st_mode):
            raise ValueError(f"organization destination component is not a directory: {current}")


def _require_organization_tree_allowed(
    path: Path,
    mutation_guard: CorpusMutationGuard,
) -> None:
    """Reject an internal root while allowing a safe ancestor container."""

    if not os.path.lexists(path):
        mutation_guard.require_paths_allowed(path)
        return
    access = CorpusAccessPolicy.capture("normal", path)
    mutation_guard.internal_paths_policy.validate_corpus_access(access)


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


# endregion [02]
