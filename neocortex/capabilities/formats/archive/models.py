"""Stable result contracts for bounded archive indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from neocortex.foundation.processing_provenance import ROUTE_SUMMARY_SCHEMA


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
    fts_rows_repaired: int = 0
    # Catalog counters are appended and keyword-only to preserve the stable
    # positional summary contract while exposing the post-route projection.
    catalog_candidates: int = field(default=0, kw_only=True)
    catalog_classified: int = field(default=0, kw_only=True)
    catalog_cache_hits: int = field(default=0, kw_only=True)
    catalog_review_required: int = field(default=0, kw_only=True)
    catalog_errors: int = field(default=0, kw_only=True)
    catalog_source_stale: int = field(default=0, kw_only=True)
    catalog_stale_marked: int = field(default=0, kw_only=True)
    catalog_source_missing: int = field(default=0, kw_only=True)
    catalog_complete: bool | None = field(default=None, kw_only=True)
    # Physical archive materialization is an explicit apply-only extension.
    # These keyword-only counters preserve the historical positional summary
    # contract while making the separate no-replace stage observable.
    materialization_applied: int = field(default=0, kw_only=True)
    materialization_reused: int = field(default=0, kw_only=True)
    materialization_pending: int = field(default=0, kw_only=True)
    materialization_collisions: int = field(default=0, kw_only=True)
    materialization_units_preserved: int = field(default=0, kw_only=True)
    materialization_manifest_digest: str | None = field(default=None, kw_only=True)


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        _defined_value.__module__ = "neocortex.capabilities.formats.archive.models"
del _defined_value


__all__ = ("ArchiveRouteSummary",)
