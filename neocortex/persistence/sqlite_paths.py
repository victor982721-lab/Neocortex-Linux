"""Compatibility facade for filesystem-safe existing-file SQLite URIs."""

from __future__ import annotations
from neocortex.sqlite_schema_lifecycle import existing_sqlite_uri, readonly_sqlite_uri


# region [01] Existing-file URI policy

__all__ = ["existing_sqlite_uri", "readonly_sqlite_uri"]


# endregion [01]
