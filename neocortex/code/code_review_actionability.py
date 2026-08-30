"""Epistemic decision contract over published Code hotspot observations.

The internal analyzer can prove that a symbol crossed a structural threshold and
can expose bounded static-call observations.  Those facts do not prove that the
structure is accidental, harmful or safe to change.  This module deliberately
keeps that boundary explicit: hotspot evidence opens a question and an
experiment, never a change recommendation by itself.
"""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from dataclasses import dataclass
from typing import Literal


CODE_REVIEW_ACTIONABILITY = "python-maintenance-epistemic-gate-v2"
CODE_REVIEW_QUESTION_ID = "maintenance.structural_hotspot_requires_change"
CODE_REVIEW_QUESTION_VERSION = "v1"

PathConventionRole = Literal["production", "test", "fixture", "tool", "compatibility"]
# Internal compatibility alias.  Public findings name this for what it is: a
# path convention, not runtime/product ownership evidence.
SourceRole = PathConventionRole
Construction = Literal[
    "algorithm",
    "builder",
    "classifier",
    "initializer",
    "lifecycle",
    "orchestrator",
    "persistence",
    "retrieval",
    "rule",
    "unknown",
    "validator",
]
Actionability = Literal[
    "act_now",
    "characterize_first",
    "intentional_complexity",
    "defer",
    "insufficient_evidence",
]
ChangeRisk = Literal["low", "medium", "high", "unknown"]
ObservationStatus = Literal["confirmed", "abstained"]
InferenceStatus = Literal["supported", "abstained"]
QuestionReadiness = Literal["ready", "abstained"]
DecisionReadiness = Literal["ready", "experiment_required", "abstained"]
DecisionOutcome = Literal["recommend_change", "defer"]


_CHANGE_DECISION_REQUIRED_EVIDENCE = (
    "confirmed_structural_hotspot",
    "behavior_or_contract_problem_observed",
    "counterevidence_evaluated",
    "discriminating_experiment_result",
)
_CHANGE_DECISION_MISSING_EVIDENCE = (
    "behavior_or_contract_problem_observed",
    "counterevidence_evaluated",
    "discriminating_experiment_result",
)
_COUNTEREVIDENCE_TO_SEEK = (
    "intentional_or_cohesive_structure",
    "existing_assurance_for_observed_behavior",
    "change_cost_or_risk_exceeds_verified_benefit",
)
_STRUCTURAL_HYPOTHESES = (
    "structural_hotspot_may_increase_maintenance_cost",
    "structure_may_be_intentional_or_cohesive",
)
_NEXT_ACTIONS = (
    "characterize_exact_behavior_and_contracts",
    "seek_counterevidence",
    "design_lowest_cost_discriminating_experiment",
)
_NO_SIGNAL_NEXT_ACTIONS = ("collect_confirmed_structural_evidence",)


@dataclass(frozen=True, slots=True)
class CodeReviewEpistemicState:
    """Typed boundary between observations, hypotheses and a decision.

    ``decision=None`` is intentional.  A structural threshold alone supplies no
    evidence-backed decision about changing behavior or architecture.
    """

    question_id: str
    question_version: str
    observation_status: ObservationStatus
    observations: tuple[str, ...]
    inference_status: InferenceStatus
    inferences: tuple[str, ...]
    hypotheses: tuple[str, ...]
    question_readiness: QuestionReadiness
    question_reason: str | None
    required_evidence: tuple[str, ...]
    satisfied_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    counterevidence_status: Literal["evaluated", "not_evaluated"]
    counterevidence_to_seek: tuple[str, ...]
    decision_readiness: DecisionReadiness
    decision: DecisionOutcome | None
    decision_reason: str
    next_actions: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        if self.observation_status not in {"confirmed", "abstained"}:
            raise ValueError("invalid structural observation status")
        if self.inference_status not in {"supported", "abstained"}:
            raise ValueError("invalid structural inference status")
        if self.question_readiness not in {"ready", "abstained"}:
            raise ValueError("invalid structural question readiness")
        if self.counterevidence_status not in {"evaluated", "not_evaluated"}:
            raise ValueError("invalid structural counterevidence status")
        if self.decision_readiness not in {"ready", "experiment_required", "abstained"}:
            raise ValueError("invalid structural decision readiness")
        if self.decision not in {None, "recommend_change", "defer"}:
            raise ValueError("invalid structural decision outcome")
        if not self.question_id or not self.question_version:
            raise ValueError("code review question identity cannot be empty")
        if self.authority != "advisory":
            raise ValueError("code review epistemic authority must remain advisory")
        if self.mutation_authority:
            raise ValueError("code review epistemic state cannot authorize mutation")
        if not self.decision_reason:
            raise ValueError("decision readiness requires an explicit reason")
        if self.observation_status == "confirmed" and not self.observations:
            raise ValueError("confirmed observation requires exact evidence")
        if self.observation_status == "abstained" and self.observations:
            raise ValueError("abstained observation cannot claim observed facts")
        if self.inference_status == "supported" and not self.inferences:
            raise ValueError("supported inference requires evidence-backed content")
        if self.inference_status == "abstained" and self.inferences:
            raise ValueError("abstained inference cannot claim inferred facts")
        if self.question_readiness == "ready" and self.question_reason is not None:
            raise ValueError("ready question cannot carry an abstention reason")
        if self.question_readiness == "abstained" and not self.question_reason:
            raise ValueError("abstained question requires a reason")
        if self.inference_status != "abstained":
            raise ValueError("structural-hotspot v1 has no semantic inference resolver")
        if self.decision_readiness == "ready" or self.decision is not None:
            raise ValueError(
                "structural-hotspot v1 has no evidence resolver and cannot issue decisions"
            )
        required = set(self.required_evidence)
        satisfied = set(self.satisfied_evidence)
        missing = set(self.missing_evidence)
        if self.question_readiness == "ready" and not self.hypotheses:
            raise ValueError("ready structural question requires competing hypotheses")
        if self.question_readiness == "ready" and not required:
            raise ValueError("ready structural question requires an evidence contract")
        if len(required) != len(self.required_evidence):
            raise ValueError("required evidence cannot contain duplicates")
        if len(satisfied) != len(self.satisfied_evidence):
            raise ValueError("satisfied evidence cannot contain duplicates")
        if len(missing) != len(self.missing_evidence):
            raise ValueError("missing evidence cannot contain duplicates")
        if satisfied.intersection(missing):
            raise ValueError("evidence cannot be both satisfied and missing")
        if satisfied.union(missing) != required:
            raise ValueError("required evidence must be partitioned into satisfied and missing")
        if self.decision_readiness == "experiment_required":
            if self.question_readiness != "ready":
                raise ValueError("experiment requires a ready question")
            if self.next_actions != _NEXT_ACTIONS:
                raise ValueError("structural-hotspot v1 next actions must be canonical")
        elif self.question_readiness == "abstained" and self.next_actions != (
            _NO_SIGNAL_NEXT_ACTIONS
        ):
            raise ValueError("abstained structural question next actions must be canonical")
        if self.question_id != CODE_REVIEW_QUESTION_ID or self.question_version != (
            CODE_REVIEW_QUESTION_VERSION
        ):
            raise ValueError("structural-hotspot v1 question identity must be canonical")
        if self.observation_status == "confirmed":
            expected = (
                self.question_readiness == "ready",
                self.question_reason is None,
                self.hypotheses == _STRUCTURAL_HYPOTHESES,
                self.required_evidence == _CHANGE_DECISION_REQUIRED_EVIDENCE,
                self.satisfied_evidence == ("confirmed_structural_hotspot",),
                self.missing_evidence == _CHANGE_DECISION_MISSING_EVIDENCE,
                self.counterevidence_status == "not_evaluated",
                self.counterevidence_to_seek == _COUNTEREVIDENCE_TO_SEEK,
                self.decision_readiness == "experiment_required",
                self.decision_reason == "structural_observation_alone_cannot_justify_change",
            )
            if not all(expected):
                raise ValueError("confirmed structural-hotspot state must be canonical")
        else:
            expected = (
                self.question_readiness == "abstained",
                self.question_reason == "confirmed_structural_hotspot_missing",
                not self.hypotheses,
                self.required_evidence == _CHANGE_DECISION_REQUIRED_EVIDENCE,
                not self.satisfied_evidence,
                self.missing_evidence == _CHANGE_DECISION_REQUIRED_EVIDENCE,
                self.counterevidence_status == "not_evaluated",
                not self.counterevidence_to_seek,
                self.decision_readiness == "abstained",
                self.decision_reason == "question_not_ready",
            )
            if not all(expected):
                raise ValueError("abstained structural-hotspot state must be canonical")


@dataclass(frozen=True, slots=True)
class CodeReviewActionabilityInput:
    """Bounded deterministic evidence available for one published hotspot."""

    path: str
    symbol: str
    root: str | None
    complexity_ratio_basis_points: int
    length_ratio_basis_points: int
    path_convention_production_callers: int
    path_convention_test_callers: int
    path_convention_fixture_callers: int
    path_convention_tool_callers: int
    path_convention_compatibility_callers: int
    resolved_static_consumer_files: int
    outgoing_calls: tuple[str, ...] = ()
    outgoing_calls_truncated: bool = False

    def __post_init__(self) -> None:
        if not self.path or not self.symbol:
            raise ValueError("structural evidence requires path and symbol identity")
        counts = {
            "complexity_ratio_basis_points": self.complexity_ratio_basis_points,
            "length_ratio_basis_points": self.length_ratio_basis_points,
            "path_convention_production_callers": self.path_convention_production_callers,
            "path_convention_test_callers": self.path_convention_test_callers,
            "path_convention_fixture_callers": self.path_convention_fixture_callers,
            "path_convention_tool_callers": self.path_convention_tool_callers,
            "path_convention_compatibility_callers": self.path_convention_compatibility_callers,
            "resolved_static_consumer_files": self.resolved_static_consumer_files,
        }
        for label, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CodeReviewActionabilityAssessment:
    """Advisory interpretation with no implicit semantic or mutation authority."""

    path_convention_role: PathConventionRole
    construction: Construction
    actionability: Actionability
    change_risk: ChangeRisk
    recommended_change: bool
    epistemic_state: CodeReviewEpistemicState
    evidence: tuple[str, ...]
    contracts_to_preserve: tuple[str, ...]
    recommended_validation: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.path_convention_role not in {
            "production",
            "test",
            "fixture",
            "tool",
            "compatibility",
        }:
            raise ValueError("invalid source role")
        expected_actionability = (
            "characterize_first"
            if self.epistemic_state.decision_readiness == "experiment_required"
            else "insufficient_evidence"
        )
        if self.construction != "unknown" or self.change_risk != "unknown":
            raise ValueError("structural evidence cannot infer construction or change risk")
        if self.epistemic_state.inference_status != "abstained":
            raise ValueError("structural evidence cannot claim semantic inference")
        if self.actionability != expected_actionability:
            raise ValueError("actionability must project epistemic decision readiness exactly")
        if self.recommended_change:
            raise ValueError("structural evidence cannot recommend semantic change")
        if self.contracts_to_preserve or self.recommended_validation:
            raise ValueError("unobserved contracts or validation cannot be claimed")


_FIXTURE_PARTS = frozenset({"fixture", "fixtures", "testdata", "test_data"})
_TEST_PARTS = frozenset({"test", "tests", "testing"})
_TOOL_PARTS = frozenset({"benchmark", "benchmarks", "dev", "script", "scripts", "tools"})
_COMPATIBILITY_PARTS = frozenset({"compat", "compatibility", "legacy"})


def _normalized_parts(path: str) -> tuple[str, ...]:
    return tuple(
        part.casefold() for part in path.replace("\\", "/").split("/") if part and part not in {"."}
    )


def classify_source_role(path: str, root: str | None = None) -> SourceRole:
    """Classify a path by an explicit portable convention.

    This is descriptive routing metadata.  It is not evidence of runtime
    reachability, product ownership or whether a change should be made.
    """

    parts = _normalized_parts(path)
    root_parts = _normalized_parts(root) if root else ()
    if root_parts and parts[: len(root_parts)] == root_parts:
        parts = parts[len(root_parts) :]
    filename = parts[-1] if parts else ""
    if _FIXTURE_PARTS.intersection(parts):
        return "fixture"
    if (
        _TEST_PARTS.intersection(parts)
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    ):
        return "test"
    if _TOOL_PARTS.intersection(parts):
        return "tool"
    if _COMPATIBILITY_PARTS.intersection(parts) or "_compat" in filename:
        return "compatibility"
    return "production"


def _confirmed_observations(evidence: CodeReviewActionabilityInput) -> tuple[str, ...]:
    observations: list[str] = []
    if evidence.complexity_ratio_basis_points >= 10_000:
        observations.append(
            "cyclomatic_complexity_threshold_met_or_exceeded:"
            f"{evidence.complexity_ratio_basis_points}bp"
        )
    if evidence.length_ratio_basis_points >= 10_000:
        observations.append(
            f"function_length_threshold_met_or_exceeded:{evidence.length_ratio_basis_points}bp"
        )
    observations.extend(
        (
            f"path_convention_production_callers:{evidence.path_convention_production_callers}",
            "path_convention_test_or_fixture_callers:"
            f"{evidence.path_convention_test_callers + evidence.path_convention_fixture_callers}",
            f"resolved_static_consumer_files:{evidence.resolved_static_consumer_files}",
        )
    )
    return tuple(observations)


def _epistemic_state(evidence: CodeReviewActionabilityInput) -> CodeReviewEpistemicState:
    confirmed_signal = (
        evidence.complexity_ratio_basis_points >= 10_000
        or evidence.length_ratio_basis_points >= 10_000
    )
    if not confirmed_signal:
        return CodeReviewEpistemicState(
            question_id=CODE_REVIEW_QUESTION_ID,
            question_version=CODE_REVIEW_QUESTION_VERSION,
            observation_status="abstained",
            observations=(),
            inference_status="abstained",
            inferences=(),
            hypotheses=(),
            question_readiness="abstained",
            question_reason="confirmed_structural_hotspot_missing",
            required_evidence=_CHANGE_DECISION_REQUIRED_EVIDENCE,
            satisfied_evidence=(),
            missing_evidence=_CHANGE_DECISION_REQUIRED_EVIDENCE,
            counterevidence_status="not_evaluated",
            counterevidence_to_seek=(),
            decision_readiness="abstained",
            decision=None,
            decision_reason="question_not_ready",
            next_actions=_NO_SIGNAL_NEXT_ACTIONS,
        )
    return CodeReviewEpistemicState(
        question_id=CODE_REVIEW_QUESTION_ID,
        question_version=CODE_REVIEW_QUESTION_VERSION,
        observation_status="confirmed",
        observations=_confirmed_observations(evidence),
        inference_status="abstained",
        inferences=(),
        hypotheses=_STRUCTURAL_HYPOTHESES,
        question_readiness="ready",
        question_reason=None,
        required_evidence=_CHANGE_DECISION_REQUIRED_EVIDENCE,
        satisfied_evidence=("confirmed_structural_hotspot",),
        missing_evidence=_CHANGE_DECISION_MISSING_EVIDENCE,
        counterevidence_status="not_evaluated",
        counterevidence_to_seek=_COUNTEREVIDENCE_TO_SEEK,
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="structural_observation_alone_cannot_justify_change",
        next_actions=_NEXT_ACTIONS,
    )


def assess_code_review_actionability(
    evidence: CodeReviewActionabilityInput,
) -> CodeReviewActionabilityAssessment:
    """Open a maintenance question without inventing semantic construction.

    Names, file suffixes and outgoing call spellings are deliberately excluded
    from semantic classification.  Renaming a repository, adding a wrapper or
    spelling a call ``commit`` cannot manufacture persistence evidence or a
    change recommendation.
    """

    source_role = classify_source_role(evidence.path, evidence.root)
    epistemic = _epistemic_state(evidence)
    actionability: Actionability = (
        "characterize_first"
        if epistemic.decision_readiness == "experiment_required"
        else "insufficient_evidence"
    )
    assessment_evidence = (
        f"source_role_path_convention:{source_role}",
        "semantic_construction:abstained:not_observed",
        f"question_readiness:{epistemic.question_readiness}",
        f"decision_readiness:{epistemic.decision_readiness}",
        *epistemic.observations,
    )
    if evidence.outgoing_calls_truncated:
        assessment_evidence = (*assessment_evidence, "outgoing_calls:truncated_not_interpreted")
    return CodeReviewActionabilityAssessment(
        path_convention_role=source_role,
        construction="unknown",
        actionability=actionability,
        change_risk="unknown",
        recommended_change=False,
        epistemic_state=epistemic,
        evidence=assessment_evidence,
        contracts_to_preserve=(),
        recommended_validation=(),
    )


__all__ = [
    "CODE_REVIEW_ACTIONABILITY",
    "CODE_REVIEW_QUESTION_ID",
    "CODE_REVIEW_QUESTION_VERSION",
    "Actionability",
    "ChangeRisk",
    "CodeReviewActionabilityAssessment",
    "CodeReviewActionabilityInput",
    "CodeReviewEpistemicState",
    "Construction",
    "DecisionOutcome",
    "DecisionReadiness",
    "InferenceStatus",
    "ObservationStatus",
    "PathConventionRole",
    "QuestionReadiness",
    "SourceRole",
    "assess_code_review_actionability",
    "classify_source_role",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_review_actionability")
