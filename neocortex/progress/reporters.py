"""Simple progress reporters without terminal presentation dependencies."""

from __future__ import annotations

from .events import ProgressEvent


class NullProgress:
    """Drop-in reporter for services or externally managed interfaces."""

    def __call__(self, event: ProgressEvent) -> None:
        return None

    def __enter__(self) -> "NullProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class RecordingProgress:
    """In-memory reporter for deterministic integrations and tests."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def __enter__(self) -> "RecordingProgress":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None
