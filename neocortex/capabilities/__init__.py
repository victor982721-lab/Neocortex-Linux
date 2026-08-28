"""Capability implementations grouped by product responsibility.

The runtime probe contract used to live in a flat ``capabilities.py`` module.
It now resides in :mod:`neocortex.capabilities.runtime`, while this package
keeps the historical public imports and hosts format-specific implementations.
"""

from __future__ import annotations

from . import runtime as _runtime
from .runtime import (
    CAPABILITY_MANIFESTS,
    CAPABILITY_SPECS,
    ROUTE_CAPABILITY_NAMES,
    RUNTIME_CAPABILITY_PROBE_POLICY,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    TEXT_BUILTIN_IMPLEMENTATION_ID,
    TEXT_EXTRACT_CAPABILITY_ID,
    TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID,
    TEXT_RAW_INPUT_SCHEMA,
    TEXT_REPRESENTATION_OUTPUT_SCHEMA,
    CapabilityAvailability,
    CapabilityBinaryIdentity,
    CapabilityBroker,
    CapabilityLanguageMode,
    CapabilityLifecycle,
    CapabilityManifest,
    CapabilityMimeBinaryAlternatives,
    CapabilityPolicy,
    CapabilityPrivacy,
    CapabilityQualityMetric,
    CapabilityQualityRequirement,
    CapabilityRequest,
    CapabilitySelection,
    CapabilityState,
    RequirementKind,
    RuntimeCapabilitySpec,
    RuntimeCapabilityStatus,
    RuntimeComponentStatus,
    RuntimeRequirement,
    build_runtime_capability_broker,
    inspect_capability_implementation_availability,
    inspect_runtime_capabilities,
    inspect_runtime_capability,
)

# Preserve the narrow monkeypatch seams used by existing callers while the
# implementation lives in the responsibility module.
os = _runtime.os
hashlib = _runtime.hashlib
_binary_identity = _runtime._binary_identity

__all__ = [
    "CAPABILITY_MANIFESTS",
    "CAPABILITY_SPECS",
    "ROUTE_CAPABILITY_NAMES",
    "RUNTIME_CAPABILITY_PROBE_POLICY",
    "RUNTIME_CAPABILITY_SCHEMA_VERSION",
    "TEXT_BUILTIN_IMPLEMENTATION_ID",
    "TEXT_EXTRACT_CAPABILITY_ID",
    "TEXT_LEGACY_OFFICE_IMPLEMENTATION_ID",
    "TEXT_RAW_INPUT_SCHEMA",
    "TEXT_REPRESENTATION_OUTPUT_SCHEMA",
    "CapabilityAvailability",
    "CapabilityBinaryIdentity",
    "CapabilityBroker",
    "CapabilityLanguageMode",
    "CapabilityLifecycle",
    "CapabilityManifest",
    "CapabilityMimeBinaryAlternatives",
    "CapabilityPolicy",
    "CapabilityPrivacy",
    "CapabilityQualityMetric",
    "CapabilityQualityRequirement",
    "CapabilityRequest",
    "CapabilitySelection",
    "CapabilityState",
    "RequirementKind",
    "RuntimeCapabilitySpec",
    "RuntimeCapabilityStatus",
    "RuntimeComponentStatus",
    "RuntimeRequirement",
    "build_runtime_capability_broker",
    "formats",
    "inspect_capability_implementation_availability",
    "inspect_runtime_capabilities",
    "inspect_runtime_capability",
]
