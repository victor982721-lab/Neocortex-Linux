"""Parser, validation, and dispatch contract for the unified Code query surface."""

from __future__ import annotations

import argparse
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from neocortex.api.cli.cli_operations import (
    DirectOperationFamily,
    selected_direct_operations,
)
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _validated(*arguments: str) -> argparse.Namespace:
    args = build_parser().parse_args(arguments)
    validate_arguments(args)
    return args


def test_code_query_parser_preserves_repeatable_filters_and_defaults() -> None:
    args = _validated(
        "--code-query",
        "review",
        "--code-query-provider",
        "git-history-local",
        "--code-query-provider",
        "cosmic-ray-focal-mutation",
        "--code-query-category",
        "mutation",
        "--code-query-module",
        "pkg.worker",
        "--code-query-status",
        "partial",
        "--code-query-delta",
        "added",
        "--code-query-work-package",
        "wp-7",
        "--code-json",
    )

    assert args.code_query == "review"
    assert args.code_query_provider == [
        "git-history-local",
        "cosmic-ray-focal-mutation",
    ]
    assert args.code_query_category == ["mutation"]
    assert args.code_query_module == ["pkg.worker"]
    assert args.code_query_status == ["partial"]
    assert args.code_query_delta == ["added"]
    assert args.code_query_work_package == ["wp-7"]
    assert args.code_query_limit == 50
    assert args.code_query_baseline is None
    assert args._explicit_options.issuperset(
        {
            "code_query",
            "code_query_provider",
            "code_query_category",
            "code_query_module",
            "code_query_status",
            "code_query_delta",
            "code_query_work_package",
            "code_json",
        }
    )


def test_code_query_diff_requires_and_accepts_only_its_baseline() -> None:
    args = _validated(
        "--code-query",
        "diff",
        "--code-query-baseline",
        "C:/baseline/state",
        "--code-query-limit",
        "500",
        "--code-json",
    )

    assert args.code_query_baseline == "C:/baseline/state"
    assert args.code_query_limit == 500


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--code-query-provider", "git-history-local"),
            "code query filters, limit and baseline require --code-query",
        ),
        (
            ("--code-query-limit", "20"),
            "code query filters, limit and baseline require --code-query",
        ),
        (
            ("--code-query-baseline", "C:/baseline"),
            "code query filters, limit and baseline require --code-query",
        ),
        (
            ("--code-query", "diff"),
            "--code-query diff requires --code-query-baseline",
        ),
        (
            ("--code-query", "status", "--code-query-baseline", "C:/baseline"),
            "--code-query-baseline is only valid with --code-query diff",
        ),
        (
            ("--code-query", "review", "--code-query-baseline", "C:/baseline"),
            "--code-query-baseline is only valid with --code-query diff",
        ),
        (
            ("--code-query", "review", "--code-query-limit", "0"),
            "--code-query-limit must be between 1 and 500",
        ),
        (
            ("--code-query", "status", "--code-query-limit", "501"),
            "--code-query-limit must be between 1 and 500",
        ),
        (
            ("--code-query", "status", "--code-status"),
            "--code-query cannot be combined with another direct code operation",
        ),
        (
            ("--code-query", "status", "--apply"),
            "direct code operations are read-only and reject --apply",
        ),
        (
            ("--code-query", "status", "--route", "code"),
            "direct code operations cannot be combined with --route",
        ),
    ),
)
def test_code_query_validation_is_bounded_and_fail_closed(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message


def test_code_query_is_one_lazy_code_direct_operation_and_allows_json() -> None:
    args = _validated("--code-query", "status", "--code-json")
    selected = selected_direct_operations(args, family=DirectOperationFamily.CODE)

    assert tuple(operation.destination for operation in selected) == ("code_query",)
    operation = selected[0]
    assert operation.handler_name == "run_code_query"
    assert operation.module_name == ".cli_code"

    handler = Mock(return_value=23)
    module = ModuleType("neocortex.api.cli.cli_code")
    module.run_code_query = handler  # type: ignore[attr-defined]
    with patch(
        "neocortex.api.cli.cli_operations.importlib.import_module",
        return_value=module,
    ):
        assert operation.dispatch(args) == 23
    handler.assert_called_once_with(args)


def test_previous_code_direct_operation_precedence_is_unchanged() -> None:
    destinations = tuple(
        operation.destination
        for operation in selected_direct_operations(
            build_parser().parse_args(
                (
                    "--code-status",
                    "--code-review",
                    "--code-publication-diff",
                    "C:/baseline",
                    "--code-search",
                    "breaker",
                    "--code-projects",
                    "--code-reconstruct",
                    "project",
                    "--code-doctor",
                    "--code-query",
                    "status",
                )
            ),
            family=DirectOperationFamily.CODE,
        )
    )

    assert destinations == (
        "code_status",
        "code_review",
        "code_publication_diff",
        "code_search",
        "code_projects",
        "code_reconstruct",
        "code_doctor",
        "code_query",
    )
