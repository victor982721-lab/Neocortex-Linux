"""Allowed shapes for the local Semgrep ruleset."""

import sqlite3
import subprocess
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def grant_fence(grant: object, fence: object) -> Iterator[None]:
    """Fixture-only marker for an already validated mutation boundary."""

    if grant is None or fence is None:
        raise ValueError("fixture requires both grant and fence")
    yield


def bounded_process(command: list[str]) -> None:
    subprocess.run(command, timeout=5, check=True)


def bounded_popen(command: list[str], limiter: object) -> subprocess.Popen[str]:
    return subprocess.Popen(command, preexec_fn=limiter)


def fenced_mutation(source: str, target: str, grant: object, fence: object) -> None:
    with grant_fence(grant, fence):
        # The context is the explicit policy boundary, not a runtime bypass.
        import shutil

        shutil.move(source, target)


def private_memory_database() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")
