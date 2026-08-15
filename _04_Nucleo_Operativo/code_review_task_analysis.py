"""Fail-closed Code evidence for the durable Framework ReviewTask protocol.

This module projects only versioned contracts that are already owned by
Framework, Review and Code architecture.  It does not inspect a live database,
authenticate a human actor, execute recovery experiments or authorize a state
transition.  Those decision-level claims require isolated experiment receipts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisScalar,
    AnalysisSubjectRef,
    EvidenceFreshness,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from .code_architecture_contracts import architecture_contract_manifest
from .framework_schema import SCHEMA_VERSION as FRAMEWORK_SCHEMA_VERSION
from .logical_owner_contracts import LOGICAL_OWNER_SPECS, matching_logical_owners
from .review_task_contracts import (
    REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
    REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION,
    CanonicalJsonObject,
    ReviewTaskActorKind,
    ReviewTaskState,
    ReviewTaskTransition,
)
from .state_topology_contracts import STATE_STORE_REGISTRY

CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA = (
    "neocortex.code-review-task-protocol-analysis/v1"
)
CODE_REVIEW_TASK_PROTOCOL_POLICY = "framework-review-task-contract-projection-v1"
CODE_REVIEW_TASK_PROTOCOL_RESOLVER = "framework-review-task-contract-resolver"
CODE_REVIEW_TASK_PROTOCOL_RESOLVER_VERSION = "v1"

FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID = (
    "framework.review_task_lifecycle_preserves_atomicity_and_human_authority"
)
FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_VERSION = "v1"
FRAMEWORK_REVIEW_TASK_PROTOCOL_SUBJECT_KEY = (
    "contract:framework-review-task-protocol"
)

_LOGICAL_OWNER_ID = "review"
_STATE_OWNER_ID = "framework"
_STATE_STORE_ID = "sqlite:framework.sqlite3"
_DATABASE_NAME = "framework.sqlite3"
_FRAMEWORK_SCHEMA_VERSION = 22
_REVIEW_TASK_CONTRACT_VERSION = 1
_PUBLIC_ADAPTER_MODULE = "neocortex.review_task_cli_adapter"
_PUBLIC_PORT_MODULE = "_04_Nucleo_Operativo.value_review_port"
_HUMAN_TERMINAL_STATES = ("dismissed", "resolved")
_OWNER_STORE_RECORD_KIND = "framework_review_task_owner_store_contract"
_PUBLIC_PROTOCOL_RECORD_KIND = "framework_review_task_public_protocol_contract"

_LIMITATIONS = (
    "declared_actor_identity_is_not_an_authenticated_human_identity",
    "contract_projection_does_not_prove_atomic_publication_recovery_or_idempotent_replay",
    "sqlite_process_death_experiments_do_not_prove_power_loss_safety",
    "review_task_analysis_is_advisory_and_never_authorizes_a_transition",
)


FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION = AnalysisQuestionSpec(
    question_id=FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID,
    version=FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_VERSION,
    subject_kinds=("contract",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "framework_review_task_owner_store_contract_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "framework_review_task_public_protocol_contract_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "review_task_stale_head_and_fault_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "isolated_review_task_protocol_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "review_task_publication_preserves_one_atomic_cas_ordered_human_owned_lifecycle",
        "stale_heads_faults_or_replays_can_publish_partial_duplicate_or_unauthorized_state",
    ),
    counterevidence_rules=(
        "a_stale_expected_head_must_not_advance_the_durable_review_task_head",
        "non_human_terminal_decisions_and_public_superseded_transitions_must_be_rejected",
        "faulted_publication_and_same_key_replay_must_not_create_partial_or_conflicting_state",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_framework_review_task_protocol_contract",
            "characterization",
            "Re-resolve the declared owner, store, schema, public port and lifecycle contract.",
        ),
        AnalysisNextActionSpec(
            "seek_review_task_stale_head_and_fault_counterevidence",
            "counterevidence_search",
            "Exercise stale CAS, non-human terminal, superseded and interrupted "
            "publication controls.",
        ),
        AnalysisNextActionSpec(
            "run_framework_review_task_protocol_experiment",
            "experiment",
            "Run the isolated ReviewTask publish, CAS, replay, recovery and authority matrix.",
        ),
    ),
)


def _required_text(label: str, value: object, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _terminal_transition(
    state: ReviewTaskState,
    *,
    actor_kind: ReviewTaskActorKind,
    decision: CanonicalJsonObject | None,
) -> ReviewTaskTransition:
    return ReviewTaskTransition(
        event_id=f"contract-probe-{state.value}-{actor_kind.value}",
        event_key=f"contract-probe-key-{state.value}-{actor_kind.value}",
        task_id="contract-probe-task",
        expected_event_id="contract-probe-head",
        expected_state=ReviewTaskState.OPEN,
        to_state=state,
        actor_kind=actor_kind,
        actor_id="contract-probe-actor",
        provenance=CanonicalJsonObject.from_mapping({"source": "contract-probe"}),
        decision=decision,
        note=None,
        observed_ns=1,
        recorded_ns=1,
    )


def _terminal_decisions_require_human() -> bool:
    decision = CanonicalJsonObject.from_mapping({"disposition": "contract-probe"})
    for state in (ReviewTaskState.DISMISSED, ReviewTaskState.RESOLVED):
        _terminal_transition(
            state,
            actor_kind=ReviewTaskActorKind.HUMAN,
            decision=decision,
        )
        for actor_kind, candidate_decision in (
            (ReviewTaskActorKind.SYSTEM, decision),
            (ReviewTaskActorKind.HUMAN, None),
        ):
            try:
                _terminal_transition(
                    state,
                    actor_kind=actor_kind,
                    decision=candidate_decision,
                )
            except ValueError as exc:
                if str(exc) != "resolved/dismissed review events require a human decision":
                    return False
            else:
                return False
    return True


def _superseded_is_repository_only() -> bool:
    try:
        _terminal_transition(
            ReviewTaskState.SUPERSEDED,
            actor_kind=ReviewTaskActorKind.SYSTEM,
            decision=None,
        )
    except ValueError as exc:
        return str(exc) == "SUPERSEDED is reserved for receipt-backed repository transitions"
    return False


def _public_adapter_port_is_declared() -> bool:
    manifest = architecture_contract_manifest()
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list):
        return False
    selected = tuple(
        item
        for item in contracts
        if isinstance(item, dict)
        and item.get("contract_id") == "neocortex-core-ui-boundary-v1"
    )
    if len(selected) != 1:
        return False
    allowlist = selected[0].get("allowlist")
    if not isinstance(allowlist, list):
        return False
    pair = [_PUBLIC_ADAPTER_MODULE, _PUBLIC_PORT_MODULE]
    return sum(item == pair for item in allowlist) == 1


def _live_contract_projection() -> dict[str, object]:
    owner_matches = matching_logical_owners(
        "_04_Nucleo_Operativo.review_task_repository"
    )
    adapter_owner_matches = matching_logical_owners(_PUBLIC_ADAPTER_MODULE)
    owner_specs = tuple(
        item for item in LOGICAL_OWNER_SPECS if item.owner_id == _LOGICAL_OWNER_ID
    )
    if (
        owner_matches != (_LOGICAL_OWNER_ID,)
        or adapter_owner_matches != (_LOGICAL_OWNER_ID,)
        or len(owner_specs) != 1
        or owner_specs[0].state_owner_ids != (_STATE_OWNER_ID,)
    ):
        raise ValueError("Framework ReviewTask logical ownership contract is incompatible")

    store = STATE_STORE_REGISTRY.by_owner(_STATE_OWNER_ID)
    if (
        store.state_store_id != _STATE_STORE_ID
        or store.database_name != _DATABASE_NAME
        or store.storage_engine != "sqlite"
        or store.expected_schema_version != _FRAMEWORK_SCHEMA_VERSION
        or FRAMEWORK_SCHEMA_VERSION != _FRAMEWORK_SCHEMA_VERSION
        or REVIEW_TASK_FRAMEWORK_SCHEMA_VERSION != _FRAMEWORK_SCHEMA_VERSION
        or REVIEW_TASK_CONTRACT_SCHEMA_VERSION != _REVIEW_TASK_CONTRACT_VERSION
    ):
        raise ValueError("Framework ReviewTask durable store or schema contract is incompatible")
    if not _public_adapter_port_is_declared():
        raise ValueError("Framework ReviewTask public adapter/port contract is incompatible")

    terminal_states = tuple(
        sorted((ReviewTaskState.DISMISSED.value, ReviewTaskState.RESOLVED.value))
    )
    terminal_human = _terminal_decisions_require_human()
    superseded_repository_only = _superseded_is_repository_only()
    if (
        terminal_states != _HUMAN_TERMINAL_STATES
        or not terminal_human
        or not superseded_repository_only
    ):
        raise ValueError("Framework ReviewTask lifecycle authority contract is incompatible")

    return {
        "policy_id": CODE_REVIEW_TASK_PROTOCOL_POLICY,
        "logical_owner_id": _LOGICAL_OWNER_ID,
        "state_owner_id": _STATE_OWNER_ID,
        "state_store_id": store.state_store_id,
        "database_name": store.database_name,
        "framework_schema_version": store.expected_schema_version,
        "review_task_contract_version": REVIEW_TASK_CONTRACT_SCHEMA_VERSION,
        "public_adapter_module": _PUBLIC_ADAPTER_MODULE,
        "public_port_module": _PUBLIC_PORT_MODULE,
        "terminal_states": terminal_states,
        "terminal_decisions_require_human": terminal_human,
        "superseded_repository_only": superseded_repository_only,
        "authority": "advisory",
        "mutation_authority": False,
    }


@dataclass(frozen=True, slots=True)
class FrameworkReviewTaskProtocolAnalysis:
    analysis_id: str
    policy_id: str
    logical_owner_id: str
    state_owner_id: str
    state_store_id: str
    database_name: str
    framework_schema_version: int
    review_task_contract_version: int
    public_adapter_module: str
    public_port_module: str
    terminal_states: tuple[str, ...]
    terminal_decisions_require_human: bool
    superseded_repository_only: bool
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("ReviewTask protocol analysis id", self.analysis_id)
        expected = {
            "policy_id": CODE_REVIEW_TASK_PROTOCOL_POLICY,
            "logical_owner_id": _LOGICAL_OWNER_ID,
            "state_owner_id": _STATE_OWNER_ID,
            "state_store_id": _STATE_STORE_ID,
            "database_name": _DATABASE_NAME,
            "framework_schema_version": _FRAMEWORK_SCHEMA_VERSION,
            "review_task_contract_version": _REVIEW_TASK_CONTRACT_VERSION,
            "public_adapter_module": _PUBLIC_ADAPTER_MODULE,
            "public_port_module": _PUBLIC_PORT_MODULE,
            "terminal_states": _HUMAN_TERMINAL_STATES,
            "terminal_decisions_require_human": True,
            "superseded_repository_only": True,
            "authority": "advisory",
            "mutation_authority": False,
        }
        actual = {key: value for key, value in asdict(self).items() if key != "analysis_id"}
        if actual != expected:
            raise ValueError("Framework ReviewTask protocol analysis fields are incompatible")
        expected_id = analysis_identity("code-review-task-protocol-analysis-v1", expected)
        if self.analysis_id != expected_id:
            raise ValueError("Framework ReviewTask protocol analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA, **asdict(self)}


def build_framework_review_task_protocol_analysis() -> FrameworkReviewTaskProtocolAnalysis:
    """Re-resolve the exact v1 projection from live versioned contracts."""

    projection = _live_contract_projection()
    return FrameworkReviewTaskProtocolAnalysis(
        analysis_id=analysis_identity(
            "code-review-task-protocol-analysis-v1",
            projection,
        ),
        **projection,  # type: ignore[arg-type]
    )


def _contract_evidence(
    *,
    subject: AnalysisSubjectRef,
    analysis: FrameworkReviewTaskProtocolAnalysis,
    record_kind: str,
    projection: Mapping[str, object],
    facts: tuple[AnalysisFact, ...],
) -> AnalysisEvidenceRef:
    digest = analysis_identity(
        f"{record_kind}-source-v1",
        projection,
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            f"{record_kind}-evidence-v1",
            {
                "subject": subject.subject_key,
                "snapshot_id": subject.snapshot_id,
                "analysis_id": analysis.analysis_id,
                "projection_digest": digest,
            },
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id=CODE_REVIEW_TASK_PROTOCOL_RESOLVER,
        producer_version=CODE_REVIEW_TASK_PROTOCOL_RESOLVER_VERSION,
        source_schema=CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA,
        source_record_kind=record_kind,
        source_record_id=analysis.analysis_id,
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=True,
        truncated=False,
        resolver_id=CODE_REVIEW_TASK_PROTOCOL_RESOLVER,
        resolver_version=CODE_REVIEW_TASK_PROTOCOL_RESOLVER_VERSION,
        limitations=_LIMITATIONS,
    )


def framework_review_task_questions(
    *,
    snapshot_id: str,
    snapshot_freshness: EvidenceFreshness,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Publish the contract observation while requiring runtime decision evidence."""

    _required_text("ReviewTask protocol snapshot id", snapshot_id, maximum=2_048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("ReviewTask protocol snapshot freshness is invalid")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("ReviewTask protocol question rank must be positive")

    analysis = build_framework_review_task_protocol_analysis()
    spec = FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    subject = AnalysisSubjectRef(
        subject_kind="contract",
        subject_key=FRAMEWORK_REVIEW_TASK_PROTOCOL_SUBJECT_KEY,
        display_name="Framework durable ReviewTask protocol",
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=analysis.analysis_id,
    )
    owner_projection: dict[str, AnalysisScalar] = {
        "logical_owner_id": analysis.logical_owner_id,
        "state_owner_id": analysis.state_owner_id,
        "state_store_id": analysis.state_store_id,
        "database_name": analysis.database_name,
        "framework_schema_version": analysis.framework_schema_version,
        "review_task_contract_version": analysis.review_task_contract_version,
    }
    owner_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_OWNER_STORE_RECORD_KIND,
        projection=owner_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in owner_projection.items()),
    )
    protocol_projection: dict[str, AnalysisScalar] = {
        "public_adapter_module": analysis.public_adapter_module,
        "public_port_module": analysis.public_port_module,
        "terminal_states": ",".join(analysis.terminal_states),
        "terminal_decisions_require_human": analysis.terminal_decisions_require_human,
        "superseded_repository_only": analysis.superseded_repository_only,
    }
    protocol_evidence = _contract_evidence(
        subject=subject,
        analysis=analysis,
        record_kind=_PUBLIC_PROTOCOL_RECORD_KIND,
        projection=protocol_projection,
        facts=tuple(AnalysisFact(name, value) for name, value in protocol_projection.items()),
    )
    requirements = (
        AnalysisRequirementEvaluation(
            "framework_review_task_owner_store_contract_resolved",
            "satisfied",
            (owner_evidence.evidence_id,),
            "review_owner_and_framework_sqlite_store_are_exactly_declared",
        ),
        AnalysisRequirementEvaluation(
            "framework_review_task_public_protocol_contract_resolved",
            "satisfied",
            (protocol_evidence.evidence_id,),
            "public_adapter_port_and_lifecycle_authority_contracts_are_exactly_declared",
        ),
        AnalysisRequirementEvaluation(
            "review_task_stale_head_and_fault_counterevidence_evaluated",
            "not_evaluated",
            (),
            "no_linked_stale_head_authority_or_fault_counterevidence_receipt",
        ),
        AnalysisRequirementEvaluation(
            "isolated_review_task_protocol_experiment_result",
            "missing",
            (),
            "no_linked_isolated_review_task_protocol_experiment_receipt",
        ),
    )
    evidence = tuple(sorted((owner_evidence, protocol_evidence), key=lambda item: item.evidence_id))
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "framework-review-task-protocol-question-evaluation-v1",
            {
                "snapshot_id": snapshot_id,
                "analysis_id": analysis.analysis_id,
                "spec": fingerprint,
                "evidence_ids": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=fingerprint,
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
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
    return (spec,), (evaluation,)


__all__ = [
    "CODE_REVIEW_TASK_PROTOCOL_ANALYSIS_SCHEMA",
    "CODE_REVIEW_TASK_PROTOCOL_POLICY",
    "FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION",
    "FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_ID",
    "FRAMEWORK_REVIEW_TASK_PROTOCOL_QUESTION_VERSION",
    "FRAMEWORK_REVIEW_TASK_PROTOCOL_SUBJECT_KEY",
    "FrameworkReviewTaskProtocolAnalysis",
    "build_framework_review_task_protocol_analysis",
    "framework_review_task_questions",
]
