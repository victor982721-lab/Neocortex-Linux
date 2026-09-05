"""Rich progress must not turn failed work into a completed bar."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from neocortex.progress import ProgressEvent, ProgressMetric, RichProgress


def test_failed_terminal_event_keeps_unknown_total_in_rich_task() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    reporter(ProgressEvent("pdf", "extract", "Procesando PDF", 1, 10))
    reporter(
        ProgressEvent(
            "pdf",
            "extract",
            "Procesando PDF — falló",
            1,
            None,
            metrics=(ProgressMetric("status", "failed"),),
            finished=True,
        )
    )

    task = reporter._progress.tasks[0]  # type: ignore[attr-defined]
    assert task.completed == 1
    assert task.total is None
    reporter.stop()


def test_successful_terminal_event_preserves_its_declared_total() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    reporter(
        ProgressEvent(
            "pdf",
            "extract",
            "PDF completados",
            10,
            10,
            finished=True,
        )
    )

    task = reporter._progress.tasks[0]  # type: ignore[attr-defined]
    assert task.completed == 10
    assert task.total == 10
    reporter.stop()
