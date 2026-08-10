"""Regression tests for lazy content-route loading."""


# region [01] Isolated-process harness

from __future__ import annotations

import os
import subprocess
import sys
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


# region [02] Registry and orchestrator isolation


class RouteRegistryIsolationTests(unittest.TestCase):
    def test_building_registry_does_not_load_route_engines(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from _04_Nucleo_Operativo.route_registry import (
                builtin_route_registry,
            )

            registry = builtin_route_registry()
            if tuple(registry) != (
                "pdf", "docx", "office", "archive", "text", "audio", "video", "image", "code"
            ):
                raise SystemExit(f"unexpected registry: {tuple(registry)!r}")
            forbidden = {
                "_02_Deduplicacion",
                "_04_Nucleo_Operativo.global_resources",
                "_04_Nucleo_Operativo.pdf_route",
                "_04_Nucleo_Operativo.docx_route",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.office_route",
                "_04_Nucleo_Operativo.archive_route",
                "_04_Nucleo_Operativo.text_route",
                "_04_Nucleo_Operativo.audio_route",
                "_04_Nucleo_Operativo.video_route",
                "_04_Nucleo_Operativo.code_route",
                "_04_Nucleo_Operativo.code_analyzers",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit("registry loaded: " + ",".join(sorted(loaded)))
            print("REGISTRY_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("REGISTRY_ISOLATED", completed.stdout)

    def test_deferred_route_reexport_loads_only_its_engine(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from _04_Nucleo_Operativo import route_registry

            engines = {
                "_04_Nucleo_Operativo.pdf_route",
                "_04_Nucleo_Operativo.docx_route",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.office_route",
                "_04_Nucleo_Operativo.audio_route",
                "_04_Nucleo_Operativo.video_route",
            }
            if engines.intersection(sys.modules):
                raise SystemExit("route engines loaded before deferred access")
            if "PdfRoute" not in dir(route_registry):
                raise SystemExit("deferred export absent from dir()")

            from _04_Nucleo_Operativo.route_registry import PdfRoute
            from _04_Nucleo_Operativo.pdf_route import PdfRoute as OriginalPdfRoute

            if PdfRoute is not OriginalPdfRoute:
                raise SystemExit("deferred export changed the original symbol")
            loaded = engines.intersection(sys.modules)
            if loaded != {"_04_Nucleo_Operativo.pdf_route"}:
                raise SystemExit("deferred export loaded: " + ",".join(loaded))
            print("DEFERRED_EXPORT_ISOLATED")
            """
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DEFERRED_EXPORT_ISOLATED", completed.stdout)

    def test_route_none_orchestrator_does_not_load_route_engines(self) -> None:
        completed = _run_isolated(
            """
            import sys

            from _04_Nucleo_Operativo.models import FrameworkConfig
            from _04_Nucleo_Operativo.orchestrator import FrameworkOrchestrator

            orchestrator = FrameworkOrchestrator(FrameworkConfig(route="none"))
            if orchestrator.selected_routes:
                raise SystemExit(
                    f"unexpected selected routes: {orchestrator.selected_routes!r}"
                )
            forbidden = {
                "_04_Nucleo_Operativo.pdf_route",
                "_04_Nucleo_Operativo.docx_route",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.audio_route",
                "_04_Nucleo_Operativo.video_route",
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

            from _04_Nucleo_Operativo.models import FrameworkConfig
            from _04_Nucleo_Operativo.orchestrator import FrameworkOrchestrator

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
                "_04_Nucleo_Operativo.pdf_route",
                "_04_Nucleo_Operativo.docx_route",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.audio_route",
            }
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    "selection loaded: " + ",".join(sorted(loaded))
                )
            print("SELECTION_ISOLATED:" + expression)
        """

        for expression in ("pdf", "docx,office,audio,video,image"):
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

            from _04_Nucleo_Operativo import route_registry

            route_name = os.environ["NEOCORTEX_TEST_ROUTE"]
            module_names = {
                "pdf": "_04_Nucleo_Operativo.pdf_route",
                "docx": "_04_Nucleo_Operativo.docx_route",
                "image": "_04_Nucleo_Operativo.image_route",
                "office": "_04_Nucleo_Operativo.office_route",
                "audio": "_04_Nucleo_Operativo.audio_route",
                "video": "_04_Nucleo_Operativo.video_route",
                "text": "_04_Nucleo_Operativo.text_route",
            }
            class_names = {
                "pdf": ("PdfRoute", "PdfRouteConfig"),
                "docx": ("DocxRoute", "DocxRouteConfig"),
                "image": ("ImageRoute", "ImageRouteConfig"),
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
            if route_name in {"pdf", "image"}:
                dedup_module = types.ModuleType("_02_Deduplicacion")
                dedup_module.DedupIndex = FakeDedupIndex
                sys.modules["_02_Deduplicacion"] = dedup_module
            else:
                resources_module = types.ModuleType(
                    "_04_Nucleo_Operativo.global_resources"
                )
                resources_module.CoordinatedMemoryGate = object
                sys.modules[
                    "_04_Nucleo_Operativo.global_resources"
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

            other_modules = set(module_names.values()) - {module_names[route_name]}
            loaded = other_modules.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    f"{route_name} adapter loaded: " + ",".join(sorted(loaded))
                )
            if route_name == "pdf":
                unrelated_dependency = (
                    "_04_Nucleo_Operativo.global_resources"
                )
            elif route_name == "image":
                unrelated_dependency = None
            else:
                unrelated_dependency = "_02_Deduplicacion"
            if unrelated_dependency is not None and unrelated_dependency in sys.modules:
                raise SystemExit(
                    f"{route_name} adapter loaded: {unrelated_dependency}"
                )
            print("ADAPTER_ISOLATED:" + route_name)
        """

        for route_name in ("pdf", "docx", "office", "text", "audio", "video", "image"):
            with self.subTest(route=route_name):
                completed = _run_isolated(script, NEOCORTEX_TEST_ROUTE=route_name)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"ADAPTER_ISOLATED:{route_name}", completed.stdout)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
