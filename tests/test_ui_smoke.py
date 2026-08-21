# region [00] Contexto del módulo
# Módulo: tests/test_ui_smoke.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
# endregion [01]

# region [02] Implementación

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from neocortex.interface.presentation.windows.main import MainWindow
from neocortex.interface.application.request import ROUTE_ORDER


class UiSmokeTests(unittest.TestCase):
    application: ClassVar[QApplication]

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        if instance is not None and not isinstance(instance, QApplication):
            raise RuntimeError("A non-GUI Qt application already exists")
        cls.application = instance or QApplication([])

    def test_window_builds_without_starting_operational_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow(
                initial_root=root,
                state_directory=root / "state",
                settings_path=root / "ui.ini",
            )
            window.show()
            self.application.processEvents()

            self.assertEqual(window.pages.count(), 5)
            self.assertEqual(tuple(window.route_toggles), ROUTE_ORDER)
            self.assertTrue(window.analysis_radio.isChecked())
            self.assertFalse(window.controller.is_running)
            request = window._current_request()
            self.assertFalse(request.apply)
            self.assertEqual(request.routes, ROUTE_ORDER)

            window.apply_radio.setChecked(True)
            window.scope_combo.setCurrentIndex(1)
            self.application.processEvents()
            isolated = window._current_request()
            self.assertTrue(isolated.route_only)
            self.assertFalse(isolated.apply)
            self.assertTrue(window.analysis_radio.isChecked())
            self.assertFalse(window.apply_radio.isEnabled())

            window._update_progress(
                {
                    "operation": "test",
                    "phase": "fixture",
                    "description": "Fixture",
                    "completed": 1,
                    "total": 2,
                    "unit": "archivos",
                    "finished": False,
                    "metrics": {"errors": 0},
                }
            )
            self.assertEqual(len(window._progress_items), 1)
            window._on_worker_message(
                {
                    "type": "heartbeat",
                    "elapsed_seconds": 65,
                    "active": [
                        {
                            "operation": "pdf",
                            "phase": "profile",
                            "description": "Perfilando PDF",
                            "completed": 0,
                            "total": 26,
                            "unit": "PDF",
                            "finished": False,
                            "metrics": {"in_flight": 4, "remaining": 26},
                        }
                    ],
                }
            )
            self.assertEqual(window.activity_title.text(), "Ahora: Perfilando PDF")
            self.assertIn("1 min 05 s", window.activity_detail.text())
            self.assertIn("4 tareas internas activas", window.activity_detail.text())
            self.assertEqual(window.activity_progress.maximum(), 0)
            window._on_worker_message(
                {
                    "type": "completed",
                    "run_id": 8,
                    "files_checked": 10,
                    "action_errors": 0,
                    "route_errors": {"pdf": 2},
                    "issues": 2,
                    "completion_status": "completed_with_issues",
                    "exit_code": 0,
                }
            )
            self.assertEqual(window.live_status.property("state"), "warning")
            window.close()
            self.application.processEvents()

    def test_invalid_durable_schema_is_visibly_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            connection = sqlite3.connect(state / "framework.sqlite3")
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.close()

            window = MainWindow(
                initial_root=root,
                state_directory=state,
                settings_path=root / "ui.ini",
            )
            window.show()
            self.application.processEvents()

            self.assertEqual(window.header_status.property("state"), "failed")
            self.assertEqual(window.overview_run_card.value_label.text(), "No disponible")
            self.assertIn("Estado durable no disponible", window.session_log.toPlainText())
            window.close()
            self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
# endregion [02]
