"""Canonical diagnostic aliases retain the underlying argparse contract."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from neocortex.interface.entrypoint import _translate_canonical_arguments


TEST_CAPABILITIES = ("base",)


def _invoke(arguments: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        HOME=str(tmp_path / "home"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "neocortex", *arguments],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert not tuple(tmp_path.iterdir()), "invalid arguments must not create application state"
    return result


@pytest.mark.parametrize("option", ["select", "mime-type", "input-bytes"])
@pytest.mark.parametrize("following", [[], ["--json"]])
def test_missing_diagnostic_value_has_argparse_parity(
    option: str, following: list[str], tmp_path: Path
) -> None:
    canonical = _invoke(["doctor", "capabilities", f"--{option}", *following], tmp_path)
    original = _invoke(
        [
            "--doctor-capabilities",
            f"--doctor-capabilities-{option}",
            *(["--doctor-capabilities-json"] if following else []),
        ],
        tmp_path,
    )
    for result in (canonical, original):
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert f"--doctor-capabilities-{option}" in result.stderr
        assert "expected one argument" in result.stderr
        assert not result.stdout
    assert canonical.stderr == original.stderr


@pytest.mark.parametrize("value", ["0", "false", "true", ""])
def test_boolean_alias_does_not_discard_an_explicit_value(value: str, tmp_path: Path) -> None:
    canonical = _invoke(["doctor", "capabilities", f"--json={value}"], tmp_path)
    original = _invoke(
        ["--doctor-capabilities", f"--doctor-capabilities-json={value}"], tmp_path
    )
    for result in (canonical, original):
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert "ignored explicit argument" in result.stderr
        assert not result.stdout
    assert canonical.stderr == original.stderr


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        (("doctor", "capabilities"), ("--doctor-capabilities", "--doctor-capabilities-json")),
        (("doctor", "platform"), ("--doctor-platform", "--doctor-platform-json")),
        (("models", "status"), ("--models-status", "--models-json")),
        (("models", "prepare"), ("--models-prepare", "--models-json")),
    ],
)
def test_alias_translation_preserves_forms_without_starting_any_capability(
    command: tuple[str, str], flags: tuple[str, str]
) -> None:
    assert _translate_canonical_arguments([*command, "--json"]) == list(flags)
    assert _translate_canonical_arguments([*command, "--json=0"]) == [flags[0], flags[1] + "=0"]


def test_diagnostic_value_forms_and_unrelated_flags_are_preserved() -> None:
    assert _translate_canonical_arguments(
        ["doctor", "capabilities", "--select", "documents", "--mime-type=text/plain", "--input-bytes", "0"]
    ) == [
        "--doctor-capabilities",
        "--doctor-capabilities-select",
        "documents",
        "--doctor-capabilities-mime-type=text/plain",
        "--doctor-capabilities-input-bytes",
        "0",
    ]
    assert _translate_canonical_arguments(["--version"]) == ["--version"]
