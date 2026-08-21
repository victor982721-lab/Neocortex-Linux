"""Exhaustive target architecture and compatibility contracts for Core.

This module is deliberately data-only and imports only the standard library.
It assigns every current ``_04_Nucleo_Operativo`` Python module to exactly one
of the 45 product responsibilities, or to the explicit compatibility surface.
The family dependency policy is the desired end-state DAG; a frozen transition
baseline makes existing reverse edges visible and non-increasing while the
cohorts move.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Final

CORE_RESPONSIBILITY_REGISTRY_SCHEMA: Final = "neocortex.core-responsibility-registry/v1"
CORE_FAMILY_DAG_SCHEMA: Final = "neocortex.core-family-dag/v1"
CORE_COMPATIBILITY_MATRIX_SCHEMA: Final = "neocortex.core-compatibility-matrix/v1"
CORE_TRANSITION_BASELINE_SCHEMA: Final = "neocortex.core-family-transition-baseline/v1"
CORE_ARCHITECTURE_FINGERPRINT_PREFIX: Final = "core-architecture-target-v1:sha256:"
CORE_MODULE_ROOT: Final = "_04_Nucleo_Operativo"

_MODULE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RESPONSIBILITY_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_TEST_PATH = re.compile(r"^tests/(?:[a-z0-9_]+/)*test_[a-z0-9_]+\.py$")


def _validate_module_id(value: str) -> None:
    if _MODULE_ID.fullmatch(value) is None:
        raise ValueError(f"invalid Core module id: {value!r}")


TARGET_FAMILY_LAYERS: Final = (
    "compat",
    "api",
    "runtime",
    "workflow",
    "knowledge",
    "semantic",
    "code",
    "documents",
    "formats",
    "persistence",
    "integrations",
    "safety",
    "foundation",
)
TARGET_FAMILIES: Final = TARGET_FAMILY_LAYERS[1:]
TARGET_FAMILY_DEPENDENCIES: Final = tuple(pairwise(TARGET_FAMILY_LAYERS))

TARGET_RESPONSIBILITY_IDS: Final = (
    "api.cli",
    "api.facade",
    "code.analysis",
    "code.contracts",
    "code.discovery_search",
    "code.experiments",
    "code.external_providers",
    "code.ingestion_state",
    "code.review",
    "code.validation",
    "documents.catalog",
    "documents.organization",
    "documents.taxonomy",
    "formats.archive",
    "formats.audio",
    "formats.docx",
    "formats.image_ocr",
    "formats.office",
    "formats.pdf",
    "formats.text",
    "formats.video",
    "foundation.identity",
    "foundation.provenance",
    "integrations.inventory",
    "knowledge.asset_health",
    "knowledge.contracts",
    "knowledge.evaluation",
    "knowledge.planning",
    "knowledge.retrieval",
    "knowledge.snapshot",
    "persistence.framework",
    "runtime.config",
    "runtime.control",
    "runtime.orchestration",
    "safety.access_policy",
    "semantic.backends_workers",
    "semantic.contracts",
    "semantic.persistence_lineage",
    "semantic.planning",
    "semantic.search_service",
    "semantic.sources_preparation",
    "workflow.actions_recovery",
    "workflow.retention",
    "workflow.review",
    "workflow.self_analysis",
)

RESPONSIBILITY_MODULES: Final = {
    "api.cli": (
        "_04_Nucleo_Operativo.cli_app",
        "_04_Nucleo_Operativo.cli_archive",
        "_04_Nucleo_Operativo.cli_archive_surface",
        "_04_Nucleo_Operativo.cli_audio",
        "_04_Nucleo_Operativo.cli_audio_surface",
        "_04_Nucleo_Operativo.cli_capabilities",
        "_04_Nucleo_Operativo.cli_capabilities_surface",
        "_04_Nucleo_Operativo.cli_code",
        "_04_Nucleo_Operativo.cli_code_surface",
        "_04_Nucleo_Operativo.cli_config",
        "_04_Nucleo_Operativo.cli_direct",
        "_04_Nucleo_Operativo.cli_docx_surface",
        "_04_Nucleo_Operativo.cli_knowledge",
        "_04_Nucleo_Operativo.cli_knowledge_surface",
        "_04_Nucleo_Operativo.cli_models",
        "_04_Nucleo_Operativo.cli_models_surface",
        "_04_Nucleo_Operativo.cli_office_surface",
        "_04_Nucleo_Operativo.cli_operations",
        "_04_Nucleo_Operativo.cli_parser",
        "_04_Nucleo_Operativo.cli_platform",
        "_04_Nucleo_Operativo.cli_platform_surface",
        "_04_Nucleo_Operativo.cli_reporting",
        "_04_Nucleo_Operativo.cli_retention",
        "_04_Nucleo_Operativo.cli_review_evidence",
        "_04_Nucleo_Operativo.cli_semantic",
        "_04_Nucleo_Operativo.cli_semantic_surface",
        "_04_Nucleo_Operativo.cli_text_surface",
        "_04_Nucleo_Operativo.cli_validation",
        "_04_Nucleo_Operativo.cli_video",
        "_04_Nucleo_Operativo.cli_video_surface",
        "_04_Nucleo_Operativo.cli_watcher",
    ),
    "api.facade": (
        "_04_Nucleo_Operativo",
        "_04_Nucleo_Operativo.capabilities",
        "_04_Nucleo_Operativo.capabilities.formats",
        "_04_Nucleo_Operativo.models",
        "_04_Nucleo_Operativo.read_api_port",
        "_04_Nucleo_Operativo.state",
    ),
    "code.analysis": (
        "_04_Nucleo_Operativo.code_analysis_epistemics",
        "_04_Nucleo_Operativo.code_analyzer_calibration",
        "_04_Nucleo_Operativo.code_analyzer_effectiveness",
        "_04_Nucleo_Operativo.code_architecture_analysis",
        "_04_Nucleo_Operativo.code_assurance_analysis",
        "_04_Nucleo_Operativo.code_capability_reachability_analysis",
        "_04_Nucleo_Operativo.code_change_evolution_analysis",
        "_04_Nucleo_Operativo.code_class_surface_analysis",
        "_04_Nucleo_Operativo.code_coverage_analysis",
        "_04_Nucleo_Operativo.code_engineering_analytics",
        "_04_Nucleo_Operativo.code_external_evidence",
        "_04_Nucleo_Operativo.code_interface_surface_analysis",
        "_04_Nucleo_Operativo.code_invariant_assurance_analysis",
        "_04_Nucleo_Operativo.code_knowledge_asset_health_analysis",
        "_04_Nucleo_Operativo.code_knowledge_pdf_asset_health_analysis",
        "_04_Nucleo_Operativo.code_publication_diff",
        "_04_Nucleo_Operativo.code_question_resolver",
        "_04_Nucleo_Operativo.code_retention_analysis",
        "_04_Nucleo_Operativo.code_route_capability_analysis",
        "_04_Nucleo_Operativo.code_state_interaction_analysis",
        "_04_Nucleo_Operativo.code_state_projection_analysis",
        "_04_Nucleo_Operativo.code_state_topology_analysis",
        "_04_Nucleo_Operativo.code_storage_analysis",
        "_04_Nucleo_Operativo.code_supply_chain_analysis",
        "_04_Nucleo_Operativo.code_unused_analysis",
    ),
    "code.contracts": (
        "_04_Nucleo_Operativo.code",
        "_04_Nucleo_Operativo.code.contracts",
        "_04_Nucleo_Operativo.code.contracts.target_projection",
        "_04_Nucleo_Operativo.code.contracts.target_registry",
        "_04_Nucleo_Operativo.code_architecture_contracts",
        "_04_Nucleo_Operativo.code_architecture_questions",
        "_04_Nucleo_Operativo.code_contracts",
        "_04_Nucleo_Operativo.code_invariant_contracts",
        "_04_Nucleo_Operativo.code_security_dependency_questions",
        "_04_Nucleo_Operativo.logical_owner_contracts",
        "_04_Nucleo_Operativo.platform.shared.architecture_projection",
        "_04_Nucleo_Operativo.platform.shared.capability_registry",
        "_04_Nucleo_Operativo.platform.shared.capability_registry_specs",
        "_04_Nucleo_Operativo.state_topology_contracts",
    ),
    "code.discovery_search": (
        "_04_Nucleo_Operativo.code_analysis_query",
        "_04_Nucleo_Operativo.code_search",
        "_04_Nucleo_Operativo.code_semantic_links",
    ),
    "code.experiments": (
        "_04_Nucleo_Operativo.code_experiment_executor",
        "_04_Nucleo_Operativo.code_experiment_planner",
        "_04_Nucleo_Operativo.code_experiment_store",
        "_04_Nucleo_Operativo.code_technical_verification",
    ),
    "code.external_providers": (
        "_04_Nucleo_Operativo.external_architecture_providers",
        "_04_Nucleo_Operativo.external_architecture_worker",
        "_04_Nucleo_Operativo.external_deep_coverage",
        "_04_Nucleo_Operativo.external_deep_coverage_worker",
        "_04_Nucleo_Operativo.external_dependency_hygiene",
        "_04_Nucleo_Operativo.external_evidence_models",
        "_04_Nucleo_Operativo.external_evidence_providers",
        "_04_Nucleo_Operativo.external_evidence_store",
        "_04_Nucleo_Operativo.external_git_history",
        "_04_Nucleo_Operativo.external_mutation_cosmic_ray",
        "_04_Nucleo_Operativo.external_mutation_cosmic_ray_worker",
        "_04_Nucleo_Operativo.external_semgrep_invariants",
        "_04_Nucleo_Operativo.external_supply_chain_audit",
        "_04_Nucleo_Operativo.external_unused_vulture",
        "_04_Nucleo_Operativo.external_unused_vulture_worker",
    ),
    "code.ingestion_state": (
        "_04_Nucleo_Operativo.code_analyzer_common",
        "_04_Nucleo_Operativo.code_analyzers",
        "_04_Nucleo_Operativo.code_candidate_scope",
        "_04_Nucleo_Operativo.code_detection",
        "_04_Nucleo_Operativo.code_generic",
        "_04_Nucleo_Operativo.code_projects",
        "_04_Nucleo_Operativo.code_python",
        "_04_Nucleo_Operativo.code_route",
        "_04_Nucleo_Operativo.code_rust",
        "_04_Nucleo_Operativo.code_schema",
        "_04_Nucleo_Operativo.code_state",
    ),
    "code.review": (
        "_04_Nucleo_Operativo.code_review",
        "_04_Nucleo_Operativo.code_review_actionability",
        "_04_Nucleo_Operativo.code_review_eligibility",
        "_04_Nucleo_Operativo.code_review_epistemics",
        "_04_Nucleo_Operativo.code_review_models",
        "_04_Nucleo_Operativo.code_review_serialization",
        "_04_Nucleo_Operativo.code_review_task_analysis",
        "_04_Nucleo_Operativo.code_review_work_packages",
    ),
    "code.validation": (
        "_04_Nucleo_Operativo.code.validation_supply",
        "_04_Nucleo_Operativo.code_change_validation",
        "_04_Nucleo_Operativo.code_validation_public_review",
        "_04_Nucleo_Operativo.code_validation_receipts",
        "_04_Nucleo_Operativo.code_validation_resources",
    ),
    "documents.catalog": (
        "_04_Nucleo_Operativo.document_cache_sync",
        "_04_Nucleo_Operativo.document_catalog",
        "_04_Nucleo_Operativo.document_catalog_schema",
    ),
    "documents.organization": (
        "_04_Nucleo_Operativo.document_naming",
        "_04_Nucleo_Operativo.document_organization",
        "_04_Nucleo_Operativo.document_organization_application",
        "_04_Nucleo_Operativo.document_organization_models",
        "_04_Nucleo_Operativo.document_organization_planning",
    ),
    "documents.taxonomy": (
        "_04_Nucleo_Operativo.document_signals",
        "_04_Nucleo_Operativo.document_taxonomy",
        "_04_Nucleo_Operativo.document_taxonomy_entities",
        "_04_Nucleo_Operativo.document_taxonomy_kinds",
        "_04_Nucleo_Operativo.document_taxonomy_models",
        "_04_Nucleo_Operativo.document_taxonomy_overlay",
        "_04_Nucleo_Operativo.document_taxonomy_references",
        "_04_Nucleo_Operativo.document_taxonomy_vocabulary",
    ),
    "formats.archive": (
        "_04_Nucleo_Operativo.capabilities.formats.archive",
        "_04_Nucleo_Operativo.capabilities.formats.archive.models",
        "_04_Nucleo_Operativo.capabilities.formats.archive.route",
        "_04_Nucleo_Operativo.capabilities.formats.archive.state",
        "_04_Nucleo_Operativo.capabilities.formats.archive.text_worker",
    ),
    "formats.audio": (
        "_04_Nucleo_Operativo.capabilities.formats.audio",
        "_04_Nucleo_Operativo.capabilities.formats.audio.models",
        "_04_Nucleo_Operativo.capabilities.formats.audio.probe",
        "_04_Nucleo_Operativo.capabilities.formats.audio.route",
        "_04_Nucleo_Operativo.capabilities.formats.audio.state",
        "_04_Nucleo_Operativo.capabilities.formats.audio.whisper",
    ),
    "formats.docx": (
        "_04_Nucleo_Operativo.capabilities.formats.docx",
        "_04_Nucleo_Operativo.capabilities.formats.docx.integrity",
        "_04_Nucleo_Operativo.capabilities.formats.docx.layout",
        "_04_Nucleo_Operativo.capabilities.formats.docx.models",
        "_04_Nucleo_Operativo.capabilities.formats.docx.route",
        "_04_Nucleo_Operativo.capabilities.formats.docx.schema",
        "_04_Nucleo_Operativo.capabilities.formats.docx.state",
    ),
    "formats.image_ocr": (
        "_04_Nucleo_Operativo.capabilities.formats.image",
        "_04_Nucleo_Operativo.capabilities.formats.image.adult",
        "_04_Nucleo_Operativo.capabilities.formats.image.analysis",
        "_04_Nucleo_Operativo.capabilities.formats.image.decision",
        "_04_Nucleo_Operativo.capabilities.formats.image.decode",
        "_04_Nucleo_Operativo.capabilities.formats.image.document",
        "_04_Nucleo_Operativo.capabilities.formats.image.errors",
        "_04_Nucleo_Operativo.capabilities.formats.image.features",
        "_04_Nucleo_Operativo.capabilities.formats.image.isolation",
        "_04_Nucleo_Operativo.capabilities.formats.image.models",
        "_04_Nucleo_Operativo.capabilities.formats.image.png",
        "_04_Nucleo_Operativo.capabilities.formats.image.policy",
        "_04_Nucleo_Operativo.capabilities.formats.image.route",
        "_04_Nucleo_Operativo.capabilities.formats.image.semantics",
        "_04_Nucleo_Operativo.capabilities.formats.image.state",
        "_04_Nucleo_Operativo.capabilities.formats.image.visual",
        "_04_Nucleo_Operativo.ocr_image_preprocess",
        "_04_Nucleo_Operativo.ocr_profiles",
    ),
    "formats.office": (
        "_04_Nucleo_Operativo.capabilities.formats.office",
        "_04_Nucleo_Operativo.capabilities.formats.office.extraction",
        "_04_Nucleo_Operativo.capabilities.formats.office.extraction_support",
        "_04_Nucleo_Operativo.capabilities.formats.office.legacy_worker",
        "_04_Nucleo_Operativo.capabilities.formats.office.models",
        "_04_Nucleo_Operativo.capabilities.formats.office.route",
        "_04_Nucleo_Operativo.capabilities.formats.office.state",
        "_04_Nucleo_Operativo.capabilities.formats.office.xlsx",
    ),
    "formats.pdf": (
        "_04_Nucleo_Operativo.pdf_admin",
        "_04_Nucleo_Operativo.pdf_cache",
        "_04_Nucleo_Operativo.pdf_derived",
        "_04_Nucleo_Operativo.pdf_derived_queries",
        "_04_Nucleo_Operativo.pdf_derived_schema",
        "_04_Nucleo_Operativo.pdf_isolation",
        "_04_Nucleo_Operativo.pdf_layout",
        "_04_Nucleo_Operativo.pdf_profile",
        "_04_Nucleo_Operativo.pdf_route",
        "_04_Nucleo_Operativo.pdf_route_cache",
        "_04_Nucleo_Operativo.pdf_route_models",
        "_04_Nucleo_Operativo.pdf_route_storage",
        "_04_Nucleo_Operativo.pdf_runtime",
        "_04_Nucleo_Operativo.pdf_schema",
        "_04_Nucleo_Operativo.pdf_state",
        "_04_Nucleo_Operativo.pdf_writer",
    ),
    "formats.text": (
        "_04_Nucleo_Operativo.text_derivation_repository",
        "_04_Nucleo_Operativo.text_route",
        "_04_Nucleo_Operativo.text_state",
    ),
    "formats.video": (
        "_04_Nucleo_Operativo.capabilities.formats.video",
        "_04_Nucleo_Operativo.capabilities.formats.video.frames",
        "_04_Nucleo_Operativo.capabilities.formats.video.models",
        "_04_Nucleo_Operativo.capabilities.formats.video.probe",
        "_04_Nucleo_Operativo.capabilities.formats.video.route",
        "_04_Nucleo_Operativo.capabilities.formats.video.state",
    ),
    "foundation.identity": (
        "_04_Nucleo_Operativo.file_identity",
        "_04_Nucleo_Operativo.platform",
        "_04_Nucleo_Operativo.platform.shared",
        "_04_Nucleo_Operativo.platform.shared.content_types",
    ),
    "foundation.provenance": ("_04_Nucleo_Operativo.processing_provenance",),
    "integrations.inventory": (
        "_04_Nucleo_Operativo.inventory_boundary",
        "_04_Nucleo_Operativo.inventory_coordinator",
        "_04_Nucleo_Operativo.reconcile",
    ),
    "knowledge.asset_health": (
        "_04_Nucleo_Operativo.knowledge_asset_health",
        "_04_Nucleo_Operativo.knowledge_asset_health_contracts",
        "_04_Nucleo_Operativo.knowledge_asset_health_pdf",
        "_04_Nucleo_Operativo.knowledge_asset_health_repository",
    ),
    "knowledge.contracts": (
        "_04_Nucleo_Operativo.knowledge_contract_context",
        "_04_Nucleo_Operativo.knowledge_contract_payloads",
        "_04_Nucleo_Operativo.knowledge_contract_protocols",
        "_04_Nucleo_Operativo.knowledge_contract_references",
        "_04_Nucleo_Operativo.knowledge_contract_snapshot",
        "_04_Nucleo_Operativo.knowledge_contract_telemetry",
        "_04_Nucleo_Operativo.knowledge_contract_validation",
        "_04_Nucleo_Operativo.knowledge_contracts",
        "_04_Nucleo_Operativo.knowledge_search_contracts",
    ),
    "knowledge.evaluation": ("_04_Nucleo_Operativo.knowledge_evaluation",),
    "knowledge.planning": (
        "_04_Nucleo_Operativo.knowledge_planner",
        "_04_Nucleo_Operativo.knowledge_planner_exact",
        "_04_Nucleo_Operativo.knowledge_planner_intents",
        "_04_Nucleo_Operativo.knowledge_planner_steps",
    ),
    "knowledge.retrieval": (
        "_04_Nucleo_Operativo.knowledge_context",
        "_04_Nucleo_Operativo.knowledge_exact",
        "_04_Nucleo_Operativo.knowledge_search",
        "_04_Nucleo_Operativo.knowledge_search_catalog",
        "_04_Nucleo_Operativo.knowledge_search_code",
        "_04_Nucleo_Operativo.knowledge_search_content",
        "_04_Nucleo_Operativo.knowledge_search_fusion",
        "_04_Nucleo_Operativo.knowledge_search_inventory",
        "_04_Nucleo_Operativo.knowledge_service",
    ),
    "knowledge.snapshot": ("_04_Nucleo_Operativo.knowledge_snapshot",),
    "persistence.framework": (
        "_04_Nucleo_Operativo.framework_connection",
        "_04_Nucleo_Operativo.framework_route_state",
        "_04_Nucleo_Operativo.framework_schema",
        "_04_Nucleo_Operativo.framework_state_common",
        "_04_Nucleo_Operativo.framework_state_writer",
        "_04_Nucleo_Operativo.sqlite_cancellation",
        "_04_Nucleo_Operativo.sqlite_immutable",
        "_04_Nucleo_Operativo.sqlite_paths",
        "_04_Nucleo_Operativo.sqlite_schema_contract",
        "_04_Nucleo_Operativo.sqlite_schema_lifecycle",
    ),
    "runtime.config": (
        "_04_Nucleo_Operativo.app_paths",
        "_04_Nucleo_Operativo.application_config",
        "_04_Nucleo_Operativo.application_config_projections",
        "_04_Nucleo_Operativo.model_management",
    ),
    "runtime.control": (
        "_04_Nucleo_Operativo.bounded_subprocess",
        "_04_Nucleo_Operativo.cancellation",
        "_04_Nucleo_Operativo.console_cancellation",
        "_04_Nucleo_Operativo.cpu_runtime",
        "_04_Nucleo_Operativo.global_resources",
        "_04_Nucleo_Operativo.incremental_gate",
        "_04_Nucleo_Operativo.isolated_process",
        "_04_Nucleo_Operativo.locking",
        "_04_Nucleo_Operativo.memory_runtime",
        "_04_Nucleo_Operativo.retry_policy",
        "_04_Nucleo_Operativo.watcher",
        "_04_Nucleo_Operativo.watcher_life_lease",
    ),
    "runtime.orchestration": (
        "_04_Nucleo_Operativo.orchestrator",
        "_04_Nucleo_Operativo.route_registry",
        "_04_Nucleo_Operativo.route_selection",
        "_04_Nucleo_Operativo.run_lifecycle",
        "_04_Nucleo_Operativo.run_status",
    ),
    "safety.access_policy": (
        "_04_Nucleo_Operativo.corpus_access",
        "_04_Nucleo_Operativo.internal_paths",
        "_04_Nucleo_Operativo.platform.shared.zip_safety",
        "_04_Nucleo_Operativo.protected_content",
        "_04_Nucleo_Operativo.route_filters",
        "_04_Nucleo_Operativo.windows_handle_mutation",
    ),
    "semantic.backends_workers": (
        "_04_Nucleo_Operativo.semantic_backend_supervisor",
        "_04_Nucleo_Operativo.semantic_backends",
        "_04_Nucleo_Operativo.semantic_generation_worker",
        "_04_Nucleo_Operativo.semantic_image_index",
        "_04_Nucleo_Operativo.semantic_text_index",
    ),
    "semantic.contracts": (
        "_04_Nucleo_Operativo.semantic_config",
        "_04_Nucleo_Operativo.semantic_contract_payloads",
        "_04_Nucleo_Operativo.semantic_contract_validation",
        "_04_Nucleo_Operativo.semantic_models",
        "_04_Nucleo_Operativo.semantic_ontology",
        "_04_Nucleo_Operativo.semantic_quality",
        "_04_Nucleo_Operativo.semantic_service_contracts",
    ),
    "semantic.persistence_lineage": (
        "_04_Nucleo_Operativo.derivation_contracts",
        "_04_Nucleo_Operativo.derivation_lineage_service",
        "_04_Nucleo_Operativo.derivation_projection",
        "_04_Nucleo_Operativo.semantic_evidence_repository",
        "_04_Nucleo_Operativo.semantic_generation_repository",
        "_04_Nucleo_Operativo.semantic_item_repository",
        "_04_Nucleo_Operativo.semantic_lineage_repository",
        "_04_Nucleo_Operativo.semantic_repository_common",
        "_04_Nucleo_Operativo.semantic_schema",
        "_04_Nucleo_Operativo.semantic_state",
    ),
    "semantic.planning": (
        "_04_Nucleo_Operativo.semantic_plan_errors",
        "_04_Nucleo_Operativo.semantic_plan_owners",
        "_04_Nucleo_Operativo.semantic_plan_results",
        "_04_Nucleo_Operativo.semantic_plan_scratch",
        "_04_Nucleo_Operativo.semantic_planner",
        "_04_Nucleo_Operativo.semantic_work_budget",
    ),
    "semantic.search_service": (
        "_04_Nucleo_Operativo.semantic_lexical",
        "_04_Nucleo_Operativo.semantic_search_repository",
        "_04_Nucleo_Operativo.semantic_search_service",
        "_04_Nucleo_Operativo.semantic_service",
        "_04_Nucleo_Operativo.semantic_status_service",
    ),
    "semantic.sources_preparation": (
        "_04_Nucleo_Operativo.semantic_chunking",
        "_04_Nucleo_Operativo.semantic_classification_service",
        "_04_Nucleo_Operativo.semantic_preparation",
        "_04_Nucleo_Operativo.semantic_sources",
    ),
    "workflow.actions_recovery": (
        "_04_Nucleo_Operativo.action_policy",
        "_04_Nucleo_Operativo.actions",
        "_04_Nucleo_Operativo.file_action_reconciliation_store",
        "_04_Nucleo_Operativo.file_action_recovery",
    ),
    "workflow.retention": ("_04_Nucleo_Operativo.retention_planner",),
    "workflow.review": (
        "_04_Nucleo_Operativo.review",
        "_04_Nucleo_Operativo.review_evidence",
        "_04_Nucleo_Operativo.review_task_contracts",
        "_04_Nucleo_Operativo.review_task_repository",
        "_04_Nucleo_Operativo.value_review",
        "_04_Nucleo_Operativo.value_review_contracts",
        "_04_Nucleo_Operativo.value_review_port",
        "_04_Nucleo_Operativo.value_review_repository",
        "_04_Nucleo_Operativo.value_review_tasks",
    ),
    "workflow.self_analysis": (
        "_04_Nucleo_Operativo.self_analysis",
        "_04_Nucleo_Operativo.self_analysis_finalization",
        "_04_Nucleo_Operativo.self_analysis_freshness",
        "_04_Nucleo_Operativo.self_analysis_manifest",
        "_04_Nucleo_Operativo.self_analysis_status",
    ),
}

COMPATIBILITY_MODULES: Final = (
    "_04_Nucleo_Operativo.archive_models",
    "_04_Nucleo_Operativo.archive_route",
    "_04_Nucleo_Operativo.archive_state",
    "_04_Nucleo_Operativo.archive_text_worker",
    "_04_Nucleo_Operativo.audio_models",
    "_04_Nucleo_Operativo.audio_probe",
    "_04_Nucleo_Operativo.audio_route",
    "_04_Nucleo_Operativo.audio_state",
    "_04_Nucleo_Operativo.audio_whisper",
    "_04_Nucleo_Operativo.content_types",
    "_04_Nucleo_Operativo.docx_integrity",
    "_04_Nucleo_Operativo.docx_layout",
    "_04_Nucleo_Operativo.docx_models",
    "_04_Nucleo_Operativo.docx_route",
    "_04_Nucleo_Operativo.docx_schema",
    "_04_Nucleo_Operativo.docx_state",
    "_04_Nucleo_Operativo.image_adult",
    "_04_Nucleo_Operativo.image_analysis",
    "_04_Nucleo_Operativo.image_decision",
    "_04_Nucleo_Operativo.image_decode",
    "_04_Nucleo_Operativo.image_document",
    "_04_Nucleo_Operativo.image_errors",
    "_04_Nucleo_Operativo.image_features",
    "_04_Nucleo_Operativo.image_isolation",
    "_04_Nucleo_Operativo.image_models",
    "_04_Nucleo_Operativo.image_png",
    "_04_Nucleo_Operativo.image_policy",
    "_04_Nucleo_Operativo.image_route",
    "_04_Nucleo_Operativo.image_semantics",
    "_04_Nucleo_Operativo.image_state",
    "_04_Nucleo_Operativo.image_visual",
    "_04_Nucleo_Operativo.legacy_office_worker",
    "_04_Nucleo_Operativo.office_route",
    "_04_Nucleo_Operativo.office_state",
    "_04_Nucleo_Operativo.video_frames",
    "_04_Nucleo_Operativo.video_models",
    "_04_Nucleo_Operativo.video_probe",
    "_04_Nucleo_Operativo.video_route",
    "_04_Nucleo_Operativo.video_state",
    "_04_Nucleo_Operativo.zip_safety",
)


def _format_compatibility_pairs(
    source_prefix: str,
    target_tree: str,
    roles: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (f"{CORE_MODULE_ROOT}.{source_prefix}_{role}", f"{CORE_MODULE_ROOT}.{target_tree}.{role}")
        for role in roles
    )


COMPATIBILITY_MODULE_PAIRS: Final = tuple(
    sorted(
        (
            (
                f"{CORE_MODULE_ROOT}.content_types",
                f"{CORE_MODULE_ROOT}.platform.shared.content_types",
            ),
            (
                f"{CORE_MODULE_ROOT}.zip_safety",
                f"{CORE_MODULE_ROOT}.platform.shared.zip_safety",
            ),
            *_format_compatibility_pairs(
                "archive",
                "capabilities.formats.archive",
                ("models", "route", "state", "text_worker"),
            ),
            *_format_compatibility_pairs(
                "audio",
                "capabilities.formats.audio",
                ("models", "probe", "route", "state", "whisper"),
            ),
            *_format_compatibility_pairs(
                "docx",
                "capabilities.formats.docx",
                ("integrity", "layout", "models", "route", "schema", "state"),
            ),
            *_format_compatibility_pairs(
                "image",
                "capabilities.formats.image",
                (
                    "adult",
                    "analysis",
                    "decision",
                    "decode",
                    "document",
                    "errors",
                    "features",
                    "isolation",
                    "models",
                    "png",
                    "policy",
                    "route",
                    "semantics",
                    "state",
                    "visual",
                ),
            ),
            (
                f"{CORE_MODULE_ROOT}.legacy_office_worker",
                f"{CORE_MODULE_ROOT}.capabilities.formats.office.legacy_worker",
            ),
            (
                f"{CORE_MODULE_ROOT}.office_route",
                f"{CORE_MODULE_ROOT}.capabilities.formats.office.route",
            ),
            (
                f"{CORE_MODULE_ROOT}.office_state",
                f"{CORE_MODULE_ROOT}.capabilities.formats.office.state",
            ),
            *_format_compatibility_pairs(
                "video",
                "capabilities.formats.video",
                ("frames", "models", "probe", "route", "state"),
            ),
        )
    )
)


_PICKLE_COMPATIBILITY_MODULES: Final = frozenset(COMPATIBILITY_MODULES) - {
    "_04_Nucleo_Operativo.image_policy"
}
_INSTANCE_PICKLE_MODULES: Final = frozenset(
    {
        "_04_Nucleo_Operativo.archive_models",
        "_04_Nucleo_Operativo.audio_models",
        "_04_Nucleo_Operativo.content_types",
        "_04_Nucleo_Operativo.docx_models",
        "_04_Nucleo_Operativo.image_models",
        "_04_Nucleo_Operativo.office_route",
        "_04_Nucleo_Operativo.video_models",
    }
)
_MONKEYPATCH_COMPATIBILITY_MODULES: Final = frozenset(
    {
        "_04_Nucleo_Operativo.archive_route",
        "_04_Nucleo_Operativo.archive_state",
        "_04_Nucleo_Operativo.audio_probe",
        "_04_Nucleo_Operativo.audio_state",
        "_04_Nucleo_Operativo.audio_whisper",
        "_04_Nucleo_Operativo.content_types",
        "_04_Nucleo_Operativo.docx_integrity",
        "_04_Nucleo_Operativo.docx_route",
        "_04_Nucleo_Operativo.docx_state",
        "_04_Nucleo_Operativo.image_analysis",
        "_04_Nucleo_Operativo.image_document",
        "_04_Nucleo_Operativo.image_features",
        "_04_Nucleo_Operativo.image_route",
        "_04_Nucleo_Operativo.legacy_office_worker",
        "_04_Nucleo_Operativo.office_route",
        "_04_Nucleo_Operativo.office_state",
        "_04_Nucleo_Operativo.video_frames",
        "_04_Nucleo_Operativo.video_probe",
        "_04_Nucleo_Operativo.video_route",
        "_04_Nucleo_Operativo.video_state",
        "_04_Nucleo_Operativo.zip_safety",
    }
)
_EXECUTABLE_COMPATIBILITY_MODULES: Final = frozenset(
    {
        "_04_Nucleo_Operativo.archive_text_worker",
        "_04_Nucleo_Operativo.legacy_office_worker",
    }
)
_COMPATIBILITY_REQUIREMENTS: Final = frozenset(
    {
        "historical_pickle_global",
        "import_module_identity",
        "lazy_parent_import",
        "monkeypatch_seam",
        "pickle_instance_roundtrip",
        "source_alias_only",
        "type_checking_exports",
        "worker_module_execution",
    }
)
_GENERIC_COMPATIBILITY_TEST = "tests/test_format_module_move_compatibility.py"


@dataclass(frozen=True, slots=True, order=True)
class CompatibilityModuleContract:
    legacy_module_id: str
    canonical_module_id: str
    requirement_ids: tuple[str, ...]
    test_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_module_id(self.legacy_module_id)
        _validate_module_id(self.canonical_module_id)
        if self.legacy_module_id == self.canonical_module_id:
            raise ValueError("compatibility modules must identify a real move")
        if self.requirement_ids != tuple(sorted(set(self.requirement_ids))):
            raise ValueError("compatibility requirements must be canonical")
        if not set(self.requirement_ids) <= _COMPATIBILITY_REQUIREMENTS:
            raise ValueError("compatibility requirement is unsupported")
        if self.test_roots != tuple(sorted(set(self.test_roots))):
            raise ValueError("compatibility tests must be canonical")
        if not self.test_roots or any(
            _TEST_PATH.fullmatch(item) is None for item in self.test_roots
        ):
            raise ValueError("compatibility tests must be normalized test modules")


def _compatibility_test_roots(legacy_module_id: str) -> tuple[str, ...]:
    roots = {_GENERIC_COMPATIBILITY_TEST}
    if ".archive_" in legacy_module_id:
        roots.add("tests/test_archive_namespace_migration.py")
    elif ".audio_" in legacy_module_id:
        roots.add("tests/test_audio_namespace_migration.py")
    elif ".docx_" in legacy_module_id:
        roots.add("tests/test_docx_namespace_migration.py")
    elif ".image_" in legacy_module_id:
        roots.add("tests/test_image_namespace_migration.py")
    elif ".office_" in legacy_module_id or legacy_module_id.endswith(".legacy_office_worker"):
        roots.add("tests/test_office_namespace_migration.py")
    elif ".video_" in legacy_module_id:
        roots.add("tests/test_video_namespace_migration.py")
    return tuple(sorted(roots))


def _compatibility_requirement_ids(legacy_module_id: str) -> tuple[str, ...]:
    requirements = {
        "import_module_identity",
        "lazy_parent_import",
        "source_alias_only",
        "type_checking_exports",
    }
    if legacy_module_id in _PICKLE_COMPATIBILITY_MODULES:
        requirements.add("historical_pickle_global")
    if legacy_module_id in _INSTANCE_PICKLE_MODULES:
        requirements.add("pickle_instance_roundtrip")
    if legacy_module_id in _MONKEYPATCH_COMPATIBILITY_MODULES:
        requirements.add("monkeypatch_seam")
    if legacy_module_id in _EXECUTABLE_COMPATIBILITY_MODULES:
        requirements.add("worker_module_execution")
    return tuple(sorted(requirements))


COMPATIBILITY_CONTRACTS: Final = tuple(
    CompatibilityModuleContract(
        legacy,
        canonical,
        _compatibility_requirement_ids(legacy),
        _compatibility_test_roots(legacy),
    )
    for legacy, canonical in COMPATIBILITY_MODULE_PAIRS
)


@dataclass(frozen=True, slots=True, order=True)
class FamilyEdgeBaseline:
    source_family: str
    target_family: str
    direct_module_edges: int

    def __post_init__(self) -> None:
        if self.source_family not in TARGET_FAMILIES:
            raise ValueError("family-edge baseline source is unsupported")
        if self.target_family not in TARGET_FAMILIES:
            raise ValueError("family-edge baseline target is unsupported")
        if self.source_family == self.target_family:
            raise ValueError("family-edge baseline cannot describe an internal edge")
        if type(self.direct_module_edges) is not int or self.direct_module_edges < 1:
            raise ValueError("family-edge baseline count must be positive")


FORBIDDEN_FAMILY_EDGE_BASELINE: Final = (
    FamilyEdgeBaseline("code", "knowledge", 9),
    FamilyEdgeBaseline("code", "runtime", 22),
    FamilyEdgeBaseline("code", "semantic", 43),
    FamilyEdgeBaseline("code", "workflow", 11),
    FamilyEdgeBaseline("documents", "runtime", 1),
    FamilyEdgeBaseline("documents", "semantic", 1),
    FamilyEdgeBaseline("documents", "workflow", 2),
    FamilyEdgeBaseline("formats", "api", 7),
    FamilyEdgeBaseline("formats", "knowledge", 2),
    FamilyEdgeBaseline("formats", "runtime", 38),
    FamilyEdgeBaseline("formats", "semantic", 8),
    FamilyEdgeBaseline("formats", "workflow", 11),
    FamilyEdgeBaseline("foundation", "runtime", 1),
    FamilyEdgeBaseline("foundation", "safety", 1),
    FamilyEdgeBaseline("integrations", "api", 1),
    FamilyEdgeBaseline("integrations", "runtime", 1),
    FamilyEdgeBaseline("knowledge", "workflow", 2),
    FamilyEdgeBaseline("persistence", "workflow", 8),
    FamilyEdgeBaseline("runtime", "api", 7),
    FamilyEdgeBaseline("safety", "runtime", 1),
    FamilyEdgeBaseline("semantic", "knowledge", 4),
    FamilyEdgeBaseline("semantic", "runtime", 1),
    FamilyEdgeBaseline("workflow", "api", 3),
    FamilyEdgeBaseline("workflow", "runtime", 1),
)


def _responsibility_family(responsibility_id: str) -> str:
    return responsibility_id.partition(".")[0]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _responsibility_payload() -> list[dict[str, object]]:
    return [
        {
            "responsibility_id": responsibility_id,
            "family": _responsibility_family(responsibility_id),
            "target_module_tree": f"{CORE_MODULE_ROOT}.{responsibility_id}",
            "modules": list(RESPONSIBILITY_MODULES[responsibility_id]),
        }
        for responsibility_id in TARGET_RESPONSIBILITY_IDS
    ]


def core_architecture_target_payload() -> dict[str, object]:
    return {
        "responsibility_registry": {
            "schema": CORE_RESPONSIBILITY_REGISTRY_SCHEMA,
            "coverage_policy": "exhaustive-exact-module-no-default-v1",
            "responsibilities": _responsibility_payload(),
            "compatibility_modules": list(COMPATIBILITY_MODULES),
        },
        "family_dag": {
            "schema": CORE_FAMILY_DAG_SCHEMA,
            "edge_semantics": "importer-may-depend-on-later-layer-v1",
            "layer_order": list(TARGET_FAMILY_LAYERS),
            "direct_dependencies": [list(item) for item in TARGET_FAMILY_DEPENDENCIES],
            "compat_families": ["compat"],
        },
        "compatibility_matrix": {
            "schema": CORE_COMPATIBILITY_MATRIX_SCHEMA,
            "contracts": [asdict(item) for item in COMPATIBILITY_CONTRACTS],
        },
        "transition_baseline": {
            "schema": CORE_TRANSITION_BASELINE_SCHEMA,
            "policy": "forbidden-family-direct-edge-counts-must-not-increase-v1",
            "edges": [asdict(item) for item in FORBIDDEN_FAMILY_EDGE_BASELINE],
        },
    }


def core_architecture_target_fingerprint() -> str:
    encoded = _canonical_json(core_architecture_target_payload()).encode("utf-8")
    return CORE_ARCHITECTURE_FINGERPRINT_PREFIX + hashlib.sha256(encoded).hexdigest()


_MODULE_RESPONSIBILITIES: Final = {
    module_id: responsibility_id
    for responsibility_id, module_ids in RESPONSIBILITY_MODULES.items()
    for module_id in module_ids
}
_COMPATIBILITY_SET: Final = frozenset(COMPATIBILITY_MODULES)


def matching_target_responsibilities(module_id: str) -> tuple[str, ...]:
    """Return the one explicit product responsibility, never a default."""

    responsibility = _MODULE_RESPONSIBILITIES.get(module_id)
    return () if responsibility is None else (responsibility,)


def matching_target_families(module_id: str) -> tuple[str, ...]:
    """Return one exact target family or the explicit compatibility family."""

    if module_id in _COMPATIBILITY_SET:
        return ("compat",)
    responsibility = _MODULE_RESPONSIBILITIES.get(module_id)
    return () if responsibility is None else (_responsibility_family(responsibility),)


def forbidden_family_edge_baseline() -> dict[tuple[str, str], int]:
    return {
        (item.source_family, item.target_family): item.direct_module_edges
        for item in FORBIDDEN_FAMILY_EDGE_BASELINE
    }


def registered_core_modules() -> tuple[str, ...]:
    return tuple(sorted((*_MODULE_RESPONSIBILITIES, *COMPATIBILITY_MODULES)))


def _validate_responsibility_vocabulary() -> None:
    if len(TARGET_RESPONSIBILITY_IDS) != 45:
        raise ValueError("Core target must retain exactly 45 product responsibilities")
    if TARGET_RESPONSIBILITY_IDS != tuple(sorted(set(TARGET_RESPONSIBILITY_IDS))):
        raise ValueError("Core responsibility identities must be canonical")
    if tuple(sorted(RESPONSIBILITY_MODULES)) != TARGET_RESPONSIBILITY_IDS:
        raise ValueError("Core responsibility mapping does not cover the target vocabulary")
    if {_responsibility_family(item) for item in TARGET_RESPONSIBILITY_IDS} != set(TARGET_FAMILIES):
        raise ValueError("Core target families and responsibilities disagree")


def _validated_assigned_modules() -> tuple[str, ...]:
    assigned: list[str] = []
    for responsibility_id, module_ids in RESPONSIBILITY_MODULES.items():
        if _RESPONSIBILITY_ID.fullmatch(responsibility_id) is None:
            raise ValueError("Core responsibility identity is invalid")
        if not module_ids or module_ids != tuple(sorted(set(module_ids))):
            raise ValueError("Core responsibility modules must be non-empty and canonical")
        for module_id in module_ids:
            _validate_module_id(module_id)
        assigned.extend(module_ids)
    if len(assigned) != len(set(assigned)):
        raise ValueError("Core module cannot belong to two product responsibilities")
    return tuple(assigned)


def _validate_compatibility_registry(assigned: tuple[str, ...]) -> None:
    if COMPATIBILITY_MODULES != tuple(sorted(set(COMPATIBILITY_MODULES))):
        raise ValueError("Core compatibility modules must be canonical")
    if set(assigned) & set(COMPATIBILITY_MODULES):
        raise ValueError("Core implementation and compatibility modules overlap")
    if tuple(item.legacy_module_id for item in COMPATIBILITY_CONTRACTS) != (COMPATIBILITY_MODULES):
        raise ValueError("Core compatibility matrix does not cover every facade exactly")
    if any(item.canonical_module_id not in set(assigned) for item in COMPATIBILITY_CONTRACTS):
        raise ValueError("Core compatibility target is not a registered implementation")


def _validate_family_transition_baseline() -> None:
    if TARGET_FAMILY_LAYERS != tuple(dict.fromkeys(TARGET_FAMILY_LAYERS)):
        raise ValueError("Core family layers cannot repeat")
    rank = {family: index for index, family in enumerate(TARGET_FAMILY_LAYERS)}
    baselines = tuple(
        (item.source_family, item.target_family) for item in FORBIDDEN_FAMILY_EDGE_BASELINE
    )
    if baselines != tuple(sorted(set(baselines))):
        raise ValueError("Core transition baseline pairs must be canonical")
    if any(rank[source] < rank[target] for source, target in baselines):
        raise ValueError("Core transition baseline contains an allowed family dependency")


def _validate_registry() -> None:
    _validate_responsibility_vocabulary()
    assigned = _validated_assigned_modules()
    _validate_compatibility_registry(assigned)
    _validate_family_transition_baseline()


_validate_registry()


__all__ = [
    "COMPATIBILITY_CONTRACTS",
    "COMPATIBILITY_MODULES",
    "COMPATIBILITY_MODULE_PAIRS",
    "CORE_ARCHITECTURE_FINGERPRINT_PREFIX",
    "CORE_COMPATIBILITY_MATRIX_SCHEMA",
    "CORE_FAMILY_DAG_SCHEMA",
    "CORE_MODULE_ROOT",
    "CORE_RESPONSIBILITY_REGISTRY_SCHEMA",
    "CORE_TRANSITION_BASELINE_SCHEMA",
    "FORBIDDEN_FAMILY_EDGE_BASELINE",
    "RESPONSIBILITY_MODULES",
    "TARGET_FAMILIES",
    "TARGET_FAMILY_DEPENDENCIES",
    "TARGET_FAMILY_LAYERS",
    "TARGET_RESPONSIBILITY_IDS",
    "CompatibilityModuleContract",
    "FamilyEdgeBaseline",
    "core_architecture_target_fingerprint",
    "core_architecture_target_payload",
    "forbidden_family_edge_baseline",
    "matching_target_families",
    "matching_target_responsibilities",
    "registered_core_modules",
]
