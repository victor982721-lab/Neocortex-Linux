"""Translation from validated CLI arguments to framework configuration."""


# region [01] Imports and public contract
# Configuration translation is isolated from execution and terminal concerns.

from __future__ import annotations

import argparse

from .app_paths import default_code_project_roots
from .models import FrameworkConfig
from .route_filters import CandidateSelection

__all__ = ["framework_config_from_args"]

# endregion [01]


# region [02] Framework configuration


def framework_config_from_args(args: argparse.Namespace) -> FrameworkConfig:
    """Build the durable framework configuration from validated arguments."""

    return FrameworkConfig(
        root=args.root,
        state_directory=args.state_directory,
        self_analysis=args.self_analysis,
        analysis_profile=args.analysis_profile,
        deep_test_selectors=tuple(args.deep_test_selectors),
        deep_max_tests=args.deep_max_tests,
        deep_time_budget_seconds=args.deep_time_budget_seconds,
        deep_shard_size=args.deep_shard_size,
        deep_mutation_target=args.deep_mutation_target,
        deep_mutation_symbol=args.deep_mutation_symbol,
        deep_mutation_max_mutants=args.deep_mutation_max_mutants,
        deep_mutation_timeout_seconds=args.deep_mutation_timeout_seconds,
        deep_mutation_time_budget_seconds=args.deep_mutation_time_budget_seconds,
        corpus_access_mode="analyze_only" if args.self_analysis else "normal",
        apply_actions=args.apply,
        preview_group_limit=args.show_groups,
        dedup_policy=args.dedup_policy,
        route=args.route,
        route_only=bool(args.route_only or args.resume_run is not None),
        candidate_run_id=args.candidate_run,
        resume_run_id=args.resume_run,
        selection=CandidateSelection.from_values(
            statuses=args.select_status,
            error_types=args.select_error_type,
            recommendations=args.select_recommendation,
            paths=args.select_path,
            failed_pages_only=args.failed_pages_only,
        ),
        document_catalog_enabled=not args.no_document_catalog,
        document_taxonomy_path=args.document_taxonomy,
        organization_root=args.organization_root,
        organization_min_confidence=args.organization_min_confidence,
        global_memory_budget_bytes=(
            None
            if args.global_memory_budget_mb is None
            else args.global_memory_budget_mb * 1024 * 1024
        ),
        global_min_free_memory_bytes=(
            None
            if args.global_min_free_memory_mb is None
            else args.global_min_free_memory_mb * 1024 * 1024
        ),
        global_min_free_commit_bytes=(
            None
            if args.global_min_free_commit_mb is None
            else args.global_min_free_commit_mb * 1024 * 1024
        ),
        global_cpu_slots=args.global_cpu_slots,
        global_max_cpu_load_percent=args.global_max_cpu_load_percent,
        global_resource_wait_timeout_seconds=(args.global_resource_wait_timeout),
        code_max_file_bytes=args.code_max_file_bytes,
        code_max_documents=args.code_max_documents,
        code_max_text_chars=args.code_max_text_chars,
        code_chunk_chars=args.code_chunk_chars,
        code_retry_errors=args.retry_code_errors,
        code_cache_validation=args.code_cache_validation,
        code_candidate_scope=args.code_candidate_scope,
        code_project_roots=(
            default_code_project_roots()
            if args.code_project_root is None
            else tuple(args.code_project_root)
        ),
        code_include_generated=args.code_include_generated,
        code_include_vendored=args.code_include_vendored,
        code_complexity_warning=args.code_complexity_warning,
        code_function_lines_warning=args.code_function_lines_warning,
        image_workers=args.image_workers,
        image_max_file_bytes=args.image_max_file_bytes,
        image_max_documents=args.image_max_documents,
        image_retry_errors=args.retry_image_errors,
        image_memory_budget_bytes=args.image_memory_budget_mb * 1024 * 1024,
        image_min_free_memory_bytes=args.image_min_free_memory_mb * 1024 * 1024,
        image_min_free_commit_bytes=args.image_min_free_commit_mb * 1024 * 1024,
        image_memory_wait_timeout_seconds=args.image_memory_wait_timeout,
        image_worker_timeout_seconds=args.image_worker_timeout,
        image_document_ocr_mode=args.image_document_ocr,
        image_document_ocr_lang=args.image_ocr_lang or args.ocr_lang,
        image_document_ocr_timeout_seconds=args.image_ocr_timeout,
        image_document_ocr_profile=(args.image_ocr_profile or args.ocr_profile),
        image_tesseract_cmd=args.tesseract_cmd,
        image_tessdata_dir=args.tessdata_dir,
        docx_max_file_bytes=args.docx_max_file_bytes,
        docx_max_documents=args.docx_max_documents,
        docx_max_text_chars=args.docx_max_text_chars,
        docx_retry_errors=args.retry_docx_errors,
        docx_memory_budget_bytes=args.docx_memory_budget_mb * 1024 * 1024,
        docx_min_free_memory_bytes=args.docx_min_free_memory_mb * 1024 * 1024,
        docx_min_free_commit_bytes=args.docx_min_free_commit_mb * 1024 * 1024,
        docx_memory_wait_timeout_seconds=args.docx_memory_wait_timeout,
        office_max_file_bytes=args.office_max_file_bytes,
        office_max_documents=args.office_max_documents,
        office_max_text_chars=args.office_max_text_chars,
        office_retry_errors=args.retry_office_errors,
        office_memory_budget_bytes=args.office_memory_budget_mb * 1024 * 1024,
        office_min_free_memory_bytes=(args.office_min_free_memory_mb * 1024 * 1024),
        office_min_free_commit_bytes=(args.office_min_free_commit_mb * 1024 * 1024),
        office_memory_wait_timeout_seconds=args.office_memory_wait_timeout,
        archive_max_file_bytes=args.archive_max_file_bytes,
        archive_max_documents=args.archive_max_documents,
        archive_retry_errors=args.retry_archive_errors,
        archive_max_depth=args.archive_max_depth,
        archive_max_members=args.archive_max_members,
        archive_max_central_directory_bytes=(args.archive_max_central_directory_mb * 1024 * 1024),
        archive_max_member_bytes=args.archive_max_member_mb * 1024 * 1024,
        archive_max_total_uncompressed_bytes=args.archive_max_total_mb * 1024 * 1024,
        archive_max_text_chars=args.archive_max_text_chars,
        archive_max_total_text_chars=args.archive_max_total_text_chars,
        archive_max_compression_ratio=args.archive_max_compression_ratio,
        archive_pdf_max_pages=args.archive_pdf_max_pages,
        archive_pdf_timeout_seconds=args.archive_pdf_timeout,
        archive_pdf_worker_memory_bytes=(args.archive_pdf_worker_memory_mb * 1024 * 1024),
        archive_ocr_mode=args.ocr,
        archive_ocr_lang=args.ocr_lang,
        archive_ocr_dpi=args.pdf_dpi,
        archive_ocr_max_pages=(50 if args.max_ocr_pages is None else args.max_ocr_pages),
        archive_ocr_max_render_pixels=args.pdf_max_render_pixels,
        archive_ocr_timeout_seconds=min(float(args.ocr_timeout), args.archive_pdf_timeout),
        archive_tesseract_cmd=args.tesseract_cmd,
        archive_tessdata_dir=args.tessdata_dir,
        text_max_file_bytes=args.text_max_file_bytes,
        text_max_documents=args.text_max_documents,
        text_max_text_chars=args.text_max_chars,
        text_worker_timeout_seconds=args.text_worker_timeout,
        text_worker_memory_bytes=args.text_worker_memory_mb * 1024 * 1024,
        text_retry_errors=args.retry_text_errors,
        text_libreoffice_cmd=args.libreoffice_path,
        audio_model_name=args.whisper_model,
        audio_device=args.whisper_device,
        audio_compute_type=args.whisper_compute_type,
        audio_language=(None if args.audio_language.casefold() == "auto" else args.audio_language),
        audio_beam_size=args.whisper_beam_size,
        audio_vad_filter=args.audio_vad,
        audio_include_video=args.audio_include_video,
        audio_max_file_bytes=args.audio_max_file_bytes,
        audio_max_documents=args.audio_max_documents,
        audio_max_duration_seconds=args.audio_max_duration_seconds,
        audio_max_transcript_chars=args.audio_max_transcript_chars,
        audio_max_segments=args.audio_max_segments,
        audio_file_timeout_seconds=args.audio_file_timeout,
        audio_worker_startup_timeout_seconds=args.audio_worker_startup_timeout,
        audio_worker_memory_bytes=args.audio_worker_memory_mb * 1024 * 1024,
        audio_retry_errors=args.retry_audio_errors,
        audio_ffprobe_path=args.ffprobe_path,
        audio_model_cache_directory=args.audio_model_cache,
        audio_local_models_only=args.audio_local_models_only,
        audio_memory_budget_bytes=args.audio_memory_budget_mb * 1024 * 1024,
        audio_min_free_memory_bytes=args.audio_min_free_memory_mb * 1024 * 1024,
        audio_min_free_commit_bytes=args.audio_min_free_commit_mb * 1024 * 1024,
        audio_memory_wait_timeout_seconds=args.audio_memory_wait_timeout,
        video_max_file_bytes=args.video_max_file_bytes,
        video_max_documents=args.video_max_documents,
        video_max_duration_seconds=args.video_max_duration_seconds,
        video_max_frames=args.video_max_frames,
        video_interval_seconds=args.video_interval_seconds,
        video_scene_threshold=args.video_scene_threshold,
        video_include_scenes=args.video_include_scenes,
        video_include_keyframes=args.video_include_keyframes,
        video_max_frame_pixels=args.video_max_frame_pixels,
        video_max_frame_side=args.video_max_frame_side,
        video_probe_timeout_seconds=args.video_probe_timeout,
        video_discovery_timeout_seconds=args.video_discovery_timeout,
        video_frame_timeout_seconds=args.video_frame_timeout,
        video_file_timeout_seconds=args.video_file_timeout,
        video_worker_memory_bytes=args.video_worker_memory_mb * 1024 * 1024,
        video_retry_errors=args.retry_video_errors,
        video_ffmpeg_path=args.video_ffmpeg_path,
        video_ffprobe_path=args.video_ffprobe_path,
        video_ocr_mode=args.video_ocr,
        video_ocr_lang=args.video_ocr_lang or args.ocr_lang,
        video_ocr_profile=(args.video_ocr_profile or args.ocr_profile),
        video_ocr_timeout_seconds=args.video_ocr_timeout,
        video_tesseract_cmd=args.tesseract_cmd,
        video_tessdata_dir=args.tessdata_dir,
        pdf_ocr_mode=args.ocr,
        pdf_ocr_lang=args.ocr_lang,
        pdf_ocr_profile=args.ocr_profile,
        pdf_dpi=args.pdf_dpi,
        pdf_workers=args.pdf_workers,
        pdf_ocr_workers=args.ocr_workers,
        pdf_min_page_chars=args.pdf_min_page_chars,
        pdf_max_page_text_chars=args.pdf_max_page_text_chars,
        pdf_max_render_pixels=args.pdf_max_render_pixels,
        pdf_max_pages=args.max_pdf_pages,
        pdf_max_file_bytes=args.pdf_max_file_bytes,
        pdf_max_documents=args.pdf_max_documents,
        pdf_max_ocr_pages=args.max_ocr_pages,
        pdf_ocr_timeout_seconds=args.ocr_timeout,
        pdf_retry_errors=args.retry_pdf_errors,
        pdfminer_fallback=not args.no_pdfminer_fallback,
        pdf_similarity_threshold=args.pdf_similarity,
        pdf_cache_validation=args.pdf_cache_validation,
        pdf_tesseract_cmd=args.tesseract_cmd,
        pdf_tessdata_dir=args.tessdata_dir,
        pdf_page_start=args.pdf_page_start,
        pdf_page_end=args.pdf_page_end,
        pdf_fail_fast_pages=args.pdf_fail_fast_pages,
        pdf_document_timeout_seconds=args.pdf_document_timeout,
        pdf_timeout_mode=args.pdf_timeout_mode,
        pdf_max_document_timeout_seconds=max(
            args.pdf_document_timeout,
            args.pdf_max_document_timeout,
        ),
        pdf_min_free_bytes=args.pdf_min_free_bytes,
        pdf_memory_backpressure_bytes=args.pdf_memory_backpressure_bytes,
        pdf_commit_backpressure_bytes=args.pdf_commit_backpressure_bytes,
        pdf_memory_budget_bytes=args.pdf_memory_budget_bytes,
        pdf_worker_memory_bytes=args.pdf_worker_memory_bytes,
        pdf_memory_wait_timeout_seconds=args.pdf_memory_wait_timeout,
        pdf_large_document_bytes=args.pdf_large_document_bytes,
        pdf_large_document_workers=args.pdf_large_document_workers,
    )


# endregion [02]
