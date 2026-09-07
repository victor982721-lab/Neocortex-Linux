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
