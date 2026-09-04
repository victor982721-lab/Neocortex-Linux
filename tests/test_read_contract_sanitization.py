"""Bounded traversal regressions for untrusted read payloads."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

from neocortex.api.read_contract import (
    MAX_SANITIZED_PAYLOAD_DEPTH,
    MAX_SANITIZED_PAYLOAD_NODES,
    sanitize_untrusted_payload,
)


_OMISSION = "[contenido omitido por límite]"
_TRUNCATION_KEY = "__neocortex_sanitization_truncated__"


class _CountingList(list[int]):
    def __init__(self, size: int) -> None:
        super().__init__(range(size))
        self.iterated = 0

    def __iter__(self) -> Iterator[int]:
        for item in super().__iter__():
            self.iterated += 1
            yield item


class _CountingMapping(Mapping[str, int]):
    def __init__(self, size: int, *, reserve_marker_key: bool = False) -> None:
        self.size = size
        self.reserve_marker_key = reserve_marker_key
        self.iterated = 0

    def __len__(self) -> int:
        return self.size

    def __iter__(self) -> Iterator[str]:
        for index in range(self.size):
            self.iterated += 1
            if index == 0 and self.reserve_marker_key:
                yield _TRUNCATION_KEY
            else:
                yield f"item-{index}"

    def __getitem__(self, key: str) -> int:
        if key == _TRUNCATION_KEY and self.reserve_marker_key:
            return -1
        return int(key.removeprefix("item-"))


def test_large_list_stops_iteration_at_node_budget_and_marks_truncation() -> None:
    source = _CountingList(MAX_SANITIZED_PAYLOAD_NODES + 5_000)

    sanitized = sanitize_untrusted_payload(source)

    assert isinstance(sanitized, list)
    assert source.iterated == MAX_SANITIZED_PAYLOAD_NODES - 1
    assert len(sanitized) == MAX_SANITIZED_PAYLOAD_NODES
    assert sanitized[-1] == _OMISSION
    json.dumps(sanitized, allow_nan=False)


def test_large_mapping_stops_and_cannot_hide_system_truncation_marker() -> None:
    source = _CountingMapping(
        MAX_SANITIZED_PAYLOAD_NODES + 5_000,
        reserve_marker_key=True,
    )

    sanitized = sanitize_untrusted_payload(source)

    assert isinstance(sanitized, dict)
    assert source.iterated == MAX_SANITIZED_PAYLOAD_NODES - 1
    assert len(sanitized) == MAX_SANITIZED_PAYLOAD_NODES
    assert sanitized[_TRUNCATION_KEY] == -1
    assert sanitized[f"{_TRUNCATION_KEY}#2"] == _OMISSION
    json.dumps(sanitized, allow_nan=False)


def test_nested_mapping_and_list_stop_together_with_deterministic_signals() -> None:
    nested = _CountingList(MAX_SANITIZED_PAYLOAD_NODES + 5_000)
    source = {"nested": nested, "must-not-be-read": "untrusted"}

    first = sanitize_untrusted_payload(source)
    second = sanitize_untrusted_payload(
        {
            "nested": list(range(MAX_SANITIZED_PAYLOAD_NODES + 5_000)),
            "must-not-be-read": "untrusted",
        }
    )

    assert first == second
    assert isinstance(first, dict)
    assert nested.iterated == MAX_SANITIZED_PAYLOAD_NODES - 2
    assert first[_TRUNCATION_KEY] == _OMISSION
    nested_result = first["nested"]
    assert isinstance(nested_result, list)
    assert nested_result[-1] == _OMISSION


def test_deep_mapping_list_payload_has_stable_depth_omission() -> None:
    source: object = "leaf"
    for index in range(MAX_SANITIZED_PAYLOAD_DEPTH + 2):
        source = {f"level-{index}": [source]}

    first = sanitize_untrusted_payload(source)
    second = sanitize_untrusted_payload(source)

    assert first == second
    assert _OMISSION in json.dumps(first, ensure_ascii=False)
