"""Canonical table lifecycle declarations consumed by the public owner registry.

These are table semantics, not filesystem classifications. Unknown tables
have no reset or retention authority, including when they are empty.
Owner-local transforms and dependency checks remain with each owner.
"""
from __future__ import annotations

from collections.abc import Iterable

from neocortex.safety.state_lifecycle_contracts import TableLifecycleRole, TableLifecycleRule

LIFECYCLE_POLICY_VERSION = 1
_PROTECTED_METADATA_MARKERS = ("policy", "policie", "correction", "review", "recovery", "authorization", "decision", "evidence", "receipt")


def metadata_requires_owner_preservation(keys: Iterable[str]) -> bool:
    """Conservatively protect legacy metadata whose authority is not normalized.

    This only withholds removal authority; it never classifies a table or
    makes an unrecognized object disposable.
    """
    return any(marker in key.casefold() for key in keys for marker in _PROTECTED_METADATA_MARKERS)

# Every table appears once. FTS shadows inherit an explicitly declared role.
_DECLARATIONS: dict[str, dict[TableLifecycleRole, tuple[str, ...]]] = {
    'framework': {
        'authoritative': ('curation_authorization_grants', 'file_action_events', 'file_action_reconciliation_events', 'file_actions', 'review_candidates', 'review_decisions', 'review_evidence_examples', 'review_evidence_progress', 'review_task_batch_memberships', 'review_task_batches', 'review_task_events', 'review_task_scan_progress', 'review_task_source_publications', 'review_tasks', 'semantic_content_admission_events', 'semantic_content_admission_policies'),
        'operational': ('initial_runs', 'route_candidates', 'route_phase_runs', 'route_runs', 'run_actions', 'run_events'),
        'schema_metadata': ('metadata',),
        'derived': ('content_type_cache',),
    },
    'inventory': {
        'authoritative': ('duplicate_plan_summaries', 'fingerprint_content_evidence', 'planned_duplicate_groups', 'planned_duplicate_members'),
        'operational': ('duplicate_plan_heads', 'inventory_checkpoints', 'inventory_generation_heads', 'inventory_scan_successors', 'scans'),
        'schema_metadata': ('metadata',),
        'derived': ('files', 'fingerprints'),
    },
    'catalog': {
        'authoritative': ('catalog_generation_manifests', 'classification_corrections', 'classification_history', 'organization_plans'),
        'operational': ('catalog_generations', 'catalog_publications', 'catalog_runs'),
        'schema_metadata': ('metadata',),
        'derived': ('catalog_generation_documents', 'documents'),
    },
    'semantic': {
        'authoritative': ('semantic_derivation_outbox', 'semantic_evidence', 'semantic_work_receipts'),
        'operational': ('embedding_generations', 'embedding_jobs', 'published_embedding_heads'),
        'schema_metadata': ('metadata', 'schema_migrations'),
        'derived': ('embedding_generation_members', 'embedding_models', 'image_embeddings', 'label_prototypes', 'semantic_chunk_derivations', 'semantic_chunk_revisions', 'semantic_item_revisions', 'semantic_items', 'text_channel_revisions', 'text_chunks', 'text_embeddings', 'vector_payloads', 'vector_spaces'),
    },
    'pdf': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('document_warnings', 'documents', 'page_fts_state', 'text_signatures', 'similarity_buckets', 'similarity_relations', 'similarity_state', 'page_layouts', 'document_layouts', 'layout_groups', 'layout_group_members', 'page_errors', 'page_fts', 'page_fts_config', 'page_fts_content', 'page_fts_data', 'page_fts_docsize', 'page_fts_idx', 'page_staging', 'pages', 'pdf_inventory'),
    },
    'docx': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('document_diagnostics', 'document_fts', 'document_fts_config', 'document_fts_content', 'document_fts_data', 'document_fts_docsize', 'document_fts_idx', 'document_parts', 'documents', 'docx_inventory', 'layout_groups', 'pdf_counterparts'),
    },
    'office': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('document_fts', 'document_fts_config', 'document_fts_content', 'document_fts_data', 'document_fts_docsize', 'document_fts_idx', 'documents', 'office_inventory', 'xlsx_cells'),
    },
    'audio': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('audio_inventory', 'documents', 'segments', 'transcript_fts', 'transcript_fts_config', 'transcript_fts_content', 'transcript_fts_data', 'transcript_fts_docsize', 'transcript_fts_idx'),
    },
    'video': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('documents', 'frame_fts', 'frame_fts_config', 'frame_fts_content', 'frame_fts_data', 'frame_fts_docsize', 'frame_fts_idx', 'frames', 'video_inventory'),
    },
    'image': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('images', 'images_without_nudenet'),
    },
    'archive': {
        'authoritative': (),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('archive_issues', 'archive_logical_documents', 'containers', 'document_fts', 'document_fts_config', 'document_fts_content', 'document_fts_data', 'document_fts_docsize', 'document_fts_idx', 'documents'),
    },
    'text': {
        'authoritative': ('text_derivation_outbox', 'text_work_receipts'),
        'operational': (),
        'schema_metadata': ('metadata',),
        'derived': ('document_fts', 'document_fts_config', 'document_fts_content', 'document_fts_data', 'document_fts_docsize', 'document_fts_idx', 'documents', 'text_derivation_attempts', 'text_derivation_input_bindings', 'text_derivation_output_bindings', 'text_input_revisions', 'text_materialization_heads', 'text_materializations'),
    },
    'code': {
        'authoritative': ('code_experiment_receipts',),
        'operational': ('analysis_runs', 'graph_generations', 'graph_heads'),
        'schema_metadata': ('graph_generation_migrations', 'metadata', 'schema_migrations'),
        'derived': ('code_chunks', 'code_fts', 'code_fts_config', 'code_fts_content', 'code_fts_data', 'code_fts_docsize', 'code_fts_idx', 'code_references', 'dependencies', 'diagnostics', 'embedding_links', 'external_findings', 'external_metrics', 'external_relations', 'external_run_contracts', 'external_run_counters', 'external_run_inputs', 'external_run_replays', 'external_tool_runs', 'file_versions', 'files', 'graph_batches', 'graph_checkpoints', 'graph_generation_metadata', 'graph_input_snapshots', 'graph_memberships', 'graph_snapshot_inputs', 'invalidation_history', 'metrics', 'project_edges', 'project_memberships', 'projects', 'symbols', 'version_relations'),
    },
}

def owner_lifecycle_rules(owner: str) -> tuple[TableLifecycleRule, ...]:
    """Resolve declarations without importing any owner runtime."""
    declarations = _DECLARATIONS[owner]
    return tuple(
        TableLifecycleRule(
            table=table, role=role,
            reconstruction_source=(None if role == "authoritative" else "owner-validated-inputs"),
            retention_policy=("explicit-authority" if role == "authoritative" else "unreferenced-only"),
            dependency_selector=f"{owner}:references",
            durability_boundary=f"{owner}:transaction",
            reset_action=("preserve" if role in {"authoritative", "schema_metadata"} else "owner-transform"),
        )
        for role, tables in declarations.items() for table in tables
    )
