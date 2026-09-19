from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic import semantic_text_index as indexing
from neocortex.semantic.semantic_generation_worker import run_generation
from neocortex.semantic.semantic_state import start_embedding_generation
from neocortex.semantic.semantic_work_budget import SemanticWorkBudget
from tests.test_semantic_text_staging_session import (
    CHUNKING, _FixtureBackend, _generation, _model, _records, _stage,
)

TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


def _revision_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE embedding_generation_members(
            generation_id INTEGER,entity_kind TEXT,item_id TEXT,item_revision_id INTEGER);
        CREATE INDEX embedding_generation_members_item_revision_idx
          ON embedding_generation_members(generation_id,entity_kind,item_id,item_revision_id);
        CREATE TABLE semantic_item_revisions(
            item_revision_id INTEGER PRIMARY KEY,item_id TEXT,source_kind TEXT,
            source_identity TEXT,identity_version TEXT,path TEXT,
            content_xxh3_128 TEXT,content_bytes INTEGER,content_xxh3_64_guard TEXT,
            provenance_json TEXT,source_revision_json TEXT);
    """)
    return connection


def _revision(connection: sqlite3.Connection, revision_id: int, item_id: str,
              provenance: str = "{}") -> None:
    connection.execute("INSERT INTO semantic_item_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (revision_id, item_id, "pdf", item_id, "v1", "/fixture.pdf",
                        "a" * 32, 10, "b" * 16, provenance, "{}"))


class _Rows:
    def __init__(self, cursor, owner):
        self.cursor, self.owner = cursor, owner

    def __iter__(self):
        for row in self.cursor:
            self.owner.rows_transferred += 1
            yield row

    def fetchall(self):
        pytest.fail("revision loading must stream a bounded identity window")


class _ObservedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.rows_transferred = 0
        self.sql = ""
        self.parameters = ()

    def execute(self, sql, parameters):
        self.sql, self.parameters = sql, parameters
        return _Rows(self.connection.execute(sql, parameters), self)


@pytest.mark.parametrize("chunks", (400, 40000))
def test_revision_metadata_crosses_sqlite_once_per_item_revision_not_per_chunk(chunks: int) -> None:
    connection = _revision_database()
    try:
        _revision(connection, 1, "item-00001")
        connection.executemany("INSERT INTO embedding_generation_members VALUES(1,'text_chunk',?,1)",
                               (("item-00001",) for _ in range(chunks)))
        observed = _ObservedConnection(connection)
        keys = indexing._published_item_revision_keys(observed, 1, "pdf")
        assert tuple(keys) == ("item-00001",)
        assert observed.rows_transferred == 1
        plans = tuple(connection.execute("EXPLAIN QUERY PLAN " + observed.sql, observed.parameters))
        assert any("COVERING INDEX embedding_generation_members_item_revision_idx" in str(row[3])
                   for row in plans)
    finally:
        connection.close()


def test_revision_window_work_is_independent_of_unrelated_generation_members() -> None:
    connection = _revision_database()
    try:
        _revision(connection, 1, "item-00000")
        connection.executemany(
            "INSERT INTO embedding_generation_members VALUES(1,'text_chunk',?,1)",
            (("item-00000",) for _ in range(64)),
        )
        connection.executemany(
            "INSERT INTO embedding_generation_members VALUES(1,'text_chunk',?,1)",
            ((f"unrelated-{number:05d}",) for number in range(64_000)),
        )
        intervals = 0

        def observe_work() -> int:
            nonlocal intervals
            intervals += 1
            return 0

        connection.set_progress_handler(observe_work, 1000)
        keys = indexing._published_item_revision_keys(
            connection, 1, "pdf", first_item_id="item-00000", batch_size=1,
        )
        connection.set_progress_handler(None, 0)
        assert tuple(keys) == ("item-00000",)
        # Only the chosen item's 64 members may drive this lookup. A query
        # plan that scans all 64,000 unrelated members exceeds this margin.
        assert intervals < 20
    finally:
        connection.close()


@pytest.mark.parametrize("descending", (False, True))
def test_revision_windows_are_bounded_and_conflicting_historical_bindings_remain_unsafe(
    descending: bool,
) -> None:
    connection = _revision_database()
    try:
        for number in range(300):
            item_id = f"item-{number:05d}"
            _revision(connection, number + 1, item_id)
            connection.execute("INSERT INTO embedding_generation_members VALUES(1,'text_chunk',?,?)",
                               (item_id, number + 1))
        _revision(connection, 999, "item-00004", '{"different":true}')
        connection.execute("INSERT INTO embedding_generation_members VALUES(1,'text_chunk',?,999)",
                           ("item-00004",))
        keys = indexing._published_item_revision_keys(
            connection, 1, "pdf",
            first_item_id="item-00130" if descending else "item-00003",
            descending=descending,
        )
        assert len(keys) == 128
        assert keys["item-00004"] is None
        assert keys["item-00003"] is not None
        assert "item-00131" not in keys
    finally:
        connection.close()


@pytest.mark.parametrize("reverse", (False, True))
def test_incremental_replay_streams_source_order_and_commits_seen_items_in_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool,
) -> None:
    database = tmp_path / "semantic.sqlite3"
    baseline_id = _generation(database, "base-published")
    records = _records(260)
    assert _stage(database, baseline_id, records) == (260, 520, 520)
    assert run_generation(database, baseline_id, _FixtureBackend(_model()),
                          queued=520).summary.status == "ready"
    generation_id = start_embedding_generation(
        database, model_signature=_model().model_signature,
        processing_signature="changed-owner-head", materialize_base=False,
    )
    budget = SemanticWorkBudget(max_items=1, max_new_jobs=1)
    marked: set[str] = set()
    windows: list[int] = []
    commits = 0
    original_seen = indexing._SemanticTextStagingSession.mark_unchanged_item_seen
    original_lookup = indexing._published_item_revision_keys

    def seen(session, item, base_revision):
        result = original_seen(session, item, base_revision)
        assert result
        marked.add(item.item_id)
        return result

    def lookup(*args, **kwargs):
        result = original_lookup(*args, **kwargs)
        windows.append(len(result))
        return result

    def after_commit():
        nonlocal commits
        with sqlite3.connect(database, timeout=0) as another_owner:
            another_owner.execute("BEGIN IMMEDIATE")
            another_owner.rollback()
        commits += 1

    def source_records(_state, _source):
        for record in reversed(records) if reverse else records:
            yield record
            # Reading future groups cannot force previous text sections into
            # a preloaded batch before that item is processed by the owner.
            assert record.item.item_id in marked

    monkeypatch.setattr(indexing._SemanticTextStagingSession, "mark_unchanged_item_seen", seen)
    monkeypatch.setattr(indexing, "_published_item_revision_keys", lookup)
    monkeypatch.setattr(indexing, "staging_commit_checkpoint", after_commit)
    result = indexing._stage_source(
        database, database.parent, "pdf", generation_id=generation_id,
        base_generation_id=baseline_id, refresh_token="incremental-refresh",
        chunking=CHUNKING, source_record_iterator=source_records, work_budget=budget,
    )
    assert result == (0, 0, 0, True)
    assert len(marked) == 260
    assert all(size <= 128 for size in windows)
    # The first item has no observed direction. The second window follows the
    # actual traversal and amortizes reverse order over bounded slices too.
    assert len(windows) == (4 if reverse else 3)
    assert commits >= 3
    assert budget.items_admitted == budget.new_jobs_admitted == 0
