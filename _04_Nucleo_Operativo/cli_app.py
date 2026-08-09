"""Application control flow for the NeoCortex command-line interface."""


# region [01] Lightweight imports and public contract
# Route engines are imported only after parsing and direct-command dispatch.

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .cli_operations import dispatch_direct_operation
from .cli_parser import build_parser

__all__ = ["dispatch_direct", "main", "run_framework"]

# endregion [01]


# region [02] Direct operation dispatch


def dispatch_direct(args: argparse.Namespace) -> int | None:
    """Run a selected direct operation, or return ``None`` for a full run."""

    return dispatch_direct_operation(args)


# endregion [02]


# region [03] Framework configuration and execution


def _run_framework_with_progress(args: argparse.Namespace, progress):
    """Execute the framework with one caller-owned progress reporter."""

    from .cli_config import framework_config_from_args
    from .console_cancellation import ConsoleCancellationBridge
    from .orchestrator import FrameworkOrchestrator

    config = framework_config_from_args(args)
    orchestrator = FrameworkOrchestrator(config, progress=progress)
    with ConsoleCancellationBridge(orchestrator.request_cancellation):
        return orchestrator.run()


def run_framework(args: argparse.Namespace, *, progress=None):
    """Build the validated configuration and run the integrated framework."""

    from _03_Progreso import RichProgress

    if progress is not None:
        return _run_framework_with_progress(args, progress)
    with RichProgress() as progress:
        return _run_framework_with_progress(args, progress)


def _integrated_self_analysis_arguments() -> tuple[str, ...]:
    """Return the canonical protected self-analysis invocation owned by ``--all``."""

    from .app_paths import self_analysis_data_directory, source_repository_directory

    return (
        "--self-analysis",
        "--root",
        str(source_repository_directory()),
        "--state-directory",
        str(self_analysis_data_directory()),
    )


def _run_integrated_self_analysis() -> int:
    """Execute the existing protected service before the document ``--all`` flow."""

    try:
        return main(_integrated_self_analysis_arguments())
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


# endregion [03]


# region [04] Application control flow


def main(arguments: Sequence[str] | None = None) -> int:
    """Parse, validate and dispatch one NeoCortex command invocation."""

    parser = build_parser()
    args = parser.parse_args(arguments)

    from .cli_validation import validate_arguments

    try:
        validate_arguments(args)
    except SystemExit as exc:
        # Domain validators retain a lightweight direct-call contract, but the
        # public CLI reports every argument error through argparse with code 2.
        if isinstance(exc.code, str):
            parser.error(exc.code)
        raise
    direct_exit_code = dispatch_direct(args)
    if direct_exit_code is not None:
        return direct_exit_code

    self_analysis_exit_code = 0
    if args.all:
        self_analysis_exit_code = _run_integrated_self_analysis()

    from _03_Progreso import RichProgress
    from rich.console import Console
    from _02_Deduplicacion import InventoryError

    from .cli_reporting import (
        has_organization_errors,
        has_strict_route_errors,
        print_professional_summary,
        print_reports,
    )

    professional_output = Console().is_terminal
    semantic_results: list[tuple[str, object]] = []
    semantic_exit_code = 0
    semantic_attempted = False
    try:
        with RichProgress() as progress:
            result = run_framework(args, progress=progress)
            actions = getattr(result, "actions", None)
            framework_failed = bool(
                (actions is not None and actions.errors) or has_organization_errors(result)
            )
            if args.all and not framework_failed:
                from .cli_semantic import run_integrated_all_semantic_index

                semantic_attempted = True
                semantic_exit_code = run_integrated_all_semantic_index(
                    args,
                    progress=progress,
                    result_sink=lambda scope, value: semantic_results.append((scope, value)),
                    print_output=not professional_output,
                )
    except InventoryError as exc:
        print(f"ERROR corpus_unavailable: {exc}", file=sys.stderr)
        return 2

    if professional_output:
        print_professional_summary(
            result,
            args,
            semantic_results=tuple(semantic_results),
            semantic_exit_code=semantic_exit_code,
            semantic_attempted=semantic_attempted,
        )
    else:
        print_reports(result, args)
    actions = getattr(result, "actions", None)
    if (actions is not None and actions.errors) or has_organization_errors(result):
        return 2
    if semantic_exit_code != 0:
        return 2
    if self_analysis_exit_code != 0:
        return 2
    if args.strict_exit_codes and has_strict_route_errors(result):
        return 2
    return 0


# endregion [04]
