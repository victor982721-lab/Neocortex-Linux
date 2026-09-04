"""Stable inventory scanning facade composed from focused implementation modules."""

from __future__ import annotations

# ``os`` remains available for the established scandir monkeypatch seam. All
# implementation modules reference the same process-wide module object.
import os as os

from .policy import (
    DEFAULT_EXCLUDED_PATHS as DEFAULT_EXCLUDED_PATHS,
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS as DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_NAMES as DEFAULT_GENERATED_DIRECTORY_NAMES,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES as DEFAULT_GENERATED_DIRECTORY_PREFIXES,
    DEFAULT_GENERATED_FILE_SUFFIXES as DEFAULT_GENERATED_FILE_SUFFIXES,
    DEFAULT_INVENTORY_EXCLUSION_POLICY as DEFAULT_INVENTORY_EXCLUSION_POLICY,
    FILE_ATTRIBUTE_HIDDEN as FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_REPARSE_POINT as FILE_ATTRIBUTE_REPARSE_POINT,
    INTERNAL_DIRECTORY_PREFIXES as INTERNAL_DIRECTORY_PREFIXES,
    INVENTORY_EXCLUSION_SIGNATURE_VERSION as INVENTORY_EXCLUSION_SIGNATURE_VERSION,
    MAX_INVENTORY_EXCLUSION_PATH_CHARS as MAX_INVENTORY_EXCLUSION_PATH_CHARS,
    MAX_INVENTORY_EXCLUSION_RULE_CHARS as MAX_INVENTORY_EXCLUSION_RULE_CHARS,
    MAX_INVENTORY_EXCLUSION_RULES as MAX_INVENTORY_EXCLUSION_RULES,
    InventoryExclusionPolicy as InventoryExclusionPolicy,
    exclusion_path_keys as exclusion_path_keys,
    is_excluded_directory as is_excluded_directory,
    resolve_inventory_exclusion_policy as resolve_inventory_exclusion_policy,
)
from .scanner import (
    DEFAULT_BATCH_SIZE as DEFAULT_BATCH_SIZE,
    FILE_UPSERT_SQL as FILE_UPSERT_SQL,
    InventoryScanner as InventoryScanner,
    InventoryScanBudgetExceeded as InventoryScanBudgetExceeded,
    InventoryScanCancelled as InventoryScanCancelled,
    InventoryScanDeadlineExceeded as InventoryScanDeadlineExceeded,
    InventoryWorkBudget as InventoryWorkBudget,
    MAX_BATCH_SIZE as MAX_BATCH_SIZE,
    MAX_SCAN_BYTES as MAX_SCAN_BYTES,
    MAX_SCAN_FILES as MAX_SCAN_FILES,
    id_blob as id_blob,
)
from .traversal import validate_inventory_root as validate_inventory_root


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EXCLUDED_PATHS",
    "DEFAULT_GENERATED_DIRECTORY_FRAGMENTS",
    "DEFAULT_GENERATED_DIRECTORY_NAMES",
    "DEFAULT_GENERATED_DIRECTORY_PREFIXES",
    "DEFAULT_GENERATED_FILE_SUFFIXES",
    "DEFAULT_INVENTORY_EXCLUSION_POLICY",
    "FILE_ATTRIBUTE_HIDDEN",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_UPSERT_SQL",
    "INTERNAL_DIRECTORY_PREFIXES",
    "INVENTORY_EXCLUSION_SIGNATURE_VERSION",
    "MAX_BATCH_SIZE",
    "MAX_SCAN_BYTES",
    "MAX_SCAN_FILES",
    "InventoryExclusionPolicy",
    "InventoryScanBudgetExceeded",
    "InventoryScanCancelled",
    "InventoryScanDeadlineExceeded",
    "InventoryScanner",
    "InventoryWorkBudget",
    "exclusion_path_keys",
    "id_blob",
    "is_excluded_directory",
    "resolve_inventory_exclusion_policy",
    "validate_inventory_root",
]
