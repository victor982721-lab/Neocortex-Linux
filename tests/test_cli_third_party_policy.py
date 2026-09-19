"""CLI/configuration contract for explicit third-party Code cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.runtime.config.third_party_policy import (
    DEFAULT_THIRD_PARTY_KINDS,
    CodeThirdPartyPolicy,
)


def _validated(*arguments: str):
    args = build_parser().parse_args(arguments)
    validate_arguments(args)
    return args


def test_integrated_all_keeps_code_policy_inert(tmp_path: Path) -> None:
    args = _validated(
        "--root",
        str(tmp_path),
        "--state-directory",
        str(tmp_path / "state"),
        "--all",
    )

    policy = framework_config_from_args(args).code_third_party_policy

    assert isinstance(policy, CodeThirdPartyPolicy)
    assert policy.action == "keep"
    assert policy.kinds == DEFAULT_THIRD_PARTY_KINDS
    assert policy.min_confidence == pytest.approx(0.95)
    assert policy.max_actions == 256
    assert policy.mutation_requested is False


def test_direct_code_defaults_to_keep_for_internal_callers(tmp_path: Path) -> None:
    args = _validated(
        "--root",
        str(tmp_path),
        "--route",
        "code",
        "--code-scope",
        "broad",
    )

    policy = framework_config_from_args(args).code_third_party_policy
    assert policy.action == "keep"
    assert policy.mutation_requested is False


def test_third_party_trash_requires_explicit_code_route(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()

    preview = build_parser().parse_args(
        [
            "--root",
            str(root),
            "--all",
            "--code-project-root",
            str(root / "owned"),
            "--code-third-party-action",
            "trash",
        ]
    )
    with pytest.raises(SystemExit, match="requires --route code"):
        validate_arguments(preview)

    no_code_route = build_parser().parse_args(
        [
            "--root",
            str(root),
            "--route",
            "pdf",
            "--apply",
            "--code-third-party-action",
            "trash",
        ]
    )
    with pytest.raises(SystemExit, match="requires --route code"):
        validate_arguments(no_code_route)

    no_owned_root = build_parser().parse_args(
        ["--all", "--apply", "--code-third-party-action", "trash"]
    )
    with pytest.raises(SystemExit, match="requires --route code"):
        validate_arguments(no_owned_root)

    no_project_allowlist = build_parser().parse_args(
        [
            "--root",
            str(root),
            "--all",
            "--apply",
            "--code-third-party-action",
            "trash",
        ]
    )
    with pytest.raises(SystemExit, match="requires --route code"):
        validate_arguments(no_project_allowlist)

    disjoint_project_root = build_parser().parse_args(
        [
            "--root",
            str(root),
            "--all",
            "--code-project-root",
            str(tmp_path / "outside"),
        ]
    )
    validate_arguments(disjoint_project_root)
    assert framework_config_from_args(disjoint_project_root).code_third_party_policy.action == "keep"


def test_third_party_trash_policy_is_explicit_and_bounded(tmp_path: Path) -> None:
    args = _validated(
        "--root",
        str(tmp_path),
        "--route",
        "code",
        "--apply",
        "--code-project-root",
        str(tmp_path / "owned-project"),
        "--code-third-party-action",
        "trash",
        "--code-third-party-min-confidence",
        "0.98",
        "--code-third-party-max-actions",
        "512",
        "--code-third-party-kind",
        "dependency",
        "--code-third-party-kind",
        "vendored",
        "--code-third-party-kind",
        "binary",
        "--code-third-party-kind",
        "generated",
    )
    policy = framework_config_from_args(args).code_third_party_policy

    assert policy.action == "trash"
    assert policy.mutation_requested is True
    assert policy.min_confidence == pytest.approx(0.98)
    assert policy.max_actions == 512
    assert policy.kinds == ("dependency", "vendored", "binary", "generated")
    assert policy.admits("dependency", 0.98)
    assert not policy.admits("dependency", 0.979)
    assert policy.admits("generated", 1.0)
    assert not policy.admits("cache", 1.0)


@pytest.mark.parametrize(
    ("option", "value", "message"),
    (
        (
            "--code-third-party-min-confidence",
            "1.1",
            "must be between 0 and 1",
        ),
        (
            "--code-third-party-max-actions",
            "0",
            "must be between 1 and 10000",
        ),
    ),
)
def test_third_party_policy_limits_are_validated(
    tmp_path: Path,
    option: str,
    value: str,
    message: str,
) -> None:
    args = build_parser().parse_args(
        [
            "--root",
            str(tmp_path),
            "--all",
            option,
            value,
        ]
    )
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


def test_third_party_policy_manifest_payload_is_non_authorizing() -> None:
    payload = CodeThirdPartyPolicy(action="trash").to_dict()

    assert payload["schema"] == "neocortex.code-third-party-policy/v1"
    assert payload["mutation_requested"] is True
    assert "authorized" not in payload
    assert payload["kinds"] == list(DEFAULT_THIRD_PARTY_KINDS)
    assert payload["requires_regeneration_proof"] is True
