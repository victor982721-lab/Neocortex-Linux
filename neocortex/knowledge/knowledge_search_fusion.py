"""Evidence-key RRF fusion and post-retrieval diversity."""
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_search_fusion.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .knowledge_contracts import (
    EvidenceRef,
    KnowledgeHit,
    RankingSignal,
    ResourceDisposition,
    RevisionState,
)
from .knowledge_search_contracts import KnowledgeCandidate, ResourceDiscoverySignal
# endregion [01]

# region [02] Implementación


_EvidenceKey = tuple[str, str, str]
_OverlapCheck = Callable[[EvidenceRef, EvidenceRef, int], bool]


@dataclass(slots=True)
class _EvidenceAggregate:
    candidate: KnowledgeCandidate
    contributions: list[float] = field(default_factory=list)
    signals: list[RankingSignal] = field(default_factory=list)
    reasons: set[str] = field(default_factory=set)
    warnings: set[str] = field(default_factory=set)


def _validate_options(
    *,
    limit: int,
    max_per_resource: int,
    min_section_distance: int,
) -> None:
    if not 1 <= limit <= 1_000:
        raise ValueError("Knowledge fusion limit must be between 1 and 1000")
    if not 1 <= max_per_resource <= 100:
        raise ValueError("max_per_resource must be between 1 and 100")
    if not 0 <= min_section_distance <= 1_000_000:
        raise ValueError("min_section_distance is outside its supported bound")


def _checkpoint(cancellation_check: Callable[[], None] | None) -> None:
    if cancellation_check is not None:
        cancellation_check()


def _periodic_checkpoint(
    cancellation_check: Callable[[], None] | None,
    position: int,
) -> None:
    if cancellation_check is not None and position % 128 == 0:
        cancellation_check()


def _candidate_is_visible(
    candidate: KnowledgeCandidate,
    *,
    include_history: bool,
) -> bool:
    if candidate.resource.disposition is ResourceDisposition.DUPLICATE:
        return False
    if include_history:
        return True
    return not (
        candidate.resource.disposition is ResourceDisposition.SUPERSEDED
        or candidate.revision.state
        in {RevisionState.HISTORICAL, RevisionState.SUPERSEDED}
    )


def _canonical_evidence_key(
    candidate: KnowledgeCandidate,
    snippet_aliases: dict[_EvidenceKey, _EvidenceKey],
) -> _EvidenceKey:
    evidence = candidate.evidence
    key = candidate.evidence_key
    if (
        not evidence.snippet
        or evidence.section_kind is None
        or evidence.section_id is None
    ):
        return key
    normalized_snippet = " ".join(evidence.snippet.split()).casefold()
    if not normalized_snippet:
        return key
    locator = "|".join(
        str(value)
        for value in (
            evidence.section_kind,
            evidence.section_id,
            evidence.page,
            evidence.start_line,
            evidence.end_line,
            evidence.start_ms,
            evidence.end_ms,
            evidence.start_char,
            evidence.end_char,
        )
    )
    alias_key = (
        candidate.resource.resource_id,
        candidate.revision.revision_id,
        f"{locator}|{normalized_snippet}",
    )
    return snippet_aliases.setdefault(alias_key, key)


def _record_candidate(
    aggregates: dict[_EvidenceKey, _EvidenceAggregate],
    key: _EvidenceKey,
    candidate: KnowledgeCandidate,
    *,
    ranking_name: str,
    rrf_k: float,
) -> None:
    aggregate = aggregates.get(key)
    if aggregate is None:
        aggregate = _EvidenceAggregate(candidate)
        aggregates[key] = aggregate
    elif (
        aggregate.candidate.resource != candidate.resource
        or aggregate.candidate.revision != candidate.revision
    ):
        raise ValueError("one evidence identity resolved to incompatible records")
    source_rank = candidate.signal.source_rank
    contribution = 1.0 / (rrf_k + source_rank)
    aggregate.contributions.append(contribution)
    aggregate.signals.append(
        replace(
            candidate.signal,
            source=ranking_name,
            source_rank=source_rank,
            contribution=contribution,
        )
    )
    aggregate.reasons.add(candidate.reason)
    aggregate.warnings.update(candidate.warnings)


def _collect_aggregates(
    rankings: Mapping[str, Sequence[KnowledgeCandidate]],
    *,
    include_history: bool,
    cancellation_check: Callable[[], None] | None,
    rrf_k: float,
) -> dict[_EvidenceKey, _EvidenceAggregate]:
    aggregates: dict[_EvidenceKey, _EvidenceAggregate] = {}
    snippet_aliases: dict[_EvidenceKey, _EvidenceKey] = {}
    for ranking_name in sorted(rankings):
        _checkpoint(cancellation_check)
        if not ranking_name.strip():
            raise ValueError("Knowledge ranking names cannot be blank")
        seen: set[_EvidenceKey] = set()
        for position, candidate in enumerate(rankings[ranking_name], 1):
            _periodic_checkpoint(cancellation_check, position)
            if not _candidate_is_visible(candidate, include_history=include_history):
                continue
            key = _canonical_evidence_key(candidate, snippet_aliases)
            if key in seen:
                continue
            seen.add(key)
            _record_candidate(
                aggregates,
                key,
                candidate,
                ranking_name=ranking_name,
                rrf_k=rrf_k,
            )
    return aggregates


def _apply_discovery_signals(
    aggregates: dict[_EvidenceKey, _EvidenceAggregate],
    discovery_signals: Sequence[ResourceDiscoverySignal],
    *,
    cancellation_check: Callable[[], None] | None,
    rrf_k: float,
) -> None:
    """Boost one already-grounded evidence aggregate per resource revision."""

    by_resource_revision: dict[tuple[str, str], list[_EvidenceKey]] = {}
    for key, aggregate in aggregates.items():
        candidate = aggregate.candidate
        by_resource_revision.setdefault(
            (candidate.resource.resource_id, candidate.revision.revision_id),
            [],
        ).append(key)
    seen: set[tuple[str, str, str]] = set()
    ordered = sorted(
        discovery_signals,
        key=lambda value: (
            value.signal.source,
            value.signal.source_rank,
            value.resource.resource_id,
            value.revision.revision_id,
        ),
    )
    for position, discovery in enumerate(ordered, 1):
        _periodic_checkpoint(cancellation_check, position)
        resource_id, revision_id = discovery.resource_revision_key
        identity = (discovery.signal.source, resource_id, revision_id)
        if identity in seen:
            continue
        seen.add(identity)
        eligible = by_resource_revision.get((resource_id, revision_id), ())
        if not eligible:
            continue
        best_key = min(
            eligible,
            key=lambda key: (
                -math.fsum(sorted(aggregates[key].contributions)),
                key,
            ),
        )
        aggregate = aggregates[best_key]
        contribution = discovery.fusion_weight / (rrf_k + discovery.signal.source_rank)
        aggregate.contributions.append(contribution)
        aggregate.signals.append(replace(discovery.signal, contribution=contribution))
        aggregate.reasons.add(discovery.reason)
        aggregate.warnings.update(discovery.warnings)


def overlaps_or_too_close(
    selected: EvidenceRef,
    candidate: EvidenceRef,
    minimum_distance: int,
) -> bool:
    if (
        selected.section_kind != candidate.section_kind
        or selected.section_id != candidate.section_id
    ):
        return False
    if selected.start_char is None or candidate.start_char is None:
        return True
    assert selected.end_char is not None
    assert candidate.end_char is not None
    if (
        candidate.start_char < selected.end_char
        and selected.start_char < candidate.end_char
    ):
        return True
    gap = max(candidate.start_char, selected.start_char) - min(
        candidate.end_char,
        selected.end_char,
    )
    return gap < minimum_distance


def _ordered_keys(
    aggregates: Mapping[_EvidenceKey, _EvidenceAggregate],
) -> list[_EvidenceKey]:
    return sorted(
        aggregates,
        key=lambda key: (
            -math.fsum(sorted(aggregates[key].contributions)),
            key,
        ),
    )


def _find_overlap_index(
    prior: Sequence[int],
    selected: Sequence[KnowledgeHit],
    candidate: KnowledgeCandidate,
    *,
    min_section_distance: int,
    overlap_check: _OverlapCheck,
) -> int | None:
    for index in prior:
        if overlap_check(
            selected[index].evidence,
            candidate.evidence,
            min_section_distance,
        ):
            return index
    return None


def _merged_overlap_hit(
    existing_hit: KnowledgeHit,
    aggregate: _EvidenceAggregate,
) -> KnowledgeHit:
    best_signals: dict[str, RankingSignal] = {
        signal.source: signal for signal in existing_hit.signals
    }
    for signal in aggregate.signals:
        current_signal = best_signals.get(signal.source)
        if current_signal is None or signal.source_rank < current_signal.source_rank:
            best_signals[signal.source] = signal
    merged_signals = tuple(
        sorted(
            best_signals.values(),
            key=lambda item: (item.source, item.source_rank),
        )
    )
    return replace(
        existing_hit,
        signals=merged_signals,
        fused_score=math.fsum(
            signal.contribution if signal.contribution is not None else 0.0
            for signal in merged_signals
        ),
        reasons=tuple(sorted({*existing_hit.reasons, *aggregate.reasons})),
        warnings=tuple(
            sorted(
                {
                    *existing_hit.warnings,
                    *aggregate.warnings,
                    "overlapping_evidence_merged",
                }
            )
        ),
    )


def _new_hit(aggregate: _EvidenceAggregate, rank: int) -> KnowledgeHit:
    candidate = aggregate.candidate
    return KnowledgeHit(
        rank=rank,
        resource=candidate.resource,
        revision=candidate.revision,
        evidence=candidate.evidence,
        signals=tuple(
            sorted(
                aggregate.signals,
                key=lambda item: (item.source, item.source_rank),
            )
        ),
        fused_score=math.fsum(sorted(aggregate.contributions)),
        reasons=tuple(sorted(aggregate.reasons)),
        confidence=candidate.confidence,
        warnings=tuple(sorted(aggregate.warnings)),
    )


def _cluster_evidence(
    aggregates: Mapping[_EvidenceKey, _EvidenceAggregate],
    *,
    min_section_distance: int,
    cancellation_check: Callable[[], None] | None,
    overlap_check: _OverlapCheck,
) -> list[KnowledgeHit]:
    selected: list[KnowledgeHit] = []
    selected_by_resource: dict[str, list[int]] = {}
    for position, key in enumerate(_ordered_keys(aggregates), 1):
        _periodic_checkpoint(cancellation_check, position)
        aggregate = aggregates[key]
        candidate = aggregate.candidate
        prior = selected_by_resource.setdefault(candidate.resource.resource_id, [])
        overlap_index = _find_overlap_index(
            prior,
            selected,
            candidate,
            min_section_distance=min_section_distance,
            overlap_check=overlap_check,
        )
        if overlap_index is not None:
            selected[overlap_index] = _merged_overlap_hit(
                selected[overlap_index],
                aggregate,
            )
            continue
        prior.append(len(selected))
        selected.append(_new_hit(aggregate, len(selected) + 1))
    return selected


def _apply_diversity(
    selected: Sequence[KnowledgeHit],
    *,
    limit: int,
    max_per_resource: int,
) -> tuple[tuple[KnowledgeHit, ...], int]:
    clustered = sorted(
        selected,
        key=lambda hit: (
            -hit.fused_score,
            hit.resource.resource_id,
            hit.revision.revision_id,
            hit.evidence.evidence_id,
        ),
    )
    diverse: list[KnowledgeHit] = []
    accepted_per_resource: dict[str, int] = {}
    omitted = 0
    for hit in clustered:
        resource_id = hit.resource.resource_id
        if accepted_per_resource.get(resource_id, 0) >= max_per_resource:
            omitted += 1
            continue
        if len(diverse) >= limit:
            omitted += 1
            continue
        accepted_per_resource[resource_id] = (
            accepted_per_resource.get(resource_id, 0) + 1
        )
        diverse.append(replace(hit, rank=len(diverse) + 1))
    return tuple(diverse), omitted


def fuse_evidence_rankings(
    rankings: Mapping[str, Sequence[KnowledgeCandidate]],
    *,
    discovery_signals: Sequence[ResourceDiscoverySignal] = (),
    limit: int,
    max_per_resource: int,
    min_section_distance: int,
    include_history: bool = False,
    cancellation_check: Callable[[], None] | None = None,
    rrf_k: float,
    overlap_check: _OverlapCheck,
) -> tuple[tuple[KnowledgeHit, ...], int]:
    """Fuse independent ranks by evidence, then apply bounded diversity."""

    _validate_options(
        limit=limit,
        max_per_resource=max_per_resource,
        min_section_distance=min_section_distance,
    )
    aggregates = _collect_aggregates(
        rankings,
        include_history=include_history,
        cancellation_check=cancellation_check,
        rrf_k=rrf_k,
    )
    _apply_discovery_signals(
        aggregates,
        discovery_signals,
        cancellation_check=cancellation_check,
        rrf_k=rrf_k,
    )
    selected = _cluster_evidence(
        aggregates,
        min_section_distance=min_section_distance,
        cancellation_check=cancellation_check,
        overlap_check=overlap_check,
    )
    return _apply_diversity(
        selected,
        limit=limit,
        max_per_resource=max_per_resource,
    )


__all__ = ("fuse_evidence_rankings", "overlaps_or_too_close")
# endregion [02]
