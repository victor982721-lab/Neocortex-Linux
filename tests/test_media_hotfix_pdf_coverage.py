"""Proposed read-only coverage regressions; run locally before accepting hotfix."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.capabilities.formats.pdf import pdf_derived
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state


TEST_CAPABILITIES = ("documents",)

_REFERENCE_MISSING = """SELECT COUNT(*) FROM pages p JOIN documents d USING(file_key)
WHERE d.status IN ('done','partial') AND d.last_seen_run_id=?
AND (NOT EXISTS(SELECT 1 FROM page_fts_state s
    WHERE s.file_key=p.file_key AND s.page_number=p.page_number)
OR NOT EXISTS(SELECT 1 FROM page_fts f
    WHERE f.file_key=p.file_key AND f.page_number=p.page_number))"""


@pytest.fixture
def coverage_db(tmp_path, monkeypatch):
    path = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(path)
    connection = sqlite3.connect(path)

    @contextmanager
    def readonly_database(_path: Path, *, readonly: bool = False):
        assert readonly is True
        connection.execute("PRAGMA query_only=ON")
        yield connection
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1

    monkeypatch.setattr(pdf_derived, "pdf_database", readonly_database)
    try:
        yield connection, path
    finally:
        connection.close()


def _document(connection, key: str, *, run_id=7, status="done"):
    connection.execute(
        """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
        processing_signature,status,last_seen_run_id,updated_ns)
        VALUES(?,?,1,1,1,'fixture',?,?,1)""",
        (key, key + ".pdf", status, run_id),
    )


def _page(connection, key: str, page_number: int, *, state=True):
    connection.execute(
        """INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars)
        VALUES(?,?,'native',X'789C030000000001',0)""",
        (key, page_number),
    )
    if state:
        connection.execute("INSERT INTO page_fts_state VALUES(?,?,'hash')", (key, page_number))


@pytest.mark.parametrize(
    ("values", "missing"),
    (
        ((), 1), ((1,), 0), ((1, 1), 0), (("1",), 0), (("01",), 0),
        ((1.0,), 0), (("1.0",), 0), (("1x",), 1), ((1.5,), 1),
        (("1.5",), 1), ((b"1",), 1), ((None,), 1),
        (("1x", 1), 0), ((1, 2, "1x", b"1"), 0),
    ),
)
@pytest.mark.parametrize("state", (True, False))
def test_coverage_preserves_unindexed_fts_type_comparisons(coverage_db, values, missing, state):
    connection, path = coverage_db
    _document(connection, "doc")
    _page(connection, "doc", 1, state=state)
    connection.executemany(
        "INSERT INTO page_fts(file_key,path,page_number,text) VALUES('doc','doc.pdf',?,'text')",
        ((value,) for value in values),
    )
    connection.commit()
    expected = connection.execute(_REFERENCE_MISSING, (7,)).fetchone()[0]
    assert expected == (missing if state else 1)
    before = connection.total_changes
    actual = pdf_derived.inspect_pdf_derived_coverage(path, 7)
    assert actual.missing_fts_pages == expected
    assert connection.total_changes == before


def test_coverage_preserves_run_status_key_identity_and_orphans(coverage_db):
    connection, path = coverage_db
    for key, status, run_id in (("doc", "done", 7), ("DOC", "partial", 7),
                                ("older", "done", 8), ("error", "error", 7)):
        _document(connection, key, status=status, run_id=run_id)
        _page(connection, key, 1)
    connection.executemany(
        "INSERT INTO page_fts(file_key,path,page_number,text) VALUES(?,'fixture.pdf',1,'text')",
        (("doc",), (b"DOC",), ("older",), ("error",), ("orphan",)),
    )
    connection.execute("INSERT INTO page_fts_state VALUES('orphan',1,'hash')")
    connection.commit()
    expected = connection.execute(_REFERENCE_MISSING, (7,)).fetchone()[0]
    actual = pdf_derived.inspect_pdf_derived_coverage(path, 7)
    assert expected == actual.missing_fts_pages == 1
    assert actual.documents == actual.pages == 2
    assert actual.orphan_fts_rows == 1
    assert pdf_derived.inspect_pdf_derived_coverage(path, 999).missing_fts_pages == 0


def test_coverage_has_no_per_page_full_fts_rescan(coverage_db):
    connection, path = coverage_db
    _document(connection, "doc")
    for page in range(512):
        _page(connection, "doc", page)
        connection.execute(
            "INSERT INTO page_fts(file_key,path,page_number,text) VALUES('doc','doc.pdf',?,'x')",
            (page,),
        )
    connection.commit()
    steps = 0

    def progress():
        nonlocal steps
        steps += 1000
        return 0

    connection.set_progress_handler(progress, 1000)
    try:
        actual = pdf_derived.inspect_pdf_derived_coverage(path, 7)
    finally:
        connection.set_progress_handler(None, 0)
    assert actual.missing_fts_pages == 0
    # Work bound, not a wall-clock assertion.  The original correlated scan
    # exceeds two million VM operations for this fixture; leave ample margin
    # for supported SQLite versions and the other coverage queries.
    assert steps < 750_000
