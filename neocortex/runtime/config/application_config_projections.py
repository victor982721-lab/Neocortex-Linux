"""Import-light projections from the flat application configuration.

This module defers owner contracts until a projection call or explicit runtime
type-hint resolution.  Tests, help and unrelated routes therefore keep their
dependency doubles and lazy-load guarantees intact.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

# region [01] Static contracts and public projection surface


class _DeferredTypeModule:
    """Resolve annotation types without importing owners on a cold import."""

    __slots__ = ("_module", "_module_name")

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: ModuleType | None = None

    def __getattr__(self, name: str) -> object:
        module = self._module
        if module is None:
            module = import_module(self._module_name, __package__)
            self._module = module
        return getattr(module, name)


if TYPE_CHECKING:
    from neocortex.capabilities.formats.audio import models as _audio_contracts
    from neocortex.code import code_contracts as _code_contracts
    from neocortex.runtime.control import global_resources as _resource_contracts
    from neocortex.runtime import models as _application_contracts
    from neocortex.capabilities.formats.office import route as _office_contracts
    from neocortex.capabilities.formats.pdf import pdf_route_models as _pdf_contracts
    from neocortex.capabilities.formats.text import text_route as _text_contracts
    from neocortex.capabilities.formats.video import route as _video_contracts
    from neocortex.capabilities.formats.archive import route as _archive_contracts
    from neocortex.capabilities.formats.docx import models as _docx_contracts
    from neocortex.capabilities.formats.image import route as _image_contracts
else:
    _application_contracts = _DeferredTypeModule("neocortex.runtime.models")
    _archive_contracts = _DeferredTypeModule("neocortex.capabilities.formats.archive.route")
    _audio_contracts = _DeferredTypeModule("neocortex.capabilities.formats.audio.models")
    _code_contracts = _DeferredTypeModule("neocortex.code.code_contracts")
    _docx_contracts = _DeferredTypeModule("neocortex.capabilities.formats.docx.models")
    _image_contracts = _DeferredTypeModule("neocortex.capabilities.formats.image.route")
    _office_contracts = _DeferredTypeModule("neocortex.capabilities.formats.office.route")
    _pdf_contracts = _DeferredTypeModule("neocortex.capabilities.formats.pdf.pdf_route_models")
    _text_contracts = _DeferredTypeModule("neocortex.capabilities.formats.text.text_route")
    _video_contracts = _DeferredTypeModule("neocortex.capabilities.formats.video.route")
    _resource_contracts = _DeferredTypeModule("neocortex.runtime.control.global_resources")

__all__ = [
    "archive_route_config_from_application",
    "audio_route_config_from_application",
    "code_route_config_from_application",
    "docx_route_config_from_application",
    "global_resource_limits_from_application",
    "image_route_config_from_application",
    "office_route_config_from_application",
    "pdf_route_config_from_application",
    "text_route_config_from_application",
    "video_route_config_from_application",
]

# endregion [01]


# region [02] Import-local owner projections


def archive_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _archive_contracts.ArchiveRouteConfig:
    """Project current application values into recursive ZIP indexing."""

    from neocortex.capabilities.formats.archive.route import ArchiveRouteConfig

    return ArchiveRouteConfig(
        state_path=config.archive_database,
        max_file_bytes=config.archive_max_file_bytes,
        max_documents=config.archive_max_documents,
        retry_errors=config.archive_retry_errors,
        selection=config.selection,
        max_depth=config.archive_max_depth,
        max_members=config.archive_max_members,
        max_central_directory_bytes=config.archive_max_central_directory_bytes,
        max_member_bytes=config.archive_max_member_bytes,
        max_total_uncompressed_bytes=config.archive_max_total_uncompressed_bytes,
        max_text_chars=config.archive_max_text_chars,
        max_total_text_chars=config.archive_max_total_text_chars,
        max_compression_ratio=config.archive_max_compression_ratio,
        pdf_max_pages=config.archive_pdf_max_pages,
        pdf_timeout_seconds=config.archive_pdf_timeout_seconds,
        pdf_worker_memory_bytes=config.archive_pdf_worker_memory_bytes,
        ocr_mode=config.archive_ocr_mode,
        ocr_lang=config.archive_ocr_lang,
        ocr_dpi=config.archive_ocr_dpi,
        ocr_max_pages=config.archive_ocr_max_pages,
        ocr_max_render_pixels=config.archive_ocr_max_render_pixels,
        ocr_timeout_seconds=config.archive_ocr_timeout_seconds,
        tesseract_cmd=config.archive_tesseract_cmd,
        tessdata_dir=config.archive_tessdata_dir,
    )


def text_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _text_contracts.TextRouteConfig:
    """Project generic text and legacy Office extraction limits."""

    from neocortex.capabilities.formats.text.text_route import TextRouteConfig

    return TextRouteConfig(
        state_path=config.text_database,
        max_file_bytes=config.text_max_file_bytes,
        max_documents=config.text_max_documents,
        max_text_chars=config.text_max_text_chars,
        worker_timeout_seconds=config.text_worker_timeout_seconds,
        worker_memory_bytes=config.text_worker_memory_bytes,
        retry_errors=config.text_retry_errors,
        libreoffice_cmd=config.text_libreoffice_cmd,
        selection=config.selection,
    )


def audio_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _audio_contracts.AudioRouteConfig:
    """Project current application values into the audio owner's contract."""

    from neocortex.capabilities.formats.audio.models import AudioRouteConfig

    return AudioRouteConfig(
        state_path=config.audio_database,
        model_name=config.audio_model_name,
        device=config.audio_device,
        compute_type=config.audio_compute_type,
        language=config.audio_language,
        beam_size=config.audio_beam_size,
        vad_filter=config.audio_vad_filter,
        include_video=config.audio_include_video,
        max_file_bytes=config.audio_max_file_bytes,
        max_documents=config.audio_max_documents,
        max_duration_seconds=config.audio_max_duration_seconds,
        max_transcript_chars=config.audio_max_transcript_chars,
        max_segments=config.audio_max_segments,
        file_timeout_seconds=config.audio_file_timeout_seconds,
        worker_startup_timeout_seconds=config.audio_worker_startup_timeout_seconds,
        worker_memory_bytes=config.audio_worker_memory_bytes,
        retry_errors=config.audio_retry_errors,
        ffprobe_path=config.audio_ffprobe_path,
        model_cache_directory=config.audio_model_cache_directory,
        local_models_only=config.audio_local_models_only,
        selection=config.selection,
        memory_budget_bytes=config.audio_memory_budget_bytes,
        min_free_memory_bytes=config.audio_min_free_memory_bytes,
        min_free_commit_bytes=config.audio_min_free_commit_bytes,
        memory_wait_timeout_seconds=config.audio_memory_wait_timeout_seconds,
    )


def video_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
    *,
    root: Path | None = None,
) -> _video_contracts.VideoRouteConfig:
    """Project current values into dedicated visual-video inspection."""

    from neocortex.capabilities.formats.video.route import VideoRouteConfig

    return VideoRouteConfig(
        state_path=config.video_database,
        root=config.root if root is None else root,
        audio_state_path=config.audio_database,
        max_file_bytes=config.video_max_file_bytes,
        max_documents=config.video_max_documents,
        max_duration_seconds=config.video_max_duration_seconds,
        max_frames=config.video_max_frames,
        interval_seconds=config.video_interval_seconds,
        scene_threshold=config.video_scene_threshold,
        include_scenes=config.video_include_scenes,
        include_keyframes=config.video_include_keyframes,
        max_frame_pixels=config.video_max_frame_pixels,
        max_frame_side=config.video_max_frame_side,
        probe_timeout_seconds=config.video_probe_timeout_seconds,
        discovery_timeout_seconds=config.video_discovery_timeout_seconds,
        frame_timeout_seconds=config.video_frame_timeout_seconds,
        file_timeout_seconds=config.video_file_timeout_seconds,
        worker_memory_bytes=config.video_worker_memory_bytes,
        retry_errors=config.video_retry_errors,
        ffmpeg_path=config.video_ffmpeg_path,
        ffprobe_path=config.video_ffprobe_path,
        ocr_mode=config.video_ocr_mode,
        ocr_lang=config.video_ocr_lang,
        ocr_profile=config.video_ocr_profile,
        ocr_timeout_seconds=config.video_ocr_timeout_seconds,
        tesseract_cmd=config.video_tesseract_cmd,
        tessdata_dir=config.video_tessdata_dir,
        selection=config.selection,
    )


def code_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _code_contracts.CodeRouteConfig:
    """Project current application values into the code owner's contract."""

    from neocortex.code.code_contracts import CodeRouteConfig

    return CodeRouteConfig(
        state_path=config.code_database,
        dedup_path=config.dedup_database,
        max_file_bytes=config.code_max_file_bytes,
        max_text_chars=config.code_max_text_chars,
        max_documents=config.code_max_documents,
        chunk_chars=config.code_chunk_chars,
        retry_errors=config.code_retry_errors,
        cache_validation=config.code_cache_validation,
        candidate_scope=getattr(config, "code_candidate_scope", "broad"),
        include_generated=config.code_include_generated,
        include_vendored=config.code_include_vendored,
        complexity_warning=config.code_complexity_warning,
        function_lines_warning=config.code_function_lines_warning,
        external_evidence_root=config.root if config.self_analysis else None,
        explicit_project_roots=(
            (config.root,) if config.self_analysis else config.code_project_roots
        ),
        analysis_profile=getattr(config, "analysis_profile", "protected"),
        deep_test_selectors=getattr(config, "deep_test_selectors", ()),
        deep_max_tests=getattr(config, "deep_max_tests", 3000),
        deep_time_budget_seconds=getattr(config, "deep_time_budget_seconds", 600),
        deep_shard_size=getattr(config, "deep_shard_size", 20),
        deep_mutation_target=getattr(config, "deep_mutation_target", None),
        deep_mutation_symbol=getattr(config, "deep_mutation_symbol", None),
        deep_mutation_max_mutants=getattr(config, "deep_mutation_max_mutants", 20),
        deep_mutation_timeout_seconds=getattr(config, "deep_mutation_timeout_seconds", 30),
        deep_mutation_time_budget_seconds=getattr(config, "deep_mutation_time_budget_seconds", 600),
        selection=config.selection,
    )


def docx_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _docx_contracts.DocxRouteConfig:
    """Project current application values into the DOCX owner's contract."""

    from neocortex.capabilities.formats.docx.models import DocxRouteConfig

    return DocxRouteConfig(
        state_path=config.docx_database,
        max_file_bytes=config.docx_max_file_bytes,
        max_documents=config.docx_max_documents,
        max_text_chars=config.docx_max_text_chars,
        retry_errors=config.docx_retry_errors,
        selection=config.selection,
        memory_budget_bytes=config.docx_memory_budget_bytes,
        min_free_memory_bytes=config.docx_min_free_memory_bytes,
        min_free_commit_bytes=config.docx_min_free_commit_bytes,
        memory_wait_timeout_seconds=config.docx_memory_wait_timeout_seconds,
    )


def image_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
    *,
    root: Path | None = None,
) -> _image_contracts.ImageRouteConfig:
    """Project current values and the effective root into the image contract."""

    from neocortex.capabilities.formats.image.route import ImageRouteConfig

    return ImageRouteConfig(
        state_path=config.image_database,
        root=config.root if root is None else root,
        workers=config.image_workers,
        max_file_bytes=config.image_max_file_bytes,
        max_documents=config.image_max_documents,
        retry_errors=config.image_retry_errors,
        selection=config.selection,
        memory_budget_bytes=config.image_memory_budget_bytes,
        min_free_memory_bytes=config.image_min_free_memory_bytes,
        min_free_commit_bytes=config.image_min_free_commit_bytes,
        memory_wait_timeout_seconds=config.image_memory_wait_timeout_seconds,
        worker_timeout_seconds=config.image_worker_timeout_seconds,
        document_ocr_mode=config.image_document_ocr_mode,
        document_ocr_lang=config.image_document_ocr_lang,
        document_ocr_timeout_seconds=config.image_document_ocr_timeout_seconds,
        tesseract_cmd=config.image_tesseract_cmd,
        tessdata_dir=config.image_tessdata_dir,
        document_ocr_profile=config.image_document_ocr_profile,
    )


def office_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _office_contracts.OfficeRouteConfig:
    """Project current application values into the Office owner's contract."""

    from neocortex.capabilities.formats.office.route import OfficeRouteConfig

    return OfficeRouteConfig(
        state_path=config.office_database,
        max_file_bytes=config.office_max_file_bytes,
        max_documents=config.office_max_documents,
        max_text_chars=config.office_max_text_chars,
        retry_errors=config.office_retry_errors,
        selection=config.selection,
        memory_budget_bytes=config.office_memory_budget_bytes,
        min_free_memory_bytes=config.office_min_free_memory_bytes,
        min_free_commit_bytes=config.office_min_free_commit_bytes,
        memory_wait_timeout_seconds=config.office_memory_wait_timeout_seconds,
    )


def pdf_route_config_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _pdf_contracts.PdfRouteConfig:
    """Project current application values into the PDF owner's contract."""

    from neocortex.capabilities.formats.pdf.pdf_route_models import PdfRouteConfig

    return PdfRouteConfig(
        state_path=config.pdf_database,
        apply_actions=config.apply_actions,
        ocr_mode=config.pdf_ocr_mode,
        ocr_lang=config.pdf_ocr_lang,
        dpi=config.pdf_dpi,
        workers=config.pdf_workers,
        ocr_workers=config.pdf_ocr_workers,
        min_page_chars=config.pdf_min_page_chars,
        max_page_text_chars=config.pdf_max_page_text_chars,
        max_render_pixels=config.pdf_max_render_pixels,
        max_pages=config.pdf_max_pages,
        max_file_bytes=config.pdf_max_file_bytes,
        max_documents=config.pdf_max_documents,
        max_ocr_pages=config.pdf_max_ocr_pages,
        ocr_timeout_seconds=config.pdf_ocr_timeout_seconds,
        retry_errors=config.pdf_retry_errors,
        selection=config.selection,
        resume_source_run_id=config.resume_run_id,
        pdfminer_fallback=config.pdfminer_fallback,
        similarity_threshold=config.pdf_similarity_threshold,
        cache_validation=config.pdf_cache_validation,
        tesseract_cmd=config.pdf_tesseract_cmd,
        tessdata_dir=config.pdf_tessdata_dir,
        page_start=config.pdf_page_start,
        page_end=config.pdf_page_end,
        fail_fast_pages=config.pdf_fail_fast_pages,
        document_timeout_seconds=config.pdf_document_timeout_seconds,
        timeout_mode=config.pdf_timeout_mode,
        max_document_timeout_seconds=config.pdf_max_document_timeout_seconds,
        min_free_bytes=config.pdf_min_free_bytes,
        memory_backpressure_bytes=config.pdf_memory_backpressure_bytes,
        commit_backpressure_bytes=config.pdf_commit_backpressure_bytes,
        memory_budget_bytes=config.pdf_memory_budget_bytes,
        worker_memory_bytes=config.pdf_worker_memory_bytes,
        memory_wait_timeout_seconds=config.pdf_memory_wait_timeout_seconds,
        large_document_bytes=config.pdf_large_document_bytes,
        large_document_workers=config.pdf_large_document_workers,
        ocr_profile=config.pdf_ocr_profile,
    )


def global_resource_limits_from_application(
    config: _application_contracts.FrameworkConfig,
) -> _resource_contracts.GlobalResourceLimits:
    """Project current application values into resource-owner limits."""

    from neocortex.runtime.control.global_resources import GlobalResourceLimits

    return GlobalResourceLimits(
        memory_budget_bytes=config.global_memory_budget_bytes,
        min_free_memory_bytes=config.global_min_free_memory_bytes,
        min_free_commit_bytes=config.global_min_free_commit_bytes,
        cpu_slots=config.global_cpu_slots,
        max_cpu_load_percent=config.global_max_cpu_load_percent,
        wait_timeout_seconds=config.global_resource_wait_timeout_seconds,
    )


# endregion [02]
