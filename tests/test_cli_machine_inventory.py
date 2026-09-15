"""Focused contracts for the federated machine-inventory CLI leaf."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


def _owner_module(function) -> types.ModuleType:
    module = types.ModuleType("neocortex.runtime.machine_inventory")
    module.__dict__["collect_machine_inventory"] = function
    return module


def test_parser_tracks_repeatable_absolute_machine_roots_and_defaults() -> None:
    args = build_parser().parse_args(
        [
            "machine-inventory",
            "--machine-root",
            "/tmp/one",
            "--machine-root=/tmp/two",
            "--machine-max-entries",
            "20",
            "--machine-max-depth",
            "8",
            "--machine-max-bytes",
            "1024",
            "--machine-json",
        ]
    )

    assert args.command == "machine-inventory"
    assert args.machine_root == [Path("/tmp/one"), Path("/tmp/two")]
    assert args.machine_max_entries == 20
    assert args.machine_max_depth == 8
    assert args.machine_max_bytes == 1024
    assert args.machine_json is True
    assert {"machine_root", "machine_max_entries", "machine_max_depth", "machine_max_bytes", "machine_json"} <= args._explicit_options

    validate_arguments(args)


def test_default_machine_profile_does_not_require_a_machine_root() -> None:
    args = build_parser().parse_args(["machine-inventory", "--machine-json"])
    validate_arguments(args)
    assert args.machine_root is None


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (("--apply",), "read-only"),
        (("--all",), "--all"),
        (("--root", "/tmp/corpus"), "--root"),
        (("--route", "none"), "--route"),
        (("--route", "pdf"), "--route"),
        (("--dedupe",), "--dedupe"),
        (("--status",), "direct"),
        (("--pdf-search", "needle"), "direct"),
    ),
)
def test_machine_inventory_rejects_framework_and_direct_intent(
    extra: tuple[str, ...], message: str
) -> None:
    args = build_parser().parse_args(["machine-inventory", *extra])
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--machine-root", "relative"),
        ("--machine-max-entries", "0"),
        ("--machine-max-entries", "1000001"),
        ("--machine-max-depth", "-1"),
        ("--machine-max-depth", "65"),
        ("--machine-max-bytes", "-1"),
        ("--machine-max-bytes", str((1 << 50) + 1)),
    ),
)
def test_machine_inventory_rejects_invalid_configuration(option: str, value: str) -> None:
    args = build_parser().parse_args(["machine-inventory", option, value])
    with pytest.raises(SystemExit):
        validate_arguments(args)


def test_machine_owner_is_lazy_until_the_explicit_leaf_runs(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from neocortex.api.cli.cli_app import main
        from neocortex.api.cli.cli_parser import build_parser

        build_parser().parse_args(["machine-inventory", "--machine-json"])
        if "neocortex.runtime.machine_inventory" in sys.modules:
            raise SystemExit("machine owner imported during parser setup")
        # The missing owner is still a read-only diagnostic, not a framework
        # failure and not permission to fall back to the content inventory.
        if main(["machine-inventory", "--machine-json"]) != 0:
            raise SystemExit("machine diagnostic returned a non-zero code")
        if "neocortex.runtime.machine_inventory" not in sys.modules:
            raise SystemExit("machine owner was not imported at dispatch")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_machine_json_and_json_converge_and_sanitize_owner_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[Path], int, int, int]] = []

    def collect_machine_inventory(
        *, roots: list[Path], max_entries: int, max_depth: int, max_bytes: int
    ) -> dict[str, object]:
        calls.append((roots, max_entries, max_depth, max_bytes))
        return {
            "status": "observed",
            "coverage": "complete",
            "truncated": True,
            "scanned": 2,
            "records": [{"path": "\x1b[31msecret\x1b[0m", "note\x00": "line\nvalue"}],
            "reason_summary": {"bounds_exceeded": 1},
            "evil_top_level": "must remain inside result",
        }

    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.machine_inventory",
        _owner_module(collect_machine_inventory),
    )
    root = tmp_path / "machine"
    root.mkdir()
    for selector in ("--machine-json", "--json"):
        code = main(
            [
                "machine-inventory",
                "--machine-root",
                str(root),
                "--machine-max-entries",
                "12",
                "--machine-max-depth",
                "3",
                "--machine-max-bytes",
                "4096",
                selector,
            ]
        )
        captured = capsys.readouterr()
        assert code == 0
        assert captured.err == ""
        payload = json.loads(captured.out)
        assert payload["schema"] == "neocortex.machine-inventory/v1"
        assert payload["operation"] == "machine-inventory"
        assert payload["read_only"] is True
        assert payload["diagnostic_only"] is True
        assert payload["mutation_authorized"] is False
        assert payload["applied"] == 0
        assert payload["roots"] == [str(root)]
        assert payload["limits"] == {
            "max_entries": 12,
            "max_depth": 3,
            "max_bytes": 4096,
        }
        assert payload["truncated"] is True
        assert "evil_top_level" not in payload
        encoded = captured.out
        assert "\x1b" not in encoded
        assert "\x00" not in encoded
    assert calls == [
        ([root], 12, 3, 4096),
        ([root], 12, 3, 4096),
    ]


def test_owner_failures_remain_zero_exit_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def collect_machine_inventory(**_kwargs):
        raise PermissionError("denied\n\x1b[31mroot\x1b[0m")

    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.machine_inventory",
        _owner_module(collect_machine_inventory),
    )
    assert main(["machine-inventory", "--machine-json"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["status"] == "blocked"
    assert payload["coverage"] == "blocked"
    assert payload["exit_code"] == 0
    assert payload["error"]["code"] == "PermissionError"
    assert "\x1b" not in output.out
    assert "\n" not in payload["error"]["message"]


def test_human_machine_output_is_read_only_and_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "neocortex.runtime.machine_inventory",
        _owner_module(lambda **_kwargs: {"status": "unknown", "scanned": 0}),
    )
    root = tmp_path / "machine"
    root.mkdir()
    assert main(["machine-inventory", "--machine-root", str(root)]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("MACHINE-INVENTORY status=unknown")
    assert "read_only=true" in output.out
    assert "ROOT index=0" in output.out
