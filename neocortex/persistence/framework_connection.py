"""Owner-specific connection policy for existing framework state."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/framework_connection.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import sqlite3
from pathlib import Path

from neocortex.persistence.sqlite_paths import existing_sqlite_uri, readonly_sqlite_uri
# endregion [01]

# region [02] Implementación


def connect_existing_framework(
    path: str | Path,
    *,
    readonly: bool,
    timeout_seconds: float = 60.0,
) -> sqlite3.Connection:
    """Open existing framework state without ever creating a replacement file."""

    if timeout_seconds <= 0:
        raise ValueError("framework SQLite timeout must be positive")
    uri = readonly_sqlite_uri(path) if readonly else existing_sqlite_uri(path)
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout={max(1, round(timeout_seconds * 1000))}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise RuntimeError("framework connection could not enable foreign keys")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("framework connection is not query-only")
    except BaseException:
        connection.close()
        raise
    return connection


__all__ = ["connect_existing_framework"]
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.framework_connection")
