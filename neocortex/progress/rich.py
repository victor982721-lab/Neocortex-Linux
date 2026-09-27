"""Aligned terminal tables projected from typed progress events."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Iterable
from threading import RLock

from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Console, ConsoleDimensions, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import Progress, Task, TaskID
from rich.rule import Rule
from rich.segment import Segment
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
    "planned": "Planificado",
    "applied": "Aplicado",
    "unknown": "Desconocido",
}

_ROUTE_LABELS = {
    "pdf": "PDF",
    "docx": "DOCX",
    "office": "Office",
    "archive": "Archive",
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
_DROP_PRIORITY = ("elapsed", "waits", "new", "cache", "unit")


class _AsciiPresentation:
    """Transliterate only the human view; never mutate source events or keys."""

    _BORDERS = str.maketrans({
        "╭": "+", "╮": "+", "╰": "+", "╯": "+", "│": "|", "┃": "|",
        "─": "-", "━": "-", "┄": "-", "┅": "-", "—": "-", "·": "|", "…": "~",
    })

    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self.renderable)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for segment in console.render(self.renderable, options):
            if segment.control:
                yield segment
                continue
            text = unicodedata.normalize("NFKD", segment.text.translate(self._BORDERS))
            text = "".join(char for char in text if not unicodedata.combining(char))
            yield Segment(text.encode("ascii", "replace").decode("ascii"), segment.style)


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
    if operation in {"dedup", "zip-intake"} or (
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
    if operation == "zip-intake":
        return "Revisar contenedores"
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
        ("zip-intake", "process"): 1,
        ("framework", "zip-intake-reconciliation"): 2,
        ("framework", "content-types"): 3,
        ("dedup", "verify"): 4,
        ("framework", "duplicates"): 5,
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
    }.get(status, "default")
    cells = {
        "label": Text(_task_label(
            str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
        ), style="bold default"),
        "advance": Text(f"{completed}/{total_text}"),
        "unit": Text("conten." if task.fields.get("operation") == "zip-intake"
                     else str(task.fields.get("unit", "elementos"))),
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
            cells[field] = Text(str(value), style=style or "default")
    return cells


class _GroupedProgress(Progress):
    """One common table schema per section, with explicit optional details."""

    def __init__(
        self, *, console: Console, transient: bool = False,
        refresh_per_second: float = 10.0, details: bool = False,
    ) -> None:
        self.details = details
        self._layout_console = console
        self.compact = False
        self._displayed_metrics: dict[TaskID, frozenset[str]] = {}
        super().__init__(
            console=console, transient=transient, refresh_per_second=refresh_per_second,
        )

    def _layout_width(self) -> int:
        return max(1, min(self._layout_console.width - 2, 136))

    def _decorated(self) -> bool:
        return self._layout_console.is_terminal and not self._layout_console.is_dumb_terminal

    def _table_width(self) -> int:
        # The frame and its padding share the width budget with the data.
        return max(1, self._layout_width() - (4 if self._decorated() else 0))

    def make_tasks_table(self, tasks: Iterable[Task]) -> Table:
        visible = sorted((task for task in tasks if task.visible), key=_task_order)
        route_section = any(
            _group_index(str(task.fields.get("operation", "")), str(task.fields.get("phase", "")))
            in {2, 3} for task in visible
        )
        fields = list(_ROUTE_FIELDS if route_section else _GENERAL_FIELDS)
        cells = [_task_cells(task) for task in visible]
        headers = dict(_FIELD_LABELS)
        if self._table_width() < 60:
            headers["errors"] = "Err."
        headers["label"] = "Ruta" if visible and all(
            _group_index(str(task.fields.get("operation", "")), str(task.fields.get("phase", "")))
            == 2 for task in visible
        ) else "Tarea"
        width = self._table_width()
        sizes = {
            field: max(
                cell_len(headers[field]),
                max((row[field].cell_len for row in cells), default=0),
            )
            for field in fields
        }
        # Labels may abbreviate; counters, units and state cells never do.
        label_min = max(cell_len(headers["label"]), min(sizes["label"], 6))

        def required_width() -> int:
            return label_min + sum(sizes[field] for field in fields if field != "label") + (
                2 * (len(fields) - 1)
            )

        for field in _DROP_PRIORITY:
            if required_width() <= width:
                break
            if field in fields:
                fields.remove(field)
        if required_width() > width:
            # Exceptionally large integer counts may not fit beside a state.
            # Keep one logical row per task, but stack the indispensable cells
            # instead of cropping a number or manufacturing a shorter value.
            self.compact = True
            table = Table(box=None, padding=0, width=width, header_style="bold")
            table.add_column("Tarea / avance / estado", overflow="fold")
            for task, row in zip(visible, cells, strict=True):
                cell = Text()
                cell.append_text(row["label"])
                cell.append("\n")
                advance = row["advance"].plain
                if cell_len(advance) > width:
                    completed, total = advance.split("/", 1)
                    cell.append(f"Hechos: {completed}\nTotal: {total}")
                else:
                    cell.append_text(row["advance"])
                cell.append("\n")
                cell.append_text(row["status"])
                cell.append(" · Errores: ")
                cell.append_text(row["errors"])
                table.add_row(cell)
                self._displayed_metrics[task.id] = frozenset({"status", "errors"})
            return table
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
            header_style="bold" if self._decorated() else "dim",
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

    def _zip_summary(self, task: Task) -> Table:
        metrics = _metric_values(task)
        items = (
            ("Archivos .zip (por nombre)", "zip_files_inventoried"),
            ("Archivos .zip admitidos", "zip_files_admitted"),
            ("Paquetes atómicos", "atomic_packages"),
            ("ZIP genéricos", "generic_identified"),
            ("Sin clasificar", "unclassified"),
            ("Bloqueados", "blocked"),
            ("Contenedores aplicados", "applied"),
        )
        width = self._table_width()
        columns = 2 if width >= 70 else 1
        grid = Table.grid(padding=(0, 3), expand=False)
        for _ in range(columns):
            grid.add_column(overflow="fold")
        values = []
        for label, key in items:
            value = metrics.get(key)
            # Legacy events can supply the same observed counters under their
            # original names. Absence is unknown, never total minus another
            # category: names, classification and blocked work can overlap.
            if key == "atomic_packages" and value is None:
                value = metrics.get("atomic")
            if key == "generic_identified" and value is None:
                value = metrics.get("generic")
            known = type(value) is int and value >= 0
            cell = Text(f"{label}: ")
            cell.append(str(value) if known else "—", style="bold" if known else "dim")
            values.append(cell)
        for index in range(0, len(values), columns):
            grid.add_row(*values[index:index + columns])
        return grid

    def _section(
        self, title: str, members: list[Task], *, group: int,
    ) -> RenderableType:
        table = self.make_tasks_table(members)
        contents: list[RenderableType] = [
            Align.center(table, width=self._table_width()) if self._decorated() else table
        ]
        for task in members:
            if task.fields.get("operation") == "zip-intake":
                summary = self._zip_summary(task)
                contents.extend((Text(""),
                                 Align.center(summary, width=self._table_width())
                                 if self._decorated() else summary))
        if not self._decorated():
            return Group(Text(title, style="bold"), Padding(Group(*contents), (0, 0, 0, 2)))
        # One frame per stage, not per metric or row. Terminal foreground is
        # inherited; the single accent doesn't impose a dark-only palette.
        padding = (1, 1) if self._layout_console.height >= 40 and self._layout_console.width >= 80 else (0, 1)
        return Align.center(Panel(
            Group(*contents),
            title=Text(f"{group + 1:02d}  {title}", style="bold default"),
            title_align="center", border_style="cyan", box=box.ROUNDED,
            width=self._layout_width(), padding=padding, safe_box=True,
        ))

    def _focus_renderables(self, tasks: list[Task]) -> Iterable[RenderableType]:
        """Keep current work and warnings in a short live viewport.

        This is a presentation filter, not a second task/history store. The
        complete dashboard returns when Live stops; the structured stream is
        never filtered. Completed history must not push a failure offscreen.
        """

        unsuccessful = {"failed", "error", "cancelled", "canceled", "partial", "incomplete",
                        "blocked", "unavailable", "paused", "interrumpido", "unknown"}
        known_statuses = unsuccessful | {"running", "pending", "complete", "completed", "success",
                                        "ok", "applied", "planned", "skipped"}

        def priority(task: Task) -> tuple[int, int]:
            metrics = _metric_values(task)
            status = str(metrics.get("status", metrics.get("completion_status", ""))).lower()
            errors = metrics.get("errors")
            unknown_status = bool(status) and status not in known_statuses
            if status in unsuccessful or unknown_status or (type(errors) is int and errors > 0):
                if status in unsuccessful or unknown_status:
                    return (0 if task.fields.get("operation") in _ROUTE_LABELS else 1), -task.id
                return 2, -task.id
            if not task.fields.get("event_finished", False):
                return 3, -task.id
            return 4, -task.id

        relevant = [task for task in tasks if priority(task)[0] < 4]
        # A row can stack its count and state if the horizontal budget is too
        # small. Reserve that space before selecting rows, not after drawing.
        candidates = sorted(relevant or tasks, key=priority)
        stacked = len(self.make_tasks_table(candidates).columns) == 1
        zip_task = next((task for task in tasks if task.fields.get("operation") == "zip-intake"), None)
        zip_line: Text | None = None
        zip_rows = 0
        if zip_task is not None:
            metrics = _metric_values(zip_task)

            def observed(key: str, alias: str = "") -> str:
                value = metrics.get(key, metrics.get(alias))
                return str(value) if type(value) is int and value >= 0 else "—"

            zip_line = Text(
                f".zip: {observed('zip_files_inventoried')} · "
                f"paquetes: {observed('atomic_packages', 'atomic')} · "
                f"gen.: {observed('generic_identified', 'generic')}",
            )
            zip_rows = max(1, (zip_line.cell_len + self._layout_width() - 1) // self._layout_width())
        available = max(1, self._layout_console.height - 5 - zip_rows)
        selected: list[Task] = []
        for task in candidates:
            rows = 1
            if stacked:
                cells = _task_cells(task)
                rows = 3 + int(cells["advance"].cell_len > self._table_width())
                rows += max(0, (cells["status"].cell_len + cells["errors"].cell_len + 12 - 1)
                            // self._table_width())
            if rows > available and selected:
                break
            selected.append(task)
            available -= rows
            if available <= 0:
                break
        selected.sort(key=_task_order)
        margin = max(0, (self._layout_console.width - self._layout_width()) // 2)
        yield Padding(Rule(Text("Trabajo actual y avisos", style="bold default"), style="cyan",
                           align="center"), (0, margin))
        yield Align.center(self.make_tasks_table(selected), width=self._layout_width())
        if zip_line is not None:
            yield Align.center(zip_line, width=self._layout_width())
        hidden = len(tasks) - len(selected)
        if hidden:
            yield Align.center(Text(
                f"Vista compacta: {hidden} más; detalle al finalizar.", style="dim",
            ), width=self._layout_width())

    def get_renderables(self) -> Iterable[RenderableType]:
        ascii_only = self._layout_console.options.ascii_only or (
            os.environ.get("NEOCORTEX_PROGRESS_ASCII") == "1"
        ) or self._layout_console.is_dumb_terminal
        for renderable in self._dashboard_renderables():
            yield _AsciiPresentation(renderable) if ascii_only else renderable

    def _dashboard_renderables(self) -> Iterable[RenderableType]:
        tasks = sorted((task for task in self.tasks if task.visible), key=_task_order)
        self.compact = False
        self._displayed_metrics = {}
        groups = {_group_index(str(task.fields.get("operation", "")),
                               str(task.fields.get("phase", ""))) for task in tasks}
        zip_rows = 5 if self._table_width() >= 70 else 8
        expected_rows = len(tasks) + len(groups) * 5 + sum(
            zip_rows for task in tasks if task.fields.get("operation") == "zip-intake"
        ) + 2
        if (tasks and self._decorated() and self.live.is_started and not self.details
                and expected_rows > self._layout_console.height):
            yield from self._focus_renderables(tasks)
            return
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
            yield self._section(title, members, group=group)
            shown_group = True
        if tasks:
            yield Text("")
            if self.compact:
                yield Align.center(Text("Amplía la terminal para ver todas las columnas.", style="dim"))
            if any(
                name not in _metric_values(task)
                for task in tasks for name in _METRIC_FIELDS.values()
                if _group_index(
                    str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
                ) in {2, 3}
            ):
                yield Align.center(Text("— dato no informado", style="dim"))
        if self.details and tasks:
            yield Text("")
            yield Text("Detalles de las tareas", style="bold blue")
            for task in tasks:
                label = _task_label(
                    str(task.fields.get("operation", "")), str(task.fields.get("phase", "")),
                )
                detail = Text(f"{label}  ", style="bold")
                detail.append(task.description, style="default")
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
                # A terminal event stops the clock, not an invented remaining
                # workload. Keep declared partial and unknown totals for every
                # status, including error/paused and legacy terminal events.
                self._progress.update(task_id, completed=event.completed, refresh=False)
                task = next(task for task in self._progress.tasks if task.id == task_id)
                task.total = event.total
                self._progress.stop_task(task_id)
                self._progress.refresh()

    def __enter__(self) -> "RichProgress":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
