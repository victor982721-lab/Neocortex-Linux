# region [00] Contexto del módulo
# Módulo: tests/test_ui_status_repository.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from neocortex.interface.read.status import StatusRepository, StatusRepositoryError

import pytest


TEST_CAPABILITIES = ("base", 'ui')
pytestmark = pytest.mark.capability("base", 'ui')
# endregion [01]

# region [02] Implementación


class UiStatusRepositoryTests(unittest.TestCase):
    def test_recent_runs_is_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            database = state / "framework.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE initial_runs(
                    run_id INTEGER PRIMARY KEY, root TEXT, status TEXT,
                    current_phase TEXT, run_kind TEXT, started_ns INTEGER,
                    completed_ns INTEGER
                );
                CREATE TABLE run_actions(
                    run_id INTEGER PRIMARY KEY, files_checked INTEGER, errors INTEGER
                );
                CREATE TABLE route_runs(
                    run_id INTEGER, route_name TEXT, status TEXT,
                    started_ns INTEGER, completed_ns INTEGER,
                    current_phase TEXT, heartbeat_ns INTEGER,
                    source_run_id INTEGER, summary_json TEXT,
                    error_type TEXT, error_message TEXT
                );
                CREATE TABLE route_phase_runs(
                    run_id INTEGER, route_name TEXT, phase_name TEXT,
                    status TEXT, started_ns INTEGER, completed_ns INTEGER,
                    heartbeat_ns INTEGER, source_run_id INTEGER,
                    summary_json TEXT, error_type TEXT, error_message TEXT
                );
                CREATE TABLE run_events(
                    event_id INTEGER PRIMARY KEY, run_id INTEGER,
                    occurred_ns INTEGER, level TEXT, phase TEXT,
                    message TEXT, details_json TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO initial_runs VALUES(7,?,?,?,?,?,?)",
                ("C:/corpus", "completed", "completed", "initial", 10, 20),
            )
            connection.execute("INSERT INTO run_actions VALUES(7,120,0)")
            connection.execute(
                "INSERT INTO route_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    7,
                    "pdf",
                    "completed",
                    10,
                    20,
                    "completed",
                    20,
                    None,
                    json.dumps(
                        {
                            "errors": 0,
                            "cached_errors": 2,
                            "page_errors": 3,
                            "partial_documents": 1,
                            "document_timeouts": 1,
                            "catalog_errors": 1,
                        }
                    ),
                    None,
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO route_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    7,
                    "image",
                    "failed",
                    10,
                    20,
                    "failed",
                    20,
                    None,
                    json.dumps({"errors": 99}),
                    "RuntimeError",
                    "fixture",
                ),
            )
            connection.execute(
                "INSERT INTO run_events VALUES(1,7,10,'info','inventory','ok',?)",
                (json.dumps({"files": 125}),),
            )
            connection.commit()
            connection.close()

            repository = StatusRepository(state)
            runs = repository.recent_runs(5)

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].run_id, 7)
            self.assertEqual(runs[0].files_checked, 120)
            self.assertEqual(runs[0].route_errors, 7)
            self.assertEqual(repository.latest_event_details(7, "inventory"), {"files": 125})

            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE route_runs SET summary_json=? WHERE route_name='pdf'",
                (json.dumps({"errors": "not-a-counter"}),),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(StatusRepositoryError, "non-negative integer"):
                repository.recent_runs(5)

    def test_missing_database_is_an_empty_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(StatusRepository(Path(directory)).recent_runs(), ())

    def test_invalid_schema_is_reported_instead_of_looking_like_empty_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "framework.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.close()

            with self.assertRaisesRegex(
                StatusRepositoryError,
                "no initial_runs table",
            ):
                StatusRepository(Path(directory)).recent_runs()


if __name__ == "__main__":
    unittest.main()
# endregion [02]
