"""Direct semantic evidence uses immutable members, not another retrieval run."""

from __future__ import annotations

import copy
import hashlib
import sqlite3
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.api import read_api
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.capabilities.formats.office.state import initialize_office_state
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.capabilities.formats.text.text_state import initialize_text_state
from neocortex.knowledge import knowledge_evidence_lookup as lookup
from neocortex.knowledge import knowledge_context_hydration as hydration
from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
from neocortex.knowledge.knowledge_contracts import KnowledgeHit
from neocortex.knowledge.knowledge_search import _candidate_from_resolved
from neocortex.knowledge.knowledge_search_contracts import KnowledgeSearchResult
from neocortex.knowledge.knowledge_service import KnowledgeSearchService
from neocortex.knowledge.knowledge_planner import KnowledgeQuery
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths, collect_knowledge_snapshot
from neocortex.semantic import semantic_lexical, semantic_service
from neocortex.semantic.semantic_chunking import TextChunkingConfig
from neocortex.semantic.semantic_models import EmbeddingModality, ResolvedSearchHit, SearchHit, fingerprint_text
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.persistence.sqlite_immutable import preferred_sqlite_read_mode
from neocortex.semantic.semantic_schema import (
    SemanticStateError, semantic_database, semantic_read_context,
)
from neocortex.semantic.semantic_search_repository import resolve_search_hits
from tests.semantic_test_backend import DeterministicTestBackend


_KEY = "00000000000000000000000000000001:00000000000000000000000000000002"
_BODY = ("Radiador inspeccionado, presión perdida y embalaje intacto, conexión revisada. " * 3).strip()


def _backend(model, **_kwargs):
    return DeterministicTestBackend(replace(model, provider="test-deterministic"))


def _hashes(state: Path) -> dict[str, str]:
    return {str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(state.rglob("*")) if path.is_file()}


@pytest.fixture(params=("pdf", "text", "docx", "docx-fallback", "xlsx", "pptx", "odt"))
def semantic_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    source_kind = "docx" if request.param == "docx-fallback" else request.param
    owner = "office" if source_kind in {"xlsx", "pptx", "odt"} else source_kind
    state = tmp_path / "state"
    state.mkdir()
    path = state / f"{owner}.sqlite3"
    source_path = f"/fixture/report.{source_kind}"
    initializers = {"pdf": initialize_pdf_state, "text": initialize_text_state,
                    "docx": initialize_docx_state, "office": initialize_office_state,
                    }
    initializers[owner](path)
    with closing(sqlite3.connect(path)) as connection, connection:
        if owner == "pdf":
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,page_count,last_seen_run_id,updated_ns,
                normalized_text_xxh3_128,normalized_text_chars)
                VALUES(?,?,100,1,-1,'pdf:fixture','done',1,1,1,?,?)""",
                (_KEY, source_path, fingerprint_text(_BODY).xxh3_128, len(_BODY)),
            )
            connection.execute(
                "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars) VALUES(?,0,'native',?,?)",
                (_KEY, zlib.compress(_BODY.encode()), len(_BODY)),
            )
            connection.execute(
                "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,0,?)",
                (_KEY, source_path, _BODY),
            )
        elif owner == "text":
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,content_kind,media_type,text_zlib,text_chars,
                text_xxh3_128,last_seen_run_id,updated_ns)
                VALUES(?,?,100,1,-1,'text:fixture','complete','plain','text/plain',?,?,?,1,1)""",
                (_KEY, source_path, zlib.compress(_BODY.encode()), len(_BODY), fingerprint_text(_BODY).xxh3_128),
            )
            connection.execute(
                "INSERT INTO document_fts(file_key,path,content_kind,body) VALUES(?,?,'plain',?)",
                (_KEY, source_path, _BODY),
            )
        elif owner == "docx":
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,updated_ns)
                VALUES(?,?,100,1,-1,'docx:fixture','complete',?,?,?,1,1)""",
                (_KEY, source_path, zlib.compress(_BODY.encode()), len(_BODY), fingerprint_text(_BODY).xxh3_128),
            )
            if request.param != "docx-fallback":
                connection.execute(
                    """INSERT INTO document_parts(file_key,part_name,part_kind,ordinal,text_zlib,text_chars)
                    VALUES(?,'word/document.xml','body',0,?,?)""",
                    (_KEY, zlib.compress(_BODY.encode()), len(_BODY)),
                )
        elif owner == "office":
            connection.execute(
                """INSERT INTO documents(file_key,format,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,updated_ns)
                VALUES(?,?,?,100,1,-1,'office:fixture','complete',?,?,?,1,1)""",
                (_KEY, source_kind, source_path, zlib.compress(_BODY.encode()), len(_BODY), fingerprint_text(_BODY).xxh3_128),
            )
    monkeypatch.setattr(semantic_service, "_backend", _backend)
    model = multilingual_text_model()
    indexed = semantic_service.index_text_embeddings(
        state, source_kinds=(source_kind,), model=model, local_files_only=True,
        chunking=TextChunkingConfig(max_chars=256, max_terms=64, overlap_chars=0,
                                   overlap_terms=0, min_natural_break_chars=32),
    )
    assert len(indexed.generations) == 1
    assert indexed.generations[0].summary.status == "ready"
    with semantic_database(state / "semantic.sqlite3", readonly=True) as connection:
        member = connection.execute(
            """SELECT member.member_id,member.entity_id,member.item_id,
            member.generation_id,member.model_signature,model.vector_space
            FROM embedding_generation_members member
            JOIN embedding_models model ON model.model_signature=member.model_signature
            JOIN semantic_chunk_revisions chunk ON chunk.chunk_revision_id=member.chunk_revision_id
            WHERE chunk.section_kind!='semantic_metadata_title'
            ORDER BY member.member_id LIMIT 1"""
        ).fetchone()
        semantic_hit = SearchHit(
            ref_id=int(member["member_id"]), entity_id=str(member["entity_id"]),
            item_id=str(member["item_id"]), indexed_model_signature=str(member["model_signature"]),
            vector_space=str(member["vector_space"]), modality=EmbeddingModality.TEXT,
            score=0.8, generation_id=int(member["generation_id"]),
        )
    resolved, = resolve_search_hits(state / "semantic.sqlite3", (semantic_hit,), snippet_chars=4096)
    candidate = _candidate_from_resolved(resolved, ranking_name="semantic_text", source_rank=1,
                                         producer="semantic-fixture")
    hit = KnowledgeHit(rank=1, resource=candidate.resource, revision=candidate.revision,
                       evidence=candidate.evidence, signals=(candidate.signal,),
                       fused_score=0.8, reasons=("fixture",))
    snapshot = collect_knowledge_snapshot(KnowledgeStatePaths.from_directory(state), source_version="fixture")
    context = build_context_response_v2(
        [{"scope": "personal", "result": {"hits": [hit.to_dict()], "complete": True,
                                           "snapshot": snapshot.to_dict()}}],
        query="radiador", scope="personal", request_id="immutable-fixture", max_characters=20000,
    )
    source, = context["sources"]
    citation, = context["citations"]
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    return state, owner, source, citation, resolved


def test_exact_published_chunk_replay_never_searches_embeds_or_writes(semantic_reference, monkeypatch):
    state, owner, source, citation, resolved = semantic_reference
    before = _hashes(state)

    def forbidden(*_args, **_kwargs):
        pytest.fail("direct evidence must not search, compile a query, or embed")

    monkeypatch.setattr(read_api, "context_payload", forbidden)
    monkeypatch.setattr(semantic_service, "_backend", forbidden)
    monkeypatch.setattr(semantic_service, "_query_vector", forbidden)
    monkeypatch.setattr(semantic_lexical, "_compile_natural_fts_query_plan", forbidden)
    monkeypatch.setattr(semantic_lexical, "search_lexical_source", forbidden)
    statements = []
    observe = lookup._observe_owner

    def trace_observation(connection, selected_owner):
        connection.set_trace_callback(statements.append)
        return observe(connection, selected_owner)

    monkeypatch.setattr(lookup, "_observe_owner", trace_observation)
    first = lookup.lookup_owner_evidence(state, source, citation)
    second = lookup.lookup_owner_evidence(state, source, citation)
    assert first == second
    assert first["hits"][0]["evidence"]["snippet"] == resolved.snippet
    assert first["hits"][0]["evidence"]["evidence_id"] == citation["evidence_id"]
    assert first["hits"][0]["evidence"]["generation"] == citation["generation"]
    extent = first["hits"][0]["evidence_extent"]
    assert not extent["bounded"]
    assert extent["returned_range"] == {"start_char": 0, "end_char": len(resolved.snippet),
                                        "basis": "normalized_chunk"}
    assert first["snapshot"]["consistency"] == "owner_revalidated"
    assert first["snapshot"]["validation_scope"] == "referenced_owners_only"
    assert first["snapshot"]["snapshot_id"] != source["snapshot_id"]
    assert first["snapshot"]["origin_snapshot_id"] == source["snapshot_id"]
    result = read_api.evidence_payload(source_ref=source, evidence_ref=citation, scope="personal")
    assert result["exit_code"] == 0, result["error"]
    returned, = result["citations"]
    assert returned["evidence_id"] == citation["evidence_id"]
    assert returned["locator"] == citation["locator"]
    assert returned["extent"] == extent
    if owner == "pdf":
        assert returned["locator"]["page"] == 0
    assert not any(" MATCH " in statement.upper() for statement in statements)
    assert _hashes(state) == before


@pytest.mark.parametrize(("target", "field", "value", "code"), (
    ("source", "revision_id", "forged", "owner_revision_changed"),
    ("source", "resource_id", "forged", "owner_revision_changed"),
    ("source", "processing_signature", "forged", "owner_revision_changed"),
    ("source", "source_id", "forged", "invalid_evidence_reference"),
    ("source", "retrieval_publication", [], "retrieval_publication_changed"),
    ("citation", "generation", 99999, "published_evidence_absent_or_ambiguous"),
    ("citation", "generation", True, "invalid_evidence_reference"),
    ("citation", "generation", 1 << 80, "invalid_evidence_reference"),
    ("citation", "source_identity", "other:file", "published_evidence_absent_or_ambiguous"),
    ("citation", "evidence_id", "forged", "invalid_evidence_reference"),
))
def test_changed_reference_is_not_rebound(semantic_reference, target, field, value, code):
    state, _owner, original_source, original_citation, _resolved = semantic_reference
    source, citation = copy.deepcopy(original_source), copy.deepcopy(original_citation)
    (source if target == "source" else citation)[field] = value
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=f"^{code}$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


def test_current_owner_revision_must_match_even_when_watermark_does_not_change(semantic_reference):
    state, owner, source, citation, _resolved = semantic_reference
    with closing(sqlite3.connect(state / f"{owner}.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET size=size+1")
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=r"^owner_revision_changed$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


def test_moved_published_head_rejects_previous_member(semantic_reference):
    state, _owner, source, citation, _resolved = semantic_reference
    with closing(sqlite3.connect(state / "semantic.sqlite3")) as connection, connection:
        connection.execute("DELETE FROM published_embedding_heads")
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=r"^retrieval_publication_changed$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


def test_semantic_current_item_drift_does_not_make_a_historical_member_current(semantic_reference):
    state, _owner, source, citation, _resolved = semantic_reference
    with closing(sqlite3.connect(state / "semantic.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE semantic_items SET source_revision_json=json_set(source_revision_json,'$.mtime_ns',2)"
        )
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=r"^owner_revision_changed$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


@pytest.mark.parametrize(("field", "value", "code"), (
    ("end_char", 999999, "evidence_range_unavailable"),
    ("section_id", "another-section", "evidence_locator_changed"),
    ("start_char", True, "invalid_evidence_reference"),
))
def test_bound_locator_must_match_the_same_chunk(semantic_reference, field, value, code):
    state, _owner, source, citation, _resolved = semantic_reference
    citation = copy.deepcopy(citation)
    citation["locator"][field] = value
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=f"^{code}$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


def test_exact_subrange_returns_only_requested_text(semantic_reference):
    state, _owner, source, citation, resolved = semantic_reference
    citation = copy.deepcopy(citation)
    citation["locator"]["start_char"] = resolved.start_char + 4
    citation["locator"]["end_char"] = resolved.start_char + 20
    result = lookup.lookup_owner_evidence(state, source, citation)
    assert result["hits"][0]["evidence"]["snippet"] == resolved.snippet[4:20]
    assert not result["hits"][0]["evidence_extent"]["bounded"]


def test_long_chunk_prefix_is_explicitly_bounded_and_late_subrange_abstains(semantic_reference):
    _state, _owner, _source, citation, resolved = semantic_reference
    resolved = replace(resolved, start_char=0, end_char=8000, snippet="a" * 4096)
    locator = {**citation["locator"], "start_char": 0, "end_char": 8000}
    fragment, extent = lookup._semantic_fragment(resolved, locator, chunk_chars=8000)
    assert len(fragment.snippet) == 4096
    assert extent["bounded"]
    assert extent["returned_range"] == {"start_char": 0, "end_char": 4096, "basis": "normalized_chunk"}
    with pytest.raises(lookup.EvidenceLookupError, match=r"^evidence_range_unavailable$"):
        lookup._semantic_fragment(resolved, {**locator, "start_char": 5000, "end_char": 5010}, chunk_chars=8000)


def test_normalization_keeps_source_and_normalized_ranges_distinct(semantic_reference):
    _state, _owner, _source, citation, resolved = semantic_reference
    resolved = replace(resolved, end_char=resolved.end_char + 7)
    locator = {**citation["locator"], "end_char": resolved.end_char}
    fragment, extent = lookup._semantic_fragment(resolved, locator, chunk_chars=len(resolved.snippet))
    assert fragment.snippet == resolved.snippet
    assert not extent["bounded"]
    assert extent["chunk_range"]["basis"] == "source_section"
    assert extent["returned_range"]["basis"] == "normalized_chunk"
    with pytest.raises(lookup.EvidenceLookupError, match=r"^evidence_range_unavailable$"):
        lookup._semantic_fragment(resolved, {**locator, "end_char": 20}, chunk_chars=len(resolved.snippet))


def test_ambient_operation_reuses_owner_and_semantic_preparations(semantic_reference):
    state, owner, source, citation, _resolved = semantic_reference
    before = _hashes(state)
    with semantic_read_context() as context:
        # Model views already opened by the enclosing search operation.
        for selected_owner in (owner, "semantic"):
            path = state / f"{selected_owner}.sqlite3"
            with context.acquire(path, mode=preferred_sqlite_read_mode(path).value) as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert context.metrics["prepared_views"] == 2
        for explicit in (None, context, None):
            result = lookup.lookup_owner_evidence(state, source, citation, read_context=explicit)
            assert result["hits"][0]["evidence"]["evidence_id"] == citation["evidence_id"]
            assert context.metrics["prepared_views"] == 2
        assert context.metrics["reused_views"] >= 9
        context.verify_owner_fences()
    assert context.metrics["retained_views"] == 0
    assert _hashes(state) == before


def test_ambient_final_barrier_detects_source_owner_drift(semantic_reference):
    state, owner, source, citation, _resolved = semantic_reference
    with pytest.raises(SemanticStateError, match="changed within"):
        with semantic_read_context() as context:
            lookup.lookup_owner_evidence(state, source, citation)
            with closing(sqlite3.connect(state / f"{owner}.sqlite3")) as connection, connection:
                connection.execute("UPDATE documents SET updated_ns=updated_ns+1")
            with pytest.raises(SemanticStateError, match="changed within"):
                lookup.lookup_owner_evidence(state, source, citation)
            context.verify_owner_fences()
    # Drift was introduced only by the fixture's explicit writer.
    assert context.metrics["retained_views"] == 0


def test_nontext_lexical_references_abstain_without_search(semantic_reference):
    state, owner, source, citation, _resolved = semantic_reference
    if owner in {"text", "pdf"}:
        return
    reference = copy.deepcopy(citation)
    reference["retrieval_entity_id"] = f"lexical:{source['source_kind']}:{citation['source_identity']}:body"
    reference["evidence_id"] = f"evidence:{source['source_kind']}:{reference['retrieval_entity_id']}"
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=r"^unsupported_evidence_lookup$"):
        lookup.lookup_owner_evidence(state, source, reference)
    assert _hashes(state) == before


def test_route_specific_locator_and_parent_bindings_are_revalidated(semantic_reference):
    state, owner, source, citation, _resolved = semantic_reference
    if owner not in {"docx", "office"}:
        return
    with closing(sqlite3.connect(state / f"{owner}.sqlite3")) as connection, connection:
        if owner == "office":
            connection.execute("UPDATE documents SET format='other-format'")
            code = "owner_revision_changed"
        elif citation["locator"]["section_kind"] == "docx_document":
            connection.execute(
                """INSERT INTO document_parts(file_key,part_name,part_kind,ordinal,text_zlib,text_chars)
                VALUES(?,'word/document.xml','body',0,?,?)""",
                (_KEY, zlib.compress(_BODY.encode()), len(_BODY)),
            )
            code = "evidence_locator_changed"
        else:
            connection.execute("UPDATE document_parts SET part_kind='header'")
            code = "evidence_locator_changed"
    before = _hashes(state)
    with pytest.raises(lookup.EvidenceLookupError, match=rf"^{code}$"):
        lookup.lookup_owner_evidence(state, source, citation)
    assert _hashes(state) == before


@pytest.mark.parametrize("owner", ("text", "pdf", "docx"))
@pytest.mark.parametrize("characters", (64, 4096, 4097))
def test_lexical_extent_measures_the_same_owner_row_not_the_prefix_length(tmp_path, monkeypatch, owner, characters):
    state = tmp_path / "lexical"
    state.mkdir()
    path = state / f"{owner}.sqlite3"
    source_path = f"/fixture/report.{owner}"
    pattern = "Incidente observado.\nLa carga rozó el soporte; no hubo daño.\tΔó\n"
    text = (pattern * (characters // len(pattern) + 1))[:characters]
    payload = text.encode()
    {"text": initialize_text_state, "pdf": initialize_pdf_state,
     "docx": initialize_docx_state}[owner](path)
    with closing(sqlite3.connect(path)) as connection, connection:
        if owner == "text":
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,content_kind,media_type,text_zlib,text_chars,
                text_xxh3_128,last_seen_run_id,updated_ns)
                VALUES(?,?,?,1,-1,'fixture:lexical','complete','plain','text/plain',?,?,?,1,1)""",
                (_KEY, source_path, len(payload), zlib.compress(payload), characters, fingerprint_text(text).xxh3_128),
            )
            connection.execute(
                "INSERT INTO document_fts(file_key,path,content_kind,body) VALUES(?,?,'plain',?)",
                (_KEY, source_path, text),
            )
        elif owner == "docx":
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,text_zlib,text_chars,text_xxh3_128,last_seen_run_id,updated_ns)
                VALUES(?,?,?,1,-1,'fixture:lexical','complete',?,?,?,1,1)""",
                (_KEY, source_path, len(payload), zlib.compress(payload), characters,
                 fingerprint_text(text).xxh3_128),
            )
            connection.execute("INSERT INTO document_fts(file_key,path,body) VALUES(?,?,?)",
                               (_KEY, source_path, text))
        else:
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,page_count,last_seen_run_id,updated_ns,
                normalized_text_xxh3_128,normalized_text_chars)
                VALUES(?,?,?,1,-1,'fixture:lexical','done',1,1,1,?,?)""",
                (_KEY, source_path, len(payload), fingerprint_text(text).xxh3_128, characters),
            )
            connection.execute(
                "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars) VALUES(?,0,'native',?,?)",
                (_KEY, zlib.compress(payload), characters),
            )
            connection.execute(
                "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,0,?)",
                (_KEY, source_path, text),
            )
    entity = f"lexical:pdf:{_KEY}:page:0" if owner == "pdf" else f"lexical:{owner}:{_KEY}:fulltext"
    revision = {"size": len(payload), "mtime_ns": 1, "birthtime_ns": -1,
                "processing_signature": "fixture:lexical", "last_seen_run_id": 1}
    if owner == "pdf":
        revision["is_partial"] = False
    resolved = ResolvedSearchHit(
        hit=SearchHit(ref_id=0, entity_id=entity, item_id=f"item:{owner}:{_KEY}",
                      indexed_model_signature="fixture", vector_space="owner:evidence:text:v1",
                      modality=EmbeddingModality.TEXT, score=0.0, generation_id=0),
        path=source_path, source_kind=owner, source_identity=_KEY,
        section_kind="pdf_page" if owner == "pdf" else "document",
        section_id="0" if owner == "pdf" else "fulltext", start_char=None, end_char=None,
        snippet=text[:64], source_revision=revision, source_status="done" if owner == "pdf" else "complete",
    )
    candidate = _candidate_from_resolved(resolved, ranking_name=f"fts_{owner}", source_rank=1, producer="fixture")
    snapshot = collect_knowledge_snapshot(KnowledgeStatePaths.from_directory(state), source_version="fixture")
    owner_snapshot = next(value for value in snapshot.owners if value.owner == owner)
    source = {"source_id": "S1", "scope": "personal", "owner": owner, "source_kind": owner,
              "resource_id": candidate.resource.resource_id, "revision_id": candidate.revision.revision_id,
              "processing_signature": candidate.revision.processing_signature, "snapshot_id": snapshot.snapshot_id,
              "publication": [value.to_dict() for value in owner_snapshot.publications],
              "owner_watermarks": [value.to_dict() for value in owner_snapshot.watermarks]}
    citation = {"source_id": "S1", "source_identity": _KEY, "retrieval_entity_id": entity,
                "evidence_id": candidate.evidence.evidence_id,
                "locator": {"section_kind": resolved.section_kind, "section_id": resolved.section_id}}
    if owner == "pdf":
        citation["locator"]["page"] = 0
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    before = _hashes(state)
    with semantic_read_context() as context:
        if characters > 4096:
            with pytest.raises(lookup.EvidenceLookupError, match=r"^evidence_range_unavailable$"):
                lookup.lookup_owner_evidence(state, source, citation)
            citation["locator"]["start_char"] = 64
            citation["locator"]["end_char"] = 128
        first = lookup.lookup_owner_evidence(state, source, citation)
        second = lookup.lookup_owner_evidence(state, source, citation)
        assert first == second
        assert context.metrics["prepared_views"] == (2 if characters > 4096 else 1)
        extent = first["hits"][0]["evidence_extent"]
        assert extent["source_total_chars"] == characters
        assert extent["bounded"] is False
        expected_start, expected_end = (64, 128) if characters > 4096 else (0, characters)
        assert extent["returned_range"] == {"start_char": expected_start, "end_char": expected_end,
                                            "basis": "source_section"}
        assert extent["exact_reference_range"] == {"start_char": expected_start, "end_char": expected_end,
                                                   "basis": "source_section"}
        assert first["hits"][0]["evidence"]["snippet"] == text[expected_start:expected_end]
        assert "\n" in first["hits"][0]["evidence"]["snippet"]
        assert extent["document_scope"] == ("pdf_page" if owner == "pdf" else "document")
        if owner == "pdf":
            assert extent["pdf_page_index"] == 0
        response = read_api.evidence_payload(source_ref=source, evidence_ref=citation, scope="personal")
        assert response["citations"][0]["extent"]["source_total_chars"] == characters
        context.verify_owner_fences()
    assert _hashes(state) == before
    assert not (state / "semantic.sqlite3").exists()
    if owner == "docx" and characters == 64:
        context = read_api.context_payload("incidente", scope="personal", response_version=2)
        assert context["citations"], (context["coverage"], context["error"])
        for item in context["citations"]:
            assert item["hydration"]["status"] == "owner_verified"
            assert item["excerpt"] == text
            assert item["evidence_disposition"] == "evidence_candidate"
            assert item["witness_checks"]["missing_necessary_witnesses"] == []
        assert _hashes(state) == before


@pytest.mark.parametrize("owner", ("text", "pdf", "docx"))
def test_lexical_locator_mutation_abstains_and_explicit_range_is_preserved(owner):
    locator = {"section_kind": "pdf_page" if owner == "pdf" else "document",
               "section_id": "0" if owner == "pdf" else "fulltext"}
    if owner == "pdf":
        locator["page"] = 0
    assert lookup._lexical_range(owner, locator, page=0 if owner == "pdf" else None, total=500) == (0, 500)
    ranged = {**locator, "start_char": 240, "end_char": 320}
    assert lookup._lexical_range(owner, ranged, page=0 if owner == "pdf" else None, total=500) == (240, 320)
    for field in ("section_kind", "section_id"):
        mutated = {**locator, field: "forged"}
        with pytest.raises(lookup.EvidenceLookupError, match=r"^evidence_locator_changed$"):
            lookup._lexical_range(owner, mutated, page=0 if owner == "pdf" else None, total=500)
    mutated = {**locator, "start_char": 240}
    with pytest.raises(lookup.EvidenceLookupError, match=r"^invalid_evidence_reference$"):
        lookup._lexical_range(owner, mutated, page=0 if owner == "pdf" else None, total=500)


def _fixture_hydration_service(semantic_reference, *, after_search=None):
    state, owner, _source, _citation, fixture_resolved = semantic_reference
    snapshots, executions, contexts = [], [], []

    def collect(paths, *, source_version, cancellation_check=None):
        value = collect_knowledge_snapshot(
            paths, source_version=source_version, cancellation_check=cancellation_check,
            _immutable_owners=None,
        )
        snapshots.append(value)
        return value

    def execute(paths, plan, snapshot, *, cancellation_check=None):
        if cancellation_check is not None:
            cancellation_check()
        with semantic_read_context() as context:
            contexts.append(context)
            owner_path = state / f"{owner}.sqlite3"
            with context.acquire(owner_path, mode=preferred_sqlite_read_mode(owner_path).value) as connection:
                record = connection.execute(
                    "SELECT size,mtime_ns,birthtime_ns FROM documents WHERE file_key=?",
                    (fixture_resolved.source_identity,),
                ).fetchone()
                assert record is not None
                assert record["size"] == fixture_resolved.source_revision["size"]
            with semantic_database(paths.semantic, readonly=True) as connection:
                assert connection.execute(
                    "SELECT generation_id FROM published_embedding_heads WHERE generation_id=?",
                    (fixture_resolved.hit.generation_id,),
                ).fetchone() is not None
            # Backend shape is injected, but its identity/evidence is resolved
            # from the same real published Semantic member inside this attempt.
            resolved, = resolve_search_hits(paths.semantic, (fixture_resolved.hit,), snippet_chars=48)
            candidate = _candidate_from_resolved(
                resolved, ranking_name="semantic_text", source_rank=1, producer="semantic-fixture",
            )
            hit = KnowledgeHit(rank=1, resource=candidate.resource, revision=candidate.revision,
                               evidence=candidate.evidence, signals=(candidate.signal,),
                               fused_score=0.8, reasons=("fixture",))
            result = KnowledgeSearchResult(
                plan=plan, snapshot=snapshot, hits=(hit,), rankings=(), complete=True,
                truncated=False, omitted_candidates=0, rows_scanned=1, vectors_scanned=1,
                elapsed_milliseconds=0,
            )
            executions.append(result)
            if after_search is not None:
                after_search(len(executions))
            return result

    service = KnowledgeSearchService(
        KnowledgeStatePaths.from_directory(state), source_version="fixture",
        snapshot_collector=collect, search_executor=execute,
    )
    return service, snapshots, executions, contexts


def test_search_and_owner_hydration_share_one_real_attempt(semantic_reference, monkeypatch):
    state, _owner, _source, citation, resolved = semantic_reference
    service, snapshots, executions, contexts = _fixture_hydration_service(semantic_reference)
    before = _hashes(state)
    metrics = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("hydration must not rerun retrieval, compile queries, or generate embeddings")

    monkeypatch.setattr(semantic_service, "_backend", forbidden)
    monkeypatch.setattr(semantic_service, "_query_vector", forbidden)
    monkeypatch.setattr(semantic_lexical, "_compile_natural_fts_query_plan", forbidden)
    result, data = hydration.search_context_evidence(
        service, KnowledgeQuery("¿Qué información contiene el documento?"),
        scope="personal", read_metrics_sink=metrics.append,
    )
    assert len(executions) == len(contexts) == len(metrics) == 1
    assert len(snapshots) == 2
    assert snapshots[0].snapshot_id == snapshots[1].snapshot_id == result.snapshot.snapshot_id
    assert metrics[0]["outcome"] == "stable"
    assert metrics[0]["semantic_read"]["prepared_views"] == 2
    assert metrics[0]["semantic_read"]["reused_views"] >= 4
    assert metrics[0]["semantic_read"]["retained_views"] == 0
    assert data["context_hydration"]["status"] == "complete"
    assert data["context_hydration"]["available_references"] == 1
    assert data["context_hydration"]["attempted_references"] == 1
    original = executions[0].to_dict()
    assert result.hits == executions[0].hits
    assert len(result.hits[0].evidence.snippet) == 48
    hit, = data["hits"]
    assert hit["evidence"]["evidence_id"] == citation["evidence_id"]
    assert hit["evidence"]["snippet"] == resolved.snippet
    assert hit["evidence_hydration"]["status"] == "owner_verified"
    assert not hit["evidence_extent"]["bounded"]
    for field in ("rank", "fused_score", "resource", "revision", "reasons"):
        assert hit[field] == original["hits"][0][field]
    for field in ("rows_scanned", "vectors_scanned", "rankings"):
        assert data[field] == original[field]
    for before_signal, after_signal in zip(original["hits"][0]["signals"], hit["signals"], strict=True):
        assert {key: value for key, value in before_signal.items() if key != "evidence"} == {
            key: value for key, value in after_signal.items()
            if key not in {"evidence", "evidence_hydration", "evidence_extent"}
        }
    assert _hashes(state) == before


@pytest.mark.parametrize("semantic_reference", ("text", "pdf"), indirect=True)
def test_search_hydration_retry_discards_the_conflicted_projection(semantic_reference, monkeypatch):
    state, owner, _source, citation, resolved = semantic_reference
    writer_hashes = []

    def conflict_once(attempt):
        if attempt == 1:
            with closing(sqlite3.connect(state / f"{owner}.sqlite3")) as connection, connection:
                connection.execute("UPDATE documents SET updated_ns=updated_ns+1 WHERE file_key=?",
                                   (resolved.source_identity,))
            writer_hashes.append(_hashes(state))

    service, snapshots, executions, contexts = _fixture_hydration_service(
        semantic_reference, after_search=conflict_once,
    )
    projections, metrics = [], []
    real_hydrate = hydration.hydrate_context_result

    def record_projection(result, **kwargs):
        projection = real_hydrate(result, **kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(hydration, "hydrate_context_result", record_projection)
    result, data = hydration.search_context_evidence(
        service, KnowledgeQuery("¿Qué información contiene el documento?"),
        scope="personal", read_metrics_sink=metrics.append,
    )
    assert len(executions) == len(contexts) == len(projections) == 2
    assert len(writer_hashes) == 1
    assert contexts[0] is not contexts[1]
    assert len(snapshots) == 4
    assert snapshots[0].snapshot_id != snapshots[1].snapshot_id
    assert len({snapshot.snapshot_id for snapshot in snapshots[1:]}) == 1
    assert [item["outcome"] for item in metrics] == ["owner_fence_changed", "stable"]
    assert all(item["semantic_read"]["prepared_views"] == 2 for item in metrics)
    assert all(item["semantic_read"]["retained_views"] == 0 for item in metrics)
    assert data is projections[1]
    assert data is not projections[0]
    assert projections[0]["context_hydration"]["available_references"] == 0
    assert data["context_hydration"]["available_references"] == 1
    assert result.complete
    assert "snapshot_retry_succeeded" in result.warnings
    assert result.snapshot.snapshot_id == snapshots[2].snapshot_id
    assert data["snapshot"]["snapshot_id"] == result.snapshot.snapshot_id
    assert data["hits"][0]["evidence"]["evidence_id"] == citation["evidence_id"]
    assert data["hits"][0]["evidence"]["snippet"] == resolved.snippet
    assert data["hits"][0]["evidence_hydration"]["status"] == "owner_verified"
    assert _hashes(state) == writer_hashes[0]
