"""Canonical filesystem-enumeration contracts and implementations.

The package keeps the portable data contracts import-light.  Platform-specific
NTFS/USN support and the optional SQLite path index are loaded only when their
public symbols are requested.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .errors import (
        CorruptBufferError as CorruptBufferError,
        InvalidVolumeError as InvalidVolumeError,
        JournalDiscontinuityError as JournalDiscontinuityError,
        NtfsUsnError as NtfsUsnError,
        UnsupportedPlatformError as UnsupportedPlatformError,
        UnsupportedRecordVersionError as UnsupportedRecordVersionError,
        VolumeAccessError as VolumeAccessError,
    )
    from .models import (
        EnumerationCheckpoint as EnumerationCheckpoint,
        JournalCursor as JournalCursor,
        NtfsEntry as NtfsEntry,
        UsnChangeBatch as UsnChangeBatch,
        UsnJournalInfo as UsnJournalInfo,
    )
    from .ntfs.enumeration import (
        VolumeEnumeration as VolumeEnumeration,
        enumerate_volume as enumerate_volume,
        query_journal_cursor as query_journal_cursor,
    )
    from .ntfs.journal import (
        ALL_REASONS as ALL_REASONS,
        UsnJournalReader as UsnJournalReader,
        consume_changes as consume_changes,
    )
    from .path_index.repository import SqlitePathIndex as SqlitePathIndex

__all__ = [  # noqa: RUF022 - mirror the compatibility manifest exactly.
    "CorruptBufferError",
    "ALL_REASONS",
    "EnumerationCheckpoint",
    "InvalidVolumeError",
    "JournalDiscontinuityError",
    "JournalCursor",
    "NtfsEntry",
    "NtfsUsnError",
    "SqlitePathIndex",
    "UnsupportedPlatformError",
    "UnsupportedRecordVersionError",
    "UsnJournalInfo",
    "UsnChangeBatch",
    "UsnJournalReader",
    "VolumeAccessError",
    "VolumeEnumeration",
    "enumerate_volume",
    "query_journal_cursor",
    "consume_changes",
]

_EXPORTS: Final = {
    "ALL_REASONS": ("neocortex.enumeration.ntfs.journal", "ALL_REASONS"),
    "CorruptBufferError": ("neocortex.enumeration.errors", "CorruptBufferError"),
    "EnumerationCheckpoint": (
        "neocortex.enumeration.models",
        "EnumerationCheckpoint",
    ),
    "InvalidVolumeError": ("neocortex.enumeration.errors", "InvalidVolumeError"),
    "JournalCursor": ("neocortex.enumeration.models", "JournalCursor"),
    "JournalDiscontinuityError": (
        "neocortex.enumeration.errors",
        "JournalDiscontinuityError",
    ),
    "NtfsEntry": ("neocortex.enumeration.models", "NtfsEntry"),
    "NtfsUsnError": ("neocortex.enumeration.errors", "NtfsUsnError"),
    "SqlitePathIndex": (
        "neocortex.enumeration.path_index.repository",
        "SqlitePathIndex",
    ),
    "UnsupportedPlatformError": (
        "neocortex.enumeration.errors",
        "UnsupportedPlatformError",
    ),
    "UnsupportedRecordVersionError": (
        "neocortex.enumeration.errors",
        "UnsupportedRecordVersionError",
    ),
    "UsnChangeBatch": ("neocortex.enumeration.models", "UsnChangeBatch"),
    "UsnJournalInfo": ("neocortex.enumeration.models", "UsnJournalInfo"),
    "UsnJournalReader": (
        "neocortex.enumeration.ntfs.journal",
        "UsnJournalReader",
    ),
    "VolumeAccessError": ("neocortex.enumeration.errors", "VolumeAccessError"),
    "VolumeEnumeration": (
        "neocortex.enumeration.ntfs.enumeration",
        "VolumeEnumeration",
    ),
    "consume_changes": ("neocortex.enumeration.ntfs.journal", "consume_changes"),
    "enumerate_volume": (
        "neocortex.enumeration.ntfs.enumeration",
        "enumerate_volume",
    ),
    "query_journal_cursor": (
        "neocortex.enumeration.ntfs.enumeration",
        "query_journal_cursor",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve one public symbol without importing unrelated implementations."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
