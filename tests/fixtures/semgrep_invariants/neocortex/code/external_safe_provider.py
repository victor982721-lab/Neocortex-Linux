"""Negative fixture for Neocortex's local Semgrep project invariants."""

import subprocess


def safe_shell(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=False, text=True)


def safe_finding(factory):
    return factory(mutation_authority=False, fix_available=False)


def safe_command() -> tuple[str, ...]:
    return ("semgrep", "--no-autofix", "--no-fix", "--no-fix-only")
