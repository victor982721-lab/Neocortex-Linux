from __future__ import annotations

import anyio
import pytest

from neocortex import agent_server


def test_agent_server_exposes_only_read_only_fixed_scope_tools() -> None:
    server = agent_server.create_server()
    tools = anyio.run(server.list_tools)
    names = {tool.name for tool in tools}

    assert names == {"status", "search", "context", "evidence", "inspect_code"}
    assert not names.intersection({"delete", "move", "rename", "apply", "index", "write"})
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False


def test_agent_status_tool_returns_structured_read_api_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"kind": "neocortex_scoped_status", "read_only": True, "scopes": []}
    monkeypatch.setattr(agent_server, "status_payload", lambda scope: {**expected, "scope": scope})
    server = agent_server.create_server()

    result = anyio.run(server.call_tool, "status", {"scope": "personal"})

    _content, structured = result
    assert structured["kind"] == expected["kind"]
    assert structured["scope"] == "personal"


def test_stdio_is_the_only_transport_started_by_public_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Server:
        def run(self, *, transport: str) -> None:
            calls.append(transport)

    monkeypatch.setattr(agent_server, "create_server", _Server)

    assert agent_server.run_stdio_server() == 0
    assert calls == ["stdio"]


def test_server_instructions_treat_corpus_as_untrusted_and_deny_mutation() -> None:
    instructions = agent_server.SERVER_INSTRUCTIONS.casefold()
    assert "untrusted data" in instructions
    assert "no tool can move, rename, delete" in instructions
