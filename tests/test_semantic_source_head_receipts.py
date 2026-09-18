"""Public replays reuse fenced heads and compatibility evidence across calls."""
from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
from contextlib import closing

import pytest

from neocortex.semantic import semantic_service as service
from neocortex.semantic import semantic_sources as sources
from tests.test_semantic_service import _declare_source_state, _patch_backend, _text_records
from tests.test_semantic_text_source_delta import _declare_text_state, _text_compatible_record


def test_public_exact_replays_do_not_reproject_unchanged_source_heads(tmp_path, monkeypatch):
    _declare_source_state(tmp_path, "pdf")
    records = _text_records(2)
    with closing(sqlite3.connect(tmp_path / "pdf.sqlite3")) as connection, connection:
        for record in records:
            connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                record.item.source_identity, record.item.path, "fixture", "done", 10, 20, 30,
                1, 0, record.item.fingerprint.xxh3_128, len(record.section.text)))
            connection.execute("INSERT INTO pages VALUES(?,?,?,?,?)", (
                record.item.source_identity, 1, "text", b"fixture", len(record.section.text)))
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter(records))
    first = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
    assert first.complete
    def forbidden(*args, **kwargs):
        pytest.fail("unchanged replay must not reproject heads, load model or enumerate records")
    monkeypatch.setattr(sources, "_text_source_head", forbidden)
    monkeypatch.setattr(service, "_backend", forbidden)
    monkeypatch.setattr(service, "iter_text_source_records", forbidden)
    for _ in range(2):
        replay = service.index_text_embeddings(tmp_path, source_kinds=("pdf",))
        assert replay.execution_mode == "exact_replay"
        assert replay.generations[0].summary.generation_id == first.generations[0].summary.generation_id


def test_source_fence_drift_invalidates_persisted_head_receipt(tmp_path, monkeypatch):
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    service.index_text_embeddings(tmp_path, source_kinds=("text",))
    original = sources._text_source_head
    projections = []
    def project(*args, **kwargs):
        projections.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(sources, "_text_source_head", project)
    with closing(sqlite3.connect(tmp_path / "text.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET processing_signature='route-v2'")
    service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert projections == [1]


def test_compatible_replay_receipt_avoids_repeated_full_comparison(tmp_path, monkeypatch):
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    with closing(sqlite3.connect(tmp_path / "text.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET processing_signature='route-v2'")
    original = service._text_index._source_head_query
    comparisons = []
    def source_query(*args, **kwargs):
        comparisons.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service._text_index, "_source_head_query", source_query)
    for run in range(3):
        replay = service.index_text_embeddings(tmp_path, source_kinds=("text",))
        assert replay.execution_mode == "content_compatible_replay"
        assert replay.generations[0].summary.generation_id == baseline.generations[0].summary.generation_id
        assert len(comparisons) == 1, f"comparison repeated on run {run}"


def test_compatible_receipt_rejects_changed_semantic_members(tmp_path, monkeypatch):
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    generation = baseline.generations[0].summary.generation_id
    with closing(sqlite3.connect(tmp_path / "text.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET processing_signature='route-v2'")
    assert service.index_text_embeddings(tmp_path, source_kinds=("text",)).execution_mode == "content_compatible_replay"
    with closing(sqlite3.connect(tmp_path / "semantic.sqlite3")) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        removed = connection.execute("DELETE FROM embedding_generation_members WHERE generation_id=?", (generation,)).rowcount
        assert removed > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    repaired = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert repaired.execution_mode == "enumerated"
    assert repaired.generations[0].summary.generation_id != generation


@pytest.mark.parametrize("damage", ("corrupt", "missing", "symlink", "oversized"))
def test_invalid_sidecar_is_a_bounded_cache_miss(tmp_path, monkeypatch, damage):
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    service.index_text_embeddings(tmp_path, source_kinds=("text",))
    receipt = tmp_path / "semantic.sqlite3.replay-receipts.json"
    assert receipt.is_file()
    external = tmp_path / "external.json"
    if damage == "corrupt":
        receipt.write_text('{"payload":null}', encoding="utf-8")
    elif damage == "missing":
        receipt.unlink()
    elif damage == "oversized":
        receipt.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    else:
        external.write_text("external", encoding="utf-8")
        receipt.unlink()
        receipt.symlink_to(external)
    projections = []
    original = sources._text_source_head
    def project(*args, **kwargs):
        projections.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(sources, "_text_source_head", project)
    replay = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert replay.complete
    assert replay.execution_mode == "exact_replay"
    assert projections == [1]
    if damage == "symlink":
        assert external.read_text(encoding="utf-8") == "external"
        assert receipt.is_symlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO contract requires POSIX")
def test_fifo_receipt_is_rejected_without_waiting_for_a_writer(tmp_path):
    database = tmp_path / "semantic.sqlite3"
    receipt = tmp_path / "semantic.sqlite3.replay-receipts.json"
    os.mkfifo(receipt)
    probe = subprocess.run([sys.executable, "-B", "-c",
        "from pathlib import Path; import sys; "
        "from neocortex.semantic.semantic_source_head_cache import _load_records; "
        "assert _load_records(Path(sys.argv[1])) == {}; print('cache_miss')", str(database)],
        capture_output=True, text=True, timeout=3, check=True)
    assert probe.stdout.strip() == "cache_miss"


def test_receipt_publication_does_not_certify_a_later_owner_fence(tmp_path, monkeypatch):
    from neocortex.semantic import semantic_source_head_cache as cache
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    baseline = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    generation = baseline.generations[0].summary.generation_id
    with closing(sqlite3.connect(tmp_path / "text.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET processing_signature='route-v2'")
    save = cache._save_records
    def change_before_saving(database, records):
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM embedding_generation_members WHERE generation_id=?", (generation,))
        save(database, records)
    monkeypatch.setattr(cache, "_save_records", change_before_saving)
    # Validation saw the intact generation. Publication must retain that
    # old fence; a concurrent later owner must never inherit its receipt.
    assert service.index_text_embeddings(tmp_path, source_kinds=("text",)).execution_mode == "content_compatible_replay"
    monkeypatch.setattr(cache, "_save_records", save)
    repaired = service.index_text_embeddings(tmp_path, source_kinds=("text",))
    assert repaired.execution_mode == "enumerated"
    assert repaired.generations[0].summary.generation_id != generation


def test_sidecar_write_failure_falls_back_to_revalidation(tmp_path, monkeypatch):
    from neocortex.semantic import semantic_source_head_cache as cache
    _declare_text_state(tmp_path)
    _patch_backend(monkeypatch)
    monkeypatch.setattr(service, "iter_text_source_records", lambda *args: iter((_text_compatible_record(),)))
    service.index_text_embeddings(tmp_path, source_kinds=("text",))
    receipt = tmp_path / "semantic.sqlite3.replay-receipts.json"
    before = receipt.read_bytes()
    with closing(sqlite3.connect(tmp_path / "text.sqlite3")) as connection, connection:
        connection.execute("UPDATE documents SET processing_signature='route-v2'")
    comparisons = []
    query = service._text_index._source_head_query
    def observe(*args, **kwargs):
        comparisons.append(1)
        return query(*args, **kwargs)
    replace = cache.os.replace
    def fail_receipt(source, destination, **kwargs):
        if str(destination).endswith(cache.REPLAY_RECEIPT_SUFFIX):
            raise OSError("fixture receipt publication failed")
        return replace(source, destination, **kwargs)
    monkeypatch.setattr(service._text_index, "_source_head_query", observe)
    monkeypatch.setattr(cache.os, "replace", fail_receipt)
    for _ in range(2):
        result = service.index_text_embeddings(tmp_path, source_kinds=("text",))
        assert result.complete
        assert result.execution_mode == "content_compatible_replay"
        assert receipt.read_bytes() == before
    assert len(comparisons) == 2
