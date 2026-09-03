from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.api import agent_server


def test_agent_server_exposes_only_read_only_fixed_scope_tools() -> None:
    server = agent_server.create_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert names == {
        "status",
        "search",
        "context",
        "evidence",
        "inspect_code",
        "lineage",
        "asset_health",
    }
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

    result = asyncio.run(server.call_tool("status", {"scope": "personal"}))

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


def test_windows_stdio_uses_upstream_cross_platform_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def run_upstream(server: object) -> None:
        calls.append(server)

    async def reject_asyncio_pipes() -> None:
        raise AssertionError("Windows must not open asyncio standard-stream pipes")

    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(agent_server, "_requires_upstream_stdio_transport", lambda: True)
    monkeypatch.setattr(FastMCP, "run_stdio_async", run_upstream)
    monkeypatch.setattr(agent_server, "_asyncio_stdio_files", reject_asyncio_pipes)
    server = agent_server.create_server()

    asyncio.run(server.run_stdio_async())

    assert calls == [server]


def test_linux_stdio_private_sdk_boundary_is_versioned_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, object]] = []

    @dataclass
    class _LowLevel:
        async def run(self, reader: object, writer: object, options: object) -> None:
            calls.append((reader, writer, options))

        @staticmethod
        def create_initialization_options() -> object:
            return {"fixture": "options"}

    class _Server:
        _mcp_server = _LowLevel()

    monkeypatch.setattr(agent_server.importlib.metadata, "version", lambda _name: "1.29.0")
    reader = object()
    writer = object()

    asyncio.run(agent_server._run_fastmcp_over_streams(_Server(), reader, writer))

    assert calls == [(reader, writer, {"fixture": "options"})]

    monkeypatch.setattr(agent_server.importlib.metadata, "version", lambda _name: "2.0.0")
    with pytest.raises(RuntimeError, match="unsupported MCP stdio bridge version"):
        asyncio.run(agent_server._run_fastmcp_over_streams(_Server(), reader, writer))

    monkeypatch.setattr(agent_server.importlib.metadata, "version", lambda _name: "1.29.0")
    with pytest.raises(RuntimeError, match="bridge contract is unavailable"):
        asyncio.run(agent_server._run_fastmcp_over_streams(object(), reader, writer))


def test_server_instructions_treat_corpus_as_untrusted_and_deny_mutation() -> None:
    instructions = agent_server.SERVER_INSTRUCTIONS.casefold()
    assert "untrusted data" in instructions
    assert "no tool can move, rename, delete" in instructions


def test_public_stdio_server_completes_a_real_read_only_protocol_exchange(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        entry for entry in (source_root, environment.get("PYTHONPATH", "")) if entry
    )
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_STATE_HOME"] = str(tmp_path / "state")
    environment["XDG_DATA_HOME"] = str(tmp_path / "data")
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    if os.name == "nt":
        environment["LOCALAPPDATA"] = str(tmp_path / "local")
    else:
        environment.pop("LOCALAPPDATA", None)
    process = subprocess.Popen(
        (sys.executable, "-m", "neocortex", "agent", "serve"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = process.stdout
    stderr = process.stderr
    responses: queue.Queue[str] = queue.Queue(maxsize=8)

    def collect_responses() -> None:
        for line in stdout:
            responses.put(line)

    reader = threading.Thread(
        target=collect_responses,
        name="mcp-response-reader",
        daemon=True,
    )
    reader.start()

    def send(message: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def receive() -> dict[str, object]:
        payload = json.loads(responses.get(timeout=10))
        assert isinstance(payload, dict)
        return payload

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "neocortex-test", "version": "1"},
                },
            }
        )
        initialized = receive()
        assert initialized["id"] == 1
        initialization = initialized["result"]
        assert isinstance(initialization, dict)
        assert initialization["protocolVersion"] == "2025-11-25"
        assert initialization["serverInfo"]["name"] == "Neocortex"
        assert "tools" in initialization["capabilities"]

        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = receive()
        tools = listed["result"]["tools"]
        assert {tool["name"] for tool in tools} == {
            "status",
            "search",
            "context",
            "evidence",
            "inspect_code",
            "lineage",
            "asset_health",
        }
        assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools)

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {"scope": "personal"}},
            }
        )
        called = receive()
        assert called["result"]["isError"] is False
        structured = called["result"]["structuredContent"]
        assert structured["read_only"] is True
        assert structured["scope_requested"] == "personal"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        reader.join(timeout=10)
        stdout.close()
        stderr.close()

    assert process.returncode == 0
    assert not list(tmp_path.rglob("*"))
