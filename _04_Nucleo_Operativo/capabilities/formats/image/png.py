"""Bounded structural PNG probe for independent corruption evidence."""

from __future__ import annotations

from . import preserve_legacy_module as _preserve_legacy_module

import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal


# region [01] Probe contract and bounds


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_PROBE_VERSION = "png-structure-crc32-v1"
PNG_READ_CHUNK_BYTES = 64 * 1024
MAX_PNG_PROBE_BYTES = 512 * 1024 * 1024
MAX_PNG_CHUNKS = 1_000_000

PngProbeStatus = Literal["valid", "corrupt", "inconclusive"]


@dataclass(frozen=True, slots=True)
class PngProbeResult:
    status: PngProbeStatus
    reason_code: str
    bytes_checked: int
    chunks_checked: int
    width: int | None = None
    height: int | None = None
    provenance: str = PNG_PROBE_VERSION

    @property
    def deterministic_corruption(self) -> bool:
        return self.status == "corrupt"

    def evidence(self) -> dict[str, object]:
        return {
            "probe_status": self.status,
            "probe_reason": self.reason_code,
            "bytes_checked": self.bytes_checked,
            "chunks_checked": self.chunks_checked,
            "width": self.width,
            "height": self.height,
            "probe_provenance": self.provenance,
        }


# endregion [01]


# region [02] Incremental structural validation


def _read_exact(stream: BinaryIO, length: int) -> bytes | None:
    payload = stream.read(length)
    return payload if len(payload) == length else None


def _valid_ihdr(payload: bytes) -> tuple[int, int] | None:
    if len(payload) != 13:
        return None
    width = int.from_bytes(payload[0:4], "big")
    height = int.from_bytes(payload[4:8], "big")
    bit_depth = payload[8]
    color_type = payload[9]
    compression, filtering, interlace = payload[10:13]
    legal_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if (
        width < 1
        or height < 1
        or bit_depth not in legal_depths.get(color_type, set())
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
    ):
        return None
    return width, height


@dataclass(slots=True)
class _PngProbeState:
    bytes_checked: int = 0
    chunks_checked: int = 0
    width: int | None = None
    height: int | None = None
    saw_ihdr: bool = False
    saw_idat: bool = False
    saw_iend: bool = False


@dataclass(frozen=True, slots=True)
class _PngChunk:
    kind: bytes
    length: int
    ihdr_payload: bytes | None


def _probe_result(
    state: _PngProbeState,
    status: PngProbeStatus,
    reason: str,
    *,
    include_dimensions: bool = True,
) -> PngProbeResult:
    width = state.width if include_dimensions else None
    height = state.height if include_dimensions else None
    return PngProbeResult(
        status,
        reason,
        state.bytes_checked,
        state.chunks_checked,
        width,
        height,
    )


def _read_png_chunk_header(
    stream: BinaryIO,
    file_size: int,
    state: _PngProbeState,
) -> tuple[int, bytes] | PngProbeResult:
    header = _read_exact(stream, 8)
    if header is None:
        return _probe_result(state, "corrupt", "truncated_chunk_header")
    state.bytes_checked += 8
    length = int.from_bytes(header[:4], "big")
    chunk_type = header[4:]
    if not all(65 <= value <= 90 or 97 <= value <= 122 for value in chunk_type):
        return _probe_result(state, "corrupt", "invalid_chunk_type")
    if length > file_size - state.bytes_checked - 4:
        return _probe_result(state, "corrupt", "invalid_chunk_length")
    return length, chunk_type


def _read_png_chunk_payload(
    stream: BinaryIO,
    chunk_type: bytes,
    length: int,
    state: _PngProbeState,
) -> tuple[int, bytes | None] | PngProbeResult:
    checksum = zlib.crc32(chunk_type)
    remaining = length
    ihdr_payload = bytearray() if chunk_type == b"IHDR" else None
    while remaining:
        block = stream.read(min(remaining, PNG_READ_CHUNK_BYTES))
        if not block:
            return _probe_result(state, "corrupt", "truncated_chunk_data")
        state.bytes_checked += len(block)
        remaining -= len(block)
        checksum = zlib.crc32(block, checksum)
        if ihdr_payload is not None:
            ihdr_payload.extend(block)
    return checksum, bytes(ihdr_payload) if ihdr_payload is not None else None


def _read_png_chunk(
    stream: BinaryIO,
    file_size: int,
    state: _PngProbeState,
) -> _PngChunk | PngProbeResult:
    header = _read_png_chunk_header(stream, file_size, state)
    if isinstance(header, PngProbeResult):
        return header
    length, chunk_type = header
    payload = _read_png_chunk_payload(stream, chunk_type, length, state)
    if isinstance(payload, PngProbeResult):
        return payload
    checksum, ihdr_payload = payload
    stored_crc = _read_exact(stream, 4)
    if stored_crc is None:
        return _probe_result(state, "corrupt", "truncated_chunk_crc")
    state.bytes_checked += 4
    state.chunks_checked += 1
    if int.from_bytes(stored_crc, "big") != checksum & 0xFFFFFFFF:
        return _probe_result(state, "corrupt", "crc32_mismatch")
    return _PngChunk(chunk_type, length, ihdr_payload)


def _record_png_chunk(
    chunk: _PngChunk,
    file_size: int,
    state: _PngProbeState,
) -> PngProbeResult | None:
    if state.chunks_checked == 1 and chunk.kind != b"IHDR":
        return _probe_result(
            state, "corrupt", "ihdr_not_first", include_dimensions=False
        )
    if chunk.kind == b"IHDR":
        if state.saw_ihdr or chunk.ihdr_payload is None:
            return _probe_result(
                state, "corrupt", "duplicate_ihdr", include_dimensions=False
            )
        dimensions = _valid_ihdr(chunk.ihdr_payload)
        if dimensions is None:
            return _probe_result(
                state, "corrupt", "invalid_ihdr", include_dimensions=False
            )
        state.width, state.height = dimensions
        state.saw_ihdr = True
    elif chunk.kind == b"IDAT":
        state.saw_idat = True
    elif chunk.kind == b"IEND":
        if chunk.length != 0:
            return _probe_result(state, "corrupt", "invalid_iend_length")
        state.saw_iend = True
        if state.bytes_checked != file_size:
            return _probe_result(state, "corrupt", "trailing_bytes_after_iend")
    return None


def _complete_png_probe(state: _PngProbeState) -> PngProbeResult:
    if not state.saw_ihdr:
        reason = "missing_ihdr"
    elif not state.saw_idat:
        reason = "missing_idat"
    elif not state.saw_iend:
        reason = "missing_iend"
    else:
        return _probe_result(state, "valid", "structure_and_crc_valid")
    return _probe_result(state, "corrupt", reason)


def probe_png_structure(path: Path) -> PngProbeResult:
    """Validate one PNG incrementally without decoding or retaining pixels."""

    file_size = path.stat().st_size
    if file_size > MAX_PNG_PROBE_BYTES:
        return PngProbeResult("inconclusive", "probe_byte_limit", 0, 0)
    state = _PngProbeState()

    with path.open("rb") as stream:
        signature = _read_exact(stream, len(PNG_SIGNATURE))
        state.bytes_checked += len(signature or b"")
        if signature != PNG_SIGNATURE:
            return _probe_result(state, "corrupt", "invalid_signature")

        while state.bytes_checked < file_size:
            if state.chunks_checked >= MAX_PNG_CHUNKS:
                return _probe_result(state, "inconclusive", "probe_chunk_limit")
            chunk = _read_png_chunk(stream, file_size, state)
            if isinstance(chunk, PngProbeResult):
                return chunk
            error = _record_png_chunk(chunk, file_size, state)
            if error is not None:
                return error
            if state.saw_iend:
                break

    return _complete_png_probe(state)


# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.image_png")
