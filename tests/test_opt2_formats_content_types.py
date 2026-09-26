"""ZIP Identify regressions for Python ``ZipInfo`` compatibility details."""

from __future__ import annotations

import binascii
import struct
import zipfile
from pathlib import Path

import pytest

from neocortex.platform import content_types


_UNICODE_PATH_EXTRA = 0x7075


def _unicode_path_extra(raw_name: str, effective_name: str) -> bytes:
    raw = raw_name.encode("utf-8")
    payload = (
        b"\x01"
        + struct.pack("<I", binascii.crc32(raw) & 0xFFFFFFFF)
        + effective_name.encode("utf-8")
    )
    return struct.pack("<HH", _UNICODE_PATH_EXTRA, len(payload)) + payload


def _write_zip(
    path: Path,
    members: tuple[tuple[str, bytes, str | None], ...],
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for raw_name, payload, effective_name in members:
            info = zipfile.ZipInfo(raw_name)
            if effective_name is not None:
                info.extra = _unicode_path_extra(raw_name, effective_name)
            archive.writestr(info, payload)


def _detected_tuple(path: Path) -> tuple[str, str, str]:
    detected = content_types._detect_zip(path)
    return detected.mime, detected.canonical_extension, detected.evidence


@pytest.mark.parametrize(
    ("name", "members", "expected"),
    (
        (
            "unicode_path_introduces_ooxml",
            (
                ("[Content_Types].xml", b"<Types/>", None),
                ("ordinary.bin", b"<document/>", "word/document.xml"),
            ),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".docx",
                "zip:ooxml-word",
            ),
        ),
        (
            "unicode_path_hides_ooxml",
            (
                ("[Content_Types].xml", b"<Types/>", None),
                ("word/document.xml", b"<document/>", "ordinary.bin"),
            ),
            ("application/zip", ".zip", "magic:zip"),
        ),
        (
            "unicode_path_changes_mimetype",
            (("ordinary.bin", b"application/epub+zip", "mimetype"),),
            ("application/epub+zip", ".epub", "zip:mimetype"),
        ),
        (
            "unicode_path_introduces_apk",
            (("ordinary.bin", b"", "AndroidManifest.xml"),),
            ("application/vnd.android.package-archive", ".apk", "zip:android-manifest"),
        ),
        (
            "unicode_path_introduces_jar",
            (("ordinary.bin", b"Manifest-Version: 1.0", "META-INF/MANIFEST.MF"),),
            ("application/java-archive", ".jar", "zip:java-manifest"),
        ),
        (
            "unicode_path_nul_is_sanitized_by_zipfile",
            (
                ("[Content_Types].xml", b"<Types/>", None),
                ("ordinary.bin", b"<document/>", "word/document.xml\x00"),
            ),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".docx",
                "zip:ooxml-word",
            ),
        ),
    ),
)
def test_unicode_path_effective_names_preserve_zipfile_detection(
    tmp_path: Path,
    name: str,
    members: tuple[tuple[str, bytes, str | None], ...],
    expected: tuple[str, str, str],
) -> None:
    source = tmp_path / f"{name}.zip"
    _write_zip(source, members)

    assert _detected_tuple(source) == expected


def test_unicode_path_crc_mismatch_keeps_central_name(tmp_path: Path) -> None:
    source = tmp_path / "unicode-path-crc-mismatch.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        info = zipfile.ZipInfo("ordinary.bin")
        payload = b"\x01" + struct.pack("<I", 0) + b"word/document.xml"
        info.extra = struct.pack("<HH", _UNICODE_PATH_EXTRA, len(payload)) + payload
        archive.writestr(info, b"<document/>")

    assert _detected_tuple(source) == ("application/zip", ".zip", "magic:zip")


@pytest.mark.parametrize(
    "extra",
    (
        struct.pack("<HH", _UNICODE_PATH_EXTRA, 2) + b"\x01\x00",
        struct.pack("<HH", _UNICODE_PATH_EXTRA, 6)
        + b"\x01"
        + struct.pack("<I", binascii.crc32(b"word/document.xml") & 0xFFFFFFFF)
        + b"\xff",
    ),
)
def test_malformed_unicode_path_extra_keeps_magic_zip_abstention(
    tmp_path: Path,
    extra: bytes,
) -> None:
    source = tmp_path / "malformed-unicode-path.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        info = zipfile.ZipInfo("word/document.xml")
        info.extra = extra
        archive.writestr(info, b"<document/>")

    assert _detected_tuple(source) == ("application/zip", ".zip", "magic:zip")


def test_unsupported_extract_version_keeps_magic_zip_abstention(tmp_path: Path) -> None:
    source = tmp_path / "unsupported-extract-version.zip"
    _write_zip(
        source,
        (
            ("[Content_Types].xml", b"<Types/>", None),
            ("word/document.xml", b"<document/>", None),
        ),
    )
    payload = bytearray(source.read_bytes())
    central_offset = payload.find(b"PK\x01\x02")
    assert central_offset >= 0
    struct.pack_into("<H", payload, central_offset + 6, 99)
    source.write_bytes(payload)

    assert _detected_tuple(source) == ("application/zip", ".zip", "magic:zip")
