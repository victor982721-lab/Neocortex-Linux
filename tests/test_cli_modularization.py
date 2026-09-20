"""Focused parser and configuration contracts for the modular CLI."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


class ModularParserTests(unittest.TestCase):
    def test_long_option_abbreviations_are_rejected(self) -> None:
        cases = (("--rou", "pdf"), ("--all", "--global-cpu-s", "8"))
        for arguments in cases:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_configuration_translation_preserves_cli_units(self) -> None:
        args = build_parser().parse_args(
            (
                "--route",
                "image",
                "--image-memory-budget-mb",
                "384",
                "--image-ocr-lang",
                "spa",
                "--MaxMB",
                "1.5",
            )
        )
        validate_arguments(args)
        config = framework_config_from_args(args)

        self.assertEqual(config.route, "image")
        self.assertEqual(config.image_memory_budget_bytes, 384 * 1024 * 1024)
        self.assertEqual(config.image_document_ocr_lang, "spa")
        self.assertEqual(config.pdf_max_file_bytes, 1_500_000)

    def test_retired_code_validation_options_are_not_registered(self) -> None:
        parser = build_parser()
        for option in (
            "--self-analysis",
            "--deep-test-selector",
        ):
            with self.subTest(option=option):
                self.assertNotIn(option, parser._option_string_actions)


if __name__ == "__main__":
    unittest.main()
