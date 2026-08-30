"""Bounded loading and validation for user-controlled taxonomy overlays."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_overlay.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import xxhash

from .document_taxonomy_models import (
    AuthoritySpec,
    ClientSpec,
    OrganizationSpec,
    ProjectSpec,
    TechnicalTaxonomy,
)
from .document_taxonomy_vocabulary import (
    BUILTIN_TAXONOMY_VERSION,
    builtin_taxonomy,
)
# endregion [01]

# region [02] Implementación

MAX_TAXONOMY_BYTES = 1_048_576
MAX_TAXONOMY_TABLES_PER_SECTION = 256
MAX_TAXONOMY_TEXT_CHARS = 512
MAX_TAXONOMY_SEQUENCE_ITEMS = 128
MAX_TAXONOMY_PATTERNS = 64
MAX_TAXONOMY_PATTERN_CHARS = 512


def load_taxonomy(path: Path | None = None) -> TechnicalTaxonomy:
    """Load optional TOML additions without replacing the sector defaults."""

    taxonomy = builtin_taxonomy()
    if path is None:
        return taxonomy
    raw = _read_taxonomy_bytes(path)
    data = tomllib.loads(raw.decode("utf-8"))
    authorities = list(taxonomy.authorities)
    organizations = list(taxonomy.organizations)
    clients = list(taxonomy.clients)
    projects = list(taxonomy.projects)
    for item in _table_sequence(data.get("authorities"), "authorities"):
        code = _required_text(item, "code").upper()
        aliases = _text_sequence(item.get("aliases", ()), "aliases")
        patterns = _text_sequence(
            item.get("identifier_patterns", ()),
            "identifier_patterns",
            max_items=MAX_TAXONOMY_PATTERNS,
            max_chars=MAX_TAXONOMY_PATTERN_CHARS,
        )
        for pattern in patterns:
            _validate_identifier_pattern(pattern, code)
        authorities.append(AuthoritySpec(code, aliases or (code,), patterns))
    for item in _table_sequence(data.get("organizations"), "organizations"):
        name = _required_text(item, "name")
        aliases = _text_sequence(item.get("aliases", ()), "aliases")
        organizations.append(OrganizationSpec(name, aliases or (name,)))
    for item in _table_sequence(data.get("clients"), "clients"):
        name = _required_text(item, "name")
        aliases = _text_sequence(item.get("aliases", ()), "aliases")
        clients.append(ClientSpec(name, aliases or (name,)))
    for item in _table_sequence(data.get("projects"), "projects"):
        name = _required_text(item, "name")
        client = _required_text(item, "client")
        aliases = _text_sequence(item.get("aliases", ()), "aliases")
        projects.append(ProjectSpec(name, client, aliases or (name,)))
    digest = xxhash.xxh3_64_hexdigest(raw)
    return TechnicalTaxonomy(
        signature=f"{BUILTIN_TAXONOMY_VERSION}|custom-xxh3-64={digest}",
        authorities=_deduplicate_authorities(authorities),
        organizations=_deduplicate_organizations(organizations),
        clients=_deduplicate_clients(clients),
        projects=_deduplicate_projects(projects),
    )


def _read_taxonomy_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_TAXONOMY_BYTES + 1)
    if len(raw) > MAX_TAXONOMY_BYTES:
        raise ValueError(f"taxonomy file exceeds the {MAX_TAXONOMY_BYTES}-byte limit")
    return raw


def _table_sequence(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"taxonomy {name} must be an array of tables")
    if len(value) > MAX_TAXONOMY_TABLES_PER_SECTION:
        raise ValueError(
            f"taxonomy {name} exceeds the {MAX_TAXONOMY_TABLES_PER_SECTION}-table limit"
        )
    return tuple(value)


def _required_text(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"taxonomy field {name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > MAX_TAXONOMY_TEXT_CHARS:
        raise ValueError(
            f"taxonomy field {name} exceeds the {MAX_TAXONOMY_TEXT_CHARS}-character limit"
        )
    return normalized


def _text_sequence(
    value: Any,
    name: str,
    *,
    max_items: int = MAX_TAXONOMY_SEQUENCE_ITEMS,
    max_chars: int = MAX_TAXONOMY_TEXT_CHARS,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"taxonomy field {name} must be a text array")
    if len(value) > max_items:
        raise ValueError(f"taxonomy field {name} exceeds the {max_items}-item limit")
    normalized = tuple(dict.fromkeys(item.strip() for item in value))
    if any(len(item) > max_chars for item in normalized):
        raise ValueError(
            f"taxonomy field {name} contains text longer than {max_chars} characters"
        )
    return normalized


def _validate_identifier_pattern(pattern: str, authority_code: str) -> None:
    """Accept bounded regular expressions without high-risk repeated subexpressions."""

    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(
            f"invalid identifier pattern for authority {authority_code}: {exc}"
        ) from exc
    unsafe_reason = _unsafe_custom_regex_reason(pattern)
    if unsafe_reason is not None:
        raise ValueError(
            f"unsafe identifier pattern for authority {authority_code}: {unsafe_reason}"
        )


_BACKREFERENCE_REASON = "backreferences are not allowed"
_SPECIAL_GROUP_REASON = (
    "lookarounds, named groups, and inline extensions are not allowed"
)
_REPEATED_GROUP_REASON = (
    "a repeated group cannot itself contain repetition or alternation"
)


@dataclass(slots=True)
class _RegexGroupFrame:
    has_repetition: bool = False
    has_alternation: bool = False


class _CustomRegexSafetyScanner:
    __slots__ = ("_frames", "_in_character_class", "_index", "_pattern")

    def __init__(self, pattern: str) -> None:
        self._pattern = pattern
        self._frames = [_RegexGroupFrame()]
        self._in_character_class = False
        self._index = 0

    def unsafe_reason(self) -> str | None:
        while self._index < len(self._pattern):
            reason = self._consume_next()
            if reason is not None:
                return reason
        return None

    def _consume_next(self) -> str | None:
        character = self._pattern[self._index]
        if character == "\\":
            return self._consume_escape()
        if self._consume_character_class(character):
            return None
        return self._consume_structural_token(character)

    def _consume_escape(self) -> str | None:
        if (
            self._index + 1 < len(self._pattern)
            and self._pattern[self._index + 1] in "123456789"
        ):
            return _BACKREFERENCE_REASON
        self._index += 2
        return None

    def _consume_character_class(self, character: str) -> bool:
        if character == "[" and not self._in_character_class:
            self._in_character_class = True
        elif character == "]" and self._in_character_class:
            self._in_character_class = False
        elif not self._in_character_class:
            return False
        self._index += 1
        return True

    def _consume_structural_token(self, character: str) -> str | None:
        if character == "(":
            return self._open_group()
        if character == "|":
            self._frames[-1].has_alternation = True
            self._index += 1
            return None
        if character == ")" and len(self._frames) > 1:
            return self._close_group()
        self._consume_quantifier_or_literal()
        return None

    def _open_group(self) -> str | None:
        if self._pattern.startswith("(?:", self._index):
            self._index += 3
        elif self._pattern.startswith("(?", self._index):
            return _SPECIAL_GROUP_REASON
        else:
            self._index += 1
        self._frames.append(_RegexGroupFrame())
        return None

    def _close_group(self) -> str | None:
        frame = self._frames.pop()
        repeated, next_index = _regex_quantifier_end(
            self._pattern,
            self._index + 1,
        )
        if repeated and (frame.has_repetition or frame.has_alternation):
            return _REPEATED_GROUP_REASON
        parent = self._frames[-1]
        parent.has_repetition = (
            parent.has_repetition or frame.has_repetition or repeated
        )
        parent.has_alternation = parent.has_alternation or frame.has_alternation
        self._index = next_index if repeated else self._index + 1
        return None

    def _consume_quantifier_or_literal(self) -> None:
        repeated, next_index = _regex_quantifier_end(self._pattern, self._index)
        if repeated:
            self._frames[-1].has_repetition = True
            self._index = next_index
        else:
            self._index += 1


def _unsafe_custom_regex_reason(pattern: str) -> str | None:
    """Conservatively reject constructs commonly responsible for regex backtracking."""

    return _CustomRegexSafetyScanner(pattern).unsafe_reason()


def _regex_quantifier_end(pattern: str, index: int) -> tuple[bool, int]:
    if index >= len(pattern):
        return False, index
    if pattern[index] in "*+?":
        end = index + 1
        if end < len(pattern) and pattern[end] in "+?":
            end += 1
        return True, end
    if pattern[index] != "{":
        return False, index
    match = re.match(r"\{\d+(?:,\d*)?\}[+?]?", pattern[index:])
    if match is None:
        return False, index
    return True, index + len(match.group(0))


def _deduplicate_authorities(
    values: Iterable[AuthoritySpec],
) -> tuple[AuthoritySpec, ...]:
    merged: dict[str, AuthoritySpec] = {}
    for value in values:
        prior = merged.get(value.code.casefold())
        if prior is None:
            merged[value.code.casefold()] = value
            continue
        merged[value.code.casefold()] = AuthoritySpec(
            prior.code,
            tuple(dict.fromkeys((*prior.aliases, *value.aliases))),
            tuple(
                dict.fromkeys((*prior.identifier_patterns, *value.identifier_patterns))
            ),
        )
    return tuple(merged.values())


def _deduplicate_organizations(
    values: Iterable[OrganizationSpec],
) -> tuple[OrganizationSpec, ...]:
    merged: dict[str, OrganizationSpec] = {}
    for value in values:
        prior = merged.get(value.name.casefold())
        if prior is None:
            merged[value.name.casefold()] = value
            continue
        merged[value.name.casefold()] = OrganizationSpec(
            prior.name,
            tuple(dict.fromkeys((*prior.aliases, *value.aliases))),
        )
    return tuple(merged.values())


def _deduplicate_clients(values: Iterable[ClientSpec]) -> tuple[ClientSpec, ...]:
    merged: dict[str, ClientSpec] = {}
    for value in values:
        prior = merged.get(value.name.casefold())
        if prior is None:
            merged[value.name.casefold()] = value
            continue
        merged[value.name.casefold()] = ClientSpec(
            prior.name,
            tuple(dict.fromkeys((*prior.aliases, *value.aliases))),
        )
    return tuple(merged.values())


def _deduplicate_projects(values: Iterable[ProjectSpec]) -> tuple[ProjectSpec, ...]:
    merged: dict[str, ProjectSpec] = {}
    for value in values:
        key = value.name.casefold()
        prior = merged.get(key)
        if prior is None:
            merged[key] = value
            continue
        if prior.client.casefold() != value.client.casefold():
            raise ValueError(
                f"taxonomy project {value.name} has conflicting clients: "
                f"{prior.client} and {value.client}"
            )
        merged[key] = ProjectSpec(
            prior.name,
            prior.client,
            tuple(dict.fromkeys((*prior.aliases, *value.aliases))),
        )
    return tuple(merged.values())
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_overlay")
