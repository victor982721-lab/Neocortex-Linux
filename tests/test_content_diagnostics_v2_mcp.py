"""MCP adapter contract for the federated content-diagnostics/v2 surface."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from neocortex.api import agent_server
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database


TEST_CAPABILITIES = ("agent",)


@pytest.fixture
def diagnostic_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    database = state / "text.sqlite3"
    initialize_text_state(database)
    with text_database(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,error_type,error_message,last_seen_run_id,updated_ns)
            VALUES('fixture','/fixture/bad.txt',1,1,-1,'fixture','error','text',
            'text/plain','DecodeError','fixture error',1,1)"""
        )
        connection.commit()
    monkeypatch.setattr(agent_server, "default_state_directory", lambda: state)
    monkeypatch.setattr(agent_server, "default_corpus_root", lambda: corpus)
    return state, corpus


def test_mcp_v2_tool_is_additive_bounded_and_read_only(
    diagnostic_state: tuple[Path, Path],
) -> None:
    state, corpus = diagnostic_state
    server = agent_server.create_server()
    tool = next(
        item for item in asyncio.run(server.list_tools()) if item.name == "content_diagnostics_v2"
    )
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.inputSchema["properties"]["owner"]["default"] == "all"

    content, payload = asyncio.run(
        server.call_tool("content_diagnostics_v2", {"owner": "text", "limit": 10})
    )
    assert len(content) == 1
    assert json.loads(content[0].text) == payload
    assert payload["schema"] == "neocortex.content-diagnostics/v2"
    assert payload["response_version"] == 2
    assert payload["owner"] == "text"
    assert payload["owners"] == ["text"]
    assert payload["requested_root"] == str(corpus)
    assert payload["state_directory"] == str(state)
    assert payload["read_only"] is True
    assert payload["mutation_authorized"] is False
    assert payload["count"] == len(payload["items"])

    with sqlite3.connect(state / "text.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

