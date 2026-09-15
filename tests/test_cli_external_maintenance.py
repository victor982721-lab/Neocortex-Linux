"""CLI contracts for the explicit external read-only diagnostic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def test_external_command_requires_absolute_root_and_category() -> None:
    parser = build_parser()
    args = parser.parse_args(["external-maintenance"])
    with pytest.raises(SystemExit, match="--external-root"):
        validate_arguments(args)

    args = parser.parse_args(
        ["external-maintenance", "--external-root", "relative", "--external-category", "cache"]
    )
    with pytest.raises(SystemExit, match="absoluto"):
        validate_arguments(args)

    args = parser.parse_args(
        ["external-maintenance", "--external-root", "/tmp", "--external-category", ""]
    )
    with pytest.raises(SystemExit, match="--external-category"):
        validate_arguments(args)


def test_external_command_is_read_only_and_bounded(tmp_path: Path, capsys) -> None:
    root = tmp_path / "external"
    root.mkdir(mode=0o700)
    payload = root / "cache.bin"
    payload.write_bytes(b"keep")

    code = main(
        [
            "external-maintenance",
            "--external-root",
            str(root),
            "--external-category",
            "application_cache",
            "--external-json",
            "--external-max-entries",
            "4",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["schema"] == "neocortex.external-maintenance/v1"
    assert result["operation"] == "external-maintenance"
    assert result["read_only"] is True
    assert result["diagnostic_only"] is True
    assert result["candidates"] == result["applied"] == 0
    assert payload.read_bytes() == b"keep"


def test_external_command_rejects_apply_and_corpus_root(tmp_path: Path) -> None:
    base = [
        "external-maintenance",
        "--external-root",
        str(tmp_path / "external"),
        "--external-category",
        "cache",
    ]
    with pytest.raises(SystemExit):
        main([*base, "--apply"])
    with pytest.raises(SystemExit):
        main([*base, "--root", str(tmp_path / "corpus")])


def test_external_options_without_command_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--external-root",
                str(tmp_path),
                "--external-category",
                "cache",
            ]
        )
