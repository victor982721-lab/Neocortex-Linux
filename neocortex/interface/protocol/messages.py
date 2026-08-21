"""Bounded line protocol shared by the Qt controller and worker process."""

from __future__ import annotations

import json
from typing import Any

from neocortex.progress import ProgressEvent


# region [01] Wire format

PROTOCOL_VERSION = 1
MESSAGE_PREFIX = "@neocortex-ui/v1 "
MAX_MESSAGE_BYTES = 1_000_000


def encode_message(message_type: str, **payload: Any) -> bytes:
    """Encode one compact UTF-8 record with an unambiguous protocol prefix."""

    record = {
        "protocol": PROTOCOL_VERSION,
        "type": message_type,
        **payload,
    }
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
        raise ValueError("UI protocol message exceeds the bounded record size")
    record = json.loads(payload)
    if not isinstance(record, dict) or record.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("Unsupported UI protocol record")
    if not isinstance(record.get("type"), str):
        raise ValueError("UI protocol record has no message type")
    return record


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
    if command not in {"cancel"}:
        raise ValueError(f"Unsupported worker command: {command}")
    return encode_message("command", command=command)


# endregion [01]
