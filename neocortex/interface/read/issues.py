"""Stable projection of route summaries into UI-visible issue counts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# region [01] Route issue projection

# These counters describe disjoint work failures and can therefore be added.
_ADDITIVE_FIELDS = (
    "errors",
    "cached_errors",
    "profile_errors",
    "catalog_errors",
    "catalog_source_stale",
    "catalog_source_missing",
    "safety_issues",
    "protected",
    "retryable_errors",
    "manual_review_errors",
    "source_missing",
)

# These counters are alternate views of partial route coverage.  The same
# degraded document/container may be represented by more than one of them, so
# use the strongest observed count instead of adding the same finding twice.
_PARTIAL_RESULT_FIELDS = (
    "partial_documents",
    "partial",
    "containers_partial",
    "page_errors",
    "document_timeouts",
)


def _nonnegative_counter(summary: Mapping[str, Any], field: str) -> int:
    value = summary.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"route summary field {field!r} is not a non-negative integer")
    return value


def route_summary_mapping(summary: object) -> Mapping[str, Any]:
    """Expose either a persisted mapping or a typed route summary uniformly."""

    if isinstance(summary, Mapping):
        return summary
    return {
        field: getattr(summary, field, 0) for field in (*_ADDITIVE_FIELDS, *_PARTIAL_RESULT_FIELDS)
    }


def route_issue_count(summary: object | None, *, failed: bool = False) -> int:
    """Count visible route issues without double-counting one failed route."""

    if failed:
        return 1
    if summary is None:
        return 0
    values = route_summary_mapping(summary)
    additive = sum(_nonnegative_counter(values, field) for field in _ADDITIVE_FIELDS)
    partial = max(
        (_nonnegative_counter(values, field) for field in _PARTIAL_RESULT_FIELDS),
        default=0,
    )
    return additive + partial


# endregion [01]
