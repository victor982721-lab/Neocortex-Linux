"""Safely apply exact-duplicate and extension-correction actions."""
# region [00] Contexto del módulo
# Módulo: neocortex/actions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from neocortex.platform.policy import stat_birthtime_ns

from neocortex.deduplication import (
    DedupIndex,
    DedupPlan,
    FileChangedError,
    FileSnapshot,
    files_equal_exact,
    full_fingerprint,
    snapshot_path,
    stat_matches_snapshot,
)
from neocortex.deduplication.inventory.index import (
    DEFAULT_EXCLUDED_PATHS,
    InventoryExclusionPolicy,
    validate_inventory_root,
)
from neocortex.progress import ProgressCallback, ProgressEvent, ProgressMetric, emit_progress
from neocortex.workflow.actions.action_policy import (
    corrected_path as _corrected_path,
    path_key as _path_key,
    postorder_directories as _postorder_directories,
    protected_path_reason as _protected_path_reason,
    same_snapshot as _same_snapshot,
    validate_mutation_path as _validate_mutation_path,
)
from neocortex.platform.content_types import DETECTOR_VERSION, DetectedType, detect_content_type
from neocortex.safety.corpus_access import CorpusMutationGuard, ProtectedAnalysisRootError
from neocortex.safety.internal_paths import InternalPathProtectionError
from neocortex.runtime.models import ActionSummary
from neocortex.safety.protected_content import ProtectedContentError
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from neocortex.curation.application import BackendOutcome, KioTrashBackend
# endregion [01]

# region [02] Implementación


TRASH_BATCH_SIZE = 256
TRASH_IDENTITY_ABSTENTION = (
    "Recycle Bin mutation abstained: the available Send2Trash backends resolve "
    "the source by path and cannot bind the observed file identity to the syscall"
)
# Compatibility probe for existing diagnostic/test consumers that monkeypatch
# the removed path backend to assert it is never invoked. Production code never
# reads or calls this sentinel.
send2trash: None = None


class FrameworkActions:
    """Apply bounded action batches with durable before/after records."""

    def __init__(
        self,
        index: DedupIndex,
        state: FrameworkState,
        run_id: int,
        scan_id: int,
        *,
        apply: bool,
        verify_bytes_before_trash: bool = True,
        excluded_paths: Iterable[str | Path] = DEFAULT_EXCLUDED_PATHS,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
        trash_backend: KioTrashBackend | None = None,
    ):
        self._index = index
        self._state = state
        self._run_id = run_id
        self._scan_id = scan_id
        self._apply = apply
        # Destructive mode never relies on a non-cryptographic fingerprint
        # alone, even when candidate reduction used the fast policy.
        self._verify_bytes_before_trash = apply or verify_bytes_before_trash
        self._exclusion_policy = exclusion_policy or InventoryExclusionPolicy.compile(
            excluded_paths
        )
        self._progress = progress
        self._trash_backend = trash_backend
        self._deferred_reconciliation_paths: list[str] = []

    def execute(self, plan: DedupPlan, *, cleanup_empty_directories: bool = True) -> ActionSummary:
        self._validate_apply_root()
        self._deferred_reconciliation_paths.clear()
        summary = ActionSummary(apply_actions=self._apply)
        started = time.perf_counter_ns()
        summary = self._trash_empty_files(plan, summary)
        self._record_phase("empty-files", started, summary)
        started = time.perf_counter_ns()
        summary = self._trash_duplicates(plan, summary)
        self._record_phase("duplicates", started, summary)
        started = time.perf_counter_ns()
        summary = self._validate_extensions(plan, summary)
        self._record_phase("content-types", started, summary)
        if cleanup_empty_directories:
            started = time.perf_counter_ns()
            summary = self._trash_empty_directories(plan, summary)
            self._record_phase("empty-directories", started, summary)
        self._state.store_action_summary(self._run_id, summary)
        return summary

    def recycle_verified_files(
        self,
        action_type: str,
        candidates: Iterable[tuple[FileSnapshot, str]],
    ) -> tuple[int, int, int]:
        """Recycle snapshot-verified files in bounded, durably recorded batches."""

        if not action_type.startswith("trash_"):
            raise ValueError("recycle action types must start with 'trash_'")
        applied = failed = protected = 0
        batch: list[tuple[FileSnapshot, str]] = []

        def flush() -> None:
            nonlocal applied, failed, protected
            if not batch:
                return
            result = self._apply_trash_batch(
                action_type,
                tuple((snapshot.path, evidence) for snapshot, evidence in batch),
                expected_snapshots=tuple(snapshot for snapshot, _evidence in batch),
            )
            applied += result[0]
            failed += result[1]
            protected += result[2]
            batch.clear()

        for candidate in candidates:
            batch.append(candidate)
            if len(batch) >= TRASH_BATCH_SIZE:
                flush()
        flush()
        return applied, failed, protected

    def _record_phase(self, phase: str, started_ns: int, summary: ActionSummary) -> None:
        self._state.record_event(
            self._run_id,
            "info",
            phase,
            "Fase de acciones completada",
            {
                "elapsed_ns": time.perf_counter_ns() - started_ns,
                "files_checked": summary.files_checked,
                "type_cache_hits": summary.type_cache_hits,
                "type_cache_misses": summary.type_cache_misses,
                "stale_inventory": summary.stale_inventory,
                "errors": summary.errors,
            },
        )

    def cleanup_empty_directories(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        """Run final directory cleanup after an optional content route."""

        self._validate_apply_root()
        started = time.perf_counter_ns()
        result = self._trash_empty_directories(plan, summary)
        self._record_phase("empty-directories", started, result)
        self._state.store_action_summary(self._run_id, result)
        return result

    def _trash_empty_directories(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        root = self._index.scan_root(plan.scan_id)
        traversal_error_count = [0]
        pending: list[tuple[str, str, FileSnapshot]] = []
        logical_child_counts: dict[str, int] = {}
        candidates = applied_total = failed_total = protected_total = completed = 0
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-directories",
                "Buscando directorios vacíos",
                0,
                unit="directorios",
            ),
        )

        def flush() -> None:
            nonlocal applied_total, failed_total, protected_total, completed
            if not pending:
                return
            batch = tuple((path, evidence) for path, evidence, _snapshot in pending)
            expected = tuple(snapshot for _path, _evidence, snapshot in pending)
            applied, failed, protected = self._apply_trash_batch(
                "trash_empty_directory", batch, expected_snapshots=expected
            )
            applied_total += applied
            failed_total += failed
            protected_total += protected
            completed += len(batch)
            if self._apply:
                for path, _evidence, _snapshot in pending:
                    if os.path.lexists(path):
                        continue
                    parent_key = _path_key(Path(path).parent)
                    remaining = logical_child_counts.get(parent_key, 0) - 1
                    if remaining > 0:
                        logical_child_counts[parent_key] = remaining
                    else:
                        logical_child_counts.pop(parent_key, None)
            pending.clear()
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "empty-directories",
                    "Enviando directorios vacíos",
                    completed,
                    unit="directorios",
                ),
            )

        for directory in _postorder_directories(
            root, self._exclusion_policy, traversal_error_count
        ):
            directory_snapshot = self._empty_directory_snapshot(
                directory,
                logical_child_counts,
                traversal_error_count,
                flush,
            )
            if directory_snapshot is None:
                continue
            path = str(directory)
            pending.append((path, "directory-empty;policy=trash", directory_snapshot))
            parent_key = _path_key(directory.parent)
            logical_child_counts[parent_key] = logical_child_counts.get(parent_key, 0) + 1
            candidates += 1
            if len(pending) >= TRASH_BATCH_SIZE:
                flush()
        flush()
        summary = replace(
            summary,
            empty_directory_candidates=candidates,
            empty_directories_trashed=applied_total,
            empty_directory_skips=(failed_total + protected_total + traversal_error_count[0]),
            errors=summary.errors + failed_total + traversal_error_count[0],
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-directories",
                "Directorios vacíos procesados",
                candidates,
                candidates,
                "directorios",
                True,
            ),
        )
        return summary

    def _empty_directory_snapshot(
        self,
        directory: Path,
        logical_child_counts: dict[str, int],
        traversal_error_count: list[int],
        flush_pending: Callable[[], None],
    ) -> FileSnapshot | None:
        entry_count = self._directory_entry_count(
            directory,
            traversal_error_count,
        )
        if entry_count is None:
            return None
        scheduled_children = logical_child_counts.pop(_path_key(directory), 0)
        if entry_count != scheduled_children:
            return None
        if self._apply and scheduled_children:
            # A parent is admitted only after its planned children have been
            # applied and a new physical-empty observation succeeds.
            flush_pending()
            if (
                self._directory_entry_count(
                    directory,
                    traversal_error_count,
                    missing_is_error=True,
                )
                != 0
            ):
                return None
        try:
            return snapshot_path(directory)
        except OSError:
            traversal_error_count[0] += 1
            return None

    @staticmethod
    def _directory_entry_count(
        directory: Path,
        traversal_error_count: list[int],
        *,
        missing_is_error: bool = False,
    ) -> int | None:
        count = 0
        try:
            with os.scandir(directory) as entries:
                for _entry in entries:
                    count += 1
        except FileNotFoundError:
            if missing_is_error:
                traversal_error_count[0] += 1
            return None
        except OSError:
            traversal_error_count[0] += 1
            return None
        return count

    def _apply_trash_batch(
        self,
        action_type: str,
        batch: tuple[tuple[str, str], ...],
        *,
        expected_snapshots: tuple[FileSnapshot | None, ...] | None = None,
        reference_snapshots: tuple[FileSnapshot | None, ...] | None = None,
        defer_reconciliation: bool = False,
    ) -> tuple[int, int, int]:
        """Apply one bounded batch and isolate partial Recycle Bin failures."""

        # The empty-file phase is plan-independent but runs before the
        # duplicate-plan generator.  Always defer its successor publication so
        # that ``plan.scan_id`` still resolves to the generation containing the
        # persisted duplicate groups.  Keep the keyword optional for older
        # diagnostic wrappers that forward this private method.
        defer_reconciliation = defer_reconciliation or action_type == "trash_empty_file"
        mutation_guard = self._effective_mutation_guard()
        validated_root = self._validate_apply_root(mutation_guard=mutation_guard)
        expected, references = self._normalize_trash_snapshots(
            batch,
            expected_snapshots,
            reference_snapshots,
        )
        eligible, protected = self._begin_trash_candidates(
            action_type,
            batch,
            expected,
            references,
            mutation_guard=mutation_guard,
        )
        if not self._apply:
            self._state.finish_file_actions(
                (candidate[0] for candidate in eligible),
                "planned",
            )
            return 0, 0, protected
        active, preflight_failures = self._preflight_trash_candidates(
            action_type,
            eligible,
            validated_root=validated_root,
        )
        if not active:
            return 0, preflight_failures, protected
        # Revalidate the immutable guard once for the whole batch at the
        # mutation frontier.  Candidate identity remains a per-path check: a
        # component may have been substituted after the admission pass even
        # when the corpus root and policy objects themselves are unchanged.
        mutation_guard.require_paths_allowed(*(candidate[1] for candidate in active))
        mutation_root = self._validate_apply_root(mutation_guard=mutation_guard)
        if mutation_root is None:
            raise RuntimeError("apply mutation root is unavailable")
        ready, revalidation_failures = self._revalidate_trash_candidates(
            action_type,
            active,
            validated_root=mutation_root,
        )
        preflight_failures += revalidation_failures
        if not ready:
            return 0, preflight_failures, protected
        if self._trash_backend is None or action_type == "trash_empty_directory":
            # Directory trash remains deliberately outside the KIO adapter.
            # The injected backend is an explicit opt-in; ordinary framework
            # runs preserve their historical fail-closed behavior.
            detail = (
                TRASH_IDENTITY_ABSTENTION
                if self._trash_backend is None
                else "KIO directory trash is unsupported; only regular files are supported"
            )
            self._state.finish_file_actions(
                (candidate[0] for candidate in ready),
                "skipped",
                detail,
            )
            return 0, preflight_failures, protected + len(ready)

        batch_apply = self._optional_trash_batch_backend()
        if batch_apply is not None:
            return self._apply_trash_backend_batch(
                action_type,
                ready,
                mutation_root=mutation_root,
                failed=preflight_failures,
                protected=protected,
                apply_batch=batch_apply,
                defer_reconciliation=defer_reconciliation,
            )

        applied = 0
        failed = preflight_failures
        applied_paths: list[str] = []
        for action_id, path, planned, reference, _current_stat in ready:
            if planned is None:
                self._state.finish_file_action(
                    action_id,
                    "failed",
                    "trash candidate has no expected snapshot",
                )
                failed += 1
                continue
            try:
                source_digest = "xxh3_128_full_v1:" + full_fingerprint(planned).hex()
                if reference is not None:
                    if not files_equal_exact(planned, reference):
                        raise RuntimeError("keeper changed during exact duplicate comparison")
                expected_json = expected_identity_json(
                    planned,
                    source_path=path,
                    target_path=None,
                )
                self._state.mark_file_actions_applying(((action_id, expected_json),))
                apply_snapshot = getattr(self._trash_backend, "apply_snapshot", None)
                if callable(apply_snapshot):
                    outcome = apply_snapshot(
                        planned,
                        root=mutation_root,
                        source_digest=source_digest,
                    )
                else:
                    # Compatibility seam for older injected backends that
                    # implement the grant-style ``apply(candidate)`` only.
                    apply_effect = SimpleNamespace(
                        action="trash",
                        source=planned,
                        source_digest=source_digest,
                        keeper=None,
                        keeper_digest=None,
                        target_path=None,
                    )
                    apply_method = getattr(self._trash_backend, "apply", None)
                    if not callable(apply_method):
                        raise RuntimeError("trash backend lacks apply_snapshot(candidate)")
                    outcome = apply_method(
                        SimpleNamespace(effect=apply_effect, root=mutation_root)
                    )
                if not isinstance(outcome, BackendOutcome):
                    raise RuntimeError("trash backend returned an unsupported outcome")
                if outcome.status == "applied" and outcome.receipt_json is not None:
                    self._state.confirm_file_actions_applied(
                        ((action_id, outcome.receipt_json),)
                    )
                    applied += 1
                    applied_paths.append(path)
                    continue
                detail = outcome.detail or outcome.reason
                if outcome.status == "recovery_required":
                    self._state.require_file_action_recovery((action_id,), detail)
                    failed += 1
                else:
                    self._state.finish_file_action(action_id, "failed", detail)
                    failed += 1
            except (OSError, RuntimeError, FileChangedError, ValueError) as exc:
                # A failure for one member must not suppress independent
                # candidates in the same bounded batch.
                try:
                    row = self._state._connection.execute(
                        "SELECT status FROM file_actions WHERE action_id=?",
                        (action_id,),
                    ).fetchone()
                    if row is not None and str(row[0]) == "applying":
                        self._state.require_file_action_recovery((action_id,), str(exc))
                    elif row is not None and str(row[0]) == "started":
                        self._state.finish_file_action(action_id, "failed", str(exc))
                except BaseException as persistence_error:
                    exc.add_note(f"file action transition failed: {persistence_error}")
                failed += 1
        if applied_paths:
            if defer_reconciliation:
                self._deferred_reconciliation_paths.extend(applied_paths)
            else:
                self._index.apply_reconciliation(
                    self._scan_id,
                    remove_paths=tuple(applied_paths),
                )
        return applied, failed, protected

    def _optional_trash_batch_backend(self) -> Callable[..., object] | None:
        """Return the optional multi-snapshot seam without probing ``__getattr__``.

        ``KioTrashBackend`` exposes ``apply_many_snapshots`` (and a compatibility
        alias) only when the batch safety adapter is available.  Looking at
        ``dir`` first is intentional: an unconfigured ``MagicMock`` fabricates
        arbitrary attributes, and must continue through the individual fixture
        seam instead of being mistaken for a batch backend.
        """

        backend = self._trash_backend
        if backend is None:
            return None
        for name in ("apply_many_snapshots", "apply_snapshot_batch", "apply_batch"):
            if name not in dir(backend):
                continue
            candidate = getattr(backend, name, None)
            if callable(candidate):
                return candidate
        return None

    def _apply_trash_backend_batch(
        self,
        action_type: str,
        ready: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        *,
        mutation_root: Path,
        failed: int,
        protected: int,
        apply_batch: Callable[..., object],
        defer_reconciliation: bool,
    ) -> tuple[int, int, int]:
        """Run one optional backend batch while retaining per-item ledger rows."""

        # Compute each digest and exact-keeper check before crossing any
        # ``applying`` frontier.  One stale member is failed independently;
        # unrelated members can still use the same physical batch.
        prepared: list[tuple[int, str, FileSnapshot, str, str]] = []
        for action_id, path, planned, reference, _current_stat in ready:
            if planned is None:
                self._state.finish_file_action(
                    action_id,
                    "failed",
                    "trash candidate has no expected snapshot",
                )
                failed += 1
                continue
            try:
                source_digest = "xxh3_128_full_v1:" + full_fingerprint(planned).hex()
                if reference is not None and not files_equal_exact(planned, reference):
                    raise RuntimeError("keeper changed during exact duplicate comparison")
                expected_json = expected_identity_json(
                    planned,
                    source_path=path,
                    target_path=None,
                )
            except (OSError, RuntimeError, FileChangedError, ValueError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failed += 1
                continue
            prepared.append((action_id, path, planned, source_digest, expected_json))

        if not prepared:
            return 0, failed, protected

        # The state writer owns this transition.  It is one transaction for the
        # batch, but every action receives its own expected identity and event.
        self._state.mark_file_actions_applying(
            (action_id, expected_json) for action_id, _path, _snapshot, _digest, expected_json in prepared
        )

        try:
            batch_result = apply_batch(
                tuple((snapshot, source_digest) for _id, _path, snapshot, source_digest, _expected in prepared),
                root=mutation_root,
            )
            outcomes_value = (
                batch_result
                if isinstance(batch_result, (tuple, list))
                else getattr(batch_result, "outcomes", None)
            )
            if outcomes_value is None:
                raise RuntimeError("trash backend returned no batch outcomes")
            outcomes = tuple(outcomes_value)
            if len(outcomes) != len(prepared):
                raise RuntimeError(
                    "trash backend returned an outcome count different from the batch"
                )
        except (OSError, RuntimeError, FileChangedError, ValueError, TypeError) as exc:
            # A batch process may have crossed its physical frontier before an
            # exception reached this owner.  Never retry it as individual work;
            # preserve one recovery row for every member instead.
            detail = str(exc) or "trash backend batch outcome is unavailable"
            for action_id, _path, _snapshot, _digest, _expected in prepared:
                self._best_effort_require_recovery((action_id,), detail, exc)
            return 0, failed + len(prepared), protected
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit or an unexpected backend failure
            # may arrive after the shared physical frontier.  Preserve every
            # applying row before re-raising the control-flow interruption;
            # never retry the batch as individual operations.
            detail = str(exc) or "trash backend batch operation was interrupted"
            for action_id, _path, _snapshot, _digest, _expected in prepared:
                self._best_effort_require_recovery((action_id,), detail, exc)
            raise

        applied_paths: list[str] = []
        applied = 0
        confirmations: list[tuple[int, str, str]] = []
        for (
            action_id,
            path,
            _snapshot,
            _source_digest,
            _expected,
        ), outcome in zip(prepared, outcomes, strict=True):
            if not isinstance(outcome, BackendOutcome):
                detail = "trash backend returned an unsupported batch outcome"
                self._best_effort_require_recovery((action_id,), detail, RuntimeError(detail))
                failed += 1
                continue
            if outcome.status == "applied":
                if outcome.receipt_json is None:
                    detail = "trash backend reported applied without a receipt"
                    self._best_effort_require_recovery(
                        (action_id,), detail, RuntimeError(detail)
                    )
                    failed += 1
                    continue
                try:
                    receipt_value = json.loads(outcome.receipt_json)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._best_effort_require_recovery((action_id,), str(exc), exc)
                    failed += 1
                    continue
                if not isinstance(receipt_value, dict):
                    detail = "trash backend returned a non-object effect receipt"
                    self._best_effort_require_recovery(
                        (action_id,), detail, RuntimeError(detail)
                    )
                    failed += 1
                    continue
                confirmations.append((action_id, outcome.receipt_json, path))
                continue

            detail = outcome.detail or outcome.reason
            # All members entered ``applying`` before the shared call.  The
            # state contract therefore cannot safely transition one member
            # back to ``failed`` after the frontier; retain recovery even when
            # the backend reports a per-item block, because the batch may have
            # crossed the physical frontier for another member.
            self._best_effort_require_recovery(
                (action_id,), detail, RuntimeError(detail)
            )
            failed += 1

        if confirmations:
            try:
                self._state.confirm_file_actions_applied(
                    (action_id, receipt) for action_id, receipt, _path in confirmations
                )
            except (OSError, RuntimeError, ValueError) as exc:
                # The physical results were classified as applied, but an
                # atomic ledger confirmation failed.  Preserve recovery for
                # every member and do not reconcile an unconfirmed path.
                for action_id, _receipt, _path in confirmations:
                    self._best_effort_require_recovery((action_id,), str(exc), exc)
                failed += len(confirmations)
            else:
                applied = len(confirmations)
                applied_paths.extend(path for _action_id, _receipt, path in confirmations)

        # Reconciliation is deliberately after all receipts have crossed the
        # durable confirmation frontier, and exactly once for this batch.
        if applied_paths:
            if defer_reconciliation:
                self._deferred_reconciliation_paths.extend(applied_paths)
            else:
                self._index.apply_reconciliation(
                    self._scan_id,
                    remove_paths=tuple(applied_paths),
                )
        return applied, failed, protected

    def _best_effort_require_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
        original_error: BaseException,
    ) -> None:
        try:
            self._state.require_file_action_recovery(action_ids, detail)
        except BaseException as persistence_error:
            original_error.add_note(
                "file action remains in applying state because recovery marking "
                f"failed: {type(persistence_error).__name__}: {persistence_error}"
            )

    @staticmethod
    def _normalize_trash_snapshots(
        batch: tuple[tuple[str, str], ...],
        expected_snapshots: tuple[FileSnapshot | None, ...] | None,
        reference_snapshots: tuple[FileSnapshot | None, ...] | None,
    ) -> tuple[
        tuple[FileSnapshot | None, ...],
        tuple[FileSnapshot | None, ...],
    ]:
        expected = (None,) * len(batch) if expected_snapshots is None else expected_snapshots
        if len(expected) != len(batch):
            raise ValueError("expected snapshot count does not match trash batch")
        references = (None,) * len(batch) if reference_snapshots is None else reference_snapshots
        if len(references) != len(batch):
            raise ValueError("reference snapshot count does not match trash batch")
        return expected, references

    def _begin_trash_candidates(
        self,
        action_type: str,
        batch: tuple[tuple[str, str], ...],
        expected: tuple[FileSnapshot | None, ...],
        references: tuple[FileSnapshot | None, ...],
        *,
        mutation_guard: CorpusMutationGuard | None = None,
    ) -> tuple[
        list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]],
        int,
    ]:
        evaluated: list[
            tuple[
                tuple[str, str],
                FileSnapshot | None,
                FileSnapshot | None,
                str | None,
            ]
        ] = []
        filtered_protected = 0
        mutation_guard = mutation_guard or self._effective_mutation_guard()
        guard_paths = tuple(
            path for path, _evidence in batch if _protected_path_reason(path) is None
        )
        guard_reasons = iter(mutation_guard.mutation_path_protection_reasons(*guard_paths))
        for item, planned, reference in zip(batch, expected, references, strict=True):
            path, _evidence = item
            reason = _protected_path_reason(path)
            if reason is None:
                if next(guard_reasons) is not None:
                    # A declared Protected Content root is outside the action
                    # domain altogether; it must not acquire a file_actions row.
                    filtered_protected += 1
                    continue
            evaluated.append((item, planned, reference, reason))
        if not evaluated:
            return [], filtered_protected

        action_ids = self._state.begin_file_actions(
            self._run_id,
            (
                (
                    action_type,
                    path,
                    None,
                    "application/octet-stream",
                    evidence,
                    self._apply,
                )
                for (path, evidence), _planned, _reference, _reason in evaluated
            ),
        )
        eligible: list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]] = []
        protected_by_reason: dict[str, list[int]] = {}
        for action_id, ((path, _evidence), planned, reference, reason) in zip(
            action_ids, evaluated, strict=True
        ):
            if reason is None:
                eligible.append((action_id, path, planned, reference))
            else:
                protected_by_reason.setdefault(reason, []).append(action_id)
        for reason, protected_ids in protected_by_reason.items():
            self._state.finish_file_actions(protected_ids, "skipped", reason)
        protected = filtered_protected + sum(
            len(action_ids) for action_ids in protected_by_reason.values()
        )
        return eligible, protected

    def _preflight_trash_candidates(
        self,
        action_type: str,
        eligible: list[tuple[int, str, FileSnapshot | None, FileSnapshot | None]],
        *,
        validated_root: Path | None = None,
    ) -> tuple[
        list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        int,
    ]:
        active: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ] = []
        failures = 0
        for action_id, path, planned, reference in eligible:
            try:
                current_stat = self._validate_trash_candidate(
                    action_type,
                    path,
                    planned,
                    reference,
                    validated_root=validated_root,
                )
            except (InternalPathProtectionError, ProtectedAnalysisRootError):
                raise
            except (OSError, RuntimeError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failures += 1
                continue
            active.append((action_id, path, planned, reference, current_stat))
        return active, failures

    def _revalidate_trash_candidates(
        self,
        action_type: str,
        active: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        *,
        validated_root: Path | None = None,
    ) -> tuple[
        list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ],
        int,
    ]:
        # The first pass admits candidates independently.  This second pass is
        # deliberately adjacent to the mutating call so a component replaced
        # after preflight cannot make an otherwise-safe batch cross its root.
        ready: list[
            tuple[
                int,
                str,
                FileSnapshot | None,
                FileSnapshot | None,
                os.stat_result,
            ]
        ] = []
        failures = 0
        for action_id, path, planned, reference, original_stat in active:
            try:
                current_stat = self._validate_trash_candidate(
                    action_type,
                    path,
                    planned,
                    reference,
                    original_stat=original_stat,
                    validated_root=validated_root,
                )
            except (InternalPathProtectionError, ProtectedAnalysisRootError):
                raise
            except (OSError, RuntimeError) as exc:
                self._state.finish_file_action(action_id, "failed", str(exc))
                failures += 1
                continue
            ready.append((action_id, path, planned, reference, current_stat))
        return ready, failures

    def _validate_trash_candidate(
        self,
        action_type: str,
        path: str,
        planned: FileSnapshot | None,
        reference: FileSnapshot | None,
        *,
        original_stat: os.stat_result | None = None,
        validated_root: Path | None = None,
    ) -> os.stat_result:
        """Revalidate one source and its keeper without following reparses."""

        current_stat = (
            self._validate_action_path(path, role="trash source")
            if validated_root is None
            else _validate_mutation_path(validated_root, path, role="trash source")
        )
        if current_stat is None:
            raise RuntimeError("trash source disappeared before the operation")
        if planned is not None and not stat_matches_snapshot(planned, current_stat):
            raise RuntimeError("metadata changed after the trash candidate was planned")
        if original_stat is not None and not self._same_runtime_stat(original_stat, current_stat):
            raise RuntimeError("trash source changed after mutation preflight")
        if action_type == "trash_empty_directory":
            if planned is None:
                raise RuntimeError("empty-directory action has no expected snapshot")
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    raise RuntimeError("directory is no longer physically empty")
        if reference is not None:
            reference_stat = (
                self._validate_observation_path(
                    reference.path,
                    role="trash keeper/reference",
                )
                if validated_root is None
                else _validate_mutation_path(
                    validated_root,
                    reference.path,
                    role="trash keeper/reference",
                )
            )
            if reference_stat is None or not stat_matches_snapshot(reference, reference_stat):
                raise RuntimeError("keeper changed after exact duplicate comparison")
        return current_stat

    def _validate_action_path(
        self,
        path: str | Path,
        *,
        role: str,
        allow_missing_leaf: bool = False,
    ) -> os.stat_result | None:
        mutation_guard = self._effective_mutation_guard()
        mutation_guard.require_paths_allowed(path)
        root = self._validate_apply_root()
        if root is None:
            return None
        return _validate_mutation_path(
            root,
            path,
            role=role,
            allow_missing_leaf=allow_missing_leaf,
        )

    def _validate_observation_path(
        self,
        path: str | Path,
        *,
        role: str,
    ) -> os.stat_result | None:
        """Validate a corpus reference without treating it as a mutation target."""

        root = self._validate_apply_root()
        if root is None:
            return None
        return _validate_mutation_path(root, path, role=role)

    @staticmethod
    def _same_runtime_stat(
        original: os.stat_result,
        current: os.stat_result,
    ) -> bool:
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
        )
        if any(getattr(original, name) != getattr(current, name) for name in identity):
            return False
        original_birthtime = stat_birthtime_ns(original)
        current_birthtime = stat_birthtime_ns(current)
        return bool(original_birthtime == current_birthtime)

    def _trash_empty_files(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        candidates = self._index.file_count_by_size(plan.scan_id, 0)
        if not candidates:
            return summary
        skips_before_phase = summary.duplicate_skips
        summary = replace(
            summary,
            duplicate_candidates=summary.duplicate_candidates + candidates,
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-files",
                "Enviando archivos vacíos",
                0,
                candidates,
                "archivos",
            ),
        )
        pending: list[tuple[str, str, FileSnapshot]] = []
        completed = 0
        after_path = ""
        while True:
            page = self._index.snapshots_by_size_page(
                plan.scan_id,
                0,
                after_path=after_path,
                limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            for snapshot in page:
                pending.append((snapshot.path, "size=0;policy=trash-all-empty", snapshot))
            after_path = page[-1].path
            applied, failed, protected = self._apply_trash_batch(
                "trash_empty_file",
                tuple((path, evidence) for path, evidence, _snapshot in pending),
                expected_snapshots=tuple(snapshot for _path, _evidence, snapshot in pending),
            )
            completed += len(pending)
            pending.clear()
            summary = replace(
                summary,
                duplicates_trashed=summary.duplicates_trashed + applied,
                duplicate_skips=summary.duplicate_skips + failed + protected,
                errors=summary.errors + failed,
            )
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "empty-files",
                    "Enviando archivos vacíos",
                    completed,
                    candidates,
                    "archivos",
                ),
            )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "empty-files",
                "Archivos vacíos procesados",
                completed,
                candidates,
                "archivos",
                True,
                (
                    ProgressMetric(
                        "planned",
                        (
                            max(0, candidates - (summary.duplicate_skips - skips_before_phase))
                            if not self._apply
                            else 0
                        ),
                    ),
                    ProgressMetric("applied", summary.duplicates_trashed),
                ),
            ),
        )
        return summary

    def _trash_duplicates(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        candidates = plan.redundant_files
        summary = replace(
            summary,
            duplicate_candidates=summary.duplicate_candidates + candidates,
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "duplicates",
                "Procesando duplicados",
                0,
                candidates,
                "archivos",
            ),
        )
        completed = 0
        applied_before_phase = summary.duplicates_trashed
        skips_before_phase = summary.duplicate_skips
        pending: list[tuple[str, str, FileSnapshot, FileSnapshot]] = []

        def progress_metrics() -> tuple[ProgressMetric, ...]:
            skipped = max(0, summary.duplicate_skips - skips_before_phase)
            return (
                ProgressMetric(
                    "planned",
                    max(0, candidates - skipped) if not self._apply else 0,
                ),
                ProgressMetric(
                    "applied",
                    max(0, summary.duplicates_trashed - applied_before_phase),
                ),
            )

        def report() -> None:
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "duplicates",
                    "Procesando duplicados",
                    completed,
                    candidates,
                    "archivos",
                    metrics=progress_metrics(),
                ),
            )

        def flush_pending() -> None:
            nonlocal completed, summary
            if not pending:
                return
            batch = tuple((path, evidence) for path, evidence, _snapshot, _reference in pending)
            expected = tuple(snapshot for _path, _evidence, snapshot, _reference in pending)
            references = tuple(reference for _path, _evidence, _snapshot, reference in pending)
            pending.clear()
            applied, failed, protected = self._apply_trash_batch(
                "trash_duplicate",
                batch,
                expected_snapshots=expected,
                reference_snapshots=references,
            )
            summary = replace(
                summary,
                duplicates_trashed=summary.duplicates_trashed + applied,
                duplicate_skips=summary.duplicate_skips + failed + protected,
                errors=summary.errors + failed,
            )
            completed += len(batch)
            report()

        def fail_candidate(path: str, evidence: str, detail: str) -> None:
            nonlocal completed, summary
            protected_reason = _protected_path_reason(path)
            if protected_reason is None:
                protected_reason = self._protected_content_skip_reason(path)
            if protected_reason is not None:
                summary = replace(
                    summary,
                    duplicate_skips=summary.duplicate_skips + 1,
                )
                completed += 1
                report()
                return
            action_id = self._state.begin_file_action(
                self._run_id,
                "trash_duplicate",
                path,
                None,
                "application/octet-stream",
                evidence,
                self._apply,
            )
            self._state.finish_file_action(action_id, "failed", detail)
            summary = replace(
                summary,
                duplicate_skips=summary.duplicate_skips + 1,
                errors=summary.errors + 1,
            )
            completed += 1
            report()

        for group in self._index.iter_duplicate_groups(plan.scan_id):
            evidence = (
                f"xxh3-128={group.full_fingerprint};"
                f"byte-for-byte={str(self._verify_bytes_before_trash).lower()};"
                f"keep={group.keep.path}"
            )
            keep_now, keep_error = self._validated_duplicate_keeper(group.keep)
            for redundant in group.redundant:
                if keep_error is not None:
                    fail_candidate(redundant.path, evidence, keep_error)
                    continue
                if self._apply:
                    try:
                        redundant_now = snapshot_path(redundant.path)
                        if not _same_snapshot(redundant, redundant_now):
                            raise RuntimeError("metadata changed after exact duplicate planning")
                        assert keep_now is not None
                        if self._verify_bytes_before_trash and not files_equal_exact(
                            keep_now, redundant_now
                        ):
                            raise RuntimeError("content changed after exact duplicate planning")
                    except (OSError, RuntimeError, FileChangedError) as exc:
                        fail_candidate(redundant.path, evidence, str(exc))
                        continue
                pending.append((redundant.path, evidence, redundant, group.keep))
                if len(pending) >= TRASH_BATCH_SIZE:
                    flush_pending()
        flush_pending()
        # Empty-file effects intentionally defer reconciliation: publishing an
        # inventory successor before this generator is exhausted would make
        # ``plan.scan_id`` resolve to a generation without its duplicate plan.
        self._flush_deferred_reconciliation()
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "duplicates",
                "Duplicados procesados",
                candidates,
                candidates,
                "archivos",
                True,
                progress_metrics(),
            ),
        )
        return summary

    def _validated_duplicate_keeper(
        self,
        planned: FileSnapshot,
    ) -> tuple[FileSnapshot | None, str | None]:
        if not self._apply:
            return None, None
        try:
            current = snapshot_path(planned.path)
            if not _same_snapshot(planned, current):
                raise RuntimeError("keeper metadata changed after duplicate planning")
        except (OSError, RuntimeError) as exc:
            return None, str(exc)
        return current, None

    def _validate_extensions(self, plan: DedupPlan, summary: ActionSummary) -> ActionSummary:
        # Keep direct phase callers safe as well as the normal ``execute``
        # route, whose duplicate phase normally flushes this queue first.
        self._flush_deferred_reconciliation()
        # The inventory is the physical source of truth for this pass.  A
        # dry-run only records proposed actions; it does not remove any
        # inventory member from the route input set.  In particular, a
        # planned duplicate is still a real file and must retain its identity
        # and content-type coverage until an effect is actually observed.
        # Applied runs may read the same immutable inventory snapshot because
        # the admission check below rejects sources that were really removed
        # (or changed) before route publication.
        # Empty files have their own explicit, planned ``trash_empty_file``
        # record and are not content-route inputs.  Keep them out of this
        # content-type denominator, but never subtract proposed duplicate
        # files: unlike an observed effect, a dry-run proposal leaves those
        # physical sources available for extraction.
        total = self._index.file_count(self._scan_id) - self._index.file_count_by_size(
            self._scan_id, 0
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "content-types",
                "Validando tipos de contenido",
                0,
                total,
                "archivos",
            ),
        )
        completed = 0
        route_candidates: list[tuple[str, FileSnapshot]] = []
        cache_updates: list[tuple[FileSnapshot, DetectedType | None]] = []

        def flush_route_candidates() -> None:
            if route_candidates:
                self._state.store_route_candidates(self._run_id, route_candidates)
                route_candidates.clear()

        def flush_cache_updates() -> None:
            if cache_updates:
                self._state.store_content_type_cache_batch(
                    cache_updates, DETECTOR_VERSION, self._run_id
                )
                cache_updates.clear()

        def report_progress() -> None:
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "content-types",
                    "Validando tipos de contenido",
                    completed,
                    total,
                    "archivos",
                ),
            )

        # Read bounded pages through the already-open inventory owner.  The
        # page tuple closes its SQLite cursor before this loop can persist
        # route/cache state or apply an extension rename; reopening the same
        # WAL-backed inventory here can exhaust the temporary snapshot budget.
        after_path = ""
        while True:
            page = self._index.snapshots_page(
                self._scan_id,
                after_path=after_path,
                limit=TRASH_BATCH_SIZE,
            )
            if not page:
                break
            after_path = page[-1].path
            for planned in page:
                if planned.size == 0:
                    continue
                completed += 1
                summary, route_candidate, cache_update = self._inspect_content_type_candidate(
                    planned, summary
                )
                if cache_update is not None:
                    cache_updates.append(cache_update)
                    if len(cache_updates) >= 1000:
                        flush_cache_updates()
                if route_candidate is not None:
                    route_candidates.append(route_candidate)
                    if len(route_candidates) >= 1000:
                        flush_route_candidates()
                report_progress()
        flush_route_candidates()
        flush_cache_updates()
        summary = replace(
            summary,
            type_cache_pruned=self._state.prune_content_type_cache(self._run_id, DETECTOR_VERSION),
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "content-types",
                "Validación de tipos completada",
                completed,
                completed,
                "archivos",
                True,
            ),
        )
        return summary

    def _flush_deferred_reconciliation(self) -> None:
        """Publish deferred empty-file removals after the duplicate plan pass."""

        if not self._deferred_reconciliation_paths:
            return
        paths = tuple(self._deferred_reconciliation_paths)
        self._index.apply_reconciliation(
            self._scan_id,
            remove_paths=paths,
        )
        # Clear only after the owner has acknowledged the successor.  If the
        # reconciliation raises, the paths remain available to an explicit
        # retry by the caller rather than being silently discarded.
        self._deferred_reconciliation_paths.clear()

    def _inspect_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[
        ActionSummary,
        tuple[str, FileSnapshot] | None,
        tuple[FileSnapshot, DetectedType | None] | None,
    ]:
        summary, admitted = self._admit_content_type_candidate(planned, summary)
        if not admitted:
            return summary, None, None
        summary, detected, usable = self._detect_planned_content_type(
            planned,
            summary,
        )
        if not usable:
            return summary, None, None
        summary, route_candidate = self._classify_detected_content_type(
            planned,
            detected,
            summary,
        )
        return summary, route_candidate, (planned, detected)

    def _admit_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, bool]:
        if _protected_path_reason(planned.path, check_attributes=True) is not None:
            return summary, False
        try:
            current = snapshot_path(planned.path)
        except FileNotFoundError:
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        except OSError as exc:
            self._record_content_type_error(planned, exc)
            return replace(
                summary,
                files_checked=summary.files_checked + 1,
                errors=summary.errors + 1,
            ), False
        if not _same_snapshot(planned, current):
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        return replace(
            summary,
            files_checked=summary.files_checked + 1,
        ), True

    def _detect_planned_content_type(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, DetectedType | None, bool]:
        cache_hit, detected = self._state.get_content_type_cache(
            planned,
            DETECTOR_VERSION,
        )
        if cache_hit:
            return (
                replace(
                    summary,
                    type_cache_hits=summary.type_cache_hits + 1,
                ),
                detected,
                True,
            )
        summary = replace(
            summary,
            type_cache_misses=summary.type_cache_misses + 1,
        )
        try:
            detected = detect_content_type(planned.path)
            refreshed = snapshot_path(planned.path)
        except FileNotFoundError:
            return (
                replace(
                    summary,
                    stale_inventory=summary.stale_inventory + 1,
                ),
                None,
                False,
            )
        except OSError as exc:
            self._record_content_type_error(planned, exc)
            return replace(summary, errors=summary.errors + 1), None, False
        if not _same_snapshot(planned, refreshed):
            return (
                replace(
                    summary,
                    stale_inventory=summary.stale_inventory + 1,
                ),
                None,
                False,
            )
        return summary, detected, True

    def _classify_detected_content_type(
        self,
        planned: FileSnapshot,
        detected: DetectedType | None,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, tuple[str, FileSnapshot] | None]:
        if detected is None:
            return replace(
                summary,
                unknown_types=summary.unknown_types + 1,
            ), None
        summary = replace(summary, types_detected=summary.types_detected + 1)
        if detected.accepts(planned.path):
            return replace(
                summary,
                extensions_matching=summary.extensions_matching + 1,
            ), (detected.mime, planned)
        summary = self._rename_mismatch(planned, detected, summary)
        target = _corrected_path(Path(planned.path), detected.canonical_extension)
        actual_path = (
            target if target.is_file() and not Path(planned.path).exists() else Path(planned.path)
        )
        return summary, (detected.mime, replace(planned, path=str(actual_path)))

    def _record_content_type_error(
        self,
        planned: FileSnapshot,
        error: OSError,
    ) -> None:
        protected_reason = self._protected_content_skip_reason(planned.path)
        if protected_reason is not None:
            self._state.record_event(
                self._run_id,
                "error",
                "content-types",
                "Protected content inspection failed",
                {
                    "actionable": False,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "path": planned.path,
                    "protected_reason": protected_reason,
                },
            )
            return
        action_id = self._state.begin_file_action(
            self._run_id,
            "validate_content_type",
            planned.path,
            None,
            None,
            None,
            self._apply,
        )
        self._state.finish_file_action(action_id, "failed", str(error))

    def _rename_protected_reason(self, source: Path, target: Path) -> str | None:
        protected_reason = _protected_path_reason(source)
        if protected_reason is None:
            protected_reason = _protected_path_reason(
                target,
                check_attributes=False,
            )
        if protected_reason is None:
            protected_reason = self._protected_content_skip_reason(source, target)
        return protected_reason

    def _begin_rename_action(
        self,
        source: Path,
        target: Path,
        detected: DetectedType,
    ) -> int:
        return self._state.begin_file_action(
            self._run_id,
            "correct_extension",
            str(source),
            str(target),
            detected.mime,
            detected.evidence,
            self._apply,
        )

    def _rename_mismatch(self, planned, detected, summary: ActionSummary) -> ActionSummary:
        """Record an extension correction without a product mutation backend."""

        source = Path(planned.path)
        target = _corrected_path(source, detected.canonical_extension)
        summary = replace(summary, rename_candidates=summary.rename_candidates + 1)
        if self._rename_protected_reason(source, target) is not None:
            return replace(summary, rename_skips=summary.rename_skips + 1)
        action_id = self._begin_rename_action(source, target, detected)
        if not self._apply:
            self._state.finish_file_action(action_id, "planned")
            return summary
        try:
            self._validate_apply_root()
            self._validate_action_path(source, role="rename source")
            self._validate_action_path(
                target,
                role="rename target",
                allow_missing_leaf=True,
            )
        except (InternalPathProtectionError, ProtectedAnalysisRootError):
            raise
        except (OSError, RuntimeError) as exc:
            self._state.finish_file_action(action_id, "failed", str(exc))
            return replace(
                summary,
                rename_skips=summary.rename_skips + 1,
                errors=summary.errors + 1,
            )
        self._state.finish_file_action(
            action_id,
            "skipped",
            "linux_mutation_backend_unavailable",
        )
        return replace(summary, rename_skips=summary.rename_skips + 1)

    def _protected_content_skip_reason(
        self,
        *paths: str | Path,
    ) -> str | None:
        """Return only a content-policy denial; propagate systemic denials."""

        try:
            self._effective_mutation_guard().require_paths_allowed(*paths)
        except ProtectedContentError as exc:
            return str(exc)
        return None

    def _validate_apply_root(
        self,
        *,
        mutation_guard: CorpusMutationGuard | None = None,
    ) -> Path | None:
        """Revalidate the mutation boundary immediately before an action."""

        if not self._apply:
            return None
        mutation_guard = mutation_guard or self._effective_mutation_guard()
        mutation_guard.reject_run_mutation()
        recorded_root = self._index.scan_root(self._scan_id)
        recorded_volume, recorded_file, recorded_birthtime = self._index.scan_root_identity(
            self._scan_id
        )
        run_policy = mutation_guard.policy
        run_identity = (
            run_policy.root_device_id,
            run_policy.root_file_id,
            run_policy.root_birthtime_ns,
        )
        if (
            _path_key(run_policy.root) != _path_key(recorded_root)
            or None in run_identity
            or run_identity != (recorded_volume, recorded_file, recorded_birthtime)
        ):
            raise RuntimeError(
                "framework run root does not match the inventory scan root: "
                f"run={run_policy.root}; scan={recorded_root}"
            )
        current_root = validate_inventory_root(recorded_root)
        if _path_key(recorded_root) != _path_key(current_root):
            raise RuntimeError(
                "inventory root no longer resolves to its recorded canonical path: "
                f"{recorded_root} -> {current_root}"
            )
        current = snapshot_path(current_root)
        if (
            current.identity != (recorded_volume, recorded_file)
            or current.birthtime_ns != recorded_birthtime
        ):
            raise RuntimeError(
                f"inventory root identity changed after the scan was recorded: {recorded_root}"
            )
        return current_root

    def _effective_mutation_guard(self) -> CorpusMutationGuard:
        """Reload the current fail-closed guard at every mutation boundary."""

        return self._state.corpus_mutation_guard(self._run_id)


def apply_exact_dedupe_plan(
    index: DedupIndex,
    state: FrameworkState,
    run_id: int,
    plan: DedupPlan,
    *,
    trash_backend: KioTrashBackend | None = None,
    excluded_paths: Iterable[str | Path] = DEFAULT_EXCLUDED_PATHS,
    exclusion_policy: InventoryExclusionPolicy | None = None,
    progress: ProgressCallback | None = None,
) -> ActionSummary:
    """Apply an existing, complete exact-dedup plan through native KIO.

    This is intentionally not a planner: callers provide the already
    published ``DedupPlan`` and its inventory owner.  Only duplicate members
    are processed; extension corrections, empty files/directories, and any
    non-exact or partial plan remain outside this E1 service.
    """

    if plan.verification_mode != "full_hash" or plan.requested_policy != "exact":
        raise ValueError("exact dedupe application requires a complete full-hash plan")
    if plan.coverage != "complete":
        raise ValueError("exact dedupe application requires complete plan coverage")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be positive")
    runner = FrameworkActions(
        index,
        state,
        run_id,
        plan.scan_id,
        apply=True,
        verify_bytes_before_trash=True,
        excluded_paths=excluded_paths,
        exclusion_policy=exclusion_policy,
        progress=progress,
        trash_backend=trash_backend or KioTrashBackend(),
    )
    summary = runner._trash_duplicates(plan, ActionSummary(apply_actions=True))
    state.store_action_summary(run_id, summary)
    return summary


# A short alias is useful to importers that already use the noun "dedupe";
# both names intentionally point at the same no-planner service.
apply_exact_dedupe = apply_exact_dedupe_plan


__all__ = ["FrameworkActions", "apply_exact_dedupe", "apply_exact_dedupe_plan"]
# endregion [02]
