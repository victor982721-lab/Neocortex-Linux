"""Canonical construction preserves exact processing-signature bytes."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from neocortex.foundation.hash_compat import sha256
from neocortex.foundation.processing_provenance import build_processing_provenance


def test_nested_provenance_retains_canonical_manifest_and_signature() -> None:
    configuration: dict[str, Any] = {"path": Path("/fixture/á"), "options": {"z": (1, False, None), "a": 2.5}}
    components: tuple[dict[str, Any], ...] = (
        {"version": "2", "name": "zeta", "artifacts": [{"path": Path("/fixture/model"), "bytes": 42}]},
        {"version": "1", "name": "álpha", "nested": {"z": {"b": 2, "a": 1}, "a": True}},
    )
    expected_json = json.dumps(
        {
            "schema": "neocortex.processing-provenance/v1",
            "pipeline": "test-route",
            "algorithm_version": "algorithm-v1",
            "configuration": {"path": "/fixture/á", "options": {"a": 2.5, "z": [1, False, None]}},
            "components": [
                {"name": "zeta", "version": "2", "artifacts": [{"path": "/fixture/model", "bytes": 42}]},
                {"name": "álpha", "version": "1", "nested": {"a": True, "z": {"a": 1, "b": 2}}},
            ],
        },
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    expected_signature = "psig-v1|test-route|contract-v1|" + sha256.sha256_128(expected_json.encode()).hexdigest()
    for ordered in (components, tuple(reversed(components))):
        result = build_processing_provenance(
            "test-route", "algorithm-v1", configuration, iter(ordered), compatibility_tag="contract-v1",
        )
        assert result.manifest_json == expected_json
        assert result.signature == expected_signature
    assert isinstance(configuration["path"], Path)
    assert isinstance(components[0]["artifacts"][0]["path"], Path)


@pytest.mark.parametrize("shared_sort_key", (False, True))
def test_component_name_subclasses_preserve_canonical_string_order(shared_sort_key: bool) -> None:
    class ComponentName(str):
        def __lt__(self, other: str) -> bool:
            return str.__gt__(self, other)

        def __str__(self) -> str:
            return "shared" if shared_sort_key else super().__str__()

    expected = build_processing_provenance(
        "test-route", "algorithm-v1", {},
        ({"name": "alpha"}, {"name": "zeta"}), compatibility_tag="contract-v1",
    )
    for names in (("alpha", "zeta"), ("zeta", "alpha")):
        result = build_processing_provenance(
            "test-route", "algorithm-v1", {},
            ({"name": ComponentName(name)} for name in names), compatibility_tag="contract-v1",
        )
        assert result.manifest_json == expected.manifest_json
        assert result.signature == expected.signature


@pytest.mark.parametrize(
    "components",
    (
        ({"name": "same"}, {"name": "same", "version": "other"}),
        ({"name": "engine", "nested": {"value": float("nan")}},),
        ({"name": "engine", "nested": {"value": float("inf")}},),
        ({"name": "engine", "nested": {1: "invalid key"}},),
        ({"name": "engine", "nested": {"value": object()}},),
    ),
)
def test_invalid_nested_components_cannot_produce_a_signature(components: Iterable[Mapping[str, Any]]) -> None:
    with pytest.raises((TypeError, ValueError)):
        build_processing_provenance(
            "test-route", "algorithm-v1", {}, components, compatibility_tag="contract-v1",
        )
