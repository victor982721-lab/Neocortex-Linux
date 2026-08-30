"""Main PySide6 window for supervised NeoCortex operations."""

from __future__ import annotations

import importlib.util
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from neocortex.platform_policy import default_corpus_root

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from neocortex.runtime.config.app_paths import default_ui_settings_path

from ...application.controller import WorkerController
from ...application.elevation import is_elevated, start_elevated_ui
from ...application.request import ROUTE_ORDER, RunRequest
from ...read.client import SharedReadClient
from ...read.models import (
    ReadClient,
    ReadOperation,
    ReadPresentation,
    ReadRequest,
)
from ...read.status import RunStatus, StatusRepository, StatusRepositoryError
from ...read.tasks import ReadTaskController
from ..theme import COLORS
from .pages import (
    build_consultation_page,
    build_execution_page,
    build_history_page,
    build_overview_page,
    build_system_page,
)
from ..widgets import (
    MetricCard,
    NavButton,
    ProgressItem,
    RouteToggle,
    StatusPill,
    format_count,
    format_duration,
)


# region [01] Window shell


class MainWindow(QMainWindow):
    """Supervise one run while keeping durable status visible and bounded."""

    PAGE_METADATA = (
        ("Inicio", "Resumen operativo y estado durable"),
        ("Ejecución", "Configura y supervisa una ejecución"),
        ("Historial", "Últimas ejecuciones registradas"),
        ("Sistema", "Dependencias y preparación del entorno"),
        ("Consulta", "Busca y revisa evidencia publicada sin modificar archivos"),
    )

    overview_run_card: MetricCard
    overview_files_card: MetricCard
    overview_duration_card: MetricCard
    overview_errors_card: MetricCard
    overview_table: QTableWidget
    history_table: QTableWidget
    root_edit: QLineEdit
    route_toggles: dict[str, RouteToggle]
    scope_combo: QComboBox
    analysis_radio: QRadioButton
    apply_radio: QRadioButton
    start_button: QPushButton
    cancel_button: QPushButton
    live_status: StatusPill
    activity_title: QLabel
    activity_detail: QLabel
    activity_progress: QProgressBar
    progress_scroll: QScrollArea
    progress_layout: QVBoxLayout
    progress_placeholder: QLabel
    session_log: QPlainTextEdit
    dependencies_layout: QGridLayout
    consult_status: StatusPill
    consult_operation: QComboBox
    consult_scope: QComboBox
    consult_limit: QSpinBox
    consult_operation_note: QLabel
    consult_query: QLineEdit
    consult_button: QPushButton
    consult_cancel_button: QPushButton
    consult_result_title: QLabel
    consult_result_summary: QLabel
    consult_result: QPlainTextEdit
    consult_copy_button: QPushButton

    def __init__(
        self,
        *,
        initial_root: Path,
        state_directory: Path,
        settings_path: Path | None = None,
        controller: WorkerController | None = None,
        read_client: ReadClient | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("NeoCortex")
        self.setMinimumSize(1120, 720)
        self.resize(1440, 900)
        self._default_root = Path(initial_root)
        self._state_directory = Path(state_directory)
        effective_settings = settings_path or default_ui_settings_path()
        self._settings = QSettings(
            os.fspath(effective_settings),
            QSettings.Format.IniFormat,
        )
        self._repository = StatusRepository(self._state_directory)
        self._portable_linux = os.name != "nt"
        self._execution_elevated = is_elevated()
        self._controller = controller or WorkerController(self)
        self._read_client = read_client or SharedReadClient()
        self._read_tasks = ReadTaskController(self._read_client, self)
        self._active_read_request: int | None = None
        self._nav_buttons: list[NavButton] = []
        self._progress_items: dict[tuple[str, str], ProgressItem] = {}
        self._last_status_error: str | None = None

        self._build_shell()
        self._connect_controller()
        self._connect_read_tasks()
        self._load_settings()
        self._refresh_data()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3_000)
        self._status_timer.timeout.connect(self._refresh_data)
        self._status_timer.start()

    @property
    def controller(self) -> WorkerController:
        return self._controller

    def _build_shell(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_header())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_overview_page())
        self.pages.addWidget(self._build_execution_page())
        self.pages.addWidget(self._build_history_page())
        self.pages.addWidget(self._build_system_page())
        self.pages.addWidget(self._build_consultation_page())
        content_layout.addWidget(self.pages, 1)
        layout.addWidget(content, 1)
        self._select_page(0)

    def _build_sidebar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Sidebar")
        frame.setFixedWidth(226)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 24, 18, 20)
        layout.setSpacing(8)

        brand = QHBoxLayout()
        mark = QLabel("N")
        mark.setObjectName("BrandMark")
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel("NEOCORTEX")
        name.setObjectName("BrandName")
        caption = QLabel("Modo portátil Linux" if self._portable_linux else "Control operativo")
        caption.setObjectName("BrandCaption")
        names.addWidget(name)
        names.addWidget(caption)
        brand.addWidget(mark)
        brand.addSpacing(7)
        brand.addLayout(names, 1)
        layout.addLayout(brand)
        layout.addSpacing(30)

        labels = (
            "⌂  Inicio",
            "▶  Ejecución",
            "≡  Historial",
            "◇  Sistema",
            "⌕  Consulta",
        )
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, label in enumerate(labels):
            button = NavButton(label)
            button.clicked.connect(lambda _checked=False, page=index: self._select_page(page))
            group.addButton(button, index)
            self._nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch(1)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color: #243140;")
        layout.addWidget(separator)
        footer = QLabel("Estado persistente\nSin trabajo en segundo plano no supervisado")
        footer.setProperty("muted", True)
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return frame

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Header")
        frame.setFixedHeight(88)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(28, 14, 30, 14)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.page_title = QLabel()
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)
        self.header_root = QLabel()
        self.header_root.setProperty("muted", True)
        self.header_root.setMaximumWidth(430)
        self.header_root.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.header_status = StatusPill()
        layout.addWidget(self.header_root)
        layout.addSpacing(14)
        layout.addWidget(self.header_status)
        return frame

    def _select_page(self, index: int) -> None:
        if hasattr(self, "pages"):
            self.pages.setCurrentIndex(index)
        for button_index, button in enumerate(self._nav_buttons):
            button.setChecked(button_index == index)
        title, subtitle = self.PAGE_METADATA[index]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        if index in {0, 2, 3}:
            self._refresh_data()

    # endregion [01]

    # region [02] Page builders

    def _page_canvas(self) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        canvas = QWidget()
        canvas.setObjectName("PageCanvas")
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(28, 25, 30, 30)
        layout.setSpacing(18)
        scroll.setWidget(canvas)
        return scroll, canvas, layout

    def _build_overview_page(self) -> QWidget:
        return build_overview_page(self)

    def _build_execution_page(self) -> QWidget:
        return build_execution_page(self)

    def _build_history_page(self) -> QWidget:
        return build_history_page(self)

    def _build_system_page(self) -> QWidget:
        return build_system_page(self)

    def _build_consultation_page(self) -> QWidget:
        return build_consultation_page(self)

    def _section_heading(self, title: str, caption: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        caption_label = QLabel(caption)
        caption_label.setObjectName("SectionCaption")
        layout.addWidget(title_label)
        layout.addWidget(caption_label)
        return widget

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: 680;")
        return label

    def _make_history_table(self, limit_height: int | None = None) -> QTableWidget:
        table = QTableWidget(0, 8)
        table.setHorizontalHeaderLabels(
            (
                "Corrida",
                "Estado",
                "Tipo",
                "Fase",
                "Inicio",
                "Duración",
                "Errores",
                "Raíz",
            )
        )
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        if limit_height is not None:
            table.setMaximumHeight(limit_height)
        return table

    def _consult_operation_changed(self, _index: int = -1) -> None:
        operation = str(self.consult_operation.currentData())
        needs_query = operation in {"search", "ask"}
        self.consult_query.setEnabled(needs_query)
        self.consult_limit.setEnabled(operation != "status")

        defaults = {"status": 1, "search": 10, "ask": 8, "review": 50}
        self.consult_limit.setValue(defaults.get(operation, 10))
        placeholders = {
            "search": "Ejemplo: pruebas eléctricas del transformador U5",
            "ask": "Ejemplo: ¿qué evidencia existe sobre el tratamiento de aceite?",
            "status": "El estado publicado no requiere una consulta",
            "review": "La revisión de valor no requiere una consulta",
        }
        notes = {
            "search": (
                "Devuelve evidencia concreta con ruta y ubicación. Los resultados "
                "de Personal y Framework conservan rankings independientes."
            ),
            "ask": (
                "Prepara citas locales para sustentar una respuesta; si falta evidencia, "
                "lo indica en lugar de completarla por inferencia."
            ),
            "status": (
                "Resume disponibilidad, compatibilidad y snapshot de las fuentes "
                "publicadas sin abrir productores."
            ),
            "review": (
                "Muestra candidatos conservadores y su incertidumbre. Es una vista "
                "consultiva: no mueve, archiva ni elimina archivos."
            ),
        }
        button_labels = {
            "search": "Buscar",
            "ask": "Preparar evidencia",
            "status": "Consultar estado",
            "review": "Revisar sin cambios",
        }
        self.consult_query.setPlaceholderText(placeholders.get(operation, ""))
        self.consult_operation_note.setText(notes.get(operation, ""))
        self.consult_button.setText(button_labels.get(operation, "Consultar"))

    def _run_read_request(self) -> None:
        if self._active_read_request is not None:
            return
        operation = cast(ReadOperation, str(self.consult_operation.currentData()))
        request = ReadRequest(
            operation=operation,
            scope=str(self.consult_scope.currentData()),
            query=self.consult_query.text(),
            limit=self.consult_limit.value(),
        )
        try:
            request = request.validated()
        except ValueError as exc:
            self.consult_status.set_state("warning", "Falta información")
            self.consult_result_title.setText("Consulta incompleta")
            self.consult_result_summary.setText(str(exc))
            self.consult_result.setPlainText(
                "No se consultó ningún estado ni se modificó ningún archivo."
            )
            return

        self.consult_status.set_state("running", "Consultando…")
        self._set_consult_running(True)
        try:
            self._active_read_request = self._read_tasks.start(request)
        except RuntimeError as exc:
            self._active_read_request = None
            self._set_consult_running(False)
            self._show_read_failure(" ".join(str(exc).split())[:800])

    def _connect_read_tasks(self) -> None:
        self._read_tasks.succeeded.connect(self._read_succeeded)
        self._read_tasks.failed.connect(self._read_failed)
        self._read_tasks.cancelled.connect(self._read_cancelled)

    def _set_consult_running(self, running: bool) -> None:
        self.consult_operation.setEnabled(not running)
        self.consult_scope.setEnabled(not running)
        self.consult_query.setEnabled(not running)
        self.consult_limit.setEnabled(not running)
        self.consult_button.setEnabled(not running)
        self.consult_cancel_button.setEnabled(running)
        if not running:
            self._consult_operation_changed()

    def _cancel_read_request(self) -> None:
        request_id = self._active_read_request
        if request_id is None:
            return
        if self._read_tasks.cancel(request_id):
            self.consult_cancel_button.setEnabled(False)
            self.consult_status.set_state("running", "Cancelando…")

    def _read_succeeded(self, request_id: int, value: object) -> None:
        if request_id != self._active_read_request:
            return
        if not isinstance(value, ReadPresentation):
            self._show_read_failure("La consulta devolvió una presentación incompatible.")
            self._finish_read_request(request_id)
            return
        presentation = value
        status_text = {
            "completed": "Consulta lista",
            "warning": "Cobertura parcial",
            "failed": "Requiere atención",
        }[presentation.state]
        self.consult_status.set_state(presentation.state, status_text)
        self.consult_result_title.setText(presentation.title)
        self.consult_result_summary.setText(presentation.summary)
        self.consult_result.setPlainText(presentation.body)
        self.consult_result.moveCursor(QTextCursor.MoveOperation.Start)
        self.consult_copy_button.setEnabled(bool(presentation.body))
        self._finish_read_request(request_id)

    def _read_failed(self, request_id: int, detail: str) -> None:
        if request_id != self._active_read_request:
            return
        self._show_read_failure(detail)
        self._finish_read_request(request_id)

    def _read_cancelled(self, request_id: int) -> None:
        if request_id != self._active_read_request:
            return
        self.consult_status.set_state("warning", "Consulta cancelada")
        self.consult_result_title.setText("Consulta cancelada")
        self.consult_result_summary.setText(
            "Se descartó el resultado de la operación local de solo lectura."
        )
        self.consult_result.setPlainText(
            "No se creó, migró ni modificó estado. Puedes iniciar otra consulta."
        )
        self._finish_read_request(request_id)

    def _show_read_failure(self, detail: str) -> None:
        self.consult_status.set_state("failed", "No disponible")
        self.consult_result_title.setText("No fue posible consultar")
        self.consult_result_summary.setText(detail)
        self.consult_result.setPlainText(
            "La consulta se abstuvo de continuar. No se creó, migró ni modificó estado."
        )

    def _finish_read_request(self, request_id: int) -> None:
        if request_id != self._active_read_request:
            return
        self._active_read_request = None
        self._set_consult_running(False)

    def _copy_consult_result(self) -> None:
        text = self.consult_result.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    # endregion [02]

    # region [03] Durable status and settings

    def _load_settings(self) -> None:
        root = str(self._settings.value("execution/root", str(self._default_root)))
        selected = str(self._settings.value("execution/routes", ",".join(ROUTE_ORDER)))
        selected_routes = frozenset(part for part in selected.split(",") if part)
        self.root_edit.setText(root)
        for route, toggle in self.route_toggles.items():
            toggle.setChecked(route in selected_routes)
        self.analysis_radio.setChecked(True)

    def _save_settings(self) -> None:
        self._settings.setValue("execution/root", self.root_edit.text().strip())
        selected = [route for route, toggle in self.route_toggles.items() if toggle.isChecked()]
        self._settings.setValue("execution/routes", ",".join(selected))
        self._settings.sync()

    def _root_changed(self, value: str) -> None:
        self.header_root.setText(value.strip())
        self.header_root.setToolTip(value.strip())

    def _refresh_data(self) -> None:
        try:
            runs = self._repository.recent_runs(50)
            latest = runs[0] if runs else None
            inventory = (
                {}
                if latest is None
                else self._repository.latest_event_details(latest.run_id, "inventory")
            )
        except StatusRepositoryError as exc:
            self._show_status_error(str(exc))
            self._refresh_dependencies()
            return
        self._last_status_error = None
        self._populate_history(self.history_table, runs)
        self._populate_history(self.overview_table, runs[:5])
        self._refresh_overview(latest, inventory)
        if not self._controller.is_running:
            if latest is None:
                self.header_status.set_state("idle")
            else:
                self.header_status.set_state(self._visual_status(latest))
        self._refresh_dependencies()

    def _show_status_error(self, detail: str) -> None:
        self._populate_history(self.history_table, ())
        self._populate_history(self.overview_table, ())
        self.overview_run_card.set_value("No disponible", "Estado durable no legible")
        self.overview_files_card.set_value("—", "Inventario no consultado")
        self.overview_duration_card.set_value("—", "")
        self.overview_errors_card.set_value("—", "Requiere diagnóstico de SQLite")
        if not self._controller.is_running:
            self.header_status.set_state("failed", "Estado no disponible")
        if detail != self._last_status_error:
            self._append_log(f"Estado durable no disponible: {detail}")
            self._last_status_error = detail

    def _refresh_overview(
        self,
        latest: RunStatus | None,
        inventory: dict[str, Any],
    ) -> None:
        if latest is None:
            self.overview_run_card.set_value("Sin datos", "Todavía no hay corridas")
            self.overview_files_card.set_value("0", "Sin inventario registrado")
            self.overview_duration_card.set_value("—", "")
            self.overview_errors_card.set_value("0", "")
            return
        file_count = int(inventory.get("files", latest.files_checked))
        errors = latest.action_errors + latest.route_errors
        self.overview_run_card.set_value(
            f"#{latest.run_id}",
            self._status_label(latest),
        )
        self.overview_files_card.set_value(
            format_count(file_count),
            f"{format_count(latest.files_checked)} tipos verificados",
        )
        self.overview_duration_card.set_value(
            format_duration(latest.duration_seconds),
            latest.phase.replace("_", " "),
        )
        self.overview_errors_card.set_value(
            format_count(errors),
            "acciones y rutas" if errors else "sin fallos operativos",
        )

    def _populate_history(self, table: QTableWidget, runs: tuple[RunStatus, ...]) -> None:
        table.setRowCount(len(runs))
        for row_index, run in enumerate(runs):
            started = datetime.fromtimestamp(run.started_ns / 1_000_000_000)
            values = (
                f"#{run.run_id}",
                self._status_label(run),
                run.run_kind.replace("_", " "),
                run.phase.replace("_", " "),
                started.strftime("%d/%m/%Y %H:%M"),
                format_duration(run.duration_seconds),
                str(run.action_errors + run.route_errors),
                run.root,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 6}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 1:
                    visual = self._visual_status(run)
                    color = {
                        "completed": COLORS["accent"],
                        "running": COLORS["teal"],
                        "warning": COLORS["amber"],
                        "failed": COLORS["danger"],
                    }.get(visual, COLORS["muted"])
                    item.setForeground(QColor(color))
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()

    def _visual_status(self, run: RunStatus) -> str:
        if run.status == "completed":
            return "warning" if run.action_errors or run.route_errors else "completed"
        if run.status in {"running", "completed", "failed"}:
            return run.status
        if run.status in {"cancelled", "interrupted"}:
            return "warning"
        return "idle"

    def _status_label(self, run: RunStatus) -> str:
        labels = {
            "completed": "Completado",
            "running": "En ejecución",
            "failed": "Fallido",
            "cancelled": "Cancelado",
            "interrupted": "Interrumpido",
        }
        value = labels.get(run.status, run.status.capitalize())
        if run.status == "completed" and (run.action_errors or run.route_errors):
            return "Con incidencias"
        return value

    # endregion [03]

    # region [04] Execution lifecycle

    def _connect_controller(self) -> None:
        self._controller.message_received.connect(self._on_worker_message)
        self._controller.output_received.connect(self._append_log)
        self._controller.running_changed.connect(self._running_changed)
        self._controller.execution_finished.connect(self._execution_finished)
        self._controller.startup_failed.connect(self._startup_failed)

    def _current_request(self) -> RunRequest:
        routes = tuple(route for route in ROUTE_ORDER if self.route_toggles[route].isChecked())
        return RunRequest(
            root=Path(self.root_edit.text().strip()),
            routes=routes,
            apply=self.apply_radio.isChecked(),
            route_only=bool(self.scope_combo.currentData()),
        ).validated()

    def _scope_changed(self, _index: int = -1) -> None:
        route_only = bool(self.scope_combo.currentData())
        if route_only:
            self.analysis_radio.setChecked(True)
        self.apply_radio.setEnabled(
            not self._portable_linux and not route_only and not self._controller.is_running
        )

    def _start_execution(self) -> None:
        try:
            request = self._current_request()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Configuración no válida", str(exc))
            return
        if not self._portable_linux and not self._execution_elevated:
            self._offer_elevated_restart(request.root)
            return
        if request.apply:
            routes = ", ".join(request.routes) or "mantenimiento común"
            answer = QMessageBox.warning(
                self,
                "Confirmar aplicación",
                "NeoCortex podrá enviar duplicados verificados y directorios vacíos "
                "a la papelera, además de aplicar organización técnica autorizada.\n\n"
                f"Raíz: {request.root}\nRutas: {routes}\n\n"
                "¿Deseas iniciar en modo Apply?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._save_settings()
        self._clear_progress()
        self.session_log.clear()
        self._append_log("Iniciando modo " + ("Apply" if request.apply else "Análisis"))
        try:
            self._controller.start(request)
        except RuntimeError as exc:
            QMessageBox.critical(self, "No fue posible iniciar", str(exc))
            return
        self.live_status.set_state("running")
        self.header_status.set_state("running")
        self._set_activity(
            "Preparando ejecución",
            "Worker iniciado; esperando el primer evento del motor.",
            indeterminate=True,
        )

    def _offer_elevated_restart(self, root: Path) -> None:
        answer = QMessageBox.information(
            self,
            "Permiso requerido para ejecutar",
            "Windows exige privilegios administrativos para consultar el diario USN "
            "del volumen. La interfaz se reabrirá con esos permisos; ninguna ejecución "
            "se iniciará automáticamente.\n\n¿Deseas continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._save_settings()
        try:
            process_id = start_elevated_ui(root)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "No fue posible obtener permisos",
                str(exc),
            )
            return
        self._append_log(f"UI elevada iniciada · PID {process_id}")
        self.close()

    def _request_cancellation(self) -> None:
        if self._controller.request_cancellation():
            self.cancel_button.setEnabled(False)
            self.live_status.set_state("warning", "Cancelando…")
            self._append_log("Cancelación cooperativa solicitada; esperando un límite seguro.")

    def _on_worker_message(self, record: dict[str, Any]) -> None:
        message_type = str(record.get("type", ""))
        if message_type == "progress":
            self._update_progress(record)
            self._update_activity_from_progress(record)
            return
        if message_type == "heartbeat":
            self._update_activity_from_heartbeat(record)
            return
        handlers = {
            "started": self._worker_started,
            "cancel_acknowledged": self._worker_cancel_acknowledged,
            "completed": self._worker_completed,
            "cancelled": self._worker_cancelled,
            "failed": self._worker_failed,
        }
        handler = handlers.get(message_type)
        if handler is not None:
            handler(record)

    def _worker_started(self, _record: dict[str, Any]) -> None:
        self._append_log(f"Worker iniciado · PID {self._controller.process_id}")

    def _worker_cancel_acknowledged(self, _record: dict[str, Any]) -> None:
        self._append_log("El motor reconoció la solicitud de cancelación.")

    def _worker_completed(self, record: dict[str, Any]) -> None:
        issues = int(record.get("action_errors", 0)) + sum(
            int(value) for value in dict(record.get("route_errors", {})).values()
        )
        issues = max(issues, int(record.get("issues", issues)))
        completed_with_issues = str(record.get("completion_status", "")) == "completed_with_issues"
        has_issues = bool(issues or completed_with_issues or int(record.get("exit_code", 0)))
        self.live_status.set_state("warning" if has_issues else "completed")
        self._set_activity(
            "Ejecución completada",
            f"Corrida #{record.get('run_id')} finalizada.",
            completed=1,
            total=1,
        )
        self._append_log(
            f"Corrida #{record.get('run_id')} completada · "
            f"{record.get('files_checked', 0)} archivos verificados · "
            f"{issues} incidencias"
        )

    def _worker_cancelled(self, record: dict[str, Any]) -> None:
        self.live_status.set_state("cancelled")
        self._set_activity("Ejecución cancelada", str(record.get("detail", "")))
        self._append_log(str(record.get("detail", "Ejecución cancelada")))

    def _worker_failed(self, record: dict[str, Any]) -> None:
        detail = f"{record.get('error_type', 'Error')}: {record.get('detail', '')}"
        self.live_status.set_state("failed")
        self._set_activity("La ejecución falló", detail)
        self._append_log(detail)

    def _update_progress(self, record: dict[str, Any]) -> None:
        key = (str(record.get("operation", "")), str(record.get("phase", "")))
        item = self._progress_items.get(key)
        if item is None:
            if self.progress_placeholder.isVisible():
                self.progress_placeholder.hide()
            item = ProgressItem(record)
            self._progress_items[key] = item
            self.progress_layout.insertWidget(self.progress_layout.count() - 1, item)
        else:
            item.update_event(record)
        self.progress_scroll.ensureWidgetVisible(item, 0, 20)

    def _set_activity(
        self,
        title: str,
        detail: str,
        *,
        completed: int | None = None,
        total: int | None = None,
        indeterminate: bool = False,
    ) -> None:
        self.activity_title.setText(title)
        self.activity_detail.setText(detail)
        if indeterminate:
            self.activity_progress.setRange(0, 0)
            return
        self.activity_progress.setRange(0, 1000)
        if completed is None or total is None:
            self.activity_progress.setValue(0)
            return
        ratio = 1000 if total == 0 else int(1000 * completed / max(1, total))
        self.activity_progress.setValue(min(1000, max(0, ratio)))

    def _update_activity_from_progress(
        self,
        record: dict[str, Any],
        *,
        elapsed_seconds: int | None = None,
        active_count: int | None = None,
    ) -> None:
        completed = max(0, int(record.get("completed", 0)))
        total_value = record.get("total")
        total = None if total_value is None else max(0, int(total_value))
        unit = str(record.get("unit", "elementos"))
        finished = bool(record.get("finished"))
        metrics = self._progress_metrics(record)
        detail_parts = self._activity_detail_parts(
            completed,
            total,
            unit,
            metrics,
            elapsed_seconds=elapsed_seconds,
            active_count=active_count,
        )
        in_flight = max(0, int(metrics.get("in_flight", 0)))
        prefix = "Etapa completada: " if finished else "Ahora: "
        self._set_activity(
            prefix + str(record.get("description", "Procesando")),
            "  ·  ".join(detail_parts),
            completed=completed,
            total=total,
            indeterminate=not finished and in_flight > 0 and completed == 0,
        )

    @staticmethod
    def _progress_metrics(record: dict[str, Any]) -> dict[str, Any]:
        metrics = record.get("metrics")
        return metrics if isinstance(metrics, dict) else {}

    @staticmethod
    def _activity_detail_parts(
        completed: int,
        total: int | None,
        unit: str,
        metrics: dict[str, Any],
        *,
        elapsed_seconds: int | None,
        active_count: int | None,
    ) -> list[str]:
        detail_parts: list[str] = []
        if elapsed_seconds is not None:
            detail_parts.append(f"Worker activo · {format_duration(elapsed_seconds)}")
        if total is None:
            detail_parts.append(f"{format_count(completed)} {unit}")
        else:
            detail_parts.append(f"{format_count(completed)} de {format_count(total)} {unit}")
        in_flight = max(0, int(metrics.get("in_flight", 0)))
        if in_flight:
            detail_parts.append(f"{format_count(in_flight)} tareas internas activas")
        if active_count is not None and active_count > 1:
            detail_parts.append(f"{active_count} etapas concurrentes")
        errors = max(0, int(metrics.get("errors", 0)))
        if errors:
            detail_parts.append(f"{format_count(errors)} errores")
        return detail_parts

    def _update_activity_from_heartbeat(self, record: dict[str, Any]) -> None:
        elapsed_seconds = max(0, int(record.get("elapsed_seconds", 0)))
        raw_active = record.get("active")
        active = (
            [item for item in raw_active if isinstance(item, dict)]
            if isinstance(raw_active, list)
            else []
        )
        if active:
            self._update_activity_from_progress(
                active[-1],
                elapsed_seconds=elapsed_seconds,
                active_count=len(active),
            )
            return
        self._set_activity(
            "Motor activo",
            f"Worker activo · {format_duration(elapsed_seconds)} · esperando la siguiente etapa",
            indeterminate=True,
        )

    def _running_changed(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.root_edit.setEnabled(not running)
        self.scope_combo.setEnabled(not running)
        self.analysis_radio.setEnabled(not running)
        self.apply_radio.setEnabled(
            not self._portable_linux and not running and not bool(self.scope_combo.currentData())
        )
        for toggle in self.route_toggles.values():
            toggle.setEnabled(not running)

    def _execution_finished(self, exit_code: int, lifecycle: str) -> None:
        if lifecycle not in {"completed", "cancelled", "failed"}:
            self.live_status.set_state("completed" if exit_code == 0 else "failed")
            self._set_activity(
                "Proceso finalizado" if exit_code == 0 else "El proceso se interrumpió",
                f"Código de salida {exit_code}.",
                completed=1 if exit_code == 0 else None,
                total=1 if exit_code == 0 else None,
            )
        self._append_log(f"Proceso finalizado con código {exit_code}.")
        self._refresh_data()

    def _startup_failed(self, detail: str) -> None:
        self.live_status.set_state("failed")
        self._set_activity("No fue posible iniciar", detail)
        self._append_log(detail)

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.session_log.appendPlainText(f"{timestamp}  {text}")

    def _clear_progress(self) -> None:
        for item in self._progress_items.values():
            self.progress_layout.removeWidget(item)
            item.deleteLater()
        self._progress_items.clear()
        self.progress_placeholder.show()
        self.live_status.set_state("idle")
        self._set_activity(
            "Sin ejecución activa",
            "La etapa actual y su tiempo activo aparecerán aquí.",
        )

    # endregion [04]

    # region [05] Environment and dialogs

    def _browse_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar directorio raíz",
            self.root_edit.text().strip() or str(default_corpus_root()),
        )
        if selected:
            self.root_edit.setText(selected)

    def _refresh_dependencies(self) -> None:
        self._clear_dependencies()
        for index, dependency in enumerate(self._dependency_specs()):
            self._add_dependency(index, *dependency)
        self.dependencies_layout.setColumnStretch(0, 1)
        self.dependencies_layout.setColumnStretch(1, 1)

    def _clear_dependencies(self) -> None:
        while self.dependencies_layout.count():
            item = self.dependencies_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _dependency_specs(self) -> tuple[tuple[str, bool, str], ...]:
        platform_dependency = (
            (
                "Modo de plataforma",
                True,
                "Portátil Linux · mutaciones deshabilitadas",
            )
            if self._portable_linux
            else (
                "Permisos USN",
                self._execution_elevated,
                "Administrador" if self._execution_elevated else "Se solicitarán antes de ejecutar",
            )
        )
        return (
            platform_dependency,
            ("PySide6", importlib.util.find_spec("PySide6") is not None, "Interfaz Qt"),
            ("PyMuPDF", importlib.util.find_spec("fitz") is not None, "Extracción PDF"),
            (
                "Whisper",
                importlib.util.find_spec("faster_whisper") is not None,
                "Audio local",
            ),
            (
                "Tesseract",
                shutil.which("tesseract") is not None,
                shutil.which("tesseract") or "No encontrado",
            ),
            (
                "FFmpeg",
                shutil.which("ffmpeg") is not None,
                shutil.which("ffmpeg") or "No encontrado",
            ),
            (
                "qpdf",
                shutil.which("qpdf") is not None,
                shutil.which("qpdf") or "Opcional",
            ),
        )

    def _add_dependency(self, index: int, name: str, available: bool, detail: str) -> None:
        frame = QFrame()
        frame.setObjectName("DependencyItem")
        row = QHBoxLayout(frame)
        row.setContentsMargins(15, 12, 15, 12)
        marker = QLabel("●")
        marker.setStyleSheet(f"color: {COLORS['accent'] if available else COLORS['danger']};")
        labels = QVBoxLayout()
        labels.setSpacing(1)
        title = QLabel(name)
        title.setStyleSheet("font-weight: 700;")
        caption = QLabel(detail)
        caption.setProperty("muted", True)
        caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        labels.addWidget(title)
        labels.addWidget(caption)
        row.addWidget(marker)
        row.addLayout(labels, 1)
        self.dependencies_layout.addWidget(frame, index // 2, index % 2)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._active_read_request is not None:
            self._cancel_read_request()
            self.consult_status.set_state("running", "Cancelando antes de cerrar…")
            event.ignore()
            return
        if self._controller.is_running:
            answer = QMessageBox.warning(
                self,
                "Ejecución activa",
                "La ventana supervisa un proceso operativo activo. Puede solicitar "
                "su cancelación cooperativa y mantener la interfaz abierta hasta que termine.",
                QMessageBox.StandardButton.Abort | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Abort:
                self._request_cancellation()
            event.ignore()
            return
        self._save_settings()
        event.accept()


# endregion [05]
