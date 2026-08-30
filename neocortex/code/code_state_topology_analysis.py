"""Resolved Text transaction-boundary evidence for Code review.

The analysis asks whether the *current* Text v2 state is relationally closed at
its declared terminal-publication boundary.  It reads one immutable SQLite
owner, validates its exact schema, and runs exhaustive anti-joins.  A closed
final state is not evidence of historical atomicity or crash recovery, so the
result always abstains from inference and change decisions.
"""

from __future__ import annotations
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database
from neocortex.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.safety.state_topology_contracts import TEXT_DERIVATION_WORKFLOW
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION, text_schema_contract

CODE_STATE_TOPOLOGY_SCHEMA = "neocortex.code-state-topology/v1"
TEXT_TERMINAL_PUBLICATION_POLICY = "text-terminal-publication-closure-v1"
TEXT_TERMINAL_PUBLICATION_QUESTION_ID = "state.text_terminal_publication_is_relationally_closed"
TEXT_TERMINAL_PUBLICATION_QUESTION_VERSION = "v1"
_BOUNDARY_ID = "text.terminal-publication"
_RESOLVER_ID = "text.sqlite-terminal-publication-closure-resolver"
_RESOLVER_VERSION = "v1"
_SOURCE_RECORD_KIND = "transaction_boundary_closure"
TEXT_STATE_TOPOLOGY_EXAMPLE_LIMIT = 100

_LIMITATIONS = (
    "final_relational_closure_does_not_prove_historical_atomicity",
    "process_death_power_loss_and_restart_recovery_are_not_exercised",
    "legacy_documents_without_revision_are_partitioned_not_attributed",
    "running_attempts_are_in_progress_not_terminal_publications",
    "single_text_owner_only_no_cross_store_transaction_is_claimed",
)


TEXT_TERMINAL_PUBLICATION_QUESTION = AnalysisQuestionSpec(
    question_id=TEXT_TERMINAL_PUBLICATION_QUESTION_ID,
    version=TEXT_TERMINAL_PUBLICATION_QUESTION_VERSION,
    subject_kinds=("transaction_scope",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "declared_owner_local_boundary",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "current_relational_closure_observed",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "legacy_and_running_counterevidence_partitioned",
            "decision",
            "counterevidence",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "process_death_recovery_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "terminal_text_publications_may_remain_owner_locally_closed",
        "a_failure_window_may_leave_terminal_text_state_relationally_open",
    ),
    counterevidence_rules=(
        "running_attempts_are_not_terminal_publications",
        "legacy_unattributed_documents_do_not_claim_v2_lineage",
        "closed_final_rows_do_not_demonstrate_restart_recovery",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_text_terminal_closure_observation",
            "characterization",
            "Inspect the exact closure partition without changing Text state.",
        ),
        AnalysisNextActionSpec(
            "preserve_running_and_legacy_partitions",
            "counterevidence_search",
            "Keep running attempts and legacy unattributed documents out of terminal claims.",
        ),
        AnalysisNextActionSpec(
            "terminate_after_text_begin_and_before_terminal_commit_then_restart",
            "experiment",
            "Terminate after the committed running attempt and before terminal commit, then restart and verify bounded reconciliation.",
        ),
    ),
)


class CodeStateTopologyResolutionError(ValueError):
    """The exact owner-local state cannot be resolved safely."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _text_tuple(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item, maximum=2_048) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


def _non_negative_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class TextTerminalPublicationClosure:
    running_attempts: int
    terminal_attempts: int
    receipts: int
    outbox_events: int
    attributed_documents: int
    legacy_unattributed_documents: int
    orphan_terminal_attempt_count: int
    orphan_terminal_attempt_ids: tuple[str, ...]
    orphan_terminal_attempts_truncated: bool
    orphan_receipt_count: int
    orphan_receipt_ids: tuple[str, ...]
    orphan_receipts_truncated: bool
    orphan_outbox_event_count: int
    orphan_outbox_event_ids: tuple[str, ...]
    orphan_outbox_events_truncated: bool
    open_attributed_document_count: int
    open_attributed_document_keys: tuple[str, ...]
    open_attributed_documents_truncated: bool
    relationally_closed: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("running attempts", self.running_attempts),
            ("terminal attempts", self.terminal_attempts),
            ("receipts", self.receipts),
            ("outbox events", self.outbox_events),
            ("attributed documents", self.attributed_documents),
            ("legacy unattributed documents", self.legacy_unattributed_documents),
            ("orphan terminal attempts", self.orphan_terminal_attempt_count),
            ("orphan receipts", self.orphan_receipt_count),
            ("orphan outbox events", self.orphan_outbox_event_count),
            ("open attributed documents", self.open_attributed_document_count),
        ):
            _non_negative_int(label, value)
        for label, values in (
            ("orphan terminal attempt", self.orphan_terminal_attempt_ids),
            ("orphan receipt", self.orphan_receipt_ids),
            ("orphan outbox event", self.orphan_outbox_event_ids),
            ("open attributed document", self.open_attributed_document_keys),
        ):
            _text_tuple(label, values)
        for count, values, truncated in (
            (
                self.orphan_terminal_attempt_count,
                self.orphan_terminal_attempt_ids,
                self.orphan_terminal_attempts_truncated,
            ),
            (self.orphan_receipt_count, self.orphan_receipt_ids, self.orphan_receipts_truncated),
            (
                self.orphan_outbox_event_count,
                self.orphan_outbox_event_ids,
                self.orphan_outbox_events_truncated,
            ),
            (
                self.open_attributed_document_count,
                self.open_attributed_document_keys,
                self.open_attributed_documents_truncated,
            ),
        ):
            if not isinstance(truncated, bool) or truncated != (
                count > TEXT_STATE_TOPOLOGY_EXAMPLE_LIMIT
            ):
                raise ValueError("Text terminal closure truncation is invalid")
            if len(values) != min(count, TEXT_STATE_TOPOLOGY_EXAMPLE_LIMIT):
                raise ValueError("Text terminal closure examples disagree with exact count")
        expected = not (
            self.orphan_terminal_attempt_count
            or self.orphan_receipt_count
            or self.orphan_outbox_event_count
            or self.open_attributed_document_count
        )
        if self.relationally_closed != expected:
            raise ValueError("Text terminal closure is not derived from exhaustive anti-joins")


@dataclass(frozen=True, slots=True)
class CodeStateTopologyAnalysis:
    analysis_id: str
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    source_version: str
    workflow_contract_schema: str
    workflow_id: str
    workflow_version: str
    boundary_id: str
    state_owner_id: str
    state_store_id: str
    database_name: str
    observed_schema_version: int | None
    snapshot_id: str | None
    snapshot_freshness: Literal["current", "unknown"]
    closure: TextTerminalPublicationClosure | None
    observation: Literal["relationally_closed", "relational_delta_observed"] | None
    inference_status: Literal["abstained"]
    decision_readiness: Literal["experiment_required", "abstained"]
    decision: None
    next_action_ids: tuple[str, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("state topology analysis id", self.analysis_id, maximum=256)
        _required_text("state topology source version", self.source_version, maximum=256)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("state topology status is invalid")
        if self.policy_id != TEXT_TERMINAL_PUBLICATION_POLICY:
            raise ValueError("state topology policy is invalid")
        if self.workflow_contract_schema != TEXT_DERIVATION_WORKFLOW.schema:
            raise ValueError("state topology workflow schema is invalid")
        if (
            self.workflow_id != TEXT_DERIVATION_WORKFLOW.workflow_id
            or self.workflow_version != TEXT_DERIVATION_WORKFLOW.version
            or self.boundary_id != _BOUNDARY_ID
        ):
            raise ValueError("state topology workflow boundary is invalid")
        boundary = TEXT_DERIVATION_WORKFLOW.boundary(self.boundary_id)
        store = boundary.state_store_id.removeprefix("sqlite:")
        if (
            self.state_owner_id != boundary.state_owner_id
            or self.state_store_id != boundary.state_store_id
            or self.database_name != store
        ):
            raise ValueError("state topology owner/store identity is invalid")
        if self.inference_status != "abstained" or self.decision is not None:
            raise ValueError("state topology cannot infer a defect or own a decision")
        if self.limitations != _LIMITATIONS:
            raise ValueError("state topology limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("state topology must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("state topology abstention reason", self.reason, maximum=256)
            if (
                self.observed_schema_version is not None
                or self.snapshot_id is not None
                or self.snapshot_freshness != "unknown"
                or self.closure is not None
                or self.observation is not None
                or self.next_action_ids
                or self.decision_readiness != "abstained"
            ):
                raise ValueError("abstained state topology cannot assert partial evidence")
        else:
            if self.reason is not None:
                raise ValueError("ready state topology cannot carry an abstention reason")
            if self.observed_schema_version != TEXT_SCHEMA_VERSION:
                raise ValueError("ready state topology requires current Text schema")
            _required_text("state topology snapshot", self.snapshot_id, maximum=2_048)
            if self.snapshot_freshness != "current" or self.closure is None:
                raise ValueError("ready state topology requires one current resolved closure")
            expected = (
                "relationally_closed"
                if self.closure.relationally_closed
                else "relational_delta_observed"
            )
            if self.observation != expected:
                raise ValueError("state topology observation is not derived from closure")
            if self.decision_readiness != "experiment_required":
                raise ValueError("state topology observation cannot become a change decision")
            expected_actions = tuple(
                item.action_id for item in TEXT_TERMINAL_PUBLICATION_QUESTION.next_actions
            )
            if self.next_action_ids != expected_actions:
                raise ValueError("state topology next actions are not canonical")
        expected_id = _analysis_id(
            {key: value for key, value in asdict(self).items() if key != "analysis_id"}
        )
        if self.analysis_id != expected_id:
            raise ValueError("state topology analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_STATE_TOPOLOGY_SCHEMA, **asdict(self)}


def _analysis_id(values: Mapping[str, object]) -> str:
    payload = dict(values)
    closure = payload.get("closure")
    if isinstance(closure, TextTerminalPublicationClosure):
        payload["closure"] = asdict(closure)
    return analysis_identity("code-state-topology-analysis-v1", payload)


def _base_values(source_version: str) -> dict[str, object]:
    boundary = TEXT_DERIVATION_WORKFLOW.boundary(_BOUNDARY_ID)
    return {
        "policy_id": TEXT_TERMINAL_PUBLICATION_POLICY,
        "source_version": _required_text("state topology source version", source_version),
        "workflow_contract_schema": TEXT_DERIVATION_WORKFLOW.schema,
        "workflow_id": TEXT_DERIVATION_WORKFLOW.workflow_id,
        "workflow_version": TEXT_DERIVATION_WORKFLOW.version,
        "boundary_id": boundary.boundary_id,
        "state_owner_id": boundary.state_owner_id,
        "state_store_id": boundary.state_store_id,
        "database_name": boundary.state_store_id.removeprefix("sqlite:"),
    }


def abstained_code_state_topology(
    reason: str,
    *,
    source_version: str,
) -> CodeStateTopologyAnalysis:
    values: dict[str, object] = {
        **_base_values(source_version),
        "status": "abstained",
        "reason": _required_text("state topology abstention reason", reason, maximum=256),
        "observed_schema_version": None,
        "snapshot_id": None,
        "snapshot_freshness": "unknown",
        "closure": None,
        "observation": None,
        "inference_status": "abstained",
        "decision_readiness": "abstained",
        "decision": None,
        "next_action_ids": (),
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeStateTopologyAnalysis(
        analysis_id=_analysis_id(values),
        **values,  # type: ignore[arg-type]
    )


def _count(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise CodeStateTopologyResolutionError("Text topology count is invalid")
    return int(row[0])


def _count_and_identifiers(
    connection: sqlite3.Connection,
    statement: str,
) -> tuple[int, tuple[str, ...], bool]:
    projection = statement.strip().rstrip(";")
    count = _count(connection, f"SELECT COUNT(*) FROM ({projection}) AS topology_rows")
    values = tuple(
        _required_text("Text topology identifier", row[0], maximum=2_048)
        for row in connection.execute(
            f"SELECT * FROM ({projection}) AS topology_rows LIMIT ?",
            (TEXT_STATE_TOPOLOGY_EXAMPLE_LIMIT,),
        )
    )
    return count, values, count > TEXT_STATE_TOPOLOGY_EXAMPLE_LIMIT


def _read_closure(connection: sqlite3.Connection) -> TextTerminalPublicationClosure:
    orphan_terminal_count, orphan_terminal_ids, orphan_terminal_truncated = _count_and_identifiers(
        connection,
        """SELECT a.attempt_id
            FROM text_derivation_attempts a
            LEFT JOIN text_work_receipts r
              ON r.attempt_id=a.attempt_id AND r.receipt_id=a.receipt_id
             AND r.outcome=a.status
            LEFT JOIN text_derivation_outbox o
              ON o.attempt_id=a.attempt_id AND o.receipt_id=a.receipt_id
             AND o.event_id='text-outbox:' || a.receipt_id
             AND o.event_type='text.work_' || a.status || '.v1'
             AND o.payload_json=r.receipt_json
            WHERE a.status<>'running' AND (r.receipt_id IS NULL OR o.sequence IS NULL)
            ORDER BY a.attempt_id""",
    )
    orphan_receipt_count, orphan_receipt_ids, orphan_receipt_truncated = _count_and_identifiers(
        connection,
        """SELECT r.receipt_id
            FROM text_work_receipts r
            LEFT JOIN text_derivation_attempts a
              ON a.attempt_id=r.attempt_id AND a.receipt_id=r.receipt_id
             AND a.status=r.outcome AND a.status<>'running'
            LEFT JOIN text_derivation_outbox o
              ON o.attempt_id=r.attempt_id AND o.receipt_id=r.receipt_id
             AND o.event_id='text-outbox:' || r.receipt_id
             AND o.event_type='text.work_' || r.outcome || '.v1'
             AND o.payload_json=r.receipt_json
            WHERE a.attempt_id IS NULL OR o.sequence IS NULL
            ORDER BY r.receipt_id""",
    )
    orphan_outbox_count, orphan_outbox_ids, orphan_outbox_truncated = _count_and_identifiers(
        connection,
        """SELECT o.event_id
            FROM text_derivation_outbox o
            LEFT JOIN text_work_receipts r
              ON r.receipt_id=o.receipt_id AND r.attempt_id=o.attempt_id
             AND o.event_id='text-outbox:' || r.receipt_id
             AND o.event_type='text.work_' || r.outcome || '.v1'
             AND o.payload_json=r.receipt_json
            LEFT JOIN text_derivation_attempts a
              ON a.attempt_id=o.attempt_id AND a.receipt_id=o.receipt_id
             AND a.status=r.outcome AND a.status<>'running'
            WHERE r.receipt_id IS NULL OR a.attempt_id IS NULL
            ORDER BY o.event_id""",
    )
    open_document_count, open_document_keys, open_document_truncated = _count_and_identifiers(
        connection,
        """SELECT d.file_key
            FROM documents d
            LEFT JOIN text_input_revisions ir ON ir.revision_id=d.revision_id
            LEFT JOIN text_materialization_heads representation
              ON representation.resource_id=ir.resource_id
             AND representation.revision_id=ir.revision_id
             AND representation.materialization_kind='text_representation'
            LEFT JOIN text_materialization_heads fts
              ON fts.resource_id=ir.resource_id
             AND fts.revision_id=ir.revision_id
             AND fts.materialization_kind='text_fts'
            LEFT JOIN text_materializations rm
              ON rm.owner=representation.materialization_owner
             AND rm.materialization_id=representation.materialization_id
             AND rm.revision_id=ir.revision_id
             AND rm.producer_receipt_id=representation.producer_receipt_id
            LEFT JOIN text_materializations fm
              ON fm.owner=fts.materialization_owner
             AND fm.materialization_id=fts.materialization_id
             AND fm.revision_id=ir.revision_id
             AND fm.producer_receipt_id=fts.producer_receipt_id
            LEFT JOIN text_work_receipts rr
              ON rr.receipt_id=representation.producer_receipt_id
             AND rr.outcome='succeeded'
            LEFT JOIN text_work_receipts fr
              ON fr.receipt_id=fts.producer_receipt_id
             AND fr.outcome='succeeded'
            LEFT JOIN text_derivation_output_bindings rob
              ON rob.attempt_id=rr.attempt_id
             AND rob.binding_name='text_representation'
             AND rob.materialization_owner=rm.owner
             AND rob.materialization_id=rm.materialization_id
            LEFT JOIN text_derivation_output_bindings fob
              ON fob.attempt_id=fr.attempt_id
             AND fob.binding_name='text_fts'
             AND fob.materialization_owner=fm.owner
             AND fob.materialization_id=fm.materialization_id
            WHERE d.revision_id IS NOT NULL AND (
                ir.revision_id IS NULL OR d.status NOT IN ('complete','error')
                OR (d.status='complete' AND (
                    (SELECT COUNT(*) FROM document_fts df
                        WHERE df.file_key=d.file_key)<>1
                    OR representation.materialization_id IS NULL
                    OR representation.materialization_owner<>'text'
                    OR rm.materialization_id IS NULL OR rm.kind<>'text_representation'
                    OR rr.receipt_id IS NULL OR rob.attempt_id IS NULL
                    OR fts.materialization_id IS NULL
                    OR fts.materialization_owner<>'text'
                    OR fm.materialization_id IS NULL OR fm.kind<>'text_fts'
                    OR fr.receipt_id IS NULL OR fob.attempt_id IS NULL
                    OR (SELECT COUNT(*) FROM text_materialization_heads h
                        WHERE h.resource_id=ir.resource_id
                          AND h.materialization_owner='text')<>2
                ))
                OR (d.status='error' AND (
                    EXISTS(SELECT 1 FROM document_fts df WHERE df.file_key=d.file_key)
                    OR EXISTS(SELECT 1 FROM text_materialization_heads h
                        WHERE h.resource_id=ir.resource_id
                          AND h.materialization_owner='text')
                    OR NOT EXISTS(
                        SELECT 1 FROM text_derivation_attempts failure_attempt
                        JOIN text_derivation_input_bindings failure_input
                          ON failure_input.attempt_id=failure_attempt.attempt_id
                        JOIN text_work_receipts failure_receipt
                          ON failure_receipt.receipt_id=failure_attempt.receipt_id
                         AND failure_receipt.attempt_id=failure_attempt.attempt_id
                         AND failure_receipt.outcome='failed'
                        WHERE failure_attempt.stage_id='text.extract'
                          AND failure_attempt.processing_signature=d.processing_signature
                          AND failure_attempt.status='failed'
                          AND failure_input.revision_id=d.revision_id
                    )
                ))
            ) ORDER BY d.file_key""",
    )
    values = {
        "running_attempts": _count(
            connection,
            "SELECT COUNT(*) FROM text_derivation_attempts WHERE status='running'",
        ),
        "terminal_attempts": _count(
            connection,
            "SELECT COUNT(*) FROM text_derivation_attempts WHERE status<>'running'",
        ),
        "receipts": _count(connection, "SELECT COUNT(*) FROM text_work_receipts"),
        "outbox_events": _count(connection, "SELECT COUNT(*) FROM text_derivation_outbox"),
        "attributed_documents": _count(
            connection,
            "SELECT COUNT(*) FROM documents WHERE revision_id IS NOT NULL",
        ),
        "legacy_unattributed_documents": _count(
            connection,
            "SELECT COUNT(*) FROM documents WHERE revision_id IS NULL",
        ),
        "orphan_terminal_attempt_count": orphan_terminal_count,
        "orphan_terminal_attempt_ids": orphan_terminal_ids,
        "orphan_terminal_attempts_truncated": orphan_terminal_truncated,
        "orphan_receipt_count": orphan_receipt_count,
        "orphan_receipt_ids": orphan_receipt_ids,
        "orphan_receipts_truncated": orphan_receipt_truncated,
        "orphan_outbox_event_count": orphan_outbox_count,
        "orphan_outbox_event_ids": orphan_outbox_ids,
        "orphan_outbox_events_truncated": orphan_outbox_truncated,
        "open_attributed_document_count": open_document_count,
        "open_attributed_document_keys": open_document_keys,
        "open_attributed_documents_truncated": open_document_truncated,
    }
    relationally_closed = not any(
        values[key]
        for key in (
            "orphan_terminal_attempt_count",
            "orphan_receipt_count",
            "orphan_outbox_event_count",
            "open_attributed_document_count",
        )
    )
    return TextTerminalPublicationClosure(
        relationally_closed=relationally_closed,
        **values,  # type: ignore[arg-type]
    )


def analyze_text_terminal_publication(
    state_directory: Path,
    *,
    source_version: str,
) -> CodeStateTopologyAnalysis:
    """Resolve current Text closure without creating or touching SQLite sidecars."""

    base = _base_values(source_version)
    path = Path(state_directory) / str(base["database_name"])
    try:
        with immutable_sqlite_database(path) as connection:
            observed_schema = read_application_schema_version(connection, label="text")
            if observed_schema != TEXT_SCHEMA_VERSION:
                raise CodeStateTopologyResolutionError("Text schema is not current")
            validate_sqlite_schema_contract(
                connection,
                text_schema_contract(),
                label="text",
                exact=True,
            )
            closure = _read_closure(connection)
    except (
        ImmutableSQLiteUnavailable,
        OSError,
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return abstained_code_state_topology(
            f"text_terminal_publication_unresolvable:{type(exc).__name__}",
            source_version=source_version,
        )
    source_projection = {
        **base,
        "observed_schema_version": observed_schema,
        "closure": asdict(closure),
    }
    snapshot_id = analysis_identity("text-state-topology-snapshot-v1", source_projection)
    observation: Literal["relationally_closed", "relational_delta_observed"] = (
        "relationally_closed" if closure.relationally_closed else "relational_delta_observed"
    )
    next_actions = tuple(item.action_id for item in TEXT_TERMINAL_PUBLICATION_QUESTION.next_actions)
    values: dict[str, object] = {
        **base,
        "status": "ready",
        "reason": None,
        "observed_schema_version": observed_schema,
        "snapshot_id": snapshot_id,
        "snapshot_freshness": "current",
        "closure": closure,
        "observation": observation,
        "inference_status": "abstained",
        "decision_readiness": "experiment_required",
        "decision": None,
        "next_action_ids": next_actions,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeStateTopologyAnalysis(
        analysis_id=_analysis_id(values),
        **values,  # type: ignore[arg-type]
    )


def _contract_evidence(analysis: CodeStateTopologyAnalysis) -> AnalysisEvidenceRef:
    assert analysis.snapshot_id is not None
    boundary = TEXT_DERIVATION_WORKFLOW.boundary(analysis.boundary_id)
    projection = boundary.as_payload()
    projection_digest = analysis_identity("durable-boundary-projection-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "durable-boundary-evidence-v1",
            {"snapshot_id": analysis.snapshot_id, "projection": projection_digest},
        ),
        subject_key=analysis.boundary_id,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="knowledge",
        producer_id="state_topology_contracts",
        producer_version=analysis.workflow_version,
        source_schema=analysis.workflow_contract_schema,
        source_record_kind="durable_transaction_boundary",
        source_record_id=analysis.boundary_id,
        source_projection_digest=projection_digest,
        snapshot_id=analysis.snapshot_id,
        revision_id=analysis.workflow_version,
        facts=(
            AnalysisFact("state_owner_id", boundary.state_owner_id),
            AnalysisFact("state_store_id", boundary.state_store_id),
            AnalysisFact("begin_authority", boundary.begin_authority),
            AnalysisFact("commit_authority", boundary.commit_authority),
            AnalysisFact("transaction_scope", boundary.transaction_scope),
            AnalysisFact("required_read_table_count", len(boundary.required_read_tables)),
            AnalysisFact("required_write_table_count", len(boundary.required_write_tables)),
            AnalysisFact("conditional_write_table_count", len(boundary.conditional_write_tables)),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="public-durable-workflow-contract-resolver",
        resolver_version="v1",
        limitations=("declared_contract_is_not_runtime_transaction_trace",),
    )


def _closure_evidence(analysis: CodeStateTopologyAnalysis) -> AnalysisEvidenceRef:
    assert analysis.snapshot_id is not None and analysis.closure is not None
    projection = asdict(analysis.closure)
    projection_digest = analysis_identity("text-terminal-closure-projection-v1", projection)
    closure = analysis.closure
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "text-terminal-closure-evidence-v1",
            {"snapshot_id": analysis.snapshot_id, "projection": projection_digest},
        ),
        subject_key=analysis.boundary_id,
        role="supporting",
        evidence_kind="internal_relation",
        source_owner_id=analysis.state_owner_id,
        producer_id=_RESOLVER_ID,
        producer_version=_RESOLVER_VERSION,
        source_schema=f"neocortex.text-state/sqlite-v{TEXT_SCHEMA_VERSION}",
        source_record_kind=_SOURCE_RECORD_KIND,
        source_record_id=analysis.analysis_id,
        source_projection_digest=projection_digest,
        snapshot_id=analysis.snapshot_id,
        revision_id=analysis.workflow_version,
        facts=(
            AnalysisFact("terminal_attempts", closure.terminal_attempts),
            AnalysisFact("receipts", closure.receipts),
            AnalysisFact("outbox_events", closure.outbox_events),
            AnalysisFact("attributed_documents", closure.attributed_documents),
            AnalysisFact("orphan_terminal_attempts", closure.orphan_terminal_attempt_count),
            AnalysisFact("orphan_receipts", closure.orphan_receipt_count),
            AnalysisFact("orphan_outbox_events", closure.orphan_outbox_event_count),
            AnalysisFact("open_attributed_documents", closure.open_attributed_document_count),
            AnalysisFact("relationally_closed", closure.relationally_closed),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id=_RESOLVER_ID,
        resolver_version=_RESOLVER_VERSION,
        limitations=("current_final_state_only",),
    )


def _partition_evidence(analysis: CodeStateTopologyAnalysis) -> AnalysisEvidenceRef:
    assert analysis.snapshot_id is not None and analysis.closure is not None
    closure = analysis.closure
    projection = {
        "running_attempts": closure.running_attempts,
        "legacy_unattributed_documents": closure.legacy_unattributed_documents,
    }
    projection_digest = analysis_identity("text-topology-partition-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "text-topology-partition-evidence-v1",
            {"snapshot_id": analysis.snapshot_id, "projection": projection_digest},
        ),
        subject_key=analysis.boundary_id,
        role="counterevidence",
        evidence_kind="internal_fact",
        source_owner_id=analysis.state_owner_id,
        producer_id=_RESOLVER_ID,
        producer_version=_RESOLVER_VERSION,
        source_schema=f"neocortex.text-state/sqlite-v{TEXT_SCHEMA_VERSION}",
        source_record_kind="non_terminal_partition",
        source_record_id=analysis.analysis_id,
        source_projection_digest=projection_digest,
        snapshot_id=analysis.snapshot_id,
        revision_id=analysis.workflow_version,
        facts=(
            AnalysisFact("running_attempts", closure.running_attempts),
            AnalysisFact(
                "legacy_unattributed_documents",
                closure.legacy_unattributed_documents,
            ),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id=_RESOLVER_ID,
        resolver_version=_RESOLVER_VERSION,
        limitations=("partitions_are_not_terminal_publication_claims",),
    )


def state_topology_questions(
    analysis: CodeStateTopologyAnalysis,
    *,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Project a ready resolved analysis into the generic v15 question contract."""

    if analysis.status != "ready" or analysis.closure is None or analysis.snapshot_id is None:
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "state-topology-question-evaluation-v1",
                {
                    "analysis_id": analysis.analysis_id,
                    "rank": rank,
                    "question_spec": analysis_question_spec_fingerprint(
                        TEXT_TERMINAL_PUBLICATION_QUESTION
                    ),
                },
            ),
            question_id=TEXT_TERMINAL_PUBLICATION_QUESTION.question_id,
            question_version=TEXT_TERMINAL_PUBLICATION_QUESTION.version,
            question_spec_fingerprint=analysis_question_spec_fingerprint(
                TEXT_TERMINAL_PUBLICATION_QUESTION
            ),
            rank=rank,
            subject=AnalysisSubjectRef(
                subject_kind="transaction_scope",
                subject_key=analysis.boundary_id,
                display_name="Text terminal publication transaction boundary",
                source_owner_id=analysis.state_owner_id,
                snapshot_id=analysis.analysis_id,
                snapshot_freshness="unknown",
                revision_id=analysis.workflow_version,
            ),
            evidence=(),
            requirements=(
                AnalysisRequirementEvaluation(
                    "declared_owner_local_boundary",
                    "missing",
                    (),
                    "state_topology_resolution_abstained",
                ),
                AnalysisRequirementEvaluation(
                    "current_relational_closure_observed",
                    "missing",
                    (),
                    "state_topology_resolution_abstained",
                ),
                AnalysisRequirementEvaluation(
                    "legacy_and_running_counterevidence_partitioned",
                    "not_evaluated",
                    (),
                    "state_topology_resolution_abstained",
                ),
                AnalysisRequirementEvaluation(
                    "process_death_recovery_experiment_result",
                    "missing",
                    (),
                    "no_linked_process_death_recovery_result",
                ),
            ),
            observation_status="abstained",
            inference_status="abstained",
            inferences=(),
            hypotheses=TEXT_TERMINAL_PUBLICATION_QUESTION.hypotheses,
            question_readiness="abstained",
            decision_readiness="abstained",
            decision=None,
            decision_reason="question_evidence_incomplete",
            counterevidence_status="not_evaluated",
            next_action_ids=(),
            limitations=(*_LIMITATIONS, "state_topology_resolution_abstained"),
        )
        validate_analysis_question_evaluation(TEXT_TERMINAL_PUBLICATION_QUESTION, evaluation)
        return (TEXT_TERMINAL_PUBLICATION_QUESTION,), (evaluation,)
    contract = _contract_evidence(analysis)
    closure = _closure_evidence(analysis)
    partition = _partition_evidence(analysis)
    evidence = (contract, closure, partition)
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "state-topology-question-evaluation-v1",
            {
                "analysis_id": analysis.analysis_id,
                "rank": rank,
                "evidence_ids": tuple(item.evidence_id for item in evidence),
                "question_spec": analysis_question_spec_fingerprint(
                    TEXT_TERMINAL_PUBLICATION_QUESTION
                ),
            },
        ),
        question_id=TEXT_TERMINAL_PUBLICATION_QUESTION.question_id,
        question_version=TEXT_TERMINAL_PUBLICATION_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(
            TEXT_TERMINAL_PUBLICATION_QUESTION
        ),
        rank=rank,
        subject=AnalysisSubjectRef(
            subject_kind="transaction_scope",
            subject_key=analysis.boundary_id,
            display_name="Text terminal publication transaction boundary",
            source_owner_id=analysis.state_owner_id,
            snapshot_id=analysis.snapshot_id,
            snapshot_freshness="current",
            revision_id=analysis.workflow_version,
        ),
        evidence=evidence,
        requirements=(
            AnalysisRequirementEvaluation(
                "declared_owner_local_boundary",
                "satisfied",
                (contract.evidence_id,),
                "public_versioned_boundary_contract_resolved",
            ),
            AnalysisRequirementEvaluation(
                "current_relational_closure_observed",
                "satisfied",
                (closure.evidence_id,),
                "exact_complete_text_anti_joins_resolved",
            ),
            AnalysisRequirementEvaluation(
                "legacy_and_running_counterevidence_partitioned",
                "satisfied",
                (partition.evidence_id,),
                "non_terminal_and_unattributed_rows_partitioned",
            ),
            AnalysisRequirementEvaluation(
                "process_death_recovery_experiment_result",
                "missing",
                (),
                "no_linked_process_death_recovery_result",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=TEXT_TERMINAL_PUBLICATION_QUESTION.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="evaluated",
        next_action_ids=tuple(
            item.action_id for item in TEXT_TERMINAL_PUBLICATION_QUESTION.next_actions
        ),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(TEXT_TERMINAL_PUBLICATION_QUESTION, evaluation)
    return (TEXT_TERMINAL_PUBLICATION_QUESTION,), (evaluation,)


def resolve_state_topology_questions(
    state_directory: Path,
    analysis: CodeStateTopologyAnalysis,
    *,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Re-resolve every aggregate source pointer before projecting questions."""

    resolved = analyze_text_terminal_publication(
        state_directory,
        source_version=analysis.source_version,
    )
    if resolved != analysis:
        raise CodeStateTopologyResolutionError(
            "state topology projection disagrees with its current source records"
        )
    return state_topology_questions(resolved, rank=rank)


def parse_code_state_topology_payload(
    payload: Mapping[str, object],
) -> CodeStateTopologyAnalysis:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_STATE_TOPOLOGY_SCHEMA:
        raise ValueError("state topology payload schema is invalid")
    expected = {field.name for field in fields(CodeStateTopologyAnalysis)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("state topology payload fields are invalid")
    values = {key: value for key, value in payload.items() if key != "schema"}
    raw_closure = values.get("closure")
    if raw_closure is not None:
        closure_fields = {field.name for field in fields(TextTerminalPublicationClosure)}
        if not isinstance(raw_closure, Mapping) or set(raw_closure) != closure_fields:
            raise ValueError("state topology closure fields are invalid")
        closure_values = dict(raw_closure)
        for key in (
            "orphan_terminal_attempt_ids",
            "orphan_receipt_ids",
            "orphan_outbox_event_ids",
            "open_attributed_document_keys",
        ):
            closure_values[key] = _text_tuple(f"state topology {key}", closure_values[key])
        values["closure"] = TextTerminalPublicationClosure(**closure_values)
    values["next_action_ids"] = _text_tuple("state topology next action", values["next_action_ids"])
    values["limitations"] = _text_tuple("state topology limitation", values["limitations"])
    return CodeStateTopologyAnalysis(**values)  # type: ignore[arg-type]


__all__ = [
    "CODE_STATE_TOPOLOGY_SCHEMA",
    "TEXT_TERMINAL_PUBLICATION_POLICY",
    "TEXT_TERMINAL_PUBLICATION_QUESTION",
    "TEXT_TERMINAL_PUBLICATION_QUESTION_ID",
    "TEXT_TERMINAL_PUBLICATION_QUESTION_VERSION",
    "CodeStateTopologyAnalysis",
    "CodeStateTopologyResolutionError",
    "TextTerminalPublicationClosure",
    "abstained_code_state_topology",
    "analyze_text_terminal_publication",
    "parse_code_state_topology_payload",
    "resolve_state_topology_questions",
    "state_topology_questions",
]
