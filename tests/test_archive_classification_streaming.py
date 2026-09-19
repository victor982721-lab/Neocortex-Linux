"""Classification verifies all ZIP bytes without retaining ordinary payloads."""

from __future__ import annotations

import io
import tracemalloc
import warnings
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import units


def test_ordinary_member_classification_memory_is_independent_of_member_size(tmp_path: Path) -> None:
    source = tmp_path / "large.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("ordinary.bin", b"payload!" * (1024 * 1024))
    tracemalloc.start()
    try:
        result = units.classify_archive(source)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (result.status, result.integrity_verified) == ("storage", True)
    assert peak < 2 * 1024 * 1024


def test_discarded_member_still_verifies_final_crc_and_total_budget(tmp_path: Path) -> None:
    source = tmp_path / "bounded.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("first.bin", b"a" * 131_072)
        archive.writestr("second.bin", b"b" * 131_072)
    bounded = units.classify_archive(source, max_total_bytes=200_000)
    assert (bounded.status, bounded.integrity_verified) == ("budget", False)
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("second.bin")
    payload_end = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + info.compress_size
    with source.open("r+b") as stream:
        stream.seek(payload_end - 1)
        stream.write(b"X")
    corrupted = units.classify_archive(source)
    assert (corrupted.status, corrupted.integrity_verified) == ("corrupt", False)
    assert "CRC" in str(corrupted.detail)


@pytest.mark.parametrize("xml,expected", ((b"<document/>", "validated"), (b"<document>", "partial")))
def test_streaming_keeps_package_markers_for_xml_validation(xml: bytes, expected: str) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/media/image.bin", b"ordinary binary payload")
    result = units.classify_archive_bytes(payload.getvalue())
    assert result.status == expected
    assert result.kind == "docx"
    assert result.preserve_as_unit


def test_duplicate_member_accounting_is_linear_and_preserves_ordinal_names(monkeypatch) -> None:
    comparisons = 0

    class MeasuredName(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    payload = io.BytesIO()
    count = 700
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(payload, "w") as archive:
            for index in range(count):
                archive.writestr(f"member-{index}.txt", "")
            archive.writestr("member-0.txt", "")
    original = zipfile.ZipFile.infolist

    def measured_infos(archive):
        infos = original(archive)
        for info in infos:
            info.filename = MeasuredName(info.filename)
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", measured_infos)
    result = units.classify_archive_bytes(payload.getvalue())
    assert result.status == "storage" and result.integrity_verified
    assert result.evidence == ("duplicate_name:member-0.txt", "no_functional_package_markers")
    assert len(result.member_names) == count + 1
    assert comparisons < count * 40
