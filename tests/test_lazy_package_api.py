"""Isolated tests for the lazy operational-package facade."""


# region [01] Imports and stable expectations

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import _04_Nucleo_Operativo as operational

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXPORTS = [
    "ActionSummary",
    "ApplicationConfig",
    "AudioRoute",
    "AudioRouteConfig",
    "AudioRouteSummary",
    "CapabilityFailure",
    "CodeRelationEndpoint",
    "CodeRoute",
    "CodeRouteConfig",
    "CodeRouteSummary",
    "CodeSearchHit",
    "CodeSearchQuery",
    "CodeSearchRelation",
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "DetectedType",
    "DerivationRef",
    "DocxRoute",
    "DocxRouteConfig",
    "DocxRouteSummary",
    "FrameworkConfig",
    "FrameworkOrchestrator",
    "GlobalResourceCoordinator",
    "GlobalResourceLimits",
    "GlobalResourceSummary",
    "ImageRoute",
    "ImageRouteConfig",
    "ImageRouteSummary",
    "InputBinding",
    "MaterializationRef",
    "OutputBinding",
    "PdfDoctorReport",
    "PdfRoute",
    "PdfRouteConfig",
    "PdfRouteSummary",
    "PdfVerifyReport",
    "RouteAdapter",
    "RouteExecutionContext",
    "ReproducibilityClass",
    "PdfDerivedIndexer",
    "PdfDerivedSummary",
    "search_pdf_state",
    "search_docx_state",
    "doctor_pdf_runtime",
    "InitialRunResult",
    "OfficeRoute",
    "OfficeRouteConfig",
    "OfficeRouteSummary",
    "RouteOnlyRunResult",
    "SelfAnalysisRunResult",
    "StageDescriptor",
    "detect_content_type",
    "verify_pdf_state",
    "list_projects",
    "reconstruct_project",
    "search_code",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "EvidenceRef",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgePlan",
    "KnowledgeQuery",
    "KnowledgeQueryTelemetry",
    "KnowledgeSearchResult",
    "KnowledgeSearchService",
    "KnowledgeStateRootError",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "ResourceRef",
    "RetrievalMode",
    "RevisionRef",
    "WorkExecutionMode",
    "WorkOutcome",
    "WorkReceipt",
    "plan_knowledge_query",
]
EXPECTED_SOURCES = {
    "ActionSummary": "_04_Nucleo_Operativo.models",
    "ApplicationConfig": "_04_Nucleo_Operativo.application_config",
    "AudioRoute": "_04_Nucleo_Operativo.capabilities.formats.audio.route",
    "AudioRouteConfig": "_04_Nucleo_Operativo.capabilities.formats.audio.models",
    "AudioRouteSummary": "_04_Nucleo_Operativo.capabilities.formats.audio.models",
    "CapabilityFailure": "_04_Nucleo_Operativo.derivation_contracts",
    "CodeRelationEndpoint": "_04_Nucleo_Operativo.code_contracts",
    "CodeRoute": "_04_Nucleo_Operativo.code_route",
    "CodeRouteConfig": "_04_Nucleo_Operativo.code_contracts",
    "CodeRouteSummary": "_04_Nucleo_Operativo.code_contracts",
    "CodeSearchHit": "_04_Nucleo_Operativo.code_contracts",
    "CodeSearchQuery": "_04_Nucleo_Operativo.code_contracts",
    "CodeSearchRelation": "_04_Nucleo_Operativo.code_contracts",
    "DERIVATION_CONTRACT_SCHEMA_VERSION": ("_04_Nucleo_Operativo.derivation_contracts"),
    "DetectedType": "_04_Nucleo_Operativo.platform.shared.content_types",
    "DerivationRef": "_04_Nucleo_Operativo.derivation_contracts",
    "DocxRoute": "_04_Nucleo_Operativo.capabilities.formats.docx.route",
    "DocxRouteConfig": "_04_Nucleo_Operativo.capabilities.formats.docx.route",
    "DocxRouteSummary": "_04_Nucleo_Operativo.capabilities.formats.docx.route",
    "FrameworkConfig": "_04_Nucleo_Operativo.models",
    "FrameworkOrchestrator": "_04_Nucleo_Operativo.orchestrator",
    "GlobalResourceCoordinator": "_04_Nucleo_Operativo.global_resources",
    "GlobalResourceLimits": "_04_Nucleo_Operativo.global_resources",
    "GlobalResourceSummary": "_04_Nucleo_Operativo.global_resources",
    "ImageRoute": "_04_Nucleo_Operativo.capabilities.formats.image.route",
    "ImageRouteConfig": "_04_Nucleo_Operativo.capabilities.formats.image.route",
    "ImageRouteSummary": "_04_Nucleo_Operativo.capabilities.formats.image.route",
    "InputBinding": "_04_Nucleo_Operativo.derivation_contracts",
    "MaterializationRef": "_04_Nucleo_Operativo.derivation_contracts",
    "OutputBinding": "_04_Nucleo_Operativo.derivation_contracts",
    "PdfDoctorReport": "_04_Nucleo_Operativo.pdf_admin",
    "PdfRoute": "_04_Nucleo_Operativo.pdf_route",
    "PdfRouteConfig": "_04_Nucleo_Operativo.pdf_route",
    "PdfRouteSummary": "_04_Nucleo_Operativo.pdf_route",
    "PdfVerifyReport": "_04_Nucleo_Operativo.pdf_admin",
    "RouteAdapter": "_04_Nucleo_Operativo.route_registry",
    "RouteExecutionContext": "_04_Nucleo_Operativo.route_registry",
    "ReproducibilityClass": "_04_Nucleo_Operativo.derivation_contracts",
    "PdfDerivedIndexer": "_04_Nucleo_Operativo.pdf_derived",
    "PdfDerivedSummary": "_04_Nucleo_Operativo.pdf_derived",
    "search_pdf_state": "_04_Nucleo_Operativo.pdf_derived",
    "search_docx_state": "_04_Nucleo_Operativo.capabilities.formats.docx.route",
    "doctor_pdf_runtime": "_04_Nucleo_Operativo.pdf_admin",
    "InitialRunResult": "_04_Nucleo_Operativo.models",
    "OfficeRoute": "_04_Nucleo_Operativo.capabilities.formats.office.route",
    "OfficeRouteConfig": "_04_Nucleo_Operativo.capabilities.formats.office.route",
    "OfficeRouteSummary": "_04_Nucleo_Operativo.capabilities.formats.office.route",
    "RouteOnlyRunResult": "_04_Nucleo_Operativo.models",
    "SelfAnalysisRunResult": "_04_Nucleo_Operativo.models",
    "StageDescriptor": "_04_Nucleo_Operativo.derivation_contracts",
    "detect_content_type": "_04_Nucleo_Operativo.platform.shared.content_types",
    "verify_pdf_state": "_04_Nucleo_Operativo.pdf_admin",
    "list_projects": "_04_Nucleo_Operativo.code_projects",
    "reconstruct_project": "_04_Nucleo_Operativo.code_projects",
    "search_code": "_04_Nucleo_Operativo.code_search",
    "ContextBundle": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextContradictionRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextEntityRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextGraphBudget": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextPlanRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextPlanStepRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "ContextRelationRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "EvidenceRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgeHit": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgePhaseTiming": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgePlan": "_04_Nucleo_Operativo.knowledge_planner",
    "KnowledgeQuery": "_04_Nucleo_Operativo.knowledge_planner",
    "KnowledgeQueryTelemetry": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgeSearchResult": "_04_Nucleo_Operativo.knowledge_search",
    "KnowledgeSearchService": "_04_Nucleo_Operativo.knowledge_service",
    "KnowledgeStateRootError": "_04_Nucleo_Operativo.knowledge_snapshot",
    "KnowledgeSnapshot": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgeStatePaths": "_04_Nucleo_Operativo.knowledge_snapshot",
    "KnowledgeTelemetryClock": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgeTelemetryOperation": "_04_Nucleo_Operativo.knowledge_contracts",
    "KnowledgeTimingPhase": "_04_Nucleo_Operativo.knowledge_contracts",
    "ResourceRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "RetrievalMode": "_04_Nucleo_Operativo.knowledge_planner",
    "RevisionRef": "_04_Nucleo_Operativo.knowledge_contracts",
    "WorkExecutionMode": "_04_Nucleo_Operativo.derivation_contracts",
    "WorkOutcome": "_04_Nucleo_Operativo.derivation_contracts",
    "WorkReceipt": "_04_Nucleo_Operativo.derivation_contracts",
    "plan_knowledge_query": "_04_Nucleo_Operativo.knowledge_planner",
}

# endregion [01]


# region [02] Public facade compatibility


class LazyPackageApiTests(unittest.TestCase):
    def test_public_manifest_and_symbols_match_original_sources(self) -> None:
        self.assertEqual(operational.__all__, EXPECTED_EXPORTS)
        self.assertTrue(set(EXPECTED_EXPORTS).issubset(dir(operational)))

        for name, module_name in EXPECTED_SOURCES.items():
            with self.subTest(name=name):
                expected = getattr(importlib.import_module(module_name), name)
                self.assertIs(getattr(operational, name), expected)

        with self.assertRaises(AttributeError):
            operational.__getattr__("unsupported_public_symbol")

    def test_application_projection_boundary_has_no_runtime_owner_imports(
        self,
    ) -> None:
        script = textwrap.dedent(
            """
            import sys

            forbidden = {
                "_01_Enumeracion",
                "_02_Deduplicacion",
                "_04_Nucleo_Operativo.archive_route",
                "_04_Nucleo_Operativo.capabilities.formats.archive.route",
                "_04_Nucleo_Operativo.capabilities.formats.audio.models",
                "_04_Nucleo_Operativo.code_contracts",
                "_04_Nucleo_Operativo.docx_models",
                "_04_Nucleo_Operativo.capabilities.formats.docx.models",
                "_04_Nucleo_Operativo.global_resources",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.capabilities.formats.image.route",
                "_04_Nucleo_Operativo.models",
                "_04_Nucleo_Operativo.capabilities.formats.office.route",
                "_04_Nucleo_Operativo.pdf_route_models",
            }
            from _04_Nucleo_Operativo.application_config_projections import (
                audio_route_config_from_application,
                code_route_config_from_application,
                docx_route_config_from_application,
                global_resource_limits_from_application,
                image_route_config_from_application,
                office_route_config_from_application,
                pdf_route_config_from_application,
            )

            projections = (
                audio_route_config_from_application,
                code_route_config_from_application,
                docx_route_config_from_application,
                global_resource_limits_from_application,
                image_route_config_from_application,
                office_route_config_from_application,
                pdf_route_config_from_application,
            )
            if not all(callable(projection) for projection in projections):
                raise SystemExit("application projection boundary is incomplete")
            loaded = forbidden.intersection(sys.modules)
            if loaded:
                raise SystemExit(
                    "projection imports loaded: " + ",".join(sorted(loaded))
                )
            print("LIGHT_PROJECTIONS_OK")
            """
        )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("LIGHT_PROJECTIONS_OK", completed.stdout)

    def test_cold_import_and_help_do_not_load_content_routes(self) -> None:
        script = textwrap.dedent(
            """
            import sys

            forbidden = {
                "_04_Nucleo_Operativo.capabilities.formats.audio.models",
                "_04_Nucleo_Operativo.capabilities.formats.audio.route",
                "_04_Nucleo_Operativo.archive_route",
                "_04_Nucleo_Operativo.capabilities.formats.archive.route",
                "_04_Nucleo_Operativo.code_contracts",
                "_04_Nucleo_Operativo.code_route",
                "_04_Nucleo_Operativo.docx_models",
                "_04_Nucleo_Operativo.capabilities.formats.docx.models",
                "_04_Nucleo_Operativo.pdf_route",
                "_04_Nucleo_Operativo.pdf_route_models",
                "_04_Nucleo_Operativo.docx_route",
                "_04_Nucleo_Operativo.capabilities.formats.docx.route",
                "_04_Nucleo_Operativo.image_route",
                "_04_Nucleo_Operativo.capabilities.formats.image.route",
                "_04_Nucleo_Operativo.capabilities.formats.office.route",
                "_04_Nucleo_Operativo.knowledge_context",
                "_04_Nucleo_Operativo.knowledge_search",
                "_04_Nucleo_Operativo.knowledge_service",
                "_04_Nucleo_Operativo.knowledge_snapshot",
                "_05_Interfaz",
                "_05_Interfaz.app",
                "_05_Interfaz.main_window",
            }
            import Orquestador
            from _04_Nucleo_Operativo import ApplicationConfig, FrameworkConfig
            from _04_Nucleo_Operativo.application_config import (
                audio_route_config_from_application,
                code_route_config_from_application,
                docx_route_config_from_application,
                image_route_config_from_application,
                office_route_config_from_application,
                pdf_route_config_from_application,
            )

            if ApplicationConfig is not FrameworkConfig:
                raise SystemExit("application compatibility identity changed")
            if not callable(audio_route_config_from_application):
                raise SystemExit("audio configuration projection is unavailable")
            if not callable(code_route_config_from_application):
                raise SystemExit("code configuration projection is unavailable")
            if not callable(docx_route_config_from_application):
                raise SystemExit("DOCX configuration projection is unavailable")
            if not callable(image_route_config_from_application):
                raise SystemExit("image configuration projection is unavailable")
            if not callable(office_route_config_from_application):
                raise SystemExit("Office configuration projection is unavailable")
            if not callable(pdf_route_config_from_application):
                raise SystemExit("PDF configuration projection is unavailable")

            loaded_after_import = forbidden.intersection(sys.modules)
            if loaded_after_import:
                raise SystemExit(
                    "routes loaded by import: " + ",".join(sorted(loaded_after_import))
                )

            sys.argv = ["Orquestador.py", "--help"]
            try:
                Orquestador.main()
            except SystemExit as exc:
                if exc.code != 0:
                    raise
            else:
                raise SystemExit("--help did not terminate through argparse")

            loaded_after_help = forbidden.intersection(sys.modules)
            if loaded_after_help:
                raise SystemExit(
                    "routes loaded by help: " + ",".join(sorted(loaded_after_help))
                )
            print("LAZY_IMPORT_OK")
            """
        )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("LAZY_IMPORT_OK", completed.stdout)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
