"""Dependency-light contracts shared by Framework state owner slices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from neocortex.safety.corpus_access import CorpusAccessPolicy

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


__all__ = [
    "CONTENT_TYPE_CACHE_LOOKUP_BATCH_SIZE",
    "CONTENT_TYPE_CACHE_WRITE_BATCH_SIZE",
    "DurableInventoryBinding",
    "DurableInventoryOwner",
    "InventoryRunEvidence",
    "RunBudgetExceeded",
    "bounded_lifecycle_name",
]
