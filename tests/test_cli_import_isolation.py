"""Regression tests for lightweight CLI validation and direct dispatch."""


# region [01] Isolated-process harness

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(script: str, **environment_values: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(environment_values)
    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# endregion [01]


# region [02] Validation and direct-operation isolation


class CliImportIsolationTests(unittest.TestCase):
    def test_validation_does_not_import_route_registry_or_engines(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from neocortex.api.cli.cli_parser import build_parser
            from neocortex.api.cli.cli_validation import validate_arguments

            args = build_parser().parse_args(["--route", "pdf,image"])
            validate_arguments(args)
            forbidden = {
                "neocortex.runtime.orchestration.route_registry",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit("validation loaded: " + ",".join(sorted(loaded)))
            print("VALIDATION_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("VALIDATION_ISOLATED", completed.stdout)

    def test_importing_direct_dispatch_loads_no_document_backend(self) -> None:
        completed = _run_isolated(
            """
            import sys

            import neocortex.api.cli.cli_direct

            forbidden = {
                "neocortex.runtime.orchestration.route_registry",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.pdf.pdf_admin",
                "neocortex.capabilities.formats.pdf.pdf_derived_queries",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit("direct import loaded: " + ",".join(sorted(loaded)))
            print("DIRECT_IMPORT_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DIRECT_IMPORT_ISOLATED", completed.stdout)

    def test_knowledge_status_does_not_load_search_or_image_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary) / "missing-state"
            completed = _run_isolated(
                """
                import os
                import sys
                from pathlib import Path

                from neocortex.api.cli.cli_app import main

                state_directory = Path(os.environ["NEOCORTEX_TEST_STATE"])
                result = main(
                    (
                        "--state-directory",
                        str(state_directory),
                        "--knowledge-status",
                        "--knowledge-json",
                    )
                )
                if result != 0:
                    raise SystemExit(f"unexpected Knowledge status: {result}")
                forbidden = {
                    "neocortex.knowledge.knowledge_search",
                    "neocortex.semantic.semantic_backends",
                    "neocortex.semantic.semantic_lexical",
                    "neocortex.semantic.semantic_search_service",
                    "neocortex.semantic.semantic_service",
                    "neocortex.semantic.semantic_sources",
                }
                loaded = forbidden.intersection(sys.modules)
                loaded_pillow = {
                    name
                    for name in sys.modules
                    if name == "PIL" or name.startswith("PIL.")
                }
                if loaded:
                    raise SystemExit(
                        "Knowledge status loaded search runtime: "
                        + ",".join(sorted(loaded))
                    )
                if loaded_pillow:
                    raise SystemExit(
                        "Knowledge status loaded Pillow: "
                        + ",".join(sorted(loaded_pillow))
                    )
                if state_directory.exists():
                    raise SystemExit("Knowledge status created missing state")
                print("KNOWLEDGE_STATUS_ISOLATED")
                """,
                NEOCORTEX_TEST_STATE=str(state_directory),
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("KNOWLEDGE_STATUS_ISOLATED", completed.stdout)

    def test_pdf_search_does_not_import_docx_or_route_engines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary) / "missing-state"
            completed = _run_isolated(
                """
                import os
                import sys
                from pathlib import Path

                from neocortex.api.cli.cli_app import main

                state_directory = Path(os.environ["NEOCORTEX_TEST_STATE"])
                sys.argv = [
                    "Neocortex",
                    "--state-directory",
                    str(state_directory),
                    "--pdf-search",
                    "transformador",
                ]
                if main() != 2:
                    raise SystemExit("unexpected PDF search status")
                forbidden = {
                    "neocortex.runtime.orchestration.route_registry",
                    "neocortex.capabilities.formats.archive.route",
                    "neocortex.capabilities.formats.archive.route",
                    "neocortex.capabilities.formats.pdf.pdf_route",
                    "neocortex.capabilities.formats.pdf.pdf_admin",
                    "neocortex.capabilities.formats.docx.route",
                    "neocortex.capabilities.formats.docx.route",
                    "neocortex.capabilities.formats.image.route",
                    "neocortex.capabilities.formats.image.route",
                }
                loaded = forbidden.intersection(sys.modules)
                if loaded:
                    raise SystemExit("PDF search loaded: " + ",".join(sorted(loaded)))
                if state_directory.exists():
                    raise SystemExit("PDF search created missing state")
                print("PDF_SEARCH_ISOLATED")
                """,
                NEOCORTEX_TEST_STATE=str(state_directory),
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("PDF_SEARCH_ISOLATED", completed.stdout)

    def test_docx_search_does_not_import_pdf_or_route_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_directory = Path(temporary) / "missing-state"
            completed = _run_isolated(
                """
                import os
                import sys
                from pathlib import Path

                from neocortex.api.cli.cli_app import main

                state_directory = Path(os.environ["NEOCORTEX_TEST_STATE"])
                sys.argv = [
                    "Neocortex",
                    "--state-directory",
                    str(state_directory),
                    "--docx-search",
                    "interruptor",
                ]
                if main() != 2:
                    raise SystemExit("unexpected DOCX search status")
                forbidden = {
                    "neocortex.runtime.orchestration.route_registry",
                    "neocortex.capabilities.formats.archive.route",
                    "neocortex.capabilities.formats.archive.route",
                    "neocortex.capabilities.formats.pdf.pdf_route",
                    "neocortex.capabilities.formats.pdf.pdf_admin",
                    "neocortex.capabilities.formats.pdf.pdf_derived_queries",
                    "neocortex.capabilities.formats.image.route",
                    "neocortex.capabilities.formats.image.route",
                }
                loaded = forbidden.intersection(sys.modules)
                if loaded:
                    raise SystemExit("DOCX search loaded: " + ",".join(sorted(loaded)))
                if state_directory.exists():
                    raise SystemExit("DOCX search created missing state")
                print("DOCX_SEARCH_ISOLATED")
                """,
                NEOCORTEX_TEST_STATE=str(state_directory),
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DOCX_SEARCH_ISOLATED", completed.stdout)


# endregion [02]


# region [03] Compatibility reexports


class RouteSelectionCompatibilityTests(unittest.TestCase):
    def test_heavy_registry_reexports_the_stable_selection_contract(self) -> None:
        from neocortex.runtime.orchestration import route_registry, route_selection

        self.assertIs(
            route_registry.BUILTIN_ROUTE_ORDER,
            route_selection.BUILTIN_ROUTE_ORDER,
        )
        self.assertIs(
            route_registry.normalize_route_selection,
            route_selection.normalize_route_selection,
        )
        self.assertEqual(
            route_registry.normalize_route_selection(
                " IMAGE,pDf ", route_registry.BUILTIN_ROUTE_ORDER
            ),
            ("image", "pdf"),
        )


# endregion [03]


if __name__ == "__main__":
    unittest.main()
