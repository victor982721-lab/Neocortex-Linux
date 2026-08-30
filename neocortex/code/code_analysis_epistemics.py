"""Generic, immutable epistemic contracts for Code analysis questions.

The Code database remains the owner of machine facts and provider receipts.
These types are a bounded, read-only projection over resolvable source records;
they are not a second fact store, a human-decision log, or mutation authority.

Version 1 deliberately has no machine-decision state.  Complete decision
evidence can make an evaluation ready for *human review*, but Code never owns
the human decision itself.
"""

from __future__ import annotations
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from typing import Literal, get_args

from neocortex.semantic.semantic_models import canonical_json, fingerprint_text

CODE_ANALYSIS_EPISTEMICS_SCHEMA = "neocortex.code-analysis-epistemics/v1"

AnalysisSubjectKind = Literal[
    "file",
    "symbol",
    "class",
    "module",
    "project",
    "run",
    "contract",
    "scc",
    "configuration",
    "entrypoint",
    "capability",
    "workflow",
    "logical_owner",
    "state_owner",
    "state_store",
    "database_table",
    "transaction_scope",
    "migration",
    "schema",
    "invariant",
    "test",
    "runtime_scenario",
    "dependency",
    "release",
    "work_package",
    "experiment",
    "analyzer",
]
EvidenceRole = Literal["supporting", "counterevidence", "experiment_result", "limitation"]
EvidenceKind = Literal[
    "internal_fact",
    "internal_metric",
    "internal_relation",
    "internal_diagnostic",
    "external_finding",
    "external_metric",
    "external_relation",
    "runtime_observation",
    "contract",
    "experiment_result",
]
EvidenceCompleteness = Literal["complete", "partial", "unknown"]
EvidenceFreshness = Literal["current", "publication_only", "unknown"]
EvidenceStage = Literal["question", "decision"]
NextActionKind = Literal["characterization", "counterevidence_search", "experiment"]
RequirementStatus = Literal["satisfied", "missing", "not_evaluated"]
ObservationStatus = Literal["confirmed", "abstained"]
QuestionReadiness = Literal["ready", "abstained"]
DecisionReadiness = Literal["human_review_required", "experiment_required", "abstained"]
CounterEvidenceStatus = Literal["evaluated", "not_evaluated"]
AnalysisScalar = str | int | float | bool | None

_SUBJECT_KINDS = frozenset(get_args(AnalysisSubjectKind))
_EVIDENCE_ROLES = frozenset(get_args(EvidenceRole))
_EVIDENCE_KINDS = frozenset(get_args(EvidenceKind))
_COMPLETENESS_VALUES = frozenset(get_args(EvidenceCompleteness))


def _required_text(label: str, value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its {maximum}-character bound")
    return value


def _unique_texts(label: str, values: tuple[str, ...], *, maximum: int = 128) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{label} must be an immutable tuple")
    for value in values:
        _required_text(label, value, maximum=maximum)
    if len(set(values)) != len(values):
        raise ValueError(f"{label} cannot contain duplicates")


def analysis_identity(prefix: str, payload: object) -> str:
    """Return one portable identity for canonical epistemic material."""

    _required_text("identity prefix", prefix, maximum=96)
    if any(character.isspace() for character in prefix):
        raise ValueError("identity prefix cannot contain whitespace")
    return f"{prefix}:xxh3_128:" + fingerprint_text(canonical_json(payload)).xxh3_128


@dataclass(frozen=True, slots=True)
class AnalysisFact:
    """One scalar observation; names and units carry no decision authority."""

    name: str
    value: AnalysisScalar
    unit: str | None = None

    def __post_init__(self) -> None:
        _required_text("fact name", self.name, maximum=128)
        if self.unit is not None:
            _required_text("fact unit", self.unit, maximum=64)
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("analysis fact values must be finite")
        if not isinstance(self.value, (str, int, float, bool, type(None))):
            raise ValueError("analysis fact value must be a JSON scalar")


@dataclass(frozen=True, slots=True)
class AnalysisSourceLocation:
    path: str
    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int = 0

    def __post_init__(self) -> None:
        _required_text("source path", self.path, maximum=32_768)
        if (
            isinstance(self.start_line, bool)
            or not isinstance(self.start_line, int)
            or self.start_line < 1
            or isinstance(self.end_line, bool)
            or not isinstance(self.end_line, int)
            or self.end_line < self.start_line
        ):
            raise ValueError("analysis source line range is invalid")
        for value in (self.start_column, self.end_column):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("analysis source columns must be non-negative integers")


@dataclass(frozen=True, slots=True)
class AnalysisSubjectRef:
    """Snapshot-bound identity; ``source_owner_id`` is not logical ownership."""

    subject_kind: AnalysisSubjectKind
    subject_key: str
    display_name: str
    source_owner_id: str
    snapshot_id: str
    snapshot_freshness: EvidenceFreshness
    revision_id: str | None = None
    location: AnalysisSourceLocation | None = None

    def __post_init__(self) -> None:
        if self.subject_kind not in _SUBJECT_KINDS:
            raise ValueError("analysis subject kind is invalid")
        for label, value, maximum in (
            ("subject key", self.subject_key, 1_024),
            ("subject display name", self.display_name, 2_048),
            ("subject source owner", self.source_owner_id, 128),
            ("subject snapshot", self.snapshot_id, 2_048),
        ):
            _required_text(label, value, maximum=maximum)
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("analysis subject snapshot freshness is invalid")
        if self.revision_id is not None:
            _required_text("subject revision", self.revision_id, maximum=512)
        if self.location is not None and not isinstance(self.location, AnalysisSourceLocation):
            raise ValueError("analysis subject location is invalid")


@dataclass(frozen=True, slots=True)
class AnalysisEvidenceRef:
    """Resolved pointer plus a bounded projection of the exact source record."""

    evidence_id: str
    subject_key: str
    role: EvidenceRole
    evidence_kind: EvidenceKind
    source_owner_id: str
    producer_id: str
    producer_version: str
    source_schema: str
    source_record_kind: str
    source_record_id: str
    source_projection_digest: str
    snapshot_id: str
    revision_id: str | None
    facts: tuple[AnalysisFact, ...]
    completeness: EvidenceCompleteness
    bounded: bool
    truncated: bool
    resolver_id: str
    resolver_version: str
    resolution_status: Literal["resolved"] = "resolved"
    limitations: tuple[str, ...] = ()
    provider_run_id: int | None = None
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("evidence id", self.evidence_id, 1_024),
            ("evidence subject", self.subject_key, 1_024),
            ("evidence source owner", self.source_owner_id, 128),
            ("evidence producer", self.producer_id, 256),
            ("evidence producer version", self.producer_version, 256),
            ("evidence source schema", self.source_schema, 256),
            ("evidence source record kind", self.source_record_kind, 128),
            ("evidence source record id", self.source_record_id, 512),
            ("evidence source projection digest", self.source_projection_digest, 256),
            ("evidence snapshot", self.snapshot_id, 2_048),
            ("evidence resolver", self.resolver_id, 256),
            ("evidence resolver version", self.resolver_version, 128),
        ):
            _required_text(label, value, maximum=maximum)
        if self.role not in _EVIDENCE_ROLES or self.evidence_kind not in _EVIDENCE_KINDS:
            raise ValueError("analysis evidence role or kind is invalid")
        if self.completeness not in _COMPLETENESS_VALUES:
            raise ValueError("analysis evidence completeness is invalid")
        if not isinstance(self.bounded, bool) or not isinstance(self.truncated, bool):
            raise ValueError("analysis evidence bounding flags must be boolean")
        if self.completeness == "complete" and self.truncated:
            raise ValueError("complete analysis evidence cannot be truncated")
        if self.resolution_status != "resolved":
            raise ValueError("analysis evidence must be resolved before publication")
        if self.revision_id is not None:
            _required_text("evidence revision", self.revision_id, maximum=512)
        if not isinstance(self.facts, tuple) or not self.facts:
            raise ValueError("analysis evidence requires immutable scalar facts")
        if any(not isinstance(fact, AnalysisFact) for fact in self.facts):
            raise ValueError("analysis evidence facts are invalid")
        fact_names = tuple(fact.name for fact in self.facts)
        if len(set(fact_names)) != len(fact_names):
            raise ValueError("analysis evidence fact names cannot repeat")
        _unique_texts("evidence limitation", self.limitations, maximum=256)
        if self.provider_run_id is not None and (
            isinstance(self.provider_run_id, bool)
            or not isinstance(self.provider_run_id, int)
            or self.provider_run_id < 1
        ):
            raise ValueError("analysis evidence provider run identity is invalid")
        if self.evidence_kind.startswith("external_") and self.provider_run_id is None:
            raise ValueError("external analysis evidence requires a provider run identity")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("analysis evidence must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class AnalysisEvidenceRequirementSpec:
    requirement_id: str
    stage: EvidenceStage
    role: EvidenceRole
    accepted_evidence_kinds: tuple[EvidenceKind, ...]
    accepted_completeness: tuple[EvidenceCompleteness, ...] = ("complete",)
    allow_truncated: bool = False
    minimum_evidence: int = 1

    def __post_init__(self) -> None:
        _required_text("evidence requirement id", self.requirement_id, maximum=256)
        if self.stage not in {"question", "decision"}:
            raise ValueError("evidence requirement stage is invalid")
        if self.role not in _EVIDENCE_ROLES:
            raise ValueError("evidence requirement role is invalid")
        if not self.accepted_evidence_kinds or any(
            kind not in _EVIDENCE_KINDS for kind in self.accepted_evidence_kinds
        ):
            raise ValueError("evidence requirement kinds are invalid")
        if len(set(self.accepted_evidence_kinds)) != len(self.accepted_evidence_kinds):
            raise ValueError("evidence requirement kinds cannot repeat")
        if not self.accepted_completeness or any(
            value not in _COMPLETENESS_VALUES for value in self.accepted_completeness
        ):
            raise ValueError("evidence requirement completeness contract is invalid")
        if len(set(self.accepted_completeness)) != len(self.accepted_completeness):
            raise ValueError("evidence requirement completeness values cannot repeat")
        if not isinstance(self.allow_truncated, bool):
            raise ValueError("evidence requirement truncation policy must be boolean")
        if (
            isinstance(self.minimum_evidence, bool)
            or not isinstance(self.minimum_evidence, int)
            or self.minimum_evidence < 1
        ):
            raise ValueError("evidence requirement minimum must be positive")


@dataclass(frozen=True, slots=True)
class AnalysisNextActionSpec:
    action_id: str
    kind: NextActionKind
    description: str

    def __post_init__(self) -> None:
        _required_text("analysis next-action id", self.action_id, maximum=256)
        if self.kind not in {"characterization", "counterevidence_search", "experiment"}:
            raise ValueError("analysis next-action kind is invalid")
        _required_text("analysis next-action description", self.description, maximum=512)


@dataclass(frozen=True, slots=True)
class AnalysisQuestionSpec:
    question_id: str
    version: str
    subject_kinds: tuple[AnalysisSubjectKind, ...]
    requirements: tuple[AnalysisEvidenceRequirementSpec, ...]
    hypotheses: tuple[str, ...]
    counterevidence_rules: tuple[str, ...]
    next_actions: tuple[AnalysisNextActionSpec, ...]
    decision_policy: Literal["human_required"] = "human_required"
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("question id", self.question_id, maximum=256)
        _required_text("question version", self.version, maximum=64)
        if not self.subject_kinds or any(kind not in _SUBJECT_KINDS for kind in self.subject_kinds):
            raise ValueError("analysis question subject kinds are invalid")
        if len(set(self.subject_kinds)) != len(self.subject_kinds):
            raise ValueError("analysis question subject kinds cannot repeat")
        if not self.requirements or any(
            not isinstance(requirement, AnalysisEvidenceRequirementSpec)
            for requirement in self.requirements
        ):
            raise ValueError("analysis question requires typed evidence requirements")
        requirement_ids = tuple(item.requirement_id for item in self.requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("analysis question requirement ids cannot repeat")
        if not any(item.stage == "question" for item in self.requirements):
            raise ValueError("analysis question requires question-readiness evidence")
        if not any(item.stage == "decision" for item in self.requirements):
            raise ValueError("analysis question requires decision-readiness evidence")
        _unique_texts("question hypothesis", self.hypotheses, maximum=256)
        _unique_texts("counterevidence rule", self.counterevidence_rules, maximum=256)
        if len(self.hypotheses) < 2:
            raise ValueError("analysis question requires competing hypotheses")
        if not self.counterevidence_rules:
            raise ValueError("analysis question requires explicit counterevidence rules")
        if not self.next_actions or any(
            not isinstance(item, AnalysisNextActionSpec) for item in self.next_actions
        ):
            raise ValueError("analysis question requires typed next actions")
        action_ids = tuple(item.action_id for item in self.next_actions)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("analysis question next-action ids cannot repeat")
        if not any(item.kind == "experiment" for item in self.next_actions):
            raise ValueError("analysis question requires a discriminating experiment template")
        if self.decision_policy != "human_required":
            raise ValueError("analysis question decisions must remain human-owned")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("analysis question spec must remain advisory and non-mutating")


def analysis_question_spec_fingerprint(spec: AnalysisQuestionSpec) -> str:
    if not isinstance(spec, AnalysisQuestionSpec):
        raise ValueError("analysis question spec is invalid")
    return analysis_identity("code-question-spec-v1", asdict(spec))


@dataclass(frozen=True, slots=True)
class AnalysisRequirementEvaluation:
    requirement_id: str
    status: RequirementStatus
    evidence_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _required_text("requirement evaluation id", self.requirement_id, maximum=256)
        if self.status not in {"satisfied", "missing", "not_evaluated"}:
            raise ValueError("requirement evaluation status is invalid")
        _unique_texts("requirement evidence id", self.evidence_ids, maximum=1_024)
        _required_text("requirement evaluation reason", self.reason, maximum=256)
        if self.status == "satisfied" and not self.evidence_ids:
            raise ValueError("satisfied requirement requires evidence identities")
        if self.status != "satisfied" and self.evidence_ids:
            raise ValueError("non-satisfied requirement cannot claim evidence identities")


@dataclass(frozen=True, slots=True)
class AnalysisQuestionEvaluation:
    evaluation_id: str
    question_id: str
    question_version: str
    question_spec_fingerprint: str
    rank: int
    subject: AnalysisSubjectRef
    evidence: tuple[AnalysisEvidenceRef, ...]
    requirements: tuple[AnalysisRequirementEvaluation, ...]
    observation_status: ObservationStatus
    inference_status: Literal["abstained"]
    inferences: tuple[()]
    hypotheses: tuple[str, ...]
    question_readiness: QuestionReadiness
    decision_readiness: DecisionReadiness
    decision: None
    decision_reason: str
    counterevidence_status: CounterEvidenceStatus
    next_action_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("question evaluation id", self.evaluation_id, maximum=1_024)
        _required_text("question evaluation question id", self.question_id, maximum=256)
        _required_text("question evaluation version", self.question_version, maximum=64)
        _required_text(
            "question evaluation spec fingerprint",
            self.question_spec_fingerprint,
            maximum=256,
        )
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("analysis question rank must be positive")
        if not isinstance(self.subject, AnalysisSubjectRef):
            raise ValueError("analysis question subject is invalid")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, AnalysisEvidenceRef) for item in self.evidence
        ):
            raise ValueError("analysis question evidence must be an immutable typed tuple")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("analysis question evidence identities cannot repeat")
        if any(item.subject_key != self.subject.subject_key for item in self.evidence):
            raise ValueError("analysis question evidence must reference its subject")
        if any(item.snapshot_id != self.subject.snapshot_id for item in self.evidence):
            raise ValueError("analysis question evidence must reference its subject snapshot")
        if any(
            self.subject.revision_id is not None and item.revision_id != self.subject.revision_id
            for item in self.evidence
        ):
            raise ValueError("analysis question evidence must reference its subject revision")
        if not isinstance(self.requirements, tuple) or any(
            not isinstance(item, AnalysisRequirementEvaluation) for item in self.requirements
        ):
            raise ValueError("analysis requirement evaluations are invalid")
        if self.observation_status not in {"confirmed", "abstained"}:
            raise ValueError("analysis observation status is invalid")
        if self.inference_status != "abstained" or self.inferences:
            raise ValueError("analysis epistemics v1 has no semantic inference resolver")
        if self.question_readiness not in {"ready", "abstained"}:
            raise ValueError("analysis question readiness is invalid")
        if self.decision_readiness not in {
            "human_review_required",
            "experiment_required",
            "abstained",
        }:
            raise ValueError("analysis decision readiness is invalid")
        if self.decision is not None:
            raise ValueError("analysis epistemics v1 never owns a human decision")
        _required_text("analysis decision reason", self.decision_reason, maximum=256)
        if self.counterevidence_status not in {"evaluated", "not_evaluated"}:
            raise ValueError("analysis counterevidence status is invalid")
        _unique_texts("analysis hypothesis", self.hypotheses, maximum=256)
        _unique_texts("analysis next-action id", self.next_action_ids, maximum=256)
        _unique_texts("analysis limitation", self.limitations, maximum=256)
        if not self.limitations:
            raise ValueError("analysis question evaluation requires explicit limitations")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("analysis question evaluation must remain advisory and non-mutating")


def validate_analysis_question_evaluation(
    spec: AnalysisQuestionSpec,
    evaluation: AnalysisQuestionEvaluation,
) -> None:
    """Validate a resolved projection against one explicit evidence contract.

    Source-specific resolvers must construct and independently re-check each
    ``AnalysisEvidenceRef``.  This generic validator proves the partition,
    completeness, readiness, and authority semantics over those resolved refs;
    it does not query an owner database itself.
    """

    if evaluation.question_id != spec.question_id or evaluation.question_version != spec.version:
        raise ValueError("analysis question evaluation/spec identity mismatch")
    if evaluation.question_spec_fingerprint != analysis_question_spec_fingerprint(spec):
        raise ValueError("analysis question evaluation/spec fingerprint mismatch")
    if evaluation.subject.subject_kind not in spec.subject_kinds:
        raise ValueError("analysis question subject kind is outside the spec")
    if evaluation.hypotheses != spec.hypotheses:
        raise ValueError("analysis question hypotheses must come from the spec")
    expected_requirements = {item.requirement_id: item for item in spec.requirements}
    actual_requirements = {item.requirement_id: item for item in evaluation.requirements}
    if len(actual_requirements) != len(evaluation.requirements) or set(actual_requirements) != set(
        expected_requirements
    ):
        raise ValueError("analysis question requirement partition is incomplete")
    evidence_by_id = {item.evidence_id: item for item in evaluation.evidence}
    if len(evidence_by_id) != len(evaluation.evidence):
        raise ValueError("analysis question evidence identities are duplicated")
    referenced_evidence: set[str] = set()
    for requirement_id, requirement in expected_requirements.items():
        result = actual_requirements[requirement_id]
        if result.status != "satisfied":
            continue
        if len(result.evidence_ids) < requirement.minimum_evidence:
            raise ValueError("analysis requirement has too few evidence records")
        for evidence_id in result.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise ValueError("analysis requirement references unknown evidence")
            if evidence_id in referenced_evidence:
                raise ValueError("analysis evidence cannot satisfy multiple requirements")
            referenced_evidence.add(evidence_id)
            if evidence.role != requirement.role or evidence.evidence_kind not in (
                requirement.accepted_evidence_kinds
            ):
                raise ValueError("analysis requirement evidence has the wrong role or kind")
            if evidence.completeness not in requirement.accepted_completeness:
                raise ValueError("analysis requirement evidence is epistemically incomplete")
            if evidence.truncated and not requirement.allow_truncated:
                raise ValueError("analysis requirement rejects truncated evidence")
    if referenced_evidence != set(evidence_by_id):
        raise ValueError("analysis question contains unreferenced evidence")
    question_requirements = tuple(
        actual_requirements[item.requirement_id]
        for item in spec.requirements
        if item.stage == "question"
    )
    decision_requirements = tuple(
        actual_requirements[item.requirement_id]
        for item in spec.requirements
        if item.stage == "decision"
    )
    question_ready = all(item.status == "satisfied" for item in question_requirements)
    decision_evidence_complete = all(item.status == "satisfied" for item in decision_requirements)
    expected_question_readiness: QuestionReadiness = "ready" if question_ready else "abstained"
    expected_observation_status: ObservationStatus = "confirmed" if question_ready else "abstained"
    if decision_evidence_complete and question_ready:
        expected_decision_readiness: DecisionReadiness = "human_review_required"
        expected_decision_reason = "decision_evidence_complete_human_decision_required"
        expected_next_action_ids: tuple[str, ...] = ()
    elif question_ready:
        expected_decision_readiness = "experiment_required"
        expected_decision_reason = "decision_evidence_incomplete"
        expected_next_action_ids = tuple(item.action_id for item in spec.next_actions)
    else:
        expected_decision_readiness = "abstained"
        expected_decision_reason = "question_evidence_incomplete"
        expected_next_action_ids = ()
    counter_requirements = tuple(
        actual_requirements[item.requirement_id]
        for item in spec.requirements
        if item.role == "counterevidence"
    )
    counterevidence_evaluated = bool(counter_requirements) and all(
        item.status == "satisfied" for item in counter_requirements
    )
    expected_counter_status: CounterEvidenceStatus = (
        "evaluated" if counterevidence_evaluated else "not_evaluated"
    )
    if (
        evaluation.question_readiness != expected_question_readiness
        or evaluation.observation_status != expected_observation_status
        or evaluation.decision_readiness != expected_decision_readiness
        or evaluation.decision_reason != expected_decision_reason
        or evaluation.next_action_ids != expected_next_action_ids
        or evaluation.counterevidence_status != expected_counter_status
    ):
        raise ValueError("analysis question readiness is not derived from its evidence contract")
    if evaluation.observation_status == "confirmed" and not evaluation.evidence:
        raise ValueError("confirmed analysis question requires linked evidence")


def validate_analysis_question_set(
    specs: tuple[AnalysisQuestionSpec, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
) -> None:
    """Validate a bounded registry/evaluation projection without persisting it."""

    if not isinstance(specs, tuple) or any(
        not isinstance(item, AnalysisQuestionSpec) for item in specs
    ):
        raise ValueError("analysis question specs must be an immutable typed tuple")
    if not isinstance(evaluations, tuple) or any(
        not isinstance(item, AnalysisQuestionEvaluation) for item in evaluations
    ):
        raise ValueError("analysis question evaluations must be an immutable typed tuple")
    specs_by_id = {(item.question_id, item.version): item for item in specs}
    if len(specs_by_id) != len(specs):
        raise ValueError("analysis question spec identities cannot repeat")
    evaluation_ids = tuple(item.evaluation_id for item in evaluations)
    if len(set(evaluation_ids)) != len(evaluation_ids):
        raise ValueError("analysis question evaluation identities cannot repeat")
    ranks = tuple(item.rank for item in evaluations)
    if ranks != tuple(range(1, len(evaluations) + 1)):
        raise ValueError("analysis question ranks must be contiguous and deterministic")
    referenced_specs: set[tuple[str, str]] = set()
    for evaluation in evaluations:
        identity = (evaluation.question_id, evaluation.question_version)
        spec = specs_by_id.get(identity)
        if spec is None:
            raise ValueError("analysis question evaluation references an unknown spec")
        referenced_specs.add(identity)
        validate_analysis_question_evaluation(spec, evaluation)
    if referenced_specs != set(specs_by_id):
        raise ValueError("analysis question registry contains unused specs")


def analysis_questions_payload(
    specs: tuple[AnalysisQuestionSpec, ...],
    evaluations: tuple[AnalysisQuestionEvaluation, ...],
) -> dict[str, object]:
    validate_analysis_question_set(specs, evaluations)
    return {
        "schema": CODE_ANALYSIS_EPISTEMICS_SCHEMA,
        "specs": [
            {
                **asdict(item),
                "spec_fingerprint": analysis_question_spec_fingerprint(item),
            }
            for item in specs
        ],
        "evaluations": [asdict(item) for item in evaluations],
    }


def _strict_mapping(label: str, value: object, expected: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _sequence(label: str, value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    return value


def _text_sequence(label: str, value: object) -> tuple[str, ...]:
    return tuple(_required_text(label, item, maximum=1_024) for item in _sequence(label, value))


def _parse_question_spec(value: object) -> AnalysisQuestionSpec:
    expected = {field.name for field in fields(AnalysisQuestionSpec)} | {"spec_fingerprint"}
    raw = _strict_mapping("analysis question spec", value, expected)
    requirement_fields = {field.name for field in fields(AnalysisEvidenceRequirementSpec)}
    requirements: list[AnalysisEvidenceRequirementSpec] = []
    for item in _sequence("analysis question requirements", raw["requirements"]):
        values = dict(_strict_mapping("analysis evidence requirement", item, requirement_fields))
        values["accepted_evidence_kinds"] = _text_sequence(
            "accepted evidence kind", values["accepted_evidence_kinds"]
        )
        values["accepted_completeness"] = _text_sequence(
            "accepted evidence completeness", values["accepted_completeness"]
        )
        requirements.append(AnalysisEvidenceRequirementSpec(**values))  # type: ignore[arg-type]
    action_fields = {field.name for field in fields(AnalysisNextActionSpec)}
    actions = tuple(
        AnalysisNextActionSpec(
            **dict(_strict_mapping("analysis next action", item, action_fields))  # type: ignore[arg-type]
        )
        for item in _sequence("analysis next actions", raw["next_actions"])
    )
    values = {key: item for key, item in raw.items() if key != "spec_fingerprint"}
    values["subject_kinds"] = _text_sequence("analysis subject kind", values["subject_kinds"])
    values["requirements"] = tuple(requirements)
    values["hypotheses"] = _text_sequence("analysis hypothesis", values["hypotheses"])
    values["counterevidence_rules"] = _text_sequence(
        "analysis counterevidence rule", values["counterevidence_rules"]
    )
    values["next_actions"] = actions
    spec = AnalysisQuestionSpec(**values)  # type: ignore[arg-type]
    if raw["spec_fingerprint"] != analysis_question_spec_fingerprint(spec):
        raise ValueError("analysis question spec fingerprint is invalid")
    return spec


def _parse_subject(value: object) -> AnalysisSubjectRef:
    raw = _strict_mapping(
        "analysis subject",
        value,
        {field.name for field in fields(AnalysisSubjectRef)},
    )
    values = dict(raw)
    location = values["location"]
    if location is not None:
        values["location"] = AnalysisSourceLocation(
            **dict(
                _strict_mapping(
                    "analysis source location",
                    location,
                    {field.name for field in fields(AnalysisSourceLocation)},
                )
            )  # type: ignore[arg-type]
        )
    return AnalysisSubjectRef(**values)  # type: ignore[arg-type]


def _parse_evidence(value: object) -> AnalysisEvidenceRef:
    raw = _strict_mapping(
        "analysis evidence",
        value,
        {field.name for field in fields(AnalysisEvidenceRef)},
    )
    values = dict(raw)
    fact_fields = {field.name for field in fields(AnalysisFact)}
    values["facts"] = tuple(
        AnalysisFact(
            **dict(_strict_mapping("analysis fact", item, fact_fields))  # type: ignore[arg-type]
        )
        for item in _sequence("analysis evidence facts", values["facts"])
    )
    values["limitations"] = _text_sequence("analysis evidence limitation", values["limitations"])
    return AnalysisEvidenceRef(**values)  # type: ignore[arg-type]


def _parse_question_evaluation(value: object) -> AnalysisQuestionEvaluation:
    raw = _strict_mapping(
        "analysis question evaluation",
        value,
        {field.name for field in fields(AnalysisQuestionEvaluation)},
    )
    values = dict(raw)
    values["subject"] = _parse_subject(values["subject"])
    values["evidence"] = tuple(
        _parse_evidence(item)
        for item in _sequence("analysis question evidence", values["evidence"])
    )
    requirement_fields = {field.name for field in fields(AnalysisRequirementEvaluation)}
    requirements: list[AnalysisRequirementEvaluation] = []
    for item in _sequence("analysis requirement evaluations", values["requirements"]):
        requirement_values = dict(
            _strict_mapping("analysis requirement evaluation", item, requirement_fields)
        )
        requirement_values["evidence_ids"] = _text_sequence(
            "analysis requirement evidence id", requirement_values["evidence_ids"]
        )
        requirements.append(
            AnalysisRequirementEvaluation(**requirement_values)  # type: ignore[arg-type]
        )
    values["requirements"] = tuple(requirements)
    values["inferences"] = tuple(_sequence("analysis inferences", values["inferences"]))
    values["hypotheses"] = _text_sequence("analysis hypothesis", values["hypotheses"])
    values["next_action_ids"] = _text_sequence("analysis next action id", values["next_action_ids"])
    values["limitations"] = _text_sequence("analysis limitation", values["limitations"])
    return AnalysisQuestionEvaluation(**values)  # type: ignore[arg-type]


def parse_analysis_questions_payload(
    payload: Mapping[str, object],
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Strictly reconstruct and validate one generic epistemic wire envelope."""

    raw = _strict_mapping("analysis epistemic payload", payload, {"schema", "specs", "evaluations"})
    if raw["schema"] != CODE_ANALYSIS_EPISTEMICS_SCHEMA:
        raise ValueError("analysis epistemic payload schema is invalid")
    specs = tuple(
        _parse_question_spec(item) for item in _sequence("analysis question specs", raw["specs"])
    )
    evaluations = tuple(
        _parse_question_evaluation(item)
        for item in _sequence("analysis question evaluations", raw["evaluations"])
    )
    validate_analysis_question_set(specs, evaluations)
    return specs, evaluations


__all__ = [
    "CODE_ANALYSIS_EPISTEMICS_SCHEMA",
    "AnalysisEvidenceRef",
    "AnalysisEvidenceRequirementSpec",
    "AnalysisFact",
    "AnalysisNextActionSpec",
    "AnalysisQuestionEvaluation",
    "AnalysisQuestionSpec",
    "AnalysisRequirementEvaluation",
    "AnalysisSourceLocation",
    "AnalysisSubjectRef",
    "analysis_identity",
    "analysis_question_spec_fingerprint",
    "analysis_questions_payload",
    "parse_analysis_questions_payload",
    "validate_analysis_question_evaluation",
    "validate_analysis_question_set",
]
