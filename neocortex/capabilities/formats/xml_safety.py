"""Fail-closed XML parsing primitives for untrusted document content."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from typing import Literal, Protocol


_UNSAFE_DECLARATION_MARKERS = (b"<!DOCTYPE", b"<!ENTITY", b"<!NOTATION")
_XML_MARKER_TAIL_BYTES = max(len(marker) for marker in _UNSAFE_DECLARATION_MARKERS) - 1


class UnsafeXmlDeclarationError(ET.ParseError):
    """An XML DTD/entity declaration was rejected before parser expansion."""


XmlEvent = Literal["start", "end", "start-ns", "end-ns"]


class _ReadableXmlSource(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


def _reject_unsafe_declarations(payload: bytes) -> None:
    folded = payload.upper()
    if any(marker in folded for marker in _UNSAFE_DECLARATION_MARKERS):
        raise UnsafeXmlDeclarationError(
            "XML DTD, entity, and notation declarations are not accepted"
        )


class _GuardedXmlStream:
    """Scan each input block before passing it to ElementTree."""

    def __init__(self, source: _ReadableXmlSource):
        self._source = source
        self._tail = b""

    def read(self, size: int = -1) -> bytes:
        payload = self._source.read(size)
        if not payload:
            return payload
        window = self._tail + payload
        _reject_unsafe_declarations(window)
        self._tail = window[-_XML_MARKER_TAIL_BYTES:]
        return payload

    def close(self) -> None:
        self._source.close()


def safe_xml_fromstring(payload: bytes | str) -> ET.Element:
    """Parse XML after rejecting declarations that enable entity expansion."""

    encoded = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    _reject_unsafe_declarations(encoded)
    return ET.fromstring(payload)


def safe_xml_iterparse(
    source: _ReadableXmlSource,
    *,
    events: Iterable[XmlEvent],
) -> Iterator[tuple[str, ET.Element]]:
    """Stream XML through the same fail-closed declaration guard."""

    return ET.iterparse(_GuardedXmlStream(source), events=tuple(events))


__all__ = (
    "UnsafeXmlDeclarationError",
    "safe_xml_fromstring",
    "safe_xml_iterparse",
)
