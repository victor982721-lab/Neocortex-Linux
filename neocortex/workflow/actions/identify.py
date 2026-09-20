"""Bounded, side-effect-free content observations for Framework Identify.

The action owner keeps SQLite, policy, normalization, and route publication on
its calling thread.  This module contains only the small DTO and the worker
operation used for a cache-miss observation.  A worker may stat and read one
candidate, but it never receives a FrameworkState or any other persistence
owner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.platform.content_types import DetectedType
from neocortex.runtime.control.cancellation import CancellationRequested


@dataclass(frozen=True, slots=True)
class ContentObservation:
    """One identity-bound detector result produced outside the SQLite owner."""

    snapshot: FileSnapshot
    detected: DetectedType | None = None
    stale: bool = False
    error: Exception | None = None


def _same_snapshot(planned: FileSnapshot, current: FileSnapshot) -> bool:
    return (
        planned.identity == current.identity
        and planned.size == current.size
        and planned.mtime_ns == current.mtime_ns
        and planned.birthtime_ns == current.birthtime_ns
    )


def observe_content_type(
    snapshot: FileSnapshot,
    detector: Callable[[str], DetectedType | None],
    snapshotter: Callable[[str], FileSnapshot] | None = None,
    *,
    prevalidated: bool = False,
) -> ContentObservation:
    """Observe a cache miss while preserving the existing TOCTOU fence.

    The detector is deliberately injected by the action owner.  Besides
    keeping this worker module independent from the action coordinator, that
    preserves the public detector seam used by focused tests and makes it
    explicit that the worker performs no state access.  The integrated action
    owner passes ``prevalidated=True`` because its owner-side admission stat
    already supplies the before-read fence; the worker still verifies the
    post-read identity before returning.
    """

    capture = snapshot_path if snapshotter is None else snapshotter
    if not prevalidated:
        try:
            before = capture(snapshot.path)
        except FileNotFoundError:
            return ContentObservation(snapshot, stale=True)
        except OSError as exc:
            return ContentObservation(snapshot, error=exc)
        if not _same_snapshot(snapshot, before):
            return ContentObservation(snapshot, stale=True)

    try:
        detected = detector(snapshot.path)
    except FileNotFoundError:
        return ContentObservation(snapshot, stale=True)
    except CancellationRequested:
        raise
    except Exception as exc:  # a detector failure is owner-visible, not a worker crash
        return ContentObservation(snapshot, error=exc)

    try:
        after = capture(snapshot.path)
    except FileNotFoundError:
        return ContentObservation(snapshot, stale=True)
    except OSError as exc:
        return ContentObservation(snapshot, error=exc)
    if not _same_snapshot(snapshot, after):
        return ContentObservation(snapshot, stale=True)
    return ContentObservation(snapshot, detected=detected)


__all__ = ["ContentObservation", "observe_content_type"]
