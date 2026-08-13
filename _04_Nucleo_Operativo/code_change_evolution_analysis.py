"""Evidence-bound change-surface, history, and Code-schema questions.

This projection reads only already-published Code owner records.  A change is
the exact transition between two consecutive, comparable completed Code runs;
path relocation is kept separate from content and public-symbol deltas.  Git
history is optional provider evidence and fails closed when its run is absent,
incomplete, shallow, or truncated.  Schema evolution is deliberately limited
to the Code owner's own SQLite contract and migration ledger.

None of these observations is a defect probability, quality score, successful
refactor claim, change recommendation, or mutation authorization.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, Mapping, Sequence, cast

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
    validate_analysis_question_set,
)
from .code_schema import CODE_SCHEMA_VERSION, validate_code_schema
from .external_evidence_models import (
    ExternalProviderEvidence,
    ExternalProviderMetric,
)
from .external_evidence_store import read_external_provider_evidence
from .external_git_history import GIT_HISTORY_PROVIDER_ID, GIT_HISTORY_PROVIDER_SCHEMA
from .semantic_models import canonical_json
from .sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database


CODE_CHANGE_EVOLUTION_SCHEMA = "neocortex.code-change-evolution/v1"
CODE_CHANGE_SURFACE_POLICY = "consecutive-comparable-code-publications-v1"
CODE_HISTORY_CONTEXT_POLICY = "complete-git-history-for-change-surface-v1"
CODE_SCHEMA_EVOLUTION_POLICY = "code-owner-exact-schema-and-migration-ledger-v1"
CODE_CHANGE_EVOLUTION_LIMIT = 50
CODE_CHANGE_EVOLUTION_MAX_LIMIT = 200
CODE_CHANGE_PUBLIC_SYMBOL_LIMIT = 2_048
CODE_CHANGE_HISTORY_RELATION_LIMIT = 10_000
CODE_CHANGE_TRANSITION_HARD_LIMIT = 10_000

AnalysisStatus = Literal["ready", "partial", "abstained"]
ProjectionStatus = Literal["ready", "abstained"]
ContentChange = Literal["added", "modified", "removed", "unchanged"]
PathChange = Literal["none", "relocated"]

_ANALYSIS_LIMITATIONS = (
    "change_surface_is_observation_not_defect_probability_or_quality_score",
    "publication_delta_does_not_prove_behavioral_improvement_or_regression",
    "path_relocation_is_separate_from_content_and_public_api_delta",
    "public_symbols_are_static_python_ast_observations_not_runtime_api_guarantees",
    "git_history_and_cochange_do_not_prove_a_bug_or_an_omitted_companion_change",
    "schema_evolution_is_limited_to_the_code_owner_database",
    "live_checkout_content_is_not_read_and_freshness_is_publication_only",
    "human_decision_and_mutation_authority_are_not_owned_by_this_analysis",
)
_CHANGE_LIMITATIONS = (
    "consecutive_completed_runs_must_share_processing_signature",
    "relocation_of_an_unchanged_version_is_observed_since_its_version_path",
    "relocation_time_is_not_recorded_for_cache_reused_versions",
    "public_symbol_delta_does_not_prove_contract_compatibility",
    "call_resolution_changes_are_excluded_from_change_success_semantics",
)
_HISTORY_LIMITATIONS = (
    "history_window_is_provider_bounded",
    "history_metrics_are_observations_not_defect_probability",
    "cochange_is_not_static_dependency_or_proof_of_missing_work",
    "removed_files_are_not_present_in_the_current_git_provider_inventory",
)
_SCHEMA_LIMITATIONS = (
    "only_the_code_owner_sqlite_schema_is_observed",
    "historical_ddl_snapshots_are_not_persisted_for_direct_schema_diff",
    "migration_ledger_presence_does_not_prove_populated_upgrade_or_rollback_safety",
    "schema_shape_does_not_prove_application_invariants",
)


CHANGE_SURFACE_QUESTION = AnalysisQuestionSpec(
    question_id="evolution.change_surface_requires_review",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "comparable_publication_transition",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "capability_owner_contract_impact_observed",
            "decision",
            "supporting",
            ("internal_fact", "internal_relation", "contract", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "change_surface_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "internal_relation", "contract", "runtime_observation"),
        ),
        AnalysisEvidenceRequirementSpec(
            "change_surface_characterization_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "observed_surface_change_may_require_cross_owner_or_contract_review",
        "observed_surface_change_may_be_intentional_bounded_and_behavior_preserving",
    ),
    counterevidence_rules=(
        "declared_capability_and_owner_scope_matches_the_observed_surface",
        "public_contract_and_behavioral_characterization_remain_compatible",
        "path_only_relocation_preserves_content_and_public_symbols",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "map_changed_files_to_capabilities_owners_and_contracts",
            "characterization",
            "Map exact changed files and public symbols to declared capabilities, owners, and contracts.",
        ),
        AnalysisNextActionSpec(
            "seek_behavior_preservation_counterevidence",
            "counterevidence_search",
            "Seek explicit compatibility and behavior-preservation evidence without treating a rename as success.",
        ),
        AnalysisNextActionSpec(
            "run_smallest_affected_contract_experiment",
            "experiment",
            "Run the smallest affected contract experiment that distinguishes intended change from regression.",
        ),
    ),
)


CHANGE_HISTORY_QUESTION = AnalysisQuestionSpec(
    question_id="evolution.change_history_requires_companion_review",
    version="v1",
    subject_kinds=("run",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "comparable_change_surface",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "complete_git_history_metrics",
            "question",
            "supporting",
            ("external_metric",),
        ),
        AnalysisEvidenceRequirementSpec(
            "complete_git_cochange_relations",
            "question",
            "supporting",
            ("external_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "companion_change_semantics_characterized",
            "decision",
            "supporting",
            ("internal_fact", "internal_relation", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "history_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "internal_relation", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "companion_change_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "an_observed_cochange_companion_may_have_been_omitted",
        "the_current_change_may_intentionally_not_require_the_historical_companion",
    ),
    counterevidence_rules=(
        "cochange_was_incidental_or_owned_by_a_completed_migration",
        "current_contracts_show_the_companion_is_not_required",
        "rename_lineage_explains_the_historical_relation",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_unmodified_high_support_companions",
            "characterization",
            "Inspect unchanged cochange companions and the exact commits that produced the relation.",
        ),
        AnalysisNextActionSpec(
            "seek_intentional_independence_counterevidence",
            "counterevidence_search",
            "Seek contracts or migration evidence showing that the companion is intentionally independent.",
        ),
        AnalysisNextActionSpec(
            "run_companion_sensitive_contract_test",
            "experiment",
            "Run the smallest contract test sensitive to the changed file and its historical companion.",
        ),
    ),
)


CODE_SCHEMA_EVOLUTION_QUESTION = AnalysisQuestionSpec(
    question_id="evolution.code_owner_schema_requires_migration_review",
    version="v1",
    subject_kinds=("schema",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "exact_code_schema_and_migration_ledger",
            "question",
            "supporting",
            ("internal_fact", "contract"),
        ),
        AnalysisEvidenceRequirementSpec(
            "populated_upgrade_and_recovery_evidence",
            "decision",
            "supporting",
            ("contract", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "schema_compatibility_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("contract", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "schema_upgrade_matrix_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "the_code_owner_schema_may_need_additional_upgrade_or_recovery_assurance",
        "the_exact_schema_and_migration_protocol_may_already_be_sufficiently_assured",
    ),
    counterevidence_rules=(
        "representative_populated_upgrades_are_verified",
        "future_schema_and_interrupted_migration_fail_closed",
        "backup_recovery_and_idempotence_are_demonstrated",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inventory_supported_code_schema_sources",
            "characterization",
            "Inventory supported Code schema sources and representative populated fixtures.",
        ),
        AnalysisNextActionSpec(
            "seek_verified_migration_counterevidence",
            "counterevidence_search",
            "Seek existing populated migration, idempotence, backup, and recovery evidence.",
        ),
        AnalysisNextActionSpec(
            "run_bounded_code_schema_upgrade_matrix",
            "experiment",
            "Run a bounded populated Code-owner upgrade and recovery matrix on disposable copies.",
        ),
    ),
)


class CodeChangeEvolutionResolutionError(ValueError):
    """A source projection is incomplete, incompatible, or irreproducible."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _optional_text(label: str, value: object, *, maximum: int = 32_768) -> str | None:
    if value is None:
        return None
    return _required_text(label, value, maximum=maximum)


def _non_negative_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(label: str, value: object) -> int:
    result = _non_negative_int(label, value)
    if result < 1:
        raise ValueError(f"{label} must be positive")
    return result


def _text_tuple(label: str, values: object, *, maximum: int = 4_096) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item, maximum=maximum) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


def _revision_id(raw_xxh3_128: object, raw_xxh3_64_guard: object) -> str | None:
    if not isinstance(raw_xxh3_128, str) or not isinstance(raw_xxh3_64_guard, str):
        return None
    if not raw_xxh3_128 or not raw_xxh3_64_guard:
        return None
    return f"xxh3_128:{raw_xxh3_128}:xxh3_64_guard:{raw_xxh3_64_guard}"


def _required_revision(raw_xxh3_128: object, raw_xxh3_64_guard: object) -> str:
    revision = _revision_id(raw_xxh3_128, raw_xxh3_64_guard)
    if revision is None:
        raise CodeChangeEvolutionResolutionError("change_surface_content_digest_missing")
    return revision


def _publication_id(row: sqlite3.Row) -> str:
    return analysis_identity(
        "code-publication-v1",
        {
            "analysis_run_id": int(row["analysis_run_id"]),
            "framework_run_id": int(row["framework_run_id"]),
            "scan_id": int(row["scan_id"]),
            "processing_signature": str(row["processing_signature"]),
            "status": str(row["status"]),
            "completed_ns": int(row["completed_ns"]),
        },
    )


def _is_query_only(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA query_only").fetchone()
    return row is not None and int(row[0]) == 1


@dataclass(frozen=True, slots=True)
class CodeFileSurfaceChange:
    change_id: str
    file_id: int
    volume_id: str
    physical_file_id: str
    baseline_version_id: int | None
    current_version_id: int | None
    baseline_path: str | None
    current_path: str | None
    baseline_revision_id: str | None
    current_revision_id: str | None
    content_change: ContentChange
    path_change: PathChange
    public_symbols_added: tuple[str, ...]
    public_symbols_removed: tuple[str, ...]
    source_projection_digest: str
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("file change id", self.change_id, maximum=256)
        _positive_int("file change file id", self.file_id)
        _required_text("file change volume", self.volume_id, maximum=512)
        _required_text("file change physical identity", self.physical_file_id, maximum=512)
        for label, value in (
            ("baseline version", self.baseline_version_id),
            ("current version", self.current_version_id),
        ):
            if value is not None:
                _positive_int(label, value)
        _optional_text("baseline path", self.baseline_path)
        _optional_text("current path", self.current_path)
        _optional_text("baseline revision", self.baseline_revision_id, maximum=512)
        _optional_text("current revision", self.current_revision_id, maximum=512)
        if self.content_change not in {"added", "modified", "removed", "unchanged"}:
            raise ValueError("file content change is invalid")
        if self.path_change not in {"none", "relocated"}:
            raise ValueError("file path change is invalid")
        if self.content_change == "added" and (
            self.baseline_version_id is not None or self.current_version_id is None
        ):
            raise ValueError("added file transition is inconsistent")
        if self.content_change == "removed" and (
            self.baseline_version_id is None or self.current_version_id is not None
        ):
            raise ValueError("removed file transition is inconsistent")
        if self.content_change == "modified" and (
            self.baseline_version_id is None
            or self.current_version_id is None
            or self.baseline_version_id == self.current_version_id
        ):
            raise ValueError("modified file transition is inconsistent")
        if self.content_change == "unchanged" and (
            self.baseline_version_id is None
            or self.current_version_id is None
            or self.path_change != "relocated"
            or self.baseline_revision_id is None
            or self.current_revision_id != self.baseline_revision_id
        ):
            raise ValueError("unchanged file observation must be path-only relocation")
        expected_path_change: PathChange = (
            "relocated"
            if self.baseline_path is not None
            and self.current_path is not None
            and self.baseline_path != self.current_path
            else "none"
        )
        if self.path_change != expected_path_change:
            raise ValueError("file path change is not derived from observed paths")
        _text_tuple("public symbol added", self.public_symbols_added)
        _text_tuple("public symbol removed", self.public_symbols_removed)
        if self.content_change == "unchanged" and (
            self.public_symbols_added or self.public_symbols_removed
        ):
            raise ValueError("path-only relocation cannot claim public symbol delta")
        projection = {
            key: value
            for key, value in asdict(self).items()
            if key
            not in {
                "change_id",
                "source_projection_digest",
                "authority",
                "mutation_authority",
            }
        }
        expected_digest = analysis_identity("code-file-surface-source-v1", projection)
        if self.source_projection_digest != expected_digest:
            raise ValueError("file change source projection digest is invalid")
        expected_id = analysis_identity(
            "code-file-surface-change-v1",
            {
                "source_projection_digest": self.source_projection_digest,
                "policy": CODE_CHANGE_SURFACE_POLICY,
            },
        )
        if self.change_id != expected_id:
            raise ValueError("file change identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("file change observation must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class CodeChangeSurfaceProjection:
    projection_id: str
    status: ProjectionStatus
    reason: str | None
    policy_id: str
    snapshot_freshness: Literal["publication_only"]
    baseline_analysis_run_id: int | None
    current_analysis_run_id: int | None
    baseline_publication_id: str | None
    current_publication_id: str | None
    processing_signature: str | None
    files_added: int
    files_modified: int
    files_removed: int
    files_relocated: int
    public_symbols_added: int
    public_symbols_removed: int
    total_observations: int
    returned_observations: int
    truncated: bool
    observations: tuple[CodeFileSurfaceChange, ...]
    limitations: tuple[str, ...] = _CHANGE_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("change surface projection id", self.projection_id, maximum=256)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("change surface status is invalid")
        if self.policy_id != CODE_CHANGE_SURFACE_POLICY:
            raise ValueError("change surface policy is invalid")
        if self.snapshot_freshness != "publication_only":
            raise ValueError("change surface cannot claim live-checkout freshness")
        for label, value in (
            ("files added", self.files_added),
            ("files modified", self.files_modified),
            ("files removed", self.files_removed),
            ("files relocated", self.files_relocated),
            ("public symbols added", self.public_symbols_added),
            ("public symbols removed", self.public_symbols_removed),
            ("total change observations", self.total_observations),
            ("returned change observations", self.returned_observations),
        ):
            _non_negative_int(label, value)
        if self.limitations != _CHANGE_LIMITATIONS:
            raise ValueError("change surface limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("change surface must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("change surface abstention reason", self.reason, maximum=256)
            if (
                any(
                    value is not None
                    for value in (
                        self.baseline_analysis_run_id,
                        self.current_analysis_run_id,
                        self.baseline_publication_id,
                        self.current_publication_id,
                        self.processing_signature,
                    )
                )
                or any(
                    (
                        self.files_added,
                        self.files_modified,
                        self.files_removed,
                        self.files_relocated,
                        self.public_symbols_added,
                        self.public_symbols_removed,
                        self.total_observations,
                        self.returned_observations,
                    )
                )
                or self.truncated
                or self.observations
            ):
                raise ValueError("abstained change surface cannot assert partial observations")
        else:
            if self.reason is not None:
                raise ValueError("ready change surface cannot have an abstention reason")
            for label, run_id_value in (
                ("baseline analysis run", self.baseline_analysis_run_id),
                ("current analysis run", self.current_analysis_run_id),
            ):
                _positive_int(label, run_id_value)
            if cast(int, self.baseline_analysis_run_id) >= cast(int, self.current_analysis_run_id):
                raise ValueError("change surface publication order is invalid")
            _required_text("baseline publication", self.baseline_publication_id, maximum=256)
            _required_text("current publication", self.current_publication_id, maximum=256)
            _required_text("processing signature", self.processing_signature, maximum=2_048)
            if self.returned_observations != len(self.observations):
                raise ValueError("returned change observation count is inconsistent")
            if self.total_observations < self.returned_observations:
                raise ValueError("total change observation count is inconsistent")
            if self.truncated != (self.returned_observations < self.total_observations):
                raise ValueError("change surface truncation is inconsistent")
            if tuple(item.change_id for item in self.observations) != tuple(
                dict.fromkeys(item.change_id for item in self.observations)
            ):
                raise ValueError("change surface observation identities cannot repeat")
            if self.files_added != sum(
                item.content_change == "added" for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("files-added count disagrees with observations")
            if self.files_modified != sum(
                item.content_change == "modified" for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("files-modified count disagrees with observations")
            if self.files_removed != sum(
                item.content_change == "removed" for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("files-removed count disagrees with observations")
            if self.files_relocated != sum(
                item.path_change == "relocated" for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("files-relocated count disagrees with observations")
            if self.public_symbols_added != sum(
                len(item.public_symbols_added) for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("public-symbol-added count disagrees with observations")
            if self.public_symbols_removed != sum(
                len(item.public_symbols_removed) for item in self.observations
            ):
                if not self.truncated:
                    raise ValueError("public-symbol-removed count disagrees with observations")
        expected_id = analysis_identity(
            "code-change-surface-projection-v1",
            {
                key: value
                for key, value in asdict(self).items()
                if key not in {"projection_id", "authority", "mutation_authority"}
            },
        )
        if self.projection_id != expected_id:
            raise ValueError("change surface projection identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": "neocortex.code-change-surface/v1", **asdict(self)}


@dataclass(frozen=True, slots=True)
class CodeHistoryFileContext:
    subject_key: str
    version_id: int
    history_observed: bool
    observed_commit_count: int
    observed_touch_count: int
    observed_additions: int
    observed_deletions: int
    observed_churn_lines: int
    binary_or_unmeasured_touch_count: int
    observed_change_frequency_per_100_commits: float
    observed_age_seconds: int | None
    observed_recency_seconds: int | None

    def __post_init__(self) -> None:
        _required_text("history file subject", self.subject_key, maximum=4_096)
        _positive_int("history file version", self.version_id)
        if not isinstance(self.history_observed, bool):
            raise ValueError("history observed flag is invalid")
        for label, value in (
            ("observed commits", self.observed_commit_count),
            ("observed touches", self.observed_touch_count),
            ("observed additions", self.observed_additions),
            ("observed deletions", self.observed_deletions),
            ("observed churn", self.observed_churn_lines),
            ("unmeasured touches", self.binary_or_unmeasured_touch_count),
        ):
            _non_negative_int(label, value)
        if self.observed_churn_lines != self.observed_additions + self.observed_deletions:
            raise ValueError("history churn is not derived from additions and deletions")
        if (
            isinstance(self.observed_change_frequency_per_100_commits, bool)
            or not isinstance(self.observed_change_frequency_per_100_commits, (int, float))
            or self.observed_change_frequency_per_100_commits < 0
        ):
            raise ValueError("history frequency is invalid")
        for label, seconds in (
            ("observed age", self.observed_age_seconds),
            ("observed recency", self.observed_recency_seconds),
        ):
            if seconds is not None:
                _non_negative_int(label, seconds)
        if self.history_observed != (self.observed_commit_count > 0):
            raise ValueError("history observed flag disagrees with commit count")
        if self.history_observed != (
            self.observed_age_seconds is not None and self.observed_recency_seconds is not None
        ):
            raise ValueError("history temporal metrics are incomplete")


@dataclass(frozen=True, slots=True)
class CodeHistoryCompanion:
    relation_id: str
    source_key: str
    target_key: str
    observed_commits_together: int
    observed_frequency_per_100_commits: float
    source_changed: bool
    target_changed: bool

    def __post_init__(self) -> None:
        _required_text("history relation id", self.relation_id, maximum=512)
        _required_text("history relation source", self.source_key, maximum=4_096)
        _required_text("history relation target", self.target_key, maximum=4_096)
        if self.source_key >= self.target_key:
            raise ValueError("history companion endpoints must be canonically ordered")
        _positive_int("history commits together", self.observed_commits_together)
        if (
            isinstance(self.observed_frequency_per_100_commits, bool)
            or not isinstance(self.observed_frequency_per_100_commits, (int, float))
            or self.observed_frequency_per_100_commits < 0
        ):
            raise ValueError("history cochange frequency is invalid")
        if not isinstance(self.source_changed, bool) or not isinstance(self.target_changed, bool):
            raise ValueError("history companion change flags must be boolean")
        if not (self.source_changed or self.target_changed):
            raise ValueError("history companion must touch the current change surface")


@dataclass(frozen=True, slots=True)
class CodeHistoryEvolutionProjection:
    projection_id: str
    status: ProjectionStatus
    reason: str | None
    policy_id: str
    snapshot_freshness: Literal["publication_only"]
    provider_id: str
    provider_schema: str | None
    tool_run_id: int | None
    effective_tool_run_id: int | None
    portable_publication_id: str | None
    result_digest: str | None
    history_input_signature: str | None
    head_commit: str | None
    window_commits: int | None
    file_contexts: tuple[CodeHistoryFileContext, ...]
    companions: tuple[CodeHistoryCompanion, ...]
    limitations: tuple[str, ...] = _HISTORY_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("history projection id", self.projection_id, maximum=256)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("history projection status is invalid")
        if self.policy_id != CODE_HISTORY_CONTEXT_POLICY:
            raise ValueError("history projection policy is invalid")
        if self.snapshot_freshness != "publication_only":
            raise ValueError("history projection cannot claim live-checkout freshness")
        if self.provider_id != GIT_HISTORY_PROVIDER_ID:
            raise ValueError("history projection provider is invalid")
        if self.limitations != _HISTORY_LIMITATIONS:
            raise ValueError("history projection limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("history projection must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("history abstention reason", self.reason, maximum=256)
            if (
                any(
                    value is not None
                    for value in (
                        self.provider_schema,
                        self.tool_run_id,
                        self.effective_tool_run_id,
                        self.portable_publication_id,
                        self.result_digest,
                        self.history_input_signature,
                        self.head_commit,
                        self.window_commits,
                    )
                )
                or self.file_contexts
                or self.companions
            ):
                raise ValueError("abstained history projection cannot assert partial evidence")
        else:
            if self.reason is not None:
                raise ValueError("ready history projection cannot have an abstention reason")
            if self.provider_schema != GIT_HISTORY_PROVIDER_SCHEMA:
                raise ValueError("history provider schema is incompatible")
            _positive_int("history tool run", self.tool_run_id)
            _positive_int("history effective tool run", self.effective_tool_run_id)
            _required_text(
                "history portable publication", self.portable_publication_id, maximum=512
            )
            _required_text("history result digest", self.result_digest, maximum=512)
            _required_text("history input signature", self.history_input_signature, maximum=512)
            _required_text("history head commit", self.head_commit, maximum=128)
            _positive_int("history window commits", self.window_commits)
            if len({item.subject_key for item in self.file_contexts}) != len(self.file_contexts):
                raise ValueError("history file contexts cannot repeat")
            if len({item.relation_id for item in self.companions}) != len(self.companions):
                raise ValueError("history companion relations cannot repeat")
        expected_id = analysis_identity(
            "code-history-evolution-projection-v1",
            {
                key: value
                for key, value in asdict(self).items()
                if key not in {"projection_id", "authority", "mutation_authority"}
            },
        )
        if self.projection_id != expected_id:
            raise ValueError("history projection identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": "neocortex.code-history-evolution/v1", **asdict(self)}


@dataclass(frozen=True, slots=True)
class CodeSchemaMigrationObservation:
    version: int
    description: str
    applied_ns: int

    def __post_init__(self) -> None:
        _positive_int("Code migration version", self.version)
        _required_text("Code migration description", self.description, maximum=1_024)
        _positive_int("Code migration applied timestamp", self.applied_ns)


@dataclass(frozen=True, slots=True)
class CodeSchemaEvolutionProjection:
    projection_id: str
    status: ProjectionStatus
    reason: str | None
    policy_id: str
    owner_id: Literal["code"]
    schema_version: int | None
    migration_count: int
    migrations: tuple[CodeSchemaMigrationObservation, ...]
    table_count: int
    index_count: int
    trigger_count: int
    view_count: int
    ddl_digest: str | None
    migration_digest: str | None
    limitations: tuple[str, ...] = _SCHEMA_LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("schema projection id", self.projection_id, maximum=256)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("schema evolution status is invalid")
        if self.policy_id != CODE_SCHEMA_EVOLUTION_POLICY or self.owner_id != "code":
            raise ValueError("schema evolution scope is invalid")
        for label, value in (
            ("migration count", self.migration_count),
            ("table count", self.table_count),
            ("index count", self.index_count),
            ("trigger count", self.trigger_count),
            ("view count", self.view_count),
        ):
            _non_negative_int(label, value)
        if self.limitations != _SCHEMA_LIMITATIONS:
            raise ValueError("schema evolution limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("schema evolution must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("schema evolution abstention reason", self.reason, maximum=256)
            if (
                self.schema_version is not None
                or self.migration_count
                or self.migrations
                or any((self.table_count, self.index_count, self.trigger_count, self.view_count))
                or self.ddl_digest is not None
                or self.migration_digest is not None
            ):
                raise ValueError("abstained schema projection cannot assert partial evidence")
        else:
            if self.reason is not None:
                raise ValueError("ready schema projection cannot have an abstention reason")
            if self.schema_version != CODE_SCHEMA_VERSION:
                raise ValueError("schema projection is not the current Code contract")
            if (
                self.migration_count != len(self.migrations)
                or len(self.migrations) != CODE_SCHEMA_VERSION
            ):
                raise ValueError("Code migration ledger is incomplete")
            if tuple(item.version for item in self.migrations) != tuple(
                range(1, CODE_SCHEMA_VERSION + 1)
            ):
                raise ValueError("Code migration versions are not contiguous")
            _positive_int("Code schema table count", self.table_count)
            _required_text("Code schema DDL digest", self.ddl_digest, maximum=256)
            _required_text("Code migration digest", self.migration_digest, maximum=256)
        expected_id = analysis_identity(
            "code-schema-evolution-projection-v1",
            {
                key: value
                for key, value in asdict(self).items()
                if key not in {"projection_id", "authority", "mutation_authority"}
            },
        )
        if self.projection_id != expected_id:
            raise ValueError("schema evolution projection identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": "neocortex.code-schema-evolution/v1", **asdict(self)}


@dataclass(frozen=True, slots=True)
class CodeChangeEvolutionAnalysis:
    analysis_id: str
    database: str
    status: AnalysisStatus
    reason: str | None
    snapshot_freshness: Literal["publication_only"]
    change_surface: CodeChangeSurfaceProjection
    history: CodeHistoryEvolutionProjection
    code_schema: CodeSchemaEvolutionProjection
    limitations: tuple[str, ...] = _ANALYSIS_LIMITATIONS
    inference_status: Literal["abstained"] = "abstained"
    decision: None = None
    aggregate_score: None = None
    defect_probability: None = None
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("change evolution analysis id", self.analysis_id, maximum=256)
        _required_text("change evolution database", self.database)
        if self.status not in {"ready", "partial", "abstained"}:
            raise ValueError("change evolution status is invalid")
        if self.snapshot_freshness != "publication_only":
            raise ValueError("change evolution cannot claim live-checkout freshness")
        if self.limitations != _ANALYSIS_LIMITATIONS:
            raise ValueError("change evolution limitations are not canonical")
        if self.inference_status != "abstained" or self.decision is not None:
            raise ValueError("change evolution cannot infer a defect or own a decision")
        if self.aggregate_score is not None or self.defect_probability is not None:
            raise ValueError("change evolution cannot publish scores or defect probabilities")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("change evolution must remain advisory and non-mutating")
        ready = sum(
            item.status == "ready" for item in (self.change_surface, self.history, self.code_schema)
        )
        expected_status: AnalysisStatus = (
            "ready" if ready == 3 else "partial" if ready else "abstained"
        )
        expected_reason = (
            None
            if ready == 3
            else ("one_or_more_dimensions_abstained" if ready else "all_dimensions_abstained")
        )
        if self.status != expected_status or self.reason != expected_reason:
            raise ValueError("change evolution aggregate status is not derived")
        expected_id = analysis_identity(
            "code-change-evolution-analysis-v1",
            {
                "database": self.database,
                "status": self.status,
                "reason": self.reason,
                "snapshot_freshness": self.snapshot_freshness,
                "change_surface": self.change_surface.projection_id,
                "history": self.history.projection_id,
                "code_schema": self.code_schema.projection_id,
                "limitations": self.limitations,
            },
        )
        if self.analysis_id != expected_id:
            raise ValueError("change evolution analysis identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_CHANGE_EVOLUTION_SCHEMA, **asdict(self)}


def _projection_identity(prefix: str, values: Mapping[str, object]) -> str:
    def identity_value(value: object) -> object:
        if isinstance(
            value,
            (
                CodeFileSurfaceChange,
                CodeHistoryFileContext,
                CodeHistoryCompanion,
                CodeSchemaMigrationObservation,
            ),
        ):
            return asdict(value)
        if isinstance(value, Mapping):
            return {str(key): identity_value(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(identity_value(item) for item in value)
        return value

    return analysis_identity(
        prefix,
        {
            key: identity_value(value)
            for key, value in values.items()
            if key not in {"projection_id", "authority", "mutation_authority"}
        },
    )


def _abstained_change_surface(reason: str) -> CodeChangeSurfaceProjection:
    values: dict[str, object] = {
        "status": "abstained",
        "reason": reason,
        "policy_id": CODE_CHANGE_SURFACE_POLICY,
        "snapshot_freshness": "publication_only",
        "baseline_analysis_run_id": None,
        "current_analysis_run_id": None,
        "baseline_publication_id": None,
        "current_publication_id": None,
        "processing_signature": None,
        "files_added": 0,
        "files_modified": 0,
        "files_removed": 0,
        "files_relocated": 0,
        "public_symbols_added": 0,
        "public_symbols_removed": 0,
        "total_observations": 0,
        "returned_observations": 0,
        "truncated": False,
        "observations": (),
        "limitations": _CHANGE_LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeChangeSurfaceProjection(
        projection_id=_projection_identity("code-change-surface-projection-v1", values),
        **values,  # type: ignore[arg-type]
    )


def _abstained_history(reason: str) -> CodeHistoryEvolutionProjection:
    values: dict[str, object] = {
        "status": "abstained",
        "reason": reason,
        "policy_id": CODE_HISTORY_CONTEXT_POLICY,
        "snapshot_freshness": "publication_only",
        "provider_id": GIT_HISTORY_PROVIDER_ID,
        "provider_schema": None,
        "tool_run_id": None,
        "effective_tool_run_id": None,
        "portable_publication_id": None,
        "result_digest": None,
        "history_input_signature": None,
        "head_commit": None,
        "window_commits": None,
        "file_contexts": (),
        "companions": (),
        "limitations": _HISTORY_LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeHistoryEvolutionProjection(
        projection_id=_projection_identity("code-history-evolution-projection-v1", values),
        **values,  # type: ignore[arg-type]
    )


def _abstained_schema(reason: str) -> CodeSchemaEvolutionProjection:
    values: dict[str, object] = {
        "status": "abstained",
        "reason": reason,
        "policy_id": CODE_SCHEMA_EVOLUTION_POLICY,
        "owner_id": "code",
        "schema_version": None,
        "migration_count": 0,
        "migrations": (),
        "table_count": 0,
        "index_count": 0,
        "trigger_count": 0,
        "view_count": 0,
        "ddl_digest": None,
        "migration_digest": None,
        "limitations": _SCHEMA_LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeSchemaEvolutionProjection(
        projection_id=_projection_identity("code-schema-evolution-projection-v1", values),
        **values,  # type: ignore[arg-type]
    )


def _completed_run_pair(
    connection: sqlite3.Connection,
) -> tuple[sqlite3.Row, sqlite3.Row] | str:
    rows = connection.execute(
        """SELECT analysis_run_id,framework_run_id,scan_id,processing_signature,
        status,completed_ns,errors FROM analysis_runs WHERE status='completed'
        ORDER BY analysis_run_id DESC LIMIT 2"""
    ).fetchall()
    if len(rows) < 2:
        return "comparable_baseline_missing"
    current, baseline = rows
    if current["completed_ns"] is None or baseline["completed_ns"] is None:
        return "completed_publication_timestamp_missing"
    if str(current["processing_signature"]) != str(baseline["processing_signature"]):
        return "baseline_processing_signature_mismatch"
    if int(baseline["analysis_run_id"]) >= int(current["analysis_run_id"]):
        return "publication_order_invalid"
    if int(baseline["framework_run_id"]) >= int(current["framework_run_id"]):
        return "framework_publication_order_invalid"
    latest = connection.execute(
        "SELECT analysis_run_id,status FROM analysis_runs ORDER BY analysis_run_id DESC LIMIT 1"
    ).fetchone()
    if latest is None or int(latest["analysis_run_id"]) != int(current["analysis_run_id"]):
        return "newer_analysis_run_not_published"
    between = int(
        connection.execute(
            """SELECT COUNT(*) FROM analysis_runs
            WHERE analysis_run_id>? AND analysis_run_id<?""",
            (int(baseline["analysis_run_id"]), int(current["analysis_run_id"])),
        ).fetchone()[0]
    )
    if between:
        return "nonpublication_run_between_comparable_publications"
    if int(baseline["errors"]) or int(current["errors"]):
        return "comparable_publication_contains_analysis_errors"
    marker = connection.execute(
        "SELECT value FROM metadata WHERE key='code_graph_completion_v3'"
    ).fetchone()
    if marker is None:
        return "current_graph_publication_fence_missing"
    try:
        graph_fence = json.loads(str(marker["value"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "current_graph_publication_fence_invalid"
    if not isinstance(graph_fence, dict) or graph_fence.get("analysis_run_id") != int(
        current["analysis_run_id"]
    ):
        return "current_graph_publication_fence_stale"
    current_inventory_outside_fence = int(
        connection.execute(
            """SELECT COUNT(*) FROM files
            WHERE status='current' AND last_seen_run_id<>?""",
            (int(current["framework_run_id"]),),
        ).fetchone()[0]
    )
    if current_inventory_outside_fence:
        return "current_file_inventory_outside_publication_fence"
    return baseline, current


def _public_symbols(connection: sqlite3.Connection, version_id: int) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT kind,qualified_name,signature FROM symbols
        WHERE version_id=? AND confirmed=1 AND visibility='public'
          AND kind NOT IN ('module','entrypoint')
        ORDER BY kind,qualified_name,COALESCE(signature,''),start_byte,symbol_id""",
        (version_id,),
    ).fetchall()
    if len(rows) > CODE_CHANGE_PUBLIC_SYMBOL_LIMIT:
        raise CodeChangeEvolutionResolutionError("change_surface_public_symbol_bound_exceeded")
    return tuple(
        canonical_json(
            {
                "kind": str(row["kind"]),
                "qualified_name": str(row["qualified_name"]),
                "signature": None if row["signature"] is None else str(row["signature"]),
            }
        )
        for row in rows
    )


def _file_change(
    *,
    file_id: int,
    volume_id: str,
    physical_file_id: str,
    baseline_version_id: int | None,
    current_version_id: int | None,
    baseline_path: str | None,
    current_path: str | None,
    baseline_revision_id: str | None,
    current_revision_id: str | None,
    content_change: ContentChange,
    public_symbols_added: tuple[str, ...],
    public_symbols_removed: tuple[str, ...],
) -> CodeFileSurfaceChange:
    path_change: PathChange = (
        "relocated"
        if baseline_path is not None and current_path is not None and baseline_path != current_path
        else "none"
    )
    values: dict[str, object] = {
        "file_id": file_id,
        "volume_id": volume_id,
        "physical_file_id": physical_file_id,
        "baseline_version_id": baseline_version_id,
        "current_version_id": current_version_id,
        "baseline_path": baseline_path,
        "current_path": current_path,
        "baseline_revision_id": baseline_revision_id,
        "current_revision_id": current_revision_id,
        "content_change": content_change,
        "path_change": path_change,
        "public_symbols_added": public_symbols_added,
        "public_symbols_removed": public_symbols_removed,
    }
    projection_digest = analysis_identity("code-file-surface-source-v1", values)
    return CodeFileSurfaceChange(
        change_id=analysis_identity(
            "code-file-surface-change-v1",
            {
                "source_projection_digest": projection_digest,
                "policy": CODE_CHANGE_SURFACE_POLICY,
            },
        ),
        source_projection_digest=projection_digest,
        **values,  # type: ignore[arg-type]
    )


def _replacement_changes(
    connection: sqlite3.Connection,
    current_framework_run_id: int,
) -> list[CodeFileSurfaceChange]:
    rows = connection.execute(
        """SELECT f.file_id,f.volume_id,f.physical_file_id,f.current_path,
        current.version_id AS current_version_id,current.path_observed AS current_path_observed,
        current.raw_xxh3_128 AS current_raw_128,
        current.raw_xxh3_64_guard AS current_raw_64,
        previous.version_id AS baseline_version_id,
        previous.path_observed AS baseline_path,
        previous.raw_xxh3_128 AS baseline_raw_128,
        previous.raw_xxh3_64_guard AS baseline_raw_64
        FROM file_versions current
        JOIN files f ON f.current_version_id=current.version_id
        LEFT JOIN invalidation_history ih
          ON ih.replacement_version_id=current.version_id
         AND ih.reason='superseded_observation'
        LEFT JOIN file_versions previous ON previous.version_id=ih.version_id
        WHERE current.first_observed_run_id=? AND f.status='current'
        ORDER BY f.file_id,current.version_id""",
        (current_framework_run_id,),
    ).fetchall()
    result: list[CodeFileSurfaceChange] = []
    for row in rows:
        current_version_id = int(row["current_version_id"])
        baseline_version_id = (
            None if row["baseline_version_id"] is None else int(row["baseline_version_id"])
        )
        current_revision = _revision_id(row["current_raw_128"], row["current_raw_64"])
        baseline_revision = _revision_id(row["baseline_raw_128"], row["baseline_raw_64"])
        baseline_path = None if row["baseline_path"] is None else str(row["baseline_path"])
        current_path = str(row["current_path"])
        if baseline_version_id is None:
            if current_revision is None:
                raise CodeChangeEvolutionResolutionError("change_surface_content_digest_missing")
            current_symbols = _public_symbols(connection, current_version_id)
            result.append(
                _file_change(
                    file_id=int(row["file_id"]),
                    volume_id=str(row["volume_id"]),
                    physical_file_id=str(row["physical_file_id"]),
                    baseline_version_id=None,
                    current_version_id=current_version_id,
                    baseline_path=None,
                    current_path=current_path,
                    baseline_revision_id=None,
                    current_revision_id=current_revision,
                    content_change="added",
                    public_symbols_added=current_symbols,
                    public_symbols_removed=(),
                )
            )
            continue
        if current_revision is None or baseline_revision is None:
            raise CodeChangeEvolutionResolutionError("change_surface_content_digest_missing")
        same_content = baseline_revision == current_revision
        baseline_symbols = _public_symbols(connection, baseline_version_id)
        current_symbols = _public_symbols(connection, current_version_id)
        baseline_set = set(baseline_symbols)
        current_set = set(current_symbols)
        if same_content and baseline_set != current_set:
            raise CodeChangeEvolutionResolutionError(
                "identical_content_has_inconsistent_public_symbol_projection"
            )
        if same_content and baseline_path == current_path:
            continue
        result.append(
            _file_change(
                file_id=int(row["file_id"]),
                volume_id=str(row["volume_id"]),
                physical_file_id=str(row["physical_file_id"]),
                baseline_version_id=baseline_version_id,
                current_version_id=current_version_id,
                baseline_path=baseline_path,
                current_path=current_path,
                baseline_revision_id=baseline_revision,
                current_revision_id=current_revision,
                content_change="unchanged" if same_content else "modified",
                public_symbols_added=()
                if same_content
                else tuple(sorted(current_set - baseline_set)),
                public_symbols_removed=()
                if same_content
                else tuple(sorted(baseline_set - current_set)),
            )
        )
    return result


def _removed_changes(
    connection: sqlite3.Connection,
    current_framework_run_id: int,
) -> list[CodeFileSurfaceChange]:
    rows = connection.execute(
        """SELECT f.file_id,f.volume_id,f.physical_file_id,v.version_id,
        v.path_observed,v.raw_xxh3_128,v.raw_xxh3_64_guard
        FROM invalidation_history ih
        JOIN file_versions v ON v.version_id=ih.version_id
        JOIN files f ON f.file_id=v.file_id
        WHERE ih.replacement_version_id IS NULL
          AND CAST(json_extract(ih.evidence_json,'$.run_id') AS INTEGER)=?
          AND ih.reason IN ('not_seen_in_complete_inventory','path_reused_by_new_identity')
        ORDER BY f.file_id,v.version_id""",
        (current_framework_run_id,),
    ).fetchall()
    return [
        _file_change(
            file_id=int(row["file_id"]),
            volume_id=str(row["volume_id"]),
            physical_file_id=str(row["physical_file_id"]),
            baseline_version_id=int(row["version_id"]),
            current_version_id=None,
            baseline_path=str(row["path_observed"]),
            current_path=None,
            baseline_revision_id=_required_revision(row["raw_xxh3_128"], row["raw_xxh3_64_guard"]),
            current_revision_id=None,
            content_change="removed",
            public_symbols_added=(),
            public_symbols_removed=_public_symbols(connection, int(row["version_id"])),
        )
        for row in rows
    ]


def _cached_relocations(
    connection: sqlite3.Connection,
    *,
    baseline_framework_run_id: int,
    current_framework_run_id: int,
    excluded_file_ids: set[int],
) -> list[CodeFileSurfaceChange]:
    rows = connection.execute(
        """SELECT f.file_id,f.volume_id,f.physical_file_id,f.current_path,
        v.version_id,v.path_observed,v.raw_xxh3_128,v.raw_xxh3_64_guard
        FROM files f JOIN file_versions v ON v.version_id=f.current_version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL
          AND f.last_seen_run_id=? AND v.first_observed_run_id<=?
          AND f.current_path<>v.path_observed
        ORDER BY f.file_id,v.version_id""",
        (current_framework_run_id, baseline_framework_run_id),
    ).fetchall()
    result: list[CodeFileSurfaceChange] = []
    for row in rows:
        file_id = int(row["file_id"])
        if file_id in excluded_file_ids:
            continue
        version_id = int(row["version_id"])
        revision = _required_revision(row["raw_xxh3_128"], row["raw_xxh3_64_guard"])
        result.append(
            _file_change(
                file_id=file_id,
                volume_id=str(row["volume_id"]),
                physical_file_id=str(row["physical_file_id"]),
                baseline_version_id=version_id,
                current_version_id=version_id,
                baseline_path=str(row["path_observed"]),
                current_path=str(row["current_path"]),
                baseline_revision_id=revision,
                current_revision_id=revision,
                content_change="unchanged",
                public_symbols_added=(),
                public_symbols_removed=(),
            )
        )
    return result


def _read_change_surface(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> CodeChangeSurfaceProjection:
    pair = _completed_run_pair(connection)
    if isinstance(pair, str):
        return _abstained_change_surface(pair)
    baseline, current = pair
    try:
        observations = _replacement_changes(connection, int(current["framework_run_id"]))
        observations.extend(_removed_changes(connection, int(current["framework_run_id"])))
        observations.extend(
            _cached_relocations(
                connection,
                baseline_framework_run_id=int(baseline["framework_run_id"]),
                current_framework_run_id=int(current["framework_run_id"]),
                excluded_file_ids={item.file_id for item in observations},
            )
        )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, CodeChangeEvolutionResolutionError)
            else (f"change_surface_source_unresolvable:{type(exc).__name__}")
        )
        return _abstained_change_surface(reason)
    if len(observations) > CODE_CHANGE_TRANSITION_HARD_LIMIT:
        return _abstained_change_surface("change_surface_transition_bound_exceeded")
    order = {"modified": 0, "added": 1, "removed": 2, "unchanged": 3}
    observations.sort(
        key=lambda item: (
            order[item.content_change],
            item.current_path or item.baseline_path or "",
            item.file_id,
            item.change_id,
        )
    )
    selected = tuple(observations[:limit])
    values: dict[str, object] = {
        "status": "ready",
        "reason": None,
        "policy_id": CODE_CHANGE_SURFACE_POLICY,
        "snapshot_freshness": "publication_only",
        "baseline_analysis_run_id": int(baseline["analysis_run_id"]),
        "current_analysis_run_id": int(current["analysis_run_id"]),
        "baseline_publication_id": _publication_id(baseline),
        "current_publication_id": _publication_id(current),
        "processing_signature": str(current["processing_signature"]),
        "files_added": sum(item.content_change == "added" for item in observations),
        "files_modified": sum(item.content_change == "modified" for item in observations),
        "files_removed": sum(item.content_change == "removed" for item in observations),
        "files_relocated": sum(item.path_change == "relocated" for item in observations),
        "public_symbols_added": sum(len(item.public_symbols_added) for item in observations),
        "public_symbols_removed": sum(len(item.public_symbols_removed) for item in observations),
        "total_observations": len(observations),
        "returned_observations": len(selected),
        "truncated": len(selected) < len(observations),
        "observations": selected,
        "limitations": _CHANGE_LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeChangeSurfaceProjection(
        projection_id=_projection_identity("code-change-surface-projection-v1", values),
        **values,  # type: ignore[arg-type]
    )


_HISTORY_CORE_METRICS = (
    "history_observed",
    "observed_commit_count",
    "observed_touch_count",
    "observed_additions",
    "observed_deletions",
    "observed_churn_lines",
    "binary_or_unmeasured_touch_count",
    "observed_change_frequency_per_100_commits",
)
_HISTORY_INCOMPLETE_LIMITATIONS = frozenset(
    {
        "commit_window_truncated",
        "shallow_repository_history_incomplete",
        "cochange_relation_candidates_truncated",
        "large_commits_excluded_from_cochange_relations",
    }
)


def _history_contract_row(
    connection: sqlite3.Connection,
    analysis_run_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT t.tool_run_id,t.status,c.provider_id,c.provider_schema,
        c.input_signature,c.result_digest,c.portable_publication_id,
        c.coverage_complete,c.limitations_json
        FROM external_tool_runs t JOIN external_run_contracts c USING(tool_run_id)
        WHERE t.analysis_run_id=? AND c.provider_id=?
        ORDER BY t.tool_run_id DESC LIMIT 1""",
        (analysis_run_id, GIT_HISTORY_PROVIDER_ID),
    ).fetchone()


def _integer_metric(metric: ExternalProviderMetric) -> int:
    numeric = float(metric.value)
    if not numeric.is_integer() or numeric < 0:
        raise CodeChangeEvolutionResolutionError("history_metric_integer_contract_invalid")
    return int(numeric)


def _history_shared_metadata(
    metrics: Sequence[ExternalProviderMetric],
    *,
    input_signature: str,
) -> tuple[str, int]:
    if not metrics:
        raise CodeChangeEvolutionResolutionError("history_provider_metrics_missing")
    heads: set[str] = set()
    windows: set[int] = set()
    signatures: set[str] = set()
    for metric in metrics:
        metadata = metric.metadata
        head = metadata.get("head_commit")
        window = metadata.get("window_commits")
        signature = metadata.get("history_input_signature")
        if not isinstance(head, str) or not head:
            raise CodeChangeEvolutionResolutionError("history_head_commit_missing")
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise CodeChangeEvolutionResolutionError("history_window_contract_invalid")
        if not isinstance(signature, str) or not signature:
            raise CodeChangeEvolutionResolutionError("history_input_signature_missing")
        if metadata.get("history_truncated") is not False:
            raise CodeChangeEvolutionResolutionError("history_provider_truncated")
        if metadata.get("repository_shallow") is not False:
            raise CodeChangeEvolutionResolutionError("history_repository_shallow")
        heads.add(head)
        windows.add(window)
        signatures.add(signature)
    if len(heads) != 1 or len(windows) != 1 or signatures != {input_signature}:
        raise CodeChangeEvolutionResolutionError("history_provider_metadata_inconsistent")
    return next(iter(heads)), next(iter(windows))


def _history_file_contexts(
    evidence: ExternalProviderEvidence,
    change_surface: CodeChangeSurfaceProjection,
) -> tuple[tuple[CodeHistoryFileContext, ...], set[str]]:
    current_versions = {
        item.current_version_id
        for item in change_surface.observations
        if item.current_version_id is not None
    }
    if any(item.current_version_id is None for item in change_surface.observations):
        raise CodeChangeEvolutionResolutionError(
            "history_provider_does_not_cover_removed_change_surface"
        )
    grouped: dict[int, dict[str, ExternalProviderMetric]] = {}
    for metric in evidence.metrics:
        if metric.subject_kind != "file" or metric.version_id not in current_versions:
            continue
        by_name = grouped.setdefault(metric.version_id, {})
        if metric.metric_name in by_name:
            raise CodeChangeEvolutionResolutionError("history_metric_duplicate")
        by_name[metric.metric_name] = metric
    if set(grouped) != current_versions:
        raise CodeChangeEvolutionResolutionError(
            "history_provider_change_surface_coverage_incomplete"
        )
    contexts: list[CodeHistoryFileContext] = []
    keys: set[str] = set()
    for version_id, metrics in sorted(grouped.items()):
        if any(name not in metrics for name in _HISTORY_CORE_METRICS):
            raise CodeChangeEvolutionResolutionError("history_core_metrics_incomplete")
        subjects = {item.subject_key for item in metrics.values()}
        if len(subjects) != 1:
            raise CodeChangeEvolutionResolutionError("history_metric_subject_inconsistent")
        subject = next(iter(subjects))
        keys.add(subject)
        observed = _integer_metric(metrics["history_observed"])
        if observed not in {0, 1}:
            raise CodeChangeEvolutionResolutionError("history_observed_flag_invalid")
        age = metrics.get("observed_age_seconds")
        recency = metrics.get("observed_recency_seconds")
        contexts.append(
            CodeHistoryFileContext(
                subject_key=subject,
                version_id=version_id,
                history_observed=bool(observed),
                observed_commit_count=_integer_metric(metrics["observed_commit_count"]),
                observed_touch_count=_integer_metric(metrics["observed_touch_count"]),
                observed_additions=_integer_metric(metrics["observed_additions"]),
                observed_deletions=_integer_metric(metrics["observed_deletions"]),
                observed_churn_lines=_integer_metric(metrics["observed_churn_lines"]),
                binary_or_unmeasured_touch_count=_integer_metric(
                    metrics["binary_or_unmeasured_touch_count"]
                ),
                observed_change_frequency_per_100_commits=float(
                    metrics["observed_change_frequency_per_100_commits"].value
                ),
                observed_age_seconds=None if age is None else _integer_metric(age),
                observed_recency_seconds=None if recency is None else _integer_metric(recency),
            )
        )
    return tuple(sorted(contexts, key=lambda item: item.subject_key)), keys


def _history_companions(
    evidence: ExternalProviderEvidence,
    changed_keys: set[str],
) -> tuple[CodeHistoryCompanion, ...]:
    if len(evidence.relations) > CODE_CHANGE_HISTORY_RELATION_LIMIT:
        raise CodeChangeEvolutionResolutionError("history_relation_bound_exceeded")
    companions: list[CodeHistoryCompanion] = []
    for relation in evidence.relations:
        if relation.relation_kind != "file_cochange":
            continue
        if relation.directed or relation.source_kind != "file" or relation.target_kind != "file":
            raise CodeChangeEvolutionResolutionError("history_relation_contract_invalid")
        if relation.source_key not in changed_keys and relation.target_key not in changed_keys:
            continue
        together = relation.metadata.get("observed_commits_together")
        frequency = relation.metadata.get("observed_frequency_per_100_commits")
        if isinstance(together, bool) or not isinstance(together, int) or together < 1:
            raise CodeChangeEvolutionResolutionError("history_relation_support_invalid")
        if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or frequency < 0:
            raise CodeChangeEvolutionResolutionError("history_relation_frequency_invalid")
        source, target = sorted((relation.source_key, relation.target_key))
        companions.append(
            CodeHistoryCompanion(
                relation_id=relation.portable_relation_id,
                source_key=source,
                target_key=target,
                observed_commits_together=together,
                observed_frequency_per_100_commits=float(frequency),
                source_changed=source in changed_keys,
                target_changed=target in changed_keys,
            )
        )
    return tuple(
        sorted(
            companions,
            key=lambda item: (
                -item.observed_commits_together,
                item.source_key,
                item.target_key,
                item.relation_id,
            ),
        )
    )


def _read_history_projection(
    connection: sqlite3.Connection,
    change_surface: CodeChangeSurfaceProjection,
) -> CodeHistoryEvolutionProjection:
    if change_surface.status != "ready" or change_surface.current_analysis_run_id is None:
        return _abstained_history("comparable_change_surface_missing")
    if change_surface.truncated:
        return _abstained_history("change_surface_selection_truncated")
    row = _history_contract_row(connection, change_surface.current_analysis_run_id)
    if row is None:
        return _abstained_history("history_provider_missing")
    try:
        providers = read_external_provider_evidence(
            connection,
            change_surface.current_analysis_run_id,
            provider_ids=(GIT_HISTORY_PROVIDER_ID,),
        )
        evidence = providers.get(GIT_HISTORY_PROVIDER_ID)
        if evidence is None:
            return _abstained_history("history_provider_missing")
        if evidence.status != "ready":
            return _abstained_history(evidence.reason or "history_provider_not_ready")
        if int(row["tool_run_id"]) != evidence.tool_run_id:
            raise CodeChangeEvolutionResolutionError("history_provider_run_mismatch")
        if str(row["status"]) not in {"completed", "reused"}:
            raise CodeChangeEvolutionResolutionError("history_provider_not_completed")
        if str(row["provider_schema"]) != GIT_HISTORY_PROVIDER_SCHEMA:
            raise CodeChangeEvolutionResolutionError("history_provider_schema_incompatible")
        if int(row["coverage_complete"]) != 1:
            raise CodeChangeEvolutionResolutionError("history_provider_coverage_incomplete")
        raw_limitations = json.loads(str(row["limitations_json"]))
        limitations = _text_tuple("history provider limitation", raw_limitations)
        incomplete = sorted(set(limitations) & _HISTORY_INCOMPLETE_LIMITATIONS)
        if incomplete:
            raise CodeChangeEvolutionResolutionError(
                "history_provider_incomplete:" + ",".join(incomplete)
            )
        input_signature = str(row["input_signature"])
        head_commit, window_commits = _history_shared_metadata(
            evidence.metrics,
            input_signature=input_signature,
        )
        contexts, changed_keys = _history_file_contexts(evidence, change_surface)
        companions = _history_companions(evidence, changed_keys)
        values: dict[str, object] = {
            "status": "ready",
            "reason": None,
            "policy_id": CODE_HISTORY_CONTEXT_POLICY,
            "snapshot_freshness": "publication_only",
            "provider_id": GIT_HISTORY_PROVIDER_ID,
            "provider_schema": str(row["provider_schema"]),
            "tool_run_id": evidence.tool_run_id,
            "effective_tool_run_id": evidence.effective_tool_run_id,
            "portable_publication_id": str(row["portable_publication_id"]),
            "result_digest": str(row["result_digest"]),
            "history_input_signature": input_signature,
            "head_commit": head_commit,
            "window_commits": window_commits,
            "file_contexts": contexts,
            "companions": companions,
            "limitations": _HISTORY_LIMITATIONS,
            "authority": "advisory",
            "mutation_authority": False,
        }
        return CodeHistoryEvolutionProjection(
            projection_id=_projection_identity("code-history-evolution-projection-v1", values),
            **values,  # type: ignore[arg-type]
        )
    except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, CodeChangeEvolutionResolutionError)
            else (f"history_provider_unresolvable:{type(exc).__name__}")
        )
        return _abstained_history(reason)


def _read_schema_projection(connection: sqlite3.Connection) -> CodeSchemaEvolutionProjection:
    try:
        validate_code_schema(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != CODE_SCHEMA_VERSION:
            raise CodeChangeEvolutionResolutionError("code_schema_version_incompatible")
        migration_rows = connection.execute(
            "SELECT version,description,applied_ns FROM schema_migrations ORDER BY version"
        ).fetchall()
        migrations = tuple(
            CodeSchemaMigrationObservation(
                int(row["version"]), str(row["description"]), int(row["applied_ns"])
            )
            for row in migration_rows
        )
        if tuple(item.version for item in migrations) != tuple(range(1, CODE_SCHEMA_VERSION + 1)):
            raise CodeChangeEvolutionResolutionError("code_schema_migration_ledger_incomplete")
        ddl_rows = tuple(
            (
                str(row["type"]),
                str(row["name"]),
                str(row["tbl_name"]),
                str(row["sql"]),
            )
            for row in connection.execute(
                """SELECT type,name,tbl_name,sql FROM sqlite_schema
                WHERE type IN ('table','index','trigger','view')
                  AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
                ORDER BY type,name,tbl_name,sql"""
            ).fetchall()
        )
        counts = {
            kind: sum(row[0] == kind for row in ddl_rows)
            for kind in ("table", "index", "trigger", "view")
        }
        values: dict[str, object] = {
            "status": "ready",
            "reason": None,
            "policy_id": CODE_SCHEMA_EVOLUTION_POLICY,
            "owner_id": "code",
            "schema_version": version,
            "migration_count": len(migrations),
            "migrations": migrations,
            "table_count": counts["table"],
            "index_count": counts["index"],
            "trigger_count": counts["trigger"],
            "view_count": counts["view"],
            "ddl_digest": analysis_identity("code-schema-ddl-v1", ddl_rows),
            "migration_digest": analysis_identity(
                "code-schema-migrations-v1", tuple(asdict(item) for item in migrations)
            ),
            "limitations": _SCHEMA_LIMITATIONS,
            "authority": "advisory",
            "mutation_authority": False,
        }
        return CodeSchemaEvolutionProjection(
            projection_id=_projection_identity("code-schema-evolution-projection-v1", values),
            **values,  # type: ignore[arg-type]
        )
    except (sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, CodeChangeEvolutionResolutionError)
            else (f"code_schema_unresolvable:{type(exc).__name__}")
        )
        return _abstained_schema(reason)


def _analysis(
    database: str,
    change_surface: CodeChangeSurfaceProjection,
    history: CodeHistoryEvolutionProjection,
    code_schema: CodeSchemaEvolutionProjection,
) -> CodeChangeEvolutionAnalysis:
    ready = sum(item.status == "ready" for item in (change_surface, history, code_schema))
    status: AnalysisStatus = "ready" if ready == 3 else "partial" if ready else "abstained"
    reason = (
        None
        if ready == 3
        else ("one_or_more_dimensions_abstained" if ready else "all_dimensions_abstained")
    )
    analysis_id = analysis_identity(
        "code-change-evolution-analysis-v1",
        {
            "database": database,
            "status": status,
            "reason": reason,
            "snapshot_freshness": "publication_only",
            "change_surface": change_surface.projection_id,
            "history": history.projection_id,
            "code_schema": code_schema.projection_id,
            "limitations": _ANALYSIS_LIMITATIONS,
        },
    )
    return CodeChangeEvolutionAnalysis(
        analysis_id=analysis_id,
        database=database,
        status=status,
        reason=reason,
        snapshot_freshness="publication_only",
        change_surface=change_surface,
        history=history,
        code_schema=code_schema,
    )


def read_code_change_evolution_analysis(
    connection: sqlite3.Connection,
    *,
    database: str,
    limit: int = CODE_CHANGE_EVOLUTION_LIMIT,
) -> CodeChangeEvolutionAnalysis:
    """Read one bounded projection from an already query-only Code connection."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("change evolution analysis requires a SQLite connection")
    _required_text("change evolution database", database)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= (CODE_CHANGE_EVOLUTION_MAX_LIMIT)
    ):
        raise ValueError(
            f"change evolution limit must be between 1 and {CODE_CHANGE_EVOLUTION_MAX_LIMIT}"
        )
    if not _is_query_only(connection):
        raise CodeChangeEvolutionResolutionError("change_evolution_requires_query_only_connection")
    change_surface = _read_change_surface(connection, limit=limit)
    history = _read_history_projection(connection, change_surface)
    code_schema = _read_schema_projection(connection)
    return _analysis(database, change_surface, history, code_schema)


def analyze_code_change_evolution(
    database: Path,
    *,
    limit: int = CODE_CHANGE_EVOLUTION_LIMIT,
) -> CodeChangeEvolutionAnalysis:
    """Open one stable Code owner with immutable/query-only safeguards."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= CODE_CHANGE_EVOLUTION_MAX_LIMIT
    ):
        raise ValueError(
            f"change evolution limit must be between 1 and {CODE_CHANGE_EVOLUTION_MAX_LIMIT}"
        )
    selected = Path(database)
    try:
        with immutable_sqlite_database(selected) as connection:
            return read_code_change_evolution_analysis(
                connection,
                database=str(selected),
                limit=limit,
            )
    except (
        FileNotFoundError,
        ImmutableSQLiteUnavailable,
        OSError,
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        reason = f"code_change_evolution_unresolvable:{type(exc).__name__}"
        return _analysis(
            str(selected),
            _abstained_change_surface(reason),
            _abstained_history(reason),
            _abstained_schema(reason),
        )


def _evaluation_state(
    spec: AnalysisQuestionSpec,
    *,
    question_ready: bool,
) -> tuple[
    Literal["confirmed", "abstained"],
    Literal["ready", "abstained"],
    Literal["experiment_required", "abstained"],
    str,
    tuple[str, ...],
]:
    if question_ready:
        return (
            "confirmed",
            "ready",
            "experiment_required",
            "decision_evidence_incomplete",
            tuple(item.action_id for item in spec.next_actions),
        )
    return (
        "abstained",
        "abstained",
        "abstained",
        "question_evidence_incomplete",
        (),
    )


def _run_subject(
    analysis: CodeChangeEvolutionAnalysis,
    *,
    question_id: str,
) -> AnalysisSubjectRef:
    surface = analysis.change_surface
    ready = surface.status == "ready"
    snapshot = cast(str, surface.current_publication_id) if ready else analysis.analysis_id
    revision = cast(str, surface.current_publication_id) if ready else None
    return AnalysisSubjectRef(
        subject_kind="run",
        subject_key=analysis_identity(
            "code-change-run-subject-v1",
            {
                "question_id": question_id,
                "baseline": surface.baseline_publication_id,
                "current": surface.current_publication_id,
                "analysis": analysis.analysis_id,
            },
        ),
        display_name=(
            f"Code publications {surface.baseline_analysis_run_id}->{surface.current_analysis_run_id}"
            if ready
            else "Code publication transition unavailable"
        ),
        source_owner_id="code",
        snapshot_id=snapshot,
        snapshot_freshness="publication_only" if ready else "unknown",
        revision_id=revision,
    )


def _schema_subject(analysis: CodeChangeEvolutionAnalysis) -> AnalysisSubjectRef:
    projection = analysis.code_schema
    ready = projection.status == "ready"
    return AnalysisSubjectRef(
        subject_kind="schema",
        subject_key=analysis_identity(
            "code-owner-schema-subject-v1",
            {
                "owner": "code",
                "projection": projection.projection_id,
            },
        ),
        display_name="Code owner SQLite schema",
        source_owner_id="code",
        snapshot_id=projection.projection_id,
        snapshot_freshness="publication_only" if ready else "unknown",
        revision_id=projection.ddl_digest if ready else None,
    )


def _change_evidence(
    surface: CodeChangeSurfaceProjection,
    subject: AnalysisSubjectRef,
) -> AnalysisEvidenceRef:
    if surface.status != "ready":
        raise ValueError("change evidence requires a comparable surface")
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-change-surface-evidence-v1",
            {
                "subject": subject.subject_key,
                "projection": surface.projection_id,
            },
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_relation",
        source_owner_id="code",
        producer_id="code-state-publication-transition",
        producer_version="v1",
        source_schema=f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
        source_record_kind="consecutive_completed_analysis_runs",
        source_record_id=(f"{surface.baseline_analysis_run_id}->{surface.current_analysis_run_id}"),
        source_projection_digest=surface.projection_id,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=(
            AnalysisFact("baseline_analysis_run_id", surface.baseline_analysis_run_id),
            AnalysisFact("current_analysis_run_id", surface.current_analysis_run_id),
            AnalysisFact("files_added", surface.files_added, "count"),
            AnalysisFact("files_modified", surface.files_modified, "count"),
            AnalysisFact("files_removed", surface.files_removed, "count"),
            AnalysisFact("files_relocated", surface.files_relocated, "count"),
            AnalysisFact("public_symbols_added", surface.public_symbols_added, "count"),
            AnalysisFact("public_symbols_removed", surface.public_symbols_removed, "count"),
            AnalysisFact("selection_truncated", surface.truncated),
            AnalysisFact("policy", surface.policy_id),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code.sqlite-change-surface-resolver",
        resolver_version="v1",
        limitations=("publication_transition_only",),
    )


def _history_evidence(
    history: CodeHistoryEvolutionProjection,
    subject: AnalysisSubjectRef,
) -> tuple[AnalysisEvidenceRef, AnalysisEvidenceRef]:
    if history.status != "ready" or history.tool_run_id is None:
        raise ValueError("history evidence requires a complete provider projection")
    metric_projection = {
        "provider_result": history.result_digest,
        "input": history.history_input_signature,
        "head": history.head_commit,
        "window_commits": history.window_commits,
        "file_contexts": tuple(asdict(item) for item in history.file_contexts),
    }
    relation_projection = {
        "provider_result": history.result_digest,
        "input": history.history_input_signature,
        "companions": tuple(asdict(item) for item in history.companions),
    }
    metric_digest = analysis_identity("code-history-metric-source-v1", metric_projection)
    relation_digest = analysis_identity("code-history-relation-source-v1", relation_projection)
    metric = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-history-metric-evidence-v1",
            {"subject": subject.subject_key, "source": metric_digest},
        ),
        role="supporting",
        evidence_kind="external_metric",
        source_record_kind="git_history_file_metrics",
        source_projection_digest=metric_digest,
        facts=(
            AnalysisFact("file_context_count", len(history.file_contexts), "count"),
            AnalysisFact("head_commit", history.head_commit),
            AnalysisFact("window_commits", history.window_commits, "count"),
            AnalysisFact("provider_result_digest", history.result_digest),
        ),
        subject_key=subject.subject_key,
        source_owner_id="code",
        producer_id=history.provider_id,
        producer_version=cast(str, history.provider_schema),
        source_schema=cast(str, history.provider_schema),
        source_record_id=str(history.tool_run_id),
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code.external-git-history-resolver",
        resolver_version="v1",
        limitations=("history_observation_not_defect_probability",),
        provider_run_id=history.tool_run_id,
    )
    relation = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-history-relation-evidence-v1",
            {"subject": subject.subject_key, "source": relation_digest},
        ),
        role="supporting",
        evidence_kind="external_relation",
        source_record_kind="git_history_cochange_relations",
        source_projection_digest=relation_digest,
        facts=(
            AnalysisFact("companion_relation_count", len(history.companions), "count"),
            AnalysisFact(
                "unchanged_companion_relation_count",
                sum(item.source_changed != item.target_changed for item in history.companions),
                "count",
            ),
            AnalysisFact("provider_result_digest", history.result_digest),
        ),
        subject_key=subject.subject_key,
        source_owner_id="code",
        producer_id=history.provider_id,
        producer_version=cast(str, history.provider_schema),
        source_schema=cast(str, history.provider_schema),
        source_record_id=str(history.tool_run_id),
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code.external-git-history-resolver",
        resolver_version="v1",
        limitations=("history_observation_not_defect_probability",),
        provider_run_id=history.tool_run_id,
    )
    return metric, relation


def _schema_evidence(
    projection: CodeSchemaEvolutionProjection,
    subject: AnalysisSubjectRef,
) -> AnalysisEvidenceRef:
    if projection.status != "ready":
        raise ValueError("schema evidence requires an exact Code schema projection")
    return AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "code-schema-evolution-evidence-v1",
            {"subject": subject.subject_key, "projection": projection.projection_id},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="contract",
        source_owner_id="code",
        producer_id="code-schema-contract",
        producer_version=f"v{projection.schema_version}",
        source_schema=f"neocortex.code-state/sqlite-v{CODE_SCHEMA_VERSION}",
        source_record_kind="exact_sqlite_schema_and_migration_ledger",
        source_record_id=f"code-schema-v{projection.schema_version}",
        source_projection_digest=projection.projection_id,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=(
            AnalysisFact("schema_version", projection.schema_version),
            AnalysisFact("migration_count", projection.migration_count, "count"),
            AnalysisFact("table_count", projection.table_count, "count"),
            AnalysisFact("index_count", projection.index_count, "count"),
            AnalysisFact("trigger_count", projection.trigger_count, "count"),
            AnalysisFact("view_count", projection.view_count, "count"),
            AnalysisFact("ddl_digest", projection.ddl_digest),
            AnalysisFact("migration_digest", projection.migration_digest),
        ),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="code.sqlite-schema-contract-resolver",
        resolver_version="v1",
        limitations=("code_owner_schema_only",),
    )


def _change_question_evaluation(
    analysis: CodeChangeEvolutionAnalysis,
    *,
    rank: int,
) -> AnalysisQuestionEvaluation:
    spec = CHANGE_SURFACE_QUESTION
    subject = _run_subject(analysis, question_id=spec.question_id)
    evidence = (
        (_change_evidence(analysis.change_surface, subject),)
        if analysis.change_surface.status == "ready"
        and not analysis.change_surface.truncated
        and analysis.change_surface.total_observations > 0
        else ()
    )
    question_ready = bool(evidence)
    observation, readiness, decision_readiness, reason, next_actions = _evaluation_state(
        spec, question_ready=question_ready
    )
    return AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-change-surface-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
                "spec": analysis_question_spec_fingerprint(spec),
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=(
            AnalysisRequirementEvaluation(
                "comparable_publication_transition",
                "satisfied" if evidence else "missing",
                tuple(item.evidence_id for item in evidence),
                "linked_consecutive_comparable_code_publications"
                if evidence
                else analysis.change_surface.reason
                or (
                    "no_change_surface_observation"
                    if analysis.change_surface.status == "ready"
                    else "comparable_publication_transition_missing"
                ),
            ),
            AnalysisRequirementEvaluation(
                "capability_owner_contract_impact_observed",
                "missing",
                (),
                "capability_owner_and_contract_impact_not_linked",
            ),
            AnalysisRequirementEvaluation(
                "change_surface_counterevidence_evaluated",
                "not_evaluated",
                (),
                "change_surface_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "change_surface_characterization_result",
                "missing",
                (),
                "change_surface_characterization_result_missing",
            ),
        ),
        observation_status=observation,
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness=readiness,
        decision_readiness=decision_readiness,
        decision=None,
        decision_reason=reason,
        counterevidence_status="not_evaluated",
        next_action_ids=next_actions,
        limitations=(
            "surface_delta_does_not_prove_improvement_or_regression",
            "rename_move_and_wrapper_changes_do_not_resolve_the_question",
            "corrected_graph_resolution_is_not_change_success_evidence",
            "human_decision_not_owned_by_code_analysis",
        ),
    )


def _history_question_evaluation(
    analysis: CodeChangeEvolutionAnalysis,
    *,
    rank: int,
) -> AnalysisQuestionEvaluation:
    spec = CHANGE_HISTORY_QUESTION
    subject = _run_subject(analysis, question_id=spec.question_id)
    surface_evidence = (
        (_change_evidence(analysis.change_surface, subject),)
        if analysis.change_surface.status == "ready"
        and not analysis.change_surface.truncated
        and analysis.change_surface.total_observations > 0
        else ()
    )
    history_evidence = (
        _history_evidence(analysis.history, subject) if analysis.history.status == "ready" else ()
    )
    evidence = surface_evidence + history_evidence
    question_ready = bool(surface_evidence) and len(history_evidence) == 2
    observation, readiness, decision_readiness, reason, next_actions = _evaluation_state(
        spec, question_ready=question_ready
    )
    metric_ids = history_evidence[:1]
    relation_ids = history_evidence[1:]
    return AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-change-history-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
                "spec": analysis_question_spec_fingerprint(spec),
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=(
            AnalysisRequirementEvaluation(
                "comparable_change_surface",
                "satisfied" if surface_evidence else "missing",
                tuple(item.evidence_id for item in surface_evidence),
                "linked_comparable_change_surface"
                if surface_evidence
                else analysis.change_surface.reason
                or (
                    "no_change_surface_observation"
                    if analysis.change_surface.status == "ready"
                    else "comparable_change_surface_missing"
                ),
            ),
            AnalysisRequirementEvaluation(
                "complete_git_history_metrics",
                "satisfied" if metric_ids else "missing",
                tuple(item.evidence_id for item in metric_ids),
                "linked_complete_git_history_metrics"
                if metric_ids
                else analysis.history.reason or "complete_git_history_metrics_missing",
            ),
            AnalysisRequirementEvaluation(
                "complete_git_cochange_relations",
                "satisfied" if relation_ids else "missing",
                tuple(item.evidence_id for item in relation_ids),
                "linked_complete_git_cochange_relation_set"
                if relation_ids
                else analysis.history.reason or "complete_git_cochange_relations_missing",
            ),
            AnalysisRequirementEvaluation(
                "companion_change_semantics_characterized",
                "missing",
                (),
                "companion_change_semantics_not_characterized",
            ),
            AnalysisRequirementEvaluation(
                "history_counterevidence_evaluated",
                "not_evaluated",
                (),
                "history_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "companion_change_experiment_result",
                "missing",
                (),
                "companion_change_experiment_result_missing",
            ),
        ),
        observation_status=observation,
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness=readiness,
        decision_readiness=decision_readiness,
        decision=None,
        decision_reason=reason,
        counterevidence_status="not_evaluated",
        next_action_ids=next_actions,
        limitations=(
            "cochange_does_not_prove_an_omitted_companion",
            "provider_absence_or_incompleteness_forces_question_abstention",
            "history_does_not_prove_defect_probability",
            "human_decision_not_owned_by_code_analysis",
        ),
    )


def _schema_question_evaluation(
    analysis: CodeChangeEvolutionAnalysis,
    *,
    rank: int,
) -> AnalysisQuestionEvaluation:
    spec = CODE_SCHEMA_EVOLUTION_QUESTION
    subject = _schema_subject(analysis)
    evidence = (
        (_schema_evidence(analysis.code_schema, subject),)
        if analysis.code_schema.status == "ready"
        else ()
    )
    question_ready = bool(evidence)
    observation, readiness, decision_readiness, reason, next_actions = _evaluation_state(
        spec, question_ready=question_ready
    )
    return AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-schema-evolution-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
                "spec": analysis_question_spec_fingerprint(spec),
                "evidence": tuple(item.evidence_id for item in evidence),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=(
            AnalysisRequirementEvaluation(
                "exact_code_schema_and_migration_ledger",
                "satisfied" if evidence else "missing",
                tuple(item.evidence_id for item in evidence),
                "linked_exact_code_schema_and_contiguous_migration_ledger"
                if evidence
                else analysis.code_schema.reason or "exact_code_schema_missing",
            ),
            AnalysisRequirementEvaluation(
                "populated_upgrade_and_recovery_evidence",
                "missing",
                (),
                "populated_upgrade_and_recovery_evidence_not_linked",
            ),
            AnalysisRequirementEvaluation(
                "schema_compatibility_counterevidence_evaluated",
                "not_evaluated",
                (),
                "schema_compatibility_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "schema_upgrade_matrix_result",
                "missing",
                (),
                "schema_upgrade_matrix_result_missing",
            ),
        ),
        observation_status=observation,
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness=readiness,
        decision_readiness=decision_readiness,
        decision=None,
        decision_reason=reason,
        counterevidence_status="not_evaluated",
        next_action_ids=next_actions,
        limitations=(
            "schema_ledger_does_not_prove_populated_migration_safety",
            "schema_scope_is_limited_to_code_owner",
            "human_decision_not_owned_by_code_analysis",
        ),
    )


def expected_code_change_evolution_questions(
    analysis: CodeChangeEvolutionAnalysis,
    *,
    rank_offset: int = 0,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Build the canonical specs/evaluations for later central integration."""

    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("change evolution question rank offset must be non-negative")
    specs = (
        CHANGE_SURFACE_QUESTION,
        CHANGE_HISTORY_QUESTION,
        CODE_SCHEMA_EVOLUTION_QUESTION,
    )
    evaluations = (
        _change_question_evaluation(analysis, rank=rank_offset + 1),
        _history_question_evaluation(analysis, rank=rank_offset + 2),
        _schema_question_evaluation(analysis, rank=rank_offset + 3),
    )
    if rank_offset == 0:
        validate_analysis_question_set(specs, evaluations)
    else:
        for spec, evaluation in zip(specs, evaluations, strict=True):
            # The central registry owns final contiguous-rank validation.
            from .code_analysis_epistemics import validate_analysis_question_evaluation

            validate_analysis_question_evaluation(spec, evaluation)
    return specs, evaluations


def _strict_mapping(
    label: str,
    value: object,
    expected: set[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _sequence(label: str, value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    return value


def _parse_file_change(value: object) -> CodeFileSurfaceChange:
    raw = _strict_mapping(
        "file surface change",
        value,
        {field.name for field in fields(CodeFileSurfaceChange)},
    )
    values = dict(raw)
    values["public_symbols_added"] = _text_tuple(
        "public symbol added", values["public_symbols_added"]
    )
    values["public_symbols_removed"] = _text_tuple(
        "public symbol removed", values["public_symbols_removed"]
    )
    return CodeFileSurfaceChange(**values)  # type: ignore[arg-type]


def _parse_change_surface(value: object) -> CodeChangeSurfaceProjection:
    raw = _strict_mapping(
        "change surface",
        value,
        {field.name for field in fields(CodeChangeSurfaceProjection)},
    )
    values = dict(raw)
    values["observations"] = tuple(
        _parse_file_change(item)
        for item in _sequence("change observations", values["observations"])
    )
    values["limitations"] = _text_tuple("change surface limitation", values["limitations"])
    return CodeChangeSurfaceProjection(**values)  # type: ignore[arg-type]


def _parse_history(value: object) -> CodeHistoryEvolutionProjection:
    raw = _strict_mapping(
        "history evolution",
        value,
        {field.name for field in fields(CodeHistoryEvolutionProjection)},
    )
    values = dict(raw)
    context_keys = {field.name for field in fields(CodeHistoryFileContext)}
    companion_keys = {field.name for field in fields(CodeHistoryCompanion)}
    values["file_contexts"] = tuple(
        CodeHistoryFileContext(
            **dict(_strict_mapping("history file context", item, context_keys))  # type: ignore[arg-type]
        )
        for item in _sequence("history file contexts", values["file_contexts"])
    )
    values["companions"] = tuple(
        CodeHistoryCompanion(
            **dict(_strict_mapping("history companion", item, companion_keys))  # type: ignore[arg-type]
        )
        for item in _sequence("history companions", values["companions"])
    )
    values["limitations"] = _text_tuple("history limitation", values["limitations"])
    return CodeHistoryEvolutionProjection(**values)  # type: ignore[arg-type]


def _parse_schema(value: object) -> CodeSchemaEvolutionProjection:
    raw = _strict_mapping(
        "schema evolution",
        value,
        {field.name for field in fields(CodeSchemaEvolutionProjection)},
    )
    values = dict(raw)
    migration_keys = {field.name for field in fields(CodeSchemaMigrationObservation)}
    values["migrations"] = tuple(
        CodeSchemaMigrationObservation(
            **dict(_strict_mapping("schema migration", item, migration_keys))  # type: ignore[arg-type]
        )
        for item in _sequence("schema migrations", values["migrations"])
    )
    values["limitations"] = _text_tuple("schema limitation", values["limitations"])
    return CodeSchemaEvolutionProjection(**values)  # type: ignore[arg-type]


def parse_code_change_evolution_payload(
    payload: Mapping[str, object],
) -> CodeChangeEvolutionAnalysis:
    """Strictly reconstruct a JSON-compatible v1 analysis projection."""

    expected = {field.name for field in fields(CodeChangeEvolutionAnalysis)} | {"schema"}
    raw = _strict_mapping("change evolution payload", payload, expected)
    if raw.get("schema") != CODE_CHANGE_EVOLUTION_SCHEMA:
        raise ValueError("change evolution payload schema is invalid")
    values = {key: value for key, value in raw.items() if key != "schema"}
    values["change_surface"] = _parse_change_surface(values["change_surface"])
    values["history"] = _parse_history(values["history"])
    values["code_schema"] = _parse_schema(values["code_schema"])
    values["limitations"] = _text_tuple("change evolution limitation", values["limitations"])
    return CodeChangeEvolutionAnalysis(**values)  # type: ignore[arg-type]


__all__ = [
    "CHANGE_HISTORY_QUESTION",
    "CHANGE_SURFACE_QUESTION",
    "CODE_CHANGE_EVOLUTION_LIMIT",
    "CODE_CHANGE_EVOLUTION_MAX_LIMIT",
    "CODE_CHANGE_EVOLUTION_SCHEMA",
    "CODE_CHANGE_SURFACE_POLICY",
    "CODE_HISTORY_CONTEXT_POLICY",
    "CODE_SCHEMA_EVOLUTION_POLICY",
    "CODE_SCHEMA_EVOLUTION_QUESTION",
    "CodeChangeEvolutionAnalysis",
    "CodeChangeEvolutionResolutionError",
    "CodeChangeSurfaceProjection",
    "CodeFileSurfaceChange",
    "CodeHistoryCompanion",
    "CodeHistoryEvolutionProjection",
    "CodeHistoryFileContext",
    "CodeSchemaEvolutionProjection",
    "CodeSchemaMigrationObservation",
    "analyze_code_change_evolution",
    "expected_code_change_evolution_questions",
    "parse_code_change_evolution_payload",
    "read_code_change_evolution_analysis",
]
