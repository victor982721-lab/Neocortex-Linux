"""Read-only curation previews built from published NeoCortex state."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CURATION_PREVIEW_SCHEMA_VERSION": (".preview", "CURATION_PREVIEW_SCHEMA_VERSION"),
    "CurationItem": (".preview", "CurationItem"),
    "CurationPlanPage": (".preview", "CurationPlanPage"),
    "CurationPreview": (".preview", "CurationPreview"),
    "CurationSourceHead": (".preview", "CurationSourceHead"),
    "CurationStateError": (".preview", "CurationStateError"),
    "build_curation_plan_page": (".preview", "build_curation_plan_page"),
    "build_curation_preview": (".preview", "build_curation_preview"),
    "CURATION_REVIEW_SCHEMA_VERSION": (".lifecycle", "CURATION_REVIEW_SCHEMA_VERSION"),
    "CurationDecisionResult": (".lifecycle", "CurationDecisionResult"),
    "CurationLifecycleError": (".lifecycle", "CurationLifecycleError"),
    "CurationLifecycleSnapshotChanged": (
        ".lifecycle",
        "CurationLifecycleSnapshotChanged",
    ),
    "CurationLifecycleUnavailable": (".lifecycle", "CurationLifecycleUnavailable"),
    "CurationReviewItem": (".lifecycle", "CurationReviewItem"),
    "CurationReviewResult": (".lifecycle", "CurationReviewResult"),
    "decide_curation_item": (".lifecycle", "decide_curation_item"),
    "review_curation_page": (".lifecycle", "review_curation_page"),
    "CURATION_VERIFICATION_SCHEMA_VERSION": (
        ".verification",
        "CURATION_VERIFICATION_SCHEMA_VERSION",
    ),
    "CurationVerificationError": (".verification", "CurationVerificationError"),
    "CurationVerificationItem": (".verification", "CurationVerificationItem"),
    "CurationVerificationResult": (".verification", "CurationVerificationResult"),
    "CurationVerificationSnapshotChanged": (
        ".verification",
        "CurationVerificationSnapshotChanged",
    ),
    "CurationVerificationUnavailable": (
        ".verification",
        "CurationVerificationUnavailable",
    ),
    "CurationWorkBudget": (".verification", "CurationWorkBudget"),
    "verify_curation_page": (".verification", "verify_curation_page"),
    "CurationAuthorizationError": (".authorization", "CurationAuthorizationError"),
    "CurationAuthorizationOutcome": (".authorization", "CurationAuthorizationOutcome"),
    "CurationAuthorizationSnapshotChanged": (
        ".authorization",
        "CurationAuthorizationSnapshotChanged",
    ),
    "CurationAuthorizationUnavailable": (
        ".authorization",
        "CurationAuthorizationUnavailable",
    ),
    "authorize_curation_items": (".authorization", "authorize_curation_items"),
    "CURATION_APPLY_SCHEMA": (".application", "CURATION_APPLY_SCHEMA"),
    "CURATION_RECONCILE_SCHEMA": (".application", "CURATION_RECONCILE_SCHEMA"),
    "ApplyCandidate": (".application", "ApplyCandidate"),
    "AppliedEffect": (".application", "AppliedEffect"),
    "BackendOutcome": (".application", "BackendOutcome"),
    "CurationApplicationError": (".application", "CurationApplicationError"),
    "CurationApplicationCancelled": (".application", "CurationApplicationCancelled"),
    "CurationApplicationResult": (".application", "CurationApplicationResult"),
    "CurationApplicationSnapshotChanged": (
        ".application",
        "CurationApplicationSnapshotChanged",
    ),
    "CurationApplicationUnavailable": (
        ".application",
        "CurationApplicationUnavailable",
    ),
    "KioTrashBackend": (".application", "KioTrashBackend"),
    "PosixRenameBackend": (".application", "PosixRenameBackend"),
    "apply_authorization_grant": (".application", "apply_authorization_grant"),
    "reconcile_curation_actions": (".application", "reconcile_curation_actions"),
    "CURATION_RESTORE_SCHEMA": (".recovery", "CURATION_RESTORE_SCHEMA"),
    "RestoreCandidate": (".recovery", "RestoreCandidate"),
    "RestoreOutcome": (".recovery", "RestoreOutcome"),
    "RestoreBackend": (".recovery", "RestoreBackend"),
    "PosixRestoreBackend": (".recovery", "PosixRestoreBackend"),
    "restore_curation_action": (".recovery", "restore_curation_action"),
    "restore_curation_preview": (".recovery", "restore_curation_preview"),
    "restore_confirmation_token": (".recovery", "restore_confirmation_token"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CURATION_APPLY_SCHEMA",
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CURATION_RECONCILE_SCHEMA",
    "CURATION_RESTORE_SCHEMA",
    "CURATION_REVIEW_SCHEMA_VERSION",
    "CURATION_VERIFICATION_SCHEMA_VERSION",
    "AppliedEffect",
    "ApplyCandidate",
    "BackendOutcome",
    "CurationApplicationCancelled",
    "CurationApplicationError",
    "CurationApplicationResult",
    "CurationApplicationSnapshotChanged",
    "CurationApplicationUnavailable",
    "CurationAuthorizationError",
    "CurationAuthorizationOutcome",
    "CurationAuthorizationSnapshotChanged",
    "CurationAuthorizationUnavailable",
    "CurationDecisionResult",
    "CurationItem",
    "CurationLifecycleError",
    "CurationLifecycleSnapshotChanged",
    "CurationLifecycleUnavailable",
    "CurationPlanPage",
    "CurationPreview",
    "CurationReviewItem",
    "CurationReviewResult",
    "CurationSourceHead",
    "CurationStateError",
    "CurationVerificationError",
    "CurationVerificationItem",
    "CurationVerificationResult",
    "CurationVerificationSnapshotChanged",
    "CurationVerificationUnavailable",
    "CurationWorkBudget",
    "KioTrashBackend",
    "PosixRenameBackend",
    "PosixRestoreBackend",
    "RestoreBackend",
    "RestoreCandidate",
    "RestoreOutcome",
    "apply_authorization_grant",
    "authorize_curation_items",
    "build_curation_plan_page",
    "build_curation_preview",
    "decide_curation_item",
    "reconcile_curation_actions",
    "restore_confirmation_token",
    "restore_curation_action",
    "restore_curation_preview",
    "review_curation_page",
    "verify_curation_page",
]
