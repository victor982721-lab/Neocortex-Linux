"""Strict parsers for buffers returned by FSCTL_ENUM_USN_DATA."""
# region [00] Contexto del módulo
# Módulo: neocortex/enumeration/ntfs/parser.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ..errors import CorruptBufferError, UnsupportedRecordVersionError
from ..models import NtfsEntry
# endregion [01]

# region [02] Implementación


_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_V2_HEADER_SIZE = 60
_V3_HEADER_SIZE = 76


def _decode_filetime(value: int) -> datetime | None:
    if value == 0:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except OverflowError as exc:
        raise CorruptBufferError(f"invalid FILETIME value {value}") from exc


def _file_id(data: memoryview) -> int:
    return int.from_bytes(data, byteorder="little", signed=False)


def parse_enum_buffer(
    data: bytes | bytearray | memoryview,
) -> tuple[int, Iterator[NtfsEntry]]:
    """Parse one successful ``FSCTL_ENUM_USN_DATA`` output buffer.

    Returns ``(next_start_file_reference_number, entries_iterator)``.  The
    iterator retains a view of *data* and validates every record before use.
    """

    view = memoryview(data).cast("B")
    if len(view) < 8:
        raise CorruptBufferError("enumeration buffer is shorter than 8 bytes")
    next_start = struct.unpack_from("<Q", view, 0)[0]
    return next_start, _iter_records(view[8:])


def parse_journal_buffer(
    data: bytes | bytearray | memoryview,
) -> tuple[int, Iterator[NtfsEntry]]:
    """Parse one successful ``FSCTL_READ_USN_JOURNAL`` response."""

    view = memoryview(data).cast("B")
    if len(view) < 8:
        raise CorruptBufferError("journal buffer is shorter than 8 bytes")
    next_usn = struct.unpack_from("<q", view, 0)[0]
    return next_usn, _iter_records(view[8:])


def _iter_records(view: memoryview) -> Iterator[NtfsEntry]:
    offset = 0
    total = len(view)
    while offset < total:
        remaining = total - offset
        if remaining < 8:
            # Windows can align a buffer, but non-zero trailing bytes are not a
            # valid record and must never be silently ignored.
            if any(view[offset:]):
                raise CorruptBufferError(f"{remaining} non-record trailing bytes")
            return

        record_length, major, minor = struct.unpack_from("<IHH", view, offset)
        if record_length == 0:
            if any(view[offset:]):
                raise CorruptBufferError("zero-length record before non-zero data")
            return
        if record_length > remaining:
            raise CorruptBufferError(
                f"record length {record_length} exceeds {remaining} available bytes"
            )

        record = view[offset : offset + record_length]
        if major == 2:
            yield _parse_v2(record, minor)
        elif major == 3:
            yield _parse_v3(record, minor)
        else:
            raise UnsupportedRecordVersionError(
                f"USN_RECORD version {major}.{minor} is not supported"
            )
        offset += record_length


def _validate_name(record: memoryview, header_size: int, name_length: int, name_offset: int) -> str:
    if name_length % 2:
        raise CorruptBufferError("UTF-16 filename has an odd byte length")
    end = name_offset + name_length
    if name_offset < header_size or end > len(record):
        raise CorruptBufferError(
            f"filename range [{name_offset}, {end}) is outside record length {len(record)}"
        )
    try:
        return bytes(record[name_offset:end]).decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as exc:
        raise CorruptBufferError("filename is not valid UTF-16LE") from exc


def _parse_v2(record: memoryview, minor: int) -> NtfsEntry:
    if len(record) < _V2_HEADER_SIZE:
        raise CorruptBufferError("USN_RECORD_V2 is shorter than its header")
    (
        file_ref,
        parent_ref,
        usn,
        timestamp,
        reason,
        source_info,
        security_id,
        attributes,
        name_length,
        name_offset,
    ) = struct.unpack_from("<QQqqIIIIHH", record, 8)
    return NtfsEntry(
        file_reference_number=file_ref,
        parent_reference_number=parent_ref,
        name=_validate_name(record, _V2_HEADER_SIZE, name_length, name_offset),
        usn=usn,
        timestamp=_decode_filetime(timestamp),
        reason=reason,
        source_info=source_info,
        security_id=security_id,
        file_attributes=attributes,
        record_major_version=2,
        record_minor_version=minor,
    )


def _parse_v3(record: memoryview, minor: int) -> NtfsEntry:
    if len(record) < _V3_HEADER_SIZE:
        raise CorruptBufferError("USN_RECORD_V3 is shorter than its header")
    file_ref = _file_id(record[8:24])
    parent_ref = _file_id(record[24:40])
    (
        usn,
        timestamp,
        reason,
        source_info,
        security_id,
        attributes,
        name_length,
        name_offset,
    ) = struct.unpack_from("<qqIIIIHH", record, 40)
    return NtfsEntry(
        file_reference_number=file_ref,
        parent_reference_number=parent_ref,
        name=_validate_name(record, _V3_HEADER_SIZE, name_length, name_offset),
        usn=usn,
        timestamp=_decode_filetime(timestamp),
        reason=reason,
        source_info=source_info,
        security_id=security_id,
        file_attributes=attributes,
        record_major_version=3,
        record_minor_version=minor,
    )


# endregion [02]
