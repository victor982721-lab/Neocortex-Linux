"""Deterministic bounded chunking at natural document boundaries."""

from __future__ import annotations
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Sequence

from neocortex.foundation.hash_compat import HASH_ALGORITHM_128, xxhash

from .semantic_models import (
    ContentFingerprint,
    TextChunk,
    TextSection,
    canonical_json,
    fingerprint_text,
)


# region [01] Configuration and explicit limits


_TERM = re.compile(r"\S+", re.UNICODE)
_SENTENCE_BREAK = re.compile(
    r"[.!?;:]\s+|"
    r"[\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH EXCLAMATION MARK}"
    r"\N{FULLWIDTH QUESTION MARK}]\s*",
    re.UNICODE,
)
_CHUNK_IDENTITY_VERSION = "semantic-text-chunk-identity-v2"


class ChunkLimitExceeded(RuntimeError):
    """Raised instead of silently truncating an unexpectedly large item."""


TextTokenCounter = Callable[[Sequence[str]], tuple[Sequence[int], int]]


@dataclass(frozen=True, slots=True)
class TextChunkingConfig:
    """Backend-independent limits for a natural text window.

    ``max_terms`` is a whitespace-token safety bound, not a claim about a
    model-specific tokenizer.  A backend may use a smaller configuration when
    its tokenizer has a lower context limit (for example CLIP text).
    """

    max_chars: int = 2_048
    max_terms: int = 384
    overlap_chars: int = 256
    overlap_terms: int = 48
    min_natural_break_chars: int = 160
    max_chunks_per_item: int = 100_000
    algorithm_version: str = "natural-window-v2"
    model_token_limit: int | None = None
    tokenizer_signature: str | None = None

    def __post_init__(self) -> None:
        if not 64 <= self.max_chars <= 1_000_000:
            raise ValueError("max_chars must be between 64 and 1000000")
        if not 1 <= self.max_terms <= 100_000:
            raise ValueError("max_terms must be between 1 and 100000")
        if not 0 <= self.overlap_chars < self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        if not 0 <= self.overlap_terms < self.max_terms:
            raise ValueError("overlap_terms must be smaller than max_terms")
        if not 1 <= self.min_natural_break_chars <= self.max_chars:
            raise ValueError("min_natural_break_chars is outside the chunk window")
        if not 1 <= self.max_chunks_per_item <= 10_000_000:
            raise ValueError("max_chunks_per_item is outside the supported range")
        if not self.algorithm_version.strip():
            raise ValueError("algorithm_version cannot be blank")
        if (self.model_token_limit is None) != (self.tokenizer_signature is None):
            raise ValueError("model_token_limit and tokenizer_signature must be set together")
        if self.model_token_limit is not None and (
            isinstance(self.model_token_limit, bool)
            or not isinstance(self.model_token_limit, int)
            or not 1 <= self.model_token_limit <= 1_000_000
        ):
            raise ValueError("model_token_limit must be between 1 and 1000000")
        if self.tokenizer_signature is not None and (
            not isinstance(self.tokenizer_signature, str) or not self.tokenizer_signature.strip()
        ):
            raise ValueError("tokenizer_signature cannot be blank")

    @property
    def signature(self) -> str:
        """Algorithm-explicit signature used for cache invalidation."""

        signature = (
            f"{self.algorithm_version}|chars={self.max_chars}|terms={self.max_terms}"
            f"|overlap-chars={self.overlap_chars}"
            f"|overlap-terms={self.overlap_terms}"
            f"|natural-min={self.min_natural_break_chars}"
            f"|max-chunks={self.max_chunks_per_item}"
        )
        if self.model_token_limit is not None:
            signature += (
                f"|model-token-limit={self.model_token_limit}|tokenizer={self.tokenizer_signature}"
            )
        return signature


# endregion [01]


# region [02] Bounded window selection


def normalize_embedding_text(text: str) -> str:
    """Collapse Unicode whitespace without changing case or diacritics."""

    return " ".join(text.split())


def _term_limited_end(text: str, start: int, hard_end: int, max_terms: int) -> int:
    term_count = 0
    for match in _TERM.finditer(text, start, hard_end):
        term_count += 1
        if term_count > max_terms:
            return match.start()
    return hard_end


def _natural_end(text: str, start: int, end: int, minimum_span: int) -> int:
    """Prefer the latest bounded paragraph, sentence, line or word boundary."""

    minimum = start + minimum_span
    if end <= minimum:
        return end
    window = text[start:end]
    candidates: list[int] = []
    paragraph = window.rfind("\n\n")
    if paragraph >= 0:
        candidates.append(start + paragraph + 2)
    sentence = None
    for match in _SENTENCE_BREAK.finditer(window):
        sentence = match.end()
    if sentence is not None:
        candidates.append(start + sentence)
    line = window.rfind("\n")
    if line >= 0:
        candidates.append(start + line + 1)
    word = window.rfind(" ")
    if word >= 0:
        candidates.append(start + word + 1)
    eligible = [candidate for candidate in candidates if minimum <= candidate <= end]
    return max(eligible, default=end)


def _next_start(
    text: str,
    start: int,
    end: int,
    config: TextChunkingConfig,
) -> int:
    if config.overlap_chars == 0 or config.overlap_terms == 0:
        return end
    starts: deque[int] = deque(maxlen=config.overlap_terms)
    for match in _TERM.finditer(text, start, end):
        starts.append(match.start())
    term_start = starts[0] if starts else end
    next_start = max(end - config.overlap_chars, term_start, start + 1)
    while next_start < end and text[next_start].isspace():
        next_start += 1
    return next_start


def _chunk_identifier(
    item_id: str,
    ordinal: int,
    section: TextSection,
    start: int,
    end: int,
    fingerprint: ContentFingerprint,
    chunking_signature: str,
) -> str:
    identity = canonical_json(
        {
            "schema": _CHUNK_IDENTITY_VERSION,
            "item_id": item_id,
            "ordinal": ordinal,
            "section_kind": section.section_kind,
            "section_id": section.section_id,
            "start_char": start,
            "end_char": end,
            "content_xxh3_128": fingerprint.xxh3_128,
            "content_bytes": fingerprint.byte_count,
            "content_xxh3_64_guard": fingerprint.xxh3_64_guard,
            "chunking_signature": chunking_signature,
            "provenance": section.provenance,
        }
    )
    return (
        f"chunk-{HASH_ALGORITHM_128}:"
        f"{xxhash.xxh3_128_hexdigest(identity.encode('utf-8'))}"
    )


def _exact_token_count(
    text: str,
    token_counter: TextTokenCounter,
    expected_token_limit: int,
) -> tuple[int, int]:
    counts, token_limit = _exact_token_counts((text,), token_counter, expected_token_limit)
    return counts[0], token_limit


def _exact_token_counts(
    texts: Sequence[str], token_counter: TextTokenCounter, expected_token_limit: int,
) -> tuple[tuple[int, ...], int]:
    counts, token_limit = token_counter(texts)
    if isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit < 1:
        raise RuntimeError("text tokenizer returned an invalid token limit")
    if len(counts) != len(texts):
        raise RuntimeError("text tokenizer returned an invalid result count")
    for token_count in counts:
        if isinstance(token_count, bool) or not isinstance(token_count, int):
            raise RuntimeError("text tokenizer returned a non-integer token count")
        if token_count < 0:
            raise RuntimeError("text tokenizer returned a negative token count")
    if token_limit != expected_token_limit:
        raise RuntimeError("text tokenizer limit changed during chunking")
    return tuple(counts), token_limit


def _fit_exact_token_budget(
    text: str,
    start: int,
    end: int,
    config: TextChunkingConfig,
    token_counter: TextTokenCounter,
    *,
    initial_count: int | None = None,
) -> tuple[int, str]:
    """Shrink one natural window until the production tokenizer accepts it."""

    while True:
        normalized = normalize_embedding_text(text[start:end])
        assert config.model_token_limit is not None
        if initial_count is None:
            token_count, token_limit = _exact_token_count(
                normalized, token_counter, config.model_token_limit,
            )
        else:
            token_count, token_limit = initial_count, config.model_token_limit
            initial_count = None
        if token_count <= token_limit:
            return end, normalized
        span = end - start
        if span <= 1:
            raise ChunkLimitExceeded("one source character exceeds the production tokenizer limit")

        # Exact token counts are not assumed to be monotonic under every BPE
        # vocabulary.  Reduce by at least five percent on each rejection and
        # retain a small token reserve, so this loop is bounded without ever
        # accepting truncation.
        token_reserve = max(4, token_limit // 20)
        target_tokens = max(1, token_limit - token_reserve)
        proportional_span = max(1, span * target_tokens // max(1, token_count))
        forced_reduction_span = max(1, span - max(1, span // 20))
        next_span = min(span - 1, proportional_span, forced_reduction_span)
        proposed_end = start + next_span
        natural_end = _natural_end(
            text,
            start,
            proposed_end,
            min(config.min_natural_break_chars, next_span),
        )
        end = natural_end if start < natural_end < end else proposed_end


def _window_end(source: str, cursor: int, config: TextChunkingConfig) -> int:
    hard_end = min(len(source), cursor + config.max_chars)
    end = _term_limited_end(source, cursor, hard_end, config.max_terms)
    if end < len(source):
        end = _natural_end(source, cursor, end, config.min_natural_break_chars)
    return end if end > cursor else hard_end


def _count_window_lookahead(
    source: str, cursor: int, config: TextChunkingConfig, counter: TextTokenCounter,
    *, max_windows: int = 32,
) -> dict[tuple[int, int], int]:
    """Batch exact counts, without assuming a rejected window's next offset.

    Only accepted windows can consume the speculative suffix.  The first
    rejection discards that suffix and resumes the original shrinking rule.
    Neither token approximations nor chunk identity changes are introduced.
    """

    windows: list[tuple[int, int]] = []
    texts: list[str] = []
    limit = max(1, min(max_windows, (2 * 1024 * 1024) // config.max_chars))
    while cursor < len(source) and len(windows) < limit:
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source):
            break
        end = _window_end(source, cursor, config)
        windows.append((cursor, end))
        texts.append(normalize_embedding_text(source[cursor:end]))
        if end >= len(source):
            break
        cursor = _next_start(source, cursor, end, config)
    assert config.model_token_limit is not None
    counts, _limit = _exact_token_counts(texts, counter, config.model_token_limit)
    return dict(zip(windows, counts, strict=True))


# endregion [02]


# region [03] Streaming public API


def iter_text_chunks(
    item_id: str,
    sections: Iterable[TextSection],
    config: TextChunkingConfig | None = None,
    *,
    token_counter: TextTokenCounter | None = None,
) -> Iterator[TextChunk]:
    """Yield bounded chunks without materializing an entire item-wide list."""

    if not item_id.strip():
        raise ValueError("item_id cannot be blank")
    active_config = config or TextChunkingConfig()
    exact_contract = active_config.model_token_limit is not None
    if exact_contract != (token_counter is not None):
        raise RuntimeError(
            "exact tokenizer counter and signed token contract must be used together"
        )
    ordinal = 0
    for section in sections:
        source = section.text
        cursor = 0
        exact_counts: dict[tuple[int, int], int] = {}
        lookahead = 32
        while cursor < len(source):
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
            if cursor >= len(source):
                break
            end = _window_end(source, cursor, active_config)
            if token_counter is None:
                normalized = normalize_embedding_text(source[cursor:end])
            else:
                if (cursor, end) not in exact_counts:
                    exact_counts = _count_window_lookahead(
                        source, cursor, active_config, token_counter,
                        max_windows=lookahead,
                    )
                original_end = end
                end, normalized = _fit_exact_token_budget(
                    source,
                    cursor,
                    end,
                    active_config,
                    token_counter,
                    initial_count=exact_counts.pop((cursor, end)),
                )
                if end != original_end:
                    exact_counts.clear()
                    # Dense/BPE-heavy text may reject every natural window.
                    # Stop counting unusable speculative suffixes repeatedly.
                    lookahead = 1
                else:
                    lookahead = min(32, lookahead * 2)
            if normalized:
                if ordinal >= active_config.max_chunks_per_item:
                    raise ChunkLimitExceeded(
                        f"item {item_id!r} exceeds {active_config.max_chunks_per_item} chunks"
                    )
                fingerprint = fingerprint_text(normalized)
                yield TextChunk(
                    chunk_id=_chunk_identifier(
                        item_id,
                        ordinal,
                        section,
                        cursor,
                        end,
                        fingerprint,
                        active_config.signature,
                    ),
                    item_id=item_id,
                    ordinal=ordinal,
                    section_kind=section.section_kind,
                    section_id=section.section_id,
                    start_char=cursor,
                    end_char=end,
                    text=normalized,
                    fingerprint=fingerprint,
                    chunking_signature=active_config.signature,
                    provenance=section.provenance,
                )
                ordinal += 1
            if end >= len(source):
                break
            cursor = _next_start(source, cursor, end, active_config)


def chunk_text_sections(
    item_id: str,
    sections: Iterable[TextSection],
    config: TextChunkingConfig | None = None,
    *,
    token_counter: TextTokenCounter | None = None,
) -> tuple[TextChunk, ...]:
    """Materialize chunks only under the configuration's explicit item cap."""

    return tuple(
        iter_text_chunks(
            item_id,
            sections,
            config,
            token_counter=token_counter,
        )
    )


# endregion [03]
