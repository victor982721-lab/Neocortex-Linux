"""Canonical identities for coordinator-owned I/O budgets.

Filesystem device numbers arrive from different routes in two historical
spellings: a decimal ``st_dev`` (for example ``"2049"``) and the media
routes' ``dev:<hex>`` form (``"dev:801"``).  They refer to the same kernel
device, so keeping the strings as-is silently creates two independent budget
keys.  This module is deliberately small and side-effect free; admission is
the single place that applies it to every producer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def canonical_io_device(value: Any | None) -> str | None:
    """Return one stable key for an observed I/O device identity.

    Integer device numbers and decimal strings are interpreted as Linux
    ``st_dev`` values.  The explicit ``dev:`` spelling is hexadecimal for
    compatibility with the media routes.  Other non-empty labels are kept as
    labels rather than being guessed to be device numbers; this preserves
    callers that intentionally use names such as ``"archive"`` or ``"8:0"``.
    ``None`` and the historical empty value retain the default-device
    semantics.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass, but it is never a meaningful st_dev.  Keep
        # it as an opaque label instead of turning True into ``dev:1``.
        return str(value)
    if isinstance(value, int):
        if value < 0:
            raise ValueError("I/O device numbers cannot be negative")
        return f"dev:{value:x}"

    raw = str(value).strip()
    if not raw:
        return None
    if raw.lower().startswith("dev:"):
        token = raw[4:].strip()
        if token.lower().startswith("0x"):
            token = token[2:]
        if token and all(character in "0123456789abcdefABCDEF" for character in token):
            return f"dev:{int(token, 16):x}"
        # ``dev:`` has historically also been accepted as an opaque label;
        # do not reinterpret malformed labels as another device.
        return raw
    if raw.isdecimal():
        return f"dev:{int(raw, 10):x}"
    return raw


def io_device_key(value: Any | None) -> str:
    """Return the coordinator map key, including its explicit default."""

    return canonical_io_device(value) or "default"


def normalize_io_device_slots(
    configured: Mapping[Any, int] | None,
) -> dict[str, int] | None:
    """Normalize per-device limits and reject conflicting aliases.

    A configuration containing both ``"2049": 1`` and ``"dev:801": 2`` is
    ambiguous after canonicalization.  Rejecting it is safer than silently
    selecting one limit; equal duplicate values collapse to one key.
    """

    if configured is None:
        return None
    normalized: dict[str, int] = {}
    for raw_key, value in configured.items():
        key = io_device_key(raw_key)
        previous = normalized.get(key)
        if previous is not None and previous != value:
            raise ValueError(
                "I/O device limits contain conflicting aliases for "
                f"{key!r}: {previous!r} and {value!r}"
            )
        normalized[key] = value
    return normalized

