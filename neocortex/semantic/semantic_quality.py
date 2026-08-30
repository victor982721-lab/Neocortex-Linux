"""Conservative quality gates for text submitted to semantic encoders.

The extraction caches remain the complete source of truth.  This module only
controls the rebuildable embedding projection, where signatures, spreadsheet
formula dumps and undecodable text otherwise become very strong false
neighbours for unrelated queries.
"""

from __future__ import annotations
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .semantic_chunking import TextChunkingConfig, TextTokenCounter, iter_text_chunks
from .semantic_models import TextChunk, TextSection


SEMANTIC_TEXT_QUALITY_POLICY = "semantic-text-quality-v1"
MAX_TITLE_SAMPLE_CHARS = 32_768

_TOKEN = re.compile(r"\S+", re.UNICODE)
_BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/=])")
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
_CELL_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])\$?[A-Z]{1,3}\$?\d{1,7}")
_FORMULA_MARKER = re.compile(
    r"(?i)(?:\b(?:IF|SUM|SQRT|VLOOKUP|HLOOKUP|INDEX|MATCH|COUNTIF|SUMIF)\s*\(|"
    r"(?:^|\s)[=+\-](?:\$?[A-Z]{1,3}\$?\d+|\())"
)
_MOJIBAKE_MARKER = re.compile(r"(?:�|Ã.|Â.|â€|ðŸ)")


@dataclass(frozen=True, slots=True)
class TextQualityAssessment:
    """Explain whether one already-bounded chunk is useful retrieval input."""

    eligible: bool
    reason: str
    alphabetic_ratio: float
    token_count: int


def assess_semantic_text(text: str, *, section_kind: str = "") -> TextQualityAssessment:
    """Reject only high-confidence machine noise; keep ambiguous human text."""

    value = text.strip()
    tokens = tuple(_TOKEN.findall(value))
    characters = max(1, len(value))
    alphabetic = sum(character.isalpha() for character in value)
    alphabetic_ratio = alphabetic / characters

    def assessment(eligible: bool, reason: str) -> TextQualityAssessment:
        return TextQualityAssessment(eligible, reason, alphabetic_ratio, len(tokens))

    if section_kind == "semantic_metadata_title":
        return assessment(bool(value), "title_metadata")
    if not value:
        return assessment(False, "empty")
    if len(value) < 3:
        return assessment(False, "too_short")
    if len(value) >= 32 and alphabetic_ratio < 0.08:
        return assessment(False, "low_alphabetic_content")
    if any(len(token) > 512 for token in tokens):
        return assessment(False, "oversized_token")

    mojibake = len(_MOJIBAKE_MARKER.findall(value))
    if mojibake >= 2 and mojibake / characters >= 0.005:
        return assessment(False, "mojibake")

    base64_tokens = sum(bool(_BASE64_TOKEN.fullmatch(token)) for token in tokens)
    if _BASE64_RUN.search(value) or (
        len(tokens) >= 3 and base64_tokens >= 2 and base64_tokens / len(tokens) >= 0.35
    ):
        return assessment(False, "encoded_binary_text")

    cell_references = len(_CELL_REFERENCE.findall(value))
    formula_markers = len(_FORMULA_MARKER.findall(value))
    if cell_references >= 8 and (
        formula_markers >= 2
        or cell_references / max(1, len(tokens)) >= 0.20
        or alphabetic_ratio < 0.42
    ):
        return assessment(False, "spreadsheet_formula_dump")

    # A repeated export can contain thousands of the same formula or field.
    # Require enough evidence before using this gate so lists and tables remain
    # searchable.
    if len(tokens) >= 40:
        normalized_tokens = {token.casefold() for token in tokens}
        if len(normalized_tokens) / len(tokens) <= 0.07:
            return assessment(False, "repetitive_machine_text")

    return assessment(True, "eligible")


def iter_semantic_text_chunks(
    item_id: str,
    sections: Iterable[TextSection],
    config: TextChunkingConfig,
    *,
    token_counter: TextTokenCounter | None = None,
) -> Iterator[TextChunk]:
    """Yield quality-gated chunks and collapse exact repeats within one item."""

    seen_content: set[tuple[str, str]] = set()
    for chunk in iter_text_chunks(
        item_id,
        sections,
        config,
        token_counter=token_counter,
    ):
        assessment = assess_semantic_text(
            chunk.text,
            section_kind=chunk.section_kind,
        )
        if not assessment.eligible:
            continue
        identity = (chunk.section_kind, chunk.fingerprint.xxh3_128)
        if chunk.section_kind != "semantic_metadata_title" and identity in seen_content:
            continue
        seen_content.add(identity)
        yield chunk


def bounded_title_sample(
    sections: Iterable[TextSection],
) -> tuple[Iterator[TextSection], list[str]]:
    """Return a streaming section wrapper and a bounded mutable title sample."""

    samples: list[str] = []

    def stream() -> Iterator[TextSection]:
        remaining = MAX_TITLE_SAMPLE_CHARS
        for section in sections:
            if remaining > 0 and section.text:
                fragment = section.text[:remaining]
                samples.append(fragment)
                remaining -= len(fragment)
            yield section

    return stream(), samples


def clean_title_candidate(value: str) -> str | None:
    """Return a short human heading, or ``None`` for boilerplate/noise."""

    title = " ".join(value.split()).strip(" -—_|:;,.\t")
    if not 5 <= len(title) <= 180:
        return None
    if any(unicodedata.category(character) == "Cc" for character in title):
        return None
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().casefold()
    if re.match(
        r"^(?:pagina|page|copyright|all rights reserved|archivo protegido recuperado|"
        r"documento protegido recuperado|hoja\d*|sheet\d*|elaboro|reviso|aprobo)\b",
        folded,
    ):
        return None
    if re.search(r"(?:@|https?://|www\.)", title, re.IGNORECASE):
        return None
    quality = assess_semantic_text(title)
    if not quality.eligible or quality.alphabetic_ratio < 0.45:
        return None
    words = re.findall(r"[^\W\d_]+", title, re.UNICODE)
    if len(words) < 2 or len(words) > 22:
        return None
    return title


def content_title_from_sample(sample: str) -> str | None:
    """Choose the first plausible heading from a bounded leading sample."""

    candidates: list[str] = []
    for line in sample.replace("\r", "\n").split("\n"):
        normalized = " ".join(line.split())
        if normalized:
            candidates.append(normalized)
        if len(candidates) >= 24:
            break
    if not candidates:
        # Some Office extractors flatten all runs.  A short initial sentence is
        # still more useful than a recovery-generated basename.
        candidates = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+", " ".join(sample.split()))[:6]
            if part.strip()
        ]
    for candidate in candidates:
        cleaned = clean_title_candidate(candidate)
        if cleaned is not None:
            return cleaned
    return None


__all__ = [
    "SEMANTIC_TEXT_QUALITY_POLICY",
    "TextQualityAssessment",
    "assess_semantic_text",
    "bounded_title_sample",
    "clean_title_candidate",
    "content_title_from_sample",
    "iter_semantic_text_chunks",
]
