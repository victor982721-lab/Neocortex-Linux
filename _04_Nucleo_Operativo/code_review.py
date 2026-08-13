"""Deterministic, read-only maintenance shortlist over published Code evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .code_architecture_analysis import (
    CodeArchitectureAnalysis,
    read_code_architecture_analysis,
)
from .code_analyzer_effectiveness import analyze_code_analyzer_effectiveness
from .code_assurance_analysis import analyze_code_assurance
from .code_capability_reachability_analysis import (
    abstained_capability_reachability,
    analyze_capability_reachability,
)
from .code_change_evolution_analysis import (
    CodeChangeEvolutionAnalysis,
    read_code_change_evolution_analysis,
)
from .code_coverage_analysis import (
    CodeCoverageAnalysis,
    read_code_coverage_analysis,
)
from .code_engineering_analytics import (
    CodeEngineeringAnalytics,
    analyze_code_engineering,
)
from .code_external_evidence import (
    ExternalEvidenceStatus,
    read_external_evidence,
)
from .code_interface_surface_analysis import (
    CodeInterfaceSurfaceAnalysis,
    abstained_code_interface_surface,
    read_code_interface_surface_analysis,
)
from .code_unused_analysis import (
    CodeUnusedAnalysis,
    read_code_unused_analysis,
)
from .code_supply_chain_analysis import (
    CodeSupplyChainAnalysis,
    read_code_supply_chain_analysis,
)
from .code_review_actionability import (
    CODE_REVIEW_ACTIONABILITY,
    CodeReviewActionabilityInput,
    SourceRole,
    assess_code_review_actionability,
    classify_source_role,
)
from .code_review_eligibility import (
    code_review_eligibility,
    self_analysis_manifest_root,
)
from .code_review_models import (
    CODE_REVIEW_SCHEMA,
    CodeReviewCaller,
    CodeReviewCoverage,
    CodeReviewDiagnostic,
    CodeReviewDigest,
    CodeReviewFinding,
    CodeReviewImpact,
    CodeReviewRecommendation,
    CodeReviewResult,
    CodeReviewSnapshot,
    FindingCategory,
    RecommendationStatus,
)
from .code_review_serialization import build_code_review_digest
from .code_review_epistemics import (
    CodeReviewEvidenceResolutionError,
    expected_integrated_code_review_questions,
    resolve_code_review_questions,
)
from .code_review_work_packages import (
    CODE_REVIEW_PLANNING,
    plan_code_review_work_packages,
)
from .code_state_projection_analysis import (
    abstained_code_state_projection,
    analyze_text_semantic_projection,
)
from .code_state_topology_analysis import (
    CodeStateTopologyResolutionError,
    abstained_code_state_topology,
    analyze_text_terminal_publication,
    resolve_state_topology_questions,
)
from .code_schema import (
    CODE_SCHEMA_VERSION,
    readonly_code_database,
    validate_code_schema,
)
from .external_evidence_models import ExternalEvidenceSuiteStatus, ExternalProviderEvidence
from .external_evidence_store import (
    read_external_evidence_suite,
    read_external_provider_evidence,
)
from .self_analysis_status import (
    CodeRunStatusEvidence,
    read_self_analysis_status,
    require_sqlite_sidecars_absent,
)
from .semantic_models import canonical_json, fingerprint_text

CODE_REVIEW_RANKING = "python-confirmed-hotspots-v2"
CODE_REVIEW_LIMIT = 10
CODE_REVIEW_MAX_LIMIT = 50
CODE_REVIEW_MAX_PER_FILE = 2
CODE_REVIEW_MAX_CANDIDATES = 10_000
CODE_REVIEW_CALLER_EXAMPLES = 3
CODE_REVIEW_CONSUMER_MODULE_EXAMPLES = 5
CODE_REVIEW_OUTGOING_CALL_LIMIT = 256
CODE_REVIEW_RECOMMENDATION_LIMIT = 3


@dataclass(frozen=True, slots=True)
class _Candidate:
    file_id: int
    volume_id: str
    physical_file_id: str
    project_root: str | None
    version_id: int
    symbol_id: int
    path: str
    symbol: str
    symbol_kind: str
    signature: str | None
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    complexity: int
    function_lines: int
    complexity_threshold: int | None
    length_threshold: int | None
    complexity_ratio_basis_points: int
    length_ratio_basis_points: int
    score_basis_points: int
    high_complexity: bool
    long_function: bool
    incoming_references: int
    incoming_calls: int
    resolved_static_callers: int
    analyzer_id: str
    analyzer_version: str
    file_xxh3_128: str | None
    file_xxh3_64_guard: str | None


@dataclass(frozen=True, slots=True)
class _ReviewRead:
    latest_run: CodeRunStatusEvidence | None
    coverage: CodeReviewCoverage
    findings: tuple[CodeReviewFinding, ...]
    enumeration_truncated: bool
    external_evidence: ExternalEvidenceStatus
    external_evidence_suite: ExternalEvidenceSuiteStatus
    architecture: CodeArchitectureAnalysis
    test_coverage: CodeCoverageAnalysis
    engineering_analytics: CodeEngineeringAnalytics
    unused_analysis: CodeUnusedAnalysis
    supply_chain: CodeSupplyChainAnalysis
    provider_evidence: Mapping[str, ExternalProviderEvidence]
    change_evolution: CodeChangeEvolutionAnalysis
    interface_surface: CodeInterfaceSurfaceAnalysis


_CANDIDATE_SQL = """
WITH current_references AS (
    SELECT r.target_symbol_id,
           COUNT(*) AS incoming_references,
           SUM(CASE WHEN r.kind='call' THEN 1 ELSE 0 END) AS incoming_calls,
           COUNT(DISTINCT CASE WHEN r.kind='call' THEN
               printf('%d:%d',r.version_id,COALESCE(r.source_symbol_id,0)) END
           ) AS resolved_static_callers
    FROM code_references r
    JOIN file_versions source_version ON source_version.version_id=r.version_id
    JOIN files source_file
      ON source_file.current_version_id=source_version.version_id
     AND source_file.status='current'
    WHERE r.target_symbol_id IS NOT NULL
      AND r.confirmed=1
      AND source_version.invalidated_ns IS NULL
      AND source_version.language='python'
    GROUP BY r.target_symbol_id
), signals AS (
    SELECT d.version_id,d.start_byte,d.end_byte,
           MAX(CASE WHEN d.code='high_complexity' AND d.confirmed=1
               THEN 1 ELSE 0 END) AS high_complexity,
           MAX(CASE WHEN d.code='long_function' AND d.confirmed=1
               THEN 1 ELSE 0 END) AS long_function,
           MAX(CASE WHEN d.code='high_complexity' AND d.confirmed=1
               THEN d.metadata_json END) AS complexity_metadata_json,
           MAX(CASE WHEN d.code='long_function' AND d.confirmed=1
               THEN d.metadata_json END) AS length_metadata_json
    FROM diagnostics d
    JOIN file_versions diagnostic_version
      ON diagnostic_version.version_id=d.version_id
    WHERE diagnostic_version.invalidated_ns IS NULL
      AND d.code IN ('high_complexity','long_function')
    GROUP BY d.version_id,d.start_byte,d.end_byte
)
SELECT f.file_id,f.volume_id,f.physical_file_id,
       (SELECT p.probable_root FROM project_memberships pm
        JOIN projects p ON p.project_id=pm.project_id
        WHERE pm.version_id=v.version_id AND pm.selected=1 AND p.status='current'
          AND p.probable_root IS NOT NULL
        ORDER BY pm.confidence DESC,LENGTH(p.probable_root) DESC,p.project_id
        LIMIT 1) AS project_root,
       v.version_id,s.symbol_id,
       f.current_path,s.qualified_name,s.kind,s.signature,s.start_line,s.end_line,
       s.start_column,s.end_column,s.start_byte,s.end_byte,
       COALESCE(s.complexity,0) AS complexity,
       s.end_line-s.start_line+1 AS function_lines,
       signals.high_complexity,signals.long_function,
       COALESCE(current_references.incoming_references,0) AS incoming_references,
       COALESCE(current_references.incoming_calls,0) AS incoming_calls,
       COALESCE(current_references.resolved_static_callers,0)
           AS resolved_static_callers,
       signals.complexity_metadata_json,signals.length_metadata_json,
       v.analyzer_id,v.analyzer_version,v.raw_xxh3_128,v.raw_xxh3_64_guard,
       COUNT(*) OVER() AS total_candidates
FROM symbols s
JOIN file_versions v ON v.version_id=s.version_id
JOIN files f ON f.current_version_id=v.version_id AND f.status='current'
JOIN signals
  ON signals.version_id=s.version_id
 AND signals.start_byte=s.start_byte
 AND signals.end_byte=s.end_byte
LEFT JOIN current_references ON current_references.target_symbol_id=s.symbol_id
WHERE v.invalidated_ns IS NULL
  AND v.analysis_status='complete'
  AND v.language='python'
  AND v.generated=0 AND v.vendored=0
  AND s.confirmed=1
  AND s.kind IN ('function','method','nested_function')
  AND (signals.high_complexity=1 OR signals.long_function=1)
ORDER BY (signals.high_complexity+signals.long_function) DESC,
         COALESCE(current_references.resolved_static_callers,0) DESC,
         COALESCE(s.complexity,0) DESC,
         function_lines DESC,
         f.current_path COLLATE NOCASE,s.qualified_name,s.start_byte,s.symbol_id
LIMIT ?
"""


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


def _current_versions_match_latest_run(
    connection: sqlite3.Connection,
    latest_run: CodeRunStatusEvidence,
) -> bool:
    """Reject a mixed projection whose current rows were not observed by the head run."""

    row = connection.execute(
        """SELECT COUNT(*) FROM files f JOIN file_versions v
        ON v.version_id=f.current_version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
        AND (v.last_observed_run_id<>? OR v.processing_signature<>?)""",
        (latest_run.framework_run_id, latest_run.processing_signature),
    ).fetchone()
    return row is not None and int(row[0]) == 0


def _candidate(row: sqlite3.Row) -> _Candidate:
    complexity = int(row["complexity"])
    function_lines = int(row["function_lines"])
    high_complexity = bool(row["high_complexity"])
    long_function = bool(row["long_function"])
    complexity_threshold = _diagnostic_threshold(
        row["complexity_metadata_json"],
        expected_value=complexity,
        required=high_complexity,
        label="high_complexity",
    )
    length_threshold = _diagnostic_threshold(
        row["length_metadata_json"],
        expected_value=function_lines,
        required=long_function,
        label="long_function",
    )
    complexity_ratio = _ratio_basis_points(complexity, complexity_threshold)
    length_ratio = _ratio_basis_points(function_lines, length_threshold)
    callers = int(row["resolved_static_callers"])
    score = _score_basis_points(complexity_ratio, length_ratio, callers)
    return _Candidate(
        file_id=int(row["file_id"]),
        volume_id=str(row["volume_id"]),
        physical_file_id=str(row["physical_file_id"]),
        project_root=(None if row["project_root"] is None else str(row["project_root"])),
        version_id=int(row["version_id"]),
        symbol_id=int(row["symbol_id"]),
        path=str(row["current_path"]),
        symbol=str(row["qualified_name"]),
        symbol_kind=str(row["kind"]),
        signature=None if row["signature"] is None else str(row["signature"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        start_column=int(row["start_column"]),
        end_column=int(row["end_column"]),
        start_byte=int(row["start_byte"]),
        end_byte=int(row["end_byte"]),
        complexity=complexity,
        function_lines=function_lines,
        complexity_threshold=complexity_threshold,
        length_threshold=length_threshold,
        complexity_ratio_basis_points=complexity_ratio,
        length_ratio_basis_points=length_ratio,
        score_basis_points=score,
        high_complexity=high_complexity,
        long_function=long_function,
        incoming_references=int(row["incoming_references"]),
        incoming_calls=int(row["incoming_calls"]),
        resolved_static_callers=callers,
        analyzer_id=str(row["analyzer_id"]),
        analyzer_version=str(row["analyzer_version"]),
        file_xxh3_128=(None if row["raw_xxh3_128"] is None else str(row["raw_xxh3_128"])),
        file_xxh3_64_guard=(
            None if row["raw_xxh3_64_guard"] is None else str(row["raw_xxh3_64_guard"])
        ),
    )


def _diagnostic_threshold(
    raw_metadata: object,
    *,
    expected_value: int,
    required: bool,
    label: str,
) -> int | None:
    if raw_metadata is None:
        if required:
            raise ValueError(f"{label} diagnostic metadata is missing")
        return None
    metadata = json.loads(str(raw_metadata))
    if not isinstance(metadata, dict):
        raise ValueError(f"{label} diagnostic metadata must be an object")
    value = metadata.get("value")
    threshold = metadata.get("threshold")
    if isinstance(value, bool) or not isinstance(value, int) or value != expected_value:
        raise ValueError(f"{label} diagnostic value disagrees with symbol evidence")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError(f"{label} diagnostic threshold must be positive")
    if required and expected_value < threshold:
        raise ValueError(f"{label} diagnostic does not meet its declared threshold")
    return threshold


def _ratio_basis_points(value: int, threshold: int | None) -> int:
    return 0 if threshold is None else (10_000 * value) // threshold


def _score_basis_points(
    complexity_ratio: int,
    length_ratio: int,
    resolved_static_callers: int,
) -> int:
    """Prioritize branching evidence while retaining length and impact support."""

    return complexity_ratio + length_ratio // 4 + 250 * min(resolved_static_callers, 20)


def _rank_key(candidate: _Candidate) -> tuple[object, ...]:
    return (
        -candidate.score_basis_points,
        -max(
            candidate.complexity_ratio_basis_points,
            candidate.length_ratio_basis_points,
        ),
        -candidate.complexity_ratio_basis_points,
        -candidate.length_ratio_basis_points,
        -candidate.resolved_static_callers,
        candidate.path.casefold(),
        candidate.path,
        candidate.symbol,
        candidate.start_line,
        candidate.symbol_id,
    )


def _select_candidates(
    candidates: tuple[_Candidate, ...],
    *,
    limit: int,
) -> tuple[_Candidate, ...]:
    ordered = tuple(sorted(candidates, key=_rank_key))
    selected: list[_Candidate] = []
    selected_ids: set[int] = set()
    per_file: dict[str, int] = {}
    for allowed_per_file in range(1, CODE_REVIEW_MAX_PER_FILE + 1):
        for candidate in ordered:
            if candidate.symbol_id in selected_ids:
                continue
            path_key = candidate.path.casefold()
            if per_file.get(path_key, 0) >= allowed_per_file:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.symbol_id)
            per_file[path_key] = per_file.get(path_key, 0) + 1
            if len(selected) == limit:
                return tuple(selected)
    return tuple(selected)


def _diagnostics(
    connection: sqlite3.Connection,
    candidate: _Candidate,
) -> tuple[CodeReviewDiagnostic, ...]:
    rows = connection.execute(
        """SELECT d.diagnostic_id,d.code,d.source,d.tool_name,d.tool_version,d.confirmed,
        d.confidence,d.metadata_json FROM diagnostics d
        JOIN symbols s ON s.version_id=d.version_id
         AND s.start_byte=d.start_byte AND s.end_byte=d.end_byte
        WHERE s.symbol_id=? AND d.code IN ('high_complexity','long_function')
        ORDER BY CASE d.code WHEN 'high_complexity' THEN 0 ELSE 1 END,
        d.diagnostic_id""",
        (candidate.symbol_id,),
    ).fetchall()
    diagnostics: list[CodeReviewDiagnostic] = []
    for row in rows:
        metadata = json.loads(str(row["metadata_json"]))
        if not isinstance(metadata, dict):
            raise ValueError("code diagnostic metadata must be a JSON object")
        raw_code = str(row["code"])
        if raw_code not in {"high_complexity", "long_function"}:
            raise ValueError("code review diagnostic is unsupported")
        code = cast(Literal["high_complexity", "long_function"], raw_code)
        value = candidate.complexity if code == "high_complexity" else candidate.function_lines
        threshold_value = metadata.get("threshold")
        threshold = (
            None
            if isinstance(threshold_value, bool) or not isinstance(threshold_value, int)
            else threshold_value
        )
        diagnostics.append(
            CodeReviewDiagnostic(
                diagnostic_id=int(row["diagnostic_id"]),
                code=code,
                value=value,
                threshold=threshold,
                source=str(row["source"]),
                tool_name=str(row["tool_name"]),
                tool_version=str(row["tool_version"]),
                confirmed=bool(row["confirmed"]),
                confidence=float(row["confidence"]),
            )
        )
    return tuple(diagnostics)


def _callers(
    connection: sqlite3.Connection,
    symbol_id: int,
    project_root: str | None,
) -> tuple[CodeReviewCaller, ...]:
    rows = connection.execute(
        """SELECT source_file.current_path,source_symbol.qualified_name,
        r.start_line,r.end_line,r.confidence,r.evidence
        FROM code_references r
        JOIN file_versions source_version ON source_version.version_id=r.version_id
        JOIN files source_file
          ON source_file.current_version_id=source_version.version_id
         AND source_file.status='current'
        LEFT JOIN symbols source_symbol
          ON source_symbol.symbol_id=r.source_symbol_id
         AND source_symbol.version_id=source_version.version_id
        WHERE r.target_symbol_id=? AND r.kind='call' AND r.confirmed=1
          AND source_version.invalidated_ns IS NULL
        ORDER BY source_file.current_path COLLATE NOCASE,
        COALESCE(source_symbol.qualified_name,''),r.start_line,r.reference_id
        LIMIT ?""",
        (symbol_id, CODE_REVIEW_CALLER_EXAMPLES),
    ).fetchall()
    return tuple(
        CodeReviewCaller(
            path=str(row["current_path"]),
            symbol=(None if row["qualified_name"] is None else str(row["qualified_name"])),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            confidence=float(row["confidence"]),
            provenance=str(row["evidence"]),
            path_convention_role=classify_source_role(
                str(row["current_path"]),
                project_root,
            ),
        )
        for row in rows
    )


def _impact(
    connection: sqlite3.Connection,
    candidate: _Candidate,
) -> CodeReviewImpact:
    rows = connection.execute(
        """SELECT source_file.current_path,
        COUNT(*) AS call_sites
        FROM code_references r
        JOIN file_versions source_version ON source_version.version_id=r.version_id
        JOIN files source_file
          ON source_file.current_version_id=source_version.version_id
         AND source_file.status='current'
        WHERE r.target_symbol_id=? AND r.kind='call' AND r.confirmed=1
          AND source_version.invalidated_ns IS NULL
        GROUP BY source_version.version_id,COALESCE(r.source_symbol_id,0),
                 source_file.current_path
        ORDER BY source_file.current_path COLLATE NOCASE,
                 COALESCE(r.source_symbol_id,0)""",
        (candidate.symbol_id,),
    ).fetchall()
    caller_counts: dict[SourceRole, int] = {
        "production": 0,
        "test": 0,
        "fixture": 0,
        "tool": 0,
        "compatibility": 0,
    }
    modules_by_role: dict[SourceRole, set[str]] = {
        "production": set(),
        "test": set(),
        "fixture": set(),
        "tool": set(),
        "compatibility": set(),
    }
    call_sites = 0
    for row in rows:
        path = str(row["current_path"])
        role = classify_source_role(path, candidate.project_root)
        caller_counts[role] += 1
        modules_by_role[role].add(path)
        call_sites += int(row["call_sites"])
    if len(rows) != candidate.resolved_static_callers:
        raise ValueError("separated caller evidence disagrees with ranked caller count")
    if call_sites != candidate.incoming_calls:
        raise ValueError("separated call sites disagree with ranked incoming calls")
    consumer_modules = set().union(*modules_by_role.values())
    test_modules = modules_by_role["test"] | modules_by_role["fixture"]
    return CodeReviewImpact(
        call_sites=call_sites,
        path_convention_production_callers=caller_counts["production"],
        path_convention_test_callers=caller_counts["test"],
        path_convention_fixture_callers=caller_counts["fixture"],
        path_convention_tool_callers=caller_counts["tool"],
        path_convention_compatibility_callers=caller_counts["compatibility"],
        resolved_static_consumer_files=len(consumer_modules),
        path_convention_production_consumer_files=len(modules_by_role["production"]),
        path_convention_test_consumer_files=len(test_modules),
        consumer_file_examples=tuple(
            sorted(consumer_modules, key=lambda value: (value.casefold(), value))[
                :CODE_REVIEW_CONSUMER_MODULE_EXAMPLES
            ]
        ),
    )


def _outgoing_calls(
    connection: sqlite3.Connection,
    candidate: _Candidate,
) -> tuple[tuple[str, ...], bool]:
    rows = connection.execute(
        """SELECT DISTINCT r.name,r.target_hint
        FROM code_references r
        WHERE r.version_id=? AND r.source_symbol_id=? AND r.kind='call'
        ORDER BY r.name COLLATE NOCASE,COALESCE(r.target_hint,'') COLLATE NOCASE
        LIMIT ?""",
        (
            candidate.version_id,
            candidate.symbol_id,
            CODE_REVIEW_OUTGOING_CALL_LIMIT + 1,
        ),
    ).fetchall()
    truncated = len(rows) > CODE_REVIEW_OUTGOING_CALL_LIMIT
    values: set[str] = set()
    for row in rows[:CODE_REVIEW_OUTGOING_CALL_LIMIT]:
        values.add(str(row["name"]))
        if row["target_hint"] is not None:
            values.add(str(row["target_hint"]))
    return tuple(sorted(values, key=lambda value: (value.casefold(), value))), truncated


def _category(candidate: _Candidate) -> FindingCategory:
    if candidate.high_complexity and candidate.long_function:
        return "complex_and_long_hotspot"
    if candidate.high_complexity:
        return "high_complexity_hotspot"
    return "long_function_hotspot"


def _hotspot_id(candidate: _Candidate) -> str:
    payload = canonical_json(
        {
            "volume_id": candidate.volume_id,
            "physical_file_id": candidate.physical_file_id,
            "symbol": candidate.symbol,
            "symbol_kind": candidate.symbol_kind,
        }
    )
    return "code-hotspot-v1:xxh3_128:" + fingerprint_text(payload).xxh3_128


def _finding_id(candidate: _Candidate) -> str:
    payload = canonical_json(
        {
            "hotspot_id": _hotspot_id(candidate),
            "ranking": CODE_REVIEW_RANKING,
            "actionability": CODE_REVIEW_ACTIONABILITY,
        }
    )
    return "code-review-finding-v2:xxh3_128:" + fingerprint_text(payload).xxh3_128


def _finding(
    connection: sqlite3.Connection,
    candidate: _Candidate,
    rank: int,
) -> CodeReviewFinding:
    impact = _impact(connection, candidate)
    outgoing_calls, outgoing_calls_truncated = _outgoing_calls(connection, candidate)
    assessment = assess_code_review_actionability(
        CodeReviewActionabilityInput(
            path=candidate.path,
            symbol=candidate.symbol,
            root=candidate.project_root,
            complexity_ratio_basis_points=candidate.complexity_ratio_basis_points,
            length_ratio_basis_points=candidate.length_ratio_basis_points,
            path_convention_production_callers=impact.path_convention_production_callers,
            path_convention_test_callers=impact.path_convention_test_callers,
            path_convention_fixture_callers=impact.path_convention_fixture_callers,
            path_convention_tool_callers=impact.path_convention_tool_callers,
            path_convention_compatibility_callers=(impact.path_convention_compatibility_callers),
            resolved_static_consumer_files=impact.resolved_static_consumer_files,
            outgoing_calls=outgoing_calls,
            outgoing_calls_truncated=outgoing_calls_truncated,
        )
    )
    reasons: list[str] = []
    if candidate.high_complexity:
        reasons.append(f"confirmed_cyclomatic_complexity:{candidate.complexity}")
    if candidate.long_function:
        reasons.append(f"confirmed_function_lines:{candidate.function_lines}")
    reasons.append(f"resolved_static_callers:{candidate.resolved_static_callers}")
    return CodeReviewFinding(
        finding_id=_finding_id(candidate),
        hotspot_id=_hotspot_id(candidate),
        rank=rank,
        category=_category(candidate),
        path=candidate.path,
        symbol=candidate.symbol,
        symbol_kind=candidate.symbol_kind,
        signature=candidate.signature,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        start_column=candidate.start_column,
        end_column=candidate.end_column,
        start_byte=candidate.start_byte,
        end_byte=candidate.end_byte,
        complexity=candidate.complexity,
        function_lines=candidate.function_lines,
        complexity_ratio_basis_points=candidate.complexity_ratio_basis_points,
        length_ratio_basis_points=candidate.length_ratio_basis_points,
        score_basis_points=candidate.score_basis_points,
        incoming_references=candidate.incoming_references,
        incoming_calls=candidate.incoming_calls,
        resolved_static_callers=candidate.resolved_static_callers,
        impact=impact,
        path_convention_role=assessment.path_convention_role,
        construction=assessment.construction,
        actionability=assessment.actionability,
        change_risk=assessment.change_risk,
        recommended_change=assessment.recommended_change,
        epistemic_state=assessment.epistemic_state,
        actionability_evidence=assessment.evidence,
        contracts_to_preserve=assessment.contracts_to_preserve,
        recommended_validation=assessment.recommended_validation,
        analyzer_id=candidate.analyzer_id,
        analyzer_version=candidate.analyzer_version,
        file_xxh3_128=candidate.file_xxh3_128,
        file_xxh3_64_guard=candidate.file_xxh3_64_guard,
        diagnostics=_diagnostics(connection, candidate),
        callers=_callers(connection, candidate.symbol_id, candidate.project_root),
        reasons=tuple(reasons),
    )


def _read_review(path: Path, *, limit: int) -> _ReviewRead:
    with readonly_code_database(path) as connection:
        validate_code_schema(connection)
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != CODE_SCHEMA_VERSION:
            raise RuntimeError(f"code state schema {schema_version} is unsupported for review")
        latest_run = _latest_run(connection)
        if latest_run is not None and not _current_versions_match_latest_run(
            connection,
            latest_run,
        ):
            raise CodeReviewEvidenceResolutionError(
                "current_code_projection_not_owned_by_latest_completed_run"
            )
        rows = connection.execute(
            _CANDIDATE_SQL,
            (CODE_REVIEW_MAX_CANDIDATES,),
        ).fetchall()
        candidates = tuple(_candidate(row) for row in rows)
        total_candidates = int(rows[0]["total_candidates"]) if rows else 0
        selected = _select_candidates(candidates, limit=limit)
        findings = tuple(
            _finding(connection, candidate, rank)
            for rank, candidate in enumerate(selected, start=1)
        )
        probable_dead = int(
            connection.execute(
                """SELECT COUNT(*) FROM diagnostics d
                JOIN file_versions v ON v.version_id=d.version_id
                JOIN files f ON f.current_version_id=v.version_id
                WHERE f.status='current' AND v.invalidated_ns IS NULL
                AND v.language='python' AND d.code='probable_dead_symbol'"""
            ).fetchone()[0]
        )
        call_edges, resolved_call_edges = connection.execute(
            """SELECT COUNT(*),COUNT(r.target_symbol_id) FROM code_references r
            JOIN file_versions v ON v.version_id=r.version_id
            JOIN files f ON f.current_version_id=v.version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.language='python' AND r.kind='call' AND r.confirmed=1"""
        ).fetchone()
        current_python_files, complete_python_files = connection.execute(
            """SELECT COUNT(*),SUM(CASE WHEN v.analysis_status='complete'
            THEN 1 ELSE 0 END) FROM file_versions v
            JOIN files f ON f.current_version_id=v.version_id
            WHERE f.status='current' AND v.invalidated_ns IS NULL
            AND v.language='python' AND v.generated=0 AND v.vendored=0"""
        ).fetchone()
        current_python = int(current_python_files)
        complete_python = int(complete_python_files or 0)
        analysis_run_id = -1 if latest_run is None else latest_run.analysis_run_id
        external_evidence = read_external_evidence(
            connection,
            analysis_run_id,
            enforce_current_runtime=True,
        )[0]
        external_evidence_suite = read_external_evidence_suite(
            connection,
            analysis_run_id,
            enforce_current_runtime=True,
        )
        architecture = read_code_architecture_analysis(
            connection,
            analysis_run_id,
            database=str(path),
        )
        test_coverage = read_code_coverage_analysis(
            connection,
            analysis_run_id,
            database=str(path),
        )
        provider_evidence = read_external_provider_evidence(connection, analysis_run_id)
        engineering_analytics = analyze_code_engineering(
            architecture,
            test_coverage,
            provider_evidence,
            database=str(path),
            analysis_run_id=analysis_run_id,
        )
        unused_analysis = read_code_unused_analysis(
            connection,
            analysis_run_id,
            database=str(path),
        )
        supply_chain = read_code_supply_chain_analysis(
            connection,
            analysis_run_id,
            database=str(path),
        )
        change_evolution = read_code_change_evolution_analysis(
            connection,
            database=str(path),
            limit=limit,
        )
        interface_surface = (
            abstained_code_interface_surface("code_run_missing", database=str(path))
            if latest_run is None
            else read_code_interface_surface_analysis(
                connection,
                analysis_run_id=analysis_run_id,
                processing_signature=latest_run.processing_signature,
                database=str(path),
                limit=limit,
            )
        )
    return _ReviewRead(
        latest_run=latest_run,
        coverage=CodeReviewCoverage(
            current_python_files=current_python,
            complete_python_files=complete_python,
            incomplete_python_files=current_python - complete_python,
            candidate_hotspots=total_candidates,
            enumerated_hotspots=len(candidates),
            probable_dead_suppressed=probable_dead,
            call_edges=int(call_edges),
            resolved_call_edges=int(resolved_call_edges),
        ),
        findings=findings,
        enumeration_truncated=total_candidates > len(candidates),
        external_evidence=external_evidence,
        external_evidence_suite=external_evidence_suite,
        architecture=architecture,
        test_coverage=test_coverage,
        engineering_analytics=engineering_analytics,
        unused_analysis=unused_analysis,
        supply_chain=supply_chain,
        provider_evidence=provider_evidence,
        change_evolution=change_evolution,
        interface_surface=interface_surface,
    )


def _abstained(path: Path, reason: str) -> CodeReviewResult:
    return CodeReviewResult(
        database=str(path),
        status="abstained",
        reason=reason,
        ranking=CODE_REVIEW_RANKING,
        actionability_version=CODE_REVIEW_ACTIONABILITY,
        recommendation_status="not_evaluated",
        recommendation_reason=reason,
        planning_version=CODE_REVIEW_PLANNING,
        work_package_status="not_evaluated",
        work_package_reason=reason,
        snapshot=None,
        coverage=None,
        findings=(),
        recommendations=(),
        work_packages=(),
        external_evidence=None,
        external_evidence_suite=None,
        architecture=None,
        test_coverage=None,
        limitations=(),
        digest=None,
        unused_analysis=None,
        supply_chain=None,
        engineering_analytics=None,
    )


def review_code_state(
    state_directory: Path,
    *,
    limit: int = CODE_REVIEW_LIMIT,
) -> CodeReviewResult:
    """Return a bounded maintenance shortlist without writing any owner."""

    if isinstance(limit, bool) or not 1 <= limit <= CODE_REVIEW_MAX_LIMIT:
        raise ValueError(f"code review limit must be between 1 and {CODE_REVIEW_MAX_LIMIT}")
    state_directory = Path(state_directory)
    path = state_directory / "code.sqlite3"
    require_sqlite_sidecars_absent(path)
    if not path.is_file():
        return _abstained(path, "code_state_missing")
    try:
        read = _read_review(path, limit=limit)
    except CodeReviewEvidenceResolutionError:
        return _abstained(path, "code_review_evidence_unresolvable")
    if read.latest_run is None:
        return _abstained(path, "code_run_missing")
    if read.latest_run.status != "completed":
        return _abstained(path, f"code_run_not_completed:{read.latest_run.status}")
    status = read_self_analysis_status(state_directory, read.latest_run)
    reason, freshness, freshness_limitation = code_review_eligibility(status)
    if reason is not None:
        return _abstained(path, reason)
    if status is None or freshness is None:
        raise AssertionError("eligible code review requires self-analysis status")
    limitations = [
        "raw_ranking_score_is_not_calibrated_risk",
        "structural_hotspot_opens_a_question_not_a_change_decision",
        "intentional_complexity_requires_human_confirmation",
        "static_call_resolution_is_partial",
        "dynamic_dispatch_is_not_observed",
        "work_package_relationships_are_bounded_to_two_static_call_hops",
        "probable_dead_symbol_is_suppressed_uncalibrated_evidence",
    ]
    if freshness_limitation is not None:
        limitations.insert(0, freshness_limitation)
    if read.enumeration_truncated:
        limitations.append("candidate_enumeration_truncated")
    if read.external_evidence.status != "ready":
        limitations.append(
            "ruff_external_evidence_not_ready:"
            + (read.external_evidence.reason or read.external_evidence.status)
        )
    elif read.external_evidence.gate == "baseline":
        limitations.append("ruff_external_evidence_baseline_only")
    for provider in read.external_evidence_suite.providers:
        if provider.status != "ready":
            limitations.append(
                f"provider_not_ready:{provider.provider_id}:{provider.reason or provider.status}"
            )
        elif provider.gate == "baseline":
            limitations.append(f"provider_baseline_only:{provider.provider_id}")
    if read.architecture.status != "ready":
        limitations.append(
            "architecture_not_ready:" + (read.architecture.reason or read.architecture.status)
        )
    if read.test_coverage.status != "ready":
        limitations.append(
            "test_coverage_not_ready:" + (read.test_coverage.reason or read.test_coverage.status)
        )
    if read.engineering_analytics.status != "ready":
        limitations.append(
            "engineering_analytics_not_ready:"
            + (read.engineering_analytics.reason or read.engineering_analytics.status)
        )
    if read.unused_analysis.status != "ready":
        limitations.append(
            "unused_analysis_not_ready:"
            + (read.unused_analysis.reason or read.unused_analysis.status)
        )
    if read.supply_chain.status != "ready":
        limitations.append(
            "supply_chain_not_ready:" + (read.supply_chain.reason or read.supply_chain.status)
        )
    snapshot = CodeReviewSnapshot(
        analysis_run_id=read.latest_run.analysis_run_id,
        framework_run_id=read.latest_run.framework_run_id,
        scan_id=read.latest_run.scan_id,
        processing_signature=read.latest_run.processing_signature,
        root=self_analysis_manifest_root(status),
        freshness=freshness,
        current=status.freshness.current,
        journal_status=status.freshness.journal_status,
    )
    recommendations: tuple[CodeReviewRecommendation, ...] = ()
    recommendation_status: RecommendationStatus = "abstained"
    recommendation_reason = "no_evidence_ready_change_decision_within_bounded_findings"
    work_packages, work_package_status, work_package_reason = plan_code_review_work_packages(
        read.findings,
        (),
        (),
        architecture=read.architecture,
        architecture_root=snapshot.root,
        test_coverage=read.test_coverage,
        engineering_analytics=read.engineering_analytics,
        unused_analysis=read.unused_analysis,
        supply_chain=read.supply_chain,
    )
    try:
        with readonly_code_database(path) as connection:
            validate_code_schema(connection)
            structural_analysis, _structural_specs, _structural_evaluations = (
                resolve_code_review_questions(
                    connection,
                    read.findings,
                    snapshot,
                    class_limit=limit,
                )
            )
    except CodeReviewEvidenceResolutionError:
        return _abstained(path, "code_review_evidence_unresolvable")
    document_state: Path | None = None
    try:
        from .app_paths import self_analysis_data_directory

        if state_directory.resolve() == self_analysis_data_directory().resolve():
            document_state = self_analysis_data_directory().parent / "state"
            state_projection = analyze_text_semantic_projection(
                document_state,
                source_version=CODE_REVIEW_SCHEMA,
            )
            state_topology = analyze_text_terminal_publication(
                document_state,
                source_version=CODE_REVIEW_SCHEMA,
            )
            capability_reachability = analyze_capability_reachability(
                document_state,
                source_version=CODE_REVIEW_SCHEMA,
            )
        else:
            state_projection = abstained_code_state_projection(
                "document_state_not_configured_for_noncanonical_code_review"
            )
            state_topology = abstained_code_state_topology(
                "document_state_not_configured_for_noncanonical_code_review",
                source_version=CODE_REVIEW_SCHEMA,
            )
            capability_reachability = abstained_capability_reachability(
                "document_state_not_configured_for_noncanonical_code_review",
                source_version=CODE_REVIEW_SCHEMA,
            )
    except (OSError, RuntimeError, ValueError):
        state_projection = abstained_code_state_projection("document_state_boundary_unresolvable")
        state_topology = abstained_code_state_topology(
            "document_state_boundary_unresolvable",
            source_version=CODE_REVIEW_SCHEMA,
        )
        capability_reachability = abstained_capability_reachability(
            "document_state_boundary_unresolvable",
            source_version=CODE_REVIEW_SCHEMA,
        )
    if document_state is not None and state_topology.status == "ready":
        try:
            resolve_state_topology_questions(document_state, state_topology, rank=1)
        except CodeStateTopologyResolutionError:
            state_topology = abstained_code_state_topology(
                "state_topology_changed_during_review",
                source_version=CODE_REVIEW_SCHEMA,
            )
    assurance = analyze_code_assurance(
        read.test_coverage,
        read.provider_evidence,
        snapshot_id=snapshot.processing_signature,
        snapshot_freshness=snapshot.freshness,
        limit=limit,
    )
    analyzer_effectiveness = analyze_code_analyzer_effectiveness(
        path,
        Path(snapshot.root),
        source_version=CODE_REVIEW_SCHEMA,
        snapshot_freshness=snapshot.freshness,
        findings_observed=len(read.findings),
        recommendations_observed=len(recommendations),
        work_packages_observed=len(work_packages),
        providers=read.external_evidence_suite.providers,
    )
    question_specs, question_evaluations = expected_integrated_code_review_questions(
        read.findings,
        snapshot,
        structural_analysis,
        state_projection=state_projection,
        state_topology=state_topology,
        change_evolution=read.change_evolution,
        architecture=read.architecture,
        assurance=assurance,
        supply_chain=read.supply_chain,
        interface_surface=read.interface_surface,
        capability_reachability=capability_reachability,
        analyzer_effectiveness=analyzer_effectiveness,
    )
    limitation_tuple = tuple(limitations)
    return CodeReviewResult(
        database=str(path),
        status="ready",
        reason=None,
        ranking=CODE_REVIEW_RANKING,
        actionability_version=CODE_REVIEW_ACTIONABILITY,
        recommendation_status=recommendation_status,
        recommendation_reason=recommendation_reason,
        planning_version=CODE_REVIEW_PLANNING,
        work_package_status=work_package_status,
        work_package_reason=work_package_reason,
        snapshot=snapshot,
        coverage=read.coverage,
        findings=read.findings,
        recommendations=recommendations,
        work_packages=work_packages,
        external_evidence=read.external_evidence,
        external_evidence_suite=read.external_evidence_suite,
        architecture=read.architecture,
        test_coverage=read.test_coverage,
        unused_analysis=read.unused_analysis,
        supply_chain=read.supply_chain,
        engineering_analytics=read.engineering_analytics,
        structural_analysis=structural_analysis,
        state_projection=state_projection,
        state_topology=state_topology,
        change_evolution=read.change_evolution,
        assurance=assurance,
        capability_reachability=capability_reachability,
        analyzer_effectiveness=analyzer_effectiveness,
        interface_surface=read.interface_surface,
        question_specs=question_specs,
        question_evaluations=question_evaluations,
        limitations=limitation_tuple,
        digest=build_code_review_digest(
            snapshot,
            read.coverage,
            read.findings,
            ranking=CODE_REVIEW_RANKING,
            actionability_version=CODE_REVIEW_ACTIONABILITY,
            recommendation_status=recommendation_status,
            recommendation_reason=recommendation_reason,
            recommendations=recommendations,
            planning_version=CODE_REVIEW_PLANNING,
            work_package_status=work_package_status,
            work_package_reason=work_package_reason,
            work_packages=work_packages,
            external_evidence=read.external_evidence,
            external_evidence_suite=read.external_evidence_suite,
            architecture=read.architecture,
            test_coverage=read.test_coverage,
            engineering_analytics=read.engineering_analytics,
            structural_analysis=structural_analysis,
            state_projection=state_projection,
            state_topology=state_topology,
            change_evolution=read.change_evolution,
            assurance=assurance,
            capability_reachability=capability_reachability,
            analyzer_effectiveness=analyzer_effectiveness,
            interface_surface=read.interface_surface,
            unused_analysis=read.unused_analysis,
            supply_chain=read.supply_chain,
            question_specs=question_specs,
            question_evaluations=question_evaluations,
            limitations=limitation_tuple,
        ),
    )


__all__ = [
    "CODE_REVIEW_LIMIT",
    "CODE_REVIEW_MAX_LIMIT",
    "CODE_REVIEW_MAX_PER_FILE",
    "CODE_REVIEW_RANKING",
    "CODE_REVIEW_RECOMMENDATION_LIMIT",
    "CODE_REVIEW_SCHEMA",
    "CodeReviewCaller",
    "CodeReviewCoverage",
    "CodeReviewDiagnostic",
    "CodeReviewDigest",
    "CodeReviewFinding",
    "CodeReviewImpact",
    "CodeReviewRecommendation",
    "CodeReviewResult",
    "CodeReviewSnapshot",
    "review_code_state",
]
