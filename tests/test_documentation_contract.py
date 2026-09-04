"""Repository contracts for the compact canonical Markdown set."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ACTIVE_DOCUMENTS = frozenset(
    {
        "README.md",
        "docs/ARCHITECTURE.md",
        "docs/CHANGELOG.md",
        "docs/CLI.md",
        "docs/FILE_INTELLIGENCE_AND_CURATION.md",
        "docs/KNOWLEDGE.md",
        "docs/LINUX_KUBUNTU.md",
        "docs/OPERATIONS.md",
        "docs/PERSISTENCE.md",
        "docs/RECOVERY.md",
        "docs/ROADMAP_90_DAYS.md",
        "docs/SECURITY.md",
    }
)
_SOURCE_ONLY_DOCUMENTS = frozenset(
    {
        ".codex/handoffs/NEOCORTEX_0.11.1_RECOVERY_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.11.0_APPLY_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.10.0_CURATION_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.9.0_CURATION_2026-09-04.md",
        ".codex/handoffs/NEOCORTEX_0.7.2_PAUSE_2026-07-30.md",
        "AGENTS.md",
    }
)
_RETIRED_DOCUMENTS = frozenset(
    {
        "NeoCortex_AGENTS.md",
        "docs/AUDIT_REPORTING_STANDARD.md",
        "docs/CODE_SUBSYSTEM_CLASSIFICATION.md",
        "docs/CURATION_AUDIT_2026-08-30.md",
        "docs/KNOWLEDGE_EVOLUTION_2026-07-26_010033.md",
        "docs/OFFLINE_INSTALLATION.md",
        "docs/SELF_ANALYSIS.md",
        "docs/SELF_ANALYSIS_PROGRAM_REPORT_2026-08-03.md",
        "docs/TECHNICAL_AUDIT_2026-07-24.md",
        "docs/TECHNICAL_AUDIT_2026-07-24_204343.md",
        "docs/TECHNICAL_AUDIT_2026-07-25_012323.md",
        "docs/TECHNICAL_AUDIT_2026-07-25_102929.md",
        "docs/TECHNICAL_AUDIT_2026-07-25_172113.md",
        "docs/TECHNICAL_EVOLUTION_2026-07-26_173000.md",
        "docs/TECHNICAL_EVOLUTION_HANDOFF_2026-07-29_082142.md",
        "docs/THIRD_PARTY_LICENSE_INVENTORY.md",
        "neocortex/deduplication/README.md",
        "neocortex/enumeration/README.md",
        "neocortex/progress/README.md",
    }
)
_INLINE_LINK = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)"
)
_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")
_HTML_LINK = re.compile(r"\b(?:href|src)=[\"'](?P<target>[^\"']+)[\"']", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_EXPLICIT_ANCHOR = re.compile(r"\b(?:id|name)=[\"'](?P<anchor>[^\"']+)[\"']", re.IGNORECASE)


def _repository_markdown() -> frozenset[str]:
    paths = set(_PROJECT_ROOT.glob("*.md"))
    paths.update((_PROJECT_ROOT / "docs").rglob("*.md"))
    paths.update((_PROJECT_ROOT / "neocortex").rglob("*.md"))
    paths.update((_PROJECT_ROOT / ".codex" / "handoffs").glob("*.md"))
    return frozenset(path.relative_to(_PROJECT_ROOT).as_posix() for path in paths)


def _prose_lines(text: str) -> Iterator[tuple[int, str]]:
    fence: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            yield line_number, line


def _slug(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", "", value)).casefold()
    value = re.sub(r"[`*_~]", "", value)
    characters = (
        character
        for character in unicodedata.normalize("NFC", value)
        if character.isalnum() or character in {" ", "-", "_"}
    )
    return re.sub(r"\s+", "-", "".join(characters).strip())


def _anchors(path: Path) -> frozenset[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _line_number, line in _prose_lines(path.read_text(encoding="utf-8")):
        anchors.update(match.group("anchor") for match in _EXPLICIT_ANCHOR.finditer(line))
        heading = _HEADING.match(line)
        if heading is None:
            continue
        base = _slug(heading.group("title"))
        if not base:
            continue
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return frozenset(anchors)


def _local_links(path: Path) -> Iterator[tuple[int, str]]:
    for line_number, line in _prose_lines(path.read_text(encoding="utf-8")):
        for pattern in (_INLINE_LINK, _HTML_LINK):
            for match in pattern.finditer(line):
                yield line_number, match.group("target").strip("<>")
        reference = _REFERENCE_LINK.match(line)
        if reference is not None:
            yield line_number, reference.group("target").strip("<>")


def test_documentation_inventory_is_exactly_the_canonical_set() -> None:
    assert _repository_markdown() == _ACTIVE_DOCUMENTS | _SOURCE_ONLY_DOCUMENTS
    for relative in _ACTIVE_DOCUMENTS | _SOURCE_ONLY_DOCUMENTS:
        path = _PROJECT_ROOT / relative
        assert path.is_file() and not path.is_symlink(), relative
    for relative in _RETIRED_DOCUMENTS:
        assert not (_PROJECT_ROOT / relative).exists(), relative


def test_canonical_documents_do_not_reference_retired_documents() -> None:
    unique_basenames = {
        Path(relative).name
        for relative in _RETIRED_DOCUMENTS
        if Path(relative).name != "README.md"
    }
    tokens = _RETIRED_DOCUMENTS | unique_basenames
    for relative in _ACTIVE_DOCUMENTS | _SOURCE_ONLY_DOCUMENTS:
        text = (_PROJECT_ROOT / relative).read_text(encoding="utf-8").casefold()
        stale = sorted(token for token in tokens if token.casefold() in text)
        assert stale == [], f"{relative} references retired documentation: {stale}"


@pytest.mark.parametrize("relative", sorted(_ACTIVE_DOCUMENTS | _SOURCE_ONLY_DOCUMENTS))
def test_local_documentation_links_and_anchors_resolve(relative: str) -> None:
    source = _PROJECT_ROOT / relative
    for line_number, raw_target in _local_links(source):
        target = urlsplit(raw_target)
        if target.scheme or target.netloc or raw_target.startswith("//"):
            continue
        relative_target = unquote(target.path)
        destination = source if not relative_target else source.parent / relative_target
        destination = destination.resolve(strict=False)
        try:
            destination.relative_to(_PROJECT_ROOT)
        except ValueError as exc:
            raise AssertionError(
                f"{relative}:{line_number} escapes the repository: {raw_target}"
            ) from exc
        assert destination.is_file(), f"{relative}:{line_number} missing link: {raw_target}"
        if target.fragment and destination.suffix.casefold() == ".md":
            anchor = unquote(target.fragment).casefold()
            assert anchor in _anchors(destination), (
                f"{relative}:{line_number} missing anchor {target.fragment!r} in "
                f"{destination.relative_to(_PROJECT_ROOT).as_posix()}"
            )
