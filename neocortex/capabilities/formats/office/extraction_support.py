"""Shared bounded XML and ZIP primitives for Office extraction."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Mapping, Protocol

from .models import MAX_MEMBER_BYTES, OfficeExtractionError
from neocortex.capabilities.formats.xml_safety import safe_xml_iterparse


class CancellationCheckpoint(Protocol):
    def checkpoint(self) -> None: ...


class _TextAccumulator:
    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("office text limit must be positive")
        self._limit = limit
        self._buffer = io.StringIO()
        self._chars = 0

    def add(self, value: str | None) -> None:
        if not value:
            return
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized:
            return
        extra = len(normalized) + (1 if self._chars else 0)
        if self._chars + extra > self._limit:
            raise OfficeExtractionError(
                "office_text_limit",
                f"extracted office text exceeds {self._limit} characters",
                recommendation="manual_review",
                retryable=False,
            )
        if self._chars:
            self._buffer.write("\n")
        self._buffer.write(normalized)
        self._chars += extra

    def value(self) -> str:
        return self._buffer.getvalue()


class _ReadBudget:
    def __init__(self, limit: int):
        self.remaining = limit

    def consume(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise OfficeExtractionError(
                "office_uncompressed_limit",
                "selected office XML exceeds the uncompressed byte limit",
                recommendation="manual_review",
                retryable=False,
            )


class _BoundedZipMember:
    def __init__(
        self,
        source,
        *,
        member_limit: int,
        budget: _ReadBudget,
        cancellation: CancellationCheckpoint,
    ):
        self._source = source
        self._remaining = member_limit
        self._budget = budget
        self._cancellation = cancellation

    def read(self, size: int = -1) -> bytes:
        self._cancellation.checkpoint()
        request = self._remaining + 1 if size < 0 else min(size, self._remaining + 1)
        payload = self._source.read(request)
        self._cancellation.checkpoint()
        self._remaining -= len(payload)
        self._budget.consume(len(payload))
        if self._remaining < 0:
            raise OfficeExtractionError(
                "office_member_limit",
                "office XML member exceeds its uncompressed byte limit",
                recommendation="manual_review",
                retryable=False,
            )
        return payload

    def close(self) -> None:
        self._source.close()


def _bounded_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _ReadBudget,
    *,
    cancellation: CancellationCheckpoint,
) -> _BoundedZipMember:
    return _BoundedZipMember(
        archive.open(info),
        member_limit=min(MAX_MEMBER_BYTES, int(info.file_size) + 1),
        budget=budget,
        cancellation=cancellation,
    )


def _attribute_by_local_name(
    attributes: Mapping[str, str],
    local_name: str,
) -> str | None:
    for name, value in attributes.items():
        if _local_name(name) == local_name:
            return value
    return None


def _extract_part_text(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    format_name: str,
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> None:
    if format_name == "odt":
        _extract_odt_part_text(
            archive,
            info,
            accumulator=accumulator,
            budget=budget,
            cancellation=cancellation,
        )
        return
    bounded = _bounded_member(archive, info, budget, cancellation=cancellation)
    try:
        for _event, element in safe_xml_iterparse(bounded, events=("end",)):
            local = _local_name(element.tag)
            if format_name == "xlsx":
                if local in {"t", "f", "definedName"}:
                    accumulator.add(element.text)
                elif local == "sheet":
                    accumulator.add(element.attrib.get("name"))
            elif format_name == "pptx":
                if local == "t":
                    accumulator.add(element.text)
            elif local in {"p", "h", "span", "a"}:
                accumulator.add(element.text)
            element.clear()
    finally:
        bounded.close()


def _odt_element_text(
    element,
    *,
    cancellation: CancellationCheckpoint | None = None,
) -> str:
    """Return one ODF block in document order, including child tails.

    ``ElementTree`` exposes text after a child as ``child.tail``.  Processing
    every node independently (and clearing it immediately) loses that tail and
    publishes nested spans before their containing paragraph.  Assemble one
    paragraph/heading before releasing its subtree instead.  ODF's explicit
    whitespace elements are represented here too; the accumulator applies the
    final bounded whitespace normalization.
    """

    pieces: list[str] = []
    # Traverse iteratively: a hostile but bounded XML member can still contain
    # more nesting than Python's recursion limit.  ODF's repeated-space count
    # is represented by one space because the accumulator intentionally
    # canonicalizes whitespace; this avoids expanding an untrusted attribute.
    stack: list[tuple[ET.Element, tuple[ET.Element, ...], int]] = [
        (element, tuple(element), 0)
    ]
    visited = 0
    while stack:
        node, children, child_index = stack[-1]
        if child_index == 0:
            local = _local_name(node.tag)
            if local == "s":
                pieces.append(" ")
            elif local == "tab":
                pieces.append("\t")
            elif local == "line-break":
                pieces.append("\n")
            elif node.text:
                pieces.append(node.text)
            visited += 1
            if cancellation is not None and visited % 1024 == 0:
                cancellation.checkpoint()
        if child_index < len(children):
            child = children[child_index]
            stack[-1] = (node, children, child_index + 1)
            stack.append((child, tuple(child), 0))
        else:
            stack.pop()
            if node is not element and node.tail:
                pieces.append(node.tail)
    return "".join(pieces)


def _extract_odt_part_text(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    accumulator: _TextAccumulator,
    budget: _ReadBudget,
    cancellation: CancellationCheckpoint,
) -> None:
    """Extract ODF paragraphs without losing nested text or XML tails."""

    bounded = _bounded_member(archive, info, budget, cancellation=cancellation)
    block_stack: list[ET.Element] = []
    ancestors: list[ET.Element] = []
    try:
        for event, element in safe_xml_iterparse(bounded, events=("start", "end")):
            local = _local_name(element.tag)
            if event == "start":
                ancestors.append(element)
                if local in {"p", "h"}:
                    block_stack.append(element)
                continue

            release = not block_stack
            if block_stack:
                if block_stack[-1] is element:
                    block_stack.pop()
                    if not block_stack:
                        cancellation.checkpoint()
                        accumulator.add(
                            _odt_element_text(element, cancellation=cancellation)
                        )
                        # Nested blocks remain attached until the outer block
                        # closes, so their tails/text cannot disappear early.
                        release = True

            if release:
                # clear() alone leaves one empty element per paragraph linked
                # from office:text until EOF. Unlink completed blocks as well,
                # while retaining descendants of an open outer block for tails.
                if len(ancestors) > 1:
                    ancestors[-2].remove(element)
                element.clear()
            ancestors.pop()
    finally:
        bounded.close()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = (
    "CancellationCheckpoint",
    "_ReadBudget",
    "_TextAccumulator",
    "_attribute_by_local_name",
    "_bounded_member",
    "_extract_part_text",
    "_local_name",
)
