"""Reusable presentation widgets for the NeoCortex desktop interface."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


# region [01] Shared helpers


def repolish(widget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


# endregion [01]


# region [02] Navigation and status


class NavButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("NavButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class StatusPill(QLabel):
    LABELS = {
        "idle": "En espera",
        "running": "En ejecución",
        "completed": "Completado",
        "warning": "Con incidencias",
        "failed": "Fallido",
        "cancelled": "Cancelado",
    }

    def __init__(self, state: str = "idle", parent=None):
        super().__init__(parent)
        self.setObjectName("StatusPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state(state)

    def set_state(self, state: str, text: str | None = None) -> None:
        visual_state = "warning" if state == "cancelled" else state
        if visual_state not in {"idle", "running", "completed", "warning", "failed"}:
            visual_state = "idle"
        self.setProperty("state", visual_state)
        self.setText(text or self.LABELS.get(state, state.capitalize()))
        repolish(self)


# endregion [02]


# region [03] Dashboard cards and route controls


class MetricCard(QFrame):
    def __init__(
        self,
        title: str,
        value: str = "—",
        detail: str = "",
        *,
        accent: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("MetricCard")
        self.setProperty("accent", accent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("MetricDetail")
        self.detail_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)
        layout.addStretch(1)

    def set_value(self, value: str, detail: str | None = None) -> None:
        self.value_label.setText(value)
        if detail is not None:
            self.detail_label.setText(detail)


class RouteToggle(QCheckBox):
    def __init__(self, title: str, route_name: str, parent=None):
        super().__init__(title, parent)
        self.route_name = route_name
        self.setObjectName("RouteToggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)


# endregion [03]


# region [04] Live progress


_METRIC_LABELS = {
    "cache_hits": "caché",
    "cached_errors": "errores previos",
    "new_work": "nuevos",
    "completed_work": "hechos",
    "classified": "clasificados",
    "planned": "planeados",
    "applied": "aplicados",
    "review": "revisión",
    "errors": "errores",
    "timeouts": "timeouts",
    "recycled": "papelera",
    "protected": "protegidos",
    "remaining": "restantes",
    "in_flight": "activos",
}


class ProgressItem(QFrame):
    def __init__(self, event: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setObjectName("ProgressItem")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.description_label = QLabel()
        self.description_label.setStyleSheet("font-weight: 700;")
        self.count_label = QLabel()
        self.count_label.setProperty("muted", True)
        header.addWidget(self.description_label, 1)
        header.addWidget(self.count_label)
        self.progress = QProgressBar()
        self.metric_label = QLabel()
        self.metric_label.setProperty("muted", True)
        self.metric_label.setWordWrap(True)
        layout.addLayout(header)
        layout.addWidget(self.progress)
        layout.addWidget(self.metric_label)
        self.update_event(event)

    def update_event(self, event: dict[str, Any]) -> None:
        completed = max(0, int(event.get("completed", 0)))
        total_value = event.get("total")
        total = None if total_value is None else max(0, int(total_value))
        finished = bool(event.get("finished"))
        unit = str(event.get("unit", "elementos"))
        self.description_label.setText(str(event.get("description", "Procesando")))
        self._update_count(completed, total, unit, finished=finished)
        self.metric_label.setText(_visible_metrics(event.get("metrics")))

    def _update_count(
        self,
        completed: int,
        total: int | None,
        unit: str,
        *,
        finished: bool,
    ) -> None:
        if total is None:
            self.count_label.setText(f"{format_count(completed)} {unit}")
            self._update_indeterminate_progress(finished=finished)
            return
        self.count_label.setText(f"{format_count(completed)} / {format_count(total)} {unit}")
        self.progress.setRange(0, 1000)
        ratio = 1000 if total == 0 and finished else int(1000 * completed / max(1, total))
        self.progress.setValue(min(1000, max(0, ratio)))

    def _update_indeterminate_progress(self, *, finished: bool) -> None:
        if finished:
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            return
        self.progress.setRange(0, 0)


def _visible_metrics(value: object) -> str:
    if not isinstance(value, dict):
        return "Sin incidencias"
    visible = [
        _visible_metric(name, metric_value)
        for name, metric_value in list(value.items())[:10]
        if metric_value != 0 or name in {"errors", "remaining", "in_flight"}
    ]
    return "  ·  ".join(visible) or "Sin incidencias"


def _visible_metric(name: object, value: object) -> str:
    normalized = str(name)
    label = _METRIC_LABELS.get(normalized, normalized.replace("_", " "))
    return f"{label}: {value}"


# endregion [04]
