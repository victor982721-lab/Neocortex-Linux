"""Cold-import and byte-parity contracts for lightweight CLI leaves."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

import neocortex.api.cli.cli_app as cli_app
from neocortex.api.cli.cli_parser import build_parser
from neocortex.interface.entrypoint import entrypoint


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _invoke(call: Callable[[], int | None]) -> tuple[int, bytes, bytes]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            result = call()
        except SystemExit as exc:
            result = exc.code if isinstance(exc.code, int) else 2
    return int(result or 0), stdout.getvalue().encode(), stderr.getvalue().encode()


@pytest.mark.parametrize(
    "arguments",
    (
        ("doctor", "platform", "--json"),
        ("doctor", "capabilities", "--json"),
        (
            "doctor",
            "capabilities",
            "--select",
            "text.extract",
            "--mime-type",
            "text/plain",
            "--input-bytes",
            "42",
            "--json",
        ),
    ),
)
def test_doctor_leaf_preserves_full_parser_exit_and_output_bytes(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    with monkeypatch.context() as context:
        context.setattr(cli_app, "_run_doctor_leaf", lambda _arguments: None)
        baseline = _invoke(lambda: entrypoint(arguments))

    assert _invoke(lambda: entrypoint(arguments)) == baseline


@pytest.mark.parametrize("arguments", (("--version",), ("--help",)))
def test_global_leaf_output_matches_the_established_full_parser_bytes(
    arguments: tuple[str, ...],
) -> None:
    baseline = _invoke(lambda: build_parser().parse_args(arguments))

    assert _invoke(lambda: entrypoint(arguments)) == baseline


@pytest.mark.parametrize(
    "arguments",
    (
        ("--doctor-platform", "--doctor-capabilities-json"),
        ("--doctor-capabilities", "--doctor-platform-json"),
        ("--doctor-platform", "--doctor-capabilities-select", "text.extract"),
    ),
)
def test_doctor_leaf_rejects_cross_family_hidden_options(
    arguments: tuple[str, ...],
) -> None:
    assert cli_app._run_doctor_leaf(arguments) is None


@pytest.mark.parametrize(
    "arguments",
    (
        (
            "doctor",
            "capabilities",
            "--select",
            "text.extract",
            "--mime-type",
            "text/plain",
            "--input-bytes",
            "not-an-integer",
        ),
        ("doctor", "capabilities", "--select", "unsupported"),
        ("doctor", "platform", "--apply"),
    ),
)
def test_malformed_doctor_argv_keeps_one_full_parser_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    with monkeypatch.context() as context:
        context.setattr(cli_app, "_run_doctor_leaf", lambda _arguments: None)
        baseline = _invoke(lambda: entrypoint(arguments))

    assert _invoke(lambda: entrypoint(arguments)) == baseline


@pytest.mark.parametrize(
    ("arguments", "allowed_exit_codes"),
    (
        (("--version",), (0,)),
        (("--help",), (0,)),
        (("doctor", "platform", "--json"), (0,)),
        (("doctor", "capabilities", "--json"), (0, 2)),
    ),
)
def test_cold_leaf_avoids_read_and_runtime_owner_imports_with_bounded_latency(
    arguments: tuple[str, ...],
    allowed_exit_codes: tuple[int, ...],
) -> None:
    script = textwrap.dedent(
        f"""
        import contextlib
        import io
        import json
        import sys
        import time

        started = time.perf_counter()
        from neocortex.interface.entrypoint import entrypoint

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                exit_code = entrypoint({arguments!r})
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 2
        elapsed_seconds = time.perf_counter() - started
        forbidden = sorted(
            name
            for name in sys.modules
            if name in {{
                "neocortex.api.cli.human",
                "neocortex.api.read_api",
                "neocortex.api.read_api_port",
            }}
            or name.startswith("neocortex.knowledge_")
            or name.startswith("neocortex.semantic_")
        )
        print(json.dumps({{
            "elapsed_seconds": elapsed_seconds,
            "exit_code": exit_code,
            "forbidden": forbidden,
        }}, sort_keys=True))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["exit_code"] in allowed_exit_codes
    assert report["forbidden"] == []
    assert report["elapsed_seconds"] < 2.0


def test_entrypoint_human_recognizer_matches_the_human_facade_contract() -> None:
    from neocortex.api.cli.human import HUMAN_COMMANDS

    from neocortex.interface import entrypoint as cli

    assert cli._HUMAN_COMMANDS == HUMAN_COMMANDS
