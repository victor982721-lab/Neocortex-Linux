# region [00] Contexto del módulo
# Módulo: tests/test_app_paths.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neocortex.runtime.config.app_paths import (
    default_generated_artifact_directories,
    default_state_directory,
    local_application_data_directory,
    program_installation_directory,
    source_repository_directory,
    stable_launcher_path,
)
from neocortex.api.cli.cli_parser import build_parser
# endregion [01]

# region [02] Implementación


class ApplicationPathTests(unittest.TestCase):
    def test_canonical_user_paths_share_the_expected_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            profile = temporary_root / "profile"
            local_appdata = profile / "AppData" / "Local"
            with (
                patch.dict(os.environ, {"LOCALAPPDATA": str(local_appdata)}),
                patch.object(Path, "home", return_value=profile),
            ):
                self.assertEqual(
                    source_repository_directory(),
                    profile / "Neocortex" / "Repository",
                )
                self.assertEqual(
                    program_installation_directory(),
                    local_appdata / "Programs" / "Neocortex",
                )
                self.assertEqual(
                    default_generated_artifact_directories(),
                    tuple(
                        project / generated
                        for project in (
                            profile / "Neocortex" / "Repository",
                            profile / "MTF",
                            profile / "Documentos" / "ANDRITZ" / "Bitacoras-EPS",
                        )
                        for generated in ("build", "dist", "wheelhouse")
                    ),
                )
                self.assertEqual(
                    stable_launcher_path(),
                    local_appdata / "Programs" / "Neocortex" / "bin" / "Neocortex.exe",
                )

    def test_relative_local_appdata_is_rejected(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": "relative-local-appdata"}):
            with self.assertRaisesRegex(
                ValueError,
                "local application data path must be absolute",
            ):
                local_application_data_directory()

    def test_state_uses_fixed_local_appdata_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}):
                base = Path(directory) / "Neocortex"
                self.assertEqual(default_state_directory(), base / "state")

    def test_cli_keeps_fixed_default_and_only_mentions_protected_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}):
                parser = build_parser()
                parsed = parser.parse_args([])
                self.assertEqual(
                    parsed.state_directory,
                    Path(directory) / "Neocortex" / "state",
                )
                help_text = parser.format_help()
                self.assertNotIn("--self-analysis", parser._option_string_actions)
                self.assertNotIn("\n  --state-directory", help_text)


if __name__ == "__main__":
    unittest.main()
# endregion [02]
