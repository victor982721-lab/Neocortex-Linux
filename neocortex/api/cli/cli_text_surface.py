"""Flat CLI arguments and validation for generic physical text extraction."""

from __future__ import annotations
import argparse
from collections.abc import Callable

__all__ = ("register_text_arguments", "validate_text_arguments")


def register_text_arguments(
    parser: argparse.ArgumentParser,
    *,
    megabyte_type: Callable[[str], int],
) -> None:
    text = parser.add_argument_group("Generic text, email, and Office conversion route")
    text.add_argument(
        "--text-max-mb",
        dest="text_max_file_bytes",
        type=megabyte_type,
        default=64 * 1024 * 1024,
        metavar="MB",
        help="extract physical text files no larger than this bound",
    )
    text.add_argument(
        "--text-max-count",
        dest="text_max_documents",
        type=int,
        default=None,
        metavar="N",
    )
    text.add_argument("--text-max-chars", type=int, default=4_000_000, metavar="N")
    text.add_argument(
        "--text-worker-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
    )
    text.add_argument(
        "--text-worker-memory-mb",
        type=int,
        default=1024,
        metavar="MiB",
    )
    text.add_argument(
        "--retry-text-errors",
        action="store_true",
        help="retry unchanged generic-text files whose prior extraction failed",
    )


def validate_text_arguments(args: argparse.Namespace) -> None:
    if args.text_max_documents is not None and args.text_max_documents < 1:
        raise SystemExit("--text-max-count must be positive")
    for name in (
        "text_max_file_bytes",
        "text_max_chars",
        "text_worker_timeout",
        "text_worker_memory_mb",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
