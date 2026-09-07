"""Installed Linux entry point for the integrated NeoCortex application."""

from __future__ import annotations

import sys
from collections.abc import Sequence


_CANONICAL_COMMANDS = {
    ("doctor", "capabilities"): ("--doctor-capabilities", "--doctor-capabilities-json"),
    ("doctor", "platform"): ("--doctor-platform", "--doctor-platform-json"),
    ("doctor", "config"): ("--doctor-config", "--doctor-config-json"),
    ("models", "prepare"): ("--models-prepare", "--models-json"),
    ("models", "status"): ("--models-status", "--models-json"),
}

_CANONICAL_OPTIONS = {
    ("doctor", "capabilities"): {
        "--select": "--doctor-capabilities-select",
        "--mime-type": "--doctor-capabilities-mime-type",
        "--input-bytes": "--doctor-capabilities-input-bytes",
    }
}

_HUMAN_COMMANDS = frozenset(
    {
        "help",
        "status",
        "search",
        "ask",
        "curate",
        "inspect",
        "review",
        "knowledge",
        "databases",
        "database",
        "agent",
    }
)


def _translate_canonical_arguments(arguments: Sequence[str]) -> list[str]:
    """Translate the small set of public product aliases to parser flags."""

    if len(arguments) < 2:
        return list(arguments)
    command = (arguments[0], arguments[1])
    flat = _CANONICAL_COMMANDS.get(command)
    if flat is None:
        return list(arguments)
    translated = [flat[0]]
    option_map = _CANONICAL_OPTIONS.get(command, {})
    position = 2
    while position < len(arguments):
        token = arguments[position]
        option, separator, value = token.partition("=")
        if option == "--json":
            # Preserve explicit values so argparse can reject them for this
            # boolean flag instead of silently changing the requested meaning.
            translated.append(flat[1] + (f"={value}" if separator else ""))
        elif option in option_map:
            translated.append(option_map[option] + (f"={value}" if separator else ""))
            if not separator and position + 1 < len(arguments):
                position += 1
                translated.append(arguments[position])
        else:
            translated.append(token)
        position += 1
    return translated


def _canonical_help_requested(arguments: Sequence[str]) -> tuple[str, str] | None:
    if len(arguments) < 2:
        return None
    command = (arguments[0], arguments[1])
    if command not in _CANONICAL_COMMANDS:
        return None
    return command if any(token in {"-h", "--help"} for token in arguments[2:]) else None


def _print_canonical_help(command: tuple[str, str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="Neocortex " + " ".join(command))
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    if command[0] == "models":
        parser.add_argument("--models-root", help="explicit local model resource directory")
        parser.add_argument(
            "--models-model-id", action="append", help="canonical model identity; repeat to select"
        )
    for option, destination in _CANONICAL_OPTIONS.get(command, {}).items():
        parser.add_argument(option, dest=destination.lstrip("-").replace("-", "_"))
    if command == ("models", "prepare"):
        parser.description = "Prepare configured local models."
    elif command == ("models", "status"):
        parser.description = "Inspect configured local model caches."
    elif command == ("doctor", "capabilities"):
        parser.description = "Inspect runtime capabilities without creating state."
    elif command == ("doctor", "config"):
        parser.description = "Inspect effective configuration without creating state."
    else:
        parser.description = "Inspect Linux platform paths and capabilities."
    parser.print_help()


def _run_special_mode(arguments: Sequence[str]) -> int | None:
    if arguments and arguments[0] == "--ui":
        from neocortex.interface.application.arguments import parse_arguments

        # Help and malformed options belong to the parser, not the Qt/display
        # runtime.  Use the same argument contract as the desktop application.
        parse_arguments(arguments[1:])
        from neocortex.interface.application.app import main as run_ui

        return run_ui(arguments[1:])
    if arguments and arguments[0] == "--gui-worker":
        from neocortex.interface.protocol.worker import main as run_worker

        return run_worker(arguments[1:])
    return None


def _run_human_mode(arguments: Sequence[str]) -> int | None:
    """Dispatch read-only human commands without importing route engines."""

    if not arguments or arguments[0] not in _HUMAN_COMMANDS:
        return None
    from neocortex.api.cli.human import run_human_command

    return run_human_command(arguments)


def entrypoint(arguments: Sequence[str] | None = None) -> int:
    """Run one public CLI, desktop, or supervised-worker invocation."""

    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    canonical_help = _canonical_help_requested(forwarded)
    if canonical_help is not None:
        _print_canonical_help(canonical_help)
        return 0
    forwarded = _translate_canonical_arguments(forwarded)
    try:
        special_exit_code = _run_special_mode(forwarded)
        if special_exit_code is not None:
            return special_exit_code
        human_exit_code = _run_human_mode(forwarded)
        if human_exit_code is not None:
            return human_exit_code
        from neocortex.api.cli.cli_app import main as run_cli

        return run_cli(forwarded)
    except SystemExit as exc:
        # Every parser, including desktop help, has the same entrypoint result.
        if exc.code in (None, 0):
            return 0
        raise
    except ModuleNotFoundError as exc:
        if not exc.name or exc.name == "neocortex" or exc.name.startswith("neocortex."):
            raise
        operation = (
            "interfaz gráfica (--ui)" if forwarded[:1] == ["--ui"] else "operación solicitada"
        )
        print(
            f"No se puede ejecutar la {operation}: falta la dependencia Python {exc.name!r}. "
            "Instale los requisitos declarados para esta capacidad desde los recursos offline.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nEjecución cancelada por el usuario.", file=sys.stderr)
        return 130


__all__ = ["_translate_canonical_arguments", "entrypoint"]
