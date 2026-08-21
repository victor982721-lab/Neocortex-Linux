"""Inventory persistence, policy and scanning boundaries."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .index import DedupIndex as DedupIndex
    from .scan import (
        DEFAULT_INVENTORY_EXCLUSION_POLICY as DEFAULT_INVENTORY_EXCLUSION_POLICY,
        InventoryExclusionPolicy as InventoryExclusionPolicy,
        InventoryScanner as InventoryScanner,
        exclusion_path_keys as exclusion_path_keys,
        is_excluded_directory as is_excluded_directory,
        resolve_inventory_exclusion_policy as resolve_inventory_exclusion_policy,
        validate_inventory_root as validate_inventory_root,
    )

_EXPORTS: Final = {
    "DEFAULT_INVENTORY_EXCLUSION_POLICY": (
        ".scan",
        "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    ),
    "DedupIndex": (".index", "DedupIndex"),
    "InventoryExclusionPolicy": (".scan", "InventoryExclusionPolicy"),
    "InventoryScanner": (".scan", "InventoryScanner"),
    "exclusion_path_keys": (".scan", "exclusion_path_keys"),
    "is_excluded_directory": (".scan", "is_excluded_directory"),
    "resolve_inventory_exclusion_policy": (".scan", "resolve_inventory_exclusion_policy"),
    "validate_inventory_root": (".scan", "validate_inventory_root"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    "DedupIndex",
    "InventoryExclusionPolicy",
    "InventoryScanner",
    "exclusion_path_keys",
    "is_excluded_directory",
    "resolve_inventory_exclusion_policy",
    "validate_inventory_root",
]
