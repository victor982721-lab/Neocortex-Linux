"""Small, dependency-free contract for public NeoCortex reads.

The product has a number of historical adapters (the flat CLI, human CLI,
MCP and the Qt read client).  They all return the same logical data, but used
to validate different subsets of it.  This module is intentionally limited to
the wire contract: it does not import a route, a database owner, or an
optional engine.  Producers can therefore use it without changing the
read-only/storage boundary.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
import math
import re
from typing import Any, Literal, NotRequired, Required, TypedDict
from uuid import uuid4


READ_CONTRACT_SCHEMA = "neocortex.read-api/v1"
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Result payloads are ultimately rendered by a terminal, Qt widget or MCP
# client.  Keep the defensive copy bounded independently from each adapter so
# a malformed producer cannot turn a read into an unbounded rendering job.
MAX_SANITIZED_PAYLOAD_NODES = 20_000
MAX_SANITIZED_PAYLOAD_DEPTH = 16
MAX_SANITIZED_PAYLOAD_STRING = 128_000
_SANITIZED_PAYLOAD_TRUNCATION = "[contenido omitido por límite]"
_SANITIZED_PAYLOAD_TRUNCATION_KEY = "__neocortex_sanitization_truncated__"

ReadScopeName = Literal["personal", "framework", "all"]
ReadApiSchema = Literal["neocortex.read-api/v1"]
ReadExitCodeValue = Literal[0, 1, 2, 3, 4, 5, 6, 7, 130]


class ReadExitCode(IntEnum):
    """Codes understood by every public read adapter.

    ``FATAL`` remains part of the compatibility set because existing direct
    readers return it.  New envelopes should prefer ``USAGE`` for malformed
    requests and one of the typed state codes for a published-state outcome.
    """

    SUCCESS = 0
    FATAL = 1
    USAGE = 2
    NO_RESULTS = 3
    PARTIAL = 4
    SNAPSHOT_CHANGED = 5
    SCHEMA_INCOMPATIBLE = 6
    CORRUPT = 7
    CANCELLED = 130


class ReadCoverage(StrEnum):
    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class ReadOperation(StrEnum):
    STATUS = "status"
    SEARCH = "search"
    CONTEXT = "context"
    EVIDENCE = "evidence"
    LINEAGE = "lineage"
    ASSET_HEALTH = "asset_health"
    OPERATIONAL_QUERY = "operational_query"


@dataclass(frozen=True, slots=True)
class ReadOperationDescriptor:
    """Stable identity and validation rules for one read operation."""

    operation: ReadOperation
    schema: str
    kind: str
    query: bool = False
    mode: bool = False
    history: bool = False
    limit: bool = False


READ_OPERATION_DESCRIPTORS: dict[ReadOperation, ReadOperationDescriptor] = {
    ReadOperation.STATUS: ReadOperationDescriptor(
        ReadOperation.STATUS,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_status",
    ),
    ReadOperation.SEARCH: ReadOperationDescriptor(
        ReadOperation.SEARCH,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_search",
        query=True,
        mode=True,
        history=True,
        limit=True,
    ),
    ReadOperation.CONTEXT: ReadOperationDescriptor(
        ReadOperation.CONTEXT,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_context",
        query=True,
        mode=True,
        history=True,
        limit=True,
    ),
    ReadOperation.EVIDENCE: ReadOperationDescriptor(
        ReadOperation.EVIDENCE,
        READ_CONTRACT_SCHEMA,
        "neocortex_evidence",
        query=True,
        limit=True,
    ),
    ReadOperation.LINEAGE: ReadOperationDescriptor(
        ReadOperation.LINEAGE,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_derivation_lineage",
    ),
    ReadOperation.ASSET_HEALTH: ReadOperationDescriptor(
        ReadOperation.ASSET_HEALTH,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_asset_health",
    ),
    ReadOperation.OPERATIONAL_QUERY: ReadOperationDescriptor(
        ReadOperation.OPERATIONAL_QUERY,
        READ_CONTRACT_SCHEMA,
        "neocortex_scoped_operational_query",
        query=True,
        limit=True,
    ),
}


class ReadErrorPayload(TypedDict, total=False):
    code: Required[str]
    message: Required[str]
    retryable: NotRequired[bool]
    scope: NotRequired[ReadScopeName]


class ReadScopePayload(TypedDict, total=False):
    scope: Required[ReadScopeName]
    status: Required[str]
    exit_code: Required[ReadExitCodeValue]
    state_directory: NotRequired[str]
    error_type: NotRequired[str]
    reason: NotRequired[str]
    snapshot: NotRequired[dict[str, Any]]
    result: NotRequired[dict[str, Any]]
    context: NotRequired[dict[str, Any]]
    asset_health: NotRequired[dict[str, Any]]
    lineage: NotRequired[dict[str, Any]]
    hits: NotRequired[list[dict[str, Any]]]
    report: NotRequired[dict[str, Any]]
    operational: NotRequired[dict[str, Any]]
    refresh: NotRequired[dict[str, Any]]
    source: NotRequired[str]


class ReadEnvelopePayload(TypedDict, total=False):
    schema: Required[str]
    kind: Required[str]
    operation: Required[str]
    request_id: Required[str]
    scope: Required[ReadScopeName]
    scope_requested: Required[ReadScopeName]
    read_only: Required[Literal[True]]
    coverage: Required[str]
    status: Required[str]
    exit_code: Required[ReadExitCodeValue]
    error: Required[ReadErrorPayload | None]
    result: Required[dict[str, Any]]
    scopes: Required[list[ReadScopePayload]]
    federation_policy: NotRequired[str]
    query: NotRequired[str]
    mode: NotRequired[str]
    include_history: NotRequired[bool]
    limit_per_scope: NotRequired[int]
    max_characters_per_scope: NotRequired[int]
    cursor: NotRequired[str | None]
    citation_id: NotRequired[str]
    found: NotRequired[bool]
    resource_id: NotRequired[str]
    identifier: NotRequired[str]
    observed_epoch: NotRequired[dict[str, Any]]
    modes: NotRequired[list[str]]
    operation_alias: NotRequired[str]
    advisory_only: NotRequired[bool]
    mutation_authorized: NotRequired[bool]


class StatusOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for the status envelope."""


class SearchOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for the search envelope."""


class ContextOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for the context envelope."""


class EvidenceOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for the evidence envelope."""


class LineageOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for derivation-lineage reads."""


class AssetHealthOutput(ReadEnvelopePayload, total=False):
    """Compatibility alias for asset-health reads."""


_VALID_EXIT_CODES = frozenset(int(value) for value in ReadExitCode)
_VALID_SCOPES = frozenset(("personal", "framework", "all"))
_VALID_MODES = frozenset(("evidence", "discovery"))


class ReadContractError(ValueError):
    """A producer or adapter returned data outside the read contract."""


def sanitize_untrusted_text(
    value: object,
    *,
    limit: int | None = 800,
    single_line: bool = True,
) -> str:
    """Make corpus-derived text safe for terminal and selectable UI output."""

    text = _ANSI_ESCAPE.sub("", str(value or ""))
    text = "".join(
        "\n" if not single_line and char in "\n\r" else
        " " if ord(char) < 32 or ord(char) == 127 else char
        for char in text
    )
    if single_line:
        text = " ".join(text.split())
    if limit is None or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def sanitize_untrusted_payload(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    """Copy a producer result into a bounded JSON/render-safe value.

    The read contract deliberately permits extensible result objects, so a
    structural validator cannot enumerate every nested field.  This helper is
    the common last-mile boundary for terminal, UI and MCP adapters: strings
    lose ANSI/C0 controls, object keys become JSON-safe strings, non-finite
    floats are replaced, and unknown objects cannot escape as renderer-hostile
    values.  A fixed omission string is transport diagnostics only; it never
    authorizes an action.  Producer keys that collide after sanitization are
    rejected rather than overwriting data or inventing a new field identity.
    Omission markers use the next deterministic suffix when their key is taken.
    """

    if budget is None:
        budget = [MAX_SANITIZED_PAYLOAD_NODES]
    if budget[0] <= 0:
        return _SANITIZED_PAYLOAD_TRUNCATION
    budget[0] -= 1
    if depth > MAX_SANITIZED_PAYLOAD_DEPTH:
        return _SANITIZED_PAYLOAD_TRUNCATION
    if isinstance(value, str):
        return sanitize_untrusted_text(
            value,
            limit=MAX_SANITIZED_PAYLOAD_STRING,
            single_line=False,
        )
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        expected_items = len(value)
        items = iter(value.items())
        processed = 0
        while processed < expected_items and budget[0] > 0:
            try:
                key, item = next(items)
            except StopIteration:
                break
            processed += 1
            safe_key = sanitize_untrusted_text(str(key), limit=512, single_line=True)
            if safe_key in result:
                raise ReadContractError("payload object keys collide after sanitization")
            result[safe_key] = sanitize_untrusted_payload(item, depth=depth + 1, budget=budget)
        if processed < expected_items:
            marker_key = _SANITIZED_PAYLOAD_TRUNCATION_KEY
            suffix = 2
            while marker_key in result:
                marker_key = f"{_SANITIZED_PAYLOAD_TRUNCATION_KEY}#{suffix}"
                suffix += 1
            result[marker_key] = _SANITIZED_PAYLOAD_TRUNCATION
        return result
    if isinstance(value, (list, tuple)):
        expected_items = len(value)
        items = iter(value)
        result_list: list[object] = []
        processed = 0
        while processed < expected_items and budget[0] > 0:
            try:
                item = next(items)
            except StopIteration:
                break
            processed += 1
            result_list.append(
                sanitize_untrusted_payload(item, depth=depth + 1, budget=budget)
            )
        if processed < expected_items:
            result_list.append(_SANITIZED_PAYLOAD_TRUNCATION)
        return result_list
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return sanitize_untrusted_text(value, limit=MAX_SANITIZED_PAYLOAD_STRING, single_line=False)


def expected_read_outcome(code: int) -> tuple[str, str]:
    """Return the only valid coverage/status pair for one public exit code."""

    return _coverage_for_code(code).value, _status_for_code(code)


def _operation_descriptor(operation: str | ReadOperation) -> ReadOperationDescriptor:
    try:
        selected = operation if isinstance(operation, ReadOperation) else ReadOperation(operation)
    except (TypeError, ValueError) as exc:
        raise ReadContractError(f"unsupported read operation: {operation!r}") from exc
    return READ_OPERATION_DESCRIPTORS[selected]


def _as_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReadContractError(f"{label} must be an object")
    return value


def _valid_exit_code(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in _VALID_EXIT_CODES:
        raise ReadContractError(f"{label} must be a supported integer exit code")
    return value


def _valid_scope(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value not in _VALID_SCOPES:
        raise ReadContractError(f"{label} must be personal, framework or all")
    return value


def _valid_text(value: object, *, label: str, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ReadContractError(f"{label} must be a non-empty string")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise ReadContractError(f"{label} contains a control character")
    return value


def _valid_limit(value: object, *, label: str = "limit_per_scope") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ReadContractError(f"{label} must be between 1 and 100")
    return value


def _coverage_for_code(code: int) -> ReadCoverage:
    return {
        int(ReadExitCode.SUCCESS): ReadCoverage.COMPLETE,
        int(ReadExitCode.NO_RESULTS): ReadCoverage.EMPTY,
        int(ReadExitCode.PARTIAL): ReadCoverage.PARTIAL,
        int(ReadExitCode.SNAPSHOT_CHANGED): ReadCoverage.BLOCKED,
        int(ReadExitCode.SCHEMA_INCOMPATIBLE): ReadCoverage.UNAVAILABLE,
        int(ReadExitCode.CORRUPT): ReadCoverage.UNAVAILABLE,
        int(ReadExitCode.FATAL): ReadCoverage.UNAVAILABLE,
        int(ReadExitCode.USAGE): ReadCoverage.UNAVAILABLE,
        int(ReadExitCode.CANCELLED): ReadCoverage.BLOCKED,
    }[code]


def _status_for_code(code: int) -> str:
    return {
        int(ReadExitCode.SUCCESS): "ok",
        int(ReadExitCode.NO_RESULTS): "empty",
        int(ReadExitCode.PARTIAL): "partial",
        int(ReadExitCode.SNAPSHOT_CHANGED): "snapshot_changed",
        int(ReadExitCode.SCHEMA_INCOMPATIBLE): "schema_incompatible",
        int(ReadExitCode.CORRUPT): "corrupt",
        int(ReadExitCode.FATAL): "error",
        int(ReadExitCode.USAGE): "usage_error",
        int(ReadExitCode.CANCELLED): "cancelled",
    }[code]


def _error_from_scopes(scopes: list[Mapping[str, object]]) -> ReadErrorPayload | None:
    for entry in scopes:
        code = _valid_exit_code(entry.get("exit_code"), label="scope.exit_code")
        if code in {int(ReadExitCode.SUCCESS), int(ReadExitCode.NO_RESULTS)}:
            continue
        reason = entry.get("reason") or entry.get("error_type") or _status_for_code(code)
        message = sanitize_untrusted_text(reason, limit=800)
        if not message:
            message = _status_for_code(code)
        scope = entry.get("scope")
        result: ReadErrorPayload = {"code": _status_for_code(code), "message": message}
        if isinstance(scope, str) and scope in _VALID_SCOPES:
            result["scope"] = scope  # type: ignore[typeddict-item]
        result["retryable"] = code in {
            int(ReadExitCode.PARTIAL),
            int(ReadExitCode.SNAPSHOT_CHANGED),
        }
        return result
    return None


def normalize_read_payload(
    value: object,
    operation: str | ReadOperation,
    *,
    scope: str | None = None,
    request_id: str | None = None,
    allow_legacy_identity: bool = False,
) -> dict[str, object]:
    """Add the v1 envelope fields while retaining legacy operation fields.

    ``allow_legacy_identity`` is only for the MCP adapter during the gradual
    migration: old test doubles and old third-party adapters may omit the
    identity fields, but the result is still validated before being exposed.
    The shared client uses the strict default.
    """

    if isinstance(value, Mapping) and value.get("schema") in {
        "neocortex.context-response/v2", "neocortex.evidence-response/v2",
    }:
        return validate_read_payload(value, operation, scope=scope, strict_echo=False)

    descriptor = _operation_descriptor(operation)
    payload = dict(_as_mapping(value, label="read payload"))
    selected_scope = scope or payload.get("scope_requested")
    if selected_scope is None and allow_legacy_identity:
        selected_scope = "all"
    if "schema" not in payload and allow_legacy_identity:
        payload["schema"] = descriptor.schema
    if "kind" not in payload and allow_legacy_identity:
        payload["kind"] = descriptor.kind
    if "read_only" not in payload and allow_legacy_identity:
        payload["read_only"] = True
    if "scope_requested" not in payload and allow_legacy_identity:
        payload["scope_requested"] = selected_scope
    if "scopes" not in payload and allow_legacy_identity:
        payload["scopes"] = []

    code = _valid_exit_code(payload.get("exit_code", int(ReadExitCode.FATAL)), label="exit_code")
    scopes_value = payload.get("scopes")
    scopes = scopes_value if isinstance(scopes_value, list) else []
    normalized: dict[str, object] = payload
    if allow_legacy_identity:
        normalized.setdefault("exit_code", code)
        normalized.setdefault("scope_requested", selected_scope or "all")
        normalized.setdefault("read_only", True)
        normalized.setdefault("schema", descriptor.schema)
        normalized.setdefault("kind", descriptor.kind)
    normalized.setdefault("operation", descriptor.operation.value)
    normalized.setdefault("request_id", request_id or f"read-{uuid4().hex}")
    normalized.setdefault("scope", selected_scope or "all")
    normalized.setdefault("coverage", _coverage_for_code(code).value)
    normalized.setdefault("status", _status_for_code(code))
    normalized.setdefault("error", _error_from_scopes([_as_mapping(item, label="scope") for item in scopes]) if scopes else None)
    normalized.setdefault("result", {"scopes": scopes})
    # Older producers did not expose publication epochs.  Preserve their
    # compatibility while making the omission explicit rather than allowing a
    # consumer to mistake an unobserved epoch for a stable one.
    normalized.setdefault(
        "observed_epoch",
        {
            "schema": "neocortex.read-observed-epoch/v1",
            "status": "unavailable",
            "reason": "producer_did_not_report_epoch",
        },
    )
    return normalized


def validate_read_payload(
    value: object,
    operation: str | ReadOperation,
    *,
    scope: str | None = None,
    query: str | None = None,
    mode: str | None = None,
    include_history: bool | None = None,
    limit: int | None = None,
    strict_echo: bool = True,
) -> dict[str, object]:
    """Validate a complete public response and return a plain dict copy."""

    descriptor = _operation_descriptor(operation)
    if isinstance(value, Mapping) and value.get("schema") in {
        "neocortex.context-response/v2", "neocortex.evidence-response/v2",
    }:
        from neocortex.knowledge.knowledge_context_v2 import validate_context_response

        try:
            compact = validate_context_response(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReadContractError(str(exc)) from exc
        if compact.get("operation") != descriptor.operation.value:
            raise ReadContractError("compact response operation differs from requested operation")
        if scope is not None and compact.get("scope") != scope:
            raise ReadContractError("compact response scope differs from requested scope")
        for key, expected in (("query", query), ("mode", mode),
                              ("include_history", include_history), ("limit_per_scope", limit)):
            if expected is not None and strict_echo and compact.get(key) != expected:
                raise ReadContractError(f"compact response {key} differs from request")
        return compact
    payload = dict(_as_mapping(value, label="read payload"))
    if payload.get("schema") != descriptor.schema or payload.get("kind") != descriptor.kind:
        raise ReadContractError("read payload schema or kind is incompatible")
    if payload.get("read_only") is not True:
        raise ReadContractError("read payload did not confirm read_only=true")
    requested = _valid_scope(payload.get("scope_requested"), label="scope_requested")
    if scope is not None and requested != scope:
        raise ReadContractError("read payload scope differs from requested scope")
    if payload.get("scope") is not None and payload.get("scope") != requested:
        raise ReadContractError("read payload scope alias differs from scope_requested")
    operation_value = _valid_text(payload.get("operation"), label="operation")
    valid_operations = {descriptor.operation.value}
    if operation_value not in valid_operations:
        raise ReadContractError("read payload operation differs from requested operation")
    _valid_text(payload.get("request_id"), label="request_id")
    code = _valid_exit_code(payload.get("exit_code"), label="exit_code")
    expected_coverage, expected_status = expected_read_outcome(code)
    if payload.get("coverage") != expected_coverage:
        raise ReadContractError("status/coverage is incompatible with exit_code")
    status_value = payload.get("status")
    if status_value != expected_status:
        raise ReadContractError("status/coverage is incompatible with exit_code")
    scopes_value = payload.get("scopes")
    if not isinstance(scopes_value, list):
        raise ReadContractError("scopes must be a list")
    seen_scopes: set[str] = set()
    scope_entries: list[Mapping[str, object]] = []
    for item in scopes_value:
        entry = _as_mapping(item, label="scope entry")
        entry_scope = _valid_scope(entry.get("scope"), label="scope entry.scope")
        if entry_scope in seen_scopes:
            raise ReadContractError(f"scope {entry_scope!r} appears more than once")
        seen_scopes.add(entry_scope)
        _valid_text(entry.get("status"), label="scope entry.status")
        _valid_exit_code(entry.get("exit_code"), label="scope entry.exit_code")
        scope_entries.append(entry)
    if not isinstance(payload.get("result"), Mapping):
        raise ReadContractError("result must be an object")
    if "observed_epoch" in payload and not isinstance(payload.get("observed_epoch"), Mapping):
        raise ReadContractError("observed_epoch must be an object")
    if descriptor.query:
        response_query_value = payload.get("query")
        if response_query_value is None and not strict_echo:
            response_query = None
        else:
            response_query = _valid_text(response_query_value, label="query")
        if query is not None and response_query != query:
            raise ReadContractError("read payload query differs from requested query")
    if descriptor.mode:
        response_mode = payload.get("mode")
        if response_mode is None and not strict_echo:
            response_mode = None
        elif not isinstance(response_mode, str) or response_mode not in _VALID_MODES:
            raise ReadContractError("mode must be evidence or discovery")
        if mode is not None and response_mode != mode:
            raise ReadContractError("read payload mode differs from requested mode")
    if descriptor.history:
        history = payload.get("include_history")
        if history is None and not strict_echo:
            history = None
        elif not isinstance(history, bool):
            raise ReadContractError("include_history must be a bool")
        if include_history is not None and history != include_history:
            raise ReadContractError("read payload include_history differs from request")
    if descriptor.limit:
        response_limit_value = payload.get("limit_per_scope")
        if response_limit_value is None and not strict_echo:
            response_limit = None
        else:
            response_limit = _valid_limit(response_limit_value)
        if limit is not None and response_limit != limit:
            raise ReadContractError("read payload limit differs from request")
    if code in {int(ReadExitCode.SUCCESS), int(ReadExitCode.NO_RESULTS)}:
        if payload.get("error") is not None:
            raise ReadContractError("read payload error is incompatible with a successful or empty outcome")
    elif payload.get("error") is None:
        raise ReadContractError("read payload must describe an incomplete outcome")
    if payload.get("error") is not None:
        error = _as_mapping(payload["error"], label="error")
        _valid_text(error.get("code"), label="error.code")
        _valid_text(error.get("message"), label="error.message")
        if "retryable" in error and not isinstance(error["retryable"], bool):
            raise ReadContractError("error.retryable must be a bool")
    return payload


def make_error_payload(
    operation: str | ReadOperation,
    *,
    scope: str = "all",
    code: ReadExitCode = ReadExitCode.SCHEMA_INCOMPATIBLE,
    message: str,
) -> dict[str, object]:
    """Build a safe structured failure instead of leaking a tool exception."""

    descriptor = _operation_descriptor(operation)
    normalized_scope = _valid_scope(scope, label="scope")
    bounded_message = sanitize_untrusted_text(message, limit=800)
    if not bounded_message:
        bounded_message = _status_for_code(int(code))
    payload: dict[str, object] = {
        "schema": descriptor.schema,
        "kind": descriptor.kind,
        "operation": descriptor.operation.value,
        "request_id": f"read-{uuid4().hex}",
        "scope": normalized_scope,
        "scope_requested": normalized_scope,
        "read_only": True,
        "coverage": _coverage_for_code(int(code)).value,
        "status": _status_for_code(int(code)),
        "exit_code": int(code),
        "error": {"code": _status_for_code(int(code)), "message": bounded_message},
        "result": {"scopes": []},
        "scopes": [],
    }
    return payload


__all__ = [
    "MAX_SANITIZED_PAYLOAD_DEPTH",
    "MAX_SANITIZED_PAYLOAD_NODES",
    "MAX_SANITIZED_PAYLOAD_STRING",
    "READ_CONTRACT_SCHEMA",
    "READ_OPERATION_DESCRIPTORS",
    "AssetHealthOutput",
    "ContextOutput",
    "EvidenceOutput",
    "LineageOutput",
    "ReadContractError",
    "ReadCoverage",
    "ReadEnvelopePayload",
    "ReadErrorPayload",
    "ReadExitCode",
    "ReadExitCodeValue",
    "ReadOperation",
    "ReadOperationDescriptor",
    "ReadScopeName",
    "ReadScopePayload",
    "SearchOutput",
    "StatusOutput",
    "expected_read_outcome",
    "make_error_payload",
    "normalize_read_payload",
    "sanitize_untrusted_payload",
    "sanitize_untrusted_text",
    "validate_read_payload",
]
