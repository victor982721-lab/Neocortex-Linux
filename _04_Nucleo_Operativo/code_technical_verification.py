"""Independent, deterministic technical dispositions over completed Code evidence.

The question engines and experiment executor remain evidence producers.  This
module is a separate, allow-listed verifier: it rechecks complete requirement
partitions, exact passed receipts, measured gates, and question-specific
negative controls before it can say that no code change is required within the
verified scope.

It never impersonates a human actor, never records a Framework human decision,
and never grants mutation authority.  Questions without an exact policy remain
explicitly unresolved rather than being accepted by a generic "all tests pass"
rule.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal, Mapping, Sequence, cast

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_set,
)
from .code_architecture_questions import ARCHITECTURE_CONTRACT_QUESTION
from .code_experiment_store import ResolvedCodeExperimentReceipt
from .code_change_evolution_analysis import CODE_SCHEMA_EVOLUTION_QUESTION
from .code_route_capability_analysis import ROUTE_CAPABILITY_QUESTION
from .code_retention_analysis import RETENTION_EXPECTED_HOLDS, RETENTION_HOLD_QUESTION
from .code_review_task_analysis import FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION
from .code_schema import CODE_SCHEMA_VERSION
from .code_security_dependency_questions import (
    DEPENDENCY_EVIDENCE_QUESTION,
    SECURITY_EVIDENCE_QUESTION,
)
from .code_state_interaction_analysis import WORKFLOW_SQL_QUESTION
from .code_state_projection_analysis import TEXT_SEMANTIC_PROJECTION_QUESTION

CODE_TECHNICAL_VERIFICATION_SCHEMA = "neocortex.code-technical-verification/v1"
CODE_TECHNICAL_VERIFICATION_POLICY = (
    "allowlisted-independent-evidence-complete-no-change-verifier-v4"
)
CODE_TECHNICAL_VERIFICATION_MAX_REVIEWS = 256

TechnicalVerificationStatus = Literal["ready", "partial", "not_required"]
TechnicalDisposition = Literal["no_change_required_within_verified_scope"]


def _required(label: str, value: object, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _texts(
    label: str,
    values: object,
    *,
    sorted_values: bool = False,
    maximum: int = 2_048,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, item, maximum) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if sorted_values and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class _TechnicalPolicy:
    question_id: str
    question_version: str
    question_spec_fingerprint: str
    subject_prefix: str
    template_id: str
    required_gate_ids: tuple[str, ...]
    scope_statement: str
    reason_code: str
    residual_risks: tuple[str, ...]


_FRAMEWORK_REVIEW_TASK_COUNTER_GATES = (
    "faulted_publication_and_event_transactions_preserve_previous_heads",
    "progress_and_event_heads_reject_stale_compare_and_swap",
    "semantically_changed_retry_is_rejected_as_snapshot_changed",
)
_FRAMEWORK_REVIEW_TASK_ALL_GATES = (
    "exact_human_claim_and_terminal_decision_retries_are_idempotent",
    "faulted_publication_and_event_transactions_preserve_previous_heads",
    "page_publication_is_atomic_resumable_and_idempotent",
    "progress_and_event_heads_reject_stale_compare_and_swap",
    "semantically_changed_retry_is_rejected_as_snapshot_changed",
)


_TECHNICAL_POLICIES = (
    _TechnicalPolicy(
        ARCHITECTURE_CONTRACT_QUESTION.question_id,
        ARCHITECTURE_CONTRACT_QUESTION.version,
        analysis_question_spec_fingerprint(ARCHITECTURE_CONTRACT_QUESTION),
        "architecture:contract:",
        "architecture.declared_import_contract_acceptance",
        (
            "declared_boundary_fixture_accepts_required_entrypoints",
            "forbidden_edges_and_cycles_preserve_shortest_chain_and_line_evidence",
            "live_repository_graph_has_no_declared_contract_violation",
            "public_facade_crossings_match_the_explicit_contract",
        ),
        "the_versioned_import_contracts_have_no_observed_violation_and_the_bounded_positive_and_negative_boundary_controls_pass",
        "declared_import_contract_matrix_passed_without_a_change_signal",
        (
            "static_import_contracts_do_not_observe_runtime_dispatch_or_plugin_edges",
            "passing_declared_contracts_do_not_establish_complete_architectural_intent",
        ),
    ),
    _TechnicalPolicy(
        ROUTE_CAPABILITY_QUESTION.question_id,
        ROUTE_CAPABILITY_QUESTION.version,
        analysis_question_spec_fingerprint(ROUTE_CAPABILITY_QUESTION),
        "capability:route:text",
        "capability.public_route_acceptance",
        (
            "partial_search_abstention_is_explicit_and_read_only",
            "public_text_entrypoint_first_run_and_replay_observed",
            "same_fixture_source_reaches_public_text_search_output",
        ),
        "the_public_text_route_reaches_its_read_only_search_consumer_for_the_bounded_fixture",
        "public_text_route_contract_passed_without_a_change_signal",
        (
            "fixture_acceptance_does_not_measure_human_product_value",
            "non_text_routes_are_outside_this_disposition",
        ),
    ),
    _TechnicalPolicy(
        CODE_SCHEMA_EVOLUTION_QUESTION.question_id,
        CODE_SCHEMA_EVOLUTION_QUESTION.version,
        analysis_question_spec_fingerprint(CODE_SCHEMA_EVOLUTION_QUESTION),
        "code-owner-schema-subject-v1:",
        "evolution.code_schema_upgrade_matrix",
        (
            "future_schema_is_rejected_without_mutation_or_sidecars",
            "migration_failure_rolls_back_schema_objects_and_existing_facts",
            "oldest_populated_schema_upgrades_preserve_rows_relations_fts_and_reopen",
            "receipt_schema_upgrade_preserves_existing_code_facts",
        ),
        "the_code_owner_schema_matches_its_exact_contract_and_the_bounded_populated_upgrade_rollback_matrix_passes",
        "code_owner_schema_upgrade_matrix_passed_without_a_change_signal",
        (
            "fixture_matrix_does_not_cover_every_historical_database_or_filesystem_failure",
            "backup_restore_and_power_loss_remain_outside_this_disposition",
        ),
    ),
    _TechnicalPolicy(
        DEPENDENCY_EVIDENCE_QUESTION.question_id,
        DEPENDENCY_EVIDENCE_QUESTION.version,
        analysis_question_spec_fingerprint(DEPENDENCY_EVIDENCE_QUESTION),
        "dependency:neocortex-environment",
        "security.bounded_boundary_scenarios",
        (
            "dependency_declaration_inventory_record_and_license_evidence_are_correlated",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
            "source_only_dependency_is_hash_pinned_and_built_without_installing",
        ),
        "the_current_dependency_declaration_and_installed_record_license_gates_pass_and_the_bounded_dependency_negative_controls_are_satisfied",
        "dependency_and_installed_artifact_gates_passed_without_a_change_signal",
        (
            "development_environment_evidence_does_not_replace_candidate_wheel_install_and_replay",
            "license_metadata_presence_does_not_establish_legal_compatibility",
            "manifest_and_import_agreement_does_not_prove_runtime_reachability",
        ),
    ),
    _TechnicalPolicy(
        FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION.question_id,
        FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION.version,
        analysis_question_spec_fingerprint(FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION),
        "contract:framework-review-task-protocol",
        "framework.review_task_protocol_acceptance",
        _FRAMEWORK_REVIEW_TASK_ALL_GATES,
        "the_versioned_framework_review_task_owner_and_public_protocol_contracts_match_and_the_bounded_atomicity_cas_fault_and_exact_retry_controls_pass_without_creating_human_authority",
        "framework_review_task_protocol_matrix_passed_without_a_change_signal",
        (
            "bounded_injected_exceptions_are_not_process_death_power_loss_or_filesystem_failure",
            "fixture_human_actor_proves_typed_protocol_not_identity_authentication_or_a_human_outcome",
            "selected_review_task_protocol_fixtures_do_not_cover_every_future_producer",
        ),
    ),
    _TechnicalPolicy(
        RETENTION_HOLD_QUESTION.question_id,
        RETENTION_HOLD_QUESTION.version,
        analysis_question_spec_fingerprint(RETENTION_HOLD_QUESTION),
        "retention:canonical-durable-holds",
        "retention.durable_hold_safety",
        (
            "current_previous_builders_leases_and_human_evidence_are_protected",
            "dry_run_never_supports_deletion_and_preserves_phase_order",
            "incomplete_review_receipt_or_schema_drift_fails_closed_without_mutation",
            "reader_snapshot_does_not_mix_concurrent_owner_commit",
        ),
        "the_four_retention_owners_expose_all_declared_holds_and_the_bounded_dry_run_negative_controls_pass_without_deletion_authority",
        "retention_durable_hold_matrix_passed_without_a_change_signal",
        (
            "bounded_fixtures_do_not_prove_power_loss_behavior",
            "the_disposition_does_not_authorize_or_validate_a_future_delete_executor",
            "the_four_owner_snapshots_are_not_cross_database_atomic",
        ),
    ),
    _TechnicalPolicy(
        SECURITY_EVIDENCE_QUESTION.question_id,
        SECURITY_EVIDENCE_QUESTION.version,
        analysis_question_spec_fingerprint(SECURITY_EVIDENCE_QUESTION),
        "project:neocortex-security-evidence",
        "security.bounded_boundary_scenarios",
        (
            "bounded_local_staging_rejects_unowned_inputs",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "pip_audit_contract_records_bounded_phase_complete_result",
            "provider_environment_strips_credentials_and_disables_networked_modes",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
        ),
        "the_current_static_invariant_and_vulnerability_gates_pass_and_the_bounded_hostile_input_provider_environment_and_missing_provider_controls_are_satisfied",
        "security_provider_gates_passed_without_a_change_signal",
        (
            "a_current_advisory_feed_does_not_cover_unknown_or_uninstalled_dependencies",
            "selected_static_and_fixture_controls_do_not_prove_absence_of_runtime_vulnerabilities",
            "vulnerability_feed_results_are_time_bound",
        ),
    ),
    _TechnicalPolicy(
        WORKFLOW_SQL_QUESTION.question_id,
        WORKFLOW_SQL_QUESTION.version,
        analysis_question_spec_fingerprint(WORKFLOW_SQL_QUESTION),
        "workflow:text.derivation-publication:",
        "state.runtime_sql_trace",
        (
            "literal_sql_parser_preserves_dynamic_sql_as_missing_evidence",
            "post_terminalization_exception_leaves_no_partial_publication",
            "process_death_before_commit_rolls_back_and_restart_converges",
            "successful_terminal_transaction_contains_required_text_tables",
        ),
        "the_declared_text_publication_workflow_satisfies_the_measured_transaction_and_fault_gates",
        "text_workflow_runtime_contract_passed_without_a_change_signal",
        (
            "static_call_resolution_remains_partial_for_indirect_helpers",
            "selected_fault_injection_is_not_power_loss_proof",
        ),
    ),
    _TechnicalPolicy(
        TEXT_SEMANTIC_PROJECTION_QUESTION.question_id,
        TEXT_SEMANTIC_PROJECTION_QUESTION.version,
        analysis_question_spec_fingerprint(TEXT_SEMANTIC_PROJECTION_QUESTION),
        "workflow:text-to-semantic-published-projection",
        "state.semantic_process_death_recovery",
        (
            "committed_staging_prefix_survives_process_death",
            "dead_building_generation_remains_unpublished",
            "resume_publishes_complete_generation_atomically",
        ),
        "the_published_text_semantic_projection_is_aligned_and_the_bounded_process_death_resume_contract_passes",
        "text_semantic_projection_and_resume_contract_passed_without_a_change_signal",
        (
            "process_death_is_not_power_loss_or_filesystem_failure",
            "only_published_text_modality_heads_and_one_bounded_fixture_are_verified",
        ),
    ),
)


def _technical_policy_registry_fingerprint() -> str:
    return analysis_identity(
        "code-technical-verification-policy-v4",
        tuple(asdict(item) for item in _TECHNICAL_POLICIES),
    )


@dataclass(frozen=True, slots=True)
class CodeTechnicalReview:
    review_id: str
    evaluation_id: str
    question_id: str
    question_version: str
    question_spec_fingerprint: str
    subject_key: str
    disposition: TechnicalDisposition
    scope_statement: str
    reason_code: str
    verified_requirement_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    residual_risks: tuple[str, ...]
    verifier_id: Literal["code-independent-technical-policy-verifier"] = (
        "code-independent-technical-policy-verifier"
    )
    verifier_version: Literal["v1"] = "v1"
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("technical review id", self.review_id, 1_024),
            ("technical evaluation id", self.evaluation_id, 1_024),
            ("technical question id", self.question_id, 256),
            ("technical question version", self.question_version, 64),
            ("technical question fingerprint", self.question_spec_fingerprint, 256),
            ("technical subject key", self.subject_key, 1_024),
            ("technical scope statement", self.scope_statement, 512),
            ("technical reason code", self.reason_code, 256),
        ):
            _required(label, value, maximum)
        if self.disposition != "no_change_required_within_verified_scope":
            raise ValueError("technical disposition is invalid")
        _texts(
            "verified requirement id",
            self.verified_requirement_ids,
            sorted_values=True,
        )
        _texts("technical evidence id", self.evidence_ids, sorted_values=True)
        _texts("technical receipt id", self.receipt_ids, sorted_values=True)
        _texts("technical residual risk", self.residual_risks, sorted_values=True)
        if not self.verified_requirement_ids or not self.evidence_ids or not self.receipt_ids:
            raise ValueError("technical review requires requirements, evidence, and receipts")
        if not self.residual_risks:
            raise ValueError("technical review requires bounded residual risks")
        if (
            self.verifier_id != "code-independent-technical-policy-verifier"
            or self.verifier_version != "v1"
            or self.authority != "advisory"
            or self.mutation_authority
        ):
            raise ValueError("technical review authority is invalid")
        expected_id = analysis_identity(
            "code-technical-review-v1",
            {key: value for key, value in asdict(self).items() if key != "review_id"},
        )
        if self.review_id != expected_id:
            raise ValueError("technical review identity is invalid")


@dataclass(frozen=True, slots=True)
class CodeTechnicalReviewGap:
    evaluation_id: str
    question_id: str
    subject_key: str
    reason: str

    def __post_init__(self) -> None:
        _required("technical gap evaluation id", self.evaluation_id, 1_024)
        _required("technical gap question id", self.question_id, 256)
        _required("technical gap subject key", self.subject_key, 1_024)
        _required("technical gap reason", self.reason, 256)


@dataclass(frozen=True, slots=True)
class CodeTechnicalVerification:
    verification_id: str
    status: TechnicalVerificationStatus
    reason: str | None
    policy_id: str
    policy_fingerprint: str
    evidence_complete_evaluations: int
    reviewed_count: int
    no_change_required_count: int
    unresolved_count: int
    reviews: tuple[CodeTechnicalReview, ...]
    gaps: tuple[CodeTechnicalReviewGap, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("technical verification id", self.verification_id, 1_024)
        if self.status not in {"ready", "partial", "not_required"}:
            raise ValueError("technical verification status is invalid")
        if self.reason is not None:
            _required("technical verification reason", self.reason, 256)
        if self.policy_id != CODE_TECHNICAL_VERIFICATION_POLICY:
            raise ValueError("technical verification policy is invalid")
        if self.policy_fingerprint != _technical_policy_registry_fingerprint():
            raise ValueError("technical verification policy fingerprint is invalid")
        for label, value in (
            ("eligible evaluations", self.evidence_complete_evaluations),
            ("reviewed evaluations", self.reviewed_count),
            ("no-change dispositions", self.no_change_required_count),
            ("unresolved evaluations", self.unresolved_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"technical verification {label} must be non-negative")
        if not isinstance(self.reviews, tuple) or any(
            not isinstance(item, CodeTechnicalReview) for item in self.reviews
        ):
            raise ValueError("technical verification reviews are invalid")
        if not isinstance(self.gaps, tuple) or any(
            not isinstance(item, CodeTechnicalReviewGap) for item in self.gaps
        ):
            raise ValueError("technical verification gaps are invalid")
        if len(self.reviews) + len(self.gaps) > CODE_TECHNICAL_VERIFICATION_MAX_REVIEWS:
            raise ValueError("technical verification exceeds its public bound")
        if tuple(item.review_id for item in self.reviews) != tuple(
            sorted(item.review_id for item in self.reviews)
        ):
            raise ValueError("technical reviews must be deterministically ordered")
        if tuple(item.evaluation_id for item in self.gaps) != tuple(
            sorted(item.evaluation_id for item in self.gaps)
        ):
            raise ValueError("technical review gaps must be deterministically ordered")
        if (
            self.reviewed_count != len(self.reviews)
            or self.no_change_required_count != len(self.reviews)
            or self.unresolved_count != len(self.gaps)
            or self.evidence_complete_evaluations != len(self.reviews) + len(self.gaps)
        ):
            raise ValueError("technical verification counts are not derived")
        expected_status: TechnicalVerificationStatus = (
            "not_required"
            if self.evidence_complete_evaluations == 0
            else "partial"
            if self.gaps
            else "ready"
        )
        expected_reason = (
            "no_evidence_complete_question_requires_technical_disposition"
            if expected_status == "not_required"
            else "one_or_more_evidence_complete_questions_lack_a_verified_policy"
            if expected_status == "partial"
            else None
        )
        if self.status != expected_status or self.reason != expected_reason:
            raise ValueError("technical verification status is not derived")
        _texts("technical verification limitation", self.limitations, sorted_values=True)
        if not self.limitations:
            raise ValueError("technical verification requires explicit limitations")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("technical verification must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-technical-verification-v1",
            {key: value for key, value in asdict(self).items() if key != "verification_id"},
        )
        if self.verification_id != expected_id:
            raise ValueError("technical verification identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_TECHNICAL_VERIFICATION_SCHEMA, **asdict(self)}


def _fact_map(evidence: AnalysisEvidenceRef) -> dict[str, object]:
    return {item.name: item.value for item in evidence.facts}


def _record_facts(
    evaluation: AnalysisQuestionEvaluation,
    record_kind: str,
) -> tuple[dict[str, object], ...]:
    return tuple(
        _fact_map(item) for item in evaluation.evidence if item.source_record_kind == record_kind
    )


def _capability_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    runtime = _record_facts(evaluation, "runtime_prerequisite_observation")
    owner = _record_facts(evaluation, "state_owner_snapshot")
    causal = _record_facts(evaluation, "causal_durable_output_projection")
    return (
        len(runtime) == 1
        and runtime[0].get("runtime_state") == "available"
        and runtime[0].get("missing_required_components") == 0
        and len(owner) == 1
        and owner[0].get("owner_state") == "available"
        and owner[0].get("owner_schema_current") is True
        and len(causal) == 1
        and causal[0].get("causal_projection_status") == "resolved"
        and causal[0].get("causal_durable_output_observed") is True
    )


def _workflow_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    projections = _record_facts(evaluation, "bound_workflow_sql_projection")
    if len(projections) != 1:
        return False
    encoded = projections[0].get("boundary_projection_json")
    if not isinstance(encoded, str):
        return False
    try:
        boundaries = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    return bool(boundaries) and all(
        isinstance(item, Mapping) and item.get("unexpected_write_tables") == []
        for item in boundaries
    )


def _projection_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    snapshots = _record_facts(evaluation, "stable_cross_owner_snapshot")
    heads = _record_facts(evaluation, "published_head_revision_projection")
    head_count = heads[0].get("heads") if len(heads) == 1 else None
    return (
        len(snapshots) == 1
        and snapshots[0].get("knowledge_consistency") == "stable"
        and len(heads) == 1
        and heads[0].get("observation") == "aligned"
        and isinstance(head_count, int)
        and not isinstance(head_count, bool)
        and head_count > 0
        and heads[0].get("aligned_heads") == head_count
        and all(
            heads[0].get(name) == 0
            for name in (
                "missing_revisions",
                "extra_revisions",
                "invalid_owner_revisions",
                "invalid_materialization_revisions",
            )
        )
    )


def _schema_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    projections = _record_facts(evaluation, "exact_sqlite_schema_and_migration_ledger")
    if len(projections) != 1:
        return False
    projection = projections[0]
    ddl_digest = projection.get("ddl_digest")
    migration_digest = projection.get("migration_digest")
    table_count = projection.get("table_count")
    index_count = projection.get("index_count")
    trigger_count = projection.get("trigger_count")
    return (
        projection.get("schema_version") == CODE_SCHEMA_VERSION
        and projection.get("migration_count") == CODE_SCHEMA_VERSION
        and isinstance(table_count, int)
        and not isinstance(table_count, bool)
        and table_count > 0
        and isinstance(index_count, int)
        and not isinstance(index_count, bool)
        and index_count > 0
        and isinstance(trigger_count, int)
        and not isinstance(trigger_count, bool)
        and trigger_count > 0
        and isinstance(ddl_digest, str)
        and ddl_digest.startswith("code-schema-ddl-v1:xxh3_128:")
        and isinstance(migration_digest, str)
        and migration_digest.startswith("code-schema-migrations-v1:xxh3_128:")
    )


def _architecture_contract_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    projections = _record_facts(evaluation, "versioned_import_contract_evaluations")
    if len(projections) != 1:
        return False
    projection = projections[0]
    contracts = projection.get("contracts")
    passed_contracts = projection.get("passed_contracts")
    failed_contracts = projection.get("failed_contracts")
    violations = projection.get("contract_violations")
    contract_ids_json = projection.get("contract_ids_json")
    try:
        contract_ids = json.loads(contract_ids_json) if isinstance(contract_ids_json, str) else None
    except (TypeError, ValueError):
        return False
    return (
        isinstance(contracts, int)
        and not isinstance(contracts, bool)
        and contracts > 0
        and isinstance(passed_contracts, int)
        and not isinstance(passed_contracts, bool)
        and passed_contracts == contracts
        and isinstance(failed_contracts, int)
        and not isinstance(failed_contracts, bool)
        and failed_contracts == 0
        and isinstance(violations, int)
        and not isinstance(violations, bool)
        and violations == 0
        and isinstance(contract_ids, list)
        and len(contract_ids) == contracts
        and all(isinstance(item, str) and item for item in contract_ids)
        and len(set(contract_ids)) == contracts
    )


def _retention_predicate(evaluation: AnalysisQuestionEvaluation) -> bool:
    contracts = _record_facts(evaluation, "retention_plan_contract")
    owners = _record_facts(evaluation, "retention_store_projection")
    holds = _record_facts(evaluation, "retention_declared_hold_projection")
    pagination = _record_facts(evaluation, "retention_pagination_projection")
    gaps = _record_facts(evaluation, "retention_gap_counterevidence")
    if not all(len(items) == 1 for items in (contracts, owners, holds, pagination, gaps)):
        return False
    encoded = owners[0].get("stores_json")
    if not isinstance(encoded, str):
        return False
    try:
        stores = json.loads(encoded)
    except (TypeError, ValueError):
        return False
    expected_names = tuple(RETENTION_EXPECTED_HOLDS)
    if not isinstance(stores, list) or tuple(
        item.get("store") if isinstance(item, Mapping) else None for item in stores
    ) != expected_names:
        return False
    for item in stores:
        if not isinstance(item, Mapping) or item.get("status") != "ready":
            return False
        schema_version = item.get("schema_version")
        hold_names = item.get("hold_names")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
            or not isinstance(hold_names, list)
            or set(hold_names) != set(RETENTION_EXPECTED_HOLDS[str(item["store"])])
            or bool(item.get("truncated")) != (item.get("next_after") is not None)
        ):
            return False
    return (
        contracts[0].get("dry_run") is True
        and contracts[0].get("deletion_supported") is False
        and contracts[0].get("keep_published") == 2
        and owners[0].get("stores") == 4
        and owners[0].get("ready_stores") == 4
        and holds[0].get("expected_holds")
        == sum(len(items) for items in RETENTION_EXPECTED_HOLDS.values())
        and holds[0].get("missing_holds") == 0
        and pagination[0].get("pagination_valid") is True
        and gaps[0].get("non_ready_stores") == 0
        and gaps[0].get("missing_holds") == 0
        and gaps[0].get("invalid_pagination") == 0
    )


def _exact_record_facts(
    evaluation: AnalysisQuestionEvaluation,
    record_kind: str,
    expected: Mapping[str, object],
) -> bool:
    records = _record_facts(evaluation, record_kind)
    if len(records) != 1 or set(records[0]) != set(expected):
        return False
    return all(
        type(records[0][name]) is type(value) and records[0][name] == value
        for name, value in expected.items()
    )


def _review_task_requirement_experiment(
    evaluation: AnalysisQuestionEvaluation,
    requirement_id: str,
    *,
    role: Literal["counterevidence", "experiment_result"],
    gate_ids: tuple[str, ...],
    relation_count: int,
) -> AnalysisEvidenceRef | None:
    requirements = tuple(
        item for item in evaluation.requirements if item.requirement_id == requirement_id
    )
    if len(requirements) != 1 or len(requirements[0].evidence_ids) != 1:
        return None
    evidence_by_id = {item.evidence_id: item for item in evaluation.evidence}
    evidence = evidence_by_id.get(requirements[0].evidence_ids[0])
    if evidence is None:
        return None
    facts = _fact_map(evidence)
    expected_fact_names = {
        "receipt_status",
        "template_id",
        "source_evaluation_replayed",
        "gate_ids",
        "gate_count",
        "relation_count",
        "recorded_ns",
    }
    recorded_ns = facts.get("recorded_ns")
    if not (
        evidence.role == role
        and evidence.evidence_kind == "experiment_result"
        and evidence.source_record_kind == "code_experiment_receipt"
        and evidence.completeness == "complete"
        and evidence.bounded
        and not evidence.truncated
        and evidence.authority == "advisory"
        and not evidence.mutation_authority
        and set(facts) == expected_fact_names
        and facts.get("receipt_status") == "passed"
        and facts.get("template_id") == "framework.review_task_protocol_acceptance"
        and facts.get("source_evaluation_replayed") is False
        and facts.get("gate_ids") == ",".join(gate_ids)
        and facts.get("gate_count") == len(gate_ids)
        and facts.get("relation_count") == relation_count
        and isinstance(recorded_ns, int)
        and not isinstance(recorded_ns, bool)
        and recorded_ns > 0
    ):
        return None
    return evidence


def _framework_review_task_protocol_predicate(
    evaluation: AnalysisQuestionEvaluation,
) -> bool:
    """Recheck the frozen Framework contract and both declared outcome sets."""

    if not _exact_record_facts(
        evaluation,
        "framework_review_task_owner_store_contract",
        {
            "logical_owner_id": "review",
            "state_owner_id": "framework",
            "state_store_id": "sqlite:framework.sqlite3",
            "database_name": "framework.sqlite3",
            "framework_schema_version": 22,
            "review_task_contract_version": 1,
        },
    ) or not _exact_record_facts(
        evaluation,
        "framework_review_task_public_protocol_contract",
        {
            "public_adapter_module": "neocortex.review_task_cli_adapter",
            "public_port_module": "_04_Nucleo_Operativo.value_review_port",
            "terminal_states": "dismissed,resolved",
            "terminal_decisions_require_human": True,
            "superseded_repository_only": True,
        },
    ):
        return False
    counterevidence = _review_task_requirement_experiment(
        evaluation,
        "review_task_stale_head_and_fault_counterevidence_evaluated",
        role="counterevidence",
        gate_ids=_FRAMEWORK_REVIEW_TASK_COUNTER_GATES,
        relation_count=5,
    )
    experiment = _review_task_requirement_experiment(
        evaluation,
        "isolated_review_task_protocol_experiment_result",
        role="experiment_result",
        gate_ids=_FRAMEWORK_REVIEW_TASK_ALL_GATES,
        relation_count=8,
    )
    return (
        counterevidence is not None
        and experiment is not None
        and counterevidence.source_record_id == experiment.source_record_id
    )


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _supply_chain_predicate(
    evaluation: AnalysisQuestionEvaluation,
    *,
    domain: Literal["security", "dependency"],
) -> bool:
    coverage_kind = f"{domain}_provider_coverage_projection"
    coverage_records = _record_facts(evaluation, coverage_kind)
    provider_records = _record_facts(evaluation, "provider_and_gate_projection")
    if len(coverage_records) != 1 or len(provider_records) != 2:
        return False
    coverage = coverage_records[0]
    digest = coverage.get("supply_chain_semantic_digest")
    digest_prefix = f"code-supply-{domain}-decision-projection-v1:xxh3_128:"
    if not (
        coverage.get("supply_chain_status") == "ready"
        and coverage.get("supply_chain_reason") is None
        and coverage.get("required_provider_count") == 2
        and coverage.get("ready_provider_count") == 2
        and coverage.get("evaluated_gate_count") == 3
        and coverage.get("failed_gate_count") == 0
        and isinstance(coverage.get("observation_projection_truncated"), bool)
        and isinstance(digest, str)
        and digest.startswith(digest_prefix)
        and len(digest.removeprefix(digest_prefix)) == 32
        and all(
            character in "0123456789abcdef"
            for character in digest.removeprefix(digest_prefix)
        )
    ):
        return False
    expected = (
        {
            "semgrep-neocortex-invariants": (
                "not_applicable",
                1,
                "semgrep_invariants:passed",
            ),
            "pip-audit-known-vulnerabilities": (
                "current",
                2,
                "vulnerability_snapshot_current:passed,no_known_vulnerabilities:passed",
            ),
        }
        if domain == "security"
        else {
            "deptry-project-dependencies": (
                "not_applicable",
                1,
                "dependency_declaration_integrity:passed",
            ),
            "installed-package-inventory": (
                "unknown",
                2,
                "installed_package_integrity:passed,license_inventory_available:passed",
            ),
        }
    )
    by_provider = {item.get("provider_id"): item for item in provider_records}
    if set(by_provider) != set(expected):
        return False
    for provider_id, (freshness, gate_count, gate_statuses) in expected.items():
        facts = by_provider[provider_id]
        if not (
            facts.get("provider_status") == "ready"
            and isinstance(facts.get("provider_tool"), str)
            and bool(facts.get("provider_tool"))
            and isinstance(facts.get("provider_tool_version"), str)
            and bool(facts.get("provider_tool_version"))
            and facts.get("provider_freshness") == freshness
            and facts.get("provider_findings") == 0
            and _nonnegative_integer(facts.get("provider_metrics"))
            and _nonnegative_integer(facts.get("provider_relations"))
            and facts.get("evaluated_gate_count") == gate_count
            and facts.get("failed_gate_count") == 0
            and facts.get("gate_statuses") == gate_statuses
        ):
            return False
    return True


def _policy_predicate(
    policy: _TechnicalPolicy,
    evaluation: AnalysisQuestionEvaluation,
) -> bool:
    if policy.question_id == ARCHITECTURE_CONTRACT_QUESTION.question_id:
        return _architecture_contract_predicate(evaluation)
    if policy.question_id == ROUTE_CAPABILITY_QUESTION.question_id:
        return _capability_predicate(evaluation)
    if policy.question_id == CODE_SCHEMA_EVOLUTION_QUESTION.question_id:
        return _schema_predicate(evaluation)
    if policy.question_id == DEPENDENCY_EVIDENCE_QUESTION.question_id:
        return _supply_chain_predicate(evaluation, domain="dependency")
    if policy.question_id == FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION.question_id:
        return _framework_review_task_protocol_predicate(evaluation)
    if policy.question_id == RETENTION_HOLD_QUESTION.question_id:
        return _retention_predicate(evaluation)
    if policy.question_id == SECURITY_EVIDENCE_QUESTION.question_id:
        return _supply_chain_predicate(evaluation, domain="security")
    if policy.question_id == WORKFLOW_SQL_QUESTION.question_id:
        return _workflow_predicate(evaluation)
    if policy.question_id == TEXT_SEMANTIC_PROJECTION_QUESTION.question_id:
        return _projection_predicate(evaluation)
    return False


def _review(
    policy: _TechnicalPolicy,
    evaluation: AnalysisQuestionEvaluation,
    receipts: tuple[ResolvedCodeExperimentReceipt, ...],
) -> CodeTechnicalReview | str:
    if (
        evaluation.question_spec_fingerprint != policy.question_spec_fingerprint
        or not evaluation.subject.subject_key.startswith(policy.subject_prefix)
    ):
        return "technical_policy_scope_or_question_contract_changed"
    if evaluation.counterevidence_status != "evaluated" or any(
        item.status != "satisfied" for item in evaluation.requirements
    ):
        return "technical_policy_requires_complete_evidence_and_counterevidence"
    experiment_evidence = tuple(
        item for item in evaluation.evidence if item.evidence_kind == "experiment_result"
    )
    receipt_ids = tuple(sorted({item.source_record_id for item in experiment_evidence}))
    receipt_by_id = {item.receipt.receipt_id: item for item in receipts}
    linked = tuple(receipt_by_id[item] for item in receipt_ids if item in receipt_by_id)
    if not receipt_ids or len(linked) != len(receipt_ids):
        return "technical_policy_requires_resolvable_passed_receipts"
    if {item.receipt.template_id for item in linked} != {policy.template_id} or any(
        item.question_id != evaluation.question_id
        or item.subject_key != evaluation.subject.subject_key
        or item.receipt.status != "passed"
        or not item.receipt.code_database_unchanged
        or item.receipt.authority != "advisory"
        or item.receipt.mutation_authority
        for item in linked
    ):
        return "technical_policy_receipt_scope_or_authority_mismatch"
    passed_gates = {
        gate.gate_id
        for item in linked
        for gate in item.receipt.gate_outcomes
        if gate.status == "passed"
    }
    if not set(policy.required_gate_ids) <= passed_gates:
        return "technical_policy_required_gate_evidence_missing"
    if not _policy_predicate(policy, evaluation):
        return "technical_policy_negative_control_not_satisfied"
    values: dict[str, object] = {
        "evaluation_id": evaluation.evaluation_id,
        "question_id": evaluation.question_id,
        "question_version": evaluation.question_version,
        "question_spec_fingerprint": evaluation.question_spec_fingerprint,
        "subject_key": evaluation.subject.subject_key,
        "disposition": "no_change_required_within_verified_scope",
        "scope_statement": policy.scope_statement,
        "reason_code": policy.reason_code,
        "verified_requirement_ids": tuple(
            sorted(item.requirement_id for item in evaluation.requirements)
        ),
        "evidence_ids": tuple(sorted(item.evidence_id for item in evaluation.evidence)),
        "receipt_ids": receipt_ids,
        "residual_risks": tuple(sorted(policy.residual_risks)),
        "verifier_id": "code-independent-technical-policy-verifier",
        "verifier_version": "v1",
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeTechnicalReview(
        review_id=analysis_identity("code-technical-review-v1", values),
        **cast(Any, values),
    )


def _technical_policy_for_evaluation(
    evaluation: AnalysisQuestionEvaluation,
) -> tuple[_TechnicalPolicy | None, str | None]:
    """Resolve one exact question/version/subject policy without key collapse.

    A question may deliberately expose several independently verified subjects.
    The subject prefix therefore participates in policy identity; reducing the
    registry to ``(question_id, version)`` would silently overwrite one policy.
    Overlapping prefixes are treated as an invalid registry rather than being
    resolved by declaration order.
    """

    same_question = tuple(
        item
        for item in _TECHNICAL_POLICIES
        if item.question_id == evaluation.question_id
        and item.question_version == evaluation.question_version
    )
    matching = tuple(
        item
        for item in same_question
        if evaluation.subject.subject_key.startswith(item.subject_prefix)
    )
    if len(matching) == 1:
        return matching[0], None
    if len(matching) > 1:
        return None, "technical_policy_subject_prefix_is_ambiguous"
    if same_question:
        return None, "technical_policy_scope_or_question_contract_changed"
    return None, "no_registered_technical_verification_policy"


def build_code_technical_verification(
    specs: tuple[AnalysisQuestionSpec, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
    receipts: tuple[ResolvedCodeExperimentReceipt, ...],
) -> CodeTechnicalVerification:
    """Recheck evidence-complete questions and publish bounded dispositions."""

    validate_analysis_question_set(specs, evaluations)
    if not isinstance(receipts, tuple) or any(
        not isinstance(item, ResolvedCodeExperimentReceipt) for item in receipts
    ):
        raise ValueError("technical verification receipts are invalid")
    eligible = tuple(
        item for item in evaluations if item.decision_readiness == "human_review_required"
    )
    if len(eligible) > CODE_TECHNICAL_VERIFICATION_MAX_REVIEWS:
        raise ValueError("technical verification eligible evaluation bound exceeded")
    reviews: list[CodeTechnicalReview] = []
    gaps: list[CodeTechnicalReviewGap] = []
    for evaluation in eligible:
        policy, selection_gap = _technical_policy_for_evaluation(evaluation)
        if policy is None:
            result: CodeTechnicalReview | str = (
                selection_gap or "no_registered_technical_verification_policy"
            )
        else:
            result = _review(policy, evaluation, receipts)
        if isinstance(result, CodeTechnicalReview):
            reviews.append(result)
        else:
            gaps.append(
                CodeTechnicalReviewGap(
                    evaluation.evaluation_id,
                    evaluation.question_id,
                    evaluation.subject.subject_key,
                    result,
                )
            )
    ordered_reviews = tuple(sorted(reviews, key=lambda item: item.review_id))
    ordered_gaps = tuple(sorted(gaps, key=lambda item: item.evaluation_id))
    status: TechnicalVerificationStatus = (
        "not_required" if not eligible else "partial" if ordered_gaps else "ready"
    )
    reason = (
        "no_evidence_complete_question_requires_technical_disposition"
        if status == "not_required"
        else "one_or_more_evidence_complete_questions_lack_a_verified_policy"
        if status == "partial"
        else None
    )
    limitations = tuple(
        sorted(
            (
                "technical_disposition_is_scoped_and_does_not_prove_global_correctness",
                "technical_verifier_does_not_impersonate_a_human_actor_or_product_decision",
                "technical_verifier_never_authorizes_source_or_product_state_mutation",
                "unregistered_questions_remain_explicitly_unresolved",
            )
        )
    )
    values: dict[str, object] = {
        "status": status,
        "reason": reason,
        "policy_id": CODE_TECHNICAL_VERIFICATION_POLICY,
        "policy_fingerprint": _technical_policy_registry_fingerprint(),
        "evidence_complete_evaluations": len(eligible),
        "reviewed_count": len(ordered_reviews),
        "no_change_required_count": len(ordered_reviews),
        "unresolved_count": len(ordered_gaps),
        "reviews": ordered_reviews,
        "gaps": ordered_gaps,
        "limitations": limitations,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeTechnicalVerification(
        verification_id=analysis_identity(
            "code-technical-verification-v1",
            {
                **values,
                "reviews": tuple(asdict(item) for item in ordered_reviews),
                "gaps": tuple(asdict(item) for item in ordered_gaps),
            },
        ),
        **cast(Any, values),
    )


def parse_code_technical_verification_payload(
    payload: Mapping[str, object],
) -> CodeTechnicalVerification:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != CODE_TECHNICAL_VERIFICATION_SCHEMA
    ):
        raise ValueError("technical verification payload schema is invalid")
    expected = {field.name for field in fields(CodeTechnicalVerification)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("technical verification payload fields are invalid")
    raw_reviews = payload.get("reviews")
    raw_gaps = payload.get("gaps")
    if (
        not isinstance(raw_reviews, Sequence)
        or isinstance(raw_reviews, (str, bytes, bytearray))
        or not isinstance(raw_gaps, Sequence)
        or isinstance(raw_gaps, (str, bytes, bytearray))
    ):
        raise ValueError("technical verification records are invalid")
    review_fields = {field.name for field in fields(CodeTechnicalReview)}
    gap_fields = {field.name for field in fields(CodeTechnicalReviewGap)}
    reviews: list[CodeTechnicalReview] = []
    gaps: list[CodeTechnicalReviewGap] = []
    for item in raw_reviews:
        if not isinstance(item, Mapping) or set(item) != review_fields:
            raise ValueError("technical review record fields are invalid")
        values = dict(item)
        for key in (
            "verified_requirement_ids",
            "evidence_ids",
            "receipt_ids",
            "residual_risks",
        ):
            values[key] = _texts(
                f"technical review {key}",
                values[key],
                sorted_values=True,
            )
        reviews.append(CodeTechnicalReview(**cast(Any, values)))
    for item in raw_gaps:
        if not isinstance(item, Mapping) or set(item) != gap_fields:
            raise ValueError("technical review gap fields are invalid")
        gaps.append(CodeTechnicalReviewGap(**cast(Any, dict(item))))
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["reviews"] = tuple(reviews)
    values["gaps"] = tuple(gaps)
    values["limitations"] = _texts(
        "technical verification limitation",
        values["limitations"],
        sorted_values=True,
    )
    return CodeTechnicalVerification(**cast(Any, values))


__all__ = [
    "CODE_TECHNICAL_VERIFICATION_MAX_REVIEWS",
    "CODE_TECHNICAL_VERIFICATION_POLICY",
    "CODE_TECHNICAL_VERIFICATION_SCHEMA",
    "CodeTechnicalReview",
    "CodeTechnicalReviewGap",
    "CodeTechnicalVerification",
    "build_code_technical_verification",
    "parse_code_technical_verification_payload",
]
