"""Negative contracts for the retired virtual Archive product."""

from __future__ import annotations

import importlib.util
import json

import pytest

from neocortex.api.cli.cli_parser import build_parser
from neocortex.documents.document_resource_binding import ResourceBindingError, parse_resource_binding
from neocortex.platform.capability_registry import CAPABILITY_REGISTRY
from neocortex.platform.content_capability_manifest import CONTENT_CAPABILITIES
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.runtime.orchestration.route_selection import normalize_route_selection
from neocortex.safety.state_topology_contracts import STATE_STORE_REGISTRY


def test_virtual_archive_modules_and_capability_are_absent() -> None:
    for module in (
        "neocortex.capabilities.formats.archive.route",
        "neocortex.capabilities.formats.archive.state",
        "neocortex.capabilities.formats.archive.materialization",
        "neocortex.capabilities.formats.archive.rebuild",
        "neocortex.api.cli.cli_archive",
        "neocortex.api.cli.cli_archive_surface",
    ):
        assert importlib.util.find_spec(module) is None, module
    assert all(item.capability_id != "archive" for item in CONTENT_CAPABILITIES)
    assert all(item.capability_id != "archive" for item in CAPABILITY_REGISTRY.capabilities)
    assert "archive" not in builtin_route_registry()
    assert all(store.state_owner_id != "archive" for store in STATE_STORE_REGISTRY.stores)


def test_archive_virtual_cli_and_route_selection_are_rejected() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(("--archive-status",))
    with pytest.raises(ValueError, match="unknown routes: archive"):
        normalize_route_selection("archive", tuple(builtin_route_registry()))
    assert "--archive-status" not in parser.format_help()
    assert "--archive-search" not in parser.format_help()
    assert "--archive-list" not in parser.format_help()


def test_resource_binding_cannot_publish_virtual_archive_member() -> None:
    payload = {
        "schema": "neocortex.document-resource-binding/v1",
        "source_kind": "archive",
        "file_key": "archive:member",
        "representation_kind": "archive_member",
        "resource_ref": {
            "resource_id": "resource:archive:archive:member",
            "source_kind": "archive",
            "owner": "archive",
            "physical_identity": None,
            "current_path": "/corpus/a.zip!/member.txt",
            "disposition": None,
            "canonical_resource_id": None,
            "kind": "resource_ref",
            "schema_version": 1,
        },
        "physical_identity": None,
        "physical_anchor_path": None,
        "physical_anchor_revision": None,
        "archive_member": {
            "container_key": "archive:container",
            "container_path": "/corpus/a.zip",
            "member_chain": "member.txt",
        },
        "representation_metadata": {},
    }
    with pytest.raises(ResourceBindingError):
        parse_resource_binding(json.dumps(payload))
