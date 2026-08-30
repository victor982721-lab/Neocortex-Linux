"""Flat argument and validation contract for XLSX/PPTX/ODT CLI work."""

from __future__ import annotations
import argparse
from collections.abc import Callable
from collections.abc import Set as AbstractSet

__all__ = [
    "register_office_arguments",
    "validate_office_arguments",
    "validate_office_direct_operation",
]


# region [01] Stable flat argument registration


def register_office_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    """Append the existing flat Office group using its legacy size converter."""

    office = parser.add_argument_group("XLSX/PPTX/ODT route")
    office.add_argument(
        "--office-max-mb",
        dest="office_max_file_bytes",
        type=megabyte_type,
        default=None,
        metavar="MB",
        help="process only Office files at or below this decimal size",
    )
    office.add_argument(
        "--office-max-count",
        dest="office_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    office.add_argument(
        "--office-max-text-chars",
        type=int,
        default=20_000_000,
        help="reject an XLSX, PPTX or ODT whose extracted text exceeds this bound",
    )
    office.add_argument(
        "--retry-office-errors",
        action="store_true",
        help="force one new attempt for unchanged cached Office errors",
    )
    office.add_argument("--office-memory-budget-mb", type=int, default=512)
    office.add_argument("--office-min-free-memory-mb", type=int, default=1024)
    office.add_argument("--office-min-free-commit-mb", type=int, default=1024)
    office.add_argument("--office-memory-wait-timeout", type=float, default=60.0)
    office.add_argument("--office-search", metavar="QUERY")
    office.add_argument("--office-search-limit", type=int, default=20, metavar="N")


# endregion [01]


# region [02] Stable validation and error precedence


def validate_office_arguments(args: argparse.Namespace) -> None:
    """Validate Office route and query values in their established order."""

    if args.office_max_documents is not None and args.office_max_documents < 1:
        raise SystemExit("--office-max-count must be positive")
    if args.office_max_text_chars < 1:
        raise SystemExit("--office-max-text-chars must be positive")
    if args.office_memory_budget_mb < 1:
        raise SystemExit("--office-memory-budget-mb must be positive")
    if args.office_min_free_memory_mb < 0 or args.office_min_free_commit_mb < 0:
        raise SystemExit("Office memory headroom cannot be negative")
    if args.office_memory_wait_timeout < 0:
        raise SystemExit("--office-memory-wait-timeout cannot be negative")
    if not 1 <= args.office_search_limit <= 1000:
        raise SystemExit("--office-search-limit must be between 1 and 1000")
    if args.office_search is not None and not args.office_search.strip():
        raise SystemExit("--office-search must be non-empty")


def validate_office_direct_operation(
    args: argparse.Namespace,
    explicit: AbstractSet[str],
) -> None:
    """Validate Office direct-only options at their historical precedence point."""

    if "office_search_limit" in explicit and args.office_search is None:
        raise SystemExit("--office-search-limit requires --office-search")
    if args.office_search is not None and args.apply:
        raise SystemExit(
            "--office-search is read-only and cannot be combined with --apply"
        )


# endregion [02]
