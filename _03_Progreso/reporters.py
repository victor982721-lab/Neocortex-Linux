"""Normalized Rich and headless progress reporters."""
# region [00] Contexto del módulo
# Módulo: _03_Progreso/reporters.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

from .models import ProgressEvent, ProgressMetric
# endregion [01]

# region [02] Implementación


_METRIC_PRESENTATION = {
    "cache_hits": ("caché", "cyan"),
    "feature_cache_hits": ("caché-caract.", "bright_cyan"),
    "cached_errors": ("errores-caché", "yellow"),
    "new_work": ("nuevos", "magenta"),
    "cache_refreshes": ("actualiz.-caché", "bright_magenta"),
    "reclassified": ("reclasif.", "magenta"),
    "retries": ("reintentos", "yellow"),
    "retry_pages": ("pág-reintento", "yellow"),
    "page_progress": ("páginas", "blue"),
    "completed_work": ("hechos", "green"),
    "classified": ("clasificados", "green"),
    "planned": ("planeados", "green"),
    "applied": ("aplicados", "green"),
    "cache_synced": ("caché-sinc.", "cyan"),
    "review": ("revisión", "yellow"),
    "blocked": ("bloqueados", "bold red"),
    "stale": ("obsoletos", "yellow"),
    "already_organized": ("ya-organizados", "cyan"),
    "errors": ("errores", "bold red"),
    "timeouts": ("timeouts", "bold red"),
    "recycled": ("reciclados", "bold yellow"),
    "partial": ("parciales", "yellow"),
    "protected": ("protegidos", "yellow"),
    "ocr_attempts": ("OCR", "blue"),
    "in_flight": ("en-curso", "bright_white"),
    "active_work": ("activos", "bright_white"),
    "queued_work": ("cola", "yellow"),
    "remaining": ("faltan", "white"),
    "memory_waits": ("esperas", "yellow"),
    "sources": ("fuentes", "cyan"),
    "chunks": ("fragmentos", "blue"),
    "new_jobs": ("trabajo-nuevo", "magenta"),
    "reused": ("reutilizados", "cyan"),
    "embedded": ("vectores", "green"),
    "generation": ("generación", "bright_black"),
    "status": ("estado", "white"),
}

_ZERO_VISIBLE_METRICS = {
    "cache_hits",
    "new_work",
    "new_jobs",
    "cache_refreshes",
    "retries",
    "errors",
    "in_flight",
    "active_work",
    "queued_work",
    "remaining",
}


class _MetricsColumn(ProgressColumn):
    """Render structured counters without making routes format terminal text."""

    def render(self, task: Task) -> Text:
        metrics = task.fields.get("metrics", ())
        output = Text(no_wrap=True, overflow="ellipsis")
        rendered = 0
        for metric in metrics:
            if not isinstance(metric, ProgressMetric):
                continue
            if metric.value == 0 and metric.name not in _ZERO_VISIBLE_METRICS:
                continue
            if rendered:
                output.append(" · ", style="bright_black")
            label, style = _METRIC_PRESENTATION.get(
                metric.name,
                (metric.name.replace("_", "-"), "white"),
            )
            output.append(f"{label} ", style="bright_black")
            output.append(str(metric.value), style=style)
            rendered += 1
        return output


class NullProgress:
    """Drop-in reporter for services, tests, or externally managed UIs."""

    def __call__(self, event: ProgressEvent) -> None:
        return None

    def __enter__(self) -> "NullProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


@dataclass(slots=True)
class _LineProgressState:
    started: float
    started_completed: int
    emitted_at: float
    completed: int
    description: str
    status: object
    errors: object
    fraction: float | None


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
        status = metrics.get("status")
        errors = metrics.get("errors")
        with self._lock:
            state = self._states.get(event.key)
            if state is None:
                fraction = (
                    None
                    if event.total is None or event.total == 0
                    else event.completed / event.total
                )
                state = _LineProgressState(
                    now,
                    event.completed,
                    now,
                    event.completed,
                    event.description,
                    status,
                    errors,
                    fraction,
                )
                self._states[event.key] = state
                emit = True
            else:
                previous_fraction = state.fraction
                fraction = (
                    None
                    if event.total is None or event.total == 0
                    else event.completed / event.total
                )
                emit = bool(
                    event.finished
                    or event.description != state.description
                    or status != state.status
                    or errors != state.errors
                    or event.completed - state.completed >= self._item_interval
                    or (
                        fraction is not None
                        and isinstance(previous_fraction, float)
                        and fraction - previous_fraction >= self._fraction_interval
                    )
                    or now - state.emitted_at >= self._time_interval_seconds
                )
                if not emit:
                    return
                state.emitted_at = now
                state.completed = event.completed
                state.description = event.description
                state.status = status
                state.errors = errors
                state.fraction = fraction
            elapsed = max(0.0, now - state.started)
            completed_delta = event.completed - state.started_completed
            rate = completed_delta / elapsed if elapsed > 0 and completed_delta > 0 else None
            eta = (
                (event.total - event.completed) / rate
                if rate is not None and event.total is not None and event.total >= event.completed
                else None
            )
            if event.finished:
                # A later operation may legitimately reuse the same
                # operation/phase key.  Its first event must not inherit the
                # terminal coalescing state from this run.
                self._states.pop(event.key, None)
        payload = {
            "completed": event.completed,
            "description": event.description,
            "elapsed_seconds": round(elapsed, 3),
            "eta_seconds": None if eta is None else round(eta, 3),
            "finished": event.finished,
            "metrics": metrics,
            "operation": event.operation,
            "phase": event.phase,
            "rate_per_second": None if rate is None else round(rate, 3),
            "total": event.total,
            "unit": event.unit,
        }
        with self._lock:
            print(
                "NEOCORTEX_PROGRESS "
                + json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )

    def __enter__(self) -> "LineProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class RichProgress:
    """Render all framework events with one stable visual convention."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        transient: bool = False,
        refresh_per_second: float = 10.0,
    ):
        self._console = console or Console(stderr=True)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[unit]}"),
            _MetricsColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=transient,
            refresh_per_second=refresh_per_second,
            expand=True,
        )
        self._tasks: dict[tuple[str, str], TaskID] = {}
        self._lock = RLock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._progress.start()
                self._started = True

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._progress.stop()
                self._started = False

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if not self._started:
                self.start()
            task_id = self._tasks.get(event.key)
            if task_id is None:
                task_id = self._progress.add_task(
                    event.description,
                    total=event.total,
                    completed=event.completed,
                    unit=event.unit,
                    metrics=event.metrics,
                )
                self._tasks[event.key] = task_id
            else:
                self._progress.update(
                    task_id,
                    description=event.description,
                    completed=event.completed,
                    total=event.total,
                    unit=event.unit,
                    metrics=event.metrics,
                    refresh=False,
                )
            if event.finished:
                terminal_total = event.total if event.total is not None else event.completed
                self._progress.update(
                    task_id,
                    completed=terminal_total,
                    total=terminal_total,
                    refresh=True,
                )
                self._progress.stop_task(task_id)

    def __enter__(self) -> "RichProgress":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


class RecordingProgress:
    """In-memory reporter intended for deterministic integration tests."""

    def __init__(self):
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def __enter__(self) -> "RecordingProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


# endregion [02]
