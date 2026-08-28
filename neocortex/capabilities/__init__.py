"""Capability implementations grouped by product responsibility.

The runtime probe contract used to live in a flat ``capabilities.py`` module.
It now resides in :mod:`neocortex.capabilities.runtime`, while this package
keeps the historical public imports and hosts format-specific implementations.
"""

from __future__ import annotations

from . import runtime as _runtime

for _name in _runtime.__all__:
    globals()[_name] = getattr(_runtime, _name)

# Preserve the narrow monkeypatch seams used by existing callers while the
# implementation lives in the responsibility module.
os = _runtime.os
hashlib = _runtime.hashlib
_binary_identity = _runtime._binary_identity

__all__ = [*_runtime.__all__, "formats"]

del _name, _runtime
