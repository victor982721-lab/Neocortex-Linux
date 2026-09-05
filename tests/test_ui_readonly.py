"""Human and visual contracts for read-only desktop consultation."""

from __future__ import annotations


import os
import time
from pathlib import Path
from threading import Event

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel

from neocortex.interface.presentation.windows.main import MainWindow
from neocortex.interface.read import ReadRequest
from neocortex.interface.presentation.theme import COLORS, STYLESHEET


TEST_CAPABILITIES = ('ui',)


def _rgb_distance(first: QColor, second: QColor) -> int:
    return max(
        abs(first.red() - second.red()),
        abs(first.green() - second.green()),
        abs(first.blue() - second.blue()),
    )


def _assert_meaningful_consultation_capture(
    image: QImage,
    window: MainWindow,
) -> None:
    """Reject blank/mis-styled captures without depending on PNG compression."""

    assert not image.isNull()
    assert image.width() == window.width() == 1440
    assert image.height() == window.height() == 900

    sampled_colors: set[tuple[int, int, int]] = set()
    sampled_luminance: list[int] = []
    for y in range(0, image.height(), 24):
        for x in range(0, image.width(), 24):
            color = image.pixelColor(x, y)
            sampled_colors.add((color.red() // 8, color.green() // 8, color.blue() // 8))
            sampled_luminance.append(
                (299 * color.red() + 587 * color.green() + 114 * color.blue()) // 1000
            )
    assert len(sampled_colors) >= 8
    assert max(sampled_luminance) - min(sampled_luminance) >= 80

    button = window.consult_button
    top_left = button.mapTo(window, button.rect().topLeft())
    expected_accent = QColor(COLORS["accent"])
    accent_pixels = 0
    for y in range(top_left.y() + 6, top_left.y() + button.height() - 6, 3):
        for x in range(top_left.x() + 6, top_left.x() + button.width() - 6, 3):
            if _rgb_distance(image.pixelColor(x, y), expected_accent) <= 8:
                accent_pixels += 1
    assert accent_pixels >= 30


class _FixtureReadClient:
    def __init__(self) -> None:
        self.calls: list[ReadRequest] = []

    def execute(self, request: ReadRequest) -> dict[str, object]:
        self.calls.append(request)
        common: dict[str, object] = {
            "read_only": True,
            "scope_requested": request.scope,
            "exit_code": 0,
        }
        if request.operation == "status":
            return {
                **common,
                "schema": "neocortex.read-api/v1",
                "kind": "neocortex_scoped_status",
                "scopes": [
                    {
                        "scope": "personal",
                        "status": "ready",
                        "snapshot": {
                            "snapshot_id": "personal-published-17",
                            "owners": [
                                {"owner": "pdf", "state": "available"},
                                {"owner": "audio", "state": "absent"},
                            ],
                            "active_models": ["multilingual-e5"],
                        },
                    }
                ],
            }
        resource = {
            "resource": {"current_path": "/Corpus/Transformadores/Pruebas eléctricas U5.pdf"},
            "evidence": {
                "evidence_id": "evidence:U5:page-4",
                "page": 4,
                "snippet": (
                    "La resistencia de aislamiento y la relación de transformación "
                    "fueron verificadas antes del llenado final."
                ),
            },
            "reasons": ["frase exacta", "evidencia PDF publicada"],
        }
        if request.operation == "search":
            return {
                **common,
                "schema": "neocortex.read-api/v1",
                "kind": "neocortex_scoped_search",
                "query": request.query,
                "scopes": [
                    {
                        "scope": "personal",
                        "status": "ok",
                        "result": {
                            "complete": True,
                            "result_window_full": False,
                            "hits": [resource],
                            "warnings": [],
                        },
                    },
                    {
                        "scope": "framework",
                        "status": "ok",
                        "result": {
                            "complete": True,
                            "result_window_full": False,
                            "hits": [],
                            "warnings": [],
                        },
                    },
                ],
            }
        if request.operation == "ask":
            return {
                **common,
                "schema": "neocortex.read-api/v1",
                "kind": "neocortex_scoped_context",
                "query": request.query,
                "scopes": [
                    {
                        "scope": "personal",
                        "status": "ok",
                        "context": {
                            "completeness": "complete",
                            "selected_hits": [resource],
                            "citation_ids": [{"citation_id": "K1"}],
                        },
                    }
                ],
            }
        return {
            **common,
            "schema": "neocortex.value-review/v1",
            "kind": "neocortex_scoped_value_review",
            "operation": "value-preview",
            "advisory_only": True,
            "mutation_authorized": False,
            "scopes": [
                {
                    "scope": "personal",
                    "status": "ready",
                    "report": {
                        "matched_count": 1,
                        "items": [
                            {
                                "state": "review_low_value",
                                "path": "/Corpus/Temporal/copia_antigua.txt",
                                "size_bytes": 2048,
                                "reasons": ["old_repeated_extracted_text_in_disposable_path"],
                                "uncertainties": ["usage_history_unavailable"],
                            }
                        ],
                    },
                }
            ],
        }


class _BlockingReadClient(_FixtureReadClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def execute(self, request: ReadRequest) -> dict[str, object]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("fixture read was not released")
        return super().execute(request)


@pytest.fixture(scope="module")
def application() -> QApplication:
    instance = QApplication.instance()
    if instance is not None and not isinstance(instance, QApplication):
        raise RuntimeError("A non-GUI Qt application already exists")
    app = instance or QApplication([])
    app.setStyleSheet(STYLESHEET)
    return app


def _window(tmp_path: Path, client: _FixtureReadClient) -> MainWindow:
    return MainWindow(
        initial_root=tmp_path / "Corpus",
        state_directory=tmp_path / "state",
        settings_path=tmp_path / "config" / "ui.ini",
        read_client=client,
    )


def _wait_for_read(application: QApplication, window: MainWindow, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while window._read_tasks.is_running and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()
    assert not window._read_tasks.is_running


def test_consultation_exposes_all_read_operations_without_mutation_controls(
    application: QApplication,
    tmp_path: Path,
) -> None:
    client = _FixtureReadClient()
    window = _window(tmp_path, client)
    window.show()
    application.processEvents()
    try:
        assert window.pages.count() == 5
        window._select_page(4)
        assert window.page_title.text() == "Consulta"

        visible_text = {label.text() for label in window.findChildren(QLabel)}
        assert "Solo lectura" in visible_text
        assert any("no autoriza mutaciones" in value for value in visible_text)
        consultation_page = window.pages.widget(4)
        assert consultation_page is not None
        assert not any(
            word in button.text().casefold()
            for button in consultation_page.findChildren(type(window.consult_button))
            for word in ("eliminar", "mover", "aplicar")
        )

        window._run_read_request()
        assert client.calls == []
        assert window.consult_status.property("state") == "warning"
        assert "Escribe una consulta" in window.consult_result_summary.text()

        window.consult_query.setText("  pruebas eléctricas U5  ")
        window._run_read_request()
        _wait_for_read(application, window)
        assert client.calls[-1] == ReadRequest(
            "search",
            scope="all",
            query="pruebas eléctricas U5",
            limit=10,
        )
        assert window.consult_result.isReadOnly()
        assert "Pruebas eléctricas U5.pdf" in window.consult_result.toPlainText()
        assert "sus scores no se mezclan" in window.consult_result.toPlainText()

        window.consult_operation.setCurrentIndex(1)
        window._run_read_request()
        _wait_for_read(application, window)
        assert client.calls[-1].limit == 8
        assert "[K1]" in window.consult_result.toPlainText()

        window.consult_operation.setCurrentIndex(2)
        assert not window.consult_query.isEnabled()
        assert not window.consult_limit.isEnabled()
        window._run_read_request()
        _wait_for_read(application, window)
        assert "personal-published-17" in window.consult_result.toPlainText()

        window.consult_operation.setCurrentIndex(3)
        assert not window.consult_query.isEnabled()
        assert window.consult_button.text() == "Revisar sin cambios"
        window._run_read_request()
        _wait_for_read(application, window)
        assert client.calls[-1].limit == 50
        assert "0 acciones aplicadas" in window.consult_result_summary.text()
        assert "no autoriza mover, archivar o borrar" in window.consult_result.toPlainText()
        assert [call.operation for call in client.calls] == [
            "search",
            "ask",
            "status",
            "review",
        ]
        window.consult_copy_button.click()
        assert QApplication.clipboard().text() == window.consult_result.toPlainText()
    finally:
        window.close()
        application.processEvents()


def test_consultation_page_renders_reproducibly_offscreen(
    application: QApplication,
    tmp_path: Path,
) -> None:
    client = _FixtureReadClient()
    window = _window(tmp_path, client)
    window.resize(1440, 900)
    window.show()
    window._select_page(4)
    window.consult_query.setText("pruebas eléctricas del transformador U5")
    window._run_read_request()
    _wait_for_read(application, window)
    try:
        configured = os.environ.get("NEOCORTEX_UI_CAPTURE_PATH")
        output = Path(configured) if configured else tmp_path / "gui-readonly.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        pixmap = window.grab()
        assert pixmap.width() == 1440
        assert pixmap.height() == 900
        assert pixmap.save(str(output), "PNG")
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        encoded = QImage(str(output))
        _assert_meaningful_consultation_capture(encoded, window)
        assert window.consult_result_title.text() == "Búsqueda de evidencia"
        assert "Pruebas eléctricas U5.pdf" in window.consult_result.toPlainText()
    finally:
        window.close()
        application.processEvents()


def test_consultation_runs_off_the_gui_thread_and_discards_cancelled_results(
    application: QApplication,
    tmp_path: Path,
) -> None:
    client = _BlockingReadClient()
    window = _window(tmp_path, client)
    window.show()
    window._select_page(4)
    window.consult_query.setText("consulta fría acotada")
    try:
        started_at = time.monotonic()
        window._run_read_request()
        assert time.monotonic() - started_at < 0.25
        deadline = time.monotonic() + 2
        while not client.started.is_set() and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        assert client.started.is_set()
        assert window.consult_status.property("state") == "running"
        assert not window.consult_button.isEnabled()
        assert window.consult_cancel_button.isEnabled()

        window._cancel_read_request()
        assert not window.consult_cancel_button.isEnabled()
        client.release.set()
        _wait_for_read(application, window)

        assert window.consult_status.property("state") == "warning"
        assert window.consult_result_title.text() == "Consulta cancelada"
        assert "No se creó" in window.consult_result.toPlainText()
        assert window.consult_button.isEnabled()
    finally:
        client.release.set()
        window.close()
        application.processEvents()


def test_window_defers_close_until_an_active_read_is_cancelled(
    application: QApplication,
    tmp_path: Path,
) -> None:
    client = _BlockingReadClient()
    window = _window(tmp_path, client)
    window.show()
    window._select_page(4)
    window.consult_query.setText("consulta protegida durante el cierre")
    try:
        window._run_read_request()
        deadline = time.monotonic() + 2
        while not client.started.is_set() and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        assert client.started.is_set()

        assert not window.close()
        assert window.isVisible()
        assert not window.consult_cancel_button.isEnabled()

        client.release.set()
        _wait_for_read(application, window)
        assert window.consult_result_title.text() == "Consulta cancelada"
        assert window.close()
    finally:
        client.release.set()
        window.close()
        application.processEvents()
