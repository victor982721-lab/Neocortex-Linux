"""Append-only Code experiment receipts and their epistemic projection.

The Code database owns machine-produced experiment receipts.  A receipt is
written only after an allow-listed experiment has terminated, is tied to the
completed analysis publication and exact proposal that selected it, and never
owns a human decision or mutation authority.

This module deliberately keeps two boundaries separate:

* storage verifies the immutable receipt and its publication context;
* projection maps only explicitly registered, passed gate outcomes to the
  evidence requirements they actually measure.

Unregistered gates, stale proposals and failed/abstained receipts cannot make a
question decision-ready.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Mapping, cast

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisFact,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    analysis_identity,
    validate_analysis_question_evaluation,
    validate_analysis_question_set,
)
from .code_experiment_executor import (
    CODE_EXPERIMENT_RECEIPT_SCHEMA,
    CodeExperimentReceipt,
    parse_code_experiment_receipt_payload,
)
from .code_experiment_planner import (
    CodeExperimentPlan,
    CodeExperimentProposal,
    experiment_evaluation_binding_fingerprint,
    plan_code_experiments,
)
from .code_schema import (
    checkpoint_code_wal,
    connect_code_state,
    readonly_code_database,
    remove_checkpointed_code_sidecars,
    validate_code_schema,
)
from .semantic_models import canonical_json, fingerprint_text

CODE_EXPERIMENT_STORE_SCHEMA = "neocortex.code-experiment-store/v1"
CODE_EXPERIMENT_STORE_MAX_PAYLOAD_BYTES = 1_048_576
CODE_EXPERIMENT_STORE_MAX_RECEIPTS_PER_PROPOSAL = 32
CODE_EXPERIMENT_STORE_MAX_RESOLVED = 256


class CodeExperimentStoreError(RuntimeError):
    """A durable experiment receipt is missing, stale or contradictory."""


def _required(label: str, value: object, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def code_review_digest_identity(digest: object) -> str:
    """Encode one collision-guarded Code-review digest without importing models."""

    xxh3_128 = _required("Code-review digest", getattr(digest, "xxh3_128", None), 64)
    guard = _required("Code-review guard digest", getattr(digest, "xxh3_64_guard", None), 64)
    byte_count = getattr(digest, "byte_count", None)
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 1:
        raise ValueError("Code-review digest byte count is invalid")
    return f"xxh3_128:{xxh3_128}:xxh3_64:{guard}:bytes:{byte_count}"


def _experiment_envelope_digest(
    *,
    receipt_id: object,
    analysis_run_id: object,
    source_evaluation_id: object,
    question_id: object,
    subject_key: object,
    proposal_id: object,
    template_id: object,
    template_version: object,
    source_processing_signature: object,
    review_digest: object,
    receipt_schema: object,
    receipt_status: object,
    payload_xxh3_128: object,
    payload_xxh3_64_guard: object,
    payload_bytes: object,
    recorded_ns: object,
    authority: object,
    mutation_authority: object,
) -> str:
    return analysis_identity(
        "code-experiment-envelope-v1",
        {
            "schema": CODE_EXPERIMENT_STORE_SCHEMA,
            "receipt_id": receipt_id,
            "analysis_run_id": analysis_run_id,
            "source_evaluation_id": source_evaluation_id,
            "question_id": question_id,
            "subject_key": subject_key,
            "proposal_id": proposal_id,
            "template_id": template_id,
            "template_version": template_version,
            "source_processing_signature": source_processing_signature,
            "review_digest": review_digest,
            "receipt_schema": receipt_schema,
            "receipt_status": receipt_status,
            "payload_xxh3_128": payload_xxh3_128,
            "payload_xxh3_64_guard": payload_xxh3_64_guard,
            "payload_bytes": payload_bytes,
            "recorded_ns": recorded_ns,
            "authority": authority,
            "mutation_authority": mutation_authority,
        },
    )


@dataclass(frozen=True, slots=True)
class ResolvedCodeExperimentReceipt:
    analysis_run_id: int
    source_evaluation_id: str
    question_id: str
    subject_key: str
    review_digest: str
    recorded_ns: int
    payload_xxh3_128: str
    payload_xxh3_64_guard: str
    payload_bytes: int
    receipt: CodeExperimentReceipt

    def __post_init__(self) -> None:
        if (
            isinstance(self.analysis_run_id, bool)
            or not isinstance(self.analysis_run_id, int)
            or self.analysis_run_id < 1
        ):
            raise ValueError("experiment receipt analysis run is invalid")
        for label, value, maximum in (
            ("experiment source evaluation", self.source_evaluation_id, 1_024),
            ("experiment question", self.question_id, 256),
            ("experiment subject", self.subject_key, 1_024),
            ("experiment review digest", self.review_digest, 256),
            ("experiment payload digest", self.payload_xxh3_128, 64),
            ("experiment payload guard", self.payload_xxh3_64_guard, 64),
        ):
            _required(label, value, maximum)
        if (
            isinstance(self.recorded_ns, bool)
            or not isinstance(self.recorded_ns, int)
            or self.recorded_ns < 1
            or isinstance(self.payload_bytes, bool)
            or not isinstance(self.payload_bytes, int)
            or not 1 <= self.payload_bytes <= CODE_EXPERIMENT_STORE_MAX_PAYLOAD_BYTES
        ):
            raise ValueError("experiment receipt storage metadata is invalid")
        if not isinstance(self.receipt, CodeExperimentReceipt):
            raise ValueError("experiment receipt payload is invalid")
        payload = canonical_json(self.receipt.as_payload())
        fingerprint = fingerprint_text(payload)
        if (
            self.payload_xxh3_128,
            self.payload_xxh3_64_guard,
            self.payload_bytes,
        ) != (fingerprint.xxh3_128, fingerprint.xxh3_64_guard, fingerprint.byte_count):
            raise ValueError("experiment receipt payload disagrees with its durable digest")

    @property
    def envelope_digest(self) -> str:
        """Bind the receipt payload digests to its complete storage context."""

        receipt = self.receipt
        return _experiment_envelope_digest(
            receipt_id=receipt.receipt_id,
            analysis_run_id=self.analysis_run_id,
            source_evaluation_id=self.source_evaluation_id,
            question_id=self.question_id,
            subject_key=self.subject_key,
            proposal_id=receipt.proposal_id,
            template_id=receipt.template_id,
            template_version=receipt.template_version,
            source_processing_signature=receipt.source_version,
            review_digest=self.review_digest,
            receipt_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA,
            receipt_status=receipt.status,
            payload_xxh3_128=self.payload_xxh3_128,
            payload_xxh3_64_guard=self.payload_xxh3_64_guard,
            payload_bytes=self.payload_bytes,
            recorded_ns=self.recorded_ns,
            authority="advisory",
            mutation_authority=0,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": CODE_EXPERIMENT_STORE_SCHEMA,
            "analysis_run_id": self.analysis_run_id,
            "source_evaluation_id": self.source_evaluation_id,
            "question_id": self.question_id,
            "subject_key": self.subject_key,
            "review_digest": self.review_digest,
            "recorded_ns": self.recorded_ns,
            "payload_xxh3_128": self.payload_xxh3_128,
            "payload_xxh3_64_guard": self.payload_xxh3_64_guard,
            "payload_bytes": self.payload_bytes,
            "envelope_digest": self.envelope_digest,
            "receipt": self.receipt.as_payload(),
        }


def _resolved(
    *,
    analysis_run_id: int,
    source_evaluation_id: str,
    question_id: str,
    subject_key: str,
    review_digest: str,
    recorded_ns: int,
    payload: str,
    payload_xxh3_128: str,
    payload_xxh3_64_guard: str,
    payload_bytes: int,
) -> ResolvedCodeExperimentReceipt:
    if not isinstance(payload, str):
        raise CodeExperimentStoreError("stored_experiment_receipt_payload_invalid")
    try:
        payload_bytes_observed = len(payload.encode("utf-8"))
    except UnicodeError as exc:
        raise CodeExperimentStoreError("stored_experiment_receipt_payload_invalid") from exc
    if not 1 <= payload_bytes_observed <= CODE_EXPERIMENT_STORE_MAX_PAYLOAD_BYTES:
        raise CodeExperimentStoreError("stored_experiment_receipt_payload_invalid")
    try:
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("receipt wire is not an object")
        if canonical_json(parsed) != payload:
            raise ValueError("receipt wire is not canonical")
        receipt = parse_code_experiment_receipt_payload(parsed)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodeExperimentStoreError("stored_experiment_receipt_payload_invalid") from exc
    try:
        return ResolvedCodeExperimentReceipt(
            analysis_run_id=analysis_run_id,
            source_evaluation_id=source_evaluation_id,
            question_id=question_id,
            subject_key=subject_key,
            review_digest=review_digest,
            recorded_ns=recorded_ns,
            payload_xxh3_128=payload_xxh3_128,
            payload_xxh3_64_guard=payload_xxh3_64_guard,
            payload_bytes=payload_bytes,
            receipt=receipt,
        )
    except ValueError as exc:
        raise CodeExperimentStoreError("stored_experiment_receipt_digest_invalid") from exc


def _resolved_from_row(
    row: sqlite3.Row | Mapping[str, object],
) -> ResolvedCodeExperimentReceipt:
    """Decode one exact durable row and reject denormalized-context drift."""

    resolved = _resolved(
        analysis_run_id=cast(int, row["analysis_run_id"]),
        source_evaluation_id=cast(str, row["source_evaluation_id"]),
        question_id=cast(str, row["question_id"]),
        subject_key=cast(str, row["subject_key"]),
        review_digest=cast(str, row["review_digest"]),
        recorded_ns=cast(int, row["recorded_ns"]),
        payload=cast(str, row["payload_json"]),
        payload_xxh3_128=cast(str, row["payload_xxh3_128"]),
        payload_xxh3_64_guard=cast(str, row["payload_xxh3_64_guard"]),
        payload_bytes=cast(int, row["payload_bytes"]),
    )
    receipt = resolved.receipt
    observed_envelope_digest = _experiment_envelope_digest(
        receipt_id=row["receipt_id"],
        analysis_run_id=row["analysis_run_id"],
        source_evaluation_id=row["source_evaluation_id"],
        question_id=row["question_id"],
        subject_key=row["subject_key"],
        proposal_id=row["proposal_id"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        source_processing_signature=row["source_processing_signature"],
        review_digest=row["review_digest"],
        receipt_schema=row["receipt_schema"],
        receipt_status=row["receipt_status"],
        payload_xxh3_128=row["payload_xxh3_128"],
        payload_xxh3_64_guard=row["payload_xxh3_64_guard"],
        payload_bytes=row["payload_bytes"],
        recorded_ns=row["recorded_ns"],
        authority=row["authority"],
        mutation_authority=row["mutation_authority"],
    )
    if row["envelope_digest"] != observed_envelope_digest:
        raise CodeExperimentStoreError("stored_experiment_receipt_envelope_invalid")
    observed = (
        row["receipt_id"],
        row["analysis_run_id"],
        row["source_evaluation_id"],
        row["question_id"],
        row["subject_key"],
        row["proposal_id"],
        row["template_id"],
        row["template_version"],
        row["source_processing_signature"],
        row["receipt_schema"],
        row["receipt_status"],
        row["authority"],
        row["mutation_authority"],
    )
    expected = (
        receipt.receipt_id,
        resolved.analysis_run_id,
        resolved.source_evaluation_id,
        resolved.question_id,
        resolved.subject_key,
        receipt.proposal_id,
        receipt.template_id,
        receipt.template_version,
        receipt.source_version,
        CODE_EXPERIMENT_RECEIPT_SCHEMA,
        receipt.status,
        "advisory",
        0,
    )
    if observed != expected or isinstance(row["mutation_authority"], bool):
        raise CodeExperimentStoreError("stored_experiment_receipt_context_invalid")
    return resolved


def record_code_experiment_receipt(
    database: Path,
    receipt: CodeExperimentReceipt,
    proposal: CodeExperimentProposal,
    *,
    analysis_run_id: int,
    processing_signature: str,
    review_digest: str,
    recorded_ns: int | None = None,
) -> ResolvedCodeExperimentReceipt:
    """Append one receipt after verifying its completed Code publication context."""

    if not isinstance(receipt, CodeExperimentReceipt):
        raise TypeError("Code experiment receipt must be typed")
    if not isinstance(proposal, CodeExperimentProposal):
        raise TypeError("Code experiment proposal must be typed")
    if (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("experiment receipt analysis run is invalid")
    _required("experiment processing signature", processing_signature, 2_048)
    _required("experiment review digest", review_digest, 256)
    if (
        receipt.proposal_id != proposal.proposal_id
        or receipt.template_id != proposal.template_id
        or receipt.template_version != proposal.template_version
        or receipt.source_version != processing_signature
        or proposal.evaluation_id.strip() != proposal.evaluation_id
    ):
        raise ValueError("experiment receipt is not bound to its selected proposal")
    if proposal.planning_status != "planned" or proposal.runner_kind == "none":
        raise ValueError("experiment receipt requires an executable registered proposal")
    if receipt.status not in {"passed", "failed", "abstained"}:
        raise ValueError("experiment receipt has an invalid terminal status")
    if recorded_ns is not None and (
        isinstance(recorded_ns, bool) or not isinstance(recorded_ns, int) or recorded_ns < 1
    ):
        raise ValueError("experiment receipt timestamp is invalid")
    payload = canonical_json(receipt.as_payload())
    fingerprint = fingerprint_text(payload)
    if not 1 <= fingerprint.byte_count <= CODE_EXPERIMENT_STORE_MAX_PAYLOAD_BYTES:
        raise ValueError("experiment receipt payload exceeds its durable bound")

    selected = Path(database)
    connection = connect_code_state(selected, create=False)
    try:
        validate_code_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """SELECT status,processing_signature FROM analysis_runs
            WHERE analysis_run_id=?""",
            (analysis_run_id,),
        ).fetchone()
        if run is None or str(run["status"]) != "completed":
            raise CodeExperimentStoreError("experiment_source_analysis_run_not_completed")
        if str(run["processing_signature"]) != processing_signature:
            raise CodeExperimentStoreError("experiment_source_processing_signature_changed")
        latest = connection.execute("SELECT MAX(analysis_run_id) FROM analysis_runs").fetchone()
        if latest is None or int(latest[0]) != analysis_run_id:
            raise CodeExperimentStoreError("experiment_source_analysis_run_not_latest")
        prior = connection.execute(
            """SELECT * FROM code_experiment_receipts WHERE receipt_id=?""",
            (receipt.receipt_id,),
        ).fetchone()
        if prior is None:
            ordering = connection.execute(
                """SELECT COUNT(*),MAX(recorded_ns) FROM code_experiment_receipts
                WHERE proposal_id=?""",
                (proposal.proposal_id,),
            ).fetchone()
            if ordering is None:
                raise CodeExperimentStoreError("experiment_receipt_order_unresolvable")
            count = int(ordering[0])
            if count >= CODE_EXPERIMENT_STORE_MAX_RECEIPTS_PER_PROPOSAL:
                raise CodeExperimentStoreError("experiment_receipt_proposal_bound_exceeded")
            observed_ns = time.time_ns() if recorded_ns is None else recorded_ns
            prior_recorded_ns = None if ordering[1] is None else int(ordering[1])
            if prior_recorded_ns is not None and observed_ns <= prior_recorded_ns:
                raise CodeExperimentStoreError("experiment_receipt_order_not_monotonic")
            resolved = ResolvedCodeExperimentReceipt(
                analysis_run_id,
                proposal.evaluation_id,
                proposal.question_id,
                proposal.subject_key,
                review_digest,
                observed_ns,
                fingerprint.xxh3_128,
                fingerprint.xxh3_64_guard,
                fingerprint.byte_count,
                receipt,
            )
            connection.execute(
                """INSERT INTO code_experiment_receipts(
                receipt_id,analysis_run_id,source_evaluation_id,question_id,subject_key,
                proposal_id,template_id,template_version,source_processing_signature,
                review_digest,envelope_digest,receipt_schema,receipt_status,payload_json,
                payload_xxh3_128,payload_xxh3_64_guard,payload_bytes,recorded_ns,
                authority,mutation_authority)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'advisory',0)""",
                (
                    receipt.receipt_id,
                    analysis_run_id,
                    proposal.evaluation_id,
                    proposal.question_id,
                    proposal.subject_key,
                    proposal.proposal_id,
                    receipt.template_id,
                    receipt.template_version,
                    processing_signature,
                    review_digest,
                    resolved.envelope_digest,
                    CODE_EXPERIMENT_RECEIPT_SCHEMA,
                    receipt.status,
                    payload,
                    fingerprint.xxh3_128,
                    fingerprint.xxh3_64_guard,
                    fingerprint.byte_count,
                    observed_ns,
                ),
            )
            connection.commit()
        else:
            resolved = _resolved_from_row(prior)
            expected = (
                analysis_run_id,
                proposal.evaluation_id,
                proposal.question_id,
                proposal.subject_key,
                review_digest,
                receipt,
            )
            observed = (
                resolved.analysis_run_id,
                resolved.source_evaluation_id,
                resolved.question_id,
                resolved.subject_key,
                resolved.review_digest,
                resolved.receipt,
            )
            if observed != expected:
                raise CodeExperimentStoreError("experiment_receipt_identity_collision")
            connection.rollback()
        # The row commit is the publication boundary.  A checkpoint failure is
        # recoverable by retrying the same immutable receipt, which must also
        # checkpoint the already-published idempotent branch before cleanup.
        checkpoint_code_wal(connection, error_type=CodeExperimentStoreError)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    remove_checkpointed_code_sidecars(
        selected,
        error_type=CodeExperimentStoreError,
        require_removal=False,
    )
    return resolved


def read_code_experiment_receipts(
    database: Path,
    *,
    analysis_run_id: int,
    processing_signature: str,
    plan: CodeExperimentPlan,
) -> tuple[ResolvedCodeExperimentReceipt, ...]:
    """Resolve the newest terminal evidence per current proposal and signature.

    A prior completed owner may supply the receipt when a later completed Code
    publication is an exact processing-signature replay.  A different signature,
    an incomplete current owner or a newer failed/abstained receipt stays closed.
    """

    if not isinstance(plan, CodeExperimentPlan):
        raise TypeError("Code experiment plan must be typed")
    if (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("experiment receipt analysis run is invalid")
    _required("experiment processing signature", processing_signature, 2_048)
    proposals = tuple(
        item
        for item in plan.proposals
        if item.planning_status == "planned" and item.runner_kind != "none"
    )
    if not proposals:
        return ()
    if len(proposals) > CODE_EXPERIMENT_STORE_MAX_RESOLVED:
        raise CodeExperimentStoreError("experiment_receipt_read_bound_exceeded")
    proposal_by_id = {item.proposal_id: item for item in proposals}
    placeholders = ",".join("?" for _ in proposals)
    query = f"""WITH ranked AS (
        SELECT r.*,ROW_NUMBER() OVER (
            PARTITION BY r.proposal_id
            ORDER BY r.recorded_ns DESC,r.receipt_id
        ) AS receipt_rank
        FROM code_experiment_receipts r
        JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
        WHERE r.analysis_run_id<=?
          AND r.source_processing_signature=?
          AND a.status='completed'
          AND r.proposal_id IN ({placeholders})
    )
    SELECT * FROM ranked WHERE receipt_rank=1 AND receipt_status='passed'
    ORDER BY proposal_id LIMIT ?"""
    with readonly_code_database(Path(database)) as connection:
        validate_code_schema(connection)
        current = connection.execute(
            """SELECT status,processing_signature,
            (SELECT MAX(analysis_run_id) FROM analysis_runs) AS latest_analysis_run_id
            FROM analysis_runs
            WHERE analysis_run_id=?""",
            (analysis_run_id,),
        ).fetchone()
        if (
            current is None
            or str(current["status"]) != "completed"
            or str(current["processing_signature"]) != processing_signature
            or int(current["latest_analysis_run_id"]) != analysis_run_id
        ):
            return ()
        rows = connection.execute(
            query,
            (
                analysis_run_id,
                processing_signature,
                *(item.proposal_id for item in proposals),
                CODE_EXPERIMENT_STORE_MAX_RESOLVED + 1,
            ),
        ).fetchall()
    if len(rows) > CODE_EXPERIMENT_STORE_MAX_RESOLVED:
        raise CodeExperimentStoreError("experiment_receipt_read_bound_exceeded")
    result: list[ResolvedCodeExperimentReceipt] = []
    for row in rows:
        proposal = proposal_by_id.get(str(row["proposal_id"]))
        if proposal is None:
            raise CodeExperimentStoreError("stored_experiment_proposal_is_not_current")
        resolved = _resolved_from_row(row)
        receipt = resolved.receipt
        if (
            resolved.analysis_run_id > analysis_run_id
            or resolved.question_id != proposal.question_id
            or resolved.subject_key != proposal.subject_key
            or receipt.proposal_id != proposal.proposal_id
            or receipt.template_id != proposal.template_id
            or receipt.template_version != proposal.template_version
            or receipt.source_version != processing_signature
            or receipt.status != "passed"
        ):
            raise CodeExperimentStoreError("stored_experiment_receipt_context_invalid")
        result.append(resolved)
    return tuple(result)


def parse_resolved_code_experiment_receipt_payload(
    payload: Mapping[str, object],
) -> ResolvedCodeExperimentReceipt:
    """Parse one strict public storage envelope and recheck its receipt digest."""

    expected = {
        "schema",
        "analysis_run_id",
        "source_evaluation_id",
        "question_id",
        "subject_key",
        "review_digest",
        "recorded_ns",
        "payload_xxh3_128",
        "payload_xxh3_64_guard",
        "payload_bytes",
        "envelope_digest",
        "receipt",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected
        or payload.get("schema") != CODE_EXPERIMENT_STORE_SCHEMA
    ):
        raise ValueError("resolved experiment receipt payload fields are invalid")
    raw_receipt = payload.get("receipt")
    if not isinstance(raw_receipt, Mapping):
        raise ValueError("resolved experiment receipt wire is invalid")
    canonical = canonical_json(raw_receipt)
    try:
        resolved = _resolved(
            analysis_run_id=payload["analysis_run_id"],  # type: ignore[arg-type]
            source_evaluation_id=payload["source_evaluation_id"],  # type: ignore[arg-type]
            question_id=payload["question_id"],  # type: ignore[arg-type]
            subject_key=payload["subject_key"],  # type: ignore[arg-type]
            review_digest=payload["review_digest"],  # type: ignore[arg-type]
            recorded_ns=payload["recorded_ns"],  # type: ignore[arg-type]
            payload=canonical,
            payload_xxh3_128=payload["payload_xxh3_128"],  # type: ignore[arg-type]
            payload_xxh3_64_guard=payload["payload_xxh3_64_guard"],  # type: ignore[arg-type]
            payload_bytes=payload["payload_bytes"],  # type: ignore[arg-type]
        )
    except CodeExperimentStoreError as exc:
        raise ValueError("resolved experiment receipt payload is invalid") from exc
    if payload["envelope_digest"] != resolved.envelope_digest:
        raise ValueError("resolved experiment receipt envelope digest is invalid")
    return resolved


@dataclass(frozen=True, slots=True)
class _RequirementBinding:
    question_id: str
    template_id: str
    requirement_id: str
    gate_ids: tuple[str, ...]


_REQUIREMENT_BINDINGS: tuple[_RequirementBinding, ...] = (
    _RequirementBinding(
        "architecture.declared_import_contracts_are_evaluated",
        "architecture.declared_import_contract_acceptance",
        "observed_violation_impact_characterized",
        (
            "forbidden_edges_and_cycles_preserve_shortest_chain_and_line_evidence",
            "live_repository_graph_has_no_declared_contract_violation",
        ),
    ),
    _RequirementBinding(
        "architecture.declared_import_contracts_are_evaluated",
        "architecture.declared_import_contract_acceptance",
        "contract_exception_counterevidence_evaluated",
        (
            "declared_boundary_fixture_accepts_required_entrypoints",
            "public_facade_crossings_match_the_explicit_contract",
        ),
    ),
    _RequirementBinding(
        "architecture.declared_import_contracts_are_evaluated",
        "architecture.declared_import_contract_acceptance",
        "boundary_acceptance_experiment_result",
        (
            "declared_boundary_fixture_accepts_required_entrypoints",
            "forbidden_edges_and_cycles_preserve_shortest_chain_and_line_evidence",
            "live_repository_graph_has_no_declared_contract_violation",
            "public_facade_crossings_match_the_explicit_contract",
        ),
    ),
    _RequirementBinding(
        "capability.route_reaches_user_visible_outcome",
        "capability.public_route_acceptance",
        "causal_durable_execution_path_observed",
        ("public_text_entrypoint_first_run_and_replay_observed",),
    ),
    _RequirementBinding(
        "capability.route_reaches_user_visible_outcome",
        "capability.public_route_acceptance",
        "public_read_consumer_observed",
        ("same_fixture_source_reaches_public_text_search_output",),
    ),
    _RequirementBinding(
        "capability.route_reaches_user_visible_outcome",
        "capability.public_route_acceptance",
        "public_acceptance_scenario_observed",
        (
            "partial_search_abstention_is_explicit_and_read_only",
            "public_text_entrypoint_first_run_and_replay_observed",
            "same_fixture_source_reaches_public_text_search_output",
        ),
    ),
    _RequirementBinding(
        "dependency.declaration_installation_and_license_evidence_is_resolved",
        "security.bounded_boundary_scenarios",
        "dependency_counterevidence_evaluated",
        (
            "dependency_declaration_inventory_record_and_license_evidence_are_correlated",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
        ),
    ),
    _RequirementBinding(
        "dependency.declaration_installation_and_license_evidence_is_resolved",
        "security.bounded_boundary_scenarios",
        "release_artifact_dependency_experiment_result",
        (
            "dependency_declaration_inventory_record_and_license_evidence_are_correlated",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
            "source_only_dependency_is_hash_pinned_and_built_without_installing",
        ),
    ),
    _RequirementBinding(
        "evolution.code_owner_schema_requires_migration_review",
        "evolution.code_schema_upgrade_matrix",
        "populated_upgrade_and_recovery_evidence",
        (
            "migration_failure_rolls_back_schema_objects_and_existing_facts",
            "oldest_populated_schema_upgrades_preserve_rows_relations_fts_and_reopen",
            "receipt_schema_upgrade_preserves_existing_code_facts",
        ),
    ),
    _RequirementBinding(
        "evolution.code_owner_schema_requires_migration_review",
        "evolution.code_schema_upgrade_matrix",
        "schema_compatibility_counterevidence_evaluated",
        (
            "future_schema_is_rejected_without_mutation_or_sidecars",
            "migration_failure_rolls_back_schema_objects_and_existing_facts",
        ),
    ),
    _RequirementBinding(
        "evolution.code_owner_schema_requires_migration_review",
        "evolution.code_schema_upgrade_matrix",
        "schema_upgrade_matrix_result",
        (
            "future_schema_is_rejected_without_mutation_or_sidecars",
            "migration_failure_rolls_back_schema_objects_and_existing_facts",
            "oldest_populated_schema_upgrades_preserve_rows_relations_fts_and_reopen",
            "receipt_schema_upgrade_preserves_existing_code_facts",
        ),
    ),
    _RequirementBinding(
        "retention.dry_run_preserves_declared_durable_holds",
        "retention.durable_hold_safety",
        "isolated_retention_safety_experiment_result",
        (
            "current_previous_builders_leases_and_human_evidence_are_protected",
            "dry_run_never_supports_deletion_and_preserves_phase_order",
            "incomplete_review_receipt_or_schema_drift_fails_closed_without_mutation",
            "reader_snapshot_does_not_mix_concurrent_owner_commit",
        ),
    ),
    _RequirementBinding(
        "security.static_invariants_and_vulnerability_evidence_is_resolved",
        "security.bounded_boundary_scenarios",
        "security_counterevidence_evaluated",
        (
            "bounded_local_staging_rejects_unowned_inputs",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "provider_environment_strips_credentials_and_disables_networked_modes",
        ),
    ),
    _RequirementBinding(
        "security.static_invariants_and_vulnerability_evidence_is_resolved",
        "security.bounded_boundary_scenarios",
        "security_verification_experiment_result",
        (
            "bounded_local_staging_rejects_unowned_inputs",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "pip_audit_contract_records_bounded_phase_complete_result",
            "provider_environment_strips_credentials_and_disables_networked_modes",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
        ),
    ),
    _RequirementBinding(
        "state.declared_workflow_sql_matches_implementation",
        "state.runtime_sql_trace",
        "runtime_transaction_order_observed",
        ("successful_terminal_transaction_contains_required_text_tables",),
    ),
    _RequirementBinding(
        "state.declared_workflow_sql_matches_implementation",
        "state.runtime_sql_trace",
        "indirect_helper_and_dynamic_sql_counterevidence_evaluated",
        ("literal_sql_parser_preserves_dynamic_sql_as_missing_evidence",),
    ),
    _RequirementBinding(
        "state.declared_workflow_sql_matches_implementation",
        "state.runtime_sql_trace",
        "workflow_fault_boundary_experiment_result",
        (
            "post_terminalization_exception_leaves_no_partial_publication",
            "process_death_before_commit_rolls_back_and_restart_converges",
        ),
    ),
    _RequirementBinding(
        "state.text_semantic_published_projection_is_aligned",
        "state.semantic_process_death_recovery",
        "build_recovery_counterevidence_evaluated",
        (
            "dead_building_generation_remains_unpublished",
            "resume_publishes_complete_generation_atomically",
        ),
    ),
    _RequirementBinding(
        "state.text_semantic_published_projection_is_aligned",
        "state.semantic_process_death_recovery",
        "process_death_recovery_experiment_result",
        (
            "committed_staging_prefix_survives_process_death",
            "dead_building_generation_remains_unpublished",
            "resume_publishes_complete_generation_atomically",
        ),
    ),
)


def _experiment_evidence(
    evaluation: AnalysisQuestionEvaluation,
    requirement_id: str,
    gate_ids: tuple[str, ...],
    resolved: ResolvedCodeExperimentReceipt,
    *,
    role: Literal["supporting", "counterevidence", "experiment_result"],
) -> AnalysisEvidenceRef:
    receipt = resolved.receipt
    gates = {item.gate_id: item for item in receipt.gate_outcomes}
    selected = tuple(gates[gate_id] for gate_id in gate_ids)
    if any(item.status != "passed" for item in selected):
        raise CodeExperimentStoreError("experiment_requirement_gate_not_passed")
    projection = {
        "receipt_id": receipt.receipt_id,
        "proposal_id": receipt.proposal_id,
        "source_evaluation_id": resolved.source_evaluation_id,
        "current_evaluation_id": evaluation.evaluation_id,
        "question_id": resolved.question_id,
        "subject_key": resolved.subject_key,
        "requirement_id": requirement_id,
        "gate_outcomes": [asdict(item) for item in selected],
        "payload_digest": resolved.payload_xxh3_128,
    }
    projection_digest = analysis_identity("code-experiment-projection-v1", projection)
    evidence_id = analysis_identity(
        "code-experiment-evidence-v1",
        {
            "subject_key": evaluation.subject.subject_key,
            "requirement_id": requirement_id,
            "projection_digest": projection_digest,
        },
    )
    return AnalysisEvidenceRef(
        evidence_id=evidence_id,
        subject_key=evaluation.subject.subject_key,
        role=role,
        evidence_kind="experiment_result",
        source_owner_id="code",
        producer_id="code-experiment-executor",
        producer_version=receipt.policy_id,
        source_schema=CODE_EXPERIMENT_RECEIPT_SCHEMA,
        source_record_kind="code_experiment_receipt",
        source_record_id=receipt.receipt_id,
        source_projection_digest=projection_digest,
        snapshot_id=evaluation.subject.snapshot_id,
        revision_id=evaluation.subject.revision_id,
        facts=(
            AnalysisFact("receipt_status", receipt.status),
            AnalysisFact("template_id", receipt.template_id),
            AnalysisFact(
                "source_evaluation_replayed",
                resolved.source_evaluation_id != evaluation.evaluation_id,
            ),
            AnalysisFact("gate_ids", ",".join(gate_ids)),
            AnalysisFact("gate_count", len(gate_ids), "count"),
            AnalysisFact(
                "relation_count",
                sum(len(item.relation_ids) for item in selected),
                "count",
            ),
            AnalysisFact("recorded_ns", resolved.recorded_ns, "nanoseconds"),
        ),
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="code.sqlite-experiment-receipt-resolver",
        resolver_version="v1",
        limitations=(
            "exact_test_contract_outcome_not_formal_truth",
            "receipt_does_not_own_human_decision",
            "receipt_does_not_authorize_product_mutation",
            "receipt_rebind_requires_stable_proposal_identity_and_exact_processing_signature",
        ),
    )


def apply_code_experiment_receipts(
    specs: tuple[AnalysisQuestionSpec, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
    plan: CodeExperimentPlan,
    receipts: tuple[ResolvedCodeExperimentReceipt, ...],
) -> tuple[AnalysisQuestionEvaluation, ...]:
    """Link passed, exact receipts and recompute decision readiness fail-closed."""

    validate_analysis_question_set(specs, evaluations)
    if plan != plan_code_experiments(specs, evaluations):
        raise ValueError("experiment receipt projection requires the exact base plan")
    if not receipts:
        return evaluations
    spec_by_id = {(item.question_id, item.version): item for item in specs}
    evaluation_by_id = {item.evaluation_id: item for item in evaluations}
    proposal_by_id = {item.proposal_id: item for item in plan.proposals}
    receipt_by_evaluation: dict[str, ResolvedCodeExperimentReceipt] = {}
    for resolved in receipts:
        proposal = proposal_by_id.get(resolved.receipt.proposal_id)
        evaluation = None if proposal is None else evaluation_by_id.get(proposal.evaluation_id)
        if (
            proposal is None
            or evaluation is None
            or proposal.evaluation_id != evaluation.evaluation_id
            or resolved.question_id != proposal.question_id
            or resolved.subject_key != proposal.subject_key
            or evaluation.question_id != resolved.question_id
            or evaluation.subject.subject_key != resolved.subject_key
            or proposal.evaluation_binding_fingerprint
            != experiment_evaluation_binding_fingerprint(evaluation)
        ):
            raise CodeExperimentStoreError("experiment_receipt_has_no_current_evaluation")
        if resolved.receipt.status != "passed" or proposal.evaluation_id in receipt_by_evaluation:
            raise CodeExperimentStoreError("experiment_receipt_projection_is_not_unique_and_passed")
        receipt_by_evaluation[proposal.evaluation_id] = resolved

    projected: list[AnalysisQuestionEvaluation] = []
    for evaluation in evaluations:
        linked_receipt = receipt_by_evaluation.get(evaluation.evaluation_id)
        if linked_receipt is None:
            projected.append(evaluation)
            continue
        receipt = linked_receipt.receipt
        bindings = tuple(
            item
            for item in _REQUIREMENT_BINDINGS
            if item.question_id == evaluation.question_id
            and item.template_id == receipt.template_id
        )
        if not bindings:
            raise CodeExperimentStoreError("experiment_receipt_has_no_evidence_binding")
        spec = spec_by_id[(evaluation.question_id, evaluation.question_version)]
        requirement_specs = {item.requirement_id: item for item in spec.requirements}
        gate_ids = {item.gate_id for item in receipt.gate_outcomes if item.status == "passed"}
        if any(not set(binding.gate_ids) <= gate_ids for binding in bindings):
            raise CodeExperimentStoreError("experiment_receipt_lacks_bound_gate_evidence")
        new_evidence: list[AnalysisEvidenceRef] = list(evaluation.evidence)
        replacements: dict[str, AnalysisRequirementEvaluation] = {}
        for binding in bindings:
            requirement = requirement_specs.get(binding.requirement_id)
            if (
                requirement is None
                or "experiment_result" not in requirement.accepted_evidence_kinds
            ):
                raise CodeExperimentStoreError("experiment_evidence_binding_contract_changed")
            evidence = _experiment_evidence(
                evaluation,
                binding.requirement_id,
                binding.gate_ids,
                linked_receipt,
                role=requirement.role,  # type: ignore[arg-type]
            )
            new_evidence.append(evidence)
            current_requirement = next(
                item
                for item in evaluation.requirements
                if item.requirement_id == binding.requirement_id
            )
            replacements[binding.requirement_id] = AnalysisRequirementEvaluation(
                binding.requirement_id,
                "satisfied",
                (*current_requirement.evidence_ids, evidence.evidence_id),
                "passed_registered_experiment_receipt_linked",
            )
        requirements = tuple(
            replacements.get(item.requirement_id, item) for item in evaluation.requirements
        )
        decision_complete = all(
            item.status == "satisfied"
            for item in requirements
            if requirement_specs[item.requirement_id].stage == "decision"
        )
        counter_requirements = tuple(
            item
            for item in requirements
            if requirement_specs[item.requirement_id].role == "counterevidence"
        )
        provisional = replace(
            evaluation,
            evidence=tuple(new_evidence),
            requirements=requirements,
            decision_readiness=(
                "human_review_required" if decision_complete else "experiment_required"
            ),
            decision_reason=(
                "decision_evidence_complete_human_decision_required"
                if decision_complete
                else "decision_evidence_incomplete"
            ),
            counterevidence_status=(
                "evaluated"
                if counter_requirements
                and all(item.status == "satisfied" for item in counter_requirements)
                else "not_evaluated"
            ),
            next_action_ids=() if decision_complete else evaluation.next_action_ids,
            limitations=tuple(
                dict.fromkeys(
                    (
                        *evaluation.limitations,
                        "linked_experiment_receipt_is_exact_test_evidence_not_formal_truth",
                        "human_decision_remains_required_after_experiment",
                    )
                )
            ),
        )
        identity_values = {
            key: value
            for key, value in asdict(provisional).items()
            if key not in {"evaluation_id", "rank"}
        }
        projected_evaluation = replace(
            provisional,
            evaluation_id=analysis_identity(
                "code-question-evaluation-with-experiment-v1",
                identity_values,
            ),
        )
        validate_analysis_question_evaluation(spec, projected_evaluation)
        projected.append(projected_evaluation)
    result = tuple(projected)
    validate_analysis_question_set(specs, result)
    return result


__all__ = [
    "CODE_EXPERIMENT_STORE_MAX_PAYLOAD_BYTES",
    "CODE_EXPERIMENT_STORE_MAX_RECEIPTS_PER_PROPOSAL",
    "CODE_EXPERIMENT_STORE_MAX_RESOLVED",
    "CODE_EXPERIMENT_STORE_SCHEMA",
    "CodeExperimentStoreError",
    "ResolvedCodeExperimentReceipt",
    "apply_code_experiment_receipts",
    "code_review_digest_identity",
    "parse_resolved_code_experiment_receipt_payload",
    "read_code_experiment_receipts",
    "record_code_experiment_receipt",
]
