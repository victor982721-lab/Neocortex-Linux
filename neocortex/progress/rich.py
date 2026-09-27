"""Adaptive terminal presentation for normalized progress events."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from threading import RLock

from rich.cells import cell_len
from rich.console import Console, ConsoleDimensions
from rich.progress import Column, Progress, ProgressColumn, Task, TaskID
from rich.table import Table
from rich.text import Text

from .events import ProgressEvent, ProgressMetric


_METRIC_PRESENTATION = {
    "cache_hits": ("caché", "cyan"),
    "feature_cache_hits": ("caché caract.", "bright_cyan"),
    "cached_errors": ("errores caché", "yellow"),
    "new_work": ("trabajo nuevo", "magenta"),
    "cache_refreshes": ("actualiz. caché", "bright_magenta"),
    "reclassified": ("reclasif.", "magenta"),
    "retries": ("reintentos", "yellow"),
    "retry_pages": ("pág. reintento", "yellow"),
    "page_progress": ("páginas", "blue"),
    "completed_work": ("hechos", "green"),
    "classified": ("clasificados", "green"),
    "planned": ("planeados", "green"),
    "applied": ("aplicados", "green"),
    "cache_synced": ("caché sinc.", "cyan"),
    "review": ("revisión", "yellow"),
    "blocked": ("bloqueados", "bold red"),
    "stale": ("obsoletos", "yellow"),
    "already_organized": ("ya organizados", "cyan"),
    "errors": ("errores", "bold red"),
    "timeouts": ("timeouts", "bold red"),
    "recycled": ("reciclados", "bold yellow"),
    "partial": ("parciales", "yellow"),
    "protected": ("protegidos", "yellow"),
    "ocr_attempts": ("OCR", "blue"),
    "in_flight": ("en curso", "bright_white"),
    "active_work": ("activos", "bright_white"),
    "queued_work": ("cola", "yellow"),
    "remaining": ("faltan", "white"),
    "memory_waits": ("esperas", "yellow"),
    "sources": ("fuentes", "cyan"),
    "chunks": ("fragmentos", "blue"),
    "new_jobs": ("trabajos nuevos", "magenta"),
    "reused": ("reutilizados", "cyan"),
    "embedded": ("vectores", "green"),
    "generation": ("generación", "bright_black"),
    "status": ("estado", "white"),
}

_COMPACT_METRIC_LABELS = {
    "new_work": "nuevo",
    "new_jobs": "trabajos",
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

_CORE_METRIC_ORDER = (
    "cache_hits",
    "feature_cache_hits",
    "cached_errors",
    "cache_refreshes",
    "cache_synced",
    "reused",
    "new_work",
    "new_jobs",
    "errors",
    "timeouts",
    "memory_waits",
)

# Narrow terminals retain the exact progress count and state first. When space
# is available, errors and waits precede the cache/new-work counters; the final
# row is then assembled in the stable presentation order above.
_METRIC_SELECTION_PRIORITY = (
    "errors",
    "timeouts",
    "memory_waits",
    "new_work",
    "new_jobs",
    "cache_hits",
    "feature_cache_hits",
    "cached_errors",
    "cache_refreshes",
    "cache_synced",
    "reused",
)

_STATUS_LABELS = {
    "running": "en curso",
    "complete": "completo",
    "completed": "completo",
    "success": "correcto",
    "failed": "fallido",
    "cancelled": "cancelado",
    "canceled": "cancelado",
    "partial": "parcial",
    "incomplete": "incompleto",
    "blocked": "bloqueado",
    "unavailable": "no disponible",
    "paused": "en pausa",
    "pending": "pendiente",
    "skipped": "omitido",
}

_ROUTE_LABELS = {
    "archive": "ARCHIVE",
    "audio": "AUDIO",
    "docx": "DOCX",
    "image": "IMAGEN",
    "office": "OFFICE",
    "pdf": "PDF",
    "text": "TEXTO",
    "video": "VIDEO",
    "zip-intake": "ZIP",
}

_GROUP_LABELS = (
    "Preparación",
    "Inventario y validación",
    "Procesamiento por rutas",
    "Catálogos y Semantic",
    "Cierre",
)

_COMPACT_UNITS = {
    "archivos": "arch.",
    "directorios": "dirs.",
    "documentos": "docs",
    "elementos": "elem.",
    "ejecución": "ejec.",
    "imágenes": "imgs",
    "operaciones": "ops.",
    "páginas": "pág.",
    "trabajos": "trab.",
}


def _terminal_dimensions() -> tuple[int, int] | None:
    """Read the current stderr PTY size, ignoring stale ``COLUMNS``/``LINES``."""

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


class _DynamicTerminalConsole(Console):
    """Keep Rich's layout in step with a PTY resize while ignoring stale env."""

    def __init__(self) -> None:
        super().__init__(stderr=True)
        # Rich snapshots COLUMNS/LINES into these fields during construction.
        # Interactive sizing comes from the PTY instead, including fallback.
        self._width = None
        self._height = None
        self._environ = self._environ.copy()
        self._environ.pop("COLUMNS", None)
        self._environ.pop("LINES", None)

    @property
    def size(self) -> ConsoleDimensions:
        dimensions = _terminal_dimensions()
        if dimensions is None:
            return super().size
        columns, lines = dimensions
        return ConsoleDimensions(columns, lines)


def _default_console() -> Console:
    """Use live PTY dimensions interactively and Rich defaults for pipes."""

    if _terminal_dimensions() is None:
        return Console(stderr=True)
    # Leave width/height unset. _DynamicTerminalConsole re-reads the PTY for
    # each layout, rather than pinning the dimensions observed at construction.
    return _DynamicTerminalConsole()


def _group_index(operation: str, phase: str) -> int:
    if operation == "framework" and phase == "prepare":
        return 0
    if operation == "dedup":
        return 1
    if operation == "framework" and phase in {
        "content-types",
        "duplicates",
        "zip-intake-reconciliation",
    }:
        return 1
    if operation in _ROUTE_LABELS:
        return 2
    if operation == "semantic" or operation.startswith("catalog-"):
        return 3
    return 4


def _group_label(operation: str, phase: str) -> str:
    return _GROUP_LABELS[_group_index(operation, phase)]


def _task_label(operation: str, phase: str) -> str:
    if operation in _ROUTE_LABELS:
        return _ROUTE_LABELS[operation]
    if operation == "framework":
        return {
            "prepare": "EJECUCIÓN",
            "content-types": "TIPOS",
            "duplicates": "DUPLICADOS",
            "empty-directories": "DIR. VACÍOS",
            "empty-files": "ARCHIVOS VACÍOS",
            "redlist": "REDLIST",
            "result": "RESULTADO",
            "complete": "ETAPA",
            "zip-effects": "ZIP",
            "zip-intake-reconciliation": "ZIP",
        }.get(phase, "MARCO")
    if operation == "dedup":
        return "INVENTARIO" if phase == "inventory" else "DUPLICADOS"
    if operation == "semantic":
        return "SEMANTIC"
    if operation.startswith("catalog-"):
        source = operation.removeprefix("catalog-")
        return f"CAT. {source.upper()}"
    if operation.startswith("organization"):
        return "ORGANIZACIÓN"
    return operation.upper() or "TAREA"


def _format_count(value: float | int) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds_part:02d}"
    return f"{minutes:02d}:{seconds_part:02d}"


def _metric_values(task: Task) -> dict[str, int | str]:
    metrics = task.fields.get("metrics", ())
    return {
        metric.name: metric.value
        for metric in metrics
        if isinstance(metric, ProgressMetric)
    }


def _metric_text(name: str, value: int | str, *, compact: bool = False) -> tuple[str, str]:
    default_label, style = _METRIC_PRESENTATION.get(
        name,
        (name.replace("_", "-"), "white"),
    )
    label = _COMPACT_METRIC_LABELS.get(name, default_label) if compact else default_label
    return f"{label} {value}", style


def _status_value(task: Task, metrics: dict[str, int | str]) -> str:
    raw_status = metrics.get("status")
    if raw_status is None:
        return "finalizado" if task.fields.get("event_finished", False) else "en curso"
    status = str(raw_status)
    return _STATUS_LABELS.get(status.lower(), status)


def _progress_text(task: Task, width: int) -> str:
    completed = _format_count(task.completed)
    total = "?" if task.total is None else _format_count(task.total)
    count = f"{completed}/{total}"
    unit = str(task.fields.get("unit", "elementos"))
    if width < 90 or task.total is None:
        return f"{count} {unit}"
    if task.total <= 0:
        percentage = 100 if task.fields.get("event_finished", False) else 0
    else:
        percentage = min(100, max(0, round(task.completed / task.total * 100)))
    if width < 100:
        return f"{percentage}% {count} {unit}"
    return f"avance {percentage}% · {count} {unit}"


class _TaskLineColumn(ProgressColumn):
    """One width-aware row with exact counts/state and prioritized metrics."""

    def __init__(self, console: Console) -> None:
        super().__init__(table_column=Column(no_wrap=True, overflow="crop", ratio=1))
        self._console = console

    @staticmethod
    def _segments(
        *,
        prefix: str | None,
        progress: str,
        selected_metrics: set[str],
        metrics: dict[str, int | str],
        timing: str | None,
        status: str,
        status_label: bool = True,
        description: str | None = None,
        secondary: tuple[str, ...] = (),
        compact_labels: bool = False,
    ) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        if prefix:
            segments.append((prefix, "bold cyan"))
        if description:
            segments.append((description, "white"))
        segments.append((progress, "bright_white"))
        for name in _CORE_METRIC_ORDER:
            if name in selected_metrics and name in metrics:
                segment, style = _metric_text(name, metrics[name], compact=compact_labels)
                segments.append((segment, style))
        for name in secondary:
            if name in metrics:
                segment, style = _metric_text(name, metrics[name], compact=compact_labels)
                segments.append((segment, style))
        if timing:
            segments.append((timing, "bright_black"))
        state_text = f"estado {status}" if status_label else status
        segments.append((state_text, "white"))
        return segments

    @staticmethod
    def _line_width(segments: list[tuple[str, str]]) -> int:
        return sum(cell_len(value) for value, _ in segments) + max(0, len(segments) - 1) * 3

    @staticmethod
    def _render_segments(segments: list[tuple[str, str]]) -> Text:
        result = Text(no_wrap=True, overflow="crop")
        for index, (value, style) in enumerate(segments):
            if index:
                result.append(" · ", style="bright_black")
            result.append(value, style=style)
        return result

    def render(self, task: Task) -> Text:
        width = max(1, self._console.width)
        operation = str(task.fields.get("operation", ""))
        phase = str(task.fields.get("phase", ""))
        prefix: str | None = _task_label(operation, phase)
        progress = _progress_text(task, width)
        metrics = _metric_values(task)
        status = _status_value(task, metrics)
        unit = str(task.fields.get("unit", "elementos"))
        if prefix and prefix.casefold() == unit.casefold():
            prefix = None
        selected_metrics: set[str] = set()
        secondary: list[str] = []
        timing: str | None = None
        description: str | None = None
        status_label = True

        def assemble() -> list[tuple[str, str]]:
            return self._segments(
                prefix=prefix,
                description=description,
                progress=progress,
                selected_metrics=selected_metrics,
                metrics=metrics,
                timing=timing,
                status=status,
                status_label=status_label,
                secondary=tuple(secondary),
                compact_labels=width < 90,
            )

        # Keep the exact count/unit and status together before admitting any
        # optional display field. The route/stage tag is the first field shed.
        segments = assemble()
        if self._line_width(segments) > width:
            prefix = None
            segments = assemble()
        if self._line_width(segments) > width and width < 64:
            progress = _progress_text(task, 40)
            segments = assemble()
        if self._line_width(segments) > width and width < 42:
            unit = str(task.fields.get("unit", "elementos"))
            short_unit = _COMPACT_UNITS.get(unit, unit)
            total = "?" if task.total is None else _format_count(task.total)
            progress = f"{_format_count(task.completed)}/{total} {short_unit}"
            segments = assemble()
        if self._line_width(segments) > width:
            # Very narrow terminals may not fit the "estado" label, but the
            # complete state value remains visible alongside the count.
            status_label = False
            segments = assemble()

        # Select optional core counters by importance, then render them in the
        # fixed cache/new/error/wait order declared above.
        for name in _METRIC_SELECTION_PRIORITY:
            if name not in metrics:
                continue
            selected_metrics.add(name)
            candidate_segments = assemble()
            if self._line_width(candidate_segments) <= width:
                segments = candidate_segments
            else:
                selected_metrics.remove(name)
                break

        # Wider terminals also get elapsed/ETA and secondary typed counters.
        if width >= 100 and task.elapsed is not None:
            timing = f"tiempo {_format_duration(task.elapsed)}"
            remaining = task.time_remaining
            if remaining is not None and not task.fields.get("event_finished", False):
                timing += f" · ETA {_format_duration(remaining)}"
            candidate_segments = assemble()
            if self._line_width(candidate_segments) <= width:
                segments = candidate_segments
            else:
                timing = None

        if width >= 128:
            known = set(_CORE_METRIC_ORDER) | {"status"}
            for name, value in metrics.items():
                if name in known or (value == 0 and name not in _ZERO_VISIBLE_METRICS):
                    continue
                secondary.append(name)
                candidate_segments = assemble()
                if self._line_width(candidate_segments) <= width:
                    segments = candidate_segments
                else:
                    secondary.pop()

        description = task.description.strip()
        if width >= 100 and description:
            description = None
            base_segments = assemble()
            available = width - self._line_width(base_segments) - 3
            if available >= 8:
                description_text = Text(task.description.strip())
                description_text.truncate(min(available, 36), overflow="ellipsis")
                description = description_text.plain
                described_segments = assemble()
                if self._line_width(described_segments) <= width:
                    segments = described_segments
                else:
                    description = None

        return self._render_segments(segments)


class _GroupedProgress(Progress):
    """Sort stable tasks by workflow stage and draw a heading per stage."""

    def make_tasks_table(self, tasks: Iterable[Task]) -> Table:
        visible = sorted(
            (task for task in tasks if task.visible),
            key=lambda task: (
                _group_index(
                    str(task.fields.get("operation", "")),
                    str(task.fields.get("phase", "")),
                ),
                task.id,
            ),
        )
        columns = tuple(
            Column(no_wrap=True, overflow="crop")
            if isinstance(column, str)
            else column.get_table_column().copy()
            for column in self.columns
        )
        table = Table.grid(*columns, padding=(0, 0), expand=self.expand)
        current_group: int | None = None
        for task in visible:
            operation = str(task.fields.get("operation", ""))
            phase = str(task.fields.get("phase", ""))
            group = _group_index(operation, phase)
            if group != current_group:
                heading = Text(
                    f"── {_group_label(operation, phase)} ──",
                    style="bold bright_blue",
                )
                table.add_row(heading)
                current_group = group
            table.add_row(
                *(
                    column.format(task=task) if isinstance(column, str) else column(task)
                    for column in self.columns
                )
            )
        return table


class RichProgress:
    """Render each ``(operation, phase)`` as one adaptive, stable row."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        transient: bool = False,
        refresh_per_second: float = 10.0,
    ) -> None:
        self._console = console if console is not None else _default_console()
        self._progress = _GroupedProgress(
            _TaskLineColumn(self._console),
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
            fields = {
                "unit": event.unit,
                "metrics": event.metrics,
                "operation": event.operation,
                "phase": event.phase,
                "event_finished": event.finished,
            }
            if task_id is None:
                task_id = self._progress.add_task(
                    event.description,
                    total=event.total,
                    completed=event.completed,
                    **fields,
                )
                self._tasks[event.key] = task_id
            else:
                task = next(task for task in self._progress.tasks if task.id == task_id)
                if task.fields.get("event_finished", False) and not event.finished:
                    # A reused stable key starts a fresh clock/counter history
                    # while retaining the same visible task row.
                    self._progress.reset(
                        task_id,
                        start=True,
                        total=event.total,
                        completed=event.completed,
                        description=event.description,
                        **fields,
                    )
                    task.stop_time = None
                    if event.total is None:
                        task.total = None
                else:
                    self._progress.update(
                        task_id,
                        description=event.description,
                        completed=event.completed,
                        total=event.total,
                        refresh=False,
                        **fields,
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
                    # Keep failures/cancellations indeterminate instead of
                    # manufacturing an N/N total that looks successful.
                    self._progress.update(
                        task_id,
                        completed=event.completed,
                        refresh=False,
                    )
                    task = next(task for task in self._progress.tasks if task.id == task_id)
                    task.total = None
                    self._progress.refresh()
                else:
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
