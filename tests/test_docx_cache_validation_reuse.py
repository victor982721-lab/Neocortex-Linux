"""DOCX replay validates its current representation once before consumption."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.docx import route as docx_route
from neocortex.capabilities.formats.docx.route import (
    DOCX_MIME,
    DocxRoute,
    DocxRouteConfig,
    search_docx_state,
)
from neocortex.deduplication import snapshot_path
from tests.test_docx_route import _State, _break_deflate_stream, _make_docx


TEST_CAPABILITIES = ("base",)


def _cached_docx(tmp_path: Path, *, partial: bool = False):
    source = tmp_path / "input.docx"
    _make_docx(source, "candidate evidence " * 2_000, compression=zipfile.ZIP_DEFLATED)
    if partial:
        _break_deflate_stream(source, "word/header1.xml")
    state = _State({DOCX_MIME: [snapshot_path(source)]})
    config = DocxRouteConfig(
        tmp_path / "state" / "docx.sqlite3",
        min_free_memory_bytes=0,
        min_free_commit_bytes=0,
    )
    first = DocxRoute(config, state, 1).run()
    assert first.extracted == 1 and first.partial_documents == int(partial)
    return source, config, state


@pytest.mark.parametrize("partial", (False, True))
def test_docx_replay_validates_document_and_parts_once(
    tmp_path: Path, monkeypatch, partial: bool,
) -> None:
    source, config, state = _cached_docx(tmp_path, partial=partial)
    original_decode = docx_route._decode_cached_text
    original_validate = docx_route._cached_docx_representation
    decoded_chars: list[int] = []
    validations: list[str] = []

    def observed_decode(payload, expected_chars, *, max_chars):
        decoded_chars.append(int(expected_chars))
        return original_decode(payload, expected_chars, max_chars=max_chars)

    def observed_validate(connection, row, *, max_chars):
        validations.append(str(row["file_key"]))
        return original_validate(connection, row, max_chars=max_chars)

    monkeypatch.setattr(docx_route, "_decode_cached_text", observed_decode)
    monkeypatch.setattr(docx_route, "_cached_docx_representation", observed_validate)
    replay = DocxRoute(config, state, 2).run()

    assert (replay.cache_hits, replay.extracted, replay.fts_documents_indexed) == (1, 0, 0)
    assert replay.cached_partial_documents == int(partial)
    assert len(validations) == 1
    assert len(decoded_chars) == (2 if partial else 3)
    assert search_docx_state(config.state_path, "candidate")[0]["path"] == str(source)


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE documents SET text_zlib=X'00'",
        "UPDATE documents SET layout_signature='changed'",
        "DELETE FROM document_parts",
        "UPDATE documents SET processing_signature='changed'",
        "UPDATE documents SET birthtime_ns=birthtime_ns+1",
        "UPDATE documents SET status='partial'",
    ),
)
def test_docx_replay_rejects_evidence_changed_after_cache_classification(
    tmp_path: Path, monkeypatch, mutation: str,
) -> None:
    source, config, state = _cached_docx(tmp_path)
    original_touch = DocxRoute._touch_cache_hit
    original_extract = docx_route.extract_docx
    consumed = []
    extractions = []

    def changed_touch(self, connection, snapshot, cache_status):
        # Inject drift on the route-owned connection at the boundary between
        # classification and consumption; no second reader opens a fenced owner.
        connection.execute(mutation)
        result = original_touch(self, connection, snapshot, cache_status)
        consumed.append(result)
        return result

    def observed_extract(*args, **kwargs):
        extractions.append(True)
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(DocxRoute, "_touch_cache_hit", changed_touch)
    monkeypatch.setattr(docx_route, "extract_docx", observed_extract)
    replay = DocxRoute(config, state, 2).run()

    assert consumed == [None]
    assert len(extractions) == 1
    assert (replay.cache_hits, replay.extracted, replay.errors) == (0, 1, 0)
    assert search_docx_state(config.state_path, "candidate")[0]["path"] == str(source)
