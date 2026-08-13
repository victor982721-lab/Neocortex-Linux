"""Explicit, versioned logical-owner selectors for the first owner slice.

Selectors are declarations, not name-derived inference.  Version 1 deliberately
covers only the six cross-cutting owners needed by the current architecture and
state questions.  Unmatched modules remain unmapped; they are never assigned to
Framework or another owner by default.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .code_architecture_contracts import stable_architecture_id
from .semantic_models import canonical_json

LOGICAL_OWNER_CONTRACT_SCHEMA = "neocortex.logical-owner-contract/v1"


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


LOGICAL_OWNER_SPECS = (
    LogicalOwnerSpec(
        "framework",
        (
            LogicalOwnerSelector(
                "framework-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.framework_",
            ),
        ),
        ("framework",),
    ),
    LogicalOwnerSpec(
        "knowledge",
        (
            LogicalOwnerSelector(
                "knowledge-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.knowledge_",
            ),
        ),
    ),
    LogicalOwnerSpec(
        "retention",
        (
            LogicalOwnerSelector(
                "retention-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.retention_",
            ),
        ),
    ),
    LogicalOwnerSpec(
        "review",
        (
            LogicalOwnerSelector(
                "review-task-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.review_",
            ),
            LogicalOwnerSelector(
                "value-review-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.value_review_",
            ),
            LogicalOwnerSelector(
                "review-task-public-adapter",
                "exact_module",
                "neocortex.review_task_cli_adapter",
            ),
            LogicalOwnerSelector(
                "value-review-public-adapter",
                "exact_module",
                "neocortex.value_cli_adapter",
            ),
        ),
        ("framework",),
    ),
    LogicalOwnerSpec(
        "semantic",
        (
            LogicalOwnerSelector(
                "semantic-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.semantic_",
            ),
        ),
        ("semantic",),
    ),
    LogicalOwnerSpec(
        "text",
        (
            LogicalOwnerSelector(
                "text-core-modules",
                "module_prefix",
                "_04_Nucleo_Operativo.text_",
            ),
        ),
        ("text",),
    ),
)


def logical_owner_registry_payload() -> dict[str, object]:
    return {
        "schema": LOGICAL_OWNER_CONTRACT_SCHEMA,
        "coverage_policy": "explicit-partial-no-default-owner-v1",
        "owners": tuple(asdict(item) for item in LOGICAL_OWNER_SPECS),
    }


def logical_owner_registry_fingerprint() -> str:
    return stable_architecture_id(
        "logical-owner-contract-v1",
        canonical_json(logical_owner_registry_payload()),
    )


def matching_logical_owners(module_id: str) -> tuple[str, ...]:
    """Return only explicit selector matches in canonical owner order."""

    return tuple(
        item.owner_id
        for item in LOGICAL_OWNER_SPECS
        if any(selector.matches(module_id) for selector in item.selectors)
    )


__all__ = [
    "LOGICAL_OWNER_CONTRACT_SCHEMA",
    "LOGICAL_OWNER_SPECS",
    "LogicalOwnerSelector",
    "LogicalOwnerSpec",
    "logical_owner_registry_fingerprint",
    "logical_owner_registry_payload",
    "matching_logical_owners",
]
