"""Functional cache-repair coverage for Archive and Code owners."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import neocortex.capabilities.formats.archive.route as archive_route_module
import neocortex.code.code_route as code_route_module
import neocortex.code.code_state as code_state_module
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.capabilities.formats.archive.state import search_archive_state
from neocortex.code.code_contracts import CodeRouteConfig, CodeSearchQuery
from neocortex.code.code_route import CodeRoute
from neocortex.code.code_state import CodeState
from neocortex.code.search.code_search import search_code
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


class _CodeInventory:
    def __init__(self, paths: Iterable[Path]):
        self.paths = tuple(paths)

    def snapshots(self, _scan_id: int) -> Iterable[FileSnapshot]:
        return iter(snapshot_path(path) for path in self.paths)


class _CodeFramework:
    def begin_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        _phase_name: str,
        *,
        source_run_id: int | None = None,
    ) -> None:
        del source_run_id

    def complete_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        _phase_name: str,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        assert summary is not None

    def fail_route_phase(
        self,
        _run_id: int,
        _route_name: str,
        _phase_name: str,
        _exc: BaseException,
    ) -> None:
        pass


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


def _code_config(root: Path) -> CodeRouteConfig:
    return CodeRouteConfig(
        state_path=root / "state" / "code.sqlite3",
        dedup_path=root / "state" / "dedup.sqlite3",
        max_file_bytes=1024 * 1024,
        max_text_chars=100_000,
        chunk_chars=1024,
    )


def _code_route(config: CodeRouteConfig, source: Path, run_id: int) -> CodeRoute:
    return CodeRoute(
        config,
        _CodeInventory((source,)),
        _CodeFramework(),
        run_id,
        run_id,
        cancellation=CancellationToken(),
    )


def test_code_cache_rejects_impossible_text_size_before_decompression(tmp_path: Path, monkeypatch):
    source = tmp_path / "small.py"
    source.write_text("def bounded_cache():\n    return 1\n")
    config = _code_config(tmp_path)
    assert _code_route(config, source, 1).run().processed == 1
    with CodeState(config.state_path) as state:
        version_id = state.connection.execute("SELECT current_version_id FROM files").fetchone()[0]
        state.connection.execute("UPDATE file_versions SET text_chars=1000000000 WHERE version_id=?", (version_id,))
        state.connection.commit()
        monkeypatch.setattr(code_state_module.zlib, "decompressobj", lambda: pytest.fail("invalid length must be rejected before allocation"))
        assert state._cached_code_fts_rows(version_id) is None


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


def test_code_cache_rebuilds_fts_from_durable_chunks_without_reanalysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fixture.py"
    source.write_text(
        "def cache_repair_token():\n    return 'durable code token'\n",
        encoding="utf-8",
    )
    config = _code_config(tmp_path)

    first = _code_route(config, source, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        chunk_count = int(connection.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0])
        connection.execute("DELETE FROM code_fts")
        connection.commit()

    def fail_analysis(*_args, **_kwargs):
        raise AssertionError("cache replay must not analyze source code")

    monkeypatch.setattr(code_route_module.CodeRoute, "_analyze_bytes", fail_analysis)
    second = _code_route(config, source, 2).run()

    assert first.processed == 1
    assert second.cache_hits == 1
    assert second.processed == 0
    assert second.fts_rows_repaired == chunk_count
    hits = search_code(
        config.state_path,
        CodeSearchQuery(text="durable code token", modes=("fts",), limit=5),
    )
    assert hits and hits[0].path == str(source)


@pytest.mark.parametrize("drop_index", (0, 1, 2))
def test_code_cache_rejects_missing_first_middle_or_last_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drop_index: int,
) -> None:
    source = tmp_path / "chunk-gap.py"
    source.write_text(
        "VALUE = '" + "x" * 900 + " cache_chunk_marker " + "x" * 2200 + "'\n",
        encoding="utf-8",
    )
    config = _code_config(tmp_path)
    first = _code_route(config, source, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        chunks = connection.execute(
            "SELECT chunk_id FROM code_chunks ORDER BY chunk_index"
        ).fetchall()
        assert len(chunks) >= 3
        connection.execute("DELETE FROM code_chunks WHERE chunk_id=?", (chunks[drop_index][0],))
        connection.execute("DELETE FROM code_fts")
        connection.commit()

    original_analyze = code_route_module.CodeRoute._analyze_bytes
    calls = 0

    def counted_analyze(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_analyze(*args, **kwargs)

    monkeypatch.setattr(code_route_module.CodeRoute, "_analyze_bytes", counted_analyze)
    second = _code_route(config, source, 2).run()

    assert first.processed == 1
    assert calls == 1
    assert second.cache_hits == 0
    assert second.processed == 1
    assert second.fts_rows_repaired == 0
    assert search_code(
        config.state_path,
        CodeSearchQuery(text="cache_chunk_marker", modes=("fts",), limit=5),
    )


@pytest.mark.parametrize("drop_index", (1, 2))
def test_legacy_code_cache_proves_complete_extent_before_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drop_index: int,
) -> None:
    source = tmp_path / "legacy-chunk-gap.py"
    source.write_text(
        "VALUE = '" + "x" * 900 + " legacy_chunk_marker " + "x" * 2200 + "'\n",
        encoding="utf-8",
    )
    config = _code_config(tmp_path)
    first = _code_route(config, source, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        chunks = connection.execute(
            "SELECT chunk_id FROM code_chunks ORDER BY chunk_index"
        ).fetchall()
        assert len(chunks) >= 3
        # Remove only the new cardinality marker: this is a pre-marker owner,
        # while its complete chunks and FTS remain valid for the next replay.
        connection.execute(
            "UPDATE file_versions SET provenance_json=? WHERE invalidated_ns IS NULL",
            ('{"legacy":true}',),
        )
        connection.commit()

    def fail_analysis(*_args, **_kwargs):
        raise AssertionError("a complete legacy cache must remain reusable")

    with monkeypatch.context() as legacy_patch:
        legacy_patch.setattr(code_route_module.CodeRoute, "_analyze_bytes", fail_analysis)
        intact = _code_route(config, source, 2).run()

    with sqlite3.connect(config.state_path) as connection:
        chunk_id = chunks[drop_index][0]
        connection.execute("DELETE FROM code_chunks WHERE chunk_id=?", (chunk_id,))
        # Keep the surviving FTS rows: their internal set is consistent, but
        # canonical extent proof must still detect the missing chunk.
        connection.execute("DELETE FROM code_fts WHERE chunk_id=?", (chunk_id,))
        connection.commit()

    original_analyze = code_route_module.CodeRoute._analyze_bytes
    calls = 0

    def counted_analyze(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_analyze(*args, **kwargs)

    monkeypatch.setattr(code_route_module.CodeRoute, "_analyze_bytes", counted_analyze)
    repaired = _code_route(config, source, 3).run()

    assert first.processed == 1
    assert intact.cache_hits == 1
    assert intact.processed == 0
    assert calls == 1
    assert repaired.cache_hits == 0
    assert repaired.processed == 1
    assert search_code(
        config.state_path,
        CodeSearchQuery(text="legacy_chunk_marker", modes=("fts",), limit=5),
    )


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


def test_code_cache_rejects_corrupt_durable_representation_and_reprocesses(
    tmp_path: Path,
) -> None:
    source = tmp_path / "drift.py"
    source.write_text("def drift_token():\n    return 1\n", encoding="utf-8")
    config = _code_config(tmp_path)
    _code_route(config, source, 1).run()

    with sqlite3.connect(config.state_path) as connection:
        connection.execute(
            "UPDATE file_versions SET text_zlib=? WHERE invalidated_ns IS NULL",
            (b"not-a-zlib-representation",),
        )
        connection.commit()

    second = _code_route(config, source, 2).run()

    assert second.cache_hits == 0
    assert second.processed == 1
    assert second.fts_rows_repaired == 0


def test_code_recoverable_retry_flag_reprocesses_only_explicit_retry_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retry.py"
    source.write_text("def retry_token():\n    return 1\n", encoding="utf-8")
    config = _code_config(tmp_path)
    _code_route(config, source, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        connection.execute(
            """UPDATE file_versions SET analysis_status='error',
            provenance_json=? WHERE invalidated_ns IS NULL""",
            ('{"recommendation":"retry"}',),
        )
        connection.commit()

    cached = _code_route(config, source, 2).run()
    retried = _code_route(
        replace(config, retry_recoverable_errors=True),
        source,
        3,
    ).run()

    assert cached.cache_hits == 1
    assert cached.processed == 0
    assert retried.cache_hits == 0
    assert retried.processed == 1


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


def test_code_cache_decoder_rejects_overlimit_output_without_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bounded.py"
    source.write_text("def bounded_decoder():\n    return 1\n", encoding="utf-8")
    config = _code_config(tmp_path)
    _code_route(config, source, 1).run()

    class Decoder:
        eof = True
        unconsumed_tail = b""
        unused_data = b""

        def decompress(self, _payload: bytes, _max_length: int) -> bytes:
            return b"xxxxx"

        def flush(self, *_args) -> bytes:
            raise AssertionError("cache decoding must not use unbounded flush")

    with CodeState(config.state_path) as state:
        version_id = int(
            state.connection.execute(
                "SELECT version_id FROM file_versions WHERE invalidated_ns IS NULL"
            ).fetchone()[0]
        )
        monkeypatch.setattr(code_state_module.zlib, "decompressobj", Decoder)
        assert state._cached_code_fts_rows(version_id) is None
