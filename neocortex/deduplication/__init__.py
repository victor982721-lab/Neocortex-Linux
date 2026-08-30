"""Canonical inventory and exact-deduplication boundary."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .domain.errors import DedupError as DedupError
    from .domain.errors import FileChangedError as FileChangedError
    from .domain.errors import InventoryError as InventoryError
    from .domain.errors import MissingDependencyError as MissingDependencyError
    from .domain.models import DedupPlan as DedupPlan
    from .domain.models import DuplicateGroup as DuplicateGroup
    from .domain.models import FileSnapshot as FileSnapshot
    from .domain.models import InventoryCheckpoint as InventoryCheckpoint
    from .domain.models import PlanStatistics as PlanStatistics
    from .domain.models import ScanSummary as ScanSummary
    from .fingerprinting import FULL_ALGORITHM as FULL_ALGORITHM
    from .fingerprinting import PARTIAL_ALGORITHM as PARTIAL_ALGORITHM
    from .fingerprinting import files_equal_exact as files_equal_exact
    from .fingerprinting import full_fingerprint as full_fingerprint
    from .fingerprinting import partial_fingerprint as partial_fingerprint
    from .fingerprinting import snapshot_path as snapshot_path
    from .fingerprinting import stat_matches_snapshot as stat_matches_snapshot
    from .inventory.index import DedupIndex as DedupIndex
    from .inventory.scan import (
        DEFAULT_INVENTORY_EXCLUSION_POLICY as DEFAULT_INVENTORY_EXCLUSION_POLICY,
    )
    from .inventory.scan import InventoryExclusionPolicy as InventoryExclusionPolicy
    from .planning.planner import DedupPlanner as DedupPlanner

_EXPORTS: Final = {
    "DedupError": (".domain.errors", "DedupError"),
    "DedupIndex": (".inventory.index", "DedupIndex"),
    "DedupPlan": (".domain.models", "DedupPlan"),
    "DedupPlanner": (".planning.planner", "DedupPlanner"),
    "DuplicateGroup": (".domain.models", "DuplicateGroup"),
    "DEFAULT_INVENTORY_EXCLUSION_POLICY": (
        ".inventory.scan",
        "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    ),
    "FULL_ALGORITHM": (".fingerprinting", "FULL_ALGORITHM"),
    "FileChangedError": (".domain.errors", "FileChangedError"),
    "FileSnapshot": (".domain.models", "FileSnapshot"),
    "InventoryCheckpoint": (".domain.models", "InventoryCheckpoint"),
    "InventoryError": (".domain.errors", "InventoryError"),
    "InventoryExclusionPolicy": (".inventory.scan", "InventoryExclusionPolicy"),
    "MissingDependencyError": (".domain.errors", "MissingDependencyError"),
    "PARTIAL_ALGORITHM": (".fingerprinting", "PARTIAL_ALGORITHM"),
    "PlanStatistics": (".domain.models", "PlanStatistics"),
    "ScanSummary": (".domain.models", "ScanSummary"),
    "files_equal_exact": (".fingerprinting", "files_equal_exact"),
    "full_fingerprint": (".fingerprinting", "full_fingerprint"),
    "partial_fingerprint": (".fingerprinting", "partial_fingerprint"),
    "snapshot_path": (".fingerprinting", "snapshot_path"),
    "stat_matches_snapshot": (".fingerprinting", "stat_matches_snapshot"),
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
    "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    "FULL_ALGORITHM",
    "PARTIAL_ALGORITHM",
    "DedupError",
    "DedupIndex",
    "DedupPlan",
    "DedupPlanner",
    "DuplicateGroup",
    "FileChangedError",
    "FileSnapshot",
    "InventoryCheckpoint",
    "InventoryError",
    "InventoryExclusionPolicy",
    "MissingDependencyError",
    "PlanStatistics",
    "ScanSummary",
    "files_equal_exact",
    "full_fingerprint",
    "partial_fingerprint",
    "snapshot_path",
    "stat_matches_snapshot",
]
