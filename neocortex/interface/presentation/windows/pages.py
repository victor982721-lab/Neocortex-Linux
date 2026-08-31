"""Cohesive Qt page builders used by :class:`MainWindow`."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...application.request import ROUTE_ORDER
from ...read.models import MAX_QUERY_CHARACTERS
from ..widgets import MetricCard, RouteToggle, StatusPill


def build_overview_page(window: Any) -> QWidget:
    scroll, _canvas, layout = window._page_canvas()
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
    start.clicked.connect(lambda: window._select_page(1))
    hero_layout.addWidget(start)
    layout.addWidget(hero)

    cards = QHBoxLayout()
    cards.setSpacing(14)
    window.overview_run_card = MetricCard("ÚLTIMA CORRIDA", "—", accent=True)
    window.overview_files_card = MetricCard("ARCHIVOS VERIFICADOS")
    window.overview_duration_card = MetricCard("DURACIÓN")
    window.overview_errors_card = MetricCard("INCIDENCIAS")
    for card in (
        window.overview_run_card,
        window.overview_files_card,
        window.overview_duration_card,
        window.overview_errors_card,
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
    window.overview_table = window._make_history_table(limit_height=265)
    panel_layout.addWidget(window.overview_table)
    layout.addWidget(panel)
    layout.addStretch(1)
    return scroll


def build_execution_page(window: Any) -> QWidget:
    scroll, _canvas, layout = window._page_canvas()
    configuration = QFrame()
    configuration.setObjectName("Panel")
    config_layout = QVBoxLayout(configuration)
    config_layout.setContentsMargins(22, 20, 22, 22)
    config_layout.setSpacing(15)
    config_layout.addWidget(
        window._section_heading("Configuración", "Define alcance y modo antes de iniciar")
    )

    root_row = QHBoxLayout()
    window.root_edit = QLineEdit()
    window.root_edit.setPlaceholderText("Directorio raíz")
    window.root_edit.textChanged.connect(window._root_changed)
    browse = QPushButton("Examinar")
    browse.clicked.connect(window._browse_root)
    root_row.addWidget(window.root_edit, 1)
    root_row.addWidget(browse)
    config_layout.addWidget(window._field_label("Directorio raíz"))
    config_layout.addLayout(root_row)

    config_layout.addWidget(window._field_label("Estado local de la aplicación"))
    state_panel = QFrame()
    state_panel.setObjectName("DependencyItem")
    state_layout = QVBoxLayout(state_panel)
    state_layout.setContentsMargins(14, 10, 14, 10)
    state_layout.setSpacing(2)
    state_path = QLabel(str(window._state_directory))
    state_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    state_note = QLabel("Ubicación XDG aislada; queda fuera del inventario.")
    state_note.setProperty("muted", True)
    state_layout.addWidget(state_path)
    state_layout.addWidget(state_note)
    config_layout.addWidget(state_panel)

    config_layout.addWidget(window._field_label("Rutas de contenido"))
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
    window.route_toggles = {}
    for index, route in enumerate(ROUTE_ORDER):
        toggle = RouteToggle(names[route], route)
        toggle.setChecked(True)
        window.route_toggles[route] = toggle
        route_grid.addWidget(toggle, index // 4, index % 4)
    for column in range(4):
        route_grid.setColumnStretch(column, 1)
    config_layout.addLayout(route_grid)

    options_row = QHBoxLayout()
    options = QVBoxLayout()
    options.addWidget(window._field_label("Alcance"))
    window.scope_combo = QComboBox()
    window.scope_combo.addItem("Inventario, mantenimiento y rutas", False)
    window.scope_combo.addItem("Solo rutas sobre el último inventario", True)
    options.addWidget(window.scope_combo)
    mode = QVBoxLayout()
    mode.addWidget(window._field_label("Modo"))
    mode_row = QHBoxLayout()
    window.analysis_radio = QRadioButton("Analizar")
    window.analysis_radio.setObjectName("ModeButton")
    window.analysis_radio.setChecked(True)
    window.apply_radio = QRadioButton("Aplicar cambios")
    window.apply_radio.setObjectName("ModeButton")
    window.apply_radio.setProperty("danger", True)
    window.apply_radio.setEnabled(False)
    window.apply_radio.setToolTip(
        "Las mutaciones del corpus están deshabilitadas por la política Linux vigente."
    )
    mode_group = QButtonGroup(window)
    mode_group.addButton(window.analysis_radio)
    mode_group.addButton(window.apply_radio)
    mode_row.addWidget(window.analysis_radio)
    mode_row.addWidget(window.apply_radio)
    window.scope_combo.currentIndexChanged.connect(window._scope_changed)
    mode.addLayout(mode_row)
    options_row.addLayout(options, 1)
    options_row.addSpacing(14)
    options_row.addLayout(mode, 1)
    config_layout.addLayout(options_row)

    safety = QLabel(
        "Modo Linux: inventario, búsqueda y procesamiento están disponibles; "
        "las mutaciones del corpus permanecen deshabilitadas."
    )
    safety.setProperty("muted", True)
    safety.setWordWrap(True)
    config_layout.addWidget(safety)

    actions = QHBoxLayout()
    window.start_button = QPushButton("Iniciar ejecución")
    window.start_button.setObjectName("PrimaryButton")
    window.start_button.clicked.connect(window._start_execution)
    window.cancel_button = QPushButton("Solicitar cancelación")
    window.cancel_button.setObjectName("DangerButton")
    window.cancel_button.setEnabled(False)
    window.cancel_button.clicked.connect(window._request_cancellation)
    actions.addWidget(window.start_button, 1)
    actions.addWidget(window.cancel_button)
    config_layout.addLayout(actions)
    layout.addWidget(configuration)

    live = QFrame()
    live.setObjectName("Panel")
    live_layout = QVBoxLayout(live)
    live_layout.setContentsMargins(22, 20, 22, 22)
    live_layout.setSpacing(13)
    live_header = QHBoxLayout()
    live_header.addWidget(
        window._section_heading("Progreso en vivo", "Eventos estructurados del motor"),
        1,
    )
    window.live_status = StatusPill()
    live_header.addWidget(window.live_status)
    live_layout.addLayout(live_header)
    activity = QFrame()
    activity.setObjectName("ActivityBanner")
    activity_layout = QVBoxLayout(activity)
    activity_layout.setContentsMargins(16, 12, 16, 12)
    activity_layout.setSpacing(7)
    window.activity_title = QLabel("Sin ejecución activa")
    window.activity_title.setObjectName("ActivityTitle")
    window.activity_detail = QLabel("La etapa actual y su tiempo activo aparecerán aquí.")
    window.activity_detail.setObjectName("ActivityDetail")
    window.activity_detail.setWordWrap(True)
    window.activity_progress = QProgressBar()
    window.activity_progress.setTextVisible(False)
    window.activity_progress.setRange(0, 1000)
    window.activity_progress.setValue(0)
    activity_layout.addWidget(window.activity_title)
    activity_layout.addWidget(window.activity_detail)
    activity_layout.addWidget(window.activity_progress)
    live_layout.addWidget(activity)
    window.progress_scroll = QScrollArea()
    window.progress_scroll.setWidgetResizable(True)
    window.progress_scroll.setFrameShape(QFrame.Shape.NoFrame)
    window.progress_scroll.setMinimumHeight(260)
    progress_canvas = QWidget()
    window.progress_layout = QVBoxLayout(progress_canvas)
    window.progress_layout.setContentsMargins(0, 0, 4, 0)
    window.progress_layout.setSpacing(9)
    window.progress_placeholder = QLabel("El progreso de la siguiente ejecución aparecerá aquí.")
    window.progress_placeholder.setProperty("muted", True)
    window.progress_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.progress_layout.addWidget(window.progress_placeholder)
    window.progress_layout.addStretch(1)
    window.progress_scroll.setWidget(progress_canvas)
    live_layout.addWidget(window.progress_scroll)
    live_layout.addWidget(window._field_label("Registro de sesión"))
    window.session_log = QPlainTextEdit()
    window.session_log.setReadOnly(True)
    window.session_log.setMaximumBlockCount(500)
    window.session_log.setMaximumHeight(175)
    live_layout.addWidget(window.session_log)
    layout.addWidget(live)
    layout.addStretch(1)
    return scroll


def build_history_page(window: Any) -> QWidget:
    scroll, _canvas, layout = window._page_canvas()
    panel = QFrame()
    panel.setObjectName("Panel")
    panel_layout = QVBoxLayout(panel)
    panel_layout.setContentsMargins(22, 20, 22, 22)
    heading_row = QHBoxLayout()
    heading_row.addWidget(
        window._section_heading("Historial durable", "Lectura acotada del estado SQLite"),
        1,
    )
    refresh = QPushButton("Actualizar")
    refresh.clicked.connect(window._refresh_data)
    heading_row.addWidget(refresh)
    panel_layout.addLayout(heading_row)
    window.history_table = window._make_history_table()
    panel_layout.addWidget(window.history_table)
    layout.addWidget(panel)
    layout.addStretch(1)
    return scroll


def build_system_page(window: Any) -> QWidget:
    scroll, _canvas, layout = window._page_canvas()
    panel = QFrame()
    panel.setObjectName("Panel")
    panel_layout = QVBoxLayout(panel)
    panel_layout.setContentsMargins(22, 20, 22, 22)
    panel_layout.setSpacing(12)
    heading_row = QHBoxLayout()
    heading_row.addWidget(
        window._section_heading("Componentes", "Disponibilidad del entorno actual"), 1
    )
    refresh = QPushButton("Comprobar")
    refresh.clicked.connect(window._refresh_dependencies)
    heading_row.addWidget(refresh)
    panel_layout.addLayout(heading_row)
    window.dependencies_layout = QGridLayout()
    window.dependencies_layout.setSpacing(10)
    panel_layout.addLayout(window.dependencies_layout)
    layout.addWidget(panel)

    note = QFrame()
    note.setObjectName("Panel")
    note_layout = QVBoxLayout(note)
    note_layout.setContentsMargins(22, 18, 22, 18)
    note_layout.addWidget(
        window._section_heading(
            "Distribución",
            "Runtime versionado y acceso integrado en KDE",
        )
    )
    detail = QLabel(
        "El launcher estable usa el release activo; los modelos y el estado "
        "permanecen compartidos fuera de cada release inmutable."
    )
    detail.setProperty("muted", True)
    detail.setWordWrap(True)
    note_layout.addWidget(detail)
    layout.addWidget(note)
    layout.addStretch(1)
    return scroll


def build_consultation_page(window: Any) -> QWidget:
    scroll, _canvas, layout = window._page_canvas()

    request_panel = QFrame()
    request_panel.setObjectName("Panel")
    request_layout = QVBoxLayout(request_panel)
    request_layout.setContentsMargins(22, 20, 22, 22)
    request_layout.setSpacing(14)

    heading_row = QHBoxLayout()
    heading_row.addWidget(
        window._section_heading(
            "Consulta tu información",
            "Estado, búsqueda, contexto citado y revisión consultiva",
        ),
        1,
    )
    window.consult_status = StatusPill("idle")
    window.consult_status.setText("Solo lectura")
    heading_row.addWidget(window.consult_status)
    request_layout.addLayout(heading_row)

    controls = QGridLayout()
    controls.setHorizontalSpacing(12)
    controls.setVerticalSpacing(6)

    controls.addWidget(window._field_label("Acción"), 0, 0)
    controls.addWidget(window._field_label("Alcance publicado"), 0, 1)
    controls.addWidget(window._field_label("Resultados por alcance"), 0, 2)

    window.consult_operation = QComboBox()
    window.consult_operation.setObjectName("ConsultOperation")
    window.consult_operation.addItem("Buscar evidencia", "search")
    window.consult_operation.addItem("Preparar respuesta citada", "ask")
    window.consult_operation.addItem("Estado publicado", "status")
    window.consult_operation.addItem("Revisar valor", "review")
    controls.addWidget(window.consult_operation, 1, 0)

    window.consult_scope = QComboBox()
    window.consult_scope.setObjectName("ConsultScope")
    window.consult_scope.addItem("Personal + Framework (separados)", "all")
    window.consult_scope.addItem("Personal", "personal")
    window.consult_scope.addItem("Framework", "framework")
    controls.addWidget(window.consult_scope, 1, 1)

    window.consult_limit = QSpinBox()
    window.consult_limit.setObjectName("ConsultLimit")
    window.consult_limit.setRange(1, 100)
    window.consult_limit.setValue(10)
    window.consult_limit.setSuffix(" máx.")
    controls.addWidget(window.consult_limit, 1, 2)
    controls.setColumnStretch(0, 3)
    controls.setColumnStretch(1, 3)
    controls.setColumnStretch(2, 1)
    request_layout.addLayout(controls)

    window.consult_operation_note = QLabel()
    window.consult_operation_note.setProperty("muted", True)
    window.consult_operation_note.setWordWrap(True)
    request_layout.addWidget(window.consult_operation_note)

    query_row = QHBoxLayout()
    window.consult_query = QLineEdit()
    window.consult_query.setObjectName("ConsultQuery")
    window.consult_query.setMaxLength(MAX_QUERY_CHARACTERS)
    window.consult_query.setClearButtonEnabled(True)
    window.consult_query.returnPressed.connect(window._run_read_request)
    window.consult_button = QPushButton("Buscar")
    window.consult_button.setObjectName("PrimaryButton")
    window.consult_button.clicked.connect(window._run_read_request)
    window.consult_cancel_button = QPushButton("Cancelar")
    window.consult_cancel_button.setObjectName("ConsultCancelButton")
    window.consult_cancel_button.setEnabled(False)
    window.consult_cancel_button.clicked.connect(window._cancel_read_request)
    query_row.addWidget(window.consult_query, 1)
    query_row.addWidget(window.consult_button)
    query_row.addWidget(window.consult_cancel_button)
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
    window.consult_result_title = QLabel("Resultado")
    window.consult_result_title.setObjectName("SectionTitle")
    window.consult_result_summary = QLabel(
        "Escribe una consulta o elige una acción que no requiere texto."
    )
    window.consult_result_summary.setObjectName("SectionCaption")
    window.consult_result_summary.setWordWrap(True)
    window.consult_result = QPlainTextEdit()
    window.consult_result.setObjectName("ConsultResult")
    window.consult_result.setReadOnly(True)
    window.consult_result.setMaximumBlockCount(3_000)
    window.consult_result.setMinimumHeight(310)
    window.consult_result.setPlainText(
        "NeoCortex mostrará aquí evidencia, citas y límites de cobertura en lenguaje legible."
    )
    result_layout.addWidget(window.consult_result_title)
    result_layout.addWidget(window.consult_result_summary)
    result_layout.addWidget(window.consult_result, 1)
    result_actions = QHBoxLayout()
    result_actions.addStretch(1)
    window.consult_copy_button = QPushButton("Copiar resultado")
    window.consult_copy_button.setEnabled(False)
    window.consult_copy_button.clicked.connect(window._copy_consult_result)
    result_actions.addWidget(window.consult_copy_button)
    result_layout.addLayout(result_actions)
    layout.addWidget(result_panel)
    layout.addStretch(1)

    window.consult_operation.currentIndexChanged.connect(window._consult_operation_changed)
    window._consult_operation_changed()
    return scroll
