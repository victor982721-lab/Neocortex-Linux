"""Durable organization obligations within initial and resumed Framework runs."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING

from neocortex.platform.policy import stat_birthtime_ns
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.documents.document_organization_recovery import (
    OrganizationRecoveryRequired,
    capture_organization_checkpoint,
    inspect_organization_checkpoint,
)

if TYPE_CHECKING:
    from neocortex.persistence.framework_state_writer import FrameworkState
    from neocortex.runtime.models import FrameworkConfig
    from neocortex.runtime.control.cancellation import CancellationToken
    from neocortex.progress import ProgressCallback
    from neocortex.documents.document_organization_models import OrganizationApplySummary, OrganizationPlanSummary

STAGES = ("organization_plan", "organization_apply")
STAGE_SCHEMA = "neocortex.organization-stage/v1"
TERMINAL = {"completed", "skipped"}


def organization_stage_state(state: FrameworkState, run_id: int) -> dict[str, dict[str, Any]]:
    reader = getattr(state, "read_run_stages", None)
    if not callable(reader):
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for event in reader(run_id):
        if event.get("stage") in STAGES:
            latest[str(event["stage"])] = dict(event)
    return latest


def organization_pending(state: FrameworkState, run_id: int) -> bool:
    return any(event.get("status") not in TERMINAL for event in organization_stage_state(state, run_id).values())


def _details(config: FrameworkConfig, root: Path) -> dict[str, object]:
    from neocortex.documents.document_organization import default_organization_root

    destination = config.organization_root
    if destination is None:
        destination = default_organization_root(config.framework_database, analysis_root=root)
    else:
        destination = Path(os.path.abspath(destination.expanduser()))
    st = root.lstat()
    return {
        "schema": STAGE_SCHEMA, "root": str(root),
        "root_identity": [st.st_dev, st.st_ino, stat_birthtime_ns(st)],
        "organization_root": str(destination),
        "min_confidence": config.organization_min_confidence,
        "apply_requested": config.apply_actions,
    }


def register_organization_stages(state: FrameworkState, run_id: int, config: FrameworkConfig, root: Path) -> None:
    details = _details(config, root)
    state.publish_run_stage(run_id, "organization_plan", "pending", details=details)
    if config.apply_actions:
        state.publish_run_stage(run_id, "organization_apply", "pending", details=details)


def copy_organization_stages(state: FrameworkState, source_run_id: int, target_run_id: int) -> None:
    stages = organization_stage_state(state, source_run_id)
    if not any(event.get("status") not in TERMINAL for event in stages.values()):
        return
    checkpoints = {
        str(event["stage"]): dict(event["checkpoint"])
        for event in state.read_run_checkpoints(source_run_id)
        if event.get("stage") in STAGES
    }
    for name in STAGES:
        event = stages.get(name)
        if event is None:
            continue
        details = dict(event["details"])
        if details.get("schema") != STAGE_SCHEMA:
            raise OrganizationRecoveryRequired("organization stage has no supported scope contract")
        details["resumed_from_run_id"] = source_run_id
        status = str(event["status"]) if event["status"] in TERMINAL else "pending"
        state.publish_run_stage(target_run_id, name, status, details=details, checkpoint=checkpoints.get(name))


def run_organization_stages(
    config: FrameworkConfig,
    *,
    root: Path,
    state: FrameworkState,
    run_id: int,
    progress: ProgressCallback | None,
    cancellation: CancellationToken,
    reserve: Callable[[str, str, int], object],
) -> tuple[OrganizationPlanSummary | None, OrganizationApplySummary | None]:
    from neocortex.documents.document_organization import (
        apply_all_document_organization, capture_organization_input_scope,
        plan_document_organization,
    )
    from neocortex.documents.document_organization_planning import OrganizationCorpusPolicy

    stages = organization_stage_state(state, run_id)
    managed = bool(stages)
    record = stages.get("organization_plan") or stages.get("organization_apply")
    details = dict(record["details"]) if record is not None else _details(config, root)
    observed = root.lstat()
    if (
        details.get("schema") != STAGE_SCHEMA or details.get("root") != str(root)
        or details.get("root_identity") != [observed.st_dev, observed.st_ino, stat_birthtime_ns(observed)]
    ):
        raise OrganizationRecoveryRequired("organization stage corpus identity changed")
    organization_root = Path(str(details["organization_root"]))
    if not organization_root.is_absolute() or str(organization_root) != os.path.abspath(organization_root):
        raise OrganizationRecoveryRequired("organization destination is not canonical")
    apply_requested = details.get("apply_requested") is True
    checkpoints = (
        {str(event["stage"]): dict(event["checkpoint"]) for event in state.read_run_checkpoints(run_id) if event.get("stage") in STAGES}
        if managed else {}
    )

    def publish(name: str, status: str, checkpoint: Mapping[str, Any] | None = None, *, reason: str | None = None) -> None:
        if managed:
            payload = dict(details)
            if reason is not None:
                payload["reason"] = reason
            state.publish_run_stage(run_id, name, status, details=payload, checkpoint=checkpoint)

    def checkpoint_cancellation() -> None:
        try:
            cancellation.checkpoint()
        except CancellationRequested as exc:
            raise KeyboardInterrupt("organization cancelled") from exc

    def checked_progress(event: object) -> None:
        checkpoint_cancellation()
        if progress is not None:
            progress(event)  # type: ignore[arg-type]
        checkpoint_cancellation()

    if not config.document_catalog_database.is_file():
        if checkpoints:
            raise OrganizationRecoveryRequired("organization owner disappeared after preparation")
        state.record_event(run_id, "warning", "document-organization-plan", "Plan no disponible: todavía no hay un catálogo durable", {"reason": "catalog_unavailable", "effects": "none"})
        publish("organization_plan", "skipped", reason="catalog_unavailable")
        if apply_requested:
            publish("organization_apply", "skipped", reason="catalog_unavailable")
        return None, None

    plan_record = stages.get("organization_plan", {})
    checkpoint = checkpoints.get("organization_plan")
    if plan_record.get("status") == "skipped":
        return None, None
    if plan_record.get("status") == "completed":
        if checkpoint is None:
            raise OrganizationRecoveryRequired("completed organization plan has no owner checkpoint")
        plan_summary, _ = inspect_organization_checkpoint(config.document_catalog_database, checkpoint, root, organization_root)
    else:
        checkpoint_cancellation()
        state.set_run_phase(run_id, "organization_plan")
        if checkpoint is not None:
            from neocortex.documents.document_organization_scope import OrganizationInputScope

            if checkpoint.get("schema") != "neocortex.organization-preparation/v1":
                raise OrganizationRecoveryRequired("organization preparation checkpoint is invalid")
            source_scope = OrganizationInputScope.from_json(checkpoint["source_scope"])
            if source_scope.root != root or checkpoint.get("organization_root") != str(organization_root):
                raise OrganizationRecoveryRequired("organization preparation scope changed")
            source_scope.verify()
        else:
            source_scope = capture_organization_input_scope(config.document_catalog_database, root)
        preparation = {"schema": "neocortex.organization-preparation/v1", "source_scope": source_scope.serialized, "organization_root": str(organization_root)}
        publish("organization_plan", "running", preparation)
        arguments: dict[str, Any] = {
            "source_scope": source_scope, "min_confidence": details["min_confidence"],
            "cancellation": cancellation,
            "progress": checked_progress, "mutation_guard": state.corpus_mutation_guard(run_id),
            "corpus_policy": OrganizationCorpusPolicy(allow_general=True, allow_uncertain=True, allow_sensitive=True, allow_nontechnical=True),
        }
        signature_target = getattr(plan_document_organization, "side_effect", None)
        if not callable(signature_target):
            signature_target = plan_document_organization
        parameters: Mapping[str, inspect.Parameter]
        try:
            parameters = inspect.signature(signature_target).parameters
        except (TypeError, ValueError):
            parameters = {}
        if not any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            for optional in ("corpus_policy", "cancellation"):
                if optional not in parameters:
                    arguments.pop(optional, None)
        try:
            plan_summary = plan_document_organization(config.document_catalog_database, organization_root, **arguments)
            checkpoint = capture_organization_checkpoint(config.document_catalog_database, source_scope, organization_root, plan_summary) if managed else None
            reserve("organization_plan", f"organization:plan:{plan_summary.catalog_run_id}", int(plan_summary.considered))
            publish("organization_plan", "completed", checkpoint)
        except BaseException as exc:
            publish("organization_plan", "interrupted" if isinstance(exc, (KeyboardInterrupt, CancellationRequested)) else "failed", preparation, reason=type(exc).__name__)
            if isinstance(exc, CancellationRequested):
                raise KeyboardInterrupt("organization cancelled") from exc
            raise
        state.record_event(run_id, "warning" if plan_summary.blocked else "info", "document-organization-plan", "Plan de organización técnica completado", {"organization_root": str(organization_root), **asdict(plan_summary)})
    checkpoint_cancellation()
    if not apply_requested:
        return plan_summary, None
    # A resume is a request to recover state, not an AuthorizationGrant. Read
    # the exact owner intent and statuses before considering the old effect
    # path; uncertain or unauthorized application remains explicitly pending.
    if checkpoint is not None:
        _, statuses = inspect_organization_checkpoint(config.document_catalog_database, checkpoint, root, organization_root)
        uncertain = any(statuses.get(name, 0) for name in ("applying", "moved_cache_pending", "recovery_required"))
    else:
        uncertain = True
    if uncertain or not config.apply_actions or config.resume_run_id is not None:
        reason = "organization_effect_reconciliation_required" if uncertain else "organization_apply_authority_required"
        publish("organization_apply", "partial", checkpoint, reason=reason)
        raise OrganizationRecoveryRequired(reason)
    state.set_run_phase(run_id, "organization_apply")
    publish("organization_apply", "running", checkpoint)
    try:
        apply_summary = apply_all_document_organization(config.document_catalog_database, organization_root, progress=checked_progress, mutation_guard=state.corpus_mutation_guard(run_id))
        issues = apply_summary.stale + apply_summary.blocked + apply_summary.failed + apply_summary.cache_pending + apply_summary.remaining
        reserve("organization_apply", f"organization:apply:{apply_summary.catalog_run_id}", int(apply_summary.selected))
        publish("organization_apply", "partial" if issues else "completed", checkpoint, reason="organization_apply_incomplete" if issues else None)
        if issues:
            raise OrganizationRecoveryRequired("organization_apply_incomplete")
    except BaseException as exc:
        if not isinstance(exc, OrganizationRecoveryRequired):
            publish("organization_apply", "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", checkpoint, reason=type(exc).__name__)
        raise
    state.record_event(run_id, "info", "document-organization-apply", "Aplicación de organización técnica completada", {"organization_root": str(organization_root), **asdict(apply_summary)})
    return plan_summary, apply_summary
