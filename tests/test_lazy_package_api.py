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

from neocortex.api import public as operational

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXPORTS = [
    "ActionSummary",
    "ApplicationConfig",
    "AudioRoute",
    "AudioRouteConfig",
    "AudioRouteSummary",
    "CapabilityFailure",
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CurationPlanOutput",
    "CurationApplyOutput",
    "CurationReconcileOutput",
    "CurationPlanPage",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
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
    "StageDescriptor",
    "detect_content_type",
    "verify_pdf_state",
    "list_projects",
    "reconstruct_project",
    "search_code",
    "curation_plan_payload",
    "curation_review_payload",
    "curation_decide_payload",
    "curation_authorize_payload",
    "curation_apply_payload",
    "curation_reconcile_payload",
    "curation_scan_payload",
    "curation_verify_payload",
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
    "ActionSummary": "neocortex.runtime.models",
    "ApplicationConfig": "neocortex.runtime.config.application_config",
    "AudioRoute": "neocortex.capabilities.formats.audio.route",
    "AudioRouteConfig": "neocortex.capabilities.formats.audio.models",
    "AudioRouteSummary": "neocortex.capabilities.formats.audio.models",
    "CapabilityFailure": "neocortex.semantic.derivation_contracts",
    "CURATION_AUTHORIZATION_API_SCHEMA": "neocortex.api.curation_authorization_api",
    "CURATION_APPLY_API_SCHEMA": "neocortex.api.curation_application_api",
    "CURATION_APPLY_SCHEMA": "neocortex.api.curation_application_api",
    "CURATION_RECONCILE_API_SCHEMA": "neocortex.api.curation_application_api",
    "CURATION_DECISION_API_SCHEMA": "neocortex.api.curation_lifecycle_api",
    "CURATION_PLAN_API_SCHEMA": "neocortex.api.curation_api",
    "CURATION_REVIEW_API_SCHEMA": "neocortex.api.curation_lifecycle_api",
    "CURATION_SCAN_API_SCHEMA": "neocortex.api.curation_verification_api",
    "CURATION_VERIFY_API_SCHEMA": "neocortex.api.curation_verification_api",
    "CurationPlanOutput": "neocortex.api.curation_api",
    "CurationApplyOutput": "neocortex.api.curation_application_api",
    "CurationReconcileOutput": "neocortex.api.curation_application_api",
    "CurationPlanPage": "neocortex.curation.preview",
    "CurationScanOutput": "neocortex.api.curation_verification_api",
    "CurationSourceHead": "neocortex.curation.preview",
    "CurationVerifyOutput": "neocortex.api.curation_verification_api",
    "CodeRelationEndpoint": "neocortex.code.code_contracts",
    "CodeRoute": "neocortex.code.code_route",
    "CodeRouteConfig": "neocortex.code.code_contracts",
    "CodeRouteSummary": "neocortex.code.code_contracts",
    "CodeSearchHit": "neocortex.code.code_contracts",
    "CodeSearchQuery": "neocortex.code.code_contracts",
    "CodeSearchRelation": "neocortex.code.code_contracts",
    "DERIVATION_CONTRACT_SCHEMA_VERSION": ("neocortex.semantic.derivation_contracts"),
    "DetectedType": "neocortex.platform.content_types",
    "DerivationRef": "neocortex.semantic.derivation_contracts",
    "DocxRoute": "neocortex.capabilities.formats.docx.route",
    "DocxRouteConfig": "neocortex.capabilities.formats.docx.route",
    "DocxRouteSummary": "neocortex.capabilities.formats.docx.route",
    "FrameworkConfig": "neocortex.runtime.models",
    "FrameworkOrchestrator": "neocortex.runtime.orchestration.orchestrator",
    "GlobalResourceCoordinator": "neocortex.runtime.control.global_resources",
    "GlobalResourceLimits": "neocortex.runtime.control.global_resources",
    "GlobalResourceSummary": "neocortex.runtime.control.global_resources",
    "ImageRoute": "neocortex.capabilities.formats.image.route",
    "ImageRouteConfig": "neocortex.capabilities.formats.image.route",
    "ImageRouteSummary": "neocortex.capabilities.formats.image.route",
    "InputBinding": "neocortex.semantic.derivation_contracts",
    "MaterializationRef": "neocortex.semantic.derivation_contracts",
    "OutputBinding": "neocortex.semantic.derivation_contracts",
    "PdfDoctorReport": "neocortex.capabilities.formats.pdf.pdf_admin",
    "PdfRoute": "neocortex.capabilities.formats.pdf.pdf_route",
    "PdfRouteConfig": "neocortex.capabilities.formats.pdf.pdf_route",
    "PdfRouteSummary": "neocortex.capabilities.formats.pdf.pdf_route",
    "PdfVerifyReport": "neocortex.capabilities.formats.pdf.pdf_admin",
    "RouteAdapter": "neocortex.runtime.orchestration.route_registry",
    "RouteExecutionContext": "neocortex.runtime.orchestration.route_registry",
    "ReproducibilityClass": "neocortex.semantic.derivation_contracts",
    "PdfDerivedIndexer": "neocortex.capabilities.formats.pdf.pdf_derived",
    "PdfDerivedSummary": "neocortex.capabilities.formats.pdf.pdf_derived",
    "search_pdf_state": "neocortex.capabilities.formats.pdf.pdf_derived",
    "search_docx_state": "neocortex.capabilities.formats.docx.route",
    "doctor_pdf_runtime": "neocortex.capabilities.formats.pdf.pdf_admin",
    "InitialRunResult": "neocortex.runtime.models",
    "OfficeRoute": "neocortex.capabilities.formats.office.route",
    "OfficeRouteConfig": "neocortex.capabilities.formats.office.route",
    "OfficeRouteSummary": "neocortex.capabilities.formats.office.route",
    "RouteOnlyRunResult": "neocortex.runtime.models",
    "StageDescriptor": "neocortex.semantic.derivation_contracts",
    "detect_content_type": "neocortex.platform.content_types",
    "verify_pdf_state": "neocortex.capabilities.formats.pdf.pdf_admin",
    "list_projects": "neocortex.code.ingestion.code_projects",
    "reconstruct_project": "neocortex.code.ingestion.code_projects",
    "search_code": "neocortex.code.search.code_search",
    "curation_plan_payload": "neocortex.api.curation_api",
    "curation_review_payload": "neocortex.api.curation_lifecycle_api",
    "curation_decide_payload": "neocortex.api.curation_lifecycle_api",
    "curation_authorize_payload": "neocortex.api.curation_authorization_api",
    "curation_apply_payload": "neocortex.api.curation_application_api",
    "curation_reconcile_payload": "neocortex.api.curation_application_api",
    "curation_scan_payload": "neocortex.api.curation_verification_api",
    "curation_verify_payload": "neocortex.api.curation_verification_api",
    "ContextBundle": "neocortex.knowledge.knowledge_contracts",
    "ContextContradictionRef": "neocortex.knowledge.knowledge_contracts",
    "ContextEntityRef": "neocortex.knowledge.knowledge_contracts",
    "ContextGraphBudget": "neocortex.knowledge.knowledge_contracts",
    "ContextPlanRef": "neocortex.knowledge.knowledge_contracts",
    "ContextPlanStepRef": "neocortex.knowledge.knowledge_contracts",
    "ContextRelationRef": "neocortex.knowledge.knowledge_contracts",
    "EvidenceRef": "neocortex.knowledge.knowledge_contracts",
    "KnowledgeHit": "neocortex.knowledge.knowledge_contracts",
    "KnowledgePhaseTiming": "neocortex.knowledge.knowledge_contracts",
    "KnowledgePlan": "neocortex.knowledge.knowledge_planner",
    "KnowledgeQuery": "neocortex.knowledge.knowledge_planner",
    "KnowledgeQueryTelemetry": "neocortex.knowledge.knowledge_contracts",
    "KnowledgeSearchResult": "neocortex.knowledge.knowledge_search",
    "KnowledgeSearchService": "neocortex.knowledge.knowledge_service",
    "KnowledgeStateRootError": "neocortex.knowledge.knowledge_snapshot",
    "KnowledgeSnapshot": "neocortex.knowledge.knowledge_contracts",
    "KnowledgeStatePaths": "neocortex.knowledge.knowledge_snapshot",
    "KnowledgeTelemetryClock": "neocortex.knowledge.knowledge_contracts",
    "KnowledgeTelemetryOperation": "neocortex.knowledge.knowledge_contracts",
    "KnowledgeTimingPhase": "neocortex.knowledge.knowledge_contracts",
    "ResourceRef": "neocortex.knowledge.knowledge_contracts",
    "RetrievalMode": "neocortex.knowledge.knowledge_planner",
    "RevisionRef": "neocortex.knowledge.knowledge_contracts",
    "WorkExecutionMode": "neocortex.semantic.derivation_contracts",
    "WorkOutcome": "neocortex.semantic.derivation_contracts",
    "WorkReceipt": "neocortex.semantic.derivation_contracts",
    "plan_knowledge_query": "neocortex.knowledge.knowledge_planner",
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
                "neocortex.enumeration",
                "neocortex.deduplication",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.audio.models",
                "neocortex.code.code_contracts",
                "neocortex.capabilities.formats.docx.models",
                "neocortex.capabilities.formats.docx.models",
                "neocortex.runtime.control.global_resources",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.runtime.models",
                "neocortex.capabilities.formats.office.route",
                "neocortex.capabilities.formats.pdf.pdf_route_models",
            }
            from neocortex.runtime.config.application_config_projections import (
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
                "neocortex.capabilities.formats.audio.models",
                "neocortex.capabilities.formats.audio.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.capabilities.formats.archive.route",
                "neocortex.code.code_contracts",
                "neocortex.code.code_route",
                "neocortex.capabilities.formats.docx.models",
                "neocortex.capabilities.formats.docx.models",
                "neocortex.capabilities.formats.pdf.pdf_route",
                "neocortex.capabilities.formats.pdf.pdf_route_models",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.docx.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.image.route",
                "neocortex.capabilities.formats.office.route",
                "neocortex.knowledge.knowledge_context",
                "neocortex.knowledge.knowledge_search",
                "neocortex.knowledge.knowledge_service",
                "neocortex.knowledge.knowledge_snapshot",
            }
            import neocortex.interface.entrypoint as public_cli
            from neocortex.api.public import ApplicationConfig, FrameworkConfig
            from neocortex.runtime.config.application_config import (
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

            sys.argv = ["Neocortex", "--help"]
            try:
                result = public_cli.entrypoint()
            except SystemExit as exc:
                if exc.code != 0:
                    raise
            else:
                if result != 0:
                    raise SystemExit(f"--help returned {result}")

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
