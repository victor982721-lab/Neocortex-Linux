# region [00] Contexto del módulo
# Módulo: tests/test_ntfs_usn.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from neocortex.enumeration import (
    CorruptBufferError,
    JournalCursor,
    NtfsEntry,
    SqlitePathIndex,
    UsnChangeBatch,
)
from neocortex.enumeration.ntfs.parser import parse_enum_buffer, parse_journal_buffer
from neocortex.enumeration.ntfs.volume import normalize_volume
# endregion [01]

# region [02] Implementación


def v2_record(
    file_ref: int = 42,
    parent_ref: int = 5,
    name: str = "foto.jpg",
    attributes: int = 0x20,
) -> bytes:
    encoded = name.encode("utf-16-le")
    length = 60 + len(encoded)
    aligned = (length + 7) & ~7
    record = bytearray(aligned)
    struct.pack_into(
        "<IHHQQqqIIIIHH",
        record,
        0,
        aligned,
        2,
        0,
        file_ref,
        parent_ref,
        123,
        132_537_600_000_000_000,
        0x100,
        0,
        7,
        attributes,
        len(encoded),
        60,
    )
    record[60 : 60 + len(encoded)] = encoded
    return bytes(record)


def v3_record(file_ref: int, parent_ref: int, name: str) -> bytes:
    encoded = name.encode("utf-16-le")
    length = 76 + len(encoded)
    aligned = (length + 7) & ~7
    record = bytearray(aligned)
    struct.pack_into("<IHH", record, 0, aligned, 3, 0)
    record[8:24] = file_ref.to_bytes(16, "little")
    record[24:40] = parent_ref.to_bytes(16, "little")
    struct.pack_into("<qqIIIIHH", record, 40, 321, 0, 0x200, 0, 3, 0x10, len(encoded), 76)
    record[76 : 76 + len(encoded)] = encoded
    return bytes(record)


class ParserTests(unittest.TestCase):
    def test_parses_v2_record(self) -> None:
        data = struct.pack("<Q", 100) + v2_record()
        next_frn, iterator = parse_enum_buffer(data)
        entry = next(iterator)
        self.assertEqual(next_frn, 100)
        self.assertEqual(entry.file_reference_number, 42)
        self.assertEqual(entry.parent_reference_number, 5)
        self.assertEqual(entry.name, "foto.jpg")
        self.assertEqual(entry.timestamp, datetime(2020, 12, 30, tzinfo=UTC))

    def test_rejects_truncated_record(self) -> None:
        data = struct.pack("<Q", 100) + v2_record()[:-1]
        _, iterator = parse_enum_buffer(data)
        with self.assertRaises(CorruptBufferError):
            list(iterator)

    def test_parses_v3_128_bit_ids(self) -> None:
        file_ref = (1 << 100) + 25
        parent_ref = (1 << 96) + 5
        data = struct.pack("<Q", 200) + v3_record(file_ref, parent_ref, "directorio")
        _, iterator = parse_enum_buffer(data)
        entry = next(iterator)
        self.assertEqual(entry.file_reference_number, file_ref)
        self.assertEqual(entry.parent_reference_number, parent_ref)
        self.assertTrue(entry.is_directory)
        self.assertIsNone(entry.timestamp)

    def test_parses_journal_cursor_and_records(self) -> None:
        data = struct.pack("<q", 9876) + v2_record(name="nuevo.txt")
        next_usn, iterator = parse_journal_buffer(data)
        self.assertEqual(next_usn, 9876)
        self.assertEqual([entry.name for entry in iterator], ["nuevo.txt"])

    def test_normalizes_drive_designators(self) -> None:
        self.assertEqual(normalize_volume("c"), ("C:", r"\\.\C:", "C:\\"))
        self.assertEqual(normalize_volume(r"\\.\d:"), ("D:", r"\\.\D:", "D:\\"))


class PathIndexTests(unittest.TestCase):
    @staticmethod
    def entry(frn: int, parent: int, name: str) -> NtfsEntry:
        return NtfsEntry(frn, parent, name, 0, None, 0, 0, 0, 0, 2, 0)

    def test_resolves_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paths.db"
            with SqlitePathIndex(database, cache_size=2) as index:
                count = index.ingest(
                    [
                        self.entry(5, 5, "."),
                        self.entry(10, 5, "Pictures"),
                        self.entry(11, 10, "image.jpg"),
                    ],
                    batch_size=2,
                )
                self.assertEqual(count, 3)
                self.assertEqual(str(index.relative_path(11)), r"Pictures\image.jpg")
                self.assertIsNone(index.relative_path(999))

    def test_applies_change_and_cursor_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "changes.db"
            before = JournalCursor("C:", 77, 100)
            after = JournalCursor("C:", 77, 200)
            changed = self.entry(11, 10, "renamed.jpg")
            changed = NtfsEntry(
                changed.file_reference_number,
                changed.parent_reference_number,
                changed.name,
                150,
                None,
                0x2000,
                0,
                0,
                0,
                2,
                0,
            )
            with SqlitePathIndex(database) as index:
                index.ingest([self.entry(5, 5, "."), self.entry(10, 5, "Pictures")])
                index.bind_checkpoint(before)
                affected = index.apply_change_batch(UsnChangeBatch(before, after, (changed,)))
                self.assertEqual(affected, 1)
                self.assertEqual(index.journal_cursor, after)
                self.assertEqual(str(index.relative_path(11)), r"Pictures\renamed.jpg")


if __name__ == "__main__":
    unittest.main()
# endregion [02]
