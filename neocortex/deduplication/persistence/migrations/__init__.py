"""Ordered inventory migration registry."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ...domain.errors import InventoryError
from ..ddl import SCHEMA_VERSION
from . import (
    v1_to_v2,
    v2_to_v3,
    v3_to_v4,
    v4_to_v5,
    v5_to_v6,
    v6_to_v7,
    v7_to_v8,
    v8_to_v9,
    v9_to_v10,
    v10_to_v11,
)
from .common import advance_version


Migration = Callable[[sqlite3.Connection], None]

MIGRATIONS: dict[int, Migration] = {
    1: v1_to_v2.migrate,
    2: v2_to_v3.migrate,
    3: v3_to_v4.migrate,
    4: v4_to_v5.migrate,
    5: v5_to_v6.migrate,
    6: v6_to_v7.migrate,
    7: v7_to_v8.migrate,
    8: v8_to_v9.migrate,
    9: v9_to_v10.migrate,
    10: v10_to_v11.migrate,
}


def migrate(connection: sqlite3.Connection, version: int) -> None:
    """Advance every version sequentially inside the caller's transaction."""

    while version < SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise InventoryError(f"no inventory migration exists for schema {version}")
        migration(connection)
        version += 1
        advance_version(connection, version)
