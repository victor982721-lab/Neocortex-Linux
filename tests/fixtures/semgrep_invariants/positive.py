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
