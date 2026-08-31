"""Compatibility and domain projections for application configuration."""
# region [00] Contexto del módulo
# Módulo: tests/test_application_config.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

import neocortex.runtime.config.application_config_projections as runtime_projections
from neocortex.api.public import ApplicationConfig, FrameworkConfig
from neocortex.runtime.config.application_config import (
    archive_route_config_from_application,
    code_route_config_from_application,
    docx_route_config_from_application,
    global_resource_limits_from_application,
    pdf_route_config_from_application,
    text_route_config_from_application,
)
from neocortex.capabilities.formats.archive.route import ArchiveRouteConfig
from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.code.code_contracts import CodeRouteConfig
from neocortex.capabilities.formats.docx.models import DocxRouteConfig
from neocortex.runtime.control.global_resources import GlobalResourceLimits
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig
from neocortex.safety.route_filters import CandidateSelection
from neocortex.runtime.orchestration.route_registry import (
    RouteAdapter,
    archive_route_config_from_framework,
    code_route_config_from_framework,
    docx_route_config_from_framework,
    pdf_route_config_from_framework,
    text_route_config_from_framework,
)
from neocortex.capabilities.formats.text.text_route import TextRouteConfig
# endregion [01]

# region [02] Implementación


def test_application_config_preserves_the_product_dataclass() -> None:
    assert ApplicationConfig is FrameworkConfig
    application_fields = fields(ApplicationConfig)
    assert len(application_fields) == 175
    field_names = {item.name for item in application_fields}
    assert {
        "code_candidate_scope",
        "code_project_roots",
        "archive_max_depth",
        "archive_max_members",
        "archive_max_total_uncompressed_bytes",
        "archive_ocr_mode",
        "archive_ocr_max_pages",
        "text_max_file_bytes",
        "text_max_text_chars",
        "text_libreoffice_cmd",
        "video_max_frames",
        "video_interval_seconds",
        "video_worker_memory_bytes",
        "video_ocr_lang",
        "video_ocr_profile",
        "image_document_ocr_profile",
        "pdf_ocr_profile",
    } <= field_names
    assert not field_names.intersection(
        {
            "analysis_profile",
            "deep_test_selectors",
            "deep_max_tests",
            "deep_time_budget_seconds",
            "deep_shard_size",
            "deep_mutation_target",
            "deep_mutation_symbol",
            "deep_mutation_max_mutants",
            "deep_mutation_timeout_seconds",
            "code_validation_baseline",
            "code_validation_time_budget_seconds",
        }
    )
    base = Path("synthetic-application-config")

    original = ApplicationConfig(
        root=base / "requested-root",
        state_directory=base / "requested-state",
        route="none",
    )
    canonical = replace(
        original,
        root=base / "canonical-root",
        state_directory=base / "canonical-state",
    )

    assert type(canonical) is FrameworkConfig
    assert original.root == base / "requested-root"
    assert canonical.framework_database == (base / "canonical-state" / "framework.sqlite3")
    assert canonical.code_database == base / "canonical-state" / "code.sqlite3"
    assert canonical.archive_database == base / "canonical-state" / "archive.sqlite3"
    assert canonical.text_database == base / "canonical-state" / "text.sqlite3"
    assert canonical.video_database == base / "canonical-state" / "video.sqlite3"


def test_application_facade_reexports_the_runtime_projections() -> None:
    assert (
        archive_route_config_from_application
        is runtime_projections.archive_route_config_from_application
    )
    assert (
        code_route_config_from_application is runtime_projections.code_route_config_from_application
    )
    assert (
        docx_route_config_from_application is runtime_projections.docx_route_config_from_application
    )
    assert (
        global_resource_limits_from_application
        is runtime_projections.global_resource_limits_from_application
    )
    assert (
        pdf_route_config_from_application is runtime_projections.pdf_route_config_from_application
    )
    assert (
        text_route_config_from_application is runtime_projections.text_route_config_from_application
    )


def test_text_projection_preserves_all_limits_and_selection() -> None:
    selection = CandidateSelection(paths=("mensaje.eml",))
    config = ApplicationConfig(
        state_directory=Path("text-state"),
        selection=selection,
        text_max_file_bytes=5_000_000,
        text_max_documents=31,
        text_max_text_chars=456_789,
        text_worker_timeout_seconds=12.5,
        text_worker_memory_bytes=678_000_000,
        text_retry_errors=True,
        text_libreoffice_cmd=r"C:\Program Files\LibreOffice\program\soffice.exe",
    )
    expected = TextRouteConfig(
        state_path=Path("text-state") / "text.sqlite3",
        max_file_bytes=5_000_000,
        max_documents=31,
        max_text_chars=456_789,
        worker_timeout_seconds=12.5,
        worker_memory_bytes=678_000_000,
        retry_errors=True,
        libreoffice_cmd=r"C:\Program Files\LibreOffice\program\soffice.exe",
        selection=selection,
    )

    assert text_route_config_from_application(config) == expected
    assert text_route_config_from_framework(config) == expected


def test_archive_projection_preserves_recursive_limits_and_selection() -> None:
    selection = CandidateSelection(paths=("nested.zip",))
    config = ApplicationConfig(
        state_directory=Path("archive-state"),
        selection=selection,
        archive_max_file_bytes=80_000_000,
        archive_max_documents=23,
        archive_retry_errors=True,
        archive_max_depth=7,
        archive_max_members=12_345,
        archive_max_central_directory_bytes=7_000_000,
        archive_max_member_bytes=8_000_000,
        archive_max_total_uncompressed_bytes=90_000_000,
        archive_max_text_chars=456_789,
        archive_max_total_text_chars=4_567_890,
        archive_max_compression_ratio=88.0,
        archive_pdf_max_pages=321,
        archive_pdf_timeout_seconds=12.0,
        archive_pdf_worker_memory_bytes=456_000_000,
    )
    expected = ArchiveRouteConfig(
        state_path=Path("archive-state") / "archive.sqlite3",
        max_file_bytes=80_000_000,
        max_documents=23,
        retry_errors=True,
        selection=selection,
        max_depth=7,
        max_members=12_345,
        max_central_directory_bytes=7_000_000,
        max_member_bytes=8_000_000,
        max_total_uncompressed_bytes=90_000_000,
        max_text_chars=456_789,
        max_total_text_chars=4_567_890,
        max_compression_ratio=88.0,
        pdf_max_pages=321,
        pdf_timeout_seconds=12.0,
        pdf_worker_memory_bytes=456_000_000,
    )

    assert archive_route_config_from_application(config) == expected
    assert archive_route_config_from_framework(config) == expected


def test_default_code_projection_uses_current_canonical_paths() -> None:
    requested = ApplicationConfig(
        state_directory=Path("requested-code-state"),
    )
    canonical = replace(
        requested,
        state_directory=Path("canonical-code-state"),
    )

    projected = code_route_config_from_application(canonical)
    expected = CodeRouteConfig(
        state_path=Path("canonical-code-state") / "code.sqlite3",
        dedup_path=Path("canonical-code-state") / "dedup.sqlite3",
        candidate_scope="projects",
        explicit_project_roots=canonical.code_project_roots,
        include_generated=False,
        include_vendored=False,
    )

    assert requested.code_database == Path("requested-code-state") / "code.sqlite3"
    assert projected == expected
    assert projected.processing_signature == expected.processing_signature


def test_code_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("error",),
        error_types=("PermissionError",),
        recommendations=("retry",),
        paths=(r"C:\Corpus\sample.py",),
    )
    config = ApplicationConfig(
        state_directory=Path("overridden-code-state"),
        selection=selection,
        code_max_file_bytes=9_000_000,
        code_max_documents=17,
        code_max_text_chars=345_678,
        code_chunk_chars=4_096,
        code_retry_errors=True,
        code_cache_validation="full",
        code_candidate_scope="broad",
        code_include_generated=False,
        code_include_vendored=False,
        code_complexity_warning=23,
        code_function_lines_warning=321,
    )
    expected = CodeRouteConfig(
        state_path=Path("overridden-code-state") / "code.sqlite3",
        dedup_path=Path("overridden-code-state") / "dedup.sqlite3",
        max_file_bytes=9_000_000,
        max_documents=17,
        max_text_chars=345_678,
        chunk_chars=4_096,
        retry_errors=True,
        cache_validation="full",
        candidate_scope="broad",
        explicit_project_roots=config.code_project_roots,
        include_generated=False,
        include_vendored=False,
        complexity_warning=23,
        function_lines_warning=321,
        selection=selection,
    )

    projected = code_route_config_from_application(config)

    assert projected == expected
    assert projected.processing_signature == expected.processing_signature


def test_route_registry_preserves_the_legacy_code_projection_name() -> None:
    config = ApplicationConfig(state_directory=Path("legacy-code-state"))

    with patch(
        "neocortex.runtime.config.application_config_projections.code_route_config_from_application",
        wraps=code_route_config_from_application,
    ) as projection:
        legacy = code_route_config_from_framework(config)

    projection.assert_called_once_with(config)
    assert legacy == code_route_config_from_application(config)


def test_default_pdf_and_docx_projections_use_current_canonical_paths() -> None:
    requested = ApplicationConfig(
        state_directory=Path("requested-document-state"),
    )
    canonical = replace(
        requested,
        state_directory=Path("canonical-document-state"),
    )

    pdf = pdf_route_config_from_application(canonical)
    expected_pdf = PdfRouteConfig(
        state_path=Path("canonical-document-state") / "pdf.sqlite3",
        document_timeout_seconds=600.0,
    )
    docx = docx_route_config_from_application(canonical)
    expected_docx = DocxRouteConfig(
        state_path=Path("canonical-document-state") / "docx.sqlite3",
    )

    assert requested.pdf_database == Path("requested-document-state") / "pdf.sqlite3"
    assert requested.docx_database == (Path("requested-document-state") / "docx.sqlite3")
    assert pdf == expected_pdf
    assert docx == expected_docx
    assert pdf.processing_signature == expected_pdf.processing_signature
    assert docx.processing_signature == expected_docx.processing_signature


def test_pdf_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("partial",),
        error_types=("PdfReadError",),
        recommendations=("retry",),
        paths=(r"C:\Corpus\manual.pdf",),
        failed_pages_only=True,
    )
    config = ApplicationConfig(
        state_directory=Path("overridden-pdf-state"),
        apply_actions=True,
        resume_run_id=41,
        selection=selection,
        pdf_ocr_mode="never",
        pdf_ocr_lang="eng",
        pdf_dpi=300,
        pdf_workers=3,
        pdf_ocr_workers=1,
        pdf_min_page_chars=55,
        pdf_max_page_text_chars=1_234_567,
        pdf_max_render_pixels=9_876_543,
        pdf_max_pages=120,
        pdf_max_file_bytes=80_000_000,
        pdf_max_documents=19,
        pdf_max_ocr_pages=12,
        pdf_ocr_timeout_seconds=45,
        pdf_retry_errors=True,
        pdfminer_fallback=False,
        pdf_similarity_threshold=0.87,
        pdf_cache_validation="full",
        pdf_tesseract_cmd=r"C:\Tools\tesseract.exe",
        pdf_tessdata_dir=r"C:\Tools\tessdata",
        pdf_page_start=2,
        pdf_page_end=11,
        pdf_fail_fast_pages=True,
        pdf_document_timeout_seconds=240.0,
        pdf_timeout_mode="fixed",
        pdf_max_document_timeout_seconds=480.0,
        pdf_min_free_bytes=111_000_000,
        pdf_memory_backpressure_bytes=222_000_000,
        pdf_commit_backpressure_bytes=333_000_000,
        pdf_memory_budget_bytes=444_000_000,
        pdf_worker_memory_bytes=555_000_000,
        pdf_memory_wait_timeout_seconds=22.5,
        pdf_large_document_bytes=66_000_000,
        pdf_large_document_workers=1,
    )
    expected = PdfRouteConfig(
        state_path=Path("overridden-pdf-state") / "pdf.sqlite3",
        apply_actions=True,
        ocr_mode="never",
        ocr_lang="eng",
        dpi=300,
        workers=3,
        ocr_workers=1,
        min_page_chars=55,
        max_page_text_chars=1_234_567,
        max_render_pixels=9_876_543,
        max_pages=120,
        max_file_bytes=80_000_000,
        max_documents=19,
        max_ocr_pages=12,
        ocr_timeout_seconds=45,
        retry_errors=True,
        selection=selection,
        resume_source_run_id=41,
        pdfminer_fallback=False,
        similarity_threshold=0.87,
        cache_validation="full",
        tesseract_cmd=r"C:\Tools\tesseract.exe",
        tessdata_dir=r"C:\Tools\tessdata",
        page_start=2,
        page_end=11,
        fail_fast_pages=True,
        document_timeout_seconds=240.0,
        timeout_mode="fixed",
        max_document_timeout_seconds=480.0,
        min_free_bytes=111_000_000,
        memory_backpressure_bytes=222_000_000,
        commit_backpressure_bytes=333_000_000,
        memory_budget_bytes=444_000_000,
        worker_memory_bytes=555_000_000,
        memory_wait_timeout_seconds=22.5,
        large_document_bytes=66_000_000,
        large_document_workers=1,
    )

    projected = pdf_route_config_from_application(config)

    assert projected == expected
    assert projected.processing_signature == expected.processing_signature


def test_docx_projection_preserves_every_override_and_signature_input() -> None:
    selection = CandidateSelection(
        statuses=("error",),
        recommendations=("manual_review",),
        paths=(r"C:\Corpus\report.docx",),
    )
    config = ApplicationConfig(
        state_directory=Path("overridden-docx-state"),
        selection=selection,
        docx_max_file_bytes=20_000_000,
        docx_max_documents=7,
        docx_max_text_chars=876_543,
        docx_retry_errors=True,
        docx_memory_budget_bytes=333_000_000,
        docx_min_free_memory_bytes=444_000_000,
        docx_min_free_commit_bytes=555_000_000,
        docx_memory_wait_timeout_seconds=19.5,
    )
    expected = DocxRouteConfig(
        state_path=Path("overridden-docx-state") / "docx.sqlite3",
        max_file_bytes=20_000_000,
        max_documents=7,
        max_text_chars=876_543,
        retry_errors=True,
        selection=selection,
        memory_budget_bytes=333_000_000,
        min_free_memory_bytes=444_000_000,
        min_free_commit_bytes=555_000_000,
        memory_wait_timeout_seconds=19.5,
    )

    projected = docx_route_config_from_application(config)

    assert projected == expected
    assert projected.processing_signature == expected.processing_signature


def test_route_registry_delegates_pdf_and_docx_projections() -> None:
    config = ApplicationConfig(state_directory=Path("legacy-document-state"))

    with patch(
        "neocortex.runtime.config.application_config_projections.pdf_route_config_from_application",
        wraps=pdf_route_config_from_application,
    ) as pdf_projection:
        legacy_pdf = pdf_route_config_from_framework(config)
    with patch(
        "neocortex.runtime.config.application_config_projections.docx_route_config_from_application",
        wraps=docx_route_config_from_application,
    ) as docx_projection:
        legacy_docx = docx_route_config_from_framework(config)

    pdf_projection.assert_called_once_with(config)
    docx_projection.assert_called_once_with(config)
    assert legacy_pdf == pdf_route_config_from_application(config)
    assert legacy_docx == docx_route_config_from_application(config)


def test_default_application_values_project_to_existing_resource_defaults() -> None:
    assert global_resource_limits_from_application(ApplicationConfig()) == GlobalResourceLimits()


def test_resource_projection_preserves_every_explicit_application_value() -> None:
    config = ApplicationConfig(
        global_memory_budget_bytes=4_000_000_000,
        global_min_free_memory_bytes=1_500_000_000,
        global_min_free_commit_bytes=1_250_000_000,
        global_cpu_slots=7,
        global_max_cpu_load_percent=73.5,
        global_resource_wait_timeout_seconds=41.25,
    )

    assert global_resource_limits_from_application(config) == GlobalResourceLimits(
        memory_budget_bytes=4_000_000_000,
        min_free_memory_bytes=1_500_000_000,
        min_free_commit_bytes=1_250_000_000,
        cpu_slots=7,
        max_cpu_load_percent=73.5,
        wait_timeout_seconds=41.25,
    )


def test_cli_values_reach_the_resource_domain_without_unit_drift() -> None:
    args = build_parser().parse_args(
        [
            "--route",
            "pdf,docx",
            "--global-memory-budget-mb",
            "384",
            "--global-min-free-memory-mb",
            "128",
            "--global-min-free-commit-mb",
            "64",
            "--global-cpu-slots",
            "3",
            "--global-max-cpu-load-percent",
            "72.5",
            "--global-resource-wait-timeout",
            "17.25",
        ]
    )
    validate_arguments(args)

    limits = global_resource_limits_from_application(framework_config_from_args(args))

    assert limits == GlobalResourceLimits(
        memory_budget_bytes=384 * 1024 * 1024,
        min_free_memory_bytes=128 * 1024 * 1024,
        min_free_commit_bytes=64 * 1024 * 1024,
        cpu_slots=3,
        max_cpu_load_percent=72.5,
        wait_timeout_seconds=17.25,
    )


def test_orchestrator_consumes_the_domain_projection() -> None:
    registry = {
        "first": RouteAdapter("first", lambda _context: {}),
        "second": RouteAdapter("second", lambda _context: {}),
    }
    config = ApplicationConfig(route="first,second")
    orchestrator = FrameworkOrchestrator(config, route_registry=registry)
    projected = GlobalResourceLimits(cpu_slots=2)

    with (
        patch(
            "neocortex.runtime.orchestration.orchestrator.global_resource_limits_from_application",
            return_value=projected,
        ) as projection,
        patch("neocortex.runtime.orchestration.orchestrator.GlobalResourceCoordinator") as coordinator,
    ):
        result = orchestrator._resource_coordinator()

    projection.assert_called_once_with(config)
    coordinator.assert_called_once_with(
        ("first", "second"),
        projected,
        cancellation=orchestrator._cancellation,
    )
    assert result is coordinator.return_value


# endregion [02]
