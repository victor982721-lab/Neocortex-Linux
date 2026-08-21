# region [00] Contexto del módulo
# Módulo: tests/test_ui_run_request.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from _04_Nucleo_Operativo.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)
from neocortex.interface.application.request import ROUTE_ORDER, RunRequest
# endregion [01]

# region [02] Implementación


class UiRunRequestTests(unittest.TestCase):
    def test_all_visible_routes_are_serialized_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                root=root,
                routes=tuple(reversed(ROUTE_ORDER)),
                apply=True,
            )

            with patch("neocortex.interface.application.request.os.name", "nt"):
                arguments = request.cli_arguments()

            self.assertNotIn("--all", arguments)
            self.assertIn("--apply", arguments)
            selected = arguments[arguments.index("--route") + 1]
            self.assertEqual(selected, ",".join(ROUTE_ORDER))
            self.assertIn("code", selected.split(","))
            self.assertEqual(
                normalize_route_selection(selected, BUILTIN_ROUTE_ORDER),
                ROUTE_ORDER,
            )

    def test_subset_preserves_canonical_route_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                root=root,
                routes=("image", "pdf"),
            )

            arguments = request.cli_arguments()

            route_index = arguments.index("--route")
            self.assertEqual(arguments[route_index + 1], "pdf,image")
            self.assertNotIn("--apply", arguments)

    def test_route_only_requires_a_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = RunRequest(
                root=root,
                routes=(),
                route_only=True,
            )
            with self.assertRaisesRegex(ValueError, "al menos una ruta"):
                request.validated()

    def test_route_only_all_visible_routes_never_expands_to_global_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = RunRequest(
                root=Path(directory),
                routes=ROUTE_ORDER,
                route_only=True,
            )

            arguments = request.cli_arguments()

            self.assertNotIn("--all", arguments)
            selected = arguments[arguments.index("--route") + 1]
            self.assertEqual(selected, ",".join(ROUTE_ORDER))
            self.assertNotEqual(selected, "all")
            self.assertIn("code", selected.split(","))
            self.assertIn("--route-only", arguments)

    def test_route_only_rejects_apply_before_starting_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = RunRequest(
                root=Path(directory),
                routes=("pdf",),
                apply=True,
                route_only=True,
            )

            with self.assertRaisesRegex(ValueError, "siempre no destructiva"):
                request.validated()

    def test_linux_rejects_apply_before_starting_a_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = RunRequest(
                root=Path(directory),
                routes=("pdf",),
                apply=True,
            )
            with (
                patch("neocortex.interface.application.request.os.name", "posix"),
                self.assertRaisesRegex(ValueError, "linux_mutation_backend_unavailable"),
            ):
                request.validated()


if __name__ == "__main__":
    unittest.main()
# endregion [02]
