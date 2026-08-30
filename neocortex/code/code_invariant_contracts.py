"""Versioned invariant and runtime-scenario declarations for Code assurance.

The registry links exact pytest nodeids to deliberately named invariants.  A
passing nodeid is evidence that one declared scenario executed successfully;
it is never promoted to a proof that the invariant holds for all executions.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Literal

from .code_analysis_epistemics import analysis_identity

CODE_INVARIANT_REGISTRY_SCHEMA = "neocortex.code-invariant-registry/v3"
CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA = "neocortex.code-runtime-scenario-registry/v11"

_PUBLIC_CLI_FLAT_INVALID_NODEIDS = tuple(
    "tests/test_code_observability_cli.py::"
    f"test_flat_observability_arguments_fail_closed[{parameter_id}]"
    for parameter_id in (
        "arguments0-requires --code-question",
        "arguments1-non-empty trimmed text",
        "arguments2-between 1 and 50",
        "arguments3-require --code-storage",
        "arguments4-between 1 and 1000000",
        "arguments5-between 1 and --code-storage-run-limit",
    )
)


def _required(label: str, value: object, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeScenarioGateSpec:
    gate_id: str
    test_nodeids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required("runtime scenario gate id", self.gate_id, 256)
        if (
            not self.test_nodeids
            or len(set(self.test_nodeids)) != len(self.test_nodeids)
            or self.test_nodeids
            != tuple(sorted(self.test_nodeids, key=lambda item: (item.casefold(), item)))
        ):
            raise ValueError("runtime scenario gate nodeids must be non-empty, unique, and ordered")
        for nodeid in self.test_nodeids:
            _required("runtime scenario gate nodeid", nodeid, 16_384)


@dataclass(frozen=True, slots=True)
class RuntimeScenarioSpec:
    scenario_id: str
    version: str
    test_nodeids: tuple[str, ...]
    scenario_kind: Literal["state_fixture", "process_death", "metamorphic"]
    isolation: Literal["pytest_tmp_path", "spawned_process_and_tmp_path"]
    limitation: str
    gate_specs: tuple[RuntimeScenarioGateSpec, ...] = ()

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("scenario id", self.scenario_id, 256),
            ("scenario version", self.version, 64),
            ("scenario limitation", self.limitation, 512),
        ):
            _required(label, value, maximum)
        if (
            not self.test_nodeids
            or len(set(self.test_nodeids)) != len(self.test_nodeids)
            or self.test_nodeids
            != tuple(sorted(self.test_nodeids, key=lambda item: (item.casefold(), item)))
        ):
            raise ValueError("runtime scenario nodeids must be non-empty, unique, and ordered")
        for test_nodeid in self.test_nodeids:
            _required("scenario test nodeid", test_nodeid, 16_384)
            if "::test_" not in test_nodeid:
                raise ValueError("runtime scenario must bind exact pytest tests")
        if self.scenario_kind not in {"state_fixture", "process_death", "metamorphic"}:
            raise ValueError("runtime scenario kind is invalid")
        if self.isolation not in {"pytest_tmp_path", "spawned_process_and_tmp_path"}:
            raise ValueError("runtime scenario isolation is invalid")
        if not isinstance(self.gate_specs, tuple) or any(
            not isinstance(item, RuntimeScenarioGateSpec) for item in self.gate_specs
        ):
            raise ValueError("runtime scenario gate specs are invalid")
        gate_ids = tuple(item.gate_id for item in self.gate_specs)
        if gate_ids != tuple(sorted(gate_ids)) or len(set(gate_ids)) != len(gate_ids):
            raise ValueError("runtime scenario gate ids must be unique and ordered")
        if any(not set(item.test_nodeids) <= set(self.test_nodeids) for item in self.gate_specs):
            raise ValueError("runtime scenario gate references an undeclared nodeid")


@dataclass(frozen=True, slots=True)
class InvariantSpec:
    invariant_id: str
    version: str
    statement: str
    scope: Literal["state", "publication", "analyzer"]
    scenario_ids: tuple[str, ...]
    failure_impact: Literal["consistency", "publication", "analyzer_integrity"]

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("invariant id", self.invariant_id, 256),
            ("invariant version", self.version, 64),
            ("invariant statement", self.statement, 2_048),
        ):
            _required(label, value, maximum)
        if self.scope not in {"state", "publication", "analyzer"}:
            raise ValueError("invariant scope is invalid")
        if not self.scenario_ids or len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError("invariant scenarios must be non-empty and unique")
        if self.failure_impact not in {"consistency", "publication", "analyzer_integrity"}:
            raise ValueError("invariant failure impact is invalid")


_RETENTION_INCOMPLETE_REVIEW_RECEIPT_NODEIDS = tuple(
    "tests/test_retention_planner.py::"
    "test_framework_retention_fails_closed_on_incomplete_review_source_receipt"
    f"[{case}]"
    for case in (
        "batch_cursor",
        "membership",
        "membership_binding",
        "progress",
        "progress_mismatch",
        "source_receipt",
    )
)


RUNTIME_SCENARIOS = (
    RuntimeScenarioSpec(
        scenario_id="analyzer.calibration_and_worktree_controls",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_analyzer_calibration.py::"
                "test_antigoodhart_receipt_is_linked_without_overstating_general_invariance"
            ),
            (
                "tests/test_code_analyzer_effectiveness.py::"
                "test_exact_visible_checkout_is_observed_without_claiming_calibration"
            ),
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation="seeded_and_checkout_controls_do_not_establish_real_world_precision_or_recall",
    ),
    RuntimeScenarioSpec(
        scenario_id="analyzer.hotspot_name_path_call_invariance",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/semantic_lineage_reader.py-"
                "semantic_lineage_reader.lookup_text_chunk_lineage-outgoing_calls0]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/semantic_lineage_repository.py-"
                "semantic_lineage_repository.run_text_chunk_lineage-outgoing_calls2]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/semantic_lineage_store.py-"
                "semantic_lineage_store.build_text_chunk_lineage-outgoing_calls1]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/state_writer.py-state_writer.persist_and_enqueue-outgoing_calls3]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/value_review_repository.py-"
                "value_review_repository._observation-outgoing_calls5]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/pkg/value_review_tasks.py-"
                "value_review_tasks.read_value_review_task_queue-outgoing_calls4]"
            ),
            (
                "tests/test_code_review_epistemics.py::"
                "test_names_paths_and_outgoing_call_spellings_cannot_change_epistemic_state"
                "[/repo/tests/test_semantic_lineage.py-"
                "test_semantic_lineage.compute-outgoing_calls6]"
            ),
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation="declared_transformations_only_not_general_detector_invariance",
    ),
    RuntimeScenarioSpec(
        scenario_id="architecture.declared_boundary_and_owner_mapping",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_architecture_questions.py::"
                "test_path_namespace_rename_does_not_change_explicit_owner_projection_or_authority"
            ),
            (
                "tests/test_code_architecture_questions.py::"
                "test_ready_architecture_exposes_graph_contract_and_owner_gap_without_a_decision"
            ),
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation="selected_static_boundaries_do_not_establish_complete_runtime_reachability",
    ),
    RuntimeScenarioSpec(
        scenario_id="architecture.declared_import_contract_acceptance",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_architecture_contracts.py::"
                "test_declared_boundary_entry_points_pass_with_acyclic_v5_baseline"
            ),
            (
                "tests/test_code_architecture_contracts.py::"
                "test_live_repository_graph_satisfies_published_architecture_contracts"
            ),
            (
                "tests/test_code_architecture_contracts.py::"
                "test_violations_expose_shortest_chains_lines_and_new_cycle"
            ),
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation=(
            "declared_static_import_contracts_and_selected_negative_controls_do_not_"
            "establish_complete_runtime_dependency_reachability"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "declared_boundary_fixture_accepts_required_entrypoints",
                (
                    "tests/test_code_architecture_contracts.py::"
                    "test_declared_boundary_entry_points_pass_with_acyclic_v5_baseline",
                ),
            ),
            RuntimeScenarioGateSpec(
                "forbidden_edges_and_cycles_preserve_shortest_chain_and_line_evidence",
                (
                    "tests/test_code_architecture_contracts.py::"
                    "test_violations_expose_shortest_chains_lines_and_new_cycle",
                ),
            ),
            RuntimeScenarioGateSpec(
                "live_repository_graph_has_no_declared_contract_violation",
                (
                    "tests/test_code_architecture_contracts.py::"
                    "test_live_repository_graph_satisfies_published_architecture_contracts",
                ),
            ),
            RuntimeScenarioGateSpec(
                "public_facade_crossings_match_the_explicit_contract",
                (
                    "tests/test_code_architecture_contracts.py::"
                    "test_live_repository_graph_satisfies_published_architecture_contracts",
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="capability.public_text_route_to_search",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_public_route_experiments.py::"
                "test_public_text_route_replays_and_reaches_search_output"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation="one_text_fixture_proves_a_public_path_not_human_product_value_or_other_routes",
        gate_specs=(
            RuntimeScenarioGateSpec(
                "partial_search_abstention_is_explicit_and_read_only",
                (
                    "tests/test_code_public_route_experiments.py::"
                    "test_public_text_route_replays_and_reaches_search_output",
                ),
            ),
            RuntimeScenarioGateSpec(
                "public_text_entrypoint_first_run_and_replay_observed",
                (
                    "tests/test_code_public_route_experiments.py::"
                    "test_public_text_route_replays_and_reaches_search_output",
                ),
            ),
            RuntimeScenarioGateSpec(
                "same_fixture_source_reaches_public_text_search_output",
                (
                    "tests/test_code_public_route_experiments.py::"
                    "test_public_text_route_replays_and_reaches_search_output",
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="evolution.change_surface_antigoodhart_controls",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_change_evolution_analysis.py::"
                "test_change_history_schema_vertical_preserves_epistemic_boundaries"
            ),
            (
                "tests/test_code_change_evolution_analysis.py::"
                "test_corrected_call_resolution_cannot_claim_change_success"
            ),
        ),
        scenario_kind="metamorphic",
        isolation="pytest_tmp_path",
        limitation="fixture_history_does_not_reconstruct_unobserved_product_intent",
    ),
    RuntimeScenarioSpec(
        scenario_id="evolution.code_schema_upgrade_matrix",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_experiment_store.py::"
                "test_populated_code_v5_migrates_to_versioned_receipts_without_fact_drift"
            ),
            (
                "tests/test_code_experiment_store.py::"
                "test_v5_to_v6_migration_failure_rolls_back_every_receipt_object"
            ),
            (
                "tests/test_code_schema_migration_v1_v2.py::"
                "test_v1_to_current_migration_failure_rolls_back_ddl_and_data"
            ),
            (
                "tests/test_code_schema_migration_v1_v2.py::"
                "test_v1_to_current_migration_preserves_rows_relations_and_fts"
            ),
            (
                "tests/test_framework_code_path_collation.py::"
                "test_code_future_schema_is_rejected_read_only_without_sidecars"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "bounded_populated_code_schema_fixtures_do_not_cover_every_historical_"
            "database_or_filesystem_failure"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "future_schema_is_rejected_without_mutation_or_sidecars",
                (
                    "tests/test_framework_code_path_collation.py::"
                    "test_code_future_schema_is_rejected_read_only_without_sidecars",
                ),
            ),
            RuntimeScenarioGateSpec(
                "migration_failure_rolls_back_schema_objects_and_existing_facts",
                (
                    "tests/test_code_experiment_store.py::"
                    "test_v5_to_v6_migration_failure_rolls_back_every_receipt_object",
                    "tests/test_code_schema_migration_v1_v2.py::"
                    "test_v1_to_current_migration_failure_rolls_back_ddl_and_data",
                ),
            ),
            RuntimeScenarioGateSpec(
                "oldest_populated_schema_upgrades_preserve_rows_relations_fts_and_reopen",
                (
                    "tests/test_code_schema_migration_v1_v2.py::"
                    "test_v1_to_current_migration_preserves_rows_relations_and_fts",
                ),
            ),
            RuntimeScenarioGateSpec(
                "receipt_schema_upgrade_preserves_existing_code_facts",
                (
                    "tests/test_code_experiment_store.py::"
                    "test_populated_code_v5_migrates_to_versioned_receipts_without_fact_drift",
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="framework.review_task_protocol_acceptance",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_framework_review_task_experiments.py::"
                "test_public_entrypoint_review_task_journey_preserves_cas_idempotency_"
                "and_human_authority"
            ),
            (
                "tests/test_review_task_cli_adapter.py::"
                "test_review_task_cli_changed_command_is_not_mistaken_for_retry"
            ),
            (
                "tests/test_review_task_cli_adapter.py::"
                "test_review_task_cli_claim_decide_history_and_exact_retries"
            ),
            (
                "tests/test_review_tasks.py::"
                "test_event_crash_rolls_back_without_changing_current_head"
            ),
            ("tests/test_review_tasks.py::test_event_transition_is_cas_append_only_and_idempotent"),
            (
                "tests/test_review_tasks.py::"
                "test_final_page_crash_cannot_publish_source_head_or_partial_supersession"
            ),
            ("tests/test_review_tasks.py::test_progress_cursor_and_revision_are_exact_cas"),
            ("tests/test_review_tasks.py::test_publish_page_is_atomic_resumable_and_idempotent"),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "bounded_sqlite_fixtures_and_injected_exceptions_do_not_model_process_death_"
            "power_loss_or_every_review_task_producer;public_cli_actor_is_synthetic_"
            "and_not_identity_authenticated"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "exact_human_claim_and_terminal_decision_retries_are_idempotent",
                (
                    (
                        "tests/test_code_framework_review_task_experiments.py::"
                        "test_public_entrypoint_review_task_journey_preserves_cas_"
                        "idempotency_and_human_authority"
                    ),
                    (
                        "tests/test_review_task_cli_adapter.py::"
                        "test_review_task_cli_claim_decide_history_and_exact_retries"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "faulted_publication_and_event_transactions_preserve_previous_heads",
                (
                    (
                        "tests/test_review_tasks.py::"
                        "test_event_crash_rolls_back_without_changing_current_head"
                    ),
                    (
                        "tests/test_review_tasks.py::"
                        "test_final_page_crash_cannot_publish_source_head_or_partial_supersession"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "page_publication_is_atomic_resumable_and_idempotent",
                (
                    (
                        "tests/test_review_tasks.py::"
                        "test_publish_page_is_atomic_resumable_and_idempotent"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "progress_and_event_heads_reject_stale_compare_and_swap",
                (
                    (
                        "tests/test_review_tasks.py::"
                        "test_event_transition_is_cas_append_only_and_idempotent"
                    ),
                    ("tests/test_review_tasks.py::test_progress_cursor_and_revision_are_exact_cas"),
                ),
            ),
            RuntimeScenarioGateSpec(
                "semantically_changed_retry_is_rejected_as_snapshot_changed",
                (
                    (
                        "tests/test_review_task_cli_adapter.py::"
                        "test_review_task_cli_changed_command_is_not_mistaken_for_retry"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="interfaces.public_cli_and_static_surface",
        version="v4",
        test_nodeids=(
            (
                "tests/test_cli_capabilities.py::"
                "test_canonical_argv_translates_to_hidden_flat_compatibility_flags"
            ),
            (
                "tests/test_cli_capabilities.py::"
                "test_canonical_help_is_specific_without_changing_global_parser_help"
            ),
            (
                "tests/test_cli_capabilities.py::"
                "test_flat_alias_is_explicit_but_hidden_from_global_help"
            ),
            (
                "tests/test_cli_code_surface.py::"
                "test_code_actions_aliases_and_help_preserve_the_normalized_contract"
            ),
            (
                "tests/test_cli_code_surface.py::"
                "test_code_explicit_aliases_and_abbreviation_policy_remain_stable"
            ),
            (
                "tests/test_cli_modularization.py::ModularParserTests::"
                "test_long_option_abbreviations_are_rejected"
            ),
            (
                "tests/test_code_interface_surface_analysis.py::"
                "test_cli_static_view_does_not_claim_effective_parser_behavior"
            ),
            (
                "tests/test_code_interface_surface_analysis.py::"
                "test_interface_surface_observes_modules_configuration_and_static_cli"
            ),
            (
                "tests/test_code_observability_cli.py::"
                "test_canonical_help_and_translation_accept_question_options_before_or_"
                "after_positional"
            ),
            ("tests/test_code_observability_cli.py::test_direct_handlers_map_only_ready_to_zero"),
            *_PUBLIC_CLI_FLAT_INVALID_NODEIDS,
            (
                "tests/test_code_observability_cli.py::"
                "test_public_question_command_is_focal_and_preserves_owner_bytes_and_"
                "sidecars"
            ),
            (
                "tests/test_code_observability_cli.py::"
                "test_public_question_unsupported_and_missing_storage_exit_two_without_"
                "creating_state"
            ),
            (
                "tests/test_code_observability_cli.py::"
                "test_public_storage_command_is_bounded_and_immutable"
            ),
            (
                "tests/test_code_observability_cli.py::"
                "test_read_api_code_question_uses_only_fixed_scopes_and_exact_focal_reader"
            ),
            (
                "tests/test_code_public_cli_interface_experiments.py::"
                "test_public_entrypoint_dispatch_precedence_covers_special_human_"
                "canonical_and_flat"
            ),
            (
                "tests/test_code_public_cli_interface_experiments.py::"
                "test_public_entrypoint_invalid_help_and_incomplete_commands_are_bounded_"
                "and_state_free"
            ),
            ("tests/test_human_cli.py::test_help_lists_useful_facade_and_legacy_compatibility"),
            (
                "tests/test_human_cli.py::"
                "test_installed_entrypoint_dispatches_human_commands_before_flat_cli"
            ),
            (
                "tests/test_packaging_entrypoint.py::"
                "test_installed_entrypoint_forwards_arguments_to_integrated_cli"
            ),
            (
                "tests/test_packaging_entrypoint.py::"
                "test_project_metadata_uses_package_version_and_installed_command"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "selected_help_parser_dispatch_and_static_projection_controls_do_not_execute_"
            "every_command_handler_gui_mcp_worker_or_external_effect"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "declared_entrypoint_and_effective_help_contract_are_observed",
                (
                    (
                        "tests/test_cli_capabilities.py::"
                        "test_canonical_help_is_specific_without_changing_global_parser_help"
                    ),
                    (
                        "tests/test_cli_code_surface.py::"
                        "test_code_actions_aliases_and_help_preserve_the_normalized_contract"
                    ),
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_canonical_help_and_translation_accept_question_options_before_"
                        "or_after_positional"
                    ),
                    (
                        "tests/test_human_cli.py::"
                        "test_help_lists_useful_facade_and_legacy_compatibility"
                    ),
                    (
                        "tests/test_packaging_entrypoint.py::"
                        "test_project_metadata_uses_package_version_and_installed_command"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "dynamic_hidden_and_static_surfaces_remain_explicitly_non_equivalent",
                (
                    (
                        "tests/test_cli_capabilities.py::"
                        "test_flat_alias_is_explicit_but_hidden_from_global_help"
                    ),
                    (
                        "tests/test_code_interface_surface_analysis.py::"
                        "test_cli_static_view_does_not_claim_effective_parser_behavior"
                    ),
                    (
                        "tests/test_code_interface_surface_analysis.py::"
                        "test_interface_surface_observes_modules_configuration_and_static_cli"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "focal_question_and_storage_reads_are_bounded_and_immutable",
                (
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_direct_handlers_map_only_ready_to_zero"
                    ),
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_public_question_command_is_focal_and_preserves_owner_bytes_and_"
                        "sidecars"
                    ),
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_public_storage_command_is_bounded_and_immutable"
                    ),
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_read_api_code_question_uses_only_fixed_scopes_and_exact_focal_"
                        "reader"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "invalid_abbreviated_and_incomplete_commands_fail_closed_without_state",
                (
                    (
                        "tests/test_cli_code_surface.py::"
                        "test_code_explicit_aliases_and_abbreviation_policy_remain_stable"
                    ),
                    (
                        "tests/test_cli_modularization.py::ModularParserTests::"
                        "test_long_option_abbreviations_are_rejected"
                    ),
                    *_PUBLIC_CLI_FLAT_INVALID_NODEIDS,
                    (
                        "tests/test_code_observability_cli.py::"
                        "test_public_question_unsupported_and_missing_storage_exit_two_without_"
                        "creating_state"
                    ),
                    (
                        "tests/test_code_public_cli_interface_experiments.py::"
                        "test_public_entrypoint_invalid_help_and_incomplete_commands_are_"
                        "bounded_and_state_free"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "special_human_canonical_and_flat_dispatch_precedence_is_exact",
                (
                    (
                        "tests/test_cli_capabilities.py::"
                        "test_canonical_argv_translates_to_hidden_flat_compatibility_flags"
                    ),
                    (
                        "tests/test_code_public_cli_interface_experiments.py::"
                        "test_public_entrypoint_dispatch_precedence_covers_special_human_"
                        "canonical_and_flat"
                    ),
                    (
                        "tests/test_human_cli.py::"
                        "test_installed_entrypoint_dispatches_human_commands_before_flat_cli"
                    ),
                    (
                        "tests/test_packaging_entrypoint.py::"
                        "test_installed_entrypoint_forwards_arguments_to_integrated_cli"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="knowledge.asset_health_causal_acceptance",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_knowledge_asset_health_analysis.py::"
                "test_public_read_is_read_only_and_resource_identity_is_strict"
            ),
            (
                "tests/test_code_knowledge_asset_health_analysis.py::"
                "test_search_and_health_share_identity_and_missing_state_never_becomes_healthy"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_active_wal_abstains_without_touching_owner_sidecars"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_aligned_published_text_trace_is_healthy_and_replay_deterministic"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_identity_or_processing_signature_mismatch_never_cross_joins[identity]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_identity_or_processing_signature_mismatch_never_cross_joins"
                "[processing_signature]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy"
                "[corrupt]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy"
                "[future]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy"
                "[missing]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy"
                "[unpublished_catalog]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_missing_future_corrupt_and_unpublished_evidence_never_reports_healthy"
                "[unpublished_inventory]"
            ),
            (
                "tests/test_knowledge_asset_health.py::"
                "test_second_fact_snapshot_change_abstains_after_one_bounded_retry"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "isolated_text_owner_fixtures_do_not_prove_every_knowledge_route_owner_or_"
            "distributed_power_loss_boundary"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "aligned_four_stage_causal_trace_is_healthy",
                (
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_aligned_published_text_trace_is_healthy_and_replay_deterministic"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "mismatch_absence_future_corruption_and_unpublished_fail_closed",
                (
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_identity_or_processing_signature_mismatch_never_cross_joins"
                        "[identity]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_identity_or_processing_signature_mismatch_never_cross_joins"
                        "[processing_signature]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_missing_future_corrupt_and_unpublished_evidence_never_reports_"
                        "healthy[corrupt]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_missing_future_corrupt_and_unpublished_evidence_never_reports_"
                        "healthy[future]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_missing_future_corrupt_and_unpublished_evidence_never_reports_"
                        "healthy[missing]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_missing_future_corrupt_and_unpublished_evidence_never_reports_"
                        "healthy[unpublished_catalog]"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_missing_future_corrupt_and_unpublished_evidence_never_reports_"
                        "healthy[unpublished_inventory]"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "public_read_is_read_only_and_resource_identity_is_strict",
                (
                    (
                        "tests/test_code_knowledge_asset_health_analysis.py::"
                        "test_public_read_is_read_only_and_resource_identity_is_strict"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_active_wal_abstains_without_touching_owner_sidecars"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "snapshot_change_abstains_and_search_health_identity_is_stable",
                (
                    (
                        "tests/test_code_knowledge_asset_health_analysis.py::"
                        "test_search_and_health_share_identity_and_missing_state_never_becomes_"
                        "healthy"
                    ),
                    (
                        "tests/test_knowledge_asset_health.py::"
                        "test_second_fact_snapshot_change_abstains_after_one_bounded_retry"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="knowledge.pdf_asset_health_causal_acceptance",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_knowledge_pdf_asset_health_analysis.py::"
                "test_pdf_asset_health_question_requires_partial_protected_and_recovery_"
                "experiment"
            ),
            (
                "tests/test_knowledge_asset_health_pdf.py::"
                "test_pdf_aligned_full_projection_is_healthy_read_only_and_content_blind"
            ),
            (
                "tests/test_knowledge_asset_health_pdf.py::"
                "test_pdf_dispatch_ambiguity_and_owner_fences_abstain_without_mutation"
            ),
            (
                "tests/test_knowledge_asset_health_pdf.py::"
                "test_pdf_empty_bounded_and_partial_projections_have_typed_health"
            ),
            (
                "tests/test_knowledge_asset_health_pdf.py::"
                "test_pdf_projection_recovery_and_terminal_inconsistencies_fail_closed"
            ),
            (
                "tests/test_knowledge_asset_health_pdf.py::"
                "test_pdf_protected_error_processing_and_unknown_states_do_not_invent_catalog"
            ),
            (
                "tests/test_pdf_birthtime.py::PdfBirthtimeInvariantTests::"
                "test_reconciles_abandoned_processing_without_discarding_staging"
            ),
            (
                "tests/test_pdf_route.py::PdfRouteTests::"
                "test_extracts_incrementally_and_reuses_cache"
            ),
            (
                "tests/test_pdf_route.py::PdfRouteTests::"
                "test_page_error_preserves_other_pages_as_partial_document"
            ),
            (
                "tests/test_pdf_route.py::PdfRouteTests::"
                "test_pdf_route_republishes_incomplete_cache_hits"
            ),
            (
                "tests/test_pdf_route.py::PdfRouteTests::"
                "test_recovery_restart_clears_failed_qpdf_pages_and_promotes_done"
            ),
            (
                "tests/test_pdf_route.py::PdfRouteTests::"
                "test_timeout_flushes_sub_batch_progress_for_next_resume"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "bounded_pdf_owner_route_and_health_fixtures_do_not_prove_ocr_visual_"
            "semantic_fidelity_power_loss_or_every_future_recovery_engine"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "page_staging_fts_and_catalog_mismatch_fail_closed",
                (
                    (
                        "tests/test_knowledge_asset_health_pdf.py::"
                        "test_pdf_empty_bounded_and_partial_projections_have_typed_health"
                    ),
                    (
                        "tests/test_knowledge_asset_health_pdf.py::"
                        "test_pdf_protected_error_processing_and_unknown_states_do_not_"
                        "invent_catalog"
                    ),
                    (
                        "tests/test_pdf_route.py::PdfRouteTests::"
                        "test_page_error_preserves_other_pages_as_partial_document"
                    ),
                    (
                        "tests/test_pdf_route.py::PdfRouteTests::"
                        "test_pdf_route_republishes_incomplete_cache_hits"
                    ),
                    (
                        "tests/test_pdf_route.py::PdfRouteTests::"
                        "test_timeout_flushes_sub_batch_progress_for_next_resume"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "recovery_is_version_and_message_independent",
                (
                    (
                        "tests/test_knowledge_asset_health_pdf.py::"
                        "test_pdf_projection_recovery_and_terminal_inconsistencies_fail_closed"
                    ),
                    (
                        "tests/test_pdf_birthtime.py::PdfBirthtimeInvariantTests::"
                        "test_reconciles_abandoned_processing_without_discarding_staging"
                    ),
                    (
                        "tests/test_pdf_route.py::PdfRouteTests::"
                        "test_recovery_restart_clears_failed_qpdf_pages_and_promotes_done"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "typed_pdf_states_preserve_partial_and_protected_semantics",
                (
                    (
                        "tests/test_code_knowledge_pdf_asset_health_analysis.py::"
                        "test_pdf_asset_health_question_requires_partial_protected_and_"
                        "recovery_experiment"
                    ),
                    (
                        "tests/test_knowledge_asset_health_pdf.py::"
                        "test_pdf_aligned_full_projection_is_healthy_read_only_and_content_"
                        "blind"
                    ),
                    (
                        "tests/test_pdf_route.py::PdfRouteTests::"
                        "test_extracts_incrementally_and_reuses_cache"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "wal_snapshot_and_owner_ambiguity_remain_read_only",
                (
                    (
                        "tests/test_knowledge_asset_health_pdf.py::"
                        "test_pdf_dispatch_ambiguity_and_owner_fences_abstain_without_mutation"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="retention.durable_hold_safety",
        version="v2",
        test_nodeids=(
            (
                "tests/test_retention_planner.py::"
                "test_catalog_protects_publications_builders_and_uncertain_actions"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_framework_protects_uncertain_actions_and_human_evidence"
            ),
            *_RETENTION_INCOMPLETE_REVIEW_RECEIPT_NODEIDS,
            (
                "tests/test_retention_planner.py::"
                "test_framework_retention_holds_and_validates_complete_review_batch_chain"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_inventory_protects_current_previous_builder_candidate_and_framework_use"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_plan_retention_signature_and_snapshot_then_planning_phase_order"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_reader_snapshot_does_not_mix_concurrent_semantic_commit"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_schema_drift_blocks_without_modifying_main_database"
            ),
            (
                "tests/test_retention_planner.py::"
                "test_semantic_policy_protects_heads_builders_leases_and_base_chain"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "bounded_owner_fixtures_do_not_prove_power_loss_or_the_safety_of_a_future_"
            "delete_executor"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "current_previous_builders_leases_and_human_evidence_are_protected",
                (
                    (
                        "tests/test_retention_planner.py::"
                        "test_catalog_protects_publications_builders_and_uncertain_actions"
                    ),
                    (
                        "tests/test_retention_planner.py::"
                        "test_framework_protects_uncertain_actions_and_human_evidence"
                    ),
                    (
                        "tests/test_retention_planner.py::"
                        "test_framework_retention_holds_and_validates_complete_review_batch_chain"
                    ),
                    (
                        "tests/test_retention_planner.py::"
                        "test_inventory_protects_current_previous_builder_candidate_and_framework_use"
                    ),
                    (
                        "tests/test_retention_planner.py::"
                        "test_semantic_policy_protects_heads_builders_leases_and_base_chain"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "dry_run_never_supports_deletion_and_preserves_phase_order",
                (
                    (
                        "tests/test_retention_planner.py::"
                        "test_plan_retention_signature_and_snapshot_then_planning_phase_order"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "incomplete_review_receipt_or_schema_drift_fails_closed_without_mutation",
                (
                    *_RETENTION_INCOMPLETE_REVIEW_RECEIPT_NODEIDS,
                    (
                        "tests/test_retention_planner.py::"
                        "test_schema_drift_blocks_without_modifying_main_database"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "reader_snapshot_does_not_mix_concurrent_owner_commit",
                (
                    (
                        "tests/test_retention_planner.py::"
                        "test_reader_snapshot_does_not_mix_concurrent_semantic_commit"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="security.supply_chain_gate_controls",
        version="v2",
        test_nodeids=(
            (
                "tests/test_build_binary_inputs.py::"
                "test_source_only_dependency_is_hash_pinned_and_built_without_installing"
            ),
            (
                "tests/test_code_supply_chain_analysis.py::"
                "test_missing_provider_never_passes_its_gates"
            ),
            (
                "tests/test_code_supply_chain_analysis.py::"
                "test_zero_findings_is_valid_and_all_absolute_gates_pass"
            ),
            (
                "tests/test_external_dependency_hygiene.py::"
                "test_real_deptry_accepts_exact_stage_without_exclusion_panic"
            ),
            (
                "tests/test_external_semgrep_invariants.py::"
                "test_command_and_environment_disable_network_registry_and_autofix"
            ),
            (
                "tests/test_external_semgrep_invariants.py::"
                "test_staging_rejects_empty_extra_unowned_and_mismatched_paths"
            ),
            (
                "tests/test_external_supply_chain_audit.py::"
                "test_base_dependency_gates_exclude_false_markers_and_optional_extras"
            ),
            (
                "tests/test_external_supply_chain_audit.py::"
                "test_installed_inventory_correlates_pyproject_licenses_requirements_and_record"
            ),
            (
                "tests/test_external_supply_chain_audit.py::"
                "test_pip_audit_public_contract_phase_order_and_complete_result"
            ),
            (
                "tests/test_external_supply_chain_provider_registry.py::"
                "test_semgrep_and_deptry_use_their_exact_python_domains_and_replay"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "bounded_local_provider_and_artifact_fixtures_do_not_prove_future_feeds_"
            "complete_or_candidate_artifacts_correct"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "bounded_local_staging_rejects_unowned_inputs",
                (
                    (
                        "tests/test_external_semgrep_invariants.py::"
                        "test_staging_rejects_empty_extra_unowned_and_mismatched_paths"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "dependency_declaration_inventory_record_and_license_evidence_are_correlated",
                (
                    (
                        "tests/test_external_dependency_hygiene.py::"
                        "test_real_deptry_accepts_exact_stage_without_exclusion_panic"
                    ),
                    (
                        "tests/test_external_supply_chain_audit.py::"
                        "test_base_dependency_gates_exclude_false_markers_and_optional_extras"
                    ),
                    (
                        "tests/test_external_supply_chain_audit.py::"
                        "test_installed_inventory_correlates_pyproject_licenses_requirements_"
                        "and_record"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "missing_provider_cannot_pass_and_clean_complete_fixture_passes_absolute_gates",
                (
                    (
                        "tests/test_code_supply_chain_analysis.py::"
                        "test_missing_provider_never_passes_its_gates"
                    ),
                    (
                        "tests/test_code_supply_chain_analysis.py::"
                        "test_zero_findings_is_valid_and_all_absolute_gates_pass"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "pip_audit_contract_records_bounded_phase_complete_result",
                (
                    (
                        "tests/test_external_supply_chain_audit.py::"
                        "test_pip_audit_public_contract_phase_order_and_complete_result"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "provider_environment_strips_credentials_and_disables_networked_modes",
                (
                    (
                        "tests/test_external_semgrep_invariants.py::"
                        "test_command_and_environment_disable_network_registry_and_autofix"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "provider_replay_is_bound_to_exact_domains_versions_and_result_digests",
                (
                    (
                        "tests/test_external_supply_chain_provider_registry.py::"
                        "test_semgrep_and_deptry_use_their_exact_python_domains_and_replay"
                    ),
                ),
            ),
            RuntimeScenarioGateSpec(
                "source_only_dependency_is_hash_pinned_and_built_without_installing",
                (
                    (
                        "tests/test_build_binary_inputs.py::"
                        "test_source_only_dependency_is_hash_pinned_and_built_without_installing"
                    ),
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="semantic.staging_process_death_resume",
        version="v1",
        test_nodeids=(
            (
                "tests/test_semantic_text_staging_session.py::"
                "test_process_death_preserves_committed_prefix_and_resume_publishes_atomically"
            ),
        ),
        scenario_kind="process_death",
        isolation="spawned_process_and_tmp_path",
        limitation="process_exit_is_observed_but_power_loss_and_filesystem_failure_are_not",
        gate_specs=(
            RuntimeScenarioGateSpec(
                "committed_staging_prefix_survives_process_death",
                (
                    "tests/test_semantic_text_staging_session.py::"
                    "test_process_death_preserves_committed_prefix_and_resume_publishes_atomically",
                ),
            ),
            RuntimeScenarioGateSpec(
                "dead_building_generation_remains_unpublished",
                (
                    "tests/test_semantic_text_staging_session.py::"
                    "test_process_death_preserves_committed_prefix_and_resume_publishes_atomically",
                ),
            ),
            RuntimeScenarioGateSpec(
                "resume_publishes_complete_generation_atomically",
                (
                    "tests/test_semantic_text_staging_session.py::"
                    "test_process_death_preserves_committed_prefix_and_resume_publishes_atomically",
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="state.text_semantic_exact_projection",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_state_projection_analysis.py::"
                "test_projection_excludes_empty_text_and_observes_exact_alignment"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation="fixture_alignment_is_not_live_cross_owner_recovery_evidence",
    ),
    RuntimeScenarioSpec(
        scenario_id="state.text_sql_runtime_trace",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_state_interaction_analysis.py::"
                "test_literal_sql_is_parsed_and_dynamic_sql_remains_missing_evidence"
            ),
            (
                "tests/test_text_derivation_route.py::"
                "test_process_death_before_text_terminal_commit_rolls_back_and_recovers"
            ),
            (
                "tests/test_text_derivation_route.py::"
                "test_terminal_publication_rollback_leaves_no_partial_document_or_fts"
            ),
            (
                "tests/test_text_derivation_route.py::"
                "test_text_terminal_publication_exposes_a_closed_traceable_transaction_boundary"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation=(
            "one_text_workflow_trace_and_process_death_probe_do_not_observe_every_dynamic_sql_"
            "path_store_or_power_loss"
        ),
        gate_specs=(
            RuntimeScenarioGateSpec(
                "literal_sql_parser_preserves_dynamic_sql_as_missing_evidence",
                (
                    "tests/test_code_state_interaction_analysis.py::"
                    "test_literal_sql_is_parsed_and_dynamic_sql_remains_missing_evidence",
                ),
            ),
            RuntimeScenarioGateSpec(
                "post_terminalization_exception_leaves_no_partial_publication",
                (
                    "tests/test_text_derivation_route.py::"
                    "test_terminal_publication_rollback_leaves_no_partial_document_or_fts",
                ),
            ),
            RuntimeScenarioGateSpec(
                "process_death_before_commit_rolls_back_and_restart_converges",
                (
                    "tests/test_text_derivation_route.py::"
                    "test_process_death_before_text_terminal_commit_rolls_back_and_recovers",
                ),
            ),
            RuntimeScenarioGateSpec(
                "successful_terminal_transaction_contains_required_text_tables",
                (
                    "tests/test_text_derivation_route.py::"
                    "test_text_terminal_publication_exposes_a_closed_traceable_transaction_boundary",
                ),
            ),
        ),
    ),
    RuntimeScenarioSpec(
        scenario_id="state.text_terminal_relational_closure",
        version="v1",
        test_nodeids=(
            (
                "tests/test_code_state_topology_analysis.py::"
                "test_exact_text_closure_preserves_running_and_legacy_negative_controls"
            ),
        ),
        scenario_kind="state_fixture",
        isolation="pytest_tmp_path",
        limitation="final_relational_closure_does_not_prove_historical_atomicity",
    ),
)

INVARIANT_SCENARIO_IDS = (
    "analyzer.hotspot_name_path_call_invariance",
    "semantic.staging_process_death_resume",
    "state.text_semantic_exact_projection",
    "state.text_terminal_relational_closure",
)

EXPERIMENT_SCENARIO_IDS = (
    "architecture.declared_import_contract_acceptance",
    "capability.public_text_route_to_search",
    "evolution.code_schema_upgrade_matrix",
    "framework.review_task_protocol_acceptance",
    "interfaces.public_cli_and_static_surface",
    "knowledge.asset_health_causal_acceptance",
    "knowledge.pdf_asset_health_causal_acceptance",
    "retention.durable_hold_safety",
    "security.supply_chain_gate_controls",
    "semantic.staging_process_death_resume",
    "state.text_sql_runtime_trace",
)

CALIBRATION_SCENARIO_IDS = (
    "analyzer.calibration_and_worktree_controls",
    "architecture.declared_boundary_and_owner_mapping",
    "evolution.change_surface_antigoodhart_controls",
)

INVARIANT_RUNTIME_SCENARIOS = tuple(
    item for item in RUNTIME_SCENARIOS if item.scenario_id in set(INVARIANT_SCENARIO_IDS)
)


INVARIANT_SPECS = (
    InvariantSpec(
        invariant_id="analyzer.metrics_do_not_become_semantic_change_authority",
        version="v1",
        statement=(
            "Renames, path conventions, wrappers, and transaction-like call spellings do not "
            "create a semantic change decision."
        ),
        scope="analyzer",
        scenario_ids=("analyzer.hotspot_name_path_call_invariance",),
        failure_impact="analyzer_integrity",
    ),
    InvariantSpec(
        invariant_id="semantic.building_generation_is_not_published_after_process_death",
        version="v1",
        statement=(
            "A process death during Semantic staging leaves the prior head visible and a resume "
            "publishes the completed generation atomically."
        ),
        scope="publication",
        scenario_ids=("semantic.staging_process_death_resume",),
        failure_impact="publication",
    ),
    InvariantSpec(
        invariant_id="state.published_semantic_text_matches_eligible_text_revisions",
        version="v1",
        statement=(
            "A published Semantic text head represents exactly eligible non-empty Text revisions "
            "under the declared source-owner contract."
        ),
        scope="state",
        scenario_ids=("state.text_semantic_exact_projection",),
        failure_impact="consistency",
    ),
    InvariantSpec(
        invariant_id="state.text_terminal_publications_are_relationally_closed",
        version="v1",
        statement=(
            "Terminal Text attempts have their required receipt and outbox relations while "
            "running and legacy records remain separately classified."
        ),
        scope="state",
        scenario_ids=("state.text_terminal_relational_closure",),
        failure_impact="consistency",
    ),
)


def _validate_registry() -> None:
    scenario_ids = tuple(item.scenario_id for item in RUNTIME_SCENARIOS)
    invariant_ids = tuple(item.invariant_id for item in INVARIANT_SPECS)
    if scenario_ids != tuple(sorted(scenario_ids)) or len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("runtime-scenario registry must be unique and canonically ordered")
    if invariant_ids != tuple(sorted(invariant_ids)) or len(set(invariant_ids)) != len(
        invariant_ids
    ):
        raise ValueError("invariant registry must be unique and canonically ordered")
    declared = set(scenario_ids)
    invariant_scenarios = set(INVARIANT_SCENARIO_IDS)
    experiment_scenarios = set(EXPERIMENT_SCENARIO_IDS)
    calibration_scenarios = set(CALIBRATION_SCENARIO_IDS)
    if (
        tuple(INVARIANT_SCENARIO_IDS) != tuple(sorted(INVARIANT_SCENARIO_IDS))
        or tuple(EXPERIMENT_SCENARIO_IDS) != tuple(sorted(EXPERIMENT_SCENARIO_IDS))
        or tuple(CALIBRATION_SCENARIO_IDS) != tuple(sorted(CALIBRATION_SCENARIO_IDS))
        or invariant_scenarios & calibration_scenarios
        or experiment_scenarios & calibration_scenarios
        or invariant_scenarios | experiment_scenarios | calibration_scenarios != declared
    ):
        raise ValueError("runtime scenarios must have canonical assurance roles")
    referenced = {scenario for item in INVARIANT_SPECS for scenario in item.scenario_ids}
    if referenced != invariant_scenarios:
        raise ValueError("invariant specs must reference exactly invariant runtime scenarios")
    scenario_by_id = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    if any(not scenario_by_id[item].gate_specs for item in EXPERIMENT_SCENARIO_IDS):
        raise ValueError("executable experiment scenarios require measured gate contracts")
    if any(scenario_by_id[item].gate_specs for item in declared - experiment_scenarios):
        raise ValueError("non-experiment scenarios cannot publish acceptance gate contracts")
    all_nodeids = tuple(nodeid for item in RUNTIME_SCENARIOS for nodeid in item.test_nodeids)
    if len(all_nodeids) != len(set(all_nodeids)):
        raise ValueError("runtime scenario nodeids must be globally unique")


_validate_registry()


def runtime_scenario_registry_payload() -> dict[str, object]:
    return {
        "schema": CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA,
        "scenarios": tuple(asdict(item) for item in RUNTIME_SCENARIOS),
        "invariant_scenario_ids": INVARIANT_SCENARIO_IDS,
        "experiment_scenario_ids": EXPERIMENT_SCENARIO_IDS,
        "calibration_scenario_ids": CALIBRATION_SCENARIO_IDS,
        "claim_scope": "allowlisted_test_scenarios_not_question_conclusions_or_formal_proof",
    }


def runtime_scenario_registry_fingerprint() -> str:
    return analysis_identity(
        "code-runtime-scenario-registry-v11", runtime_scenario_registry_payload()
    )


def invariant_registry_payload() -> dict[str, object]:
    return {
        "schema": CODE_INVARIANT_REGISTRY_SCHEMA,
        "scenario_schema": CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA,
        "invariants": tuple(asdict(item) for item in INVARIANT_SPECS),
        "scenarios": tuple(asdict(item) for item in INVARIANT_RUNTIME_SCENARIOS),
        "claim_scope": "declared_scenario_execution_not_formal_proof",
    }


def invariant_registry_fingerprint() -> str:
    return analysis_identity("code-invariant-registry-v3", invariant_registry_payload())


def runtime_scenario(scenario_id: str) -> RuntimeScenarioSpec:
    selected = _required("scenario id", scenario_id, 256)
    match = next((item for item in RUNTIME_SCENARIOS if item.scenario_id == selected), None)
    if match is None:
        raise ValueError(f"unknown runtime scenario: {selected}")
    return match


__all__ = [
    "CALIBRATION_SCENARIO_IDS",
    "CODE_INVARIANT_REGISTRY_SCHEMA",
    "CODE_RUNTIME_SCENARIO_REGISTRY_SCHEMA",
    "EXPERIMENT_SCENARIO_IDS",
    "INVARIANT_RUNTIME_SCENARIOS",
    "INVARIANT_SCENARIO_IDS",
    "INVARIANT_SPECS",
    "RUNTIME_SCENARIOS",
    "InvariantSpec",
    "RuntimeScenarioGateSpec",
    "RuntimeScenarioSpec",
    "invariant_registry_fingerprint",
    "invariant_registry_payload",
    "runtime_scenario",
    "runtime_scenario_registry_fingerprint",
    "runtime_scenario_registry_payload",
]
