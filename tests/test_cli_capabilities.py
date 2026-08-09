"""Canonical doctor-capabilities facade, output, safety and lazy contracts."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_capabilities.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import _04_Nucleo_Operativo.cli_capabilities as cli_capabilities
from neocortex.capabilities import (
    CapabilityState,
    RequirementKind,
    RuntimeCapabilityStatus,
    RuntimeComponentStatus,
    RuntimeRequirement,
)
from neocortex.cli import _translate_canonical_arguments, entrypoint
from _04_Nucleo_Operativo.cli_app import main
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _python_requirement() -> RuntimeRequirement:
    return RuntimeRequirement(
        component="fixture-python",
        kind=RequirementKind.PYTHON_DISTRIBUTION,
        required=True,
        missing_reason="fixture_python_unavailable",
        extra="fixture",
        distribution="fixture-python",
        module="fixture_python",
    )


def _executable_requirement(*, required: bool) -> RuntimeRequirement:
    return RuntimeRequirement(
        component="fixture-executable",
        kind=RequirementKind.EXECUTABLE,
        required=required,
        missing_reason="fixture_executable_unavailable",
        extra="fixture",
        executable="fixture.exe",
    )


def _available_status() -> RuntimeCapabilityStatus:
    return RuntimeCapabilityStatus(
        capability="fixture",
        state=CapabilityState.AVAILABLE,
        components=(
            RuntimeComponentStatus(
                _python_requirement(),
                available=True,
                version="1.2.3",
            ),
            RuntimeComponentStatus(
                _executable_requirement(required=True),
                available=True,
                path="C:/fixture/fixture.exe",
            ),
        ),
        degradation_reasons=(),
        extra="fixture",
    )


def _degraded_status() -> RuntimeCapabilityStatus:
    return RuntimeCapabilityStatus(
        capability="fixture",
        state=CapabilityState.DEGRADED,
        components=(
            RuntimeComponentStatus(
                _python_requirement(),
                available=True,
                version="1.2.3",
            ),
            RuntimeComponentStatus(
                _executable_requirement(required=False),
                available=False,
            ),
        ),
        degradation_reasons=("fixture_executable_unavailable",),
        extra="fixture",
    )


def _unavailable_status() -> RuntimeCapabilityStatus:
    return RuntimeCapabilityStatus(
        capability="fixture",
        state=CapabilityState.UNAVAILABLE,
        components=(
            RuntimeComponentStatus(
                _python_requirement(),
                available=False,
            ),
        ),
        degradation_reasons=("fixture_python_unavailable",),
        extra="fixture",
    )


def test_canonical_argv_translates_to_hidden_flat_compatibility_flags() -> None:
    with patch("_04_Nucleo_Operativo.cli_app.main", return_value=7) as run_cli:
        result = entrypoint(("doctor", "capabilities", "--json"))

    assert result == 7
    run_cli.assert_called_once_with(["--doctor-capabilities", "--doctor-capabilities-json"])
    assert _translate_canonical_arguments(("doctor", "other")) == [
        "doctor",
        "other",
    ]
    assert _translate_canonical_arguments(("--version",)) == ["--version"]
    assert _translate_canonical_arguments(("--ui", "--help")) == [
        "--ui",
        "--help",
    ]


def test_flat_alias_is_explicit_but_hidden_from_global_help() -> None:
    parser = build_parser()
    args = parser.parse_args(("--doctor-capabilities", "--doctor-capabilities-json"))

    assert parser.allow_abbrev is False
    assert args.doctor_capabilities is True
    assert args.doctor_capabilities_json is True
    assert args._explicit_options == frozenset({"doctor_capabilities", "doctor_capabilities_json"})
    help_text = parser.format_help()
    assert "--doctor-capabilities" not in help_text
    assert "--doctor-capabilities-json" not in help_text


def test_canonical_help_is_specific_without_changing_global_parser_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entrypoint(("doctor", "capabilities", "--help")) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "usage: Neocortex doctor capabilities [-h] [--json]" in captured.out
    assert "--json" in captured.out
    assert "--doctor-capabilities" not in captured.out


def test_available_capabilities_emit_canonical_json_and_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    statuses = (_available_status(),)
    with (
        patch.object(
            cli_capabilities,
            "inspect_runtime_capabilities",
            return_value=statuses,
        ),
        patch.object(
            cli_capabilities.json,
            "dumps",
            wraps=json.dumps,
        ) as dumps,
    ):
        code = entrypoint(("doctor", "capabilities", "--json"))

    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "all_available": True,
        "capabilities": [statuses[0].to_dict()],
        "kind": "runtime_capabilities_report",
        "probe_policy": "metadata-spec-path-only-v1",
        "schema_version": 1,
    }
    assert payload["capabilities"][0]["components"][0]["version"] == "1.2.3"
    assert payload["capabilities"][0]["components"][1]["path"] == ("C:/fixture/fixture.exe")
    assert captured.out == (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    assert dumps.call_args.kwargs["allow_nan"] is False


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    (
        (
            _degraded_status(),
            "degraded",
            "fixture_executable_unavailable",
        ),
        (
            _unavailable_status(),
            "unavailable",
            "fixture_python_unavailable",
        ),
    ),
)
def test_valid_non_available_probe_emits_human_evidence_and_exits_two(
    status: RuntimeCapabilityStatus,
    state: str,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(
        cli_capabilities,
        "inspect_runtime_capabilities",
        return_value=(status,),
    ):
        code = main(("--doctor-capabilities",))

    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"CAPABILITY name=fixture state={state}" in captured.out
    assert f"reasons={reason}" in captured.out
    assert "version=" in captured.out
    assert "path=" in captured.out
    assert f"reason={reason}" in captured.out


def test_fatal_probe_error_exits_one_without_partial_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(
        cli_capabilities,
        "inspect_runtime_capabilities",
        side_effect=RuntimeError("fixture probe failed"),
    ):
        code = main(("--doctor-capabilities", "--doctor-capabilities-json"))

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR doctor-capabilities RuntimeError: fixture probe failed" in captured.err


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--doctor-capabilities-json",),
            "--doctor-capabilities-json requires --doctor-capabilities",
        ),
        (
            ("--doctor-capabilities", "--status"),
            "operations are mutually exclusive",
        ),
        (
            ("--doctor-capabilities", "--apply"),
            "doctor capabilities is read-only and rejects --apply",
        ),
        (
            ("--doctor-capabilities", "--route", "pdf"),
            "doctor capabilities cannot be combined with --route",
        ),
    ),
)
def test_capability_validation_rejects_ambiguous_or_mutating_combinations(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


def test_probe_ignores_state_directory_and_creates_no_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "missing-state"
    with patch.object(
        cli_capabilities,
        "inspect_runtime_capabilities",
        return_value=(_available_status(),),
    ):
        code = entrypoint(
            (
                "doctor",
                "capabilities",
                "--state-directory",
                str(state_directory),
            )
        )

    assert code == 0
    assert "CAPABILITY name=fixture state=available" in capsys.readouterr().out
    assert not state_directory.exists()
    assert not (tmp_path / "framework.lock").exists()


def test_cold_canonical_probe_loads_no_optional_engine_and_creates_no_state(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "missing-state"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["NEOCORTEX_TEST_STATE"] = str(state_directory)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """
                import contextlib
                import importlib.abc
                import io
                import json
                import os
                import sys
                from pathlib import Path

                blocked_roots = {
                    "PIL", "PySide6", "ctranslate2", "fastembed",
                    "faster_whisper", "fitz", "nudenet", "numpy",
                    "pdfminer", "pytesseract",
                }

                class OptionalEngineBlocker(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        del path, target
                        if fullname.partition(".")[0] in blocked_roots:
                            raise ModuleNotFoundError(
                                f"blocked optional engine: {fullname}",
                                name=fullname,
                            )
                        return None

                sys.meta_path.insert(0, OptionalEngineBlocker())
                from neocortex.cli import entrypoint

                if "neocortex.capabilities" in sys.modules:
                    raise SystemExit("capability backend loaded before dispatch")
                state = Path(os.environ["NEOCORTEX_TEST_STATE"])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = entrypoint((
                        "doctor", "capabilities", "--json",
                        "--state-directory", str(state),
                    ))
                if code != 2:
                    raise SystemExit(f"unexpected degraded exit: {code}")
                payload = json.loads(output.getvalue())
                if any(
                    item["models_loaded"] or item["models_downloaded"]
                    for item in payload["capabilities"]
                ):
                    raise SystemExit("capability probe touched models")
                loaded = sorted(
                    name for name in sys.modules
                    if name.partition(".")[0] in blocked_roots
                )
                if loaded:
                    raise SystemExit("optional engines loaded: " + ",".join(loaded))
                if state.exists() or (state.parent / "framework.lock").exists():
                    raise SystemExit("capability probe created state")
                print("CAPABILITIES_COLD_OK")
                """
            ),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "CAPABILITIES_COLD_OK" in completed.stdout


# endregion [02]
