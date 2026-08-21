# region [00] Contexto del módulo
# Módulo: tests/test_ui_protocol.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import unittest

from neocortex.progress import ProgressEvent, ProgressMetric
from neocortex.interface.protocol.messages import (
    command_record,
    decode_message,
    encode_message,
    progress_payload,
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


if __name__ == "__main__":
    unittest.main()
# endregion [02]
