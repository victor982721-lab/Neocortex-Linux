"""Public immutable contracts for deterministic Code review."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    analysis_questions_payload,
    validate_analysis_question_set,
)
from .code_architecture_analysis import CodeArchitectureAnalysis
from .code_analyzer_effectiveness import CodeAnalyzerEffectivenessAnalysis
from .code_analyzer_calibration import CodeAnalyzerCalibrationAnalysis
from .code_assurance_analysis import CodeAssuranceAnalysis
from .code_capability_reachability_analysis import CodeCapabilityReachabilityAnalysis
from .code_change_evolution_analysis import CodeChangeEvolutionAnalysis
from .code_class_surface_analysis import CodeClassSurfaceAnalysis
from .code_coverage_analysis import (
    CODE_COVERAGE_SCHEMA,
    CodeCoverageAnalysis,
    CoverageScopeSummary,
    TestToSymbolRelation,
    WorkPackageCoverageProjection,
)
from .code_engineering_analytics import (
    CODE_ENGINEERING_ANALYTICS_SCHEMA,
    CodeEngineeringAnalytics,
    EngineeringDimension,
    EngineeringGate,
    ModuleEngineeringProfile,
)
from .code_external_evidence import ExternalEvidenceStatus
from .code_interface_surface_analysis import CodeInterfaceSurfaceAnalysis
from .code_experiment_planner import CodeExperimentPlan, plan_code_experiments
from .code_experiment_store import (
    CODE_EXPERIMENT_STORE_MAX_RESOLVED,
    ResolvedCodeExperimentReceipt,
    apply_code_experiment_receipts,
)
from .code_invariant_assurance_analysis import CodeInvariantAssuranceAnalysis
from .code_route_capability_analysis import CodeRouteCapabilityAnalysis
from .code_retention_analysis import CodeRetentionAnalysis
from .code_state_interaction_analysis import CodeStateInteractionAnalysis
from .code_supply_chain_analysis import (
    CodeSupplyChainAnalysis,
    SupplyChainGateEvaluation,
    SupplyChainObservation,
)
from .code_unused_analysis import CodeUnusedAnalysis, UnusedConsensusCandidate
from .code_review_actionability import (
    Actionability,
    ChangeRisk,
    CodeReviewEpistemicState,
    Construction,
    SourceRole,
)
from .code_review_serialization import (
    CODE_REVIEW_COMPATIBLE_SCHEMAS,
    CODE_REVIEW_SCHEMA,
    CodeReviewDigest,
    rebuild_code_review_result_digest,
)
from .code_state_projection_analysis import CodeStateProjectionAnalysis
from .code_state_topology_analysis import CodeStateTopologyAnalysis
from .code_technical_verification import (
    CodeTechnicalVerification,
    build_code_technical_verification,
)
from .external_evidence_models import ExternalEvidenceSuiteStatus
from .semantic_models import canonical_json, fingerprint_text

# v19 adds a reproducible, read-only four-owner Retention projection and an
# allow-listed negative-control experiment.  Passed receipts remain linked to
# exact evidence requirements; the projection never authorizes deletion.
# v18 linked immutable, passed experiment receipts back into the
# exact evidence requirements they measured.  Receipts remain advisory and a
# separate allow-listed verifier can publish a scoped no-change technical
# disposition without impersonating a human actor or authorizing mutation.
# Earlier contracts cannot satisfy the expanded wire, so no compatibility is
# claimed without an explicit adapter.
CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT = 20
CODE_REVIEW_ENGINEERING_EXAMPLE_LIMIT = 20
CODE_REVIEW_UNUSED_EXAMPLE_LIMIT = 20
CODE_REVIEW_PUBLIC_MATERIALIZATION_MAX = 50

_UNUSED_CHARACTERIZATION_REQUIREMENTS = (
    "verify_import_reexport_callback_registry_protocol_and_entry_point_usage",
    "run_targeted_tests_and_public_import_smoke_without_mutating_code",
    "record_explicit_human_confirmation_or_reclassify_with_new_evidence",
    "require_comparable_unused_analysis_replay_before_any_separate_change",
)
_UNUSED_CHARACTERIZATION_OBJECTIVE = "characterize_high_consensus_unused_candidate_without_mutation"
_UNUSED_CHARACTERIZATION_ACCEPTANCE_GATES = (
    "unused_analysis_comparable",
    "candidate_remains_probable_unused_high_consensus",
    "dynamic_usage_ruled_out_by_human_review",
    "human_confirmation_recorded",
    "tests_passed",
    "public_import_surface_preserved",
    "architecture_contracts_not_degraded",
    "no_new_import_cycles",
    "unused_coverage_status_honest",
)
_UNUSED_CHARACTERIZATION_CONTRACTS = (
    "public_import_and_reexport_surface",
    "callbacks_registries_protocols_and_entry_points",
    "runtime_and_test_fixture_behavior",
)
_UNUSED_CHARACTERIZATION_VALIDATION = (
    "inspect_import_reexport_and___all___usage",
    "inspect_callbacks_registries_protocols_and_entry_points",
    "run_targeted_tests_and_public_import_smoke",
    "record_human_confirmation_before_any_separate_change",
)
_UNUSED_REQUIRED_LIMITATIONS = frozenset(
    {
        "characterization_package_is_advice_not_change_authorization",
        "candidate_requires_explicit_human_confirmation",
        "dynamic_usage_may_remain_unobserved",
        "coverage_can_explain_usage_but_never_strengthens_missing_evidence",
        "package_has_zero_delete_or_mutation_authority",
    }
)

ReviewStatus = Literal["ready", "abstained"]
ReviewFreshness = Literal["current", "publication_only"]
RecommendationStatus = Literal["ready", "abstained", "not_evaluated"]
WorkPackageConfidence = Literal["unused_high_consensus_advisory"]
WorkPackagePhase = Literal["characterize"]
WorkPackageKind = Literal["unused_characterization"]
FindingCategory = Literal[
    "complex_and_long_hotspot",
    "high_complexity_hotspot",
    "long_function_hotspot",
]


@dataclass(frozen=True, slots=True)
class CodeReviewDiagnostic:
    """One exact analyzer diagnostic supporting a symbol hotspot."""

    diagnostic_id: int
    code: Literal["high_complexity", "long_function"]
    value: int
    threshold: int | None
    source: str
    tool_name: str
    tool_version: str
    confirmed: bool
    confidence: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.diagnostic_id, bool)
            or not isinstance(self.diagnostic_id, int)
            or self.diagnostic_id < 1
        ):
            raise ValueError("code-review diagnostic identity is invalid")
        if self.code not in {"high_complexity", "long_function"}:
            raise ValueError("code-review diagnostic code is unsupported")
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("code-review diagnostic value is invalid")
        if self.threshold is None or (
            isinstance(self.threshold, bool)
            or not isinstance(self.threshold, int)
            or self.threshold < 1
        ):
            raise ValueError("code-review diagnostic threshold is invalid")
        if self.value < self.threshold:
            raise ValueError("code-review diagnostic does not meet its threshold")
        if not self.source or not self.tool_name or not self.tool_version:
            raise ValueError("code-review diagnostic provenance is incomplete")
        if not self.confirmed:
            raise ValueError("code-review finding requires a confirmed diagnostic")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("code-review diagnostic confidence is invalid")


@dataclass(frozen=True, slots=True)
class CodeReviewCaller:
    """One bounded, resolved static call site used as impact evidence."""

    path: str
    symbol: str | None
    start_line: int
    end_line: int
    confidence: float
    provenance: str
    path_convention_role: SourceRole


@dataclass(frozen=True, slots=True)
class CodeReviewImpact:
    """Static caller evidence split only by explicit path conventions."""

    call_sites: int
    path_convention_production_callers: int
    path_convention_test_callers: int
    path_convention_fixture_callers: int
    path_convention_tool_callers: int
    path_convention_compatibility_callers: int
    resolved_static_consumer_files: int
    path_convention_production_consumer_files: int
    path_convention_test_consumer_files: int
    consumer_file_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeReviewFinding:
    """One symbol-level observation and its explicit epistemic boundary."""

    finding_id: str
    hotspot_id: str
    rank: int
    category: FindingCategory
    path: str
    symbol: str
    symbol_kind: str
    signature: str | None
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    complexity: int
    function_lines: int
    complexity_ratio_basis_points: int
    length_ratio_basis_points: int
    score_basis_points: int
    incoming_references: int
    incoming_calls: int
    resolved_static_callers: int
    impact: CodeReviewImpact
    path_convention_role: SourceRole
    construction: Construction
    actionability: Actionability
    change_risk: ChangeRisk
    recommended_change: bool
    epistemic_state: CodeReviewEpistemicState
    actionability_evidence: tuple[str, ...]
    contracts_to_preserve: tuple[str, ...]
    recommended_validation: tuple[str, ...]
    analyzer_id: str
    analyzer_version: str
    file_xxh3_128: str | None
    file_xxh3_64_guard: str | None
    diagnostics: tuple[CodeReviewDiagnostic, ...]
    callers: tuple[CodeReviewCaller, ...]
    reasons: tuple[str, ...]
    observation_confidence: Literal["confirmed_static_evidence"] = "confirmed_static_evidence"

    def __post_init__(self) -> None:
        if self.observation_confidence != "confirmed_static_evidence":
            raise ValueError("invalid structural observation-confidence scope")
        if self.recommended_change or self.actionability == "act_now":
            raise ValueError(
                "code-review/v15 structural findings cannot authorize a change recommendation"
            )
        expected_actionability = (
            "characterize_first"
            if self.epistemic_state.decision_readiness == "experiment_required"
            else "insufficient_evidence"
        )
        if self.actionability != expected_actionability:
            raise ValueError("finding actionability must project decision readiness exactly")
        if self.construction != "unknown" or self.change_risk != "unknown":
            raise ValueError("structural finding cannot infer construction or change risk")
        if self.epistemic_state.inference_status != "abstained":
            raise ValueError("structural-hotspot v1 has no semantic inference resolver")
        if (
            self.epistemic_state.observation_status != "confirmed"
            or self.epistemic_state.question_readiness != "ready"
            or self.epistemic_state.decision_readiness != "experiment_required"
        ):
            raise ValueError(
                "published structural finding requires confirmed observation and an experiment"
            )
        if self.contracts_to_preserve or self.recommended_validation:
            raise ValueError("structural finding cannot claim unobserved contracts or validation")
        if not self.diagnostics:
            raise ValueError("confirmed structural finding requires diagnostic evidence")
        if len({diagnostic.diagnostic_id for diagnostic in self.diagnostics}) != len(
            self.diagnostics
        ):
            raise ValueError("structural finding diagnostic identities are duplicated")
        expected_codes: set[str] = set()
        if self.category in {"complex_and_long_hotspot", "high_complexity_hotspot"}:
            expected_codes.add("high_complexity")
        if self.category in {"complex_and_long_hotspot", "long_function_hotspot"}:
            expected_codes.add("long_function")
        diagnostic_codes = {diagnostic.code for diagnostic in self.diagnostics}
        if diagnostic_codes != expected_codes or len(diagnostic_codes) != len(self.diagnostics):
            raise ValueError("structural finding category disagrees with diagnostic evidence")
        for diagnostic in self.diagnostics:
            expected_value = (
                self.complexity if diagnostic.code == "high_complexity" else self.function_lines
            )
            if diagnostic.threshold is None:
                raise ValueError("structural finding diagnostic threshold is missing")
            expected_ratio = (10_000 * diagnostic.value) // diagnostic.threshold
            published_ratio = (
                self.complexity_ratio_basis_points
                if diagnostic.code == "high_complexity"
                else self.length_ratio_basis_points
            )
            if diagnostic.value != expected_value or expected_ratio != published_ratio:
                raise ValueError("structural finding metric disagrees with diagnostic evidence")
        expected_observations: list[str] = []
        if self.complexity_ratio_basis_points >= 10_000:
            expected_observations.append(
                "cyclomatic_complexity_threshold_met_or_exceeded:"
                f"{self.complexity_ratio_basis_points}bp"
            )
        if self.length_ratio_basis_points >= 10_000:
            expected_observations.append(
                f"function_length_threshold_met_or_exceeded:{self.length_ratio_basis_points}bp"
            )
        expected_observations.extend(
            (
                "path_convention_production_callers:"
                f"{self.impact.path_convention_production_callers}",
                "path_convention_test_or_fixture_callers:"
                f"{self.impact.path_convention_test_callers + self.impact.path_convention_fixture_callers}",
                f"resolved_static_consumer_files:{self.impact.resolved_static_consumer_files}",
            )
        )
        if self.epistemic_state.observations != tuple(expected_observations):
            raise ValueError("structural observations must be derived from published evidence")
        expected_evidence = (
            f"source_role_path_convention:{self.path_convention_role}",
            "semantic_construction:abstained:not_observed",
            f"question_readiness:{self.epistemic_state.question_readiness}",
            f"decision_readiness:{self.epistemic_state.decision_readiness}",
            *self.epistemic_state.observations,
        )
        if self.actionability_evidence not in {
            expected_evidence,
            (*expected_evidence, "outgoing_calls:truncated_not_interpreted"),
        }:
            raise ValueError("structural actionability evidence is not reproducible")
        expected_reasons = (
            *(
                (f"confirmed_cyclomatic_complexity:{self.complexity}",)
                if "high_complexity" in expected_codes
                else ()
            ),
            *(
                (f"confirmed_function_lines:{self.function_lines}",)
                if "long_function" in expected_codes
                else ()
            ),
            f"resolved_static_callers:{self.resolved_static_callers}",
        )
        if self.reasons != expected_reasons:
            raise ValueError("structural finding reasons are not reproducible")
        role_callers = (
            self.impact.path_convention_production_callers
            + self.impact.path_convention_test_callers
            + self.impact.path_convention_fixture_callers
            + self.impact.path_convention_tool_callers
            + self.impact.path_convention_compatibility_callers
        )
        if self.resolved_static_callers != role_callers:
            raise ValueError("structural caller totals disagree with path-convention observations")
        if self.incoming_calls != self.impact.call_sites:
            raise ValueError("structural call-site total disagrees with impact evidence")
        if self.incoming_references < self.incoming_calls:
            raise ValueError("structural incoming-reference totals are inconsistent")


@dataclass(frozen=True, slots=True)
class CodeReviewRecommendation:
    """Legacy wire shape; v11 has no public construction authority."""

    recommendation_rank: int
    finding_id: str
    hotspot_id: str
    hotspot_rank: int
    path: str
    symbol: str
    construction: Construction
    change_risk: ChangeRisk
    production_callers: int
    test_callers: int
    evidence: tuple[str, ...]
    contracts_to_preserve: tuple[str, ...]
    recommended_validation: tuple[str, ...]

    def __post_init__(self) -> None:
        raise ValueError("code-review/v15 cannot construct semantic change recommendations")


@dataclass(frozen=True, slots=True)
class CodeReviewWorkPackageStep:
    """One ordered, non-mutating planning step with an explicit decision gate."""

    order: int
    phase: WorkPackagePhase
    target: str
    requirement: str


@dataclass(frozen=True, slots=True)
class CodeReviewWorkPackage:
    """A deterministic maintenance unit assembled from published evidence."""

    package_rank: int
    package_id: str
    title: str
    objective: str
    primary_finding_id: str
    primary_hotspot_id: str
    primary_symbol: str
    primary_module: str | None
    change_risk: ChangeRisk
    members: tuple[()]
    members_truncated: bool
    consumer_module_examples: tuple[str, ...]
    import_chains: tuple[tuple[str, ...], ...]
    affected_architecture_contracts: tuple[str, ...]
    test_coverage: WorkPackageCoverageProjection | None
    test_coverage_scope: CoverageScopeSummary | None
    contracts_to_preserve: tuple[str, ...]
    steps: tuple[CodeReviewWorkPackageStep, ...]
    recommended_validation: tuple[str, ...]
    acceptance_gates: tuple[str, ...]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    confidence: WorkPackageConfidence
    package_kind: WorkPackageKind = "unused_characterization"
    unused_candidates: tuple[UnusedConsensusCandidate, ...] = ()
    supply_chain_observations: tuple[SupplyChainObservation, ...] = ()
    supply_chain_relations: tuple[SupplyChainObservation, ...] = ()
    supply_chain_gates: tuple[SupplyChainGateEvaluation, ...] = ()
    engineering_profile: ModuleEngineeringProfile | None = None
    engineering_gates: tuple[EngineeringGate, ...] = ()
    requires_human_confirmation: bool = False
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        if self.mutation_authority:
            raise ValueError("code review work package cannot authorize mutation")
        if self.package_kind != "unused_characterization":
            raise ValueError("code-review/v15 only supports unused-code characterization packages")
        if not self.steps or any(step.phase != "characterize" for step in self.steps):
            raise ValueError("unused-code package may contain characterization steps only")
        if len(self.unused_candidates) != 1 or any(
            candidate.state != "probable_unused_high_consensus"
            for candidate in self.unused_candidates
        ):
            raise ValueError("unused-code package requires one calibrated high-consensus candidate")
        candidate = self.unused_candidates[0]
        expected_target = candidate.symbol or candidate.name
        if (
            self.primary_finding_id != candidate.candidate_id
            or self.primary_hotspot_id != candidate.candidate_id
            or self.primary_symbol != expected_target
            or self.primary_module != candidate.module_id
        ):
            raise ValueError("unused-code package primary identity must match its candidate")
        if self.title != f"{expected_target} unused-code characterization":
            raise ValueError("unused-code characterization title must be canonical")
        expected_package_payload = canonical_json(
            {
                "planning": "unused-characterization-work-packages-v1",
                "candidate_id": candidate.candidate_id,
            }
        )
        expected_package_id = (
            "code-unused-work-package-v1:xxh3_128:"
            + fingerprint_text(expected_package_payload).xxh3_128
        )
        if self.package_id != expected_package_id:
            raise ValueError("unused-code characterization package identity must be canonical")
        if self.objective != _UNUSED_CHARACTERIZATION_OBJECTIVE:
            raise ValueError("unused-code characterization objective must be canonical")
        if tuple(step.order for step in self.steps) != (1, 2, 3, 4):
            raise ValueError("unused-code characterization step order must be canonical")
        if (
            any(step.target != expected_target for step in self.steps)
            or tuple(step.requirement for step in self.steps)
            != _UNUSED_CHARACTERIZATION_REQUIREMENTS
        ):
            raise ValueError("unused-code characterization steps must be canonical")
        allowed_acceptance_gates = {
            *_UNUSED_CHARACTERIZATION_ACCEPTANCE_GATES,
            "semgrep_invariants",
            "dependency_declaration_integrity",
            "vulnerability_snapshot_current",
            "no_known_vulnerabilities",
            "installed_package_integrity",
            "license_inventory_available",
        }
        if self.acceptance_gates[
            : len(_UNUSED_CHARACTERIZATION_ACCEPTANCE_GATES)
        ] != _UNUSED_CHARACTERIZATION_ACCEPTANCE_GATES or not set(self.acceptance_gates).issubset(
            allowed_acceptance_gates
        ):
            raise ValueError("unused-code characterization gates must be canonical")
        if self.contracts_to_preserve != _UNUSED_CHARACTERIZATION_CONTRACTS:
            raise ValueError("unused-code characterization contracts must be canonical")
        if self.recommended_validation != _UNUSED_CHARACTERIZATION_VALIDATION:
            raise ValueError("unused-code characterization validation must be canonical")
        if not self.requires_human_confirmation:
            raise ValueError("unused-code package requires explicit human confirmation")
        if self.members:
            raise ValueError("unused-code package cannot contain change targets")
        if any(
            candidate.authority != "advisory"
            or candidate.mutation_authority
            or not candidate.provider_ids
            or not candidate.evidence
            for candidate in self.unused_candidates
        ):
            raise ValueError("unused-code package candidate evidence must remain advisory")
        if self.change_risk != "unknown":
            raise ValueError("unused-code characterization cannot infer change risk")
        if self.confidence != "unused_high_consensus_advisory":
            raise ValueError("invalid unused-code characterization confidence")
        required_evidence = {
            f"unused_candidate:{candidate.candidate_id}:{candidate.state}",
            *(f"provider:{provider_id}" for provider_id in candidate.provider_ids),
        }
        if not required_evidence.issubset(self.evidence):
            raise ValueError("unused-code package lacks canonical candidate evidence")
        allowed_evidence_prefixes = (
            "unused_candidate:",
            "provider:",
            "reason:",
            "calibration_signature:",
            "coverage_status:",
            "architecture:",
            "engineering:",
            "engineering_profile:",
            "engineering_dimension:",
            "engineering_gate:",
            "supply_chain:",
            "supply_chain_observations:",
            "supply_chain_relations:",
        )
        if any(not item.startswith(allowed_evidence_prefixes) for item in self.evidence):
            raise ValueError("unused-code package evidence vocabulary is not canonical")
        if not _UNUSED_REQUIRED_LIMITATIONS.issubset(self.limitations):
            raise ValueError("unused-code package lacks mandatory authority limitations")
        allowed_limitation_prefixes = (
            "characterization_package_",
            "candidate_requires_",
            "dynamic_usage_",
            "coverage_",
            "package_has_",
            "architecture_",
            "engineering_",
            "supply_chain_",
            "static_absence_",
            "probable_unused_",
            "unused_analysis_",
            "no_aggregate_",
            "no_dimension_",
            "mutation_findings_",
        )
        if any(not item.startswith(allowed_limitation_prefixes) for item in self.limitations):
            raise ValueError("unused-code package limitation vocabulary is not canonical")


@dataclass(frozen=True, slots=True)
class CodeReviewSnapshot:
    """Published self-analysis snapshot that authorized this review."""

    analysis_run_id: int
    framework_run_id: int
    scan_id: int
    processing_signature: str
    root: str
    freshness: ReviewFreshness
    current: bool
    journal_status: str


@dataclass(frozen=True, slots=True)
class CodeReviewCoverage:
    """Bounded coverage and deliberately suppressed evidence."""

    current_python_files: int
    complete_python_files: int
    incomplete_python_files: int
    candidate_hotspots: int
    enumerated_hotspots: int
    probable_dead_suppressed: int
    call_edges: int
    resolved_call_edges: int


@dataclass(frozen=True, slots=True)
class CodeReviewResult:
    """One JSON-ready review result with a deterministic content digest."""

    database: str
    status: ReviewStatus
    reason: str | None
    ranking: str
    actionability_version: str
    recommendation_status: RecommendationStatus
    recommendation_reason: str | None
    planning_version: str
    work_package_status: RecommendationStatus
    work_package_reason: str | None
    snapshot: CodeReviewSnapshot | None
    coverage: CodeReviewCoverage | None
    findings: tuple[CodeReviewFinding, ...]
    recommendations: tuple[CodeReviewRecommendation, ...]
    work_packages: tuple[CodeReviewWorkPackage, ...]
    external_evidence: ExternalEvidenceStatus | None
    external_evidence_suite: ExternalEvidenceSuiteStatus | None
    architecture: CodeArchitectureAnalysis | None
    test_coverage: CodeCoverageAnalysis | None
    limitations: tuple[str, ...]
    digest: CodeReviewDigest | None
    unused_analysis: CodeUnusedAnalysis | None = None
    supply_chain: CodeSupplyChainAnalysis | None = None
    engineering_analytics: CodeEngineeringAnalytics | None = None
    structural_analysis: CodeClassSurfaceAnalysis | None = None
    state_projection: CodeStateProjectionAnalysis | None = None
    state_topology: CodeStateTopologyAnalysis | None = None
    retention_analysis: CodeRetentionAnalysis | None = None
    state_interactions: CodeStateInteractionAnalysis | None = None
    change_evolution: CodeChangeEvolutionAnalysis | None = None
    assurance: CodeAssuranceAnalysis | None = None
    invariant_assurance: CodeInvariantAssuranceAnalysis | None = None
    capability_reachability: CodeCapabilityReachabilityAnalysis | None = None
    route_capabilities: CodeRouteCapabilityAnalysis | None = None
    analyzer_effectiveness: CodeAnalyzerEffectivenessAnalysis | None = None
    analyzer_calibration: CodeAnalyzerCalibrationAnalysis | None = None
    experiment_plan: CodeExperimentPlan | None = None
    interface_surface: CodeInterfaceSurfaceAnalysis | None = None
    question_specs: tuple[AnalysisQuestionSpec, ...] = ()
    question_evaluations: tuple[AnalysisQuestionEvaluation, ...] = ()
    experiment_receipts: tuple[ResolvedCodeExperimentReceipt, ...] = ()
    technical_verification: CodeTechnicalVerification | None = None
    # Presentation-only bound.  The digest continues to bind the complete
    # evidence model while public example projections expose totals/truncation.
    materialization_limit: int = CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT

    def __post_init__(self) -> None:
        if (
            isinstance(self.materialization_limit, bool)
            or not isinstance(self.materialization_limit, int)
            or not 1 <= self.materialization_limit <= CODE_REVIEW_PUBLIC_MATERIALIZATION_MAX
        ):
            raise ValueError("code-review materialization limit must be between 1 and 50")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("invalid code-review result status")
        if self.recommendation_status not in {"ready", "abstained", "not_evaluated"}:
            raise ValueError("invalid code-review recommendation status")
        if self.work_package_status not in {"ready", "abstained", "not_evaluated"}:
            raise ValueError("invalid code-review work-package status")
        if self.recommendations:
            raise ValueError("code-review/v19 cannot publish semantic change recommendations")
        if self.recommendation_status == "ready":
            raise ValueError("code-review/v19 recommendation status must abstain")
        if self.recommendation_status == "abstained" and not self.recommendation_reason:
            raise ValueError("abstained recommendation status requires a reason")
        if self.recommendation_status == "not_evaluated" and not self.recommendation_reason:
            raise ValueError("not-evaluated recommendation status requires a reason")
        if any(package.package_kind != "unused_characterization" for package in self.work_packages):
            raise ValueError("code-review/v19 cannot publish hotspot change packages")
        if (self.work_package_status == "ready") != bool(self.work_packages):
            raise ValueError("work-package readiness must match published packages")
        if self.work_package_status == "ready" and self.work_package_reason is not None:
            raise ValueError("ready work-package status cannot carry an abstention reason")
        if self.work_package_status != "ready" and not self.work_package_reason:
            raise ValueError("non-ready work-package status requires a reason")
        if self.status == "abstained":
            if not self.reason:
                raise ValueError("abstained code-review result requires a reason")
            if (
                any(
                    item is not None
                    for item in (
                        self.snapshot,
                        self.coverage,
                        self.external_evidence,
                        self.external_evidence_suite,
                        self.architecture,
                        self.test_coverage,
                        self.unused_analysis,
                        self.supply_chain,
                        self.engineering_analytics,
                        self.state_topology,
                        self.retention_analysis,
                        self.state_interactions,
                        self.change_evolution,
                        self.assurance,
                        self.invariant_assurance,
                        self.capability_reachability,
                        self.route_capabilities,
                        self.analyzer_effectiveness,
                        self.analyzer_calibration,
                        self.experiment_plan,
                        self.interface_surface,
                        self.digest,
                    )
                )
                or self.findings
                or self.work_packages
                or self.structural_analysis is not None
                or self.state_projection is not None
                or self.question_specs
                or self.question_evaluations
                or self.experiment_receipts
                or self.technical_verification is not None
            ):
                raise ValueError("abstained code-review result cannot publish unverified evidence")
            return
        if self.reason is not None:
            raise ValueError("ready code-review result cannot carry an abstention reason")
        if self.digest is None:
            raise ValueError("ready code-review result requires an evidence digest")
        if self.snapshot is None or self.coverage is None:
            raise ValueError("ready code-review result lacks evidence required by its digest")
        if self.structural_analysis is None:
            raise ValueError("ready code-review result requires resolved structural analysis")
        if self.architecture is None:
            raise ValueError("ready code-review result requires resolved architecture analysis")
        if self.state_projection is None:
            raise ValueError("ready code-review result requires a state projection result")
        if self.state_topology is None:
            raise ValueError("ready code-review result requires state topology evidence")
        if self.retention_analysis is None:
            raise ValueError("ready code-review result requires retention evidence")
        if self.state_interactions is None:
            raise ValueError("ready code-review result requires state interaction evidence")
        if self.change_evolution is None:
            raise ValueError("ready code-review result requires change evolution evidence")
        if self.assurance is None:
            raise ValueError("ready code-review result requires assurance evidence")
        if self.invariant_assurance is None:
            raise ValueError("ready code-review result requires invariant assurance evidence")
        if self.capability_reachability is None:
            raise ValueError("ready code-review result requires capability reachability evidence")
        if self.route_capabilities is None:
            raise ValueError("ready code-review result requires route capability evidence")
        if self.analyzer_effectiveness is None:
            raise ValueError("ready code-review result requires analyzer effectiveness evidence")
        if self.analyzer_calibration is None:
            raise ValueError("ready code-review result requires analyzer calibration evidence")
        if self.experiment_plan is None:
            raise ValueError("ready code-review result requires an experiment plan")
        if self.interface_surface is None:
            raise ValueError("ready code-review result requires interface surface evidence")
        if self.technical_verification is None:
            raise ValueError("ready code-review result requires technical verification")
        if self.supply_chain is None:
            raise ValueError("ready code-review result requires supply-chain evidence")
        if (
            not isinstance(self.experiment_receipts, tuple)
            or len(self.experiment_receipts) > CODE_EXPERIMENT_STORE_MAX_RESOLVED
            or any(
                not isinstance(item, ResolvedCodeExperimentReceipt)
                for item in self.experiment_receipts
            )
        ):
            raise ValueError("code-review experiment receipts are invalid or out of bounds")
        if any(
            item.analysis_run_id > self.snapshot.analysis_run_id
            or item.receipt.source_version != self.snapshot.processing_signature
            for item in self.experiment_receipts
        ):
            raise ValueError("code-review experiment receipts disagree with their snapshot")
        if (
            self.structural_analysis.snapshot_id != self.snapshot.processing_signature
            or self.structural_analysis.snapshot_freshness != self.snapshot.freshness
        ):
            raise ValueError("code-review structural evidence disagrees with its snapshot")
        if (
            self.state_topology.source_version != CODE_REVIEW_SCHEMA
            or self.retention_analysis.source_version != CODE_REVIEW_SCHEMA
            or self.capability_reachability.source_version != CODE_REVIEW_SCHEMA
            or self.route_capabilities.source_version != CODE_REVIEW_SCHEMA
            or self.analyzer_calibration.source_version != CODE_REVIEW_SCHEMA
        ):
            raise ValueError("code-review integrated projection version is inconsistent")
        if (
            self.assurance.snapshot_id != self.snapshot.processing_signature
            or self.assurance.snapshot_freshness != self.snapshot.freshness
        ):
            raise ValueError("code-review assurance evidence disagrees with its snapshot")
        if self.analyzer_effectiveness.status == "ready" and (
            self.analyzer_effectiveness.analysis_run_id != self.snapshot.analysis_run_id
            or self.analyzer_effectiveness.framework_run_id != self.snapshot.framework_run_id
            or self.analyzer_effectiveness.processing_signature
            != self.snapshot.processing_signature
            or self.analyzer_effectiveness.snapshot_freshness != self.snapshot.freshness
            or self.analyzer_effectiveness.source_version != CODE_REVIEW_SCHEMA
        ):
            raise ValueError("code-review analyzer effectiveness disagrees with its snapshot")
        if self.state_interactions.status != "abstained" and (
            self.state_interactions.analysis_run_id != self.snapshot.analysis_run_id
            or self.state_interactions.source_processing_signature
            != self.snapshot.processing_signature
        ):
            raise ValueError("code-review state interactions disagree with its snapshot")
        if (
            self.invariant_assurance.snapshot_id != self.snapshot.processing_signature
            or self.invariant_assurance.snapshot_freshness != self.snapshot.freshness
        ):
            raise ValueError("code-review invariant assurance disagrees with its snapshot")
        if self.supply_chain.analysis_run_id != self.snapshot.analysis_run_id:
            raise ValueError("code-review supply-chain evidence disagrees with its snapshot")
        if self.interface_surface.status == "ready" and (
            self.interface_surface.analysis_run_id != self.snapshot.analysis_run_id
            or self.interface_surface.processing_signature != self.snapshot.processing_signature
        ):
            raise ValueError("code-review interface surface disagrees with its snapshot")
        surface = self.change_evolution.change_surface
        if surface.status == "ready" and (
            surface.current_analysis_run_id != self.snapshot.analysis_run_id
            or surface.processing_signature != self.snapshot.processing_signature
        ):
            raise ValueError("code-review change surface disagrees with its snapshot")
        if rebuild_code_review_result_digest(self) != self.digest:
            raise ValueError("code-review result digest disagrees with published evidence")
        validate_analysis_question_set(self.question_specs, self.question_evaluations)
        from .code_review_epistemics import expected_integrated_code_review_questions

        expected_specs, expected_evaluations = expected_integrated_code_review_questions(
            self.findings,
            self.snapshot,
            self.structural_analysis,
            state_projection=self.state_projection,
            state_topology=self.state_topology,
            retention_analysis=self.retention_analysis,
            change_evolution=self.change_evolution,
            architecture=self.architecture,
            assurance=self.assurance,
            supply_chain=self.supply_chain,
            interface_surface=self.interface_surface,
            capability_reachability=self.capability_reachability,
            analyzer_effectiveness=self.analyzer_effectiveness,
            state_interactions=self.state_interactions,
            invariant_assurance=self.invariant_assurance,
            route_capabilities=self.route_capabilities,
            analyzer_calibration=self.analyzer_calibration,
        )
        if self.question_specs != expected_specs:
            raise ValueError("code-review question specs are not reproducible from evidence")
        base_plan = plan_code_experiments(expected_specs, expected_evaluations)
        expected_evaluations = apply_code_experiment_receipts(
            expected_specs,
            expected_evaluations,
            base_plan,
            self.experiment_receipts,
        )
        if self.question_evaluations != expected_evaluations:
            raise ValueError("code-review questions are not reproducible from published evidence")
        if self.experiment_plan != plan_code_experiments(
            self.question_specs, self.question_evaluations
        ):
            raise ValueError("code-review experiment plan is not reproducible from questions")
        if self.technical_verification != build_code_technical_verification(
            self.question_specs,
            self.question_evaluations,
            self.experiment_receipts,
        ):
            raise ValueError("code-review technical verification is not reproducible")

    def as_payload(self) -> dict[str, object]:
        if self.status == "ready":
            if self.digest is None or rebuild_code_review_result_digest(self) != self.digest:
                raise ValueError("code-review result changed after digest verification")
        payload: dict[str, object] = {
            "database": self.database,
            "status": self.status,
            "reason": self.reason,
            "ranking": self.ranking,
            "actionability_version": self.actionability_version,
            "recommendation_status": self.recommendation_status,
            "recommendation_reason": self.recommendation_reason,
            "planning_version": self.planning_version,
            "work_package_status": self.work_package_status,
            "work_package_reason": self.work_package_reason,
            "snapshot": None if self.snapshot is None else asdict(self.snapshot),
            "coverage": None if self.coverage is None else asdict(self.coverage),
            "findings": [asdict(item) for item in self.findings],
            "recommendations": [],
            "work_packages": [
                bounded_code_review_work_package_payload(
                    item,
                    limit=self.materialization_limit,
                )
                for item in self.work_packages
            ],
            "external_evidence": (
                None if self.external_evidence is None else self.external_evidence.as_payload()
            ),
            "external_evidence_suite": (
                None
                if self.external_evidence_suite is None
                else self.external_evidence_suite.as_payload()
            ),
            "architecture": None if self.architecture is None else self.architecture.as_payload(),
            "test_coverage": (
                None
                if self.test_coverage is None
                else bounded_code_coverage_payload(
                    self.test_coverage,
                    limit=self.materialization_limit,
                )
            ),
            "limitations": list(self.limitations),
            "digest": None if self.digest is None else asdict(self.digest),
            "unused_analysis": (
                None
                if self.unused_analysis is None
                else bounded_code_unused_payload(
                    self.unused_analysis,
                    limit=self.materialization_limit,
                )
            ),
            "supply_chain": (None if self.supply_chain is None else self.supply_chain.as_payload()),
            "engineering_analytics": (
                None
                if self.engineering_analytics is None
                else bounded_code_engineering_payload(
                    self.engineering_analytics,
                    limit=self.materialization_limit,
                )
            ),
            "structural_analysis": (
                None if self.structural_analysis is None else self.structural_analysis.as_payload()
            ),
            "state_projection": (
                None if self.state_projection is None else self.state_projection.as_payload()
            ),
            "state_topology": (
                None if self.state_topology is None else self.state_topology.as_payload()
            ),
            "retention_analysis": (
                None
                if self.retention_analysis is None
                else self.retention_analysis.as_payload()
            ),
            "state_interactions": (
                None if self.state_interactions is None else self.state_interactions.as_payload()
            ),
            "change_evolution": (
                None if self.change_evolution is None else self.change_evolution.as_payload()
            ),
            "assurance": None if self.assurance is None else self.assurance.as_payload(),
            "invariant_assurance": (
                None if self.invariant_assurance is None else self.invariant_assurance.as_payload()
            ),
            "capability_reachability": (
                None
                if self.capability_reachability is None
                else self.capability_reachability.as_payload()
            ),
            "route_capabilities": (
                None if self.route_capabilities is None else self.route_capabilities.as_payload()
            ),
            "analyzer_effectiveness": (
                None
                if self.analyzer_effectiveness is None
                else self.analyzer_effectiveness.as_payload()
            ),
            "analyzer_calibration": (
                None
                if self.analyzer_calibration is None
                else self.analyzer_calibration.as_payload()
            ),
            "experiment_plan": (
                None if self.experiment_plan is None else self.experiment_plan.as_payload()
            ),
            "experiment_receipts": [item.as_payload() for item in self.experiment_receipts],
            "technical_verification": (
                None
                if self.technical_verification is None
                else self.technical_verification.as_payload()
            ),
            "interface_surface": (
                None if self.interface_surface is None else self.interface_surface.as_payload()
            ),
            "epistemics": analysis_questions_payload(
                self.question_specs,
                self.question_evaluations,
            ),
        }
        return {
            "kind": "code-review",
            "schema": CODE_REVIEW_SCHEMA,
            "compatible_schemas": list(CODE_REVIEW_COMPATIBLE_SCHEMAS),
            **payload,
        }


def _public_materialization_limit(limit: int) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= CODE_REVIEW_PUBLIC_MATERIALIZATION_MAX
    ):
        raise ValueError("public Code review limit must be between 1 and 50")
    return limit


def _bounded_scope_payload(
    scope: CoverageScopeSummary,
    *,
    limit: int = CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    return {
        "subject_kind": scope.subject_kind,
        "subject_key": scope.subject_key,
        "module_key": scope.module_key,
        "symbol_key": scope.symbol_key,
        "qualified_name": scope.qualified_name,
        "start_line": scope.start_line,
        "end_line": scope.end_line,
        "relative_path": scope.relative_path,
        "totals": asdict(scope.totals),
        "missing_line_ranges": [list(item) for item in scope.missing_line_ranges[:limit]],
        "missing_line_ranges_total": len(scope.missing_line_ranges),
        "missing_line_ranges_truncated": (
            scope.missing_line_ranges_truncated or len(scope.missing_line_ranges) > limit
        ),
        "missing_branch_arcs": [list(item) for item in scope.missing_branch_arcs[:limit]],
        "missing_branch_arcs_total": len(scope.missing_branch_arcs),
        "missing_branch_arcs_truncated": (
            scope.missing_branch_arcs_truncated or len(scope.missing_branch_arcs) > limit
        ),
        "executing_tests": list(scope.executing_tests[:limit]),
        "executing_tests_total": len(scope.executing_tests),
        "executing_tests_truncated": len(scope.executing_tests) > limit,
    }


def _bounded_unused_candidate_payload(
    candidate: UnusedConsensusCandidate,
    *,
    limit: int = CODE_REVIEW_UNUSED_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    payload = asdict(candidate)
    for field_name in ("provider_ids", "reasons", "evidence", "limitations"):
        values = getattr(candidate, field_name)
        payload[field_name] = list(values[:limit])
        payload[f"{field_name}_total"] = len(values)
        payload[f"{field_name}_truncated"] = len(values) > limit
    signals = payload.get("signals")
    if isinstance(signals, dict):
        evidence_ids = candidate.signals.evidence_ids
        signals["evidence_ids"] = list(evidence_ids[:limit])
        signals["evidence_ids_total"] = len(evidence_ids)
        signals["evidence_ids_truncated"] = len(evidence_ids) > limit
    return payload


def bounded_code_unused_payload(
    analysis: CodeUnusedAnalysis,
    *,
    limit: int = CODE_REVIEW_UNUSED_EXAMPLE_LIMIT,
) -> dict[str, object]:
    """Project small public examples while the digest retains every candidate."""

    limit = _public_materialization_limit(limit)
    payload = analysis.as_payload()
    payload["candidates"] = [
        _bounded_unused_candidate_payload(candidate, limit=limit)
        for candidate in analysis.candidates[:limit]
    ]
    payload["candidates_total"] = len(analysis.candidates)
    payload["candidates_truncated"] = len(analysis.candidates) > limit
    counts = dict(analysis.counts)
    counts["total"] = len(analysis.candidates)
    payload["counts"] = dict(sorted(counts.items()))
    payload["limitations"] = list(analysis.limitations[:limit])
    payload["limitations_total"] = len(analysis.limitations)
    payload["limitations_truncated"] = len(analysis.limitations) > limit
    return payload


def _bounded_relation_payload(
    relation: TestToSymbolRelation,
    *,
    limit: int = CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    return {
        "relation_id": relation.relation_id,
        "test_key": relation.test_key,
        "production_symbol": relation.production_symbol,
        "test_nodeids": list(relation.test_nodeids[:limit]),
        "test_nodeids_total": len(relation.test_nodeids),
        "test_nodeids_truncated": len(relation.test_nodeids) > limit,
        "lines": list(relation.lines[:limit]),
        "lines_total": len(relation.lines),
        "lines_truncated": len(relation.lines) > limit,
        "contexts": list(relation.contexts[:limit]),
        "contexts_total": len(relation.contexts),
        "contexts_truncated": len(relation.contexts) > limit,
        "relative_path": relation.relative_path,
        "module_key": relation.module_key,
        "symbol_key": relation.symbol_key,
    }


def _missing_scope_examples(
    scopes: tuple[CoverageScopeSummary, ...],
) -> tuple[CoverageScopeSummary, ...]:
    return tuple(
        item
        for item in scopes
        if item.totals.missing_lines > 0 or item.totals.missing_branch_exits > 0
    )


def bounded_code_coverage_payload(
    analysis: CodeCoverageAnalysis,
    *,
    limit: int = CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    missing_modules = _missing_scope_examples(analysis.modules)
    missing_symbols = _missing_scope_examples(analysis.symbols)
    return {
        "kind": "code-coverage-analysis",
        "schema": CODE_COVERAGE_SCHEMA,
        "database": analysis.database,
        "analysis_run_id": analysis.analysis_run_id,
        "provider_id": analysis.provider_id,
        "tool_run_id": analysis.tool_run_id,
        "effective_tool_run_id": analysis.effective_tool_run_id,
        "status": analysis.status,
        "reason": analysis.reason,
        "suite_selection": analysis.suite_selection,
        "measurement_complete": analysis.measurement_complete,
        "content_executed": analysis.content_executed,
        "tool_versions": [asdict(item) for item in analysis.tool_versions],
        "suite_signature": analysis.suite_signature,
        "configuration_signature": analysis.configuration_signature,
        "measurement_scope_signature": analysis.measurement_scope_signature,
        "outcomes": None if analysis.outcomes is None else asdict(analysis.outcomes),
        "totals": None if analysis.totals is None else asdict(analysis.totals),
        "counts": {
            "modules": len(analysis.modules),
            "symbols": len(analysis.symbols),
            "test_relations": len(analysis.test_relations),
            "failed_tests": len(analysis.failed_test_nodeids),
            "modules_with_missing": len(missing_modules),
            "symbols_with_missing": len(missing_symbols),
        },
        "failed_test_examples": list(analysis.failed_test_nodeids[:limit]),
        "failed_test_examples_truncated": len(analysis.failed_test_nodeids) > limit,
        "module_missing_examples": [
            _bounded_scope_payload(item, limit=limit) for item in missing_modules[:limit]
        ],
        "module_missing_examples_truncated": len(missing_modules) > limit,
        "symbol_missing_examples": [
            _bounded_scope_payload(item, limit=limit) for item in missing_symbols[:limit]
        ],
        "symbol_missing_examples_truncated": len(missing_symbols) > limit,
        "test_relation_examples": [
            _bounded_relation_payload(item, limit=limit) for item in analysis.test_relations[:limit]
        ],
        "test_relation_examples_truncated": len(analysis.test_relations) > limit,
        "gates": [asdict(item) for item in analysis.gates],
        "limitations": list(analysis.limitations),
    }


def _bounded_engineering_dimension_payload(
    dimension: EngineeringDimension,
    *,
    limit: int = CODE_REVIEW_ENGINEERING_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    return {
        "dimension": dimension.dimension,
        "status": dimension.status,
        "reason": dimension.reason,
        "metrics": [asdict(item) for item in dimension.metrics[:limit]],
        "metrics_total": len(dimension.metrics),
        "metrics_truncated": len(dimension.metrics) > limit,
        "provenance": list(dimension.provenance[:limit]),
        "provenance_total": len(dimension.provenance),
        "provenance_truncated": len(dimension.provenance) > limit,
        "limitations": list(dimension.limitations[:limit]),
        "limitations_total": len(dimension.limitations),
        "limitations_truncated": len(dimension.limitations) > limit,
    }


def _bounded_engineering_profile_payload(
    profile: ModuleEngineeringProfile,
    *,
    limit: int = CODE_REVIEW_ENGINEERING_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    return {
        "module_id": profile.module_id,
        "path_namespace_id": profile.path_namespace_id,
        "complexity": _bounded_engineering_dimension_payload(profile.complexity, limit=limit),
        "coverage": _bounded_engineering_dimension_payload(profile.coverage, limit=limit),
        "mutation": _bounded_engineering_dimension_payload(profile.mutation, limit=limit),
        "history": _bounded_engineering_dimension_payload(profile.history, limit=limit),
        "graph": _bounded_engineering_dimension_payload(profile.graph, limit=limit),
    }


def bounded_code_engineering_payload(
    analysis: CodeEngineeringAnalytics,
    *,
    limit: int = CODE_REVIEW_ENGINEERING_EXAMPLE_LIMIT,
) -> dict[str, object]:
    """Project bounded module examples without changing analytics authority."""

    limit = _public_materialization_limit(limit)
    return {
        "kind": "code-engineering-analytics",
        "schema": CODE_ENGINEERING_ANALYTICS_SCHEMA,
        "database": analysis.database,
        "analysis_run_id": analysis.analysis_run_id,
        "status": analysis.status,
        "reason": analysis.reason,
        "providers": [asdict(item) for item in analysis.providers[:limit]],
        "providers_total": len(analysis.providers),
        "providers_truncated": len(analysis.providers) > limit,
        "modules": [
            _bounded_engineering_profile_payload(item, limit=limit)
            for item in analysis.modules[:limit]
        ],
        "modules_total": len(analysis.modules),
        "modules_truncated": len(analysis.modules) > limit,
        "gates": [asdict(item) for item in analysis.gates[:limit]],
        "gates_total": len(analysis.gates),
        "gates_truncated": len(analysis.gates) > limit,
        "mutation_scope_signature": analysis.mutation_scope_signature,
        "mutation_score": analysis.mutation_score,
        "limitations": list(analysis.limitations[:limit]),
        "limitations_total": len(analysis.limitations),
        "limitations_truncated": len(analysis.limitations) > limit,
        "digest": analysis.digest,
        "authority": analysis.authority,
        "mutation_authority": analysis.mutation_authority,
        "aggregate_score": analysis.aggregate_score,
        "defect_probability": analysis.defect_probability,
    }


def bounded_code_review_work_package_payload(
    package: CodeReviewWorkPackage,
    *,
    limit: int = CODE_REVIEW_COVERAGE_EXAMPLE_LIMIT,
) -> dict[str, object]:
    limit = _public_materialization_limit(limit)
    payload = asdict(
        replace(
            package,
            test_coverage=None,
            test_coverage_scope=None,
            unused_candidates=package.unused_candidates[:limit],
            engineering_profile=None,
            engineering_gates=(),
        )
    )
    projection = package.test_coverage
    if projection is not None:
        payload["test_coverage"] = {
            "primary_symbol": projection.primary_symbol,
            "status": projection.status,
            "executing_tests": list(projection.executing_tests[:limit]),
            "executing_tests_total": len(projection.executing_tests),
            "executing_tests_truncated": len(projection.executing_tests) > limit,
            "relation_ids": list(projection.relation_ids[:limit]),
            "relation_ids_total": len(projection.relation_ids),
            "relation_ids_truncated": len(projection.relation_ids) > limit,
            "gate": asdict(projection.gate),
        }
    if package.test_coverage_scope is not None:
        payload["test_coverage_scope"] = _bounded_scope_payload(
            package.test_coverage_scope,
            limit=limit,
        )
    payload["unused_candidates"] = [
        _bounded_unused_candidate_payload(item, limit=limit)
        for item in package.unused_candidates[:limit]
    ]
    payload["unused_candidates_total"] = len(package.unused_candidates)
    payload["unused_candidates_truncated"] = len(package.unused_candidates) > limit
    if package.engineering_profile is not None:
        payload["engineering_profile"] = _bounded_engineering_profile_payload(
            package.engineering_profile,
            limit=limit,
        )
    payload["engineering_gates"] = [asdict(item) for item in package.engineering_gates[:limit]]
    payload["engineering_gates_total"] = len(package.engineering_gates)
    payload["engineering_gates_truncated"] = len(package.engineering_gates) > limit
    return payload


def build_code_review_recommendations(
    findings: tuple[CodeReviewFinding, ...],
    *,
    limit: int,
) -> tuple[CodeReviewRecommendation, ...]:
    """Abstain until an independently resolvable decision-evidence model exists.

    ``code-review/v15`` observes structural hotspots but deliberately exposes no
    factory for a semantic change decision.  Keeping the fail-closed boundary
    here prevents a legacy flag or a manually assembled object from reviving
    the former name-based recommendation path.
    """

    del findings, limit
    return ()


__all__ = [
    "CODE_REVIEW_COMPATIBLE_SCHEMAS",
    "CODE_REVIEW_SCHEMA",
    "CodeReviewCaller",
    "CodeReviewCoverage",
    "CodeReviewDiagnostic",
    "CodeReviewDigest",
    "CodeReviewFinding",
    "CodeReviewImpact",
    "CodeReviewRecommendation",
    "CodeReviewResult",
    "CodeReviewSnapshot",
    "CodeReviewWorkPackage",
    "CodeReviewWorkPackageStep",
    "FindingCategory",
    "RecommendationStatus",
    "ReviewFreshness",
    "WorkPackageConfidence",
    "WorkPackageKind",
    "WorkPackagePhase",
    "bounded_code_coverage_payload",
    "bounded_code_engineering_payload",
    "bounded_code_review_work_package_payload",
    "bounded_code_unused_payload",
    "build_code_review_recommendations",
]
