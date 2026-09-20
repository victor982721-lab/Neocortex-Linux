"""MCP read producers execute inside the structured error boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from neocortex.api import agent_server, read_api
from neocortex.api.read_contract import ReadContractError, ReadOperation, validate_read_payload


TEST_CAPABILITIES = ("agent",)

_READ_TOOLS = [
    ("status", "status_payload", {}),
    ("search", "search_payload", {"query": "fixture"}),
    ("context", "context_payload", {"query": "fixture", "response_version": 1}),
    ("operational_query", "operational_query_payload", {"query": "pdf error"}),
    ("evidence", "evidence_payload", {"query": "fixture", "citation_id": "C1", "response_version": 1}),
    ("lineage", "lineage_payload", {"identifier": "fixture"}),
    ("asset_health", "asset_health_payload", {"resource_id": "resource:file:1:2:-1"}),
]


def test_mcp_operational_query_uses_the_shared_read_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer_payload = {
        "schema": "neocortex.read-api/v1",
        "kind": "neocortex_scoped_operational_query",
        "operation": "operational_query",
        "request_id": "operational-test",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "advisory_only": True,
        "mutation_authorized": False,
        "coverage": "complete",
        "status": "ok",
        "exit_code": 0,
        "error": None,
        "query": "pdf error",
        "limit_per_scope": 2,
        "scopes": [],
        "result": {"scopes": []},
    }
    calls: list[tuple[str, str, int, str | None]] = []

    def fake(query: str, scope: str, *, limit: int, cursor: str | None = None):
        calls.append((query, scope, limit, cursor))
        return producer_payload

    monkeypatch.setattr(agent_server, "operational_query_payload", fake)
    server = agent_server.create_server()
    _content, payload = asyncio.run(
        server.call_tool(
            "operational_query",
            {"query": "pdf error", "scope": "personal", "limit": 2, "cursor": "c1"},
        )
    )
    validate_read_payload(payload, ReadOperation.OPERATIONAL_QUERY, scope="personal", query="pdf error", limit=2)
    assert calls == [("pdf error", "personal", 2, "c1")]
    assert payload["advisory_only"] is True
    assert payload["mutation_authorized"] is False


@pytest.mark.parametrize(("tool", "producer", "arguments"), _READ_TOOLS)
@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (ValueError("invalid fixture input\x1b[31m"), 2),
        (ModuleNotFoundError("No module named 'xxhash'", name="xxhash"), 1),
        (sqlite3.OperationalError("fixture read unavailable"), 1),
        (ReadContractError("fixture producer contract invalid"), 6),
    ],
)
def test_read_producer_errors_are_typed_and_preserve_request_echoes(
    tool: str,
    producer: str,
    arguments: dict[str, str],
    exception: Exception,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail(*_args: object, **_kwargs: object) -> Any:
        nonlocal calls
        calls += 1
        raise exception

    monkeypatch.setattr(agent_server, producer, fail)
    server = agent_server.create_server()
    _content, payload = asyncio.run(
        server.call_tool(tool, {**arguments, "scope": "personal"})
    )
    validate_read_payload(payload, ReadOperation(tool), scope="personal")
    assert payload["exit_code"] == code
    assert payload["read_only"] is True
    assert payload["error"] is not None
    assert "\x1b" not in payload["error"]["message"]
    if "query" in arguments:
        assert payload["query"] == arguments["query"]
    assert calls == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("lineage", {"identifier": "\x00"}),
        ("asset_health", {"resource_id": "/untrusted/path"}),
    ],
)
def test_mcp_identifiers_rejected_before_any_state_lookup_use_a_usage_envelope(
    tool: str,
    arguments: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden_lookup(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("invalid identifiers must not inspect state")

    monkeypatch.setattr(read_api, "scope_bindings", forbidden_lookup)
    server = agent_server.create_server()
    _content, payload = asyncio.run(
        server.call_tool(tool, {**arguments, "scope": "personal"})
    )
    validate_read_payload(payload, ReadOperation(tool), scope="personal")
    assert payload["exit_code"] == 2
    assert payload["scopes"] == []
    assert not tuple(tmp_path.iterdir())


def test_mcp_rejects_sanitized_key_collisions_with_a_schema_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_server,
        "status_payload",
        lambda _scope: {"exit_code": 0, "result": {"x\t": "first", "x ": "second"}},
    )
    server = agent_server.create_server()
    _content, payload = asyncio.run(server.call_tool("status", {"scope": "personal"}))
    validate_read_payload(payload, ReadOperation.STATUS, scope="personal")
    assert payload["exit_code"] == 6
    assert "keys" in payload["error"]["message"]
    assert payload["result"] == {"scopes": []}


@pytest.mark.parametrize("code", [0, 3])
def test_mcp_contradictory_success_errors_use_a_schema_envelope(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent_server,
        "status_payload",
        lambda _scope: {
            "exit_code": code,
            "error": {"code": "error", "message": "producer failed"},
        },
    )
    server = agent_server.create_server()
    _content, payload = asyncio.run(server.call_tool("status", {"scope": "personal"}))
    validate_read_payload(payload, ReadOperation.STATUS, scope="personal")
    assert payload["exit_code"] == 6
    assert "error" in payload["error"]["message"]


def test_read_boundary_does_not_consume_user_cancellation() -> None:
    def cancelled() -> Any:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        agent_server._structured_read_payload(cancelled, ReadOperation.STATUS, scope="personal")
