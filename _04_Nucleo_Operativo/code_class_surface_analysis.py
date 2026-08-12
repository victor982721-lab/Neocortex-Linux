"""Resolved, observation-only class surface analysis for Code review.

The selection policy is an attention filter, not a defect detector.  Every
published observation is rebuilt from one current Python ``class`` symbol and
its direct, confirmed AST children.  Names and paths are identity/display
material only and never decide selection or actionability.
"""

from __future__ import annotations

import json
import sqlite3
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
    AnalysisSourceLocation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
)
from .code_schema import CODE_SCHEMA_VERSION
from .semantic_models import canonical_json

CODE_CLASS_SURFACE_SCHEMA = "neocortex.code-class-surface/v1"
CODE_CLASS_SURFACE_POLICY = "python-ast-direct-class-surface-v1"
CLASS_SPAN_LINES_ATTENTION_THRESHOLD = 500
CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD = 20

_CLASS_SURFACE_LIMITATIONS = (
    "selection_thresholds_are_provisional_attention_filters_not_calibrated_risk",
    "direct_ast_members_do_not_prove_runtime_surface_or_responsibility",
    "class_size_does_not_prove_low_cohesion_or_maintenance_harm",
    "role_ownership_consumers_churn_and_method_field_cohesion_are_not_observed",
    "rename_or_move_within_a_revision_changes_the_class_subject_identity",
    "published_code_snapshot_may_not_match_the_live_checkout",
)
_CLASS_EVALUATION_LIMITATIONS = (
    "class_surface_observation_does_not_prove_maintenance_harm",
    "architectural_role_and_cohesion_are_not_resolved",
    "source_record_projection_is_resolved_but_semantics_are_not",
    "human_decision_not_owned_by_code_analysis",
)

CLASS_SURFACE_QUESTION = AnalysisQuestionSpec(
    question_id="maintenance.class_surface_requires_change",
    version="v1",
    subject_kinds=("class",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "resolved_direct_class_surface",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "declared_architectural_role_observed",
            "decision",
            "supporting",
            ("internal_fact", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "cohesion_and_responsibility_evidence_observed",
            "decision",
            "supporting",
            ("internal_metric", "internal_relation", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "class_surface_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "internal_metric", "internal_relation", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "class_surface_characterization_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "class_surface_may_combine_accidental_responsibilities",
        "class_surface_may_be_cohesive_declarative_or_an_intentional_composition_role",
    ),
    counterevidence_rules=(
        "declared_composition_protocol_or_declarative_role",
        "methods_share_state_and_change_together_cohesively",
        "stable_low_churn_surface_with_bounded_consumers",
        "separation_cost_or_risk_exceeds_verified_benefit",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "resolve_declared_class_role_and_consumers",
            "characterization",
            "Resolve the declared role and real consumers without inferring them from names or paths.",
        ),
        AnalysisNextActionSpec(
            "measure_method_to_state_and_change_clusters",
            "counterevidence_search",
            "Measure method-to-state cohesion and historical change clusters, preserving contrary evidence.",
        ),
        AnalysisNextActionSpec(
            "characterize_class_clusters_without_source_changes",
            "experiment",
            "Cluster direct methods by state access and consumers without changing source, then compare the competing hypotheses.",
        ),
    ),
)


class CodeClassSurfaceEvidenceResolutionError(ValueError):
    """A class-surface projection cannot be reproduced from current Code facts."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _positive_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _non_negative_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _text_tuple(label: str, values: object, *, maximum: int = 512) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, value, maximum=maximum) for value in values)
    return result


def _revision_id(raw_xxh3_128: str, raw_xxh3_64_guard: str) -> str:
    return f"xxh3_128:{raw_xxh3_128}:xxh3_64_guard:{raw_xxh3_64_guard}"


def _selection_signals(span_lines: int, direct_methods: int) -> tuple[str, ...]:
    signals: list[str] = []
    if span_lines >= CLASS_SPAN_LINES_ATTENTION_THRESHOLD:
        signals.append("class_span_attention_threshold_met")
    if direct_methods >= CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD:
        signals.append("direct_method_attention_threshold_met")
    return tuple(signals)


def _class_subject_key(
    *,
    volume_id: str,
    physical_file_id: str,
    revision_id: str,
    qualified_name: str,
    start_byte: int,
) -> str:
    return analysis_identity(
        "code-class-subject-v1",
        {
            "volume_id": volume_id,
            "physical_file_id": physical_file_id,
            "revision_id": revision_id,
            "qualified_name": qualified_name,
            "start_byte": start_byte,
        },
    )


def _source_projection(values: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "snapshot_id",
        "revision_id",
        "symbol_id",
        "version_id",
        "file_id",
        "volume_id",
        "physical_file_id",
        "path",
        "name",
        "qualified_name",
        "start_line",
        "end_line",
        "start_column",
        "end_column",
        "start_byte",
        "end_byte",
        "span_lines",
        "direct_methods",
        "public_methods",
        "private_methods",
        "special_methods",
        "class_variables",
        "nested_classes",
        "bases",
        "decorators",
        "metadata_digest",
        "analyzer_id",
        "analyzer_version",
        "parser_kind",
        "selection_signals",
    )
    return {key: values[key] for key in keys}


@dataclass(frozen=True, slots=True)
class CodeClassSurfaceObservation:
    observation_id: str
    subject_key: str
    source_projection_digest: str
    rank: int
    snapshot_id: str
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    revision_id: str
    symbol_id: int
    version_id: int
    file_id: int
    volume_id: str
    physical_file_id: str
    path: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    span_lines: int
    direct_methods: int
    public_methods: int
    private_methods: int
    special_methods: int
    class_variables: int
    nested_classes: int
    bases: tuple[str, ...]
    decorators: tuple[str, ...]
    metadata_digest: str
    analyzer_id: str
    analyzer_version: str
    parser_kind: str
    selection_signals: tuple[str, ...]
    limitations: tuple[str, ...] = _CLASS_SURFACE_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("class observation id", self.observation_id, 256),
            ("class subject key", self.subject_key, 256),
            ("class projection digest", self.source_projection_digest, 256),
            ("class snapshot", self.snapshot_id, 2_048),
            ("class revision", self.revision_id, 512),
            ("class volume", self.volume_id, 512),
            ("class physical identity", self.physical_file_id, 512),
            ("class path", self.path, 32_768),
            ("class name", self.name, 1_024),
            ("class qualified name", self.qualified_name, 2_048),
            ("class metadata digest", self.metadata_digest, 256),
            ("class analyzer id", self.analyzer_id, 256),
            ("class analyzer version", self.analyzer_version, 256),
            ("class parser kind", self.parser_kind, 256),
        ):
            _required_text(label, value, maximum=maximum)
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("class snapshot freshness is invalid")
        for label, value in (
            ("class rank", self.rank),
            ("class symbol id", self.symbol_id),
            ("class version id", self.version_id),
            ("class file id", self.file_id),
            ("class start line", self.start_line),
            ("class end line", self.end_line),
            ("class span lines", self.span_lines),
        ):
            _positive_int(label, value)
        for label, value in (
            ("class start column", self.start_column),
            ("class end column", self.end_column),
            ("class start byte", self.start_byte),
            ("class end byte", self.end_byte),
            ("class direct methods", self.direct_methods),
            ("class public methods", self.public_methods),
            ("class private methods", self.private_methods),
            ("class special methods", self.special_methods),
            ("class variables", self.class_variables),
            ("nested classes", self.nested_classes),
        ):
            _non_negative_int(label, value)
        if self.end_line < self.start_line or self.end_byte < self.start_byte:
            raise ValueError("class source range is invalid")
        if self.span_lines != self.end_line - self.start_line + 1:
            raise ValueError("class span does not match its source range")
        if self.direct_methods != (
            self.public_methods + self.private_methods + self.special_methods
        ):
            raise ValueError("class method visibility partition is incomplete")
        _text_tuple("class base", self.bases)
        _text_tuple("class decorator", self.decorators)
        if self.selection_signals != _selection_signals(self.span_lines, self.direct_methods):
            raise ValueError("class selection signals do not match structural facts")
        if not self.selection_signals:
            raise ValueError("class observation does not meet the attention policy")
        if self.limitations != _CLASS_SURFACE_LIMITATIONS:
            raise ValueError("class observation limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("class observation must remain advisory and non-mutating")
        expected_subject = _class_subject_key(
            volume_id=self.volume_id,
            physical_file_id=self.physical_file_id,
            revision_id=self.revision_id,
            qualified_name=self.qualified_name,
            start_byte=self.start_byte,
        )
        if self.subject_key != expected_subject:
            raise ValueError("class subject identity is not derived from physical evidence")
        projection = _source_projection(asdict(self))
        expected_projection_digest = analysis_identity(
            "code-class-surface-source-projection-v1", projection
        )
        if self.source_projection_digest != expected_projection_digest:
            raise ValueError("class source projection digest is invalid")
        expected_observation_id = analysis_identity(
            "code-class-surface-observation-v1",
            {
                "subject_key": self.subject_key,
                "source_projection_digest": self.source_projection_digest,
                "policy": CODE_CLASS_SURFACE_POLICY,
            },
        )
        if self.observation_id != expected_observation_id:
            raise ValueError("class observation identity is invalid")


@dataclass(frozen=True, slots=True)
class CodeClassSurfaceAnalysis:
    analysis_id: str
    snapshot_id: str
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    status: Literal["ready"]
    reason: None
    policy_id: str
    span_lines_threshold: int
    direct_methods_threshold: int
    eligible_classes: int
    selected_classes: int
    returned_classes: int
    selection_truncated: bool
    observations: tuple[CodeClassSurfaceObservation, ...]
    limitations: tuple[str, ...] = _CLASS_SURFACE_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("class analysis id", self.analysis_id, maximum=256)
        _required_text("class analysis snapshot", self.snapshot_id, maximum=2_048)
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("class analysis freshness is invalid")
        if self.status != "ready" or self.reason is not None:
            raise ValueError("class analysis v1 is a ready resolved projection")
        if self.policy_id != CODE_CLASS_SURFACE_POLICY:
            raise ValueError("class analysis policy is invalid")
        if (
            self.span_lines_threshold != CLASS_SPAN_LINES_ATTENTION_THRESHOLD
            or self.direct_methods_threshold != CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD
        ):
            raise ValueError("class analysis thresholds are not canonical")
        for label, value in (
            ("eligible classes", self.eligible_classes),
            ("selected classes", self.selected_classes),
            ("returned classes", self.returned_classes),
        ):
            _non_negative_int(label, value)
        if not (
            self.eligible_classes
            >= self.selected_classes
            >= self.returned_classes
            == len(self.observations)
        ):
            raise ValueError("class analysis coverage counts are inconsistent")
        if self.selection_truncated != (self.returned_classes < self.selected_classes):
            raise ValueError("class analysis truncation disagrees with its counts")
        if tuple(item.rank for item in self.observations) != tuple(
            range(1, len(self.observations) + 1)
        ):
            raise ValueError("class observation ranks must be contiguous")
        if any(
            item.snapshot_id != self.snapshot_id
            or item.snapshot_freshness != self.snapshot_freshness
            for item in self.observations
        ):
            raise ValueError("class observations must share the analysis snapshot")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("class observation identities cannot repeat")
        if self.limitations != _CLASS_SURFACE_LIMITATIONS:
            raise ValueError("class analysis limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("class analysis must remain advisory and non-mutating")
        expected_id = analysis_identity(
            "code-class-surface-analysis-v1",
            {
                "snapshot_id": self.snapshot_id,
                "snapshot_freshness": self.snapshot_freshness,
                "policy": self.policy_id,
                "eligible_classes": self.eligible_classes,
                "selected_classes": self.selected_classes,
                "observation_ids": tuple(item.observation_id for item in self.observations),
            },
        )
        if self.analysis_id != expected_id:
            raise ValueError("class analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_CLASS_SURFACE_SCHEMA, **asdict(self)}


_CLASS_BASE_SQL = """
WITH direct_members AS (
    SELECT parent_symbol_id,
           SUM(CASE WHEN confirmed=1 AND kind='method' THEN 1 ELSE 0 END)
               AS direct_methods,
           SUM(CASE WHEN confirmed=1 AND kind='method' AND visibility='public'
                    THEN 1 ELSE 0 END) AS public_methods,
           SUM(CASE WHEN confirmed=1 AND kind='method' AND visibility='private'
                    THEN 1 ELSE 0 END) AS private_methods,
           SUM(CASE WHEN confirmed=1 AND kind='method' AND visibility='special'
                    THEN 1 ELSE 0 END) AS special_methods,
           SUM(CASE WHEN confirmed=1 AND kind='class_variable' THEN 1 ELSE 0 END)
               AS class_variables,
           SUM(CASE WHEN confirmed=1 AND kind='class' THEN 1 ELSE 0 END)
               AS nested_classes
    FROM symbols
    WHERE parent_symbol_id IS NOT NULL
    GROUP BY parent_symbol_id
)
SELECT s.symbol_id,s.version_id,v.file_id,f.volume_id,f.physical_file_id,
       f.current_path,s.name,s.qualified_name,s.start_line,s.end_line,
       s.start_column,s.end_column,s.start_byte,s.end_byte,s.metadata_json,
       v.raw_xxh3_128,v.raw_xxh3_64_guard,v.analyzer_id,v.analyzer_version,
       v.parser_kind,
       COALESCE(dm.direct_methods,0) AS direct_methods,
       COALESCE(dm.public_methods,0) AS public_methods,
       COALESCE(dm.private_methods,0) AS private_methods,
       COALESCE(dm.special_methods,0) AS special_methods,
       COALESCE(dm.class_variables,0) AS class_variables,
       COALESCE(dm.nested_classes,0) AS nested_classes
FROM symbols s
JOIN file_versions v ON v.version_id=s.version_id
JOIN files f ON f.current_version_id=v.version_id
LEFT JOIN direct_members dm ON dm.parent_symbol_id=s.symbol_id
WHERE s.kind='class' AND s.confirmed=1
  AND f.status='current' AND v.invalidated_ns IS NULL
  AND v.analysis_status='complete' AND v.language='python'
  AND v.generated=0 AND v.vendored=0 AND v.text_truncated=0
"""


def _row_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodeClassSurfaceEvidenceResolutionError(
            f"class surface source field {key} is not an integer"
        )
    return value


def _row_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise CodeClassSurfaceEvidenceResolutionError(
            f"class surface source field {key} is not text"
        )
    return value


def _metadata(row: sqlite3.Row) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    try:
        payload = json.loads(_row_text(row, "metadata_json"))
    except (TypeError, ValueError) as exc:
        raise CodeClassSurfaceEvidenceResolutionError(
            "class surface metadata is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CodeClassSurfaceEvidenceResolutionError("class surface metadata is not an object")
    try:
        bases = _text_tuple("class base", payload.get("bases", ()))
        decorators = _text_tuple("class decorator", payload.get("decorators", ()))
    except ValueError as exc:
        raise CodeClassSurfaceEvidenceResolutionError(str(exc)) from exc
    return (
        bases,
        decorators,
        analysis_identity("code-class-metadata-v1", payload),
    )


def _observation_from_row(
    row: sqlite3.Row,
    *,
    rank: int,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
) -> CodeClassSurfaceObservation:
    raw_xxh3_128 = _row_text(row, "raw_xxh3_128")
    raw_xxh3_64_guard = _row_text(row, "raw_xxh3_64_guard")
    revision_id = _revision_id(raw_xxh3_128, raw_xxh3_64_guard)
    bases, decorators, metadata_digest = _metadata(row)
    values: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "revision_id": revision_id,
        "symbol_id": _row_int(row, "symbol_id"),
        "version_id": _row_int(row, "version_id"),
        "file_id": _row_int(row, "file_id"),
        "volume_id": _row_text(row, "volume_id"),
        "physical_file_id": _row_text(row, "physical_file_id"),
        "path": _row_text(row, "current_path"),
        "name": _row_text(row, "name"),
        "qualified_name": _row_text(row, "qualified_name"),
        "start_line": _row_int(row, "start_line"),
        "end_line": _row_int(row, "end_line"),
        "start_column": _row_int(row, "start_column"),
        "end_column": _row_int(row, "end_column"),
        "start_byte": _row_int(row, "start_byte"),
        "end_byte": _row_int(row, "end_byte"),
        "direct_methods": _row_int(row, "direct_methods"),
        "public_methods": _row_int(row, "public_methods"),
        "private_methods": _row_int(row, "private_methods"),
        "special_methods": _row_int(row, "special_methods"),
        "class_variables": _row_int(row, "class_variables"),
        "nested_classes": _row_int(row, "nested_classes"),
        "bases": bases,
        "decorators": decorators,
        "metadata_digest": metadata_digest,
        "analyzer_id": _row_text(row, "analyzer_id"),
        "analyzer_version": _row_text(row, "analyzer_version"),
        "parser_kind": _row_text(row, "parser_kind"),
    }
    values["span_lines"] = cast(int, values["end_line"]) - cast(int, values["start_line"]) + 1
    values["selection_signals"] = _selection_signals(
        cast(int, values["span_lines"]), cast(int, values["direct_methods"])
    )
    subject_key = _class_subject_key(
        volume_id=cast(str, values["volume_id"]),
        physical_file_id=cast(str, values["physical_file_id"]),
        revision_id=revision_id,
        qualified_name=cast(str, values["qualified_name"]),
        start_byte=cast(int, values["start_byte"]),
    )
    projection_digest = analysis_identity(
        "code-class-surface-source-projection-v1",
        _source_projection(values),
    )
    observation_id = analysis_identity(
        "code-class-surface-observation-v1",
        {
            "subject_key": subject_key,
            "source_projection_digest": projection_digest,
            "policy": CODE_CLASS_SURFACE_POLICY,
        },
    )
    return CodeClassSurfaceObservation(
        observation_id=observation_id,
        subject_key=subject_key,
        source_projection_digest=projection_digest,
        rank=rank,
        snapshot_freshness=snapshot_freshness,
        selection_signals=cast(tuple[str, ...], values.pop("selection_signals")),
        **values,  # type: ignore[arg-type]
    )


def _read_source_row(connection: sqlite3.Connection, symbol_id: int) -> sqlite3.Row:
    row = connection.execute(
        _CLASS_BASE_SQL + " AND s.symbol_id=?",
        (symbol_id,),
    ).fetchone()
    if row is None:
        raise CodeClassSurfaceEvidenceResolutionError(
            "class surface source symbol is not current and resolvable"
        )
    return row


def validate_code_class_surface_observation(
    connection: sqlite3.Connection,
    observation: CodeClassSurfaceObservation,
) -> None:
    """Reopen the symbol/member projection and require exact equality."""

    try:
        rebuilt = _observation_from_row(
            _read_source_row(connection, observation.symbol_id),
            rank=observation.rank,
            snapshot_id=observation.snapshot_id,
            snapshot_freshness=observation.snapshot_freshness,
        )
    except ValueError as exc:
        raise CodeClassSurfaceEvidenceResolutionError(
            "class surface projection disagrees with its source symbols"
        ) from exc
    if rebuilt != observation:
        raise CodeClassSurfaceEvidenceResolutionError(
            "class surface projection disagrees with its source symbols"
        )


def _analysis_identity(
    *,
    snapshot_id: str,
    snapshot_freshness: str,
    eligible_classes: int,
    selected_classes: int,
    observations: tuple[CodeClassSurfaceObservation, ...],
) -> str:
    return analysis_identity(
        "code-class-surface-analysis-v1",
        {
            "snapshot_id": snapshot_id,
            "snapshot_freshness": snapshot_freshness,
            "policy": CODE_CLASS_SURFACE_POLICY,
            "eligible_classes": eligible_classes,
            "selected_classes": selected_classes,
            "observation_ids": tuple(item.observation_id for item in observations),
        },
    )


def read_code_class_surface_analysis(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    limit: int,
) -> CodeClassSurfaceAnalysis:
    """Read and independently re-resolve bounded current Python class surfaces."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("class surface analysis requires a SQLite connection")
    _required_text("class analysis snapshot", snapshot_id, maximum=2_048)
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("class analysis freshness is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("class surface limit must be between 1 and 50")
    eligible_classes = int(
        connection.execute(f"SELECT COUNT(*) FROM ({_CLASS_BASE_SQL})").fetchone()[0]
    )
    selection = "(end_line-start_line+1)>=? OR direct_methods>=?"
    selected_classes = int(
        connection.execute(
            f"SELECT COUNT(*) FROM ({_CLASS_BASE_SQL}) WHERE {selection}",
            (
                CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
                CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
            ),
        ).fetchone()[0]
    )
    rows = connection.execute(
        f"""SELECT * FROM ({_CLASS_BASE_SQL})
        WHERE {selection}
        ORDER BY CASE WHEN (end_line-start_line+1)>=?
                           AND direct_methods>=? THEN 1 ELSE 0 END DESC,
                 (end_line-start_line+1) DESC,direct_methods DESC,
                 current_path,qualified_name,start_byte,symbol_id
        LIMIT ?""",
        (
            CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
            CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
            CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
            CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
            limit,
        ),
    ).fetchall()
    observations = tuple(
        _observation_from_row(
            row,
            rank=rank,
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
        )
        for rank, row in enumerate(rows, start=1)
    )
    for observation in observations:
        validate_code_class_surface_observation(connection, observation)
    return CodeClassSurfaceAnalysis(
        analysis_id=_analysis_identity(
            snapshot_id=snapshot_id,
            snapshot_freshness=snapshot_freshness,
            eligible_classes=eligible_classes,
            selected_classes=selected_classes,
            observations=observations,
        ),
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        status="ready",
        reason=None,
        policy_id=CODE_CLASS_SURFACE_POLICY,
        span_lines_threshold=CLASS_SPAN_LINES_ATTENTION_THRESHOLD,
        direct_methods_threshold=CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD,
        eligible_classes=eligible_classes,
        selected_classes=selected_classes,
        returned_classes=len(observations),
        selection_truncated=len(observations) < selected_classes,
        observations=observations,
    )


def _class_evidence(observation: CodeClassSurfaceObservation) -> AnalysisEvidenceRef:
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-class-surface-evidence-v1",
            {
                "subject_key": observation.subject_key,
                "source_projection_digest": observation.source_projection_digest,
            },
        ),
        subject_key=observation.subject_key,
        role="supporting",
        evidence_kind="internal_fact",
        source_owner_id="code",
        producer_id=observation.analyzer_id,
        producer_version=observation.analyzer_version,
        source_schema=f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
        source_record_kind="class_symbol_with_direct_ast_members",
        source_record_id=str(observation.symbol_id),
        source_projection_digest=observation.source_projection_digest,
        snapshot_id=observation.snapshot_id,
        revision_id=observation.revision_id,
        facts=(
            AnalysisFact("class_span_lines", observation.span_lines, "lines"),
            AnalysisFact("direct_methods", observation.direct_methods, "count"),
            AnalysisFact("public_methods", observation.public_methods, "count"),
            AnalysisFact("private_methods", observation.private_methods, "count"),
            AnalysisFact("special_methods", observation.special_methods, "count"),
            AnalysisFact("class_variables", observation.class_variables, "count"),
            AnalysisFact("nested_classes", observation.nested_classes, "count"),
            AnalysisFact("bases_json", canonical_json(observation.bases)),
            AnalysisFact("decorators_json", canonical_json(observation.decorators)),
            AnalysisFact("selection_policy", CODE_CLASS_SURFACE_POLICY),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code.sqlite-class-surface-resolver",
        resolver_version="v1",
        limitations=("complete_direct_confirmed_ast_members_only",),
    )


def expected_class_surface_questions(
    analysis: CodeClassSurfaceAnalysis,
    *,
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Rebuild the canonical question projection from resolved observations."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("class question rank offset must be non-negative")
    evaluations: list[AnalysisQuestionEvaluation] = []
    spec_fingerprint = analysis_question_spec_fingerprint(CLASS_SURFACE_QUESTION)
    for observation in analysis.observations:
        evidence = _class_evidence(observation)
        evaluations.append(
            AnalysisQuestionEvaluation(
                evaluation_id=analysis_identity(
                    "code-question-evaluation-v1",
                    {
                        "observation_id": observation.observation_id,
                        "snapshot": observation.snapshot_id,
                        "question_spec": spec_fingerprint,
                        "evidence_id": evidence.evidence_id,
                    },
                ),
                question_id=CLASS_SURFACE_QUESTION.question_id,
                question_version=CLASS_SURFACE_QUESTION.version,
                question_spec_fingerprint=spec_fingerprint,
                rank=rank_offset + observation.rank,
                subject=AnalysisSubjectRef(
                    subject_kind="class",
                    subject_key=observation.subject_key,
                    display_name=observation.qualified_name,
                    source_owner_id="code",
                    snapshot_id=observation.snapshot_id,
                    snapshot_freshness=observation.snapshot_freshness,
                    revision_id=observation.revision_id,
                    location=AnalysisSourceLocation(
                        observation.path,
                        observation.start_line,
                        observation.end_line,
                        observation.start_column,
                        observation.end_column,
                    ),
                ),
                evidence=(evidence,),
                requirements=(
                    AnalysisRequirementEvaluation(
                        "resolved_direct_class_surface",
                        "satisfied",
                        (evidence.evidence_id,),
                        "linked_current_class_and_direct_ast_members",
                    ),
                    AnalysisRequirementEvaluation(
                        "declared_architectural_role_observed",
                        "missing",
                        (),
                        "no_declared_architectural_role_evidence_linked",
                    ),
                    AnalysisRequirementEvaluation(
                        "cohesion_and_responsibility_evidence_observed",
                        "missing",
                        (),
                        "no_method_state_or_responsibility_evidence_linked",
                    ),
                    AnalysisRequirementEvaluation(
                        "class_surface_counterevidence_evaluated",
                        "not_evaluated",
                        (),
                        "class_surface_counterevidence_not_evaluated",
                    ),
                    AnalysisRequirementEvaluation(
                        "class_surface_characterization_result",
                        "missing",
                        (),
                        "no_class_surface_characterization_result_linked",
                    ),
                ),
                observation_status="confirmed",
                inference_status="abstained",
                inferences=(),
                hypotheses=CLASS_SURFACE_QUESTION.hypotheses,
                question_readiness="ready",
                decision_readiness="experiment_required",
                decision=None,
                decision_reason="decision_evidence_incomplete",
                counterevidence_status="not_evaluated",
                next_action_ids=tuple(
                    item.action_id for item in CLASS_SURFACE_QUESTION.next_actions
                ),
                limitations=_CLASS_EVALUATION_LIMITATIONS,
            )
        )
    specs = (CLASS_SURFACE_QUESTION,) if evaluations else ()
    return specs, tuple(evaluations)


def parse_code_class_surface_payload(payload: Mapping[str, object]) -> CodeClassSurfaceAnalysis:
    """Strictly reconstruct the typed v1 projection from a public JSON mapping."""

    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_CLASS_SURFACE_SCHEMA:
        raise ValueError("class surface payload schema is invalid")
    expected_analysis_keys = {field.name for field in fields(CodeClassSurfaceAnalysis)} | {"schema"}
    if set(payload) != expected_analysis_keys:
        raise ValueError("class surface payload fields are invalid")
    raw_observations = payload.get("observations")
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes, bytearray)
    ):
        raise ValueError("class surface observations are invalid")
    expected_observation_keys = {field.name for field in fields(CodeClassSurfaceObservation)}
    observations: list[CodeClassSurfaceObservation] = []
    for raw in raw_observations:
        if not isinstance(raw, Mapping) or set(raw) != expected_observation_keys:
            raise ValueError("class surface observation fields are invalid")
        values = dict(raw)
        for key in ("bases", "decorators", "selection_signals", "limitations"):
            values[key] = _text_tuple(f"class observation {key}", values[key])
        observations.append(CodeClassSurfaceObservation(**values))  # type: ignore[arg-type]
    values = {key: value for key, value in payload.items() if key != "schema"}
    values["observations"] = tuple(observations)
    values["limitations"] = _text_tuple("class analysis limitations", values["limitations"])
    return CodeClassSurfaceAnalysis(**values)  # type: ignore[arg-type]


__all__ = [
    "CLASS_DIRECT_METHODS_ATTENTION_THRESHOLD",
    "CLASS_SPAN_LINES_ATTENTION_THRESHOLD",
    "CLASS_SURFACE_QUESTION",
    "CODE_CLASS_SURFACE_POLICY",
    "CODE_CLASS_SURFACE_SCHEMA",
    "CodeClassSurfaceAnalysis",
    "CodeClassSurfaceEvidenceResolutionError",
    "CodeClassSurfaceObservation",
    "expected_class_surface_questions",
    "parse_code_class_surface_payload",
    "read_code_class_surface_analysis",
    "validate_code_class_surface_observation",
]
