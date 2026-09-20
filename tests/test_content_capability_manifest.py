"""Contracts for the canonical content producer-to-consumer manifest."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from neocortex.platform.content_capability_manifest import (
    CONTENT_CAPABILITIES,
    CONTENT_CAPABILITY_MANIFEST_SCHEMA,
    CapabilityDependency,
    ContentCapability,
    content_capabilities_for_mime,
    content_capability_by_id,
    content_capability_for_source,
    content_capability_manifest_fingerprint,
    content_capability_manifest_json,
    content_capability_manifest_payload,
)


def test_manifest_covers_every_content_route_and_is_joined() -> None:
    assert tuple(item.capability_id for item in CONTENT_CAPABILITIES) == (
        "audio",
        "docx",
        "image",
        "office",
        "pdf",
        "text",
        "video",
    )
    assert len({item.state_owner_id for item in CONTENT_CAPABILITIES}) == len(CONTENT_CAPABILITIES)
    for capability in CONTENT_CAPABILITIES:
        assert capability.route_name == capability.capability_id
        assert capability.semantic_source_kinds
        assert set(capability.catalog_source_kinds).issubset(capability.semantic_source_kinds)
        assert capability.locators
        assert {"cli", "human", "mcp"}.issubset(capability.public_surfaces)
        assert "ui" not in capability.public_surfaces
    video = content_capability_by_id("video")
    assert video.route_dependencies == (
        CapabilityDependency(
            "audio",
            required=False,
            reason="reuse complete transcript when a video has an audio stream",
        ),
    )


def test_manifest_resolves_mime_and_source_kinds_without_importing_routes() -> None:
    assert content_capability_for_source("video").capability_id == "video"
    assert content_capability_for_source("image_ocr").capability_id == "image"
    assert content_capabilities_for_mime("video/mp4")[0].capability_id == "video"
    assert content_capabilities_for_mime("image/png")[0].capability_id == "image"
    assert content_capabilities_for_mime("application/pdf")[0].capability_id == "pdf"
    assert content_capabilities_for_mime("application/octet-stream") == ()
    with pytest.raises(ValueError, match="unknown content source"):
        content_capability_for_source("missing")
    with pytest.raises(ValueError, match="exact lowercase MIME"):
        content_capabilities_for_mime("VIDEO/MP4")


def test_manifest_json_and_fingerprint_are_stable() -> None:
    payload = content_capability_manifest_payload()
    assert payload["schema"] == CONTENT_CAPABILITY_MANIFEST_SCHEMA
    assert json.loads(content_capability_manifest_json()) == payload
    first = content_capability_manifest_fingerprint()
    assert first == content_capability_manifest_fingerprint()
    assert first.startswith("content-capability-manifest-v1:sha256:")
    assert len(first.rsplit(":", 1)[-1]) == 64


def test_manifest_contracts_are_frozen_and_fail_closed() -> None:
    capability = content_capability_by_id("video")
    with pytest.raises(FrozenInstanceError):
        capability.route_name = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="cannot depend on itself"):
        replace(
            capability,
            route_dependencies=(CapabilityDependency("video"),),
        )
    with pytest.raises(ValueError, match="Semantic source kinds"):
        ContentCapability(
            capability_id="bad",
            route_name="bad",
            input_source="route_candidates",
            mime_types=(),
            state_owner_id="bad",
            state_database="bad.sqlite3",
            state_schema_version=1,
            catalog_source_kinds=("catalog-only",),
            fts_tables=(),
            semantic_source_kinds=("other",),
            semantic_channel="text",
            locators=("character",),
        )
