"""Shared bounded XML and ZIP primitives for Office extraction."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Mapping, Protocol

from .models import MAX_MEMBER_BYTES, OfficeExtractionError


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
    ):
        self._source = source
        self._remaining = member_limit
        self._budget = budget

    def read(self, size: int = -1) -> bytes:
        request = self._remaining + 1 if size < 0 else min(size, self._remaining + 1)
        payload = self._source.read(request)
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
) -> _BoundedZipMember:
    return _BoundedZipMember(
        archive.open(info),
        member_limit=min(MAX_MEMBER_BYTES, int(info.file_size) + 1),
        budget=budget,
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
) -> None:
    source = archive.open(info)
    bounded = _BoundedZipMember(
        source,
        member_limit=min(MAX_MEMBER_BYTES, int(info.file_size) + 1),
        budget=budget,
    )
    try:
        for _event, element in ET.iterparse(bounded, events=("end",)):
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
