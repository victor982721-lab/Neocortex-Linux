"""Focused contracts for the read-only hygiene CLI leaf."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _owner_module(function) -> types.ModuleType:
    module = types.ModuleType("neocortex.runtime.hygiene")
    module.plan_hygiene = function
    return module


def test_parser_tracks_repeatable_roots_limits_and_preview() -> None:
    args = build_parser().parse_args(
        [
            "hygiene",
            "--hygiene-root",
            "/tmp/one",
            "--hygiene-root=/tmp/two",
            "--hygiene-max-entries",
            "20",
            "--hygiene-max-depth",
            "8",
            "--hygiene-max-bytes",
            "4096",
            "--hygiene-preview",
            "--hygiene-json",
        ]
    )

    assert args.command == "hygiene"
    assert args.hygiene_root == [Path("/tmp/one"), Path("/tmp/two")]
    assert args.hygiene_max_entries == 20
    assert args.hygiene_max_depth == 8
    assert args.hygiene_max_bytes == 4096
    assert args.hygiene_preview is True
    assert args.hygiene_json is True
    assert {
        "hygiene_root",
        "hygiene_max_entries",
        "hygiene_max_depth",
        "hygiene_max_bytes",
        "hygiene_preview",
        "hygiene_json",
    } <= args._explicit_options

    validate_arguments(args)


@pytest.mark.parametrize(
    "extra",
    (
        ("--apply",),
        ("--all",),
        ("--dedupe",),
        ("--root", "/tmp/corpus"),
        ("--route", "pdf"),
        ("--organization-apply",),
    ),
)
def test_hygiene_rejects_effects_and_other_operation_selectors(
    extra: tuple[str, ...],
) -> None:
    args = build_parser().parse_args(["hygiene", *extra])
    with pytest.raises(SystemExit):
        validate_arguments(args)


@pytest.mark.parametrize(
    "argv",
    (
        ("--hygiene-json",),
        ("--hygiene-root", "relative"),
        ("hygiene", "--hygiene-max-entries", "0"),
        ("hygiene", "--hygiene-max-depth", "-1"),
        ("hygiene", "--hygiene-max-bytes", "-1"),
    ),
)
def test_hygiene_rejects_missing_command_or_invalid_bounds(argv: tuple[str, ...]) -> None:
    args = build_parser().parse_args(list(argv))
    with pytest.raises(SystemExit):
        validate_arguments(args)


def test_hygiene_json_projects_the_owner_result_and_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, int, int, int, bool]] = []

    def plan_hygiene(
        *,
        roots: list[Path] | None,
        max_entries: int,
        max_depth: int,
        max_bytes: int,
        preview: bool,
    ) -> dict[str, object]:
        calls.append((roots, max_entries, max_depth, max_bytes, preview))
        return {
            "schema": "not-the-cli-schema",
            "status": "planned",
            "candidate_count": 2,
            "entries_scanned": 3,
            "read_only": False,
            "effects_enabled": True,
            "deletion_performed": 19,
            "items": [{"path": "\x1b[31mkeep\x1b[0m"}],
        }

    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.hygiene",
        _owner_module(plan_hygiene),
    )
    root = tmp_path / "root"
    root.mkdir()

    code = main(
        [
            "hygiene",
            "--hygiene-root",
            str(root),
            "--hygiene-max-entries",
            "12",
            "--hygiene-max-depth",
            "3",
            "--hygiene-max-bytes",
            "4096",
            "--hygiene-preview",
            "--hygiene-json",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema"] == "neocortex.hygiene/v1"
    assert payload["operation"] == "hygiene"
    assert payload["read_only"] is True
    assert payload["effects_enabled"] is False
    assert payload["preview_only"] is True
    assert payload["deletion_performed"] == 0
    assert payload["actions_ready"] is False
    assert payload["next_gate"] == "human_review"
    assert payload["limits"] == {
        "max_entries": 12,
        "max_depth": 3,
        "max_bytes": 4096,
    }
    assert payload["roots"] == [str(root)]
    assert payload["items"] == [{"path": "keep"}]
    assert "\x1b" not in captured.out
    assert calls == [([root], 12, 3, 4096, True)]


def test_hygiene_human_output_is_bounded_and_declares_no_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.hygiene",
        _owner_module(
            lambda **_kwargs: {
                "status": "planned",
                "entries_scanned": 3,
                "candidate_count": 1,
            }
        ),
    )

    assert main(["hygiene", "--hygiene-preview"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("HYGIENE mode=preview status=planned")
    assert "scanned=3" in output.out
    assert "candidates=1" in output.out
    assert "deletion_performed=0" in output.out
    assert "effects_enabled=false" in output.out
    assert "actions_ready=false" in output.out
    assert "next_gate=human_review" in output.out


def test_hygiene_does_not_mutate_fixture(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    keep = root / "keep.txt"
    keep.write_bytes(b"canonical bytes")
    before = keep.read_bytes()

    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.hygiene",
        _owner_module(
            lambda **_kwargs: {
                "status": "planned",
                "candidate_count": 1,
                "deletion_performed": 99,
            }
        ),
    )

    assert (
        main(
            [
                "hygiene",
                "--hygiene-root",
                str(root),
                "--hygiene-json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["deletion_performed"] == 0
    assert payload["effects_enabled"] is False
    assert keep.exists()
    assert keep.read_bytes() == before
