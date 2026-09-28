"""No hidden history, truthful physical counters and distinct producer phases."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from neocortex.api.cli.cli_reporting import has_organization_errors, print_professional_summary
from neocortex.documents.document_organization_models import OrganizationApplySummary
from neocortex.progress import ProgressEvent, ProgressMetric, RichProgress
from neocortex.progress.rich import _group_index, _task_label


def test_terminal_overflow_switches_once_to_append_only_history(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = StringIO()
    reporter = RichProgress(console=Console(file=output, width=120, height=8, force_terminal=True))
    reporter(ProgressEvent("pdf", "extract", "PDF", 1, 20))
    reporter(ProgressEvent("text", "extract", "Texto", 2, 30))
    assert reporter._scrollback
    assert not reporter._progress.live.is_started
    output.seek(0)
    output.truncate()
    event = ProgressEvent("office", "format:xlsx", "XLSX", 321, 321, finished=True)
    for _ in range(20):
        reporter(event)
    reporter.stop()
    raw = output.getvalue()
    assert raw.count("321/321") == 1
    assert "XLSX" in raw
    assert "Vista compacta" not in raw
    assert "\x1b[2K" not in raw  # existing scrollback is not erased


def test_scrollback_flushes_latest_nonterminal_count_on_stop(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = StringIO()
    reporter = RichProgress(console=Console(file=output, width=120, height=5, force_terminal=True))
    reporter(ProgressEvent("pdf", "extract", "PDF", 1, None))
    for count in range(2, 101):
        reporter(ProgressEvent("pdf", "extract", "PDF", count, None))
    reporter.stop()
    assert "100/?" in output.getvalue()
    assert len(reporter._progress.tasks) == 1
    assert reporter._progress.tasks[0].fields["event_finished"] is False


def test_labels_distinguish_format_catalog_and_vector_scope() -> None:
    assert _task_label("office", "catalog-xlsx") == "XLSX / Catálogo"
    assert _task_label("office", "catalog-pptx") == "PPTX / Catálogo"
    assert _task_label("text", "format:xml") == "XML / Extracción"
    assert _task_label("text", "catalog-text") == "Texto / Catálogo"
    assert _task_label("semantic", "generation:3", {"source_scope": "image-ocr"}) == "Vectores OCR imagen G3"


def test_narrow_view_keeps_primary_time_cache_and_waits_without_details() -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=40), transient=True, details=False)
    reporter(ProgressEvent(
        "pdf", "extract", "PDF", 123456, 246912, metrics=(
            ProgressMetric("cache_hits", 23), ProgressMetric("new_work", 9),
            ProgressMetric("memory_waits", 7), ProgressMetric("errors", 0),
        ),
    ))
    output = StringIO()
    Console(file=output, width=40).print(reporter._progress.get_renderable())
    text = " ".join(output.getvalue().split())
    assert "Tiempo:" in text and "Caché: 23" in text and "Esperas: 7" in text
    assert "123456/246912" in text
    reporter.stop()


def test_apply_metrics_are_visible_without_optional_details() -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=140), transient=True)
    reporter(ProgressEvent(
        "catalog", "organization-apply", "Aplicar", 40, 40, finished=True,
        metrics=tuple(ProgressMetric(k, v) for k, v in {
            "applied": 0, "blocked": 40, "advisory_blocked": 40, "cache_pending": 0,
        }.items()),
    ))
    output = StringIO()
    Console(file=output, width=140).print(reporter._progress.get_renderable())
    text = " ".join(output.getvalue().split())
    assert "aplicados=0" in text and "bloqueados=40" in text
    assert "abstenciones advisory=40" in text
    reporter.stop()


def test_text_format_metrics_are_shown_without_details_or_new_tasks() -> None:
    reporter = RichProgress(console=Console(file=StringIO(), width=140), transient=True)
    reporter(ProgressEvent(
        "text", "extract", "Texto", 4, 4, finished=True,
        metrics=tuple(ProgressMetric(k, v) for k, v in {
            "format:candidates:xml": 3, "format:processed:xml": 3,
            "format:extracted:xml": 3, "format:candidates:email": 1,
            "format:processed:email": 1, "format:cache_hits:email": 1,
        }.items()),
    ))
    output = StringIO()
    Console(file=output, width=140).print(reporter._progress.get_renderable())
    text = " ".join(output.getvalue().split())
    assert "XML" in text and "EML" in text
    assert "extraídos=3" in text and "caché=1" in text
    assert len(reporter._progress.tasks) == 1  # presentation is not another route
    reporter.stop()


def test_advisory_apply_is_not_reclassified_as_error_by_its_plan() -> None:
    result = SimpleNamespace(
        organization_plan=SimpleNamespace(blocked=40),
        organization_apply=OrganizationApplySummary(
            catalog_run_id=1, selected=40, blocked=40, advisory_blocked=40,
        ),
    )
    assert not has_organization_errors(result)
    result.organization_apply = None
    assert has_organization_errors(result)


def test_human_summary_reports_zero_effects_and_directory_skips(capsys, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "240")
    result = SimpleNamespace(
        run_id=1, route_results={},
        organization_plan=SimpleNamespace(considered=143, planned=40, review_required=103, blocked=0),
        organization_apply=OrganizationApplySummary(
            catalog_run_id=1, selected=40, blocked=40, advisory_blocked=40,
        ),
        actions=SimpleNamespace(
            errors=0, apply_actions=True, empty_directory_candidates=186,
            empty_directories_trashed=0, empty_directory_skips=186,
        ),
    )
    print_professional_summary(result, SimpleNamespace(all=True, no_document_catalog=True))
    text = " ".join(capsys.readouterr().out.split())
    assert "movimientos aplicados=0" in text and "advisory=40" in text
    assert "candidatos=186" in text and "retirados=0" in text and "omitidos=186" in text
    assert "modo apply" in text


def test_summary_separates_vector_publication_from_visual_readiness(capsys) -> None:
    result = SimpleNamespace(run_id=1, route_results={}, organization_plan=None, organization_apply=None)
    args = SimpleNamespace(all=True, no_document_catalog=True, _semantic_image_readiness={
        "status": "requires_calibration", "reason": "image_retrieval_not_calibrated",
        "action": "calibrate",
    })
    print_professional_summary(result, args)
    text = " ".join(capsys.readouterr().out.split())
    assert "Búsqueda visual: NO HABILITADA" in text
    assert "--semantic-image-calibrate" in text


def test_post_run_readiness_uses_public_owner_reader_and_fails_closed(tmp_path, monkeypatch) -> None:
    from neocortex.api.cli.cli_app import _post_run_image_readiness
    from neocortex.semantic import image_retrieval_calibration as calibration

    calls = []

    def observe(path):
        calls.append(path)
        raise RuntimeError("new writer; no bypass allowed")

    monkeypatch.setattr(calibration, "image_retrieval_readiness", observe)
    args = SimpleNamespace(all=True, state_directory=tmp_path)
    assert _post_run_image_readiness(args, SimpleNamespace(image=None)) is None
    assert not calls
    observed = _post_run_image_readiness(args, SimpleNamespace(image=SimpleNamespace(candidates=76)))
    assert calls == [tmp_path / "semantic.sqlite3"]
    assert observed is not None and observed["status"] == "unavailable"
    assert observed["action"] == "inspect_semantic_status_when_quiescent"


def test_email_zip_progress_has_independent_label_and_inventory_group() -> None:
    assert _task_label("email-zip-intake", "process") == "ZIP de adjuntos EML"
    assert _task_label("zip-intake", "process") == "Revisar contenedores"
    assert _group_index("email-zip-intake", "process") == 1
    assert _group_index("framework", "email-zip-intake-reconciliation") == 1
    assert _task_label("framework", "email-zip-intake-reconciliation") == "Conciliar ZIP de EML"


def test_framework_email_stage_is_not_rendered_as_closure() -> None:
    assert _group_index("framework", "email-intake") == 1
    assert _task_label("framework", "email-intake") == "Adjuntos de correo"
