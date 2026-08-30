"""Hidden compatibility flags for the canonical platform doctor."""

from __future__ import annotations
import argparse

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection


def register_platform_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--doctor-platform", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--doctor-platform-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def validate_platform_arguments(args: argparse.Namespace) -> None:
    explicit = set(getattr(args, "_explicit_options", ()))
    operations = selected_direct_operations(args, family=DirectOperationFamily.PLATFORM)
    if "doctor_platform_json" in explicit and not operations:
        raise SystemExit("--doctor-platform-json requires --doctor-platform")
    if operations and args.apply:
        raise SystemExit("doctor platform is read-only and rejects --apply")
    if operations and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("doctor platform cannot be combined with --route")


__all__ = ["register_platform_arguments", "validate_platform_arguments"]
