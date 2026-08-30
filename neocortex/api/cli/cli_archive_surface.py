"""Flat CLI arguments and validation for recursive ZIP indexing and queries."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import argparse
from collections.abc import Callable, Set

from .cli_operations import DirectOperationFamily, selected_direct_operations

__all__ = (
    "register_archive_arguments",
    "validate_archive_arguments",
    "validate_archive_direct_operation",
)


def register_archive_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    archive = parser.add_argument_group("ZIP archive route (including nested ZIPs)")
    archive.add_argument(
        "--archive-max-mb",
        dest="archive_max_file_bytes",
        type=megabyte_type,
        default=None,
        metavar="MB",
        help="index only top-level ZIP files at or below this decimal size",
    )
    archive.add_argument(
        "--archive-max-count",
        dest="archive_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    archive.add_argument("--archive-max-depth", type=int, default=5, metavar="N")
    archive.add_argument("--archive-max-members", type=int, default=20_000, metavar="N")
    archive.add_argument(
        "--archive-max-central-directory-mb",
        type=int,
        default=32,
        metavar="MiB",
    )
    archive.add_argument("--archive-max-member-mb", type=int, default=64, metavar="MiB")
    archive.add_argument("--archive-max-total-mb", type=int, default=512, metavar="MiB")
    archive.add_argument(
        "--archive-max-text-chars",
        type=int,
        default=2_000_000,
        metavar="N",
    )
    archive.add_argument(
        "--archive-max-total-text-chars",
        type=int,
        default=20_000_000,
        metavar="N",
    )
    archive.add_argument(
        "--archive-max-compression-ratio",
        type=float,
        default=200.0,
        metavar="RATIO",
    )
    archive.add_argument("--archive-pdf-max-pages", type=int, default=500, metavar="N")
    archive.add_argument(
        "--archive-pdf-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
    )
    archive.add_argument(
        "--archive-pdf-worker-memory-mb",
        type=int,
        default=768,
        metavar="MiB",
    )
    archive.add_argument(
        "--retry-archive-errors",
        action="store_true",
        help="retry unchanged top-level ZIPs whose prior traversal failed",
    )
    direct = archive.add_mutually_exclusive_group()
    direct.add_argument(
        "--archive-status",
        action="store_true",
        help="show read-only ZIP index coverage without scanning",
    )
    direct.add_argument("--archive-search", metavar="QUERY")
    direct.add_argument(
        "--archive-list",
        type=int,
        metavar="N",
        help="list up to N indexed members in stable archive order",
    )
    archive.add_argument("--archive-search-limit", type=int, default=20, metavar="N")
    archive.add_argument(
        "--archive-container",
        metavar="PATH_FRAGMENT",
        help="restrict an archive search or listing to one container path fragment",
    )
    archive.add_argument("--archive-json", action="store_true")


def validate_archive_arguments(args: argparse.Namespace) -> None:
    if args.archive_max_documents is not None and args.archive_max_documents < 1:
        raise SystemExit("--archive-max-count must be positive")
    for name in (
        "archive_max_depth",
        "archive_max_members",
        "archive_max_central_directory_mb",
        "archive_max_member_mb",
        "archive_max_total_mb",
        "archive_max_text_chars",
        "archive_max_total_text_chars",
        "archive_max_compression_ratio",
        "archive_pdf_max_pages",
        "archive_pdf_timeout",
        "archive_pdf_worker_memory_mb",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.archive_max_depth > 20:
        raise SystemExit("--archive-max-depth cannot exceed 20")
    if args.archive_max_members > 1_000_000:
        raise SystemExit("--archive-max-members cannot exceed 1000000")
    if not 1 <= args.archive_search_limit <= 1_000:
        raise SystemExit("--archive-search-limit must be between 1 and 1000")
    if args.archive_list is not None and not 1 <= args.archive_list <= 1_000:
        raise SystemExit("--archive-list must be between 1 and 1000")
    if args.archive_search is not None and not args.archive_search.strip():
        raise SystemExit("--archive-search must be non-empty")
    if args.archive_container is not None and not args.archive_container.strip():
        raise SystemExit("--archive-container must be non-empty")


def validate_archive_direct_operation(
    args: argparse.Namespace,
    explicit: Set[str],
) -> None:
    actions = selected_direct_operations(args, family=DirectOperationFamily.ARCHIVE)
    if "archive_search_limit" in explicit and args.archive_search is None:
        raise SystemExit("--archive-search-limit requires --archive-search")
    if args.archive_container is not None and not (
        args.archive_search is not None or args.archive_list is not None
    ):
        raise SystemExit("--archive-container requires --archive-search or --archive-list")
    if args.archive_json and not actions:
        raise SystemExit("--archive-json requires an archive status, search or list operation")
    if not actions:
        return
    if args.apply:
        raise SystemExit("archive direct operations are read-only and cannot use --apply")
    if args.route != "none":
        raise SystemExit("archive direct operations cannot be combined with --route")


_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_archive_surface')
