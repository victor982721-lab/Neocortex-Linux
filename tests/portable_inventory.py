"""Test-only metadata for legacy fixtures that still pass a cursor-shaped value.

Production Linux inventory no longer exposes platform journal cursors.  These
fixtures only need a stable object for old state-construction call sites; the
portable writer deliberately discards it.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortableInventoryCursor:
    volume: str
    journal_id: int
    next_usn: int
