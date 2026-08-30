"""Bounded, read-only evidence for NeoCortex durable-retention safety.

The Retention planner already owns the store-specific SQL and schema checks.
This module does not duplicate those rules or authorize deletion.  It projects
one canonical dry-run page into Code epistemics, preserves missing holds and
blocked owners as counterevidence, and requires an isolated negative-control
experiment before an independent technical disposition can exist.
"""

from __future__ import annotations
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

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
from neocortex.workflow.retention.planner import (
    STORE_ORDER,
    RetentionPlan,
    RetentionPlanningCancelled,
    RetentionPolicy,
    RetentionStorePlan,
    plan_retention,
)
from neocortex.semantic.semantic_models import canonical_json

CODE_RETENTION_ANALYSIS_SCHEMA = "neocortex.code-retention-analysis/v1"
CODE_RETENTION_POLICY = "canonical-four-store-dry-run-hold-projection-v1"
CODE_RETENTION_RESOLVER = "retention-plan-owner-projection-resolver"
CODE_RETENTION_RESOLVER_VERSION = "v1"

RETENTION_EXPECTED_HOLDS: Mapping[str, tuple[str, ...]] = {
    "semantic": (
        "semantic_model_registry",
        "shared_semantic_payload_and_evidence",
        "shared_semantic_source_content",
    ),
    "catalog": (
        "classification_history",
        "uncertain_organization_actions",
    ),
    "inventory": ("shared_fingerprints",),
    "framework": (
        "file_action_audit_evidence",
        "human_review_evidence",
        "published_review_task_state",
    ),
}

RETENTION_HOLD_QUESTION = AnalysisQuestionSpec(
    question_id="retention.dry_run_preserves_declared_durable_holds",
    version="v1",
    subject_kinds=("workflow",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "dry_run_non_deletion_contract_resolved",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "owner_schemas_and_dependencies_resolved",
            "question",
            "supporting",
            ("internal_relation",),
            accepted_completeness=("complete", "partial"),
            allow_truncated=True,
        ),
        AnalysisEvidenceRequirementSpec(
            "declared_durable_holds_observed",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "bounded_pagination_is_explicit",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "retention_gap_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "isolated_retention_safety_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "declared_heads_builders_human_evidence_and_shared_payloads_are_protected",
        "a_missing_hold_blocked_owner_or_mixed_snapshot_can_make_eligibility_unsafe",
    ),
    counterevidence_rules=(
        "absent_blocked_or_schema_incompatible_owners_prevent_a_complete_disposition",
        "missing_declared_holds_or_invalid_resume_cursors_are_preserved_as_gaps",
        "a_read_only_plan_does_not_prove_that_a_future_delete_executor_is_safe",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_retention_owner_and_hold_gaps",
            "counterevidence_search",
            "Resolve blocked owners, missing holds and invalid pagination without deleting state.",
        ),
        AnalysisNextActionSpec(
            "run_retention_hold_safety_experiment",
            "experiment",
            "Exercise protected and eligible fixtures plus corruption and concurrent-commit controls.",
        ),
    ),
)

_LIMITATIONS = (
    "retention_analysis_is_a_dry_run_and_never_authorizes_deletion",
    "each_owner_has_a_stable_read_snapshot_but_the_four_owners_are_not_cross_database_atomic",
    "bounded_items_do_not_claim_complete_enumeration_when_a_resume_cursor_is_present",
    "estimated_rows_and_bytes_are_lower_bound_planning_observations",
    "passing_fixture_controls_do_not_prove_power_loss_or_a_future_delete_implementation",
)

# Narrow aliases keep runtime casts readable without widening the public model.
AnyRetentionStore = Literal["semantic", "catalog", "inventory", "framework"]
AnyRetentionStatus = Literal["ready", "absent", "blocked"]


class CodeRetentionResolutionError(ValueError):
    """The Retention projection could not be reproduced from live owners."""


def _required_text(label: str, value: object, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_text(label: str, value: object, maximum: int = 32_768) -> str | None:
    if value is None:
        return None
    return _required_text(label, value, maximum)


def _non_negative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_non_negative(label: str, value: object) -> int | None:
    if value is None:
        return None
    return _non_negative(label, value)


def _texts(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item, 512) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


@dataclass(frozen=True, slots=True)
class RetentionStoreObservation:
    store: Literal["semantic", "catalog", "inventory", "framework"]
    status: Literal["ready", "absent", "blocked"]
    schema_version: int | None
    eligible_rows: int
    eligible_bytes: int
    protected_rows: int
    protected_bytes: int
    item_count: int
    hold_names: tuple[str, ...]
    hold_rows: int
    next_after: int | None
    truncated: bool
    detail_code: str | None

    def __post_init__(self) -> None:
        if self.store not in set(STORE_ORDER) or self.status not in {
            "ready",
            "absent",
            "blocked",
        }:
            raise ValueError("retention store observation identity is invalid")
        _optional_non_negative("retention store schema", self.schema_version)
        for label, value in (
            ("eligible rows", self.eligible_rows),
            ("eligible bytes", self.eligible_bytes),
            ("protected rows", self.protected_rows),
            ("protected bytes", self.protected_bytes),
            ("item count", self.item_count),
            ("hold rows", self.hold_rows),
        ):
            _non_negative(label, value)
        _texts("retention hold name", self.hold_names)
        if self.hold_names != tuple(sorted(self.hold_names)):
            raise ValueError("retention hold names must be canonical")
        _optional_non_negative("retention next cursor", self.next_after)
        if not isinstance(self.truncated, bool):
            raise ValueError("retention truncation flag must be boolean")
        if self.truncated != (self.next_after is not None):
            raise ValueError("retention truncation must match its resume cursor")
        _optional_text("retention detail code", self.detail_code, 256)
        if self.status == "ready" and self.schema_version is None:
            raise ValueError("ready retention store requires an exact schema")
        if self.status != "ready" and any(
            (
                self.eligible_rows,
                self.eligible_bytes,
                self.protected_rows,
                self.protected_bytes,
                self.item_count,
                self.hold_rows,
            )
        ):
            raise ValueError("non-ready retention store cannot publish classified counts")


@dataclass(frozen=True, slots=True)
class CodeRetentionAnalysis:
    analysis_id: str
    status: Literal["ready", "abstained"]
    reason: str | None
    source_version: str
    policy_id: str
    stores: tuple[RetentionStoreObservation, ...]
    missing_hold_ids: tuple[str, ...]
    observation: Literal["declared_holds_resolved", "retention_gap_observed"] | None
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("retention analysis id", self.analysis_id, 256)
        _required_text("retention source version", self.source_version, 256)
        if self.policy_id != CODE_RETENTION_POLICY:
            raise ValueError("retention analysis policy is invalid")
        if self.status not in {"ready", "abstained"}:
            raise ValueError("retention analysis status is invalid")
        _optional_text("retention analysis reason", self.reason, 256)
        if not isinstance(self.stores, tuple) or any(
            not isinstance(item, RetentionStoreObservation) for item in self.stores
        ):
            raise ValueError("retention store observations are invalid")
        _texts("missing retention hold", self.missing_hold_ids)
        if self.missing_hold_ids != tuple(sorted(self.missing_hold_ids)):
            raise ValueError("missing retention holds must be canonical")
        if self.status == "abstained":
            if not self.reason or self.stores or self.missing_hold_ids or self.observation is not None:
                raise ValueError("abstained retention analysis cannot publish unresolved facts")
        else:
            if self.reason is not None:
                raise ValueError("ready retention analysis cannot carry an abstention reason")
            if tuple(item.store for item in self.stores) != STORE_ORDER:
                raise ValueError("ready retention analysis requires all stores in canonical order")
            expected_observation = (
                "declared_holds_resolved"
                if all(item.status == "ready" for item in self.stores)
                and not self.missing_hold_ids
                else "retention_gap_observed"
            )
            if self.observation != expected_observation:
                raise ValueError("retention observation is not derived")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("retention analysis must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-retention-analysis-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("retention analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_RETENTION_ANALYSIS_SCHEMA, **asdict(self)}


def _detail_code(detail: str | None) -> str | None:
    if detail is None:
        return None
    normalized = detail.casefold()
    if "schema" in normalized:
        return "schema_incompatible"
    if "dependency" in normalized:
        return "dependency_unresolvable"
    if "foreign_keys" in normalized:
        return "foreign_keys_unavailable"
    return "owner_unavailable"


def _store_observation(plan_store: RetentionStorePlan) -> RetentionStoreObservation:
    holds = plan_store.holds
    return RetentionStoreObservation(
        store=plan_store.store,
        status=plan_store.status,
        schema_version=plan_store.schema_version,
        eligible_rows=plan_store.eligible_rows,
        eligible_bytes=plan_store.eligible_bytes,
        protected_rows=plan_store.protected_rows,
        protected_bytes=plan_store.protected_bytes,
        item_count=len(plan_store.items),
        hold_names=tuple(sorted(str(item.name) for item in holds)),
        hold_rows=sum(int(item.rows) for item in holds),
        next_after=plan_store.next_after,
        truncated=plan_store.truncated,
        detail_code=_detail_code(plan_store.detail),
    )


def _analysis_values(
    *,
    status: Literal["ready", "abstained"],
    reason: str | None,
    source_version: str,
    stores: tuple[RetentionStoreObservation, ...],
    missing_hold_ids: tuple[str, ...],
) -> dict[str, object]:
    observation = (
        None
        if status == "abstained"
        else "declared_holds_resolved"
        if all(item.status == "ready" for item in stores) and not missing_hold_ids
        else "retention_gap_observed"
    )
    return {
        "status": status,
        "reason": reason,
        "source_version": source_version,
        "policy_id": CODE_RETENTION_POLICY,
        "stores": stores,
        "missing_hold_ids": missing_hold_ids,
        "observation": observation,
        "authority": "advisory",
        "mutation_authority": False,
    }


def _build_analysis(**values: object) -> CodeRetentionAnalysis:
    payload = {
        key: value if key != "stores" else [asdict(item) for item in cast(tuple, value)]
        for key, value in values.items()
    }
    return CodeRetentionAnalysis(
        analysis_id=analysis_identity("code-retention-analysis-v1", payload),
        **cast(Any, values),
    )


def abstained_code_retention(
    reason: str,
    *,
    source_version: str,
) -> CodeRetentionAnalysis:
    return _build_analysis(
        **_analysis_values(
            status="abstained",
            reason=_required_text("retention abstention reason", reason, 256),
            source_version=_required_text("retention source version", source_version, 256),
            stores=(),
            missing_hold_ids=(),
        )
    )


def _missing_holds(stores: tuple[RetentionStoreObservation, ...]) -> tuple[str, ...]:
    observed: dict[str, set[str]] = {
        item.store: set(item.hold_names) for item in stores
    }
    return tuple(
        sorted(
            f"{store}:{hold}"
            for store, expected in RETENTION_EXPECTED_HOLDS.items()
            for hold in expected
            if hold not in observed.get(store, set())
        )
    )


def analyze_code_retention(
    state_directory: Path,
    *,
    source_version: str,
    reference_time_ns: int | None = None,
) -> CodeRetentionAnalysis:
    """Read all four retention owners once without creating or migrating state."""

    source_version = _required_text("retention source version", source_version, 256)
    observed_ns = time.time_ns() if reference_time_ns is None else reference_time_ns
    _non_negative("retention reference time", observed_ns)
    try:
        plan = plan_retention(
            Path(state_directory),
            policy=RetentionPolicy(minimum_age_ns=0, keep_published=2, batch_size=100),
            now_ns=observed_ns,
        )
    except (OSError, RuntimeError, ValueError, RetentionPlanningCancelled):
        return abstained_code_retention(
            "retention_owner_projection_unresolvable",
            source_version=source_version,
        )
    if not isinstance(plan, RetentionPlan) or not plan.dry_run or plan.deletion_supported:
        return abstained_code_retention(
            "retention_non_deletion_contract_unresolvable",
            source_version=source_version,
        )
    stores = tuple(_store_observation(item) for item in plan.stores)
    values = _analysis_values(
        status="ready",
        reason=None,
        source_version=source_version,
        stores=stores,
        missing_hold_ids=_missing_holds(stores),
    )
    return _build_analysis(**values)


def resolve_code_retention(
    state_directory: Path,
    analysis: CodeRetentionAnalysis,
    *,
    reference_time_ns: int | None = None,
) -> None:
    """Reopen every owner and require the portable projection to be identical."""

    if analysis.status != "ready":
        raise CodeRetentionResolutionError("retention analysis is not ready")
    repeated = analyze_code_retention(
        state_directory,
        source_version=analysis.source_version,
        reference_time_ns=reference_time_ns,
    )
    if repeated != analysis:
        raise CodeRetentionResolutionError("retention projection changed during review")


def _projection_digest(kind: str, projection: object) -> str:
    return analysis_identity(kind, projection)


def _evidence(
    *,
    subject: AnalysisSubjectRef,
    record_kind: str,
    role: Literal["supporting", "counterevidence"],
    evidence_kind: Literal["internal_fact", "internal_relation", "contract"],
    projection: Mapping[str, object],
    facts: tuple[AnalysisFact, ...],
    completeness: Literal["complete", "partial"] = "complete",
    bounded: bool = False,
    truncated: bool = False,
) -> AnalysisEvidenceRef:
    digest = _projection_digest(f"retention-{record_kind}-source-v1", projection)
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            f"retention-{record_kind}-evidence-v1",
            {"subject": subject.subject_key, "projection_digest": digest},
        ),
        subject_key=subject.subject_key,
        role=role,
        evidence_kind=evidence_kind,
        source_owner_id="knowledge",
        producer_id=CODE_RETENTION_RESOLVER,
        producer_version=CODE_RETENTION_RESOLVER_VERSION,
        source_schema=CODE_RETENTION_ANALYSIS_SCHEMA,
        source_record_kind=record_kind,
        source_record_id=subject.snapshot_id,
        source_projection_digest=digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness=completeness,
        bounded=bounded,
        truncated=truncated,
        resolver_id=CODE_RETENTION_RESOLVER,
        resolver_version=CODE_RETENTION_RESOLVER_VERSION,
        limitations=_LIMITATIONS,
    )


def retention_questions(
    analysis: CodeRetentionAnalysis,
    *,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("retention question rank must be positive")
    spec = RETENTION_HOLD_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    subject = AnalysisSubjectRef(
        subject_kind="workflow",
        subject_key="retention:canonical-durable-holds",
        display_name="Canonical four-store durable retention holds",
        source_owner_id="knowledge",
        snapshot_id=analysis.analysis_id,
        snapshot_freshness="current" if analysis.status == "ready" else "unknown",
        revision_id=analysis.policy_id,
    )
    if analysis.status != "ready":
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "retention-question-evaluation-v1",
                {"analysis": analysis.analysis_id, "rank": rank, "spec": fingerprint},
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=fingerprint,
            rank=rank,
            subject=subject,
            evidence=(),
            requirements=tuple(
                AnalysisRequirementEvaluation(
                    item.requirement_id,
                    "not_evaluated" if item.role == "counterevidence" else "missing",
                    (),
                    analysis.reason or "retention_projection_abstained",
                )
                for item in spec.requirements
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
            limitations=(*_LIMITATIONS, "retention_projection_abstained"),
        )
        validate_analysis_question_evaluation(spec, evaluation)
        return (spec,), (evaluation,)

    store_payload = tuple(asdict(item) for item in analysis.stores)
    all_ready = all(item.status == "ready" for item in analysis.stores)
    pagination_valid = all(item.truncated == (item.next_after is not None) for item in analysis.stores)
    any_truncated = any(item.truncated for item in analysis.stores)
    contract = _evidence(
        subject=subject,
        record_kind="retention_plan_contract",
        role="supporting",
        evidence_kind="contract",
        projection={
            "policy_id": analysis.policy_id,
            "dry_run": True,
            "deletion_supported": False,
            "keep_published": 2,
            "minimum_age_ns": 0,
        },
        facts=(
            AnalysisFact("dry_run", True),
            AnalysisFact("deletion_supported", False),
            AnalysisFact("keep_published", 2, "count"),
            AnalysisFact("minimum_age_ns", 0, "nanoseconds"),
        ),
    )
    owners = _evidence(
        subject=subject,
        record_kind="retention_store_projection",
        role="supporting",
        evidence_kind="internal_relation",
        projection={"stores": store_payload},
        facts=(
            AnalysisFact("stores", len(analysis.stores), "count"),
            AnalysisFact("ready_stores", sum(item.status == "ready" for item in analysis.stores), "count"),
            AnalysisFact("stores_json", canonical_json(store_payload)),
        ),
        completeness="partial" if any_truncated else "complete",
        bounded=True,
        truncated=any_truncated,
    )
    holds = _evidence(
        subject=subject,
        record_kind="retention_declared_hold_projection",
        role="supporting",
        evidence_kind="internal_fact",
        projection={
            "expected_holds": RETENTION_EXPECTED_HOLDS,
            "missing_hold_ids": analysis.missing_hold_ids,
        },
        facts=(
            AnalysisFact(
                "expected_holds",
                sum(len(item) for item in RETENTION_EXPECTED_HOLDS.values()),
                "count",
            ),
            AnalysisFact("missing_holds", len(analysis.missing_hold_ids), "count"),
            AnalysisFact("missing_hold_ids_json", canonical_json(analysis.missing_hold_ids)),
        ),
    )
    pagination = _evidence(
        subject=subject,
        record_kind="retention_pagination_projection",
        role="supporting",
        evidence_kind="internal_fact",
        projection={
            "pagination_valid": pagination_valid,
            "truncated_stores": tuple(item.store for item in analysis.stores if item.truncated),
        },
        facts=(
            AnalysisFact("pagination_valid", pagination_valid),
            AnalysisFact("truncated_stores", sum(item.truncated for item in analysis.stores), "count"),
        ),
    )
    counterevidence = _evidence(
        subject=subject,
        record_kind="retention_gap_counterevidence",
        role="counterevidence",
        evidence_kind="internal_fact",
        projection={
            "non_ready_stores": tuple(item.store for item in analysis.stores if item.status != "ready"),
            "missing_hold_ids": analysis.missing_hold_ids,
            "pagination_valid": pagination_valid,
        },
        facts=(
            AnalysisFact("non_ready_stores", sum(item.status != "ready" for item in analysis.stores), "count"),
            AnalysisFact("missing_holds", len(analysis.missing_hold_ids), "count"),
            AnalysisFact("invalid_pagination", 0 if pagination_valid else 1, "count"),
        ),
    )
    requirement_results = (
        AnalysisRequirementEvaluation(
            "dry_run_non_deletion_contract_resolved",
            "satisfied",
            (contract.evidence_id,),
            "retention_planner_is_explicitly_dry_run_without_deletion_support",
        ),
        AnalysisRequirementEvaluation(
            "owner_schemas_and_dependencies_resolved",
            "satisfied" if all_ready else "missing",
            (owners.evidence_id,) if all_ready else (),
            "all_retention_owner_schemas_and_dependencies_resolved"
            if all_ready
            else "one_or_more_retention_owners_are_not_ready",
        ),
        AnalysisRequirementEvaluation(
            "declared_durable_holds_observed",
            "satisfied" if not analysis.missing_hold_ids else "missing",
            (holds.evidence_id,) if not analysis.missing_hold_ids else (),
            "all_declared_durable_hold_classes_are_observed"
            if not analysis.missing_hold_ids
            else "one_or_more_declared_durable_hold_classes_are_missing",
        ),
        AnalysisRequirementEvaluation(
            "bounded_pagination_is_explicit",
            "satisfied" if pagination_valid else "missing",
            (pagination.evidence_id,) if pagination_valid else (),
            "every_truncated_page_has_an_exact_resume_cursor"
            if pagination_valid
            else "retention_pagination_contract_is_inconsistent",
        ),
        AnalysisRequirementEvaluation(
            "retention_gap_counterevidence_evaluated",
            "satisfied",
            (counterevidence.evidence_id,),
            "blocked_missing_and_pagination_gap_controls_were_evaluated",
        ),
        AnalysisRequirementEvaluation(
            "isolated_retention_safety_experiment_result",
            "missing",
            (),
            "no_linked_isolated_retention_safety_receipt",
        ),
    )
    evidence_by_id = {
        item.evidence_id: item
        for item in (contract, owners, holds, pagination, counterevidence)
    }
    referenced = {
        evidence_id for item in requirement_results for evidence_id in item.evidence_ids
    }
    evidence = tuple(evidence_by_id[item] for item in sorted(referenced))
    question_ready = all(item.status == "satisfied" for item in requirement_results[:4])
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "retention-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
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
        requirements=requirement_results,
        observation_status="confirmed" if question_ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready" if question_ready else "abstained",
        decision_readiness="experiment_required" if question_ready else "abstained",
        decision=None,
        decision_reason="decision_evidence_incomplete" if question_ready else "question_evidence_incomplete",
        counterevidence_status="evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions) if question_ready else (),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return (spec,), (evaluation,)


def parse_code_retention_analysis_payload(payload: Mapping[str, object]) -> CodeRetentionAnalysis:
    expected = {"schema", *(item.name for item in fields(CodeRetentionAnalysis))}
    if set(payload) != expected or payload.get("schema") != CODE_RETENTION_ANALYSIS_SCHEMA:
        raise ValueError("retention analysis payload shape is invalid")
    raw_stores = payload.get("stores")
    if not isinstance(raw_stores, Sequence) or isinstance(raw_stores, (str, bytes, bytearray)):
        raise ValueError("retention analysis stores are invalid")
    stores: list[RetentionStoreObservation] = []
    store_fields = {item.name for item in fields(RetentionStoreObservation)}
    for raw in raw_stores:
        if not isinstance(raw, Mapping) or set(raw) != store_fields:
            raise ValueError("retention store payload shape is invalid")
        stores.append(
            RetentionStoreObservation(
                store=cast(AnyRetentionStore, raw.get("store")),
                status=cast(AnyRetentionStatus, raw.get("status")),
                schema_version=_optional_non_negative("retention store schema", raw.get("schema_version")),
                eligible_rows=_non_negative("eligible rows", raw.get("eligible_rows")),
                eligible_bytes=_non_negative("eligible bytes", raw.get("eligible_bytes")),
                protected_rows=_non_negative("protected rows", raw.get("protected_rows")),
                protected_bytes=_non_negative("protected bytes", raw.get("protected_bytes")),
                item_count=_non_negative("retention item count", raw.get("item_count")),
                hold_names=_texts("retention hold name", raw.get("hold_names")),
                hold_rows=_non_negative("retention hold rows", raw.get("hold_rows")),
                next_after=_optional_non_negative("retention next cursor", raw.get("next_after")),
                truncated=cast(bool, raw.get("truncated")),
                detail_code=_optional_text("retention detail code", raw.get("detail_code"), 256),
            )
        )
    status = payload.get("status")
    observation = payload.get("observation")
    return CodeRetentionAnalysis(
        analysis_id=_required_text("retention analysis id", payload.get("analysis_id"), 256),
        status=cast(Literal["ready", "abstained"], status),
        reason=_optional_text("retention analysis reason", payload.get("reason"), 256),
        source_version=_required_text("retention source version", payload.get("source_version"), 256),
        policy_id=_required_text("retention policy", payload.get("policy_id"), 256),
        stores=tuple(stores),
        missing_hold_ids=_texts("missing retention hold", payload.get("missing_hold_ids")),
        observation=cast(
            Literal["declared_holds_resolved", "retention_gap_observed"] | None,
            observation,
        ),
        authority=cast(Literal["advisory"], payload.get("authority")),
        mutation_authority=cast(Literal[False], payload.get("mutation_authority")),
    )


__all__ = [
    "CODE_RETENTION_ANALYSIS_SCHEMA",
    "CODE_RETENTION_POLICY",
    "RETENTION_EXPECTED_HOLDS",
    "RETENTION_HOLD_QUESTION",
    "CodeRetentionAnalysis",
    "CodeRetentionResolutionError",
    "RetentionStoreObservation",
    "abstained_code_retention",
    "analyze_code_retention",
    "parse_code_retention_analysis_payload",
    "resolve_code_retention",
    "retention_questions",
]
