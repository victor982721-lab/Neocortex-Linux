"""Fatal route errors retain owners and nonzero CLI status without tracebacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.api.cli import cli_app
from neocortex.runtime.orchestration.orchestrator import RouteExecutionError
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)


@pytest.mark.parametrize("strict", (False, True))
def test_route_failure_is_fatal_without_reinterpreting_all(
    strict: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    observed = []
    failure = RouteExecutionError(
        {
            "audio": RuntimeError("audio requires the faster-whisper/CTranslate2 runtime"),
            "video": ValueError("bounded FFmpeg processing failed"),
        }
    )

    def run(args, *, progress):
        observed.append(args)
        raise failure

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", run)
    state = tmp_path / "state"
    arguments = ["--all", "--root", str(tmp_path / "corpus"), "--state-directory", str(state)]
    if strict:
        arguments.append("--strict-exit-codes")

    assert cli_app.main(arguments) == 2
    output = capsys.readouterr()
    assert len(observed) == 1
    assert observed[0].all is True
    assert normalize_route_selection(observed[0].route, BUILTIN_ROUTE_ORDER) == BUILTIN_ROUTE_ORDER
    assert "route_execution_failed status=failed completion=incomplete" in output.err
    assert "route=audio error_type=RuntimeError: audio requires the faster-whisper" in output.err
    assert "route=video error_type=ValueError: bounded FFmpeg processing failed" in output.err
    assert "Traceback" not in output.err
    assert output.out == ""
    assert not state.exists()


def test_route_failure_messages_are_bounded_single_line_terminal_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    failure = RouteExecutionError(
        {"audio": RuntimeError("missing backend\n\x1b[31m" + "x" * 5000)}
    )

    def run(_args, *, progress):
        raise failure

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", run)

    assert cli_app.main(["--route", "audio", "--state-directory", str(tmp_path / "state")]) == 2
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 2
    assert "\x1b" not in output.err
    assert "missing backend " in output.err
    assert len(output.err) < 1900
    assert output.out == ""


@pytest.mark.parametrize("failure", (RuntimeError("unexpected defect"), KeyboardInterrupt()))
def test_only_typed_route_failures_are_translated(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(_args, *, progress):
        raise failure

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", run)

    with pytest.raises(type(failure)) as raised:
        cli_app.main(["--route", "text", "--state-directory", str(tmp_path / "state")])

    assert raised.value is failure
