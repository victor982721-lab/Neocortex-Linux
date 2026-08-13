"""Independent-label calibration and anti-Goodhart evidence for Code analysis.

The analyzer already reports cost and explicitly says its effectiveness is not
calibrated.  This vertical turns reviewed fixtures into a typed calibration
corpus while preserving their epistemic status.  Provisional labels are useful
as development counterexamples but can never establish precision, recall or a
human-decision rate.

No detector imports its own expected output from this module.  A label source
must declare who/what established the label, whether it is independent of the
detector, and which partition (calibration, negative control, or holdout) it
belongs to.  Anti-Goodhart checks are registered source contracts bound to
exact tests; a passing test receipt may later satisfy them, but the declaration
alone does not.
"""

from __future__ import annotations

import json
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
from .code_invariant_assurance_analysis import CodeInvariantAssuranceAnalysis
from .code_invariant_contracts import RUNTIME_SCENARIOS

CODE_ANALYZER_CALIBRATION_SCHEMA = "neocortex.code-analyzer-calibration/v1"
CODE_ANALYZER_CALIBRATION_POLICY = "independent-labels-holdout-and-antigoodhart-v1"
CODE_ANALYZER_CALIBRATION_MAX_LABELS = 2_000
CODE_ANALYZER_CALIBRATION_MAX_CORPORA = 32

ANALYZER_CALIBRATION_EVIDENCE_QUESTION = AnalysisQuestionSpec(
    question_id="analyzer.calibration_evidence_is_independent_and_antigoodhart_resistant",
    version="v1",
    subject_kinds=("analyzer",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "versioned_calibration_corpus_observed",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "independent_holdout_labels_linked",
            "decision",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "antigoodhart_controls_evaluated",
            "decision",
            "counterevidence",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "human_decision_attention_result_linked",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "independent_labels_and_controls_support_useful_analyzer_calibration",
        "provisional_or_detector_coupled_labels_overstate_analyzer_effectiveness",
    ),
    counterevidence_rules=(
        "provisional_labels_never_establish_precision_recall_or_decision_utility",
        "registered_but_unexecuted_antigoodhart_controls_are_missing_evidence",
        "a_passing_internal_control_does_not_replace_an_independent_holdout",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "link_review_task_outcomes_without_copying_human_decisions",
            "characterization",
            "Link existing human ReviewTask outcomes by stable pointers without copying authority.",
        ),
        AnalysisNextActionSpec(
            "execute_registered_antigoodhart_controls",
            "experiment",
            "Execute every registered rename, move, wrapper, call-spelling, and dilution control.",
        ),
        AnalysisNextActionSpec(
            "run_seeded_holdout_and_negative_control_calibration",
            "experiment",
            "Run an independently labelled seeded holdout and acceptable negative controls.",
        ),
    ),
)

LabelStatus = Literal["independent_human_validated", "provisional_not_human_validated"]
Partition = Literal["calibration", "negative_control", "holdout"]
ExpectedDisposition = Literal[
    "confirmed_problem",
    "acceptable_structure",
    "review_candidate",
    "demonstrably_used",
    "external_contract",
]

_LIMITATIONS = (
    "provisional_labels_never_establish_precision_or_recall",
    "source_fixtures_may_be_stale_relative_to_current_code",
    "registered_antigoodhart_test_is_not_an_execution_receipt",
    "passing_antigoodhart_checks_does_not_establish_real_world_recall",
    "human_decision_time_and_escaped_defects_are_not_yet_linked",
    "calibration_has_no_change_or_mutation_authority",
)


def _required(label: str, value: object, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _texts(label: str, values: object, *, sorted_values: bool = False) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, item) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if sorted_values and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class AnalyzerCalibrationLabel:
    label_id: str
    corpus_id: str
    subject_key: str
    partition: Partition
    expected_disposition: ExpectedDisposition
    label_status: LabelStatus
    label_source: str
    detector_independent: bool
    rationale: str
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for name, value in (
            ("calibration label id", self.label_id),
            ("calibration corpus id", self.corpus_id),
            ("calibration subject key", self.subject_key),
            ("calibration label source", self.label_source),
            ("calibration rationale", self.rationale),
        ):
            _required(name, value, 4_096)
        if self.partition not in {"calibration", "negative_control", "holdout"}:
            raise ValueError("calibration label partition is invalid")
        if self.expected_disposition not in {
            "confirmed_problem",
            "acceptable_structure",
            "review_candidate",
            "demonstrably_used",
            "external_contract",
        }:
            raise ValueError("calibration expected disposition is invalid")
        if self.label_status not in {
            "independent_human_validated",
            "provisional_not_human_validated",
        }:
            raise ValueError("calibration label status is invalid")
        if not isinstance(self.detector_independent, bool):
            raise ValueError("calibration independence flag must be boolean")
        if self.label_status == "independent_human_validated" and not self.detector_independent:
            raise ValueError("human-validated calibration labels must be detector-independent")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("calibration labels must remain advisory and non-mutating")
        expected = analysis_identity(
            "analyzer-calibration-label-v1",
            {key: value for key, value in asdict(self).items() if key != "label_id"},
        )
        if self.label_id != expected:
            raise ValueError("calibration label identity is invalid")


@dataclass(frozen=True, slots=True)
class AnalyzerCalibrationCorpus:
    corpus_id: str
    source_path: str
    source_schema: str
    source_digest: str
    label_status: LabelStatus
    partition: Partition
    labels: tuple[AnalyzerCalibrationLabel, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("calibration corpus id", self.corpus_id),
            ("calibration source path", self.source_path),
            ("calibration source schema", self.source_schema),
            ("calibration source digest", self.source_digest),
        ):
            _required(label, value)
        if self.label_status not in {
            "independent_human_validated",
            "provisional_not_human_validated",
        }:
            raise ValueError("calibration corpus label status is invalid")
        if self.partition not in {"calibration", "negative_control", "holdout"}:
            raise ValueError("calibration corpus partition is invalid")
        if not self.labels or len(self.labels) > CODE_ANALYZER_CALIBRATION_MAX_LABELS:
            raise ValueError("calibration corpus label count is invalid")
        if any(
            item.corpus_id != self.corpus_id
            or item.label_status != self.label_status
            or item.partition != self.partition
            for item in self.labels
        ):
            raise ValueError("calibration corpus labels disagree with their envelope")
        if len({item.subject_key for item in self.labels}) != len(self.labels):
            raise ValueError("calibration corpus subject labels repeat")
        _texts("calibration corpus limitation", self.limitations)
        if not self.limitations:
            raise ValueError("calibration corpus requires explicit limitations")


@dataclass(frozen=True, slots=True)
class AntiGoodhartControl:
    control_id: str
    transformation: Literal[
        "rename",
        "move",
        "wrapper",
        "metric_dilution",
        "call_spelling",
    ]
    test_nodeid: str
    status: Literal["passed", "failed", "not_observed"]
    provider_run_id: int | None
    limitation: str

    def __post_init__(self) -> None:
        _required("anti-Goodhart control id", self.control_id, 256)
        _required("anti-Goodhart test nodeid", self.test_nodeid, 16_384)
        _required("anti-Goodhart limitation", self.limitation, 512)
        if self.transformation not in {
            "rename",
            "move",
            "wrapper",
            "metric_dilution",
            "call_spelling",
        }:
            raise ValueError("anti-Goodhart transformation is invalid")
        if self.status not in {"passed", "failed", "not_observed"}:
            raise ValueError("anti-Goodhart control status is invalid")
        if (self.status != "not_observed") != (self.provider_run_id is not None):
            raise ValueError("observed anti-Goodhart control requires a provider run")


_ANTI_GOODHART_DECLARATIONS = (
    (
        "call_spelling",
        "analyzer.hotspot_name_path_call_invariance",
    ),
    ("move", "analyzer.hotspot_name_path_call_invariance"),
    ("rename", "analyzer.hotspot_name_path_call_invariance"),
    ("wrapper", "analyzer.hotspot_name_path_call_invariance"),
)


@dataclass(frozen=True, slots=True)
class CodeAnalyzerCalibrationAnalysis:
    analysis_id: str
    status: Literal["calibrated", "provisional", "not_established", "abstained"]
    reason: str | None
    policy_id: str
    source_version: str
    corpora: tuple[AnalyzerCalibrationCorpus, ...]
    labels_total: int
    independent_labels: int
    provisional_labels: int
    calibration_labels: int
    negative_control_labels: int
    holdout_labels: int
    anti_goodhart_controls: tuple[AntiGoodhartControl, ...]
    anti_goodhart_passed: int
    anti_goodhart_failed: int
    anti_goodhart_not_observed: int
    precision_at_k: float | None
    recall: float | None
    finding_to_decision_rate: float | None
    decisions_per_attention_minute: float | None
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("analyzer calibration id", self.analysis_id)
        _required("analyzer calibration source version", self.source_version)
        if self.status not in {"calibrated", "provisional", "not_established", "abstained"}:
            raise ValueError("analyzer calibration status is invalid")
        if self.policy_id != CODE_ANALYZER_CALIBRATION_POLICY:
            raise ValueError("analyzer calibration policy is invalid")
        if len(self.corpora) > CODE_ANALYZER_CALIBRATION_MAX_CORPORA or any(
            not isinstance(item, AnalyzerCalibrationCorpus) for item in self.corpora
        ):
            raise ValueError("analyzer calibration corpora are invalid")
        labels = tuple(label for corpus in self.corpora for label in corpus.labels)
        for name, value in (
            ("total labels", self.labels_total),
            ("independent labels", self.independent_labels),
            ("provisional labels", self.provisional_labels),
            ("calibration labels", self.calibration_labels),
            ("negative-control labels", self.negative_control_labels),
            ("holdout labels", self.holdout_labels),
            ("anti-Goodhart passed", self.anti_goodhart_passed),
            ("anti-Goodhart failed", self.anti_goodhart_failed),
            ("anti-Goodhart not observed", self.anti_goodhart_not_observed),
        ):
            _nonnegative(name, value)
        if (
            self.labels_total != len(labels)
            or self.independent_labels
            != sum(item.label_status == "independent_human_validated" for item in labels)
            or self.provisional_labels
            != sum(item.label_status == "provisional_not_human_validated" for item in labels)
            or self.calibration_labels != sum(item.partition == "calibration" for item in labels)
            or self.negative_control_labels
            != sum(item.partition == "negative_control" for item in labels)
            or self.holdout_labels != sum(item.partition == "holdout" for item in labels)
        ):
            raise ValueError("analyzer calibration label counts are not derived")
        if (
            self.anti_goodhart_passed,
            self.anti_goodhart_failed,
            self.anti_goodhart_not_observed,
        ) != (
            sum(item.status == "passed" for item in self.anti_goodhart_controls),
            sum(item.status == "failed" for item in self.anti_goodhart_controls),
            sum(item.status == "not_observed" for item in self.anti_goodhart_controls),
        ):
            raise ValueError("anti-Goodhart counts are not derived")
        metrics = (
            self.precision_at_k,
            self.recall,
            self.finding_to_decision_rate,
            self.decisions_per_attention_minute,
        )
        if self.status != "calibrated" and any(item is not None for item in metrics):
            raise ValueError("uncalibrated analyzer cannot publish effectiveness metrics")
        if self.status == "calibrated":
            if (
                self.independent_labels == 0
                or self.holdout_labels == 0
                or self.anti_goodhart_not_observed
                or self.anti_goodhart_failed
                or any(item is None for item in metrics)
            ):
                raise ValueError("calibrated analyzer lacks independent holdout evidence")
        expected_status = (
            "not_established"
            if not labels
            else "calibrated"
            if self.independent_labels
            and self.holdout_labels
            and all(item is not None for item in metrics)
            else "provisional"
        )
        if self.status != expected_status:
            raise ValueError("analyzer calibration status is not derived from evidence")
        if self.status == "not_established":
            _required("analyzer calibration reason", self.reason, 256)
        elif self.reason is not None:
            raise ValueError("observed calibration cannot carry an abstention reason")
        if self.limitations != _LIMITATIONS:
            raise ValueError("analyzer calibration limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("analyzer calibration must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-analyzer-calibration-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("analyzer calibration identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_ANALYZER_CALIBRATION_SCHEMA, **asdict(self)}


def _source_digest(path: Path, content: bytes) -> str:
    from .semantic_models import fingerprint_bytes

    observed = fingerprint_bytes(content)
    return f"path:{path.as_posix()}:xxh3_128:{observed.xxh3_128}:xxh3_64:{observed.xxh3_64_guard}"


def _label(
    *,
    corpus_id: str,
    subject_key: str,
    partition: Partition,
    disposition: ExpectedDisposition,
    status: LabelStatus,
    source: str,
    independent: bool,
    rationale: str,
) -> AnalyzerCalibrationLabel:
    values: dict[str, object] = {
        "corpus_id": corpus_id,
        "subject_key": subject_key,
        "partition": partition,
        "expected_disposition": disposition,
        "label_status": status,
        "label_source": source,
        "detector_independent": independent,
        "rationale": rationale,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return AnalyzerCalibrationLabel(
        label_id=analysis_identity("analyzer-calibration-label-v1", values),
        **values,  # type: ignore[arg-type]
    )


def _load_probable_dead_fixture(path: Path) -> AnalyzerCalibrationCorpus:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema") != (
        "neocortex-probable-dead-calibration/v1"
    ):
        raise ValueError("probable-dead calibration fixture schema is invalid")
    source = payload.get("source")
    labels = payload.get("labels")
    if not isinstance(source, Mapping) or not isinstance(labels, list):
        raise ValueError("probable-dead calibration fixture is malformed")
    status = source.get("ground_truth_status")
    if status != "provisional_not_human_validated":
        raise ValueError("probable-dead fixture label status changed unexpectedly")
    corpus_id = "probable-dead-rc11-sample-v1"
    result: list[AnalyzerCalibrationLabel] = []
    disposition_map: dict[str, ExpectedDisposition] = {
        "demonstrably_used": "demonstrably_used",
        "external_contract": "external_contract",
        "review_candidate": "review_candidate",
    }
    for raw_label in labels:
        if not isinstance(raw_label, Mapping):
            raise ValueError("probable-dead calibration label is malformed")
        classification = raw_label.get("classification")
        path_value = raw_label.get("path")
        symbol = raw_label.get("symbol")
        rationale = raw_label.get("rationale", raw_label.get("evidence"))
        if (
            classification not in disposition_map
            or not isinstance(path_value, str)
            or not isinstance(symbol, str)
            or not isinstance(rationale, str)
        ):
            raise ValueError("probable-dead calibration label values are invalid")
        result.append(
            _label(
                corpus_id=corpus_id,
                subject_key=f"{path_value}::{symbol}",
                partition="calibration",
                disposition=disposition_map[str(classification)],
                status="provisional_not_human_validated",
                source="rc11_manual_review_not_human_validated",
                independent=False,
                rationale=rationale,
            )
        )
    return AnalyzerCalibrationCorpus(
        corpus_id,
        path.as_posix(),
        str(payload["schema"]),
        _source_digest(path, raw),
        "provisional_not_human_validated",
        "calibration",
        tuple(result),
        (
            "fixture_explicitly_declares_provisional_not_human_validated",
            "same_development_lineage_may_have_influenced_detector_suppression",
        ),
    )


def _anti_goodhart_controls(
    assurance: CodeInvariantAssuranceAnalysis | None,
) -> tuple[AntiGoodhartControl, ...]:
    scenario_by_id = {item.scenario_id: item for item in RUNTIME_SCENARIOS}
    outcome_by_scenario: dict[str, tuple[str, int | None]] = {}
    if assurance is not None and assurance.status != "abstained":
        for observation in assurance.observations:
            for outcome in observation.scenarios:
                outcome_by_scenario[outcome.scenario_id] = (
                    outcome.status,
                    outcome.provider_run_id,
                )
    controls: list[AntiGoodhartControl] = []
    for transformation, scenario_id in _ANTI_GOODHART_DECLARATIONS:
        scenario = scenario_by_id[scenario_id]
        raw_status, run_id = outcome_by_scenario.get(scenario_id, ("not_selected", None))
        status: Literal["passed", "failed", "not_observed"] = (
            "passed"
            if raw_status == "passed"
            else "failed"
            if raw_status in {"failed", "skipped"}
            else "not_observed"
        )
        controls.append(
            AntiGoodhartControl(
                control_id=f"antigoodhart:{transformation}:v1",
                transformation=cast(Any, transformation),
                test_nodeid=scenario.test_nodeid,
                status=status,
                provider_run_id=run_id if status != "not_observed" else None,
                limitation="one_declared_metamorphic_scenario_not_general_detector_invariance",
            )
        )
    # metric dilution remains an explicit unimplemented control rather than a
    # claimed property hidden behind another scenario.
    controls.append(
        AntiGoodhartControl(
            "antigoodhart:metric_dilution:v1",
            "metric_dilution",
            (
                "tests/test_code_analyzer_calibration.py::"
                "test_metric_dilution_control_remains_explicit_until_executed"
            ),
            "not_observed",
            None,
            "control_is_registered_but_not_yet_linked_to_trusted_deep_receipt",
        )
    )
    return tuple(sorted(controls, key=lambda item: item.control_id))


def analyze_code_analyzer_calibration(
    repository_root: Path,
    *,
    source_version: str,
    invariant_assurance: CodeInvariantAssuranceAnalysis | None = None,
) -> CodeAnalyzerCalibrationAnalysis:
    fixture = Path(repository_root) / "tests/fixtures/code_review/rc11_probable_dead_sample_v1.json"
    corpora: tuple[AnalyzerCalibrationCorpus, ...]
    try:
        corpora = (_load_probable_dead_fixture(fixture),)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        corpora = ()
    labels = tuple(label for corpus in corpora for label in corpus.labels)
    controls = _anti_goodhart_controls(invariant_assurance)
    status: Literal["calibrated", "provisional", "not_established", "abstained"] = (
        "provisional" if labels else "not_established"
    )
    values: dict[str, object] = {
        "status": status,
        "reason": None if labels else "calibration_corpus_unavailable",
        "policy_id": CODE_ANALYZER_CALIBRATION_POLICY,
        "source_version": _required("analyzer calibration source version", source_version),
        "corpora": corpora,
        "labels_total": len(labels),
        "independent_labels": sum(
            item.label_status == "independent_human_validated" for item in labels
        ),
        "provisional_labels": sum(
            item.label_status == "provisional_not_human_validated" for item in labels
        ),
        "calibration_labels": sum(item.partition == "calibration" for item in labels),
        "negative_control_labels": sum(item.partition == "negative_control" for item in labels),
        "holdout_labels": sum(item.partition == "holdout" for item in labels),
        "anti_goodhart_controls": controls,
        "anti_goodhart_passed": sum(item.status == "passed" for item in controls),
        "anti_goodhart_failed": sum(item.status == "failed" for item in controls),
        "anti_goodhart_not_observed": sum(item.status == "not_observed" for item in controls),
        "precision_at_k": None,
        "recall": None,
        "finding_to_decision_rate": None,
        "decisions_per_attention_minute": None,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    identity_values = dict(values)
    identity_values["corpora"] = tuple(asdict(item) for item in corpora)
    identity_values["anti_goodhart_controls"] = tuple(asdict(item) for item in controls)
    return CodeAnalyzerCalibrationAnalysis(
        analysis_id=analysis_identity("code-analyzer-calibration-v1", identity_values),
        **values,  # type: ignore[arg-type]
    )


def analyzer_calibration_questions(
    analysis: CodeAnalyzerCalibrationAnalysis,
    *,
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Project the calibration corpus without promoting provisional labels."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("analyzer calibration rank offset must be non-negative")
    spec = ANALYZER_CALIBRATION_EVIDENCE_QUESTION
    subject = AnalysisSubjectRef(
        subject_kind="analyzer",
        subject_key="analyzer:code-self-analysis",
        display_name="Code self-analysis calibration evidence",
        source_owner_id="code",
        snapshot_id=analysis.analysis_id,
        snapshot_freshness="unknown",
        revision_id=analysis.source_version,
    )
    corpus_evidence: AnalysisEvidenceRef | None = None
    if analysis.corpora:
        corpus_evidence = AnalysisEvidenceRef(
            evidence_id=analysis_identity(
                "analyzer-calibration-corpus-evidence-v1",
                {
                    "analysis": analysis.analysis_id,
                    "corpora": tuple(item.corpus_id for item in analysis.corpora),
                },
            ),
            subject_key=subject.subject_key,
            role="supporting",
            evidence_kind="contract",
            source_owner_id="code",
            producer_id="code-analyzer-calibration",
            producer_version=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_schema=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_record_kind="versioned_label_corpora",
            source_record_id=",".join(item.corpus_id for item in analysis.corpora),
            source_projection_digest=analysis.analysis_id,
            snapshot_id=subject.snapshot_id,
            revision_id=subject.revision_id,
            facts=(
                AnalysisFact("labels_total", analysis.labels_total, "count"),
                AnalysisFact("independent_labels", analysis.independent_labels, "count"),
                AnalysisFact("provisional_labels", analysis.provisional_labels, "count"),
                AnalysisFact("holdout_labels", analysis.holdout_labels, "count"),
                AnalysisFact("calibration_status", analysis.status),
            ),
            completeness="complete",
            bounded=True,
            truncated=False,
            resolver_id="code-analyzer-calibration-corpus-resolver",
            resolver_version="v1",
            limitations=(
                "label_status_is_preserved_from_each_source_corpus",
                "provisional_labels_are_not_independent_ground_truth",
            ),
        )
    independent_evidence: AnalysisEvidenceRef | None = None
    if analysis.independent_labels and analysis.holdout_labels:
        independent_evidence = AnalysisEvidenceRef(
            evidence_id=analysis_identity(
                "analyzer-independent-holdout-evidence-v1",
                {
                    "analysis": analysis.analysis_id,
                    "independent": analysis.independent_labels,
                    "holdout": analysis.holdout_labels,
                },
            ),
            subject_key=subject.subject_key,
            role="supporting",
            evidence_kind="internal_relation",
            source_owner_id="code",
            producer_id="code-analyzer-calibration",
            producer_version=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_schema=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_record_kind="independent_holdout_label_links",
            source_record_id=analysis.analysis_id,
            source_projection_digest=analysis.analysis_id,
            snapshot_id=subject.snapshot_id,
            revision_id=subject.revision_id,
            facts=(
                AnalysisFact("independent_labels", analysis.independent_labels, "count"),
                AnalysisFact("holdout_labels", analysis.holdout_labels, "count"),
            ),
            completeness="complete",
            bounded=True,
            truncated=False,
            resolver_id="code-analyzer-independent-label-resolver",
            resolver_version="v1",
            limitations=("human_decision_authority_remains_in_framework_review_tasks",),
        )
    controls_evidence: AnalysisEvidenceRef | None = None
    if analysis.anti_goodhart_controls and analysis.anti_goodhart_not_observed == 0:
        controls_evidence = AnalysisEvidenceRef(
            evidence_id=analysis_identity(
                "analyzer-antigoodhart-evidence-v1",
                {
                    "analysis": analysis.analysis_id,
                    "controls": tuple(asdict(item) for item in analysis.anti_goodhart_controls),
                },
            ),
            subject_key=subject.subject_key,
            role="counterevidence",
            evidence_kind="experiment_result",
            source_owner_id="code",
            producer_id="code-analyzer-calibration",
            producer_version=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_schema=CODE_ANALYZER_CALIBRATION_SCHEMA,
            source_record_kind="antigoodhart_control_receipts",
            source_record_id=analysis.analysis_id,
            source_projection_digest=analysis.analysis_id,
            snapshot_id=subject.snapshot_id,
            revision_id=subject.revision_id,
            facts=(
                AnalysisFact("controls_passed", analysis.anti_goodhart_passed, "count"),
                AnalysisFact("controls_failed", analysis.anti_goodhart_failed, "count"),
            ),
            completeness="complete",
            bounded=True,
            truncated=False,
            resolver_id="code-analyzer-antigoodhart-receipt-resolver",
            resolver_version="v1",
            limitations=("controls_do_not_establish_real_world_recall",),
        )
    evidence = tuple(
        item
        for item in (corpus_evidence, independent_evidence, controls_evidence)
        if item is not None
    )
    requirements = (
        AnalysisRequirementEvaluation(
            "versioned_calibration_corpus_observed",
            "satisfied" if corpus_evidence is not None else "missing",
            () if corpus_evidence is None else (corpus_evidence.evidence_id,),
            "versioned_calibration_corpus_resolved"
            if corpus_evidence is not None
            else analysis.reason or "calibration_corpus_unavailable",
        ),
        AnalysisRequirementEvaluation(
            "independent_holdout_labels_linked",
            "satisfied" if independent_evidence is not None else "missing",
            () if independent_evidence is None else (independent_evidence.evidence_id,),
            "independent_holdout_labels_resolved"
            if independent_evidence is not None
            else "independent_holdout_labels_missing",
        ),
        AnalysisRequirementEvaluation(
            "antigoodhart_controls_evaluated",
            "satisfied" if controls_evidence is not None else "not_evaluated",
            () if controls_evidence is None else (controls_evidence.evidence_id,),
            "all_registered_controls_have_execution_outcomes"
            if controls_evidence is not None
            else "one_or_more_registered_controls_lack_execution_outcomes",
        ),
        AnalysisRequirementEvaluation(
            "human_decision_attention_result_linked",
            "missing",
            (),
            "no_human_decision_attention_receipt_is_linked",
        ),
    )
    ready = corpus_evidence is not None
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "analyzer-calibration-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank_offset + 1,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed" if ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready" if ready else "abstained",
        decision_readiness="experiment_required" if ready else "abstained",
        decision=None,
        decision_reason="decision_evidence_incomplete" if ready else "question_evidence_incomplete",
        counterevidence_status="evaluated" if controls_evidence is not None else "not_evaluated",
        next_action_ids=tuple(item.action_id for item in spec.next_actions) if ready else (),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(spec, evaluation)
    return (spec,), (evaluation,)


def parse_code_analyzer_calibration_payload(
    payload: Mapping[str, object],
) -> CodeAnalyzerCalibrationAnalysis:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != CODE_ANALYZER_CALIBRATION_SCHEMA
    ):
        raise ValueError("analyzer calibration payload schema is invalid")
    expected = {field.name for field in fields(CodeAnalyzerCalibrationAnalysis)} | {"schema"}
    if set(payload) != expected:
        raise ValueError("analyzer calibration payload fields are invalid")
    raw_corpora = payload.get("corpora")
    if not isinstance(raw_corpora, Sequence) or isinstance(raw_corpora, (str, bytes, bytearray)):
        raise ValueError("analyzer calibration corpora are invalid")
    corpus_fields = {field.name for field in fields(AnalyzerCalibrationCorpus)}
    label_fields = {field.name for field in fields(AnalyzerCalibrationLabel)}
    corpora: list[AnalyzerCalibrationCorpus] = []
    for raw_corpus in raw_corpora:
        if not isinstance(raw_corpus, Mapping) or set(raw_corpus) != corpus_fields:
            raise ValueError("analyzer calibration corpus fields are invalid")
        corpus_values = dict(raw_corpus)
        raw_labels = corpus_values["labels"]
        if not isinstance(raw_labels, Sequence) or isinstance(raw_labels, (str, bytes, bytearray)):
            raise ValueError("analyzer calibration labels are invalid")
        labels = tuple(
            AnalyzerCalibrationLabel(**cast(Any, dict(item)))
            for item in raw_labels
            if isinstance(item, Mapping) and set(item) == label_fields
        )
        if len(labels) != len(raw_labels):
            raise ValueError("analyzer calibration label fields are invalid")
        corpus_values["labels"] = labels
        corpus_values["limitations"] = _texts(
            "calibration corpus limitation", corpus_values["limitations"]
        )
        corpora.append(AnalyzerCalibrationCorpus(**cast(Any, corpus_values)))
    raw_controls = payload.get("anti_goodhart_controls")
    if not isinstance(raw_controls, Sequence) or isinstance(raw_controls, (str, bytes, bytearray)):
        raise ValueError("anti-Goodhart controls are invalid")
    control_fields = {field.name for field in fields(AntiGoodhartControl)}
    controls = tuple(
        AntiGoodhartControl(**cast(Any, dict(item)))
        for item in raw_controls
        if isinstance(item, Mapping) and set(item) == control_fields
    )
    if len(controls) != len(raw_controls):
        raise ValueError("anti-Goodhart control fields are invalid")
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["corpora"] = tuple(corpora)
    values["anti_goodhart_controls"] = controls
    values["limitations"] = _texts("analyzer calibration limitation", values["limitations"])
    return CodeAnalyzerCalibrationAnalysis(**cast(Any, values))


__all__ = [
    "ANALYZER_CALIBRATION_EVIDENCE_QUESTION",
    "CODE_ANALYZER_CALIBRATION_MAX_CORPORA",
    "CODE_ANALYZER_CALIBRATION_MAX_LABELS",
    "CODE_ANALYZER_CALIBRATION_POLICY",
    "CODE_ANALYZER_CALIBRATION_SCHEMA",
    "AnalyzerCalibrationCorpus",
    "AnalyzerCalibrationLabel",
    "AntiGoodhartControl",
    "CodeAnalyzerCalibrationAnalysis",
    "analyze_code_analyzer_calibration",
    "analyzer_calibration_questions",
    "parse_code_analyzer_calibration_payload",
]
