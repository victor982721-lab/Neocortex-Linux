"""Resolve Code-review observations into the general question contract."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal, Protocol, cast

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
from .code_review_actionability import (
    CODE_REVIEW_QUESTION_ID,
    CODE_REVIEW_QUESTION_VERSION,
)
from .code_schema import CODE_SCHEMA_VERSION


class _DiagnosticEvidence(Protocol):
    diagnostic_id: int
    code: Literal["high_complexity", "long_function"]
    value: int
    threshold: int | None
    source: str
    tool_name: str
    tool_version: str
    confirmed: bool
    confidence: float


class _FindingEvidence(Protocol):
    finding_id: str
    hotspot_id: str
    rank: int
    path: str
    symbol: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    file_xxh3_128: str | None
    file_xxh3_64_guard: str | None
    diagnostics: tuple[_DiagnosticEvidence, ...]


class _SnapshotEvidence(Protocol):
    processing_signature: str
    freshness: str


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
        snapshot_freshness=cast(
            Literal["current", "publication_only", "unknown"],
            snapshot.freshness,
        ),
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
    findings: tuple[_FindingEvidence, ...],
    snapshot: _SnapshotEvidence,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Rebuild the canonical projection from already-resolved review findings."""

    evaluations = tuple(_evaluation(finding, snapshot) for finding in findings)
    specs = (STRUCTURAL_HOTSPOT_QUESTION,) if evaluations else ()
    validate_analysis_question_set(specs, evaluations)
    return specs, evaluations


def resolve_code_review_questions(
    connection: sqlite3.Connection,
    findings: tuple[_FindingEvidence, ...],
    snapshot: _SnapshotEvidence,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Resolve every diagnostic pointer against the read-only Code connection."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("Code-review evidence resolver requires a SQLite connection")
    for finding in findings:
        for diagnostic in finding.diagnostics:
            _validate_resolved_diagnostic(connection, finding, diagnostic)
    return expected_code_review_questions(findings, snapshot)


__all__ = [
    "STRUCTURAL_HOTSPOT_QUESTION",
    "CodeReviewEvidenceResolutionError",
    "expected_code_review_questions",
    "resolve_code_review_questions",
]
