from __future__ import annotations

import io
import random
import struct
import zlib
from contextlib import contextmanager
from pathlib import Path

import pytest

from neocortex.enumeration import JournalCursor, NtfsEntry, UsnChangeBatch
from neocortex.deduplication import InventoryExclusionPolicy
from neocortex.runtime.control.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from neocortex.capabilities.formats.docx.layout import TextBudget, xml_text_and_layout
from neocortex.capabilities.formats.image import png as image_png
from neocortex.integrations.inventory.reconcile import (
    FILE_ATTRIBUTE_DIRECTORY,
    USN_REASON_FILE_DELETE,
    USN_REASON_RENAME_NEW_NAME,
    USN_REASON_RENAME_OLD_NAME,
    reconcile_usn_window,
)
from neocortex.platform.zip_safety import (
    LOCAL_FILE_SIGNATURE,
    RAW_DEFLATE_CHUNK_BYTES,
    ZipStructureError,
    read_raw_deflate_member,
)


# region [01] Streaming OOXML parity


_DOCUMENT_XML = b"""\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/><w:jc w:val="center"/></w:pPr>
      <w:r><w:t>Alpha</w:t><w:tab/><w:t>Beta</w:t><w:br/></w:r>
    </w:p>
    <w:tbl/>
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838" w:orient="portrait"/>
      <w:pgMar w:top="1" w:right="2" w:bottom="3" w:left="4"/>
      <w:cols w:num="2"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def test_docx_xml_refactor_preserves_text_layout_and_exact_budget() -> None:
    budget = TextBudget(12)

    text, layout = xml_text_and_layout(
        io.BytesIO(_DOCUMENT_XML), collect_layout=True, budget=budget
    )

    assert text == "Alpha\tBeta\n\n"
    assert budget.consumed == 12
    assert layout["paragraphs"] == 1
    assert layout["tables"] == 1
    assert layout["styles"] == {"Heading1": 1}
    assert layout["alignments"] == {"center": 1}
    assert layout["sections"] == [
        {
            "width": "11906",
            "height": "16838",
            "orientation": "portrait",
            "top": "1",
            "right": "2",
            "bottom": "3",
            "left": "4",
            "columns": "2",
        }
    ]

    with pytest.raises(ValueError, match=r"DOCX text exceeds 11 characters"):
        xml_text_and_layout(
            io.BytesIO(_DOCUMENT_XML),
            collect_layout=False,
            budget=TextBudget(11),
        )


def test_docx_xml_refactor_preserves_cooperative_cancellation() -> None:
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(CancellationRequested):
        xml_text_and_layout(
            io.BytesIO(_DOCUMENT_XML),
            collect_layout=True,
            budget=TextBudget(100),
            cancellation=cancellation,
        )


# endregion [01]


# region [02] Incremental PNG state-machine parity


def _png_chunk(kind: bytes, payload: bytes, *, corrupt_crc: bool = False) -> bytes:
    checksum = zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF
    if corrupt_crc:
        checksum ^= 1
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


_IHDR = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
_VALID_IHDR = _png_chunk(b"IHDR", _IHDR)
_VALID_IDAT = _png_chunk(b"IDAT", b"")
_VALID_IEND = _png_chunk(b"IEND", b"")


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (image_png.PNG_SIGNATURE + b"\x00", "truncated_chunk_header"),
        (
            image_png.PNG_SIGNATURE + struct.pack(">I", 0) + b"12!4" + b"\x00" * 4,
            "invalid_chunk_type",
        ),
        (
            image_png.PNG_SIGNATURE + struct.pack(">I", 100) + b"IHDR" + b"\x00" * 4,
            "invalid_chunk_length",
        ),
        (
            image_png.PNG_SIGNATURE + _png_chunk(b"IHDR", _IHDR, corrupt_crc=True),
            "crc32_mismatch",
        ),
        (image_png.PNG_SIGNATURE + _VALID_IDAT, "ihdr_not_first"),
        (
            image_png.PNG_SIGNATURE + _VALID_IHDR + _VALID_IHDR,
            "duplicate_ihdr",
        ),
        (
            image_png.PNG_SIGNATURE
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 1, 8, 2, 0, 0, 0)),
            "invalid_ihdr",
        ),
        (
            image_png.PNG_SIGNATURE + _VALID_IHDR + _VALID_IDAT + _png_chunk(b"IEND", b"x"),
            "invalid_iend_length",
        ),
        (
            image_png.PNG_SIGNATURE + _VALID_IHDR + _VALID_IDAT + _VALID_IEND + b"x",
            "trailing_bytes_after_iend",
        ),
        (image_png.PNG_SIGNATURE, "missing_ihdr"),
        (image_png.PNG_SIGNATURE + _VALID_IHDR, "missing_idat"),
        (
            image_png.PNG_SIGNATURE + _VALID_IHDR + _VALID_IDAT,
            "missing_iend",
        ),
    ),
)
def test_png_refactor_preserves_structural_reason_codes(
    tmp_path: Path, payload: bytes, reason: str
) -> None:
    path = tmp_path / "case.png"
    path.write_bytes(payload)

    result = image_png.probe_png_structure(path)

    assert result.status == "corrupt"
    assert result.reason_code == reason
    assert result.bytes_checked <= len(payload)


def test_png_refactor_preserves_chunk_limit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "limited.png"
    path.write_bytes(image_png.PNG_SIGNATURE + _VALID_IHDR + _VALID_IDAT + _VALID_IEND)
    monkeypatch.setattr(image_png, "MAX_PNG_CHUNKS", 1)

    result = image_png.probe_png_structure(path)

    assert result.status == "inconclusive"
    assert result.reason_code == "probe_chunk_limit"
    assert result.chunks_checked == 1
    assert (result.width, result.height) == (1, 1)


# endregion [02]


# region [03] Bounded raw-DEFLATE parity


def _write_raw_deflate_member(path: Path, payload: bytes) -> int:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(payload) + compressor.flush()
    name = b"payload.bin"
    header = struct.pack(
        "<4s5H3I2H",
        LOCAL_FILE_SIGNATURE,
        20,
        0,
        8,
        0,
        0,
        0,
        len(compressed),
        len(payload),
        len(name),
        0,
    )
    path.write_bytes(header + name + compressed)
    return len(compressed)


def test_raw_deflate_refactor_preserves_payload_evidence_and_checkpoints(
    tmp_path: Path,
) -> None:
    payload = random.Random(0).randbytes(RAW_DEFLATE_CHUNK_BYTES * 2 + 137)
    path = tmp_path / "raw-member.bin"
    compressed_size = _write_raw_deflate_member(path, payload)
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    result = read_raw_deflate_member(
        path,
        header_offset=0,
        compressed_size=compressed_size,
        upper_bound=path.stat().st_size,
        max_compressed_bytes=compressed_size,
        max_output_bytes=len(payload),
        checkpoint=checkpoint,
    )

    assert result.payload == payload
    assert result.actual_size == len(payload)
    assert result.actual_crc32 == zlib.crc32(payload) & 0xFFFFFFFF
    assert checkpoints == (compressed_size + RAW_DEFLATE_CHUNK_BYTES - 1) // RAW_DEFLATE_CHUNK_BYTES

    with pytest.raises(ZipStructureError, match="output exceeds the safety limit"):
        read_raw_deflate_member(
            path,
            header_offset=0,
            compressed_size=compressed_size,
            upper_bound=path.stat().st_size,
            max_compressed_bytes=compressed_size,
            max_output_bytes=len(payload) - 1,
        )


def test_raw_deflate_refactor_propagates_checkpoint_error_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cancelled-member.bin"
    compressed_size = _write_raw_deflate_member(path, b"content")

    with pytest.raises(CancellationRequested):
        read_raw_deflate_member(
            path,
            header_offset=0,
            compressed_size=compressed_size,
            upper_bound=path.stat().st_size,
            max_compressed_bytes=compressed_size,
            max_output_bytes=100,
            checkpoint=lambda: (_ for _ in ()).throw(
                CancellationRequested("controlled cancellation")
            ),
        )


# endregion [03]


# region [04] USN reconciliation parity with a bounded fake window


class _FakeReader:
    def __init__(self, batch: UsnChangeBatch, parent: Path) -> None:
        self._batch = batch
        self._parent = parent
        self.cursor = batch.cursor_after
        self.resolve_calls = 0

    def iter_until(self, target_usn: int):
        assert target_usn == self._batch.cursor_after.next_usn
        yield self._batch

    def resolve_path(self, file_id: int) -> str:
        assert file_id == 10
        self.resolve_calls += 1
        return str(self._parent)


class _FakeIndex:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []

    def contains_identity(self, _scan_id: int, _volume_id: int, _file_id: int) -> bool:
        return False

    def require_scan_inventory_policy_signature(
        self,
        _scan_id: int,
        expected_signature: str,
    ) -> None:
        assert expected_signature == InventoryExclusionPolicy.compile(()).signature

    def apply_reconciliation(
        self,
        scan_id: int,
        *,
        upserts,
        remove_paths,
        remove_identities,
        checkpoint,
    ) -> None:
        self.applied.append(
            {
                "scan_id": scan_id,
                "upserts": tuple(upserts),
                "remove_paths": frozenset(remove_paths),
                "remove_identities": frozenset(remove_identities),
                "checkpoint": checkpoint,
            }
        )


def _entry(file_id: int, name: str, reason: int, attributes: int = 0) -> NtfsEntry:
    return NtfsEntry(file_id, 10, name, file_id, None, reason, 0, 0, attributes, 3, 0)


def test_reconcile_refactor_discards_unsafe_batch_without_advancing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = tmp_path / "created.bin"
    created.write_bytes(b"created")
    start = JournalCursor("C:", 7, 100)
    target = JournalCursor("C:", 7, 104)
    batch = UsnChangeBatch(
        start,
        target,
        (
            _entry(1, created.name, 0),
            _entry(2, "deleted.bin", USN_REASON_FILE_DELETE),
            _entry(
                3,
                "old-folder",
                USN_REASON_RENAME_OLD_NAME,
                FILE_ATTRIBUTE_DIRECTORY,
            ),
            _entry(
                3,
                "new-folder",
                USN_REASON_RENAME_NEW_NAME,
                FILE_ATTRIBUTE_DIRECTORY,
            ),
        ),
    )
    reader = _FakeReader(batch, tmp_path)

    @contextmanager
    def fake_consume_changes(*_args, **_kwargs):
        yield reader

    monkeypatch.setattr("neocortex.integrations.inventory.reconcile.consume_changes", fake_consume_changes)
    index = _FakeIndex()

    result = reconcile_usn_window(
        index, 5, tmp_path, start, target, persist_checkpoint=True, excluded_paths=()
    )

    assert result.cursor == start
    assert result.records_seen == 4
    assert result.files_upserted == 0
    assert result.files_removed == 0
    assert result.requires_rescan is True
    assert reader.resolve_calls == 1
    assert index.applied == []


# endregion [04]
