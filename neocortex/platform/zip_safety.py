"""Bounded ZIP structure preflight before Python materializes member metadata."""

from __future__ import annotations

import io
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from . import preserve_legacy_module as _preserve_legacy_module


# region [01] ZIP records and explicit bounds

EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
EOCD_FIXED_BYTES = 22
MAX_ZIP_COMMENT_BYTES = 65_535
ZIP64_LOCATOR_BYTES = 20
ZIP64_EOCD_MIN_BYTES = 56
DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES = 32 * 1024 * 1024
LOCAL_FILE_SIGNATURE = b"PK\x03\x04"
RAW_DEFLATE_CHUNK_BYTES = 64 * 1024

_EOCD = struct.Struct("<4s4H2IH")
_ZIP64_LOCATOR = struct.Struct("<4sIQI")
_ZIP64_EOCD = struct.Struct("<4sQ2H2I4Q")
_LOCAL_FILE = struct.Struct("<4s5H3I2H")


class ZipStructureError(ValueError):
    """ZIP metadata is absent, inconsistent or exceeds an explicit bound."""


@dataclass(frozen=True, slots=True)
class ZipStructure:
    members: int
    central_directory_bytes: int
    central_directory_offset: int
    zip64: bool


@dataclass(frozen=True, slots=True)
class RawDeflateMember:
    """One bounded raw-DEFLATE recovery result with non-cryptographic evidence."""

    payload: bytes
    actual_size: int
    actual_crc32: int


# endregion [01]


# region [02] Bounded end-record discovery


def _read_exact(source, offset: int, length: int) -> bytes:
    source.seek(offset)
    payload = source.read(length)
    if len(payload) != length:
        raise ZipStructureError("truncated ZIP metadata")
    return payload


def _find_eocd(source, file_size: int) -> tuple[int, tuple[int, ...]]:
    tail_size = min(file_size, EOCD_FIXED_BYTES + MAX_ZIP_COMMENT_BYTES)
    tail_offset = file_size - tail_size
    tail = _read_exact(source, tail_offset, tail_size)
    search_end = len(tail)
    while True:
        index = tail.rfind(EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            raise ZipStructureError("ZIP end-of-central-directory record not found")
        if index + EOCD_FIXED_BYTES <= len(tail):
            values = _EOCD.unpack_from(tail, index)
            comment_length = int(values[-1])
            if index + EOCD_FIXED_BYTES + comment_length <= len(tail):
                return tail_offset + index, tuple(int(value) for value in values[1:])
        search_end = index


def _zip64_values(source, eocd_offset: int, file_size: int) -> tuple[int, int, int]:
    locator_offset = eocd_offset - ZIP64_LOCATOR_BYTES
    if locator_offset < 0:
        raise ZipStructureError("ZIP64 locator is missing")
    locator = _ZIP64_LOCATOR.unpack(_read_exact(source, locator_offset, ZIP64_LOCATOR_BYTES))
    signature, disk_number, record_offset, disk_count = locator
    if signature != ZIP64_LOCATOR_SIGNATURE:
        raise ZipStructureError("ZIP64 locator is missing")
    if disk_number != 0 or disk_count != 1:
        raise ZipStructureError("multi-disk ZIP containers are not supported")
    if record_offset < 0 or record_offset + ZIP64_EOCD_MIN_BYTES > file_size:
        raise ZipStructureError("ZIP64 end record points outside the file")
    record = _ZIP64_EOCD.unpack(_read_exact(source, int(record_offset), ZIP64_EOCD_MIN_BYTES))
    (
        signature,
        record_size,
        _made_by,
        _needed,
        disk_number,
        central_disk,
        disk_members,
        members,
        central_size,
        central_offset,
    ) = record
    if signature != ZIP64_EOCD_SIGNATURE or record_size < 44:
        raise ZipStructureError("invalid ZIP64 end record")
    if disk_number != 0 or central_disk != 0 or disk_members != members:
        raise ZipStructureError("multi-disk ZIP containers are not supported")
    return int(members), int(central_size), int(central_offset)


# endregion [02]


# region [03] Public validation


def _inspect_zip_source(
    source: BinaryIO,
    file_size: int,
    *,
    max_members: int,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ZipStructure:
    """Validate one seekable ZIP source without materializing its directory."""

    if max_members < 1:
        raise ValueError("max_members must be positive")
    if max_central_directory_bytes < 1:
        raise ValueError("max_central_directory_bytes must be positive")
    if file_size < EOCD_FIXED_BYTES:
        raise ZipStructureError("file is too small to contain a ZIP end record")
    eocd_offset, values = _find_eocd(source, file_size)
    (
        disk_number,
        central_disk,
        disk_members,
        members,
        central_size,
        central_offset,
        _comment_length,
    ) = values
    is_zip64 = (
        disk_members == 0xFFFF
        or members == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    )
    if is_zip64:
        members, central_size, central_offset = _zip64_values(source, eocd_offset, file_size)
    elif disk_number != 0 or central_disk != 0 or disk_members != members:
        raise ZipStructureError("multi-disk ZIP containers are not supported")

    if members > max_members:
        raise ZipStructureError(f"ZIP contains {members} members; limit is {max_members}")
    if central_size > max_central_directory_bytes:
        raise ZipStructureError(
            "ZIP central directory exceeds the safety limit: "
            f"{central_size} > {max_central_directory_bytes} bytes"
        )
    if central_offset > file_size or central_size > file_size - central_offset:
        raise ZipStructureError("ZIP central directory points outside the file")
    return ZipStructure(members, central_size, central_offset, is_zip64)


def inspect_zip_stream(
    source: BinaryIO,
    file_size: int,
    *,
    max_members: int,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ZipStructure:
    """Preflight one already-open seekable ZIP stream and restore its position."""

    if file_size < 0:
        raise ValueError("file_size cannot be negative")
    position = source.tell()
    try:
        return _inspect_zip_source(
            source,
            file_size,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )
    finally:
        source.seek(position)


def inspect_zip_structure(
    path: str | Path,
    *,
    max_members: int,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ZipStructure:
    """Read only bounded end metadata and reject an unsafe central directory."""

    file_size = os.path.getsize(path)
    with open(path, "rb", buffering=0) as source:
        return inspect_zip_stream(
            source,
            file_size,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )


def inspect_zip_bytes(
    payload: bytes | bytearray | memoryview,
    *,
    max_members: int,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ZipStructure:
    """Preflight an already-bounded in-memory ZIP, including nested archives."""

    view = memoryview(payload)
    with io.BytesIO(view) as source:
        return inspect_zip_stream(
            source,
            view.nbytes,
            max_members=max_members,
            max_central_directory_bytes=max_central_directory_bytes,
        )


# endregion [03]


# region [04] Bounded raw-DEFLATE recovery


def _validate_raw_member_bounds(
    path: str | Path,
    *,
    header_offset: int,
    compressed_size: int,
    upper_bound: int,
    max_compressed_bytes: int,
    max_output_bytes: int,
) -> None:
    if header_offset < 0 or compressed_size < 0:
        raise ZipStructureError("negative ZIP member offset or size")
    if max_compressed_bytes < 1 or max_output_bytes < 1:
        raise ValueError("raw DEFLATE bounds must be positive")
    if compressed_size > max_compressed_bytes:
        raise ZipStructureError("compressed ZIP member exceeds the raw recovery safety limit")
    file_size = os.path.getsize(path)
    if upper_bound < 0 or upper_bound > file_size:
        raise ZipStructureError("ZIP member boundary points outside the file")
    if header_offset > upper_bound - _LOCAL_FILE.size:
        raise ZipStructureError("truncated ZIP local header")


def _raw_member_payload_offset(
    source: BinaryIO,
    *,
    header_offset: int,
    compressed_size: int,
    upper_bound: int,
) -> int:
    header = _LOCAL_FILE.unpack(_read_exact(source, header_offset, _LOCAL_FILE.size))
    (
        signature,
        _version,
        flags,
        compression_method,
        _modified_time,
        _modified_date,
        _crc32,
        _local_compressed_size,
        _local_uncompressed_size,
        name_length,
        extra_length,
    ) = header
    if signature != LOCAL_FILE_SIGNATURE:
        raise ZipStructureError("invalid ZIP local file header")
    if flags & 0x1:
        raise ZipStructureError("encrypted ZIP members cannot be recovered")
    if flags & 0x8:
        raise ZipStructureError("ZIP data descriptors are not supported for raw recovery")
    if compression_method != 8:
        raise ZipStructureError("raw recovery supports only DEFLATE members")
    payload_offset = header_offset + _LOCAL_FILE.size + name_length + extra_length
    payload_end = payload_offset + compressed_size
    if payload_offset < header_offset or payload_end > upper_bound:
        raise ZipStructureError("ZIP member payload crosses its boundary")
    return payload_offset


def _extend_raw_deflate_output(
    decompressor,
    compressed: bytes,
    output: bytearray,
    max_output_bytes: int,
) -> None:
    pending = compressed
    while pending:
        before = len(pending)
        produced = decompressor.decompress(pending, max_output_bytes - len(output) + 1)
        output.extend(produced)
        if len(output) > max_output_bytes:
            raise ZipStructureError("raw DEFLATE output exceeds the safety limit")
        pending = decompressor.unconsumed_tail
        if pending and len(pending) == before and not produced:
            raise zlib.error("raw DEFLATE decoder made no progress")


def _decompress_raw_member(
    source: BinaryIO,
    *,
    payload_offset: int,
    compressed_size: int,
    max_output_bytes: int,
    checkpoint: Callable[[], None] | None,
) -> bytes:
    source.seek(payload_offset)
    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    output = bytearray()
    remaining = compressed_size
    while remaining:
        if checkpoint is not None:
            checkpoint()
        chunk = source.read(min(RAW_DEFLATE_CHUNK_BYTES, remaining))
        if not chunk:
            raise ZipStructureError("truncated ZIP member payload")
        remaining -= len(chunk)
        _extend_raw_deflate_output(decompressor, chunk, output, max_output_bytes)

    output.extend(decompressor.flush(max_output_bytes - len(output) + 1))
    if len(output) > max_output_bytes:
        raise ZipStructureError("raw DEFLATE output exceeds the safety limit")
    if not decompressor.eof:
        raise zlib.error("incomplete or truncated raw DEFLATE stream")
    if decompressor.unused_data:
        raise ZipStructureError("raw DEFLATE member contains trailing data")
    return bytes(output)


def read_raw_deflate_member(
    path: str | Path,
    *,
    header_offset: int,
    compressed_size: int,
    upper_bound: int,
    max_compressed_bytes: int,
    max_output_bytes: int,
    checkpoint: Callable[[], None] | None = None,
) -> RawDeflateMember:
    """Recover one simple DEFLATE member without trusting CRC/size metadata.

    Recovery is intentionally limited to unencrypted local entries without a
    data descriptor. The caller supplies the next structural boundary so the
    compressed payload cannot overlap another member or the central directory.
    """

    _validate_raw_member_bounds(
        path,
        header_offset=header_offset,
        compressed_size=compressed_size,
        upper_bound=upper_bound,
        max_compressed_bytes=max_compressed_bytes,
        max_output_bytes=max_output_bytes,
    )

    with open(path, "rb", buffering=0) as source:
        payload_offset = _raw_member_payload_offset(
            source,
            header_offset=header_offset,
            compressed_size=compressed_size,
            upper_bound=upper_bound,
        )
        payload = _decompress_raw_member(
            source,
            payload_offset=payload_offset,
            compressed_size=compressed_size,
            max_output_bytes=max_output_bytes,
            checkpoint=checkpoint,
        )
    return RawDeflateMember(
        payload=payload,
        actual_size=len(payload),
        actual_crc32=zlib.crc32(payload) & 0xFFFFFFFF,
    )


# endregion [04]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.zip_safety")
