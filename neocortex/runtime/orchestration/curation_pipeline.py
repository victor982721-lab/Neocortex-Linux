"""Cheap curation fixed point, shared by all physical document producers.

Archive owns nested ZIP recursion. This loop is only over newly observed
physical deltas (including EML descendants), never over ZIPs or the stable
corpus. Routes and the SHA-256 planner are deliberately absent from this owner.
"""

from __future__ import annotations

import time
from dataclasses import replace

from neocortex.workflow.actions.redlist import redlist_policy_digest
from neocortex.platform.logical_filename import LogicalFilename


MAX_ADMISSION_WAVES = 64
_FAILURES = {"partial", "failed", "blocked", "recovery_required"}
_COUNTERS = (
    "parents_examined", "parents_with_attachments", "attachments_planned",
    "attachments_materialized", "attachments_replayed", "attachments_consumed", "failed",
)


def _sum_report(target: dict, source: dict, counters) -> None:
    for name in counters:
        value = source.get(name)
        if type(value) is int and value >= 0:
            target[name] = int(target.get(name, 0)) + value
    if source.get("status") in _FAILURES:
        target["status"] = "partial"


def stabilize_corpus_admission(
    owner, *, state, run_id, boundary, inventory, dedup_index, runner,
    admission, excluded_paths,
):
    """Return a stable physical generation, reusing one action owner.

    Initial ZIP intake already preceded this function. A delta produced by an
    EML reaches that same Archive owner *before* Identify. No list intended for
    UI presentation (created_paths/source_outcomes) grants admission authority.
    """
    email: dict[str, object] = {"status": "completed", **dict.fromkeys(_COUNTERS, 0)}
    nested: dict[str, object] = {"status": "completed"}
    report: dict[str, object] = {
        "status": "completed", "preclean_matched": 0, "preclean_trashed": 0,
        "redlist_matched": 0, "redlist_trashed": 0,
        "content_cleanup_matched": 0, "content_cleanup_trashed": 0,
        "blocked": 0, "recovery_required": 0,
    }
    started = time.perf_counter_ns()

    def refresh():
        nonlocal inventory
        scan = dedup_index.current_scan_id(inventory.scan.scan_id)
        if scan != inventory.scan.scan_id:
            inventory = replace(inventory, scan=dedup_index.scan_summary(scan))
        runner._scan_id = inventory.scan.scan_id
        admission.scan_id = inventory.scan.scan_id

    def record_cleanup(result, *, prefix):
        report[prefix + "_matched"] += int(result.get("matched", 0))
        report[prefix + "_trashed"] += int(result.get("applied", result.get("trashed", 0)))
        blocked = sum(int(result.get(name, 0)) for name in ("blocked", "protected", "failed_pre_effect"))
        report["blocked"] += blocked
        report["recovery_required"] += int(result.get("recovery_required", 0))
        if blocked or result.get("status") in _FAILURES or result.get("failed"):
            report["status"] = "partial"

    def reset_delta_path_hints():
        # Path spelling is not an identity. If a producer reuses a previously
        # removed path, an older logical rename/exclusion cannot decide for the
        # new observation. Stable observations retain their preview hints.
        for snapshot in admission.snapshots():
            runner._normalized_paths.pop(snapshot.path, None)
            runner._redlist_excluded_paths.discard(snapshot.path)

    wave = 0
    while admission.capture_delta(inventory.scan.scan_id):
        owner._cancellation.checkpoint()
        state.check_run_budget(run_id)
        wave += 1
        if wave > MAX_ADMISSION_WAVES:
            # Do not call Dedupe with an incomplete producer frontier.
            state.record_event(run_id, "error", "curation-admission",
                               "Límite de descendencia de productores alcanzado",
                               {"waves": wave - 1, "pending": admission.pending_count})
            raise RuntimeError("curation_admission_producer_depth_limit")
        owner._curation_wave = wave
        runner._curation_round = wave
        reset_delta_path_hints()
        runner._expensive_admission = None
        # A policy previously activated for originals is not authority over
        # a new .dll whose bytes may prove PDF. Disable its extension shortcut
        # until this delta has completed Identify/Normalize.
        runner._redlist_policy_active = False
        runner._action_snapshot_selector = admission.current_page
        runner._action_snapshot_count = admission.pending_count
        state.set_run_phase(run_id, "curation_preclean")
        preclean = runner.run_artifact_prepass(admission.current_snapshots(), preidentify=True)
        record_cleanup(preclean, prefix="preclean")
        refresh()
        if wave > 1:
            inventory, result = owner._run_zip_intake_stage(
                state=state, run_id=run_id, root=boundary.access_policy.root,
                boundary=boundary, inventory=inventory, dedup_index=dedup_index,
                snapshots=admission.current_snapshots,
                stage_name="email-zip-intake" if wave == 2 else f"email-zip-intake-delta-{wave}",
                progress_operation="email-zip-intake",
                reconciliation_operation="email-zip-intake-reconciliation",
                reconciliation_phase="inventory_email_zip_reconciliation",
                preserve_primary_result=False,
            )
            _sum_report(nested, result, (
                "containers_examined", "generic_candidates", "atomic_packages", "blocked",
                "planned", "applied", "published", "trashed", "extracted_files",
            ))
            if result.get("status") in _FAILURES:
                report["status"] = "partial"
            # Reconcile the entire batch once if required, then select only
            # unseen observations. This includes all Archive descendants.
            refresh()
            admission.capture_delta(inventory.scan.scan_id)
            reset_delta_path_hints()
            runner._action_snapshot_count = admission.pending_count
        state.set_run_phase(run_id, "identify" if wave == 1 else "identify_delta")
        runner.identify_and_normalize(preserve_identified=wave > 1)
        if owner.config.apply_actions:
            admission.rebind(runner._normalized_paths)
        refresh()
        state.set_run_phase(run_id, "redlist")
        redlist = runner.apply_redlist_prepass(
            policy_digest=redlist_policy_digest(), preserve_exclusions=True,
        )
        record_cleanup(redlist, prefix="redlist")
        refresh()
        state.set_run_phase(run_id, "artifact_policy")
        artifacts = runner.run_artifact_prepass(admission.current_snapshots())
        record_cleanup(artifacts, prefix="content_cleanup")
        refresh()

        def decision(snapshot, runner=runner):
            if not runner._size_is_admitted(snapshot):
                return False, "size_limit"
            if runner._redlist_is_excluded(snapshot.path):
                return False, "redlist"
            if snapshot.path in getattr(runner, "_artifact_excluded_paths", ()):
                return False, "artifact_policy"
            key = runner._detection_key(snapshot)
            if snapshot.size and key not in runner._identified_types:
                return False, "identify_incomplete"
            detected = runner._identified_types.get(key)
            if owner.config.apply_actions and detected is not None and (
                not detected.accepts(snapshot.path) or LogicalFilename.parse(snapshot.path).gnu_suffixes
            ):
                return False, "normalization_incomplete"
            if getattr(detected, "mime", None) in {"application/zip", "application/x-zip-compressed"}:
                # A generic physical ZIP which survived intake is blocked or
                # preview-only; it cannot be mistaken for a settled document.
                return False, "archive_not_consumed"
            return True, "curated"

        admission.settle(decision)
        # EML parents are selected only from this just-curated delta. Durable
        # child receipts handle replay; older stabilized EMLs are not reopened.
        inventory, runner, result = owner._run_email_intake_stage(
            state=state, run_id=run_id, root=boundary.access_policy.root,
            boundary=boundary, inventory=inventory, dedup_index=dedup_index,
            action_runner=runner, excluded_paths=excluded_paths,
            snapshots=(snapshot for snapshot in admission.current_snapshots() if admission.permits(snapshot)),
            content_admission_check=admission.permits,
        )
        _sum_report(email, result, _COUNTERS)
        if result.get("status") in _FAILURES:
            report["status"] = "partial"
            # A failed producer may hide pending children. Abstain for every
            # EML in this batch, without trusting a truncated error sample.
            for snapshot in admission.current_snapshots():
                detected = runner._identified_types.get(runner._detection_key(snapshot))
                if getattr(detected, "mime", None) == "message/rfc822":
                    admission.exclude(snapshot, "producer_incomplete")
        refresh()
        state.record_event(run_id, "info", "curation-admission-delta",
                           "Delta físico evaluado antes de trabajo costoso",
                           {"wave": wave, "scan_id": inventory.scan.scan_id,
                            "email_materialized": result.get("attachments_materialized", 0),
                            **admission.summary()})

    runner._action_snapshot_selector = None
    runner._action_snapshot_count = None
    runner._expensive_admission = admission.permits
    owner._curation_admission_check = admission.permits
    report.update(admission.summary())
    report["pending_physical_admission"] = 0
    report["elapsed_ns"] = time.perf_counter_ns() - started
    report["expensive_work_avoided"] = sum(
        int(report[name]) for name in ("preclean_trashed", "redlist_trashed", "content_cleanup_trashed")
    ) + sum(int(count) for reason, count in report["exclusion_reasons"].items()
            if reason in {"redlist", "artifact_policy"})
    report["expensive_work_avoided_basis"] = "files_excluded_before_dedupe_not_cpu_estimate"
    if int(report["excluded"]) and owner.config.apply_actions:
        report["status"] = "partial"
    email["nested_zip_intake"] = nested
    state.publish_run_stage(run_id, "curation-admission", str(report["status"]),
                            details=report, idempotency_key="curation-admission:fixed-point")
    owner._curation_admission_result = report
    return inventory, runner, email
