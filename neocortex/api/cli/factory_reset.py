"""Flat CLI adapter for the one-shot factory reset operation.

The persistence owner is the only implementation of the reset.  This module
keeps the command deliberately small: it forwards the optional fixture state
directory, renders one human-readable result, and never exposes the former
preview/apply/backup/digest contract.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path


def _state_directory(args: argparse.Namespace) -> str | Path | None:
    """Return ``None`` unless the caller explicitly selected a state path.

    The integrated parser has a canonical default for other operations.  A
    factory reset must let the persistence owner choose its own production
    default, while tests and fixtures may pass ``--state-directory``.
    """

    explicit = getattr(args, "_explicit_options", None)
    if explicit is not None:
        if "state_directory" not in explicit:
            return None
    return getattr(args, "state_directory", None)


def _render_success(result: Mapping[str, object]) -> None:
    """Render a bounded, stable human result without replaying engine details."""

    fields = ["FACTORY_RESET", "status=complete"]
    value = result.get("deleted_count")
    if isinstance(value, int) and not isinstance(value, bool):
        fields.append(f"deleted_count={value}")
    print(" ".join(fields))


def run_factory_reset(args: argparse.Namespace) -> int:
    """Execute the sole ``--factory-reset`` CLI operation."""

    try:
        from neocortex.persistence.factory_reset import factory_reset

        result = factory_reset(state_directory=_state_directory(args))
        if not isinstance(result, Mapping):
            raise TypeError("factory_reset must return a mapping")
        if result.get("status") != "complete":
            detail = result.get("error") or result.get("message") or result.get("status")
            raise RuntimeError(
                "factory reset did not complete"
                if detail is None
                else f"factory reset did not complete: {detail}"
            )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        message = str(error).replace("\n", " ")[:1_000] or type(error).__name__
        print(f"ERROR factory_reset: {message}", file=sys.stderr)
        return 1

    _render_success(result)
    return 0


__all__ = ["run_factory_reset"]
