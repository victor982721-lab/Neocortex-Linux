"""Bounded ZIP structure preflight before Python materializes member metadata."""

from __future__ import annotations

import io
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable
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
_CENTRAL_DIRECTORY = struct.Struct("<4s6H3I5H2I")
_EXTRA_FIELD_HEADER = struct.Struct("<HH")
ZIP64_EXTRA_FIELD_ID = 0x0001


class ZipStructureError(ValueError):
    """ZIP metadata is absent, inconsistent or exceeds an explicit bound."""


@dataclass(frozen=True, slots=True)
class ZipStructure:
    members: int
    central_directory_bytes: int
    central_directory_offset: int
    zip64: bool
    # The entry tuple is an additive extension of the historical four-field
    # structure.  Keeping it on the preflight result means callers can retain
    # duplicate names without building a ``name -> ZipInfo`` dictionary.  The
    # default keeps hand-built/legacy instances source compatible.
    entries: tuple["ZipMemberStructure", ...] = ()


@dataclass(frozen=True, slots=True)
class ZipMemberStructure:
    """Bounded structural identity for one central-directory entry.

    ``ordinal`` is the zero-based central-directory position and
    ``header_offset`` is the physical local-header offset after any prepended
    self-extracting stub adjustment.  Neither the filename nor CRC is used as
    identity; ZIPs are allowed to contain repeated names and stale metadata.
    ``upper_bound`` is the next local-header boundary (or the physical central
    directory start), useful to keep recovery reads inside one entry.
    """

    ordinal: int
    filename: str
    header_offset: int
    payload_offset: int
    compressed_size: int
    uncompressed_size: int
    crc32: int
    compression_method: int
    flags: int
    upper_bound: int
    is_directory: bool = False


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


def _zip64_details(
    source,
    eocd_offset: int,
    file_size: int,
) -> tuple[int, int, int, int]:
    locator_offset = eocd_offset - ZIP64_LOCATOR_BYTES
    if locator_offset < 0:
        raise ZipStructureError("ZIP64 locator is missing")
    locator = _ZIP64_LOCATOR.unpack(_read_exact(source, locator_offset, ZIP64_LOCATOR_BYTES))
    signature, disk_number, record_offset, disk_count = locator
    if signature != ZIP64_LOCATOR_SIGNATURE:
        raise ZipStructureError("ZIP64 locator is missing")
    if disk_number != 0 or disk_count != 1:
        raise ZipStructureError("multi-disk ZIP containers are not supported")
    raw_record_offset = int(record_offset)
    physical_record_offset = raw_record_offset
    record: tuple[Any, ...] | None = None
    if raw_record_offset + ZIP64_EOCD_MIN_BYTES <= file_size:
        candidate = _read_exact(source, raw_record_offset, ZIP64_EOCD_MIN_BYTES)
        if candidate.startswith(ZIP64_EOCD_SIGNATURE):
            candidate_size = struct.unpack_from("<Q", candidate, 4)[0]
            candidate_end = raw_record_offset + 12 + int(candidate_size)
            if candidate_size >= 44 and candidate_end == locator_offset:
                record = _ZIP64_EOCD.unpack(candidate)
    if record is None:
        # ZIP readers, including Python's zipfile, support a prepended stub by
        # locating a fixed-size ZIP64 end record immediately before the locator.
        physical_record_offset = locator_offset - ZIP64_EOCD_MIN_BYTES
        if physical_record_offset < 0:
            raise ZipStructureError("ZIP64 end record points outside the file")
        record = _ZIP64_EOCD.unpack(
            _read_exact(source, physical_record_offset, ZIP64_EOCD_MIN_BYTES)
        )
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
    record_end = physical_record_offset + 12 + int(record_size)
    if record_end != locator_offset:
        raise ZipStructureError("ZIP64 end record is not adjacent to its locator")
    if disk_number != 0 or central_disk != 0 or disk_members != members:
        raise ZipStructureError("multi-disk ZIP containers are not supported")
    if record_end > file_size:
        raise ZipStructureError("ZIP64 end record points outside the file")
    return int(members), int(central_size), int(central_offset), physical_record_offset


def _zip64_values(source, eocd_offset: int, file_size: int) -> tuple[int, int, int]:
    """Return the ZIP64 directory values, preserving the historical helper API."""

    members, central_size, central_offset, _record_offset = _zip64_details(
        source,
        eocd_offset,
        file_size,
    )
    return members, central_size, central_offset


def _zip64_entry_values(
    central: tuple[Any, ...],
    extra: bytes,
) -> tuple[int, int, int, int]:
    """Resolve ZIP64 member fields and validate every extra-field length."""

    (
        _signature,
        _version_made,
        _version_needed,
        _flags,
        _compression,
        _modified_time,
        _modified_date,
        _crc32,
        compressed_size,
        uncompressed_size,
        _name_length,
        _extra_length,
        _comment_length,
        disk_number_start,
        _internal_attributes,
        _external_attributes,
        local_header_offset,
    ) = central
    zip64_payload: bytes | None = None
    cursor = 0
    while cursor < len(extra):
        if len(extra) - cursor < _EXTRA_FIELD_HEADER.size:
            raise ZipStructureError("truncated ZIP extra field header")
        field_id, field_length = _EXTRA_FIELD_HEADER.unpack_from(extra, cursor)
        field_start = cursor + _EXTRA_FIELD_HEADER.size
        field_end = field_start + int(field_length)
        if field_end > len(extra):
            raise ZipStructureError("ZIP extra field extends beyond its member record")
        if field_id == ZIP64_EXTRA_FIELD_ID:
            if zip64_payload is not None:
                raise ZipStructureError("duplicate ZIP64 extra field")
            zip64_payload = extra[field_start:field_end]
        cursor = field_end

    needs_uncompressed = uncompressed_size == 0xFFFFFFFF
    needs_compressed = compressed_size == 0xFFFFFFFF
    needs_local_offset = local_header_offset == 0xFFFFFFFF
    needs_disk_number = disk_number_start == 0xFFFF
    if not any((needs_uncompressed, needs_compressed, needs_local_offset, needs_disk_number)):
        return (
            int(compressed_size),
            int(uncompressed_size),
            int(local_header_offset),
            int(disk_number_start),
        )
    if zip64_payload is None:
        raise ZipStructureError("ZIP64 member field has no ZIP64 extra value")

    cursor = 0

    def take(width: int) -> bytes:
        nonlocal cursor
        if len(zip64_payload) - cursor < width:
            raise ZipStructureError("truncated ZIP64 member extra data")
        result = zip64_payload[cursor : cursor + width]
        cursor += width
        return result

    if needs_uncompressed:
        uncompressed_size = struct.unpack("<Q", take(8))[0]
    if needs_compressed:
        compressed_size = struct.unpack("<Q", take(8))[0]
    if needs_local_offset:
        local_header_offset = struct.unpack("<Q", take(8))[0]
    if needs_disk_number:
        disk_number_start = struct.unpack("<I", take(4))[0]
    return (
        int(compressed_size),
        int(uncompressed_size),
        int(local_header_offset),
        int(disk_number_start),
    )


def _validate_central_member(
    source,
    *,
    central: tuple[Any, ...],
    ordinal: int = 0,
    extra_offset: int,
    name_offset: int | None = None,
    central_directory_offset: int,
    offset_adjustment: int,
    file_size: int,
) -> ZipMemberStructure:
    """Validate one central record and its local data boundary."""

    (
        signature,
        _version_made,
        _version_needed,
        _flags,
        _compression,
        _modified_time,
        _modified_date,
        _crc32,
        _compressed_size,
        _uncompressed_size,
        _name_length,
        extra_length,
        _comment_length,
        _disk_number_start,
        _internal_attributes,
        _external_attributes,
        _local_header_offset,
    ) = central
    if name_offset is None:
        # Kept as an optional keyword for the historical private helper seam;
        # central-directory callers always pass the actual name offset.
        name_offset = extra_offset - int(_name_length)
    raw_name = _read_exact(source, int(name_offset), int(_name_length))
    try:
        name = raw_name.decode("utf-8" if int(_flags) & 0x800 else "cp437", "strict")
    except UnicodeDecodeError as exc:
        raise ZipStructureError("ZIP member name is not decodable") from exc
    if "\x00" in name:
        raise ZipStructureError("ZIP member name contains a NUL byte")
    if signature != b"PK\x01\x02":
        raise ZipStructureError("invalid ZIP central directory record signature")
    extra = _read_exact(source, extra_offset, int(extra_length))
    compressed_size, _uncompressed_size, local_header_offset, disk_number_start = (
        _zip64_entry_values(central, extra)
    )
    if disk_number_start != 0:
        raise ZipStructureError("multi-disk ZIP members are not supported")
    local_header_offset += offset_adjustment
    if local_header_offset < 0 or local_header_offset >= central_directory_offset:
        raise ZipStructureError("ZIP local header points outside member data")
    if local_header_offset + _LOCAL_FILE.size > central_directory_offset:
        raise ZipStructureError("truncated ZIP local file header")
    local = _LOCAL_FILE.unpack(
        _read_exact(source, int(local_header_offset), _LOCAL_FILE.size)
    )
    (
        local_signature,
        _version,
        local_flags,
        _local_compression,
        _modified_time,
        _modified_date,
        _local_crc32,
        _local_compressed_size,
        _local_uncompressed_size,
        local_name_length,
        local_extra_length,
    ) = local
    if local_signature != LOCAL_FILE_SIGNATURE:
        raise ZipStructureError("invalid ZIP local file header")
    payload_offset = (
        int(local_header_offset)
        + _LOCAL_FILE.size
        + int(local_name_length)
        + int(local_extra_length)
    )
    if payload_offset > central_directory_offset:
        raise ZipStructureError("ZIP local file header extends into the central directory")
    if compressed_size > central_directory_offset - payload_offset:
        raise ZipStructureError("ZIP member payload points outside member data")
    if not (int(local_flags) & 0x8):
        if int(_local_compressed_size) not in {0xFFFFFFFF, compressed_size}:
            raise ZipStructureError("ZIP local and central compressed sizes disagree")
        if int(_local_uncompressed_size) not in {0xFFFFFFFF, _uncompressed_size}:
            raise ZipStructureError("ZIP local and central uncompressed sizes disagree")
    if payload_offset + compressed_size > file_size:
        raise ZipStructureError("ZIP member payload points outside the file")
    return ZipMemberStructure(
        ordinal=int(ordinal),
        filename=name,
        header_offset=int(local_header_offset),
        payload_offset=int(payload_offset),
        compressed_size=int(compressed_size),
        uncompressed_size=int(_uncompressed_size),
        crc32=int(_crc32),
        compression_method=int(_compression),
        flags=int(_flags),
        # The next local-header boundary is filled in by the central-directory
        # pass once all offsets are known.  ``central_directory_offset`` is a
        # safe conservative value for the one-entry case.
        upper_bound=int(central_directory_offset),
        is_directory=name.endswith("/"),
    )


def _inspect_central_directory(
    source,
    *,
    central_directory_offset: int,
    central_directory_bytes: int,
    declared_members: int,
    max_members: int,
    offset_adjustment: int,
    file_size: int,
) -> tuple[ZipMemberStructure, ...]:
    """Count and validate central records without building a ZipInfo list."""

    central_end = central_directory_offset + central_directory_bytes
    cursor = central_directory_offset
    actual_members = 0
    seen_local_offsets: set[int] = set()
    entries: list[ZipMemberStructure] = []
    while cursor < central_end:
        remaining = central_end - cursor
        if remaining < _CENTRAL_DIRECTORY.size:
            raise ZipStructureError("truncated ZIP central directory record")
        central = _CENTRAL_DIRECTORY.unpack(
            _read_exact(source, cursor, _CENTRAL_DIRECTORY.size)
        )
        if central[0] != b"PK\x01\x02":
            raise ZipStructureError("invalid ZIP central directory record signature")
        name_length = int(central[10])
        extra_length = int(central[11])
        comment_length = int(central[12])
        record_length = _CENTRAL_DIRECTORY.size + name_length + extra_length + comment_length
        if record_length > remaining:
            raise ZipStructureError("ZIP central directory record extends beyond its boundary")
        actual_members += 1
        if actual_members > max_members:
            raise ZipStructureError(
                f"ZIP contains more than {max_members} members; limit is {max_members}"
            )
        if actual_members > declared_members:
            raise ZipStructureError(
                "ZIP central directory contains more records than its end record declares: "
                f"declared {declared_members}, found at least {actual_members}"
            )
        local_header_offset = _validate_central_member(
            source,
            central=central,
            ordinal=actual_members - 1,
            name_offset=cursor + _CENTRAL_DIRECTORY.size,
            extra_offset=cursor + _CENTRAL_DIRECTORY.size + name_length,
            central_directory_offset=central_directory_offset,
            offset_adjustment=offset_adjustment,
            file_size=file_size,
        )
        if local_header_offset.header_offset in seen_local_offsets:
            raise ZipStructureError("ZIP members share a local header offset")
        seen_local_offsets.add(local_header_offset.header_offset)
        entries.append(local_header_offset)
        cursor += record_length
    if cursor != central_end:
        raise ZipStructureError("ZIP central directory length is inconsistent")
    if actual_members != declared_members:
        raise ZipStructureError(
            "ZIP central directory member count disagrees with its end record: "
            f"declared {declared_members}, found {actual_members}"
        )
    # Local file headers need not occur in central-directory order.  Recovery
    # must nevertheless use physical, not lexical, boundaries so a malformed
    # compressed-size field cannot consume a neighbouring member.  The central
    # record's ordinal remains stable and is restored in the returned order.
    by_offset = sorted(entries, key=lambda item: item.header_offset)
    upper_bounds = {
        item.header_offset: (
            by_offset[index + 1].header_offset
            if index + 1 < len(by_offset)
            else central_directory_offset
        )
        for index, item in enumerate(by_offset)
    }
    return tuple(
        ZipMemberStructure(
            ordinal=item.ordinal,
            filename=item.filename,
            header_offset=item.header_offset,
            payload_offset=item.payload_offset,
            compressed_size=item.compressed_size,
            uncompressed_size=item.uncompressed_size,
            crc32=item.crc32,
            compression_method=item.compression_method,
            flags=item.flags,
            upper_bound=upper_bounds[item.header_offset],
            is_directory=item.is_directory,
        )
        for item in entries
    )


# endregion [02]


# region [03] Public validation


def _inspect_zip_source(
    source: BinaryIO,
    file_size: int,
    *,
    max_members: int,
    max_central_directory_bytes: int = DEFAULT_MAX_CENTRAL_DIRECTORY_BYTES,
) -> ZipStructure:
    """Validate EOCD and every central record without materializing ZipInfo objects."""

    if file_size < 0:
        raise ValueError("file_size cannot be negative")
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
    comment_length = int(values[-1])
    if eocd_offset + EOCD_FIXED_BYTES + comment_length != file_size:
        raise ZipStructureError("ZIP end record is not at the end of the file")
    if disk_number != 0 or central_disk != 0:
        raise ZipStructureError("multi-disk ZIP containers are not supported")
    is_zip64 = (
        disk_members == 0xFFFF
        or members == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    )
    zip64_record_offset: int | None = None
    if is_zip64:
        zip64_members, zip64_size, zip64_offset, zip64_record_offset = _zip64_details(
            source,
            eocd_offset,
            file_size,
        )
        if disk_members != 0xFFFF and disk_members != zip64_members:
            raise ZipStructureError("ZIP end record member counts are inconsistent")
        if members != 0xFFFF and members != zip64_members:
            raise ZipStructureError("ZIP end record member counts are inconsistent")
        if central_size != 0xFFFFFFFF and central_size != zip64_size:
            raise ZipStructureError("ZIP end record central directory sizes are inconsistent")
        if central_offset != 0xFFFFFFFF and central_offset != zip64_offset:
            raise ZipStructureError("ZIP end record central directory offsets are inconsistent")
        members, central_size, central_offset = zip64_members, zip64_size, zip64_offset
    elif disk_members != members:
        raise ZipStructureError("multi-disk ZIP containers are not supported")

    if members > max_members:
        raise ZipStructureError(f"ZIP contains {members} members; limit is {max_members}")
    if central_size > max_central_directory_bytes:
        raise ZipStructureError(
            "ZIP central directory exceeds the safety limit: "
            f"{central_size} > {max_central_directory_bytes} bytes"
        )
    expected_central_end = (
        zip64_record_offset if zip64_record_offset is not None else eocd_offset
    )
    if central_size > expected_central_end:
        raise ZipStructureError("ZIP central directory points outside the file")
    offset_adjustment = expected_central_end - central_size - central_offset
    if offset_adjustment < 0:
        raise ZipStructureError("ZIP central directory does not end at its end record")
    physical_central_offset = central_offset + offset_adjustment
    if physical_central_offset < 0 or physical_central_offset + central_size > file_size:
        raise ZipStructureError("ZIP central directory points outside the file")
    entries = _inspect_central_directory(
        source,
        central_directory_offset=physical_central_offset,
        central_directory_bytes=central_size,
        declared_members=members,
        max_members=max_members,
        offset_adjustment=offset_adjustment,
        file_size=file_size,
    )
    return ZipStructure(
        members,
        central_size,
        physical_central_offset,
        is_zip64,
        entries,
    )


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
    """Read bounded end metadata and incrementally validate the central directory."""

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
