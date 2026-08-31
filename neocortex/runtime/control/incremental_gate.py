"""Read-only authorization gate for reusing one durable inventory boundary.
# region [00] Contexto del módulo
# Módulo: neocortex/incremental_gate.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


The evaluator deliberately owns the ordering shared by normal and read-only
inventory runs. It reads evidence from the framework and inventory owners, but
never creates, updates, or repairs either owner.
"""

# region [01] Dependencias del módulo
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from neocortex.enumeration import JournalCursor
from neocortex.deduplication import InventoryError
# endregion [01]

# region [02] Implementación


# ``analyze_only`` is retained solely to read legacy framework rows; no current
# public command produces that mode.
IncrementalAccessMode = Literal["normal", "analyze_only"]
RootIdentity = tuple[int | None, int | None, int | None]
IncrementalGateReason = Literal[
    "no_matching_latest_durable_run",
    "durable_policy_not_reusable",
    "durable_root_identity_mismatch",
    "missing_checkpoint",
    "checkpoint_scan_mismatch",
    "durable_cursor_missing",
    "checkpoint_not_at_durable_boundary",
    "live_cursor_incompatible",
    "checkpoint_scan_not_reusable",
    "checkpoint_evidence_mismatch",
    "latest_durable_checkpoint_match",
]


class RootPolicyEvidence(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def root(self) -> Path: ...

    @property
    def root_device_id(self) -> int | None: ...

    @property
    def root_file_id(self) -> int | None: ...

    @property
    def root_birthtime_ns(self) -> int | None: ...


class MutationGuardEvidence(Protocol):
    @property
    def policy(self) -> RootPolicyEvidence: ...


class DurableBindingEvidence(Protocol):
    @property
    def run_id(self) -> int: ...

    @property
    def scan_id(self) -> int: ...

    @property
    def end_cursor(self) -> JournalCursor | None: ...


class InventoryCheckpointEvidence(Protocol):
    @property
    def scan_id(self) -> int: ...

    @property
    def volume(self) -> str | None: ...

    @property
    def journal_id(self) -> int | None: ...

    @property
    def next_usn(self) -> int | None: ...

    @property
    def valid(self) -> bool: ...

    @property
    def inventory_policy_signature(self) -> str | None: ...


class ScanSummaryEvidence(Protocol):
    @property
    def root(self) -> str: ...

    @property
    def files_seen(self) -> int: ...


class FrameworkGateEvidence(Protocol):
    def latest_durable_inventory_binding(
        self,
        root: Path,
        *,
        corpus_access_mode: str | None = None,
        inventory_policy_signature: str | None = None,
    ) -> DurableBindingEvidence | None: ...

    def corpus_mutation_guard(self, run_id: int) -> MutationGuardEvidence: ...


class InventoryGateEvidence(Protocol):
    def inventory_checkpoint(
        self,
        root: str | Path,
    ) -> InventoryCheckpointEvidence | None: ...

    def require_scan_inventory_policy_signature(
        self,
        scan_id: int,
        expected_signature: str,
    ) -> None: ...

    def scan_summary(self, scan_id: int) -> ScanSummaryEvidence: ...

    def scan_root_identity(self, scan_id: int) -> tuple[int, int, int]: ...

    def file_count(self, scan_id: int) -> int: ...


@dataclass(frozen=True, slots=True)
class IncrementalGateRequest:
    """Immutable policy and live-boundary inputs for one authorization check."""

    root: Path
    corpus_access_mode: IncrementalAccessMode
    framework_policy_signature: str
    inventory_policy_signature: str
    root_identity: RootIdentity
    journal_before: JournalCursor
    verify_final: Callable[[], None]

    @classmethod
    def from_access_policy(
        cls,
        access_policy: RootPolicyEvidence,
        *,
        framework_policy_signature: str,
        inventory_policy_signature: str,
        journal_before: JournalCursor,
        verify_final: Callable[[], None],
    ) -> IncrementalGateRequest:
        mode = access_policy.mode
        if mode not in {"normal", "analyze_only"}:
            raise ValueError(f"unsupported corpus access mode: {mode!r}")
        return cls(
            root=access_policy.root,
            corpus_access_mode=cast(IncrementalAccessMode, mode),
            framework_policy_signature=framework_policy_signature,
            inventory_policy_signature=inventory_policy_signature,
            root_identity=(
                access_policy.root_device_id,
                access_policy.root_file_id,
                access_policy.root_birthtime_ns,
            ),
            journal_before=journal_before,
            verify_final=verify_final,
        )


@dataclass(frozen=True, slots=True)
class IncrementalGateDecision:
    """Fail-closed decision with the exact durable owner considered."""

    allowed: bool
    reason: IncrementalGateReason
    source_run_id: int | None

    def as_tuple(self) -> tuple[bool, str, int | None]:
        return self.allowed, self.reason, self.source_run_id


def _normalized_root(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _denied(
    reason: IncrementalGateReason,
    source_run_id: int | None,
) -> IncrementalGateDecision:
    return IncrementalGateDecision(False, reason, source_run_id)


def _persisted_root_reason(
    request: IncrementalGateRequest,
    state: FrameworkGateEvidence,
    source_run_id: int,
) -> IncrementalGateReason | None:
    try:
        persisted_policy = state.corpus_mutation_guard(source_run_id).policy
    except (PermissionError, ValueError):
        return "durable_policy_not_reusable"
    persisted_identity = (
        persisted_policy.root_device_id,
        persisted_policy.root_file_id,
        persisted_policy.root_birthtime_ns,
    )
    if (
        persisted_policy.mode != request.corpus_access_mode
        or _normalized_root(persisted_policy.root) != _normalized_root(request.root)
        or persisted_identity != request.root_identity
    ):
        return "durable_root_identity_mismatch"
    return None


def _checkpoint_reason(
    request: IncrementalGateRequest,
    checkpoint: InventoryCheckpointEvidence,
    source_scan_id: int,
) -> IncrementalGateReason | None:
    if (
        not checkpoint.valid
        or checkpoint.scan_id != source_scan_id
        or checkpoint.inventory_policy_signature != request.inventory_policy_signature
    ):
        return "checkpoint_scan_mismatch"
    return None


def _cursor_reason(
    request: IncrementalGateRequest,
    checkpoint: InventoryCheckpointEvidence,
    durable_cursor: JournalCursor | None,
) -> IncrementalGateReason | None:
    if durable_cursor is None:
        return "durable_cursor_missing"
    if checkpoint.volume is None or checkpoint.journal_id is None or checkpoint.next_usn is None:
        return "checkpoint_not_at_durable_boundary"
    if (
        checkpoint.volume != durable_cursor.volume
        or checkpoint.journal_id != durable_cursor.journal_id
        or checkpoint.next_usn != durable_cursor.next_usn
    ):
        return "checkpoint_not_at_durable_boundary"
    live_cursor = request.journal_before
    if (
        durable_cursor.volume != live_cursor.volume
        or durable_cursor.journal_id != live_cursor.journal_id
        or durable_cursor.next_usn > live_cursor.next_usn
    ):
        return "live_cursor_incompatible"
    return None


def _scan_reason(
    request: IncrementalGateRequest,
    inventory: InventoryGateEvidence,
    source_scan_id: int,
) -> IncrementalGateReason | None:
    try:
        inventory.require_scan_inventory_policy_signature(
            source_scan_id,
            request.inventory_policy_signature,
        )
        summary = inventory.scan_summary(source_scan_id)
        scan_identity = inventory.scan_root_identity(source_scan_id)
        persisted_files = inventory.file_count(source_scan_id)
    except InventoryError:
        return "checkpoint_scan_not_reusable"
    if (
        _normalized_root(summary.root) != _normalized_root(request.root)
        or scan_identity != request.root_identity
        or persisted_files != summary.files_seen
    ):
        return "checkpoint_evidence_mismatch"
    return None


def evaluate_incremental_gate(
    request: IncrementalGateRequest,
    *,
    state: FrameworkGateEvidence,
    inventory: InventoryGateEvidence,
) -> IncrementalGateDecision:
    """Authorize reuse from only the newest exact durable owner.

    Evidence is evaluated in this fail-closed order: owner, persisted guard and
    root identity, raw checkpoint binding, durable/live cursor, scan evidence,
    and finally the caller's live boundary verifier.
    """

    owner = state.latest_durable_inventory_binding(
        request.root,
        corpus_access_mode=request.corpus_access_mode,
        inventory_policy_signature=request.framework_policy_signature,
    )
    if owner is None:
        return _denied("no_matching_latest_durable_run", None)
    source_run_id = owner.run_id

    reason = _persisted_root_reason(request, state, source_run_id)
    if reason is not None:
        return _denied(reason, source_run_id)

    checkpoint = inventory.inventory_checkpoint(request.root)
    if checkpoint is None:
        return _denied("missing_checkpoint", source_run_id)
    reason = _checkpoint_reason(request, checkpoint, owner.scan_id)
    if reason is not None:
        return _denied(reason, source_run_id)

    reason = _cursor_reason(request, checkpoint, owner.end_cursor)
    if reason is not None:
        return _denied(reason, source_run_id)

    reason = _scan_reason(request, inventory, owner.scan_id)
    if reason is not None:
        return _denied(reason, source_run_id)

    request.verify_final()
    return IncrementalGateDecision(
        True,
        "latest_durable_checkpoint_match",
        source_run_id,
    )


__all__ = (
    "FrameworkGateEvidence",
    "IncrementalGateDecision",
    "IncrementalGateRequest",
    "InventoryGateEvidence",
    "evaluate_incremental_gate",
)
# endregion [02]
