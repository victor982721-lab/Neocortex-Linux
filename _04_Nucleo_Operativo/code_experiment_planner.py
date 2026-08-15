"""Cheapest-first, non-mutating experiment plans for Code questions.

Question producers own their action identifiers.  This registry gives those
identifiers an execution class, isolation contract, hard budget and explicit
acceptance gates.  Unregistered actions remain visible as planning gaps; they
are never converted into shell commands or silently dropped.

Version 1 selects at most one executable experiment per evaluation and keeps
characterization/counterevidence actions as advisory alternatives.  Execution
is a separate boundary and requires a typed template whose runner is
allow-listed by code, not free-form text from a repository.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Literal, Mapping, Sequence, cast

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_identity,
    validate_analysis_question_evaluation,
    validate_analysis_question_set,
)
from .code_invariant_contracts import INVARIANT_RUNTIME_SCENARIOS, RUNTIME_SCENARIOS

CODE_EXPERIMENT_PLAN_SCHEMA = "neocortex.code-experiment-plan/v2"
CODE_EXPERIMENT_TEMPLATE_REGISTRY_SCHEMA = "neocortex.code-experiment-template-registry/v11"
CODE_EXPERIMENT_PLANNING_POLICY = "registered-applicable-cheapest-discriminating-experiment-v11"
CODE_EXPERIMENT_MAX_PROPOSALS = 256

ExperimentKind = Literal[
    "static_characterization",
    "read_only_probe",
    "isolated_pytest",
    "isolated_fault_injection",
    "isolated_mutation",
    "isolated_upgrade_matrix",
    "human_outcome_linkage",
]
IsolationKind = Literal[
    "read_only_process",
    "pytest_tmp_path",
    "spawned_process_and_tmp_path",
    "disposable_state_copy",
    "disposable_worktree_and_state",
    "review_task_pointer_only",
]
RunnerKind = Literal["none", "trusted_deep_declared_scenarios"]
CostTier = Literal["metadata", "focal", "bounded", "deep"]

_COST_ORDER = {"metadata": 0, "focal": 1, "bounded": 2, "deep": 3}


def _required(label: str, value: object, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _texts(label: str, values: object, *, sorted_values: bool = False) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, item, 16_384) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if sorted_values and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class CodeExperimentTemplate:
    template_id: str
    version: str
    action_ids: tuple[str, ...]
    applicable_question_ids: tuple[str, ...]
    applicable_subject_key_prefixes: tuple[str, ...]
    experiment_kind: ExperimentKind
    isolation: IsolationKind
    runner_kind: RunnerKind
    cost_tier: CostTier
    estimated_attention_minutes: int
    timeout_seconds: int
    max_items: int
    scenario_ids: tuple[str, ...]
    acceptance_gates: tuple[str, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("experiment template id", self.template_id, 256)
        _required("experiment template version", self.version, 64)
        _texts("experiment action id", self.action_ids)
        if not self.action_ids:
            raise ValueError("experiment template requires at least one action id")
        _texts(
            "experiment applicable question id",
            self.applicable_question_ids,
            sorted_values=True,
        )
        _texts(
            "experiment applicable subject prefix",
            self.applicable_subject_key_prefixes,
            sorted_values=True,
        )
        if self.experiment_kind not in {
            "static_characterization",
            "read_only_probe",
            "isolated_pytest",
            "isolated_fault_injection",
            "isolated_mutation",
            "isolated_upgrade_matrix",
            "human_outcome_linkage",
        }:
            raise ValueError("experiment kind is invalid")
        if self.isolation not in {
            "read_only_process",
            "pytest_tmp_path",
            "spawned_process_and_tmp_path",
            "disposable_state_copy",
            "disposable_worktree_and_state",
            "review_task_pointer_only",
        }:
            raise ValueError("experiment isolation is invalid")
        if self.runner_kind not in {"none", "trusted_deep_declared_scenarios"}:
            raise ValueError("experiment runner kind is invalid")
        if self.cost_tier not in _COST_ORDER:
            raise ValueError("experiment cost tier is invalid")
        for label, value, minimum, maximum in (
            ("attention minutes", self.estimated_attention_minutes, 0, 240),
            ("timeout seconds", self.timeout_seconds, 1, 900),
            ("maximum items", self.max_items, 1, 5_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"experiment {label} is outside its bound")
        _texts("experiment scenario id", self.scenario_ids, sorted_values=True)
        _texts("experiment acceptance gate", self.acceptance_gates)
        _texts("experiment limitation", self.limitations)
        if not self.acceptance_gates or not self.limitations:
            raise ValueError("experiment template requires gates and limitations")
        declared_scenarios = {item.scenario_id for item in RUNTIME_SCENARIOS}
        if not set(self.scenario_ids) <= declared_scenarios:
            raise ValueError("experiment template references an unknown runtime scenario")
        if self.runner_kind == "trusted_deep_declared_scenarios":
            if not self.scenario_ids or self.isolation not in {
                "pytest_tmp_path",
                "spawned_process_and_tmp_path",
            }:
                raise ValueError("trusted-deep template requires exact isolated scenarios")
            if not self.applicable_question_ids or not self.applicable_subject_key_prefixes:
                raise ValueError("executable template requires exact question and subject scope")
            scenario_by_id = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
            measured_gates = tuple(
                gate.gate_id
                for scenario_id in self.scenario_ids
                for gate in scenario_by_id[scenario_id].gate_specs
            )
            if self.acceptance_gates != measured_gates:
                raise ValueError("executable template gates must be measured by its scenarios")
        elif self.scenario_ids:
            raise ValueError("non-executable template cannot claim scenario selectors")
        if self.experiment_kind == "isolated_mutation" and self.mutation_authority:
            raise ValueError(
                "mutation experiments mutate only disposable state, never product state"
            )
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("experiment templates must remain advisory and non-mutating")

    @property
    def executable(self) -> bool:
        return self.runner_kind != "none"

    def applies_to(self, *, question_id: str, subject_key: str) -> bool:
        question_matches = not self.applicable_question_ids or question_id in set(
            self.applicable_question_ids
        )
        subject_matches = not self.applicable_subject_key_prefixes or any(
            subject_key.startswith(prefix) for prefix in self.applicable_subject_key_prefixes
        )
        return question_matches and subject_matches


def _template(
    template_id: str,
    action_ids: tuple[str, ...],
    experiment_kind: ExperimentKind,
    isolation: IsolationKind,
    cost_tier: CostTier,
    *,
    timeout: int,
    max_items: int,
    attention: int,
    scenarios: tuple[str, ...] = (),
    runner: RunnerKind = "none",
    questions: tuple[str, ...] = (),
    subject_prefixes: tuple[str, ...] = (),
    gates: tuple[str, ...],
    limitations: tuple[str, ...],
) -> CodeExperimentTemplate:
    versions = {
        "analyzer.registered_invariant_scenarios": "v3",
        "architecture.declared_import_contract_acceptance": "v1",
        "capability.public_route_acceptance": "v2",
        "evolution.code_schema_upgrade_matrix": "v1",
        "framework.review_task_protocol_acceptance": "v1",
        "interfaces.public_cli_contract_acceptance": "v3",
        "interfaces.public_contract_acceptance": "v2",
        "knowledge.asset_health_causal_acceptance": "v1",
        "knowledge.pdf_asset_health_causal_acceptance": "v1",
        "retention.durable_hold_safety": "v2",
        "security.bounded_boundary_scenarios": "v2",
        "state.semantic_process_death_recovery": "v1",
        "state.runtime_sql_trace": "v2",
    }
    return CodeExperimentTemplate(
        template_id,
        versions.get(template_id, "v1"),
        action_ids,
        tuple(sorted(questions)),
        tuple(sorted(subject_prefixes)),
        experiment_kind,
        isolation,
        runner,
        cost_tier,
        attention,
        timeout,
        max_items,
        tuple(sorted(scenarios)),
        gates,
        limitations,
    )


CODE_EXPERIMENT_TEMPLATES: tuple[CodeExperimentTemplate, ...] = (
    _template(
        "analyzer.registered_invariant_scenarios",
        (
            "run_independent_invariant_scenario",
            "terminate_after_text_begin_and_before_terminal_commit_then_restart",
        ),
        "isolated_fault_injection",
        "spawned_process_and_tmp_path",
        "bounded",
        timeout=300,
        max_items=sum(len(item.test_nodeids) for item in INVARIANT_RUNTIME_SCENARIOS),
        attention=5,
        gates=(
            "all_selected_nodeids_report_terminal_outcomes",
            "no_canonical_state_or_corpus_path_is_used",
            "result_digest_and_environment_receipt_are_recorded",
        ),
        limitations=(
            "no_independent_additional_scenario_runner_is_registered",
            "scenario_pass_is_not_formal_proof",
            "process_death_is_not_power_loss",
            "coverage_is_main_process_only",
        ),
    ),
    _template(
        "state.semantic_process_death_recovery",
        ("run_semantic_process_death_recovery_experiment",),
        "isolated_fault_injection",
        "spawned_process_and_tmp_path",
        "bounded",
        timeout=300,
        max_items=1,
        attention=5,
        scenarios=("semantic.staging_process_death_resume",),
        runner="trusted_deep_declared_scenarios",
        questions=("state.text_semantic_published_projection_is_aligned",),
        subject_prefixes=("workflow:text-to-semantic-published-projection",),
        gates=(
            "committed_staging_prefix_survives_process_death",
            "dead_building_generation_remains_unpublished",
            "resume_publishes_complete_generation_atomically",
        ),
        limitations=(
            "process_death_is_not_power_loss_or_filesystem_failure",
            "one_bounded_semantic_text_fixture_is_not_every_source_or_model",
        ),
    ),
    _template(
        "structure.static_characterization",
        (
            "design_lowest_cost_discriminating_experiment",
            "characterize_class_clusters_without_source_changes",
            "compare_module_change_clusters_with_runtime_consumers",
        ),
        "static_characterization",
        "read_only_process",
        "metadata",
        timeout=120,
        max_items=50,
        attention=5,
        gates=(
            "subject_identity_and_source_snapshot_are_preserved",
            "observations_counterevidence_and_missing_evidence_are_separated",
            "no_source_or_product_state_is_mutated",
        ),
        limitations=(
            "static_characterization_does_not_select_a_refactor",
            "clusters_and_consumers_do_not_prove_product_intent",
        ),
    ),
    _template(
        "state.runtime_sql_trace",
        (
            "trace_sql_and_transaction_events_in_isolation",
            "inject_failure_at_each_durable_boundary",
        ),
        "isolated_fault_injection",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=50,
        attention=10,
        scenarios=("state.text_sql_runtime_trace",),
        runner="trusted_deep_declared_scenarios",
        questions=("state.declared_workflow_sql_matches_implementation",),
        subject_prefixes=("workflow:text.derivation-publication:",),
        gates=(
            "literal_sql_parser_preserves_dynamic_sql_as_missing_evidence",
            "post_terminalization_exception_leaves_no_partial_publication",
            "process_death_before_commit_rolls_back_and_restart_converges",
            "successful_terminal_transaction_contains_required_text_tables",
        ),
        limitations=(
            "runtime_trace_observes_only_selected_paths",
            "fault_injection_does_not_model_power_loss",
        ),
    ),
    _template(
        "capability.public_route_acceptance",
        (
            "exercise_text_capability_from_public_entrypoint",
            "exercise_bounded_route_from_public_entrypoint",
            "observe_user_visible_consumer_of_text_result",
        ),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=20,
        attention=10,
        scenarios=("capability.public_text_route_to_search",),
        runner="trusted_deep_declared_scenarios",
        questions=("capability.route_reaches_user_visible_outcome",),
        subject_prefixes=("capability:route:text",),
        gates=(
            "partial_search_abstention_is_explicit_and_read_only",
            "public_text_entrypoint_first_run_and_replay_observed",
            "same_fixture_source_reaches_public_text_search_output",
        ),
        limitations=(
            "fixture_acceptance_does_not_measure_human_product_value",
            "routes_without_receipts_remain_unattributed",
        ),
    ),
    _template(
        "assurance.targeted_mutation_or_runtime",
        (
            "run_focal_mutation_or_declared_runtime_scenario",
            "seek_negative_control_or_surviving_mutant",
        ),
        "isolated_mutation",
        "disposable_worktree_and_state",
        "deep",
        timeout=600,
        max_items=20,
        attention=15,
        gates=(
            "mutations_are_bounded_to_declared_target",
            "survivors_and_killed_mutants_are_preserved",
            "source_and_canonical_state_are_unchanged",
        ),
        limitations=(
            "mutation_score_is_not_test_quality_probability",
            "selected_mutants_do_not_measure_global_assurance",
        ),
    ),
    _template(
        "evolution.affected_contract",
        (
            "run_smallest_affected_contract_experiment",
            "run_companion_sensitive_contract_test",
        ),
        "isolated_upgrade_matrix",
        "disposable_state_copy",
        "bounded",
        timeout=600,
        max_items=20,
        attention=15,
        gates=(
            "baseline_and_candidate_are_comparable",
            "populated_fixture_or_contract_scenario_is_used",
            "result_records_counterexamples_and_abstentions",
        ),
        limitations=(
            "selected_history_is_not_product_intent",
            "fixture_matrix_is_not_every_deployed_state",
        ),
    ),
    _template(
        "evolution.code_schema_upgrade_matrix",
        ("run_bounded_code_schema_upgrade_matrix",),
        "isolated_upgrade_matrix",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=5,
        attention=10,
        scenarios=("evolution.code_schema_upgrade_matrix",),
        runner="trusted_deep_declared_scenarios",
        questions=("evolution.code_owner_schema_requires_migration_review",),
        subject_prefixes=("code-owner-schema-subject-v1:",),
        gates=(
            "future_schema_is_rejected_without_mutation_or_sidecars",
            "migration_failure_rolls_back_schema_objects_and_existing_facts",
            "oldest_populated_schema_upgrades_preserve_rows_relations_fts_and_reopen",
            "receipt_schema_upgrade_preserves_existing_code_facts",
        ),
        limitations=(
            "fixture_matrix_does_not_cover_every_historical_database_or_filesystem_failure",
            "successful_upgrade_tests_do_not_authorize_an_unreviewed_schema_change",
        ),
    ),
    _template(
        "framework.review_task_protocol_acceptance",
        ("run_framework_review_task_protocol_experiment",),
        "isolated_fault_injection",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=8,
        attention=10,
        scenarios=("framework.review_task_protocol_acceptance",),
        runner="trusted_deep_declared_scenarios",
        questions=("framework.review_task_lifecycle_preserves_atomicity_and_human_authority",),
        subject_prefixes=("contract:framework-review-task-protocol",),
        gates=(
            "exact_human_claim_and_terminal_decision_retries_are_idempotent",
            "faulted_publication_and_event_transactions_preserve_previous_heads",
            "page_publication_is_atomic_resumable_and_idempotent",
            "progress_and_event_heads_reject_stale_compare_and_swap",
            "semantically_changed_retry_is_rejected_as_snapshot_changed",
        ),
        limitations=(
            "bounded_sqlite_fixtures_do_not_observe_every_review_task_producer",
            "injected_exceptions_are_not_process_death_power_loss_or_filesystem_failure",
            "public_cli_actor_is_synthetic_and_not_identity_authenticated",
            "passing_protocol_controls_do_not_create_or_impersonate_a_human_decision",
        ),
    ),
    _template(
        "architecture.boundary_acceptance",
        (
            "run_architecture_boundary_acceptance_scenario",
            "exercise_representative_logical_owner_boundary",
        ),
        "isolated_pytest",
        "disposable_state_copy",
        "bounded",
        timeout=300,
        max_items=20,
        attention=10,
        gates=(
            "declared_owner_mapping_is_resolved",
            "static_and_dynamic_boundary_evidence_are_separated",
            "counterexamples_are_preserved",
        ),
        limitations=(
            "selected_boundary_is_not_complete_runtime_reachability",
            "module_ownership_is_never_inferred_from_names",
        ),
    ),
    _template(
        "architecture.declared_import_contract_acceptance",
        ("exercise_declared_architecture_boundary",),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=3,
        attention=10,
        scenarios=("architecture.declared_import_contract_acceptance",),
        runner="trusted_deep_declared_scenarios",
        questions=("architecture.declared_import_contracts_are_evaluated",),
        subject_prefixes=("architecture:contract:",),
        gates=(
            "declared_boundary_fixture_accepts_required_entrypoints",
            "forbidden_edges_and_cycles_preserve_shortest_chain_and_line_evidence",
            "live_repository_graph_has_no_declared_contract_violation",
            "public_facade_crossings_match_the_explicit_contract",
        ),
        limitations=(
            "declared_import_contracts_do_not_observe_runtime_dispatch_or_plugin_edges",
            "selected_negative_controls_do_not_prove_complete_architectural_intent",
        ),
    ),
    _template(
        "interfaces.public_contract_acceptance",
        (
            "exercise_configuration_override_and_default_scenarios",
            "rerun_interface_surface_projection",
        ),
        "isolated_pytest",
        "disposable_state_copy",
        "focal",
        timeout=180,
        max_items=20,
        attention=5,
        gates=(
            "public_contract_output_is_captured",
            "defaults_overrides_and_dispatch_are_observed",
            "no_configuration_value_is_inferred_from_key_names",
        ),
        limitations=(
            "selected_entrypoints_do_not_cover_every_dynamic_interface",
            "help_output_does_not_prove_product_value",
        ),
    ),
    _template(
        "interfaces.public_cli_contract_acceptance",
        ("execute_public_help_and_dispatch_acceptance_scenarios",),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=26,
        attention=10,
        scenarios=("interfaces.public_cli_and_static_surface",),
        runner="trusted_deep_declared_scenarios",
        questions=("structure.static_cli_calls_require_runtime_contract_evidence",),
        subject_prefixes=("entrypoint:neocortex-interface-surface",),
        gates=(
            "declared_entrypoint_and_effective_help_contract_are_observed",
            "dynamic_hidden_and_static_surfaces_remain_explicitly_non_equivalent",
            "focal_question_and_storage_reads_are_bounded_and_immutable",
            "invalid_abbreviated_and_incomplete_commands_fail_closed_without_state",
            "special_human_canonical_and_flat_dispatch_precedence_is_exact",
        ),
        limitations=(
            "selected_parser_help_and_dispatch_controls_do_not_execute_every_command_handler",
            "stubbed_dispatch_edges_do_not_prove_gui_mcp_worker_or_external_effect_behavior",
            "candidate_wheel_installation_remains_a_separate_code_validation_gate",
            "focal_readers_cover_only_registered_questions_and_bounded_storage_observability",
        ),
    ),
    _template(
        "knowledge.asset_health_causal_acceptance",
        ("run_knowledge_asset_health_causal_experiment",),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=12,
        attention=10,
        scenarios=("knowledge.asset_health_causal_acceptance",),
        runner="trusted_deep_declared_scenarios",
        questions=("knowledge.asset_health_trace_is_snapshot_bound_and_causally_explainable",),
        subject_prefixes=("capability:knowledge-asset-health",),
        gates=(
            "aligned_four_stage_causal_trace_is_healthy",
            "mismatch_absence_future_corruption_and_unpublished_fail_closed",
            "public_read_is_read_only_and_resource_identity_is_strict",
            "snapshot_change_abstains_and_search_health_identity_is_stable",
        ),
        limitations=(
            "isolated_text_owner_fixtures_do_not_prove_every_knowledge_route_owner",
            "sqlite_snapshot_and_wal_controls_do_not_prove_distributed_power_loss_safety",
            "a_passed_receipt_remains_advisory_and_cannot_authorize_corpus_or_state_mutation",
        ),
    ),
    _template(
        "knowledge.pdf_asset_health_causal_acceptance",
        ("run_knowledge_pdf_asset_health_causal_experiment",),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=12,
        attention=10,
        scenarios=("knowledge.pdf_asset_health_causal_acceptance",),
        runner="trusted_deep_declared_scenarios",
        questions=(
            "knowledge.pdf_asset_health_preserves_page_partial_protected_and_recovery_causality",
        ),
        subject_prefixes=("capability:knowledge-asset-health:pdf",),
        gates=(
            "page_staging_fts_and_catalog_mismatch_fail_closed",
            "recovery_is_version_and_message_independent",
            "typed_pdf_states_preserve_partial_and_protected_semantics",
            "wal_snapshot_and_owner_ambiguity_remain_read_only",
        ),
        limitations=(
            "bounded_pdf_fixtures_do_not_prove_ocr_visual_or_semantic_content_fidelity",
            "process_recovery_controls_do_not_prove_power_loss_or_filesystem_failure_safety",
            "a_passed_receipt_remains_advisory_and_cannot_authorize_corpus_or_state_mutation",
        ),
    ),
    _template(
        "retention.durable_hold_safety",
        ("run_retention_hold_safety_experiment",),
        "isolated_pytest",
        "pytest_tmp_path",
        "bounded",
        timeout=300,
        max_items=14,
        attention=10,
        scenarios=("retention.durable_hold_safety",),
        runner="trusted_deep_declared_scenarios",
        questions=("retention.dry_run_preserves_declared_durable_holds",),
        subject_prefixes=("retention:canonical-durable-holds",),
        gates=(
            "current_previous_builders_leases_and_human_evidence_are_protected",
            "dry_run_never_supports_deletion_and_preserves_phase_order",
            "incomplete_review_receipt_or_schema_drift_fails_closed_without_mutation",
            "reader_snapshot_does_not_mix_concurrent_owner_commit",
        ),
        limitations=(
            "bounded_owner_fixtures_do_not_prove_power_loss",
            "passing_dry_run_controls_do_not_authorize_or_validate_a_future_delete_executor",
            "owner_snapshots_are_not_a_cross_database_atomic_snapshot",
        ),
    ),
    _template(
        "security.bounded_boundary_scenarios",
        (
            "execute_bounded_security_boundary_scenarios",
            "compare_built_artifact_with_lock_and_installed_inventory",
            "run_missing_or_stale_security_providers",
            "run_missing_dependency_and_inventory_providers",
        ),
        "isolated_pytest",
        "pytest_tmp_path",
        "deep",
        timeout=600,
        max_items=10,
        attention=15,
        scenarios=("security.supply_chain_gate_controls",),
        runner="trusted_deep_declared_scenarios",
        questions=(
            "dependency.declaration_installation_and_license_evidence_is_resolved",
            "security.static_invariants_and_vulnerability_evidence_is_resolved",
        ),
        subject_prefixes=(
            "dependency:neocortex-environment",
            "project:neocortex-security-evidence",
        ),
        gates=(
            "bounded_local_staging_rejects_unowned_inputs",
            "dependency_declaration_inventory_record_and_license_evidence_are_correlated",
            "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
            "pip_audit_contract_records_bounded_phase_complete_result",
            "provider_environment_strips_credentials_and_disables_networked_modes",
            "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
            "source_only_dependency_is_hash_pinned_and_built_without_installing",
        ),
        limitations=(
            "selected_security_scenarios_do_not_prove_absence_of_vulnerabilities",
            "known_vulnerability_feeds_are_time_bound",
            "bounded_dependency_fixtures_do_not_replace_candidate_wheel_install_and_replay",
        ),
    ),
    _template(
        "analyzer.seeded_holdout",
        (
            "run_seeded_holdout_and_negative_control_calibration",
            "rerun_bounded_self_analysis_then_compare_again",
        ),
        "isolated_pytest",
        "disposable_worktree_and_state",
        "bounded",
        timeout=600,
        max_items=50,
        attention=20,
        gates=(
            "positive_negative_and_holdout_partitions_are_distinct",
            "labels_are_independent_of_detector_output",
            "renames_moves_wrappers_and_metric_dilution_are_tested",
        ),
        limitations=(
            "seeded_defects_are_not_all_real_world_defects",
            "holdout_quality_depends_on_independent_labels",
        ),
    ),
)


def _validate_registry() -> None:
    template_ids = tuple(item.template_id for item in CODE_EXPERIMENT_TEMPLATES)
    if template_ids != tuple(sorted(template_ids)) or len(set(template_ids)) != len(template_ids):
        raise ValueError("experiment template registry must be sorted and unique")
    action_ids = [action for item in CODE_EXPERIMENT_TEMPLATES for action in item.action_ids]
    if len(set(action_ids)) != len(action_ids):
        raise ValueError("experiment action identifiers cannot map to multiple templates")


# Keep the registry order canonical while declarations remain grouped by domain.
CODE_EXPERIMENT_TEMPLATES = tuple(
    sorted(CODE_EXPERIMENT_TEMPLATES, key=lambda item: item.template_id)
)
_validate_registry()


def experiment_template_registry_payload() -> dict[str, object]:
    return {
        "schema": CODE_EXPERIMENT_TEMPLATE_REGISTRY_SCHEMA,
        "templates": tuple(asdict(item) for item in CODE_EXPERIMENT_TEMPLATES),
    }


def experiment_template_registry_fingerprint() -> str:
    return analysis_identity(
        "code-experiment-template-registry-v11",
        experiment_template_registry_payload(),
    )


@dataclass(frozen=True, slots=True)
class CodeExperimentProposal:
    proposal_id: str
    evaluation_id: str
    evaluation_binding_fingerprint: str
    question_id: str
    subject_key: str
    selected_action_id: str | None
    template_id: str | None
    template_version: str | None
    cost_tier: CostTier | None
    estimated_attention_minutes: int | None
    timeout_seconds: int | None
    max_items: int | None
    isolation: IsolationKind | None
    runner_kind: RunnerKind | None
    scenario_ids: tuple[str, ...]
    acceptance_gates: tuple[str, ...]
    missing_requirement_ids: tuple[str, ...]
    alternative_action_ids: tuple[str, ...]
    planning_status: Literal["planned", "registry_gap", "not_required"]
    reason: str
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("experiment proposal id", self.proposal_id),
            ("experiment evaluation id", self.evaluation_id),
            ("experiment evaluation binding", self.evaluation_binding_fingerprint),
            ("experiment question id", self.question_id),
            ("experiment subject key", self.subject_key),
            ("experiment proposal reason", self.reason),
        ):
            _required(label, value, 2_048)
        _texts("experiment proposal scenario", self.scenario_ids, sorted_values=True)
        _texts("experiment proposal gate", self.acceptance_gates)
        _texts("missing requirement id", self.missing_requirement_ids, sorted_values=True)
        _texts("alternative action id", self.alternative_action_ids)
        if self.planning_status not in {"planned", "registry_gap", "not_required"}:
            raise ValueError("experiment proposal status is invalid")
        optionals = (
            self.selected_action_id,
            self.template_id,
            self.template_version,
            self.cost_tier,
            self.estimated_attention_minutes,
            self.timeout_seconds,
            self.max_items,
            self.isolation,
            self.runner_kind,
        )
        if self.planning_status == "planned":
            if any(value is None for value in optionals):
                raise ValueError("planned experiment requires a complete registered template")
            template = experiment_template(cast(str, self.template_id))
            if (
                self.selected_action_id not in template.action_ids
                or self.template_version != template.version
                or self.cost_tier != template.cost_tier
                or self.estimated_attention_minutes != template.estimated_attention_minutes
                or self.timeout_seconds != template.timeout_seconds
                or self.max_items != template.max_items
                or self.isolation != template.isolation
                or self.runner_kind != template.runner_kind
                or self.scenario_ids != template.scenario_ids
                or self.acceptance_gates != template.acceptance_gates
                or not template.applies_to(
                    question_id=self.question_id,
                    subject_key=self.subject_key,
                )
            ):
                raise ValueError("experiment proposal is not derived from its template")
        elif (
            any(value is not None for value in optionals)
            or self.scenario_ids
            or self.acceptance_gates
        ):
            raise ValueError("unplanned experiment proposal cannot claim execution details")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("experiment proposals must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-experiment-proposal-v2",
            {
                key: value
                for key, value in asdict(self).items()
                if key not in {"proposal_id", "evaluation_id"}
            },
        )
        if self.proposal_id != expected_id:
            raise ValueError("experiment proposal identity is invalid")


@dataclass(frozen=True, slots=True)
class CodeExperimentPlan:
    plan_id: str
    status: Literal["ready", "partial", "not_required", "abstained"]
    reason: str | None
    policy_id: str
    registry_fingerprint: str
    source_evaluation_count: int
    experiment_required_count: int
    planned_count: int
    executable_count: int
    registry_gap_count: int
    proposals: tuple[CodeExperimentProposal, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("experiment plan id", self.plan_id)
        if self.status not in {"ready", "partial", "not_required", "abstained"}:
            raise ValueError("experiment plan status is invalid")
        if self.policy_id != CODE_EXPERIMENT_PLANNING_POLICY:
            raise ValueError("experiment planning policy is invalid")
        if self.registry_fingerprint != experiment_template_registry_fingerprint():
            raise ValueError("experiment plan registry fingerprint is invalid")
        for label, value in (
            ("source evaluations", self.source_evaluation_count),
            ("experiment-required evaluations", self.experiment_required_count),
            ("planned proposals", self.planned_count),
            ("executable proposals", self.executable_count),
            ("registry gaps", self.registry_gap_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"experiment plan {label} must be non-negative")
        if not isinstance(self.proposals, tuple) or any(
            not isinstance(item, CodeExperimentProposal) for item in self.proposals
        ):
            raise ValueError("experiment plan proposals are invalid")
        if len(self.proposals) > CODE_EXPERIMENT_MAX_PROPOSALS:
            raise ValueError("experiment plan exceeds its proposal bound")
        if tuple(item.proposal_id for item in self.proposals) != tuple(
            sorted(item.proposal_id for item in self.proposals)
        ):
            raise ValueError("experiment proposals must be deterministically ordered")
        if len({item.evaluation_id for item in self.proposals}) != len(self.proposals):
            raise ValueError("experiment plan can contain at most one proposal per evaluation")
        derived_required = sum(item.planning_status != "not_required" for item in self.proposals)
        derived_planned = sum(item.planning_status == "planned" for item in self.proposals)
        derived_executable = sum(
            item.planning_status == "planned" and item.runner_kind != "none"
            for item in self.proposals
        )
        derived_gaps = sum(item.planning_status == "registry_gap" for item in self.proposals)
        if (
            self.experiment_required_count != derived_required
            or self.planned_count != derived_planned
            or self.executable_count != derived_executable
            or self.registry_gap_count != derived_gaps
        ):
            raise ValueError("experiment plan counts are not derived from proposals")
        expected_status = (
            "abstained"
            if self.source_evaluation_count > CODE_EXPERIMENT_MAX_PROPOSALS
            else "not_required"
            if self.experiment_required_count == 0
            else "partial"
            if self.registry_gap_count
            else "ready"
        )
        if self.status != expected_status:
            raise ValueError("experiment plan status is not derived from coverage")
        if self.status in {"not_required", "abstained"}:
            _required("experiment plan reason", self.reason, 256)
        elif self.reason is not None:
            raise ValueError("ready or partial experiment plan cannot carry a reason")
        _texts("experiment plan limitation", self.limitations)
        if not self.limitations:
            raise ValueError("experiment plan requires limitations")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("experiment plan must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-experiment-plan-v2",
            {key: value for key, value in asdict(self).items() if key != "plan_id"},
        )
        if self.plan_id != expected_id:
            raise ValueError("experiment plan identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_EXPERIMENT_PLAN_SCHEMA, **asdict(self)}


def experiment_template(template_id: str) -> CodeExperimentTemplate:
    selected = _required("experiment template id", template_id, 256)
    match = next((item for item in CODE_EXPERIMENT_TEMPLATES if item.template_id == selected), None)
    if match is None:
        raise ValueError(f"unknown experiment template: {selected}")
    return match


def _template_by_action() -> dict[str, CodeExperimentTemplate]:
    return {
        action: template for template in CODE_EXPERIMENT_TEMPLATES for action in template.action_ids
    }


def experiment_evaluation_binding_fingerprint(
    evaluation: AnalysisQuestionEvaluation,
) -> str:
    """Fingerprint decision-relevant evidence without capture-local identities.

    Evaluation, evidence, snapshot and provider-run IDs identify one capture.
    They must not make an otherwise exact replay look like a new experiment.
    Facts, contracts, completeness, requirement outcomes and the stable subject
    do remain in the binding so a real semantic or state change invalidates the
    previous receipt.
    """

    if not isinstance(evaluation, AnalysisQuestionEvaluation):
        raise ValueError("experiment evaluation binding input is invalid")
    evidence_projection: dict[str, dict[str, object]] = {}
    for evidence in evaluation.evidence:
        semantic: dict[str, object] = {
            "subject_key": evidence.subject_key,
            "role": evidence.role,
            "evidence_kind": evidence.evidence_kind,
            "source_owner_id": evidence.source_owner_id,
            "producer_id": evidence.producer_id,
            "producer_version": evidence.producer_version,
            "source_schema": evidence.source_schema,
            "source_record_kind": evidence.source_record_kind,
            "revision_id": evidence.revision_id,
            "facts": tuple(
                asdict(item) for item in sorted(evidence.facts, key=lambda item: item.name)
            ),
            "completeness": evidence.completeness,
            "bounded": evidence.bounded,
            "truncated": evidence.truncated,
            "resolver_id": evidence.resolver_id,
            "resolver_version": evidence.resolver_version,
            "resolution_status": evidence.resolution_status,
            "limitations": tuple(sorted(evidence.limitations)),
            "authority": evidence.authority,
            "mutation_authority": evidence.mutation_authority,
        }
        evidence_projection[evidence.evidence_id] = semantic
    requirements = tuple(
        {
            "requirement_id": item.requirement_id,
            "status": item.status,
            "reason": item.reason,
            "evidence": tuple(
                sorted(
                    analysis_identity(
                        "code-experiment-evidence-binding-v1",
                        evidence_projection[evidence_id],
                    )
                    for evidence_id in item.evidence_ids
                )
            ),
        }
        for item in evaluation.requirements
    )
    subject = evaluation.subject
    return analysis_identity(
        "code-experiment-evaluation-binding-v1",
        {
            "question_id": evaluation.question_id,
            "question_version": evaluation.question_version,
            "question_spec_fingerprint": evaluation.question_spec_fingerprint,
            "subject": {
                "kind": subject.subject_kind,
                "key": subject.subject_key,
                "source_owner_id": subject.source_owner_id,
                "snapshot_freshness": subject.snapshot_freshness,
                "revision_id": subject.revision_id,
                "location": None if subject.location is None else asdict(subject.location),
            },
            "requirements": requirements,
            "observation_status": evaluation.observation_status,
            "inference_status": evaluation.inference_status,
            "hypotheses": evaluation.hypotheses,
            "question_readiness": evaluation.question_readiness,
            "decision_readiness": evaluation.decision_readiness,
            "decision_reason": evaluation.decision_reason,
            "counterevidence_status": evaluation.counterevidence_status,
            "next_action_ids": evaluation.next_action_ids,
            "limitations": tuple(sorted(evaluation.limitations)),
            "authority": evaluation.authority,
            "mutation_authority": evaluation.mutation_authority,
        },
    )


def _proposal(
    evaluation: AnalysisQuestionEvaluation,
    *,
    template: CodeExperimentTemplate | None,
    selected_action: str | None,
) -> CodeExperimentProposal:
    missing = tuple(
        sorted(
            item.requirement_id for item in evaluation.requirements if item.status != "satisfied"
        )
    )
    alternatives = tuple(
        action for action in evaluation.next_action_ids if action != selected_action
    )
    values: dict[str, object] = {
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_binding_fingerprint": experiment_evaluation_binding_fingerprint(evaluation),
        "question_id": evaluation.question_id,
        "subject_key": evaluation.subject.subject_key,
        "selected_action_id": selected_action,
        "template_id": None if template is None else template.template_id,
        "template_version": None if template is None else template.version,
        "cost_tier": None if template is None else template.cost_tier,
        "estimated_attention_minutes": (
            None if template is None else template.estimated_attention_minutes
        ),
        "timeout_seconds": None if template is None else template.timeout_seconds,
        "max_items": None if template is None else template.max_items,
        "isolation": None if template is None else template.isolation,
        "runner_kind": None if template is None else template.runner_kind,
        "scenario_ids": () if template is None else template.scenario_ids,
        "acceptance_gates": () if template is None else template.acceptance_gates,
        "missing_requirement_ids": missing,
        "alternative_action_ids": alternatives,
        "planning_status": "registry_gap" if template is None else "planned",
        "reason": (
            "no_registered_experiment_template_for_any_next_action"
            if template is None
            else "cheapest_registered_discriminating_experiment_selected"
        ),
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeExperimentProposal(
        proposal_id=analysis_identity(
            "code-experiment-proposal-v2",
            {key: value for key, value in values.items() if key != "evaluation_id"},
        ),
        **values,  # type: ignore[arg-type]
    )


def plan_code_experiments(
    specs: tuple[AnalysisQuestionSpec, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
) -> CodeExperimentPlan:
    validate_analysis_question_set(specs, evaluations)
    specs_by_id = {(item.question_id, item.version): item for item in specs}
    for evaluation in evaluations:
        validate_analysis_question_evaluation(
            specs_by_id[(evaluation.question_id, evaluation.question_version)],
            evaluation,
        )
    limitations = (
        "plan_selects_only_registered_templates_not_free_form_commands",
        "proposal_is_not_execution_result_or_change_authority",
        "characterization_and_counterevidence_actions_remain_visible_as_alternatives",
        "runner_executes_only_explicit_source_versioned_scenarios_in_disposable_state",
    )
    if len(evaluations) > CODE_EXPERIMENT_MAX_PROPOSALS:
        values: dict[str, object] = {
            "status": "abstained",
            "reason": "evaluation_bound_exceeded",
            "policy_id": CODE_EXPERIMENT_PLANNING_POLICY,
            "registry_fingerprint": experiment_template_registry_fingerprint(),
            "source_evaluation_count": len(evaluations),
            "experiment_required_count": 0,
            "planned_count": 0,
            "executable_count": 0,
            "registry_gap_count": 0,
            "proposals": (),
            "limitations": limitations,
            "authority": "advisory",
            "mutation_authority": False,
        }
    else:
        by_action = _template_by_action()
        proposals: list[CodeExperimentProposal] = []
        for evaluation in evaluations:
            if evaluation.decision_readiness != "experiment_required":
                continue
            candidates = tuple(
                (action, by_action[action])
                for action in evaluation.next_action_ids
                if action in by_action
                and by_action[action].applies_to(
                    question_id=evaluation.question_id,
                    subject_key=evaluation.subject.subject_key,
                )
            )
            selected_action: str | None = None
            selected_template: CodeExperimentTemplate | None = None
            if candidates:
                selected_action, selected_template = min(
                    candidates,
                    key=lambda item: (
                        _COST_ORDER[item[1].cost_tier],
                        item[1].estimated_attention_minutes,
                        item[1].timeout_seconds,
                        item[1].template_id,
                        item[0],
                    ),
                )
            proposals.append(
                _proposal(
                    evaluation,
                    template=selected_template,
                    selected_action=selected_action,
                )
            )
        ordered = tuple(sorted(proposals, key=lambda item: item.proposal_id))
        required = len(ordered)
        planned = sum(item.planning_status == "planned" for item in ordered)
        executable = sum(
            item.planning_status == "planned" and item.runner_kind != "none" for item in ordered
        )
        gaps = sum(item.planning_status == "registry_gap" for item in ordered)
        values = {
            "status": "not_required" if not required else "partial" if gaps else "ready",
            "reason": "no_evaluation_requires_an_experiment" if not required else None,
            "policy_id": CODE_EXPERIMENT_PLANNING_POLICY,
            "registry_fingerprint": experiment_template_registry_fingerprint(),
            "source_evaluation_count": len(evaluations),
            "experiment_required_count": required,
            "planned_count": planned,
            "executable_count": executable,
            "registry_gap_count": gaps,
            "proposals": ordered,
            "limitations": limitations,
            "authority": "advisory",
            "mutation_authority": False,
        }
    identity_values = dict(values)
    identity_values["proposals"] = tuple(
        asdict(item) if isinstance(item, CodeExperimentProposal) else item
        for item in cast(tuple[object, ...], values["proposals"])
    )
    return CodeExperimentPlan(
        plan_id=analysis_identity("code-experiment-plan-v2", identity_values),
        **values,  # type: ignore[arg-type]
    )


def parse_code_experiment_plan_payload(payload: Mapping[str, object]) -> CodeExperimentPlan:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_EXPERIMENT_PLAN_SCHEMA:
        raise ValueError("experiment plan payload schema is invalid")
    expected = {field.name for field in fields(CodeExperimentPlan)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("experiment plan payload fields are invalid")
    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, Sequence) or isinstance(
        raw_proposals, (str, bytes, bytearray)
    ):
        raise ValueError("experiment plan proposals are invalid")
    proposal_fields = {field.name for field in fields(CodeExperimentProposal)}
    proposals: list[CodeExperimentProposal] = []
    for raw in raw_proposals:
        if not isinstance(raw, Mapping) or set(raw) != proposal_fields:
            raise ValueError("experiment proposal payload fields are invalid")
        values = dict(raw)
        for key in (
            "scenario_ids",
            "acceptance_gates",
            "missing_requirement_ids",
            "alternative_action_ids",
        ):
            values[key] = _texts(
                f"experiment proposal {key}",
                values[key],
                sorted_values=key in {"scenario_ids", "missing_requirement_ids"},
            )
        proposals.append(CodeExperimentProposal(**cast(Any, values)))
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["proposals"] = tuple(proposals)
    values["limitations"] = _texts("experiment plan limitation", values["limitations"])
    return CodeExperimentPlan(**cast(Any, values))


__all__ = [
    "CODE_EXPERIMENT_MAX_PROPOSALS",
    "CODE_EXPERIMENT_PLANNING_POLICY",
    "CODE_EXPERIMENT_PLAN_SCHEMA",
    "CODE_EXPERIMENT_TEMPLATES",
    "CODE_EXPERIMENT_TEMPLATE_REGISTRY_SCHEMA",
    "CodeExperimentPlan",
    "CodeExperimentProposal",
    "CodeExperimentTemplate",
    "experiment_evaluation_binding_fingerprint",
    "experiment_template",
    "experiment_template_registry_fingerprint",
    "experiment_template_registry_payload",
    "parse_code_experiment_plan_payload",
    "plan_code_experiments",
]
