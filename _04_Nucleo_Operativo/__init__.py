"""Compatibility package delegating its public facade to ``neocortex.api``."""

from __future__ import annotations

from typing import Any

from neocortex.api import public as _public

__all__: list[str] = list(_public.__all__)


def __getattr__(name: str) -> Any:
    return getattr(_public, name)


def __dir__() -> list[str]:
    return _public.__dir__()
