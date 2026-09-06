"""Public rendering fixes do not imply effects, complete extraction or disk savings."""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

from neocortex.api.cli.cli_knowledge_surface import validate_knowledge_arguments
from neocortex.api.cli.cli_semantic_surface import validate_semantic_arguments
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_reporting import (
    _human_bytes,
    _print_duplicate_groups,
    _professional_route_rows,
)


def test_binary_byte_units_are_iec() -> None:
    assert _human_bytes(22_898_590) == "21.8 MiB"
    assert _human_bytes(1024) == "1.0 KiB"
    assert _human_bytes(12) == "12 B"


def test_terminal_and_pipe_share_requested_duplicate_details() -> None:
    group = SimpleNamespace(
        keep=SimpleNamespace(path=Path('/fixture/Report.pdf')),
        redundant=(SimpleNamespace(path=Path('/fixture/Report (1).pdf')),),
        verification_mode='full_hash',
        proof=None,
    )
    result = SimpleNamespace(dedup_plan=SimpleNamespace(groups=(group,), group_count=3))
    raw: list[str] = []
    _print_duplicate_groups(result, 1, emit=raw.append)
    terminal = StringIO()
    console = Console(file=terminal, force_terminal=True, color_system=None, width=200)
    _print_duplicate_groups(
        result, 1, emit=lambda line: console.print(Text(line), soft_wrap=True),
    )
    assert terminal.getvalue().splitlines() == raw
    assert 'shown=1 total=3 limit=1 truncated=1' in raw[0]
    assert 'physical_reclaimable_bytes=not_verified' in raw[0]
    assert 'evidence=legacy_unknown' in raw[1]
    assert raw[2:] == ['KEEP /fixture/Report.pdf', 'CANDIDATE /fixture/Report (1).pdf']


def test_zero_group_limit_does_not_print_details() -> None:
    emitted: list[str] = []
    _print_duplicate_groups(SimpleNamespace(), 0, emit=emitted.append)
    assert emitted == []


def test_text_participates_in_professional_coverage() -> None:
    summary = SimpleNamespace(candidates=3, errors=1)
    assert _professional_route_rows(SimpleNamespace(text=summary)) == (('Texto', summary),)


def test_context_response_defaults_to_v2_and_v1_is_explicit() -> None:
    parser = build_parser()
    args = parser.parse_args(['--knowledge-context', 'radiadores sin presión'])
    assert args.knowledge_response_version == 2
    validate_knowledge_arguments(args)
    legacy = parser.parse_args([
        '--knowledge-context', 'radiadores', '--knowledge-response-version', '1',
    ])
    validate_knowledge_arguments(legacy)
    assert legacy.knowledge_response_version == 1


def test_response_version_is_only_valid_with_context() -> None:
    args = build_parser().parse_args([
        '--knowledge-search', 'radiadores', '--knowledge-response-version', '2',
    ])
    with pytest.raises(SystemExit, match='requires --knowledge-context'):
        validate_knowledge_arguments(args)


def test_target_diagnostics_are_bounded_and_search_only() -> None:
    parser = build_parser()
    args = parser.parse_args(['--semantic-status', '--semantic-diagnostic-item', 'known-item'])
    with pytest.raises(SystemExit, match='require --semantic-search'):
        validate_semantic_arguments(args)
