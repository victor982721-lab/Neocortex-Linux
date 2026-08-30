"""Versioned SQLite schema for structured source-code intelligence."""

from __future__ import annotations
import os
import sqlite3
import stat as stat_module
import time
from collections.abc import Callable
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from neocortex.persistence.sqlite_connection import (
    READONLY_EXISTING,
    READWRITE_CREATE,
    READWRITE_EXISTING,
    SQLiteConnectionPolicy,
    SQLiteWriterPragmas,
    connect_sqlite,
)
from neocortex.platform.policy import sqlite_path_collation

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)

# region [01] Versioned DDL


CODE_SCHEMA_VERSION = 7
_PATH_COLLATION = sqlite_path_collation()


def _files_table_ddl(path_collation: str) -> str:
    if path_collation not in {"BINARY", "NOCASE"}:
        raise ValueError(f"unsupported Code path collation: {path_collation}")
    return f"""CREATE TABLE files(
        file_id INTEGER PRIMARY KEY,
        volume_id TEXT NOT NULL,
        physical_file_id TEXT NOT NULL,
        current_path TEXT NOT NULL COLLATE {path_collation},
        current_version_id INTEGER,
        status TEXT NOT NULL CHECK(status IN ('current','missing','stale')),
        first_seen_run_id INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL,
        UNIQUE(volume_id,physical_file_id),
        FOREIGN KEY(current_version_id) REFERENCES file_versions(version_id)
            DEFERRABLE INITIALLY DEFERRED
    )"""


_FILES_TABLE_DDL = _files_table_ddl(_PATH_COLLATION)
_LEGACY_FILES_TABLE_DDL = _files_table_ddl("NOCASE")
_FILES_CURRENT_PATH_INDEX_DDL = """CREATE UNIQUE INDEX files_current_path_idx
        ON files(current_path) WHERE status='current'"""
_FILES_LAST_SEEN_INDEX_DDL = (
    "CREATE INDEX files_last_seen_idx ON files(last_seen_run_id,status,file_id)"
)

_V1_DDL: tuple[str, ...] = (
    """CREATE TABLE metadata(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE schema_migrations(
        version INTEGER PRIMARY KEY CHECK(version>0),
        description TEXT NOT NULL,
        applied_ns INTEGER NOT NULL CHECK(applied_ns>0)
    )""",
    """CREATE TABLE analysis_runs(
        analysis_run_id INTEGER PRIMARY KEY,
        framework_run_id INTEGER NOT NULL,
        scan_id INTEGER NOT NULL,
        processing_signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'running','completed','partial','failed','cancelled','interrupted'
        )),
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        candidates INTEGER NOT NULL DEFAULT 0 CHECK(candidates>=0),
        processed INTEGER NOT NULL DEFAULT 0 CHECK(processed>=0),
        cache_hits INTEGER NOT NULL DEFAULT 0 CHECK(cache_hits>=0),
        errors INTEGER NOT NULL DEFAULT 0 CHECK(errors>=0),
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT
    )""",
    """CREATE INDEX analysis_runs_framework_idx
        ON analysis_runs(framework_run_id,analysis_run_id)""",
    """CREATE INDEX analysis_runs_status_idx
        ON analysis_runs(status,started_ns)""",
    _FILES_TABLE_DDL,
    _FILES_CURRENT_PATH_INDEX_DDL,
    _FILES_LAST_SEEN_INDEX_DDL,
    """CREATE TABLE file_versions(
        version_id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL,
        path_observed TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL CHECK(size>=0),
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        raw_xxh3_128 TEXT,
        raw_xxh3_64_guard TEXT,
        text_xxh3_128 TEXT,
        text_xxh3_64_guard TEXT,
        normalized_xxh3_128 TEXT,
        token_xxh3_128 TEXT,
        structure_xxh3_128 TEXT,
        encoding TEXT,
        language TEXT,
        artifact_kind TEXT NOT NULL,
        generated INTEGER NOT NULL CHECK(generated IN (0,1)),
        vendored INTEGER NOT NULL CHECK(vendored IN (0,1)),
        classification_confidence REAL NOT NULL
            CHECK(classification_confidence>=0.0 AND classification_confidence<=1.0),
        classification_evidence_json TEXT NOT NULL,
        analysis_status TEXT NOT NULL CHECK(analysis_status IN (
            'complete','partial','text_only','skipped_limit','binary','error'
        )),
        processing_signature TEXT NOT NULL,
        analyzer_id TEXT NOT NULL,
        analyzer_version TEXT NOT NULL,
        parser_kind TEXT NOT NULL,
        text_zlib BLOB,
        text_chars INTEGER NOT NULL DEFAULT 0 CHECK(text_chars>=0),
        text_truncated INTEGER NOT NULL DEFAULT 0 CHECK(text_truncated IN (0,1)),
        provenance_json TEXT NOT NULL,
        first_observed_run_id INTEGER NOT NULL,
        last_observed_run_id INTEGER NOT NULL,
        valid_from_ns INTEGER NOT NULL,
        invalidated_ns INTEGER,
        invalidation_reason TEXT,
        FOREIGN KEY(file_id) REFERENCES files(file_id) ON DELETE RESTRICT
    )""",
    """CREATE UNIQUE INDEX file_versions_current_idx
        ON file_versions(file_id) WHERE invalidated_ns IS NULL""",
    """CREATE INDEX file_versions_path_idx
        ON file_versions(path_observed,invalidated_ns,version_id)""",
    """CREATE INDEX file_versions_language_idx
        ON file_versions(language,artifact_kind,invalidated_ns,version_id)""",
    """CREATE INDEX file_versions_exact_hash_idx
        ON file_versions(raw_xxh3_128,size,invalidated_ns,version_id)""",
    """CREATE INDEX file_versions_normalized_hash_idx
        ON file_versions(normalized_xxh3_128,language,invalidated_ns,version_id)""",
    """CREATE TABLE invalidation_history(
        invalidation_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        invalidated_ns INTEGER NOT NULL,
        reason TEXT NOT NULL,
        replacement_version_id INTEGER,
        evidence_json TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(replacement_version_id) REFERENCES file_versions(version_id)
    )""",
    """CREATE INDEX invalidation_version_idx
        ON invalidation_history(version_id,invalidation_id)""",
    """CREATE TABLE symbols(
        symbol_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        parent_symbol_id INTEGER,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        qualified_name TEXT NOT NULL,
        signature TEXT,
        visibility TEXT,
        docstring TEXT,
        confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        complexity INTEGER CHECK(complexity IS NULL OR complexity>=0),
        start_line INTEGER NOT NULL CHECK(start_line>0),
        start_column INTEGER NOT NULL CHECK(start_column>=0),
        end_line INTEGER NOT NULL CHECK(end_line>=start_line),
        end_column INTEGER NOT NULL CHECK(end_column>=0),
        start_byte INTEGER NOT NULL CHECK(start_byte>=0),
        end_byte INTEGER NOT NULL CHECK(end_byte>=start_byte),
        metadata_json TEXT NOT NULL,
        UNIQUE(version_id,kind,qualified_name,start_byte),
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE,
        FOREIGN KEY(parent_symbol_id) REFERENCES symbols(symbol_id)
    )""",
    """CREATE INDEX symbols_name_idx
        ON symbols(name,kind,version_id)""",
    """CREATE INDEX symbols_qualified_idx
        ON symbols(qualified_name,version_id)""",
    """CREATE INDEX symbols_complexity_idx
        ON symbols(complexity DESC,version_id)""",
    """CREATE TABLE code_references(
        reference_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        source_symbol_id INTEGER,
        target_symbol_id INTEGER,
        target_version_id INTEGER,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        target_hint TEXT,
        confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence TEXT NOT NULL,
        start_line INTEGER NOT NULL CHECK(start_line>0),
        start_column INTEGER NOT NULL CHECK(start_column>=0),
        end_line INTEGER NOT NULL CHECK(end_line>=start_line),
        end_column INTEGER NOT NULL CHECK(end_column>=0),
        start_byte INTEGER NOT NULL CHECK(start_byte>=0),
        end_byte INTEGER NOT NULL CHECK(end_byte>=start_byte),
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE,
        FOREIGN KEY(source_symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(target_symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(target_version_id) REFERENCES file_versions(version_id)
    )""",
    """CREATE INDEX code_references_name_idx
        ON code_references(name,kind,version_id)""",
    """CREATE INDEX code_references_target_idx
        ON code_references(target_symbol_id,kind,reference_id)""",
    """CREATE TABLE dependencies(
        dependency_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        resolved_version_id INTEGER,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        scope TEXT,
        version_spec TEXT,
        confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence TEXT NOT NULL,
        start_line INTEGER,
        start_column INTEGER,
        end_line INTEGER,
        end_column INTEGER,
        start_byte INTEGER,
        end_byte INTEGER,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE,
        FOREIGN KEY(resolved_version_id) REFERENCES file_versions(version_id)
    )""",
    """CREATE INDEX dependencies_name_idx
        ON dependencies(name,kind,version_id)""",
    """CREATE INDEX dependencies_resolved_idx
        ON dependencies(resolved_version_id,dependency_id)""",
    """CREATE TABLE diagnostics(
        diagnostic_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        code TEXT NOT NULL,
        severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
        message TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        start_line INTEGER,
        start_column INTEGER,
        end_line INTEGER,
        end_column INTEGER,
        start_byte INTEGER,
        end_byte INTEGER,
        metadata_json TEXT NOT NULL,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE
    )""",
    """CREATE INDEX diagnostics_lookup_idx
        ON diagnostics(code,severity,version_id)""",
    """CREATE INDEX diagnostics_source_idx
        ON diagnostics(source,tool_name,version_id)""",
    """CREATE TABLE metrics(
        metric_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        symbol_id INTEGER,
        name TEXT NOT NULL,
        value REAL NOT NULL,
        confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        provenance TEXT NOT NULL,
        UNIQUE(version_id,symbol_id,name,provenance),
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE,
        FOREIGN KEY(symbol_id) REFERENCES symbols(symbol_id)
    )""",
    """CREATE INDEX metrics_lookup_idx
        ON metrics(name,value DESC,version_id)""",
    """CREATE TABLE code_chunks(
        chunk_id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL,
        symbol_id INTEGER,
        chunk_index INTEGER NOT NULL CHECK(chunk_index>=0),
        kind TEXT NOT NULL,
        start_line INTEGER NOT NULL CHECK(start_line>0),
        end_line INTEGER NOT NULL CHECK(end_line>=start_line),
        start_byte INTEGER NOT NULL CHECK(start_byte>=0),
        end_byte INTEGER NOT NULL CHECK(end_byte>=start_byte),
        text TEXT NOT NULL,
        text_xxh3_128 TEXT NOT NULL,
        UNIQUE(version_id,chunk_index),
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id) ON DELETE CASCADE,
        FOREIGN KEY(symbol_id) REFERENCES symbols(symbol_id)
    )""",
    """CREATE INDEX code_chunks_symbol_idx
        ON code_chunks(symbol_id,chunk_index)""",
    """CREATE VIRTUAL TABLE code_fts USING fts5(
        chunk_id UNINDEXED,
        version_id UNINDEXED,
        path,
        project,
        language UNINDEXED,
        symbol,
        signature,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TABLE version_relations(
        relation_id INTEGER PRIMARY KEY,
        left_version_id INTEGER NOT NULL,
        right_version_id INTEGER NOT NULL,
        relation_kind TEXT NOT NULL CHECK(relation_kind IN (
            'exact_duplicate','normalized_duplicate','token_similar',
            'structure_similar','predecessor','divergent_same_name'
        )),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence_json TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        UNIQUE(left_version_id,right_version_id,relation_kind),
        CHECK(left_version_id<right_version_id),
        FOREIGN KEY(left_version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(right_version_id) REFERENCES file_versions(version_id)
    )""",
    """CREATE INDEX version_relations_right_idx
        ON version_relations(right_version_id,relation_kind,left_version_id)""",
)

# Schemas v1-v4 predate the cross-platform path policy and therefore always
# used SQLite NOCASE for the one path that owns current filesystem identity.
# Keep an exact historical builder so a Linux migration never accepts a
# partially altered or future-shaped source database.
_CURRENT_V1_DDL = _V1_DDL
_V1_DDL = tuple(
    _LEGACY_FILES_TABLE_DDL if statement == _FILES_TABLE_DDL else statement
    for statement in _CURRENT_V1_DDL
)
_LEGACY_V1_DDL = _V1_DDL

_V2_DDL = (
    """CREATE TABLE projects(
        project_id INTEGER PRIMARY KEY,
        project_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        ecosystem TEXT NOT NULL,
        probable_root TEXT COLLATE NOCASE,
        manifest_kind TEXT,
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence_json TEXT NOT NULL,
        first_seen_run_id INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('current','historical','ambiguous'))
    )""",
    """CREATE INDEX projects_name_idx
        ON projects(name,ecosystem,status,project_id)""",
    """CREATE TABLE project_memberships(
        project_id INTEGER NOT NULL,
        version_id INTEGER NOT NULL,
        proposed_path TEXT NOT NULL,
        relation TEXT NOT NULL,
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        selected INTEGER NOT NULL CHECK(selected IN (0,1)),
        conflict_group TEXT,
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(project_id,version_id),
        FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX project_membership_path_idx
        ON project_memberships(project_id,proposed_path,selected,version_id)""",
    """CREATE INDEX project_membership_version_idx
        ON project_memberships(version_id,project_id)""",
    """CREATE TABLE project_edges(
        source_project_id INTEGER NOT NULL,
        target_project_id INTEGER,
        dependency_name TEXT NOT NULL,
        edge_kind TEXT NOT NULL,
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(source_project_id,dependency_name,edge_kind),
        FOREIGN KEY(source_project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
        FOREIGN KEY(target_project_id) REFERENCES projects(project_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE embedding_links(
        chunk_id INTEGER NOT NULL,
        semantic_item_id TEXT NOT NULL,
        model_signature TEXT NOT NULL,
        vector_space TEXT NOT NULL,
        generation_id INTEGER NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        provenance_json TEXT NOT NULL,
        PRIMARY KEY(chunk_id,model_signature,generation_id),
        FOREIGN KEY(chunk_id) REFERENCES code_chunks(chunk_id) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE INDEX embedding_links_active_idx
        ON embedding_links(model_signature,active,chunk_id)""",
    """CREATE TABLE external_tool_runs(
        tool_run_id INTEGER PRIMARY KEY,
        analysis_run_id INTEGER NOT NULL,
        project_id INTEGER,
        tool_name TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        configuration_signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'completed','failed','timeout','unavailable','skipped'
        )),
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER NOT NULL,
        provenance_json TEXT NOT NULL,
        FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(analysis_run_id),
        FOREIGN KEY(project_id) REFERENCES projects(project_id)
    )""",
    """CREATE INDEX external_tool_runs_lookup_idx
        ON external_tool_runs(tool_name,tool_version,status,analysis_run_id)""",
)


_V3_DDL = (
    """CREATE TABLE external_run_contracts(
        tool_run_id INTEGER PRIMARY KEY,
        provider_id TEXT NOT NULL,
        provider_schema TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source LIKE 'external:%'),
        profile TEXT NOT NULL CHECK(profile IN (
            'protected','trusted-static','trusted-deep'
        )),
        trust_requirement TEXT NOT NULL CHECK(trust_requirement IN (
            'untrusted-safe','trusted-static','trusted-execution'
        )),
        scope TEXT NOT NULL,
        observed_root TEXT NOT NULL COLLATE NOCASE,
        root_identity TEXT NOT NULL,
        project_configuration_digest TEXT,
        environment_signature TEXT NOT NULL,
        input_signature TEXT NOT NULL,
        comparability_signature TEXT NOT NULL,
        execution_strategy TEXT NOT NULL,
        invalidation_strategy TEXT NOT NULL CHECK(invalidation_strategy IN (
            'file_local','module_closure','dependency_closure',
            'project_wide','dynamic_suite'
        )),
        cache_policy TEXT NOT NULL,
        execution TEXT NOT NULL CHECK(execution IN (
            'full','cache_replay','skipped','attempted','unavailable'
        )),
        result_digest TEXT,
        portable_publication_id TEXT NOT NULL,
        authority TEXT NOT NULL CHECK(authority='advisory'),
        mutation_authority INTEGER NOT NULL CHECK(mutation_authority=0),
        loads_project_configuration INTEGER NOT NULL CHECK(
            loads_project_configuration IN (0,1)
        ),
        loads_plugins INTEGER NOT NULL CHECK(loads_plugins IN (0,1)),
        imports_content INTEGER NOT NULL CHECK(imports_content IN (0,1)),
        executes_content INTEGER NOT NULL CHECK(executes_content IN (0,1)),
        uses_network INTEGER NOT NULL CHECK(uses_network IN (0,1)),
        coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0,1)),
        limitations_json TEXT NOT NULL,
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE INDEX external_run_contracts_provider_idx
        ON external_run_contracts(provider_id,profile,tool_run_id DESC)""",
    """CREATE INDEX external_run_contracts_comparable_idx
        ON external_run_contracts(
            provider_id,comparability_signature,tool_run_id DESC
        )""",
    """CREATE INDEX external_run_contracts_exact_idx
        ON external_run_contracts(
            provider_id,input_signature,tool_run_id DESC
        )""",
    """CREATE TABLE external_run_inputs(
        tool_run_id INTEGER NOT NULL,
        version_id INTEGER NOT NULL,
        portable_input_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
        covered INTEGER NOT NULL CHECK(covered IN (0,1)),
        coverage_reason TEXT,
        size INTEGER NOT NULL CHECK(size>=0),
        content_digest TEXT NOT NULL,
        PRIMARY KEY(tool_run_id,portable_input_id),
        UNIQUE(tool_run_id,version_id),
        CHECK(covered<=eligible),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX external_run_inputs_coverage_idx
        ON external_run_inputs(tool_run_id,eligible,covered)""",
    """CREATE TABLE external_findings(
        finding_id INTEGER PRIMARY KEY,
        tool_run_id INTEGER NOT NULL,
        portable_finding_id TEXT NOT NULL,
        version_id INTEGER,
        symbol_id INTEGER,
        project_id INTEGER,
        category TEXT NOT NULL,
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        observation_confirmed INTEGER NOT NULL CHECK(
            observation_confirmed IN (0,1)
        ),
        tool_confidence REAL CHECK(
            tool_confidence IS NULL OR
            (tool_confidence>=0.0 AND tool_confidence<=1.0)
        ),
        calibrated_confidence REAL CHECK(
            calibrated_confidence IS NULL OR
            (calibrated_confidence>=0.0 AND calibrated_confidence<=1.0)
        ),
        gate_authority TEXT NOT NULL,
        mutation_authority INTEGER NOT NULL CHECK(mutation_authority=0),
        start_line INTEGER,
        start_column INTEGER,
        end_line INTEGER,
        end_column INTEGER,
        metadata_json TEXT NOT NULL,
        projected_diagnostic_id INTEGER,
        UNIQUE(tool_run_id,portable_finding_id),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(project_id) REFERENCES projects(project_id),
        FOREIGN KEY(projected_diagnostic_id) REFERENCES diagnostics(diagnostic_id)
            ON DELETE SET NULL
    )""",
    """CREATE INDEX external_findings_run_idx
        ON external_findings(tool_run_id,category,severity)""",
    """CREATE INDEX external_findings_version_idx
        ON external_findings(version_id,tool_run_id)""",
    """CREATE TABLE external_run_replays(
        tool_run_id INTEGER PRIMARY KEY,
        source_tool_run_id INTEGER NOT NULL,
        verification_signature TEXT NOT NULL,
        files_verified INTEGER NOT NULL CHECK(files_verified>=0),
        bytes_verified INTEGER NOT NULL CHECK(bytes_verified>=0),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE,
        FOREIGN KEY(source_tool_run_id) REFERENCES external_tool_runs(tool_run_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE external_run_counters(
        tool_run_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        value INTEGER NOT NULL CHECK(value>=0),
        PRIMARY KEY(tool_run_id,name),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE
    ) WITHOUT ROWID""",
)


_V4_DDL = (
    """CREATE TABLE external_metrics(
        metric_id INTEGER PRIMARY KEY,
        tool_run_id INTEGER NOT NULL,
        portable_metric_id TEXT NOT NULL,
        subject_kind TEXT NOT NULL CHECK(subject_kind IN (
            'file','symbol','module','project','run','contract','scc'
        )),
        subject_key TEXT NOT NULL,
        category TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        value REAL NOT NULL,
        unit TEXT NOT NULL,
        version_id INTEGER,
        symbol_id INTEGER,
        project_id INTEGER,
        metadata_json TEXT NOT NULL,
        UNIQUE(tool_run_id,portable_metric_id),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE,
        FOREIGN KEY(version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(project_id) REFERENCES projects(project_id)
    )""",
    """CREATE INDEX external_metrics_run_idx
        ON external_metrics(tool_run_id,category,metric_name)""",
    """CREATE INDEX external_metrics_subject_idx
        ON external_metrics(subject_kind,subject_key,metric_name,tool_run_id)""",
    """CREATE TABLE external_relations(
        relation_id INTEGER PRIMARY KEY,
        tool_run_id INTEGER NOT NULL,
        portable_relation_id TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN (
            'file','symbol','module','project','run','contract','scc'
        )),
        source_key TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK(target_kind IN (
            'file','symbol','module','project','run','contract','scc'
        )),
        target_key TEXT NOT NULL,
        directed INTEGER NOT NULL CHECK(directed IN (0,1)),
        confidence REAL CHECK(
            confidence IS NULL OR (confidence>=0.0 AND confidence<=1.0)
        ),
        source_version_id INTEGER,
        source_symbol_id INTEGER,
        source_project_id INTEGER,
        target_version_id INTEGER,
        target_symbol_id INTEGER,
        target_project_id INTEGER,
        metadata_json TEXT NOT NULL,
        UNIQUE(tool_run_id,portable_relation_id),
        FOREIGN KEY(tool_run_id) REFERENCES external_tool_runs(tool_run_id)
            ON DELETE CASCADE,
        FOREIGN KEY(source_version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(source_symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(source_project_id) REFERENCES projects(project_id),
        FOREIGN KEY(target_version_id) REFERENCES file_versions(version_id),
        FOREIGN KEY(target_symbol_id) REFERENCES symbols(symbol_id),
        FOREIGN KEY(target_project_id) REFERENCES projects(project_id)
    )""",
    """CREATE INDEX external_relations_run_idx
        ON external_relations(tool_run_id,relation_kind)""",
    """CREATE INDEX external_relations_source_idx
        ON external_relations(
            source_kind,source_key,relation_kind,target_kind,target_key,tool_run_id
        )""",
    """CREATE INDEX external_relations_target_idx
        ON external_relations(
            target_kind,target_key,relation_kind,source_kind,source_key,tool_run_id
        )""",
)


_V6_DDL = (
    """CREATE TABLE code_experiment_receipts(
        receipt_id TEXT PRIMARY KEY,
        analysis_run_id INTEGER NOT NULL,
        source_evaluation_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        subject_key TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        template_version TEXT NOT NULL,
        source_processing_signature TEXT NOT NULL,
        review_digest TEXT NOT NULL,
        envelope_digest TEXT NOT NULL CHECK(length(envelope_digest)>0),
        receipt_schema TEXT NOT NULL CHECK(
            receipt_schema='neocortex.code-experiment-receipt/v3'
        ),
        receipt_status TEXT NOT NULL CHECK(receipt_status IN (
            'passed','failed','abstained'
        )),
        payload_json TEXT NOT NULL,
        payload_xxh3_128 TEXT NOT NULL,
        payload_xxh3_64_guard TEXT NOT NULL,
        payload_bytes INTEGER NOT NULL CHECK(
            payload_bytes BETWEEN 1 AND 1048576
            AND payload_bytes=length(CAST(payload_json AS BLOB))
        ),
        recorded_ns INTEGER NOT NULL CHECK(recorded_ns>0),
        authority TEXT NOT NULL CHECK(authority='advisory'),
        mutation_authority INTEGER NOT NULL CHECK(mutation_authority=0),
        FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
            ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    """CREATE INDEX code_experiment_receipts_context_idx
        ON code_experiment_receipts(
            analysis_run_id,source_evaluation_id,recorded_ns DESC,receipt_id
        )""",
    """CREATE INDEX code_experiment_receipts_proposal_idx
        ON code_experiment_receipts(
            proposal_id,receipt_status,recorded_ns DESC,receipt_id
        )""",
    """CREATE TRIGGER code_experiment_receipts_no_update
        BEFORE UPDATE ON code_experiment_receipts
        BEGIN
            SELECT RAISE(ABORT,'Code experiment receipts are immutable');
        END""",
    """CREATE TRIGGER code_experiment_receipts_no_delete
        BEFORE DELETE ON code_experiment_receipts
        BEGIN
            SELECT RAISE(ABORT,'Code experiment receipts are immutable');
        END""",
)


_V7_DDL = (
    """CREATE TABLE code_experiment_receipts(
        receipt_id TEXT PRIMARY KEY,
        analysis_run_id INTEGER NOT NULL,
        source_evaluation_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        subject_key TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        template_version TEXT NOT NULL,
        source_processing_signature TEXT NOT NULL,
        review_digest TEXT NOT NULL,
        envelope_digest TEXT NOT NULL CHECK(length(envelope_digest)>0),
        receipt_schema TEXT NOT NULL CHECK(receipt_schema IN (
            'neocortex.code-experiment-receipt/v3',
            'neocortex.code-experiment-receipt/v4'
        )),
        receipt_status TEXT NOT NULL CHECK(receipt_status IN (
            'passed','failed','abstained'
        )),
        payload_json TEXT NOT NULL,
        payload_xxh3_128 TEXT NOT NULL,
        payload_xxh3_64_guard TEXT NOT NULL,
        payload_bytes INTEGER NOT NULL CHECK(
            payload_bytes BETWEEN 1 AND 1048576
            AND payload_bytes=length(CAST(payload_json AS BLOB))
        ),
        recorded_ns INTEGER NOT NULL CHECK(recorded_ns>0),
        authority TEXT NOT NULL CHECK(authority='advisory'),
        mutation_authority INTEGER NOT NULL CHECK(mutation_authority=0),
        FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(analysis_run_id)
            ON DELETE RESTRICT
    ) WITHOUT ROWID""",
    """CREATE INDEX code_experiment_receipts_context_idx
        ON code_experiment_receipts(
            analysis_run_id,source_evaluation_id,recorded_ns DESC,receipt_id
        )""",
    """CREATE INDEX code_experiment_receipts_proposal_idx
        ON code_experiment_receipts(
            proposal_id,receipt_status,recorded_ns DESC,receipt_id
        )""",
    """CREATE TRIGGER code_experiment_receipts_no_update
        BEFORE UPDATE ON code_experiment_receipts
        BEGIN
            SELECT RAISE(ABORT,'Code experiment receipts are immutable');
        END""",
    """CREATE TRIGGER code_experiment_receipts_no_delete
        BEFORE DELETE ON code_experiment_receipts
        BEGIN
            SELECT RAISE(ABORT,'Code experiment receipts are immutable');
        END""",
)


_CODE_EXPERIMENT_RECEIPT_COLUMNS = (
    "receipt_id",
    "analysis_run_id",
    "source_evaluation_id",
    "question_id",
    "subject_key",
    "proposal_id",
    "template_id",
    "template_version",
    "source_processing_signature",
    "review_digest",
    "envelope_digest",
    "receipt_schema",
    "receipt_status",
    "payload_json",
    "payload_xxh3_128",
    "payload_xxh3_64_guard",
    "payload_bytes",
    "recorded_ns",
    "authority",
    "mutation_authority",
)


# endregion [01]


# region [02] Connection and exact contract


_CODE_SQLITE_POLICY = SQLiteConnectionPolicy(
    label="code state",
    timeout_seconds=60.0,
    row_factory=sqlite3.Row,
    writer_pragmas=SQLiteWriterPragmas(
        journal_mode="WAL",
        synchronous="NORMAL",
        cache_size_kib=32_768,
        wal_autocheckpoint_pages=2_048,
        journal_size_limit_bytes=268_435_456,
    ),
)


def connect_code_state(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
) -> sqlite3.Connection:
    """Open code state, optionally refusing creation after initialization."""

    mode = READONLY_EXISTING if readonly else READWRITE_CREATE if create else READWRITE_EXISTING
    return connect_sqlite(
        path,
        mode=mode,
        policy=_CODE_SQLITE_POLICY,
    )


@contextmanager
def code_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = True,
):
    if readonly:
        with readonly_code_database(path) as connection:
            yield connection
        return
    connection = connect_code_state(path, readonly=readonly, create=create)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def readonly_code_database(
    path: Path,
    *,
    connect: Callable[..., sqlite3.Connection] = connect_code_state,
    close_connection: bool = True,
):
    """Read Code without recreating sidecars when its main file is quiescent.

    An already active WAL/SHM remains owned by SQLite and is read normally.
    No read path checkpoints, removes, or treats a racing writer as quiescent.
    """

    if not callable(connect):
        raise TypeError("Code read connector must be callable")
    if not isinstance(close_connection, bool):
        raise TypeError("close_connection must be a boolean")
    selected = Path(path)
    sidecars = (Path(f"{selected}-wal"), Path(f"{selected}-shm"))
    if (
        connect is connect_code_state
        and selected.is_file()
        and not any(os.path.lexists(sidecar) for sidecar in sidecars)
    ):
        from neocortex.workflow.self_analysis.self_analysis_status import quiescent_sqlite_database

        with quiescent_sqlite_database(selected, timeout_seconds=60) as connection:
            yield connection
        return
    connection = connect(selected, readonly=True, create=False)
    try:
        yield connection
    finally:
        if close_connection:
            connection.close()


def _execute(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        connection.execute(statement)


def _build_current_schema(connection: sqlite3.Connection) -> None:
    _execute(connection, _CURRENT_V1_DDL)
    _execute(connection, _V2_DDL)
    _execute(connection, _V3_DDL)
    _execute(connection, _V4_DDL)
    _execute(connection, _V7_DDL)


def _build_legacy_schema(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    if version not in {1, 2, 3, 4, 5, 6}:
        raise ValueError(f"unsupported legacy Code schema: {version}")
    _execute(connection, _CURRENT_V1_DDL if version >= 5 else _LEGACY_V1_DDL)
    if version >= 2:
        _execute(connection, _V2_DDL)
    if version >= 3:
        _execute(connection, _V3_DDL)
    if version >= 4:
        _execute(connection, _V4_DDL)
    if version >= 6:
        _execute(connection, _V6_DDL)


@lru_cache(maxsize=1)
def code_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_current_schema)


@lru_cache(maxsize=6)
def _legacy_code_schema_contract(version: int) -> SQLiteSchemaContract:
    return schema_contract_from_builder(
        lambda connection: _build_legacy_schema(connection, version)
    )


def validate_code_schema(connection: sqlite3.Connection) -> None:
    validate_sqlite_schema_contract(
        connection,
        code_schema_contract(),
        label="code",
        exact=True,
    )


def _validate_legacy_code_schema(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    validate_sqlite_schema_contract(
        connection,
        _legacy_code_schema_contract(version),
        label=f"code v{version} migration source",
        exact=True,
    )


# endregion [02]


# region [03] Creation, migration and validation


def _read_version(connection: sqlite3.Connection) -> int | None:
    objects = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if int(objects) == 0:
        return None
    metadata = connection.execute("SELECT type FROM sqlite_master WHERE name='metadata'").fetchone()
    if metadata is None or str(metadata[0]) != "table":
        raise RuntimeError("code database contains objects but no metadata table")
    rows = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("code metadata has no unique schema_version")
    try:
        version = int(rows[0][0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("code schema_version is malformed") from exc
    pragma_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if pragma_version not in {0, version}:
        raise RuntimeError("code metadata and PRAGMA user_version disagree")
    if not 1 <= version <= CODE_SCHEMA_VERSION:
        raise RuntimeError(
            f"code schema {version} is unsupported; expected 1..{CODE_SCHEMA_VERSION}"
        )
    return version


def _record_migration(
    connection: sqlite3.Connection,
    version: int,
    description: str,
    applied_ns: int,
) -> None:
    connection.execute(
        "INSERT INTO schema_migrations(version,description,applied_ns) VALUES(?,?,?)",
        (version, description, applied_ns),
    )
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )
    connection.execute(f"PRAGMA user_version={version}")


def _validate_code_storage_integrity(
    connection: sqlite3.Connection,
    *,
    label: str,
) -> None:
    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise RuntimeError(f"{label} has a foreign-key integrity violation")
    integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    if integrity != ("ok",):
        raise RuntimeError(f"{label} failed integrity_check: {integrity!r}")


def verify_code_storage_integrity(path: Path) -> None:
    """Run the intentionally expensive whole-owner integrity audit explicitly.

    ``PRAGMA integrity_check`` scales with every page retained by the Code
    owner.  It belongs at migration, backup and maintenance boundaries, not in
    the ordinary route-open path: running it before every incremental replay
    made a cache-only publication spend minutes rereading an unchanged
    multi-gigabyte database.  The normal initializer still validates the exact
    schema and migration history on every open; callers that need a physical
    full-store audit opt into this separate boundary.
    """

    selected = Path(path)
    if not selected.is_file():
        raise FileNotFoundError(selected)
    with code_database(selected, readonly=True) as connection:
        version = _read_version(connection)
        if version != CODE_SCHEMA_VERSION:
            raise RuntimeError(
                f"code storage integrity requires the current schema; observed {version!r}"
            )
        validate_code_schema(connection)
        _validate_migration_history(connection)
        _validate_code_storage_integrity(connection, label="code current state")


def _create_fresh(connection: sqlite3.Connection, applied_ns: int) -> None:
    _execute(connection, _CURRENT_V1_DDL)
    _record_migration(
        connection,
        1,
        "versioned file observations, symbols, relations, diagnostics and FTS",
        applied_ns,
    )
    _execute(connection, _V2_DDL)
    _record_migration(
        connection,
        2,
        "probable projects, reconstruction provenance and semantic links",
        applied_ns + 1,
    )
    _execute(connection, _V3_DDL)
    _record_migration(
        connection,
        3,
        "normalized multi-provider external code evidence",
        applied_ns + 2,
    )
    _execute(connection, _V4_DDL)
    _record_migration(
        connection,
        4,
        "portable external provider metrics and relations",
        applied_ns + 3,
    )
    _record_migration(
        connection,
        5,
        "platform-aware current filesystem path identity",
        applied_ns + 4,
    )
    _execute(connection, _V7_DDL)
    _record_migration(
        connection,
        6,
        "immutable post-publication Code experiment receipts",
        applied_ns + 5,
    )
    _record_migration(
        connection,
        7,
        "versioned Code experiment receipt compatibility",
        applied_ns + 6,
    )


def _migrate_one_to_two(connection: sqlite3.Connection, applied_ns: int) -> None:
    _validate_legacy_code_schema(connection, 1)
    _execute(connection, _V2_DDL)
    _record_migration(
        connection,
        2,
        "probable projects, reconstruction provenance and semantic links",
        applied_ns,
    )


def _migrate_two_to_three(connection: sqlite3.Connection, applied_ns: int) -> None:
    _validate_legacy_code_schema(connection, 2)
    _execute(connection, _V3_DDL)
    _record_migration(
        connection,
        3,
        "normalized multi-provider external code evidence",
        applied_ns,
    )


def _migrate_three_to_four(connection: sqlite3.Connection, applied_ns: int) -> None:
    _validate_legacy_code_schema(connection, 3)
    _execute(connection, _V4_DDL)
    _record_migration(
        connection,
        4,
        "portable external provider metrics and relations",
        applied_ns,
    )


_FILES_COLUMNS = (
    "file_id",
    "volume_id",
    "physical_file_id",
    "current_path",
    "current_version_id",
    "status",
    "first_seen_run_id",
    "last_seen_run_id",
)


def _migrate_four_to_five(connection: sqlite3.Connection, applied_ns: int) -> None:
    """Adopt host path equivalence without reinterpreting physical identity."""

    _validate_legacy_code_schema(connection, 4)
    if _PATH_COLLATION != "NOCASE":
        legacy_table = "__neocortex_code_v4_files"
        collision = connection.execute(
            "SELECT type FROM sqlite_master WHERE name=?",
            (legacy_table,),
        ).fetchone()
        if collision is not None:
            raise RuntimeError(f"reserved Code migration object exists: {legacy_table}")
        source_count = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        connection.execute("ALTER TABLE files RENAME TO " + legacy_table)
        connection.execute(_FILES_TABLE_DDL)
        column_sql = ",".join(_FILES_COLUMNS)
        inserted = connection.execute(
            f"INSERT INTO files({column_sql}) SELECT {column_sql} FROM {legacy_table}"
        )
        if inserted.rowcount != source_count:
            raise RuntimeError("Code v5 file row count changed during migration")
        missing = connection.execute(
            f"SELECT {column_sql} FROM {legacy_table} EXCEPT SELECT {column_sql} FROM files LIMIT 1"
        ).fetchone()
        extra = connection.execute(
            f"SELECT {column_sql} FROM files EXCEPT SELECT {column_sql} FROM {legacy_table} LIMIT 1"
        ).fetchone()
        if missing is not None or extra is not None:
            raise RuntimeError("Code v5 file evidence changed during migration")
        connection.execute(f"DROP TABLE {legacy_table}")
        connection.execute(_FILES_CURRENT_PATH_INDEX_DDL)
        connection.execute(_FILES_LAST_SEEN_INDEX_DDL)
    _record_migration(
        connection,
        5,
        "platform-aware current filesystem path identity",
        applied_ns,
    )


def _migrate_five_to_six(connection: sqlite3.Connection, applied_ns: int) -> None:
    """Add append-only experiment receipts without reinterpreting prior facts."""

    _validate_legacy_code_schema(connection, 5)
    _execute(connection, _V6_DDL)
    _record_migration(
        connection,
        6,
        "immutable post-publication Code experiment receipts",
        applied_ns,
    )


def _migrate_six_to_seven(connection: sqlite3.Connection, applied_ns: int) -> None:
    """Permit v4 receipts without reinterpreting any immutable v3 row."""

    _validate_legacy_code_schema(connection, 6)
    legacy_table = "__neocortex_code_v6_experiment_receipts"
    collision = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=?",
        (legacy_table,),
    ).fetchone()
    if collision is not None:
        raise RuntimeError(f"reserved Code migration object exists: {legacy_table}")

    source_count = int(
        connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone()[0]
    )
    connection.execute("DROP TRIGGER code_experiment_receipts_no_update")
    connection.execute("DROP TRIGGER code_experiment_receipts_no_delete")
    connection.execute("DROP INDEX code_experiment_receipts_context_idx")
    connection.execute("DROP INDEX code_experiment_receipts_proposal_idx")
    connection.execute("ALTER TABLE code_experiment_receipts RENAME TO " + legacy_table)
    connection.execute(_V7_DDL[0])

    column_sql = ",".join(_CODE_EXPERIMENT_RECEIPT_COLUMNS)
    inserted = connection.execute(
        f"INSERT INTO code_experiment_receipts({column_sql}) "
        f"SELECT {column_sql} FROM {legacy_table}"
    )
    destination_count = int(
        connection.execute("SELECT COUNT(*) FROM code_experiment_receipts").fetchone()[0]
    )
    if inserted.rowcount != source_count or destination_count != source_count:
        raise RuntimeError("Code v7 receipt row count changed during migration")
    missing = connection.execute(
        f"SELECT {column_sql} FROM {legacy_table} "
        f"EXCEPT SELECT {column_sql} FROM code_experiment_receipts LIMIT 1"
    ).fetchone()
    extra = connection.execute(
        f"SELECT {column_sql} FROM code_experiment_receipts "
        f"EXCEPT SELECT {column_sql} FROM {legacy_table} LIMIT 1"
    ).fetchone()
    if missing is not None or extra is not None:
        raise RuntimeError("Code v7 receipt evidence changed during migration")

    connection.execute(f"DROP TABLE {legacy_table}")
    _execute(connection, _V7_DDL[1:])
    _record_migration(
        connection,
        7,
        "versioned Code experiment receipt compatibility",
        applied_ns,
    )


def _validate_migration_history(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT version,description,applied_ns FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = tuple(int(row[0]) for row in rows)
    if versions != tuple(range(1, CODE_SCHEMA_VERSION + 1)):
        raise RuntimeError("code schema migration history is incomplete")
    if any(int(row[2]) <= 0 for row in rows):
        raise RuntimeError("code schema migration timestamps are invalid")


def initialize_code_state(path: Path) -> None:
    """Create or migrate code state atomically without replacing prior evidence.

    Current owners receive a bounded structural validation.  Full physical
    integrity remains mandatory before and after migrations and is available
    for explicit maintenance through :func:`verify_code_storage_integrity`.
    """

    prior: int | None = None
    if path.is_file():
        with code_database(path, readonly=True) as connection:
            prior = _read_version(connection)
            if prior == CODE_SCHEMA_VERSION:
                validate_code_schema(connection)
                _validate_migration_history(connection)
                return
            if prior is not None:
                _validate_legacy_code_schema(connection, prior)
                _validate_code_storage_integrity(
                    connection,
                    label=f"code v{prior} migration source",
                )

    connection = connect_code_state(path, create=True)
    rebuilds_path_identity = prior is not None and prior < 5 and _PATH_COLLATION != "NOCASE"
    try:
        if rebuilds_path_identity:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
                raise RuntimeError("Code migration could not suspend foreign-key enforcement")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = _read_version(connection)
            if current != prior:
                raise RuntimeError("code schema changed during initialization")
            applied_ns = time.time_ns()
            if current is None:
                _create_fresh(connection, applied_ns)
            elif current == 1:
                _migrate_one_to_two(connection, applied_ns)
                _migrate_two_to_three(connection, applied_ns + 1)
                _migrate_three_to_four(connection, applied_ns + 2)
                _migrate_four_to_five(connection, applied_ns + 3)
                _migrate_five_to_six(connection, applied_ns + 4)
                _migrate_six_to_seven(connection, applied_ns + 5)
            elif current == 2:
                _migrate_two_to_three(connection, applied_ns)
                _migrate_three_to_four(connection, applied_ns + 1)
                _migrate_four_to_five(connection, applied_ns + 2)
                _migrate_five_to_six(connection, applied_ns + 3)
                _migrate_six_to_seven(connection, applied_ns + 4)
            elif current == 3:
                _migrate_three_to_four(connection, applied_ns)
                _migrate_four_to_five(connection, applied_ns + 1)
                _migrate_five_to_six(connection, applied_ns + 2)
                _migrate_six_to_seven(connection, applied_ns + 3)
            elif current == 4:
                _migrate_four_to_five(connection, applied_ns)
                _migrate_five_to_six(connection, applied_ns + 1)
                _migrate_six_to_seven(connection, applied_ns + 2)
            elif current == 5:
                _migrate_five_to_six(connection, applied_ns)
                _migrate_six_to_seven(connection, applied_ns + 1)
            elif current == 6:
                _migrate_six_to_seven(connection, applied_ns)
            else:
                raise RuntimeError(f"unsupported code migration start: {current}")
            validate_code_schema(connection)
            _validate_migration_history(connection)
            _validate_code_storage_integrity(connection, label="code migrated state")
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        if rebuilds_path_identity:
            connection.execute("PRAGMA legacy_alter_table=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
        connection.close()


def checkpoint_code_wal(
    connection: sqlite3.Connection,
    *,
    error_type: type[RuntimeError] = RuntimeError,
) -> None:
    """Checkpoint a completed Code publication before quiescent diagnostics."""

    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None or len(row) != 3:
        raise error_type("Code WAL checkpoint returned no bounded result")
    busy, remaining, checkpointed = (int(value) for value in row)
    if busy or remaining != checkpointed:
        raise error_type("Code WAL checkpoint could not make publication quiescent")


def remove_checkpointed_code_sidecars(
    code_path: Path,
    *,
    error_type: type[RuntimeError] = RuntimeError,
    require_removal: bool = True,
) -> bool:
    """Remove only regular reconstructible sidecars after a verified empty WAL."""

    sidecars = (Path(f"{code_path}-wal"), Path(f"{code_path}-shm"))
    wal = sidecars[0]
    if os.path.lexists(wal):
        wal_metadata = os.lstat(wal)
        if not stat_module.S_ISREG(wal_metadata.st_mode) or wal.is_symlink():
            raise error_type("Code WAL sidecar is not a regular file")
        if wal_metadata.st_size != 0:
            raise error_type("Code WAL still contains frames after checkpoint")
    removed = True
    for sidecar in sidecars:
        if not os.path.lexists(sidecar):
            continue
        metadata = os.lstat(sidecar)
        if not stat_module.S_ISREG(metadata.st_mode) or sidecar.is_symlink():
            raise error_type("Code SQLite sidecar is not a regular file")
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        except PermissionError:
            if require_removal:
                raise
            removed = False
    return removed


# endregion [03]


__all__ = [
    "CODE_SCHEMA_VERSION",
    "checkpoint_code_wal",
    "code_database",
    "code_schema_contract",
    "connect_code_state",
    "initialize_code_state",
    "readonly_code_database",
    "remove_checkpointed_code_sidecars",
    "validate_code_schema",
    "verify_code_storage_integrity",
]
