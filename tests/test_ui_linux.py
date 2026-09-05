"""Linux/KDE desktop mode contracts."""

from __future__ import annotations


import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from neocortex.interface.presentation.windows.main import MainWindow


TEST_CAPABILITIES = ('ui',)


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux desktop mode contract")


def test_linux_window_is_portable_non_elevated_and_non_mutating(tmp_path: Path) -> None:
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
        assert not window.apply_radio.isEnabled()
        assert "política Linux" in window.apply_radio.toolTip()
        assert not window._current_request().apply
    finally:
        window.close()
        application.processEvents()
