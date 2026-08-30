"""Canonical enumeration ownership after numbered-root retirement."""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from pathlib import Path

import neocortex.enumeration as canonical
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXPORTS = [
    "CorruptBufferError",
    "ALL_REASONS",
    "EnumerationCheckpoint",
    "InvalidVolumeError",
    "JournalDiscontinuityError",
    "JournalCursor",
    "NtfsEntry",
    "NtfsUsnError",
    "SqlitePathIndex",
    "UnsupportedPlatformError",
    "UnsupportedRecordVersionError",
    "UsnJournalInfo",
    "UsnChangeBatch",
    "UsnJournalReader",
    "VolumeAccessError",
    "VolumeEnumeration",
    "enumerate_volume",
    "query_journal_cursor",
    "consume_changes",
]


def test_canonical_surface_has_responsibility_owned_implementations() -> None:
    assert list(canonical.__all__) == EXPECTED_EXPORTS
    assert set(canonical.__all__) <= set(dir(canonical))
    assert canonical.JournalCursor.__module__ == "neocortex.enumeration.models"
    assert canonical.UnsupportedPlatformError.__module__ == "neocortex.enumeration.errors"
    assert canonical.enumerate_volume.__module__ == "neocortex.enumeration.ntfs.enumeration"
    assert canonical.consume_changes.__module__ == "neocortex.enumeration.ntfs.journal"
    assert canonical.SqlitePathIndex.__module__ == "neocortex.enumeration.path_index.repository"
    with pytest.raises(AttributeError, match="has no attribute"):
        canonical.__getattr__("missing_enumeration_symbol")


@pytest.mark.parametrize(
    "module_name",
    (
        "neocortex.enumeration.errors",
        "neocortex.enumeration.models",
        "neocortex.enumeration.ntfs.enumeration",
        "neocortex.enumeration.ntfs.journal",
        "neocortex.enumeration.ntfs.parser",
        "neocortex.enumeration.ntfs.volume",
        "neocortex.enumeration.path_index.repository",
        "neocortex.enumeration.path_index.schema",
    ),
)
def test_canonical_leaf_modules_are_importable(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


def test_canonical_pickle_roundtrip_uses_the_owned_model_path() -> None:
    cursor = canonical.JournalCursor("C:", 7, 11)

    assert pickle.loads(pickle.dumps(cursor)) == cursor
    assert canonical.JournalCursor.__module__ == "neocortex.enumeration.models"


def test_public_facade_defers_platform_and_sqlite_implementations() -> None:
    script = """
import sys
import neocortex.enumeration as canonical

for module in (
    "neocortex.enumeration.ntfs.volume",
    "neocortex.enumeration.path_index.repository",
    "neocortex.enumeration.path_index.schema",
):
    assert module not in sys.modules, module
assert canonical.JournalCursor.__module__ == "neocortex.enumeration.models"
assert "neocortex.enumeration.models" in sys.modules
assert "neocortex.enumeration.ntfs.volume" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_numbered_enumeration_root_is_extinct() -> None:
    legacy = "_01" + "_Enumeracion"

    assert not (REPOSITORY_ROOT / legacy).exists()
    for relative in ("neocortex", "neocortex", "tools"):
        for path in (REPOSITORY_ROOT / relative).rglob("*.py"):
            assert legacy not in path.read_text(encoding="utf-8"), path
