"""Lossless POSIX path identity, separate from a safe display representation.

The display string is never an operating-system path.  Consumers that cannot
represent ``bytes_base64`` must preserve this envelope and report
``unsupported_path_encoding`` instead of rewriting a name.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

PATH_IDENTITY_SCHEMA = "neocortex.path-identity/v1"


@dataclass(frozen=True, slots=True)
class PathIdentity:
    bytes_base64: str
    display_escaped: str

    @classmethod
    def from_path(cls, path: Path | str | bytes | os.PathLike[str]) -> "PathIdentity":
        raw = os.fsencode(path)
        if b"\0" in raw:
            raise ValueError("POSIX path identity contains a NUL")
        # ascii() distinguishes literal backslashes from controls and decoded
        # surrogateescape codepoints, while the authoritative field is binary.
        display = ascii(os.fsdecode(raw))[1:-1]
        return cls(base64.b64encode(raw).decode("ascii"), display)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PathIdentity":
        if value.get("schema") != PATH_IDENTITY_SCHEMA:
            raise ValueError("unsupported POSIX path identity schema")
        encoded = value.get("bytes_base64")
        if not isinstance(encoded, str):
            raise ValueError("POSIX path identity has no binary name")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid POSIX path identity encoding") from exc
        result = cls.from_path(raw)
        if result.as_dict() != dict(value):
            raise ValueError("POSIX path identity is not canonical")
        return result

    def as_dict(self) -> dict[str, str]:
        return {"schema": PATH_IDENTITY_SCHEMA, "bytes_base64": self.bytes_base64,
                "display_escaped": self.display_escaped}

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.bytes_base64, validate=True)

    def to_path(self) -> Path:
        return Path(os.fsdecode(self.to_bytes()))


__all__ = ["PATH_IDENTITY_SCHEMA", "PathIdentity"]
