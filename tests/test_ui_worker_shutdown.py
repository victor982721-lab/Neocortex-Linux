# region [00] Contexto del módulo
# Módulo: tests/test_ui_worker_shutdown.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from neocortex.interface.protocol.messages import decode_message
from neocortex.interface.protocol.worker import _summary_payload
# endregion [01]

# region [02] Implementación


class UiWorkerShutdownTests(unittest.TestCase):
    def test_early_failure_exits_cleanly_while_command_pipe_remains_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_root = Path(directory) / "missing"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "neocortex.interface.protocol.worker",
                    "--root",
                    str(missing_root),
                    "--route",
                    "none",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                exit_code = process.wait(timeout=15)
                stdout = process.stdout.read() if process.stdout is not None else b""
                stderr = process.stderr.read() if process.stderr is not None else b""
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        records = [
            decoded for line in stdout.splitlines() if (decoded := decode_message(line)) is not None
        ]
        self.assertEqual(exit_code, 1, stderr.decode("utf-8", errors="replace"))
        self.assertIn("failed", {record["type"] for record in records})
        self.assertNotIn(b"Fatal Python error", stderr)
        self.assertNotIn(b"_enter_buffered_busy", stderr)

    def test_invalid_preparation_emits_failed_record_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "neocortex.interface.protocol.worker",
                    "--root",
                    directory,
                    "--route",
                    "all",
                    "--route-only",
                    "--apply",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                capture_output=True,
                timeout=15,
            )

        records = [
            decoded
            for line in process.stdout.splitlines()
            if (decoded := decode_message(line)) is not None
        ]
        terminal = [record for record in records if record["type"] == "failed"]
        self.assertEqual(process.returncode, 2, process.stderr.decode(errors="replace"))
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["error_type"], "InvalidArguments")
        self.assertEqual(terminal[0]["stage"], "preparation")
        expected = "remove --apply" if os.name == "nt" else "linux_mutation_backend_unavailable"
        self.assertIn(expected, terminal[0]["detail"])
        self.assertNotIn("traceback", terminal[0])

    def test_summary_projects_cached_partial_and_catalog_issues(self) -> None:
        summary = _summary_payload(
            SimpleNamespace(
                run_id=9,
                actions=SimpleNamespace(files_checked=4, errors=0),
                route_results={
                    "pdf": SimpleNamespace(
                        errors=0,
                        cached_errors=2,
                        page_errors=3,
                        partial_documents=1,
                        document_timeouts=1,
                        catalog_errors=1,
                    )
                },
            )
        )

        self.assertEqual(summary["route_errors"], {"pdf": 6})


if __name__ == "__main__":
    unittest.main()
# endregion [02]
