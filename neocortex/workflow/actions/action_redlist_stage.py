"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

# mypy: disable-error-code=attr-defined

import hashlib
import json
import os
from pathlib import Path

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.progress import ProgressEvent, ProgressMetric, emit_progress
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.workflow.actions.action_contracts import (
    REDLIST_REASON_CODE_LIMIT,
    REDLIST_REASON_EXAMPLE_LIMIT,
    RedlistPrepassError,
    TRASH_BATCH_SIZE,
    _redlist_reason_code,
)

class RedlistActionsMixin:
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    def _reset_redlist_diagnostics(self, *, clear_exclusions: bool = False) -> None:
        if clear_exclusions:
            self._redlist_excluded_paths.clear()
        self._redlist_diagnostics = {
            "blocked": 0,
            "protected": 0,
            "failed_pre_effect": 0,
            "recovery_required": 0,
            "reason_codes": {},
            "examples": [],
        }
        self._redlist_batch_diagnostics = {}

    @staticmethod
    def _redlist_path_digest(path: str | Path) -> str:
        """Return a bounded path example without persisting the path itself."""

        return hashlib.sha256(os.fsencode(str(path))).hexdigest()

    def _record_redlist_diagnostic(
        self,
        category: str,
        reason: object,
        path: str | Path | None = None,
    ) -> None:
        if category not in {"blocked", "protected", "failed_pre_effect", "recovery_required"}:
            return
        details = self._redlist_diagnostics
        details[category] = int(details.get(category, 0)) + 1
        code = _redlist_reason_code(reason)
        reason_codes = details.get("reason_codes")
        if not isinstance(reason_codes, dict):
            reason_codes = {}
            details["reason_codes"] = reason_codes
        if len(reason_codes) < REDLIST_REASON_CODE_LIMIT or code in reason_codes:
            reason_codes[code] = int(reason_codes.get(code, 0)) + 1
        examples = details.get("examples")
        if not isinstance(examples, list):
            examples = []
            details["examples"] = examples
        if path is not None and len(examples) < REDLIST_REASON_EXAMPLE_LIMIT:
            examples.append(
                {
                    "category": category,
                    "reason_code": code,
                    "path_digest": self._redlist_path_digest(path),
                }
            )

    def _begin_redlist_batch_diagnostics(self) -> None:
        self._redlist_batch_diagnostics = {
            "blocked": 0,
            "protected": 0,
            "failed_pre_effect": 0,
            "recovery_required": 0,
            "reason_codes": {},
            "examples": [],
        }

    def _record_redlist_batch_diagnostic(
        self,
        category: str,
        reason: object,
        path: str | Path | None = None,
    ) -> None:
        previous = self._redlist_diagnostics
        self._redlist_diagnostics = self._redlist_batch_diagnostics
        try:
            self._record_redlist_diagnostic(category, reason, path)
        finally:
            self._redlist_diagnostics = previous

    def _consume_redlist_batch_diagnostics(self) -> dict[str, object]:
        batch = self._redlist_batch_diagnostics
        self._redlist_batch_diagnostics = {}
        return batch

    def _merge_redlist_batch_diagnostics(self, batch: dict[str, object]) -> None:
        for category in ("blocked", "protected", "failed_pre_effect", "recovery_required"):
            count = self._redlist_counter(batch, category)
            if count:
                self._redlist_diagnostics[category] = (
                    self._redlist_counter(self._redlist_diagnostics, category) + count
                )
        source_codes = batch.get("reason_codes")
        target_codes = self._redlist_diagnostics.get("reason_codes")
        if isinstance(source_codes, dict) and isinstance(target_codes, dict):
            for raw_code, raw_count in source_codes.items():
                code = _redlist_reason_code(raw_code)
                if type(raw_count) is not int or raw_count < 1:
                    continue
                if len(target_codes) >= REDLIST_REASON_CODE_LIMIT and code not in target_codes:
                    continue
                target_codes[code] = int(target_codes.get(code, 0)) + raw_count
        source_examples = batch.get("examples")
        target_examples = self._redlist_diagnostics.get("examples")
        if isinstance(source_examples, list) and isinstance(target_examples, list):
            target_examples.extend(source_examples[: max(0, REDLIST_REASON_EXAMPLE_LIMIT - len(target_examples))])

    @staticmethod
    def _redlist_counter(details: dict[str, object], name: str) -> int:
        value = details.get(name, 0)
        return value if type(value) is int and value >= 0 else 0

    def _redlist_is_excluded(self, path: str | Path) -> bool:
        candidate = str(path)
        if candidate in self._redlist_excluded_paths:
            return True
        if not self._redlist_policy_active:
            return False
        # The prepass and the action runner are separate instances in the
        # integrated flow.  Re-evaluating the explicit metadata-only policy is
        # safe, bounded, and keeps a protected redlisted source out of route
        # publication without opening payload bytes.
        from neocortex.workflow.actions.redlist import redlist_match

        policy_path = self._normalized_paths.get(candidate, candidate)
        return redlist_match(policy_path) is not None

    def _publish_redlist_stage(
        self,
        *,
        status: str,
        policy_digest: str,
        matched: int,
        applied: int,
        failed: int,
        protected: int,
        blocked: int = 0,
        failed_pre_effect: int = 0,
        recovery_required: int = 0,
        planned: int = 0,
        skipped: int = 0,
        reason_codes: dict[str, int] | None = None,
        examples: list[dict[str, object]] | tuple[dict[str, object], ...] = (),
        error: BaseException | None = None,
    ) -> None:
        """Publish bounded redlist counters through the Framework lifecycle.

        The redlist is an integrated stage, not merely an action-side event.
        Keeping the counters in the stage details makes cancellation, budget
        exhaustion, and partial physical outcomes visible to the public status
        reader without serializing paths or payload bytes.
        """

        from neocortex.workflow.actions.redlist import REDLIST_POLICY_SCHEMA

        details: dict[str, object] = {
            "schema": REDLIST_POLICY_SCHEMA,
            "policy_digest": policy_digest,
            "matched": max(0, int(matched)),
            "applied": max(0, int(applied)),
            "failed": max(0, int(failed)),
            "protected": max(0, int(protected)),
            "blocked": max(0, int(blocked)),
            "failed_pre_effect": max(0, int(failed_pre_effect)),
            "recovery_required": max(0, int(recovery_required)),
            "planned": max(0, int(planned)),
            "skipped": max(0, int(skipped)),
            "reason_codes": dict(reason_codes or {}),
            "examples": list(examples)[:REDLIST_REASON_EXAMPLE_LIMIT],
        }
        if error is not None:
            details.update(
                {
                    "error_type": type(error).__name__,
                    "error": str(error)[:8192],
                }
            )
        publish_stage = getattr(self._state, "publish_run_stage", None)
        read_manifest = getattr(self._state, "read_run_manifest", None)
        if (
            callable(publish_stage)
            and callable(read_manifest)
            and read_manifest(self._run_id) is not None
        ):
            publish_stage(
                self._run_id,
                "redlist",
                status,
                details=details,
                idempotency_key=f"redlist:{policy_digest}:{status}",
            )
            return
        # Direct action callers may use a pre-manifest fixture run.  Preserve
        # their diagnostic evidence without pretending it is a lifecycle stage.
        self._state.record_event(
            self._run_id,
            "error"
            if status == "failed"
            else "warning"
            if status in {"interrupted", "partial"}
            else "info",
            "redlist",
            f"Redlist stage {status}",
            details,
        )

    def apply_redlist_prepass(self, *, policy_digest: str) -> dict[str, object]:
        """Trash configured redlist matches before content planning.

        The inventory has already captured metadata, but no content bytes have
        been read.  This pass deliberately uses only the explicit basename or
        final-suffix policy.  The only content binding used by the Trash
        safety adapter is the metadata-only source binding; it is not a
        content hash and exists solely to bind the physical effect to the
        preflighted inode/metadata snapshot.
        """

        from neocortex.workflow.actions.redlist import (
            REDLIST_POLICY_SCHEMA,
            redlist_match,
        )

        matched = applied = failed = protected = 0
        blocked = failed_pre_effect = recovery_required = 0
        planned = skipped = 0
        self._reset_redlist_diagnostics(clear_exclusions=True)
        self._redlist_prepass_active = True
        self._redlist_policy_active = True
        self._redlist_suppress_late_mutation = True
        pending: list[tuple[str, str, FileSnapshot]] = []
        after_path = ""
        self._publish_redlist_stage(
            status="running",
            policy_digest=policy_digest,
            matched=matched,
            applied=applied,
            failed=failed,
            protected=protected,
            blocked=blocked,
            failed_pre_effect=failed_pre_effect,
            recovery_required=recovery_required,
        )
        try:
            while True:
                self._checkpoint()
                page = self._index.snapshots_page(
                    self._scan_id,
                    after_path=after_path,
                    limit=TRASH_BATCH_SIZE,
                )
                if not page:
                    break
                # Redlist matching is metadata-only, but it still consumes the
                # The redlist pass still consumes bounded lifecycle work. Reserve
                # classifying or effecting any member.  Bytes remain zero: no
                # payload is read by this policy.
                self._redlist_page_reserved = self._reserve_work is not None
                self._reserve_snapshot_work(
                    "redlist-page",
                    page,
                    bytes_override=0,
                )
                for snapshot in page:
                    # Redlist matching is an action/effect decision.  Keep
                    # the global content-admission gate ahead of it so an
                    # oversize source remains untouched and cannot acquire a
                    # redlist ledger row under this run's temporary policy.
                    if not self._size_is_admitted(snapshot):
                        continue
                    policy_path = self._normalized_paths.get(snapshot.path, snapshot.path)
                    token = redlist_match(policy_path)
                    if token is None:
                        continue
                    matched += 1
                    self._redlist_excluded_paths.add(str(snapshot.path))
                    evidence = json.dumps(
                        {
                            "schema": REDLIST_POLICY_SCHEMA,
                            "policy_digest": policy_digest,
                            "redlist_entry": token,
                            "policy_path": policy_path,
                            "match": "basename_or_suffix_casefold_v1",
                            "snapshot": {
                                "volume_id": snapshot.volume_id,
                                "file_id": snapshot.file_id,
                                "size": snapshot.size,
                                "mtime_ns": snapshot.mtime_ns,
                                "birthtime_ns": snapshot.birthtime_ns,
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    pending.append((snapshot.path, evidence, snapshot))
                    if len(pending) >= TRASH_BATCH_SIZE:
                        self._begin_redlist_batch_diagnostics()
                        a, f, p = self._apply_trash_batch(
                            "trash_redlist",
                            tuple((path, evidence) for path, evidence, _ in pending),
                            expected_snapshots=tuple(snapshot for _, _, snapshot in pending),
                            defer_reconciliation=True,
                        )
                        applied += a
                        failed += f
                        batch = self._consume_redlist_batch_diagnostics()
                        blocked += self._redlist_counter(batch, "blocked")
                        failed_pre_effect += self._redlist_counter(
                            batch, "failed_pre_effect"
                        )
                        recovery_required += self._redlist_counter(
                            batch, "recovery_required"
                        )
                        protected += max(
                            0,
                            p
                            - self._redlist_counter(batch, "blocked")
                            - self._redlist_counter(batch, "failed_pre_effect")
                            - self._redlist_counter(batch, "recovery_required"),
                        )
                        self._merge_redlist_batch_diagnostics(batch)
                        pending.clear()
                self._redlist_page_reserved = False
                after_path = page[-1].path
                emit_progress(
                    self._progress,
                    ProgressEvent(
                        "framework",
                        "redlist",
                        "Enviando redlist a Papelera",
                        matched,
                        None,
                        "archivos",
                        metrics=(
                            ProgressMetric("applied", applied),
                            ProgressMetric("errors", failed),
                        ),
                    ),
                )
            if pending:
                # The final page has already been reserved above; avoid a
                # second reservation for this final partial batch.
                self._redlist_page_reserved = self._reserve_work is not None
                try:
                    self._begin_redlist_batch_diagnostics()
                    a, f, p = self._apply_trash_batch(
                        "trash_redlist",
                        tuple((path, evidence) for path, evidence, _ in pending),
                        expected_snapshots=tuple(snapshot for _, _, snapshot in pending),
                        defer_reconciliation=True,
                    )
                finally:
                    self._redlist_page_reserved = False
                applied += a
                failed += f
                batch = self._consume_redlist_batch_diagnostics()
                blocked += self._redlist_counter(batch, "blocked")
                failed_pre_effect += self._redlist_counter(batch, "failed_pre_effect")
                recovery_required += self._redlist_counter(batch, "recovery_required")
                protected += max(
                    0,
                    p
                    - self._redlist_counter(batch, "blocked")
                    - self._redlist_counter(batch, "failed_pre_effect")
                    - self._redlist_counter(batch, "recovery_required"),
                )
                self._merge_redlist_batch_diagnostics(batch)
            planned = (
                max(0, matched - applied - failed - protected - blocked)
                if not self._apply
                else 0
            )
            skipped = failed_pre_effect + blocked + protected
            if self._apply and recovery_required:
                # Protected/blocked/pre-effect denials never cross a physical
                # frontier and therefore do not justify a recovery abort.  An
                # actual ambiguity remains fail-closed and is the sole fatal
                # redlist outcome.
                raise RedlistPrepassError(
                    matched=matched,
                    applied=applied,
                    failed=failed,
                    protected=protected,
                    blocked=blocked,
                    failed_pre_effect=failed_pre_effect,
                    recovery_required=recovery_required,
                    reason_codes=dict(self._redlist_diagnostics["reason_codes"]),
                    examples=tuple(self._redlist_diagnostics["examples"]),
                )
            if applied:
                self._flush_deferred_reconciliation()
                self._index.refresh_scan_aggregates(
                    self._index.current_scan_id(self._scan_id)
                )
        except (KeyboardInterrupt, CancellationRequested, RunBudgetExceeded) as exc:
            self._redlist_page_reserved = False
            self._redlist_prepass_active = False
            self._publish_redlist_stage(
                status="interrupted",
                policy_digest=policy_digest,
                matched=matched,
                applied=applied,
                failed=failed,
                protected=protected,
                blocked=blocked,
                failed_pre_effect=failed_pre_effect,
                recovery_required=recovery_required,
                reason_codes=dict(self._redlist_diagnostics["reason_codes"]),
                examples=list(self._redlist_diagnostics["examples"]),
                error=exc,
            )
            raise
        except BaseException as exc:
            self._redlist_page_reserved = False
            self._redlist_prepass_active = False
            self._publish_redlist_stage(
                status="failed",
                policy_digest=policy_digest,
                matched=matched,
                applied=applied,
                failed=failed,
                protected=protected,
                blocked=blocked,
                failed_pre_effect=failed_pre_effect,
                recovery_required=recovery_required,
                reason_codes=dict(self._redlist_diagnostics["reason_codes"]),
                examples=list(self._redlist_diagnostics["examples"]),
                error=exc,
            )
            raise
        self._redlist_prepass_active = False
        stage_status = (
            "partial"
            if blocked or protected or failed_pre_effect
            else "completed"
        )
        self._publish_redlist_stage(
            status=stage_status,
            policy_digest=policy_digest,
            matched=matched,
            applied=applied,
            failed=failed,
            protected=protected,
            blocked=blocked,
            failed_pre_effect=failed_pre_effect,
            recovery_required=recovery_required,
            planned=planned,
            skipped=skipped,
            reason_codes=dict(self._redlist_diagnostics["reason_codes"]),
            examples=list(self._redlist_diagnostics["examples"]),
        )
        self._state.record_event(
            self._run_id,
            "info",
            "redlist",
            "Redlist evaluada antes del procesamiento de contenido",
            {
                "schema": REDLIST_POLICY_SCHEMA,
                "policy_digest": policy_digest,
                "matched": matched,
                "applied": applied,
                "failed": failed,
                "protected": protected,
                "blocked": blocked,
                "failed_pre_effect": failed_pre_effect,
                "recovery_required": recovery_required,
                "planned": planned,
                "skipped": skipped,
                "reason_codes": dict(self._redlist_diagnostics["reason_codes"]),
                "examples": list(self._redlist_diagnostics["examples"]),
            },
        )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "redlist",
                "Redlist evaluada" if not self._apply else "Redlist aplicada",
                matched,
                matched,
                "archivos",
                True,
                (
                    ProgressMetric("applied", applied),
                    ProgressMetric("errors", failed_pre_effect + recovery_required),
                    ProgressMetric("blocked", blocked),
                    ProgressMetric("protected", protected),
                    ProgressMetric("planned", planned),
                    ProgressMetric("skipped", skipped),
                ),
            ),
        )
        return {
            "schema": REDLIST_POLICY_SCHEMA,
            "policy_digest": policy_digest,
            "matched": matched,
            "applied": applied,
            "failed": failed,
            "protected": protected,
            "blocked": blocked,
            "failed_pre_effect": failed_pre_effect,
            "recovery_required": recovery_required,
            "planned": planned,
            "skipped": skipped,
            "reason_codes": dict(self._redlist_diagnostics["reason_codes"]),
            "examples": list(self._redlist_diagnostics["examples"]),
        }

__all__ = ["RedlistActionsMixin"]
