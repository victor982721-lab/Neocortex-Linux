"""Classification input limits include separators and cancellable empty rows."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import zlib

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.documents import document_catalog_text as text_reader
from neocortex.documents import document_catalog_workers as workers
from neocortex.documents import document_taxonomy as taxonomy
from neocortex.documents.document_catalog_models import SourceDocument, SourceKind
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from tests.test_catalog_binding_write_amplification import _source


def _source_rows(connection: sqlite3.Connection, kind: SourceKind, texts: list[str]) -> SourceDocument:
    connection.row_factory = sqlite3.Row
    if kind == "pdf":
        connection.execute("CREATE TABLE pages(file_key TEXT,page_number INTEGER,text_zlib BLOB,PRIMARY KEY(file_key,page_number)) WITHOUT ROWID")
        connection.executemany("INSERT INTO pages VALUES('fixture',?,?)", ((index, zlib.compress(text.encode())) for index, text in enumerate(texts)))
    elif kind == "video":
        connection.execute("CREATE TABLE frame_fts(file_key TEXT,timestamp_ms INTEGER,body TEXT)")
        connection.executemany("INSERT INTO frame_fts VALUES('fixture',?,?)", enumerate(texts))
    else:
        raise AssertionError(f"unsupported fixture source kind: {kind}")
    connection.commit()
    return SourceDocument(kind, "fixture", "/fixture", "1", "2", 1, 1, -1, "complete", "fixture", "text", "", "", "")


@pytest.mark.parametrize("kind", ("pdf", "video"))
@pytest.mark.parametrize("limit", (1, 2, 3, 4, 7, 12, 32, 129))
def test_source_prefix_limit_counts_unicode_characters_and_separators(kind: SourceKind, limit: int) -> None:
    pieces = ["", "", "🧠á", "", "e\u0301z", "tail" * 40]
    expected = "\n".join(pieces if kind == "pdf" else [piece for piece in pieces if piece])[:limit]
    with closing(sqlite3.connect(":memory:")) as connection:
        document = _source_rows(connection, kind, pieces)
        result = text_reader._load_leading_text(connection, document, max_text_chars=limit)
    assert result == expected
    assert len(result) <= limit


def test_many_empty_pdf_pages_stop_when_the_output_prefix_is_complete() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        document = _source_rows(connection, "pdf", [""] * 20_000)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        result = text_reader._load_leading_text(connection, document, max_text_chars=64)
        connection.set_trace_callback(None)
        assert result == "\n" * 64
        assert sum("substr(text_zlib" in sql for sql in statements) <= 65


@pytest.mark.parametrize("kind", ("pdf", "video"))
@pytest.mark.parametrize("encoding", ("UTF-8", "UTF-16le", "UTF-16be"))
@pytest.mark.parametrize("limit", (1, 4, 12, 64))
def test_source_prefix_preserves_nul_and_database_encoding(kind: SourceKind, encoding: str, limit: int) -> None:
    for pieces in (["before\0IEC 61850", "tail"], ["\0negative claim", "🧠positive"], ["", "🧠\0é", "x"]):
        expected = "\n".join(pieces if kind == "pdf" else [piece for piece in pieces if piece])[:limit]
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(f"PRAGMA encoding='{encoding}'")
            document = _source_rows(connection, kind, pieces)
            assert text_reader._load_leading_text(connection, document, max_text_chars=limit) == expected


@pytest.mark.parametrize("kind", ("video",))
@pytest.mark.parametrize("invalid", (b"\xff", b"text\xf0", b"\0\xff"))
def test_invalid_stored_text_prefix_is_not_silently_discarded(kind: SourceKind, invalid: bytes) -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        document = _source_rows(connection, kind, ["placeholder"])
        if kind == "video":
            connection.execute("UPDATE frame_fts SET body=CAST(? AS TEXT)", (invalid,))
        with pytest.raises(UnicodeDecodeError):
            text_reader._load_leading_text(connection, document, max_text_chars=64)


@pytest.mark.parametrize("kind", ("video",))
@pytest.mark.parametrize("encoding", ("UTF-8", "UTF-16le", "UTF-16be"))
def test_text_prefix_transfer_is_bounded_at_multibyte_boundary(kind: SourceKind, encoding: str) -> None:
    text = "x\0" + "🧠" * 10_000
    transferred: list[int] = []

    def observe_row(cursor, values):
        transferred.extend(len(value) for value in values if isinstance(value, bytes))
        return sqlite3.Row(cursor, values)

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(f"PRAGMA encoding='{encoding}'")
        document = _source_rows(connection, kind, [text])
        connection.row_factory = observe_row
        result = text_reader._load_leading_text(connection, document, max_text_chars=4)
    assert result == text[:4]
    assert transferred == [20]


@pytest.mark.parametrize("kind", ("video",))
def test_empty_source_rows_still_poll_worker_cancellation(
    monkeypatch: pytest.MonkeyPatch, kind: SourceKind,
) -> None:
    class CancelDuringRows(CancellationToken):
        def __init__(self):
            super().__init__()
            self.checks = 0

        def checkpoint(self):
            self.checks += 1
            if self.checks == 16:
                self.cancel()
            super().checkpoint()

    token = CancelDuringRows()
    monkeypatch.setattr("neocortex.runtime.control.elastic_workers.current_worker_cancellation", lambda: token)
    with closing(sqlite3.connect(":memory:")) as connection:
        document = _source_rows(connection, kind, [""] * 20_000)
        with pytest.raises(CancellationRequested):
            text_reader._load_leading_text(connection, document, max_text_chars=64)
    assert token.checks == 16


def test_worker_cancellation_interrupts_sql_before_sorted_rows_are_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    document = _source_rows(connection, "video", [""] * 20_000)
    row_returned = False
    before_first_row = []

    class ObservedConnection:
        def execute(self, *args, **kwargs):
            nonlocal row_returned
            result = connection.execute(*args, **kwargs)
            if "FROM frame_fts" in args[0]:
                row_returned = True
            return result

        def set_progress_handler(self, *args):
            return connection.set_progress_handler(*args)

        def close(self):
            connection.close()

    class CancelDuringSql(CancellationToken):
        def checkpoint(self):
            before_first_row.append(not row_returned)
            if len(before_first_row) == 8:
                self.cancel()
            super().checkpoint()

    token = CancelDuringSql()
    monkeypatch.setattr("neocortex.runtime.control.elastic_workers.current_worker_cancellation", lambda: token)
    monkeypatch.setattr("neocortex.persistence.sqlite_immutable.open_immutable_sqlite_connection", lambda *_args, **_kwargs: ObservedConnection())
    current_taxonomy = catalog.load_taxonomy()
    task = workers.CatalogClassificationTask(
        tmp_path / "caller-owned-source.sqlite3", document, current_taxonomy, 64,
        taxonomy.document_classifier_signature(current_taxonomy),
    )
    with pytest.raises(CancellationRequested):
        workers.classify_catalog_task(task)
    assert all(before_first_row)
    assert not row_returned


def test_text_policy_version_rebuilds_old_cache_once_then_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    version = taxonomy.CLASSIFIER_VERSION
    assert version == "technical-document-classifier-v17"
    with monkeypatch.context() as old:
        old.setattr(taxonomy, "CLASSIFIER_VERSION", "technical-document-classifier-v16")
        initial = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
        old_replay = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    assert initial.classified == 1 and old_replay.publication_state == "unchanged"
    updated = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    assert updated.classified == 1 and updated.cache_hits == 0
    assert updated.generation_id != initial.generation_id
    replay = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    assert replay.classified == 0 and replay.cache_hits == 1
    assert replay.publication_state == "unchanged" and replay.generation_id == updated.generation_id
    with catalog.document_catalog_database(target, readonly=True) as connection:
        signature = connection.execute("SELECT classifier_signature FROM documents").fetchone()[0]
    assert signature.startswith(version + "|")
