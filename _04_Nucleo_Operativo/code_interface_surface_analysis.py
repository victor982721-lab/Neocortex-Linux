"""Bounded module, configuration, and static CLI surface observations.

All source text comes from the published Code snapshot.  The CLI projection is
syntactic: literal ``argparse`` calls are observed without executing repository
code, and therefore never claim effective runtime reachability or behavior.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import tomllib
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from typing import Literal, cast

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
from .code_schema import CODE_SCHEMA_VERSION

CODE_INTERFACE_SURFACE_SCHEMA = "neocortex.code-interface-surface/v1"
CODE_INTERFACE_SURFACE_POLICY = "published-module-config-argparse-surface-v1"
MODULE_LINE_THRESHOLD = 1_000
MODULE_DIRECT_SYMBOL_THRESHOLD = 20
MODULE_PUBLIC_SYMBOL_THRESHOLD = 20
INTERFACE_SURFACE_LIMIT = 50
INTERFACE_MAX_MODULES = 20_000
INTERFACE_MAX_CONFIG_ARTIFACTS = 2_000
INTERFACE_MAX_SOURCE_BYTES = 4 * 1024 * 1024
INTERFACE_MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
INTERFACE_MAX_CONFIG_NODES = 100_000
INTERFACE_MAX_CONFIG_DEPTH = 64
INTERFACE_EXAMPLE_LIMIT = 50

MODULE_SURFACE_QUESTION = AnalysisQuestionSpec(
    question_id="structure.module_surface_concentration_requires_characterization",
    version="v1",
    subject_kinds=("module",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "published_module_surface_projection",
            "question",
            "supporting",
            ("internal_metric",),
            accepted_completeness=("complete", "partial"),
            allow_truncated=True,
        ),
        AnalysisEvidenceRequirementSpec(
            "module_responsibility_and_cohesion_characterized",
            "decision",
            "supporting",
            ("internal_relation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "module_role_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "module_characterization_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "large_or_wide_module_contains_accidental_responsibility_concentration",
        "large_or_wide_module_is_a_cohesive_declared_composition_or_contract_surface",
    ),
    counterevidence_rules=(
        "line_and_symbol_counts_do_not_measure_cohesion_or_maintenance_harm",
        "a_composition_root_or_declarative_builder_can_be_large_without_a_defect",
        "moving_or_wrapping_code_does_not_resolve_structural_concentration",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "characterize_module_symbol_and_dependency_clusters",
            "characterization",
            "Cluster direct symbols by dependencies, state access, and co-change evidence.",
        ),
        AnalysisNextActionSpec(
            "inspect_declared_module_role_counterevidence",
            "counterevidence_search",
            "Resolve composition-root, generated, facade, and contract-role declarations.",
        ),
        AnalysisNextActionSpec(
            "compare_module_change_clusters_with_runtime_consumers",
            "experiment",
            "Compare module clusters with consumers and representative change history.",
        ),
    ),
)

CONFIGURATION_SURFACE_QUESTION = AnalysisQuestionSpec(
    question_id="structure.configuration_inventory_requires_complete_parsing",
    version="v1",
    subject_kinds=("configuration",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "published_configuration_inventory_projection",
            "question",
            "supporting",
            ("internal_metric",),
        ),
        AnalysisEvidenceRequirementSpec(
            "configuration_keys_and_consumers_resolved",
            "decision",
            "supporting",
            ("internal_relation", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "configuration_format_and_generation_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "configuration_characterization_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "published_configuration_surface_is_parseable_bounded_and_consumed",
        "configuration_surface_is_partially_parsed_generated_or_unlinked_to_consumers",
    ),
    counterevidence_rules=(
        "key_count_does_not_measure_configuration_quality",
        "unsupported_yaml_or_text_only_artifacts_are_not_absent_configuration",
        "a_parsed_key_is_not_evidence_of_runtime_use",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "resolve_unsupported_configuration_formats",
            "characterization",
            "Add a deterministic parser contract for unsupported configuration artifacts.",
        ),
        AnalysisNextActionSpec(
            "trace_configuration_keys_to_runtime_consumers",
            "counterevidence_search",
            "Link parsed key paths to exact readers, defaults, and public capabilities.",
        ),
        AnalysisNextActionSpec(
            "exercise_configuration_override_and_default_scenarios",
            "experiment",
            "Execute isolated default, override, invalid, and unknown-key scenarios.",
        ),
    ),
)

CLI_SURFACE_QUESTION = AnalysisQuestionSpec(
    question_id="structure.static_cli_calls_require_runtime_contract_evidence",
    version="v1",
    subject_kinds=("entrypoint",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "published_static_argparse_call_projection",
            "question",
            "supporting",
            ("internal_metric",),
        ),
        AnalysisEvidenceRequirementSpec(
            "effective_runtime_parser_contract_observed",
            "decision",
            "supporting",
            ("runtime_observation", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "dynamic_cli_construction_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "public_cli_acceptance_scenario_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "static_argparse_calls_match_the_effective_public_command_surface",
        "dynamic_composition_aliases_or_unreached_builders_make_the_static_view_incomplete",
    ),
    counterevidence_rules=(
        "an_add_argument_call_does_not_prove_the_parser_is_publicly_reachable",
        "literal_option_strings_do_not_prove_defaults_destinations_or_dispatch_behavior",
        "test_or_worker_parsers_are_not_automatically_product_entrypoints",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "capture_effective_public_parser_contract",
            "characterization",
            "Serialize the canonical parser actions from the public command in isolation.",
        ),
        AnalysisNextActionSpec(
            "inspect_dynamic_and_nonliteral_cli_calls",
            "counterevidence_search",
            "Resolve dynamic option strings, parser composition, aliases, and hidden workers.",
        ),
        AnalysisNextActionSpec(
            "execute_public_help_and_dispatch_acceptance_scenarios",
            "experiment",
            "Run bounded public help, parse, invalid-input, and dispatch scenarios.",
        ),
    ),
)

INTERFACE_SURFACE_AVAILABILITY_QUESTION = AnalysisQuestionSpec(
    question_id="structure.interface_evidence_provider_is_resolved",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "published_code_run_resolved",
            "question",
            "supporting",
            ("contract", "internal_metric"),
        ),
        AnalysisEvidenceRequirementSpec(
            "module_configuration_and_cli_projection_resolved",
            "question",
            "supporting",
            ("internal_metric",),
        ),
        AnalysisEvidenceRequirementSpec(
            "runtime_interface_contract_resolved",
            "decision",
            "supporting",
            ("contract", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "incomplete_or_dynamic_surface_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "runtime_observation", "experiment_result"),
        ),
    ),
    hypotheses=(
        "interface_surface_evidence_is_unavailable_or_incompatible",
        "interface_surface_is_resolved_for_characterization",
    ),
    counterevidence_rules=(
        "provider_absence_is_not_evidence_that_an_interface_surface_is_empty",
        "an_unresolved_publication_cannot_support_module_configuration_or_cli_counts",
        "a_static_projection_does_not_establish_runtime_reachability",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "resolve_published_code_interface_inputs",
            "characterization",
            "Resolve the published Code run and its bounded source projection.",
        ),
        AnalysisNextActionSpec(
            "rerun_interface_surface_projection",
            "experiment",
            "Rerun module, configuration, and static CLI characterization.",
        ),
    ),
)

_LIMITATIONS = (
    "structural_counts_do_not_establish_a_defect_or_refactor",
    "configuration_values_are_never_exposed",
    "static_argparse_calls_do_not_prove_effective_runtime_surface",
    "published_snapshot_may_be_stale_relative_to_the_worktree",
    "human_decision_not_owned_by_code_analysis",
)


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _text_tuple(label: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must be unique")
    return result


@dataclass(frozen=True, slots=True)
class ModuleSurfaceObservation:
    observation_id: str
    version_id: int
    symbol_id: int
    path: str
    module_name: str
    line_count: int
    direct_symbols: int
    public_direct_symbols: int
    direct_functions: int
    direct_classes: int
    direct_variables: int
    confirmed_dependencies: int
    confirmed_references: int
    selection_reasons: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("module observation id", self.observation_id),
            ("module path", self.path),
            ("module name", self.module_name),
        ):
            _required_text(label, value)
        for label, id_value in (
            ("module version", self.version_id),
            ("module symbol", self.symbol_id),
        ):
            if isinstance(id_value, bool) or not isinstance(id_value, int) or id_value < 1:
                raise ValueError(f"{label} must be positive")
        for field_name in (
            "line_count",
            "direct_symbols",
            "public_direct_symbols",
            "direct_functions",
            "direct_classes",
            "direct_variables",
            "confirmed_dependencies",
            "confirmed_references",
        ):
            _nonnegative(field_name, getattr(self, field_name))
        if self.line_count < 1 or self.public_direct_symbols > self.direct_symbols:
            raise ValueError("module surface counts are invalid")
        _text_tuple("module selection reason", self.selection_reasons)
        expected = tuple(
            sorted(
                reason
                for reason, selected in (
                    ("line_threshold", self.line_count >= MODULE_LINE_THRESHOLD),
                    (
                        "direct_symbol_threshold",
                        self.direct_symbols >= MODULE_DIRECT_SYMBOL_THRESHOLD,
                    ),
                    (
                        "public_symbol_threshold",
                        self.public_direct_symbols >= MODULE_PUBLIC_SYMBOL_THRESHOLD,
                    ),
                )
                if selected
            )
        )
        if self.selection_reasons != expected or not expected:
            raise ValueError("module surface selection is not threshold-derived")
        expected_id = analysis_identity(
            "module-surface-observation-v1",
            {key: value for key, value in asdict(self).items() if key != "observation_id"},
        )
        if self.observation_id != expected_id:
            raise ValueError("module surface observation identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("module surface observation must remain advisory")


@dataclass(frozen=True, slots=True)
class ConfigurationSurfaceObservation:
    observation_id: str
    version_id: int
    path: str
    artifact_kind: str
    language: str
    analysis_status: str
    text_chars: int
    parse_status: Literal["exact", "unsupported", "incomplete", "error"]
    parser_kind: Literal["json", "toml"] | None
    top_level_keys: int | None
    total_keys: int | None
    leaf_values: int | None
    max_depth: int | None
    top_level_key_examples: tuple[str, ...]
    examples_truncated: bool
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("configuration observation id", self.observation_id),
            ("configuration path", self.path),
            ("configuration artifact kind", self.artifact_kind),
            ("configuration language", self.language),
            ("configuration analysis status", self.analysis_status),
        ):
            _required_text(label, value)
        if (
            isinstance(self.version_id, bool)
            or not isinstance(self.version_id, int)
            or self.version_id < 1
        ):
            raise ValueError("configuration version must be positive")
        _nonnegative("configuration text characters", self.text_chars)
        if self.parse_status not in {"exact", "unsupported", "incomplete", "error"}:
            raise ValueError("configuration parse status is invalid")
        if self.parser_kind not in {None, "json", "toml"}:
            raise ValueError("configuration parser kind is invalid")
        optional_counts = (self.top_level_keys, self.total_keys, self.leaf_values, self.max_depth)
        for count in optional_counts:
            if count is not None:
                _nonnegative("configuration structure count", count)
        _text_tuple("configuration top-level key example", self.top_level_key_examples)
        if not isinstance(self.examples_truncated, bool):
            raise ValueError("configuration example truncation must be boolean")
        if self.parse_status == "exact":
            if self.parser_kind is None or any(value is None for value in optional_counts):
                raise ValueError("exact configuration parse lacks structural counts")
            assert self.top_level_keys is not None
            if len(self.top_level_key_examples) != min(
                self.top_level_keys, INTERFACE_EXAMPLE_LIMIT
            ) or self.examples_truncated != (self.top_level_keys > INTERFACE_EXAMPLE_LIMIT):
                raise ValueError("configuration key examples are not derived")
        elif (
            self.parser_kind is not None
            or any(value is not None for value in optional_counts)
            or self.top_level_key_examples
            or self.examples_truncated
        ):
            raise ValueError("non-exact configuration parse cannot assert structure")
        expected_id = analysis_identity(
            "configuration-surface-observation-v1",
            {key: value for key, value in asdict(self).items() if key != "observation_id"},
        )
        if self.observation_id != expected_id:
            raise ValueError("configuration surface observation identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("configuration surface observation must remain advisory")


@dataclass(frozen=True, slots=True)
class CliSurfaceObservation:
    observation_id: str
    version_id: int
    path: str
    parse_status: Literal["exact", "incomplete", "error"]
    recorded_argparse_call_sites: int
    ast_argparse_call_sites: int
    add_argument_calls: int
    literal_argument_calls: int
    dynamic_argument_calls: int
    literal_option_strings: int
    positional_argument_calls: int
    add_parser_calls: int
    literal_subcommands: int
    option_examples: tuple[str, ...]
    subcommand_examples: tuple[str, ...]
    keyword_names: tuple[str, ...]
    examples_truncated: bool
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("CLI observation id", self.observation_id)
        _required_text("CLI path", self.path)
        if (
            isinstance(self.version_id, bool)
            or not isinstance(self.version_id, int)
            or self.version_id < 1
        ):
            raise ValueError("CLI version must be positive")
        if self.parse_status not in {"exact", "incomplete", "error"}:
            raise ValueError("CLI parse status is invalid")
        for field_name in (
            "recorded_argparse_call_sites",
            "ast_argparse_call_sites",
            "add_argument_calls",
            "literal_argument_calls",
            "dynamic_argument_calls",
            "literal_option_strings",
            "positional_argument_calls",
            "add_parser_calls",
            "literal_subcommands",
        ):
            _nonnegative(field_name, getattr(self, field_name))
        for label, values in (
            ("CLI option example", self.option_examples),
            ("CLI subcommand example", self.subcommand_examples),
            ("CLI keyword name", self.keyword_names),
        ):
            _text_tuple(label, values)
        if not isinstance(self.examples_truncated, bool):
            raise ValueError("CLI example truncation must be boolean")
        if self.parse_status == "exact":
            if self.literal_argument_calls + self.dynamic_argument_calls != self.add_argument_calls:
                raise ValueError("CLI literal/dynamic argument calls are not a partition")
            total_examples = self.literal_option_strings + self.literal_subcommands
            visible = len(self.option_examples) + len(self.subcommand_examples)
            if self.examples_truncated != (total_examples > visible):
                raise ValueError("CLI example truncation is not derived")
        elif (
            any(
                getattr(self, name)
                for name in (
                    "ast_argparse_call_sites",
                    "add_argument_calls",
                    "literal_argument_calls",
                    "dynamic_argument_calls",
                    "literal_option_strings",
                    "positional_argument_calls",
                    "add_parser_calls",
                    "literal_subcommands",
                )
            )
            or self.option_examples
            or self.subcommand_examples
            or self.keyword_names
            or self.examples_truncated
        ):
            raise ValueError("unparsed CLI surface cannot assert AST observations")
        expected_id = analysis_identity(
            "cli-surface-observation-v1",
            {key: value for key, value in asdict(self).items() if key != "observation_id"},
        )
        if self.observation_id != expected_id:
            raise ValueError("CLI surface observation identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("CLI surface observation must remain advisory")


@dataclass(frozen=True, slots=True)
class CodeInterfaceSurfaceAnalysis:
    analysis_id: str
    database: str
    analysis_run_id: int | None
    processing_signature: str | None
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    total_modules: int
    selected_modules: int
    returned_modules: int
    module_selection_truncated: bool
    modules: tuple[ModuleSurfaceObservation, ...]
    configuration_artifacts: int
    exact_configuration_artifacts: int
    unsupported_configuration_artifacts: int
    incomplete_configuration_artifacts: int
    errored_configuration_artifacts: int
    configurations: tuple[ConfigurationSurfaceObservation, ...]
    cli_candidate_files: int
    exact_cli_files: int
    incomplete_cli_files: int
    errored_cli_files: int
    recorded_argparse_call_sites: int
    ast_argparse_call_sites: int
    cli_files: tuple[CliSurfaceObservation, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("interface surface id", self.analysis_id)
        _required_text("interface surface database", self.database)
        if self.policy_id != CODE_INTERFACE_SURFACE_POLICY:
            raise ValueError("interface surface policy is invalid")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("interface surface status is invalid")
        for field_name in (
            "total_modules",
            "selected_modules",
            "returned_modules",
            "configuration_artifacts",
            "exact_configuration_artifacts",
            "unsupported_configuration_artifacts",
            "incomplete_configuration_artifacts",
            "errored_configuration_artifacts",
            "cli_candidate_files",
            "exact_cli_files",
            "incomplete_cli_files",
            "errored_cli_files",
            "recorded_argparse_call_sites",
            "ast_argparse_call_sites",
        ):
            _nonnegative(field_name, getattr(self, field_name))
        if not isinstance(self.module_selection_truncated, bool):
            raise ValueError("module selection truncation must be boolean")
        if not isinstance(self.modules, tuple) or any(
            not isinstance(item, ModuleSurfaceObservation) for item in self.modules
        ):
            raise ValueError("module observations are invalid")
        if not isinstance(self.configurations, tuple) or any(
            not isinstance(item, ConfigurationSurfaceObservation) for item in self.configurations
        ):
            raise ValueError("configuration observations are invalid")
        if not isinstance(self.cli_files, tuple) or any(
            not isinstance(item, CliSurfaceObservation) for item in self.cli_files
        ):
            raise ValueError("CLI observations are invalid")
        _text_tuple("interface surface limitation", self.limitations)
        if self.limitations != _LIMITATIONS:
            raise ValueError("interface surface limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("interface surface analysis must remain advisory")
        if self.status == "abstained":
            _required_text("interface surface abstention reason", self.reason, maximum=256)
            if (
                self.analysis_run_id is not None
                or self.processing_signature is not None
                or any(
                    (
                        self.total_modules,
                        self.selected_modules,
                        self.returned_modules,
                        self.configuration_artifacts,
                        self.exact_configuration_artifacts,
                        self.unsupported_configuration_artifacts,
                        self.incomplete_configuration_artifacts,
                        self.errored_configuration_artifacts,
                        self.cli_candidate_files,
                        self.exact_cli_files,
                        self.incomplete_cli_files,
                        self.errored_cli_files,
                        self.recorded_argparse_call_sites,
                        self.ast_argparse_call_sites,
                    )
                )
                or self.modules
                or self.configurations
                or self.cli_files
                or self.module_selection_truncated
            ):
                raise ValueError("abstained interface surface cannot assert partial evidence")
        else:
            if self.reason is not None:
                raise ValueError("ready interface surface cannot carry a reason")
            if (
                isinstance(self.analysis_run_id, bool)
                or not isinstance(self.analysis_run_id, int)
                or self.analysis_run_id < 1
            ):
                raise ValueError("interface surface run must be positive")
            _required_text("interface processing signature", self.processing_signature)
            if not 0 <= self.returned_modules <= self.selected_modules <= self.total_modules:
                raise ValueError("module surface counts are inconsistent")
            if self.returned_modules != len(self.modules) or self.module_selection_truncated != (
                self.selected_modules > self.returned_modules
            ):
                raise ValueError("module surface selection is inconsistent")
            if self.configuration_artifacts != len(self.configurations) or (
                self.exact_configuration_artifacts
                + self.unsupported_configuration_artifacts
                + self.incomplete_configuration_artifacts
                + self.errored_configuration_artifacts
                != self.configuration_artifacts
            ):
                raise ValueError("configuration surface counts are inconsistent")
            if self.cli_candidate_files != len(self.cli_files) or (
                self.exact_cli_files + self.incomplete_cli_files + self.errored_cli_files
                != self.cli_candidate_files
            ):
                raise ValueError("CLI surface counts are inconsistent")
            if self.recorded_argparse_call_sites != sum(
                item.recorded_argparse_call_sites for item in self.cli_files
            ) or self.ast_argparse_call_sites != sum(
                item.ast_argparse_call_sites for item in self.cli_files
            ):
                raise ValueError("CLI call totals are inconsistent")
        expected_id = analysis_identity(
            "code-interface-surface-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("interface surface identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_INTERFACE_SURFACE_SCHEMA, **asdict(self)}


def _analysis(values: dict[str, object]) -> CodeInterfaceSurfaceAnalysis:
    identity_values = {
        **values,
        "modules": tuple(
            asdict(item) for item in cast(tuple[ModuleSurfaceObservation, ...], values["modules"])
        ),
        "configurations": tuple(
            asdict(item)
            for item in cast(tuple[ConfigurationSurfaceObservation, ...], values["configurations"])
        ),
        "cli_files": tuple(
            asdict(item) for item in cast(tuple[CliSurfaceObservation, ...], values["cli_files"])
        ),
    }
    return CodeInterfaceSurfaceAnalysis(
        analysis_id=analysis_identity("code-interface-surface-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def abstained_code_interface_surface(
    reason: str,
    *,
    database: str,
) -> CodeInterfaceSurfaceAnalysis:
    return _analysis(
        {
            "database": database,
            "analysis_run_id": None,
            "processing_signature": None,
            "status": "abstained",
            "reason": _required_text("interface abstention reason", reason, maximum=256),
            "policy_id": CODE_INTERFACE_SURFACE_POLICY,
            "total_modules": 0,
            "selected_modules": 0,
            "returned_modules": 0,
            "module_selection_truncated": False,
            "modules": (),
            "configuration_artifacts": 0,
            "exact_configuration_artifacts": 0,
            "unsupported_configuration_artifacts": 0,
            "incomplete_configuration_artifacts": 0,
            "errored_configuration_artifacts": 0,
            "configurations": (),
            "cli_candidate_files": 0,
            "exact_cli_files": 0,
            "incomplete_cli_files": 0,
            "errored_cli_files": 0,
            "recorded_argparse_call_sites": 0,
            "ast_argparse_call_sites": 0,
            "cli_files": (),
            "limitations": _LIMITATIONS,
            "authority": "advisory",
            "mutation_authority": False,
        }
    )


def _module_observations(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> tuple[int, int, tuple[ModuleSurfaceObservation, ...]]:
    rows = tuple(
        connection.execute(
            """SELECT v.version_id,v.path_observed,m.symbol_id,m.name,
                      m.start_line,m.end_line,
                      COUNT(c.symbol_id) AS direct_symbols,
                      SUM(CASE WHEN c.visibility='public' THEN 1 ELSE 0 END) AS public_symbols,
                      SUM(CASE WHEN c.kind IN ('function','entrypoint') THEN 1 ELSE 0 END) AS functions,
                      SUM(CASE WHEN c.kind='class' THEN 1 ELSE 0 END) AS classes,
                      SUM(CASE WHEN c.kind='module_variable' THEN 1 ELSE 0 END) AS variables,
                      (SELECT COUNT(*) FROM dependencies d
                       WHERE d.version_id=v.version_id AND d.confirmed=1) AS dependencies,
                      (SELECT COUNT(*) FROM code_references r
                       WHERE r.version_id=v.version_id AND r.confirmed=1) AS references_count
               FROM files f
               JOIN file_versions v ON v.version_id=f.current_version_id
               JOIN symbols m ON m.version_id=v.version_id AND m.kind='module'
               LEFT JOIN symbols c ON c.parent_symbol_id=m.symbol_id
               WHERE f.status='current' AND v.invalidated_ns IS NULL
                 AND v.language='python' AND v.analysis_status='complete'
                 AND m.confirmed=1
               GROUP BY v.version_id,v.path_observed,m.symbol_id,m.name,m.start_line,m.end_line
               ORDER BY v.path_observed,m.symbol_id"""
        ).fetchall()
    )
    if len(rows) > INTERFACE_MAX_MODULES:
        raise ValueError("module inventory exceeds its hard bound")
    selected: list[ModuleSurfaceObservation] = []
    for row in rows:
        line_count = int(row["end_line"]) - int(row["start_line"]) + 1
        direct_symbols = int(row["direct_symbols"])
        public_symbols = int(row["public_symbols"] or 0)
        values: dict[str, object] = {
            "version_id": int(row["version_id"]),
            "symbol_id": int(row["symbol_id"]),
            "path": str(row["path_observed"]),
            "module_name": str(row["name"]),
            "line_count": line_count,
            "direct_symbols": direct_symbols,
            "public_direct_symbols": public_symbols,
            "direct_functions": int(row["functions"] or 0),
            "direct_classes": int(row["classes"] or 0),
            "direct_variables": int(row["variables"] or 0),
            "confirmed_dependencies": int(row["dependencies"]),
            "confirmed_references": int(row["references_count"]),
        }
        reasons = tuple(
            sorted(
                reason
                for reason, enabled in (
                    ("line_threshold", line_count >= MODULE_LINE_THRESHOLD),
                    (
                        "direct_symbol_threshold",
                        direct_symbols >= MODULE_DIRECT_SYMBOL_THRESHOLD,
                    ),
                    (
                        "public_symbol_threshold",
                        public_symbols >= MODULE_PUBLIC_SYMBOL_THRESHOLD,
                    ),
                )
                if enabled
            )
        )
        if not reasons:
            continue
        values["selection_reasons"] = reasons
        values["authority"] = "advisory"
        values["mutation_authority"] = False
        selected.append(
            ModuleSurfaceObservation(
                observation_id=analysis_identity("module-surface-observation-v1", values),
                **values,  # type: ignore[arg-type]
            )
        )
    selected.sort(
        key=lambda item: (
            -item.line_count,
            -item.direct_symbols,
            -item.public_direct_symbols,
            item.path,
            item.symbol_id,
        )
    )
    return len(rows), len(selected), tuple(selected[:limit])


def _decode_published_text(row: sqlite3.Row, *, budget: list[int]) -> str | None:
    raw = row["text_zlib"]
    chars = int(row["text_chars"])
    if raw is None or bool(row["text_truncated"]) or chars > INTERFACE_MAX_SOURCE_BYTES:
        return None
    try:
        decoder = zlib.decompressobj()
        payload = decoder.decompress(bytes(raw), INTERFACE_MAX_SOURCE_BYTES + 1)
        if len(payload) > INTERFACE_MAX_SOURCE_BYTES or not decoder.eof:
            return None
        text = payload.decode("utf-8")
    except (UnicodeDecodeError, ValueError, zlib.error):
        return None
    if len(text) != chars or budget[0] + len(payload) > INTERFACE_MAX_TOTAL_SOURCE_BYTES:
        return None
    budget[0] += len(payload)
    return text


def _configuration_shape(value: object) -> tuple[int, int, int, int, tuple[str, ...], bool]:
    top_keys = tuple(sorted(str(key) for key in value)) if isinstance(value, Mapping) else ()
    stack: list[tuple[object, int]] = [(value, 0)]
    total_keys = 0
    leaves = 0
    maximum_depth = 0
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > INTERFACE_MAX_CONFIG_NODES or depth > INTERFACE_MAX_CONFIG_DEPTH:
            raise ValueError("configuration structure exceeds its bound")
        maximum_depth = max(maximum_depth, depth)
        if isinstance(current, Mapping):
            total_keys += len(current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        else:
            leaves += 1
    examples = top_keys[:INTERFACE_EXAMPLE_LIMIT]
    return len(top_keys), total_keys, leaves, maximum_depth, examples, len(top_keys) > len(examples)


def _configuration_observations(
    connection: sqlite3.Connection,
) -> tuple[ConfigurationSurfaceObservation, ...]:
    rows = tuple(
        connection.execute(
            """SELECT v.version_id,v.path_observed,v.artifact_kind,v.language,
                      v.analysis_status,v.text_zlib,v.text_chars,v.text_truncated
               FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
               WHERE f.status='current' AND v.invalidated_ns IS NULL
                 AND v.artifact_kind IN ('config','manifest','lock')
               ORDER BY v.path_observed,v.version_id"""
        ).fetchall()
    )
    if len(rows) > INTERFACE_MAX_CONFIG_ARTIFACTS:
        raise ValueError("configuration inventory exceeds its hard bound")
    budget = [0]
    observations: list[ConfigurationSurfaceObservation] = []
    for row in rows:
        analysis_status = str(row["analysis_status"])
        language = str(row["language"])
        parser: Literal["json", "toml"] | None = (
            "json" if language == "json" else "toml" if language == "toml" else None
        )
        parse_status: Literal["exact", "unsupported", "incomplete", "error"]
        shape: tuple[int, int, int, int, tuple[str, ...], bool] | None = None
        if analysis_status != "complete":
            parse_status = "incomplete"
            parser = None
        elif parser is None:
            parse_status = "unsupported"
        else:
            text = _decode_published_text(row, budget=budget)
            if text is None:
                parse_status = "incomplete"
                parser = None
            else:
                try:
                    decoded = json.loads(text) if parser == "json" else tomllib.loads(text)
                    shape = _configuration_shape(decoded)
                    parse_status = "exact"
                except (json.JSONDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
                    parse_status = "error"
                    parser = None
        values = {
            "version_id": int(row["version_id"]),
            "path": str(row["path_observed"]),
            "artifact_kind": str(row["artifact_kind"]),
            "language": language,
            "analysis_status": analysis_status,
            "text_chars": int(row["text_chars"]),
            "parse_status": parse_status,
            "parser_kind": parser,
            "top_level_keys": None if shape is None else shape[0],
            "total_keys": None if shape is None else shape[1],
            "leaf_values": None if shape is None else shape[2],
            "max_depth": None if shape is None else shape[3],
            "top_level_key_examples": () if shape is None else shape[4],
            "examples_truncated": False if shape is None else shape[5],
            "authority": "advisory",
            "mutation_authority": False,
        }
        observations.append(
            ConfigurationSurfaceObservation(
                observation_id=analysis_identity("configuration-surface-observation-v1", values),
                **values,  # type: ignore[arg-type]
            )
        )
    return tuple(observations)


_ARGPARSE_CALLS = frozenset({"add_argument", "add_parser", "add_subparsers", "add_argument_group"})


def _call_name(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _literal_text(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _cli_ast_projection(text: str) -> dict[str, object]:
    tree = ast.parse(text)
    argparse_calls = 0
    add_argument_calls = 0
    literal_argument_calls = 0
    dynamic_argument_calls = 0
    positional_argument_calls = 0
    add_parser_calls = 0
    option_strings: set[str] = set()
    subcommands: set[str] = set()
    keyword_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in _ARGPARSE_CALLS:
            continue
        argparse_calls += 1
        keyword_names.update(item.arg if item.arg is not None else "**" for item in node.keywords)
        if name == "add_argument":
            add_argument_calls += 1
            literals = tuple(_literal_text(item) for item in node.args)
            if not literals or any(item is None for item in literals):
                dynamic_argument_calls += 1
                continue
            values = cast(tuple[str, ...], literals)
            literal_argument_calls += 1
            options = tuple(item for item in values if item.startswith("-"))
            if options:
                option_strings.update(options)
            else:
                positional_argument_calls += 1
        elif name == "add_parser":
            add_parser_calls += 1
            if node.args:
                literal = _literal_text(node.args[0])
                if literal is not None:
                    subcommands.add(literal)
    ordered_options = tuple(sorted(option_strings))
    ordered_subcommands = tuple(sorted(subcommands))
    remaining = INTERFACE_EXAMPLE_LIMIT
    option_examples = ordered_options[:remaining]
    remaining -= len(option_examples)
    subcommand_examples = ordered_subcommands[:remaining]
    return {
        "ast_argparse_call_sites": argparse_calls,
        "add_argument_calls": add_argument_calls,
        "literal_argument_calls": literal_argument_calls,
        "dynamic_argument_calls": dynamic_argument_calls,
        "literal_option_strings": len(ordered_options),
        "positional_argument_calls": positional_argument_calls,
        "add_parser_calls": add_parser_calls,
        "literal_subcommands": len(ordered_subcommands),
        "option_examples": option_examples,
        "subcommand_examples": subcommand_examples,
        "keyword_names": tuple(sorted(keyword_names)),
        "examples_truncated": len(ordered_options) + len(ordered_subcommands)
        > len(option_examples) + len(subcommand_examples),
    }


def _cli_observations(connection: sqlite3.Connection) -> tuple[CliSurfaceObservation, ...]:
    rows = tuple(
        connection.execute(
            """SELECT v.version_id,v.path_observed,v.analysis_status,v.text_zlib,
                      v.text_chars,v.text_truncated,COUNT(r.reference_id) AS call_sites
               FROM files f
               JOIN file_versions v ON v.version_id=f.current_version_id
               JOIN code_references r ON r.version_id=v.version_id
               WHERE f.status='current' AND v.invalidated_ns IS NULL
                 AND v.language='python' AND r.kind='call'
                 AND (r.name LIKE '%.add_argument'
                      OR r.name LIKE '%.add_parser'
                      OR r.name LIKE '%.add_subparsers'
                      OR r.name LIKE '%.add_argument_group')
               GROUP BY v.version_id,v.path_observed,v.analysis_status,
                        v.text_zlib,v.text_chars,v.text_truncated
               ORDER BY v.path_observed,v.version_id"""
        ).fetchall()
    )
    if len(rows) > INTERFACE_MAX_MODULES:
        raise ValueError("CLI candidate inventory exceeds its hard bound")
    budget = [0]
    result: list[CliSurfaceObservation] = []
    for row in rows:
        recorded = int(row["call_sites"])
        parse_status: Literal["exact", "incomplete", "error"]
        projection: dict[str, object] | None = None
        if str(row["analysis_status"]) != "complete":
            parse_status = "incomplete"
        else:
            text = _decode_published_text(row, budget=budget)
            if text is None:
                parse_status = "incomplete"
            else:
                try:
                    projection = _cli_ast_projection(text)
                    parse_status = "exact"
                except (SyntaxError, TypeError, ValueError):
                    parse_status = "error"
        values = {
            "version_id": int(row["version_id"]),
            "path": str(row["path_observed"]),
            "parse_status": parse_status,
            "recorded_argparse_call_sites": recorded,
            "ast_argparse_call_sites": 0
            if projection is None
            else projection["ast_argparse_call_sites"],
            "add_argument_calls": 0 if projection is None else projection["add_argument_calls"],
            "literal_argument_calls": 0
            if projection is None
            else projection["literal_argument_calls"],
            "dynamic_argument_calls": 0
            if projection is None
            else projection["dynamic_argument_calls"],
            "literal_option_strings": 0
            if projection is None
            else projection["literal_option_strings"],
            "positional_argument_calls": 0
            if projection is None
            else projection["positional_argument_calls"],
            "add_parser_calls": 0 if projection is None else projection["add_parser_calls"],
            "literal_subcommands": 0 if projection is None else projection["literal_subcommands"],
            "option_examples": () if projection is None else projection["option_examples"],
            "subcommand_examples": () if projection is None else projection["subcommand_examples"],
            "keyword_names": () if projection is None else projection["keyword_names"],
            "examples_truncated": False if projection is None else projection["examples_truncated"],
            "authority": "advisory",
            "mutation_authority": False,
        }
        result.append(
            CliSurfaceObservation(
                observation_id=analysis_identity("cli-surface-observation-v1", values),
                **values,  # type: ignore[arg-type]
            )
        )
    return tuple(result)


def read_code_interface_surface_analysis(
    connection: sqlite3.Connection,
    *,
    analysis_run_id: int,
    processing_signature: str,
    database: str,
    limit: int = INTERFACE_SURFACE_LIMIT,
) -> CodeInterfaceSurfaceAnalysis:
    """Read one complete interface projection from a validated query-only connection."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("interface surface requires a SQLite connection")
    if (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("interface surface run must be positive")
    _required_text("interface processing signature", processing_signature)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("interface surface limit must be between 1 and 1000")
    try:
        total_modules, selected_modules, modules = _module_observations(connection, limit=limit)
        configurations = _configuration_observations(connection)
        cli_files = _cli_observations(connection)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return abstained_code_interface_surface(
            f"interface_surface_unresolvable:{type(exc).__name__}",
            database=database,
        )
    exact_config = sum(item.parse_status == "exact" for item in configurations)
    unsupported_config = sum(item.parse_status == "unsupported" for item in configurations)
    incomplete_config = sum(item.parse_status == "incomplete" for item in configurations)
    errored_config = sum(item.parse_status == "error" for item in configurations)
    exact_cli = sum(item.parse_status == "exact" for item in cli_files)
    incomplete_cli = sum(item.parse_status == "incomplete" for item in cli_files)
    errored_cli = sum(item.parse_status == "error" for item in cli_files)
    values = {
        "database": database,
        "analysis_run_id": analysis_run_id,
        "processing_signature": processing_signature,
        "status": "ready",
        "reason": None,
        "policy_id": CODE_INTERFACE_SURFACE_POLICY,
        "total_modules": total_modules,
        "selected_modules": selected_modules,
        "returned_modules": len(modules),
        "module_selection_truncated": selected_modules > len(modules),
        "modules": modules,
        "configuration_artifacts": len(configurations),
        "exact_configuration_artifacts": exact_config,
        "unsupported_configuration_artifacts": unsupported_config,
        "incomplete_configuration_artifacts": incomplete_config,
        "errored_configuration_artifacts": errored_config,
        "configurations": configurations,
        "cli_candidate_files": len(cli_files),
        "exact_cli_files": exact_cli,
        "incomplete_cli_files": incomplete_cli,
        "errored_cli_files": errored_cli,
        "recorded_argparse_call_sites": sum(
            item.recorded_argparse_call_sites for item in cli_files
        ),
        "ast_argparse_call_sites": sum(item.ast_argparse_call_sites for item in cli_files),
        "cli_files": cli_files,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _analysis(values)


def _surface_subject(
    analysis: CodeInterfaceSurfaceAnalysis,
    *,
    kind: Literal["module", "configuration", "entrypoint"],
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisSubjectRef:
    assert analysis.processing_signature is not None
    display = {
        "module": "Python module surface selection",
        "configuration": "Configuration artifact surface",
        "entrypoint": "Static argparse call surface",
    }[kind]
    return AnalysisSubjectRef(
        subject_kind=kind,
        subject_key=f"{kind}:neocortex-interface-surface",
        display_name=display,
        source_owner_id="code",
        snapshot_id=analysis.processing_signature,
        snapshot_freshness=snapshot_freshness,
        revision_id=analysis.analysis_id,
    )


def _surface_evidence(
    analysis: CodeInterfaceSurfaceAnalysis,
    subject: AnalysisSubjectRef,
    *,
    domain: Literal["module", "configuration", "entrypoint"],
) -> AnalysisEvidenceRef:
    facts: tuple[AnalysisFact, ...]
    if domain == "module":
        facts = (
            AnalysisFact("total_modules", analysis.total_modules, "count"),
            AnalysisFact("selected_modules", analysis.selected_modules, "count"),
            AnalysisFact("returned_modules", analysis.returned_modules, "count"),
            AnalysisFact("selection_truncated", analysis.module_selection_truncated),
            AnalysisFact("line_threshold", MODULE_LINE_THRESHOLD, "lines"),
            AnalysisFact("direct_symbol_threshold", MODULE_DIRECT_SYMBOL_THRESHOLD, "count"),
            AnalysisFact("public_symbol_threshold", MODULE_PUBLIC_SYMBOL_THRESHOLD, "count"),
        )
        truncated = analysis.module_selection_truncated
    elif domain == "configuration":
        facts = (
            AnalysisFact("configuration_artifacts", analysis.configuration_artifacts, "count"),
            AnalysisFact("exact_artifacts", analysis.exact_configuration_artifacts, "count"),
            AnalysisFact(
                "unsupported_artifacts", analysis.unsupported_configuration_artifacts, "count"
            ),
            AnalysisFact(
                "incomplete_artifacts", analysis.incomplete_configuration_artifacts, "count"
            ),
            AnalysisFact("errored_artifacts", analysis.errored_configuration_artifacts, "count"),
        )
        truncated = False
    else:
        facts = (
            AnalysisFact("cli_candidate_files", analysis.cli_candidate_files, "count"),
            AnalysisFact("exact_cli_files", analysis.exact_cli_files, "count"),
            AnalysisFact("incomplete_cli_files", analysis.incomplete_cli_files, "count"),
            AnalysisFact("errored_cli_files", analysis.errored_cli_files, "count"),
            AnalysisFact(
                "recorded_argparse_call_sites", analysis.recorded_argparse_call_sites, "count"
            ),
            AnalysisFact("ast_argparse_call_sites", analysis.ast_argparse_call_sites, "count"),
        )
        truncated = False
    projection_digest = analysis_identity(
        f"code-{domain}-surface-projection-v1",
        {"analysis": analysis.analysis_id, "facts": tuple(asdict(item) for item in facts)},
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            f"code-{domain}-surface-evidence-v1",
            {"subject": subject.subject_key, "projection": projection_digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_metric",
        source_owner_id="code",
        producer_id="code-interface-surface-resolver",
        producer_version="v1",
        source_schema=f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
        source_record_kind=f"{domain}_surface_projection",
        source_record_id=str(analysis.analysis_run_id),
        source_projection_digest=projection_digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="partial" if truncated else "complete",
        bounded=True,
        truncated=truncated,
        resolver_id="code-interface-surface-resolver",
        resolver_version="v1",
        limitations=("surface_projection_does_not_establish_runtime_behavior_or_maintenance_harm",),
    )


def _surface_evaluation(
    analysis: CodeInterfaceSurfaceAnalysis,
    spec: AnalysisQuestionSpec,
    *,
    domain: Literal["module", "configuration", "entrypoint"],
    rank: int,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisQuestionEvaluation:
    subject = _surface_subject(
        analysis,
        kind=domain,
        snapshot_freshness=snapshot_freshness,
    )
    evidence = _surface_evidence(analysis, subject, domain=domain)
    first = spec.requirements[0]
    decision_support = spec.requirements[1]
    counter = spec.requirements[2]
    experiment = spec.requirements[3]
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            f"code-{domain}-surface-question-v1",
            {
                "analysis": analysis.analysis_id,
                "spec": analysis_question_spec_fingerprint(spec),
                "evidence": evidence.evidence_id,
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=(evidence,),
        requirements=(
            AnalysisRequirementEvaluation(
                first.requirement_id,
                "satisfied",
                (evidence.evidence_id,),
                "linked_published_interface_surface_projection",
            ),
            AnalysisRequirementEvaluation(
                decision_support.requirement_id,
                "missing",
                (),
                "semantic_or_runtime_consumer_evidence_not_linked",
            ),
            AnalysisRequirementEvaluation(
                counter.requirement_id,
                "not_evaluated",
                (),
                "declared_role_and_dynamic_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                experiment.requirement_id,
                "missing",
                (),
                "characterization_or_acceptance_experiment_not_linked",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return evaluation


def interface_surface_questions(
    analysis: CodeInterfaceSurfaceAnalysis,
    *,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("interface surface rank offset must be non-negative")
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("interface surface snapshot freshness is invalid")
    if analysis.status != "ready":
        spec = INTERFACE_SURFACE_AVAILABILITY_QUESTION
        reason = analysis.reason or "interface_surface_evidence_unavailable"
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "code-interface-surface-availability-question-v1",
                {
                    "analysis_id": analysis.analysis_id,
                    "question": spec.question_id,
                    "reason": reason,
                },
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
            rank=rank_offset + 1,
            subject=AnalysisSubjectRef(
                subject_kind="run",
                subject_key=f"interface-surface-analysis:{analysis.analysis_id}",
                display_name="Code interface surface evidence",
                source_owner_id="code",
                snapshot_id=analysis.analysis_id,
                snapshot_freshness=snapshot_freshness,
                revision_id=CODE_INTERFACE_SURFACE_SCHEMA,
            ),
            evidence=(),
            requirements=(
                AnalysisRequirementEvaluation(
                    "published_code_run_resolved",
                    "missing",
                    (),
                    reason,
                ),
                AnalysisRequirementEvaluation(
                    "module_configuration_and_cli_projection_resolved",
                    "missing",
                    (),
                    "interface_projection_unavailable",
                ),
                AnalysisRequirementEvaluation(
                    "runtime_interface_contract_resolved",
                    "not_evaluated",
                    (),
                    "runtime_contract_not_evaluated_without_static_subjects",
                ),
                AnalysisRequirementEvaluation(
                    "incomplete_or_dynamic_surface_counterevidence_evaluated",
                    "not_evaluated",
                    (),
                    "counterevidence_not_evaluated_without_resolved_projection",
                ),
            ),
            observation_status="abstained",
            inference_status="abstained",
            inferences=(),
            hypotheses=spec.hypotheses,
            question_readiness="abstained",
            decision_readiness="abstained",
            decision=None,
            decision_reason="question_evidence_incomplete",
            counterevidence_status="not_evaluated",
            next_action_ids=(),
            limitations=(
                *_LIMITATIONS,
                "analysis_envelope_identity_is_not_a_resolved_code_publication",
                reason,
            ),
        )
        validate_analysis_question_evaluation(spec, evaluation)
        return (spec,), (evaluation,)
    specs = (MODULE_SURFACE_QUESTION, CONFIGURATION_SURFACE_QUESTION, CLI_SURFACE_QUESTION)
    domains: tuple[Literal["module", "configuration", "entrypoint"], ...] = (
        "module",
        "configuration",
        "entrypoint",
    )
    evaluations = tuple(
        _surface_evaluation(
            analysis,
            spec,
            domain=domain,
            rank=rank_offset + index,
            snapshot_freshness=snapshot_freshness,
        )
        for index, (spec, domain) in enumerate(
            zip(specs, domains, strict=True),
            start=1,
        )
    )
    return specs, evaluations


def parse_code_interface_surface_payload(
    payload: Mapping[str, object],
) -> CodeInterfaceSurfaceAnalysis:
    expected = {field.name for field in fields(CodeInterfaceSurfaceAnalysis)} | {"schema"}
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected
        or payload.get("schema") != CODE_INTERFACE_SURFACE_SCHEMA
    ):
        raise ValueError("interface surface payload envelope is invalid")
    values = {key: value for key, value in payload.items() if key != "schema"}
    nested = (
        ("modules", ModuleSurfaceObservation),
        ("configurations", ConfigurationSurfaceObservation),
        ("cli_files", CliSurfaceObservation),
    )
    for key, model in nested:
        raw_items = values[key]
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
            raise ValueError(f"interface {key} must be a sequence")
        expected_fields = {field.name for field in fields(model)}
        parsed = []
        for raw in raw_items:
            if not isinstance(raw, Mapping) or set(raw) != expected_fields:
                raise ValueError(f"interface {key} fields are invalid")
            item_values = dict(raw)
            for tuple_key in {
                "selection_reasons",
                "top_level_key_examples",
                "option_examples",
                "subcommand_examples",
                "keyword_names",
            }.intersection(item_values):
                item_values[tuple_key] = _text_tuple(
                    f"interface {key} {tuple_key}", item_values[tuple_key]
                )
            parsed.append(model(**item_values))
        values[key] = tuple(parsed)
    values["limitations"] = _text_tuple("interface limitations", values["limitations"])
    return CodeInterfaceSurfaceAnalysis(**values)  # type: ignore[arg-type]


__all__ = [
    "CLI_SURFACE_QUESTION",
    "CODE_INTERFACE_SURFACE_SCHEMA",
    "CONFIGURATION_SURFACE_QUESTION",
    "INTERFACE_SURFACE_AVAILABILITY_QUESTION",
    "MODULE_SURFACE_QUESTION",
    "CliSurfaceObservation",
    "CodeInterfaceSurfaceAnalysis",
    "ConfigurationSurfaceObservation",
    "ModuleSurfaceObservation",
    "abstained_code_interface_surface",
    "interface_surface_questions",
    "parse_code_interface_surface_payload",
    "read_code_interface_surface_analysis",
]
