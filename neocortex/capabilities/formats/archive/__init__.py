"""Import-light recursive archive capability package.

The materialization/repair helpers are lazy exports so asking the CLI for help
does not import ZIP engines or create temporary state.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ArchiveEntry": (".materialization", "ArchiveEntry"),
    "ArchiveManifest": (".materialization", "ArchiveManifest"),
    "ArchiveMaterializationLimits": (".materialization", "ArchiveMaterializationLimits"),
    "ArchiveMaterializedOutput": (".materialization", "ArchiveMaterializedOutput"),
    "ArchiveUnitClassification": (".units", "ArchiveUnitClassification"),
    "classify_archive": (".units", "classify_archive"),
    "classify_archive_bytes": (".units", "classify_archive_bytes"),
    "materialize_archive": (".materialization", "materialize_archive"),
    "materialize_zip": (".materialization", "materialize_zip"),
    "is_container_normalized": (".materialization", "is_container_normalized"),
    "repair_zip_candidate": (".repair", "repair_zip_candidate"),
    "ZipRepairLimits": (".repair", "ZipRepairLimits"),
    "ZipRepairResult": (".repair", "ZipRepairResult"),
    "scan_archive": (".materialization", "scan_archive"),
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
