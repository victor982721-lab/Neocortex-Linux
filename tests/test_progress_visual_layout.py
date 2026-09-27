"""Public visual contracts for framed stages, ZIP counts and compact terminals."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console

from neocortex.progress import LineProgress, ProgressEvent, ProgressMetric, RichProgress


def _metrics(**values: int | str) -> tuple[ProgressMetric, ...]:
    return tuple(ProgressMetric(name, value) for name, value in values.items())


def _view(reporter: RichProgress) -> str:
    output = StringIO()
    console = Console(file=output, width=reporter._console.width, color_system=None)
    console.print(reporter._progress.get_renderable())
    return output.getvalue()


@pytest.mark.parametrize("width", (40, 60, 80, 120, 160, 240))
def test_framed_stages_center_titles_and_share_a_bounded_dashboard(
    width: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    console = Console(file=StringIO(), width=width, height=80, force_terminal=True)
    reporter = RichProgress(console=console, transient=True)
    reporter(ProgressEvent("framework", "prepare", "Prepare", 1, 1, "fase", True))
    reporter(ProgressEvent("dedup", "inventory", "Inventory", 112535, 112535, "archivos", True))
    reporter.stop()
    view = _view(reporter)
    for title in ("01  Preparación", "02  Inventario y validación"):
        line = next(line for line in view.splitlines() if title in line)
        before, after = line.split(title)
        assert abs(cell_len(before) - cell_len(after)) <= 2
        assert "─" in line and "╭" in line and "╮" in line
    frames = [line for line in view.splitlines() if "╰" in line]
    assert len(frames) == 2 and len({cell_len(line) for line in frames}) == 1
    assert "112535/112535" in view and "Finalizado" in view
    assert all(cell_len(line) <= width for line in view.splitlines())


@pytest.mark.parametrize("width", (40, 60, 80, 120, 160, 240))
def test_zip_breakdown_is_always_visible_and_not_the_progress_denominator(
    width: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    event = ProgressEvent(
        "zip-intake", "process", "ZIPs 999 fabricated description", 1790, 1790, "ZIPs", True,
        _metrics(status="partial", zip_files_inventoried=11, zip_files_admitted=9,
                 atomic_packages=1781, generic_identified=1, unclassified=8, blocked=9, applied=0),
    )
    reporter = RichProgress(
        console=Console(file=StringIO(), width=width, height=80, force_terminal=True),
        transient=True, details=False,
    )
    reporter(event)
    reporter.stop()
    view = _view(reporter)
    words = " ".join(view.split())
    assert "1790/1790" in view and "Parcial" in view
    assert "Archivos .zip (por nombre): 11" in words
    assert "Archivos .zip admitidos: 9" in words
    assert "Paquetes atómicos: 1781" in words
    assert "ZIP genéricos: 1" in words and "Sin clasificar: 8" in words
    assert "Bloqueados: 9" in words
    assert "1790 ZIPs" not in view and "999" not in view
    assert all(cell_len(line) <= width for line in view.splitlines())


def test_absent_zip_categories_stay_unknown_including_legacy_events() -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
    reporter(ProgressEvent("zip-intake", "process", "ZIP", 1, 1, "ZIPs", True))
    view = _view(reporter)
    assert "Archivos .zip (por nombre): —" in view
    assert "Paquetes atómicos: —" in view and "ZIP genéricos: —" in view
    assert "Contenedores aplicados: —" in view
    reporter.stop()


@pytest.mark.parametrize("width", (40, 60, 80, 120, 160, 240))
def test_large_counts_error_counts_and_states_are_not_cropped(
    width: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    count = 2**53 + 1
    reporter = RichProgress(
        console=Console(file=StringIO(), width=width, height=80, force_terminal=True),
        transient=True,
    )
    reporter(ProgressEvent("pdf", "extract", "PDF", count, count + 1, "PDF", True,
                           _metrics(status="failed", errors=9)))
    reporter.stop()
    view = _view(reporter)
    assert f"{count}/{count + 1}" in view and "Fallido" in view and "9" in view
    assert all(cell_len(line) <= width for line in view.splitlines())
    task = reporter._progress.tasks[0]
    assert task.completed == count and task.total == count + 1


@pytest.mark.parametrize("status", ("failed", "partial", "error", "unknown", "future"))
def test_terminal_unknown_total_is_not_completed_by_the_renderer(status: str) -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
    reporter(ProgressEvent("pdf", "extract", "PDF", 1, 10))
    reporter(ProgressEvent("pdf", "extract", "End", 3, None, finished=True,
                           metrics=_metrics(status=status)))
    task = reporter._progress.tasks[0]
    assert task.completed == 3 and task.total is None
    assert "3/?" in _view(reporter)
    reporter.stop()


def test_low_height_keeps_route_errors_before_history_and_returns_full_view_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    reporter = RichProgress(
        console=Console(file=StringIO(), width=40, height=8, force_terminal=True),
        transient=True,
    )
    for route in ("docx", "office", "text", "video", "image"):
        reporter(ProgressEvent(route, "extract", "done", 1, 1, finished=True))
    reporter(ProgressEvent("pdf", "extract", "PDF error", 123, 246, finished=True,
                           metrics=_metrics(status="failed", errors=9)))
    reporter(ProgressEvent("framework", "result", "failed", 0, None, finished=True,
                           metrics=_metrics(status="failed", errors=1)))
    compact = _view(reporter)
    assert "123/246" in compact and "Fallido" in compact and "9" in compact
    assert "Trabajo actual y avisos" in compact
    assert "Vista compacta" in compact
    assert len(compact.splitlines()) <= 8
    reporter.stop()
    full = _view(reporter)
    assert "Procesamiento por rutas" in full and "DOCX" in full and "Office" in full
    assert len(reporter._progress.tasks) == 7


@pytest.mark.parametrize("mode", ("dumb", "flag"))
def test_ascii_human_presentation_does_not_change_structured_events(
    mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "dumb" if mode == "dumb" else "xterm-256color")
    if mode == "flag":
        monkeypatch.setenv("NEOCORTEX_PROGRESS_ASCII", "1")
    event = ProgressEvent("dedup", "inventory", "Identificación", 1, 2, "imágenes", False,
                          _metrics(status="running"))
    before = asdict(event)
    reporter = RichProgress(
        console=Console(file=StringIO(), width=80, height=40, force_terminal=True),
        transient=True,
    )
    reporter(event)
    view = _view(reporter)
    assert view.isascii() and "1/2" in view and "imagenes" in view
    assert asdict(event) == before
    reporter.stop()
    output = StringIO()
    monkeypatch.setattr(sys, "stderr", output)
    LineProgress(clock=lambda: 0.0)(event)
    payload = json.loads(output.getvalue().removeprefix("NEOCORTEX_PROGRESS "))
    assert len(payload) == 11
    for field in ("operation", "phase", "description", "completed", "total", "unit", "finished"):
        assert payload[field] == before[field]
    assert payload["metrics"] == {metric.name: metric.value for metric in event.metrics}


def test_no_color_removes_color_but_preserves_typographic_emphasis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    output = StringIO()
    console = Console(file=output, width=100, height=60, force_terminal=True,
                      color_system="standard")
    reporter = RichProgress(console=console, transient=True)
    reporter(ProgressEvent("pdf", "extract", "PDF", 1, 2, finished=True,
                           metrics=_metrics(status="failed", errors=1)))
    reporter.stop()
    output.seek(0)
    output.truncate()
    console.print(reporter._progress.get_renderable())
    raw = output.getvalue()
    for sequence in re.findall(r"\x1b\[([0-9;]*)m", raw):
        codes = {int(part) for part in sequence.split(";") if part}
        assert not codes.intersection({*range(30, 38), 38, *range(40, 48), 48,
                                       *range(90, 98), *range(100, 108)})
    assert "Fallido" in raw
