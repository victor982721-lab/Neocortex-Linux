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

from _04_Nucleo_Operativo.app_paths import default_ui_settings_path

from .controller import WorkerController
from .elevation import is_elevated, start_elevated_ui
from .read_client import (
    MAX_QUERY_CHARACTERS,
    ReadClient,
    ReadClientError,
    ReadOperation,
    ReadRequest,
    SharedReadClient,
    present_read_payload,
)
from .run_request import ROUTE_ORDER, RunRequest
from .status_repository import RunStatus, StatusRepository, StatusRepositoryError
from .theme import COLORS
from .widgets import (
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
        self._nav_buttons: list[NavButton] = []
        self._progress_items: dict[tuple[str, str], ProgressItem] = {}
        self._last_status_error: str | None = None

        self._build_shell()
        self._connect_controller()
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
        scroll, _canvas, layout = self._page_canvas()
        hero = QFrame()
        hero.setObjectName("Panel")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        text_layout = QVBoxLayout()
        title = QLabel("Tu entorno documental, bajo control")
        title.setStyleSheet("font-size: 18pt; font-weight: 760;")
        caption = QLabel(
            "Inventario incremental, clasificación técnica y acciones verificadas "
            "desde una sola interfaz."
        )
        caption.setProperty("muted", True)
        caption.setWordWrap(True)
        text_layout.addWidget(title)
        text_layout.addWidget(caption)
        hero_layout.addLayout(text_layout, 1)
        start = QPushButton("Nueva ejecución")
        start.setObjectName("PrimaryButton")
        start.clicked.connect(lambda: self._select_page(1))
        hero_layout.addWidget(start)
        layout.addWidget(hero)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.overview_run_card = MetricCard("ÚLTIMA CORRIDA", "—", accent=True)
        self.overview_files_card = MetricCard("ARCHIVOS VERIFICADOS")
        self.overview_duration_card = MetricCard("DURACIÓN")
        self.overview_errors_card = MetricCard("INCIDENCIAS")
        for card in (
            self.overview_run_card,
            self.overview_files_card,
            self.overview_duration_card,
            self.overview_errors_card,
        ):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 20)
        panel_layout.setSpacing(12)
        heading = QLabel("Actividad reciente")
        heading.setObjectName("SectionTitle")
        panel_layout.addWidget(heading)
        self.overview_table = self._make_history_table(limit_height=265)
        panel_layout.addWidget(self.overview_table)
        layout.addWidget(panel)
        layout.addStretch(1)
        return scroll

    def _build_execution_page(self) -> QWidget:
        scroll, _canvas, layout = self._page_canvas()
        configuration = QFrame()
        configuration.setObjectName("Panel")
        config_layout = QVBoxLayout(configuration)
        config_layout.setContentsMargins(22, 20, 22, 22)
        config_layout.setSpacing(15)
        config_layout.addWidget(
            self._section_heading("Configuración", "Define alcance y modo antes de iniciar")
        )

        root_row = QHBoxLayout()
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("Directorio raíz")
        self.root_edit.textChanged.connect(self._root_changed)
        browse = QPushButton("Examinar")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(browse)
        config_layout.addWidget(self._field_label("Directorio raíz"))
        config_layout.addLayout(root_row)

        config_layout.addWidget(self._field_label("Estado local de la aplicación"))
        state_panel = QFrame()
        state_panel.setObjectName("DependencyItem")
        state_layout = QVBoxLayout(state_panel)
        state_layout.setContentsMargins(14, 10, 14, 10)
        state_layout.setSpacing(2)
        state_path = QLabel(str(self._state_directory))
        state_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        state_note = QLabel(
            "Ubicación XDG aislada; queda fuera del inventario."
            if self._portable_linux
            else "Ubicación fija dentro de AppData; queda fuera del inventario."
        )
        state_note.setProperty("muted", True)
        state_layout.addWidget(state_path)
        state_layout.addWidget(state_note)
        config_layout.addWidget(state_panel)

        config_layout.addWidget(self._field_label("Rutas de contenido"))
        route_grid = QGridLayout()
        route_grid.setHorizontalSpacing(10)
        route_grid.setVerticalSpacing(10)
        names = {
            "pdf": "PDF",
            "docx": "Word",
            "office": "Office",
            "archive": "ZIP",
            "text": "Texto y correo",
            "audio": "Audio",
            "image": "Imágenes",
            "code": "Código",
        }
        self.route_toggles: dict[str, RouteToggle] = {}
        for index, route in enumerate(ROUTE_ORDER):
            toggle = RouteToggle(names[route], route)
            toggle.setChecked(True)
            self.route_toggles[route] = toggle
            route_grid.addWidget(toggle, index // 4, index % 4)
        for column in range(4):
            route_grid.setColumnStretch(column, 1)
        config_layout.addLayout(route_grid)

        options_row = QHBoxLayout()
        options = QVBoxLayout()
        options.addWidget(self._field_label("Alcance"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Inventario, mantenimiento y rutas", False)
        self.scope_combo.addItem("Solo rutas sobre el último inventario", True)
        options.addWidget(self.scope_combo)
        mode = QVBoxLayout()
        mode.addWidget(self._field_label("Modo"))
        mode_row = QHBoxLayout()
        self.analysis_radio = QRadioButton("Analizar")
        self.analysis_radio.setObjectName("ModeButton")
        self.analysis_radio.setChecked(True)
        self.apply_radio = QRadioButton("Aplicar cambios")
        self.apply_radio.setObjectName("ModeButton")
        self.apply_radio.setProperty("danger", True)
        if self._portable_linux:
            self.apply_radio.setEnabled(False)
            self.apply_radio.setToolTip(
                "Las mutaciones del corpus requieren el backend seguro de Windows."
            )
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.analysis_radio)
        mode_group.addButton(self.apply_radio)
        mode_row.addWidget(self.analysis_radio)
        mode_row.addWidget(self.apply_radio)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        mode.addLayout(mode_row)
        options_row.addLayout(options, 1)
        options_row.addSpacing(14)
        options_row.addLayout(mode, 1)
        config_layout.addLayout(options_row)

        safety = QLabel(
            (
                "Modo portátil Linux: inventario, búsqueda y procesamiento están "
                "disponibles; las mutaciones del corpus permanecen deshabilitadas."
            )
            if self._portable_linux
            else (
                "Analizar no modifica archivos. Aplicar usa las mismas validaciones, "
                "protecciones y papelera del motor NeoCortex."
            )
        )
        safety.setProperty("muted", True)
        safety.setWordWrap(True)
        config_layout.addWidget(safety)

        actions = QHBoxLayout()
        self.start_button = QPushButton(
            "Iniciar ejecución"
            if self._portable_linux or self._execution_elevated
            else "Habilitar ejecución"
        )
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_execution)
        self.cancel_button = QPushButton("Solicitar cancelación")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._request_cancellation)
        actions.addWidget(self.start_button, 1)
        actions.addWidget(self.cancel_button)
        config_layout.addLayout(actions)
        layout.addWidget(configuration)

        live = QFrame()
        live.setObjectName("Panel")
        live_layout = QVBoxLayout(live)
        live_layout.setContentsMargins(22, 20, 22, 22)
        live_layout.setSpacing(13)
        live_header = QHBoxLayout()
        live_header.addWidget(
            self._section_heading("Progreso en vivo", "Eventos estructurados del motor"),
            1,
        )
        self.live_status = StatusPill()
        live_header.addWidget(self.live_status)
        live_layout.addLayout(live_header)
        activity = QFrame()
        activity.setObjectName("ActivityBanner")
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(16, 12, 16, 12)
        activity_layout.setSpacing(7)
        self.activity_title = QLabel("Sin ejecución activa")
        self.activity_title.setObjectName("ActivityTitle")
        self.activity_detail = QLabel("La etapa actual y su tiempo activo aparecerán aquí.")
        self.activity_detail.setObjectName("ActivityDetail")
        self.activity_detail.setWordWrap(True)
        self.activity_progress = QProgressBar()
        self.activity_progress.setTextVisible(False)
        self.activity_progress.setRange(0, 1000)
        self.activity_progress.setValue(0)
        activity_layout.addWidget(self.activity_title)
        activity_layout.addWidget(self.activity_detail)
        activity_layout.addWidget(self.activity_progress)
        live_layout.addWidget(activity)
        self.progress_scroll = QScrollArea()
        self.progress_scroll.setWidgetResizable(True)
        self.progress_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.progress_scroll.setMinimumHeight(260)
        progress_canvas = QWidget()
        self.progress_layout = QVBoxLayout(progress_canvas)
        self.progress_layout.setContentsMargins(0, 0, 4, 0)
        self.progress_layout.setSpacing(9)
        self.progress_placeholder = QLabel("El progreso de la siguiente ejecución aparecerá aquí.")
        self.progress_placeholder.setProperty("muted", True)
        self.progress_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_layout.addWidget(self.progress_placeholder)
        self.progress_layout.addStretch(1)
        self.progress_scroll.setWidget(progress_canvas)
        live_layout.addWidget(self.progress_scroll)
        live_layout.addWidget(self._field_label("Registro de sesión"))
        self.session_log = QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setMaximumBlockCount(500)
        self.session_log.setMaximumHeight(175)
        live_layout.addWidget(self.session_log)
        layout.addWidget(live)
        layout.addStretch(1)
        return scroll

    def _build_history_page(self) -> QWidget:
        scroll, _canvas, layout = self._page_canvas()
        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 22)
        heading_row = QHBoxLayout()
        heading_row.addWidget(
            self._section_heading("Historial durable", "Lectura acotada del estado SQLite"),
            1,
        )
        refresh = QPushButton("Actualizar")
        refresh.clicked.connect(self._refresh_data)
        heading_row.addWidget(refresh)
        panel_layout.addLayout(heading_row)
        self.history_table = self._make_history_table()
        panel_layout.addWidget(self.history_table)
        layout.addWidget(panel)
        layout.addStretch(1)
        return scroll

    def _build_system_page(self) -> QWidget:
        scroll, _canvas, layout = self._page_canvas()
        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 22)
        panel_layout.setSpacing(12)
        heading_row = QHBoxLayout()
        heading_row.addWidget(
            self._section_heading("Componentes", "Disponibilidad del entorno actual"), 1
        )
        refresh = QPushButton("Comprobar")
        refresh.clicked.connect(self._refresh_dependencies)
        heading_row.addWidget(refresh)
        panel_layout.addLayout(heading_row)
        self.dependencies_layout = QGridLayout()
        self.dependencies_layout.setSpacing(10)
        panel_layout.addLayout(self.dependencies_layout)
        layout.addWidget(panel)

        note = QFrame()
        note.setObjectName("Panel")
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(22, 18, 22, 18)
        note_layout.addWidget(
            self._section_heading(
                "Distribución",
                (
                    "Runtime versionado y acceso integrado en KDE"
                    if self._portable_linux
                    else "Runtime personal versionado"
                ),
            )
        )
        detail = QLabel(
            (
                "El launcher estable usa el release activo; los modelos y el estado "
                "permanecen compartidos fuera de cada release inmutable."
            )
            if self._portable_linux
            else (
                "La interfaz separa el proceso operativo, el protocolo de progreso "
                "y la persistencia del runtime versionado."
            )
        )
        detail.setProperty("muted", True)
        detail.setWordWrap(True)
        note_layout.addWidget(detail)
        layout.addWidget(note)
        layout.addStretch(1)
        return scroll

    def _build_consultation_page(self) -> QWidget:
        scroll, _canvas, layout = self._page_canvas()

        request_panel = QFrame()
        request_panel.setObjectName("Panel")
        request_layout = QVBoxLayout(request_panel)
        request_layout.setContentsMargins(22, 20, 22, 22)
        request_layout.setSpacing(14)

        heading_row = QHBoxLayout()
        heading_row.addWidget(
            self._section_heading(
                "Consulta tu información",
                "Estado, búsqueda, contexto citado y revisión consultiva",
            ),
            1,
        )
        self.consult_status = StatusPill("idle")
        self.consult_status.setText("Solo lectura")
        heading_row.addWidget(self.consult_status)
        request_layout.addLayout(heading_row)

        controls = QGridLayout()
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(6)

        controls.addWidget(self._field_label("Acción"), 0, 0)
        controls.addWidget(self._field_label("Alcance publicado"), 0, 1)
        controls.addWidget(self._field_label("Resultados por alcance"), 0, 2)

        self.consult_operation = QComboBox()
        self.consult_operation.setObjectName("ConsultOperation")
        self.consult_operation.addItem("Buscar evidencia", "search")
        self.consult_operation.addItem("Preparar respuesta citada", "ask")
        self.consult_operation.addItem("Estado publicado", "status")
        self.consult_operation.addItem("Revisar valor", "review")
        controls.addWidget(self.consult_operation, 1, 0)

        self.consult_scope = QComboBox()
        self.consult_scope.setObjectName("ConsultScope")
        self.consult_scope.addItem("Personal + Framework (separados)", "all")
        self.consult_scope.addItem("Personal", "personal")
        self.consult_scope.addItem("Framework", "framework")
        controls.addWidget(self.consult_scope, 1, 1)

        self.consult_limit = QSpinBox()
        self.consult_limit.setObjectName("ConsultLimit")
        self.consult_limit.setRange(1, 100)
        self.consult_limit.setValue(10)
        self.consult_limit.setSuffix(" máx.")
        controls.addWidget(self.consult_limit, 1, 2)
        controls.setColumnStretch(0, 3)
        controls.setColumnStretch(1, 3)
        controls.setColumnStretch(2, 1)
        request_layout.addLayout(controls)

        self.consult_operation_note = QLabel()
        self.consult_operation_note.setProperty("muted", True)
        self.consult_operation_note.setWordWrap(True)
        request_layout.addWidget(self.consult_operation_note)

        query_row = QHBoxLayout()
        self.consult_query = QLineEdit()
        self.consult_query.setObjectName("ConsultQuery")
        self.consult_query.setMaxLength(MAX_QUERY_CHARACTERS)
        self.consult_query.setClearButtonEnabled(True)
        self.consult_query.returnPressed.connect(self._run_read_request)
        self.consult_button = QPushButton("Buscar")
        self.consult_button.setObjectName("PrimaryButton")
        self.consult_button.clicked.connect(self._run_read_request)
        query_row.addWidget(self.consult_query, 1)
        query_row.addWidget(self.consult_button)
        request_layout.addLayout(query_row)

        safety = QLabel(
            "Esta superficie usa exclusivamente snapshots publicados y scopes fijos. "
            "No acepta rutas de estado, no procesa el corpus y no autoriza mutaciones."
        )
        safety.setObjectName("ReadOnlyNotice")
        safety.setProperty("muted", True)
        safety.setWordWrap(True)
        request_layout.addWidget(safety)
        layout.addWidget(request_panel)

        result_panel = QFrame()
        result_panel.setObjectName("Panel")
        result_layout = QVBoxLayout(result_panel)
        result_layout.setContentsMargins(22, 20, 22, 22)
        result_layout.setSpacing(10)
        self.consult_result_title = QLabel("Resultado")
        self.consult_result_title.setObjectName("SectionTitle")
        self.consult_result_summary = QLabel(
            "Escribe una consulta o elige una acción que no requiere texto."
        )
        self.consult_result_summary.setObjectName("SectionCaption")
        self.consult_result_summary.setWordWrap(True)
        self.consult_result = QPlainTextEdit()
        self.consult_result.setObjectName("ConsultResult")
        self.consult_result.setReadOnly(True)
        self.consult_result.setMaximumBlockCount(3_000)
        self.consult_result.setMinimumHeight(310)
        self.consult_result.setPlainText(
            "NeoCortex mostrará aquí evidencia, citas y límites de cobertura en lenguaje legible."
        )
        result_layout.addWidget(self.consult_result_title)
        result_layout.addWidget(self.consult_result_summary)
        result_layout.addWidget(self.consult_result, 1)
        layout.addWidget(result_panel)
        layout.addStretch(1)

        self.consult_operation.currentIndexChanged.connect(self._consult_operation_changed)
        self._consult_operation_changed()
        return scroll

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
        self.consult_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            payload = self._read_client.execute(request)
            presentation = present_read_payload(request, payload)
        except (
            AttributeError,
            ImportError,
            OSError,
            ReadClientError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            detail = " ".join(str(exc).split())[:800] or type(exc).__name__
            self.consult_status.set_state("failed", "No disponible")
            self.consult_result_title.setText("No fue posible consultar")
            self.consult_result_summary.setText(detail)
            self.consult_result.setPlainText(
                "La consulta se abstuvo de continuar. No se creó, migró ni modificó estado."
            )
        else:
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
        finally:
            QApplication.restoreOverrideCursor()
            self.consult_button.setEnabled(True)

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
        if message_type == "started":
            self._append_log(f"Worker iniciado · PID {self._controller.process_id}")
        elif message_type == "cancel_acknowledged":
            self._append_log("El motor reconoció la solicitud de cancelación.")
        elif message_type == "completed":
            issues = int(record.get("action_errors", 0)) + sum(
                int(value) for value in dict(record.get("route_errors", {})).values()
            )
            issues = max(issues, int(record.get("issues", issues)))
            completed_with_issues = (
                str(record.get("completion_status", "")) == "completed_with_issues"
            )
            state = (
                "warning"
                if issues or completed_with_issues or int(record.get("exit_code", 0))
                else "completed"
            )
            self.live_status.set_state(state)
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
        elif message_type == "cancelled":
            self.live_status.set_state("cancelled")
            self._set_activity("Ejecución cancelada", str(record.get("detail", "")))
            self._append_log(str(record.get("detail", "Ejecución cancelada")))
        elif message_type == "failed":
            self.live_status.set_state("failed")
            self._set_activity(
                "La ejecución falló",
                f"{record.get('error_type', 'Error')}: {record.get('detail', '')}",
            )
            self._append_log(f"{record.get('error_type', 'Error')}: {record.get('detail', '')}")

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
        metrics = record.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        detail_parts = []
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
        prefix = "Etapa completada: " if finished else "Ahora: "
        self._set_activity(
            prefix + str(record.get("description", "Procesando")),
            "  ·  ".join(detail_parts),
            completed=completed,
            total=total,
            indeterminate=not finished and in_flight > 0 and completed == 0,
        )

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
        while self.dependencies_layout.count():
            item = self.dependencies_layout.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
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
        dependencies = (
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
        for index, (name, available, detail) in enumerate(dependencies):
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
            caption = QLabel(str(detail))
            caption.setProperty("muted", True)
            caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            labels.addWidget(title)
            labels.addWidget(caption)
            row.addWidget(marker)
            row.addLayout(labels, 1)
            self.dependencies_layout.addWidget(frame, index // 2, index % 2)
        self.dependencies_layout.setColumnStretch(0, 1)
        self.dependencies_layout.setColumnStretch(1, 1)

    def closeEvent(self, event: QCloseEvent) -> None:
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
