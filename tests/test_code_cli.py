"""CLI contracts for the user-facing Code knowledge capability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.api.cli import cli_code
from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _validated(*arguments: str):
    args = build_parser().parse_args(arguments)
    validate_arguments(args)
    return args


def test_code_route_configuration_is_product_only(tmp_path: Path) -> None:
    args = _validated(
        "--state-directory",
        str(tmp_path),
        "--route",
        "code",
        "--code-max-mb",
        "2",
        "--code-max-count",
        "7",
        "--code-cache-validation",
        "full",
        "--no-code-generated",
        "--no-code-vendored",
    )

    config = framework_config_from_args(args)

    assert config.route == "code"
    assert config.code_database == tmp_path / "code.sqlite3"
    assert config.code_max_file_bytes == 2_000_000
    assert config.code_max_documents == 7
    assert config.code_cache_validation == "full"
    assert config.code_candidate_scope == "projects"
    assert not config.code_include_generated
    assert not config.code_include_vendored
    assert not hasattr(config, "analysis_profile")
    assert not hasattr(config, "deep_test_selectors")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--code-search", " "), "--code-search must be non-empty"),
        (("--code-language", "python"), "code search filters require --code-search"),
        (
            ("--code-reconstruct-strategy", "branches"),
            "--code-reconstruct-strategy requires --code-reconstruct",
        ),
        (("--code-status", "--apply"), "direct code operations are read-only and reject --apply"),
        (("--code-projects", "--route", "code"), "direct code operations cannot be combined with --route"),
    ],
)
def test_code_direct_options_reject_ambiguous_or_unsafe_combinations(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--self-analysis",),
        ("--code-review",),
        ("--code-experiment-run", "proposal"),
        ("--deep-test-selector", "tests/test_code_cli.py"),
        ("--refresh-self-analysis",),
    ],
)
def test_retired_code_qa_options_are_not_cli_contracts(arguments: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)


def test_code_status_is_read_only_and_does_not_initialize_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _validated("--state-directory", str(tmp_path), "--code-status", "--code-json")
    assert dispatch_direct(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "code-status"
    assert payload["schema"] == "neocortex.code-status/v2"
    assert payload["state"] == "not_initialized"
    assert payload["exists"] is False
    assert payload["counts"] == {}
    assert payload["latest_run"] is None
    assert not (tmp_path / "code.sqlite3").exists()


def test_code_human_status_is_bounded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _validated("--state-directory", str(tmp_path), "--code-status")
    assert dispatch_direct(args) == 0
    output = capsys.readouterr().out
    assert output.startswith("CODE_STATUS state=not_initialized")
    assert "self_analysis" not in output
    assert "external" not in output


def test_cli_code_exports_only_product_operations() -> None:
    assert set(cli_code.__all__) == {
        "run_code_projects",
        "run_code_reconstruct",
        "run_code_search",
        "run_code_status",
    }
