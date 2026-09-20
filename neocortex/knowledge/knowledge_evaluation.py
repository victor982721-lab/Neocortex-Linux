"""Executable, versioned Phase-1 Knowledge golden evaluation.

The golden file contains expectations and bounded scripted owner candidates,
never precomputed outcomes.  ``run_golden_suite`` executes the live
deterministic planner, evidence fusion and context compiler for every case; the
snapshot-change case additionally crosses the real service retry boundary via
injected read-only seams.  This validates orchestration and metric contracts,
not semantic-model quality or production latency.
"""

from __future__ import annotations
import json
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .knowledge_contracts import KnowledgeSnapshot, RevisionState
    from .knowledge_planner import KnowledgePlan, KnowledgeQuery
    from .knowledge_search import KnowledgeCandidate, KnowledgeSearchResult
    from .knowledge_snapshot import KnowledgeStatePaths


# region [01] Versioned executable golden input


KNOWLEDGE_EVALUATION_SCHEMA_VERSION = 1
MAX_GOLDEN_FIXTURE_BYTES = 4 * 1024 * 1024
SCRIPTED_FIXTURE_LIMITATION = (
    "Scripted owner candidates exercise live planning, fusion, context, and "
    "service seams; they do not validate semantic model quality."
)

_REQUIRED_CASE_NAMES = (
    "exact_identifier",
    "lexical",
    "semantic_paraphrase",
    "multiple_evidence_same_resource",
    "two_sources_formats_same_answer",
    "current_vs_superseded",
    "exact_duplicate",
    "contradiction",
    "no_answer",
    "incomplete_by_limit",
    "snapshot_changes",
    "absent_owner_base",
    "future_schema",
    "unicode_spaces_hash_path",
)
REQUIRED_GOLDEN_CATEGORIES = frozenset(_REQUIRED_CASE_NAMES)
REQUIRED_GOLDEN_GROUPS: tuple[tuple[str, frozenset[str]], ...] = tuple(
    (name, frozenset({name})) for name in _REQUIRED_CASE_NAMES
)


class EvaluationProvenance(StrEnum):
    SCRIPTED_CANDIDATES = "scripted_candidates"


class EvaluationOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    NO_EVIDENCE = "no_evidence"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    SNAPSHOT_CHANGED = "snapshot_changed"


class EvaluationRevisionState(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"


class EvaluationDisposition(StrEnum):
    CANONICAL = "canonical"
    DUPLICATE = "duplicate"


class EvaluationOwnerCondition(StrEnum):
    AVAILABLE = "available"
    ABSENT = "absent"
    FUTURE = "future"


def _required_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be blank")
    return normalized


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    kind: str
    value: str
    section_index: int | None = None
    section_count: int | None = None

    def __post_init__(self) -> None:
        _required_text("locator kind", self.kind)
        _required_text("locator value", self.value)
        if (self.section_index is None) != (self.section_count is None):
            raise ValueError("section locator requires index and count together")
        if self.section_index is not None and (
            isinstance(self.section_index, bool)
            or isinstance(self.section_count, bool)
            or self.section_index < 1
            or self.section_count is None
            or self.section_index > self.section_count
        ):
            raise ValueError("section locator index/count is invalid")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind, "value": self.value}
        if self.section_index is not None:
            payload["section_index"] = self.section_index
            payload["section_count"] = self.section_count
        return payload


@dataclass(frozen=True, slots=True)
class RelevanceJudgment:
    evidence_id: str
    gain: int
    source_kind: str
    format: str
    revision_state: EvaluationRevisionState
    locator: EvidenceLocator

    def __post_init__(self) -> None:
        _required_text("relevant evidence_id", self.evidence_id)
        _required_text("relevant source_kind", self.source_kind)
        _required_text("relevant format", self.format)
        if isinstance(self.gain, bool) or not 1 <= self.gain <= 3:
            raise ValueError("relevance gain must be an integer from 1 to 3")

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "gain": self.gain,
            "source_kind": self.source_kind,
            "format": self.format,
            "revision_state": self.revision_state.value,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    evidence_id: str
    resource_id: str
    source_kind: str
    format: str
    revision_state: EvaluationRevisionState
    locator: EvidenceLocator
    source_rank: int
    raw_score: float
    disposition: EvaluationDisposition = EvaluationDisposition.CANONICAL
    canonical_resource_id: str | None = None
    current_path: str | None = None
    snippet: str | None = None
    claim_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate evidence_id", self.evidence_id),
            ("candidate resource_id", self.resource_id),
            ("candidate source_kind", self.source_kind),
            ("candidate format", self.format),
        ):
            _required_text(name, value)
        if isinstance(self.source_rank, bool) or self.source_rank < 1:
            raise ValueError("candidate source_rank must be positive")
        if not math.isfinite(self.raw_score):
            raise ValueError("candidate raw_score must be finite")
        for name, optional_value in (
            ("candidate current_path", self.current_path),
            ("candidate snippet", self.snippet),
            ("candidate claim_id", self.claim_id),
        ):
            if optional_value is not None:
                _required_text(name, optional_value)
        if self.disposition is EvaluationDisposition.DUPLICATE:
            if self.canonical_resource_id is None:
                raise ValueError("duplicate candidate requires canonical_resource_id")
            _required_text("canonical_resource_id", self.canonical_resource_id)
            if self.canonical_resource_id == self.resource_id:
                raise ValueError("duplicate resource cannot canonicalize to itself")
        elif self.canonical_resource_id is not None:
            raise ValueError("canonical candidate cannot name canonical_resource_id")

    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.resource_id,
            self.source_kind,
            self.format,
            self.revision_state,
            self.locator,
            self.disposition,
            self.canonical_resource_id,
            self.current_path,
            self.snippet,
            self.claim_id,
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "resource_id": self.resource_id,
            "source_kind": self.source_kind,
            "format": self.format,
            "revision_state": self.revision_state.value,
            "locator": self.locator.to_dict(),
            "source_rank": self.source_rank,
            "raw_score": self.raw_score,
            "disposition": self.disposition.value,
        }
        for name, value in (
            ("canonical_resource_id", self.canonical_resource_id),
            ("current_path", self.current_path),
            ("snippet", self.snippet),
            ("claim_id", self.claim_id),
        ):
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class CandidateRanking:
    name: str
    channel: str
    candidates: tuple[CandidateEvidence, ...]

    def __post_init__(self) -> None:
        _required_text("ranking name", self.name)
        _required_text("ranking channel", self.channel)
        evidence_ids = [item.evidence_id for item in self.candidates]
        ranks = [item.source_rank for item in self.candidates]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("one ranking cannot repeat an evidence_id")
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("candidate source ranks must be contiguous and ordered")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "channel": self.channel,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class CitationRef:
    evidence_id: str
    locator: EvidenceLocator

    def __post_init__(self) -> None:
        _required_text("citation evidence_id", self.evidence_id)

    def to_dict(self) -> dict[str, object]:
        return {"evidence_id": self.evidence_id, "locator": self.locator.to_dict()}


@dataclass(frozen=True, slots=True)
class OwnerObservation:
    owner: str
    condition: EvaluationOwnerCondition
    expected_schema_version: int
    observed_schema_version: int | None

    def __post_init__(self) -> None:
        _required_text("owner", self.owner)
        if isinstance(self.expected_schema_version, bool) or (
            self.expected_schema_version < 1
        ):
            raise ValueError("expected owner schema version must be positive")
        if self.observed_schema_version is not None and (
            isinstance(self.observed_schema_version, bool)
            or self.observed_schema_version < 0
        ):
            raise ValueError("observed owner schema version cannot be negative")
        if self.condition is EvaluationOwnerCondition.AVAILABLE and (
            self.observed_schema_version != self.expected_schema_version
        ):
            raise ValueError("available owner must expose the expected schema")
        if self.condition is EvaluationOwnerCondition.ABSENT and (
            self.observed_schema_version is not None
        ):
            raise ValueError("absent owner cannot expose a schema")
        if self.condition is EvaluationOwnerCondition.FUTURE and (
            self.observed_schema_version is None
            or self.observed_schema_version <= self.expected_schema_version
        ):
            raise ValueError("future owner must expose a newer schema")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "owner": self.owner,
            "condition": self.condition.value,
            "expected_schema_version": self.expected_schema_version,
        }
        if self.observed_schema_version is not None:
            payload["observed_schema_version"] = self.observed_schema_version
        return payload


@dataclass(frozen=True, slots=True)
class ClaimRef:
    claim_id: str
    topic: str
    value: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text("claim_id", self.claim_id)
        _required_text("claim topic", self.topic)
        _required_text("claim value", self.value)
        if not self.evidence_ids or len(set(self.evidence_ids)) != len(
            self.evidence_ids
        ):
            raise ValueError("claim evidence must be non-empty and unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "topic": self.topic,
            "value": self.value,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class ContradictionRef:
    left_claim_id: str
    right_claim_id: str
    relation: str

    def __post_init__(self) -> None:
        _required_text("left_claim_id", self.left_claim_id)
        _required_text("right_claim_id", self.right_claim_id)
        _required_text("contradiction relation", self.relation)
        if self.left_claim_id == self.right_claim_id:
            raise ValueError("a claim cannot contradict itself")

    def to_dict(self) -> dict[str, object]:
        return {
            "left_claim_id": self.left_claim_id,
            "right_claim_id": self.right_claim_id,
            "relation": self.relation,
        }


@dataclass(frozen=True, slots=True)
class RelationHop:
    from_resource_id: str
    relation: str
    to_resource_id: str
    evidence_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("relation from_resource_id", self.from_resource_id),
            ("relation", self.relation),
            ("relation to_resource_id", self.to_resource_id),
            ("relation evidence_id", self.evidence_id),
        ):
            _required_text(name, value)
        if self.from_resource_id == self.to_resource_id:
            raise ValueError("relation hop cannot point to the same resource")

    def to_dict(self) -> dict[str, object]:
        return {
            "from_resource_id": self.from_resource_id,
            "relation": self.relation,
            "to_resource_id": self.to_resource_id,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True, slots=True)
class SnapshotTransition:
    changes_during_attempts: int

    def __post_init__(self) -> None:
        if self.changes_during_attempts not in {0, 2}:
            raise ValueError("snapshot transition changes must be zero or two")

    def to_dict(self) -> dict[str, object]:
        return {"changes_during_attempts": self.changes_during_attempts}


@dataclass(frozen=True, slots=True)
class QueryOptions:
    source_kinds: tuple[str, ...]
    formats: tuple[str, ...]
    project: str | None
    include_history: bool

    def __post_init__(self) -> None:
        if not isinstance(self.include_history, bool):
            raise ValueError("include_history must be boolean")
        for value in (*self.source_kinds, *self.formats):
            _required_text("query option", value)
        if self.project is not None:
            _required_text("project", self.project)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_kinds": list(self.source_kinds),
            "formats": list(self.formats),
            "include_history": self.include_history,
        }
        if self.project is not None:
            payload["project"] = self.project
        return payload


@dataclass(frozen=True, slots=True)
class CaseLimits:
    limit: int
    max_per_resource: int
    min_section_distance: int
    max_vectors: int

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not 1 <= self.limit <= 1_000:
            raise ValueError("case limit must be between 1 and 1000")
        if (
            isinstance(self.max_per_resource, bool)
            or not 1 <= self.max_per_resource <= 100
        ):
            raise ValueError("case max_per_resource must be between 1 and 100")
        if isinstance(self.min_section_distance, bool) or self.min_section_distance < 0:
            raise ValueError("case min_section_distance cannot be negative")
        if isinstance(self.max_vectors, bool) or self.max_vectors < 1:
            raise ValueError("case max_vectors must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "max_per_resource": self.max_per_resource,
            "min_section_distance": self.min_section_distance,
            "max_vectors": self.max_vectors,
        }


@dataclass(frozen=True, slots=True)
class GoldenCase:
    scenario_id: str
    category: str
    query: str
    query_options: QueryOptions
    limits: CaseLimits
    required_plan_steps: tuple[str, ...]
    forbidden_plan_steps: tuple[str, ...]
    expected_outcome: EvaluationOutcome
    expected_abstain: bool
    relevant_evidence: tuple[RelevanceJudgment, ...]
    expected_retrieved_ids: tuple[str, ...]
    expected_citations: tuple[CitationRef, ...]
    owner_conditions: tuple[OwnerObservation, ...]
    rankings: tuple[CandidateRanking, ...]
    claims: tuple[ClaimRef, ...]
    contradictions: tuple[ContradictionRef, ...]
    relation_hops: tuple[RelationHop, ...]
    snapshot_transition: SnapshotTransition | None
    expected_filtered_duplicates: int
    expected_excluded_revisions: int
    expected_omitted_by_limit: int
    expected_contradictions: int

    def __post_init__(self) -> None:
        _required_text("scenario_id", self.scenario_id)
        _required_text("case query", self.query)
        if self.category not in REQUIRED_GOLDEN_CATEGORIES:
            raise ValueError("case category is not a required Phase-1 category")
        if not isinstance(self.expected_abstain, bool):
            raise ValueError("expected_abstain must be boolean")
        for field_name, values in (
            ("required plan steps", self.required_plan_steps),
            ("forbidden plan steps", self.forbidden_plan_steps),
            ("expected retrieved ids", self.expected_retrieved_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
            for value in values:
                _required_text(field_name, value)
        for name, count in (
            ("expected_filtered_duplicates", self.expected_filtered_duplicates),
            ("expected_excluded_revisions", self.expected_excluded_revisions),
            ("expected_omitted_by_limit", self.expected_omitted_by_limit),
            ("expected_contradictions", self.expected_contradictions),
        ):
            _nonnegative_int(name, count)
        relevant = {item.evidence_id: item for item in self.relevant_evidence}
        if len(relevant) != len(self.relevant_evidence):
            raise ValueError("relevant evidence identities must be unique")
        expected_citations = {
            citation.evidence_id: citation for citation in self.expected_citations
        }
        if len(expected_citations) != len(self.expected_citations):
            raise ValueError("expected citation evidence identities must be unique")
        for evidence_id, citation in expected_citations.items():
            judgment = relevant.get(evidence_id)
            if judgment is None or judgment.locator != citation.locator:
                raise ValueError(
                    "expected citation must match relevant evidence locator"
                )
        ranking_names = [ranking.name for ranking in self.rankings]
        if len(set(ranking_names)) != len(ranking_names):
            raise ValueError("candidate ranking names must be unique")
        candidate_contracts: dict[str, tuple[object, ...]] = {}
        candidates: dict[str, CandidateEvidence] = {}
        for ranking in self.rankings:
            for candidate in ranking.candidates:
                prior = candidate_contracts.setdefault(
                    candidate.evidence_id, candidate.identity_tuple()
                )
                if prior != candidate.identity_tuple():
                    raise ValueError(
                        "one evidence_id has incompatible candidate records"
                    )
                candidates.setdefault(candidate.evidence_id, candidate)
        if not set(self.expected_retrieved_ids).issubset(candidates):
            raise ValueError("expected retrieved evidence must be a scripted candidate")
        owners = [owner.owner for owner in self.owner_conditions]
        if not owners or len(set(owners)) != len(owners):
            raise ValueError("owner observations must be non-empty and unique")
        claims = {claim.claim_id: claim for claim in self.claims}
        if len(claims) != len(self.claims):
            raise ValueError("claim identities must be unique")
        for claim in self.claims:
            if not set(claim.evidence_ids).issubset(candidates):
                raise ValueError("claim refers to unknown candidate evidence")
        for candidate in candidates.values():
            if candidate.claim_id is None:
                continue
            resolved_claim = claims.get(candidate.claim_id)
            if (
                resolved_claim is None
                or candidate.evidence_id not in resolved_claim.evidence_ids
            ):
                raise ValueError("candidate claim_id is not evidence-backed")
        for contradiction in self.contradictions:
            if (
                contradiction.left_claim_id not in claims
                or contradiction.right_claim_id not in claims
            ):
                raise ValueError("contradiction refers to unknown claim")
        for hop in self.relation_hops:
            if hop.evidence_id not in candidates:
                raise ValueError("relation hop refers to unknown candidate evidence")
        self._validate_required_feature(candidates)

    def _validate_required_feature(
        self, candidates: Mapping[str, CandidateEvidence]
    ) -> None:
        expected = tuple(candidates[value] for value in self.expected_retrieved_ids)
        if self.category == "exact_identifier" and not any(
            item.locator.kind == "identifier" for item in self.relevant_evidence
        ):
            raise ValueError("exact identifier case needs an identifier locator")
        if self.category == "lexical" and "lexical" not in self.required_plan_steps:
            raise ValueError("lexical case must require lexical planning")
        if self.category == "semantic_paraphrase" and (
            "semantic" not in self.required_plan_steps
        ):
            raise ValueError("semantic case must require semantic planning")
        if self.category == "multiple_evidence_same_resource":
            counts = Counter(item.resource_id for item in expected)
            if not counts or max(counts.values()) < 2:
                raise ValueError("multiple evidence case needs a shared resource")
        if self.category == "two_sources_formats_same_answer" and (
            len({item.source_kind for item in expected}) < 2
            or len({item.format for item in expected}) < 2
            or not self.claims
        ):
            raise ValueError("two-source case needs two formats and one claim")
        if self.category == "current_vs_superseded" and (
            not any(
                item.revision_state is EvaluationRevisionState.CURRENT
                for item in expected
            )
            or not any(
                item.revision_state is EvaluationRevisionState.SUPERSEDED
                for item in candidates.values()
            )
        ):
            raise ValueError("revision case needs current and superseded inputs")
        if self.category == "exact_duplicate" and not any(
            item.disposition is EvaluationDisposition.DUPLICATE
            for item in candidates.values()
        ):
            raise ValueError("duplicate case needs an exact duplicate candidate")
        if self.category == "contradiction" and (
            len(self.claims) < 2 or not self.contradictions
        ):
            raise ValueError("contradiction case needs claims and relation")
        if self.category == "no_answer" and (
            self.relevant_evidence
            or self.expected_retrieved_ids
            or not self.expected_abstain
            or self.expected_outcome is not EvaluationOutcome.NO_EVIDENCE
        ):
            raise ValueError("no answer must expect abstention without evidence")
        if self.category == "incomplete_by_limit" and (
            self.expected_omitted_by_limit < 1
            or {item.evidence_id for item in self.relevant_evidence}.issubset(
                self.expected_retrieved_ids
            )
            or self.expected_outcome is not EvaluationOutcome.PARTIAL
        ):
            raise ValueError("limit case must expect bounded incompleteness")
        if self.category == "snapshot_changes" and (
            self.snapshot_transition is None
            or self.snapshot_transition.changes_during_attempts != 2
            or self.expected_outcome is not EvaluationOutcome.SNAPSHOT_CHANGED
        ):
            raise ValueError("snapshot case needs two observed changes")
        if self.category == "absent_owner_base" and not any(
            item.condition is EvaluationOwnerCondition.ABSENT
            for item in self.owner_conditions
        ):
            raise ValueError("absent owner case needs an absent observation")
        if self.category == "future_schema" and not any(
            item.condition is EvaluationOwnerCondition.FUTURE
            for item in self.owner_conditions
        ):
            raise ValueError("future schema case needs a future observation")
        if self.category == "unicode_spaces_hash_path" and not any(
            item.current_path is not None
            and " " in item.current_path
            and "#" in item.current_path
            and any(ord(character) > 127 for character in item.current_path)
            for item in expected
        ):
            raise ValueError("Unicode path case needs Unicode, spaces, and #")

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "query": self.query,
            "query_options": self.query_options.to_dict(),
            "limits": self.limits.to_dict(),
            "required_plan_steps": list(self.required_plan_steps),
            "forbidden_plan_steps": list(self.forbidden_plan_steps),
            "expected_outcome": self.expected_outcome.value,
            "expected_abstain": self.expected_abstain,
            "relevant_evidence": [item.to_dict() for item in self.relevant_evidence],
            "expected_retrieved_ids": list(self.expected_retrieved_ids),
            "expected_citations": [item.to_dict() for item in self.expected_citations],
            "owner_conditions": [item.to_dict() for item in self.owner_conditions],
            "rankings": [item.to_dict() for item in self.rankings],
            "claims": [item.to_dict() for item in self.claims],
            "contradictions": [item.to_dict() for item in self.contradictions],
            "relation_hops": [item.to_dict() for item in self.relation_hops],
            "snapshot_transition": (
                None
                if self.snapshot_transition is None
                else self.snapshot_transition.to_dict()
            ),
            "expected_filtered_duplicates": self.expected_filtered_duplicates,
            "expected_excluded_revisions": self.expected_excluded_revisions,
            "expected_omitted_by_limit": self.expected_omitted_by_limit,
            "expected_contradictions": self.expected_contradictions,
        }


@dataclass(frozen=True, slots=True)
class GoldenSuite:
    schema_version: int
    suite_id: str
    description: str
    provenance: EvaluationProvenance
    limitations: tuple[str, ...]
    cases: tuple[GoldenCase, ...]

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_EVALUATION_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported Knowledge evaluation schema: {self.schema_version}"
            )
        if not isinstance(self.provenance, EvaluationProvenance):
            raise ValueError("provenance must be an EvaluationProvenance")
        _required_text("suite_id", self.suite_id)
        _required_text("suite description", self.description)
        if SCRIPTED_FIXTURE_LIMITATION not in self.limitations:
            raise ValueError("scripted suite must disclose its semantic-quality limit")
        case_ids = [case.scenario_id for case in self.cases]
        categories = [case.category for case in self.cases]
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError("golden case identities must be non-empty and unique")
        if len(set(categories)) != len(categories):
            raise ValueError("golden case categories must be unique")

    @property
    def scripted_fixture(self) -> bool:
        return self.provenance is EvaluationProvenance.SCRIPTED_CANDIDATES

    @property
    def covered_categories(self) -> frozenset[str]:
        return frozenset(case.category for case in self.cases)

    @property
    def covered_groups(self) -> frozenset[str]:
        return self.covered_categories

    @property
    def scenarios(self) -> tuple[GoldenCase, ...]:
        """Compatibility spelling for callers that enumerate golden cases."""

        return self.cases

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "description": self.description,
            "provenance": self.provenance.value,
            "limitations": list(self.limitations),
            "cases": [case.to_dict() for case in self.cases],
        }


# endregion [01]


# region [02] Strict bounded fixture decoding


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{context} keys must be strings")
        result[key] = item
    return result


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a JSON array")
    return value


def _keys(
    payload: Mapping[str, object],
    *,
    context: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    missing = sorted(required.difference(payload))
    unknown = sorted(set(payload).difference(required | optional))
    if missing:
        raise ValueError(f"{context} missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{context} unknown fields: {', '.join(unknown)}")


def _string(payload: Mapping[str, object], key: str, context: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{context}.{key} must be a string")
    return _required_text(f"{context}.{key}", value)


def _optional_string(
    payload: Mapping[str, object], key: str, context: str
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{context}.{key} must be a string or null")
    return _required_text(f"{context}.{key}", value)


def _integer(payload: Mapping[str, object], key: str, context: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}.{key} must be an integer")
    return value


def _optional_integer(
    payload: Mapping[str, object], key: str, context: str
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}.{key} must be an integer or null")
    return value


def _number(payload: Mapping[str, object], key: str, context: str) -> float:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context}.{key} must be numeric")
    return float(value)


def _boolean(payload: Mapping[str, object], key: str, context: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be boolean")
    return value


def _strings(value: object, context: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, item in enumerate(_array(value, context)):
        if not isinstance(item, str):
            raise ValueError(f"{context}[{index}] must be a string")
        result.append(_required_text(f"{context}[{index}]", item))
    return tuple(result)


def _enum(
    enum_type: type[StrEnum], payload: Mapping[str, object], key: str, context: str
) -> StrEnum:
    raw = _string(payload, key, context)
    try:
        return enum_type(raw)
    except ValueError as error:
        raise ValueError(f"{context}.{key} has unsupported value {raw!r}") from error


def _locator(value: object, context: str) -> EvidenceLocator:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"kind", "value"}),
        optional=frozenset({"section_index", "section_count"}),
    )
    return EvidenceLocator(
        _string(payload, "kind", context),
        _string(payload, "value", context),
        _optional_integer(payload, "section_index", context),
        _optional_integer(payload, "section_count", context),
    )


def _relevance(value: object, context: str) -> RelevanceJudgment:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset(
            {
                "evidence_id",
                "gain",
                "source_kind",
                "format",
                "revision_state",
                "locator",
            }
        ),
    )
    return RelevanceJudgment(
        _string(payload, "evidence_id", context),
        _integer(payload, "gain", context),
        _string(payload, "source_kind", context),
        _string(payload, "format", context),
        EvaluationRevisionState(
            _enum(EvaluationRevisionState, payload, "revision_state", context)
        ),
        _locator(payload["locator"], f"{context}.locator"),
    )


def _candidate(value: object, context: str) -> CandidateEvidence:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset(
            {
                "evidence_id",
                "resource_id",
                "source_kind",
                "format",
                "revision_state",
                "locator",
                "source_rank",
                "raw_score",
                "disposition",
            }
        ),
        optional=frozenset(
            {"canonical_resource_id", "current_path", "snippet", "claim_id"}
        ),
    )
    return CandidateEvidence(
        evidence_id=_string(payload, "evidence_id", context),
        resource_id=_string(payload, "resource_id", context),
        source_kind=_string(payload, "source_kind", context),
        format=_string(payload, "format", context),
        revision_state=EvaluationRevisionState(
            _enum(EvaluationRevisionState, payload, "revision_state", context)
        ),
        locator=_locator(payload["locator"], f"{context}.locator"),
        source_rank=_integer(payload, "source_rank", context),
        raw_score=_number(payload, "raw_score", context),
        disposition=EvaluationDisposition(
            _enum(EvaluationDisposition, payload, "disposition", context)
        ),
        canonical_resource_id=_optional_string(
            payload, "canonical_resource_id", context
        ),
        current_path=_optional_string(payload, "current_path", context),
        snippet=_optional_string(payload, "snippet", context),
        claim_id=_optional_string(payload, "claim_id", context),
    )


def _ranking(value: object, context: str) -> CandidateRanking:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"name", "channel", "candidates"}),
    )
    return CandidateRanking(
        _string(payload, "name", context),
        _string(payload, "channel", context),
        tuple(
            _candidate(item, f"{context}.candidates[{index}]")
            for index, item in enumerate(
                _array(payload["candidates"], f"{context}.candidates")
            )
        ),
    )


def _citation(value: object, context: str) -> CitationRef:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"evidence_id", "locator"}),
    )
    return CitationRef(
        _string(payload, "evidence_id", context),
        _locator(payload["locator"], f"{context}.locator"),
    )


def _owner(value: object, context: str) -> OwnerObservation:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"owner", "condition", "expected_schema_version"}),
        optional=frozenset({"observed_schema_version"}),
    )
    return OwnerObservation(
        _string(payload, "owner", context),
        EvaluationOwnerCondition(
            _enum(EvaluationOwnerCondition, payload, "condition", context)
        ),
        _integer(payload, "expected_schema_version", context),
        _optional_integer(payload, "observed_schema_version", context),
    )


def _claim(value: object, context: str) -> ClaimRef:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"claim_id", "topic", "value", "evidence_ids"}),
    )
    return ClaimRef(
        _string(payload, "claim_id", context),
        _string(payload, "topic", context),
        _string(payload, "value", context),
        _strings(payload["evidence_ids"], f"{context}.evidence_ids"),
    )


def _contradiction(value: object, context: str) -> ContradictionRef:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"left_claim_id", "right_claim_id", "relation"}),
    )
    return ContradictionRef(
        _string(payload, "left_claim_id", context),
        _string(payload, "right_claim_id", context),
        _string(payload, "relation", context),
    )


def _hop(value: object, context: str) -> RelationHop:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset(
            {"from_resource_id", "relation", "to_resource_id", "evidence_id"}
        ),
    )
    return RelationHop(
        _string(payload, "from_resource_id", context),
        _string(payload, "relation", context),
        _string(payload, "to_resource_id", context),
        _string(payload, "evidence_id", context),
    )


def _query_options(value: object, context: str) -> QueryOptions:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset({"source_kinds", "formats", "include_history"}),
        optional=frozenset({"project"}),
    )
    return QueryOptions(
        _strings(payload["source_kinds"], f"{context}.source_kinds"),
        _strings(payload["formats"], f"{context}.formats"),
        _optional_string(payload, "project", context),
        _boolean(payload, "include_history", context),
    )


def _limits(value: object, context: str) -> CaseLimits:
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset(
            {"limit", "max_per_resource", "min_section_distance", "max_vectors"}
        ),
    )
    return CaseLimits(
        _integer(payload, "limit", context),
        _integer(payload, "max_per_resource", context),
        _integer(payload, "min_section_distance", context),
        _integer(payload, "max_vectors", context),
    )


def _case(value: object, index: int) -> GoldenCase:
    context = f"cases[{index}]"
    payload = _mapping(value, context)
    _keys(
        payload,
        context=context,
        required=frozenset(
            {
                "scenario_id",
                "category",
                "query",
                "query_options",
                "limits",
                "required_plan_steps",
                "forbidden_plan_steps",
                "expected_outcome",
                "expected_abstain",
                "relevant_evidence",
                "expected_retrieved_ids",
                "expected_citations",
                "owner_conditions",
                "rankings",
                "claims",
                "contradictions",
                "relation_hops",
                "snapshot_transition",
                "expected_filtered_duplicates",
                "expected_excluded_revisions",
                "expected_omitted_by_limit",
                "expected_contradictions",
            }
        ),
    )
    transition_value = payload["snapshot_transition"]
    if transition_value is None:
        transition = None
    else:
        transition_payload = _mapping(
            transition_value, f"{context}.snapshot_transition"
        )
        _keys(
            transition_payload,
            context=f"{context}.snapshot_transition",
            required=frozenset({"changes_during_attempts"}),
        )
        transition = SnapshotTransition(
            _integer(
                transition_payload,
                "changes_during_attempts",
                f"{context}.snapshot_transition",
            )
        )
    return GoldenCase(
        scenario_id=_string(payload, "scenario_id", context),
        category=_string(payload, "category", context),
        query=_string(payload, "query", context),
        query_options=_query_options(
            payload["query_options"], f"{context}.query_options"
        ),
        limits=_limits(payload["limits"], f"{context}.limits"),
        required_plan_steps=_strings(
            payload["required_plan_steps"], f"{context}.required_plan_steps"
        ),
        forbidden_plan_steps=_strings(
            payload["forbidden_plan_steps"], f"{context}.forbidden_plan_steps"
        ),
        expected_outcome=EvaluationOutcome(
            _enum(EvaluationOutcome, payload, "expected_outcome", context)
        ),
        expected_abstain=_boolean(payload, "expected_abstain", context),
        relevant_evidence=tuple(
            _relevance(item, f"{context}.relevant_evidence[{position}]")
            for position, item in enumerate(
                _array(payload["relevant_evidence"], f"{context}.relevant_evidence")
            )
        ),
        expected_retrieved_ids=_strings(
            payload["expected_retrieved_ids"], f"{context}.expected_retrieved_ids"
        ),
        expected_citations=tuple(
            _citation(item, f"{context}.expected_citations[{position}]")
            for position, item in enumerate(
                _array(payload["expected_citations"], f"{context}.expected_citations")
            )
        ),
        owner_conditions=tuple(
            _owner(item, f"{context}.owner_conditions[{position}]")
            for position, item in enumerate(
                _array(payload["owner_conditions"], f"{context}.owner_conditions")
            )
        ),
        rankings=tuple(
            _ranking(item, f"{context}.rankings[{position}]")
            for position, item in enumerate(
                _array(payload["rankings"], f"{context}.rankings")
            )
        ),
        claims=tuple(
            _claim(item, f"{context}.claims[{position}]")
            for position, item in enumerate(
                _array(payload["claims"], f"{context}.claims")
            )
        ),
        contradictions=tuple(
            _contradiction(item, f"{context}.contradictions[{position}]")
            for position, item in enumerate(
                _array(payload["contradictions"], f"{context}.contradictions")
            )
        ),
        relation_hops=tuple(
            _hop(item, f"{context}.relation_hops[{position}]")
            for position, item in enumerate(
                _array(payload["relation_hops"], f"{context}.relation_hops")
            )
        ),
        snapshot_transition=transition,
        expected_filtered_duplicates=_integer(
            payload, "expected_filtered_duplicates", context
        ),
        expected_excluded_revisions=_integer(
            payload, "expected_excluded_revisions", context
        ),
        expected_omitted_by_limit=_integer(
            payload, "expected_omitted_by_limit", context
        ),
        expected_contradictions=_integer(payload, "expected_contradictions", context),
    )


def golden_suite_from_mapping(value: object) -> GoldenSuite:
    payload = _mapping(value, "golden_suite")
    _keys(
        payload,
        context="golden_suite",
        required=frozenset(
            {
                "schema_version",
                "suite_id",
                "description",
                "provenance",
                "limitations",
                "cases",
            }
        ),
    )
    return GoldenSuite(
        schema_version=_integer(payload, "schema_version", "golden_suite"),
        suite_id=_string(payload, "suite_id", "golden_suite"),
        description=_string(payload, "description", "golden_suite"),
        provenance=EvaluationProvenance(
            _enum(EvaluationProvenance, payload, "provenance", "golden_suite")
        ),
        limitations=_strings(payload["limitations"], "limitations"),
        cases=tuple(
            _case(item, index)
            for index, item in enumerate(_array(payload["cases"], "cases"))
        ),
    )


def load_golden_suite(path: str | Path) -> GoldenSuite:
    """Read at most four MiB of strict UTF-8 JSON."""

    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_GOLDEN_FIXTURE_BYTES + 1)
    if len(raw) > MAX_GOLDEN_FIXTURE_BYTES:
        raise ValueError(f"golden fixture exceeds {MAX_GOLDEN_FIXTURE_BYTES} bytes")
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("golden fixture must be valid UTF-8 JSON") from error
    return golden_suite_from_mapping(decoded)


# endregion [02]


# region [03] Live deterministic orchestration runner


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    evidence_id: str
    resource_id: str
    rank: int
    source_kind: str
    format: str
    revision_state: EvaluationRevisionState
    locator: EvidenceLocator
    disposition: EvaluationDisposition
    canonical_resource_id: str | None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "resource_id": self.resource_id,
            "rank": self.rank,
            "source_kind": self.source_kind,
            "format": self.format,
            "revision_state": self.revision_state.value,
            "locator": self.locator.to_dict(),
            "disposition": self.disposition.value,
        }
        if self.canonical_resource_id is not None:
            payload["canonical_resource_id"] = self.canonical_resource_id
        return payload


@dataclass(frozen=True, slots=True)
class ScenarioTelemetry:
    latency_milliseconds: int
    rows_scanned: int
    vectors_scanned: int
    context_characters: int

    def to_dict(self) -> dict[str, object]:
        return {
            "latency_milliseconds": self.latency_milliseconds,
            "rows_scanned": self.rows_scanned,
            "vectors_scanned": self.vectors_scanned,
            "context_characters": self.context_characters,
        }


@dataclass(frozen=True, slots=True)
class ScenarioObservation:
    scenario_id: str
    category: str
    plan_steps: tuple[str, ...]
    actual_outcome: EvaluationOutcome
    actual_abstained: bool
    retrieved_evidence: tuple[RetrievedEvidence, ...]
    produced_citations: tuple[CitationRef, ...]
    filtered_duplicates: int
    excluded_revisions: int
    omitted_by_limit: int
    contradictions: int
    relation_hops: tuple[RelationHop, ...]
    stale_retrieved: int
    duplicate_retrieved: int
    snapshot_changed: bool
    context_complete: str
    telemetry: ScenarioTelemetry
    acceptance_passed: bool
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        evidence_ids = [item.evidence_id for item in self.retrieved_evidence]
        citation_ids = [item.evidence_id for item in self.produced_citations]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("runner returned a repeated evidence_id")
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("runner returned repeated produced citations")
        _nonnegative_int("runner stale_retrieved", self.stale_retrieved)
        _nonnegative_int("runner duplicate_retrieved", self.duplicate_retrieved)

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "plan_steps": list(self.plan_steps),
            "actual_outcome": self.actual_outcome.value,
            "actual_abstained": self.actual_abstained,
            "retrieved_evidence": [item.to_dict() for item in self.retrieved_evidence],
            "produced_citations": [item.to_dict() for item in self.produced_citations],
            "filtered_duplicates": self.filtered_duplicates,
            "excluded_revisions": self.excluded_revisions,
            "omitted_by_limit": self.omitted_by_limit,
            "contradictions": self.contradictions,
            "relation_hops": [item.to_dict() for item in self.relation_hops],
            "stale_retrieved": self.stale_retrieved,
            "duplicate_retrieved": self.duplicate_retrieved,
            "snapshot_changed": self.snapshot_changed,
            "context_complete": self.context_complete,
            "telemetry": self.telemetry.to_dict(),
            "acceptance_passed": self.acceptance_passed,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class GoldenRun:
    suite: GoldenSuite
    observations: tuple[ScenarioObservation, ...]

    def __post_init__(self) -> None:
        if tuple(case.scenario_id for case in self.suite.cases) != tuple(
            observation.scenario_id for observation in self.observations
        ):
            raise ValueError("run observations do not align with golden cases")

    @property
    def gate_passed(self) -> bool:
        return all(item.acceptance_passed for item in self.observations)


def _candidate_lookup(case: GoldenCase) -> dict[str, CandidateEvidence]:
    result: dict[str, CandidateEvidence] = {}
    for ranking in case.rankings:
        for candidate in ranking.candidates:
            result.setdefault(candidate.evidence_id, candidate)
    return result


def _locator_from_evidence(value: object) -> EvidenceLocator:
    from .knowledge_contracts import EvidenceRef

    if not isinstance(value, EvidenceRef):
        raise TypeError("runner locator source must be EvidenceRef")
    if value.page is not None:
        return EvidenceLocator("page", str(value.page))
    if value.start_line is not None:
        return EvidenceLocator("lines", f"{value.start_line}-{value.end_line}")
    if value.start_ms is not None:
        return EvidenceLocator("timestamp_ms", f"{value.start_ms}-{value.end_ms}")
    if value.section_kind == "evaluation_locator_v1" and value.section_id is not None:
        try:
            payload: object = json.loads(value.section_id)
        except json.JSONDecodeError as error:
            raise ValueError(
                "runner evidence has an invalid encoded locator"
            ) from error
        return _locator(payload, "runner evidence locator")
    if value.section_kind is not None and value.section_id is not None:
        return EvidenceLocator(value.section_kind, value.section_id)
    raise ValueError("runner evidence does not expose a supported locator")


def _relation_hops_from_bundle(
    case: GoldenCase,
    bundle_value: object,
) -> tuple[RelationHop, ...]:
    from .knowledge_contracts import ContextBundle

    if not isinstance(bundle_value, ContextBundle):
        raise TypeError("runner relation source must be ContextBundle")
    entity_labels = {entity.entity_id: entity.label for entity in bundle_value.entities}
    observed: list[RelationHop] = []
    for expected in case.relation_hops:
        if any(
            entity_labels.get(relation.source_entity_id) == expected.from_resource_id
            and entity_labels.get(relation.target_entity_id) == expected.to_resource_id
            and relation.relation_kind == expected.relation
            and expected.evidence_id in relation.evidence_ids
            for relation in bundle_value.relations
        ):
            observed.append(expected)
    return tuple(observed)


def _snapshot(case: GoldenCase, *, salt: str = "stable") -> KnowledgeSnapshot:
    from .knowledge_contracts import (
        KnowledgeSnapshot,
        LogicalWatermark,
        OwnerAvailability,
        OwnerSnapshot,
    )

    state_map = {
        EvaluationOwnerCondition.AVAILABLE: OwnerAvailability.AVAILABLE,
        EvaluationOwnerCondition.ABSENT: OwnerAvailability.ABSENT,
        EvaluationOwnerCondition.FUTURE: OwnerAvailability.FUTURE,
    }
    owners = tuple(
        OwnerSnapshot(
            owner=item.owner,
            state=state_map[item.condition],
            expected_schema_version=item.expected_schema_version,
            observed_schema_version=item.observed_schema_version,
            watermarks=(LogicalWatermark("evaluation_runner", salt),),
        )
        for item in case.owner_conditions
    )
    return KnowledgeSnapshot.create(
        source_version="0.7.0",
        captured_at_utc="2026-07-26T04:00:00Z",
        captured_monotonic_ns=1,
        owners=owners,
    )


def _revision_state(value: EvaluationRevisionState) -> RevisionState:
    from .knowledge_contracts import RevisionState

    return {
        EvaluationRevisionState.CURRENT: RevisionState.CURRENT,
        EvaluationRevisionState.HISTORICAL: RevisionState.HISTORICAL,
        EvaluationRevisionState.SUPERSEDED: RevisionState.SUPERSEDED,
    }[value]


def _evaluation_disposition(value: object) -> EvaluationDisposition:
    from .knowledge_contracts import ResourceDisposition

    if value is None or value is ResourceDisposition.CANONICAL:
        return EvaluationDisposition.CANONICAL
    if value is ResourceDisposition.DUPLICATE:
        return EvaluationDisposition.DUPLICATE
    raise ValueError("runner retrieved an unsupported resource disposition")


def _candidate_contract(
    case: GoldenCase,
    ranking: CandidateRanking,
    candidate: CandidateEvidence,
) -> KnowledgeCandidate:
    from .knowledge_contracts import (
        EvidenceMethod,
        EvidenceRef,
        PhysicalIdentityRef,
        RankingSignal,
        ResourceDisposition,
        ResourceRef,
        RevisionRef,
    )
    from .knowledge_search import KnowledgeCandidate

    dispositions = {
        EvaluationDisposition.CANONICAL: ResourceDisposition.CANONICAL,
        EvaluationDisposition.DUPLICATE: ResourceDisposition.DUPLICATE,
    }
    resource = ResourceRef(
        resource_id=candidate.resource_id,
        source_kind=candidate.source_kind,
        owner=candidate.source_kind,
        physical_identity=PhysicalIdentityRef(
            "evaluation_fixture", candidate.resource_id, 1
        ),
        current_path=candidate.current_path,
        disposition=dispositions[candidate.disposition],
        canonical_resource_id=candidate.canonical_resource_id,
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=f"revision:evaluation:{candidate.resource_id}:{candidate.revision_state.value}",
        producer="knowledge-evaluation-runner",
        processing_signature="knowledge-evaluation-runner-v1",
        generation=None,
        state=_revision_state(candidate.revision_state),
    )
    page: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    section_kind: str | None = None
    section_id: str | None = None
    evidence_method = EvidenceMethod.EXTRACTED
    if candidate.locator.kind == "page":
        page = int(candidate.locator.value)
    elif candidate.locator.kind == "lines":
        start, end = candidate.locator.value.split("-", maxsplit=1)
        start_line = int(start)
        end_line = int(end)
    elif candidate.locator.kind == "timestamp_ms":
        start, end = candidate.locator.value.split("-", maxsplit=1)
        start_ms = int(start)
        end_ms = int(end)
    else:
        section_kind = "evaluation_locator_v1"
        section_id = json.dumps(
            candidate.locator.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    claim = next(
        (item for item in case.claims if item.claim_id == candidate.claim_id),
        None,
    )
    identifiers: list[tuple[str, str]] = []
    if claim is not None:
        identifiers.append((f"claim:{claim.topic}", claim.value))
    evidence = EvidenceRef(
        evidence_id=candidate.evidence_id,
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=evidence_method,
        page=page,
        start_line=start_line,
        end_line=end_line,
        start_ms=start_ms,
        end_ms=end_ms,
        section_kind=section_kind,
        section_id=section_id,
        snippet=candidate.snippet,
        extractor="knowledge-evaluation-runner",
        extractor_version="1",
        identifiers=tuple(identifiers),
    )
    return KnowledgeCandidate(
        resource=resource,
        revision=revision,
        evidence=evidence,
        signal=RankingSignal(
            ranking.name,
            "scripted_owner_score",
            candidate.raw_score,
            candidate.source_rank,
        ),
        reason=f"executable golden input:{ranking.channel}",
    )


def _knowledge_query(case: GoldenCase) -> KnowledgeQuery:
    from .knowledge_planner import KnowledgeQuery, RetrievalMode

    return KnowledgeQuery(
        text=case.query,
        retrieval_mode=RetrievalMode.EVIDENCE,
        include_history=case.query_options.include_history,
        source_kinds=case.query_options.source_kinds,
        formats=case.query_options.formats,
        project=case.query_options.project,
        limit=case.limits.limit,
        max_per_resource=case.limits.max_per_resource,
        min_section_distance=case.limits.min_section_distance,
        max_vectors=case.limits.max_vectors,
    )


def _service_snapshot_change(
    case: GoldenCase,
    query: KnowledgeQuery,
    plan: KnowledgePlan,
    result: KnowledgeSearchResult,
) -> KnowledgeSearchResult:
    from .knowledge_service import KnowledgeSearchService
    from .knowledge_snapshot import KnowledgeStatePaths

    variants = [_snapshot(case, salt=value) for value in ("a", "b", "c", "d")]

    def collector(
        paths: KnowledgeStatePaths,
        *,
        source_version: str,
        cancellation_check: Callable[[], None] | None = None,
    ) -> KnowledgeSnapshot:
        del paths, source_version, cancellation_check
        return variants.pop(0)

    def executor(
        paths: KnowledgeStatePaths,
        plan: KnowledgePlan,
        snapshot: KnowledgeSnapshot,
        *,
        cancellation_check: Callable[[], None] | None = None,
    ) -> KnowledgeSearchResult:
        del paths, plan, cancellation_check
        return replace(result, snapshot=snapshot)

    def planner(query: KnowledgeQuery) -> KnowledgePlan:
        del query
        return plan

    service = KnowledgeSearchService(
        paths=KnowledgeStatePaths.from_directory(Path("Z:/evaluation-do-not-open")),
        source_version="0.7.0",
        snapshot_collector=collector,
        query_planner=planner,
        search_executor=executor,
    )
    return service.search(query)


def _run_case(case: GoldenCase) -> ScenarioObservation:
    from .knowledge_context import build_context_bundle
    from .knowledge_contracts import SnapshotConsistency
    from .knowledge_planner import plan_knowledge_query
    from .knowledge_search import (
        KnowledgeSearchResult,
        RankingExecution,
        fuse_evidence_rankings,
    )

    started = time.perf_counter_ns()
    query = _knowledge_query(case)
    plan = plan_knowledge_query(query)
    plan_steps = tuple(dict.fromkeys(step.channel for step in plan.steps))
    scripted_rankings = {
        ranking.name: tuple(
            _candidate_contract(case, ranking, candidate)
            for candidate in ranking.candidates
        )
        for ranking in case.rankings
    }
    hits, omitted = fuse_evidence_rankings(
        scripted_rankings,
        limit=case.limits.limit,
        max_per_resource=case.limits.max_per_resource,
        min_section_distance=case.limits.min_section_distance,
        include_history=case.query_options.include_history,
    )
    unique_candidates = _candidate_lookup(case)
    filtered_duplicates = sum(
        item.disposition is EvaluationDisposition.DUPLICATE
        for item in unique_candidates.values()
    )
    excluded_revisions = sum(
        item.revision_state
        in {
            EvaluationRevisionState.HISTORICAL,
            EvaluationRevisionState.SUPERSEDED,
        }
        for item in unique_candidates.values()
        if not case.query_options.include_history
    )
    omitted_by_limit = max(0, omitted - filtered_duplicates - excluded_revisions)
    owner_available = all(
        item.condition is EvaluationOwnerCondition.AVAILABLE
        for item in case.owner_conditions
    )
    executions = tuple(
        RankingExecution(
            name=ranking.name,
            channel=ranking.channel,
            executed=True,
            available=owner_available,
            complete=owner_available
            and not (
                ranking.channel == "exact"
                and any(
                    candidate.source_kind == "inventory"
                    for candidate in ranking.candidates
                )
            ),
            returned=len(ranking.candidates),
            rows_scanned=len(ranking.candidates),
            vectors_scanned=(
                len(ranking.candidates) if ranking.channel == "semantic" else 0
            ),
        )
        for ranking in case.rankings
    )
    if not executions:
        executions = (
            RankingExecution(
                name="executable_empty_owner_result",
                channel="golden",
                executed=True,
                available=owner_available,
                complete=owner_available,
                returned=0,
            ),
        )
    search_result = KnowledgeSearchResult(
        plan=plan,
        snapshot=_snapshot(case),
        hits=hits,
        rankings=executions,
        complete=(
            owner_available
            and omitted_by_limit == 0
            and all(execution.complete for execution in executions)
        ),
        truncated=omitted_by_limit > 0,
        omitted_candidates=omitted_by_limit,
        rows_scanned=sum(len(item.candidates) for item in case.rankings),
        vectors_scanned=sum(
            len(item.candidates) for item in case.rankings if item.channel == "semantic"
        ),
        elapsed_milliseconds=0,
    )
    if case.snapshot_transition is not None and (
        case.snapshot_transition.changes_during_attempts == 2
    ):
        search_result = _service_snapshot_change(case, query, plan, search_result)
    bundle = build_context_bundle(
        search_result,
        character_limit=12_000,
        max_hits=min(100, max(1, case.limits.limit)),
    )
    lookup = _candidate_lookup(case)
    retrieved = tuple(
        RetrievedEvidence(
            evidence_id=hit.evidence.evidence_id,
            resource_id=hit.resource.resource_id,
            rank=hit.rank,
            source_kind=hit.resource.source_kind,
            format=lookup[hit.evidence.evidence_id].format,
            revision_state=EvaluationRevisionState(hit.revision.state.value),
            locator=_locator_from_evidence(hit.evidence),
            disposition=_evaluation_disposition(hit.resource.disposition),
            canonical_resource_id=hit.resource.canonical_resource_id,
        )
        for hit in search_result.hits
    )
    selected_hits = {hit.evidence.evidence_id: hit for hit in bundle.selected_hits}
    produced_citations = tuple(
        CitationRef(
            evidence_id=evidence_id,
            locator=_locator_from_evidence(selected_hits[evidence_id].evidence),
        )
        for _citation_id, evidence_id in bundle.citation_ids
    )
    relation_hops = _relation_hops_from_bundle(case, bundle)
    stale_retrieved = sum(
        item.revision_state is not EvaluationRevisionState.CURRENT for item in retrieved
    )
    duplicate_retrieved = sum(
        item.disposition is EvaluationDisposition.DUPLICATE for item in retrieved
    )
    snapshot_changed = (
        search_result.snapshot.consistency is SnapshotConsistency.SNAPSHOT_CHANGED
    )
    if snapshot_changed:
        outcome = EvaluationOutcome.SNAPSHOT_CHANGED
    elif any(
        item.condition is EvaluationOwnerCondition.FUTURE
        for item in case.owner_conditions
    ):
        outcome = EvaluationOutcome.SCHEMA_INCOMPATIBLE
    elif any(
        item.condition is EvaluationOwnerCondition.ABSENT
        for item in case.owner_conditions
    ):
        outcome = EvaluationOutcome.PARTIAL
    elif (
        omitted_by_limit
        or bundle.contradictions
        or (retrieved and not search_result.complete)
    ):
        outcome = EvaluationOutcome.PARTIAL
    elif not retrieved:
        outcome = EvaluationOutcome.NO_EVIDENCE
    else:
        outcome = EvaluationOutcome.SUCCESS
    actual_abstained = not retrieved
    retrieved_ids = tuple(item.evidence_id for item in retrieved)
    produced_by_id = {item.evidence_id: item for item in produced_citations}
    expected_citations_present = all(
        produced_by_id.get(item.evidence_id) == item for item in case.expected_citations
    )
    diagnostics: list[str] = []
    required_missing = sorted(set(case.required_plan_steps).difference(plan_steps))
    forbidden_present = sorted(set(case.forbidden_plan_steps).intersection(plan_steps))
    if required_missing:
        diagnostics.append(f"plan_missing:{','.join(required_missing)}")
    if forbidden_present:
        diagnostics.append(f"plan_forbidden:{','.join(forbidden_present)}")
    if retrieved_ids != case.expected_retrieved_ids:
        diagnostics.append("retrieved_evidence_mismatch")
    if not expected_citations_present:
        diagnostics.append("expected_citation_missing_or_wrong_locator")
    if outcome is not case.expected_outcome:
        diagnostics.append(f"outcome:{outcome.value}!={case.expected_outcome.value}")
    if actual_abstained != case.expected_abstain:
        diagnostics.append("abstention_mismatch")
    if relation_hops != case.relation_hops:
        diagnostics.append("relation_hops_mismatch")
    if stale_retrieved:
        diagnostics.append(f"stale_retrieved:{stale_retrieved}")
    if duplicate_retrieved:
        diagnostics.append(f"duplicate_retrieved:{duplicate_retrieved}")
    for name, actual, expected in (
        ("filtered_duplicates", filtered_duplicates, case.expected_filtered_duplicates),
        ("excluded_revisions", excluded_revisions, case.expected_excluded_revisions),
        ("omitted_by_limit", omitted_by_limit, case.expected_omitted_by_limit),
        ("contradictions", len(bundle.contradictions), case.expected_contradictions),
    ):
        if actual != expected:
            diagnostics.append(f"{name}:{actual}!={expected}")
    elapsed = max(0, (time.perf_counter_ns() - started) // 1_000_000)
    return ScenarioObservation(
        scenario_id=case.scenario_id,
        category=case.category,
        plan_steps=plan_steps,
        actual_outcome=outcome,
        actual_abstained=actual_abstained,
        retrieved_evidence=retrieved,
        produced_citations=produced_citations,
        filtered_duplicates=filtered_duplicates,
        excluded_revisions=excluded_revisions,
        omitted_by_limit=omitted_by_limit,
        contradictions=len(bundle.contradictions),
        relation_hops=relation_hops,
        stale_retrieved=stale_retrieved,
        duplicate_retrieved=duplicate_retrieved,
        snapshot_changed=snapshot_changed,
        context_complete=bundle.completeness.value,
        telemetry=ScenarioTelemetry(
            latency_milliseconds=int(elapsed),
            rows_scanned=search_result.rows_scanned,
            vectors_scanned=search_result.vectors_scanned,
            context_characters=len(bundle.rendered_context),
        ),
        acceptance_passed=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def run_golden_suite(
    suite: GoldenSuite,
    *,
    require_all_categories: bool = True,
) -> GoldenRun:
    """Execute planner, fusion, context and applicable service seams per case."""

    if require_all_categories and (
        len(suite.cases) != 14 or suite.covered_categories != REQUIRED_GOLDEN_CATEGORIES
    ):
        raise ValueError("golden suite must contain exactly the 14 required cases")
    return GoldenRun(suite, tuple(_run_case(case) for case in suite.cases))


# endregion [03]


# region [04] Metrics, diagnostics and canonical report


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    count: int
    total: int
    minimum: int | None
    maximum: int | None
    mean: float | None
    p50: int | None
    p95: int | None

    @classmethod
    def from_values(cls, values: Sequence[int]) -> DistributionSummary:
        if not values:
            return cls(0, 0, None, None, None, None, None)
        ordered = sorted(values)
        total = sum(ordered)
        return cls(
            len(ordered),
            total,
            ordered[0],
            ordered[-1],
            total / len(ordered),
            ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)],
            ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "total": self.total,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "p50": self.p50,
            "p95": self.p95,
        }


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    scenario_id: str
    category: str
    acceptance_passed: bool
    relevant_evidence: int
    retrieved_evidence: int
    retrieved_relevant: int
    recall_at_k: float | None
    reciprocal_rank: float | None
    ndcg_at_k: float | None
    evidence_coverage: float | None
    valid_citations: int
    evidence_valid_citations: int
    produced_citations: int
    citation_precision: float | None
    locator_precision: float | None
    stale_retrieved: int
    stale_candidates: int
    stale_rate: float | None
    duplicate_retrieved: int
    duplicate_candidates: int
    duplicate_rate: float | None
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, retrieved, candidates, rate in (
            (
                "stale",
                self.stale_retrieved,
                self.stale_candidates,
                self.stale_rate,
            ),
            (
                "duplicate",
                self.duplicate_retrieved,
                self.duplicate_candidates,
                self.duplicate_rate,
            ),
        ):
            _nonnegative_int(f"{label}_retrieved", retrieved)
            _nonnegative_int(f"{label}_candidates", candidates)
            if retrieved > candidates:
                raise ValueError(f"{label} retrieved exceeds candidate denominator")
            expected = None if candidates == 0 else retrieved / candidates
            if rate != expected:
                raise ValueError(f"{label} rate does not match its denominator")

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "acceptance_passed": self.acceptance_passed,
            "relevant_evidence": self.relevant_evidence,
            "retrieved_evidence": self.retrieved_evidence,
            "retrieved_relevant": self.retrieved_relevant,
            "recall_at_k": self.recall_at_k,
            "reciprocal_rank": self.reciprocal_rank,
            "ndcg_at_k": self.ndcg_at_k,
            "evidence_coverage": self.evidence_coverage,
            "valid_citations": self.valid_citations,
            "evidence_valid_citations": self.evidence_valid_citations,
            "produced_citations": self.produced_citations,
            "citation_precision": self.citation_precision,
            "locator_precision": self.locator_precision,
            "stale_retrieved": self.stale_retrieved,
            "stale_candidates": self.stale_candidates,
            "stale_rate": self.stale_rate,
            "duplicate_retrieved": self.duplicate_retrieved,
            "duplicate_candidates": self.duplicate_candidates,
            "duplicate_rate": self.duplicate_rate,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    evaluated_queries: int
    recall_at_k: float
    mean_reciprocal_rank: float
    ndcg_at_k: float
    evidence_coverage: float | None
    relevant_evidence: int
    covered_evidence: int

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluated_queries": self.evaluated_queries,
            "recall_at_k": self.recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "ndcg_at_k": self.ndcg_at_k,
            "evidence_coverage": self.evidence_coverage,
            "relevant_evidence": self.relevant_evidence,
            "covered_evidence": self.covered_evidence,
        }


@dataclass(frozen=True, slots=True)
class IntegrityMetrics:
    citation_precision: float | None
    locator_precision: float | None
    valid_citations: int
    evidence_valid_citations: int
    produced_citations: int
    expected_abstention_rate: float
    actual_abstention_rate: float
    abstention_accuracy: float
    outcome_accuracy: float
    stale_retrieved: int
    stale_candidates: int
    stale_rate: float | None
    duplicate_retrieved: int
    duplicate_candidates: int
    duplicate_rate: float | None

    def __post_init__(self) -> None:
        for label, retrieved, candidates, rate in (
            (
                "stale",
                self.stale_retrieved,
                self.stale_candidates,
                self.stale_rate,
            ),
            (
                "duplicate",
                self.duplicate_retrieved,
                self.duplicate_candidates,
                self.duplicate_rate,
            ),
        ):
            _nonnegative_int(f"integrity {label}_retrieved", retrieved)
            _nonnegative_int(f"integrity {label}_candidates", candidates)
            if retrieved > candidates:
                raise ValueError(f"integrity {label} retrieved exceeds denominator")
            expected = None if candidates == 0 else retrieved / candidates
            if rate != expected:
                raise ValueError(f"integrity {label} rate/denominator mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "citation_precision": self.citation_precision,
            "locator_precision": self.locator_precision,
            "valid_citations": self.valid_citations,
            "evidence_valid_citations": self.evidence_valid_citations,
            "produced_citations": self.produced_citations,
            "expected_abstention_rate": self.expected_abstention_rate,
            "actual_abstention_rate": self.actual_abstention_rate,
            "abstention_accuracy": self.abstention_accuracy,
            "outcome_accuracy": self.outcome_accuracy,
            "stale_retrieved": self.stale_retrieved,
            "stale_candidates": self.stale_candidates,
            "stale_rate": self.stale_rate,
            "duplicate_retrieved": self.duplicate_retrieved,
            "duplicate_candidates": self.duplicate_candidates,
            "duplicate_rate": self.duplicate_rate,
        }


@dataclass(frozen=True, slots=True)
class TelemetrySummary:
    latency_milliseconds: DistributionSummary
    rows_scanned: DistributionSummary
    vectors_scanned: DistributionSummary
    context_characters: DistributionSummary

    def to_dict(self) -> dict[str, object]:
        return {
            "latency_milliseconds": self.latency_milliseconds.to_dict(),
            "rows_scanned": self.rows_scanned.to_dict(),
            "vectors_scanned": self.vectors_scanned.to_dict(),
            "context_characters": self.context_characters.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    schema_version: int
    suite_id: str
    cutoff_k: int
    scenario_count: int
    scripted_fixture: bool
    limitations: tuple[str, ...]
    gate_passed: bool
    scenarios: tuple[ScenarioEvaluation, ...]
    observations: tuple[ScenarioObservation, ...]
    retrieval: RetrievalMetrics
    integrity: IntegrityMetrics
    telemetry: TelemetrySummary

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "knowledge_evaluation_report",
            "suite_id": self.suite_id,
            "cutoff_k": self.cutoff_k,
            "scenario_count": self.scenario_count,
            "scripted_fixture": self.scripted_fixture,
            "limitations": list(self.limitations),
            "gate_passed": self.gate_passed,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "observations": [item.to_dict() for item in self.observations],
            "retrieval": self.retrieval.to_dict(),
            "integrity": self.integrity.to_dict(),
            "telemetry": self.telemetry.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _evaluate_case(
    case: GoldenCase,
    observation: ScenarioObservation,
    cutoff_k: int,
) -> ScenarioEvaluation:
    gains = {item.evidence_id: item.gain for item in case.relevant_evidence}
    retrieved = {item.evidence_id: item for item in observation.retrieved_evidence}
    top = observation.retrieved_evidence[:cutoff_k]
    top_ids = {item.evidence_id for item in top}
    covered = len(set(gains).intersection(retrieved))
    recall: float | None
    reciprocal: float | None
    ndcg: float | None
    coverage: float | None
    if gains:
        recall = len(set(gains).intersection(top_ids)) / len(gains)
        first = next((item.rank for item in top if item.evidence_id in gains), None)
        reciprocal = 0.0 if first is None else 1.0 / first
        actual_dcg = math.fsum(
            (2 ** gains[item.evidence_id] - 1) / math.log2(item.rank + 1)
            for item in top
            if item.evidence_id in gains
        )
        ideal = sorted(gains.values(), reverse=True)[:cutoff_k]
        ideal_dcg = math.fsum(
            (2**gain - 1) / math.log2(rank + 1)
            for rank, gain in enumerate(ideal, start=1)
        )
        ndcg = actual_dcg / ideal_dcg
        if ndcg > 1.0 + 1e-12:
            raise ValueError("nDCG invariant violated by repeated evidence gain")
        ndcg = min(1.0, ndcg)
        coverage = covered / len(gains)
    else:
        recall = reciprocal = ndcg = coverage = None
    expected = {item.evidence_id: item for item in case.expected_citations}
    evidence_valid = 0
    valid = 0
    for citation in observation.produced_citations:
        target = expected.get(citation.evidence_id)
        hit = retrieved.get(citation.evidence_id)
        if target is None or hit is None:
            continue
        evidence_valid += 1
        if citation.locator == target.locator == hit.locator:
            valid += 1
    produced = len(observation.produced_citations)
    stale_candidates = observation.stale_retrieved + observation.excluded_revisions
    duplicate_candidates = (
        observation.duplicate_retrieved + observation.filtered_duplicates
    )
    diagnostics = list(observation.diagnostics)
    missing_relevant = sorted(set(gains).difference(retrieved))
    if missing_relevant:
        diagnostics.append(f"missing_relevant:{','.join(missing_relevant)}")
    if produced > valid:
        diagnostics.append(f"invalid_citations:{produced - valid}")
    if evidence_valid > valid:
        diagnostics.append(f"locator_mismatches:{evidence_valid - valid}")
    if observation.omitted_by_limit:
        diagnostics.append(f"omitted_by_limit:{observation.omitted_by_limit}")
    return ScenarioEvaluation(
        scenario_id=case.scenario_id,
        category=case.category,
        acceptance_passed=observation.acceptance_passed,
        relevant_evidence=len(gains),
        retrieved_evidence=len(retrieved),
        retrieved_relevant=covered,
        recall_at_k=recall,
        reciprocal_rank=reciprocal,
        ndcg_at_k=ndcg,
        evidence_coverage=coverage,
        valid_citations=valid,
        evidence_valid_citations=evidence_valid,
        produced_citations=produced,
        citation_precision=None if produced == 0 else valid / produced,
        locator_precision=None if evidence_valid == 0 else valid / evidence_valid,
        stale_retrieved=observation.stale_retrieved,
        stale_candidates=stale_candidates,
        stale_rate=(
            None
            if stale_candidates == 0
            else observation.stale_retrieved / stale_candidates
        ),
        duplicate_retrieved=observation.duplicate_retrieved,
        duplicate_candidates=duplicate_candidates,
        duplicate_rate=(
            None
            if duplicate_candidates == 0
            else observation.duplicate_retrieved / duplicate_candidates
        ),
        diagnostics=tuple(diagnostics),
    )


def evaluate_golden_suite(
    suite: GoldenSuite,
    *,
    cutoff_k: int = 10,
    require_all_categories: bool = True,
) -> EvaluationReport:
    """Execute the golden suite, then compute reproducible quality metrics."""

    if (
        not isinstance(cutoff_k, int)
        or isinstance(cutoff_k, bool)
        or not 1 <= cutoff_k <= 1_000
    ):
        raise ValueError("cutoff_k must be an integer from 1 to 1000")
    run = run_golden_suite(suite, require_all_categories=require_all_categories)
    scenario_results = tuple(
        _evaluate_case(case, observation, cutoff_k)
        for case, observation in zip(suite.cases, run.observations, strict=True)
    )
    ranked = tuple(item for item in scenario_results if item.recall_at_k is not None)
    relevant_total = sum(item.relevant_evidence for item in ranked)
    covered_total = sum(item.retrieved_relevant for item in ranked)
    ranked_count = len(ranked)
    retrieval = RetrievalMetrics(
        evaluated_queries=ranked_count,
        recall_at_k=(
            0.0
            if ranked_count == 0
            else math.fsum(item.recall_at_k or 0.0 for item in ranked) / ranked_count
        ),
        mean_reciprocal_rank=(
            0.0
            if ranked_count == 0
            else math.fsum(item.reciprocal_rank or 0.0 for item in ranked)
            / ranked_count
        ),
        ndcg_at_k=(
            0.0
            if ranked_count == 0
            else math.fsum(item.ndcg_at_k or 0.0 for item in ranked) / ranked_count
        ),
        evidence_coverage=(
            None if relevant_total == 0 else covered_total / relevant_total
        ),
        relevant_evidence=relevant_total,
        covered_evidence=covered_total,
    )
    produced = sum(item.produced_citations for item in scenario_results)
    evidence_valid = sum(item.evidence_valid_citations for item in scenario_results)
    valid = sum(item.valid_citations for item in scenario_results)
    expected_abstentions = sum(case.expected_abstain for case in suite.cases)
    actual_abstentions = sum(item.actual_abstained for item in run.observations)
    abstention_correct = sum(
        case.expected_abstain == item.actual_abstained
        for case, item in zip(suite.cases, run.observations, strict=True)
    )
    outcome_correct = sum(
        case.expected_outcome is item.actual_outcome
        for case, item in zip(suite.cases, run.observations, strict=True)
    )
    stale_retrieved = sum(item.stale_retrieved for item in run.observations)
    stale_candidates = stale_retrieved + sum(
        item.excluded_revisions for item in run.observations
    )
    duplicate_retrieved = sum(item.duplicate_retrieved for item in run.observations)
    duplicate_candidates = duplicate_retrieved + sum(
        item.filtered_duplicates for item in run.observations
    )
    count = len(suite.cases)
    integrity = IntegrityMetrics(
        citation_precision=None if produced == 0 else valid / produced,
        locator_precision=None if evidence_valid == 0 else valid / evidence_valid,
        valid_citations=valid,
        evidence_valid_citations=evidence_valid,
        produced_citations=produced,
        expected_abstention_rate=expected_abstentions / count,
        actual_abstention_rate=actual_abstentions / count,
        abstention_accuracy=abstention_correct / count,
        outcome_accuracy=outcome_correct / count,
        stale_retrieved=stale_retrieved,
        stale_candidates=stale_candidates,
        stale_rate=(
            None if stale_candidates == 0 else stale_retrieved / stale_candidates
        ),
        duplicate_retrieved=duplicate_retrieved,
        duplicate_candidates=duplicate_candidates,
        duplicate_rate=(
            None
            if duplicate_candidates == 0
            else duplicate_retrieved / duplicate_candidates
        ),
    )
    telemetry = TelemetrySummary(
        DistributionSummary.from_values(
            [item.telemetry.latency_milliseconds for item in run.observations]
        ),
        DistributionSummary.from_values(
            [item.telemetry.rows_scanned for item in run.observations]
        ),
        DistributionSummary.from_values(
            [item.telemetry.vectors_scanned for item in run.observations]
        ),
        DistributionSummary.from_values(
            [item.telemetry.context_characters for item in run.observations]
        ),
    )
    return EvaluationReport(
        schema_version=KNOWLEDGE_EVALUATION_SCHEMA_VERSION,
        suite_id=suite.suite_id,
        cutoff_k=cutoff_k,
        scenario_count=count,
        scripted_fixture=suite.scripted_fixture,
        limitations=suite.limitations,
        gate_passed=run.gate_passed,
        scenarios=scenario_results,
        observations=run.observations,
        retrieval=retrieval,
        integrity=integrity,
        telemetry=telemetry,
    )


# endregion [04]
