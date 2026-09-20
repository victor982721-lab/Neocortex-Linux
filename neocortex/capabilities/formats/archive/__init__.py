"""Import-light ZIP intake classification helpers.

Generic ZIPs are temporary transport and are handled by the ZIP intake owner.
The former virtual-member route/state/materialization API is intentionally not
part of this package's public surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ArchiveUnitClassification": (".units", "ArchiveUnitClassification"),
    "classify_archive": (".units", "classify_archive"),
    "classify_archive_bytes": (".units", "classify_archive_bytes"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(name) from error
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = sorted(_EXPORTS)
