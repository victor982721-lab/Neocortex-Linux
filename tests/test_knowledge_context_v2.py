"""Compact evidence, global output budgets and direct owner lookup."""
from __future__ import annotations

import copy
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.api import read_api
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.knowledge.knowledge_context_v2 import (
    build_context_response_v2, emitted_response_characters, serialize_context_response,
)
from neocortex.knowledge.knowledge_contracts import KnowledgeHit
from neocortex.knowledge.knowledge_search import _candidate_from_resolved
from neocortex.semantic.semantic_models import EmbeddingModality, ResolvedSearchHit, SearchHit


def _hit(evidence, *, snippet, owner="pdf", resource="file:one"):
    return {
        "rank": 1, "resource": {"resource_id": resource, "owner": owner,
            "source_kind": owner, "current_path": "/fixture/report.pdf"},
        "revision": {"revision_id": "revision:one", "state": "current", "processing_signature": "owner-v1"},
        "evidence": {"evidence_id": evidence, "page": 0, "snippet": snippet,
            "method": "extracted", "identifiers": [
                {"namespace": "source_identity", "value": "1:2"},
                {"namespace": "archive_depth", "value": "0"},
            ]},
    }


def _entry(*hits, scope="personal", complete=True, rankings=()):
    return {"scope": scope, "result": {"hits": list(hits), "complete": complete,
        "rankings": list(rankings), "snapshot": {"snapshot_id": f"snapshot:{scope}",
        "consistency": "stable", "owners": []}}}


@pytest.mark.parametrize("transport", ["text", "json", "mcp"])
def test_global_budget_sources_unique_and_substantive(transport):
    entries = [_entry(_hit("e:1", snippet="Radiador sin presión. " * 80),
                      _hit("e:2", snippet="Inspección del embalaje. " * 80))]
    first = build_context_response_v2(entries, query='presión "real"', scope="personal",
        request_id="stable-request", max_characters=10000, transport=transport)
    second = build_context_response_v2(entries, query='presión "real"', scope="personal",
        request_id="stable-request", max_characters=10000, transport=transport)
    assert first == second
    assert len(first["sources"]) == 1
    assert len(first["citations"]) == 2
    assert all(len(citation["excerpt"]) >= 240 for citation in first["citations"])
    assert "archive_depth" not in serialize_context_response(first)
    assert "selected_hits" not in first and "result" not in first
    assert first["budget"]["characters_used"] == emitted_response_characters(first, transport)
    assert first["budget"]["characters_used"] <= 10000


def test_reference_only_hits_do_not_starve_text_or_claim_pixels():
    entries = [_entry(_hit("visual", snippet=None, owner="image", resource="image:1"),
                      _hit("text", snippet="Presión perdida documentada" * 50))]
    payload = build_context_response_v2(entries, query="presión", scope="personal",
        request_id="r", max_characters=8000, transport="mcp")
    assert payload["citations"][0]["evidence_id"] == "text"
    for citation in payload["citations"]:
        if citation["evidence_id"] == "visual":
            assert citation["modality"] == "reference_only"
            assert citation["fragment_state"] == "unavailable_from_owner"
    assert payload["coverage"]["presentation"]["reasons"]
    tight = build_context_response_v2(entries, query="presión", scope="personal",
        request_id="r", max_characters=5000, transport="mcp")
    assert not tight["citations"] or tight["citations"][0]["evidence_id"] == "text"


def test_relation_failure_keeps_document_retrieval_status_and_reasons():
    entries = [_entry(_hit("e:1", snippet="El radiador perdió presión" * 100), complete=False,
        rankings=[{"name": "fts_pdf", "channel": "lexical", "executed": True,
                   "available": True, "complete": True},
                  {"name": "inventory_duplicate_plan", "channel": "structural", "executed": True,
                   "available": True, "complete": False, "reason": "invalid_or_conflicting_duplicate_plan"}])]
    payload = build_context_response_v2(entries, query="presión", scope="all",
        request_id="r", max_characters=4800, transport="mcp")
    assert payload["coverage"]["retrieval"]["status"] == "complete"
    assert payload["coverage"]["relations"]["status"] == "partial"
    assert "invalid_or_conflicting_duplicate_plan" in str(payload["coverage"])
    assert payload["exit_code"] == 4


def test_scopes_share_one_emitted_budget():
    entries = [_entry(_hit("e:1", snippet="first " * 400)),
               _entry(_hit("e:2", snippet="second " * 400), scope="framework")]
    payload = build_context_response_v2(entries, query="query", scope="all", request_id="r",
        max_characters=7000, transport="mcp")
    assert len(payload["coverage"]["scopes"]) == 2
    assert emitted_response_characters(payload, "mcp") <= 7000


@pytest.mark.parametrize("kwargs", [
    {"limit": float("nan")}, {"limit": float("inf")},
    {"include_history": object()}, {"max_characters": 999999999},
])
def test_invalid_api_parameters_are_serializable_typed_errors(kwargs):
    payload = read_api.context_payload("query", response_version=2, **kwargs)
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "invalid_request"
    assert "increase max_characters" not in payload["error"]["message"]
    assert not payload["citations"]
    assert serialize_context_response(payload)


def test_cross_revision_witness_is_never_assigned_to_the_primary_source():
    hit = _hit("current", snippet="current condition")
    hit["signals"] = [{"evidence": {
        "evidence_id": "historical", "resource_id": "file:one",
        "revision_id": "revision:old", "snippet": "historical condition",
    }}]
    with pytest.raises(ValueError, match="different resource or revision"):
        build_context_response_v2([_entry(hit)], query="condition", scope="personal", request_id="r")


def test_stale_source_revision_is_explicit_without_owner_verification():
    hit = _hit("e:stale", snippet="historical condition")
    hit["revision"].update({
        "state": "historical",
        "current_revision_id": "revision:current",
    })
    hit["evidence_hydration"] = {
        "status": "unavailable",
        "reason": "historical_reference_not_hydrated",
    }
    payload = build_context_response_v2(
        [_entry(hit)], query="condition", scope="personal", request_id="r",
    )
    source = payload["sources"][0]
    assert source["revision_binding"] == {
        "requested_revision_id": "revision:one",
        "available_revision_id": "revision:current",
        "source_revision_is_current": False,
        "reason": "historical_reference_not_hydrated",
    }
    assert payload["citations"][0]["hydration"]["status"] != "owner_verified"


def test_stale_revision_binding_survives_real_resolved_hit_materialization():
    key = "00000000000000000000000000000001:00000000000000000000000000000002"
    body = "Historical source passage remains available for review."
    resolved = ResolvedSearchHit(
        hit=SearchHit(
            ref_id=1, entity_id=f"lexical:text:{key}:fulltext",
            item_id="item:text:fixture", indexed_model_signature="fixture-model",
            vector_space="fixture-space", modality=EmbeddingModality.TEXT,
            score=0.8, generation_id=1,
        ),
        path="/fixture/historical.txt", source_kind="text", source_identity=key,
        section_kind="document", section_id="fulltext", start_char=0,
        end_char=len(body), snippet=body,
        source_revision={
            "revision_id": "revision:text:published",
            "processing_signature": "text:fixture",
        },
        source_status="complete", published_revision_id=7, current_revision_id=8,
    )
    candidate = _candidate_from_resolved(
        resolved, ranking_name="fts_text", source_rank=1, producer="fixture",
    )
    hit = KnowledgeHit(
        rank=1, resource=candidate.resource, revision=candidate.revision,
        evidence=candidate.evidence, signals=(candidate.signal,), fused_score=0.8,
        reasons=("fixture",), warnings=candidate.warnings,
    )
    payload = build_context_response_v2(
        [{"scope": "personal", "result": {
            "hits": [hit.to_dict()], "complete": True, "rankings": [],
            "snapshot": {"snapshot_id": "snapshot:fixture", "consistency": "stable", "owners": []},
        }}], query="historical", scope="personal", request_id="real-flow",
    )
    source = payload["sources"][0]
    assert source["revision_binding"] == {
        "requested_revision_id": "revision:text:published",
        "available_revision_id": 8,
        "published_revision_id": 7,
        "current_revision_id": 8,
        "source_revision_is_current": False,
        "reason": "published_source_revision_is_not_current",
    }
    assert payload["citations"][0].get("hydration", {}).get("status") != "owner_verified"


def test_discovery_title_signal_never_becomes_body_evidence():
    hit = _hit("e:discovery", snippet="body evidence")
    hit["signals"] = [{
        "source": "semantic_text",
        "evidence": dict(hit["evidence"]),
    }, {
        "source": "semantic_title",
        "query_support": {"support": "title_only"},
    }]
    payload = build_context_response_v2(
        [_entry(hit)], query="body", scope="personal", request_id="r",
        mode="discovery",
    )
    assert payload["mode"] == "discovery"
    assert payload["citations"]
    assert all(item.get("retrieval_channel") != "semantic_title" for item in payload["citations"])
    assert not any("semantic_title" in item for item in payload["citations"])


def test_wider_budget_keeps_the_first_substantive_evidence_monotonic():
    first = _hit("e:first", snippet=("First substantive condition. " * 28))
    second = _hit("e:second", snippet=("Second substantive condition. " * 28))
    narrow = build_context_response_v2(
        [_entry(first, second)], query="condition", scope="personal", request_id="r",
        max_characters=3_500,
    )
    wide = build_context_response_v2(
        [_entry(first, second)], query="condition", scope="personal", request_id="r",
        max_characters=12_000,
    )
    assert narrow["citations"]
    assert wide["citations"]
    assert wide["citations"][0]["evidence_id"] == narrow["citations"][0]["evidence_id"]
    assert wide["citations"][0]["excerpt"].startswith(narrow["citations"][0]["excerpt"].removesuffix(" …[truncated]"))
    assert narrow["coverage"]["presentation"]["status"] == "partial"
    assert "omitted_citations:1" in narrow["coverage"]["presentation"]["reasons"]
    assert wide["coverage"]["presentation"]["status"] == "complete"
    assert wide["budget"]["characters_used"] >= narrow["budget"]["characters_used"]


def test_15000_character_compact_view_keeps_tail_minimum_evidence_units():
    hits = []
    for position in range(1, 8):
        resource_id = f"resource:radiator:{position}"
        revision_id = f"revision:radiator:{position}"
        evidence_id = f"evidence:radiator:{position}"
        body = (
            "Administrative planning text without the observed condition. " * 200
            + f"\nIdentificador: radiador R{position:02d}. Condición: radiador R{position:02d} "
            "se recibió sin presión."
        )
        hit = _hit(evidence_id, snippet=body, owner="text", resource=resource_id)
        hit["revision"]["revision_id"] = revision_id
        hit["evidence"]["resource_id"] = resource_id
        hit["evidence"]["revision_id"] = revision_id
        hits.append(hit)
    payload = build_context_response_v2(
        [_entry(*hits)], query="radiadores sin presión", scope="personal",
        request_id="tail-units", max_characters=15_000,
    )
    excerpts = [item["excerpt"] for item in payload["citations"]]
    assert sum(map(len, excerpts)) >= 6_000
    assert len(excerpts) >= 6
    for position, excerpt in enumerate(excerpts, 1):
        assert f"R{position:02d}" in excerpt
        assert "sin presión" in excerpt
    assert all("retrieval_support" not in item for item in payload["citations"])
    assert all("publication" in source and "owner_watermarks" in source
               for source in payload["sources"])
    assert payload["budget"]["characters_used"] <= 15_000


def test_compact_view_is_monotonic_when_budget_expands():
    hits = []
    for position in range(1, 8):
        resource_id = f"resource:monotonic:{position}"
        revision_id = f"revision:monotonic:{position}"
        body = (
            "Unrelated administrative prefix. " * 200
            + f" Identificador R{position:02d}. Condición: radiador R{position:02d} "
            "se recibió sin presión."
        )
        hit = _hit(f"evidence:monotonic:{position}", snippet=body,
                   owner="text", resource=resource_id)
        hit["revision"]["revision_id"] = revision_id
        hit["evidence"]["resource_id"] = resource_id
        hit["evidence"]["revision_id"] = revision_id
        hits.append(hit)
    entries = [_entry(*hits)]
    narrow = build_context_response_v2(
        entries, query="radiadores sin presión", scope="personal",
        request_id="monotonic", max_characters=15_000,
    )
    wide = build_context_response_v2(
        entries, query="radiadores sin presión", scope="personal",
        request_id="monotonic", max_characters=16_000,
    )
    assert len(wide["citations"]) >= len(narrow["citations"])
    assert [item["evidence_id"] for item in wide["citations"][:len(narrow["citations"])] ] == [
        item["evidence_id"] for item in narrow["citations"]
    ]
    assert [item["excerpt"] for item in wide["citations"][:len(narrow["citations"])] ] == [
        item["excerpt"] for item in narrow["citations"]
    ]


def test_compact_profile_does_not_shrink_complete_units_to_240_chars():
    hits = []
    for position in range(1, 8):
        resource_id = f"resource:bounded:{position}"
        revision_id = f"revision:bounded:{position}"
        body = (
            "Administrative prefix. " * 90
            + f" Identificador: radiador R{position:02d}. Condición: radiador R{position:02d} "
            "se recibió sin presión."
        )
        hit = _hit(f"evidence:bounded:{position}", snippet=body,
                   owner="text", resource=resource_id)
        hit["revision"]["revision_id"] = revision_id
        hit["evidence"]["resource_id"] = resource_id
        hit["evidence"]["revision_id"] = revision_id
        hits.append(hit)
    short = _hit("evidence:bounded:short", snippet="Radiador R99 sin presión",
                 owner="text", resource="resource:bounded:short")
    hits.append(short)
    payload = build_context_response_v2(
        [_entry(*hits)], query="radiadores sin presión", scope="personal",
        request_id="bounded-units", max_characters=15_000,
    )
    long_units = [item for item in payload["citations"]
                  if item["evidence_id"] != "evidence:bounded:short"]
    assert len(long_units) >= 5
    assert all(len(item["excerpt"]) > 500 for item in long_units)
    evidence_chars = sum(
        len(item["excerpt"].removesuffix(" …[truncated]"))
        for item in payload["citations"]
    )
    assert evidence_chars >= 6_000
    assert evidence_chars / payload["budget"]["characters_used"] >= 0.5
    assert "evidence:bounded:short" not in {
        item["evidence_id"] for item in payload["citations"]
    }
    assert payload["budget"]["characters_used"] <= 15_000


def test_compact_profile_keeps_resolution_and_hydration_status_with_reduced_diagnostics():
    hits = []
    for position in range(1, 8):
        resource_id = f"resource:hydrated:{position}"
        revision_id = f"revision:hydrated:{position}"
        body = (
            "Administrative prefix. " * 90
            + f" Identificador: radiador R{position:02d}. Condición: radiador R{position:02d} "
            "se recibió sin presión."
        )
        hit = _hit(
            f"evidence:hydrated:{position}", snippet=body, owner="text",
            resource=resource_id,
        )
        hit["revision"]["revision_id"] = revision_id
        hit["evidence"]["resource_id"] = resource_id
        hit["evidence"]["revision_id"] = revision_id
        hit["evidence_hydration"] = {
            "status": "owner_verified",
            "inspected_scope": "published_evidence_reference",
        }
        hits.append(hit)

    entries = [_entry(*hits)]
    compact = build_context_response_v2(
        entries, query="radiadores sin presión", scope="personal",
        request_id="hydrated-compact", max_characters=15_000,
    )
    replay = build_context_response_v2(
        entries, query="radiadores sin presión", scope="personal",
        request_id="hydrated-compact", max_characters=15_000,
    )
    evidence_chars = sum(
        len(item["excerpt"].removesuffix(" …[truncated]"))
        for item in compact["citations"]
    )
    assert evidence_chars / compact["budget"]["characters_used"] >= 0.5
    assert compact == replay
    assert all(item["hydration"]["status"] == "owner_verified"
               for item in compact["citations"])
    assert all("inspected_scope" not in item["hydration"]
               for item in compact["citations"])
    assert all(item["locator"]["page"] == 0 for item in compact["citations"])
    assert all(item["resource_id"] == item["revision_id"].replace("revision:", "resource:")
               for item in compact["sources"])
    assert all("policy_signature" not in item["witness_checks"]
               for item in compact["citations"])

    expanded = build_context_response_v2(
        entries, query="radiadores sin presión", scope="personal",
        request_id="hydrated-expanded", max_characters=30_000,
    )
    assert all("policy_signature" in item["witness_checks"]
               for item in expanded["citations"])


def test_compact_profile_requires_observable_query_support_not_raw_volume():
    hits = [
        _hit(
            f"evidence:administrative:{position}",
            snippet="Administrative prefix. " * 120,
            owner="text",
            resource=f"resource:administrative:{position}",
        )
        for position in range(1, 8)
    ]
    payload = build_context_response_v2(
        [_entry(*hits)], query="radiadores sin presión", scope="personal",
        request_id="administrative-volume", max_characters=15_000,
    )
    assert payload["budget"]["character_limit"] == 15_000
    assert sum(len(item["excerpt"]) for item in payload["citations"]) > 0
    assert all("radiador" not in item["excerpt"].casefold() for item in payload["citations"])
    assert all("retrieval_rank" in item for item in payload["citations"])
    assert payload["budget"]["within_limit"] is True


def test_missing_witness_makes_global_context_partial_not_ok():
    query = "¿Qué factura demuestra el reemplazo de los rodamientos de Q7?"
    body = "Factura FA-27. Venta de rodamientos para Q7, material entregado al almacén."
    payload = build_context_response_v2(
        [_entry(_hit("e:manual", snippet=body))], query=query,
        scope="personal", request_id="r", max_characters=12_000,
    )
    assert payload["coverage"]["witness_checks"]["status"] == "missing"
    assert payload["status"] == "partial"
    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "incomplete_context"


def test_general_manual_about_incident_is_related_only():
    query = "¿Qué ocurrió durante el incidente de izaje?"
    body = "El manual general describe el procedimiento de izaje; no es un registro del incidente ocurrido."
    payload = build_context_response_v2(
        [_entry(_hit("e:manual", snippet=body))], query=query,
        scope="personal", request_id="r", max_characters=12_000,
    )
    citation = payload["citations"][0]
    assert citation["evidence_disposition"] == "related_only"
    assert citation["answer_sufficiency"] == "not_assessed"


@pytest.fixture
def pdf_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "pdf-state"
    state.mkdir()
    path = state / "pdf.sqlite3"
    initialize_pdf_state(path)
    key = "00000000000000000000000000000001:00000000000000000000000000000002"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,page_count,last_seen_run_id,updated_ns)
            VALUES(?, '/fixture/report.pdf',100,1,-1,'pdf:fixture','done',1,1,1)""", (key,))
        connection.execute("INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,0,?)",
            (key, "/fixture/report.pdf", "Inspección radiador presión perdida"))
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    return state


def test_pdf_zero_based_direct_lookup_revalidates_owner_without_query(pdf_state, monkeypatch):
    payload = read_api.context_payload("radiador", scope="personal", response_version=2)
    citation = next(item for item in payload["citations"] if item["locator"].get("page") == 0)
    source = next(item for item in payload["sources"] if item["source_id"] == citation["source_id"])
    before = hashlib.sha256((pdf_state / "pdf.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(read_api, "context_payload",
                        lambda *args, **kwargs: pytest.fail("unexpected search replay"))
    result = read_api.evidence_payload(source_ref=source, evidence_ref=citation, scope="personal")
    assert result["exit_code"] == 0, result["error"]
    assert result["citations"][0]["evidence_id"] == citation["evidence_id"]
    assert "presión perdida" in result["citations"][0]["excerpt"]
    assert hashlib.sha256((pdf_state / "pdf.sqlite3").read_bytes()).hexdigest() == before
    changed = copy.deepcopy(source)
    changed["revision_id"] = "forged-revision"
    rejected = read_api.evidence_payload(source_ref=changed, evidence_ref=citation, scope="personal")
    assert rejected["error"]["code"] == "owner_revision_changed"
    assert not rejected["citations"]
    with closing(sqlite3.connect(pdf_state / "pdf.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET updated_ns=2")
    drift = read_api.evidence_payload(source_ref=source, evidence_ref=citation, scope="personal")
    assert drift["error"]["code"] == "owner_publication_changed"
    assert not drift["citations"]


def test_query_evidence_v2_is_explicitly_selectable_without_legacy_wrapper(pdf_state):
    context = read_api.context_payload(
        "radiador", scope="personal", response_version=2, request_id="context-fixed",
    )
    citation = context["citations"][0]
    evidence = read_api.evidence_payload(
        "radiador", citation["citation_id"], "personal",
        evidence_id=citation["evidence_id"],
        expected_snapshot_id=context["coverage"]["scopes"][0]["snapshot_id"],
        response_version=2,
        request_id="evidence-fixed",
    )
    assert evidence["schema"] == "neocortex.evidence-response/v2"
    assert evidence["operation"] == "evidence"
    assert evidence["response_version"] == 2
    assert len(evidence["sources"]) == len(evidence["citations"]) == 1
    assert evidence["citations"][0]["evidence_id"] == citation["evidence_id"]
