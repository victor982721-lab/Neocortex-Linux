"""Regression tests for lazy content-route loading."""


# region [01] Isolated-process harness

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import pytest


TEST_CAPABILITIES = ('base', 'documents')

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


# region [02] Registry and orchestrator isolation


class RouteRegistryIsolationTests(unittest.TestCase):
    def test_building_registry_does_not_load_route_engines(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from neocortex.runtime.orchestration.route_registry import (
                builtin_route_registry,
            )

            registry = builtin_route_registry()
            if tuple(registry) != (
                "pdf", "docx", "office", "archive", "text", "audio", "video", "image"
            ):
                raise SystemExit(f"unexpected registry: {tuple(registry)!r}")
            forbidden = {
                "neocortex.deduplication",
                "neocortex.runtime.control.global_resources",
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.office.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.text.text_route",
                "neocortex.capabilities.formats.audio.route",
                "neocortex.capabilities.formats.video.route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit("registry loaded: " + ",".join(sorted(loaded)))
            print("REGISTRY_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("REGISTRY_ISOLATED", completed.stdout)

    @pytest.mark.capability('documents')
    def test_registry_does_not_reexport_route_engines(self) -> None:
        completed = _run_isolated(
            """
            from neocortex.runtime.orchestration import route_registry

            if "PdfRoute" in dir(route_registry):
                raise SystemExit("route registry still reexports PdfRoute")
            from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute
            if PdfRoute.__module__ != "neocortex.capabilities.formats.pdf.pdf_route":
                raise SystemExit("canonical route identity changed")
            print("REGISTRY_CANONICAL_ONLY")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("REGISTRY_CANONICAL_ONLY", completed.stdout)

    def test_route_none_orchestrator_does_not_load_route_engines(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from neocortex.runtime.models import FrameworkConfig
            from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

            orchestrator = FrameworkOrchestrator(FrameworkConfig(route="none"))
            if orchestrator.selected_routes:
                raise SystemExit(
                    f"unexpected selected routes: {orchestrator.selected_routes!r}"
                )
            forbidden = {
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.audio.route",
                "neocortex.capabilities.formats.video.route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    "route none loaded: " + ",".join(sorted(loaded))
                )
            print("ROUTE_NONE_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ROUTE_NONE_ISOLATED", completed.stdout)

    def test_selecting_routes_does_not_eagerly_load_their_engines(self) -> None:
        script = """
            import os
            import sys

            from neocortex.runtime.models import FrameworkConfig
            from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

            expression = os.environ["NEOCORTEX_TEST_SELECTION"]
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(route=expression)
            )
            expected = tuple(expression.split(","))
            if orchestrator.selected_routes != expected:
                raise SystemExit(
                    f"unexpected selected routes: {orchestrator.selected_routes!r}"
                )
            forbidden = {
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.audio.route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    "selection loaded: " + ",".join(sorted(loaded))
                )
            print("SELECTION_ISOLATED:" + expression)
        """

        for expression in ("pdf", "docx,office,archive,audio,video,image"):
            with self.subTest(selection=expression):
                completed = _run_isolated(script, NEOCORTEX_TEST_SELECTION=expression)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"SELECTION_ISOLATED:{expression}", completed.stdout)

    def test_adapter_with_engine_double_does_not_load_other_engines(self) -> None:
        script = """
            import os
            import sys
            import types
            from pathlib import Path

            from neocortex.runtime.orchestration import route_registry

            route_name = os.environ["NEOCORTEX_TEST_ROUTE"]
            module_names = {
                "pdf": "neocortex.capabilities.formats.pdf.pdf_route",
                "docx": "neocortex.capabilities.formats.docx.route",
                "image": "neocortex.capabilities.formats.image.route",
                "archive": "neocortex.capabilities.formats.archive.route",
                "office": "neocortex.capabilities.formats.office.route",
                "audio": "neocortex.capabilities.formats.audio.route",
                "video": "neocortex.capabilities.formats.video.route",
                "text": "neocortex.capabilities.formats.text.text_route",
            }
            canonical_module_names = {
                "docx": "neocortex.capabilities.formats.docx.route",
                "image": "neocortex.capabilities.formats.image.route",
                "archive": "neocortex.capabilities.formats.archive.route",
            }
            class_names = {
                "pdf": ("PdfRoute", "PdfRouteConfig"),
                "docx": ("DocxRoute", "DocxRouteConfig"),
                "image": ("ImageRoute", "ImageRouteConfig"),
                "archive": ("ArchiveRoute", "ArchiveRouteConfig"),
                "office": ("OfficeRoute", "OfficeRouteConfig"),
                "audio": ("AudioRoute", "AudioRouteConfig"),
                "video": ("VideoRoute", "VideoRouteConfig"),
                "text": ("TextRoute", "TextRouteConfig"),
            }

            calls = []

            class FakeRouteConfig:
                def __init__(self, *args, **kwargs):
                    calls.append(("config", args, kwargs))

            class FakeRoute:
                def __init__(self, *args, **kwargs):
                    calls.append(("route", args, kwargs))

                def run(self):
                    calls.append(("run", (), {}))
                    return route_name

            class FakeDedupIndex:
                def __init__(self, *args, **kwargs):
                    calls.append(("dedup", args, kwargs))

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

            class FakeFrameworkConfig:
                def __getattr__(self, name):
                    if name.endswith("_database"):
                        return Path("unused") / f"{name}.sqlite3"
                    return None

            selected_module = types.ModuleType(module_names[route_name])
            route_class_name, config_class_name = class_names[route_name]
            setattr(selected_module, route_class_name, FakeRoute)
            setattr(selected_module, config_class_name, FakeRouteConfig)
            sys.modules[module_names[route_name]] = selected_module
            canonical_name = canonical_module_names.get(route_name)
            if canonical_name is not None:
                sys.modules[canonical_name] = selected_module
            if route_name in {"pdf", "image"}:
                dedup_module = types.ModuleType("neocortex.deduplication")
                dedup_module.DedupIndex = FakeDedupIndex
                sys.modules["neocortex.deduplication"] = dedup_module
            else:
                resources_module = types.ModuleType(
                    "neocortex.runtime.control.global_resources"
                )
                resources_module.CoordinatedMemoryGate = object
                sys.modules[
                    "neocortex.runtime.control.global_resources"
                ] = resources_module

            context = route_registry.RouteExecutionContext(
                config=FakeFrameworkConfig(),
                root=Path("unused-root"),
                framework_state=object(),
                run_id=1,
                scan_id=2,
                progress=None,
                resource_coordinator=None,
                cancellation=object(),
            )
            result = route_registry.builtin_route_registry()[route_name].execute(
                context
            )
            if result != route_name or not any(call[0] == "run" for call in calls):
                raise SystemExit(f"adapter did not execute its double: {calls!r}")

            selected_names = {module_names[route_name]}
            if canonical_name is not None:
                selected_names.add(canonical_name)
            other_modules = (
                set(module_names.values()) | set(canonical_module_names.values())
            ) - selected_names
            loaded = other_modules.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    f"{route_name} adapter loaded: " + ",".join(sorted(loaded))
                )
            if route_name == "pdf":
                unrelated_dependency = (
                    "neocortex.runtime.control.global_resources"
                )
            elif route_name == "image":
                unrelated_dependency = None
            else:
                unrelated_dependency = "neocortex.deduplication"
            if unrelated_dependency is not None and unrelated_dependency in sys.modules:
                raise SystemExit(
                    f"{route_name} adapter loaded: {unrelated_dependency}"
                )
            print("ADAPTER_ISOLATED:" + route_name)
        """

        for route_name in (
            "pdf",
            "docx",
            "office",
            "archive",
            "text",
            "audio",
            "video",
            "image",
        ):
            with self.subTest(route=route_name):
                completed = _run_isolated(script, NEOCORTEX_TEST_ROUTE=route_name)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"ADAPTER_ISOLATED:{route_name}", completed.stdout)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
