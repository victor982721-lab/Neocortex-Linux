"""Transactional schema management for the framework orchestration database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from .sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    capture_sqlite_schema_contract,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


SCHEMA_VERSION = 21


class _FrameworkSchemaMigrationError(RuntimeError):
    """A legacy schema cannot be transformed without risking persisted data."""


# region [01] Canonical schema


_REVIEW_EVIDENCE_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_evidence_examples (
    decision_id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    route_name TEXT NOT NULL,
    volume_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    path TEXT NOT NULL COLLATE NOCASE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    birthtime_ns INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    candidate_generation INTEGER NOT NULL CHECK(candidate_generation>=0),
    source_status TEXT,
    target_recommendation TEXT CHECK(
        target_recommendation IS NULL OR target_recommendation IN (
            'retry','keep_protected','manual_review','deletion_candidate'
        )
    ),
    retryable INTEGER CHECK(retryable IS NULL OR retryable IN (0,1)),
    confidence REAL CHECK(
        confidence IS NULL OR (confidence>=0.0 AND confidence<=1.0)
    ),
    evidence_json TEXT,
    detector_version TEXT,
    decision_status TEXT NOT NULL CHECK(decision_status IN (
        'confirmed','dismissed','deferred'
    )),
    actor TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    note TEXT,
    decided_ns INTEGER NOT NULL,
    recorded_ns INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'accepted','rejected','abstained'
    )),
    candidate_evidence_complete INTEGER NOT NULL CHECK(
        candidate_evidence_complete IN (0,1)
    ),
    evidence_schema_version INTEGER NOT NULL CHECK(evidence_schema_version>=1),
    materialized_ns INTEGER NOT NULL,
    CHECK(
        (decision_status='confirmed' AND outcome='accepted') OR
        (decision_status='dismissed' AND outcome='rejected') OR
        (decision_status='deferred' AND outcome='abstained')
    ),
    CHECK(
        (candidate_evidence_complete=0 AND source_status IS NULL AND
         target_recommendation IS NULL AND retryable IS NULL AND
         confidence IS NULL AND evidence_json IS NULL AND
         detector_version IS NULL) OR
        (candidate_evidence_complete=1 AND source_status IS NOT NULL AND
         target_recommendation IS NOT NULL AND retryable IS NOT NULL AND
         confidence IS NOT NULL AND evidence_json IS NOT NULL AND
         detector_version IS NOT NULL)
    )
)
"""

_REVIEW_EVIDENCE_PROGRESS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_evidence_progress (
    pipeline_key TEXT PRIMARY KEY,
    last_scanned_decision_id INTEGER NOT NULL CHECK(last_scanned_decision_id>=0),
    updated_ns INTEGER NOT NULL
) WITHOUT ROWID
"""

_REVIEW_TASK_BATCHES_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_task_batches (
    batch_id TEXT PRIMARY KEY CHECK(length(batch_id) BETWEEN 1 AND 256),
    batch_key TEXT NOT NULL UNIQUE CHECK(length(batch_key) BETWEEN 1 AND 256),
    scope TEXT NOT NULL CHECK(length(trim(scope)) BETWEEN 1 AND 128),
    task_type TEXT NOT NULL CHECK(length(trim(task_type)) BETWEEN 1 AND 128),
    selector_signature TEXT NOT NULL CHECK(
        length(trim(selector_signature)) BETWEEN 1 AND 512
    ),
    source_snapshot_fingerprint TEXT NOT NULL CHECK(
        length(source_snapshot_fingerprint)=102 AND
        substr(source_snapshot_fingerprint,1,38)=
            'review-task-source-snapshot-v1:sha256:' AND
        substr(source_snapshot_fingerprint,39) NOT GLOB '*[^0-9a-f]*'
    ),
    source_snapshot_json TEXT NOT NULL CHECK(
        json_valid(source_snapshot_json) AND
        json_type(source_snapshot_json)='object' AND
        length(CAST(source_snapshot_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    cursor_before_json TEXT CHECK(
        cursor_before_json IS NULL OR (
            json_valid(cursor_before_json) AND
            json_type(cursor_before_json)='object' AND
            length(CAST(cursor_before_json AS BLOB)) BETWEEN 1 AND 65536
        )
    ),
    cursor_after_json TEXT CHECK(
        cursor_after_json IS NULL OR (
            json_valid(cursor_after_json) AND
            json_type(cursor_after_json)='object' AND
            length(CAST(cursor_after_json AS BLOB)) BETWEEN 1 AND 65536
        )
    ),
    previous_batch_id TEXT CHECK(
        previous_batch_id IS NULL OR length(previous_batch_id) BETWEEN 1 AND 256
    ),
    scan_revision INTEGER NOT NULL CHECK(scan_revision>=1),
    page_size INTEGER NOT NULL CHECK(page_size BETWEEN 0 AND 1000),
    scanned_count INTEGER NOT NULL CHECK(
        scanned_count=page_size AND scanned_count BETWEEN 0 AND 1000
    ),
    selected_count INTEGER NOT NULL CHECK(
        selected_count BETWEEN 0 AND 100 AND selected_count<=scanned_count
    ),
    cumulative_scanned_count INTEGER NOT NULL CHECK(
        cumulative_scanned_count>=scanned_count
    ),
    cumulative_selected_count INTEGER NOT NULL CHECK(
        cumulative_selected_count>=selected_count AND
        cumulative_selected_count<=cumulative_scanned_count
    ),
    coverage TEXT NOT NULL CHECK(coverage IN ('partial','complete')),
    evidence_complete INTEGER NOT NULL CHECK(evidence_complete IN (0,1)),
    evidence_reason TEXT CHECK(
        evidence_reason IS NULL OR
        length(trim(evidence_reason)) BETWEEN 1 AND 512
    ),
    cumulative_evidence_complete INTEGER NOT NULL CHECK(
        cumulative_evidence_complete IN (0,1)
    ),
    cumulative_evidence_reason TEXT CHECK(
        cumulative_evidence_reason IS NULL OR
        length(trim(cumulative_evidence_reason)) BETWEEN 1 AND 512
    ),
    producer_signature TEXT NOT NULL CHECK(
        length(trim(producer_signature)) BETWEEN 1 AND 512
    ),
    receipt_json TEXT NOT NULL CHECK(
        json_valid(receipt_json) AND json_type(receipt_json)='object' AND
        length(CAST(receipt_json AS BLOB)) BETWEEN 1 AND 8388608
    ),
    confirmed_ns INTEGER NOT NULL CHECK(confirmed_ns>=0),
    receipt_schema_version INTEGER NOT NULL CHECK(receipt_schema_version=1),
    UNIQUE(batch_id,scope,task_type,source_snapshot_fingerprint),
    UNIQUE(
        batch_id,scope,task_type,selector_signature,source_snapshot_fingerprint
    ),
    UNIQUE(
        scope,task_type,selector_signature,source_snapshot_fingerprint,
        scan_revision
    ),
    CHECK(cursor_after_json IS NOT NULL OR coverage='complete'),
    CHECK(
        (scan_revision=1 AND previous_batch_id IS NULL) OR
        (scan_revision>1 AND previous_batch_id IS NOT NULL)
    ),
    CHECK(
        (evidence_complete=1 AND evidence_reason IS NULL) OR
        (evidence_complete=0 AND evidence_reason IS NOT NULL)
    ),
    CHECK(
        (cumulative_evidence_complete=1 AND
         cumulative_evidence_reason IS NULL) OR
        (cumulative_evidence_complete=0 AND
         cumulative_evidence_reason IS NOT NULL)
    ),
    FOREIGN KEY(
        previous_batch_id,scope,task_type,selector_signature,
        source_snapshot_fingerprint
    ) REFERENCES review_task_batches(
        batch_id,scope,task_type,selector_signature,source_snapshot_fingerprint
    ) ON DELETE RESTRICT
) WITHOUT ROWID
"""

_REVIEW_TASKS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY CHECK(length(task_id) BETWEEN 1 AND 256),
    logical_key TEXT NOT NULL CHECK(length(logical_key) BETWEEN 1 AND 512),
    task_version INTEGER NOT NULL CHECK(task_version>=1),
    task_type TEXT NOT NULL CHECK(length(trim(task_type)) BETWEEN 1 AND 128),
    scope TEXT NOT NULL CHECK(length(trim(scope)) BETWEEN 1 AND 128),
    source_kind TEXT NOT NULL CHECK(length(trim(source_kind)) BETWEEN 1 AND 128),
    source_input_id TEXT NOT NULL CHECK(length(source_input_id) BETWEEN 1 AND 512),
    source_ref_json TEXT NOT NULL CHECK(
        json_valid(source_ref_json) AND json_type(source_ref_json)='object' AND
        length(CAST(source_ref_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    source_snapshot_fingerprint TEXT NOT NULL CHECK(
        length(source_snapshot_fingerprint)=102 AND
        substr(source_snapshot_fingerprint,1,38)=
            'review-task-source-snapshot-v1:sha256:' AND
        substr(source_snapshot_fingerprint,39) NOT GLOB '*[^0-9a-f]*'
    ),
    snapshot_json TEXT NOT NULL CHECK(
        json_valid(snapshot_json) AND json_type(snapshot_json)='object' AND
        length(CAST(snapshot_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    evidence_json TEXT NOT NULL CHECK(
        json_valid(evidence_json) AND json_type(evidence_json)='array' AND
        length(CAST(evidence_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) BETWEEN 1 AND 128),
    uncertainty_json TEXT NOT NULL CHECK(
        json_valid(uncertainty_json) AND
        json_type(uncertainty_json)='object' AND
        length(CAST(uncertainty_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    impact REAL NOT NULL CHECK(impact>=0.0 AND impact<=1.0),
    uncertainty REAL NOT NULL CHECK(uncertainty>=0.0 AND uncertainty<=1.0),
    irreversibility REAL NOT NULL CHECK(
        irreversibility>=0.0 AND irreversibility<=1.0
    ),
    priority REAL NOT NULL CHECK(
        priority>=0.0 AND priority<=1.0 AND
        abs(priority-(impact*uncertainty*irreversibility))<=0.000000000001
    ),
    priority_algorithm TEXT NOT NULL CHECK(
        priority_algorithm='impact-x-uncertainty-x-irreversibility-v1'
    ),
    suggestions_json TEXT NOT NULL CHECK(
        json_valid(suggestions_json) AND json_type(suggestions_json)='array' AND
        length(CAST(suggestions_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    batch_id TEXT NOT NULL CHECK(length(batch_id) BETWEEN 1 AND 256),
    supersedes_task_id TEXT CHECK(
        supersedes_task_id IS NULL OR
        length(supersedes_task_id) BETWEEN 1 AND 256
    ),
    supersession_event_id TEXT CHECK(
        supersession_event_id IS NULL OR
        length(supersession_event_id) BETWEEN 1 AND 256
    ),
    created_ns INTEGER NOT NULL CHECK(created_ns>=0),
    UNIQUE(logical_key,task_version),
    UNIQUE(batch_id,source_input_id),
    FOREIGN KEY(batch_id,scope,task_type,source_snapshot_fingerprint)
    REFERENCES review_task_batches(
        batch_id,scope,task_type,source_snapshot_fingerprint
    ) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_task_id)
    REFERENCES review_tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY(supersession_event_id,supersedes_task_id)
    REFERENCES review_task_events(event_id,task_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (task_version=1 AND supersedes_task_id IS NULL AND
         supersession_event_id IS NULL) OR
        (task_version>1 AND supersedes_task_id IS NOT NULL AND
         supersession_event_id IS NOT NULL)
    )
) WITHOUT ROWID
"""

_REVIEW_TASK_BATCH_MEMBERSHIPS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_task_batch_memberships (
    membership_id TEXT PRIMARY KEY CHECK(length(membership_id) BETWEEN 1 AND 256),
    batch_id TEXT NOT NULL CHECK(length(batch_id) BETWEEN 1 AND 256),
    task_id TEXT NOT NULL UNIQUE CHECK(length(task_id) BETWEEN 1 AND 256),
    source_input_id TEXT NOT NULL CHECK(length(source_input_id) BETWEEN 1 AND 512),
    recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=0),
    membership_schema_version INTEGER NOT NULL CHECK(membership_schema_version=1),
    UNIQUE(batch_id,source_input_id),
    FOREIGN KEY(batch_id) REFERENCES review_task_batches(batch_id) ON DELETE RESTRICT,
    FOREIGN KEY(task_id) REFERENCES review_tasks(task_id) ON DELETE RESTRICT
) WITHOUT ROWID
"""

_REVIEW_TASK_EVENTS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_task_events (
    event_id TEXT PRIMARY KEY CHECK(length(event_id) BETWEEN 1 AND 256),
    event_key TEXT NOT NULL UNIQUE CHECK(length(event_key) BETWEEN 1 AND 256),
    task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 256),
    sequence INTEGER NOT NULL CHECK(sequence>=1),
    previous_event_id TEXT CHECK(
        previous_event_id IS NULL OR
        length(previous_event_id) BETWEEN 1 AND 256
    ),
    from_state TEXT CHECK(
        from_state IS NULL OR from_state IN (
            'open','in_review','resolved','dismissed','superseded'
        )
    ),
    to_state TEXT NOT NULL CHECK(to_state IN (
        'open','in_review','resolved','dismissed','superseded'
    )),
    actor_kind TEXT NOT NULL CHECK(actor_kind IN ('system','human')),
    actor_id TEXT NOT NULL CHECK(length(trim(actor_id)) BETWEEN 1 AND 256),
    provenance_json TEXT NOT NULL CHECK(
        json_valid(provenance_json) AND
        json_type(provenance_json)='object' AND
        length(CAST(provenance_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    decision_json TEXT CHECK(
        decision_json IS NULL OR (
            json_valid(decision_json) AND
            json_type(decision_json)='object' AND
            length(CAST(decision_json AS BLOB)) BETWEEN 1 AND 65536
        )
    ),
    note TEXT CHECK(
        note IS NULL OR (
            length(CAST(note AS BLOB)) BETWEEN 1 AND 8192 AND
            length(trim(note))>0 AND note=trim(note)
        )
    ),
    observed_ns INTEGER NOT NULL CHECK(observed_ns>=0),
    recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=observed_ns),
    event_schema_version INTEGER NOT NULL CHECK(event_schema_version=1),
    UNIQUE(event_id,task_id),
    UNIQUE(task_id,sequence),
    CHECK(
        (sequence=1 AND previous_event_id IS NULL AND from_state IS NULL) OR
        (sequence>1 AND previous_event_id IS NOT NULL AND from_state IS NOT NULL)
    ),
    CHECK(
        (to_state IN ('resolved','dismissed') AND
         actor_kind='human' AND decision_json IS NOT NULL) OR
        (to_state IN ('open','in_review') AND decision_json IS NULL) OR
        to_state='superseded'
    ),
    FOREIGN KEY(task_id) REFERENCES review_tasks(task_id) ON DELETE RESTRICT,
    FOREIGN KEY(previous_event_id,task_id)
    REFERENCES review_task_events(event_id,task_id) ON DELETE RESTRICT
) WITHOUT ROWID
"""

_REVIEW_TASK_SCAN_PROGRESS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_task_scan_progress (
    progress_id TEXT PRIMARY KEY CHECK(length(progress_id) BETWEEN 1 AND 256),
    scope TEXT NOT NULL CHECK(length(trim(scope)) BETWEEN 1 AND 128),
    task_type TEXT NOT NULL CHECK(length(trim(task_type)) BETWEEN 1 AND 128),
    selector_signature TEXT NOT NULL CHECK(
        length(trim(selector_signature)) BETWEEN 1 AND 512
    ),
    source_snapshot_fingerprint TEXT NOT NULL CHECK(
        length(source_snapshot_fingerprint)=102 AND
        substr(source_snapshot_fingerprint,1,38)=
            'review-task-source-snapshot-v1:sha256:' AND
        substr(source_snapshot_fingerprint,39) NOT GLOB '*[^0-9a-f]*'
    ),
    source_snapshot_json TEXT NOT NULL CHECK(
        json_valid(source_snapshot_json) AND
        json_type(source_snapshot_json)='object' AND
        length(CAST(source_snapshot_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    cursor_json TEXT CHECK(
        cursor_json IS NULL OR (
            json_valid(cursor_json) AND json_type(cursor_json)='object' AND
            length(CAST(cursor_json AS BLOB)) BETWEEN 1 AND 65536
        )
    ),
    last_batch_id TEXT NOT NULL CHECK(length(last_batch_id) BETWEEN 1 AND 256),
    scanned_count INTEGER NOT NULL CHECK(scanned_count>=0),
    selected_count INTEGER NOT NULL CHECK(
        selected_count>=0 AND selected_count<=scanned_count
    ),
    complete INTEGER NOT NULL CHECK(complete IN (0,1)),
    evidence_complete INTEGER NOT NULL CHECK(evidence_complete IN (0,1)),
    evidence_reason TEXT CHECK(
        evidence_reason IS NULL OR
        length(trim(evidence_reason)) BETWEEN 1 AND 512
    ),
    revision INTEGER NOT NULL CHECK(revision>=1),
    created_ns INTEGER NOT NULL CHECK(created_ns>=0),
    updated_ns INTEGER NOT NULL CHECK(updated_ns>=created_ns),
    UNIQUE(scope,task_type,selector_signature,source_snapshot_fingerprint),
    CHECK(cursor_json IS NOT NULL OR complete=1),
    CHECK(
        (evidence_complete=1 AND evidence_reason IS NULL) OR
        (evidence_complete=0 AND evidence_reason IS NOT NULL)
    ),
    FOREIGN KEY(
        last_batch_id,scope,task_type,selector_signature,
        source_snapshot_fingerprint
    ) REFERENCES review_task_batches(
        batch_id,scope,task_type,selector_signature,source_snapshot_fingerprint
    ) ON DELETE RESTRICT
) WITHOUT ROWID
"""

_REVIEW_TASK_SOURCE_PUBLICATIONS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS review_task_source_publications (
    publication_id TEXT PRIMARY KEY CHECK(length(publication_id) BETWEEN 1 AND 256),
    publication_key TEXT NOT NULL UNIQUE CHECK(
        length(publication_key) BETWEEN 1 AND 256
    ),
    scope TEXT NOT NULL CHECK(length(trim(scope)) BETWEEN 1 AND 128),
    task_type TEXT NOT NULL CHECK(length(trim(task_type)) BETWEEN 1 AND 128),
    selector_signature TEXT NOT NULL CHECK(
        length(trim(selector_signature)) BETWEEN 1 AND 512
    ),
    source_snapshot_fingerprint TEXT NOT NULL CHECK(
        length(source_snapshot_fingerprint)=102 AND
        substr(source_snapshot_fingerprint,1,38)=
            'review-task-source-snapshot-v1:sha256:' AND
        substr(source_snapshot_fingerprint,39) NOT GLOB '*[^0-9a-f]*'
    ),
    source_snapshot_json TEXT NOT NULL CHECK(
        json_valid(source_snapshot_json) AND
        json_type(source_snapshot_json)='object' AND
        length(CAST(source_snapshot_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    batch_id TEXT NOT NULL UNIQUE CHECK(length(batch_id) BETWEEN 1 AND 256),
    previous_publication_id TEXT CHECK(
        previous_publication_id IS NULL OR
        length(previous_publication_id) BETWEEN 1 AND 256
    ),
    revision INTEGER NOT NULL CHECK(revision>=1),
    confirmed_ns INTEGER NOT NULL CHECK(confirmed_ns>=0),
    receipt_json TEXT NOT NULL CHECK(
        json_valid(receipt_json) AND json_type(receipt_json)='object' AND
        length(CAST(receipt_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    receipt_schema_version INTEGER NOT NULL CHECK(receipt_schema_version=1),
    UNIQUE(scope,task_type,selector_signature,revision),
    UNIQUE(publication_id,scope,task_type,selector_signature,revision),
    CHECK(
        (revision=1 AND previous_publication_id IS NULL) OR
        (revision>1 AND previous_publication_id IS NOT NULL)
    ),
    FOREIGN KEY(
        batch_id,scope,task_type,selector_signature,
        source_snapshot_fingerprint
    ) REFERENCES review_task_batches(
        batch_id,scope,task_type,selector_signature,source_snapshot_fingerprint
    ) ON DELETE RESTRICT,
    FOREIGN KEY(previous_publication_id)
    REFERENCES review_task_source_publications(publication_id) ON DELETE RESTRICT
) WITHOUT ROWID
"""


_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS initial_runs (
        run_id INTEGER PRIMARY KEY,
        root TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        status TEXT NOT NULL,
        run_kind TEXT NOT NULL DEFAULT 'initial',
        source_run_id INTEGER,
        current_phase TEXT,
        owner_pid INTEGER,
        heartbeat_ns INTEGER,
        scan_id INTEGER,
        journal_volume TEXT,
        journal_id TEXT,
        start_usn INTEGER,
        end_usn INTEGER,
        reconciliation_records INTEGER,
        inventory_attempts INTEGER,
        inventory_mode TEXT,
        corpus_access_mode TEXT NOT NULL DEFAULT 'normal' CHECK(
            corpus_access_mode IN ('normal','analyze_only')
        ),
        root_device_id_hex TEXT,
        root_file_id_hex TEXT,
        root_birthtime_ns INTEGER,
        state_directory TEXT,
        inventory_policy_signature TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        event_id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        occurred_ns INTEGER NOT NULL,
        level TEXT NOT NULL,
        phase TEXT NOT NULL,
        message TEXT NOT NULL,
        details_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_runs (
        run_id INTEGER NOT NULL,
        route_name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        current_phase TEXT,
        heartbeat_ns INTEGER,
        source_run_id INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT,
        PRIMARY KEY(run_id, route_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS route_phase_runs (
        run_id INTEGER NOT NULL,
        route_name TEXT NOT NULL,
        phase_name TEXT NOT NULL,
        status TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        heartbeat_ns INTEGER,
        source_run_id INTEGER,
        summary_json TEXT,
        error_type TEXT,
        error_message TEXT,
        PRIMARY KEY(run_id, route_name, phase_name)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS run_actions (
        run_id INTEGER PRIMARY KEY,
        apply_actions INTEGER NOT NULL,
        duplicate_candidates INTEGER NOT NULL,
        duplicates_trashed INTEGER NOT NULL,
        duplicate_skips INTEGER NOT NULL,
        files_checked INTEGER NOT NULL,
        types_detected INTEGER NOT NULL,
        extensions_matching INTEGER NOT NULL,
        unknown_types INTEGER NOT NULL,
        type_cache_hits INTEGER NOT NULL DEFAULT 0,
        type_cache_misses INTEGER NOT NULL DEFAULT 0,
        type_cache_pruned INTEGER NOT NULL DEFAULT 0,
        stale_inventory INTEGER NOT NULL DEFAULT 0,
        rename_candidates INTEGER NOT NULL,
        files_renamed INTEGER NOT NULL,
        rename_skips INTEGER NOT NULL,
        empty_directory_candidates INTEGER NOT NULL,
        empty_directories_trashed INTEGER NOT NULL,
        empty_directory_skips INTEGER NOT NULL,
        errors INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_actions (
        action_id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL,
        action_type TEXT NOT NULL,
        source_path TEXT NOT NULL,
        target_path TEXT,
        detected_mime TEXT,
        evidence TEXT,
        apply_requested INTEGER NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        started_ns INTEGER NOT NULL,
        completed_ns INTEGER,
        idempotency_key TEXT,
        expected_identity_json TEXT,
        effect_receipt_json TEXT,
        applying_ns INTEGER,
        corpus_access_mode TEXT NOT NULL DEFAULT 'normal' CHECK(
            corpus_access_mode IN ('normal','analyze_only')
        ),
        protected_root TEXT,
        protected_root_device_id_hex TEXT,
        protected_root_file_id_hex TEXT,
        protected_root_birthtime_ns INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_action_events (
        event_id INTEGER PRIMARY KEY,
        action_id INTEGER NOT NULL,
        occurred_ns INTEGER NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        stage TEXT NOT NULL,
        detail TEXT,
        evidence_json TEXT,
        FOREIGN KEY(action_id) REFERENCES file_actions(action_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_action_reconciliation_events (
        reconciliation_event_id INTEGER PRIMARY KEY,
        action_id INTEGER NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence>=1),
        previous_event_id INTEGER,
        reconciliation_key TEXT NOT NULL UNIQUE,
        observed_ns INTEGER NOT NULL CHECK(observed_ns>=0),
        recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=0),
        action_status TEXT NOT NULL CHECK(action_status IN (
            'applying','recovery_required'
        )),
        reconciler_signature TEXT NOT NULL CHECK(length(reconciler_signature)>0),
        event_schema_version INTEGER NOT NULL CHECK(event_schema_version=1),
        actor TEXT NOT NULL CHECK(length(trim(actor))>0),
        provenance_json TEXT NOT NULL,
        classification TEXT NOT NULL CHECK(classification IN (
            'confirmed','not_performed','ambiguous','impossible_to_check'
        )),
        recommendation TEXT NOT NULL,
        detail TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        UNIQUE(reconciliation_event_id, action_id),
        UNIQUE(action_id, sequence),
        CHECK(
            (sequence=1 AND previous_event_id IS NULL) OR
            (sequence>1 AND previous_event_id IS NOT NULL)
        ),
        CHECK(
            (classification='confirmed' AND
             recommendation='confirm_action_record') OR
            (classification='not_performed' AND
             recommendation='review_before_new_authorized_attempt') OR
            (classification IN ('ambiguous','impossible_to_check') AND
             recommendation='preserve_evidence_and_review_manually')
        ),
        FOREIGN KEY(action_id) REFERENCES file_actions(action_id) ON DELETE RESTRICT,
        FOREIGN KEY(previous_event_id, action_id)
        REFERENCES file_action_reconciliation_events(
            reconciliation_event_id, action_id
        ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_candidates (
        run_id INTEGER NOT NULL,
        mime TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        PRIMARY KEY(run_id, path)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS content_type_cache (
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL DEFAULT -1,
        detector_version TEXT NOT NULL,
        status TEXT NOT NULL,
        mime TEXT,
        canonical_extension TEXT,
        accepted_extensions_json TEXT,
        evidence TEXT,
        last_seen_run_id INTEGER NOT NULL DEFAULT 0,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(volume_id, file_id, detector_version)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS review_candidates (
        route_name TEXT NOT NULL,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT NOT NULL,
        recommendation TEXT NOT NULL CHECK(recommendation IN (
            'retry','keep_protected','manual_review','deletion_candidate'
        )),
        retryable INTEGER NOT NULL CHECK(retryable IN (0,1)),
        confidence REAL NOT NULL CHECK(confidence>=0.0 AND confidence<=1.0),
        evidence_json TEXT NOT NULL,
        detector_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','resolved')),
        first_detected_ns INTEGER NOT NULL,
        last_detected_ns INTEGER NOT NULL,
        last_seen_run_id INTEGER NOT NULL,
        resolved_ns INTEGER,
        resolved_run_id INTEGER,
        resolution_note TEXT,
        PRIMARY KEY(route_name,volume_id,file_id,reason_code)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS review_decisions (
        decision_id INTEGER PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        route_name TEXT NOT NULL,
        volume_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        candidate_generation INTEGER NOT NULL CHECK(candidate_generation>=0),
        path TEXT NOT NULL COLLATE NOCASE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER NOT NULL,
        source_status TEXT,
        recommendation TEXT CHECK(
            recommendation IS NULL OR recommendation IN (
                'retry','keep_protected','manual_review','deletion_candidate'
            )
        ),
        retryable INTEGER CHECK(retryable IS NULL OR retryable IN (0,1)),
        confidence REAL CHECK(
            confidence IS NULL OR (confidence>=0.0 AND confidence<=1.0)
        ),
        evidence_json TEXT,
        detector_version TEXT,
        status TEXT NOT NULL CHECK(status IN (
            'confirmed','dismissed','deferred'
        )),
        actor TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        note TEXT,
        decided_ns INTEGER NOT NULL,
        recorded_ns INTEGER NOT NULL
    )
    """,
    _REVIEW_EVIDENCE_TABLE_STATEMENT,
    _REVIEW_EVIDENCE_PROGRESS_TABLE_STATEMENT,
    _REVIEW_TASK_BATCHES_TABLE_STATEMENT,
    _REVIEW_TASKS_TABLE_STATEMENT,
    _REVIEW_TASK_BATCH_MEMBERSHIPS_TABLE_STATEMENT,
    _REVIEW_TASK_EVENTS_TABLE_STATEMENT,
    _REVIEW_TASK_SCAN_PROGRESS_TABLE_STATEMENT,
    _REVIEW_TASK_SOURCE_PUBLICATIONS_TABLE_STATEMENT,
)

_INDEX_STATEMENTS = (
    """
    CREATE INDEX IF NOT EXISTS run_events_run_idx
        ON run_events(run_id, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_runs_status_idx
        ON route_runs(status, run_id, route_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_phase_status_idx
        ON route_phase_runs(status, run_id, route_name, phase_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_actions_run_idx
        ON file_actions(run_id, action_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS file_actions_idempotency_key_idx
        ON file_actions(idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_actions_recovery_idx
        ON file_actions(status, action_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_action_events_action_idx
        ON file_action_events(action_id, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_action_reconciliation_events_action_idx
        ON file_action_reconciliation_events(action_id, reconciliation_event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS route_candidates_mime_idx
        ON route_candidates(run_id, mime, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS review_candidates_status_idx
        ON review_candidates(status, recommendation, route_name, path)
    """,
    """
    CREATE INDEX IF NOT EXISTS review_candidates_path_idx
        ON review_candidates(path, route_name, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS review_decisions_identity_idx
        ON review_decisions(
            route_name, volume_id, file_id, reason_code,
            candidate_generation, decision_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_decisions_status_idx
        ON review_decisions(status, recorded_ns, decision_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS review_evidence_outcome_idx
        ON review_evidence_examples(
            decision_status, route_name, reason_code, decision_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_evidence_target_idx
        ON review_evidence_examples(
            target_recommendation, detector_version, decision_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_tasks_queue_idx
        ON review_tasks(
            scope, task_type, priority DESC, created_ns, task_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_tasks_source_idx
        ON review_tasks(
            source_snapshot_fingerprint, source_input_id, task_type, task_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_task_events_current_idx
        ON review_task_events(task_id, sequence DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS review_task_batches_scan_idx
        ON review_task_batches(
            scope, task_type, selector_signature,
            source_snapshot_fingerprint, confirmed_ns, batch_id
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS review_task_source_publications_head_idx
        ON review_task_source_publications(
            scope, task_type, selector_signature, revision DESC, publication_id
        )
    """,
)

_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS initial_runs_corpus_policy_no_update
    BEFORE UPDATE OF root,run_kind,corpus_access_mode,root_device_id_hex,
    root_file_id_hex,root_birthtime_ns,state_directory,inventory_policy_signature
    ON initial_runs
    BEGIN
        SELECT RAISE(ABORT, 'initial run corpus policy is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_actions_corpus_policy_insert
    BEFORE INSERT ON file_actions
    WHEN NOT EXISTS(
        SELECT 1 FROM initial_runs AS run
        WHERE run.run_id=NEW.run_id
          AND run.corpus_access_mode=NEW.corpus_access_mode
          AND (
            (NEW.corpus_access_mode='normal'
             AND NEW.protected_root IS NULL
             AND NEW.protected_root_device_id_hex IS NULL
             AND NEW.protected_root_file_id_hex IS NULL
             AND NEW.protected_root_birthtime_ns IS NULL)
            OR
            (NEW.corpus_access_mode='analyze_only'
             AND NEW.protected_root=run.root COLLATE NOCASE
             AND NEW.protected_root_device_id_hex=run.root_device_id_hex
             AND NEW.protected_root_file_id_hex=run.root_file_id_hex
             AND NEW.protected_root_birthtime_ns=run.root_birthtime_ns)
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'file action corpus policy mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_actions_corpus_policy_no_update
    BEFORE UPDATE OF corpus_access_mode,protected_root,
    protected_root_device_id_hex,protected_root_file_id_hex,
    protected_root_birthtime_ns ON file_actions
    BEGIN
        SELECT RAISE(ABORT, 'file action corpus policy is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_events_no_update
    BEFORE UPDATE ON file_action_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_events_no_delete
    BEFORE DELETE ON file_action_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_reconciliation_events_no_update
    BEFORE UPDATE ON file_action_reconciliation_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_reconciliation_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS file_action_reconciliation_events_no_delete
    BEFORE DELETE ON file_action_reconciliation_events
    BEGIN
        SELECT RAISE(ABORT, 'file_action_reconciliation_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_tasks_validate_insert
    BEFORE INSERT ON review_tasks
    WHEN NOT (
        EXISTS(
            SELECT 1 FROM review_task_batches AS owner_batch,
            json_each(owner_batch.receipt_json,'$.tasks') AS receipt_task,
            json_each(owner_batch.receipt_json,'$.inputs') AS receipt_input
            WHERE owner_batch.batch_id=NEW.batch_id
              AND json_extract(receipt_task.value,'$.task_id')=NEW.task_id
              AND json_extract(receipt_task.value,'$.source_input_id')=
                  NEW.source_input_id
              AND json_extract(receipt_input.value,'$.input_id')=
                  NEW.source_input_id
        ) AND
        (
            (
                NEW.task_version=1 AND NEW.supersedes_task_id IS NULL AND
                NOT EXISTS(
                    SELECT 1 FROM review_tasks AS existing
                    WHERE existing.logical_key=NEW.logical_key
                )
            ) OR
            (
                NEW.task_version>1 AND NEW.supersedes_task_id IS NOT NULL AND
                EXISTS(
                    SELECT 1 FROM review_tasks AS previous
                    WHERE previous.task_id=NEW.supersedes_task_id
                      AND previous.logical_key=NEW.logical_key
                      AND previous.task_version=NEW.task_version-1
                      AND previous.scope=NEW.scope
                      AND previous.task_type=NEW.task_type
                      AND previous.source_kind=NEW.source_kind
                      AND previous.created_ns<NEW.created_ns
                      AND EXISTS(
                          SELECT 1 FROM review_task_events AS current_event
                          WHERE current_event.task_id=previous.task_id
                            AND (
                                current_event.to_state IN ('open','in_review') OR
                                (
                                    current_event.to_state='superseded' AND
                                    current_event.event_id=
                                        NEW.supersession_event_id AND
                                    current_event.actor_kind='system'
                                )
                            )
                            AND NOT EXISTS(
                                SELECT 1 FROM review_task_events AS later_event
                                WHERE later_event.task_id=current_event.task_id
                                  AND later_event.sequence>current_event.sequence
                            )
                      )
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task version chain conflict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_tasks_no_update
    BEFORE UPDATE ON review_tasks
    BEGIN
        SELECT RAISE(ABORT, 'review_tasks is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_tasks_no_delete
    BEFORE DELETE ON review_tasks
    BEGIN
        SELECT RAISE(ABORT, 'review_tasks is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_events_validate_insert
    BEFORE INSERT ON review_task_events
    WHEN NOT (
        EXISTS(
            SELECT 1 FROM review_tasks AS owner_task
            WHERE owner_task.task_id=NEW.task_id
              AND owner_task.created_ns<=NEW.observed_ns
        ) AND
        (
            (
                NEW.sequence=1 AND NEW.previous_event_id IS NULL AND
                NEW.from_state IS NULL AND NEW.to_state='open' AND
                NOT EXISTS(
                    SELECT 1 FROM review_task_events AS existing
                    WHERE existing.task_id=NEW.task_id
                )
            ) OR
            (
                NEW.sequence>1 AND NEW.previous_event_id IS NOT NULL AND
                NEW.from_state IS NOT NULL AND
                EXISTS(
                    SELECT 1 FROM review_task_events AS previous
                    WHERE previous.event_id=NEW.previous_event_id
                      AND previous.task_id=NEW.task_id
                      AND previous.sequence=NEW.sequence-1
                      AND previous.to_state=NEW.from_state
                      AND previous.observed_ns<=NEW.observed_ns
                      AND previous.recorded_ns<NEW.recorded_ns
                ) AND
                (
                    (NEW.from_state='open' AND NEW.to_state IN (
                        'in_review','resolved','dismissed','superseded'
                    )) OR
                    (NEW.from_state='in_review' AND NEW.to_state IN (
                        'resolved','dismissed','superseded'
                    ))
                ) AND (
                    (
                        NEW.to_state='superseded' AND
                        NEW.actor_kind='system' AND NEW.decision_json IS NULL AND
                        (
                            (
                                NEW.actor_id='review-task-refresh' AND
                                json_extract(NEW.provenance_json,'$.reason_code')=
                                    'replacement_task_published' AND
                                json_type(
                                    NEW.provenance_json,'$.replacement_task_id'
                                )='text' AND
                                EXISTS(
                                    SELECT 1
                                    FROM review_tasks AS replaced_task
                                    JOIN review_tasks AS replacement_task
                                      ON replacement_task.supersedes_task_id=
                                         replaced_task.task_id
                                     AND replacement_task.supersession_event_id=
                                         NEW.event_id
                                    JOIN review_task_batches AS replacement_batch
                                      ON replacement_batch.batch_id=
                                         replacement_task.batch_id
                                    JOIN json_each(
                                        replacement_batch.receipt_json,'$.tasks'
                                    ) AS replacement_receipt
                                    WHERE replaced_task.task_id=NEW.task_id
                                      AND json_extract(
                                          replacement_receipt.value,'$.task_id'
                                      )=replacement_task.task_id
                                      AND replacement_task.task_id=json_extract(
                                          NEW.provenance_json,'$.replacement_task_id'
                                      )
                                      AND replacement_batch.scope=
                                          replaced_task.scope
                                      AND replacement_batch.task_type=
                                          replaced_task.task_type
                                      AND replacement_batch.source_snapshot_fingerprint=
                                          json_extract(
                                              NEW.provenance_json,
                                              '$.source_snapshot_fingerprint'
                                          )
                                      AND replacement_batch.confirmed_ns=
                                          NEW.observed_ns
                                      AND NEW.recorded_ns=NEW.observed_ns
                                )
                            ) OR (
                                NEW.actor_id='review-task-source-publication' AND
                                json_extract(NEW.provenance_json,'$.derived')=1 AND
                                json_extract(NEW.provenance_json,'$.reason_code')=
                                    'absent_from_complete_source_snapshot' AND
                                EXISTS(
                                    SELECT 1
                                    FROM review_tasks AS effective_task
                                    JOIN review_tasks AS replacement_task
                                      ON replacement_task.supersedes_task_id=
                                         effective_task.task_id
                                     AND replacement_task.supersession_event_id=
                                         NEW.event_id
                                    JOIN review_task_batches AS effective_batch
                                      ON effective_batch.batch_id=
                                         effective_task.batch_id
                                    JOIN review_task_source_publications AS source_head
                                      ON source_head.publication_id=json_extract(
                                          NEW.provenance_json,
                                          '$.source_publication_id'
                                      )
                                     AND source_head.scope=effective_task.scope
                                     AND source_head.task_type=
                                         effective_task.task_type
                                     AND source_head.selector_signature=
                                         effective_batch.selector_signature
                                    WHERE effective_task.task_id=NEW.task_id
                                      AND effective_task.source_snapshot_fingerprint<>
                                          source_head.source_snapshot_fingerprint
                                      AND source_head.source_snapshot_fingerprint=
                                          json_extract(
                                              NEW.provenance_json,
                                              '$.source_snapshot_fingerprint'
                                          )
                                      AND effective_batch.confirmed_ns<
                                          source_head.confirmed_ns
                                      AND NEW.observed_ns=source_head.confirmed_ns
                                      AND NEW.recorded_ns=source_head.confirmed_ns
                                      AND NOT EXISTS(
                                          SELECT 1
                                          FROM review_task_source_publications AS later_head
                                          WHERE later_head.scope=source_head.scope
                                            AND later_head.task_type=
                                                source_head.task_type
                                            AND later_head.selector_signature=
                                                source_head.selector_signature
                                            AND later_head.revision>
                                                source_head.revision
                                      )
                                      AND NOT EXISTS(
                                          SELECT 1
                                          FROM review_tasks AS replacement
                                          WHERE replacement.logical_key=
                                                effective_task.logical_key
                                            AND replacement.scope=
                                                effective_task.scope
                                            AND replacement.task_type=
                                                effective_task.task_type
                                            AND replacement.source_snapshot_fingerprint=
                                                source_head.source_snapshot_fingerprint
                                      )
                                )
                            )
                        )
                    ) OR (
                        NEW.to_state<>'superseded' AND NOT EXISTS(
                        SELECT 1 FROM review_tasks AS effective_task
                        JOIN review_task_batches AS effective_batch
                          ON effective_batch.batch_id=effective_task.batch_id
                        JOIN review_task_source_publications AS source_head
                          ON source_head.scope=effective_task.scope
                         AND source_head.task_type=effective_task.task_type
                         AND source_head.selector_signature=
                             effective_batch.selector_signature
                        JOIN review_task_events AS effective_previous
                          ON effective_previous.event_id=NEW.previous_event_id
                         AND effective_previous.task_id=NEW.task_id
                        WHERE effective_task.task_id=NEW.task_id
                          AND effective_task.source_snapshot_fingerprint<>
                              source_head.source_snapshot_fingerprint
                          AND effective_batch.confirmed_ns<=
                              source_head.confirmed_ns
                          AND effective_previous.recorded_ns<
                              source_head.confirmed_ns
                          AND NOT EXISTS(
                              SELECT 1
                              FROM review_task_source_publications AS later_head
                              WHERE later_head.scope=source_head.scope
                                AND later_head.task_type=source_head.task_type
                                AND later_head.selector_signature=
                                    source_head.selector_signature
                                AND later_head.revision>source_head.revision
                          )
                          AND NOT EXISTS(
                              SELECT 1 FROM review_tasks AS replacement
                              WHERE replacement.logical_key=
                                    effective_task.logical_key
                                AND replacement.scope=effective_task.scope
                                AND replacement.task_type=effective_task.task_type
                                AND replacement.source_snapshot_fingerprint=
                                    source_head.source_snapshot_fingerprint
                          )
                        )
                    )
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task event CAS or transition conflict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_events_no_update
    BEFORE UPDATE ON review_task_events
    BEGIN
        SELECT RAISE(ABORT, 'review_task_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_events_no_delete
    BEFORE DELETE ON review_task_events
    BEGIN
        SELECT RAISE(ABORT, 'review_task_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batches_validate_insert
    BEFORE INSERT ON review_task_batches
    WHEN NOT (
        (
            NEW.scan_revision=1 AND NEW.previous_batch_id IS NULL AND
            NEW.cursor_before_json IS NULL AND
            NEW.cumulative_scanned_count=NEW.scanned_count AND
            NEW.cumulative_selected_count=NEW.selected_count AND
            NEW.cumulative_evidence_complete=NEW.evidence_complete AND
            NEW.cumulative_evidence_reason IS NEW.evidence_reason AND
            NOT EXISTS(
                SELECT 1 FROM review_task_batches AS existing
                WHERE existing.scope=NEW.scope
                  AND existing.task_type=NEW.task_type
                  AND existing.selector_signature=NEW.selector_signature
                  AND existing.source_snapshot_fingerprint=
                      NEW.source_snapshot_fingerprint
            )
        ) OR (
            NEW.scan_revision>1 AND NEW.previous_batch_id IS NOT NULL AND
            EXISTS(
                SELECT 1 FROM review_task_batches AS previous
                WHERE previous.batch_id=NEW.previous_batch_id
                  AND previous.scope=NEW.scope
                  AND previous.task_type=NEW.task_type
                  AND previous.selector_signature=NEW.selector_signature
                  AND previous.source_snapshot_fingerprint=
                      NEW.source_snapshot_fingerprint
                  AND previous.source_snapshot_json=NEW.source_snapshot_json
                  AND previous.scan_revision=NEW.scan_revision-1
                  AND previous.coverage='partial'
                  AND previous.cursor_after_json IS NEW.cursor_before_json
                  AND previous.confirmed_ns<NEW.confirmed_ns
                  AND NEW.cumulative_scanned_count=
                      previous.cumulative_scanned_count+NEW.scanned_count
                  AND NEW.cumulative_selected_count=
                      previous.cumulative_selected_count+NEW.selected_count
                  AND NEW.cumulative_evidence_complete=(
                      previous.cumulative_evidence_complete AND
                      NEW.evidence_complete
                  )
                  AND NEW.cumulative_evidence_reason IS CASE
                      WHEN previous.cumulative_evidence_complete=0
                          THEN previous.cumulative_evidence_reason
                      WHEN NEW.evidence_complete=0 THEN NEW.evidence_reason
                      ELSE NULL
                  END
                  AND NOT EXISTS(
                      SELECT 1 FROM review_task_batches AS later
                      WHERE later.scope=previous.scope
                        AND later.task_type=previous.task_type
                        AND later.selector_signature=previous.selector_signature
                        AND later.source_snapshot_fingerprint=
                            previous.source_snapshot_fingerprint
                        AND later.scan_revision>previous.scan_revision
                  )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task batch chain conflict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batches_no_update
    BEFORE UPDATE ON review_task_batches
    BEGIN
        SELECT RAISE(ABORT, 'review_task_batches is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batches_no_delete
    BEFORE DELETE ON review_task_batches
    BEGIN
        SELECT RAISE(ABORT, 'review_task_batches is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batch_memberships_validate_insert
    BEFORE INSERT ON review_task_batch_memberships
    WHEN NOT EXISTS(
        SELECT 1 FROM review_tasks AS task
        JOIN review_task_batches AS batch ON batch.batch_id=task.batch_id
        WHERE task.task_id=NEW.task_id
          AND task.batch_id=NEW.batch_id
          AND task.source_input_id=NEW.source_input_id
          AND batch.confirmed_ns=NEW.recorded_ns
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task batch membership mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batch_memberships_no_update
    BEFORE UPDATE ON review_task_batch_memberships
    BEGIN
        SELECT RAISE(ABORT, 'review_task_batch_memberships is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_batch_memberships_no_delete
    BEFORE DELETE ON review_task_batch_memberships
    BEGIN
        SELECT RAISE(ABORT, 'review_task_batch_memberships is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_source_publications_validate_insert
    BEFORE INSERT ON review_task_source_publications
    WHEN NOT (
        EXISTS(
            SELECT 1 FROM review_task_batches AS batch
            JOIN review_task_scan_progress AS progress
              ON progress.last_batch_id=batch.batch_id
             AND progress.scope=batch.scope
             AND progress.task_type=batch.task_type
             AND progress.selector_signature=batch.selector_signature
             AND progress.source_snapshot_fingerprint=
                 batch.source_snapshot_fingerprint
            WHERE batch.batch_id=NEW.batch_id
              AND batch.scope=NEW.scope
              AND batch.task_type=NEW.task_type
              AND batch.selector_signature=NEW.selector_signature
              AND batch.source_snapshot_fingerprint=
                  NEW.source_snapshot_fingerprint
              AND batch.source_snapshot_json=NEW.source_snapshot_json
              AND batch.coverage='complete'
              AND batch.evidence_complete=1
              AND batch.confirmed_ns=NEW.confirmed_ns
              AND progress.source_snapshot_json=NEW.source_snapshot_json
              AND progress.complete=1
              AND progress.evidence_complete=1
              AND batch.scan_revision=progress.revision
              AND batch.cumulative_scanned_count=progress.scanned_count
              AND batch.cumulative_selected_count=progress.selected_count
              AND batch.cumulative_evidence_complete=progress.evidence_complete
              AND batch.cumulative_evidence_reason IS progress.evidence_reason
              AND (
                  SELECT COUNT(*)
                  FROM review_task_batch_memberships AS membership
                  WHERE membership.batch_id=batch.batch_id
              )=batch.selected_count
        ) AND (
            (
                NEW.revision=1 AND NEW.previous_publication_id IS NULL AND
                NOT EXISTS(
                    SELECT 1 FROM review_task_source_publications AS existing
                    WHERE existing.scope=NEW.scope
                      AND existing.task_type=NEW.task_type
                      AND existing.selector_signature=NEW.selector_signature
                )
            ) OR (
                NEW.revision>1 AND NEW.previous_publication_id IS NOT NULL AND
                EXISTS(
                    SELECT 1 FROM review_task_source_publications AS previous
                    WHERE previous.publication_id=NEW.previous_publication_id
                      AND previous.scope=NEW.scope
                      AND previous.task_type=NEW.task_type
                      AND previous.selector_signature=NEW.selector_signature
                      AND previous.revision=NEW.revision-1
                      AND previous.confirmed_ns<NEW.confirmed_ns
                      AND NOT EXISTS(
                          SELECT 1
                          FROM review_task_source_publications AS later
                          WHERE later.scope=previous.scope
                            AND later.task_type=previous.task_type
                            AND later.selector_signature=
                                previous.selector_signature
                            AND later.revision>previous.revision
                      )
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task source publication conflict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_source_publications_no_update
    BEFORE UPDATE ON review_task_source_publications
    BEGIN
        SELECT RAISE(ABORT, 'review_task_source_publications is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_source_publications_no_delete
    BEFORE DELETE ON review_task_source_publications
    BEGIN
        SELECT RAISE(ABORT, 'review_task_source_publications is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_scan_progress_validate_insert
    BEFORE INSERT ON review_task_scan_progress
    WHEN NEW.revision<>1 OR NOT EXISTS(
        SELECT 1 FROM review_task_batches AS batch
        WHERE batch.batch_id=NEW.last_batch_id
          AND batch.scope=NEW.scope
          AND batch.task_type=NEW.task_type
          AND batch.selector_signature=NEW.selector_signature
          AND batch.source_snapshot_fingerprint=
              NEW.source_snapshot_fingerprint
          AND batch.source_snapshot_json=NEW.source_snapshot_json
          AND batch.cursor_before_json IS NULL
          AND batch.cursor_after_json IS NEW.cursor_json
          AND batch.previous_batch_id IS NULL
          AND batch.scan_revision=NEW.revision
          AND batch.scanned_count=NEW.scanned_count
          AND batch.selected_count=NEW.selected_count
          AND batch.cumulative_scanned_count=NEW.scanned_count
          AND batch.cumulative_selected_count=NEW.selected_count
          AND batch.evidence_complete=NEW.evidence_complete
          AND batch.evidence_reason IS NEW.evidence_reason
          AND batch.cumulative_evidence_complete=NEW.evidence_complete
          AND batch.cumulative_evidence_reason IS NEW.evidence_reason
          AND (
              SELECT COUNT(*) FROM review_task_batch_memberships AS membership
              WHERE membership.batch_id=batch.batch_id
          )=batch.selected_count
          AND (
              (batch.coverage='complete' AND NEW.complete=1) OR
              (batch.coverage='partial' AND NEW.complete=0)
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'review task initial scan receipt mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_scan_progress_validate_update
    BEFORE UPDATE ON review_task_scan_progress
    WHEN OLD.complete=1 OR
         NEW.progress_id IS NOT OLD.progress_id OR
         NEW.scope IS NOT OLD.scope OR
         NEW.task_type IS NOT OLD.task_type OR
         NEW.selector_signature IS NOT OLD.selector_signature OR
         NEW.source_snapshot_fingerprint IS NOT
             OLD.source_snapshot_fingerprint OR
         NEW.source_snapshot_json IS NOT OLD.source_snapshot_json OR
         NEW.created_ns<>OLD.created_ns OR
         NEW.revision<>OLD.revision+1 OR
         NEW.updated_ns<=OLD.updated_ns OR
         NEW.last_batch_id=OLD.last_batch_id OR
         NOT EXISTS(
             SELECT 1 FROM review_task_batches AS batch
             WHERE batch.batch_id=NEW.last_batch_id
               AND batch.scope=NEW.scope
               AND batch.task_type=NEW.task_type
               AND batch.selector_signature=NEW.selector_signature
               AND batch.source_snapshot_fingerprint=
                   NEW.source_snapshot_fingerprint
               AND batch.source_snapshot_json=NEW.source_snapshot_json
               AND batch.cursor_before_json IS OLD.cursor_json
               AND batch.cursor_after_json IS NEW.cursor_json
               AND batch.previous_batch_id=OLD.last_batch_id
               AND batch.scan_revision=NEW.revision
               AND batch.scanned_count=
                   NEW.scanned_count-OLD.scanned_count
               AND batch.selected_count=
                   NEW.selected_count-OLD.selected_count
               AND batch.cumulative_scanned_count=NEW.scanned_count
               AND batch.cumulative_selected_count=NEW.selected_count
               AND NEW.evidence_complete=(
                   OLD.evidence_complete AND batch.evidence_complete
               )
               AND NEW.evidence_reason IS CASE
                   WHEN OLD.evidence_complete=0 THEN OLD.evidence_reason
                   WHEN batch.evidence_complete=0 THEN batch.evidence_reason
                   ELSE NULL
               END
               AND batch.cumulative_evidence_complete=NEW.evidence_complete
               AND batch.cumulative_evidence_reason IS NEW.evidence_reason
               AND (
                   SELECT COUNT(*)
                   FROM review_task_batch_memberships AS membership
                   WHERE membership.batch_id=batch.batch_id
               )=batch.selected_count
               AND (
                   (batch.coverage='complete' AND NEW.complete=1) OR
                   (batch.coverage='partial' AND NEW.complete=0)
               )
         )
    BEGIN
        SELECT RAISE(ABORT, 'review task scan progress CAS conflict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_task_scan_progress_no_delete
    BEFORE DELETE ON review_task_scan_progress
    BEGIN
        SELECT RAISE(ABORT, 'review_task_scan_progress is durable');
    END
    """,
)

_TABLE_NAMES = (
    "metadata",
    "initial_runs",
    "run_events",
    "route_runs",
    "route_phase_runs",
    "run_actions",
    "file_actions",
    "file_action_events",
    "file_action_reconciliation_events",
    "route_candidates",
    "content_type_cache",
    "review_candidates",
    "review_decisions",
    "review_evidence_examples",
    "review_evidence_progress",
    "review_task_batches",
    "review_tasks",
    "review_task_batch_memberships",
    "review_task_events",
    "review_task_scan_progress",
    "review_task_source_publications",
)

_NAMED_INDEXES = {
    "run_events_run_idx": "run_events",
    "route_runs_status_idx": "route_runs",
    "route_phase_status_idx": "route_phase_runs",
    "file_actions_run_idx": "file_actions",
    "file_actions_idempotency_key_idx": "file_actions",
    "file_actions_recovery_idx": "file_actions",
    "file_action_events_action_idx": "file_action_events",
    "file_action_reconciliation_events_action_idx": ("file_action_reconciliation_events"),
    "route_candidates_mime_idx": "route_candidates",
    "review_candidates_status_idx": "review_candidates",
    "review_candidates_path_idx": "review_candidates",
    "review_decisions_identity_idx": "review_decisions",
    "review_decisions_status_idx": "review_decisions",
    "review_evidence_outcome_idx": "review_evidence_examples",
    "review_evidence_target_idx": "review_evidence_examples",
    "review_tasks_queue_idx": "review_tasks",
    "review_tasks_source_idx": "review_tasks",
    "review_task_events_current_idx": "review_task_events",
    "review_task_batches_scan_idx": "review_task_batches",
    "review_task_source_publications_head_idx": ("review_task_source_publications"),
}


# endregion [01]


# region [02] Sequential migrations


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted_identifier(table)})")
    }


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    existing = _column_names(connection, table)
    for name, declaration in columns:
        if name in existing:
            continue
        connection.execute(
            f"ALTER TABLE {_quoted_identifier(table)} ADD COLUMN "
            f"{_quoted_identifier(name)} {declaration}"
        )


def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "initial_runs",
        (
            ("reconciliation_records", "INTEGER"),
            ("inventory_attempts", "INTEGER"),
        ),
    )


def _migrate_2_to_3(connection: sqlite3.Connection) -> None:
    del connection


def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "run_actions",
        (
            ("empty_directory_candidates", "INTEGER NOT NULL DEFAULT 0"),
            ("empty_directories_trashed", "INTEGER NOT NULL DEFAULT 0"),
            ("empty_directory_skips", "INTEGER NOT NULL DEFAULT 0"),
        ),
    )


def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
    del connection


def _migrate_5_to_6(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "initial_runs", (("inventory_mode", "TEXT"),))


def _migrate_6_to_7(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "run_actions",
        (
            ("type_cache_hits", "INTEGER NOT NULL DEFAULT 0"),
            ("type_cache_misses", "INTEGER NOT NULL DEFAULT 0"),
        ),
    )


def _migrate_7_to_8(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "content_type_cache",
        (("last_seen_run_id", "INTEGER NOT NULL DEFAULT 0"),),
    )
    _add_columns(
        connection,
        "run_actions",
        (("type_cache_pruned", "INTEGER NOT NULL DEFAULT 0"),),
    )


def _migrate_8_to_9(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "run_actions",
        (("stale_inventory", "INTEGER NOT NULL DEFAULT 0"),),
    )


def _migrate_9_to_10(connection: sqlite3.Connection) -> None:
    del connection


def _migrate_10_to_11(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "content_type_cache",
        (("birthtime_ns", "INTEGER NOT NULL DEFAULT -1"),),
    )


def _migrate_11_to_12(connection: sqlite3.Connection) -> None:
    del connection


def _migrate_12_to_13(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "initial_runs",
        (
            ("run_kind", "TEXT NOT NULL DEFAULT 'initial'"),
            ("source_run_id", "INTEGER"),
            ("current_phase", "TEXT"),
            ("owner_pid", "INTEGER"),
            ("heartbeat_ns", "INTEGER"),
        ),
    )
    _add_columns(
        connection,
        "route_runs",
        (
            ("current_phase", "TEXT"),
            ("heartbeat_ns", "INTEGER"),
            ("source_run_id", "INTEGER"),
        ),
    )


def _migrate_13_to_14(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "review_candidates",
        (("resolved_run_id", "INTEGER"),),
    )


def _migrate_14_to_15(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "review_decisions",
        (
            ("source_status", "TEXT"),
            (
                "recommendation",
                "TEXT CHECK(recommendation IS NULL OR recommendation IN "
                "('retry','keep_protected','manual_review','deletion_candidate'))",
            ),
            (
                "retryable",
                "INTEGER CHECK(retryable IS NULL OR retryable IN (0,1))",
            ),
            (
                "confidence",
                "REAL CHECK(confidence IS NULL OR (confidence>=0.0 AND confidence<=1.0))",
            ),
            ("evidence_json", "TEXT"),
            ("detector_version", "TEXT"),
        ),
    )


def _migrate_15_to_16(connection: sqlite3.Connection) -> None:
    # ``_create_tables`` runs before sequential migrations so this is normally
    # already present.  Keep the transition explicit and independently safe.
    connection.execute(_REVIEW_EVIDENCE_TABLE_STATEMENT)
    connection.execute(_REVIEW_EVIDENCE_PROGRESS_TABLE_STATEMENT)


_REVIEW_CANDIDATE_COLUMNS = (
    "route_name",
    "volume_id",
    "file_id",
    "reason_code",
    "path",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "source_status",
    "recommendation",
    "retryable",
    "confidence",
    "evidence_json",
    "detector_version",
    "status",
    "first_detected_ns",
    "last_detected_ns",
    "last_seen_run_id",
    "resolved_ns",
    "resolved_run_id",
    "resolution_note",
)

_REVIEW_DECISION_COLUMNS = (
    "decision_id",
    "idempotency_key",
    "route_name",
    "volume_id",
    "file_id",
    "reason_code",
    "candidate_generation",
    "path",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "source_status",
    "recommendation",
    "retryable",
    "confidence",
    "evidence_json",
    "detector_version",
    "status",
    "actor",
    "provenance_json",
    "note",
    "decided_ns",
    "recorded_ns",
)

_REVIEW_CANDIDATE_LEGACY_COLUMN_ORDER = (
    *_REVIEW_CANDIDATE_COLUMNS[:-3],
    "resolved_ns",
    "resolution_note",
    "resolved_run_id",
)

_REVIEW_DECISION_LEGACY_COLUMN_ORDER = (
    "decision_id",
    "idempotency_key",
    "route_name",
    "volume_id",
    "file_id",
    "reason_code",
    "candidate_generation",
    "path",
    "size",
    "mtime_ns",
    "birthtime_ns",
    "status",
    "actor",
    "provenance_json",
    "note",
    "decided_ns",
    "recorded_ns",
    "source_status",
    "recommendation",
    "retryable",
    "confidence",
    "evidence_json",
    "detector_version",
)

type _CanonicalTokens = tuple[str, ...]
type _OrdinaryDefinitionParts = tuple[
    tuple[tuple[str, _CanonicalTokens], ...],
    tuple[_CanonicalTokens, ...],
]


def _ordinary_definition_parts(definition: object) -> _OrdinaryDefinitionParts:
    columns = getattr(definition, "columns", None)
    constraints = getattr(definition, "constraints", None)
    if not isinstance(columns, tuple) or not isinstance(constraints, tuple):
        raise _FrameworkSchemaMigrationError("review table does not have an ordinary definition")
    return (
        cast(tuple[tuple[str, _CanonicalTokens], ...], columns),
        cast(tuple[_CanonicalTokens, ...], constraints),
    )


def _definition_without_inline_checks(
    definition: object,
    checkless_columns: frozenset[str],
) -> _OrdinaryDefinitionParts:
    columns, constraints = _ordinary_definition_parts(definition)
    legacy_columns: list[tuple[str, _CanonicalTokens]] = []
    for name, tokens in columns:
        if name in checkless_columns:
            try:
                check_index = tokens.index("keyword:check")
            except ValueError as exc:  # pragma: no cover - canonical DDL invariant
                raise RuntimeError(f"canonical CHECK is missing from {name}") from exc
            tokens = tokens[:check_index]
        legacy_columns.append((name, tokens))
    return tuple(legacy_columns), constraints


def _rebuild_table_with_current_definition(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    """Atomically copy one known legacy table into its canonical definition."""

    legacy_table = f"__neocortex_schema_17_{table}"
    collision = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=?",
        (legacy_table,),
    ).fetchone()
    if collision is not None:
        raise _FrameworkSchemaMigrationError(
            f"reserved migration object already exists: {legacy_table}"
        )
    source_count = int(
        connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
    )
    connection.execute(
        f"ALTER TABLE {_quoted_identifier(table)} RENAME TO {_quoted_identifier(legacy_table)}"
    )
    _create_tables(connection)
    column_sql = ",".join(_quoted_identifier(column) for column in columns)
    inserted = connection.execute(
        f"INSERT INTO {_quoted_identifier(table)}({column_sql}) "
        f"SELECT {column_sql} FROM {_quoted_identifier(legacy_table)}"
    )
    target_count = int(
        connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
    )
    if inserted.rowcount != source_count or target_count != source_count:
        raise _FrameworkSchemaMigrationError(f"row preservation failed while rebuilding {table}")
    connection.execute(f"DROP TABLE {_quoted_identifier(legacy_table)}")


def _migrate_16_to_17(connection: sqlite3.Connection) -> None:
    """Materialize CHECK constraints omitted by historical additive upgrades."""

    actual = {table.name: table for table in capture_sqlite_schema_contract(connection).tables}
    expected = {table.name: table for table in _exact_schema_contract().tables}
    review_tables = (
        (
            "review_candidates",
            _REVIEW_CANDIDATE_COLUMNS,
            (_REVIEW_CANDIDATE_COLUMNS, _REVIEW_CANDIDATE_LEGACY_COLUMN_ORDER),
            frozenset({"recommendation", "retryable", "confidence", "status"}),
        ),
        (
            "review_decisions",
            _REVIEW_DECISION_COLUMNS,
            (_REVIEW_DECISION_COLUMNS, _REVIEW_DECISION_LEGACY_COLUMN_ORDER),
            frozenset({"candidate_generation", "status"}),
        ),
    )
    for table, columns, allowed_orders, checkless_columns in review_tables:
        actual_table = actual.get(table)
        expected_table = expected.get(table)
        if actual_table is None or expected_table is None:  # pragma: no cover
            raise _FrameworkSchemaMigrationError(f"review schema table is missing: {table}")
        if actual_table.definition == expected_table.definition:
            continue
        actual_order = tuple(column.name for column in actual_table.columns)
        if actual_order not in allowed_orders:
            raise _FrameworkSchemaMigrationError(
                f"{table} has an unexpected legacy column layout: {actual_order!r}"
            )
        if (
            actual_table.table_type != expected_table.table_type
            or actual_table.without_rowid != expected_table.without_rowid
            or actual_table.strict != expected_table.strict
            or actual_table.foreign_keys != expected_table.foreign_keys
            or {column.name: column for column in actual_table.columns}
            != {column.name: column for column in expected_table.columns}
        ):
            raise _FrameworkSchemaMigrationError(
                f"{table} has incompatible legacy column declarations or options"
            )
        unexpected_indexes = set(actual_table.indexes) - set(expected_table.indexes)
        if unexpected_indexes:
            names = sorted(index.name or "<automatic>" for index in unexpected_indexes)
            raise _FrameworkSchemaMigrationError(
                f"{table} has unexpected legacy indexes: {', '.join(names)}"
            )
        triggers = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (table,),
            )
        )
        if triggers:
            raise _FrameworkSchemaMigrationError(f"{table} has unexpected triggers: {triggers!r}")
        if _ordinary_definition_parts(actual_table.definition) != _definition_without_inline_checks(
            expected_table.definition,
            checkless_columns,
        ):
            raise _FrameworkSchemaMigrationError(f"{table} has an unsupported legacy definition")
        _rebuild_table_with_current_definition(connection, table, columns)


_FILE_ACTION_COLUMNS_V17 = frozenset(
    {
        "action_id",
        "run_id",
        "action_type",
        "source_path",
        "target_path",
        "detected_mime",
        "evidence",
        "apply_requested",
        "status",
        "detail",
        "started_ns",
        "completed_ns",
    }
)


def _migrate_17_to_18(connection: sqlite3.Connection) -> None:
    """Add action receipts without assigning meaning to legacy action rows."""

    existing = _column_names(connection, "file_actions")
    # Very old schemas can lack file_actions, so _create_tables() materializes
    # its current definition before the sequential migrations run.  Accept
    # only the explicitly known post-v17 columns; owner extensions still fail
    # closed as unexpected schema.
    current_additions = {
        "idempotency_key",
        "expected_identity_json",
        "effect_receipt_json",
        "applying_ns",
        "corpus_access_mode",
        "protected_root",
        "protected_root_device_id_hex",
        "protected_root_file_id_hex",
        "protected_root_birthtime_ns",
    }
    unexpected = existing - _FILE_ACTION_COLUMNS_V17 - current_additions
    missing_legacy = _FILE_ACTION_COLUMNS_V17 - existing
    if unexpected or missing_legacy:
        raise _FrameworkSchemaMigrationError(
            "file_actions has an unsupported version-17 column layout: "
            f"missing={sorted(missing_legacy)!r}, unexpected={sorted(unexpected)!r}"
        )
    source_count = int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0])
    _add_columns(
        connection,
        "file_actions",
        (
            ("idempotency_key", "TEXT"),
            ("expected_identity_json", "TEXT"),
            ("effect_receipt_json", "TEXT"),
            ("applying_ns", "INTEGER"),
        ),
    )
    target_count = int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0])
    if target_count != source_count:
        raise _FrameworkSchemaMigrationError(
            "file_actions row preservation failed during version-18 migration"
        )


def _migrate_18_to_19(connection: sqlite3.Connection) -> None:
    """Add an empty append-only reconciliation log without rewriting actions."""

    action_count = int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0])
    action_event_count = int(
        connection.execute("SELECT COUNT(*) FROM file_action_events").fetchone()[0]
    )
    reconciliation_count = int(
        connection.execute("SELECT COUNT(*) FROM file_action_reconciliation_events").fetchone()[0]
    )
    if reconciliation_count != 0:
        raise _FrameworkSchemaMigrationError(
            "version-18 database already contains reconciliation events"
        )
    if int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0]) != (action_count):
        raise _FrameworkSchemaMigrationError(
            "file_actions row preservation failed during version-19 migration"
        )
    if (
        int(connection.execute("SELECT COUNT(*) FROM file_action_events").fetchone()[0])
        != action_event_count
    ):
        raise _FrameworkSchemaMigrationError(
            "file_action_events row preservation failed during version-19 migration"
        )


def _migrate_19_to_20(connection: sqlite3.Connection) -> None:
    """Add immutable corpus-policy evidence without reinterpreting legacy rows."""

    run_count = int(connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0])
    action_count = int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0])
    _add_columns(
        connection,
        "initial_runs",
        (
            (
                "corpus_access_mode",
                "TEXT NOT NULL DEFAULT 'normal' CHECK("
                "corpus_access_mode IN ('normal','analyze_only'))",
            ),
            ("root_device_id_hex", "TEXT"),
            ("root_file_id_hex", "TEXT"),
            ("root_birthtime_ns", "INTEGER"),
            ("state_directory", "TEXT"),
            ("inventory_policy_signature", "TEXT"),
        ),
    )
    _add_columns(
        connection,
        "file_actions",
        (
            (
                "corpus_access_mode",
                "TEXT NOT NULL DEFAULT 'normal' CHECK("
                "corpus_access_mode IN ('normal','analyze_only'))",
            ),
            ("protected_root", "TEXT"),
            ("protected_root_device_id_hex", "TEXT"),
            ("protected_root_file_id_hex", "TEXT"),
            ("protected_root_birthtime_ns", "INTEGER"),
        ),
    )
    if int(connection.execute("SELECT COUNT(*) FROM initial_runs").fetchone()[0]) != (run_count):
        raise _FrameworkSchemaMigrationError(
            "initial_runs row preservation failed during version-20 migration"
        )
    if int(connection.execute("SELECT COUNT(*) FROM file_actions").fetchone()[0]) != (action_count):
        raise _FrameworkSchemaMigrationError(
            "file_actions row preservation failed during version-20 migration"
        )


def _migrate_20_to_21(connection: sqlite3.Connection) -> None:
    """Publish empty review-task stores without deriving synthetic tasks."""

    preserved_tables = (
        "initial_runs",
        "run_events",
        "route_runs",
        "route_phase_runs",
        "run_actions",
        "file_actions",
        "file_action_events",
        "file_action_reconciliation_events",
        "route_candidates",
        "content_type_cache",
        "review_candidates",
        "review_decisions",
        "review_evidence_examples",
        "review_evidence_progress",
    )
    before = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
        )
        for table in preserved_tables
    }
    for table in (
        "review_tasks",
        "review_task_events",
        "review_task_batches",
        "review_task_batch_memberships",
        "review_task_scan_progress",
        "review_task_source_publications",
    ):
        if int(
            connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
        ):
            raise _FrameworkSchemaMigrationError(
                f"version-20 database already contains rows in {table}"
            )
    after = {
        table: int(
            connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
        )
        for table in preserved_tables
    }
    if after != before:
        raise _FrameworkSchemaMigrationError("owner rows changed during empty version-21 migration")


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
    8: _migrate_8_to_9,
    9: _migrate_9_to_10,
    10: _migrate_10_to_11,
    11: _migrate_11_to_12,
    12: _migrate_12_to_13,
    13: _migrate_13_to_14,
    14: _migrate_14_to_15,
    15: _migrate_15_to_16,
    16: _migrate_16_to_17,
    17: _migrate_17_to_18,
    18: _migrate_18_to_19,
    19: _migrate_19_to_20,
    20: _migrate_20_to_21,
}


# endregion [02]


# region [03] Contract derivation and validation


@dataclass(frozen=True, slots=True)
class _ColumnContract:
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class _TableContract:
    columns: dict[str, _ColumnContract]
    without_rowid: bool
    strict: bool


@dataclass(frozen=True, slots=True)
class _SchemaContract:
    tables: dict[str, _TableContract]
    indexes: dict[str, tuple[str, tuple[str, ...], bool]]
    unique_keys: frozenset[tuple[str, tuple[str, ...]]]


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_options(connection: sqlite3.Connection) -> dict[str, tuple[bool, bool]]:
    return {
        str(row[1]): (bool(row[4]), bool(row[5]))
        for row in connection.execute("PRAGMA table_list")
        if str(row[2]) == "table"
    }


def _index_columns(connection: sqlite3.Connection, index: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA index_info({_quoted_identifier(index)})").fetchall()
    return tuple(str(row[2]) for row in rows)


def _build_exact_schema(connection: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for statement in _INDEX_STATEMENTS:
        connection.execute(statement)
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(statement)


def _build_v20_exact_schema(connection: sqlite3.Connection) -> None:
    """Reconstruct the exact v20 contract for read-only compatibility checks."""

    _build_exact_schema(connection)
    for trigger in (
        "review_tasks_validate_insert",
        "review_tasks_no_update",
        "review_tasks_no_delete",
        "review_task_events_validate_insert",
        "review_task_events_no_update",
        "review_task_events_no_delete",
        "review_task_batches_validate_insert",
        "review_task_batches_no_update",
        "review_task_batches_no_delete",
        "review_task_batch_memberships_validate_insert",
        "review_task_batch_memberships_no_update",
        "review_task_batch_memberships_no_delete",
        "review_task_scan_progress_validate_insert",
        "review_task_scan_progress_validate_update",
        "review_task_scan_progress_no_delete",
        "review_task_source_publications_validate_insert",
        "review_task_source_publications_no_update",
        "review_task_source_publications_no_delete",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    for table in (
        "review_task_scan_progress",
        "review_task_batch_memberships",
        "review_task_events",
        "review_tasks",
        "review_task_source_publications",
        "review_task_batches",
    ):
        connection.execute(f"DROP TABLE {table}")


def _build_v19_exact_schema(connection: sqlite3.Connection) -> None:
    """Reconstruct the exact v19 contract for read-only compatibility checks."""

    _build_v20_exact_schema(connection)
    for trigger in (
        "initial_runs_corpus_policy_no_update",
        "file_actions_corpus_policy_insert",
        "file_actions_corpus_policy_no_update",
    ):
        connection.execute(f"DROP TRIGGER {trigger}")
    for column in (
        "inventory_policy_signature",
        "state_directory",
        "root_birthtime_ns",
        "root_file_id_hex",
        "root_device_id_hex",
        "corpus_access_mode",
    ):
        connection.execute(f"ALTER TABLE initial_runs DROP COLUMN {column}")
    for column in (
        "protected_root_birthtime_ns",
        "protected_root_file_id_hex",
        "protected_root_device_id_hex",
        "protected_root",
        "corpus_access_mode",
    ):
        connection.execute(f"ALTER TABLE file_actions DROP COLUMN {column}")


@lru_cache(maxsize=1)
def _exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_exact_schema)


@lru_cache(maxsize=1)
def _v20_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v20_exact_schema)


@lru_cache(maxsize=1)
def _v19_exact_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_v19_exact_schema)


def validate_framework_schema_v19(connection: sqlite3.Connection) -> None:
    """Validate an exact v19 database without migrating it to v20."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _v19_exact_schema_contract(),
            label="framework v19 read compatibility",
            exact=True,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v19 schema contract validation failed: {exc}") from exc


def validate_framework_schema_v20(connection: sqlite3.Connection) -> None:
    """Validate an exact v20 database without migrating it to v21."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _v20_exact_schema_contract(),
            label="framework v20 read compatibility",
            exact=True,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v20 schema contract validation failed: {exc}") from exc


def validate_framework_schema_v21(connection: sqlite3.Connection) -> None:
    """Validate the exact current v21 contract without creating or migrating state."""

    try:
        validate_sqlite_schema_contract(
            connection,
            _exact_schema_contract(),
            label="framework v21",
            exact=True,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework v21 schema contract validation failed: {exc}") from exc


@lru_cache(maxsize=1)
def _canonical_contract() -> _SchemaContract:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _TABLE_STATEMENTS:
            connection.execute(statement)
        for statement in _INDEX_STATEMENTS:
            connection.execute(statement)

        table_options = _table_options(connection)
        tables: dict[str, _TableContract] = {}
        unique_keys: set[tuple[str, tuple[str, ...]]] = set()
        for table in _TABLE_NAMES:
            columns = {
                str(row[1]): _ColumnContract(
                    declared_type=str(row[2]).upper(),
                    not_null=bool(row[3]),
                    default_sql=None if row[4] is None else str(row[4]),
                    primary_key_position=int(row[5]),
                )
                for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
            }
            without_rowid, strict = table_options[table]
            tables[table] = _TableContract(columns, without_rowid, strict)
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})"):
                if bool(row[2]) and str(row[3]) == "u":
                    unique_keys.add((table, _index_columns(connection, str(row[1]))))

        indexes: dict[str, tuple[str, tuple[str, ...], bool]] = {}
        for index, table in _NAMED_INDEXES.items():
            row = next(
                (
                    item
                    for item in connection.execute(
                        f"PRAGMA index_list({_quoted_identifier(table)})"
                    )
                    if str(item[1]) == index
                ),
                None,
            )
            if row is None:  # pragma: no cover - canonical DDL invariant
                raise AssertionError(f"canonical index was not created: {index}")
            indexes[index] = (
                table,
                _index_columns(connection, index),
                bool(row[2]),
            )
        return _SchemaContract(tables, indexes, frozenset(unique_keys))
    finally:
        connection.close()


def _validate_schema(connection: sqlite3.Connection) -> None:
    expected = _canonical_contract()
    actual_objects = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    table_options = _table_options(connection)
    errors: list[str] = []

    for table, table_contract in expected.tables.items():
        object_type = actual_objects.get(table)
        if object_type != "table":
            errors.append(
                f"required table {table!r} is missing"
                if object_type is None
                else f"required table {table!r} is a {object_type}"
            )
            continue
        actual_columns = {
            str(row[1]): _ColumnContract(
                declared_type=str(row[2]).upper(),
                not_null=bool(row[3]),
                default_sql=None if row[4] is None else str(row[4]),
                primary_key_position=int(row[5]),
            )
            for row in connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table)})")
        }
        for column, column_contract in table_contract.columns.items():
            actual = actual_columns.get(column)
            if actual is None:
                errors.append(f"table {table!r} is missing column {column!r}")
            elif actual != column_contract:
                errors.append(f"table {table!r} column {column!r} has an invalid declaration")
        options = table_options.get(table)
        if options is not None and options != (
            table_contract.without_rowid,
            table_contract.strict,
        ):
            errors.append(f"table {table!r} has incompatible table options")

    for index, (table, columns, unique) in expected.indexes.items():
        rows = {
            str(row[1]): row
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
        }
        actual = rows.get(index)
        if actual is None:
            errors.append(f"required index {index!r} is missing from table {table!r}")
            continue
        if bool(actual[2]) != unique or bool(actual[4]):
            errors.append(f"index {index!r} has incompatible options")
        if _index_columns(connection, index) != columns:
            errors.append(f"index {index!r} has incompatible columns")

    for table, columns in expected.unique_keys:
        actual_unique_keys = {
            _index_columns(connection, str(row[1]))
            for row in connection.execute(f"PRAGMA index_list({_quoted_identifier(table)})")
            if bool(row[2])
        }
        if columns not in actual_unique_keys:
            errors.append(f"table {table!r} is missing required unique key {columns!r}")

    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(f"framework schema contract validation failed: {detail}")
    try:
        validate_sqlite_schema_contract(
            connection,
            _exact_schema_contract(),
            label="framework",
            exact=True,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"framework schema contract validation failed: {exc}") from exc


# endregion [03]


# region [04] Initialization transaction


def _application_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    objects = _application_objects(connection)
    if not objects:
        return None
    metadata_type = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='metadata'"
    ).fetchone()
    if metadata_type != ("table",):
        raise RuntimeError("framework database contains objects but no valid metadata table")
    try:
        rows = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version' LIMIT 2"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("framework schema metadata is malformed") from exc
    if len(rows) != 1:
        raise RuntimeError("framework schema metadata has no unique schema_version")
    raw_version = str(rows[0][0])
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise RuntimeError(f"framework schema version is not an integer: {raw_version!r}") from exc
    if raw_version != str(version):
        raise RuntimeError(f"framework schema version is not canonical: {raw_version!r}")
    return version


def _require_supported_version(version: int | None) -> None:
    if version is None:
        return
    if version < 1 or version > SCHEMA_VERSION:
        raise RuntimeError(
            f"framework schema {version} is unsupported; expected 1..{SCHEMA_VERSION}"
        )


def _validate_framework_storage_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise RuntimeError(f"{label} has a foreign-key integrity violation")
    integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
    if integrity != ("ok",):
        raise RuntimeError(f"{label} failed integrity_check: {integrity!r}")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("PRAGMA wal_autocheckpoint=4096")
    connection.execute("PRAGMA journal_size_limit=268435456")


def _create_tables(connection: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)


def _create_indexes(connection: sqlite3.Connection) -> None:
    for statement in _INDEX_STATEMENTS:
        connection.execute(statement)


def _create_triggers(connection: sqlite3.Connection) -> None:
    for statement in _TRIGGER_STATEMENTS:
        connection.execute(statement)


def _apply_migrations(connection: sqlite3.Connection, version: int) -> None:
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:  # pragma: no cover - module invariant
            raise RuntimeError(f"framework schema migration {version} is missing")
        migration(connection)
        version += 1
        updated = connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(version),),
        )
        if updated.rowcount != 1:
            raise RuntimeError("framework schema_version disappeared during migration")


def initialize_framework_schema(
    connection: sqlite3.Connection,
    post_migration: Callable[[], None],
) -> None:
    """Create, migrate, and validate the schema in one atomic transaction."""

    initial_version = _read_schema_version(connection)
    _require_supported_version(initial_version)
    if initial_version == SCHEMA_VERSION:
        # Reject a falsely current database without repairing or otherwise mutating it.
        _validate_schema(connection)
    elif initial_version == 20:
        # The additive v21 migration must not silently repair damaged v20 state.
        validate_framework_schema_v20(connection)
        _validate_framework_storage_integrity(connection, label="framework v20")

    _configure_connection(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _read_schema_version(connection)
        _require_supported_version(version)
        if version is None:
            _create_tables(connection)
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
        elif version < SCHEMA_VERSION:
            if version == 20:
                validate_framework_schema_v20(connection)
                _validate_framework_storage_integrity(
                    connection,
                    label="framework v20 locked preflight",
                )
            _create_tables(connection)
            _apply_migrations(connection, version)

        _create_indexes(connection)
        _create_triggers(connection)
        _validate_schema(connection)
        post_migration()
        _validate_schema(connection)
        if initial_version == 20:
            _validate_framework_storage_integrity(connection, label="framework v21 migration")
        connection.commit()
    except _FrameworkSchemaMigrationError as exc:
        connection.rollback()
        source = "new" if initial_version is None else str(initial_version)
        raise RuntimeError(
            f"framework schema initialization from version {source} failed: {exc}"
        ) from exc
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        source = "new" if initial_version is None else str(initial_version)
        raise RuntimeError(f"framework schema initialization from version {source} failed") from exc
    except BaseException:
        connection.rollback()
        raise


# endregion [04]
