"""Rich progress must not turn failed work into a completed bar."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from neocortex.progress import ProgressEvent, ProgressMetric, RichProgress
from neocortex.progress import rich as rich_progress


def test_failed_terminal_event_keeps_unknown_total_in_rich_task() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    assert reporter._progress.expand is False  # type: ignore[attr-defined]
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


def test_repeated_terminal_and_variable_description_updates_keep_one_task_per_key() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    for _ in range(32):
        reporter(ProgressEvent("framework", "prepare", "Preparando ejecución", 0, 1, "fase"))
        reporter(ProgressEvent("framework", "prepare", "Ejecución preparada", 1, 1, "fase", True))
    for completed in range(1, 64):
        description = (
            "Validando identidades y alias físicos"
            if completed % 2
            else "Calculando hashes completos"
        )
        reporter(ProgressEvent("dedup", "verify", description, completed, 64, "operaciones"))

    assert len(reporter._progress.tasks) == 2  # type: ignore[attr-defined]
    assert reporter._progress.tasks[0].description == "Ejecución preparada"  # type: ignore[attr-defined]
    reporter.stop()


def test_default_console_uses_live_pty_dimensions_over_stale_environment(
    monkeypatch,
) -> None:
    monkeypatch.setattr(rich_progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(rich_progress.sys.stderr, "fileno", lambda: 2)
    monkeypatch.setattr(
        rich_progress.os,
        "get_terminal_size",
        lambda _fd: SimpleNamespace(columns=73, lines=19),
    )
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setenv("LINES", "2")

    reporter = RichProgress()

    assert reporter._console.width == 73  # type: ignore[attr-defined]
    assert reporter._console.height == 19  # type: ignore[attr-defined]
    reporter.stop()


def test_default_console_keeps_pipe_mode_unforced(monkeypatch) -> None:
    monkeypatch.setattr(rich_progress.sys.stderr, "isatty", lambda: False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)

    reporter = RichProgress()

    # No explicit width/height is injected for pipes; Rich retains its normal
    # noninteractive fallback and the caller can still provide a custom
    # Console when it needs deterministic capture dimensions.
    assert reporter._console._width is None  # type: ignore[attr-defined]
    assert reporter._console._height is None  # type: ignore[attr-defined]
    reporter.stop()


def test_pty_rendering_does_not_leave_duplicate_wrapped_rows() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=80, height=8)
    with RichProgress(console=console, transient=False) as reporter:
        for _ in range(3):
            reporter(ProgressEvent("framework", "prepare", "Preparando ejecución", 0, 1, "fase"))
            reporter(
                ProgressEvent(
                    "framework", "prepare", "Ejecución preparada", 1, 1, "fase", True
                )
            )
        for completed in range(1, 30):
            description = (
                "Validando identidades y alias físicos"
                if completed % 2
                else "Calculando hashes completos"
            )
            reporter(
                ProgressEvent(
                    "dedup", "verify", description, completed, 64, "operaciones"
                )
            )

    rendered = output.getvalue().replace("\r", "")
    assert rendered.count("Ejecución preparada") == 1
    assert "Preparando ejecución" not in rendered
