"""Proposed replay parity tests for the partial memory reduction."""

from __future__ import annotations

import sqlite3
import zlib

import pytest

from neocortex.capabilities.formats.audio.models import MediaProbe, TranscriptResult, TranscriptSegment
from neocortex.capabilities.formats.audio.route import (
    _read_cached_audio_segments, _repair_cached_audio_derivatives, _store_success,
)
from neocortex.capabilities.formats.audio.state import _create_audio_schema
from neocortex.capabilities.formats.fts_lookup import initialize_format_fts_lookup
from neocortex.deduplication import FileSnapshot


class _Cursor(sqlite3.Cursor):
    statement = ""
    was_closed = False

    def close(self):
        self.was_closed = True
        return super().close()

    def fetchall(self):
        if "FROM segments" in self.statement:
            raise AssertionError("segment validation must not retain all SQLite rows")
        return super().fetchall()


class _Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        cursor = self.cursor(factory=_Cursor)
        cursor.statement = sql
        if "FROM segments" in sql:
            self.last_segment_cursor = cursor
        return cursor.execute(sql, parameters)


@pytest.fixture
def audio_cache(tmp_path):
    connection = sqlite3.connect(":memory:", factory=_Connection)
    connection.row_factory = sqlite3.Row
    _create_audio_schema(connection)
    snapshot = FileSnapshot(str(tmp_path / "clip.ogg"), 1, 1, 1, 1, 1)
    segments = tuple(
        TranscriptSegment(i, i * 1000, (i + 1) * 1000, word, None, None)
        for i, word in enumerate(("hola", "文😀", "fin"))
    )
    transcript = TranscriptResult(
        "hola 文😀 fin", "es", 1.0, 3.0, 3.0, segments,
        "fixture", "fixture", "cpu", "int8",
    )
    _store_success(connection, snapshot, "audio/ogg", "fixture",
                   MediaProbe(3, "ogg", "opus", 48_000, 1, 1, 0), transcript, 1)
    connection.commit()
    try:
        yield connection, snapshot, transcript.text
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("damage", "accepted"),
    ((None, True), ("blob_missing", True), ("blob_invalid", True),
     ("hash_invalid", True), ("fts_missing", True), ("fts_duplicates", True),
     ("valid_conflicting_blob", False), ("both_witnesses_missing", False),
     ("missing_segment", False), ("segment_hole", False),
     ("binary_segment", False), ("timing_damage", False)),
)
def test_replay_preserves_completeness_witnesses_and_derivative_repair(audio_cache, damage, accepted):
    connection, snapshot, expected = audio_cache
    if damage == "blob_missing":
        connection.execute("UPDATE documents SET text_zlib=NULL")
    elif damage == "blob_invalid":
        connection.execute("UPDATE documents SET text_zlib=X'00'")
    elif damage == "hash_invalid":
        connection.execute("UPDATE documents SET text_xxh3_128='wrong'")
    elif damage == "fts_missing":
        connection.execute("DELETE FROM transcript_fts")
    elif damage == "fts_duplicates":
        connection.execute("INSERT INTO transcript_fts SELECT * FROM transcript_fts")
        connection.execute("INSERT INTO transcript_fts SELECT * FROM transcript_fts")
    elif damage == "valid_conflicting_blob":
        connection.execute("UPDATE documents SET text_zlib=?", (zlib.compress(b"longer witness"),))
    elif damage == "both_witnesses_missing":
        connection.execute("UPDATE documents SET text_zlib=NULL,text_xxh3_128=NULL")
    elif damage == "missing_segment":
        connection.execute("DELETE FROM segments WHERE segment_index=2")
    elif damage == "segment_hole":
        connection.execute("UPDATE segments SET segment_index=4 WHERE segment_index=2")
    elif damage == "binary_segment":
        connection.execute("UPDATE segments SET text=X'00' WHERE segment_index=1")
    elif damage == "timing_damage":
        connection.execute("UPDATE documents SET speech_duration_seconds=2.0")
    connection.commit()
    initialize_format_fts_lookup(connection, "transcript_fts")
    before = connection.total_changes
    assert _repair_cached_audio_derivatives(connection, snapshot) is accepted
    if accepted:
        document = connection.execute("SELECT text_zlib,segment_count FROM documents").fetchone()
        assert zlib.decompress(document["text_zlib"]).decode("utf-8") == expected
        assert document["segment_count"] == 3
        assert [row[0] for row in connection.execute("SELECT body FROM transcript_fts")] == [expected]
    else:
        assert connection.total_changes == before


def test_segment_cursor_closes_on_invalid_rows_and_preserves_separator_policy(audio_cache):
    connection, _snapshot, _text = audio_cache
    key = connection.execute("SELECT file_key FROM documents").fetchone()[0]
    metadata = connection.execute("SELECT media_metadata_json FROM documents").fetchone()[0]
    connection.execute("UPDATE segments SET text='' WHERE segment_index=1")
    assert _read_cached_audio_segments(connection, key, metadata) == ("hola  fin", 3.0, 3)
    assert connection.last_segment_cursor.was_closed
    connection.execute("UPDATE segments SET end_ms=-1 WHERE segment_index=1")
    assert _read_cached_audio_segments(connection, key, metadata) is None
    assert connection.last_segment_cursor.was_closed
    # A subsequent write/read must work after an early validation return.
    connection.execute("DELETE FROM segments")
    assert _read_cached_audio_segments(connection, key, metadata) is None
