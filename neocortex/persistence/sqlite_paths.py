"""Compatibility facade for filesystem-safe existing-file SQLite URIs."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from neocortex.sqlite_schema_lifecycle import existing_sqlite_uri, readonly_sqlite_uri


# region [01] Existing-file URI policy

__all__ = ["existing_sqlite_uri", "readonly_sqlite_uri"]


# endregion [01]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.sqlite_paths")
