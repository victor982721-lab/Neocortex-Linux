"""Repository contracts for the compact canonical Markdown set."""

from __future__ import annotations

import argparse
import html
import os
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from tools.export_profile import (
    MANIFEST_NAME, documentation_paths, validate_export_directory,
)

from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.human import build_human_parser


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The default is an explicit development-checkout contract. Sanitized exports
# opt in by name and must verify their immutable manifest before collection.
_DECLARED_PROFILE = os.environ.get("NEOCORTEX_DOCUMENTATION_PROFILE", "checkout")
_DOCUMENTS = documentation_paths(_DECLARED_PROFILE)
if _DECLARED_PROFILE == "sanitized":
    validate_export_directory(
        _PROJECT_ROOT, _PROJECT_ROOT / MANIFEST_NAME, expected_profile="sanitized",
        expected_commit=os.environ.get("NEOCORTEX_EXPORT_SOURCE_COMMIT"),
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
_CLI_FLAG = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")

# These options are intentionally outside the two parser builders below.  The
# UI entrypoint is handled before the integrated parser by
# ``neocortex.interface.entrypoint`` and must remain an explicit exception,
# not a wildcard that would hide obsolete documentation.
_ENTRYPOINT_FLAGS = frozenset({"--ui"})

# The installed help hides compatibility/configuration plumbing, while the
# canonical CLI document still shows it where it is needed for a reproducible
# invocation.  Keep this list exact so a newly hidden or stale option fails
# the contract instead of being silently accepted.
_DOCUMENTED_HIDDEN_FLAGS = frozenset(
    {
        "--doctor-platform",
        "--doctor-platform-json",
        "--models-json",
        "--models-model-id",
        "--models-prepare",
        "--models-root",
        "--models-status",
        "--state-directory",
    }
)

# The allowlist is deliberately limited to parser construction and help
# rendering.  No documented argv is parsed or dispatched here, so examples
# containing ``--apply``, ``--models-prepare`` or another mutating operation
# can never touch corpus or state during this contract test.
_READ_ONLY_HELP_BUILDERS = (build_parser, build_human_parser)


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


def _markdown_code_blocks(text: str) -> Iterator[str]:
    """Yield fenced blocks without interpreting prose as executable input."""

    fence: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        marker = line.lstrip()[:3]
        if fence is None:
            if marker in {"```", "~~~"}:
                fence = marker
                lines = []
            continue
        if marker == fence:
            yield "\n".join(lines)
            fence = None
            lines = []
            continue
        lines.append(line)


def _documented_cli_examples(path: Path) -> tuple[str, ...]:
    """Extract command examples for static inspection, never dispatch them."""

    examples: list[str] = []
    for block in _markdown_code_blocks(path.read_text(encoding="utf-8")):
        lines = block.splitlines()
        position = 0
        while position < len(lines):
            line = lines[position].strip()
            if not line.startswith("Neocortex"):
                position += 1
                continue
            parts = [line]
            while parts[-1].endswith("\\") and position + 1 < len(lines):
                parts[-1] = parts[-1][:-1].rstrip()
                position += 1
                parts.append(lines[position].strip())
            examples.append(" ".join(parts))
            position += 1
    return tuple(examples)


def _parser_options_and_help(parser: argparse.ArgumentParser) -> tuple[frozenset[str], str]:
    """Collect nested argparse options and their installed help text."""

    options: set[str] = set()
    help_text: list[str] = []
    visited: set[int] = set()

    def visit(current: argparse.ArgumentParser) -> None:
        identity = id(current)
        if identity in visited:
            return
        visited.add(identity)
        help_text.append(current.format_help())
        for action in current._actions:
            options.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict):
                continue
            for child in choices.values():
                if isinstance(child, argparse.ArgumentParser):
                    visit(child)

    visit(parser)
    return frozenset(options), "\n".join(help_text)


def test_documentation_inventory_is_exactly_the_canonical_set() -> None:
    assert _repository_markdown() == _DOCUMENTS
    for relative in _DOCUMENTS:
        path = _PROJECT_ROOT / relative
        assert path.is_file() and not path.is_symlink(), relative
    for relative in _RETIRED_DOCUMENTS:
        assert not (_PROJECT_ROOT / relative).exists(), relative


def test_canonical_documents_do_not_reference_retired_documents() -> None:
    unique_basenames = {
        Path(relative).name for relative in _RETIRED_DOCUMENTS if Path(relative).name != "README.md"
    }
    tokens = _RETIRED_DOCUMENTS | unique_basenames
    for relative in _DOCUMENTS:
        text = (_PROJECT_ROOT / relative).read_text(encoding="utf-8").casefold()
        stale = sorted(token for token in tokens if token.casefold() in text)
        assert stale == [], f"{relative} references retired documentation: {stale}"


def test_documented_cli_examples_match_installed_parser_help() -> None:
    """Detect retired flags without dispatching any documented command."""

    cli_document = _PROJECT_ROOT / "docs/CLI.md"
    examples = _documented_cli_examples(cli_document)
    assert examples, "docs/CLI.md must retain at least one CLI example"

    documented_flags = frozenset(
        flag for example in examples for flag in _CLI_FLAG.findall(example)
    )
    # Include inline option references too, so a stale flag cannot hide in
    # explanatory prose after its example is removed.
    documented_flags |= frozenset(_CLI_FLAG.findall(cli_document.read_text(encoding="utf-8")))

    contracts = tuple(_parser_options_and_help(builder()) for builder in _READ_ONLY_HELP_BUILDERS)
    installed_options = frozenset().union(*(options for options, _help in contracts))
    installed_help = "\n".join(help_text for _options, help_text in contracts)

    unknown = sorted(documented_flags - installed_options - _ENTRYPOINT_FLAGS)
    assert unknown == [], f"docs/CLI.md references obsolete CLI flags: {unknown}"

    not_in_help = sorted(
        documented_flags
        - set(_CLI_FLAG.findall(installed_help))
        - _DOCUMENTED_HIDDEN_FLAGS
        - _ENTRYPOINT_FLAGS
    )
    assert not_in_help == [], (
        f"documented flags are absent from build_parser/build_human_parser help: {not_in_help}"
    )

    undocumented_hidden = sorted(_DOCUMENTED_HIDDEN_FLAGS - installed_options)
    assert undocumented_hidden == [], (
        f"the hidden-help allowlist contains options no longer registered: {undocumented_hidden}"
    )


@pytest.mark.parametrize("relative", sorted(_DOCUMENTS))
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
