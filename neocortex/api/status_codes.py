"""Shared status codes for canonical read-only API surfaces."""

from __future__ import annotations

from enum import IntEnum


class KnowledgeExitCode(IntEnum):
    """Stable process codes shared by Knowledge and review adapters."""

    SUCCESS = 0
    FATAL = 1
    USAGE = 2
    NO_RESULTS = 3
    PARTIAL = 4
    SNAPSHOT_CHANGED = 5
    SCHEMA_INCOMPATIBLE = 6
    CORRUPT = 7
    CANCELLED = 130


__all__ = ["KnowledgeExitCode"]
