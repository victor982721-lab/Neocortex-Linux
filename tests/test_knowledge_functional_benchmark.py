"""Development metric/fixture contracts; injected examples do not certify models."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.benchmark_knowledge_functional import (
    FROZEN_DATASET_SHA256,
    InstalledMeasurement,
    acceptance,
    citation_errors,
    load_fixtures,
    locator_errors,
    logical_ranking,
    ranking_metrics,
    score_predictions,
    sha256,
    verify_freeze,
)


FIXTURES = Path(__file__).parent / "fixtures" / "knowledge_functional_v1"


def _example(tmp_path: Path) -> tuple[dict, dict]:
    path = tmp_path / "source.txt"
    body = "Una observación verificable dentro de un documento sintético."
    path.write_text(body)
    pin = hashlib.sha256(body.encode()).hexdigest()
    entries = {
        str(path): {
            "logical_resource_id": "document-A",
            "revision_pin": pin,
            "sha256": sha256(path),
            "source_text": body,
        }
    }
    hit = {
        "resource": {"current_path": str(path), "resource_id": "resource-A"},
        "revision": {
            "revision_id": "revision-A",
            "resource_id": "resource-A",
            "state": "current",
            "processing_signature": "test-only-extraction",
        },
        "evidence": {
            "evidence_id": "evidence-A",
            "resource_id": "resource-A",
            "revision_id": "revision-A",
            "section_id": "fulltext",
            "snippet": body,
            "start_char": 0,
            "end_char": len(body),
        },
    }
    return hit, entries


def _bundle(hit: dict) -> dict:
    return {
        "selected_hits": [hit],
        "citation_ids": [{"citation_id": "C1", "evidence_id": "evidence-A"}],
        "rendered_context": "[C1] Una observación verificable",
    }


def test_frozen_development_partition_and_opaque_reserve_have_exact_counts() -> None:
    assert sha256(FIXTURES / "freeze.json") == FROZEN_DATASET_SHA256
    freeze = json.loads((FIXTURES / "freeze.json").read_text())
    manifest, judgments = load_fixtures(FIXTURES / "dev")
    verify_freeze(FIXTURES / "dev", FIXTURES / "freeze.json")
    assert len(manifest["files"]) == 24
    assert len({entry["logical_resource_id"] for entry in manifest["files"]}) == 23
    assert len(judgments["queries"]) == 20
    assert sum(query["kind"] == "positive" for query in judgments["queries"]) == 16
    assert sum(query["kind"] == "negative" for query in judgments["queries"]) == 4
    assert freeze["reserve"]["files"] == 16
    assert freeze["reserve"]["positive_queries"] == 8
    assert freeze["reserve"]["negative_queries"] == 2
    assert freeze["family_disjoint"] is True
    assert sha256(FIXTURES / "reserve.tar.aes") == freeze["reserve_archive_sha256"]
    assert not (FIXTURES / "reserve" / "queries.json").exists()
    assert len({Path(item["path"]).suffix for item in manifest["files"]}) >= 8


def test_duplicate_formats_share_a_content_revision_not_two_relevant_documents() -> None:
    manifest, _ = load_fixtures(FIXTURES / "dev")
    incident = [
        entry for entry in manifest["files"] if entry["logical_resource_id"] == "incident-radiator"
    ]
    assert len(incident) == 2
    assert len({entry["revision_pin"] for entry in incident}) == 1
    assert len({entry["sha256"] for entry in incident}) == 2


def test_success_is_not_recall_and_ndcg_uses_frozen_graded_relevance() -> None:
    metrics = ranking_metrics(["A", "unrelated"], {"A": 3, "B": 2, "C": 1})
    assert metrics["success_at_5"] == 1.0
    assert metrics["recall_at_5"] == pytest.approx(1 / 3)
    assert 0 < metrics["ndcg_at_10"] < 1
    assert ranking_metrics(["A", "B", "C"], {"A": 3, "B": 2, "C": 1})["ndcg_at_10"] == 1


def test_duplicate_chunks_and_aliases_are_collapsed_before_top_k(tmp_path: Path) -> None:
    hit, entries = _example(tmp_path)
    alternate = copy.deepcopy(hit)
    alias = tmp_path / "alternate.txt"
    alternate["resource"]["current_path"] = str(alias)
    entries[str(alias)] = dict(next(iter(entries.values())))
    other = copy.deepcopy(hit)
    second = tmp_path / "second.txt"
    other["resource"]["current_path"] = str(second)
    entries[str(second)] = {**next(iter(entries.values())), "logical_resource_id": "document-B"}
    ranking = logical_ranking([hit] * 8 + [alternate] * 8 + [other], entries)
    assert ranking == ["document-A", "document-B"]
    assert ranking_metrics(ranking, {"document-B": 3})["success_at_5"] == 1


def test_unknown_sources_occupy_rank_instead_of_inflating_metrics(tmp_path: Path) -> None:
    hit, _ = _example(tmp_path)
    assert logical_ranking([hit], {}) == [f"unknown:{hit['resource']['current_path']}"]
    assert locator_errors(hit, {}) == ["unknown_source_path"]


def test_locator_checks_bytes_revision_source_text_and_coordinates(tmp_path: Path) -> None:
    hit, entries = _example(tmp_path)
    assert locator_errors(hit, entries) == []
    bad = copy.deepcopy(hit)
    bad["evidence"]["revision_id"] = "stale"
    bad["evidence"]["snippet"] = "Una afirmación ausente de la fuente"
    bad["evidence"]["end_char"] = 999999
    errors = locator_errors(bad, entries)
    assert "evidence_revision_mismatch" in errors
    assert "snippet_not_in_frozen_source" in errors
    assert "character_locator_outside_source_bound" in errors
    Path(hit["resource"]["current_path"]).write_text("changed")
    assert "source_bytes_changed" in locator_errors(hit, entries)


def test_each_rendered_citation_resolves_to_exactly_one_selected_evidence(tmp_path: Path) -> None:
    hit, _ = _example(tmp_path)
    context = _bundle(hit)
    assert citation_errors(context) == []
    context["citation_ids"][0]["evidence_id"] = "missing-evidence"
    assert "citation_does_not_resolve_exactly_once" in citation_errors(context)
    context = _bundle(hit)
    context["rendered_context"] = "No citation was rendered"
    assert "citation_missing_from_rendered_context" in citation_errors(context)


def test_fts_highlighting_and_ellipsis_are_not_misclassified_as_fabrication(tmp_path: Path) -> None:
    hit, entries = _example(tmp_path)
    hit["evidence"]["snippet"] = " ... Una [observación] verificable ... "
    assert locator_errors(hit, entries) == []
    hit["evidence"]["snippet"] = " ... Una [observación] inventada ... "
    assert "snippet_not_in_frozen_source" in locator_errors(hit, entries)


def test_negative_evidence_and_missing_requests_cannot_disappear_from_denominators(
    tmp_path: Path,
) -> None:
    hit, entries = _example(tmp_path)
    queries = [
        {"query_id": "P", "kind": "positive", "relevance": {"document-A": 3}},
        {"query_id": "N", "kind": "negative", "relevance": {}},
    ]
    prediction = {
        "search": {"hits": [hit], "complete": True, "vectors_scanned": 1},
        "context": _bundle(hit),
    }
    result = score_predictions(queries, {"N": prediction}, entries)
    aggregate = result["aggregate"]
    assert aggregate["queries"] == 2
    assert aggregate["success_at_5"] == 0
    assert aggregate["execution_invalid_queries"] == 1
    assert aggregate["negative_unsupported_evidence"] == 1
    assert aggregate["real_vector_queries"] == 1


def test_reserved_acceptance_requires_all_eight_positives_and_real_model_execution() -> None:
    aggregate = {
        "success_at_5": 1.0,
        "positive_queries": 8,
        "positive_successes_at_5": 8,
        "ndcg_at_10": 0.95,
        "negative_unsupported_evidence": 0,
        "locator_integrity": 1.0,
        "citation_invalid_queries": 0,
        "citation_checks": 8,
        "execution_invalid_queries": 0,
        "real_vector_queries": 10,
        "queries": 10,
    }
    assert all(acceptance(aggregate, aggregate).values())
    seven = {**aggregate, "success_at_5": 7 / 8, "positive_successes_at_5": 7}
    assert not acceptance(seven, aggregate)["success_at_5"]
    assert not acceptance(seven, aggregate)["all_eight_reserved_positives"]
    injected = {**aggregate, "real_vector_queries": 0}
    assert not acceptance(injected, aggregate)["real_model_used"]
    assert not acceptance({**aggregate, "ndcg_at_10": 0.94}, aggregate)["ndcg_not_below_baseline"]


def test_declared_partial_coverage_is_preserved_not_fabricated_as_complete(tmp_path: Path) -> None:
    hit, entries = _example(tmp_path)
    query = {"query_id": "P", "kind": "positive", "relevance": {"document-A": 3}}
    result = score_predictions(
        [query],
        {
            "P": {
                "search": {
                    "hits": [hit],
                    "complete": False,
                    "vectors_scanned": 1,
                    "warnings": ["ranking_unavailable:out_of_scope_media"],
                },
                "context": _bundle(hit),
            }
        },
        entries,
    )
    assert result["aggregate"]["coverage_partial_queries"] == 1
    assert result["aggregate"]["coverage_warning_codes"] == [
        "ranking_unavailable:out_of_scope_media"
    ]
    assert result["aggregate"]["execution_invalid_queries"] == 0


def test_hard_timeout_never_accepts_more_than_fifteen_minutes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1 and 900"):
        InstalledMeasurement(Path("/bin/true"), tmp_path, 901)


def test_tampered_judgments_cannot_pass_the_frozen_commitment(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_bytes((FIXTURES / "dev" / "manifest.json").read_bytes())
    (tmp_path / "queries.json").write_text("{}\n")
    with pytest.raises(ValueError, match="frozen manifest or judgments changed"):
        verify_freeze(tmp_path, FIXTURES / "freeze.json")


def test_v2_citations_cannot_silently_look_like_empty_legacy_context(tmp_path: Path) -> None:
    hit, entries = _example(tmp_path)
    query = {"query_id": "N", "kind": "negative", "relevance": {}}
    prediction = {
        "search": {"hits": [hit], "complete": True},
        "context": {
            "schema": "neocortex.context-response/v2",
            "citations": [{"excerpt": "not sufficient"}],
        },
    }
    with pytest.raises(ValueError, match="not empty legacy scoring"):
        score_predictions([query], {"N": prediction}, entries)
