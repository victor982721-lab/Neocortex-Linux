"""Installed Linux entry point for the integrated NeoCortex CLI."""

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
        "knowledge",
        "databases",
        "database",
        "agent",
    }
)


def _root_help_requested(arguments: Sequence[str]) -> bool:
    """Recognize the small root help form without constructing route state."""

    if not arguments:
        return False
    position = 0
    remaining: list[str] = []
    value_options = {"--root", "--state-directory"}
    while position < len(arguments):
        token = arguments[position]
        option, separator, _value = token.partition("=")
        if option in value_options:
            if separator:
                if not _value:
                    return False
                position += 1
                continue
            if position + 1 >= len(arguments):
                return False
            position += 2
            continue
        remaining.append(token)
        position += 1
    return remaining in (["--help"], ["-h"])


def _print_root_help() -> None:
    """Print concise installed-command guidance, not the route inventory."""

    print(
        """usage: Neocortex [--root ROOT] (--factory-reset | --all | --dedupe | --route ROUTES | COMMAND)

Consulta local:
  status, search, ask, inspect, knowledge, curate, databases
  machine-inventory --machine-root PATH [--machine-root PATH ...]
                        diagnóstico federado read-only y bounded

Procesamiento:
  --factory-reset       borra el estado local administrado, sin backup, plan, digest ni confirmaciones extra
  --all                 ejecuta las rutas configuradas
  --dedupe              solicita el servicio de duplicados, sin rutas de contenido
  maintenance --scope S planifica o retira scratch propio o audita históricos
                        (owned-temp|audit-work|historical-temp)
  --maintenance-audit-root PATH
                        raíz absoluta explícita para historical-temp; no usa /tmp por defecto
  external-maintenance --external-root PATH --external-category CATEGORY
                        diagnóstico bounded read-only de una raíz externa; nunca aplica cambios
  dedupe                alias de --dedupe
  --route ROUTES        ejecuta una o más rutas sobre el inventario
  --root ROOT           conserva precedencia explícita sobre la raíz por defecto
  --apply               sólo procede con una capacidad de backend verificada
  --json                emite un resumen JSON para --all, --dedupe, --route o machine-inventory
  --dedupe-json         emite el contrato JSON específico de --dedupe
  --version             muestra la identidad de la instalación activa

Usa `Neocortex COMMAND --help` para una operación concreta. `--help`
no inicia inventario ni crea estado.
"""
    )


def _translate_canonical_arguments(arguments: Sequence[str]) -> list[str]:
    """Translate the small set of public product aliases to parser flags."""

    # The specialized spelling is an additive compatibility convenience.  It
    # must converge on the exact same ``--dedupe`` service; ``--all`` remains a
    # flag and is intentionally not introduced as a subcommand.
    if arguments and arguments[0] == "dedupe":
        return ["--dedupe", *arguments[1:]]
    # Keep the global path overrides in front of the translated selector so
    # ``Neocortex --root ROOT dedupe`` has the same precedence as the flat
    # spelling.  Only the two value-bearing global options are skipped here;
    # every other token remains the responsibility of argparse.
    position = 0
    while position < len(arguments):
        option, separator, value = arguments[position].partition("=")
        if option not in {"--root", "--state-directory"}:
            break
        if separator:
            if not value:
                break
            position += 1
            continue
        if position + 1 >= len(arguments):
            break
        position += 2
    if position < len(arguments) and arguments[position] == "dedupe":
        return [*arguments[:position], "--dedupe", *arguments[position + 1 :]]
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


def _run_human_mode(arguments: Sequence[str]) -> int | None:
    """Dispatch read-only human commands without importing route engines."""

    if not arguments or arguments[0] not in _HUMAN_COMMANDS:
        return None
    from neocortex.api.cli.human import run_human_command

    return run_human_command(arguments)


def entrypoint(arguments: Sequence[str] | None = None) -> int:
    """Run one public CLI invocation."""

    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    if _root_help_requested(forwarded):
        _print_root_help()
        return 0
    canonical_help = _canonical_help_requested(forwarded)
    if canonical_help is not None:
        _print_canonical_help(canonical_help)
        return 0
    forwarded = _translate_canonical_arguments(forwarded)
    try:
        human_exit_code = _run_human_mode(forwarded)
        if human_exit_code is not None:
            return human_exit_code
        from neocortex.api.cli.cli_app import main as run_cli

        return run_cli(forwarded)
    except SystemExit as exc:
        # Every parser has the same entrypoint result.
        if exc.code in (None, 0):
            return 0
        raise
    except ModuleNotFoundError as exc:
        if not exc.name or exc.name == "neocortex" or exc.name.startswith("neocortex."):
            raise
        print(
            f"No se puede ejecutar la operación solicitada: falta la dependencia Python {exc.name!r}. "
            "Instale los requisitos declarados para esta capacidad desde los recursos offline.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\nEjecución cancelada por el usuario.", file=sys.stderr)
        return 130


__all__ = ["_translate_canonical_arguments", "entrypoint"]
