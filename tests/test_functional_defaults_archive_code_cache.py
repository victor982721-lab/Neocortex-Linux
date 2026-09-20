"""Functional cache-repair coverage for the Archive owner."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

import pytest

import neocortex.capabilities.formats.archive.route as archive_route_module
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.capabilities.formats.archive.state import search_archive_state
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.persistence.framework_route_state import FrameworkRouteState
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class _ArchiveFramework(FrameworkRouteState):
    def __init__(self, candidates: Iterable[FileSnapshot]):
        self.candidates = tuple(candidates)

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        eligible = tuple(
            item
            for item in self.candidates
            if max_file_bytes is None or item.size <= max_file_bytes
        )
        return len(self.candidates), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ) -> Iterable[FileSnapshot]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        return iter(self.candidates)






def _zip_bytes(entries: Mapping[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _archive_route(state: Path, source: Path, run_id: int) -> ArchiveRoute:
    return ArchiveRoute(
        ArchiveRouteConfig(state_path=state, ocr_mode="never"),
        _ArchiveFramework((snapshot_path(source),)),
        run_id,
        cancellation=CancellationToken(),
    )








def test_archive_cache_rebuilds_fts_from_nested_durable_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fixture.zip"
    nested = _zip_bytes({"deep.txt": "nested durable archive token"})
    source.write_bytes(
        _zip_bytes(
            {
                "inner.zip": nested,
                "plain.txt": "plain durable archive token",
            }
        )
    )
    state = tmp_path / "archive.sqlite3"

    first = _archive_route(state, source, 1).run()
    with sqlite3.connect(state) as connection:
        document_count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        connection.execute("DELETE FROM document_fts")
        connection.commit()

    def fail_extraction(*_args, **_kwargs):
        raise AssertionError("cache replay must not extract ZIP members")

    monkeypatch.setattr(archive_route_module, "_extract_member_content", fail_extraction)
    second = _archive_route(state, source, 2).run()

    assert first.processed == 1
    assert second.cache_hits == 1
    # Archive's legacy summary counts each selected container, including a
    # cache observation; no extraction work is implied by this counter.
    assert second.processed == 1
    assert second.fts_rows_repaired == document_count
    hit = search_archive_state(state, "nested durable archive token")[0]
    assert hit.member_chain == "inner.zip!/deep.txt"
    assert hit.virtual_path == f"{source}!/inner.zip!/deep.txt"








def test_archive_cache_rejects_corrupt_durable_representation_and_reextracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "corrupt-cache.zip"
    source.write_bytes(_zip_bytes({"member.txt": "reextract archive token"}))
    state = tmp_path / "archive.sqlite3"
    _archive_route(state, source, 1).run()

    with sqlite3.connect(state) as connection:
        connection.execute("UPDATE documents SET text_zlib=?", (b"not-zlib",))
        connection.commit()

    original_extract = archive_route_module._extract_member_content
    calls = 0

    def counted_extract(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(archive_route_module, "_extract_member_content", counted_extract)
    second = _archive_route(state, source, 2).run()

    assert calls == 1
    assert second.cache_hits == 0
    assert second.processed == 1
    assert second.fts_rows_repaired == 0
    assert search_archive_state(state, "reextract archive token")






def test_archive_cache_decoder_rejects_overlimit_output_without_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Decoder:
        eof = True
        unconsumed_tail = b""
        unused_data = b""

        def decompress(self, _payload: bytes, max_length: int) -> bytes:
            assert max_length == 5
            return b"xxxxx"

        def flush(self, *_args) -> bytes:
            raise AssertionError("cache decoding must not use unbounded flush")

    monkeypatch.setattr(archive_route_module.zlib, "decompressobj", Decoder)
    with pytest.raises(archive_route_module._ArchiveCacheInvalid):
        archive_route_module._cached_archive_text(
            cast(
                sqlite3.Row,
                {
                    "file_key": "archive:overlimit",
                    "status": "indexed",
                    "text_zlib": b"fixture",
                    "text_chars": 1,
                    "text_xxh3_128": "0" * 32,
                },
            ),
            max_text_chars=1,
        )
