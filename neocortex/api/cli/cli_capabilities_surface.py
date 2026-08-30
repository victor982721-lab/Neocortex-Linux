"""Hidden flat compatibility contract for the canonical capabilities doctor."""


# region [01] Lightweight imports and public contract

from __future__ import annotations
import argparse
import re

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection

__all__ = [
    "register_capabilities_arguments",
    "validate_capabilities_arguments",
]

_EXACT_MIME_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")

# endregion [01]


# region [02] Hidden flat compatibility flags


def register_capabilities_arguments(parser: argparse.ArgumentParser) -> None:
    """Register internal flags used after canonical argv translation."""

    parser.add_argument(
        "--doctor-capabilities",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--doctor-capabilities-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--doctor-capabilities-select",
        metavar="CAPABILITY",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--doctor-capabilities-mime-type",
        metavar="MIME",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--doctor-capabilities-input-bytes",
        type=int,
        metavar="BYTES",
        help=argparse.SUPPRESS,
    )


# endregion [02]


# region [03] Stable validation and safety boundary


def validate_capabilities_arguments(args: argparse.Namespace) -> None:
    """Validate the internal capabilities operation without touching runtime state."""

    explicit = set(getattr(args, "_explicit_options", ()))
    operations = selected_direct_operations(
        args,
        family=DirectOperationFamily.CAPABILITIES,
    )
    if "doctor_capabilities_json" in explicit and not operations:
        raise SystemExit("--doctor-capabilities-json requires --doctor-capabilities")
    selection_options = {
        "doctor_capabilities_select",
        "doctor_capabilities_mime_type",
        "doctor_capabilities_input_bytes",
    }
    for destination in sorted(selection_options & explicit):
        if not operations:
            option = destination.replace("_", "-")
            raise SystemExit(f"--{option} requires --doctor-capabilities")
    if operations and args.apply:
        raise SystemExit("doctor capabilities is read-only and rejects --apply")
    if operations and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("doctor capabilities cannot be combined with --route")

    selected = args.doctor_capabilities_select
    mime_type = args.doctor_capabilities_mime_type
    input_bytes = args.doctor_capabilities_input_bytes
    if selected is None:
        if "doctor_capabilities_mime_type" in explicit:
            raise SystemExit(
                "--doctor-capabilities-mime-type requires --doctor-capabilities-select"
            )
        if "doctor_capabilities_input_bytes" in explicit:
            raise SystemExit(
                "--doctor-capabilities-input-bytes requires --doctor-capabilities-select"
            )
        return
    if selected != "text.extract":
        raise SystemExit("--doctor-capabilities-select currently supports only text.extract")
    if mime_type is None:
        raise SystemExit("--doctor-capabilities-select requires --doctor-capabilities-mime-type")
    if _EXACT_MIME_PATTERN.fullmatch(mime_type) is None:
        raise SystemExit("--doctor-capabilities-mime-type must be an exact MIME type")
    if input_bytes is None:
        raise SystemExit("--doctor-capabilities-select requires --doctor-capabilities-input-bytes")
    if input_bytes < 0:
        raise SystemExit("--doctor-capabilities-input-bytes cannot be negative")


# endregion [03]
