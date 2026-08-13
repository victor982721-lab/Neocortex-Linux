"""Resolve declared invariants against exact trusted-deep test outcomes.

This is the first assurance vertical that links a versioned invariant registry
to independently executed runtime scenarios.  The provider receipt proves the
outcome of one exact selected pytest nodeid under one bounded measurement
scope.  It does not prove the invariant universally, infer assertions from test
names, or authorize a change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Literal, Mapping, Sequence, cast

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
    analysis_questions_payload,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from .code_invariant_contracts import (
    CODE_INVARIANT_REGISTRY_SCHEMA,
    INVARIANT_SPECS,
    RUNTIME_SCENARIOS,
    InvariantSpec,
    invariant_registry_fingerprint,
)
from .external_deep_coverage import (
    DEEP_COVERAGE_PROVIDER_SCHEMA,
    PYTEST_COVERAGE_PROVIDER_ID,
)
from .external_evidence_models import ExternalProviderEvidence, ExternalProviderRelation

CODE_INVARIANT_ASSURANCE_SCHEMA = "neocortex.code-invariant-assurance/v1"
CODE_INVARIANT_ASSURANCE_POLICY = "declared-invariant-exact-test-outcome-receipts-v1"

INVARIANT_ASSURANCE_QUESTION = AnalysisQuestionSpec(
    question_id="assurance.declared_invariant_scenarios_are_observed",
    version="v1",
    subject_kinds=("invariant",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "versioned_invariant_and_scenario_contract",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "exact_selected_runtime_scenario_outcomes",
            "question",
            "supporting",
            ("external_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "negative_scenario_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("external_relation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "independent_additional_scenario_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "declared_scenarios_continue_to_exercise_the_invariant_under_the_current_snapshot",
        "the_declared_scenarios_are_missing_failing_or_too_narrow_for_the_current_snapshot",
    ),
    counterevidence_rules=(
        "a_failed_or_skipped_declared_scenario_is_preserved_not_hidden_by_aggregate_suite_status",
        "a_passing_scenario_is_not_a_formal_or_exhaustive_invariant_proof",
        "coverage_execution_without_an_explicit_registry_link_is_not_invariant_evidence",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_declared_scenario_scope",
            "characterization",
            "Inspect exact scenario inputs, isolation, assertions, and known limitations.",
        ),
        AnalysisNextActionSpec(
            "seek_failed_skipped_or_unselected_scenario_counterevidence",
            "counterevidence_search",
            "Preserve failed, skipped, and unselected declared scenarios as uncertainty.",
        ),
        AnalysisNextActionSpec(
            "run_independent_invariant_scenario",
            "experiment",
            "Run the cheapest additional isolated scenario that challenges the invariant.",
        ),
    ),
)

_LIMITATIONS = (
    "passing_declared_scenario_is_not_formal_or_exhaustive_invariant_proof",
    "test_outcome_receipt_does_not_describe_individual_assertions",
    "scenario_registry_is_source_versioned_and_requires_current_snapshot_execution",
    "failed_skipped_and_unselected_scenarios_remain_counterevidence_or_missing_evidence",
    "human_decision_and_mutation_authority_are_not_owned_by_this_analysis",
)


def _required(label: str, value: object, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class InvariantScenarioOutcome:
    scenario_id: str
    test_nodeid: str
    status: Literal["passed", "failed", "skipped", "not_selected", "provider_abstained"]
    provider_run_id: int | None
    relation_id: str | None
    measurement_scope_signature: str | None

    def __post_init__(self) -> None:
        _required("scenario id", self.scenario_id, 256)
        _required("scenario test nodeid", self.test_nodeid)
        if self.status not in {
            "passed",
            "failed",
            "skipped",
            "not_selected",
            "provider_abstained",
        }:
            raise ValueError("invariant scenario outcome status is invalid")
        resolved = self.status in {"passed", "failed", "skipped"}
        if resolved != (self.provider_run_id is not None and self.relation_id is not None):
            raise ValueError("resolved scenario outcome requires exact provider evidence")
        if self.provider_run_id is not None and (
            isinstance(self.provider_run_id, bool) or self.provider_run_id < 1
        ):
            raise ValueError("scenario provider run identity is invalid")
        if self.relation_id is not None:
            _required("scenario relation id", self.relation_id, 1024)
        if self.measurement_scope_signature is not None:
            _required("scenario measurement scope", self.measurement_scope_signature, 512)


@dataclass(frozen=True, slots=True)
class InvariantAssuranceObservation:
    observation_id: str
    invariant_id: str
    statement: str
    scope: str
    failure_impact: str
    scenarios: tuple[InvariantScenarioOutcome, ...]
    status: Literal[
        "all_declared_scenarios_passed",
        "counterevidence_observed",
        "scenario_evidence_incomplete",
        "provider_abstained",
    ]
    passed: int
    failed: int
    skipped: int
    missing: int
    decision: None = None
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("invariant assurance observation", self.observation_id, 512)
        _required("invariant id", self.invariant_id, 256)
        _required("invariant statement", self.statement, 2048)
        if not self.scenarios:
            raise ValueError("invariant assurance requires declared scenarios")
        counts = {
            "passed": sum(item.status == "passed" for item in self.scenarios),
            "failed": sum(item.status == "failed" for item in self.scenarios),
            "skipped": sum(item.status == "skipped" for item in self.scenarios),
            "missing": sum(
                item.status in {"not_selected", "provider_abstained"} for item in self.scenarios
            ),
        }
        if (self.passed, self.failed, self.skipped, self.missing) != tuple(counts.values()):
            raise ValueError("invariant scenario counts are not derived from outcomes")
        expected = (
            "counterevidence_observed"
            if self.failed or self.skipped
            else "provider_abstained"
            if all(item.status == "provider_abstained" for item in self.scenarios)
            else "scenario_evidence_incomplete"
            if self.missing
            else "all_declared_scenarios_passed"
        )
        if self.status != expected:
            raise ValueError("invariant assurance status is not derived from outcomes")
        if self.decision is not None or self.authority != "advisory" or self.mutation_authority:
            raise ValueError("invariant assurance must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class CodeInvariantAssuranceAnalysis:
    analysis_id: str
    status: Literal["ready", "partial", "abstained"]
    reason: str | None
    policy_id: str
    snapshot_id: str
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    provider_id: str
    provider_status: Literal["ready", "abstained", "not_recorded"]
    provider_run_id: int | None
    registry_fingerprint: str
    declared_invariants: int
    declared_scenarios: int
    resolved_scenarios: int
    passed_scenarios: int
    counterevidence_scenarios: int
    observations: tuple[InvariantAssuranceObservation, ...]
    question_specs: tuple[AnalysisQuestionSpec, ...]
    question_evaluations: tuple[AnalysisQuestionEvaluation, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("invariant assurance analysis", self.analysis_id, 512)
        _required("invariant assurance snapshot", self.snapshot_id, 2048)
        if self.status not in {"ready", "partial", "abstained"}:
            raise ValueError("invariant assurance status is invalid")
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("invariant assurance freshness is invalid")
        if self.policy_id != CODE_INVARIANT_ASSURANCE_POLICY:
            raise ValueError("invariant assurance policy is invalid")
        if self.provider_id != PYTEST_COVERAGE_PROVIDER_ID:
            raise ValueError("invariant assurance provider is invalid")
        if self.provider_status not in {"ready", "abstained", "not_recorded"}:
            raise ValueError("invariant assurance provider status is invalid")
        if (self.provider_status == "ready") != (self.provider_run_id is not None):
            raise ValueError("invariant assurance provider identity is inconsistent")
        if self.declared_invariants != len(INVARIANT_SPECS) or self.declared_scenarios != len(
            RUNTIME_SCENARIOS
        ):
            raise ValueError("invariant assurance registry counts are invalid")
        outcomes = tuple(
            item for observation in self.observations for item in observation.scenarios
        )
        if self.resolved_scenarios != sum(
            item.status in {"passed", "failed", "skipped"} for item in outcomes
        ):
            raise ValueError("invariant assurance resolved count is invalid")
        if self.passed_scenarios != sum(item.status == "passed" for item in outcomes):
            raise ValueError("invariant assurance pass count is invalid")
        if self.counterevidence_scenarios != sum(
            item.status in {"failed", "skipped"} for item in outcomes
        ):
            raise ValueError("invariant assurance counterevidence count is invalid")
        if self.status == "abstained":
            if self.reason is None or self.observations or self.question_evaluations:
                raise ValueError("abstained invariant assurance has invalid evidence")
        else:
            if self.reason is not None or len(self.observations) != len(INVARIANT_SPECS):
                raise ValueError("observed invariant assurance has invalid readiness")
            if self.question_specs != (INVARIANT_ASSURANCE_QUESTION,):
                raise ValueError("invariant assurance question registry is invalid")
            for evaluation in self.question_evaluations:
                validate_analysis_question_evaluation(INVARIANT_ASSURANCE_QUESTION, evaluation)
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("invariant assurance analysis must remain advisory and non-mutating")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"schema": CODE_INVARIANT_ASSURANCE_SCHEMA, **asdict(self)}
        questions = analysis_questions_payload(self.question_specs, self.question_evaluations)
        payload["question_specs"] = questions["specs"]
        payload["question_evaluations"] = questions["evaluations"]
        return payload


def _outcome_relations(
    provider: ExternalProviderEvidence,
) -> dict[str, ExternalProviderRelation]:
    result: dict[str, ExternalProviderRelation] = {}
    for relation in provider.relations:
        if relation.relation_kind != "declared_test_outcome":
            continue
        metadata = relation.metadata
        nodeid = metadata.get("nodeid")
        outcome = metadata.get("outcome")
        if (
            not isinstance(nodeid, str)
            or outcome not in {"passed", "failed", "skipped"}
            or relation.source_key != f"pytest-nodeid:{nodeid}"
            or relation.source_kind != "contract"
            or relation.target_kind != "run"
            or metadata.get("claim_scope") != "exact_selected_test_execution_outcome"
            or metadata.get("assertion_or_invariant_proof") is not False
            or nodeid in result
        ):
            raise ValueError("trusted-deep test outcome relation is incompatible")
        result[nodeid] = relation
    return result


def _scenario_outcome(
    scenario_id: str,
    nodeid: str,
    *,
    provider: ExternalProviderEvidence | None,
    relations: Mapping[str, ExternalProviderRelation],
) -> InvariantScenarioOutcome:
    if provider is None:
        return InvariantScenarioOutcome(scenario_id, nodeid, "not_selected", None, None, None)
    if provider.status != "ready" or provider.effective_tool_run_id is None:
        return InvariantScenarioOutcome(scenario_id, nodeid, "provider_abstained", None, None, None)
    relation = relations.get(nodeid)
    if relation is None:
        return InvariantScenarioOutcome(scenario_id, nodeid, "not_selected", None, None, None)
    outcome = str(relation.metadata["outcome"])
    scope = relation.metadata.get("measurement_scope_signature")
    return InvariantScenarioOutcome(
        scenario_id,
        nodeid,
        outcome,  # type: ignore[arg-type]
        provider.effective_tool_run_id,
        relation.portable_relation_id,
        str(scope) if isinstance(scope, str) else None,
    )


def _observation(
    invariant: InvariantSpec,
    *,
    provider: ExternalProviderEvidence | None,
    relations: Mapping[str, ExternalProviderRelation],
) -> InvariantAssuranceObservation:
    scenarios_by_id = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    outcomes = tuple(
        _scenario_outcome(
            scenario_id,
            scenarios_by_id[scenario_id].test_nodeid,
            provider=provider,
            relations=relations,
        )
        for scenario_id in invariant.scenario_ids
    )
    passed = sum(item.status == "passed" for item in outcomes)
    failed = sum(item.status == "failed" for item in outcomes)
    skipped = sum(item.status == "skipped" for item in outcomes)
    missing = sum(item.status in {"not_selected", "provider_abstained"} for item in outcomes)
    status = (
        "counterevidence_observed"
        if failed or skipped
        else "provider_abstained"
        if all(item.status == "provider_abstained" for item in outcomes)
        else "scenario_evidence_incomplete"
        if missing
        else "all_declared_scenarios_passed"
    )
    return InvariantAssuranceObservation(
        observation_id=analysis_identity(
            "invariant-assurance-observation-v1",
            {
                "invariant": invariant.invariant_id,
                "outcomes": tuple(asdict(item) for item in outcomes),
                "registry": invariant_registry_fingerprint(),
            },
        ),
        invariant_id=invariant.invariant_id,
        statement=invariant.statement,
        scope=invariant.scope,
        failure_impact=invariant.failure_impact,
        scenarios=outcomes,
        status=status,  # type: ignore[arg-type]
        passed=passed,
        failed=failed,
        skipped=skipped,
        missing=missing,
    )


def _evaluation(
    observation: InvariantAssuranceObservation,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    rank: int,
) -> AnalysisQuestionEvaluation:
    subject = AnalysisSubjectRef(
        subject_kind="invariant",
        subject_key=f"invariant:{observation.invariant_id}",
        display_name=observation.statement,
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=invariant_registry_fingerprint(),
    )
    contract_digest = invariant_registry_fingerprint()
    contract = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "invariant-contract-ref-v1",
            {"subject": subject.subject_key, "registry": contract_digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id="code-invariant-registry",
        producer_version=CODE_INVARIANT_REGISTRY_SCHEMA,
        source_schema=CODE_INVARIANT_REGISTRY_SCHEMA,
        source_record_kind="versioned_invariant_spec",
        source_record_id=observation.invariant_id,
        source_projection_digest=contract_digest,
        snapshot_id=snapshot_id,
        revision_id=contract_digest,
        facts=(
            AnalysisFact("declared_scenarios", len(observation.scenarios), "count"),
            AnalysisFact("failure_impact", observation.failure_impact),
            AnalysisFact("registry_fingerprint", contract_digest),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-invariant-registry-resolver",
        resolver_version="v1",
        limitations=("source_versioned_registry_not_runtime_proof",),
    )
    resolved = tuple(
        item for item in observation.scenarios if item.status in {"passed", "failed", "skipped"}
    )
    scenario_evidence: AnalysisEvidenceRef | None = None
    counterevidence: AnalysisEvidenceRef | None = None
    if resolved:
        run_ids = {item.provider_run_id for item in resolved}
        scopes = {item.measurement_scope_signature for item in resolved}
        if len(run_ids) != 1 or None in run_ids:
            raise ValueError("invariant scenarios do not share an exact provider run")
        digest = analysis_identity(
            "invariant-runtime-scenario-projection-v1",
            tuple(asdict(item) for item in resolved),
        )
        scenario_evidence = AnalysisEvidenceRef(
            evidence_id=analysis_identity(
                "invariant-runtime-scenario-ref-v1",
                {"subject": subject.subject_key, "projection": digest},
            ),
            subject_key=subject.subject_key,
            role="supporting",
            evidence_kind="external_relation",
            source_owner_id="code",
            producer_id=PYTEST_COVERAGE_PROVIDER_ID,
            producer_version=DEEP_COVERAGE_PROVIDER_SCHEMA,
            source_schema=DEEP_COVERAGE_PROVIDER_SCHEMA,
            source_record_kind="declared_test_outcome_relations",
            source_record_id=str(next(iter(run_ids))),
            source_projection_digest=digest,
            snapshot_id=snapshot_id,
            revision_id=contract_digest,
            facts=(
                AnalysisFact("resolved_scenarios", len(resolved), "count"),
                AnalysisFact("passed_scenarios", observation.passed, "count"),
                AnalysisFact("failed_scenarios", observation.failed, "count"),
                AnalysisFact("skipped_scenarios", observation.skipped, "count"),
                AnalysisFact("measurement_scopes", len(scopes), "count"),
            ),
            completeness="complete" if len(resolved) == len(observation.scenarios) else "partial",
            bounded=True,
            truncated=False,
            resolver_id="trusted-deep-declared-test-outcome-resolver",
            resolver_version="v1",
            limitations=(
                "outcome_is_exact_test_execution_not_assertion_level_evidence",
                "passing_scenario_is_not_universal_invariant_proof",
            ),
            provider_run_id=next(iter(run_ids)),
        )
        negatives = tuple(item for item in resolved if item.status in {"failed", "skipped"})
        if negatives:
            negative_digest = analysis_identity(
                "invariant-negative-scenario-projection-v1",
                tuple(asdict(item) for item in negatives),
            )
            counterevidence = AnalysisEvidenceRef(
                evidence_id=analysis_identity(
                    "invariant-negative-scenario-ref-v1",
                    {"subject": subject.subject_key, "projection": negative_digest},
                ),
                subject_key=subject.subject_key,
                role="counterevidence",
                evidence_kind="external_relation",
                source_owner_id="code",
                producer_id=PYTEST_COVERAGE_PROVIDER_ID,
                producer_version=DEEP_COVERAGE_PROVIDER_SCHEMA,
                source_schema=DEEP_COVERAGE_PROVIDER_SCHEMA,
                source_record_kind="failed_or_skipped_declared_scenarios",
                source_record_id=str(next(iter(run_ids))),
                source_projection_digest=negative_digest,
                snapshot_id=snapshot_id,
                revision_id=contract_digest,
                facts=(
                    AnalysisFact("negative_scenarios", len(negatives), "count"),
                    AnalysisFact("failed_scenarios", observation.failed, "count"),
                    AnalysisFact("skipped_scenarios", observation.skipped, "count"),
                ),
                completeness="complete",
                bounded=True,
                truncated=False,
                resolver_id="trusted-deep-negative-scenario-resolver",
                resolver_version="v1",
                limitations=("failure_or_skip_requires_human_interpretation",),
                provider_run_id=next(iter(run_ids)),
            )
    evidence = tuple(
        item for item in (contract, scenario_evidence, counterevidence) if item is not None
    )
    requirements = (
        AnalysisRequirementEvaluation(
            "versioned_invariant_and_scenario_contract",
            "satisfied",
            (contract.evidence_id,),
            "versioned_invariant_contract_resolved",
        ),
        AnalysisRequirementEvaluation(
            "exact_selected_runtime_scenario_outcomes",
            "satisfied" if scenario_evidence is not None else "missing",
            () if scenario_evidence is None else (scenario_evidence.evidence_id,),
            (
                "declared_runtime_scenario_outcomes_resolved"
                if scenario_evidence is not None
                else "declared_runtime_scenario_outcomes_missing"
            ),
        ),
        AnalysisRequirementEvaluation(
            "negative_scenario_counterevidence_evaluated",
            "satisfied" if counterevidence is not None else "not_evaluated",
            () if counterevidence is None else (counterevidence.evidence_id,),
            (
                "failed_or_skipped_scenario_counterevidence_observed"
                if counterevidence is not None
                else "negative_scenario_counterevidence_not_observed"
            ),
        ),
        AnalysisRequirementEvaluation(
            "independent_additional_scenario_result",
            "missing",
            (),
            "independent_additional_scenario_not_recorded",
        ),
    )
    question_ready = scenario_evidence is not None
    result = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "invariant-assurance-question-evaluation-v1",
            {
                "observation": observation.observation_id,
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=INVARIANT_ASSURANCE_QUESTION.question_id,
        question_version=INVARIANT_ASSURANCE_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(INVARIANT_ASSURANCE_QUESTION),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed" if question_ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=INVARIANT_ASSURANCE_QUESTION.hypotheses,
        question_readiness="ready" if question_ready else "abstained",
        decision_readiness="experiment_required" if question_ready else "abstained",
        decision=None,
        decision_reason="decision_evidence_incomplete"
        if question_ready
        else "question_evidence_incomplete",
        counterevidence_status="evaluated" if counterevidence is not None else "not_evaluated",
        next_action_ids=(
            tuple(item.action_id for item in INVARIANT_ASSURANCE_QUESTION.next_actions)
            if question_ready
            else ()
        ),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(INVARIANT_ASSURANCE_QUESTION, result)
    return result


def analyze_code_invariant_assurance(
    providers: Mapping[str, ExternalProviderEvidence],
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> CodeInvariantAssuranceAnalysis:
    _required("invariant assurance snapshot", snapshot_id, 2048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("invariant assurance freshness is invalid")
    provider = providers.get(PYTEST_COVERAGE_PROVIDER_ID)
    if provider is None:
        provider_status: Literal["ready", "abstained", "not_recorded"] = "not_recorded"
        relations: Mapping[str, ExternalProviderRelation] = {}
    else:
        provider_status = provider.status
        relations = _outcome_relations(provider) if provider.status == "ready" else {}
    observations = tuple(
        _observation(invariant, provider=provider, relations=relations)
        for invariant in INVARIANT_SPECS
    )
    resolved = sum(
        item.status in {"passed", "failed", "skipped"}
        for observation in observations
        for item in observation.scenarios
    )
    passed = sum(
        item.status == "passed" for observation in observations for item in observation.scenarios
    )
    negatives = sum(
        item.status in {"failed", "skipped"}
        for observation in observations
        for item in observation.scenarios
    )
    status: Literal["ready", "partial", "abstained"]
    if provider is None or provider.status != "ready":
        status = "abstained"
        reason = "trusted_deep_provider_not_recorded" if provider is None else provider.reason
        published_observations: tuple[InvariantAssuranceObservation, ...] = ()
        evaluations: tuple[AnalysisQuestionEvaluation, ...] = ()
        specs: tuple[AnalysisQuestionSpec, ...] = ()
    else:
        status = "ready" if resolved == len(RUNTIME_SCENARIOS) else "partial"
        reason = None
        published_observations = observations
        evaluations = tuple(
            _evaluation(
                item,
                snapshot_id=snapshot_id,
                snapshot_freshness=snapshot_freshness,
                rank=index,
            )
            for index, item in enumerate(observations, start=1)
        )
        specs = (INVARIANT_ASSURANCE_QUESTION,)
    payload = {
        "snapshot": snapshot_id,
        "freshness": snapshot_freshness,
        "provider_status": provider_status,
        "provider_run": None if provider is None else provider.effective_tool_run_id,
        "registry": invariant_registry_fingerprint(),
        "observations": tuple(asdict(item) for item in published_observations),
        "policy": CODE_INVARIANT_ASSURANCE_POLICY,
    }
    return CodeInvariantAssuranceAnalysis(
        analysis_id=analysis_identity("code-invariant-assurance-v1", payload),
        status=status,
        reason=reason,
        policy_id=CODE_INVARIANT_ASSURANCE_POLICY,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        provider_id=PYTEST_COVERAGE_PROVIDER_ID,
        provider_status=provider_status,
        provider_run_id=(
            provider.effective_tool_run_id
            if provider is not None and provider.status == "ready"
            else None
        ),
        registry_fingerprint=invariant_registry_fingerprint(),
        declared_invariants=len(INVARIANT_SPECS),
        declared_scenarios=len(RUNTIME_SCENARIOS),
        resolved_scenarios=resolved if status != "abstained" else 0,
        passed_scenarios=passed if status != "abstained" else 0,
        counterevidence_scenarios=negatives if status != "abstained" else 0,
        observations=published_observations,
        question_specs=specs,
        question_evaluations=evaluations,
    )


def parse_code_invariant_assurance_payload(
    payload: Mapping[str, object],
) -> CodeInvariantAssuranceAnalysis:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_INVARIANT_ASSURANCE_SCHEMA:
        raise ValueError("invariant assurance payload schema is invalid")
    values = dict(payload)
    values.pop("schema", None)
    if set(values) != {item.name for item in fields(CodeInvariantAssuranceAnalysis)}:
        raise ValueError("invariant assurance payload fields are invalid")
    parsed_observations: list[InvariantAssuranceObservation] = []
    for raw in cast(Sequence[object], values["observations"]):
        if not isinstance(raw, Mapping):
            raise ValueError("invariant assurance observation is invalid")
        item = dict(raw)
        item["scenarios"] = tuple(InvariantScenarioOutcome(**entry) for entry in item["scenarios"])
        parsed_observations.append(InvariantAssuranceObservation(**item))
    values["observations"] = tuple(parsed_observations)
    # The generic parser provides full forged-wire validation for these nested contracts.
    from .code_analysis_epistemics import parse_analysis_questions_payload

    specs, evaluations = parse_analysis_questions_payload(
        {
            "schema": "neocortex.code-analysis-epistemics/v1",
            "specs": values["question_specs"],
            "evaluations": values["question_evaluations"],
        }
    )
    values["question_specs"] = specs
    values["question_evaluations"] = evaluations
    values["limitations"] = tuple(values["limitations"])
    return CodeInvariantAssuranceAnalysis(**values)


__all__ = [
    "CODE_INVARIANT_ASSURANCE_POLICY",
    "CODE_INVARIANT_ASSURANCE_SCHEMA",
    "INVARIANT_ASSURANCE_QUESTION",
    "CodeInvariantAssuranceAnalysis",
    "InvariantAssuranceObservation",
    "InvariantScenarioOutcome",
    "analyze_code_invariant_assurance",
    "parse_code_invariant_assurance_payload",
]
