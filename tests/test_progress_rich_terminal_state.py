"""Rich progress must redraw as one bounded row per typed task key."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import termios
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from neocortex.progress import LineProgress, ProgressEvent, ProgressMetric, RichProgress
from neocortex.progress import rich as rich_progress


def test_failed_terminal_event_keeps_unknown_total_in_rich_task() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    reporter(ProgressEvent("pdf", "extract", "Procesando PDF", 1, 10))
    reporter(
        ProgressEvent(
            "pdf",
            "extract",
            "PDF fallido",
            1,
            None,
            metrics=(ProgressMetric("status", "failed"),),
            finished=True,
        )
    )

    task = reporter._progress.tasks[0]  # type: ignore[attr-defined]
    assert task.completed == 1
    assert task.total is None
    assert task.fields["event_finished"] is True
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
    console = Console(file=StringIO(), width=160, force_terminal=False)
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

    tasks = reporter._progress.tasks  # type: ignore[attr-defined]
    assert len(tasks) == 2
    assert tasks[0].description == "Ejecución preparada"
    assert tasks[0].fields["event_finished"] is True
    reporter.stop()


def test_default_console_uses_live_pty_dimensions_over_stale_environment(monkeypatch) -> None:
    dimensions = {"columns": 73, "lines": 19}
    monkeypatch.setattr(rich_progress.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(rich_progress.sys.stderr, "fileno", lambda: 2)
    monkeypatch.setattr(
        rich_progress.os,
        "get_terminal_size",
        lambda _fd: SimpleNamespace(**dimensions),
    )
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setenv("LINES", "2")

    reporter = RichProgress()

    assert reporter._console._width is None  # type: ignore[attr-defined]
    assert reporter._console._height is None  # type: ignore[attr-defined]
    assert reporter._console.width == 73  # type: ignore[attr-defined]
    assert reporter._console.height == 19  # type: ignore[attr-defined]
    dimensions.update(columns=51, lines=11)
    assert reporter._console.width == 51  # type: ignore[attr-defined]
    assert reporter._console.height == 11  # type: ignore[attr-defined]
    reporter.stop()


def test_default_console_keeps_pipe_mode_unforced(monkeypatch) -> None:
    monkeypatch.setattr(rich_progress.sys.stderr, "isatty", lambda: False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)

    reporter = RichProgress()

    assert reporter._console._width is None  # type: ignore[attr-defined]
    assert reporter._console._height is None  # type: ignore[attr-defined]
    reporter.stop()


def test_task_summary_normalizes_metric_order_without_truncating_core_values() -> None:
    console = Console(file=StringIO(), width=180, force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    reporter(
        ProgressEvent(
            "docx",
            "extract",
            "Indexando DOCX",
            1234,
            2468,
            "documentos",
            metrics=(
                ProgressMetric("memory_waits", 7),
                ProgressMetric("status", "running"),
                ProgressMetric("errors", 2),
                ProgressMetric("new_work", 1200),
                ProgressMetric("cache_hits", 9),
                ProgressMetric("retries", 4),
                ProgressMetric("protected", 3),
            ),
        )
    )

    task = reporter._progress.tasks[0]  # type: ignore[attr-defined]
    line = reporter._progress.columns[0].render(task).plain  # type: ignore[attr-defined]
    ordered_fields = (
        "avance 50% · 1234/2468 documentos",
        "caché 9",
        "trabajo nuevo 1200",
        "errores 2",
        "esperas 7",
        "estado en curso",
    )
    assert all(field in line for field in ordered_fields)
    assert [line.index(field) for field in ordered_fields] == sorted(
        line.index(field) for field in ordered_fields
    )
    assert "Indexando DOCX" in line
    assert len(line) <= console.width
    reporter.stop()


def test_phase_groups_are_ordered_independently_of_event_arrival() -> None:
    console = Console(file=StringIO(), width=180, force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    events = (
        ProgressEvent("framework", "complete", "Etapa previa completada", 1, 1, "fase"),
        ProgressEvent("semantic", "integrated", "Semantic completado", 1, 1, "fase"),
        ProgressEvent("pdf", "extract", "Procesando PDF", 1, 2, "PDF"),
        ProgressEvent("dedup", "inventory", "Inventariando archivos", 1, 2, "archivos"),
        ProgressEvent("framework", "prepare", "Ejecución preparada", 1, 1, "fase"),
    )
    for event in events:
        reporter(event)

    snapshot = StringIO()
    snapshot_console = Console(file=snapshot, width=180, force_terminal=False)
    snapshot_console.print(
        reporter._progress.make_tasks_table(reporter._progress.tasks)  # type: ignore[attr-defined]
    )
    rendered = snapshot.getvalue()
    expected_groups = (
        "Preparación",
        "Inventario y validación",
        "Procesamiento por rutas",
        "Catálogos y Semantic",
        "Cierre",
    )
    assert all(group in rendered for group in expected_groups)
    assert [rendered.index(group) for group in expected_groups] == sorted(
        rendered.index(group) for group in expected_groups
    )
    assert len(reporter._progress.tasks) == len(events)  # type: ignore[attr-defined]
    reporter.stop()


def test_pty_resize_redraw_tracks_both_directions_and_keeps_one_unwrapped_task(monkeypatch) -> None:
    master, slave = pty.openpty()
    stream = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
    monkeypatch.setattr(rich_progress.sys, "stderr", stream)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.setenv("LINES", "2")

    def resize(columns: int, lines: int) -> None:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))

    def drain() -> bytes:
        output = bytearray()
        while select.select([master], [], [], 0)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return bytes(output)

    reporter = None
    terminal_output = bytearray()
    try:
        resize(44, 10)
        reporter = RichProgress(transient=False, refresh_per_second=30)
        updates = (
            (44, 10, ProgressEvent(
                "pdf",
                "extract",
                "Descripción anterior extensa que se debe reemplazar",
                400,
                2000,
                "PDF",
                metrics=(
                    ProgressMetric("status", "running"),
                    ProgressMetric("cache_hits", 0),
                    ProgressMetric("new_work", 400),
                    ProgressMetric("errors", 0),
                    ProgressMetric("memory_waits", 4),
                ),
            )),
            (160, 24, ProgressEvent(
                "pdf",
                "extract",
                "Indexando PDF con detalles actualizados",
                800,
                2000,
                "PDF",
                metrics=(
                    ProgressMetric("status", "running"),
                    ProgressMetric("cache_hits", 1),
                    ProgressMetric("new_work", 800),
                    ProgressMetric("errors", 0),
                    ProgressMetric("memory_waits", 5),
                ),
            )),
            (40, 8, ProgressEvent(
                "pdf",
                "extract",
                "PDF fallido",
                900,
                None,
                "PDF",
                True,
                (
                    ProgressMetric("status", "failed"),
                    ProgressMetric("errors", 9),
                ),
            )),
            (40, 8, ProgressEvent(
                "pdf",
                "extract",
                "Reanudando PDF",
                901,
                2000,
                "PDF",
                metrics=(
                    ProgressMetric("status", "running"),
                    ProgressMetric("errors", 0),
                ),
            )),
        )
        expected_progress = (
            "400/2000 PDF",
            "800/2000 PDF",
            "900/? PDF",
            "901/2000 PDF",
        )
        for (columns, lines, event), progress_text in zip(updates, expected_progress, strict=True):
            resize(columns, lines)
            reporter(event)
            reporter._progress.refresh()  # type: ignore[attr-defined]
            terminal_output.extend(drain())

            assert reporter._console.width == columns  # type: ignore[attr-defined]
            assert reporter._console.height == lines  # type: ignore[attr-defined]
            assert len(reporter._progress.tasks) == 1  # type: ignore[attr-defined]
            task = reporter._progress.tasks[0]  # type: ignore[attr-defined]
            row = reporter._progress.columns[0].render(task)  # type: ignore[attr-defined]
            assert row.cell_len <= columns
            assert progress_text in row.plain

            if event.description == "PDF fallido":
                assert task.total is None
                assert "errores 9" in row.plain
                assert "estado fallido" in row.plain
            if event.description == "Reanudando PDF":
                assert task.finished is False
                assert task.stop_time is None
                assert "estado en curso" in row.plain
                assert "fallido" not in row.plain
                assert "Descripción anterior" not in row.plain

        reporter.stop()
        terminal_output.extend(drain())
        assert b"\x1b[" in terminal_output
    finally:
        if reporter is not None:
            reporter.stop()
        stream.close()
        os.close(slave)
        os.close(master)


def test_pipe_progress_stream_keeps_its_json_envelope(capsys) -> None:
    reporter = LineProgress(clock=lambda: 1.0)
    reporter(
        ProgressEvent(
            "pdf",
            "extract",
            "Procesando PDF",
            7,
            20,
            "PDF",
            metrics=(ProgressMetric("errors", 2), ProgressMetric("status", "running")),
        )
    )

    output = capsys.readouterr().err.strip()
    marker, payload_text = output.split(" ", 1)
    payload = json.loads(payload_text)
    assert marker == "NEOCORTEX_PROGRESS"
    assert set(payload) == {
        "completed",
        "description",
        "elapsed_seconds",
        "eta_seconds",
        "finished",
        "metrics",
        "operation",
        "phase",
        "rate_per_second",
        "total",
        "unit",
    }
    assert payload["metrics"] == {"errors": 2, "status": "running"}
