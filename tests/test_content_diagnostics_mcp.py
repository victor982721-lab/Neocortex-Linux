"""Real MCP format diagnostics over contained owners and configured roots."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from neocortex.api import agent_server
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state, pdf_database
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database


TEST_CAPABILITIES = ("agent",)


def _fingerprints(state: Path) -> dict[str, str]:
    return {
        str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def format_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "selected"
    # These are only persisted fixture paths. The corpus directory does not
    # exist: diagnostics must not scan or reopen any of the source files.
    rows = (
        ("a", root / "a", "FixtureError"),
        ("b", root / "b", "FixtureError"),
        ("c", root / "c", "OtherError"),
        ("outside", tmp_path / "selected-other" / "outside", "FixtureError"),
    )
    initialize_pdf_state(state / "pdf.sqlite3")
    with pdf_database(state / "pdf.sqlite3") as connection:
        for key, source, reason in rows:
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,error_type,error_message,page_count,updated_ns)
                VALUES(?,?,1,1,-1,'fixture','error',?,'fixture extraction error',2,1)""",
                (key, str(source) + ".pdf", reason),
            )
        connection.commit()
    initialize_text_state(state / "text.sqlite3")
    with text_database(state / "text.sqlite3") as connection:
        for key, source, reason in rows:
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,content_kind,media_type,error_type,error_message,
                last_seen_run_id,updated_ns)
                VALUES(?,?,1,1,-1,'fixture','error','plain','text/plain',?,
                'fixture decode error',1,1)""",
                (key, str(source) + ".txt", reason),
            )
        connection.commit()
    monkeypatch.setattr(agent_server, "default_state_directory", lambda: state)
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(root))
    before = _fingerprints(state)
    yield state, root
    assert _fingerprints(state) == before
    assert not root.exists()


def _call(server: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    content, payload = asyncio.run(server.call_tool("content_diagnostics", arguments))
    assert len(content) == 1
    assert content[0].type == "text"
    assert json.loads(content[0].text) == payload
    assert payload["schema"] == "neocortex.content-diagnostics/v1"
    assert payload["read_only"] is True
    return payload


def test_mcp_lists_one_readonly_diagnostic_tool_without_root_or_effect_arguments() -> None:
    server = agent_server.create_server()
    matches = [tool for tool in asyncio.run(server.list_tools()) if tool.name == "content_diagnostics"]
    assert len(matches) == 1
    tool = matches[0]
    properties = tool.inputSchema["properties"]
    assert set(properties) == {"owner", "limit", "cursor", "file_key", "path_fragment", "reason"}
    assert set(properties["owner"]["enum"]) == {"pdf", "text"}
    assert properties["limit"]["default"] == 20
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 1_000
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True
    assert tool.annotations.openWorldHint is False
    assert tool.outputSchema is not None
    assert tool.outputSchema["properties"]["schema"]["const"] == "neocortex.content-diagnostics/v1"


@pytest.mark.parametrize("owner", ("pdf", "text"))
def test_mcp_real_diagnostics_are_root_scoped_exact_filtered_paged_and_replayable(
    format_state: tuple[Path, Path],
    owner: str,
) -> None:
    state, root = format_state
    server = agent_server.create_server()
    arguments = {"owner": owner, "limit": 1, "reason": "FixtureError"}
    first = _call(server, arguments)
    assert first["status"] == "ok", first["error"]
    assert first["requested_root"] == str(root)
    assert first["owner_path"] == str(state / f"{owner}.sqlite3")
    assert first["count"] == 1
    assert first["truncated"] is True
    assert first["next_cursor"]
    assert first["reason_field"] == "error_type"
    assert first == _call(server, arguments)
    second = _call(server, {**arguments, "cursor": first["next_cursor"]})
    assert second["status"] == "ok", second["error"]
    assert second["count"] == 1
    assert second["truncated"] is False
    assert second["next_cursor"] is None
    keys = "file_key"
    assert {first["items"][0][keys], second["items"][0][keys]} == {"a", "b"}
    coverage = first["coverage"]
    assert coverage["persisted_only"] is True
    assert coverage["snapshot_consistent"] is True
    assert coverage["root_summary_scope"] == "requested_root_without_query_filters"
    assert first["matched_count"] == 2
    assert coverage["root_summary"]["documents"] == 3
    partial_reason = _call(server, {"owner": owner, "reason": "Fixture"})
    assert partial_reason["status"] == "ok"
    assert partial_reason["count"] == 0
    exact_key = _call(server, {"owner": owner, "file_key": "a"})
    assert exact_key["count"] == 1
    assert exact_key["items"][0][keys] == "a"
    wildcard = _call(server, {"owner": owner, "path_fragment": "%"})
    assert wildcard["status"] == "ok"
    assert wildcard["count"] == 0


@pytest.mark.parametrize("owner", ("pdf", "text"))
def test_mcp_missing_diagnostic_owner_is_unknown_not_zero_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
) -> None:
    state = tmp_path / "absent-state"
    root = tmp_path / "unread-corpus"
    monkeypatch.setattr(agent_server, "default_state_directory", lambda: state)
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(root))
    payload = _call(agent_server.create_server(), {"owner": owner})
    assert payload["status"] == "unavailable"
    assert payload["error"]["kind"] == "owner_missing"
    assert payload["count"] == 0
    assert payload["matched_count"] is None
    assert payload["truncated"] is None
    assert payload["coverage"]["status"] == "unknown"
    assert payload["coverage"]["snapshot_consistent"] is False
    assert not state.exists()
    assert not root.exists()


def test_mcp_diagnostic_cursor_cannot_rebind_to_another_reason(
    format_state: tuple[Path, Path],
) -> None:
    server = agent_server.create_server()
    first = _call(server, {"owner": "text", "limit": 1, "reason": "FixtureError"})
    changed = _call(
        server,
        {"owner": "text", "limit": 1, "reason": "OtherError", "cursor": first["next_cursor"]},
    )
    assert changed["status"] == "error"
    assert changed["error"]["kind"] == "invalid_cursor"
    assert changed["items"] == []
    assert changed["matched_count"] is None


def test_mcp_bad_configuration_stays_typed_without_falling_back_to_owner_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", "relative-is-invalid")
    monkeypatch.setattr(
        agent_server, "default_state_directory",
        lambda: pytest.fail("invalid corpus configuration must stop before owner resolution"),
    )
    payload = _call(agent_server.create_server(), {"owner": "text"})
    assert payload["status"] == "blocked"
    assert payload["error"]["kind"] == "configuration_unavailable"
    assert payload["requested_root"] is None
    assert payload["owner_path"] is None
    assert payload["items"] == []
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("malformation", ("wrong_root", "wrong_count", "raised"))
def test_mcp_diagnostic_adapter_failures_are_typed_without_an_apparently_valid_page(
    format_state: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    server = agent_server.create_server()
    original = _call(server, {"owner": "text"})
    malformed = copy.deepcopy(original)
    if malformation == "wrong_root":
        malformed["requested_root"] = "/outside/configured-root"
    elif malformation == "wrong_count":
        malformed["count"] += 1

    def producer(*_args: Any, **_kwargs: Any) -> Any:
        if malformation == "raised":
            raise RuntimeError("fixture unavailable\x1b[31m")
        return malformed

    monkeypatch.setattr(agent_server, "content_diagnostics_payload", producer)
    payload = _call(server, {"owner": "text"})
    assert payload["status"] != "ok"
    assert payload["error"]["kind"] == (
        "owner_state_unavailable" if malformation == "raised" else "adapter_contract_error"
    )
    assert "\x1b" not in payload["error"]["message"]
    assert payload["items"] == []
    assert payload["matched_count"] is None
