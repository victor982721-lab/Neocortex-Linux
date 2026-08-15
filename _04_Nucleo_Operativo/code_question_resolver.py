"""Bounded focal resolution for exact published Code questions.

The resolver is a strict router over existing question specifications and
projection builders.  A registered reader may materialize only the evidence
owner needed by its exact question.  Unknown questions are handed back through
an explicit, non-automatic fallback; they are never guessed from prefixes or
natural language.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .code_analysis_epistemics import (
    AnalysisQuestionEvaluation,
    analysis_identity,
)
from .code_interface_surface_analysis import (
    CLI_SURFACE_QUESTION,
    CodeInterfaceSurfaceAnalysis,
    interface_surface_questions,
    read_code_interface_surface_analysis,
)
from .code_review_eligibility import code_review_eligibility
from .code_schema import CODE_SCHEMA_VERSION, validate_code_schema
from .self_analysis_status import (
    CodeRunStatusEvidence,
    SelfAnalysisStatus,
    quiescent_sqlite_database,
    read_self_analysis_status,
    require_sqlite_sidecars_absent,
)


CODE_QUESTION_RESOLUTION_SCHEMA = "neocortex.code-question-resolution/v1"
CODE_QUESTION_RESOLVER_POLICY = "exact-question-focal-reader-v1"
CODE_QUESTION_RESOLVER_MAX_RESULTS = 50

QuestionResolutionStatus = Literal["ready", "abstained", "unsupported"]
QuestionParityStatus = Literal["matched", "mismatched", "not_evaluated"]
QuestionSourceSurface = Literal["interface_surface", "review"]


def _required(label: str, value: object, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _strings(
    label: str,
    values: object,
    *,
    ordered: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required(label, item) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    if ordered and result != tuple(sorted(result)):
        raise ValueError(f"{label} must be canonically ordered")
    return result


def _is_non_text_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


@dataclass(frozen=True, slots=True)
class CodeQuestionFallback:
    """Explicit hand-off when no focal reader owns an exact question."""

    fallback_kind: Literal["code_analysis_query"]
    surface: Literal["review"]
    question_id: str
    reason: Literal["question_reader_not_registered"]
    automatic: Literal[False] = False

    def __post_init__(self) -> None:
        _required("fallback question id", self.question_id, 256)
        if (
            self.fallback_kind != "code_analysis_query"
            or self.surface != "review"
            or self.reason != "question_reader_not_registered"
            or self.automatic
        ):
            raise ValueError("question fallback contract is invalid")


@dataclass(frozen=True, slots=True)
class CodeQuestionReaderSpec:
    question_id: str
    question_version: str
    reader_id: str
    source_surface: Literal["interface_surface"]
    consumer_interfaces: tuple[Literal["cli", "knowledge"], ...]
    max_results: int

    def __post_init__(self) -> None:
        _required("question id", self.question_id, 256)
        _required("question version", self.question_version, 64)
        _required("question reader id", self.reader_id, 256)
        if self.source_surface != "interface_surface":
            raise ValueError("question reader source surface is invalid")
        if self.consumer_interfaces != ("cli", "knowledge"):
            raise ValueError("question reader interfaces must remain canonical")
        if self.max_results != CODE_QUESTION_RESOLVER_MAX_RESULTS:
            raise ValueError("question reader result bound is invalid")


@dataclass(frozen=True, slots=True)
class CodeQuestionResolution:
    question_id: str
    question_version: str | None
    status: QuestionResolutionStatus
    reason: str | None
    reader_id: str | None
    source_surface: QuestionSourceSurface
    source_digest: str | None
    total_matches: int
    evaluations: tuple[AnalysisQuestionEvaluation, ...]
    truncated: bool
    fallback: CodeQuestionFallback | None
    limitations: tuple[str, ...]
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required("resolution question id", self.question_id, 256)
        if self.question_version is not None:
            _required("resolution question version", self.question_version, 64)
        if self.status not in {"ready", "abstained", "unsupported"}:
            raise ValueError("question resolution status is invalid")
        if self.source_surface not in {"interface_surface", "review"}:
            raise ValueError("question resolution source surface is invalid")
        if isinstance(self.total_matches, bool) or self.total_matches < 0:
            raise ValueError("question resolution count is invalid")
        if len(self.evaluations) > CODE_QUESTION_RESOLVER_MAX_RESULTS:
            raise ValueError("question resolution result bound exceeded")
        if self.total_matches < len(self.evaluations):
            raise ValueError("question resolution count is inconsistent")
        if self.truncated != (self.total_matches > len(self.evaluations)):
            raise ValueError("question resolution truncation is inconsistent")
        if any(
            item.question_id != self.question_id
            or (
                self.question_version is not None and item.question_version != self.question_version
            )
            for item in self.evaluations
        ):
            raise ValueError("question resolution contains another question")
        evaluation_ids = tuple(item.evaluation_id for item in self.evaluations)
        if evaluation_ids != tuple(sorted(evaluation_ids)) or len(set(evaluation_ids)) != len(
            evaluation_ids
        ):
            raise ValueError("question resolution evaluations must be unique and ordered")
        _strings("question resolution limitation", self.limitations)
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("question resolution must remain advisory and non-mutating")
        if self.status == "unsupported":
            if (
                self.question_version is not None
                or self.reader_id is not None
                or self.source_surface != "review"
                or self.source_digest is not None
                or self.total_matches
                or self.evaluations
                or self.truncated
                or self.fallback is None
                or self.reason != "question_reader_not_registered"
            ):
                raise ValueError("unsupported question resolution is contradictory")
        else:
            if (
                self.fallback is not None
                or self.reader_id is None
                or self.source_surface == "review"
            ):
                raise ValueError("registered question resolution source is contradictory")
            if self.question_version is None:
                raise ValueError("registered question resolution lacks its exact version")
            if self.status == "ready":
                if self.reason is not None or self.source_digest is None:
                    raise ValueError("ready question resolution lacks focal source evidence")
            elif not self.reason:
                raise ValueError("abstained question resolution requires a reason")

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": CODE_QUESTION_RESOLUTION_SCHEMA,
            "policy_id": CODE_QUESTION_RESOLVER_POLICY,
            **asdict(self),
        }


@dataclass(frozen=True, slots=True)
class CodeQuestionParity:
    question_id: str
    question_version: str
    status: QuestionParityStatus
    reason: str | None
    resolver_evaluation_ids: tuple[str, ...]
    canonical_evaluation_ids: tuple[str, ...]
    resolver_evaluation_digests: tuple[str, ...]
    canonical_evaluation_digests: tuple[str, ...]
    missing_evaluation_ids: tuple[str, ...]
    unexpected_evaluation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required("parity question id", self.question_id, 256)
        _required("parity question version", self.question_version, 64)
        if self.status not in {"matched", "mismatched", "not_evaluated"}:
            raise ValueError("question parity status is invalid")
        for label, values in (
            ("resolver parity evaluation id", self.resolver_evaluation_ids),
            ("canonical parity evaluation id", self.canonical_evaluation_ids),
            ("missing parity evaluation id", self.missing_evaluation_ids),
            ("unexpected parity evaluation id", self.unexpected_evaluation_ids),
        ):
            _strings(label, values, ordered=True)
        for label, values in (
            ("resolver parity evaluation digest", self.resolver_evaluation_digests),
            ("canonical parity evaluation digest", self.canonical_evaluation_digests),
        ):
            if not isinstance(values, tuple):
                raise ValueError(f"{label} must be a tuple")
            for value in values:
                _required(label, value, 256)
        if len(self.resolver_evaluation_ids) != len(self.resolver_evaluation_digests):
            raise ValueError("resolver parity identities and digests differ in size")
        if len(self.canonical_evaluation_ids) != len(self.canonical_evaluation_digests):
            raise ValueError("canonical parity identities and digests differ in size")
        expected_missing = tuple(
            sorted(set(self.canonical_evaluation_ids) - set(self.resolver_evaluation_ids))
        )
        expected_unexpected = tuple(
            sorted(set(self.resolver_evaluation_ids) - set(self.canonical_evaluation_ids))
        )
        if (
            self.missing_evaluation_ids != expected_missing
            or self.unexpected_evaluation_ids != expected_unexpected
        ):
            raise ValueError("question parity deltas are not derived")
        exact = (
            self.resolver_evaluation_ids == self.canonical_evaluation_ids
            and self.resolver_evaluation_digests == self.canonical_evaluation_digests
        )
        expected_status: QuestionParityStatus = (
            "not_evaluated"
            if not self.resolver_evaluation_ids and not self.canonical_evaluation_ids
            else "matched"
            if exact
            else "mismatched"
        )
        if self.status != expected_status:
            raise ValueError("question parity status is not derived")
        expected_reason = (
            "no_question_evaluations_observed"
            if expected_status == "not_evaluated"
            else None
            if expected_status == "matched"
            else "focal_and_canonical_question_evaluations_differ"
        )
        if self.reason != expected_reason:
            raise ValueError("question parity reason is not derived")


@dataclass(frozen=True, slots=True)
class _QuestionFence:
    latest_run: CodeRunStatusEvidence
    status: SelfAnalysisStatus
    freshness: Literal["current", "publication_only"]
    freshness_limitation: str | None


_CLI_READER = CodeQuestionReaderSpec(
    question_id=CLI_SURFACE_QUESTION.question_id,
    question_version=CLI_SURFACE_QUESTION.version,
    reader_id="code-interface-static-cli-question-reader",
    source_surface="interface_surface",
    consumer_interfaces=("cli", "knowledge"),
    max_results=CODE_QUESTION_RESOLVER_MAX_RESULTS,
)

CODE_QUESTION_READERS = (_CLI_READER,)


def _validate_registry() -> None:
    identities = tuple((item.question_id, item.question_version) for item in CODE_QUESTION_READERS)
    if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
        raise ValueError("question reader registry must be exact, unique, and ordered")


_validate_registry()


def code_question_reader_registry() -> tuple[CodeQuestionReaderSpec, ...]:
    return CODE_QUESTION_READERS


def _latest_run(connection: sqlite3.Connection) -> CodeRunStatusEvidence | None:
    row = connection.execute(
        """SELECT analysis_run_id,framework_run_id,scan_id,
        processing_signature,status
        FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    return CodeRunStatusEvidence(
        analysis_run_id=int(row["analysis_run_id"]),
        framework_run_id=int(row["framework_run_id"]),
        scan_id=int(row["scan_id"]),
        processing_signature=str(row["processing_signature"]),
        status=str(row["status"]),
    )


def _current_projection_matches(
    connection: sqlite3.Connection,
    latest_run: CodeRunStatusEvidence,
) -> bool:
    row = connection.execute(
        """SELECT COUNT(*) FROM files f JOIN file_versions v
        ON v.version_id=f.current_version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND (v.last_observed_run_id<>? OR v.processing_signature<>?)""",
        (latest_run.framework_run_id, latest_run.processing_signature),
    ).fetchone()
    return row is not None and int(row[0]) == 0


def _question_fence(
    state_directory: Path,
    database: Path,
) -> tuple[_QuestionFence | None, str | None]:
    with quiescent_sqlite_database(database, timeout_seconds=60) as connection:
        validate_code_schema(connection)
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != CODE_SCHEMA_VERSION:
            return None, f"code_state_schema_unsupported:{schema_version}"
        latest_run = _latest_run(connection)
        if latest_run is None:
            return None, "code_run_missing"
        if latest_run.status != "completed":
            return None, f"code_run_not_completed:{latest_run.status}"
        if not _current_projection_matches(connection, latest_run):
            return None, "current_code_projection_not_owned_by_latest_completed_run"
    status = read_self_analysis_status(state_directory, latest_run)
    reason, freshness, freshness_limitation = code_review_eligibility(status)
    if reason is not None:
        return None, reason
    if status is None or freshness is None:
        raise AssertionError("eligible question resolution requires freshness evidence")
    return _QuestionFence(latest_run, status, freshness, freshness_limitation), None


def _read_cli_question(
    database: Path,
    fence: _QuestionFence,
    *,
    source_limit: int,
) -> tuple[CodeInterfaceSurfaceAnalysis, tuple[AnalysisQuestionEvaluation, ...]]:
    with quiescent_sqlite_database(database, timeout_seconds=60) as connection:
        validate_code_schema(connection)
        latest_run = _latest_run(connection)
        if latest_run is None or latest_run != fence.latest_run:
            raise RuntimeError("latest_code_run_changed_during_question_resolution")
        if not _current_projection_matches(connection, latest_run):
            raise RuntimeError("current_code_projection_changed_during_question_resolution")
        analysis = read_code_interface_surface_analysis(
            connection,
            analysis_run_id=latest_run.analysis_run_id,
            processing_signature=latest_run.processing_signature,
            database=str(database),
            limit=source_limit,
        )
    if analysis.status != "ready":
        return analysis, ()
    _specs, evaluations = interface_surface_questions(
        analysis,
        snapshot_freshness=fence.freshness,
        rank_offset=0,
    )
    selected = tuple(
        sorted(
            (
                item
                for item in evaluations
                if item.question_id == CLI_SURFACE_QUESTION.question_id
                and item.question_version == CLI_SURFACE_QUESTION.version
            ),
            key=lambda item: item.evaluation_id,
        )
    )
    return analysis, selected


QuestionReader = Callable[
    [Path, _QuestionFence],
    tuple[CodeInterfaceSurfaceAnalysis, tuple[AnalysisQuestionEvaluation, ...]],
]


_READER_BY_QUESTION: Mapping[
    str,
    Callable[..., tuple[CodeInterfaceSurfaceAnalysis, tuple[AnalysisQuestionEvaluation, ...]]],
] = {CLI_SURFACE_QUESTION.question_id: _read_cli_question}


def _abstained_resolution(
    spec: CodeQuestionReaderSpec,
    reason: str,
    *,
    limitations: tuple[str, ...] = (),
) -> CodeQuestionResolution:
    return CodeQuestionResolution(
        question_id=spec.question_id,
        question_version=spec.question_version,
        status="abstained",
        reason=_required("question abstention reason", reason, 512),
        reader_id=spec.reader_id,
        source_surface=spec.source_surface,
        source_digest=None,
        total_matches=0,
        evaluations=(),
        truncated=False,
        fallback=None,
        limitations=tuple(dict.fromkeys((*limitations, "focal_question_resolution_failed_closed"))),
    )


def resolve_code_question(
    state_directory: Path,
    question_id: str,
    *,
    limit: int = 10,
) -> CodeQuestionResolution:
    """Resolve one exact question without materializing the global Code review."""

    selected = _required("question id", question_id, 256)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not (1 <= limit <= CODE_QUESTION_RESOLVER_MAX_RESULTS)
    ):
        raise ValueError(
            f"question resolution limit must be between 1 and {CODE_QUESTION_RESOLVER_MAX_RESULTS}"
        )
    spec = next((item for item in CODE_QUESTION_READERS if item.question_id == selected), None)
    if spec is None:
        fallback = CodeQuestionFallback(
            "code_analysis_query",
            "review",
            selected,
            "question_reader_not_registered",
        )
        return CodeQuestionResolution(
            question_id=selected,
            question_version=None,
            status="unsupported",
            reason=fallback.reason,
            reader_id=None,
            source_surface="review",
            source_digest=None,
            total_matches=0,
            evaluations=(),
            truncated=False,
            fallback=fallback,
            limitations=("fallback_is_explicit_and_never_executed_automatically",),
        )

    state_directory = Path(state_directory)
    database = state_directory / "code.sqlite3"
    try:
        require_sqlite_sidecars_absent(database)
        if not database.is_file():
            return _abstained_resolution(spec, "code_state_missing")
        fence, fence_reason = _question_fence(state_directory, database)
        if fence_reason is not None:
            return _abstained_resolution(spec, fence_reason)
        if fence is None:
            raise AssertionError("eligible focal resolution requires a fence")
        reader = _READER_BY_QUESTION[spec.question_id]
        analysis, evaluations = reader(database, fence, source_limit=limit)
        if analysis.status != "ready":
            return _abstained_resolution(
                spec,
                analysis.reason or "focal_interface_surface_abstained_without_reason",
                limitations=analysis.limitations,
            )
        post_fence, post_reason = _question_fence(state_directory, database)
        if post_reason is not None:
            return _abstained_resolution(spec, post_reason)
        if post_fence is None:
            raise AssertionError("completed focal resolution requires a post-read fence")
        if post_fence.latest_run != fence.latest_run:
            return _abstained_resolution(spec, "latest_code_run_changed_during_resolution")
        if post_fence != fence:
            return _abstained_resolution(
                spec,
                "self_analysis_freshness_changed_during_resolution",
            )
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return _abstained_resolution(spec, "focal_question_evidence_unresolvable")

    returned = evaluations[:limit]
    limitations = tuple(
        dict.fromkeys(
            (
                *analysis.limitations,
                *(() if fence.freshness_limitation is None else (fence.freshness_limitation,)),
                "focal_resolution_reads_interface_projection_without_global_review",
                "evaluation_rank_is_local_to_the_focal_projection",
            )
        )
    )
    return CodeQuestionResolution(
        question_id=spec.question_id,
        question_version=spec.question_version,
        status="ready",
        reason=None,
        reader_id=spec.reader_id,
        source_surface=spec.source_surface,
        source_digest=analysis.analysis_id,
        total_matches=len(evaluations),
        evaluations=returned,
        truncated=len(evaluations) > len(returned),
        fallback=None,
        limitations=limitations,
    )


def _evaluation_digest(evaluation: AnalysisQuestionEvaluation) -> str:
    payload = asdict(evaluation)
    payload.pop("evaluation_id")
    payload.pop("rank")
    return analysis_identity("code-question-parity-evaluation-v1", payload)


def compare_code_question_parity(
    resolution: CodeQuestionResolution,
    canonical_evaluations: Sequence[AnalysisQuestionEvaluation],
) -> CodeQuestionParity:
    """Compare focal and canonical evaluations, ignoring presentation rank only."""

    if not isinstance(resolution, CodeQuestionResolution):
        raise TypeError("question parity resolution is invalid")
    if resolution.status != "ready" or resolution.question_version is None:
        raise ValueError("question parity requires a ready registered resolution")
    if not _is_non_text_sequence(canonical_evaluations):
        raise TypeError("canonical question evaluations must be a sequence")
    canonical = tuple(
        sorted(
            (
                item
                for item in canonical_evaluations
                if isinstance(item, AnalysisQuestionEvaluation)
                and item.question_id == resolution.question_id
                and item.question_version == resolution.question_version
            ),
            key=lambda item: item.evaluation_id,
        )
    )
    resolver_ids = tuple(item.evaluation_id for item in resolution.evaluations)
    canonical_ids = tuple(item.evaluation_id for item in canonical)
    resolver_digests = tuple(_evaluation_digest(item) for item in resolution.evaluations)
    canonical_digests = tuple(_evaluation_digest(item) for item in canonical)
    missing = tuple(sorted(set(canonical_ids) - set(resolver_ids)))
    unexpected = tuple(sorted(set(resolver_ids) - set(canonical_ids)))
    exact = resolver_ids == canonical_ids and resolver_digests == canonical_digests
    status: QuestionParityStatus = (
        "not_evaluated"
        if not resolver_ids and not canonical_ids
        else "matched"
        if exact
        else "mismatched"
    )
    reason = (
        "no_question_evaluations_observed"
        if status == "not_evaluated"
        else None
        if status == "matched"
        else "focal_and_canonical_question_evaluations_differ"
    )
    return CodeQuestionParity(
        resolution.question_id,
        resolution.question_version,
        status,
        reason,
        resolver_ids,
        canonical_ids,
        resolver_digests,
        canonical_digests,
        missing,
        unexpected,
    )


__all__ = [
    "CODE_QUESTION_READERS",
    "CODE_QUESTION_RESOLUTION_SCHEMA",
    "CODE_QUESTION_RESOLVER_MAX_RESULTS",
    "CODE_QUESTION_RESOLVER_POLICY",
    "CodeQuestionFallback",
    "CodeQuestionParity",
    "CodeQuestionReaderSpec",
    "CodeQuestionResolution",
    "code_question_reader_registry",
    "compare_code_question_parity",
    "resolve_code_question",
]
