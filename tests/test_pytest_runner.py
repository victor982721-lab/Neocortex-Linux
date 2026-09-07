"""Regression coverage for repository-local pytest import resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def test_pytest_uses_repository_test_root(pytestconfig: Any) -> None:
    """Keep both pytest entry points anchored at the repository root."""

    assert pytestconfig.getini("testpaths") == ["tests"]
    assert pytestconfig.getini("pythonpath") == [Path(__file__).resolve().parent.parent]
