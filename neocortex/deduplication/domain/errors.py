"""Stable exception hierarchy for inventory and deduplication."""


class DedupError(Exception):
    """Base package exception."""


class MissingDependencyError(DedupError):
    """A required optimized native dependency is unavailable."""


class FileChangedError(DedupError):
    """A file changed while it was being fingerprinted or compared."""


class InventoryError(DedupError):
    """An inventory operation could not be completed safely."""


__all__ = [
    "DedupError",
    "FileChangedError",
    "InventoryError",
    "MissingDependencyError",
]
