"""Streaming initial enumeration of an NTFS volume."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/ntfs/enumeration.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..errors import CorruptBufferError, JournalDiscontinuityError
from ..models import EnumerationCheckpoint, JournalCursor, NtfsEntry
from .parser import parse_enum_buffer
from .volume import VolumeHandle
# endregion [01]

# region [02] Implementación


DEFAULT_BUFFER_SIZE = 1024 * 1024
MIN_BUFFER_SIZE = 4096
MAX_BUFFER_SIZE = 64 * 1024 * 1024


class VolumeEnumeration(Iterator[NtfsEntry]):
    """A bounded-memory iterator over live MFT records.

    Prefer use as a context manager so a partially consumed iterator releases
    its volume handle immediately.  Exhaustion also closes it automatically.
    """

    def __init__(self, volume: str | Path, *, buffer_size: int = DEFAULT_BUFFER_SIZE):
        if not MIN_BUFFER_SIZE <= buffer_size <= MAX_BUFFER_SIZE:
            raise ValueError(
                f"buffer_size must be between {MIN_BUFFER_SIZE} and {MAX_BUFFER_SIZE} bytes"
            )
        self._volume = VolumeHandle(volume)
        self._buffer_size = buffer_size
        self._cursor = 0
        self._entries: Iterator[NtfsEntry] = iter(())
        self._started = False
        self._finished = False
        self._checkpoint: EnumerationCheckpoint | None = None

    @property
    def checkpoint(self) -> EnumerationCheckpoint:
        """Snapshot boundary; opens the volume on first access."""

        self._start()
        assert self._checkpoint is not None
        return self._checkpoint

    def _start(self) -> None:
        if self._started:
            return
        try:
            self._volume.open()
            journal = self._volume.query_journal()
        except BaseException:
            self._volume.close()
            raise
        self._checkpoint = EnumerationCheckpoint(
            volume=self._volume.display,
            journal=journal,
            enumeration_high_usn=journal.next_usn,
        )
        self._started = True

    def __iter__(self) -> "VolumeEnumeration":
        return self

    def __next__(self) -> NtfsEntry:
        if self._finished:
            raise StopIteration
        try:
            self._start()
            while True:
                try:
                    return next(self._entries)
                except StopIteration:
                    assert self._checkpoint is not None
                    raw = self._volume.enum_mft(
                        self._cursor,
                        0,
                        self._checkpoint.enumeration_high_usn,
                        self._buffer_size,
                    )
                    if raw is None:
                        self._finish_and_verify()
                        raise StopIteration from None
                    next_cursor, entries = parse_enum_buffer(raw)
                    if next_cursor <= self._cursor:
                        raise CorruptBufferError(
                            f"MFT enumeration cursor did not advance: "
                            f"{self._cursor} -> {next_cursor}"
                        ) from None
                    self._cursor = next_cursor
                    self._entries = entries
        except StopIteration:
            raise
        except BaseException:
            self.close()
            raise

    def _finish_and_verify(self) -> None:
        assert self._checkpoint is not None
        try:
            current = self._volume.query_journal()
            initial = self._checkpoint.journal
            if current.journal_id != initial.journal_id:
                raise JournalDiscontinuityError(
                    "the USN journal ID changed during initial enumeration; rescan required"
                )
            if initial.next_usn < current.lowest_valid_usn:
                raise JournalDiscontinuityError(
                    "the journal wrapped past the initial checkpoint; rescan required"
                )
        finally:
            self._finished = True
            self._volume.close()

    def close(self) -> None:
        self._finished = True
        self._volume.close()

    def __enter__(self) -> "VolumeEnumeration":
        self._start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort safeguard for callers that stop iteration without using
        # the recommended context-manager form.
        try:
            self._volume.close()
        except Exception:
            pass


def enumerate_volume(
    volume: str | Path, *, buffer_size: int = DEFAULT_BUFFER_SIZE
) -> VolumeEnumeration:
    """Create a streaming initial enumeration for an NTFS drive.

    Example::

        with enumerate_volume("C:") as scan:
            checkpoint = scan.checkpoint
            for entry in scan:
                consume(entry)
    """

    return VolumeEnumeration(volume, buffer_size=buffer_size)


def query_journal_cursor(volume: str | Path) -> JournalCursor:
    """Capture the current journal boundary without enumerating the MFT.

    This is useful immediately before a scoped directory inventory: subsequent
    journal consumption can close the race window created while that inventory
    was running.
    """

    with VolumeHandle(volume) as handle:
        journal = handle.query_journal()
        return JournalCursor(handle.display, journal.journal_id, journal.next_usn)


# endregion [02]
