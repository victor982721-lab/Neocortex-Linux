"""Flat argument and validation contract for the read-only Knowledge CLI."""


# region [01] Lightweight imports and public contract

from __future__ import annotations
import argparse

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection

__all__ = ["register_knowledge_arguments", "validate_knowledge_arguments"]

# endregion [01]


# region [02] Stable flat argument registration


def register_knowledge_arguments(parser: argparse.ArgumentParser) -> None:
    """Append the existing flat Knowledge group without changing its contract."""

    knowledge = parser.add_argument_group("Read-only Knowledge Plane")
    knowledge.add_argument(
        "--knowledge-status",
        action="store_true",
        help="show a bounded cross-owner logical snapshot without creating state",
    )
    knowledge.add_argument(
        "--knowledge-health",
        metavar="RESOURCE_ID",
        help="explain one stable asset identity across its published owner facts",
    )
    knowledge.add_argument(
        "--knowledge-search",
        metavar="QUERY",
        help="search available lexical, semantic, structural and catalog evidence",
    )
    knowledge.add_argument(
        "--knowledge-context",
        metavar="QUERY",
        help="build a bounded cited context from one read-only Knowledge search",
    )
    knowledge.add_argument("--knowledge-json", action="store_true")
    knowledge.add_argument("--knowledge-limit", type=int, default=20, metavar="N")
    knowledge.add_argument(
        "--knowledge-context-characters",
        type=int,
        default=12_000,
        metavar="N",
        help=(
            "maximum ContextBundle characters; defaults to 12000 "
            "(allowed range: 1..1000000)"
        ),
    )
    knowledge.add_argument("--knowledge-history", action="store_true")
    knowledge.add_argument(
        "--knowledge-mode",
        choices=("discovery", "evidence"),
        default="evidence",
    )


# endregion [02]


# region [03] Stable validation and error precedence


def validate_knowledge_arguments(args: argparse.Namespace) -> None:
    """Validate one Knowledge selection using the established error order."""

    explicit = set(getattr(args, "_explicit_options", ()))
    operations = selected_direct_operations(
        args,
        family=DirectOperationFamily.KNOWLEDGE,
    )
    if len(operations) > 1:
        raise SystemExit("Knowledge direct actions are mutually exclusive")
    if args.knowledge_health is not None:
        if not args.knowledge_health.strip():
            raise SystemExit("--knowledge-health must be non-empty")
        if len(args.knowledge_health) > 512:
            raise SystemExit("--knowledge-health cannot exceed 512 characters")
    for name in ("knowledge_search", "knowledge_context"):
        value = getattr(args, name)
        if value is not None and not value.strip():
            raise SystemExit(f"--{name.replace('_', '-')} must be non-empty")
        if value is not None and len(value) > 4_096:
            raise SystemExit(
                f"--{name.replace('_', '-')} cannot exceed 4096 characters"
            )
    if not 1 <= args.knowledge_limit <= 1_000:
        raise SystemExit("--knowledge-limit must be between 1 and 1000")
    if args.knowledge_context is not None and args.knowledge_limit > 100:
        raise SystemExit(
            "--knowledge-limit must be between 1 and 100 for --knowledge-context"
        )
    if not 1 <= args.knowledge_context_characters <= 1_000_000:
        raise SystemExit("--knowledge-context-characters must be between 1 and 1000000")
    if "knowledge_context_characters" in explicit and args.knowledge_context is None:
        raise SystemExit("--knowledge-context-characters requires --knowledge-context")
    optional = {
        "knowledge_context_characters",
        "knowledge_json",
        "knowledge_limit",
        "knowledge_history",
        "knowledge_mode",
    }
    if not operations and optional.intersection(explicit):
        raise SystemExit("Knowledge options require one Knowledge direct action")
    query_selected = (
        args.knowledge_search is not None or args.knowledge_context is not None
    )
    query_only = {"knowledge_limit", "knowledge_history", "knowledge_mode"}
    if query_only.intersection(explicit) and not query_selected:
        raise SystemExit("Knowledge limit, history and mode require search or context")
    if operations and args.apply:
        raise SystemExit("Knowledge operations are read-only and reject --apply")
    if operations and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("Knowledge operations cannot be combined with --route")


# endregion [03]
