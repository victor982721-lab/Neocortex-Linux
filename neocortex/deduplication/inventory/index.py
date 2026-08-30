"""Public facade for the modular incremental deduplication inventory."""
# region [00] Contexto del módulo
# Module: canonical inventory index
# Propósito: composición pública de los repositorios internos del inventario.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from neocortex.progress import ProgressCallback

from ..domain.models import FileSnapshot, ScanSummary
from ..fingerprinting import snapshot_path
from ..persistence import (
    SCHEMA_VERSION as SCHEMA_VERSION,
    connect_existing_inventory_database,
    configure_inventory_connection,
    initialize_inventory_schema,
)
from .repository_connection import ConnectionLifecycleMixin, open_inventory_repository
from .repository_files import FileRepositoryMixin, iter_size_collision_groups
from .repository_plans import PlanRepositoryMixin, _PATH_COLLATION as _PATH_COLLATION
from .repository_reconciliation import (
    ReconciliationRepositoryMixin,
    _reconciliation_identity_rows as _reconciliation_identity_rows,
    _reconciliation_path_rows as _reconciliation_path_rows,
    _refresh_reconciliation_aggregates as _refresh_reconciliation_aggregates,
    _remove_reconciled_rows as _remove_reconciled_rows,
    _upsert_reconciled_snapshot as _upsert_reconciled_snapshot,
)
from .repository_scans import (
    PRUNE_BATCH_SIZE as PRUNE_BATCH_SIZE,
    _delete_batches as _delete_batches,
    _delete_duplicate_group_batches as _delete_duplicate_group_batches,
    scan_inventory,
)
from .scan import (
    DEFAULT_BATCH_SIZE as DEFAULT_BATCH_SIZE,
    DEFAULT_EXCLUDED_PATHS as DEFAULT_EXCLUDED_PATHS,
    DEFAULT_GENERATED_DIRECTORY_FRAGMENTS as DEFAULT_GENERATED_DIRECTORY_FRAGMENTS,
    DEFAULT_GENERATED_DIRECTORY_NAMES as DEFAULT_GENERATED_DIRECTORY_NAMES,
    DEFAULT_GENERATED_DIRECTORY_PREFIXES as DEFAULT_GENERATED_DIRECTORY_PREFIXES,
    DEFAULT_GENERATED_FILE_SUFFIXES as DEFAULT_GENERATED_FILE_SUFFIXES,
    DEFAULT_INVENTORY_EXCLUSION_POLICY as DEFAULT_INVENTORY_EXCLUSION_POLICY,
    FILE_ATTRIBUTE_HIDDEN as FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_REPARSE_POINT as FILE_ATTRIBUTE_REPARSE_POINT,
    INTERNAL_DIRECTORY_PREFIXES as INTERNAL_DIRECTORY_PREFIXES,
    InventoryExclusionPolicy as InventoryExclusionPolicy,
    InventoryScanner,
    exclusion_path_keys as exclusion_path_keys,
    id_blob as _scan_id_blob,
    is_excluded_directory as is_excluded_directory,
    validate_inventory_root as validate_inventory_root,
)
# endregion [01]

# region [02] Implementación

_id_blob = _scan_id_blob


class DedupIndex(
    ConnectionLifecycleMixin,
    ReconciliationRepositoryMixin,
    FileRepositoryMixin,
    PlanRepositoryMixin,
):
    """Persistent inventory and fingerprint cache with bounded transactions."""

    def __init__(self, database: str | Path):
        self.path, self._connection = open_inventory_repository(
            database,
            initialize_schema=initialize_inventory_schema,
            connect_database=connect_existing_inventory_database,
            configure_connection=configure_inventory_connection,
        )

    def scan(
        self,
        root: str | Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        excluded_paths: Iterable[str | Path] | None = None,
        exclusion_policy: InventoryExclusionPolicy | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanSummary:
        """Inventory files under a legacy root list or compiled policy.

        The default policy excludes internal Neocortex work trees, dependency
        environments, generated caches, and Python bytecode. Path matching
        uses the filesystem's native path collation (case-insensitive on
        Windows, case-sensitive on Linux). Links and junctions are never
        followed. ``excluded_paths`` remains compatible; callers needing
        recursive names or file rules pass ``exclusion_policy``.
        """

        return scan_inventory(
            self._connection,
            root,
            batch_size=batch_size,
            excluded_paths=excluded_paths,
            exclusion_policy=exclusion_policy,
            progress=progress,
            scanner_type=InventoryScanner,
        )

    def size_collision_groups(self, scan_id: int) -> Iterator[tuple[FileSnapshot, ...]]:
        yield from iter_size_collision_groups(
            self._connection,
            scan_id,
            snapshot_resolver=snapshot_path,
        )

    def close(self) -> None:
        ConnectionLifecycleMixin.close(self)

    def __enter__(self) -> "DedupIndex":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


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
    "INTERNAL_DIRECTORY_PREFIXES",
    "PRUNE_BATCH_SIZE",
    "SCHEMA_VERSION",
    "DedupIndex",
    "InventoryExclusionPolicy",
    "exclusion_path_keys",
    "is_excluded_directory",
    "validate_inventory_root",
]


# endregion [02]
