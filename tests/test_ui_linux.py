"""Linux/KDE desktop mode contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from _05_Interfaz.main_window import MainWindow


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux desktop mode contract")


def test_linux_window_is_portable_non_elevated_and_non_mutating(tmp_path: Path) -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow(
        initial_root=tmp_path / "Corpus con espacio",
        state_directory=tmp_path / "state",
        settings_path=tmp_path / "config" / "ui.ini",
    )
    window.show()
    application.processEvents()
    try:
        visible_text = {label.text() for label in window.findChildren(QLabel)}
        assert "Modo portátil Linux" in visible_text
        assert window.route_toggles["code"].text() == "Código"
        assert window._portable_linux is True
        assert window._execution_elevated is False
        assert window.start_button.text() == "Iniciar ejecución"
        assert window.start_button.isEnabled()
        assert not window.apply_radio.isEnabled()
        assert "backend seguro de Windows" in window.apply_radio.toolTip()
        assert not window._current_request().apply
    finally:
        window.close()
        application.processEvents()
