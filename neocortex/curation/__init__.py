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
    "CURATION_CHECKPOINT_CONTRACT": (".checkpoints", "CURATION_CHECKPOINT_CONTRACT"),
    "CURATION_CHECKPOINT_SCHEMA_VERSION": (
        ".checkpoints",
        "CURATION_CHECKPOINT_SCHEMA_VERSION",
    ),
    "CurationCheckpoint": (".checkpoints", "CurationCheckpoint"),
    "CurationCheckpointBudget": (".checkpoints", "CurationCheckpointBudget"),
    "CurationCheckpointConflictError": (".checkpoints", "CurationCheckpointConflictError"),
    "CurationCheckpointCorruptError": (".checkpoints", "CurationCheckpointCorruptError"),
    "CurationCheckpointError": (".checkpoints", "CurationCheckpointError"),
    "CurationCheckpointRoot": (".checkpoints", "CurationCheckpointRoot"),
    "CurationCheckpointSourceHead": (".checkpoints", "CurationCheckpointSourceHead"),
    "CurationCheckpointStorageError": (".checkpoints", "CurationCheckpointStorageError"),
    "CurationCheckpointValidation": (".checkpoints", "CurationCheckpointValidation"),
    "CurationCheckpointResume": (".checkpoints", "CurationCheckpointResume"),
    "CurationSnapshotObservation": (".checkpoints", "CurationSnapshotObservation"),
    "CurationSnapshotObserver": (".checkpoints", "CurationSnapshotObserver"),
    "compute_batch_digest": (".checkpoints", "compute_batch_digest"),
    "create_checkpoint": (".checkpoints", "create_checkpoint"),
    "read_checkpoint": (".checkpoints", "read_checkpoint"),
    "resume_checkpoint": (".checkpoints", "resume_checkpoint"),
    "validate_checkpoint": (".checkpoints", "validate_checkpoint"),
    "write_checkpoint": (".checkpoints", "write_checkpoint"),
    "CURATION_RESTORE_SCHEMA": (".recovery", "CURATION_RESTORE_SCHEMA"),
    "RestoreCandidate": (".recovery", "RestoreCandidate"),
    "RestoreOutcome": (".recovery", "RestoreOutcome"),
    "RestoreBackend": (".recovery", "RestoreBackend"),
    "PosixRestoreBackend": (".recovery", "PosixRestoreBackend"),
    "restore_curation_action": (".recovery", "restore_curation_action"),
    "restore_curation_preview": (".recovery", "restore_curation_preview"),
    "restore_confirmation_token": (".recovery", "restore_confirmation_token"),
    "CURATION_READ_SCHEMA": (".read", "CURATION_READ_SCHEMA"),
    "CurationAttemptView": (".read", "CurationAttemptView"),
    "CurationRecoveryView": (".read", "CurationRecoveryView"),
    "CurationReadSnapshot": (".read", "CurationReadSnapshot"),
    "read_curation_snapshot": (".read", "read_curation_snapshot"),
    "KIO_DESKTOP_HARNESS_CLIENTS": (".kio_harness", "KIO_DESKTOP_HARNESS_CLIENTS"),
    "KIO_DESKTOP_HARNESS_SCHEMA": (".kio_harness", "KIO_DESKTOP_HARNESS_SCHEMA"),
    "KIO_DESKTOP_HARNESS_TRASH_URL": (".kio_harness", "KIO_DESKTOP_HARNESS_TRASH_URL"),
    "KioDesktopHarnessError": (".kio_harness", "KioDesktopHarnessError"),
    "KioDesktopHarnessSpec": (".kio_harness", "KioDesktopHarnessSpec"),
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


__all__ = [  # noqa: RUF022
    "CURATION_PREVIEW_SCHEMA_VERSION",
    "CURATION_RESTORE_SCHEMA",
    "CURATION_VERIFICATION_SCHEMA_VERSION",
    "CurationItem",
    "CurationPlanPage",
    "CurationPreview",
    "CurationSourceHead",
    "CurationStateError",
    "CurationVerificationError",
    "CurationVerificationItem",
    "CurationVerificationResult",
    "CurationVerificationSnapshotChanged",
    "CurationVerificationUnavailable",
    "CurationWorkBudget",
    "CURATION_CHECKPOINT_CONTRACT",
    "CURATION_CHECKPOINT_SCHEMA_VERSION",
    "CurationCheckpoint",
    "CurationCheckpointBudget",
    "CurationCheckpointConflictError",
    "CurationCheckpointCorruptError",
    "CurationCheckpointError",
    "CurationCheckpointRoot",
    "CurationCheckpointSourceHead",
    "CurationCheckpointStorageError",
    "CurationCheckpointValidation",
    "CurationCheckpointResume",
    "CurationSnapshotObservation",
    "CurationSnapshotObserver",
    "PosixRestoreBackend",
    "RestoreBackend",
    "RestoreCandidate",
    "RestoreOutcome",
    "build_curation_plan_page",
    "build_curation_preview",
    "compute_batch_digest",
    "create_checkpoint",
    "read_checkpoint",
    "restore_confirmation_token",
    "restore_curation_action",
    "restore_curation_preview",
    "CURATION_READ_SCHEMA",
    "CurationAttemptView",
    "CurationRecoveryView",
    "CurationReadSnapshot",
    "read_curation_snapshot",
    "KIO_DESKTOP_HARNESS_CLIENTS",
    "KIO_DESKTOP_HARNESS_SCHEMA",
    "KIO_DESKTOP_HARNESS_TRASH_URL",
    "KioDesktopHarnessError",
    "KioDesktopHarnessSpec",
    "resume_checkpoint",
    "verify_curation_page",
    "validate_checkpoint",
    "write_checkpoint",
]
