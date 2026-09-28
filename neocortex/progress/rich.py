"""Aligned terminal tables projected from typed progress events."""

from __future__ import annotations

import os
import sys
import time
import unicodedata
from collections.abc import Iterable
from threading import RLock
from typing import TypedDict

from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Console, ConsoleDimensions, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import Progress, Task, TaskID
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
    "advisory_blocked": ("abstenciones advisory", "yellow"),
    "cache_pending": ("caché pendiente", "yellow"),
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
    "model_id": ("modelo", "bright_black"),
    "source_scope": ("contenido", "blue"),
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


class _ProgressFields(TypedDict):
    unit: str
    metrics: tuple[ProgressMetric, ...]
    operation: str
    phase: str
    event_finished: bool
    completed_count: int
    total_count: int | None


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
        environment = dict(self._environ)
        environment.pop("COLUMNS", None)
        environment.pop("LINES", None)
        self._environ = environment

    @property
    def size(self) -> ConsoleDimensions:
        dimensions = _terminal_dimensions()
        if dimensions is None:
            return super().size
        columns, lines = dimensions
        return ConsoleDimensions(columns, lines)

    @size.setter
    def size(self, new_size: tuple[int, int]) -> None:
        self._width, self._height = new_size


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
    if operation in {"dedup", "zip-intake", "email-intake"} or (
        operation == "framework"
        and phase in {"content-types", "duplicates", "zip-intake-reconciliation"}
    ):
        return 1
    if operation in _ROUTE_LABELS:
        return 2
    if operation == "semantic" or operation.startswith("catalog-"):
        return 3
    return 4


def _task_label(operation: str, phase: str, metrics: dict[str, int | str] | None = None) -> str:
    if operation == "zip-intake":
        return "Revisar contenedores"
    if operation == "email-intake":
        return "Adjuntos de correo"
    if operation in _ROUTE_LABELS:
        route = _ROUTE_LABELS[operation]
        if phase.startswith("catalog-"):
            kind = phase.removeprefix("catalog-")
            label = kind.upper() if kind not in _ROUTE_LABELS else _ROUTE_LABELS[kind]
            return f"{label} / Catálogo"
        if phase.startswith("format:"):
            return f"{phase.removeprefix('format:').upper()} / Extracción"
        suffix = {"fts": "FTS", "profile": "Perfil", "extract": "Extracción"}.get(phase)
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
        if kind == "generation":
            source = str((metrics or {}).get("source_scope", ""))
            scope_label = {"text": "Texto", "image": "Imagen", "image-ocr": "OCR imagen"}.get(source, "")
            return f"{label} {scope_label} G{scope}" if scope_label else f"{label} G{scope}"
        return f"{label} {scope.upper()}" if scope else label
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
            str(task.fields.get("operation", "")), str(task.fields.get("phase", "")), metrics,
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
        self._displayed_fields: dict[TaskID, frozenset[str]] = {}
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
                self._displayed_fields[task.id] = frozenset({"label", "advance", "status", "errors"})
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
            self._displayed_fields[task.id] = frozenset(fields)
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
            expected_fields = _ROUTE_FIELDS if group in {2, 3} else _GENERAL_FIELDS
            omitted = [field for field in expected_fields
                       if field not in self._displayed_fields.get(task.id, frozenset(expected_fields))]
            if omitted:
                cells = _task_cells(task)
                values = " · ".join(f"{_FIELD_LABELS[field]}: {cells[field].plain}" for field in omitted)
                contents.append(Text(f"{cells['label'].plain} · {values}", style="dim"))
            format_summary = self._text_format_summary(task)
            if format_summary is not None:
                contents.append(format_summary)
            if task.fields.get("operation") == "zip-intake":
                summary = self._zip_summary(task)
                contents.extend((Text(""),
                                 Align.center(summary, width=self._table_width())
                                 if self._decorated() else summary))
            if task.fields.get("phase") in {"organization-apply", "organization-plan", "empty-directories"}:
                metrics = _metric_values(task)
                effect_values = [f"{_METRIC_PRESENTATION.get(k, (k, ''))[0]}={metrics[k]}"
                          for k in ("applied", "blocked", "advisory_blocked", "review",
                                    "cache_pending", "remaining") if k in metrics]
                if effect_values:
                    contents.append(Text(" · ".join(effect_values)))
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

    def _text_format_summary(self, task: Task) -> Table | None:
        """Project typed per-format observations, without reprocessing sources."""
        formats: dict[str, dict[str, int]] = {}
        for name, value in _metric_values(task).items():
            parts = name.split(":", 2)
            if len(parts) == 3 and parts[0] == "format" and type(value) is int:
                formats.setdefault(parts[2], {})[parts[1]] = value
        if not formats:
            return None
        table = Table(box=None, padding=(0, 1), header_style="dim", width=self._table_width())
        table.add_column("Formato", no_wrap=False)
        table.add_column("Cobertura observada", overflow="fold")
        labels = {"email": "EML", "markdown": "MD", "txt": "TXT"}
        for kind, metrics in sorted(formats.items()):
            values = []
            for key, label in (("candidates", "observados"), ("processed", "completos"),
                               ("cache_hits", "caché"), ("extracted", "extraídos"),
                               ("errors", "errores"), ("cached_errors", "errores caché")):
                if key in metrics:
                    values.append(f"{label}={metrics[key]}")
            table.add_row(labels.get(kind, kind.upper()), " · ".join(values))
        return table

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
        self._displayed_fields = {}
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
        self._transient = transient
        self._scrollback = False
        self._printed: dict[TaskID, tuple[float, tuple[object, ...]]] = {}

    def _overflowing(self) -> bool:
        """Switch to an append-only log instead of hiding completed rows.

        Live cannot erase lines which have scrolled beyond a terminal's height.
        Repainting an oversized dashboard repeatedly would duplicate history.
        Estimate conservatively; once switched, never erase existing scrollback.
        """
        if self._transient or not self._console.is_terminal or self._console.is_dumb_terminal:
            return False
        tasks = [task for task in self._progress.tasks if task.visible]
        groups = {_group_index(str(t.fields.get("operation", "")),
                               str(t.fields.get("phase", ""))) for t in tasks}
        rows = len(tasks) + len(groups) * 5 + 2
        if self._console.width < 100:
            rows += 2 * len(tasks)  # wrapped primary counters/time stay visible
        rows += sum(8 for task in tasks if task.fields.get("operation") == "zip-intake")
        for task in tasks:
            formats = {name.split(":", 2)[2] for name in _metric_values(task)
                       if name.startswith("format:") and len(name.split(":", 2)) == 3}
            if formats:
                rows += 3 + 2 * len(formats)
        if self._progress.details:
            rows += 2 + 2 * len(tasks)
        return rows > self._console.height

    @staticmethod
    def _print_signature(task: Task) -> tuple[object, ...]:
        return (task.fields.get("completed_count"), task.fields.get("total_count"),
                task.fields.get("event_finished"), task.fields.get("metrics"), task.description)

    def _print_scrollback_task(self, task: Task, *, final: bool = False) -> None:
        now = time.monotonic()
        signature = self._print_signature(task)
        previous = self._printed.get(task.id)
        if previous is not None:
            if previous[1] == signature:
                return
            if not final and not task.fields.get("event_finished") and now - previous[0] < 5.0:
                return
        group = _group_index(str(task.fields.get("operation", "")),
                             str(task.fields.get("phase", "")))
        renderable = self._progress._section(_GROUP_LABELS[group], [task], group=group)
        if self._console.options.ascii_only or os.environ.get("NEOCORTEX_PROGRESS_ASCII") == "1":
            renderable = _AsciiPresentation(renderable)
        self._console.print(renderable)
        self._printed[task.id] = (now, signature)

    def _switch_to_scrollback(self) -> None:
        # Clear the bounded viewport once, then emit the complete accumulated
        # state to the terminal's normal history. No alternate screen is used.
        self._progress.live.transient = True
        self._progress.stop()
        self._scrollback = True
        self._console.print(self._progress.get_renderable())
        now = time.monotonic()
        self._printed = {task.id: (now, self._print_signature(task))
                         for task in self._progress.tasks}

    def start(self) -> None:
        with self._lock:
            if not self._started:
                if not self._scrollback:
                    self._progress.start()
                self._started = True

    def stop(self) -> None:
        with self._lock:
            if self._started:
                if self._scrollback:
                    for task in self._progress.tasks:
                        self._print_scrollback_task(task, final=True)
                else:
                    self._progress.stop()
                self._started = False

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if not self._started:
                self.start()
            task_id = self._tasks.get(event.key)
            fields: _ProgressFields = {
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
            task = next(task for task in self._progress.tasks if task.id == task_id)
            if not self._scrollback and self._overflowing():
                self._switch_to_scrollback()
            elif self._scrollback:
                self._print_scrollback_task(task)
            elif event.finished:
                self._progress.refresh()

    def __enter__(self) -> "RichProgress":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
