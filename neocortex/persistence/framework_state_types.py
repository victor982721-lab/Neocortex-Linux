"""Dependency-light contracts shared by Framework state owner slices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from neocortex.safety.corpus_access import CorpusAccessPolicy

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

    from neocortex.deduplication import FileSnapshot
    from neocortex.platform.content_types import DetectedType

CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE = 256
CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE = 512


class RunBudgetExceeded(RuntimeError):
    """A durable run budget rejected additional work."""

    def __init__(self, reason: str, snapshot: Mapping[str, Any] | None = None):
        self.reason = reason
        self.snapshot = None if snapshot is None else dict(snapshot)
        super().__init__(f"run budget exceeded: {reason}")


def bounded_lifecycle_name(value: object, *, label: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > limit:
        raise ValueError(f"{label} is empty or too large")
    return value


@dataclass(frozen=True, slots=True)
class InventoryRunEvidence:
    event_id: int
    scan_id: int
    files: int
    reconciliation_records: int
    inventory_attempts: int
    inventory_mode: str


@dataclass(frozen=True, slots=True)
class DurableInventoryBinding:
    run_id: int
    scan_id: int
    corpus_access_mode: str
    inventory_policy_signature: str | None
    end_cursor: None


@dataclass(frozen=True, slots=True)
class DurableInventoryOwner:
    binding: DurableInventoryBinding
    access_policy: CorpusAccessPolicy


class _FrameworkStateOwner:
    """Type-only surface shared by the extracted state-owner mixins.

    The concrete :class:`FrameworkState` supplies these attributes and methods
    across its mixins.  Keeping the declarations here lets each slice retain a
    precise static contract without adding a second runtime owner or a module
    wide ``attr-defined`` suppression.
    """

    if TYPE_CHECKING:
        _connection: sqlite3.Connection
        path: Path

        def _append_lifecycle_event_once(
            self,
            run_id: int,
            *,
            level: str,
            phase: str,
            message: str,
            idempotency_key: str,
            details: Mapping[str, Any],
        ) -> bool: ...

        def cancel_initial_run(self, run_id: int) -> bool: ...

        def fail_initial_run(self, run_id: int) -> bool: ...

        def route_candidate_workload(self, run_id: int) -> tuple[int, int]: ...

        def _read_run_budget_locked(self, run_id: int) -> dict[str, Any] | None: ...

        def read_run_manifest(self, run_id: int) -> dict[str, Any] | None: ...

        def read_run_route_capabilities(self, run_id: int) -> dict[str, str]: ...

        def read_run_stages(self, run_id: int) -> tuple[dict[str, Any], ...]: ...

        def read_route_input_sources(self, run_id: int) -> dict[str, str]: ...

        def _pending_organization_stages(self, run_id: int) -> tuple[str, ...]: ...

        @staticmethod
        def _validate_inventory_binding(
            scan_id: int,
            reconciliation_records: int,
            inventory_attempts: int,
            inventory_mode: str,
            candidate_rows: int,
        ) -> None: ...

        def _check_run_completion_budget_locked(
            self,
            run_id: int,
        ) -> dict[str, Any] | None: ...

        def _check_organization_completion_locked(self, run_id: int) -> None: ...

        @staticmethod
        def _content_type_cache_key(
            snapshot: FileSnapshot,
        ) -> tuple[int, int, int, int, int]: ...

        @staticmethod
        def _decode_content_type_cache_row(
            row: sqlite3.Row | tuple[object, ...],
        ) -> DetectedType | None: ...


__all__ = [
    "CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE",
    "CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE",
    "DurableInventoryBinding",
    "DurableInventoryOwner",
    "InventoryRunEvidence",
    "RunBudgetExceeded",
    "bounded_lifecycle_name",
]
