"""Exact cross-owner questions over already-published Text and Semantic state.

This module deliberately answers one narrow question.  It does not parse SQL,
infer a workflow, or claim a distributed transaction.  A stable Knowledge
snapshot gates two owner-local read transactions; the published Semantic head
is compared with the exact Text revisions that the production adapter declares
eligible.  The result is an observation, never a defect or repair decision.
"""

from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
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
    validate_analysis_question_evaluation,
)
from neocortex.knowledge.knowledge_contracts import (
    KnowledgeSnapshot,
    LogicalWatermark,
    OwnerAvailability,
    OwnerSnapshot,
    PublicationHead,
    SnapshotConsistency,
)
from neocortex.semantic import semantic_schema as semantic_schema_module
from neocortex.semantic.semantic_schema import SEMANTIC_SCHEMA_VERSION
from neocortex.persistence.sqlite_immutable import (
    ImmutableSQLiteUnavailable,
    capture_sqlite_immutable_fence,
    immutable_sqlite_database,
)
from neocortex.persistence.sqlite_schema_contract import (
    read_application_schema_version,
    validate_sqlite_schema_contract,
)
from neocortex.capabilities.formats.text.text_state import TEXT_SCHEMA_VERSION, text_schema_contract

CODE_STATE_PROJECTION_SCHEMA = "neocortex.code-state-projection/v1"
TEXT_SEMANTIC_PROJECTION_POLICY = "text-semantic-published-head-projection-v2"
TEXT_SEMANTIC_PROJECTION_QUESTION = AnalysisQuestionSpec(
    question_id="state.text_semantic_published_projection_is_aligned",
    version="v2",
    subject_kinds=("workflow",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "stable_cross_owner_snapshot",
            "question",
            "supporting",
            ("internal_fact",),
        ),
        AnalysisEvidenceRequirementSpec(
            "exact_published_head_projection",
            "question",
            "supporting",
            ("internal_relation",),
        ),
        AnalysisEvidenceRequirementSpec(
            "build_recovery_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_fact", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "process_death_recovery_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "published_projection_is_current_and_consistent",
        "observed_delta_is_transient_or_exposes_a_recovery_gap",
    ),
    counterevidence_rules=(
        "active_build_or_reconciliation_can_explain_a_delta",
        "stable_alignment_does_not_prove_process_death_recovery",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_semantic_build_and_reconciliation_state",
            "counterevidence_search",
            "Resolve active builds, freshness, recovery and reconciliation before judging a delta.",
        ),
        AnalysisNextActionSpec(
            "run_semantic_process_death_recovery_experiment",
            "experiment",
            "Terminate staging after a durable prefix and verify head isolation and convergence.",
        ),
    ),
)

_LIMITATIONS = (
    "comparison_is_cross_owner_observation_not_a_distributed_transaction",
    "delta_does_not_prove_defect_while_a_build_or_reconciliation_can_exist",
    "process_death_power_loss_and_recovery_are_not_exercised",
    "only_published_text_modality_heads_are_observed",
    "no_logical_workflow_or_transaction_boundary_is_inferred",
)


class CodeStateProjectionResolutionError(ValueError):
    """The state projection cannot be read completely and consistently."""


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _text_tuple(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item, maximum=4_096) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


def _non_negative_int(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_text(label: str, value: object, *, maximum: int = 32_768) -> str | None:
    if value is None:
        return None
    return _required_text(label, value, maximum=maximum)


def _optional_non_negative_int(label: str, value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_int(label, value)


def _enum_text(label: str, value: object, allowed: set[str]) -> str:
    result = _required_text(label, value, maximum=256)
    if result not in allowed:
        raise ValueError(f"{label} is invalid")
    return result


def _boolean(label: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class TextSemanticHeadProjection:
    projection_id: str
    model_signature: str
    generation_id: int
    generation_status: Literal["ready", "ready_partial"]
    eligible_text_revisions: int
    published_text_revisions: int
    published_chunks: int
    matching_revisions: int
    missing_revision_ids: tuple[str, ...]
    extra_revision_ids: tuple[str, ...]
    invalid_owner_revision_ids: tuple[str, ...]
    invalid_materialization_revision_ids: tuple[str, ...]
    aligned: bool
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("head projection id", self.projection_id, maximum=256)
        _required_text("head model signature", self.model_signature, maximum=2_048)
        _non_negative_int("head generation", self.generation_id)
        if self.generation_id < 1 or self.generation_status not in {"ready", "ready_partial"}:
            raise ValueError("head generation is not a published ready state")
        for label, value in (
            ("eligible revisions", self.eligible_text_revisions),
            ("published revisions", self.published_text_revisions),
            ("published chunks", self.published_chunks),
            ("matching revisions", self.matching_revisions),
        ):
            _non_negative_int(label, value)
        for label, values in (
            ("missing revision", self.missing_revision_ids),
            ("extra revision", self.extra_revision_ids),
            ("invalid owner revision", self.invalid_owner_revision_ids),
            (
                "invalid materialization revision",
                self.invalid_materialization_revision_ids,
            ),
        ):
            _text_tuple(label, values)
        expected_aligned = not (
            self.missing_revision_ids
            or self.extra_revision_ids
            or self.invalid_owner_revision_ids
            or self.invalid_materialization_revision_ids
        ) and self.matching_revisions == self.eligible_text_revisions == (
            self.published_text_revisions
        )
        if self.aligned != expected_aligned:
            raise ValueError("head alignment is not derived from exact revision sets")
        expected_id = analysis_identity(
            "text-semantic-head-projection-v1",
            {
                "model_signature": self.model_signature,
                "generation_id": self.generation_id,
                "generation_status": self.generation_status,
                "eligible_text_revisions": self.eligible_text_revisions,
                "published_text_revisions": self.published_text_revisions,
                "published_chunks": self.published_chunks,
                "matching_revisions": self.matching_revisions,
                "missing_revision_ids": self.missing_revision_ids,
                "extra_revision_ids": self.extra_revision_ids,
                "invalid_owner_revision_ids": self.invalid_owner_revision_ids,
                "invalid_materialization_revision_ids": (self.invalid_materialization_revision_ids),
            },
        )
        if self.projection_id != expected_id:
            raise ValueError("head projection identity is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("head projection must remain advisory and non-mutating")


@dataclass(frozen=True, slots=True)
class CodeStateProjectionAnalysis:
    analysis_id: str
    status: Literal["ready", "abstained"]
    reason: str | None
    policy_id: str
    knowledge_snapshot_id: str | None
    knowledge_consistency: str | None
    text_owner_schema: int | None
    semantic_owner_schema: int | None
    complete_text_rows: int | None
    eligible_text_rows: int | None
    excluded_empty_text_rows: int | None
    excluded_other_text_rows: int | None
    heads: tuple[TextSemanticHeadProjection, ...]
    observation: Literal["aligned", "delta_observed"] | None
    inference_status: Literal["abstained"]
    decision_readiness: Literal["experiment_required", "abstained"]
    decision: None
    next_action_ids: tuple[str, ...]
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        _required_text("state projection id", self.analysis_id, maximum=256)
        if self.status not in {"ready", "abstained"}:
            raise ValueError("state projection status is invalid")
        if self.policy_id != TEXT_SEMANTIC_PROJECTION_POLICY:
            raise ValueError("state projection policy is invalid")
        if self.inference_status != "abstained" or self.decision is not None:
            raise ValueError("state projection cannot infer a defect or own a decision")
        if self.limitations != _LIMITATIONS:
            raise ValueError("state projection limitations are not canonical")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("state projection must remain advisory and non-mutating")
        if self.status == "abstained":
            _required_text("state projection abstention reason", self.reason, maximum=256)
            if (
                any(
                    value is not None
                    for value in (
                        self.knowledge_snapshot_id,
                        self.knowledge_consistency,
                        self.text_owner_schema,
                        self.semantic_owner_schema,
                        self.complete_text_rows,
                        self.eligible_text_rows,
                        self.excluded_empty_text_rows,
                        self.excluded_other_text_rows,
                        self.observation,
                    )
                )
                or self.heads
                or self.next_action_ids
            ):
                raise ValueError("abstained state projection cannot assert partial evidence")
            if self.decision_readiness != "abstained":
                raise ValueError("abstained state projection cannot claim decision readiness")
        else:
            if self.reason is not None:
                raise ValueError("ready state projection cannot carry an abstention reason")
            _required_text(
                "state projection Knowledge snapshot",
                self.knowledge_snapshot_id,
                maximum=2_048,
            )
            if self.knowledge_consistency != "stable":
                raise ValueError("ready state projection requires a stable Knowledge snapshot")
            for label, value in (
                ("text owner schema", self.text_owner_schema),
                ("semantic owner schema", self.semantic_owner_schema),
                ("complete text rows", self.complete_text_rows),
                ("eligible text rows", self.eligible_text_rows),
                ("excluded empty text rows", self.excluded_empty_text_rows),
                ("excluded other text rows", self.excluded_other_text_rows),
            ):
                _non_negative_int(label, value)
            if self.text_owner_schema is None or self.text_owner_schema < 1:
                raise ValueError("ready state projection requires Text schema")
            if self.semantic_owner_schema is None or self.semantic_owner_schema < 1:
                raise ValueError("ready state projection requires Semantic schema")
            assert self.complete_text_rows is not None
            assert self.eligible_text_rows is not None
            assert self.excluded_empty_text_rows is not None
            assert self.excluded_other_text_rows is not None
            if self.complete_text_rows != (
                self.eligible_text_rows
                + self.excluded_empty_text_rows
                + self.excluded_other_text_rows
            ):
                raise ValueError("Text eligibility partition is incomplete")
            if not self.heads:
                raise ValueError("ready state projection requires a published text head")
            expected_observation = (
                "aligned" if all(head.aligned for head in self.heads) else "delta_observed"
            )
            if self.observation != expected_observation:
                raise ValueError("state projection observation is not derived from heads")
            if self.decision_readiness != "experiment_required":
                raise ValueError("state projection cannot become a change decision")
            expected_actions = (
                (
                    "inspect_build_freshness_recovery_and_reconciliation",
                    "design_process_death_experiment_if_delta_persists",
                )
                if self.observation == "delta_observed"
                else ("monitor_published_projection_on_comparable_snapshots",)
            )
            if self.next_action_ids != expected_actions:
                raise ValueError("state projection actions are not canonical")
        expected_id = _analysis_id(
            {key: value for key, value in asdict(self).items() if key != "analysis_id"}
        )
        if self.analysis_id != expected_id:
            raise ValueError("state projection identity is invalid")

    def as_payload(self) -> dict[str, object]:
        return {"schema": CODE_STATE_PROJECTION_SCHEMA, **asdict(self)}


def _analysis_id(values: Mapping[str, object]) -> str:
    payload = dict(values)
    raw_heads = payload.get("heads")
    if isinstance(raw_heads, tuple):
        payload["heads"] = tuple(
            asdict(item) if isinstance(item, TextSemanticHeadProjection) else item
            for item in raw_heads
        )
    return analysis_identity("code-state-projection-analysis-v1", payload)


def abstained_code_state_projection(reason: str) -> CodeStateProjectionAnalysis:
    values: dict[str, object] = {
        "status": "abstained",
        "reason": _required_text("state projection abstention reason", reason, maximum=256),
        "policy_id": TEXT_SEMANTIC_PROJECTION_POLICY,
        "knowledge_snapshot_id": None,
        "knowledge_consistency": None,
        "text_owner_schema": None,
        "semantic_owner_schema": None,
        "complete_text_rows": None,
        "eligible_text_rows": None,
        "excluded_empty_text_rows": None,
        "excluded_other_text_rows": None,
        "heads": (),
        "observation": None,
        "inference_status": "abstained",
        "decision_readiness": "abstained",
        "decision": None,
        "next_action_ids": (),
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeStateProjectionAnalysis(
        analysis_id=_analysis_id(values),
        **values,  # type: ignore[arg-type]
    )


def _eligible_text_revisions(connection: sqlite3.Connection) -> tuple[int, set[str], int, int]:
    rows = connection.execute(
        """SELECT revision_id,text_chars,text_zlib IS NOT NULL AS has_blob
        FROM documents
        WHERE status='complete' AND revision_id IS NOT NULL
        ORDER BY revision_id"""
    ).fetchall()
    eligible: set[str] = set()
    excluded_empty = 0
    excluded_other = 0
    for row in rows:
        revision_id = _required_text("Text revision id", row["revision_id"], maximum=2_048)
        text_chars = row["text_chars"]
        has_blob = row["has_blob"]
        if not isinstance(text_chars, int) or text_chars < 0 or has_blob not in {0, 1}:
            raise CodeStateProjectionResolutionError("Text eligibility fields are invalid")
        if text_chars > 0 and has_blob == 1:
            if revision_id in eligible:
                raise CodeStateProjectionResolutionError("Text eligible revision repeats")
            eligible.add(revision_id)
        elif text_chars == 0:
            excluded_empty += 1
        else:
            excluded_other += 1
    return len(rows), eligible, excluded_empty, excluded_other


def _published_text_heads(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, int, str, dict[str, list[tuple[str, str, str]]]], ...]:
    heads = connection.execute(
        """SELECT h.model_signature,h.generation_id,g.status
        FROM published_embedding_heads h
        JOIN embedding_models m ON m.model_signature=h.model_signature
        JOIN embedding_generations g ON g.generation_id=h.generation_id
        WHERE m.modality='text' AND g.model_signature=h.model_signature
          AND g.status IN ('ready','ready_partial')
        ORDER BY h.model_signature"""
    ).fetchall()
    result: list[tuple[str, int, str, dict[str, list[tuple[str, str, str]]]]] = []
    for head in heads:
        model_signature = _required_text(
            "Semantic model signature", head["model_signature"], maximum=2_048
        )
        generation_id = head["generation_id"]
        status = head["status"]
        if not isinstance(generation_id, int) or generation_id < 1:
            raise CodeStateProjectionResolutionError("Semantic head generation is invalid")
        if status not in {"ready", "ready_partial"}:
            raise CodeStateProjectionResolutionError("Semantic head status is invalid")
        rows = connection.execute(
            """SELECT r.source_revision_json
            FROM embedding_generation_members gm
            JOIN semantic_item_revisions r ON r.item_revision_id=gm.item_revision_id
            WHERE gm.generation_id=? AND gm.model_signature=?
              AND gm.entity_kind='text_chunk' AND r.source_kind='text'
            ORDER BY gm.member_id""",
            (generation_id, model_signature),
        ).fetchall()
        revisions: dict[str, list[tuple[str, str, str]]] = {}
        for row in rows:
            try:
                source = json.loads(_required_text("source revision JSON", row[0]))
                revision_id = _required_text(
                    "Semantic source revision id", source["revision_id"], maximum=2_048
                )
                source_owner = _required_text(
                    "Semantic source owner",
                    source["owner_revision"]["owner"],
                    maximum=128,
                )
                materialization = source["consumed_materialization"]["materialization"]
                materialization_owner = _required_text(
                    "Semantic materialization owner", materialization["owner"], maximum=128
                )
                materialization_kind = _required_text(
                    "Semantic materialization kind",
                    materialization["materialization_kind"],
                    maximum=128,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CodeStateProjectionResolutionError(
                    "Semantic text member lacks its owner-native revision contract"
                ) from exc
            revisions.setdefault(revision_id, []).append(
                (source_owner, materialization_owner, materialization_kind)
            )
        result.append((model_signature, generation_id, str(status), revisions))
    return tuple(result)


def _head_projection(
    *,
    model_signature: str,
    generation_id: int,
    generation_status: str,
    eligible: set[str],
    revisions: dict[str, list[tuple[str, str, str]]],
) -> TextSemanticHeadProjection:
    published = set(revisions)
    invalid_owner = tuple(
        sorted(
            revision_id
            for revision_id, contracts in revisions.items()
            if any(owner != "text" for owner, _materialization_owner, _kind in contracts)
        )
    )
    invalid_materialization = tuple(
        sorted(
            revision_id
            for revision_id, contracts in revisions.items()
            if any(
                materialization_owner != "text" or kind != "text_representation"
                for _owner, materialization_owner, kind in contracts
            )
        )
    )
    values = {
        "model_signature": model_signature,
        "generation_id": generation_id,
        "generation_status": generation_status,
        "eligible_text_revisions": len(eligible),
        "published_text_revisions": len(published),
        "published_chunks": sum(len(contracts) for contracts in revisions.values()),
        "matching_revisions": len(eligible & published),
        "missing_revision_ids": tuple(sorted(eligible - published)),
        "extra_revision_ids": tuple(sorted(published - eligible)),
        "invalid_owner_revision_ids": invalid_owner,
        "invalid_materialization_revision_ids": invalid_materialization,
    }
    aligned = (
        not any(
            values[key]
            for key in (
                "missing_revision_ids",
                "extra_revision_ids",
                "invalid_owner_revision_ids",
                "invalid_materialization_revision_ids",
            )
        )
        and values["matching_revisions"]
        == values["eligible_text_revisions"]
        == values["published_text_revisions"]
    )
    projection_id = analysis_identity("text-semantic-head-projection-v1", values)
    return TextSemanticHeadProjection(
        projection_id=projection_id,
        aligned=bool(aligned),
        **values,  # type: ignore[arg-type]
    )


def _schema_version(connection: sqlite3.Connection, *, owner: str) -> int:
    value = read_application_schema_version(connection, label=owner)
    if value is None:
        raise CodeStateProjectionResolutionError(f"{owner} schema version is absent")
    return value


def _projection_snapshot(
    *,
    source_version: str,
    text_schema: int,
    semantic_schema: int,
    complete: int,
    eligible: int,
    excluded_empty: int,
    excluded_other: int,
    head_rows: tuple[tuple[str, int, str, dict[str, list[tuple[str, str, str]]]], ...],
) -> KnowledgeSnapshot:
    publications = tuple(
        PublicationHead(
            scope=f"model:{model_signature}",
            publication_id=f"semantic:{generation_id}",
            generation=generation_id,
            model_signature=model_signature,
        )
        for model_signature, generation_id, _status, _revisions in head_rows
    )
    return KnowledgeSnapshot.create(
        source_version=source_version,
        captured_at_utc=datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        captured_monotonic_ns=time.monotonic_ns(),
        owners=(
            OwnerSnapshot(
                owner="text",
                state=OwnerAvailability.AVAILABLE,
                expected_schema_version=TEXT_SCHEMA_VERSION,
                observed_schema_version=text_schema,
                watermarks=(
                    LogicalWatermark("complete_rows", str(complete)),
                    LogicalWatermark("eligible_rows", str(eligible)),
                    LogicalWatermark("excluded_empty_rows", str(excluded_empty)),
                    LogicalWatermark("excluded_other_rows", str(excluded_other)),
                ),
                data_version_before=1,
                data_version_after=1,
            ),
            OwnerSnapshot(
                owner="semantic",
                state=OwnerAvailability.AVAILABLE,
                expected_schema_version=SEMANTIC_SCHEMA_VERSION,
                observed_schema_version=semantic_schema,
                publications=publications,
                watermarks=(LogicalWatermark("published_text_heads", str(len(publications))),),
                data_version_before=1,
                data_version_after=1,
            ),
        ),
        consistency=SnapshotConsistency.STABLE,
    )


def analyze_text_semantic_projection(
    state_directory: Path,
    *,
    source_version: str,
) -> CodeStateProjectionAnalysis:
    """Compare eligible Text revisions with published Semantic text heads."""

    state_directory = Path(state_directory)
    text_path = state_directory / "text.sqlite3"
    semantic_path = state_directory / "semantic.sqlite3"
    try:
        fences_before = (
            capture_sqlite_immutable_fence(text_path),
            capture_sqlite_immutable_fence(semantic_path),
        )
        with immutable_sqlite_database(text_path) as text_connection:
            text_schema = _schema_version(text_connection, owner="text")
            if text_schema != TEXT_SCHEMA_VERSION:
                raise CodeStateProjectionResolutionError("Text schema is not current")
            validate_sqlite_schema_contract(
                text_connection,
                text_schema_contract(),
                label="text",
                exact=True,
            )
            complete, eligible, excluded_empty, excluded_other = _eligible_text_revisions(
                text_connection
            )
        with immutable_sqlite_database(semantic_path) as semantic_connection:
            semantic_schema = _schema_version(semantic_connection, owner="semantic")
            pragma_schema = int(semantic_connection.execute("PRAGMA user_version").fetchone()[0])
            if semantic_schema != SEMANTIC_SCHEMA_VERSION or pragma_schema not in {
                0,
                semantic_schema,
            }:
                raise CodeStateProjectionResolutionError("Semantic schema is not current")
            semantic_schema_module._validate_schema(semantic_connection, semantic_schema)
            head_rows = _published_text_heads(semantic_connection)
        fences_after = (
            capture_sqlite_immutable_fence(text_path),
            capture_sqlite_immutable_fence(semantic_path),
        )
    except (
        ImmutableSQLiteUnavailable,
        KeyError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return abstained_code_state_projection(
            f"text_semantic_projection_unresolvable:{type(exc).__name__}"
        )
    if fences_after != fences_before:
        return abstained_code_state_projection("owner_state_changed_during_projection")
    if not head_rows:
        return abstained_code_state_projection("published_text_semantic_head_missing")
    snapshot = _projection_snapshot(
        source_version=source_version,
        text_schema=text_schema,
        semantic_schema=semantic_schema,
        complete=complete,
        eligible=len(eligible),
        excluded_empty=excluded_empty,
        excluded_other=excluded_other,
        head_rows=head_rows,
    )
    heads = tuple(
        _head_projection(
            model_signature=model_signature,
            generation_id=generation_id,
            generation_status=status,
            eligible=eligible,
            revisions=revisions,
        )
        for model_signature, generation_id, status, revisions in head_rows
    )
    observation: Literal["aligned", "delta_observed"] = (
        "aligned" if all(item.aligned for item in heads) else "delta_observed"
    )
    next_actions = (
        (
            "inspect_build_freshness_recovery_and_reconciliation",
            "design_process_death_experiment_if_delta_persists",
        )
        if observation == "delta_observed"
        else ("monitor_published_projection_on_comparable_snapshots",)
    )
    values: dict[str, object] = {
        "status": "ready",
        "reason": None,
        "policy_id": TEXT_SEMANTIC_PROJECTION_POLICY,
        "knowledge_snapshot_id": snapshot.snapshot_id,
        "knowledge_consistency": str(snapshot.consistency),
        "text_owner_schema": text_schema,
        "semantic_owner_schema": semantic_schema,
        "complete_text_rows": complete,
        "eligible_text_rows": len(eligible),
        "excluded_empty_text_rows": excluded_empty,
        "excluded_other_text_rows": excluded_other,
        "heads": heads,
        "observation": observation,
        "inference_status": "abstained",
        "decision_readiness": "experiment_required",
        "decision": None,
        "next_action_ids": next_actions,
        "limitations": _LIMITATIONS,
        "authority": "advisory",
        "mutation_authority": False,
    }
    return CodeStateProjectionAnalysis(
        analysis_id=_analysis_id(values),
        **values,  # type: ignore[arg-type]
    )


def parse_code_state_projection_payload(
    payload: Mapping[str, object],
) -> CodeStateProjectionAnalysis:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_STATE_PROJECTION_SCHEMA:
        raise ValueError("state projection payload schema is invalid")
    expected_keys = {field.name for field in fields(CodeStateProjectionAnalysis)} | {"schema"}
    if set(payload) != expected_keys:
        raise ValueError("state projection payload fields are invalid")
    raw_heads = payload.get("heads")
    if not isinstance(raw_heads, Sequence) or isinstance(raw_heads, (str, bytes, bytearray)):
        raise ValueError("state projection heads are invalid")
    head_keys = {field.name for field in fields(TextSemanticHeadProjection)}
    heads: list[TextSemanticHeadProjection] = []
    for raw in raw_heads:
        if not isinstance(raw, Mapping) or set(raw) != head_keys:
            raise ValueError("state projection head fields are invalid")
        generation_status = _enum_text(
            "state projection generation status",
            raw.get("generation_status"),
            {"ready", "ready_partial"},
        )
        if raw.get("authority") != "advisory":
            raise ValueError("state projection head authority must remain advisory")
        mutation_authority = _boolean(
            "state projection head mutation authority",
            raw.get("mutation_authority"),
        )
        if mutation_authority:
            raise ValueError("state projection head cannot own mutation authority")
        heads.append(
            TextSemanticHeadProjection(
                projection_id=_required_text(
                    "state projection head id", raw.get("projection_id"), maximum=256
                ),
                model_signature=_required_text(
                    "state projection model signature",
                    raw.get("model_signature"),
                    maximum=2_048,
                ),
                generation_id=_non_negative_int(
                    "state projection generation id", raw.get("generation_id")
                ),
                generation_status=cast(Literal["ready", "ready_partial"], generation_status),
                eligible_text_revisions=_non_negative_int(
                    "state projection eligible revisions",
                    raw.get("eligible_text_revisions"),
                ),
                published_text_revisions=_non_negative_int(
                    "state projection published revisions",
                    raw.get("published_text_revisions"),
                ),
                published_chunks=_non_negative_int(
                    "state projection published chunks", raw.get("published_chunks")
                ),
                matching_revisions=_non_negative_int(
                    "state projection matching revisions", raw.get("matching_revisions")
                ),
                missing_revision_ids=_text_tuple(
                    "state projection missing revision", raw.get("missing_revision_ids")
                ),
                extra_revision_ids=_text_tuple(
                    "state projection extra revision", raw.get("extra_revision_ids")
                ),
                invalid_owner_revision_ids=_text_tuple(
                    "state projection invalid owner revision",
                    raw.get("invalid_owner_revision_ids"),
                ),
                invalid_materialization_revision_ids=_text_tuple(
                    "state projection invalid materialization revision",
                    raw.get("invalid_materialization_revision_ids"),
                ),
                aligned=_boolean("state projection alignment", raw.get("aligned")),
                authority="advisory",
                mutation_authority=mutation_authority,
            )
        )
    status = _enum_text("state projection status", payload.get("status"), {"ready", "abstained"})
    observation_value = payload.get("observation")
    observation = (
        None
        if observation_value is None
        else _enum_text(
            "state projection observation",
            observation_value,
            {"aligned", "delta_observed"},
        )
    )
    inference_status = _enum_text(
        "state projection inference status",
        payload.get("inference_status"),
        {"abstained"},
    )
    decision_readiness = _enum_text(
        "state projection decision readiness",
        payload.get("decision_readiness"),
        {"experiment_required", "abstained"},
    )
    if payload.get("decision") is not None:
        raise ValueError("state projection decision must be null")
    if payload.get("authority") != "advisory":
        raise ValueError("state projection authority must remain advisory")
    mutation_authority = _boolean(
        "state projection mutation authority", payload.get("mutation_authority")
    )
    if mutation_authority:
        raise ValueError("state projection cannot own mutation authority")
    return CodeStateProjectionAnalysis(
        analysis_id=_required_text(
            "state projection analysis id", payload.get("analysis_id"), maximum=256
        ),
        status=cast(Literal["ready", "abstained"], status),
        reason=_optional_text("state projection reason", payload.get("reason"), maximum=256),
        policy_id=_required_text("state projection policy", payload.get("policy_id"), maximum=256),
        knowledge_snapshot_id=_optional_text(
            "state projection Knowledge snapshot",
            payload.get("knowledge_snapshot_id"),
            maximum=2_048,
        ),
        knowledge_consistency=_optional_text(
            "state projection Knowledge consistency",
            payload.get("knowledge_consistency"),
            maximum=256,
        ),
        text_owner_schema=_optional_non_negative_int(
            "state projection Text schema", payload.get("text_owner_schema")
        ),
        semantic_owner_schema=_optional_non_negative_int(
            "state projection Semantic schema", payload.get("semantic_owner_schema")
        ),
        complete_text_rows=_optional_non_negative_int(
            "state projection complete Text rows", payload.get("complete_text_rows")
        ),
        eligible_text_rows=_optional_non_negative_int(
            "state projection eligible Text rows", payload.get("eligible_text_rows")
        ),
        excluded_empty_text_rows=_optional_non_negative_int(
            "state projection excluded empty Text rows",
            payload.get("excluded_empty_text_rows"),
        ),
        excluded_other_text_rows=_optional_non_negative_int(
            "state projection excluded other Text rows",
            payload.get("excluded_other_text_rows"),
        ),
        heads=tuple(heads),
        observation=cast(Literal["aligned", "delta_observed"] | None, observation),
        inference_status=cast(Literal["abstained"], inference_status),
        decision_readiness=cast(Literal["experiment_required", "abstained"], decision_readiness),
        decision=None,
        next_action_ids=_text_tuple("state projection action", payload.get("next_action_ids")),
        limitations=_text_tuple("state projection limitation", payload.get("limitations")),
        authority="advisory",
        mutation_authority=mutation_authority,
    )


def state_projection_questions(
    analysis: CodeStateProjectionAnalysis,
    *,
    rank: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    """Project the exact Text/Semantic comparison into the generic question wire."""

    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("state projection question rank must be positive")
    spec = TEXT_SEMANTIC_PROJECTION_QUESTION
    fingerprint = analysis_question_spec_fingerprint(spec)
    subject_snapshot = analysis.knowledge_snapshot_id or analysis.analysis_id
    subject = AnalysisSubjectRef(
        subject_kind="workflow",
        subject_key="workflow:text-to-semantic-published-projection",
        display_name="Text to Semantic published projection",
        source_owner_id="knowledge",
        snapshot_id=subject_snapshot,
        snapshot_freshness="current" if analysis.status == "ready" else "unknown",
        revision_id=analysis.policy_id,
    )
    if analysis.status != "ready":
        evaluation = AnalysisQuestionEvaluation(
            evaluation_id=analysis_identity(
                "state-projection-question-evaluation-v1",
                {"analysis_id": analysis.analysis_id, "rank": rank, "spec": fingerprint},
            ),
            question_id=spec.question_id,
            question_version=spec.version,
            question_spec_fingerprint=fingerprint,
            rank=rank,
            subject=subject,
            evidence=(),
            requirements=tuple(
                AnalysisRequirementEvaluation(
                    requirement.requirement_id,
                    "not_evaluated" if requirement.role == "counterevidence" else "missing",
                    (),
                    analysis.reason or "state_projection_resolution_abstained",
                )
                for requirement in spec.requirements
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
            limitations=(*_LIMITATIONS, "state_projection_resolution_abstained"),
        )
        validate_analysis_question_evaluation(spec, evaluation)
        return (spec,), (evaluation,)
    assert analysis.knowledge_snapshot_id is not None
    stable_projection = {
        "knowledge_snapshot_id": analysis.knowledge_snapshot_id,
        "knowledge_consistency": analysis.knowledge_consistency,
        "text_owner_schema": analysis.text_owner_schema,
        "semantic_owner_schema": analysis.semantic_owner_schema,
        "complete_text_rows": analysis.complete_text_rows,
        "eligible_text_rows": analysis.eligible_text_rows,
        "excluded_empty_text_rows": analysis.excluded_empty_text_rows,
        "excluded_other_text_rows": analysis.excluded_other_text_rows,
    }
    stable_digest = analysis_identity("state-projection-snapshot-source-v1", stable_projection)
    stable = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "state-projection-snapshot-evidence-v1",
            {"snapshot": analysis.knowledge_snapshot_id, "digest": stable_digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_fact",
        source_owner_id="knowledge",
        producer_id="text-semantic-published-projection-resolver",
        producer_version="v1",
        source_schema=CODE_STATE_PROJECTION_SCHEMA,
        source_record_kind="stable_cross_owner_snapshot",
        source_record_id=analysis.analysis_id,
        source_projection_digest=stable_digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=tuple(AnalysisFact(key, value) for key, value in stable_projection.items()),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="text-semantic-published-projection-resolver",
        resolver_version="v1",
        limitations=("stable_snapshot_is_observational_not_transactional",),
    )
    head_projection = {
        "observation": analysis.observation,
        "heads": len(analysis.heads),
        "aligned_heads": sum(item.aligned for item in analysis.heads),
        "missing_revisions": sum(len(item.missing_revision_ids) for item in analysis.heads),
        "extra_revisions": sum(len(item.extra_revision_ids) for item in analysis.heads),
        "invalid_owner_revisions": sum(
            len(item.invalid_owner_revision_ids) for item in analysis.heads
        ),
        "invalid_materialization_revisions": sum(
            len(item.invalid_materialization_revision_ids) for item in analysis.heads
        ),
    }
    head_digest = analysis_identity("state-projection-head-source-v1", head_projection)
    heads = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "state-projection-head-evidence-v1",
            {"snapshot": analysis.knowledge_snapshot_id, "digest": head_digest},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_relation",
        source_owner_id="semantic",
        producer_id="text-semantic-published-projection-resolver",
        producer_version="v1",
        source_schema=CODE_STATE_PROJECTION_SCHEMA,
        source_record_kind="published_head_revision_projection",
        source_record_id=analysis.analysis_id,
        source_projection_digest=head_digest,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=tuple(AnalysisFact(key, value) for key, value in head_projection.items()),
        completeness="complete",
        bounded=False,
        truncated=False,
        resolver_id="text-semantic-published-projection-resolver",
        resolver_version="v1",
        limitations=("published_head_alignment_does_not_prove_recovery",),
    )
    evaluation = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "state-projection-question-evaluation-v1",
            {
                "analysis_id": analysis.analysis_id,
                "rank": rank,
                "spec": fingerprint,
                "evidence": (stable.evidence_id, heads.evidence_id),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=fingerprint,
        rank=rank,
        subject=subject,
        evidence=(stable, heads),
        requirements=(
            AnalysisRequirementEvaluation(
                "stable_cross_owner_snapshot",
                "satisfied",
                (stable.evidence_id,),
                "stable_knowledge_snapshot_resolved",
            ),
            AnalysisRequirementEvaluation(
                "exact_published_head_projection",
                "satisfied",
                (heads.evidence_id,),
                "exact_revision_sets_resolved_for_all_published_text_heads",
            ),
            AnalysisRequirementEvaluation(
                "build_recovery_counterevidence_evaluated",
                "not_evaluated",
                (),
                "build_and_reconciliation_state_not_linked",
            ),
            AnalysisRequirementEvaluation(
                "process_death_recovery_experiment_result",
                "missing",
                (),
                "process_death_recovery_result_not_linked",
            ),
        ),
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
    "CODE_STATE_PROJECTION_SCHEMA",
    "TEXT_SEMANTIC_PROJECTION_POLICY",
    "TEXT_SEMANTIC_PROJECTION_QUESTION",
    "CodeStateProjectionAnalysis",
    "CodeStateProjectionResolutionError",
    "TextSemanticHeadProjection",
    "abstained_code_state_projection",
    "analyze_text_semantic_projection",
    "parse_code_state_projection_payload",
    "state_projection_questions",
]
