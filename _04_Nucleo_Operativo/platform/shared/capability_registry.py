"""Static capability ownership and namespace-migration contracts.

The registry is deliberately data-only.  It records exact source modules,
canonical capability trees, state-owner foreign keys, route subjects, and the
small set of Python FQNs needed to validate a migration.  It never imports a
capability implementation, probes an installed dependency, opens product
state, or falls back from one resolver namespace to the other.

Runtime dependency availability remains owned by :mod:`neocortex.capabilities`;
route execution remains owned by ``route_registry``; physical state topology
remains owned by ``state_topology_contracts``.  Consumers must join those
contracts explicitly instead of treating this registry as runtime dispatch.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .capability_registry_specs import CAPABILITY_SPEC_PAYLOADS
else:
    _specs_path = Path(__file__).with_name("capability_registry_specs.py")
    _specs_digest = hashlib.sha256(_specs_path.read_bytes()).hexdigest()[:16]
    _specs_alias = f"_neocortex_capability_registry_specs_{_specs_digest}"
    _specs_module = sys.modules.get(_specs_alias)
    if _specs_module is None:
        _spec = importlib.util.spec_from_file_location(_specs_alias, _specs_path)
        if _spec is None or _spec.loader is None:
            raise RuntimeError("capability registry specs cannot be loaded")
        _specs_module = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = _specs_module
        _spec.loader.exec_module(_specs_module)
    CAPABILITY_SPEC_PAYLOADS = _specs_module.CAPABILITY_SPEC_PAYLOADS

CAPABILITY_REGISTRY_SCHEMA: Literal["neocortex.capability-registry/v1"] = (
    "neocortex.capability-registry/v1"
)
CAPABILITY_REGISTRY_FINGERPRINT_PREFIX = "capability-registry-v1:sha256:"

ModuleResolutionMode = Literal["source", "canonical"]
RouteInputSource = Literal["route_candidates", "inventory_snapshot"]
RouteSubjectMatchKind = Literal["exact_mime", "mime_prefix"]
KnowledgeCaptureMode = Literal["configured", "if_present"]
WarningPolicy = Literal["silent"]
LogicalOwnerMatchKind = Literal["exact_module", "module_tree"]

_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_ROLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_MODULE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SYMBOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _required_text(label: str, value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _identifier(label: str, value: object, pattern: re.Pattern[str]) -> str:
    selected = _required_text(label, value)
    if pattern.fullmatch(selected) is None:
        raise ValueError(f"{label} is invalid")
    return selected


def _module_id(label: str, value: object) -> str:
    return _identifier(label, value, _MODULE_ID)


def _unique_texts(
    label: str,
    values: tuple[str, ...],
    *,
    ordered: bool = False,
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{label} values must be an immutable tuple")
    for value in values:
        _required_text(label, value)
    if len(set(values)) != len(values):
        raise ValueError(f"{label} values cannot repeat")
    if ordered and values != tuple(sorted(values)):
        raise ValueError(f"{label} values must be canonically ordered")


def _module_in_tree(module_id: str, module_tree: str) -> bool:
    return module_id == module_tree or module_id.startswith(module_tree + ".")


def _validate_test_root(value: str) -> None:
    _required_text("capability test root", value, maximum=1_024)
    if (
        not value.startswith("tests/")
        or not value.endswith(".py")
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("capability test root must be a normalized tests/*.py path")


@dataclass(frozen=True, slots=True)
class PythonSymbolRef:
    """One import-free reference to a Python symbol."""

    module_id: str
    symbol_name: str

    def __post_init__(self) -> None:
        _module_id("Python symbol module id", self.module_id)
        _identifier("Python symbol name", self.symbol_name, _SYMBOL_NAME)

    @property
    def qualified_name(self) -> str:
        return f"{self.module_id}.{self.symbol_name}"

    def as_payload(self) -> dict[str, object]:
        return {"module_id": self.module_id, "symbol_name": self.symbol_name}


@dataclass(frozen=True, slots=True)
class CapabilityModuleBinding:
    """One exact flat source module and its canonical capability module."""

    role: str
    canonical_module_id: str
    legacy_module_id: str | None
    public_symbols: tuple[str, ...] = ()
    warning_policy: WarningPolicy = "silent"

    def __post_init__(self) -> None:
        _identifier("capability module role", self.role, _ROLE_ID)
        _module_id("canonical capability module id", self.canonical_module_id)
        if self.legacy_module_id is not None:
            _module_id("legacy capability module id", self.legacy_module_id)
            if self.legacy_module_id == self.canonical_module_id:
                raise ValueError("legacy and canonical capability modules must differ")
        _unique_texts("capability public symbol", self.public_symbols, ordered=True)
        for symbol in self.public_symbols:
            _identifier("capability public symbol", symbol, _SYMBOL_NAME)
        if self.warning_policy != "silent":
            raise ValueError("capability compatibility warnings must remain silent")

    def as_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "canonical_module_id": self.canonical_module_id,
            "legacy_module_id": self.legacy_module_id,
            "public_symbols": list(self.public_symbols),
            "warning_policy": self.warning_policy,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRouteContract:
    """Static route identity, candidate selector, and public Python FQNs."""

    route_name: str
    input_source: RouteInputSource
    subject_match_kind: RouteSubjectMatchKind
    subject_value: str
    route_class: PythonSymbolRef
    config_class: PythonSymbolRef
    summary_class: PythonSymbolRef
    version_symbol: PythonSymbolRef

    def __post_init__(self) -> None:
        _identifier("capability route name", self.route_name, _CAPABILITY_ID)
        if self.input_source not in {"route_candidates", "inventory_snapshot"}:
            raise ValueError("capability route input source is invalid")
        if self.subject_match_kind not in {"exact_mime", "mime_prefix"}:
            raise ValueError("capability route subject match kind is invalid")
        subject = _required_text("capability route subject", self.subject_value)
        if subject != subject.casefold() or subject.count("/") != 1:
            raise ValueError("capability route subject must be one lowercase MIME expression")
        if self.subject_match_kind == "exact_mime" and subject.endswith("/"):
            raise ValueError("exact MIME route subjects cannot end with a slash")
        if self.subject_match_kind == "mime_prefix" and not subject.endswith("/"):
            raise ValueError("MIME-prefix route subjects must end with a slash")

    def as_payload(self) -> dict[str, object]:
        return {
            "route_name": self.route_name,
            "input_source": self.input_source,
            "subject_match_kind": self.subject_match_kind,
            "subject_value": self.subject_value,
            "route_class": self.route_class.as_payload(),
            "config_class": self.config_class.as_payload(),
            "summary_class": self.summary_class.as_payload(),
            "version_symbol": self.version_symbol.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class CapabilityStateContract:
    """Foreign-key projection into the public physical state-store registry."""

    state_owner_id: str
    state_store_id: str
    database_name: str
    knowledge_path_attribute: str
    expected_schema_version: int
    knowledge_read_kind: str
    knowledge_capture_mode: KnowledgeCaptureMode
    state_module_id: str
    schema_module_id: str
    schema_version_symbol: PythonSymbolRef
    storage_engine: Literal["sqlite"] = "sqlite"

    def __post_init__(self) -> None:
        _identifier("capability state owner id", self.state_owner_id, _CAPABILITY_ID)
        _required_text("capability state store id", self.state_store_id)
        database = _required_text("capability database name", self.database_name)
        _identifier(
            "capability Knowledge path attribute",
            self.knowledge_path_attribute,
            _ROLE_ID,
        )
        _required_text("capability Knowledge read kind", self.knowledge_read_kind)
        if not database.endswith(".sqlite3"):
            raise ValueError("capability database must identify a SQLite file")
        if self.state_store_id != f"sqlite:{database}":
            raise ValueError("capability state store id and database name disagree")
        if (
            isinstance(self.expected_schema_version, bool)
            or not isinstance(self.expected_schema_version, int)
            or self.expected_schema_version < 1
        ):
            raise ValueError("capability schema version must be a positive integer")
        if self.knowledge_capture_mode not in {"configured", "if_present"}:
            raise ValueError("capability Knowledge capture mode is invalid")
        _module_id("capability state module id", self.state_module_id)
        _module_id("capability schema module id", self.schema_module_id)
        if self.schema_version_symbol.module_id != self.schema_module_id:
            raise ValueError("capability schema-version symbol must belong to its schema module")
        if self.storage_engine != "sqlite":
            raise ValueError("capability state storage engine must remain SQLite")

    def as_payload(self) -> dict[str, object]:
        return {
            "state_owner_id": self.state_owner_id,
            "state_store_id": self.state_store_id,
            "database_name": self.database_name,
            "knowledge_path_attribute": self.knowledge_path_attribute,
            "expected_schema_version": self.expected_schema_version,
            "knowledge_read_kind": self.knowledge_read_kind,
            "knowledge_capture_mode": self.knowledge_capture_mode,
            "state_module_id": self.state_module_id,
            "schema_module_id": self.schema_module_id,
            "schema_version_symbol": self.schema_version_symbol.as_payload(),
            "storage_engine": self.storage_engine,
        }


@dataclass(frozen=True, slots=True)
class CapabilityLogicalOwnerBinding:
    """Data-only projection consumed by ``logical_owner_contracts``."""

    owner_id: str
    selector_id: str
    match_kind: LogicalOwnerMatchKind
    value: str
    state_owner_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier("capability logical owner id", self.owner_id, _CAPABILITY_ID)
        _identifier("capability logical owner selector id", self.selector_id, _CAPABILITY_ID)
        if self.match_kind not in {"exact_module", "module_tree"}:
            raise ValueError("capability logical ownership match kind is invalid")
        _module_id("capability logical owner module selector", self.value)
        _unique_texts("capability logical state owner", self.state_owner_ids)
        for owner_id in self.state_owner_ids:
            _identifier("capability logical state owner", owner_id, _CAPABILITY_ID)

    def as_payload(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "selector_id": self.selector_id,
            "match_kind": self.match_kind,
            "value": self.value,
            "state_owner_ids": list(self.state_owner_ids),
        }


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """One capability leaf and its explicit source-to-canonical contract."""

    capability_id: str
    architecture_family_id: str
    compatibility_family_id: str
    logical_owner_id: str
    canonical_module_tree: str
    modules: tuple[CapabilityModuleBinding, ...]
    route: CapabilityRouteContract | None
    state: CapabilityStateContract | None
    test_roots: tuple[str, ...]
    executable_module_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier("capability id", self.capability_id, _CAPABILITY_ID)
        architecture_family_id = _module_id(
            "capability architecture family id", self.architecture_family_id
        )
        compatibility_family_id = _module_id(
            "capability compatibility family id", self.compatibility_family_id
        )
        if architecture_family_id == compatibility_family_id:
            raise ValueError("capability architecture and compatibility families must differ")
        _identifier("capability logical owner id", self.logical_owner_id, _CAPABILITY_ID)
        tree = _module_id("canonical capability module tree", self.canonical_module_tree)
        if not self.modules or any(
            not isinstance(item, CapabilityModuleBinding) for item in self.modules
        ):
            raise ValueError("capability must declare typed module bindings")
        roles = tuple(item.role for item in self.modules)
        _unique_texts("capability module role", roles, ordered=True)
        canonical_modules = tuple(item.canonical_module_id for item in self.modules)
        _unique_texts("canonical capability module", canonical_modules)
        if any(not _module_in_tree(module, tree) for module in canonical_modules):
            raise ValueError("canonical capability modules must remain inside their module tree")
        legacy_modules = tuple(
            item.legacy_module_id for item in self.modules if item.legacy_module_id is not None
        )
        _unique_texts("legacy capability module", legacy_modules)
        if any(_module_in_tree(module, tree) for module in legacy_modules):
            raise ValueError("legacy capability modules cannot live inside the canonical tree")

        known_modules = set(canonical_modules)
        if self.route is not None:
            route_refs = (
                self.route.route_class,
                self.route.config_class,
                self.route.summary_class,
                self.route.version_symbol,
            )
            if any(ref.module_id not in known_modules for ref in route_refs):
                raise ValueError("capability route FQNs must belong to declared canonical modules")
        if self.state is not None:
            if (
                self.state.state_module_id not in known_modules
                or self.state.schema_module_id not in known_modules
            ):
                raise ValueError("capability state modules must be declared canonical modules")

        _unique_texts("capability test root", self.test_roots, ordered=True)
        for test_root in self.test_roots:
            _validate_test_root(test_root)
        _unique_texts(
            "capability executable module",
            self.executable_module_ids,
            ordered=True,
        )
        if any(module not in known_modules for module in self.executable_module_ids):
            raise ValueError("capability executable modules must be declared canonical modules")

    def module(self, role: str) -> CapabilityModuleBinding:
        selected = _identifier("capability module role", role, _ROLE_ID)
        match = next((item for item in self.modules if item.role == selected), None)
        if match is None:
            raise ValueError(f"unknown module role for {self.capability_id}: {selected}")
        return match

    def canonical_logical_owner_binding(self) -> CapabilityLogicalOwnerBinding:
        state_owner_ids = () if self.state is None else (self.state.state_owner_id,)
        return CapabilityLogicalOwnerBinding(
            owner_id=self.logical_owner_id,
            selector_id=f"{self.capability_id}-core-modules",
            match_kind="module_tree",
            value=self.canonical_module_tree,
            state_owner_ids=state_owner_ids,
        )

    def source_logical_owner_bindings(self) -> tuple[CapabilityLogicalOwnerBinding, ...]:
        """Return exact compatibility selectors without widening a flat prefix."""

        state_owner_ids = () if self.state is None else (self.state.state_owner_id,)
        return tuple(
            CapabilityLogicalOwnerBinding(
                owner_id=self.logical_owner_id,
                selector_id=(f"{self.capability_id}-legacy-{binding.role.replace('_', '-')}"),
                match_kind="exact_module",
                value=binding.legacy_module_id,
                state_owner_ids=state_owner_ids,
            )
            for binding in self.modules
            if binding.legacy_module_id is not None
        )

    def logical_owner_bindings(self) -> tuple[CapabilityLogicalOwnerBinding, ...]:
        """Return canonical-tree and exact-source bindings in stable order."""

        return (self.canonical_logical_owner_binding(), *self.source_logical_owner_bindings())

    def as_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "architecture_family_id": self.architecture_family_id,
            "compatibility_family_id": self.compatibility_family_id,
            "logical_owner_id": self.logical_owner_id,
            "canonical_module_tree": self.canonical_module_tree,
            "modules": [item.as_payload() for item in self.modules],
            "route": None if self.route is None else self.route.as_payload(),
            "state": None if self.state is None else self.state.as_payload(),
            "test_roots": list(self.test_roots),
            "executable_module_ids": list(self.executable_module_ids),
        }


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    """Canonical, immutable capability registry with disjoint resolvers."""

    schema: Literal["neocortex.capability-registry/v1"]
    capabilities: tuple[CapabilitySpec, ...]

    def __post_init__(self) -> None:
        if self.schema != CAPABILITY_REGISTRY_SCHEMA:
            raise ValueError("capability registry schema is invalid")
        if not self.capabilities or any(
            not isinstance(item, CapabilitySpec) for item in self.capabilities
        ):
            raise ValueError("capability registry requires typed capabilities")
        capability_ids = tuple(item.capability_id for item in self.capabilities)
        _unique_texts("capability id", capability_ids, ordered=True)
        logical_owner_ids = tuple(item.logical_owner_id for item in self.capabilities)
        _unique_texts("capability logical owner id", logical_owner_ids)
        route_names = tuple(
            item.route.route_name for item in self.capabilities if item.route is not None
        )
        _unique_texts("capability route name", route_names)
        state_store_ids = tuple(
            item.state.state_store_id for item in self.capabilities if item.state is not None
        )
        _unique_texts("capability state store id", state_store_ids)

        trees = tuple(item.canonical_module_tree for item in self.capabilities)
        _unique_texts("canonical capability module tree", trees)
        for index, tree in enumerate(trees):
            if any(
                index != other_index
                and (_module_in_tree(tree, other) or _module_in_tree(other, tree))
                for other_index, other in enumerate(trees)
            ):
                raise ValueError("canonical capability module trees cannot overlap")

        canonical_modules = tuple(
            binding.canonical_module_id
            for capability in self.capabilities
            for binding in capability.modules
        )
        _unique_texts("canonical capability module", canonical_modules)
        legacy_modules = tuple(
            binding.legacy_module_id
            for capability in self.capabilities
            for binding in capability.modules
            if binding.legacy_module_id is not None
        )
        _unique_texts("legacy capability module", legacy_modules)
        if set(canonical_modules) & set(legacy_modules):
            raise ValueError("legacy and canonical capability modules cannot overlap")
        cross_namespace_matches = tuple(
            (legacy_module, canonical_tree)
            for legacy_module in legacy_modules
            for canonical_tree in trees
            if _module_in_tree(legacy_module, canonical_tree)
        )
        if cross_namespace_matches:
            raise ValueError("legacy capability modules cannot match any canonical capability tree")

        owner_bindings = tuple(
            binding
            for capability in self.capabilities
            for binding in capability.logical_owner_bindings()
        )
        _unique_texts(
            "capability logical owner selector id",
            tuple(binding.selector_id for binding in owner_bindings),
        )

    def by_id(self, capability_id: str) -> CapabilitySpec:
        selected = _identifier("capability id", capability_id, _CAPABILITY_ID)
        match = next(
            (item for item in self.capabilities if item.capability_id == selected),
            None,
        )
        if match is None:
            raise ValueError(f"unknown capability: {selected}")
        return match

    def by_route(self, route_name: str) -> CapabilitySpec:
        selected = _identifier("capability route name", route_name, _CAPABILITY_ID)
        match = next(
            (
                item
                for item in self.capabilities
                if item.route is not None and item.route.route_name == selected
            ),
            None,
        )
        if match is None:
            raise ValueError(f"unknown capability route: {selected}")
        return match

    def resolve_source(self, module_id: str) -> tuple[CapabilitySpec, ...]:
        """Resolve an exact flat source module; never inspect canonical trees."""

        selected = _module_id("source capability module id", module_id)
        return tuple(
            capability
            for capability in self.capabilities
            if any(binding.legacy_module_id == selected for binding in capability.modules)
        )

    def resolve_canonical(self, module_id: str) -> tuple[CapabilitySpec, ...]:
        """Resolve a canonical module tree; never fall back to source modules."""

        selected = _module_id("canonical capability module id", module_id)
        return tuple(
            capability
            for capability in self.capabilities
            if _module_in_tree(selected, capability.canonical_module_tree)
        )

    def target_families(
        self,
        module_id: str,
        *,
        resolver_mode: ModuleResolutionMode,
    ) -> tuple[str, ...]:
        if resolver_mode == "source":
            matches = self.resolve_source(module_id)
        elif resolver_mode == "canonical":
            matches = self.resolve_canonical(module_id)
        else:
            raise ValueError("capability resolver mode is invalid")
        return tuple(dict.fromkeys(item.architecture_family_id for item in matches))

    def as_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "capabilities": [item.as_payload() for item in self.capabilities],
        }


def resolve_source_capabilities(module_id: str) -> tuple[CapabilitySpec, ...]:
    return CAPABILITY_REGISTRY.resolve_source(module_id)


def resolve_canonical_capabilities(module_id: str) -> tuple[CapabilitySpec, ...]:
    return CAPABILITY_REGISTRY.resolve_canonical(module_id)


def source_target_architecture_families(module_id: str) -> tuple[str, ...]:
    return CAPABILITY_REGISTRY.target_families(module_id, resolver_mode="source")


def canonical_target_architecture_families(module_id: str) -> tuple[str, ...]:
    return CAPABILITY_REGISTRY.target_families(module_id, resolver_mode="canonical")


def source_compatibility_architecture_families(module_id: str) -> tuple[str, ...]:
    """Resolve explicit compatibility families for exact source modules only."""

    return tuple(
        dict.fromkeys(
            item.compatibility_family_id for item in CAPABILITY_REGISTRY.resolve_source(module_id)
        )
    )


def capability_canonical_logical_owner_bindings() -> tuple[CapabilityLogicalOwnerBinding, ...]:
    """Return one canonical module-tree selector per capability."""

    return tuple(
        item.canonical_logical_owner_binding() for item in CAPABILITY_REGISTRY.capabilities
    )


def capability_source_logical_owner_bindings() -> tuple[CapabilityLogicalOwnerBinding, ...]:
    """Return exact selectors for source compatibility modules."""

    return tuple(
        binding
        for item in CAPABILITY_REGISTRY.capabilities
        for binding in item.source_logical_owner_bindings()
    )


def capability_logical_owner_bindings() -> tuple[CapabilityLogicalOwnerBinding, ...]:
    """Return canonical and compatibility selectors without importing the adapter."""

    return tuple(
        binding
        for item in CAPABILITY_REGISTRY.capabilities
        for binding in item.logical_owner_bindings()
    )


def capability_registry_payload() -> dict[str, object]:
    return CAPABILITY_REGISTRY.as_payload()


def capability_registry_canonical_json() -> str:
    return json.dumps(
        capability_registry_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def capability_registry_fingerprint() -> str:
    digest = hashlib.sha256(capability_registry_canonical_json().encode("utf-8")).hexdigest()
    return CAPABILITY_REGISTRY_FINGERPRINT_PREFIX + digest


def _mapping(value: object, label: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} fields are invalid")
    return value


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _parse_symbol(value: object) -> PythonSymbolRef:
    raw = _mapping(value, "Python symbol reference", {"module_id", "symbol_name"})
    return PythonSymbolRef(
        module_id=_required_text("Python symbol module id", raw["module_id"]),
        symbol_name=_required_text("Python symbol name", raw["symbol_name"]),
    )


def _parse_module(value: object) -> CapabilityModuleBinding:
    raw = _mapping(
        value,
        "capability module binding",
        {
            "role",
            "canonical_module_id",
            "legacy_module_id",
            "public_symbols",
            "warning_policy",
        },
    )
    legacy = raw["legacy_module_id"]
    if legacy is not None and not isinstance(legacy, str):
        raise ValueError("legacy capability module id is invalid")
    return CapabilityModuleBinding(
        role=_required_text("capability module role", raw["role"]),
        canonical_module_id=_required_text(
            "canonical capability module id", raw["canonical_module_id"]
        ),
        legacy_module_id=legacy,
        public_symbols=tuple(
            _required_text("capability public symbol", item)
            for item in _sequence(raw["public_symbols"], "capability public symbols")
        ),
        warning_policy=cast(
            WarningPolicy,
            _required_text("capability warning policy", raw["warning_policy"]),
        ),
    )


def _parse_route(value: object) -> CapabilityRouteContract:
    raw = _mapping(
        value,
        "capability route contract",
        {
            "route_name",
            "input_source",
            "subject_match_kind",
            "subject_value",
            "route_class",
            "config_class",
            "summary_class",
            "version_symbol",
        },
    )
    return CapabilityRouteContract(
        route_name=_required_text("capability route name", raw["route_name"]),
        input_source=cast(
            RouteInputSource,
            _required_text("capability route input source", raw["input_source"]),
        ),
        subject_match_kind=cast(
            RouteSubjectMatchKind,
            _required_text("capability route subject match kind", raw["subject_match_kind"]),
        ),
        subject_value=_required_text("capability route subject", raw["subject_value"]),
        route_class=_parse_symbol(raw["route_class"]),
        config_class=_parse_symbol(raw["config_class"]),
        summary_class=_parse_symbol(raw["summary_class"]),
        version_symbol=_parse_symbol(raw["version_symbol"]),
    )


def _parse_state(value: object) -> CapabilityStateContract:
    raw = _mapping(
        value,
        "capability state contract",
        {
            "state_owner_id",
            "state_store_id",
            "database_name",
            "knowledge_path_attribute",
            "expected_schema_version",
            "knowledge_read_kind",
            "knowledge_capture_mode",
            "state_module_id",
            "schema_module_id",
            "schema_version_symbol",
            "storage_engine",
        },
    )
    schema_version = raw["expected_schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("capability schema version is invalid")
    return CapabilityStateContract(
        state_owner_id=_required_text("capability state owner id", raw["state_owner_id"]),
        state_store_id=_required_text("capability state store id", raw["state_store_id"]),
        database_name=_required_text("capability database name", raw["database_name"]),
        knowledge_path_attribute=_required_text(
            "capability Knowledge path attribute", raw["knowledge_path_attribute"]
        ),
        expected_schema_version=schema_version,
        knowledge_read_kind=_required_text(
            "capability Knowledge read kind", raw["knowledge_read_kind"]
        ),
        knowledge_capture_mode=cast(
            KnowledgeCaptureMode,
            _required_text("capability Knowledge capture mode", raw["knowledge_capture_mode"]),
        ),
        state_module_id=_required_text("capability state module id", raw["state_module_id"]),
        schema_module_id=_required_text("capability schema module id", raw["schema_module_id"]),
        schema_version_symbol=_parse_symbol(raw["schema_version_symbol"]),
        storage_engine=cast(
            Literal["sqlite"],
            _required_text("capability state storage engine", raw["storage_engine"]),
        ),
    )


def _parse_capability(value: object) -> CapabilitySpec:
    raw = _mapping(
        value,
        "capability spec",
        {
            "capability_id",
            "architecture_family_id",
            "compatibility_family_id",
            "logical_owner_id",
            "canonical_module_tree",
            "modules",
            "route",
            "state",
            "test_roots",
            "executable_module_ids",
        },
    )
    route = None if raw["route"] is None else _parse_route(raw["route"])
    state = None if raw["state"] is None else _parse_state(raw["state"])
    return CapabilitySpec(
        capability_id=_required_text("capability id", raw["capability_id"]),
        architecture_family_id=_required_text(
            "capability architecture family id", raw["architecture_family_id"]
        ),
        compatibility_family_id=_required_text(
            "capability compatibility family id", raw["compatibility_family_id"]
        ),
        logical_owner_id=_required_text("capability logical owner id", raw["logical_owner_id"]),
        canonical_module_tree=_required_text(
            "canonical capability module tree", raw["canonical_module_tree"]
        ),
        modules=tuple(
            _parse_module(item) for item in _sequence(raw["modules"], "capability module bindings")
        ),
        route=route,
        state=state,
        test_roots=tuple(
            _required_text("capability test root", item)
            for item in _sequence(raw["test_roots"], "capability test roots")
        ),
        executable_module_ids=tuple(
            _required_text("capability executable module", item)
            for item in _sequence(raw["executable_module_ids"], "capability executable modules")
        ),
    )


def parse_capability_registry_payload(payload: Mapping[str, object]) -> CapabilityRegistry:
    raw = _mapping(payload, "capability registry", {"schema", "capabilities"})
    schema = raw["schema"]
    if schema != CAPABILITY_REGISTRY_SCHEMA:
        raise ValueError("capability registry payload schema is invalid")
    return CapabilityRegistry(
        schema=CAPABILITY_REGISTRY_SCHEMA,
        capabilities=tuple(
            _parse_capability(item)
            for item in _sequence(raw["capabilities"], "capability registry entries")
        ),
    )


CAPABILITY_REGISTRY = CapabilityRegistry(
    schema=CAPABILITY_REGISTRY_SCHEMA,
    capabilities=tuple(_parse_capability(item) for item in CAPABILITY_SPEC_PAYLOADS),
)


__all__ = [
    "CAPABILITY_REGISTRY",
    "CAPABILITY_REGISTRY_FINGERPRINT_PREFIX",
    "CAPABILITY_REGISTRY_SCHEMA",
    "CapabilityLogicalOwnerBinding",
    "CapabilityModuleBinding",
    "CapabilityRegistry",
    "CapabilityRouteContract",
    "CapabilitySpec",
    "CapabilityStateContract",
    "PythonSymbolRef",
    "canonical_target_architecture_families",
    "capability_canonical_logical_owner_bindings",
    "capability_logical_owner_bindings",
    "capability_registry_canonical_json",
    "capability_registry_fingerprint",
    "capability_registry_payload",
    "capability_source_logical_owner_bindings",
    "parse_capability_registry_payload",
    "resolve_canonical_capabilities",
    "resolve_source_capabilities",
    "source_compatibility_architecture_families",
    "source_target_architecture_families",
]
