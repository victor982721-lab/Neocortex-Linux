"""Contracts for the canonical Progress boundary and legacy-root extinction."""

from __future__ import annotations

from pathlib import Path

import neocortex.progress as progress
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_progress_public_surface_has_one_bounded_owner() -> None:
    assert progress.__all__ == [
        "LineProgress",
        "NullProgress",
        "ProgressCallback",
        "ProgressEvent",
        "ProgressMetric",
        "RecordingProgress",
        "RichProgress",
        "emit_progress",
    ]
    assert progress.ProgressEvent.__module__ == "neocortex.progress.events"
    assert progress.ProgressMetric.__module__ == "neocortex.progress.events"
    assert progress.emit_progress.__module__ == "neocortex.progress.events"
    assert progress.LineProgress.__module__ == "neocortex.progress.line"
    assert progress.RichProgress.__module__ == "neocortex.progress.rich"
    assert progress.NullProgress.__module__ == "neocortex.progress.reporters"
    assert progress.RecordingProgress.__module__ == "neocortex.progress.reporters"
    assert set(progress.__all__) <= set(dir(progress))
    with pytest.raises(AttributeError, match="has no attribute"):
        progress.__getattr__("missing_reporter")


def test_callback_null_and_recording_reporters_preserve_the_event_contract() -> None:
    event = progress.ProgressEvent("inventory", "scan", "Inventario", 1, 1, finished=True)
    observed: list[progress.ProgressEvent] = []

    progress.emit_progress(None, event)
    progress.emit_progress(observed.append, event)
    assert observed == [event]

    with progress.NullProgress() as null_progress:
        assert null_progress(event) is None
    with progress.RecordingProgress() as recording:
        recording(event)
    assert recording.events == [event]


def test_numbered_progress_root_and_python_references_are_extinct() -> None:
    legacy = "_03" + "_Progreso"
    assert not (REPOSITORY_ROOT / legacy).exists()
    for relative in (
        "_04_Nucleo_Operativo",
        "neocortex",
        "tests",
        "tools",
    ):
        for path in (REPOSITORY_ROOT / relative).rglob("*.py"):
            assert legacy not in path.read_text(encoding="utf-8"), path
    assert legacy not in (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
