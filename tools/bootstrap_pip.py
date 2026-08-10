#!/usr/bin/env python3
"""Seed exact pip from its authenticated wheel without invoking ambient pip."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from neocortex import pip_bootstrap


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="target interpreter path (defaults to the running interpreter)",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        help="optional already-downloaded canonical wheel for offline use",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    python = arguments.python.expanduser().absolute()
    try:
        if arguments.wheel is None:
            with tempfile.TemporaryDirectory(prefix="neocortex-pip-bootstrap-") as temporary:
                version = pip_bootstrap.bootstrap_python(python, Path(temporary))
        else:
            version = pip_bootstrap.seed_pip(
                python,
                arguments.wheel.expanduser().absolute(),
            )
    except (OSError, pip_bootstrap.PipBootstrapError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "pip": version,
                "python": os.fspath(python),
                "wheel_sha256": pip_bootstrap.PIP_BOOTSTRAP_SHA256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
