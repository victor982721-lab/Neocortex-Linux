"""Versioned, lossless codec for stable filesystem identities.

The packed representation intentionally has no textual version prefix because it is
already persisted as a primary key by every route cache.  The selected codec is
versioned by :class:`FileIdentityEncoding`; changing the on-disk text would require
an explicit state migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


# region [01] Public contract and validation bounds

PACKED_HEX_COMPONENT_WIDTH = 32
MAX_FILE_IDENTITY_COMPONENT = (1 << (PACKED_HEX_COMPONENT_WIDTH * 4)) - 1


class FileIdentityEncoding(StrEnum):
    """Supported textual encodings for one stable file identity."""

    PACKED_HEX_V1 = "packed-hex-v1"
    LEGACY_DECIMAL = "legacy-decimal"
    AUTO = "auto"


class FileIdentityError(ValueError):
    """A stable file identity is malformed or outside the codec bounds."""


class AmbiguousFileIdentityError(FileIdentityError):
    """Text is valid in multiple encodings and needs an explicit encoding."""


class FileIdentitySource(Protocol):
    """Structural input accepted from inventory snapshots without coupling layers."""

    @property
    def volume_id(self) -> int: ...

    @property
    def file_id(self) -> int: ...


def _validated_component(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FileIdentityError(f"{name} must be an integer")
    if not 0 <= value <= MAX_FILE_IDENTITY_COMPONENT:
        raise FileIdentityError(
            f"{name} must be between 0 and {MAX_FILE_IDENTITY_COMPONENT}"
        )
    return value


# endregion [01]


# region [02] Immutable identity and explicit codecs


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Neutral pair of bounded unsigned 128-bit filesystem identifiers."""

    volume_id: int
    file_id: int

    def __post_init__(self) -> None:
        _validated_component(self.volume_id, "volume_id")
        _validated_component(self.file_id, "file_id")

    @property
    def packed_key(self) -> str:
        """Return the exact primary-key representation already used by route state."""

        return (
            f"{self.volume_id:0{PACKED_HEX_COMPONENT_WIDTH}x}:"
            f"{self.file_id:0{PACKED_HEX_COMPONENT_WIDTH}x}"
        )

    @property
    def decimal_components(self) -> tuple[str, str]:
        """Return neutral decimal fields used by catalog and action snapshots."""

        return str(self.volume_id), str(self.file_id)

    def encode(
        self,
        encoding: FileIdentityEncoding = FileIdentityEncoding.PACKED_HEX_V1,
    ) -> str:
        """Encode through a named format; automatic detection is decode-only."""

        encoding = _coerce_encoding(encoding)
        if encoding is FileIdentityEncoding.PACKED_HEX_V1:
            return self.packed_key
        if encoding is FileIdentityEncoding.LEGACY_DECIMAL:
            volume_id, file_id = self.decimal_components
            return f"{volume_id}:{file_id}"
        raise FileIdentityError("AUTO is decode-only for file identities")

    @classmethod
    def decode(
        cls,
        key: str,
        *,
        encoding: FileIdentityEncoding = FileIdentityEncoding.AUTO,
    ) -> FileIdentity:
        return decode_file_identity(key, encoding=encoding)


def _coerce_encoding(encoding: FileIdentityEncoding | str) -> FileIdentityEncoding:
    try:
        return FileIdentityEncoding(encoding)
    except (TypeError, ValueError) as exc:
        raise FileIdentityError(
            f"unsupported file identity encoding: {encoding!r}"
        ) from exc


def _split_key(key: str) -> tuple[str, str]:
    if not isinstance(key, str):
        raise FileIdentityError("file identity key must be text")
    if key.count(":") != 1:
        raise FileIdentityError(
            "file identity key must contain exactly one ':' separator"
        )
    volume_id, file_id = key.split(":", 1)
    if not volume_id or not file_id:
        raise FileIdentityError("file identity components cannot be empty")
    return volume_id, file_id


def _is_packed_hex_component(value: str) -> bool:
    return len(value) == PACKED_HEX_COMPONENT_WIDTH and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _is_canonical_decimal_component(value: str) -> bool:
    return value == "0" or (
        value[0] in "123456789"
        and value.isascii()
        and all(character in "0123456789" for character in value)
    )


def _decode_packed_hex(volume_id: str, file_id: str) -> FileIdentity:
    if not (_is_packed_hex_component(volume_id) and _is_packed_hex_component(file_id)):
        raise FileIdentityError(
            "packed-hex-v1 identities require two 32-character hexadecimal components"
        )
    return FileIdentity(int(volume_id, 16), int(file_id, 16))


def _decode_legacy_decimal(volume_id: str, file_id: str) -> FileIdentity:
    if not (
        _is_canonical_decimal_component(volume_id)
        and _is_canonical_decimal_component(file_id)
    ):
        raise FileIdentityError(
            "legacy-decimal identities require canonical unsigned decimal components"
        )
    return FileIdentity(int(volume_id, 10), int(file_id, 10))


def decode_file_identity(
    key: str,
    *,
    encoding: FileIdentityEncoding | str = FileIdentityEncoding.AUTO,
) -> FileIdentity:
    """Decode a packed or legacy key without guessing ambiguous decimal text.

    Automatic detection accepts canonical legacy decimal keys and fixed-width packed
    keys.  If both interpretations are valid (notably two unpadded 32-digit decimal
    components), the caller must provide the known storage encoding explicitly.
    """

    encoding = _coerce_encoding(encoding)
    volume_id, file_id = _split_key(key)
    if encoding is FileIdentityEncoding.PACKED_HEX_V1:
        return _decode_packed_hex(volume_id, file_id)
    if encoding is FileIdentityEncoding.LEGACY_DECIMAL:
        return _decode_legacy_decimal(volume_id, file_id)

    packed = _is_packed_hex_component(volume_id) and _is_packed_hex_component(file_id)
    decimal = _is_canonical_decimal_component(
        volume_id
    ) and _is_canonical_decimal_component(file_id)
    if packed and decimal:
        raise AmbiguousFileIdentityError(
            "file identity is valid as both packed-hex-v1 and legacy-decimal; "
            "provide an explicit encoding"
        )
    if packed:
        return _decode_packed_hex(volume_id, file_id)
    if decimal:
        return _decode_legacy_decimal(volume_id, file_id)
    raise FileIdentityError(
        "file identity is neither packed-hex-v1 nor canonical legacy-decimal"
    )


def encode_file_identity(volume_id: int, file_id: int) -> str:
    """Build the canonical packed key from two neutral integer identifiers."""

    return FileIdentity(volume_id, file_id).packed_key


def file_key_from_snapshot(snapshot: FileIdentitySource) -> str:
    """Build the canonical key from any structurally compatible snapshot."""

    return encode_file_identity(snapshot.volume_id, snapshot.file_id)


# endregion [02]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        try:
            _defined_value.__module__ = "_04_Nucleo_Operativo.file_identity"
        except (AttributeError, TypeError):
            pass
del _defined_value
