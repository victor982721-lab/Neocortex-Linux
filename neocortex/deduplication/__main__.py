"""Compatibility command that delegates to the integrated NeoCortex CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neocortex.platform.policy import default_corpus_root

from neocortex.runtime.config.app_paths import default_state_directory


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compatibility wrapper for integrated, non-destructive NeoCortex "
            "inventory and deduplication"
        )
    )
    parser.add_argument("--root", type=Path, default=default_corpus_root())
    parser.add_argument(
        "--state-database",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--show-groups", type=int, default=0, metavar="N")
    args = parser.parse_args(arguments)
    if args.show_groups < 0:
        parser.error("--show-groups cannot be negative")
    state_directory = default_state_directory()
    if args.state_database is not None:
        database = args.state_database.expanduser().absolute()
        if database.name.casefold() != "dedup.sqlite3":
            parser.error(
                "legacy --state-database must name dedup.sqlite3; use the "
                "integrated --state-directory option with Neocortex"
            )
        state_directory = database.parent
    print(
        "El comando de deduplicación aislado está obsoleto; use Neocortex con los mismos argumentos.",
        file=sys.stderr,
    )
    from neocortex.api.cli.cli_app import main as run_integrated

    return run_integrated(
        (
            "--root",
            str(args.root),
            "--state-directory",
            str(state_directory),
            "--show-groups",
            str(args.show_groups),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
