"""Public immutable data models."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/models.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
# endregion [01]

# region [02] Implementación


FILE_ATTRIBUTE_DIRECTORY = 0x00000010


@dataclass(frozen=True, slots=True)
class UsnJournalInfo:
    """A point-in-time description of an NTFS USN journal."""

    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int
    maximum_size: int
    allocation_delta: int


@dataclass(frozen=True, slots=True)
class EnumerationCheckpoint:
    """Boundary required to consume journal changes after an initial scan."""

    volume: str
    journal: UsnJournalInfo
    enumeration_high_usn: int

    def journal_cursor(self) -> "JournalCursor":
        """Return the first cursor to use after this initial enumeration."""

        return JournalCursor(
            volume=self.volume,
            journal_id=self.journal.journal_id,
            next_usn=self.journal.next_usn,
        )


@dataclass(frozen=True, slots=True)
class JournalCursor:
    """Durable resume position for one volume and one journal incarnation."""

    volume: str
    journal_id: int
    next_usn: int


@dataclass(frozen=True, slots=True)
class NtfsEntry:
    """Metadata returned for one live MFT file record.

    ``file_reference_number`` and ``parent_reference_number`` are represented as
    Python integers, including 128-bit IDs returned by USN_RECORD_V3.
    """

    file_reference_number: int
    parent_reference_number: int
    name: str
    usn: int
    timestamp: datetime | None
    reason: int
    source_info: int
    security_id: int
    file_attributes: int
    record_major_version: int
    record_minor_version: int

    @property
    def is_directory(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_DIRECTORY)


@dataclass(frozen=True, slots=True)
class UsnChangeBatch:
    """One bounded journal batch and its exact resume boundaries."""

    cursor_before: JournalCursor
    cursor_after: JournalCursor
    records: tuple[NtfsEntry, ...]


# endregion [02]
