"""Coverage from published pages, never from skipped historical attempts."""

import json
from pathlib import Path

import pytest

from neocortex.capabilities.formats.pdf.pdf_diagnostics import (
    list_pdf_diagnostics,
    read_pdf_coverage,
)
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state, pdf_database


def _pdf(
    path: Path,
    key: str,
    *,
    source: str,
    status: str = "done",
    total: int | None = 3,
    published: int = 3,
    recovered: bool = False,
) -> None:
    metadata = (
        {
            "neocortex_recovery": {
                "engine": "qpdf+pymupdf",
                "primary_error": 'PdfPageSequenceAborted: {"consecutive_errors":32,"last_attempted_page":37,"skipped_pages":799}',
            }
        }
        if recovered
        else {}
    )
    with pdf_database(path) as connection:
        connection.execute(
            """INSERT INTO documents(file_key,path,size,mtime_ns,processing_signature,status,
            page_count,completed_pages,page_start,page_end,metadata_json,updated_ns)
            VALUES(?,?,1,1,'fixture-v1',?,?,?,?,?,?,1)""",
            (key, source, status, total, published, 1, total, json.dumps(metadata)),
        )
        connection.execute(
            "INSERT INTO pdf_inventory(file_key,path,size,mtime_ns,last_seen_run_id) VALUES(?,?,1,1,1)",
            (key, source),
        )
        connection.executemany(
            "INSERT INTO pages(file_key,page_number,source,text_zlib,text_chars) VALUES(?,?,'native',X'00',?)",
            ((key, page, int(page != 0)) for page in range(published)),
        )


def test_recovered_pdf_final_pages_do_not_inherit_799_historical_gaps(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite"
    initialize_pdf_state(state)
    _pdf(state, "complete", source="/fixture/A/manual.pdf", recovered=True)
    _pdf(state, "partial", source="/fixture/A/partial.pdf", published=2, recovered=True)
    _pdf(
        state,
        "protected",
        source="/fixture/A/locked.pdf",
        status="protected",
        total=None,
        published=0,
    )
    items = {item["file_key"]: item for item in list_pdf_diagnostics(state).items}
    complete = items["complete"]
    assert complete["historical_attempts"][0]["skipped_pages"] == 799
    assert complete["historical_attempts"][0]["last_attempted_page"] == 37
    assert complete["final_coverage"]["status"] == "complete"
    assert complete["final_coverage"]["missing_pages"] == 0
    assert complete["final_coverage"]["pages_without_text"] == 1
    assert items["partial"]["final_coverage"]["status"] == "partial"
    assert items["partial"]["final_coverage"]["missing_pages"] == 1
    assert items["protected"]["recommendation"] == "keep_protected"
    assert items["protected"]["final_coverage"]["missing_pages"] is None
    coverage = read_pdf_coverage(state)
    assert (
        coverage["candidates"],
        coverage["documents"],
        coverage["extractions_complete"],
        coverage["protected"],
        coverage["recovered"],
        coverage["partial"],
    ) == (3, 3, 1, 1, 2, 1)


def test_pdf_scoped_pagination_replay_and_stale_cursor(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite"
    initialize_pdf_state(state)
    for key, source in (
        ("a", "/fixture/A/one.pdf"),
        ("b", "/fixture/A/two.pdf"),
        ("c", "/fixture/A-extra/three.pdf"),
    ):
        _pdf(state, key, source=source)
    first = list_pdf_diagnostics(state, 1, path_scope="/fixture/A")
    assert first.matched_count == 2 and first.next_cursor
    assert first.to_dict() == list_pdf_diagnostics(state, 1, path_scope="/fixture/A").to_dict()
    second = list_pdf_diagnostics(state, 1, path_scope="/fixture/A", cursor=first.next_cursor)
    assert second.items[0]["file_key"] == "b" and second.next_cursor is None
    with pytest.raises(ValueError, match="cursor"):
        list_pdf_diagnostics(state, 1, path_scope="/fixture/A-extra", cursor=first.next_cursor)
    with pdf_database(state) as connection:
        connection.execute("UPDATE documents SET status='partial' WHERE file_key='b'")
    with pytest.raises(ValueError, match="cursor"):
        list_pdf_diagnostics(state, 1, path_scope="/fixture/A", cursor=first.next_cursor)


def test_future_pdf_schema_and_bad_page_bounds_abstain(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite"
    initialize_pdf_state(state)
    _pdf(state, "a", source="/fixture/A.pdf")
    with pdf_database(state) as connection:
        connection.execute("UPDATE pages SET page_number=5 WHERE page_number=2")
    item = list_pdf_diagnostics(state).items[0]
    assert item["final_coverage"]["complete"] is False
    assert item["final_coverage"]["status"] == "unknown"
    with pdf_database(state) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    before = state.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        list_pdf_diagnostics(state)
    assert state.read_bytes() == before


def test_old_page_errors_replaced_by_published_pages_are_not_final_gaps(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite"
    initialize_pdf_state(state)
    _pdf(state, "a", source="/fixture/recovered.pdf", recovered=True)
    with pdf_database(state) as connection:
        connection.execute("UPDATE documents SET page_errors_count=1 WHERE file_key='a'")
        connection.execute("""INSERT INTO page_errors(file_key,processing_signature,page_number,error_type,error_message,updated_ns)
            VALUES('a','fixture-v1',1,'RuntimeError','old primary attempt',1)""")
    item = list_pdf_diagnostics(state).items[0]
    assert item["final_coverage"]["complete"] is True
    assert item["final_coverage"]["missing_pages"] == 0
    assert item["final_coverage"]["page_errors"] == 0
    assert item["final_coverage"]["stored_page_errors_count"] == 1
    assert item["historical_attempts"][-1]["status"] == "superseded"


def test_pdf_error_type_is_exact_scoped_and_bound_to_cursor(tmp_path: Path) -> None:
    state = tmp_path / "pdf.sqlite"
    initialize_pdf_state(state)
    for key, source in (
        ("a", "/fixture/A/a.pdf"),
        ("b", "/fixture/A/b.pdf"),
        ("c", "/fixture/A/c.pdf"),
        ("d", "/fixture/A-extra/d.pdf"),
    ):
        _pdf(state, key, source=source, status="error", published=0)
    with pdf_database(state) as connection:
        connection.execute("UPDATE documents SET error_type='DecodeError'")
        connection.execute("UPDATE documents SET error_type='OtherError' WHERE file_key='c'")
    first = list_pdf_diagnostics(state, 1, path_scope="/fixture/A", error_type="DecodeError")
    assert first.matched_count == 2 and first.next_cursor
    assert first.items[0]["file_key"] == "a"
    second = list_pdf_diagnostics(
        state, 1, path_scope="/fixture/A", error_type="DecodeError", cursor=first.next_cursor
    )
    assert second.items[0]["file_key"] == "b" and second.next_cursor is None
    assert list_pdf_diagnostics(state, error_type="decodeerror").matched_count == 0
    with pytest.raises(ValueError, match="cursor"):
        list_pdf_diagnostics(
            state, 1, path_scope="/fixture/A", error_type="OtherError", cursor=first.next_cursor
        )
    with pytest.raises(ValueError, match="error_type"):
        list_pdf_diagnostics(state, error_type="x" * 257)
