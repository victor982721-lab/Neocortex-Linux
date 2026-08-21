"""Connection lifecycle primitives for the deduplication inventory repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Self


def open_inventory_repository(
    database: str | Path,
    *,
    initialize_schema: Callable[[str | Path], None],
    connect_database: Callable[[Path], sqlite3.Connection],
    configure_connection: Callable[[sqlite3.Connection], None],
) -> tuple[Path, sqlite3.Connection]:
    """Open one configured writer while preserving the constructor patch seams."""

    path = Path(database)
    initialize_schema(path)
    connection = connect_database(path)
    try:
        configure_connection(connection)
    except BaseException:
        connection.close()
        raise
    return path, connection


class ConnectionLifecycleMixin:
    """Own the close and context-manager lifecycle for an inventory writer."""

    _connection: sqlite3.Connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = ["ConnectionLifecycleMixin", "open_inventory_repository"]
