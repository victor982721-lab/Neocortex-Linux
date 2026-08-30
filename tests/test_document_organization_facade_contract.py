# region [00] Contexto del módulo
# Módulo: tests/test_document_organization_facade_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import inspect

from neocortex.documents import document_organization as facade
from neocortex.documents import document_organization_application as application
from neocortex.documents import document_organization_models as models
from neocortex.documents import document_organization_planning as planning
# endregion [01]

# region [02] Implementación


EXPECTED_PUBLIC_NAMES = (
    "DEFAULT_ORGANIZATION_DIRECTORY_NAME",
    "ORGANIZATION_APPLY_BATCH_SIZE",
    "ORGANIZATION_FILENAME_LIMIT",
    "ORGANIZATION_PROGRESS_INTERVAL",
    "OrganizationApplyProgress",
    "OrganizationApplyProgressCallback",
    "OrganizationApplySummary",
    "OrganizationPlanSummary",
    "OrganizationPlanView",
    "apply_all_document_organization",
    "apply_document_organization",
    "default_organization_root",
    "list_organization_plans",
    "plan_document_organization",
)


def test_document_organization_facade_exports_stable_names() -> None:
    assert facade.__all__ == EXPECTED_PUBLIC_NAMES
    assert all(hasattr(facade, name) for name in EXPECTED_PUBLIC_NAMES)


def test_document_organization_facade_delegates_to_bounded_layers() -> None:
    assert facade.plan_document_organization is planning.plan_document_organization
    assert facade.apply_document_organization is application.apply_document_organization
    assert (
        facade.apply_all_document_organization
        is application.apply_all_document_organization
    )
    assert facade.default_organization_root is models.default_organization_root
    assert facade.list_organization_plans is models.list_organization_plans
    assert facade.OrganizationPlanSummary is models.OrganizationPlanSummary
    assert facade.OrganizationApplySummary is models.OrganizationApplySummary
    assert facade.OrganizationPlanView is models.OrganizationPlanView
    assert facade._create_destination_parent is application._create_destination_parent


def test_document_organization_public_signatures_require_mutation_guard() -> None:
    signatures = {
        name: str(inspect.signature(getattr(facade, name)))
        for name in (
            "default_organization_root",
            "plan_document_organization",
            "apply_document_organization",
            "apply_all_document_organization",
            "list_organization_plans",
        )
    }
    assert signatures == {
        "default_organization_root": (
            "(framework_database: 'Path', *, analysis_root: 'Path | None' = None) "
            "-> 'Path'"
        ),
        "plan_document_organization": (
            "(catalog_path: 'Path', organization_root: 'Path', *, "
            "min_confidence: 'float' = 0.72, progress: 'ProgressCallback | None' "
            "= None, progress_operation: 'str' = 'framework', "
            "mutation_guard: 'CorpusMutationGuard | None' = None) -> "
            "'OrganizationPlanSummary'"
        ),
        "apply_document_organization": (
            "(catalog_path: 'Path', organization_root: 'Path', *, "
            "mutation_guard: 'CorpusMutationGuard', max_actions: 'int' = 100, "
            "on_progress: 'OrganizationApplyProgressCallback | None' = None) -> "
            "'OrganizationApplySummary'"
        ),
        "apply_all_document_organization": (
            "(catalog_path: 'Path', organization_root: 'Path', *, "
            "mutation_guard: 'CorpusMutationGuard', batch_size: 'int' = 100, "
            "progress: 'ProgressCallback | None' = None, progress_operation: 'str' "
            "= 'framework') -> 'OrganizationApplySummary'"
        ),
        "list_organization_plans": (
            "(catalog_path: 'Path', *, limit: 'int', status: 'str | None' = None) "
            "-> 'tuple[OrganizationPlanView, ...]'"
        ),
    }
# endregion [02]
