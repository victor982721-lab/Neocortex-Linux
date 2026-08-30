"""Stable file identity codec and route-consumer regressions."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.capabilities.formats.audio.route import _file_key as audio_file_key
from neocortex.documents.document_catalog import _split_file_key
from neocortex.capabilities.formats.docx.route import _file_key as docx_file_key
from neocortex.foundation.file_identity import (
    MAX_FILE_IDENTITY_COMPONENT,
    AmbiguousFileIdentityError,
    FileIdentity,
    FileIdentityEncoding,
    FileIdentityError,
    decode_file_identity,
    encode_file_identity,
    file_key_from_snapshot,
)
from neocortex.capabilities.formats.image.state import file_key as image_file_key
from neocortex.capabilities.formats.office.route import _file_key as office_file_key
from neocortex.capabilities.formats.pdf.pdf_route_cache import file_key as pdf_file_key
from neocortex.semantic.semantic_sources import (
    SemanticSourceError,
    _snapshot_from_image_row,
)


# region [01] Codec round trips, boundaries and canonical output


@pytest.mark.parametrize(
    ("volume_id", "file_id"),
    (
        (0, 0),
        (1, 15),
        (16, 255),
        ((1 << 64) - 1, 1 << 64),
        (1 << 127, 1 << 127),
        (MAX_FILE_IDENTITY_COMPONENT, MAX_FILE_IDENTITY_COMPONENT),
    ),
)
def test_packed_hex_v1_round_trips_all_unsigned_128_bit_boundaries(
    volume_id: int,
    file_id: int,
) -> None:
    identity = FileIdentity(volume_id, file_id)
    key = identity.packed_key

    assert len(key) == 65
    assert key[32] == ":"
    assert key == key.lower()
    assert decode_file_identity(key, encoding=FileIdentityEncoding.PACKED_HEX_V1) == identity
    assert FileIdentity.decode(key, encoding=FileIdentityEncoding.PACKED_HEX_V1) == identity


def test_packed_output_preserves_the_existing_primary_key_exactly() -> None:
    assert encode_file_identity(0x1A, 0x2B) == (
        "0000000000000000000000000000001a:0000000000000000000000000000002b"
    )


def test_packed_decoder_accepts_uppercase_but_reencodes_canonically() -> None:
    key = "000000000000000000000000000000AF:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"

    identity = decode_file_identity(key, encoding=FileIdentityEncoding.PACKED_HEX_V1)

    assert identity.packed_key == key.lower()


@pytest.mark.parametrize(
    "component",
    (-1, MAX_FILE_IDENTITY_COMPONENT + 1, True, 1.0),
)
def test_identity_rejects_values_outside_the_unsigned_128_bit_contract(
    component: object,
) -> None:
    with pytest.raises(FileIdentityError):
        FileIdentity(component, 1)  # type: ignore[arg-type]


# endregion [01]


# region [02] Explicit legacy compatibility and ambiguity rejection


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        ("0:0", FileIdentity(0, 0)),
        ("1:2", FileIdentity(1, 2)),
        (
            "12345678901234567890123456789012:7",
            FileIdentity(12345678901234567890123456789012, 7),
        ),
        (
            f"{MAX_FILE_IDENTITY_COMPONENT}:{MAX_FILE_IDENTITY_COMPONENT}",
            FileIdentity(
                MAX_FILE_IDENTITY_COMPONENT,
                MAX_FILE_IDENTITY_COMPONENT,
            ),
        ),
    ),
)
def test_legacy_decimal_is_supported_as_an_explicit_versioned_encoding(
    key: str,
    expected: FileIdentity,
) -> None:
    identity = decode_file_identity(key, encoding=FileIdentityEncoding.LEGACY_DECIMAL)

    assert identity == expected
    assert identity.encode(FileIdentityEncoding.LEGACY_DECIMAL) == key


def test_auto_detection_keeps_unambiguous_legacy_state_compatible() -> None:
    assert decode_file_identity("12345678901234567890123456789012:7") == FileIdentity(
        12345678901234567890123456789012,
        7,
    )
    assert decode_file_identity("21:34") == FileIdentity(21, 34)


def test_auto_detection_rejects_two_ambiguous_32_digit_decimal_components() -> None:
    key = "12345678901234567890123456789012:23456789012345678901234567890123"

    with pytest.raises(AmbiguousFileIdentityError, match="explicit encoding"):
        decode_file_identity(key)

    assert decode_file_identity(key, encoding=FileIdentityEncoding.LEGACY_DECIMAL) == FileIdentity(
        12345678901234567890123456789012,
        23456789012345678901234567890123,
    )
    assert decode_file_identity(key, encoding=FileIdentityEncoding.PACKED_HEX_V1) == FileIdentity(
        int("12345678901234567890123456789012", 16),
        int("23456789012345678901234567890123", 16),
    )


@pytest.mark.parametrize(
    "key",
    (
        "",
        "1",
        "1:2:3",
        ":1",
        "1:",
        "-1:2",
        "+1:2",
        "01:2",
        " 1:2",
        "1:2 ",
    ),
)
def test_legacy_decimal_rejects_noncanonical_or_malformed_text(key: str) -> None:
    with pytest.raises(FileIdentityError):
        decode_file_identity(key, encoding=FileIdentityEncoding.LEGACY_DECIMAL)


def test_encoder_rejects_auto_and_unknown_encodings() -> None:
    with pytest.raises(FileIdentityError, match="decode-only"):
        FileIdentity(1, 2).encode(FileIdentityEncoding.AUTO)
    with pytest.raises(FileIdentityError, match="unsupported"):
        decode_file_identity("1:2", encoding="future-v9")


# endregion [02]


# region [03] Integrated route, catalog and semantic consumers


def test_all_route_cache_writers_share_the_exact_same_codec() -> None:
    snapshot = FileSnapshot(
        path=r"C:\datos\equipo.pdf",
        volume_id=0x1234,
        file_id=0xABCDEF,
        size=10,
        mtime_ns=20,
        birthtime_ns=30,
    )
    expected = "00000000000000000000000000001234:00000000000000000000000000abcdef"

    assert file_key_from_snapshot(snapshot) == expected
    assert audio_file_key(snapshot) == expected
    assert docx_file_key(snapshot) == expected
    assert image_file_key(snapshot) == expected
    assert office_file_key(snapshot) == expected
    assert pdf_file_key(snapshot) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        (
            "0000000000000000000000000000001a:0000000000000000000000000000002b",
            ("26", "43"),
        ),
        ("26:43", ("26", "43")),
    ),
)
def test_catalog_identity_fields_use_neutral_decimal_values(
    key: str,
    expected: tuple[str, str],
) -> None:
    assert _split_file_key(key) == expected


def test_catalog_rejects_ambiguous_legacy_identity_instead_of_reinterpreting_it() -> None:
    key = "12345678901234567890123456789012:23456789012345678901234567890123"

    with pytest.raises(AmbiguousFileIdentityError):
        _split_file_key(key)


def _image_row(file_key: str) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    with closing(connection):
        return connection.execute(
            """SELECT ? AS file_key,? AS path,? AS size,? AS mtime_ns,
            ? AS birthtime_ns""",
            (file_key, r"C:\datos\imagen.jpg", 10, 20, 30),
        ).fetchone()


@pytest.mark.parametrize(
    ("key", "expected_identity"),
    (
        (
            "0000000000000000000000000000001a:0000000000000000000000000000002b",
            (26, 43),
        ),
        ("26:43", (26, 43)),
    ),
)
def test_semantic_image_adapter_decodes_current_and_legacy_state(
    key: str,
    expected_identity: tuple[int, int],
) -> None:
    snapshot = _snapshot_from_image_row(_image_row(key))

    assert snapshot.identity == expected_identity


def test_semantic_image_adapter_wraps_ambiguous_identity_as_source_error() -> None:
    key = "12345678901234567890123456789012:23456789012345678901234567890123"

    with pytest.raises(SemanticSourceError, match="invalid image file identity"):
        _snapshot_from_image_row(_image_row(key))


# endregion [03]
