"""Source/item delta reuse keeps published text members immutable."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.semantic import semantic_service as service
from neocortex.semantic.semantic_models import SemanticItem, TextSection, fingerprint_text
from neocortex.semantic.semantic_sources import SemanticSourceHead, TextSourceRecord
from neocortex.semantic.semantic_state import semantic_database
from tests.test_semantic_service import (
    _declare_source_state,
    _patch_backend,
    _text_records,
)


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _head(source_kind: str, digest: str) -> SemanticSourceHead:
    return SemanticSourceHead(
        source_kind,
        f"{source_kind}.sqlite3",
        "fixture-adapter-v1",
        1,
        2,
        "sha256:" + digest,
        True,
    )


def _docx_record() -> TextSourceRecord:
    source = _text_records(1)[0]
    item = replace(
        source.item,
        item_id="item:docx:fixture-docx-0",
        source_kind="docx",
        source_identity="fixture-docx-0",
        path="C:/fixtures/fixture-docx-0.docx",
    )
    return TextSourceRecord(
        item,
        TextSection("docx_body", "document", "Contenido del documento docx.", {"fixture": True}),
    )


def _declare_empty_docx_state(state_directory: Path) -> None:
    with sqlite3.connect(service.semantic_source_database(state_directory, "docx")) as connection:
        connection.executescript(
            """CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT,processing_signature TEXT,
                status TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,
                text_xxh3_128 TEXT,text_chars INTEGER
            );
            CREATE TABLE document_parts(
                file_key TEXT,part_name TEXT,part_kind TEXT,ordinal INTEGER,text_chars INTEGER
            );"""
        )


def _declare_text_state(state_directory: Path) -> None:
    with sqlite3.connect(service.semantic_source_database(state_directory, "text")) as connection:
        connection.execute(
            """CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,path TEXT,processing_signature TEXT,
                status TEXT,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER,
                text_xxh3_128 TEXT,text_chars INTEGER,content_kind TEXT,
                media_type TEXT,title TEXT,author TEXT,metadata_json TEXT,
                text_truncated INTEGER,detail TEXT,text_zlib BLOB
            )"""
        )
        connection.execute(
            """INSERT INTO documents VALUES(
                'fixture-text','/corpus/fixture.txt','route-v1','complete',
                10,20,30,?,20,'text','text/plain','Fixture Title',NULL,'{}',0,NULL,?
            )""",
            ("f" * 32, b"fixture-text-content"),
        )


def _text_compatible_record() -> TextSourceRecord:
    return TextSourceRecord(
        SemanticItem(
            item_id="item:text:fixture-text",
            source_kind="text",
            source_identity="fixture-text",
            identity_version="fixture-text-v1",
            fingerprint=fingerprint_text("fixture-text-descriptor"),
            path="/corpus/fixture.txt",
            provenance={"source_title": "Fixture Title"},
            source_revision={
                "size": 10,
                "mtime_ns": 20,
                "birthtime_ns": 30,
                "owner_revision": {
                    "owner": "text",
                    "revision": {
                        "resource_id": "resource:fixture-text",
                        "revision_id": "revision:fixture-text:1",
                        "producer": "fixture",
                        "processing_signature": "route-v1",
                        "generation": 1,
                        "state": "current",
                        "observed_at_utc": None,
                    },
                    "fingerprint_algorithm": "xxh3-128",
                    "fingerprint": "f" * 32,
                },
            },
        ),
        TextSection("document", "fulltext", "Contenido estable.", {"fixture": True}),
    )


def test_content_compatible_replay_requires_projection_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_text_state(tmp_path)
    record = _text_compatible_record()
    heads = [_head("text", "a" * 64)]
    monkeypatch.setattr(service._text_index, "semantic_source_heads", lambda *_args: tuple(heads))
    monkeypatch.setattr(
        service,
        "iter_text_source_records",
        lambda _state, _source: iter((record,)),
    )
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert baseline.complete

    heads[0] = replace(heads[0], digest="sha256:" + "b" * 64)
    def unexpected_backend(*_args: object, **_kwargs: object) -> object:
        pytest.fail("content-compatible replay must not initialize a backend")
        return None

    monkeypatch.setattr(service, "_backend", unexpected_backend)
    replay = service.index_text_embeddings(tmp_path, source_kinds=("text",))

    assert replay.complete
    assert replay.execution_mode == "content_compatible_replay"
    assert replay.sources_reused == 1
    assert replay.sources_enumerated == 0


@pytest.mark.parametrize(
    "metadata_name",
    ("SEMANTIC_TITLE_POLICY", "SEMANTIC_TEXT_QUALITY_POLICY"),
)
def test_content_compatible_replay_rejects_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_name: str,
) -> None:
    _patch_backend(monkeypatch)
    _declare_text_state(tmp_path)
    record = _text_compatible_record()
    heads = [_head("text", "a" * 64)]
    monkeypatch.setattr(service._text_index, "semantic_source_heads", lambda *_args: tuple(heads))
    calls: list[str] = []

    def source_records(_state: Path, _source: str):
        calls.append(_source)
        return iter((record,))

    monkeypatch.setattr(service, "iter_text_source_records", source_records)
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert baseline.complete

    monkeypatch.setattr(service._text_index, metadata_name, "drifted-policy")
    heads[0] = replace(heads[0], digest="sha256:" + "b" * 64)
    refreshed = service.index_text_embeddings(tmp_path, source_kinds=("text",))

    assert refreshed.complete
    assert refreshed.execution_mode == "enumerated"
    assert refreshed.sources_reused == 0
    assert refreshed.sources_enumerated == 1
    assert calls == ["text", "text"]


def test_changed_source_stages_only_changed_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(2)
    current_records = list(records)
    heads = [_head("pdf", "a" * 64)]
    monkeypatch.setattr(service._text_index, "semantic_source_heads", lambda *_args: tuple(heads))
    calls: list[str] = []

    def source_records(_state: Path, source_kind: str):
        calls.append(source_kind)
        return iter(tuple(current_records))

    monkeypatch.setattr(service, "iter_text_source_records", source_records)
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    assert baseline.complete
    assert baseline.items_staged == 2

    # Owner observation clocks are intentionally not semantic revisions.
    current_records[0] = replace(
        current_records[0],
        item=replace(
            current_records[0].item,
            source_revision={"last_seen_run_id": 999},
        ),
    )
    changed = records[1]
    current_records[1] = TextSourceRecord(
        replace(
            changed.item,
            fingerprint=fingerprint_text("changed-semantic-item"),
        ),
        replace(
            changed.section,
            text="Contenido modificado del segundo documento.",
        ),
    )
    heads[0] = replace(heads[0], digest="sha256:" + "b" * 64)

    refreshed = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert refreshed.complete
    assert refreshed.items_staged == 1
    assert refreshed.sources_reused == 0
    assert refreshed.sources_enumerated == 1
    assert calls == ["pdf", "pdf"]
    with semantic_database(refreshed.semantic_database, readonly=True) as connection:
        active_items = connection.execute(
            "SELECT item_id FROM semantic_items WHERE source_kind='pdf' AND active=1 ORDER BY item_id"
        ).fetchall()
        assert len(active_items) == 2


def test_unchanged_source_skips_enumeration_and_remains_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    _declare_empty_docx_state(tmp_path)
    pdf_record = _text_records(1)[0]
    docx_record = _docx_record()
    records = {"pdf": (pdf_record,), "docx": (docx_record,)}
    heads = [_head("pdf", "a" * 64), _head("docx", "c" * 64)]
    monkeypatch.setattr(
        service._text_index,
        "semantic_source_heads",
        lambda *_args: tuple(heads),
    )
    calls: list[str] = []

    def source_records(_state: Path, source_kind: str):
        calls.append(source_kind)
        return iter(records[source_kind])

    monkeypatch.setattr(service, "iter_text_source_records", source_records)
    baseline = service.index_text_embeddings(
        tmp_path,
        source_kinds=("pdf", "docx"),
    )
    assert baseline.complete
    assert calls == ["pdf", "docx"]

    heads[0] = replace(heads[0], digest="sha256:" + "b" * 64)
    refreshed = service.index_text_embeddings(
        tmp_path,
        source_kinds=("pdf", "docx"),
    )

    assert refreshed.complete
    assert refreshed.sources_reused == 1
    assert refreshed.sources_enumerated == 1
    assert calls == ["pdf", "docx", "pdf"]
    with semantic_database(refreshed.semantic_database, readonly=True) as connection:
        assert connection.execute(
            """SELECT active FROM semantic_items
            WHERE item_id=?""",
            (docx_record.item.item_id,),
        ).fetchone()[0] == 1


def test_subset_scope_reuses_published_superset_without_pdf_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integrated ``--all`` scopes may reuse a broader ready text generation."""

    _patch_backend(monkeypatch)
    _declare_source_state(tmp_path, "pdf")
    _declare_empty_docx_state(tmp_path)
    records = {"pdf": (_text_records(1)[0],), "docx": (_docx_record(),)}
    heads = {"pdf": _head("pdf", "a" * 64), "docx": _head("docx", "c" * 64)}
    monkeypatch.setattr(
        service._text_index,
        "semantic_source_heads",
        lambda _state, source_kinds: tuple(heads[source] for source in source_kinds),
    )
    calls: list[str] = []

    def source_records(_state: Path, source_kind: str):
        calls.append(source_kind)
        return iter(records[source_kind])

    monkeypatch.setattr(service, "iter_text_source_records", source_records)
    baseline = service.index_text_embeddings(
        tmp_path,
        source_kinds=("pdf", "docx"),
    )
    assert baseline.complete

    # The integrated scope selects only PDF while the published baseline also
    # covers DOCX; no PDF owner changed, so source delta must clone the base.
    replay = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))

    assert replay.complete
    assert replay.execution_mode == "enumerated"
    assert replay.sources_reused == 1
    assert replay.sources_enumerated == 0
    assert calls == ["pdf", "docx"]
