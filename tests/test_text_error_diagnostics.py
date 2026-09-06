"""Generic-text errors retain cause, resource and exact scope through paging."""

from pathlib import Path

import pytest

from neocortex.capabilities.formats.text.text_diagnostics import (
    list_text_errors,
    read_text_coverage,
)
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database


def test_text_errors_are_scoped_paged_and_explain_cause(tmp_path: Path) -> None:
    state = tmp_path / "text.sqlite"
    initialize_text_state(state)
    with text_database(state) as connection:
        for key, source, status in (
            ("a", "/fixture/A/one.txt", "error"),
            ("b", "/fixture/A/two.txt", "error"),
            ("c", "/fixture/A-extra/secret.txt", "error"),
            ("d", "/fixture/A/ok.txt", "complete"),
        ):
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,content_kind,media_type,error_type,error_message,retryable,
                last_seen_run_id,updated_ns) VALUES(?,?,1,1,-1,'fixture',?,'plain','text/plain',?,?,0,1,1)""",
                (
                    key,
                    source,
                    status,
                    "UnicodeError" if status == "error" else None,
                    "bounded decode failed" if status == "error" else None,
                ),
            )
        connection.commit()
    first = list_text_errors(state, 1, path_scope="/fixture/A")
    assert first.matched_count == 2 and first.next_cursor
    assert first.items[0]["error_type"] == "UnicodeError"
    assert first.items[0]["error_message"] == "bounded decode failed"
    assert first.to_dict() == list_text_errors(state, 1, path_scope="/fixture/A").to_dict()
    second = list_text_errors(state, 1, path_scope="/fixture/A", cursor=first.next_cursor)
    assert second.items[0]["file_key"] == "b" and second.next_cursor is None
    with pytest.raises(ValueError, match="cursor"):
        list_text_errors(state, 1, error_type="UnicodeError", cursor=first.next_cursor)
    coverage = read_text_coverage(state, path_scope="/fixture/A")
    assert (coverage["documents"], coverage["complete"], coverage["errors"]) == (3, 1, 2)
    assert coverage["candidate_scope"] == "unknown_inventory_not_owned_by_text"
