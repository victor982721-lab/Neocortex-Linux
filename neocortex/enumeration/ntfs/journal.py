"""Resumable, bounded-memory consumption of subsequent USN changes."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/ntfs/journal.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import Event

from .enumeration import DEFAULT_BUFFER_SIZE, MAX_BUFFER_SIZE, MIN_BUFFER_SIZE
from ..errors import JournalDiscontinuityError, VolumeAccessError
from ..models import (
    EnumerationCheckpoint,
    JournalCursor,
    UsnChangeBatch,
)
from .parser import parse_journal_buffer
from .volume import VolumeHandle, normalize_volume
# endregion [01]

# region [02] Implementación


ALL_REASONS = 0xFFFFFFFF
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181


class UsnJournalReader(Iterator[UsnChangeBatch]):
    """Continuously read journal changes after an initial checkpoint.

    Each yielded batch is bounded by ``buffer_size``. Persist
    ``batch.cursor_after`` only after the corresponding records have been
    committed by the caller. The default wait returns control at least once per
    second, so ``iter_batches(stop_event=...)`` can stop cooperatively.
    """

    def __init__(
        self,
        volume: str | Path,
        start: EnumerationCheckpoint | JournalCursor,
        *,
        reason_mask: int = ALL_REASONS,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        timeout_seconds: int = 1,
        bytes_to_wait_for: int = 1,
    ):
        display, _, _ = normalize_volume(volume)
        cursor = start.journal_cursor() if isinstance(start, EnumerationCheckpoint) else start
        cursor_display, _, _ = normalize_volume(cursor.volume)
        if display != cursor_display:
            raise ValueError(f"cursor belongs to {cursor_display}, not requested volume {display}")
        if not 0 <= reason_mask <= 0xFFFFFFFF:
            raise ValueError("reason_mask must be an unsigned 32-bit value")
        if not MIN_BUFFER_SIZE <= buffer_size <= MAX_BUFFER_SIZE:
            raise ValueError(
                f"buffer_size must be between {MIN_BUFFER_SIZE} and {MAX_BUFFER_SIZE} bytes"
            )
        if not isinstance(timeout_seconds, int) or not 0 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be an integer from 0 through 300")
        if not 0 <= bytes_to_wait_for <= MAX_BUFFER_SIZE:
            raise ValueError(f"bytes_to_wait_for must be from 0 through {MAX_BUFFER_SIZE}")
        if timeout_seconds and not bytes_to_wait_for:
            raise ValueError("a nonzero timeout requires bytes_to_wait_for > 0")

        self._volume = VolumeHandle(display)
        self._cursor = JournalCursor(display, cursor.journal_id, cursor.next_usn)
        self._reason_mask = reason_mask
        self._buffer_size = buffer_size
        self._timeout_seconds = timeout_seconds
        self._bytes_to_wait_for = bytes_to_wait_for
        self._started = False
        self._closed = False

    @property
    def cursor(self) -> JournalCursor:
        """Current in-memory position (not necessarily persisted by the caller)."""

        return self._cursor

    def _start(self) -> None:
        if self._started:
            return
        try:
            self._volume.open()
            current = self._volume.query_journal()
            if current.journal_id != self._cursor.journal_id:
                raise JournalDiscontinuityError(
                    "the saved journal ID no longer matches; initial rescan required"
                )
            if self._cursor.next_usn < current.lowest_valid_usn:
                raise JournalDiscontinuityError(
                    "the saved USN has been discarded by journal wrap; initial rescan required"
                )
            if self._cursor.next_usn > current.next_usn:
                raise JournalDiscontinuityError(
                    "the saved USN is ahead of the current journal; initial rescan required"
                )
            self._started = True
        except BaseException:
            self._volume.close()
            raise

    def __iter__(self) -> "UsnJournalReader":
        return self

    def __next__(self) -> UsnChangeBatch:
        if self._closed:
            raise StopIteration
        while True:
            batch = self.poll()
            if batch is not None:
                return batch

    def poll(self) -> UsnChangeBatch | None:
        """Perform one bounded journal read without persisting its cursor.

        ``None`` represents a normal kernel timeout.  The in-memory cursor may
        advance when a batch is returned, but durable advancement remains the
        caller's responsibility after the corresponding work commits.
        """

        return self._read_once()

    def _read_once(self) -> UsnChangeBatch | None:
        """Perform one kernel read; return ``None`` for a normal timeout."""

        self._start()
        before = self._cursor
        try:
            raw = self._volume.read_journal(
                start_usn=before.next_usn,
                journal_id=before.journal_id,
                reason_mask=self._reason_mask,
                timeout_seconds=self._timeout_seconds,
                bytes_to_wait_for=self._bytes_to_wait_for,
                buffer_size=self._buffer_size,
            )
            next_usn, records_iterator = parse_journal_buffer(raw)
            if next_usn < before.next_usn:
                raise JournalDiscontinuityError(
                    f"journal cursor moved backwards: {before.next_usn} -> {next_usn}"
                )
            records = tuple(records_iterator)
            if next_usn == before.next_usn and not records:
                return None
            after = JournalCursor(before.volume, before.journal_id, next_usn)
            self._cursor = after
            return UsnChangeBatch(before, after, records)
        except VolumeAccessError as exc:
            self.close()
            if exc.winerror in {
                ERROR_JOURNAL_DELETE_IN_PROGRESS,
                ERROR_JOURNAL_NOT_ACTIVE,
                ERROR_JOURNAL_ENTRY_DELETED,
            }:
                raise JournalDiscontinuityError(
                    f"the USN journal is unavailable or lost history: {exc}"
                ) from exc
            raise
        except BaseException:
            self.close()
            raise

    def iter_batches(self, stop_event: Event | None = None) -> Iterator[UsnChangeBatch]:
        """Yield continuously until closed or *stop_event* becomes set."""

        while not self._closed and (stop_event is None or not stop_event.is_set()):
            batch = self.poll()
            if batch is not None:
                yield batch

    def iter_until(self, target_usn: int) -> Iterator[UsnChangeBatch]:
        """Read a finite historical window, possibly advancing past its target.

        The first returned USN boundary may exceed *target_usn* because Windows
        returns journal records in bounded buffers rather than accepting an end
        USN. The resulting ``cursor`` is always the exact safe resume position.
        """

        if target_usn < self._cursor.next_usn:
            raise ValueError("target_usn precedes the reader cursor")
        while self._cursor.next_usn < target_usn:
            batch = self.poll()
            if batch is None:
                raise JournalDiscontinuityError(
                    "journal made no progress before the requested target USN"
                )
            yield batch

    def resolve_path(self, file_reference_number: int) -> str:
        """Resolve a live record ID using the reader's already-open volume."""

        self._start()
        return self._volume.resolve_path(file_reference_number)

    def close(self) -> None:
        self._closed = True
        self._volume.close()

    def __enter__(self) -> "UsnJournalReader":
        self._start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._volume.close()
        except Exception:
            pass


def consume_changes(
    volume: str | Path,
    start: EnumerationCheckpoint | JournalCursor,
    **kwargs,
) -> UsnJournalReader:
    """Create a continuous reader beginning at an exact durable cursor."""

    return UsnJournalReader(volume, start, **kwargs)


# endregion [02]
