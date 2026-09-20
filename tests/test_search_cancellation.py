"""Cooperative cancellation and owner isolation for read-only search APIs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.semantic.semantic_lexical import (
    LexicalAvailability,
    LexicalStatePaths,
    search_lexical_source,
    search_lexical_sources,
)


# region [01] Bounded owner fixtures


def _create_pdf_state(path: Path, *, rows: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                is_partial INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE page_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                page_number UNINDEXED,
                text,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        documents = (
            (
                f"pdf-{index}",
                f"C:/docs/protection-{index}.pdf",
                "done",
                0,
                100 + index,
                20 + index,
                10 + index,
                "pdf-cancellation-v1",
                1,
            )
            for index in range(rows)
        )
        pages = (
            (
                f"pdf-{index}",
                f"C:/docs/protection-{index}.pdf",
                index + 1,
                f"protection breaker cancellation fixture {index}",
            )
            for index in range(rows)
        )
        connection.executemany(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
            documents,
        )
        connection.executemany("INSERT INTO page_fts VALUES(?,?,?,?)", pages)


def _create_docx_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents(
                file_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                birthtime_ns INTEGER NOT NULL,
                processing_signature TEXT NOT NULL,
                last_seen_run_id INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE document_fts USING fts5(
                file_key UNINDEXED,
                path UNINDEXED,
                title,
                author,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO documents VALUES(
                'docx-1','C:/docs/protection.docx','complete',200,30,11,
                'docx-cancellation-v1',1
            );
            INSERT INTO document_fts VALUES(
                'docx-1','C:/docs/protection.docx','Protection study','Victor',
                'protection breaker healthy owner'
            );
            """
        )




# endregion [01]


# region [02] Owner isolation and pre-owner checkpoints


def test_lexical_sources_isolate_broken_owner_and_preserve_healthy_owner(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "corrupt-pdf.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    docx = tmp_path / "healthy-docx.sqlite3"
    _create_docx_state(docx)

    rankings = search_lexical_sources(
        LexicalStatePaths(pdf=corrupt, docx=docx),
        "protection breaker",
    )

    assert rankings[0].availability is LexicalAvailability.READ_FAILED
    assert rankings[0].unavailable_reason == (
        "state_database_read_failed:DatabaseError"
    )
    assert rankings[0].hits == ()
    assert rankings[1].availability is LexicalAvailability.AVAILABLE
    assert len(rankings[1].hits) == 1


def test_lexical_owner_isolation_never_swallows_callback_exception(
    tmp_path: Path,
) -> None:
    class OwnerCancellation(RuntimeError):
        pass

    state = tmp_path / "unused.sqlite3"
    cancellation = OwnerCancellation("cancel before lexical owner")

    def cancel() -> None:
        raise cancellation

    with pytest.raises(OwnerCancellation) as raised:
        search_lexical_sources(
            LexicalStatePaths(pdf=state),
            "protection",
            cancellation_check=cancel,
        )

    assert raised.value is cancellation
    assert not state.exists()




# endregion [02]


# region [03] SQLite progress interruption


def test_lexical_sqlite_progress_rethrows_original_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    state = tmp_path / "large-pdf.sqlite3"
    _create_pdf_state(state, rows=5_000)
    calls = 0
    cancellation = KeyboardInterrupt("cancel inside lexical SQLite")

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        search_lexical_source(
            "pdf",
            state,
            "protection",
            limit=1_000,
            cancellation_check=cancel,
        )

    assert raised.value is cancellation
    assert calls >= 2
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)




# endregion [03]
