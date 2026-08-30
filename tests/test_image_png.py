from __future__ import annotations

# region [01] PNG fixture construction

import struct
import zlib
from pathlib import Path

from neocortex.capabilities.formats.image.errors import ImageFailure, refine_image_failure
from neocortex.capabilities.formats.image.png import PNG_SIGNATURE, probe_png_structure


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)
    )


def _minimal_png(path: Path) -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00")
    path.write_bytes(
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )


# endregion [01]


# region [02] Structural disposition


def test_png_probe_accepts_valid_structure(tmp_path) -> None:
    path = tmp_path / "valid.png"
    _minimal_png(path)

    result = probe_png_structure(path)

    assert result.status == "valid"
    original = ImageFailure(
        "UnidentifiedImageError",
        "decoder could not identify image",
        "decode",
        False,
        "manual_review",
    )
    assert refine_image_failure(path, original) is original


def test_png_crc_corruption_is_deletion_candidate_only_after_probe(tmp_path) -> None:
    path = tmp_path / "corrupt.png"
    _minimal_png(path)
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)
    original = ImageFailure(
        "UnidentifiedImageError",
        "decoder could not identify image",
        "decode",
        False,
        "manual_review",
    )

    result = probe_png_structure(path)
    refined = refine_image_failure(path, original)

    assert result.status == "corrupt"
    assert result.reason_code == "crc32_mismatch"
    assert refined.error_type == "PngStructureCorrupt"
    assert refined.disposition == "deletion_candidate"
    assert refined.retryable is False
    assert refined.provenance == "png-structure-crc32-v1"


def test_non_png_error_stays_manual_review(tmp_path) -> None:
    path = tmp_path / "named.png"
    path.write_bytes(b"not a png")
    original = ImageFailure(
        "OSError",
        "decode failed",
        "decode",
        False,
        "manual_review",
    )

    assert refine_image_failure(path, original) is original


# endregion [02]
