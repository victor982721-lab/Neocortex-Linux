"""Contracts for the Knowledge Search data-model extraction."""
# region [00] Contexto del módulo
# Módulo: tests/test_knowledge_search_extraction_contract.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import hashlib
import pickle
from dataclasses import FrozenInstanceError, replace

import pytest

from neocortex.knowledge import knowledge_search
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    KnowledgeSnapshot,
    OwnerAvailability,
    OwnerSnapshot,
    PhysicalIdentityRef,
    RankingSignal,
    ResourceRef,
    RevisionRef,
    RevisionState,
)
from neocortex.knowledge.knowledge_planner import (
    KnowledgeQuery,
    plan_knowledge_query,
)
from neocortex.knowledge.knowledge_search import (
    KnowledgeCandidate,
    KnowledgeSearchResult,
    RankingExecution,
    fuse_evidence_rankings,
)
# endregion [01]

# region [02] Implementación


PUBLIC_MODULE = "neocortex.knowledge.knowledge_search"
CONTRACT_MODULE = "neocortex.knowledge.knowledge_search_contracts"
RESULT_JSON_SHA256 = "E675133B33177ED2E1F808DB4526D1F62A0FFE9917AB3803AD8C868CC0E2E596"


def _snapshot() -> KnowledgeSnapshot:
    versions = {
        "pdf": 11,
        "docx": 5,
        "office": 1,
        "audio": 1,
        "semantic": 6,
        "catalog": 6,
        "inventory": 7,
    }
    return KnowledgeSnapshot.create(
        source_version="0.7.1",
        captured_at_utc="2026-07-30T12:00:00Z",
        captured_monotonic_ns=1,
        owners=tuple(
            OwnerSnapshot(owner, OwnerAvailability.AVAILABLE, version, version)
            for owner, version in versions.items()
        ),
    )


def _candidate(
    *,
    evidence_id: str = "evidence:fixture",
    section_id: str = "1",
    ranking: str = "fts_pdf",
    source_rank: int = 1,
) -> KnowledgeCandidate:
    physical_identity = "1:2:3"
    resource = ResourceRef(
        f"resource:file:{physical_identity}",
        "pdf",
        "pdf",
        PhysicalIdentityRef("posix_device_inode_birthtime", physical_identity, 1),
        "C:/fixture/report.pdf",
    )
    revision = RevisionRef(
        resource.resource_id,
        "revision:fixture",
        "fixture-owner",
        "fixture-v1",
        1,
        RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        evidence_id,
        resource.resource_id,
        revision.revision_id,
        EvidenceMethod.EXTRACTED,
        section_kind="pdf_page",
        section_id=section_id,
        start_char=0,
        end_char=10,
    )
    return KnowledgeCandidate(
        resource,
        revision,
        evidence,
        RankingSignal(ranking, "fixture", 1.0, source_rank),
        "modularization fixture",
    )


def _ranking() -> RankingExecution:
    return RankingExecution(
        "fts_pdf",
        "lexical",
        True,
        True,
        True,
        1,
        rows_scanned=2,
        vectors_scanned=3,
        reason="fixture",
        owner="pdf",
        elapsed_ns=4,
    )


def _result() -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        plan_knowledge_query(KnowledgeQuery("breaker protection")),
        _snapshot(),
        (),
        (_ranking(),),
        False,
        True,
        2,
        3,
        4,
        5,
        ("fixture_warning",),
    )


def test_public_contract_shape_and_pickle_paths_are_stable() -> None:
    values_and_fields = (
        (_candidate(), "reason"),
        (_ranking(), "name"),
        (_result(), "complete"),
    )
    for value, field_name in values_and_fields:
        contract = type(value)
        assert contract.__module__ == CONTRACT_MODULE
        assert contract.__qualname__ == contract.__name__
        assert contract.__match_args__ == contract.__slots__
        assert contract.__dataclass_params__.frozen
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, getattr(value, field_name))
        restored = pickle.loads(pickle.dumps(value, protocol=5))
        assert type(restored) is contract
        assert restored == value

    for symbol in (
        KnowledgeCandidate,
        RankingExecution,
        KnowledgeSearchResult,
        fuse_evidence_rankings,
    ):
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_public_contract_payload_order_and_json_bytes_are_stable() -> None:
    candidate = _candidate()
    ranking = _ranking()
    result = _result()

    assert candidate.evidence_key == (
        candidate.resource.resource_id,
        candidate.revision.revision_id,
        candidate.evidence.evidence_id,
    )
    assert tuple(ranking.to_dict()) == (
        "name",
        "channel",
        "executed",
        "available",
        "complete",
        "returned",
        "rows_scanned",
        "row_count_semantics",
        "vectors_scanned",
        "reason",
        "owner",
        "elapsed_ns",
    )
    assert tuple(result.to_dict()) == (
        "schema_version",
        "kind",
        "query",
        "plan",
        "snapshot",
        "hits",
        "rankings",
        "complete",
        "truncated",
        "omitted_candidates",
        "rows_scanned",
        "row_count_semantics",
        "vectors_scanned",
        "elapsed_milliseconds",
        "warnings",
    )
    assert (
        hashlib.sha256(result.to_json().encode("utf-8")).hexdigest().upper()
        == RESULT_JSON_SHA256
    )


def test_public_contract_validation_messages_and_precedence_are_stable() -> None:
    candidate = _candidate()
    wrong_revision = replace(candidate.revision, resource_id="resource:file:other")
    wrong_evidence = replace(candidate.evidence, revision_id="revision:other")
    ranking = _ranking()
    result = _result()
    cases = (
        (
            lambda: replace(candidate, reason=" ", revision=wrong_revision),
            "candidate retrieval reason cannot be blank",
        ),
        (
            lambda: replace(candidate, revision=wrong_revision),
            "candidate revision does not belong to its resource",
        ),
        (
            lambda: replace(candidate, evidence=wrong_evidence),
            "candidate evidence does not belong to its revision",
        ),
        (
            lambda: replace(candidate, confidence=float("nan")),
            "candidate confidence must be between 0 and 1",
        ),
        (
            lambda: replace(ranking, name="", returned=-1),
            "ranking execution name and channel cannot be blank",
        ),
        (
            lambda: replace(ranking, returned=-1),
            "ranking execution counters cannot be negative",
        ),
        (
            lambda: replace(ranking, owner=" "),
            "ranking execution owner cannot be blank",
        ),
        (
            lambda: replace(ranking, elapsed_ns=-1),
            "ranking execution elapsed_ns cannot be negative",
        ),
        (
            lambda: replace(result, telemetry=object()),
            "KnowledgeSearchResult telemetry must describe search",
        ),
        (
            lambda: replace(result, blocking_owners=["pdf"]),
            "search blocking owners must be a tuple",
        ),
        (
            lambda: replace(result, blocking_owners=("pdf", "pdf")),
            "search blocking owners must be unique and ordered",
        ),
    )

    for factory, message in cases:
        with pytest.raises(ValueError) as raised:
            factory()
        assert str(raised.value) == message


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"limit": 0}, "Knowledge fusion limit must be between 1 and 1000"),
        ({"max_per_resource": 0}, "max_per_resource must be between 1 and 100"),
        (
            {"min_section_distance": -1},
            "min_section_distance is outside its supported bound",
        ),
    ),
)
def test_fusion_validation_messages_are_stable(
    overrides: dict[str, int],
    message: str,
) -> None:
    options = {
        "limit": 5,
        "max_per_resource": 5,
        "min_section_distance": 0,
    }
    options.update(overrides)
    with pytest.raises(ValueError) as raised:
        fuse_evidence_rankings({}, **options)
    assert str(raised.value) == message

    with pytest.raises(ValueError) as blank_name:
        fuse_evidence_rankings(
            {" ": ()},
            limit=5,
            max_per_resource=5,
            min_section_distance=0,
        )
    assert str(blank_name.value) == "Knowledge ranking names cannot be blank"


def test_fusion_dependencies_and_cancellation_remain_late_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    with monkeypatch.context() as patcher:
        patcher.setattr(knowledge_search, "RRF_K", 0.0)
        hits, omitted = fuse_evidence_rankings(
            {"fts_pdf": (candidate,)},
            limit=5,
            max_per_resource=5,
            min_section_distance=0,
        )
    assert omitted == 0
    assert hits[0].fused_score == 1.0

    second = _candidate(
        evidence_id="evidence:other",
        section_id="2",
        ranking="semantic_text",
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(
            knowledge_search,
            "_overlaps_or_too_close",
            lambda *_args: True,
        )
        hits, omitted = fuse_evidence_rankings(
            {"fts_pdf": (candidate,), "semantic_text": (second,)},
            limit=5,
            max_per_resource=5,
            min_section_distance=0,
        )
    assert omitted == 0
    assert len(hits) == 1
    assert "overlapping_evidence_merged" in hits[0].warnings

    expected = RuntimeError("fusion cancellation fixture")
    checkpoints = 0

    def cancel() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise expected

    with pytest.raises(RuntimeError) as raised:
        fuse_evidence_rankings(
            {"ranking-b": (), "ranking-a": ()},
            limit=5,
            max_per_resource=5,
            min_section_distance=0,
            cancellation_check=cancel,
        )
    assert raised.value is expected
    assert checkpoints == 2


# endregion [02]
