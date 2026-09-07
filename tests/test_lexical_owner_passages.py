"""Lexical passages remain verbatim and owner-locatable at every depth."""

from __future__ import annotations

import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

import pytest

from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.capabilities.formats.text.text_state import initialize_text_state
from neocortex.knowledge.knowledge_evidence_lookup import lookup_owner_evidence
from neocortex.knowledge.knowledge_search import _candidate_from_resolved
from neocortex.knowledge.knowledge_snapshot import KnowledgeStatePaths, collect_knowledge_snapshot
from neocortex.semantic.semantic_lexical import search_lexical_source
from neocortex.semantic.semantic_models import fingerprint_text


_KEY = "00000000000000000000000000000001:00000000000000000000000000000002"
_MARKERS = ("TARGET_BEGIN", "TARGET_MIDDLE", "TARGET_END")


def _long_body() -> str:
    return (
        "opening filler " * 80
        + "TARGET_BEGIN no pressure observed. "
        + "middle filler " * 700
        + "TARGET_MIDDLE no pressure observed. "
        + "closing filler " * 700
        + "TARGET_END no pressure observed."
    )


def _create_state(tmp_path: Path, owner: str, body: str) -> Path:
    path = tmp_path / f"{owner}.sqlite3"
    source_path = f"/fixture/long.{owner}"
    if owner == "text":
        initialize_text_state(path)
    else:
        initialize_pdf_state(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        if owner == "text":
            connection.execute(
                """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                    content_kind,media_type,text_zlib,text_chars,text_xxh3_128,
                    last_seen_run_id,updated_ns,revision_id
                ) VALUES(?,?,?,1,-1,'fixture','complete','plain','text/plain',?,?,?,1,1,
                    'revision:fixture')""",
                (_KEY, source_path, len(body), zlib.compress(body.encode()),
                 len(body), fingerprint_text(body).xxh3_128),
            )
            connection.execute(
                "INSERT INTO document_fts(file_key,path,content_kind,body) VALUES(?,?,'plain',?)",
                (_KEY, source_path, body),
            )
        else:
            connection.execute(
                """INSERT INTO documents(
                    file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                    page_count,last_seen_run_id,updated_ns,normalized_text_xxh3_128,
                    normalized_text_chars
                ) VALUES(?,?,?,1,-1,'fixture','done',1,1,1,?,?)""",
                (_KEY, source_path, len(body), fingerprint_text(body).xxh3_128, len(body)),
            )
            connection.execute(
                "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars) VALUES(?,0,'native',?,?)",
                (_KEY, zlib.compress(body.encode()), len(body)),
            )
            connection.execute(
                "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,?,0,?)",
                (_KEY, source_path, body),
            )
    return path


@pytest.mark.parametrize("owner", ("text", "pdf"))
def test_long_lexical_hits_are_verbatim_and_owner_hydratable(tmp_path: Path, owner: str) -> None:
    body = _long_body()
    state_path = _create_state(tmp_path, owner, body)
    state_directory = state_path.parent
    snapshot = collect_knowledge_snapshot(
        KnowledgeStatePaths.from_directory(state_directory), source_version="fixture",
    )
    owner_snapshot = next(item for item in snapshot.owners if item.owner == owner)

    for marker in _MARKERS:
        resolved = search_lexical_source(owner, state_path, marker, limit=1).hits[0]
        assert resolved.start_char is not None and resolved.end_char is not None
        assert resolved.snippet == body[resolved.start_char:resolved.end_char]
        marker_start = body.index(marker)
        assert resolved.start_char <= marker_start < resolved.end_char
        assert marker in resolved.snippet
        assert len(body) > 4096

        candidate = _candidate_from_resolved(
            resolved, ranking_name=f"fts_{owner}", source_rank=1, producer="fixture",
        )
        source = {
            "source_id": "S1",
            "scope": "personal",
            "owner": owner,
            "source_kind": owner,
            "resource_id": candidate.resource.resource_id,
            "revision_id": candidate.revision.revision_id,
            "processing_signature": candidate.revision.processing_signature,
            "snapshot_id": snapshot.snapshot_id,
            "publication": [item.to_dict() for item in owner_snapshot.publications],
            "owner_watermarks": [item.to_dict() for item in owner_snapshot.watermarks],
        }
        evidence = candidate.evidence.to_dict()
        evidence["source_id"] = "S1"
        evidence["source_identity"] = _KEY
        evidence["retrieval_entity_id"] = resolved.hit.entity_id
        evidence["locator"] = {
            name: evidence[name]
            for name in ("page", "start_char", "end_char", "section_kind", "section_id")
            if evidence.get(name) is not None
        }
        hydrated = lookup_owner_evidence(state_directory, source, evidence)
        returned = hydrated["hits"][0]
        assert returned["evidence"]["snippet"] == resolved.snippet
        assert returned["evidence_extent"]["exact_reference_range"] == {
            "start_char": resolved.start_char,
            "end_char": resolved.end_char,
            "basis": "source_section",
        }
