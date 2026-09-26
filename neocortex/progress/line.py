"""Coalesced machine-readable progress stream."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock

from .events import ProgressEvent


@dataclass(slots=True)
class _LineProgressState:
    started: float
    started_completed: int
    emitted_at: float
    completed: int
    total: int | None
    description: str
    status: object
    errors: object
    fraction: float | None


def _event_fraction(event: ProgressEvent) -> float | None:
    if event.total is None or event.total == 0:
        return None
    return event.completed / event.total


def _new_state(
    event: ProgressEvent,
    metrics: Mapping[str, int | str],
    *,
    now: float,
    fraction: float | None,
) -> _LineProgressState:
    return _LineProgressState(
        started=now,
        started_completed=event.completed,
        emitted_at=now,
        completed=event.completed,
        total=event.total,
        description=event.description,
        status=metrics.get("status"),
        errors=metrics.get("errors"),
        fraction=fraction,
    )


def _should_emit(
    state: _LineProgressState,
    event: ProgressEvent,
    metrics: Mapping[str, int | str],
    *,
    now: float,
    fraction: float | None,
    item_interval: int,
    fraction_interval: float,
    time_interval_seconds: float,
) -> bool:
    fraction_advanced = (
        fraction is not None
        and state.fraction is not None
        and fraction - state.fraction >= fraction_interval
    )
    return bool(
        event.finished
        # ProgressEvent carries absolute counters.  A retry/replay may move
        # the counter backwards or change its total without changing the
        # description; suppressing that update leaves a stale machine stream
        # and can make a failed/restarted phase look complete.
        or event.completed < state.completed
        or event.total != state.total
        or event.description != state.description
        or metrics.get("status") != state.status
        or metrics.get("errors") != state.errors
        or event.completed - state.completed >= item_interval
        or fraction_advanced
        or now - state.emitted_at >= time_interval_seconds
    )


def _refresh_state(
    state: _LineProgressState,
    event: ProgressEvent,
    metrics: Mapping[str, int | str],
    *,
    now: float,
    fraction: float | None,
) -> None:
    state.emitted_at = now
    state.completed = event.completed
    state.total = event.total
    state.description = event.description
    state.status = metrics.get("status")
    state.errors = metrics.get("errors")
    state.fraction = fraction


def _rate_and_eta(
    state: _LineProgressState,
    event: ProgressEvent,
    *,
    elapsed: float,
) -> tuple[float | None, float | None]:
    completed_delta = event.completed - state.started_completed
    rate = completed_delta / elapsed if elapsed > 0 and completed_delta > 0 else None
    eta = None
    if rate is not None and event.total is not None and event.total >= event.completed:
        eta = (event.total - event.completed) / rate
    return rate, eta


def _line_payload(
    event: ProgressEvent,
    metrics: Mapping[str, int | str],
    *,
    elapsed: float,
    rate: float | None,
    eta: float | None,
) -> dict[str, object]:
    return {
        "completed": event.completed,
        "description": event.description,
        "elapsed_seconds": round(elapsed, 3),
        "eta_seconds": None if eta is None else round(eta, 3),
        "finished": event.finished,
        "metrics": dict(metrics),
        "operation": event.operation,
        "phase": event.phase,
        "rate_per_second": None if rate is None else round(rate, 3),
        "total": event.total,
        "unit": event.unit,
    }


class LineProgress:
    """Emit coalesced, machine-readable progress without hiding terminals."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        item_interval: int = 25,
        fraction_interval: float = 0.05,
        time_interval_seconds: float = 5.0,
    ) -> None:
        self._lock = RLock()
        self._clock = clock
        self._item_interval = item_interval
        self._fraction_interval = fraction_interval
        self._time_interval_seconds = time_interval_seconds
        self._states: dict[tuple[str, str], _LineProgressState] = {}

    def __call__(self, event: ProgressEvent) -> None:
        now = self._clock()
        metrics = {metric.name: metric.value for metric in event.metrics}
        fraction = _event_fraction(event)
        with self._lock:
            state = self._states.get(event.key)
            if state is None:
                state = _new_state(event, metrics, now=now, fraction=fraction)
                self._states[event.key] = state
            elif not _should_emit(
                state,
                event,
                metrics,
                now=now,
                fraction=fraction,
                item_interval=self._item_interval,
                fraction_interval=self._fraction_interval,
                time_interval_seconds=self._time_interval_seconds,
            ):
                return
            else:
                _refresh_state(state, event, metrics, now=now, fraction=fraction)
            elapsed = max(0.0, now - state.started)
            rate, eta = _rate_and_eta(state, event, elapsed=elapsed)
            if event.finished:
                self._states.pop(event.key, None)
            payload = _line_payload(event, metrics, elapsed=elapsed, rate=rate, eta=eta)
            print(
                "NEOCORTEX_PROGRESS "
                + json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                file=sys.stderr,
                flush=True,
            )

    def __enter__(self) -> "LineProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None
