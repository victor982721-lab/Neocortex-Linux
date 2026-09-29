"""Bounded ArtifactPolicy prepass owned by Framework actions.

The stage is deliberately an adapter around the existing effect owner.  It
does not implement a second Trash path, does not hash sources, and does not
start SQLite workers.  Callers may provide the complete inventory, one page,
or a mutation delta; ``None`` consumes the current scan in bounded pages.
"""

from __future__ import annotations

# mypy: disable-error-code=attr-defined

import json
from collections.abc import Iterable, Iterator, Mapping
from itertools import islice
from typing import TYPE_CHECKING

from neocortex.deduplication import FileSnapshot
from neocortex.persistence.framework_state_writer import RunBudgetExceeded
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.workflow.actions.action_contracts import TRASH_BATCH_SIZE
from neocortex.workflow.actions.artifact_policy import (
    ARTIFACT_POLICY_SCHEMA,
    ArtifactDecision,
    ArtifactPolicy,
)

if TYPE_CHECKING:
    from neocortex.platform.content_types import DetectedType


_PREVIEW_LIMIT = 64
_REASON_CODE_LIMIT = 64


def _counter(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


class ArtifactPrepassError(RuntimeError):
    """An artifact Trash batch crossed an uncertain recovery frontier."""

    def __init__(self, *, matched: int, applied: int, failed: int, protected: int, recovery_required: int) -> None:
        self.matched = matched
        self.applied = applied
        self.failed = failed
        self.protected = protected
        self.recovery_required = recovery_required
        super().__init__(
            "artifact prepass incomplete: "
            f"matched={matched} applied={applied} failed={failed} "
            f"protected={protected} recovery_required={recovery_required}"
        )


class ArtifactStageMixin:
    """Run deterministic artifact decisions before Dedupe/routes."""

    if TYPE_CHECKING:
        _artifact_excluded_paths: set[str]
        _artifact_excluded_identities: set[tuple[int, int]]
        _artifact_policy: ArtifactPolicy

    def _artifact_policy_instance(self) -> ArtifactPolicy:
        policy = getattr(self, "_artifact_policy", None)
        if isinstance(policy, ArtifactPolicy):
            return policy
        policy = ArtifactPolicy(cancellation_check=getattr(self, "_checkpoint", None))
        self._artifact_policy = policy
        return policy

    def _artifact_iter_snapshots(
        self,
        snapshots: Iterable[FileSnapshot] | FileSnapshot | None,
    ) -> Iterator[FileSnapshot]:
        if isinstance(snapshots, FileSnapshot):
            yield snapshots
            return
        if snapshots is not None:
            # Snapshot deltas are intentionally one-level iterables.  A page
            # tuple is accepted directly; a nested page is flattened only one
            # level so malformed recursive iterables cannot hang the stage.
            for item in snapshots:
                if isinstance(item, FileSnapshot):
                    yield item
                    continue
                if isinstance(item, (tuple, list)):
                    for nested in item:
                        if isinstance(nested, FileSnapshot):
                            yield nested
            return

        index = self._index
        page_reader = getattr(self, "_action_snapshots_page", None)
        after_path = ""
        while True:
            if callable(page_reader):
                page = page_reader(after_path=after_path, limit=TRASH_BATCH_SIZE)
            else:
                page = index.snapshots_page(
                    self._scan_id,
                    after_path=after_path,
                    limit=TRASH_BATCH_SIZE,
                )
            if not page:
                return
            yield from page
            after_path = page[-1].path

    def _artifact_detected_type(self, snapshot: FileSnapshot) -> DetectedType | None:
        identified = getattr(self, "_identified_types", None)
        if not isinstance(identified, Mapping):
            return None
        key_builder = getattr(self, "_detection_key", None)
        if callable(key_builder):
            try:
                value = identified.get(key_builder(snapshot))
            except (TypeError, ValueError):
                value = None
            return value
        # A direct action fixture may expose the physical identity key without
        # the FrameworkActions helper.  This fallback remains metadata-only.
        key = (
            int(snapshot.volume_id),
            int(snapshot.file_id),
            int(snapshot.size),
            int(snapshot.mtime_ns),
            int(snapshot.birthtime_ns),
        )
        value = identified.get(key)
        return value

    def _artifact_context(
        self,
        snapshot: FileSnapshot,
        detected: DetectedType | None,
        *,
        preidentify: bool,
    ) -> dict[str, object]:
        context: dict[str, object] = {"preidentify": preidentify}
        return context

    def _mark_artifact_excluded(self, snapshot: FileSnapshot) -> None:
        paths = getattr(self, "_artifact_excluded_paths", None)
        if not isinstance(paths, set):
            paths = set()
            self._artifact_excluded_paths = paths
        paths.add(str(snapshot.path))
        identities = getattr(self, "_artifact_excluded_identities", None)
        if not isinstance(identities, set):
            identities = set()
            self._artifact_excluded_identities = identities
        identities.add((int(snapshot.volume_id), int(snapshot.file_id)))

    def _artifact_is_excluded(self, value: str | FileSnapshot) -> bool:
        paths = getattr(self, "_artifact_excluded_paths", set())
        if isinstance(value, FileSnapshot):
            identities = getattr(self, "_artifact_excluded_identities", set())
            return (
                str(value.path) in paths
                or (int(value.volume_id), int(value.file_id)) in identities
            )
        return str(value) in paths

    def _publish_artifact_stage(
        self,
        *,
        status: str,
        phase: str,
        policy_digest: str,
        details: Mapping[str, object],
        error: BaseException | None = None,
    ) -> None:
        round_value = getattr(self, "_curation_round", None)
        base_stage = "preclean" if phase == "preidentify" else "artifact_policy"
        stage_name = (
            f"{base_stage}-delta-{round_value}"
            if type(round_value) is int and round_value > 1
            else base_stage
        )
        payload = {
            "schema": ARTIFACT_POLICY_SCHEMA,
            "policy_digest": policy_digest,
            "phase": phase,
            "stage_name": stage_name,
            "curation_round": round_value,
            **dict(details),
        }
        if error is not None:
            payload.update(
                {
                    "error_type": type(error).__name__,
                    "error": str(error)[:8192],
                }
            )
        state = getattr(self, "_state", None)
        publish = getattr(state, "publish_run_stage", None)
        read_manifest = getattr(state, "read_run_manifest", None)
        if callable(publish) and callable(read_manifest):
            try:
                manifest = read_manifest(self._run_id)
            except (OSError, RuntimeError, ValueError):
                manifest = None
            if manifest is not None:
                publish(
                    self._run_id,
                    stage_name,
                    status,
                    details=payload,
                    idempotency_key=(
                        f"artifact-policy:{policy_digest}:{stage_name}:{status}"
                    ),
                )
                return
        record_event = getattr(state, "record_event", None)
        if callable(record_event):
            record_event(
                self._run_id,
                "error" if status == "failed" else "warning" if status != "completed" else "info",
                stage_name,
                f"ArtifactPolicy {phase} {status}",
                payload,
            )

    @staticmethod
    def _artifact_metadata(snapshot: FileSnapshot) -> dict[str, object]:
        return {
            "volume_id": int(snapshot.volume_id),
            "file_id": int(snapshot.file_id),
            "size": int(snapshot.size),
            "mtime_ns": int(snapshot.mtime_ns),
            "birthtime_ns": int(snapshot.birthtime_ns),
        }

    def _artifact_recovery_count(self, paths: Iterable[str]) -> int:
        """Read only this batch's action statuses after the effect owner returns."""

        state = getattr(self, "_state", None)
        connection = getattr(state, "_connection", None)
        if connection is None:
            return 0
        selected = tuple(dict.fromkeys(str(path) for path in paths))
        if not selected:
            return 0
        placeholders = ",".join("?" for _ in selected)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM file_actions "
                "WHERE run_id=? AND action_type='trash_artifact' "
                f"AND status='recovery_required' AND source_path IN ({placeholders})",
                (self._run_id, *selected),
            ).fetchone()
        except Exception:
            # A post-frontier status read that cannot complete is itself an
            # unresolved recovery boundary.  Do not turn that uncertainty into
            # a successful/clean stage, and never retry the physical effect.
            return 1 if bool(getattr(self, "_apply", False)) else 0
        return int(row[0]) if row and type(row[0]) is int and row[0] >= 0 else 0

    def run_artifact_prepass(
        self,
        snapshots: Iterable[FileSnapshot] | FileSnapshot | None = None,
        *,
        preidentify: bool = False,
    ) -> dict[str, object]:
        """Evaluate and, when authorized, Trash deterministic artifacts.

        ``preidentify=True`` is the optional conservative early phase.  The
        policy itself then accepts only direct package-metadata relation proofs
        and will not use a suffix or a weak content hint.  Generated-file and
        content proofs are deferred to the post-Identify pass.  The default
        phase runs after Identify/Normalize and can use bounded strong magic or
        structural probes.
        """

        policy = self._artifact_policy_instance()
        phase = "preidentify" if preidentify else "postidentify"
        policy_digest = policy.policy_digest
        self._artifact_policy_active = True
        # A new pre-identify wave starts a fresh admission delta.  Its
        # exclusions must not leak by path into a later wave where an inode
        # may have been replaced.  The post-Identify pass in the same wave
        # deliberately preserves this set.
        if preidentify:
            existing_paths = getattr(self, "_artifact_excluded_paths", None)
            if isinstance(existing_paths, set):
                existing_paths.clear()
            existing_identities = getattr(self, "_artifact_excluded_identities", None)
            if isinstance(existing_identities, set):
                existing_identities.clear()
        if not isinstance(getattr(self, "_artifact_excluded_paths", None), set):
            self._artifact_excluded_paths = set()
        if not isinstance(getattr(self, "_artifact_excluded_identities", None), set):
            self._artifact_excluded_identities = set()

        details: dict[str, object] = {
            "phase": phase,
            "matched": 0,
            "trash_candidates": 0,
            "applied": 0,
            "failed": 0,
            "protected": 0,
            "blocked": 0,
            "kept": 0,
            "planned": 0,
            "skipped": 0,
            "recovery_required": 0,
            "excluded": 0,
            "seen": 0,
            "deduplicated_snapshots": 0,
            "reason_codes": {},
            "previews": [],
        }
        self._publish_artifact_stage(
            status="running",
            phase=phase,
            policy_digest=policy_digest,
            details=details,
        )
        pending: list[tuple[FileSnapshot, ArtifactDecision]] = []

        def add_reason(reason: str) -> None:
            reason_codes = details["reason_codes"]
            if not isinstance(reason_codes, dict):
                reason_codes = {}
                details["reason_codes"] = reason_codes
            if reason not in reason_codes and len(reason_codes) >= _REASON_CODE_LIMIT:
                return
            reason_codes[reason] = _counter(reason_codes.get(reason, 0)) + 1

        def reserve_page(page: Iterable[FileSnapshot]) -> None:
            selected = tuple(page)
            if not selected:
                return
            reserve = getattr(self, "_reserve_snapshot_work", None)
            if callable(reserve):
                probe_bound = sum(
                    2 * min(max(0, int(snapshot.size)), policy.max_probe_bytes)
                    for snapshot in selected
                )
                reserve("artifact-policy-page", selected, bytes_override=probe_bound)

        def flush_pending() -> None:
            if not pending:
                return
            batch = tuple(
                (
                    snapshot.path,
                    json.dumps(
                        {
                            "schema": ARTIFACT_POLICY_SCHEMA,
                            "policy_digest": policy_digest,
                            "phase": phase,
                            "rule_id": decision.rule_id,
                            "decision": decision.disposition,
                            "evidence": dict(decision.evidence),
                            "preview": dict(decision.preview),
                            "content_proof": (
                                decision.content_proof.as_dict()
                                if decision.content_proof is not None
                                else None
                            ),
                            "snapshot": self._artifact_metadata(snapshot),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for snapshot, decision in pending
            )
            expected = tuple(snapshot for snapshot, _decision in pending)
            expected_proofs = tuple(
                decision.content_proof for _snapshot, decision in pending
            )
            batch_paths = tuple(snapshot.path for snapshot, _decision in pending)
            apply_batch = self._apply_trash_batch
            applied, failed, protected = apply_batch(
                "trash_artifact",
                batch,
                expected_snapshots=expected,
                expected_content_proofs=expected_proofs,
                defer_reconciliation=True,
            )
            details["applied"] = _counter(details["applied"]) + int(applied)
            details["failed"] = _counter(details["failed"]) + int(failed)
            details["protected"] = _counter(details["protected"]) + int(protected)
            details["skipped"] = _counter(details["skipped"]) + int(protected)
            details["recovery_required"] = _counter(details["recovery_required"]) + self._artifact_recovery_count(
                batch_paths
            )
            pending.clear()

        try:
            snapshot_iterator = iter(self._artifact_iter_snapshots(snapshots))
            while True:
                # Reserve the bounded metadata page before any policy probe or
                # effect.  Inventory owns uniqueness; this stage does not
                # construct an unbounded set of snapshot keys.
                page = tuple(islice(snapshot_iterator, TRASH_BATCH_SIZE))
                if not page:
                    break
                candidate_page = tuple(
                    snapshot
                    for snapshot in page
                    if policy.may_probe(
                        snapshot,
                        self._artifact_detected_type(snapshot),
                        context=self._artifact_context(
                            snapshot,
                            self._artifact_detected_type(snapshot),
                            preidentify=preidentify,
                        ),
                    )
                )
                reserve_page(candidate_page)
                for snapshot in page:
                    checkpoint = getattr(self, "_checkpoint", None)
                    if callable(checkpoint):
                        checkpoint()
                    details["seen"] = _counter(details["seen"]) + 1
                    detected = self._artifact_detected_type(snapshot)
                    context = self._artifact_context(
                        snapshot,
                        detected,
                        preidentify=preidentify,
                    )
                    decision = policy.evaluate(
                        snapshot,
                        detected,
                        metadata=self._artifact_metadata(snapshot),
                        context=context,
                    )
                    previews = details["previews"]
                    if isinstance(previews, list) and len(previews) < _PREVIEW_LIMIT:
                        previews.append(decision.as_dict())
                    if decision.disposition == "trash":
                        details["matched"] = _counter(details["matched"]) + 1
                        details["trash_candidates"] = _counter(details["trash_candidates"]) + 1
                        details["excluded"] = _counter(details["excluded"]) + 1
                        self._mark_artifact_excluded(snapshot)
                        pending.append((snapshot, decision))
                        add_reason(decision.rule_id)
                        if len(pending) >= TRASH_BATCH_SIZE:
                            flush_pending()
                    elif decision.disposition == "block":
                        details["blocked"] = _counter(details["blocked"]) + 1
                        details["excluded"] = _counter(details["excluded"]) + 1
                        self._mark_artifact_excluded(snapshot)
                        add_reason(decision.rule_id)
                    else:
                        details["kept"] = _counter(details["kept"]) + 1
                flush_pending()
            flush_pending()
            flush_reconciliation = getattr(self, "_flush_deferred_reconciliation", None)
            if callable(flush_reconciliation) and _counter(details["applied"]):
                flush_reconciliation()
            if not bool(getattr(self, "_apply", False)):
                details["planned"] = max(
                    0,
                    _counter(details["matched"])
                    - _counter(details["protected"])
                    - _counter(details["failed"]),
                )
            status = (
                "partial"
                if any(
                    _counter(details[name])
                    for name in ("failed", "protected", "blocked", "recovery_required")
                )
                else "completed"
            )
            if bool(getattr(self, "_apply", False)) and _counter(details["recovery_required"]):
                raise ArtifactPrepassError(
                    matched=_counter(details["matched"]),
                    applied=_counter(details["applied"]),
                    failed=_counter(details["failed"]),
                    protected=_counter(details["protected"]),
                    recovery_required=_counter(details["recovery_required"]),
                )
            self._publish_artifact_stage(
                status=status,
                phase=phase,
                policy_digest=policy_digest,
                details=details,
            )
            return {
                "schema": ARTIFACT_POLICY_SCHEMA,
                "policy_digest": policy_digest,
                **details,
                "candidates": _counter(details["matched"]),
                "trashed": _counter(details["applied"]),
                "preview": tuple(details["previews"])
                if isinstance(details["previews"], list)
                else (),
                "excluded_path_count": len(getattr(self, "_artifact_excluded_paths", set())),
                "excluded_paths": tuple(
                    sorted(getattr(self, "_artifact_excluded_paths", set()))[:_PREVIEW_LIMIT]
                ),
                "excluded_identity_count": len(
                    getattr(self, "_artifact_excluded_identities", set())
                ),
                "excluded_identities": tuple(
                    sorted(getattr(self, "_artifact_excluded_identities", set()))[:_PREVIEW_LIMIT]
                ),
                "status": status,
            }
        except (CancellationRequested, RunBudgetExceeded, KeyboardInterrupt) as exc:
            self._publish_artifact_stage(
                status="interrupted",
                phase=phase,
                policy_digest=policy_digest,
                details=details,
                error=exc,
            )
            raise
        except BaseException as exc:
            self._publish_artifact_stage(
                status="failed",
                phase=phase,
                policy_digest=policy_digest,
                details=details,
                error=exc,
            )
            raise


__all__ = ["ArtifactPrepassError", "ArtifactStageMixin"]
