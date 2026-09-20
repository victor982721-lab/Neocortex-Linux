"""Exact-syntax recognition primitives for deterministic knowledge planning."""
# region [00] Contexto del módulo
# Módulo: neocortex/knowledge_planner_exact.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations
import re
# endregion [01]

# region [02] Implementación


_PATH_PREFIX = (
    r"(?:[A-Za-z]:[\\/]|\\\\|//|\.{1,2}[\\/]|[\\/]"
    r"|(?:[^\s\\/:*?\"<>|,;()\[\]]+[\\/])+)"
)
_PATH = re.compile(
    rf'"(?P<quoted>{_PATH_PREFIX}[^"\r\n]+)"'
    rf"|(?P<file>(?<![A-Za-z0-9:/\\]){_PATH_PREFIX}[^\r\n\"',]*?"
    r"\.[A-Za-z0-9]{1,16}(?:\.[A-Za-z0-9]{1,16})*)"
    r"(?=\s|[,;:!?\)\]]|[.](?:\s|$)|$)"
    rf"|(?P<bare>(?<![A-Za-z0-9:/\\]){_PATH_PREFIX}"
    r"[^\s\r\n\"',;!?()]+)"
)
_QUOTED_FILE_NAME = re.compile(
    r'"(?P<double>[^"\\/\r\n]+?\.[A-Za-z0-9]{1,16})"'
    r"|'(?P<single>[^'\\/\r\n]+?\.[A-Za-z0-9]{1,16})'"
)
_SIMPLE_FILE_NAME = re.compile(
    r"(?<![\w%#.-])(?P<name>[^\s\\/:*?\"<>|,;()\[\]]+?"
    r"\.[A-Za-z0-9]{1,16})(?=\s|[,;:!?\)\].]|$)"
)
_FULL_FILE_NAME = re.compile(
    r"\s*(?P<name>[^\\/:*?\"<>|\r\n,;()\[\]]+?"
    r"\.[A-Za-z0-9]{1,16})[.,:;!?\)\]]?\s*"
)
_FILE_NAME_QUERY_LEADER = re.compile(
    r"^(?:abre|archivo|busca|buscar|file|find|name|nombre|open)\s+",
    re.IGNORECASE,
)
_PATH_QUERY_CUE = re.compile(
    r"(?:archivo|directorio|file|folder|path|ruta)\s+$",
    re.IGNORECASE,
)
_EXPLICIT_PATH_PREFIX = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|\.\.?[\\/]|[\\/])")
_RELATIVE_PATH_ROOTS = frozenset(
    {
        "app",
        "apps",
        "assets",
        "bin",
        "build",
        "config",
        "data",
        "dist",
        "doc",
        "docs",
        "folder",
        "include",
        "lib",
        "modules",
        "packages",
        "scripts",
        "src",
        "test",
        "tests",
    }
)
SERIAL_PATTERN = re.compile(
    r"\b(?:(?:SN|S/N)(?:[\s:#-]+|(?=\d))|SERIAL[\s:#-]+)"
    r"[A-Z0-9][A-Z0-9._/-]*(?<![._/-])(?![A-Z0-9._/-])",
    re.IGNORECASE,
)
HASH_PATTERN = re.compile(r"\b[0-9a-fA-F]{16,64}\b")
NUMBERED_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_.]*(?:[-_/][A-Za-z0-9_.]+)*[-_/][0-9][A-Za-z0-9_.-]*\b"
)
RELATIONAL_WORDS = frozenset(
    {
        "belong",
        "belongs",
        "relación",
        "relationship",
        "depende",
        "depends",
        "pertenece",
        "pertenecen",
        "usa",
        "uses",
    }
)
TEMPORAL_WORDS = frozenset(
    {
        "antes",
        "after",
        "before",
        "después",
        "histórico",
        "historical",
        "versión",
        "version",
    }
)
TEMPORAL_YEAR_WORDS = frozenset(
    {
        "año",
        "date",
        "desde",
        "durante",
        "en",
        "fecha",
        "from",
        "in",
        "on",
        "since",
        "year",
    }
)
_PLAUSIBLE_YEAR = re.compile(r"(?<!\d)(?:19\d{2}|20\d{2}|2100)(?!\d)")
_TECHNICAL_UNIT_AFTER_YEAR = re.compile(r"[ \t]*(?:A|kV|V|Hz|mm)\b")

_Candidate = tuple[int, int, int, str, bool]
_Surface = tuple[str, int, int]


def token_words(text: str) -> frozenset[str]:
    return frozenset(
        token.casefold() for token in re.findall(r"[^\W_]+", text, re.UNICODE)
    )


def _path_value(match: re.Match[str]) -> str:
    quoted = match.group("quoted")
    if quoted is not None:
        return quoted.strip()
    value = next(
        group
        for group in (
            match.group("file"),
            match.group("bare"),
        )
        if group is not None
    )
    return value.strip().rstrip(".,:;!?)]")


def _path_match_is_serial(text: str, match: re.Match[str]) -> bool:
    value = _path_value(match)
    if SERIAL_PATTERN.fullmatch(value) is not None:
        return True
    return (
        value.casefold() == "s/n"
        and re.match(
            r"[\s:#-]+[A-Z0-9]",
            text[match.end() :],
            re.IGNORECASE,
        )
        is not None
    )


def _path_match_is_plausible(text: str, match: re.Match[str]) -> bool:
    if _path_match_is_serial(text, match):
        return False
    if match.group("quoted") is not None or match.group("file") is not None:
        return True
    value = _path_value(match)
    if _EXPLICIT_PATH_PREFIX.match(value) is not None:
        return True
    first_segment = re.split(r"[\\/]", value, maxsplit=1)[0].casefold()
    if first_segment in _RELATIVE_PATH_ROOTS:
        return True
    prefix = text[max(0, match.start() - 32) : match.start()]
    return _PATH_QUERY_CUE.search(prefix) is not None


def _masked_match_text(text: str, matches: tuple[re.Match[str], ...]) -> str:
    characters = list(text)
    for match in matches:
        characters[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(characters)


def _masked_span_text(
    text: str,
    spans: tuple[tuple[int, int], ...],
) -> str:
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _quoted_file_surfaces(
    text: str,
) -> tuple[list[_Surface], tuple[re.Match[str], ...]]:
    surfaces: list[_Surface] = []
    matches = tuple(_QUOTED_FILE_NAME.finditer(text))
    for match in matches:
        value = match.group("double") or match.group("single")
        if value is None:
            raise AssertionError("quoted filename match has no value")
        surfaces.append((value.strip(), match.start(), match.end()))
    return surfaces, matches


def _is_full_file_name_candidate(value: str, leader: re.Match[str] | None) -> bool:
    first_word = value.split(maxsplit=1)[0].casefold()
    cue_words = (
        RELATIONAL_WORDS
        | TEMPORAL_WORDS
        | frozenset({"hash", "identifier", "identificador", "s/n", "serial", "sn"})
    )
    if first_word in cue_words:
        return False
    if any(
        pattern.search(value) is not None
        for pattern in (SERIAL_PATTERN, HASH_PATTERN, NUMBERED_IDENTIFIER_PATTERN)
    ):
        return False
    if leader is not None or len(value.split()) <= 4:
        return True
    return any(mark in value for mark in "%_#")


def _full_file_name_surface(
    text: str,
    simple_matches: tuple[re.Match[str], ...],
) -> _Surface | None:
    full_match = _FULL_FILE_NAME.fullmatch(text)
    if full_match is None or len(simple_matches) != 1:
        return None
    value = full_match.group("name").strip()
    start = full_match.start("name")
    leader = _FILE_NAME_QUERY_LEADER.match(value)
    if leader is not None:
        start += leader.end()
        value = value[leader.end() :].strip()
    if not _is_full_file_name_candidate(value, leader):
        return None
    return value, start, start + len(value)


def _file_name_surfaces(text: str) -> tuple[_Surface, ...]:
    surfaces, quoted_matches = _quoted_file_surfaces(text)
    unquoted_text = _masked_span_text(
        text,
        tuple((match.start(), match.end()) for match in quoted_matches),
    )
    simple_matches = tuple(_SIMPLE_FILE_NAME.finditer(unquoted_text))
    full_surface = _full_file_name_surface(unquoted_text, simple_matches)
    if full_surface is not None:
        surfaces.append(full_surface)
        return tuple(sorted(surfaces, key=lambda item: (item[1], item[2])))
    surfaces.extend(
        (match.group("name"), match.start("name"), match.end("name"))
        for match in simple_matches
    )
    return tuple(sorted(surfaces, key=lambda item: (item[1], item[2])))


def has_temporal_year(text: str) -> bool:
    for match in _PLAUSIBLE_YEAR.finditer(text):
        if _TECHNICAL_UNIT_AFTER_YEAR.match(text, match.end()) is None:
            return True
    return False


def _path_candidates(text: str) -> tuple[list[_Candidate], tuple[re.Match[str], ...]]:
    path_matches = tuple(
        match for match in _PATH.finditer(text) if _path_match_is_plausible(text, match)
    )
    candidates = [
        (match.start(), match.end(), 0, _path_value(match), False)
        for match in path_matches
    ]
    return candidates, path_matches


def _pattern_candidates(
    text: str,
) -> tuple[list[_Candidate], tuple[re.Match[str], ...]]:
    candidates: list[_Candidate] = []
    temporal_matches: list[re.Match[str]] = []
    for priority, pattern, masks_temporal_year in (
        (2, SERIAL_PATTERN, True),
        (4, HASH_PATTERN, True),
        (5, NUMBERED_IDENTIFIER_PATTERN, True),
    ):
        for match in pattern.finditer(text):
            candidates.append(
                (
                    match.start(),
                    match.end(),
                    priority,
                    match.group(0).strip(),
                    False,
                )
            )
            if masks_temporal_year:
                temporal_matches.append(match)
    return candidates, tuple(temporal_matches)


def _accepted_candidates(candidates: list[_Candidate]) -> tuple[tuple[str, ...], bool]:
    values: list[str] = []
    accepted_spans: list[tuple[int, int]] = []
    for start, end, _, value, _is_symbol in sorted(
        candidates,
        key=lambda item: (item[0], item[2], -item[1]),
    ):
        if any(
            accepted_start <= start and end <= accepted_end
            for accepted_start, accepted_end in accepted_spans
        ):
            continue
        if value not in values:
            values.append(value)
        accepted_spans.append((start, end))
    return tuple(values), False


def extract_exact_terms(
    text: str,
) -> tuple[tuple[str, ...], bool, bool, bool, str, str]:
    candidates, path_matches = _path_candidates(text)
    non_path_text = _masked_match_text(text, path_matches)
    file_names = _file_name_surfaces(non_path_text)
    candidates.extend((start, end, 1, value, False) for value, start, end in file_names)
    non_name_text = _masked_span_text(
        non_path_text,
        tuple((start, end) for _, start, end in file_names),
    )
    pattern_candidates, temporal_matches = _pattern_candidates(non_name_text)
    candidates.extend(pattern_candidates)
    values, _ = _accepted_candidates(candidates)
    temporal_text = _masked_match_text(non_name_text, temporal_matches)
    return (
        values,
        bool(path_matches),
        bool(file_names),
        False,
        non_name_text,
        temporal_text,
    )


__all__ = (
    "HASH_PATTERN",
    "NUMBERED_IDENTIFIER_PATTERN",
    "RELATIONAL_WORDS",
    "SERIAL_PATTERN",
    "TEMPORAL_WORDS",
    "TEMPORAL_YEAR_WORDS",
    "extract_exact_terms",
    "has_temporal_year",
    "token_words",
)
# endregion [02]
