"""Uniform, bounded replay counters for content-route summaries.

Route implementations historically used ``processed`` with two different
meanings: some counted every selected candidate, including cache hits, while
others counted only work that crossed the extraction/analyse boundary.  Keep
those owner-local counters intact for compatibility, but publish one normalized
view at the orchestration boundary so status and human summaries do not infer
replay from an ambiguous subtraction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass


_CAPABILITIES = frozenset({"phase_resume", "safe_replay", "not_resumable"})

# These routes count a cached error as ``processed`` but do not include it in
# ``cache_hits``.  It is still reused work, not new extraction work.
_CACHED_ERROR_NOT_IN_CACHE_HITS = frozenset({"docx", "image"})

# Text and Code already count only cache-miss work in ``processed``.  The other
# built-ins count selected candidates and expose cache hits as a subset.
_PROCESSED_IS_NEW_WORK = frozenset({"text", "code"})


def _counter(value: object) -> int:
    """Return a bounded non-negative integer for a route counter."""

    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_route_replay_metrics(
    route_name: str,
    summary: Mapping[str, object],
    *,
    replayability: str = "safe_replay",
) -> dict[str, object]:
    """Add canonical replay counters without changing owner-local semantics.

    ``new_work`` means selected candidates that were not satisfied by a
    reusable route result, including retries. ``cached_errors`` are reused
    observations even when a legacy route does not count them in
    ``cache_hits``. ``replay_status`` is deliberately conservative: a route
    with fewer reusable observations than candidates is never reported as a
    full replay.
    """

    if not isinstance(summary, Mapping):
        raise TypeError("route summary must be a mapping")
    result = dict(summary)
    candidates = _counter(result.get("candidates"))
    processed = _counter(result.get("processed"))
    cache_hits = _counter(result.get("cache_hits"))
    cached_errors = _counter(result.get("cached_errors"))

    explicit_new_work = result.get("new_work")
    if isinstance(explicit_new_work, int) and not isinstance(explicit_new_work, bool):
        new_work = max(0, explicit_new_work)
    elif route_name in _PROCESSED_IS_NEW_WORK:
        new_work = processed
    elif route_name in _CACHED_ERROR_NOT_IN_CACHE_HITS:
        new_work = max(0, processed - cache_hits - cached_errors)
    else:
        new_work = max(0, processed - cache_hits)

    if replayability not in _CAPABILITIES:
        replayability = "not_resumable"
    reused = cache_hits + cached_errors
    if candidates == 0:
        replay_status = "unobserved"
    elif new_work == 0 and reused >= candidates:
        replay_status = "replayed"
    elif new_work == 0:
        replay_status = "partial_reuse"
    else:
        replay_status = "mixed"

    result.update(
        {
            "candidates": candidates,
            "processed": processed,
            "cache_hits": cache_hits,
            "cached_errors": cached_errors,
            "new_work": new_work,
            "replayability": replayability,
            "replay_status": replay_status,
        }
    )
    return result


def route_replay_metrics(
    route_name: str,
    summary: object,
    *,
    replayability: str = "safe_replay",
) -> dict[str, object]:
    """Normalize a dataclass or mapping returned by a route."""

    if isinstance(summary, Mapping):
        mapping = summary
    elif is_dataclass(summary) and not isinstance(summary, type):
        mapping = asdict(summary)
    else:
        mapping = {
            name: getattr(summary, name)
            for name in (
                "candidates",
                "processed",
                "cache_hits",
                "cached_errors",
                "new_work",
            )
            if hasattr(summary, name)
        }
    return normalize_route_replay_metrics(
        route_name,
        mapping,
        replayability=replayability,
    )


__all__ = ["normalize_route_replay_metrics", "route_replay_metrics"]
