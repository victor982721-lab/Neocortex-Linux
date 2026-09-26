"""D2 retrieval: optional owner FTS work is not executed for image-only scopes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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
from neocortex.knowledge.knowledge_planner import KnowledgeQuery, plan_knowledge_query
from neocortex.knowledge.knowledge_search_contracts import KnowledgeCandidate, RankingExecution
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths


def _snapshot(*, semantic: OwnerAvailability = OwnerAvailability.AVAILABLE) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.create(
        source_version="opt2-fixture",
        captured_at_utc="2026-01-01T00:00:00Z",
        captured_monotonic_ns=1,
        owners=(
            OwnerSnapshot("semantic", semantic, 1, 1),
            OwnerSnapshot("pdf", OwnerAvailability.AVAILABLE, 1, 1),
            OwnerSnapshot("inventory", OwnerAvailability.ABSENT, 1, 1),
            OwnerSnapshot("catalog", OwnerAvailability.ABSENT, 1, 1),
        ),
    )


def _semantic_stub(_paths, plan, _snapshot, *_args, **_kwargs):
    return (
        {},
        (),
        [
            RankingExecution(
                step.ranking_name,
                step.channel,
                True,
                True,
                True,
                0,
                owner="semantic",
            )
            for step in plan.steps
            if step.channel == "semantic"
        ],
    )


def _candidate(*, source_kind: str, owner: str, suffix: str) -> KnowledgeCandidate:
    resource = ResourceRef(
        resource_id=f"resource:{source_kind}:{suffix}",
        source_kind=source_kind,
        owner=owner,
        physical_identity=PhysicalIdentityRef("owner_file_key", suffix, 1),
        current_path=f"/synthetic/{suffix}.png" if owner == "image" else f"/synthetic/{suffix}.pdf",
    )
    revision = RevisionRef(
        resource_id=resource.resource_id,
        revision_id=f"revision:{suffix}",
        producer="opt2-fixture",
        processing_signature="opt2-fixture-v1",
        generation=1,
        state=RevisionState.CURRENT,
    )
    evidence = EvidenceRef(
        evidence_id=f"evidence:{suffix}",
        resource_id=resource.resource_id,
        revision_id=revision.revision_id,
        method=EvidenceMethod.EXTRACTED,
        section_kind="image_ocr" if owner == "image" else "pdf_page",
        section_id="1",
        snippet=f"positive evidence {suffix}",
        start_char=0,
        end_char=len(f"positive evidence {suffix}"),
    )
    return KnowledgeCandidate(
        resource=resource,
        revision=revision,
        evidence=evidence,
        signal=RankingSignal(
            "semantic_text" if owner == "image" else "fts_pdf",
            "cosine" if owner == "image" else "bm25",
            0.9,
            1,
        ),
        reason="opt2 positive fixture evidence",
    )


def _semantic_stub_with_candidate(_paths, plan, _snapshot, *_args, **_kwargs):
    candidate = _candidate(source_kind="image", owner="image", suffix="semantic-positive")
    rankings = {
        "semantic_text": (candidate,)
    }
    reports = [
        RankingExecution(
            step.ranking_name,
            step.channel,
            True,
            True,
            True,
            len(rankings.get(step.ranking_name, ())),
            owner="semantic",
        )
        for step in plan.steps
        if step.channel == "semantic"
    ]
    return rankings, (), reports


def _catalog_stub(*_args, **_kwargs):
    return (), RankingExecution("catalog_metadata", "catalog", True, True, True, 0)


def test_image_only_scope_skips_optional_fts_owner_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def lexical_stub(_paths, plan, _snapshot, **_kwargs):
        calls.append(plan.source_kinds)
        raise AssertionError("optional lexical scope must not open owner FTS")

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _semantic_stub)
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical_stub)
    monkeypatch.setattr(knowledge_search, "_catalog_ranking", _catalog_stub)

    plan = plan_knowledge_query(
        KnowledgeQuery("estado del interruptor", source_kinds=("image",))
    )
    result = knowledge_search.execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert calls == []
    assert not any(report.channel == "lexical" for report in result.rankings)
    assert result.complete
    assert result.hits == ()


def test_unknown_explicit_scope_keeps_required_lexical_failure_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def lexical_stub(_paths, plan, _snapshot, **_kwargs):
        calls.append(plan.source_kinds)
        return {}, [
            RankingExecution(
                "lexical_scope_unsupported",
                "lexical",
                False,
                False,
                False,
                0,
            )
        ]

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _semantic_stub)
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical_stub)

    plan = plan_knowledge_query(
        KnowledgeQuery("manual", source_kinds=("unmapped-owner",))
    )
    result = knowledge_search.execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert calls == [("unmapped-owner",)]
    assert not result.complete
    assert "ranking_unavailable:lexical_scope_unsupported" in result.warnings


def test_broad_scope_retains_required_fts_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def lexical_stub(_paths, _plan, _snapshot, **_kwargs):
        nonlocal calls
        calls += 1
        return {}, [
            RankingExecution("fts_pdf", "lexical", True, True, True, 0, owner="pdf")
        ]

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _semantic_stub)
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical_stub)

    plan = plan_knowledge_query(KnowledgeQuery("estado del interruptor"))
    result = knowledge_search.execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert calls == 1
    assert result.complete


@pytest.mark.parametrize("source_kinds", ((), ("pdf",)))
def test_custom_optional_lexical_plan_preserves_positive_pdf_hit_and_citation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kinds: tuple[str, ...],
) -> None:
    candidate = _candidate(source_kind="pdf", owner="pdf", suffix="custom-pdf")
    calls = 0

    def lexical_stub(_paths, _plan, _snapshot, **_kwargs):
        nonlocal calls
        calls += 1
        return {"fts_pdf": (candidate,)}, [
            RankingExecution("fts_pdf", "lexical", True, True, True, 1, owner="pdf")
        ]

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _semantic_stub)
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical_stub)
    monkeypatch.setattr(knowledge_search, "_catalog_ranking", _catalog_stub)
    base = plan_knowledge_query(
        KnowledgeQuery("estado del interruptor", source_kinds=source_kinds)
    )
    plan = replace(
        base,
        plan_id="knowledge-plan-v1:review-custom-optional-lexical",
        steps=tuple(
            replace(step, required=False) if step.channel == "lexical" else step
            for step in base.steps
        ),
    )

    result = knowledge_search.execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    assert calls == 1
    assert result.complete
    assert any(
        hit.resource.resource_id == candidate.resource.resource_id
        and hit.evidence.evidence_id == candidate.evidence.evidence_id
        for hit in result.hits
    )


@pytest.mark.parametrize(
    ("source_kinds", "formats", "lexical_required"),
    (
        (("image", "pdf"), (), True),
        (("image_ocr", "pdf"), (), True),
        ((), ("png",), False),
        (("image",), ("png",), False),
        (("image", "image_ocr"), (), False),
    ),
)
def test_image_scope_matrix_preserves_hit_and_citation_without_dropping_pdf_fts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kinds: tuple[str, ...],
    formats: tuple[str, ...],
    lexical_required: bool,
) -> None:
    calls = 0

    def lexical_stub(_paths, _plan, _snapshot, **_kwargs):
        nonlocal calls
        calls += 1
        if not lexical_required:
            raise AssertionError("image/OCR-exclusive optional FTS must be skipped")
        pdf = _candidate(source_kind="pdf", owner="pdf", suffix="mixed-pdf")
        return {"fts_pdf": (pdf,)}, [
            RankingExecution("fts_pdf", "lexical", True, True, True, 1, owner="pdf")
        ]

    monkeypatch.setattr(knowledge_search, "_semantic_rankings", _semantic_stub_with_candidate)
    monkeypatch.setattr(knowledge_search, "_lexical_rankings", lexical_stub)
    monkeypatch.setattr(knowledge_search, "_catalog_ranking", _catalog_stub)
    plan = plan_knowledge_query(
        KnowledgeQuery(
            "estado del interruptor",
            source_kinds=source_kinds,
            formats=formats,
        )
    )

    result = knowledge_search.execute_knowledge_search(
        KnowledgeStatePaths.from_directory(tmp_path / "state"),
        plan,
        _snapshot(),
    )

    if lexical_required:
        assert calls == 1
        assert any(hit.evidence.evidence_id == "evidence:mixed-pdf" for hit in result.hits)
    else:
        assert calls == 0
        assert any(hit.evidence.evidence_id == "evidence:semantic-positive" for hit in result.hits)
    assert result.complete
