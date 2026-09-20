"""Cohesive owner mixin extracted from the Framework facade."""

from __future__ import annotations

# mypy: disable-error-code=attr-defined
# The mixin is composed into FrameworkActions at runtime; its two boolean
# lifecycle flags are owned by that facade and are intentionally not duplicated
# as a second runtime base.
# mypy: disable-error-code=has-type

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from neocortex.deduplication import DedupPlan, FileChangedError, FileSnapshot, snapshot_path
from neocortex.platform.content_types import DetectedType
from neocortex.progress import ProgressEvent, emit_progress
from neocortex.runtime.models import ActionSummary
from neocortex.safety.corpus_access import CorpusMutationGuard, ProtectedAnalysisRootError
from neocortex.safety.internal_paths import InternalPathProtectionError
from neocortex.safety.protected_content import ProtectedContentError
from neocortex.workflow.actions.action_contracts import (
    CONTENT_PREFIX_BYTES,
    IDENTIFY_PROGRESS_INTERVAL_NS,
    IDENTIFY_PROGRESS_ITEM_STEP,
    RedlistPrepassError,
    TRASH_BATCH_SIZE,
)
from neocortex.workflow.actions.action_policy import (
    corrected_path as _corrected_path,
    path_key as _path_key,
    protected_path_reason as _protected_path_reason,
    same_snapshot as _same_snapshot,
)
from neocortex.workflow.actions.file_action_recovery import expected_identity_json
from neocortex.workflow.actions.identify import ContentObservation, observe_content_type
from neocortex.workflow.mutations import ApplyCandidate, BackendOutcome
from neocortex.safety.kio_trash import metadata_binding
from neocortex.deduplication.inventory.index import validate_inventory_root

class IdentifyActionsMixin:
    """Implementation for one FrameworkState/FrameworkActions responsibility."""

    if TYPE_CHECKING:
        _max_file_bytes: int | None

        def _size_is_admitted(self, snapshot: FileSnapshot) -> bool: ...

        def _record_size_skip(
            self, summary: ActionSummary, snapshot: FileSnapshot
        ) -> ActionSummary: ...

        def _with_size_limit(self, summary: ActionSummary) -> ActionSummary: ...

    def _content_type_total(self) -> int:
        """Count non-empty snapshots admitted by this run's size ceiling.

        The inventory remains the complete physical universe.  This metadata
        pass only establishes the denominator for Identify progress; it does
        not stat, open, hash, or inspect any source payload.
        """

        if self._max_file_bytes is None:
            return self._index.file_count(self._scan_id) - self._index.file_count_by_size(
                self._scan_id, 0
            )
        total = 0
        after_path = ""
        while True:
            page = self._index.snapshots_page(
                self._scan_id,
                after_path=after_path,
                limit=TRASH_BATCH_SIZE,
            )
            if not page:
                return total
            after_path = page[-1].path
            for snapshot in page:
                if snapshot.size > 0 and self._size_is_admitted(snapshot):
                    total += 1

    def identify_and_normalize(self) -> ActionSummary:
        """Run bounded Identify/Normalize before any duplicate planning.

        The phase intentionally does not publish route candidates.  It only
        reads bounded detector input, records the detector cache, and applies
        identity-bound extension corrections when ``--apply`` is enabled.
        Policy/redlist and Dedupe are subsequent phases in the orchestrator.
        """

        self._identified_types.clear()
        self._identified_detector_version = self._detector_version()
        self._identify_summary = None
        self._validate_apply_root()
        previous_suppress = self._redlist_suppress_late_mutation
        previous_no_hash = self._normalize_without_full_hash
        self._redlist_suppress_late_mutation = True
        self._normalize_without_full_hash = True
        try:
            summary = self._validate_extensions(
                None,
                self._with_size_limit(ActionSummary(apply_actions=self._apply)),
                publish_routes=False,
                prune_cache=False,
            )
        finally:
            self._redlist_suppress_late_mutation = previous_suppress
            self._normalize_without_full_hash = previous_no_hash
        self._identify_summary = summary
        return summary

    def _validate_extensions(
        self,
        plan: DedupPlan | None,
        summary: ActionSummary,
        *,
        publish_routes: bool = True,
        prune_cache: bool = True,
        reuse_identified: bool = False,
    ) -> ActionSummary:
        # Keep direct phase callers safe as well as the normal ``execute``
        # route, whose duplicate phase normally flushes this queue first.
        self._flush_deferred_reconciliation()
        # The inventory is the physical source of truth for this pass.  A
        # dry-run only records proposed actions; it does not remove any
        # inventory member from the route input set.  In particular, a
        # planned duplicate is still a real file and must retain its identity
        # and content-type coverage until an effect is actually observed.
        # Applied runs may read the same immutable inventory snapshot because
        # the source check below rejects sources that were really removed
        # (or changed) before route publication.
        # Empty files have their own explicit, planned ``trash_empty_file``
        # record and are not content-route inputs.  Keep them out of this
        # content-type denominator, but never subtract proposed duplicate
        # files: unlike an observed effect, a dry-run proposal leaves those
        # physical sources available for extraction.
        total = self._content_type_total()
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
        processed = 0
        route_candidates: list[tuple[str, FileSnapshot]] = []
        cache_updates: list[tuple[FileSnapshot, DetectedType | None]] = []
        last_report_completed = 0
        last_report_ns = time.monotonic_ns()

        def flush_route_candidates() -> None:
            if publish_routes and route_candidates:
                self._state.store_route_candidates(self._run_id, route_candidates)
                route_candidates.clear()

        def flush_cache_updates() -> None:
            if cache_updates:
                self._state.store_content_type_cache_batch(
                    cache_updates, self._detector_version(), self._run_id
                )
                cache_updates.clear()

        def report_progress(*, force: bool = False) -> None:
            nonlocal last_report_completed, last_report_ns
            now = time.monotonic_ns()
            if not force and (
                processed - last_report_completed < IDENTIFY_PROGRESS_ITEM_STEP
                and now - last_report_ns < IDENTIFY_PROGRESS_INTERVAL_NS
            ):
                return
            last_report_completed = processed
            last_report_ns = now
            emit_progress(
                self._progress,
                ProgressEvent(
                    "framework",
                    "content-types",
                    "Validando tipos de contenido",
                    processed,
                    total,
                    "archivos",
                ),
            )

        def classify(
            planned: FileSnapshot,
            detected: DetectedType | None,
        ) -> None:
            nonlocal summary
            summary, route_candidate = self._classify_detected_content_type(
                planned,
                detected,
                summary,
            )
            if publish_routes and route_candidate is not None:
                route_candidates.append(route_candidate)
                if len(route_candidates) >= 1000:
                    flush_route_candidates()

        # Read bounded pages through the already-open inventory owner.  The
        # page tuple closes its SQLite cursor before this loop can persist
        # route/cache state or apply an extension rename; reopening the same
        # WAL-backed inventory here can exhaust the temporary snapshot budget.
        capacity_cache = [0, 0]
        observation_samples: list[int] = []
        observation_samples_lock = threading.Lock()
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
            if self._reserve_work is not None and not reuse_identified:
                admitted_prefix_bytes = 0
                for snapshot in page:
                    if self._size_is_admitted(snapshot):
                        admitted_prefix_bytes += min(
                            CONTENT_PREFIX_BYTES, max(0, int(snapshot.size))
                        )
                self._reserve_snapshot_work(
                    "content-prefix",
                    page,
                    items=0,
                    bytes_override=admitted_prefix_bytes,
                )
            if reuse_identified:
                # The integrated route pass normally consumes the in-memory
                # Identify decisions.  Keep its changed/new-file fallback
                # serial and owner-bound; only the canonical Identify pass
                # below dispatches cache misses to workers.
                for planned in page:
                    self._checkpoint()
                    if planned.size == 0:
                        continue
                    summary, route_candidate, cache_update = self._inspect_content_type_candidate(
                        planned,
                        summary,
                        reuse_identified=True,
                    )
                    if cache_update is not None:
                        cache_updates.append(cache_update)
                        if len(cache_updates) >= 1000:
                            flush_cache_updates()
                    if publish_routes and route_candidate is not None:
                        route_candidates.append(route_candidate)
                        if len(route_candidates) >= 1000:
                            flush_route_candidates()
                    if self._size_is_admitted(planned):
                        processed += 1
                        report_progress()
                continue

            admitted: list[FileSnapshot] = []
            for planned in page:
                self._checkpoint()
                if planned.size == 0:
                    continue
                summary, is_admitted = self._validate_content_type_candidate(
                    planned,
                    summary,
                )
                if is_admitted:
                    admitted.append(planned)
                elif self._size_is_admitted(planned):
                    processed += 1
                    report_progress()
            if not admitted:
                continue

            cache_lookup = self._state.get_content_type_cache_batch(
                admitted,
                self._detector_version(),
            )
            pending: list[tuple[int, FileSnapshot]] = []
            observations: list[ContentObservation | None] = [None] * len(admitted)
            for index, planned in enumerate(admitted):
                cached = cache_lookup.get(self._detection_key(planned))
                if cached is not None and cached[0]:
                    detected = cached[1]
                    self._remember_identified_type(planned, detected)
                    summary = replace(
                        summary,
                        type_cache_hits=summary.type_cache_hits + 1,
                    )
                    observations[index] = ContentObservation(planned, detected=detected)
                else:
                    summary = replace(
                        summary,
                        type_cache_misses=summary.type_cache_misses + 1,
                    )
                    pending.append((index, planned))

            if pending:
                # The shared coordinator is optional for direct action
                # callers, but the integrated runner binds one resource scope
                # for the complete run.  Registering this short-lived phase
                # keeps worker capacity elastic without adding a second
                # coordinator or a fixed worker ceiling.
                from concurrent.futures import ThreadPoolExecutor
                from neocortex.runtime.control.global_resources import (
                    current_resource_coordinator,
                    resource_grant_scope,
                    resource_gate,
                )
                from neocortex.runtime.control.cpu_runtime import effective_cpu_count

                coordinator = current_resource_coordinator()
                gate = (
                    None
                    if coordinator is None
                    else resource_gate("actions.identify", coordinator)
                )
                cancellation = None if coordinator is None else coordinator.cancellation

                # Direct FrameworkActions callers do not bind a coordinator.
                # Keep their capacity probe adaptive, but cache the cgroup /
                # affinity read briefly: probing those files once per result
                # dominates small detector workloads and defeats the bounded
                # worker pipeline.  A bound coordinator owns its own live
                # capacity probe and is intentionally left untouched.
                def direct_capacity(
                    cache: list[int] = capacity_cache,
                    samples: list[int] = observation_samples,
                    samples_lock: threading.Lock = observation_samples_lock,
                ) -> int:
                    now = time.monotonic_ns()
                    if now - cache[1] >= IDENTIFY_PROGRESS_INTERVAL_NS:
                        observed = effective_cpu_count()
                        # Content detection is predominantly Python work
                        # (the detector profile is GIL-bound), so using every
                        # logical CPU only adds thread stacks and context
                        # switches.  Derive a conservative width from live
                        # capacity instead of a fixed worker count.  The
                        # bounded batch below keeps this width memory-safe.
                        with samples_lock:
                            recent = tuple(samples[-32:])
                        mean_ns = (
                            sum(recent) / len(recent)
                            if len(recent) >= 8
                            else 0.0
                        )
                        # Only widen for clearly I/O-bound observations; a
                        # few-millisecond sample can be scheduler/GIL noise
                        # and must not cause a memory-heavy worker surge.
                        if len(recent) < 8:
                            # Keep a short pilot wide enough to expose a
                            # genuinely blocking detector before narrowing a
                            # CPU-bound workload.  A cold page has no timing
                            # evidence yet, so the initial live-capacity
                            # divisor remains the conservative value used by
                            # the historical bounded pipeline.
                            divisor = 4
                        else:
                            # The content detector is Python/GIL-bound for
                            # small headers.  Once the pilot has enough
                            # observations, one worker avoids context-switch
                            # and thread hand-off overhead.  Slow detectors
                            # (for example a blocked filesystem) still widen
                            # to half of the live capacity.
                            divisor = 2 if mean_ns >= 8_000_000 else 16
                        cache[0] = max(1, min(observed, observed // divisor or 1))
                        cache[1] = now
                    return max(1, cache[0])

                def observe(
                    planned: FileSnapshot,
                    samples: list[int] = observation_samples,
                    samples_lock: threading.Lock = observation_samples_lock,
                ) -> ContentObservation:
                    # Resolve the module global at invocation time so focused
                    # detector tests and diagnostics retain their seam.
                    started_ns = time.monotonic_ns()
                    try:
                        return observe_content_type(
                            planned,
                            self._detector_function(),
                            snapshotter=snapshot_path,
                            prevalidated=True,
                        )
                    finally:
                        with samples_lock:
                            samples.append(time.monotonic_ns() - started_ns)
                            del samples[:-64]

                # Submit bounded batches rather than one future per file.  A
                # page is already limited to TRASH_BATCH_SIZE; grouping the
                # pure observations keeps the in-flight window bounded while
                # avoiding thousands of executor/future hand-offs for small
                # headers.  Results retain the inventory order explicitly.
                observation_batches = tuple(
                    tuple(pending[offset : offset + 32])
                    for offset in range(0, len(pending), 32)
                )

                def observe_batch(
                    batch: tuple[tuple[int, FileSnapshot], ...],
                    resource_gate_value=gate,
                    cancel_token=cancellation,
                ) -> tuple[tuple[int, ContentObservation], ...]:
                    device = str(batch[0][1].volume_id) if batch else None
                    if resource_gate_value is None:
                        result = tuple((index, observe(planned)) for index, planned in batch)
                    else:
                        with resource_gate_value.admit(
                            CONTENT_PREFIX_BYTES * 2 * 32,
                            native_threads=1,
                            io_slots=1,
                            io_device=device,
                            phase="actions.identify",
                            cancellation=cancel_token,
                        ) as grant:
                            with resource_grant_scope(grant):
                                result = tuple(
                                    (index, observe(planned)) for index, planned in batch
                                )
                    return result

                if coordinator is None:
                    worker_count = direct_capacity()
                else:
                    assert gate is not None
                    worker_count = gate.worker_capacity(
                        max_workers=None,
                        estimated_bytes=CONTENT_PREFIX_BYTES * 2 * 32,
                        native_threads=1,
                    )
                with ThreadPoolExecutor(
                    max_workers=max(1, int(worker_count)),
                    thread_name_prefix="neocortex-identify",
                ) as executor:
                    futures = [executor.submit(observe_batch, batch) for batch in observation_batches]
                    for future in futures:
                        for index, observation in future.result():
                            observations[index] = observation

            for index, planned in enumerate(admitted):
                self._checkpoint()
                observed = observations[index]
                if observed is None:
                    # This is an internal invariant failure rather than a
                    # detector error: do not publish a partial route.
                    raise RuntimeError("Identify worker lost a content observation")
                observation = observed
                if observation.stale:
                    summary = replace(
                        summary,
                        stale_inventory=summary.stale_inventory + 1,
                    )
                elif observation.error is not None:
                    self._record_content_type_error(planned, observation.error)
                    summary = replace(summary, errors=summary.errors + 1)
                else:
                    detected = observation.detected
                    self._remember_identified_type(planned, detected)
                    cache_updates.append((planned, detected))
                    if len(cache_updates) >= 1000:
                        flush_cache_updates()
                    classify(planned, detected)
                processed += 1
                report_progress()
        flush_route_candidates()
        flush_cache_updates()
        self._flush_deferred_reconciliation()
        if prune_cache:
            summary = replace(
                summary,
                type_cache_pruned=self._state.prune_content_type_cache(
                    self._run_id, self._detector_version()
                ),
            )
        emit_progress(
            self._progress,
            ProgressEvent(
                "framework",
                "content-types",
                "Validación de tipos completada",
                processed,
                total,
                "archivos",
                True,
            ),
        )
        return summary

    def _flush_deferred_reconciliation(self) -> None:
        """Publish deferred physical removals after the owning phase completes."""

        if not self._deferred_reconciliation_paths and not self._deferred_reconciliation_upserts:
            return
        paths = tuple(self._deferred_reconciliation_paths)
        upserts = tuple(self._deferred_reconciliation_upserts)
        self._index.apply_reconciliation(
            self._scan_id,
            upserts=upserts,
            remove_paths=paths,
        )
        # Clear only after the owner has acknowledged the successor.  If the
        # reconciliation raises, the paths remain available to an explicit
        # retry by the caller rather than being silently discarded.
        self._deferred_reconciliation_paths.clear()
        self._deferred_reconciliation_upserts.clear()

    def _inspect_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
        *,
        reuse_identified: bool = False,
    ) -> tuple[
        ActionSummary,
        tuple[str, FileSnapshot] | None,
        tuple[FileSnapshot, DetectedType | None] | None,
    ]:
        summary, admitted = self._validate_content_type_candidate(
            planned,
            summary,
            count_files_checked=not reuse_identified,
            record_size_skip=not reuse_identified,
        )
        if not admitted:
            return summary, None, None
        if reuse_identified:
            key = self._detection_key(planned)
            from_map = (
                self._identified_detector_version == self._detector_version()
                and key in self._identified_types
            )
            if from_map:
                detected = self._identified_types[key]
                usable = True
                cache_update = None
            else:
                # A changed or newly-added file is outside the original
                # Identify snapshot.  Re-identify only that identity; the
                # unchanged population remains a zero-detector route pass.
                summary, detected, usable = self._detect_planned_content_type(
                    planned,
                    summary,
                )
                cache_update = (planned, detected) if usable else None
                if usable:
                    summary = replace(
                        summary,
                        files_checked=summary.files_checked + 1,
                    )
            if not usable:
                return summary, None, cache_update
            summary, route_candidate = self._classify_detected_content_type(
                planned,
                detected,
                summary,
                normalize=False,
                count_metrics=not from_map,
            )
            return summary, route_candidate, cache_update
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

    def _validate_content_type_candidate(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
        *,
        count_files_checked: bool = True,
        record_size_skip: bool = True,
    ) -> tuple[ActionSummary, bool]:
        # This must be the first branch: Inventory already captured the size,
        # so an oversize source cannot reach redlist evaluation, protected
        # checks, stat refresh, cache lookup, detector, or route publication.
        if not self._size_is_admitted(planned):
            if record_size_skip:
                summary = self._record_size_skip(summary, planned)
            return summary, False
        if self._redlist_is_excluded(planned.path):
            # A redlisted source that remains physically present because a
            # hard boundary or a pre-effect block refused Trash is still a
            # policy exclusion.  Never let it reach route candidates, even in
            # preview mode or when the integrated runner was reconstructed
            # after the prepass.
            return summary, False
        if _protected_path_reason(planned.path, check_attributes=True) is not None:
            return summary, False
        try:
            current = self._snapshot_path(planned.path)
        except FileNotFoundError:
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        except OSError as exc:
            self._record_content_type_error(planned, exc)
            return replace(
                summary,
                files_checked=(
                    summary.files_checked + 1
                    if count_files_checked
                    else summary.files_checked
                ),
                errors=summary.errors + 1,
            ), False
        if not _same_snapshot(planned, current):
            return replace(
                summary,
                stale_inventory=summary.stale_inventory + 1,
            ), False
        return (
            replace(
                summary,
                files_checked=(
                    summary.files_checked + 1
                    if count_files_checked
                    else summary.files_checked
                ),
            ),
            True,
        )

    def _detect_planned_content_type(
        self,
        planned: FileSnapshot,
        summary: ActionSummary,
    ) -> tuple[ActionSummary, DetectedType | None, bool]:
        cache_hit, detected = self._state.get_content_type_cache(
            planned,
            self._detector_version(),
        )
        if cache_hit:
            self._remember_identified_type(planned, detected)
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
            detected = self._detector_function()(planned.path)
            refreshed = self._snapshot_path(planned.path)
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
        self._remember_identified_type(planned, detected)
        return summary, detected, True

    def _classify_detected_content_type(
        self,
        planned: FileSnapshot,
        detected: DetectedType | None,
        summary: ActionSummary,
        *,
        normalize: bool = True,
        count_metrics: bool = True,
    ) -> tuple[ActionSummary, tuple[str, FileSnapshot] | None]:
        if detected is None:
            if count_metrics:
                summary = replace(summary, unknown_types=summary.unknown_types + 1)
            return summary, None
        if count_metrics:
            summary = replace(summary, types_detected=summary.types_detected + 1)
        if detected.accepts(planned.path):
            if count_metrics:
                summary = replace(
                    summary,
                    extensions_matching=summary.extensions_matching + 1,
                )
            return summary, (detected.mime, planned)
        target = _corrected_path(Path(planned.path), detected.canonical_extension)
        from neocortex.workflow.actions.redlist import redlist_match

        redlist_entry = redlist_match(target)
        if redlist_entry is not None and not self._redlist_suppress_late_mutation:
            summary = self._trash_detected_redlist(
                planned,
                detected,
                redlist_entry,
                summary,
            )
            return summary, None
        if not normalize:
            return summary, (detected.mime, planned)
        summary = self._rename_mismatch(planned, detected, summary)
        actual_path = (
            target if target.is_file() and not Path(planned.path).exists() else Path(planned.path)
        )
        return summary, (detected.mime, replace(planned, path=str(actual_path)))

    def _trash_detected_redlist(
        self,
        planned: FileSnapshot,
        detected: DetectedType,
        redlist_entry: str,
        summary: ActionSummary,
    ) -> ActionSummary:
        """Trash an extensionless/mismatched source whose proved type is redlisted."""

        from neocortex.workflow.actions.redlist import (
            REDLIST_POLICY_SCHEMA,
            redlist_policy_digest,
        )

        policy_digest = redlist_policy_digest()
        evidence = json.dumps(
            {
                "schema": REDLIST_POLICY_SCHEMA,
                "policy_digest": policy_digest,
                "redlist_entry": redlist_entry,
                "match": "detected_canonical_extension_v1",
                "original_suffix": Path(planned.path).suffix,
                "detected_extension": detected.canonical_extension,
                "detection_evidence": detected.evidence,
                "snapshot": {
                    "volume_id": planned.volume_id,
                    "file_id": planned.file_id,
                    "size": planned.size,
                    "mtime_ns": planned.mtime_ns,
                    "birthtime_ns": planned.birthtime_ns,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._begin_redlist_batch_diagnostics()
        applied, failed, protected = self._apply_trash_batch(
            "trash_redlist",
            ((planned.path, evidence),),
            expected_snapshots=(planned,),
            defer_reconciliation=True,
        )
        batch = self._consume_redlist_batch_diagnostics()
        self._merge_redlist_batch_diagnostics(batch)
        recovery_required = self._redlist_counter(batch, "recovery_required")
        self._redlist_excluded_paths.add(str(planned.path))
        if self._apply and recovery_required:
            raise RedlistPrepassError(
                matched=1,
                applied=applied,
                failed=failed,
                protected=protected,
                blocked=self._redlist_counter(batch, "blocked"),
                failed_pre_effect=self._redlist_counter(batch, "failed_pre_effect"),
                recovery_required=recovery_required,
                reason_codes=dict(self._redlist_diagnostics["reason_codes"]),
                examples=tuple(self._redlist_diagnostics["examples"]),
            )
        return summary

    def _record_content_type_error(
        self,
        planned: FileSnapshot,
        error: Exception,
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
        """Correct one detected extension through the canonical POSIX backend."""

        source = Path(planned.path)
        target = _corrected_path(source, detected.canonical_extension)
        # Keep the policy view deterministic even in dry-run mode.  The
        # physical source remains untouched until ``--apply`` but Redlist must
        # evaluate the normalized successor rather than the stale suffix.
        self._normalized_paths[str(source)] = str(target)
        summary = replace(summary, rename_candidates=summary.rename_candidates + 1)
        if self._rename_protected_reason(source, target) is not None:
            return replace(summary, rename_skips=summary.rename_skips + 1)
        action_id = self._begin_rename_action(source, target, detected)
        if not self._apply:
            self._state.finish_file_action(action_id, "planned")
            return summary
        try:
            mutation_root = self._validate_apply_root()
            if mutation_root is None:
                raise RuntimeError("apply mutation root is unavailable")
            self._validate_action_path(source, role="rename source")
            target_stat = self._validate_action_path(
                target,
                role="rename target",
                allow_missing_leaf=True,
            )
            if target_stat is not None:
                self._state.finish_file_action(action_id, "skipped", "destination_exists")
                return replace(summary, rename_skips=summary.rename_skips + 1)
        except (InternalPathProtectionError, ProtectedAnalysisRootError):
            raise
        except (OSError, RuntimeError) as exc:
            self._state.finish_file_action(action_id, "failed", str(exc))
            return replace(
                summary,
                rename_skips=summary.rename_skips + 1,
                errors=summary.errors + 1,
            )
        try:
            # Normalization is an identity-bound metadata operation.  The
            # source is already revalidated by the POSIX backend immediately
            # before ``renameat2``; reading the whole payload here would move
            # Identify/Normalize after the full-hash/Dedupe frontier.
            source_digest = metadata_binding(planned)
            effect = SimpleNamespace(
                action="rename",
                source=planned,
                source_digest=source_digest,
                keeper=None,
                keeper_digest=None,
                target_path=str(target),
            )
            candidate = ApplyCandidate(
                owner_id=f"framework:{self._run_id}",
                owner_digest="sha256:" + "0" * 64,
                root=mutation_root,
                effect=effect,
            )
            expected_json = expected_identity_json(
                planned,
                source_path=str(source),
                target_path=str(target),
            )

            def mark_frontier() -> None:
                self._state.mark_file_actions_applying(((action_id, expected_json),))

            outcome = self._rename_backend.apply(candidate, before_syscall=mark_frontier)
            if not isinstance(outcome, BackendOutcome):
                raise RuntimeError("rename backend returned an unsupported outcome")
            detail = outcome.detail or outcome.reason
            if outcome.status == "applied":
                if outcome.receipt_json is None:
                    raise RuntimeError("rename backend reported applied without a receipt")
                try:
                    self._state.confirm_file_actions_applied(
                        ((action_id, outcome.receipt_json),)
                    )
                except BaseException as exc:
                    self._best_effort_require_recovery((action_id,), str(exc), exc)
                    return replace(
                        summary,
                        rename_skips=summary.rename_skips + 1,
                        errors=summary.errors + 1,
                    )
                renamed = self._snapshot_path(target)
                self._deferred_reconciliation_upserts.append(renamed)
                self._deferred_reconciliation_paths.append(str(source))
                return replace(summary, files_renamed=summary.files_renamed + 1)
            if outcome.status == "recovery_required":
                self._state.require_file_action_recovery((action_id,), detail)
                return replace(
                    summary,
                    rename_skips=summary.rename_skips + 1,
                    errors=summary.errors + 1,
                )
            self._state.finish_file_action(action_id, "skipped", detail)
            return replace(
                summary,
                rename_skips=summary.rename_skips + 1,
                errors=summary.errors + int(outcome.reason != "destination_exists"),
            )
        except (InternalPathProtectionError, ProtectedAnalysisRootError):
            raise
        except (OSError, RuntimeError, FileChangedError, ValueError) as exc:
            self._state.finish_file_action(action_id, "failed", str(exc))
            return replace(
                summary,
                rename_skips=summary.rename_skips + 1,
                errors=summary.errors + 1,
            )
        except BaseException as exc:
            self._best_effort_require_recovery((action_id,), str(exc), exc)
            raise

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
        current = self._snapshot_path(current_root)
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

__all__ = ["IdentifyActionsMixin"]
