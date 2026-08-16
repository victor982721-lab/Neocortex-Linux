"""Stable result contracts for bounded archive indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....processing_provenance import ROUTE_SUMMARY_SCHEMA


@dataclass(frozen=True, slots=True)
class ArchiveRouteSummary:
    candidate_pool: int = 0
    candidates: int = 0
    skipped_by_size: int = 0
    skipped_by_count: int = 0
    processed: int = 0
    cache_hits: int = 0
    cached_errors: int = 0
    containers_complete: int = 0
    containers_partial: int = 0
    errors: int = 0
    members_seen: int = 0
    members_indexed: int = 0
    metadata_only: int = 0
    nested_archives: int = 0
    text_chars: int = 0
    safety_issues: int = 0
    cache_containers_pruned: int = 0
    cache_members_pruned: int = 0
    peak_reserved_bytes: int = 0
    memory_waits: int = 0
    processing_signature: str | None = None
    processing_provenance: dict[str, Any] | None = None
    summary_schema: str = ROUTE_SUMMARY_SCHEMA


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "_04_Nucleo_Operativo.archive_models"
del _defined_value


__all__ = ("ArchiveRouteSummary",)
