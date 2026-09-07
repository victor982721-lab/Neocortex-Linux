"""Hidden flat compatibility flags for the configuration doctor."""

from __future__ import annotations

import argparse

from .cli_operations import selected_direct_operations
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)

__all__ = ["register_config_doctor_arguments", "validate_config_doctor_arguments"]


def register_config_doctor_arguments(parser: argparse.ArgumentParser) -> None:
    """Register hidden flags used by the canonical and flat doctor forms."""

    parser.add_argument("--doctor-config", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--doctor-config-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def validate_config_doctor_arguments(args: argparse.Namespace) -> None:
    """Validate the read-only configuration operation before dispatch."""

    explicit = set(getattr(args, "_explicit_options", ()))
    config_selected = bool(getattr(args, "doctor_config", False))
    direct_operations = selected_direct_operations(args)
    if "doctor_config_json" in explicit and not config_selected:
        raise SystemExit("--doctor-config-json requires --doctor-config")
    if config_selected and direct_operations:
        raise SystemExit("doctor config cannot be combined with another direct operation")
    if config_selected and getattr(args, "all", False):
        raise SystemExit("doctor config cannot be combined with --all")
    if config_selected and args.apply:
        raise SystemExit("doctor config is read-only and rejects --apply")
    if config_selected and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("doctor config cannot be combined with --route")
