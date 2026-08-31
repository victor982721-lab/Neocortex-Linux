from __future__ import annotations

import os

import pytest

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


# region [01] Route-only selection


def _parse(*values: str):
    args = build_parser().parse_args(list(values))
    validate_arguments(args)
    return args


def test_route_only_selection_translates_to_explicit_contract(tmp_path) -> None:
    args = _parse(
        "--root",
        str(tmp_path),
        "--state-directory",
        str(tmp_path / "state"),
        "--route",
        "pdf",
        "--route-only",
        "--candidate-run",
        "7",
        "--select-status",
        "error",
        "--select-error-type",
        "PdfDocumentTimeout",
        "--select-recommendation",
        "retry",
        "--select-path",
        str(tmp_path / "one.pdf"),
        "--failed-pages-only",
    )
    config = framework_config_from_args(args)
    assert config.route_only is True
    assert config.candidate_run_id == 7
    assert config.selection.statuses == ("error",)
    assert config.selection.error_types == ("PdfDocumentTimeout",)
    assert config.selection.force_incomplete_retry is True
    assert config.selection.paths == (str((tmp_path / "one.pdf").absolute()),)


def test_selection_requires_route_only() -> None:
    args = build_parser().parse_args(["--route", "pdf", "--select-status", "error"])
    with pytest.raises(SystemExit, match="requires --route-only"):
        validate_arguments(args)


def test_route_only_is_non_destructive() -> None:
    args = build_parser().parse_args(["--route", "pdf", "--route-only", "--apply"])
    with pytest.raises(SystemExit, match="never executes file actions"):
        validate_arguments(args)


def test_all_apply_is_the_single_authorization_for_verified_actions() -> None:
    if os.name != "nt":
        args = _parse("--all", "--apply")
        config = framework_config_from_args(args)
        assert config.route == "all"
        assert config.apply_actions is True
        return
    args = _parse("--all", "--apply")
    config = framework_config_from_args(args)
    assert config.route == "all"
    assert config.apply_actions is True


# endregion [01]


# region [02] Status command


def test_status_options_are_read_only_and_bounded() -> None:
    args = _parse("--status", "--status-run", "12", "--status-limit", "20")
    assert args.status is True
    assert args.status_run == 12
    assert args.status_limit == 20

    invalid = build_parser().parse_args(["--status-json"])
    with pytest.raises(SystemExit, match="require --status"):
        validate_arguments(invalid)


# endregion [02]
