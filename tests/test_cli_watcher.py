"""Canonical CLI integration for the foreground incremental watcher."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_watcher.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from _04_Nucleo_Operativo.cli_app import dispatch_direct
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_reporting import watcher_exit_code
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.watcher import (
    WatcherEvent,
    WatcherRunSummary,
    WatcherSummary,
)
# endregion [01]

# region [02] Implementación


def _summary(
    *,
    cancelled: bool = False,
    failed_runs: int = 0,
    source_errors: int = 0,
) -> WatcherSummary:
    return WatcherSummary(
        started_ns=100,
        finished_ns=200,
        cancelled=cancelled,
        bootstrap_runs=1,
        change_runs=2,
        discontinuity_runs=0,
        portable_runs=1,
        successful_runs=3,
        failed_runs=failed_runs,
        signal_batches=2,
        signal_records=7,
        idle_polls=4,
        source_restarts=source_errors,
        source_errors=source_errors,
        backoff_waits=source_errors,
        checkpoint_loads=5,
        last_run=None,
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--watch", "--apply"), "--watch cannot be combined with --apply"),
        (
            ("--watch", "--route", "pdf", "--route-only"),
            "--watch cannot be combined with --route-only",
        ),
        (
            ("--watch", "--resume-run", "7"),
            "--watch cannot be combined with --resume-run",
        ),
        (
            ("--watch", "--candidate-run", "7"),
            "--watch cannot be combined with --candidate-run",
        ),
    ),
)
def test_watch_rejects_incompatible_run_modes(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)
    assert str(raised.value) == message


def test_watcher_timing_options_require_watch_and_remain_bounded() -> None:
    without_watch = build_parser().parse_args(("--watch-debounce-seconds", "0.5"))
    with pytest.raises(SystemExit, match="watcher timing options require --watch"):
        validate_arguments(without_watch)

    invalid = build_parser().parse_args(
        ("--watch", "--watch-poll-timeout-seconds", "0")
    )
    with pytest.raises(
        SystemExit,
        match="--watch-poll-timeout-seconds must be between 1 and 300",
    ):
        validate_arguments(invalid)

    invalid_portable = build_parser().parse_args(
        ("--watch", "--watch-portable-interval-seconds", "nan")
    )
    with pytest.raises(
        SystemExit,
        match="--watch-portable-interval-seconds must be between 1 and 86400",
    ):
        validate_arguments(invalid_portable)


def test_watch_all_expands_the_normal_integrated_configuration() -> None:
    args = build_parser().parse_args(("--watch", "--all"))
    validate_arguments(args)
    assert args.route == "all"
    assert args.ocr == "auto"


def test_watch_dispatch_builds_normal_config_reports_and_bridges_cancellation(
    tmp_path,
    capsys,
) -> None:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    args = build_parser().parse_args(
        (
            "--root",
            str(root),
            "--state-directory",
            str(state),
            "--route",
            "pdf",
            "--watch",
            "--watch-bootstrap",
            "always",
            "--watch-poll-timeout-seconds",
            "3",
            "--watch-debounce-seconds",
            "0.5",
            "--watch-max-debounce-seconds",
            "5",
            "--watch-error-backoff-initial-seconds",
            "0.25",
            "--watch-error-backoff-max-seconds",
            "8",
            "--watch-error-backoff-multiplier",
            "1.5",
            "--watch-portable-interval-seconds",
            "45",
        )
    )
    validate_arguments(args)

    final_summary = _summary()
    fake_watcher = MagicMock()
    progress_manager = MagicMock()
    progress = object()
    progress_manager.__enter__.return_value = progress
    bridge_manager = MagicMock()

    def build_watcher(*constructor_args, **constructor_kwargs):
        def run_foreground():
            constructor_kwargs["event_callback"](
                WatcherEvent(1, 101, "started", "observing", {"root": str(root)})
            )
            constructor_kwargs["run_callback"](
                WatcherRunSummary(
                    reason="bootstrap",
                    succeeded=True,
                    started_ns=102,
                    elapsed_ns=50,
                    checkpoint_before=None,
                    run_id=9,
                    inventory_mode="full",
                )
            )
            return final_summary

        fake_watcher.run_foreground.side_effect = run_foreground
        return fake_watcher

    with (
        patch(
            "_04_Nucleo_Operativo.watcher.IncrementalWatcher",
            side_effect=build_watcher,
        ) as watcher_class,
        patch("neocortex.progress.RichProgress", return_value=progress_manager),
        patch(
            "_04_Nucleo_Operativo.console_cancellation.ConsoleCancellationBridge",
            return_value=bridge_manager,
        ) as bridge_class,
    ):
        assert dispatch_direct(args) == 0

    framework_config, watch_config = watcher_class.call_args.args
    assert framework_config.root == root
    assert framework_config.state_directory == state
    assert framework_config.route == "pdf"
    assert framework_config.apply_actions is False
    assert framework_config.route_only is False
    assert watch_config.bootstrap == "always"
    assert watch_config.poll_timeout_seconds == 3
    assert watch_config.debounce_seconds == 0.5
    assert watch_config.max_debounce_seconds == 5
    assert watch_config.error_backoff_initial_seconds == 0.25
    assert watch_config.error_backoff_max_seconds == 8
    assert watch_config.error_backoff_multiplier == 1.5
    assert watch_config.portable_interval_seconds == 45
    assert watcher_class.call_args.kwargs["progress"] is progress
    bridge_class.assert_called_once_with(fake_watcher.request_cancellation)

    output = capsys.readouterr().out
    assert "WATCH_EVENT sequence=1 kind=started" in output
    assert "WATCH_RUN reason=bootstrap succeeded=1 run_id=9" in output
    assert "WATCH_SUMMARY cancelled=0 bootstrap_runs=1 change_runs=2" in output


def test_watcher_exit_codes_distinguish_success_errors_and_cancellation() -> None:
    assert watcher_exit_code(_summary()) == 0
    assert watcher_exit_code(_summary(failed_runs=1)) == 2
    assert watcher_exit_code(_summary(source_errors=1)) == 2
    assert watcher_exit_code(_summary(cancelled=True)) == 130


def test_watch_dispatch_returns_error_for_retained_watcher_failures(tmp_path) -> None:
    args = build_parser().parse_args(("--state-directory", str(tmp_path), "--watch"))
    validate_arguments(args)
    fake_watcher = MagicMock()
    fake_watcher.run_foreground.return_value = _summary(failed_runs=1)
    progress_manager = MagicMock()
    with (
        patch(
            "_04_Nucleo_Operativo.watcher.IncrementalWatcher",
            return_value=fake_watcher,
        ),
        patch("neocortex.progress.RichProgress", return_value=progress_manager),
        patch(
            "_04_Nucleo_Operativo.console_cancellation.ConsoleCancellationBridge",
            return_value=MagicMock(),
        ),
    ):
        assert dispatch_direct(args) == 2


# endregion [02]
