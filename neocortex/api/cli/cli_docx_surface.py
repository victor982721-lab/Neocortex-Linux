"""Flat argument and validation contract for the DOCX CLI route."""

from __future__ import annotations

import argparse
from collections.abc import Callable

__all__ = ["register_docx_arguments", "validate_docx_arguments"]


# region [01] Stable flat argument registration


def register_docx_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    """Append the existing flat DOCX group using its legacy size converter."""

    docx = parser.add_argument_group("DOCX route")
    docx.add_argument(
        "--docx-max-mb",
        dest="docx_max_file_bytes",
        type=megabyte_type,
        default=None,
        metavar="MB",
        help="process only DOCX files at or below this decimal size",
    )
    docx.add_argument(
        "--docx-max-count",
        dest="docx_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    docx.add_argument(
        "--docx-max-text-chars",
        type=int,
        default=20_000_000,
        help="reject a DOCX whose extracted text exceeds this bound",
    )
    docx.add_argument(
        "--retry-docx-errors",
        action="store_true",
        help="force one new attempt for unchanged cached DOCX errors",
    )
    docx.add_argument("--docx-memory-budget-mb", type=int, default=512)
    docx.add_argument("--docx-min-free-memory-mb", type=int, default=1024)
    docx.add_argument("--docx-min-free-commit-mb", type=int, default=1024)
    docx.add_argument("--docx-memory-wait-timeout", type=float, default=60.0)
    docx.add_argument("--docx-search", metavar="QUERY")
    docx.add_argument("--docx-search-limit", type=int, default=20)
    docx.add_argument("--docx-layout-groups", type=int, metavar="N")
    docx.add_argument(
        "--docx-missing-pdf",
        type=int,
        metavar="N",
        help="list up to N indexed DOCX files with no matching PDF",
    )


# endregion [01]


# region [02] Stable validation and error precedence


def validate_docx_arguments(args: argparse.Namespace) -> None:
    """Validate DOCX route and query values in their established order."""

    if args.docx_max_documents is not None and args.docx_max_documents < 1:
        raise SystemExit("--docx-max-count must be positive")
    if args.docx_max_text_chars < 1:
        raise SystemExit("--docx-max-text-chars must be positive")
    if args.docx_memory_budget_mb < 1:
        raise SystemExit("--docx-memory-budget-mb must be positive")
    if args.docx_min_free_memory_mb < 0 or args.docx_min_free_commit_mb < 0:
        raise SystemExit("DOCX memory headroom cannot be negative")
    if args.docx_memory_wait_timeout < 0:
        raise SystemExit("--docx-memory-wait-timeout cannot be negative")
    if not 1 <= args.docx_search_limit <= 1000:
        raise SystemExit("--docx-search-limit must be between 1 and 1000")
    if args.docx_layout_groups is not None and not 1 <= args.docx_layout_groups <= 100:
        raise SystemExit("--docx-layout-groups must be between 1 and 100")
    if args.docx_missing_pdf is not None and not 1 <= args.docx_missing_pdf <= 1000:
        raise SystemExit("--docx-missing-pdf must be between 1 and 1000")


# endregion [02]


from neocortex.platform import preserve_legacy_module as _preserve_legacy_module
_preserve_legacy_module(globals(), '_04_Nucleo_Operativo.cli_docx_surface')
