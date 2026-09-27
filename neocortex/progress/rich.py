"""Aligned terminal tables projected from typed progress events."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from threading import RLock

from rich import box
from rich.cells import cell_len
from rich.console import Console, ConsoleDimensions, RenderableType
from rich.padding import Padding
from rich.progress import Progress, Task, TaskID
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
    "atomic": ("ZIP atómicos", "white"),
    "generic": ("ZIP genéricos", "white"),
    "frames": ("fotogramas", "white"),
    "ocr_positive": ("OCR positivo", "white"),
    "transcript_chars": ("caracteres", "white"),
}

_STATUS_LABELS = {
    "running": "En curso",
    "complete": "Completo",
    "completed": "Completo",
    "success": "Correcto",
    "ok": "Completo",
    "error": "Incompleto",
    "interrumpido": "Interrumpido",
    "failed": "Fallido",
    "cancelled": "Cancelado",
    "canceled": "Cancelado",
    "partial": "Parcial",
    "incomplete": "Incompleto",
    "blocked": "Bloqueado",
    "unavailable": "No disponible",
    "paused": "En pausa",
    "pending": "Pendiente",
    "skipped": "Omitido",
}

_ROUTE_LABELS = {
    "pdf": "PDF",
    "docx": "DOCX",
    "office": "Office",
    "archive": "Archive",
    "zip-intake": "ZIP",
    "text": "Texto",
    "audio": "Audio",
    "video": "Video",
    "image": "Imagen",
}
_ROUTE_ORDER = {name: index for index, name in enumerate(_ROUTE_LABELS)}
_GROUP_LABELS = (
    "Preparación",
    "Inventario y validación",
    "Procesamiento por rutas",
    "Catálogos y Semantic",
    "Cierre",
)
_FIELD_LABELS = {
    "label": "Tarea",
    "advance": "Avance",
    "unit": "Unidad",
    "cache": "Caché",
    "new": "Nuevo",
    "errors": "Errores",
    "waits": "Esperas",
    "elapsed": "Tiempo",
    "status": "Estado",
}
_GENERAL_FIELDS = ("label", "advance", "unit", "errors", "elapsed", "status")
_ROUTE_FIELDS = (
    "label", "advance", "unit", "cache", "new", "errors", "waits", "elapsed", "status",
)
# A metric that a producer does not publish stays unknown, rather than becoming
# a made-up zero or a subtraction whose semantics differ between routes.
_METRIC_FIELDS = {
    "cache": "cache_hits",
    "new": "new_work",
    "errors": "errors",
    "waits": "memory_waits",
}
_PRIMARY_METRICS = frozenset((*_METRIC_FIELDS.values(), "status"))
_DROP_PRIORITY = ("elapsed", "waits", "new", "cache", "unit", "errors")


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
    if operation == "dedup" or (
        operation == "framework"
        and phase in {"content-types", "duplicates", "zip-intake-reconciliation"}
    ):
        return 1
    if operation in _ROUTE_LABELS:
        return 2
    if operation == "semantic" or operation.startswith("catalog-"):
        return 3
    return 4


def _task_label(operation: str, phase: str) -> str:
    if operation in _ROUTE_LABELS:
        route = _ROUTE_LABELS[operation]
        suffix = {"fts": "Texto", "profile": "Perfil"}.get(phase)
        return f"{route} / {suffix}" if suffix else route
    if operation == "framework":
        return {
            "prepare": "Ejecución",
            "content-types": "Tipos de contenido",
            "duplicates": "Aplicar duplicados",
            "empty-directories": "Directorios vacíos",
            "empty-files": "Archivos vacíos",
            "redlist": "Redlist",
            "result": "Resultado",
            "complete": "Etapa previa",
            "zip-effects": "Efectos ZIP",
            "zip-intake-reconciliation": "Conciliar inventario",
        }.get(phase, phase.replace("-", " ").capitalize())
    if operation == "dedup":
        return "Inventario" if phase == "inventory" else "Validar duplicados"
    if operation.startswith("catalog-"):
        return f"Catálogo {operation.removeprefix('catalog-').upper()}"
    if operation == "semantic":
        kind, _, scope = phase.partition(":")
        label = {
            "integrated": "Semantic",
            "stage": "Preparar",
            "generation": "Vectores",
            "exact-replay": "Reutilizar",
            "unavailable": "Modelo",
            "blocked": "Modelo",
            "retry": "Reintento",
        }.get(kind, "Semantic")
        return f"{label} {scope.upper()}" if scope and kind != "generation" else label
    if phase == "organization-plan":
        return "Planificar organización"
    if phase == "organization-apply":
        return "Aplicar organización"
    return f"{operation} / {phase}".replace("_", " ").replace("-", " ")


def _task_order(task: Task) -> tuple[int, int, int]:
    operation = str(task.fields.get("operation", ""))
    phase = str(task.fields.get("phase", ""))
    stage_order = {
        ("dedup", "inventory"): 0,
        ("framework", "zip-intake-reconciliation"): 1,
        ("framework", "content-types"): 2,
        ("dedup", "verify"): 3,
        ("framework", "duplicates"): 4,
    }
    group = _group_index(operation, phase)
    rank = _ROUTE_ORDER.get(operation, 99) if group == 2 else stage_order.get(
        (operation, phase), 99,
    )
    return group, rank, task.id


def _format_duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds_part:02d}"
        if hours else f"{minutes:02d}:{seconds_part:02d}"
    )


def _metric_values(task: Task) -> dict[str, int | str]:
    return {
        metric.name: metric.value
        for metric in task.fields.get("metrics", ())
        if isinstance(metric, ProgressMetric)
    }


def _status_value(task: Task, metrics: dict[str, int | str]) -> str:
    raw = metrics.get("status", metrics.get("completion_status"))
    if raw is None:
        return "Finalizado" if task.fields.get("event_finished", False) else "En curso"
    return _STATUS_LABELS.get(str(raw).lower(), str(raw))


def _task_cells(task: Task) -> dict[str, Text]:
    metrics = _metric_values(task)
    completed = task.fields.get("completed_count", int(task.completed))
    total = task.fields.get("total_count", task.total)
    total_text = "?" if total is None else str(int(total))
    status = _status_value(task, metrics)
    status_style = {
        "Fallido": "bold red", "Cancelado": "yellow", "Incompleto": "yellow",
        "Bloqueado": "yellow", "Parcial": "yellow", "Finalizado": "green",
        "Completo": "green",
        "Interrumpido": "yellow",
    }.get(status, "white")
    cells = {
        "label": Text(_task_label(
            str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
        ), style="bold cyan"),
        "advance": Text(f"{completed}/{total_text}"),
        "unit": Text(str(task.fields.get("unit", "elementos"))),
        "elapsed": Text("—", style="dim") if task.elapsed is None
        else Text(_format_duration(task.elapsed)),
        "status": Text(status, style=status_style),
    }
    for field, name in _METRIC_FIELDS.items():
        value = metrics.get(name)
        style = "bold red" if field == "errors" and isinstance(value, int) and value > 0 else ""
        if value is None:
            cells[field] = Text("—", style="dim")
        else:
            cells[field] = Text(str(value), style=style or ("dim" if value == 0 else "white"))
    return cells


class _GroupedProgress(Progress):
    """One common table schema per section, with explicit optional details."""

    def __init__(
        self, *, console: Console, transient: bool = False,
        refresh_per_second: float = 10.0, details: bool = False,
    ) -> None:
        self.details = details
        self.compact = False
        self._displayed_metrics: dict[TaskID, frozenset[str]] = {}
        super().__init__(
            console=console, transient=transient, refresh_per_second=refresh_per_second,
        )

    def make_tasks_table(self, tasks: Iterable[Task]) -> Table:
        visible = sorted((task for task in tasks if task.visible), key=_task_order)
        route_section = any(
            _group_index(str(task.fields.get("operation", "")), str(task.fields.get("phase", "")))
            in {2, 3} for task in visible
        )
        fields = list(_ROUTE_FIELDS if route_section else _GENERAL_FIELDS)
        cells = [_task_cells(task) for task in visible]
        headers = dict(_FIELD_LABELS)
        headers["label"] = "Ruta" if visible and all(
            _group_index(str(task.fields.get("operation", "")), str(task.fields.get("phase", "")))
            == 2 for task in visible
        ) else "Tarea"
        width = max(1, min(self.console.width - 2, 136))
        sizes = {
            field: max(
                cell_len(headers[field]),
                max((row[field].cell_len for row in cells), default=0),
            )
            for field in fields
        }
        # Labels may abbreviate; counters, units and state cells never do.
        label_min = max(cell_len(headers["label"]), min(sizes["label"], 8))

        def required_width() -> int:
            return label_min + sum(sizes[field] for field in fields if field != "label") + (
                2 * (len(fields) - 1)
            )

        for field in _DROP_PRIORITY:
            if required_width() <= width:
                break
            if field in fields:
                fields.remove(field)
        self.compact = self.compact or fields != list(
            _ROUTE_FIELDS if route_section else _GENERAL_FIELDS
        )
        label_budget = width - sum(sizes[field] for field in fields if field != "label") - (
            2 * (len(fields) - 1)
        )
        sizes["label"] = max(1, min(sizes["label"], 25, label_budget))
        table = Table(
            box=box.SIMPLE_HEAD,
            show_edge=False,
            padding=(0, 1),
            pad_edge=False,
            collapse_padding=True,
            header_style="dim",
        )
        for field in fields:
            size = sizes[field]
            table.add_column(
                headers[field],
                justify="left" if field in {"label", "unit", "status"} else "right",
                width=size,
                min_width=size,
                max_width=size,
                no_wrap=True,
                overflow="ellipsis" if field == "label" else "crop",
            )
        displayed = frozenset(
            name for field, name in _METRIC_FIELDS.items() if field in fields
        ) | {"status"}
        for task, row in zip(visible, cells, strict=True):
            self._displayed_metrics[task.id] = displayed
            table.add_row(*(row[field] for field in fields))
        return table

    def get_renderables(self) -> Iterable[RenderableType]:
        tasks = sorted((task for task in self.tasks if task.visible), key=_task_order)
        self.compact = False
        self._displayed_metrics = {}
        shown_group = False
        for group, title in enumerate(_GROUP_LABELS):
            members = [
                task for task in tasks
                if _group_index(
                    str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
                ) == group
            ]
            if not members:
                continue
            if shown_group:
                yield Text("")
            yield Text(title, style="bold blue")
            yield Padding(self.make_tasks_table(members), (0, 0, 0, 2), expand=False)
            shown_group = True
        if tasks:
            yield Text("")
            if self.compact:
                yield Text("Amplía la terminal para ver todas las columnas.", style="dim")
            if any(
                name not in _metric_values(task)
                for task in tasks for name in _METRIC_FIELDS.values()
                if _group_index(
                    str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
                ) in {2, 3}
            ):
                yield Text("— dato no informado", style="dim")
        if self.details and tasks:
            yield Text("")
            yield Text("Detalles de las tareas", style="bold blue")
            for task in tasks:
                label = _task_label(
                    str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
                )
                detail = Text(f"{label}  ", style="bold")
                detail.append(task.description, style="white")
                for name, value in _metric_values(task).items():
                    if name in self._displayed_metrics.get(task.id, _PRIMARY_METRICS):
                        continue
                    metric_label = _METRIC_PRESENTATION.get(
                        name, (name.replace("_", " "), "white"),
                    )[0]
                    detail.append(f"  ·  {metric_label}: {value}", style="dim")
                remaining = task.time_remaining
                if remaining is not None and not task.fields.get("event_finished", False):
                    detail.append(f"  ·  ETA: {_format_duration(remaining)}", style="dim")
                yield Padding(detail, (0, 0, 0, 2), expand=False)


class RichProgress:
    """Render each ``(operation, phase)`` as one adaptive, stable row."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        transient: bool = False,
        refresh_per_second: float = 10.0,
        details: bool | None = None,
    ) -> None:
        self._console = console if console is not None else _default_console()
        self._progress = _GroupedProgress(
            details=(os.environ.get("NEOCORTEX_PROGRESS_DETAILS") == "1")
            if details is None else details,
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
                "completed_count": event.completed,
                "total_count": event.total,
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
