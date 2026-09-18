"""A Text replay reuses one validated payload across explicit commit fences."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.capabilities.formats.text import text_route as route_module
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationIntegrityError,
    read_reusable_text_derivation_from_connection,
)
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.capabilities.formats.text.text_state import text_database
from neocortex.deduplication import snapshot_path
from tests.test_text_route_cache_replay_counters import _Candidates


def _fixture(tmp_path: Path):
    source = tmp_path / "text.txt"
    source.write_text("Texto y evidencia inmutable.\n" * 2000, encoding="utf-8")
    config = TextRouteConfig(state_path=tmp_path / "text.sqlite3")
    candidates = _Candidates({"text/plain": (snapshot_path(source),)})
    return source, config, candidates


def test_replay_decompresses_once_and_keeps_current_source_identity(tmp_path: Path, monkeypatch) -> None:
    source, config, candidates = _fixture(tmp_path)
    first = TextRoute(config, candidates, 1).run()
    reads = []
    decodes = []
    original_read = route_module._read_exact
    original_decode = route_module.zlib.decompress

    def read(*args, **kwargs):
        payload = original_read(*args, **kwargs)
        reads.append(len(payload))
        return payload

    def decode(*args, **kwargs):
        decodes.append(1)
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(route_module, "_read_exact", read)
    monkeypatch.setattr(route_module.zlib, "decompress", decode)
    replay = TextRoute(config, candidates, 2).run()
    assert replay.cache_hits == 1 and replay.extracted == 0
    assert (replay.text_chars, replay.plain_text) == (first.text_chars, first.plain_text)
    assert reads == [source.stat().st_size]
    assert len(decodes) == 1


@pytest.mark.parametrize("other_connection", (False, True))
@pytest.mark.parametrize("before_begin", (False, True))
def test_mutation_after_attempt_commit_cannot_publish_observed_payload(
    tmp_path: Path, monkeypatch, other_connection: bool, before_begin: bool,
) -> None:
    _source, config, candidates = _fixture(tmp_path)
    TextRoute(config, candidates, 1).run()
    original_begin = TextRoute._begin_derivation

    def begin(self, connection, *args, **kwargs):
        work = None if before_begin else original_begin(self, connection, *args, **kwargs)
        if kwargs.get("causation_id") is not None:
            target = sqlite3.connect(config.state_path) if other_connection else connection
            try:
                target.execute("UPDATE document_fts SET body='changed between commits'")
                target.commit()
            finally:
                if other_connection:
                    target.close()
        return original_begin(self, connection, *args, **kwargs) if before_begin else work

    monkeypatch.setattr(TextRoute, "_begin_derivation", begin)
    with pytest.raises(TextDerivationIntegrityError, match="changed after"):
        TextRoute(config, candidates, 2).run()
    with text_database(config.state_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM text_work_receipts").fetchone()[0] == 1
    monkeypatch.setattr(TextRoute, "_begin_derivation", original_begin)
    repaired = TextRoute(config, candidates, 3).run()
    assert repaired.cache_hits == 0 and repaired.extracted == 1


def test_read_with_uncommitted_rows_cannot_grant_a_replay_observation(tmp_path: Path) -> None:
    _source, config, candidates = _fixture(tmp_path)
    TextRoute(config, candidates, 1).run()
    with text_database(config.state_path) as conn:
        row = conn.execute("SELECT file_key,processing_signature FROM documents").fetchone()
        conn.execute("BEGIN")
        reusable = read_reusable_text_derivation_from_connection(
            conn, str(row["file_key"]), stage_id="text.extract",
            processing_signature=str(row["processing_signature"]),
        )
        assert reusable is not None and reusable.representation is not None
        assert reusable.observation is None
        conn.rollback()
