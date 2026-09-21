"""CLI contract tests for the global ``-S`` file-size ceiling."""

from __future__ import annotations

import pytest

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser


def test_short_attached_and_separated_spellings_share_the_global_field() -> None:
    parser = build_parser()

    attached = parser.parse_args(["--all", "-S10"])
    separated = parser.parse_args(["--all", "-S", "10"])
    long = parser.parse_args(["--all", "--max-size-mb", "10"])

    assert attached.max_file_bytes == 10_000_000
    assert separated.max_file_bytes == attached.max_file_bytes
    assert long.max_file_bytes == attached.max_file_bytes
    expected_explicit_options = frozenset({"all", "max_file_bytes"})
    expected_explicit_counts = {"all": 1, "max_file_bytes": 1}
    assert attached._explicit_options == expected_explicit_options
    assert separated._explicit_options == expected_explicit_options
    assert long._explicit_options == expected_explicit_options
    assert attached._explicit_option_counts == expected_explicit_counts
    assert separated._explicit_option_counts == expected_explicit_counts
    assert long._explicit_option_counts == expected_explicit_counts

    attached_config = framework_config_from_args(attached)
    separated_config = framework_config_from_args(separated)
    long_config = framework_config_from_args(long)
    assert attached_config.max_file_bytes == 10_000_000
    assert separated_config.max_file_bytes == attached_config.max_file_bytes
    assert long_config.max_file_bytes == attached_config.max_file_bytes


def test_decimal_megabytes_are_converted_to_decimal_bytes() -> None:
    args = build_parser().parse_args(["--max-size-mb", "1.25"])
    assert args.max_file_bytes == 1_250_000


def test_size_limit_defaults_to_unlimited() -> None:
    args = build_parser().parse_args([])
    assert args.max_file_bytes is None
    assert framework_config_from_args(args).max_file_bytes is None


@pytest.mark.parametrize("arguments", (("-S",), ("--max-size-mb",)))
def test_size_limit_requires_a_value(arguments: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(list(arguments))
    assert raised.value.code == 2


@pytest.mark.parametrize("value", ("0", "-1", "NaN", "inf", "-inf"))
def test_size_limit_rejects_non_positive_or_non_finite_values(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["-S" + value])
    assert raised.value.code == 2


def test_help_exposes_only_the_canonical_global_size_spellings() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "-S MB, --max-size-mb MB" in help_text
    assert "--max-file-mb" not in help_text
    assert "--size" not in help_text
    assert "-M" not in parser._option_string_actions
