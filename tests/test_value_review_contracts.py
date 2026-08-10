from __future__ import annotations

from dataclasses import replace

import pytest

from _04_Nucleo_Operativo.value_review import rank_value_observations
from _04_Nucleo_Operativo.value_review_contracts import (
    ValueDimensionName,
    ValueEvidence,
    ValueEvidenceFact,
    ValueEvidenceStrength,
    ValueFileObservation,
    ValueOwnerHealth,
    ValueProvenance,
    ValueReviewQuery,
    ValueReviewState,
)


DAY_NS = 86_400_000_000_000
REFERENCE_NS = 1_800 * DAY_NS
FULL_HASH = "a" * 32
TEXT_HASH = "b" * 32


def _inventory_evidence(resource_id: str, path: str) -> ValueEvidence:
    return ValueEvidence(
        evidence_id=f"inventory:{resource_id}",
        owner="inventory",
        kind="published_inventory_record",
        strength=ValueEvidenceStrength.STRONG,
        publication_id="inventory-scan:1",
        record_id=resource_id,
        facts=(ValueEvidenceFact("path", path),),
    )


def _catalog_evidence(resource_id: str) -> ValueEvidence:
    return ValueEvidence(
        evidence_id=f"catalog:{resource_id}",
        owner="catalog",
        kind="published_catalog_record",
        strength=ValueEvidenceStrength.MODERATE,
        publication_id="catalog:text:1",
        record_id=resource_id,
        facts=(ValueEvidenceFact("source_status", "complete"),),
    )


def _exact_evidence(
    resource_id: str,
    *,
    role: str,
    full_hash: str = FULL_HASH,
    strength: ValueEvidenceStrength = ValueEvidenceStrength.STRONG,
) -> ValueEvidence:
    return ValueEvidence(
        evidence_id=f"duplicate:{resource_id}",
        owner="inventory",
        kind="exact_duplicate_plan",
        strength=strength,
        publication_id="inventory-scan:1",
        record_id=resource_id,
        facts=(
            ValueEvidenceFact("full_fingerprint", full_hash),
            ValueEvidenceFact("group_id", "7"),
            ValueEvidenceFact("role", role),
            ValueEvidenceFact("keeper_resource_id", "resource:file:1:1:-1"),
        ),
    )


def _history_evidence(resource_id: str, kind: str) -> ValueEvidence:
    return ValueEvidence(
        evidence_id=f"history:{kind}:{resource_id}",
        owner="knowledge_history",
        kind=kind,
        strength=ValueEvidenceStrength.STRONG,
        publication_id="history:1",
        record_id=resource_id,
    )


def _observation(
    number: int,
    *,
    path: str | None = None,
    size: int = 100,
    age_days: int = 365,
    health: ValueOwnerHealth = ValueOwnerHealth.UNKNOWN,
    text_count: int | None = None,
    text_hash: str | None = None,
    source_status: str | None = None,
    catalog_status: str | None = None,
    project: str | None = None,
    exact_role: str | None = None,
    exact_hash: str = FULL_HASH,
    exact_strength: ValueEvidenceStrength = ValueEvidenceStrength.STRONG,
    usage_count: int | None = None,
    citation_count: int | None = None,
    attach_usage: bool = False,
    attach_citation: bool = False,
    future_mtime: bool = False,
) -> ValueFileObservation:
    resource_id = f"resource:file:1:{number}:-1"
    selected_path = path or f"/corpus/docs/file-{number}.txt"
    evidence = [_inventory_evidence(resource_id, selected_path)]
    if source_status is not None or catalog_status is not None or text_hash is not None:
        evidence.append(_catalog_evidence(resource_id))
    if exact_role is not None:
        evidence.append(
            _exact_evidence(
                resource_id,
                role=exact_role,
                full_hash=exact_hash,
                strength=exact_strength,
            )
        )
    usage_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    if attach_usage:
        item = _history_evidence(resource_id, "usage_history")
        evidence.append(item)
        usage_ids = (item.evidence_id,)
    if attach_citation:
        item = _history_evidence(resource_id, "citation_history")
        evidence.append(item)
        citation_ids = (item.evidence_id,)
    mtime = REFERENCE_NS + DAY_NS if future_mtime else REFERENCE_NS - age_days * DAY_NS
    return ValueFileObservation(
        resource_id=resource_id,
        path=selected_path,
        size_bytes=size,
        mtime_ns=mtime,
        birthtime_ns=-1,
        owner_health=health,
        source_kind="text" if source_status is not None else None,
        source_status=source_status,
        catalog_status=catalog_status,
        primary_project=project,
        text_fingerprint=text_hash,
        text_duplicate_count=text_count,
        exact_duplicate_role=exact_role,
        exact_duplicate_hash=exact_hash if exact_role is not None else None,
        exact_duplicate_group_id="7" if exact_role is not None else None,
        exact_duplicate_keeper_id="resource:file:1:1:-1" if exact_role is not None else None,
        usage_count=usage_count,
        citation_count=citation_count,
        usage_evidence_ids=usage_ids,
        citation_evidence_ids=citation_ids,
        evidence=tuple(evidence),
        provenance=(ValueProvenance("inventory", 9, "inventory-scan:1"),),
    )


LOGICAL_CASES = (
    (
        "exact redundant",
        _observation(2, exact_role="redundant"),
        ValueReviewState.EXACT_DUPLICATE_CANDIDATE,
    ),
    ("exact keeper", _observation(1, exact_role="keep"), ValueReviewState.KEEP),
    (
        "project exact redundant",
        _observation(3, path="/corpus/projects/app/copy.txt", exact_role="redundant"),
        ValueReviewState.KEEP,
    ),
    (
        "catalog project exact redundant",
        _observation(4, project="Malpaso", exact_role="redundant"),
        ValueReviewState.KEEP,
    ),
    (
        "partial exact redundant",
        _observation(5, health=ValueOwnerHealth.PARTIAL, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "incompatible exact redundant",
        _observation(6, health=ValueOwnerHealth.INCOMPATIBLE, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "corrupt exact redundant",
        _observation(7, health=ValueOwnerHealth.CORRUPT, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "encrypted exact redundant",
        _observation(8, health=ValueOwnerHealth.ENCRYPTED, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "failed exact redundant",
        _observation(9, health=ValueOwnerHealth.FAILED, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "invalid exact hash",
        _observation(10, exact_role="redundant", exact_hash="not-a-hash"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "weak exact evidence",
        _observation(11, exact_role="redundant", exact_strength=ValueEvidenceStrength.WEAK),
        ValueReviewState.UNKNOWN,
    ),
    ("positive use", _observation(12, usage_count=3, attach_usage=True), ValueReviewState.KEEP),
    (
        "positive citation",
        _observation(13, citation_count=1, attach_citation=True),
        ValueReviewState.KEEP,
    ),
    ("use without evidence", _observation(14, usage_count=3), ValueReviewState.UNKNOWN),
    (
        "unique text",
        _observation(
            15,
            health=ValueOwnerHealth.HEALTHY,
            text_count=1,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.KEEP,
    ),
    (
        "unique partial text",
        _observation(
            16,
            health=ValueOwnerHealth.PARTIAL,
            text_count=1,
            text_hash=TEXT_HASH,
            source_status="partial",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "old repeated temp",
        _observation(
            17,
            path="/corpus/tmp/repeated.log",
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.REVIEW_LOW_VALUE,
    ),
    (
        "recent repeated temp",
        _observation(
            18,
            path="/corpus/tmp/recent.log",
            age_days=10,
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "repeated temp no reference",
        _observation(
            19,
            path="/corpus/tmp/no-age.log",
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "old repeated archive",
        _observation(
            20,
            path="/corpus/archive/repeated.txt",
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.ARCHIVE_CANDIDATE,
    ),
    (
        "young repeated archive",
        _observation(
            21,
            path="/corpus/archive/young.txt",
            age_days=179,
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "catalog review temp",
        _observation(
            22,
            path="/corpus/tmp/review.log",
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="review",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "unknown owner temp",
        _observation(
            23,
            path="/corpus/tmp/unknown.log",
            health=ValueOwnerHealth.UNKNOWN,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "old empty temp",
        _observation(24, path="/corpus/tmp/empty.tmp", size=0, health=ValueOwnerHealth.UNKNOWN),
        ValueReviewState.UNKNOWN,
    ),
    (
        "new empty temp",
        _observation(25, path="/corpus/tmp/new.tmp", size=0, age_days=6),
        ValueReviewState.UNKNOWN,
    ),
    (
        "project empty temp",
        _observation(26, path="/corpus/projects/app/tmp/empty.tmp", size=0),
        ValueReviewState.KEEP,
    ),
    (
        "ordinary repeated text",
        _observation(
            27,
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "temp without uniqueness",
        _observation(
            28,
            path="/corpus/tmp/maybe.log",
            health=ValueOwnerHealth.HEALTHY,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "source tree",
        _observation(29, path="/home/victor/repo/src/module.py"),
        ValueReviewState.KEEP,
    ),
    (
        "protected root",
        _observation(30, path="/home/victor/work/client/file.pdf"),
        ValueReviewState.KEEP,
    ),
    (
        "future mtime",
        _observation(
            31,
            path="/corpus/tmp/future.log",
            future_mtime=True,
            health=ValueOwnerHealth.HEALTHY,
            text_count=2,
            text_hash=TEXT_HASH,
            source_status="complete",
            catalog_status="classified",
        ),
        ValueReviewState.UNKNOWN,
    ),
    (
        "explicit zero citations",
        _observation(32, citation_count=0, attach_citation=True),
        ValueReviewState.UNKNOWN,
    ),
    (
        "keeper identity mismatch",
        _observation(33, exact_role="keep"),
        ValueReviewState.UNKNOWN,
    ),
    (
        "redundant identity equals keeper",
        _observation(1, exact_role="redundant"),
        ValueReviewState.UNKNOWN,
    ),
)


@pytest.mark.parametrize(
    ("label", "observation", "expected"), LOGICAL_CASES, ids=[case[0] for case in LOGICAL_CASES]
)
def test_logical_value_review_fixtures_are_conservative(
    label: str,
    observation: ValueFileObservation,
    expected: ValueReviewState,
) -> None:
    reference = None if label == "repeated temp no reference" else REFERENCE_NS
    protected = ("/home/victor/work",) if label == "protected root" else ()
    report = rank_value_observations(
        (observation,),
        ValueReviewQuery(reference_time_ns=reference, protected_roots=protected),
    )

    assert report.items[0].state is expected
    assert report.items[0].advisory_only is True
    assert report.items[0].mutation_authorized is False
    assert tuple(item.name for item in report.items[0].dimensions) == tuple(ValueDimensionName)


def test_missing_history_is_unknown_and_never_inferred_as_zero() -> None:
    observation = _observation(50)
    item = rank_value_observations(
        (observation,), ValueReviewQuery(reference_time_ns=REFERENCE_NS)
    ).items[0]
    dimensions = {dimension.name: dimension for dimension in item.dimensions}

    assert dimensions[ValueDimensionName.COVERAGE].value is None
    assert dimensions[ValueDimensionName.CITATIONS].value is None
    assert dimensions[ValueDimensionName.USAGE].value is None
    assert "citation_history_unavailable" in dimensions[ValueDimensionName.CITATIONS].uncertainties
    assert "usage_history_unavailable" in dimensions[ValueDimensionName.USAGE].uncertainties


def test_ranking_and_json_replay_are_deterministic() -> None:
    observations = tuple(case[1] for case in LOGICAL_CASES[:18])
    query = ValueReviewQuery(limit=18, reference_time_ns=REFERENCE_NS)

    forward = rank_value_observations(observations, query)
    reverse = rank_value_observations(tuple(reversed(observations)), query)

    assert forward.to_json() == reverse.to_json()
    assert forward.items[0].state is ValueReviewState.EXACT_DUPLICATE_CANDIDATE
    assert forward.mutation_authorized is False


def test_limit_is_normal_top_k_and_reported_without_authority() -> None:
    values = (
        _observation(60, exact_role="redundant", size=200),
        _observation(61, exact_role="redundant", size=100),
    )
    report = rank_value_observations(
        values,
        ValueReviewQuery(limit=1, reference_time_ns=REFERENCE_NS),
    )

    assert report.complete is True
    assert report.matched_count == 2
    assert report.returned_count == 1
    assert report.truncated is True
    assert report.items[0].size_bytes == 200
    assert report.mutation_authorized is False


@pytest.mark.parametrize("limit", (0, 1001, True, 1.5))
def test_limit_contract_is_one_through_one_thousand(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        rank_value_observations((), ValueReviewQuery(limit=limit))  # type: ignore[arg-type]


def test_scope_and_prefilters_are_explicit_and_deterministic() -> None:
    values = (
        _observation(70, path="/corpus/tmp/a.log", source_status="complete"),
        _observation(71, path="/corpus/tmp/b.txt", source_status="complete"),
        _observation(72, path="/elsewhere/tmp/c.log", source_status="complete"),
        replace(_observation(73, path="/corpus/tmp/d.log"), source_kind="pdf"),
    )
    report = rank_value_observations(
        values,
        ValueReviewQuery(
            scope="/corpus",
            extensions=(".log",),
            source_kinds=("text",),
            states=(ValueReviewState.UNKNOWN,),
            reference_time_ns=REFERENCE_NS,
        ),
    )

    assert [item.path for item in report.items] == ["/corpus/tmp/a.log"]


def test_duplicate_resource_identity_fails_closed() -> None:
    value = _observation(80)
    report = rank_value_observations((value, value), ValueReviewQuery())

    assert report.complete is False
    assert report.reason == "ambiguous_resource_identity"
    assert report.items == ()


def test_state_prefilter_requires_typed_contract_values() -> None:
    with pytest.raises(ValueError, match="ValueReviewState"):
        rank_value_observations(
            (),
            ValueReviewQuery(states=("unknown",)),  # type: ignore[arg-type]
        )
