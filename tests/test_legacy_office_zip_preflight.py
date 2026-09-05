from __future__ import annotations

import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from neocortex.capabilities.formats.office import legacy_worker
from neocortex.platform.zip_safety import ZipStructureError


def _miscounted_ooxml() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        for index in range(1_000):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", "<worksheet/>")
    payload = bytearray(stream.getvalue())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    if eocd_offset < 0:
        raise AssertionError("fixture has no EOCD")
    struct.pack_into("<HH", payload, eocd_offset + 8, 1, 1)
    return bytes(payload)


class LegacyOfficeZipPreflightTests(unittest.TestCase):
    def test_rejects_miscounted_conversion_before_zipfile_materializes_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "converted.xlsx"
            path.write_bytes(_miscounted_ooxml())
            with patch.object(
                legacy_worker.zipfile,
                "ZipFile",
                side_effect=AssertionError("ZipFile must not be opened before preflight"),
            ):
                with self.assertRaisesRegex(ZipStructureError, "records|members"):
                    legacy_worker._ooxml_text(
                        path,
                        "xls",
                        10_000,
                    )


if __name__ == "__main__":
    unittest.main()
