"""Hidden compatibility flags for canonical model lifecycle commands."""

from __future__ import annotations
import argparse
from pathlib import Path

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection


def register_models_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models-prepare", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--models-status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--models-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--models-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--models-model-id", action="append", help=argparse.SUPPRESS)


def validate_models_arguments(args: argparse.Namespace) -> None:
    explicit = set(getattr(args, "_explicit_options", ()))
    operations = selected_direct_operations(args, family=DirectOperationFamily.MODELS)
    if explicit.intersection({"models_json", "models_root", "models_model_id"}) and not operations:
        raise SystemExit("model lifecycle options require --models-prepare or --models-status")
    if operations and args.apply:
        raise SystemExit("model lifecycle commands reject --apply")
    if operations and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("model lifecycle commands cannot be combined with --route")


__all__ = ["register_models_arguments", "validate_models_arguments"]
