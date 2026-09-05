"""The public CLI distinguishes failed and cancelled runs in its real JSON stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.api.cli import cli_app, cli_reporting, cli_semantic
from neocortex.interface.entrypoint import entrypoint
from neocortex.persistence.sqlite_immutable import ImmutableSQLiteUnavailable
from neocortex.progress import ProgressEvent
from neocortex.runtime.orchestration.orchestrator import RouteExecutionError


def _stream_events(stderr: str) -> list[dict[str, object]]:
    prefix = "NEOCORTEX_PROGRESS "
    return [
        json.loads(line.removeprefix(prefix))
        for line in stderr.splitlines()
        if line.startswith(prefix)
    ]


@pytest.mark.parametrize("wrapped", (False, True))
def test_sqlite_failure_has_one_failed_terminal_event_and_safe_next_step(
    wrapped: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    sqlite_failure = ImmutableSQLiteUnavailable(
        "SQLite owner changed during immutable read\n\x1b[31m" + "x" * 3000
    )
    failure = (
        RouteExecutionError({"text": sqlite_failure, "audio": sqlite_failure})
        if wrapped
        else sqlite_failure
    )

    def run(_args, *, progress):
        progress(ProgressEvent("framework", "prepare", "Preparando ejecución", 0, 1))
        raise failure

    def unexpected(*_args, **_kwargs):
        pytest.fail("An incomplete framework must not run Semantic or report success")

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", run)
    monkeypatch.setattr(cli_semantic, "run_integrated_all_semantic_index", unexpected)
    monkeypatch.setattr(cli_reporting, "print_reports", unexpected)
    monkeypatch.setattr(cli_reporting, "print_professional_summary", unexpected)
    state = tmp_path / "state"

    assert entrypoint(["--all", "--state-directory", str(state)]) == 2

    output = capsys.readouterr()
    events = _stream_events(output.err)
    terminal = [event for event in events if event["phase"] == "result"]
    assert len(terminal) == 1
    assert events[-1] == terminal[0]
    assert terminal[0]["operation"] == "framework"
    assert terminal[0]["finished"] is True
    assert terminal[0]["completed"] == 0
    assert terminal[0]["total"] is None
    metrics = terminal[0]["metrics"]
    assert metrics["status"] == "failed"
    assert metrics["completion"] == "incomplete"
    assert metrics["exit_code"] == 2
    assert metrics["error_code"] == (
        "route_execution_failed" if wrapped else "sqlite_snapshot_unavailable"
    )
    assert metrics["errors"] == (2 if wrapped else 1)
    assert metrics["failed_routes"] == ("audio,text" if wrapped else "")
    assert "SQLite owner changed" in metrics["cause"]
    assert len(metrics["cause"]) <= 512
    assert "\x1b" not in metrics["cause"]
    assert "\n" not in metrics["cause"]
    assert "WAL/SHM" in output.err
    assert "--status --status-json" in output.err
    assert "Traceback" not in output.err
    assert "COMPLETADA" not in output.err
    assert output.out == ""
    assert not state.exists()


def test_keyboard_interrupt_has_cancelled_terminal_event_and_exit_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def run(_args, *, progress):
        progress(ProgressEvent("audio", "extract", "Extrayendo audio", 1, 4))
        raise KeyboardInterrupt

    monkeypatch.setenv("NEOCORTEX_PROGRESS_STREAM", "1")
    monkeypatch.setattr(cli_app, "run_framework", run)
    assert entrypoint(["--route", "audio", "--state-directory", str(tmp_path / "state")]) == 130

    output = capsys.readouterr()
    terminal = _stream_events(output.err)[-1]
    assert terminal["operation"] == "framework"
    assert terminal["phase"] == "result"
    assert terminal["finished"] is True
    assert terminal["completed"] == 0
    assert terminal["total"] is None
    assert terminal["metrics"]["status"] == "cancelled"
    assert terminal["metrics"]["error_code"] == "execution_cancelled"
    assert terminal["metrics"]["exit_code"] == 130
    assert terminal["metrics"]["errors"] == 0
    assert "route_execution_failed" not in output.err
    assert "Traceback" not in output.err
    assert output.out == ""


def test_failed_event_reaches_rich_reporter_before_it_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import neocortex.progress as progress_module

    class Reporter:
        active = False

        def __init__(self):
            self.events = []

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, *_args):
            self.active = False

        def __call__(self, event):
            assert self.active
            self.events.append(event)

    reporter = Reporter()

    def run(_args, *, progress):
        raise RouteExecutionError({"audio": RuntimeError("missing local decoder")})

    monkeypatch.delenv("NEOCORTEX_PROGRESS_STREAM", raising=False)
    monkeypatch.setattr(progress_module, "RichProgress", lambda: reporter)
    monkeypatch.setattr(cli_app, "run_framework", run)
    assert cli_app.main(["--route", "audio", "--state-directory", str(tmp_path / "state")]) == 2

    assert not reporter.active
    assert len(reporter.events) == 1
    event = reporter.events[0]
    assert event.key == ("framework", "result")
    assert {metric.name: metric.value for metric in event.metrics}["status"] == "failed"
