"""Focused contracts for the additive all-owner diagnostics read."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neocortex.api.content_diagnostics_api import (
    CONTENT_DIAGNOSTIC_V2_OWNERS,
    CONTENT_DIAGNOSTICS_SCHEMA,
    CONTENT_DIAGNOSTICS_V2_SCHEMA,
    ContentDiagnosticOwnerState,
    ContentDiagnosticsCursor,
    content_diagnostics_payload,
    content_diagnostics_v2_payload,
)
from neocortex.capabilities.formats.office.state import initialize_office_state
from neocortex.knowledge.knowledge_operational_query import (
    KnowledgeOperationalQueryService,
    OperationalQueryRequest,
)
from neocortex.knowledge.knowledge_read_budget import (
    KnowledgeReadBudget,
    KnowledgeReadBudgetExceeded,
)


def _office_state(tmp_path: Path, count: int = 3) -> tuple[Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "office.sqlite3"
    initialize_office_state(path)
    with sqlite3.connect(path) as connection:
        for index in range(count):
            connection.execute(
                """INSERT INTO documents(
                file_key,format,path,size,mtime_ns,birthtime_ns,processing_signature,status,
                error_type,error_message,retryable,review_disposition,last_seen_run_id,updated_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"key-{index}",
                    "pptx",
                    f"/fixture/{index}.pptx",
                    1,
                    1,
                    -1,
                    "fixture",
                    "error",
                    "FixtureError",
                    "bounded diagnostic",
                    0,
                    "none",
                    1,
                    index + 1,
                ),
            )
        connection.commit()
    return state, path


def test_v1_schema_and_owner_set_are_preserved() -> None:
    assert CONTENT_DIAGNOSTICS_SCHEMA == "neocortex.content-diagnostics/v1"
    assert CONTENT_DIAGNOSTICS_V2_SCHEMA == "neocortex.content-diagnostics/v2"
    assert CONTENT_DIAGNOSTIC_V2_OWNERS == (
        "pdf",
        "docx",
        "office",
        "text",
        "audio",
        "video",
        "image",
    )


def test_v2_pages_are_root_and_filter_bound_and_replayable(tmp_path: Path) -> None:
    state, owner_path = _office_state(tmp_path)
    before = owner_path.read_bytes()
    first = content_diagnostics_v2_payload(
        "office", state, "/fixture", 1, reason="FixtureError"
    )
    assert first["schema"] == CONTENT_DIAGNOSTICS_V2_SCHEMA
    assert first["status"] == "partial"
    assert first["count"] == 1 and first["matched_count"] == 3
    assert first["owner_states"]["office"]["state"] == ContentDiagnosticOwnerState.READY.value
    token = first["next_cursor"]
    assert isinstance(token, str)
    cursor = ContentDiagnosticsCursor.from_token(token)
    assert cursor.source_root == "/fixture"
    assert dict(cursor.filters)["reason"] == "FixtureError"
    assert dict(cursor.snapshots)["office"] == first["snapshots"]["office"]
    second = content_diagnostics_v2_payload(
        "office", state, "/fixture", 1, reason="FixtureError", cursor=token
    )
    assert second["status"] == "partial"
    assert first["items"][0]["record_id"] != second["items"][0]["record_id"]
    rebound = content_diagnostics_v2_payload(
        "office", state, "/other", 1, reason="FixtureError", cursor=token
    )
    assert rebound["status"] == "error"
    assert rebound["error"]["kind"] == "cursor_binding_mismatch"
    assert owner_path.read_bytes() == before


def test_v2_reports_missing_owner_without_calling_it_empty(tmp_path: Path) -> None:
    state, _ = _office_state(tmp_path, count=0)
    payload = content_diagnostics_v2_payload("all", state, "/fixture", 10)
    assert payload["status"] == "partial"
    assert payload["owner_states"]["office"]["state"] == "ready"
    assert payload["owner_states"]["office"]["status"] == "empty"
    for owner in CONTENT_DIAGNOSTIC_V2_OWNERS:
        if owner != "office":
            assert payload["owner_states"][owner]["state"] == "missing"
    assert "office" not in payload["coverage"]["missing_owners"]
    assert "pdf" in payload["coverage"]["missing_owners"]
    assert payload["owner_states"]["pdf"]["matched_count"] is None


@pytest.mark.parametrize("kind,expected_state", [("future", "future"), ("corrupt", "corrupt")])
def test_v2_typed_future_and_corrupt_owner_states(
    tmp_path: Path, kind: str, expected_state: str
) -> None:
    state, owner_path = _office_state(tmp_path, count=0)
    if kind == "future":
        with sqlite3.connect(owner_path) as connection:
            connection.execute(
                "UPDATE metadata SET value='999' WHERE key='schema_version'"
            )
            connection.commit()
    else:
        owner_path.write_bytes(b"not sqlite")
    payload = content_diagnostics_v2_payload("office", state, "/fixture")
    assert payload["owner_states"]["office"]["state"] == expected_state
    assert payload["owner_states"]["office"]["matched_count"] is None
    assert payload["status"] in {"unavailable", "blocked"}


def test_v2_budget_stops_before_completion_without_result_cache(tmp_path: Path) -> None:
    state, _ = _office_state(tmp_path)
    budget = KnowledgeReadBudget(max_rows=1)
    first = content_diagnostics_v2_payload("office", state, "/fixture", 20, budget=budget)
    assert first["status"] == "partial"
    assert first["truncated"] is True
    assert first["metrics"]["rows_returned"] == 1
    assert first["metrics"]["budget"]["rows_used"] == 1
    assert first["next_cursor"]
    # A spent budget is not silently reset or reused as a result cache.
    with pytest.raises(KnowledgeReadBudgetExceeded, match="rows exhausted"):
        budget.checkpoint(rows=1)


def test_v2_rejects_budget_on_legacy_payload_and_operational_v2_projects_metrics(
    tmp_path: Path,
) -> None:
    state, _ = _office_state(tmp_path, count=0)
    legacy = content_diagnostics_payload(
        "pdf", state, "/fixture", budget=KnowledgeReadBudget(max_rows=1)
    )
    assert legacy["status"] == "error"
    assert legacy["error"]["kind"] == "invalid_request"
    request = OperationalQueryRequest(
        "¿Qué errores tienen mis archivos?",
        state,
        Path("/fixture"),
        limit=2,
        response_version=2,
        diagnostic_owner="office",
        budget=KnowledgeReadBudget(max_rows=2),
    )
    result = KnowledgeOperationalQueryService().query(request)
    assert result.status == "empty"
    assert result.metrics["budget"]["max_rows"] == 2
    assert result.to_dict()["mutation_authorized"] is False
