"""Small run-scoped content-admission primitives shared by dedup owners.

Inventory is intentionally not filtered by this policy.  Callers use the
validated ceiling only when selecting snapshots for a content-aware phase.
The value is therefore ephemeral run configuration, not file evidence.
"""

from __future__ import annotations


def validate_max_file_bytes(value: int | None) -> int | None:
    """Return a valid decimal-byte ceiling, or ``None`` for unlimited runs."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_file_bytes must be a positive integer or None")
    return value


def size_is_admitted(size: int, max_file_bytes: int | None) -> bool:
    """Apply one global size decision without touching file content."""

    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("file size must be a non-negative integer")
    limit = validate_max_file_bytes(max_file_bytes)
    return limit is None or size <= limit


__all__ = ["size_is_admitted", "validate_max_file_bytes"]
