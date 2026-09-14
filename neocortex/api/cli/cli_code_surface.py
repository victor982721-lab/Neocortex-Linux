"""CLI arguments for the user-facing Code knowledge capability."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from .cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.runtime.orchestration.route_selection import (
    BUILTIN_ROUTE_ORDER,
    normalize_route_selection,
)
from neocortex.runtime.config.third_party_policy import (
    DEFAULT_THIRD_PARTY_KINDS,
    THIRD_PARTY_ACTION_CHOICES,
    THIRD_PARTY_KIND_CHOICES,
    CodeThirdPartyPolicy,
)


def register_code_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    """Register bounded code ingestion and query options."""

    code = parser.add_argument_group("Code knowledge")
    code.add_argument(
        "--code-max-mb",
        dest="code_max_file_bytes",
        type=megabyte_type,
        default=8 * 1024 * 1024,
        metavar="MB",
        help="maximum bytes read from one code artifact",
    )
    code.add_argument("--code-max-count", dest="code_max_documents", type=int, metavar="N")
    code.add_argument("--code-max-text-chars", type=int, default=4_000_000)
    code.add_argument("--code-chunk-chars", type=int, default=12_000)
    code.add_argument("--code-complexity-warning", type=int, default=15, help=argparse.SUPPRESS)
    code.add_argument(
        "--code-function-lines-warning", type=int, default=200, help=argparse.SUPPRESS
    )
    code.add_argument(
        "--code-cache-validation",
        choices=("metadata", "full"),
        default="metadata",
        help="metadata is fast; full rechecks exact bytes before reuse",
    )
    code.add_argument(
        "--code-project-root",
        action="append",
        type=Path,
        default=None,
        metavar="PATH",
        help="replace the default project roots; repeat for each owned project",
    )
    code.add_argument(
        "--code-scope",
        dest="code_candidate_scope",
        choices=("projects", "broad"),
        default="projects",
        help="admit owned projects or all code-like inventory candidates",
    )
    code.add_argument(
        "--code-generated",
        dest="code_include_generated",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="include generated artifacts in structural representation",
    )
    code.add_argument(
        "--code-vendored",
        dest="code_include_vendored",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="include vendored artifacts in structural representation",
    )
    code.add_argument(
        "--code-third-party-action",
        choices=THIRD_PARTY_ACTION_CHOICES,
        default="keep",
        help=(
            "keep identified dependency/vendor/binary artifacts as advisory "
            "content (default), or request a bounded trash plan; the plan "
            "needs --apply to enact and an explicit Code route"
        ),
    )
    code.add_argument(
        "--code-third-party-min-confidence",
        type=float,
        default=0.95,
        metavar="SCORE",
        help="minimum classification confidence admitted to a third-party trash plan",
    )
    code.add_argument(
        "--code-third-party-max-actions",
        type=int,
        default=256,
        metavar="N",
        help="maximum third-party trash candidates admitted in one run",
    )
    code.add_argument(
        "--code-third-party-kind",
        dest="code_third_party_kinds",
        action="append",
        choices=THIRD_PARTY_KIND_CHOICES,
        default=None,
        metavar="KIND",
        help=(
            "third-party class admitted to an explicit trash plan; repeat to "
            "opt into generated, build_artifact or cache artifacts"
        ),
    )
    code.add_argument("--retry-code-errors", action="store_true")
    code.add_argument("--code-status", action="store_true", help="show the code index status")
    code.add_argument("--code-search", metavar="QUERY", help="search indexed code evidence")
    code.add_argument(
        "--code-search-mode",
        action="append",
        choices=(
            "literal",
            "fts",
            "path",
            "language",
            "symbol",
            "definition",
            "reference",
            "import",
            "dependency",
            "call",
            "signature",
            "diagnostic",
            "complexity",
            "semantic",
            "hybrid",
        ),
        help="repeat to combine lexical, structural and semantic channels",
    )
    code.add_argument("--code-search-limit", type=int, default=20, metavar="N")
    code.add_argument("--code-path", metavar="FRAGMENT")
    code.add_argument("--code-language")
    code.add_argument("--code-project")
    code.add_argument("--code-symbol")
    code.add_argument("--code-diagnostic")
    code.add_argument("--code-min-complexity", type=float)
    code.add_argument("--code-projects", action="store_true", help="list indexed code projects")
    code.add_argument("--code-reconstruct", metavar="PROJECT_OR_ID")
    code.add_argument(
        "--code-reconstruct-strategy",
        choices=("latest", "coherent", "branches"),
        default="coherent",
    )
    code.add_argument("--code-json", action="store_true")


def validate_code_arguments(args: argparse.Namespace) -> None:
    """Validate bounded product Code options and direct-operation exclusivity."""

    if args.code_max_file_bytes < 4096:
        raise SystemExit("--code-max-mb must be at least 0.004096")
    if args.code_max_documents is not None and args.code_max_documents < 1:
        raise SystemExit("--code-max-count must be positive")
    if args.code_max_text_chars < 1024:
        raise SystemExit("--code-max-text-chars must be at least 1024")
    if not 1024 <= args.code_chunk_chars <= 1_000_000:
        raise SystemExit("--code-chunk-chars must be between 1024 and 1000000")
    if args.code_complexity_warning < 1 or args.code_function_lines_warning < 1:
        raise SystemExit("code diagnostic thresholds must be positive")
    if args.code_search is not None:
        if not args.code_search.strip():
            raise SystemExit("--code-search must be non-empty")
        if len(args.code_search) > 4096:
            raise SystemExit("--code-search cannot exceed 4096 characters")
    if not 1 <= args.code_search_limit <= 1000:
        raise SystemExit("--code-search-limit must be between 1 and 1000")
    if args.code_min_complexity is not None and args.code_min_complexity < 0:
        raise SystemExit("--code-min-complexity cannot be negative")
    if args.code_search_mode and len(set(args.code_search_mode)) != len(args.code_search_mode):
        raise SystemExit("--code-search-mode values cannot be duplicated")

    explicit: set[str] = set(getattr(args, "_explicit_options", ()))
    search_options = {
        "code_search_mode",
        "code_search_limit",
        "code_path",
        "code_language",
        "code_project",
        "code_symbol",
        "code_diagnostic",
        "code_min_complexity",
    }
    if search_options.intersection(explicit) and args.code_search is None:
        raise SystemExit("code search filters require --code-search")
    if "code_reconstruct_strategy" in explicit and args.code_reconstruct is None:
        raise SystemExit("--code-reconstruct-strategy requires --code-reconstruct")
    code_direct = selected_direct_operations(args, family=DirectOperationFamily.CODE)
    if "code_json" in explicit and not code_direct:
        raise SystemExit("--code-json requires a direct code operation")
    if code_direct and args.apply:
        raise SystemExit("direct code operations are read-only and reject --apply")
    if code_direct and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("direct code operations cannot be combined with --route")

    _validate_explicit_root_project_scope(args, explicit, code_direct=bool(code_direct))
    _validate_third_party_arguments(args, explicit, code_direct=bool(code_direct))


def code_third_party_policy_from_args(args: argparse.Namespace) -> CodeThirdPartyPolicy:
    """Project parsed Code origin-cleanup switches into one immutable policy."""

    kinds = tuple(getattr(args, "code_third_party_kinds", None) or DEFAULT_THIRD_PARTY_KINDS)
    return CodeThirdPartyPolicy(
        action=args.code_third_party_action,
        min_confidence=args.code_third_party_min_confidence,
        max_actions=args.code_third_party_max_actions,
        kinds=kinds,
    )


def _validate_third_party_arguments(
    args: argparse.Namespace,
    explicit: set[str],
    *,
    code_direct: bool,
) -> None:
    """Keep third-party effects opt-in and separate from read-only Code queries."""

    if not 0.0 <= args.code_third_party_min_confidence <= 1.0:
        raise SystemExit("--code-third-party-min-confidence must be between 0 and 1")
    if not 1 <= args.code_third_party_max_actions <= 10_000:
        raise SystemExit("--code-third-party-max-actions must be between 1 and 10000")
    kinds = tuple(args.code_third_party_kinds or ())
    if len(kinds) != len(set(kinds)):
        raise SystemExit("--code-third-party-kind values must be unique")

    policy_requested = bool(
        {
            "code_third_party_action",
            "code_third_party_min_confidence",
            "code_third_party_max_actions",
            "code_third_party_kinds",
        }
        & explicit
    )
    selected_routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    code_selected = bool(args.all or "code" in selected_routes)
    if code_direct and policy_requested:
        raise SystemExit("third-party Code policy cannot be combined with direct Code queries")
    if args.dedupe and policy_requested:
        raise SystemExit("third-party Code policy cannot be combined with --dedupe")
    if policy_requested and not code_selected and not args.route_only and args.resume_run is None:
        raise SystemExit("third-party Code policy requires --all or --route code")
    if args.code_third_party_action == "trash":
        if not code_selected:
            raise SystemExit("--code-third-party-action trash requires --all or --route code")
        if args.route_only or args.resume_run is not None:
            raise SystemExit("third-party Code trash is unavailable with --route-only/--resume-run")
        if "root" not in explicit:
            raise SystemExit(
                "--code-third-party-action trash requires an explicit --root"
            )
        if "code_project_root" not in explicit:
            raise SystemExit(
                "--code-third-party-action trash requires at least one --code-project-root"
            )


def _validate_explicit_root_project_scope(
    args: argparse.Namespace,
    explicit: set[str],
    *,
    code_direct: bool,
) -> None:
    """Reject an explicit Code route that is provably going to be a no-op.

    A full ``--all`` run may legitimately continue to report failures from
    other routes, so only an explicitly selected ``--code-scope projects`` is
    rejected during argument validation.  This keeps the broad integrated
    command's established failure ordering while making the focused Code
    command fail closed before it creates inventory or Code state.
    """

    if code_direct or "root" not in explicit:
        return
    if args.code_candidate_scope != "projects":
        return
    if args.route_only or args.resume_run is not None:
        return

    routes = normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER)
    code_selected = "code" in routes
    if args.all:
        # ``--all`` uses the default project scope unless the user opts into
        # it explicitly.  Do not preempt unrelated route diagnostics for the
        # legacy default; an explicitly requested project scope is safe to
        # reject here because its no-candidate outcome is deterministic.
        code_selected = code_selected and "code_candidate_scope" in explicit
    if not code_selected:
        return

    from neocortex.runtime.config.app_paths import default_code_project_roots

    configured_roots = (
        default_code_project_roots()
        if args.code_project_root is None
        else tuple(args.code_project_root)
    )
    from .cli_code import _code_scope_feedback

    feedback = _code_scope_feedback(
        root=args.root,
        project_roots=configured_roots,
        candidate_scope=args.code_candidate_scope,
    )
    if feedback is not None:
        raise SystemExit(feedback["message"])


__all__ = [
    "code_third_party_policy_from_args",
    "register_code_arguments",
    "validate_code_arguments",
]
