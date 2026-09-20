"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

from collections.abc import Iterable

from neocortex.runtime.models import ActionSummary
from neocortex.persistence.framework_state_common import (
    FileActionSpec,
    begin_file_actions,
    confirm_file_actions_applied,
    finish_file_actions,
    mark_file_actions_applying,
)
from neocortex.workflow.actions.file_action_recovery import FileActionReconciliation
from neocortex.workflow.actions.file_action_reconciliation_store import (
    RecordedFileActionReconciliation,
    record_file_action_reconciliation,
)

class FrameworkStateActionsMixin:
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def begin_file_action(
        self,
        run_id: int,
        action_type: str,
        source_path: str,
        target_path: str | None,
        detected_mime: str | None,
        evidence: str | None,
        apply_requested: bool,
    ) -> int:
        return self.begin_file_actions(
            run_id,
            (
                (
                    action_type,
                    source_path,
                    target_path,
                    detected_mime,
                    evidence,
                    apply_requested,
                ),
            ),
        )[0]

    def begin_file_actions(
        self,
        run_id: int,
        actions: Iterable[FileActionSpec],
    ) -> list[int]:
        """Insert a bounded action batch in one transaction."""

        return begin_file_actions(self._connection, run_id, actions)

    def finish_file_action(self, action_id: int, status: str, detail: str | None = None) -> None:
        self.finish_file_actions((action_id,), status, detail)

    def finish_file_actions(
        self,
        action_ids: Iterable[int],
        status: str,
        detail: str | None = None,
    ) -> None:
        """Complete a bounded action batch in one transaction."""

        finish_file_actions(self._connection, action_ids, status, detail)

    def mark_file_actions_applying(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Persist expected identities before any filesystem syscall."""

        mark_file_actions_applying(self._connection, actions)

    def confirm_file_actions_applied(
        self,
        actions: Iterable[tuple[int, str]],
    ) -> None:
        """Store successful syscall receipts through an applying-state CAS."""

        confirm_file_actions_applied(self._connection, actions)

    def require_file_action_recovery(
        self,
        action_ids: Iterable[int],
        detail: str,
    ) -> None:
        """Preserve an uncertain post-frontier effect without retrying it."""

        finish_file_actions(
            self._connection,
            action_ids,
            "recovery_required",
            detail,
        )

    def record_file_action_reconciliation(
        self,
        reconciliation: FileActionReconciliation,
        *,
        actor: str,
        provenance_json: str,
        expected_previous_event_id: int | None,
        observed_ns: int | None = None,
    ) -> RecordedFileActionReconciliation:
        """Append read-only observation evidence; never retry the action."""

        return record_file_action_reconciliation(
            self._connection,
            reconciliation,
            actor=actor,
            provenance_json=provenance_json,
            expected_previous_event_id=expected_previous_event_id,
            observed_ns=observed_ns,
        )

    def store_action_summary(self, run_id: int, summary: ActionSummary) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO run_actions("
                "run_id,apply_actions,duplicate_candidates,duplicates_trashed,"
                "duplicate_skips,files_checked,types_detected,extensions_matching,"
                "unknown_types,type_cache_hits,type_cache_misses,type_cache_pruned,"
                "stale_inventory,"
                "rename_candidates,files_renamed,rename_skips,"
                "empty_directory_candidates,empty_directories_trashed,"
                "empty_directory_skips,errors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    int(summary.apply_actions),
                    summary.duplicate_candidates,
                    summary.duplicates_trashed,
                    summary.duplicate_skips,
                    summary.files_checked,
                    summary.types_detected,
                    summary.extensions_matching,
                    summary.unknown_types,
                    summary.type_cache_hits,
                    summary.type_cache_misses,
                    summary.type_cache_pruned,
                    summary.stale_inventory,
                    summary.rename_candidates,
                    summary.files_renamed,
                    summary.rename_skips,
                    summary.empty_directory_candidates,
                    summary.empty_directories_trashed,
                    summary.empty_directory_skips,
                    summary.errors,
                ),
            )

__all__ = ["FrameworkStateActionsMixin"]
