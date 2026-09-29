"""Explicit, revalidated and resumable document-organization application."""
# region [00] Contexto del módulo
# Módulo: neocortex/document_organization_application.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import os
import sqlite3
import stat as stat_module
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from neocortex.platform.policy import sqlite_path_collation, stat_birthtime_ns
from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding
from neocortex.runtime.control.locking import FrameworkRunLock

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressMetric,
    emit_progress,
)

from neocortex.workflow.actions.action_policy import (
    protected_path_reason,
    same_snapshot,
    validate_descendant_path,
    validate_mutation_path,
)
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
    path_trees_intersect,
)
from neocortex.safety.kio_trash import metadata_binding
from neocortex.workflow.mutations import ApplyCandidate, PosixRenameBackend
from .document_cache_sync import (
    DocumentMoveTransition,
    synchronize_moved_document,
    synchronize_moved_documents,
)
from .document_catalog import document_catalog_database, initialize_document_catalog
from .document_organization_scope import OrganizationInputScope, assess_organization_resource
from .document_resource_binding import (
    ResourceBindingError,
    parse_resource_binding,
    rebind_resource_binding_path,
)
from .semantic_curation_gate import (
    FastCurationPolicySource,
    validate_current_fast_curation_decision,
)
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
    is_advisory_organization_block,
)
from .document_organization_planning import (
    _reject_state_destination,
    _resolve_plan_destination,
    _same_path,
    _validate_destination,
)
from neocortex.safety.protected_content import ProtectedContentError
# endregion [01]

# region [02] Implementación


_PATH_COLLATION = sqlite_path_collation()
_ORGANIZATION_MOVE_RECEIPT_SCHEMA = "neocortex.organization-move-receipt/v1"
_ORGANIZATION_MOVE_BACKEND = "posix-link-unlink-no-replace-v1"


@dataclass(frozen=True, slots=True)
class _OrganizationFilesystemOutcome:
    """One physical boundary result, including durable evidence when moved."""

    status: str
    detail: str
    receipt_json: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"moved", "blocked", "stale", "failed", "recovery_required"}:
            raise ValueError(f"unsupported organization filesystem status: {self.status}")
        if self.status == "moved" and not self.receipt_json:
            raise ValueError("a moved organization outcome requires a receipt")


@dataclass(frozen=True, slots=True)
class _OrganizationMoveEffect:
    """Duck-typed effect consumed by :class:`PosixRenameBackend`."""

    action: str
    source: FileSnapshot
    source_digest: str
    target_path: Path


@dataclass(slots=True)
class _OrganizationApplyCounters:
    applied: int = 0
    stale: int = 0
    blocked: int = 0
    failed: int = 0
    cache_synced: int = 0
    cache_pending: int = 0
    advisory_blocked: int = 0

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
            if outcome.advisory_blocked:
                self.advisory_blocked += 1
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
            advisory_blocked=self.advisory_blocked,
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
            advisory_blocked=self.advisory_blocked,
            remaining=remaining,
        )

    def resolve_cache_sync(self, count: int, *, complete: bool) -> None:
        """Resolve physical moves after one coalesced owner publication."""

        if count < 0 or count > self.cache_pending:
            raise ValueError("cache-sync resolution count is outside pending moves")
        if not complete:
            return
        self.cache_pending -= count
        self.applied += count
        self.cache_synced += count


def apply_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    max_actions: int = 100,
    on_progress: OrganizationApplyProgressCallback | None = None,
    framework_lock_held: bool = False,
    checkpoint: Callable[[], None] | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
    curation_policy_bundle: FastCurationPolicySource | None = None,
) -> OrganizationApplySummary:
    """Apply plans and mark them complete only after every cache is synchronized."""

    if type(framework_lock_held) is not bool:
        raise TypeError("framework_lock_held must be boolean")
    if fast_curation_policy_bundle is not None and curation_policy_bundle is not None:
        raise ValueError("Fast Curation policy bundle was supplied twice")
    resolved_curation_bundle = (
        fast_curation_policy_bundle
        if fast_curation_policy_bundle is not None
        else curation_policy_bundle
    )
    root = _validated_organization_apply_request(
        catalog_path,
        organization_root,
        mutation_guard=mutation_guard,
        max_actions=max_actions,
    )
    lock = (
        nullcontext()
        if framework_lock_held
        else FrameworkRunLock(catalog_path.parent / "framework.lock")
    )
    with lock:
        initialize_document_catalog(catalog_path)
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
                    True,
                    checkpoint,
                    resolved_curation_bundle,
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
    framework_lock_held: bool,
    checkpoint: Callable[[], None] | None,
    fast_curation_policy_bundle: FastCurationPolicySource | None,
) -> OrganizationApplySummary:
    protected_denials, root_stat = _prepare_selected_organization_plans(
        catalog_path,
        root,
        rows,
        mutation_guard,
        connection=connection,
        fast_curation_policy_bundle=fast_curation_policy_bundle,
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
        framework_lock_held,
        checkpoint,
        fast_curation_policy_bundle,
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
    *,
    connection: sqlite3.Connection,
    fast_curation_policy_bundle: FastCurationPolicySource | None,
) -> tuple[dict[str, str], os.stat_result | None]:
    protected_denials = _organization_execution_denials(
        connection,
        rows,
        fast_curation_policy_bundle=fast_curation_policy_bundle,
    )
    remaining_rows = [row for row in rows if str(row["plan_id"]) not in protected_denials]
    protected_denials.update(_protected_organization_plan_denials(remaining_rows, mutation_guard))
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


def _organization_execution_denials(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> dict[str, str]:
    """Reject advisory/legacy proposals before any destination preparation."""

    denials: dict[str, str] = {}
    for row in rows:
        plan_id = str(row["plan_id"])
        try:
            if "source_scope_json" not in row.keys() or row["source_scope_json"] is None:
                denials[plan_id] = "legacy_unscoped_organization_plan"
                continue
            scope = OrganizationInputScope.from_json(row["source_scope_json"])
            if scope.scope_id != row["source_scope_id"]:
                raise ValueError("organization_scope_digest_mismatch")
            scope.verify(connection)
            # Once the filesystem move is durable, the catalog document path is
            # intentionally rebound to the destination while the owner caches
            # may still be pending.  The original resource binding still names
            # the pre-effect locator, so re-running the pre-effect assessment
            # would incorrectly turn a resumable cache transition into a
            # scope denial.  The physical receipt and scope/root fences are
            # checked again by the replay path below.
            assessment = (
                None
                if str(row["status"]) in {"applying", "moved_cache_pending"}
                else assess_organization_resource(row, scope)
            )
            if assessment is not None and not assessment.included:
                denials[plan_id] = assessment.reason or "organization_scope_unverified"
            elif (
                row["representation_kind"] != "physical_file"
                or row["operation_kind"] != "move_physical"
            ):
                denials[plan_id] = "organization_operation_not_physical"
            elif not row["executable"] or json.loads(row["blockers_json"]):
                denials[plan_id] = "organization_plan_advisory_only"
            elif row["destination_path"] is None:
                denials[plan_id] = "organization_plan_has_no_destination"
            elif not Path(str(row["organization_root"])).is_relative_to(scope.root):
                # The no-replace backend must open one selected source-scope
                # descriptor for both source and destination.  Do not let a
                # writable database flag widen that root.
                denials[plan_id] = "organization_authorized_backend_unavailable"
            elif str(row["status"]) in {"applying", "moved_cache_pending"}:
                # These rows already crossed or may have crossed the
                # filesystem frontier.  Recovery validates the durable move
                # receipt and rebinds the current curation decision below;
                # applying the pre-effect gate here would compare an old plan
                # path with the already-rebound current Catalog path.
                continue
            else:
                try:
                    current_gate = validate_current_fast_curation_decision(
                        connection,
                        source_kind=str(row["source_kind"]),
                        file_key=str(row["file_key"]),
                        policy_bundle=fast_curation_policy_bundle,
                        expected_binding=parse_resource_binding(row["resource_binding_json"]),
                        expected_path=str(row["source_path"]),
                    )
                except (OSError, TypeError, ValueError, KeyError) as exc:
                    denials[plan_id] = f"fast_curation_effect_gate_error:{type(exc).__name__}"
                else:
                    if not current_gate.eligible:
                        denials[plan_id] = current_gate.reason
                    else:
                        # The persisted bit is only a capability hint.  The
                        # filesystem frontier repeats this gate immediately
                        # before the no-replace syscall.
                        continue
        except (OSError, ValueError, TypeError, KeyError) as exc:
            denials[plan_id] = f"organization_contract_invalid:{type(exc).__name__}"
    return denials


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
    framework_lock_held: bool,
    checkpoint: Callable[[], None] | None,
    fast_curation_policy_bundle: FastCurationPolicySource | None,
) -> None:
    pending_rows: list[sqlite3.Row] = []
    for selected_index, row in enumerate(rows, start=1):
        outcome = _apply_organization_row(
            connection,
            catalog_path,
            root,
            root_stat,
            row,
            protected_denials,
            mutation_guard,
            synchronize_cache=False,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
        counters.record(outcome)
        connection.commit()
        if outcome.cache_pending:
            refreshed = connection.execute(
                "SELECT * FROM organization_plans WHERE plan_id=?",
                (row["plan_id"],),
            ).fetchone()
            if refreshed is not None:
                pending_rows.append(refreshed)
        _report_organization_apply_progress(
            on_progress,
            counters,
            selected_index=selected_index,
            selected_total=len(rows),
        )
    if not pending_rows:
        return
    transitions = tuple(
        DocumentMoveTransition(
            source_kind=str(row["source_kind"]),
            file_key=str(row["file_key"]),
            old_path=str(row["source_path"]),
            new_path=str(row["destination_path"]),
            volume_id=str(row["volume_id"]),
            file_id=str(row["file_id"]),
        )
        for row in pending_rows
    )
    sync = synchronize_moved_documents(
        catalog_path.parent,
        transitions,
        framework_lock_held=framework_lock_held,
        work_check=checkpoint,
    )
    sync_json = sync.as_json()
    counters.resolve_cache_sync(len(pending_rows), complete=sync.complete)
    for row in pending_rows:
        receipt = _stored_move_receipt(row["cache_sync_json"])
        envelope = _cache_sync_envelope(sync_json, receipt)
        if sync.complete:
            connection.execute(
                """UPDATE organization_plans SET status='applied',
                detail=?,completed_ns=?,cache_sync_status='synced',
                cache_sync_json=?,cache_sync_error=NULL WHERE plan_id=?""",
                (
                    "filesystem move and batch cache synchronization completed",
                    time.time_ns(),
                    envelope,
                    row["plan_id"],
                ),
            )
        else:
            connection.execute(
                """UPDATE organization_plans SET status='moved_cache_pending',
                detail=?,completed_ns=NULL,cache_sync_status='pending',
                cache_sync_json=?,cache_sync_error=? WHERE plan_id=?""",
                (
                    "filesystem move completed; batch cache synchronization pending",
                    envelope,
                    sync.error_message,
                    row["plan_id"],
                ),
            )
    connection.commit()
    _report_organization_apply_progress(
        on_progress,
        counters,
        selected_index=len(rows),
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
    *,
    synchronize_cache: bool = True,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
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
        synchronize_cache=synchronize_cache,
        fast_curation_policy_bundle=fast_curation_policy_bundle,
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
    if str(row["status"]) in {"applying", "moved_cache_pending"}:
        connection.execute(
            """UPDATE organization_plans SET status='recovery_required',detail=?,completed_ns=NULL
            WHERE plan_id=?""",
            (detail, row["plan_id"]),
        )
        return _ApplyRowOutcome("recovery_required")
    connection.execute(
        """UPDATE organization_plans
        SET status='blocked',detail=?,completed_ns=?,
        cache_sync_status='not_required',cache_sync_error=NULL
        WHERE plan_id=?""",
        (detail, time.time_ns(), row["plan_id"]),
    )
    return _ApplyRowOutcome(
        "blocked",
        advisory_blocked=is_advisory_organization_block(detail),
    )


def _apply_selected_organization_plan(
    connection: sqlite3.Connection,
    catalog_path: Path,
    row: sqlite3.Row,
    root: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
    *,
    synchronize_cache: bool = True,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> _ApplyRowOutcome:
    status = str(row["status"])
    detail = str(row["detail"] or "")
    filesystem: _OrganizationFilesystemOutcome | None = None
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
            filesystem = _apply_one_plan(
                row,
                catalog_path.parent,
                root,
                root_stat,
                mutation_guard,
                connection=connection,
                fast_curation_policy_bundle=fast_curation_policy_bundle,
            )
            status = filesystem.status
            detail = filesystem.detail
        if status == "moved":
            assert filesystem is not None and filesystem.receipt_json is not None
            recorded_status = _record_moved_path(
                connection,
                row,
                detail,
                receipt_json=filesystem.receipt_json,
            )
            connection.commit()
            if recorded_status == "recovery_required":
                return _ApplyRowOutcome("recovery_required")
            status = recorded_status
    if status == "moved_cache_pending":
        pending_receipt = _validate_pending_organization_move(connection, row)
        if pending_receipt is None:
            return _ApplyRowOutcome("recovery_required")
        if not synchronize_cache:
            return _ApplyRowOutcome("moved_cache_pending", cache_pending=True)
        return _synchronize_applied_organization_plan(
            connection,
            catalog_path,
            row,
            physical_receipt=pending_receipt,
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


def _validate_pending_organization_move(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> str | None:
    """Revalidate the already-crossed physical boundary before cache writes."""

    source = Path(str(row["source_path"]))
    destination_value = row["destination_path"]
    if destination_value is None:
        detail = "moved cache pending row has no destination"
        connection.execute(
            "UPDATE organization_plans SET status='recovery_required',detail=?,completed_ns=NULL WHERE plan_id=?",
            (detail, row["plan_id"]),
        )
        return None
    destination = Path(str(destination_value))
    expected, identity_error = _planned_source_snapshot(row, source)
    if identity_error is not None or expected is None:
        detail = identity_error[1] if identity_error is not None else "planned source snapshot missing"
        connection.execute(
            "UPDATE organization_plans SET status='recovery_required',detail=?,completed_ns=NULL WHERE plan_id=?",
            (detail, row["plan_id"]),
        )
        return None
    recovered = _recover_organization_destination(
        source,
        destination,
        expected,
        receipt_json=_stored_move_receipt(row["cache_sync_json"]),
        effect_pending=str(row["status"]) in {"applying", "moved_cache_pending"},
    )
    if recovered is None:
        detail = "moved cache pending row no longer proves destination identity"
        connection.execute(
            "UPDATE organization_plans SET status='recovery_required',detail=?,completed_ns=NULL WHERE plan_id=?",
            (detail, row["plan_id"]),
        )
        return None
    if recovered.status != "moved" or recovered.receipt_json is None:
        connection.execute(
            "UPDATE organization_plans SET status='recovery_required',detail=?,completed_ns=NULL WHERE plan_id=?",
            (recovered.detail, row["plan_id"]),
        )
        return None
    try:
        _rebind_current_catalog_document(
            connection,
            row,
            receipt_json=recovered.receipt_json,
        )
    except (ResourceBindingError, RuntimeError, ValueError) as exc:
        detail = f"catalog resource binding recovery required: {type(exc).__name__}: {exc}"
        connection.execute(
            """UPDATE organization_plans
            SET status='recovery_required',detail=?,completed_ns=NULL,
            cache_sync_status='pending',cache_sync_error=?
            WHERE plan_id=?""",
            (detail, detail, row["plan_id"]),
        )
        return None
    if _stored_move_receipt(row["cache_sync_json"]) is None:
        # Preserve a receipt reconstructed from the exact destination snapshot
        # before any owner cache rebinding is attempted.
        raw = row["cache_sync_json"]
        try:
            payload = json.loads(raw) if isinstance(raw, str) and raw else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["physical_receipt"] = json.loads(recovered.receipt_json)
        connection.execute(
            "UPDATE organization_plans SET cache_sync_json=? WHERE plan_id=?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                row["plan_id"],
            ),
        )
        connection.commit()
    return recovered.receipt_json


def _synchronize_applied_organization_plan(
    connection: sqlite3.Connection,
    catalog_path: Path,
    row: sqlite3.Row,
    *,
    physical_receipt: str | None = None,
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
    if physical_receipt is None:
        physical_receipt = _stored_move_receipt(row["cache_sync_json"])
    sync_json = _cache_sync_envelope(sync.as_json(), physical_receipt)
    if sync.complete:
        connection.execute(
            """UPDATE organization_plans
            SET status='applied',detail=?,completed_ns=?,
            cache_sync_status='synced',cache_sync_json=?,
            cache_sync_error=NULL WHERE plan_id=?""",
            (
                "filesystem move and cache synchronization completed",
                time.time_ns(),
                sync_json,
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
            sync_json,
            sync.error_message,
            row["plan_id"],
        ),
    )
    return _ApplyRowOutcome("moved_cache_pending", cache_pending=True)


def _cache_sync_envelope(sync_json: str, physical_receipt: str | None) -> str:
    """Keep the physical receipt beside cache evidence without a schema change."""

    try:
        payload = json.loads(sync_json)
    except (TypeError, ValueError) as exc:  # pragma: no cover - sync owns its schema
        raise RuntimeError("cache synchronization returned invalid JSON") from exc
    if not isinstance(payload, dict):  # pragma: no cover - defensive contract guard
        raise RuntimeError("cache synchronization returned a non-object payload")
    if physical_receipt is not None:
        try:
            receipt = json.loads(physical_receipt)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("filesystem move returned an invalid receipt") from exc
        if not isinstance(receipt, dict):
            raise RuntimeError("filesystem move returned a non-object receipt")
        payload["physical_receipt"] = receipt
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored_move_receipt(raw: object) -> str | None:
    """Read the receipt from either the current envelope or legacy sync JSON."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    receipt = payload.get("physical_receipt")
    if not isinstance(receipt, dict):
        return None
    return json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def apply_all_document_organization(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    batch_size: int = ORGANIZATION_APPLY_BATCH_SIZE,
    progress: ProgressCallback | None = None,
    progress_operation: str = "framework",
    framework_lock_held: bool = False,
    checkpoint: Callable[[], None] | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
    curation_policy_bundle: FastCurationPolicySource | None = None,
) -> OrganizationApplySummary:
    """Consume every actionable plan in bounded, resumable apply batches."""

    mutation_guard.reject_run_mutation()
    if type(framework_lock_held) is not bool:
        raise TypeError("framework_lock_held must be boolean")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if fast_curation_policy_bundle is not None and curation_policy_bundle is not None:
        raise ValueError("Fast Curation policy bundle was supplied twice")
    resolved_curation_bundle = (
        fast_curation_policy_bundle
        if fast_curation_policy_bundle is not None
        else curation_policy_bundle
    )
    lock = (
        nullcontext()
        if framework_lock_held
        else FrameworkRunLock(catalog_path.parent / "framework.lock")
    )
    with lock:
        return _apply_all_document_organization_locked(
            catalog_path,
            organization_root,
            mutation_guard=mutation_guard,
            batch_size=batch_size,
            progress=progress,
            progress_operation=progress_operation,
            checkpoint=checkpoint,
            fast_curation_policy_bundle=resolved_curation_bundle,
        )


def _apply_all_document_organization_locked(
    catalog_path: Path,
    organization_root: Path,
    *,
    mutation_guard: CorpusMutationGuard,
    batch_size: int,
    progress: ProgressCallback | None,
    progress_operation: str,
    checkpoint: Callable[[], None] | None,
    fast_curation_policy_bundle: FastCurationPolicySource | None,
) -> OrganizationApplySummary:
    """Run all organization batches while the caller owns framework.lock."""

    selected = applied = stale = blocked = failed = cache_synced = advisory_blocked = 0
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
        advisory_blocked=0,
        remaining=total,
    )
    while True:
        selected_before = selected
        applied_before = applied
        stale_before = stale
        blocked_before = blocked
        failed_before = failed
        cache_synced_before = cache_synced
        advisory_blocked_before = advisory_blocked
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
            advisory_blocked_before=advisory_blocked_before,
        )

        current = apply_document_organization(
            catalog_path,
            organization_root,
            mutation_guard=mutation_guard,
            max_actions=batch_size,
            on_progress=report_batch,
            framework_lock_held=True,
            checkpoint=checkpoint,
            fast_curation_policy_bundle=fast_curation_policy_bundle,
        )
        batches += 1
        last_run_id = current.catalog_run_id
        selected += current.selected
        applied += current.applied
        stale += current.stale
        blocked += current.blocked
        failed += current.failed
        cache_synced += current.cache_synced
        advisory_blocked += current.advisory_blocked
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
        advisory_blocked=advisory_blocked,
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
        advisory_blocked=advisory_blocked,
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
    advisory_blocked_before: int,
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
            advisory_blocked=advisory_blocked_before + current.advisory_blocked,
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
    advisory_blocked: int,
    remaining: int,
    finished: bool = False,
) -> None:
    unresolved = stale + max(0, blocked - advisory_blocked) + failed + remaining
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
                ProgressMetric("advisory_blocked", advisory_blocked),
                ProgressMetric("errors", failed),
                ProgressMetric("cache_pending", remaining),
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
        f"""SELECT 1 FROM documents
        WHERE active=1 AND path=? COLLATE {_PATH_COLLATION}
        AND NOT (source_kind=? AND file_key=?) LIMIT 1""",
        (destination, row["source_kind"], row["file_key"]),
    ).fetchone()
    return conflict is not None


def _record_moved_path(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    detail: str,
    *,
    receipt_json: str,
) -> str:
    _validate_organization_move_receipt(
        receipt_json,
        source=Path(str(row["source_path"])),
        destination=Path(str(row["destination_path"])),
        expected=_planned_source_snapshot(row, Path(str(row["source_path"])))[0],
    )
    try:
        _rebind_current_catalog_document(connection, row, receipt_json=receipt_json)
    except (ResourceBindingError, RuntimeError, ValueError) as exc:
        # The filesystem boundary has already crossed.  Keep its receipt and
        # stop at explicit recovery rather than reporting success with a
        # stale current-owner locator or dropping evidence of the move.
        detail = f"catalog resource binding recovery required: {type(exc).__name__}: {exc}"
        connection.execute(
            """UPDATE organization_plans
            SET status='recovery_required',detail=?,move_completed_ns=?,
            completed_ns=NULL,cache_sync_status='pending',cache_sync_json=?,
            cache_sync_error=? WHERE plan_id=?""",
            (
                detail,
                time.time_ns(),
                _cache_sync_envelope(
                    json.dumps(
                        {
                            "schema": _ORGANIZATION_MOVE_RECEIPT_SCHEMA,
                            "databases": [],
                            "complete": False,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    receipt_json,
                ),
                detail,
                row["plan_id"],
            ),
        )
        return "recovery_required"
    connection.execute(
        """UPDATE organization_plans
        SET status='moved_cache_pending',detail=?,move_completed_ns=?,
        completed_ns=NULL,cache_sync_status='pending',cache_sync_json=?,cache_sync_error=NULL
        WHERE plan_id=?""",
        (
            detail,
            time.time_ns(),
            _cache_sync_envelope(
                json.dumps(
                    {
                        "schema": _ORGANIZATION_MOVE_RECEIPT_SCHEMA,
                        "databases": [],
                        "complete": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                receipt_json,
            ),
            row["plan_id"],
        ),
    )
    return "moved_cache_pending"


def _rebind_current_catalog_document(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    receipt_json: str,
) -> None:
    """Atomically rebind the mutable catalog owner after a physical move.

    Published ``catalog_generation_documents`` rows are immutable historical
    evidence and are intentionally not touched here.  Only the current
    ``documents`` owner follows the exact physical receipt destination.
    """

    source = str(row["source_path"])
    destination = str(row["destination_path"])
    document = connection.execute(
        """SELECT path,resource_binding_json FROM documents
        WHERE source_kind=? AND file_key=?""",
        (row["source_kind"], row["file_key"]),
    ).fetchone()
    if document is None:
        raise RuntimeError("organization plan no longer has a catalog document")
    current = Path(str(document["path"]))
    if not _same_path(current, Path(destination)) and not _same_path(current, Path(source)):
        raise RuntimeError("catalog path is neither planned source nor destination")
    raw_binding = document["resource_binding_json"]
    if raw_binding is None:
        raise ResourceBindingError(
            "current catalog document has no physical resource binding",
            field="resource_binding_json",
            encoding="missing",
            value=None,
            code="resource_binding_missing",
        )
    rebound = rebind_resource_binding_path(
        raw_binding,
        expected_path=source,
        new_path=destination,
    )
    if rebound["source_kind"] != row["source_kind"] or rebound["file_key"] != row["file_key"]:
        raise ResourceBindingError(
            "current catalog resource binding owner does not match organization plan",
            field="resource_binding_json",
            encoding="owner",
            value=raw_binding,
            code="resource_binding_owner_mismatch",
        )
    try:
        expected_identity = FileIdentity(int(row["volume_id"]), int(row["file_id"]))
        physical_identity = FileIdentity.decode(
            rebound["physical_identity"]["packed_key"],
            encoding=FileIdentityEncoding.PACKED_HEX_V1,
        )
        expected_birthtime = int(row["birthtime_ns"])
        expected_size = int(row["size"])
        expected_mtime = int(row["mtime_ns"])
        revision = rebound["physical_anchor_revision"]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ResourceBindingError(
            "current catalog resource binding identity cannot be verified",
            field="physical_identity",
            encoding="organization-plan",
            value=raw_binding,
            code="resource_binding_identity_unresolved",
        ) from exc
    if (
        physical_identity != expected_identity
        or rebound["physical_identity"]["birthtime_ns"] != expected_birthtime
        or revision["size"] != expected_size
        or revision["mtime_ns"] != expected_mtime
    ):
        raise ResourceBindingError(
            "current catalog resource binding identity differs from organization plan",
            field="physical_identity",
            encoding="organization-plan",
            value=raw_binding,
            code="resource_binding_identity_mismatch",
        )
    binding_json = json.dumps(
        rebound,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """UPDATE documents SET path=?,resource_binding_json=?,updated_ns=?
        WHERE source_kind=? AND file_key=?""",
        (
            destination,
            binding_json,
            time.time_ns(),
            row["source_kind"],
            row["file_key"],
        ),
    )
    # Keep the Catalog-owned Fast Curation decision bound to the same physical
    # owner.  This is part of the same Catalog transaction as the current
    # document rebind; the immutable origin context remains owned by curation.
    curation_table = connection.execute(
        """SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='curator_decisions' LIMIT 1"""
    ).fetchone()
    if curation_table is not None:
        decision_row = connection.execute(
            """SELECT 1 FROM curator_decisions
            WHERE source_kind=? AND file_key=? LIMIT 1""",
            (row["source_kind"], row["file_key"]),
        ).fetchone()
        if decision_row is not None:
            from .curation_state import (
                read_current_curation_decision,
                rebind_curation_decision,
            )

            current_decision = read_current_curation_decision(
                connection,
                source_kind=str(row["source_kind"]),
                file_key=str(row["file_key"]),
            )
            if current_decision is None:
                raise RuntimeError("current Fast Curation decision disappeared during rebind")
            decision_path = current_decision.source_binding.get("physical_anchor_path")
            if decision_path == destination:
                # Recovery may replay this helper after the first transaction
                # already rebound the curation row.  The receipt and current
                # binding remain the idempotent proof; do not demand source
                # path a second time.
                if dict(current_decision.source_binding) != rebound:
                    raise RuntimeError(
                        "current Fast Curation binding differs from Catalog rebind"
                    )
            elif decision_path == source:
                curation_receipt = receipt_json
                try:
                    receipt_payload = json.loads(receipt_json)
                    target_identity = receipt_payload["target_identity"]
                    physical_identity = FileIdentity.decode(
                        rebound["physical_identity"]["packed_key"],
                        encoding=FileIdentityEncoding.PACKED_HEX_V1,
                    )
                    target_identity["volume_id"] = str(physical_identity.volume_id)
                    target_identity["file_id"] = str(physical_identity.file_id)
                    curation_receipt = json.dumps(
                        receipt_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        "organization receipt identity cannot be normalized for curation"
                    ) from exc
                rebind_curation_decision(
                    connection,
                    source_kind=str(row["source_kind"]),
                    file_key=str(row["file_key"]),
                    source_path=source,
                    target_path=destination,
                    receipt=curation_receipt,
                    current_source_binding=rebound,
                    updated_ns=time.time_ns(),
                )
            else:
                raise RuntimeError("current Fast Curation decision is bound to an unexpected path")


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
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags)
    try:
        pinned = os.fstat(parent_fd)
        if (pinned.st_dev, pinned.st_ino) != (parent_stat.st_dev, parent_stat.st_ino):
            raise ValueError("organization root parent changed before creation")
        _require_directory_identity(parent, pinned, role="organization root parent")
        try:
            os.mkdir(root.name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(root.name, flags, dir_fd=parent_fd)
        try:
            root_stat = os.fstat(child_fd)
            if root_stat.st_dev != parent_stat.st_dev:
                raise ValueError("organization root crosses a filesystem boundary")
            os.fsync(parent_fd)
        finally:
            os.close(child_fd)
        _require_directory_identity(parent, parent_stat, role="organization root parent")
        _require_directory_identity(root, root_stat, role="organization root")
        _require_disjoint_path_trees(
            state_directory, root,
            detail="organization root and framework state directory",
        )
        _require_organization_tree_allowed(root, mutation_guard)
        return root_stat
    finally:
        os.close(parent_fd)


def _apply_one_plan(
    row: sqlite3.Row,
    state_directory: Path,
    root: Path,
    root_stat: os.stat_result,
    mutation_guard: CorpusMutationGuard,
    *,
    connection: sqlite3.Connection | None = None,
    fast_curation_policy_bundle: FastCurationPolicySource | None = None,
) -> _OrganizationFilesystemOutcome:
    source = Path(str(row["source_path"]))
    destination_value = row["destination_path"]
    if destination_value is None:
        return _OrganizationFilesystemOutcome("blocked", "plan has no destination")
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
        return _OrganizationFilesystemOutcome("blocked", boundary_error)
    try:
        scope = OrganizationInputScope.from_json(row["source_scope_json"])
        if connection is not None:
            scope.verify(connection)
        else:
            scope.verify()
        backend_root = scope.root
        if not source.is_relative_to(backend_root) or not destination.is_relative_to(backend_root):
            return _OrganizationFilesystemOutcome(
                "blocked",
                "organization source and destination must remain inside the selected source scope",
            )
    except (OSError, TypeError, ValueError) as exc:
        return _OrganizationFilesystemOutcome(
            "blocked",
            f"organization backend root cannot be verified: {type(exc).__name__}: {exc}",
        )
    expected, identity_error = _planned_source_snapshot(row, source)
    if identity_error is not None:
        return _OrganizationFilesystemOutcome(*identity_error)
    assert expected is not None
    recovered = _recover_organization_destination(
        source,
        destination,
        expected,
        receipt_json=_stored_move_receipt(row["cache_sync_json"]),
        effect_pending=str(row["status"]) in {"applying", "moved_cache_pending"},
    )
    if recovered is not None:
        return recovered
    if connection is None:
        return _OrganizationFilesystemOutcome(
            "blocked", "fast_curation_effect_gate_requires_catalog_connection"
        )
    try:
        expected_binding = parse_resource_binding(row["resource_binding_json"])
    except (TypeError, ValueError, KeyError):
        return _OrganizationFilesystemOutcome(
            "blocked", "organization_plan_resource_binding_invalid"
        )
    current_gate = validate_current_fast_curation_decision(
        connection,
        source_kind=str(row["source_kind"]),
        file_key=str(row["file_key"]),
        policy_bundle=fast_curation_policy_bundle,
        expected_binding=expected_binding,
        expected_path=str(source),
    )
    if not current_gate.eligible:
        return _OrganizationFilesystemOutcome("blocked", current_gate.reason)
    current, source_error = _validated_organization_source(
        source,
        expected,
        int(root_stat.st_dev),
    )
    if source_error is not None:
        return _OrganizationFilesystemOutcome(*source_error)
    assert current is not None
    return _move_organization_source(
        source,
        destination,
        expected,
        state_directory,
        root,
        root_stat,
        mutation_guard,
        backend_root=backend_root,
        owner_id=f"organization-plan:{row['plan_id']}",
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
    *,
    receipt_json: str | None = None,
    effect_pending: bool = False,
) -> _OrganizationFilesystemOutcome | None:
    source_present = os.path.lexists(source)
    destination_present = os.path.lexists(destination)
    if not destination_present:
        if effect_pending and not source_present:
            return _OrganizationFilesystemOutcome(
                "recovery_required",
                "neither source nor destination exists during recovery",
            )
        return None
    if source_present:
        return _OrganizationFilesystemOutcome(
            "blocked", "both source and destination exist during recovery"
        )
    try:
        destination_metadata = os.lstat(destination)
    except OSError as exc:
        return _OrganizationFilesystemOutcome(
            "failed", f"recovery destination metadata failed: {type(exc).__name__}: {exc}"
        )
    if (
        stat_module.S_ISLNK(destination_metadata.st_mode)
        or not stat_module.S_ISREG(destination_metadata.st_mode)
        or destination_metadata.st_nlink != 1
        or _is_junction(destination)
    ):
        return _OrganizationFilesystemOutcome(
            "recovery_required" if effect_pending else "blocked",
            "recovery destination is not a unique regular file",
        )
    try:
        recovered = snapshot_path(destination)
    except OSError as exc:
        return _OrganizationFilesystemOutcome(
            "recovery_required" if effect_pending else "failed",
            f"destination snapshot failed: {type(exc).__name__}: {exc}",
        )
    if not same_snapshot(expected, recovered):
        return _OrganizationFilesystemOutcome(
            "recovery_required" if effect_pending else "blocked",
            "recovery destination does not match the planned snapshot",
        )
    if receipt_json is None:
        receipt_json = _organization_move_receipt(
            source,
            destination,
            recovered,
            detail="recovered a completed move from its exact destination snapshot",
        )
    else:
        try:
            _validate_organization_move_receipt(
                receipt_json,
                source=source,
                destination=destination,
                expected=expected,
            )
        except ValueError as exc:
            return _OrganizationFilesystemOutcome(
                "recovery_required" if effect_pending else "blocked",
                f"stored move receipt is invalid: {exc}",
            )
    return _OrganizationFilesystemOutcome(
        "moved", "recovered a completed move from its exact destination snapshot", receipt_json
    )


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
    *,
    backend_root: Path,
    owner_id: str,
) -> _OrganizationFilesystemOutcome:
    """Apply one identity-bound Linux move through the no-replace backend."""

    try:
        # Destination parents are created only after all corpus/state fences have
        # been checked.  The descriptor-relative backend repeats path and source
        # identity validation immediately before renameat2.
        _create_destination_parent(
            state_directory,
            source,
            root,
            destination,
            root_stat,
            mutation_guard,
        )
        mutation_guard.require_paths_allowed(source, destination)
        _require_organization_boundaries(
            state_directory,
            root,
            source,
            destination,
            root_stat,
            mutation_guard,
        )
        scope_root_stat = os.stat(backend_root, follow_symlinks=False)
        if not stat_module.S_ISDIR(scope_root_stat.st_mode) or backend_root.is_symlink():
            return _OrganizationFilesystemOutcome(
                "blocked", "selected source scope is no longer a real directory"
            )

        def before_syscall() -> None:
            """Re-check all mutable fences at the renameat2 frontier."""

            mutation_guard.require_paths_allowed(source, destination)
            _require_organization_boundaries(
                state_directory,
                root,
                source,
                destination,
                root_stat,
                mutation_guard,
            )
            _require_directory_identity(
                backend_root,
                scope_root_stat,
                role="selected source scope root",
            )

        source_digest = metadata_binding(expected)
        effect = _OrganizationMoveEffect(
            action="move",
            source=expected,
            source_digest=source_digest,
            target_path=destination,
        )
        outcome = PosixRenameBackend().apply(
            ApplyCandidate(
                owner_id=owner_id,
                owner_digest=source_digest,
                root=backend_root,
                effect=effect,
            ),
            before_syscall=before_syscall,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _OrganizationFilesystemOutcome(
            "blocked", f"linux organization backend preflight failed: {type(exc).__name__}: {exc}"
        )
    if outcome.status == "applied":
        if outcome.receipt_json is None:  # pragma: no cover - backend contract enforces this
            return _OrganizationFilesystemOutcome(
                "recovery_required", "linux organization backend returned no receipt"
            )
        try:
            receipt = _organization_move_receipt_from_backend(outcome.receipt_json)
            _validate_organization_move_receipt(
                receipt,
                source=source,
                destination=destination,
                expected=expected,
            )
        except ValueError as exc:
            return _OrganizationFilesystemOutcome(
                "recovery_required", f"linux organization move receipt is invalid: {exc}"
            )
        return _OrganizationFilesystemOutcome("moved", "filesystem move verified", receipt)
    if outcome.status == "recovery_required":
        return _OrganizationFilesystemOutcome(
            "recovery_required",
            outcome.detail or f"linux organization move requires recovery: {outcome.reason}",
        )
    return _OrganizationFilesystemOutcome(
        "blocked",
        outcome.detail or f"linux organization move blocked: {outcome.reason}",
    )


def _organization_move_receipt_from_backend(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("backend receipt is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("backend receipt is not an object")
    payload["organization_receipt_schema"] = _ORGANIZATION_MOVE_RECEIPT_SCHEMA
    payload.setdefault("backend", _ORGANIZATION_MOVE_BACKEND)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _organization_move_receipt(
    source: Path,
    destination: Path,
    target: FileSnapshot,
    *,
    detail: str,
) -> str:
    """Reconstruct durable evidence after an interrupted DB write."""

    payload = {
        "backend": _ORGANIZATION_MOVE_BACKEND,
        "detail": detail,
        "operation": "move",
        "organization_receipt_schema": _ORGANIZATION_MOVE_RECEIPT_SCHEMA,
        "receipt_type": "successful_return_and_observation",
        "schema_version": 1,
        "source_digest": metadata_binding(target),
        "source_absent": True,
        "source_path": str(source),
        "target_identity": {
            "birthtime_ns": target.birthtime_ns,
            "file_id": f"{target.file_id:x}",
            "mtime_ns": target.mtime_ns,
            "path": str(destination),
            "size": target.size,
            "volume_id": f"{target.volume_id:x}",
        },
        "target_path": str(destination),
        "target_digest": metadata_binding(target),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_organization_move_receipt(
    receipt_json: str,
    *,
    source: Path,
    destination: Path,
    expected: FileSnapshot | None,
) -> None:
    if expected is None:
        raise ValueError("planned source snapshot is missing")
    try:
        payload = json.loads(receipt_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("receipt is not an object")
    if payload.get("organization_receipt_schema") not in {
        None,
        _ORGANIZATION_MOVE_RECEIPT_SCHEMA,
    }:
        raise ValueError("receipt schema is unsupported")
    if payload.get("operation") not in {"move", "rename"}:
        raise ValueError("receipt operation is not a move")
    if payload.get("source_absent") is not True:
        raise ValueError("receipt does not attest source absence")
    if not _same_path(Path(str(payload.get("source_path", ""))), source):
        raise ValueError("receipt source path differs from the plan")
    if not _same_path(Path(str(payload.get("target_path", ""))), destination):
        raise ValueError("receipt destination path differs from the plan")
    target_identity = payload.get("target_identity")
    if not isinstance(target_identity, dict):
        raise ValueError("receipt target identity is missing")
    try:
        identity = (
            int(str(target_identity["volume_id"]), 16),
            int(str(target_identity["file_id"]), 16),
        )
        metadata = (
            int(target_identity["size"]),
            int(target_identity["mtime_ns"]),
            int(target_identity["birthtime_ns"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("receipt target identity is malformed") from exc
    if identity != expected.identity or metadata != (
        expected.size,
        expected.mtime_ns,
        expected.birthtime_ns,
    ):
        raise ValueError("receipt target identity differs from the planned source")
    if not _same_path(Path(str(target_identity.get("path", ""))), destination):
        raise ValueError("receipt target identity path differs from the plan")
    expected_binding = metadata_binding(expected)
    for field in ("source_digest", "target_digest"):
        if field in payload and payload[field] != expected_binding:
            raise ValueError(f"receipt {field} differs from the planned source")


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
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(root, flags)
    current = root
    try:
        pinned_root = os.fstat(parent_fd)
        if (pinned_root.st_dev, pinned_root.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise ValueError("organization root changed before parent creation")
        for part in relative.parts:
            parent_stat = os.fstat(parent_fd)
            _require_directory_identity(current, parent_stat, role="destination parent")
            child = current / part
            mutation_guard.require_paths_allowed(source, destination, child)
            _require_organization_boundaries(
                state_directory, root, source, destination, root_stat, mutation_guard,
            )
            try:
                os.mkdir(part, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            try:
                child_stat = os.fstat(child_fd)
                if child_stat.st_dev != root_stat.st_dev:
                    raise ValueError("organization destination crosses a filesystem boundary")
                _require_directory_identity(current, parent_stat, role="destination parent")
                _require_directory_identity(child, child_stat, role="destination directory")
                _require_organization_boundaries(
                    state_directory, root, source, destination, root_stat, mutation_guard,
                )
                os.fsync(parent_fd)
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd
            current = child
    finally:
        os.close(parent_fd)


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
