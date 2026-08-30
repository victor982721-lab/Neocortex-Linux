"""Read-only self-audit of Code publication freshness, cost, and calibration.

The projection compares the latest completed Code publication with the current
Git-visible worktree.  It reads bytes itself, uses collision-guarded content
fingerprints, and treats ignored files and repositories without a resolvable
Git inventory as explicit limits.  It never converts freshness, provider
counts, or elapsed time into a quality score or a defect probability.
"""

from __future__ import annotations
import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

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
from .external_evidence_models import ExternalProviderStatus
from neocortex.semantic.semantic_models import fingerprint_bytes
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database

CODE_ANALYZER_EFFECTIVENESS_SCHEMA = "neocortex.code-analyzer-effectiveness/v1"
CODE_ANALYZER_EFFECTIVENESS_POLICY = "latest-publication-vs-git-visible-worktree-v1"
CODE_ANALYZER_EFFECTIVENESS_MAX_FILES = 20_000
CODE_ANALYZER_EFFECTIVENESS_MAX_BYTES = 2 * 1024 * 1024 * 1024
CODE_ANALYZER_EFFECTIVENESS_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
CODE_ANALYZER_EFFECTIVENESS_EXAMPLE_LIMIT = 50

ANALYZER_FRESHNESS_QUESTION = AnalysisQuestionSpec(
    question_id="analyzer.latest_publication_is_compared_to_git_visible_worktree",
    version="v1",
    subject_kinds=("analyzer",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "latest_completed_publication_observed",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "git_visible_worktree_compared_by_content",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "ignored_and_external_project_scope_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "comparable_reanalysis_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "published_code_evidence_matches_the_current_visible_checkout",
        "published_code_evidence_is_stale_or_scope_incomplete",
    ),
    counterevidence_rules=(
        "git_ignored_files_are_outside_the_visible_inventory_claim",
        "files_from_other_project_roots_are_counted_but_not_compared_to_this_checkout",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "rerun_bounded_self_analysis_then_compare_again",
            "experiment",
            "Run one bounded self-analysis publication and require the same comparison to converge.",
        ),
        AnalysisNextActionSpec(
            "inspect_checkout_scope_deltas",
            "counterevidence_search",
            "Inspect bounded missing, changed, unindexed and non-regular path examples.",
        ),
    ),
)

ANALYZER_CALIBRATION_QUESTION = AnalysisQuestionSpec(
    question_id="analyzer.effectiveness_requires_independent_outcome_calibration",
    version="v1",
    subject_kinds=("analyzer",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "analyzer_output_and_cost_observed",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "independent_human_decision_or_escaped_defect_labels_linked",
            "decision",
            "supporting",
            ("internal_relation", "external_relation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "negative_controls_and_holdout_evaluated",
            "decision",
            "counterevidence",
            ("experiment_result",),
        ),
        AnalysisEvidenceRequirementSpec(
            "effectiveness_calibration_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "the_analyzer_improves_human_decisions_per_attention_minute",
        "the_analyzer_adds_unmeasured_noise_or_is_being_gamed",
    ),
    counterevidence_rules=(
        "findings_without_independent_outcomes_do_not_establish_precision_or_recall",
        "passing_internal_tests_are_not_holdout_outcomes",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "link_review_task_outcomes_without_copying_human_decisions",
            "characterization",
            "Link advisory Code evaluations to existing human ReviewTask outcome receipts.",
        ),
        AnalysisNextActionSpec(
            "run_seeded_holdout_and_negative_control_calibration",
            "experiment",
            "Evaluate a held-out seeded corpus and explicit acceptable controls.",
        ),
    ),
)

_LIMITATIONS = (
    "git_visible_inventory_excludes_ignored_files",
    "only_the_selected_source_root_is_compared",
    "files_from_other_code_project_roots_are_counted_not_compared",
    "content_equality_does_not_prove_semantic_analysis_completeness",
    "filesystem_snapshot_is_best_effort_not_kernel_atomic",
    "provider_counts_do_not_measure_provider_precision",
    "human_decisions_and_escaped_defects_are_not_linked_in_v1",
    "precision_recall_and_decision_rate_are_not_claimed_without_independent_labels",
    "human_decision_not_owned_by_code_analysis",
)


class CodeAnalyzerEffectivenessResolutionError(ValueError):
    """The live checkout or Code publication cannot be resolved safely."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _nonnegative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _texts(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, value) for value in values)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{label} must be sorted and unique")
    return result


@dataclass(frozen=True, slots=True)
class CodeAnalyzerEffectivenessAnalysis:
    analysis_id: str
    database: str
    source_root: str
    source_version: str
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    analysis_run_id: int | None
    framework_run_id: int | None
    processing_signature: str | None
    snapshot_freshness: Literal["current", "publication_only", "unknown"]
    inventory_observation: (
        Literal[
            "exact",
            "content_stale",
            "scope_incomplete",
            "content_stale_and_scope_incomplete",
        ]
        | None
    )
    recorded_current_files: int
    scoped_recorded_files: int
    recorded_files_outside_root: int
    git_visible_files: int
    exact_content_files: int
    metadata_changed_content_equal_files: int
    content_changed_files: int
    missing_recorded_files: int
    unindexed_git_visible_files: int
    nonregular_files: int
    bytes_compared: int
    content_changed_examples: tuple[str, ...]
    missing_recorded_examples: tuple[str, ...]
    unindexed_git_visible_examples: tuple[str, ...]
    nonregular_examples: tuple[str, ...]
    examples_truncated: bool
    duration_ms: int | None
    analyze_ms: int | None
    graph_ms: int | None
    external_ms: int | None
    candidates: int | None
    processed: int | None
    cache_hits: int | None
    errors: int | None
    providers_observed: int
    providers_ready: int
    providers_not_ready: int
    findings_observed: int
    recommendations_observed: int
    work_packages_observed: int
    calibration_status: Literal["not_established"]
    independent_outcome_labels: int
    precision_at_k: None
    recall: None
    finding_to_decision_rate: None
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for text_label, text_value in (
            ("analyzer effectiveness id", self.analysis_id),
            ("analyzer database", self.database),
            ("analyzer source root", self.source_root),
            ("analyzer source version", self.source_version),
        ):
            _required_text(text_label, text_value)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("analyzer effectiveness status is invalid")
        if self.policy_id != CODE_ANALYZER_EFFECTIVENESS_POLICY:
            raise ValueError("analyzer effectiveness policy is invalid")
        if self.snapshot_freshness not in {"current", "publication_only", "unknown"}:
            raise ValueError("analyzer effectiveness freshness is invalid")
        for count_label, count_value in (
            ("recorded current files", self.recorded_current_files),
            ("scoped recorded files", self.scoped_recorded_files),
            ("recorded files outside root", self.recorded_files_outside_root),
            ("Git-visible files", self.git_visible_files),
            ("exact content files", self.exact_content_files),
            ("metadata-only files", self.metadata_changed_content_equal_files),
            ("content changed files", self.content_changed_files),
            ("missing recorded files", self.missing_recorded_files),
            ("unindexed Git-visible files", self.unindexed_git_visible_files),
            ("nonregular files", self.nonregular_files),
            ("bytes compared", self.bytes_compared),
            ("providers observed", self.providers_observed),
            ("providers ready", self.providers_ready),
            ("providers not ready", self.providers_not_ready),
            ("findings observed", self.findings_observed),
            ("recommendations observed", self.recommendations_observed),
            ("work packages observed", self.work_packages_observed),
            ("independent outcome labels", self.independent_outcome_labels),
        ):
            _nonnegative(count_label, count_value)
        for example_label, example_values in (
            ("changed file example", self.content_changed_examples),
            ("missing file example", self.missing_recorded_examples),
            ("unindexed file example", self.unindexed_git_visible_examples),
            ("nonregular file example", self.nonregular_examples),
        ):
            _texts(example_label, example_values)
            if len(example_values) > CODE_ANALYZER_EFFECTIVENESS_EXAMPLE_LIMIT:
                raise ValueError("analyzer effectiveness examples exceed their bound")
        if not isinstance(self.examples_truncated, bool):
            raise ValueError("analyzer effectiveness truncation flag must be boolean")
        if self.providers_ready + self.providers_not_ready != self.providers_observed:
            raise ValueError("analyzer provider counts are not a partition")
        if self.scoped_recorded_files + self.recorded_files_outside_root != (
            self.recorded_current_files
        ):
            raise ValueError("analyzer recorded file scope is not a partition")
        for optional_metric in (
            self.duration_ms,
            self.analyze_ms,
            self.graph_ms,
            self.external_ms,
            self.candidates,
            self.processed,
            self.cache_hits,
            self.errors,
        ):
            if optional_metric is not None:
                _nonnegative("analyzer run metric", optional_metric)
        if (
            self.calibration_status != "not_established"
            or self.independent_outcome_labels != 0
            or any(
                value is not None
                for value in (self.precision_at_k, self.recall, self.finding_to_decision_rate)
            )
        ):
            raise ValueError("analyzer effectiveness cannot invent calibration outcomes")
        if self.limitations != _LIMITATIONS:
            raise ValueError("analyzer effectiveness limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("analyzer effectiveness must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("analyzer effectiveness abstention reason", self.reason, maximum=256)
            if (
                any(
                    value is not None
                    for value in (
                        self.analysis_run_id,
                        self.framework_run_id,
                        self.processing_signature,
                        self.inventory_observation,
                        self.duration_ms,
                        self.analyze_ms,
                        self.graph_ms,
                        self.external_ms,
                        self.candidates,
                        self.processed,
                        self.cache_hits,
                        self.errors,
                    )
                )
                or any(
                    (
                        self.recorded_current_files,
                        self.scoped_recorded_files,
                        self.recorded_files_outside_root,
                        self.git_visible_files,
                        self.exact_content_files,
                        self.metadata_changed_content_equal_files,
                        self.content_changed_files,
                        self.missing_recorded_files,
                        self.unindexed_git_visible_files,
                        self.nonregular_files,
                        self.bytes_compared,
                        self.providers_observed,
                        self.providers_ready,
                        self.providers_not_ready,
                        self.findings_observed,
                        self.recommendations_observed,
                        self.work_packages_observed,
                    )
                )
                or self.content_changed_examples
                or self.missing_recorded_examples
                or self.unindexed_git_visible_examples
                or self.nonregular_examples
                or self.examples_truncated
                or self.snapshot_freshness != "unknown"
            ):
                raise ValueError("abstained analyzer effectiveness cannot assert partial evidence")
        else:
            if self.reason is not None:
                raise ValueError("ready analyzer effectiveness cannot carry an abstention reason")
            for run_label, run_value in (
                ("analysis run", self.analysis_run_id),
                ("framework run", self.framework_run_id),
            ):
                if isinstance(run_value, bool) or not isinstance(run_value, int) or run_value < 1:
                    raise ValueError(f"{run_label} must be positive")
            _required_text("analyzer processing signature", self.processing_signature)
            content_stale = bool(
                self.content_changed_files or self.missing_recorded_files or self.nonregular_files
            )
            scope_incomplete = bool(self.unindexed_git_visible_files)
            expected_observation = (
                "content_stale_and_scope_incomplete"
                if content_stale and scope_incomplete
                else "content_stale"
                if content_stale
                else "scope_incomplete"
                if scope_incomplete
                else "exact"
            )
            if self.inventory_observation != expected_observation:
                raise ValueError("analyzer inventory observation is not derived")
            if (
                self.exact_content_files
                + self.content_changed_files
                + self.nonregular_files
                + (self.missing_recorded_files)
                != self.scoped_recorded_files
            ):
                raise ValueError("analyzer scoped file comparison is not exhaustive")
            if self.metadata_changed_content_equal_files > self.exact_content_files:
                raise ValueError("metadata-only count exceeds content-equal files")
        expected_id = analysis_identity(
            "code-analyzer-effectiveness-v1",
            {key: value for key, value in asdict(self).items() if key != "analysis_id"},
        )
        if self.analysis_id != expected_id:
            raise ValueError("analyzer effectiveness identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_ANALYZER_EFFECTIVENESS_SCHEMA, **asdict(self)}


def _analysis(values: dict[str, object]) -> CodeAnalyzerEffectivenessAnalysis:
    return CodeAnalyzerEffectivenessAnalysis(
        analysis_id=analysis_identity("code-analyzer-effectiveness-v1", values),
        **values,  # type: ignore[arg-type]
    )


def abstained_code_analyzer_effectiveness(
    reason: str,
    *,
    database: str,
    source_root: str,
    source_version: str,
) -> CodeAnalyzerEffectivenessAnalysis:
    values: dict[str, object] = {
        "database": _required_text("analyzer database", database),
        "source_root": _required_text("analyzer source root", source_root),
        "source_version": _required_text("analyzer source version", source_version),
        "status": "abstained",
        "reason": _required_text("analyzer abstention reason", reason, maximum=256),
        "policy_id": CODE_ANALYZER_EFFECTIVENESS_POLICY,
        "analysis_run_id": None,
        "framework_run_id": None,
        "processing_signature": None,
        "snapshot_freshness": "unknown",
        "inventory_observation": None,
        "recorded_current_files": 0,
        "scoped_recorded_files": 0,
        "recorded_files_outside_root": 0,
        "git_visible_files": 0,
        "exact_content_files": 0,
        "metadata_changed_content_equal_files": 0,
        "content_changed_files": 0,
        "missing_recorded_files": 0,
        "unindexed_git_visible_files": 0,
        "nonregular_files": 0,
        "bytes_compared": 0,
        "content_changed_examples": (),
        "missing_recorded_examples": (),
        "unindexed_git_visible_examples": (),
        "nonregular_examples": (),
        "examples_truncated": False,
        "duration_ms": None,
        "analyze_ms": None,
        "graph_ms": None,
        "external_ms": None,
        "candidates": None,
        "processed": None,
        "cache_hits": None,
        "errors": None,
        "providers_observed": 0,
        "providers_ready": 0,
        "providers_not_ready": 0,
        "findings_observed": 0,
        "recommendations_observed": 0,
        "work_packages_observed": 0,
        "calibration_status": "not_established",
        "independent_outcome_labels": 0,
        "precision_at_k": None,
        "recall": None,
        "finding_to_decision_rate": None,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return _analysis(values)


def _git_visible_paths(root: Path) -> tuple[str, ...]:
    command = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    environment = dict(os.environ)
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "LANG": "C"})
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_unresolvable")
    if len(completed.stdout) > CODE_ANALYZER_EFFECTIVENESS_MAX_GIT_OUTPUT_BYTES:
        raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_output_bound")
    try:
        raw_paths = tuple(
            item.decode("utf-8", errors="strict") for item in completed.stdout.split(b"\0") if item
        )
    except UnicodeDecodeError as exc:
        raise CodeAnalyzerEffectivenessResolutionError(
            "git_visible_inventory_path_encoding_invalid"
        ) from exc
    normalized: list[str] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or raw != path.as_posix():
            raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_path_invalid")
        normalized.append(raw)
    result = tuple(sorted(set(normalized)))
    if len(result) != len(raw_paths):
        raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_path_repeated")
    if len(result) > CODE_ANALYZER_EFFECTIVENESS_MAX_FILES:
        raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_file_bound")
    return result


def _run_summary(row: sqlite3.Row) -> tuple[int, int, int, int]:
    try:
        raw = json.loads(str(row["summary_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodeAnalyzerEffectivenessResolutionError("analysis_run_summary_invalid") from exc
    if not isinstance(raw, dict):
        raise CodeAnalyzerEffectivenessResolutionError("analysis_run_summary_invalid")
    values: list[int] = []
    for key in ("analyze_milliseconds", "graph_milliseconds", "external_milliseconds"):
        value = raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodeAnalyzerEffectivenessResolutionError("analysis_run_summary_metric_invalid")
        values.append(value)
    started = row["started_ns"]
    completed = row["completed_ns"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (started, completed)):
        raise CodeAnalyzerEffectivenessResolutionError("analysis_run_time_invalid")
    if completed < started:
        raise CodeAnalyzerEffectivenessResolutionError("analysis_run_time_invalid")
    return (
        (completed - started) // 1_000_000,
        values[0],
        values[1],
        values[2],
    )


def _bounded(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values))[:CODE_ANALYZER_EFFECTIVENESS_EXAMPLE_LIMIT]


def analyze_code_analyzer_effectiveness(
    database: Path,
    source_root: Path,
    *,
    source_version: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    findings_observed: int,
    recommendations_observed: int,
    work_packages_observed: int,
    providers: Sequence[ExternalProviderStatus] = (),
) -> CodeAnalyzerEffectivenessAnalysis:
    """Compare one published Code owner with a stable Git-visible checkout."""

    selected_database = Path(database)
    selected_root = Path(source_root)
    try:
        root = selected_root.resolve(strict=True)
        if not root.is_dir():
            raise CodeAnalyzerEffectivenessResolutionError("source_root_not_directory")
        first_inventory = _git_visible_paths(root)
        with immutable_sqlite_database(selected_database) as connection:
            connection.row_factory = sqlite3.Row
            run = connection.execute(
                """SELECT * FROM analysis_runs WHERE status='completed'
                ORDER BY analysis_run_id DESC LIMIT 1"""
            ).fetchone()
            if run is None or int(run["errors"]) != 0:
                raise CodeAnalyzerEffectivenessResolutionError(
                    "latest_completed_analysis_run_missing_or_contains_errors"
                )
            rows = tuple(
                connection.execute(
                    """SELECT f.current_path,f.last_seen_run_id,v.size,v.mtime_ns,
                    v.raw_xxh3_128,v.raw_xxh3_64_guard
                    FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
                    WHERE f.status='current' AND v.invalidated_ns IS NULL
                    ORDER BY f.current_path"""
                ).fetchall()
            )
            if len(rows) > CODE_ANALYZER_EFFECTIVENESS_MAX_FILES:
                raise CodeAnalyzerEffectivenessResolutionError("recorded_file_inventory_bound")
            duration_ms, analyze_ms, graph_ms, external_ms = _run_summary(run)
        scoped: dict[str, sqlite3.Row] = {}
        outside = 0
        for row in rows:
            observed = Path(_required_text("recorded current path", row["current_path"]))
            try:
                relative = observed.relative_to(root).as_posix()
            except ValueError:
                outside += 1
                continue
            if relative in scoped:
                raise CodeAnalyzerEffectivenessResolutionError("recorded_path_repeated")
            scoped[relative] = row
        visible = set(first_inventory)
        recorded = set(scoped)
        missing: set[str] = set()
        changed: set[str] = set()
        nonregular: set[str] = set()
        exact = 0
        metadata_changed = 0
        bytes_compared = 0
        for relative, row in scoped.items():
            path = root / relative
            try:
                before = path.lstat()
            except FileNotFoundError:
                missing.add(relative)
                continue
            if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                nonregular.add(relative)
                continue
            if before.st_size + bytes_compared > CODE_ANALYZER_EFFECTIVENESS_MAX_BYTES:
                raise CodeAnalyzerEffectivenessResolutionError("worktree_content_byte_bound")
            raw = path.read_bytes()
            after = path.lstat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise CodeAnalyzerEffectivenessResolutionError("worktree_changed_during_read")
            bytes_compared += len(raw)
            fingerprint = fingerprint_bytes(raw)
            if (
                fingerprint.xxh3_128 == row["raw_xxh3_128"]
                and fingerprint.xxh3_64_guard == row["raw_xxh3_64_guard"]
            ):
                exact += 1
                if before.st_size != int(row["size"]) or before.st_mtime_ns != int(row["mtime_ns"]):
                    metadata_changed += 1
            else:
                changed.add(relative)
        unindexed = visible - recorded
        second_inventory = _git_visible_paths(root)
        if second_inventory != first_inventory:
            raise CodeAnalyzerEffectivenessResolutionError("git_visible_inventory_changed")
        examples_truncated = any(
            len(values) > CODE_ANALYZER_EFFECTIVENESS_EXAMPLE_LIMIT
            for values in (changed, missing, unindexed, nonregular)
        )
        ready = sum(item.status == "ready" for item in providers)
        values: dict[str, object] = {
            "database": str(selected_database),
            "source_root": str(root),
            "source_version": _required_text("analyzer source version", source_version),
            "status": "ready",
            "reason": None,
            "policy_id": CODE_ANALYZER_EFFECTIVENESS_POLICY,
            "analysis_run_id": int(run["analysis_run_id"]),
            "framework_run_id": int(run["framework_run_id"]),
            "processing_signature": str(run["processing_signature"]),
            "snapshot_freshness": snapshot_freshness,
            "inventory_observation": (
                "content_stale_and_scope_incomplete"
                if (changed or missing or nonregular) and unindexed
                else "content_stale"
                if changed or missing or nonregular
                else "scope_incomplete"
                if unindexed
                else "exact"
            ),
            "recorded_current_files": len(rows),
            "scoped_recorded_files": len(scoped),
            "recorded_files_outside_root": outside,
            "git_visible_files": len(visible),
            "exact_content_files": exact,
            "metadata_changed_content_equal_files": metadata_changed,
            "content_changed_files": len(changed),
            "missing_recorded_files": len(missing),
            "unindexed_git_visible_files": len(unindexed),
            "nonregular_files": len(nonregular),
            "bytes_compared": bytes_compared,
            "content_changed_examples": _bounded(changed),
            "missing_recorded_examples": _bounded(missing),
            "unindexed_git_visible_examples": _bounded(unindexed),
            "nonregular_examples": _bounded(nonregular),
            "examples_truncated": examples_truncated,
            "duration_ms": duration_ms,
            "analyze_ms": analyze_ms,
            "graph_ms": graph_ms,
            "external_ms": external_ms,
            "candidates": int(run["candidates"]),
            "processed": int(run["processed"]),
            "cache_hits": int(run["cache_hits"]),
            "errors": int(run["errors"]),
            "providers_observed": len(providers),
            "providers_ready": ready,
            "providers_not_ready": len(providers) - ready,
            "findings_observed": _nonnegative("findings observed", findings_observed),
            "recommendations_observed": _nonnegative(
                "recommendations observed", recommendations_observed
            ),
            "work_packages_observed": _nonnegative(
                "work packages observed", work_packages_observed
            ),
            "calibration_status": "not_established",
            "independent_outcome_labels": 0,
            "precision_at_k": None,
            "recall": None,
            "finding_to_decision_rate": None,
            "limitations": _LIMITATIONS,
            "authority": "advisory",
            "mutation_authority": False,
        }
        return _analysis(values)
    except (
        CodeAnalyzerEffectivenessResolutionError,
        FileNotFoundError,
        ImmutableSQLiteUnavailable,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        return abstained_code_analyzer_effectiveness(
            f"analyzer_effectiveness_unresolvable:{type(exc).__name__}",
            database=str(selected_database),
            source_root=str(selected_root),
            source_version=source_version,
        )


def _effectiveness_subject(analysis: CodeAnalyzerEffectivenessAnalysis) -> AnalysisSubjectRef:
    snapshot = analysis.processing_signature or analysis.analysis_id
    return AnalysisSubjectRef(
        subject_kind="analyzer",
        subject_key="analyzer:neocortex-code-self-analysis",
        display_name="NeoCortex Code self-analysis",
        source_owner_id="code",
        snapshot_id=snapshot,
        snapshot_freshness=analysis.snapshot_freshness,
        revision_id=analysis.source_version,
    )


def _evidence(
    analysis: CodeAnalyzerEffectivenessAnalysis,
    *,
    subject: AnalysisSubjectRef,
    record_kind: str,
    facts: tuple[AnalysisFact, ...],
) -> AnalysisEvidenceRef:
    projection = analysis_identity(
        "analyzer-effectiveness-source-v1",
        {"record_kind": record_kind, "facts": tuple(asdict(item) for item in facts)},
    )
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "analyzer-effectiveness-evidence-v1",
            {"analysis": analysis.analysis_id, "record_kind": record_kind, "digest": projection},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_relation"
        if record_kind == "worktree_comparison"
        else "internal_fact",
        source_owner_id="code",
        producer_id="code-analyzer-effectiveness-resolver",
        producer_version="v1",
        source_schema=CODE_ANALYZER_EFFECTIVENESS_SCHEMA,
        source_record_kind=record_kind,
        source_record_id=analysis.analysis_id,
        source_projection_digest=projection,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=facts,
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code-analyzer-effectiveness-resolver",
        resolver_version="v1",
        limitations=("observation_has_no_decision_authority",),
    )


def analyzer_effectiveness_questions(
    analysis: CodeAnalyzerEffectivenessAnalysis,
    *,
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Build freshness and calibration questions from one resolved projection."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("analyzer effectiveness rank offset must be non-negative")
    specs = (ANALYZER_FRESHNESS_QUESTION, ANALYZER_CALIBRATION_QUESTION)
    subject = _effectiveness_subject(analysis)
    if analysis.status != "ready":
        evaluations: list[AnalysisQuestionEvaluation] = []
        for index, spec in enumerate(specs, start=1):
            evaluation = AnalysisQuestionEvaluation(
                evaluation_id=analysis_identity(
                    "analyzer-effectiveness-question-v1",
                    {"analysis": analysis.analysis_id, "question": spec.question_id},
                ),
                question_id=spec.question_id,
                question_version=spec.version,
                question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
                rank=rank_offset + index,
                subject=subject,
                evidence=(),
                requirements=tuple(
                    AnalysisRequirementEvaluation(
                        item.requirement_id,
                        "not_evaluated" if item.role == "counterevidence" else "missing",
                        (),
                        analysis.reason or "analyzer_effectiveness_resolution_abstained",
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
                limitations=(*_LIMITATIONS, "analyzer_effectiveness_resolution_abstained"),
            )
            validate_analysis_question_evaluation(spec, evaluation)
            evaluations.append(evaluation)
        return specs, tuple(evaluations)
    publication = _evidence(
        analysis,
        subject=subject,
        record_kind="completed_code_publication",
        facts=(
            AnalysisFact("analysis_run_id", analysis.analysis_run_id),
            AnalysisFact("framework_run_id", analysis.framework_run_id),
            AnalysisFact("duration_ms", analysis.duration_ms, "milliseconds"),
            AnalysisFact("candidates", analysis.candidates),
            AnalysisFact("processed", analysis.processed),
            AnalysisFact("cache_hits", analysis.cache_hits),
            AnalysisFact("errors", analysis.errors),
        ),
    )
    comparison = _evidence(
        analysis,
        subject=subject,
        record_kind="worktree_comparison",
        facts=(
            AnalysisFact("inventory_observation", analysis.inventory_observation),
            AnalysisFact("scoped_recorded_files", analysis.scoped_recorded_files),
            AnalysisFact("git_visible_files", analysis.git_visible_files),
            AnalysisFact("exact_content_files", analysis.exact_content_files),
            AnalysisFact("content_changed_files", analysis.content_changed_files),
            AnalysisFact("missing_recorded_files", analysis.missing_recorded_files),
            AnalysisFact("unindexed_git_visible_files", analysis.unindexed_git_visible_files),
            AnalysisFact("nonregular_files", analysis.nonregular_files),
        ),
    )
    freshness = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "analyzer-effectiveness-question-v1",
            {
                "analysis": analysis.analysis_id,
                "question": ANALYZER_FRESHNESS_QUESTION.question_id,
                "evidence": (publication.evidence_id, comparison.evidence_id),
            },
        ),
        question_id=ANALYZER_FRESHNESS_QUESTION.question_id,
        question_version=ANALYZER_FRESHNESS_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(ANALYZER_FRESHNESS_QUESTION),
        rank=rank_offset + 1,
        subject=subject,
        evidence=(publication, comparison),
        requirements=(
            AnalysisRequirementEvaluation(
                "latest_completed_publication_observed",
                "satisfied",
                (publication.evidence_id,),
                "completed_error_free_code_publication_resolved",
            ),
            AnalysisRequirementEvaluation(
                "git_visible_worktree_compared_by_content",
                "satisfied",
                (comparison.evidence_id,),
                "stable_git_visible_inventory_compared_with_collision_guarded_content",
            ),
            AnalysisRequirementEvaluation(
                "ignored_and_external_project_scope_counterevidence_evaluated",
                "not_evaluated",
                (),
                "ignored_and_external_project_contents_are_not_enumerated",
            ),
            AnalysisRequirementEvaluation(
                "comparable_reanalysis_result",
                "missing",
                (),
                "no_post_observation_reanalysis_result_linked",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=ANALYZER_FRESHNESS_QUESTION.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(item.action_id for item in ANALYZER_FRESHNESS_QUESTION.next_actions),
        limitations=_LIMITATIONS,
    )
    calibration_evidence = _evidence(
        analysis,
        subject=subject,
        record_kind="analyzer_output_and_calibration",
        facts=(
            AnalysisFact("findings_observed", analysis.findings_observed),
            AnalysisFact("recommendations_observed", analysis.recommendations_observed),
            AnalysisFact("work_packages_observed", analysis.work_packages_observed),
            AnalysisFact("providers_observed", analysis.providers_observed),
            AnalysisFact("providers_ready", analysis.providers_ready),
            AnalysisFact("calibration_status", analysis.calibration_status),
            AnalysisFact("independent_outcome_labels", analysis.independent_outcome_labels),
        ),
    )
    calibration = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "analyzer-effectiveness-question-v1",
            {
                "analysis": analysis.analysis_id,
                "question": ANALYZER_CALIBRATION_QUESTION.question_id,
                "evidence": calibration_evidence.evidence_id,
            },
        ),
        question_id=ANALYZER_CALIBRATION_QUESTION.question_id,
        question_version=ANALYZER_CALIBRATION_QUESTION.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(ANALYZER_CALIBRATION_QUESTION),
        rank=rank_offset + 2,
        subject=subject,
        evidence=(calibration_evidence,),
        requirements=(
            AnalysisRequirementEvaluation(
                "analyzer_output_and_cost_observed",
                "satisfied",
                (calibration_evidence.evidence_id,),
                "bounded_outputs_provider_counts_and_run_cost_observed",
            ),
            AnalysisRequirementEvaluation(
                "independent_human_decision_or_escaped_defect_labels_linked",
                "missing",
                (),
                "no_independent_outcome_labels_linked",
            ),
            AnalysisRequirementEvaluation(
                "negative_controls_and_holdout_evaluated",
                "not_evaluated",
                (),
                "no_holdout_result_linked",
            ),
            AnalysisRequirementEvaluation(
                "effectiveness_calibration_experiment_result",
                "missing",
                (),
                "no_effectiveness_calibration_result_linked",
            ),
        ),
        observation_status="confirmed",
        inference_status="abstained",
        inferences=(),
        hypotheses=ANALYZER_CALIBRATION_QUESTION.hypotheses,
        question_readiness="ready",
        decision_readiness="experiment_required",
        decision=None,
        decision_reason="decision_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=tuple(
            item.action_id for item in ANALYZER_CALIBRATION_QUESTION.next_actions
        ),
        limitations=_LIMITATIONS,
    )
    validate_analysis_question_evaluation(ANALYZER_FRESHNESS_QUESTION, freshness)
    validate_analysis_question_evaluation(ANALYZER_CALIBRATION_QUESTION, calibration)
    return specs, (freshness, calibration)


def parse_code_analyzer_effectiveness_payload(
    payload: Mapping[str, object],
) -> CodeAnalyzerEffectivenessAnalysis:
    expected = {field.name for field in fields(CodeAnalyzerEffectivenessAnalysis)} | {"schema"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("analyzer effectiveness payload fields are invalid")
    if payload.get("schema") != CODE_ANALYZER_EFFECTIVENESS_SCHEMA:
        raise ValueError("analyzer effectiveness payload schema is invalid")
    values = {key: value for key, value in payload.items() if key != "schema"}
    for key in (
        "content_changed_examples",
        "missing_recorded_examples",
        "unindexed_git_visible_examples",
        "nonregular_examples",
    ):
        values[key] = _texts(f"analyzer {key}", values[key])
    raw_limitations = values["limitations"]
    if not isinstance(raw_limitations, Sequence) or isinstance(
        raw_limitations, (str, bytes, bytearray)
    ):
        raise ValueError("analyzer limitations must be a sequence")
    values["limitations"] = tuple(
        _required_text("analyzer limitation", item) for item in raw_limitations
    )
    return CodeAnalyzerEffectivenessAnalysis(**values)  # type: ignore[arg-type]


__all__ = [
    "ANALYZER_CALIBRATION_QUESTION",
    "ANALYZER_FRESHNESS_QUESTION",
    "CODE_ANALYZER_EFFECTIVENESS_POLICY",
    "CODE_ANALYZER_EFFECTIVENESS_SCHEMA",
    "CodeAnalyzerEffectivenessAnalysis",
    "CodeAnalyzerEffectivenessResolutionError",
    "abstained_code_analyzer_effectiveness",
    "analyze_code_analyzer_effectiveness",
    "analyzer_effectiveness_questions",
    "parse_code_analyzer_effectiveness_payload",
]
