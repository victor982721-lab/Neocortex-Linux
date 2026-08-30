"""Focused compatibility tests for the modular NeoCortex CLI."""


# region [01] Imports

from __future__ import annotations

import io
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from neocortex.deduplication import InventoryError
from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments

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
        validate_arguments(args)
        config = framework_config_from_args(args)

        self.assertEqual(config.route, "image")
        self.assertEqual(config.image_memory_budget_bytes, 384 * 1024 * 1024)
        self.assertEqual(config.image_document_ocr_lang, "spa")
        self.assertEqual(config.pdf_max_file_bytes, 1_500_000)

    def test_all_help_exposes_exact_validation_receipt_reuse(self) -> None:
        action = build_parser()._option_string_actions["--all"]

        self.assertIn("validation receipt", action.help or "")


# endregion [02]


# region [03] Integrated CLI tests


class CliIntegrationTests(unittest.TestCase):
    def test_all_runs_integrated_semantic_after_framework(self) -> None:
        result = SimpleNamespace(actions=None)
        with (
            patch(
                "neocortex.code.code_validation_receipts."
                "load_current_code_validation_receipt",
                return_value=SimpleNamespace(
                    status="reused",
                    reason="exact_validation_receipt_reused",
                    validation_digest="sha256:" + "a" * 64,
                    head_sha="b" * 40,
                ),
            ) as receipt,
            patch("neocortex.api.cli.cli_app.run_framework", return_value=result),
            patch("neocortex.api.cli.cli_reporting.print_reports"),
            patch(
                "neocortex.api.cli.cli_reporting.has_organization_errors",
                return_value=False,
            ),
            patch(
                "neocortex.api.cli.cli_reporting.has_strict_route_errors",
                return_value=False,
            ),
            patch(
                "neocortex.api.cli.cli_semantic.run_integrated_all_semantic_index",
                return_value=0,
            ) as semantic,
        ):
            self.assertEqual(main(["--all"]), 0)

        semantic.assert_called_once()
        self.assertTrue(semantic.call_args.args[0].all)
        receipt.assert_called_once_with()

    def test_all_does_not_start_the_canonical_self_analysis_service(self) -> None:
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
                    "neocortex.code.code_validation_receipts."
                    "load_current_code_validation_receipt",
                    return_value=SimpleNamespace(
                        status="missing",
                        reason="receipt_missing",
                        validation_digest=None,
                        head_sha=None,
                    ),
                ),
                patch(
                    "neocortex.api.cli.cli_app.run_framework",
                    side_effect=record_framework,
                ),
                patch("neocortex.api.cli.cli_reporting.print_reports"),
                patch(
                    "neocortex.api.cli.cli_reporting.has_organization_errors",
                    return_value=False,
                ),
                patch(
                    "neocortex.api.cli.cli_reporting.has_strict_route_errors",
                    return_value=False,
                ),
                patch(
                    "neocortex.api.cli.cli_semantic.run_integrated_all_semantic_index",
                    return_value=0,
                ),
            ):
                self.assertEqual(main(["--all", "--root", str(corpus)]), 0)

        self.assertEqual(observed_modes, [False])

    def test_all_checks_receipt_before_a_missing_corpus_is_reported(self) -> None:
        events: list[str] = []
        stderr = io.StringIO()

        def load_receipt():
            events.append("receipt")
            return SimpleNamespace(
                status="missing",
                reason="receipt_missing",
                validation_digest=None,
                head_sha=None,
            )

        def fail_corpus(*_args, **_kwargs):
            events.append("corpus")
            raise InventoryError("cannot inspect inventory root: missing")

        with (
            patch(
                "neocortex.code.code_validation_receipts."
                "load_current_code_validation_receipt",
                side_effect=load_receipt,
            ),
            patch(
                "neocortex.api.cli.cli_app.run_framework",
                side_effect=fail_corpus,
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(["--all"]), 2)

        self.assertEqual(events, ["receipt", "corpus"])
        self.assertIn("ERROR corpus_unavailable:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_all_strict_receipt_failure_stops_before_corpus(self) -> None:
        with (
            patch(
                "neocortex.code.code_validation_receipts."
                "load_current_code_validation_receipt",
                return_value=SimpleNamespace(
                    status="stale",
                    reason="receipt_head_changed",
                    validation_digest=None,
                    head_sha=None,
                ),
            ),
            patch("neocortex.api.cli.cli_app.run_framework") as framework,
        ):
            self.assertEqual(main(["--all", "--require-fresh-self-analysis"]), 2)

        framework.assert_not_called()

    def test_all_refreshes_self_analysis_only_when_explicit(self) -> None:
        result = SimpleNamespace(actions=None)
        with (
            patch(
                "neocortex.api.cli.cli_app._run_integrated_self_analysis",
                return_value=0,
            ) as self_analysis,
            patch("neocortex.api.cli.cli_app.run_framework", return_value=result),
            patch("neocortex.api.cli.cli_reporting.print_reports"),
            patch(
                "neocortex.api.cli.cli_reporting.has_organization_errors",
                return_value=False,
            ),
            patch(
                "neocortex.api.cli.cli_reporting.has_strict_route_errors",
                return_value=False,
            ),
            patch(
                "neocortex.api.cli.cli_semantic.run_integrated_all_semantic_index",
                return_value=0,
            ),
        ):
            self.assertEqual(main(["--all", "--refresh-self-analysis"]), 0)

        self_analysis.assert_called_once_with()


# endregion [03]


if __name__ == "__main__":
    unittest.main()
