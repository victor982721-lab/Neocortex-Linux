"""Canonical persistence API for the deduplication inventory schema lifecycle."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import connections as _connections
from . import contracts as _contracts
from . import ddl as _ddl
from . import lifecycle as _lifecycle
from . import migrations as _migrations
from . import validation as _validation
from .migrations import common as _migration_common
from .migrations import v1_to_v2 as _v1_to_v2
from .migrations import v2_to_v3 as _v2_to_v3
from .migrations import v3_to_v4 as _v3_to_v4
from .migrations import v4_to_v5 as _v4_to_v5
from .migrations import v5_to_v6 as _v5_to_v6
from .migrations import v6_to_v7 as _v6_to_v7
from .migrations import v7_to_v8 as _v7_to_v8
from .migrations import v8_to_v9 as _v8_to_v9
from .migrations import v9_to_v10 as _v9_to_v10

SCHEMA_VERSION = _ddl.SCHEMA_VERSION
configure_inventory_connection = _connections.configure_inventory_connection
connect_existing_inventory_database = _connections.connect_existing_inventory_database
inventory_schema_contract = _contracts.inventory_schema_contract
validate_inventory_schema = _validation.validate_inventory_schema

# Shared schema lifecycle helpers used by the inventory persistence owner.
_SCHEMA_LABEL = _ddl.SCHEMA_LABEL
_PATH_COLLATION = _ddl.PATH_COLLATION
_METADATA_DDL = _ddl.METADATA_DDL
_V8_CHECKPOINT_DDL = _ddl.V8_CHECKPOINT_DDL
_V9_CHECKPOINT_DDL = _ddl.V9_CHECKPOINT_DDL
_V9_PLANNED_MEMBERS_PATH_INDEX_DDL = _ddl.V9_PLANNED_MEMBERS_PATH_INDEX_DDL
_V9_DDL = _ddl.V9_DDL
_V10_INDEX_DDL = _ddl.V10_INDEX_DDL
_CURRENT_DDL = _ddl.CURRENT_DDL
_CURRENT_SHARED_DDL_START = _ddl.CURRENT_SHARED_DDL_START
_LEGACY_SHARED_DDL = _ddl.LEGACY_SHARED_DDL
_V8_GENERATIONAL_DDL = _ddl.V8_GENERATIONAL_DDL
_V7_GENERATIONAL_DDL = _ddl.V7_GENERATIONAL_DDL
_V6_GENERATIONAL_DDL = _ddl.V6_GENERATIONAL_DDL
_V2_OBJECT_DDL = _ddl.V2_OBJECT_DDL
_SCAN_COUNTER_COLUMNS = _ddl.SCAN_COUNTER_COLUMNS
_SCAN_ROOT_COLUMNS = _ddl.SCAN_ROOT_COLUMNS
_execute_ddl = _ddl.execute_ddl
_build_metadata_schema = _ddl.build_metadata_schema
_build_current_schema = _ddl.build_current_schema
_build_v6_schema = _ddl.build_v6_schema
_build_v7_schema = _ddl.build_v7_schema
_build_v8_schema = _ddl.build_v8_schema
_build_v9_schema = _ddl.build_v9_schema

_configure_owner_connection = _connections.configure_owner_connection
_metadata_contract = _contracts.metadata_contract
_inventory_v6_schema_contract = _contracts.inventory_v6_schema_contract
_inventory_v7_schema_contract = _contracts.inventory_v7_schema_contract
_inventory_v8_schema_contract = _contracts.inventory_v8_schema_contract
_inventory_v9_schema_contract = _contracts.inventory_v9_schema_contract
_validate_metadata = _validation.validate_metadata
_column_names = _migration_common.column_names
_add_columns = _migration_common.add_columns
_ensure_v2_objects = _migration_common.ensure_v2_objects
_add_scan_counters = _migration_common.add_scan_counters
_add_fingerprint_birthtime = _migration_common.add_fingerprint_birthtime
_add_scan_root_identity = _migration_common.add_scan_root_identity
_invalidate_checkpoints = _migration_common.invalidate_checkpoints
_advance_version = _migration_common.advance_version
_migrate_one_to_two = _v1_to_v2.migrate
_migrate_two_to_three = _v2_to_v3.migrate
_migrate_three_to_four = _v3_to_v4.migrate
_migrate_four_to_five = _v4_to_v5.migrate
_migrate_five_to_six = _v5_to_v6.migrate
_migrate_six_to_seven = _v6_to_v7.migrate
_migrate_seven_to_eight = _v7_to_v8.migrate
_migrate_eight_to_nine = _v8_to_v9.migrate
_migrate_nine_to_ten = _v9_to_v10.migrate
_MIGRATIONS = _migrations.MIGRATIONS
_migrate = _migrations.migrate
_create_fresh = _lifecycle.create_fresh


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open one inventory database with the canonical connection policy."""

    return _connections.connect(path, readonly=readonly)


def initialize_inventory_schema(database: str | Path) -> None:
    """Create, migrate, or read-only validate one inventory database."""

    _lifecycle.initialize_inventory_schema(database, connect_factory=_connect)


__all__ = [
    "SCHEMA_VERSION",
    "configure_inventory_connection",
    "initialize_inventory_schema",
    "inventory_schema_contract",
    "validate_inventory_schema",
]
