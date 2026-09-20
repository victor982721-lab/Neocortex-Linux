"""Real context negotiation and final-transport checks on contained Text state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

import pytest

from neocortex.api import agent_server, read_api
from neocortex.api.cli import human
from neocortex.api.read_contract import ReadContractError
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database
from neocortex.interface.entrypoint import entrypoint


TEST_CAPABILITIES = ("agent",)


@pytest.fixture
def published_text_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create one searchable publication, never consulting production roots."""

    state = tmp_path / "published"
    state.mkdir()
    database = state / "text.sqlite3"
    initialize_text_state(database)
    key = "00000000000000000000000000000001:00000000000000000000000000000002"
    path = "/fixture/protección-\u03b1.txt"
    body = 'Protección del interruptor, evidencia "técnica" en \\equipo \u03b1.'
    with text_database(database) as connection:
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
            status,content_kind,media_type,text_zlib,text_chars,last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,?,'complete','text','text/plain',?,?,1,1)""",
            (
                key,
                path,
                len(body.encode("utf-8")),
                1,
                -1,
                "text-v2:fixture",
                zlib.compress(body.encode("utf-8")),
                len(body),
            ),
        )
        connection.execute(
            """INSERT INTO document_fts(file_key,path,content_kind,title,author,body)
            VALUES(?,?,?,?,?,?)""",
            (key, path, "text", "", "", body),
        )
        connection.commit()
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    return state


def _fingerprints(state: Path) -> dict[str, str]:
    return {
        str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.rglob("*")
        if path.is_file()
    }


def _assert_compact(payload: dict[str, Any]) -> None:
    assert payload["schema"] == "neocortex.context-response/v2"
    assert payload["operation"] == "context"
    assert isinstance(payload["coverage"], dict)
    assert payload["sources"]
    assert payload["citations"]
    assert not {"result", "scopes", "selected_hits", "rendered_context"}.intersection(payload)
    source_ids = {source["source_id"] for source in payload["sources"]}
    assert len(source_ids) == len(payload["sources"])
    assert all(citation["source_id"] in source_ids for citation in payload["citations"])


@pytest.mark.parametrize("legacy", (False, True))
def test_human_ask_json_negotiates_real_context_and_preserves_state(
    published_text_state: Path,
    legacy: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.knowledge.knowledge_context_v2 import serialize_context_response

    before = _fingerprints(published_text_state)
    flags = ("--response-version", "1") if legacy else ()
    code = entrypoint(("ask", "protección", "--json", *flags))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert code == payload["exit_code"]
    if legacy:
        assert payload["schema"] == "neocortex.read-api/v1"
        assert payload["scopes"][0]["context"]["selected_hits"]
    else:
        _assert_compact(payload)
        assert captured.out == serialize_context_response(payload) + "\n"
        assert payload["budget"]["characters_used"] == len(captured.out)
        assert len(captured.out) <= 12_000
    assert _fingerprints(published_text_state) == before


def test_human_ask_text_emits_only_the_budgeted_renderer(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.knowledge.knowledge_context_v2 import render_context_response

    actual_context = read_api.context_payload
    observed: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["response_version"] == 2
        assert kwargs["response_transport"] == "text"
        payload = actual_context(*args, **kwargs)
        observed.append(payload)
        return payload

    monkeypatch.setattr(human, "context_payload", record)
    before = _fingerprints(published_text_state)
    code = entrypoint(("ask", "protección"))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(observed) == 1
    _assert_compact(observed[0])
    assert code == observed[0]["exit_code"]
    assert captured.out == render_context_response(observed[0]) + "\n"
    assert observed[0]["budget"]["characters_used"] == len(captured.out)
    assert len(captured.out) <= 12_000
    assert _fingerprints(published_text_state) == before


def test_mcp_context_defaults_to_v2_and_keeps_v1_explicit(
    published_text_state: Path,
) -> None:
    """The MCP negotiation matches the CLI default without losing legacy v1."""

    before = _fingerprints(published_text_state)
    server = agent_server.create_server()

    default_result = asyncio.run(
        server.call_tool("context", {"query": "protección", "scope": "personal"})
    )
    compact = getattr(default_result, "structuredContent", None)
    assert isinstance(compact, dict)
    _assert_compact(compact)
    assert compact["response_version"] == 2
    assert all(
        citation.get("answer_sufficiency", "not_assessed") == "not_assessed"
        for citation in compact["citations"]
    )

    legacy_result = asyncio.run(
        server.call_tool(
            "context",
            {"query": "protección", "scope": "personal", "response_version": 1},
        )
    )
    _content, legacy = legacy_result
    assert legacy["schema"] == "neocortex.read-api/v1"
    assert legacy["scopes"][0]["context"]
    legacy_rendered = legacy["scopes"][0]["context"]["rendered_context"]
    assert '"instruction_authority":false' in legacy_rendered
    assert '"tools_authorized":false' in legacy_rendered
    assert '"actions_authorized":false' in legacy_rendered
    assert _fingerprints(published_text_state) == before


def test_mcp_operational_cursor_matches_bounded_read_api_contract() -> None:
    server = agent_server.create_server()
    tool = next(
        item for item in asyncio.run(server.list_tools()) if item.name == "operational_query"
    )
    cursor_schema = tool.inputSchema["properties"]["cursor"]
    cursor_branch = next(item for item in cursor_schema["anyOf"] if item.get("type") == "string")
    assert cursor_branch["maxLength"] == 8_192


@pytest.mark.parametrize("legacy", (False, True))
def test_flat_context_uses_explicit_state_root_and_negotiates(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.knowledge.knowledge_context_v2 import serialize_context_response

    def forbid_fixed_root() -> Path:
        raise AssertionError("flat context must retain its explicit state-directory")

    monkeypatch.setattr(read_api, "default_state_directory", forbid_fixed_root)
    before = _fingerprints(published_text_state)
    flags = ("--knowledge-response-version", "1") if legacy else ()
    code = entrypoint(
        (
            "--state-directory",
            str(published_text_state),
            "--knowledge-context",
            "protección",
            "--knowledge-json",
            *flags,
        )
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    if legacy:
        assert payload["schema_version"] == 1
        assert payload["selected_hits"]
    else:
        _assert_compact(payload)
        assert code == payload["exit_code"]
        assert captured.out == serialize_context_response(payload) + "\n"
        assert payload["budget"]["characters_used"] == len(captured.out)
        assert len(captured.out) <= 12_000
    assert _fingerprints(published_text_state) == before


def test_python_context_keeps_low_level_v1_and_explicit_v2_transport(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_context = read_api.context_payload
    assert actual_context("protección")["schema"] == "neocortex.read-api/v1"
    calls: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return actual_context(*args, **kwargs)

    monkeypatch.setattr(read_api, "context_payload", record)
    before = _fingerprints(published_text_state)
    payload = read_api.context_payload(
        "protección",
        scope="personal",
        response_version=2,
    )
    assert payload["schema"] == "neocortex.context-response/v2"
    assert len(calls) == 1
    assert calls[0]["response_version"] == 2
    assert _fingerprints(published_text_state) == before


def test_flat_context_v2_reports_unavailable_owner_without_creating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "not-a-directory"
    state.write_bytes(b"unchanged fixture")
    code = entrypoint(
        (
            "--state-directory",
            str(state),
            "--knowledge-context",
            "protección",
            "--knowledge-json",
        )
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "neocortex.context-response/v2"
    assert code == payload["exit_code"] == 1
    assert payload["error"] is not None
    assert payload["sources"] == payload["citations"] == []
    assert captured.err == ""
    assert state.read_bytes() == b"unchanged fixture"


@pytest.mark.parametrize("json_output", (False, True))
def test_human_v2_tiny_budget_returns_only_explicit_budget_error(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.knowledge.knowledge_context_v2 import (
        render_context_response,
        serialize_context_response,
    )

    actual_context = read_api.context_payload
    observed: list[dict[str, Any]] = []

    def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = actual_context(*args, **kwargs)
        observed.append(payload)
        return payload

    monkeypatch.setattr(human, "context_payload", record)
    before = _fingerprints(published_text_state)
    flags = ("--json",) if json_output else ()
    code = entrypoint(("ask", "protección", "--characters", "1", *flags))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(observed) == 1
    payload = observed[0]
    assert code == payload["exit_code"] == 2
    assert payload["schema"] == "neocortex.context-response/v2"
    assert payload["error"]["code"] == "budget_insufficient"
    assert payload["sources"] == payload["citations"] == []
    assert payload["budget"]["within_limit"] is False
    assert payload["budget"]["characters_used"] == len(captured.out)
    assert payload["budget"]["minimum_required"] == len(captured.out)
    expected = serialize_context_response(payload) if json_output else render_context_response(payload)
    assert captured.out == expected + "\n"
    assert _fingerprints(published_text_state) == before


@pytest.mark.parametrize("query", ("", " "))
def test_human_invalid_request_keeps_default_v2_contract(
    published_text_state: Path,
    query: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _fingerprints(published_text_state)
    code = entrypoint(("ask", query, "--json"))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == payload["exit_code"] == 2
    assert payload["schema"] == "neocortex.context-response/v2"
    assert payload["error"]["code"] == "invalid_request"
    assert payload["sources"] == payload["citations"] == []
    assert payload["budget"]["characters_used"] == len(captured.out)
    assert captured.err == ""
    assert _fingerprints(published_text_state) == before


@pytest.mark.capability("agent")
def test_mcp_context_uses_real_sdk_compact_tool_result_and_legacy_opt_in(
    published_text_state: Path,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api.agent_server import create_server
    from neocortex.knowledge.knowledge_context_v2 import serialize_context_response

    before = _fingerprints(published_text_state)
    server = create_server()
    metadata = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "context")
    version = metadata.inputSchema["properties"]["response_version"]
    assert version["default"] == 2
    assert version["enum"] == [1, 2]
    result = asyncio.run(server.call_tool("context", {"query": "protección"}))
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = result.structuredContent
    assert isinstance(payload, dict)
    _assert_compact(payload)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == serialize_context_response(payload)
    wire = result.model_dump_json(by_alias=True, exclude_none=True)
    assert payload["budget"]["characters_used"] == len(wire)
    assert payload["budget"]["measurement_scope"] == "mcp_tool_result"
    assert len(wire) <= 12_000
    assert json.loads(wire)["structuredContent"] == payload
    legacy = asyncio.run(
        server.call_tool("context", {"query": "protección", "response_version": 1})
    )
    assert isinstance(legacy, tuple)
    assert legacy[1]["schema"] == "neocortex.read-api/v1"
    assert _fingerprints(published_text_state) == before


@pytest.mark.capability("agent")
def test_mcp_tiny_budget_keeps_a_typed_result_with_exact_minimum(
    published_text_state: Path,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api.agent_server import create_server

    before = _fingerprints(published_text_state)
    server = create_server()
    result = asyncio.run(
        server.call_tool("context", {"query": "protección", "max_characters": 1})
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = result.structuredContent
    assert isinstance(payload, dict)
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "budget_insufficient"
    assert payload["sources"] == payload["citations"] == []
    wire = result.model_dump_json(by_alias=True, exclude_none=True)
    assert payload["budget"]["characters_used"] == len(wire)
    assert payload["budget"]["minimum_required"] == len(wire)
    assert payload["budget"]["within_limit"] is False
    assert _fingerprints(published_text_state) == before


@pytest.mark.capability("agent")
def test_mcp_evidence_resolves_context_references_without_search_replay(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api.agent_server import create_server
    from neocortex.knowledge.knowledge_context_v2 import serialize_context_response
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService

    before = _fingerprints(published_text_state)
    server = create_server()
    context = asyncio.run(server.call_tool("context", {"query": "protección"}))
    assert isinstance(context, CallToolResult)
    context_payload = context.structuredContent
    assert isinstance(context_payload, dict)
    citation = context_payload["citations"][0]
    source = next(
        item for item in context_payload["sources"] if item["source_id"] == citation["source_id"]
    )

    def forbid_retrieval(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("reference resolution must not rerun context or search")

    monkeypatch.setattr(KnowledgeSearchService, "search", forbid_retrieval)
    monkeypatch.setattr(KnowledgeSearchService, "context", forbid_retrieval)
    metadata = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "evidence")
    assert {"source_ref", "evidence_ref"}.issubset(metadata.inputSchema["properties"])
    assert not {"query", "citation_id"}.intersection(metadata.inputSchema.get("required", ()))
    evidence = asyncio.run(
        server.call_tool(
            "evidence",
            {"source_ref": source, "evidence_ref": citation, "scope": source["scope"]},
        )
    )
    assert isinstance(evidence, CallToolResult)
    assert evidence.isError is False
    payload = evidence.structuredContent
    assert isinstance(payload, dict)
    assert payload["schema"] == "neocortex.evidence-response/v2"
    assert payload["operation"] == "evidence"
    assert payload["exit_code"] == 0
    assert len(payload["sources"]) == len(payload["citations"]) == 1
    assert payload["citations"][0]["evidence_id"] == citation["evidence_id"]
    assert len(evidence.content) == 1
    assert evidence.content[0].type == "text"
    assert evidence.content[0].text == serialize_context_response(payload)
    wire = evidence.model_dump_json(by_alias=True, exclude_none=True)
    assert payload["budget"]["characters_used"] == len(wire) <= 12_000
    assert _fingerprints(published_text_state) == before


@pytest.mark.capability("agent")
def test_mcp_evidence_query_path_defaults_to_v2_and_keeps_v1_opt_in(
    published_text_state: Path,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api.agent_server import create_server

    server = create_server()
    metadata = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "evidence")
    properties = metadata.inputSchema["properties"]
    assert properties["response_version"]["default"] == 2
    assert properties["response_version"]["enum"] == [1, 2]

    v2 = asyncio.run(server.call_tool(
        "evidence", {"query": "protección", "citation_id": "K1", "scope": "personal"},
    ))
    assert isinstance(v2, CallToolResult)
    assert v2.isError is False
    payload = v2.structuredContent
    assert isinstance(payload, dict)
    assert payload["schema"] == "neocortex.evidence-response/v2"
    assert payload["operation"] == "evidence"
    assert payload["response_version"] == 2
    assert payload["citations"]

    v1 = asyncio.run(server.call_tool(
        "evidence", {"query": "protección", "citation_id": "K1",
                      "scope": "personal", "response_version": 1},
    ))
    assert isinstance(v1, tuple)
    assert v1[1]["schema"] == "neocortex.read-api/v1"


@pytest.mark.capability("agent")
def test_mcp_evidence_invalid_references_stay_structured_and_do_not_search(
    published_text_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api.agent_server import create_server
    from neocortex.knowledge.knowledge_service import KnowledgeSearchService

    def forbid_retrieval(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid references never authorize a fallback query")

    monkeypatch.setattr(KnowledgeSearchService, "search", forbid_retrieval)
    monkeypatch.setattr(KnowledgeSearchService, "context", forbid_retrieval)
    before = _fingerprints(published_text_state)
    server = create_server()
    result = asyncio.run(server.call_tool("evidence", {"source_ref": {}, "evidence_ref": {}}))
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = result.structuredContent
    assert isinstance(payload, dict)
    assert payload["schema"] == "neocortex.evidence-response/v2"
    assert payload["exit_code"] != 0
    assert payload["error"] is not None
    assert payload["sources"] == payload["citations"] == []
    assert payload["budget"]["characters_used"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True)
    )
    legacy = asyncio.run(server.call_tool("evidence", {"response_version": 1}))
    assert isinstance(legacy, tuple)
    assert legacy[1]["schema"] == "neocortex.read-api/v1"
    assert legacy[1]["exit_code"] == 2
    assert _fingerprints(published_text_state) == before


@pytest.mark.capability("agent")
@pytest.mark.parametrize("operation", ("context", "evidence"))
@pytest.mark.parametrize(
    ("exception", "exit_code"),
    (
        (ValueError("invalid fixture input\x1b[31m"), 2),
        (ModuleNotFoundError("No module named 'fixture'", name="fixture"), 1),
        (sqlite3.OperationalError("fixture read unavailable"), 1),
        (ReadContractError("fixture producer contract invalid"), 6),
    ),
)
def test_mcp_v2_producer_errors_remain_budgeted_structured_results(
    operation: str,
    exception: Exception,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api import agent_server

    calls = 0

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise exception

    monkeypatch.setattr(agent_server, f"{operation}_payload", fail)
    server = agent_server.create_server()
    arguments = (
        {"query": "fixture", "scope": "personal"}
        if operation == "context"
        else {"source_ref": {}, "evidence_ref": {}, "scope": "personal"}
    )
    result = asyncio.run(server.call_tool(operation, arguments))
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = result.structuredContent
    assert isinstance(payload, dict)
    assert payload["schema"] == f"neocortex.{operation}-response/v2"
    assert payload["exit_code"] == exit_code
    assert payload["sources"] == payload["citations"] == []
    assert payload["error"] is not None
    assert "\x1b" not in payload["error"]["message"]
    assert payload["scope"] == "personal"
    assert payload["query"] == ("fixture" if operation == "context" else "")
    assert payload["budget"]["characters_used"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True)
    )
    assert calls == 1


@pytest.mark.capability("agent")
@pytest.mark.parametrize("malformation", ("wrong_operation", "extra_field", "not_an_object"))
def test_mcp_v2_malformed_producer_is_a_schema_error_not_sdk_traceback(
    malformation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import CallToolResult

    from neocortex.api import agent_server
    from neocortex.knowledge.knowledge_context_v2 import (
        build_context_response_v2,
        emitted_response_characters,
    )

    payload = build_context_response_v2(
        [],
        query="fixture",
        scope="personal",
        request_id="malformed-fixture",
        transport="mcp",
        operation="evidence" if malformation == "wrong_operation" else "context",
    )
    if malformation == "extra_field":
        payload["unexpected_field"] = "must not reach the SDK converter"
        for _ in range(10):
            used = emitted_response_characters(payload, "mcp")
            if used == payload["budget"]["characters_used"]:
                break
            payload["budget"]["characters_used"] = used
    malformed = [] if malformation == "not_an_object" else payload
    monkeypatch.setattr(agent_server, "context_payload", lambda *_args, **_kwargs: malformed)
    server = agent_server.create_server()
    result = asyncio.run(server.call_tool("context", {"query": "fixture", "scope": "personal"}))
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    safe = result.structuredContent
    assert isinstance(safe, dict)
    assert safe["schema"] == "neocortex.context-response/v2"
    assert safe["exit_code"] == 6
    assert safe["error"]["code"] == "schema_incompatible"
    assert safe["sources"] == safe["citations"] == []
    assert safe["budget"]["characters_used"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True)
    )


def test_v2_preserves_required_owner_corruption_in_exit_code_and_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "corrupt-owner"
    state.mkdir()
    database = state / "pdf.sqlite3"
    database.write_bytes(b"fixture corrupt sqlite")
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state)
    before = _fingerprints(state)
    legacy = read_api.context_payload("pdf: protección", "personal", response_version=1)
    compact = read_api.context_payload("pdf: protección", "personal", response_version=2)
    assert legacy["exit_code"] == compact["exit_code"] == 7
    assert compact["status"] == "corrupt"
    assert any("pdf" in reason and "corrupt" in reason
               for reason in compact["coverage"]["retrieval"]["reasons"])
    assert compact["sources"] == compact["citations"] == []
    assert _fingerprints(state) == before


@pytest.mark.parametrize("transport", ("json", "text", "mcp"))
@pytest.mark.parametrize("length", (241, 300))
def test_v2_admits_full_evidence_when_it_costs_less_than_provisional_truncation(
    transport: str,
    length: int,
) -> None:
    from neocortex.knowledge.knowledge_context_v2 import (
        build_context_response_v2,
        emitted_response_characters,
    )

    entries = [
        {
            "scope": "personal",
            "result": {
                "complete": True,
                "rankings": [],
                "snapshot": {"snapshot_id": "fixture", "consistency": "stable", "owners": []},
                "hits": [
                    {
                        "resource": {
                            "resource_id": "resource:fixture",
                            "owner": "pdf",
                            "source_kind": "pdf",
                            "current_path": "/fixture/report.pdf",
                        },
                        "revision": {
                            "revision_id": "revision:fixture",
                            "state": "current",
                            "processing_signature": "pdf:fixture",
                        },
                        "evidence": {
                            "evidence_id": "evidence:fixture",
                            "method": "extracted",
                            "page": 0,
                            "snippet": "x" * length,
                        },
                    }
                ],
            },
        }
    ]
    options = {"query": "q", "scope": "personal", "request_id": "fixed", "transport": transport}
    full = build_context_response_v2(entries, max_characters=12_000, **options)
    assert full["citations"][0]["fragment_state"] == "full"
    known_fitting_limit = full["budget"]["characters_used"]
    tight = build_context_response_v2(entries, max_characters=known_fitting_limit, **options)
    assert len(tight["citations"]) == 1
    assert tight["citations"][0]["fragment_state"] == "full"
    assert tight["citations"][0]["excerpt"] == "x" * length
    assert tight["exit_code"] == 0
    assert tight["budget"]["characters_used"] == emitted_response_characters(tight, transport)
    assert tight["budget"]["characters_used"] <= known_fitting_limit
