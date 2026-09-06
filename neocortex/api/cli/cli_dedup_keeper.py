"""Explicit, bounded keeper preferences for the normal inventory/plan route."""

from __future__ import annotations

import argparse
from pathlib import Path

from .cli_operations import selected_direct_operations

MAX_KEEPER_SELECTORS = 256


def register_dedup_keeper_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Duplicate keeper preferences (advisory only)")
    group.add_argument(
        "--dedup-keep",
        type=Path,
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "prefer the current physical inventory identity of FILE as keeper; repeatable, "
            "conflicting keeps within one content group fail closed; never authorizes deletion"
        ),
    )
    group.add_argument(
        "--dedup-prefer-root",
        type=Path,
        action="append",
        default=[],
        metavar="DIRECTORY",
        help=(
            "prefer inventoried files under a verified DIRECTORY inside --root; repeatable "
            "in priority order, after explicit --dedup-keep decisions"
        ),
    )


def validate_dedup_keeper_arguments(args: argparse.Namespace) -> None:
    keeps = getattr(args, "dedup_keep", ()) or ()
    roots = getattr(args, "dedup_prefer_root", ()) or ()
    if len(keeps) + len(roots) > MAX_KEEPER_SELECTORS:
        raise SystemExit(
            f"duplicate keeper preferences allow at most {MAX_KEEPER_SELECTORS} selectors"
        )
    if not keeps and not roots:
        return
    if any("\0" in str(value) for value in (*keeps, *roots)):
        raise SystemExit("duplicate keeper paths cannot contain NUL")
    if selected_direct_operations(args):
        raise SystemExit(
            "--dedup-keep/--dedup-prefer-root require an inventory and duplicate-plan run"
        )
    if any(getattr(args, name, None) for name in ("route_only", "candidate_run", "resume_run")):
        raise SystemExit(
            "duplicate keeper preferences cannot be ignored by route-only/resume operations"
        )


__all__ = ["register_dedup_keeper_arguments", "validate_dedup_keeper_arguments"]
