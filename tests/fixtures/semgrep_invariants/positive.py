"""Deliberate violations used to exercise the local Semgrep ruleset."""

import os
import runpy
import shutil
import sqlite3
import subprocess


def shell_violation(command: str) -> None:
    subprocess.run(command, shell=True, timeout=5)


def eval_violation(source: str) -> object:
    return eval(source)


def os_system_violation(command: str) -> int:
    return os.system(command)


def corpus_execution_violation(path: str) -> dict[str, object]:
    return runpy.run_path(path)


def sqlite_violation(database: str) -> sqlite3.Connection:
    return sqlite3.connect(database)


def subprocess_limit_violation(command: list[str]) -> None:
    subprocess.run(command)


def mutation_violation(source: str, target: str) -> None:
    shutil.move(source, target)


def _semantic_owner_lease(path, timeout):
    from neocortex.persistence.sqlite_paths import existing_sqlite_uri
    from neocortex.persistence.sqlite_writer_snapshot import SQLiteProgressConnection
    safe = sqlite3.connect(
        existing_sqlite_uri(path), uri=True, timeout=timeout,
        factory=SQLiteProgressConnection,
    )
    # A second unbounded open inside the allowed helper must not be hidden.
    unsafe = sqlite3.connect(path)
    return safe, unsafe


def public_reader_using_lease_shape(path, timeout):
    from neocortex.persistence.sqlite_paths import existing_sqlite_uri
    from neocortex.persistence.sqlite_writer_snapshot import SQLiteProgressConnection
    # Safe-looking syntax outside the authenticated owner seam is not enough.
    return sqlite3.connect(
        existing_sqlite_uri(path), uri=True, timeout=timeout,
        factory=SQLiteProgressConnection,
    )
