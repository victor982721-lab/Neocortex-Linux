"""Bounded causal health query for one published Knowledge resource."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from collections.abc import Callable
from pathlib import Path

from .knowledge_asset_health_contracts import (
    KnowledgeAssetFactSnapshot,
    KnowledgeAssetHealthCompleteness,
    KnowledgeAssetHealthFact,
    KnowledgeAssetHealthQuery,
    KnowledgeAssetHealthReport,
    KnowledgeAssetHealthStage,
    KnowledgeAssetHealthState,
)
from .knowledge_asset_health_repository import capture_knowledge_asset_fact_snapshot
from .knowledge_contracts import KnowledgeSnapshot, SnapshotConsistency
from .knowledge_snapshot import (
    KnowledgeStatePaths,
    KnowledgeStateRootError,
    collect_knowledge_snapshot,
)
from _04_Nucleo_Operativo.sqlite_immutable import ImmutableSQLiteUnavailable, capture_sqlite_immutable_fence
from _04_Nucleo_Operativo.state_topology_contracts import STATE_STORE_REGISTRY


KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION = "knowledge-asset-health-v1"

_ABSTAINING_GAP_MARKERS = (
    "_owner_corrupt",
    "_owner_future",
    "_owner_incompatible",
    "_owner_not_quiescent",
    "_owner_read_failed",
    "_record_invalid",
    "_schema_invalid",
    "_schema_version_absent",
    "_future_schema",
    "_incompatible_schema",
)
_PROTECTED_STATUSES = frozenset({"protected"})
_FAILED_STATUSES = frozenset({"error", "failed"})
_DEGRADED_STATUSES = frozenset({"degraded", "partial"})
_COMPLETE_SOURCE_STATUSES = frozenset({"complete", "done"})


class _RequiredOwnerNotQuiescent(RuntimeError):
    def __init__(self, owner: str) -> None:
        self.owner = owner
        super().__init__(owner)


def _snapshot_owner_paths(paths: KnowledgeStatePaths) -> tuple[tuple[str, Path], ...]:
    selected: list[tuple[str, Path]] = []
    for store in STATE_STORE_REGISTRY.stores:
        path = getattr(paths, store.knowledge_path_attribute)
        if path is not None:
            selected.append((store.state_owner_id, path))
    return tuple(selected)


def _require_quiescent_snapshot_owners(paths: KnowledgeStatePaths) -> None:
    """Reject an active owner before the general snapshot reader can touch SHM."""

    for owner, path in _snapshot_owner_paths(paths):
        try:
            before = capture_sqlite_immutable_fence(path)
            after = capture_sqlite_immutable_fence(path)
        except FileNotFoundError:
            continue
        except ImmutableSQLiteUnavailable as exc:
            raise _RequiredOwnerNotQuiescent(owner) from exc
        if before != after:
            raise _RequiredOwnerNotQuiescent(owner)


def _fact_by_stage(
    fact_snapshot: KnowledgeAssetFactSnapshot,
    stage: KnowledgeAssetHealthStage,
) -> KnowledgeAssetHealthFact | None:
    return next((fact for fact in fact_snapshot.facts if fact.stage is stage), None)


def _fact_values(fact: KnowledgeAssetHealthFact) -> dict[str, str]:
    return {value.name: value.value for value in fact.values}


def _classify_pdf_trace(
    source: KnowledgeAssetHealthFact,
    catalog: KnowledgeAssetHealthFact | None,
    search: KnowledgeAssetHealthFact | None,
) -> tuple[KnowledgeAssetHealthState, KnowledgeAssetHealthCompleteness, str]:
    status = source.status
    if status == "protected":
        return (
            KnowledgeAssetHealthState.PROTECTED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_source_status_protected",
        )
    if status == "error":
        return (
            KnowledgeAssetHealthState.FAILED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pipeline_status_failed",
        )
    if status == "processing":
        return (
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "pdf_processing_incomplete",
        )
    if status not in {"done", "partial"}:
        return (
            KnowledgeAssetHealthState.UNKNOWN,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "typed_status_unrecognized",
        )
    if catalog is None or search is None:
        return (
            KnowledgeAssetHealthState.UNKNOWN,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "causal_trace_incomplete",
        )
    if catalog.status in _FAILED_STATUSES:
        return (
            KnowledgeAssetHealthState.FAILED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pipeline_status_failed",
        )
    if status == "partial":
        return (
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pdf_status_partial",
        )
    if _fact_values(source).get("is_partial") == "true":
        return (
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pdf_bounded_range",
        )
    if catalog.status == "classified" and search.status == "eligible":
        return (
            KnowledgeAssetHealthState.HEALTHY,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "causal_trace_aligned",
        )
    return (
        KnowledgeAssetHealthState.UNKNOWN,
        KnowledgeAssetHealthCompleteness.PARTIAL,
        "typed_status_unrecognized",
    )


def _classify_stable_fact_snapshot(
    fact_snapshot: KnowledgeAssetFactSnapshot,
) -> tuple[KnowledgeAssetHealthState, KnowledgeAssetHealthCompleteness, str]:
    if "source_owner_identity_ambiguous" in fact_snapshot.counterevidence:
        return (
            KnowledgeAssetHealthState.UNKNOWN,
            KnowledgeAssetHealthCompleteness.ABSTAINED,
            "source_owner_identity_ambiguous",
        )
    if fact_snapshot.gaps:
        abstained = any(
            marker in gap for gap in fact_snapshot.gaps for marker in _ABSTAINING_GAP_MARKERS
        )
        if abstained:
            completeness = KnowledgeAssetHealthCompleteness.ABSTAINED
            reason = "required_owner_evidence_unavailable"
        elif fact_snapshot.facts:
            completeness = KnowledgeAssetHealthCompleteness.PARTIAL
            reason = "causal_trace_incomplete"
        else:
            completeness = KnowledgeAssetHealthCompleteness.NO_EVIDENCE
            reason = "published_inventory_evidence_absent"
        return KnowledgeAssetHealthState.UNKNOWN, completeness, reason

    if fact_snapshot.counterevidence:
        return (
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "causal_trace_mismatch",
        )

    inventory = _fact_by_stage(fact_snapshot, KnowledgeAssetHealthStage.INVENTORY)
    source = _fact_by_stage(fact_snapshot, KnowledgeAssetHealthStage.SOURCE_OWNER)
    catalog = _fact_by_stage(fact_snapshot, KnowledgeAssetHealthStage.CATALOG)
    search = _fact_by_stage(fact_snapshot, KnowledgeAssetHealthStage.KNOWLEDGE_SEARCH)
    if source is not None and source.owner == "pdf":
        return _classify_pdf_trace(source, catalog, search)
    if None in {inventory, source, catalog, search}:
        return (
            KnowledgeAssetHealthState.UNKNOWN,
            KnowledgeAssetHealthCompleteness.PARTIAL,
            "causal_trace_incomplete",
        )
    assert source is not None and catalog is not None and search is not None

    source_values = _fact_values(source)
    source_status = source.status
    catalog_status = catalog.status
    search_status = search.status
    if source_status in _PROTECTED_STATUSES:
        return (
            KnowledgeAssetHealthState.PROTECTED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_source_status_protected",
        )
    if source_status in _FAILED_STATUSES or catalog_status in _FAILED_STATUSES:
        return (
            KnowledgeAssetHealthState.FAILED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pipeline_status_failed",
        )
    if (
        source_status in _DEGRADED_STATUSES
        or catalog_status == "review"
        or source_values.get("text_truncated") == "true"
        or search_status == "excluded"
    ):
        return (
            KnowledgeAssetHealthState.DEGRADED,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "typed_pipeline_status_degraded",
        )
    if (
        source_status in _COMPLETE_SOURCE_STATUSES
        and catalog_status == "classified"
        and search_status == "eligible"
    ):
        return (
            KnowledgeAssetHealthState.HEALTHY,
            KnowledgeAssetHealthCompleteness.COMPLETE,
            "causal_trace_aligned",
        )
    return (
        KnowledgeAssetHealthState.UNKNOWN,
        KnowledgeAssetHealthCompleteness.PARTIAL,
        "typed_status_unrecognized",
    )


def _report_from_fact_snapshot(
    fact_snapshot: KnowledgeAssetFactSnapshot,
    *,
    attempts: int,
) -> KnowledgeAssetHealthReport:
    health, completeness, reason = _classify_stable_fact_snapshot(fact_snapshot)
    return KnowledgeAssetHealthReport(
        resource_id=fact_snapshot.resource_id,
        health=health,
        completeness=completeness,
        reason_code=reason,
        knowledge_snapshot_id=fact_snapshot.knowledge_snapshot_id,
        fact_snapshot_id=fact_snapshot.fact_snapshot_id,
        snapshot_consistency="stable",
        attempts=attempts,
        facts=fact_snapshot.facts,
        gaps=fact_snapshot.gaps,
        counterevidence=fact_snapshot.counterevidence,
        examples=fact_snapshot.examples,
        examples_truncated=fact_snapshot.examples_truncated,
    )


def _snapshot_changed_report(
    fact_snapshot: KnowledgeAssetFactSnapshot,
    *,
    knowledge_changed: bool,
    facts_changed: bool,
) -> KnowledgeAssetHealthReport:
    counterevidence = set(fact_snapshot.counterevidence)
    if knowledge_changed:
        counterevidence.add("knowledge_snapshot_changed")
    if facts_changed:
        counterevidence.add("fact_snapshot_changed")
    fenced = KnowledgeAssetFactSnapshot.create(
        resource_id=fact_snapshot.resource_id,
        knowledge_snapshot_id=fact_snapshot.knowledge_snapshot_id,
        facts=fact_snapshot.facts,
        gaps=fact_snapshot.gaps,
        counterevidence=tuple(sorted(counterevidence)),
        examples=fact_snapshot.examples,
        examples_truncated=fact_snapshot.examples_truncated,
    )
    return KnowledgeAssetHealthReport(
        resource_id=fenced.resource_id,
        health=KnowledgeAssetHealthState.UNKNOWN,
        completeness=KnowledgeAssetHealthCompleteness.ABSTAINED,
        reason_code="snapshot_changed",
        knowledge_snapshot_id=fenced.knowledge_snapshot_id,
        fact_snapshot_id=fenced.fact_snapshot_id,
        snapshot_consistency="snapshot_changed",
        attempts=2,
        facts=fenced.facts,
        gaps=fenced.gaps,
        counterevidence=fenced.counterevidence,
        examples=fenced.examples,
        examples_truncated=fenced.examples_truncated,
    )


def _unavailable_report(
    query: KnowledgeAssetHealthQuery,
    *,
    attempts: int,
    reason_code: str = "knowledge_snapshot_unavailable",
    gap: str = "knowledge_snapshot_unavailable",
) -> KnowledgeAssetHealthReport:
    return KnowledgeAssetHealthReport(
        resource_id=query.resource_id,
        health=KnowledgeAssetHealthState.UNKNOWN,
        completeness=KnowledgeAssetHealthCompleteness.ABSTAINED,
        reason_code=reason_code,
        knowledge_snapshot_id=None,
        fact_snapshot_id=None,
        snapshot_consistency="unavailable",
        attempts=attempts,
        gaps=(gap,),
    )


def _capture(
    paths: KnowledgeStatePaths,
    query: KnowledgeAssetHealthQuery,
    *,
    source_version: str,
) -> tuple[KnowledgeSnapshot, KnowledgeAssetFactSnapshot]:
    _require_quiescent_snapshot_owners(paths)
    snapshot = collect_knowledge_snapshot(
        paths,
        source_version=source_version,
        _immutable_owners=True,
    )
    facts = capture_knowledge_asset_fact_snapshot(paths, query.identity, snapshot)
    return snapshot, facts


def inspect_knowledge_asset_health(
    paths: KnowledgeStatePaths,
    query: KnowledgeAssetHealthQuery,
    *,
    source_version: str = KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION,
    _between_observations: Callable[[int], None] | None = None,
) -> KnowledgeAssetHealthReport:
    """Return one deterministic causal report, retrying a changed view once."""

    if not isinstance(paths, KnowledgeStatePaths):
        raise TypeError("paths must be KnowledgeStatePaths")
    if not isinstance(query, KnowledgeAssetHealthQuery):
        raise TypeError("query must be KnowledgeAssetHealthQuery")
    if not isinstance(source_version, str) or not source_version.strip():
        raise ValueError("source_version must be non-blank text")

    for attempt in (1, 2):
        try:
            first_snapshot, first_facts = _capture(
                paths,
                query,
                source_version=source_version,
            )
            if _between_observations is not None:
                _between_observations(attempt)
            second_snapshot, second_facts = _capture(
                paths,
                query,
                source_version=source_version,
            )
        except _RequiredOwnerNotQuiescent as exc:
            return _unavailable_report(
                query,
                attempts=attempt,
                reason_code="required_owner_evidence_unavailable",
                gap=f"{exc.owner}_owner_not_quiescent",
            )
        except KnowledgeStateRootError:
            return _unavailable_report(query, attempts=attempt)

        knowledge_changed = (
            first_snapshot.consistency is not SnapshotConsistency.STABLE
            or second_snapshot.consistency is not SnapshotConsistency.STABLE
            or first_snapshot.snapshot_id != second_snapshot.snapshot_id
        )
        facts_changed = first_facts.fact_snapshot_id != second_facts.fact_snapshot_id
        if not knowledge_changed and not facts_changed:
            return _report_from_fact_snapshot(second_facts, attempts=attempt)
        if attempt == 2:
            return _snapshot_changed_report(
                second_facts,
                knowledge_changed=knowledge_changed,
                facts_changed=facts_changed,
            )
    raise AssertionError("bounded Knowledge asset health loop did not return")


__all__ = [
    "KNOWLEDGE_ASSET_HEALTH_SOURCE_VERSION",
    "inspect_knowledge_asset_health",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.knowledge_asset_health")
