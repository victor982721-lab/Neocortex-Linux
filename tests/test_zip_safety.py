# region [00] Contexto del módulo
# Módulo: tests/test_zip_safety.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from neocortex.platform.zip_safety import (
    ZipStructureError,
    inspect_zip_bytes,
    inspect_zip_stream,
    inspect_zip_structure,
)
# endregion [01]


_EOCD = struct.Struct("<4s4H2IH")
_ZIP64_EOCD = struct.Struct("<4sQ2H2I4Q")
_ZIP64_LOCATOR = struct.Struct("<4sIQI")


def _eocd_offset(payload: bytes | bytearray) -> int:
    offset = payload.rfind(b"PK\x05\x06")
    if offset < 0:
        raise AssertionError("fixture has no EOCD")
    return offset


def _archive_bytes(member_count: int) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for index in range(member_count):
            archive.writestr(f"member-{index:04d}.txt", f"payload-{index}")
    return stream.getvalue()


def _with_eocd_counts(payload: bytes, members: int) -> bytes:
    patched = bytearray(payload)
    struct.pack_into("<HH", patched, _eocd_offset(patched) + 8, members, members)
    return bytes(patched)


def _with_zip64_end_records(payload: bytes) -> bytes:
    eocd_offset = _eocd_offset(payload)
    eocd = _EOCD.unpack_from(payload, eocd_offset)
    central_size = int(eocd[5])
    central_offset = int(eocd[6])
    members = int(eocd[4])
    zip64_offset = eocd_offset
    zip64 = _ZIP64_EOCD.pack(
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        members,
        members,
        central_size,
        central_offset,
    )
    locator = _ZIP64_LOCATOR.pack(b"PK\x06\x07", 0, zip64_offset, 1)
    replacement = _EOCD.pack(
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )
    return payload[:eocd_offset] + zip64 + locator + replacement


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)

# region [02] Implementación


class ZipSafetyTests(unittest.TestCase):
    def test_reads_normal_archive_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("one.txt", "one")
                archive.writestr("two.txt", "two")

            structure = inspect_zip_structure(path, max_members=4)

            self.assertEqual(structure.members, 2)
            self.assertGreater(structure.central_directory_bytes, 0)
            self.assertFalse(structure.zip64)

    def test_rejects_member_count_before_loading_central_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "many.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("one.txt", "one")
                archive.writestr("two.txt", "two")

            with self.assertRaisesRegex(ZipStructureError, "members"):
                inspect_zip_structure(path, max_members=1)

    def test_preflights_bounded_nested_zip_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("inside.txt", "nested evidence")
                archive.writestr("second.txt", "more evidence")

            structure = inspect_zip_bytes(path.read_bytes(), max_members=2)

            self.assertEqual(structure.members, 2)
            with self.assertRaisesRegex(ZipStructureError, "members"):
                inspect_zip_bytes(path.read_bytes(), max_members=1)

    def test_rejects_declared_oversized_central_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "oversized.zip"
            eocd = struct.pack(
                "<4s4H2IH",
                b"PK\x05\x06",
                0,
                0,
                0,
                0,
                1024,
                0,
                0,
            )
            path.write_bytes(eocd)

            with self.assertRaisesRegex(ZipStructureError, "safety limit"):
                inspect_zip_structure(
                    path,
                    max_members=10,
                    max_central_directory_bytes=128,
                )

    def test_counts_real_records_before_accepting_a_manipulated_member_count(self) -> None:
        payload = _archive_bytes(1_000)
        source = _TrackingBytesIO(_with_eocd_counts(payload, 1))

        with self.assertRaisesRegex(ZipStructureError, "records|members"):
            inspect_zip_stream(source, len(payload), max_members=1_000)

        # The parser stops at the second central record and does not materialize
        # a ZipFile directory for the remaining 998 records.
        self.assertLess(source.read_calls, 10)

    def test_rejects_declared_member_count_smaller_or_larger_than_real_records(self) -> None:
        payload = _archive_bytes(3)
        for declared in (1, 10):
            with self.subTest(declared=declared):
                with self.assertRaisesRegex(ZipStructureError, "member|records"):
                    inspect_zip_bytes(_with_eocd_counts(payload, declared), max_members=10)

    def test_rejects_truncated_central_directory_and_inconsistent_variable_lengths(self) -> None:
        payload = _archive_bytes(2)
        eocd_offset = _eocd_offset(payload)
        eocd = _EOCD.unpack_from(payload, eocd_offset)

        truncated = bytearray(payload)
        struct.pack_into("<I", truncated, eocd_offset + 12, int(eocd[5]) - 1)
        with self.assertRaisesRegex(ZipStructureError, "central directory"):
            inspect_zip_bytes(bytes(truncated), max_members=2)

        malformed_lengths = bytearray(payload)
        struct.pack_into("<H", malformed_lengths, int(eocd[6]) + 28, 0xFFFF)
        with self.assertRaisesRegex(ZipStructureError, "record|boundary"):
            inspect_zip_bytes(bytes(malformed_lengths), max_members=2)

    def test_rejects_central_record_with_out_of_range_local_offset(self) -> None:
        payload = bytearray(_archive_bytes(1))
        eocd_offset = _eocd_offset(payload)
        eocd = _EOCD.unpack_from(payload, eocd_offset)
        struct.pack_into("<I", payload, int(eocd[6]) + 42, len(payload) + 1)

        with self.assertRaisesRegex(ZipStructureError, "local header|member data"):
            inspect_zip_bytes(bytes(payload), max_members=1)

    def test_accepts_exact_member_limit_and_rejects_one_over(self) -> None:
        payload = _archive_bytes(2)
        structure = inspect_zip_bytes(payload, max_members=2)
        self.assertEqual(structure.members, 2)
        self.assertEqual(
            inspect_zip_bytes(
                payload,
                max_members=2,
                max_central_directory_bytes=structure.central_directory_bytes,
            ).central_directory_bytes,
            structure.central_directory_bytes,
        )
        with self.assertRaisesRegex(ZipStructureError, "safety limit"):
            inspect_zip_bytes(
                payload,
                max_members=2,
                max_central_directory_bytes=structure.central_directory_bytes - 1,
            )
        with self.assertRaisesRegex(ZipStructureError, "members"):
            inspect_zip_bytes(payload, max_members=1)

    def test_validates_zip64_end_records(self) -> None:
        payload = _with_zip64_end_records(_archive_bytes(1))
        structure = inspect_zip_bytes(payload, max_members=1)
        self.assertTrue(structure.zip64)
        self.assertEqual(structure.members, 1)

    def test_validates_zip64_with_prepended_data(self) -> None:
        payload = b"self-extracting stub" + _with_zip64_end_records(_archive_bytes(1))
        structure = inspect_zip_bytes(payload, max_members=1)
        self.assertTrue(structure.zip64)
        self.assertEqual(structure.members, 1)

    def test_preflights_nested_zip_records_independently(self) -> None:
        inner = _archive_bytes(2)
        outer_stream = io.BytesIO()
        with zipfile.ZipFile(outer_stream, "w") as archive:
            archive.writestr("nested.zip", inner)
        outer = inspect_zip_bytes(outer_stream.getvalue(), max_members=1)
        self.assertEqual(outer.members, 1)
        with self.assertRaisesRegex(ZipStructureError, "members"):
            inspect_zip_bytes(inner, max_members=1)


if __name__ == "__main__":
    unittest.main()
# endregion [02]
