"""Fail-closed behavioral-assurance projection over existing Code evidence.

Coverage contexts answer which declared tests executed which recorded lines.
They do not answer whether those tests assert a behavior or protect an
invariant.  This module preserves that boundary explicitly and correlates the
coverage projection with the existing focal Cosmic Ray provider without
creating another evidence store.

Version 1 deliberately has no assertion/invariant-link provider and no runtime
scenario provider.  Those dimensions therefore remain ``not_recorded``; test
names, source names, percentages, and arbitrary provider metadata are never
promoted to assertion evidence.  A complete mutation experiment is retained
as a distinct experiment/counterevidence dimension, not as proof of
correctness.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSourceLocation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_set,
)
from .code_coverage_analysis import (
    CODE_COVERAGE_PROVIDER_ID,
    CODE_COVERAGE_SCHEMA,
    CodeCoverageAnalysis,
    CoverageScopeSummary,
    TestToSymbolRelation,
)
from .external_evidence_models import ExternalProviderEvidence, ExternalProviderMetric
from .external_mutation_cosmic_ray import (
    COSMIC_RAY_MUTATION_PROVIDER_ID,
    COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
)
from .semantic_models import canonical_json

CODE_ASSURANCE_SCHEMA = "neocortex.code-assurance-analysis/v1"
CODE_ASSURANCE_POLICY = "coverage-context-plus-independent-behavior-evidence-v1"

AssuranceStatus = Literal["ready", "abstained"]
ExecutionEvidenceStatus = Literal["observed", "not_observed", "not_evaluated"]
RecordedEvidenceStatus = Literal["not_recorded"]
MutationEvidenceStatus = Literal[
    "not_recorded",
    "not_targeted",
    "abstained",
    "incompatible",
    "incomplete",
    "complete",
]

_ANALYSIS_LIMITATIONS = (
    "coverage_context_proves_listed_line_execution_not_assertion_or_invariant_protection",
    "assertion_invariant_evidence_provider_not_registered_in_v1",
    "runtime_scenario_evidence_provider_not_registered_in_v1",
    "mutation_is_focal_experimental_evidence_not_proof_of_correctness",
    "no_behavioral_protection_label_is_emitted",
    "calibration_requires_independent_outcome_labels",
    "human_decision_not_owned_by_code_analysis",
)
_OBSERVATION_LIMITATIONS = (
    "executing_tests_are_coverage_contexts_not_protecting_tests",
    "coverage_percentage_does_not_prove_assertion_quality",
    "assertion_or_invariant_links_are_not_inferred_from_names_source_or_metadata",
    "coverage_symbol_revision_digest_is_not_exposed_by_the_v1_projection",
    "runtime_scenarios_are_not_inferred_from_test_execution",
    "human_decision_not_owned_by_code_analysis",
)
_CALIBRATION_LIMITATIONS = (
    "no_independent_human_decision_or_escaped_defect_labels_were_observed",
    "coverage_only_subjects_are_a_workload_count_not_a_false_positive_rate",
    "precision_recall_and_decision_rate_are_not_claimed_without_outcome_labels",
)


ASSURANCE_QUESTION = AnalysisQuestionSpec(
    question_id="assurance.symbol_behavior_is_independently_observed",
    version="v1",
    subject_kinds=("symbol",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "declared_test_execution_observed",
            "question",
            "supporting",
            ("external_metric",),
        ),
        AnalysisEvidenceRequirementSpec(
            "symbol_line_execution_context_observed",
            "question",
            "supporting",
            ("external_relation",),
            accepted_completeness=("complete", "partial"),
            allow_truncated=True,
        ),
        AnalysisEvidenceRequirementSpec(
            "explicit_assertion_or_invariant_link_observed",
            "decision",
            "supporting",
            ("contract", "internal_relation", "external_relation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "discriminating_mutation_or_runtime_scenario_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "negative_control_or_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "recorded_tests_execute_the_symbol_and_discriminate_a_declared_invariant",
        "recorded_tests_execute_lines_without_evidence_that_they_protect_declared_behavior",
    ),
    counterevidence_rules=(
        "a_test_context_without_an_explicit_assertion_or_invariant_link_is_execution_only",
        "surviving_mutants_or_failed_runtime_scenarios_are_preserved_as_counterevidence",
        "full_line_or_branch_coverage_is_not_assertion_evidence",
        "test_names_and_untyped_metadata_never_establish_behavioral_protection",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "declare_invariant_and_resolve_assertion_link",
            "characterization",
            "Declare the relevant invariant and resolve an explicit test-to-invariant assertion record.",
        ),
        AnalysisNextActionSpec(
            "seek_negative_control_or_surviving_mutant",
            "counterevidence_search",
            "Seek a negative control, surviving mutant, or failing scenario that challenges the claim.",
        ),
        AnalysisNextActionSpec(
            "run_focal_mutation_or_declared_runtime_scenario",
            "experiment",
            "Run the cheapest focal mutation campaign or declared runtime scenario that discriminates behavior.",
        ),
    ),
)


class CodeAssuranceEvidenceError(ValueError):
    """A provider projection is incompatible with the assurance contract."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _valid_run_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


@dataclass(frozen=True, slots=True)
class MutationAssuranceObservation:
    status: MutationEvidenceStatus
    reason: str
    target_symbol: str | None
    test_selectors: tuple[str, ...]
    generated: int | None
    selected: int | None
    completed: int | None
    killed: int | None
    survived: int | None
    timed_out: int | None
    incompetent: int | None
    measurement_scope_signature: str | None
    discriminating_result_observed: bool
    counterevidence_evaluated: bool

    def __post_init__(self) -> None:
        if self.status not in {
            "not_recorded",
            "not_targeted",
            "abstained",
            "incompatible",
            "incomplete",
            "complete",
        }:
            raise ValueError("mutation assurance status is invalid")
        _required_text("mutation assurance reason", self.reason, maximum=256)
        if self.target_symbol is not None:
            _required_text("mutation assurance target", self.target_symbol, maximum=1_024)
        if self.measurement_scope_signature is not None:
            _required_text(
                "mutation measurement scope",
                self.measurement_scope_signature,
                maximum=512,
            )
        if self.test_selectors != tuple(sorted(set(self.test_selectors))):
            raise ValueError("mutation test selectors must be sorted and unique")
        for selector in self.test_selectors:
            _required_text("mutation test selector", selector, maximum=16_384)
        counts = (
            self.generated,
            self.selected,
            self.completed,
            self.killed,
            self.survived,
            self.timed_out,
            self.incompetent,
        )
        for value in counts:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("mutation assurance counts must be non-negative integers")
        if self.status == "complete":
            if any(value is None for value in counts):
                raise ValueError("complete mutation assurance requires outcome counts")
            generated, selected, completed, killed, survived, timed_out, incompetent = cast(
                tuple[int, int, int, int, int, int, int], counts
            )
            if not (
                generated >= selected == completed > 0
                and completed == killed + survived + timed_out + incompetent
                and timed_out == 0
                and incompetent == 0
            ):
                raise ValueError("complete mutation assurance count partition is invalid")
            if not self.discriminating_result_observed or not self.counterevidence_evaluated:
                raise ValueError("complete mutation assurance must preserve experiment evidence")
        elif self.discriminating_result_observed or self.counterevidence_evaluated:
            raise ValueError("incomplete mutation evidence cannot satisfy assurance requirements")


@dataclass(frozen=True, slots=True)
class SymbolAssuranceObservation:
    observation_id: str
    rank: int
    subject_key: str
    display_name: str
    relative_path: str | None
    start_line: int | None
    end_line: int | None
    test_execution_status: ExecutionEvidenceStatus
    test_execution_reason: str
    coverage_observed: bool
    executing_tests: tuple[str, ...]
    covered_lines: int
    executable_lines: int
    line_coverage_percent: float | None
    covered_branch_exits: int
    branch_exits: int
    branch_coverage_percent: float | None
    assertion_invariant_evidence_status: RecordedEvidenceStatus
    mutation: MutationAssuranceObservation
    runtime_scenario_evidence_status: RecordedEvidenceStatus
    behavioral_assurance_status: Literal["not_established"]
    question_evaluation_id: str
    limitations: tuple[str, ...] = _OBSERVATION_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("assurance observation id", self.observation_id, maximum=256)
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("assurance observation rank must be positive")
        _required_text("assurance subject key", self.subject_key, maximum=1_024)
        _required_text("assurance display name", self.display_name, maximum=2_048)
        if self.relative_path is not None:
            _required_text("assurance relative path", self.relative_path)
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("assurance source line range must be complete or absent")
        if self.start_line is not None and (
            isinstance(self.start_line, bool)
            or not isinstance(self.start_line, int)
            or self.start_line < 1
            or isinstance(self.end_line, bool)
            or not isinstance(self.end_line, int)
            or self.end_line < self.start_line
        ):
            raise ValueError("assurance source line range is invalid")
        if self.test_execution_status not in {
            "observed",
            "not_observed",
            "not_evaluated",
        }:
            raise ValueError("assurance test execution status is invalid")
        _required_text("assurance test execution reason", self.test_execution_reason, maximum=256)
        if not isinstance(self.coverage_observed, bool):
            raise ValueError("assurance coverage observation flag must be boolean")
        if self.executing_tests != tuple(sorted(set(self.executing_tests))):
            raise ValueError("executing tests must be sorted and unique")
        if self.coverage_observed != bool(self.executing_tests):
            raise ValueError("coverage observation must be derived from executing test contexts")
        for nodeid in self.executing_tests:
            _required_text("executing test nodeid", nodeid, maximum=16_384)
        for value in (
            self.covered_lines,
            self.executable_lines,
            self.covered_branch_exits,
            self.branch_exits,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("assurance coverage counts must be non-negative integers")
        if self.covered_lines > self.executable_lines:
            raise ValueError("covered lines exceed executable lines")
        if self.covered_branch_exits > self.branch_exits:
            raise ValueError("covered branches exceed branch exits")
        for percent in (self.line_coverage_percent, self.branch_coverage_percent):
            if percent is not None and (
                isinstance(percent, bool)
                or not math.isfinite(float(percent))
                or not 0.0 <= float(percent) <= 100.0
            ):
                raise ValueError("assurance coverage percentage is invalid")
        if self.assertion_invariant_evidence_status != "not_recorded":
            raise ValueError("assurance v1 cannot invent assertion or invariant evidence")
        if self.runtime_scenario_evidence_status != "not_recorded":
            raise ValueError("assurance v1 cannot invent runtime scenario evidence")
        if self.behavioral_assurance_status != "not_established":
            raise ValueError("assurance v1 cannot claim behavioral protection")
        _required_text(
            "assurance question evaluation id",
            self.question_evaluation_id,
            maximum=1_024,
        )
        if self.limitations != _OBSERVATION_LIMITATIONS:
            raise ValueError("assurance observation limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("assurance observation must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class CodeAssuranceCalibration:
    status: Literal["not_established"]
    independent_outcome_labels: int
    evaluated_subjects: int
    execution_and_coverage_observed: int
    coverage_only_subjects: int
    explicit_assertion_invariant_links: int
    discriminating_mutation_results: int
    runtime_scenario_results: int
    human_review_ready: int
    behavioral_assurance_claims: int
    precision_at_k: None
    recall: None
    finding_to_decision_rate: None
    limitations: tuple[str, ...] = _CALIBRATION_LIMITATIONS

    def __post_init__(self) -> None:
        if self.status != "not_established":
            raise ValueError("assurance calibration requires independent labels")
        for value in (
            self.independent_outcome_labels,
            self.evaluated_subjects,
            self.execution_and_coverage_observed,
            self.coverage_only_subjects,
            self.explicit_assertion_invariant_links,
            self.discriminating_mutation_results,
            self.runtime_scenario_results,
            self.human_review_ready,
            self.behavioral_assurance_claims,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("assurance calibration counts must be non-negative integers")
        if any(
            value != 0
            for value in (
                self.independent_outcome_labels,
                self.explicit_assertion_invariant_links,
                self.runtime_scenario_results,
                self.human_review_ready,
                self.behavioral_assurance_claims,
            )
        ):
            raise ValueError("assurance v1 cannot claim unavailable calibration evidence")
        if self.execution_and_coverage_observed > self.evaluated_subjects:
            raise ValueError("assurance calibration coverage count exceeds evaluated subjects")
        if self.coverage_only_subjects > self.execution_and_coverage_observed:
            raise ValueError("coverage-only count exceeds execution/coverage observations")
        if self.discriminating_mutation_results > self.evaluated_subjects:
            raise ValueError("assurance mutation count exceeds evaluated subjects")
        if any(
            value is not None
            for value in (self.precision_at_k, self.recall, self.finding_to_decision_rate)
        ):
            raise ValueError("uncalibrated assurance cannot publish performance rates")
        if self.limitations != _CALIBRATION_LIMITATIONS:
            raise ValueError("assurance calibration limitations are not canonical")


@dataclass(frozen=True, slots=True)
class CodeAssuranceAnalysis:
    analysis_id: str
    snapshot_id: str
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    status: AssuranceStatus
    reason: str | None
    policy_id: str
    coverage_provider_id: str
    coverage_measurement_scope_signature: str | None
    eligible_symbols: int
    returned_symbols: int
    selection_truncated: bool
    observations: tuple[SymbolAssuranceObservation, ...]
    question_specs: tuple[AnalysisQuestionSpec, ...]
    question_evaluations: tuple[AnalysisQuestionEvaluation, ...]
    calibration: CodeAssuranceCalibration
    limitations: tuple[str, ...] = _ANALYSIS_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("assurance analysis id", self.analysis_id, maximum=256)
        _required_text("assurance snapshot", self.snapshot_id, maximum=2_048)
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("assurance snapshot freshness is invalid")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("assurance analysis status is invalid")
        if (self.status == "ready") != (self.reason is None):
            raise ValueError("assurance status and reason disagree")
        if self.reason is not None:
            _required_text("assurance abstention reason", self.reason, maximum=256)
        if self.policy_id != CODE_ASSURANCE_POLICY:
            raise ValueError("assurance analysis policy is invalid")
        if self.coverage_provider_id != CODE_COVERAGE_PROVIDER_ID:
            raise ValueError("assurance coverage provider identity is invalid")
        if self.coverage_measurement_scope_signature is not None:
            _required_text(
                "assurance coverage scope signature",
                self.coverage_measurement_scope_signature,
                maximum=512,
            )
        for value in (self.eligible_symbols, self.returned_symbols):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("assurance symbol counts must be non-negative integers")
        if self.returned_symbols != len(self.observations) or (
            self.returned_symbols > self.eligible_symbols
        ):
            raise ValueError("assurance symbol counts are inconsistent")
        if self.selection_truncated != (self.returned_symbols < self.eligible_symbols):
            raise ValueError("assurance selection truncation disagrees with counts")
        if self.status == "ready" and not self.observations:
            raise ValueError("ready assurance analysis requires observations")
        if self.status == "abstained" and self.observations:
            raise ValueError("abstained assurance analysis cannot publish observations")
        if tuple(item.rank for item in self.observations) != tuple(
            range(1, len(self.observations) + 1)
        ):
            raise ValueError("assurance observation ranks must be contiguous")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("assurance observation identities cannot repeat")
        if self.status == "ready":
            if self.question_specs != (ASSURANCE_QUESTION,):
                raise ValueError("ready assurance analysis requires its canonical question spec")
            validate_analysis_question_set(self.question_specs, self.question_evaluations)
            evaluation_by_id = {item.evaluation_id: item for item in self.question_evaluations}
            if len(evaluation_by_id) != len(self.question_evaluations):
                raise ValueError("assurance question evaluation identities cannot repeat")
            if tuple(item.rank for item in self.question_evaluations) != tuple(
                range(1, len(self.observations) + 1)
            ):
                raise ValueError("assurance question ranks must match observations")
            for observation in self.observations:
                evaluation = evaluation_by_id.get(observation.question_evaluation_id)
                if evaluation is None or evaluation.subject.subject_key != observation.subject_key:
                    raise ValueError("assurance observation/evaluation projection is incomplete")
        elif self.question_specs or self.question_evaluations:
            raise ValueError("abstained assurance analysis cannot assert question evidence")
        if self.calibration != _calibration(self.observations, self.question_evaluations):
            raise ValueError("assurance calibration is not derived from observations")
        if self.limitations != _ANALYSIS_LIMITATIONS:
            raise ValueError("assurance analysis limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("assurance analysis must remain advisory and non-mutating")
        if self.analysis_id != _analysis_identity(
            snapshot_id=self.snapshot_id,
            snapshot_freshness=self.snapshot_freshness,
            status=self.status,
            reason=self.reason,
            coverage_measurement_scope_signature=self.coverage_measurement_scope_signature,
            eligible_symbols=self.eligible_symbols,
            observations=self.observations,
            evaluations=self.question_evaluations,
            calibration=self.calibration,
        ):
            raise ValueError("assurance analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_ASSURANCE_SCHEMA, **asdict(self)}


@dataclass(frozen=True, slots=True)
class _MutationResolution:
    observation: MutationAssuranceObservation
    experiment_evidence: AnalysisEvidenceRef | None = None
    counterevidence: AnalysisEvidenceRef | None = None


def _coverage_toolchain_version(coverage: CodeCoverageAnalysis) -> str:
    return analysis_identity(
        "coverage-toolchain-v1",
        tuple((item.name, item.version) for item in coverage.tool_versions),
    )


def _test_execution_status(
    coverage: CodeCoverageAnalysis,
) -> tuple[ExecutionEvidenceStatus, str]:
    if coverage.status != "ready":
        return "not_evaluated", coverage.reason or "coverage_provider_not_ready"
    if coverage.measurement_complete is not True:
        return "not_evaluated", "coverage_measurement_incomplete"
    if coverage.content_executed is not True:
        return "not_evaluated", "test_content_not_executed"
    if coverage.outcomes is None:
        return "not_evaluated", "test_outcomes_not_recorded"
    gate = next((item for item in coverage.gates if item.gate == "tests_passed"), None)
    if gate is None or gate.status == "not_evaluated":
        return "not_evaluated", "tests_passed_gate_not_evaluated"
    if gate.status != "passed" or coverage.outcomes.failed:
        return "not_observed", gate.reason or "declared_tests_failed"
    if coverage.outcomes.selected < 1 or coverage.outcomes.passed < 1:
        return "not_observed", "no_declared_test_passed"
    if _valid_run_id(coverage.effective_tool_run_id) is None:
        return "not_evaluated", "coverage_provider_run_identity_invalid"
    return "observed", "declared_test_run_completed_and_passed"


def _execution_evidence(
    coverage: CodeCoverageAnalysis,
    *,
    subject_key: str,
    snapshot_id: str,
) -> AnalysisEvidenceRef:
    if coverage.outcomes is None:
        raise CodeAssuranceEvidenceError("coverage_outcomes_missing")
    provider_run_id = _valid_run_id(coverage.effective_tool_run_id)
    if provider_run_id is None:
        raise CodeAssuranceEvidenceError("coverage_provider_run_identity_invalid")
    projection = {
        "provider_id": coverage.provider_id,
        "effective_tool_run_id": provider_run_id,
        "suite_selection": coverage.suite_selection,
        "measurement_complete": coverage.measurement_complete,
        "content_executed": coverage.content_executed,
        "suite_signature": coverage.suite_signature,
        "configuration_signature": coverage.configuration_signature,
        "measurement_scope_signature": coverage.measurement_scope_signature,
        "outcomes": asdict(coverage.outcomes),
        "gates": tuple(asdict(item) for item in coverage.gates),
        "tool_versions": tuple(asdict(item) for item in coverage.tool_versions),
    }
    projection_digest = analysis_identity("code-assurance-coverage-run-projection-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-assurance-test-execution-evidence-v1",
            {
                "subject_key": subject_key,
                "projection_digest": projection_digest,
            },
        ),
        subject_key=subject_key,
        role="supporting",
        evidence_kind="external_metric",
        source_owner_id="code",
        producer_id=CODE_COVERAGE_PROVIDER_ID,
        producer_version=_coverage_toolchain_version(coverage),
        source_schema=CODE_COVERAGE_SCHEMA,
        source_record_kind="coverage_provider_run_projection",
        source_record_id=str(provider_run_id),
        source_projection_digest=projection_digest,
        snapshot_id=snapshot_id,
        revision_id=None,
        facts=(
            AnalysisFact("suite_selection", coverage.suite_selection),
            AnalysisFact("tests_selected", coverage.outcomes.selected, "count"),
            AnalysisFact("tests_passed", coverage.outcomes.passed, "count"),
            AnalysisFact("tests_failed", coverage.outcomes.failed, "count"),
            AnalysisFact("measurement_complete", coverage.measurement_complete),
            AnalysisFact("content_executed", coverage.content_executed),
        ),
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="code.coverage-analysis-assurance-resolver",
        resolver_version="v1",
        limitations=(
            "execution_is_bounded_to_the_declared_suite_and_provider_scope",
            "passing_tests_do_not_prove_assertion_or_invariant_protection",
        ),
        provider_run_id=provider_run_id,
    )


def _coverage_relations_for_symbol(
    coverage: CodeCoverageAnalysis,
    subject_key: str,
) -> tuple[TestToSymbolRelation, ...]:
    return tuple(
        sorted(
            (
                relation
                for relation in coverage.test_relations
                if relation.production_symbol == subject_key
                and relation.test_nodeids
                and relation.lines
            ),
            key=lambda item: (item.test_key, item.relation_id),
        )
    )


def _coverage_relation_evidence(
    coverage: CodeCoverageAnalysis,
    relation: TestToSymbolRelation,
    *,
    subject_key: str,
    snapshot_id: str,
) -> AnalysisEvidenceRef:
    provider_run_id = _valid_run_id(coverage.effective_tool_run_id)
    if provider_run_id is None:
        raise CodeAssuranceEvidenceError("coverage_provider_run_identity_invalid")
    projection = asdict(relation)
    projection_digest = analysis_identity(
        "code-assurance-coverage-relation-projection-v1", projection
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-assurance-coverage-context-evidence-v1",
            {
                "subject_key": subject_key,
                "relation_id": relation.relation_id,
                "projection_digest": projection_digest,
            },
        ),
        subject_key=subject_key,
        role="supporting",
        evidence_kind="external_relation",
        source_owner_id="code",
        producer_id=CODE_COVERAGE_PROVIDER_ID,
        producer_version=_coverage_toolchain_version(coverage),
        source_schema=CODE_COVERAGE_SCHEMA,
        source_record_kind="test_covers_symbol",
        source_record_id=relation.relation_id,
        source_projection_digest=projection_digest,
        snapshot_id=snapshot_id,
        revision_id=None,
        facts=(
            AnalysisFact("test_key", relation.test_key),
            AnalysisFact(
                "test_nodeids_json",
                canonical_json({"test_nodeids": relation.test_nodeids}),
            ),
            AnalysisFact(
                "observed_lines_json",
                canonical_json({"observed_lines": relation.lines}),
            ),
            AnalysisFact(
                "coverage_contexts_json",
                canonical_json({"coverage_contexts": relation.contexts}),
            ),
            AnalysisFact("observed_line_count", len(relation.lines), "count"),
        ),
        completeness="partial",
        bounded=True,
        truncated=True,
        resolver_id="code.coverage-analysis-assurance-resolver",
        resolver_version="v1",
        limitations=(
            "relation_proves_only_the_listed_test_context_and_lines",
            "legacy_coverage_projection_omits_upstream_relation_truncation_flags",
            "coverage_context_is_not_assertion_or_invariant_evidence",
        ),
        provider_run_id=provider_run_id,
    )


def _metric_count(metric: ExternalProviderMetric, expected_unit: str) -> int:
    if metric.unit != expected_unit:
        raise CodeAssuranceEvidenceError(f"mutation_metric_unit_invalid:{metric.metric_name}")
    if not float(metric.value).is_integer() or not 0 <= metric.value <= 1_000_000_000:
        raise CodeAssuranceEvidenceError(f"mutation_metric_count_invalid:{metric.metric_name}")
    return int(metric.value)


def _metric_boolean(metric: ExternalProviderMetric) -> bool:
    value = _metric_count(metric, "boolean")
    if value not in {0, 1}:
        raise CodeAssuranceEvidenceError(f"mutation_metric_boolean_invalid:{metric.metric_name}")
    return bool(value)


def _metadata_mapping(metric: ExternalProviderMetric) -> Mapping[str, object]:
    if not isinstance(metric.metadata, Mapping):
        raise CodeAssuranceEvidenceError("mutation_metric_metadata_not_mapping")
    return metric.metadata


def _metadata_text(metadata: Mapping[str, object], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise CodeAssuranceEvidenceError(f"mutation_metadata_invalid:{name}")
    return value


def _metadata_bool(metadata: Mapping[str, object], name: str) -> bool:
    value = metadata.get(name)
    if not isinstance(value, bool):
        raise CodeAssuranceEvidenceError(f"mutation_metadata_invalid:{name}")
    return value


def _metadata_selectors(metadata: Mapping[str, object]) -> tuple[str, ...]:
    raw = metadata.get("test_selectors")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise CodeAssuranceEvidenceError("mutation_metadata_invalid:test_selectors")
    selectors = tuple(raw)
    if any(not isinstance(item, str) or not item for item in selectors):
        raise CodeAssuranceEvidenceError("mutation_metadata_invalid:test_selectors")
    if not selectors or tuple(sorted(set(selectors))) != selectors:
        raise CodeAssuranceEvidenceError("mutation_metadata_invalid:test_selectors")
    return cast(tuple[str, ...], selectors)


def _mutation_metric_bundle(
    provider: ExternalProviderEvidence,
    target_symbol: str,
) -> tuple[dict[str, ExternalProviderMetric], Mapping[str, object]]:
    metrics = tuple(
        item
        for item in provider.metrics
        if item.category == "mutation_testing"
        and item.subject_kind == "symbol"
        and item.subject_key == target_symbol
    )
    if not metrics:
        return {}, {}
    by_name = {item.metric_name: item for item in metrics}
    if len(by_name) != len(metrics):
        raise CodeAssuranceEvidenceError("mutation_metric_name_duplicated")
    required = {
        "mutants_generated",
        "mutants_selected",
        "mutants_completed",
        "mutants_killed",
        "mutants_survived",
        "mutants_timed_out",
        "mutants_incompetent",
        "mutants_reused",
        "baseline_passed",
        "measurement_complete",
    }
    if not required.issubset(by_name):
        raise CodeAssuranceEvidenceError("mutation_required_metrics_missing")
    first = _metadata_mapping(metrics[0])
    contract = {
        "provider_schema": _metadata_text(first, "provider_schema"),
        "target_symbol": _metadata_text(first, "target_symbol"),
        "measurement_scope_signature": _metadata_text(first, "measurement_scope_signature"),
        "selection_truncated": _metadata_bool(first, "selection_truncated"),
        "measurement_complete": _metadata_bool(first, "measurement_complete"),
        "baseline_passed": _metadata_bool(first, "baseline_passed"),
        "test_selectors": _metadata_selectors(first),
    }
    for metric in metrics[1:]:
        metadata = _metadata_mapping(metric)
        observed = {
            "provider_schema": _metadata_text(metadata, "provider_schema"),
            "target_symbol": _metadata_text(metadata, "target_symbol"),
            "measurement_scope_signature": _metadata_text(metadata, "measurement_scope_signature"),
            "selection_truncated": _metadata_bool(metadata, "selection_truncated"),
            "measurement_complete": _metadata_bool(metadata, "measurement_complete"),
            "baseline_passed": _metadata_bool(metadata, "baseline_passed"),
            "test_selectors": _metadata_selectors(metadata),
        }
        if observed != contract:
            raise CodeAssuranceEvidenceError("mutation_metric_contracts_disagree")
    if (
        contract["provider_schema"] != COSMIC_RAY_MUTATION_PROVIDER_SCHEMA
        or contract["target_symbol"] != target_symbol
    ):
        raise CodeAssuranceEvidenceError("mutation_metric_contract_identity_mismatch")
    return by_name, contract


def _mutation_relations(
    provider: ExternalProviderEvidence,
    *,
    target_symbol: str,
    scope: str,
    selectors: tuple[str, ...],
) -> None:
    targets = tuple(
        item
        for item in provider.relations
        if item.relation_kind == "mutation_targets_symbol"
        and item.source_kind == "run"
        and item.source_key == scope
        and item.target_kind == "symbol"
        and item.target_key == target_symbol
    )
    if len(targets) != 1:
        raise CodeAssuranceEvidenceError("mutation_target_relation_missing_or_duplicated")
    tested_by = tuple(
        item
        for item in provider.relations
        if item.relation_kind == "mutation_tested_by"
        and item.source_kind == "symbol"
        and item.source_key == target_symbol
    )
    if not tested_by:
        raise CodeAssuranceEvidenceError("mutation_tested_by_relation_missing")
    observed_selectors: set[str] = set()
    for relation in (*targets, *tested_by):
        if not isinstance(relation.metadata, Mapping):
            raise CodeAssuranceEvidenceError("mutation_relation_metadata_not_mapping")
        metadata = relation.metadata
        if (
            _metadata_text(metadata, "measurement_scope_signature") != scope
            or _metadata_text(metadata, "provider_schema") != COSMIC_RAY_MUTATION_PROVIDER_SCHEMA
            or _metadata_text(metadata, "target_symbol") != target_symbol
        ):
            raise CodeAssuranceEvidenceError("mutation_relation_contract_mismatch")
        if relation.relation_kind == "mutation_tested_by":
            raw = metadata.get("selectors")
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise CodeAssuranceEvidenceError("mutation_relation_selectors_invalid")
            if any(not isinstance(item, str) or not item for item in raw):
                raise CodeAssuranceEvidenceError("mutation_relation_selectors_invalid")
            observed_selectors.update(cast(Sequence[str], raw))
    if tuple(sorted(observed_selectors)) != selectors:
        raise CodeAssuranceEvidenceError("mutation_relation_selectors_disagree")


def _mutation_evidence_refs(
    *,
    provider: ExternalProviderEvidence,
    subject_key: str,
    snapshot_id: str,
    target_symbol: str,
    metrics: Mapping[str, ExternalProviderMetric],
    counts: Mapping[str, int],
    scope: str,
    selectors: tuple[str, ...],
) -> tuple[AnalysisEvidenceRef, AnalysisEvidenceRef]:
    provider_run_id = _valid_run_id(provider.effective_tool_run_id)
    if provider_run_id is None:
        raise CodeAssuranceEvidenceError("mutation_provider_run_identity_invalid")
    experiment_names = (
        "mutants_selected",
        "mutants_completed",
        "mutants_killed",
        "mutants_timed_out",
        "mutants_incompetent",
        "baseline_passed",
        "measurement_complete",
    )
    experiment_ids = tuple(metrics[name].portable_metric_id for name in experiment_names)
    experiment_projection = {
        "metric_ids": experiment_ids,
        "target_symbol": target_symbol,
        "measurement_scope_signature": scope,
        "test_selectors": selectors,
        "selected": counts["selected"],
        "completed": counts["completed"],
        "killed": counts["killed"],
        "timed_out": counts["timed_out"],
        "incompetent": counts["incompetent"],
    }
    experiment_digest = analysis_identity(
        "code-assurance-mutation-experiment-projection-v1", experiment_projection
    )
    experiment = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-assurance-mutation-experiment-evidence-v1",
            {"subject_key": subject_key, "projection_digest": experiment_digest},
        ),
        subject_key=subject_key,
        role="experiment_result",
        evidence_kind="experiment_result",
        source_owner_id="code",
        producer_id=COSMIC_RAY_MUTATION_PROVIDER_ID,
        producer_version=COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        source_schema=COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        source_record_kind="external_mutation_metric_bundle",
        source_record_id=analysis_identity("mutation-metric-bundle-v1", experiment_ids),
        source_projection_digest=experiment_digest,
        snapshot_id=snapshot_id,
        revision_id=None,
        facts=(
            AnalysisFact("mutation_target_symbol", target_symbol),
            AnalysisFact("mutants_selected", counts["selected"], "count"),
            AnalysisFact("mutants_completed", counts["completed"], "count"),
            AnalysisFact("mutants_killed", counts["killed"], "count"),
            AnalysisFact("mutants_timed_out", counts["timed_out"], "count"),
            AnalysisFact("mutants_incompetent", counts["incompetent"], "count"),
            AnalysisFact(
                "test_selectors_json",
                canonical_json({"test_selectors": selectors}),
            ),
        ),
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="code.cosmic-ray-assurance-resolver",
        resolver_version="v1",
        limitations=(
            "mutation_evidence_is_bounded_to_one_declared_target_and_test_selection",
            "killed_mutants_do_not_prove_behavioral_correctness",
        ),
        provider_run_id=provider_run_id,
    )
    survived_metric = metrics["mutants_survived"]
    counter_projection = {
        "metric_id": survived_metric.portable_metric_id,
        "target_symbol": target_symbol,
        "measurement_scope_signature": scope,
        "survived": counts["survived"],
        "selection_complete": True,
    }
    counter_digest = analysis_identity(
        "code-assurance-mutation-counterevidence-projection-v1", counter_projection
    )
    counterevidence = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-assurance-mutation-counterevidence-v1",
            {"subject_key": subject_key, "projection_digest": counter_digest},
        ),
        subject_key=subject_key,
        role="counterevidence",
        evidence_kind="experiment_result",
        source_owner_id="code",
        producer_id=COSMIC_RAY_MUTATION_PROVIDER_ID,
        producer_version=COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        source_schema=COSMIC_RAY_MUTATION_PROVIDER_SCHEMA,
        source_record_kind="external_metric",
        source_record_id=survived_metric.portable_metric_id,
        source_projection_digest=counter_digest,
        snapshot_id=snapshot_id,
        revision_id=None,
        facts=(
            AnalysisFact("mutants_survived", counts["survived"], "count"),
            AnalysisFact("counterexample_search_completed", True),
        ),
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id="code.cosmic-ray-assurance-resolver",
        resolver_version="v1",
        limitations=("zero_survivors_means_counterevidence_was_sought_not_that_none_can_exist",),
        provider_run_id=provider_run_id,
    )
    return experiment, counterevidence


def _empty_mutation(status: MutationEvidenceStatus, reason: str) -> _MutationResolution:
    return _MutationResolution(
        MutationAssuranceObservation(
            status=status,
            reason=reason,
            target_symbol=None,
            test_selectors=(),
            generated=None,
            selected=None,
            completed=None,
            killed=None,
            survived=None,
            timed_out=None,
            incompetent=None,
            measurement_scope_signature=None,
            discriminating_result_observed=False,
            counterevidence_evaluated=False,
        )
    )


def _mutation_resolution(
    provider: ExternalProviderEvidence | None,
    scope_summary: CoverageScopeSummary,
    *,
    subject_key: str,
    snapshot_id: str,
) -> _MutationResolution:
    if provider is None:
        return _empty_mutation("not_recorded", "mutation_provider_not_recorded")
    if provider.provider_id != COSMIC_RAY_MUTATION_PROVIDER_ID:
        return _empty_mutation("incompatible", "mutation_provider_identity_mismatch")
    if provider.status != "ready" or _valid_run_id(provider.effective_tool_run_id) is None:
        return _empty_mutation(
            "abstained",
            provider.reason or "mutation_provider_not_ready",
        )
    target_symbol = scope_summary.qualified_name
    if target_symbol is None:
        return _empty_mutation("not_targeted", "coverage_symbol_has_no_qualified_name")
    try:
        metrics, contract = _mutation_metric_bundle(provider, target_symbol)
        if not metrics:
            return _MutationResolution(
                MutationAssuranceObservation(
                    status="not_targeted",
                    reason="mutation_provider_did_not_target_symbol",
                    target_symbol=target_symbol,
                    test_selectors=(),
                    generated=None,
                    selected=None,
                    completed=None,
                    killed=None,
                    survived=None,
                    timed_out=None,
                    incompetent=None,
                    measurement_scope_signature=None,
                    discriminating_result_observed=False,
                    counterevidence_evaluated=False,
                )
            )
        counts = {
            "generated": _metric_count(metrics["mutants_generated"], "count"),
            "selected": _metric_count(metrics["mutants_selected"], "count"),
            "completed": _metric_count(metrics["mutants_completed"], "count"),
            "killed": _metric_count(metrics["mutants_killed"], "count"),
            "survived": _metric_count(metrics["mutants_survived"], "count"),
            "timed_out": _metric_count(metrics["mutants_timed_out"], "count"),
            "incompetent": _metric_count(metrics["mutants_incompetent"], "count"),
            "reused": _metric_count(metrics["mutants_reused"], "count"),
        }
        baseline_passed = _metric_boolean(metrics["baseline_passed"])
        measurement_complete = _metric_boolean(metrics["measurement_complete"])
        if baseline_passed != contract["baseline_passed"] or (
            measurement_complete != contract["measurement_complete"]
        ):
            raise CodeAssuranceEvidenceError("mutation_metric_boolean_contract_mismatch")
        if not (
            counts["generated"] >= counts["selected"] >= counts["completed"]
            and counts["completed"]
            == counts["killed"] + counts["survived"] + counts["timed_out"] + counts["incompetent"]
            and counts["reused"] <= counts["completed"]
        ):
            raise CodeAssuranceEvidenceError("mutation_count_partition_invalid")
        if contract["selection_truncated"] != (counts["selected"] < counts["generated"]):
            raise CodeAssuranceEvidenceError("mutation_selection_truncation_mismatch")
        if measurement_complete != (counts["completed"] == counts["selected"]):
            raise CodeAssuranceEvidenceError("mutation_measurement_completeness_mismatch")
        denominator = counts["killed"] + counts["survived"]
        score = metrics.get("mutation_score")
        if denominator:
            if (
                score is None
                or score.unit != "ratio"
                or not math.isclose(
                    score.value,
                    counts["killed"] / denominator,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise CodeAssuranceEvidenceError("mutation_score_mismatch")
        elif score is not None:
            raise CodeAssuranceEvidenceError("mutation_score_without_outcomes")
        scope = cast(str, contract["measurement_scope_signature"])
        selectors = cast(tuple[str, ...], contract["test_selectors"])
        _mutation_relations(
            provider,
            target_symbol=target_symbol,
            scope=scope,
            selectors=selectors,
        )
        complete = bool(
            baseline_passed
            and measurement_complete
            and not contract["selection_truncated"]
            and counts["selected"] > 0
            and counts["timed_out"] == 0
            and counts["incompetent"] == 0
            and denominator == counts["selected"]
        )
        status: MutationEvidenceStatus = "complete" if complete else "incomplete"
        reason = (
            "complete_focal_mutation_result_observed"
            if complete
            else "mutation_result_incomplete_or_non_discriminating"
        )
        observation = MutationAssuranceObservation(
            status=status,
            reason=reason,
            target_symbol=target_symbol,
            test_selectors=selectors,
            generated=counts["generated"],
            selected=counts["selected"],
            completed=counts["completed"],
            killed=counts["killed"],
            survived=counts["survived"],
            timed_out=counts["timed_out"],
            incompetent=counts["incompetent"],
            measurement_scope_signature=scope,
            discriminating_result_observed=complete,
            counterevidence_evaluated=complete,
        )
        if not complete:
            return _MutationResolution(observation)
        experiment, counterevidence = _mutation_evidence_refs(
            provider=provider,
            subject_key=subject_key,
            snapshot_id=snapshot_id,
            target_symbol=target_symbol,
            metrics=metrics,
            counts=counts,
            scope=scope,
            selectors=selectors,
        )
        return _MutationResolution(observation, experiment, counterevidence)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc)
        if not reason or len(reason) > 200:
            reason = type(exc).__name__
        return _empty_mutation("incompatible", f"mutation_evidence_incompatible:{reason}")


def _subject_ref(
    scope: CoverageScopeSummary,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisSubjectRef:
    location = None
    if (
        scope.relative_path is not None
        and scope.start_line is not None
        and scope.end_line is not None
    ):
        location = AnalysisSourceLocation(
            scope.relative_path,
            scope.start_line,
            scope.end_line,
        )
    return AnalysisSubjectRef(
        subject_kind="symbol",
        subject_key=scope.subject_key,
        display_name=scope.qualified_name or scope.subject_key,
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=None,
        location=location,
    )


def _question_evaluation(
    coverage: CodeCoverageAnalysis,
    scope: CoverageScopeSummary,
    mutation: _MutationResolution,
    *,
    rank: int,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> AnalysisQuestionEvaluation:
    execution_status, execution_reason = _test_execution_status(coverage)
    evidence: list[AnalysisEvidenceRef] = []
    requirement_evaluations: list[AnalysisRequirementEvaluation] = []
    if execution_status == "observed":
        execution_evidence = _execution_evidence(
            coverage,
            subject_key=scope.subject_key,
            snapshot_id=snapshot_id,
        )
        evidence.append(execution_evidence)
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "declared_test_execution_observed",
                "satisfied",
                (execution_evidence.evidence_id,),
                "declared_test_run_completed_and_passed",
            )
        )
    else:
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "declared_test_execution_observed",
                "not_evaluated" if execution_status == "not_evaluated" else "missing",
                (),
                execution_reason,
            )
        )
    relations = _coverage_relations_for_symbol(coverage, scope.subject_key)
    relation_evidence: tuple[AnalysisEvidenceRef, ...] = ()
    if _valid_run_id(coverage.effective_tool_run_id) is not None:
        relation_evidence = tuple(
            _coverage_relation_evidence(
                coverage,
                relation,
                subject_key=scope.subject_key,
                snapshot_id=snapshot_id,
            )
            for relation in relations
        )
    if relation_evidence:
        evidence.extend(relation_evidence)
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "symbol_line_execution_context_observed",
                "satisfied",
                tuple(item.evidence_id for item in relation_evidence),
                "exact_test_contexts_observed_for_listed_symbol_lines",
            )
        )
    else:
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "symbol_line_execution_context_observed",
                "missing" if coverage.status == "ready" else "not_evaluated",
                (),
                "no_resolved_test_context_for_symbol_lines",
            )
        )
    requirement_evaluations.append(
        AnalysisRequirementEvaluation(
            "explicit_assertion_or_invariant_link_observed",
            "not_evaluated",
            (),
            "assertion_invariant_provider_not_registered",
        )
    )
    if mutation.experiment_evidence is not None:
        evidence.append(mutation.experiment_evidence)
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "discriminating_mutation_or_runtime_scenario_result",
                "satisfied",
                (mutation.experiment_evidence.evidence_id,),
                "complete_focal_mutation_result_observed",
            )
        )
    else:
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "discriminating_mutation_or_runtime_scenario_result",
                "missing" if mutation.observation.status != "abstained" else "not_evaluated",
                (),
                mutation.observation.reason,
            )
        )
    if mutation.counterevidence is not None:
        evidence.append(mutation.counterevidence)
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "negative_control_or_counterevidence_evaluated",
                "satisfied",
                (mutation.counterevidence.evidence_id,),
                "complete_mutation_survivor_search_observed",
            )
        )
    else:
        requirement_evaluations.append(
            AnalysisRequirementEvaluation(
                "negative_control_or_counterevidence_evaluated",
                "not_evaluated",
                (),
                "negative_control_and_counterevidence_not_evaluated",
            )
        )
    question_ready = execution_status == "observed" and bool(relation_evidence)
    evaluation_id = analysis_identity(
        "code-assurance-question-evaluation-v1",
        {
            "subject_key": scope.subject_key,
            "snapshot_id": snapshot_id,
            "question_spec": analysis_question_spec_fingerprint(ASSURANCE_QUESTION),
            "evidence_ids": tuple(item.evidence_id for item in evidence),
        },
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=evaluation_id,
        question_id=ASSURANCE_QUESTION.question_id,
        question_version=ASSURANCE_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(ASSURANCE_QUESTION),
        rank=rank,
        subject=_subject_ref(
            scope,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
        ),
        evidence=tuple(evidence),
        requirements=tuple(requirement_evaluations),
        observation_status="confirmed" if question_ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=ASSURANCE_QUESTION.hypotheses,
        question_readiness="ready" if question_ready else "abstained",
        decision_readiness="experiment_required" if question_ready else "abstained",
        decision=None,
        decision_reason=(
            "decision_evidence_incomplete" if question_ready else "question_evidence_incomplete"
        ),
        counterevidence_status=(
            "evaluated" if mutation.counterevidence is not None else "not_evaluated"
        ),
        next_action_ids=(
            tuple(item.action_id for item in ASSURANCE_QUESTION.next_actions)
            if question_ready
            else ()
        ),
        limitations=(
            "coverage_context_does_not_establish_assertion_or_invariant_protection",
            "assertion_invariant_and_runtime_scenario_providers_are_not_registered",
            "mutation_evidence_remains_a_bounded_experiment",
            "human_decision_not_owned_by_code_analysis",
        ),
    )
    return evaluation


def _observation(
    coverage: CodeCoverageAnalysis,
    scope: CoverageScopeSummary,
    mutation: _MutationResolution,
    evaluation: AnalysisQuestionEvaluation,
    *,
    rank: int,
) -> SymbolAssuranceObservation:
    execution_status, execution_reason = _test_execution_status(coverage)
    relations = _coverage_relations_for_symbol(coverage, scope.subject_key)
    executing_tests = tuple(
        sorted({nodeid for relation in relations for nodeid in relation.test_nodeids})
    )
    identity = analysis_identity(
        "code-assurance-observation-v1",
        {
            "subject_key": scope.subject_key,
            "qualified_name": scope.qualified_name,
            "relative_path": scope.relative_path,
            "start_line": scope.start_line,
            "end_line": scope.end_line,
            "coverage_scope": coverage.measurement_scope_signature,
            "execution_status": execution_status,
            "execution_reason": execution_reason,
            "coverage_totals": asdict(scope.totals),
            "coverage_relation_ids": tuple(item.relation_id for item in relations),
            "executing_tests": executing_tests,
            "mutation": asdict(mutation.observation),
        },
    )
    return SymbolAssuranceObservation(
        observation_id=identity,
        rank=rank,
        subject_key=scope.subject_key,
        display_name=scope.qualified_name or scope.subject_key,
        relative_path=scope.relative_path,
        start_line=scope.start_line,
        end_line=scope.end_line,
        test_execution_status=execution_status,
        test_execution_reason=execution_reason,
        coverage_observed=bool(executing_tests),
        executing_tests=executing_tests,
        covered_lines=scope.totals.covered_lines,
        executable_lines=scope.totals.executable_lines,
        line_coverage_percent=scope.totals.line_coverage_percent,
        covered_branch_exits=scope.totals.covered_branch_exits,
        branch_exits=scope.totals.branch_exits,
        branch_coverage_percent=scope.totals.branch_coverage_percent,
        assertion_invariant_evidence_status="not_recorded",
        mutation=mutation.observation,
        runtime_scenario_evidence_status="not_recorded",
        behavioral_assurance_status="not_established",
        question_evaluation_id=evaluation.evaluation_id,
    )


def _calibration(
    observations: tuple[SymbolAssuranceObservation, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
) -> CodeAssuranceCalibration:
    execution_coverage = sum(
        item.test_execution_status == "observed" and item.coverage_observed for item in observations
    )
    coverage_only = sum(
        item.test_execution_status == "observed"
        and item.coverage_observed
        and not item.mutation.discriminating_result_observed
        for item in observations
    )
    mutation_results = sum(item.mutation.discriminating_result_observed for item in observations)
    return CodeAssuranceCalibration(
        status="not_established",
        independent_outcome_labels=0,
        evaluated_subjects=len(observations),
        execution_and_coverage_observed=execution_coverage,
        coverage_only_subjects=coverage_only,
        explicit_assertion_invariant_links=0,
        discriminating_mutation_results=mutation_results,
        runtime_scenario_results=0,
        human_review_ready=sum(
            item.decision_readiness == "human_review_required" for item in evaluations
        ),
        behavioral_assurance_claims=0,
        precision_at_k=None,
        recall=None,
        finding_to_decision_rate=None,
    )


def _analysis_identity(
    *,
    snapshot_id: str,
    snapshot_freshness: str,
    status: AssuranceStatus,
    reason: str | None,
    coverage_measurement_scope_signature: str | None,
    eligible_symbols: int,
    observations: tuple[SymbolAssuranceObservation, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
    calibration: CodeAssuranceCalibration,
) -> str:
    return analysis_identity(
        "code-assurance-analysis-v1",
        {
            "snapshot_id": snapshot_id,
            "snapshot_freshness": snapshot_freshness,
            "status": status,
            "reason": reason,
            "policy": CODE_ASSURANCE_POLICY,
            "coverage_measurement_scope_signature": coverage_measurement_scope_signature,
            "eligible_symbols": eligible_symbols,
            "observation_ids": tuple(item.observation_id for item in observations),
            "evaluation_ids": tuple(item.evaluation_id for item in evaluations),
            "calibration": asdict(calibration),
        },
    )


def analyze_code_assurance(
    coverage: CodeCoverageAnalysis | None,
    providers: Mapping[str, ExternalProviderEvidence],
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    limit: int = 100,
) -> CodeAssuranceAnalysis:
    """Build a bounded assurance projection without executing or mutating code.

    Only normalized coverage contexts and the existing focal mutation provider
    are consumed.  Version 1 intentionally exposes no parameter through which
    callers could smuggle inferred ``ASSERTS`` or runtime-scenario claims.
    """

    _required_text("assurance snapshot", snapshot_id, maximum=2_048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("assurance snapshot freshness is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("assurance symbol limit must be within 1..1000")
    if not isinstance(providers, Mapping):
        raise TypeError("assurance providers must be a mapping")
    mutation_provider = providers.get(COSMIC_RAY_MUTATION_PROVIDER_ID)
    if coverage is None or coverage.status != "ready" or not coverage.symbols:
        reason = (
            "coverage_analysis_missing"
            if coverage is None
            else coverage.reason or "coverage_symbol_evidence_unavailable"
        )
        calibration = _calibration((), ())
        analysis_id = _analysis_identity(
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            status="abstained",
            reason=reason,
            coverage_measurement_scope_signature=(
                None if coverage is None else coverage.measurement_scope_signature
            ),
            eligible_symbols=0 if coverage is None else len(coverage.symbols),
            observations=(),
            evaluations=(),
            calibration=calibration,
        )
        return CodeAssuranceAnalysis(
            analysis_id=analysis_id,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            status="abstained",
            reason=reason,
            policy_id=CODE_ASSURANCE_POLICY,
            coverage_provider_id=CODE_COVERAGE_PROVIDER_ID,
            coverage_measurement_scope_signature=(
                None if coverage is None else coverage.measurement_scope_signature
            ),
            eligible_symbols=0 if coverage is None else len(coverage.symbols),
            returned_symbols=0,
            selection_truncated=bool(coverage is not None and coverage.symbols),
            observations=(),
            question_specs=(),
            question_evaluations=(),
            calibration=calibration,
        )
    scopes = tuple(sorted(coverage.symbols, key=lambda item: item.subject_key))[:limit]
    evaluations: list[AnalysisQuestionEvaluation] = []
    observations: list[SymbolAssuranceObservation] = []
    for rank, scope in enumerate(scopes, start=1):
        mutation = _mutation_resolution(
            mutation_provider,
            scope,
            subject_key=scope.subject_key,
            snapshot_id=snapshot_id,
        )
        evaluation = _question_evaluation(
            coverage,
            scope,
            mutation,
            rank=rank,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
        )
        evaluations.append(evaluation)
        observations.append(
            _observation(
                coverage,
                scope,
                mutation,
                evaluation,
                rank=rank,
            )
        )
    frozen_observations = tuple(observations)
    frozen_evaluations = tuple(evaluations)
    calibration = _calibration(frozen_observations, frozen_evaluations)
    analysis_id = _analysis_identity(
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        status="ready",
        reason=None,
        coverage_measurement_scope_signature=coverage.measurement_scope_signature,
        eligible_symbols=len(coverage.symbols),
        observations=frozen_observations,
        evaluations=frozen_evaluations,
        calibration=calibration,
    )
    return CodeAssuranceAnalysis(
        analysis_id=analysis_id,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        status="ready",
        reason=None,
        policy_id=CODE_ASSURANCE_POLICY,
        coverage_provider_id=CODE_COVERAGE_PROVIDER_ID,
        coverage_measurement_scope_signature=coverage.measurement_scope_signature,
        eligible_symbols=len(coverage.symbols),
        returned_symbols=len(frozen_observations),
        selection_truncated=len(frozen_observations) < len(coverage.symbols),
        observations=frozen_observations,
        question_specs=(ASSURANCE_QUESTION,),
        question_evaluations=frozen_evaluations,
        calibration=calibration,
    )


__all__ = [
    "ASSURANCE_QUESTION",
    "CODE_ASSURANCE_POLICY",
    "CODE_ASSURANCE_SCHEMA",
    "CodeAssuranceAnalysis",
    "CodeAssuranceCalibration",
    "CodeAssuranceEvidenceError",
    "MutationAssuranceObservation",
    "SymbolAssuranceObservation",
    "analyze_code_assurance",
]
