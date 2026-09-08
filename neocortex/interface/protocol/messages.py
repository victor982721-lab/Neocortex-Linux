"""Bounded and typed line protocol shared by the Qt controller and worker.

The desktop worker is a separate process, so its stdout is an untrusted
transport rather than an in-process callback.  This module keeps the wire
format deliberately small, validates every lifecycle record, and provides a
stateful validator for ordering and terminal-state guarantees.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, Required, TypedDict, cast

from neocortex.progress import ProgressEvent


# region [01] Wire format and type contracts

PROTOCOL_VERSION = 1
MESSAGE_PREFIX = "@neocortex-ui/v1 "
MAX_MESSAGE_BYTES = 1_000_000
MAX_SEQUENCE = 10_000_000
MAX_WORKER_RUN_ID_LENGTH = 128
MAX_TEXT_FIELD_LENGTH = 8_192
MAX_TRACEBACK_LENGTH = 20_000
MAX_PROGRESS_METRICS = 32
MAX_HEARTBEAT_ITEMS = 24

WorkerMessageType = Literal[
    "started",
    "progress",
    "heartbeat",
    "cancel_acknowledged",
    "completed",
    "cancelled",
    "failed",
]
TerminalMessageType = Literal["completed", "cancelled", "failed"]

OUTPUT_MESSAGE_TYPES = frozenset(
    {
        "started",
        "progress",
        "heartbeat",
        "cancel_acknowledged",
        "completed",
        "cancelled",
        "failed",
    }
)
TERMINAL_MESSAGE_TYPES = frozenset({"completed", "cancelled", "failed"})


class WorkerMessage(TypedDict, total=False):
    """Typed superset of records emitted by the supervised worker.

    Lifecycle-specific fields are optional at the type level because the
    transport also carries a few compatibility records.  ``validate_message``
    applies the required fields for the selected ``type`` at runtime.
    """

    protocol: Required[int]
    type: Required[WorkerMessageType]
    worker_run_id: Required[str]
    sequence: Required[int]
    request_id: NotRequired[str]
    operation: NotRequired[str]
    phase: NotRequired[str]
    description: NotRequired[str]
    completed: NotRequired[int]
    total: NotRequired[int | None]
    unit: NotRequired[str]
    finished: NotRequired[bool]
    metrics: NotRequired[dict[str, int | float | str | bool | None]]
    elapsed_seconds: NotRequired[int]
    active: NotRequired[list[dict[str, Any]]]
    root: NotRequired[str]
    state_directory: NotRequired[str]
    apply: NotRequired[bool]
    route: NotRequired[str]
    profile: NotRequired[str]
    max_items: NotRequired[int]
    deadline_seconds: NotRequired[float]
    run_id: NotRequired[int]
    files_checked: NotRequired[int]
    action_errors: NotRequired[int]
    route_errors: NotRequired[dict[str, int]]
    organization_errors: NotRequired[bool]
    issues: NotRequired[int]
    completion_status: NotRequired[str]
    exit_code: NotRequired[int]
    detail: NotRequired[str]
    error_type: NotRequired[str]
    stage: NotRequired[str]
    traceback: NotRequired[str]


class WorkerProtocolError(ValueError):
    """A malformed, unsupported, or out-of-order worker record."""


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def sanitize_text(value: object, *, limit: int = MAX_TEXT_FIELD_LENGTH) -> str:
    """Return bounded display text with terminal controls and ANSI removed."""

    text = _ANSI_ESCAPE.sub("", str(value))
    # Keep ordinary whitespace useful in labels, but never permit control
    # characters to forge additional terminal/UI records.
    text = "".join(
        character for character in text if character in {"\t", "\n", "\r"} or ord(character) >= 0x20
    )
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _require_text(
    record: Mapping[str, Any],
    name: str,
    *,
    limit: int = MAX_TEXT_FIELD_LENGTH,
    required: bool = True,
    allow_line_breaks: bool = False,
) -> str | None:
    value = record.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WorkerProtocolError(f"worker record field {name!r} must be a non-empty string")
    allowed_controls = {"\t", "\n", "\r"} if allow_line_breaks else set()
    if len(value) > limit or any(
        ord(character) < 0x20 and character not in allowed_controls for character in value
    ):
        raise WorkerProtocolError(f"worker record field {name!r} exceeds its safe text bound")
    return value


def _require_int(
    record: Mapping[str, Any],
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    required: bool = True,
) -> int | None:
    value = record.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerProtocolError(f"worker record field {name!r} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise WorkerProtocolError(f"worker record field {name!r} is outside its safe bound")
    return value


def _require_number(
    record: Mapping[str, Any],
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
    required: bool = True,
) -> float | None:
    value = record.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerProtocolError(f"worker record field {name!r} must be numeric")
    numeric = float(value)
    if (
        not math.isfinite(numeric)
        or numeric < minimum
        or (maximum is not None and numeric > maximum)
    ):
        raise WorkerProtocolError(f"worker record field {name!r} is outside its safe bound")
    return numeric


def _validate_metrics(value: object) -> None:
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker progress metrics must be an object")
    if len(value) > MAX_PROGRESS_METRICS:
        raise WorkerProtocolError("worker progress metrics exceed the bounded field count")
    for name, metric in value.items():
        if not isinstance(name, str) or not name or len(name) > 96:
            raise WorkerProtocolError("worker progress metric names must be bounded strings")
        if any(ord(character) < 0x20 for character in name):
            raise WorkerProtocolError("worker progress metric names cannot contain controls")
        if metric is not None and not isinstance(metric, (bool, int, float, str)):
            raise WorkerProtocolError("worker progress metric values must be scalar")
        if isinstance(metric, float) and not math.isfinite(metric):
            raise WorkerProtocolError("worker progress metric values must be finite")


def _validate_progress_fields(record: Mapping[str, Any]) -> None:
    _require_text(record, "operation")
    _require_text(record, "phase")
    _require_text(record, "description")
    _require_text(record, "unit")
    _require_int(record, "completed", maximum=2**63 - 1)
    _require_int(record, "total", maximum=2**63 - 1, required=False)
    if not isinstance(record.get("finished"), bool):
        raise WorkerProtocolError("worker progress field 'finished' must be boolean")
    _validate_metrics(record.get("metrics"))


def validate_message(record: Mapping[str, Any]) -> WorkerMessage:
    """Validate one complete worker record and return a typed mapping.

    Validation is intentionally strict for worker output.  Ordinary process
    diagnostics are not protocol records and are handled by the controller
    separately.
    """

    if not isinstance(record, Mapping):
        raise WorkerProtocolError("worker protocol record must be an object")
    if record.get("protocol") != PROTOCOL_VERSION:
        raise WorkerProtocolError("Unsupported UI protocol record")
    message_type = record.get("type")
    if message_type not in OUTPUT_MESSAGE_TYPES:
        raise WorkerProtocolError("worker protocol record has an unsupported message type")
    _require_text(record, "worker_run_id", limit=MAX_WORKER_RUN_ID_LENGTH)
    _require_int(record, "sequence", minimum=1, maximum=MAX_SEQUENCE)

    if message_type == "started":
        _require_text(record, "request_id", limit=MAX_WORKER_RUN_ID_LENGTH, required=False)
        _require_text(record, "root", limit=MAX_TEXT_FIELD_LENGTH * 2)
        _require_text(record, "state_directory", limit=MAX_TEXT_FIELD_LENGTH * 2)
        if not isinstance(record.get("apply"), bool):
            raise WorkerProtocolError("worker started field 'apply' must be boolean")
        _require_text(record, "route")
        _require_text(record, "profile", limit=32, required=False)
        _require_int(record, "max_items", minimum=1, maximum=100_000, required=False)
        _require_number(
            record,
            "deadline_seconds",
            minimum=0.001,
            maximum=172_800.0,
            required=False,
        )
    elif message_type == "progress":
        _validate_progress_fields(record)
    elif message_type == "heartbeat":
        _require_int(record, "elapsed_seconds", maximum=172_800)
        active = record.get("active")
        if not isinstance(active, list) or len(active) > MAX_HEARTBEAT_ITEMS:
            raise WorkerProtocolError("worker heartbeat active list exceeds its bound")
        for item in active:
            if not isinstance(item, Mapping):
                raise WorkerProtocolError("worker heartbeat active item must be an object")
            _validate_progress_fields(item)
    elif message_type == "cancel_acknowledged":
        pass
    elif message_type == "completed":
        _require_int(record, "run_id", maximum=2**63 - 1)
        _require_int(record, "files_checked", maximum=2**63 - 1)
        _require_int(record, "action_errors", maximum=2**63 - 1)
        route_errors = record.get("route_errors")
        if not isinstance(route_errors, dict) or len(route_errors) > 64:
            raise WorkerProtocolError("worker completed route_errors must be a bounded object")
        for name, count in route_errors.items():
            if not isinstance(name, str) or len(name) > 64:
                raise WorkerProtocolError("worker completed route names must be bounded strings")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise WorkerProtocolError("worker completed route error counts must be integers")
        if not isinstance(record.get("organization_errors"), bool):
            raise WorkerProtocolError("worker completed organization_errors must be boolean")
        _require_int(record, "issues", maximum=2**63 - 1)
        _require_text(record, "completion_status", limit=64)
        _require_int(record, "exit_code", maximum=255)
    elif message_type == "cancelled":
        _require_text(record, "detail", limit=MAX_TEXT_FIELD_LENGTH)
    elif message_type == "failed":
        _require_text(record, "error_type", limit=256)
        _require_text(record, "detail", limit=MAX_TEXT_FIELD_LENGTH)
        _require_text(record, "stage", limit=256)
        _require_text(
            record,
            "traceback",
            limit=MAX_TRACEBACK_LENGTH,
            required=False,
            allow_line_breaks=True,
        )
        _require_int(record, "exit_code", maximum=255, required=False)

    return cast(WorkerMessage, dict(record))


# endregion [01]


# region [02] Encoding and stateless decoding


def encode_message(message_type: str, **payload: Any) -> bytes:
    """Encode one compact UTF-8 record with a validated protocol envelope."""

    if message_type == "command":
        if set(payload) != {"command"} or payload.get("command") != "cancel":
            raise ValueError("Unsupported worker command")
        record: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "type": "command",
            **payload,
        }
    elif message_type in OUTPUT_MESSAGE_TYPES:
        if "protocol" in payload or "type" in payload:
            raise ValueError("protocol and type are owned by the encoder")
        # Direct callers such as unit tests can still build one record without
        # a worker session.  Real worker output always replaces these defaults
        # with its UUID and monotonic sequence in worker._emit.
        record = {
            "protocol": PROTOCOL_VERSION,
            "type": message_type,
            "worker_run_id": "standalone",
            "sequence": 1,
            **payload,
        }
        validate_message(record)
    else:
        raise ValueError(f"Unsupported worker message type: {message_type}")
    encoded = (
        MESSAGE_PREFIX + json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("UI protocol message exceeds the bounded record size")
    return encoded


def decode_message(line: bytes | str) -> dict[str, Any] | None:
    """Decode one protocol record; return ``None`` for ordinary process text."""

    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    text = text.rstrip("\r\n")
    if not text.startswith(MESSAGE_PREFIX):
        return None
    payload = text[len(MESSAGE_PREFIX) :]
    if len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise WorkerProtocolError("UI protocol message exceeds the bounded record size")
    try:
        record = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("UI protocol record is not valid JSON") from exc
    if not isinstance(record, dict):
        raise WorkerProtocolError("Unsupported UI protocol record")
    if record.get("type") == "command":
        if record.get("protocol") != PROTOCOL_VERSION or record.get("command") != "cancel":
            raise WorkerProtocolError("Unsupported worker command")
        return record
    return dict(validate_message(record))


def progress_payload(event: ProgressEvent) -> dict[str, Any]:
    """Convert the backend-neutral event schema without presentation leakage."""

    return {
        "operation": event.operation,
        "phase": event.phase,
        "description": event.description,
        "completed": event.completed,
        "total": event.total,
        "unit": event.unit,
        "finished": event.finished,
        "metrics": {metric.name: metric.value for metric in event.metrics},
    }


def command_record(command: str) -> bytes:
    if command != "cancel":
        raise ValueError(f"Unsupported worker command: {command}")
    return encode_message("command", command=command)


# endregion [02]


# region [03] Stateful ordering and lifecycle validation


class WorkerMessageValidator:
    """Enforce one worker run's sequence, identity, and terminal lifecycle."""

    def __init__(self) -> None:
        self.worker_run_id: str | None = None
        self.last_sequence = 0
        self.started = False
        self.terminal = False

    def reset(self) -> None:
        self.worker_run_id = None
        self.last_sequence = 0
        self.started = False
        self.terminal = False

    def accept(self, record: Mapping[str, Any]) -> WorkerMessage:
        validated = validate_message(record)
        message_type = validated["type"]
        worker_run_id = validated["worker_run_id"]
        sequence = validated["sequence"]
        if self.worker_run_id is None:
            self.worker_run_id = worker_run_id
        elif worker_run_id != self.worker_run_id:
            raise WorkerProtocolError("worker protocol run identity changed mid-execution")
        expected = self.last_sequence + 1
        if sequence != expected:
            raise WorkerProtocolError(
                f"worker protocol sequence expected {expected}, received {sequence}"
            )
        if not self.started and message_type != "started":
            raise WorkerProtocolError("worker protocol must start with a started record")
        if self.started and message_type == "started":
            raise WorkerProtocolError("worker protocol contains duplicate started records")
        if self.terminal:
            raise WorkerProtocolError("worker protocol emitted records after its terminal record")
        self.last_sequence = sequence
        if message_type == "started":
            self.started = True
        if message_type in TERMINAL_MESSAGE_TYPES:
            self.terminal = True
        return validated

    def synthetic_failure(self, detail: str, *, stage: str = "transport") -> WorkerMessage:
        """Build a bounded UI failure record after transport validation fails."""

        worker_run_id = self.worker_run_id or "invalid"
        return cast(
            WorkerMessage,
            {
                "protocol": PROTOCOL_VERSION,
                "type": "failed",
                "worker_run_id": worker_run_id,
                "sequence": min(self.last_sequence + 1, MAX_SEQUENCE),
                "error_type": "WorkerProtocolError",
                "detail": sanitize_text(detail),
                "stage": sanitize_text(stage, limit=256),
                "exit_code": 1,
            },
        )


def is_terminal_message(message_type: object) -> bool:
    """Return whether a type is one of the required worker terminal records."""

    return isinstance(message_type, str) and message_type in TERMINAL_MESSAGE_TYPES


# endregion [03]
