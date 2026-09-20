from __future__ import annotations


import asyncio
import json
from collections.abc import Callable

import pytest

from neocortex.api.read_contract import (
    READ_CONTRACT_SCHEMA,
    ReadContractError,
    ReadExitCode,
    ReadOperation,
    make_error_payload,
    normalize_read_payload,
    sanitize_untrusted_text,
    validate_read_payload,
)


TEST_CAPABILITIES = ('base', 'agent')


def _search_payload() -> dict[str, object]:
    return {
        "schema": READ_CONTRACT_SCHEMA,
        "kind": "neocortex_scoped_search",
        "operation": "search",
        "request_id": "read-fixture-1",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "coverage": "complete",
        "status": "ok",
        "exit_code": int(ReadExitCode.SUCCESS),
        "error": None,
        "result": {"scopes": []},
        "scopes": [],
        "query": "relay",
        "mode": "evidence",
        "include_history": False,
        "limit_per_scope": 4,
    }


def test_v1_envelope_validates_echoes_and_scopes() -> None:
    payload = _search_payload()

    validated = validate_read_payload(
        payload,
        ReadOperation.SEARCH,
        scope="personal",
        query="relay",
        mode="evidence",
        include_history=False,
        limit=4,
    )

    assert validated is not payload
    assert validated["request_id"] == "read-fixture-1"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.update(query="other"),
        lambda payload: payload.update(mode="bogus"),
        lambda payload: payload.update(include_history="false"),
        lambda payload: payload.update(limit_per_scope=101),
        lambda payload: payload.update(scopes=[{}]),
        lambda payload: payload.update(exit_code=True),
    ),
)
def test_v1_envelope_rejects_malformed_machine_payloads(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    payload = _search_payload()
    mutate(payload)

    with pytest.raises(ReadContractError):
        validate_read_payload(
            payload,
            ReadOperation.SEARCH,
            scope="personal",
            query="relay",
            mode="evidence",
            include_history=False,
            limit=4,
        )


def test_legacy_payload_is_normalized_without_repairing_present_identity() -> None:
    payload = normalize_read_payload(
        {"kind": "neocortex_scoped_status", "read_only": True, "scopes": []},
        ReadOperation.STATUS,
        scope="framework",
        allow_legacy_identity=True,
    )

    assert payload["schema"] == READ_CONTRACT_SCHEMA
    assert payload["kind"] == "neocortex_scoped_status"
    assert payload["scope_requested"] == "framework"
    assert payload["read_only"] is True
    assert isinstance(payload["request_id"], str)
    assert payload["result"] == {"scopes": []}


def test_structured_error_is_bounded_and_machine_readable() -> None:
    payload = make_error_payload(
        ReadOperation.SEARCH,
        scope="all",
        message="bad\x1b[31m\ninput",
    )

    assert payload["exit_code"] == int(ReadExitCode.SCHEMA_INCOMPATIBLE)
    assert payload["read_only"] is True
    assert "\x1b" not in json.dumps(payload)
    assert len(payload["error"]["message"]) < 800  # type: ignore[index]


def test_untrusted_text_removes_terminal_controls_without_flattening_json() -> None:
    assert sanitize_untrusted_text("a\x1b[31mb\x1b[0m\nforged") == "ab forged"
    assert sanitize_untrusted_text("a\x1b[31mb\x1b[0m\nforged", limit=None, single_line=False) == (
        "ab\nforged"
    )


def test_read_contract_rejects_a_complete_payload_with_wrong_query() -> None:
    payload = _search_payload()

    with pytest.raises(ReadContractError, match="query"):
        validate_read_payload(
            payload,
            ReadOperation.SEARCH,
            scope="personal",
            query="breaker",
            mode="evidence",
            include_history=False,
            limit=4,
        )


def test_no_operation_does_not_start_an_inventory_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from neocortex.api.cli import cli_app

    monkeypatch.setattr(cli_app, "run_framework", lambda *_args, **_kwargs: pytest.fail("framework started"))

    assert cli_app.main([]) == 0
    output = capsys.readouterr().out
    assert "operación directa" in output


@pytest.mark.parametrize(
    "arguments",
    (
        ("--pdf-search", "relay", "--route", "pdf"),
        ("--docx-search", "relay", "--route", "docx"),
        ("--office-search", "relay", "--route", "office"),
    ),
)
def test_direct_format_reads_reject_route_mix(arguments: tuple[str, ...]) -> None:
    from neocortex.api.cli.cli_parser import build_parser
    from neocortex.api.cli.cli_validation import validate_arguments

    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match="cannot be combined with --route"):
        validate_arguments(args)


@pytest.mark.capability('agent')
def test_mcp_exposes_bounded_inputs_and_typed_output_schema() -> None:
    from neocortex.api import agent_server

    server = agent_server.create_server()
    tools = asyncio.run(server.list_tools())
    search = next(tool for tool in tools if tool.name == "search")

    assert search.inputSchema["properties"]["scope"]["enum"] == [
        "personal",
        "framework",
        "all",
    ]
    assert search.inputSchema["properties"]["limit"]["minimum"] == 1
    assert search.inputSchema["properties"]["limit"]["maximum"] == 100
    assert search.outputSchema is not None
    assert "operation" in search.outputSchema["required"]
    assert search.outputSchema.get("additionalProperties") is not True
