"""Knowledge-side replay checks for the deterministic XLSX projection."""

from __future__ import annotations

import copy
import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.capabilities.formats.office.state import initialize_office_state
from neocortex.knowledge import knowledge_evidence_lookup as lookup
from neocortex.knowledge.knowledge_context_v2 import build_context_response_v2
from neocortex.knowledge.knowledge_contracts import KnowledgeHit
from neocortex.knowledge.knowledge_search import _candidate_from_resolved
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths, collect_knowledge_snapshot
from neocortex.semantic.semantic_chunking import normalize_embedding_text
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    ResolvedSearchHit,
    SearchHit,
    TextSection,
    fingerprint_text,
)
from neocortex.semantic import semantic_service
from neocortex.semantic.semantic_chunking import TextChunkingConfig
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.semantic.semantic_schema import semantic_database
from neocortex.semantic.semantic_office_projection import (
    XLSX_DENSE_SECTION_KIND,
    project_xlsx_section,
)
from neocortex.semantic.semantic_sources import SOURCE_ADAPTER_VERSION
from neocortex.semantic.semantic_search_repository import resolve_search_hits
from tests.semantic_test_backend import DeterministicTestBackend


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")

_KEY = "00000000000000000000000000000011:00000000000000000000000000000022"


def _cell(a1: str, value: object, *, formula: object = None, cached_value: object = None) -> str:
    return "XLSX_CELL " + json.dumps(
        {
            "workbook": "synthetic.xlsx",
            "sheet": "Resumen",
            "a1": a1,
            "type": "number" if formula is not None else "string",
            "value": value,
            "formula": formula,
            "cached_value": cached_value,
        },
        separators=(",", ":"),
    )


def _raw_body(cell_count: int = 130) -> str:
    lines = [_cell(f"A{index}", f"valor-{index}") for index in range(1, cell_count + 1)]
    lines.append(_cell("B1", "raw-formula", formula="A1+A2", cached_value="0"))
    return "\n".join(lines)


def _owner_row(state: Path, body: str) -> sqlite3.Row:
    database = state / "office.sqlite3"
    initialize_office_state(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO documents(
            file_key,format,path,size,mtime_ns,birthtime_ns,
            processing_signature,status,text_zlib,text_chars,text_xxh3_128,
            last_seen_run_id,updated_ns)
            VALUES(?,?,?,100,1,-1,'office:projection-fixture','complete',?,?,?,1,1)""",
            (
                _KEY,
                "xlsx",
                "/fixture/synthetic.xlsx",
                zlib.compress(body.encode("utf-8")),
                len(body),
                fingerprint_text(body).xxh3_128,
            ),
        )
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?", (_KEY,)
        ).fetchone()
        assert row is not None
        return row


def _resolved(dense: TextSection) -> ResolvedSearchHit:
    snippet = normalize_embedding_text(dense.text)
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=1,
            entity_id="chunk:synthetic",
            item_id=f"item:xlsx:{_KEY}",
            indexed_model_signature="fixture-model",
            vector_space="fixture-space",
            modality=EmbeddingModality.TEXT,
            score=0.0,
            generation_id=1,
        ),
        path="/fixture/synthetic.xlsx",
        source_kind="xlsx",
        source_identity=_KEY,
        section_kind=dense.section_kind,
        section_id=dense.section_id,
        start_char=0,
        end_char=len(dense.text),
        snippet=snippet,
        section_provenance=dense.provenance,
        source_status="complete",
    )


def test_knowledge_replays_xlsx_dense_locator_against_owner_raw(tmp_path: Path) -> None:
    body = _raw_body()
    state = tmp_path / "state"
    state.mkdir()
    row = _owner_row(state, body)
    source = TextSection("xlsx_document", "body", body, {"adapter": SOURCE_ADAPTER_VERSION})
    dense = next(section for section in project_xlsx_section(source) if section.section_kind == XLSX_DENSE_SECTION_KIND)

    with closing(sqlite3.connect(state / "office.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        actual_row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?", (_KEY,)
        ).fetchone()
        assert actual_row is not None
        lookup._validate_owner_locator(connection, "office", actual_row, _resolved(dense))
    assert row["text_xxh3_128"] == fingerprint_text(body).xxh3_128


@pytest.mark.parametrize("mutation", ("snippet", "provenance", "section_id"))
def test_knowledge_projection_mutations_abstain(
    tmp_path: Path, mutation: str,
) -> None:
    body = _raw_body()
    state = tmp_path / "state"
    state.mkdir()
    _owner_row(state, body)
    source = TextSection("xlsx_document", "body", body, {"adapter": SOURCE_ADAPTER_VERSION})
    dense = next(section for section in project_xlsx_section(source) if section.section_kind == XLSX_DENSE_SECTION_KIND)
    resolved = _resolved(dense)
    if mutation == "snippet":
        resolved = replace(resolved, snippet="forged")
    elif mutation == "provenance":
        provenance = copy.deepcopy(dict(dense.provenance))
        provenance["policy_signature"] = "semantic-xlsx-cell-dense-v2"
        resolved = replace(resolved, section_provenance=provenance)
    else:
        resolved = replace(resolved, section_id="body:other")

    with closing(sqlite3.connect(state / "office.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        actual_row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?", (_KEY,)
        ).fetchone()
        assert actual_row is not None
        with pytest.raises(lookup.EvidenceLookupError):
            lookup._validate_owner_locator(connection, "office", actual_row, resolved)


def test_knowledge_projection_rejects_owner_digest_drift(tmp_path: Path) -> None:
    body = _raw_body()
    state = tmp_path / "state"
    state.mkdir()
    _owner_row(state, body)
    source = TextSection("xlsx_document", "body", body, {"adapter": SOURCE_ADAPTER_VERSION})
    dense = next(section for section in project_xlsx_section(source) if section.section_kind == XLSX_DENSE_SECTION_KIND)
    with closing(sqlite3.connect(state / "office.sqlite3")) as connection, connection:
        connection.execute(
            "UPDATE documents SET text_zlib=? WHERE file_key=?",
            (zlib.compress(body.replace("valor-1", "valor-X").encode("utf-8")), _KEY),
        )
    with closing(sqlite3.connect(state / "office.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        actual_row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?", (_KEY,)
        ).fetchone()
        assert actual_row is not None
        with pytest.raises(lookup.EvidenceLookupError, match=r"^owner_revision_changed$"):
            lookup._validate_owner_locator(connection, "office", actual_row, _resolved(dense))


def test_knowledge_projection_walk_honors_cancellation_checkpoint(tmp_path: Path) -> None:
    body = _raw_body(256)
    state = tmp_path / "state"
    state.mkdir()
    _owner_row(state, body)
    source = TextSection("xlsx_document", "body", body, {"adapter": SOURCE_ADAPTER_VERSION})
    dense = next(section for section in project_xlsx_section(source) if section.section_kind == XLSX_DENSE_SECTION_KIND)
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise RuntimeError("fixture cancellation")

    with closing(sqlite3.connect(state / "office.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        actual_row = connection.execute(
            "SELECT * FROM documents WHERE file_key=?", (_KEY,)
        ).fetchone()
        assert actual_row is not None
        with pytest.raises(RuntimeError, match="fixture cancellation"):
            lookup._validate_owner_locator(
                connection, "office", actual_row, _resolved(dense), checkpoint=checkpoint,
            )
    assert calls >= 4


def test_public_knowledge_quote_replays_the_published_xlsx_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_body()
    state = tmp_path / "state"
    state.mkdir()
    _owner_row(state, body)

    monkeypatch.setattr(
        semantic_service,
        "_backend",
        lambda model, **_kwargs: DeterministicTestBackend(replace(model, provider="test-deterministic")),
    )
    indexed = semantic_service.index_text_embeddings(
        state,
        source_kinds=("xlsx",),
        model=multilingual_text_model(),
        local_files_only=True,
        chunking=TextChunkingConfig(
            max_chars=256,
            max_terms=64,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=32,
        ),
    )
    assert indexed.generations[0].summary.status == "ready"
    with semantic_database(state / "semantic.sqlite3", readonly=True) as connection:
        member = connection.execute(
            """SELECT member.member_id,member.entity_id,member.item_id,
            member.generation_id,member.model_signature,model.vector_space
            FROM embedding_generation_members member
            JOIN embedding_models model ON model.model_signature=member.model_signature
            JOIN semantic_chunk_revisions chunk ON chunk.chunk_revision_id=member.chunk_revision_id
            WHERE chunk.section_kind=? ORDER BY member.member_id LIMIT 1""",
            (XLSX_DENSE_SECTION_KIND,),
        ).fetchone()
        assert member is not None
        hit = SearchHit(
            ref_id=int(member["member_id"]),
            entity_id=str(member["entity_id"]),
            item_id=str(member["item_id"]),
            indexed_model_signature=str(member["model_signature"]),
            vector_space=str(member["vector_space"]),
            modality=EmbeddingModality.TEXT,
            score=0.8,
            generation_id=int(member["generation_id"]),
        )
    resolved, = resolve_search_hits(state / "semantic.sqlite3", (hit,), snippet_chars=4096)
    candidate = _candidate_from_resolved(
        resolved, ranking_name="semantic_text", source_rank=1, producer="projection-fixture",
    )
    knowledge_hit = KnowledgeHit(
        rank=1,
        resource=candidate.resource,
        revision=candidate.revision,
        evidence=candidate.evidence,
        signals=(candidate.signal,),
        fused_score=0.8,
        reasons=("projection-fixture",),
    )
    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state), source_version="projection-fixture",
    )
    context = build_context_response_v2(
        [{
            "scope": "personal",
            "result": {
                "hits": [knowledge_hit.to_dict()],
                "complete": True,
                "snapshot": snapshot.to_dict(),
            },
        }],
        query="valor",
        scope="personal",
        request_id="projection-replay",
        max_characters=20_000,
    )
    source, = context["sources"]
    citation, = context["citations"]
    replay = lookup.lookup_owner_evidence(state, source, citation)
    assert replay["complete"] is True
    assert replay["hits"][0]["evidence"]["snippet"] == resolved.snippet
    assert replay["hits"][0]["evidence"]["section_kind"] == XLSX_DENSE_SECTION_KIND
