"""Desktop command arguments that can be inspected without a Qt runtime."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from neocortex.platform.policy import default_corpus_root
from neocortex.runtime.config.app_paths import default_state_directory


def parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    """Parse the public desktop options before importing Qt or creating a display."""

    parser = argparse.ArgumentParser(prog="Neocortex --ui", allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=default_corpus_root())
    parser.add_argument("--state-directory", type=Path, default=default_state_directory())
    return parser.parse_args(list(arguments))
