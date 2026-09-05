"""Fail-closed feedback for Code project scope and explicit inventory roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.api.cli.cli_code import _code_scope_feedback
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _parse(*arguments: str):
    args = build_parser().parse_args(arguments)
    validate_arguments(args)
    return args


def test_explicit_root_outside_project_allowlist_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "temporary-corpus"
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    args = build_parser().parse_args(
        (
            "--root",
            str(root),
            "--state-directory",
            str(tmp_path / "state"),
            "--route",
            "code",
        )
    )

    with pytest.raises(SystemExit, match=r"scope=projects would admit 0 candidates") as raised:
        validate_arguments(args)

    assert "--code-project-root PATH" in str(raised.value)
    assert "--code-scope broad" in str(raised.value)
    assert not (tmp_path / "state").exists()


def test_broad_scope_preserves_an_explicit_temporary_root(tmp_path: Path) -> None:
    root = tmp_path / "temporary-corpus"

    args = _parse(
        "--root",
        str(root),
        "--state-directory",
        str(tmp_path / "state"),
        "--route",
        "code",
        "--code-scope",
        "broad",
    )

    config = framework_config_from_args(args)
    assert config.root == root
    assert config.code_candidate_scope == "broad"


def test_explicit_project_root_can_target_a_temporary_project(tmp_path: Path) -> None:
    root = tmp_path / "temporary-project"

    args = _parse(
        "--root",
        str(root),
        "--state-directory",
        str(tmp_path / "state"),
        "--route",
        "code",
        "--code-project-root",
        str(root),
    )

    config = framework_config_from_args(args)
    assert config.code_candidate_scope == "projects"
    assert config.code_project_roots == (root,)


def test_feedback_is_bounded_and_escapes_control_characters(tmp_path: Path) -> None:
    root = tmp_path / "temporary\nroot"
    feedback = _code_scope_feedback(
        root=root,
        project_roots=(tmp_path / "owned-project",),
        candidate_scope="projects",
    )

    assert feedback is not None
    assert feedback["code"] == "code_scope_no_candidates"
    assert feedback["severity"] == "warning"
    assert "\\n" in feedback["message"]
    assert "--code-project-root PATH" in feedback["message"]


def test_feedback_is_absent_for_broad_or_overlapping_scope(tmp_path: Path) -> None:
    root = tmp_path / "owned-project" / "src"

    assert (
        _code_scope_feedback(
            root=root,
            project_roots=(tmp_path / "owned-project",),
            candidate_scope="projects",
        )
        is None
    )
    assert (
        _code_scope_feedback(
            root=tmp_path / "temporary-corpus",
            project_roots=(tmp_path / "owned-project",),
            candidate_scope="broad",
        )
        is None
    )
