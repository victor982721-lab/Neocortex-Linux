"""Positive fixture for Neocortex's local Semgrep project invariants."""

import subprocess


def unsafe_shell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, text=True)


def unsafe_finding(factory):
    return factory(mutation_authority=True)


def unsafe_payload() -> dict[str, bool]:
    return {"mutation_authority": True}


def unsafe_local_authority() -> bool:
    mutation_authority = True
    return mutation_authority


def unsafe_command() -> tuple[str, ...]:
    return ("semgrep", "--autofix", "--fix")
