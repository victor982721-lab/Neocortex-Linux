"""Linux/KDE desktop mode contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from neocortex.interface.presentation.windows.main import MainWindow


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux desktop mode contract")


def test_linux_window_exposes_verified_mutation_mode(tmp_path: Path) -> None:
    application = QApplication.instance() or QApplication([])
    root = tmp_path / "Corpus con espacio"
    root.mkdir()
    window = MainWindow(
        initial_root=root,
        state_directory=tmp_path / "state",
        settings_path=tmp_path / "config" / "ui.ini",
    )
    window.show()
    application.processEvents()
    try:
        visible_text = {label.text() for label in window.findChildren(QLabel)}
        assert "Modo Linux" in visible_text
        assert window.route_toggles["code"].text() == "Código"
        assert window.route_toggles["archive"].text() == "ZIP"
        assert window.start_button.text() == "Iniciar ejecución"
        assert window.start_button.isEnabled()
        assert window.apply_radio.isEnabled()
        assert not window._current_request().apply
    finally:
        window.close()
        application.processEvents()
