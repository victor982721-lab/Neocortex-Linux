"""SQLite-backed NTFS path-index repository and schema contract."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .repository import SqlitePathIndex as SqlitePathIndex
    from .schema import (
        SCHEMA_VERSION as SCHEMA_VERSION,
        configure_path_index_connection as configure_path_index_connection,
        initialize_path_index_schema as initialize_path_index_schema,
        path_index_schema_contract as path_index_schema_contract,
        validate_path_index_schema as validate_path_index_schema,
    )

__all__ = [
    "SCHEMA_VERSION",
    "SqlitePathIndex",
    "configure_path_index_connection",
    "initialize_path_index_schema",
    "path_index_schema_contract",
    "validate_path_index_schema",
]

_EXPORTS: Final = {
    "SCHEMA_VERSION": ("neocortex.enumeration.path_index.schema", "SCHEMA_VERSION"),
    "SqlitePathIndex": (
        "neocortex.enumeration.path_index.repository",
        "SqlitePathIndex",
    ),
    "configure_path_index_connection": (
        "neocortex.enumeration.path_index.schema",
        "configure_path_index_connection",
    ),
    "initialize_path_index_schema": (
        "neocortex.enumeration.path_index.schema",
        "initialize_path_index_schema",
    ),
    "path_index_schema_contract": (
        "neocortex.enumeration.path_index.schema",
        "path_index_schema_contract",
    ),
    "validate_path_index_schema": (
        "neocortex.enumeration.path_index.schema",
        "validate_path_index_schema",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
