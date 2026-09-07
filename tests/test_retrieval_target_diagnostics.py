"""Targeted retrieval accounting observes only the original bounded exact scan."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from neocortex.semantic import semantic_search_repository as repository
from neocortex.semantic import semantic_search_service, semantic_service, semantic_state
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    ExactSearchPage,
    SearchHit,
    SemanticItem,
    TextChunk,
    fingerprint_text,
)
from neocortex.semantic.semantic_service_contracts import SemanticRanking
from tests.test_semantic_generation_search_failures import _initialize, _model, _query
from tests.test_semantic_service import _ConstantBackend, _calibrated_ranking_hit


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _hit(item: str, entity: str, ref: int, score: float) -> SearchHit:
    return SearchHit(
        ref, entity, item, "fixture-model", "fixture-space", EmbeddingModality.TEXT,
        score, 7, provenance={"backend": "local-fixture"},
    )


def _entries(value: dict[str, object]) -> dict[str, dict[str, object]]:
    return {entry["item_id"]: entry for entry in value["target_diagnostics"]}


@pytest.mark.parametrize(("evidence_mode", "expected_rank"), ((False, 3), (True, 4)))
def test_target_rank_counts_items_or_evidence_without_altering_original_hits(
    evidence_mode: bool, expected_rank: int,
) -> None:
    hits = (
        _hit("item:a", "chunk:a:0", 1, 0.9),
        _hit("item:a", "chunk:a:1", 2, 0.8),
        _hit("item:b", "chunk:b:0", 3, 0.7),
        _hit("item:target", "chunk:target", 4, 0.6),
    )
    diagnostics = repository._TargetedSearchDiagnostics(("item:target",), evidence_mode)
    for hit in hits:
        diagnostics.observe(hit)
    page = ExactSearchPage((hits[0],), 4, None, True)

    exported = diagnostics.export(page)
    target = _entries(exported)["item:target"]

    assert target["raw_rank"] == target["observed_rank"] == expected_rank
    assert target["rank_granularity"] == ("evidence" if evidence_mode else "item")
    assert target["rank_is_global"] is True
    assert target["within_candidate_window"] is False
    assert target["candidate_rank"] is None
    assert target["stage"] == "outside_candidate_window"
    assert target["raw_score"] == 0.6 and target["ref_id"] == 4
    assert target["generation_id"] == 7 and target["entity_id"] == "chunk:target"
    assert exported["target_hits"] == (hits[-1],)
    assert page.hits == (hits[0],) and hits[-1].score == 0.6
    assert all(isinstance(entry, tuple) for entry in diagnostics.best.values())
    assert all(not isinstance(value, SearchHit) for entry in diagnostics.best.values() for value in entry)


def test_equal_score_global_rank_uses_deterministic_item_order() -> None:
    hits = (
        _hit("item:z", "chunk:z", 1, 0.6),
        _hit("item:a", "chunk:a", 2, 0.6),
        _hit("item:m", "chunk:m", 3, 0.6),
    )
    diagnostics = repository._TargetedSearchDiagnostics(("item:z", "item:a", "item:m"), False)
    for hit in hits:
        diagnostics.observe(hit)

    entries = _entries(diagnostics.export(ExactSearchPage((), 3, None, True)))

    assert [entries[item]["raw_rank"] for item in ("item:a", "item:m", "item:z")] == [1, 2, 3]


def test_discovery_target_tie_selects_same_entity_as_original_item_retention() -> None:
    first = _hit("item:target", "chunk:a", 1, 0.5)
    second = _hit("item:target", "chunk:z", 2, 0.5)
    diagnostics = repository._TargetedSearchDiagnostics(("item:target",), False)
    retained = {}
    heap = []
    for hit in (first, second):
        repository._retain_exact_search_hit(hit, limit=1, best_by_item=retained, heap=heap)
        diagnostics.observe(hit)

    result = diagnostics.export(ExactSearchPage((), 2, None, True))

    assert result["target_hits"] == (retained["item:target"][2],)


@pytest.mark.parametrize("complete", (True, False))
def test_unobserved_target_distinguishes_complete_scope_from_incomplete_scan(complete: bool) -> None:
    observed = _hit("item:a", "chunk:a", 1, 0.8)
    diagnostics = repository._TargetedSearchDiagnostics(("item:a", "item:unseen"), False)
    diagnostics.observe(observed)
    page = ExactSearchPage((observed,), 1, None if complete else 1, complete)

    entries = _entries(diagnostics.export(page))

    assert entries["item:a"]["observed_rank"] == 1
    assert entries["item:a"]["raw_rank"] == (1 if complete else None)
    assert entries["item:a"]["rank_is_global"] is complete
    unseen = entries["item:unseen"]
    assert unseen["observed_in_published_scope"] is False
    assert unseen["stage"] == (
        "not_in_published_search_scope" if complete else "unobserved_in_incomplete_scan"
    )
    assert unseen["raw_rank"] is None and unseen["rank_is_global"] is False
    assert "raw_score" not in unseen and "ref_id" not in unseen


@pytest.mark.parametrize("budget", ("entries", "bytes"))
@pytest.mark.parametrize("in_window", (True, False))
def test_rank_accounting_cap_does_not_drop_target_or_claim_an_unknown_outside_rank(
    monkeypatch: pytest.MonkeyPatch, budget: str, in_window: bool,
) -> None:
    monkeypatch.setattr(repository, "_MAX_DIAGNOSTIC_RANK_ENTRIES", 1 if budget == "entries" else 100)
    monkeypatch.setattr(repository, "_MAX_DIAGNOSTIC_RANK_BYTES", 1 if budget == "bytes" else 100_000)
    first = _hit("item:a", "chunk:a", 1, 0.8)
    target = _hit("item:target", "chunk:target", 2, 0.6)
    diagnostics = repository._TargetedSearchDiagnostics(("item:target",), False)
    diagnostics.observe(first)
    diagnostics.observe(target)
    page = ExactSearchPage((first, target) if in_window else (first,), 2, None, True)

    exported = diagnostics.export(page)
    entry = _entries(exported)["item:target"]

    assert diagnostics.rank_budget_exhausted is True and diagnostics.best == {}
    assert entry["rank_budget_exhausted"] is True
    assert entry["raw_rank"] == (2 if in_window else None)
    assert entry["rank_is_global"] is in_window
    assert entry["observed_in_published_scope"] is True and entry["raw_score"] == 0.6
    assert exported["target_hits"] == (target,)
    assert page.scanned == 2 and page.complete is True


@pytest.mark.parametrize(
    "value", (None, [], "item:a", ("",), (" ",), (1,), ("line\nbreak",),
              ("control\x7f",), ("x" * 513,), tuple(f"item:{index}" for index in range(21))),
)
def test_malformed_target_ids_fail_before_repository_or_public_service_state_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object,
) -> None:
    def no_state(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("target validation must precede any state access")

    monkeypatch.setattr(repository, "semantic_database", no_state)
    monkeypatch.setattr(semantic_search_service, "_prepare_search_context", no_state)
    with pytest.raises(ValueError, match="diagnostic_item_ids"):
        repository.search_exact_page(tmp_path / "unopened.sqlite3", _query(_model()), diagnostic_item_ids=value)
    with pytest.raises(ValueError, match="diagnostic_item_ids"):
        semantic_service.search_semantic_index(tmp_path, "presión", diagnostic_item_ids=value)
    assert tuple(tmp_path.iterdir()) == ()


def test_empty_target_ids_remain_opt_in_and_duplicates_are_stably_deduplicated() -> None:
    assert repository.validate_diagnostic_item_ids(()) == ()
    assert repository.validate_diagnostic_item_ids(("item:b", "item:a", "item:b")) == ("item:b", "item:a")
    assert repository._TargetedSearchDiagnostics((), False).export(ExactSearchPage((), 0, None, True)) == {
        "target_diagnostics": [], "target_hits": (),
    }


def _published_fixture(database: Path):
    model = _model(signature="target-accounting-fixture-v1", space="target-accounting-space")
    _initialize(database, model)
    source_scores = {
        "item:a": (1.0, 0.9), "item:b": (0.8, 0.5),
        "item:tie-a": (0.6,), "item:tie-b": (0.6,), "item:target": (0.4,),
    }
    chunks: list[TextChunk] = []
    score_by_chunk: dict[str, float] = {}
    for item_id, scores in source_scores.items():
        text = f"Contenido sintético de {item_id}: presión interna"
        item = SemanticItem(
            item_id, "pdf", f"fixture:{item_id}", "target-source-v1", fingerprint_text(text),
            path=str(database.parent / f"{item_id.replace(':', '-')}.pdf"),
        )
        semantic_state.upsert_semantic_item(database, item, refresh_token="items", updated_ns=10)
        item_chunks = []
        for index, score in enumerate(scores):
            chunk_text = f"{text}, sección {index + 1}"
            chunk = TextChunk(
                f"chunk:{item_id}:{index}", item_id, index, "pdf_page", str(index + 1),
                0, len(chunk_text), chunk_text, fingerprint_text(chunk_text), "target-chunking-v1",
            )
            item_chunks.append(chunk)
            score_by_chunk[chunk.chunk_id] = score
        semantic_state.stage_text_chunks(database, tuple(item_chunks), refresh_token="chunks", updated_ns=11)
        semantic_state.finalize_text_chunk_refresh(
            database, item_id=item_id, chunking_signature="target-chunking-v1",
            refresh_token="chunks", updated_ns=12,
        )
        chunks.extend(item_chunks)
    generation = semantic_state.start_embedding_generation(
        database, model_signature=model.model_signature,
        processing_signature="target-processing-v1", started_ns=20,
    )
    semantic_state.enqueue_text_chunk_jobs(database, generation, tuple(chunk.chunk_id for chunk in chunks), now_ns=21)
    leases = semantic_state.claim_embedding_jobs(database, generation, worker_id="local-fixture", limit=20, lease_seconds=60, now_ns=22)
    for lease in leases:
        score = score_by_chunk[lease.entity_id]
        semantic_state.complete_embedding_job(
            database, lease.job_id, worker_id="local-fixture",
            vector=(score, math.sqrt(max(0.0, 1 - score * score)), 0.0, 0.0),
            provenance={"backend": "local-fixture"}, now_ns=23,
        )
    semantic_state.finalize_embedding_generation(database, generation, completed_ns=24)
    return model


@pytest.mark.parametrize(("evidence_mode", "target_rank", "tie_rank"), ((False, 5, 3), (True, 7, 4)))
def test_exact_scan_diagnoses_outside_top_k_and_equal_scores_from_one_vector_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence_mode: bool,
    target_rank: int, tie_rank: int,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    original = repository._exact_search_hits
    decoded: list[SearchHit] = []

    def once(*args: object, **kwargs: object):
        hits = original(*args, **kwargs)
        decoded.extend(hits)
        return hits

    monkeypatch.setattr(repository, "_exact_search_hits", once)
    diagnostics: dict[str, object] = {}
    search = repository.search_exact_evidence_page if evidence_mode else repository.search_exact_page
    page = search(
        database, _query(model), limit=2, max_vectors=100, batch_size=2,
        diagnostic_item_ids=("item:target", "item:tie-a", "item:missing"), diagnostics=diagnostics,
    )

    assert page.complete is True and page.scanned == 7
    assert len(decoded) == len({hit.ref_id for hit in decoded}) == 7
    assert len(page.hits) == 2 and all(hit.item_id != "item:target" for hit in page.hits)
    entries = _entries(diagnostics)
    assert entries["item:target"]["raw_rank"] == target_rank
    assert entries["item:tie-a"]["raw_rank"] == tie_rank
    original_target = next(hit for hit in decoded if hit.item_id == "item:target")
    assert entries["item:target"]["raw_score"] == original_target.score
    assert entries["item:target"]["stage"] == "outside_candidate_window"
    assert entries["item:missing"]["stage"] == "not_in_published_search_scope"


def test_partial_runtime_scan_does_not_infer_target_is_unindexed(tmp_path: Path) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    diagnostics: dict[str, object] = {}

    page = repository.search_exact_page(
        database, _query(model), limit=2, max_vectors=1, batch_size=2,
        diagnostic_item_ids=("item:target",), diagnostics=diagnostics,
    )

    assert page.complete is False and page.scanned == 1 and page.next_cursor is not None
    entry = _entries(diagnostics)["item:target"]
    assert entry["stage"] == "unobserved_in_incomplete_scan"
    assert entry["observed_in_published_scope"] is False
    assert entry["raw_rank"] is None and "raw_score" not in entry


def test_semantic_ranking_resolves_outside_window_target_without_rescoring_or_promoting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    original_scan = repository._exact_search_hits
    original_resolve = semantic_search_service.resolve_search_hits
    decoded: list[SearchHit] = []
    resolved_refs: list[int] = []

    def scanned(*args: object, **kwargs: object):
        hits = original_scan(*args, **kwargs)
        decoded.extend(hits)
        return hits

    def resolved(path: Path, hits: tuple[SearchHit, ...], **kwargs: object):
        resolved_refs.extend(hit.ref_id for hit in hits)
        return original_resolve(path, hits, **kwargs)

    monkeypatch.setattr(repository, "_exact_search_hits", scanned)
    monkeypatch.setattr(semantic_search_service, "resolve_search_hits", resolved)
    ranking = semantic_search_service.semantic_ranking(
        database, name="semantic_text", query_model=model, target_modality=EmbeddingModality.TEXT,
        vector=(1.0, 0.0, 0.0, 0.0), indexed_model_signatures=(model.model_signature,),
        limit=2, max_vectors=100, query="presión interna", diagnostic_item_ids=("item:a", "item:target"),
    )

    assert ranking.scanned == len(decoded) == len({hit.ref_id for hit in decoded}) == 7
    assert len(ranking.hits) == 2 and all(hit.item_id != "item:target" for hit in ranking.hits)
    assert len(resolved_refs) == len(set(resolved_refs)) == 3
    target = _entries(ranking.provenance)["item:target"]
    original_target = next(hit for hit in decoded if hit.item_id == "item:target")
    assert target["raw_rank"] == 5 and target["raw_score"] == original_target.score
    assert target["stage"] == "outside_candidate_window"
    assert target["published_chunk_fingerprint_verified"] is True
    assert "presión interna" in target["snippet"]
    assert target["source_kind"] == "pdf" and target["ref_id"] in resolved_refs


def test_public_semantic_api_forwards_target_diagnostics_with_one_local_query_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    model = _published_fixture(database)
    backends: list[_ConstantBackend] = []

    def backend(selected_model, **kwargs: object):
        assert selected_model == model
        assert kwargs["local_files_only"] is True
        instance = _ConstantBackend(selected_model)
        backends.append(instance)
        return instance

    monkeypatch.setattr(semantic_service, "_backend", backend)
    result = semantic_service.search_semantic_index(
        tmp_path, "presión interna", semantic_database=database,
        text_model=model, model_cache=tmp_path / "cache", limit=2, candidate_limit=2,
        max_vectors=100, include_images=False, include_lexical=False,
        diagnostic_item_ids=("item:target",),
    )

    assert len(backends) == 1 and len(backends[0].requests) == 1
    assert len(result.rankings) == 1
    target = _entries(result.rankings[0].provenance)["item:target"]
    assert target["raw_rank"] == 5 and target["stage"] == "outside_candidate_window"
    assert target["published_chunk_fingerprint_verified"] is True
    assert all(hit.item_id != "item:target" for hit in result.rankings[0].hits)


def test_existing_text_floor_updates_only_target_stage_without_changing_scores() -> None:
    low, low_resolved = _calibrated_ranking_hit(1, source_kind="pdf", score=0.419)
    high, high_resolved = _calibrated_ranking_hit(2, source_kind="pdf", score=0.421)
    entries = []
    for hit in (low, high):
        entries.append({
            "item_id": hit.item_id, "ref_id": hit.ref_id, "raw_score": hit.score,
            "model_signature": hit.indexed_model_signature, "source_kind": "pdf",
            "pipeline": hit.provenance["pipeline"], "backend": "fastembed",
            "calibration_contract_conflict": False, "stage": "candidate_selected",
        })
    entries.append({"item_id": "item:unseen", "stage": "unobserved_in_incomplete_scan"})
    ranking = SemanticRanking(
        "semantic_text", (high, low), (high_resolved, low_resolved), 2, False,
        provenance={"target_diagnostics": entries},
    )

    result = semantic_search_service.apply_text_retrieval_calibration(
        ranking, selected_model=semantic_service.multilingual_text_model()
    )

    assert result.hits == (high,) and result.resolved == (high_resolved,)
    targets = _entries(result.provenance)
    assert targets[low.item_id]["stage"] == "rejected_by_text_floor"
    assert targets[high.item_id]["stage"] == "accepted_by_text_calibration"
    assert targets["item:unseen"]["stage"] == "unobserved_in_incomplete_scan"
    assert targets[low.item_id]["source_score_floor"] == targets[high.item_id]["source_score_floor"] == 0.42
    assert targets[low.item_id]["above_score_floor"] is False
    assert targets[high.item_id]["above_score_floor"] is True
    assert targets[low.item_id]["raw_score"] == low.score == 0.419
    assert targets[high.item_id]["raw_score"] == high.score == 0.421
    assert entries[0]["stage"] == entries[1]["stage"] == "candidate_selected"
    final = semantic_search_service.apply_document_result_diversity(result, max_evidence_per_item=1)
    final_targets = _entries(final.provenance)
    assert final_targets[high.item_id]["stage"] == "retained"
    assert final_targets[low.item_id]["stage"] == "rejected_by_text_floor"
    assert final.hits == (high,) and final.hits[0].score == 0.421


def test_outside_candidate_target_reports_threshold_as_not_reached() -> None:
    low, low_resolved = _calibrated_ranking_hit(1, source_kind="pdf", score=0.419)
    target = {
        "item_id": "item:outside",
        "raw_score": 0.419,
        "model_signature": low.indexed_model_signature,
        "source_kind": "pdf",
        "pipeline": low_resolved.hit.provenance["pipeline"],
        "backend": "fastembed",
        "calibration_contract_conflict": False,
        "ref_id": 99,
        "stage": "outside_candidate_window",
    }
    ranking = SemanticRanking(
        "semantic_text", (low,), (low_resolved,), 3, True,
        provenance={"target_diagnostics": [target]},
    )

    result = semantic_search_service.apply_text_retrieval_calibration(
        ranking, selected_model=semantic_service.multilingual_text_model()
    )

    entry = _entries(result.provenance)["item:outside"]
    assert entry["stage"] == "outside_candidate_window"
    assert entry["source_score_floor"] == 0.42
    assert entry["above_score_floor"] is False
    assert entry["threshold_evaluation"] == "not_reached_candidate_window"
