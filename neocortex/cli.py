"""Installed command entry point for the integrated NeoCortex application."""
# region [00] Contexto del módulo
# Módulo: neocortex/cli.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
# endregion [01]

# region [02] Implementación


_CANONICAL_COMMANDS = {
    ("code", "experiment"): (
        "--code-experiment-run",
        "--code-json",
        "Execute one allow-listed test-contract experiment against current source with disposable fixture state.",
    ),
    ("code", "query"): (
        "--code-query",
        "--code-json",
        "Query published analyzer evidence, evidence gaps and experiment proposals.",
    ),
    ("code", "review"): (
        "--code-review",
        "--code-json",
        "Review the current protected self-analysis publication without mutating source or state.",
    ),
    ("code", "status"): (
        "--code-status",
        "--code-json",
        "Inspect the current protected self-analysis publication and provider readiness.",
    ),
    ("code", "validate"): (
        "--code-validate-change",
        "--code-json",
        "Run the sole local Linux validation for a source change.",
    ),
    ("doctor", "capabilities"): (
        "--doctor-capabilities",
        "--doctor-capabilities-json",
        "Inspect declared runtime capabilities without importing optional engines, "
        "loading models or creating state.",
    ),
    ("doctor", "platform"): (
        "--doctor-platform",
        "--doctor-platform-json",
        "Inspect platform paths, inventory, identity, containment, elevation and "
        "mutation capabilities without creating state.",
    ),
    ("models", "prepare"): (
        "--models-prepare",
        "--models-json",
        "Explicitly and sequentially download and validate all production models.",
    ),
    ("models", "status"): (
        "--models-status",
        "--models-json",
        "Inspect all production model caches locally without downloads or writes.",
    ),
}

_CAPABILITIES_CANONICAL_OPTIONS = {
    "--select": "--doctor-capabilities-select",
    "--mime-type": "--doctor-capabilities-mime-type",
    "--input-bytes": "--doctor-capabilities-input-bytes",
}

_CODE_VALIDATION_CANONICAL_OPTIONS = {
    "--baseline": "--code-validation-baseline",
    "--max-tests": "--code-validation-max-tests",
    "--time-budget-seconds": "--code-validation-time-budget-seconds",
}

_CODE_QUERY_CANONICAL_OPTIONS = {
    "--provider": "--code-query-provider",
    "--category": "--code-query-category",
    "--question-id": "--code-query-category",
    "--module": "--code-query-module",
    "--status": "--code-query-status",
    "--delta": "--code-query-delta",
    "--work-package": "--code-query-work-package",
    "--limit": "--code-query-limit",
    "--baseline-state": "--code-query-baseline",
}

_CODE_ANALYSIS_CANONICAL_COMMANDS = frozenset(
    {
        ("code", "experiment"),
        ("code", "query"),
        ("code", "review"),
        ("code", "status"),
    }
)

# This first-token allowlist is intentionally duplicated at the installed
# entrypoint boundary.  Importing ``human_cli`` merely to ask whether an argv
# belongs to that facade also imports its operational read adapters.  Leaf
# commands that cannot be human commands must stay on the lightweight parser
# path instead.
_HUMAN_COMMANDS = frozenset({"help", "status", "search", "ask", "inspect", "review", "agent"})


def _prepend_owned_executable_directories() -> None:
    """Expose executable shims installed inside the active Neocortex runtime."""

    prefix = Path(sys.prefix)
    candidates = (
        prefix / "tools" / "pyright" / "node_modules" / ".bin",
        prefix / "tools" / "node" / "bin",
        prefix / "tools" / "node",
        prefix.parent / "tools" / "node",
    )
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    known_entries = {os.path.normcase(os.path.normpath(entry)) for entry in path_entries}
    additions: list[str] = []
    for candidate in candidates:
        candidate_text = str(candidate)
        candidate_key = os.path.normcase(os.path.normpath(candidate_text))
        if candidate.is_dir() and candidate_key not in known_entries:
            additions.append(candidate_text)
            known_entries.add(candidate_key)
    if additions:
        os.environ["PATH"] = os.pathsep.join((*additions, *path_entries))


def _canonical_command(arguments: Sequence[str]) -> tuple[str, str] | None:
    command = tuple(arguments[:2])
    return command if command in _CANONICAL_COMMANDS else None


def _canonical_help_requested(arguments: Sequence[str]) -> bool:
    if _canonical_command(arguments) is None:
        return False
    for token in arguments[2:]:
        if token == "--":
            return False
        if token in {"-h", "--help"}:
            return True
    return False


def _print_canonical_help(command: tuple[str, str]) -> None:
    _flat, _json_flat, description = _CANONICAL_COMMANDS[command]
    parser = argparse.ArgumentParser(
        prog="Neocortex " + " ".join(command),
        description=description,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one canonical JSON capability report",
    )
    if command in _CODE_ANALYSIS_CANONICAL_COMMANDS:
        from _04_Nucleo_Operativo.app_paths import self_analysis_data_directory

        parser.add_argument(
            "--state-directory",
            default=str(self_analysis_data_directory()),
            metavar="DIRECTORY",
            help="published self-analysis state (defaults to the canonical personal owner)",
        )
    if command == ("code", "review"):
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="bounded observations per review surface (1..50)",
        )
    if command == ("code", "query"):
        parser.add_argument("surface", choices=("status", "review", "diff"))
        parser.add_argument("--provider", action="append", metavar="ID")
        parser.add_argument("--category", action="append", metavar="VALUE")
        parser.add_argument("--question-id", action="append", metavar="ID")
        parser.add_argument("--module", action="append", metavar="MODULE")
        parser.add_argument(
            "--status",
            action="append",
            metavar="DIMENSION:VALUE",
            help=("exact dimension such as decision:experiment_required or execution:executable"),
        )
        parser.add_argument("--delta", action="append", metavar="VALUE")
        parser.add_argument("--work-package", action="append", metavar="VALUE")
        parser.add_argument(
            "--executable-only",
            action="store_true",
            help="return only questions or proposals with an allow-listed runner",
        )
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--baseline-state", metavar="DIRECTORY")
    if command == ("code", "experiment"):
        parser.add_argument("proposal_id", metavar="PROPOSAL_ID")
    if command == ("doctor", "capabilities"):
        parser.add_argument(
            "--select",
            metavar="CAPABILITY",
            help="explain provider selection for text.extract",
        )
        parser.add_argument(
            "--mime-type",
            metavar="MIME",
            help="exact input MIME type for --select",
        )
        parser.add_argument(
            "--input-bytes",
            type=int,
            metavar="BYTES",
            help="non-negative input size for --select",
        )
    if command == ("code", "validate"):
        parser.add_argument("--baseline", default="HEAD", help="Git baseline (default HEAD)")
        parser.add_argument("--max-tests", type=int, default=5000)
        parser.add_argument("--time-budget-seconds", type=int, default=900)
    parser.print_help()


def _translate_canonical_arguments(arguments: Sequence[str]) -> list[str]:
    """Translate one exact canonical facade into hidden flat compatibility flags."""

    forwarded = list(arguments)
    command = _canonical_command(forwarded)
    if command is None:
        return forwarded
    flat_flag, json_flat_flag, _description = _CANONICAL_COMMANDS[command]
    remaining = list(forwarded[2:])
    translated = [flat_flag]
    if command in {("code", "query"), ("code", "experiment")}:
        value_options = (
            {
                "--state-directory",
                "--provider",
                "--category",
                "--question-id",
                "--module",
                "--status",
                "--delta",
                "--work-package",
                "--limit",
                "--baseline-state",
            }
            if command == ("code", "query")
            else {"--state-directory"}
        )
        expects_value = False
        positional_index: int | None = None
        for index, token in enumerate(remaining):
            if expects_value:
                expects_value = False
                continue
            if token == "--":
                if index + 1 < len(remaining):
                    positional_index = index + 1
                break
            option, separator, _value = token.partition("=")
            if option in value_options and not separator:
                expects_value = True
                continue
            if not token.startswith("-"):
                positional_index = index
                break
        if positional_index is not None:
            translated.append(remaining.pop(positional_index))
    if command in _CODE_ANALYSIS_CANONICAL_COMMANDS and not any(
        token == "--state-directory" or token.startswith("--state-directory=")
        for token in remaining
    ):
        from _04_Nucleo_Operativo.app_paths import self_analysis_data_directory

        translated.extend(("--state-directory", str(self_analysis_data_directory())))
    translate_options = True
    for token in remaining:
        option, separator, value = token.partition("=")
        if token == "--":
            translate_options = False
            translated.append(token)
        elif translate_options and option == "--json":
            translated.append(json_flat_flag + separator + value)
        elif (
            translate_options
            and command == ("doctor", "capabilities")
            and option in _CAPABILITIES_CANONICAL_OPTIONS
        ):
            translated.append(_CAPABILITIES_CANONICAL_OPTIONS[option] + separator + value)
        elif (
            translate_options
            and command == ("code", "validate")
            and option in _CODE_VALIDATION_CANONICAL_OPTIONS
        ):
            translated.append(_CODE_VALIDATION_CANONICAL_OPTIONS[option] + separator + value)
        elif command == ("code", "review") and option == "--limit":
            translated.append("--code-review-limit" + separator + value)
        elif command == ("code", "query") and option in _CODE_QUERY_CANONICAL_OPTIONS:
            translated.append(_CODE_QUERY_CANONICAL_OPTIONS[option] + separator + value)
        elif command == ("code", "query") and option == "--executable-only":
            if separator:
                translated.append(token)
            else:
                translated.extend(("--code-query-status", "execution:executable"))
        else:
            translated.append(token)
    return translated


def _run_special_mode(arguments: Sequence[str]) -> int | None:
    if arguments and arguments[0] == "--ui":
        from _05_Interfaz.app import main as run_ui

        return run_ui(arguments[1:])
    if arguments and arguments[0] == "--gui-worker":
        from _05_Interfaz.worker import main as run_worker

        return run_worker(arguments[1:])
    return None


def _run_human_mode(arguments: Sequence[str]) -> int | None:
    """Dispatch the concise facade without importing operational readers eagerly."""

    if not arguments or arguments[0] not in _HUMAN_COMMANDS:
        return None

    from .human_cli import run_human_command

    return run_human_command(arguments)


def entrypoint(arguments: Sequence[str] | None = None) -> int:
    """Run one CLI, desktop, or supervised-worker invocation."""

    _prepend_owned_executable_directories()
    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    try:
        special_exit_code = _run_special_mode(forwarded)
        if special_exit_code is not None:
            return special_exit_code
        human_exit_code = _run_human_mode(forwarded)
        if human_exit_code is not None:
            return human_exit_code
        if _canonical_help_requested(forwarded):
            command = _canonical_command(forwarded)
            assert command is not None
            _print_canonical_help(command)
            return 0
        forwarded = _translate_canonical_arguments(forwarded)
        from _04_Nucleo_Operativo.cli_app import main as run_cli

        return run_cli(forwarded)
    except KeyboardInterrupt:
        print("\nEjecución cancelada por el usuario.", file=sys.stderr)
        return 130


__all__ = ["entrypoint"]
# endregion [02]
