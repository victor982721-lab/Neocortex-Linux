"""Hidden compatibility flags for canonical model lifecycle commands."""

from __future__ import annotations

import argparse

from .cli_operations import DirectOperationFamily, selected_direct_operations
from .route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection


def register_models_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models-prepare", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--models-status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--models-json", action="store_true", help=argparse.SUPPRESS)


def validate_models_arguments(args: argparse.Namespace) -> None:
    explicit = set(getattr(args, "_explicit_options", ()))
    operations = selected_direct_operations(args, family=DirectOperationFamily.MODELS)
    if "models_json" in explicit and not operations:
        raise SystemExit("--models-json requires --models-prepare or --models-status")
    if operations and args.apply:
        raise SystemExit("model lifecycle commands reject --apply")
    if operations and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("model lifecycle commands cannot be combined with --route")


__all__ = ["register_models_arguments", "validate_models_arguments"]
