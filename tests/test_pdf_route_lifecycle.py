# region [00] Contexto del módulo
# Módulo: tests/test_pdf_route_lifecycle.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

from unittest.mock import Mock

from neocortex.capabilities.formats.pdf.pdf_derived import PdfDerivedSummary
from neocortex.capabilities.formats.pdf.pdf_route import (
    PdfRoute,
    _ExtractionStats,
    _IsolatedExtractionState,
    _PdfRunPlan,
)
from neocortex.capabilities.formats.pdf.pdf_route_models import (
    CacheDecision,
    DocumentResult,
    PdfRouteSummary,
)
# endregion [01]

# region [02] Implementación


def test_run_delegates_phases_in_durable_order() -> None:
    route = object.__new__(PdfRoute)
    calls: list[str] = []
    plan = _PdfRunPlan(False, False, False, 8, 6, 6, iter(()))
    extraction = _ExtractionStats(total=4, processed=4)
    derived = PdfDerivedSummary()
    expected = PdfRouteSummary(candidate_pool=8, candidates=4)

    def prepare() -> _PdfRunPlan:
        calls.append("prepare")
        return plan

    def extract(value: _PdfRunPlan) -> _ExtractionStats:
        assert value is plan
        calls.append("extraction")
        return extraction

    def deduplicate(skipped: bool) -> tuple[int, int, int, int]:
        assert skipped is False
        calls.append("text_dedup")
        return 1, 2, 0, 0

    def derive(skipped: bool) -> tuple[PdfDerivedSummary, int, int]:
        assert skipped is False
        calls.append("derived")
        return derived, 3, 5

    def summarize(*args: object) -> PdfRouteSummary:
        assert args
        calls.append("summary")
        return expected

    route._prepare_run = Mock(  # type: ignore[method-assign]
        side_effect=prepare
    )
    route._run_extraction_phase = Mock(  # type: ignore[method-assign]
        side_effect=extract
    )
    route._run_text_dedup_phase = Mock(  # type: ignore[method-assign]
        side_effect=deduplicate
    )
    route._run_derived_phase = Mock(  # type: ignore[method-assign]
        side_effect=derive
    )
    route._build_run_summary = Mock(  # type: ignore[method-assign]
        side_effect=summarize
    )

    assert route.run() is expected
    assert calls == ["prepare", "extraction", "text_dedup", "derived", "summary"]
    route._run_extraction_phase.assert_called_once_with(plan)
    route._run_text_dedup_phase.assert_called_once_with(False)
    route._run_derived_phase.assert_called_once_with(False)
    route._build_run_summary.assert_called_once_with(
        plan,
        extraction,
        (1, 2, 0, 0),
        derived,
        3,
        5,
    )


def test_extraction_stats_preserve_cache_and_result_classification() -> None:
    stats = _ExtractionStats()
    stats.register_cache_miss(CacheDecision(False))
    stats.register_cache_miss(
        CacheDecision(False, prior_status="partial", retry_pages=7)
    )
    stats.register_cache_miss(CacheDecision(False, prior_status="stale"))

    stats.register_result(DocumentResult("done", native_pages=2))
    stats.register_result(
        DocumentResult(
            "partial",
            ocr_pages=3,
            page_errors=1,
            timed_out=True,
            warning_count=2,
        )
    )
    stats.register_result(DocumentResult("protected"))
    stats.register_result(DocumentResult("error", recycled=True))

    assert stats.new_documents == 1
    assert stats.retried_documents == 1
    assert stats.retry_pages_planned == 7
    assert stats.cache_refreshes == 1
    assert stats.extracted == 2
    assert stats.partial_documents == 1
    assert stats.protected == 1
    assert stats.errors == 1
    assert stats.native_pages == 2
    assert stats.ocr_pages == 3
    assert stats.page_errors == 1
    assert stats.document_timeouts == 1
    assert stats.unrecoverable_recycled == 1
    assert stats.warning_documents == 1
    assert stats.mupdf_warnings == 2


def test_structural_restart_resets_attempt_but_retains_diagnostics_context() -> None:
    recovery: dict[str, object] = {"engine": "qpdf"}
    state = _IsolatedExtractionState(
        initial_staged_pages=4,
        native_pages=2,
        ocr_pages=1,
        page_errors=3,
        page_count=10,
        start=1,
        end=8,
        metadata={"title": "fixture"},
        prepared=True,
        warning_count=2,
        warning_samples=("warning",),
        recovery_evidence=recovery,
        page_error_limit={"skipped_pages": 2},
        batch=[("page", 1, "native", "text")],
        batch_bytes=4,
    )

    state.reset_for_structural_recovery()

    assert state.native_pages == state.ocr_pages == state.page_errors == 0
    assert state.batch == []
    assert state.batch_bytes == 0
    assert state.page_diagnostic is None
    assert state.page_error_limit is None
    assert state.prepared is False
    assert state.page_count == 10
    assert state.metadata == {"title": "fixture"}
    assert state.warning_count == 2
    assert state.warning_samples == ("warning",)
    assert state.recovery_evidence is recovery
# endregion [02]
