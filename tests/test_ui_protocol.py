# region [00] Contexto del módulo
# Módulo: tests/test_ui_protocol.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import unittest

from neocortex.progress import ProgressEvent, ProgressMetric
from neocortex.interface.protocol.messages import (
    WorkerMessageValidator,
    WorkerProtocolError,
    command_record,
    decode_message,
    encode_message,
    progress_payload,
    sanitize_text,
)
# endregion [01]

# region [02] Implementación


class UiProtocolTests(unittest.TestCase):
    def test_progress_event_round_trips_as_structured_utf8(self) -> None:
        event = ProgressEvent(
            operation="inventory",
            phase="scan",
            description="Clasificación técnica",
            completed=17,
            total=20,
            unit="archivos",
            metrics=(ProgressMetric("errors", 0),),
        )

        encoded = encode_message("progress", **progress_payload(event))
        decoded = decode_message(encoded)

        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["description"], "Clasificación técnica")
        self.assertEqual(decoded["completed"], 17)
        self.assertEqual(decoded["metrics"], {"errors": 0})

    def test_ordinary_process_output_is_not_protocol(self) -> None:
        self.assertIsNone(decode_message("ordinary diagnostic output\n"))

    def test_cancel_command_is_explicit(self) -> None:
        decoded = decode_message(command_record("cancel"))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["type"], "command")
        self.assertEqual(decoded["command"], "cancel")

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            command_record("terminate")

    def test_lifecycle_records_are_bounded_and_typed(self) -> None:
        record = decode_message(
            encode_message(
                "progress",
                operation="pdf",
                phase="extract",
                description="Lectura",
                completed=1,
                total=2,
                unit="documentos",
                finished=False,
                metrics={},
            )
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["worker_run_id"], "standalone")
        self.assertEqual(record["sequence"], 1)

        malformed = (
            '@neocortex-ui/v1 {"protocol":1,"type":"progress",'
            '"worker_run_id":"run","sequence":1,"completed":0}'
        )
        with self.assertRaises(WorkerProtocolError):
            decode_message(malformed)

    def test_validator_rejects_gaps_identity_changes_and_records_after_terminal(self) -> None:
        validator = WorkerMessageValidator()
        started = {
            "protocol": 1,
            "type": "started",
            "worker_run_id": "run-1",
            "sequence": 1,
            "root": "/tmp/corpus",
            "state_directory": "/tmp/state",
            "apply": False,
            "route": "pdf",
        }
        completed = {
            "protocol": 1,
            "type": "completed",
            "worker_run_id": "run-1",
            "sequence": 2,
            "run_id": 7,
            "files_checked": 1,
            "action_errors": 0,
            "route_errors": {},
            "organization_errors": False,
            "issues": 0,
            "completion_status": "completed",
            "exit_code": 0,
        }
        validator.accept(started)
        validator.accept(completed)
        with self.assertRaisesRegex(WorkerProtocolError, "terminal"):
            validator.accept({**completed, "sequence": 3})

        gap = WorkerMessageValidator()
        with self.assertRaisesRegex(WorkerProtocolError, "start"):
            gap.accept({**completed, "sequence": 1})

    def test_display_sanitization_removes_terminal_controls_and_bounds_text(self) -> None:
        value = sanitize_text("ok\x1b[31m\nforged\x1b[0m", limit=64)
        self.assertEqual(value, "ok forged")
        self.assertNotIn("\x1b", value)


if __name__ == "__main__":
    unittest.main()
# endregion [02]
