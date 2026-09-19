"""Rich terminal presentation for normalized progress events."""

from __future__ import annotations

import os
import sys
from threading import RLock

from rich.console import Console
from rich.progress import (
    BarColumn,
    Column,
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

from .events import ProgressEvent, ProgressMetric


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


def _terminal_dimensions() -> tuple[int, int] | None:
    """Return the live stderr PTY size, ignoring stale ``COLUMNS``/``LINES``.

    Rich normally gives the environment variables precedence over the terminal
    ioctl.  A stale ``COLUMNS`` value is common when a command is launched from
    a wrapper or an embedded terminal, and makes Live compute a wider table
    than the PTY can actually render.  That is precisely the condition in
    which a changing description can wrap and leave old rows behind.  Only the
    interactive default console uses this probe; pipes and caller-supplied
    consoles retain Rich's existing non-interactive behavior.
    """

    stream = sys.stderr
    try:
        if not stream.isatty():
            return None
        size = os.get_terminal_size(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return None
    columns, lines = int(size.columns), int(size.lines)
    if columns <= 0 or lines <= 0:
        return None
    return columns, lines


def _default_console() -> Console:
    """Build the default console without changing pipe/noninteractive output."""

    dimensions = _terminal_dimensions()
    if dimensions is None:
        return Console(stderr=True)
    columns, lines = dimensions
    return Console(stderr=True, width=columns, height=lines)


class _MetricsColumn(ProgressColumn):
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


class RichProgress:
    """Render all framework events with one stable visual convention."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        transient: bool = False,
        refresh_per_second: float = 10.0,
    ) -> None:
        self._console = console if console is not None else _default_console()
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn(
                "[progress.description]{task.description}",
                table_column=Column(no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn(
                "{task.fields[unit]}",
                table_column=Column(no_wrap=True, overflow="ellipsis"),
            ),
            _MetricsColumn(table_column=Column(no_wrap=True, overflow="ellipsis")),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self._console,
            transient=transient,
            refresh_per_second=refresh_per_second,
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
                terminal_status = next(
                    (
                        metric.value
                        for metric in event.metrics
                        if metric.name == "status"
                    ),
                    None,
                )
                unknown_terminal = event.total is None and terminal_status in {
                    "failed",
                    "cancelled",
                    "partial",
                    "incomplete",
                }
                if unknown_terminal:
                    # A failed or cancelled route deliberately emits a
                    # terminal event without a total.  Do not manufacture a
                    # completed total here: doing so makes Rich render a
                    # failed partial operation as ``N/N`` (and therefore as
                    # complete), which contradicts the event's outcome.
                    self._progress.update(
                        task_id,
                        completed=event.completed,
                        refresh=False,
                    )
                    # ``Progress.update(total=None)`` means "leave the
                    # existing total unchanged" in Rich, not "clear it".
                    # Clear the public Task field explicitly before the
                    # terminal refresh so a prior known total cannot imply
                    # complete work after a failure/cancellation.
                    task = next(task for task in self._progress.tasks if task.id == task_id)
                    task.total = None
                    self._progress.refresh()
                else:
                    # Successful events with no declared total retain the
                    # historical terminal convention: the observed completed
                    # count becomes the explicit total, whereas failed or
                    # cancelled events above remain visibly indeterminate.
                    terminal_total = event.completed if event.total is None else event.total
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
