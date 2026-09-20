"""Canonical lazy SDK facade over Knowledge and curation lifecycle contracts."""


# region [01] Isolated-process harness and stable surface

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import neocortex.sdk as sdk

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LEGACY_EXPORTS = (
    "DERIVATION_CONTRACT_SCHEMA_VERSION",
    "CapabilityFailure",
    "ContextBundle",
    "ContextContradictionRef",
    "ContextEntityRef",
    "ContextGraphBudget",
    "ContextPlanRef",
    "ContextPlanStepRef",
    "ContextRelationRef",
    "DerivationRef",
    "EvidenceRef",
    "InputBinding",
    "KnowledgeHit",
    "KnowledgePhaseTiming",
    "KnowledgePlan",
    "KnowledgeQuery",
    "KnowledgeQueryTelemetry",
    "KnowledgeSearchResult",
    "KnowledgeSearchService",
    "KnowledgeSnapshot",
    "KnowledgeStatePaths",
    "KnowledgeStateRootError",
    "KnowledgeTelemetryClock",
    "KnowledgeTelemetryOperation",
    "KnowledgeTimingPhase",
    "MaterializationRef",
    "OutputBinding",
    "ReproducibilityClass",
    "ResourceRef",
    "RetrievalMode",
    "RevisionRef",
    "StageDescriptor",
    "WorkExecutionMode",
    "WorkOutcome",
    "WorkReceipt",
    "plan_knowledge_query",
)

CURATION_EXPORTS = (
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CURATION_RECOVERY_STATUS_API_SCHEMA",
    "CURATION_RESTORE_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    "CurationApplyOutput",
    "CurationPlanOutput",
    "CurationPlanPage",
    "CurationReconcileOutput",
    "CurationRecoveryStatusOutput",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
    "curation_plan_payload",
    "curation_review_payload",
    "curation_decide_payload",
    "curation_authorize_payload",
    "curation_apply_payload",
    "curation_reconcile_payload",
    "curation_recovery_status_payload",
    "curation_restore_payload",
    "curation_restore_preview_payload",
    "curation_checkpoint_create_payload",
    "curation_checkpoint_resume_payload",
    "curation_checkpoint_status_payload",
)

DIAGNOSTIC_EXPORTS = (
    "CONTENT_DIAGNOSTICS_SCHEMA",
    "CONTENT_DIAGNOSTICS_V2_SCHEMA",
)

BUDGET_EXPORTS = (
    "KNOWLEDGE_READ_BUDGET_SCHEMA",
    "KnowledgeReadBudget",
    "KnowledgeReadBudgetExceeded",
)

READ_EXPORTS = (
    "status_payload",
    "search_payload",
    "context_payload",
    "content_diagnostics_payload",
    "content_diagnostics_v2_payload",
    "evidence_payload",
    "operational_query_payload",
    "asset_health_payload",
    "lineage_payload",
    "knowledge_search_projection_payload",
)

EXPECTED_EXPORTS = (
    "CURATION_APPLY_API_SCHEMA",
    "CURATION_APPLY_SCHEMA",
    "CURATION_AUTHORIZATION_API_SCHEMA",
    "CURATION_DECISION_API_SCHEMA",
    "CURATION_PLAN_API_SCHEMA",
    "CURATION_RECONCILE_API_SCHEMA",
    "CURATION_RECOVERY_STATUS_API_SCHEMA",
    "CURATION_RESTORE_API_SCHEMA",
    "CURATION_REVIEW_API_SCHEMA",
    "CURATION_SCAN_API_SCHEMA",
    "CURATION_VERIFY_API_SCHEMA",
    "CURATION_CHECKPOINT_CREATE_API_SCHEMA",
    "CURATION_CHECKPOINT_RESUME_API_SCHEMA",
    "CURATION_CHECKPOINT_STATUS_API_SCHEMA",
    *LEGACY_EXPORTS[:1],
    *DIAGNOSTIC_EXPORTS,
    *LEGACY_EXPORTS[1:9],
    "CurationApplyOutput",
    "CurationPlanOutput",
    "CurationPlanPage",
    "CurationReconcileOutput",
    "CurationRecoveryStatusOutput",
    "CurationScanOutput",
    "CurationSourceHead",
    "CurationVerifyOutput",
    *LEGACY_EXPORTS[9:10],
    "EVIDENCE_PROJECTION_SCHEMA",
    "EVIDENCE_PROJECTION_VERSION",
    "EvidenceProjection",
    *LEGACY_EXPORTS[10:12],
    *LEGACY_EXPORTS[12:13],
    "KNOWLEDGE_EVIDENCE_PROJECTION_SCHEMA",
    "KNOWLEDGE_EVIDENCE_PROJECTION_VERSION",
    "KnowledgeEvidenceProjection",
    "KnowledgeSearchProjection",
    *LEGACY_EXPORTS[13:17],
    *BUDGET_EXPORTS,
    *LEGACY_EXPORTS[17:25],
    "evidence_projection_payload",
    "evidence_search_projection_payload",
    "knowledge_evidence_projection_payload",
    "project_knowledge_hit",
    "project_knowledge_search",
    *LEGACY_EXPORTS[25:-1],
    "LIFECYCLE_ENVELOPE_SCHEMA",
    "LIFECYCLE_STATUS_KIND",
    "LIFECYCLE_STATUS_OPERATION",
    "LifecycleStatusContractError",
    "RUN_CHECKPOINT_SCHEMA",
    "lifecycle_status_payload",
    "curation_apply_payload",
    "curation_authorize_payload",
    "curation_decide_payload",
    "curation_plan_payload",
    "curation_reconcile_payload",
    "curation_recovery_status_payload",
    "curation_restore_payload",
    "curation_restore_preview_payload",
    "curation_review_payload",
    "curation_scan_payload",
    "curation_verify_payload",
    "curation_checkpoint_create_payload",
    "curation_checkpoint_resume_payload",
    "curation_checkpoint_status_payload",
    *READ_EXPORTS,
    LEGACY_EXPORTS[-1],
    "RunBudget",
    "RunManifest",
    "RunStatus",
    "read_run_status",
    "read_run_status_json",
)

FUTURE_ENDPOINTS = (
    "compare_revisions",
    "explain_hit",
    "get_resource",
    "get_revision",
    "read_evidence",
    "recent_changes",
)


def _run_isolated(
    script: str,
    **environment_values: str,
) -> subprocess.CompletedProcess[str]:
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


# region [02] Public identity and scope


def test_sdk_manifest_is_the_public_read_surface() -> None:
    assert sdk.__all__ == EXPECTED_EXPORTS
    assert set(EXPECTED_EXPORTS).issubset(dir(sdk))
    assert set(CURATION_EXPORTS).issubset(sdk.__all__)

    for endpoint in FUTURE_ENDPOINTS:
        assert endpoint not in sdk.__all__
        with pytest.raises(AttributeError):
            getattr(sdk, endpoint)


def test_sdk_exports_preserve_legacy_object_identity() -> None:
    from neocortex.api import public as legacy

    for name in LEGACY_EXPORTS:
        assert getattr(sdk, name) is getattr(legacy, name), name

    service_type = sdk.KnowledgeSearchService
    assert callable(service_type.status)
    assert callable(service_type.search)
    assert callable(service_type.context)


def test_sdk_lazily_exports_the_fixed_root_curation_contract() -> None:
    import inspect

    from neocortex.api import curation_api
    from neocortex.curation.preview import CurationPlanPage

    assert sdk.CURATION_PLAN_API_SCHEMA == "neocortex.curation-plan/v1"
    assert sdk.CurationPlanOutput is curation_api.CurationPlanOutput
    assert sdk.CurationPlanPage is CurationPlanPage
    assert sdk.curation_plan_payload is curation_api.curation_plan_payload
    assert set(inspect.signature(sdk.curation_plan_payload).parameters) == {
        "limit",
        "cursor",
        "request_id",
        "budget",
    }


def test_sdk_service_annotations_are_runtime_resolvable_without_search_import() -> None:
    from typing import Any, get_type_hints

    service_type = sdk.KnowledgeSearchService
    assert get_type_hints(service_type.status)["return"] is sdk.KnowledgeSnapshot
    assert get_type_hints(service_type.search)["return"] is Any
    assert get_type_hints(service_type.context)["return"] is sdk.ContextBundle


# endregion [02]


# region [03] Cold import and absent-state status


def test_sdk_cold_import_resolves_no_operational_or_optional_engine() -> None:
    completed = _run_isolated(
        """
        import sys

        before = set(sys.modules)
        import neocortex.sdk as sdk
        loaded = set(sys.modules) - before

        forbidden_prefixes = (
            "PIL",
            "PySide6",
            "ctranslate2",
            "cv2",
            "fastembed",
            "faster_whisper",
            "fitz",
            "numpy",
            "onnxruntime",
            "pdfminer",
            "pytesseract",
        )
        unexpected = sorted(
            name
            for name in loaded
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in forbidden_prefixes
            )
        )
        if unexpected:
            raise SystemExit("SDK cold import loaded: " + ",".join(unexpected))

        eager_exports = sorted(set(sdk.__all__).intersection(sdk.__dict__))
        if eager_exports:
            raise SystemExit("SDK eagerly resolved: " + ",".join(eager_exports))
        if not set(sdk.__all__).issubset(dir(sdk)):
            raise SystemExit("SDK dir() omitted public exports")
        print("SDK_COLD_IMPORT_OK")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert "SDK_COLD_IMPORT_OK" in completed.stdout


def test_sdk_status_tolerates_absent_optional_engines_without_creating_state(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "missing-state"
    completed = _run_isolated(
        """
        import importlib.abc
        import os
        import sys
        from pathlib import Path

        blocked_roots = {
            "PIL",
            "PySide6",
            "ctranslate2",
            "cv2",
            "fastembed",
            "faster_whisper",
            "fitz",
            "numpy",
            "onnxruntime",
            "pdfminer",
            "pytesseract",
        }

        class OptionalEngineBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                del path, target
                if fullname.partition(".")[0] in blocked_roots:
                    raise ModuleNotFoundError(
                        f"blocked optional engine: {fullname}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, OptionalEngineBlocker())

        from neocortex.sdk import KnowledgeSearchService, KnowledgeStatePaths

        state_directory = Path(os.environ["NEOCORTEX_TEST_STATE"])
        if state_directory.exists():
            raise SystemExit("status probe state unexpectedly exists")

        service = KnowledgeSearchService(
            KnowledgeStatePaths.from_directory(state_directory)
        )
        snapshot = service.status()
        states = {owner.state.value for owner in snapshot.owners}
        if len(snapshot.owners) != 10 or states != {"absent"}:
            raise SystemExit(f"unexpected absent-state snapshot: {states!r}")
        if state_directory.exists():
            raise SystemExit("SDK status created missing state")

        forbidden_modules = {
            "neocortex.knowledge.knowledge_context",
            "neocortex.knowledge.knowledge_search",
            "neocortex.semantic.semantic_backends",
            "neocortex.semantic.semantic_preparation",
            "neocortex.semantic.semantic_search_service",
            "neocortex.semantic.semantic_service",
        }
        loaded = sorted(forbidden_modules.intersection(sys.modules))
        if loaded:
            raise SystemExit("SDK status loaded search engines: " + ",".join(loaded))
        print("SDK_ABSENT_STATUS_OK")
        """,
        NEOCORTEX_TEST_STATE=str(state_directory),
    )

    assert completed.returncode == 0, completed.stderr
    assert "SDK_ABSENT_STATUS_OK" in completed.stdout


def test_sdk_knowledge_paths_preserve_legacy_constructor_without_video(
    tmp_path: Path,
) -> None:
    from neocortex.sdk import KnowledgeSearchService, KnowledgeStatePaths

    state = tmp_path / "legacy-sdk-state"
    paths = KnowledgeStatePaths(
        inventory=state / "dedup.sqlite3",
        framework=state / "framework.sqlite3",
        catalog=state / "document_catalog.sqlite3",
        pdf=state / "pdf.sqlite3",
        docx=state / "docx.sqlite3",
        office=state / "office.sqlite3",
        audio=state / "audio.sqlite3",
        image=state / "image.sqlite3",
        semantic=state / "semantic.sqlite3",
    )

    assert paths.video is None
    snapshot = KnowledgeSearchService(paths).status()
    assert {owner.owner for owner in snapshot.owners} == {
        "inventory",
        "framework",
        "catalog",
        "pdf",
        "docx",
        "office",
        "audio",
        "image",
        "semantic",
    }
    assert {owner.state.value for owner in snapshot.owners} == {"absent"}
    assert not state.exists()


# endregion [03]
