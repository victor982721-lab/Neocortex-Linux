"""Small structural binary headers generated only for isolated tests."""

from __future__ import annotations

import struct


def minimal_pe_image(*, dll: bool = False) -> bytes:
    """A coherent bounded PE32/COFF header, not the weak two-byte MZ marker."""
    image = bytearray(400)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 64)
    image[64:68] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 68, 0x14C, 1, 0, 0, 0, 0xE0,
                     0x2002 if dll else 0x0002)
    struct.pack_into("<H", image, 88, 0x10B)
    return bytes(image)
