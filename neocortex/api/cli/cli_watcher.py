"""Foreground watcher command wiring for the canonical CLI."""

from __future__ import annotations

import argparse
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neocortex.runtime.control.watcher import IncrementalWatcherConfig

__all__ = ["run_incremental_watcher", "watcher_config_from_args"]


# region [01] CLI configuration translation


def watcher_config_from_args(args: argparse.Namespace) -> IncrementalWatcherConfig:
    """Build the explicit bounded watcher timing policy."""

    from neocortex.runtime.control.watcher import IncrementalWatcherConfig

    return IncrementalWatcherConfig(
        bootstrap=args.watch_bootstrap,
        poll_timeout_seconds=args.watch_poll_timeout_seconds,
        debounce_seconds=args.watch_debounce_seconds,
        max_debounce_seconds=args.watch_max_debounce_seconds,
        error_backoff_initial_seconds=args.watch_error_backoff_initial_seconds,
        error_backoff_max_seconds=args.watch_error_backoff_max_seconds,
        error_backoff_multiplier=args.watch_error_backoff_multiplier,
        portable_interval_seconds=args.watch_portable_interval_seconds,
    )


# endregion [01]


# region [02] Foreground command


def run_incremental_watcher(args: argparse.Namespace) -> int:
    """Run the incremental watcher in this process and calling thread."""

    from neocortex.progress import RichProgress

    from .cli_config import framework_config_from_args
    from .cli_reporting import (
        print_watcher_event,
        print_watcher_interrupted,
        print_watcher_run_summary,
        print_watcher_summary,
        watcher_exit_code,
    )
    from neocortex.runtime.control.console_cancellation import ConsoleCancellationBridge
    from neocortex.runtime.control.watcher import IncrementalWatcher

    watcher: IncrementalWatcher | None = None
    try:
        framework_config = framework_config_from_args(args)
        watch_config = watcher_config_from_args(args)
        with RichProgress() as progress:
            watcher = IncrementalWatcher(
                framework_config,
                watch_config,
                event_callback=print_watcher_event,
                run_callback=print_watcher_run_summary,
                progress=progress,
            )
            with ConsoleCancellationBridge(watcher.request_cancellation):
                summary = watcher.run_foreground()
    except KeyboardInterrupt:
        if watcher is not None:
            watcher.request_cancellation()
        print_watcher_interrupted()
        return 130
    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError) as exc:
        print(f"ERROR watch {type(exc).__name__}: {exc}")
        return 2

    print_watcher_summary(summary)
    return watcher_exit_code(summary)


# endregion [02]


from neocortex.platform import preserve_legacy_module as _preserve_legacy_module
_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_watcher')
