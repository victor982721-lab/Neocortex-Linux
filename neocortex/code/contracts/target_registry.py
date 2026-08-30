"""Exhaustive target architecture contracts for the canonical Core tree.

This module is deliberately data-only and imports only the standard library.
It assigns every current ``neocortex`` production module to exactly one of the
45 product responsibilities. The family dependency policy is the desired
end-state DAG; the frozen transition baseline keeps forbidden edges explicit.
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
CORE_TRANSITION_BASELINE_SCHEMA: Final = "neocortex.core-family-transition-baseline/v1"
CORE_ARCHITECTURE_FINGERPRINT_PREFIX: Final = "core-architecture-target-v1:sha256:"
CORE_MODULE_ROOT: Final = "neocortex"

_MODULE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RESPONSIBILITY_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_TEST_PATH = re.compile(r"^tests/(?:[a-z0-9_]+/)*test_[a-z0-9_]+\.py$")


def _validate_module_id(value: str) -> None:
    if _MODULE_ID.fullmatch(value) is None:
        raise ValueError(f"invalid Core module id: {value!r}")


TARGET_FAMILY_LAYERS: Final = (
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
TARGET_FAMILIES: Final = TARGET_FAMILY_LAYERS
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
        "neocortex.api.cli.cli_app",
        "neocortex.api.cli.cli_archive",
        "neocortex.api.cli.cli_archive_surface",
        "neocortex.api.cli.cli_audio",
        "neocortex.api.cli.cli_audio_surface",
        "neocortex.api.cli.cli_capabilities",
        "neocortex.api.cli.cli_capabilities_surface",
        "neocortex.api.cli.cli_code",
        "neocortex.api.cli.cli_code_surface",
        "neocortex.api.cli.cli_config",
        "neocortex.api.cli.cli_direct",
        "neocortex.api.cli.cli_docx_surface",
        "neocortex.api.cli.cli_knowledge",
        "neocortex.api.cli.cli_knowledge_surface",
        "neocortex.api.cli.cli_models",
        "neocortex.api.cli.cli_models_surface",
        "neocortex.api.cli.cli_office_surface",
        "neocortex.api.cli.cli_operations",
        "neocortex.api.cli.cli_parser",
        "neocortex.api.cli.cli_platform",
        "neocortex.api.cli.cli_platform_surface",
        "neocortex.api.cli.cli_reporting",
        "neocortex.api.cli.cli_retention",
        "neocortex.api.cli.cli_review_evidence",
        "neocortex.api.cli.cli_semantic",
        "neocortex.api.cli.cli_semantic_surface",
        "neocortex.api.cli.cli_text_surface",
        "neocortex.api.cli.cli_validation",
        "neocortex.api.cli.cli_video",
        "neocortex.api.cli.cli_video_surface",
        "neocortex.api.cli.cli_watcher",
    ),
    "api.facade": (
        "neocortex",
        "neocortex.__main__",
        "neocortex.api",
        "neocortex.api.agent_server",
        "neocortex.api.cli",
        "neocortex.api.cli.human",
        "neocortex.api.cli.review_task",
        "neocortex.api.cli.value_review",
        "neocortex.api.public",
        "neocortex.api.read_api",
        "neocortex.api.read_api_port",
        "neocortex.api.status_codes",
        "neocortex.capabilities",
        "neocortex.capabilities.broker",
        "neocortex.capabilities.formats",
        "neocortex.capabilities.runtime",
        "neocortex.code.pip_bootstrap",
        "neocortex.interface",
        "neocortex.interface.__main__",
        "neocortex.interface.application",
        "neocortex.interface.application.app",
        "neocortex.interface.application.controller",
        "neocortex.interface.application.elevation",
        "neocortex.interface.application.request",
        "neocortex.interface.entrypoint",
        "neocortex.interface.presentation",
        "neocortex.interface.presentation.assets",
        "neocortex.interface.presentation.theme",
        "neocortex.interface.presentation.widgets",
        "neocortex.interface.presentation.windows",
        "neocortex.interface.presentation.windows.main",
        "neocortex.interface.presentation.windows.pages",
        "neocortex.interface.protocol",
        "neocortex.interface.protocol.messages",
        "neocortex.interface.protocol.worker",
        "neocortex.interface.read",
        "neocortex.interface.read.client",
        "neocortex.interface.read.issues",
        "neocortex.interface.read.models",
        "neocortex.interface.read.presentation",
        "neocortex.interface.read.status",
        "neocortex.interface.read.tasks",
        "neocortex.runtime.models",
        "neocortex.sdk",
    ),
    "code.analysis": (
        "neocortex.code.code_analysis_epistemics",
        "neocortex.code.code_analyzer_calibration",
        "neocortex.code.code_analyzer_effectiveness",
        "neocortex.code.code_architecture_analysis",
        "neocortex.code.code_assurance_analysis",
        "neocortex.code.code_capability_reachability_analysis",
        "neocortex.code.code_change_evolution_analysis",
        "neocortex.code.code_class_surface_analysis",
        "neocortex.code.code_coverage_analysis",
        "neocortex.code.code_engineering_analytics",
        "neocortex.code.code_external_evidence",
        "neocortex.code.code_interface_surface_analysis",
        "neocortex.code.code_invariant_assurance_analysis",
        "neocortex.code.code_knowledge_asset_health_analysis",
        "neocortex.code.code_knowledge_pdf_asset_health_analysis",
        "neocortex.code.code_publication_diff",
        "neocortex.code.code_question_resolver",
        "neocortex.code.code_retention",
        "neocortex.code.code_retention_analysis",
        "neocortex.code.code_route_capability_analysis",
        "neocortex.code.code_state_interaction_analysis",
        "neocortex.code.code_state_projection_analysis",
        "neocortex.code.code_state_topology_analysis",
        "neocortex.code.code_storage_analysis",
        "neocortex.code.code_supply_chain_analysis",
        "neocortex.code.code_unused_analysis",
    ),
    "code.contracts": (
        "neocortex.code",
        "neocortex.code.code_architecture_contracts",
        "neocortex.code.code_architecture_questions",
        "neocortex.code.code_contracts",
        "neocortex.code.code_invariant_contracts",
        "neocortex.code.code_security_dependency_questions",
        "neocortex.code.contracts",
        "neocortex.code.contracts.target_projection",
        "neocortex.code.contracts.target_registry",
        "neocortex.code.logical_owner_contracts",
        "neocortex.platform.architecture_projection",
        "neocortex.platform.capability_registry",
        "neocortex.platform.capability_registry_specs",
        "neocortex.safety.state_topology_contracts",
    ),
    "code.discovery_search": (
        "neocortex.code.code_analysis_query",
        "neocortex.code.code_search",
        "neocortex.code.code_semantic_links",
    ),
    "code.experiments": (
        "neocortex.code.code_experiment_executor",
        "neocortex.code.code_experiment_planner",
        "neocortex.code.code_experiment_store",
        "neocortex.code.code_technical_verification",
    ),
    "code.external_providers": (
        "neocortex.code.external_architecture_providers",
        "neocortex.code.external_architecture_worker",
        "neocortex.code.external_deep_coverage",
        "neocortex.code.external_deep_coverage_worker",
        "neocortex.code.external_dependency_hygiene",
        "neocortex.code.external_evidence_models",
        "neocortex.code.external_evidence_providers",
        "neocortex.code.external_evidence_store",
        "neocortex.code.external_git_history",
        "neocortex.code.external_mutation_cosmic_ray",
        "neocortex.code.external_mutation_cosmic_ray_worker",
        "neocortex.code.external_semgrep_invariants",
        "neocortex.code.external_supply_chain_audit",
        "neocortex.code.external_unused_vulture",
        "neocortex.code.external_unused_vulture_worker",
        "neocortex.code.semgrep_tool_contract",
    ),
    "code.ingestion_state": (
        "neocortex.code.code_analyzer_common",
        "neocortex.code.code_analyzers",
        "neocortex.code.code_candidate_scope",
        "neocortex.code.code_detection",
        "neocortex.code.code_generic",
        "neocortex.code.code_projects",
        "neocortex.code.code_python",
        "neocortex.code.code_route",
        "neocortex.code.code_rust",
        "neocortex.code.code_schema",
        "neocortex.code.code_state",
    ),
    "code.review": (
        "neocortex.code.code_review",
        "neocortex.code.code_review_actionability",
        "neocortex.code.code_review_eligibility",
        "neocortex.code.code_review_epistemics",
        "neocortex.code.code_review_models",
        "neocortex.code.code_review_serialization",
        "neocortex.code.code_review_task_analysis",
        "neocortex.code.code_review_work_packages",
    ),
    "code.validation": (
        "neocortex.code.code_change_validation",
        "neocortex.code.code_validation_public_review",
        "neocortex.code.code_validation_receipts",
        "neocortex.code.code_validation_resources",
        "neocortex.code.validation_supply",
    ),
    "documents.catalog": (
        "neocortex.documents",
        "neocortex.documents.document_cache_sync",
        "neocortex.documents.document_catalog",
        "neocortex.documents.document_catalog_schema",
    ),
    "documents.organization": (
        "neocortex.documents.document_naming",
        "neocortex.documents.document_organization",
        "neocortex.documents.document_organization_application",
        "neocortex.documents.document_organization_models",
        "neocortex.documents.document_organization_planning",
    ),
    "documents.taxonomy": (
        "neocortex.documents.document_signals",
        "neocortex.documents.document_taxonomy",
        "neocortex.documents.document_taxonomy_entities",
        "neocortex.documents.document_taxonomy_kinds",
        "neocortex.documents.document_taxonomy_models",
        "neocortex.documents.document_taxonomy_overlay",
        "neocortex.documents.document_taxonomy_references",
        "neocortex.documents.document_taxonomy_vocabulary",
    ),
    "formats.archive": (
        "neocortex.capabilities.formats.archive",
        "neocortex.capabilities.formats.archive.models",
        "neocortex.capabilities.formats.archive.route",
        "neocortex.capabilities.formats.archive.state",
        "neocortex.capabilities.formats.archive.text_worker",
    ),
    "formats.audio": (
        "neocortex.capabilities.formats.audio",
        "neocortex.capabilities.formats.audio.models",
        "neocortex.capabilities.formats.audio.probe",
        "neocortex.capabilities.formats.audio.route",
        "neocortex.capabilities.formats.audio.state",
        "neocortex.capabilities.formats.audio.whisper",
    ),
    "formats.docx": (
        "neocortex.capabilities.formats.docx",
        "neocortex.capabilities.formats.docx.integrity",
        "neocortex.capabilities.formats.docx.layout",
        "neocortex.capabilities.formats.docx.models",
        "neocortex.capabilities.formats.docx.route",
        "neocortex.capabilities.formats.docx.schema",
        "neocortex.capabilities.formats.docx.state",
    ),
    "formats.image_ocr": (
        "neocortex.capabilities.formats.image",
        "neocortex.capabilities.formats.image.adult",
        "neocortex.capabilities.formats.image.analysis",
        "neocortex.capabilities.formats.image.decision",
        "neocortex.capabilities.formats.image.decode",
        "neocortex.capabilities.formats.image.document",
        "neocortex.capabilities.formats.image.errors",
        "neocortex.capabilities.formats.image.features",
        "neocortex.capabilities.formats.image.isolation",
        "neocortex.capabilities.formats.image.models",
        "neocortex.capabilities.formats.image.png",
        "neocortex.capabilities.formats.image.policy",
        "neocortex.capabilities.formats.image.route",
        "neocortex.capabilities.formats.image.semantics",
        "neocortex.capabilities.formats.image.state",
        "neocortex.capabilities.formats.image.visual",
        "neocortex.safety.ocr_image_preprocess",
        "neocortex.safety.ocr_profiles",
    ),
    "formats.office": (
        "neocortex.capabilities.formats.office",
        "neocortex.capabilities.formats.office.extraction",
        "neocortex.capabilities.formats.office.extraction_support",
        "neocortex.capabilities.formats.office.legacy_worker",
        "neocortex.capabilities.formats.office.models",
        "neocortex.capabilities.formats.office.route",
        "neocortex.capabilities.formats.office.state",
        "neocortex.capabilities.formats.office.xlsx",
    ),
    "formats.pdf": (
        "neocortex.capabilities.formats.pdf",
        "neocortex.capabilities.formats.pdf.pdf_admin",
        "neocortex.capabilities.formats.pdf.pdf_cache",
        "neocortex.capabilities.formats.pdf.pdf_derived",
        "neocortex.capabilities.formats.pdf.pdf_derived_queries",
        "neocortex.capabilities.formats.pdf.pdf_derived_schema",
        "neocortex.capabilities.formats.pdf.pdf_isolation",
        "neocortex.capabilities.formats.pdf.pdf_layout",
        "neocortex.capabilities.formats.pdf.pdf_profile",
        "neocortex.capabilities.formats.pdf.pdf_route",
        "neocortex.capabilities.formats.pdf.pdf_route_cache",
        "neocortex.capabilities.formats.pdf.pdf_route_models",
        "neocortex.capabilities.formats.pdf.pdf_route_storage",
        "neocortex.capabilities.formats.pdf.pdf_runtime",
        "neocortex.capabilities.formats.pdf.pdf_schema",
        "neocortex.capabilities.formats.pdf.pdf_state",
        "neocortex.capabilities.formats.pdf.pdf_writer",
    ),
    "formats.text": (
        "neocortex.capabilities.formats.text",
        "neocortex.capabilities.formats.text.text_derivation_repository",
        "neocortex.capabilities.formats.text.text_route",
        "neocortex.capabilities.formats.text.text_state",
    ),
    "formats.video": (
        "neocortex.capabilities.formats.video",
        "neocortex.capabilities.formats.video.frames",
        "neocortex.capabilities.formats.video.models",
        "neocortex.capabilities.formats.video.probe",
        "neocortex.capabilities.formats.video.route",
        "neocortex.capabilities.formats.video.state",
    ),
    "foundation.identity": (
        "neocortex.foundation",
        "neocortex.foundation.file_identity",
        "neocortex.platform",
        "neocortex.platform.content_types",
        "neocortex.platform.policy",
        "neocortex.platform.zip_safety",
    ),
    "foundation.provenance": (
        "neocortex.foundation.processing_provenance",
    ),
    "integrations.inventory": (
        "neocortex.deduplication",
        "neocortex.deduplication.__main__",
        "neocortex.deduplication.domain",
        "neocortex.deduplication.domain.errors",
        "neocortex.deduplication.domain.models",
        "neocortex.deduplication.fingerprinting",
        "neocortex.deduplication.inventory",
        "neocortex.deduplication.inventory.index",
        "neocortex.deduplication.inventory.policy",
        "neocortex.deduplication.inventory.repository_connection",
        "neocortex.deduplication.inventory.repository_files",
        "neocortex.deduplication.inventory.repository_plans",
        "neocortex.deduplication.inventory.repository_reconciliation",
        "neocortex.deduplication.inventory.repository_scans",
        "neocortex.deduplication.inventory.scan",
        "neocortex.deduplication.inventory.scanner",
        "neocortex.deduplication.inventory.traversal",
        "neocortex.deduplication.io",
        "neocortex.deduplication.persistence",
        "neocortex.deduplication.persistence.connections",
        "neocortex.deduplication.persistence.contracts",
        "neocortex.deduplication.persistence.ddl",
        "neocortex.deduplication.persistence.lifecycle",
        "neocortex.deduplication.persistence.migrations",
        "neocortex.deduplication.persistence.migrations.common",
        "neocortex.deduplication.persistence.migrations.v1_to_v2",
        "neocortex.deduplication.persistence.migrations.v2_to_v3",
        "neocortex.deduplication.persistence.migrations.v3_to_v4",
        "neocortex.deduplication.persistence.migrations.v4_to_v5",
        "neocortex.deduplication.persistence.migrations.v5_to_v6",
        "neocortex.deduplication.persistence.migrations.v6_to_v7",
        "neocortex.deduplication.persistence.migrations.v7_to_v8",
        "neocortex.deduplication.persistence.migrations.v8_to_v9",
        "neocortex.deduplication.persistence.migrations.v9_to_v10",
        "neocortex.deduplication.persistence.validation",
        "neocortex.deduplication.planning",
        "neocortex.deduplication.planning.pipeline",
        "neocortex.deduplication.planning.planner",
        "neocortex.enumeration",
        "neocortex.enumeration.errors",
        "neocortex.enumeration.models",
        "neocortex.enumeration.ntfs",
        "neocortex.enumeration.ntfs.enumeration",
        "neocortex.enumeration.ntfs.journal",
        "neocortex.enumeration.ntfs.parser",
        "neocortex.enumeration.ntfs.volume",
        "neocortex.enumeration.path_index",
        "neocortex.enumeration.path_index.repository",
        "neocortex.enumeration.path_index.schema",
        "neocortex.integrations",
        "neocortex.integrations.inventory",
        "neocortex.integrations.inventory.inventory_boundary",
        "neocortex.integrations.inventory.inventory_coordinator",
        "neocortex.integrations.inventory.reconcile",
    ),
    "knowledge.asset_health": (
        "neocortex.knowledge.knowledge_asset_health",
        "neocortex.knowledge.knowledge_asset_health_contracts",
        "neocortex.knowledge.knowledge_asset_health_pdf",
        "neocortex.knowledge.knowledge_asset_health_repository",
    ),
    "knowledge.contracts": (
        "neocortex.knowledge",
        "neocortex.knowledge.knowledge_contract_context",
        "neocortex.knowledge.knowledge_contract_payloads",
        "neocortex.knowledge.knowledge_contract_protocols",
        "neocortex.knowledge.knowledge_contract_references",
        "neocortex.knowledge.knowledge_contract_snapshot",
        "neocortex.knowledge.knowledge_contract_telemetry",
        "neocortex.knowledge.knowledge_contract_validation",
        "neocortex.knowledge.knowledge_contracts",
        "neocortex.knowledge.knowledge_search_contracts",
    ),
    "knowledge.evaluation": (
        "neocortex.knowledge.knowledge_evaluation",
    ),
    "knowledge.planning": (
        "neocortex.knowledge.knowledge_planner",
        "neocortex.knowledge.knowledge_planner_exact",
        "neocortex.knowledge.knowledge_planner_intents",
        "neocortex.knowledge.knowledge_planner_steps",
    ),
    "knowledge.retrieval": (
        "neocortex.knowledge.knowledge_context",
        "neocortex.knowledge.knowledge_exact",
        "neocortex.knowledge.knowledge_search",
        "neocortex.knowledge.knowledge_search_catalog",
        "neocortex.knowledge.knowledge_search_code",
        "neocortex.knowledge.knowledge_search_content",
        "neocortex.knowledge.knowledge_search_fusion",
        "neocortex.knowledge.knowledge_search_inventory",
        "neocortex.knowledge.knowledge_service",
    ),
    "knowledge.snapshot": (
        "neocortex.knowledge.knowledge_snapshot",
    ),
    "persistence.framework": (
        "neocortex.persistence",
        "neocortex.persistence.framework_connection",
        "neocortex.persistence.framework_route_state",
        "neocortex.persistence.framework_schema",
        "neocortex.persistence.framework_state_common",
        "neocortex.persistence.framework_state_writer",
        "neocortex.persistence.sqlite_backup",
        "neocortex.persistence.sqlite_cancellation",
        "neocortex.persistence.sqlite_connection",
        "neocortex.persistence.sqlite_immutable",
        "neocortex.persistence.sqlite_integrity",
        "neocortex.persistence.sqlite_paths",
        "neocortex.persistence.sqlite_schema_contract",
        "neocortex.persistence.sqlite_schema_lifecycle",
    ),
    "runtime.config": (
        "neocortex.runtime.config",
        "neocortex.runtime.config.app_paths",
        "neocortex.runtime.config.application_config",
        "neocortex.runtime.config.application_config_projections",
        "neocortex.runtime.config.model_management",
    ),
    "runtime.control": (
        "neocortex.progress",
        "neocortex.progress.events",
        "neocortex.progress.line",
        "neocortex.progress.reporters",
        "neocortex.progress.rich",
        "neocortex.runtime",
        "neocortex.runtime.control",
        "neocortex.runtime.control.bounded_subprocess",
        "neocortex.runtime.control.cancellation",
        "neocortex.runtime.control.console_cancellation",
        "neocortex.runtime.control.cpu_runtime",
        "neocortex.runtime.control.global_resources",
        "neocortex.runtime.control.incremental_gate",
        "neocortex.runtime.control.isolated_process",
        "neocortex.runtime.control.locking",
        "neocortex.runtime.control.memory_runtime",
        "neocortex.runtime.control.retry_policy",
        "neocortex.runtime.control.watcher",
        "neocortex.runtime.control.watcher_life_lease",
        "neocortex.runtime.source_staging",
    ),
    "runtime.orchestration": (
        "neocortex.runtime.orchestration",
        "neocortex.runtime.orchestration.orchestrator",
        "neocortex.runtime.orchestration.route_registry",
        "neocortex.runtime.orchestration.route_selection",
        "neocortex.runtime.orchestration.run_lifecycle",
        "neocortex.runtime.orchestration.run_status",
    ),
    "safety.access_policy": (
        "neocortex.safety",
        "neocortex.safety.corpus_access",
        "neocortex.safety.internal_paths",
        "neocortex.safety.protected_content",
        "neocortex.safety.route_filters",
        "neocortex.safety.windows_handle_mutation",
    ),
    "semantic.backends_workers": (
        "neocortex.semantic.semantic_backend_supervisor",
        "neocortex.semantic.semantic_backends",
        "neocortex.semantic.semantic_generation_worker",
        "neocortex.semantic.semantic_image_index",
        "neocortex.semantic.semantic_text_index",
    ),
    "semantic.contracts": (
        "neocortex.semantic",
        "neocortex.semantic.semantic_config",
        "neocortex.semantic.semantic_contract_payloads",
        "neocortex.semantic.semantic_contract_validation",
        "neocortex.semantic.semantic_models",
        "neocortex.semantic.semantic_ontology",
        "neocortex.semantic.semantic_quality",
        "neocortex.semantic.semantic_service_contracts",
    ),
    "semantic.persistence_lineage": (
        "neocortex.semantic.derivation_contracts",
        "neocortex.semantic.derivation_lineage_service",
        "neocortex.semantic.derivation_projection",
        "neocortex.semantic.semantic_evidence_repository",
        "neocortex.semantic.semantic_generation_repository",
        "neocortex.semantic.semantic_item_repository",
        "neocortex.semantic.semantic_lineage_repository",
        "neocortex.semantic.semantic_repository_common",
        "neocortex.semantic.semantic_schema",
        "neocortex.semantic.semantic_state",
    ),
    "semantic.planning": (
        "neocortex.semantic.semantic_plan_errors",
        "neocortex.semantic.semantic_plan_owners",
        "neocortex.semantic.semantic_plan_results",
        "neocortex.semantic.semantic_plan_scratch",
        "neocortex.semantic.semantic_planner",
        "neocortex.semantic.semantic_work_budget",
    ),
    "semantic.search_service": (
        "neocortex.semantic.semantic_lexical",
        "neocortex.semantic.semantic_search_repository",
        "neocortex.semantic.semantic_search_service",
        "neocortex.semantic.semantic_service",
        "neocortex.semantic.semantic_status_service",
    ),
    "semantic.sources_preparation": (
        "neocortex.semantic.semantic_chunking",
        "neocortex.semantic.semantic_classification_service",
        "neocortex.semantic.semantic_preparation",
        "neocortex.semantic.semantic_sources",
    ),
    "workflow.actions_recovery": (
        "neocortex.workflow",
        "neocortex.workflow.actions",
        "neocortex.workflow.actions.action_policy",
        "neocortex.workflow.actions.actions",
        "neocortex.workflow.actions.file_action_reconciliation_store",
        "neocortex.workflow.actions.file_action_recovery",
    ),
    "workflow.retention": (
        "neocortex.workflow.retention",
        "neocortex.workflow.retention.planner",
    ),
    "workflow.review": (
        "neocortex.workflow.review",
        "neocortex.workflow.review.review",
        "neocortex.workflow.review.review_evidence",
        "neocortex.workflow.review.review_task_contracts",
        "neocortex.workflow.review.review_task_repository",
        "neocortex.workflow.review.value_review",
        "neocortex.workflow.review.value_review_contracts",
        "neocortex.workflow.review.value_review_port",
        "neocortex.workflow.review.value_review_repository",
        "neocortex.workflow.review.value_review_tasks",
    ),
    "workflow.self_analysis": (
        "neocortex.workflow.self_analysis",
        "neocortex.workflow.self_analysis.self_analysis",
        "neocortex.workflow.self_analysis.self_analysis_finalization",
        "neocortex.workflow.self_analysis.self_analysis_freshness",
        "neocortex.workflow.self_analysis.self_analysis_manifest",
        "neocortex.workflow.self_analysis.self_analysis_status",
    ),
}

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
    FamilyEdgeBaseline("code", "api", 5),
    FamilyEdgeBaseline("code", "knowledge", 9),
    FamilyEdgeBaseline("code", "runtime", 25),
    FamilyEdgeBaseline("code", "semantic", 43),
    FamilyEdgeBaseline("code", "workflow", 11),
    FamilyEdgeBaseline("documents", "runtime", 4),
    FamilyEdgeBaseline("documents", "semantic", 1),
    FamilyEdgeBaseline("documents", "workflow", 2),
    FamilyEdgeBaseline("formats", "api", 3),
    FamilyEdgeBaseline("formats", "knowledge", 2),
    FamilyEdgeBaseline("formats", "runtime", 47),
    FamilyEdgeBaseline("formats", "semantic", 8),
    FamilyEdgeBaseline("formats", "workflow", 11),
    FamilyEdgeBaseline("foundation", "integrations", 1),
    FamilyEdgeBaseline("foundation", "runtime", 1),
    FamilyEdgeBaseline("integrations", "api", 1),
    FamilyEdgeBaseline("integrations", "persistence", 12),
    FamilyEdgeBaseline("integrations", "runtime", 10),
    FamilyEdgeBaseline("knowledge", "api", 1),
    FamilyEdgeBaseline("knowledge", "workflow", 2),
    FamilyEdgeBaseline("persistence", "workflow", 8),
    FamilyEdgeBaseline("runtime", "api", 6),
    FamilyEdgeBaseline("safety", "integrations", 1),
    FamilyEdgeBaseline("safety", "runtime", 1),
    FamilyEdgeBaseline("semantic", "knowledge", 4),
    FamilyEdgeBaseline("semantic", "runtime", 5),
    FamilyEdgeBaseline("workflow", "api", 2),
    FamilyEdgeBaseline("workflow", "runtime", 2),
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
        },
        "family_dag": {
            "schema": CORE_FAMILY_DAG_SCHEMA,
            "edge_semantics": "importer-may-depend-on-later-layer-v1",
            "layer_order": list(TARGET_FAMILY_LAYERS),
            "direct_dependencies": [list(item) for item in TARGET_FAMILY_DEPENDENCIES],
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
def matching_target_responsibilities(module_id: str) -> tuple[str, ...]:
    """Return the one explicit product responsibility, never a default."""

    responsibility = _MODULE_RESPONSIBILITIES.get(module_id)
    return () if responsibility is None else (responsibility,)


def matching_target_families(module_id: str) -> tuple[str, ...]:
    """Return one exact canonical target family, never a legacy fallback."""

    responsibility = _MODULE_RESPONSIBILITIES.get(module_id)
    return () if responsibility is None else (_responsibility_family(responsibility),)


def forbidden_family_edge_baseline() -> dict[tuple[str, str], int]:
    return {
        (item.source_family, item.target_family): item.direct_module_edges
        for item in FORBIDDEN_FAMILY_EDGE_BASELINE
    }


def registered_core_modules() -> tuple[str, ...]:
    return tuple(sorted(_MODULE_RESPONSIBILITIES))


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
    _validated_assigned_modules()
    _validate_family_transition_baseline()


_validate_registry()


__all__ = [
    "CORE_ARCHITECTURE_FINGERPRINT_PREFIX",
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
    "FamilyEdgeBaseline",
    "core_architecture_target_fingerprint",
    "core_architecture_target_payload",
    "forbidden_family_edge_baseline",
    "matching_target_families",
    "matching_target_responsibilities",
    "registered_core_modules",
]
