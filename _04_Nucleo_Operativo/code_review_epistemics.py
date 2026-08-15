"""Resolve Code-review observations into the general question contract."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal, Protocol

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
from .code_assurance_analysis import CodeAssuranceAnalysis, assurance_questions
from .code_architecture_analysis import CodeArchitectureAnalysis
from .code_architecture_questions import architecture_questions
from .code_analyzer_calibration import (
    CodeAnalyzerCalibrationAnalysis,
    analyzer_calibration_questions,
)
from .code_analyzer_effectiveness import (
    CodeAnalyzerEffectivenessAnalysis,
    analyzer_effectiveness_questions,
)
from .code_capability_reachability_analysis import (
    CodeCapabilityReachabilityAnalysis,
    capability_reachability_questions,
)
from .code_change_evolution_analysis import (
    CodeChangeEvolutionAnalysis,
    expected_code_change_evolution_questions,
)
from .code_class_surface_analysis import (
    CodeClassSurfaceAnalysis,
    expected_class_surface_questions,
    read_code_class_surface_analysis,
)
from .code_interface_surface_analysis import (
    CodeInterfaceSurfaceAnalysis,
    interface_surface_questions,
)
from .code_invariant_assurance_analysis import (
    CodeInvariantAssuranceAnalysis,
    invariant_assurance_questions,
)
from .code_route_capability_analysis import (
    CodeRouteCapabilityAnalysis,
    route_capability_questions,
)
from .code_retention_analysis import CodeRetentionAnalysis, retention_questions
from .code_review_actionability import (
    CODE_REVIEW_QUESTION_ID,
    CODE_REVIEW_QUESTION_VERSION,
)
from .code_security_dependency_questions import security_dependency_questions
from .code_schema import CODE_SCHEMA_VERSION
from .code_supply_chain_analysis import CodeSupplyChainAnalysis
from .code_state_topology_analysis import (
    CodeStateTopologyAnalysis,
    state_topology_questions,
)
from .code_state_interaction_analysis import (
    CodeStateInteractionAnalysis,
    state_interaction_questions,
)
from .code_state_projection_analysis import (
    CodeStateProjectionAnalysis,
    state_projection_questions,
)


class _DiagnosticEvidence(Protocol):
    @property
    def diagnostic_id(self) -> int: ...

    @property
    def code(self) -> Literal["high_complexity", "long_function"]: ...

    @property
    def value(self) -> int: ...

    @property
    def threshold(self) -> int | None: ...

    @property
    def source(self) -> str: ...

    @property
    def tool_name(self) -> str: ...

    @property
    def tool_version(self) -> str: ...

    @property
    def confirmed(self) -> bool: ...

    @property
    def confidence(self) -> float: ...


class _FindingEvidence(Protocol):
    @property
    def finding_id(self) -> str: ...

    @property
    def hotspot_id(self) -> str: ...

    @property
    def rank(self) -> int: ...

    @property
    def path(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def start_line(self) -> int: ...

    @property
    def end_line(self) -> int: ...

    @property
    def start_column(self) -> int: ...

    @property
    def end_column(self) -> int: ...

    @property
    def start_byte(self) -> int: ...

    @property
    def end_byte(self) -> int: ...

    @property
    def file_xxh3_128(self) -> str | None: ...

    @property
    def file_xxh3_64_guard(self) -> str | None: ...

    @property
    def diagnostics(self) -> tuple[_DiagnosticEvidence, ...]: ...


class _SnapshotEvidence(Protocol):
    @property
    def processing_signature(self) -> str: ...

    @property
    def freshness(self) -> Literal["current", "publication_only", "unknown"]: ...


STRUCTURAL_HOTSPOT_QUESTION = AnalysisQuestionSpec(
    question_id=CODE_REVIEW_QUESTION_ID,
    version=CODE_REVIEW_QUESTION_VERSION,
    subject_kinds=("symbol",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "confirmed_structural_hotspot",
            "question",
            "supporting",
            ("internal_diagnostic",),
        ),
        AnalysisEvidenceRequirementSpec(
            "behavior_or_contract_problem_observed",
            "decision",
            "supporting",
            ("internal_fact", "contract", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "contract", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "discriminating_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "structural_hotspot_may_increase_maintenance_cost",
        "structure_may_be_intentional_or_cohesive",
    ),
    counterevidence_rules=(
        "intentional_or_cohesive_structure",
        "existing_assurance_for_observed_behavior",
        "change_cost_or_risk_exceeds_verified_benefit",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "characterize_exact_behavior_and_contracts",
            "characterization",
            "Characterize exact behavior and contracts without changing source.",
        ),
        AnalysisNextActionSpec(
            "seek_counterevidence",
            "counterevidence_search",
            "Seek explicit evidence that the observed structure is cohesive or intentional.",
        ),
        AnalysisNextActionSpec(
            "design_lowest_cost_discriminating_experiment",
            "experiment",
            "Design the lowest-cost experiment that distinguishes the competing hypotheses.",
        ),
    ),
)

_DIAGNOSTIC_RESOLVER_ID = "code.sqlite-diagnostic-resolver"
_DIAGNOSTIC_RESOLVER_VERSION = "v1"


class CodeReviewEvidenceResolutionError(ValueError):
    """The published projection no longer agrees with its Code source row."""


def _revision_id(finding: _FindingEvidence) -> str | None:
    if finding.file_xxh3_128 is None or finding.file_xxh3_64_guard is None:
        return None
    return f"xxh3_128:{finding.file_xxh3_128}:xxh3_64_guard:{finding.file_xxh3_64_guard}"


def _diagnostic_projection(
    finding: _FindingEvidence,
    diagnostic: _DiagnosticEvidence,
    snapshot: _SnapshotEvidence,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.processing_signature,
        "revision_id": _revision_id(finding),
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "start_column": finding.start_column,
        "end_column": finding.end_column,
        "code": diagnostic.code,
        "value": diagnostic.value,
        "threshold": diagnostic.threshold,
        "source": diagnostic.source,
        "tool_name": diagnostic.tool_name,
        "tool_version": diagnostic.tool_version,
        "confirmed": diagnostic.confirmed,
        "reported_confidence": diagnostic.confidence,
    }


def _validate_resolved_diagnostic(
    connection: sqlite3.Connection,
    finding: _FindingEvidence,
    diagnostic: _DiagnosticEvidence,
) -> None:
    row = connection.execute(
        """SELECT d.diagnostic_id,d.code,d.source,d.tool_name,d.tool_version,
        d.confirmed,d.confidence,d.start_line,d.start_column,d.end_line,d.end_column,
        d.start_byte,d.end_byte,d.metadata_json,v.analysis_status,v.language,
        v.generated,v.vendored,v.text_truncated,v.raw_xxh3_128,v.raw_xxh3_64_guard,
        f.current_path,f.status AS file_status
        FROM diagnostics d
        JOIN file_versions v ON v.version_id=d.version_id
        JOIN files f ON f.current_version_id=v.version_id
        WHERE d.diagnostic_id=? AND v.invalidated_ns IS NULL
          AND f.status='current'""",
        (diagnostic.diagnostic_id,),
    ).fetchone()
    if row is None:
        raise CodeReviewEvidenceResolutionError(
            "code-review diagnostic source record is not resolvable"
        )
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError) as exc:
        raise CodeReviewEvidenceResolutionError(
            "code-review diagnostic source metadata is invalid"
        ) from exc
    if not isinstance(metadata, dict):
        raise CodeReviewEvidenceResolutionError("code-review diagnostic source metadata is invalid")
    observed = (
        str(row["code"]),
        str(row["source"]),
        str(row["tool_name"]),
        str(row["tool_version"]),
        bool(row["confirmed"]),
        float(row["confidence"]),
        metadata.get("value"),
        metadata.get("threshold"),
        str(row["current_path"]),
        row["start_line"],
        row["start_column"],
        row["end_line"],
        row["end_column"],
        row["start_byte"],
        row["end_byte"],
        str(row["analysis_status"]),
        str(row["language"]),
        bool(row["generated"]),
        bool(row["vendored"]),
        bool(row["text_truncated"]),
        row["raw_xxh3_128"],
        row["raw_xxh3_64_guard"],
        str(row["file_status"]),
    )
    expected = (
        diagnostic.code,
        diagnostic.source,
        diagnostic.tool_name,
        diagnostic.tool_version,
        diagnostic.confirmed,
        diagnostic.confidence,
        diagnostic.value,
        diagnostic.threshold,
        finding.path,
        finding.start_line,
        finding.start_column,
        finding.end_line,
        finding.end_column,
        finding.start_byte,
        finding.end_byte,
        "complete",
        "python",
        False,
        False,
        False,
        finding.file_xxh3_128,
        finding.file_xxh3_64_guard,
        "current",
    )
    if observed != expected:
        raise CodeReviewEvidenceResolutionError(
            "code-review diagnostic projection disagrees with its source record"
        )


def _diagnostic_evidence(
    finding: _FindingEvidence,
    snapshot: _SnapshotEvidence,
) -> tuple[AnalysisEvidenceRef, ...]:
    revision_id = _revision_id(finding)
    evidence: list[AnalysisEvidenceRef] = []
    for diagnostic in finding.diagnostics:
        source_projection = _diagnostic_projection(finding, diagnostic, snapshot)
        source_projection_digest = analysis_identity(
            "code-source-projection-v1",
            source_projection,
        )
        evidence_id = analysis_identity(
            "code-diagnostic-evidence-v1",
            {
                "subject_key": finding.hotspot_id,
                "source_projection_digest": source_projection_digest,
                "code": diagnostic.code,
            },
        )
        evidence.append(
            AnalysisEvidenceRef(
                evidence_id=evidence_id,
                subject_key=finding.hotspot_id,
                role="supporting",
                evidence_kind="internal_diagnostic",
                source_owner_id="code",
                producer_id=diagnostic.tool_name,
                producer_version=diagnostic.tool_version,
                source_schema=f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
                source_record_kind="diagnostic",
                source_record_id=str(diagnostic.diagnostic_id),
                source_projection_digest=source_projection_digest,
                snapshot_id=snapshot.processing_signature,
                revision_id=revision_id,
                facts=(
                    AnalysisFact("code", diagnostic.code),
                    AnalysisFact("value", diagnostic.value),
                    AnalysisFact("threshold", diagnostic.threshold),
                    AnalysisFact("confirmed", diagnostic.confirmed),
                    AnalysisFact("reported_confidence", diagnostic.confidence, "ratio"),
                ),
                completeness="complete",
                bounded=False,
                truncated=False,
                resolver_id=_DIAGNOSTIC_RESOLVER_ID,
                resolver_version=_DIAGNOSTIC_RESOLVER_VERSION,
                limitations=("diagnostic_confirms_threshold_only",),
            )
        )
    return tuple(evidence)


def _evaluation(
    finding: _FindingEvidence,
    snapshot: _SnapshotEvidence,
) -> AnalysisQuestionEvaluation:
    evidence = _diagnostic_evidence(finding, snapshot)
    evidence_ids = tuple(item.evidence_id for item in evidence)
    subject = AnalysisSubjectRef(
        subject_kind="symbol",
        subject_key=finding.hotspot_id,
        display_name=finding.symbol,
        source_owner_id="code",
        snapshot_id=snapshot.processing_signature,
        snapshot_freshness=snapshot.freshness,
        revision_id=_revision_id(finding),
        location=AnalysisSourceLocation(
            finding.path,
            finding.start_line,
            finding.end_line,
            finding.start_column,
            finding.end_column,
        ),
    )
    return AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-question-evaluation-v1",
            {
                "finding_id": finding.finding_id,
                "snapshot": snapshot.processing_signature,
                "question_spec": analysis_question_spec_fingerprint(STRUCTURAL_HOTSPOT_QUESTION),
                "evidence_ids": evidence_ids,
            },
        ),
        question_id=STRUCTURAL_HOTSPOT_QUESTION.question_id,
        question_version=STRUCTURAL_HOTSPOT_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(STRUCTURAL_HOTSPOT_QUESTION),
        rank=finding.rank,
        subject=subject,
        evidence=evidence,
        requirements=(
            AnalysisRequirementEvaluation(
                "confirmed_structural_hotspot",
                "satisfied",
                evidence_ids,
                "linked_confirmed_threshold_diagnostics",
            ),
            AnalysisRequirementEvaluation(
                "behavior_or_contract_problem_observed",
                "missing",
                (),
                "no_behavior_or_contract_problem_evidence_linked",
            ),
            AnalysisRequirementEvaluation(
                "counterevidence_evaluated",
                "not_evaluated",
                (),
                "counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "discriminating_experiment_result",
                "missing",
                (),
                "no_discriminating_experiment_result_linked",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=STRUCTURAL_HOTSPOT_QUESTION.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in STRUCTURAL_HOTSPOT_QUESTION.next_actions),
        limitations=(
            "structural_threshold_does_not_prove_maintenance_harm",
            "source_record_projection_is_resolved_but_semantics_are_not",
            "human_decision_not_owned_by_code_analysis",
        ),
    )


def expected_code_review_questions(
    findings: Sequence[_FindingEvidence],
    snapshot: _SnapshotEvidence,
    class_surface: CodeClassSurfaceAnalysis,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Rebuild the canonical projection from already-resolved review findings."""

    hotspot_evaluations = tuple(_evaluation(finding, snapshot) for finding in findings)
    hotspot_specs = (STRUCTURAL_HOTSPOT_QUESTION,) if hotspot_evaluations else ()
    class_specs, class_evaluations = expected_class_surface_questions(
        class_surface,
        rank_offset=len(hotspot_evaluations),
    )
    specs = hotspot_specs + class_specs
    evaluations = hotspot_evaluations + class_evaluations
    validate_analysis_question_set(specs, evaluations)
    return specs, evaluations


def expected_integrated_code_review_questions(
    findings: Sequence[_FindingEvidence],
    snapshot: _SnapshotEvidence,
    class_surface: CodeClassSurfaceAnalysis,
    *,
    state_projection: CodeStateProjectionAnalysis,
    state_topology: CodeStateTopologyAnalysis,
    retention_analysis: CodeRetentionAnalysis,
    change_evolution: CodeChangeEvolutionAnalysis,
    architecture: CodeArchitectureAnalysis,
    assurance: CodeAssuranceAnalysis,
    supply_chain: CodeSupplyChainAnalysis,
    interface_surface: CodeInterfaceSurfaceAnalysis,
    capability_reachability: CodeCapabilityReachabilityAnalysis,
    analyzer_effectiveness: CodeAnalyzerEffectivenessAnalysis,
    state_interactions: CodeStateInteractionAnalysis,
    invariant_assurance: CodeInvariantAssuranceAnalysis,
    route_capabilities: CodeRouteCapabilityAnalysis,
    analyzer_calibration: CodeAnalyzerCalibrationAnalysis,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Rebuild every v19 question from its already-resolved owner projection."""

    base_specs, base_evaluations = expected_code_review_questions(
        findings,
        snapshot,
        class_surface,
    )
    specs = list(base_specs)
    evaluations = list(base_evaluations)

    architecture_specs, architecture_evaluations = architecture_questions(
        architecture,
        snapshot_id=snapshot.processing_signature,
        snapshot_freshness=snapshot.freshness,
        rank_offset=len(evaluations),
    )
    specs.extend(architecture_specs)
    evaluations.extend(architecture_evaluations)

    interface_specs, interface_evaluations = interface_surface_questions(
        interface_surface,
        snapshot_freshness=snapshot.freshness,
        rank_offset=len(evaluations),
    )
    specs.extend(interface_specs)
    evaluations.extend(interface_evaluations)

    projection_specs, projection_evaluations = state_projection_questions(
        state_projection,
        rank=len(evaluations) + 1,
    )
    specs.extend(projection_specs)
    evaluations.extend(projection_evaluations)

    topology_specs, topology_evaluations = state_topology_questions(
        state_topology,
        rank=len(evaluations) + 1,
    )
    specs.extend(topology_specs)
    evaluations.extend(topology_evaluations)

    retention_specs, retention_evaluations = retention_questions(
        retention_analysis,
        rank=len(evaluations) + 1,
    )
    specs.extend(retention_specs)
    evaluations.extend(retention_evaluations)

    interaction_specs, interaction_evaluations = state_interaction_questions(
        state_interactions,
        snapshot_id=snapshot.processing_signature,
        snapshot_freshness=snapshot.freshness,
        rank_offset=len(evaluations),
    )
    specs.extend(interaction_specs)
    evaluations.extend(interaction_evaluations)

    evolution_specs, evolution_evaluations = expected_code_change_evolution_questions(
        change_evolution,
        rank_offset=len(evaluations),
    )
    specs.extend(evolution_specs)
    evaluations.extend(evolution_evaluations)

    assurance_specs, assurance_evaluations = assurance_questions(
        assurance,
        rank_offset=len(evaluations),
    )
    specs.extend(assurance_specs)
    evaluations.extend(assurance_evaluations)

    invariant_specs, invariant_evaluations = invariant_assurance_questions(
        invariant_assurance,
        rank_offset=len(evaluations),
    )
    specs.extend(invariant_specs)
    evaluations.extend(invariant_evaluations)

    security_specs, security_evaluations = security_dependency_questions(
        supply_chain,
        snapshot_id=snapshot.processing_signature,
        snapshot_freshness=snapshot.freshness,
        rank_offset=len(evaluations),
    )
    specs.extend(security_specs)
    evaluations.extend(security_evaluations)

    capability_specs, capability_evaluations = capability_reachability_questions(
        capability_reachability,
        rank_offset=len(evaluations),
    )
    specs.extend(capability_specs)
    evaluations.extend(capability_evaluations)

    route_specs, route_evaluations = route_capability_questions(
        route_capabilities,
        rank_offset=len(evaluations),
    )
    specs.extend(route_specs)
    evaluations.extend(route_evaluations)

    effectiveness_specs, effectiveness_evaluations = analyzer_effectiveness_questions(
        analyzer_effectiveness,
        rank_offset=len(evaluations),
    )
    specs.extend(effectiveness_specs)
    evaluations.extend(effectiveness_evaluations)

    calibration_specs, calibration_evaluations = analyzer_calibration_questions(
        analyzer_calibration,
        rank_offset=len(evaluations),
    )
    specs.extend(calibration_specs)
    evaluations.extend(calibration_evaluations)

    frozen_specs = tuple(specs)
    frozen_evaluations = tuple(evaluations)
    validate_analysis_question_set(frozen_specs, frozen_evaluations)
    return frozen_specs, frozen_evaluations


def resolve_code_review_questions(
    connection: sqlite3.Connection,
    findings: Sequence[_FindingEvidence],
    snapshot: _SnapshotEvidence,
    *,
    class_limit: int,
) -> tuple[
    CodeClassSurfaceAnalysis,
    tuple[AnalysisQuestionSpec, ...],
    tuple[AnalysisQuestionEvaluation, ...],
]:
    """Resolve every diagnostic pointer against the read-only Code connection."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("Code-review evidence resolver requires a SQLite connection")
    for finding in findings:
        for diagnostic in finding.diagnostics:
            _validate_resolved_diagnostic(connection, finding, diagnostic)
    try:
        class_surface = read_code_class_surface_analysis(
            connection,
            snapshot_id=snapshot.processing_signature,
            snapshot_freshness=snapshot.freshness,
            limit=class_limit,
        )
    except ValueError as exc:
        raise CodeReviewEvidenceResolutionError(
            "code-review class-surface evidence is not resolvable"
        ) from exc
    specs, evaluations = expected_code_review_questions(
        findings,
        snapshot,
        class_surface,
    )
    return class_surface, specs, evaluations


__all__ = [
    "STRUCTURAL_HOTSPOT_QUESTION",
    "CodeReviewEvidenceResolutionError",
    "expected_code_review_questions",
    "expected_integrated_code_review_questions",
    "resolve_code_review_questions",
]
