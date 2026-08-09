"""Focused compatibility tests for the modular NeoCortex CLI."""


# region [01] Imports

from __future__ import annotations

import io
import runpy
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import Orquestador
from _02_Deduplicacion import InventoryError
from _04_Nucleo_Operativo.cli_app import main, run_framework
from _04_Nucleo_Operativo.cli_config import framework_config_from_args
from _04_Nucleo_Operativo.cli_parser import (
    ExplicitArgumentParser,
    build_parser,
    decimal_megabytes,
)
from _04_Nucleo_Operativo.cli_reporting import has_strict_route_errors

# endregion [01]


# region [02] Parser and configuration tests


class ModularParserTests(unittest.TestCase):
    def test_long_option_abbreviations_are_rejected(self) -> None:
        cases = (
            ["--rou", "pdf"],
            ["--all", "--global-cpu-s", "8"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_configuration_translation_preserves_cli_units(self) -> None:
        args = build_parser().parse_args(
            [
                "--route",
                "image",
                "--image-memory-budget-mb",
                "384",
                "--image-ocr-lang",
                "spa",
                "--MaxMB",
                "1.5",
            ]
        )
        Orquestador._validate_arguments(args)
        config = framework_config_from_args(args)

        self.assertEqual(config.route, "image")
        self.assertEqual(config.image_memory_budget_bytes, 384 * 1024 * 1024)
        self.assertEqual(config.image_document_ocr_lang, "spa")
        self.assertEqual(config.pdf_max_file_bytes, 1_500_000)

    def test_all_help_exposes_integrated_protected_self_analysis(self) -> None:
        action = build_parser()._option_string_actions["--all"]

        self.assertIn("protected self-analysis", action.help or "")


# endregion [02]


# region [03] Stable shim tests


class OrchestratorShimTests(unittest.TestCase):
    def test_historical_symbols_reexport_modular_implementations(self) -> None:
        self.assertIs(Orquestador._ExplicitArgumentParser, ExplicitArgumentParser)
        self.assertIs(Orquestador._decimal_megabytes, decimal_megabytes)
        self.assertIs(Orquestador._parser, build_parser)
        self.assertIs(Orquestador._run_framework, run_framework)
        self.assertIs(Orquestador._has_strict_route_errors, has_strict_route_errors)
        self.assertIs(Orquestador.main, main)
        self.assertEqual(Orquestador._parser().prog, "Neocortex")

    def test_process_shim_preserves_keyboard_interrupt_exit(self) -> None:
        stderr = io.StringIO()
        shim_path = Path(Orquestador.__file__)
        with (
            patch(
                "_04_Nucleo_Operativo.cli_app.main",
                side_effect=KeyboardInterrupt,
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(str(shim_path), run_name="__main__")

        self.assertEqual(raised.exception.code, 130)
        self.assertIn("Ejecución cancelada por el usuario.", stderr.getvalue())

    def test_all_runs_integrated_semantic_after_framework(self) -> None:
        result = SimpleNamespace(actions=None)
        with (
            patch(
                "_04_Nucleo_Operativo.cli_app._run_integrated_self_analysis",
                return_value=0,
            ) as self_analysis,
            patch("_04_Nucleo_Operativo.cli_app.run_framework", return_value=result),
            patch("_04_Nucleo_Operativo.cli_reporting.print_reports"),
            patch(
                "_04_Nucleo_Operativo.cli_reporting.has_organization_errors",
                return_value=False,
            ),
            patch(
                "_04_Nucleo_Operativo.cli_reporting.has_strict_route_errors",
                return_value=False,
            ),
            patch(
                "_04_Nucleo_Operativo.cli_semantic.run_integrated_all_semantic_index",
                return_value=0,
            ) as semantic,
        ):
            self.assertEqual(main(["--all"]), 0)

        semantic.assert_called_once()
        self.assertTrue(semantic.call_args.args[0].all)
        self_analysis.assert_called_once_with()

    def test_all_reuses_the_canonical_self_analysis_service(self) -> None:
        result = SimpleNamespace(actions=None)
        observed_modes: list[bool] = []

        def record_framework(args, **_kwargs):
            observed_modes.append(bool(args.self_analysis))
            return result

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = temporary / "Repository"
            repository.mkdir()
            corpus = temporary / "Corpus"
            corpus.mkdir()
            with (
                patch(
                    "_04_Nucleo_Operativo.app_paths.source_repository_directory",
                    return_value=repository,
                ),
                patch(
                    "_04_Nucleo_Operativo.app_paths.self_analysis_data_directory",
                    return_value=temporary / "self-analysis-state",
                ),
                patch(
                    "_04_Nucleo_Operativo.cli_app.run_framework",
                    side_effect=record_framework,
                ),
                patch("_04_Nucleo_Operativo.cli_reporting.print_reports"),
                patch(
                    "_04_Nucleo_Operativo.cli_reporting.has_organization_errors",
                    return_value=False,
                ),
                patch(
                    "_04_Nucleo_Operativo.cli_reporting.has_strict_route_errors",
                    return_value=False,
                ),
                patch(
                    "_04_Nucleo_Operativo.cli_semantic.run_integrated_all_semantic_index",
                    return_value=0,
                ),
            ):
                self.assertEqual(main(["--all", "--root", str(corpus)]), 0)

        self.assertEqual(observed_modes, [True, False])

    def test_all_runs_self_analysis_before_a_missing_corpus_is_reported(self) -> None:
        events: list[str] = []
        stderr = io.StringIO()

        def run_self_analysis() -> int:
            events.append("self-analysis")
            return 0

        def fail_corpus(*_args, **_kwargs):
            events.append("corpus")
            raise InventoryError("cannot inspect inventory root: missing")

        with (
            patch(
                "_04_Nucleo_Operativo.cli_app._run_integrated_self_analysis",
                side_effect=run_self_analysis,
            ),
            patch(
                "_04_Nucleo_Operativo.cli_app.run_framework",
                side_effect=fail_corpus,
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(["--all"]), 2)

        self.assertEqual(events, ["self-analysis", "corpus"])
        self.assertIn("ERROR corpus_unavailable:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_all_propagates_integrated_self_analysis_failure(self) -> None:
        result = SimpleNamespace(actions=None)
        with (
            patch(
                "_04_Nucleo_Operativo.cli_app._run_integrated_self_analysis",
                return_value=2,
            ),
            patch("_04_Nucleo_Operativo.cli_app.run_framework", return_value=result),
            patch("_04_Nucleo_Operativo.cli_reporting.print_reports"),
            patch(
                "_04_Nucleo_Operativo.cli_reporting.has_organization_errors",
                return_value=False,
            ),
            patch(
                "_04_Nucleo_Operativo.cli_reporting.has_strict_route_errors",
                return_value=False,
            ),
            patch(
                "_04_Nucleo_Operativo.cli_semantic.run_integrated_all_semantic_index",
                return_value=0,
            ),
        ):
            self.assertEqual(main(["--all"]), 2)


# endregion [03]


if __name__ == "__main__":
    unittest.main()
