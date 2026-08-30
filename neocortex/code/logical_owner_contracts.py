"""Explicit, versioned package and logical ownership declarations.

The registry is intentionally declarative.  A selector is evidence that the
repository assigned a module to an owner; it is not a heuristic derived from a
path at analysis time.  Version 2 distinguishes physical package ownership from
logical/domain ownership and still has no catch-all owner.  Unmatched modules
remain unknown and overlapping declarations remain visible to the analyzer.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass

from neocortex.code.code_architecture_contracts import stable_architecture_id
from neocortex.platform.capability_registry import capability_logical_owner_bindings
from neocortex.semantic.semantic_models import canonical_json

LOGICAL_OWNER_CONTRACT_SCHEMA = "neocortex.logical-owner-contract/v2"
PACKAGE_OWNER_CONTRACT_SCHEMA = "neocortex.package-owner-contract/v1"


@dataclass(frozen=True, slots=True)
class LogicalOwnerSelector:
    selector_id: str
    match_kind: str
    value: str

    def __post_init__(self) -> None:
        if not self.selector_id or not self.value:
            raise ValueError("logical-owner selector identity and value are required")
        if self.match_kind not in {"exact_module", "module_tree", "module_prefix"}:
            raise ValueError("logical-owner selector kind is invalid")
        if self.match_kind == "module_tree" and self.value.endswith("."):
            raise ValueError("logical-owner module-tree selector must omit the trailing dot")

    def matches(self, module_id: str) -> bool:
        if not module_id:
            raise ValueError("logical-owner module identity is required")
        if self.match_kind == "exact_module":
            return module_id == self.value
        if self.match_kind == "module_tree":
            return module_id == self.value or module_id.startswith(self.value + ".")
        return module_id.startswith(self.value)


@dataclass(frozen=True, slots=True)
class LogicalOwnerSpec:
    owner_id: str
    selectors: tuple[LogicalOwnerSelector, ...]
    state_owner_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.owner_id or not self.selectors:
            raise ValueError("logical-owner spec requires an identity and selectors")
        if len({item.selector_id for item in self.selectors}) != len(self.selectors):
            raise ValueError("logical-owner selector identities cannot repeat")
        if len(set(self.state_owner_ids)) != len(self.state_owner_ids):
            raise ValueError("logical-owner state-owner identities cannot repeat")


@dataclass(frozen=True, slots=True)
class PackageOwnerSpec:
    """One explicit owner for a physical Python package namespace."""

    package_owner_id: str
    module_tree: str
    responsibility: str

    def __post_init__(self) -> None:
        if not self.package_owner_id or not self.module_tree or not self.responsibility:
            raise ValueError("package-owner identity, module tree, and responsibility are required")
        if self.module_tree.endswith("."):
            raise ValueError("package-owner module tree must omit the trailing dot")

    def matches(self, module_id: str) -> bool:
        if not module_id:
            raise ValueError("package-owner module identity is required")
        return module_id == self.module_tree or module_id.startswith(self.module_tree + ".")


def _selector(selector_id: str, match_kind: str, value: str) -> LogicalOwnerSelector:
    return LogicalOwnerSelector(selector_id, match_kind, value)


def _registered_capability_owner_spec(owner_id: str) -> LogicalOwnerSpec:
    """Compose canonical selectors from the capability SSOT."""

    bindings = tuple(
        item for item in capability_logical_owner_bindings() if item.owner_id == owner_id
    )
    if not bindings:
        raise ValueError(f"registered capability owner is unavailable: {owner_id}")
    state_owner_ids = {item.state_owner_ids for item in bindings}
    if len(state_owner_ids) != 1:
        raise ValueError(f"registered capability state ownership disagrees: {owner_id}")
    return LogicalOwnerSpec(
        owner_id,
        tuple(_selector(item.selector_id, item.match_kind, item.value) for item in bindings),
        next(iter(state_owner_ids)),
    )


# Canonical order is owner_id order.  Prefixes below are explicit source
# declarations, not a runtime inference policy.  Broad utility/orchestration
# modules deliberately remain unmapped unless their ownership is contractual.
LOGICAL_OWNER_SPECS = (
    _registered_capability_owner_spec("archive"),
    _registered_capability_owner_spec("audio"),
    LogicalOwnerSpec(
        "capability",
        (
            _selector("capability-broker", "exact_module", "neocortex.capabilities.broker"),
            _selector("capability-manifests", "exact_module", "neocortex.capabilities"),
        ),
    ),
    LogicalOwnerSpec(
        "catalog",
        (
            _selector("document-catalog", "exact_module", "neocortex.documents.document_catalog"),
            _selector(
                "document-catalog-schema",
                "exact_module",
                "neocortex.documents.document_catalog_schema",
            ),
        ),
        ("catalog",),
    ),
    LogicalOwnerSpec(
        "code-analysis",
        (
            _selector("code-analysis-modules", "module_tree", "neocortex.code"),
            _selector(
                "external-analysis-providers",
                "module_prefix",
                "neocortex.code.external_",
            ),
            _selector(
                "self-analysis-modules",
                "module_prefix",
                "neocortex.workflow.self_analysis.self_analysis",
            ),
            _selector(
                "logical-owner-contracts",
                "exact_module",
                "neocortex.code.logical_owner_contracts",
            ),
            _selector(
                "state-topology-contracts",
                "exact_module",
                "neocortex.safety.state_topology_contracts",
            ),
        ),
        ("code",),
    ),
    LogicalOwnerSpec(
        "deduplication",
        (
            _selector(
                "deduplication-canonical-package",
                "module_tree",
                "neocortex.deduplication",
            ),
        ),
        ("inventory",),
    ),
    _registered_capability_owner_spec("docx"),
    LogicalOwnerSpec(
        "enumeration",
        (
            _selector(
                "enumeration-canonical-package",
                "module_tree",
                "neocortex.enumeration",
            ),
        ),
        ("inventory",),
    ),
    LogicalOwnerSpec(
        "framework",
        (
            _selector(
                "framework-core-modules",
                "module_prefix",
                "neocortex.persistence.framework_",
            ),
        ),
        ("framework",),
    ),
    _registered_capability_owner_spec("image"),
    LogicalOwnerSpec(
        "interface",
        (
            _selector("cli-core-modules", "module_prefix", "neocortex.api.cli.cli_"),
            _selector("gui-package", "module_tree", "neocortex.interface"),
            _selector("public-cli", "exact_module", "neocortex.interface.entrypoint"),
            _selector("public-human-cli", "exact_module", "neocortex.api.cli.human"),
            _selector("public-read-api", "exact_module", "neocortex.api.read_api"),
        ),
    ),
    LogicalOwnerSpec(
        "inventory",
        (
            _selector(
                "inventory-boundary",
                "exact_module",
                "neocortex.integrations.inventory.inventory_boundary",
            ),
            _selector(
                "inventory-coordinator",
                "exact_module",
                "neocortex.integrations.inventory.inventory_coordinator",
            ),
        ),
        ("inventory",),
    ),
    LogicalOwnerSpec(
        "knowledge",
        (
            _selector(
                "knowledge-core-modules",
                "module_prefix",
                "neocortex.knowledge",
            ),
        ),
    ),
    _registered_capability_owner_spec("office"),
    LogicalOwnerSpec(
        "orchestration",
        tuple(
            _selector(f"orchestration-{name.replace('_', '-')}", "exact_module", name)
            for name in (
                "neocortex.workflow.actions.action_policy",
                "neocortex.workflow.actions.actions",
                "neocortex.runtime.orchestration.orchestrator",
                "neocortex.runtime.orchestration.route_registry",
                "neocortex.runtime.orchestration.route_selection",
                "neocortex.runtime.orchestration.run_lifecycle",
                "neocortex.runtime.orchestration.run_status",
            )
        ),
    ),
    LogicalOwnerSpec(
        "pdf",
        (_selector("pdf-core-modules", "module_prefix", "neocortex.capabilities.formats.pdf"),),
        ("pdf",),
    ),
    LogicalOwnerSpec(
        "progress",
        (
            _selector(
                "progress-canonical-package",
                "module_tree",
                "neocortex.progress",
            ),
        ),
    ),
    LogicalOwnerSpec(
        "retention",
        (
            _selector(
                "retention-core-modules",
                "module_prefix",
                "neocortex.workflow.retention",
            ),
        ),
    ),
    LogicalOwnerSpec(
        "review",
        (
            _selector(
                "review-task-core-modules",
                "module_prefix",
                "neocortex.workflow.review.review_",
            ),
            _selector(
                "review-task-canonical-modules",
                "module_prefix",
                "neocortex.workflow.review.review",
            ),
            _selector(
                "value-review-core-modules",
                "module_prefix",
                "neocortex.workflow.review.value_review_",
            ),
            _selector(
                "value-review-canonical-modules",
                "module_prefix",
                "neocortex.workflow.review.value_review",
            ),
            _selector(
                "review-task-public-adapter",
                "exact_module",
                "neocortex.api.cli.review_task",
            ),
            _selector(
                "value-review-public-adapter",
                "exact_module",
                "neocortex.api.cli.value_review",
            ),
        ),
        ("framework",),
    ),
    LogicalOwnerSpec(
        "runtime",
        (
            _selector(
                "runtime-canonical-package",
                "module_tree",
                "neocortex.runtime",
            ),
        ),
    ),
    LogicalOwnerSpec(
        "semantic",
        (
            _selector(
                "semantic-core-modules",
                "module_prefix",
                "neocortex.semantic",
            ),
        ),
        ("semantic",),
    ),
    LogicalOwnerSpec(
        "text",
        (_selector("text-core-modules", "module_prefix", "neocortex.capabilities.formats.text"),),
        ("text",),
    ),
    _registered_capability_owner_spec("video"),
)


PACKAGE_OWNER_SPECS = (
    PackageOwnerSpec("product", "neocortex", "canonical_product_namespace"),
)


def _validate_registries() -> None:
    logical_ids = tuple(item.owner_id for item in LOGICAL_OWNER_SPECS)
    if logical_ids != tuple(sorted(logical_ids)) or len(set(logical_ids)) != len(logical_ids):
        raise ValueError("logical-owner registry must be unique and canonically ordered")
    selector_ids = tuple(
        selector.selector_id for spec in LOGICAL_OWNER_SPECS for selector in spec.selectors
    )
    if len(set(selector_ids)) != len(selector_ids):
        raise ValueError("logical-owner selector identities must be globally unique")
    package_ids = tuple(item.package_owner_id for item in PACKAGE_OWNER_SPECS)
    if package_ids != tuple(sorted(package_ids)) or len(set(package_ids)) != len(package_ids):
        raise ValueError("package-owner registry must be unique and canonically ordered")
    package_trees = tuple(item.module_tree for item in PACKAGE_OWNER_SPECS)
    if len(set(package_trees)) != len(package_trees):
        raise ValueError("package-owner module trees cannot repeat")


_validate_registries()


def logical_owner_registry_payload() -> dict[str, object]:
    return {
        "schema": LOGICAL_OWNER_CONTRACT_SCHEMA,
        "coverage_policy": "explicit-partial-no-default-owner-v2",
        "owners": tuple(asdict(item) for item in LOGICAL_OWNER_SPECS),
        "package_registry_fingerprint": package_owner_registry_fingerprint(),
    }


def package_owner_registry_payload() -> dict[str, object]:
    return {
        "schema": PACKAGE_OWNER_CONTRACT_SCHEMA,
        "coverage_policy": "explicit-package-tree-no-default-owner-v1",
        "owners": tuple(asdict(item) for item in PACKAGE_OWNER_SPECS),
    }


def logical_owner_registry_fingerprint() -> str:
    return stable_architecture_id(
        "logical-owner-contract-v2",
        canonical_json(logical_owner_registry_payload()),
    )


def package_owner_registry_fingerprint() -> str:
    return stable_architecture_id(
        "package-owner-contract-v1",
        canonical_json(package_owner_registry_payload()),
    )


def matching_logical_owners(module_id: str) -> tuple[str, ...]:
    """Return only explicit selector matches in canonical owner order."""

    return tuple(
        item.owner_id
        for item in LOGICAL_OWNER_SPECS
        if any(selector.matches(module_id) for selector in item.selectors)
    )


def matching_package_owners(module_id: str) -> tuple[str, ...]:
    """Return explicit physical-package owners independently of logical owners."""

    return tuple(item.package_owner_id for item in PACKAGE_OWNER_SPECS if item.matches(module_id))


__all__ = [
    "LOGICAL_OWNER_CONTRACT_SCHEMA",
    "LOGICAL_OWNER_SPECS",
    "PACKAGE_OWNER_CONTRACT_SCHEMA",
    "PACKAGE_OWNER_SPECS",
    "LogicalOwnerSelector",
    "LogicalOwnerSpec",
    "PackageOwnerSpec",
    "logical_owner_registry_fingerprint",
    "logical_owner_registry_payload",
    "matching_logical_owners",
    "matching_package_owners",
    "package_owner_registry_fingerprint",
    "package_owner_registry_payload",
]
