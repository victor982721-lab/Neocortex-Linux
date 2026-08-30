"""Bounded normalization and regular-expression helpers for document signals."""

from __future__ import annotations
import re
import unicodedata
from functools import lru_cache
from itertools import islice
from typing import Iterator


# region [01] Stable path and text signals

_MANAGED_ROOT_KEY = "consulta tecnica organizada"


def fold_signal(value: str) -> str:
    """Normalize one bounded classifier signal without changing word order."""

    decomposed = unicodedata.normalize("NFKD", value.replace("_", " "))
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_marks).upper()


def is_framework_managed_path(path: str) -> bool:
    """Return whether a path is below the framework's organization root."""

    return any(
        _path_segment_key(segment) == _MANAGED_ROOT_KEY
        for segment in re.split(r"[\\/]", path)
    )


def classification_path_signal(path: str) -> str:
    """Exclude managed category directories that would reinforce prior labels."""

    if not is_framework_managed_path(path):
        return path
    return re.split(r"[\\/]", path)[-1]


def _path_segment_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.replace("_", " "))
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_marks).strip().casefold()


# endregion [01]


# region [02] Bounded compiled-regex cache

REGEX_CACHE_SIZE = 4_096


@lru_cache(maxsize=REGEX_CACHE_SIZE)
def compiled_regex(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile stable and custom rules once with a bounded process cache."""

    return re.compile(pattern, flags)


def first_regex_matches(
    pattern: str,
    text: str,
    *,
    flags: int = 0,
    limit: int = 2,
) -> tuple[re.Match[str], ...]:
    """Return only the evidence matches a caller can persist."""

    if limit < 1:
        return ()
    matches: Iterator[re.Match[str]] = compiled_regex(pattern, flags).finditer(text)
    return tuple(islice(matches, limit))


@lru_cache(maxsize=REGEX_CACHE_SIZE)
def _compiled_rule_set(patterns: tuple[str, ...], flags: int) -> re.Pattern[str]:
    combined = "|".join(f"(?:{pattern})" for pattern in patterns)
    return re.compile(combined, flags)


def any_regex_match(
    patterns: tuple[str, ...],
    text: str,
    *,
    flags: int = 0,
) -> bool:
    """Reject absent rule families with one search before evaluating each rule."""

    return bool(patterns and _compiled_rule_set(patterns, flags).search(text))


@lru_cache(maxsize=REGEX_CACHE_SIZE)
def _compiled_alias(folded_alias: str) -> re.Pattern[str]:
    expression = re.escape(folded_alias).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){expression}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=REGEX_CACHE_SIZE)
def _folded_alias(alias: str) -> str:
    return fold_signal(alias)


def alias_count(text: str, alias: str) -> int:
    """Count a present alias while avoiding regex work for absent aliases."""

    folded_alias = _folded_alias(alias)
    if not folded_alias or folded_alias not in text:
        return 0
    return sum(1 for _match in _compiled_alias(folded_alias).finditer(text))


# endregion [02]
