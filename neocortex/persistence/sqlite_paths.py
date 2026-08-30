"""Filesystem-safe SQLite URIs for existing owner databases."""

from __future__ import annotations
from pathlib import Path


def readonly_sqlite_uri(path: str | Path) -> str:
    """Return an escaped URI that refuses to create or write a database."""

    return f"{Path(path).resolve(strict=False).as_uri()}?mode=ro"


def existing_sqlite_uri(path: str | Path) -> str:
    """Return an escaped read-write URI that refuses to create a database."""

    return f"{Path(path).resolve(strict=False).as_uri()}?mode=rw"


# region [01] Existing-file URI policy

__all__ = ["existing_sqlite_uri", "readonly_sqlite_uri"]


# endregion [01]
