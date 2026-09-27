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

from rich.cells import cell_len
from rich.console import Console

from neocortex.progress import LineProgress, ProgressEvent, ProgressMetric, RichProgress
from neocortex.progress import rich as rich_progress


def test_zip_intake_is_shown_before_identify_and_content_routes() -> None:
    console = Console(file=StringIO(), width=160, force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    for operation, phase, unit in (
        ("pdf", "extract", "PDF"),
        ("framework", "content-types", "archivos"),
        ("zip-intake", "process", "ZIPs"),
        ("dedup", "inventory", "archivos"),
    ):
        reporter(ProgressEvent(operation, phase, "fixture", 1, 1, unit, True,
                               (ProgressMetric("status", "applied"),)))
    tasks = sorted(reporter._progress.tasks, key=rich_progress._task_order)
    assert [task.fields["operation"] for task in tasks] == [
        "dedup", "zip-intake", "framework", "pdf",
    ]
    console.file = StringIO()
    console.print(reporter._progress.get_renderable())
    rendered = console.file.getvalue()
    assert rendered.index("Revisar contenedores") < rendered.index("Tipos de contenido")
    assert rendered.index("Revisar contenedores") < rendered.index("Procesamiento por rutas")
    assert "Aplicado" in rendered
    reporter.stop()


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



def _snapshot(reporter: RichProgress) -> str:
    output = StringIO()
    console = Console(file=output, width=reporter._console.width, color_system=None)
    console.print(reporter._progress.get_renderable())
    return output.getvalue()


def _metric_tuple(**values: int | str) -> tuple[ProgressMetric, ...]:
    return tuple(ProgressMetric(name, value) for name, value in values.items())


def test_routes_share_columns_and_distinguish_zero_from_missing_metrics() -> None:
    console = Console(file=StringIO(), width=180, force_terminal=False)
    reporter = RichProgress(console=console, transient=True, details=False)
    reporter(ProgressEvent(
        "docx", "extract", "caché 999 errores 999; descripción no es un contador",
        1234, 2468, "documentos",
        metrics=_metric_tuple(memory_waits=7, errors=0, new_work=1200, cache_hits=9),
    ))
    reporter(ProgressEvent(
        "text", "extract", "Descripción diferente", 200, 300, "documentos",
        metrics=_metric_tuple(errors=2, cache_hits=100),
    ))
    table = reporter._progress.make_tasks_table(reporter._progress.tasks)
    assert [column.header for column in table.columns] == [
        "Ruta", "Avance", "Unidad", "Caché", "Nuevo", "Errores", "Esperas", "Tiempo", "Estado",
    ]
    assert len(table.rows) == 2
    docx, text = reporter._progress.tasks
    docx_cells = rich_progress._task_cells(docx)
    text_cells = rich_progress._task_cells(text)
    assert docx_cells["new"].plain == "1200"
    assert docx_cells["errors"].plain == "0"
    assert text_cells["new"].plain == "—"
    assert text_cells["waits"].plain == "—"
    rendered = _snapshot(reporter)
    assert "999" not in rendered
    assert "1234/2468" in rendered
    assert "200/300" in rendered
    assert "— dato no informado" in rendered
    reporter.stop()


def test_real_route_terminal_producer_supplies_and_replaces_typed_outcome() -> None:
    from threading import RLock

    from neocortex.progress import RecordingProgress
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator

    for outcome, state in (
        ("completed", "Completo"), ("failed", "Fallido"), ("cancelled", "Cancelado"),
    ):
        recording = RecordingProgress()
        orchestrator = FrameworkOrchestrator.__new__(FrameworkOrchestrator)
        orchestrator._progress_lock = RLock()
        orchestrator._active_progress = {}
        orchestrator.progress = recording
        orchestrator._coordinated_progress(ProgressEvent(
            "pdf", "extract", "Descripción sin resultado", 900, 2000, "PDF",
            metrics=_metric_tuple(errors=9, status="running"),
        ))
        orchestrator._finish_route_progress("pdf", outcome)
        terminal = recording.events[-1]
        assert terminal.finished
        assert [metric.value for metric in terminal.metrics if metric.name == "status"] == [
            outcome,
        ]
        reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
        reporter(terminal)
        view = _snapshot(reporter)
        assert state in view
        expected_advance = "900/2000" if outcome == "completed" else "900/?"
        assert expected_advance in view
        reporter.stop()


def test_semantic_no_sources_producer_reports_partial_or_skipped(tmp_path, monkeypatch) -> None:
    from neocortex.api.cli.cli_parser import build_parser
    from neocortex.progress import RecordingProgress
    from neocortex.semantic import semantic_application

    for unavailable, state in ((True, "Parcial"), (False, "Omitido")):
        def select(args, _run_id, unavailable=unavailable):
            args.semantic_source = ()
            args._semantic_source_unavailable = {"pdf": "unavailable"} if unavailable else {}
            return (), False

        monkeypatch.setattr(semantic_application, "_select_integrated_sources", select)
        args = build_parser().parse_args(["--all", "--state-directory", str(tmp_path)])
        args.state_directory = tmp_path
        recording = RecordingProgress()
        result = semantic_application.run_integrated_all_semantic_index(
            args, progress=recording, print_output=False, framework_lock_held=True,
        )
        assert result == (2 if unavailable else 0)
        terminal = recording.events[-1]
        reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
        reporter(terminal)
        assert state in _snapshot(reporter)
        assert "Finalizado" not in _snapshot(reporter)
        reporter.stop()


def test_existing_semantic_status_codes_have_consistent_human_labels() -> None:
    for outcome, state in (
        ("ok", "Completo"), ("error", "Incompleto"), ("interrumpido", "Interrumpido"),
    ):
        reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
        reporter(ProgressEvent(
            "semantic", "integrated", "Descripción", 1, 1, "fase", True,
            _metric_tuple(status=outcome),
        ))
        assert state in _snapshot(reporter)
        reporter.stop()


def test_semantic_completion_metric_is_used_when_status_is_not_published() -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=120), transient=True)
    reporter(ProgressEvent(
        "semantic", "unavailable:text", "Modelo no disponible", 0, 1, "ámbitos",
        metrics=_metric_tuple(completion_status="partial"),
    ))
    assert "Parcial" in _snapshot(reporter)
    reporter.stop()


def test_display_uses_exact_event_counts_even_after_partial_finalization() -> None:
    console = Console(file=StringIO(), width=180, force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    reporter(ProgressEvent(
        "pdf", "extract", "Resultado parcial", 2, 10, "PDF", True,
        _metric_tuple(status="failed", errors=1),
    ))
    assert rich_progress._task_cells(reporter._progress.tasks[0])["advance"].plain == "2/10"
    large = 9007199254740993
    reporter(ProgressEvent("pdf", "extract", "Reanudación", large, large + 1, "PDF"))
    assert rich_progress._task_cells(reporter._progress.tasks[0])["advance"].plain == (
        f"{large}/{large + 1}"
    )
    reporter.stop()


def test_secondary_fields_stay_out_of_main_tables_until_details_are_requested() -> None:
    event = ProgressEvent(
        "video", "inspect", "Inspección visual en curso", 12, 20, "archivos",
        metrics=_metric_tuple(cache_hits=3, errors=0, frames=200, ocr_positive=4),
    )
    for details in (False, True):
        console = Console(file=StringIO(), width=240, force_terminal=False)
        reporter = RichProgress(console=console, transient=True, details=details)
        reporter(event)
        view = _snapshot(reporter)
        assert ("Detalles de las tareas" in view) is details
        assert ("fotogramas: 200" in view) is details
        assert ("Inspección visual en curso" in view) is details
        assert len(reporter._progress.make_tasks_table(reporter._progress.tasks).rows) == 1
        reporter.stop()


def test_details_environment_is_opt_in_and_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("NEOCORTEX_PROGRESS_DETAILS", "1")
    console = Console(file=StringIO(), force_terminal=False)
    reporter = RichProgress(console=console)
    assert reporter._progress.details is True
    explicit = RichProgress(console=console, details=False)
    assert explicit._progress.details is False


def test_details_also_expose_primary_columns_hidden_by_narrow_width() -> None:
    reporter = RichProgress(
        console=Console(file=StringIO(), width=40), transient=True, details=True,
    )
    reporter(ProgressEvent(
        "pdf", "extract", "PDF", 10, 20, "PDF",
        metrics=_metric_tuple(cache_hits=7, new_work=3, errors=1, memory_waits=8),
    ))
    view = _snapshot(reporter)
    details = " ".join(view.split("Detalles de las tareas", 1)[1].split())
    assert "caché: 7" in details
    assert "trabajo nuevo: 3" in details
    assert "esperas: 8" in details
    reporter.stop()


def test_narrow_and_wide_tables_keep_counts_and_state_complete() -> None:
    for width in (40, 60, 80, 120, 240):
        console = Console(file=StringIO(), width=width, force_terminal=False)
        reporter = RichProgress(console=console, transient=True, details=False)
        reporter(ProgressEvent(
            "pdf", "extract", "PDF", 123456, 246912, "PDF",
            metrics=_metric_tuple(status="running", cache_hits=100, new_work=3,
                                  errors=2, memory_waits=7),
        ))
        reporter(ProgressEvent(
            "docx", "extract", "DOCX", 24, 48, "documentos",
            metrics=_metric_tuple(status="failed", errors=1),
        ))
        view = _snapshot(reporter)
        assert "123456/246912" in view
        assert "24/48" in view
        assert "En curso" in view
        assert "Fallido" in view
        assert all(cell_len(line) <= width for line in view.splitlines())
        table = reporter._progress.make_tasks_table(reporter._progress.tasks)
        assert all(len(column._cells) == 2 for column in table.columns)
        reporter.stop()


def test_phase_groups_have_blank_separation_and_stable_stage_names() -> None:
    console = Console(file=StringIO(), width=180, force_terminal=False)
    reporter = RichProgress(console=console, transient=True)
    events = (
        ProgressEvent("framework", "complete", "Etapa", 1, 1, "fase"),
        ProgressEvent("semantic", "integrated", "Semantic", 1, 1, "fase"),
        ProgressEvent("pdf", "extract", "PDF", 1, 2, "PDF"),
        ProgressEvent("dedup", "verify", "Duplicados", 1, 2, "operaciones"),
        ProgressEvent("framework", "content-types", "Tipos", 1, 2, "archivos"),
        ProgressEvent("dedup", "inventory", "Inventario", 1, 2, "archivos"),
        ProgressEvent("framework", "duplicates", "Duplicados", 1, 2, "archivos"),
        ProgressEvent("framework", "prepare", "Preparación", 1, 1, "fase"),
    )
    for event in events:
        reporter(event)
    rendered = _snapshot(reporter)
    expected_groups = (
        "Preparación", "Inventario y validación", "Procesamiento por rutas",
        "Catálogos y Semantic", "Cierre",
    )
    assert [rendered.index(group) for group in expected_groups] == sorted(
        rendered.index(group) for group in expected_groups
    )
    for group in expected_groups[1:]:
        assert f"\n\n{group}\n" in rendered
    assert rendered.index("Tipos de contenido") < rendered.index("Validar duplicados")
    assert "Aplicar duplicados" in rendered
    assert len(reporter._progress.tasks) == len(events)
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
        reporter = RichProgress(transient=False, refresh_per_second=30, details=False)
        reporter(ProgressEvent("docx", "extract", "DOCX", 3, 30, "documentos"))
        reporter(ProgressEvent("text", "extract", "Texto", 5, 50, "documentos"))
        reporter(ProgressEvent("framework", "prepare", "Preparado", 1, 1, "fase", True))
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
            assert len(reporter._progress.tasks) == 4  # type: ignore[attr-defined]
            task = next(task for task in reporter._progress.tasks
                        if task.fields["operation"] == "pdf")
            row = rich_progress._task_cells(task)
            view = _snapshot(reporter)
            assert all(cell_len(line) <= columns for line in view.splitlines())
            assert progress_text.removesuffix(" PDF") in view

            if event.description == "PDF fallido":
                assert task.total is None
                assert row["errors"].plain == "9"
                assert row["status"].plain == "Fallido"
                assert "Fallido" in view
            if event.description == "Reanudando PDF":
                assert task.finished is False
                assert task.stop_time is None
                assert row["status"].plain == "En curso"
                assert "Fallido" not in view
                assert "Descripción anterior" not in view

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
