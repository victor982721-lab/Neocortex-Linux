"""Legacy NTFS MFT enumeration and USN journal support.

The implementation is preserved for compatibility but remains Windows-only;
Linux callers use the portable inventory traversal owned by deduplication.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .enumeration import (
        VolumeEnumeration as VolumeEnumeration,
        enumerate_volume as enumerate_volume,
        query_journal_cursor as query_journal_cursor,
    )
    from .journal import (
        UsnJournalReader as UsnJournalReader,
        consume_changes as consume_changes,
    )
    from .parser import (
        parse_enum_buffer as parse_enum_buffer,
        parse_journal_buffer as parse_journal_buffer,
    )
    from .volume import VolumeHandle as VolumeHandle, normalize_volume as normalize_volume

__all__ = [
    "UsnJournalReader",
    "VolumeEnumeration",
    "VolumeHandle",
    "consume_changes",
    "enumerate_volume",
    "normalize_volume",
    "parse_enum_buffer",
    "parse_journal_buffer",
    "query_journal_cursor",
]

_EXPORTS: Final = {
    "UsnJournalReader": ("neocortex.enumeration.ntfs.journal", "UsnJournalReader"),
    "VolumeEnumeration": (
        "neocortex.enumeration.ntfs.enumeration",
        "VolumeEnumeration",
    ),
    "VolumeHandle": ("neocortex.enumeration.ntfs.volume", "VolumeHandle"),
    "consume_changes": ("neocortex.enumeration.ntfs.journal", "consume_changes"),
    "enumerate_volume": (
        "neocortex.enumeration.ntfs.enumeration",
        "enumerate_volume",
    ),
    "normalize_volume": ("neocortex.enumeration.ntfs.volume", "normalize_volume"),
    "parse_enum_buffer": ("neocortex.enumeration.ntfs.parser", "parse_enum_buffer"),
    "parse_journal_buffer": (
        "neocortex.enumeration.ntfs.parser",
        "parse_journal_buffer",
    ),
    "query_journal_cursor": (
        "neocortex.enumeration.ntfs.enumeration",
        "query_journal_cursor",
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
