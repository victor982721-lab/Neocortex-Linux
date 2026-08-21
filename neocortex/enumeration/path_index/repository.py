"""Optional bounded-memory, SQLite-backed parent/name index."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/path_index/repository.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

from ..models import EnumerationCheckpoint, JournalCursor, NtfsEntry, UsnChangeBatch
from .schema import (
    SCHEMA_VERSION as SCHEMA_VERSION,
    connect_existing_path_index_database,
    configure_path_index_connection,
    initialize_path_index_schema,
)
# endregion [01]

# region [02] Implementación


USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000


def _id_blob(value: int) -> bytes:
    if value < 0 or value.bit_length() > 128:
        raise ValueError("file reference numbers must be unsigned 128-bit integers")
    return value.to_bytes(16, "little")


class SqlitePathIndex:
    """Persist FRN relationships without retaining the full MFT in RAM."""

    def __init__(self, database: str | Path, *, cache_size: int = 8192):
        if cache_size < 0:
            raise ValueError("cache_size cannot be negative")
        self.path = Path(database)
        self._cache_size = cache_size
        self._cache: OrderedDict[int, tuple[int, str]] = OrderedDict()
        initialize_path_index_schema(self.path)
        self._connection = connect_existing_path_index_database(self.path)
        try:
            configure_path_index_connection(self._connection)
        except BaseException:
            self._connection.close()
            raise

    def ingest(self, entries: Iterable[NtfsEntry], *, batch_size: int = 10_000) -> int:
        """Upsert entries in bounded transactions and return the processed count."""

        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        count = 0
        batch: list[tuple[bytes, bytes, str, int]] = []
        sql = """
            INSERT INTO nodes(frn, parent_frn, name, file_attributes)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(frn) DO UPDATE SET
                parent_frn=excluded.parent_frn,
                name=excluded.name,
                file_attributes=excluded.file_attributes
        """
        for entry in entries:
            batch.append(
                (
                    _id_blob(entry.file_reference_number),
                    _id_blob(entry.parent_reference_number),
                    entry.name,
                    entry.file_attributes,
                )
            )
            if len(batch) >= batch_size:
                with self._connection:
                    self._connection.executemany(sql, batch)
                count += len(batch)
                batch.clear()
        if batch:
            with self._connection:
                self._connection.executemany(sql, batch)
            count += len(batch)
        self._cache.clear()
        return count

    def bind_checkpoint(self, checkpoint: EnumerationCheckpoint | JournalCursor) -> None:
        """Persist the cursor after a completed initial index transaction."""

        cursor = (
            checkpoint.journal_cursor()
            if isinstance(checkpoint, EnumerationCheckpoint)
            else checkpoint
        )
        with self._connection:
            self._write_cursor(cursor)

    @property
    def journal_cursor(self) -> JournalCursor | None:
        rows = dict(
            self._connection.execute(
                "SELECT key, value FROM metadata WHERE key IN "
                "('journal_volume', 'journal_id', 'journal_next_usn')"
            )
        )
        if len(rows) != 3:
            return None
        return JournalCursor(
            volume=rows["journal_volume"],
            journal_id=int(rows["journal_id"]),
            next_usn=int(rows["journal_next_usn"]),
        )

    def _write_cursor(self, cursor: JournalCursor) -> None:
        self._connection.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                ("journal_volume", cursor.volume),
                ("journal_id", str(cursor.journal_id)),
                ("journal_next_usn", str(cursor.next_usn)),
            ),
        )

    def apply_change_batch(self, batch: UsnChangeBatch) -> int:
        """Apply metadata changes and advance the cursor in one transaction."""

        saved = self.journal_cursor
        if saved is not None and saved != batch.cursor_before:
            raise RuntimeError(
                f"batch starts at {batch.cursor_before}, but index cursor is {saved}"
            )
        upsert = """
            INSERT INTO nodes(frn, parent_frn, name, file_attributes)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(frn) DO UPDATE SET
                parent_frn=excluded.parent_frn,
                name=excluded.name,
                file_attributes=excluded.file_attributes
        """
        affected = 0
        with self._connection:
            for record in batch.records:
                if record.reason & USN_REASON_FILE_DELETE:
                    self._connection.execute(
                        "DELETE FROM nodes WHERE frn=?",
                        (_id_blob(record.file_reference_number),),
                    )
                    affected += 1
                elif (
                    record.reason & USN_REASON_RENAME_OLD_NAME
                    and not record.reason & USN_REASON_RENAME_NEW_NAME
                ):
                    # The following NEW_NAME record contains the authoritative
                    # parent and name. Keeping the old row prevents a transient
                    # orphan if a batch boundary falls between the pair.
                    continue
                else:
                    self._connection.execute(
                        upsert,
                        (
                            _id_blob(record.file_reference_number),
                            _id_blob(record.parent_reference_number),
                            record.name,
                            record.file_attributes,
                        ),
                    )
                    affected += 1
            self._write_cursor(batch.cursor_after)
        self._cache.clear()
        return affected

    def _node(self, frn: int) -> tuple[int, str] | None:
        cached = self._cache.get(frn)
        if cached is not None:
            self._cache.move_to_end(frn)
            return cached
        row = self._connection.execute(
            "SELECT parent_frn, name FROM nodes WHERE frn=?", (_id_blob(frn),)
        ).fetchone()
        if row is None:
            return None
        value = (int.from_bytes(row[0], "little"), row[1])
        if self._cache_size:
            self._cache[frn] = value
            self._cache.move_to_end(frn)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return value

    def relative_path(self, frn: int) -> PureWindowsPath | None:
        """Resolve a path relative to the volume, or ``None`` for an orphan."""

        components: list[str] = []
        seen: set[int] = set()
        current = frn
        while True:
            if current in seen:
                raise RuntimeError(f"cycle detected while resolving FRN {frn}")
            seen.add(current)
            node = self._node(current)
            if node is None:
                return None
            parent, name = node
            if parent == current:
                break
            components.append(name)
            current = parent
        return PureWindowsPath(*reversed(components))

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqlitePathIndex":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# endregion [02]
