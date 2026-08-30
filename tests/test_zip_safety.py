# region [00] Contexto del módulo
# Módulo: tests/test_zip_safety.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from neocortex.platform.zip_safety import (
    ZipStructureError,
    inspect_zip_bytes,
    inspect_zip_structure,
)
# endregion [01]

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


if __name__ == "__main__":
    unittest.main()
# endregion [02]
