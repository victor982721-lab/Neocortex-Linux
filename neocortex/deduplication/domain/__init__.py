"""Domain values and errors for deduplication."""

from .errors import DedupError, FileChangedError, InventoryError, MissingDependencyError
from .models import (
    DedupPlan,
    DuplicateGroup,
    FileSnapshot,
    InventoryCheckpoint,
    PlanStatistics,
    ScanSummary,
)

__all__ = [
    "DedupError",
    "DedupPlan",
    "DuplicateGroup",
    "FileChangedError",
    "FileSnapshot",
    "InventoryCheckpoint",
    "InventoryError",
    "MissingDependencyError",
    "PlanStatistics",
    "ScanSummary",
]
