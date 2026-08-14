"""Flat argument and validation contract for structured-code CLI operations."""


# region [01] Lightweight imports and public contract

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from .cli_operations import DirectOperationFamily, selected_direct_operations
from .route_selection import BUILTIN_ROUTE_ORDER, normalize_route_selection

__all__ = ["register_code_arguments", "validate_code_arguments"]

# endregion [01]


# region [02] Stable flat argument registration


def register_code_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    """Append the existing flat Code group using its legacy size converter."""

    code = parser.add_argument_group("Structured source-code intelligence route")
    code.add_argument(
        "--code-max-mb",
        dest="code_max_file_bytes",
        type=megabyte_type,
        default=8 * 1024 * 1024,
        metavar="MB",
        help="analyze only textual artifacts at or below this bounded size",
    )
    code.add_argument(
        "--code-max-count",
        dest="code_max_documents",
        type=int,
        metavar="N",
    )
    code.add_argument("--code-max-text-chars", type=int, default=4_000_000)
    code.add_argument("--code-chunk-chars", type=int, default=12_000)
    code.add_argument("--code-complexity-warning", type=int, default=15)
    code.add_argument("--code-function-lines-warning", type=int, default=200)
    code.add_argument(
        "--code-cache-validation",
        choices=("metadata", "full"),
        default="metadata",
        help="metadata is incremental-fast; full rechecks exact bytes before reuse",
    )
    code.add_argument(
        "--code-project-root",
        action="append",
        type=Path,
        default=None,
        metavar="PATH",
        help="replace default Code project allowlist; repeat for each owned project",
    )
    code.add_argument(
        "--code-scope",
        dest="code_candidate_scope",
        choices=("projects", "broad"),
        default="projects",
        help=(
            "projects admits only explicit project roots; broad restores the "
            "legacy profile-wide textual selection"
        ),
    )
    code.add_argument(
        "--code-generated",
        dest="code_include_generated",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="retain generated artifacts in structural analysis",
    )
    code.add_argument(
        "--code-vendored",
        dest="code_include_vendored",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="retain vendored artifacts in structural analysis",
    )
    code.add_argument(
        "--retry-code-errors",
        action="store_true",
        help="retry unchanged partial or failed code observations",
    )
    code.add_argument(
        "--code-status",
        action="store_true",
        help="show bounded code database, analyzer and index status",
    )
    code.add_argument(
        "--code-review",
        action="store_true",
        help=(
            "show deterministic read-only structural observations, open questions "
            "and the underlying hotspot ranking"
        ),
    )
    code.add_argument(
        "--code-validate-change",
        action="store_true",
        help=(
            "run the canonical local Linux validation: Git change, affected tests, "
            "static and architecture gates, trusted-deep review, experiments, "
            "installed candidate wheel, replay and one fail-closed verdict"
        ),
    )
    code.add_argument(
        "--code-validation-baseline",
        default="HEAD",
        metavar="GIT_REVISION",
        help="Git baseline used only by --code-validate-change (default HEAD)",
    )
    code.add_argument(
        "--code-validation-max-tests",
        type=int,
        default=5000,
        metavar="N",
        help="maximum tests admitted by diff-aware trusted-deep coverage (1..5000)",
    )
    code.add_argument(
        "--code-validation-time-budget-seconds",
        type=int,
        default=900,
        metavar="SECONDS",
        help="hard affected-test coverage budget (30..900 seconds; default 900)",
    )
    code.add_argument(
        "--code-review-limit",
        type=int,
        default=10,
        metavar="N",
        help=(
            "bound each review surface to 1 to 50 observations; values above 10 require --code-json"
        ),
    )
    code.add_argument(
        "--code-experiment-run",
        metavar="PROPOSAL_ID",
        help=(
            "execute one exact allow-listed experiment proposal from the current "
            "code-review plan in isolated temporary state"
        ),
    )
    code.add_argument(
        "--code-publication-diff",
        metavar="BASELINE_STATE",
        help=(
            "compare the current completed Code publication with a baseline "
            "state without writing either owner"
        ),
    )
    code.add_argument("--code-search", metavar="QUERY")
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
        help="repeat to fuse complementary exact and structural search modes",
    )
    code.add_argument("--code-search-limit", type=int, default=20, metavar="N")
    code.add_argument("--code-path", metavar="FRAGMENT")
    code.add_argument("--code-language")
    code.add_argument("--code-project")
    code.add_argument("--code-symbol")
    code.add_argument("--code-diagnostic")
    code.add_argument("--code-min-complexity", type=float)
    code.add_argument(
        "--code-projects",
        action="store_true",
        help="list inferred project instances without touching source files",
    )
    code.add_argument("--code-reconstruct", metavar="PROJECT_OR_ID")
    code.add_argument(
        "--code-reconstruct-strategy",
        choices=("latest", "coherent", "branches"),
        default="coherent",
    )
    code.add_argument(
        "--code-doctor",
        action="store_true",
        help="verify code schema, FTS and analyzer availability without analysis",
    )
    code.add_argument(
        "--code-query",
        choices=("status", "review", "diff"),
        help="query published Code analysis through one bounded read-only surface",
    )
    for option in (
        "provider",
        "category",
        "module",
        "status",
        "delta",
        "work-package",
    ):
        code.add_argument(
            f"--code-query-{option}",
            action="append",
            metavar="VALUE",
            help="repeat to add an exact Code analysis query filter",
        )
    code.add_argument(
        "--code-query-limit",
        type=int,
        default=50,
        metavar="N",
        help="return between 1 and 500 bounded query rows",
    )
    code.add_argument(
        "--code-query-baseline",
        metavar="BASELINE_STATE",
        help="baseline state required only by --code-query diff",
    )
    code.add_argument("--code-json", action="store_true")


# endregion [02]


# region [03] Stable validation and error precedence


def _validate_code_review_limit(args: argparse.Namespace) -> None:
    if not 1 <= args.code_review_limit <= 50:
        raise SystemExit("--code-review-limit must be between 1 and 50")


def _validate_code_review_selection(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    if "code_review_limit" in explicit and not args.code_review:
        raise SystemExit("--code-review-limit requires --code-review")
    if args.code_review and args.code_review_limit > 10 and not args.code_json:
        raise SystemExit("--code-review-limit above 10 requires --code-json")
    if args.code_experiment_run is not None:
        if not args.code_experiment_run.strip():
            raise SystemExit("--code-experiment-run must be non-empty")
        if len(args.code_experiment_run) > 1024:
            raise SystemExit("--code-experiment-run cannot exceed 1024 characters")


def _validate_change_validation(args: argparse.Namespace, explicit: set[str]) -> None:
    controls = {
        "code_validation_baseline",
        "code_validation_max_tests",
        "code_validation_time_budget_seconds",
    }
    if controls.intersection(explicit) and not args.code_validate_change:
        raise SystemExit("code validation controls require --code-validate-change")
    baseline = args.code_validation_baseline
    if (
        not isinstance(baseline, str)
        or not baseline
        or baseline.strip() != baseline
        or len(baseline.encode("utf-8")) > 1024
        or baseline.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in baseline)
    ):
        raise SystemExit("--code-validation-baseline is invalid")
    if not 1 <= args.code_validation_max_tests <= 5000:
        raise SystemExit("--code-validation-max-tests must be between 1 and 5000")
    if not 30 <= args.code_validation_time_budget_seconds <= 900:
        raise SystemExit("--code-validation-time-budget-seconds must be between 30 and 900")


def _validate_code_query_selection(
    args: argparse.Namespace,
    explicit: set[str],
) -> None:
    query_options = {
        "code_query_provider",
        "code_query_category",
        "code_query_module",
        "code_query_status",
        "code_query_delta",
        "code_query_work_package",
        "code_query_limit",
        "code_query_baseline",
    }
    if query_options.intersection(explicit) and args.code_query is None:
        raise SystemExit("code query filters, limit and baseline require --code-query")
    if not 1 <= args.code_query_limit <= 500:
        raise SystemExit("--code-query-limit must be between 1 and 500")
    if args.code_query == "diff":
        if args.code_query_baseline is None:
            raise SystemExit("--code-query diff requires --code-query-baseline")
        if not args.code_query_baseline.strip():
            raise SystemExit("--code-query-baseline must be non-empty")
    elif args.code_query_baseline is not None:
        raise SystemExit("--code-query-baseline is only valid with --code-query diff")


def validate_code_arguments(args: argparse.Namespace) -> None:
    """Validate Code route and direct selections in the established order."""

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
    _validate_code_review_limit(args)
    if args.code_min_complexity is not None and args.code_min_complexity < 0:
        raise SystemExit("--code-min-complexity cannot be negative")
    if args.code_search_mode and len(set(args.code_search_mode)) != len(args.code_search_mode):
        raise SystemExit("--code-search-mode values cannot be duplicated")

    explicit: set[str] = set(getattr(args, "_explicit_options", ()))
    _validate_code_review_selection(args, explicit)
    _validate_change_validation(args, explicit)
    _validate_code_query_selection(args, explicit)
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
    if args.code_query is not None and any(
        operation.destination != "code_query" for operation in code_direct
    ):
        raise SystemExit("--code-query cannot be combined with another direct code operation")
    if "code_json" in explicit and not code_direct:
        raise SystemExit("--code-json requires a direct code operation")
    if code_direct and args.apply:
        raise SystemExit("direct code operations are read-only and reject --apply")
    if code_direct and normalize_route_selection(args.route, BUILTIN_ROUTE_ORDER):
        raise SystemExit("direct code operations cannot be combined with --route")


# endregion [03]
