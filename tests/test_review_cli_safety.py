from __future__ import annotations

# region [01] Review command safety

import pytest

from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


@pytest.mark.parametrize("status", ("open", "resolved"))
def test_review_status_requires_review_command(status: str) -> None:
    args = build_parser().parse_args(["--review-status", status, "--apply"])

    with pytest.raises(SystemExit, match="review filters require"):
        validate_arguments(args)


def test_review_query_rejects_apply_even_with_limit() -> None:
    args = build_parser().parse_args(["--review-candidates", "10", "--apply"])

    with pytest.raises(SystemExit, match="read-only"):
        validate_arguments(args)


def test_review_query_without_apply_is_valid() -> None:
    args = build_parser().parse_args(
        [
            "--review-candidates",
            "10",
            "--review-status",
            "resolved",
            "--review-recommendation",
            "manual_review",
        ]
    )

    validate_arguments(args)


# endregion [01]
