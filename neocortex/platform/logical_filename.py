"""Logical filenames and deterministic, non-destructive collision spelling.

GNU backup suffixes and NeoCortex identity decorators are naming metadata, not
content types. This owner never reads payloads, tests existence, or authorizes
an effect; callers still use their identity-bound no-replace backend.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_GNU = re.compile(r"\.~[1-9][0-9]*~$")
_IDENTITY = re.compile(r"^(?P<stem>.+?)(?P<tag>(?:__|~)[0-9a-fA-F]{8,64}(?:_[1-9][0-9]*)?)$")
_COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".d.ts", ".d.ts.map")


@dataclass(frozen=True, slots=True)
class LogicalFilename:
    physical_basename: str
    basename: str
    stem: str
    extension: str
    collision_decorator: str = ""
    gnu_suffixes: tuple[str, ...] = ()

    @classmethod
    def parse(cls, path: str | Path) -> LogicalFilename:
        physical = Path(path).name
        name = physical
        gnu: list[str] = []
        while match := _GNU.search(name):
            gnu.append(match.group())
            name = name[:match.start()]
        # A dotfile is a basename, not an invented extension (Path's portable
        # lexical behavior is appropriate only after decorations are removed).
        extension = Path(name).suffix
        decorated_stem = name[:-len(extension)] if extension else name
        match = _IDENTITY.fullmatch(decorated_stem)
        stem = match['stem'] if match else decorated_stem
        decorator = match['tag'] if match else ""
        return cls(physical, name, stem, extension, decorator, tuple(reversed(gnu)))

    @property
    def logical_extension(self) -> str:
        return self.extension.casefold()

    @property
    def compound_extension(self) -> str:
        plain = (self.stem + self.extension).casefold()
        return next((suffix for suffix in sorted(_COMPOUND, key=len, reverse=True)
                     if plain.endswith(suffix)), self.logical_extension)

    @property
    def normalized_basename(self) -> str:
        return self.stem + self.collision_decorator + self.extension

    def with_extension(self, extension: str) -> str:
        if not extension.startswith('.') or any(c in extension for c in '/\\\x00'):
            raise ValueError('a logical extension must be a safe dot-prefixed suffix')
        return self.stem + self.collision_decorator + extension


def identity_token(*identity: object) -> str:
    """Hash only stable identity metadata, never file contents or mutable paths."""
    encoded = json.dumps(identity, ensure_ascii=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('ascii')).hexdigest()[:12]


def collision_path(requested: Path, token: str, *, attempt: int = 1) -> Path:
    """Return a stable readable candidate; existence is the caller's decision."""
    if re.fullmatch(r'[0-9a-f]{8,64}', token) is None or type(attempt) is not int or attempt < 1:
        raise ValueError('invalid collision identity or attempt')
    name = LogicalFilename.parse(requested)
    counter = '' if attempt == 1 else f'_{attempt}'
    suffix = f'__{token}{counter}{name.extension}'
    # Respect Linux byte limits without splitting a Unicode codepoint. The
    # physical no-replace guard, not this spelling, decides destination safety.
    stem = name.stem
    while len((stem + suffix).encode('utf-8', 'surrogateescape')) > 240 and stem:
        stem = stem[:-1]
    return requested.with_name((stem or 'file') + suffix)
