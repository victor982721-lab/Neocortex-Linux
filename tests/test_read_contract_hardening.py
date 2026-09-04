"""Regression contracts for the shared read envelope boundary."""

from __future__ import annotations

import asyncio
import json

import pytest

from neocortex.api.read_contract import (
    READ_CONTRACT_SCHEMA,
    ReadContractError,
    ReadOperation,
    normalize_read_payload,
    sanitize_untrusted_payload,
    validate_read_payload,
)


def _status_payload(*, exit_code: int = 0) -> dict[str, object]:
    return {
        "schema": READ_CONTRACT_SCHEMA,
        "kind": "neocortex_scoped_status",
        "operation": "status",
        "request_id": "fixture-read-1",
        "scope": "personal",
        "scope_requested": "personal",
        "read_only": True,
        "coverage": "complete" if exit_code == 0 else "partial",
        "status": "ok" if exit_code == 0 else "partial",
        "exit_code": exit_code,
        "error": None if exit_code == 0 else {"code": "partial", "message": "partial"},
        "result": {"scopes": []},
        "scopes": [],
    }


def test_validator_rejects_status_and_coverage_that_lie_about_exit_code() -> None:
    payload = _status_payload(exit_code=4)
    payload["coverage"] = "complete"

    with pytest.raises(ReadContractError, match="status/coverage"):
        validate_read_payload(payload, ReadOperation.STATUS, scope="personal")

    payload = _status_payload(exit_code=4)
    payload["status"] = "ok"
    with pytest.raises(ReadContractError, match="status/coverage"):
        validate_read_payload(payload, ReadOperation.STATUS, scope="personal")


def test_legacy_normalization_explicitly_marks_epoch_unavailable() -> None:
    payload = normalize_read_payload(
        {"kind": "neocortex_scoped_status", "read_only": True, "scopes": []},
        ReadOperation.STATUS,
        scope="personal",
        allow_legacy_identity=True,
    )

    assert payload["observed_epoch"] == {
        "schema": "neocortex.read-observed-epoch/v1",
        "status": "unavailable",
        "reason": "producer_did_not_report_epoch",
    }


def test_payload_sanitizer_handles_non_json_keys_nonfinite_values_and_objects() -> None:
    class _Opaque:
        def __str__(self) -> str:
            return "opaque\x1b[31m"

    sanitized = sanitize_untrusted_payload(
        {0: {"snippet": "safe\x1b[31m\ntext"}, "nan": float("nan"), "opaque": _Opaque()}
    )

    assert sanitized == {
        "0": {"snippet": "safe\ntext"},
        "nan": None,
        "opaque": "opaque",
    }
    json.dumps(sanitized, allow_nan=False)


def test_mcp_input_schema_rejects_whitespace_only_text() -> None:
    pytest.importorskip("mcp")
    from neocortex.api.agent_server import create_server

    server = create_server()
    tools = asyncio.run(server.list_tools())
    search = next(tool for tool in tools if tool.name == "search")
    query_schema = search.inputSchema["properties"]["query"]
    assert query_schema["pattern"] == r"(?s).*\S.*"
