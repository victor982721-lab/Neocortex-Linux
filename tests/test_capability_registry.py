"""Canonical capability registry contracts."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from neocortex.platform.capability_registry import (
    CAPABILITY_REGISTRY,
    CAPABILITY_REGISTRY_SCHEMA,
    CapabilityLogicalOwnerBinding,
    CapabilityModuleBinding,
    CapabilityRegistry,
    CapabilityRouteContract,
    CapabilitySpec,
    CapabilityStateContract,
    PythonSymbolRef,
    canonical_target_architecture_families,
    capability_canonical_logical_owner_bindings,
    capability_logical_owner_bindings,
    capability_registry_canonical_json,
    capability_registry_fingerprint,
    capability_registry_payload,
    parse_capability_registry_payload,
    resolve_canonical_capabilities,
)

_FORMATS_ROOT = "neocortex.capabilities.formats"
_EXPECTED_IDS = ("archive", "audio", "docx", "image", "office", "video")


def _ids(values: tuple[CapabilitySpec, ...]) -> tuple[str, ...]:
    return tuple(item.capability_id for item in values)


def test_registry_is_canonical_and_has_no_compatibility_fields() -> None:
    assert CAPABILITY_REGISTRY.schema == CAPABILITY_REGISTRY_SCHEMA
    assert _ids(CAPABILITY_REGISTRY.capabilities) == _EXPECTED_IDS
    assert len(CAPABILITY_REGISTRY.capabilities) == len(_EXPECTED_IDS)
    for capability in CAPABILITY_REGISTRY.capabilities:
        assert capability.architecture_family_id == _FORMATS_ROOT
        assert not hasattr(capability, "compatibility_family_id")
        assert capability.canonical_module_tree.startswith(_FORMATS_ROOT + ".")
        for binding in capability.modules:
            assert not hasattr(binding, "legacy_module_id")
            assert binding.canonical_module_id.startswith(capability.canonical_module_tree + ".")

    payload = capability_registry_payload()
    serialized = capability_registry_canonical_json()
    assert "legacy_module_id" not in serialized
    assert "compatibility_family_id" not in serialized
    assert "_04_Nucleo_Operativo" not in serialized
    assert parse_capability_registry_payload(json.loads(serialized)) == CAPABILITY_REGISTRY
    assert payload == json.loads(serialized)


def test_registry_resolves_only_canonical_module_trees() -> None:
    canonical = f"{_FORMATS_ROOT}.archive.route"
    nested = f"{_FORMATS_ROOT}.archive.future.reader"
    assert _ids(resolve_canonical_capabilities(canonical)) == ("archive",)
    assert _ids(resolve_canonical_capabilities(nested)) == ("archive",)
    assert canonical_target_architecture_families(canonical) == (_FORMATS_ROOT,)
    assert resolve_canonical_capabilities("neocortex.archive_route") == ()
    with pytest.raises(ValueError, match="only canonical"):
        CAPABILITY_REGISTRY.target_families(canonical, resolver_mode="source")  # type: ignore[arg-type]


def test_logical_owner_projection_contains_only_canonical_tree_bindings() -> None:
    bindings = capability_canonical_logical_owner_bindings()
    assert tuple(item.owner_id for item in bindings) == _EXPECTED_IDS
    assert tuple(item.selector_id for item in bindings) == tuple(
        f"{item}-core-modules" for item in _EXPECTED_IDS
    )
    assert all(item.match_kind == "module_tree" for item in bindings)
    assert capability_logical_owner_bindings() == bindings
    assert all(item.value.startswith(_FORMATS_ROOT + ".") for item in bindings)


def test_routes_states_and_test_roots_are_complete() -> None:
    repository = Path(__file__).resolve().parents[1]
    for capability in CAPABILITY_REGISTRY.capabilities:
        assert capability.route is not None
        assert capability.state is not None
        assert capability.route.route_class.module_id.startswith(capability.canonical_module_tree)
        assert capability.state.state_module_id.startswith(capability.canonical_module_tree)
        assert capability.state.schema_module_id.startswith(capability.canonical_module_tree)
        for relative in capability.test_roots:
            path = repository / relative
            assert path.is_file() and not path.is_symlink(), relative
            assert "namespace_migration" not in relative
            assert "format_module_move_compatibility" not in relative


def test_contract_types_are_frozen_and_fail_closed() -> None:
    archive = CAPABILITY_REGISTRY.by_id("archive")
    assert all(
        getattr(item, "__slots__", None)
        for item in (
            PythonSymbolRef,
            CapabilityModuleBinding,
            CapabilityRouteContract,
            CapabilityStateContract,
            CapabilityLogicalOwnerBinding,
            CapabilitySpec,
            CapabilityRegistry,
        )
    )
    with pytest.raises(FrozenInstanceError):
        archive.capability_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="inside their module tree"):
        replace(
            archive,
            modules=(
                replace(archive.modules[0], canonical_module_id="other.module"),
                *archive.modules[1:],
            ),
        )
    with pytest.raises(ValueError, match="unknown module role"):
        archive.module("missing")
    with pytest.raises(ValueError, match="unknown capability"):
        CAPABILITY_REGISTRY.by_id("missing")
    with pytest.raises(ValueError, match="unknown capability route"):
        CAPABILITY_REGISTRY.by_route("missing")
    with pytest.raises(ValueError, match="schema is invalid"):
        CapabilityRegistry("future", (archive,))  # type: ignore[arg-type]


def test_payload_and_fingerprint_are_stable() -> None:
    first = capability_registry_fingerprint()
    second = capability_registry_fingerprint()
    assert first == second
    assert first.startswith("capability-registry-v1:sha256:")
    assert len(first.removeprefix("capability-registry-v1:sha256:")) == 64
    payload = capability_registry_payload()
    with pytest.raises(ValueError, match="fields are invalid"):
        parse_capability_registry_payload({**payload, "unexpected": True})
