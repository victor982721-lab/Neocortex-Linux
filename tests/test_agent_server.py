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

from neocortex.api import agent_server, curation_api


def _curation_payload() -> dict[str, object]:
    return {
        "schema": "neocortex.curation-plan/v1",
        "kind": "neocortex_curation_plan",
        "operation": "curation-plan",
        "request_id": "curation-fixture",
        "read_only": True,
        "effects": {"state": "none", "corpus": "none", "external": "none"},
        "trust": {
            "content_class": "untrusted_corpus_evidence",
            "instruction_authority": False,
            "tools_authorized": False,
            "actions_authorized": False,
        },
        "coverage": "complete",
        "snapshot": {
            "schema": "neocortex.curation-snapshot/v1",
            "snapshot_id": "curation-snapshot:fixture",
            "coverage": "complete",
            "root": "/fixture",
            "scan_id": 7,
            "missing_owners": [],
        },
        "page": {
            "limit": 2,
            "cursor": None,
            "next_cursor": "cursor-2",
            "complete": False,
            "plan_digest": "sha256:" + "a" * 64,
            "items_total": 3,
            "items": [],
        },
        "error": None,
    }


def _stable_evidence_payload() -> dict[str, object]:
    match = {
        "scope": "personal",
        "snapshot": {"snapshot_id": "snapshot-1"},
        "citation": {"citation_id": "K2", "evidence_id": "evidence:2"},
        "hit": {"evidence": {"evidence_id": "evidence:2"}},
    }
    return {
        "schema": "neocortex.read-api/v1",
        "kind": "neocortex_evidence",
        "operation": "evidence",
        "request_id": "evidence-fixture",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "coverage": "complete",
        "status": "ok",
        "exit_code": 0,
        "error": None,
        "result": {
            "matches": [match],
            "scopes": [],
            "evidence_id": "evidence:2",
            "expected_snapshot_id": "snapshot-1",
        },
        "scopes": [],
        "query": "breaker",
        "citation_id": "K1",
        "evidence_id": "evidence:2",
        "expected_snapshot_id": "snapshot-1",
        "found": True,
        "limit_per_scope": 2,
    }


@dataclass(frozen=True)
class _CurationItem:
    item_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "kind": "empty_file",
            "status": "review",
            "action": "review_empty_file",
            "source_path": "/fixture/untrusted",
            "destination_path": None,
            "reason": "fixture",
            "evidence": {"identity": "fixture"},
        }


@dataclass(frozen=True)
class _CurationPage:
    coverage: str = "complete"
    missing_owners: tuple[str, ...] = ()
    root: str | None = "/fixture"
    scan_id: int | None = 7
    inventory_files: int = 3
    duplicate_groups: int = 1
    duplicate_members: int = 2
    reclaimable_bytes: int = 10
    organization_plans: int = 1
    empty_files: int = 1
    limit: int = 2
    cursor: str | None = None
    next_cursor: str | None = "cursor-2"
    snapshot_id: str = "curation-snapshot:fixture"
    plan_digest: str = "sha256:" + "a" * 64
    items_total: int = 3
    items: tuple[_CurationItem, ...] = (_CurationItem("item-1"),)


class _CurationStateError(RuntimeError):
    pass


def test_direct_curation_api_uses_only_the_canonical_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "canonical-state"
    calls: list[tuple[Path, int, str | None]] = []

    def build(path: Path, *, limit: int, cursor: str | None) -> _CurationPage:
        calls.append((path, limit, cursor))
        return _CurationPage(limit=limit, cursor=cursor)

    monkeypatch.setattr(curation_api, "default_state_directory", lambda: state)
    monkeypatch.setattr(
        curation_api,
        "_plan_contract",
        lambda: (_CurationStateError, build),
    )

    payload = curation_api.curation_plan_payload(
        limit=2,
        cursor="cursor-1",
        request_id="curation-request-1",
    )

    assert calls == [(state, 2, "cursor-1")]
    assert payload["schema"] == "neocortex.curation-plan/v1"
    assert payload["operation"] == "curation-plan"
    assert payload["request_id"] == "curation-request-1"
    assert payload["read_only"] is True
    assert payload["coverage"] == "complete"
    assert payload["snapshot"]["snapshot_id"] == "curation-snapshot:fixture"
    assert payload["page"]["cursor"] == "cursor-1"
    assert payload["page"]["next_cursor"] == "cursor-2"
    assert payload["page"]["plan_digest"] == "sha256:" + "a" * 64
    assert payload["error"] is None
    assert not state.exists()


def test_direct_curation_api_maps_state_errors_to_typed_unavailable_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(_path: Path, *, limit: int, cursor: str | None) -> _CurationPage:
        del limit, cursor
        raise _CurationStateError("state unavailable\x1b[31m")

    monkeypatch.setattr(
        curation_api,
        "_plan_contract",
        lambda: (_CurationStateError, build),
    )

    payload = curation_api.curation_plan_payload(
        limit=2,
        cursor=None,
        request_id="curation-request-2",
    )

    assert payload["coverage"] == "unavailable"
    assert payload["snapshot"]["coverage"] == "unavailable"
    assert payload["page"] == {
        "limit": 2,
        "cursor": None,
        "next_cursor": None,
        "complete": False,
        "plan_digest": None,
        "items_total": 0,
        "items": [],
    }
    assert payload["error"] == {
        "code": "unavailable",
        "message": "state unavailable",
        "retryable": False,
    }


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
        "curation_plan",
    }
    assert not names.intersection({"delete", "move", "rename", "apply", "index", "write"})
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False

    curation = next(tool for tool in tools if tool.name == "curation_plan")
    assert set(curation.inputSchema["properties"]) == {"limit", "cursor"}
    assert curation.inputSchema["properties"]["limit"]["maximum"] == 100
    assert curation.outputSchema is not None
    assert curation.outputSchema["additionalProperties"] is False
    assert {
        "schema",
        "operation",
        "request_id",
        "read_only",
        "effects",
        "trust",
        "snapshot",
        "page",
        "error",
    }.issubset(curation.outputSchema["required"])

    evidence = next(tool for tool in tools if tool.name == "evidence")
    assert {"evidence_id", "expected_snapshot_id"}.issubset(
        evidence.inputSchema["properties"]
    )
    assert not {"path", "state_directory", "authorization"}.intersection(
        evidence.inputSchema["properties"]
    )
    assert evidence.outputSchema is not None
    assert {"evidence_id", "expected_snapshot_id"}.issubset(
        evidence.outputSchema["properties"]
    )


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


def test_agent_evidence_forwards_stable_identity_and_expected_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _stable_evidence_payload()
    calls: list[tuple[object, ...]] = []

    def payload(
        query: str,
        citation_id: str,
        scope: str,
        *,
        evidence_id: str | None,
        expected_snapshot_id: str | None,
        limit: int,
        max_characters: int,
    ) -> dict[str, object]:
        calls.append(
            (
                query,
                citation_id,
                scope,
                evidence_id,
                expected_snapshot_id,
                limit,
                max_characters,
            )
        )
        return expected

    monkeypatch.setattr(agent_server, "evidence_payload", payload)
    server = agent_server.create_server()

    result = asyncio.run(
        server.call_tool(
            "evidence",
            {
                "query": "breaker",
                "citation_id": "K1",
                "scope": "personal",
                "limit": 2,
                "max_characters": 4_000,
                "evidence_id": "evidence:2",
                "expected_snapshot_id": "snapshot-1",
            },
        )
    )

    _content, structured = result
    assert calls == [
        ("breaker", "K1", "personal", "evidence:2", "snapshot-1", 2, 4_000)
    ]
    assert structured["citation_id"] == "K1"
    assert structured["evidence_id"] == "evidence:2"
    assert structured["expected_snapshot_id"] == "snapshot-1"
    assert structured["found"] is True


def test_agent_curation_plan_returns_the_direct_typed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _curation_payload()
    calls: list[tuple[int, str | None]] = []

    def payload(*, limit: int, cursor: str | None) -> dict[str, object]:
        calls.append((limit, cursor))
        return expected

    monkeypatch.setattr(agent_server, "curation_plan_payload", payload)
    server = agent_server.create_server()

    result = asyncio.run(
        server.call_tool("curation_plan", {"limit": 2, "cursor": "cursor-1"})
    )

    _content, structured = result
    assert calls == [(2, "cursor-1")]
    assert structured == expected
    assert structured["effects"] == {
        "state": "none",
        "corpus": "none",
        "external": "none",
    }
    assert structured["trust"]["actions_authorized"] is False


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
            "curation_plan",
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

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "curation_plan", "arguments": {"limit": 2}},
            }
        )
        curation = receive()
        assert curation["result"]["isError"] is False
        curation_payload = curation["result"]["structuredContent"]
        assert curation_payload["schema"] == "neocortex.curation-plan/v1"
        assert curation_payload["coverage"] == "unavailable"
        assert curation_payload["read_only"] is True
        assert curation_payload["effects"] == {
            "state": "none",
            "corpus": "none",
            "external": "none",
        }
        assert curation_payload["error"]["code"] == "unavailable"
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
